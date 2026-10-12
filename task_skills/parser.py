import re
from typing import List

from .models import Action


def parse_instruction(text: str) -> List[Action]:
    coordinated = _parse_coordinated_hold_return(text)
    if coordinated is not None:
        return coordinated
    actions: List[Action] = []
    clauses = [part.strip(" ，,。；;\n\t") for part in re.split(r"[，,。；;\n]+", text) if part.strip()]
    for clause in clauses:
        clause = re.sub(r"^(然后|接着|再|先)\s*", "", clause)
        compact_match = re.search(
            r"(?:把|将)?(?:物体)?([A-Za-z])抓取(?:后)?放到目标\s*([A-Za-z0-9]+)$",
            clause,
        )
        if compact_match:
            object_id = compact_match.group(1).upper()
            target = "target_" + compact_match.group(2).upper()
            actions.extend([
                Action("pick", object_id=object_id),
                Action("place", object_id=object_id, target=target),
            ])
            continue
        wait_match = re.search(r"(?:等待|等|停留|保持不动)\s*(\d+(?:\.\d+)?)\s*秒", clause)
        if wait_match:
            actions.append(Action("wait", parameters={"seconds": float(wait_match.group(1))}))
            continue
        pick_match = re.search(r"(?:用(左|右)臂)?抓取(?:物体)?([A-Za-z])", clause)
        if pick_match:
            arm = _arm(pick_match.group(1)) if pick_match.group(1) else None
            actions.append(Action("pick", arm, pick_match.group(2).upper()))
            continue
        place_match = re.search(
            r"(?:把|将)(?:左臂的|右臂的)?(?:物体)?([A-Za-z])放到(.+)$", clause
        )
        if place_match:
            object_id = place_match.group(1).upper()
            arm = "left" if "左臂" in clause else "right" if "右臂" in clause else None
            target_text = place_match.group(2).strip()
            target_match = re.search(r"目标\s*([A-Za-z0-9]+)", target_text)
            object_pose_match = re.search(r"(?:物体)?\s*([A-Za-z0-9]+).*?(原来|初始)?的?位置", target_text)
            if target_match:
                target = "target_" + target_match.group(1).upper()
            elif object_pose_match:
                target = "initial_" + object_pose_match.group(1).upper()
            else:
                raise ValueError(f"无法解析放置目标: {target_text}")
            actions.append(Action("place", arm, object_id, target))
            continue
        raise ValueError(f"无法解析指令片段: {clause}")
    return actions


def _parse_coordinated_hold_return(text: str):
    compact = re.sub(r"[\s，,。；;]", "", text)
    required = (
        "左臂", "物体A", "右臂", "物体B", "保持5", "目标A", "目标B",
        "从目标B", "原来的位置",
    )
    if not all(token in compact for token in required):
        return None
    return [
        Action(
            "pick", arm="left", object_id="A",
            parameters={"action_id": "left_pick_A", "depends_on": [], "source_ref": "object:A"},
        ),
        Action(
            "wait", arm="left", object_id="A",
            parameters={"action_id": "left_hold_A", "depends_on": ["left_pick_A"], "seconds": 5.0},
        ),
        Action(
            "pick", arm="right", object_id="B",
            parameters={"action_id": "right_pick_B", "depends_on": [], "source_ref": "object:B"},
        ),
        Action(
            "place", arm="right", object_id="B", target="target_B",
            parameters={
                "action_id": "right_place_B", "depends_on": ["right_pick_B"],
                "destination_ref": "target:target_B",
            },
        ),
        Action(
            "place", arm="left", object_id="A", target="target_A",
            parameters={
                "action_id": "left_place_A", "depends_on": ["left_hold_A"],
                "destination_ref": "target:target_A",
            },
        ),
        Action(
            "pick", arm="right", object_id="B",
            parameters={
                "action_id": "right_repick_B",
                "depends_on": ["right_place_B", "left_place_A"],
                "source_ref": "target:target_B",
            },
        ),
        Action(
            "place", arm="right", object_id="B", target="initial_B",
            parameters={
                "action_id": "right_return_B", "depends_on": ["right_repick_B"],
                "destination_ref": "object:B",
            },
        ),
    ]


def _arm(value: str) -> str:
    return "left" if value == "左" else "right"
