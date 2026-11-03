"""Live serial telemetry: stream device lines, diagnose stalls.

The monitor takes the serial port over exclusively (the probes hold it
persistently once adopted, and reopen lazily afterwards). The stream
yields raw device lines; anomaly detection runs in the browser against
the live text; diagnosis names a suspect wire from the netlist, or
honestly none. Monitoring and probing are mutually exclusive by UI
state: entering the monitor pauses auto-checks. Proof is the build's
only verifier: trailing healthy-shape lines, never a tap.
"""
from __future__ import annotations

import json
import re

DEFAULT_BAUD = 115200
MAX_DIAGNOSE_LINES = 30
PROOF_LINES = 5

_PROOF_BOOT = re.compile(r"rst:|boot:|load:|entry |ets [A-Z]",
                           re.IGNORECASE)
_PROOF_FAULT_WORD = re.compile(
    r"\b(error|timeout|time-out|fail|failed|nan|no echo|no response|"
    r"not found|disconnect|offline|disconnected)\b", re.IGNORECASE)
_PROOF_SENTINEL = re.compile(r"(^|:\s*)-1(\D|$)")
_PROOF_DISTANCE = re.compile(
    r"distance\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*"
    r"(cm|mm|meters?|inches?|in|m)?", re.IGNORECASE)
_PROOF_UNIT = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(cm|mm|meters?|inches?|in|m)\b", re.IGNORECASE)
_TAGGED_DEVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _\-+]*\Z")
_TAGGED_METRIC = re.compile(r"[A-Za-z0-9 _\-+%]+\Z")
_TAGGED_NUMBER = re.compile(r"-?\d+(?:\.\d+)?\Z")


def _parse_tagged(text: object) -> dict | None:
    """Split a DEVICE | METRIC | VALUE | UNIT line, or None.

    Mirrors the browser's parseTaggedTelemetry exactly: strict device
    and metric tokens keep prose containing "|" on the legacy path.
    """
    parts = [p.strip() for p in str(text or "").split("|")]
    if len(parts) < 3:
        return None
    device, metric = parts[0], parts[1]
    if not _TAGGED_DEVICE.match(device) or not _TAGGED_METRIC.match(metric):
        return None
    return {"device": device, "metric": metric, "value": parts[2],
            "unit": parts[3] if len(parts) > 3 else ""}
_PROOF_HEARTBEAT = re.compile(r"status|heartbeat|\bhb\b|^ok\b|alive",
                              re.IGNORECASE)

_DIAGNOSE_FALLBACK = (
    "I lost the readings from the board. Push every wire firmly home "
    "— power and ground first, then the signal wires — and watch the "
    "monitor pick back up.")


class TelemetryError(Exception):
    """The telemetry stream failed (port busy, unplugged, read error)."""


def default_opener(port: str, baud: int):
    """Open a serial handle for streaming (pyserial, 1s read timeout)."""
    import serial

    return serial.Serial(port, baud, timeout=1.0)


def iter_telemetry_lines(port: str, baud: int = DEFAULT_BAUD,
                         opener=None, max_lines: int | None = None):
    """Yield decoded device lines until the stream ends. Never yields
    blanks; read timeouts tick silently so the loop stays responsive
    to close. Raises TelemetryError when the port cannot open or a
    read fails; the handle always closes."""
    open_serial = opener or default_opener
    try:
        ser = open_serial(port, baud)
    except Exception as exc:
        raise TelemetryError("could not open %s: %s" % (port, exc))
    yielded = 0
    try:
        while max_lines is None or yielded < max_lines:
            try:
                raw = ser.readline()
            except Exception as exc:
                raise TelemetryError(
                    "serial read failed on %s: %s" % (port, exc))
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            yielded += 1
            yield text
    finally:
        try:
            ser.close()
        except Exception:
            pass


def _proof_class(line: object) -> str | None:
    """One line's proof verdict: "fault", "healthy", or None (chatter).

    Mirrors the browser classifier exactly (same shapes, same order):
    boot chatter never counts, fault words/sentinels/negatives veto,
    measurements and heartbeat tokens count. Tagged lines triage on
    their metric + value; any numeric tagged value counts.
    """
    text = str(line or "")
    if not text.strip() or _PROOF_BOOT.search(text):
        return None
    tagged = _parse_tagged(text)
    if tagged is not None:
        num = None
        if _TAGGED_NUMBER.match(tagged["value"]):
            num = float(tagged["value"])
        if (_PROOF_FAULT_WORD.search(
                tagged["metric"] + " " + tagged["value"])
                or (num is not None and num < 0)):
            return "fault"
        if num is not None:
            return "healthy"
        if _PROOF_HEARTBEAT.search(text):
            return "healthy"
        return None
    value = None
    m = _PROOF_DISTANCE.search(text)
    if m:
        value = float(m.group(1))
    else:
        m = _PROOF_UNIT.search(text)
        if m:
            value = float(m.group(1))
    if (_PROOF_FAULT_WORD.search(text) or _PROOF_SENTINEL.search(text)
            or (value is not None and value < 0)):
        return "fault"
    if value is not None:
        return "healthy"
    if _PROOF_HEARTBEAT.search(text):
        return "healthy"
    return None


def telemetry_proof(lines: list) -> dict:
    """Trailing-streak proof verdict for submitted monitor lines.

    Chatter holds the streak, faults reset it; verified only when the
    trailing run reaches PROOF_LINES. Deterministic: the DONE gate.
    """
    streak, faults = 0, 0
    for line in lines or []:
        verdict = _proof_class(line)
        if verdict == "fault":
            streak, faults = 0, faults + 1
        elif verdict == "healthy":
            streak += 1
    return {"verified": streak >= PROOF_LINES,
            "healthy": streak, "faults": faults}


def iter_telemetry_events(port: str, baud: int = DEFAULT_BAUD,
                          opener=None):
    """(event, payload) tuples for the SSE endpoint. Never raises: a
    failed stream ends with one error frame."""
    yield ("ready", {"port": port, "baud": baud})
    try:
        for line in iter_telemetry_lines(port, baud, opener=opener):
            yield ("line", {"line": line})
    except TelemetryError as exc:
        yield ("error", {"detail": str(exc)})


def _match_suspect(suspect: object, wires: list) -> dict | None:
    """Suspect wire with its board pin, or None when the model names
    anything the netlist does not contain."""
    if not isinstance(suspect, dict):
        return None
    part = str(suspect.get("part") or "").strip().lower()
    pin = str(suspect.get("part_pin") or "").strip().upper()
    if not part or not pin:
        return None
    for wire in wires or []:
        if not isinstance(wire, dict):
            continue
        if str(wire.get("part") or "").strip().lower() == part \
                and str(wire.get("part_pin") or "").strip().upper() == pin:
            return {"part": wire.get("part"),
                    "part_pin": wire.get("part_pin"),
                    "board_pin": wire.get("board_pin")}
    return None


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        import re

        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    return text


def _part_wires(wires: list, part: str | None) -> list:
    """Every netlist wire on one part, for highlighting the whole
    device — not just the suspect pin."""
    if not part:
        return []
    return [{"part": w.get("part"), "part_pin": w.get("part_pin"),
             "board_pin": w.get("board_pin")}
            for w in wires or []
            if isinstance(w, dict) and w.get("part") == part]


def diagnose_telemetry(lines: list, netlist: dict, project_title: str,
                       brain, board: str | None = None,
                       device: str | None = None,
                       healthy: list | None = None) -> dict:
    """One model diagnosis for a stalled telemetry stream.

    Returns {"message", "suspect", "part_wires"}: the message is
    always spoken-safe guidance, the suspect is a netlist wire (part,
    part_pin, board_pin) or None, and part_wires carries every wire
    on the suspect's part for highlighting the whole device.
    Model-written when possible, grounded fallback otherwise.
    Never raises.
    """
    from .prompts import (telemetry_diagnose_system,
                          telemetry_diagnose_user)

    wires = [w for w in (netlist or {}).get("wires", []) or []
             if isinstance(w, dict)]
    if getattr(brain, "mock", False):
        return {"message": _DIAGNOSE_FALLBACK, "suspect": None,
                "part_wires": []}
    system = telemetry_diagnose_system(board)
    base_user = telemetry_diagnose_user(lines, wires, project_title,
                                        device, healthy)
    user_msg = base_user
    for _ in (1, 2):
        try:
            data = json.loads(_strip_fences(
                brain.complete_text(system, user_msg)))
            if not isinstance(data, dict):
                raise ValueError("diagnosis is not an object")
            message = str(data.get("message") or "").strip()
            if not message:
                raise ValueError("diagnosis has no message")
            suspect = _match_suspect(data.get("suspect"), wires)
            return {"message": message, "suspect": suspect,
                    "part_wires": _part_wires(
                        wires, (suspect or {}).get("part"))}
        except Exception as exc:
            user_msg = (base_user + "\nYour last reply was rejected:\n- %s\n"
                        "Reply with ONLY the corrected JSON object." % exc)
    return {"message": _DIAGNOSE_FALLBACK, "suspect": None,
            "part_wires": []}
