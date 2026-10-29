#!/usr/bin/env python3
"""Run real SAM segmentation alone on the latest debug frame.

Uses the live MetaSAMAdapter path (one streamed cookbook request per
concept, decoded with meta_sam_parser) and prints the detections. Faster
than a full check-now turn when iterating on vision.

Requires: MOCK_META=false, META_API_KEY, meta-sam-parser installed
(backend\\.venv\\Scripts\\python.exe -m pip install -r backend\\requirements.txt).

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe scripts\\sam_probe.py
"""
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from backend.app.adapters import mock_meta_enabled  # noqa: E402
from backend.app.adapters.sam_adapter import SAM_CONCEPTS, MetaSAMAdapter  # noqa: E402


def main() -> None:
    if mock_meta_enabled():
        print("Set MOCK_META=false in .env first (this probes the live API).")
        sys.exit(1)
    if not os.environ.get("META_API_KEY", "").strip():
        print("META_API_KEY is not set in .env.")
        sys.exit(1)
    frames = sorted((Path(ROOT) / "backend" / "data" / "debug_frames").glob("*.jpg"),
                    key=lambda p: p.stat().st_mtime)
    if not frames:
        print("No debug frames found. Press CHECK NOW once first.")
        sys.exit(1)
    latest = frames[-1]
    print("frame: %s (%d bytes)" % (latest.name, latest.stat().st_size))
    print("concepts: %s" % ", ".join(c for c, _ in SAM_CONCEPTS))
    sam = MetaSAMAdapter(mock=False)
    try:
        out = sam.analyze(latest.read_bytes(), frame_path=latest)
    except Exception as exc:
        print("SAM FAILED: %s" % exc)
        sys.exit(1)
    print("latency_ms=%s http=%s request_id=%s" % (
        out.get("latency_ms"), out.get("http_status"), out.get("request_id")))
    print("notes: %s" % out.get("notes"))
    for det in out.get("detections", []):
        print("  %-14s box=%s conf=%.2f%s" % (
            det.get("label"), det.get("box"), det.get("confidence", 0),
            " +mask" if det.get("mask_svg") else ""))


if __name__ == "__main__":
    main()
