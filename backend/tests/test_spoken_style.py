"""Spoken style: model replies read aloud stay short and speakable.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_spoken_style -v
"""
import unittest

from backend.app.adapters.muse_adapter import SYSTEM_PROMPT
from backend.app.prompts import (
    DESCRIBE_SYSTEM,
    LIVE_SYSTEM,
    fundi_chapters_system,
    learning_hint_prompt,
)


class SpokenStyleTest(unittest.TestCase):
    def test_spoken_replies_capped_and_plain(self):
        for prompt in (SYSTEM_PROMPT, DESCRIBE_SYSTEM, LIVE_SYSTEM):
            self.assertIn("ALL-CAPS", prompt)
        self.assertIn("under 25 words", SYSTEM_PROMPT)
        self.assertIn("under 40 words", DESCRIBE_SYSTEM)
        self.assertIn("under 30 words", LIVE_SYSTEM)

    def test_fundi_chapters_stay_brief_and_speakable(self):
        prompt = fundi_chapters_system("arduino_uno")
        self.assertIn("under 50 words", prompt)
        self.assertIn("never spoken", prompt)  # title is visual-only
        self.assertIn("no ALL-CAPS words", prompt)
        self.assertIn("sensor test decides", prompt)

    def test_hint_prompt_caps_length(self):
        text = learning_hint_prompt(
            {"n": 1, "instruction": "wire it"},
            {"test": "CHECK_GPIO", "measured": "GPIO13 did not toggle"})
        self.assertIn("under 25 words", text)

    def test_voice_speak_mock_returns_audio_and_route_registered(self):
        # The old 501 contract applied when no TTS provider existed; Meta
        # Voice TTS is implemented now, so mock mode returns audio and real
        # failures surface as 502 (covered in test_tts.py).
        import os
        from unittest import mock

        import backend.app.main as app_main

        with mock.patch.dict(os.environ, {"MOCK_META": "true",
                                          "TTS_PROVIDER": "meta"}):
            resp = app_main.api_voice_speak({"text": "hi"})
        self.assertEqual(resp.media_type, "audio/wav")
        self.assertTrue(bytes(resp.body).startswith(b"RIFF"))
        routes = [(r.path, tuple(sorted(getattr(r, "methods", None) or [])))
                  for r in app_main.app.routes if hasattr(r, "path")]
        self.assertIn(("/api/voice/speak", ("POST",)), routes)


if __name__ == "__main__":
    unittest.main()
