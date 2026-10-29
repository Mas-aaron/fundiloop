#!/usr/bin/env python3
"""REAL live-video proof: track a clip with SAM 3.1 and read it with Muse Spark.

Proves the Meta-native live-video half of a Gemini-Live-style loop works:
  1. reads a short clip from disk and refuses any mock mode
  2. SAM 3.1 (input_video) tracks one concept through the clip, printing the
     per-object tracks (stable object ids) it follows across frames
  3. Muse Spark reads the SAME clip (video + its embedded audio) and returns
     the next spoken instruction
  4. fails loudly (exit 1) on mock data or any error

Meta has no streaming video endpoint: a live turn is a short clip, so proving
this is proving the live-video path.

Needs a short H.264/MP4 clip (a few seconds is enough). Convert from anything:
  ffmpeg -i input.mov -t 5 -c:v libx264 -pix_fmt yuv420p -c:a aac clip.mp4

The optional second argument is the concept to track (default: electronic
components on the bench). Pass a noun phrase that is actually in the clip or
SAM correctly returns zero tracks.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe scripts\\vision_video_proof.py clip.mp4 ["breadboard"]
"""
import base64
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from backend.app.adapters import mock_meta_enabled  # noqa: E402
from backend.app.adapters.muse_adapter import MetaClaudeAdapter  # noqa: E402
from backend.app.adapters.sam_adapter import (  # noqa: E402
    DEFAULT_VIDEO_CONCEPT,
    DEFAULT_VIDEO_MIME,
    MetaSAMAdapter,
)
from backend.app.prompts import LIVE_SYSTEM  # noqa: E402


def fail(msg: str) -> "object":
    print("LIVE VIDEO PROOF FAILED: %s" % msg)
    sys.exit(1)


def main() -> None:
    print("== FundiLoop live video proof (REAL SAM 3.1 + REAL Muse Spark) ==")
    if len(sys.argv) < 2:
        fail("usage: vision_video_proof.py <clip.mp4> [concept]")
    path = sys.argv[1]
    concept = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_VIDEO_CONCEPT
    if mock_meta_enabled():
        fail("MOCK_META is not 'false' (refusing to prove mocks). Set MOCK_META=false in .env.")
    if not os.environ.get("META_API_KEY", "").strip():
        fail("META_API_KEY is not set in .env.")
    if not os.path.isfile(path):
        fail("clip not found: %s" % path)

    clip = open(path, "rb").read()
    if not clip:
        fail("clip is empty: %s" % path)
    mime = DEFAULT_VIDEO_MIME if path.lower().endswith(".mp4") else "video/webm"

    sam = MetaSAMAdapter(mock=False)
    muse = MetaClaudeAdapter(mock=False)
    if sam.mock or muse.mock:
        fail("adapters unexpectedly in mock mode (MOCK_META=%r)." % os.environ.get("MOCK_META"))

    # 1. SAM 3.1 video tracking on the clip.
    print("[1/3] SAM %s tracking the clip (%d bytes, %s) for %r ..." % (
        sam.model, len(clip), mime, concept))
    try:
        tracks = sam.analyze_clip(clip, context={"clip_source": path},
                                  video_path=path, concept=concept, mime=mime)
    except Exception as exc:
        fail("SAM clip call failed: %s" % exc)
    if tracks.get("mock"):
        fail("SAM result is flagged mock.")
    print("[2/3] %d track(s) over %d frame(s); notes: %s" % (
        tracks.get("track_count"), tracks.get("frame_count"), tracks.get("notes") or ""))
    for t in tracks.get("tracks", [])[:6]:
        print("      object %s: %d frame(s) %d..%d, mean confidence %.2f" % (
            t.get("object_id"), t.get("frames_seen"), t.get("first_frame"),
            t.get("last_frame"), t.get("mean_confidence", 0)))
    if not tracks.get("tracks"):
        fail("SAM returned no tracks: %r is not visible in this clip "
             "(pass a concept that is). The API call itself succeeded." % concept)

    # 2. Muse Spark reads the same clip (video + embedded audio).
    print("[3/3] Muse %s reading the clip ..." % muse.model)
    prompt = (
        "This is a live clip from a technician's bench. In one spoken sentence "
        "of at most 30 words, say the single next thing they should do. Only "
        "name what the clip shows. Do not claim anything is fixed."
    )
    try:
        text = muse.complete_text(
            LIVE_SYSTEM, prompt,
            video_b64=base64.b64encode(clip).decode("ascii"), video_mime=mime)
    except Exception as exc:
        fail("Muse clip call failed: %s" % exc)
    if not text.strip():
        fail("Muse returned empty guidance.")
    print("      guidance: %r" % text.strip())

    print("LIVE VIDEO PROOF PASSED: %d track(s), real Muse guidance." % len(tracks["tracks"]))
    print("Check backend/data/logs/model_calls.jsonl for the logged calls.")
    print(json.dumps({"media": tracks.get("media"), "track_count": tracks.get("track_count"),
                      "frame_count": tracks.get("frame_count")}, indent=2))


if __name__ == "__main__":
    main()
