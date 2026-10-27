"""Fault ranker: compare observed state against the known-good circuit graph.

Evidence sources: SAM segments, I2C devices, serial errors, and pin
definitions parsed from the technician's pasted code snippet. Each of the
5 defined faults gets a 0..1 score plus cited evidence, sorted best first.
Deterministic: used by the Muse mock and (later) as the evidence front-end
for real-mode reasoning.
"""
from __future__ import annotations

import json
import re

from .models import VerificationTest

BME_ADDRS = ("0x76", "0x77")
OLED_ADDR = "0x3C"

#: A fault at or above this score is worth pursuing as the hypothesis.
RANK_THRESHOLD = 0.5

#: Verification test that proves/disproves each fault after a fix attempt.
FAULT_TESTS = {
    "SDA_SCL_SWAPPED": VerificationTest.RUN_I2C_SCAN,
    "LOOSE_GND": VerificationTest.PING,
    "CODE_PIN_MISMATCH": VerificationTest.SELFTEST,
    "SENSOR_DISCONNECTED": VerificationTest.RUN_I2C_SCAN,
    "LED_REVERSED": VerificationTest.BLINK_LED,
}


def parse_code_pins(code: str) -> dict:
    """Extract LED + I2C pin numbers from an Arduino snippet (best effort)."""
    pins: dict = {"led": None, "sda": None, "scl": None}
    if not code:
        return pins
    m = re.search(r"(?:GPIO|LED[_\s-]?PIN|pin)\D{0,8}(\d{1,2})", code, re.IGNORECASE)
    if m:
        pins["led"] = int(m.group(1))
    m = re.search(r"Wire\.begin\(\s*(\d{1,2})\s*,\s*(\d{1,2})", code)
    if m:
        pins["sda"], pins["scl"] = int(m.group(1)), int(m.group(2))
    for name in ("sda", "scl"):
        m = re.search(r"#define\s+%s(?:_PIN)?\s+(\d{1,2})" % name.upper(), code, re.IGNORECASE)
        if m:
            pins[name] = int(m.group(1))
    return pins


def serial_errors(probe) -> list[str]:
    """Collect 'error' strings from the probe's raw replies."""
    try:
        raw = json.loads(getattr(probe, "raw", "") or "{}")
    except (json.JSONDecodeError, TypeError):
        return []
    errors = []
    for reply in raw.values():
        if isinstance(reply, dict) and reply.get("error"):
            errors.append("%s: %s" % (reply.get("cmd", "?"), reply["error"]))
    return errors


def rank_faults(probe, sam: dict | None, code_snippet: str = "", graph=None) -> list[dict]:
    """Score the 5 defined faults against observed state, best first."""
    sam = sam or {}
    devices = list(getattr(probe, "i2c_devices", []) or [])
    connected = bool(getattr(probe, "connected", False))
    temp = getattr(probe, "temperature_c", None)
    oled_ok = getattr(probe, "oled_ok", None)
    led_ok = getattr(probe, "led_ok", None)
    has_bme = any(a in devices for a in BME_ADDRS)
    has_oled = OLED_ADDR in devices
    errors = serial_errors(probe)
    pins = parse_code_pins(code_snippet or "")
    labels = [d.get("label", "") for d in sam.get("detections", [])]
    wires_ok = sam.get("wires_ok", True)

    led_gpio = getattr(graph, "led_gpio", 2)
    sda_gpio = getattr(graph, "i2c_sda_gpio", 21)
    scl_gpio = getattr(graph, "i2c_scl_gpio", 22)
    scan = "I2C scan found [%s]" % (", ".join(devices) or "nothing")

    ranked: dict[str, list] = {}  # fault_id -> [score, evidence]

    def add(fid: str, score: float, evidence: str):
        cur = ranked.get(fid, [0.0, []])
        cur[0] = max(cur[0], min(1.0, score))
        if evidence and evidence not in cur[1]:
            cur[1].append(evidence)
        ranked[fid] = cur

    for fid in FAULT_TESTS:
        ranked[fid] = [0.05, ["no evidence for %s" % fid]]

    # SENSOR_DISCONNECTED: OLED on bus, BME missing, sensor read errors.
    if has_oled and not has_bme:
        add("SENSOR_DISCONNECTED", 0.95, scan + " (0x3C present, 0x76/0x77 missing)")
    elif not has_bme:
        add("SENSOR_DISCONNECTED", 0.4, scan + " (BME280 absent)")
    for err in errors:
        if ("BME280" in err or "READ_SENSOR" in err) and devices:
            # Only module-specific when the bus is otherwise alive; on a
            # totally silent bus a sensor error is expected fallout.
            add("SENSOR_DISCONNECTED", 0.9, "serial error: %s" % err)
    if labels and not any("BME280" in label for label in labels):
        add("SENSOR_DISCONNECTED", 0.65, "vision: BME280 module not visible in frame")

    # LOOSE_GND: dead serial, dark-but-present OLED, silent bus.
    if not connected:
        add("LOOSE_GND", 0.95, "no PING reply from patient board over USB serial")
    if has_oled and oled_ok is False:
        add("LOOSE_GND", 0.7, "OLED answers on I2C but the screen stays dark (VCC/GND suspect)")
    if not devices and connected:
        add("LOOSE_GND", 0.5, scan + " though the board answers (power/ground suspect)")
    if wires_ok is False:
        add("LOOSE_GND", 0.6, "vision: loose or missing jumper wires")

    # SDA_SCL_SWAPPED: live board, totally silent bus, or swapped code pins.
    if not devices and connected:
        add("SDA_SCL_SWAPPED", 0.85, scan + " though the board is alive (bus wires suspect)")
    if pins["sda"] is not None and pins["scl"] is not None:
        if (pins["sda"], pins["scl"]) == (scl_gpio, sda_gpio):
            add("SDA_SCL_SWAPPED", 0.9,
                "code sets SDA=%d SCL=%d but the graph wires SDA=%d SCL=%d"
                % (pins["sda"], pins["scl"], sda_gpio, scl_gpio))
        elif (pins["sda"], pins["scl"]) != (sda_gpio, scl_gpio):
            add("CODE_PIN_MISMATCH", 0.7,
                "code I2C pins SDA=%d SCL=%d differ from graph SDA=%d SCL=%d"
                % (pins["sda"], pins["scl"], sda_gpio, scl_gpio))

    # CODE_PIN_MISMATCH: firmware drives a different LED pin than the graph.
    if pins["led"] is not None and pins["led"] != led_gpio:
        add("CODE_PIN_MISMATCH", 0.9,
            "code drives GPIO %d but the graph wires the LED to GPIO %d"
            % (pins["led"], led_gpio))

    # LED_REVERSED: GPIO toggles, LED stays dark, code pin matches (or no code).
    if led_ok is False:
        if pins["led"] is not None and pins["led"] != led_gpio:
            add("LED_REVERSED", 0.3, "LED dead, but code pin mismatch is the likelier cause")
        else:
            add("LED_REVERSED", 0.85, "GPIO %d toggles but the LED never lights" % led_gpio)
    if temp is not None and not 10.0 <= temp <= 40.0:
        add("SENSOR_DISCONNECTED", 0.6, "sensor reads %.1f C, outside the plausible range" % temp)

    out = [{"fault_id": fid, "score": round(score, 2), "evidence": ev}
           for fid, (score, ev) in ranked.items()]
    out.sort(key=lambda r: r["score"], reverse=True)
    return out
