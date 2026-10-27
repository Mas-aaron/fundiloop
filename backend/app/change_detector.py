"""Frame change detection: only analyze settled frames.

OpenCV frame differencing gates expensive Meta API calls: update() returns
the frame only when motion has settled for `settle_seconds` (default 1s),
otherwise None. Without OpenCV installed it passes frames through.
"""
from __future__ import annotations

import time

try:
    import cv2
    import numpy as np

    _CV2 = True
except ImportError:  # pragma: no cover - optional dependency
    _CV2 = False


class MotionSettler:
    def __init__(self, settle_seconds: float = 1.0, motion_fraction: float = 0.02) -> None:
        self.settle_seconds = settle_seconds
        self.motion_fraction = motion_fraction
        self.available = _CV2
        self._prev_gray = None
        self._last_motion: float | None = None
        self.settled = False

    def update(self, frame: bytes) -> bytes | None:
        """Feed a JPEG frame; return it when settled, else None."""
        now = time.monotonic()
        if not self.available or not frame:
            self.settled = True
            return frame or None
        gray = self._decode_gray(frame)
        if gray is None:
            self.settled = False
            return None
        if self._prev_gray is None or self._prev_gray.shape != gray.shape:
            self._prev_gray = gray
            self._last_motion = now - self.settle_seconds  # first frame is clean
            self.settled = True
            return frame
        diff = cv2.absdiff(self._prev_gray, gray)
        changed = int(np.count_nonzero(diff > 25)) / diff.size
        self._prev_gray = gray
        if changed > self.motion_fraction:
            self._last_motion = now
            self.settled = False
            return None
        self.settled = (now - (self._last_motion or 0.0)) >= self.settle_seconds
        return frame if self.settled else None

    def reset(self) -> None:
        self._prev_gray = None
        self._last_motion = None
        self.settled = False

    @staticmethod
    def _decode_gray(frame: bytes):
        arr = np.frombuffer(frame, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        return cv2.GaussianBlur(img, (9, 9), 0)
