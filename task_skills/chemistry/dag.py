"""Mermaid export for a validated chemistry graph.

One place turns a graph into the picture a human reviews, so the drawing cannot
say something the validated graph does not: the arrows are the ``depends_on``
edges and every label is a normalised parameter. Regenerating is therefore safe
after any edit - the diagram is derived, never hand-maintained.
"""

from __future__ import annotations

from typing import Any


# Mermaid node shapes, chosen so the kind of action is visible before reading the
# label: moves are plain boxes, grippers are parallelograms, a dwell is a stadium,
# a pivot is a hexagon, and a park is a subroutine box.
_SHAPES = {
    "gripper.set_position": ('[/"', '"/]'),
    "control.hold": ('(["', '"])'),
    "arm.rotate_about_pivot": ('{{"', '"}}'),
    "arm.move_to_home": ('[["', '"]]'),
}
_DEFAULT_SHAPE = ('["', '"]')

_CLASS_DEFS = {
    "leftArm": "fill:#e8f1fb,stroke:#1a5fb4,stroke-width:1px,color:#0d2b4e",
    "rightArm": "fill:#fdecec,stroke:#c0392b,stroke-width:1px,color:#5b1a15",
    "holdNode": "fill:#fff6da,stroke:#c9a227,stroke-width:2px,color:#5a4708",
    "pourNode": "fill:#e9f7ee,stroke:#1e8449,stroke-width:2px,color:#0d3d22",
    "homeNode": "fill:#eeeafc,stroke:#5b4bc4,stroke-width:2px,color:#241a5c",
}

_SPECIAL_CLASSES = {
    "control.hold": "holdNode",
    "arm.rotate_about_pivot": "pourNode",
    "arm.move_to_home": "homeNode",
}


def _trim(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number == int(number) else f"{number:g}"


def _detail(node: dict[str, Any]) -> str:
    parameters = node["parameters"]
    skill = node["skill"]
    if skill in {"arm.move_joint", "arm.move_linear"}:
        source = parameters.get("source_ref")
        target = parameters["target_ref"]
        return f"{source} &rarr; {target}" if source else f"&rarr; {target}"
    if skill == "arm.move_to_home":
        return "&rarr; 标定初始位姿"
    if skill == "gripper.set_position":
        release = " + release" if parameters.get("allow_release") else ""
        return f"pos={parameters['position']}{release}"
    if skill == "arm.rotate_about_pivot":
        return f"tilt {_trim(parameters['tilt_deg'])}&deg;"
    if skill == "control.hold":
        return f"wait {_trim(parameters['seconds'])}s"
    return skill


def mermaid(graph: dict[str, Any]) -> str:
    """Render a validated graph as a Mermaid flowchart."""
    order = graph["topological_order"]
    nodes = {node["id"]: node for node in graph["nodes"]}
    lines = ["flowchart TD"]
    for index, node_id in enumerate(order, start=1):
        node = nodes[node_id]
        opening, closing = _SHAPES.get(node["skill"], _DEFAULT_SHAPE)
        lines.append(f"  {node_id}{opening}<b>{index:02d} {node_id}</b><br/>"
                     f"{node['arm']} &middot; {node['skill']}<br/>{_detail(node)}{closing}")
    lines.append("")
    for node_id in order:
        for dependency in nodes[node_id]["depends_on"]:
            lines.append(f"  {dependency} --> {node_id}")
    lines.append("")
    for name, style in _CLASS_DEFS.items():
        lines.append(f"  classDef {name} {style}")
    for arm, name in (("left", "leftArm"), ("right", "rightArm")):
        members = [node_id for node_id in order if nodes[node_id]["arm"] == arm]
        if members:
            lines.append(f"  class {','.join(members)} {name}")
    for skill, name in _SPECIAL_CLASSES.items():
        members = [node_id for node_id in order if nodes[node_id]["skill"] == skill]
        if members:
            lines.append(f"  class {','.join(members)} {name}")
    return "\n".join(lines) + "\n"
