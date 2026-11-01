"""Offline ESP32 firmware builds via arduino-cli (Learning mode).

Each step sketch compiles to an ESP32 binary through the `arduino-cli`
toolchain installed once on the laptop (ESP32 core, offline after
that). Builds cache by code hash, so re-flashing an unchanged step
never recompiles. Only ESP32 is supported; anything else raises
ValueError instead of producing a binary for the wrong chip.

Envs: ARDUINO_CLI_BIN (default "arduino-cli"), ARDUINO_FQBN (default
"esp32:esp32:esp32"), ARDUINO_COMPILE_TIMEOUT (seconds, default 180),
FIRMWARE_BUILD_DIR (default backend/data/firmware_builds),
ARDUINO_AUTO_LIBS ("0" disables library auto-install).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_FQBN = "esp32:esp32:esp32"
DEFAULT_TIMEOUT = 180
SKETCH_NAME = "step_sketch"

# Standard ESP32 flash offsets for arduino-cli build outputs.
OFFSET_BOOTLOADER = "0x1000"
OFFSET_PARTITIONS = "0x8000"
OFFSET_FIRMWARE = "0x10000"

# Sketch headers -> arduino-cli Library Manager names. Our templates
# (learning.py) and model-generated sketches emit Adafruit/DHT/JSON
# includes that a bare toolchain lacks; the build installs whatever is
# mapped and missing before compiling. Bundled core headers (Arduino.h,
# Wire.h, SPI.h, WiFi.h, ...) need nothing. Unknown third-party headers
# stay unmapped on purpose: guessing install names risks the registry's
# interactive "multiple libraries found" prompt, which would hang a
# non-tty build; the compile error names the missing header instead.
HEADER_LIB_MAP = {
    "Adafruit_SSD1306.h": "Adafruit SSD1306",
    "Adafruit_GFX.h": "Adafruit GFX Library",
    "Adafruit_BME280.h": "Adafruit BME280 Library",
    "Adafruit_Sensor.h": "Adafruit Unified Sensor",
    "DHT.h": "DHT sensor library",
    "ArduinoJson.h": "ArduinoJson",
    "PubSubClient.h": "PubSubClient",
    "NTPClient.h": "NTPClient",
}

_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def arduino_cli_bin() -> str:
    """arduino-cli executable path from env (never hardcoded)."""
    return os.environ.get("ARDUINO_CLI_BIN", "").strip() or "arduino-cli"


def arduino_fqbn() -> str:
    """Target board FQBN from env (ESP32 default)."""
    return os.environ.get("ARDUINO_FQBN", "").strip() or DEFAULT_FQBN


def compile_timeout() -> float:
    """Compile timeout in seconds from env (default 180)."""
    try:
        return max(5.0, float(os.environ.get("ARDUINO_COMPILE_TIMEOUT",
                                             str(DEFAULT_TIMEOUT))))
    except ValueError:
        return float(DEFAULT_TIMEOUT)


def build_dir_for(code_hash: str = "") -> str:
    """Build cache root (or one hashed build's dir) from env."""
    override = os.environ.get("FIRMWARE_BUILD_DIR", "").strip()
    root = override or str(Path(__file__).resolve().parent.parent
                           / "data" / "firmware_builds")
    return os.path.join(root, code_hash) if code_hash else root


def code_hash(code: str) -> str:
    """Cache key: first 16 hex chars of the sketch's sha256."""
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()[:16]


def _excerpt(text: str, lines: int = 20, width: int = 2000) -> str:
    """Failure context: root error lines first, then the tail.

    Toolchains print the cause early ("Error loading hardware
    platform…") and the consequence late ("instance no longer
    valid"), so a tail-only excerpt hides the diagnosis. ANSI color
    codes are stripped: the UI is plain text, not a terminal.
    """
    all_lines = _ANSI_RE.sub("", text or "").splitlines()
    errs = [line for line in all_lines if "error" in line.lower()][:8]
    out = errs + ["…"] + all_lines[-lines:] if errs else all_lines[-lines:]
    joined = "\n".join(out)
    return joined[-width:] if len(joined) > width else joined


def _find_artifacts(outdir: str) -> list[dict]:
    """Map arduino-cli outputs to esptool (offset, file) pairs.

    Bootloader and partition table ride at their standard offsets when
    present; the main sketch binary always flashes at 0x10000. Sorted
    by offset so the flash order is deterministic.
    """
    try:
        names = sorted(os.listdir(outdir))
    except OSError:
        return []
    boot = next((n for n in names if n.endswith(".bootloader.bin")), None)
    parts = next((n for n in names if n.endswith(".partitions.bin")), None)
    mains = [n for n in names
             if n.endswith(".bin") and n != boot and n != parts
             and not n.endswith(".elf.bin")]
    main = next((n for n in mains if SKETCH_NAME in n), None) or \
        next(iter(mains), None)
    artifacts = []
    if boot:
        artifacts.append({"offset": OFFSET_BOOTLOADER,
                          "file": os.path.join(outdir, boot)})
    if parts:
        artifacts.append({"offset": OFFSET_PARTITIONS,
                          "file": os.path.join(outdir, parts)})
    if main:
        artifacts.append({"offset": OFFSET_FIRMWARE,
                          "file": os.path.join(outdir, main)})
    return artifacts


def _cached_build(outdir: str, want_hash: str) -> dict | None:
    """Return the cached build record when it is complete, else None."""
    record_path = os.path.join(outdir, "build.json")
    try:
        with open(record_path, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("build_hash") != want_hash:
        return None
    artifacts = record.get("artifacts") or []
    if not artifacts or not all(
            isinstance(a, dict) and os.path.isfile(a.get("file", ""))
            for a in artifacts):
        return None
    record["cached"] = True
    record["ok"] = True
    return record


def needed_libraries(code: str) -> list[str]:
    """arduino-cli library names for a sketch's mapped includes.

    Include order, deduplicated; bundled and unknown headers yield
    nothing.
    """
    found = []
    for header in _INCLUDE_RE.findall(code or ""):
        name = HEADER_LIB_MAP.get(header.strip())
        if name and name not in found:
            found.append(name)
    return found


def auto_libs_enabled() -> bool:
    """Library auto-install switch (ARDUINO_AUTO_LIBS, default on)."""
    return os.environ.get("ARDUINO_AUTO_LIBS", "1").strip() != "0"


def _installed_library_names(run) -> set[str]:
    """Installed library names (lowercased) via `lib list --format json`.

    Any failure (missing CLI, bad JSON, odd shape) yields the empty set:
    over-installing is harmless (the CLI short-circuits what is already
    there) while under-installing breaks the compile.
    """
    proc = run([arduino_cli_bin(), "lib", "list", "--format", "json"],
               capture_output=True, text=True, timeout=compile_timeout())
    if getattr(proc, "returncode", 1) != 0:
        return set()
    try:
        payload = json.loads(getattr(proc, "stdout", "") or "{}")
    except ValueError:
        return set()
    installed = payload.get("installed_libraries") \
        if isinstance(payload, dict) else None
    if not isinstance(installed, list):
        return set()
    names = set()
    for entry in installed:
        lib = entry.get("library") if isinstance(entry, dict) else None
        name = lib.get("name") if isinstance(lib, dict) else None
        if isinstance(name, str) and name.strip():
            names.add(name.strip().lower())
    return names


def ensure_libraries(code: str, run) -> dict:
    """Install the mapped libraries a sketch needs (best-effort).

    Returns {"ok", "installed", "log"} and never raises except
    FileNotFoundError (missing CLI), which the caller converts to the
    usual friendly message. Every other failure just fills "log": the
    compile still runs, since the library may already be present, and
    its own error names any header still missing.
    """
    needed = needed_libraries(code)
    if not needed or not auto_libs_enabled():
        return {"ok": True, "installed": [], "log": ""}
    installed = _installed_library_names(run)
    missing = [n for n in needed if n.lower() not in installed]
    if not missing:
        return {"ok": True, "installed": [], "log": ""}
    try:
        proc = run([arduino_cli_bin(), "lib", "install"] + missing,
                   capture_output=True, text=True,
                   timeout=compile_timeout())
    except FileNotFoundError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "installed": [], "log": "lib install failed: %s"
                % exc}
    if getattr(proc, "returncode", 1) != 0:
        out = (getattr(proc, "stdout", "") or "") + "\n" + \
            (getattr(proc, "stderr", "") or "")
        return {"ok": False, "installed": [],
                "log": _excerpt(out, lines=6)}
    return {"ok": True, "installed": missing, "log": ""}


def compile_sketch(code: str, board: str = "esp32_devkit",
                   runner=None) -> dict:
    """Compile one sketch to ESP32 binaries (cached by code hash).

    Returns {"ok", "build_hash", "build_dir", "artifacts", "fqbn",
    "log_excerpt", "cached", "libraries"}. Mapped libraries are
    installed before compiling (best-effort; the compile still runs
    when that step fails). Failures (missing CLI, compile errors,
    timeouts) return ok False with a log excerpt — never raise. Only
    `runner` (subprocess.run-compatible) is injectable, for hermetic
    tests; production always shells the real arduino-cli.
    """
    if (board or "").strip().lower() != "esp32_devkit":
        raise ValueError("firmware builds support ESP32 only (board: %s)"
                         % (board or "?"))
    if not (code or "").strip():
        return {"ok": False, "build_hash": "", "build_dir": "",
                "artifacts": [], "fqbn": arduino_fqbn(),
                "log_excerpt": "empty sketch: nothing to compile",
                "cached": False}
    digest = code_hash(code)
    outdir = build_dir_for(digest)
    hit = _cached_build(outdir, digest)
    if hit is not None:
        return hit
    run = runner or subprocess.run
    # Arduino requires <dir>/<dir>.ino: folder and main file share a name.
    sketch_dir = os.path.join(outdir, SKETCH_NAME)
    try:
        os.makedirs(sketch_dir, exist_ok=True)
        with open(os.path.join(sketch_dir, SKETCH_NAME + ".ino"),
                  "w", encoding="utf-8") as fh:
            fh.write(code)
    except OSError as exc:
        return {"ok": False, "build_hash": digest, "build_dir": outdir,
                "artifacts": [], "fqbn": arduino_fqbn(),
                "log_excerpt": "cannot stage sketch: %s" % exc,
                "cached": False}
    try:
        libs = ensure_libraries(code, run)
    except FileNotFoundError:
        # Missing CLI: the compile attempt below reports it the usual way.
        libs = {"ok": True, "installed": [], "log": ""}
    cmd = [arduino_cli_bin(), "compile", "--fqbn", arduino_fqbn(),
           "--output-dir", outdir, sketch_dir]
    try:
        proc = run(cmd, capture_output=True, text=True,
                   timeout=compile_timeout())
    except FileNotFoundError:
        return {"ok": False, "build_hash": digest, "build_dir": outdir,
                "artifacts": [], "fqbn": arduino_fqbn(),
                "log_excerpt": "arduino-cli not found at %r: install it "
                "once (see README) or set ARDUINO_CLI_BIN" % cmd[0],
                "cached": False}
    except subprocess.TimeoutExpired:
        return {"ok": False, "build_hash": digest, "build_dir": outdir,
                "artifacts": [], "fqbn": arduino_fqbn(),
                "log_excerpt": "arduino-cli compile timed out after %ss"
                % compile_timeout(), "cached": False}
    log = (getattr(proc, "stdout", "") or "") + "\n" + \
        (getattr(proc, "stderr", "") or "")
    if getattr(proc, "returncode", 1) != 0:
        excerpt = _excerpt(log) or "arduino-cli failed"
        if libs["log"]:
            excerpt = libs["log"] + "\n" + excerpt
        return {"ok": False, "build_hash": digest, "build_dir": outdir,
                "artifacts": [], "fqbn": arduino_fqbn(),
                "log_excerpt": excerpt, "cached": False}
    artifacts = _find_artifacts(outdir)
    if not artifacts:
        return {"ok": False, "build_hash": digest, "build_dir": outdir,
                "artifacts": [], "fqbn": arduino_fqbn(),
                "log_excerpt": (_excerpt(log) + "\nno .bin outputs found"
                                ).strip(), "cached": False}
    record = {"ok": True, "build_hash": digest, "build_dir": outdir,
              "artifacts": artifacts, "fqbn": arduino_fqbn(),
              "log_excerpt": _excerpt(log), "cached": False,
              "libraries": libs["installed"]}
    try:
        with open(os.path.join(outdir, "build.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(record, fh)
    except OSError:
        pass  # the binaries stand even when the record cannot persist
    return record


def python_exe() -> str:
    """Interpreter for `-m esptool` flashes (test seam)."""
    return sys.executable
