"""Server-side speech synthesis (primary mouth): Meta Voice or Kokoro TTS.

Every spoken utterance in the project goes through POST /api/voice/speak
first; the frontend falls back to default browser speechSynthesis for that
utterance only when the server path fails, then retries the server next
turn. TTS_PROVIDER picks the engine: "meta" (default) or "kokoro".

Meta config (never hardcoded callers): model from TTS_MODEL, voice from
META_TTS_VOICE (default warm female), endpoint from META_TTS_URL
(default META_API_BASE_URL + /tts/synthesize). Real mode POSTs JSON
{model, voice, text} and accepts either raw audio bytes or JSON carrying
base64 audio.

Kokoro config: voice from KOKORO_VOICE (default af_heart, American
English), model files from KOKORO_MODEL_DIR (default
backend/data/kokoro). Real mode synthesizes locally via kokoro-onnx —
no key, no network, fully offline once installed.

Mock mode returns a short valid WAV silence so offline demos exercise the
same play path.

Honesty note: the Meta Model API lists Transcribe (speech-to-text) but
no TTS model (verified live against /v1/models), so Meta real mode 502s
until Meta ships TTS — and the per-utterance browser fallback keeps
speech working. Kokoro is the working neural voice today.
"""
from __future__ import annotations

import base64
import io
import json
import os
import struct
import threading
import time
import urllib.request
import wave
from pathlib import Path

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


# -- Kokoro (local neural TTS) -------------------------------------------------
KOKORO_DEFAULT_VOICE = "af_heart"
KOKORO_MODEL_FILE = "kokoro-v1.0.onnx"
KOKORO_VOICES_FILE = "voices-v1.0.bin"
KOKORO_MODEL_URL = ("https://github.com/thewh1teagle/kokoro-onnx/releases/"
                    "download/model-files-v1.1/kokoro-v1.0.onnx")
KOKORO_VOICES_URL = ("https://github.com/thewh1teagle/kokoro-onnx/releases/"
                     "download/model-files-v1.1/voices-v1.0.bin")

_kokoro_lock = threading.Lock()
_kokoro_engines: dict = {}
# Real-mode synthesis cache: short acks repeat every turn, and Kokoro
# is deterministic, so repeats skip synthesis entirely (~1.3s -> ~0ms).
# Keyed by model dir too, so tests with fake dirs stay isolated.
_kokoro_synth_cache: dict = {}
_KOKORO_CACHE_MAX = 32

# Every fixed ack the UI speaks, verbatim as sent (speakable() leaves
# these unchanged): warmed once at startup so each flow's first audio
# is a cache hit instead of a ~1s synthesis.
WARMUP_TEXTS = (
    "Analyzing.",
    "Planning your projects.",
    "Looking.",
    "Tap a card to start.",
    "Found your board.",
    "On it — thinking…",
)


def tts_provider() -> str:
    """Active speech engine: "meta" (default) or "kokoro"."""
    return os.environ.get("TTS_PROVIDER", "meta").strip().lower() or "meta"


def kokoro_voice() -> str:
    return (os.environ.get("KOKORO_VOICE", KOKORO_DEFAULT_VOICE).strip()
            or KOKORO_DEFAULT_VOICE)


def kokoro_model_dir() -> str:
    override = os.environ.get("KOKORO_MODEL_DIR", "").strip()
    if override:
        return override
    return str(Path(__file__).resolve().parent.parent / "data" / "kokoro")


def _frames_fast(samples):
    """Vectorized float->int16, or None when the slow loop must run.

    The slow loop keeps every legacy quirk (bad items -> 0.0, odd
    shapes, nan); the fast path only runs on clean 1-D float data,
    which is everything the real engine returns.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        arr = np.asarray(samples, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if arr.ndim != 1:
        return None
    if arr.size == 0:
        return np.empty(0, dtype="<i2")
    if np.isnan(arr).any():
        return None
    clipped = np.clip(arr, -1.0, 1.0)
    return np.where(clipped < 0, clipped * 32768,
                    clipped * 32767).astype("<i2")


def _frames_slow(samples) -> list:
    """Per-item conversion with legacy edge behavior."""
    try:
        items = list(samples)  # no truthiness: numpy arrays are ambiguous
    except TypeError:
        items = []
    frames = []
    for sample in items:
        try:
            value = float(sample)
        except (TypeError, ValueError):
            value = 0.0
        clamped = max(-1.0, min(1.0, value))
        scale = 32768 if clamped < 0 else 32767
        frames.append(int(clamped * scale))
    return frames


def _samples_to_wav(samples, sample_rate: int) -> bytes:
    """Mono float samples (-1..1, any sequence) to 16-bit WAV bytes."""
    if samples is None:
        raise MetaAPIError("kokoro returned no audio")
    frames = _frames_fast(samples)
    if frames is None:
        frames = _frames_slow(samples)
    if len(frames) == 0:
        raise MetaAPIError("kokoro returned no audio")
    try:
        rate = int(sample_rate)
    except (TypeError, ValueError):
        raise MetaAPIError("kokoro returned a bad sample rate: %r" % (sample_rate,))
    if rate <= 0:
        raise MetaAPIError("kokoro returned a bad sample rate: %r" % (sample_rate,))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        tobytes = getattr(frames, "tobytes", None)
        if callable(tobytes):
            wf.writeframes(tobytes())
        else:
            wf.writeframes(struct.pack("<%dh" % len(frames), *frames))
    return buf.getvalue()


def _kokoro_engine():
    """Load (once per model dir) the kokoro-onnx engine. Never partially built."""
    try:
        from kokoro_onnx import Kokoro
    except ImportError as exc:
        raise MetaAPIError(
            "kokoro-onnx is not installed: pip install -r "
            "backend/requirements-kokoro.txt (%s)" % exc) from exc
    directory = kokoro_model_dir()
    model_path = os.path.join(directory, KOKORO_MODEL_FILE)
    voices_path = os.path.join(directory, KOKORO_VOICES_FILE)
    missing = [p for p in (model_path, voices_path) if not os.path.isfile(p)]
    if missing:
        raise MetaAPIError(
            "kokoro model files missing in %s: %s — download %s and %s "
            "into that directory" % (directory, ", ".join(missing),
                                     KOKORO_MODEL_URL, KOKORO_VOICES_URL))
    key = (model_path, voices_path)
    with _kokoro_lock:
        engine = _kokoro_engines.get(key)
        if engine is None:
            try:
                engine = Kokoro(model_path, voices_path)
            except Exception as exc:
                raise MetaAPIError("kokoro failed to load: %s" % exc) from exc
            _kokoro_engines[key] = engine
        return engine


class KokoroTTSProvider:
    """Local neural speech via kokoro-onnx. Same mock/real split as Meta."""

    name = "kokoro-tts"

    def __init__(self, voice: str | None = None, mock: bool | None = None) -> None:
        self.voice = voice or kokoro_voice()
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
        use_voice = voice or self.voice
        key = (kokoro_model_dir(), use_voice, 1.0, "en-us", clean)
        with _kokoro_lock:
            hit = _kokoro_synth_cache.get(key)
        if hit is not None:
            return hit
        engine = _kokoro_engine()
        started = time.monotonic()
        try:
            samples, sample_rate = engine.create(
                clean, voice=use_voice, speed=1.0, lang="en-us")
            audio = _samples_to_wav(samples, sample_rate)
        except MetaAPIError:
            raise
        except Exception as exc:
            raise MetaAPIError("kokoro synthesis failed: %s" % exc) from exc
        latency_ms = (time.monotonic() - started) * 1000
        log_model_call("kokoro-tts", latency_ms, 200, "", "", raw_summary=clean)
        with _kokoro_lock:
            if len(_kokoro_synth_cache) >= _KOKORO_CACHE_MAX:
                _kokoro_synth_cache.pop(next(iter(_kokoro_synth_cache)))
            _kokoro_synth_cache[key] = audio
        return audio


def warmup_kokoro() -> bool:
    """Preload the Kokoro engine and cache every fixed ack. Never raises.

    Runs in a background thread at server startup so the first spoken
    turn skips the ~7s model load. No-op unless Kokoro is configured
    for real mode. Returns True when the engine is warm.
    """
    try:
        if tts_provider() != "kokoro" or mock_meta_enabled():
            return False
        provider = KokoroTTSProvider(mock=False)
        warmed = False
        for text in WARMUP_TEXTS:
            try:
                provider.synthesize(text)
            except Exception:
                break
            warmed = True
    except Exception:
        return False
    return warmed


def get_provider():
    """Return the configured TTS engine (TTS_PROVIDER=meta|kokoro)."""
    provider = tts_provider()
    if provider == "kokoro":
        return KokoroTTSProvider()
    if provider == "meta":
        return MetaTTSProvider()
    raise MetaAPIError("unknown TTS_PROVIDER %r (want meta or kokoro)" % provider)
