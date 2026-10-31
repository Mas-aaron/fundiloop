"""Kokoro TTS provider: local neural speech behind the same seam.

TTS_PROVIDER=kokoro selects KokoroTTSProvider (af_heart default); anything
else (default) keeps MetaTTSProvider. The kokoro_onnx package is stubbed
here, so these tests need no weights, no pip install, and no network.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_tts_kokoro -v
"""
import io
import os
import sys
import unittest
import wave
from unittest import mock


class FakeKokoro:
    instances = []

    def __init__(self, model_path, voices_path):
        self.model_path = model_path
        self.voices_path = voices_path
        self.calls = []
        FakeKokoro.instances.append(self)

    def create(self, text, voice="af_heart", speed=1.0, lang="en-us"):
        self.calls.append({"text": text, "voice": voice, "speed": speed,
                           "lang": lang})
        n = max(240, len(text) * 24)
        return [0.5 * ((i % 100) / 100.0) - 0.25 for i in range(n)], 24000


_MISSING = object()


def install_fake_kokoro(testcase):
    # Surgical save/restore, NOT mock.patch.dict(sys.modules): patch.dict
    # evicts every module imported inside its body on exit — including
    # numpy (first imported by synthesize) — which permanently breaks
    # native re-import with "cannot load module more than once".
    FakeKokoro.instances = []
    module = type(sys)("kokoro_onnx")
    module.Kokoro = FakeKokoro
    saved = sys.modules.get("kokoro_onnx", _MISSING)
    sys.modules["kokoro_onnx"] = module

    def restore():
        if saved is _MISSING:
            sys.modules.pop("kokoro_onnx", None)
        else:
            sys.modules["kokoro_onnx"] = saved

    testcase.addCleanup(restore)
    return module


class KokoroFactoryTests(unittest.TestCase):
    def test_default_provider_is_meta(self):
        from backend.app.tts import MetaTTSProvider, get_provider

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TTS_PROVIDER", None)
            self.assertIsInstance(get_provider(), MetaTTSProvider)

    def test_kokoro_selected_by_env(self):
        from backend.app.tts import KokoroTTSProvider, get_provider

        with mock.patch.dict(os.environ, {"TTS_PROVIDER": "kokoro"}):
            self.assertIsInstance(get_provider(), KokoroTTSProvider)
        with mock.patch.dict(os.environ, {"TTS_PROVIDER": "Kokoro"}):
            self.assertIsInstance(get_provider(), KokoroTTSProvider)

    def test_unknown_provider_raises(self):
        from backend.app.adapters import MetaAPIError
        from backend.app.tts import get_provider

        with mock.patch.dict(os.environ, {"TTS_PROVIDER": "elevenlabs"}):
            with self.assertRaises(MetaAPIError):
                get_provider()

    def test_kokoro_voice_default_and_override(self):
        from backend.app import tts as tts_mod

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KOKORO_VOICE", None)
            self.assertEqual(tts_mod.kokoro_voice(), "af_heart")
        with mock.patch.dict(os.environ, {"KOKORO_VOICE": "af_bella"}):
            self.assertEqual(tts_mod.kokoro_voice(), "af_bella")

    def test_kokoro_model_dir_default_and_override(self):
        from backend.app.tts import kokoro_model_dir

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KOKORO_MODEL_DIR", None)
            self.assertTrue(kokoro_model_dir().replace("\\", "/").endswith(
                "backend/data/kokoro"))
        with mock.patch.dict(os.environ, {"KOKORO_MODEL_DIR": "C:/x/models"}):
            self.assertEqual(kokoro_model_dir(), "C:/x/models")


class KokoroSynthesizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_mock_returns_valid_wav(self):
        from backend.app.tts import KokoroTTSProvider

        with mock.patch.dict(os.environ, {"MOCK_META": "true"}):
            audio = KokoroTTSProvider(mock=True).synthesize("hello there")
        self.assertTrue(audio.startswith(b"RIFF"))
        with wave.open(io.BytesIO(audio), "rb") as wf:
            self.assertEqual((wf.getnchannels(), wf.getsampwidth()), (1, 2))
            self.assertGreater(wf.getnframes(), 0)

    def test_mock_with_mock_meta_false_raises(self):
        from backend.app.adapters import MetaAPIError
        from backend.app.tts import KokoroTTSProvider

        with mock.patch.dict(os.environ, {"MOCK_META": "false"}):
            with self.assertRaises(MetaAPIError):
                KokoroTTSProvider(mock=True).synthesize("hi")

    def test_empty_text_raises(self):
        from backend.app.adapters import MetaAPIError
        from backend.app.tts import KokoroTTSProvider

        with mock.patch.dict(os.environ, {"MOCK_META": "false"}):
            with self.assertRaises(MetaAPIError):
                KokoroTTSProvider(mock=False).synthesize("   ")

    def test_missing_package_raises_with_install_hint(self):
        from backend.app.adapters import MetaAPIError
        from backend.app.tts import KokoroTTSProvider

        with mock.patch.dict(os.environ, {"MOCK_META": "false",
                                          "KOKORO_MODEL_DIR": self.tmp.name}):
            with mock.patch.dict(sys.modules, {"kokoro_onnx": None}):
                with self.assertRaises(MetaAPIError) as ctx:
                    KokoroTTSProvider(mock=False).synthesize("hi")
        self.assertIn("kokoro-onnx", str(ctx.exception))

    def test_missing_model_files_raise_with_dir(self):
        from backend.app.adapters import MetaAPIError
        from backend.app.tts import KokoroTTSProvider

        install_fake_kokoro(self)
        with mock.patch.dict(os.environ, {"MOCK_META": "false",
                                          "KOKORO_MODEL_DIR": self.tmp.name}):
            with self.assertRaises(MetaAPIError) as ctx:
                KokoroTTSProvider(mock=False).synthesize("hi")
        self.assertIn(self.tmp.name, str(ctx.exception))

    def test_happy_path_returns_spoken_wav(self):
        from backend.app.tts import KokoroTTSProvider

        install_fake_kokoro(self)
        for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            with open(os.path.join(self.tmp.name, name), "wb") as fh:
                fh.write(b"fake")
        with mock.patch.dict(os.environ, {"MOCK_META": "false",
                                          "KOKORO_MODEL_DIR": self.tmp.name,
                                          "KOKORO_VOICE": "af_heart"}):
            audio = KokoroTTSProvider(mock=False).synthesize("hello bench")
        self.assertTrue(audio.startswith(b"RIFF"))
        with wave.open(io.BytesIO(audio), "rb") as wf:
            self.assertEqual((wf.getnchannels(), wf.getsampwidth(),
                              wf.getframerate()), (1, 2, 24000))
            frames = wf.readframes(wf.getnframes())
        self.assertTrue(any(frames))  # not silence
        inst = FakeKokoro.instances[-1]
        self.assertEqual(inst.calls[0]["voice"], "af_heart")
        self.assertEqual(inst.calls[0]["lang"], "en-us")
        self.assertEqual(inst.calls[0]["text"], "hello bench")
        self.assertTrue(inst.model_path.endswith("kokoro-v1.0.onnx"))
        self.assertTrue(inst.voices_path.endswith("voices-v1.0.bin"))

    def test_explicit_voice_overrides_env(self):
        from backend.app.tts import KokoroTTSProvider

        install_fake_kokoro(self)
        for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            with open(os.path.join(self.tmp.name, name), "wb") as fh:
                fh.write(b"fake")
        with mock.patch.dict(os.environ, {"MOCK_META": "false",
                                          "KOKORO_MODEL_DIR": self.tmp.name,
                                          "KOKORO_VOICE": "af_heart"}):
            KokoroTTSProvider(mock=False).synthesize("hi", voice="af_bella")
        self.assertEqual(FakeKokoro.instances[-1].calls[0]["voice"], "af_bella")

    def test_numpy_array_samples_convert(self):
        import numpy as np

        from backend.app.tts import _samples_to_wav

        audio = _samples_to_wav(np.array([0.5, -0.5], dtype=np.float32), 24000)
        with wave.open(io.BytesIO(audio), "rb") as wf:
            self.assertEqual(wf.getnframes(), 2)

    def test_out_of_range_samples_clamp(self):
        from backend.app.tts import KokoroTTSProvider, _samples_to_wav

        audio = _samples_to_wav([2.0, -2.0, 0.0], 24000)
        with wave.open(io.BytesIO(audio), "rb") as wf:
            import struct

            vals = struct.unpack("<3h", wf.readframes(3))
        self.assertEqual(vals, (32767, -32768, 0))
        self.assertTrue(KokoroTTSProvider)  # provider import stays covered

    def test_fast_and_slow_conversion_agree(self):
        import struct

        import numpy as np

        from backend.app.tts import _samples_to_wav

        samples = [0.5, -0.5, 2.0, -2.0, 0.0, 0.1, -0.1]
        # Hand-derived: clamp, asymmetric scale, truncate toward zero.
        want = (16383, -16384, 32767, -32768, 0, 3276, -3276)
        for feed in (samples, np.array(samples, dtype=np.float32)):
            audio = _samples_to_wav(feed, 24000)
            with wave.open(io.BytesIO(audio), "rb") as wf:
                vals = struct.unpack("<7h", wf.readframes(7))
            self.assertEqual(vals, want, type(feed))

    def test_bad_sample_falls_back_to_silence(self):
        import struct

        from backend.app.tts import _samples_to_wav

        audio = _samples_to_wav(["x", 0.5], 24000)
        with wave.open(io.BytesIO(audio), "rb") as wf:
            vals = struct.unpack("<2h", wf.readframes(2))
        self.assertEqual(vals, (0, 16383))

    def test_repeat_synthesis_served_from_cache(self):
        from backend.app.tts import KokoroTTSProvider

        install_fake_kokoro(self)
        for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            with open(os.path.join(self.tmp.name, name), "wb") as fh:
                fh.write(b"fake")
        with mock.patch.dict(os.environ, {"MOCK_META": "false",
                                          "KOKORO_MODEL_DIR": self.tmp.name}):
            first = KokoroTTSProvider(mock=False).synthesize("cache me twice")
            second = KokoroTTSProvider(mock=False).synthesize("cache me twice")
        self.assertEqual(first, second)
        self.assertEqual(len(FakeKokoro.instances[-1].calls), 1)

    def test_warmup_preloads_engine_and_cache(self):
        from backend.app import tts as tts_mod

        install_fake_kokoro(self)
        for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            with open(os.path.join(self.tmp.name, name), "wb") as fh:
                fh.write(b"fake")
        with mock.patch.dict(os.environ, {"MOCK_META": "false",
                                          "TTS_PROVIDER": "kokoro",
                                          "KOKORO_MODEL_DIR": self.tmp.name}):
            self.assertTrue(tts_mod.warmup_kokoro())
            key = (os.path.join(self.tmp.name, "kokoro-v1.0.onnx"),
                   os.path.join(self.tmp.name, "voices-v1.0.bin"))
            self.assertIn(key, tts_mod._kokoro_engines)
            calls = len(FakeKokoro.instances[-1].calls)
            tts_mod.KokoroTTSProvider(mock=False).synthesize(
                tts_mod.WARMUP_TEXTS[0])
            self.assertEqual(len(FakeKokoro.instances[-1].calls), calls)

    def test_warmup_noop_in_mock_mode(self):
        from backend.app import tts as tts_mod

        with mock.patch.dict(os.environ, {"MOCK_META": "true",
                                          "TTS_PROVIDER": "kokoro"}):
            self.assertFalse(tts_mod.warmup_kokoro())

    def test_warmup_failure_returns_false(self):
        from backend.app import tts as tts_mod

        install_fake_kokoro(self)
        with mock.patch.dict(os.environ, {"MOCK_META": "false",
                                          "TTS_PROVIDER": "kokoro",
                                          "KOKORO_MODEL_DIR": self.tmp.name}):
            self.assertFalse(tts_mod.warmup_kokoro())


class KokoroEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_speak_endpoint_serves_kokoro_audio(self):
        import backend.app.main as app_main

        install_fake_kokoro(self)
        for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            with open(os.path.join(self.tmp.name, name), "wb") as fh:
                fh.write(b"fake")
        with mock.patch.dict(os.environ, {"MOCK_META": "false",
                                          "TTS_PROVIDER": "kokoro",
                                          "KOKORO_MODEL_DIR": self.tmp.name}):
            resp = app_main.api_voice_speak({"text": "hello"})
        self.assertEqual(resp.media_type, "audio/wav")
        self.assertTrue(bytes(resp.body).startswith(b"RIFF"))

    def test_unknown_provider_is_502(self):
        import backend.app.main as app_main
        from fastapi import HTTPException

        with mock.patch.dict(os.environ, {"MOCK_META": "false",
                                          "TTS_PROVIDER": "nope"}):
            with self.assertRaises(HTTPException) as ctx:
                app_main.api_voice_speak({"text": "hi"})
        self.assertEqual(ctx.exception.status_code, 502)

    def test_status_reports_provider(self):
        import backend.app.main as app_main

        with mock.patch.dict(os.environ, {"TTS_PROVIDER": "kokoro"}):
            view = app_main.api_status()
        self.assertEqual(view["config"]["tts_provider"], "kokoro")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TTS_PROVIDER", None)
            view = app_main.api_status()
        self.assertEqual(view["config"]["tts_provider"], "meta")


if __name__ == "__main__":
    unittest.main()
