"""One-click ESP32 flashing: validation, stubbed esptool, handle release, audit.

Hermetic: compile + esptool are stubbed, the audit trail lands in a temp
file, and no serial port is touched. No hardware, no network, no model
calls in these tests.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_firmware_flash -v
"""
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.app import firmware_flash as ff
from backend.app.boards import pin_map_for


ARTIFACTS = [{"offset": "0x1000", "file": "boot.bin"},
             {"offset": "0x10000", "file": "sketch.bin"}]


class FlashArtifactsTest(unittest.TestCase):
    def test_success_flashes_offsets_in_order(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="Wrote 16 bytes",
                                   stderr="")

        out = ff.flash_artifacts(ARTIFACTS, "COM3", runner=run)
        self.assertTrue(out["ok"])
        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertIn("write_flash", cmd)
        self.assertIn("COM3", cmd)
        self.assertLess(cmd.index("0x1000"), cmd.index("0x10000"))
        self.assertIn("sketch.bin", cmd)

    def test_failure_surfaces_excerpt(self):
        def run(*args, **kwargs):
            return SimpleNamespace(returncode=2, stdout="",
                                   stderr="esptool: chip sync error")

        out = ff.flash_artifacts(ARTIFACTS, "COM3", runner=run)
        self.assertFalse(out["ok"])
        self.assertIn("chip sync", out["log_excerpt"])

    def test_missing_esptool_is_honest(self):
        def run(*args, **kwargs):
            raise FileNotFoundError("no esptool")

        out = ff.flash_artifacts(ARTIFACTS, "COM3", runner=run)
        self.assertFalse(out["ok"])
        self.assertIn("pip install esptool", out["log_excerpt"])

    def test_timeout_is_honest(self):
        def run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

        out = ff.flash_artifacts(ARTIFACTS, "COM3", runner=run)
        self.assertFalse(out["ok"])
        self.assertIn("timed out", out["log_excerpt"])

    def test_no_artifacts_refuses(self):
        out = ff.flash_artifacts([], "COM3",
                                 runner=lambda *a, **k: None)
        self.assertFalse(out["ok"])
        self.assertIn("nothing to flash", out["log_excerpt"])


class BusyPortTest(unittest.TestCase):
    def test_busy_log_detected(self):
        self.assertTrue(ff.is_busy_port("could not open port 'COM3'"))
        self.assertTrue(ff.is_busy_port("PermissionError(13, 'Access is denied')"))
        self.assertTrue(ff.is_busy_port("SerialException: device busy"))
        self.assertFalse(ff.is_busy_port("chip sync error"))
        self.assertFalse(ff.is_busy_port(""))

    def test_guidance_names_port_and_serial_monitor(self):
        text = ff.busy_port_guidance("COM3")
        self.assertIn("COM3", text)
        self.assertIn("Serial Monitor", text)


class CheckPortTest(unittest.TestCase):
    def test_open_port_passes_and_closes(self):
        closed = []

        class Handle:
            def close(self):
                closed.append(True)

        fake = SimpleNamespace(Serial=lambda *a, **k: Handle())
        with mock.patch.dict(sys.modules, {"serial": fake}):
            out = ff.check_port("COM3")
        self.assertTrue(out["ok"])
        self.assertTrue(closed)

    def test_held_port_reports_detail(self):
        class Busy(Exception):
            pass

        def raiser(*args, **kwargs):
            raise Busy("could not open port 'COM3'")

        fake = SimpleNamespace(Serial=raiser, SerialException=Busy)
        with mock.patch.dict(sys.modules, {"serial": fake}):
            out = ff.check_port("COM3")
        self.assertFalse(out["ok"])
        self.assertIn("COM3", out["detail"])

    def test_missing_pyserial_defers_to_esptool(self):
        with mock.patch.dict(sys.modules, {"serial": None}):
            out = ff.check_port("COM3")
        self.assertTrue(out["ok"])


class VerifyPingTest(unittest.TestCase):
    def test_answered(self):
        probe = SimpleNamespace(send=lambda cmd: {"cmd": "PING",
                                                  "status": "OK"})
        self.assertEqual(ff.verify_ping(probe)["answered"], True)

    def test_silent_is_reported_not_raised(self):
        probe = SimpleNamespace(
            send=lambda cmd: {"cmd": "PING", "error": "timeout"})
        out = ff.verify_ping(probe)
        self.assertFalse(out["answered"])
        self.assertIn("timeout", out["reply"])

    def test_raising_probe_never_raises(self):
        def send(cmd):
            raise RuntimeError("port gone")

        out = ff.verify_ping(SimpleNamespace(send=send))
        self.assertFalse(out["answered"])

    def test_settle_skipped_for_mock(self):
        with mock.patch("backend.app.firmware_flash.time.sleep") as nap:
            ff.settle_before_ping(SimpleNamespace(mock=True))
        nap.assert_not_called()


class FlashEndpointTest(unittest.TestCase):
    def setUp(self):
        import backend.app.main as app_main

        self.app_main = app_main
        self.plan = [{"n": 1, "instruction": "Blink.", "verify": "CHECK_GPIO",
                      "expect": {}, "depends_on_parts": ["led"]}]
        self.session = {"learning": True, "board": "esp32_devkit",
                        "pin_map": pin_map_for("esp32_devkit"),
                        "plan": self.plan, "step_code": {}}
        self.orc_closed, self.live_closed = [], []
        orc_probe = SimpleNamespace(
            mock=True, close=lambda: self.orc_closed.append(True),
            send=lambda cmd: {"cmd": cmd, "status": "OK"})
        fake_orc = SimpleNamespace(sessions={"s1": self.session},
                                   Muse=SimpleNamespace(mock=True),
                                   probe=orc_probe)
        fake_live = SimpleNamespace(
            probe=SimpleNamespace(
                mock=True, close=lambda: self.live_closed.append(True)))
        self.patches = [
            mock.patch.object(app_main, "orchestrator", fake_orc),
            mock.patch.object(app_main, "live_guide", fake_live),
            mock.patch("backend.app.firmware_flash.check_port",
                       return_value={"ok": True, "detail": ""}),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        audit = os.path.join(self.tmp.name, "coach_audit.jsonl")
        env = mock.patch.dict(os.environ, {"COACH_AUDIT_LOG": audit})
        env.start()
        self.addCleanup(env.stop)
        self.audit = audit

    def _stub_pipeline(self, build_ok=True, flash_ok=True, flash_log="wrote"):
        build = {"ok": build_ok, "build_hash": "abc123",
                 "build_dir": self.tmp.name, "artifacts": ARTIFACTS,
                 "fqbn": "esp32:esp32:esp32",
                 "log_excerpt": "" if build_ok else "compile boom",
                 "cached": False}
        flashed = {"ok": flash_ok, "log_excerpt": flash_log}
        c_patch = mock.patch("backend.app.firmware_build.compile_sketch",
                             return_value=build)
        f_patch = mock.patch("backend.app.firmware_flash.flash_artifacts",
                             return_value=flashed)
        c_patch.start()
        f_patch.start()
        self.addCleanup(c_patch.stop)
        self.addCleanup(f_patch.stop)
        return build, flashed

    def _audit_events(self):
        from backend.app.coach import read_audit_log, verify_audit_chain

        events = read_audit_log()
        self.assertTrue(verify_audit_chain(events))
        return [e for e in events if e.get("event") == "flash"]

    def test_success_releases_handles_and_audits(self):
        self._stub_pipeline()
        out = self.app_main.api_step_flash("s1", 1, {"port": "COM3"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["build_hash"], "abc123")
        self.assertEqual(out["port"], "COM3")
        self.assertTrue(out["verify_ping"]["answered"])
        self.assertEqual(out["source"], "template")
        self.assertTrue(self.orc_closed)
        self.assertTrue(self.live_closed)
        flashes = self._audit_events()
        self.assertEqual(len(flashes), 1)
        self.assertTrue(flashes[0]["ok"])
        self.assertEqual(flashes[0]["args"]["port"], "COM3")

    def test_busy_flash_failure_carries_guidance_and_audits(self):
        from fastapi import HTTPException

        self._stub_pipeline(flash_ok=False,
                            flash_log="could not open port 'COM3'")
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_flash("s1", 1, {"port": "COM3"})
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("Serial Monitor", ctx.exception.detail)
        flashes = self._audit_events()
        self.assertEqual(len(flashes), 1)
        self.assertFalse(flashes[0]["ok"])

    def test_compile_failure_is_500_and_audited(self):
        from fastapi import HTTPException

        self._stub_pipeline(build_ok=False)
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_flash("s1", 1, {"port": "COM3"})
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("compile failed", ctx.exception.detail)
        self.assertEqual(len(self._audit_events()), 1)
        self.assertFalse(self.orc_closed)  # no port held: nothing to release

    def test_unknown_session_is_404(self):
        from fastapi import HTTPException

        self._stub_pipeline()
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_flash("nope", 1, {"port": "COM3"})
        self.assertEqual(ctx.exception.status_code, 404)

    def test_unknown_step_is_404(self):
        from fastapi import HTTPException

        self._stub_pipeline()
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_flash("s1", 7, {"port": "COM3"})
        self.assertEqual(ctx.exception.status_code, 404)

    def test_uno_board_is_400(self):
        from fastapi import HTTPException

        self.session["board"] = "arduino_uno"
        self._stub_pipeline()
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_flash("s1", 1, {"port": "COM3"})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_missing_port_is_400(self):
        from fastapi import HTTPException

        self._stub_pipeline()
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_flash("s1", 1, {"port": " "})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_busy_port_fails_fast_with_409(self):
        from fastapi import HTTPException

        self._stub_pipeline()
        with mock.patch("backend.app.firmware_flash.check_port",
                        return_value={"ok": False,
                                      "detail": "Access denied"}):
            with mock.patch(
                    "backend.app.firmware_build.compile_sketch") as comp:
                with self.assertRaises(HTTPException) as ctx:
                    self.app_main.api_step_flash("s1", 1, {"port": "COM3"})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Serial Monitor", ctx.exception.detail)
        comp.assert_not_called()
        flashes = self._audit_events()
        self.assertEqual(len(flashes), 1)
        self.assertEqual(flashes[0]["reason"], "port_busy")


if __name__ == "__main__":
    unittest.main()
