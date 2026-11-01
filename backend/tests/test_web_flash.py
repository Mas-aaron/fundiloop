"""Firmware manifest + binary downloads for browser-side flashing.

The page compiles on the server (cached), downloads the binaries, and
drives the vendored esptool-js bundle over Web Serial. Hermetic:
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


class FirmwareEndpointTest(unittest.TestCase):
    def setUp(self):
        import backend.app.main as app_main

        self.app_main = app_main
        self.plan = [{"n": 1, "instruction": "Blink.", "verify": "CHECK_GPIO",
                      "expect": {}, "depends_on_parts": ["led"]}]
        self.session = {"learning": True, "board": "esp32_devkit",
                        "pin_map": pin_map_for("esp32_devkit"),
                        "plan": self.plan, "step_code": {}}
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
        build = {"ok": ok, "build_hash": "abc123",
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
        out = self.app_main.api_step_firmware("s1", 1)
        self.assertEqual(out["build_hash"], "abc123")
        self.assertEqual(out["source"], "template")
        self.assertEqual([(f["index"], f["offset"], f["size"])
                          for f in out["files"]],
                         [(0, "0x1000", 8), (1, "0x10000", 32)])
        self.assertEqual(out["files"][1]["url"],
                         "/project/s1/step/1/firmware/1")

    def test_manifest_unknown_session_is_404(self):
        from fastapi import HTTPException

        self._stub_build()
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_firmware("nope", 1)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_manifest_unknown_step_is_404(self):
        from fastapi import HTTPException

        self._stub_build()
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_firmware("s1", 7)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_manifest_uno_is_400(self):
        from fastapi import HTTPException

        self.session["board"] = "arduino_uno"
        self._stub_build()
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_firmware("s1", 1)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_manifest_compile_failure_is_500(self):
        from fastapi import HTTPException

        self._stub_build(ok=False)
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_firmware("s1", 1)
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("compile boom", ctx.exception.detail)

    def test_file_serves_binary(self):
        self._stub_build()
        resp = self.app_main.api_step_firmware_file("s1", 1, 1)
        self.assertEqual(resp.path, self.main)
        self.assertEqual(resp.media_type, "application/octet-stream")

    def test_file_bad_index_is_404(self):
        from fastapi import HTTPException

        self._stub_build()
        for bad in (-1, 2):
            with self.assertRaises(HTTPException) as ctx:
                self.app_main.api_step_firmware_file("s1", 1, bad)
            self.assertEqual(ctx.exception.status_code, 404)

    def test_file_outside_build_dir_is_404(self):
        from fastapi import HTTPException

        build = self._stub_build()
        build["artifacts"][0]["file"] = os.path.abspath(__file__)
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_firmware_file("s1", 1, 0)
        self.assertEqual(ctx.exception.status_code, 404)

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


if __name__ == "__main__":
    unittest.main()
