import json
import unittest
import sys
import threading
import time
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from task_skills.atomic_executor import AtomicGraphExecutor
from task_skills.atomic_graph import AtomicNode, compile_atomic_instruction
from task_skills.dobot_adapter import DobotHardwareAdapter
from task_skills.journal import ExecutionJournal
from task_skills.models import Action, Pose, SceneConfig
from task_skills.parser import parse_instruction
from task_skills.run_task import run_instruction


def scene_config():
    return SceneConfig(
        objects={
            "A": Pose(335.8601, -325.3453, 256.1169, 179.9632, 0.0539, -89.9401),
            "B": Pose(-314.1404, -363.9942, 258.4784, -179.9268, -0.0600, 51.3437),
        },
        targets={
            "target_A": Pose(23.0903, -399.6884, 255.4660, -179.9909, 0.0646, -47.5580),
            "target_B": Pose(4.1451, -421.3667, 260.6784, -179.9079, 0.0220, -0.2228),
        },
        transport_heights={"left": 415.5, "right": 415.5},
    )


COORDINATED_INSTRUCTION = (
    "用左臂把物体A拿起来，然后右臂把物体B拿起来。"
    "左臂拿起物体A以后保持5秒，右臂直接送到目标B。"
    "然后5s后左臂再把物体A送到目标A。"
    "随后右臂把物体B从目标B拿起来放回物体B原来的位置。"
)


class RecordingAdapter:
    def __init__(self, fail_operation=None):
        self.calls = []
        self.fail_operation = fail_operation

    def execute_atomic(self, node, scene):
        self.calls.append(node.operation)
        if node.operation == self.fail_operation:
            raise RuntimeError("injected failure")
        return {"controller_idle": True}

    def stop(self, arm):
        self.calls.append(f"stop:{arm}")

    def snapshot(self, arm):
        return {"arm": arm, "mode": 5, "errors": []}

    def snapshot_all(self):
        return {"left": self.snapshot("left")}


class FakeDashboard:
    def __init__(self):
        self.calls = []

    def RobotMode(self):
        self.calls.append("RobotMode")
        return "0,{5},RobotMode();"

    def GetErrorID(self):
        return "0,{[[],[],[],[],[],[],[]]},GetErrorID();"


class FakeMotion:
    def __init__(self):
        self.calls = []


class FakeGripper:
    def __init__(self):
        self.calls = []
        # This rig inverts the Feetech convention: 0 is closed, 255 is open.
        self.position = 255

    def get_open_position(self):
        return 255

    def get_closed_position(self):
        return 0

    def move(self, position, speed, force):
        self.calls.append((position, speed, force))
        self.position = position
        return True, 0

    def get_current_position(self):
        return self.position


class ClosableAdapter:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class ParserTests(unittest.TestCase):
    def test_compact_pick_place_expands_to_two_actions(self):
        self.assertEqual(
            parse_instruction("把物体A抓取放到目标A"),
            [
                Action("pick", arm=None, object_id="A"),
                Action("place", arm=None, object_id="A", target="target_A"),
            ],
        )


class CoordinatedParserTests(unittest.TestCase):
    def test_full_hold_and_return_instruction_has_explicit_dependencies(self):
        actions = parse_instruction(COORDINATED_INSTRUCTION)

        self.assertEqual(
            [action.parameters["action_id"] for action in actions],
            [
                "left_pick_A",
                "left_hold_A",
                "right_pick_B",
                "right_place_B",
                "left_place_A",
                "right_repick_B",
                "right_return_B",
            ],
        )
        self.assertEqual(actions[1].parameters["seconds"], 5.0)
        self.assertEqual(actions[5].parameters["depends_on"], ["right_place_B", "left_place_A"])
        self.assertEqual(actions[5].parameters["source_ref"], "target:target_B")
        self.assertEqual(actions[6].parameters["destination_ref"], "object:B")


class AtomicGraphTests(unittest.TestCase):
    def test_a_pick_place_expands_to_expected_atomic_operations(self):
        graph = compile_atomic_instruction("把物体A抓取放到目标A")

        self.assertEqual(
            [node.operation for node in graph.nodes],
            [
                "wait_controller_ready",
                "clear_error",
                "enable_robot",
                "configure_robot",
                "open_gripper",
                "move_safe_pose",
                "move_pose",
                "move_pose",
                "close_gripper",
                "move_pose",
                "move_pose",
                "move_pose",
                "open_gripper",
                "move_pose",
                "move_safe_pose",
            ],
        )
        self.assertTrue(all(node.arm == "left" for node in graph.nodes))
        pose_refs = [node.parameters["pose_ref"] for node in graph.nodes if node.operation == "move_pose"]
        self.assertEqual(
            pose_refs,
            [
                "object:A:transport",
                "object:A",
                "object:A:transport",
                "target:target_A:transport",
                "target:target_A",
                "target:target_A:transport",
            ],
        )
        motions = [node.parameters["motion"] for node in graph.nodes if node.operation == "move_pose"]
        self.assertEqual(motions, ["linear"] * 6)
        close_node = next(node for node in graph.nodes if node.operation == "close_gripper")
        self.assertIn("meaningful_closing_travel", close_node.success_criteria)
        transport_node = next(
            node for node in graph.nodes if node.parameters.get("pose_ref") == "object:A:transport"
        )
        self.assertIn("target_feedback_within_tolerance", transport_node.success_criteria)

    def test_default_robot_configuration_uses_ten_percent(self):
        graph = compile_atomic_instruction("把物体A抓取放到目标A")
        configure = next(node for node in graph.nodes if node.operation == "configure_robot")

        self.assertEqual(configure.parameters["speed_percent"], 10)

    def test_atomic_edges_form_a_strict_sequence_for_one_arm(self):
        graph = compile_atomic_instruction("把物体A抓取放到目标A")

        self.assertEqual(
            graph.edges,
            [
                (graph.nodes[index].node_id, graph.nodes[index + 1].node_id)
                for index in range(len(graph.nodes) - 1)
            ],
        )

    def test_binding_mismatch_fails_before_graph_creation(self):
        with self.assertRaisesRegex(ValueError, "left.*target_A"):
            compile_atomic_instruction("左臂抓取物体A，把A放到目标B")


class CoordinatedGraphTests(unittest.TestCase):
    def test_graph_has_parallel_initial_picks_wait_and_joined_return(self):
        graph = compile_atomic_instruction(COORDINATED_INSTRUCTION)
        by_label = {node.label: node for node in graph.nodes}

        barrier = by_label["initialization_barrier"]
        self.assertEqual(by_label["left_pick_A:transport"].dependencies, [barrier.node_id])
        self.assertEqual(by_label["right_pick_B:transport"].dependencies, [barrier.node_id])
        self.assertEqual(by_label["left_hold_A:wait"].parameters["seconds"], 5.0)
        self.assertCountEqual(
            by_label["right_repick_B:transport"].dependencies,
            [by_label["right_place_B:retract"].node_id, by_label["left_place_A:retract"].node_id],
        )
        self.assertEqual(by_label["right_repick_B:contact"].parameters["pose_ref"], "target:target_B")
        self.assertEqual(by_label["right_return_B:contact"].parameters["pose_ref"], "object:B")
        self.assertIn("disable_robot", [node.operation for node in graph.nodes])


class AtomicExecutorTests(unittest.TestCase):
    def test_executor_consumes_same_graph_object_and_persists_success(self):
        with TemporaryDirectory() as directory:
            graph = compile_atomic_instruction("把物体A抓取放到目标A")
            adapter = RecordingAdapter()
            journal = ExecutionJournal(Path(directory))

            result = AtomicGraphExecutor(adapter, scene_config(), journal).execute(graph)

            self.assertIs(result, graph)
            self.assertEqual(graph.status, "succeeded")
            self.assertTrue((Path(directory) / "execution_graph.json").exists())
            self.assertTrue((Path(directory) / "execution_events.jsonl").exists())
            self.assertTrue((Path(directory) / "execution_summary.json").exists())

    def test_final_snapshot_failure_is_recorded_without_stopping_successful_graph(self):
        class SnapshotFailingAdapter(RecordingAdapter):
            def __init__(self):
                super().__init__()
                self.snapshot_attempts = 0

            def snapshot_all(self):
                self.snapshot_attempts += 1
                raise ValueError("read pos failed: comm_result=-3001, servo_error=0")

        with TemporaryDirectory() as directory:
            graph = compile_atomic_instruction("把物体A抓取放到目标A")
            adapter = SnapshotFailingAdapter()
            journal = ExecutionJournal(Path(directory))

            result = AtomicGraphExecutor(adapter, scene_config(), journal).execute(graph)

            summary = json.loads((Path(directory) / "execution_summary.json").read_text(encoding="utf-8"))
            events = journal.read_events()

        self.assertIs(result, graph)
        self.assertEqual(graph.status, "succeeded")
        self.assertTrue(all(node.status == "succeeded" for node in graph.nodes))
        self.assertEqual(adapter.snapshot_attempts, 1)
        self.assertEqual(sorted(adapter.calls), sorted(node.operation for node in graph.nodes))
        self.assertNotIn("stop:left", adapter.calls)
        self.assertEqual(summary["status"], "succeeded")
        self.assertIn("warning", summary["final_state"])
        self.assertEqual(
            summary["final_state"]["snapshot_error"],
            "ValueError: read pos failed: comm_result=-3001, servo_error=0",
        )
        failed_snapshot = next(event for event in events if event["event"] == "final_snapshot_failed")
        self.assertEqual(failed_snapshot["snapshot_error"], summary["final_state"]["snapshot_error"])

    def test_node_feedback_failure_still_stops_and_marks_graph_failed(self):
        class FeedbackFailingAdapter(RecordingAdapter):
            def execute_atomic(self, node, scene):
                self.calls.append(node.operation)
                if node.operation == "close_gripper":
                    raise ValueError("communication failure: comm_result=-3001, servo_error=0")
                return {"controller_idle": True}

        with TemporaryDirectory() as directory:
            graph = compile_atomic_instruction("把物体A抓取放到目标A")
            adapter = FeedbackFailingAdapter()
            journal = ExecutionJournal(Path(directory))

            with self.assertRaisesRegex(ValueError, "communication failure"):
                AtomicGraphExecutor(adapter, scene_config(), journal).execute(graph)

        self.assertEqual(graph.status, "failed")
        self.assertEqual(next(node for node in graph.nodes if node.status == "failed").operation, "close_gripper")
        self.assertIn("stop:left", adapter.calls)
        self.assertTrue(all(node.status != "pending" for node in graph.nodes))

    def test_failure_stops_arm_and_blocks_dependent_nodes(self):
        with TemporaryDirectory() as directory:
            graph = compile_atomic_instruction("把物体A抓取放到目标A")
            adapter = RecordingAdapter(fail_operation="close_gripper")
            journal = ExecutionJournal(Path(directory))

            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                AtomicGraphExecutor(adapter, scene_config(), journal).execute(graph)

            failed_index = next(index for index, node in enumerate(graph.nodes) if node.status == "failed")
            self.assertTrue(all(node.status == "blocked" for node in graph.nodes[failed_index + 1:]))
            self.assertIn("stop:left", adapter.calls)


class CoordinatedExecutorTests(unittest.TestCase):
    def test_left_wait_allows_right_place_and_preserves_same_arm_serial_execution(self):
        class ConcurrentRecordingAdapter:
            def __init__(self):
                self.lock = threading.Lock()
                self.active_arms = set()
                self.same_arm_overlap = False
                self.right_place_completed = threading.Event()
                self.right_completed_during_wait = False
                self.started = {}
                self.completed = {}

            def execute_atomic(self, node, scene):
                with self.lock:
                    if node.arm in self.active_arms and node.arm != "system":
                        self.same_arm_overlap = True
                    self.active_arms.add(node.arm)
                    self.started[node.label] = time.monotonic()
                try:
                    if node.label == "left_hold_A:wait":
                        self.right_completed_during_wait = self.right_place_completed.wait(0.5)
                    elif node.label == "right_place_B:retract":
                        self.right_place_completed.set()
                    time.sleep(0.001)
                    return {"label": node.label}
                finally:
                    with self.lock:
                        self.completed[node.label] = time.monotonic()
                        self.active_arms.remove(node.arm)

            def snapshot_all(self):
                return {}

            def snapshot(self, arm):
                return {"arm": arm}

            def stop(self, arm):
                pass

        with TemporaryDirectory() as directory:
            graph = compile_atomic_instruction(COORDINATED_INSTRUCTION)
            adapter = ConcurrentRecordingAdapter()
            journal = ExecutionJournal(Path(directory))

            AtomicGraphExecutor(adapter, scene_config(), journal).execute(graph)

        self.assertTrue(adapter.right_completed_during_wait)
        self.assertFalse(adapter.same_arm_overlap)
        self.assertGreaterEqual(
            adapter.started["right_repick_B:transport"], adapter.completed["left_place_A:retract"]
        )
        self.assertGreaterEqual(
            adapter.started["right_repick_B:transport"], adapter.completed["right_place_B:retract"]
        )


class CoordinatedFailureTests(unittest.TestCase):
    def test_any_arm_failure_stops_and_disables_both_arms(self):
        class FailingAdapter:
            def __init__(self):
                self.calls = []

            def execute_atomic(self, node, scene):
                if node.label == "right_place_B:contact":
                    raise RuntimeError("right placement failed")
                return {"label": node.label}

            def stop_all(self):
                self.calls.extend(["stop:left", "disable:left", "stop:right", "disable:right"])

            def snapshot_all(self):
                return {}

            def snapshot(self, arm):
                return {"arm": arm}

        with TemporaryDirectory() as directory:
            graph = compile_atomic_instruction(COORDINATED_INSTRUCTION)
            adapter = FailingAdapter()
            journal = ExecutionJournal(Path(directory))

            with self.assertRaisesRegex(RuntimeError, "right placement failed"):
                AtomicGraphExecutor(adapter, scene_config(), journal).execute(graph)

        self.assertEqual(
            adapter.calls,
            ["stop:left", "disable:left", "stop:right", "disable:right"],
        )
        failed = next(node for node in graph.nodes if node.status == "failed")
        self.assertEqual(failed.label, "right_place_B:contact")
        self.assertTrue(all(node.status != "pending" for node in graph.nodes))


class AdapterSafetyTests(unittest.TestCase):
    def test_constructor_does_not_enable_move_or_cycle_gripper(self):
        dashboard = FakeDashboard()
        motion = FakeMotion()
        gripper = FakeGripper()

        DobotHardwareAdapter.from_components(
            dashboards={"left": dashboard},
            motions={"left": motion},
            grippers={"left": gripper},
        )

        self.assertEqual(dashboard.calls, [])
        self.assertEqual(motion.calls, [])
        self.assertEqual(gripper.calls, [])

    def test_explicit_open_gripper_is_first_gripper_motion(self):
        gripper = FakeGripper()
        adapter = DobotHardwareAdapter.from_components(
            dashboards={"left": FakeDashboard()},
            motions={"left": FakeMotion()},
            grippers={"left": gripper},
        )
        node = AtomicNode("atomic-000", 0, "open_gripper", "left")

        adapter.execute_atomic(node, scene_config())

        self.assertEqual(gripper.calls, [(255, 30, 50)])

    def test_robot_configuration_uses_tool_zero_without_redefining_tool_one(self):
        class ConfigDashboard(FakeDashboard):
            def __getattr__(self, name):
                def command(*arguments):
                    self.calls.append((name, arguments))
                    return "0,{},command();"

                return command

        dashboard = ConfigDashboard()
        adapter = DobotHardwareAdapter.from_components(
            dashboards={"left": dashboard}, motions={"left": FakeMotion()}, grippers={"left": FakeGripper()}
        )

        adapter.configure_robot("left", 10)

        command_calls = [call for call in dashboard.calls if isinstance(call, tuple)]
        self.assertIn(("Tool", (0,)), command_calls)
        self.assertFalse(any(name == "SetTool" for name, _ in command_calls))

    def test_motion_does_not_complete_on_stale_idle_pose(self):
        class StaleDashboard(FakeDashboard):
            def GetPose(self):
                return "0,{0,0,0,0,0,0},GetPose();"

        class CartesianMotion(FakeMotion):
            def MovL(self, *pose):
                self.calls.append(("MovL", pose))
                return "0,{},MovL();"

            def Sync(self):
                return "0,{},Sync();"

        adapter = DobotHardwareAdapter.from_components(
            dashboards={"left": StaleDashboard()},
            motions={"left": CartesianMotion()},
            grippers={"left": FakeGripper()},
            settle_timeout=0.01,
        )

        with self.assertRaises(TimeoutError):
            adapter.move_to("left", Pose(100, 200, 300, 180, 0, -90), motion="linear")

    def test_close_gripper_waits_for_meaningful_stable_motion(self):
        class MovingGripper(FakeGripper):
            def __init__(self):
                super().__init__()
                self.positions = iter([255, 251, 225, 195, 195, 195, 195, 195])

            def get_current_position(self):
                return next(self.positions)

        gripper = MovingGripper()
        adapter = DobotHardwareAdapter.from_components(
            dashboards={"left": FakeDashboard()},
            motions={"left": FakeMotion()},
            grippers={"left": gripper},
            settle_timeout=0.5,
        )

        evidence = adapter.close_gripper("left", timeout=0.5)

        self.assertEqual(evidence["gripper_position"], 195)
        self.assertGreaterEqual(evidence["gripper_travel"], 10)

    def test_close_gripper_rejects_command_acceptance_without_closure(self):
        class BarelyMovingGripper(FakeGripper):
            def get_current_position(self):
                return 254 if self.calls else 255

        adapter = DobotHardwareAdapter.from_components(
            dashboards={"left": FakeDashboard()},
            motions={"left": FakeMotion()},
            grippers={"left": BarelyMovingGripper()},
            settle_timeout=0.01,
        )

        with self.assertRaises(TimeoutError):
            adapter.close_gripper("left", timeout=0.01)

    def test_move_without_joint_feedback_is_rejected_as_unverifiable(self):
        class LegacyMotion:
            def __init__(self):
                self.commands = []

            def JointMovJ(self, *joints):
                self.commands.append(joints)
                return "accepted"

        class LegacyDashboard:
            def RobotMode(self):
                return "0,{5},RobotMode();"

        class LegacyRobot:
            def __init__(self):
                self.robot = LegacyMotion()
                self.r_inter = LegacyDashboard()
                self.gripper = FakeGripper()

        left = LegacyRobot()
        adapter = DobotHardwareAdapter(left=left, settle_timeout=0.1)

        with self.assertRaisesRegex(RuntimeError, "GetAngle"):
            adapter.move_to("left", Pose(1, 2, 3, joints=(1, 2, 3, 4, 5, 6)))

        self.assertEqual(left.robot.commands, [(1, 2, 3, 4, 5, 6)])

    def test_motion_requires_idle_mode_after_reaching_target(self):
        class MovingThenIdleDashboard(FakeDashboard):
            def __init__(self):
                super().__init__()
                self.modes = iter([7, 7, 5, 5, 5])

            def RobotMode(self):
                self.calls.append("RobotMode")
                return f"0,{{{next(self.modes)}}},RobotMode();"

            def GetPose(self):
                return "0,{100,200,300,180,0,-90},GetPose();"

        class CartesianMotion(FakeMotion):
            def MovL(self, *pose):
                self.calls.append(("MovL", pose))
                return "0,{},MovL();"

            def Sync(self):
                return "0,{},Sync();"

        dashboard = MovingThenIdleDashboard()
        adapter = DobotHardwareAdapter.from_components(
            dashboards={"left": dashboard},
            motions={"left": CartesianMotion()},
            grippers={"left": FakeGripper()},
            settle_timeout=0.5,
        )

        adapter.move_to("left", Pose(100, 200, 300, 180, 0, -90), motion="linear")

        self.assertEqual(dashboard.calls.count("RobotMode"), 5)


class CoordinatedAdapterTests(unittest.TestCase):
    def test_wait_uses_elapsed_time_without_hardware_commands(self):
        dashboard = FakeDashboard()
        motion = FakeMotion()
        gripper = FakeGripper()
        adapter = DobotHardwareAdapter.from_components(
            dashboards={"left": dashboard}, motions={"left": motion}, grippers={"left": gripper}
        )
        node = AtomicNode("atomic-wait", 0, "wait", "left", parameters={"seconds": 0.01})

        evidence = adapter.execute_atomic(node, scene_config())

        self.assertGreaterEqual(evidence["elapsed_seconds"], 0.01)
        self.assertEqual(dashboard.calls, [])
        self.assertEqual(motion.calls, [])
        self.assertEqual(gripper.calls, [])

    def test_barrier_never_touches_hardware(self):
        adapter = DobotHardwareAdapter.from_components(dashboards={}, motions={}, grippers={})
        node = AtomicNode("atomic-barrier", 0, "barrier", "system")

        self.assertEqual(adapter.execute_atomic(node, scene_config()), {"dependencies_satisfied": True})

    def test_disable_waits_for_mode_four(self):
        class DisableDashboard(FakeDashboard):
            def DisableRobot(self):
                self.calls.append("DisableRobot")
                return "0,{},DisableRobot();"

            def RobotMode(self):
                self.calls.append("RobotMode")
                return "0,{4},RobotMode();"

            def GetPose(self):
                return "0,{0,0,0,0,0,0},GetPose();"

            def GetAngle(self):
                return "0,{0,0,0,0,0,0},GetAngle();"

        dashboard = DisableDashboard()
        adapter = DobotHardwareAdapter.from_components(
            dashboards={"left": dashboard}, motions={"left": FakeMotion()}, grippers={"left": FakeGripper()}
        )
        node = AtomicNode("atomic-disable", 0, "disable_robot", "left", timeout_seconds=0.5)

        evidence = adapter.execute_atomic(node, scene_config())

        self.assertEqual(evidence["mode"], 4)
        self.assertEqual(dashboard.calls.count("DisableRobot"), 1)

    def test_stop_all_attempts_stop_and_disable_for_both_arms(self):
        class StopDashboard(FakeDashboard):
            def StopScript(self):
                self.calls.append("StopScript")
                return "0,{},StopScript();"

            def DisableRobot(self):
                self.calls.append("DisableRobot")
                return "0,{},DisableRobot();"

        left = StopDashboard()
        right = StopDashboard()
        adapter = DobotHardwareAdapter.from_components(
            dashboards={"left": left, "right": right}, motions={}, grippers={}
        )

        result = adapter.stop_all()

        self.assertEqual(left.calls, ["StopScript", "DisableRobot"])
        self.assertEqual(right.calls, ["StopScript", "DisableRobot"])
        self.assertEqual(set(result), {"left", "right"})


class CliGateTests(unittest.TestCase):
    def test_non_yes_cancels_without_constructing_adapter(self):
        with TemporaryDirectory() as directory:
            constructed = []

            result = run_instruction(
                "把物体A抓取放到目标A",
                scene_config(),
                artifact_root=Path(directory),
                input_fn=lambda _: "yes",
                output=StringIO(),
                adapter_factory=lambda arms: constructed.append(arms),
            )

            self.assertEqual(result.exit_code, 2)
            self.assertEqual(constructed, [])

    def test_exact_yes_executes_previewed_graph_instance(self):
        with TemporaryDirectory() as directory:
            observed = {}
            adapter = ClosableAdapter()

            class RecordingExecutor:
                def execute(self, graph):
                    observed["executed"] = graph
                    graph.status = "succeeded"
                    return graph

            result = run_instruction(
                "把物体A抓取放到目标A",
                scene_config(),
                artifact_root=Path(directory),
                input_fn=lambda _: "YES",
                output=StringIO(),
                adapter_factory=lambda arms: adapter,
                executor_factory=lambda hardware, scene, journal: RecordingExecutor(),
            )

            self.assertIs(observed["executed"], result.graph)
            self.assertTrue(adapter.closed)


class SceneValidationTests(unittest.TestCase):
    def test_target_a_is_on_taught_negative_y_side(self):
        from task_skills.models import load_scene_config
        from task_skills.run_task import DEFAULT_SCENE_PATH

        scene = load_scene_config(DEFAULT_SCENE_PATH)

        self.assertAlmostEqual(scene.targets["target_A"].y, -399.6884)

    def test_transport_pose_uses_initial_left_height(self):
        from task_skills.models import load_scene_config, resolve_pose_ref
        from task_skills.run_task import DEFAULT_SCENE_PATH

        scene = load_scene_config(DEFAULT_SCENE_PATH)
        target = scene.targets["target_A"]
        transport = resolve_pose_ref(scene, "target:target_A:transport", arm="left")

        self.assertEqual((transport.x, transport.y, transport.z), (target.x, target.y, 415.5))
        self.assertEqual((transport.rx, transport.ry, transport.rz), (target.rx, target.ry, target.rz))

    def test_object_transport_pose_uses_same_left_height(self):
        from task_skills.models import load_scene_config, resolve_pose_ref
        from task_skills.run_task import DEFAULT_SCENE_PATH

        scene = load_scene_config(DEFAULT_SCENE_PATH)
        transport = resolve_pose_ref(scene, "object:A:transport", arm="left")

        self.assertAlmostEqual(transport.z, 415.5)


class PhysicalLayoutTests(unittest.TestCase):
    def test_left_nova_uses_physically_left_gripper_channel(self):
        from task_skills.layout import physical_nova_gripper_binding

        self.assertEqual(physical_nova_gripper_binding("left"), ("GRIPPER_RIGHT", "COM8"))

    def test_right_nova_uses_physically_right_gripper_channel(self):
        from task_skills.layout import physical_nova_gripper_binding

        self.assertEqual(physical_nova_gripper_binding("right"), ("GRIPPER_LEFT", "COM7"))

    def test_live_adapter_consumes_crossed_physical_gripper_bindings(self):
        constructed_grippers = []

        class FakeResource:
            def __init__(self, *arguments):
                self.arguments = arguments

            def close(self):
                pass

        class FakeLiveGripper:
            def __init__(self, **arguments):
                constructed_grippers.append(arguments)

            def disconnect(self):
                pass

        configured_grippers = {
            "GRIPPER_LEFT": SimpleNamespace(port="left-linux", id_name=21, pos=(2048, 3052)),
            "GRIPPER_RIGHT": SimpleNamespace(port="right-linux", id_name=22, pos=(2048, 3052)),
        }
        fake_modules = {
            "dobot_control.gripper.dobot_gripper": SimpleNamespace(DobotGripper=FakeLiveGripper),
            "dobot_control.robots.dobot_api": SimpleNamespace(
                DobotApiDashboard=FakeResource,
                DobotApiMove=FakeResource,
            ),
            "scripts.manipulate_utils": SimpleNamespace(
                load_ini_data_gripper=lambda: (None, configured_grippers),
            ),
        }

        with patch.dict(sys.modules, fake_modules), patch("task_skills.dobot_adapter.os.name", "nt"):
            adapter = DobotHardwareAdapter.from_live(("left", "right"))
            adapter.close()

        self.assertEqual(
            [(value["port"], value["id_name"]) for value in constructed_grippers],
            [("COM8", 22), ("COM7", 21)],
        )


class EndToEndDryRunTests(unittest.TestCase):
    def test_compact_instruction_previews_resolved_target_and_creates_artifacts_without_hardware(self):
        with TemporaryDirectory() as directory:
            constructed = []
            output = StringIO()

            result = run_instruction(
                "把物体A抓取放到目标A",
                scene_config(),
                artifact_root=Path(directory),
                output=output,
                adapter_factory=lambda arms: constructed.append(arms),
                dry_run=True,
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(constructed, [])
            self.assertIn("-399.6884", output.getvalue())
            self.assertIn("speed_limit: 10%", output.getvalue())
            self.assertTrue((result.artifact_directory / "execution_graph.json").exists())
            self.assertTrue((result.artifact_directory / "execution_events.jsonl").exists())
            self.assertTrue((result.artifact_directory / "execution_summary.json").exists())

    def test_coordinated_instruction_previews_parallel_hold_return_without_hardware(self):
        with TemporaryDirectory() as directory:
            constructed = []
            output = StringIO()

            result = run_instruction(
                COORDINATED_INSTRUCTION,
                scene_config(),
                artifact_root=Path(directory),
                output=output,
                adapter_factory=lambda arms: constructed.append(arms),
                dry_run=True,
            )

            preview = output.getvalue()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(constructed, [])
            self.assertIn("parallel_start: left_pick_A + right_pick_B", preview)
            self.assertIn("left_hold_A:wait", preview)
            self.assertIn("seconds=5.0", preview)
            self.assertIn("target:target_B", preview)
            self.assertIn("object:B", preview)
            self.assertIn("disable_robot", preview)


if __name__ == "__main__":
    unittest.main()
