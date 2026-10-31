"""Per-step wiring schematics for Learning mode (ESP32 only).

The netlist is progressive: step N shows the wiring for every part the
plan needs up to and including step N, so the full project schematic is
the last step's. Wiring comes from three honest sources only — the
plan's depends_on_parts, the session's ESP32 pin map, and
SENSOR_INTERFACES — plus a documented default GPIO allocator for parts
that need signal pins. Anything without a wiring profile renders
"unresolved" with a reason; wiring is never invented.

Deterministic throughout: same plan + pin map = same bytes, so the SVG
and the speech wire list are stable across renders.
"""
from __future__ import annotations

import re
from xml.sax.saxutils import escape

# The board itself under its vocabulary names: not wired, not unresolved.
MCU_ALIASES = {"esp32", "arduino_uno", "arduino_nano", "esp8266", "pico"}

# Mechanical parts with no electrical netlist entry: not wired, not
# unresolved. (A resistor next to an LED adds a sizing note instead.)
PASSIVE_PARTS = {"breadboard", "jumper_wire", "wire", "resistor", "multimeter"}

# I2C modules share the bus straight from the pin map.
I2C_PARTS = {"bme280": "i2c", "ssd1306": "i2c", "lcd1602": "i2c backpack"}

# Modules whose VCC needs USB 5V (VIN), not the 3V3 rail.
POWER_5V = {"servo", "relay"}

# Signal pins per known part: (part_pin, needs_analog). "led" and
# "rgb_led" are special-cased below; I2C parts use SDA/SCL instead.
SIGNAL_PINS: dict[str, list[tuple[str, bool]]] = {
    "dht22": [("DATA", False)],
    "dht11": [("DATA", False)],
    "ds18b20": [("DQ", False)],
    "hc_sr04": [("TRIG", False), ("ECHO", False)],
    "pir": [("OUT", False)],
    "mq2": [("AO", True)],
    "ldr": [("OUT", True)],
    "lm35": [("VOUT", True)],
    "soil_moisture": [("AO", True)],
    "potentiometer": [("WIPER", True)],
    "button": [("SIG", False)],
    "buzzer": [("SIG", False)],
    "servo": [("SIG", False)],
    "relay": [("IN", False)],
    "rgb_led": [("R", False), ("G", False), ("B", False)],
}

# Default GPIO allocator pools (ESP32 DevKit, SDA/SCL/LED reserved out).
# Analog-needing parts take ADC-capable pins first; digital parts take
# plain GPIOs first and spill into ADC pins only when those run out.
ANALOG_ORDER = [32, 33, 25, 26, 27, 14, 12, 13, 4]
DIGITAL_ORDER = [4, 5, 13, 15, 16, 17, 18, 19, 23,
                 12, 14, 25, 26, 27, 32, 33]

_WIRE_COLORS = {"power": "#c0392b", "ground": "#2c3e50", "SDA": "#2471a3",
                "SCL": "#1e8449", "SIG": "#7d6608"}
_DEFAULT_WIRE_COLOR = "#5d6d7e"
_WIRE_POWER = "power"
_WIRE_GROUND = "ground"


def _norm(name: object) -> str:
    return re.sub(r"[\s\-]+", "_", str(name or "").strip().lower())


def _gpio(pin: object) -> str:
    return "GPIO%s" % pin


def _allocate(signals: list[tuple[str, bool]],
              taken: set[int],
              analog_order: list[int] | None = None,
              digital_order: list[int] | None = None) -> list[int] | None:
    """Assign free GPIOs to one part's signals, or None when short.

    Parts are allocated in sorted-name order by the caller, so the
    assignment is deterministic for a given part set.
    """
    analog_order = ANALOG_ORDER if analog_order is None else analog_order
    digital_order = DIGITAL_ORDER if digital_order is None else digital_order
    picked: list[int] = []
    for _, want_analog in signals:
        pool = analog_order if want_analog else digital_order
        pin = next((p for p in pool if p not in taken and p not in picked),
                   None)
        if pin is None and want_analog:
            pin = next((p for p in digital_order
                        if p not in taken and p not in picked), None)
        if pin is None:
            return None
        picked.append(pin)
    taken.update(picked)
    return picked


def build_netlist(plan: list, step_n: int, pin_map: dict | None) -> dict:
    """Build the progressive netlist for steps 1..step_n.

    Returns {"board", "steps_included", "current_step", "parts",
    "wires", "unresolved", "notes", "requested"}. Every wire carries
    the step that introduced it (`step_added`) so the renderer can
    highlight the current step and dim earlier ones. `parts` holds
    wired parts only (sorted);
    `unresolved` holds {"part", "reason"} for parts with no wiring
    profile or no free GPIO left. MCU aliases and passive parts are
    skipped silently from wiring but listed in `requested`, and an
    empty netlist gains an explanatory note (a no-wiring step is a
    real answer, not missing data).
    """
    pin_map = pin_map or {}
    sda = pin_map.get("sda", 21)
    scl = pin_map.get("scl", 22)
    led_gpio = pin_map.get("led_gpio", 2)
    steps = [s for s in (plan or [])
             if isinstance(s, dict) and isinstance(s.get("n"), int)
             and s["n"] <= step_n]
    steps.sort(key=lambda s: s["n"])
    included = [s["n"] for s in steps]
    needed: set[str] = set()
    requested: set[str] = set()
    first_step: dict[str, int] = {}
    for step in steps:
        deps = step.get("depends_on_parts") or []
        if isinstance(deps, list):
            for dep in deps:
                name = _norm(dep)
                if not name:
                    continue
                requested.add(name)
                if name not in MCU_ALIASES and name not in PASSIVE_PARTS:
                    needed.add(name)
                    # Steps ascend, so the first sighting is the minimum.
                    first_step.setdefault(name, step["n"])
    wires: list[dict] = []
    unresolved: list[dict] = []
    parts: list[str] = []
    taken: set[int] = set()
    notes: list[str] = []
    has_i2c = False
    for name in sorted(needed):
        if name in I2C_PARTS:
            has_i2c = True
            parts.append(name)
            vcc = "VIN" if name in POWER_5V else "3V3"
            wires.append({"part": name, "part_pin": "VCC",
                          "board_pin": vcc, "signal": "power"})
            wires.append({"part": name, "part_pin": "GND",
                          "board_pin": "GND", "signal": "ground"})
            wires.append({"part": name, "part_pin": "SDA",
                          "board_pin": _gpio(sda), "signal": "SDA"})
            wires.append({"part": name, "part_pin": "SCL",
                          "board_pin": _gpio(scl), "signal": "SCL"})
        elif name == "led":
            parts.append(name)
            wires.append({"part": name, "part_pin": "+",
                          "board_pin": _gpio(led_gpio), "signal": "SIG"})
            wires.append({"part": name, "part_pin": "-",
                          "board_pin": "GND", "signal": "ground"})
        elif name == "rgb_led":
            pins = _allocate(SIGNAL_PINS[name], taken)
            if pins is None:
                unresolved.append({"part": name, "reason":
                                   "no free GPIO left for the R/G/B signals"})
                continue
            parts.append(name)
            for (sig, _), pin in zip(SIGNAL_PINS[name], pins):
                wires.append({"part": name, "part_pin": sig,
                              "board_pin": _gpio(pin), "signal": sig})
            wires.append({"part": name, "part_pin": "COM",
                          "board_pin": "GND", "signal": "ground"})
        elif name in SIGNAL_PINS:
            pins = _allocate(SIGNAL_PINS[name], taken)
            if pins is None:
                unresolved.append({"part": name, "reason":
                                   "no free GPIO left for its signal pin(s)"})
                continue
            parts.append(name)
            if name in POWER_5V:
                wires.append({"part": name, "part_pin": "VCC",
                              "board_pin": "VIN", "signal": "power"})
            elif name not in ("button", "buzzer"):
                wires.append({"part": name, "part_pin": "VCC",
                              "board_pin": "3V3", "signal": "power"})
            wires.append({"part": name, "part_pin": "GND",
                          "board_pin": "GND", "signal": "ground"})
            for (sig, _), pin in zip(SIGNAL_PINS[name], pins):
                wires.append({"part": name, "part_pin": sig,
                              "board_pin": _gpio(pin), "signal": sig})
        else:
            unresolved.append({"part": name, "reason":
                               "no wiring profile for %r: wire it by hand "
                               "from its datasheet" % name})
    for wire in wires:
        wire["step_added"] = first_step.get(str(wire.get("part")), step_n)
    if has_i2c:
        notes.append("Shared I2C bus: SDA %s, SCL %s "
                     "(most breakout boards include pull-ups)."
                     % (_gpio(sda), _gpio(scl)))
    if "led" in parts and "rgb_led" in parts:
        notes.append("LED and RGB LED share %s as a common ground return; "
                     "keep each anode on its own GPIO." % "GND")
    if not wires and not unresolved:
        if requested:
            notes.append(
                "Step %d needs no external wiring (involved: %s — "
                "the board itself, USB + built-in parts only)."
                % (step_n, ", ".join(sorted(requested))))
        else:
            notes.append("Step %d names no parts: nothing to wire."
                         % step_n)
    return {"board": pin_map.get("board", "esp32_devkit"),
            "steps_included": included, "current_step": step_n,
            "parts": parts, "wires": wires,
            "unresolved": unresolved, "notes": notes,
            "requested": sorted(requested)}


def _svg_id(text: str) -> str:
    """Stable SVG group id: word chars kept, everything else collapses."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(text or "").strip()) or "id"


# Breadboard strip geometry: power (+) and ground (−) rails that every
# power/ground wire visibly taps via a node dot. Signal wires cross
# straight through to their part boxes.
_BB_X, _BB_W = 236, 64
_BB_PWR_X, _BB_GND_X = 250, 270


def render_svg(netlist: dict, step_n: int | None = None) -> str:
    """Render a netlist as a deterministic standalone SVG schematic.

    The board box always draws on the left, a breadboard strip with
    power/ground rails sits mid-canvas, and one row per wire lands on
    a part box at the right; unresolved parts trail as dashed rows.
    The drawing is cumulative to the current step: wires introduced
    by this step render bright, earlier wires dim — the fog-of-war
    reveal, computed server-side so the UI just shows the SVG. Every
    part and wire sits in a stable `<g id>` group (part-<name>,
    wire-<part>-<pin>) for future client-side reveal work. Empty
    steps keep the board plus an explicit "no external wiring"
    message. No timestamps, no random ids: the same netlist always
    renders the same bytes.
    """
    wires = list((netlist or {}).get("wires") or [])
    unresolved = list((netlist or {}).get("unresolved") or [])
    requested = list((netlist or {}).get("requested") or [])
    board = str((netlist or {}).get("board") or "esp32_devkit")
    n = step_n
    if n is None:
        included = list((netlist or {}).get("steps_included") or [])
        n = included[-1] if included else "?"
    row_h, header_h, footer_h, width = 30, 62, 30, 660
    rows = len(wires) + len(unresolved)
    span = rows if rows else 2  # empty steps still draw board + message
    height = header_h + span * row_h + footer_h
    dimming = any(w.get("step_added", n) != n for w in wires)
    if dimming:
        sub = ("%d wire(s), %d unresolved; bright = new in step %s, "
               "dimmed = earlier steps"
               % (len(wires), len(unresolved), n))
    else:
        sub = ("%d wire(s), %d unresolved part(s); progressive to this step"
               % (len(wires), len(unresolved)))
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" role="img">' % (width, height, width, height),
        "<title>%s</title>"
        % escape("Step %s wiring schematic (%s)" % (n, board)),
        '<rect x="0" y="0" width="%d" height="%d" fill="#ffffff"/>'
        % (width, height),
        '<text x="16" y="26" font-family="sans-serif" font-size="17" '
        'font-weight="bold" fill="#111111">%s</text>'
        % escape("Step %s wiring — %s" % (n, board)),
        '<text x="16" y="46" font-family="sans-serif" font-size="12" '
        'fill="#555555">%s</text>' % escape(sub),
        '<g id="esp32-board">',
        '<rect x="16" y="%d" width="200" height="%d" fill="#eaf2f8" '
        'stroke="#2471a3" stroke-width="2"/>'
        % (header_h - 8, span * row_h + 8),
        '<text x="24" y="%d" font-family="sans-serif" font-size="12" '
        'font-weight="bold" fill="#1a5276">%s</text>'
        % (header_h + 6, escape(board)),
        "</g>",
    ]
    if wires:
        rails_bottom = header_h + len(wires) * row_h
        out += [
            '<g id="breadboard">',
            '<rect x="%d" y="%d" width="%d" height="%d" fill="#f8f9fa" '
            'stroke="#aab2bd"/>' % (_BB_X, header_h - 4, _BB_W,
                                    len(wires) * row_h + 8),
            '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#c0392b" '
            'stroke-width="3"/>' % (_BB_PWR_X, header_h, _BB_PWR_X,
                                    rails_bottom),
            '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#2c3e50" '
            'stroke-width="3"/>' % (_BB_GND_X, header_h, _BB_GND_X,
                                    rails_bottom),
            "</g>",
        ]
    by_part: dict[str, list] = {}
    for wire in wires:
        by_part.setdefault(str(wire.get("part")), []).append(wire)
    y = header_h
    for part in sorted(by_part):
        out.append('<g id="%s">' % _svg_id("part-" + part))
        for wire in by_part[part]:
            fresh = wire.get("step_added", n) == n
            color = _WIRE_COLORS.get(str(wire.get("signal")),
                                     _DEFAULT_WIRE_COLOR)
            rail = {_WIRE_POWER: _BB_PWR_X,
                    _WIRE_GROUND: _BB_GND_X}.get(str(wire.get("signal")))
            out.append('<g id="%s" opacity="%s">'
                       % (_svg_id("wire-%s-%s" % (part, wire.get("part_pin"))),
                          "1" if fresh else "0.35"))
            out.append(
                '<line x1="216" y1="%d" x2="420" y2="%d" stroke="%s" '
                'stroke-width="2"/>' % (y + 15, y + 15, color))
            if rail is not None:
                out.append(
                    '<circle cx="%d" cy="%d" r="3.5" fill="%s"/>'
                    % (rail, y + 15, color))
            out.append(
                '<text x="208" y="%d" font-family="monospace" font-size="13" '
                'text-anchor="end" fill="#111111">%s</text>'
                % (y + 19, escape(str(wire.get("board_pin")))))
            out.append(
                '<rect x="420" y="%d" width="224" height="24" fill="#f4f6f7" '
                'stroke="#2c3e50"/>' % (y + 3))
            out.append(
                '<text x="428" y="%d" font-family="monospace" font-size="13" '
                'fill="#111111">%s</text>'
                % (y + 20, escape("%s.%s" % (wire.get("part"),
                                             wire.get("part_pin")))))
            out.append("</g>")
            y += row_h
        out.append("</g>")
    for item in unresolved:
        out.append('<g id="%s">'
                   % _svg_id("unresolved-%s" % item.get("part")))
        out.append(
            '<rect x="16" y="%d" width="628" height="24" fill="#fef9e7" '
            'stroke="#b7950b" stroke-dasharray="6,3"/>' % (y + 3))
        out.append(
            '<text x="24" y="%d" font-family="sans-serif" font-size="12" '
            'fill="#7d6608">%s</text>'
            % (y + 20, escape("unresolved: %s — %s"
                              % (item.get("part"), item.get("reason")))))
        out.append("</g>")
        y += row_h
    if not rows:
        detail = ("Parts: %s" % ", ".join(requested)) if requested \
            else "This step names no parts."
        out.append(
            '<text x="24" y="%d" font-family="sans-serif" font-size="12" '
            'fill="#1a5276">USB + built-in LED</text>' % (y + 28))
        out.append(
            '<text x="228" y="%d" font-family="sans-serif" font-size="13" '
            'font-weight="bold" fill="#111111">%s</text>'
            % (y + 19, escape("No external wiring — on-board only.")))
        out.append(
            '<text x="228" y="%d" font-family="sans-serif" font-size="12" '
            'fill="#555555">%s</text>' % (y + 19 + row_h, escape(detail)))
    footer = "ESP32 DevKit defaults · unresolved parts are never auto-wired"
    if wires:
        footer += " · red rail = power, dark rail = ground"
    out.append(
        '<text x="16" y="%d" font-family="sans-serif" font-size="11" '
        'fill="#888888">%s</text>' % (height - 10, escape(footer)))
    out.append("</svg>")
    return "\n".join(out)
