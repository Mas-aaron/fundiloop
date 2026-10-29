"""Spoken style: model replies read aloud stay short and speakable.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_spoken_style -v
"""
import unittest

from backend.app.adapters.muse_adapter import SYSTEM_PROMPT
from backend.app.prompts import DESCRIBE_SYSTEM, LIVE_SYSTEM, learning_hint_prompt


class SpokenStyleTest(unittest.TestCase):
    def test_spoken_replies_capped_and_plain(self):
        for prompt in (SYSTEM_PROMPT, DESCRIBE_SYSTEM, LIVE_SYSTEM):
            self.assertIn("ALL-CAPS", prompt)
        self.assertIn("under 25 words", SYSTEM_PROMPT)
        self.assertIn("under 40 words", DESCRIBE_SYSTEM)
        self.assertIn("under 30 words", LIVE_SYSTEM)

    def test_hint_prompt_caps_length(self):
        text = learning_hint_prompt(
            {"n": 1, "instruction": "wire it"},
            {"test": "CHECK_GPIO", "measured": "GPIO13 did not toggle"})
        self.assertIn("under 25 words", text)


if __name__ == "__main__":
    unittest.main()
