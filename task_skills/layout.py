from typing import Dict, Tuple


PHYSICAL_NOVA_IP: Dict[str, str] = {
    "left": "192.168.5.1",
    "right": "192.168.5.2",
}

PHYSICAL_NOVA_HOME_DEGREES: Dict[str, Tuple[float, ...]] = {
    "left": (90, 0, 90, 0, -90, -90),
    "right": (-90, 0, -90, 0, 90, 90),
}

PHYSICAL_NOVA_GRIPPER_BINDINGS: Dict[str, Tuple[str, str]] = {
    "left": ("GRIPPER_RIGHT", "COM8"),
    "right": ("GRIPPER_LEFT", "COM7"),
}

# Measured on this rig on 2026-09-24 during the single-arm object-A run: the
# normalized gripper position is inverted relative to the Feetech driver
# default, i.e. 0 is fully closed and 255 is fully open. The teleop path keeps
# the driver convention; the atomic adapter must use these measured values.
GRIPPER_OPEN_POSITION = 255
GRIPPER_CLOSED_POSITION = 0

MASTER_HOME_RADIANS: Dict[str, Tuple[float, ...]] = {
    "left": (-1.57, 0, -1.57, 0, 1.57, 1.57),
    "right": (1.57, 0, 1.57, 0, -1.57, -1.57),
}


def physical_nova_ip(side: str) -> str:
    try:
        return PHYSICAL_NOVA_IP[side]
    except KeyError as error:
        raise ValueError(f"Unknown physical Nova side: {side}") from error


def physical_nova_home_degrees(side: str) -> Tuple[float, ...]:
    try:
        return PHYSICAL_NOVA_HOME_DEGREES[side]
    except KeyError as error:
        raise ValueError(f"Unknown physical Nova side: {side}") from error


def physical_nova_gripper_binding(side: str) -> Tuple[str, str]:
    try:
        return PHYSICAL_NOVA_GRIPPER_BINDINGS[side]
    except KeyError as error:
        raise ValueError(f"Unknown physical Nova side: {side}") from error


def physical_nova_home_radians(side: str):
    import math

    return tuple(math.radians(value) for value in physical_nova_home_degrees(side))


def master_home_radians(side: str) -> Tuple[float, ...]:
    try:
        return MASTER_HOME_RADIANS[side]
    except KeyError as error:
        raise ValueError(f"Unknown master side: {side}") from error
