"""Meta API adapters. Real-call patterns mirror the legacy clients
(backend/muse_client.py, backend/sam_client.py, backend/transcribe.py):
OpenAI-compatible chat for Muse/SAM, multipart POST for ASR. Base URL and
timeout come from env.

Mock adapters exist only for offline development. When MOCK_META=false they
raise instead of answering, so a real-mode run can never silently simulate.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_API_BASE_URL = "https://api.meta.ai/v1"

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


class MetaAPIError(RuntimeError):
    """A Meta Model API call failed (network, timeout, bad output).

    The API layer converts this into an HTTP 502 with the real error text;
    it is never replaced with a canned instruction.
    """


def mock_meta_enabled() -> bool:
    """Single source of truth for mock mode. Controlled ONLY by the env var."""
    return os.environ.get("MOCK_META", "true").lower() in ("1", "true", "yes")


def api_base_url() -> str:
    return os.environ.get("META_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/") or DEFAULT_API_BASE_URL


def api_key() -> str:
    return os.environ.get("META_API_KEY", "")


def api_timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("META_API_TIMEOUT", "30")))
    except ValueError:
        return 30.0


def model_calls_log_path() -> Path:
    override = os.environ.get("MODEL_CALLS_LOG", "").strip()
    if override:
        return Path(override)
    return DATA_DIR / "logs" / "model_calls.jsonl"


def log_model_call(
    model: str,
    latency_ms: float,
    status: str | int,
    frame_file: str = "",
    request_id: str = "",
    raw_summary: str = "",
    ok: bool = True,
    error: str = "",
) -> dict:
    """Append one entry to backend/data/logs/model_calls.jsonl. Never raises."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "latency_ms": round(float(latency_ms), 1),
        "status": status,
        "request_id": request_id or "",
        "frame_file": frame_file or "",
        "raw_summary": (raw_summary or "")[:300],
        "ok": bool(ok),
        "error": error or "",
    }
    try:
        path = model_calls_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # logging must never break the loop
    return entry
