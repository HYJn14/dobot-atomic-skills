# R10 beaker grasp and pour-ready demo

> **QUARANTINED:** The default real `--live` path rejects the current R09-to-R10
> candidate before asking for `YES`, acquiring a lease, or creating a hardware
> adapter. In run `run_20261015_033808_302411_ur-ready_1c0d01`, the R09 joint
> approach succeeded at measured Z 171.649 mm and J5 179.4845 degrees; the next
> R10 `MovL` reported controller alarm 26 after about 16 ms; the final snapshot
> was mode 9 and remained at R09. The controller reported a planned point near a
> wrist singularity, as defined in the [official V3 controller alarm profile](https://raw.githubusercontent.com/Dobot-Arm/TCP-IP-Python-V3/main/files/alarm_controller.json).
> R10 was not reached and the gripper close node did not run; gripper
> feedback remained at the reported open position 253. Do not rerun or
> automatically clear this alarm. The fixed Cartesian
> endpoints and existing 0.25-degree reject-only J5 margin remain unchanged;
> endpoint IK/FK results and that numerical margin do not validate the motion
> path. `--dry-run` and read-only `--preflight-only` remain available, and their
> reports mark the actual motion plan as rejected even when endpoint queries pass.

The current pose file retains each source XYZ and Rx value and all original angle signs. The R09 and R10
grasp poses use Ry 89.5 degrees and Rz shifted by -2.25 degrees; pour-ready
retains its source Rz and uses Ry 90 degrees. Use `--preflight-only` to query
the controller's seeded IK/FK solutions without moving the arm or accessing
the gripper. The query is kinematic evidence; it does not verify the mounted
tool/cup frame, table alignment, collision clearance, interpolation, or the
physical path.

The read-only preflight acquires the rig lease, opens only the physical right
dashboard on port 29999, and queries seeded IK plus positive-solution FK for the
selected targets. It passes explicit User 0 and Tool 0 values to both queries,
so the controller's current selections do not affect those calculations. It
does not issue RobotMode, ClearError, EnableRobot, User, Tool, speed, motion,
stop, or gripper commands. The lease is released and the dashboard closed after
the queries, including when a check fails. The report lists each seed, returned
joints, raw wrist deltas, and result; it makes no physical-path guarantee.

The previously documented live sequence required exact uppercase `YES`, then
acquired the rig lease and connected to the right arm and gripper. It checked that the gripper is
open and the controller is ready, configures User 0, Tool 0, and speed and
acceleration 10, then solves and round-trips every selected Cartesian target
before any approach or gripper close. Each inverse solve is seeded with that
target's original taught joint reference; the returned solution must pass the
raw wrist-continuity check and the J5 singularity-margin guard. The flow then
uses the fresh returned R09 joints for one `JointMovJ`, verifies joint and
Cartesian feedback, moves linearly to R10,
closes the right gripper at speed 10, and stays at R10. The taught references
seed the solve and continuity check; they are never used as substitute
solutions. A failed check enters the existing live stop-and-hold cleanup and
does not approach or close.

Pass `--to-pour-ready` to include the Cartesian pour-ready target in the same
seeded IK/FK preflight. After gripping, the arm lifts linearly back to R09 and
moves linearly to `[-117.5765, -344.8222, 415.5852, 176.3784, 90.0,
86.3587]`. It ends at that target with the right robot enabled and the gripper
close command active. This path does not open the gripper or perform a pour.

The measured near-level candidates are fixed in
[`beaker_r10_scene.json`](../task_skills/demos/beaker_r10_scene.json). R09 is
`[-117.5765, -344.8222, 171.6492, -175.763, 89.5, 170.2908]`; R10 is
`[-117.5764, -344.8220, 93.5212, -175.7637, 89.5, 170.2901]`; pour-ready is
unchanged at `[-117.5765, -344.8222, 415.5852, 176.3784, 90.0, 86.3587]`.
For both grasp poses, Ry changes by +6.0079 degrees and Rz by -2.25 degrees;
source XYZ, Rx values, and their signs are preserved. Pour-ready Ry changes by
+0.4372 degrees and its other values are unchanged.

Under the Z-Y-X RPY model, nominal tool-X points 0.5 degrees from world -Z for
R09 and R10, and 0 degrees for pour-ready. Treating the nominal cup/tool-X
direction as table-normal is an explicit assumption. This candidate is
approximately level under that assumption; it does not establish absolute
level or certify the controller's interpolation or complete motion path. The
saved read-only controller record in
[`near_level_query_20261015.json`](../task_skills/artifacts/near_level_query_20261015.json)
reports R09 wrist deltas `[2.864571, 6.355139, 0.707131]` degrees and J5
179.483239 degrees, and R10 deltas `[3.542251, 6.356544, 1.576153]` degrees
and J5 179.494344 degrees. The separate read-only record in
[`wrist_query_20261015.json`](../task_skills/artifacts/wrist_query_20261015.json)
confirms pour-ready remains at its current Ry 90-degree candidate; its wrist deltas are
`[0.497016, -0.000204, -0.027327]` degrees and its J5 margin is 89.981004
degrees. These records contain controller IK/FK results only; no motion was
issued as part of the queries.

The static graphs
[`beaker_r10_graph.json`](../task_skills/demos/beaker_r10_graph.json) and
[`beaker_pour_ready_graph.json`](../task_skills/demos/beaker_pour_ready_graph.json)
show the generated grasp-only and extended sequences. They contain Cartesian
targets without taught joint arrays. At execution, every pose intended in the
selected flow uses the raw controller command
`InverseSolution(x,y,z,rx,ry,rz,user,tool,1,{j1,...,j6})`, seeded by its
corresponding source joint reference. Each response must contain a zero status
and six finite joint values; its `PositiveSolution` result must match position
and rotation-matrix orientation within the adapter tolerances. The live R09
approach uses that fresh returned solution and waits for matching joint and
Cartesian feedback before descending.

The SDK's variadic `InverseSolution` wrapper does not serialize these dynamic
arguments in the required controller format, so the adapter builds the raw
command and sends it with `sendRecvMsg`. The saved read-only controller records
contain IK responses and positive-solution FK result data for the measured R09
and R10 targets and the current pour-ready candidate. Each preflight still reruns
the selected targets against the controller. These records describe kinematics
only and do not verify physical motion or the complete path.

Before `PositiveSolution`, preflight also compares raw, unwrapped J4/J5/J6
values from each fresh solution with the corresponding taught references from
the source screenshots. It rejects a per-joint difference greater than 15°;
exactly 15° is accepted, and a full 360° turn remains a 360° difference. It
also rejects J5 values less than 0.25° from any integer multiple of 180°,
including 0 and positive or negative full turns. The measured R09 and R10
candidate records have J5 margins of 0.516761° and 0.505656°. This numerical
margin is reject-only; it is not a certified safe singularity margin. The
references are R09 `[-104.0774, -40.8842, -120.9522, 52.4488, 173.1281,
-20.0117]`, R10 `[-104.073, -58.9861, -117.6177, 67.2813, 173.1378,
-19.9461]`, and pour-ready `[-90.0111, -25.5603, -117.5143, 142.7309,
90.0192, 90.0595]`. Successful preflight evidence records each seed, raw
command, returned joint solution, and wrist deltas. This is a conservative
reject-only continuity bound, not a desired-joint specification, proof of a
correct IK solution, or physical path validation. It catches the previously
rejected physical R09 solution `[-104.168291, -41.857328, -86.82728,
-49.214687, 177.512036, -87.816269]` at J4, but does not certify any candidate
that passes. Verify the mounted cup/tool and candidate path physically before
a live run.

R10 does not call blocking `Sync()` to complete a motion. A zero-status reply
from `JointMovJ` or `MovL` confirms command acceptance; the next graph step is
released only after measured feedback is stable at the target. The joint
approach requires RobotMode 5 plus matching joint and Cartesian readings, and
each linear move requires RobotMode 5 plus a matching Cartesian pose. This
avoids treating a `Sync()` socket timeout as motion failure or as completion.

Preview the default sequence and save artifacts without hardware access:

```bash
python -m task_skills.run_beaker_grasp --dry-run
```

Preview the full continuation without connecting to hardware:

```bash
python -m task_skills.run_beaker_grasp --dry-run --to-pour-ready
```

Run the read-only controller kinematics check first. Add `--to-pour-ready` to
include all three Cartesian targets:

```bash
python -m task_skills.run_beaker_grasp --preflight-only
python -m task_skills.run_beaker_grasp --preflight-only --to-pour-ready
```

On Windows, R10 uses the shared device-owner directory
`C:\DobotRuntime\device-owner` unless `DOBOT_DEVICE_LOCK_PATH` overrides it.
If lease acquisition reports that the rig is owned or quarantined, inspect the
owner and its process before retrying. The command never removes or bypasses an
existing lock.

The commands below currently return exit code 2 before prompting or connecting
to hardware because the current motion candidate is quarantined above. There is
no CLI bypass. The previously documented hardware invocation is:

```bash
python -m task_skills.run_beaker_grasp --live
python -m task_skills.run_beaker_grasp --live --to-pour-ready
```

These are historical run details for a future motion plan after the quarantined
candidate has been replaced. Before any such run, make sure the right gripper is
empty and open. The live preflight refuses arm motion when measured opening is
below 250 and never opens the gripper automatically. The right-side live adapter
uses the existing `GRIPPER_LEFT` binding (COM7, servo ID 21).

The close command requires zero serial result and zero servo error, then waits
for stable feedback after meaningful closing travel or for feedback at the
closed position. This confirms the command and gripper feedback; it does not
prove physical contact or verify the beaker was grasped. Force is not regulated
or verified. On failure or cancellation, the existing stop and verification
path stops the right arm and holds the gripper at its measured position.
There is no automatic open or recovery motion.
