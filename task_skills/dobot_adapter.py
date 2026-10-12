import os
import re
import threading
import time
from math import fabs
from typing import Any, Dict, Iterable, Optional


class DobotHardwareAdapter:
    _POSITION_TOLERANCE_MM = 1.0
    _ANGLE_TOLERANCE_DEGREES = 1.0
    _STABLE_SAMPLES = 3
    _GRIPPER_STABLE_SAMPLES = 5
    _GRIPPER_STABLE_TOLERANCE = 2
    _MIN_GRIPPER_TRAVEL = 10
    _POLL_INTERVAL_SECONDS = 0.05

    def __init__(
        self,
        left: Any = None,
        right: Any = None,
        settle_timeout: float = 180.0,
        dashboards: Optional[Dict[str, Any]] = None,
        motions: Optional[Dict[str, Any]] = None,
        grippers: Optional[Dict[str, Any]] = None,
    ):
        if dashboards is None and motions is None and grippers is None:
            robots = {arm: robot for arm, robot in (("left", left), ("right", right)) if robot is not None}
            dashboards = {arm: robot.r_inter for arm, robot in robots.items()}
            motions = {arm: robot.robot for arm, robot in robots.items()}
            grippers = {arm: robot.gripper for arm, robot in robots.items() if hasattr(robot, "gripper")}
            self._legacy_robots = robots
        else:
            self._legacy_robots = {}
        self.dashboards = dict(dashboards or {})
        self.motions = dict(motions or {})
        self.grippers = dict(grippers or {})
        self._settle_timeout = float(settle_timeout)
        self._cancel_event = None
        self._commands_stopped = threading.Event()
        self._command_gates = {arm: threading.RLock() for arm in set(self.dashboards) | set(self.motions) | set(self.grippers)}

    def set_cancel_event(self, event):
        self._cancel_event = event

    def _check_cancel(self):
        if self._commands_stopped.is_set() or (self._cancel_event is not None and self._cancel_event.is_set()):
            raise InterruptedError("Execution cancellation requested")

    def _send_command(self, arm, command):
        with self._command_gates[arm]:
            self._check_cancel()
            return command()

    def _sleep(self, seconds):
        if self._cancel_event is None:
            time.sleep(seconds)
        elif self._cancel_event.wait(seconds):
            self._check_cancel()

    @classmethod
    def from_components(cls, dashboards, motions, grippers, settle_timeout: float = 180.0):
        return cls(dashboards=dashboards, motions=motions, grippers=grippers, settle_timeout=settle_timeout)

    @classmethod
    def from_live(cls, arms: Iterable[str] = ("left", "right"), settle_timeout: float = 180.0):
        from dobot_control.gripper.dobot_gripper import DobotGripper
        from dobot_control.robots.dobot_api import DobotApiDashboard, DobotApiMove
        from scripts.manipulate_utils import load_ini_data_gripper
        from .layout import physical_nova_gripper_binding, physical_nova_ip

        selected_arms = tuple(dict.fromkeys(arms))
        _, configured_grippers = load_ini_data_gripper()
        dashboards = {}
        motions = {}
        grippers = {}
        try:
            for arm in selected_arms:
                ip = physical_nova_ip(arm)
                dashboards[arm] = DobotApiDashboard(ip, 29999)
                motions[arm] = DobotApiMove(ip, 30003)
                config_key, default_windows_port = physical_nova_gripper_binding(arm)
                config = configured_grippers[config_key]
                port = os.environ.get(f"DOBOT_{arm.upper()}_GRIPPER_PORT")
                if not port:
                    port = default_windows_port if os.name == "nt" else config.port
                grippers[arm] = DobotGripper(port=port, id_name=config.id_name, servo_pos=config.pos)
        except Exception:
            cls._close_resources(dashboards, motions, grippers)
            raise
        return cls.from_components(dashboards, motions, grippers, settle_timeout)

    def close(self) -> None:
        self._close_resources(self.dashboards, self.motions, self.grippers)

    @staticmethod
    def _close_resources(dashboards, motions, grippers) -> None:
        for gripper in grippers.values():
            try:
                gripper.disconnect()
            except Exception:
                pass
        for resource in list(motions.values()) + list(dashboards.values()):
            try:
                resource.close()
            except Exception:
                pass

    def execute_atomic(self, node, scene) -> Dict[str, Any]:
        self._check_cancel()
        operation = node.operation
        arm = node.arm
        if operation == "wait_controller_ready":
            return self.wait_controller_ready(arm, node.timeout_seconds)
        if operation == "clear_error":
            return self._dashboard_command(arm, "ClearError")
        if operation == "enable_robot":
            evidence = self._dashboard_command(arm, "EnableRobot")
            self._wait_until_idle(arm, node.timeout_seconds)
            evidence.update(self.snapshot(arm))
            return evidence
        if operation == "configure_robot":
            return self.configure_robot(arm, int(node.parameters.get("speed_percent", 10)))
        if operation == "barrier":
            return {"dependencies_satisfied": True}
        if operation == "wait":
            seconds = float(node.parameters["seconds"])
            started = time.monotonic()
            self._sleep(seconds)
            return {"requested_seconds": seconds, "elapsed_seconds": time.monotonic() - started}
        if operation == "disable_robot":
            evidence = self._dashboard_command(arm, "DisableRobot")
            self._wait_for_mode(arm, 4, node.timeout_seconds)
            evidence.update(self.snapshot(arm))
            return evidence
        if operation == "open_gripper":
            return self.open_gripper(arm, timeout=node.timeout_seconds)
        if operation == "close_gripper":
            return self.close_gripper(arm, timeout=node.timeout_seconds)
        if operation == "move_safe_pose":
            self.move_to_safe_pose(arm, timeout=node.timeout_seconds)
            return self.snapshot(arm)
        if operation == "move_pose":
            pose = self.resolve_pose_ref(scene, node.parameters["pose_ref"], arm=arm)
            self.move_to(arm, pose, motion=node.parameters["motion"], timeout=node.timeout_seconds)
            evidence = self.snapshot(arm)
            evidence.update({"pose_ref": node.parameters["pose_ref"], "motion": node.parameters["motion"]})
            return evidence
        raise ValueError(f"Unsupported atomic operation: {operation}")

    def wait_controller_ready(self, arm: str, timeout: float) -> Dict[str, Any]:
        dashboard = self._dashboard(arm)
        deadline = time.monotonic() + float(timeout)
        last_mode = None
        while time.monotonic() < deadline:
            try:
                last_mode = self._mode_value(dashboard.RobotMode())
                if last_mode != 3:
                    return self.snapshot(arm)
            except (OSError, TimeoutError, ConnectionError):
                pass
            self._sleep(0.5)
        raise TimeoutError(f"Dobot {arm} remained unavailable or powered off: mode={last_mode}")

    def configure_robot(self, arm: str, speed_percent: int = 10) -> Dict[str, Any]:
        dashboard = self._dashboard(arm)
        replies = {}
        for name, arguments in (
            ("SpeedFactor", (speed_percent,)), ("AccJ", (speed_percent,)),
            ("SpeedJ", (speed_percent,)), ("AccL", (speed_percent,)),
            ("SpeedL", (speed_percent,)), ("Tool", (0,)),
        ):
            reply = self._send_command(arm, lambda: getattr(dashboard, name)(*arguments))
            self._require_success(f"{arm} {name}", reply)
            replies[name] = str(reply)
        return {"speed_percent": speed_percent, "replies": replies}

    def move_to(self, arm: str, pose, motion: Optional[str] = None, timeout: Optional[float] = None) -> None:
        self._check_cancel()
        motion_api = self._motion(arm)
        selected_motion = motion or ("calibrated_joint" if pose.joints else "joint")
        if selected_motion == "calibrated_joint":
            if not pose.joints:
                raise ValueError("calibrated_joint motion requires pose.joints")
            reply = self._send_command(arm, lambda: motion_api.JointMovJ(*pose.joints))
        elif selected_motion == "joint":
            reply = self._send_command(arm, lambda: motion_api.MovJ(pose.x, pose.y, pose.z, pose.rx, pose.ry, pose.rz))
        elif selected_motion == "linear":
            reply = self._send_command(arm, lambda: motion_api.MovL(pose.x, pose.y, pose.z, pose.rx, pose.ry, pose.rz))
        else:
            raise ValueError(f"Unsupported motion mode: {selected_motion}")
        self._require_success(f"{arm} {selected_motion} move", reply)
        if hasattr(motion_api, "Sync"):
            self._require_success(f"{arm} sync", self._send_command(arm, motion_api.Sync))
        if selected_motion == "calibrated_joint":
            self._wait_for_joint_target(arm, pose.joints, timeout)
        else:
            self._wait_for_pose_target(arm, pose, timeout)

    def open_gripper(self, arm: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        self._check_cancel()
        from .layout import GRIPPER_OPEN_POSITION

        gripper = self._gripper(arm)
        start = self._gripper_position(arm)
        target = int(GRIPPER_OPEN_POSITION)
        self._require_success(f"{arm} gripper open", self._send_command(arm, lambda: gripper.move(target, 30, 50)))
        return self._wait_for_gripper(arm, start, target, closing=False, timeout=timeout)

    def close_gripper(self, arm: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        self._check_cancel()
        from .layout import GRIPPER_CLOSED_POSITION

        gripper = self._gripper(arm)
        start = self._gripper_position(arm)
        target = int(GRIPPER_CLOSED_POSITION)
        self._require_success(f"{arm} gripper close", self._send_command(arm, lambda: gripper.move(target, 30, 50)))
        return self._wait_for_gripper(arm, start, target, closing=True, timeout=timeout)

    def move_to_safe_pose(self, arm: str, timeout: Optional[float] = None) -> None:
        self._check_cancel()
        from .layout import physical_nova_home_degrees

        motion_api = self._motion(arm)
        self._require_success(f"{arm} safe pose", self._send_command(arm, lambda: motion_api.JointMovJ(*physical_nova_home_degrees(arm))))
        if hasattr(motion_api, "Sync"):
            self._require_success(f"{arm} safe pose sync", self._send_command(arm, motion_api.Sync))
        self._wait_for_joint_target(arm, physical_nova_home_degrees(arm), timeout)

    def stop(self, arm: str) -> None:
        dashboard = self._dashboard(arm)
        if hasattr(dashboard, "StopScript"):
            self._require_success(f"{arm} stop", dashboard.StopScript())

    def disable(self, arm: str) -> Dict[str, Any]:
        return self._dashboard_command(arm, "DisableRobot")

    def stop_all(self) -> Dict[str, Any]:
        results = {}
        for arm in self.dashboards:
            arm_result = {}
            try:
                self.stop(arm)
                arm_result["stop"] = "accepted"
            except Exception as error:
                arm_result["stop"] = f"{type(error).__name__}: {error}"
            try:
                arm_result["disable"] = self.disable(arm)
            except Exception as error:
                arm_result["disable"] = f"{type(error).__name__}: {error}"
            results[arm] = arm_result
        return results

    def snapshot(self, arm: str) -> Dict[str, Any]:
        dashboard = self._dashboard(arm)
        value = {"arm": arm, "mode": self._mode_value(dashboard.RobotMode()), "errors": str(dashboard.GetErrorID())}
        if hasattr(dashboard, "GetPose"):
            value["pose"] = str(dashboard.GetPose())
        if hasattr(dashboard, "GetAngle"):
            value["joints"] = str(dashboard.GetAngle())
        gripper = self.grippers.get(arm)
        if gripper is not None and hasattr(gripper, "get_current_position"):
            value["gripper_position"] = self._gripper_position(arm)
        return value

    def snapshot_all(self) -> Dict[str, Any]:
        return {arm: self.snapshot(arm) for arm in self.dashboards}

    def stop_and_verify(self):
        """Attempt every arm independently and hold each gripper at measured position.

        Verification must continue after the cancellation flag is set. Never
        open a gripper or home an arm as an automatic failure response.
        """
        from concurrent.futures import ThreadPoolExecutor
        self._commands_stopped.set()

        def confirm(arm):
            dashboard = self.dashboards[arm]
            errors = []
            try:
                with self._command_gates[arm]:
                    reply = dashboard.StopScript()
                if not re.match(r"^\s*0\s*,", str(reply)):
                    raise RuntimeError("Missing stop acknowledgement")
            except Exception as error:
                errors.append("arm_stop:" + type(error).__name__)
            gripper = self.grippers.get(arm)
            hold_target = None
            if gripper is not None:
                try:
                    hold_target = self._gripper_position(arm)
                    with self._command_gates[arm]:
                        self._require_success("gripper hold", gripper.move(hold_target, 30, 50))
                except Exception as error:
                    errors.append("gripper_hold:" + type(error).__name__)
            stable_arm, stable_gripper = 0, 0
            previous = None
            deadline = time.monotonic() + 10.0
            try:
                while time.monotonic() < deadline:
                    mode = self._mode_value(dashboard.RobotMode())
                    stable_arm = stable_arm + 1 if mode in (4, 5) else 0
                    if gripper is None:
                        stable_gripper = self._GRIPPER_STABLE_SAMPLES
                    elif hold_target is not None:
                        measured = self._gripper_position(arm)
                        near = abs(measured - hold_target) <= self._GRIPPER_STABLE_TOLERANCE
                        stable_gripper = stable_gripper + 1 if near and previous is not None and abs(measured - previous) <= self._GRIPPER_STABLE_TOLERANCE else 0
                        previous = measured
                    if stable_arm >= self._STABLE_SAMPLES and stable_gripper >= self._GRIPPER_STABLE_SAMPLES:
                        return {"stop_confirmed": not errors, "mode": mode, "errors": errors,
                                "gripper_hold_position": hold_target}
                    time.sleep(self._POLL_INTERVAL_SECONDS)
            except Exception as error:
                errors.append("feedback:" + type(error).__name__)
            return {"stop_confirmed": False, "errors": errors or ["STOP_TIMEOUT"]}

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="dobot-stop") as pool:
            arms = list(self.dashboards)
            results = dict(zip(arms, pool.map(confirm, arms)))
        return {"confirmed": bool(results) and all(r["stop_confirmed"] for r in results.values()), "arms": results}

    @staticmethod
    def resolve_pose_ref(scene, reference: str, height: float = 50.0, arm: Optional[str] = None):
        from .models import resolve_pose_ref

        return resolve_pose_ref(scene, reference, height, arm=arm)

    def _dashboard_command(self, arm: str, name: str) -> Dict[str, Any]:
        reply = self._send_command(arm, lambda: getattr(self._dashboard(arm), name)())
        self._require_success(f"{arm} {name}", reply)
        return {"command": name, "reply": str(reply)}

    def _wait_until_idle(self, arm: str, timeout: Optional[float] = None) -> None:
        dashboard = self._dashboard(arm)
        deadline = time.monotonic() + (self._settle_timeout if timeout is None else float(timeout))
        stable_samples = 0
        while time.monotonic() < deadline:
            mode = self._mode_value(dashboard.RobotMode())
            if mode in (3, 9):
                errors = dashboard.GetErrorID() if hasattr(dashboard, "GetErrorID") else "unavailable"
                raise RuntimeError(f"Dobot {arm} entered mode {mode}: {errors}")
            if mode == 5:
                errors = dashboard.GetErrorID() if hasattr(dashboard, "GetErrorID") else None
                if errors is not None and self._has_alarm(errors):
                    raise RuntimeError(f"Dobot {arm} reported alarms: {errors}")
                stable_samples += 1
                if stable_samples >= self._STABLE_SAMPLES:
                    return
            else:
                stable_samples = 0
            self._sleep(self._POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"Dobot {arm} did not settle within timeout: {dashboard.RobotMode()}")

    def _wait_for_mode(self, arm: str, expected: int, timeout: Optional[float] = None) -> None:
        dashboard = self._dashboard(arm)
        deadline = time.monotonic() + (self._settle_timeout if timeout is None else float(timeout))
        stable_samples = 0
        last_mode = None
        while time.monotonic() < deadline:
            last_mode = self._mode_value(dashboard.RobotMode())
            self._raise_for_bad_mode_or_alarm(arm, dashboard, last_mode)
            if last_mode == expected:
                stable_samples += 1
                if stable_samples >= self._STABLE_SAMPLES:
                    return
            else:
                stable_samples = 0
            self._sleep(self._POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"Dobot {arm} did not enter mode {expected}: mode={last_mode}")

    def _wait_for_pose_target(self, arm: str, pose, timeout: Optional[float]) -> None:
        target = (pose.x, pose.y, pose.z, pose.rx, pose.ry, pose.rz)
        self._wait_for_motion_target(arm, "GetPose", target, 3, timeout)

    def _wait_for_joint_target(self, arm: str, joints, timeout: Optional[float]) -> None:
        self._wait_for_motion_target(arm, "GetAngle", tuple(joints), 0, timeout)

    def _wait_for_motion_target(
        self,
        arm: str,
        query_name: str,
        target,
        linear_dimensions: int,
        timeout: Optional[float],
    ) -> None:
        dashboard = self._dashboard(arm)
        if not hasattr(dashboard, query_name):
            raise RuntimeError(f"Dobot {arm} cannot verify motion completion without {query_name}()")
        deadline = time.monotonic() + (self._settle_timeout if timeout is None else float(timeout))
        stable_samples = 0
        last_values = None
        last_mode = None
        while time.monotonic() < deadline:
            last_mode = self._mode_value(dashboard.RobotMode())
            self._raise_for_bad_mode_or_alarm(arm, dashboard, last_mode)
            last_values = self._numeric_payload(getattr(dashboard, query_name)())
            at_target = len(last_values) >= len(target) and self._target_matches(
                last_values[:len(target)], target, linear_dimensions
            )
            if last_mode == 5 and at_target:
                stable_samples += 1
                if stable_samples >= self._STABLE_SAMPLES:
                    return
            else:
                stable_samples = 0
            self._sleep(self._POLL_INTERVAL_SECONDS)
        raise TimeoutError(
            f"Dobot {arm} did not reach target: mode={last_mode}, measured={last_values}, target={tuple(target)}"
        )

    def _wait_for_gripper(
        self,
        arm: str,
        start: int,
        target: int,
        closing: bool,
        timeout: Optional[float],
    ) -> Dict[str, Any]:
        # This rig inverts the Feetech convention (0 = closed, 255 = open), so
        # completion is judged by travel towards the commanded target rather
        # than by a hard-coded direction.
        direction = 1 if target >= start else -1
        deadline = time.monotonic() + (self._settle_timeout if timeout is None else float(timeout))
        previous = start
        stable_samples = 1
        last = start
        while time.monotonic() < deadline:
            last = self._gripper_position(arm)
            if abs(last - previous) <= self._GRIPPER_STABLE_TOLERANCE:
                stable_samples += 1
            else:
                stable_samples = 1
            travel = (last - start) * direction
            reached = (
                travel >= self._MIN_GRIPPER_TRAVEL
                or abs(last - target) <= self._GRIPPER_STABLE_TOLERANCE
            )
            if stable_samples >= self._GRIPPER_STABLE_SAMPLES and reached:
                return {
                    "gripper_command_accepted": True,
                    "gripper_target": "closed" if closing else "open",
                    "gripper_start_position": start,
                    "gripper_position": last,
                    "gripper_travel": max(0, travel),
                    "gripper_stable_samples": stable_samples,
                }
            previous = last
            self._sleep(self._POLL_INTERVAL_SECONDS)
        raise TimeoutError(
            f"Dobot {arm} gripper did not {'close' if closing else 'open'} and stabilize: "
            f"start={start}, last={last}, target={target}"
        )

    def _gripper_position(self, arm: str) -> int:
        gripper = self._gripper(arm)
        if not hasattr(gripper, "get_current_position"):
            raise RuntimeError(f"Dobot {arm} gripper cannot verify completion without position feedback")
        with self._command_gates[arm]:
            return int(gripper.get_current_position())

    def _raise_for_bad_mode_or_alarm(self, arm: str, dashboard: Any, mode: int) -> None:
        if mode in (3, 9):
            errors = dashboard.GetErrorID() if hasattr(dashboard, "GetErrorID") else "unavailable"
            raise RuntimeError(f"Dobot {arm} entered mode {mode}: {errors}")
        if mode == 5 and hasattr(dashboard, "GetErrorID"):
            errors = dashboard.GetErrorID()
            if self._has_alarm(errors):
                raise RuntimeError(f"Dobot {arm} reported alarms: {errors}")

    @classmethod
    def _target_matches(cls, measured, target, linear_dimensions: int) -> bool:
        for index, (actual, expected) in enumerate(zip(measured, target)):
            if index < linear_dimensions:
                difference = fabs(actual - expected)
                tolerance = cls._POSITION_TOLERANCE_MM
            else:
                difference = cls._angular_distance(actual, expected)
                tolerance = cls._ANGLE_TOLERANCE_DEGREES
            if difference > tolerance:
                return False
        return True

    @staticmethod
    def _angular_distance(actual: float, expected: float) -> float:
        return abs((actual - expected + 180.0) % 360.0 - 180.0)

    @staticmethod
    def _numeric_payload(reply: Any):
        match = re.search(r"\{([^{}]*)\}", str(reply))
        if not match:
            raise RuntimeError(f"Unable to parse numeric reply: {reply}")
        try:
            return tuple(float(value.strip()) for value in match.group(1).split(",") if value.strip())
        except ValueError as error:
            raise RuntimeError(f"Unable to parse numeric reply: {reply}") from error

    def _dashboard(self, arm: str) -> Any:
        try:
            return self.dashboards[arm]
        except KeyError as error:
            raise ValueError(f"Unknown physical arm: {arm}") from error

    def _motion(self, arm: str) -> Any:
        try:
            return self.motions[arm]
        except KeyError as error:
            raise ValueError(f"Unknown physical arm: {arm}") from error

    def _gripper(self, arm: str) -> Any:
        try:
            return self.grippers[arm]
        except KeyError as error:
            raise ValueError(f"No gripper configured for physical arm: {arm}") from error

    @staticmethod
    def _mode_value(reply: Any) -> int:
        match = re.search(r"\{\s*(-?\d+(?:\.\d+)?)\s*\}", str(reply))
        if not match:
            raise RuntimeError(f"Unable to parse RobotMode reply: {reply}")
        return int(float(match.group(1)))

    @staticmethod
    def _has_alarm(reply: Any) -> bool:
        text = str(reply)
        payload_start = text.find("{")
        payload_end = text.rfind("}")
        payload = text[payload_start + 1:payload_end] if payload_start >= 0 and payload_end > payload_start else text
        return bool(re.search(r"\b[1-9]\d*\b", payload))

    @staticmethod
    def _require_success(label: str, reply: Any) -> None:
        if isinstance(reply, tuple) and reply and isinstance(reply[0], bool):
            if not reply[0]:
                raise RuntimeError(f"{label} failed: {reply}")
            return
        if str(reply).lstrip().startswith(("-", "1,")):
            raise RuntimeError(f"{label} failed: {reply}")
