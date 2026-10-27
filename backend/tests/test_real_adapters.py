"""Real Meta API paths with faked transports (no network, no keys).

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_real_adapters -v
"""
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.app.adapters import MetaAPIError
from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.adapters.sam_adapter import MetaSAMAdapter
from backend.app.adapters.voice_adapter import MetaVoiceAdapter, asr_url
from backend.app.agent_orchestrator import AgentOrchestrator
from backend.app.camera_client import CameraClient
from backend.app.change_detector import MotionSettler
from backend.app.circuit_graph import load_circuit_graph
from backend.app.models import CheckNowRequest, Mode, ProbeReading
from backend.app.serial_probe import SerialProbe

VALID_DECISION = {
    "mode": "GUIDED_ASSEMBLY", "state": "INSTRUCTING", "fault_hypothesis": None,
    "confidence": 0.9, "evidence": ["ok"], "next_instruction": "wire it",
    "speak_to_tech": "wire it now", "verification_test": "PING",
}


class FakeCompletions:
    """Scripted chat.completions: pop a reply (or raise) per create call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=item))])


class FakeOpenAI:
    def __init__(self, script, **kwargs):
        self.init_kwargs = kwargs
        self.chat = SimpleNamespace(completions=FakeCompletions(script))


def fake_urlopen_factory(body: bytes, error=None):
    seen = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body

    def fake_urlopen(req, timeout=None):
        seen["req"] = req
        seen["timeout"] = timeout
        if error is not None:
            raise error
        return FakeResp()

    return fake_urlopen, seen


class RealAdapterTests(unittest.TestCase):
    # -- Muse ---------------------------------------------------------
    def test_claude_valid_json_parsed(self):
        comp = FakeCompletions([json.dumps(VALID_DECISION)])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=SimpleNamespace(
                    chat=SimpleNamespace(completions=comp))):
            d = MetaClaudeAdapter(model="test-Muse", mock=False).decide({
                "mode": Mode.GUIDED_ASSEMBLY,
                "probe": ProbeReading(connected=True, i2c_devices=["0x3C", "0x76"]),
                "history": [{"role": "user", "content": "hi"}]})
        self.assertEqual(d.state, "INSTRUCTING")
        self.assertEqual(comp.calls[0]["model"], "test-Muse")
        user_msg = comp.calls[0]["messages"][-1]["content"]
        self.assertIn("0x76", user_msg)  # probe state forwarded
        self.assertTrue(any(m.get("content") == "hi" for m in comp.calls[0]["messages"]))

    def test_claude_retries_once_on_invalid_json(self):
        comp = FakeCompletions(["not json", json.dumps(VALID_DECISION)])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=SimpleNamespace(
                    chat=SimpleNamespace(completions=comp))):
            d = MetaClaudeAdapter(model="m", mock=False).decide({"probe": ProbeReading()})
        self.assertEqual(d.verification_test, "PING")
        self.assertEqual(len(comp.calls), 2)

    def test_claude_invalid_twice_raises(self):
        comp = FakeCompletions(["bad1", "bad2"])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=SimpleNamespace(
                    chat=SimpleNamespace(completions=comp))):
            with self.assertRaises(MetaAPIError):
                MetaClaudeAdapter(model="m", mock=False).decide({"probe": ProbeReading()})

    def test_claude_network_error_raises(self):
        comp = FakeCompletions([RuntimeError("boom"), RuntimeError("boom")])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=SimpleNamespace(
                    chat=SimpleNamespace(completions=comp))):
            with self.assertRaises(MetaAPIError):
                MetaClaudeAdapter(model="m", mock=False).decide({"probe": ProbeReading()})

    # -- SAM ------------------------------------------------------------
    def test_sam_detections_parsed(self):
        reply = json.dumps({"detections": [
            {"label": "ESP32 DevKit", "box": [1, 2, 3, 4], "confidence": 0.9}],
            "wire_count": 5, "wires_ok": True, "oled_lit": False, "notes": "n"})
        comp = FakeCompletions([reply])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=SimpleNamespace(
                    chat=SimpleNamespace(completions=comp))):
            out = MetaSAMAdapter(model="test-sam", mock=False).analyze(b"fakejpeg")
        self.assertFalse(out["mock"])
        self.assertEqual(out["detections"][0]["box"], [1, 2, 3, 4])
        self.assertEqual(out["wire_count"], 5)
        content = comp.calls[0]["messages"][0]["content"]
        self.assertTrue(any("image_url" in part for part in content))

    def test_sam_garbage_raises(self):
        comp = FakeCompletions(["no json here at all"])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=SimpleNamespace(
                    chat=SimpleNamespace(completions=comp))):
            with self.assertRaises(MetaAPIError):
                MetaSAMAdapter(model="m", mock=False).analyze(b"fakejpeg")

    # -- orchestrator fallbacks ------------------------------------------
    def _mock_orc(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return AgentOrchestrator(
            graph=load_circuit_graph(), camera=CameraClient(mock=True),
            settler=MotionSettler(), probe=SerialProbe(mock=True),
            sam=MetaSAMAdapter(mock=True), Muse=MetaClaudeAdapter(mock=True),
            voice=MetaVoiceAdapter(mock=True),
            attempt_log_path=os.path.join(tmp.name, "a.jsonl"))

    def test_claude_outage_returns_safe_retry(self):
        orc = self._mock_orc()
        orc.start_session(Mode.GUIDED_ASSEMBLY)
        for _ in range(3):
            orc.check_now(CheckNowRequest())

        def down(ctx):
            raise MetaAPIError("service down")

        orc.Muse.decide = down
        v = orc.check_now(CheckNowRequest(simulate_fault="SENSOR_DISCONNECTED"))
        self.assertEqual(v["state"], "FAULT_FOUND")  # sensor still decided
        self.assertEqual(v["step_index"], 3)  # stayed on the step
        self.assertIn("retry", v["instruction"].lower())
        self.assertIn("Meta API error", v["decision"]["evidence"][0])

    def test_sam_outage_continues_on_probe(self):
        orc = self._mock_orc()
        orc.start_session(Mode.GUIDED_ASSEMBLY)

        def down(frame, prompt=""):
            raise MetaAPIError("vision down")

        orc.sam.analyze = down
        v = orc.check_now(CheckNowRequest())
        self.assertIn("vision down", v["sam"].get("error", ""))
        self.assertTrue(v["verification"]["passed"])  # serial probe carried on

    def test_history_accumulates(self):
        orc = self._mock_orc()
        orc.start_session(Mode.GUIDED_ASSEMBLY)
        orc.check_now(CheckNowRequest(transcript="done"))
        orc.check_now(CheckNowRequest())
        hist = orc.session["history"]
        self.assertEqual(len(hist), 4)
        self.assertEqual(hist[0], {"role": "user", "content": "done"})
        self.assertEqual(hist[2], {"role": "user", "content": "(pressed CHECK NOW)"})

    # -- voice ------------------------------------------------------------
    def test_asr_url_default_and_override(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("META_ASR_URL", None)
            os.environ.pop("META_API_BASE_URL", None)
            self.assertEqual(asr_url(), "https://api.meta.ai/v1/asr/transcribe")
        with mock.patch.dict(os.environ, {"META_ASR_URL": "https://x.test/asr"}):
            self.assertEqual(asr_url(), "https://x.test/asr")

    def test_voice_transcribe_posts_audio(self):
        fake, seen = fake_urlopen_factory(b'{"text": "done"}')
        with mock.patch.dict(os.environ, {"META_API_KEY": "k"}):
            with mock.patch("urllib.request.urlopen", fake):
                out = MetaVoiceAdapter(mock=False).transcribe(b"RIFF...", "talk.wav")
        self.assertEqual(out, {"text": "done", "mock": False})
        req = seen["req"]
        self.assertIn("Bearer", req.get_header("Authorization"))
        self.assertIn("multipart/form-data", req.get_header("Content-type"))

    def test_voice_transcribe_error_raises(self):
        fake, _ = fake_urlopen_factory(b"", error=OSError("net down"))
        with mock.patch.dict(os.environ, {"META_API_KEY": "k"}):
            with mock.patch("urllib.request.urlopen", fake):
                with self.assertRaises(MetaAPIError):
                    MetaVoiceAdapter(mock=False).transcribe(b"RIFF...", "talk.wav")

    def test_voice_route_registered(self):
        from backend.app.main import app

        self.assertTrue(any(r.path == "/api/voice/transcribe"
                            and "POST" in (r.methods or []) for r in app.routes))


if __name__ == "__main__":
    unittest.main()
