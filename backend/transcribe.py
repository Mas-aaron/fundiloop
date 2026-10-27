"""Audio transcription: MediaRecorder webm/opus -> WAV -> Meta ASR."""
import json
import os
import time
import urllib.request
from pathlib import Path

ASR_URL = "https://api.meta.ai/v1/asr/transcribe"
BOUNDARY = "----fundiloop-audio"


def convert_to_wav(src: Path, dst: Path) -> None:
    """Convert any pydub-readable audio file to WAV. Needs ffmpeg."""
    from pydub import AudioSegment

    AudioSegment.from_file(src).export(dst, format="wav")


def post_wav_to_asr(wav_path: Path, api_key: str) -> str:
    """POST a WAV file to Meta ASR, return the recognized text."""
    data = wav_path.read_bytes()
    head = (
        "--%s\r\nContent-Disposition: form-data; name=\"file\"; "
        'filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n' % BOUNDARY
    ).encode("ascii")
    tail = ("\r\n--%s--\r\n" % BOUNDARY).encode("ascii")
    req = urllib.request.Request(
        ASR_URL,
        data=head + data + tail,
        headers={
            "Authorization": "Bearer %s" % api_key,
            "Content-Type": "multipart/form-data; boundary=%s" % BOUNDARY,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if isinstance(payload, dict):
        return payload.get("text", "") or ""
    return str(payload)


def transcribe_upload(raw: bytes, filename: str, workdir: Path) -> dict:
    """Save an upload, convert to WAV, transcribe. Never raises."""
    try:
        from pydub import AudioSegment  # noqa: F401
    except Exception:
        return {"text": "", "error": "audio conversion unavailable"}
    api_key = os.environ.get("MODEL_API_KEY", "")
    if not api_key:
        return {"text": "", "error": "MODEL_API_KEY is not set"}
    workdir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    suffix = Path(filename or "audio.webm").suffix or ".webm"
    src = workdir / f"audio-{stamp}{suffix}"
    dst = workdir / f"audio-{stamp}.wav"
    try:
        src.write_bytes(raw)
        try:
            convert_to_wav(src, dst)
        except Exception:
            return {"text": "", "error": "audio conversion unavailable"}
        try:
            return {"text": post_wav_to_asr(dst, api_key)}
        except Exception as exc:
            return {"text": "", "error": str(exc)}
    finally:
        for path in (src, dst):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
