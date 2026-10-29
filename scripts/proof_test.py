#!/usr/bin/env python3
"""REAL-hardware proof test: camera -> SAM -> Muse, no mocks allowed.

Proves the FundiLoop pipeline uses the real ESP32-CAM and real Meta models:
  1. GETs CAMERA_CAPTURE_URL and saves the frame to backend/data/debug_frames/
  2. Prints frame size + path so you can visually confirm your breadboard
  3. Calls SAM (SAM_MODEL) on the saved frame, prints returned labels
  4. Calls Muse (MUSE_MODEL) with SAM results + a synthetic I2C scan,
     prints the AgentDecision JSON
  5. Fails loudly (exit 1) if ANY step uses mock data

Requires the real setup in .env (see .env.example): MOCK_META=false,
CAMERA_CAPTURE_URL, META_API_KEY, META_API_BASE_URL.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe scripts\\proof_test.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from backend.app.adapters import mock_meta_enabled  # noqa: E402
from backend.app.adapters.muse_adapter import MetaClaudeAdapter  # noqa: E402
from backend.app.adapters.sam_adapter import MetaSAMAdapter  # noqa: E402
from backend.app.camera_client import CameraClient  # noqa: E402
from backend.app.models import Mode, ProbeReading  # noqa: E402


def fail(msg: str) -> "object":
    print("PROOF FAILED: %s" % msg)
    sys.exit(1)


def main() -> None:
    print("== FundiLoop proof test (REAL hardware + REAL Meta models) ==")

    # 0. Refuse to run in mock mode: that would prove nothing.
    if mock_meta_enabled():
        fail("MOCK_META is not 'false' (refusing to prove mocks). Set MOCK_META=false in .env.")
    if not os.environ.get("CAMERA_CAPTURE_URL", "").strip() and not os.environ.get(
        "CAMERA_FRAME_URL", ""
    ).strip():
        fail("CAMERA_CAPTURE_URL is not set in .env.")
    if not os.environ.get("META_API_KEY", "").strip():
        fail("META_API_KEY is not set in .env.")

    sam = MetaSAMAdapter(mock=False)
    muse = MetaClaudeAdapter(mock=False)
    if sam.mock or muse.mock:
        fail("adapters unexpectedly in mock mode (MOCK_META=%r)." % os.environ.get("MOCK_META"))

    # 1+2. Real /capture still, saved for visual confirmation.
    print("[1/4] GET %s ..." % CameraClient().capture_url)
    try:
        frame, saved = CameraClient().capture_analysis_frame("proof")
    except Exception as exc:
        fail("camera /capture failed: %s" % exc)
    if not (frame[:2] == b"\xff\xd8" and frame[-2:] == b"\xff\xd9"):
        fail("captured bytes are not a real JPEG (bad magic).")
    print("[2/4] frame: %d bytes -> %s" % (len(frame), saved))
    print("      OPEN THIS FILE: it must be a photo of YOUR breadboard, not a diagram.")

    # 3. Real SAM call on the SAVED frame.
    print("[3/4] SAM %s on %s ..." % (sam.model, os.path.basename(saved)))
    try:
        result = sam.analyze(frame, frame_path=saved)
    except Exception as exc:
        fail("SAM call failed: %s" % exc)
    if result.get("mock"):
        fail("SAM result is flagged mock.")
    labels = [(d.get("label"), d.get("confidence")) for d in result.get("detections", [])]
    print("      labels: %s" % (labels or "(none returned)"))
    print("      wire_count=%s wires_ok=%s oled_lit=%s latency_ms=%s http=%s request_id=%s" % (
        result.get("wire_count"), result.get("wires_ok"), result.get("oled_lit"),
        result.get("latency_ms"), result.get("http_status"), result.get("request_id")))
    print("      notes: %s" % (result.get("notes") or ""))

    # 4. Real Muse call with SAM results + a synthetic I2C scan.
    #    (The I2C scan here is explicit test input, not pipeline output.)
    print("[4/4] Muse %s with SAM results + synthetic I2C scan ..." % claude.model)
    fake_probe = ProbeReading(
        connected=True,
        i2c_devices=["0x3C", "0x76"],
        temperature_c=24.6,
        humidity_pct=48.2,
        pressure_hpa=1013.2,
        oled_ok=True,
        led_ok=True,
        mock=False,
        raw='{"note": "synthetic proof-test input, not a probe reading"}',
    )
    try:
        decision = claude.decide({
            "mode": Mode.GUIDED_ASSEMBLY,
            "step": None,
            "step_index": 0,
            "steps_total": 8,
            "probe": fake_probe,
            "sam": result,
            "code_snippet": "",
            "graph": None,
            "history": [],
            "frame_file": str(saved),
        })
    except Exception as exc:
        fail("Muse call failed: %s" % exc)
    print("      AgentDecision:")
    print(json.dumps(decision.model_dump(), indent=2))

    print("PROOF PASSED: real JPEG (%d bytes), real SAM (%d labels), real Muse decision."
          % (len(frame), len(labels)))
    print("Check backend/data/logs/model_calls.jsonl for the logged calls.")


if __name__ == "__main__":
    main()
