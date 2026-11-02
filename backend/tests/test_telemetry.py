"""Live serial telemetry: stream device lines, diagnose stalls.

The monitor takes the serial port over exclusively (probes hold it
persistently once adopted, and reopen lazily afterwards). The stream
yields raw device lines; anomaly detection runs in the browser;
diagnosis names a suspect wire from the netlist, or honestly none.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_telemetry -v
"""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.app.telemetry import (
    PROOF_LINES,
    TelemetryError,
    diagnose_telemetry,
    iter_telemetry_events,
    iter_telemetry_lines,
    telemetry_proof,
)
from backend.tests.test_fundi import StubBrain


class FakeSerial:
    def __init__(self, script):
        self.script = list(script)
        self.closed = False

    def readline(self):
        if not self.script:
            return b""
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class ProofRuleTest(unittest.TestCase):
    """DONE admits only a trailing healthy streak: measurements and
    heartbeats count, boot chatter holds, faults reset."""

    def test_five_measurements_verify(self):
        lines = ["Distance: %dcm" % n for n in (45, 44, 43, 42, 41)]
        proof = telemetry_proof(lines)
        self.assertTrue(proof["verified"])
        self.assertEqual(proof["healthy"], 5)
        self.assertEqual(proof["faults"], 0)

    def test_short_streak_does_not_verify(self):
        proof = telemetry_proof(["Distance: 45cm"] * 4)
        self.assertFalse(proof["verified"])
        self.assertEqual(proof["healthy"], 4)

    def test_fault_resets_the_streak(self):
        lines = (["Distance: 45cm"] * 4 + ["Error: Timeout"]
                 + ["Distance: 45cm"] * 5)
        proof = telemetry_proof(lines)
        self.assertTrue(proof["verified"])
        self.assertEqual(proof["healthy"], 5)
        self.assertEqual(proof["faults"], 1)

    def test_trailing_fault_vetoes(self):
        lines = ["Distance: 45cm"] * 6 + ["ECHO timeout"]
        proof = telemetry_proof(lines)
        self.assertFalse(proof["verified"])
        self.assertEqual(proof["healthy"], 0)

    def test_heartbeat_counts_boot_chatter_holds(self):
        lines = ["rst:0x1 (POWERON)", "STATUS ok", "ok", "System alive",
                 "boot:0x13", "HB 1", "Distance: 20cm"]
        proof = telemetry_proof(lines)
        self.assertTrue(proof["verified"])
        self.assertEqual(proof["healthy"], 5)

    def test_bare_chatter_never_counts(self):
        proof = telemetry_proof(["hello world"] * 9)
        self.assertFalse(proof["verified"])
        self.assertEqual(proof["healthy"], 0)

    def test_negative_reading_vetoes(self):
        lines = ["Distance: 45cm"] * 4 + ["Distance: -1"]
        proof = telemetry_proof(lines)
        self.assertFalse(proof["verified"])

    def test_empty_proves_nothing(self):
        proof = telemetry_proof([])
        self.assertFalse(proof["verified"])

    def test_proof_line_count_is_five(self):
        self.assertEqual(PROOF_LINES, 5)


class TelemetryVerifyTest(unittest.TestCase):
    """The verified endpoint marks DONE on proof, holds without it."""

    def test_proof_marks_done(self):
        import tempfile

        from backend.tests.helpers import use_mock_env
        from backend.tests.test_latency import make_orchestrator

        use_mock_env(self)
        orc = make_orchestrator(tempfile.mkdtemp())
        view = orc.start_learning_session(
            "proj",
            [{"n": 1, "instruction": "Wire it.", "verify": "CHECK_GPIO",
              "expect": {}, "depends_on_parts": []}],
            project_title="Blink", board="esp32_devkit")
        sid = view["id"]
        orc.answer_rollcall(sid, "ready")
        lines = ["Distance: %dcm" % n for n in (45, 44, 43, 42, 41)]
        done = orc.telemetry_verify(sid, lines)
        self.assertEqual(done["state"], "DONE")
        self.assertIn("It works!", done["speak"])
        self.assertTrue(done["code_ready"])
        self.assertEqual(orc.sessions[sid]["verified"]["steps"][-1]["test"],
                         "TELEMETRY")

    def test_weak_evidence_holds(self):
        import tempfile

        from backend.tests.helpers import use_mock_env
        from backend.tests.test_latency import make_orchestrator

        use_mock_env(self)
        orc = make_orchestrator(tempfile.mkdtemp())
        view = orc.start_learning_session(
            "proj",
            [{"n": 1, "instruction": "Wire it.", "verify": "CHECK_GPIO",
              "expect": {}, "depends_on_parts": []}],
            project_title="Blink", board="esp32_devkit")
        sid = view["id"]
        out = orc.telemetry_verify(sid, ["hello", "world"])
        self.assertFalse(out["verified"])
        self.assertFalse(orc.sessions[sid]["completed_at"])
        self.assertFalse(orc.sessions[sid]["code_ready"])

    def test_unknown_session_404s(self):
        import tempfile

        from backend.tests.helpers import use_mock_env
        from backend.tests.test_latency import make_orchestrator

        use_mock_env(self)
        orc = make_orchestrator(tempfile.mkdtemp())
        with self.assertRaises(KeyError):
            orc.telemetry_verify("nope", ["Distance: 1cm"] * 5)


class IterLinesTest(unittest.TestCase):
    def test_streams_decoded_lines(self):
        fake = FakeSerial([b"Distance: 45cm\r\n", b"\r\n",
                           b"Distance: 44cm\r\n"])
        out = list(iter_telemetry_lines("COM3", 115200,
                                        opener=lambda p, b: fake,
                                        max_lines=2))
        self.assertEqual(out, ["Distance: 45cm", "Distance: 44cm"])
        self.assertTrue(fake.closed)

    def test_open_failure_raises_telemetry_error(self):
        def boom(port, baud):
            raise OSError("busy")

        with self.assertRaises(TelemetryError) as ctx:
            list(iter_telemetry_lines("COM3", 115200, opener=boom))
        self.assertIn("COM3", str(ctx.exception))

    def test_read_failure_ends_stream(self):
        fake = FakeSerial([b"ok\r\n", OSError("unplugged")])
        it = iter_telemetry_lines("COM3", 115200,
                                  opener=lambda p, b: fake)
        self.assertEqual(next(it), "ok")
        with self.assertRaises(TelemetryError):
            list(it)
        self.assertTrue(fake.closed)

    def test_blank_lines_skipped_timeouts_ignored(self):
        fake = FakeSerial([b"", b"\n", b"hi\r\n"])
        out = list(iter_telemetry_lines("COM3", 115200,
                                        opener=lambda p, b: fake,
                                        max_lines=1))
        self.assertEqual(out, ["hi"])

    def test_events_frame_ready_lines_error(self):
        fake = FakeSerial([b"Distance: 45cm\r\n", OSError("unplugged")])
        out = list(iter_telemetry_events(
            "COM3", 115200, opener=lambda p, b: fake))
        self.assertEqual(out[0], ("ready", {"port": "COM3", "baud": 115200}))
        self.assertEqual(out[1], ("line", {"line": "Distance: 45cm"}))
        self.assertEqual(out[2][0], "error")
        self.assertIn("unplugged", out[2][1]["detail"])


class DiagnoseTest(unittest.TestCase):
    NET = {"wires": [
        {"part": "hc_sr04", "part_pin": "TRIG", "board_pin": "GPIO25",
         "signal": "TRIG"},
        {"part": "hc_sr04", "part_pin": "ECHO", "board_pin": "GPIO27",
         "signal": "ECHO"},
    ]}

    def test_valid_suspect_kept(self):
        brain = StubBrain([json.dumps({
            "message": "ECHO looks loose.",
            "suspect": {"part": "hc_sr04", "part_pin": "ECHO"}})])
        out = diagnose_telemetry(["Distance: nan"], self.NET, "Weather",
                                 brain)
        self.assertEqual(out["suspect"]["board_pin"], "GPIO27")
        self.assertIn("ECHO", out["message"])
        self.assertEqual(len(brain.calls), 1)

    def test_unknown_suspect_cleared(self):
        brain = StubBrain([json.dumps({
            "message": "Maybe the flux capacitor.",
            "suspect": {"part": "flux", "part_pin": "X"}})])
        out = diagnose_telemetry(["nan"], self.NET, "W", brain)
        self.assertIsNone(out["suspect"])
        self.assertTrue(out["message"])

    def test_bad_reply_retries_then_falls_back(self):
        brain = StubBrain(["not json", "still not json"])
        out = diagnose_telemetry(["nan"], self.NET, "W", brain)
        self.assertIsNone(out["suspect"])
        self.assertIn("wire", out["message"].lower())
        self.assertEqual(len(brain.calls), 2)

    def test_model_failure_falls_back(self):
        brain = StubBrain([RuntimeError("down")])
        out = diagnose_telemetry(["nan"], self.NET, "W", brain)
        self.assertIsNone(out["suspect"])
        self.assertIn("wire", out["message"].lower())

    def test_mock_brain_uses_fallback_without_calling(self):
        brain = StubBrain([])
        brain.mock = True
        out = diagnose_telemetry(["nan"], self.NET, "W", brain)
        self.assertEqual(brain.calls, [])
        self.assertIsNone(out["suspect"])


class TelemetryEndpointTest(unittest.TestCase):
    def test_stream_unknown_session_404(self):
        import backend.app.main as app_main
        from fastapi import HTTPException

        fake = SimpleNamespace(sessions={})
        with mock.patch.object(app_main, "orchestrator", fake):
            with self.assertRaises(HTTPException) as ctx:
                app_main.api_telemetry_stream("nope", port="COM3")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_stream_body_frames_lines(self):
        import backend.app.main as app_main

        frames = [("ready", {"port": "COM7", "baud": 115200}),
                  ("line", {"line": "Distance: 45cm"})]
        with mock.patch("backend.app.telemetry.iter_telemetry_events",
                        return_value=iter(frames)) as events:
            body = "".join(app_main.telemetry_sse_body("COM7", 115200))
        events.assert_called_once_with("COM7", 115200)
        self.assertIn("event: ready", body)
        self.assertIn("Distance: 45cm", body)

    def test_stream_releases_probes(self):
        import backend.app.main as app_main

        closed = []
        fake_orc = SimpleNamespace(
            sessions={"s1": {"learning": True}},
            probe=SimpleNamespace(
                port="COM9",
                close=lambda: closed.append("orc")))
        fake_live = SimpleNamespace(
            probe=SimpleNamespace(close=lambda: closed.append("live")))
        with mock.patch.object(app_main, "orchestrator", fake_orc), \
                mock.patch.object(app_main, "live_guide", fake_live):
            resp = app_main.api_telemetry_stream("s1", port="COM7")
        self.assertEqual(closed, ["orc", "live"])
        self.assertEqual(resp.media_type, "text/event-stream")

    def test_stream_needs_a_port(self):
        import backend.app.main as app_main

        body = "".join(app_main.telemetry_sse_body("", 115200))
        self.assertIn("event: error", body)
        self.assertIn("no serial port", body)
        fake_orc = SimpleNamespace(
            sessions={"s1": {"learning": True}},
            probe=SimpleNamespace(port="", close=lambda: None))
        with mock.patch.object(app_main, "orchestrator", fake_orc):
            resp = app_main.api_telemetry_stream("s1", port="")
        self.assertEqual(resp.media_type, "text/event-stream")

    def test_diagnose_unknown_session_404(self):
        import backend.app.main as app_main
        from fastapi import HTTPException

        fake = SimpleNamespace(sessions={})
        with mock.patch.object(app_main, "orchestrator", fake):
            with self.assertRaises(HTTPException) as ctx:
                app_main.api_telemetry_diagnose("nope", {"lines": []})
        self.assertEqual(ctx.exception.status_code, 404)

    def test_diagnose_returns_model_answer(self):
        import backend.app.main as app_main
        from backend.app.boards import pin_map_for

        brain = StubBrain([json.dumps({
            "message": "ECHO looks loose.",
            "suspect": {"part": "hc_sr04", "part_pin": "ECHO"}})])
        fake_orc = SimpleNamespace(
            sessions={"s1": {
                "learning": True, "project_title": "Parking",
                "board": "esp32_devkit",
                "pin_map": pin_map_for("esp32_devkit"),
                "plan": [{"n": 1, "instruction": "Wire the sensor.",
                          "verify": "CHECK_GPIO", "expect": {},
                          "depends_on_parts": ["hc_sr04"]}]}},
            Muse=brain)
        with mock.patch.object(app_main, "orchestrator", fake_orc):
            out = app_main.api_telemetry_diagnose(
                "s1", {"lines": ["Distance: nan"]})
        self.assertIn("ECHO", out["message"])
        self.assertTrue(out["suspect"]["board_pin"].startswith("GPIO"))


if __name__ == "__main__":
    unittest.main()
