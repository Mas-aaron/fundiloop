"""Fundi chapters: validated narration, events only on sensor evidence.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_fundi -v
"""
import json
import tempfile
import unittest
from unittest import mock

from backend.app.fundi import (
    plan_chapters,
    speak_passed,
    speak_start,
    template_chapters,
    validate_chapters,
)
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
        self.orc.Muse = StubBrain(script)
        return self.orc.start_learning_session(
            "blink", [dict(s) for s in plan], visible_parts=[],
            session_id=sid, project_title="Blink", board="arduino_uno")

    def test_start_emits_chapter_start(self):
        v = self.start(PLAN, [json.dumps({"chapters": CHAPTERS})])
        self.assertEqual(v["fundi"]["event"], "CHAPTER_START")
        self.assertEqual(v["fundi"]["chapter"]["title"], "Meet the sensor")
        self.assertIn("two shared wires", v["speak"])
        self.assertNotIn("Chapter", v["speak"])
        self.assertEqual(v["step_index"], 0)

    def test_pass_emits_chapter_passed_with_next(self):
        self.start(PLAN, [json.dumps({"chapters": CHAPTERS})])
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

    def test_fail_emits_chapter_hint_without_advancing(self):
        self.start(PLAN, [json.dumps({"chapters": CHAPTERS}),
                          "Reseat the SDA wire."])
        seen = []
        with mock.patch.object(self.orc.probe, "read_all",
                               return_value=I2C_EMPTY):
            v = self.orc.check_now(CheckNowRequest(),
                                   progress=lambda s, d: seen.append(s))
        self.assertEqual(v["fundi"]["event"], "CHAPTER_HINT")
        self.assertEqual(v["fundi"]["chapter"]["n"], 1)
        self.assertEqual(v["fundi"]["hint"], "Reseat the SDA wire.")
        self.assertIn("CHAPTER_HINT", seen)
        self.assertEqual(v["step_index"], 0)
        self.assertNotIn("celebration", v["fundi"])  # cheer only on passes

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


if __name__ == "__main__":
    unittest.main()
