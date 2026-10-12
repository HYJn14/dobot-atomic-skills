from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Dict

from .atomic_graph import AtomicExecutionGraph, AtomicNode
from .graph import utc_now


class AtomicGraphExecutor:
    def __init__(self, adapter: Any, scene: Any, journal: Any):
        self.adapter = adapter
        self.scene = scene
        self.journal = journal

    def execute(self, graph: AtomicExecutionGraph) -> AtomicExecutionGraph:
        graph.status = "running"
        self.journal.write_graph(graph)
        active_arms = set()
        futures: Dict[Any, AtomicNode] = {}
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dobot-atomic")
        try:
            while True:
                self._schedule_ready_nodes(graph, executor, futures, active_arms)
                if not futures:
                    pending = [node for node in graph.nodes if node.status == "pending"]
                    if pending:
                        self._block_unfinished(graph)
                        raise RuntimeError("Atomic DAG has pending nodes with unsatisfied dependencies")
                    break

                completed, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in completed:
                    node = futures.pop(future)
                    active_arms.discard(node.arm)
                    try:
                        node.evidence = future.result()
                    except Exception as error:
                        node.status = "failed"
                        self._block_unfinished(graph)
                        self.journal.record(
                            "atomic_failed",
                            node=node,
                            error={"type": type(error).__name__, "message": str(error)},
                        )
                        self._stop_all(graph)
                        graph.status = "failed"
                        graph.completed_at = utc_now()
                        self.journal.write_graph(graph)
                        self.journal.write_atomic_summary(graph, self._safe_snapshot_all())
                        for remaining in futures:
                            remaining.cancel()
                        raise
                    node.status = "succeeded"
                    self.journal.record("atomic_completed", node=node, evidence=node.evidence)
                    self.journal.write_graph(graph)
        except Exception:
            if graph.status != "failed":
                self._block_unfinished(graph)
                self._stop_all(graph)
                graph.status = "failed"
                graph.completed_at = utc_now()
                self.journal.write_graph(graph)
                self.journal.write_atomic_summary(graph, self._safe_snapshot_all())
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        graph.status = "succeeded"
        graph.completed_at = utc_now()
        self.journal.write_graph(graph)
        self.journal.write_atomic_summary(graph, self.adapter.snapshot_all())
        return graph

    def _schedule_ready_nodes(self, graph, executor, futures, active_arms) -> None:
        available_slots = 2 - len(futures)
        if available_slots <= 0:
            return
        for node in graph.nodes:
            if available_slots <= 0:
                break
            if node.status != "pending" or not self._dependencies_succeeded(graph, node):
                continue
            if node.arm in active_arms:
                continue
            node.status = "running"
            active_arms.add(node.arm)
            self.journal.record("atomic_started", node=node)
            self.journal.write_graph(graph)
            futures[executor.submit(self.adapter.execute_atomic, node, self.scene)] = node
            available_slots -= 1

    @staticmethod
    def _dependencies_succeeded(graph: AtomicExecutionGraph, node: AtomicNode) -> bool:
        statuses = {candidate.node_id: candidate.status for candidate in graph.nodes}
        return all(statuses.get(dependency) == "succeeded" for dependency in node.dependencies)

    @staticmethod
    def _block_unfinished(graph: AtomicExecutionGraph) -> None:
        for node in graph.nodes:
            if node.status in {"pending", "running"}:
                node.status = "blocked"

    def _stop_all(self, graph: AtomicExecutionGraph) -> None:
        if hasattr(self.adapter, "stop_all"):
            try:
                self.adapter.stop_all()
            except Exception as stop_error:
                self.journal.record(
                    "atomic_stop_failed",
                    error={"type": type(stop_error).__name__, "message": str(stop_error)},
                )
            return
        for arm in sorted({node.arm for node in graph.nodes if node.arm in {"left", "right"}}):
            try:
                self.adapter.stop(arm)
            except Exception as stop_error:
                self.journal.record(
                    "atomic_stop_failed",
                    arm=arm,
                    error={"type": type(stop_error).__name__, "message": str(stop_error)},
                )
            if hasattr(self.adapter, "disable"):
                try:
                    self.adapter.disable(arm)
                except Exception as disable_error:
                    self.journal.record(
                        "atomic_disable_failed",
                        arm=arm,
                        error={"type": type(disable_error).__name__, "message": str(disable_error)},
                    )

    def _safe_snapshot_all(self):
        try:
            return self.adapter.snapshot_all()
        except Exception as error:
            return {"snapshot_error": f"{type(error).__name__}: {error}"}

    def _safe_snapshot(self, arm: str):
        try:
            return self.adapter.snapshot(arm)
        except Exception as error:
            return {"arm": arm, "snapshot_error": f"{type(error).__name__}: {error}"}
