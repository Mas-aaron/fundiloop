"""USB serial probe for the patient ESP32 board.

Speaks the Prompt-1 command set (PING, I2C_SCAN, READ_SENSOR, SELFTEST)
and parses the structured JSON lines from firmware/patient_board.

Mock mode (MOCK_META=true, the default) returns realistic sample data so
the whole loop runs with no hardware. With MOCK_META=false the probe uses
the real serial port and falls back to mock only if the port fails.
"""
from __future__ import annotations

import json
import os
import time

from .models import ProbeReading

COMMANDS = ("PING", "I2C_SCAN", "READ_SENSOR", "SELFTEST")

_DEFAULT_PORT = "COM3" if os.name == "nt" else "/dev/ttyUSB0"


def _mock_enabled() -> bool:
    return os.environ.get("MOCK_META", "true").lower() in ("1", "true", "yes")


class SerialProbe:
    def __init__(
        self,
        port: str | None = None,
        baud: int | None = None,
        mock: bool | None = None,
        timeout: float = 4.0,
    ) -> None:
        self.port = port or os.environ.get("SERIAL_PORT") or _DEFAULT_PORT
        self.baud = baud or int(os.environ.get("SERIAL_BAUD", "115200"))
        self.mock = _mock_enabled() if mock is None else mock
        self.timeout = timeout
        self.simulate_fault: str | None = None
        self._ser = None  # persistent handle, opened lazily

    # -- public API ----------------------------------------------------
    def send(self, command: str) -> dict:
        """Send one command, return the parsed JSON reply (never raises)."""
        command = command.strip().upper()
        if command not in COMMANDS:
            return {"cmd": command, "error": "unknown command %r" % command}
        if self.mock:
            return self._mock_response(command)
        try:
            return self._real_send(command)
        except Exception as exc:  # demo robustness: fall back to mock
            reply = self._mock_response(command)
            reply["mock_fallback"] = "%s: %s" % (self.port, exc)
            return reply

    def read_all(self, simulate_fault: str | None = None) -> ProbeReading:
        """Run the full sense pass and aggregate one ProbeReading."""
        self.simulate_fault = simulate_fault
        ping = self.send("PING")
        scan = self.send("I2C_SCAN")
        sensor = self.send("READ_SENSOR")
        selftest = self.send("SELFTEST")
        connected = ping.get("status") == "OK" and "error" not in ping
        devices = list(scan.get("i2c_devices") or [])
        mock = bool(ping.get("mock") or scan.get("mock") or "mock_fallback" in ping)
        return ProbeReading(
            connected=connected,
            i2c_devices=devices,
            temperature_c=sensor.get("temperature_c"),
            humidity_pct=sensor.get("humidity_pct"),
            pressure_hpa=sensor.get("pressure_hpa"),
            oled_ok=selftest.get("oled_ok"),
            led_ok=selftest.get("led_ok"),
            mock=mock,
            raw=json.dumps({"ping": ping, "scan": scan, "sensor": sensor, "selftest": selftest}),
        )

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    # -- real serial ----------------------------------------------------
    def _real_send(self, command: str) -> dict:
        import serial  # pyserial; see requirements.txt

        if self._ser is None:
            self._ser = serial.Serial(self.port, self.baud, timeout=1.0)
            time.sleep(0.2)
            self._ser.reset_input_buffer()
        self._ser.write((command + "\n").encode("ascii"))
        self._ser.flush()
        lines: list[str] = []
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            raw = self._ser.readline()
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            lines.append(text)
            reply = self._parse_line(command, text)
            if reply is not None:
                reply["raw_lines"] = lines
                return reply
        return {"cmd": command, "error": "timeout waiting for reply", "raw_lines": lines}

    @staticmethod
    def _parse_line(command: str, text: str) -> dict | None:
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return None
            if isinstance(data, dict) and data.get("cmd") == command:
                return data
            return None
        # Legacy text format from the Day-1 firmware (I2C_SCAN:found=...).
        if command == "I2C_SCAN" and text.startswith("I2C_SCAN:"):
            found: list[str] = []
            for part in text.split("I2C_SCAN:", 1)[1].split(";"):
                if part.strip().startswith("found="):
                    found = [v.strip() for v in part.split("=", 1)[1].split(",") if v.strip()]
            return {"cmd": command, "i2c_devices": found, "legacy": True}
        if command == "PING" and text in ("OK", "READY", "PONG"):
            return {"cmd": command, "status": "OK", "legacy": True}
        return None

    # -- mock -----------------------------------------------------------
    def _mock_response(self, command: str) -> dict:
        fault = (self.simulate_fault or "").upper()
        devices = ["0x3C", "0x76"]
        if fault in ("SENSOR_DISCONNECTED",):
            devices = ["0x3C"]
        elif fault in ("LOOSE_GND", "SDA_SCL_SWAPPED"):
            devices = []
        temp = 24.6 if "0x76" in devices or "0x77" in devices else None
        led_ok = fault not in ("LED_REVERSED", "CODE_PIN_MISMATCH")
        oled_ok = "0x3C" in devices
        base = {"cmd": command, "mock": True}
        if command == "PING":
            if fault == "LOOSE_GND":
                return {**base, "error": "no reply (ground suspect)"}
            return {**base, "status": "OK", "uptime_ms": 424242}
        if command == "I2C_SCAN":
            return {**base, "i2c_devices": devices}
        if command == "READ_SENSOR":
            if temp is None:
                return {**base, "error": "BME280 not found",
                        "temperature_c": None, "humidity_pct": None, "pressure_hpa": None}
            return {**base, "temperature_c": temp, "humidity_pct": 48.2,
                    "pressure_hpa": 1013.2, "bme_address": "0x76"}
        if command == "SELFTEST":
            return {**base, "i2c_devices": devices, "oled_ok": oled_ok,
                    "bme_ok": temp is not None, "led_ok": led_ok,
                    "temperature_c": temp}
        return {**base, "error": "unreachable"}
