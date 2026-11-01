"""One-time fetch of the esptool-js browser bundle.

The browser flasher imports /esptool-js/bundle.js from our own server
(never a CDN at runtime). The file is fetched once from a pinned
release URL the first time it is requested, then served from disk —
the operator copies nothing by hand. If the fetch fails (offline
machine), the error carries manual download instructions.

Envs: ESPTOOL_JS_URL (default pinned unpkg release),
ESPTOOL_JS_TIMEOUT (seconds, default 60).
"""
from __future__ import annotations

import os

BUNDLE_NAME = "bundle.js"
DEFAULT_URL = "https://unpkg.com/esptool-js@0.5.7/bundle.js"
DEFAULT_TIMEOUT = 60

_MANUAL_HINT = (
    "manual fallback: download the esptool-js ESM bundle "
    "(must export Transport + ESPLoader) and save it as "
    "frontend/esptool-js/bundle.js, then reload"
)


class BundleError(Exception):
    """The bundle is missing and could not be fetched."""


def bundle_url() -> str:
    """Pinned release URL from env (never hardcoded at call sites)."""
    return os.environ.get("ESPTOOL_JS_URL", "").strip() or DEFAULT_URL


def bundle_timeout() -> float:
    """Fetch timeout in seconds from env (default 60)."""
    try:
        return max(5.0, float(os.environ.get("ESPTOOL_JS_TIMEOUT",
                                             str(DEFAULT_TIMEOUT))))
    except ValueError:
        return float(DEFAULT_TIMEOUT)


def _looks_valid(data: bytes) -> bool:
    """True when the bytes look like the esptool-js ESM build."""
    return (len(data or b"") > 5000 and b"ESPLoader" in data
            and b"Transport" in data)


def ensure_bundle(dest_dir, opener=None) -> str:
    """Return the bundle path, fetching it once when missing.

    `dest_dir` is the directory holding bundle.js (created as needed).
    `opener` (urlopen-compatible, injectable for hermetic tests)
    defaults to urllib with the configured timeout. Raises BundleError
    with manual instructions when the file is absent and the fetch
    fails or returns something that is not the bundle.
    """
    import urllib.request

    path = os.path.join(str(dest_dir), BUNDLE_NAME)
    try:
        with open(path, "rb") as fh:
            if _looks_valid(fh.read()):
                return path
    except OSError:
        pass
    url = bundle_url()
    fetch = opener or (lambda u: urllib.request.urlopen(
        u, timeout=bundle_timeout()))
    try:
        with fetch(url) as resp:
            data = resp.read()
    except Exception as exc:
        raise BundleError(
            "could not download the browser flasher (%s): %s. %s."
            % (url, exc, _MANUAL_HINT))
    if not _looks_valid(data):
        raise BundleError(
            "download from %s is not the esptool-js bundle. %s."
            % (url, _MANUAL_HINT))
    try:
        os.makedirs(str(dest_dir), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        raise BundleError("cannot save the browser flasher: %s. %s."
                          % (exc, _MANUAL_HINT))
    return path
