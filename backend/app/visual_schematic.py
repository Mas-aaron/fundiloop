"""Pin-card wiring diagrams for Learning mode (ESP32).

The drawing is the netlist: one card per part, the ESP32 in the middle,
and one colored wire per connection. Only parts introduced by steps
1..N are present, so the schematic grows with the lesson. Each wire and
part group carries data-step so the UI can draw new wires in as the
student advances.
"""
from __future__ import annotations

import re
from xml.sax.saxutils import escape

from .schematic import svg_id

COLORS = {
    "power": "#ef4444",
    "ground": "#9ca3af",
    "SIG": "#a855f7",
    "signal": "#a855f7",
    "TRIG": "#3b82f6",
    "ECHO": "#22c55e",
    "SDA": "#38bdf8",
    "SCL": "#22c55e",
    "DATA": "#f59e0b",
    "DQ": "#f59e0b",
    "OUT": "#f59e0b",
    "AO": "#f97316",
    "VOUT": "#f97316",
    "WIPER": "#f97316",
    "IN": "#a855f7",
    "R": "#ef4444",
    "G": "#22c55e",
    "B": "#3b82f6",
    "+": "#f59e0b",
    "-": "#9ca3af",
    "COM": "#9ca3af",
}
_DEFAULT = "#64748b"

# Spoken name per wire color, mirroring COLORS above: the tutor says
# "use a red wire" for exactly the wire drawn in red.
COLOR_NAMES = {
    "power": "red",
    "ground": "gray",
    "SIG": "purple",
    "signal": "purple",
    "TRIG": "blue",
    "ECHO": "green",
    "SDA": "blue",
    "SCL": "green",
    "DATA": "amber",
    "DQ": "amber",
    "OUT": "amber",
    "AO": "orange",
    "VOUT": "orange",
    "WIPER": "orange",
    "IN": "purple",
    "R": "red",
    "G": "green",
    "B": "blue",
    "+": "amber",
    "-": "gray",
    "COM": "gray",
}
_DEFAULT_NAME = "gray"

TITLES = {
    "hc_sr04": "HC-SR04P Ultrasonic",
    "servo": "SG90 Servo",
    "bme280": "BME280",
    "ssd1306": "SSD1306 OLED",
    "lcd1602": "LCD 1602",
    "dht22": "DHT22",
    "dht11": "DHT11",
    "ds18b20": "DS18B20",
    "pir": "PIR Motion",
    "mq2": "MQ-2 Gas",
    "ldr": "LDR",
    "lm35": "LM35",
    "soil_moisture": "Soil Moisture",
    "potentiometer": "Potentiometer",
    "button": "Button",
    "buzzer": "Buzzer",
    "relay": "Relay",
    "led": "LED",
    "rgb_led": "RGB LED",
}

KINDS = {
    "servo": "Actuator",
    "relay": "Actuator",
    "buzzer": "Output",
    "led": "Output",
    "rgb_led": "Output",
    "ssd1306": "Display",
    "lcd1602": "Display",
    "button": "Input",
    "potentiometer": "Input",
}

CARD_W = 188
PIN_H = 26
HEADER_H = 64
PAD = 14
ESP_W = 220
GAP = 56


def _color(signal: object) -> str:
    key = str(signal or "")
    return COLORS.get(key, COLORS.get(key.lower(), _DEFAULT))


def wire_color_name(signal: object) -> str:
    """Spoken wire color for a netlist signal (unknown signals are gray)."""
    key = str(signal or "")
    return COLOR_NAMES.get(key, COLOR_NAMES.get(key.lower(),
                                                _DEFAULT_NAME))


def annotate_wires(wires: list) -> list:
    """Add presentation keys in place: the drawn hex color, its spoken
    name, and the part's display title. Skips non-dicts."""
    for wire in wires or []:
        if not isinstance(wire, dict):
            continue
        wire["color"] = _color(wire.get("signal"))
        wire["color_name"] = wire_color_name(wire.get("signal"))
        wire["part_title"] = _title(str(wire.get("part") or ""))
    return wires


def _title(part: str) -> str:
    return TITLES.get(part, part.replace("_", " ").title())


def _kind(part: str) -> str:
    if part in KINDS:
        return KINDS[part]
    return "Sensor"


def _display_board_pin(pin: object) -> str:
    text = str(pin or "")
    if text in ("VIN", "5V"):
        return "5V"
    return text


def _board_pin_key(pin: object) -> tuple:
    text = _display_board_pin(pin)
    rank = {"3V3": 0, "5V": 1, "GND": 2}
    if text in rank:
        return (rank[text], 0, text)
    m = re.match(r"GPIO(\d+)$", text, re.I)
    if m:
        return (10, int(m.group(1)), text)
    return (20, 0, text)


def _icon(name: str, cx: int, cy: int) -> str:
    if name == "usb_c":
        return (
            '<rect x="%d" y="%d" width="22" height="14" rx="4" fill="none" '
            'stroke="#9ca3af" stroke-width="1.6"/>'
            '<rect x="%d" y="%d" width="10" height="6" rx="2" fill="#9ca3af"/>'
            % (cx - 11, cy - 7, cx - 5, cy - 3)
        )
    if name == "esp32":
        return (
            '<rect x="%d" y="%d" width="28" height="20" rx="3" fill="none" '
            'stroke="#60a5fa" stroke-width="1.6"/>'
            '<circle cx="%d" cy="%d" r="2.2" fill="#60a5fa"/>'
            % (cx - 14, cy - 10, cx + 8, cy - 4)
        )
    if name == "servo":
        return (
            '<rect x="%d" y="%d" width="22" height="16" rx="2" fill="#2563eb"/>'
            '<circle cx="%d" cy="%d" r="5" fill="#e5e7eb"/>'
            % (cx - 11, cy - 8, cx, cy - 4)
        )
    if name == "hc_sr04":
        return (
            '<circle cx="%d" cy="%d" r="7" fill="none" stroke="#9ca3af" '
            'stroke-width="1.6"/>'
            '<circle cx="%d" cy="%d" r="7" fill="none" stroke="#9ca3af" '
            'stroke-width="1.6"/>'
            % (cx - 10, cy, cx + 10, cy)
        )
    return (
        '<rect x="%d" y="%d" width="20" height="14" rx="2" fill="none" '
        'stroke="#9ca3af" stroke-width="1.5"/>'
        % (cx - 10, cy - 7)
    )


def _card(gid: str, step: int, x: int, y: int, w: int, h: int,
          title: str, kind: str, icon: str, pins: list[tuple[str, int]],
          pin_side: str = "right") -> list[str]:
    rows = [
        '<g id="%s" class="schematic-part" data-step="%d">'
        % (svg_id(gid), step),
        "<title>%s</title>" % escape("%s (%s)" % (title, kind)),
        '<rect x="%d" y="%d" width="%d" height="%d" rx="16" fill="#141418" '
        'stroke="#2a2a32" stroke-width="1.4"/>' % (x, y, w, h),
        icon,
        '<text x="%d" y="%d" font-family="sans-serif" font-size="13" '
        'font-weight="700" fill="#f4f4f5">%s</text>'
        % (x + 44, y + 28, escape(title)),
        '<text x="%d" y="%d" font-family="sans-serif" font-size="10" '
        'fill="#9ca3af">%s</text>' % (x + 44, y + 44, escape(kind)),
    ]
    for label, py in pins:
        if pin_side == "left":
            rows.append(
                '<circle cx="%d" cy="%d" r="3.2" fill="#3f3f46"/>' % (x + 10, py))
            rows.append(
                '<text x="%d" y="%d" font-family="sans-serif" font-size="11" '
                'fill="#d4d4d8">%s</text>'
                % (x + 22, py + 4, escape(label)))
        else:
            rows.append(
                '<text x="%d" y="%d" font-family="sans-serif" font-size="11" '
                'fill="#d4d4d8">%s</text>'
                % (x + 16, py + 4, escape(label)))
            rows.append(
                '<circle cx="%d" cy="%d" r="3.2" fill="#3f3f46"/>'
                % (x + w - 10, py))
    rows.append("</g>")
    return rows


def _wire_path(x1: float, y1: float, x2: float, y2: float) -> str:
    mid = (x1 + x2) / 2.0
    return "M %.1f %.1f C %.1f %.1f, %.1f %.1f, %.1f %.1f" % (
        x1, y1, mid, y1, mid, y2, x2, y2)


def _wire_group(wire_id: str, step: int, fresh: bool, title: str,
                d: str, color: str, x1: float, y1: float,
                x2: float, y2: float) -> list[str]:
    width = "3.2" if fresh else "2.2"
    cls = "schematic-wire schematic-new" if fresh else "schematic-wire schematic-prior"
    opacity = "1" if fresh else "0.42"
    return [
        '<g id="%s" class="%s" data-step="%d" opacity="%s">'
        % (svg_id(wire_id), cls, step, opacity),
        "<title>%s</title>" % escape(title),
        '<path d="%s" fill="none" stroke="%s" stroke-width="%s" '
        'stroke-linecap="round"/>' % (d, color, width),
        '<circle cx="%.1f" cy="%.1f" r="3.4" fill="%s"/>' % (x1, y1, color),
        '<circle cx="%.1f" cy="%.1f" r="3.4" fill="%s"/>' % (x2, y2, color),
        "</g>",
    ]


def render_visual_svg(netlist: dict, step_n: int) -> str:
    wires = list((netlist or {}).get("wires") or [])
    unresolved = list((netlist or {}).get("unresolved") or [])
    board = str((netlist or {}).get("board") or "esp32_devkit")
    by_part: dict[str, list] = {}
    for wire in wires:
        by_part.setdefault(str(wire.get("part")), []).append(wire)
    parts = list((netlist or {}).get("parts") or sorted(by_part))

    board_pins = []
    seen_pins = set()
    for pin in ("3V3", "5V", "GND"):
        seen_pins.add(pin)
        board_pins.append(pin)
    for wire in wires:
        shown = _display_board_pin(wire.get("board_pin"))
        if shown not in seen_pins:
            seen_pins.add(shown)
            board_pins.append(shown)
    board_pins.sort(key=_board_pin_key)

    left_parts: list[str] = ["usb_c"]
    right_parts: list[str] = []
    for i, name in enumerate(parts):
        if i % 2 == 0:
            left_parts.append(name)
        else:
            right_parts.append(name)

    def _part_h(name: str) -> int:
        n = 2 if name == "usb_c" else max(1, len(by_part.get(name, [])))
        return HEADER_H + n * PIN_H + PAD

    left_h = sum(_part_h(p) + 16 for p in left_parts) if left_parts else 0
    right_h = sum(_part_h(p) + 16 for p in right_parts) if right_parts else 0
    esp_h = HEADER_H + max(3, len(board_pins)) * PIN_H + PAD
    height = max(left_h, right_h, esp_h) + 80
    width = CARD_W + GAP + ESP_W + GAP + CARD_W + 48
    if not right_parts:
        width = CARD_W + GAP + ESP_W + 56

    left_x = 24
    esp_x = left_x + CARD_W + GAP
    right_x = esp_x + ESP_W + GAP
    top = 36
    esp_y = top + max(0, (height - 80 - esp_h) // 2)

    pin_y = {
        pin: esp_y + HEADER_H + i * PIN_H
        for i, pin in enumerate(board_pins)
    }

    bb_x, bb_y = esp_x - 18, max(24, esp_y - 18)
    bb_w, bb_h = ESP_W + 36, esp_h + 36

    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" role="img">' % (width, height, width, height),
        "<title>%s</title>"
        % escape("Step %s wiring schematic (%s)" % (step_n, board)),
        '<rect width="100%" height="100%" fill="#0a0a0c"/>',
        '<text x="24" y="22" font-family="sans-serif" font-size="12" '
        'fill="#71717a">Step %s — wires draw in as you go</text>' % step_n,
        '<g id="breadboard" opacity="0.9">',
        '<rect x="%d" y="%d" width="%d" height="%d" rx="12" fill="#f8fafc" '
        'stroke="#d1d5db"/>' % (bb_x, bb_y, bb_w, bb_h),
    ]
    hole_cols = max(8, (bb_w - 24) // 10)
    hole_rows = max(10, (bb_h - 24) // 10)
    for i in range(hole_cols):
        if 3 <= i <= hole_cols - 4:
            continue
        for j in range(hole_rows):
            out.append(
                '<circle cx="%d" cy="%d" r="1.4" fill="#d1d5db"/>'
                % (bb_x + 12 + i * 10, bb_y + 12 + j * 10))
    out.append(
        '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#ef4444" '
        'stroke-width="1.2" stroke-dasharray="3,3"/>'
        % (bb_x + 8, bb_y + 10, bb_x + 8, bb_y + bb_h - 10))
    out.append(
        '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#3b82f6" '
        'stroke-width="1.2" stroke-dasharray="3,3"/>'
        % (bb_x + bb_w - 8, bb_y + 10, bb_x + bb_w - 8, bb_y + bb_h - 10))
    out.append("</g>")

    esp_pins = [(pin, pin_y[pin]) for pin in board_pins]
    out.append('<g id="part-esp32" class="schematic-part" data-step="1">')
    out.append("<title>ESP32 DevKit v1 (Board)</title>")
    out.append(
        '<rect x="%d" y="%d" width="%d" height="%d" rx="16" fill="#141418" '
        'stroke="#2a2a32" stroke-width="1.4"/>' % (esp_x, esp_y, ESP_W, esp_h))
    out.append(_icon("esp32", esp_x + 26, esp_y + 30))
    out.append(
        '<text x="%d" y="%d" font-family="sans-serif" font-size="13" '
        'font-weight="700" fill="#f4f4f5">ESP32 DevKit v1</text>'
        % (esp_x + 44, esp_y + 28))
    out.append(
        '<text x="%d" y="%d" font-family="sans-serif" font-size="10" '
        'fill="#9ca3af">Board</text>' % (esp_x + 44, esp_y + 44))
    for pin, py in esp_pins:
        out.append(
            '<circle cx="%d" cy="%d" r="3.2" fill="#3f3f46"/>' % (esp_x + 10, py)
        )
        out.append(
            '<text x="%d" y="%d" font-family="sans-serif" font-size="11" '
            'fill="#d4d4d8">%s</text>' % (esp_x + 22, py + 4, escape(pin)))
        out.append(
            '<circle cx="%d" cy="%d" r="3.2" fill="#3f3f46"/>'
            % (esp_x + ESP_W - 10, py)
        )
        out.append(
            '<text x="%d" y="%d" font-family="sans-serif" font-size="11" '
            'fill="#d4d4d8" text-anchor="end">%s</text>'
            % (esp_x + ESP_W - 22, py + 4, escape(pin)))
    out.append("</g>")

    def _stack(names: list[str], x: int) -> dict[str, dict]:
        placed = {}
        y = top
        for name in names:
            h = _part_h(name)
            if name == "usb_c":
                pins = [("+5V", y + HEADER_H), ("GND", y + HEADER_H + PIN_H)]
                out.extend(_card(
                    "part-usb_c", 1, x, y, CARD_W, h,
                    "USB-C 5V Adapter", "Power",
                    _icon("usb_c", x + 26, y + 30), pins))
                placed[name] = {"x": x, "y": y, "h": h, "pins": {
                    "+5V": pins[0][1], "GND": pins[1][1]}}
            else:
                rows = by_part.get(name, [])
                pins = []
                pin_map = {}
                for i, wire in enumerate(rows):
                    py = y + HEADER_H + i * PIN_H
                    label = str(wire.get("part_pin") or "")
                    pins.append((label, py))
                    pin_map[label] = py
                step = int(rows[0].get("step_added", step_n)) if rows else step_n
                out.extend(_card(
                    "part-" + name, step, x, y, CARD_W, h,
                    _title(name), _kind(name),
                    _icon(name, x + 26, y + 30), pins,
                    "left" if x > esp_x else "right"))
                placed[name] = {"x": x, "y": y, "h": h, "pins": pin_map}
            y += h + 16
        return placed

    left_at = _stack(left_parts, left_x)
    right_at = _stack(right_parts, right_x) if right_parts else {}

    # USB-C always feeds the board's 5V and GND from step 1.
    usb = left_at["usb_c"]
    usb_5v = (usb["x"] + CARD_W - 10, usb["pins"]["+5V"])
    usb_gnd = (usb["x"] + CARD_W - 10, usb["pins"]["GND"])
    esp_5v = (esp_x + 10, pin_y.get("5V", pin_y["GND"]))
    esp_gnd_l = (esp_x + 10, pin_y["GND"])
    out += _wire_group(
        "wire-usb_c-VCC", 1, step_n == 1,
        "USB-C +5V → 5V (added step 1)",
        _wire_path(usb_5v[0], usb_5v[1], esp_5v[0], esp_5v[1]),
        COLORS["power"], usb_5v[0], usb_5v[1], esp_5v[0], esp_5v[1])
    out += _wire_group(
        "wire-usb_c-GND", 1, step_n == 1,
        "USB-C GND → GND (added step 1)",
        _wire_path(usb_gnd[0], usb_gnd[1], esp_gnd_l[0], esp_gnd_l[1]),
        COLORS["ground"], usb_gnd[0], usb_gnd[1], esp_gnd_l[0], esp_gnd_l[1])

    for name, rows in by_part.items():
        side = left_at if name in left_at else right_at
        if name not in side:
            continue
        card = side[name]
        from_left = name in left_at
        for wire in rows:
            part_pin = str(wire.get("part_pin") or "")
            shown = _display_board_pin(wire.get("board_pin"))
            py = card["pins"].get(part_pin)
            by = pin_y.get(shown)
            if py is None or by is None:
                continue
            if from_left:
                x1, y1 = card["x"] + CARD_W - 10, py
                x2, y2 = esp_x + 10, by
            else:
                x1, y1 = card["x"] + 10, py
                x2, y2 = esp_x + ESP_W - 10, by
            added = int(wire.get("step_added", step_n))
            title = "%s %s → %s (added step %s)" % (
                name, part_pin, wire.get("board_pin"), added)
            out += _wire_group(
                "wire-%s-%s" % (name, part_pin), added, added == step_n,
                title, _wire_path(x1, y1, x2, y2),
                _color(wire.get("signal")), x1, y1, x2, y2)

    y_u = height - 18 - 18 * len(unresolved)
    for i, item in enumerate(unresolved):
        out.append(
            '<text x="24" y="%d" font-family="sans-serif" font-size="11" '
            'fill="#b45309">unresolved: %s — %s</text>'
            % (y_u + i * 16,
               escape(str(item.get("part") or "")),
               escape(str(item.get("reason") or ""))))
    if not wires:
        out.append(
            '<text x="%d" y="%d" font-family="sans-serif" font-size="12" '
            'fill="#a1a1aa">No external wiring yet — power the board, then '
            "parts appear as the lesson names them.</text>"
            % (esp_x, min(height - 16, esp_y + esp_h + 28)))
    out.append("</svg>")
    return "\n".join(out)
