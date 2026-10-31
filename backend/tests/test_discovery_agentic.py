"""Agentic learning: validator gates, sensor-gated steps, code gating.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_discovery_agentic -v

No hardware, no network: mock probe + mock Muse adapters. The validator
rejects bad plans, the probe alone advances steps, and firmware is gated
until every step passes.
"""
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException

from backend.app import discovery
from backend.app import main as app_main
from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.adapters.sam_adapter import MetaSAMAdapter
from backend.app.adapters.voice_adapter import MetaVoiceAdapter
from backend.app.agent_orchestrator import AgentOrchestrator
from backend.app.change_detector import MotionSettler
from backend.app.circuit_graph import load_circuit_graph
from backend.app.models import CheckNowRequest
from backend.app.serial_probe import SerialProbe
from backend.tests.helpers import make_camera, use_mock_env


def step(n, verify, expect=None, depends=None, instruction="wire it"):
    return {"n": n, "instruction": instruction, "verify": verify,
            "expect": expect or {}, "depends_on_parts": depends or []}


PLAN_2STEP = [
    step(1, "RUN_I2C_SCAN", expect={"addresses": ["0x76"]},
         depends=["bme280"], instruction="Wire the BME280 to the I2C bus."),
    step(2, "READ_SENSOR", depends=["bme280"],
         instruction="Confirm the sensor reads temperature."),
]


class ValidatorTest(unittest.TestCase):
    def test_rejects_unknown_test(self):
        reasons = discovery.validate_project(
            {"steps": [step(1, "MAGIC_TEST")]}, ["esp32"])
        self.assertTrue(reasons)
        self.assertTrue(any("MAGIC_TEST" in r for r in reasons), reasons)

    def test_rejects_unavailable_part(self):
        reasons = discovery.validate_project(
            {"steps": [step(1, "CHECK_GPIO", depends=["servo"])]},
            ["esp32", "bme280"])
        self.assertTrue(reasons)
        self.assertTrue(any("servo" in r for r in reasons), reasons)

    def test_start_learning_session_rejects_bad_plan(self):
        use_mock_env(self)
        orc = _make_orc(self)
        with self.assertRaises(ValueError):
            orc.start_learning_session("bad", [step(1, "MAGIC_TEST")],
                                       visible_parts=["esp32"])


def _make_orc(tc):
    tmp = tempfile.TemporaryDirectory()
    tc.addCleanup(tmp.cleanup)
    return AgentOrchestrator(
        graph=load_circuit_graph(),
        camera=make_camera(tmp.name),
        settler=MotionSettler(settle_seconds=1.0),
        probe=SerialProbe(mock=True),
        sam=MetaSAMAdapter(mock=True),
        Muse=MetaClaudeAdapter(mock=True),
        voice=MetaVoiceAdapter(mock=True),
        attempt_log_path="%s/attempts.jsonl" % tmp.name,
    )


class LearningLoopTest(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.orc = _make_orc(self)

    def test_start_answers_instantly_and_fills_chapters_in_background(self):
        full = [{"n": 1, "title": "T1", "teach": "do it", "task": "wire",
                 "verify": "RUN_I2C_SCAN", "celebration": ""},
                {"n": 2, "title": "T2", "teach": "read it",
                 "task": "read", "verify": "READ_SENSOR",
                 "celebration": ""}]
        calls, threads = [], []

        def fake_plan(plan, Muse, title="", board=None):
            calls.append(list(plan))
            return [dict(c) for c in full]

        class FakeThread:
            def __init__(self, *a, **k):
                threads.append(k)
                self._target, self._args = k["target"], k["args"]

            def start(self):
                pass  # the worker itself runs explicitly below

        self.orc.Muse = MetaClaudeAdapter(mock=False)
        with mock.patch("backend.app.agent_orchestrator.plan_chapters",
                        side_effect=fake_plan):
            with mock.patch("threading.Thread", FakeThread):
                view = self.orc.start_learning_session(
                    "weather", PLAN_2STEP, visible_parts=["esp32", "bme280"],
                    session_id="learn-fast", project_title="Weather station")
        # Zero model calls on the start path: templates answer now.
        self.assertEqual(calls, [])
        self.assertIn("Starting Weather station", view["speak"])
        self.assertEqual(len(self.orc.session["chapters"]), 2)
        self.assertEqual(self.orc.session["chapters"][0]["teach"], "")
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0]["daemon"])
        # Explicit fill step: the background worker swaps the full set.
        with mock.patch("backend.app.agent_orchestrator.plan_chapters",
                        side_effect=fake_plan):
            threads[0]["target"](*threads[0]["args"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 2)
        self.assertEqual(self.orc.session["chapters"][0]["teach"], "do it")
        self.assertEqual(self.orc.session["chapters"][1]["teach"], "read it")

    def test_fill_worker_swaps_full_chapters(self):
        self.orc.start_learning_session(
            "weather", PLAN_2STEP, visible_parts=["esp32", "bme280"],
            session_id="learn-fill")
        self.orc.session["chapters"] = [{"marker": True}]
        self.orc._fill_chapters("learn-fill", self.orc.session, PLAN_2STEP,
                                "Weather station", "esp32_devkit")
        chapters = self.orc.session["chapters"]
        self.assertEqual(len(chapters), 2)
        self.assertNotIn("marker", chapters[0])

    def test_fill_worker_ignores_stale_session(self):
        self.orc.start_learning_session(
            "weather", PLAN_2STEP, visible_parts=["esp32", "bme280"],
            session_id="learn-stale")
        old = self.orc.session
        self.orc.start_learning_session(
            "weather", PLAN_2STEP, visible_parts=["esp32", "bme280"],
            session_id="learn-stale")  # restart replaces the session object
        self.orc.session["chapters"] = [{"marker": True}]
        self.orc._fill_chapters("learn-stale", old, PLAN_2STEP,
                                "Weather station", "esp32_devkit")
        self.assertEqual(self.orc.session["chapters"], [{"marker": True}])

    def test_advances_one_step_per_pass_and_refuses_early_done(self):
        self.orc.start_learning_session(
            "weather", PLAN_2STEP, visible_parts=["esp32", "bme280"],
            session_id="learn-1", project_title="Weather station")
        v = self.orc.check_now(CheckNowRequest())
        self.assertTrue(v["verification"]["passed"], v["verification"])
        self.assertEqual(v["step_index"], 1)
        self.assertEqual(v["state"], "INSTRUCTING")
        self.assertNotEqual(v["state"], "DONE")
        self.assertFalse(v["code_ready"])

    def test_fail_does_not_advance_and_includes_evidence(self):
        bad = [dict(PLAN_2STEP[0], expect={"addresses": ["0x99"]})]
        self.orc.start_learning_session(
            "weather", bad, visible_parts=["esp32", "bme280"],
            session_id="learn-fail")
        v = self.orc.check_now(CheckNowRequest())
        self.assertFalse(v["verification"]["passed"])
        self.assertEqual(v["step_index"], 0)
        self.assertEqual(v["state"], "VERIFYING")
        self.assertIn("MISSING", v["verification"]["measured"])
        self.assertIn("0x99", v["verification"]["measured"])
        self.assertIn(v["verification"]["measured"], v["instruction"])

    def test_code_409_before_done_200_after(self):
        self.orc.start_learning_session(
            "weather", PLAN_2STEP, visible_parts=["esp32", "bme280"],
            session_id="learn-code", project_title="Weather station")
        with mock.patch.object(app_main, "orchestrator", self.orc):
            with self.assertRaises(HTTPException) as before:
                app_main.api_project_code("learn-code")
            self.assertEqual(before.exception.status_code, 409)
            self.orc.check_now(CheckNowRequest())
            v = self.orc.check_now(CheckNowRequest())
            self.assertEqual(v["state"], "DONE")
            self.assertTrue(v["code_ready"])
            self.assertIn("code", v["speak"].lower())  # DONE promises the .ino
            self.assertIn("code", v["instruction"].lower())
            out = app_main.api_project_code("learn-code")
        self.assertEqual(out["session_id"], "learn-code")
        self.assertIn("#define SDA_PIN 21", out["code"])
        self.assertIn("#define SCL_PIN 22", out["code"])
        self.assertIn("0x76", out["code"])

    def test_draft_code_available_before_done(self):
        self.orc.start_learning_session(
            "weather", PLAN_2STEP, visible_parts=["esp32", "bme280"],
            session_id="learn-draft", project_title="Weather station")
        with mock.patch.object(app_main, "orchestrator", self.orc):
            out = app_main.api_project_code("learn-draft", {"draft": True})
        self.assertFalse(out["verified"])
        self.assertEqual(
            (out["steps_passed"], out["steps_total"]), (0, 2))
        self.assertIn("// DRAFT", out["code"])
        self.assertIn("0 of 2 steps", out["code"])
        self.assertIn("#define LED_PIN", out["code"])

    def test_draft_counts_verified_steps(self):
        self.orc.start_learning_session(
            "weather", PLAN_2STEP, visible_parts=["esp32", "bme280"],
            session_id="learn-draft2", project_title="Weather station")
        self.orc.check_now(CheckNowRequest())
        with mock.patch.object(app_main, "orchestrator", self.orc):
            out = app_main.api_project_code("learn-draft2", {"draft": True})
        self.assertFalse(out["verified"])
        self.assertEqual(
            (out["steps_passed"], out["steps_total"]), (1, 2))
        self.assertIn("1 of 2 steps", out["code"])

    def test_draft_prompt_marks_unverified_build(self):
        from backend.app.prompts import learning_code_prompt

        text = learning_code_prompt("t", [], [], {}, {}, draft=True)
        self.assertIn("NOT fully verified", text)
        final = learning_code_prompt("t", [], [], {}, {})
        self.assertIn("VERIFIED", final)

    def test_check_now_past_done_stays_done(self):
        self.orc.start_learning_session(
            "blink", [step(1, "CHECK_GPIO", depends=["led"],
                            instruction="Wire the LED to GPIO2.")],
            visible_parts=["esp32", "led"], session_id="learn-gpio")
        v = self.orc.check_now(CheckNowRequest())
        self.assertEqual(v["state"], "DONE")
        v2 = self.orc.check_now(CheckNowRequest())
        self.assertEqual(v2["state"], "DONE")
        self.assertEqual(v2["step_index"], 1)


if __name__ == "__main__":
    unittest.main()
