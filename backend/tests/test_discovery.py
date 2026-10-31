"""Generative discovery: scene understanding + validated plans, no catalog.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_discovery -v

No hardware, no network: the SAM and Muse clients are monkeypatched.
"""
import json
import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from backend.app import discovery
from backend.app import main as app_main
from backend.app.camera_client import CameraClient
from backend.tests.helpers import TEST_JPEG, use_mock_env


class FakeSAM:
    """Scripted MetaSAMAdapter: fixed detections per analyze call."""

    def __init__(self, detections, only_prompt=None):
        self.detections = detections
        self.only_prompt = only_prompt
        self.calls = []

    def analyze(self, frame, context=None, frame_path=None, concepts=None):
        self.calls.append({"concepts": concepts})
        if self.only_prompt is not None and concepts != [(self.only_prompt, self.only_prompt)]:
            return {"detections": []}
        return {"detections": [dict(d) for d in self.detections]}


class FakeClaude:
    """Scripted MetaClaudeAdapter: pop a completion per call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete_text(self, system, user, image_b64=None):
        self.calls.append({"system": system, "user": user,
                           "has_image": image_b64 is not None})
        return self.script[min(len(self.calls) - 1, len(self.script) - 1)]


def scene_json(parts, conns=None, uncs=None, notes="n"):
    return json.dumps({"visible_parts": parts,
                       "connections_observed": conns or [],
                       "uncertainties": uncs or [], "notes": notes})


def part(name, conf=0.9):
    return {"name": name, "confidence": conf, "evidence": "seen",
            "state": "loose on bench"}


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.frame_path = os.path.join(self.tmp.name, "frame.jpg")
        with open(self.frame_path, "wb") as fh:
            fh.write(TEST_JPEG)

    def patch_clients(self, sam, Muse):
        for target, fake in (("backend.app.discovery.MetaSAMAdapter", sam),
                             ("backend.app.discovery.MetaClaudeAdapter", Muse)):
            patcher = mock.patch(target, return_value=fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- detect_parts ------------------------------------------------
    def test_detect_parts_merges(self):
        sam = FakeSAM([{"label": "bme280", "box": [1, 2, 3, 4], "confidence": 0.9}])
        Muse = FakeClaude([scene_json([part("bme280"), part("ssd1306", 0.8)])])
        self.patch_clients(sam, Muse)
        out = discovery.detect_parts(self.frame_path)
        names = [p["name"] for p in out["visible_parts"]]
        self.assertIn("bme280", names)
        self.assertIn("ssd1306", names)
        by_name = {p["name"]: p for p in out["visible_parts"]}
        self.assertTrue(by_name["bme280"]["sam_confirmed"])
        self.assertFalse(by_name["ssd1306"]["sam_confirmed"])
        # Prompt calls overlap in threads: esp32 is grounded, in any order.
        self.assertIn([("esp32", "esp32")],
                      [c["concepts"] for c in sam.calls])

    # -- plan_projects validation ------------------------------------
    def test_plan_rejects_unknown_test(self):
        bad = json.dumps({"projects": [{
            "id": "p1", "title": "Magic", "summary": "s",
            "steps": [{"n": 1, "instruction": "wave wand",
                       "verify": "MAGIC_TEST", "expect": {},
                       "depends_on_parts": ["esp32"]}],
            "missing_parts": [], "alternatives": []}]})
        Muse = FakeClaude([bad, bad])
        self.patch_clients(FakeSAM([]), Muse)
        res = discovery.plan_projects(["esp32"], None, None)
        self.assertEqual(res["projects"], [])
        self.assertEqual(len(Muse.calls), 2)
        self.assertIn("MAGIC_TEST", Muse.calls[1]["user"])
        reasons = [r for e in res["rejected_plans"] for r in e["reasons"]]
        self.assertTrue(any("MAGIC_TEST" in r for r in reasons))

    def test_plan_rejects_unavailable_part(self):
        plan = json.dumps({"projects": [{
            "id": "p1", "title": "Servo", "summary": "s",
            "steps": [{"n": 1, "instruction": "wire servo",
                       "verify": "CHECK_GPIO", "expect": {"pin": 2},
                       "depends_on_parts": ["servo"]}],
            "missing_parts": [], "alternatives": []}]})
        Muse = FakeClaude([plan, plan])
        self.patch_clients(FakeSAM([]), Muse)
        res = discovery.plan_projects(["esp32", "bme280"], None, None)
        self.assertEqual(res["projects"], [])
        reasons = [r for e in res["rejected_plans"] for r in e["reasons"]]
        self.assertTrue(any("servo" in r for r in reasons))

    def test_plan_accepts_valid(self):
        plan = {"projects": [{
            "id": "wx", "title": "Weather station", "summary": "T/H on sensors",
            "steps": [
                {"n": 1, "instruction": "Wire BME280 and OLED to the I2C bus.",
                 "verify": "RUN_I2C_SCAN", "expect": {"addresses": ["0x76", "0x3C"]},
                 "depends_on_parts": ["bme280", "ssd1306"]},
                {"n": 2, "instruction": "Read temperature and humidity.",
                 "verify": "READ_SENSOR", "expect": {"temperature_c": "10..40"},
                 "depends_on_parts": ["bme280"]},
                {"n": 3, "instruction": "Show the readings on the OLED.",
                 "verify": "CHECK_OLED", "expect": {"ack": True},
                 "depends_on_parts": ["ssd1306"]},
            ],
            "missing_parts": [], "alternatives": []}]}
        Muse = FakeClaude([json.dumps(plan)])
        self.patch_clients(FakeSAM([]), Muse)
        res = discovery.plan_projects(["esp32", "bme280", "ssd1306"],
                                      "weather station", None)
        self.assertEqual(len(res["projects"]), 1)
        self.assertEqual(res["rejected_plans"], [])
        self.assertRegex(res["projects"][0]["session_id"], r"^[0-9a-f]{32}$")

    # -- understand_scene --------------------------------------------
    def test_understand_scene_parses_full_json(self):
        Muse = FakeClaude([scene_json(
            [part("esp32")],
            conns=[{"from": "esp32", "to": "breadboard", "confidence": 0.7,
                    "evidence": "wire visible"}],
            uncs=["LED polarity unclear"], notes="bench")])
        self.patch_clients(FakeSAM([]), Muse)
        out = discovery.understand_scene(self.frame_path)
        for key in ("visible_parts", "connections_observed", "uncertainties",
                    "notes", "sam_prompts_empty", "sam_confirmed_count"):
            self.assertIn(key, out)
        self.assertIsInstance(out["uncertainties"], list)
        self.assertEqual(len(out["visible_parts"]), 1)
        self.assertEqual(len(out["connections_observed"]), 1)
        self.assertEqual(out["notes"], "bench")

    def test_understand_scene_marks_unsam_confirmed(self):
        Muse = FakeClaude([scene_json([part("bme280")])])
        self.patch_clients(FakeSAM([]), Muse)
        out = discovery.understand_scene(self.frame_path)
        self.assertFalse(out["visible_parts"][0]["sam_confirmed"])

    def test_understand_scene_adds_unknown_sam_box(self):
        sam = FakeSAM([{"label": "servo", "box": [5, 6, 7, 8], "confidence": 0.8}],
                      only_prompt="servo")
        Muse = FakeClaude([scene_json([part("bme280")])])
        self.patch_clients(sam, Muse)
        out = discovery.understand_scene(self.frame_path)
        unknowns = [p for p in out["visible_parts"] if p["name"] == "unknown"]
        self.assertEqual(len(unknowns), 1)
        self.assertTrue(unknowns[0]["sam_confirmed"])

    def test_understand_scene_handles_bad_json(self):
        Muse = FakeClaude(["not json{{{"])
        self.patch_clients(FakeSAM([]), Muse)
        out = discovery.understand_scene(self.frame_path)  # must not raise
        self.assertEqual(out["visible_parts"], [])
        self.assertEqual(out["connections_observed"], [])
        self.assertTrue(out["uncertainties"])
        self.assertIn("scene understanding failed", out["uncertainties"][0])

    def test_scene_prompts_overlap(self):
        import threading
        import time

        events = []
        lock = threading.Lock()
        base = FakeSAM([])
        real_analyze = base.analyze

        def slow(frame, context=None, frame_path=None, concepts=None):
            with lock:
                events.append(("enter", tuple(concepts or [])))
            try:
                time.sleep(0.3)
                return real_analyze(frame, context=context,
                                    frame_path=frame_path, concepts=concepts)
            finally:
                with lock:
                    events.append(("exit", tuple(concepts or [])))

        base.analyze = slow
        Muse = FakeClaude([scene_json([part("esp32")])])
        self.patch_clients(base, Muse)
        t0 = time.monotonic()
        out = discovery.understand_scene(self.frame_path)
        wall = time.monotonic() - t0
        self.assertIn("esp32", [p["name"] for p in out["visible_parts"]])
        enters = [e for e in events if e[0] == "enter"]
        self.assertGreater(len(enters), 2)
        # Overlap proof: several prompts entered before the first finished.
        first_exit = next(i for i, e in enumerate(events) if e[0] == "exit")
        prior = [e for e in events[:first_exit] if e[0] == "enter"]
        self.assertGreater(len(prior), 1)
        # Sequential wall time is physically >= the sum of the sleeps.
        self.assertLess(wall, len(enters) * 0.3)

    def test_sam_prompt_failure_degrades_gracefully(self):
        base = FakeSAM([{"label": "esp32", "box": [1, 2, 3, 4],
                         "confidence": 0.9}])
        real_analyze = base.analyze

        def flaky(frame, context=None, frame_path=None, concepts=None):
            if concepts == [("led", "led")]:
                raise RuntimeError("sam hiccup")
            return real_analyze(frame, context=context,
                                frame_path=frame_path, concepts=concepts)

        base.analyze = flaky
        Muse = FakeClaude([scene_json([part("esp32")])])
        self.patch_clients(base, Muse)
        out = discovery.understand_scene(self.frame_path)  # must not raise
        self.assertIn("esp32", [p["name"] for p in out["visible_parts"]])
        self.assertTrue(any("led" in u for u in out["uncertainties"]),
                        out["uncertainties"])

    def test_understand_scene_muse_failure_returns_uncertainties(self):
        class FailingClaude(FakeClaude):
            def complete_text(self, system, user, image_b64=None):
                raise Exception("api down")

        self.patch_clients(FakeSAM([]), FailingClaude([]))
        out = discovery.understand_scene(self.frame_path)  # must not raise
        self.assertEqual(out["visible_parts"], [])
        self.assertTrue(any("discovery unavailable" in u for u in out["uncertainties"]))

    def test_scene_image_ships_downscaled(self):
        import base64

        from backend.app.frames import PreparedFrame

        small = b"\xff\xd8smallthumb"
        seen = {}

        class CapturingClaude(FakeClaude):
            def complete_text(self, system, user, image_b64=None):
                seen["image_b64"] = image_b64
                return super().complete_text(system, user,
                                             image_b64=image_b64)

        Muse = CapturingClaude([scene_json([part("esp32")])])
        self.patch_clients(FakeSAM([]), Muse)
        tiny = PreparedFrame(small, 100, 100, 100, 100, True)
        with mock.patch("backend.app.discovery.prepare_frame",
                        return_value=tiny):
            discovery.understand_scene(self.frame_path)
        # The scene call carries the 448px still, never the full upload.
        self.assertEqual(base64.b64decode(seen["image_b64"]), small)

    # -- endpoints ----------------------------------------------------
    def test_discover_parts_backward_compatible(self):
        from backend.app.main import api_discover_parts

        Muse = FakeClaude([scene_json([part("esp32")])])
        self.patch_clients(FakeSAM([]), Muse)
        res = api_discover_parts({"frame_path": self.frame_path},
                                 include_scene=False)
        self.assertEqual(set(res.keys()), {"visible_parts"})
        res = api_discover_parts({"frame_path": self.frame_path},
                                 include_scene=True)
        self.assertIn("uncertainties", res)

    def test_plan_uses_connections(self):
        plan = {"projects": [{
            "id": "p1", "title": "T", "summary": "s",
            "steps": [{"n": 1, "instruction": "scan bus",
                       "verify": "RUN_I2C_SCAN", "expect": {},
                       "depends_on_parts": ["bme280"]}],
            "missing_parts": [], "alternatives": []}]}
        Muse = FakeClaude([json.dumps(plan)])
        self.patch_clients(FakeSAM([]), Muse)
        discovery.plan_projects(
            ["esp32", "bme280"], None, None,
            connections_observed=[{"from": "bme280", "to": "esp32"}],
            uncertainties=["wire route unclear"])
        self.assertIn("observed connections", Muse.calls[0]["system"])

    # -- user request is honored, never silently swapped --------------
    def test_plan_prompt_requires_requested_build_first(self):
        from backend.app.prompts import plan_system_prompt

        from backend.app.capabilities import AVAILABLE_TESTS

        system = plan_system_prompt(AVAILABLE_TESTS, ["esp32"], [], [],
                                    "weather station", None, None)
        self.assertIn("FIRST project MUST be", system)
        self.assertIn("weather station", system)
        free = plan_system_prompt(AVAILABLE_TESTS, ["esp32"], [], [],
                                  None, None, None)
        self.assertNotIn("FIRST project MUST be", free)

    def test_plan_retry_keeps_user_project(self):
        bad = json.dumps({"projects": [{
            "id": "p1", "title": "Magic", "summary": "s",
            "steps": [{"n": 1, "instruction": "wave wand",
                       "verify": "MAGIC_TEST", "expect": {},
                       "depends_on_parts": ["esp32"]}],
            "missing_parts": [], "alternatives": []}]})
        good = json.dumps({"projects": [{
            "id": "p1", "title": "Magic wand", "summary": "s",
            "steps": [{"n": 1, "instruction": "blink",
                       "verify": "CHECK_GPIO", "expect": {},
                       "depends_on_parts": ["led"]}],
            "missing_parts": [], "alternatives": []}]})
        Muse = FakeClaude([bad, good])
        self.patch_clients(FakeSAM([]), Muse)
        res = discovery.plan_projects(["esp32", "led"], "magic wand", None)
        self.assertEqual(len(res["projects"]), 1)
        self.assertIn("without swapping", Muse.calls[1]["user"])
        self.assertIn("magic wand", Muse.calls[1]["user"])

    def test_plan_hint_when_request_unmatched(self):
        plan = json.dumps({"projects": [{
            "id": "p1", "title": "Blink", "summary": "s",
            "steps": [{"n": 1, "instruction": "blink",
                       "verify": "CHECK_GPIO", "expect": {},
                       "depends_on_parts": ["led"]}],
            "missing_parts": [], "alternatives": []}]})
        Muse = FakeClaude([plan])
        self.patch_clients(FakeSAM([]), Muse)
        res = discovery.plan_projects(["esp32", "led"], "weather station",
                                      None)
        self.assertEqual(len(res["projects"]), 1)
        self.assertIn("weather station", res["hint"])
        self.assertIn("esp32", res["hint"])

    def test_plan_no_hint_when_request_covered(self):
        plan = json.dumps({"projects": [{
            "id": "wx", "title": "Weather Station", "summary": "s",
            "steps": [{"n": 1, "instruction": "read sensor",
                       "verify": "READ_SENSOR", "expect": {},
                       "depends_on_parts": ["bme280"]}],
            "missing_parts": [], "alternatives": []}]})
        Muse = FakeClaude([plan])
        self.patch_clients(FakeSAM([]), Muse)
        res = discovery.plan_projects(["esp32", "bme280"], "weather station",
                                      None)
        self.assertEqual(len(res["projects"]), 1)
        self.assertNotIn("hint", res)

    def test_plan_hint_carries_rejection_reasons(self):
        bad = json.dumps({"projects": [{
            "id": "p1", "title": "Servo arm", "summary": "s",
            "steps": [{"n": 1, "instruction": "wire servo",
                       "verify": "CHECK_GPIO", "expect": {},
                       "depends_on_parts": ["servo"]}],
            "missing_parts": [], "alternatives": []}]})
        good = json.dumps({"projects": [{
            "id": "p1", "title": "Blink", "summary": "s",
            "steps": [{"n": 1, "instruction": "blink",
                       "verify": "CHECK_GPIO", "expect": {},
                       "depends_on_parts": ["led"]}],
            "missing_parts": [], "alternatives": []}]})
        Muse = FakeClaude([bad, good])
        self.patch_clients(FakeSAM([]), Muse)
        res = discovery.plan_projects(["esp32", "led"], "servo arm", None)
        self.assertEqual(len(res["projects"]), 1)
        self.assertIn("servo", res["hint"])
        self.assertIn("visible parts", res["hint"])


TEST_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128


class DiscoverUploadTest(unittest.TestCase):
    """POST /discover/upload: equipment photo without a camera."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cam = CameraClient(debug_dir=self.tmp.name)
        patcher = mock.patch.object(
            app_main, "orchestrator", SimpleNamespace(camera=cam))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_upload_jpeg_returns_saved_frame_path(self):
        out = app_main.api_discover_upload(TEST_JPEG)
        self.assertTrue(out["frame_path"].endswith(".jpg"))
        self.assertTrue(os.path.isfile(out["frame_path"]))
        # The upload pins nothing: no live override is set.
        self.assertIsNone(app_main.orchestrator.camera._override)

    def test_upload_png_keeps_png_suffix(self):
        out = app_main.api_discover_upload(TEST_PNG)
        self.assertTrue(out["frame_path"].endswith(".png"))
        self.assertTrue(os.path.isfile(out["frame_path"]))

    def test_upload_rejects_garbage_and_oversize(self):
        with self.assertRaises(HTTPException) as ctx:
            app_main.api_discover_upload(b"not an image")
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException) as ctx:
            app_main.api_discover_upload(b"\xff\xd8" + b"\x00" * (6 * 1024 * 1024))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_saved_png_frame_serves_with_png_media_type(self):
        name = "frame_test.png"
        with open(os.path.join(self.tmp.name, name), "wb") as fh:
            fh.write(TEST_PNG)
        resp = app_main.api_saved_frame(name)
        self.assertEqual(resp.media_type, "image/png")
        with self.assertRaises(HTTPException) as ctx:
            app_main.api_saved_frame("frame_test.txt")
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
