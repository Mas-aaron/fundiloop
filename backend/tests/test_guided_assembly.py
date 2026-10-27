"""Guided assembly mode, end to end in mock mode (no hardware, no sockets).

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_guided_assembly -v
"""
import json
import os
import tempfile
import unittest

from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.adapters.sam_adapter import MetaSAMAdapter
from backend.app.adapters.voice_adapter import MetaVoiceAdapter
from backend.app.agent_orchestrator import AgentOrchestrator
from backend.app.camera_client import CameraClient
from backend.app.change_detector import MotionSettler
from backend.app.circuit_graph import load_circuit_graph
from backend.app.models import CheckNowRequest, Mode
from backend.app.serial_probe import SerialProbe


class GuidedAssemblyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = os.path.join(self.tmp.name, "attempts.jsonl")
        self.orc = AgentOrchestrator(
            graph=load_circuit_graph(),
            camera=CameraClient(mock=True),
            settler=MotionSettler(settle_seconds=1.0),
            probe=SerialProbe(mock=True),
            sam=MetaSAMAdapter(mock=True),
            Muse=MetaClaudeAdapter(mock=True),
            voice=MetaVoiceAdapter(mock=True),
            attempt_log_path=self.log_path,
        )
        self.orc.start_session(Mode.GUIDED_ASSEMBLY)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_run_passes_and_issues_passport(self):
        v = None
        for _ in range(8):
            v = self.orc.check_now(CheckNowRequest())
            self.assertTrue(v["verification"]["passed"], v["verification"])
        self.assertEqual(v["state"], "COMPLETE")
        self.assertEqual(v["passport"]["steps_completed"], 8)
        self.assertEqual(len(v["attempts"]), 8)
        self.assertTrue(all(a["passed"] for a in v["attempts"]))

    def test_failure_stays_and_corrects_from_evidence(self):
        for _ in range(3):  # reach S4_BME280_WIRING (index 3)
            self.orc.check_now(CheckNowRequest())
        v = self.orc.check_now(CheckNowRequest(simulate_fault="SENSOR_DISCONNECTED"))
        self.assertEqual(v["state"], "FAULT_FOUND")
        self.assertEqual(v["step_index"], 3)  # stays on the same step
        self.assertEqual(v["decision"]["fault_hypothesis"], "SENSOR_DISCONNECTED")
        evidence = " ".join(v["decision"]["evidence"])
        self.assertIn("MISSING", evidence)  # failed-test measurement reaches Muse
        self.assertTrue(v["decision"]["next_instruction"])
        v = self.orc.check_now(CheckNowRequest())  # fix verified, advance
        self.assertTrue(v["verification"]["passed"])
        self.assertEqual(v["step_index"], 4)

    def test_transcript_done_is_logged(self):
        v = self.orc.check_now(CheckNowRequest(transcript="done"))
        self.assertEqual(v["attempts"][-1]["transcript"], "done")

    def test_attempt_log_jsonl_matches_memory(self):
        self.orc.check_now(CheckNowRequest(transcript="done"))
        self.orc.check_now(CheckNowRequest())
        with open(self.log_path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        mem = self.orc.session["attempts"]
        self.assertEqual(len(lines), len(mem))
        for entry in lines:
            for key in ("at", "turn", "step_id", "test", "passed", "measured"):
                self.assertIn(key, entry)

    def test_passport_counts_attempts(self):
        for _ in range(3):
            self.orc.check_now(CheckNowRequest())
        self.orc.check_now(CheckNowRequest(simulate_fault="SENSOR_DISCONNECTED"))
        v = None
        for _ in range(5):  # S4 retry + S5..S8
            v = self.orc.check_now(CheckNowRequest())
        self.assertEqual(v["state"], "COMPLETE")
        self.assertEqual(v["passport"]["total_attempts"], 9)
        self.assertEqual(v["passport"]["failed_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
