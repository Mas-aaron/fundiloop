"""Eyeball confirmations must survive the AUTO check loop.

A fresh check turn used to void confirm_pending unconditionally, so
an AUTO turn landing between the eyeball question and the learner's
CONFIRM tap made the confirm fail with "no confirmation pending".
A pending ask for the SAME step must survive re-checks; a sensor
pass still clears it.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_confirm_race -v
"""
import tempfile
import unittest
from unittest import mock

import backend.app.learning as learning
from backend.app.models import CheckNowRequest
from backend.tests.helpers import use_mock_env
from backend.tests.test_latency import make_orchestrator

PLAN = [
    {"n": 1, "instruction": "Wire the LED to GPIO2.",
     "verify": "CHECK_GPIO", "expect": {"pin": 2},
     "depends_on_parts": ["led"]},
    {"n": 2, "instruction": "Wire the BME280 SDA to GPIO21.",
     "verify": "RUN_I2C_SCAN", "expect": {},
     "depends_on_parts": ["bme280"]},
]

EYEBALL = {"test": "CHECK_GPIO", "passed": False,
           "needs_confirmation": True,
           "measured": "I can't see whether GPIO2 is blinking from here.",
           "board_state": ""}


class ConfirmSurvivesRecheckTest(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)  # full suite loads the real .env (MOCK_META=false)
        self.orc = make_orchestrator(tempfile.mkdtemp())
        self.view = self.orc.start_learning_session(
            "proj", PLAN, project_title="Blink", board="esp32_devkit")
        self.sid = self.view["id"]

    def _check(self, verification):
        with mock.patch.object(learning, "run_learning_verification",
                               return_value=verification):
            return self.orc.check_now(CheckNowRequest())

    def test_pending_survives_same_step_recheck_and_confirms(self):
        first = self._check(dict(EYEBALL))
        self.assertTrue(first.get("confirm_pending"))
        second = self._check(dict(EYEBALL))  # AUTO turn lands
        self.assertTrue(second.get("confirm_pending"))
        after = self.orc.confirm_learning_step(self.sid, True)
        self.assertEqual(after["step_index"], 1)
        self.assertIn("Next step 2", after["speak"])

    def test_advance_during_eyeball_moves_on(self):
        self._check(dict(EYEBALL))
        after = self.orc.advance_learning_step(self.sid, "move on")
        self.assertEqual(after["step_index"], 1)

    def test_sensor_pass_clears_pending(self):
        self._check(dict(EYEBALL))
        passed = {"test": "CHECK_GPIO", "passed": True,
                  "needs_confirmation": False,
                  "measured": "GPIO2 toggles (LED check).",
                  "board_state": ""}
        view = self._check(passed)
        self.assertFalse(view.get("confirm_pending"))
        with self.assertRaises(ValueError):
            self.orc.confirm_learning_step(self.sid, True)


if __name__ == "__main__":
    unittest.main()
