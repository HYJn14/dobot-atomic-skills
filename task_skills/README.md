# Dobot Task Skills

This package provides a hardware-independent task layer for fixed-scene
bimanual bottle manipulation. It does not connect to a robot on import.

An instruction is compiled into an `ExecutionGraph`. Each node records the
skill interface, preconditions, expected outputs, internal states, success
criteria, and safety exit policy. When an `ExecutionJournal` is supplied, the
executor persists:

- `execution_graph.json`: the executable skill graph and final node statuses;
- `execution_events.jsonl`: state transitions, intermediate snapshots, and
  failure evidence;
- `execution_summary.json`: final object state and verification level.

```python
from task_skills import Action, BimanualTaskExecutor, InMemoryRobot, Pose, SceneConfig

scene = SceneConfig(
    objects={"A": Pose(300, 100, 40), "B": Pose(300, -100, 40)},
    targets={"fixed_A": Pose(500, 100, 40), "fixed_B": Pose(500, -100, 40)},
)
executor = BimanualTaskExecutor(robot_adapter, scene)
graph = executor.execute([
    Action("pick", arm="left", object_id="A"),
    Action("place", arm="left", object_id="A", target="fixed_A"),
])
```

For a persisted run, construct `ExecutionJournal(run_directory)` and pass it
to the executor. The default evidence level is `command_trace`; it does not
claim physical success without sensor or operator verification.

Use `DobotHardwareAdapter.from_live()` for the project-specific Dobot SDK.
The adapter treats `left` and `right` as physical Nova sides, uses calibrated
joint poses when a `Pose` contains `joints`, selects Tool 0 for taught atomic
poses, and requires measured pose/joint convergence plus three consecutive
`RobotMode=5` samples after every motion. Gripper nodes require position
feedback and stabilization before the graph can advance.

## Natural-language atomic runner

Preview a natural-language task without connecting to hardware:

```bash
python -m task_skills.run_task "把物体A抓取放到目标A" --dry-run
```

Run the same task on hardware:

```bash
python -m task_skills.run_task "把物体A抓取放到目标A"
```

The runner compiles the instruction into a fine-grained atomic execution
graph, prints every node and resolved pose, and accepts hardware execution only
after the operator enters exact uppercase `YES`. Adapter construction opens
connections but does not enable a robot, send motion, or cycle a gripper.

Each run writes `execution_graph.json`, `execution_events.jsonl`, and
`execution_summary.json` under a timestamped artifact directory. Successful
controller commands, target convergence, and stabilized gripper feedback
provide `command_trace` evidence only; physical grasp and placement still
require operator or independent sensor verification.

The fixed bindings are physical left Nova → A → `target_A` and physical right
Nova → B → `target_B`. Default speed and acceleration limits are 10%. Objects
must begin at their configured initial poses; recovery from a partially
completed run is a separate explicit task.

## Gripper position convention on this rig

The Feetech driver default assumes normalized position `0` is open and `255` is
closed. On the installed hardware the direction is inverted: `0` is fully
closed and `255` is fully open (measured 2026-09-24 during the single-arm
object-A run, where `open_gripper` closed the jaws and `close_gripper` opened
them). `task_skills/layout.py` therefore pins the measured
`GRIPPER_OPEN_POSITION = 255` / `GRIPPER_CLOSED_POSITION = 0`, and
`_wait_for_gripper()` verifies travel towards the commanded target instead of a
hard-coded direction. Do not switch these back to
`gripper.get_open_position()` / `get_closed_position()` without re-measuring on
hardware.

For A, the graph returns to the calibrated left home pose, moves linearly at
the configured `415.5 mm` Tool 0 transport height, descends vertically, waits
for the gripper to stabilize, lifts to the same transport height, and only then
moves horizontally to `target_A`.

The coordinated hold-and-return instruction compiles to a dependency DAG:
both arms initialize and then pick A/B concurrently; the left arm holds A for
five seconds while the right arm places B at `target_B`; the left arm then
places A at `target_A`; finally the right arm waits for both placements, picks
B from `target_B`, and returns it to B's initial pose. The graph returns both
arms home and reads back `RobotMode=4` after normal disable operations.

The DAG executor runs at most one node per physical arm and at most two nodes
globally. Any node failure stops scheduling, attempts `StopScript()` and
`DisableRobot()` on both arms, blocks all unfinished nodes, and preserves the
failure evidence.
