"""Meta Voice TTS: server-side speech synthesis (primary mouth).

Every spoken utterance in the project goes through POST /api/voice/speak
first; the frontend falls back to default browser speechSynthesis for that
utterance only when the server path fails, then retries Meta next turn.

Config (never hardcoded callers): model from TTS_MODEL, voice from
META_TTS_VOICE (default warm female), endpoint from META_TTS_URL
(default META_API_BASE_URL + /tts/synthesize).

Mock mode returns a short valid WAV silence so offline demos exercise the
same play path. Real mode POSTs JSON {model, voice, text} and accepts
either raw audio bytes or JSON carrying base64 audio.

Honesty note: the public Meta Model API documents Muse Voice Transcribe
(speech-to-text) but no TTS model today, so the default endpoint and model
name are the expected shape, unverified. Real mode will 502 until Meta
ships TTS — and the per-utterance browser fallback keeps speech working.
"""
from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.request
import wave

from .adapters import (
    MetaAPIError,
    api_base_url,
    api_key,
    api_timeout,
    log_model_call,
    mock_meta_enabled,
)

DEFAULT_TTS_MODEL = "muse-voice-tts-1.0"
DEFAULT_TTS_VOICE = "warm-female"

_MOCK_RATE = 24000
_MOCK_SECONDS = 0.12


def tts_model() -> str:
    return os.environ.get("TTS_MODEL", DEFAULT_TTS_MODEL).strip() or DEFAULT_TTS_MODEL


def tts_voice() -> str:
    return os.environ.get("META_TTS_VOICE", DEFAULT_TTS_VOICE).strip() or DEFAULT_TTS_VOICE


def tts_url() -> str:
    override = os.environ.get("META_TTS_URL", "").strip()
    return override or api_base_url() + "/tts/synthesize"


def _mock_wav() -> bytes:
    """Fixed short silence: valid WAV the browser Audio element can play."""
    buf = io.BytesIO()
    frames = int(_MOCK_RATE * _MOCK_SECONDS)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_MOCK_RATE)
        wf.writeframes(b"\x00" * frames * 2)
    return buf.getvalue()


def _decode_audio_payload(body: bytes, content_type: str) -> bytes:
    if content_type.startswith("audio/"):
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise MetaAPIError("voice TTS returned unparseable audio: %s" % exc)
    if isinstance(payload, dict):
        for key in ("audio_base64", "base64", "audio", "data"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                try:
                    return base64.b64decode(val, validate=True)
                except (TypeError, ValueError) as exc:
                    raise MetaAPIError("voice TTS returned bad base64 audio: %s" % exc)
    raise MetaAPIError("voice TTS returned JSON without audio")


class MetaTTSProvider:
    """Speech synthesis with the project's standard mock/real split."""

    name = "meta-tts"

    def __init__(self, model: str | None = None, voice: str | None = None,
                 mock: bool | None = None) -> None:
        self.model = model or tts_model()
        self.voice = voice or tts_voice()
        self.mock = mock_meta_enabled() if mock is None else mock

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """Return WAV audio bytes for text. Raises MetaAPIError on failure."""
        clean = (text or "").strip()
        if not clean:
            raise MetaAPIError("voice TTS called with empty text")
        if self.mock:
            if not mock_meta_enabled():
                raise MetaAPIError("mock TTS provider called with MOCK_META=false")
            return _mock_wav()
        if not api_key():
            raise MetaAPIError("META_API_KEY is not set")
        payload = json.dumps({
            "model": self.model,
            "voice": voice or self.voice,
            "text": clean,
        }).encode("utf-8")
        req = urllib.request.Request(
            tts_url(), data=payload,
            headers={"Authorization": "Bearer %s" % api_key(),
                     "Content-Type": "application/json"},
            method="POST")
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=api_timeout()) as resp:
                body = resp.read()
                ctype = ""
                getheader = getattr(resp, "getheader", None)
                if callable(getheader):
                    ctype = (getheader("Content-Type", "") or "").split(";")[0].strip()
                elif getattr(resp, "headers", None) is not None:
                    get_type = getattr(resp.headers, "get_content_type", None)
                    if callable(get_type):
                        ctype = get_type()
                audio = _decode_audio_payload(body, ctype)
        except MetaAPIError:
            raise
        except Exception as exc:
            hint = ""
            if getattr(exc, "code", None) == 404:
                hint = (" (HTTP 404: unknown model or path — TTS_MODEL=%r, "
                        "want %r)" % (self.model, DEFAULT_TTS_MODEL))
            raise MetaAPIError("voice TTS failed: %s%s" % (exc, hint)) from exc
        latency_ms = (time.monotonic() - started) * 1000
        log_model_call(self.model, latency_ms, 200, "", "", raw_summary=clean)
        return audio


# Backwards-compatible name: the original seam exposed TTSProvider.
TTSProvider = MetaTTSProvider


def get_provider() -> MetaTTSProvider:
    return MetaTTSProvider()
