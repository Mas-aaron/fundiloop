"""Turn-latency optimizations: overlapped sense, timings, progress, reuse.

Covers the check-now fast path without network or keys:
- SAM vision + serial probe + board glance overlap instead of serializing
- every turn view carries per-stage timings
- check_now() accepts a progress callback (drives the streaming endpoint)
- Meta HTTP clients are reused across turns (no TLS handshake per call)
- branch failures still surface as real errors, never faked turns
- POST /api/session/check-now/stream emits ack -> stages -> done/error
- preview wait fails fast when the camera is down with no frame ever

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_latency -v
"""
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.app.adapters import MetaAPIError
from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.adapters.sam_adapter import MetaSAMAdapter
from backend.app.adapters.voice_adapter import MetaVoiceAdapter
from backend.app.agent_orchestrator import AgentOrchestrator
from backend.app.camera_client import CameraClient, CameraError
from backend.app.change_detector import MotionSettler
from backend.app.circuit_graph import load_circuit_graph
from backend.app.models import CheckNowRequest, Mode
from backend.app.serial_probe import SerialProbe
from backend.tests.helpers import (
    FakeSAMClient,
    install_sam_parser_stub,
    make_camera,
    sam_box,
    sam_event_stream,
    sam_result,
    use_mock_env,
)
from backend.tests.test_real_adapters import FakeCompletions

VALID_DECISION = {
    "mode": "GUIDED_ASSEMBLY", "state": "INSTRUCTING", "fault_hypothesis": None,
    "confidence": 0.9, "evidence": ["ok"], "next_instruction": "wire it",
    "speak_to_tech": "wire it now", "verification_test": "PING",
}


def make_orchestrator(tmpdir, log_name="attempts.jsonl"):
    return AgentOrchestrator(
        graph=load_circuit_graph(),
        camera=make_camera(tmpdir),
        settler=MotionSettler(settle_seconds=0.0),
        probe=SerialProbe(mock=True),
        sam=MetaSAMAdapter(mock=True),
        Muse=MetaClaudeAdapter(mock=True),
        voice=MetaVoiceAdapter(mock=True),
        attempt_log_path=os.path.join(tmpdir, log_name),
    )


class SenseOverlapTest(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orc = make_orchestrator(self.tmp.name)
        self.orc.start_session(Mode.GUIDED_ASSEMBLY)

    def test_sam_and_probe_overlap(self):
        events = []
        lock = threading.Lock()

        def rec(name):
            with lock:
                events.append(name)

        real_analyze, real_read = self.orc.sam.analyze, self.orc.probe.read_all

        def slow_analyze(*a, **k):
            rec("sam_enter")
            try:
                time.sleep(0.6)
                return real_analyze(*a, **k)
            finally:
                rec("sam_exit")

        def slow_read(*a, **k):
            rec("probe_enter")
            try:
                time.sleep(0.6)
                return real_read(*a, **k)
            finally:
                rec("probe_exit")

        with mock.patch.object(self.orc.sam, "analyze", slow_analyze), mock.patch.object(
            self.orc.probe, "read_all", slow_read
        ):
            t0 = time.monotonic()
            v = self.orc.check_now(CheckNowRequest())
            wall = time.monotonic() - t0
        self.assertTrue(v["verification"]["passed"])
        # Deterministic overlap proof: each branch entered before the other left.
        self.assertLess(events.index("probe_enter"), events.index("sam_exit"))
        self.assertLess(events.index("sam_enter"), events.index("probe_exit"))
        # Sequential would take >= 1.2s (two 0.6s sleeps); overlapped ~0.6s.
        self.assertLess(wall, 1.1, events)

    def test_timings_present_in_turn_view(self):
        v = self.orc.check_now(CheckNowRequest())
        timings = v.get("timings")
        self.assertIsInstance(timings, dict)
        for key in ("total_ms", "sense_ms", "sam_ms", "probe_ms", "reason_ms"):
            self.assertIn(key, timings, timings)
            self.assertGreaterEqual(timings[key], 0.0)

    def test_progress_stages_ordered(self):
        stages = []
        v = self.orc.check_now(
            CheckNowRequest(), progress=lambda s, d: stages.append(s))
        self.assertTrue(v["verification"]["passed"])
        self.assertEqual(stages, ["sense", "reason", "verify", "done"])

    def test_sam_branch_error_still_raises(self):
        def boom(*a, **k):
            raise MetaAPIError("vision down")

        with mock.patch.object(self.orc.sam, "analyze", boom):
            with self.assertRaises(MetaAPIError) as ctx:
                self.orc.check_now(CheckNowRequest())
        self.assertIn("vision down", str(ctx.exception))
        self.assertEqual(self.orc.session["attempts"], [])

    def test_camera_error_with_concurrent_probe(self):
        def boom(*a, **k):
            raise CameraError("no preview frame available (camera unreachable?)")

        with mock.patch.object(self.orc.camera, "get_latest_preview_frame", boom):
            with self.assertRaises(CameraError):
                self.orc.check_now(CheckNowRequest())
        self.assertEqual(self.orc.session["attempts"], [])


class ClientReuseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "MODEL_CALLS_LOG": os.path.join(self.tmp.name, "model_calls.jsonl"),
            "META_API_KEY": "test",
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_claude_client_reused_across_decides(self):
        from backend.app.models import ProbeReading

        made = []

        def maker(**kwargs):
            made.append(kwargs)
            comp = FakeCompletions([json.dumps(VALID_DECISION)] * 2)
            return SimpleNamespace(chat=SimpleNamespace(completions=comp))

        adapter = MetaClaudeAdapter(model="m", mock=False)
        with mock.patch("openai.OpenAI", side_effect=maker):
            adapter.decide({"probe": ProbeReading(connected=True)})
            adapter.decide({"probe": ProbeReading(connected=True)})
        self.assertEqual(len(made), 1)

    def test_sam_client_reused_across_analyzes(self):
        install_sam_parser_stub(
            self, [sam_result([sam_box()]), sam_result([sam_box()])])
        made = []

        def maker(**kwargs):
            made.append(kwargs)
            return FakeSAMClient([sam_event_stream(), sam_event_stream()])

        adapter = MetaSAMAdapter(model="m", mock=False)
        with mock.patch("openai.OpenAI", side_effect=maker):
            adapter.analyze(b"fakejpeg", concepts=[("duck", "duck")])
            adapter.analyze(b"fakejpeg", concepts=[("duck", "duck")])
        self.assertEqual(len(made), 1)


class StreamEndpointTest(unittest.TestCase):
    @staticmethod
    def _frames(chunks):
        body = "".join(
            c.decode("utf-8") if isinstance(c, bytes) else c for c in chunks
        )
        out = []
        for frame in body.split("\n\n"):
            name, data = None, ""
            for line in frame.splitlines():
                if line.startswith("event:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data = line.split(":", 1)[1].strip()
            if name:
                out.append((name, json.loads(data) if data else {}))
        return out

    def test_ack_then_stages_then_done(self):
        import backend.app.main as main_mod

        class FakeOrc:
            def check_now(self, req, progress=None):
                for stage in ("sense", "reason", "verify"):
                    if progress:
                        progress(stage, {"ok": True})
                return {"speak": "hi", "state": "INSTRUCTING"}

        with mock.patch.object(main_mod, "orchestrator", FakeOrc()):
            frames = self._frames(
                main_mod.iter_check_now_events(CheckNowRequest()))
            resp = main_mod.api_check_now_stream(CheckNowRequest())
        names = [n for n, _ in frames]
        self.assertEqual(names, ["ack", "sense", "reason", "verify", "done"])
        self.assertEqual(frames[-1][1]["speak"], "hi")
        self.assertEqual(resp.media_type, "text/event-stream")

    def test_single_done_when_orchestrator_emits_done_stage(self):
        """The real check_now ends with progress("done", timings): the
        stream must forward exactly one done, carrying the full view."""
        import backend.app.main as main_mod

        class FakeOrc:
            def check_now(self, req, progress=None):
                for stage in ("sense", "reason", "verify", "done"):
                    if progress:
                        progress(stage, {"timings": {"total_ms": 1.0}})
                return {"speak": "hi", "state": "INSTRUCTING",
                        "instruction": "do the thing"}

        with mock.patch.object(main_mod, "orchestrator", FakeOrc()):
            frames = self._frames(
                main_mod.iter_check_now_events(CheckNowRequest()))
        names = [n for n, _ in frames]
        self.assertEqual(names, ["ack", "sense", "reason", "verify", "done"])
        self.assertEqual(frames[-1][1]["instruction"], "do the thing")

    def test_branch_error_streams_error_event(self):
        import backend.app.main as main_mod

        class FakeOrc:
            def check_now(self, req, progress=None):
                if progress:
                    progress("sense", {})
                raise MetaAPIError("service down")

        with mock.patch.object(main_mod, "orchestrator", FakeOrc()):
            frames = self._frames(
                main_mod.iter_check_now_events(CheckNowRequest()))
        names = [n for n, _ in frames]
        self.assertEqual(names[0], "ack")
        self.assertEqual(names[-1], "error")
        self.assertIn("service down", frames[-1][1]["detail"])


class PreviewFailFastTest(unittest.TestCase):
    def test_down_camera_fails_fast(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cam = CameraClient(
            capture_url="http://127.0.0.1:9/nope", timeout=0.5,
            debug_dir=tmp.name)
        t0 = time.monotonic()
        with self.assertRaises(CameraError):
            cam.get_latest_preview_frame()
        self.assertLess(time.monotonic() - t0, 6.0)


if __name__ == "__main__":
    unittest.main()
