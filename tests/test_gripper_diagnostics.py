import unittest
from contextlib import redirect_stdout
from io import StringIO

from dobot_control.gripper.dobot_gripper import COMM_SUCCESS, DobotGripper


class FakeServo:
    def __init__(self, response):
        self.response = response
        self.servo_ids = []

    def ReadPos(self, servo_id):
        self.servo_ids.append(servo_id)
        return self.response


def make_gripper(response):
    gripper = DobotGripper.__new__(DobotGripper)
    gripper.servo = FakeServo(response)
    gripper._servo_id = 22
    gripper._min_servo_pos = 2000
    gripper._max_servo_pos = 4000
    gripper._min_position = 0
    gripper._max_position = 255
    return gripper


class GripperDiagnosticsTests(unittest.TestCase):
    def test_communication_failure_reports_both_codes_and_servo_id(self):
        gripper = make_gripper((2500, -3001, 0))

        with self.assertRaises(ValueError) as raised:
            gripper.get_current_position()

        message = str(raised.exception)
        self.assertIn("servo ID 22", message)
        self.assertIn("communication failure", message)
        self.assertIn("comm_result=-3001", message)
        self.assertIn("servo_error=0", message)
        self.assertNotIn("servo/device error", message)
        self.assertEqual(gripper.servo.servo_ids, [22])

    def test_servo_error_is_reported_even_when_communication_succeeds(self):
        gripper = make_gripper((2500, COMM_SUCCESS, 4))

        with self.assertRaises(ValueError) as raised:
            gripper.get_current_position()

        message = str(raised.exception)
        self.assertIn("servo ID 22", message)
        self.assertIn("servo/device error", message)
        self.assertIn(f"comm_result={COMM_SUCCESS}", message)
        self.assertIn("servo_error=4", message)
        self.assertNotIn("communication failure", message)

    def test_successful_read_keeps_existing_position_conversion(self):
        gripper = make_gripper((3000, COMM_SUCCESS, 0))

        self.assertEqual(gripper.get_current_position(), 127)

    def test_disconnect_message_does_not_claim_servo_release(self):
        class FakePortHandler:
            def __init__(self):
                self.closed = False

            def closePort(self):
                self.closed = True

        gripper = DobotGripper.__new__(DobotGripper)
        port_handler = FakePortHandler()
        gripper._portHandler = port_handler
        gripper._packHandler = object()
        gripper.servo = object()
        output = StringIO()

        with redirect_stdout(output):
            gripper.disconnect()

        self.assertTrue(port_handler.closed)
        self.assertEqual(output.getvalue().strip(), "Closing gripper serial port (no open command sent)")


if __name__ == "__main__":
    unittest.main()
