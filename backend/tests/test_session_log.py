"""Hash-chained event log, passport lookup, and demo-mode fallback.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_session_log -v
"""
import os
import tempfile
import unittest
from unittest import mock

from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.adapters.sam_adapter import MetaSAMAdapter
from backend.app.adapters.voice_adapter import MetaVoiceAdapter
from backend.app.agent_orchestrator import AgentOrchestrator, verify_chain
from backend.app.camera_client import CameraClient
from backend.app.change_detector import MotionSettler
from backend.app.circuit_graph import load_circuit_graph
from backend.app.models import CheckNowRequest, Mode
from backend.app.serial_probe import SerialProbe


def make_orchestrator(log_path):
    return AgentOrchestrator(
        graph=load_circuit_graph(),
        camera=CameraClient(mock=True),
        settler=MotionSettler(settle_seconds=1.0),
        probe=SerialProbe(mock=True),
        sam=MetaSAMAdapter(mock=True),
        Muse=MetaClaudeAdapter(mock=True),
        voice=MetaVoiceAdapter(mock=True),
        attempt_log_path=log_path,
    )


class SessionLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orc = make_orchestrator(os.path.join(self.tmp.name, "attempts.jsonl"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_chain_valid_and_tamper_evident(self):
        self.orc.start_session(Mode.GUIDED_ASSEMBLY)
        for _ in range(3):
            self.orc.check_now(CheckNowRequest())
        events = self.orc.session["attempts"]
        self.assertTrue(verify_chain(events))
        for key in ("seq", "at", "prev_hash", "hash", "evidence", "decision", "verification"):
            self.assertIn(key, events[0])
        tampered = [dict(e) for e in events]
        tampered[1]["measured"] = "forged"
        self.assertFalse(verify_chain(tampered))

    def test_passport_lookup(self):
        v = self.orc.start_session(Mode.GUIDED_ASSEMBLY)
        self.orc.check_now(CheckNowRequest())
        got = self.orc.get_passport(v["id"])
        self.assertTrue(got["chain_valid"])
        self.assertEqual(got["event_count"], 1)
        self.assertFalse(got["complete"])
        self.assertIsNone(self.orc.get_passport("nope1234"))

    def test_demo_falls_back_to_mock_claude(self):
        self.orc.demo_mode = True
        self.orc.Muse = MetaClaudeAdapter(model="live", mock=False)
        self.orc.start_session(Mode.GUIDED_ASSEMBLY)
        with mock.patch.dict(os.environ, {"META_API_KEY": "k"}), mock.patch(
                "openai.OpenAI", side_effect=RuntimeError("api down")):
            v = self.orc.check_now(CheckNowRequest())
        self.assertEqual(v["fallbacks"], ["Muse"])
        self.assertTrue(v["fallback_active"])
        self.assertEqual(v["step_index"], 1)  # mock decision + passing sensor: advance

    def test_demo_off_keeps_error_fallback(self):
        self.orc.demo_mode = False
        self.orc.Muse = MetaClaudeAdapter(model="live", mock=False)
        self.orc.start_session(Mode.GUIDED_ASSEMBLY)
        with mock.patch.dict(os.environ, {"META_API_KEY": "k"}), mock.patch(
                "openai.OpenAI", side_effect=RuntimeError("api down")):
            v = self.orc.check_now(CheckNowRequest())
        self.assertEqual(v["fallbacks"], [])
        self.assertIn("retry", v["decision"]["speak_to_tech"].lower())
        self.assertIn("Meta API error", v["decision"]["evidence"][0])

    def test_camera_failure_flagged_as_fallback(self):
        self.orc.camera = CameraClient(frame_url="http://127.0.0.1:9/nope",
                                       mock=False, timeout=0.5)
        self.orc.start_session(Mode.GUIDED_ASSEMBLY)
        v = self.orc.check_now(CheckNowRequest())
        self.assertIn("camera", v["fallbacks"])
        self.assertTrue(v["fallback_active"])


if __name__ == "__main__":
    unittest.main()
