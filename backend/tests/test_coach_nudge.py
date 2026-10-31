"""Tier 5 heartbeat: proactive nudges through the same coach brain.

Quiet by default: the backend nudges only after a long idle mid-step,
never during quiet hours, and never twice within the cooldown. Rules
decide WHETHER; the brain (mock template or real model) writes WHAT.
Nudge state persists with the session; nudge turns stay out of the
mock memory-line so they never pose as user questions.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_coach_nudge -v
"""
import os
import unittest
from datetime import datetime
from unittest import mock

import backend.app.main as app_main
from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.coach import CoachSession, in_quiet_hours, nudge_config
from backend.tests.helpers import use_mock_env

NOON = datetime(2026, 10, 30, 12, 0, 0)
NIGHT = datetime(2026, 10, 30, 23, 30, 0)


def _mock_coach(session_id=None):
    return CoachSession(Muse=MetaClaudeAdapter(mock=True),
                        session_id=session_id)


class NudgeRuleTests(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "COACH_MEMORY_DIR": self.tmp.name,
            "COACH_AUDIT_LOG": os.path.join(self.tmp.name, "audit.jsonl"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_short_idle_stays_quiet(self):
        self.assertIsNone(_mock_coach("r1").nudge(60, "step 4 of 8", NOON))

    def test_long_idle_nudges_briefly(self):
        out = _mock_coach("r2").nudge(700, "step 4 of 8", NOON)
        self.assertEqual(out["reason"], "idle")
        self.assertIn("step 4 of 8", out["text"])
        self.assertLessEqual(len(out["text"].split()), 30)

    def test_quiet_hours_stay_quiet(self):
        self.assertIsNone(_mock_coach("r3").nudge(3600, "step 4 of 8", NIGHT))

    def test_cooldown_suppresses_second_nudge(self):
        coach = _mock_coach("r4")
        self.assertIsNotNone(coach.nudge(700, "step 4 of 8", NOON))
        self.assertIsNone(coach.nudge(700, "step 4 of 8", NOON))

    def test_cooldown_expires(self):
        coach = _mock_coach("r5")
        coach.nudge(700, "step 4 of 8", NOON)
        later = datetime(2026, 10, 30, 12, 30, 0)
        out = coach.nudge(700, "step 4 of 8", later)
        self.assertIsNotNone(out)

    def test_cooldown_survives_reload(self):
        _mock_coach("r6").nudge(700, "step 4 of 8", NOON)
        fresh = _mock_coach("r6")  # new instance, same id
        self.assertIsNone(fresh.nudge(700, "step 4 of 8", NOON))

    def test_nudge_turns_skip_mock_memory_line(self):
        coach = _mock_coach("r7")
        coach.ask("my hobby is kite surfing")
        coach.nudge(700, "step 4 of 8", NOON)
        out = coach.ask("what was I working on?")
        self.assertIn("kite surfing", out["reply"])
        self.assertNotIn("[nudge]", out["reply"])


class QuietHoursTests(unittest.TestCase):
    def test_overnight_window(self):
        self.assertTrue(in_quiet_hours(datetime(2026, 10, 30, 23, 0), 22, 7))
        self.assertTrue(in_quiet_hours(datetime(2026, 10, 30, 3, 0), 22, 7))
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 30, 12, 0), 22, 7))
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 30, 7, 0), 22, 7))

    def test_daytime_window(self):
        self.assertTrue(in_quiet_hours(datetime(2026, 10, 30, 14, 0), 13, 15))
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 30, 15, 0), 13, 15))

    def test_config_defaults_and_overrides(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COACH_IDLE_NUDGE_SECONDS", None)
            os.environ.pop("COACH_NUDGE_COOLDOWN_SECONDS", None)
            os.environ.pop("QUIET_HOURS_START", None)
            os.environ.pop("QUIET_HOURS_END", None)
            cfg = nudge_config()
            self.assertEqual((cfg["idle_seconds"], cfg["cooldown_seconds"]),
                             (600, 900))
            self.assertEqual((cfg["quiet_start"], cfg["quiet_end"]), (22, 7))
        with mock.patch.dict(os.environ, {"COACH_IDLE_NUDGE_SECONDS": "60",
                                          "COACH_NUDGE_COOLDOWN_SECONDS": "60",
                                          "QUIET_HOURS_START": "bad",
                                          "QUIET_HOURS_END": "8"}):
            cfg = nudge_config()
            self.assertEqual((cfg["idle_seconds"], cfg["cooldown_seconds"]),
                             (60, 60))
            self.assertEqual((cfg["quiet_start"], cfg["quiet_end"]), (22, 8))


class NudgeEndpointTests(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "COACH_MEMORY_DIR": self.tmp.name,
            "COACH_AUDIT_LOG": os.path.join(self.tmp.name, "audit.jsonl"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        app_main._COACH_SESSIONS.clear()

    def test_short_idle_returns_null_nudge(self):
        out = app_main.api_coach_nudge({"session_id": "e1", "idle_seconds": 30,
                                        "step_text": "wire it"})
        self.assertEqual(out["session_id"], "e1")
        self.assertIsNone(out["nudge"])

    def test_long_idle_returns_nudge(self):
        with mock.patch("backend.app.coach.datetime") as dt:
            dt.now.return_value = NOON
            out = app_main.api_coach_nudge(
                {"session_id": "e2", "idle_seconds": 3600,
                 "step_text": "wire the LED", "step_label": "step 2 of 8"})
        self.assertIsNotNone(out["nudge"])
        self.assertIn("step 2 of 8", out["nudge"]["text"])

    def test_endpoint_sanitizes_session(self):
        out = app_main.api_coach_nudge({"session_id": "../evil",
                                        "idle_seconds": 5})
        self.assertNotIn("..", out["session_id"])

    def test_real_outage_is_502(self):
        from fastapi import HTTPException

        with mock.patch.dict(os.environ, {"MOCK_META": "false",
                                          "META_API_KEY": ""}):
            with mock.patch("backend.app.coach.datetime") as dt:
                dt.now.return_value = NOON
                with self.assertRaises(HTTPException) as ctx:
                    app_main.api_coach_nudge({"session_id": "live",
                                              "idle_seconds": 3600})
        self.assertEqual(ctx.exception.status_code, 502)

    def test_route_registered(self):
        routes = [(r.path, tuple(sorted(getattr(r, "methods", None) or [])))
                  for r in app_main.app.routes if hasattr(r, "path")]
        self.assertIn(("/api/coach/nudge", ("POST",)), routes)


if __name__ == "__main__":
    unittest.main()
