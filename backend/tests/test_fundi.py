"""Fundi chapters: validated narration, events only on sensor evidence.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_fundi -v
"""
import json
import tempfile
import unittest
from unittest import mock

from backend.app.boards import pin_map_for
from backend.app.fundi import (
    plan_chapters,
    speak_passed,
    speak_start,
    template_chapters,
    validate_chapters,
)
from backend.app.learning import build_hint, run_learning_verification
from backend.app.models import CheckNowRequest, ProbeReading
from backend.tests.helpers import use_mock_env
from backend.tests.test_latency import make_orchestrator

PLAN = [
    {"n": 1, "instruction": "Wire the sensor SDA SCL and power.",
     "verify": "RUN_I2C_SCAN", "expect": {"addresses": ["0x76"]},
     "depends_on_parts": []},
    {"n": 2, "instruction": "Blink the builtin LED.", "verify": "CHECK_GPIO",
     "expect": {}, "depends_on_parts": []},
]

CHAPTERS = [
    {"n": 1, "title": "Meet the sensor",
     "teach": "The sensor talks to the board over two shared wires.",
     "task": "Wire SDA, SCL, and power to the sensor.",
     "verify": "RUN_I2C_SCAN", "celebration": "Boom!"},
    {"n": 2, "title": "Blink the light",
     "teach": "GPIO pins switch on and off in code.",
     "task": "Blink the builtin LED.", "verify": "CHECK_GPIO",
     "celebration": "Spot on!"},
]

I2C_OK = ProbeReading(connected=True, i2c_devices=["0x76"],
                      temperature_c=22.0, humidity_pct=50.0,
                      pressure_hpa=1010.0, oled_ok=None, led_ok=None,
                      mock=False, raw="{}")
I2C_EMPTY = I2C_OK.model_copy(update={"i2c_devices": []})
GPIO_UNKNOWN = ProbeReading(
    connected=True, i2c_devices=[], temperature_c=None, humidity_pct=None,
    pressure_hpa=None, oled_ok=None, led_ok=None, mock=False, raw="{}")


class StubBrain:
    mock = False

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete_text(self, system, user, **kwargs):
        self.calls.append(user)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ValidateChaptersTest(unittest.TestCase):
    def test_valid_chapters_pass(self):
        self.assertEqual(validate_chapters(CHAPTERS, PLAN), [])

    def test_count_must_match_plan(self):
        reasons = validate_chapters(CHAPTERS[:1], PLAN)
        self.assertEqual(reasons, ["1 chapters for 2 plan steps (must match 1:1)"])

    def test_n_must_echo_plan(self):
        bad = [dict(CHAPTERS[0], n=7), CHAPTERS[1]]
        self.assertTrue(any("echo" in r for r in validate_chapters(bad, PLAN)))

    def test_verify_must_echo_plan(self):
        bad = [dict(CHAPTERS[0], verify="CHECK_OLED"), CHAPTERS[1]]
        reasons = validate_chapters(bad, PLAN)
        self.assertTrue(any("CHECK_OLED" in r for r in reasons))

    def test_fields_required(self):
        bad = [dict(CHAPTERS[0], teach="  "), CHAPTERS[1]]
        self.assertTrue(any("teach" in r for r in validate_chapters(bad, PLAN)))

    def test_template_echoes_plan_tasks(self):
        out = template_chapters(PLAN)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["task"], PLAN[0]["instruction"])
        self.assertEqual(out[0]["teach"], "")  # teaches nothing, invents nothing


class PlanChaptersTest(unittest.TestCase):
    def test_valid_first_try(self):
        brain = StubBrain([json.dumps({"chapters": CHAPTERS})])
        out = plan_chapters(PLAN, brain, "Blink", "arduino_uno")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["title"], "Meet the sensor")
        self.assertEqual(len(brain.calls), 1)

    def test_retry_after_rejection(self):
        bad = json.dumps({"chapters": CHAPTERS[:1]})
        brain = StubBrain([bad, json.dumps({"chapters": CHAPTERS})])
        out = plan_chapters(PLAN, brain, "Blink")
        self.assertEqual(len(out), 2)
        self.assertEqual(len(brain.calls), 2)
        self.assertIn("1 chapters for 2", brain.calls[1])

    def test_double_reject_falls_back_to_template(self):
        brain = StubBrain(["not json", json.dumps({"chapters": []})])
        out = plan_chapters(PLAN, brain, "Blink")
        self.assertEqual(out[0]["task"], PLAN[0]["instruction"])
        self.assertEqual(out[0]["teach"], "")

    def test_model_error_falls_back_to_template(self):
        brain = StubBrain([RuntimeError("down"), RuntimeError("down")])
        out = plan_chapters(PLAN, brain, "Blink")
        self.assertEqual(len(out), 2)

    def test_mock_brain_uses_template_without_calling(self):
        brain = StubBrain([])
        brain.mock = True
        out = plan_chapters(PLAN, brain, "Blink")
        self.assertEqual(out[1]["verify"], "CHECK_GPIO")
        self.assertEqual(brain.calls, [])


class SpeakBuildersTest(unittest.TestCase):
    def test_start_teaches_first(self):
        text = speak_start(CHAPTERS[0], "Blink")
        self.assertEqual(text, "The sensor talks to the board over two shared "
                               "wires. Wire SDA, SCL, and power to the sensor.")
        self.assertNotIn("Chapter", text)

    def test_start_template_falls_back(self):
        text = speak_start(template_chapters(PLAN)[0], "Blink")
        self.assertEqual(text, "Starting Blink. Step 1: %s" % PLAN[0]["instruction"])

    def test_passed_celebrates_then_opens_next(self):
        text = speak_passed(CHAPTERS[0], "I2C found [0x76]", CHAPTERS[1],
                            "Blink", "Nice work.")
        self.assertEqual(text, "Boom! Verified: I2C found [0x76]. Next: "
                               "GPIO pins switch on and off in code. "
                               "Blink the builtin LED.")

    def test_passed_template_keeps_plain_phrasing(self):
        tmpl = template_chapters(PLAN)
        text = speak_passed(tmpl[0], "I2C found [0x76]", tmpl[1],
                            "Blink", "Nice work.")
        self.assertEqual(text, "Nice work. Verified: I2C found [0x76]. "
                               "Next step 2: Blink the builtin LED.")

    def test_passed_final_has_no_next(self):
        text = speak_passed(CHAPTERS[1], "GPIO13 toggles", None,
                            "Blink", "Nice work.")
        self.assertEqual(text, "Spot on! Verified: GPIO13 toggles.")


class LessonFlowTest(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orc = make_orchestrator(self.tmp.name)

    def start(self, plan, script, sid="fundi-1"):
        # Start answers instantly from templates (zero model calls); the
        # background thread stays suppressed here and the fill step runs
        # explicitly below, so no real timing is involved.
        self.orc.Muse = StubBrain(script)

        class NoThread:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

        with mock.patch("threading.Thread", NoThread):
            return self.orc.start_learning_session(
                "blink", [dict(s) for s in plan], visible_parts=[],
                session_id=sid, project_title="Blink", board="arduino_uno")

    def fill(self, sid="fundi-1"):
        self.orc._fill_chapters(
            sid, self.orc.session, PLAN, "Blink", "arduino_uno",
            Muse=StubBrain([json.dumps({"chapters": CHAPTERS})]))

    def test_start_emits_chapter_start(self):
        v = self.start(PLAN, [])
        self.assertEqual(v["fundi"]["event"], "CHAPTER_START")
        # Templates answer now, with zero model calls on the start path.
        self.assertEqual(v["fundi"]["chapter"]["title"], "Step 1")
        self.assertIn("Starting Blink", v["speak"])
        self.assertNotIn("Chapter", v["speak"])
        self.assertEqual(v["step_index"], 0)
        self.assertEqual(len(self.orc.Muse.calls), 0)
        # Step 1 code rides along from the start: no fail-first.
        self.assertEqual(v["step_code"]["source"], "template")
        self.assertEqual(v["step_code"]["n"], 1)
        self.assertIn("scan done", v["step_code"]["code"])
        # ...then the background fill swaps the full chapters in.
        self.fill()
        full = self.orc.session["chapters"]
        self.assertEqual(full[0]["title"], "Meet the sensor")
        self.assertIn("two shared wires", full[0]["teach"])

    def test_pass_emits_chapter_passed_with_next(self):
        self.start(PLAN, [json.dumps({"chapters": CHAPTERS[:1]})])
        self.fill()
        seen = []
        with mock.patch.object(self.orc.probe, "read_all",
                               return_value=I2C_OK):
            v = self.orc.check_now(CheckNowRequest(),
                                   progress=lambda s, d: seen.append(s))
        self.assertEqual(v["fundi"]["event"], "CHAPTER_PASSED")
        self.assertEqual(v["fundi"]["chapter"]["n"], 1)
        self.assertEqual(v["fundi"]["next_chapter"]["n"], 2)
        self.assertEqual(v["fundi"]["celebration"], "Boom!")
        self.assertIn("CHAPTER_PASSED", seen)
        self.assertIn("Boom! Verified:", v["speak"])
        self.assertIn("Next: GPIO pins switch on and off in code.", v["speak"])
        self.assertEqual(v["step_index"], 1)
        # Passing hands over the NEXT step's code at once.
        self.assertEqual(v["step_code"]["source"], "template")
        self.assertEqual(v["step_code"]["n"], 2)
        self.assertIn("LED_PIN", v["step_code"]["code"])

    def test_fail_emits_chapter_hint_without_advancing(self):
        self.start(PLAN, [])
        self.fill()
        seen = []
        with mock.patch.object(self.orc.probe, "read_all",
                               return_value=I2C_EMPTY):
            v = self.orc.check_now(CheckNowRequest(),
                                   progress=lambda s, d: seen.append(s))
        self.assertEqual(v["fundi"]["event"], "CHAPTER_HINT")
        self.assertEqual(v["fundi"]["chapter"]["n"], 1)
        # Classified failure: the instant template answers, no model call.
        self.assertIn("SDA and SCL", v["fundi"]["hint"])
        self.assertEqual(self.orc.Muse.calls, [])
        self.assertIn("CHAPTER_HINT", seen)
        self.assertEqual(v["step_index"], 0)
        self.assertNotIn("celebration", v["fundi"])  # cheer only on passes
        # Classified failure: speech still carries measured plus the hint.
        self.assertIn("I2C found", v["speak"])
        self.assertIn("SDA and SCL", v["speak"])
        # ...and the step's runnable code is attached, not just described.
        self.assertEqual(v["step_code"]["source"], "template")
        self.assertEqual(v["step_code"]["n"], 1)
        self.assertIn("scan done", v["step_code"]["code"])

    def test_silent_serial_fail_speaks_hint_and_attaches_code(self):
        plan = [{"n": 1, "instruction": "Wire the serial link.",
                 "verify": "CHECK_SERIAL", "expect": {},
                 "depends_on_parts": []}]
        self.start(plan, [])
        silent = ProbeReading(
            connected=False, i2c_devices=[], temperature_c=None,
            humidity_pct=None, pressure_hpa=None, oled_ok=None, led_ok=None,
            mock=False, raw=json.dumps({"ping": {
                "cmd": "PING", "error": "no PING reply on COM3, COM9",
                "board_state": "silent:COM9"}}))
        with mock.patch.object(self.orc.probe, "read_all",
                               return_value=silent):
            v = self.orc.check_now(CheckNowRequest())
        self.assertEqual(v["fundi"]["event"], "CHAPTER_HINT")
        self.assertEqual(v["fundi"]["hint"],
                         "The board on COM9 never answers. Flash the patient "
                         "sketch, then press CHECK NOW.")
        # Known failure: the port dump stays on screen, speech is guidance.
        self.assertEqual(v["speak"], v["fundi"]["hint"])
        self.assertEqual(v["instruction"], v["speak"])
        self.assertNotIn("no live serial traffic", v["speak"])
        self.assertEqual(v["step_code"]["source"], "patient")
        self.assertIn("PING", v["step_code"]["code"])
        # Template chapters + instant hint: no model calls at all.
        self.assertEqual(len(self.orc.Muse.calls), 0)

    def test_unverifiable_asks_then_confirms_to_done(self):
        one = [dict(PLAN[1])]
        one_ch = [dict(c) for c in CHAPTERS[1:]]
        self.start(one, [json.dumps({"chapters": one_ch})])
        with mock.patch.object(self.orc.probe, "read_all",
                               return_value=GPIO_UNKNOWN):
            v = self.orc.check_now(CheckNowRequest())
        self.assertEqual(v["fundi"]["event"], "CHAPTER_HINT")
        self.assertIn("CONFIRM", v["confirm_pending"]["question"])
        v = self.orc.confirm_learning_step("fundi-1", True)
        self.assertEqual(v["fundi"]["event"], "LESSON_DONE")
        self.assertTrue(v["code_ready"])

    def test_final_pass_emits_lesson_done(self):
        one = [dict(PLAN[0])]
        self.start(one, [json.dumps({"chapters": [CHAPTERS[0]]})])
        with mock.patch.object(self.orc.probe, "read_all",
                               return_value=I2C_OK):
            v = self.orc.check_now(CheckNowRequest())
        self.assertEqual(v["state"], "DONE")
        self.assertEqual(v["fundi"]["event"], "LESSON_DONE")
        self.assertEqual(v["fundi"]["chapters_total"], 1)
        self.assertIn("Boom", v["speak"])


class BuildHintGuardTest(unittest.TestCase):
    STEP = {"n": 1, "instruction": "wire it"}
    VER = {"test": "CHECK_SERIAL",
           "measured": "no live serial traffic (board silent)"}

    def test_refusal_blob_falls_back(self):
        blob = '{"refusal": "Loud refusal triggered:\\n[dev]: be brief"}'
        hint = build_hint(self.STEP, self.VER, StubBrain([blob]))
        self.assertIn("Check the wiring for step 1", hint)
        self.assertNotIn("refusal", hint)
        # The caller already speaks measured; the hint must not repeat it.
        self.assertNotIn("no live serial traffic", hint)

    def test_prose_refusal_falls_back(self):
        hint = build_hint(self.STEP, self.VER,
                          StubBrain(["Loud refusal triggered: caps conflict"]))
        self.assertIn("Check the wiring for step 1", hint)

    def test_good_hint_passes_through_caps_and_all(self):
        hint = build_hint(self.STEP, self.VER,
                          StubBrain(["Reseat the SDA wire."]))
        self.assertEqual(hint, "Reseat the SDA wire.")

    def test_model_error_falls_back(self):
        hint = build_hint(self.STEP, self.VER,
                          StubBrain([RuntimeError("boom")]))
        self.assertIn("Check the wiring for step 1", hint)
        self.assertNotIn("no live serial traffic", hint)

    def test_known_board_states_skip_model(self):
        for state, needle in (
            ("silent:COM11", "COM11"),
            ("unreachable:COM9", "COM9"),
            ("no_usb", "USB"),
            ("no_ports", "USB"),
        ):
            brain = StubBrain(["model hint"])
            hint = build_hint(self.STEP, dict(self.VER, board_state=state),
                              brain)
            self.assertEqual(brain.calls, [], state)
            self.assertIn(needle, hint, state)
            self.assertIn("CHECK NOW", hint, state)

    def test_classified_sensor_failures_skip_model(self):
        cases = (
            ({"test": "RUN_I2C_SCAN",
              "measured": "I2C found [0x3C] MISSING 0x76"}, "SDA"),
            ({"test": "READ_SENSOR",
              "measured": "sensor read failed"}, "power"),
        )
        for ver, needle in cases:
            brain = StubBrain(["model hint"])
            hint = build_hint(self.STEP, ver, brain)
            self.assertEqual(brain.calls, [], ver)
            self.assertIn(needle, hint, ver)
            self.assertIn("CHECK NOW", hint, ver)


class SerialMeasuredTruncationTest(unittest.TestCase):
    def _check(self, error):
        reading = ProbeReading(
            connected=False, i2c_devices=[], temperature_c=None,
            humidity_pct=None, pressure_hpa=None, oled_ok=None, led_ok=None,
            mock=False, raw=json.dumps({"ping": {"cmd": "PING", "error": error}}))
        return run_learning_verification(
            "CHECK_SERIAL", {}, reading, pin_map_for("esp32_devkit"))

    def test_long_error_truncates_at_word_boundary(self):
        v = self._check("COM3: could not open port 'COM3': "
                        "FileNotFoundError(2, 'The system cannot find the "
                        "file specified.', None, 2) and then some more text")
        self.assertFalse(v["passed"])
        self.assertTrue(v["measured"].endswith("…)"), v["measured"])
        self.assertLessEqual(len(v["measured"]), len(
            "no live serial traffic ()") + 120)

    def test_short_error_passes_through_whole(self):
        v = self._check("board silent")
        self.assertEqual(v["measured"],
                         "no live serial traffic (board silent)")
        self.assertEqual(v["board_state"], "")

    def test_board_state_carried_from_ping(self):
        reading = ProbeReading(
            connected=False, i2c_devices=[], temperature_c=None,
            humidity_pct=None, pressure_hpa=None, oled_ok=None, led_ok=None,
            mock=False, raw=json.dumps({"ping": {"cmd": "PING",
                                                "error": "no PING reply",
                                                "board_state": "silent:COM11"}}))
        v = run_learning_verification(
            "CHECK_SERIAL", {}, reading, pin_map_for("esp32_devkit"))
        self.assertEqual(v["board_state"], "silent:COM11")


if __name__ == "__main__":
    unittest.main()
