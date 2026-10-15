import json
import math
import os
import re
import time
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from task_skills.run_beaker_approach import build_r09_graph, load_r09_scene
from task_skills.layout import physical_nova_ip
from task_skills.run_beaker_grasp import (
    BlockedCandidateError,
    DEFAULT_SCENE_PATH,
    R10_POUR_READY_GRAPH_ID,
    R10DobotHardwareAdapter,
    R10_GRIPPER_ACTION,
    R10_GRIPPER_SPEED,
    R10_J5_SINGULARITY_MARGIN_DEGREES,
    _build_argument_parser,
    _j5_distance_to_singularity,
    _make_r10_device_lease,
    build_r10_graph,
    load_r10_scene,
    main as grasp_main,
    run_kinematics_preflight,
    run_beaker_grasp,
    _orientation_distance_degrees,
    _rotation_matrix_rpy,
)


SOURCE_R09 = {
    "x": -117.5765, "y": -344.8222, "z": 171.6492,
    "rx": -175.763, "ry": 83.4921, "rz": 172.5408,
}
SOURCE_R10 = {
    "x": -117.5764, "y": -344.8220, "z": 93.5212,
    "rx": -175.7637, "ry": 83.4921, "rz": 172.5401,
}
SOURCE_POUR_READY = {
    "x": -117.5765, "y": -344.8222, "z": 415.5852,
    "rx": 176.3784, "ry": 89.5628, "rz": 86.3587,
}
EXPECTED_R09 = {
    "x": -117.5765, "y": -344.8222, "z": 171.6492,
    "rx": -175.763, "ry": 89.5, "rz": 170.2908,
}
EXPECTED_R10 = {
    "x": -117.5764, "y": -344.8220, "z": 93.5212,
    "rx": -175.7637, "ry": 89.5, "rz": 170.2901,
}
EXPECTED_POUR_READY = {
    "x": -117.5765, "y": -344.8222, "z": 415.5852,
    "rx": 176.3784, "ry": 90.0, "rz": 86.3587,
}
IK_R09 = [-103.1001, -42.2002, -119.3003, 51.4004, 172.5005, -19.6006]
IK_R10 = [-104.7001, -59.8002, -116.9003, 66.1004, 172.2005, -20.3006]
IK_POUR_READY = [-118.1001, -31.2002, -102.3003, 142.7309, 90.0192, 90.0595]
INITIAL_JOINTS = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
TAUGHT_JOINT_REFERENCES = {
    "R09": [-104.0774, -40.8842, -120.9522, 52.4488, 173.1281, -20.0117],
    "R10": [-104.073, -58.9861, -117.6177, 67.2813, 173.1378, -19.9461],
    "pour_ready": [-90.0111, -25.5603, -117.5143, 142.7309, 90.0192, 90.0595],
}


def _controller_reply(values, status=0):
    return f"{status},{{{','.join(str(value) for value in values)}}},Fake();"


def _alias_euler(rpy):
    # Equivalent Z-Y-X Euler angles: (r + 180, 180 - p, y + 180).
    return (rpy[0] + 180.0, 180.0 - rpy[1], rpy[2] + 180.0)


class FakeDashboard:
    def __init__(
        self, inverse_reply=None, positive_reply=None, ik_solutions=None,
        positive_pose_overrides=None,
    ):
        self.calls = []
        self.mode = 5
        self.pose = [EXPECTED_R09[key] for key in ("x", "y", "z", "rx", "ry", "rz")]
        self.joints = list(INITIAL_JOINTS)
        self.closed = False
        self.inverse_reply = inverse_reply
        self.positive_reply = positive_reply
        self.ik_solutions = dict(ik_solutions or {
            "R09": IK_R09, "R10": IK_R10, "pour_ready": IK_POUR_READY,
        })
        self.positive_pose_overrides = dict(positive_pose_overrides or {})
        self.pose_by_joints = {}
        self.last_solution_name = None
        self.raw_commands = []
        self.seed_requests = []

    @staticmethod
    def _pose_name(pose):
        for name, target in (
            ("R09", EXPECTED_R09), ("R10", EXPECTED_R10), ("pour_ready", EXPECTED_POUR_READY),
        ):
            if tuple(pose) == tuple(target[key] for key in ("x", "y", "z", "rx", "ry", "rz")):
                return name
        raise AssertionError(f"Unexpected inverse-kinematics target: {pose}")

    def _command(self, name, *args):
        self.calls.append((name, args))
        if name == "DisableRobot":
            self.mode = 4
        return f"0,{{}},{name}();"

    def RobotMode(self):
        return f"0,{{{self.mode}}},RobotMode();"

    def GetErrorID(self):
        return "0,{0},GetErrorID();"

    def GetPose(self):
        return _controller_reply(self.pose)

    def GetAngle(self):
        return _controller_reply(self.joints)

    def InverseSolution(self, *arguments):
        self.calls.append(("InverseSolution", arguments))
        self.last_solution_name = self._pose_name(arguments[:6])
        if self.inverse_reply is not None:
            return self.inverse_reply
        joints = self.ik_solutions[self.last_solution_name]
        self.pose_by_joints[tuple(joints)] = list(arguments[:6])
        return _controller_reply(joints)

    def sendRecvMsg(self, command):
        self.raw_commands.append(command)
        self.calls.append(("sendRecvMsg", (command,)))
        match = re.fullmatch(r"InverseSolution\((.*)\)", command)
        if not match:
            raise AssertionError(f"Malformed seeded IK command: {command!r}")
        arguments = []
        current = []
        in_joint_set = False
        for character in match.group(1):
            if character == "{" and not in_joint_set:
                in_joint_set = True
            elif character == "}" and in_joint_set:
                in_joint_set = False
            if character == "," and not in_joint_set:
                arguments.append("".join(current))
                current = []
            else:
                current.append(character)
        if in_joint_set:
            raise AssertionError(f"Unclosed near-joint seed in command: {command!r}")
        arguments.append("".join(current))
        if len(arguments) != 10 or arguments[8] != "1":
            raise AssertionError(f"Seeded IK command must have 10 arguments and literal option 1: {command!r}")
        if not (arguments[9].startswith("{") and arguments[9].endswith("}")):
            raise AssertionError(f"Seeded IK command must use a brace-delimited joint seed: {command!r}")
        try:
            scalar_values = [float(value) for value in arguments[:8]]
            seed_joints = [float(value) for value in arguments[9][1:-1].split(",")]
        except ValueError as error:
            raise AssertionError(f"Seeded IK command contains malformed numeric arguments: {command!r}") from error
        if not all(math.isfinite(value) for value in scalar_values + seed_joints):
            raise AssertionError(f"Seeded IK command contains non-finite values: {command!r}")
        if len(seed_joints) != 6 or scalar_values[6:] != [0.0, 0.0]:
            raise AssertionError(f"Seeded IK command has the wrong seed or User/Tool values: {command!r}")
        self.seed_requests.append({
            "pose": scalar_values[:6], "user": int(scalar_values[6]), "tool": int(scalar_values[7]),
            "option": arguments[8], "seed_joints": seed_joints,
        })
        return self.InverseSolution(*scalar_values[:8], 1, seed_joints)

    def PositiveSolution(self, *arguments):
        self.calls.append(("PositiveSolution", arguments))
        if self.positive_reply is not None:
            return self.positive_reply
        joints = tuple(arguments[:6])
        target = self.pose_by_joints[joints]
        target = self.positive_pose_overrides.get(self.last_solution_name, target)
        return _controller_reply(target)

    def __getattr__(self, name):
        if name in {
            "ClearError", "EnableRobot", "SpeedFactor", "AccJ", "SpeedJ",
            "AccL", "SpeedL", "Tool", "User", "StopScript", "DisableRobot",
        }:
            return lambda *args: self._command(name, *args)
        raise AttributeError(name)

    def close(self):
        self.closed = True


class FakeMotion:
    def __init__(
        self, dashboard, update_linear=True, update_joint=True,
        failed_linear_calls=(), alias_euler_feedback=False,
        joint_reply="0,{},JointMovJ();", linear_reply="0,{},MovL();",
    ):
        self.dashboard = dashboard
        self.update_linear = update_linear
        self.update_joint = update_joint
        self.failed_linear_calls = set(failed_linear_calls)
        self.alias_euler_feedback = alias_euler_feedback
        self.joint_reply = joint_reply
        self.linear_reply = linear_reply
        self.linear_call_count = 0
        self.calls = []
        self.closed = False

    def JointMovJ(self, *joints):
        self.calls.append(("JointMovJ", joints))
        if self.update_joint:
            self.dashboard.joints = list(joints)
            self.dashboard.pose = list(self.dashboard.pose_by_joints.get(tuple(joints), self.dashboard.pose))
        return self.joint_reply

    def MovL(self, *pose):
        self.calls.append(("MovL", pose))
        self.linear_call_count += 1
        if self.linear_call_count in self.failed_linear_calls:
            raise RuntimeError("simulated linear motion failure")
        if self.update_linear:
            self.dashboard.pose = list(pose)
            if self.alias_euler_feedback:
                self.dashboard.pose[3:] = _alias_euler(self.dashboard.pose[3:])
        return self.linear_reply

    def Sync(self):
        raise AssertionError("R10 motion completion must not call blocking Sync()")

    def close(self):
        self.closed = True


class FakeGripper:
    def __init__(self, position=255, reply=(0, 0), update_position=True, position_after_move=None):
        self.position = position
        self.reply = reply
        self.update_position = update_position
        self.position_after_move = position_after_move
        self.calls = []
        self.disconnected = False

    def get_current_position(self):
        return self.position

    def move(self, position, speed, force):
        self.calls.append((position, speed, force))
        if self.update_position:
            self.position = position if self.position_after_move is None else self.position_after_move
        return self.reply

    def disconnect(self):
        self.disconnected = True


class FakeLease:
    def __init__(self):
        self.acquired = False
        self.released = False

    def acquire(self):
        self.acquired = True
        return self

    def release(self):
        self.released = True


def make_adapter(
    position=255, reply=(0, 0), update_position=True, update_linear=True,
    update_joint=True, failed_linear_calls=(), timeout=0.2,
    position_after_move=None, inverse_reply=None, positive_reply=None,
    ik_solutions=None, positive_pose_overrides=None, alias_euler_feedback=False,
    joint_reply="0,{},JointMovJ();", linear_reply="0,{},MovL();", missing_seeded_ik=False,
):
    dashboard = FakeDashboard(
        inverse_reply=inverse_reply, positive_reply=positive_reply,
        ik_solutions=ik_solutions, positive_pose_overrides=positive_pose_overrides,
    )
    if missing_seeded_ik:
        dashboard.sendRecvMsg = None
    motion = FakeMotion(
        dashboard, update_linear=update_linear, update_joint=update_joint,
        failed_linear_calls=failed_linear_calls, alias_euler_feedback=alias_euler_feedback,
        joint_reply=joint_reply, linear_reply=linear_reply,
    )
    gripper = FakeGripper(
        position=position, reply=reply, update_position=update_position,
        position_after_move=position_after_move,
    )
    adapter = R10DobotHardwareAdapter.from_components(
        dashboards={"right": dashboard}, motions={"right": motion}, grippers={"right": gripper},
        settle_timeout=timeout,
    )
    return adapter, dashboard, motion, gripper


class BeakerGraspTests(unittest.TestCase):
    def test_injected_adapter_live_enters_preflight_and_rejects_bad_wrist_before_motion(self):
        with TemporaryDirectory() as temporary:
            rejected_r09 = list(IK_R09)
            rejected_r09[3] = TAUGHT_JOINT_REFERENCES["R09"][3] + 15.01
            adapter, dashboard, motion, gripper = make_adapter(ik_solutions={
                "R09": rejected_r09, "R10": IK_R10, "pour_ready": IK_POUR_READY,
            })
            lease = FakeLease()
            with patch(
                "task_skills.run_beaker_grasp.create_live_adapter", return_value=adapter,
            ) as hardware:
                with self.assertRaisesRegex(RuntimeError, "wrist continuity veto"):
                    run_beaker_grasp(
                        load_r10_scene(), artifact_root=Path(temporary), live=True,
                        input_fn=lambda _: "YES", lease_factory=lambda: lease, output=StringIO(),
                        adapter_factory=lambda _: adapter,
                    )
            hardware.assert_not_called()
            self.assertTrue(lease.acquired and lease.released)
            self.assertEqual(dashboard.seed_requests[0]["pose"], [EXPECTED_R09[key] for key in ("x", "y", "z", "rx", "ry", "rz")])
            self.assertEqual(motion.calls, [])
            # Failure cleanup holds the measured open position; it never issues the close target.
            self.assertEqual(gripper.calls, [(255, 10, 0)])

    def test_live_pour_ready_wrist_veto_blocks_all_motion_and_close(self):
        rejected_ready = list(IK_POUR_READY)
        rejected_ready[3] = TAUGHT_JOINT_REFERENCES["pour_ready"][3] + 15.01
        adapter, dashboard, motion, gripper = make_adapter(ik_solutions={
            "R09": IK_R09, "R10": IK_R10, "pour_ready": rejected_ready,
        })
        lease = FakeLease()
        with self.assertRaisesRegex(RuntimeError, "pour_ready wrist continuity veto"):
            run_beaker_grasp(
                load_r10_scene(), live=True, to_pour_ready=True,
                input_fn=lambda _: "YES", output=StringIO(), lease_factory=lambda: lease,
                adapter_factory=lambda _: adapter,
            )
        self.assertTrue(lease.acquired and lease.released)
        self.assertEqual([request["pose"] for request in dashboard.seed_requests], [
            [EXPECTED_R09[key] for key in ("x", "y", "z", "rx", "ry", "rz")],
            [EXPECTED_R10[key] for key in ("x", "y", "z", "rx", "ry", "rz")],
            [EXPECTED_POUR_READY[key] for key in ("x", "y", "z", "rx", "ry", "rz")],
        ])
        self.assertEqual(motion.calls, [])
        self.assertEqual(gripper.calls, [(255, 10, 0)])

    def test_live_pour_ready_fk_failure_blocks_all_motion_and_close(self):
        wrong_ready_pose = [EXPECTED_POUR_READY[key] for key in ("x", "y", "z", "rx", "ry", "rz")]
        wrong_ready_pose[0] += 5.0
        adapter, dashboard, motion, gripper = make_adapter(positive_pose_overrides={
            "pour_ready": wrong_ready_pose,
        })
        lease = FakeLease()
        with self.assertRaisesRegex(RuntimeError, "pour_ready PositiveSolution round trip does not match"):
            run_beaker_grasp(
                load_r10_scene(), live=True, to_pour_ready=True,
                input_fn=lambda _: "YES", output=StringIO(), lease_factory=lambda: lease,
                adapter_factory=lambda _: adapter,
            )
        self.assertTrue(lease.acquired and lease.released)
        self.assertEqual(len(dashboard.seed_requests), 3)
        self.assertEqual(motion.calls, [])
        self.assertEqual(gripper.calls, [(255, 10, 0)])

    def test_scene_and_grasp_graph_pin_leveled_cartesian_targets_and_order(self):
        scene = load_r10_scene(DEFAULT_SCENE_PATH)
        self.assertEqual((scene.arm, scene.user_number, scene.tool_number, scene.speed_percent), ("right", 0, 0, 10))
        self.assertEqual(scene.gripper_action, R10_GRIPPER_ACTION)
        self.assertEqual(scene.gripper_speed, R10_GRIPPER_SPEED)
        self.assertEqual(
            [scene.approach_pose.x, scene.approach_pose.y, scene.approach_pose.z,
             scene.approach_pose.rx, scene.approach_pose.ry, scene.approach_pose.rz],
            [EXPECTED_R09[key] for key in ("x", "y", "z", "rx", "ry", "rz")],
        )
        self.assertEqual(
            [scene.grip_pose.x, scene.grip_pose.y, scene.grip_pose.z,
             scene.grip_pose.rx, scene.grip_pose.ry, scene.grip_pose.rz],
            [EXPECTED_R10[key] for key in ("x", "y", "z", "rx", "ry", "rz")],
        )
        self.assertEqual(
            [scene.pour_ready_pose.x, scene.pour_ready_pose.y, scene.pour_ready_pose.z,
             scene.pour_ready_pose.rx, scene.pour_ready_pose.ry, scene.pour_ready_pose.rz],
            [EXPECTED_POUR_READY[key] for key in ("x", "y", "z", "rx", "ry", "rz")],
        )

        graph = build_r10_graph(scene)
        self.assertEqual([node.operation for node in graph.nodes], [
            "check_gripper_open", "wait_controller_ready", "clear_error", "enable_robot", "configure_robot",
            "validate_kinematics", "move_pose", "move_pose", "close_gripper",
        ])
        self.assertEqual({node.arm for node in graph.nodes}, {"right"})
        validation, approach, grip, command = graph.nodes[5:]
        self.assertEqual(validation.parameters, {
            "poses": {"R09": EXPECTED_R09, "R10": EXPECTED_R10},
            "to_pour_ready": False,
        })
        self.assertEqual(approach.parameters, {
            "pose_ref": "object:B", "motion": "inverse_solution_joint",
            "kinematic_ref": "R09", "calibrated_pose": EXPECTED_R09,
        })
        self.assertEqual(grip.parameters, {
            "pose_ref": "target:R10", "motion": "linear", "calibrated_pose": EXPECTED_R10,
        })
        self.assertEqual(command.parameters, {"speed_percent": 10})
        self.assertEqual(approach.dependencies, [validation.node_id])
        self.assertEqual(grip.dependencies, [approach.node_id])
        self.assertEqual(command.dependencies, [grip.node_id])
        self.assertFalse(any(node.operation == "open_gripper" or "home" in node.label for node in graph.nodes))

    def test_measured_level_candidates_preserve_xyz_rx_signs_and_apply_linked_ry_rz_adjustment(self):
        sources = (SOURCE_R09, SOURCE_R10, SOURCE_POUR_READY)
        targets = (EXPECTED_R09, EXPECTED_R10, EXPECTED_POUR_READY)
        scene = load_r10_scene()
        loaded_targets = (
            {key: getattr(scene.approach_pose, key) for key in EXPECTED_R09},
            {key: getattr(scene.grip_pose, key) for key in EXPECTED_R10},
            {key: getattr(scene.pour_ready_pose, key) for key in EXPECTED_POUR_READY},
        )
        self.assertEqual(loaded_targets, targets)
        for name, source, target, expected_ry_delta, expected_rz_delta, expected_axis_tilt, expected_rotation_delta in zip(
            ("R09", "R10", "pour-ready"), sources, targets,
            (6.0079, 6.0079, 0.4372), (-2.25, -2.25, 0.0), (0.5, 0.5, 0.0),
            (6.4150388052, 6.4150388052, 0.4372),
        ):
            with self.subTest(pose=name):
                self.assertEqual(
                    [target[key] for key in ("x", "y", "z", "rx")],
                    [source[key] for key in ("x", "y", "z", "rx")],
                )
                self.assertAlmostEqual(target["ry"] - source["ry"], expected_ry_delta, places=9)
                self.assertAlmostEqual(target["rz"] - source["rz"], expected_rz_delta, places=9)
                self.assertAlmostEqual(
                    _orientation_distance_degrees(
                        tuple(source[key] for key in ("rx", "ry", "rz")),
                        tuple(target[key] for key in ("rx", "ry", "rz")),
                    ),
                    expected_rotation_delta,
                    places=6,
                )

                rotation = _rotation_matrix_rpy(*(target[key] for key in ("rx", "ry", "rz")))
                tool_x_world = (rotation[0][0], rotation[1][0], rotation[2][0])
                axis_tilt = math.degrees(math.acos(max(-1.0, min(1.0, -tool_x_world[2]))))
                self.assertAlmostEqual(axis_tilt, expected_axis_tilt, places=6)

    def test_measured_near_level_candidate_matches_read_only_controller_record(self):
        # Grasp values are from near_level_query_20261015.json; ready is from wrist_query_20261015.json.
        recorded_controller_results = {
            "R09": {
                "pose": EXPECTED_R09,
                "joints": (-104.179637, -42.818591, -121.760788, 55.313371, 179.483239, -19.304569),
                "wrist_deltas": (2.864571, 6.355139, 0.707131),
            },
            "R10": {
                "pose": EXPECTED_R10,
                "joints": (-104.175362, -61.523985, -117.631003, 70.823551, 179.494344, -18.369947),
                "wrist_deltas": (3.542251, 6.356544, 1.576153),
            },
            "pour_ready": {
                "pose": EXPECTED_POUR_READY,
                "joints": (-90.011155, -25.810115, -117.325185, 143.227916, 90.018996, 90.032173),
                "wrist_deltas": (0.497016, -0.000204, -0.027327),
            },
        }
        for name, record in recorded_controller_results.items():
            expected_reference = TAUGHT_JOINT_REFERENCES[name]
            expected_pose = {
                "R09": EXPECTED_R09, "R10": EXPECTED_R10, "pour_ready": EXPECTED_POUR_READY,
            }[name]
            expected_deltas = record["wrist_deltas"]
            joints = record["joints"]
            with self.subTest(target=name):
                self.assertEqual(record["pose"], expected_pose)
                actual_deltas = tuple(round(joints[index] - expected_reference[index], 6) for index in (3, 4, 5))
                self.assertEqual(actual_deltas, expected_deltas)
                self.assertTrue(all(abs(delta) <= 15.0 for delta in actual_deltas))
                self.assertGreaterEqual(
                    _j5_distance_to_singularity(joints[4]),
                    R10_J5_SINGULARITY_MARGIN_DEGREES,
                )

    def test_j5_singularity_distance_handles_zero_signed_turns_and_full_turns(self):
        for angle, expected_distance in (
            (0.0, 0.0), (0.1, 0.1), (179.9, 0.1), (-179.9, 0.1),
            (180.0, 0.0), (-180.0, 0.0), (360.0, 0.0), (-360.0, 0.0),
            (360.1, 0.1), (-360.1, 0.1), (90.0, 90.0),
        ):
            with self.subTest(angle=angle):
                self.assertAlmostEqual(_j5_distance_to_singularity(angle), expected_distance, places=9)

    def test_j5_singularity_guard_rejects_before_fk_or_motion(self):
        near_singularity = list(IK_R09)
        near_singularity[4] = 179.9
        adapter, dashboard, motion, _ = make_adapter(ik_solutions={
            "R09": near_singularity, "R10": IK_R10, "pour_ready": IK_POUR_READY,
        })
        scene = load_r10_scene()
        with self.assertRaisesRegex(RuntimeError, "R10 R09 J5 singularity margin veto"):
            adapter.execute_atomic(build_r10_graph(scene).nodes[5], scene.execution_scene())
        self.assertEqual([name for name, _ in dashboard.calls if name == "PositiveSolution"], [])
        self.assertEqual(motion.calls, [])
        self.assertEqual(adapter._ik_solutions, {})

    def test_j5_singularity_guard_accepts_exact_required_margin(self):
        at_margin = list(IK_R09)
        at_margin[4] = 179.75
        adapter, _, motion, _ = make_adapter(ik_solutions={
            "R09": at_margin, "R10": IK_R10, "pour_ready": IK_POUR_READY,
        })
        scene = load_r10_scene()
        evidence = adapter.execute_atomic(build_r10_graph(scene).nodes[5], scene.execution_scene())
        self.assertEqual(evidence["wrist_continuity"]["R09"]["j5_singularity_distance_degrees"], 0.25)
        self.assertEqual(motion.calls, [])

    def test_live_j5_singularity_veto_blocks_approach_and_gripper_close(self):
        near_singularity = list(IK_R09)
        near_singularity[4] = 179.9
        adapter, _, motion, gripper = make_adapter(ik_solutions={
            "R09": near_singularity, "R10": IK_R10, "pour_ready": IK_POUR_READY,
        })
        lease = FakeLease()
        with self.assertRaisesRegex(RuntimeError, "R10 R09 J5 singularity margin veto"):
            run_beaker_grasp(
                load_r10_scene(), live=True, input_fn=lambda _: "YES", output=StringIO(),
                adapter_factory=lambda _: adapter, lease_factory=lambda: lease,
            )
        self.assertTrue(lease.acquired and lease.released)
        self.assertEqual(motion.calls, [])
        self.assertFalse(any(target == 0 for target, _, _ in gripper.calls))

    def test_pour_ready_graph_preflights_and_moves_linearly_to_cartesian_target(self):
        scene = load_r10_scene()
        graph = build_r10_graph(scene, to_pour_ready=True)
        self.assertEqual(graph.graph_id, R10_POUR_READY_GRAPH_ID)
        self.assertEqual([node.operation for node in graph.nodes], [
            "check_gripper_open", "wait_controller_ready", "clear_error", "enable_robot", "configure_robot",
            "validate_kinematics", "move_pose", "move_pose", "close_gripper", "move_pose", "move_pose",
        ])
        validation, approach, grip, close, lift, ready = graph.nodes[5:]
        self.assertEqual(validation.parameters, {
            "poses": {
                "R09": EXPECTED_R09, "R10": EXPECTED_R10, "pour_ready": EXPECTED_POUR_READY,
            },
            "to_pour_ready": True,
        })
        self.assertEqual(lift.dependencies, [close.node_id])
        self.assertEqual(lift.parameters, {
            "pose_ref": "object:B", "motion": "linear", "calibrated_pose": EXPECTED_R09,
        })
        self.assertEqual(ready.dependencies, [lift.node_id])
        self.assertEqual(ready.parameters, {
            "pose_ref": "target:POUR_READY", "motion": "linear", "calibrated_pose": EXPECTED_POUR_READY,
        })
        self.assertIn("linear", graph.source_instruction)
        self.assertFalse(any(node.operation in {"open_gripper", "move_pour_ready"} for node in graph.nodes))

    def test_demo_graph_exports_match_the_current_graph_builders(self):
        scene = load_r10_scene()
        for filename, to_pour_ready in (
            ("beaker_r10_graph.json", False),
            ("beaker_pour_ready_graph.json", True),
        ):
            with self.subTest(filename=filename):
                exported = json.loads((DEFAULT_SCENE_PATH.parent / filename).read_text(encoding="utf-8"))
                rebuilt = build_r10_graph(scene, to_pour_ready=to_pour_ready).to_dict()
                exported["created_at"] = rebuilt["created_at"]
                self.assertEqual(exported, rebuilt)

    def test_legacy_r09_graph_remains_the_calibrated_joint_approach(self):
        graph = build_r09_graph(load_r09_scene())
        self.assertEqual(graph.graph_id, "beaker-r09-right-approach")
        self.assertEqual([node.operation for node in graph.nodes], [
            "wait_controller_ready", "clear_error", "enable_robot", "configure_robot", "move_pose",
        ])
        self.assertEqual(graph.nodes[-1].parameters["motion"], "calibrated_joint")
        self.assertNotIn("validate_kinematics", [node.operation for node in graph.nodes])

    def test_scene_loader_rejects_changed_or_nonfinite_targets(self):
        original = json.loads(DEFAULT_SCENE_PATH.read_text(encoding="utf-8"))
        for pose_name in ("r09_pose", "r10_pose", "pour_ready_pose"):
            with self.subTest(pose_name=pose_name), TemporaryDirectory() as temporary:
                changed = json.loads(json.dumps(original))
                changed[pose_name]["z"] += 1
                path = Path(temporary) / "scene.json"
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "validated measured near-level Cartesian pose"):
                    load_r10_scene(path)
        with TemporaryDirectory() as temporary:
            changed = json.loads(json.dumps(original))
            changed["r09_pose"]["rx"] = float("nan")
            path = Path(temporary) / "scene.json"
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Non-finite JSON"):
                load_r10_scene(path)

    def test_ik_fk_preflight_rejects_malformed_nonfinite_and_mismatched_replies_before_motion(self):
        failures = (
            ("inverse_reply", "-1,{1,2,3,4,5,6},Fake();", "nonzero status"),
            ("inverse_reply", "0,{1,2,3,4,5},Fake();", "exactly six values"),
            ("inverse_reply", "0,{1,2,not-a-number,4,5,6},Fake();", "non-numeric"),
            ("inverse_reply", "0,{1,2,nan,4,5,6},Fake();", "non-finite"),
            ("positive_reply", "-1,{1,2,3,4,5,6},Fake();", "nonzero status"),
            ("positive_reply", "0,{1,2,3,4,5},Fake();", "exactly six values"),
            ("positive_reply", "0,{1,2,not-a-number,4,5,6},Fake();", "non-numeric"),
            ("positive_reply", "not-a-controller-reply", "status and six-value payload"),
            ("positive_reply", "0,{1,2,3,4,5,inf},Fake();", "non-finite"),
            ("positive_reply", "-1,{1,2,3,4,5,6},Fake();", "nonzero status"),
            ("positive_reply", _controller_reply([
                EXPECTED_R09["x"] + 5, EXPECTED_R09["y"], EXPECTED_R09["z"],
                EXPECTED_R09["rx"], EXPECTED_R09["ry"], EXPECTED_R09["rz"],
            ]), "round trip does not match"),
        )
        scene = load_r10_scene()
        for field, reply, message in failures:
            with self.subTest(field=field, reply=reply), self.assertRaisesRegex(RuntimeError, message):
                options = {field: reply}
                adapter, _, motion, _ = make_adapter(**options)
                validation_node = build_r10_graph(scene).nodes[5]
                adapter.execute_atomic(validation_node, scene.execution_scene())
                self.fail("invalid kinematics feedback was accepted")
            self.assertEqual(motion.calls, [])

    def test_ik_preflight_records_fresh_solutions_for_all_cartesian_targets(self):
        custom_r09 = [-111.0, -41.0, -121.0, 55.0, 171.0, -23.0]
        custom_solutions = {
            "R09": custom_r09, "R10": IK_R10, "pour_ready": IK_POUR_READY,
        }
        adapter, dashboard, motion, _ = make_adapter(ik_solutions=custom_solutions)
        scene = load_r10_scene()
        graph = build_r10_graph(scene, to_pour_ready=True)
        evidence = adapter.execute_atomic(graph.nodes[5], scene.execution_scene())
        self.assertEqual(evidence["solutions"], custom_solutions)
        self.assertEqual(evidence["roundtrip_verified"], ["R09", "R10", "pour_ready"])
        for name in custom_solutions:
            attempt = evidence["kinematics_attempts"][name]
            self.assertTrue(attempt["raw_inverse_reply"].startswith("0,{"))
            self.assertTrue(attempt["raw_positive_solution_reply"].startswith("0,{"))
        self.assertEqual(evidence["wrist_continuity"], {
            "R09": {
                "taught_reference_joints": TAUGHT_JOINT_REFERENCES["R09"],
                "wrist_joint_deltas_degrees": {"J4": 2.5512, "J5": -2.1281, "J6": -2.9883},
                "limit_degrees": 15.0,
                "j5_singularity_distance_degrees": 9.0,
                "j5_singularity_margin_degrees": 0.25,
            },
            "R10": {
                "taught_reference_joints": TAUGHT_JOINT_REFERENCES["R10"],
                "wrist_joint_deltas_degrees": {"J4": -1.1809, "J5": -0.9373, "J6": -0.3545},
                "limit_degrees": 15.0,
                "j5_singularity_distance_degrees": 7.7995,
                "j5_singularity_margin_degrees": 0.25,
            },
            "pour_ready": {
                "taught_reference_joints": TAUGHT_JOINT_REFERENCES["pour_ready"],
                "wrist_joint_deltas_degrees": {"J4": 0.0, "J5": 0.0, "J6": 0.0},
                "limit_degrees": 15.0,
                "j5_singularity_distance_degrees": 89.9808,
                "j5_singularity_margin_degrees": 0.25,
            },
        })
        self.assertEqual(evidence["seeded_ik"], {
            name: {
                "method": "InverseSolution(seed_near_joint=1)",
                "seed_joints": TAUGHT_JOINT_REFERENCES[name],
                "raw_command": raw_command,
            }
            for name, raw_command in zip(
                ("R09", "R10", "pour_ready"),
                (
                    "InverseSolution(-117.576500000,-344.822200000,171.649200000,-175.763000000,89.500000000,170.290800000,0,0,1,{-104.077400000,-40.884200000,-120.952200000,52.448800000,173.128100000,-20.011700000})",
                    "InverseSolution(-117.576400000,-344.822000000,93.521200000,-175.763700000,89.500000000,170.290100000,0,0,1,{-104.073000000,-58.986100000,-117.617700000,67.281300000,173.137800000,-19.946100000})",
                    "InverseSolution(-117.576500000,-344.822200000,415.585200000,176.378400000,90.000000000,86.358700000,0,0,1,{-90.011100000,-25.560300000,-117.514300000,142.730900000,90.019200000,90.059500000})",
                ),
            )
        })
        self.assertEqual([request["seed_joints"] for request in dashboard.seed_requests], [
            TAUGHT_JOINT_REFERENCES["R09"], TAUGHT_JOINT_REFERENCES["R10"],
            TAUGHT_JOINT_REFERENCES["pour_ready"],
        ])
        self.assertEqual([request["option"] for request in dashboard.seed_requests], ["1", "1", "1"])
        self.assertEqual([request["user"] for request in dashboard.seed_requests], [0, 0, 0])
        self.assertEqual([request["tool"] for request in dashboard.seed_requests], [0, 0, 0])
        self.assertTrue(evidence["kinematics_preflight_verified"])
        self.assertEqual(len([name for name, _ in dashboard.calls if name == "InverseSolution"]), 3)
        self.assertEqual(len(dashboard.raw_commands), 3)
        self.assertEqual(len([name for name, _ in dashboard.calls if name == "PositiveSolution"]), 3)
        self.assertEqual(motion.calls, [])

    def test_seeded_ik_requires_raw_dashboard_method_and_never_falls_back_to_unseeded_call(self):
        adapter, dashboard, motion, _ = make_adapter(missing_seeded_ik=True)
        scene = load_r10_scene()
        with self.assertRaisesRegex(RuntimeError, "requires dashboard.sendRecvMsg"):
            adapter.execute_atomic(build_r10_graph(scene).nodes[5], scene.execution_scene())
        self.assertEqual([name for name, _ in dashboard.calls if name == "InverseSolution"], [])
        self.assertEqual(motion.calls, [])

    def test_known_rejected_r09_wrist_solution_is_vetoed_before_positive_solution_or_motion(self):
        rejected_r09 = [-104.168291, -41.857328, -86.82728, -49.214687, 177.512036, -87.816269]
        adapter, dashboard, motion, _ = make_adapter(ik_solutions={
            "R09": rejected_r09, "R10": IK_R10, "pour_ready": IK_POUR_READY,
        })
        scene = load_r10_scene()
        validation_node = build_r10_graph(scene).nodes[5]
        with self.assertRaisesRegex(
            RuntimeError,
            r"R10 R09 wrist continuity veto at J4: difference=-101\.663487000 degrees; "
            r"reference=52\.448800000, solution=-49\.214687000",
        ):
            adapter.execute_atomic(validation_node, scene.execution_scene())
        self.assertEqual([name for name, _ in dashboard.calls if name == "InverseSolution"], ["InverseSolution"])
        self.assertEqual([name for name, _ in dashboard.calls if name == "PositiveSolution"], [])
        self.assertEqual(motion.calls, [])
        self.assertEqual(adapter._ik_solutions, {})

    def test_wrist_continuity_uses_raw_j4_j6_differences_and_rejects_full_turns(self):
        cases = ((3, 16.0, "J4"), (5, -16.0, "J6"), (3, 360.0, "J4"), (5, -360.0, "J6"))
        scene = load_r10_scene()
        for joint_index, difference, joint_name in cases:
            with self.subTest(joint=joint_name, difference=difference):
                difference_pattern = re.escape(f"{difference:+.9f}")
                r09_solution = list(TAUGHT_JOINT_REFERENCES["R09"])
                r09_solution[joint_index] += difference
                adapter, dashboard, motion, _ = make_adapter(ik_solutions={
                    "R09": r09_solution, "R10": IK_R10, "pour_ready": IK_POUR_READY,
                })
                validation_node = build_r10_graph(scene).nodes[5]
                with self.assertRaisesRegex(
                    RuntimeError,
                    rf"R10 R09 wrist continuity veto at {joint_name}: difference={difference_pattern} degrees; "
                    rf"reference=.*solution=",
                ):
                    adapter.execute_atomic(validation_node, scene.execution_scene())
                self.assertEqual([name for name, _ in dashboard.calls if name == "PositiveSolution"], [])
                self.assertEqual(motion.calls, [])

    def test_wrist_continuity_accepts_exactly_fifteen_degrees_and_records_raw_delta(self):
        r09_solution = list(TAUGHT_JOINT_REFERENCES["R09"])
        r09_solution[3] += 15.0
        adapter, _, motion, _ = make_adapter(ik_solutions={
            "R09": r09_solution, "R10": IK_R10, "pour_ready": IK_POUR_READY,
        })
        scene = load_r10_scene()
        evidence = adapter.execute_atomic(build_r10_graph(scene).nodes[5], scene.execution_scene())
        self.assertEqual(evidence["wrist_continuity"]["R09"]["wrist_joint_deltas_degrees"]["J4"], 15.0)
        self.assertEqual(evidence["wrist_continuity"]["R09"]["taught_reference_joints"], TAUGHT_JOINT_REFERENCES["R09"])
        self.assertEqual(motion.calls, [])

    def test_wrist_continuity_rejects_point_one_microdegree_above_limit(self):
        r09_solution = list(TAUGHT_JOINT_REFERENCES["R09"])
        r09_solution[3] += 15.0000001
        adapter, dashboard, motion, _ = make_adapter(ik_solutions={
            "R09": r09_solution, "R10": IK_R10, "pour_ready": IK_POUR_READY,
        })
        scene = load_r10_scene()
        with self.assertRaisesRegex(
            RuntimeError,
            r"R10 R09 wrist continuity veto at J4: difference=\+15\.000000100 degrees; "
            r"reference=52\.448800000, solution=67\.448800100",
        ):
            adapter.execute_atomic(build_r10_graph(scene).nodes[5], scene.execution_scene())
        self.assertEqual([name for name, _ in dashboard.calls if name == "PositiveSolution"], [])
        self.assertEqual(motion.calls, [])

    def test_closed_gripper_preflight_stops_before_any_arm_command(self):
        adapter, dashboard, motion, _ = make_adapter(position=128)
        graph = build_r10_graph(load_r10_scene())
        with self.assertRaisesRegex(RuntimeError, "below 250.*open an empty gripper"):
            adapter.execute_atomic(graph.nodes[0], load_r10_scene().execution_scene())
        self.assertEqual(motion.calls, [])
        self.assertFalse(any(name in {"InverseSolution", "PositiveSolution"} for name, _ in dashboard.calls))

    def test_linear_move_requires_completed_kinematics_preflight_and_converges(self):
        scene = load_r10_scene()
        adapter, _, motion, gripper = make_adapter(update_linear=False, timeout=0.01)
        graph = build_r10_graph(scene)
        grip_node = graph.nodes[7]
        grip_node.timeout_seconds = 0.01
        with self.assertRaisesRegex(RuntimeError, "requires its completed IK/FK preflight"):
            adapter.execute_atomic(grip_node, scene.execution_scene())
        adapter.execute_atomic(graph.nodes[5], scene.execution_scene())
        with self.assertRaises(TimeoutError):
            adapter.execute_atomic(grip_node, scene.execution_scene())
        self.assertEqual(motion.calls[0][0], "MovL")
        self.assertEqual(gripper.calls, [])

    def test_joint_and_linear_motion_require_zero_status_replies(self):
        scene = load_r10_scene()

        for reply in ("1,{},JointMovJ();", "-1,{},JointMovJ();", ""):
            with self.subTest(command="JointMovJ", reply=reply):
                adapter, _, motion, _ = make_adapter(joint_reply=reply)
                graph = build_r10_graph(scene)
                adapter.execute_atomic(graph.nodes[5], scene.execution_scene())
                with self.assertRaisesRegex(RuntimeError, "inverse-solution JointMovJ.*zero-status"):
                    adapter.execute_atomic(graph.nodes[6], scene.execution_scene())
                self.assertEqual([call[0] for call in motion.calls], ["JointMovJ"])

        for reply in ("1,{},MovL();", "-1,{},MovL();", ""):
            with self.subTest(command="MovL", reply=reply):
                adapter, _, motion, _ = make_adapter(linear_reply=reply)
                graph = build_r10_graph(scene)
                adapter.execute_atomic(graph.nodes[5], scene.execution_scene())
                adapter.execute_atomic(graph.nodes[6], scene.execution_scene())
                with self.assertRaisesRegex(RuntimeError, "MovL.*zero-status"):
                    adapter.execute_atomic(graph.nodes[7], scene.execution_scene())
                self.assertEqual([call[0] for call in motion.calls], ["JointMovJ", "MovL"])

    def test_mode_5_does_not_release_approach_without_joint_and_cartesian_feedback(self):
        scene = load_r10_scene()
        adapter, dashboard, motion, _ = make_adapter(update_joint=False, timeout=0.01)
        graph = build_r10_graph(scene)
        adapter.execute_atomic(graph.nodes[5], scene.execution_scene())
        graph.nodes[6].timeout_seconds = 0.01
        with self.assertRaises(TimeoutError):
            adapter.execute_atomic(graph.nodes[6], scene.execution_scene())
        self.assertEqual(dashboard.mode, 5)
        self.assertEqual([call[0] for call in motion.calls], ["JointMovJ"])

    def test_linear_pose_timeout_uses_adapter_settle_timeout_when_node_timeout_is_none(self):
        scene = load_r10_scene()
        adapter, _, _, _ = make_adapter(update_linear=False, timeout=0.01)
        graph = build_r10_graph(scene)
        adapter.execute_atomic(graph.nodes[5], scene.execution_scene())
        graph.nodes[7].timeout_seconds = None
        with self.assertRaises(TimeoutError):
            adapter.execute_atomic(graph.nodes[7], scene.execution_scene())

    def test_cartesian_feedback_accepts_equivalent_euler_rotation(self):
        with TemporaryDirectory() as temporary:
            adapter, dashboard, motion, _ = make_adapter(alias_euler_feedback=True)
            result = run_beaker_grasp(
                load_r10_scene(), artifact_root=Path(temporary), live=True,
                input_fn=lambda _: "YES", output=StringIO(),
                adapter_factory=lambda _: adapter, lease_factory=FakeLease,
            )
            self.assertEqual(result.graph.status, "succeeded")
            r10_target = tuple(EXPECTED_R10[key] for key in ("x", "y", "z", "rx", "ry", "rz"))
            self.assertEqual(motion.calls[1][0], "MovL")
            self.assertEqual(tuple(motion.calls[1][1]), r10_target)
            self.assertAlmostEqual(dashboard.pose[0], EXPECTED_R10["x"])
            self.assertEqual(tuple(dashboard.pose[3:]), _alias_euler(r10_target[3:]))

    def test_close_gripper_requires_both_zero_acknowledgements(self):
        for reply in ((1, 0), (0, 1)):
            with self.subTest(reply=reply):
                adapter, _, _, gripper = make_adapter(reply=reply)
                node = build_r10_graph(load_r10_scene()).nodes[-1]
                with self.assertRaisesRegex(RuntimeError, "serial result 0 and servo error 0"):
                    adapter.execute_atomic(node, load_r10_scene().execution_scene())
                self.assertEqual(gripper.calls, [(0, 10, 0)])

    def test_close_accepts_stable_feedback_after_meaningful_closing_travel(self):
        adapter, _, _, gripper = make_adapter(position_after_move=106)
        node = build_r10_graph(load_r10_scene()).nodes[-1]
        started = time.monotonic()
        evidence = adapter.execute_atomic(node, load_r10_scene().execution_scene())
        elapsed = time.monotonic() - started
        self.assertEqual(gripper.calls, [(0, 10, 0)])
        self.assertLess(elapsed, 2.0)
        self.assertEqual(evidence["gripper_position"], 106)
        self.assertEqual(evidence["gripper_travel"], 149)
        self.assertEqual(evidence["gripper_stable_samples"], 5)
        self.assertEqual(evidence["completion_mode"], "closed_or_stable_after_closing")
        self.assertEqual(evidence["desired_action"], "close")
        self.assertFalse(evidence["physical_grasp_verified"])

    def test_fully_closed_feedback_is_accepted(self):
        adapter, _, _, gripper = make_adapter(position=0)
        node = build_r10_graph(load_r10_scene()).nodes[-1]
        evidence = adapter.execute_atomic(node, load_r10_scene().execution_scene())
        self.assertEqual(gripper.calls, [(0, 10, 0)])
        self.assertEqual(evidence["gripper_position"], 0)
        self.assertEqual(evidence["gripper_target"], "closed")

    def test_unchanged_open_feedback_times_out_and_never_reopens(self):
        adapter, _, _, gripper = make_adapter(update_position=False, timeout=0.01)
        node = build_r10_graph(load_r10_scene()).nodes[-1]
        node.timeout_seconds = 0.01
        with self.assertRaisesRegex(TimeoutError, "did not close and stabilize"):
            adapter.execute_atomic(node, load_r10_scene().execution_scene())
        self.assertEqual(gripper.calls, [(0, 10, 0)])

    def test_stop_verification_holds_at_speed_10_and_rejects_bad_acknowledgement(self):
        adapter, _, _, gripper = make_adapter(reply=(0, 1))
        result = adapter.stop_and_verify()
        self.assertFalse(result["confirmed"])
        self.assertIn("gripper_hold:RuntimeError", result["arms"]["right"]["errors"])
        self.assertEqual(gripper.calls, [(255, 10, 0)])

    def test_failed_live_run_keeps_lease_when_recovery_hold_ack_is_bad(self):
        with TemporaryDirectory() as temporary:
            adapter, _, _, gripper = make_adapter(reply=(0, 1))
            lease = FakeLease()
            with self.assertRaisesRegex(RuntimeError, "serial result 0 and servo error 0"):
                run_beaker_grasp(
                    load_r10_scene(), artifact_root=Path(temporary), live=True,
                    input_fn=lambda _: "YES", output=StringIO(),
                    adapter_factory=lambda _: adapter, lease_factory=lambda: lease,
                )
            self.assertTrue(lease.acquired)
            self.assertFalse(lease.released)
            self.assertEqual(gripper.calls, [(0, 10, 0), (0, 10, 0)])

    def test_dry_run_and_cancel_do_not_construct_hardware_or_acquire_lease(self):
        with TemporaryDirectory() as temporary:
            constructed = []
            leased = []
            preview_output = StringIO()
            preview = run_beaker_grasp(
                load_r10_scene(), artifact_root=Path(temporary), live=False,
                output=preview_output, adapter_factory=lambda arms: constructed.append(arms),
                lease_factory=lambda: leased.append(True),
            )
            self.assertEqual(preview.exit_code, 0)
            self.assertEqual(preview.graph.status, "compiled")
            self.assertIn("R09/R10 use Ry +6.0079 deg and Rz -2.25 deg", preview_output.getvalue())
            self.assertIn("R09/R10 are approximately level with 0.5 deg axis tilt", preview_output.getvalue())
            self.assertEqual(constructed, [])
            self.assertEqual(leased, [])

        with TemporaryDirectory() as temporary:
            prompts = []
            constructed = []
            leased = []
            cancelled = run_beaker_grasp(
                load_r10_scene(), artifact_root=Path(temporary), live=True,
                input_fn=lambda prompt: prompts.append(prompt) or "NO", output=StringIO(),
                adapter_factory=lambda arms: constructed.append(arms),
                lease_factory=lambda: leased.append(True),
                to_pour_ready=True,
            )
            self.assertEqual(cancelled.exit_code, 2)
            self.assertEqual(cancelled.graph.status, "cancelled")
            self.assertIn("Cartesian pour-ready pose", prompts[0])
            self.assertEqual(constructed, [])
            self.assertEqual(leased, [])

    def test_preflight_only_queries_selected_targets_and_releases_resources(self):
        dashboard = FakeDashboard()
        lease = FakeLease()
        dashboard_connections = []
        output = StringIO()

        with patch("task_skills.run_beaker_grasp.create_live_adapter") as live_adapter:
            evidence = run_kinematics_preflight(
                load_r10_scene(), output=output, to_pour_ready=True,
                dashboard_factory=lambda host, port: dashboard_connections.append((host, port)) or dashboard,
                lease_factory=lambda: lease,
            )

        live_adapter.assert_not_called()
        self.assertTrue(lease.acquired and lease.released)
        self.assertEqual(len(dashboard_connections), 1)
        self.assertEqual(dashboard_connections[0], (physical_nova_ip("right"), 29999))
        self.assertTrue(dashboard.closed)
        self.assertEqual(set(name for name, _ in dashboard.calls), {
            "sendRecvMsg", "InverseSolution", "PositiveSolution",
        })
        self.assertEqual(evidence["roundtrip_verified"], ["R09", "R10", "pour_ready"])
        report = output.getvalue()
        self.assertLess(
            report.index("actual_motion_plan_status: REJECTED"),
            report.index("target: R09"),
        )
        self.assertIn("run_20261015_033808_302411_ur-ready_1c0d01", report)
        self.assertIn("R10 MovL raised alarm 26", report)
        self.assertIn("endpoint_kinematics_result: PASS", report)
        self.assertIn("endpoint IK/FK PASS does not clear this motion rejection", report)
        for target_name in ("R09", "R10", "pour_ready"):
            self.assertIn(target_name, report)
        self.assertIn("taught_reference_joints", report)
        self.assertIn("returned_joints", report)
        self.assertIn("wrist_deltas_degrees", report)
        self.assertIn("read-only", report)
        self.assertIn("physical path is not guaranteed", report)

    def test_preflight_failure_closes_dashboard_and_releases_lease_without_stopping(self):
        rejected_r09 = list(IK_R09)
        rejected_r09[4] -= 15.01
        dashboard = FakeDashboard(ik_solutions={
            "R09": rejected_r09, "R10": IK_R10, "pour_ready": IK_POUR_READY,
        })
        lease = FakeLease()
        output = StringIO()
        with self.assertRaisesRegex(RuntimeError, "wrist continuity veto"):
            run_kinematics_preflight(
                load_r10_scene(), output=output, dashboard_factory=lambda *_: dashboard,
                lease_factory=lambda: lease,
            )
        self.assertTrue(lease.acquired and lease.released)
        self.assertTrue(dashboard.closed)
        self.assertNotIn("StopScript", [name for name, _ in dashboard.calls])
        self.assertNotIn("EnableRobot", [name for name, _ in dashboard.calls])
        self.assertEqual(len(dashboard.seed_requests), 1)
        report = output.getvalue()
        self.assertIn("target: R09", report)
        self.assertIn("taught_reference_joints", report)
        self.assertIn("returned_joints", report)
        self.assertIn("wrist_deltas_degrees", report)
        self.assertIn("result: FAIL: wrist continuity veto", report)

    def test_preflight_cleanup_failure_releases_lease_and_preserves_query_failure(self):
        class CloseFailDashboard(FakeDashboard):
            def close(self):
                raise RuntimeError("dashboard close failure")

        rejected_r09 = list(IK_R09)
        rejected_r09[4] = TAUGHT_JOINT_REFERENCES["R09"][4] - 15.01
        dashboard = CloseFailDashboard(ik_solutions={
            "R09": rejected_r09, "R10": IK_R10, "pour_ready": IK_POUR_READY,
        })
        lease = FakeLease()
        output = StringIO()
        with self.assertRaisesRegex(RuntimeError, "wrist continuity veto"):
            run_kinematics_preflight(
                load_r10_scene(), output=output, dashboard_factory=lambda *_: dashboard,
                lease_factory=lambda: lease,
            )
        self.assertTrue(lease.acquired and lease.released)
        self.assertIn("dashboard close failure", output.getvalue())

    def test_preflight_locked_lease_fails_before_dashboard_connection(self):
        class LockedLease:
            released = False

            def acquire(self):
                raise RuntimeError("locked lease")

            def release(self):
                self.released = True

        lease = LockedLease()
        dashboard_factory = lambda *_: self.fail("must not connect when lease acquisition fails")
        with self.assertRaisesRegex(RuntimeError, "locked lease"):
            run_kinematics_preflight(
                load_r10_scene(), output=StringIO(), dashboard_factory=dashboard_factory,
                lease_factory=lambda: lease,
            )
        self.assertFalse(lease.released)

    def test_preflight_dashboard_connection_failure_releases_acquired_lease(self):
        lease = FakeLease()

        def fail_connection(*_):
            raise RuntimeError("dashboard connection failure")

        with self.assertRaisesRegex(RuntimeError, "dashboard connection failure"):
            run_kinematics_preflight(
                load_r10_scene(), output=StringIO(), dashboard_factory=fail_connection,
                lease_factory=lambda: lease,
            )
        self.assertTrue(lease.acquired and lease.released)

    def test_preflight_default_lease_uses_kinematics_check_purpose(self):
        dashboard = FakeDashboard()
        lease = FakeLease()
        with patch("task_skills.run_beaker_grasp.DeviceLease", return_value=lease) as device_lease:
            run_kinematics_preflight(
                load_r10_scene(), output=StringIO(), dashboard_factory=lambda *_: dashboard,
            )
        device_lease.assert_called_once_with(
            path=Path("C:/DobotRuntime/device-owner"), purpose="kinematics-check",
        )
        self.assertTrue(lease.acquired and lease.released)

    def test_r10_windows_lease_defaults_to_shared_device_owner_path(self):
        with patch.dict(os.environ, {"DOBOT_DEVICE_LOCK_PATH": ""}), patch(
            "task_skills.run_beaker_grasp.DeviceLease"
        ) as device_lease:
            _make_r10_device_lease("kinematics-check")
        device_lease.assert_called_once_with(
            path=Path("C:/DobotRuntime/device-owner"), purpose="kinematics-check",
        )

    def test_r10_windows_lease_honors_device_owner_environment_override(self):
        override = Path("D:/custom/dobot-owner")
        with patch.dict(os.environ, {"DOBOT_DEVICE_LOCK_PATH": str(override)}), patch(
            "task_skills.run_beaker_grasp.DeviceLease"
        ) as device_lease:
            _make_r10_device_lease("legacy-control")
        device_lease.assert_called_once_with(path=override, purpose="legacy-control")

    def test_live_runner_receives_shared_legacy_lease_factory_by_default(self):
        result = object()
        with patch("task_skills.run_beaker_grasp.DeviceLease") as device_lease, patch(
            "task_skills.run_beaker_grasp.run_beaker_approach", return_value=result,
        ) as approach_runner:
            actual = run_beaker_grasp(
                load_r10_scene(), live=True, input_fn=lambda _: "YES", output=StringIO(),
                adapter_factory=lambda _: object(),
            )
            lease_factory = approach_runner.call_args.kwargs["lease_factory"]
            lease_factory()
        self.assertIs(actual, result)
        device_lease.assert_called_once_with(
            path=Path("C:/DobotRuntime/device-owner"), purpose="legacy-control",
        )

    def test_preflight_only_cli_is_exclusive_with_live_and_dry_run(self):
        parser = _build_argument_parser()
        arguments = parser.parse_args(["--preflight-only", "--to-pour-ready"])
        self.assertTrue(arguments.preflight_only)
        self.assertTrue(arguments.to_pour_ready)
        with self.assertRaises(SystemExit):
            parser.parse_args(["--preflight-only", "--live"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["--preflight-only", "--dry-run"])

    def test_preflight_only_cli_creates_no_artifacts_or_live_run(self):
        with TemporaryDirectory() as temporary, patch(
            "task_skills.run_beaker_grasp.run_kinematics_preflight"
        ) as preflight, patch("task_skills.run_beaker_grasp.run_beaker_grasp") as live_run:
            result = grasp_main(["--preflight-only", "--to-pour-ready", "--artifacts", temporary])
            self.assertEqual(list(Path(temporary).iterdir()), [])
        self.assertEqual(result, 0)
        preflight.assert_called_once()
        self.assertTrue(preflight.call_args.kwargs["to_pour_ready"])
        live_run.assert_not_called()

    def test_default_real_live_candidate_block_precedes_prompt_lease_and_adapter_creation(self):
        prompts = []
        lease_factory = unittest.mock.Mock(side_effect=AssertionError("lease must not be requested"))
        adapter_creator = unittest.mock.Mock(side_effect=AssertionError("hardware must not be created"))

        with patch("task_skills.run_beaker_grasp.create_live_adapter", adapter_creator):
            with self.assertRaisesRegex(BlockedCandidateError, "R10 MovL raised alarm 26") as raised:
                run_beaker_grasp(
                    load_r10_scene(), live=True,
                    input_fn=lambda prompt: prompts.append(prompt) or "YES",
                    output=StringIO(), lease_factory=lease_factory,
                )

        self.assertIn("run_20261015_033808_302411_ur-ready_1c0d01", str(raised.exception))
        self.assertIn("--preflight-only", str(raised.exception))
        self.assertIn("--dry-run", str(raised.exception))
        self.assertEqual(prompts, [])
        lease_factory.assert_not_called()
        adapter_creator.assert_not_called()

    def test_default_real_live_cli_reports_candidate_block_with_exit_two(self):
        stderr = StringIO()
        lease_factory = unittest.mock.Mock(side_effect=AssertionError("lease must not be requested"))
        adapter_creator = unittest.mock.Mock(side_effect=AssertionError("hardware must not be created"))
        prompt = unittest.mock.Mock(side_effect=AssertionError("confirmation must not be requested"))

        with redirect_stderr(stderr), patch("builtins.input", prompt), patch(
            "task_skills.run_beaker_grasp._make_r10_device_lease", lease_factory,
        ), patch("task_skills.run_beaker_grasp.create_live_adapter", adapter_creator):
            result = grasp_main(["--live"])

        self.assertEqual(result, 2)
        self.assertIn("R10 MovL raised alarm 26", stderr.getvalue())
        self.assertIn("current R09-to-R10 candidate is quarantined", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        prompt.assert_not_called()
        lease_factory.assert_not_called()
        adapter_creator.assert_not_called()

    def test_fake_live_run_uses_fresh_r09_inverse_solution_then_linear_r10(self):
        with TemporaryDirectory() as temporary:
            fresh_r09 = [-109.111, -47.222, -118.333, 56.444, 169.555, -21.666]
            adapter, dashboard, motion, gripper = make_adapter(ik_solutions={
                "R09": fresh_r09, "R10": IK_R10, "pour_ready": IK_POUR_READY,
            })
            lease = FakeLease()
            requested_arms = []
            result = run_beaker_grasp(
                load_r10_scene(), artifact_root=Path(temporary), live=True,
                input_fn=lambda prompt: "YES", output=StringIO(),
                adapter_factory=lambda arms: requested_arms.append(arms) or adapter,
                lease_factory=lambda: lease,
            )
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.graph.status, "succeeded")
            self.assertEqual(requested_arms, [("right",)])
            self.assertTrue(lease.acquired and lease.released)
            self.assertEqual([call[0] for call in motion.calls], ["JointMovJ", "MovL"])
            self.assertEqual(motion.calls[0], ("JointMovJ", tuple(fresh_r09)))
            self.assertEqual(motion.calls[1], (
                "MovL", tuple(EXPECTED_R10[key] for key in ("x", "y", "z", "rx", "ry", "rz")),
            ))
            self.assertEqual(gripper.calls, [(0, 10, 0)])
            self.assertEqual([name for name, _ in dashboard.calls if name in {
                "SpeedFactor", "AccJ", "SpeedJ", "AccL", "SpeedL", "Tool", "User",
            }], ["SpeedFactor", "AccJ", "SpeedJ", "AccL", "SpeedL", "Tool", "User"])
            self.assertEqual(dashboard.pose[:3], [EXPECTED_R10[key] for key in ("x", "y", "z")])
            self.assertEqual(gripper.position, 0)
            self.assertFalse(any(name in {"DisableRobot", "StopScript"} for name, _ in dashboard.calls))

    def test_live_pour_ready_runs_movl_to_r10_lift_r09_and_ready_target(self):
        with TemporaryDirectory() as temporary:
            adapter, dashboard, motion, gripper = make_adapter()
            result = run_beaker_grasp(
                load_r10_scene(), artifact_root=Path(temporary), live=True,
                input_fn=lambda _: "YES", output=StringIO(),
                adapter_factory=lambda _: adapter, lease_factory=FakeLease,
                to_pour_ready=True,
            )
            self.assertEqual(result.graph.status, "succeeded")
            self.assertEqual(len(result.graph.nodes), 11)
            self.assertTrue(result.graph.nodes[5].evidence["kinematics_preflight_verified"])
            self.assertEqual(result.graph.nodes[5].evidence["roundtrip_verified"], ["R09", "R10", "pour_ready"])
            self.assertEqual([call[0] for call in motion.calls], [
                "JointMovJ", "MovL", "MovL", "MovL",
            ])
            expected_movl = [
                EXPECTED_R10, EXPECTED_R09, EXPECTED_POUR_READY,
            ]
            actual_movl = [call[1] for call in motion.calls if call[0] == "MovL"]
            self.assertEqual(actual_movl, [
                tuple(target[key] for key in ("x", "y", "z", "rx", "ry", "rz"))
                for target in expected_movl
            ])
            self.assertEqual(dashboard.pose, [EXPECTED_POUR_READY[key] for key in ("x", "y", "z", "rx", "ry", "rz")])
            self.assertEqual(gripper.calls, [(0, 10, 0)])
            self.assertFalse(any(name in {"DisableRobot", "StopScript"} for name, _ in dashboard.calls))

    def test_failed_close_blocks_lift_and_ready_motion(self):
        with TemporaryDirectory() as temporary:
            adapter, _, motion, _ = make_adapter(reply=(0, 1))
            with self.assertRaisesRegex(RuntimeError, "serial result 0 and servo error 0"):
                run_beaker_grasp(
                    load_r10_scene(), artifact_root=Path(temporary), live=True,
                    input_fn=lambda _: "YES", output=StringIO(),
                    adapter_factory=lambda _: adapter, lease_factory=FakeLease,
                    to_pour_ready=True,
                )
            self.assertEqual([call[0] for call in motion.calls], ["JointMovJ", "MovL"])

    def test_failed_lift_blocks_cartesian_pour_ready_move(self):
        with TemporaryDirectory() as temporary:
            adapter, _, motion, _ = make_adapter(failed_linear_calls={2})
            with self.assertRaisesRegex(RuntimeError, "simulated linear motion failure"):
                run_beaker_grasp(
                    load_r10_scene(), artifact_root=Path(temporary), live=True,
                    input_fn=lambda _: "YES", output=StringIO(),
                    adapter_factory=lambda _: adapter, lease_factory=FakeLease,
                    to_pour_ready=True,
                )
            self.assertEqual([call[0] for call in motion.calls], [
                "JointMovJ", "MovL", "MovL",
            ])
            self.assertEqual(sum(call[0] == "MovL" for call in motion.calls), 2)


if __name__ == "__main__":
    unittest.main()
