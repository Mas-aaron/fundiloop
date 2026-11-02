"""Learning execution: tap-paced plan steps + verified code delivery.

The learner advances steps by tapping; live telemetry (not per-step
sensor checks) proves the build. This module authors the project's
firmware — model-first, cached per session — and rejects
never-talking firmware the monitor could never verify.
"""
from __future__ import annotations

import json
import re

from .prompts import learning_code_prompt


def _model_text_usable(text: str) -> bool:
    """True when the model's reply is usable output worth showing.

    Rejects refusals and brace-led replies so a guardrail trip or a
    JSON stub ({"code": "placeholder"}, with or without trailing
    text) degrades to the template instead of leaking into the UI or
    the compiler. Technical caps (SDA, COM9) still pass: only the
    refusal shape is rejected, never the vocabulary.
    """
    text = (text or "").strip()
    if not text or "refusal" in text.lower():
        return False
    if text.startswith("{"):
        return False
    return True


def _project_code_key(session: dict) -> str:
    """Cache key for generate_code: one firmware per evidence state.

    Plan, pins, and title never change within a session; only the
    verified evidence grows, so it alone keys the cached firmware.
    """
    return json.dumps({"verified": session.get("verified") or {}},
                      sort_keys=True, default=str)


_TELEMETRY_PRINT = re.compile(r"Serial\.(print|println|printf)\s*\(")


def _firmware_has_telemetry(code: str) -> bool:
    """True when the firmware can prove the build: it opens Serial
    and prints something. The proof rule (not this nudge) is the
    real gate; this only rejects never-talking firmware early."""
    text = str(code or "")
    return "Serial.begin(" in text and bool(_TELEMETRY_PRINT.search(text))


def _complete_with_retry(Muse, system: str, user: str) -> str:
    """One immediate retry around a firmware model call.

    The adapters run with max_retries=0 (the app owns retry policy);
    a single second attempt turns a transient API blip into firmware
    instead of a 502. Only MetaAPIError retries — anything else, and
    the second failure, propagates to the caller.
    """
    from .adapters import MetaAPIError
    try:
        return Muse.complete_text(system, user)
    except MetaAPIError:
        return Muse.complete_text(system, user)


def _extract_code_field(text: str):
    """Salvage firmware from a JSON stub carrying a real sketch.

    Returns the embedded code only when it looks compilable (setup
    and loop present); {"code": "placeholder"} stays rejected.
    """
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("code")
    if isinstance(code, str) and "void setup" in code \
            and "void loop" in code:
        return code.strip() or None
    return None


def _clean_firmware(text: str) -> str:
    code = (text or "").strip()
    if code.startswith("```"):
        import re

        code = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", code).strip()
    return code


def generate_code(session: dict, Muse) -> str:
    """Generate the project's .ino firmware.

    Cached per session: refetching unchanged firmware never burns
    another ~17s model call. Rejects never-talking firmware: the
    build proves itself through telemetry, so firmware without
    Serial output fails loud (502, retryable) instead of caching
    poison the monitor can never verify.
    """
    cache = session.setdefault("project_code", {})
    key = _project_code_key(session)
    cached = cache.get(key)
    if isinstance(cached, str) and cached:
        return cached
    if getattr(Muse, "mock", False):
        code = _mock_firmware(session)
    else:
        from .adapters import MetaAPIError

        system = ("You write Arduino firmware. Return ONLY Arduino code. "
                  "No JSON. No markdown fences. No explanations. Start with "
                  "a comment or #include.")
        user = learning_code_prompt(
            session.get("project_title", session.get("project_id", "project")),
            session.get("plan", []), session.get("visible_parts", []),
            session.get("verified", {}), session.get("pin_map", {}))

        def _usable(text):
            cleaned = _clean_firmware(text)
            if not cleaned:
                return None
            if _model_text_usable(cleaned):
                return cleaned
            return _extract_code_field(cleaned)

        code = _usable(_complete_with_retry(Muse, system, user))
        if code is None:
            # Rut-breaker: the model sometimes answers with a reasoning
            # stub instead of code. One explicit retry before failing
            # loud; a second stub raises below and caches nothing.
            code = _usable(_complete_with_retry(
                Muse,
                system + " Your last reply was not code; reply with "
                "code only.",
                user))
        if code is None:
            # A JSON stub is never firmware: fail loud (502, retryable)
            # instead of caching poison the compiler chokes on.
            raise MetaAPIError("Muse returned unusable firmware")
        if not _firmware_has_telemetry(code):
            # Telemetry contract: one explicit retry demanding Serial
            # output, then fail loud — silent firmware can never prove
            # the build, so caching it strands the monitor.
            code = _usable(_complete_with_retry(
                Muse,
                system + " Your firmware MUST open Serial at 115200 and "
                "print readings plus a STATUS heartbeat every loop; "
                "reply with code only.",
                user))
            if code is None or not _firmware_has_telemetry(code):
                raise MetaAPIError(
                    "Muse returned firmware without telemetry")
    cache[key] = code
    return code


def _mock_firmware(session: dict) -> str:
    """Deterministic mock .ino using the session's pins/addresses.

    Prints a STATUS heartbeat every loop: mock sessions verify
    end to end under the same telemetry contract as real ones.
    """
    pin_map = session.get("pin_map", {}) or {}
    sda, scl, led = pin_map.get("sda", 21), pin_map.get("scl", 22), pin_map.get("led_gpio", 2)
    verified = session.get("verified", {}) or {}
    addrs = ", ".join(verified.get("i2c", [])) or "none seen"
    title = session.get("project_title", session.get("project_id", "project"))
    board = pin_map.get("label", pin_map.get("board", "?"))
    lines = [
        "// %s for %s (mock firmware)" % (title, board),
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
        '  Serial.println("STATUS mock build running");',
        "}",
        "",
        "void loop() {",
        "  digitalWrite(LED_PIN, HIGH); delay(500);",
        "  digitalWrite(LED_PIN, LOW); delay(500);",
        '  Serial.println("STATUS ok");',
        "}",
        "",
    ]
    return "\n".join(lines)
