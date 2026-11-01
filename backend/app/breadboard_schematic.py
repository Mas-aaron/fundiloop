"""Breadboard-layout wiring diagrams for Learning mode (ESP32).

Same netlist as the pin-card schematic, drawn as a mini breadboard:
numbered rows, A–J hole columns, power rails, and jumper paths that
cross the center trench. Parts sit around the board so a student can
toggle between pin cards and this view without changing the wiring.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from .schematic import svg_id
from .visual_schematic import (
    COLORS, CARD_W, HEADER_H, PAD, PIN_H,
    _board_pin_key, _card, _color, _display_board_pin, _icon, _kind,
    _title, _wire_group, _wire_path,
)

PITCH = 13
ROWS_N = 30
COL_LETTERS = "ABCDEFGHIJ"


def _is_power(pin: str) -> bool:
    return pin in ("5V", "VIN", "+5V")


def _is_gnd(pin: str) -> bool:
    return pin == "GND"


def _is_3v3(pin: str) -> bool:
    return pin == "3V3"


def _geom():
    """Hole and rail coordinates for a 30-row mini breadboard."""
    left = 8
    rail_w = 16
    plus_l = left + 6
    minus_l = plus_l + rail_w
    col0 = minus_l + 18
    trench = 18
    cols = {}
    for i, letter in enumerate(COL_LETTERS):
        x = col0 + i * PITCH
        if i >= 5:
            x += trench
        cols[letter] = x
    minus_r = cols["J"] + 18
    plus_r = minus_r + rail_w
    top = 28
    row_y = {n: top + (n - 1) * PITCH for n in range(1, ROWS_N + 1)}
    width = plus_r + 14
    height = top + ROWS_N * PITCH + 22
    return {
        "plus_l": plus_l, "minus_l": minus_l,
        "plus_r": plus_r, "minus_r": minus_r,
        "cols": cols, "row_y": row_y, "top": top,
        "width": width, "height": height, "col0": col0,
        "trench_x": (cols["E"] + cols["F"]) / 2.0,
    }


def _assign_rows(board_pins: list[str]) -> dict[str, int]:
    """Stable row numbers for GPIOs; rails handle power/ground."""
    rows: dict[str, int] = {}
    used = {16, 18, 21}
    next_row = 22
    for pin in board_pins:
        if _is_power(pin) or _is_gnd(pin) or _is_3v3(pin):
            continue
        while next_row in used or next_row > ROWS_N:
            next_row += 1
            if next_row > ROWS_N:
                next_row = 4
        rows[pin] = next_row
        used.add(next_row)
        next_row += 2
    return rows


def _rail_xy(g: dict, pin: str, row: int) -> tuple[float, float]:
    y = g["row_y"].get(row, g["row_y"][1])
    if _is_power(pin):
        return (g["plus_r"], y)
    if _is_gnd(pin):
        return (g["minus_l"], y)
    if _is_3v3(pin):
        return (g["plus_l"], y)
    return (g["cols"]["A"], y)


def _draw_board(g: dict) -> list[str]:
    w, h = g["width"], g["height"]
    out = [
        '<g id="breadboard-board">',
        "<title>Mini breadboard: same-row holes connect; jumper the gap</title>",
        '<rect x="0" y="0" width="%d" height="%d" rx="10" fill="#f8fafc" '
        'stroke="#d1d5db"/>' % (w, h),
        '<line x1="%d" y1="10" x2="%d" y2="%d" stroke="#ef4444" '
        'stroke-width="2"/>' % (g["plus_l"], g["plus_l"], h - 10),
        '<line x1="%d" y1="10" x2="%d" y2="%d" stroke="#3b82f6" '
        'stroke-width="2"/>' % (g["minus_l"], g["minus_l"], h - 10),
        '<line x1="%d" y1="10" x2="%d" y2="%d" stroke="#3b82f6" '
        'stroke-width="2"/>' % (g["minus_r"], g["minus_r"], h - 10),
        '<line x1="%d" y1="10" x2="%d" y2="%d" stroke="#ef4444" '
        'stroke-width="2"/>' % (g["plus_r"], g["plus_r"], h - 10),
        '<rect x="%.1f" y="12" width="10" height="%d" rx="2" fill="#e5e7eb"/>'
        % (g["trench_x"] - 5, h - 24),
    ]
    for letter, x in g["cols"].items():
        out.append(
            '<text x="%d" y="16" font-family="sans-serif" font-size="8" '
            'text-anchor="middle" fill="#6b7280">%s</text>' % (x, letter))
        out.append(
            '<text x="%d" y="%d" font-family="sans-serif" font-size="8" '
            'text-anchor="middle" fill="#6b7280">%s</text>'
            % (x, h - 6, letter))
        for n, y in g["row_y"].items():
            out.append(
                '<circle cx="%d" cy="%d" r="2.1" fill="#d1d5db"/>' % (x, y))
    for n, y in g["row_y"].items():
        out.append(
            '<circle cx="%d" cy="%d" r="1.6" fill="#fca5a5"/>'
            % (g["plus_l"], y))
        out.append(
            '<circle cx="%d" cy="%d" r="1.6" fill="#93c5fd"/>'
            % (g["minus_l"], y))
        out.append(
            '<circle cx="%d" cy="%d" r="1.6" fill="#93c5fd"/>'
            % (g["minus_r"], y))
        out.append(
            '<circle cx="%d" cy="%d" r="1.6" fill="#fca5a5"/>'
            % (g["plus_r"], y))
        if n == 1 or n == ROWS_N or n % 5 == 0:
            out.append(
                '<text x="%d" y="%d" font-family="sans-serif" font-size="8" '
                'text-anchor="end" fill="#6b7280">%d</text>'
                % (g["col0"] - 6, y + 3, n))
            out.append(
                '<text x="%d" y="%d" font-family="sans-serif" font-size="8" '
                'fill="#6b7280">%d</text>'
                % (g["cols"]["J"] + 8, y + 3, n))
    out.append("</g>")
    return out


def render_breadboard_svg(netlist: dict, step_n: int) -> str:
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
    gpio_rows = _assign_rows(board_pins)

    left_parts = [p for p in parts if _kind(p) in ("Actuator", "Output")]
    right_parts = [p for p in parts if p not in left_parts]

    def _part_h(name: str) -> int:
        n = 2 if name == "usb_c" else max(1, len(by_part.get(name, [])))
        return HEADER_H + n * PIN_H + PAD

    g = _geom()
    bb_h, bb_w = g["height"], g["width"]
    left_h = sum(_part_h(p) + 16 for p in left_parts)
    esp_h = HEADER_H + max(3, len(board_pins)) * PIN_H + PAD
    right_h = _part_h("usb_c") + 16
    right_h += sum(_part_h(p) + 16 for p in right_parts)
    height = max(left_h + esp_h + 24, bb_h, right_h) + 72
    gap = 48
    left_x = 24
    bb_x = left_x + CARD_W + gap
    esp_x = left_x
    right_x = bb_x + bb_w + gap
    width = right_x + CARD_W + 24
    top = 36
    bb_y = top + max(0, (height - 72 - bb_h) // 2)
    esp_y = bb_y + max(0, (bb_h - esp_h) // 2)
    if left_parts:
        esp_y = max(esp_y, top + left_h)

    pin_y = {
        pin: esp_y + HEADER_H + i * PIN_H
        for i, pin in enumerate(board_pins)
    }

    def hole(pin: str, prefer_row: int, side: str) -> tuple[float, float]:
        row = gpio_rows.get(pin, prefer_row)
        if _is_power(pin) or _is_gnd(pin) or _is_3v3(pin):
            x, y = _rail_xy(g, pin, row if row <= ROWS_N else prefer_row)
            return (bb_x + x, bb_y + y)
        letter = "B" if side == "left" else "I"
        y = g["row_y"].get(row, g["row_y"][4])
        return (bb_x + g["cols"][letter], bb_y + y)

    def jumper_points(pin: str, prefer_row: int) -> list[tuple[float, float]]:
        if _is_power(pin) or _is_gnd(pin) or _is_3v3(pin):
            return []
        row = gpio_rows.get(pin, prefer_row)
        y = bb_y + g["row_y"].get(row, g["row_y"][4])
        return [
            (bb_x + g["cols"]["E"], y),
            (bb_x + g["cols"]["F"], y),
        ]

    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" role="img">' % (width, height, width, height),
        "<title>%s</title>"
        % escape("Step %s breadboard layout (%s)" % (step_n, board)),
        '<rect width="100%" height="100%" fill="#0a0a0c"/>',
        '<text x="24" y="22" font-family="sans-serif" font-size="12" '
        'fill="#71717a">Step %s — breadboard (toggle pin cards anytime)'
        "</text>" % step_n,
        '<g id="breadboard" transform="translate(%d %d)">' % (bb_x, bb_y),
    ]
    out += _draw_board(g)
    out.append("</g>")

    esp_pins = [(pin, pin_y[pin]) for pin in board_pins]
    out.append('<g id="part-esp32" class="schematic-part" data-step="1">')
    out.append("<title>ESP32 DevKit v1 (Board)</title>")
    out.append(
        '<rect x="%d" y="%d" width="%d" height="%d" rx="16" fill="#141418" '
        'stroke="#2a2a32" stroke-width="1.4"/>'
        % (esp_x, esp_y, CARD_W, esp_h))
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
            '<text x="%d" y="%d" font-family="sans-serif" font-size="11" '
            'fill="#d4d4d8">%s</text>'
            % (esp_x + 16, py + 4, escape(pin)))
        out.append(
            '<circle cx="%d" cy="%d" r="3.2" fill="#3f3f46"/>'
            % (esp_x + CARD_W - 10, py))
    out.append("</g>")

    def _stack(names: list[str], x: int, y0: int, pin_side: str) -> dict:
        placed = {}
        y = y0
        for name in names:
            h = _part_h(name)
            if name == "usb_c":
                pins = [("+5V", y + HEADER_H), ("GND", y + HEADER_H + PIN_H)]
                out.extend(_card(
                    "part-usb_c", 1, x, y, CARD_W, h,
                    "USB-C 5V Adapter", "Power",
                    _icon("usb_c", x + 26, y + 30), pins, pin_side))
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
                    _icon(name, x + 26, y + 30), pins, pin_side))
                placed[name] = {"x": x, "y": y, "h": h, "pins": pin_map}
            y += h + 16
        return placed

    left_at = _stack(left_parts, left_x, top, "right")
    right_names = ["usb_c"] + right_parts
    right_at = _stack(right_names, right_x, top, "left")

    rail_row = {"5V": 5, "VIN": 5, "+5V": 5, "GND": 8, "3V3": 16}
    usb = right_at["usb_c"]
    usb_5v = (usb["x"] + 10, usb["pins"]["+5V"])
    usb_gnd = (usb["x"] + 10, usb["pins"]["GND"])
    esp_5v = (esp_x + CARD_W - 10, pin_y.get("5V", pin_y["GND"]))
    esp_gnd = (esp_x + CARD_W - 10, pin_y["GND"])
    h5 = hole("5V", 5, "right")
    hg = hole("GND", 8, "left")
    out += _wire_group(
        "wire-usb_c-VCC", 1, step_n == 1,
        "USB-C +5V → 5V rail (added step 1)",
        _wire_path(usb_5v[0], usb_5v[1], h5[0], h5[1]) +
        " L %.1f %.1f" % esp_5v,
        COLORS["power"], usb_5v[0], usb_5v[1], esp_5v[0], esp_5v[1])
    out += _wire_group(
        "wire-usb_c-GND", 1, step_n == 1,
        "USB-C GND → ground rail (added step 1)",
        _wire_path(usb_gnd[0], usb_gnd[1], hg[0], hg[1]) +
        " L %.1f %.1f" % esp_gnd,
        COLORS["ground"], usb_gnd[0], usb_gnd[1], esp_gnd[0], esp_gnd[1])

    tap_row = dict(rail_row)
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
            else:
                x1, y1 = card["x"] + 10, py
            x2, y2 = esp_x + CARD_W - 10, by
            prefer = tap_row.get(shown, gpio_rows.get(shown, 10))
            if shown in tap_row:
                tap_row[shown] = min(ROWS_N, tap_row[shown] + 3)
            part_hole = hole(shown, prefer, "left" if from_left else "right")
            board_hole = hole(shown, prefer, "left")
            d = _wire_path(x1, y1, part_hole[0], part_hole[1])
            if not from_left:
                for jx, jy in jumper_points(shown, prefer):
                    d += " L %.1f %.1f" % (jx, jy)
            if board_hole != part_hole:
                d += " L %.1f %.1f" % board_hole
            d += " L %.1f %.1f" % (x2, y2)
            added = int(wire.get("step_added", step_n))
            title = "%s %s → %s (added step %s)" % (
                name, part_pin, wire.get("board_pin"), added)
            out += _wire_group(
                "wire-%s-%s" % (name, part_pin), added, added == step_n,
                title, d, _color(wire.get("signal")), x1, y1, x2, y2)

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
            % (bb_x, min(height - 16, bb_y + bb_h + 20)))
    out.append("</svg>")
    return "\n".join(out)
