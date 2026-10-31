"""Offline ESP32 firmware builds via arduino-cli (cached by code hash).

Hermetic: subprocess is stubbed, the toolchain is never shelled, and
every build lands in a temp dir. No hardware, no network, no model
calls in these tests.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_firmware_build -v
"""
import json
import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.app import firmware_build as fb


def _ok_proc(stdout="compiled fine"):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _fail_proc(stderr="error: missing header"):
    return SimpleNamespace(returncode=1, stdout="", stderr=stderr)


class FirmwareBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ,
                                  {"FIRMWARE_BUILD_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _runner_ok(self, calls):
        def run(cmd, **kwargs):
            calls.append(cmd)
            outdir = cmd[cmd.index("--output-dir") + 1]
            with open(os.path.join(outdir, "step_sketch.ino.bin"),
                      "wb") as fh:
                fh.write(b"\x00" * 16)
            return _ok_proc()
        return run

    def test_success_caches_by_code_hash(self):
        calls = []
        first = fb.compile_sketch("void setup(){} void loop(){}",
                                  runner=self._runner_ok(calls))
        self.assertTrue(first["ok"])
        self.assertFalse(first["cached"])
        self.assertEqual(len(first["artifacts"]), 1)
        self.assertEqual(first["artifacts"][0]["offset"], "0x10000")
        self.assertEqual(len(calls), 1)
        self.assertIn("compile", calls[0])
        second = fb.compile_sketch("void setup(){} void loop(){}",
                                   runner=self._runner_ok(calls))
        self.assertTrue(second["ok"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["build_hash"], first["build_hash"])
        self.assertEqual(len(calls), 1)  # cache hit: no second compile

    def test_different_code_rebuilds(self):
        calls = []
        runner = self._runner_ok(calls)
        one = fb.compile_sketch("void setup(){1}", runner=runner)
        two = fb.compile_sketch("void setup(){2}", runner=runner)
        self.assertNotEqual(one["build_hash"], two["build_hash"])
        self.assertEqual(len(calls), 2)

    def test_bootloader_and_partitions_at_standard_offsets(self):
        def run(cmd, **kwargs):
            outdir = cmd[cmd.index("--output-dir") + 1]
            for name in ("step_sketch.ino.bootloader.bin",
                         "step_sketch.ino.partitions.bin",
                         "step_sketch.ino.bin"):
                with open(os.path.join(outdir, name), "wb") as fh:
                    fh.write(b"\x00")
            return _ok_proc()
        out = fb.compile_sketch("void setup(){} void loop(){}",
                                runner=run)
        self.assertTrue(out["ok"])
        self.assertEqual([(a["offset"], os.path.basename(a["file"]))
                          for a in out["artifacts"]],
                         [("0x1000", "step_sketch.ino.bootloader.bin"),
                          ("0x8000", "step_sketch.ino.partitions.bin"),
                          ("0x10000", "step_sketch.ino.bin")])

    def test_compile_failure_surfaces_log_excerpt(self):
        out = fb.compile_sketch("void setup(){} void loop(){}",
                                runner=lambda *a, **k: _fail_proc())
        self.assertFalse(out["ok"])
        self.assertIn("missing header", out["log_excerpt"])
        self.assertEqual(out["artifacts"], [])

    def test_missing_cli_is_honest(self):
        def run(*args, **kwargs):
            raise FileNotFoundError("no arduino-cli")
        out = fb.compile_sketch("void setup(){} void loop(){}",
                                runner=run)
        self.assertFalse(out["ok"])
        self.assertIn("arduino-cli not found", out["log_excerpt"])

    def test_timeout_is_honest(self):
        def run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)
        out = fb.compile_sketch("void setup(){} void loop(){}",
                                runner=run)
        self.assertFalse(out["ok"])
        self.assertIn("timed out", out["log_excerpt"])

    def test_success_without_bins_is_honest(self):
        out = fb.compile_sketch("void setup(){} void loop(){}",
                                runner=lambda *a, **k: _ok_proc())
        self.assertFalse(out["ok"])
        self.assertIn("no .bin outputs", out["log_excerpt"])

    def test_corrupt_cache_rebuilds(self):
        calls = []
        out = fb.compile_sketch("void setup(){} void loop(){}",
                                runner=self._runner_ok(calls))
        with open(os.path.join(out["build_dir"], "build.json"),
                  "w", encoding="utf-8") as fh:
            fh.write("{not json")
        again = fb.compile_sketch("void setup(){} void loop(){}",
                                  runner=self._runner_ok(calls))
        self.assertFalse(again["cached"])
        self.assertEqual(len(calls), 2)

    def test_missing_artifact_invalidates_cache(self):
        calls = []
        out = fb.compile_sketch("void setup(){} void loop(){}",
                                runner=self._runner_ok(calls))
        os.remove(out["artifacts"][0]["file"])
        again = fb.compile_sketch("void setup(){} void loop(){}",
                                  runner=self._runner_ok(calls))
        self.assertFalse(again["cached"])
        self.assertEqual(len(calls), 2)

    def test_uno_board_refuses(self):
        with self.assertRaises(ValueError):
            fb.compile_sketch("void setup(){} void loop(){}",
                              board="arduino_uno",
                              runner=lambda *a, **k: _ok_proc())

    def test_empty_sketch_refuses(self):
        out = fb.compile_sketch("   ", runner=lambda *a, **k: _ok_proc())
        self.assertFalse(out["ok"])
        self.assertIn("empty sketch", out["log_excerpt"])

    def test_cli_path_and_fqbn_from_env(self):
        calls = []
        with mock.patch.dict(os.environ,
                             {"ARDUINO_CLI_BIN": "/opt/arduino-cli",
                              "ARDUINO_FQBN": "esp32:esp32:esp32da"}):
            fb.compile_sketch("void setup(){} void loop(){}",
                              runner=self._runner_ok(calls))
        self.assertEqual(calls[0][0], "/opt/arduino-cli")
        self.assertIn("esp32:esp32:esp32da", calls[0])

    def test_build_record_persists(self):
        out = fb.compile_sketch("void setup(){} void loop(){}",
                                runner=self._runner_ok([]))
        with open(os.path.join(out["build_dir"], "build.json"),
                  encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(record["build_hash"], out["build_hash"])
        self.assertEqual(record["fqbn"], fb.arduino_fqbn())


if __name__ == "__main__":
    unittest.main()
