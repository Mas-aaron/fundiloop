"""Board profiles: pin maps, vocab, and board-aware learning.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_boards -v

No hardware, no network.
"""
import json
import tempfile
import unittest
from unittest import mock

from backend.app import discovery
from backend.app.agent_orchestrator import AgentOrchestrator
from backend.app.adapters.muse_adapter import MetaClaudeAdapter
from backend.app.adapters.sam_adapter import MetaSAMAdapter
from backend.app.adapters.voice_adapter import MetaVoiceAdapter
from backend.app.boards import (
    BOARD_PROFILES,
    DEFAULT_BOARD,
    GUIDED_BOARDS,
    board_key,
    describe_board,
    get_board,
    pin_map_for,
)
from backend.app.capabilities import (
    AVAILABLE_TESTS,
    KNOWN_PARTS,
    SAM_PROMPT_PARTS,
    SENSOR_INTERFACES,
)
from backend.app.change_detector import MotionSettler
from backend.app.circuit_graph import load_circuit_graph
from backend.app.models import CheckNowRequest, Mode
from backend.app.serial_probe import SerialProbe
from backend.tests.helpers import make_camera, use_mock_env


class BoardRegistryTest(unittest.TestCase):
    def test_profiles_have_required_keys(self):
        for key, prof in BOARD_PROFILES.items():
            for field in ("label", "mcu", "voltage", "i2c", "led_builtin",
                          "gpio", "adc", "serial_baud"):
                self.assertIn(field, prof, "%s.%s" % (key, field))
            self.assertIn("sda", prof["i2c"], key)
            self.assertIn("scl", prof["i2c"], key)

    def test_default_and_guided_subset(self):
        self.assertEqual(DEFAULT_BOARD, "esp32_devkit")
        for key in GUIDED_BOARDS:
            self.assertIn(key, BOARD_PROFILES)

    def test_unknown_board_raises(self):
        with self.assertRaises(ValueError):
            get_board("raspberry_pi")
        self.assertEqual(board_key(None), "esp32_devkit")
        self.assertEqual(board_key("  ARDUINO_UNO "), "arduino_uno")

    def test_pin_maps(self):
        esp = pin_map_for("esp32_devkit")
        self.assertEqual((esp["sda"], esp["scl"], esp["led_gpio"]), (21, 22, 2))
        uno = pin_map_for("arduino_uno")
        self.assertEqual((uno["sda"], uno["scl"], uno["led_gpio"]), ("A4", "A5", 13))
        self.assertEqual(uno["voltage"], "5V")

    def test_describe_board_names_pins(self):
        text = describe_board("arduino_uno")
        self.assertIn("Arduino Uno", text)
        self.assertIn("A4", text)
        self.assertIn("13", text)


class VocabTest(unittest.TestCase):
    def test_sam_list_groundable_and_ordered(self):
        self.assertEqual(SAM_PROMPT_PARTS[0], "esp32")
        for part in SAM_PROMPT_PARTS:
            self.assertIn(part, KNOWN_PARTS)

    def test_sensor_vocab_present(self):
        for part in ("arduino_uno", "dht11", "ds18b20", "hc_sr04", "pir",
                     "mq2", "ldr", "relay", "rgb_led", "lcd1602"):
            self.assertIn(part, KNOWN_PARTS)

    def test_interfaces_map_to_real_tests(self):
        for part, spec in SENSOR_INTERFACES.items():
            self.assertIn(part, KNOWN_PARTS)
            for word in spec["verify"].replace(" then ", " ").split():
                self.assertIn(word, AVAILABLE_TESTS, part)


class FakeClaude:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete_text(self, system, user, image_b64=None, **kwargs):
        self.calls.append({"system": system, "user": user})
        return self.script[min(len(self.calls) - 1, len(self.script) - 1)]


def make_orc(tc):
    tmp = tempfile.TemporaryDirectory()
    tc.addCleanup(tmp.cleanup)
    return AgentOrchestrator(
        graph=load_circuit_graph(), camera=make_camera(tmp.name),
        settler=MotionSettler(settle_seconds=1.0), probe=SerialProbe(mock=True),
        sam=MetaSAMAdapter(mock=True), Muse=MetaClaudeAdapter(mock=True),
        voice=MetaVoiceAdapter(mock=True),
        attempt_log_path="%s/a.jsonl" % tmp.name)


class BoardAwareLearningTest(unittest.TestCase):
    def test_plan_prompt_carries_board(self):
        Muse = FakeClaude([json.dumps({"projects": []})])
        with mock.patch("backend.app.discovery.MetaClaudeAdapter",
                        return_value=Muse):
            out = discovery.plan_projects(
                ["arduino_uno", "dht22"], user_project="temp logger",
                board="arduino_uno")
        self.assertEqual(out["projects"], [])
        system = Muse.calls[0]["system"]
        self.assertIn("Arduino Uno", system)
        self.assertIn("A4", system)
        self.assertIn("dht22", system)

    def test_uno_learning_session_pins_and_firmware(self):
        use_mock_env(self)
        orc = make_orc(self)
        v = orc.start_learning_session(
            "blink", [{"n": 1, "instruction": "Wire the LED to pin 13.",
                       "verify": "CHECK_GPIO", "expect": {},
                       "depends_on_parts": ["led"]}],
            visible_parts=["arduino_uno", "led"], session_id="uno-1",
            project_title="Uno blink", board="arduino_uno")
        self.assertEqual(v["board"], "arduino_uno")
        self.assertEqual(orc.session["pin_map"]["led_gpio"], 13)
        done = orc.check_now(CheckNowRequest())
        self.assertEqual(done["state"], "DONE")
        from backend.app.learning import generate_code

        code = generate_code(orc.session, orc.Muse)
        self.assertIn("#define LED_PIN 13", code)
        self.assertIn("Arduino Uno", code)

    def test_unknown_board_rejected(self):
        use_mock_env(self)
        orc = make_orc(self)
        with self.assertRaises(ValueError):
            orc.start_learning_session(
                "x", [{"n": 1, "instruction": "i", "verify": "CHECK_GPIO",
                       "expect": {}, "depends_on_parts": []}],
                visible_parts=[], board="raspberry_pi")

    def test_guided_and_diagnostic_stay_esp32(self):
        use_mock_env(self)
        orc = make_orc(self)
        with self.assertRaises(ValueError):
            orc.start_session(Mode.GUIDED_ASSEMBLY, board="arduino_uno")
        with self.assertRaises(ValueError):
            orc.start_session(Mode.DIAGNOSTIC, board="arduino_uno")
        v = orc.start_session(Mode.GUIDED_ASSEMBLY)
        self.assertEqual(v["board"], "esp32_devkit")


class BoardHandoffTest(unittest.TestCase):
    def start_guided(self, glance_json):
        use_mock_env(self)
        orc = make_orc(self)
        orc.start_session(Mode.GUIDED_ASSEMBLY)
        orc.Muse.complete_text = lambda *a, **k: glance_json
        return orc

    def test_glance_hands_off_uno(self):
        orc = self.start_guided(json.dumps(
            {"board": "arduino_uno", "confidence": 0.9,
             "parts": ["arduino_uno", "led"]}))
        decided = []
        orig = orc.Muse.decide
        orc.Muse.decide = lambda ctx: (decided.append(1), orig(ctx))[1]
        v = orc.check_now(CheckNowRequest())
        self.assertEqual(v["handoff"]["board"], "arduino_uno")
        self.assertIn("led", v["handoff"]["parts"])
        self.assertIsNone(v["verification"])
        self.assertEqual(decided, [])  # no wasted reasoning
        self.assertIn("START LEARNING", v["speak"])
        self.assertEqual(v["step_index"], 0)  # nothing advanced

    def test_glance_cached_after_first_turn(self):
        calls = []
        orc = make_orc(self)
        use_mock_env(self)
        orc.start_session(Mode.GUIDED_ASSEMBLY)
        raw = json.dumps({"board": "arduino_uno", "confidence": 0.9,
                          "parts": []})
        orc.Muse.complete_text = lambda *a, **k: (calls.append(1), raw)[1]
        orc.check_now(CheckNowRequest())
        orc.check_now(CheckNowRequest())
        self.assertEqual(len(calls), 1)

    def test_esp32_glance_proceeds(self):
        orc = self.start_guided(json.dumps(
            {"board": "esp32_devkit", "confidence": 0.95, "parts": ["esp32"]}))
        v = orc.check_now(CheckNowRequest())
        self.assertNotIn("handoff", v)
        self.assertEqual(v["step_index"], 1)

    def test_none_and_bad_glance_proceed(self):
        for raw in ("{}", "not json", "", "[]"):
            orc = self.start_guided(raw)
            v = orc.check_now(CheckNowRequest())
            self.assertNotIn("handoff", v, raw)

    def test_low_confidence_proceeds(self):
        orc = self.start_guided(json.dumps(
            {"board": "arduino_uno", "confidence": 0.3, "parts": []}))
        v = orc.check_now(CheckNowRequest())
        self.assertNotIn("handoff", v)

    def test_unknown_board_label_hands_off_without_profile(self):
        orc = self.start_guided(json.dumps(
            {"board": "stm32", "confidence": 0.85, "parts": ["stm32"]}))
        v = orc.check_now(CheckNowRequest())
        self.assertIsNone(v["handoff"]["board"])
        self.assertEqual(v["handoff"]["board_label"], "stm32")
        self.assertIn("profile", v["instruction"])

    def test_parse_board_glance(self):
        from backend.app.prompts import parse_board_glance

        out = parse_board_glance(json.dumps(
            {"board": "Arduino_Uno", "confidence": 1.5, "parts": ["led", " "]}))
        self.assertEqual(out["board"], "arduino_uno")
        self.assertEqual(out["confidence"], 1.0)
        self.assertEqual(out["parts"], ["led"])
        self.assertEqual(parse_board_glance("nope")["board"], "none")
        self.assertEqual(parse_board_glance("")["parts"], [])


if __name__ == "__main__":
    unittest.main()
