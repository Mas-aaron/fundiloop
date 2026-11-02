"""Per-step wiring schematics for Learning mode (ESP32 only).

Progressive netlists from plan parts + ESP32 pin map + SENSOR_INTERFACES,
with a deterministic SVG renderer. Unknown parts render "unresolved"
with a reason; wiring is never invented. No hardware, no network, no
model calls in these tests.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_schematic -v
"""
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.app.boards import pin_map_for
from backend.app.schematic import _allocate, build_netlist, render_svg
from backend.app.breadboard_schematic import render_breadboard_svg
from backend.app.visual_schematic import render_visual_svg


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

    def test_svg_hover_titles(self):
        net = build_netlist(PLAN, 2, pin_map_for("esp32_devkit"))
        svg = render_svg(net, 2)
        self.assertIn("<title>bme280 SDA", svg)
        self.assertIn("(added step 1)", svg)
        self.assertIn("<title>Breadboard power rails", svg)
        self.assertIn("<title>esp32_devkit", svg)

    def test_svg_group_ids_sanitized(self):
        net = build_netlist(
            [{"n": 1, "instruction": "x", "verify": "CHECK_GPIO",
              "expect": {}, "depends_on_parts": ["<b>evil"]}],
            1, pin_map_for("esp32_devkit"))
        svg = render_svg(net, 1)
        self.assertIn('id="unresolved-_b_evil"', svg)
        self.assertNotIn("<b>evil", svg)


class VisualSchematicTest(unittest.TestCase):
    def test_visual_draws_netlist_wires_and_step_marks(self):
        net = build_netlist(PLAN, 1, pin_map_for("esp32_devkit"))
        svg = render_visual_svg(net, 1)
        self.assertIn('id="wire-bme280-SDA"', svg)
        self.assertIn('id="part-bme280"', svg)
        self.assertIn('id="part-esp32"', svg)
        self.assertIn('id="part-usb_c"', svg)
        self.assertIn('data-step="1"', svg)
        self.assertIn("ESP32 DevKit v1", svg)
        self.assertNotIn('id="part-led"', svg)

    def test_visual_grows_with_later_steps(self):
        pins = pin_map_for("esp32_devkit")
        svg1 = render_visual_svg(build_netlist(PLAN, 1, pins), 1)
        svg2 = render_visual_svg(build_netlist(PLAN, 2, pins), 2)
        self.assertNotIn('id="part-led"', svg1)
        self.assertIn('id="part-led"', svg2)
        self.assertIn('id="wire-led-_"', svg2)
        self.assertIn('data-step="2"', svg2)
        self.assertIn("schematic-prior", svg2)
        self.assertIn("schematic-new", svg2)

    def test_visual_servo_and_ultrasonic_use_signal_colors(self):
        plan = [{"n": 1, "instruction": "Power.", "verify": "CHECK_SERIAL",
                 "expect": {}, "depends_on_parts": ["esp32"]},
                {"n": 2, "instruction": "Eyes.", "verify": "CHECK_GPIO",
                 "expect": {}, "depends_on_parts": ["hc_sr04"]},
                {"n": 3, "instruction": "Arm.", "verify": "CHECK_GPIO",
                 "expect": {}, "depends_on_parts": ["servo"]}]
        pins = pin_map_for("esp32_devkit")
        svg = render_visual_svg(build_netlist(plan, 3, pins), 3)
        self.assertIn("HC-SR04P Ultrasonic", svg)
        self.assertIn("SG90 Servo", svg)
        self.assertIn("#a855f7", svg)  # servo signal
        self.assertIn("#3b82f6", svg)  # TRIG
        self.assertIn("#22c55e", svg)  # ECHO
        self.assertIn('id="wire-servo-SIG"', svg)
        self.assertIn('id="wire-hc_sr04-TRIG"', svg)


class BreadboardSchematicTest(unittest.TestCase):
    def test_breadboard_has_holes_and_same_wire_ids(self):
        net = build_netlist(PLAN, 1, pin_map_for("esp32_devkit"))
        svg = render_breadboard_svg(net, 1)
        self.assertEqual(svg, render_breadboard_svg(net, 1))
        self.assertIn('id="breadboard-board"', svg)
        self.assertIn('id="wire-bme280-SDA"', svg)
        self.assertIn('id="part-esp32"', svg)
        self.assertIn('id="part-usb_c"', svg)
        for letter in "ABCDEFGHIJ":
            self.assertIn(">%s</text>" % letter, svg)
        self.assertIn("breadboard layout", svg)
        self.assertNotIn("<b>", svg)

    def test_breadboard_grows_with_later_steps(self):
        pins = pin_map_for("esp32_devkit")
        svg1 = render_breadboard_svg(build_netlist(PLAN, 1, pins), 1)
        svg2 = render_breadboard_svg(build_netlist(PLAN, 2, pins), 2)
        self.assertNotIn('id="part-led"', svg1)
        self.assertIn('id="part-led"', svg2)
        self.assertIn('id="wire-led-_"', svg2)
        self.assertIn("schematic-new", svg2)

    def test_breadboard_wires_route_orthogonally(self):
        # Organized wiring like the reference diagram: off-board wire
        # paths are horizontal/vertical segments only — no bezier
        # swoops, no diagonal hops across the board. (The small E-F
        # jumper hop uses Q and is allowed.)
        plan = [
            {"n": 1, "instruction": "Power.", "verify": "CHECK_SERIAL",
             "expect": {}, "depends_on_parts": ["esp32"]},
            {"n": 2, "instruction": "Eyes.", "verify": "CHECK_GPIO",
             "expect": {}, "depends_on_parts": ["hc_sr04"]},
            {"n": 3, "instruction": "Arm.", "verify": "CHECK_GPIO",
             "expect": {}, "depends_on_parts": ["servo"]},
        ]
        svg = render_breadboard_svg(
            build_netlist(plan, 3, pin_map_for("esp32_devkit")), 3)
        groups = re.findall(r'<g id="wire-[^"]+"[^>]*>.*?</g>', svg, re.S)
        self.assertTrue(groups)
        for group in groups:
            for d in re.findall(r'<path d="([^"]+)"', group):
                self.assertNotIn("C", d)
                toks = re.findall(r"[MHVLQ]|-?\d+(?:\.\d+)?", d)
                x = y = None
                i = 0
                while i < len(toks):
                    cmd = toks[i]
                    i += 1
                    if cmd == "M":
                        x, y = float(toks[i]), float(toks[i + 1])
                        i += 2
                    elif cmd == "H":
                        x = float(toks[i])
                        i += 1
                    elif cmd == "V":
                        y = float(toks[i])
                        i += 1
                    elif cmd == "L":
                        nx, ny = float(toks[i]), float(toks[i + 1])
                        i += 2
                        self.assertTrue(
                            nx == x or ny == y,
                            "diagonal segment in %r" % d)
                        x, y = nx, ny
                    elif cmd == "Q":
                        x, y = float(toks[i + 2]), float(toks[i + 3])
                        i += 4
                    else:
                        self.fail("unexpected path token in %r" % d)


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
        self.assertIn("<svg", out["breadboard_svg"])
        self.assertIn('id="breadboard-board"', out["breadboard_svg"])
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


class WireAnnotationTest(unittest.TestCase):
    """Netlist wires carry their drawn color, its spoken name, and the
    part's display title, so narration matches the diagram exactly."""

    def test_known_signals_named(self):
        from backend.app.visual_schematic import wire_color_name

        for signal, name in (("power", "red"), ("ground", "gray"),
                             ("TRIG", "blue"), ("ECHO", "green"),
                             ("SDA", "blue"), ("SCL", "green"),
                             ("SIG", "purple"), ("OUT", "amber"),
                             ("AO", "orange")):
            self.assertEqual(wire_color_name(signal), name, signal)

    def test_unknown_signal_falls_back_to_gray(self):
        from backend.app.visual_schematic import wire_color_name

        self.assertEqual(wire_color_name("NOPE"), "gray")
        self.assertEqual(wire_color_name(""), "gray")
        self.assertEqual(wire_color_name(None), "gray")

    def test_annotate_adds_presentation_keys(self):
        from backend.app.visual_schematic import annotate_wires

        wires = [{"part": "hc_sr04", "part_pin": "TRIG",
                  "board_pin": "GPIO25", "signal": "TRIG"},
                 {"part": "mystery", "part_pin": "X",
                  "board_pin": "GPIO26", "signal": "X"}]
        annotate_wires(wires)
        self.assertEqual(wires[0]["color"], "#3b82f6")
        self.assertEqual(wires[0]["color_name"], "blue")
        self.assertEqual(wires[0]["part_title"], "HC-SR04P Ultrasonic")
        self.assertEqual(wires[1]["color_name"], "gray")
        self.assertEqual(wires[1]["part_title"], "Mystery")

    def test_endpoint_wires_carry_annotation(self):
        import backend.app.main as app_main
        from unittest import mock as _mock

        session = {"learning": True, "board": "esp32_devkit",
                   "pin_map": pin_map_for("esp32_devkit"), "plan": PLAN}
        fake_orc = SimpleNamespace(sessions={"s1": session})
        with _mock.patch.object(app_main, "orchestrator", fake_orc):
            out = app_main.api_step_schematic("s1", 2)
        sda = next(w for w in out["netlist"]["wires"]
                   if w.get("signal") == "SDA")
        self.assertEqual(sda["color_name"], "blue")
        self.assertEqual(sda["part_title"], "BME280")
        for wire in out["netlist"]["wires"]:
            self.assertIn("color", wire)
            self.assertIn("color_name", wire)
            self.assertIn("part_title", wire)


if __name__ == "__main__":
    unittest.main()
