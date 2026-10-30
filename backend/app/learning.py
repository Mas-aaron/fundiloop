"""Learning execution: sensor-gated plan steps + verified code delivery.

The model proposes plans and phrases hints. This module (with the serial
probe) decides pass/fail, advances steps, and gates firmware delivery.
Every AVAILABLE_TESTS key maps here to a real sensor read; anything the
probe cannot run fails honestly instead of passing silently.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .prompts import learning_code_prompt, learning_hint_prompt


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_addr(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text.startswith("0x") else ("0x" + text if text else "")


def _want_addresses(expect: object) -> list[str]:
    if isinstance(expect, dict):
        raw = expect.get("addresses", expect.get("contains", expect.get("address", [])))
        raw = raw if isinstance(raw, list) else [raw]
        return sorted({_norm_addr(a) for a in raw if _norm_addr(a)})
    if isinstance(expect, list):
        return sorted({_norm_addr(a) for a in expect if _norm_addr(a)})
    return []


def run_learning_verification(verify: str, expect: object, probe,
                              pin_map: dict | None = None) -> dict:
    """Run one AVAILABLE_TESTS check against a live ProbeReading.

    Returns {"test", "passed", "measured", "at"}. Never raises for bad
    input: unknown tests and unrunnable pins fail with evidence.
    """
    pin_map = pin_map or {}
    test = str(verify or "").strip().upper()
    devices = list(probe.i2c_devices or [])
    lowered = {_norm_addr(d) for d in devices}
    needs_confirmation = False
    if test == "RUN_I2C_SCAN":
        want = _want_addresses(expect)
        missing = [a for a in want if a not in lowered]
        passed = bool(probe.connected) and not missing
        measured = "I2C found [%s]" % (", ".join(devices) or "none")
        if missing:
            measured += " MISSING %s" % ", ".join(missing)
    elif test == "READ_SENSOR":
        t = probe.temperature_c
        passed = t is not None and 10.0 <= t <= 40.0
        measured = ("T=%.1fC H=%.1f%% P=%.1fhPa"
                    % (t, probe.humidity_pct or 0.0, probe.pressure_hpa or 0.0)
                    ) if t is not None else "sensor read failed"
    elif test == "CHECK_OLED":
        if probe.oled_ok is None:
            passed = False
            measured = ("OLED state unknown over serial "
                        "(probe firmware cannot assess it)")
            needs_confirmation = True
        else:
            passed = probe.oled_ok is True
            measured = "OLED lit" if passed else "OLED dark or unreachable"
    elif test == "CHECK_GPIO":
        pin = pin_map.get("led_gpio", 2)
        if isinstance(expect, dict) and expect.get("pin") is not None:
            try:
                pin = int(expect["pin"])
            except (TypeError, ValueError):
                pass
        if pin != pin_map.get("led_gpio", 2):
            passed = False
            measured = ("GPIO%d readback not supported by probe firmware "
                        "(only GPIO%d LED check)" % (pin, pin_map.get("led_gpio", 2)))
        elif probe.led_ok is None:
            # Unknown, not failed: the probe cannot assess this pin (plain
            # sketch, no patient protocol), so the technician confirms.
            passed = False
            measured = ("GPIO%d blink unverifiable over serial "
                        "(probe firmware cannot assess it)" % pin)
            needs_confirmation = True
        else:
            passed = probe.led_ok is True
            measured = ("GPIO%d toggles (LED check)" % pin) if passed \
                else "GPIO%d did not toggle" % pin
    elif test == "CHECK_SERIAL":
        try:
            raw = json.loads(probe.raw or "{}")
        except (TypeError, ValueError):
            raw = {}
        replies = [v for v in raw.values() if isinstance(v, dict)] if isinstance(raw, dict) else []
        ok = [r for r in replies if "error" not in r]
        passed = bool(probe.connected) and bool(ok)
        measured = "serial traffic: %d replies" % len(ok) if passed \
            else "no live serial traffic (%s)" % (
                next((str(r.get("error")) for r in replies if "error" in r), "board silent")[:120])
    else:
        passed, measured = False, "unknown verification test %r" % test
    return {"test": test, "passed": passed, "measured": measured,
            "needs_confirmation": needs_confirmation, "at": _now()}


def build_hint(step: dict, verification: dict, Muse) -> str:
    """One targeted fix hint. Template in mock mode, Muse when live."""
    fallback = "%s. Check the wiring for step %s, then press CHECK NOW." % (
        verification.get("measured", ""), step.get("n", "?"))
    if getattr(Muse, "mock", False):
        return fallback
    try:
        hint = Muse.complete_text(
            "You write short technician hints. Reply with one sentence, "
            "under 20 words, in plain spoken English (no ALL-CAPS words).",
            learning_hint_prompt(step, verification)).strip()
    except Exception:
        return fallback
    return hint.strip('" ') if hint else fallback


def generate_code(session: dict, Muse, draft: bool = False) -> str:
    """Generate the project's .ino (final when DONE, draft on request)."""
    if getattr(Muse, "mock", False):
        code = _mock_firmware(session, draft=draft)
    else:
        from .adapters import MetaAPIError

        text = Muse.complete_text(
            "You write Arduino firmware. Return only code.",
            learning_code_prompt(
                session.get("project_title", session.get("project_id", "project")),
                session.get("plan", []), session.get("visible_parts", []),
                session.get("verified", {}), session.get("pin_map", {}),
                draft=draft))
        code = text.strip()
        if code.startswith("```"):
            import re

            code = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", code).strip()
        if not code:
            raise MetaAPIError("Muse returned empty firmware")
    if draft:
        steps_done = len((session.get("verified") or {}).get("steps", []))
        steps_total = len(session.get("plan", []))
        code = ("// DRAFT — hardware not fully verified (%d of %d steps). "
                "Pins/addresses below come from the plan and profile "
                "defaults; verify each step, then fetch the final code.\n"
                % (steps_done, steps_total)) + code
    return code


def _mock_firmware(session: dict, draft: bool = False) -> str:
    """Deterministic mock .ino using the session's pins/addresses."""
    pin_map = session.get("pin_map", {}) or {}
    sda, scl, led = pin_map.get("sda", 21), pin_map.get("scl", 22), pin_map.get("led_gpio", 2)
    verified = session.get("verified", {}) or {}
    addrs = ", ".join(verified.get("i2c", [])) or "none seen"
    title = session.get("project_title", session.get("project_id", "project"))
    board = pin_map.get("label", pin_map.get("board", "?"))
    provenance = ("plan/profile values, hardware unverified"
                  if draft else "pins/address from verified sensor evidence")
    lines = [
        "// %s for %s (mock firmware; %s)" % (title, board, provenance),
        "// I2C seen on bus: %s" % addrs,
        "#include <Arduino.h>",
        "#include <Wire.h>",
        "#define SDA_PIN %s" % sda,
        "#define SCL_PIN %s" % scl,
        "#define LED_PIN %s" % led,
        "",
        "void setup() {",
        "  Serial.begin(115200);",
        "  Wire.begin(SDA_PIN, SCL_PIN);",
        "  pinMode(LED_PIN, OUTPUT);",
        '  Serial.println("verified build running");',
        "}",
        "",
        "void loop() {",
        "  digitalWrite(LED_PIN, HIGH); delay(500);",
        "  digitalWrite(LED_PIN, LOW); delay(500);",
        "}",
        "",
    ]
    return "\n".join(lines)
