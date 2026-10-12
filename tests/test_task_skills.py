import pytest

from task_skills import Action, ActionError, BimanualTaskExecutor, InMemoryRobot, Pose, SceneConfig, parse_instruction


def scene():
    return SceneConfig(
        objects={"A": Pose(1, 1, 1), "B": Pose(2, 2, 1)},
        targets={"fixed_A": Pose(3, 3, 1), "fixed_B": Pose(4, 4, 1)},
    )


def test_parameterized_bimanual_pick_and_place_updates_state():
    robot = InMemoryRobot()
    executor = BimanualTaskExecutor(robot, scene())
    executor.execute([
        Action("pick", arm="left", object_id="A"),
        Action("pick", arm="right", object_id="B"),
        Action("place", arm="left", object_id="A", target="fixed_A"),
        Action("place", arm="right", object_id="B", target="fixed_B"),
    ])
    assert executor.state.object_locations == {"A": "fixed_A", "B": "fixed_B"}
    assert not executor.state.held_by


def test_pick_and_place_use_vertical_approach_and_retract():
    robot = InMemoryRobot()
    executor = BimanualTaskExecutor(robot, scene())
    executor.pick("left", "A")
    executor.place("left", "A", "fixed_A")
    moves = [command for command in robot.commands if command[0] == "move_to"]
    assert [move[2].z for move in moves] == [51, 1, 51, 51, 1, 51]


def test_place_rejects_occupied_target():
    executor = BimanualTaskExecutor(InMemoryRobot(), scene())
    executor.pick("left", "A")
    executor.pick("right", "B")
    executor.place("left", "A", "fixed_A")
    with pytest.raises(ActionError, match="occupied"):
        executor.place("right", "B", "fixed_A")


def test_arm_object_and_target_binding_is_enforced():
    executor = BimanualTaskExecutor(InMemoryRobot(), scene())
    with pytest.raises(ActionError, match="may only pick object A"):
        executor.pick("right", "A")
    executor.pick("left", "A")
    with pytest.raises(ActionError, match="may only place at target_A"):
        executor.place("left", "A", "fixed_B")


def test_parse_chinese_instruction_into_actions():
    actions = parse_instruction(
        "用左臂抓取物体A，用右臂抓取物体B，"
        "把左臂的物体A放到目标A，再把右臂的物体B放到物体A原来的位置。"
    )
    assert actions == [
        Action("pick", arm="left", object_id="A"),
        Action("pick", arm="right", object_id="B"),
        Action("place", arm="left", object_id="A", target="target_A"),
        Action("place", arm="right", object_id="B", target="initial_A"),
    ]


def test_parse_both_fixed_targets_and_initial_positions():
    actions = parse_instruction(
        "左臂抓取物体A，右臂抓取物体B，"
        "把A放到目标B，把B放到物体A的位置。"
    )
    assert actions == [
        Action("pick", arm="left", object_id="A"),
        Action("pick", arm="right", object_id="B"),
        Action("place", arm=None, object_id="A", target="target_B"),
        Action("place", arm=None, object_id="B", target="initial_A"),
    ]


def test_parse_wait_and_keep_order_for_repeated_actions():
    actions = parse_instruction("先抓取物体A，把A放到目标A，等待3秒，再抓取物体A")
    assert [action.name for action in actions] == ["pick", "place", "wait", "pick"]
    assert actions[2].parameters == {"seconds": 3.0}


def test_build_execution_graph_preserves_source_order_and_parallel_groups():
    from task_skills import compile_instruction

    graph = compile_instruction("先抓取物体A，抓取物体B，然后把A放到目标A，把B放到目标B")

    assert graph.schema_version == "1.0"
    assert [node.skill_name for node in graph.nodes] == ["pick", "pick", "place", "place"]
    assert [node.sequence for node in graph.nodes] == [0, 1, 2, 3]
    assert graph.parallel_groups == [["node-000", "node-001"], ["node-002", "node-003"]]
    assert graph.edges == [
        ("node-000", "node-002"), ("node-000", "node-003"),
        ("node-001", "node-002"), ("node-001", "node-003"),
    ]


def test_execution_trace_persists_intermediate_state_and_success_evidence(tmp_path):
    from task_skills import ExecutionJournal

    journal = ExecutionJournal(tmp_path / "run")
    robot = InMemoryRobot()
    executor = BimanualTaskExecutor(robot, scene(), journal=journal)
    actions = parse_instruction("抓取物体A，把A放到目标A")
    graph = executor.execute(actions, source_instruction="抓取物体A，把A放到目标A")

    assert graph.status == "succeeded"
    assert (tmp_path / "run" / "execution_graph.json").exists()
    assert (tmp_path / "run" / "execution_events.jsonl").exists()
    assert (tmp_path / "run" / "execution_summary.json").exists()
    events = journal.read_events()
    assert any(event["event"] == "skill_started" for event in events)
    completed = [event for event in events if event["event"] == "skill_completed"]
    assert len(completed) == 2
    assert completed[-1]["state_after"]["object_locations"]["A"] == "target_A"
    assert completed[-1]["evidence"]["success"] is True


def test_execution_trace_persists_failure_evidence(tmp_path):
    from task_skills import ExecutionJournal

    journal = ExecutionJournal(tmp_path / "failed")
    executor = BimanualTaskExecutor(InMemoryRobot(), scene(), journal=journal)
    with pytest.raises(ActionError):
        executor.execute(
            [Action("pick", arm="right", object_id="A")],
            source_instruction="右臂抓取物体A",
        )

    events = journal.read_events()
    failed = [event for event in events if event["event"] == "skill_failed"]
    assert len(failed) == 1
    assert failed[0]["status"] == "failed"
    assert failed[0]["evidence"]["success"] is False
    assert failed[0]["error"]["type"] == "ActionError"


def test_physical_nova_home_is_explicitly_separate_from_master_home():
    from task_skills.layout import physical_nova_home_degrees, master_home_radians

    assert physical_nova_home_degrees("left") == (90, 0, 90, 0, -90, -90)
    assert physical_nova_home_degrees("right") == (-90, 0, -90, 0, 90, 90)
    assert master_home_radians("left") == (-1.57, 0, -1.57, 0, 1.57, 1.57)
    assert master_home_radians("right") == (1.57, 0, 1.57, 0, -1.57, -1.57)


def test_dobot_adapter_uses_calibrated_joint_pose_for_exact_scene_points():
    from task_skills.dobot_adapter import DobotHardwareAdapter

    class FakeMotion:
        def __init__(self):
            self.joint_commands = []

        def JointMovJ(self, *joints):
            self.joint_commands.append(joints)
            return "accepted"

    class FakeDashboard:
        def RobotMode(self):
            return "0,{5},RobotMode();"

        def GetPose(self):
            return "0,{0,0,0,0,0,0},GetPose();"

    class FakeGripper:
        def move(self, position, speed, force):
            return position, speed, force

    class FakeRobot:
        def __init__(self):
            self.robot = FakeMotion()
            self.r_inter = FakeDashboard()
            self.gripper = FakeGripper()

    left = FakeRobot()
    right = FakeRobot()
    adapter = DobotHardwareAdapter(left=left, right=right, settle_timeout=0.1)
    calibrated = Pose(1, 2, 3, joints=(1, 2, 3, 4, 5, 6))

    adapter.move_to("left", calibrated)

    assert left.robot.joint_commands == [(1, 2, 3, 4, 5, 6)]
    assert right.robot.joint_commands == []
