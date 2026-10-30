"""448px frame pipeline: small payloads out, source-pixel boxes back.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_frames -v
"""
import base64
import os
import unittest
from unittest import mock

from backend.app.frames import (
    FRAME_EDGE,
    PreparedFrame,
    jpeg_dimensions,
    prepare_frame,
    rescale_detections,
    scale_box,
)


def _resized_prep() -> PreparedFrame:
    return PreparedFrame(payload=b"small", src_w=1920, src_h=1080,
                         dst_w=448, dst_h=448, resized=True)


class ScaleBoxTest(unittest.TestCase):
    def test_shipped_box_maps_to_source_pixels(self):
        self.assertEqual(
            scale_box([112, 112, 224, 224], 448, 448, 1920, 1080),
            [480, 270, 960, 540])

    def test_round_trip_returns_within_one_pixel(self):
        original = [123, 456, 789, 900]
        shipped = scale_box(original, 1920, 1080, 448, 448)
        back = scale_box(shipped, 448, 448, 1920, 1080)
        for got, want in zip(back, original):
            self.assertLessEqual(abs(got - want), 1)


class RescaleDetectionsTest(unittest.TestCase):
    def test_identity_when_not_resized(self):
        dets = [{"label": "x", "box": [1, 2, 3, 4]}]
        prep = PreparedFrame(b"f", 32, 24, 32, 24, False)
        self.assertIs(rescale_detections(dets, prep), dets)
        self.assertIs(rescale_detections(dets, None), dets)

    def test_maps_boxes_without_mutating_input(self):
        dets = [{"label": "esp32", "box": [112, 112, 224, 224]},
                {"label": "note", "confidence": 0.5},
                {"label": "bad", "box": [1, 2, 3]}]
        out = rescale_detections(dets, _resized_prep())
        self.assertEqual(out[0]["box"], [480, 270, 960, 540])
        self.assertEqual(out[1], {"label": "note", "confidence": 0.5})
        self.assertEqual(out[2], {"label": "bad", "box": [1, 2, 3]})
        self.assertEqual(dets[0]["box"], [112, 112, 224, 224])  # untouched


class JpegDimensionsTest(unittest.TestCase):
    def test_reads_sof_markers(self):
        sof = (b"\xff\xd8"              # SOI
               b"\xff\xc0\x00\x08\x08"  # SOF0, len 8, precision 8
               b"\x02\x80\x01\xe0"      # 640 x 480
               b"\xff\xd9")             # EOI
        self.assertEqual(jpeg_dimensions(sof), (480, 640))

    def test_rejects_non_jpeg(self):
        self.assertIsNone(jpeg_dimensions(b"fakejpeg"))
        self.assertIsNone(jpeg_dimensions(b""))
        self.assertIsNone(jpeg_dimensions(b"\xff\xd8\xff\xd9"))


class PrepareFrameTest(unittest.TestCase):
    def test_passthrough_for_undecodable_bytes(self):
        prep = prepare_frame(b"fakejpeg")
        self.assertFalse(prep.resized)
        self.assertEqual(prep.payload, b"fakejpeg")

    @unittest.skipUnless(__import__("importlib").util.find_spec("cv2"),
                         "opencv not installed")
    def test_downscales_real_jpeg_to_448(self):
        import cv2
        import numpy as np

        img = np.full((640, 960, 3), (24, 60, 32), np.uint8)
        ok, buf = cv2.imencode(".jpg", img)
        self.assertTrue(ok)
        raw = bytes(buf)
        prep = prepare_frame(raw)
        self.assertTrue(prep.resized)
        self.assertEqual((prep.src_w, prep.src_h), (960, 640))
        self.assertEqual(jpeg_dimensions(prep.payload),
                         (FRAME_EDGE, FRAME_EDGE))
        self.assertLess(len(prep.payload), len(raw))


class SamWiringTest(unittest.TestCase):
    def test_analyze_ships_small_frame_and_rescales_boxes(self):
        from backend.app.adapters.sam_adapter import MetaSAMAdapter
        from backend.tests.helpers import (
            FakeSAMClient,
            install_sam_parser_stub,
            sam_box,
            sam_event_stream,
            sam_result,
        )

        prep = _resized_prep()
        install_sam_parser_stub(
            self, [sam_result([sam_box("0", (112, 112, 224, 224), 0.9)])])
        client = FakeSAMClient([sam_event_stream()])
        with mock.patch.dict(os.environ, {"META_API_KEY": "test"}), mock.patch(
                "openai.OpenAI", return_value=client), mock.patch(
                "backend.app.adapters.sam_adapter.prepare_frame",
                return_value=prep):
            out = MetaSAMAdapter(model="test-sam", mock=False).analyze(
                b"bigframe", concepts=[("esp32", "esp32")])
        shipped = client.responses.calls[0]["input"][0]["content"][1]["image_url"]
        shipped_b64 = shipped.split("base64,", 1)[1]
        self.assertEqual(base64.b64decode(shipped_b64), b"small")
        self.assertEqual(out["detections"][0]["box"], [480, 270, 960, 540])
        self.assertIn("shipped 448x448 from 1920x1080", out["notes"])


if __name__ == "__main__":
    unittest.main()
