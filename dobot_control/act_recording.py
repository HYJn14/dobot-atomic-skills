"""ACT telemetry helpers; no robot commands or training dependencies."""
import json
import threading
import time
from pathlib import Path

import numpy as np


class GripperFeedback:
    """Serialize read/write transactions; observation reads use only this cache."""

    def __init__(self, gripper, stop):
        self.gripper, self.stop = gripper, stop
        self.value, self.timestamp, self.error = float('nan'), 0.0, 'not sampled'
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                # Never start a read while a command owns the serial bus.
                if self.gripper.command_lock.acquire(blocking=False):
                    try:
                        raw = self.gripper.get_current_position()
                    finally:
                        self.gripper.command_lock.release()
                    if not 0 <= raw <= 255:
                        raise ValueError(f'gripper position out of range: {raw}')
                    with self.lock:
                        self.value, self.timestamp, self.error = raw / 255., time.perf_counter(), ''
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)
            self.stop.wait(.1)

    def snapshot(self):
        with self.lock:
            return self.value, self.timestamp, not self.error


class CameraSnapshots:
    def __init__(self):
        self.lock, self.frames = threading.Lock(), {}

    def publish(self, index, jpeg, received):
        with self.lock:
            previous = self.frames.get(index, (None, 0, 0))
            self.frames[index] = (jpeg, received, previous[2] + 1)

    def snapshot(self, indices):
        with self.lock:
            return tuple(self.frames[i] for i in indices)


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def enrich_observation(obs, frames, arm_flags, gripper_flags, command_time):
    """Copy pre-command measurements before control code mutates its own obs."""
    result = {k: np.asarray(v).copy() for k, v in obs.items()}
    result['joint_positions'] = result.pop('dataset_joint_positions')
    result['_act_schema'] = np.asarray(2)
    result['_act_camera_time'] = np.array([f[1] for f in frames])
    result['_act_camera_id'] = np.array([f[2] for f in frames])
    result['_act_arm_flags'] = np.asarray(arm_flags).copy()
    result['_act_gripper_flags'] = np.asarray(gripper_flags).copy()
    result['_act_command_time'] = np.asarray(command_time)
    return result
