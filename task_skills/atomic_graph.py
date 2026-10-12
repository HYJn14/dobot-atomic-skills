from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from .graph import utc_now
from .models import Action
from .parser import parse_instruction


ARM_BY_OBJECT = {"A": "left", "B": "right"}
TARGET_BY_ARM = {"left": "target_A", "right": "target_B"}
DEFAULT_SPEED_PERCENT = 10


@dataclass
class AtomicNode:
    node_id: str
    sequence: int
    operation: str
    arm: str
    label: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    preconditions: List[str] = field(default_factory=list)
    expected_outputs: List[str] = field(default_factory=list)
    success_criteria: List[str] = field(default_factory=list)
    timeout_seconds: float = 900.0
    failure_policy: str = "stop_and_preserve_evidence"
    status: str = "pending"
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AtomicExecutionGraph:
    graph_id: str
    source_instruction: str
    created_at: str
    nodes: List[AtomicNode]
    edges: List[Tuple[str, str]]
    schema_version: str = "2.0"
    status: str = "pending"
    completed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["edges"] = [list(edge) for edge in self.edges]
        return value


def compile_atomic_instruction(text: str) -> AtomicExecutionGraph:
    return compile_atomic_actions(parse_instruction(text), text)


def compile_atomic_actions(actions: List[Action], source_instruction: str) -> AtomicExecutionGraph:
    normalized = [_normalize_action(action) for action in actions]
    if any(action.parameters.get("action_id") for action in normalized):
        return _compile_dependency_actions(normalized, source_instruction)
    operations: List[Dict[str, Any]] = []
    prepared_arms = set()
    for action in normalized:
        if action.arm not in prepared_arms:
            operations.extend(_startup_operations(action.arm))
            prepared_arms.add(action.arm)
        operations.extend(_expand_action(action))
    nodes = _build_nodes(operations)
    edges = [
        (nodes[index].node_id, nodes[index + 1].node_id)
        for index in range(len(nodes) - 1)
    ]
    return AtomicExecutionGraph(
        graph_id=f"atomic-{uuid4().hex}",
        source_instruction=source_instruction,
        created_at=utc_now(),
        nodes=nodes,
        edges=edges,
    )


def _normalize_action(action: Action) -> Action:
    if action.name == "wait":
        if action.arm not in {"left", "right"}:
            raise ValueError("Atomic hardware waits require a physical arm")
        seconds = float(action.parameters.get("seconds", 0))
        if seconds <= 0:
            raise ValueError("Atomic hardware waits require positive seconds")
        return Action(
            "wait", arm=action.arm, object_id=action.object_id,
            parameters=dict(action.parameters, seconds=seconds),
        )
    if action.object_id not in ARM_BY_OBJECT:
        raise ValueError(f"Unsupported object binding: {action.object_id}")
    expected_arm = ARM_BY_OBJECT[action.object_id]
    arm = action.arm or expected_arm
    if arm != expected_arm:
        raise ValueError(f"{action.object_id} is bound to {expected_arm}, not {arm}")
    if action.name == "place":
        expected_target = TARGET_BY_ARM[arm]
        initial_target = f"initial_{action.object_id}"
        if action.target not in {expected_target, initial_target}:
            raise ValueError(f"{arm} only supports {expected_target}, not {action.target}")
    if action.name not in {"pick", "place"}:
        raise ValueError(f"Unsupported atomic action: {action.name}")
    return Action(
        action.name,
        arm=arm,
        object_id=action.object_id,
        target=action.target,
        parameters=dict(action.parameters),
    )


def _startup_operations(arm: str) -> List[Dict[str, Any]]:
    return [
        _operation("wait_controller_ready", arm, timeout_seconds=300.0),
        _operation("clear_error", arm),
        _operation("enable_robot", arm),
        _operation("configure_robot", arm, speed_percent=DEFAULT_SPEED_PERCENT),
    ]


def _expand_action(action: Action) -> List[Dict[str, Any]]:
    if action.name == "pick":
        return [
            _operation("open_gripper", action.arm),
            _operation("move_safe_pose", action.arm),
            _operation("move_pose", action.arm, pose_ref=f"object:{action.object_id}:transport", motion="linear"),
            _operation("move_pose", action.arm, pose_ref=f"object:{action.object_id}", motion="linear"),
            _operation("close_gripper", action.arm),
            _operation("move_pose", action.arm, pose_ref=f"object:{action.object_id}:transport", motion="linear"),
        ]
    if action.name == "place":
        return [
            _operation("move_pose", action.arm, pose_ref=f"target:{action.target}:transport", motion="linear"),
            _operation("move_pose", action.arm, pose_ref=f"target:{action.target}", motion="linear"),
            _operation("open_gripper", action.arm),
            _operation("move_pose", action.arm, pose_ref=f"target:{action.target}:transport", motion="linear"),
            _operation("move_safe_pose", action.arm),
        ]
    raise ValueError(f"Unsupported atomic action: {action.name}")


def _compile_dependency_actions(actions: List[Action], source_instruction: str) -> AtomicExecutionGraph:
    specifications: List[Dict[str, Any]] = []
    init_tails = []
    for arm in ("left", "right"):
        previous = None
        for operation, suffix in (
            ("wait_controller_ready", "ready"), ("clear_error", "clear"),
            ("enable_robot", "enable"), ("configure_robot", "configure"),
            ("open_gripper", "open"), ("move_safe_pose", "home"),
        ):
            label = f"init:{arm}:{suffix}"
            parameters = {"speed_percent": DEFAULT_SPEED_PERCENT} if operation == "configure_robot" else {}
            specifications.append(_labeled_operation(label, operation, arm, [previous] if previous else [], **parameters))
            previous = label
        init_tails.append(previous)
    specifications.append(_labeled_operation("initialization_barrier", "barrier", "system", init_tails))

    action_tails: Dict[str, str] = {}
    arm_tails: Dict[str, str] = {"left": "initialization_barrier", "right": "initialization_barrier"}
    for action in actions:
        action_id = action.parameters["action_id"]
        explicit_dependencies = action.parameters.get("depends_on", [])
        dependencies = [action_tails[value] for value in explicit_dependencies]
        if not dependencies:
            dependencies = [arm_tails[action.arm]]
        expanded = _expand_dependency_action(action)
        previous = None
        for index, specification in enumerate(expanded):
            specification = dict(specification)
            label = specification["label"]
            node_dependencies = dependencies if index == 0 else [previous]
            specification["dependencies"] = node_dependencies
            specifications.append(specification)
            previous = label
        action_tails[action_id] = previous
        arm_tails[action.arm] = previous

    left_home = "final:left:home"
    right_home = "final:right:home"
    specifications.append(_labeled_operation(left_home, "move_safe_pose", "left", [arm_tails["left"]]))
    specifications.append(_labeled_operation(right_home, "move_safe_pose", "right", [arm_tails["right"]]))
    specifications.append(_labeled_operation("finalization_barrier", "barrier", "system", [left_home, right_home]))
    specifications.append(_labeled_operation("final:left:disable", "disable_robot", "left", ["finalization_barrier"]))
    specifications.append(_labeled_operation("final:right:disable", "disable_robot", "right", ["final:left:disable"]))

    nodes = _build_dependency_nodes(specifications)
    edges = [(dependency, node.node_id) for node in nodes for dependency in node.dependencies]
    return AtomicExecutionGraph(
        graph_id=f"atomic-{uuid4().hex}", source_instruction=source_instruction,
        created_at=utc_now(), nodes=nodes, edges=edges,
    )


def _expand_dependency_action(action: Action) -> List[Dict[str, Any]]:
    action_id = action.parameters["action_id"]
    if action.name == "wait":
        return [{
            "label": f"{action_id}:wait", "operation": "wait", "arm": action.arm,
            "parameters": {"seconds": action.parameters["seconds"]}, "timeout_seconds": action.parameters["seconds"] + 30.0,
        }]
    if action.name == "pick":
        source = action.parameters.get("source_ref", f"object:{action.object_id}")
        return [
            _dependency_spec(f"{action_id}:transport", "move_pose", action.arm, pose_ref=f"{source}:transport", motion="linear"),
            _dependency_spec(f"{action_id}:contact", "move_pose", action.arm, pose_ref=source, motion="linear"),
            _dependency_spec(f"{action_id}:grasp", "close_gripper", action.arm),
            _dependency_spec(f"{action_id}:lift", "move_pose", action.arm, pose_ref=f"{source}:transport", motion="linear"),
        ]
    destination = action.parameters.get("destination_ref")
    if not destination:
        destination = f"target:{action.target}" if action.target.startswith("target_") else f"object:{action.object_id}"
    return [
        _dependency_spec(f"{action_id}:transport", "move_pose", action.arm, pose_ref=f"{destination}:transport", motion="linear"),
        _dependency_spec(f"{action_id}:contact", "move_pose", action.arm, pose_ref=destination, motion="linear"),
        _dependency_spec(f"{action_id}:release", "open_gripper", action.arm),
        _dependency_spec(f"{action_id}:retract", "move_pose", action.arm, pose_ref=f"{destination}:transport", motion="linear"),
    ]


def _dependency_spec(label: str, operation: str, arm: str, timeout_seconds: float = 900.0, **parameters):
    return {"label": label, "operation": operation, "arm": arm, "parameters": parameters, "timeout_seconds": timeout_seconds}


def _labeled_operation(label: str, operation: str, arm: str, dependencies: List[str], timeout_seconds: float = 900.0, **parameters):
    return {
        "label": label, "operation": operation, "arm": arm, "dependencies": dependencies,
        "parameters": parameters, "timeout_seconds": timeout_seconds,
    }


def _build_dependency_nodes(specifications: List[Dict[str, Any]]) -> List[AtomicNode]:
    label_to_id = {specification["label"]: f"atomic-{index:03d}" for index, specification in enumerate(specifications)}
    nodes = []
    for sequence, specification in enumerate(specifications):
        operation = specification["operation"]
        nodes.append(AtomicNode(
            node_id=f"atomic-{sequence:03d}", sequence=sequence, operation=operation,
            arm=specification["arm"], label=specification["label"],
            parameters=specification["parameters"],
            dependencies=[label_to_id[value] for value in specification["dependencies"]],
            preconditions=_preconditions(operation), expected_outputs=_expected_outputs(operation),
            success_criteria=_success_criteria(operation), timeout_seconds=specification["timeout_seconds"],
        ))
    return nodes


def _operation(operation: str, arm: str, timeout_seconds: float = 180.0, **parameters) -> Dict[str, Any]:
    return {
        "operation": operation,
        "arm": arm,
        "parameters": parameters,
        "timeout_seconds": timeout_seconds,
    }


def _build_nodes(operations: List[Dict[str, Any]]) -> List[AtomicNode]:
    nodes: List[AtomicNode] = []
    previous_id: Optional[str] = None
    for sequence, specification in enumerate(operations):
        node_id = f"atomic-{sequence:03d}"
        operation = specification["operation"]
        nodes.append(
            AtomicNode(
                node_id=node_id,
                sequence=sequence,
                operation=operation,
                arm=specification["arm"],
                label=f"serial:{sequence:03d}:{operation}",
                parameters=specification["parameters"],
                dependencies=[previous_id] if previous_id else [],
                preconditions=_preconditions(operation),
                expected_outputs=_expected_outputs(operation),
                success_criteria=_success_criteria(operation),
                timeout_seconds=specification["timeout_seconds"],
            )
        )
        previous_id = node_id
    return nodes


def _preconditions(operation: str) -> List[str]:
    return {
        "wait_controller_ready": ["network_route_available"],
        "clear_error": ["controller_reachable"],
        "enable_robot": ["errors_cleared", "safety_circuit_ready"],
        "configure_robot": ["robot_enabled"],
        "barrier": ["dependencies_succeeded"],
        "wait": ["arm_holding_position"],
        "disable_robot": ["robot_at_safe_pose"],
        "open_gripper": ["gripper_connected"],
        "close_gripper": ["gripper_connected"],
        "move_safe_pose": ["robot_enabled", "workspace_clear"],
        "move_pose": ["robot_enabled", "workspace_clear", "pose_configured"],
    }.get(operation, [])


def _expected_outputs(operation: str) -> List[str]:
    if operation.startswith("move"):
        return ["target_feedback_reached", "controller_idle"]
    if operation.endswith("gripper"):
        return ["gripper_command_accepted", "gripper_position_stable"]
    if operation == "wait":
        return ["requested_duration_elapsed"]
    if operation == "barrier":
        return ["dependencies_satisfied"]
    if operation == "disable_robot":
        return ["robot_mode_4"]
    return ["command_completed"]


def _success_criteria(operation: str) -> List[str]:
    if operation.startswith("move"):
        return [
            "command_accepted",
            "target_feedback_within_tolerance",
            "three_consecutive_robot_mode_5_samples",
            "no_controller_alarm",
        ]
    if operation == "close_gripper":
        return ["command_accepted", "meaningful_closing_travel", "five_stable_position_samples"]
    if operation == "open_gripper":
        return ["command_accepted", "near_open_position", "five_stable_position_samples"]
    if operation == "wait":
        return ["monotonic_elapsed_time_reached"]
    if operation == "barrier":
        return ["all_dependencies_succeeded"]
    if operation == "disable_robot":
        return ["command_accepted", "three_consecutive_robot_mode_4_samples"]
    return ["command_accepted"]
