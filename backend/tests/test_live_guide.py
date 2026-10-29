"""LiveGuide turns: probe truth + SAM tracks + coach text, spoken back.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_live_guide -v

No hardware, no network: mock adapters or scripted stubs.
"""
import base64
import unittest

from backend.app.adapters import MetaAPIError
from backend.app.live import MOCK_GUIDANCE, LiveGuide
from backend.app.serial_probe import SerialProbe
from backend.tests.helpers import use_mock_env


class StubSAM:
    mock = False

    def __init__(self, tracks):
        self.tracks = tracks
        self.calls = []

    def analyze_clip(self, clip, context=None, mime=None, **kwargs):
        self.calls.append({"clip": clip, "mime": mime})
        return dict(self.tracks)


class StubBrain:
    mock = False

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete_text(self, system, user, image_b64=None, video_b64=None,
                      video_mime=None, **kwargs):
        self.calls.append({"system": system, "user": user,
                           "video_b64": video_b64, "video_mime": video_mime})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class StubVoice:
    mock = False

    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)
        return {"text": text}


TRACKS = {"media": "clip.mp4", "concept": "c", "track_count": 1,
          "frame_count": 4, "mock": False, "notes": "n",
          "tracks": [{"object_id": "0", "frames_seen": 4,
                      "frames": [{"box": [1, 2, 3, 4]}]}]}


def make_guide(tc, sam, brain, voice):
    use_mock_env(tc)
    return LiveGuide(sam, brain, voice, SerialProbe(mock=True))


class LiveGuideTest(unittest.TestCase):
    def test_mock_guide_is_honest_and_grounded(self):
        use_mock_env(self)
        guide = LiveGuide()
        out = guide.guide(transcript="what next")
        self.assertEqual(out["speak"], MOCK_GUIDANCE)
        self.assertEqual(out["transcript"], "what next")
        self.assertTrue(out["mock"])
        self.assertIn("0x76", out["probe"]["i2c_devices"])
        self.assertIsNone(out["sam"])  # no clip, no tracks invented

    def test_live_turn_feeds_clip_tracks_and_probe_to_coach(self):
        sam, brain, voice = (StubSAM(TRACKS), StubBrain(["Seat the BME280."]),
                             StubVoice())
        guide = make_guide(self, sam, brain, voice)
        clip = b"fakeclip"
        out = guide.guide(clip=clip, transcript="where now",
                          step_text="Wire the BME280.")
        self.assertEqual(out["speak"], "Seat the BME280.")
        self.assertEqual(out["sam"]["track_count"], 1)
        self.assertEqual(sam.calls[0]["clip"], clip)
        call = brain.calls[0]
        self.assertEqual(call["video_b64"], base64.b64encode(clip).decode("ascii"))
        self.assertIn("where now", call["user"])
        self.assertIn("Wire the BME280.", call["user"])
        self.assertIn("0x76", call["user"])  # probe truth in the prompt
        self.assertEqual(voice.spoken, ["Seat the BME280."])

    def test_empty_guidance_falls_back_to_audible_prompt(self):
        guide = make_guide(self, StubSAM(TRACKS), StubBrain(["   "]),
                           StubVoice())
        out = guide.guide(clip=b"x")
        self.assertIn("Hold the board still", out["speak"])

    def test_coach_error_propagates_and_stops_turn(self):
        guide = make_guide(self, StubSAM(TRACKS),
                           StubBrain([MetaAPIError("down")]), StubVoice())
        with self.assertRaises(MetaAPIError):
            guide.guide(transcript="hi")


if __name__ == "__main__":
    unittest.main()
