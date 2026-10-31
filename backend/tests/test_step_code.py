"""Per-step sketches + patient sketch endpoint.

Every lesson step serves copy-paste firmware: instant board-aware
templates for known verify types, the patient sketch itself for
CHECK_SERIAL, model output (cached) for the rest, and a generic echo
sketch whenever the model is unavailable or refuses. Project firmware
is cached per session too: refetching unchanged code never burns
another model call. No weights, no network, no model calls in these
tests.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_step_code -v
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.app.boards import pin_map_for
from backend.app.learning import (
    generate_code,
    generate_step_code,
    read_patient_sketch,
)
from backend.app.prompts import learning_step_code_prompt
from backend.tests.test_fundi import StubBrain


def make_session(board="esp32_devkit", steps=None):
    return {"board": board, "pin_map": pin_map_for(board),
            "plan": steps if steps is not None else [
                {"n": 1, "instruction": "Blink.", "verify": "CHECK_GPIO",
                 "expect": {}, "depends_on_parts": []}],
            "step_code": {}}


def boom(*args, **kwargs):
    raise AssertionError("model called where a template must answer")


class StepTemplateTest(unittest.TestCase):
    def test_blink_uses_board_led(self):
        brain = SimpleNamespace(mock=True, complete_text=boom)
        code, source = generate_step_code(
            make_session("esp32_devkit"), {"n": 1, "verify": "CHECK_GPIO"},
            brain)
        self.assertEqual(source, "template")
        self.assertIn("#define LED_PIN 2", code)
        self.assertIn("void setup", code)
        code, _ = generate_step_code(
            make_session("arduino_uno"), {"n": 2, "verify": "check_gpio"},
            brain)
        self.assertIn("#define LED_PIN 13", code)

    def test_i2c_scan_is_board_aware(self):
        brain = SimpleNamespace(mock=True, complete_text=boom)
        code, source = generate_step_code(
            make_session("esp32_devkit"), {"n": 1, "verify": "RUN_I2C_SCAN"},
            brain)
        self.assertEqual(source, "template")
        self.assertIn("Wire.begin(SDA_PIN, SCL_PIN);", code)
        self.assertIn("#define SDA_PIN 21", code)
        uno, _ = generate_step_code(
            make_session("arduino_uno"), {"n": 1, "verify": "RUN_I2C_SCAN"},
            brain)
        self.assertIn("Wire.begin();", uno)
        self.assertNotIn("Wire.begin(SDA_PIN", uno)
        self.assertIn("#define SDA_PIN A4", uno)

    def test_sensor_and_oled_templates(self):
        brain = SimpleNamespace(mock=True, complete_text=boom)
        session = make_session("esp32_devkit")
        code, source = generate_step_code(
            session, {"n": 1, "verify": "READ_SENSOR"}, brain)
        self.assertEqual(source, "template")
        self.assertIn("BME280", code)
        self.assertIn("0x76", code)
        code, source = generate_step_code(
            session, {"n": 2, "verify": "CHECK_OLED"}, brain)
        self.assertEqual(source, "template")
        self.assertIn("SSD1306", code)
        self.assertIn("0x3C", code)

    def test_serial_serves_patient_sketch(self):
        brain = SimpleNamespace(mock=True, complete_text=boom)
        code, source = generate_step_code(
            make_session("esp32_devkit"), {"n": 1, "verify": "CHECK_SERIAL"},
            brain)
        self.assertEqual(source, "patient")
        self.assertEqual(code, read_patient_sketch())
        self.assertIn('"cmd":"PING"', code.replace(" ", ""))

    def test_unknown_verify_mock_uses_generic(self):
        brain = SimpleNamespace(mock=True, complete_text=boom)
        code, source = generate_step_code(
            make_session("esp32_devkit"), {"n": 9, "verify": "WIGGLE"},
            brain)
        self.assertEqual(source, "generic")
        self.assertIn("READY", code)

    def test_unknown_verify_live_model_cached(self):
        session = make_session("esp32_devkit")
        brain = StubBrain(["// model sketch"])
        code, source = generate_step_code(
            session, {"n": 9, "verify": "WIGGLE"}, brain)
        self.assertEqual((code, source), ("// model sketch", "model"))
        again, _ = generate_step_code(
            session, {"n": 9, "verify": "WIGGLE"}, brain)
        self.assertEqual(again, "// model sketch")
        self.assertEqual(len(brain.calls), 1)

    def test_model_refusal_falls_back_to_generic(self):
        brain = StubBrain(['{"refusal": "Loud refusal triggered"}'])
        code, source = generate_step_code(
            make_session("esp32_devkit"), {"n": 9, "verify": "WIGGLE"},
            brain)
        self.assertEqual(source, "generic")
        self.assertIn("READY", code)

    def test_auto_attach_skips_uncached_model(self):
        brain = SimpleNamespace(mock=False, complete_text=boom)
        code, source = generate_step_code(
            make_session("esp32_devkit"), {"n": 9, "verify": "WIGGLE"},
            brain, allow_model=False)
        self.assertEqual((code, source), ("", "none"))

    def test_auto_attach_uses_cached_model(self):
        session = make_session("esp32_devkit")
        session["step_code"][9] = {"code": "// cached", "source": "model"}
        brain = SimpleNamespace(mock=False, complete_text=boom)
        code, source = generate_step_code(
            session, {"n": 9, "verify": "WIGGLE"}, brain, allow_model=False)
        self.assertEqual((code, source), ("// cached", "model"))

    def test_step_prompt_carries_pins(self):
        text = learning_step_code_prompt(
            {"n": 1, "instruction": "Blink.", "verify": "CHECK_GPIO"},
            pin_map_for("esp32_devkit"))
        self.assertIn("SDA=21", text)
        self.assertIn("LED=2", text)
        self.assertIn("Blink.", text)


class ProjectCodeCacheTest(unittest.TestCase):
    def make_live_session(self):
        return {"project_title": "Blink", "project_id": "blink",
                "board": "esp32_devkit",
                "pin_map": pin_map_for("esp32_devkit"),
                "plan": [{"n": 1, "instruction": "Blink.",
                          "verify": "CHECK_GPIO", "expect": {},
                          "depends_on_parts": ["led"]}],
                "visible_parts": ["esp32", "led"],
                "verified": {"i2c": [], "steps": []}}

    def test_refetch_returns_cached_firmware(self):
        session = self.make_live_session()
        brain = StubBrain(["// live firmware"])
        first = generate_code(session, brain)
        second = generate_code(session, brain)
        self.assertEqual(first, "// live firmware")
        self.assertEqual(second, "// live firmware")
        self.assertEqual(len(brain.calls), 1)

    def test_new_evidence_invalidates_cache(self):
        session = self.make_live_session()
        brain = StubBrain(["// v1", "// v2"])
        self.assertEqual(generate_code(session, brain), "// v1")
        session["verified"]["steps"].append(
            {"n": 1, "test": "CHECK_GPIO", "passed": True,
             "measured": "GPIO2 toggles"})
        self.assertEqual(generate_code(session, brain), "// v2")
        self.assertEqual(len(brain.calls), 2)

    def test_draft_and_final_cache_separately(self):
        session = self.make_live_session()
        brain = StubBrain(["// body", "// body"])
        draft = generate_code(session, brain, draft=True)
        final = generate_code(session, brain, draft=False)
        self.assertIn("// DRAFT", draft)
        self.assertNotIn("// DRAFT", final)
        self.assertEqual(len(brain.calls), 2)
        # Refetching either hits its own cache entry, not the other's.
        self.assertEqual(generate_code(session, brain, draft=True), draft)
        self.assertEqual(generate_code(session, brain, draft=False), final)
        self.assertEqual(len(brain.calls), 2)


class StepCodeEndpointTest(unittest.TestCase):
    def setUp(self):
        import backend.app.main as app_main

        self.app_main = app_main
        self.session = make_session("esp32_devkit")
        self.session["learning"] = True
        fake_orc = SimpleNamespace(
            sessions={"s1": self.session}, Muse=SimpleNamespace(mock=True))
        patcher = mock.patch.object(app_main, "orchestrator", fake_orc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_step_code_serves_template(self):
        out = self.app_main.api_step_code("s1", 1)
        self.assertEqual(out["source"], "template")
        self.assertIn("LED_PIN", out["code"])
        self.assertEqual(out["n"], 1)

    def test_unknown_session_is_404(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_code("nope", 1)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_unknown_step_is_404(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_code("s1", 7)
        self.assertEqual(ctx.exception.status_code, 404)


class PatientSketchEndpointTest(unittest.TestCase):
    def test_sketch_served_from_repo(self):
        import backend.app.main as app_main

        out = app_main.api_patient_sketch()
        self.assertEqual(out["filename"], "patient_board.ino")
        self.assertEqual(out["code"], read_patient_sketch())
        self.assertIn("PING", out["code"])

    def test_missing_sketch_is_404(self):
        import tempfile

        import backend.app.learning as learning_mod
        import backend.app.main as app_main
        from fastapi import HTTPException

        with tempfile.TemporaryDirectory() as tmp:
            missing = __import__("os").path.join(tmp, "missing.ino")
            with mock.patch.object(learning_mod, "PATIENT_SKETCH", missing):
                with self.assertRaises(HTTPException) as ctx:
                    app_main.api_patient_sketch()
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
