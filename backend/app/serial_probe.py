"""USB serial probe for the patient ESP32 board.

Speaks the Prompt-1 command set (PING, I2C_SCAN, READ_SENSOR, SELFTEST)
and parses the structured JSON lines from firmware/patient_board.

Mock mode (MOCK_META=true, the default) returns sample data so the loop
runs with no hardware, and raises when MOCK_META=false. Real mode uses
the serial port and reports failures honestly as error replies; it never
substitutes fabricated readings.
"""
from __future__ import annotations

import json
import os
import time

from .adapters import mock_meta_enabled
from .models import ProbeReading

COMMANDS = ("PING", "I2C_SCAN", "READ_SENSOR", "SELFTEST")

_DEFAULT_PORT = "COM3" if os.name == "nt" else "/dev/ttyUSB0"

# Baud rates the probe adapts across: patient firmware speaks 115200, but
# stock Arduino sketches (and vendor bootloaders) typically use 9600.
KNOWN_BAUDS = (115200, 9600)

TIMEOUT_ERROR = "timeout waiting for reply"


def _env_timeout() -> float:
    """Per-command serial timeout, tunable via SERIAL_TIMEOUT (default 4.0s)."""
    try:
        return max(0.2, float(os.environ.get("SERIAL_TIMEOUT", "4.0")))
    except ValueError:
        return 4.0


def list_serial_ports() -> list[dict]:
    """Enumerate candidate serial ports (empty when pyserial is missing)."""
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    try:
        found = list_ports.comports()
    except Exception:
        return []
    return [{"port": p.device,
             "description": (p.description or "")[:80],
             "hwid": (p.hwid or "")[:80]} for p in found]


def _mock_enabled() -> bool:
    return mock_meta_enabled()


def _is_usb_port(info: dict) -> bool:
    """True when a listed port smells like a USB serial adapter.

    USB enumeration carries "USB" in the hwid ("USB VID:PID=...") or
    the description ("... USB to UART Bridge"); Bluetooth links
    (BTHENUM) and legacy ports do not.
    """
    text = "%s %s" % (info.get("description", ""), info.get("hwid", ""))
    return "USB" in text.upper()


class SerialProbe:
    def __init__(
        self,
        port: str | None = None,
        baud: int | None = None,
        mock: bool | None = None,
        timeout: float | None = None,
    ) -> None:
        self.port = port or os.environ.get("SERIAL_PORT") or _DEFAULT_PORT
        self.baud = baud or int(os.environ.get("SERIAL_BAUD", "115200"))
        self.mock = _mock_enabled() if mock is None else mock
        self.timeout = timeout if timeout is not None else _env_timeout()
        self.simulate_fault: str | None = None
        self.last_error: str = ""
        self.last_scan_errors: dict[str, str] = {}
        self._ser = None  # persistent handle, opened lazily

    def configure(self, port: str | None = None, baud=None) -> dict:
        """Point the probe at a new port/baud at runtime (reconnects lazily)."""
        self.close()
        if port:
            self.port = str(port)
        if baud:
            try:
                self.baud = int(baud)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
        self.last_error = ""
        return self.serial_status()

    def serial_status(self) -> dict:
        return {"port": self.port, "baud": self.baud, "mock": self.mock,
                "last_error": self.last_error}

    def autodetect(self, timeout: float = 0.8,
                   exclude: tuple[str, ...] = ()) -> dict | None:
        """Find a port+baud answering the patient PING (explicit user action).

        Probes each listed port at the current baud first, then each known
        baud, with a short timeout, and adopts the first live one. Returns
        {"port", "baud"} or None (original settings restored). Ports in
        `exclude` are skipped (already known dead by the caller). Each
        scanned port's final failure is kept in `last_scan_errors`
        (busy-vs-silent stays diagnosable after the scan). A port that
        refuses to open is tried once: other bauds cannot fix that.
        """
        if self.mock:
            return None
        original_port, original_baud, original_timeout = (
            self.port, self.baud, self.timeout)
        self.timeout = min(timeout, self.timeout)
        self.last_scan_errors = {}
        try:
            for info in list_serial_ports():
                if info["port"] in exclude:
                    continue
                for baud in dict.fromkeys([original_baud, *KNOWN_BAUDS]):
                    self.close()
                    self.port, self.baud = info["port"], baud
                    reply = self.send("PING")
                    if "error" not in reply:
                        return {"port": self.port, "baud": self.baud}
                    reason = str(reply["error"])
                    if ": " in reason:  # strip the "PORT: " prefix we add
                        reason = reason.split(": ", 1)[1]
                    self.last_scan_errors[info["port"]] = reason
                    if TIMEOUT_ERROR not in reason:
                        # The port would not open at all: no baud will fix
                        # that, so skip the remaining bauds for this port
                        # instead of burning another open + timeout each.
                        break
        finally:
            self.timeout = original_timeout
        self.close()
        self.port, self.baud = original_port, original_baud
        return None

    # -- public API ----------------------------------------------------
    def send(self, command: str) -> dict:
        """Send one command, return the parsed JSON reply (never raises)."""
        command = command.strip().upper()
        if command not in COMMANDS:
            return {"cmd": command, "error": "unknown command %r" % command}
        if self.mock:
            if not mock_meta_enabled():
                raise RuntimeError("mock serial probe called with MOCK_META=false")
            return self._mock_response(command)
        try:
            reply = self._real_send(command)
        except Exception as exc:
            reply = {"cmd": command, "error": "%s: %s" % (self.port, exc)}
        if "error" in reply:
            self.last_error = "%s: %s" % (command, reply["error"])
        elif command == "PING":
            self.last_error = ""
        return reply

    def read_all(self, simulate_fault: str | None = None) -> ProbeReading:
        """Run the full sense pass and aggregate one ProbeReading.

        Real mode adapts to the equipment: when PING goes unanswered the
        probe retries it once per alternate known baud and adopts the
        first baud that answers (patient firmware at 115200, stock
        Arduino sketches at 9600). When the configured port stays dead
        it fails over to the first other listed port answering PING
        (USB boards re-enumerate across COM ports freely). When nothing
        answers anywhere, the remaining commands short-circuit with
        instant honest errors instead of burning a full timeout each.
        """
        self.simulate_fault = simulate_fault
        ping = self.send("PING")
        if not self.mock and "error" in ping:
            ping = self._adopt_live_baud(ping)
        if not self.mock and "error" in ping:
            ping = self._adopt_live_port(ping)
        silent = not self.mock and "error" in ping
        replies = [ping]
        for command in ("I2C_SCAN", "READ_SENSOR", "SELFTEST"):
            if silent:
                replies.append({
                    "cmd": command,
                    "baud": self.baud,
                    "error": "skipped: PING failed (%s)" % ping.get("error"),
                    "short_circuited": True,
                })
            else:
                replies.append(self.send(command))
        scan, sensor, selftest = replies[1], replies[2], replies[3]
        connected = ping.get("status") == "OK" and "error" not in ping
        devices = list(scan.get("i2c_devices") or [])
        mock = bool(ping.get("mock") or scan.get("mock"))
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

    def _adopt_live_baud(self, failed_ping: dict) -> dict:
        """Retry PING at each alternate known baud; adopt the first live one.

        Returns the working PING reply, or the original failure when no
        baud answers. Retries use a short timeout so a dead board costs
        one extra second per baud, not another full timeout each. When
        the port would not even open (no timeout marker), baud hunting
        is skipped: reopening at 9600 fails exactly like 115200 did.
        """
        if TIMEOUT_ERROR not in str(failed_ping.get("error", "")):
            return failed_ping
        original_baud = self.baud
        original_timeout = self.timeout
        self.timeout = min(1.0, self.timeout)
        try:
            for baud in KNOWN_BAUDS:
                if baud == original_baud:
                    continue
                self.close()
                self.baud = baud
                reply = self.send("PING")
                if "error" not in reply:
                    return reply
        finally:
            self.timeout = original_timeout
        self.close()
        self.baud = original_baud
        return failed_ping

    def _adopt_live_port(self, failed_ping: dict) -> dict:
        """Fail over to the first other listed port answering PING.

        Returns a fresh PING reply on the adopted port, or the original
        failure annotated with which ports were tried when nothing
        answers anywhere. The failure also carries a "board_state"
        classification (no_ports, no_usb, silent:<ports>, or
        unreachable:<ports>) so callers can guide the technician
        without another model round trip.
        """
        found = self.autodetect(timeout=0.5, exclude=(self.port,))
        if found is not None:
            return self.send("PING")
        listed = list_serial_ports()
        if not listed:
            scan = "no serial ports found"
            tried = [self.port]
        else:
            tried = [self.port, *(p["port"] for p in listed
                                   if p["port"] != self.port)]
            scan = "no PING reply on %s" % ", ".join(tried)
            reasons = "; ".join(
                "%s: %s" % (port, self.last_scan_errors[port][:64])
                for port in tried if port in self.last_scan_errors)
            if reasons:
                scan += " [%s]" % reasons
        failed_ping = dict(failed_ping)
        orig = str(failed_ping.get("error", ""))
        failed_ping["board_state"] = self._board_state(listed, tried, orig)
        failed_ping["error"] = "%s (%s)" % (scan, orig)
        self.last_error = "PING: %s" % failed_ping["error"]
        return failed_ping

    def _board_state(self, listed: list, tried: list, error: str) -> str:
        """Classify a total PING failure for technician guidance.

        A port that opens but never answers (our own timeout marker)
        is "silent" (board needs the patient sketch); a USB-listed
        port that will not open is "unreachable" (busy or blocked).
        """
        silent = [self.port] if TIMEOUT_ERROR in error else []
        silent += [p for p in tried[1:]
                   if self.last_scan_errors.get(p) == TIMEOUT_ERROR]
        usb_ports = [p["port"] for p in listed if _is_usb_port(p)]
        if not listed:
            return "no_ports"
        if silent:
            return "silent:" + ",".join(silent)
        if usb_ports:
            return "unreachable:" + ",".join(usb_ports)
        return "no_usb"

    # -- real serial ----------------------------------------------------
    def _real_send(self, command: str) -> dict:
        import serial  # pyserial; see requirements.txt

        if self._ser is None:
            self._ser = serial.Serial(self.port, self.baud, timeout=1.0,
                                      write_timeout=self.timeout)
            time.sleep(0.2)
            self._ser.reset_input_buffer()
        try:
            self._ser.write((command + "\n").encode("ascii"))
            self._ser.flush()
        except Exception:
            self.close()  # wedged handle: reopen fresh on the next command
            raise
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
                reply["baud"] = self.baud
                return reply
        return {"cmd": command, "error": TIMEOUT_ERROR, "raw_lines": lines,
                "baud": self.baud}

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
