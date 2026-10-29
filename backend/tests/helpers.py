"""Shared fixtures for offline tests: a valid JPEG frame plus helpers.

TEST_JPEG is a small valid JPEG (OpenCV-encoded when available) pinned via
CameraClient.set_override, the same path a real phone snap takes. It is test
input, not a simulated camera feed: make_camera() reports a test source and
every capture still goes through real JPEG validation + debug-frame saving.

The SAM fakes below stand in for the meta_sam_parser package and the
streaming Responses transport so the adapter's own code (cookbook request
shape, event-lane closing, record mapping, logging) is tested without keys.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest import mock


def _build_jpeg() -> bytes:
    try:
        import cv2
        import numpy as np

        img = np.full((24, 32, 3), (24, 60, 32), np.uint8)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            return bytes(buf)
    except Exception:
        pass
    # No OpenCV: SOI..EOI padding passes JPEG validation; the settler passes
    # frames through undecided without cv2 anyway.
    return b"\xff\xd8" + b"\x00" * 128 + b"\xff\xd9"


TEST_JPEG = _build_jpeg()


def use_mock_env(tc) -> None:
    """Force MOCK_META=true for one test case (mock adapters need it)."""
    patcher = mock.patch.dict(os.environ, {"MOCK_META": "true"})
    patcher.start()
    tc.addCleanup(patcher.stop)


def make_camera(tmpdir: str):
    """CameraClient that serves TEST_JPEG without touching the network."""
    from backend.app.camera_client import CameraClient

    cam = CameraClient(debug_dir=tmpdir)
    cam.set_override(TEST_JPEG)
    cam.get_latest_preview_frame = lambda timeout=8.0: TEST_JPEG  # noqa: E731
    cam.describe_source = lambda: "camera test-fixture"  # noqa: E731
    return cam


def sam_event_stream(text: str = "grammar", response_id: str = "resp-test"):
    """Canned SSE dicts in the shape the live SAM API emits."""
    return [
        {"type": "response.created", "response": {"id": response_id}},
        {"type": "response.output_text.delta", "item_id": "msg-1",
         "output_index": 0, "content_index": 0, "delta": text},
        {"type": "response.content_part.done", "item_id": "msg-1",
         "output_index": 0, "content_index": 0,
         "part": {"type": "output_text", "text": text}},
        {"type": "response.completed", "response": {"id": response_id}},
    ]


class FakeSAMStream:
    """Sync stream object: `with` + iteration over dict events."""

    def __init__(self, events):
        self._events = list(events)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter(self._events)


class FakeSAMResponses:
    """Scripted responses endpoint: one event list (or error) per create."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeSAMStream(item)


class FakeSAMClient:
    """OpenAI stand-in with a scripted responses endpoint."""

    def __init__(self, script):
        self.responses = FakeSAMResponses(script)
        self.init_kwargs = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def sam_box(object_id="0", box=(1, 2, 3, 4), confidence=0.9, frame_index=None):
    left, top, right, bottom = box
    frame = None if frame_index is None else SimpleNamespace(frame_index=frame_index)
    return SimpleNamespace(kind="box", object_id=object_id, left=left, top=top,
                           right=right, bottom=bottom, confidence=confidence,
                           frame=frame)


def sam_mask(object_id="0"):
    return SimpleNamespace(kind="mask", object_id=object_id,
                           mask=SimpleNamespace(encoding="rle", width=8,
                                                height=8),
                           confidence=0.9, frame=None)


def sam_result(records, status="completed", reason=""):
    return SimpleNamespace(records=list(records), media={},
                           outcome=SimpleNamespace(status=status, reason=reason),
                           diagnostics=[])


def install_sam_parser_stub(tc, results):
    """Stub the meta_sam_parser package; results are popped per concept.

    Returns (stub, pushed_texts). The stub records every push() chunk so
    delta concatenation is exercised.
    """
    pushed: list = []
    queue = list(results)

    class FakeParser:
        def push(self, text):
            pushed.append(text)
            return []

        def finish(self, outcome):
            return SimpleNamespace(events=[], result=queue.pop(0))

    stub = SimpleNamespace(
        image_segmentation_format=lambda: SimpleNamespace(
            create_parser=lambda: FakeParser()),
        video_segmentation_format=lambda: SimpleNamespace(
            create_parser=lambda: FakeParser()),
        CompletedOutcome=lambda: "completed",
        decode_mask_to_svg_path=lambda mask: "<svg-stub>",
    )
    patcher = mock.patch.dict(sys.modules, {"meta_sam_parser": stub})
    patcher.start()
    tc.addCleanup(patcher.stop)
    return stub, pushed
