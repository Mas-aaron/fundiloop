"""Fundi chapters: validated narration, tap-paced lesson flow.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_fundi -v
"""
import json
import tempfile
import unittest
from unittest import mock

from backend.app.boards import pin_map_for
from backend.app.fundi import (
    complete_beat,
    complete_rollcall,
    next_step_opening,
    plan_chapters,
    plan_stretch,
    rollcall_accepts,
    speak_start,
    template_chapters,
    validate_chapters,
)
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
     "why": "If the sensor answers here, every later step can trust it.",
     "task": "Wire SDA, SCL, and power to the sensor.",
     "verify": "RUN_I2C_SCAN", "celebration": "Boom!"},
    {"n": 2, "title": "Blink the light",
     "teach": "GPIO pins switch on and off in code.",
     "why": "One blinking pin proves your code reaches the real world.",
     "task": "Blink the builtin LED.", "verify": "CHECK_GPIO",
     "celebration": "Spot on!"},
]

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
                               "wires. If the sensor answers here, every later "
                               "step can trust it. Wire SDA, SCL, and power "
                               "to the sensor.")
        self.assertNotIn("Chapter", text)

    def test_start_without_why_keeps_old_shape(self):
        text = speak_start({"n": 1, "teach": "T.", "task": "K."}, "Blink")
        self.assertEqual(text, "T. K.")

    def test_start_template_falls_back(self):
        ch = template_chapters(PLAN)[0]
        text = speak_start(ch, "Blink")
        # One step announcement, no repeated project title: the UI
        # opening line already names the project. The task leads so the
        # learner knows what to do; the derived why-line trails so
        # fallback mode still teaches.
        self.assertTrue(
            text.startswith("Step 1: " + PLAN[0]["instruction"]))
        self.assertIn(ch["why"], text)
        self.assertLess(text.index(PLAN[0]["instruction"]),
                        text.index(ch["why"]))

    def test_template_why_is_plain_language(self):
        tmpl = template_chapters(PLAN)
        # Fallback principle lines teach in plain words: no proverbs
        # or riddles ahead of the action.
        self.assertEqual(
            tmpl[0]["why"],
            "Each address that answers proves its wiring before any "
            "reading is trusted.")
        self.assertEqual(
            tmpl[1]["why"],
            "This check proves each wire really reaches its GPIO "
            "before the build trusts it.")
        other = template_chapters(
            [{"n": 9, "instruction": "Do the thing.", "verify": "NOPE"}])
        self.assertEqual(
            other[0]["why"],
            "Measure first, then build on what the measurement proved.")

    def test_next_step_opening_leads_with_task(self):
        tmpl = template_chapters(PLAN)
        self.assertEqual(
            next_step_opening(tmpl[1]),
            "Next step 2: Blink the builtin LED. This check proves "
            "each wire really reaches its GPIO before the build "
            "trusts it.")

class LessonFlowTest(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orc = make_orchestrator(self.tmp.name)

    def start(self, plan, script, sid="fundi-1", ready=True):
        # Start answers instantly from templates (zero model calls); the
        # background thread stays suppressed here and the fill step runs
        # explicitly below, so no real timing is involved. The parts
        # roll call gates chapter 1: ready=True answers it, tests for
        # the gate itself pass ready=False.
        self.orc.Muse = StubBrain(script)

        class NoThread:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

        with mock.patch("threading.Thread", NoThread):
            view = self.orc.start_learning_session(
                "blink", [dict(s) for s in plan], visible_parts=[],
                session_id=sid, project_title="Blink", board="arduino_uno")
        if ready:
            view = self.orc.answer_rollcall(sid, "ready")
        return view

    def fill(self, sid="fundi-1"):
        self.orc._fill_chapters(
            sid, self.orc.session, PLAN, "Blink", "arduino_uno",
            Muse=StubBrain([json.dumps({"chapters": CHAPTERS})]))

    def test_start_emits_rollcall_then_chapter_start(self):
        v = self.start(PLAN, [], ready=False)
        self.assertEqual(v["state"], "ROLLCALL")
        self.assertEqual(v["fundi"]["event"], "ROLLCALL")
        self.assertIn("READY", v["speak"])
        self.assertTrue(v["fundi"]["rollcall"]["question"].strip())
        self.assertEqual(v["step_index"], 0)
        self.assertEqual(len(self.orc.Muse.calls), 0)
        # READY begins chapter 1: templates answer now, with zero model
        # calls on the path.
        answered = self.orc.answer_rollcall("fundi-1", "ready")
        self.assertEqual(answered["fundi"]["event"], "CHAPTER_START")
        self.assertEqual(answered["fundi"]["chapter"]["title"], "Step 1")
        self.assertTrue(answered["speak"].startswith("Step 1: "))
        self.assertNotIn("Chapter", answered["speak"])
        self.assertEqual(answered["step_index"], 0)
        self.assertEqual(len(self.orc.Muse.calls), 0)
        # ...then the background fill swaps the full chapters in.
        self.fill()
        full = self.orc.session["chapters"]
        self.assertEqual(full[0]["title"], "Meet the sensor")
        self.assertIn("two shared wires", full[0]["teach"])

    def test_rollcall_names_missing_parts(self):
        plan = [{"n": 1, "instruction": "Swing the servo.",
                 "verify": "CHECK_GPIO", "expect": {},
                 "depends_on_parts": ["servo"]}]
        v = self.start(plan, [], sid="roll-1", ready=False)
        self.assertIn("servo", v["speak"])

    def test_hold_reprompts_without_advancing(self):
        self.start(PLAN, [], ready=False)
        v = self.orc.answer_rollcall("fundi-1", "wait")
        self.assertEqual(v["state"], "ROLLCALL")
        self.assertEqual(v["step_index"], 0)
        self.assertIn("READY", v["speak"])

    def test_advance_gated_until_ready(self):
        self.start(PLAN, [], ready=False)
        v = self.orc.advance_learning_step("fundi-1", "next")
        self.assertEqual(v["state"], "ROLLCALL")
        self.assertEqual(v["step_index"], 0)

    def test_ready_unknown_session_raises(self):
        self.start(PLAN, [], ready=False)
        with self.assertRaises(KeyError):
            self.orc.answer_rollcall("nope", "ready")

    def test_double_ready_reorients_without_error(self):
        self.start(PLAN, [])
        v = self.orc.answer_rollcall("fundi-1", "ready")
        self.assertEqual(v["fundi"]["event"], "CHAPTER_START")
        self.assertEqual(v["step_index"], 0)

    def test_advance_emits_chapter_passed_with_next(self):
        self.start(PLAN, [])
        self.fill()
        v = self.orc.advance_learning_step("fundi-1", "done")
        self.assertEqual(v["fundi"]["event"], "CHAPTER_PASSED")
        self.assertEqual(v["fundi"]["chapter"]["n"], 1)
        self.assertEqual(v["fundi"]["next_chapter"]["n"], 2)
        self.assertEqual(v["fundi"]["celebration"], "Boom!")
        self.assertIn("Nice work. Next: GPIO pins switch on and off in "
                      "code. One blinking pin proves your code reaches the "
                      "real world. Blink the builtin LED.", v["speak"])
        self.assertEqual(v["step_index"], 1)

    def test_escape_finishes_final_step_to_lesson_done(self):
        one = [dict(PLAN[0])]
        self.start(one, [])
        v = self.orc.advance_learning_step("fundi-1", "mark complete anyway")
        self.assertEqual(v["state"], "DONE")
        self.assertEqual(v["fundi"]["event"], "LESSON_DONE")
        self.assertEqual(v["fundi"]["chapters_total"], 1)
        self.assertIn("Boom", v["speak"])

    def test_lesson_done_carries_stretch(self):
        one = [dict(PLAN[0])]
        self.start(one, [])
        v = self.orc.advance_learning_step("fundi-1", "mark complete anyway")
        self.assertEqual(v["fundi"]["event"], "LESSON_DONE")
        stretch = v["fundi"]["stretch"]
        self.assertGreaterEqual(len(stretch), 1)
        self.assertLessEqual(len(stretch), 2)
        self.assertIn("more", v["speak"])
        # Advancing past DONE re-serves the same stretch.
        again = self.orc.advance_learning_step("fundi-1", "done")
        self.assertEqual(again["fundi"]["stretch"], stretch)

    def test_stretch_prefetch_upgrades_to_model(self):
        one = [dict(PLAN[0])]
        self.start(one, [])
        give = {"stretch": [{"title": "Model stretch",
                             "summary": "A model-written next step."}]}
        self.orc.Muse = StubBrain([json.dumps(give)])
        self.orc._prefetch_stretch()
        self.assertEqual(self.orc.session["stretch"], give["stretch"])
        v = self.orc.advance_learning_step("fundi-1", "mark complete anyway")
        self.assertEqual(v["fundi"]["stretch"], give["stretch"])
        self.assertIn("model-written", v["speak"])


class TutorBeatTest(unittest.TestCase):
    """Dynamic tutor beats: every chapter carries a check-in question,
    accepted answers, and a diagnostic question, model-written when
    possible and derived from step data otherwise."""

    def test_template_chapters_carry_complete_beats(self):
        out = template_chapters(PLAN)
        for ch in out:
            self.assertTrue(str(ch["question"] or "").strip(), ch)
            self.assertTrue(ch["answers"], ch)
            self.assertTrue(str(ch["diagnose"] or "").strip(), ch)

    def test_beat_question_targets_step_pins(self):
        plan = [{"n": 1, "instruction": "Wire TRIG to GPIO5 and ECHO to GPIO13.",
                 "verify": "CHECK_GPIO", "expect": {},
                 "depends_on_parts": ["hc-sr04"]}]
        (ch,) = template_chapters(plan)
        self.assertIn("pin", ch["question"].lower())
        self.assertTrue(ch["question"].strip().endswith("?"))
        flat = [kw for alt in ch["answers"] for kw in alt]
        self.assertIn("5", flat)
        self.assertIn("13", flat)

    def test_beat_completed_when_model_omits_fields(self):
        brain = StubBrain([json.dumps({"chapters": CHAPTERS})])
        out = plan_chapters(PLAN, brain, "Blink")
        for ch in out:
            self.assertTrue(str(ch["question"] or "").strip(), ch)
            self.assertTrue(ch["answers"], ch)
            self.assertTrue(str(ch["diagnose"] or "").strip(), ch)

    def test_beat_keeps_model_beats(self):
        give = [dict(CHAPTERS[0], question="Which pin?",
                     answers=[["5"]], diagnose="Quiet pin?"),
                dict(CHAPTERS[1], question="What blinks?",
                     answers=[["led"]], diagnose="Dark LED?")]
        brain = StubBrain([json.dumps({"chapters": give})])
        out = plan_chapters(PLAN, brain, "Blink")
        self.assertEqual(out[0]["question"], "Which pin?")
        self.assertEqual(out[0]["answers"], [["5"]])
        self.assertEqual(out[1]["diagnose"], "Dark LED?")

    def test_diagnose_templates_cover_all_tests(self):
        from backend.app.fundi import diagnose_for

        for test in ("CHECK_GPIO", "CHECK_SERIAL", "RUN_I2C_SCAN",
                     "READ_SENSOR", "CHECK_OLED"):
            self.assertTrue(diagnose_for(test).strip(), test)
        self.assertTrue(diagnose_for("NOPE").strip())

    def test_question_and_diagnose_follow_pin_repair(self):
        from backend.app.schematic import build_netlist, repair_chapters_pins

        pin_map = pin_map_for("esp32_devkit")
        plan = [{"n": 1,
                 "instruction": "Connect the DHT22 DATA pin to GPIO18.",
                 "verify": "CHECK_SERIAL", "depends_on_parts": ["dht22"]}]
        board = next(w["board_pin"] for w in build_netlist(
            plan, 1, pin_map)["wires"]
            if w["part"] == "dht22" and w["part_pin"] == "DATA")
        self.assertNotEqual(board, "GPIO18")  # the test must rewrite
        (fixed,) = repair_chapters_pins(
            [{"n": 1, "title": "T", "teach": "t", "task": "t",
              "verify": "CHECK_SERIAL", "celebration": "",
              "question": "Which pin is DATA on, GPIO18?",
              "answers": [], "diagnose": "Is DATA on GPIO18?"}],
            plan, pin_map)
        self.assertIn(board, fixed["question"])
        self.assertIn(board, fixed["diagnose"])


class ChapterNarrationTest(unittest.TestCase):
    """Every chapter teaches the principle (why) and chains the fix
    (troubleshoot): model-written when possible, derived from step
    data otherwise."""

    def test_templates_carry_why_and_troubleshoot(self):
        out = template_chapters(PLAN)
        self.assertEqual(len(out), 2)
        for ch in out:
            self.assertTrue(str(ch.get("why") or "").strip(), ch)
            tips = ch.get("troubleshoot")
            self.assertIsInstance(tips, list, ch)
            self.assertGreaterEqual(len(tips), 1, ch)
            self.assertTrue(all(str(t).strip() for t in tips), ch)

    def test_narration_differs_by_verify(self):
        out = template_chapters(PLAN)
        self.assertNotEqual(out[0]["why"], out[1]["why"])
        self.assertNotEqual(out[0]["troubleshoot"], out[1]["troubleshoot"])

    def test_troubleshoot_leads_with_diagnose(self):
        out = template_chapters(PLAN)
        self.assertIn("SDA and SCL", out[0]["troubleshoot"][0])

    def test_complete_beat_fills_missing_narration(self):
        beat = {"n": 1, "title": "t", "teach": "x", "task": "y",
                "verify": "RUN_I2C_SCAN", "celebration": ""}
        out = complete_beat(beat, PLAN[0])
        self.assertTrue(str(out["why"]).strip())
        self.assertIsInstance(out["troubleshoot"], list)
        self.assertGreaterEqual(len(out["troubleshoot"]), 1)

    def test_complete_beat_keeps_model_narration(self):
        beat = dict(CHAPTERS[0], troubleshoot=["Model tip."])
        out = complete_beat(beat, PLAN[0])
        self.assertEqual(out["why"], CHAPTERS[0]["why"])
        self.assertEqual(out["troubleshoot"], ["Model tip."])

    def test_complete_beat_repairs_bad_narration_types(self):
        beat = dict(CHAPTERS[0], why=["not", "a", "string"],
                    troubleshoot="a string, not a list")
        out = complete_beat(beat, PLAN[0])
        self.assertIsInstance(out["why"], str)
        self.assertTrue(out["why"].strip())
        self.assertIsInstance(out["troubleshoot"], list)
        self.assertTrue(all(isinstance(t, str) and t.strip()
                            for t in out["troubleshoot"]))

    def test_model_narration_survives_plan_chapters(self):
        give = [dict(CHAPTERS[0], troubleshoot=["Model tip."]), CHAPTERS[1]]
        brain = StubBrain([json.dumps({"chapters": give})])
        out = plan_chapters(PLAN, brain, "Blink")
        self.assertEqual(out[0]["why"], CHAPTERS[0]["why"])
        self.assertEqual(out[0]["troubleshoot"], ["Model tip."])
        self.assertEqual(out[1]["why"], CHAPTERS[1]["why"])


class RollcallTest(unittest.TestCase):
    """Roll call inventories the live parts data: nothing to invent,
    so it is always derived — and always accurate."""

    def test_rollcall_names_missing_parts(self):
        rc = complete_rollcall(None, ["esp32"], ["hc-sr04", "buzzer"])
        self.assertIn("hc-sr04", rc["say"])
        self.assertIn("buzzer", rc["say"])
        self.assertIn("3", rc["say"])
        self.assertTrue(rc["question"].strip().endswith("?"))
        flat = [kw for alt in rc["answers"] for kw in alt]
        self.assertIn("ready", flat)

    def test_rollcall_all_present_says_so(self):
        rc = complete_rollcall(None, ["esp32", "hc-sr04"], [])
        self.assertIn("all", rc["say"].lower())
        self.assertNotIn("missing", rc["say"].lower())

    def test_rollcall_without_inventory_stays_honest(self):
        rc = complete_rollcall(None, [], [])
        self.assertTrue(rc["say"].strip())
        self.assertTrue(rc["question"].strip().endswith("?"))
        flat = [kw for alt in rc["answers"] for kw in alt]
        self.assertIn("ready", flat)

    def test_rollcall_accepts_readies(self):
        rc = complete_rollcall(None, ["esp32"], ["hc-sr04"])
        for msg in ("ready", "yes ready", "we are ready", "let's go",
                    "all here", "yep, done"):
            self.assertTrue(rollcall_accepts(msg, rc["answers"]), msg)

    def test_rollcall_rejects_holds_and_negations(self):
        rc = complete_rollcall(None, ["esp32"], ["hc-sr04"])
        for msg in ("wait", "not ready", "hold on", "no", "not yet",
                    "give me a minute", "don't mark me ready yet",
                    "i'm not ready"):
            self.assertFalse(rollcall_accepts(msg, rc["answers"]), msg)


class StretchTest(unittest.TestCase):
    """DONE earns model-written stretch ideas grounded in the project;
    the fallback stays honest when the model is away."""

    def test_fallback_suggests_grounded_ideas(self):
        brain = StubBrain([])
        brain.mock = True
        out = plan_stretch("Blink", PLAN, ["led"], [], brain)
        self.assertGreaterEqual(len(out), 1)
        self.assertLessEqual(len(out), 2)
        for idea in out:
            self.assertTrue(str(idea["title"]).strip(), idea)
            self.assertTrue(str(idea["summary"]).strip(), idea)
            self.assertIn("Blink", idea["summary"], idea)
        self.assertEqual(brain.calls, [])

    def test_model_stretch_used_verbatim(self):
        give = {"stretch": [
            {"title": "Model idea", "summary": "A model-written stretch."}]}
        brain = StubBrain([json.dumps(give)])
        out = plan_stretch("Blink", PLAN, ["led"], [], brain)
        self.assertEqual(out, give["stretch"])
        self.assertEqual(len(brain.calls), 1)

    def test_bad_model_stretch_falls_back(self):
        brain = StubBrain(["not json",
                           json.dumps({"stretch": [{"title": "  "}]})])
        out = plan_stretch("Blink", PLAN, ["led"], [], brain)
        self.assertGreaterEqual(len(out), 1)
        for idea in out:
            self.assertTrue(str(idea["title"]).strip(), idea)
            self.assertIn("Blink", idea["summary"], idea)


if __name__ == "__main__":
    unittest.main()
