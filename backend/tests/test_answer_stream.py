"""Token-streaming answers: deltas in order, fallback, SSE framing.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_answer_stream -v
"""
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.app.adapters import MetaAPIError
from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.models import ProbeReading
from backend.tests.helpers import use_mock_env


def chunk(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


class FakeChunkStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._chunks:
            raise StopIteration
        item = self._chunks.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class FakeRawFull:
    def __init__(self, text):
        self.status_code = 200
        self.headers = {}
        self._text = text

    def parse(self):
        return SimpleNamespace(
            id="resp-fb",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self._text))])


class FakeStreamCompletions:
    """chat.completions: scripted chunk stream + full-text fallback path."""

    def __init__(self, chunks=(), create_error=None,
                 fallback_text="fallback full"):
        self._chunks = list(chunks)
        self.create_error = create_error
        self.fallback_text = fallback_text
        self.calls = []
        self.stream = None
        self.with_raw_response = self._Raw(self)

    class _Raw:
        def __init__(self, outer):
            self._outer = outer

        def create(self, **kwargs):
            self._outer.calls.append(kwargs)
            return FakeRawFull(self._outer.fallback_text)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        self.stream = FakeChunkStream(self._chunks)
        return self.stream


def stream_client(tc, **kwargs):
    comp = FakeStreamCompletions(**kwargs)
    patcher = mock.patch(
        "openai.OpenAI",
        return_value=SimpleNamespace(
            chat=SimpleNamespace(completions=comp)))
    patcher.start()
    tc.addCleanup(patcher.stop)
    return comp


class CompleteTextStreamTest(unittest.TestCase):
    def test_streams_deltas_in_order_skipping_empty(self):
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}):
            comp = stream_client(self, chunks=[
                chunk("Wire "), chunk(None), chunk("it up."),
                SimpleNamespace(choices=[])])
            out = list(MetaClaudeAdapter(model="m", mock=False)
                       .complete_text_stream("sys", "user"))
        self.assertEqual(out, ["Wire ", "it up."])
        self.assertTrue(comp.calls[0]["stream"])
        self.assertNotIn("response_format", comp.calls[0])
        self.assertTrue(comp.stream.closed)

    def test_falls_back_to_full_completion_when_stream_wont_start(self):
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}):
            comp = stream_client(
                self, create_error=RuntimeError("no stream"),
                fallback_text="full answer")
            out = list(MetaClaudeAdapter(model="m", mock=False)
                       .complete_text_stream("sys", "user"))
        self.assertEqual(out, ["full answer"])
        self.assertEqual(len(comp.calls), 2)  # stream attempt + fallback
        self.assertNotIn("response_format", comp.calls[1])  # prose fallback

    def test_mid_stream_failure_raises_and_keeps_partial(self):
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}):
            stream_client(self, chunks=[chunk("part-ok"),
                                        RuntimeError("cut")])
            gen = MetaClaudeAdapter(model="m", mock=False).complete_text_stream(
                "sys", "user")
            self.assertEqual(next(gen), "part-ok")
            with self.assertRaises(MetaAPIError):
                list(gen)

    def test_mock_yields_braces_once(self):
        use_mock_env(self)
        out = list(MetaClaudeAdapter(model="m").complete_text_stream("s", "u"))
        self.assertEqual(out, ["{}"])

    def test_missing_key_raises(self):
        with mock.patch("backend.app.adapters.muse_adapter.api_key",
                        return_value=""):
            gen = MetaClaudeAdapter(model="m", mock=False).complete_text_stream(
                "s", "u")
            with self.assertRaises(MetaAPIError):
                list(gen)


class StubBrain:
    def __init__(self, chunks=None, error=None):
        self._chunks = list(chunks or [])
        self.error = error
        self.prompts = []

    def complete_text_stream(self, system, user):
        self.prompts.append(user)
        if self.error is not None:
            raise self.error
        return iter(self._chunks)


class StubProbe:
    def __init__(self, error=None):
        self.error = error

    def read_all(self, fault):
        if self.error is not None:
            raise self.error
        return ProbeReading(connected=True, i2c_devices=["0x76"])


def run_events(question_chunks, probe_error=None, brain_error=None):
    import backend.app.main as app_main

    guide = SimpleNamespace(
        Muse=StubBrain(question_chunks, brain_error),
        probe=StubProbe(probe_error))
    with mock.patch.object(app_main, "live_guide", guide):
        frames = list(app_main.iter_answer_events(
            "why is the LED dark?", step_text="wire the LED"))
    events = []
    for frame in frames:
        name, data = "", ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        events.append((name, json.loads(data)))
    return events, guide


class AnswerEventsTest(unittest.TestCase):
    def test_tokens_stream_then_done_carries_full_text(self):
        events, guide = run_events(["Wire ", "it up."])
        self.assertEqual([n for n, _ in events],
                         ["ack", "token", "token", "done"])
        self.assertEqual(events[1][1], {"text": "Wire "})
        self.assertEqual(events[3][1], {"text": "Wire it up."})
        self.assertIn("why is the LED dark?", guide.Muse.prompts[0])
        self.assertIn("0x76", guide.Muse.prompts[0])  # probe-grounded

    def test_leading_whitespace_held_until_prose_decides(self):
        events, _ = run_events(["  ", "Hi there."])
        self.assertEqual([n for n, _ in events], ["ack", "token", "done"])
        self.assertEqual(events[1][1], {"text": "  Hi there."})

    def test_json_reply_drained_and_emitted_cleaned_once(self):
        events, _ = run_events(['{"speak": "Seat the sensor."}'])
        self.assertEqual([n for n, _ in events], ["ack", "token", "done"])
        self.assertEqual(events[1][1], {"text": "Seat the sensor."})
        self.assertEqual(events[2][1], {"text": "Seat the sensor."})

    def test_model_error_yields_error_event(self):
        events, _ = run_events([], brain_error=MetaAPIError("boom"))
        self.assertEqual([n for n, _ in events], ["ack", "error"])
        self.assertIn("boom", events[1][1]["detail"])

    def test_probe_failure_yields_error_event(self):
        events, _ = run_events([], probe_error=RuntimeError("no port"))
        self.assertEqual([n for n, _ in events], ["ack", "error"])
        self.assertIn("probe read failed", events[1][1]["detail"])

    def test_endpoint_registered(self):
        import backend.app.main as app_main

        routes = [(r.path, tuple(sorted(getattr(r, "methods", None) or [])))
                  for r in app_main.app.routes if hasattr(r, "path")]
        self.assertIn(("/api/answer/stream", ("POST",)), routes)


if __name__ == "__main__":
    unittest.main()
