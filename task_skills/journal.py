import json
import threading
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, List

from .graph import ExecutionGraph, utc_now


class ExecutionJournal:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_path = self.root / "execution_events.jsonl"
        self._lock = threading.RLock()

    def write_graph(self, graph: ExecutionGraph) -> None:
        self._write_json(self.root / "execution_graph.json", graph.to_dict())

    def record(self, event: str, **payload: Any) -> None:
        entry = {"timestamp": utc_now(), "event": event, **self._serializable(payload)}
        with self._lock:
            with self.events_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def write_summary(self, graph: ExecutionGraph, state: Any) -> None:
        value = {
            "schema_version": "1.0",
            "graph_id": graph.graph_id,
            "status": graph.status,
            "completed_at": graph.completed_at,
            "state": asdict(state),
            "verification": {
                "level": "command_trace",
                "physical_success_verified": False,
                "note": "Hardware-independent adapter evidence; physical success requires sensor or operator verification.",
            },
        }
        self._write_json(self.root / "execution_summary.json", value)

    def write_atomic_summary(self, graph: Any, final_state: Any) -> None:
        value = {
            "schema_version": "2.0",
            "graph_id": graph.graph_id,
            "source_instruction": graph.source_instruction,
            "status": graph.status,
            "completed_at": graph.completed_at,
            "final_state": self._serializable(final_state),
            "verification": {
                "level": "command_trace" if graph.status == "succeeded" else "compiled",
                "physical_success_verified": False,
                "note": "Controller evidence does not verify physical grasp or placement.",
            },
        }
        self._write_json(self.root / "execution_summary.json", value)

    def read_events(self) -> List[Dict[str, Any]]:
        if not self.events_path.exists():
            return []
        with self.events_path.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]

    def _write_json(self, path: Path, value: Dict[str, Any]) -> None:
        with self._lock:
            path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    @classmethod
    def _serializable(cls, value: Any) -> Any:
        if is_dataclass(value):
            return cls._serializable(asdict(value))
        if isinstance(value, dict):
            return {key: cls._serializable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._serializable(item) for item in value]
        return value
