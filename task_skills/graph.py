from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from .models import Action


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SkillNode:
    node_id: str
    sequence: int
    skill_name: str
    arm: Optional[str]
    object_id: Optional[str]
    target: Optional[str]
    parameters: Dict[str, Any] = field(default_factory=dict)
    preconditions: List[str] = field(default_factory=list)
    expected_outputs: List[str] = field(default_factory=list)
    internal_states: List[str] = field(default_factory=list)
    success_criteria: List[str] = field(default_factory=list)
    safety_exit: str = "stop_and_preserve_evidence"
    status: str = "pending"


@dataclass
class ExecutionGraph:
    graph_id: str
    source_instruction: Optional[str]
    created_at: str
    nodes: List[SkillNode]
    edges: List[Tuple[str, str]]
    parallel_groups: List[List[str]]
    schema_version: str = "1.0"
    status: str = "pending"
    completed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["edges"] = [list(edge) for edge in self.edges]
        return value


def build_execution_graph(actions: List[Action], source_instruction: Optional[str] = None) -> ExecutionGraph:
    nodes = [_node_from_action(index, action) for index, action in enumerate(actions)]
    parallel_groups: List[List[str]] = []
    groups: List[List[SkillNode]] = []
    index = 0
    while index < len(nodes):
        if index + 1 < len(nodes) and is_parallel_pair(actions[index:index + 2]):
            group = [nodes[index], nodes[index + 1]]
            groups.append(group)
            parallel_groups.append([node.node_id for node in group])
            index += 2
        else:
            groups.append([nodes[index]])
            index += 1
    edges = [
        (source.node_id, target.node_id)
        for previous, current in zip(groups, groups[1:])
        for source in previous
        for target in current
    ]
    return ExecutionGraph(
        graph_id=f"graph-{uuid4().hex}",
        source_instruction=source_instruction,
        created_at=utc_now(),
        nodes=nodes,
        edges=edges,
        parallel_groups=parallel_groups,
    )


def compile_instruction(text: str) -> ExecutionGraph:
    from .parser import parse_instruction

    return build_execution_graph(parse_instruction(text), source_instruction=text)


def _node_from_action(sequence: int, action: Action) -> SkillNode:
    details = {
        "pick": (
            ["arm_available", "object_is_not_held", "arm_object_binding_valid"],
            ["object_held", "object_location_unchanged_until_place"],
            ["ready", "approach", "contact", "grasp", "lift", "completed"],
            ["grasp_command_accepted", "object_marked_held"],
        ),
        "place": (
            ["object_held_by_bound_arm", "target_is_allowed", "target_is_unoccupied"],
            ["object_released", "object_location_updated"],
            ["ready", "approach", "contact", "release", "lift", "completed"],
            ["release_command_accepted", "object_marked_at_target"],
        ),
        "wait": (
            ["duration_is_nonnegative"], ["wait_elapsed"], ["waiting", "completed"], ["duration_elapsed"],
        ),
    }
    preconditions, outputs, states, criteria = details.get(
        action.name, ([], [], ["started", "completed"], ["command_accepted"])
    )
    return SkillNode(
        node_id=f"node-{sequence:03d}",
        sequence=sequence,
        skill_name=action.name,
        arm=_effective_arm(action),
        object_id=action.object_id,
        target=action.target,
        parameters=dict(action.parameters),
        preconditions=preconditions,
        expected_outputs=outputs,
        internal_states=states,
        success_criteria=criteria,
    )


def is_parallel_pair(actions: List[Action]) -> bool:
    return (
        len(actions) == 2
        and {_effective_arm(actions[0]), _effective_arm(actions[1])} == {"left", "right"}
        and {actions[0].object_id, actions[1].object_id} == {"A", "B"}
        and actions[0].name == actions[1].name
    )


def _effective_arm(action: Action) -> Optional[str]:
    if action.arm:
        return action.arm
    return {"A": "left", "B": "right"}.get(action.object_id)
