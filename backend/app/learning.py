"""Learning execution: sensor-gated plan steps + verified code delivery.

The model proposes plans and phrases hints. This module (with the serial
probe) decides pass/fail, advances steps, and gates firmware delivery.
Every AVAILABLE_TESTS key maps here to a real sensor read; anything the
probe cannot run fails honestly instead of passing silently.
"""
from __future__ import annotations

import json
import os
import textwrap
from datetime import datetime, timezone

from .prompts import (
    learning_code_prompt,
    learning_hint_prompt,
    learning_step_code_prompt,
)


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

    Returns {"test", "passed", "measured", "board_state", "at"}. Never
    raises for bad input: unknown tests and unrunnable pins fail with
    evidence. board_state carries the probe's no-board classification
    ("" when the board answered or the test is not serial).
    """
    pin_map = pin_map or {}
    test = str(verify or "").strip().upper()
    devices = list(probe.i2c_devices or [])
    lowered = {_norm_addr(d) for d in devices}
    needs_confirmation = False
    board_state = ""
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
        if passed:
            measured = "serial traffic: %d replies" % len(ok)
        else:
            err = next((str(r.get("error")) for r in replies if "error" in r),
                       "board silent")
            measured = "no live serial traffic (%s)" % textwrap.shorten(
                err, width=120, placeholder="…")
            ping = raw.get("ping") if isinstance(raw, dict) else None
            if isinstance(ping, dict):
                board_state = str(ping.get("board_state") or "")
    else:
        passed, measured = False, "unknown verification test %r" % test
    return {"test": test, "passed": passed, "measured": measured,
            "board_state": board_state,
            "needs_confirmation": needs_confirmation, "at": _now()}


def _model_text_usable(text: str) -> bool:
    """True when the model's reply is usable output worth showing.

    Rejects refusals and JSON blobs so a guardrail trip degrades to
    the template instead of leaking into the UI. Technical caps (SDA,
    COM9) still pass: only the refusal shape is rejected, never the
    vocabulary.
    """
    text = (text or "").strip()
    if not text or "refusal" in text.lower():
        return False
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            return True
        return not isinstance(parsed, dict)
    return True


def _board_hint(state: str) -> str | None:
    """Instant guidance for known no-board states (no model call).

    The probe already diagnosed the failure class; calling the model
    here only burns ~7s to restate it. Each line is guidance only —
    the caller speaks measured separately.
    """
    if state in ("no_ports", "no_usb"):
        return ("No USB board found. Check the USB data cable and driver, "
                "then press CHECK NOW.")
    if state.startswith("silent:"):
        return ("The board on %s never answers. Flash the patient sketch, "
                "then press CHECK NOW." % state[len("silent:"):])
    if state.startswith("unreachable:"):
        return ("%s is busy or blocked. Close any serial monitor using it, "
                "then press CHECK NOW." % state[len("unreachable:"):])
    return None


def _measured_hint(verification: dict) -> str | None:
    """Instant guidance for classified sensor failures (no model call).

    The verification already named the failure class; asking the model
    here only burns ~5s to restate it. Each line is guidance only —
    the caller speaks measured separately.
    """
    test = str(verification.get("test") or "").strip().upper()
    measured = str(verification.get("measured") or "")
    if test == "RUN_I2C_SCAN" and "MISSING" in measured:
        return ("An expected address is missing from the bus. Reseat the "
                "SDA and SCL wires, then press CHECK NOW.")
    if test == "READ_SENSOR" and "sensor read failed" in measured:
        return ("The sensor is not answering. Check its power and wiring, "
                "then press CHECK NOW.")
    return None


def build_hint(step: dict, verification: dict, Muse) -> str:
    """One targeted fix hint. Template in mock mode, Muse when live.

    Known no-board states answer instantly from the probe's diagnosis,
    and classified sensor failures (I2C-missing, dead sensor) from
    templates; the caller speaks measured separately, so hints never
    repeat it.
    """
    fallback = "Check the wiring for step %s, then press CHECK NOW." % (
        step.get("n", "?"))
    if getattr(Muse, "mock", False):
        return fallback
    known = _board_hint(verification.get("board_state") or "")
    if known:
        return known
    instant = _measured_hint(verification)
    if instant:
        return instant
    try:
        hint = Muse.complete_text(
            "You write short technician hints. Reply with one sentence, "
            "under 20 words, in plain spoken English (no ALL-CAPS words "
            "— write port and test names as plain lowercase words).",
            learning_hint_prompt(step, verification)).strip()
    except Exception:
        return fallback
    if not _model_text_usable(hint):
        return fallback
    return hint.strip('" ') if hint else fallback


def _project_code_key(session: dict, draft: bool) -> str:
    """Cache key for generate_code: draft flag + verified evidence.

    Plan, pins, and title never change within a session; only the
    verified evidence grows, so it alone (plus the draft flag) keys
    the cached firmware.
    """
    return json.dumps({"draft": bool(draft),
                       "verified": session.get("verified") or {}},
                      sort_keys=True, default=str)


def generate_code(session: dict, Muse, draft: bool = False) -> str:
    """Generate the project's .ino (final when DONE, draft on request).

    Cached per session: refetching unchanged firmware (final twice,
    or a draft before the next step passes) never burns another
    ~17s model call.
    """
    cache = session.setdefault("project_code", {})
    key = _project_code_key(session, draft)
    cached = cache.get(key)
    if isinstance(cached, str) and cached:
        return cached
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
    cache[key] = code
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


PATIENT_SKETCH = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "firmware", "patient_board", "patient_board.ino")


def read_patient_sketch() -> str:
    """Return the patient sketch text. Raises OSError when missing."""
    with open(PATIENT_SKETCH, encoding="utf-8") as fh:
        return fh.read()


def _wire_begin(board: str) -> str:
    """I2C init line: ESP32 takes pins, AVR-style cores take none."""
    if (board or "").strip().lower() in ("esp32_devkit", "esp8266"):
        return "Wire.begin(SDA_PIN, SCL_PIN);"
    return "Wire.begin();"


def _step_pins(pin_map: dict) -> tuple:
    pin_map = pin_map or {}
    return (pin_map.get("sda", 21), pin_map.get("scl", 22),
            pin_map.get("led_gpio", 2),
            pin_map.get("label", pin_map.get("board", "?")))


def _code_blink(n, board: str, pin_map: dict) -> str:
    sda, scl, led, label = _step_pins(pin_map)
    return "\n".join([
        "// Step %s: blink the builtin LED (%s)." % (n, label),
        "#define LED_PIN %s" % led,
        "",
        "void setup() {",
        "  Serial.begin(115200);",
        "  pinMode(LED_PIN, OUTPUT);",
        "}",
        "",
        "void loop() {",
        '  digitalWrite(LED_PIN, HIGH); Serial.println("LED ON"); delay(500);',
        '  digitalWrite(LED_PIN, LOW); Serial.println("LED OFF"); delay(500);',
        "}",
        "",
    ])


def _code_i2c_scan(n, board: str, pin_map: dict) -> str:
    sda, scl, led, label = _step_pins(pin_map)
    return "\n".join([
        "// Step %s: scan the I2C bus, print found addresses (%s)." % (n, label),
        "#include <Wire.h>",
        "#define SDA_PIN %s" % sda,
        "#define SCL_PIN %s" % scl,
        "",
        "void setup() {",
        "  Serial.begin(115200);",
        "  %s" % _wire_begin(board),
        '  Serial.println("I2C scan:");',
        "  for (uint8_t addr = 0x08; addr < 0x78; addr++) {",
        "    Wire.beginTransmission(addr);",
        "    if (Wire.endTransmission() == 0) {",
        '      Serial.print("found 0x");',
        "      Serial.println(addr, HEX);",
        "    }",
        "  }",
        '  Serial.println("scan done");',
        "}",
        "",
        "void loop() { delay(2000); }",
        "",
    ])


def _code_read_sensor(n, board: str, pin_map: dict) -> str:
    sda, scl, led, label = _step_pins(pin_map)
    return "\n".join([
        "// Step %s: read BME280 temperature/humidity/pressure (%s)." % (n, label),
        "// Library: Adafruit BME280 Library (+ Adafruit Unified Sensor).",
        "#include <Wire.h>",
        "#include <Adafruit_Sensor.h>",
        "#include <Adafruit_BME280.h>",
        "#define SDA_PIN %s" % sda,
        "#define SCL_PIN %s" % scl,
        "",
        "Adafruit_BME280 bme;",
        "",
        "void setup() {",
        "  Serial.begin(115200);",
        "  %s" % _wire_begin(board),
        "  if (!bme.begin(0x76) && !bme.begin(0x77)) {",
        '    Serial.println("BME280 not found");',
        "    while (1) delay(1000);",
        "  }",
        "}",
        "",
        "void loop() {",
        '  Serial.print("T="); Serial.print(bme.readTemperature());',
        '  Serial.print("C H="); Serial.print(bme.readHumidity());',
        '  Serial.print("% P="); Serial.print(bme.readPressure() / 100.0);',
        '  Serial.println("hPa");',
        "  delay(2000);",
        "}",
        "",
    ])


def _code_oled(n, board: str, pin_map: dict) -> str:
    sda, scl, led, label = _step_pins(pin_map)
    return "\n".join([
        "// Step %s: show text on the SSD1306 OLED (%s)." % (n, label),
        "// Libraries: Adafruit SSD1306 + Adafruit GFX Library.",
        "#include <Wire.h>",
        "#include <Adafruit_GFX.h>",
        "#include <Adafruit_SSD1306.h>",
        "#define SDA_PIN %s" % sda,
        "#define SCL_PIN %s" % scl,
        "#define OLED_ADDR 0x3C",
        "",
        "Adafruit_SSD1306 display(128, 64, &Wire, -1);",
        "",
        "void setup() {",
        "  Serial.begin(115200);",
        "  %s" % _wire_begin(board),
        "  if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {",
        '    Serial.println("OLED not found");',
        "    while (1) delay(1000);",
        "  }",
        "  display.clearDisplay();",
        "  display.setTextSize(2);",
        "  display.setTextColor(SSD1306_WHITE);",
        "  display.setCursor(0, 0);",
        '  display.println("FundiLoop");',
        "  display.display();",
        '  Serial.println("OLED hello shown");',
        "}",
        "",
        "void loop() { delay(2000); }",
        "",
    ])


def _code_generic(n, board: str, pin_map: dict) -> str:
    sda, scl, led, label = _step_pins(pin_map)
    return "\n".join([
        "// Step %s: serial echo check (%s). Upload, open Serial Monitor at" % (n, label),
        "// 115200 baud, and confirm READY appears.",
        "",
        "void setup() {",
        "  Serial.begin(115200);",
        '  Serial.println("READY");',
        "}",
        "",
        "void loop() {",
        "  if (Serial.available()) {",
        '    Serial.print("echo: ");',
        "    Serial.println(Serial.readStringUntil('\\n'));",
        "  }",
        "}",
        "",
    ])


def generate_step_code(session: dict, step: dict, Muse,
                         allow_model: bool = True) -> tuple[str, str]:
    """Return (code, source) for one plan step.

    Known verify types answer instantly from board-aware templates (no
    model call, fully offline); CHECK_SERIAL serves the patient sketch
    itself. Unknown types ask the model when live (cached per step)
    and fall back to the generic echo sketch in mock mode or on any
    refusal-shaped reply. Sources: template, patient, model, generic.
    With allow_model=False (view auto-attach), uncached unknown types
    return ("", "none") instead of burning a model call.
    """
    pin_map = session.get("pin_map", {}) or {}
    board = session.get("board", "esp32_devkit")
    test = str(step.get("verify", "")).strip().upper()
    n = step.get("n", "?")
    cached = (session.get("step_code") or {}).get(n)
    if isinstance(cached, dict) and cached.get("code"):
        return cached["code"], cached.get("source", "model")
    builders = {
        "CHECK_GPIO": _code_blink,
        "RUN_I2C_SCAN": _code_i2c_scan,
        "READ_SENSOR": _code_read_sensor,
        "CHECK_OLED": _code_oled,
    }
    if test in builders:
        return builders[test](n, board, pin_map), "template"
    if test == "CHECK_SERIAL":
        try:
            return read_patient_sketch(), "patient"
        except OSError:
            return _code_generic(n, board, pin_map), "generic"
    if getattr(Muse, "mock", False):
        return _code_generic(n, board, pin_map), "generic"
    if not allow_model:
        return "", "none"
    from .adapters import MetaAPIError

    text = Muse.complete_text(
        "You write Arduino firmware. Return only code.",
        learning_step_code_prompt(step, pin_map)).strip()
    if text.startswith("```"):
        import re

        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    if not _model_text_usable(text):
        return _code_generic(n, board, pin_map), "generic"
    session.setdefault("step_code", {})[n] = {"code": text, "source": "model"}
    return text, "model"
