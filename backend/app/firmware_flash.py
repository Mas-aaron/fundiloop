"""One-click ESP32 flashing for Learning mode (explicit user action only).

Pipeline: compile/cache the step sketch (firmware_build) -> release
both probe handles -> esptool write_flash at the build offsets ->
re-probe PING. Every flash is audit-logged (success and failure) into
the coach hash-chained audit trail. Busy-port failures carry
close-the-Serial-Monitor guidance instead of a raw traceback.

Envs: ESPTOOL_BIN (default: `sys.executable -m esptool`), ESPTOOL_BAUD
(default 921600), ESPTOOL_TIMEOUT (seconds, default 180),
FLASH_SETTLE_SECONDS (pause before the PING re-probe, default 2.0).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

from .coach import audit_rail

DEFAULT_BAUD = 921600
DEFAULT_TIMEOUT = 180
DEFAULT_SETTLE = 2.0

_BUSY_RE = re.compile(
    r"could not open port|cannot open|permission denied|busy|"
    r"access is denied|in use|being used|resource busy|locked|"
    r"serialexception",
    re.IGNORECASE)

_MISSING_PORT_RE = re.compile(
    r"filenotfounderror|cannot find the file|does not exist|"
    r"no such file|not found",
    re.IGNORECASE)


def esptool_cmd() -> list[str]:
    """esptool invocation from env (module run by default)."""
    override = os.environ.get("ESPTOOL_BIN", "").strip()
    if override:
        return [override]
    return [sys.executable, "-m", "esptool"]


def esptool_baud() -> int:
    """Flash baud from env (default 921600)."""
    try:
        return max(9600, int(os.environ.get("ESPTOOL_BAUD",
                                            str(DEFAULT_BAUD))))
    except ValueError:
        return DEFAULT_BAUD


def esptool_timeout() -> float:
    """Flash timeout in seconds from env (default 180)."""
    try:
        return max(5.0, float(os.environ.get("ESPTOOL_TIMEOUT",
                                             str(DEFAULT_TIMEOUT))))
    except ValueError:
        return float(DEFAULT_TIMEOUT)


def flash_settle_seconds() -> float:
    """Pause before the PING re-probe from env (default 2.0)."""
    try:
        return max(0.0, float(os.environ.get("FLASH_SETTLE_SECONDS",
                                             str(DEFAULT_SETTLE))))
    except ValueError:
        return DEFAULT_SETTLE


def _excerpt(text: str, lines: int = 20, width: int = 2000) -> str:
    """Failure context: root error lines first, then the tail."""
    all_lines = (text or "").splitlines()
    errs = [line for line in all_lines if "error" in line.lower()][:8]
    out = errs + ["…"] + all_lines[-lines:] if errs else all_lines[-lines:]
    joined = "\n".join(out)
    return joined[-width:] if len(joined) > width else joined


def is_busy_port(log: str) -> bool:
    """True when a flash log smells like a port another program holds."""
    return bool(_BUSY_RE.search(log or ""))


def busy_port_guidance(port: str) -> str:
    """Close-the-Serial-Monitor guidance for a busy port."""
    return ("Port %s is busy or blocked: close Arduino IDE Serial Monitor "
            "and Plotter plus any other program using it, unplug and replug "
            "the USB cable, then press FLASH ESP32 again."
            % (port or "?"))


def available_ports() -> list[str]:
    """Serial port names on this machine ([] when unknown). Never raises."""
    try:
        from .serial_probe import list_serial_ports
        return [str(p.get("port")) for p in list_serial_ports()
                if p.get("port")]
    except Exception:
        return []


def check_port(port: str) -> dict:
    """Pre-flight: can this process open `port` right now?

    Returns {"ok": True} when the port opens (closing the handle
    immediately), or when pyserial is missing — esptool remains the
    judge then. Otherwise {"ok": False, "busy": bool, "detail":
    reason}: busy=True means another program holds the port (close
    Serial Monitor), busy=False means the port does not exist (plug
    the board in, pick the right port). Never raises. Note: opening
    the port resets the board (DTR toggle), which is harmless here —
    flashing reboots it anyway.
    """
    try:
        import serial
    except ImportError:
        return {"ok": True, "detail": ""}
    handle = None
    try:
        handle = serial.Serial(port, esptool_baud(), timeout=0.5)
        return {"ok": True, "detail": ""}
    except Exception as exc:
        msg = str(exc) or "cannot open port"
        return {"ok": False, "busy": not bool(_MISSING_PORT_RE.search(msg)),
                "detail": msg}
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def flash_artifacts(artifacts: list, port: str, runner=None) -> dict:
    """Write build artifacts to the board at their offsets.

    Returns {"ok", "log_excerpt"}. Missing esptool, timeouts, and
    esptool errors return ok False with an excerpt — never raise.
    `runner` (subprocess.run-compatible) is injectable for hermetic
    tests; production always shells the real esptool.
    """
    run = runner or subprocess.run
    if not artifacts:
        return {"ok": False, "log_excerpt": "nothing to flash: no binaries"}
    cmd = [*esptool_cmd(), "--port", port, "--baud", str(esptool_baud()),
           "write_flash", "-z"]
    for artifact in artifacts:
        cmd += [str(artifact.get("offset")), str(artifact.get("file"))]
    try:
        proc = run(cmd, capture_output=True, text=True,
                   timeout=esptool_timeout())
    except FileNotFoundError:
        return {"ok": False,
                "log_excerpt": "esptool not found: pip install esptool "
                "(see README) or set ESPTOOL_BIN"}
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "log_excerpt": "esptool timed out after %ss" % esptool_timeout()}
    log = (getattr(proc, "stdout", "") or "") + "\n" + \
        (getattr(proc, "stderr", "") or "")
    if getattr(proc, "returncode", 1) != 0:
        return {"ok": False,
                "log_excerpt": _excerpt(log) or "esptool failed"}
    return {"ok": True, "log_excerpt": _excerpt(log)}


def settle_before_ping(probe) -> None:
    """Let a freshly flashed board reboot (skipped for mock probes)."""
    if getattr(probe, "mock", False):
        return
    time.sleep(flash_settle_seconds())


def verify_ping(probe) -> dict:
    """Re-probe PING after a flash. Never raises.

    A step sketch is not patient firmware, so silence here is
    informational (the flash still stands) — the caller reports it,
    not fails on it.
    """
    try:
        reply = probe.send("PING")
    except Exception as exc:  # noqa: BLE001 - verification must not raise
        return {"answered": False, "reply": "ping failed: %s" % exc}
    if not isinstance(reply, dict):
        return {"answered": False, "reply": "non-dict PING reply"}
    answered = "error" not in reply
    return {"answered": answered,
            "reply": str(reply.get("status") or reply.get("error") or reply)[:200]}


def audit_flash(session_id: str, step_n: int, port: str, build_hash: str,
                ok: bool, reason: str, detail: str = "") -> None:
    """Append one flash event to the hash-chained audit trail."""
    audit_rail("flash", session_id=session_id, tool="flash",
               args={"step": step_n, "port": port,
                     "build_hash": build_hash},
               ok=bool(ok), reason=reason, detail=(detail or "")[:500])
