"""Offline-first R09 right-arm approach to the taught beaker pose."""

import argparse
import json
import math
import re
import signal
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, TextIO
from uuid import uuid4

from .atomic_executor import AtomicGraphExecutor
from .atomic_graph import AtomicExecutionGraph, AtomicNode
from .dobot_adapter import DobotHardwareAdapter
from .graph import utc_now
from .journal import ExecutionJournal
from .models import Pose, SceneConfig


DEFAULT_SCENE_PATH = Path(__file__).with_name("demos") / "beaker_r09_scene.json"
DEFAULT_ARTIFACT_ROOT = Path(__file__).with_name("artifacts") / "beaker-r09"
R09_GRAPH_ID = "beaker-r09-right-approach"
R09_SOURCE = "Approach the R09 beaker pose with the right arm using one calibrated JointMovJ."
R09_ARM = "right"
R09_USER = 0
R09_TOOL = 0
R09_SPEED_PERCENT = 10
R09_MOTION = "calibrated_joint"


@dataclass(frozen=True)
class R09Scene:
    arm: str
    user_number: int
    tool_number: int
    speed_percent: int
    motion: str
    pose: Pose

    def execution_scene(self) -> SceneConfig:
        return SceneConfig(objects={"B": self.pose}, targets={})


@dataclass
class RunResult:
    exit_code: int
    graph: AtomicExecutionGraph
    artifact_directory: Path


def load_r09_scene(path: Path = DEFAULT_SCENE_PATH) -> R09Scene:
    def reject_constant(value: str):
        raise ValueError(f"Non-finite JSON number is not allowed: {value}")

    value = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject_constant)
    expected_keys = {"schema_version", "arm", "user_number", "tool_number", "speed_percent", "motion", "pose"}
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError(f"R09 scene keys must be exactly {sorted(expected_keys)}")
    for key, expected in (
        ("schema_version", "beaker-r09-scene/1"),
        ("arm", R09_ARM),
        ("user_number", R09_USER),
        ("tool_number", R09_TOOL),
        ("speed_percent", R09_SPEED_PERCENT),
        ("motion", R09_MOTION),
    ):
        actual = value[key]
        if type(actual) is not type(expected) or actual != expected:
            raise ValueError(f"R09 scene {key} must be {expected!r}")

    pose_value = value["pose"]
    pose_keys = {"x", "y", "z", "rx", "ry", "rz", "joints"}
    if not isinstance(pose_value, dict) or set(pose_value) != pose_keys:
        raise ValueError(f"R09 pose keys must be exactly {sorted(pose_keys)}")
    cartesian = tuple(_finite_number(pose_value[key], f"pose.{key}") for key in ("x", "y", "z", "rx", "ry", "rz"))
    joint_value = pose_value["joints"]
    if not isinstance(joint_value, list) or len(joint_value) != 6:
        raise ValueError("R09 pose.joints must contain exactly six finite numbers")
    joints = tuple(_finite_number(number, f"pose.joints[{index}]") for index, number in enumerate(joint_value))
    pose = Pose(*cartesian, joints=joints)
    return R09Scene(R09_ARM, R09_USER, R09_TOOL, R09_SPEED_PERCENT, R09_MOTION, pose)


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"R09 {field_name} must be a finite number")
    return float(value)


def _require_command_ack(command: str, reply: Any) -> None:
    if not re.match(r"^\s*0\s*,", str(reply)):
        raise RuntimeError(f"{command} did not return an acknowledged controller reply: {reply!r}")


def build_r09_graph(scene: R09Scene) -> AtomicExecutionGraph:
    pose_data = _pose_dict(scene.pose)
    specifications = [
        ("wait_controller_ready", "right:r09:wait-ready", {}, 300.0),
        ("clear_error", "right:r09:clear-error", {}, 180.0),
        ("enable_robot", "right:r09:enable", {}, 180.0),
        (
            "configure_robot",
            "right:r09:configure-user-tool-speed",
            {"speed_percent": scene.speed_percent, "tool_number": scene.tool_number, "user_number": scene.user_number},
            180.0,
        ),
        (
            "move_pose",
            "right:r09:beaker-joint-approach",
            {"pose_ref": "object:B", "motion": scene.motion, "calibrated_pose": pose_data},
            180.0,
        ),
    ]
    nodes = []
    previous_id = None
    for sequence, (operation, label, parameters, timeout) in enumerate(specifications):
        node_id = f"r09-{sequence + 1:03d}"
        if operation == "configure_robot":
            expected_outputs = ["speed_and_acceleration_limits_configured", "tool_0_selected", "user_0_selected"]
            success_criteria = [
                "speed_factor_10_acknowledged", "acc_j_10_acknowledged", "speed_j_10_acknowledged",
                "acc_l_10_acknowledged", "speed_l_10_acknowledged", "tool_0_acknowledged", "user_0_acknowledged",
            ]
        elif operation == "move_pose":
            expected_outputs = ["target_joint_feedback_reached", "controller_idle"]
            success_criteria = [
                "command_accepted", "target_joint_feedback_within_tolerance",
                "three_consecutive_robot_mode_5_samples", "no_controller_alarm",
            ]
        else:
            expected_outputs = {
                "wait_controller_ready": ["controller_reachable"],
                "clear_error": ["command_completed"],
                "enable_robot": ["robot_enabled_and_idle"],
            }[operation]
            success_criteria = ["command_accepted"]
        preconditions = {
            "wait_controller_ready": ["right_controller_reachable"],
            "clear_error": ["right_controller_reachable"],
            "enable_robot": ["right_errors_cleared", "safety_circuit_ready"],
            "configure_robot": ["right_robot_enabled"],
            "move_pose": ["right_robot_enabled", "speed_10_configured", "tool_0_selected", "user_0_selected", "workspace_clear"],
        }[operation]
        nodes.append(AtomicNode(
            node_id=node_id,
            sequence=sequence,
            operation=operation,
            arm=R09_ARM,
            label=label,
            parameters=parameters,
            dependencies=[previous_id] if previous_id else [],
            preconditions=preconditions,
            expected_outputs=expected_outputs,
            success_criteria=success_criteria,
            timeout_seconds=timeout,
        ))
        previous_id = node_id

    return AtomicExecutionGraph(
        graph_id=R09_GRAPH_ID,
        source_instruction=R09_SOURCE,
        created_at=utc_now(),
        nodes=nodes,
        edges=[(nodes[index].node_id, nodes[index + 1].node_id) for index in range(len(nodes) - 1)],
        status="compiled",
    )


def _pose_dict(pose: Pose) -> dict:
    return {
        "x": pose.x, "y": pose.y, "z": pose.z,
        "rx": pose.rx, "ry": pose.ry, "rz": pose.rz,
        "joints": list(pose.joints or ()),
    }


class R09DobotHardwareAdapter(DobotHardwareAdapter):
    """Limit this run to the right dashboard/motion API and no gripper device."""

    def configure_robot(self, arm: str, speed_percent: int = R09_SPEED_PERCENT):
        if arm != R09_ARM or speed_percent != R09_SPEED_PERCENT:
            raise ValueError("R09 only supports right-arm speed configuration at 10 percent")
        evidence = super().configure_robot(arm, speed_percent)
        for command in ("SpeedFactor", "AccJ", "SpeedJ", "AccL", "SpeedL", "Tool"):
            _require_command_ack(f"right {command}", evidence["replies"][command])
        dashboard = self._dashboard(arm)
        reply = self._send_command(arm, lambda: dashboard.User(R09_USER))
        self._require_success("right User(0)", reply)
        _require_command_ack("right User(0)", reply)
        evidence["replies"]["User"] = str(reply)
        evidence.update({"tool_number": R09_TOOL, "user_number": R09_USER, "user_reply": str(reply)})
        return evidence

    def execute_atomic(self, node, scene):
        if node.arm != R09_ARM:
            raise ValueError("R09 graph may only execute on the physical right arm")
        if node.operation == "configure_robot":
            if node.parameters.get("tool_number") != R09_TOOL or node.parameters.get("user_number") != R09_USER:
                raise ValueError("R09 graph must configure Tool(0) and User(0)")
        if node.operation == "move_pose":
            if node.parameters.get("motion") != R09_MOTION or node.parameters.get("pose_ref") != "object:B":
                raise ValueError("R09 graph only supports the calibrated object:B approach")
            if node.parameters.get("calibrated_pose") != _pose_dict(scene.objects["B"]):
                raise ValueError("R09 graph pose does not match the validated R09 scene")
        return super().execute_atomic(node, scene)


def create_live_adapter(settle_timeout: float = 180.0) -> R09DobotHardwareAdapter:
    """Open only the right dashboard and motion channels; do not construct a gripper."""
    from dobot_control.robots.dobot_api import DobotApiDashboard, DobotApiMove
    from .layout import physical_nova_ip

    ip = physical_nova_ip(R09_ARM)
    dashboard = None
    motion = None
    try:
        dashboard = DobotApiDashboard(ip, 29999)
        motion = DobotApiMove(ip, 30003)
        return R09DobotHardwareAdapter.from_components(
            dashboards={R09_ARM: dashboard}, motions={R09_ARM: motion}, grippers={}, settle_timeout=settle_timeout,
        )
    except BaseException:
        for resource in (motion, dashboard):
            if resource is not None:
                try:
                    resource.close()
                except BaseException:
                    pass
        raise


def run_beaker_approach(
    scene: R09Scene,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    live: bool = False,
    input_fn: Optional[Callable[[str], str]] = None,
    output: Optional[TextIO] = None,
    adapter_factory: Optional[Callable[[tuple], Any]] = None,
    lease_factory: Optional[Callable[[], Any]] = None,
    executor_factory: Callable[[Any, Any, ExecutionJournal], Any] = AtomicGraphExecutor,
    graph_builder: Callable[[Any], AtomicExecutionGraph] = build_r09_graph,
    preview_fn: Optional[Callable[[AtomicExecutionGraph, Any, TextIO], None]] = None,
    confirmation_prompt: str = "Enter exact uppercase YES to execute the right-arm R09 move: ",
    event_prefix: str = "r09",
) -> RunResult:
    output = sys.stdout if output is None else output
    input_fn = input if input_fn is None else input_fn
    preview_fn = print_r09_preview if preview_fn is None else preview_fn
    graph = graph_builder(scene)
    run_directory = _create_run_directory(Path(artifact_root), graph.graph_id)
    journal = ExecutionJournal(run_directory)
    execution_scene = scene.execution_scene()
    journal.write_graph(graph)
    journal.record(f"{event_prefix}_graph_compiled", graph_id=graph.graph_id, source_instruction=graph.source_instruction)
    preview_fn(graph, scene, output)
    journal.record("graph_previewed", graph_id=graph.graph_id, node_ids=[node.node_id for node in graph.nodes])
    print(f"graph_artifact: {run_directory / 'execution_graph.json'}", file=output)

    if not live:
        graph.status = "compiled"
        journal.write_graph(graph)
        journal.record("dry_run_completed", graph_id=graph.graph_id)
        journal.write_atomic_summary(graph, {"hardware_connected": False})
        return RunResult(0, graph, run_directory)

    confirmation = input_fn(confirmation_prompt)
    accepted = confirmation == "YES"
    journal.record("operator_confirmation", accepted=accepted, response=confirmation)
    if not accepted:
        graph.status = "cancelled"
        graph.completed_at = utc_now()
        journal.write_graph(graph)
        journal.write_atomic_summary(graph, {"cancelled_before_hardware": True})
        return RunResult(2, graph, run_directory)

    if lease_factory is None:
        from .device_lease import DeviceLease

        lease_factory = lambda: DeviceLease(purpose="legacy-control")
    lease = lease_factory()
    lease.acquire()
    adapter = None
    safe_release = False
    try:
        adapter_factory = (lambda _arms: create_live_adapter()) if adapter_factory is None else adapter_factory
        adapter = adapter_factory((R09_ARM,))
        executor = executor_factory(adapter, execution_scene, journal)
        _execute_with_interrupt_handling(executor, graph, adapter)
        safe_release = True
        return RunResult(0, graph, run_directory)
    except BaseException as error:
        stop_result = None
        if adapter is not None and hasattr(adapter, "stop_and_verify"):
            try:
                stop_result = adapter.stop_and_verify()
                safe_release = bool(stop_result.get("confirmed"))
            except BaseException as stop_error:
                stop_result = {"confirmed": False, "error": f"{type(stop_error).__name__}: {stop_error}"}
                safe_release = False
        if graph.status not in {"failed", "succeeded"}:
            interrupted = isinstance(error, (KeyboardInterrupt, InterruptedError))
            graph.status = "interrupted" if interrupted else "failed"
            for node in graph.nodes:
                if node.status == "running":
                    node.status = "interrupted" if interrupted else "failed"
                elif node.status == "pending":
                    node.status = "blocked"
            graph.completed_at = utc_now()
            journal.record(
                f"{event_prefix}_execution_interrupted" if interrupted else f"{event_prefix}_execution_failed",
                error={"type": type(error).__name__, "message": str(error)},
                stop=stop_result,
            )
            journal.write_graph(graph)
            journal.write_atomic_summary(graph, _safe_snapshot(adapter))
        raise
    finally:
        try:
            if adapter is not None:
                adapter.close()
        except BaseException:
            safe_release = False
            raise
        finally:
            if safe_release:
                lease.release()


def print_r09_preview(graph: AtomicExecutionGraph, scene: R09Scene, output: TextIO) -> None:
    print("=== R09 beaker right-arm approach ===", file=output)
    print(f"graph_id: {graph.graph_id}", file=output)
    print(f"instruction: {graph.source_instruction}", file=output)
    print("arm: right", file=output)
    print("user: 0  tool: 0", file=output)
    print("SpeedFactor/SpeedJ/SpeedL/AccJ/AccL: 10", file=output)
    print(
        "target_pose: " + json.dumps(_pose_dict(scene.pose), ensure_ascii=False, sort_keys=True),
        file=output,
    )
    for node in graph.nodes:
        dependencies = ",".join(node.dependencies) if node.dependencies else "-"
        print(
            f"[{node.sequence:02d}] {node.node_id} label={node.label} arm={node.arm} "
            f"operation={node.operation} depends={dependencies} parameters={node.parameters}",
            file=output,
        )
    print("motion_note: calibrated JointMovJ interpolation from the current position; no path guarantee", file=output)
    print("end_state: right robot remains enabled at the R09 target", file=output)


def _safe_snapshot(adapter: Any) -> dict:
    if adapter is None:
        return {"hardware_connected": False}
    try:
        return adapter.snapshot_all()
    except Exception as error:
        return {"snapshot_error": f"{type(error).__name__}: {error}"}


def _execute_with_interrupt_handling(executor: Any, graph: AtomicExecutionGraph, adapter: Any) -> None:
    """Make a running node observe Ctrl+C before the executor joins its worker."""
    cancel_event = threading.Event()
    if hasattr(adapter, "set_cancel_event"):
        adapter.set_cancel_event(cancel_event)
    if threading.current_thread() is not threading.main_thread():
        executor.execute(graph)
        return

    previous_handler = signal.getsignal(signal.SIGINT)

    def interrupt(signum, frame):
        cancel_event.set()
        if hasattr(adapter, "stop"):
            try:
                # This adapter path sends dashboard StopScript directly and
                # does not wait on the per-arm motion command gate.
                adapter.stop(R09_ARM)
            except BaseException:
                pass
        elif hasattr(adapter, "stop_all"):
            try:
                adapter.stop_all()
            except BaseException:
                pass
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupt)
    try:
        executor.execute(graph)
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def _create_run_directory(root: Path, graph_id: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    directory = root / f"run_{timestamp}_{graph_id[-8:]}_{uuid4().hex[:6]}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview or execute the fixed R09 right-arm beaker approach")
    parser.add_argument("--scene", type=Path, default=DEFAULT_SCENE_PATH)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="live", action="store_false", help="Preview and export without hardware (default)")
    mode.add_argument("--live", dest="live", action="store_true", help="Prompt for exact YES, then execute on the right arm")
    parser.set_defaults(live=False)
    return parser


def main(argv=None) -> int:
    arguments = _build_argument_parser().parse_args(argv)
    scene = load_r09_scene(arguments.scene)
    result = run_beaker_approach(scene, artifact_root=arguments.artifacts, live=arguments.live)
    print(f"artifacts: {result.artifact_directory}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
