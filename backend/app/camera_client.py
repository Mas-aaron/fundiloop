"""Overhead camera client for the ESP32-CAM frame feed.

The ESP32-CAM is pre-existing infrastructure: it is already mounted and
already serves frames. This adapter only fetches the latest frame from the
URL in CAMERA_FRAME_URL. No camera firmware lives in this repo.

Mock mode (default) returns a synthetic bench frame so the loop runs with
no camera. If a real URL is set but unreachable, the client falls back to
a mock frame and reports the error in the source string.
"""
from __future__ import annotations

import os
import struct
import urllib.request
import zlib

_mock_frame_cache: bytes | None = None


def _mock_enabled() -> bool:
    return os.environ.get("MOCK_META", "true").lower() in ("1", "true", "yes")


class CameraClient:
    def __init__(self, frame_url: str | None = None, mock: bool | None = None,
                 timeout: float = 6.0) -> None:
        self.frame_url = (frame_url or os.environ.get("CAMERA_FRAME_URL", "")).strip()
        if mock is None:
            mock = _mock_enabled() or not self.frame_url
        self.mock = mock
        self.timeout = timeout

    def fetch_latest(self) -> tuple[bytes, str]:
        """Return (jpeg_bytes, source). Source is honest about mock/real."""
        if self.mock or not self.frame_url:
            return mock_frame(), "mock (no CAMERA_FRAME_URL)" if not self.frame_url else "mock"
        try:
            return self._fetch_real(), "camera %s" % self.frame_url
        except Exception as exc:
            return mock_frame(), "mock-fallback (camera unreachable: %s)" % exc

    def _fetch_real(self) -> bytes:
        with urllib.request.urlopen(self.frame_url, timeout=self.timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "multipart" in ctype:  # MJPEG stream: extract first frame
                return _first_jpeg_from_stream(resp)
            data = resp.read()
        if data[:2] == b"\xff\xd8":  # single JPEG (e.g. /capture)
            return data
        # Some servers wrap the JPEG in a stream even on snapshot URLs.
        start, end = data.find(b"\xff\xd8"), data.find(b"\xff\xd9")
        if start >= 0 and end > start:
            return data[start:end + 2]
        raise RuntimeError("no JPEG frame at %s" % self.frame_url)


def _first_jpeg_from_stream(resp, limit: int = 2_000_000) -> bytes:
    buf = b""
    while len(buf) < limit:
        chunk = resp.read(8192)
        if not chunk:
            break
        buf += chunk
        start, end = buf.find(b"\xff\xd8"), buf.find(b"\xff\xd9")
        if start >= 0 and end > start:
            return buf[start:end + 2]
    raise RuntimeError("no JPEG frame in MJPEG stream")


def mock_frame() -> bytes:
    """Deterministic synthetic bench frame (static bytes: settles instantly).

    JPEG when OpenCV is installed, otherwise a stdlib-encoded PNG. Both are
    static so the motion settler treats them as settled immediately.
    """
    global _mock_frame_cache
    if _mock_frame_cache is not None:
        return _mock_frame_cache
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        _mock_frame_cache = _png_bench_frame()
        return _mock_frame_cache
    import cv2
    import numpy as np
    img = np.full((480, 640, 3), (24, 60, 32), np.uint8)  # bench mat
    cv2.rectangle(img, (60, 100), (300, 380), (200, 200, 200), 2)
    cv2.putText(img, "ESP32", (140, 250), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (240, 240, 240), 2)
    cv2.rectangle(img, (350, 130), (470, 230), (180, 180, 180), 2)
    cv2.putText(img, "BME280", (358, 185), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (240, 240, 240), 2)
    cv2.rectangle(img, (350, 260), (500, 350), (180, 180, 180), 2)
    cv2.putText(img, "OLED", (395, 310), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (240, 240, 240), 2)
    cv2.circle(img, (540, 220), 14, (80, 80, 220), -1)
    cv2.putText(img, "LED", (520, 260), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (240, 240, 240), 1)
    for i in range(8):  # jumper wires from ESP32 to the rail area
        y = 120 + i * 32
        cv2.line(img, (300, y), (350, y), (60, 160, 220 - i * 10), 2)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise RuntimeError("mock JPEG encode failed")
    _mock_frame_cache = bytes(buf)
    return _mock_frame_cache


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data))


def _png_bench_frame(width: int = 640, height: int = 480) -> bytes:
    """Encode the synthetic bench scene as PNG with stdlib only."""
    bg = bytes((24, 60, 32))
    fg = bytes((230, 230, 230))
    wire = bytes((220, 200, 60))
    rects = [(60, 100, 300, 380), (350, 130, 470, 230), (350, 260, 500, 350), (526, 206, 554, 234)]
    wire_ys = [120 + i * 32 for i in range(8)]

    raw = bytearray()
    for y in range(height):
        row = bytearray(bg * width)
        for x0, y0, x1, y1 in rects:
            if y0 <= y <= y1:
                if y in (y0, y0 + 1, y1 - 1, y1):
                    row[x0 * 3:x1 * 3] = fg * (x1 - x0)
                else:
                    row[x0 * 3:(x0 + 2) * 3] = fg * 2
                    row[(x1 - 2) * 3:x1 * 3] = fg * 2
        if y in wire_ys:
            row[300 * 3:350 * 3] = wire * 50
        raw.append(0)  # filter type 0 (none)
        raw += row

    png = bytearray(b"\x89PNG\r\n\x1a\n")
    png += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += _png_chunk(b"IEND", b"")
    return bytes(png)


def media_type_for_frame(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"
