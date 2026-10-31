"""Tier 4 memory: coach conversations persist across restarts.

A CoachSession with a session_id loads its history from disk at init and
saves after every turn, so both the terminal chat and the web voice wrap
remember earlier conversations. Missing or corrupt files start fresh
without raising; ids are sanitized to filenames; old sessions prune.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_coach_memory -v
"""
import json
import os
import unittest
from unittest import mock

import backend.app.main as app_main
from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.coach import (
    CoachSession,
    coach_memory_dir,
    sanitize_session_id,
)
from backend.tests.helpers import use_mock_env


def _mock_coach(session_id=None):
    return CoachSession(Muse=MetaClaudeAdapter(mock=True),
                        session_id=session_id)


class CoachMemoryTests(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"COACH_MEMORY_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        app_main._COACH_SESSIONS.clear()

    def test_save_and_restore_across_instances(self):
        first = _mock_coach("trip")
        first.ask("my hobby is kite surfing")
        self.assertTrue(os.listdir(self.tmp.name))  # a file was written
        second = _mock_coach("trip")
        self.assertEqual(len(second.history), 2)
        out = second.ask("what was I working on?")
        self.assertIn("kite surfing", out["reply"])

    def test_no_session_id_stays_in_memory_only(self):
        coach = _mock_coach()
        coach.ask("my hobby is kite surfing")
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_fresh_id_starts_empty(self):
        self.assertEqual(_mock_coach("new").history, [])

    def test_corrupt_file_starts_fresh(self):
        bad = os.path.join(coach_memory_dir(), "bad.json")
        os.makedirs(coach_memory_dir(), exist_ok=True)
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        coach = _mock_coach("bad")
        self.assertEqual(coach.history, [])
        out = coach.ask("hello")
        self.assertIn("8-step", out["reply"])

    def test_malformed_entries_filtered(self):
        path = os.path.join(coach_memory_dir(), "messy.json")
        os.makedirs(coach_memory_dir(), exist_ok=True)
        payload = {"version": 1, "session_id": "messy", "updated_at": "t",
                   "history": [{"role": "user", "content": "my hobby is kite surfing"},
                               "junk",
                               {"role": "user"},
                               {"role": "assistant", "content": 42},
                               {"role": "assistant", "content": "noted"}]}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        coach = _mock_coach("messy")
        self.assertEqual(len(coach.history), 2)
        out = coach.ask("what was I working on?")
        self.assertIn("kite surfing", out["reply"])

    def test_sanitize_blocks_traversal(self):
        sid = sanitize_session_id("../../etc/x")
        self.assertNotIn("/", sid)
        self.assertNotIn("\\", sid)
        self.assertNotIn("..", sid)
        _mock_coach("../../etc/x").ask("hello")
        names = os.listdir(self.tmp.name)
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".json"))

    def test_sanitize_empty_mints(self):
        self.assertTrue(sanitize_session_id(""))
        self.assertTrue(sanitize_session_id("..."))

    def test_disk_prune_keeps_newest(self):
        for i in range(35):
            _mock_coach("s%02d" % i).ask("hello")
            path = os.path.join(self.tmp.name, "s%02d.json" % i)
            stamp = 1700000000 + i
            os.utime(path, (stamp, stamp))
        _mock_coach("newest").ask("hello")
        names = sorted(os.listdir(self.tmp.name))
        self.assertLessEqual(len(names), 32)
        self.assertIn("newest.json", names)
        self.assertNotIn("s00.json", names)

    def test_web_session_survives_restart(self):
        app_main.api_coach_ask({"session_id": "web1",
                                "text": "my hobby is kite surfing"})
        app_main._COACH_SESSIONS.clear()  # simulate a backend restart
        out = app_main.api_coach_ask({"session_id": "web1",
                                      "text": "what was I working on?"})
        self.assertIn("kite surfing", out["reply"])

    def test_web_session_id_sanitized(self):
        out = app_main.api_coach_ask({"session_id": "../evil", "text": "hello"})
        self.assertNotIn("/", out["session_id"])
        self.assertNotIn("..", out["session_id"])
        names = os.listdir(self.tmp.name)
        self.assertEqual(len(names), 1)


if __name__ == "__main__":
    unittest.main()
