"""Project firmware: cached, guarded, telemetry-contracted.

generate_code authors the build's .ino model-first, cached per
evidence state; JSON stubs and silent firmware fail loud (502,
retryable) instead of caching poison the monitor could never
verify. No weights, no network, no model calls in these tests.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_project_code -v
"""
import unittest

from backend.app.adapters import MetaAPIError
from backend.app.boards import pin_map_for
from backend.app.learning import generate_code
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


def make_live_session():
    return {"project_title": "Blink", "project_id": "blink",
            "board": "esp32_devkit",
            "pin_map": pin_map_for("esp32_devkit"),
            "plan": [{"n": 1, "instruction": "Blink.",
                      "verify": "CHECK_GPIO", "expect": {},
                      "depends_on_parts": ["led"]}],
            "visible_parts": ["esp32", "led"],
            "verified": {"i2c": [], "steps": []}}


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


if __name__ == "__main__":
    unittest.main()
