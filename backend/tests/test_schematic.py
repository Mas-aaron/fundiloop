"""Per-step wiring schematics for Learning mode (ESP32 only).

Progressive netlists from plan parts + ESP32 pin map + SENSOR_INTERFACES,
with a deterministic SVG renderer. Unknown parts render "unresolved"
with a reason; wiring is never invented. No hardware, no network, no
model calls in these tests.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_schematic -v
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.app.boards import pin_map_for
from backend.app.schematic import _allocate, build_netlist, render_svg


PLAN = [
    {"n": 1, "instruction": "Wire the sensor.", "verify": "RUN_I2C_SCAN",
     "expect": {}, "depends_on_parts": ["bme280", "esp32", "breadboard"]},
    {"n": 2, "instruction": "Add the LED.", "verify": "CHECK_GPIO",
     "expect": {}, "depends_on_parts": ["led", "resistor"]},
    {"n": 3, "instruction": "Add the mystery.", "verify": "CHECK_SERIAL",
     "expect": {}, "depends_on_parts": ["quantum_flux_capacitor"]},
]


class NetlistTest(unittest.TestCase):
    def test_i2c_wiring_uses_pin_map(self):
        net = build_netlist(PLAN, 1, pin_map_for("esp32_devkit"))
        self.assertEqual(net["parts"], ["bme280"])
        self.assertEqual(net["steps_included"], [1])
        wires = {(w["part_pin"], w["board_pin"]) for w in net["wires"]}
        self.assertIn(("SDA", "GPIO21"), wires)
        self.assertIn(("SCL", "GPIO22"), wires)
        self.assertIn(("VCC", "3V3"), wires)
        self.assertIn(("GND", "GND"), wires)
        self.assertEqual(net["unresolved"], [])
        self.assertTrue(any("Shared I2C bus" in n for n in net["notes"]))

    def test_progressive_full_project_is_last_step(self):
        pins = pin_map_for("esp32_devkit")
        step1 = build_netlist(PLAN, 1, pins)
        step2 = build_netlist(PLAN, 2, pins)
        self.assertEqual(step1["parts"], ["bme280"])
        self.assertEqual(step2["parts"], ["bme280", "led"])
        self.assertEqual(step2["steps_included"], [1, 2])
        led = [w for w in step2["wires"] if w["part"] == "led"]
        self.assertIn(("+", "GPIO2"),
                      {(w["part_pin"], w["board_pin"]) for w in led})

    def test_mcu_and_passive_parts_skipped_silently(self):
        net = build_netlist(PLAN, 2, pin_map_for("esp32_devkit"))
        self.assertNotIn("esp32", net["parts"])
        self.assertNotIn("breadboard", net["parts"])
        self.assertNotIn("resistor", net["parts"])
        self.assertEqual([u["part"] for u in net["unresolved"]], [])

    def test_unknown_part_unresolved_never_wired(self):
        net = build_netlist(PLAN, 3, pin_map_for("esp32_devkit"))
        self.assertEqual([u["part"] for u in net["unresolved"]],
                         ["quantum_flux_capacitor"])
        self.assertIn("no wiring profile",
                      net["unresolved"][0]["reason"])
        self.assertFalse([w for w in net["wires"]
                          if w["part"] == "quantum_flux_capacitor"])

    def test_signal_pins_deterministic_and_disjoint(self):
        plan = [{"n": 1, "instruction": "Wire all.", "verify": "CHECK_GPIO",
                 "expect": {},
                 "depends_on_parts": ["button", "dht22", "hc_sr04", "pir",
                                      "rgb_led", "servo", "mq2", "ldr"]}]
        pins = pin_map_for("esp32_devkit")
        first = build_netlist(plan, 1, pins)
        second = build_netlist(plan, 1, pins)
        self.assertEqual(first, second)
        gpios = [w["board_pin"] for w in first["wires"]
                 if w["board_pin"].startswith("GPIO")
                 and w["signal"] not in ("SDA", "SCL")]
        self.assertEqual(len(gpios), len(set(gpios)))
        self.assertNotIn("GPIO21", gpios)
        self.assertNotIn("GPIO22", gpios)
        analog = {w["board_pin"] for w in first["wires"]
                  if w["part"] in ("mq2", "ldr")
                  and w["board_pin"].startswith("GPIO")}
        self.assertTrue(analog <= {"GPIO32", "GPIO33", "GPIO25", "GPIO26",
                                   "GPIO27", "GPIO14", "GPIO12", "GPIO13",
                                   "GPIO4"})

    def test_allocator_reports_exhaustion(self):
        taken: set[int] = set()
        self.assertEqual(_allocate([("SIG", False)], taken,
                                   digital_order=[4]), [4])
        self.assertIsNone(_allocate([("SIG", False)], taken,
                                    digital_order=[4]))

    def test_missing_pin_map_uses_esp32_defaults(self):
        net = build_netlist(PLAN, 1, {})
        wires = {(w["part_pin"], w["board_pin"]) for w in net["wires"]}
        self.assertIn(("SDA", "GPIO21"), wires)
        self.assertIn(("SDA", "GPIO21"), wires)


class SvgTest(unittest.TestCase):
    def test_svg_deterministic_and_labeled(self):
        net = build_netlist(PLAN, 2, pin_map_for("esp32_devkit"))
        first, second = render_svg(net, 2), render_svg(net, 2)
        self.assertEqual(first, second)
        self.assertIn("<svg", first)
        self.assertIn("bme280.SDA", first)
        self.assertIn("GPIO21", first)
        self.assertIn("led.+", first)

    def test_svg_shows_unresolved_dashed(self):
        net = build_netlist(PLAN, 3, pin_map_for("esp32_devkit"))
        svg = render_svg(net, 3)
        self.assertIn("unresolved: quantum_flux_capacitor", svg)
        self.assertIn("stroke-dasharray", svg)

    def test_svg_escapes_part_names(self):
        net = build_netlist(
            [{"n": 1, "instruction": "x", "verify": "CHECK_GPIO",
              "expect": {}, "depends_on_parts": ["<b>evil"]} ,],
            1, pin_map_for("esp32_devkit"))
        svg = render_svg(net, 1)
        self.assertNotIn("<b>evil", svg)
        self.assertIn("&lt;b&gt;evil", svg)

    def test_svg_empty_step_has_placeholder(self):
        net = build_netlist(
            [{"n": 1, "instruction": "Look.", "verify": "CHECK_SERIAL",
              "expect": {}, "depends_on_parts": ["esp32"]}],
            1, pin_map_for("esp32_devkit"))
        svg = render_svg(net, 1)
        self.assertIn("No external wiring", svg)
        self.assertIn("Parts: esp32", svg)
        self.assertIn("esp32_devkit", svg)  # board box still draws

    def test_no_wiring_step_lists_requested_and_note(self):
        net = build_netlist(
            [{"n": 2, "instruction": "Blink.", "verify": "CHECK_GPIO",
              "expect": {}, "depends_on_parts": ["esp32", "breadboard"]}],
            2, pin_map_for("esp32_devkit"))
        self.assertEqual(net["wires"], [])
        self.assertEqual(net["unresolved"], [])
        self.assertEqual(net["requested"], ["breadboard", "esp32"])
        self.assertEqual(len(net["notes"]), 1)
        self.assertIn("no external wiring", net["notes"][0].lower())

    def test_nameless_step_note(self):
        net = build_netlist(
            [{"n": 1, "instruction": "Look.", "verify": "CHECK_SERIAL",
              "expect": {}, "depends_on_parts": []}],
            1, pin_map_for("esp32_devkit"))
        self.assertIn("names no parts", net["notes"][0])

    def test_board_box_draws_with_wires(self):
        net = build_netlist(PLAN, 1, pin_map_for("esp32_devkit"))
        svg = render_svg(net, 1)
        self.assertIn("#eaf2f8", svg)  # board box fill
        self.assertIn("bme280.SDA", svg)

    def test_wires_carry_introducing_step(self):
        net = build_netlist(PLAN, 2, pin_map_for("esp32_devkit"))
        self.assertEqual(net["current_step"], 2)
        added = {(w["part"], w["step_added"]) for w in net["wires"]}
        self.assertTrue(added)
        self.assertTrue(all(step == 1 for part, step in added
                            if part == "bme280"))
        self.assertTrue(all(step == 2 for part, step in added
                            if part == "led"))

    def test_svg_highlights_current_step_and_dims_earlier(self):
        net = build_netlist(PLAN, 2, pin_map_for("esp32_devkit"))
        svg = render_svg(net, 2)
        self.assertIn('id="wire-bme280-SDA" opacity="0.35"', svg)
        self.assertIn('id="wire-led-_" opacity="1"', svg)
        self.assertIn('id="part-bme280"', svg)
        self.assertIn("bright = new in step 2", svg)

    def test_svg_no_dimming_when_all_wires_new(self):
        net = build_netlist(PLAN, 1, pin_map_for("esp32_devkit"))
        svg = render_svg(net, 1)
        self.assertNotIn('opacity="0.35"', svg)
        self.assertIn("progressive to this step", svg)

    def test_svg_breadboard_rails_and_power_nodes(self):
        net = build_netlist(PLAN, 1, pin_map_for("esp32_devkit"))
        svg = render_svg(net, 1)
        self.assertIn('id="breadboard"', svg)
        self.assertIn('id="esp32-board"', svg)
        self.assertIn("<circle", svg)  # power/ground tap the rails
        self.assertIn("red rail = power", svg)

    def test_svg_group_ids_sanitized(self):
        net = build_netlist(
            [{"n": 1, "instruction": "x", "verify": "CHECK_GPIO",
              "expect": {}, "depends_on_parts": ["<b>evil"]}],
            1, pin_map_for("esp32_devkit"))
        svg = render_svg(net, 1)
        self.assertIn('id="unresolved-_b_evil"', svg)
        self.assertNotIn("<b>evil", svg)


class SchematicEndpointTest(unittest.TestCase):
    def setUp(self):
        import backend.app.main as app_main

        self.app_main = app_main
        self.session = {"learning": True, "board": "esp32_devkit",
                        "pin_map": pin_map_for("esp32_devkit"), "plan": PLAN}
        fake_orc = SimpleNamespace(sessions={"s1": self.session})
        patcher = mock.patch.object(app_main, "orchestrator", fake_orc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_schematic_returns_netlist_svg_unresolved(self):
        out = self.app_main.api_step_schematic("s1", 2)
        self.assertEqual(out["n"], 2)
        self.assertEqual(out["netlist"]["parts"], ["bme280", "led"])
        self.assertIn("<svg", out["svg"])
        self.assertEqual(out["unresolved"], [])

    def test_unknown_session_is_404(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_schematic("nope", 1)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_unknown_step_is_404(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_schematic("s1", 9)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_uno_board_is_400(self):
        from fastapi import HTTPException

        self.session["board"] = "arduino_uno"
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_step_schematic("s1", 1)
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
