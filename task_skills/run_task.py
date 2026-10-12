import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, TextIO

from .atomic_executor import AtomicGraphExecutor
from .atomic_graph import AtomicExecutionGraph, compile_atomic_instruction
from .dobot_adapter import DobotHardwareAdapter
from .journal import ExecutionJournal


DEFAULT_SCENE_PATH = Path(__file__).with_name("scene_config.json")
DEFAULT_ARTIFACT_ROOT = Path(__file__).with_name("artifacts")


@dataclass
class RunResult:
    exit_code: int
    graph: AtomicExecutionGraph
    artifact_directory: Path


def run_instruction(
    instruction: str,
    scene: Any,
    artifact_root: Path,
    input_fn: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
    adapter_factory: Optional[Callable[[list], Any]] = None,
    executor_factory: Callable[[Any, Any, ExecutionJournal], Any] = AtomicGraphExecutor,
    dry_run: bool = False,
) -> RunResult:
    graph = compile_atomic_instruction(instruction)
    run_directory = _create_run_directory(Path(artifact_root), graph.graph_id)
    journal = ExecutionJournal(run_directory)
    journal.write_graph(graph)
    journal.record("instruction_compiled", graph_id=graph.graph_id, source_instruction=instruction)
    print_graph_preview(graph, scene, output)
    journal.record("graph_previewed", graph_id=graph.graph_id, node_ids=[node.node_id for node in graph.nodes])

    if dry_run:
        graph.status = "compiled"
        journal.write_graph(graph)
        journal.record("dry_run_completed", graph_id=graph.graph_id)
        journal.write_atomic_summary(graph, {"hardware_connected": False})
        return RunResult(0, graph, run_directory)

    confirmation = input_fn("输入大写 YES 执行真机任务: ")
    accepted = confirmation == "YES"
    journal.record("operator_confirmation", accepted=accepted, response=confirmation)
    if not accepted:
        graph.status = "cancelled"
        journal.write_graph(graph)
        journal.write_atomic_summary(graph, {"cancelled_before_hardware": True})
        return RunResult(2, graph, run_directory)

    # The execution graph contains synthetic ``system`` barrier nodes, but
    # hardware adapters may only be constructed for physical arms.
    arms = sorted({node.arm for node in graph.nodes if node.arm in {"left", "right"}})
    if adapter_factory is None:
        adapter_factory = lambda selected: DobotHardwareAdapter.from_live(selected)
    from .device_lease import DeviceLease

    lease = DeviceLease(purpose="legacy-control").acquire()
    adapter = None
    safe_release = False
    try:
        adapter = adapter_factory(arms)
        executor = executor_factory(adapter, scene, journal)
        executor.execute(graph)
        safe_release = True
        return RunResult(0, graph, run_directory)
    except BaseException:
        if isinstance(adapter, DobotHardwareAdapter):
            try:
                safe_release = adapter.stop_and_verify()["confirmed"]
            except Exception:
                safe_release = False
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


def print_graph_preview(graph: AtomicExecutionGraph, scene: Any, output: TextIO) -> None:
    from .models import resolve_pose_ref

    print("=== Dobot 原子技能执行图 ===", file=output)
    print(f"graph_id: {graph.graph_id}", file=output)
    print(f"instruction: {graph.source_instruction}", file=output)
    labels = {node.label for node in graph.nodes}
    if {"left_pick_A:transport", "right_pick_B:transport"}.issubset(labels):
        print("parallel_start: left_pick_A + right_pick_B", file=output)
    for node in graph.nodes:
        dependencies = ",".join(node.dependencies) if node.dependencies else "-"
        resolved_pose = ""
        if node.operation == "move_pose":
            pose = resolve_pose_ref(scene, node.parameters["pose_ref"], arm=node.arm)
            resolved_pose = (
                f" pose=({pose.x:.4f},{pose.y:.4f},{pose.z:.4f},"
                f"{pose.rx:.4f},{pose.ry:.4f},{pose.rz:.4f})"
            )
        wait_description = ""
        if node.operation == "wait":
            wait_description = f" seconds={float(node.parameters['seconds']):.1f}"
        print(
            f"[{node.sequence:02d}] {node.node_id} label={node.label or '-'} arm={node.arm} "
            f"operation={node.operation} depends={dependencies} parameters={node.parameters}{resolved_pose}",
            file=output,
        )
        if wait_description:
            print(f"     hold:{wait_description.strip()}", file=output)
    configure_nodes = [node for node in graph.nodes if node.operation == "configure_robot"]
    speed_percent = configure_nodes[0].parameters["speed_percent"] if configure_nodes else "unconfigured"
    print(f"speed_limit: {speed_percent}%", file=output)
    print("verification: controller feedback is not physical object success", file=output)


def _create_run_directory(root: Path, graph_id: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_directory = root / f"run_{timestamp}_{graph_id[-8:]}"
    run_directory.mkdir(parents=True, exist_ok=False)
    return run_directory


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compile and execute a Dobot natural-language atomic task")
    parser.add_argument("instruction", help="Natural-language task instruction")
    parser.add_argument("--scene", type=Path, default=DEFAULT_SCENE_PATH)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--ready-timeout", type=float, default=900.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None) -> int:
    from .models import load_scene_config

    arguments = _build_argument_parser().parse_args(argv)
    scene = load_scene_config(arguments.scene)

    def adapter_factory(arms):
        return DobotHardwareAdapter.from_live(arms, settle_timeout=arguments.ready_timeout)

    result = run_instruction(
        arguments.instruction,
        scene,
        artifact_root=arguments.artifacts,
        adapter_factory=adapter_factory,
        dry_run=arguments.dry_run,
    )
    print(f"artifacts: {result.artifact_directory}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
