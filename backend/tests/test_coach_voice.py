"""Tier 3 voice wrap (+ Tier 4 memory): spoken + typed turns through the
same coach brain, persisted per session id.

The Learning coach core (CoachSession: memory + tools) serves the web UI:
POST /api/coach/ask answers one turn, POST /api/coach/ask/stream emits
ack -> tool_call/tool_result -> token* -> done/error so the frontend can
speak clauses as they land. One session id = one remembered conversation
(single user; restart or a fresh id forgets until Tier 4).

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_coach_voice -v
"""
import json
import os
import unittest
from unittest import mock

import backend.app.main as app_main
from backend.tests.helpers import use_mock_env


def parse_sse(frames):
    """Split SSE frames into [(event, payload)] like the frontend parser."""
    out = []
    buf = "".join(frames)
    for frame in buf.split("\n\n"):
        name, data = "", ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if name:
            out.append((name, json.loads(data) if data else {}))
    return out


class CoachVoiceTests(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"COACH_MEMORY_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        app_main._COACH_SESSIONS.clear()

    def test_ask_echoes_session_and_answers(self):
        out = app_main.api_coach_ask({"session_id": "s1", "text": "hello"})
        self.assertEqual(out["session_id"], "s1")
        self.assertIn("8-step", out["reply"])
        self.assertEqual(out["tool_calls"], [])
        self.assertTrue(out["mock"])

    def test_missing_session_id_mints_one(self):
        out = app_main.api_coach_ask({"text": "hello"})
        self.assertTrue(out["session_id"])
        again = app_main.api_coach_ask(
            {"session_id": out["session_id"], "text": "what was I working on?"})
        self.assertEqual(again["session_id"], out["session_id"])

    def test_same_session_remembers_fresh_session_forgets(self):
        app_main.api_coach_ask({"session_id": "mem", "text": "my hobby is kite surfing"})
        out = app_main.api_coach_ask({"session_id": "mem", "text": "what was I working on?"})
        self.assertIn("kite surfing", out["reply"])
        fresh = app_main.api_coach_ask({"session_id": "new", "text": "what was I working on?"})
        self.assertNotIn("kite surfing", fresh["reply"])

    def test_tool_turn_reports_calls(self):
        out = app_main.api_coach_ask({"session_id": "t", "text": "teach me step 4"})
        self.assertEqual(out["tool_calls"][0]["name"], "explain_step")
        self.assertTrue(out["tool_calls"][0]["ok"])
        self.assertIn("Step 4", out["reply"])

    def test_bad_tool_args_answer_plainly_without_raising(self):
        out = app_main.api_coach_ask({"session_id": "t", "text": "teach me step 99"})
        self.assertFalse(out["tool_calls"][0]["ok"])
        self.assertIn("did not work", out["reply"])

    def test_empty_text_gets_prompt_reply(self):
        out = app_main.api_coach_ask({"session_id": "t", "text": "   "})
        self.assertIn("What would you like to learn", out["reply"])

    def test_real_without_key_is_502(self):
        from fastapi import HTTPException

        with mock.patch.dict(os.environ, {"MOCK_META": "false", "META_API_KEY": ""}):
            with self.assertRaises(HTTPException) as ctx:
                app_main.api_coach_ask({"session_id": "live", "text": "hello"})
        self.assertEqual(ctx.exception.status_code, 502)

    def test_session_store_caps_size(self):
        for i in range(40):
            app_main.api_coach_ask({"session_id": "s%d" % i, "text": "hello"})
        self.assertLessEqual(len(app_main._COACH_SESSIONS), 32)
        self.assertIn("s39", app_main._COACH_SESSIONS)

    def test_routes_registered(self):
        routes = [(r.path, tuple(sorted(getattr(r, "methods", None) or [])))
                  for r in app_main.app.routes if hasattr(r, "path")]
        self.assertIn(("/api/coach/ask", ("POST",)), routes)
        self.assertIn(("/api/coach/ask/stream", ("POST",)), routes)


class CoachStreamTests(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"COACH_MEMORY_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        app_main._COACH_SESSIONS.clear()

    def test_stream_tokens_assemble_to_done_reply(self):
        events = parse_sse(list(app_main.iter_coach_events("s1", "hello")))
        kinds = [k for k, _ in events]
        self.assertEqual(kinds[0], "ack")
        self.assertEqual(kinds[-1], "done")
        self.assertEqual(events[0][1]["session_id"], "s1")
        done = events[-1][1]
        spoken = "".join(p["text"] for k, p in events if k == "token")
        self.assertEqual(spoken, done["reply"])
        self.assertTrue(done["reply"])
        self.assertEqual(done["session_id"], "s1")

    def test_stream_announces_tool_calls(self):
        events = parse_sse(list(app_main.iter_coach_events("s1", "teach me step 4")))
        kinds = [k for k, _ in events]
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)
        call = next(p for k, p in events if k == "tool_call")
        self.assertEqual(call["name"], "explain_step")
        result = next(p for k, p in events if k == "tool_result")
        self.assertTrue(result["ok"])

    def test_stream_mints_session_when_missing(self):
        events = parse_sse(list(app_main.iter_coach_events("", "hello")))
        kinds = [k for k, _ in events]
        self.assertEqual(kinds[0], "ack")
        sid = events[0][1]["session_id"]
        self.assertTrue(sid)
        self.assertEqual(events[-1][1]["session_id"], sid)

    def test_stream_error_on_model_outage(self):
        with mock.patch.dict(os.environ, {"MOCK_META": "false", "META_API_KEY": ""}):
            events = parse_sse(list(app_main.iter_coach_events("live", "hello")))
        kinds = [k for k, _ in events]
        self.assertIn("error", kinds)
        err = next(p for k, p in events if k == "error")
        self.assertIn("META_API_KEY", err["detail"])


if __name__ == "__main__":
    unittest.main()
