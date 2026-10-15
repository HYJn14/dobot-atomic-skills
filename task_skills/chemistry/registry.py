"""Versioned skill whitelist and strict parameter normalization."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
SUPPORTED_VERSION = "1.0.0"


class SkillValidationError(ValueError):
    """Raised when a skill or one of its parameters is not in the registry contract."""


def load_catalog() -> dict[str, Any]:
    with (HERE / "catalog.json").open("r", encoding="utf-8") as handle:
        catalog = json.load(handle)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("skills"), list):
        raise ValueError("chemistry catalog must contain a skills array")
    return catalog


def load_profile(path: str | Path | None = None) -> dict[str, Any]:
    profile_path = Path(path) if path is not None else HERE / "profile.json"
    with profile_path.open("r", encoding="utf-8") as handle:
        profile = json.load(handle)
    if not isinstance(profile, dict):
        raise ValueError(f"profile must be a JSON object: {profile_path}")
    return profile


def _number(value: Any, label: str, minimum: float, maximum: float,
            *, minimum_exclusive: bool = False, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SkillValidationError(f"{label} must be a {'integer' if integer else 'number'}")
    if integer and not isinstance(value, int):
        raise SkillValidationError(f"{label} must be an integer")
    if not math.isfinite(float(value)):
        raise SkillValidationError(f"{label} must be finite")
    lower_bad = value <= minimum if minimum_exclusive else value < minimum
    if lower_bad or value > maximum:
        bracket = "(" if minimum_exclusive else "["
        raise SkillValidationError(f"{label} must be in {bracket}{minimum}, {maximum}]")
    return value


def _vector3(value: Any, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise SkillValidationError(f"{label} must contain exactly three finite numbers")
    result: list[float] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise SkillValidationError(f"{label}[{index}] must be a finite number")
        result.append(float(item))
    return result


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SkillValidationError(f"{label} must be an object")
    return value


def _validate_pose_reference(profile: dict[str, Any], reference: str, arm: str,
                             *, cartesian: bool = False, joints: bool = False) -> dict[str, Any]:
    poses = profile.get("poses")
    if not isinstance(poses, dict) or reference not in poses:
        raise SkillValidationError(f"pose reference {reference!r} is missing from profile.poses")
    pose = _mapping(poses[reference], f"profile.poses[{reference!r}]")
    if pose.get("arm") != arm:
        raise SkillValidationError(f"pose {reference!r} belongs to {pose.get('arm')!r}, not {arm!r}")
    if cartesian:
        _vector3(pose.get("xyz"), f"pose {reference!r}.xyz")
        _vector3(pose.get("rpy"), f"pose {reference!r}.rpy")
    if joints:
        raw_joints = pose.get("joints")
        if not isinstance(raw_joints, (list, tuple)) or len(raw_joints) != 6:
            raise SkillValidationError(f"pose {reference!r}.joints must contain exactly six finite values")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
               for value in raw_joints):
            raise SkillValidationError(f"pose {reference!r}.joints must contain exactly six finite values")
    return pose


def _strict_keys(parameters: dict[str, Any], allowed: set[str], required: set[str], skill_id: str) -> None:
    unknown = set(parameters) - allowed
    missing = required - set(parameters)
    if unknown:
        raise SkillValidationError(f"{skill_id} has unknown parameter(s): {', '.join(sorted(unknown))}")
    if missing:
        raise SkillValidationError(f"{skill_id} is missing parameter(s): {', '.join(sorted(missing))}")


def normalize_parameters(skill_id: str, arm: str, raw_parameters: Any,
                         profile: dict[str, Any]) -> dict[str, Any]:
    """Validate one registered skill and fill its versioned parameter defaults."""
    if arm not in {"left", "right"}:
        raise SkillValidationError("arm must be 'left' or 'right'")
    profile = _mapping(profile, "profile")
    parameters = dict(_mapping(raw_parameters, "parameters"))

    if skill_id == "arm.move_joint":
        allowed = {"target_ref", "source_ref", "speed", "grip_requirement", "tilt_limit_deg"}
        _strict_keys(parameters, allowed, {"target_ref"}, skill_id)
        parameters.setdefault("speed", 20)
        parameters.setdefault("grip_requirement", "any")
        parameters.setdefault("tilt_limit_deg", 1)
        target_ref = parameters["target_ref"]
        if not isinstance(target_ref, str) or not target_ref:
            raise SkillValidationError("target_ref must be a non-empty string")
        _validate_pose_reference(profile, target_ref, arm, joints=True)
        if "source_ref" in parameters:
            source_ref = parameters["source_ref"]
            if not isinstance(source_ref, str) or not source_ref:
                raise SkillValidationError("source_ref must be a non-empty string")
            _validate_pose_reference(profile, source_ref, arm)
        parameters["speed"] = _number(parameters["speed"], "speed", 1, 100, integer=True)
        if parameters["grip_requirement"] not in {"any", "open", "clamped"}:
            raise SkillValidationError("grip_requirement must be any, open, or clamped")
        parameters["tilt_limit_deg"] = _number(parameters["tilt_limit_deg"], "tilt_limit_deg", 0, 10)
        return parameters

    if skill_id == "arm.move_linear":
        allowed = {"target_ref", "source_ref", "speed", "grip_requirement", "tilt_limit_deg"}
        _strict_keys(parameters, allowed, {"target_ref", "source_ref"}, skill_id)
        parameters.setdefault("speed", 20)
        parameters.setdefault("grip_requirement", "open")
        parameters.setdefault("tilt_limit_deg", 1)
        for name in ("target_ref", "source_ref"):
            reference = parameters[name]
            if not isinstance(reference, str) or not reference:
                raise SkillValidationError(f"{name} must be a non-empty string")
            _validate_pose_reference(profile, reference, arm, cartesian=True)
        parameters["speed"] = _number(parameters["speed"], "speed", 1, 100, integer=True)
        if parameters["grip_requirement"] not in {"any", "open", "clamped"}:
            raise SkillValidationError("grip_requirement must be any, open, or clamped")
        parameters["tilt_limit_deg"] = _number(parameters["tilt_limit_deg"], "tilt_limit_deg", 0, 10)
        return parameters

    if skill_id == "arm.move_to_home":
        # No target parameter on purpose: the target is the package constant
        # layout.physical_nova_home_degrees, the same one the run_graph finaliser
        # and dobot_adapter.move_to_safe_pose use. Accepting a pose reference here
        # would create a second source of truth for "home" that could drift.
        allowed = {"source_ref", "speed"}
        _strict_keys(parameters, allowed, set(), skill_id)
        parameters.setdefault("speed", 20)
        if "source_ref" in parameters:
            source_ref = parameters["source_ref"]
            if not isinstance(source_ref, str) or not source_ref:
                raise SkillValidationError("source_ref must be a non-empty string")
            _validate_pose_reference(profile, source_ref, arm)
        parameters["speed"] = _number(parameters["speed"], "speed", 1, 100, integer=True)
        return parameters

    if skill_id == "gripper.set_position":
        allowed = {"position", "speed", "allow_release", "at_ref"}
        _strict_keys(parameters, allowed, {"position"}, skill_id)
        parameters.setdefault("speed", 10)
        parameters.setdefault("allow_release", False)
        parameters["position"] = _number(parameters["position"], "position", 0, 255, integer=True)
        parameters["speed"] = _number(parameters["speed"], "speed", 1, 100, integer=True)
        if not isinstance(parameters["allow_release"], bool):
            raise SkillValidationError("allow_release must be a boolean")
        if "at_ref" in parameters:
            reference = parameters["at_ref"]
            if not isinstance(reference, str) or not reference:
                raise SkillValidationError("at_ref must be a non-empty string")
            _validate_pose_reference(profile, reference, arm)
        return parameters

    if skill_id == "arm.rotate_about_pivot":
        allowed = {"source_ref", "tilt_deg", "offset_mm", "segments", "speed", "cp"}
        _strict_keys(parameters, allowed, {"source_ref"}, skill_id)
        if arm != "right":
            raise SkillValidationError("arm.rotate_about_pivot currently requires arm='right'")
        parameters.setdefault("tilt_deg", 80)
        parameters.setdefault("segments", 16)
        parameters.setdefault("speed", 15)
        parameters.setdefault("cp", 100)
        source_ref = parameters["source_ref"]
        if source_ref != "right.beaker.work":
            raise SkillValidationError("arm.rotate_about_pivot currently requires source_ref='right.beaker.work'")
        _validate_pose_reference(profile, source_ref, "right", cartesian=True, joints=True)
        if "offset_mm" not in parameters:
            pour = _mapping(profile.get("pour"), "profile.pour")
            parameters["offset_mm"] = pour.get("offset_mm")
        parameters["offset_mm"] = _vector3(parameters["offset_mm"], "offset_mm")
        parameters["tilt_deg"] = _number(parameters["tilt_deg"], "tilt_deg", 0, 90,
                                         minimum_exclusive=True)
        parameters["segments"] = _number(parameters["segments"], "segments", 2, 128, integer=True)
        parameters["speed"] = _number(parameters["speed"], "speed", 1, 100, integer=True)
        parameters["cp"] = _number(parameters["cp"], "cp", 0, 100, integer=True)
        return parameters

    if skill_id == "control.hold":
        _strict_keys(parameters, {"seconds"}, {"seconds"}, skill_id)
        parameters["seconds"] = _number(parameters["seconds"], "seconds", 0, 600,
                                        minimum_exclusive=True)
        return parameters

    raise SkillValidationError(f"unregistered chemistry skill: {skill_id!r}")


def derive_resources(skill_id: str, arm: str, profile: dict[str, Any]) -> list[str]:
    resources = [f"arm:{arm}"]
    if skill_id in {"arm.move_joint", "arm.move_linear", "arm.move_to_home",
                    "arm.rotate_about_pivot"}:
        resources.append("workspace:shared")
    if skill_id == "gripper.set_position":
        arms = _mapping(profile.get("arms"), "profile.arms")
        arm_config = _mapping(arms.get(arm), f"profile.arms.{arm}")
        port = arm_config.get("gripper_port")
        if not isinstance(port, str) or not port:
            raise SkillValidationError(f"profile.arms.{arm}.gripper_port must be a non-empty string")
        resources.append(f"serial:{port}")
    return resources


def invoke(skill_id: str, arm: str, parameters: dict[str, Any], driver: Any,
           profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call one atom through the same strict graph validation/execution contract."""
    from .graph import execute_graph

    selected_profile = load_profile() if profile is None else profile
    graph = {
        "schema_version": SUPPORTED_VERSION,
        "name": f"invoke:{skill_id}",
        "nodes": [{"id": "invoke", "skill": skill_id, "version": SUPPORTED_VERSION,
                   "arm": arm, "parameters": parameters, "depends_on": []}],
    }
    return execute_graph(graph, driver, profile=selected_profile)
