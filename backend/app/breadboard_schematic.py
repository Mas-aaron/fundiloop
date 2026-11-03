"""Breadboard-layout wiring diagrams for Learning mode (ESP32).

Same netlist as the pin-card schematic, drawn as a mini breadboard:
numbered rows, A–J hole columns, power rails, and jumper paths that
cross the center trench. Parts sit around the board so a student can
toggle between pin cards and this view without changing the wiring.

Wiring is orthogonal (horizontal/vertical segments only, like a tidy
Fritzing layout): each card pin runs a short stub to a vertical lane
in the gap beside the board, then into its hole or rail tap. Power
that must cross the board (5V to the right rail, 3V3 to the left
rail) rides a highway above/below the board instead of cutting
across the holes, and each side taps its near ground rail with one
tie jumper joining them under the board.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from .schematic import svg_id
from .visual_schematic import (
    COLORS, CARD_W, HEADER_H, PAD, PIN_H,
    _board_pin_key, _card, _color, _display_board_pin, _icon, _kind,
    _title,
)

PITCH = 13
ROWS_N = 30
COL_LETTERS = "ABCDEFGHIJ"

LANE_DX = 8      # spacing between vertical lanes in a card/board gap
LANE_MARGIN = 10  # nearest lane's clearance from the board edge
GAP_MIN = 48


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


def _assign_rows(board_pins: list[str], start: int = 22,
                 reserved: frozenset | set = frozenset(),
                 preferred: frozenset | set | None = None
                 ) -> dict[str, int]:
    """Stable row numbers for GPIOs; rails handle power/ground.

    `start` keeps signal rows clear of rail-tap rows so same-gap
    horizontal stubs never share a row across nets; `reserved`
    skips those rows if allocation wraps around the board.
    `preferred` (pin-clear rows) is a soft wish: allocation takes
    a clear row when one is free and any free row otherwise, so it
    always terminates — GPIO pools bound signals well under 30.
    """
    rows: dict[str, int] = {}
    used = {16, 18, 21} | set(reserved)
    cursor = start
    for pin in board_pins:
        if _is_power(pin) or _is_gnd(pin) or _is_3v3(pin):
            continue
        pick = None
        for want_clear in (True, False):
            r = cursor
            for _ in range(ROWS_N + 2):
                if r > ROWS_N:
                    r = 4
                if r not in used and (
                        not want_clear or preferred is None
                        or r in preferred):
                    pick = r
                    break
                r += 1
            if pick is not None:
                break
        if pick is None:  # absurd overflow: reuse, stay finite
            pick = cursor if cursor <= ROWS_N else 4
        rows[pin] = pick
        used.add(pick)
        cursor = pick + 2
    return rows


def _ortho_d(points: list[tuple[float, float]]) -> str:
    """Path through points using H/V where axis-aligned, L otherwise."""
    pts: list[tuple[float, float]] = []
    for p in points:
        if not pts or (abs(p[0] - pts[-1][0]) > 0.05
                       or abs(p[1] - pts[-1][1]) > 0.05):
            pts.append((float(p[0]), float(p[1])))
    if not pts:
        return ""
    out = ["M %.1f %.1f" % pts[0]]
    px, py = pts[0]
    for x, y in pts[1:]:
        if abs(y - py) <= 0.05:
            out.append("H %.1f" % x)
        elif abs(x - px) <= 0.05:
            out.append("V %.1f" % y)
        else:
            out.append("L %.1f %.1f" % (x, y))
        px, py = x, y
    return " ".join(out)


def _assign_lanes(spans: list) -> tuple[dict, int]:
    """Greedy vertical-lane assignment within one card/board gap.

    spans: iterable of (top, bottom, group, key) with top <= bottom.
    Returns ({key: lane}, lane_count). Spans of the same wire group
    may share a lane (same color, invisible overlap); other groups
    keep 5px clearance. Sorted input keeps it deterministic.
    """
    lanes: list[list[tuple]] = []
    out: dict = {}
    for top, bottom, group, key in sorted(spans):
        lane = None
        for i, occ in enumerate(lanes):
            if all(g == group or b + 5 <= top or bottom + 5 <= t
                   for t, b, g in occ):
                lane = i
                occ.append((top, bottom, group))
                break
        if lane is None:
            lanes.append([(top, bottom, group)])
            lane = len(lanes) - 1
        out[key] = lane
    return out, len(lanes)


def _bb_wire_group(wire_id: str, step: int, fresh: bool, title: str,
                   color: str, paths: list[str],
                   dots: list[tuple[float, float]]) -> list[str]:
    """One wire group holding every orthogonal leg of that net."""
    width = "3.2" if fresh else "2.2"
    cls = "schematic-wire schematic-new" if fresh else "schematic-wire schematic-prior"
    opacity = "1" if fresh else "0.42"
    rows = [
        '<g id="%s" class="%s" data-step="%d" opacity="%s">'
        % (svg_id(wire_id), cls, step, opacity),
        "<title>%s</title>" % escape(title),
    ]
    for d in paths:
        rows.append(
            '<path d="%s" fill="none" stroke="%s" stroke-width="%s" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
            % (d, color, width))
    for x, y in dots:
        rows.append(
            '<circle cx="%.1f" cy="%.1f" r="3.4" fill="%s"/>'
            % (x, y, color))
    rows.append("</g>")
    return rows


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

    left_parts = [p for p in parts if _kind(p) in ("Actuator", "Output")]
    right_parts = [p for p in parts if p not in left_parts]
    left_set = set(left_parts)

    def _part_h(name: str) -> int:
        n = 2 if name == "usb_c" else max(1, len(by_part.get(name, [])))
        return HEADER_H + n * PIN_H + PAD

    # Highways crossing the board: left-side 5V rides over the top
    # to the right rail, right-side 3V3 under the bottom to the
    # left rail. The census here must match the specs collected
    # below so every highway gets its own y.
    top_hw = 1  # ESP 5V always feeds the right rail from the left
    bot_hw = 0
    for name in sorted(by_part):
        from_left = name in left_set
        for wire in by_part[name]:
            shown = _display_board_pin(wire.get("board_pin"))
            if from_left and _is_power(shown):
                top_hw += 1
            elif not from_left and _is_3v3(shown):
                bot_hw += 1

    g = _geom()
    bb_h, bb_w = g["height"], g["width"]
    left_h = sum(_part_h(p) + 16 for p in left_parts)
    esp_h = HEADER_H + max(3, len(board_pins)) * PIN_H + PAD
    right_h = _part_h("usb_c") + 16
    right_h += sum(_part_h(p) + 16 for p in right_parts)
    top = 40 + top_hw * 10
    height = max(left_h + esp_h + 24, bb_h, right_h) + 72
    bb_y = top + max(0, (height - 72 - bb_h) // 2)
    esp_y = bb_y + max(0, (bb_h - esp_h) // 2)
    if left_parts:
        esp_y = max(esp_y, top + left_h)

    # Pin y positions (x follows once lanes size the gaps). These
    # formulas must match _stack below, which draws the cards.
    def _stack_ys(names: list[str], y0: int) -> dict:
        ys: dict = {}
        y = y0
        for name in names:
            if name == "usb_c":
                ys[name] = {"+5V": y + HEADER_H,
                            "GND": y + HEADER_H + PIN_H}
            else:
                pin_map = {}
                for i, wire in enumerate(by_part.get(name, [])):
                    pin_map[str(wire.get("part_pin") or "")] = (
                        y + HEADER_H + i * PIN_H)
                ys[name] = pin_map
            y += _part_h(name) + 16
        return ys

    left_ys = _stack_ys(left_parts, top)
    # The ESP shares the left cards' column, so its pins must not
    # share a y with theirs: same-y exit stubs would overlap.
    left_pin_ys = [y for m in left_ys.values() for y in m.values()]
    if left_pin_ys:
        for _ in range(40):
            esp_try = [esp_y + HEADER_H + i * PIN_H
                       for i in range(len(board_pins))]
            if all(abs(a - b) >= 5
                   for a in esp_try for b in left_pin_ys):
                break
            esp_y += 1
    right_ys = _stack_ys(["usb_c"] + right_parts, top)
    pin_y = {
        pin: esp_y + HEADER_H + i * PIN_H
        for i, pin in enumerate(board_pins)
    }
    pin_ys_all = (
        [y for m in left_ys.values() for y in m.values()]
        + [y for m in right_ys.values() for y in m.values()]
        + list(pin_y.values()))

    def _ry(row: int) -> float:
        return bb_y + g["row_y"][row]

    def _row_clear(row: int) -> bool:
        return all(abs(_ry(row) - py) >= 5 for py in pin_ys_all)

    # Rail taps: one shared row per rail, so every same-net tap
    # lands on one bus-like point (overlaps are same-color, hence
    # invisible) and the board can never run out of rows. 5V lives
    # on the right +rail, 3V3 on the left +rail, and each side
    # taps its near ground rail (one tie joins them under the
    # board). Tap rows stay clear of card-pin rows when possible
    # so no two nets share a horizontal run.
    def _rail_row(start: int, avoid: set[int]) -> int:
        row = start
        for _ in range(12):
            r = row if row <= ROWS_N else 4 + (row - ROWS_N - 1)
            if r not in avoid and _row_clear(r):
                return r
            row += 1
        return start if start not in avoid else start + 1

    p5_row = _rail_row(5, set())
    gndR_row = _rail_row(8, {p5_row})
    gndL_row = _rail_row(8, set())
    p3_row = _rail_row(16, {gndL_row})
    tap_rows = [p5_row, gndR_row, gndL_row, p3_row]
    usb_5v_row = esp_5v_row = p5_row
    usb_gnd_row = gndR_row
    esp_gnd_row = gndL_row
    esp_3v3_row = p3_row
    part_tap: dict[int, int] = {}
    for name in sorted(by_part):
        from_left = name in left_set
        for wire in by_part[name]:
            shown = _display_board_pin(wire.get("board_pin"))
            if _is_power(shown):
                part_tap[id(wire)] = p5_row
            elif _is_3v3(shown):
                part_tap[id(wire)] = p3_row
            elif _is_gnd(shown):
                part_tap[id(wire)] = gndL_row if from_left else gndR_row

    clear_rows = frozenset(r for r in range(1, ROWS_N + 1)
                           if _row_clear(r))
    gpio_rows = _assign_rows(
        board_pins, start=max(22, max(tap_rows) + 2),
        reserved=set(tap_rows), preferred=clear_rows)

    bb_bot = bb_y + bb_h
    hole_ys = ([_ry(r) for r in tap_rows]
               + [_ry(r) for r in gpio_rows.values()])

    def _hwy_clear(y: float) -> bool:
        return all(abs(y - z) >= 5 for z in pin_ys_all + hole_ys)

    def _hwy_bump(y: float, step: int) -> float:
        for _ in range(40):
            if _hwy_clear(y):
                return y
            y += step
        return y

    tie_y = _hwy_bump(bb_bot + 12, 1)
    top_y = []
    y = bb_y - 14
    for _ in range(top_hw):
        y = _hwy_bump(y, -1)
        top_y.append(y)
        y -= 9
    bot_y = []
    y = tie_y + 9
    for _ in range(bot_hw):
        y = _hwy_bump(y, 1)
        bot_y.append(y)
        y += 9
    if min(top_y) < 30:
        # Uniform shift: relative clearances are preserved.
        shift = 30 - min(top_y)
        top += shift
        bb_y += shift
        bb_bot += shift
        esp_y += shift
        height += shift
        top_y = [v + shift for v in top_y]
        bot_y = [v + shift for v in bot_y]
        tie_y += shift
        left_ys = _stack_ys(left_parts, top)
        right_ys = _stack_ys(["usb_c"] + right_parts, top)
        pin_y = {
            pin: esp_y + HEADER_H + i * PIN_H
            for i, pin in enumerate(board_pins)
        }
    need_below = max([tie_y] + bot_y) - bb_bot + 16
    min_bottom = need_below + 20 + 18 * len(unresolved)
    if height - (bb_y + bb_h) < min_bottom:
        height = bb_y + bb_h + min_bottom

    # Collect wire legs: ("stub", gap, y_card, x_side, y_hole, hole,
    # key), ("highway", y_card, x_side, why, y_hole, hole, kleft,
    # kright, direction), ("jumper", row), ("tie",). Lane spans only
    # need y, so lanes are assigned before x positions exist.
    groups: dict = {}

    def _group(gid: str, step: int, fresh: bool, title: str,
               color: str) -> dict:
        return groups.setdefault(gid, {"step": step, "fresh": fresh,
                                       "title": title, "color": color,
                                       "legs": []})

    spans_left: list = []
    spans_right: list = []
    key_seq = 0

    def _span(spans: list, group: str, y1: float, y2: float):
        nonlocal key_seq
        if abs(y2 - y1) < 0.5:
            return None  # pure horizontal stub needs no lane
        key = key_seq
        key_seq += 1
        spans.append((min(y1, y2), max(y1, y2), group, key))
        return key

    usb_ys = right_ys["usb_c"]
    _group("wire-usb_c-VCC", 1, step_n == 1,
           "USB-C +5V → 5V rail (added step 1)", COLORS["power"])
    _group("wire-usb_c-GND", 1, step_n == 1,
           "USB-C GND → ground rail (added step 1)", COLORS["ground"])
    groups["wire-usb_c-VCC"]["legs"].append(
        ("stub", "right", usb_ys["+5V"], "right_cards",
         _ry(usb_5v_row), ("rail", "plus_r"),
         _span(spans_right, "wire-usb_c-VCC",
               usb_ys["+5V"], _ry(usb_5v_row))))
    groups["wire-usb_c-GND"]["legs"].append(
        ("stub", "right", usb_ys["GND"], "right_cards",
         _ry(usb_gnd_row), ("rail", "minus_r"),
         _span(spans_right, "wire-usb_c-GND",
               usb_ys["GND"], _ry(usb_gnd_row))))
    top_specs = [("wire-usb_c-VCC", pin_y["5V"], "esp",
                  _ry(esp_5v_row), ("rail", "plus_r"))]
    groups["wire-usb_c-GND"]["legs"].append(
        ("stub", "left", pin_y["GND"], "esp",
         _ry(esp_gnd_row), ("rail", "minus_l"),
         _span(spans_left, "wire-usb_c-GND",
               pin_y["GND"], _ry(esp_gnd_row))))
    _group("wire-esp32-3V3", 1, step_n == 1,
           "ESP32 3V3 → 3V3 rail (added step 1)", COLORS["power"])
    groups["wire-esp32-3V3"]["legs"].append(
        ("stub", "left", pin_y["3V3"], "esp",
         _ry(esp_3v3_row), ("rail", "plus_l"),
         _span(spans_left, "wire-esp32-3V3",
               pin_y["3V3"], _ry(esp_3v3_row))))
    _group("wire-gnd-rail-tie", 1, step_n == 1,
           "GND rails tied (added step 1)", COLORS["ground"])
    groups["wire-gnd-rail-tie"]["legs"].append(("tie",))
    bot_specs: list = []

    for name in sorted(by_part):
        from_left = name in left_set
        card_ys = left_ys if from_left else right_ys
        x_side = "left_cards" if from_left else "right_cards"
        for wire in by_part[name]:
            part_pin = str(wire.get("part_pin") or "")
            shown = _display_board_pin(wire.get("board_pin"))
            gid = "wire-%s-%s" % (name, part_pin)
            added = int(wire.get("step_added", step_n))
            title = "%s %s → %s (added step %s)" % (
                name, part_pin, wire.get("board_pin"), added)
            grp = _group(gid, added, added == step_n, title,
                         _color(wire.get("signal")))
            py = card_ys[name].get(part_pin)
            by = pin_y.get(shown)
            if py is None or by is None:
                continue
            if _is_power(shown):
                row = part_tap[id(wire)]
                if from_left:
                    top_specs.append((gid, py, x_side, _ry(row),
                                      ("rail", "plus_r")))
                else:  # defensive: 5V parts sit left; tap the near rail
                    grp["legs"].append(
                        ("stub", "right", py, x_side, _ry(row),
                         ("rail", "plus_r"),
                         _span(spans_right, gid, py, _ry(row))))
            elif _is_3v3(shown):
                row = part_tap[id(wire)]
                if from_left:  # defensive: 3V3 parts sit right
                    grp["legs"].append(
                        ("stub", "left", py, x_side, _ry(row),
                         ("rail", "plus_l"),
                         _span(spans_left, gid, py, _ry(row))))
                else:
                    bot_specs.append((gid, py, x_side, _ry(row),
                                      ("rail", "plus_l")))
            elif _is_gnd(shown):
                row = part_tap[id(wire)]
                if from_left:
                    grp["legs"].append(
                        ("stub", "left", py, x_side, _ry(row),
                         ("rail", "minus_l"),
                         _span(spans_left, gid, py, _ry(row))))
                else:
                    grp["legs"].append(
                        ("stub", "right", py, x_side, _ry(row),
                         ("rail", "minus_r"),
                         _span(spans_right, gid, py, _ry(row))))
            else:
                row = gpio_rows.get(shown, 10)
                hy = _ry(row)
                if from_left:
                    grp["legs"].append(
                        ("stub", "left", py, x_side, hy, ("col", "D"),
                         _span(spans_left, gid, py, hy)))
                else:
                    grp["legs"].append(
                        ("stub", "right", py, x_side, hy, ("col", "H"),
                         _span(spans_right, gid, py, hy)))
                    grp["legs"].append(("jumper", row))
                grp["legs"].append(
                    ("stub", "left", by, "esp", hy, ("col", "B"),
                     _span(spans_left, gid, by, hy)))

    for k, (gid, y_card, x_side, y_hole, hole) in enumerate(
            sorted(top_specs, key=lambda s: s[0])):
        why = top_y[k]
        kl = _span(spans_left, gid, y_card, why)
        kr = _span(spans_right, gid, why, y_hole)
        groups[gid]["legs"].append(
            ("highway", y_card, x_side, why, y_hole, hole, kl, kr, "LR"))
    for j, (gid, y_card, x_side, y_hole, hole) in enumerate(
            sorted(bot_specs, key=lambda s: s[0])):
        why = bot_y[j]
        kr = _span(spans_right, gid, y_card, why)
        kl = _span(spans_left, gid, why, y_hole)
        groups[gid]["legs"].append(
            ("highway", y_card, x_side, why, y_hole, hole, kl, kr, "RL"))

    lanes_left, n_lane_left = _assign_lanes(spans_left)
    lanes_right, n_lane_right = _assign_lanes(spans_right)
    gap_left = max(GAP_MIN, 26 + LANE_DX * max(0, n_lane_left - 1))
    gap_right = max(GAP_MIN, 26 + LANE_DX * max(0, n_lane_right - 1))
    left_x = 24
    bb_x = left_x + CARD_W + gap_left
    esp_x = left_x
    right_x = bb_x + bb_w + gap_right
    width = right_x + CARD_W + 24
    bb_right = bb_x + bb_w

    def _lane_x(gap: str, key) -> float:
        if gap == "left":
            return bb_x - LANE_MARGIN - lanes_left[key] * LANE_DX
        return bb_right + LANE_MARGIN + lanes_right[key] * LANE_DX

    def _hole_x(hole: tuple[str, str]) -> float:
        kind, val = hole
        if kind == "col":
            return bb_x + g["cols"][val]
        return bb_x + g[val]

    def _card_x(side: str) -> float:
        if side == "right_cards":
            return right_x + 10
        return esp_x + CARD_W - 10  # left cards and ESP share a column

    def _emit_stub(gap: str, y_card: float, x_side: str, y_hole: float,
                   hole: tuple[str, str], key) -> tuple[str, list]:
        x1 = _card_x(x_side)
        x2 = _hole_x(hole)
        if key is None:
            d = _ortho_d([(x1, y_card), (x2, y_hole)])
        else:
            lx = _lane_x(gap, key)
            d = _ortho_d([(x1, y_card), (lx, y_card),
                          (lx, y_hole), (x2, y_hole)])
        return d, [(x1, y_card), (x2, y_hole)]

    def _emit_highway(y_card: float, x_side: str, why: float,
                      y_hole: float, hole: tuple[str, str],
                      kl, kr, direction: str) -> tuple[str, list]:
        x1 = _card_x(x_side)
        x2 = _hole_x(hole)
        if direction == "LR":  # card left → rail right, over the top
            lx1 = _lane_x("left", kl) if kl is not None else x1
            lx2 = _lane_x("right", kr) if kr is not None else x2
        else:  # card right → rail left, under the bottom
            lx1 = _lane_x("right", kr) if kr is not None else x1
            lx2 = _lane_x("left", kl) if kl is not None else x2
        d = _ortho_d([(x1, y_card), (lx1, y_card), (lx1, why),
                      (lx2, why), (lx2, y_hole), (x2, y_hole)])
        return d, [(x1, y_card), (x2, y_hole)]

    def _emit_jumper(row: int) -> str:
        y = _ry(row)
        ex = bb_x + g["cols"]["E"]
        fx = bb_x + g["cols"]["F"]
        return "M %.1f %.1f Q %.1f %.1f %.1f %.1f" % (
            ex, y, (ex + fx) / 2.0, y - 9, fx, y)

    def _emit_tie() -> tuple[str, list]:
        y = _ry(ROWS_N)
        xl = bb_x + g["minus_l"]
        xr = bb_x + g["minus_r"]
        d = _ortho_d([(xl, y), (xl, tie_y), (xr, tie_y), (xr, y)])
        return d, [(xl, y), (xr, y)]

    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" role="img">' % (width, height, width, height),
        "<title>%s</title>"
        % escape("Step %s breadboard layout (%s)" % (step_n, board)),
        '<rect width="100%" height="100%" fill="#0a0a0c"/>',
        '<text x="24" y="22" font-family="sans-serif" font-size="12" '
        'fill="#71717a">Step %s — breadboard view'
        "</text>" % step_n,
        '<g id="breadboard" transform="translate(%d %d)">' % (bb_x, bb_y),
    ]
    out += _draw_board(g)
    out.append("</g>")

    esp_pins = [(pin, pin_y[pin]) for pin in board_pins]
    out.append('<g id="part-esp32" class="schematic-part" data-step="1">')
    out.append("<title>ESP32 DevKit v1 (Board)</title>")
    out.append(
        '<rect x="%d" y="%d" width="%d" height="%d" rx="12" fill="#141418" '
        'stroke="#2a2a32" stroke-width="1.4"/>'
        % (esp_x, esp_y, CARD_W, esp_h))
    out.append(_icon("esp32", esp_x + 22, esp_y + 26))
    out.append(
        '<text x="%d" y="%d" font-family="sans-serif" font-size="12" '
        'font-weight="700" fill="#f4f4f5">ESP32 DevKit v1</text>'
        % (esp_x + 40, esp_y + 24))
    out.append(
        '<text x="%d" y="%d" font-family="sans-serif" font-size="9" '
        'fill="#9ca3af">Board</text>' % (esp_x + 40, esp_y + 38))
    for pin, py in esp_pins:
        out.append(
            '<text x="%d" y="%d" font-family="sans-serif" font-size="10" '
            'fill="#d4d4d8">%s</text>'
            % (esp_x + 12, py + 4, escape(pin)))
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
                    _icon("usb_c", x + 22, y + 26), pins, pin_side))
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
                    _icon(name, x + 22, y + 26), pins, pin_side))
                placed[name] = {"x": x, "y": y, "h": h, "pins": pin_map}
            y += h + 16
        return placed

    _stack(left_parts, left_x, top, "right")
    _stack(["usb_c"] + right_parts, right_x, top, "left")

    for gid in sorted(groups):
        info = groups[gid]
        paths: list[str] = []
        dots: list[tuple[float, float]] = []
        for leg in info["legs"]:
            if leg[0] == "stub":
                _, gap, y1, xs, y2, hole, key = leg
                d, dd = _emit_stub(gap, y1, xs, y2, hole, key)
                paths.append(d)
                dots.extend(dd)
            elif leg[0] == "highway":
                (_, y1, xs, why, y2, hole, kl, kr,
                 direction) = leg
                d, dd = _emit_highway(y1, xs, why, y2, hole, kl, kr,
                                      direction)
                paths.append(d)
                dots.extend(dd)
            elif leg[0] == "jumper":
                paths.append(_emit_jumper(leg[1]))
            elif leg[0] == "tie":
                d, dd = _emit_tie()
                paths.append(d)
                dots.extend(dd)
        if not paths:
            continue
        out += _bb_wire_group(gid, info["step"], info["fresh"],
                              info["title"], info["color"], paths, dots)

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
