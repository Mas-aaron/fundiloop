"""Meta TTS provider (TTS_PROVIDER=meta): cloud speech with browser fallback.

Covers the Meta engine behind POST /api/voice/speak. Every factory and
endpoint test pins TTS_PROVIDER=meta so a kokoro-configured .env can
never leak in. Kokoro coverage lives in test_tts_kokoro.py.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_tts -v
"""
import io
import json
import os
import unittest
import wave
from unittest import mock


def fake_audio_urlopen_factory(body: bytes, content_type="audio/wav", error=None):
    seen = {}

    class FakeHeaders:
        def get_content_type(self):
            return content_type

    class FakeResp:
        headers = FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body

        def getheader(self, name, default=None):
            if name.lower() == "content-type":
                return content_type
            return default

    def fake_urlopen(req, timeout=None):
        seen["req"] = req
        seen["timeout"] = timeout
        if error is not None:
            raise error
        return FakeResp()

    return fake_urlopen, seen


class TTSProviderTests(unittest.TestCase):
    def test_tts_url_default_and_override(self):
        from backend.app.tts import tts_url

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("META_TTS_URL", None)
            os.environ.pop("META_API_BASE_URL", None)
            self.assertEqual(tts_url(), "https://api.meta.ai/v1/tts/synthesize")
        with mock.patch.dict(os.environ, {"META_TTS_URL": "https://x.test/tts"}):
            self.assertEqual(tts_url(), "https://x.test/tts")

    def test_voice_and_model_from_env_with_defaults(self):
        from backend.app import tts as tts_mod

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TTS_MODEL", None)
            os.environ.pop("META_TTS_VOICE", None)
            self.assertEqual(tts_mod.tts_model(), tts_mod.DEFAULT_TTS_MODEL)
            self.assertEqual(tts_mod.tts_voice(), tts_mod.DEFAULT_TTS_VOICE)
        with mock.patch.dict(os.environ, {"TTS_MODEL": "m", "META_TTS_VOICE": "v"}):
            self.assertEqual(tts_mod.tts_model(), "m")
            self.assertEqual(tts_mod.tts_voice(), "v")

    def test_mock_returns_valid_wav(self):
        from backend.app.tts import get_provider

        with mock.patch.dict(os.environ, {"MOCK_META": "true",
                                          "TTS_PROVIDER": "meta"}):
            audio = get_provider().synthesize("hello there")
        self.assertTrue(audio.startswith(b"RIFF"))
        with wave.open(io.BytesIO(audio), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertGreater(wf.getnframes(), 0)

    def test_mock_speakable_text_unchanged(self):
        from backend.app.tts import get_provider

        with mock.patch.dict(os.environ, {"MOCK_META": "true",
                                          "TTS_PROVIDER": "meta"}):
            a = get_provider().synthesize("hello")
            b = get_provider().synthesize("hello", voice="other-voice")
        # Mock voice is a no-op: same silence either way, never an error.
        self.assertEqual(a, b)

    def test_mock_with_mock_meta_false_raises(self):
        from backend.app.adapters import MetaAPIError
        from backend.app.tts import get_provider

        with mock.patch.dict(os.environ, {"MOCK_META": "false",
                                          "TTS_PROVIDER": "meta"}):
            with self.assertRaises(MetaAPIError):
                get_provider().synthesize("hi")

    def test_real_without_key_raises(self):
        from backend.app.adapters import MetaAPIError
        from backend.app.tts import MetaTTSProvider

        with mock.patch.dict(os.environ, {"MOCK_META": "false", "META_API_KEY": ""}):
            with self.assertRaises(MetaAPIError):
                MetaTTSProvider(mock=False).synthesize("hi")

    def test_real_empty_text_raises(self):
        from backend.app.adapters import MetaAPIError
        from backend.app.tts import MetaTTSProvider

        with mock.patch.dict(os.environ, {"MOCK_META": "false", "META_API_KEY": "k"}):
            with self.assertRaises(MetaAPIError):
                MetaTTSProvider(mock=False).synthesize("   ")

    def test_real_posts_json_and_returns_audio_bytes(self):
        from backend.app.tts import MetaTTSProvider

        fake, seen = fake_audio_urlopen_factory(b"RIFF-fake-audio", "audio/wav")
        with mock.patch.dict(os.environ, {"MOCK_META": "false", "META_API_KEY": "k",
                                          "TTS_MODEL": "tm", "META_TTS_VOICE": "tv"}):
            with mock.patch("urllib.request.urlopen", fake):
                out = MetaTTSProvider(mock=False).synthesize("hello")
        self.assertEqual(out, b"RIFF-fake-audio")
        req = seen["req"]
        self.assertIn("Bearer", req.get_header("Authorization"))
        self.assertIn("application/json", req.get_header("Content-type"))
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["model"], "tm")
        self.assertEqual(payload["voice"], "tv")
        self.assertEqual(payload["text"], "hello")

    def test_real_explicit_voice_overrides_env(self):
        from backend.app.tts import MetaTTSProvider

        fake, seen = fake_audio_urlopen_factory(b"RIFF-x", "audio/wav")
        with mock.patch.dict(os.environ, {"MOCK_META": "false", "META_API_KEY": "k",
                                          "META_TTS_VOICE": "env-voice"}):
            with mock.patch("urllib.request.urlopen", fake):
                MetaTTSProvider(mock=False).synthesize("hi", voice="call-voice")
        payload = json.loads(seen["req"].data.decode("utf-8"))
        self.assertEqual(payload["voice"], "call-voice")

    def test_real_decodes_base64_json(self):
        import base64

        from backend.app.tts import MetaTTSProvider

        body = json.dumps({"audio_base64": base64.b64encode(b"RIFF-b64").decode("ascii")})
        fake, _ = fake_audio_urlopen_factory(body.encode("utf-8"), "application/json")
        with mock.patch.dict(os.environ, {"MOCK_META": "false", "META_API_KEY": "k"}):
            with mock.patch("urllib.request.urlopen", fake):
                out = MetaTTSProvider(mock=False).synthesize("hi")
        self.assertEqual(out, b"RIFF-b64")

    def test_real_error_raises(self):
        from backend.app.adapters import MetaAPIError
        from backend.app.tts import MetaTTSProvider

        fake, _ = fake_audio_urlopen_factory(b"", error=OSError("net down"))
        with mock.patch.dict(os.environ, {"MOCK_META": "false", "META_API_KEY": "k"}):
            with mock.patch("urllib.request.urlopen", fake):
                with self.assertRaises(MetaAPIError):
                    MetaTTSProvider(mock=False).synthesize("hi")

    def test_real_unparseable_json_raises(self):
        from backend.app.adapters import MetaAPIError
        from backend.app.tts import MetaTTSProvider

        fake, _ = fake_audio_urlopen_factory(b'{"no_audio": true}', "application/json")
        with mock.patch.dict(os.environ, {"MOCK_META": "false", "META_API_KEY": "k"}):
            with mock.patch("urllib.request.urlopen", fake):
                with self.assertRaises(MetaAPIError):
                    MetaTTSProvider(mock=False).synthesize("hi")


class VoiceSpeakEndpointTests(unittest.TestCase):
    def test_mock_returns_audio_response(self):
        import backend.app.main as app_main

        with mock.patch.dict(os.environ, {"MOCK_META": "true",
                                          "TTS_PROVIDER": "meta"}):
            resp = app_main.api_voice_speak({"text": "hello"})
        self.assertEqual(resp.media_type, "audio/wav")
        self.assertTrue(bytes(resp.body).startswith(b"RIFF"))

    def test_empty_text_is_400(self):
        import backend.app.main as app_main
        from fastapi import HTTPException

        with mock.patch.dict(os.environ, {"MOCK_META": "true"}):
            with self.assertRaises(HTTPException) as ctx:
                app_main.api_voice_speak({"text": "   "})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_real_without_key_is_502(self):
        import backend.app.main as app_main
        from fastapi import HTTPException

        with mock.patch.dict(os.environ, {"MOCK_META": "false", "META_API_KEY": "",
                                          "TTS_PROVIDER": "meta"}):
            with self.assertRaises(HTTPException) as ctx:
                app_main.api_voice_speak({"text": "hi"})
        self.assertEqual(ctx.exception.status_code, 502)

    def test_status_includes_tts_config(self):
        import backend.app.main as app_main

        with mock.patch.dict(os.environ, {"MOCK_META": "true", "META_TTS_VOICE": "tv",
                                          "TTS_MODEL": "tm"}):
            view = app_main.api_status()
        cfg = view["config"]
        self.assertEqual(cfg["tts_mode"], "mock")
        self.assertEqual(cfg["tts_voice"], "tv")
        self.assertEqual(cfg["tts_model"], "tm")


if __name__ == "__main__":
    unittest.main()
