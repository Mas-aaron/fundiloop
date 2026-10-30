"""Meta voice adapter: speech-to-text via Muse Voice Transcribe.

Muse Voice Transcribe is the one Meta Model API model that streams, so it is
what makes a Gemini-Live-style live conversation half-possible on Meta: audio
goes up raw and cumulative partial transcripts come back while the technician
is still speaking. Two endpoints, same model:

  POST /v1/asr/transcribe          one-shot: you already have a recording
  wss://api.meta.ai/v1/asr/realtime live: voice agents, dictation, captions

MOCK mode (default) returns a sample transcript. Real mode
(MOCK_META=false + META_API_KEY) either POSTs the uploaded push-to-talk audio
or opens the realtime WebSocket. Model name from VOICE_MODEL; endpoint from
META_ASR_URL / META_ASR_REALTIME_URL, defaulting to META_API_BASE_URL.

Speech output is Meta Voice TTS via POST /api/voice/speak (see
backend/app/tts.py), with default browser speechSynthesis as the
per-utterance fallback. speak() just echoes the text for orchestrator
views; the frontend owns actual audio playback.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
import uuid
from pathlib import Path

from . import MetaAPIError, api_base_url, api_key, api_timeout, log_model_call, mock_meta_enabled

DEFAULT_VOICE_MODEL = "muse-voice-transcribe-1.0"


def _mock_enabled() -> bool:
    return mock_meta_enabled()


def asr_url(session_id: str = "") -> str:
    override = os.environ.get("META_ASR_URL", "").strip()
    url = override or api_base_url() + "/asr/transcribe"
    return _with_session(url, session_id)


def realtime_url(session_id: str = "") -> str:
    """WebSocket URL for live transcription (wss://.../asr/realtime)."""
    override = os.environ.get("META_ASR_REALTIME_URL", "").strip()
    url = override or _to_ws(api_base_url() + "/asr/realtime")
    return _with_session(url, session_id)


def _to_ws(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def _with_session(url: str, session_id: str) -> str:
    if not session_id:
        return url
    sep = "&" if "?" in url else "?"
    return "%s%ssessionId=%s" % (url, sep, session_id)


class MetaVoiceAdapter:
    def __init__(self, model: str | None = None, mock: bool | None = None) -> None:
        self.model = model or os.environ.get("VOICE_MODEL", DEFAULT_VOICE_MODEL)
        self.mock = _mock_enabled() if mock is None else mock

    def realtime(self, mode: str = "PUSH_TO_TALK",
                 sample_rate: int = 24000) -> "RealtimeVoiceStream":
        """Open a live transcription session (gemini-live-style listening half)."""
        return RealtimeVoiceStream(model=self.model, mode=mode, mock=self.mock,
                                   sample_rate=sample_rate)

    def speak(self, text: str) -> dict:
        """Echo text for orchestrator views; audio plays via /api/voice/speak."""
        return {"text": text, "audio_base64": None, "engine": "meta-tts", "mock": self.mock}

    def transcribe(self, audio: bytes, filename: str = "talk.webm") -> dict:
        """Transcribe push-to-talk audio, return {"text": ...}."""
        if self.mock:
            if not mock_meta_enabled():
                raise MetaAPIError("mock voice adapter called with MOCK_META=false")
            # "done" exercises the frontend keyword auto-trigger in mock demos.
            return {"text": "done", "mock": True, "note": "mock transcript"}
        if not audio:
            raise MetaAPIError("voice transcribe called with empty audio")
        if not api_key():
            raise MetaAPIError("META_API_KEY is not set")
        data, content_type, fname = self._prepare_upload(audio, filename)
        session_id = "fundiloop-%s" % uuid.uuid4()
        request_json = json.dumps({"model": self.model, "mode": "PUSH_TO_TALK",
                                   "audioEncoding": "WAV"})
        boundary = "----fundiloop-audio"
        head = (
            '--%s\r\nContent-Disposition: form-data; name="request"\r\n'
            "Content-Type: application/json\r\n\r\n%s\r\n"
            '--%s\r\nContent-Disposition: form-data; name="audio"; filename="%s"\r\n'
            "Content-Type: %s\r\n\r\n" % (boundary, request_json, boundary, fname, content_type)
        ).encode("utf-8")
        tail = ("\r\n--%s--\r\n" % boundary).encode("ascii")
        req = urllib.request.Request(
            asr_url(session_id), data=head + data + tail,
            headers={"Authorization": "Bearer %s" % api_key(),
                     "Content-Type": "multipart/form-data; boundary=%s" % boundary},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=api_timeout()) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            hint = ""
            if getattr(exc, "code", None) == 404:
                hint = (" (HTTP 404: unknown model or path — VOICE_MODEL=%r, "
                        "want %r)" % (self.model, DEFAULT_VOICE_MODEL))
            raise MetaAPIError("voice transcribe failed: %s%s" % (exc, hint)) from exc
        if isinstance(payload, dict):
            # The API returns "transcript"; "text" is honored for older callers.
            text = payload.get("transcript") or payload.get("text") or ""
        else:
            text = str(payload)
        return {"text": text, "mock": False}

    @staticmethod
    def _prepare_upload(audio: bytes, filename: str) -> tuple:
        """Convert to WAV when pydub/ffmpeg allow, else post bytes as-is."""
        suffix = Path(filename or "talk.webm").suffix.lower()
        ctype = {"": "audio/webm", ".webm": "audio/webm", ".ogg": "audio/ogg",
                 ".mp3": "audio/mpeg", ".wav": "audio/wav"}.get(suffix, "application/octet-stream")
        if suffix == ".wav":
            return audio, "audio/wav", "audio.wav"
        try:
            import io

            from pydub import AudioSegment

            seg = AudioSegment.from_file(io.BytesIO(audio))
            buf = io.BytesIO()
            seg.export(buf, format="wav")
            return buf.getvalue(), "audio/wav", "audio.wav"
        except Exception:
            return audio, ctype, filename or "talk.webm"


class RealtimeVoiceStream:
    """Live transcription over Meta's realtime ASR WebSocket.

    Handshake frame configures the session; the credential travels INSIDE
    that frame (the Authorization header is ignored on this endpoint). Audio
    is raw mono 16-bit PCM at 24 kHz, sent in real time. Server events:
    speechStart / transcript (partials, cumulative) / speaker / speechEnd /
    speechComplete / error. Mode is fixed at the handshake.
    """

    AUDIO_ENCODING = "PCM_24KHZ"

    def __init__(self, model: str | None = None, mode: str = "PUSH_TO_TALK",
                 mock: bool | None = None, keywords=None, language_bias=None,
                 sample_rate: int = 24000) -> None:
        self.model = model or os.environ.get("VOICE_MODEL", DEFAULT_VOICE_MODEL)
        self.mode = mode
        self.mock = _mock_enabled() if mock is None else mock
        self.keywords = list(keywords or [])
        self.language_bias = list(language_bias or [])
        self.sample_rate = sample_rate
        self.audio_encoding = "PCM_16KHZ" if sample_rate == 16000 else self.AUDIO_ENCODING
        self.session_id = ""

    async def stream(self, frames, on_partial=None) -> dict:
        """Stream PCM chunks and return the completed transcript.

        `frames` is an async iterable of raw mono 16-bit PCM chunks (24 kHz).
        `on_partial` (optional) receives each cumulative partial transcript.
        Returns {"text", "turns", "session_id", "mock"}.
        """
        if self.mock:
            if not mock_meta_enabled():
                raise MetaAPIError("mock voice adapter called with MOCK_META=false")
            # "done" exercises the frontend keyword auto-trigger in mock demos.
            if on_partial:
                on_partial("done")
            return {"text": "done", "turns": [], "session_id": "", "mock": True}
        if not api_key():
            raise MetaAPIError("META_API_KEY is not set")

        import time

        import websockets

        self.session_id = "fundiloop-%s" % uuid.uuid4()
        handshake = {
            "authorization": {"accessToken": "Bearer %s" % api_key()},
            "audioEncoding": self.audio_encoding,
            "model": self.model,
            "mode": self.mode,
            "partialMode": "CUMULATIVE",
            "emitAudioProgress": False,
        }
        if self.keywords:
            handshake["keywords"] = self.keywords
        if self.language_bias:
            handshake["languageBias"] = self.language_bias

        started = time.monotonic()
        text = ""
        turns: list = []
        try:
            async with websockets.connect(realtime_url(self.session_id),
                                          open_timeout=api_timeout()) as ws:
                await ws.send(json.dumps(handshake))
                first = json.loads(await ws.recv())
                if "sessionId" not in first:
                    raise MetaAPIError("realtime handshake failed: %s" % first)

                async def pump() -> None:
                    async for chunk in frames:
                        await ws.send(chunk)
                    await ws.send(json.dumps({"type": "endStream"}))

                sender = asyncio.create_task(pump())
                try:
                    async for message in ws:
                        if isinstance(message, bytes):
                            continue
                        event = json.loads(message)
                        etype = event.get("type")
                        if etype == "error":
                            raise MetaAPIError("realtime error: %s" % event.get("message", ""))
                        if etype == "transcript":
                            if on_partial:
                                on_partial(event.get("transcript", ""))
                            if event.get("final"):
                                text = event.get("transcript", "")
                        elif etype == "speechComplete":
                            # speechEnd marks the boundary; the text is here.
                            text = event.get("transcript") or text
                            turns.append({"turnId": event.get("turnId"),
                                          "transcript": text})
                finally:
                    if not sender.done():
                        sender.cancel()
        except MetaAPIError:
            raise
        except Exception as exc:
            raise MetaAPIError("realtime voice session failed: %s" % exc) from exc

        latency_ms = (time.monotonic() - started) * 1000
        log_model_call(self.model, latency_ms, "stream", "", self.session_id,
                       raw_summary=text)
        return {"text": text, "turns": turns, "session_id": self.session_id, "mock": False}
