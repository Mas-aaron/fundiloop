"""Hash-chained event log, passport lookup, and real-mode error behavior.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_session_log -v
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app.adapters import MetaAPIError
from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.adapters.sam_adapter import MetaSAMAdapter
from backend.app.adapters.voice_adapter import MetaVoiceAdapter
from backend.app.agent_orchestrator import AgentOrchestrator, verify_chain
from backend.app.camera_client import CameraClient, CameraError
from backend.app.change_detector import MotionSettler
from backend.app.circuit_graph import load_circuit_graph
from backend.app.models import CheckNowRequest, Mode
from backend.app.serial_probe import SerialProbe
from backend.tests.helpers import TEST_JPEG, make_camera, use_mock_env


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


class SessionLogTest(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.orc = make_orchestrator(
            os.path.join(self.tmp.name, "attempts.jsonl"), self.tmp.name)

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

    def test_claude_error_propagates_without_canned_instruction(self):
        self.orc.Muse = MetaClaudeAdapter(model="live", mock=False)
        self.orc.start_session(Mode.GUIDED_ASSEMBLY)
        with mock.patch.dict(os.environ, {"META_API_KEY": "k"}), mock.patch(
                "openai.OpenAI", side_effect=RuntimeError("api down")):
            with self.assertRaises(MetaAPIError) as ctx:
                self.orc.check_now(CheckNowRequest())
        self.assertIn("api down", str(ctx.exception))
        self.assertEqual(self.orc.session["step_index"], 0)  # nothing advanced

    def test_sam_error_propagates(self):
        from backend.tests.helpers import install_sam_parser_stub

        install_sam_parser_stub(self, [])
        self.orc.sam = MetaSAMAdapter(model="live", mock=False)
        self.orc.start_session(Mode.GUIDED_ASSEMBLY)
        with mock.patch.dict(os.environ, {"META_API_KEY": "k"}), mock.patch(
                "openai.OpenAI", side_effect=RuntimeError("vision down")):
            with self.assertRaises(MetaAPIError) as ctx:
                self.orc.check_now(CheckNowRequest())
        self.assertIn("vision down", str(ctx.exception))

    def test_camera_failure_stops_step_without_mock_frame(self):
        with mock.patch.dict(os.environ, {"CAMERA_STREAM_URL": ""}):
            self.orc.camera = CameraClient(
                capture_url="http://127.0.0.1:9/nope", timeout=0.5,
                debug_dir=self.tmp.name)
        self.orc.start_session(Mode.GUIDED_ASSEMBLY)
        with self.assertRaises(CameraError):
            self.orc.check_now(CheckNowRequest())
        self.assertEqual(self.orc.session["attempts"], [])  # no faked turn logged

    def test_phone_snap_override(self):
        cam = CameraClient(debug_dir=self.tmp.name)
        cam.set_override(TEST_JPEG)
        frame, source = cam.fetch_latest()
        self.assertEqual(frame, TEST_JPEG)
        self.assertIn("(override)", source)
        cam.clear_override()
        with mock.patch.dict(os.environ, {"CAMERA_CAPTURE_URL": "", "CAMERA_FRAME_URL": "",
                                          "CAMERA_STREAM_URL": ""}):
            cam2 = CameraClient(debug_dir=self.tmp.name)
            self.assertNotIn("(override)", cam2.describe_source())
            with self.assertRaises(CameraError):
                cam2.fetch_latest()

    def test_grab_live_requires_camera(self):
        with self.assertRaises(CameraError):
            CameraClient(capture_url="http://127.0.0.1:9/nope", timeout=0.5,
                         debug_dir=self.tmp.name).grab_live()
        with mock.patch.dict(os.environ, {"CAMERA_CAPTURE_URL": "", "CAMERA_FRAME_URL": "",
                                          "CAMERA_STREAM_URL": ""}):
            with self.assertRaises(CameraError):
                CameraClient(debug_dir=self.tmp.name).grab_live()

    def test_mock_vision_refuses_snapped_photo(self):
        self.orc.camera.set_override(TEST_JPEG)
        self.orc.camera.describe_source = lambda: "snap (override)"
        self.orc.start_session(Mode.GUIDED_ASSEMBLY)
        v = self.orc.check_now(CheckNowRequest())
        self.assertEqual(v["sam"]["detections"], [])
        self.assertIn("cannot read pixels", v["sam"]["notes"])

    def test_describe_view_mock(self):
        self.orc.start_session(Mode.GUIDED_ASSEMBLY)
        d = self.orc.describe_view()
        self.assertIn("ESP32 DevKit", d["description"])
        self.assertIn("Simulated view", d["description"])
        self.assertTrue(d["sam"]["mock"])

    def test_speech_leads_with_live_measurement(self):
        self.orc.start_session(Mode.GUIDED_ASSEMBLY)
        v = self.orc.check_now(CheckNowRequest())
        self.assertIn("PING OK", v["speak"])
        for _ in range(2):
            v = self.orc.check_now(CheckNowRequest())
        v = self.orc.check_now(CheckNowRequest(simulate_fault="SENSOR_DISCONNECTED"))
        self.assertIn("MISSING", v["speak"])
        self.assertIn(v["verification"]["measured"], v["speak"])

    def test_diagnostic_speech_names_suspect(self):
        self.orc.start_session(Mode.DIAGNOSTIC)
        v = self.orc.check_now(CheckNowRequest(simulate_fault="SDA_SCL_SWAPPED"))
        self.assertIn("SDA_SCL_SWAPPED", v["speak"])
        self.assertIn(v["verification"]["measured"], v["speak"])

    def test_describe_view_refuses_snapped_photo(self):
        self.orc.camera.set_override(TEST_JPEG)
        self.orc.camera.describe_source = lambda: "snap (override)"
        self.orc.start_session(Mode.GUIDED_ASSEMBLY)
        d = self.orc.describe_view()
        self.assertIn("cannot read pixels", d["description"])

    def test_routes_registered(self):
        from backend.app.main import app

        paths = {(r.path, tuple(sorted(getattr(r, "methods", None) or [])))
                 for r in app.routes}
        self.assertIn(("/api/camera/snap", ("POST",)), paths)
        self.assertIn(("/api/camera/snap", ("DELETE",)), paths)
        self.assertIn(("/api/config/camera", ("POST",)), paths)
        self.assertIn(("/api/vision/describe", ("POST",)), paths)
        self.assertIn(("/api/frames/{name}", ("GET",)), paths)
        self.assertNotIn(("/api/config/demo", ("POST",)), paths)

    def test_camera_reads_new_env_vars(self):
        env = {"CAMERA_STREAM_URL": "http://cam.local:81/stream",
               "CAMERA_CAPTURE_URL": "http://cam.local/capture"}
        with mock.patch.dict(os.environ, env, clear=False):
            cam = CameraClient(debug_dir=self.tmp.name)
            self.assertEqual(cam.stream_url, env["CAMERA_STREAM_URL"])
            self.assertEqual(cam.capture_url, env["CAMERA_CAPTURE_URL"])
        with mock.patch.dict(os.environ, {"CAMERA_STREAM_URL": "", "CAMERA_CAPTURE_URL": "",
                                          "CAMERA_FRAME_URL": "http://legacy.local/capture"}):
            self.assertEqual(CameraClient(debug_dir=self.tmp.name).capture_url,
                             "http://legacy.local/capture")


class SavedFrameEndpointTest(unittest.TestCase):
    """GET /api/frames/{name}: serve the analyzed still, nothing else."""

    def setUp(self):
        use_mock_env(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from backend.app import main as app_main

        self.app_main = app_main
        with open(os.path.join(self.tmp.name, "frame_test.jpg"), "wb") as fh:
            fh.write(TEST_JPEG)
        patcher = mock.patch.object(app_main.orchestrator.camera, "debug_dir",
                                    Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_serves_saved_jpeg(self):
        resp = self.app_main.api_saved_frame("frame_test.jpg")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.media_type, "image/jpeg")
        self.assertTrue(str(resp.path).endswith("frame_test.jpg"))

    def test_missing_frame_is_404(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_saved_frame("nope.jpg")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_traversal_and_suffix_rejected(self):
        from fastapi import HTTPException

        for bad in ("../app.py", "..", "frame.png", "notes.txt", ".env"):
            with self.assertRaises(HTTPException, msg=bad):
                self.app_main.api_saved_frame(bad)


if __name__ == "__main__":
    unittest.main()
