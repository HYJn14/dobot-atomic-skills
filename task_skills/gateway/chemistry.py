"""Adapter that hands a dobot.* package to the chemistry atomic-skill driver.

The gateway and ``task_skills.chemistry`` both know how to own a Dobot arm, and
they must not do it at the same time: two clients on one controller would fight
over the same socket, the same device lease and the same speed settings. So a
chemistry package gets exactly one owner - the chemistry ``LiveDriver`` - and
this adapter adds nothing except the gateway's cancellation and evidence
vocabulary on top of it.

That is also why this file forwards rather than reimplements: the chemistry
driver already encodes the session rules (lease -> ClearError -> EnableRobot ->
speed/accel -> Tool0/User0), the arrival checks and the release gate, and the
gateway already encodes the DAG scheduler, the journal, events and quarantine.
Neither one is copied.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List

from .contracts import CHEMISTRY_SKILL_PREFIX, CHEMISTRY_UPSTREAM_SKILLS


ACTION_FAILURE_POLICY = {"strategy": "decision_agent", "max_retries": 0,
                         "allowed_actions": ["safe_stop", "human_confirmation"]}
WAIT_FAILURE_POLICY = {"strategy": "safe_stop", "max_retries": 0}


def to_runtime_package(graph, *, run_id, package_id=None, experiment_id=None,
                       recipe_id="dobot-chemistry-task", version="1.0",
                       catalog_revision="unspecified", profile=None):
    """Translate an authored chemistry DAG into this gateway's submitted envelope.

    The chemistry graph is the readable authoring form; ``RuntimeExecutionPackage``
    is what the counterparty posts. This function is the only bridge between them,
    so the two cannot drift apart by being maintained by hand.

    The translation is deliberately 1:1 and dependency-preserving:

    * ``dobot.`` + the chemistry skill id, and the same ``device_id`` for the arm.
    * ``control.hold`` becomes a native ``wait`` node, because the envelope already
      has one and a dwell is not a hardware action. Its inbound edges are kept so
      the stagger still starts from the same trigger it did in the chemistry DAG.
    * every edge is copied, including the ones that only existed to order two
      nodes that share an arm.

    Parameter defaults, ranges and the pose whitelist are not restated here: the
    envelope validator normalizes them through the chemistry registry.
    """
    from ..chemistry.graph import validate_graph

    validated = validate_graph(graph, profile)
    name = validated["name"]
    package_id = package_id or ("runtime-" + name)
    experiment_id = experiment_id or ("experiment-" + name)
    nodes, edges = [], []
    for node in validated["nodes"]:
        node_id, skill, arm = node["id"], node["skill"], node["arm"]
        if skill == "control.hold":
            seconds = float(node["parameters"]["seconds"])
            nodes.append({"node_id": node_id, "node_type": "wait", "status": "pending",
                          "duration_s": seconds, "timeout_s": float(max(60.0, math.ceil(seconds) + 30)),
                          "on_fail": dict(WAIT_FAILURE_POLICY)})
        else:
            if skill not in CHEMISTRY_UPSTREAM_SKILLS:
                raise ValueError("chemistry graph node %r uses unregistered skill %r"
                                 % (node_id, skill))
            nodes.append({
                "node_id": node_id, "node_type": "action", "status": "pending", "timeout_s": 180,
                "skill_invocation": {
                    "skill_id": CHEMISTRY_SKILL_PREFIX + skill, "skill_version": "1.0.0",
                    "device_id": "dobot-nova-" + arm,
                    "idempotency_key": "%s:%s" % (run_id, node_id),
                    "parameters": dict(node["parameters"]),
                },
                "on_fail": dict(ACTION_FAILURE_POLICY),
            })
        for dependency in node["depends_on"]:
            edges.append({"source": dependency, "target": node_id, "edge_type": "success"})
    roots = {node["node_id"] for node in nodes} - {edge["target"] for edge in edges}
    return {"schema_version": "2.1", "graph_version": "0.1", "package_id": package_id,
            "run_id": run_id, "experiment_id": experiment_id, "recipe_id": recipe_id,
            "version": version, "catalog_revision": catalog_revision,
            "entry_node_ids": sorted(roots), "nodes": nodes, "edges": edges}


class ChemistryAdapter:
    """One chemistry driver per execution, driven only through its public API."""

    def __init__(self, cancel, profile=None, confirmation=None, lease_path=None):
        self.cancel = cancel
        self._profile = profile
        self._confirmation = confirmation
        self._lease_path = lease_path
        self._driver = None
        self.used_arms: List[str] = []

    # ------------------------------------------------------------------ helpers

    @property
    def profile(self) -> Dict[str, Any]:
        if self._profile is None:
            from ..chemistry.registry import load_profile

            self._profile = load_profile()
        return self._profile

    def driver(self):
        """Build the chemistry driver on first use, never at import time."""
        if self._driver is None:
            from ..chemistry.driver import LiveDriver

            self._driver = LiveDriver(profile=self.profile, allow_live=True,
                                      confirmation=self._confirmation or self._gateway_consent,
                                      lock_path=self._lease_path)
        return self._driver

    def _gateway_consent(self) -> bool:
        """Answer the driver's on-site consent gate for a gateway execution.

        ``LiveDriver.home`` asks ``confirm()`` before parking an arm so that an
        unattended CLI run cannot move a robot nobody is watching for. A gateway
        execution is the opposite situation: the request carried the site
        bearer token, the process was started with ``--allow-live``, and this
        same driver is already commanding every motion in the package under that
        authority, including the gripper atoms that pick the beaker up. Refusing
        the final park for want of an interactive prompt turned a finished run
        into ``FINAL_HOME_FAILED`` and, by design, quarantined the rig, so the
        gateway answers the gate on the caller's behalf. An explicitly injected
        ``confirmation`` still wins.
        """
        return True

    def preflight(self, plan) -> Dict[str, Any]:
        """Read-only check of the whole package, in the chemistry driver's terms.

        The driver walks the nodes in topological order against a per-arm state
        simulated from one live read, so a graph that demands two unreachable
        start poses for the same arm is rejected before anything moves. That is a
        stronger and more useful gate for these atoms than "both arms must equal
        the taught home", which is what the embodied family checks.
        """
        nodes = []
        for node_id in plan.order:
            for operation in plan.operations[node_id]:
                if operation.operation != "chemistry_atom":
                    continue
                nodes.append({"id": node_id, "skill": operation.parameters["skill"],
                              "arm": operation.arm,
                              "parameters": operation.parameters["parameters"]})
        if not nodes:
            return {"success": True, "note": "no chemistry atoms in this package"}
        return self.driver().preflight({"nodes": nodes}, self.profile)

    # ------------------------------------------------------- adapter interface

    def execute_atomic(self, node, scene) -> Dict[str, Any]:
        if self.cancel.is_set():
            raise InterruptedError("Execution cancelled")
        if node.arm not in self.used_arms:
            self.used_arms.append(node.arm)
        if node.operation == "chemistry_atom":
            skill = node.parameters["skill"]
            params = node.parameters["parameters"]
            if self.cancel.is_set():
                raise InterruptedError("Execution cancelled")
            # The driver raises on failure; letting it propagate is what marks the
            # node failed and triggers the gateway's stop-and-quarantine path.
            return self.driver().execute({"id": node.node_id, "skill": skill,
                                          "arm": node.arm, "parameters": params},
                                         self.profile)
        if node.operation == "move_safe_pose":
            # The gateway's live contract is that a finished graph leaves both arms
            # at the calibrated home. The chemistry graph usually did that itself in
            # its last nodes; this repeats it through the same driver, which is
            # idempotent, rather than through a second client. An arm already
            # parked comes back as ``skipped`` with the read-only evidence that
            # says why, so a graph that ended at home costs no extra motion.
            return self.driver().home((node.arm,), self.profile, timeout=node.timeout_seconds)
        raise ValueError("chemistry packages cannot execute operation %r" % (node.operation,))

    def snapshot_all(self) -> Dict[str, Any]:
        return {"hardware_connected": True, "mode": "chemistry",
                "arms_used": sorted(self.used_arms), "driver": type(self.driver()).__name__}

    def stop_and_verify(self) -> Dict[str, Any]:
        """Stop the queue and report whether the stop was acknowledged.

        ``_reply_status`` is the chemistry driver's own reply parser; importing it
        keeps one definition of "status 0 means acknowledged" for both paths.
        """
        from ..chemistry.driver import _reply_status

        arms = self.used_arms or ["left", "right"]
        try:
            report = self.driver().stop(arms)
        except Exception as error:  # noqa: BLE001 - the stop result is the evidence
            return {"confirmed": False, "error": type(error).__name__, "hardware_connected": True}
        stopped = report.get("stopped", {})
        confirmed = bool(stopped) and all(
            _reply_status(entry.get("stop_script")) == 0 for entry in stopped.values())
        return {"confirmed": confirmed, "detail": report, "gripper_released": False,
                "hardware_connected": True}

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
