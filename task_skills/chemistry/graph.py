"""Strict chemistry DAG validation and concurrent fail-stop execution."""

from __future__ import annotations

import json
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

from .registry import (
    SUPPORTED_VERSION,
    SkillValidationError,
    derive_resources,
    load_profile,
    normalize_parameters,
)


class GraphValidationError(ValueError):
    """Raised before driver preflight when a graph is malformed or unsafe."""


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dict__"):
        return _json_safe(vars(value))
    return repr(value)


def _is_failure(result: Any) -> bool:
    if result is False:
        return True
    if isinstance(result, dict):
        if result.get("success") is False:
            return True
        if str(result.get("status", "")).lower() in {"failed", "failure", "error", "rejected"}:
            return True
    return False


def _is_simulated(result: Any) -> bool:
    if isinstance(result, dict):
        return result.get("simulated") is True
    return getattr(result, "simulated", False) is True


def unauthorised_gripper_openings(graph: dict[str, Any],
                                  profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Return gripper nodes that will command an opening they are not authorised for.

    The driver refuses any command that widens the jaws unless the node carries
    ``allow_release=True``, because position feedback cannot prove that nothing is
    held. An unauthorised opening is therefore not dangerous, it is unsatisfiable:
    execution stops at that node after the graph has already started moving. It is
    caught here so that a graph which can never finish is rejected before the rig
    is touched. Preparing the jaws belongs in its own explicitly authorised graph.

    The static view is conservative and deliberately incomplete: it tracks the last
    *commanded* position per arm, so it cannot see an opening that no command in
    this graph established, and from an unknown start it only flags a command to a
    wide-open position. The driver's live gate stays the authority; this rejects
    only what the graph text makes knowable.
    """
    gripper = profile.get("gripper") or {}
    open_minimum = gripper.get("open_minimum")
    nodes = {node["id"]: node for node in graph["nodes"]}
    order = graph.get("topological_order") or list(nodes)
    commanded: dict[str, int | None] = {}
    findings: list[dict[str, Any]] = []
    for node_id in order:
        node = nodes[node_id]
        if node["skill"] != "gripper.set_position":
            continue
        parameters = node["parameters"]
        arm = node["arm"]
        position = parameters["position"]
        previous = commanded.get(arm)
        if not parameters["allow_release"]:
            if previous is None and isinstance(open_minimum, int) and position >= open_minimum:
                findings.append({
                    "node": node_id, "arm": arm, "position": position,
                    "reason": "commands a wide-open position from an unknown start without "
                              "allow_release=True; the driver refuses it at execution"})
            elif previous is not None and position > previous:
                findings.append({
                    "node": node_id, "arm": arm, "position": position,
                    "reason": f"opens the jaws from the {previous} commanded earlier in this "
                              "graph without allow_release=True; the driver refuses it at "
                              "execution"})
        commanded[arm] = position
    return findings


def validate_graph(document: Any, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate the entire graph and return normalized nodes with defaults/resources."""
    if profile is None:
        profile = load_profile()
    if not isinstance(document, dict):
        raise GraphValidationError("graph document must be a JSON object")
    allowed_top = {"schema_version", "name", "nodes", "initial_facts", "description"}
    unknown_top = set(document) - allowed_top
    if unknown_top:
        raise GraphValidationError(f"graph has unknown field(s): {', '.join(sorted(unknown_top))}")
    if document.get("schema_version") != SUPPORTED_VERSION:
        raise GraphValidationError(f"schema_version must be {SUPPORTED_VERSION!r}")
    name = document.get("name")
    if not isinstance(name, str) or not name.strip():
        raise GraphValidationError("name must be a non-empty string")
    if "description" in document and not isinstance(document["description"], str):
        raise GraphValidationError("description must be a string")
    initial_facts = document.get("initial_facts", {})
    if not isinstance(initial_facts, dict) or any(not isinstance(key, str) for key in initial_facts):
        raise GraphValidationError("initial_facts must be an object with string keys")
    try:
        json.dumps(initial_facts, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise GraphValidationError(f"initial_facts must contain finite JSON values: {error}") from error

    raw_nodes = document.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise GraphValidationError("nodes must be a non-empty array")
    node_fields = {"id", "skill", "version", "arm", "parameters", "depends_on"}
    required_fields = {"id", "skill", "arm", "parameters"}
    node_by_id: dict[str, dict[str, Any]] = {}
    source_index: dict[str, int] = {}
    normalized_nodes: list[dict[str, Any]] = []

    for index, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, dict):
            raise GraphValidationError(f"nodes[{index}] must be an object")
        unknown = set(raw_node) - node_fields
        missing = required_fields - set(raw_node)
        if unknown:
            raise GraphValidationError(f"nodes[{index}] has unknown field(s): {', '.join(sorted(unknown))}")
        if missing:
            raise GraphValidationError(f"nodes[{index}] is missing field(s): {', '.join(sorted(missing))}")
        node_id = raw_node["id"]
        skill_id = raw_node["skill"]
        arm = raw_node["arm"]
        version = raw_node.get("version", SUPPORTED_VERSION)
        if not isinstance(node_id, str) or not node_id.strip():
            raise GraphValidationError(f"nodes[{index}].id must be a non-empty string")
        if node_id in node_by_id:
            raise GraphValidationError(f"duplicate node id {node_id!r}")
        if not isinstance(skill_id, str) or not skill_id:
            raise GraphValidationError(f"node {node_id!r}.skill must be a non-empty string")
        if version != SUPPORTED_VERSION:
            raise GraphValidationError(f"node {node_id!r} version must be {SUPPORTED_VERSION!r}")
        if arm not in {"left", "right"}:
            raise GraphValidationError(f"node {node_id!r}.arm must be 'left' or 'right'")
        depends_on = raw_node.get("depends_on", [])
        if not isinstance(depends_on, list) or any(not isinstance(item, str) or not item for item in depends_on):
            raise GraphValidationError(f"node {node_id!r}.depends_on must be an array of non-empty node ids")
        if len(set(depends_on)) != len(depends_on):
            raise GraphValidationError(f"node {node_id!r}.depends_on contains duplicates")
        if node_id in depends_on:
            raise GraphValidationError(f"node {node_id!r} cannot depend on itself")
        try:
            parameters = normalize_parameters(skill_id, arm, raw_node["parameters"], profile)
            resources = derive_resources(skill_id, arm, profile)
        except SkillValidationError as error:
            raise GraphValidationError(f"node {node_id!r}: {error}") from error
        node = {
            "id": node_id,
            "skill": skill_id,
            "version": version,
            "arm": arm,
            "parameters": parameters,
            "depends_on": list(depends_on),
            "resources": resources,
        }
        node_by_id[node_id] = node
        source_index[node_id] = index
        normalized_nodes.append(node)

    for node in normalized_nodes:
        missing_dependencies = set(node["depends_on"]) - set(node_by_id)
        if missing_dependencies:
            raise GraphValidationError(f"node {node['id']!r} depends on missing node(s): "
                                       f"{', '.join(sorted(missing_dependencies))}")

    indegree = {node_id: len(node["depends_on"]) for node_id, node in node_by_id.items()}
    dependents = {node_id: [] for node_id in node_by_id}
    for node in normalized_nodes:
        for dependency in node["depends_on"]:
            dependents[dependency].append(node["id"])
    ready = sorted((node_id for node_id, degree in indegree.items() if degree == 0),
                   key=source_index.__getitem__)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for dependent in sorted(dependents[current], key=source_index.__getitem__):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=source_index.__getitem__)
    if len(order) != len(normalized_nodes):
        raise GraphValidationError("graph contains a dependency cycle")

    ancestors: dict[str, set[str]] = {}
    for node_id in order:
        inherited = set(node_by_id[node_id]["depends_on"])
        for dependency in node_by_id[node_id]["depends_on"]:
            inherited.update(ancestors[dependency])
        ancestors[node_id] = inherited
    for left_index, left in enumerate(normalized_nodes):
        for right in normalized_nodes[left_index + 1:]:
            if left["arm"] != right["arm"]:
                continue
            left_before_right = left["id"] in ancestors[right["id"]]
            right_before_left = right["id"] in ancestors[left["id"]]
            if not left_before_right and not right_before_left:
                raise GraphValidationError(
                    f"same-arm nodes {left['id']!r} and {right['id']!r} need a dependency path")

    normalized = {
        "schema_version": SUPPORTED_VERSION,
        "name": name,
        "nodes": normalized_nodes,
        "topological_order": order,
        "initial_facts": dict(initial_facts),
    }
    if "description" in document:
        normalized["description"] = document["description"]
    unsatisfiable = unauthorised_gripper_openings(normalized, profile)
    if unsatisfiable:
        findings = "; ".join(f"{item['node']}: {item['reason']}" for item in unsatisfiable)
        raise GraphValidationError(
            f"graph contains gripper nodes that can never run: {findings}. Move jaw "
            "preparation into a separate graph whose opening nodes set allow_release=True.")
    return normalized


def _simultaneous_pairs(intervals: list[tuple[str, float, float]]) -> list[list[str]]:
    """Node pairs whose measured execution windows intersect.

    This is wall-clock evidence, not a restatement of the DAG. Two nodes can only
    appear together here if the driver really had both arms under command at the
    same time: validation forces every same-arm pair into a dependency chain, so
    a single arm can never be commanded twice at once. A node whose call returns
    in zero measured time cannot overlap anything and will not show up.
    """
    pairs: list[list[str]] = []
    for index, (first_id, first_begin, first_end) in enumerate(intervals):
        for second_id, second_begin, second_end in intervals[index + 1:]:
            if first_begin < second_end and second_begin < first_end:
                pairs.append(sorted((first_id, second_id)))
    return sorted(pairs)


def _close_driver(driver: Any, report: dict[str, Any]) -> None:
    """Release whatever the driver opened, on every exit path including early failures.

    A live driver holds the shared rig lease and the arm sockets, so a declined
    confirmation or a failed preflight must not leak them. A validator that only
    checks a graph opens nothing, so this is keyed on the driver exposing the hook
    rather than on the driver class.
    """
    close = getattr(driver, "close", None)
    if not callable(close):
        return
    try:
        report["close"] = _json_safe(close())
    except Exception as error:  # noqa: BLE001
        report["close_error"] = f"{type(error).__name__}: {error}"


def execute_graph(document: Any, driver: Any, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate first, then run a whole graph serially and stop on the first failure."""
    selected_profile = load_profile() if profile is None else profile
    graph = validate_graph(document, selected_profile)
    node_by_id = {node["id"]: node for node in graph["nodes"]}
    report: dict[str, Any] = {
        "name": graph["name"],
        "status": "running",
        "topological_order": list(graph["topological_order"]),
        "nodes": [],
        "used_arms": [],
        "physical_success_verified": False,
    }
    try:
        preflight = driver.preflight(graph, selected_profile)
        report["preflight"] = _json_safe(preflight)
        if _is_failure(preflight):
            report["status"] = "failed_preflight"
            report["error"] = "driver preflight reported failure"
            report["nodes"] = [{"id": node_id, "status": "not_started"} for node_id in graph["topological_order"]]
            _close_driver(driver, report)
            return report
    except Exception as error:  # noqa: BLE001
        report["status"] = "failed_preflight"
        report["preflight_error"] = f"{type(error).__name__}: {error}"
        report["nodes"] = [{"id": node_id, "status": "not_started"} for node_id in graph["topological_order"]]
        _close_driver(driver, report)
        return report

    confirm = getattr(driver, "confirm", None)
    if callable(confirm):
        try:
            if confirm() is False:
                report["status"] = "aborted"
                report["confirmation"] = "declined"
                report["nodes"] = [{"id": node_id, "status": "not_started"}
                                   for node_id in graph["topological_order"]]
                _close_driver(driver, report)
                return report
            report["confirmation"] = "accepted"
        except Exception as error:  # noqa: BLE001
            report["status"] = "aborted"
            report["confirmation_error"] = f"{type(error).__name__}: {error}"
            report["nodes"] = [{"id": node_id, "status": "not_started"}
                               for node_id in graph["topological_order"]]
            _close_driver(driver, report)
            return report

    order = graph["topological_order"]
    arms = sorted({node_by_id[node_id]["arm"] for node_id in order})
    started_at = time.monotonic()
    reported: dict[str, dict[str, Any]] = {}
    intervals: list[tuple[str, float, float]] = []
    used_arms: list[str] = []
    all_simulated = True
    failed_node: str | None = None
    in_flight: dict[Any, str] = {}
    remaining = set(order)

    def run_node(node_id: str) -> tuple[dict[str, Any], tuple[str, float, float]]:
        node = node_by_id[node_id]
        begin = time.monotonic() - started_at
        try:
            result = driver.execute(node, selected_profile)
            if _is_failure(result):
                raise RuntimeError(f"driver reported failure for node {node_id!r}")
            entry = {"id": node_id, "skill": node["skill"], "arm": node["arm"],
                     "status": "completed", "result": _json_safe(result),
                     "simulated": _is_simulated(result)}
        except Exception as error:  # noqa: BLE001 - the failure is the report
            entry = {"id": node_id, "skill": node["skill"], "arm": node["arm"],
                     "status": "failed", "error": f"{type(error).__name__}: {error}"}
        end = time.monotonic() - started_at
        entry["started_after_seconds"] = round(begin, 3)
        entry["finished_after_seconds"] = round(end, 3)
        return entry, (node_id, begin, end)

    with ThreadPoolExecutor(max_workers=max(1, len(arms)),
                            thread_name_prefix="chem-dag") as pool:
        while remaining or in_flight:
            if failed_node is None:
                # Dispatch every node whose declared dependencies are complete.
                # Validation forces each same-arm pair into a dependency chain, so
                # this can only ever put one node per arm under command.
                for node_id in order:
                    if node_id not in remaining:
                        continue
                    node = node_by_id[node_id]
                    if not set(node["depends_on"]).issubset(reported):
                        continue
                    for other in in_flight.values():
                        if node_by_id[other]["arm"] == node["arm"]:
                            raise RuntimeError(
                                f"validation let {node_id!r} and {other!r} be ready on the same "
                                f"{node['arm']} arm at once; refusing to command one arm twice")
                    remaining.discard(node_id)
                    if node["arm"] not in used_arms:
                        used_arms.append(node["arm"])
                    in_flight[pool.submit(run_node, node_id)] = node_id
            if not in_flight:
                break
            finished, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
            for future in finished:
                in_flight.pop(future)
                entry, interval = future.result()
                reported[entry["id"]] = entry
                intervals.append(interval)
                if entry["status"] == "completed":
                    all_simulated = all_simulated and entry.get("simulated") is True
                elif failed_node is None:
                    # Stop the queue on every arm already under command. A sibling
                    # still in flight is stopped rather than cancelled, so it keeps
                    # whatever its jaws hold and reports its own outcome.
                    failed_node = entry["id"]
                    try:
                        report["stop"] = _json_safe(driver.stop(used_arms))
                    except Exception as error:  # noqa: BLE001
                        report["stop_error"] = f"{type(error).__name__}: {error}"

    if failed_node is not None:
        for node_id in order:
            if node_id in reported:
                continue
            node = node_by_id[node_id]
            reported[node_id] = {"id": node_id, "skill": node["skill"], "arm": node["arm"],
                                 "status": "blocked", "blocked_by": failed_node}
        report["status"] = "failed"
        report["error_node"] = failed_node
    else:
        report["status"] = "simulated" if all_simulated else "completed_command_trace"
    report["nodes"] = [reported[node_id] for node_id in order]
    report["used_arms"] = used_arms
    report["physical_success_verified"] = False
    report["execution"] = {
        "mode": "concurrent_dag",
        "arms": arms,
        "workers": max(1, len(arms)),
        "wall_seconds": round(time.monotonic() - started_at, 3),
        "simultaneous_pairs": _simultaneous_pairs(intervals),
    }
    _close_driver(driver, report)
    return report


class MockDriver:
    """Safe software-only driver used by CLI run unless --live is explicitly selected."""

    def __init__(self, seconds: float = 0.0) -> None:
        # An instant driver cannot show concurrency: two zero-length intervals never
        # intersect, so ``simultaneous_pairs`` would always come back empty. A small
        # dwell makes the simulated run report the overlap the scheduler really did.
        # The default stays 0 so no existing call site changes behaviour.
        self.seconds = max(0.0, float(seconds))

    def _dwell(self) -> None:
        seconds = getattr(self, "seconds", 0.0)
        if seconds:
            time.sleep(seconds)

    def preflight(self, graph: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        self._dwell()
        return {"simulated": True, "nodes_checked": len(graph["nodes"]), "hardware_access": False}

    def confirm(self) -> bool:
        return True

    def execute(self, node: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        # A dwell node is the only node whose duration the graph itself declares,
        # so the simulation honours it instead of the generic per-node dwell:
        # otherwise a 5 s or 8 s stagger would be invisible in the report's
        # timing and in ``simultaneous_pairs``.
        if node["skill"] == "control.hold":
            time.sleep(float(node["parameters"]["seconds"]))
        else:
            self._dwell()
        return {
            "simulated": True,
            "node_id": node["id"],
            "skill": node["skill"],
            "arm": node["arm"],
            "parameters": node["parameters"],
            "object_held_confirmed_emitted": False,
        }

    def stop(self, used_arms: list[str]) -> dict[str, Any]:
        return {"simulated": True, "used_arms": list(used_arms), "hardware_access": False}
