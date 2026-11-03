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

    def test_excerpt_leads_with_root_errors(self):
        log = "\n".join(
            ["Error loading hardware platform: bad json"]
            + ["filler %d" % i for i in range(40)]
            + ["Error during build: instance no longer valid"])
        proc = SimpleNamespace(returncode=1, stdout=log, stderr="")
        out = fb.compile_sketch("void setup(){} void loop(){}",
                                runner=lambda *a, **k: proc)
        self.assertIn("Error loading hardware platform", out["log_excerpt"])
        self.assertIn("instance no longer valid", out["log_excerpt"])
        self.assertLess(out["log_excerpt"].index("Error loading"),
                        out["log_excerpt"].index("instance no longer"))

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

    def test_sketch_dir_matches_ino_name(self):
        def run(cmd, **kwargs):
            outdir = cmd[cmd.index("--output-dir") + 1]
            sketchdir = cmd[-1]
            ino = os.path.join(sketchdir,
                               os.path.basename(sketchdir) + ".ino")
            assert os.path.isfile(ino), "arduino needs <dir>/<dir>.ino"
            with open(os.path.join(outdir, "step_sketch.ino.bin"),
                      "wb") as fh:
                fh.write(b"\x00" * 16)
            return _ok_proc()

        out = fb.compile_sketch("void setup(){} void loop(){}",
                                runner=run)
        self.assertTrue(out["ok"])

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


_OLED_SKETCH = "\n".join([
    "#include <Wire.h>",
    "#include <Adafruit_GFX.h>",
    '#include "Adafruit_SSD1306.h"',
    "#include <Adafruit_GFX.h>",  # duplicate install name
    "#include <SomeUnknownLib.h>",  # unmapped: compile reports it
    "void setup(){} void loop(){}",
])


def _lib_list_proc(*names):
    payload = {"installed_libraries": [{"library": {"name": n}}
                                       for n in names]}
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload),
                           stderr="")


class LibraryAutoInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ,
                                  {"FIRMWARE_BUILD_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _runner(self, calls, list_proc=None, install_proc=None,
                compile_proc=None):
        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[1:3] == ["lib", "list"]:
                return list_proc if list_proc is not None \
                    else _ok_proc(stdout="{}")
            if cmd[1:3] == ["lib", "install"]:
                return install_proc if install_proc is not None \
                    else _ok_proc(stdout="installed")
            outdir = cmd[cmd.index("--output-dir") + 1]
            with open(os.path.join(outdir, "step_sketch.ino.bin"),
                      "wb") as fh:
                fh.write(b"\x00" * 16)
            return compile_proc if compile_proc is not None else _ok_proc()
        return run

    def test_needed_libraries_mapping(self):
        self.assertEqual(fb.needed_libraries(_OLED_SKETCH),
                         ["Adafruit GFX Library", "Adafruit SSD1306"])

    def test_needed_libraries_empty_for_bare_sketch(self):
        self.assertEqual(fb.needed_libraries("void setup(){} void loop(){}"),
                         [])

    def test_needed_libraries_maps_esp32_servo(self):
        self.assertEqual(
            fb.needed_libraries("#include <ESP32Servo.h>\n"
                                "void setup(){} void loop(){}"),
            ["ESP32Servo"])

    def test_installs_only_missing_before_compile(self):
        calls = []
        out = fb.compile_sketch(
            _OLED_SKETCH,
            runner=self._runner(
                calls, list_proc=_lib_list_proc("Adafruit GFX Library")))
        kinds = [c[1] for c in calls]
        self.assertEqual(kinds, ["lib", "lib", "compile"])
        self.assertEqual(calls[1][:3], [calls[1][0], "lib", "install"])
        self.assertEqual(calls[1][3:], ["Adafruit SSD1306"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["libraries"], ["Adafruit SSD1306"])

    def test_skips_install_when_all_present(self):
        calls = []
        out = fb.compile_sketch(
            _OLED_SKETCH,
            runner=self._runner(
                calls, list_proc=_lib_list_proc("Adafruit GFX Library",
                                                "Adafruit SSD1306")))
        self.assertEqual([c[1] for c in calls], ["lib", "compile"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["libraries"], [])

    def test_bad_list_output_installs_everything_needed(self):
        calls = []
        fb.compile_sketch(
            _OLED_SKETCH,
            runner=self._runner(calls, list_proc=_ok_proc(stdout="garbage")))
        installs = [c for c in calls if c[1:3] == ["lib", "install"]]
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0][3:],
                         ["Adafruit GFX Library", "Adafruit SSD1306"])

    def test_install_failure_still_compiles(self):
        calls = []
        out = fb.compile_sketch(
            _OLED_SKETCH,
            runner=self._runner(calls, install_proc=_fail_proc("no net")))
        self.assertIn("compile", [c[1] for c in calls])
        self.assertTrue(out["ok"])
        self.assertEqual(out["libraries"], [])

    def test_install_failure_surfaces_in_failing_excerpt(self):
        out = fb.compile_sketch(
            _OLED_SKETCH,
            runner=self._runner([], install_proc=_fail_proc("offline"),
                                compile_proc=_fail_proc()))
        self.assertFalse(out["ok"])
        self.assertIn("offline", out["log_excerpt"])
        self.assertIn("missing header", out["log_excerpt"])

    def test_bare_sketch_makes_no_lib_calls(self):
        calls = []
        fb.compile_sketch("void setup(){} void loop(){}",
                          runner=self._runner(calls))
        self.assertEqual(len(calls), 1)
        self.assertIn("compile", calls[0])

    def test_auto_libs_env_disables(self):
        calls = []
        with mock.patch.dict(os.environ, {"ARDUINO_AUTO_LIBS": "0"}):
            fb.compile_sketch(_OLED_SKETCH, runner=self._runner(calls))
        self.assertEqual(len(calls), 1)
        self.assertIn("compile", calls[0])

    def test_excerpt_strips_ansi_codes(self):
        proc = SimpleNamespace(returncode=1, stdout="",
                               stderr="\x1b[92mUsed library\x1b[0m\n"
                                      "\x1b[91merror: boom\x1b[0m")
        out = fb.compile_sketch("void setup(){} void loop(){}",
                                runner=lambda *a, **k: proc)
        self.assertNotIn("\x1b", out["log_excerpt"])
        self.assertIn("Used library", out["log_excerpt"])
        self.assertIn("boom", out["log_excerpt"])


class CompileStreamTest(unittest.TestCase):
    """Streaming compile yields log lines, then the same record shape."""

    def test_lines_then_ok_result(self):
        from backend.tests.test_firmware_flash import FakePopen

        code = "void setup() {}\nvoid loop() {}\n"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"FIRMWARE_BUILD_DIR": tmp}):
            outdir = fb.build_dir_for(fb.code_hash(code))
            os.makedirs(outdir, exist_ok=True)
            with open(os.path.join(outdir, "step_sketch.ino.bin"),
                      "wb") as fh:
                fh.write(b"\x01" * 16)
            fake = FakePopen(["Sketch uses 1 bytes\n"], 0)

            def boom(*args, **kwargs):
                raise AssertionError("no libraries needed here")

            events = list(fb.compile_sketch_stream(
                code, runner=boom, popen=fake))
        kinds = [e["event"] for e in events]
        self.assertIn("log", kinds)
        self.assertEqual(kinds[-1], "result")
        self.assertTrue(events[-1]["build"]["ok"])
        self.assertIn("compile", fake.cmd)

    def test_failure_result(self):
        from backend.tests.test_firmware_flash import FakePopen

        code = "void setup() {}\nvoid loop() {}\n"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"FIRMWARE_BUILD_DIR": tmp}):
            fake = FakePopen(["error: boom\n"], 2)
            events = list(fb.compile_sketch_stream(
                code, runner=lambda *a, **k: None, popen=fake))
        self.assertFalse(events[-1]["build"]["ok"])
        self.assertIn("boom", events[-1]["build"]["log_excerpt"])

    def test_cached_build_streams_no_subprocess(self):
        from backend.tests.test_firmware_flash import FakePopen

        code = "void setup() {}\nvoid loop() {}\n"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"FIRMWARE_BUILD_DIR": tmp}):
            outdir = fb.build_dir_for(fb.code_hash(code))
            os.makedirs(outdir, exist_ok=True)
            main = os.path.join(outdir, "step_sketch.ino.bin")
            with open(main, "wb") as fh:
                fh.write(b"\x01" * 16)
            with open(os.path.join(outdir, "build.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"build_hash": fb.code_hash(code),
                           "artifacts": [{"offset": "0x10000",
                                          "file": main}]}, fh)
            fake = FakePopen(["must not run\n"], 0)
            events = list(fb.compile_sketch_stream(
                code, runner=lambda *a, **k: None, popen=fake))
        self.assertIsNone(fake.cmd)
        self.assertTrue(events[-1]["build"]["cached"])


if __name__ == "__main__":
    unittest.main()
