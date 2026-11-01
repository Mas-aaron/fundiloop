"""Spoken pins must match the schematic (one allocator, stable pins).

The planner writes GPIO numbers freely in instruction text, while the
schematic allocator assigns signal pins deterministically. Without
repair, speech can say GPIO18 while the board draws GPIO5. Pins must
also stay put as later steps add parts.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_pin_consistency -v
"""
import unittest

from backend.app.boards import pin_map_for
from backend.app.schematic import build_netlist, repair_plan_pins

PIN_MAP = pin_map_for("esp32_devkit")


def _gpio_of(netlist, part, part_pin):
    for w in netlist["wires"]:
        if w["part"] == part and w["part_pin"] == part_pin:
            return w["board_pin"]
    return None


class StableAllocationTest(unittest.TestCase):
    def test_part_pin_never_moves_when_later_steps_add_parts(self):
        plan = [
            {"n": 1, "instruction": "Wire the servo.",
             "verify": "CHECK_SERIAL", "depends_on_parts": ["servo"]},
            {"n": 2, "instruction": "Add the buzzer.",
             "verify": "CHECK_SERIAL", "depends_on_parts": ["buzzer"]},
        ]
        at_one = _gpio_of(build_netlist(plan, 1, PIN_MAP), "servo", "SIG")
        at_two = _gpio_of(build_netlist(plan, 2, PIN_MAP), "servo", "SIG")
        self.assertIsNotNone(at_one)
        self.assertEqual(at_one, at_two)


class RepairInstructionPinsTest(unittest.TestCase):
    def test_wrong_gpio_is_rewritten_to_the_schematic_pin(self):
        plan = [
            {"n": 1, "instruction": "Connect the DHT22 DATA pin to GPIO18.",
             "verify": "CHECK_SERIAL", "depends_on_parts": ["dht22"]},
        ]
        board = _gpio_of(build_netlist(plan, 1, PIN_MAP), "dht22", "DATA")
        fixed = repair_plan_pins(plan, PIN_MAP)
        self.assertEqual(
            fixed[0]["instruction"],
            "Connect the DHT22 DATA pin to %s." % board)

    def test_correct_i2c_mentions_are_untouched(self):
        plan = [
            {"n": 1,
             "instruction": "Wire SDA to GPIO21 and SCL to GPIO22.",
             "verify": "RUN_I2C_SCAN", "depends_on_parts": ["bme280"]},
        ]
        fixed = repair_plan_pins(plan, PIN_MAP)
        self.assertEqual(fixed[0]["instruction"],
                         plan[0]["instruction"])

    def test_sda_mention_maps_to_sda_not_the_signal_pin(self):
        # A step adding I2C + one signal part: "SDA to GPIO18" must
        # become GPIO21, never the signal pin, and DATA follows DATA.
        plan = [
            {"n": 1,
             "instruction": "Wire SDA to GPIO18 and the DHT22 DATA "
                            "pin to GPIO19.",
             "verify": "CHECK_SERIAL",
             "depends_on_parts": ["bme280", "dht22"]},
        ]
        data_gpio = _gpio_of(build_netlist(plan, 1, PIN_MAP),
                             "dht22", "DATA")
        fixed = repair_plan_pins(plan, PIN_MAP)
        self.assertIn("SDA to GPIO21", fixed[0]["instruction"])
        self.assertIn("DATA pin to %s" % data_gpio,
                      fixed[0]["instruction"])

    def test_bare_pin_number_follows_its_signal_name(self):
        plan = [
            {"n": 1, "instruction": "Connect the DATA pin to pin 18.",
             "verify": "CHECK_SERIAL", "depends_on_parts": ["dht22"]},
        ]
        board = _gpio_of(build_netlist(plan, 1, PIN_MAP), "dht22", "DATA")
        fixed = repair_plan_pins(plan, PIN_MAP)
        self.assertEqual(fixed[0]["instruction"],
                         "Connect the DATA pin to pin %s."
                         % board.replace("GPIO", ""))

    def test_guide_carries_chapter_teach_for_narration(self):
        from backend.app.build_guide import build_guide

        session = {
            "project_title": "Weather", "board": "esp32_devkit",
            "pin_map": PIN_MAP,
            "plan": [{"n": 1, "instruction": "Wire it.",
                      "verify": "RUN_I2C_SCAN",
                      "depends_on_parts": ["bme280"]}],
            "chapters": [{"n": 1, "title": "Meet the sensor",
                          "teach": "Two shared wires carry the readings.",
                          "task": "Wire it.", "verify": "RUN_I2C_SCAN"}],
        }
        guide = build_guide(session)
        self.assertEqual(guide["steps"][0]["teach"],
                         "Two shared wires carry the readings.")

    def test_input_plan_is_not_mutated(self):
        plan = [
            {"n": 1, "instruction": "Connect DATA to GPIO18.",
             "verify": "CHECK_SERIAL", "depends_on_parts": ["dht22"]},
        ]
        repair_plan_pins(plan, PIN_MAP)
        self.assertEqual(plan[0]["instruction"],
                         "Connect DATA to GPIO18.")


if __name__ == "__main__":
    unittest.main()
