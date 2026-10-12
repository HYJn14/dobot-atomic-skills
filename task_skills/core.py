from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from .models import Action, Pose, SceneConfig
from .graph import ExecutionGraph, SkillNode, build_execution_graph, is_parallel_pair, utc_now
from .journal import ExecutionJournal


class RobotAdapter(Protocol):
    def move_to(self, arm: str, pose: Pose) -> None: ...
    def open_gripper(self, arm: str) -> None: ...
    def close_gripper(self, arm: str) -> None: ...
    def move_to_safe_pose(self, arm: str) -> None: ...


class ActionError(RuntimeError):
    pass


@dataclass
class TaskState:
    object_locations: Dict[str, str]
    held_by: Dict[str, str] = field(default_factory=dict)


class BimanualTaskExecutor:
    ARM_OBJECTS = {"left": "A", "right": "B"}
    ARM_TARGETS = {"left": "target_A", "right": "target_B"}
    APPROACH_HEIGHT = 50.0
    LIFT_HEIGHT = 50.0

    def __init__(self, robot: RobotAdapter, scene: SceneConfig, journal: Optional[ExecutionJournal] = None):
        self.robot = robot
        self.scene = scene
        self.journal = journal
        self.state = TaskState({name: name for name in scene.objects})
        self._local = threading.local()

    def execute(self, actions: List[Action], source_instruction: Optional[str] = None) -> ExecutionGraph:
        graph = build_execution_graph(actions, source_instruction)
        self._graph = graph
        if self.journal:
            self.journal.write_graph(graph)
        index = 0
        while index < len(actions):
            if index + 1 < len(actions) and self._is_bimanual_pair(actions[index:index + 2]):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(self._execute_node, graph.nodes[index + offset], action)
                               for offset, action in enumerate(actions[index:index + 2])]
                    for future in futures:
                        try:
                            future.result()
                        except Exception:
                            graph.status = "failed"
                            graph.completed_at = utc_now()
                            if self.journal:
                                self.journal.write_graph(graph)
                                self.journal.write_summary(graph, self.state)
                            raise
                index += 2
                continue
            try:
                self._execute_node(graph.nodes[index], actions[index])
            except Exception:
                graph.status = "failed"
                graph.completed_at = utc_now()
                if self.journal:
                    self.journal.write_graph(graph)
                    self.journal.write_summary(graph, self.state)
                raise
            index += 1
        graph.status = "succeeded"
        graph.completed_at = utc_now()
        if self.journal:
            self.journal.write_graph(graph)
            self.journal.write_summary(graph, self.state)
        return graph

    @staticmethod
    def _is_bimanual_pair(actions: List[Action]) -> bool:
        return is_parallel_pair(actions)

    def execute_one(self, action: Action) -> None:
        if action.name == "pick":
            self.pick(action.arm, action.object_id)
        elif action.name == "place":
            self.place(action.arm, action.object_id, action.target)
        elif action.name == "safe_pose":
            self.robot.move_to_safe_pose(action.arm)
        elif action.name == "open_gripper":
            self.robot.open_gripper(action.arm)
        elif action.name == "close_gripper":
            self.robot.close_gripper(action.arm)
        elif action.name == "wait":
            seconds = float(action.parameters.get("seconds", 0))
            if seconds < 0:
                raise ActionError("Wait duration cannot be negative")
            time.sleep(seconds)
        else:
            raise ActionError(f"Unsupported action: {action.name}")

    def _execute_node(self, node: SkillNode, action: Action) -> None:
        self._local.active_node = node
        node.status = "running"
        self._record("skill_started", node=node, state_before=self._state_dict())
        try:
            self.execute_one(action)
        except Exception as error:
            node.status = "failed"
            self._record(
                "skill_failed",
                node=node,
                status="failed",
                state_after=self._state_dict(),
                evidence={"success": False, "verification_level": "command_trace"},
                error={"type": type(error).__name__, "message": str(error)},
            )
            if self.journal:
                self.journal.write_graph(self._graph)
            raise
        node.status = "succeeded"
        self._record(
            "skill_completed",
            node=node,
            status="succeeded",
            state_after=self._state_dict(),
            evidence={
                "success": True,
                "verification_level": "command_trace",
                "physical_success_verified": False,
            },
        )
        if self.journal:
            self.journal.write_graph(self._graph)
        self._local.active_node = None

    def _record(self, event: str, **payload) -> None:
        if not self.journal:
            return
        serializable = {}
        for key, value in payload.items():
            serializable[key] = value.to_dict() if isinstance(value, ExecutionGraph) else value
            if isinstance(value, SkillNode):
                serializable[key] = value.__dict__.copy()
        self.journal.record(event, graph_node=serializable.pop("node", None), **serializable)

    def _record_phase(self, phase: str) -> None:
        node = getattr(self._local, "active_node", None)
        if node:
            self._record("skill_state", node=node, phase=phase, state=self._state_dict())

    def _state_dict(self) -> Dict[str, Dict[str, str]]:
        return {"object_locations": dict(self.state.object_locations), "held_by": dict(self.state.held_by)}

    def pick(self, arm: str, object_id: str) -> None:
        if arm is None:
            arm = "left" if object_id == "A" else "right" if object_id == "B" else None
        self._require_arm(arm)
        self._require_object(object_id)
        if self.ARM_OBJECTS[arm] != object_id:
            raise ActionError(f"Arm {arm} may only pick object {self.ARM_OBJECTS[arm]}")
        if object_id in self.state.held_by:
            raise ActionError(f"Object {object_id} is already held")
        location = self.state.object_locations[object_id]
        pose = self._location_pose(location)
        self._record_phase("ready")
        self.robot.open_gripper(arm)
        self._record_phase("approach")
        self.robot.move_to(arm, self._above(pose, self.APPROACH_HEIGHT))
        self._record_phase("contact")
        self.robot.move_to(arm, pose)
        self._record_phase("grasp")
        self.robot.close_gripper(arm)
        self.state.held_by[object_id] = arm
        self._record_phase("lift")
        self.robot.move_to(arm, self._above(pose, self.LIFT_HEIGHT))
        self._record_phase("completed")

    def place(self, arm: str, object_id: str, target: str) -> None:
        if arm is None:
            arm = self.state.held_by.get(object_id)
        self._require_arm(arm)
        self._require_object(object_id)
        if self.ARM_OBJECTS[arm] != object_id:
            raise ActionError(f"Arm {arm} may only place object {self.ARM_OBJECTS[arm]}")
        if target == self.ARM_TARGETS[arm]:
            pass
        elif target.startswith("initial_") and target == f"initial_{object_id}":
            pass
        else:
            raise ActionError(f"Arm {arm} may only place at {self.ARM_TARGETS[arm]}")
        if self.state.held_by.get(object_id) != arm:
            raise ActionError(f"Arm {arm} does not hold object {object_id}")
        pose = self._resolve_target(target)
        occupied = [obj for obj, location in self.state.object_locations.items() if location == target]
        if occupied:
            raise ActionError(f"Target {target} is occupied by {occupied[0]}")
        self._record_phase("ready")
        self.robot.move_to(arm, self._above(pose, self.APPROACH_HEIGHT))
        self._record_phase("contact")
        self.robot.move_to(arm, pose)
        self._record_phase("release")
        self.robot.open_gripper(arm)
        self.state.object_locations[object_id] = target
        del self.state.held_by[object_id]
        self._record_phase("lift")
        self.robot.move_to(arm, self._above(pose, self.LIFT_HEIGHT))
        self._record_phase("completed")

    def _location_pose(self, location: str) -> Pose:
        if location in self.scene.objects:
            return self.scene.objects[location]
        if location in self.scene.targets:
            return self.scene.targets[location]
        raise ActionError(f"Unknown object location: {location}")

    @staticmethod
    def _above(pose: Pose, height: float) -> Pose:
        return Pose(pose.x, pose.y, pose.z + height, pose.rx, pose.ry, pose.rz)

    def _resolve_target(self, target: str) -> Pose:
        if target in self.scene.targets:
            return self.scene.targets[target]
        if target.startswith("object:"):
            object_id = target.split(":", 1)[1]
            return self._location_pose(self.state.object_locations[object_id])
        raise ActionError(f"Unknown target: {target}")

    def _require_arm(self, arm: str) -> None:
        if arm not in {"left", "right"}:
            raise ActionError(f"Unknown arm: {arm}")

    def _require_object(self, object_id: str) -> None:
        if object_id not in self.scene.objects:
            raise ActionError(f"Unknown object: {object_id}")


class InMemoryRobot:
    def __init__(self):
        self.commands: List[tuple] = []

    def move_to(self, arm: str, pose: Pose) -> None:
        self.commands.append(("move_to", arm, pose))

    def open_gripper(self, arm: str) -> None:
        self.commands.append(("open_gripper", arm))

    def close_gripper(self, arm: str) -> None:
        self.commands.append(("close_gripper", arm))

    def move_to_safe_pose(self, arm: str) -> None:
        self.commands.append(("safe_pose", arm))
