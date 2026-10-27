"""Meta API adapters with offline MOCK mode.

Real-call patterns mirror the legacy clients (backend/muse_client.py,
backend/sam_client.py, backend/transcribe.py): OpenAI-compatible chat for
Muse/SAM, multipart POST for ASR. Base URL and timeout come from env.
"""
from __future__ import annotations

import os

DEFAULT_API_BASE_URL = "https://api.meta.ai/v1"


class MetaAPIError(RuntimeError):
    """A Meta Model API call failed (network, timeout, bad output).

    The orchestrator catches this and returns a safe fallback instruction
    so the session never crashes; verification still runs on the sensors.
    """


def api_base_url() -> str:
    return os.environ.get("META_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/") or DEFAULT_API_BASE_URL


def api_key() -> str:
    return os.environ.get("META_API_KEY", "")


def api_timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("META_API_TIMEOUT", "30")))
    except ValueError:
        return 30.0
