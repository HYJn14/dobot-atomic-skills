import json
import signal
import sys
import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import patch

from task_skills.atomic_graph import AtomicNode
from task_skills.run_beaker_approach import (
    DEFAULT_SCENE_PATH,
    R09DobotHardwareAdapter,
    _build_argument_parser,
    create_live_adapter,
    load_r09_scene,
    run_beaker_approach,
)


EXPECTED_POSE = {
    "x": -117.5765,
    "y": -344.8222,
    "z": 171.6492,
    "rx": -175.7630,
    "ry": 83.4921,
    "rz": 172.5408,
    "joints": [-104.0774, -40.8842, -120.9522, 52.4488, 173.1281, -20.0117],
}


class FakeLease:
    def __init__(self):
        self.acquired = False
        self.released = False

    def acquire(self):
        self.acquired = True
        return self

    def release(self):
        self.released = True


class FakeDashboard:
    def __init__(self, fail_user=False, blank_command=None):
        self.calls = []
        self.closed = False
        self.fail_user = fail_user
        self.blank_command = blank_command
        self.mode = 5

    def _command(self, name, *arguments):
        self.calls.append((name, arguments))
        if name == "DisableRobot":
            self.mode = 4
        if name == "User" and self.fail_user:
            return "-1,{error},User();"
        if name == self.blank_command:
            return ""
        return f"0,{{}},${name}();"

    def RobotMode(self):
        return f"0,{{{self.mode}}},RobotMode();"

    def GetErrorID(self):
        return "0,{0},GetErrorID();"

    def GetPose(self):
        return "0,{-117.5765,-344.8222,171.6492,-175.7630,83.4921,172.5408},GetPose();"

    def GetAngle(self):
        joints = ",".join(str(value) for value in EXPECTED_POSE["joints"])
        return f"0,{{{joints}}},GetAngle();"

    def ClearError(self):
        return self._command("ClearError")

    def EnableRobot(self):
        return self._command("EnableRobot")

    def SpeedFactor(self, *args):
        return self._command("SpeedFactor", *args)

    def AccJ(self, *args):
        return self._command("AccJ", *args)

    def SpeedJ(self, *args):
        return self._command("SpeedJ", *args)

    def AccL(self, *args):
        return self._command("AccL", *args)

    def SpeedL(self, *args):
        return self._command("SpeedL", *args)

    def Tool(self, *args):
        return self._command("Tool", *args)

    def User(self, *args):
        return self._command("User", *args)

    def StopScript(self):
        return self._command("StopScript")

    def DisableRobot(self):
        return self._command("DisableRobot")

    def close(self):
        self.closed = True


class FakeMotion:
    def __init__(self, dashboard):
        self.dashboard = dashboard
        self.calls = []
        self.closed = False

    def JointMovJ(self, *joints):
        configured = {name for name, _ in self.dashboard.calls}
        required = {"SpeedFactor", "AccJ", "SpeedJ", "AccL", "SpeedL", "Tool", "User"}
        if not required.issubset(configured):
            raise AssertionError("JointMovJ ran before speed, Tool(0), and User(0) configuration")
        self.calls.append(("JointMovJ", joints))
        return "0,{},JointMovJ();"

    def Sync(self):
        self.calls.append(("Sync", ()))
        return "0,{},Sync();"

    def close(self):
        self.closed = True


def make_adapter(fail_user=False, blank_command=None):
    dashboard = FakeDashboard(fail_user=fail_user, blank_command=blank_command)
    motion = FakeMotion(dashboard)
    adapter = R09DobotHardwareAdapter.from_components(
        dashboards={"right": dashboard}, motions={"right": motion}, grippers={}, settle_timeout=0.5,
    )
    return adapter, dashboard, motion


class BeakerApproachTests(unittest.TestCase):
    def test_default_scene_has_exact_right_arm_taught_pose(self):
        scene = load_r09_scene(DEFAULT_SCENE_PATH)
        pose = scene.pose

        self.assertEqual(
            (pose.x, pose.y, pose.z, pose.rx, pose.ry, pose.rz),
            tuple(EXPECTED_POSE[key] for key in ("x", "y", "z", "rx", "ry", "rz")),
        )
        self.assertEqual(list(pose.joints), EXPECTED_POSE["joints"])
        self.assertEqual((scene.arm, scene.user_number, scene.tool_number, scene.speed_percent, scene.motion),
                         ("right", 0, 0, 10, "calibrated_joint"))

    def test_dry_run_exports_only_right_setup_and_one_configured_joint_move(self):
        with TemporaryDirectory() as temporary:
            output = StringIO()
            constructed = []
            leased = []
            result = run_beaker_approach(
                load_r09_scene(),
                artifact_root=Path(temporary),
                output=output,
                adapter_factory=lambda arms: constructed.append(arms),
                lease_factory=lambda: leased.append(True),
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(constructed, [])
            self.assertEqual(leased, [])
            exported = json.loads((result.artifact_directory / "execution_graph.json").read_text(encoding="utf-8"))
            operations = [node["operation"] for node in exported["nodes"]]
            self.assertEqual(operations, ["wait_controller_ready", "clear_error", "enable_robot", "configure_robot", "move_pose"])
            self.assertEqual({node["arm"] for node in exported["nodes"]}, {"right"})
            configure, move = exported["nodes"][3:]
            self.assertEqual(configure["parameters"], {"speed_percent": 10, "tool_number": 0, "user_number": 0})
            self.assertEqual(move["dependencies"], [configure["node_id"]])
            self.assertEqual(move["parameters"]["motion"], "calibrated_joint")
            self.assertEqual(move["parameters"]["calibrated_pose"], EXPECTED_POSE)
            self.assertNotIn("home", operations)
            self.assertFalse(any("gripper" in operation for operation in operations))
            self.assertIn("graph_artifact:", output.getvalue())
            self.assertIn("SpeedFactor/SpeedJ/SpeedL/AccJ/AccL: 10", output.getvalue())

    def test_non_yes_response_creates_no_adapter_or_lease(self):
        with TemporaryDirectory() as temporary:
            constructed = []
            leased = []
            result = run_beaker_approach(
                load_r09_scene(),
                artifact_root=Path(temporary),
                live=True,
                input_fn=lambda _: "yes",
                output=StringIO(),
                adapter_factory=lambda arms: constructed.append(arms),
                lease_factory=lambda: leased.append(True),
            )

            self.assertEqual(result.exit_code, 2)
            self.assertEqual(result.graph.status, "cancelled")
            self.assertEqual(constructed, [])
            self.assertEqual(leased, [])

    def test_scene_rejects_wrong_arm_and_non_six_joint_pose(self):
        original = json.loads(DEFAULT_SCENE_PATH.read_text(encoding="utf-8"))
        for field, invalid in (("arm", "left"),):
            with self.subTest(field=field), TemporaryDirectory() as temporary:
                value = dict(original)
                value[field] = invalid
                path = Path(temporary) / "scene.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "arm"):
                    load_r09_scene(path)
        with TemporaryDirectory() as temporary:
            value = json.loads(json.dumps(original))
            value["pose"]["joints"] = value["pose"]["joints"][:-1]
            path = Path(temporary) / "scene.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "six"):
                load_r09_scene(path)

    def test_fake_live_run_applies_user_tool_and_all_speed_limits_before_single_move(self):
        with TemporaryDirectory() as temporary:
            adapter, dashboard, motion = make_adapter()
            lease = FakeLease()
            requested_arms = []
            result = run_beaker_approach(
                load_r09_scene(), artifact_root=Path(temporary), live=True,
                input_fn=lambda _: "YES", output=StringIO(),
                adapter_factory=lambda arms: requested_arms.append(arms) or adapter,
                lease_factory=lambda: lease,
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(requested_arms, [("right",)])
            self.assertEqual(set(adapter.grippers), set())
            settings = {name: arguments for name, arguments in dashboard.calls if name in {
                "SpeedFactor", "AccJ", "SpeedJ", "AccL", "SpeedL", "Tool", "User"
            }}
            self.assertEqual(settings, {
                "SpeedFactor": (10,), "AccJ": (10,), "SpeedJ": (10,), "AccL": (10,),
                "SpeedL": (10,), "Tool": (0,), "User": (0,),
            })
            self.assertEqual(motion.calls, [
                ("JointMovJ", tuple(EXPECTED_POSE["joints"])), ("Sync", ()),
            ])
            configure = result.graph.nodes[3]
            self.assertEqual(configure.status, "succeeded")
            self.assertEqual(configure.evidence["speed_percent"], 10)
            self.assertEqual(configure.evidence["tool_number"], 0)
            self.assertEqual(configure.evidence["user_number"], 0)
            self.assertIn("User", configure.evidence["replies"])
            self.assertNotIn("StopScript", [name for name, _ in dashboard.calls])
            self.assertTrue(dashboard.closed and motion.closed)
            self.assertTrue(lease.acquired and lease.released)

    def test_default_adapter_factory_calls_live_factory_without_arm_tuple_as_timeout(self):
        with TemporaryDirectory() as temporary:
            lease = FakeLease()
            closed = []
            adapter = type("ClosableAdapter", (), {"close": lambda self: closed.append(True)})()

            class SuccessfulExecutor:
                def execute(self, graph):
                    graph.status = "succeeded"
                    graph.nodes[0].status = "succeeded"

            with patch("task_skills.run_beaker_approach.create_live_adapter", return_value=adapter) as factory:
                result = run_beaker_approach(
                    load_r09_scene(), artifact_root=Path(temporary), live=True,
                    input_fn=lambda _: "YES", output=StringIO(),
                    lease_factory=lambda: lease,
                    executor_factory=lambda hardware, scene, journal: SuccessfulExecutor(),
                )

            factory.assert_called_once_with()
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(lease.released)
            self.assertEqual(closed, [True])

    def test_empty_speed_or_user_reply_fails_closed_before_jointmovj(self):
        for command in ("SpeedFactor", "User"):
            with self.subTest(command=command), TemporaryDirectory() as temporary:
                adapter, dashboard, motion = make_adapter(blank_command=command)
                lease = FakeLease()
                with self.assertRaisesRegex(RuntimeError, "acknowledged controller reply"):
                    run_beaker_approach(
                        load_r09_scene(), artifact_root=Path(temporary), live=True,
                        input_fn=lambda _: "YES", output=StringIO(),
                        adapter_factory=lambda arms: adapter, lease_factory=lambda: lease,
                    )

                self.assertFalse(any(name == "JointMovJ" for name, _ in motion.calls))
                self.assertIn("StopScript", [name for name, _ in dashboard.calls])
                self.assertTrue(dashboard.closed and motion.closed and lease.released)

    def test_interrupt_stops_before_executor_returns_and_records_interrupted_graph(self):
        with TemporaryDirectory() as temporary:
            actions = []

            class InterruptAdapter:
                cancel_event = None

                def set_cancel_event(self, event):
                    self.cancel_event = event

                def stop_all(self):
                    actions.append("stop")

                def stop_and_verify(self):
                    actions.append("verify")
                    return {"confirmed": True}

                def snapshot_all(self):
                    return {"hardware_connected": True}

                def close(self):
                    actions.append("close")

            adapter = InterruptAdapter()
            lease = FakeLease()

            class InterruptExecutor:
                def execute(self, graph):
                    try:
                        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
                    except KeyboardInterrupt:
                        actions.append("executor_unwound")
                        raise

            with self.assertRaises(KeyboardInterrupt):
                run_beaker_approach(
                    load_r09_scene(), artifact_root=Path(temporary), live=True,
                    input_fn=lambda _: "YES", output=StringIO(),
                    adapter_factory=lambda arms: adapter,
                    lease_factory=lambda: lease,
                    executor_factory=lambda hardware, scene, journal: InterruptExecutor(),
                )

            self.assertTrue(adapter.cancel_event.is_set())
            self.assertEqual(actions[:2], ["stop", "executor_unwound"])
            self.assertIn("verify", actions)
            self.assertEqual(actions[-1], "close")
            self.assertTrue(lease.released)

    def test_configuration_failure_stops_verifies_closes_and_keeps_graph_evidence(self):
        with TemporaryDirectory() as temporary:
            adapter, dashboard, motion = make_adapter(fail_user=True)
            lease = FakeLease()
            with self.assertRaisesRegex(RuntimeError, "User\(0\)"):
                run_beaker_approach(
                    load_r09_scene(), artifact_root=Path(temporary), live=True,
                    input_fn=lambda _: "YES", output=StringIO(),
                    adapter_factory=lambda arms: adapter, lease_factory=lambda: lease,
                )

            self.assertIn("StopScript", [name for name, _ in dashboard.calls])
            self.assertIn("DisableRobot", [name for name, _ in dashboard.calls])
            self.assertTrue(dashboard.closed and motion.closed)
            self.assertTrue(lease.released)

    def test_live_adapter_closes_dashboard_if_motion_channel_construction_fails(self):
        closed = []

        class Resource:
            def __init__(self, *args):
                pass

            def close(self):
                closed.append(True)

        def fail_motion(*args):
            raise OSError("motion channel unavailable")

        package = ModuleType("dobot_control")
        package.__path__ = []
        robots = ModuleType("dobot_control.robots")
        robots.__path__ = []
        api = ModuleType("dobot_control.robots.dobot_api")
        api.DobotApiDashboard = Resource
        api.DobotApiMove = fail_motion
        with patch.dict(sys.modules, {
            "dobot_control": package,
            "dobot_control.robots": robots,
            "dobot_control.robots.dobot_api": api,
        }), self.assertRaisesRegex(OSError, "motion channel unavailable"):
            create_live_adapter()

        self.assertEqual(len(closed), 1)

    def test_cli_defaults_to_dry_run_and_supports_both_mode_flags(self):
        parser = _build_argument_parser()
        self.assertFalse(parser.parse_args([]).live)
        self.assertFalse(parser.parse_args(["--dry-run"]).live)
        self.assertTrue(parser.parse_args(["--live"]).live)


if __name__ == "__main__":
    unittest.main()
