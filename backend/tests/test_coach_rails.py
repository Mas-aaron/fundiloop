"""Tier 6 rails: confirmation gate, config, audit trail, kill switch.

Unsafe tools (safe=False) — or every tool in strict mode — pause for
explicit technician confirmation instead of running. Rail events append
to a hash-chained audit log. The kill switch halts all coach turns
until re-armed. All coach tools ship read-only, so the gate rests
until an unsafe tool registers or strict mode flips on.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_coach_rails -v
"""
import json
import os
import unittest
from unittest import mock

import backend.app.main as app_main
from backend.app.adapters import MetaAPIError
from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.coach import (
    CoachKilledError,
    CoachSession,
    Tool,
    audit_rail,
    coach_killed,
    rails_config,
    read_audit_log,
    register_tool,
    set_killswitch,
    verify_audit_chain,
)
from backend.tests.helpers import use_mock_env


def _mock_coach(session_id=None):
    return CoachSession(Muse=MetaClaudeAdapter(mock=True),
                        session_id=session_id)


class GateTests(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "COACH_MEMORY_DIR": os.path.join(self.tmp.name, "mem"),
            "COACH_AUDIT_LOG": os.path.join(self.tmp.name, "audit.jsonl"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        app_main._COACH_SESSIONS.clear()
        self.addCleanup(self._restore_registry)

    def _restore_registry(self):
        from backend.app.coach import TOOL_REGISTRY

        TOOL_REGISTRY.pop("danger_delete", None)

    def _register_unsafe(self):
        register_tool(Tool(
            name="danger_delete", safe=False,
            description="Deletes things. Test-only unsafe tool.",
            risk="delete the test workpiece",
            inputs={"target": {"type": "str",
                               "description": "What to delete."}},
            func=lambda target: {"deleted": target}))

    def test_safe_tools_flow_freely_by_default(self):
        out = _mock_coach("g1").ask("teach me step 4")
        self.assertIsNone(out["confirmation_pending"])
        self.assertTrue(out["tool_calls"][0]["ok"])

    def test_unsafe_tool_pauses_for_confirmation(self):
        self._register_unsafe()
        coach = _mock_coach("g2")
        with mock.patch.object(coach, "_decide", return_value=(
                {"name": "danger_delete", "args": {"target": "workpiece"}}, None)):
            out = coach.ask("delete the workpiece")
        pending = out["confirmation_pending"]
        self.assertIsNotNone(pending)
        self.assertEqual(pending["tool"], "danger_delete")
        self.assertIn("delete the test workpiece", pending["question"])
        self.assertEqual(out["tool_calls"], [])

    def test_confirm_yes_runs_tool_and_replies(self):
        self._register_unsafe()
        coach = _mock_coach("g3")
        with mock.patch.object(coach, "_decide", side_effect=[
                ({"name": "danger_delete", "args": {"target": "workpiece"}}, None),
                (None, "Deleted it.")]):
            out = coach.ask("delete the workpiece")
            final = coach.confirm(out["confirmation_pending"]["call_id"], True)
        self.assertEqual(final["reply"], "Deleted it.")
        self.assertTrue(final["tool_calls"][0]["ok"])
        self.assertIsNone(final["confirmation_pending"])

    def test_confirm_no_aborts_plainly(self):
        self._register_unsafe()
        coach = _mock_coach("g4")
        with mock.patch.object(coach, "_decide", return_value=(
                {"name": "danger_delete", "args": {"target": "workpiece"}}, None)):
            out = coach.ask("delete the workpiece")
            final = coach.confirm(out["confirmation_pending"]["call_id"], False)
        self.assertIn("won't", final["reply"])
        self.assertEqual(final["tool_calls"], [])

    def test_confirm_wrong_id_raises(self):
        coach = _mock_coach("g5")
        with self.assertRaises(ValueError):
            coach.confirm("nope", True)

    def test_strict_mode_gates_safe_tools(self):
        with mock.patch.dict(os.environ, {"COACH_GATE_STRICT": "true"}):
            out = _mock_coach("g6").ask("teach me step 4")
        self.assertIsNotNone(out["confirmation_pending"])
        self.assertEqual(out["confirmation_pending"]["tool"], "explain_step")

    def test_strict_confirm_completes_turn(self):
        with mock.patch.dict(os.environ, {"COACH_GATE_STRICT": "true"}):
            coach = _mock_coach("g7")
            out = coach.ask("teach me step 4")
            final = coach.confirm(out["confirmation_pending"]["call_id"], True)
        self.assertIn("Step 4", final["reply"])

    def test_rails_config_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COACH_GATE_STRICT", None)
            self.assertEqual(rails_config(), {"gate_strict": False})


class AuditTests(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "COACH_MEMORY_DIR": os.path.join(self.tmp.name, "mem"),
            "COACH_AUDIT_LOG": os.path.join(self.tmp.name, "audit.jsonl"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        app_main._COACH_SESSIONS.clear()

    def test_gate_flow_audited_and_chain_valid(self):
        from backend.app.coach import TOOL_REGISTRY

        register_tool(Tool(
            name="danger_delete", safe=False,
            description="Deletes things. Test-only unsafe tool.",
            risk="delete the test workpiece",
            inputs={"target": {"type": "str", "description": "What."}},
            func=lambda target: {"deleted": target}))
        self.addCleanup(lambda: TOOL_REGISTRY.pop("danger_delete", None))
        coach = _mock_coach("a1")
        with mock.patch.object(coach, "_decide", side_effect=[
                ({"name": "danger_delete", "args": {"target": "w"}}, None),
                (None, "Deleted it.")]):
            out = coach.ask("delete it")
            coach.confirm(out["confirmation_pending"]["call_id"], True)
        events = read_audit_log()
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds, ["tool_proposed", "tool_confirmed", "tool_ran"])
        self.assertTrue(verify_audit_chain(events))
        self.assertEqual(events[0]["session_id"], "a1")

    def test_tampered_chain_fails(self):
        audit_rail("tool_proposed", session_id="t", tool="x")
        events = read_audit_log()
        events[0]["tool"] = "y"
        self.assertFalse(verify_audit_chain(events))

    def test_audit_never_raises(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("not a dir")
        with mock.patch.dict(os.environ, {"COACH_AUDIT_LOG": os.path.join(
                blocker, "audit.jsonl")}):
            audit_rail("tool_proposed")  # unwriteable path: silent


class KillSwitchTests(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "COACH_MEMORY_DIR": os.path.join(self.tmp.name, "mem"),
            "COACH_AUDIT_LOG": os.path.join(self.tmp.name, "audit.jsonl"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        app_main._COACH_SESSIONS.clear()

    def test_kill_halts_turns_until_rearm(self):
        self.assertFalse(coach_killed())
        set_killswitch(True, actor="test")
        self.assertTrue(coach_killed())
        with self.assertRaises(CoachKilledError):
            _mock_coach("k1").ask("hello")
        with self.assertRaises(CoachKilledError):
            _mock_coach("k1").nudge(3600, "step 1")
        set_killswitch(False, actor="test")
        self.assertFalse(coach_killed())
        out = _mock_coach("k1").ask("hello")
        self.assertIn("8-step", out["reply"])

    def test_kill_audited(self):
        set_killswitch(True, actor="web")
        set_killswitch(False, actor="web")
        kinds = [e["event"] for e in read_audit_log()]
        self.assertEqual(kinds, ["kill_tripped", "kill_rearmed"])

    def test_killed_error_is_meta_api_error(self):
        self.assertTrue(issubclass(CoachKilledError, MetaAPIError))


class RailsEndpointTests(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "COACH_MEMORY_DIR": os.path.join(self.tmp.name, "mem"),
            "COACH_AUDIT_LOG": os.path.join(self.tmp.name, "audit.jsonl"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        app_main._COACH_SESSIONS.clear()

    def test_confirm_mismatch_is_409(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            app_main.api_coach_confirm({"session_id": "e1", "call_id": "x",
                                        "confirmed": True})
        self.assertEqual(ctx.exception.status_code, 409)

    def test_killed_ask_is_503(self):
        from fastapi import HTTPException

        set_killswitch(True, actor="test")
        try:
            with self.assertRaises(HTTPException) as ctx:
                app_main.api_coach_ask({"session_id": "e2", "text": "hi"})
            self.assertEqual(ctx.exception.status_code, 503)
            with self.assertRaises(HTTPException) as ctx:
                app_main.api_coach_nudge({"session_id": "e2",
                                          "idle_seconds": 3600})
            self.assertEqual(ctx.exception.status_code, 503)
        finally:
            set_killswitch(False, actor="test")

    def test_killswitch_round_trip(self):
        self.assertFalse(app_main.api_coach_killswitch_get()["tripped"])
        out = app_main.api_coach_killswitch({"tripped": True})
        self.assertTrue(out["tripped"])
        self.assertTrue(app_main.api_coach_killswitch_get()["tripped"])
        app_main.api_coach_killswitch({"tripped": False})
        self.assertFalse(app_main.api_coach_killswitch_get()["tripped"])

    def test_killswitch_rejects_non_bool(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            app_main.api_coach_killswitch({"tripped": "yes"})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_status_reports_rails(self):
        view = app_main.api_status()
        self.assertFalse(view["config"]["coach_gate_strict"])
        self.assertFalse(view["config"]["coach_killed"])
        set_killswitch(True, actor="test")
        try:
            view = app_main.api_status()
            self.assertTrue(view["config"]["coach_killed"])
        finally:
            set_killswitch(False, actor="test")

    def test_routes_registered(self):
        routes = [(r.path, tuple(sorted(getattr(r, "methods", None) or [])))
                  for r in app_main.app.routes if hasattr(r, "path")]
        self.assertIn(("/api/coach/confirm", ("POST",)), routes)
        self.assertIn(("/api/coach/killswitch", ("GET",)), routes)
        self.assertIn(("/api/coach/killswitch", ("POST",)), routes)


if __name__ == "__main__":
    unittest.main()
