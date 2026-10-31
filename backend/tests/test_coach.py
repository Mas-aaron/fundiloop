"""Learning coach tiers 1+2: text brain loop + tool registry (no hardware).

Tier 1: the session remembers earlier turns in memory and answers in order.
Tier 2: the brain calls read-only teaching tools and reasons over results,
including failed tool results, instead of crashing.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_coach -v
"""
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.app.adapters import MetaAPIError
from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.coach import (
    COACH_SYSTEM,
    TOOL_REGISTRY,
    CoachSession,
    Tool,
    explain_part,
    explain_step,
    register_tool,
    run_tool,
)
from backend.tests.helpers import use_mock_env


class FakeRaw:
    status_code = 200
    headers = {"x-request-id": "req-coach-test"}

    def __init__(self, content):
        self._content = content

    def parse(self):
        return SimpleNamespace(
            id="chatcmpl-coach-test",
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))])


class FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.with_raw_response = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeRaw(item)


def _coach(mock=True):
    return CoachSession(Muse=MetaClaudeAdapter(mock=mock))


class CoachTests(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        # Isolate from real user state: the kill switch is global, so a
        # tripped switch on disk must never fail the core suite.
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "COACH_MEMORY_DIR": self.tmp.name,
            "COACH_AUDIT_LOG": os.path.join(self.tmp.name, "audit.jsonl"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- Tier 1: brain ----------------------------------------------------
    def test_system_names_coach_and_boundaries(self):
        self.assertIn("FundiLoop Learning", COACH_SYSTEM)
        self.assertIn("never mark a step complete", COACH_SYSTEM.lower())
        self.assertIn("never", COACH_SYSTEM.lower())

    def test_remembers_earlier_turns(self):
        coach = _coach()
        coach.ask("I am building a weather station today")
        out = coach.ask("what was I working on?")
        self.assertIn("weather", out["reply"])
        self.assertEqual(len(coach.history), 4)  # user/asst x2, no tools

    def test_history_grows_and_fresh_session_forgets(self):
        coach = _coach()
        coach.ask("hello")
        self.assertTrue(coach.history)
        fresh = _coach()
        self.assertEqual(fresh.history, [])

    def test_empty_input_needs_no_model_call(self):
        coach = _coach()
        out = coach.ask("   ")
        self.assertTrue(out["reply"])
        self.assertEqual(out["tool_calls"], [])
        self.assertEqual(coach.history, [])

    def test_real_mode_without_key_raises_clean(self):
        with mock.patch.dict(os.environ, {"MOCK_META": "false", "META_API_KEY": ""}):
            coach = _coach(mock=False)
            with self.assertRaises(MetaAPIError) as ctx:
                coach.ask("hello")
        self.assertIn("META_API_KEY", str(ctx.exception))
        # The user turn stays (retryable); no assistant turn was invented.
        self.assertEqual([m["role"] for m in coach.history], ["user"])

    def test_mock_adapter_guard_in_real_mode(self):
        with mock.patch.dict(os.environ, {"MOCK_META": "false"}):
            with self.assertRaises(MetaAPIError):
                MetaClaudeAdapter(mock=True).chat_text("s", [{"role": "user", "content": "hi"}])

    def test_chat_text_mock_returns_empty_json(self):
        self.assertEqual(
            MetaClaudeAdapter(mock=True).chat_text("s", [{"role": "user", "content": "hi"}]),
            "{}")

    # -- Tier 2: hands ----------------------------------------------------
    def test_step_question_calls_tool_and_weaves_result(self):
        coach = _coach()
        out = coach.ask("teach me step 4")
        self.assertEqual(len(out["tool_calls"]), 1)
        call = out["tool_calls"][0]
        self.assertEqual(call["name"], "explain_step")
        self.assertTrue(call["ok"])
        self.assertIn("BME280", out["reply"])
        self.assertIn("21", out["reply"])  # GPIO 21 from the real YAML step

    def test_out_of_range_step_explains_failure(self):
        coach = _coach()
        out = coach.ask("teach me step 99")  # must not raise
        self.assertEqual(len(out["tool_calls"]), 1)
        self.assertFalse(out["tool_calls"][0]["ok"])
        self.assertIn("8", out["reply"])  # only 8 steps exist

    def test_part_question_calls_tool(self):
        coach = _coach()
        out = coach.ask("what is the BME280?")
        self.assertEqual(out["tool_calls"][0]["name"], "explain_part")
        self.assertTrue(out["tool_calls"][0]["ok"])
        self.assertIn("0x76", out["reply"])

    def test_unknown_tool_name_returns_error_result(self):
        out = run_tool("nope", {})
        self.assertFalse(out["ok"])
        self.assertIn("nope", out["error"])
        self.assertIn("explain_step", out["error"])  # available names listed

    def test_bad_tool_args_rejected_plainly(self):
        missing = run_tool("explain_step", {})
        self.assertFalse(missing["ok"])
        self.assertIn("step_number", missing["error"])
        wrong_type = run_tool("explain_step", {"step_number": "four"})
        self.assertFalse(wrong_type["ok"])
        extra = run_tool("explain_part", {"part_name": "led", "bogus": 1})
        self.assertFalse(extra["ok"])

    def test_unknown_part_lists_vocabulary(self):
        with self.assertRaises(ValueError) as ctx:
            explain_part("flux_capacitor")
        self.assertIn("flux_capacitor", str(ctx.exception))

    def test_register_tool_extends_registry(self):
        register_tool(Tool(
            name="echo_test", safe=True,
            description="Use this only in tests to echo text back.",
            inputs={"text": {"type": "str", "description": "Text to echo."}},
            func=lambda text: {"echo": text}))
        try:
            out = run_tool("echo_test", {"text": "hi"})
            self.assertTrue(out["ok"])
            self.assertEqual(out["result"], {"echo": "hi"})
        finally:
            TOOL_REGISTRY.pop("echo_test", None)

    def test_tool_rounds_exhausted_summarizes(self):
        coach = CoachSession(Muse=MetaClaudeAdapter(mock=True), max_tool_rounds=1)
        out = coach.ask("teach me step 4")
        self.assertEqual(len(out["tool_calls"]), 1)
        self.assertIn("BME280", out["reply"])

    def test_ask_stream_replays_reply_tokens(self):
        coach = _coach()
        events = list(coach.ask_stream("teach me step 4"))
        kinds = [e["event"] for e in events]
        self.assertIn("tool_call", kinds)
        self.assertIn("done", kinds)
        tokens = "".join(e["text"] for e in events if e["event"] == "token")
        self.assertEqual(tokens, events[-1]["reply"])

    def test_real_brain_tool_loop_over_fakes(self):
        script = [
            json.dumps({"tool": {"name": "explain_step", "args": {"step_number": 4}},
                        "reply": None}),
            json.dumps({"tool": None, "reply": "Step 4 wires the BME280."}),
        ]
        comp = FakeCompletions(script)
        with mock.patch.dict(os.environ, {"MOCK_META": "false", "META_API_KEY": "test"}), \
                mock.patch("openai.OpenAI", return_value=SimpleNamespace(
                    chat=SimpleNamespace(completions=comp))):
            out = _coach(mock=False).ask("teach me step 4")
        self.assertEqual(out["reply"], "Step 4 wires the BME280.")
        self.assertEqual(len(out["tool_calls"]), 1)
        self.assertTrue(out["tool_calls"][0]["ok"])
        # Second model call saw the user turn plus the tool result.
        sent = comp.calls[1]["messages"]
        self.assertTrue(any("teach me step 4" in str(m.get("content")) for m in sent))
        self.assertTrue(any("BME280" in str(m.get("content")) for m in sent))

    def test_real_brain_invalid_twice_raises(self):
        comp = FakeCompletions(["bad1", "bad2"])
        with mock.patch.dict(os.environ, {"MOCK_META": "false", "META_API_KEY": "test"}), \
                mock.patch("openai.OpenAI", return_value=SimpleNamespace(
                    chat=SimpleNamespace(completions=comp))):
            with self.assertRaises(MetaAPIError):
                _coach(mock=False).ask("hello")


if __name__ == "__main__":
    unittest.main()
