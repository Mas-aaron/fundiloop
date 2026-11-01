"""Schematik-style build guide for Learning mode (ESP32 only).

One project page in three parts: gather the parts (qty table),
follow the numbered assembly steps (each card names exactly the
wires it adds), and flash at the end with the existing FLASH button.
Assembly cards link back into the schematic: every step carries the
SVG group ids of its new wires, so the UI can highlight a step's
wiring in the diagram and speak the card aloud.

Everything derives from the validated plan plus the progressive
netlist — the guide can never name a wire the schematic does not
draw. Steps that add no wiring (pure verify steps) become explicit
"power up and check" cards instead of being skipped.
"""
from __future__ import annotations

from .schematic import build_netlist, svg_id

PART_BLURBS: dict[str, str] = {
    "esp32": "The main board — runs your code.",
    "bme280": "Temp, humidity + pressure sensor (I2C).",
    "ssd1306": "Small OLED display (I2C).",
    "lcd1602": "16x2 text display (I2C backpack).",
    "dht22": "Temp + humidity sensor (1 wire).",
    "dht11": "Temp + humidity sensor (1 wire).",
    "ds18b20": "Temp sensor (1 wire).",
    "hc_sr04": "Distance sensor (ultrasonic).",
    "pir": "Motion detector.",
    "mq2": "Gas/smoke sensor (analog).",
    "ldr": "Light sensor (analog).",
    "lm35": "Temp sensor (analog).",
    "soil_moisture": "Soil wetness sensor (analog).",
    "potentiometer": "Knob (analog).",
    "button": "Push button (uses the internal pull-up).",
    "led": "Light + series resistor.",
    "rgb_led": "Red/green/blue light.",
    "buzzer": "Beeps on a digital pin.",
    "servo": "Arm that moves to an angle (USB 5V power).",
    "relay": "Switches a bigger load (USB 5V power).",
    "breadboard": "Holds parts + rails, no soldering.",
    "jumper_wire": "Hookup wires — one per connection.",
    "wire": "Hookup wire.",
    "resistor": "220 ohm for LEDs.",
    "multimeter": "For checking voltages by hand.",
}

_FALLBACK_BLURB = "Wire it by hand from its datasheet."
_JUMPER_PART = "jumper_wire"


def _blurb(part: str) -> str:
    return PART_BLURBS.get(part, _FALLBACK_BLURB)


def _wire_text(wires: list) -> str:
    """Schematik-style wire line: "part: A → X · B → Y; ..."."""
    by_part: dict[str, list] = {}
    for wire in wires:
        by_part.setdefault(str(wire.get("part")), []).append(wire)
    groups = []
    for part in sorted(by_part):
        runs = ["%s → %s" % (w.get("part_pin"), w.get("board_pin"))
                for w in by_part[part]]
        groups.append("%s: %s" % (part, " · ".join(runs)))
    return "; ".join(groups)


def build_guide(session: dict) -> dict:
    """Build the parts table + assembly cards for a learning session.

    Returns {"project_title", "board", "parts", "steps"} with parts as
    [{part, qty, blurb}] and steps as [{n, title, text, wire_ids}].
    Pure function of the session's plan, pin map, and chapters.
    """
    plan = [s for s in (session.get("plan") or []) if isinstance(s, dict)]
    pin_map = session.get("pin_map", {}) or {}
    ordered = [s for s in plan if isinstance(s.get("n"), int)]
    chapters = [c for c in (session.get("chapters") or [])
                if isinstance(c, dict)]
    if not ordered:
        return {"project_title": session.get("project_title", ""),
                "board": session.get("board", ""),
                "parts": [], "steps": []}
    full = build_netlist(session.get("plan", []),
                         max(s["n"] for s in ordered), pin_map)
    wires = full["wires"]
    requested = list(full["requested"])
    parts_order = ([p for p in requested if p in ("esp32",)]
                   + [p for p in full["parts"]]
                   + [p for p in requested
                      if p not in ("esp32",) and p not in full["parts"]])
    qtys: dict[str, int] = {p: 1 for p in parts_order}
    if wires:
        qtys[_JUMPER_PART] = max(qtys.get(_JUMPER_PART, 0), len(wires))
        if _JUMPER_PART not in parts_order:
            parts_order.append(_JUMPER_PART)
    parts = [{"part": p, "qty": qtys[p], "blurb": _blurb(p)}
             for p in parts_order]
    steps = []
    for i, step in enumerate(ordered):
        n = step["n"]
        new_wires = [w for w in wires if w.get("step_added") == n]
        chapter = chapters[i] if i < len(chapters) else {}
        title = str(chapter.get("title") or "").strip() or "Step %d" % n
        if new_wires:
            text = _wire_text(new_wires)
        else:
            instruction = str(step.get("instruction") or "").strip()
            if len(instruction) > 120:
                instruction = instruction[:117].rstrip() + "…"
            text = "Power up and check — %s" % (instruction or step.get(
                "verify", ""))
        steps.append({
            "n": n,
            "title": title,
            "text": text,
            "wire_ids": [svg_id("wire-%s-%s" % (w.get("part"),
                                                w.get("part_pin")))
                         for w in new_wires],
        })
    return {"project_title": session.get("project_title", ""),
            "board": session.get("board", ""),
            "parts": parts, "steps": steps}
