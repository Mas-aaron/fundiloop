"""Real-mode contract: mocks raise when MOCK_META=false, camera validates
and saves real JPEGs, model calls are logged, /api/status exposes proof.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_real_mode -v
"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.app.adapters import MetaAPIError
from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.adapters.sam_adapter import MetaSAMAdapter
from backend.app.adapters.voice_adapter import MetaVoiceAdapter
from backend.app.camera_client import CameraClient, CameraError
from backend.app.serial_probe import SerialProbe
from backend.tests.helpers import TEST_JPEG


def real_env(tc, tmp):
    """MOCK_META=false with MODEL_CALLS_LOG redirected to tmp."""
    patcher = mock.patch.dict(os.environ, {
        "MOCK_META": "false",
        "META_API_KEY": "test-key",
        "MODEL_CALLS_LOG": os.path.join(tmp, "model_calls.jsonl"),
        "CAMERA_STREAM_URL": "",
        "CAMERA_CAPTURE_URL": "",
        "CAMERA_FRAME_URL": "",
    })
    patcher.start()
    tc.addCleanup(patcher.stop)


class FakeRaw:
    status_code = 200
    headers = {"x-request-id": "req-real-mode-1"}

    def __init__(self, content):
        self._content = content

    def parse(self):
        return SimpleNamespace(
            id="chatcmpl-test",
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))])


class FakeCompletions:
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


VALID_DECISION = {
    "mode": "GUIDED_ASSEMBLY", "state": "INSTRUCTING", "fault_hypothesis": None,
    "confidence": 0.9, "evidence": ["ok"], "next_instruction": "wire it",
    "speak_to_tech": "wire it now", "verification_test": "PING",
}


def expect_box(shipped: list) -> list:
    """Shipped-space box mapped to source pixels, like the adapter does.

    The adapter contract is source-pixel boxes: with OpenCV installed
    (a declared dependency) prepare_frame resizes to 448px and boxes
    map back; without it the rescale is the identity. Expectations
    must follow the same mapping or they only hold on one path.
    """
    from backend.app.frames import prepare_frame, scale_box

    prep = prepare_frame(TEST_JPEG)
    if not prep.resized:
        return list(shipped)
    return scale_box(list(shipped), prep.dst_w, prep.dst_h,
                     prep.src_w, prep.src_h)


def fake_urlopen_factory(body: bytes):
    class FakeResp:
        headers = {"Content-Type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return body

    def fake_urlopen(url, timeout=None):
        return FakeResp()

    return fake_urlopen


class RealModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    # -- mocks raise when MOCK_META=false --------------------------------
    def test_mock_adapters_raise_in_real_mode(self):
        real_env(self, self.tmp.name)
        with self.assertRaises(MetaAPIError):
            MetaSAMAdapter(mock=True).analyze(b"whatever")
        with self.assertRaises(MetaAPIError):
            MetaClaudeAdapter(mock=True).decide({})
        with self.assertRaises(MetaAPIError):
            MetaVoiceAdapter(mock=True).transcribe(b"whatever")
        with self.assertRaises(RuntimeError):
            SerialProbe(mock=True).send("PING")

    def test_mock_meta_controlled_only_by_env(self):
        with mock.patch.dict(os.environ, {"MOCK_META": "false", "META_API_KEY": ""}):
            self.assertFalse(MetaSAMAdapter().mock)
            self.assertFalse(MetaClaudeAdapter().mock)
        with mock.patch.dict(os.environ, {"MOCK_META": "true", "META_API_KEY": "k"}):
            self.assertTrue(MetaSAMAdapter().mock)
            self.assertTrue(MetaClaudeAdapter().mock)

    # -- camera: validate + save, never substitute -------------------------
    def test_capture_saves_validated_jpeg_with_session_id(self):
        real_env(self, self.tmp.name)
        cam = CameraClient(capture_url="http://cam.local/capture",
                           debug_dir=os.path.join(self.tmp.name, "debug"))
        with mock.patch("urllib.request.urlopen", fake_urlopen_factory(TEST_JPEG)):
            frame, saved = cam.capture_analysis_frame("sess9")
        self.assertEqual(frame, TEST_JPEG)
        self.assertTrue(str(saved).endswith(".jpg"))
        self.assertIn("sess9", str(saved))
        self.assertEqual(saved.read_bytes(), TEST_JPEG)

    def test_capture_rejects_non_jpeg_without_fallback(self):
        real_env(self, self.tmp.name)
        cam = CameraClient(capture_url="http://cam.local/capture",
                           debug_dir=os.path.join(self.tmp.name, "debug"))
        with mock.patch("urllib.request.urlopen", fake_urlopen_factory(b"not an image" * 20)):
            with self.assertRaises(CameraError):
                cam.capture_analysis_frame("sess9")

    def test_capture_failure_raises_without_fallback(self):
        real_env(self, self.tmp.name)
        cam = CameraClient(capture_url="http://127.0.0.1:9/nope", timeout=0.5,
                           debug_dir=self.tmp.name)
        with self.assertRaises(CameraError):
            cam.capture_analysis_frame("sess9")

    def test_camera_status_reports_truthfully(self):
        real_env(self, self.tmp.name)
        offline = CameraClient(debug_dir=self.tmp.name).camera_status()
        self.assertFalse(offline["camera_online"])
        self.assertTrue(offline["camera_error"])
        cam = CameraClient(debug_dir=self.tmp.name)
        cam.set_override(TEST_JPEG)
        online = cam.camera_status()
        self.assertTrue(online["camera_online"])
        self.assertEqual(online["camera_last_frame_size_bytes"], len(TEST_JPEG))

    # -- model calls are logged with the frame used ------------------------
    def test_sam_concepts_each_logged_with_frame_file(self):
        from backend.tests.helpers import (
            FakeSAMClient,
            install_sam_parser_stub,
            sam_box,
            sam_event_stream,
            sam_mask,
            sam_result,
        )

        real_env(self, self.tmp.name)
        install_sam_parser_stub(self, [
            sam_result([sam_box("0", (1, 2, 3, 4), 0.9), sam_mask("0")]),
            sam_result([sam_box("0", (5, 6, 7, 8), 0.8),
                        sam_box("1", (9, 9, 9, 9), 0.7)]),
        ])
        client = FakeSAMClient([sam_event_stream(response_id="resp-a"),
                                sam_event_stream(response_id="resp-b")])
        with mock.patch("openai.OpenAI", return_value=client):
            out = MetaSAMAdapter(model="sam-3.1", mock=False).analyze(
                TEST_JPEG,
                concepts=[("ESP32 board", "ESP32 DevKit"),
                          ("jumper wire", "jumper wire")])
        self.assertFalse(out["mock"])
        self.assertEqual(out["http_status"], 200)
        by_label = {}
        for det in out["detections"]:
            by_label.setdefault(det["label"], []).append(det)
        self.assertEqual(by_label["ESP32 DevKit"][0]["box"],
                         expect_box([1, 2, 3, 4]))
        self.assertEqual(by_label["ESP32 DevKit"][0]["mask_svg"], "<svg-stub>")
        self.assertEqual(out["wire_count"], 2)
        log = os.path.join(self.tmp.name, "model_calls.jsonl")
        with open(log, encoding="utf-8") as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(entries), 2)  # one entry per concept call
        for entry in entries:
            self.assertEqual(entry["model"], "sam-3.1")
            self.assertEqual(entry["status"], 200)
            self.assertTrue(entry["ok"])
            self.assertIn("latency_ms", entry)
        self.assertEqual({e["request_id"] for e in entries}, {"resp-a", "resp-b"})

    def test_sam_transport_error_logged_and_surfaced(self):
        from backend.tests.helpers import FakeSAMClient, install_sam_parser_stub

        real_env(self, self.tmp.name)
        install_sam_parser_stub(self, [])
        client = FakeSAMClient([RuntimeError("boom")])
        with mock.patch("openai.OpenAI", return_value=client):
            with self.assertRaises(MetaAPIError) as ctx:
                MetaSAMAdapter(model="sam-3.1", mock=False).analyze(
                    TEST_JPEG, concepts=[("duck", "duck")])
        self.assertIn("boom", str(ctx.exception))
        log = os.path.join(self.tmp.name, "model_calls.jsonl")
        with open(log, encoding="utf-8") as fh:
            entry = json.loads(fh.read().strip().splitlines()[-1])
        self.assertFalse(entry["ok"])
        self.assertIn("boom", entry["error"])

    def test_sam_missing_parser_raises_with_install_hint(self):
        import sys

        real_env(self, self.tmp.name)
        with mock.patch.dict(sys.modules, {"meta_sam_parser": None}):
            with self.assertRaises(MetaAPIError) as ctx:
                MetaSAMAdapter(model="sam-3.1", mock=False).analyze(
                    TEST_JPEG, concepts=[("duck", "duck")])
        self.assertIn("meta-sam-parser", str(ctx.exception))

    def test_sam_deltas_concatenated_before_push(self):
        from backend.tests.helpers import (
            FakeSAMClient,
            install_sam_parser_stub,
            sam_box,
            sam_result,
        )

        real_env(self, self.tmp.name)
        _, pushed = install_sam_parser_stub(
            self, [sam_result([sam_box()]), sam_result([sam_box()])])
        split_deltas = [
            {"type": "response.output_text.delta", "output_index": 0,
             "content_index": 0, "delta": "grammar+"},
            {"type": "response.output_text.delta", "output_index": 0,
             "content_index": 0, "delta": "more"},
            {"type": "response.content_part.done", "output_index": 0,
             "content_index": 0,
             "part": {"type": "output_text", "text": "grammar+more"}},
        ]
        part_only = [
            {"type": "response.content_part.done", "output_index": 0,
             "content_index": 0,
             "part": {"type": "output_text", "text": "only-part"}},
        ]
        client = FakeSAMClient([split_deltas, part_only])
        with mock.patch("openai.OpenAI", return_value=client):
            MetaSAMAdapter(model="sam-3.1", mock=False).analyze(
                TEST_JPEG, concepts=[("a", "a"), ("b", "b")])
        # Deltas win when present; the part text is the fallback.
        self.assertEqual(pushed, ["grammar+more", "only-part"])

    def test_sam_real_parser_end_to_end(self):
        import importlib.util

        if importlib.util.find_spec("meta_sam_parser") is None:
            self.skipTest("meta-sam-parser not installed")
        from backend.tests.helpers import FakeSAMClient

        real_env(self, self.tmp.name)
        mask = ("<|mask;x=0;y=0;data=27,60,~!!!!M!0c[0o91w?q1!pIH4pPRVp2B3"
                "'7`e.ioeAf6-k/#Xd8%dX9x(|>")
        grammar = ("<0f>0<|box;x1=211;y1=228;x2=270;y2=254;w=320;h=334;c=0.87|>"
                   + mask + "\n")
        events = [
            {"type": "response.created", "response": {"id": "resp-real"}},
            {"type": "response.output_text.delta", "output_index": 0,
             "content_index": 0, "delta": grammar[:60]},
            {"type": "response.output_text.delta", "output_index": 0,
             "content_index": 0, "delta": grammar[60:]},
            {"type": "response.completed", "response": {"id": "resp-real"}},
        ]
        client = FakeSAMClient([events])
        with mock.patch("openai.OpenAI", return_value=client):
            out = MetaSAMAdapter(model="sam-3.1", mock=False).analyze(
                TEST_JPEG, concepts=[("duck", "duck")])
        self.assertEqual(len(out["detections"]), 1)
        det = out["detections"][0]
        self.assertEqual(det["label"], "duck")
        self.assertEqual(det["box"],
                         expect_box([211, 228, 271, 255]))  # source pixels
        self.assertAlmostEqual(det["confidence"], 0.87)
        self.assertTrue(det["mask_svg"].startswith("M"))
        self.assertEqual(out["request_id"], "resp-real")

    def test_claude_call_logged_with_frame_file(self):
        real_env(self, self.tmp.name)
        from backend.app.models import Mode, ProbeReading

        comp = FakeCompletions([json.dumps(VALID_DECISION)])
        with mock.patch("openai.OpenAI", return_value=SimpleNamespace(
                chat=SimpleNamespace(completions=comp))):
            d = MetaClaudeAdapter(model="muse-spark-1.3", mock=False).decide({
                "mode": Mode.GUIDED_ASSEMBLY,
                "probe": ProbeReading(connected=True, i2c_devices=["0x3C"]),
                "frame_file": "debug/frame1.jpg"})
        self.assertEqual(d.verification_test, "PING")
        log = os.path.join(self.tmp.name, "model_calls.jsonl")
        with open(log, encoding="utf-8") as fh:
            entry = json.loads(fh.read().strip().splitlines()[-1])
        self.assertEqual(entry["model"], "muse-spark-1.3")
        self.assertEqual(entry["frame_file"], "debug/frame1.jpg")
        self.assertTrue(entry["ok"])

    # -- /api/status proof fields -------------------------------------------
    def test_status_fields_and_no_key_leak(self):
        real_env(self, self.tmp.name)
        from backend.app.main import api_status

        view = api_status()
        cfg = view["config"]
        for key in ("mock_meta", "camera_online", "camera_last_frame_age_ms",
                    "camera_last_frame_size_bytes", "sam_mode", "muse_mode",
                    "meta_api_configured"):
            self.assertIn(key, cfg)
        self.assertFalse(cfg["mock_meta"])
        self.assertTrue(cfg["meta_api_configured"])
        self.assertNotIn("test-key", json.dumps(view))


if __name__ == "__main__":
    unittest.main()
