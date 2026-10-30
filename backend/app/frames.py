"""448px frame pipeline: shrink stills before Meta calls, map boxes back.

Every still shipped to SAM is resized to exactly FRAME_EDGE x FRAME_EDGE
JPEG so payloads stay small and turn latency stays low. Boxes SAM returns
are in shipped-pixel coordinates; rescale_detections() maps them back to
source pixels with exact per-axis scale factors, so every downstream
consumer still sees source-pixel boxes.

Without OpenCV installed prepare_frame() ships the original bytes and
rescaling is the identity: slower, but never wrong. Clips are untouched:
re-encoding video costs more than it saves.
"""
from __future__ import annotations

from dataclasses import dataclass

FRAME_EDGE = 448
JPEG_QUALITY = 85

try:
    import cv2
    import numpy as np

    _CV2 = True
except ImportError:  # pragma: no cover - optional dependency
    _CV2 = False


@dataclass
class PreparedFrame:
    payload: bytes  # bytes actually shipped to the model
    src_w: int
    src_h: int
    dst_w: int
    dst_h: int
    resized: bool  # False: payload is the original frame, identity scale


def jpeg_dimensions(frame: bytes) -> tuple[int, int] | None:
    """Read width/height from JPEG SOF markers without decoding pixels."""
    if len(frame) < 4 or frame[:2] != b"\xff\xd8":
        return None
    pos = 2
    end = len(frame) - 9
    while pos < end:
        if frame[pos] != 0xFF:
            pos += 1
            continue
        marker = frame[pos + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):  # SOF: height, width follow
            height = int.from_bytes(frame[pos + 5:pos + 7], "big")
            width = int.from_bytes(frame[pos + 7:pos + 9], "big")
            return (width, height) if width and height else None
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        size = int.from_bytes(frame[pos + 2:pos + 4], "big")
        if size < 2:
            return None
        pos += 2 + size
    return None


def prepare_frame(frame: bytes, edge: int = FRAME_EDGE) -> PreparedFrame:
    """Downscale one still to edge x edge JPEG; passthrough without cv2."""
    if _CV2 and frame:
        arr = np.frombuffer(frame, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            src_h, src_w = img.shape[:2]
            if src_w == edge and src_h == edge:
                return PreparedFrame(frame, src_w, src_h, edge, edge, False)
            small = cv2.resize(img, (edge, edge), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(
                ".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                return PreparedFrame(bytes(buf), src_w, src_h, edge, edge, True)
    dims = jpeg_dimensions(frame or b"")
    if dims:
        return PreparedFrame(frame, dims[0], dims[1], dims[0], dims[1], False)
    return PreparedFrame(frame, 0, 0, 0, 0, False)


def scale_box(box: list | tuple, from_w: int, from_h: int,
              to_w: int, to_h: int) -> list[int]:
    """Map one [left, top, right, bottom] box between pixel spaces."""
    left, top, right, bottom = (float(v) for v in box)
    return [round(left * to_w / from_w), round(top * to_h / from_h),
            round(right * to_w / from_w), round(bottom * to_h / from_h)]


def rescale_detections(detections: list[dict],
                       prep: PreparedFrame | None) -> list[dict]:
    """Map shipped-space boxes back to source pixels (copy, never mutate)."""
    if prep is None or not prep.resized:
        return detections
    out = []
    for det in detections:
        box = det.get("box")
        if (isinstance(box, (list, tuple)) and len(box) == 4
                and prep.dst_w > 0 and prep.dst_h > 0):
            det = {**det, "box": scale_box(
                box, prep.dst_w, prep.dst_h, prep.src_w, prep.src_h)}
        out.append(det)
    return out
