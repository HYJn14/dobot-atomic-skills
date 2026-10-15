"""Offline-first R09-to-R10 right-arm beaker approach and guarded grasp."""

import argparse
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, TextIO

from .atomic_executor import AtomicGraphExecutor
from .atomic_graph import AtomicExecutionGraph, AtomicNode
from .device_lease import DeviceLease
from .dobot_adapter import DobotHardwareAdapter
from .graph import utc_now
from .models import Pose, SceneConfig
from .run_beaker_approach import (
    DEFAULT_ARTIFACT_ROOT as R09_ARTIFACT_ROOT,
    R09_ARM,
    R09_SPEED_PERCENT,
    R09_TOOL,
    R09_USER,
    RunResult,
    R09DobotHardwareAdapter,
    run_beaker_approach,
)


DEFAULT_SCENE_PATH = Path(__file__).with_name("demos") / "beaker_r10_scene.json"
DEFAULT_ARTIFACT_ROOT = R09_ARTIFACT_ROOT.parent / "beaker-r10"
R10_GRAPH_ID = "beaker-r10-right-grasp"
R10_POUR_READY_GRAPH_ID = "beaker-r10-right-pour-ready"
R10_SOURCE = "Preflight right-arm IK/FK for approximately level Cartesian R09 and R10, approach R09, move linearly to R10, close at speed 10, and stay."
R10_POUR_READY_SOURCE = (
    "Preflight right-arm IK/FK for approximately level Cartesian R09 and R10 plus the pour-ready target, approach R09, move linearly to R10, "
    "close the gripper at speed 10, lift back to R09, then move linearly to the Cartesian pour-ready target and stay closed."
)
R10_OPEN_MINIMUM = 250
R10_GRIPPER_ACTION = "close"
R10_GRIPPER_SPEED = 10
# Measured near-level candidates retain source XYZ/Rx signs and adjust Ry/Rz together.
R10_APPROACH_POSE = {
    "x": -117.5765, "y": -344.8222, "z": 171.6492,
    "rx": -175.763, "ry": 89.5, "rz": 170.2908,
}
R10_GRIP_POSE = {
    "x": -117.5764, "y": -344.8220, "z": 93.5212,
    "rx": -175.7637, "ry": 89.5, "rz": 170.2901,
}
R10_POUR_READY_POSE = {
    "x": -117.5765, "y": -344.8222, "z": 415.5852,
    "rx": 176.3784, "ry": 90.0, "rz": 86.3587,
}
# Source-screenshot taught joint values, used only as conservative wrist-continuity veto references.
R10_TAUGHT_JOINT_REFERENCES = {
    "R09": (-104.0774, -40.8842, -120.9522, 52.4488, 173.1281, -20.0117),
    "R10": (-104.073, -58.9861, -117.6177, 67.2813, 173.1378, -19.9461),
    "pour_ready": (-90.0111, -25.5603, -117.5143, 142.7309, 90.0192, 90.0595),
}
R10_WRIST_CONTINUITY_LIMIT_DEGREES = 15.0
R10_J5_SINGULARITY_MARGIN_DEGREES = 0.25
_R10_WRIST_JOINT_INDICES = (3, 4, 5)
R10_BLOCKED_RUN_ID = "run_20261015_033808_302411_ur-ready_1c0d01"


class BlockedCandidateError(RuntimeError):
    """Raised when the default live path is quarantined by observed motion failure."""


def _blocked_candidate_message() -> str:
    return (
        "R10 current R09-to-R10 candidate is quarantined after controller run "
        f"{R10_BLOCKED_RUN_ID}: R09 JointMovJ succeeded at measured Z=171.6492 mm "
        "with J5=179.4845 degrees, then R10 MovL raised alarm 26 in mode 9 after "
        "16 ms. No R10 descent or gripper close followed; gripper feedback remained "
        "at the reported open position 253. Existing "
        "Cartesian endpoints and the 0.25-degree reject-only J5 margin are retained; "
        "endpoint IK/FK and that numerical margin do not validate the controller path. "
        "Use --dry-run or --preflight-only for inspection."
    )


@dataclass(frozen=True)
class R10Scene:
    arm: str
    user_number: int
    tool_number: int
    speed_percent: int
    gripper_action: str
    gripper_speed: int
    approach_pose: Pose
    grip_pose: Pose
    pour_ready_pose: Pose

    def execution_scene(self) -> SceneConfig:
        return SceneConfig(
            objects={"B": self.approach_pose},
            targets={"R10": self.grip_pose, "POUR_READY": self.pour_ready_pose},
        )


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"R10 {field_name} must be a finite number")
    return float(value)


def _load_pose(value: Any, field_name: str) -> Pose:
    keys = {"x", "y", "z", "rx", "ry", "rz"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"R10 {field_name} keys must be exactly {sorted(keys)}")
    cartesian = tuple(_finite_number(value[key], f"{field_name}.{key}") for key in ("x", "y", "z", "rx", "ry", "rz"))
    return Pose(*cartesian)


def _r10_pose_dict(pose: Pose) -> dict:
    return {key: getattr(pose, key) for key in ("x", "y", "z", "rx", "ry", "rz")}


def load_r10_scene(path: Path = DEFAULT_SCENE_PATH) -> R10Scene:
    def reject_constant(value: str):
        raise ValueError(f"Non-finite JSON number is not allowed: {value}")

    value = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject_constant)
    expected_keys = {
        "schema_version", "arm", "user_number", "tool_number", "speed_percent",
        "gripper_action", "gripper_speed", "r09_pose", "r10_pose", "pour_ready_pose",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError(f"R10 scene keys must be exactly {sorted(expected_keys)}")
    for key, expected in (
        ("schema_version", "beaker-r10-scene/2"),
        ("arm", R09_ARM),
        ("user_number", R09_USER),
        ("tool_number", R09_TOOL),
        ("speed_percent", R09_SPEED_PERCENT),
        ("gripper_action", R10_GRIPPER_ACTION),
        ("gripper_speed", R10_GRIPPER_SPEED),
    ):
        actual = value[key]
        if type(actual) is not type(expected) or actual != expected:
            raise ValueError(f"R10 scene {key} must be {expected!r}")
    approach_pose = _load_pose(value["r09_pose"], "r09_pose")
    grip_pose = _load_pose(value["r10_pose"], "r10_pose")
    pour_ready_pose = _load_pose(value["pour_ready_pose"], "pour_ready_pose")
    for name, pose, expected in (
        ("r09_pose", approach_pose, R10_APPROACH_POSE),
        ("r10_pose", grip_pose, R10_GRIP_POSE),
        ("pour_ready_pose", pour_ready_pose, R10_POUR_READY_POSE),
    ):
        if _r10_pose_dict(pose) != expected:
            raise ValueError(f"R10 {name} must match the validated measured near-level Cartesian pose")
    return R10Scene(
        R09_ARM, R09_USER, R09_TOOL, R09_SPEED_PERCENT, R10_GRIPPER_ACTION, R10_GRIPPER_SPEED,
        approach_pose, grip_pose, pour_ready_pose,
    )


def build_r10_graph(scene: R10Scene, to_pour_ready: bool = False) -> AtomicExecutionGraph:
    approach_data = _r10_pose_dict(scene.approach_pose)
    grip_data = _r10_pose_dict(scene.grip_pose)
    pour_ready_data = _r10_pose_dict(scene.pour_ready_pose)
    preflight_poses = {"R09": approach_data, "R10": grip_data}
    if to_pour_ready:
        preflight_poses["pour_ready"] = pour_ready_data
    specifications = [
        ("check_gripper_open", "right:r10:preflight-empty-open-gripper", {}, 30.0),
        ("wait_controller_ready", "right:r10:wait-ready", {}, 300.0),
        ("clear_error", "right:r10:clear-error", {}, 180.0),
        ("enable_robot", "right:r10:enable", {}, 180.0),
        (
            "configure_robot", "right:r10:configure-user-tool-speed",
            {"speed_percent": scene.speed_percent, "tool_number": scene.tool_number, "user_number": scene.user_number}, 180.0,
        ),
        (
            "validate_kinematics", "right:r10:preflight-inverse-and-positive-solutions",
            {"poses": preflight_poses, "to_pour_ready": to_pour_ready}, 60.0,
        ),
        (
            "move_pose", "right:r09:beaker-joint-approach",
            {"pose_ref": "object:B", "motion": "inverse_solution_joint", "kinematic_ref": "R09", "calibrated_pose": approach_data}, 180.0,
        ),
        (
            "move_pose", "right:r10:linear-grip-point",
            {"pose_ref": "target:R10", "motion": "linear", "calibrated_pose": grip_data}, 180.0,
        ),
        (
            "close_gripper", "right:r10:close-gripper-speed-10",
            {"speed_percent": scene.gripper_speed}, 30.0,
        ),
    ]
    if to_pour_ready:
        specifications.extend([
            (
                "move_pose", "right:r10:linear-lift-back-to-r09",
                {"pose_ref": "object:B", "motion": "linear", "calibrated_pose": approach_data}, 180.0,
            ),
            (
                "move_pose", "right:pour-ready:linear-cartesian-target",
                {"pose_ref": "target:POUR_READY", "motion": "linear", "calibrated_pose": pour_ready_data}, 180.0,
            ),
        ])
    nodes = []
    previous_id = None
    for sequence, (operation, label, parameters, timeout) in enumerate(specifications):
        node_id = f"r10-{sequence + 1:03d}"
        if operation == "check_gripper_open":
            expected_outputs = ["right_gripper_feedback_at_least_250"]
            success_criteria = ["measured_gripper_position_at_least_250"]
            preconditions = ["operator_confirmed_gripper_is_empty"]
        elif operation == "configure_robot":
            expected_outputs = ["speed_and_acceleration_limits_configured", "tool_0_selected", "user_0_selected"]
            success_criteria = [
                "speed_factor_10_acknowledged", "acc_j_10_acknowledged", "speed_j_10_acknowledged",
                "acc_l_10_acknowledged", "speed_l_10_acknowledged", "tool_0_acknowledged", "user_0_acknowledged",
            ]
            preconditions = ["right_robot_enabled"]
        elif operation == "move_pose":
            expected_outputs = ["target_feedback_reached", "controller_idle"]
            success_criteria = [
                "command_accepted", "target_pose_feedback_within_tolerance",
                "three_consecutive_robot_mode_5_samples", "no_controller_alarm",
            ]
            preconditions = ["right_robot_enabled", "speed_10_configured", "tool_0_selected", "user_0_selected", "workspace_clear"]
        elif operation == "validate_kinematics":
            pose_names = list(parameters["poses"])
            expected_outputs = [f"{name.lower()}_inverse_solution_verified" for name in pose_names]
            success_criteria = [
                "inverse_solution_status_zero", "six_finite_joint_angles",
                "positive_solution_status_zero", "cartesian_roundtrip_within_tolerance",
            ]
            preconditions = ["right_robot_enabled", "speed_10_configured", "tool_0_selected", "user_0_selected", "no_motion_started"]
        elif operation == "close_gripper":
            expected_outputs = ["close_command_acknowledged", "gripper_feedback_closed_or_stable_after_closing"]
            success_criteria = ["serial_result_zero", "servo_error_zero", "closed_or_stable_after_closing"]
            preconditions = ["right_arm_feedback_at_r10_grip_point"]
        else:
            expected_outputs = {
                "wait_controller_ready": ["controller_reachable"],
                "clear_error": ["command_completed"],
                "enable_robot": ["robot_enabled_and_idle"],
            }[operation]
            success_criteria = ["command_accepted"]
            preconditions = {
                "wait_controller_ready": ["right_controller_reachable"],
                "clear_error": ["right_controller_reachable"],
                "enable_robot": ["right_errors_cleared", "safety_circuit_ready"],
            }[operation]
        nodes.append(AtomicNode(
            node_id=node_id, sequence=sequence, operation=operation, arm=R09_ARM, label=label,
            parameters=parameters, dependencies=[previous_id] if previous_id else [], preconditions=preconditions,
            expected_outputs=expected_outputs, success_criteria=success_criteria, timeout_seconds=timeout,
        ))
        previous_id = node_id
    return AtomicExecutionGraph(
        graph_id=R10_POUR_READY_GRAPH_ID if to_pour_ready else R10_GRAPH_ID,
        source_instruction=R10_POUR_READY_SOURCE if to_pour_ready else R10_SOURCE,
        created_at=utc_now(), nodes=nodes,
        edges=[(nodes[index].node_id, nodes[index + 1].node_id) for index in range(len(nodes) - 1)],
        status="compiled",
    )


def _rotation_matrix_rpy(rx: float, ry: float, rz: float):
    """Return the Z-Y-X Euler rotation matrix used for RPY pose comparisons."""
    roll, pitch, yaw = (math.radians(value) for value in (rx, ry, rz))
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def _orientation_distance_degrees(first, second) -> float:
    first_matrix = _rotation_matrix_rpy(*first)
    second_matrix = _rotation_matrix_rpy(*second)
    trace = sum(first_matrix[row][column] * second_matrix[row][column] for row in range(3) for column in range(3))
    return math.degrees(math.acos(max(-1.0, min(1.0, (trace - 1.0) / 2.0))))


def _j5_distance_to_singularity(joint_degrees: float) -> float:
    """Return the raw angular distance from J5 to the nearest 180-degree singularity."""
    return abs(math.remainder(joint_degrees, 180.0))


def _cartesian_pose_matches(measured, target) -> bool:
    if len(measured) != 6 or len(target) != 6:
        return False
    if not DobotHardwareAdapter._target_matches(measured[:3], target[:3], 3):
        return False
    return _orientation_distance_degrees(measured[3:], target[3:]) <= DobotHardwareAdapter._ANGLE_TOLERANCE_DEGREES


def _require_r10_zero_ack(command: str, reply: Any) -> None:
    if not re.match(r"^\s*0\s*,", str(reply)):
        raise RuntimeError(f"R10 {command} requires a zero-status controller reply; got {reply!r}")


def _seeded_ik_command(pose: dict, seed_joints) -> str:
    target = tuple(
        _finite_number(pose[key], f"seeded IK pose.{key}")
        for key in ("x", "y", "z", "rx", "ry", "rz")
    )
    seed = tuple(_finite_number(value, "seeded IK joint") for value in seed_joints)
    if len(seed) != 6:
        raise ValueError("R10 seeded IK requires exactly six taught seed joints")
    target_fields = [f"{value:.9f}" for value in target]
    seed_fields = ",".join(f"{value:.9f}" for value in seed)
    return (
        f"InverseSolution({','.join(target_fields)},{R09_USER},{R09_TOOL},1,"
        f"{{{seed_fields}}})"
    )


class R10DobotHardwareAdapter(R09DobotHardwareAdapter):
    """Right-arm R10 adapter with controller IK preflight and guarded grasp."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ik_solutions = {}
        self._kinematics_progress = {}

    @staticmethod
    def _zero_status_values(reply: Any, field_name: str) -> tuple:
        match = re.match(r"^\s*([+-]?\d+)\s*,\s*\{([^{}]*)\}\s*(?:,|$)", str(reply))
        if not match:
            raise RuntimeError(f"R10 {field_name} reply must have a status and six-value payload: {reply!r}")
        if int(match.group(1)) != 0:
            raise RuntimeError(f"R10 {field_name} returned nonzero status {match.group(1)}: {reply!r}")
        fields = match.group(2).split(",")
        if len(fields) != 6:
            raise RuntimeError(f"R10 {field_name} reply must contain exactly six values: {reply!r}")
        try:
            values = tuple(float(value.strip()) for value in fields)
        except ValueError as error:
            raise RuntimeError(f"R10 {field_name} reply contains a non-numeric value: {reply!r}") from error
        if not all(math.isfinite(value) for value in values):
            raise RuntimeError(f"R10 {field_name} reply contains a non-finite value: {reply!r}")
        return values

    def _validated_pose_map(self, node: AtomicNode, scene: SceneConfig) -> dict:
        parameters = node.parameters
        if not isinstance(parameters, dict) or set(parameters) != {"poses", "to_pour_ready"}:
            raise ValueError("R10 kinematics preflight requires the fixed pose set and flow selection")
        if type(parameters["to_pour_ready"]) is not bool:
            raise ValueError("R10 kinematics preflight flow selection must be boolean")
        expected = {
            "R09": _r10_pose_dict(scene.objects["B"]),
            "R10": _r10_pose_dict(scene.targets["R10"]),
        }
        if parameters["to_pour_ready"]:
            expected["pour_ready"] = _r10_pose_dict(scene.targets["POUR_READY"])
        if parameters["poses"] != expected:
            raise ValueError("R10 kinematics preflight poses do not match the validated fixed Cartesian targets")
        return expected

    def _preflight_kinematics(self, node: AtomicNode, scene: SceneConfig):
        poses = self._validated_pose_map(node, scene)
        dashboard = self._dashboard(R09_ARM)
        send_recv_msg = getattr(dashboard, "sendRecvMsg", None)
        if not callable(send_recv_msg):
            raise RuntimeError("R10 seeded IK requires dashboard.sendRecvMsg(); refusing an unseeded InverseSolution call")
        solutions = {}
        wrist_continuity = {}
        seeded_ik = {}
        self._kinematics_progress = {}
        for name, pose in poses.items():
            self._check_cancel()
            target = tuple(pose[key] for key in ("x", "y", "z", "rx", "ry", "rz"))
            reference = R10_TAUGHT_JOINT_REFERENCES[name]
            inverse_command = _seeded_ik_command(pose, reference)
            attempt = {
                "taught_reference_joints": list(reference),
                "seed_joints": list(reference),
                "raw_command": inverse_command,
                "result": "pending",
            }
            self._kinematics_progress[name] = attempt
            try:
                inverse_reply = self._send_command(
                    R09_ARM, lambda command=inverse_command: send_recv_msg(command),
                )
                attempt["raw_inverse_reply"] = str(inverse_reply)
                joints = self._zero_status_values(inverse_reply, f"{name} InverseSolution")
            except BaseException as error:
                attempt["result"] = f"FAIL: {type(error).__name__}: {error}"
                raise
            attempt["returned_joints"] = list(joints)
            j5_distance = _j5_distance_to_singularity(joints[4])
            attempt["j5_singularity_distance_degrees"] = round(j5_distance, 9)
            if j5_distance < R10_J5_SINGULARITY_MARGIN_DEGREES:
                attempt["result"] = (
                    "FAIL: J5 singularity margin veto; "
                    f"distance={j5_distance:.9f} degrees"
                )
                raise RuntimeError(
                    f"R10 {name} J5 singularity margin veto: joint={joints[4]:.9f} degrees; "
                    f"nearest 180-degree multiple distance={j5_distance:.9f} degrees; "
                    f"required at least {R10_J5_SINGULARITY_MARGIN_DEGREES:.9f} degrees"
                )
            wrist_deltas = {}
            for joint_index in _R10_WRIST_JOINT_INDICES:
                difference = joints[joint_index] - reference[joint_index]
                wrist_deltas[f"J{joint_index + 1}"] = round(difference, 9)
            attempt["wrist_deltas_degrees"] = wrist_deltas
            for joint_index in _R10_WRIST_JOINT_INDICES:
                difference = joints[joint_index] - reference[joint_index]
                if (
                    abs(difference) > R10_WRIST_CONTINUITY_LIMIT_DEGREES
                    and not math.isclose(
                        abs(difference), R10_WRIST_CONTINUITY_LIMIT_DEGREES,
                        rel_tol=0.0, abs_tol=1e-9,
                    )
                ):
                    attempt["result"] = (
                        f"FAIL: wrist continuity veto at J{joint_index + 1}; "
                        f"difference={difference:+.9f} degrees"
                    )
                    raise RuntimeError(
                        f"R10 {name} wrist continuity veto at J{joint_index + 1}: "
                        f"difference={difference:+.9f} degrees; "
                        f"reference={reference[joint_index]:.9f}, solution={joints[joint_index]:.9f}; "
                        f"limit={R10_WRIST_CONTINUITY_LIMIT_DEGREES:.6f} degrees (raw joint difference)"
                    )
            wrist_continuity[name] = {
                "taught_reference_joints": list(reference),
                "wrist_joint_deltas_degrees": wrist_deltas,
                "limit_degrees": R10_WRIST_CONTINUITY_LIMIT_DEGREES,
                "j5_singularity_distance_degrees": round(j5_distance, 9),
                "j5_singularity_margin_degrees": R10_J5_SINGULARITY_MARGIN_DEGREES,
            }
            seeded_ik[name] = {
                "method": "InverseSolution(seed_near_joint=1)",
                "seed_joints": list(reference),
                "raw_command": inverse_command,
            }
            try:
                positive_reply = self._send_command(
                    R09_ARM, lambda joints=joints: dashboard.PositiveSolution(*joints, R09_USER, R09_TOOL),
                )
                attempt["raw_positive_solution_reply"] = str(positive_reply)
                positive_pose = self._zero_status_values(positive_reply, f"{name} PositiveSolution")
                if not _cartesian_pose_matches(positive_pose, target):
                    raise RuntimeError(
                        f"R10 {name} PositiveSolution round trip does not match the requested Cartesian pose: "
                        f"measured={positive_pose}, target={target}"
                    )
            except BaseException as error:
                attempt["result"] = f"FAIL: {type(error).__name__}: {error}"
                raise
            attempt["result"] = "PASS: inverse status zero and positive-solution round trip verified"
            solutions[name] = joints
        self._ik_solutions = solutions
        return {
            "kinematics_preflight_verified": True,
            "solutions": {name: list(joints) for name, joints in solutions.items()},
            "roundtrip_verified": list(solutions),
            "seeded_ik": seeded_ik,
            "wrist_continuity": wrist_continuity,
            "kinematics_attempts": self._kinematics_progress,
        }

    def _wait_for_pose_target(self, arm: str, pose, timeout: Optional[float]) -> None:
        dashboard = self._dashboard(arm)
        if not hasattr(dashboard, "GetPose"):
            raise RuntimeError(f"Dobot {arm} cannot verify motion completion without GetPose()")
        target = (pose.x, pose.y, pose.z, pose.rx, pose.ry, pose.rz)
        deadline = time.monotonic() + (self._settle_timeout if timeout is None else float(timeout))
        stable_samples = 0
        last_values = None
        last_mode = None
        while time.monotonic() < deadline:
            self._check_cancel()
            last_mode = self._mode_value(dashboard.RobotMode())
            self._raise_for_bad_mode_or_alarm(arm, dashboard, last_mode)
            last_values = self._zero_status_values(dashboard.GetPose(), "GetPose")
            if last_mode == 5 and _cartesian_pose_matches(last_values, target):
                stable_samples += 1
                if stable_samples >= self._STABLE_SAMPLES:
                    return
            else:
                stable_samples = 0
            self._sleep(self._POLL_INTERVAL_SECONDS)
        raise TimeoutError(
            f"Dobot {arm} did not reach Cartesian target: mode={last_mode}, measured={last_values}, target={target}"
        )

    def move_to(self, arm: str, pose, motion: Optional[str] = None, timeout: Optional[float] = None) -> None:
        if arm != R09_ARM or motion != "linear":
            raise ValueError("R10 Cartesian moves only support right-arm linear motion")
        self._check_cancel()
        motion_api = self._motion(arm)
        reply = self._send_command(
            arm, lambda: motion_api.MovL(pose.x, pose.y, pose.z, pose.rx, pose.ry, pose.rz),
        )
        _require_r10_zero_ack("MovL", reply)
        self._wait_for_pose_target(arm, pose, timeout)

    def _move_r10_approach(self, node: AtomicNode, pose: Pose):
        kinematic_ref = node.parameters.get("kinematic_ref")
        joints = self._ik_solutions.get(kinematic_ref)
        if kinematic_ref != "R09" or joints is None:
            raise RuntimeError("R10 approach requires the freshly validated R09 InverseSolution")
        self._check_cancel()
        motion_api = self._motion(R09_ARM)
        reply = self._send_command(R09_ARM, lambda: motion_api.JointMovJ(*joints))
        _require_r10_zero_ack("inverse-solution JointMovJ", reply)
        dashboard = self._dashboard(R09_ARM)
        deadline = time.monotonic() + float(node.timeout_seconds)
        stable_samples = 0
        measured_joints = measured_pose = None
        while time.monotonic() < deadline:
            self._check_cancel()
            mode = self._mode_value(dashboard.RobotMode())
            self._raise_for_bad_mode_or_alarm(R09_ARM, dashboard, mode)
            measured_joints = self._zero_status_values(dashboard.GetAngle(), "GetAngle")
            measured_pose = self._zero_status_values(dashboard.GetPose(), "GetPose")
            joint_match = self._target_matches(measured_joints, joints, 0)
            pose_target = (pose.x, pose.y, pose.z, pose.rx, pose.ry, pose.rz)
            pose_match = _cartesian_pose_matches(measured_pose, pose_target)
            if mode == 5 and joint_match and pose_match:
                stable_samples += 1
                if stable_samples >= self._STABLE_SAMPLES:
                    return {
                        "pose_ref": "object:B", "motion": "inverse_solution_joint",
                        "inverse_solution_joints": list(joints),
                        "joint_target_feedback_verified": True,
                        "cartesian_target_feedback_verified": True,
                        "robot_mode_5_samples": stable_samples,
                        "pose": list(measured_pose), "joints": list(measured_joints),
                    }
            else:
                stable_samples = 0
            self._sleep(self._POLL_INTERVAL_SECONDS)
        raise TimeoutError(
            "Dobot right did not reach inverse-solution approach with joint and Cartesian feedback: "
            f"measured_joints={measured_joints}, target_joints={joints}, "
            f"measured_pose={measured_pose}, target_pose={pose_target}"
        )

    def execute_atomic(self, node: AtomicNode, scene: SceneConfig):
        if node.arm != R09_ARM:
            raise ValueError("R10 graph may only execute on the physical right arm")
        if node.operation not in {
            "check_gripper_open", "wait_controller_ready", "clear_error", "enable_robot",
            "configure_robot", "validate_kinematics", "move_pose", "close_gripper",
        }:
            raise ValueError(f"R10 graph does not support operation {node.operation!r}")
        if node.operation == "check_gripper_open":
            if node.parameters:
                raise ValueError("R10 gripper preflight does not accept parameters")
            position = self._gripper_position(R09_ARM)
            if position < R10_OPEN_MINIMUM:
                raise RuntimeError(
                    f"Right gripper opening is {position}, below {R10_OPEN_MINIMUM}; "
                    "open an empty gripper before running"
                )
            return {"gripper_position": position, "minimum_opening": R10_OPEN_MINIMUM}
        if node.operation == "validate_kinematics":
            self._ik_solutions = {}
            try:
                return self._preflight_kinematics(node, scene)
            except BaseException:
                self._ik_solutions = {}
                raise
        if node.operation == "move_pose":
            parameters = node.parameters
            pose_ref = parameters.get("pose_ref")
            if pose_ref == "object:B" and parameters.get("motion") == "inverse_solution_joint":
                expected = _r10_pose_dict(scene.objects["B"])
                if parameters != {
                    "pose_ref": "object:B", "motion": "inverse_solution_joint",
                    "kinematic_ref": "R09", "calibrated_pose": expected,
                }:
                    raise ValueError("R10 approach must use the validated R09 pose and its fresh inverse solution")
                return self._move_r10_approach(node, scene.objects["B"])
            expected_by_ref = {
                "target:R10": (scene.targets["R10"], "linear"),
                "object:B": (scene.objects["B"], "linear"),
                "target:POUR_READY": (scene.targets["POUR_READY"], "linear"),
            }
            if pose_ref not in expected_by_ref:
                raise ValueError("R10 graph contains an unsupported Cartesian pose reference")
            expected_pose, expected_motion = expected_by_ref[pose_ref]
            kinematic_ref = {
                "target:R10": "R10", "object:B": "R09", "target:POUR_READY": "pour_ready",
            }[pose_ref]
            if self._ik_solutions.get(kinematic_ref) is None:
                raise RuntimeError(f"R10 {kinematic_ref} Cartesian move requires its completed IK/FK preflight")
            expected = {
                "pose_ref": pose_ref, "motion": expected_motion,
                "calibrated_pose": _r10_pose_dict(expected_pose),
            }
            if parameters != expected:
                raise ValueError("R10 Cartesian motion does not match its validated fixed pose")
            return DobotHardwareAdapter.execute_atomic(self, node, scene)
        if node.operation == "close_gripper":
            expected = {"speed_percent": R10_GRIPPER_SPEED}
            if node.parameters != expected:
                raise ValueError("R10 close-gripper command is fixed at speed 10")
            return self._close_gripper(node)
        return super().execute_atomic(node, scene)

    def _close_gripper(self, node: AtomicNode):
        from .layout import GRIPPER_CLOSED_POSITION

        arm = R09_ARM
        gripper = self._gripper(arm)
        target = int(GRIPPER_CLOSED_POSITION)
        speed = R10_GRIPPER_SPEED
        start = self._gripper_position(arm)
        reply = self._send_command(arm, lambda: gripper.move(target, speed, 0))
        if not isinstance(reply, (tuple, list)) or len(reply) != 2 or reply[0] != 0 or reply[1] != 0:
            raise RuntimeError(
                f"Right gripper close command requires serial result 0 and servo error 0; got {reply!r}"
            )
        evidence = self._wait_for_gripper(arm, start, target, closing=True, timeout=node.timeout_seconds)
        evidence.update({
            "completion_mode": "closed_or_stable_after_closing",
            "desired_action": R10_GRIPPER_ACTION,
            "physical_grasp_verified": False,
        })
        return evidence

    def _hold_gripper_for_stop(self, arm, gripper, hold_target):
        reply = gripper.move(hold_target, R10_GRIPPER_SPEED, 0)
        if not isinstance(reply, (tuple, list)) or len(reply) != 2 or reply[0] != 0 or reply[1] != 0:
            raise RuntimeError(f"Right gripper stop hold requires serial result 0 and servo error 0; got {reply!r}")


def create_live_adapter(settle_timeout: float = 180.0) -> R10DobotHardwareAdapter:
    """Use only the right arm and its existing GRIPPER_LEFT binding."""
    return R10DobotHardwareAdapter.from_live((R09_ARM,), settle_timeout=settle_timeout)


def _make_r10_device_lease(purpose: str):
    configured_path = os.environ.get("DOBOT_DEVICE_LOCK_PATH")
    if configured_path:
        return DeviceLease(path=Path(configured_path), purpose=purpose)
    if os.name == "nt":
        return DeviceLease(path=Path(r"C:\DobotRuntime\device-owner"), purpose=purpose)
    return DeviceLease(purpose=purpose)


def _create_read_only_preflight_adapter(dashboard_factory=None) -> R10DobotHardwareAdapter:
    if dashboard_factory is None:
        from dobot_control.robots.dobot_api import DobotApiDashboard

        dashboard_factory = DobotApiDashboard
    from .layout import physical_nova_ip

    dashboard = None
    try:
        dashboard = dashboard_factory(physical_nova_ip(R09_ARM), 29999)
        return R10DobotHardwareAdapter.from_components(
            dashboards={R09_ARM: dashboard}, motions={}, grippers={},
        )
    except BaseException:
        if dashboard is not None:
            try:
                dashboard.close()
            except BaseException:
                pass
        raise


def _print_kinematics_report(output: TextIO, progress: dict, overall_result: str) -> None:
    print("=== R10 read-only controller kinematics check ===", file=output)
    print(
        "actual_motion_plan_status: REJECTED (current R09-to-R10 candidate is quarantined)",
        file=output,
    )
    print(
        "actual_motion_failure: "
        f"{R10_BLOCKED_RUN_ID}: R09 JointMovJ succeeded at measured Z=171.6492 mm "
        "with J5=179.4845 degrees; R10 MovL raised alarm 26 in mode 9 after 16 ms; "
        "no R10 descent or gripper close followed, and gripper feedback remained at "
        "the reported open position 253.",
        file=output,
    )
    print(
        "endpoint IK/FK PASS does not clear this motion rejection; the 0.25-degree "
        "J5 margin is numerical evidence only and does not validate the controller path",
        file=output,
    )
    print("scope: seeded controller IK/FK queries only; no robot mode or configuration changes", file=output)
    print("scope: no arm motion or gripper command is issued", file=output)
    for name, attempt in progress.items():
        print(f"target: {name}", file=output)
        if "taught_reference_joints" in attempt:
            print("  taught_reference_joints: " + json.dumps(attempt["taught_reference_joints"]), file=output)
        if "seed_joints" in attempt:
            print("  seeded_ik_seed_joints: " + json.dumps(attempt["seed_joints"]), file=output)
        if "returned_joints" in attempt:
            print("  returned_joints: " + json.dumps(attempt["returned_joints"]), file=output)
        if "wrist_deltas_degrees" in attempt:
            print("  wrist_deltas_degrees: " + json.dumps(attempt["wrist_deltas_degrees"], sort_keys=True), file=output)
        print(f"  result: {attempt.get('result', 'not completed')}", file=output)
    print(f"endpoint_kinematics_result: {overall_result}", file=output)
    print("physical path is not guaranteed by controller kinematics checks", file=output)


def run_kinematics_preflight(
    scene: R10Scene,
    output: Optional[TextIO] = None,
    dashboard_factory: Optional[Callable[[str, int], Any]] = None,
    lease_factory: Optional[Callable[[], Any]] = None,
    to_pour_ready: bool = False,
) -> dict:
    """Run seeded controller IK/FK queries using only the physical right dashboard."""
    output = sys.stdout if output is None else output
    if lease_factory is None:
        lease_factory = lambda: _make_r10_device_lease("kinematics-check")

    lease = lease_factory()
    lease.acquire()
    adapter = None
    primary_error = None
    try:
        adapter = _create_read_only_preflight_adapter(dashboard_factory)
        graph = build_r10_graph(scene, to_pour_ready=to_pour_ready)
        node = next(node for node in graph.nodes if node.operation == "validate_kinematics")
        try:
            evidence = adapter.execute_atomic(node, scene.execution_scene())
        except BaseException as error:
            _print_kinematics_report(
                output, adapter._kinematics_progress,
                f"FAIL: {type(error).__name__}: {error}",
            )
            raise
        _print_kinematics_report(output, adapter._kinematics_progress, "PASS: all selected targets passed")
        return evidence
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_errors = []
        if adapter is not None:
            try:
                adapter.dashboards[R09_ARM].close()
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            lease.release()
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            target_error = primary_error or cleanup_errors[0]
            for cleanup_error in cleanup_errors:
                if primary_error is not None or cleanup_error is not target_error:
                    print(
                        f"preflight cleanup failure: {type(cleanup_error).__name__}: {cleanup_error}",
                        file=output,
                    )
                if cleanup_error is not target_error and hasattr(target_error, "add_note"):
                    target_error.add_note(
                        f"Preflight cleanup failure: {type(cleanup_error).__name__}: {cleanup_error}"
                    )
            if primary_error is None:
                raise target_error


def print_r10_preview(graph: AtomicExecutionGraph, scene: R10Scene, output: TextIO) -> None:
    to_pour_ready = any(node.parameters.get("pose_ref") == "target:POUR_READY" for node in graph.nodes)
    title = "R10 beaker right-arm grasp and pour-ready continuation" if to_pour_ready else "R10 beaker right-arm grasp"
    print(f"=== {title} ===", file=output)
    print("actual_motion_plan_status: REJECTED — controller alarm 26 on R09-to-R10; default live is quarantined", file=output)
    print(f"graph_id: {graph.graph_id}", file=output)
    print(f"instruction: {graph.source_instruction}", file=output)
    print("arm: right", file=output)
    print("user: 0  tool: 0", file=output)
    print("SpeedFactor/SpeedJ/SpeedL/AccJ/AccL: 10", file=output)
    print(f"preflight: right gripper feedback must be >= {R10_OPEN_MINIMUM}; operator must leave it empty", file=output)
    print("R09_approach_pose: " + json.dumps(_r10_pose_dict(scene.approach_pose), sort_keys=True), file=output)
    print("R10_grip_pose: " + json.dumps(_r10_pose_dict(scene.grip_pose), sort_keys=True), file=output)
    print("pose_source: retain source XYZ and Rx signs; R09/R10 use Ry +6.0079 deg and Rz -2.25 deg; pour-ready uses Ry +0.4372 deg", file=output)
    print("leveling_assumption: nominal cup/tool X is assumed table-normal; R09/R10 are approximately level with 0.5 deg axis tilt, pour-ready 0 deg; table alignment, mounted cup, and motion path are physically unverified", file=output)
    print(f"wrist_guard: raw J4/J5/J6 deltas <= {R10_WRIST_CONTINUITY_LIMIT_DEGREES:g} deg and J5 at least {R10_J5_SINGULARITY_MARGIN_DEGREES:g} deg from every 180 deg multiple", file=output)
    print("kinematics_preflight: solve and round-trip every Cartesian pose with User(0)/Tool(0) before motion", file=output)
    for node in graph.nodes:
        dependencies = ",".join(node.dependencies) if node.dependencies else "-"
        print(
            f"[{node.sequence:02d}] {node.node_id} label={node.label} arm={node.arm} "
            f"operation={node.operation} depends={dependencies} parameters={node.parameters}",
            file=output,
        )
    if to_pour_ready:
        print("pour_ready_pose: " + json.dumps(_r10_pose_dict(scene.pour_ready_pose), sort_keys=True), file=output)
        print("motion_note: preflighted InverseSolution JointMovJ to R09 with joint and Cartesian feedback, then linear MovL to R10, lift back to R09, and linear MovL to the Cartesian pour-ready target", file=output)
    else:
        print("motion_note: preflighted InverseSolution JointMovJ to R09 with joint and Cartesian feedback, then linear MovL to R10", file=output)
    print("gripper_note: close at speed=10 after R10 feedback converges; keep the close command active; no force control or auto-open", file=output)
    if to_pour_ready:
        print("end_state: stay at Cartesian pour-ready pose with right robot enabled and the close command active", file=output)
    else:
        print("end_state: stay at R10 with right robot enabled and the close command active after feedback stabilizes", file=output)


def run_beaker_grasp(
    scene: R10Scene,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    live: bool = False,
    input_fn: Optional[Callable[[str], str]] = None,
    output: Optional[TextIO] = None,
    adapter_factory: Optional[Callable[[tuple], Any]] = None,
    lease_factory: Optional[Callable[[], Any]] = None,
    executor_factory=None,
    to_pour_ready: bool = False,
) -> RunResult:
    if live and adapter_factory is None:
        raise BlockedCandidateError(_blocked_candidate_message())
    if lease_factory is None:
        lease_factory = lambda: _make_r10_device_lease("legacy-control")
    if adapter_factory is None:
        def adapter_factory(arms):
            if tuple(arms) != (R09_ARM,):
                raise ValueError("R10 live run only accepts the physical right arm")
            return create_live_adapter()
    return run_beaker_approach(
        scene,
        artifact_root=artifact_root,
        live=live,
        input_fn=input_fn,
        output=output,
        adapter_factory=adapter_factory,
        lease_factory=lease_factory,
        executor_factory=executor_factory if executor_factory is not None else AtomicGraphExecutor,
        graph_builder=lambda selected_scene: build_r10_graph(selected_scene, to_pour_ready=to_pour_ready),
        preview_fn=print_r10_preview,
        confirmation_prompt=(
            "Enter exact uppercase YES to execute the R10 right-arm grasp and move to the Cartesian pour-ready pose: "
            if to_pour_ready else "Enter exact uppercase YES to execute the R10 right-arm approach and grip: "
        ),
        event_prefix="r10",
    )


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview or execute the fixed R10 right-arm beaker grasp")
    parser.add_argument("--scene", type=Path, default=DEFAULT_SCENE_PATH)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument(
        "--to-pour-ready", action="store_true",
        help="after gripping, lift to R09 and move linearly to the fixed Cartesian pour-ready pose",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="live", action="store_false", help="Preview and export without hardware (default)")
    mode.add_argument(
        "--live", dest="live", action="store_true",
        help="Current R09-to-R10 candidate is quarantined; return exit 2 before prompt or hardware setup",
    )
    mode.add_argument(
        "--preflight-only", action="store_true",
        help="Acquire the rig lease and run seeded right-arm IK/FK checks without motion or gripper access",
    )
    parser.set_defaults(live=False, preflight_only=False)
    return parser


def main(argv=None) -> int:
    arguments = _build_argument_parser().parse_args(argv)
    scene = load_r10_scene(arguments.scene)
    if arguments.preflight_only:
        run_kinematics_preflight(scene, to_pour_ready=arguments.to_pour_ready)
        return 0
    try:
        result = run_beaker_grasp(
            scene, artifact_root=arguments.artifacts,
            live=arguments.live, to_pour_ready=arguments.to_pour_ready,
        )
    except BlockedCandidateError as error:
        print(f"R10 live run blocked: {error}", file=sys.stderr)
        return 2
    print(f"artifacts: {result.artifact_directory}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
