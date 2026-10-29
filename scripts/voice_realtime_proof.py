#!/usr/bin/env python3
"""REAL live-voice proof: stream a WAV to Muse Voice Transcribe in real time.

Proves the Meta-native live-listening half of a Gemini-Live-style loop works:
  1. opens wss://api.meta.ai/v1/asr/realtime (real handshake, key inside the frame)
  2. streams raw mono 16-bit PCM at real time (the server closes on >5s backlog)
  3. prints cumulative partials as they arrive, then the final transcript
  4. fails loudly (exit 1) in mock mode or on any error

Needs a mono 16-bit WAV at 16 kHz or 24 kHz. Convert anything else first:
  ffmpeg -i input.m4a -ac 1 -ar 24000 -c:a pcm_s16le -map_metadata -1 sample.wav

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe scripts\\voice_realtime_proof.py sample.wav
"""
import asyncio
import os
import sys
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from backend.app.adapters import mock_meta_enabled  # noqa: E402
from backend.app.adapters.voice_adapter import MetaVoiceAdapter, realtime_url  # noqa: E402

CHUNK_MS = 80


def fail(msg: str) -> "object":
    print("LIVE VOICE PROOF FAILED: %s" % msg)
    sys.exit(1)


def read_wav(path: str) -> tuple:
    with wave.open(path, "rb") as wf:
        channels, width, rate = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    return channels, width, rate, pcm


async def paced_frames(pcm: bytes, rate: int):
    """Yield CHUNK_MS chunks on an absolute real-time schedule.

    Pacing against a fixed schedule (not a per-chunk sleep) is what the
    cookbook warns about: flat sleeps accumulate drift until the server
    closes the session for falling behind real time.
    """
    chunk_bytes = rate * 2 * CHUNK_MS // 1000
    loop = asyncio.get_running_loop()
    start = loop.time()
    sent = 0
    while sent < len(pcm):
        chunk = pcm[sent:sent + chunk_bytes]
        sent += chunk_bytes
        yield chunk
        delay = (start + sent / (rate * 2)) - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)


async def run(path: str) -> None:
    print("== FundiLoop live voice proof (REAL Muse Voice Transcribe) ==")
    if mock_meta_enabled():
        fail("MOCK_META is not 'false' (refusing to prove mocks). Set MOCK_META=false in .env.")
    if not os.environ.get("META_API_KEY", "").strip():
        fail("META_API_KEY is not set in .env.")

    channels, width, rate, pcm = read_wav(path)
    if channels != 1 or width != 2:
        fail("WAV must be mono 16-bit (got %d channel(s), %d byte(s)/sample)."
             % (channels, width))
    if rate not in (16000, 24000):
        fail("WAV must be 16 kHz or 24 kHz (got %d)." % rate)
    print("[1/2] streaming %s: %.1fs, %d Hz mono 16-bit" % (
        os.path.basename(path), len(pcm) / (rate * 2), rate))
    print("      endpoint: %s" % realtime_url())

    session = MetaVoiceAdapter(mock=False).realtime(mode="PUSH_TO_TALK", sample_rate=rate)
    if session.mock:
        fail("voice session unexpectedly in mock mode.")

    last = {"len": 0}

    def on_partial(text: str) -> None:
        # Partials are cumulative: rewrite the line, never append.
        pad = " " * max(0, last["len"] - len(text))
        print("      %s%s" % (text, pad), end="\r", flush=True)
        last["len"] = len(text)

    started = time.monotonic()
    try:
        result = await session.stream(paced_frames(pcm, rate), on_partial=on_partial)
    except Exception as exc:
        fail("realtime session failed: %s" % exc)
    elapsed = time.monotonic() - started

    if result.get("mock"):
        fail("realtime result is flagged mock.")
    print("\n[2/2] final transcript: %r" % result["text"])
    print("      turns=%d session_id=%s elapsed=%.1fs" % (
        len(result.get("turns", [])), result.get("session_id"), elapsed))
    if not result["text"]:
        fail("server returned an empty transcript.")
    print("LIVE VOICE PROOF PASSED. Check backend/data/logs/model_calls.jsonl.")


def main() -> None:
    if len(sys.argv) < 2:
        fail("usage: voice_realtime_proof.py <mono-16bit-16k-or-24k.wav>")
    asyncio.run(run(sys.argv[1]))


if __name__ == "__main__":
    main()
