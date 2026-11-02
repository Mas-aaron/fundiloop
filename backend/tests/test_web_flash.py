"""Browser flashing: vendored esptool-js bundle + final firmware flash.

The page drives the vendored esptool-js bundle over Web Serial and
flashes the finished firmware through the server path. Hermetic:
compile is stubbed and binaries live in temp files. No hardware, no
network, no model calls in these tests.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_web_flash -v
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.app.boards import pin_map_for


VALID_BUNDLE = b"// esptool-js ESPLoader Transport " + b"x" * 6000


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._data


class BundleEndpointTest(unittest.TestCase):
    def setUp(self):
        import backend.app.main as app_main

        self.app_main = app_main
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_bundle_served_when_present(self):
        from pathlib import Path

        dest = Path(self.tmp.name) / "esptool-js"
        dest.mkdir()
        (dest / "bundle.js").write_bytes(VALID_BUNDLE)
        with mock.patch.object(self.app_main, "FRONTEND",
                               Path(self.tmp.name)):
            resp = self.app_main.esptool_js_bundle()
        self.assertTrue(str(resp.path).endswith("bundle.js"))
        self.assertEqual(resp.media_type, "text/javascript")

    def test_bundle_fetched_once_when_missing(self):
        from pathlib import Path

        with mock.patch.object(self.app_main, "FRONTEND",
                               Path(self.tmp.name)):
            with mock.patch("urllib.request.urlopen",
                            return_value=_FakeResp(VALID_BUNDLE)):
                resp = self.app_main.esptool_js_bundle()
        self.assertTrue(str(resp.path).endswith("bundle.js"))
        with open(os.path.join(self.tmp.name, "esptool-js", "bundle.js"),
                  "rb") as fh:
            self.assertEqual(fh.read(), VALID_BUNDLE)

    def test_bundle_fetch_failure_is_404_with_manual_fallback(self):
        from fastapi import HTTPException
        from pathlib import Path

        def boom(url, **kwargs):
            raise OSError("offline")

        with mock.patch.object(self.app_main, "FRONTEND",
                               Path(self.tmp.name)):
            with mock.patch("urllib.request.urlopen", side_effect=boom):
                with self.assertRaises(HTTPException) as ctx:
                    self.app_main.esptool_js_bundle()
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("manual fallback", ctx.exception.detail)

    def test_bundle_invalid_bytes_is_404(self):
        from fastapi import HTTPException
        from pathlib import Path

        with mock.patch.object(self.app_main, "FRONTEND",
                               Path(self.tmp.name)):
            with mock.patch("urllib.request.urlopen",
                            return_value=_FakeResp(b"nope")):
                with self.assertRaises(HTTPException) as ctx:
                    self.app_main.esptool_js_bundle()
        self.assertEqual(ctx.exception.status_code, 404)


class FinalFirmwareEndpointTest(unittest.TestCase):
    def setUp(self):
        import backend.app.main as app_main

        self.app_main = app_main
        self.session = {
            "learning": True, "board": "esp32_devkit",
            "project_title": "Blink", "project_id": "blink",
            "pin_map": pin_map_for("esp32_devkit"),
            "plan": [{"n": 1, "instruction": "Blink.",
                      "verify": "CHECK_GPIO", "expect": {},
                      "depends_on_parts": ["led"]}],
            "visible_parts": ["esp32", "led"],
            "verified": {"i2c": [], "steps": [{"n": 1}]},
            "step_index": 1, "code_ready": True}
        fake_orc = SimpleNamespace(sessions={"s1": self.session},
                                   Muse=SimpleNamespace(mock=True))
        patcher = mock.patch.object(app_main, "orchestrator", fake_orc)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.boot = os.path.join(self.tmp.name, "boot.bin")
        self.main = os.path.join(self.tmp.name, "sketch.bin")
        with open(self.boot, "wb") as fh:
            fh.write(b"\x00" * 8)
        with open(self.main, "wb") as fh:
            fh.write(b"\x01" * 32)

    def _stub_build(self, ok=True):
        build = {"ok": ok, "build_hash": "fin411",
                 "build_dir": self.tmp.name,
                 "artifacts": ([{"offset": "0x1000", "file": self.boot},
                                {"offset": "0x10000", "file": self.main}]
                               if ok else []),
                 "fqbn": "esp32:esp32:esp32",
                 "log_excerpt": "" if ok else "compile boom",
                 "cached": False}
        patcher = mock.patch("backend.app.firmware_build.compile_sketch",
                             return_value=build)
        patcher.start()
        self.addCleanup(patcher.stop)
        return build

    def test_manifest_lists_files_with_urls(self):
        self._stub_build()
        out = self.app_main.api_project_firmware("s1")
        self.assertEqual(out["build_hash"], "fin411")
        self.assertEqual(out["source"], "final")
        self.assertEqual([(f["index"], f["offset"], f["size"])
                          for f in out["files"]],
                         [(0, "0x1000", 8), (1, "0x10000", 32)])
        self.assertEqual(out["files"][1]["url"],
                         "/project/s1/firmware/1")

    def test_manifest_unknown_session_is_404(self):
        from fastapi import HTTPException

        self._stub_build()
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_project_firmware("nope")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_manifest_uno_is_400(self):
        from fastapi import HTTPException

        self.session["board"] = "arduino_uno"
        self._stub_build()
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_project_firmware("s1")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_manifest_wiring_incomplete_is_409(self):
        from fastapi import HTTPException

        self.session["plan"].append(
            {"n": 2, "instruction": "More.", "verify": "CHECK_GPIO",
             "expect": {}, "depends_on_parts": ["led"]})
        self.session["step_index"] = 0
        self._stub_build()
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_project_firmware("s1")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("wire every step first", ctx.exception.detail)

    def test_manifest_compile_failure_is_500(self):
        from fastapi import HTTPException

        self._stub_build(ok=False)
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_project_firmware("s1")
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("compile boom", ctx.exception.detail)

    def test_file_serves_binary(self):
        self._stub_build()
        resp = self.app_main.api_project_firmware_file("s1", 1)
        self.assertEqual(resp.path, self.main)
        self.assertEqual(resp.media_type, "application/octet-stream")

    def test_file_bad_index_is_404(self):
        from fastapi import HTTPException

        self._stub_build()
        for bad in (-1, 2):
            with self.assertRaises(HTTPException) as ctx:
                self.app_main.api_project_firmware_file("s1", bad)
            self.assertEqual(ctx.exception.status_code, 404)

    def test_file_outside_build_dir_is_404(self):
        from fastapi import HTTPException

        build = self._stub_build()
        build["artifacts"][0]["file"] = os.path.abspath(__file__)
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_project_firmware_file("s1", 0)
        self.assertEqual(ctx.exception.status_code, 404)


class ProjectFlashEndpointTest(unittest.TestCase):
    def setUp(self):
        import backend.app.main as app_main

        self.app_main = app_main
        self.session = {
            "learning": True, "board": "esp32_devkit",
            "project_title": "Blink", "project_id": "blink",
            "pin_map": pin_map_for("esp32_devkit"),
            "plan": [{"n": 1, "instruction": "Blink.",
                      "verify": "CHECK_GPIO", "expect": {},
                      "depends_on_parts": ["led"]}],
            "visible_parts": ["esp32", "led"],
            "verified": {"i2c": [], "steps": [{"n": 1}]},
            "step_index": 1, "code_ready": True}
        fake_orc = SimpleNamespace(sessions={"s1": self.session},
                                   Muse=SimpleNamespace(mock=True))
        patcher = mock.patch.object(app_main, "orchestrator", fake_orc)
        patcher.start()
        self.addCleanup(patcher.stop)
        port = mock.patch("backend.app.main._require_flash_port",
                          return_value="COM3")
        port.start()
        self.addCleanup(port.stop)

    def _stub_build(self, ok=True):
        build = {"ok": ok, "build_hash": "fin411",
                 "build_dir": "/tmp/x",
                 "artifacts": [{"offset": "0x10000", "file": "/tmp/x/a.bin"}],
                 "fqbn": "esp32:esp32:esp32",
                 "log_excerpt": "" if ok else "compile boom",
                 "cached": False}
        patcher = mock.patch("backend.app.firmware_build.compile_sketch",
                             return_value=build)
        patcher.start()
        self.addCleanup(patcher.stop)
        return build

    def test_final_flash_success(self):
        build = self._stub_build()
        written = {}

        def fake_write(sid, label, port, b, source):
            written.update(sid=sid, label=label, port=port, b=b,
                           source=source)
            return {"ok": True, "session_id": sid, "port": port,
                    "build_hash": b["build_hash"], "cached": False,
                    "verify_ping": {"answered": True}, "source": source}

        with mock.patch("backend.app.main._write_build_to_port",
                        side_effect=fake_write):
            out = self.app_main.api_project_flash("s1", {"port": "COM3"})
        self.assertTrue(out["final"])
        self.assertEqual(
            (written["sid"], written["label"], written["port"],
             written["source"]),
            ("s1", "final", "COM3", "final"))
        self.assertIs(written["b"], build)

    def test_wiring_incomplete_is_409(self):
        from fastapi import HTTPException

        self.session["plan"].append(
            {"n": 2, "instruction": "More.", "verify": "CHECK_GPIO",
             "expect": {}, "depends_on_parts": ["led"]})
        self.session["code_ready"] = False  # proof not required; wiring is
        self.session["step_index"] = 0
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_project_flash("s1", {"port": "COM3"})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("wire every step first", ctx.exception.detail)

    def test_unknown_session_is_404(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_project_flash("nope", {"port": "COM3"})
        self.assertEqual(ctx.exception.status_code, 404)

    def test_uno_board_is_400(self):
        from fastapi import HTTPException

        self.session["board"] = "arduino_uno"
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_project_flash("s1", {"port": "COM3"})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_model_failure_is_502(self):
        from fastapi import HTTPException

        from backend.app.adapters import MetaAPIError

        def fail(*args, **kwargs):
            raise MetaAPIError("api down")

        self.app_main.orchestrator.Muse = SimpleNamespace(
            mock=False, complete_text=fail)
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_project_flash("s1", {"port": "COM3"})
        self.assertEqual(ctx.exception.status_code, 502)

    def test_compile_failure_is_500_and_audited(self):
        from fastapi import HTTPException

        self._stub_build(ok=False)
        with mock.patch("backend.app.firmware_flash.audit_flash") as audit:
            with self.assertRaises(HTTPException) as ctx:
                self.app_main.api_project_flash("s1", {"port": "COM3"})
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("compile boom", ctx.exception.detail)
        audit.assert_called_once()
        args, _ = audit.call_args
        self.assertEqual(args[:6], ("s1", "final", "COM3", "", False,
                                   "compile_failed"))


class EnsureBundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_valid_file_needs_no_fetch(self):
        from backend.app.esptool_js import ensure_bundle

        with open(os.path.join(self.tmp.name, "bundle.js"), "wb") as fh:
            fh.write(VALID_BUNDLE)
        calls = []
        opener = lambda url: calls.append(url) or _FakeResp(b"nope")  # noqa: E731
        path = ensure_bundle(self.tmp.name, opener=opener)
        self.assertEqual(calls, [])
        self.assertTrue(path.endswith("bundle.js"))

    def test_invalid_file_is_refetched(self):
        from backend.app.esptool_js import ensure_bundle

        with open(os.path.join(self.tmp.name, "bundle.js"), "wb") as fh:
            fh.write(b"stale junk")
        calls = []
        opener = lambda url: calls.append(url) or _FakeResp(VALID_BUNDLE)  # noqa: E731
        path = ensure_bundle(self.tmp.name, opener=opener)
        self.assertEqual(len(calls), 1)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), VALID_BUNDLE)


class SerialReleaseTest(unittest.TestCase):
    """POST /api/serial/release drops both probe handles for Web Serial."""

    def test_release_closes_both_probes(self):
        import backend.app.main as app_main

        orc_probe = mock.Mock()
        live_probe = mock.Mock()
        with mock.patch.object(app_main, "orchestrator",
                               SimpleNamespace(probe=orc_probe)), \
             mock.patch.object(app_main, "live_guide",
                               SimpleNamespace(probe=live_probe)):
            out = app_main.api_serial_release()
        self.assertEqual(out, {"ok": True})
        orc_probe.close.assert_called_once_with()
        live_probe.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
