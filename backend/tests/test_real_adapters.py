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
from backend.app.change_detector import MotionSettler
from backend.app.circuit_graph import load_circuit_graph
from backend.app.models import CheckNowRequest, Mode, ProbeReading
from backend.app.serial_probe import SerialProbe
from backend.tests.helpers import FakeSAMClient

VALID_DECISION = {
    "mode": "GUIDED_ASSEMBLY", "state": "INSTRUCTING", "fault_hypothesis": None,
    "confidence": 0.9, "evidence": ["ok"], "next_instruction": "wire it",
    "speak_to_tech": "wire it now", "verification_test": "PING",
}


class FakeRaw:
    """Mimics openai's raw response: status + headers + parse()."""

    status_code = 200
    headers = {"x-request-id": "req-test-1"}

    def __init__(self, content):
        self._content = content

    def parse(self):
        return SimpleNamespace(
            id="chatcmpl-test",
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))])


class FakeCompletions:
    """Scripted chat.completions: pop a reply (or raise) per create call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.with_raw_response = self._Raw(self)

    class _Raw:
        def __init__(self, outer):
            self._outer = outer

        def create(self, **kwargs):
            outer = self._outer
            outer.calls.append(kwargs)
            item = outer.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return FakeRaw(item)

    def create(self, **kwargs):
        return self.with_raw_response.create(**kwargs).parse()


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
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "MODEL_CALLS_LOG": os.path.join(self.tmp.name, "model_calls.jsonl"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

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

    # -- SAM (streamed cookbook requests) -------------------------------
    def test_sam_request_matches_cookbook_shape(self):
        from backend.tests.helpers import (
            install_sam_parser_stub,
            sam_box,
            sam_event_stream,
            sam_result,
        )

        install_sam_parser_stub(self, [sam_result([sam_box()])])
        client = FakeSAMClient([sam_event_stream()])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=client):
            out = MetaSAMAdapter(model="test-sam", mock=False).analyze(
                b"fakejpeg", concepts=[("duck", "duck")])
        self.assertFalse(out["mock"])
        self.assertEqual(len(client.responses.calls), 1)
        call = client.responses.calls[0]
        self.assertEqual(call["model"], "test-sam")
        self.assertTrue(call["stream"])
        self.assertEqual(call["metadata"], {"include_confidence": "true"})
        item = call["input"][0]
        self.assertEqual((item["type"], item["role"]), ("message", "user"))
        content = item["content"]
        self.assertEqual(content[0], {"type": "input_text", "text": "duck"})
        self.assertEqual(content[1]["type"], "input_image")
        self.assertTrue(content[1]["image_url"].startswith("data:image/jpeg;base64,"))

    def test_sam_records_mapped_to_detections(self):
        from backend.tests.helpers import (
            install_sam_parser_stub,
            sam_box,
            sam_event_stream,
            sam_mask,
            sam_result,
        )

        install_sam_parser_stub(self, [sam_result(
            [sam_box("0", (1, 2, 3, 4), 0.9), sam_mask("0"),
             sam_box("1", (5, 6, 7, 8), None)])])
        client = FakeSAMClient([sam_event_stream()])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=client):
            out = MetaSAMAdapter(model="test-sam", mock=False).analyze(
                b"fakejpeg", concepts=[("duck", "duck")])
        self.assertEqual(len(out["detections"]), 2)
        first, second = out["detections"]
        self.assertEqual(first["box"], [1, 2, 3, 4])
        self.assertEqual(first["confidence"], 0.9)
        self.assertEqual(first["mask_svg"], "<svg-stub>")
        self.assertEqual(second["confidence"], 0.5)  # absent c is not zero
        self.assertNotIn("mask_svg", second)

    def test_sam_incomplete_outcome_raises(self):
        from backend.tests.helpers import (
            install_sam_parser_stub,
            sam_event_stream,
            sam_result,
        )

        install_sam_parser_stub(
            self, [sam_result([], status="incomplete", reason="eof")])
        client = FakeSAMClient([sam_event_stream()])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=client):
            with self.assertRaises(MetaAPIError) as ctx:
                MetaSAMAdapter(model="m", mock=False).analyze(
                    b"fakejpeg", concepts=[("duck", "duck")])
        self.assertIn("incomplete", str(ctx.exception))

    # -- SAM 3.1 video tracking -----------------------------------------
    def test_sam_clip_request_uses_input_video(self):
        from backend.tests.helpers import (
            install_sam_parser_stub,
            sam_box,
            sam_event_stream,
            sam_result,
        )

        install_sam_parser_stub(self, [sam_result([sam_box(frame_index=0)])])
        client = FakeSAMClient([sam_event_stream()])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=client):
            out = MetaSAMAdapter(model="test-sam", mock=False).analyze_clip(
                b"fakeclip", concept="duck")
        self.assertFalse(out["mock"])
        self.assertEqual(out["media"], "video")
        call = client.responses.calls[0]
        self.assertEqual(call["model"], "test-sam")
        self.assertEqual(call["metadata"], {"include_confidence": "true"})
        content = call["input"][0]["content"]
        self.assertEqual(content[0], {"type": "input_text", "text": "duck"})
        self.assertEqual(content[1]["type"], "input_video")
        self.assertTrue(content[1]["video_url"].startswith("data:video/mp4;base64,"))

    def test_sam_clip_groups_tracks_by_object_id(self):
        from backend.tests.helpers import (
            install_sam_parser_stub,
            sam_box,
            sam_event_stream,
            sam_result,
        )

        records = [
            sam_box("0", (1, 2, 3, 4), 0.9, frame_index=0),
            sam_box("0", (2, 3, 4, 5), 0.8, frame_index=1),
            sam_box("0", (3, 4, 5, 6), 0.7, frame_index=2),
            sam_box("1", (9, 9, 9, 9), 0.6, frame_index=2),
        ]
        install_sam_parser_stub(self, [sam_result(records)])
        client = FakeSAMClient([sam_event_stream()])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=client):
            out = MetaSAMAdapter(model="m", mock=False).analyze_clip(b"fakeclip")
        self.assertEqual(out["track_count"], 2)
        self.assertEqual(out["frame_count"], 3)
        first = out["tracks"][0]  # most frames seen first
        self.assertEqual(first["object_id"], "0")
        self.assertEqual(first["frames_seen"], 3)
        self.assertEqual((first["first_frame"], first["last_frame"]), (0, 2))
        self.assertEqual(first["mean_confidence"], 0.8)
        self.assertEqual(out["detections"][0]["box"], [3, 4, 5, 6])  # last frame

    def test_sam_clip_incomplete_outcome_raises(self):
        from backend.tests.helpers import (
            install_sam_parser_stub,
            sam_event_stream,
            sam_result,
        )

        install_sam_parser_stub(
            self, [sam_result([], status="incomplete", reason="eof")])
        client = FakeSAMClient([sam_event_stream()])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=client):
            with self.assertRaises(MetaAPIError) as ctx:
                MetaSAMAdapter(model="m", mock=False).analyze_clip(b"fakeclip")
        self.assertIn("incomplete", str(ctx.exception))

    def test_sam_clip_mock_says_it_cannot_read_pixels(self):
        with mock.patch.dict(os.environ, {"MOCK_META": "true"}):
            out = MetaSAMAdapter(mock=True).analyze_clip(
                b"clip", context={"clip_source": "uploaded (override)"})
        self.assertTrue(out["mock"])
        self.assertEqual(out["media"], "video")
        self.assertEqual(out["tracks"], [])
        self.assertIn("cannot read pixels", out["notes"])

    def test_claude_complete_text_attaches_video(self):
        comp = FakeCompletions(["move the red wire"])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=SimpleNamespace(
                    chat=SimpleNamespace(completions=comp))):
            text = MetaClaudeAdapter(model="m", mock=False).complete_text(
                "system", "user prompt", video_b64="QUJDRA==", video_mime="video/mp4")
        self.assertEqual(text, "move the red wire")
        content = comp.calls[0]["messages"][-1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0], {"type": "text", "text": "user prompt"})
        self.assertEqual(content[1]["type"], "video_url")
        self.assertEqual(content[1]["video_url"]["url"],
                         "data:video/mp4;base64,QUJDRA==")

    # -- live guidance engine -------------------------------------------
    def test_live_guide_mock_turn_keeps_probe_authoritative(self):
        from backend.app.live import LiveGuide

        guide = LiveGuide(sam=MetaSAMAdapter(mock=True),
                          Muse=MetaClaudeAdapter(mock=True),
                          voice=MetaVoiceAdapter(mock=True))
        with mock.patch.dict(os.environ, {"MOCK_META": "true"}):
            # Pin the probe: the default reads ambient env, which the
            # .env-loaded suite flips to real mode at collection time.
            guide.probe = SerialProbe(mock=True)
            out = guide.guide(clip=b"clip", transcript="i moved the wire")
        self.assertTrue(out["mock"])
        self.assertEqual(out["transcript"], "i moved the wire")
        self.assertTrue(out["speak"])
        self.assertIn("i2c_devices", out["probe"])  # probe read, not invented
        self.assertEqual(out["sam"]["track_count"], 2)  # mock clip tracks

    def test_live_routes_registered(self):
        from backend.app.main import app

        paths = [r.path for r in app.routes]
        self.assertIn("/api/vision/track-clip", paths)
        self.assertIn("/api/live/session", paths)

    # -- orchestrator error propagation (no canned fallbacks) --------------
    def _mock_orc(self):
        import tempfile

        from backend.tests.helpers import make_camera, use_mock_env

        use_mock_env(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return AgentOrchestrator(
            graph=load_circuit_graph(), camera=make_camera(tmp.name),
            settler=MotionSettler(), probe=SerialProbe(mock=True),
            sam=MetaSAMAdapter(mock=True), Muse=MetaClaudeAdapter(mock=True),
            voice=MetaVoiceAdapter(mock=True),
            attempt_log_path=os.path.join(tmp.name, "a.jsonl"))

    def test_claude_outage_raises_and_stops_step(self):
        orc = self._mock_orc()
        orc.start_session(Mode.GUIDED_ASSEMBLY)
        for _ in range(3):
            orc.check_now(CheckNowRequest())

        def down(ctx):
            raise MetaAPIError("service down")

        orc.Muse.decide = down
        with self.assertRaises(MetaAPIError) as ctx:
            orc.check_now(CheckNowRequest(simulate_fault="SENSOR_DISCONNECTED"))
        self.assertIn("service down", str(ctx.exception))
        self.assertEqual(orc.session["step_index"], 3)  # step did not advance

    def test_sam_outage_raises_and_stops_step(self):
        orc = self._mock_orc()
        orc.start_session(Mode.GUIDED_ASSEMBLY)

        def down(frame, prompt="", context=None, frame_path=None):
            raise MetaAPIError("vision down")

        orc.sam.analyze = down
        with self.assertRaises(MetaAPIError) as ctx:
            orc.check_now(CheckNowRequest())
        self.assertIn("vision down", str(ctx.exception))

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

    # -- generative vision (no fixed part script) -------------------------
    def test_sam_default_is_single_catchall_prompt(self):
        from backend.tests.helpers import (
            install_sam_parser_stub,
            sam_box,
            sam_event_stream,
            sam_result,
        )

        install_sam_parser_stub(self, [sam_result([sam_box()])])
        client = FakeSAMClient([sam_event_stream()])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=client):
            out = MetaSAMAdapter(model="test-sam", mock=False).analyze(b"fakejpeg")
        self.assertEqual(len(client.responses.calls), 1)
        content = client.responses.calls[0]["input"][0]["content"]
        self.assertEqual(content[0],
                         {"type": "input_text",
                          "text": "electronic components on the bench"})
        self.assertEqual(out["detections"][0]["label"], "component")
        self.assertIsNone(out["wires_ok"])  # unknown, not a verdict
        self.assertIsNone(out["oled_lit"])
        self.assertNotIn("not measurable", out["notes"])

    def test_claude_decide_attaches_frame_image(self):
        comp = FakeCompletions([json.dumps(VALID_DECISION)])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=SimpleNamespace(
                    chat=SimpleNamespace(completions=comp))):
            MetaClaudeAdapter(model="m", mock=False).decide({
                "probe": ProbeReading(connected=True, i2c_devices=["0x76"]),
                "frame_jpeg_b64": "QUJDRA=="})
        content = comp.calls[0]["messages"][-1]["content"]
        self.assertIsInstance(content, list)
        self.assertIn("0x76", content[0]["text"])  # state still forwarded
        self.assertEqual(content[1]["type"], "image_url")
        self.assertIn("QUJDRA==", content[1]["image_url"]["url"])

    def test_describe_view_real_uses_claude_reading(self):
        orc = self._mock_orc()
        orc.start_session(Mode.GUIDED_ASSEMBLY)
        seen = {}
        orc.sam.analyze = lambda *a, **k: {
            "mock": False, "detections": [], "notes": "SAM 3.1: 0 regions",
            "wires_ok": None}

        def fake_complete(system, user, image_b64=None):
            seen["system"] = system
            seen["user"] = user
            seen["image"] = image_b64
            return "A blurry bench: something rectangular, cannot identify."

        orc.Muse.complete_text = fake_complete
        d = orc.describe_view()
        self.assertEqual(
            d["description"],
            "A blurry bench: something rectangular, cannot identify.")
        self.assertTrue(seen["image"])  # Muse looked at the frame
        self.assertIn("0 region", seen["user"])

    def test_describe_view_real_empty_reading_says_so(self):
        orc = self._mock_orc()
        orc.start_session(Mode.GUIDED_ASSEMBLY)
        orc.sam.analyze = lambda *a, **k: {"mock": False, "detections": []}
        orc.Muse.complete_text = lambda *a, **k: "   "
        d = orc.describe_view()
        self.assertIn("empty description", d["description"])

    def test_describe_view_real_error_propagates(self):
        orc = self._mock_orc()
        orc.start_session(Mode.GUIDED_ASSEMBLY)
        orc.sam.analyze = lambda *a, **k: {"mock": False, "detections": []}

        def down(*a, **k):
            raise MetaAPIError("vision down")

        orc.Muse.complete_text = down
        with self.assertRaises(MetaAPIError):
            orc.describe_view()


if __name__ == "__main__":
    unittest.main()
