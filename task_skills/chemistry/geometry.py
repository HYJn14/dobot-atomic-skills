"""Pure geometry for the chemistry atomic skills.

Two conventions are inherited from the validated field scripts and must not be
re-derived here without re-reading them:

* The controller's Euler triple is ``R = Rz(rz) @ Ry(ry) @ Rx(rx)``, matching
  ``rotation_matrix_rpy`` in ``run_left_tube_level.py``. At ``|ry| = 90`` the
  triple is degenerate - only ``rx - rz`` is determined - so
  :func:`matrix_to_euler_zyx` folds the free degree into ``rz`` with ``rx = 0``,
  exactly like ``rpy_from_matrix`` in ``run_right_beaker_pour.py``.
* Tool0 ``X`` is the axis that is vertical when the gripper is table-parallel,
  and Tool0 ``Y`` is the finger-plate normal, i.e. the jaw opening direction
  (``run_right_beaker_pour.py`` module docstring and ``tip_axis``).

Nothing in this module touches hardware, sockets or files. Every public
function is pure, validates finiteness and shape, and raises ``ValueError`` with
a precise message instead of returning a silent NaN.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

Matrix = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
Vector = tuple[float, float, float]
RPY = tuple[float, float, float]


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _vector3(value: Any, label: str) -> Vector:
    if isinstance(value, (str, bytes)) or not isinstance(value, (Sequence, Iterable)):
        raise ValueError(f"{label} must be a sequence of three numbers")
    items = list(value)
    if len(items) != 3:
        raise ValueError(f"{label} must contain exactly three numbers, got {len(items)}")
    return (_finite(items[0], f"{label}[0]"), _finite(items[1], f"{label}[1]"),
            _finite(items[2], f"{label}[2]"))


def _matrix(value: Any, label: str) -> Matrix:
    rows = list(value)
    if len(rows) != 3:
        raise ValueError(f"{label} must be a 3x3 matrix")
    return tuple(_vector3(row, f"{label}[{index}]") for index, row in enumerate(rows))  # type: ignore[return-value]


def _minimum(value: Any, label: str, minimum: float, maximum: float) -> float:
    number = _finite(value, label)
    if not minimum <= number <= maximum:
        raise ValueError(f"{label} must be in [{minimum}, {maximum}], got {number}")
    return number


def _count(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be in [{minimum}, {maximum}], got {value}")
    return value


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def euler_zyx_to_matrix(rpy: Any) -> Matrix:
    """Build ``Rz(rz) @ Ry(ry) @ Rx(rx)`` from degrees (controller convention)."""
    rx, ry, rz = _vector3(rpy, "rpy")
    roll, pitch, yaw = (math.radians(value) for value in (rx, ry, rz))
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def matrix_to_euler_zyx(matrix: Any) -> RPY:
    """Inverse of :func:`euler_zyx_to_matrix`, in degrees.

    ``matrix[2][0] == -sin(ry)``, so ``|ry| = 90`` makes ``Rx`` and ``Rz`` act on
    the same axis. That free degree goes into ``rz`` (``rx`` is reported as 0),
    which keeps the round trip deterministic instead of alternating branches.
    """
    rows = _matrix(matrix, "matrix")
    sin_pitch = _clamp(-rows[2][0])
    pitch = math.asin(sin_pitch)
    if abs(rows[2][0]) < 1.0 - 1e-12:
        roll = math.atan2(rows[2][1], rows[2][2])
        yaw = math.atan2(rows[1][0], rows[0][0])
    else:
        roll = 0.0
        yaw = math.atan2(-rows[0][1], rows[1][1])
    return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))


def matrix_multiply(first: Any, second: Any) -> Matrix:
    """Multiply two 3x3 matrices."""
    left = _matrix(first, "first")
    right = _matrix(second, "second")
    return tuple(  # type: ignore[return-value]
        tuple(sum(left[row][k] * right[k][column] for k in range(3)) for column in range(3))
        for row in range(3)
    )


def matrix_vector(matrix: Any, vector: Any) -> Vector:
    """Rotate a 3-vector by a 3x3 matrix."""
    rows = _matrix(matrix, "matrix")
    point = _vector3(vector, "vector")
    return tuple(sum(rows[row][k] * point[k] for k in range(3)) for row in range(3))  # type: ignore[return-value]


def rotation_about_axis_deg(axis_unit: Any, angle_deg: Any) -> Matrix:
    """Rodrigues rotation about a unit axis, angles in degrees."""
    axis = _vector3(axis_unit, "axis")
    norm = math.sqrt(sum(component * component for component in axis))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("axis must be a non-zero vector")
    x, y, z = (component / norm for component in axis)
    angle = math.radians(_finite(angle_deg, "angle_deg"))
    cos, sin = math.cos(angle), math.sin(angle)
    return (
        (cos + x * x * (1 - cos), x * y * (1 - cos) - z * sin, x * z * (1 - cos) + y * sin),
        (y * x * (1 - cos) + z * sin, cos + y * y * (1 - cos), y * z * (1 - cos) - x * sin),
        (z * x * (1 - cos) - y * sin, z * y * (1 - cos) + x * sin, cos + z * z * (1 - cos)),
    )


_AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}


def tool_axis(rpy: Any, axis: str = "X") -> Vector:
    """Return one Tool0 basis axis in world coordinates."""
    if axis not in _AXIS_INDEX:
        raise ValueError(f"axis must be one of X, Y, Z, got {axis!r}")
    matrix = euler_zyx_to_matrix(rpy)
    column = _AXIS_INDEX[axis]
    return (matrix[0][column], matrix[1][column], matrix[2][column])


def angle_between_degrees(first: Any, second: Any) -> float:
    """Angle between two vectors in degrees, independent of their lengths."""
    left = _vector3(first, "first")
    right = _vector3(second, "second")
    left_norm = math.sqrt(sum(component * component for component in left))
    right_norm = math.sqrt(sum(component * component for component in right))
    if left_norm <= 1e-12 or right_norm <= 1e-12:
        raise ValueError("both vectors must be non-zero")
    dot = sum(left[index] * right[index] for index in range(3)) / (left_norm * right_norm)
    return math.degrees(math.acos(_clamp(dot)))


def tilt_from_vertical_deg(rpy: Any, axis: str = "X") -> float:
    """Angle of a Tool0 axis to the vertical LINE, in ``[0, 90]``.

    Measured to the line rather than to ``+Z`` so a tool pointing straight down
    reports 0, which is what ``tool_x_tilt_degrees`` does in the field scripts.
    """
    return math.degrees(math.acos(_clamp(abs(tool_axis(rpy, axis)[2]))))


def angle_from_horizontal_deg(vector: Any) -> float:
    """Angle of a vector out of the horizontal plane, in ``[0, 90]``.

    ``0`` means the vector lies in the table plane. The finger-plate normal must
    stay here for the plates to remain vertical while the beaker tips.
    """
    unit_ish = _vector3(vector, "vector")
    norm = math.sqrt(sum(component * component for component in unit_ish))
    if norm <= 1e-12:
        raise ValueError("vector must be non-zero")
    return math.degrees(math.asin(_clamp(abs(unit_ish[2] / norm))))


def jaw_normal_world(start_rpy: Any, axis: str = "Y") -> Vector:
    """Return the horizontalised unit jaw-plate normal of an upright start pose."""
    normal = list(tool_axis(start_rpy, axis))
    normal[2] = 0.0
    norm = math.sqrt(sum(component * component for component in normal))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError(f"Tool{axis} of the start pose cannot define a horizontal jaw-plate normal")
    return (normal[0] / norm, normal[1] / norm, normal[2] / norm)


def pivot_from_tool_offset(start_xyz: Any, start_rpy: Any, offset_mm: Any) -> Vector:
    """Estimated cup centre: the start TCP plus a Tool0-local offset."""
    point = _vector3(start_xyz, "start_xyz")
    offset = _vector3(offset_mm, "pivot_offset_tool_mm")
    rotated = matrix_vector(euler_zyx_to_matrix(start_rpy), offset)
    return (point[0] + rotated[0], point[1] + rotated[1], point[2] + rotated[2])


def tip_sweep_deg(start_rpy: Any, target_tilt_deg: Any, axis: str = "Y") -> float:
    """Signed sweep about the fixed horizontal jaw normal that reaches the target tilt.

    Mirrors ``tip_axis`` in ``run_right_beaker_pour.py``: the start must be
    upright (ToolX down, jaw normal horizontal), the sweep is the angle between
    the current ToolX vertical component and the requested tilt, and the end
    orientation must still rotate the beaker mouth away from the body.
    """
    tilt = _minimum(target_tilt_deg, "tilt_deg", 0.0, 90.0)
    if tilt <= 0.0:
        raise ValueError("tilt_deg must be greater than 0")
    start_matrix = euler_zyx_to_matrix(start_rpy)
    normal = jaw_normal_world(start_rpy, axis)
    start_x = (start_matrix[0][0], start_matrix[1][0], start_matrix[2][0])
    cross_z = normal[0] * start_x[1] - normal[1] * start_x[0]
    sweep = math.degrees(math.atan2(-start_x[2], cross_z)) - (90.0 - tilt)
    if not math.isfinite(sweep) or not 0.0 < sweep <= 180.0:
        raise ValueError("no positive fixed-normal sweep reaches the requested ToolX tilt")
    end_matrix = matrix_multiply(rotation_about_axis_deg(normal, sweep), start_matrix)
    toward_mouth = tuple(-start_matrix[row][2] for row in range(3))
    if sum(end_matrix[row][0] * toward_mouth[row] for row in range(3)) <= 0.0:
        raise ValueError("fixed-normal tip would rotate ToolX away from the intended mouth-facing direction")
    return sweep


def rotate_about_pivot(start_xyz: Any, start_rpy: Any, pivot_offset_tool_mm: Any,
                       tilt_deg: Any, segments: int = 16, axis: str = "Y") -> list[dict[str, Any]]:
    """Waypoints for one continuous tip about an estimated instrument centre.

    The TCP position is recomputed at every step so the estimated centre stays
    fixed in world space while the orientation sweeps about the initial
    horizontal jaw normal. Returns ``segments + 1`` waypoints; ``[0]`` is the
    start pose exactly and the last one is at the requested tilt.
    """
    point = _vector3(start_xyz, "start_xyz")
    tilt = _minimum(tilt_deg, "tilt_deg", 0.0, 90.0)
    if tilt <= 0.0:
        raise ValueError("tilt_deg must be greater than 0")
    count = _count(segments, "segments", 1, 1024)
    offset = _vector3(pivot_offset_tool_mm, "pivot_offset_tool_mm")
    start_matrix = euler_zyx_to_matrix(start_rpy)
    centre = pivot_from_tool_offset(point, start_rpy, offset)
    normal = jaw_normal_world(start_rpy, axis)
    sweep = tip_sweep_deg(start_rpy, tilt, axis)

    waypoints: list[dict[str, Any]] = []
    for step in range(count + 1):
        theta = sweep * step / count
        reference_matrix = matrix_multiply(rotation_about_axis_deg(normal, theta), start_matrix)
        rotated_offset = matrix_vector(reference_matrix, offset)
        waypoint_xyz = (centre[0] - rotated_offset[0], centre[1] - rotated_offset[1],
                        centre[2] - rotated_offset[2])
        reference_rpy = matrix_to_euler_zyx(reference_matrix)
        waypoints.append({
            "step": step,
            "theta_deg": theta,
            "xyz": waypoint_xyz,
            "reference_rpy": reference_rpy,
            "cup_center_xyz": centre,
            "cup_offset_tool_mm": offset,
            "start_rpy": _vector3(start_rpy, "start_rpy"),
            "jaw_normal_world": normal,
            "tip_sweep_deg": sweep,
            "target_tilt_deg": tilt,
            "reference_tool_x_tilt_deg": tilt_from_vertical_deg(reference_rpy, "X"),
        })
    return waypoints
