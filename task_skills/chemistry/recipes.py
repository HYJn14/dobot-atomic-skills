"""Small versioned graph templates built exclusively from registered atoms."""

from __future__ import annotations

from typing import Any


VERSION = "1.0.0"


def _node(node_id: str, skill: str, arm: str, parameters: dict[str, Any],
          depends_on: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": node_id,
        "skill": skill,
        "version": VERSION,
        "arm": arm,
        "parameters": parameters,
        "depends_on": list(depends_on or []),
    }


def _one_arm_workflow(arm: str, prefix: str, object_name: str) -> list[dict[str, Any]]:
    """Approach, grasp, close and carry - assuming the jaws are already open.

    Opening the jaws is a separate, explicitly authorised step: the driver refuses
    to widen them without ``allow_release=True``, so a graph that opens them here
    could never run past that node.
    """
    base_ref = f"{arm}.{object_name}"
    approach_id = f"{prefix}_approach"
    grasp_id = f"{prefix}_grasp"
    close_id = f"{prefix}_close"
    work_id = f"{prefix}_work"
    return [
        _node(approach_id, "arm.move_joint", arm,
              {"target_ref": f"{base_ref}.approach", "grip_requirement": "open"}),
        _node(grasp_id, "arm.move_linear", arm,
              {"source_ref": f"{base_ref}.approach", "target_ref": f"{base_ref}.grasp",
               "grip_requirement": "open"}, [approach_id]),
        _node(close_id, "gripper.set_position", arm,
              {"position": 0, "allow_release": False, "at_ref": f"{base_ref}.grasp"}, [grasp_id]),
        _node(work_id, "arm.move_joint", arm,
              {"source_ref": f"{base_ref}.grasp", "target_ref": f"{base_ref}.work",
               "grip_requirement": "clamped"}, [close_id]),
    ]


def _gripper_open_node(arm: str, object_name: str) -> dict[str, Any]:
    """An authorised jaw opening. Position feedback cannot prove the jaws are empty,
    so ``allow_release=True`` is the graph author asserting it on site."""
    return _node(f"{arm}_{object_name}_gripper_open", "gripper.set_position", arm,
                 {"position": 255, "speed": 10, "allow_release": True})


def compile_recipe(name: str) -> dict[str, Any]:
    """Return a fresh JSON-compatible graph for a compact named chemistry workflow."""
    if name == "prepare_open_grippers":
        return {
            "schema_version": VERSION,
            "name": name,
            "description": ("Open both grippers with explicit release authorisation. Run this "
                            "first, once the site confirms the jaws hold nothing; every task "
                            "graph assumes open jaws and never opens them itself."),
            "initial_facts": {"left_gripper": "unknown; operator asserts empty",
                              "right_gripper": "unknown; operator asserts empty"},
            "nodes": [_gripper_open_node("left", "tube"), _gripper_open_node("right", "beaker")],
        }
    if name == "right_beaker_to_work":
        return {
            "schema_version": VERSION,
            "name": name,
            "description": ("Approach, linearly grasp, close, and carry the right beaker to its "
                            "work pose. Assumes open jaws: run prepare_open_grippers first."),
            "nodes": _one_arm_workflow("right", "right_beaker", "beaker"),
        }
    if name == "left_tube_to_work":
        return {
            "schema_version": VERSION,
            "name": name,
            "description": ("Approach, linearly grasp, close, and carry the left tube to its work "
                            "pose. Assumes open jaws: run prepare_open_grippers first."),
            "nodes": _one_arm_workflow("left", "left_tube", "tube"),
        }
    if name == "dual_prepare":
        nodes = _one_arm_workflow("left", "left_tube", "tube")
        nodes.extend(_one_arm_workflow("right", "right_beaker", "beaker"))
        return {
            "schema_version": VERSION,
            "name": name,
            "description": ("Prepare both instruments serially; no parallel collision-free motion "
                            "is asserted. Assumes open jaws: run prepare_open_grippers first."),
            "nodes": nodes,
        }
    if name == "right_beaker_pour":
        return {
            "schema_version": VERSION,
            "name": name,
            "description": ("Tip the right beaker about its estimated cup center; stream placement "
                            "is uncalibrated. Assumes the beaker is already held at its work pose."),
            "nodes": [_node("right_beaker_pour", "arm.rotate_about_pivot", "right",
                            {"source_ref": "right.beaker.work"})],
        }
    raise KeyError(f"unknown chemistry recipe {name!r}; available: "
                   ", ".join(recipe_names()))


def recipe_names() -> tuple[str, ...]:
    return ("prepare_open_grippers", "right_beaker_to_work", "left_tube_to_work",
            "dual_prepare", "right_beaker_pour")
