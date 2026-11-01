"""Schematik-style build guide: parts table + assembly cards.

Pure function of the session plan/pin map/chapters plus the endpoint
shape (404s, ESP32-only 400). Assembly cards name exactly the wires
their step adds and carry the SVG group ids for UI highlight. No
hardware, no network, no model calls in these tests.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_build_guide -v
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.app.boards import pin_map_for
from backend.app.build_guide import build_guide


PLAN = [
    {"n": 1, "instruction": "Wire the sensor.", "verify": "RUN_I2C_SCAN",
     "expect": {}, "depends_on_parts": ["bme280", "esp32", "breadboard"]},
    {"n": 2, "instruction": "Add the LED.", "verify": "CHECK_GPIO",
     "expect": {}, "depends_on_parts": ["led"]},
    {"n": 3, "instruction": "Read the temperature.", "verify": "READ_SENSOR",
     "expect": {}, "depends_on_parts": ["bme280"]},
]

CHAPTERS = [
    {"n": 1, "title": "Meet the sensor"},
    {"n": 2, "title": "Blink the light"},
    {"n": 3, "title": "Feel the heat"},
]


def make_session(**over):
    session = {"learning": True, "board": "esp32_devkit",
               "project_title": "Weather", "pin_map": pin_map_for("esp32_devkit"),
               "plan": [dict(s) for s in PLAN], "chapters": list(CHAPTERS)}
    session.update(over)
    return session


class BuildGuideTest(unittest.TestCase):
    def test_parts_cover_whole_project(self):
        guide = build_guide(make_session())
        by_part = {p["part"]: p for p in guide["parts"]}
        for name in ("esp32", "breadboard", "bme280", "led",
                     "jumper_wire"):
            self.assertIn(name, by_part)
        self.assertEqual(by_part["bme280"]["qty"], 1)
        self.assertIn("sensor", by_part["bme280"]["blurb"].lower())
        self.assertEqual(by_part["esp32"]["qty"], 1)

    def test_jumper_qty_matches_wire_count(self):
        from backend.app.schematic import build_netlist

        session = make_session()
        net = build_netlist(session["plan"], 3, session["pin_map"])
        guide = build_guide(session)
        jumpers = next(p for p in guide["parts"]
                       if p["part"] == "jumper_wire")
        self.assertEqual(jumpers["qty"], len(net["wires"]))
        self.assertGreater(len(net["wires"]), 0)

    def test_steps_name_exactly_their_new_wires(self):
        guide = build_guide(make_session())
        self.assertEqual([s["n"] for s in guide["steps"]], [1, 2, 3])
        one, two, three = guide["steps"]
        self.assertEqual(one["title"], "Meet the sensor")
        self.assertIn("bme280: ", one["text"])
        self.assertIn("SDA → GPIO21", one["text"])
        self.assertIn("wire-bme280-SDA", one["wire_ids"])
        self.assertEqual(len(one["wire_ids"]), 4)
        self.assertIn("led: ", two["text"])
        self.assertIn("wire-led-_", two["wire_ids"])
        self.assertEqual(three["wire_ids"], [])
        self.assertTrue(three["text"].startswith("Power up and check"))
        self.assertIn("Read the temperature", three["text"])

    def test_wire_ids_match_svg_groups(self):
        from backend.app.schematic import build_netlist, render_svg

        session = make_session()
        guide = build_guide(session)
        svg = render_svg(build_netlist(session["plan"], 3,
                                       session["pin_map"]), 3)
        for step in guide["steps"]:
            for gid in step["wire_ids"]:
                self.assertIn('id="%s"' % gid, svg)

    def test_titles_fall_back_without_chapters(self):
        guide = build_guide(make_session(chapters=[]))
        self.assertEqual([s["title"] for s in guide["steps"]],
                         ["Step 1", "Step 2", "Step 3"])

    def test_titles_align_positionally_not_by_n(self):
        chapters = [{"title": "Alpha"}, {"title": "Beta"}, {"title": "Gamma"}]
        guide = build_guide(make_session(chapters=chapters))
        self.assertEqual([s["title"] for s in guide["steps"]],
                         ["Alpha", "Beta", "Gamma"])

    def test_steps_follow_plan_list_order(self):
        plan = [dict(PLAN[2]), dict(PLAN[0]), dict(PLAN[1])]
        guide = build_guide(make_session(plan=plan, chapters=[]))
        self.assertEqual([s["n"] for s in guide["steps"]], [3, 1, 2])

    def test_unknown_part_gets_fallback_blurb(self):
        plan = [dict(PLAN[0], depends_on_parts=["quantum_flux"])]
        guide = build_guide(make_session(plan=plan, chapters=[]))
        by_part = {p["part"]: p for p in guide["parts"]}
        self.assertEqual(by_part["quantum_flux"]["blurb"],
                         "Wire it by hand from its datasheet.")

    def test_empty_plan_is_empty_guide(self):
        guide = build_guide(make_session(plan=[]))
        self.assertEqual(guide["parts"], [])
        self.assertEqual(guide["steps"], [])


class GuideEndpointTest(unittest.TestCase):
    def setUp(self):
        import backend.app.main as app_main

        self.app_main = app_main
        fake_orc = SimpleNamespace(
            sessions={"s1": make_session()})
        patcher = mock.patch.object(app_main, "orchestrator", fake_orc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_guide_shape(self):
        out = self.app_main.api_project_guide("s1")
        self.assertEqual(out["session_id"], "s1")
        self.assertEqual(out["project_title"], "Weather")
        self.assertTrue(out["parts"])
        self.assertEqual(len(out["steps"]), 3)

    def test_unknown_session_is_404(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_project_guide("nope")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_uno_board_is_400(self):
        from fastapi import HTTPException

        self.app_main.orchestrator.sessions["s1"]["board"] = "arduino_uno"
        with self.assertRaises(HTTPException) as ctx:
            self.app_main.api_project_guide("s1")
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
