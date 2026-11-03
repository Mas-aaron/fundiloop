"""Project firmware: cached, guarded, telemetry-contracted.

generate_code authors the build's .ino model-first, cached per
evidence state; JSON stubs and silent firmware fail loud (502,
retryable) instead of caching poison the monitor could never
verify. No weights, no network, no model calls in these tests.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_project_code -v
"""
import os
import tempfile
import time
import unittest

from backend.app.adapters import MetaAPIError
from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.adapters.sam_adapter import MetaSAMAdapter
from backend.app.adapters.voice_adapter import MetaVoiceAdapter
from backend.app.agent_orchestrator import AgentOrchestrator
from backend.app.boards import pin_map_for
from backend.app.change_detector import MotionSettler
from backend.app.circuit_graph import load_circuit_graph
from backend.app.learning import generate_code
from backend.app.serial_probe import SerialProbe
from backend.tests.helpers import make_camera, use_mock_env
from backend.tests.test_fundi import StubBrain


# Talking firmware: the telemetry gate rejects model replies without
# Serial output, so every scripted "good" reply opens Serial and prints.
LIVE_FIRMWARE = ("// live firmware\n"
                 "void setup() { Serial.begin(115200); }\n"
                 "void loop() { Serial.println(\"STATUS ok\"); }")
V1_FIRMWARE = ("// v1\nvoid setup() { Serial.begin(115200); }\n"
               "void loop() { Serial.println(1); }")
V2_FIRMWARE = ("// v2\nvoid setup() { Serial.begin(115200); }\n"
               "void loop() { Serial.println(2); }")
SERVO_SKETCH = ("#include <Servo.h>\n"
                "Servo s;\n"
                "void setup() { Serial.begin(115200); s.attach(13); }\n"
                "void loop() { s.write(90); "
                "Serial.println(\"STATUS ok\"); }")


def make_live_session():
    return {"project_title": "Blink", "project_id": "blink",
            "board": "esp32_devkit",
            "pin_map": pin_map_for("esp32_devkit"),
            "plan": [{"n": 1, "instruction": "Blink.",
                      "verify": "CHECK_GPIO", "expect": {},
                      "depends_on_parts": ["led"]}],
            "visible_parts": ["esp32", "led"],
            "verified": {"i2c": [], "steps": []}}


class RecordingBrain:
    """StubBrain that also keeps the system prompts (prompt-shape tests)."""

    mock = False

    def __init__(self, script):
        self.script = list(script)
        self.systems = []
        self.users = []
        self.kwarg_list = []

    def complete_text(self, system, user, **kwargs):
        self.systems.append(system)
        self.users.append(user)
        self.kwarg_list.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ProjectCodeCacheTest(unittest.TestCase):
    def test_refetch_returns_cached_firmware(self):
        session = make_live_session()
        brain = StubBrain([LIVE_FIRMWARE])
        first = generate_code(session, brain)
        second = generate_code(session, brain)
        self.assertEqual(first, LIVE_FIRMWARE)
        self.assertEqual(second, LIVE_FIRMWARE)
        self.assertEqual(len(brain.calls), 1)

    def test_new_evidence_invalidates_cache(self):
        session = make_live_session()
        brain = StubBrain([V1_FIRMWARE, V2_FIRMWARE])
        self.assertEqual(generate_code(session, brain), V1_FIRMWARE)
        session["verified"]["steps"].append(
            {"n": 1, "test": "CHECK_GPIO", "passed": True,
             "measured": "GPIO2 toggles"})
        self.assertEqual(generate_code(session, brain), V2_FIRMWARE)
        self.assertEqual(len(brain.calls), 2)


class FirmwareRetryTest(unittest.TestCase):
    def test_project_code_retries_once_then_succeeds(self):
        brain = StubBrain([MetaAPIError("blip"), LIVE_FIRMWARE])
        code = generate_code(make_live_session(), brain)
        self.assertEqual(code, LIVE_FIRMWARE)
        self.assertEqual(len(brain.calls), 2)

    def test_project_code_second_failure_raises(self):
        brain = StubBrain([MetaAPIError("down"), MetaAPIError("still down")])
        with self.assertRaises(MetaAPIError):
            generate_code(make_live_session(), brain)
        self.assertEqual(len(brain.calls), 2)

    def test_project_code_non_api_error_does_not_retry(self):
        brain = StubBrain([ValueError("bad prompt")])
        with self.assertRaises(ValueError):
            generate_code(make_live_session(), brain)
        self.assertEqual(len(brain.calls), 1)


class FirmwareGuardTest(unittest.TestCase):
    """Model firmware that is not code must never reach the compiler.

    The model sometimes answers a firmware prompt with a JSON stub
    ({"code": "placeholder"}) instead of a sketch; without a guard
    the stub would be cached verbatim and compiled.
    """

    def test_project_code_rejects_json_stub(self):
        session = make_live_session()
        brain = StubBrain(['{"code": "placeholder"}',
                           '{"code": "placeholder"}'])
        with self.assertRaises(MetaAPIError):
            generate_code(session, brain)
        # Nothing cached: the retry burns a fresh model call, not poison.
        self.assertEqual(session.get("project_code"), {})
        brain.script.append(LIVE_FIRMWARE)
        self.assertEqual(generate_code(session, brain), LIVE_FIRMWARE)

    def test_project_code_retries_once_after_stub(self):
        session = make_live_session()
        brain = StubBrain(['{"reasoning": "thinking out loud"}',
                           LIVE_FIRMWARE])
        self.assertEqual(generate_code(session, brain), LIVE_FIRMWARE)
        self.assertEqual(len(brain.calls), 2)

    def test_codegen_disables_json_mode(self):
        brain = RecordingBrain(['{"code": "nope"}', LIVE_FIRMWARE])
        generate_code(make_live_session(), brain)
        self.assertEqual(len(brain.kwarg_list), 2)
        for kwargs in brain.kwarg_list:
            self.assertIs(kwargs.get("json_mode"), False)

    def test_codegen_prompt_marks_context_json_vs_reply_code(self):
        session = make_live_session()
        brain = RecordingBrain(['{"NOT ALLOWED": "conflict"}',
                                LIVE_FIRMWARE])
        self.assertEqual(generate_code(session, brain), LIVE_FIRMWARE)
        self.assertIn("input context", brain.systems[0])
        self.assertIn("Begin with a comment line", brain.systems[1])
        retry_suffix = brain.systems[1][len(brain.systems[0]):]
        self.assertNotIn("JSON", retry_suffix)
        self.assertIn("Reply with the complete .ino sketch only",
                      brain.users[0])

    def test_servo_include_rewritten_to_esp32(self):
        session = make_live_session()
        brain = RecordingBrain([SERVO_SKETCH])
        code = generate_code(session, brain)
        self.assertIn("#include <ESP32Servo.h>", code)
        self.assertNotIn("<Servo.h>", code)
        self.assertEqual(generate_code(session, brain), code)
        self.assertEqual(len(brain.systems), 1)

    def test_legacy_cached_servo_spelling_heals_on_read(self):
        from backend.app.learning import _project_code_key

        session = make_live_session()
        session.setdefault("project_code", {})[
            _project_code_key(session)] = SERVO_SKETCH
        brain = RecordingBrain([])
        code = generate_code(session, brain)
        self.assertIn("#include <ESP32Servo.h>", code)
        self.assertNotIn("<Servo.h>", code)

    def test_project_code_extracts_embedded_sketch(self):
        session = make_live_session()
        sketch = ("void setup(){Serial.begin(115200);} "
                  "void loop(){Serial.println(1);}")
        brain = StubBrain(['{"code": "%s"}' % sketch])
        self.assertEqual(generate_code(session, brain), sketch)
        self.assertEqual(len(brain.calls), 1)


class FirmwareTelemetryTest(unittest.TestCase):
    """Firmware without Serial output can never prove the build.

    The gate demands telemetry (Serial.begin + a print): silent
    firmware gets one explicit retry, then fails loud so the
    monitor is never stranded waiting on a mute board.
    """

    def test_silent_firmware_retries_then_raises(self):
        session = make_live_session()
        silent = "void setup(){} void loop(){}"
        brain = StubBrain([silent, silent])
        with self.assertRaises(MetaAPIError) as ctx:
            generate_code(session, brain)
        self.assertIn("without telemetry", str(ctx.exception))
        self.assertEqual(len(brain.calls), 2)
        self.assertEqual(session.get("project_code"), {})

    def test_telemetry_retry_demands_serial_output(self):
        session = make_live_session()
        brain = StubBrain(["void setup(){} void loop(){}", LIVE_FIRMWARE])
        self.assertEqual(generate_code(session, brain), LIVE_FIRMWARE)
        self.assertEqual(len(brain.calls), 2)

    def test_codegen_uses_extended_timeout(self):
        from backend.app.learning import CODEGEN_TIMEOUT_S
        session = make_live_session()
        seen = {}

        class TimeoutBrain(StubBrain):
            def complete_text(self, system, user, **kwargs):
                seen.update(kwargs)
                return super().complete_text(system, user, **kwargs)

        brain = TimeoutBrain([LIVE_FIRMWARE])
        self.assertEqual(generate_code(session, brain), LIVE_FIRMWARE)
        self.assertEqual(seen.get("timeout_s"), CODEGEN_TIMEOUT_S)

    def test_unusable_firmware_logs_rejected_text(self):
        session = make_live_session()
        brain = StubBrain(['{"code": "nope"}', '{"foo": 1}'])
        with self.assertLogs("backend.app.learning", "WARNING") as logs:
            with self.assertRaises(MetaAPIError):
                generate_code(session, brain)
        logged = "\n".join(logs.output)
        self.assertIn("rejected firmware", logged)
        self.assertIn('{"code": "nope"}', logged)


class FirmwareWarmupTest(unittest.TestCase):
    """Firmware generates in the background from session start.

    The cache is warm (checked, telemetry-contracted) before the
    learner opens the Code tab or deploys; every step advance
    rewarms for the new evidence key. Mock brains: instant and
    deterministic, no model calls.
    """

    def _orc(self):
        use_mock_env(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return AgentOrchestrator(
            graph=load_circuit_graph(),
            camera=make_camera(tmp.name),
            settler=MotionSettler(settle_seconds=1.0),
            probe=SerialProbe(mock=True),
            sam=MetaSAMAdapter(mock=True),
            Muse=MetaClaudeAdapter(mock=True),
            voice=MetaVoiceAdapter(mock=True),
            attempt_log_path=os.path.join(tmp.name, "attempts.jsonl"))

    def _plan(self):
        return [
            {"n": 1, "instruction": "Wire the sensor.",
             "verify": "RUN_I2C_SCAN", "expect": {},
             "depends_on_parts": ["bme280", "esp32", "breadboard"]},
            {"n": 2, "instruction": "Add the LED.", "verify": "CHECK_GPIO",
             "expect": {}, "depends_on_parts": ["led", "resistor"]},
        ]

    def _await_entries(self, session, count, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            cache = session.get("project_code") or {}
            if len(cache) >= count:
                return cache
            time.sleep(0.05)
        self.fail("firmware never warmed %d entries" % count)

    def test_start_warms_firmware_cache(self):
        orc = self._orc()
        orc.start_learning_session(
            project_id="weather", plan=self._plan(),
            visible_parts=["esp32", "bme280", "led", "breadboard"],
            project_title="Weather")
        cache = self._await_entries(orc.session, 1)
        code = next(iter(cache.values()))
        self.assertIn("Serial.begin(", code)
        self.assertIn("SYS | HEARTBEAT | OK | -", code)

    def test_advance_rewarms_for_new_evidence(self):
        orc = self._orc()
        view = orc.start_learning_session(
            project_id="weather", plan=self._plan(),
            visible_parts=["esp32", "bme280", "led", "breadboard"],
            project_title="Weather")
        sid = view["id"]
        self._await_entries(orc.session, 1)
        orc.answer_rollcall(sid, "ready")
        orc.advance_learning_step(sid, "done")
        cache = self._await_entries(orc.session, 2)
        for code in cache.values():
            self.assertIn("Serial.begin(", code)


if __name__ == "__main__":
    unittest.main()
