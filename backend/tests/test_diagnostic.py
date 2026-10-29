"""Diagnostic mode, end to end in mock mode (no hardware, no sockets).

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_diagnostic -v
"""
import os
import tempfile
import unittest

from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.adapters.sam_adapter import MetaSAMAdapter
from backend.app.adapters.voice_adapter import MetaVoiceAdapter
from backend.app.agent_orchestrator import AgentOrchestrator
from backend.app.change_detector import MotionSettler
from backend.app.circuit_graph import load_circuit_graph
from backend.app.models import CheckNowRequest, Mode
from backend.app.serial_probe import SerialProbe
from backend.tests.helpers import make_camera, use_mock_env


def make_orchestrator(log_path, tmpdir):
    return AgentOrchestrator(
        graph=load_circuit_graph(),
        camera=make_camera(tmpdir),
        settler=MotionSettler(settle_seconds=1.0),
        probe=SerialProbe(mock=True),
        sam=MetaSAMAdapter(mock=True),
        Muse=MetaClaudeAdapter(mock=True),
        voice=MetaVoiceAdapter(mock=True),
        attempt_log_path=log_path,
    )


class DiagnosticTest(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.orc = make_orchestrator(
            os.path.join(self.tmp.name, "attempts.jsonl"), self.tmp.name)
        self.orc.start_session(Mode.DIAGNOSTIC)

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_captures_baseline(self):
        base = self.orc.session["baseline"]
        self.assertEqual(base["i2c_devices"], ["0x3C", "0x76"])
        self.assertTrue(base["connected"])
        self.assertIsNotNone(base["temperature_c"])
        self.assertTrue(self.orc.session["measurements_before"])

    def test_sensor_fault_ranked_top_and_verified(self):
        v = self.orc.check_now(CheckNowRequest(simulate_fault="SENSOR_DISCONNECTED"))
        self.assertEqual(v["state"], "FAULT_FOUND")
        self.assertEqual(v["ranking"][0]["fault_id"], "SENSOR_DISCONNECTED")
        self.assertEqual(v["decision"]["fault_hypothesis"], "SENSOR_DISCONNECTED")
        self.assertFalse(v["verification"]["passed"])  # vs known-good S8 ref
        self.assertFalse(v["terminal"]["READ_SENSOR"]["passed"])

    def test_code_pin_mismatch_cites_evidence(self):
        v = self.orc.check_now(CheckNowRequest(
            code_snippet="#define LED_PIN 4", simulate_fault="LED_REVERSED"))
        self.assertEqual(v["ranking"][0]["fault_id"], "CODE_PIN_MISMATCH")
        self.assertEqual(v["decision"]["fault_hypothesis"], "CODE_PIN_MISMATCH")
        evidence = " ".join(v["decision"]["evidence"])
        self.assertIn("GPIO 4", evidence)
        self.assertIn("GPIO 2", evidence)

    def test_moves_to_next_ranked_fault_when_disproven(self):
        v1 = self.orc.check_now(CheckNowRequest(simulate_fault="SENSOR_DISCONNECTED"))
        self.assertEqual(v1["decision"]["fault_hypothesis"], "SENSOR_DISCONNECTED")
        v2 = self.orc.check_now(CheckNowRequest(simulate_fault="LED_REVERSED"))
        self.assertEqual(v2["state"], "FAULT_FOUND")
        self.assertEqual(v2["decision"]["fault_hypothesis"], "LED_REVERSED")
        self.assertIn("disproven", " ".join(v2["decision"]["evidence"]))
        self.assertEqual(
            sorted(self.orc.session["faults_found"]),
            ["LED_REVERSED", "SENSOR_DISCONNECTED"])

    def test_led_fault_blocks_end_until_fixed(self):
        v = self.orc.check_now(CheckNowRequest(simulate_fault="LED_REVERSED"))
        self.assertTrue(v["terminal"]["READ_SENSOR"]["passed"])
        self.assertTrue(v["terminal"]["CHECK_OLED"]["passed"])
        self.assertEqual(v["state"], "FAULT_FOUND")  # ranked fault still open
        v = self.orc.check_now(CheckNowRequest())  # fix applied
        self.assertEqual(v["state"], "VERIFIED_FIXED")
        self.assertIsNotNone(v["passport"])

    def test_repair_passport_has_before_after_evidence(self):
        self.orc.check_now(CheckNowRequest(simulate_fault="SENSOR_DISCONNECTED"))
        v = self.orc.check_now(CheckNowRequest())
        self.assertEqual(v["state"], "VERIFIED_FIXED")
        pp = v["passport"]
        self.assertEqual(pp["mode"], "DIAGNOSTIC")
        self.assertEqual(pp["faults_found"], ["SENSOR_DISCONNECTED"])
        # As-found baseline, faulty attempt, verified fix: the full trail.
        self.assertEqual(pp["measurements_before"]["i2c_devices"], ["0x3C", "0x76"])
        first, second = v["attempts"]
        self.assertFalse(first["passed"])
        self.assertEqual(first["fault_hypothesis"], "SENSOR_DISCONNECTED")
        self.assertIn("MISSING", first["measured"])
        self.assertTrue(second["passed"])
        self.assertEqual(pp["measurements_after"]["i2c_devices"], ["0x3C", "0x76"])
        self.assertEqual(pp["total_attempts"], 2)


if __name__ == "__main__":
    unittest.main()
