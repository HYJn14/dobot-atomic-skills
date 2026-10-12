import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    z: float
    rx: float = 0.0
    ry: float = 0.0
    rz: float = 0.0
    joints: Optional[tuple] = None


@dataclass
class SceneConfig:
    objects: Dict[str, Pose]
    targets: Dict[str, Pose]
    transport_heights: Dict[str, float] = field(default_factory=dict)
    temporary_target: Optional[str] = None


@dataclass(frozen=True)
class Action:
    name: str
    arm: Optional[str] = None
    object_id: Optional[str] = None
    target: Optional[str] = None
    parameters: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Action":
        if len(value) != 1:
            raise ValueError("Each action entry must contain exactly one action name")
        name, payload = next(iter(value.items()))
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError(f"Payload for {name!r} must be a mapping")
        return cls(name=name, **payload)


def parse_plan(value: List[Dict[str, Any]]) -> List[Action]:
    return [Action.from_dict(item) for item in value]


def load_scene_config(path: Path) -> SceneConfig:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    return SceneConfig(
        objects={name: _pose_from_dict(pose) for name, pose in value["objects"].items()},
        targets={name: _pose_from_dict(pose) for name, pose in value["targets"].items()},
        transport_heights={arm: float(height) for arm, height in value.get("transport_heights", {}).items()},
        temporary_target=value.get("temporary_target"),
    )


def resolve_pose_ref(
    scene: SceneConfig,
    reference: str,
    height: float = 50.0,
    arm: Optional[str] = None,
) -> Pose:
    parts = reference.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid pose reference: {reference}")
    category, identifier = parts[:2]
    if category == "object":
        pose = scene.objects[identifier]
    elif category == "target":
        pose = scene.targets[identifier]
    else:
        raise ValueError(f"Invalid pose category: {category}")
    if len(parts) == 2:
        return pose
    modifier = parts[2]
    if modifier == "above":
        return Pose(pose.x, pose.y, pose.z + height, pose.rx, pose.ry, pose.rz)
    if modifier == "transport":
        if arm is None:
            raise ValueError(f"Transport pose requires an arm: {reference}")
        try:
            transport_height = scene.transport_heights[arm]
        except KeyError as error:
            raise ValueError(f"No transport height configured for arm: {arm}") from error
        return Pose(pose.x, pose.y, transport_height, pose.rx, pose.ry, pose.rz)
    raise ValueError(f"Invalid pose modifier: {modifier}")


def _pose_from_dict(value: Dict[str, Any]) -> Pose:
    pose = dict(value)
    if pose.get("joints") is not None:
        pose["joints"] = tuple(pose["joints"])
    return Pose(**pose)
