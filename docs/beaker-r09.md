# R09 beaker approach demo

This dedicated runner builds a local `AtomicExecutionGraph` (schema `2.0`)
for the physical right arm. It opens only the right Dobot dashboard and motion
channels, configures User 0, Tool 0, and all speed/acceleration limits to 10,
then sends one `JointMovJ` to the taught R09 pose. It does not initialize or
command a gripper, home either arm, or disable the right arm at the end.

The default command is offline and writes a preview, `execution_graph.json`,
events, and a summary under `task_skills/artifacts/beaker-r09/`:

```bash
python -m task_skills.run_beaker_approach --dry-run
```

Hardware mode prompts for exact uppercase `YES` before acquiring the device
lease or constructing the direct dashboard/motion adapter:

```bash
python -m task_skills.run_beaker_approach --live
```

`task_skills/demos/beaker_r09_scene.json` is the narrowly validated source for
this scene. It permits finite numeric Cartesian and six-joint values while
pinning the arm to `right`, motion to `calibrated_joint`, User and Tool to 0,
and every speed limit to 10. The exported
[`beaker_r09_graph.json`](../task_skills/demos/beaker_r09_graph.json) records
the exact target values and requires successful configuration before its sole
move node.

The graph is the local atomic graph schema `2.0`; it is not the HTTP gateway's
`dobot-dag/1` package format. `JointMovJ` interpolates in joint space from the
robot's current position. The graph verifies the final joint feedback and
controller idle state, but it provides no path guarantee. The successful end
state leaves the right arm enabled at the R09 target.
