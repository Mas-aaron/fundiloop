"""Real ESP32-CAM client: MJPEG preview stream + /capture analysis stills.

The ESP32-CAM is pre-existing infrastructure on the local network. This
module only fetches real frames from it:

- CAMERA_STREAM_URL: MJPEG stream (e.g. http://192.168.1.50:81/stream).
  A background thread reads it continuously but keeps ONLY the latest
  frame, which get_latest_preview_frame() returns for the live preview.
- CAMERA_CAPTURE_URL: still endpoint (e.g. http://192.168.1.50/capture).
  capture_analysis_frame() GETs one real JPEG, validates it, and saves it
  to backend/data/debug_frames/ for the analysis pipeline.

There is no mock frame generator and no silent fallback: every failure
raises CameraError so the API returns an error and stops the session step.
CAMERA_FRAME_URL is honored as a legacy alias for CAMERA_CAPTURE_URL.
"""
from __future__ import annotations

import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_DEBUG_DIR = DATA_DIR / "debug_frames"


class CameraError(RuntimeError):
    """The real camera could not provide a frame (offline, bad URL, bad JPEG)."""


def _capture_url(explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip()
    return (
        os.environ.get("CAMERA_CAPTURE_URL", "").strip()
        or os.environ.get("CAMERA_FRAME_URL", "").strip()
    )


def _stream_url(explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip()
    return os.environ.get("CAMERA_STREAM_URL", "").strip()


def _slice_jpeg(data: bytes, source: str) -> bytes:
    """Extract one JPEG from a response body. Raises CameraError if none."""
    start, end = data.find(b"\xff\xd8"), data.find(b"\xff\xd9")
    if start < 0 or end <= start or (end + 2 - start) < 64:
        raise CameraError("no valid JPEG frame from %s (%d bytes)" % (source, len(data)))
    return data[start : end + 2]


def _http_get(url: str, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except Exception as exc:
        raise CameraError("GET %s failed: %s" % (url, exc)) from exc


class CameraClient:
    def __init__(
        self,
        stream_url: str | None = None,
        capture_url: str | None = None,
        frame_url: str | None = None,  # legacy alias for capture_url
        timeout: float = 6.0,
        debug_dir: str | Path | None = None,
    ) -> None:
        self.stream_url = _stream_url(stream_url)
        self.capture_url = _capture_url(capture_url or frame_url)
        self.timeout = timeout
        self.debug_dir = Path(debug_dir) if debug_dir else DEFAULT_DEBUG_DIR
        self._override: bytes | None = None
        self._lock = threading.Lock()
        self._latest: bytes | None = None
        self._latest_at: float = 0.0
        self._last_error: str = ""
        self._thread: threading.Thread | None = None

    # -- configuration -------------------------------------------------
    def set_urls(self, stream_url: str = "", capture_url: str = "") -> None:
        """Point the client at new URLs (runtime camera config)."""
        with self._lock:
            self.stream_url = stream_url.strip()
            self.capture_url = capture_url.strip()
            self._latest = None
            self._latest_at = 0.0
            self._last_error = ""
            self._thread = None  # old daemon exits on its own; new one starts lazily

    def set_override(self, frame: bytes) -> None:
        """Pin a user-supplied frame (phone snap) as the live image."""
        self._override = bytes(frame)

    def clear_override(self) -> None:
        self._override = None

    def describe_source(self) -> str:
        """Where frames currently come from, without fetching anything."""
        if self._override is not None:
            return "snap (override)"
        parts = []
        if self.capture_url:
            parts.append("capture %s" % self.capture_url)
        if self.stream_url:
            parts.append("stream %s" % self.stream_url)
        return "camera (%s)" % ", ".join(parts) if parts else "camera (no URL configured)"

    # -- live preview: background MJPEG thread, latest frame only --------
    def get_latest_preview_frame(self, timeout: float = 8.0) -> bytes:
        """Return the latest preview frame. Raises CameraError if none."""
        if self._override is not None:
            return self._override
        self._ensure_thread()
        deadline = time.monotonic() + timeout
        error_since: float | None = None
        while time.monotonic() < deadline:
            with self._lock:
                if self._latest is not None:
                    return self._latest
                err = self._last_error
            if err and not self.stream_url and not self.capture_url:
                break  # nothing configured: no point waiting
            if err:
                # A failed fetch with no frame ever means the camera is down,
                # not slow: allow one quick retry window, not the full wait.
                now = time.monotonic()
                if error_since is None:
                    error_since = now
                elif now - error_since >= 1.0:
                    break
            else:
                error_since = None
            time.sleep(0.15)
        with self._lock:
            err = self._last_error
        raise CameraError(
            "no preview frame available%s"
            % (" (%s)" % err if err else " (camera unreachable?)")
        )

    def camera_status(self) -> dict:
        """Cheap status for GET /api/status. Never fetches, never raises."""
        if self._override is not None:
            return {
                "camera_online": True,
                "camera_last_frame_age_ms": 0,
                "camera_last_frame_size_bytes": len(self._override),
                "camera_error": "",
            }
        self._ensure_thread()
        with self._lock:
            latest, at, err = self._latest, self._latest_at, self._last_error
        if latest is None:
            return {
                "camera_online": False,
                "camera_last_frame_age_ms": None,
                "camera_last_frame_size_bytes": 0,
                "camera_error": err or "no frame received yet",
            }
        age_ms = int((time.monotonic() - at) * 1000)
        return {
            "camera_online": age_ms < 15000,
            "camera_last_frame_age_ms": age_ms,
            "camera_last_frame_size_bytes": len(latest),
            "camera_error": "" if age_ms < 15000 else (err or "preview frame is stale"),
        }

    def _ensure_thread(self) -> None:
        if self._override is not None:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self.stream_url and not self.capture_url:
                self._last_error = "CAMERA_STREAM_URL/CAMERA_CAPTURE_URL not set"
                return
            self._thread = threading.Thread(target=self._run, name="esp32cam-preview", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        me = threading.current_thread()
        with self._lock:
            if self._thread is not me:
                return
        if self.stream_url:
            self._run_stream(me)
        else:
            self._run_poll(me)

    def _current(self, me: threading.Thread) -> bool:
        with self._lock:
            return self._thread is me

    def _store(self, frame: bytes) -> None:
        with self._lock:
            self._latest = frame  # only the latest frame is ever kept
            self._latest_at = time.monotonic()
            self._last_error = ""

    def _fail(self, message: str) -> None:
        with self._lock:
            self._last_error = message

    def _run_stream(self, me: threading.Thread) -> None:
        """Read the MJPEG stream forever, keeping only the latest frame."""
        url = self.stream_url
        while self._current(me):
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                    buf = b""
                    while self._current(me):
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        buf += chunk
                        if len(buf) > 4_000_000:  # resync if headers pile up
                            buf = buf[-1_000_000:]
                        start = buf.find(b"\xff\xd8")
                        end = buf.find(b"\xff\xd9")
                        if start >= 0 and end > start:
                            self._store(buf[start : end + 2])
                            buf = buf[end + 2 :]
            except Exception as exc:
                self._fail("stream %s: %s" % (url, exc))
                time.sleep(2.0)

    def _run_poll(self, me: threading.Thread) -> None:
        """No MJPEG URL: poll /capture so the preview still shows live frames."""
        while self._current(me):
            url = self.capture_url
            if not url:
                self._fail("CAMERA_STREAM_URL/CAMERA_CAPTURE_URL not set")
                return
            try:
                self._store(_slice_jpeg(_http_get(url, self.timeout), url))
            except CameraError as exc:
                self._fail(str(exc))
            deadline = time.monotonic() + 2.0
            while self._current(me) and time.monotonic() < deadline:
                time.sleep(0.2)

    # -- analysis stills: GET /capture, validate, save ------------------
    def capture_analysis_frame(self, session_id: str = "") -> tuple[bytes, Path]:
        """GET one real JPEG from CAMERA_CAPTURE_URL and save it.

        Returns (jpeg_bytes, saved_path). The file lands in
        backend/data/debug_frames/<timestamp>_<session_id>.jpg.
        Raises CameraError on any failure; never substitutes another image.
        """
        if self._override is not None:
            frame = _slice_jpeg(self._override, "snap (override)")
            return frame, self._save_debug_frame(frame, session_id)
        if not self.capture_url:
            raise CameraError("no CAMERA_CAPTURE_URL configured (legacy CAMERA_FRAME_URL also empty)")
        frame = _slice_jpeg(_http_get(self.capture_url, self.timeout), self.capture_url)
        return frame, self._save_debug_frame(frame, session_id)

    def _save_debug_frame(self, frame: bytes, session_id: str,
                          suffix: str = ".jpg") -> Path:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3]
        safe_session = "".join(c for c in (session_id or "nosession") if c.isalnum() or c in "-_") or "nosession"
        path = self.debug_dir / ("%s_%s%s" % (stamp, safe_session, suffix))
        path.write_bytes(frame)
        return path

    def save_uploaded_frame(self, data: bytes, session_id: str = "upload") -> Path:
        """Validate and save an uploaded equipment photo for discovery.

        Unlike set_override this pins nothing: the file is used for one
        /discover/parts pass. Raises CameraError unless the bytes are a
        real JPEG or PNG image.
        """
        if data[:2] == b"\xff\xd8":
            frame, suffix = _slice_jpeg(data, "uploaded photo"), ".jpg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 64:
            frame, suffix = bytes(data), ".png"
        else:
            raise CameraError("uploaded file is not a JPEG/PNG image")
        return self._save_debug_frame(frame, session_id, suffix=suffix)

    # -- compatibility ---------------------------------------------------
    def grab_live(self) -> bytes:
        """Fetch one frame right now (unsaved). Used by the snap route."""
        if self._override is not None:
            return self._override
        if not self.capture_url:
            raise CameraError("no CAMERA_CAPTURE_URL configured (legacy CAMERA_FRAME_URL also empty)")
        return _slice_jpeg(_http_get(self.capture_url, self.timeout), self.capture_url)

    def fetch_latest(self) -> tuple[bytes, str]:
        """Return (jpeg_bytes, source). Raises CameraError on failure."""
        if self._override is not None:
            return self._override, "snap (override)"
        frame = self.grab_live()
        return frame, "camera %s" % self.capture_url


def media_type_for_frame(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"
