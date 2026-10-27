"""USB serial probe for the ESP32 patient board."""
import os
from datetime import datetime, timezone

import serial

SERIAL_PORT = os.environ.get("SERIAL_PORT", "/dev/ttyUSB0")
SERIAL_BAUD = int(os.environ.get("SERIAL_BAUD", "115200"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_scan_line(line: str) -> tuple[list[str], list[str]]:
    """Parse 'I2C_SCAN:found=0x3C,0x76;missing=0x50' into lists."""
    found: list[str] = []
    missing: list[str] = []
    payload = line.split("I2C_SCAN:", 1)[1].strip()
    for part in payload.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        items = [v.strip() for v in value.split(",") if v.strip()]
        if key.strip() == "found":
            found = items
        elif key.strip() == "missing":
            missing = items
    return found, missing


def run_i2c_scan(
    port: str = SERIAL_PORT,
    baud: int = SERIAL_BAUD,
    timeout: float = 5.0,
):
    """Send SCAN, wait for an I2C_SCAN: line, return a ProbeResult."""
    from .schemas import ProbeResult

    lines: list[str] = []
    try:
        with serial.Serial(port, baud, timeout=1.0) as ser:
            ser.reset_input_buffer()
            ser.write(b"SCAN\n")
            ser.flush()
            import time

            deadline = time.time() + timeout
            while time.time() < deadline:
                raw = ser.readline()
                if not raw:
                    continue
                text = raw.decode("utf-8", errors="replace").strip()
                lines.append(text)
                if text.startswith("I2C_SCAN:"):
                    found, missing = _parse_scan_line(text)
                    return ProbeResult(
                        found=found,
                        missing=missing,
                        raw="\n".join(lines),
                        timestamp=_now(),
                    )
        return ProbeResult(
            found=[],
            missing=[],
            raw="\n".join(lines) or "ERROR: no I2C_SCAN line received",
            timestamp=_now(),
        )
    except serial.SerialException as exc:
        return ProbeResult(
            found=[],
            missing=[],
            raw=f"ERROR: {exc}",
            timestamp=_now(),
        )


def read_last_serial(port: str = SERIAL_PORT, n: int = 20) -> str:
    """Read up to n recent lines from the patient board."""
    try:
        lines: list[str] = []
        with serial.Serial(port, SERIAL_BAUD, timeout=2.0) as ser:
            for _ in range(n):
                raw = ser.readline()
                if not raw:
                    break
                lines.append(raw.decode("utf-8", errors="replace").rstrip("\n"))
        return "\n".join(lines)
    except serial.SerialException as exc:
        return f"ERROR: {exc}"
