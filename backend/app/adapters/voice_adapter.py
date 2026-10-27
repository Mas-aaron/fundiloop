"""Meta voice adapter: speech-to-text via Muse Voice Transcribe.

MOCK mode (default) returns a sample transcript. Real mode
(MOCK_META=false + META_API_KEY) POSTs the uploaded push-to-talk audio to
the Meta ASR endpoint, following the legacy backend/transcribe.py multipart
pattern. Model name from VOICE_MODEL; endpoint from META_ASR_URL, defaulting
to META_API_BASE_URL + "/asr/transcribe".

Speech output is always the browser's speechSynthesis (frontend speaks the
speak_to_tech text), so speak() never calls an API in any mode.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

from . import MetaAPIError, api_base_url, api_key, api_timeout


def _mock_enabled() -> bool:
    if os.environ.get("MOCK_META", "true").lower() in ("1", "true", "yes"):
        return True
    return not os.environ.get("META_API_KEY")


def asr_url() -> str:
    override = os.environ.get("META_ASR_URL", "").strip()
    if override:
        return override
    return api_base_url() + "/asr/transcribe"


class MetaVoiceAdapter:
    def __init__(self, model: str | None = None, mock: bool | None = None) -> None:
        self.model = model or os.environ.get("VOICE_MODEL", "meta-voice-1")
        self.mock = _mock_enabled() if mock is None else mock

    def speak(self, text: str) -> dict:
        """Speech output is the browser's speechSynthesis; just echo text."""
        return {"text": text, "audio_base64": None, "engine": "browser-tts", "mock": self.mock}

    def transcribe(self, audio: bytes, filename: str = "talk.webm") -> dict:
        """Transcribe push-to-talk audio, return {"text": ...}."""
        if self.mock or not audio:
            # "done" exercises the frontend keyword auto-trigger in mock demos.
            return {"text": "done", "mock": True, "note": "mock transcript"}
        if not api_key():
            raise MetaAPIError("META_API_KEY is not set")
        data, content_type, fname = self._prepare_upload(audio, filename)
        boundary = "----fundiloop-audio"
        head = ('--%s\r\nContent-Disposition: form-data; name="file"; '
                'filename="%s"\r\nContent-Type: %s\r\n'
                "X-Model: %s\r\n\r\n" % (boundary, fname, content_type, self.model)).encode("ascii")
        tail = ("\r\n--%s--\r\n" % boundary).encode("ascii")
        req = urllib.request.Request(
            asr_url(), data=head + data + tail,
            headers={"Authorization": "Bearer %s" % api_key(),
                     "Content-Type": "multipart/form-data; boundary=%s" % boundary},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=api_timeout()) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise MetaAPIError("voice transcribe failed: %s" % exc) from exc
        text = payload.get("text", "") if isinstance(payload, dict) else str(payload)
        return {"text": text or "", "mock": False}

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
