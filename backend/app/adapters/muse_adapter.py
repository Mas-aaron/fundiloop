"""Meta Muse adapter: reason over sense data, return an AgentDecision.

MOCK mode (default) applies deterministic rules over the probe + vision
data so the loop runs offline. The decision is advisory: the orchestrator
runs verification_test itself and the sensor result decides pass/fail.
"""
from __future__ import annotations

import json
import os
import re

from . import MetaAPIError, api_base_url, api_key, api_timeout
from ..models import AgentDecision, AgentState, Mode, VerificationTest


def _mock_enabled() -> bool:
    if os.environ.get("MOCK_META", "true").lower() in ("1", "true", "yes"):
        return True
    return not os.environ.get("META_API_KEY")


BME_ADDRS = ("0x76", "0x77")
OLED_ADDR = "0x3C"

SYSTEM_PROMPT = """You are FundiLoop, a verifiable assembly and diagnostic agent for embedded hardware.
The target circuit is an ESP32 DevKit with a BME280 sensor over I2C (0x76 or 0x77), an SSD1306 OLED over I2C (0x3C), and an LED on GPIO 2.
A technician builds or repairs this circuit while you see SAM vision segments, serial probe measurements, and an optional code snippet.
Return ONLY valid JSON matching this schema, no prose:
{"mode": "GUIDED_ASSEMBLY|DIAGNOSTIC", "state": "INSTRUCTING|VERIFYING|FAULT_FOUND|VERIFIED_FIXED|COMPLETE",
 "fault_hypothesis": "SDA_SCL_SWAPPED|LOOSE_GND|CODE_PIN_MISMATCH|SENSOR_DISCONNECTED|LED_REVERSED|null",
 "confidence": 0.0-1.0, "evidence": [str], "next_instruction": str, "speak_to_tech": str,
 "verification_test": "RUN_I2C_SCAN|CHECK_OLED|BLINK_LED|READ_SENSOR|SELFTEST|PING|NONE"}
Rules: never declare success yourself. The backend runs verification_test and the sensor reading decides.
Guide exactly one step or one fix at a time. Keep speak_to_tech under 25 words, speakable."""


class MetaClaudeAdapter:
    """Reasoning adapter. Aliased as MetaMuseAdapter (Muse Spark)."""

    def __init__(self, model: str | None = None, mock: bool | None = None) -> None:
        self.model = model or os.environ.get("MUSE_MODEL", "muse-spark-1.3")
        self.mock = _mock_enabled() if mock is None else mock

    def decide(self, context: dict) -> AgentDecision:
        """Return the next AgentDecision for the sensed context."""
        if self.mock:
            return self._mock_decide(context)
        return self._real_decide(context)

    def _real_decide(self, context: dict) -> AgentDecision:
        """Call Muse Spark (legacy backend/muse_client.py pattern).

        Forces structured JSON, validates into AgentDecision, retries once
        on invalid output, then raises MetaAPIError for a safe fallback.
        """
        from openai import OpenAI

        if not api_key():
            raise MetaAPIError("META_API_KEY is not set")
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for turn in context.get("history") or []:
            if isinstance(turn, dict) and "role" in turn and "content" in turn:
                messages.append({"role": turn["role"], "content": str(turn["content"])})
        messages.append({"role": "user", "content": json.dumps(self._context_payload(context))})
        last_err: Exception | None = None
        for _ in range(2):  # initial try + one retry on invalid JSON
            try:
                client = OpenAI(base_url=api_base_url(), api_key=api_key(),
                                timeout=api_timeout())
                try:
                    resp = client.chat.completions.create(
                        model=self.model, messages=messages,
                        response_format={"type": "json_object"})
                except Exception:
                    resp = client.chat.completions.create(model=self.model, messages=messages)
            except Exception as exc:
                raise MetaAPIError("Muse request failed: %s" % exc) from exc
            raw = (resp.choices[0].message.content or "").strip()
            if raw.startswith("```"):  # strip code fences if the model adds them
                raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw)
            try:
                return AgentDecision.model_validate_json(raw)
            except Exception as exc:
                last_err = exc
                messages.append({"role": "user", "content":
                                 "Your last reply was not valid JSON matching the schema. "
                                 "Reply with ONLY the JSON object."})
        raise MetaAPIError("Muse returned invalid JSON twice: %s" % last_err)

    @staticmethod
    def _context_payload(ctx: dict) -> dict:
        """Project the orchestrator context into JSON-serializable state."""
        step = ctx.get("step")
        probe = ctx.get("probe")
        graph = ctx.get("graph")
        sam = ctx.get("sam") or {}
        code = str(ctx.get("code_snippet") or "")
        mode = ctx.get("mode", Mode.GUIDED_ASSEMBLY)
        return {
            "mode": mode.value if isinstance(mode, Mode) else str(mode),
            "step": step.model_dump() if hasattr(step, "model_dump") else step,
            "step_index": ctx.get("step_index", 0),
            "steps_total": ctx.get("steps_total", 0),
            "known_good": {
                "led_gpio": getattr(graph, "led_gpio", 2),
                "i2c_sda_gpio": getattr(graph, "i2c_sda_gpio", 21),
                "i2c_scl_gpio": getattr(graph, "i2c_scl_gpio", 22),
                "i2c_expected": getattr(graph, "i2c_expected", {}),
            },
            "probe": probe.model_dump(exclude={"raw"}) if hasattr(probe, "model_dump") else probe,
            "sam": {"detections": sam.get("detections", []),
                    "wire_count": sam.get("wire_count"),
                    "wires_ok": sam.get("wires_ok"), "notes": sam.get("notes", "")},
            "ranked_faults": ctx.get("ranked"),
            "failed_verification": ctx.get("verification"),
            "code_snippet": code[:2000],
        }

    # -- mock reasoning -------------------------------------------------
    def _mock_decide(self, ctx: dict) -> AgentDecision:
        mode = ctx.get("mode", Mode.GUIDED_ASSEMBLY)
        if mode == Mode.DIAGNOSTIC or str(mode) == "DIAGNOSTIC":
            return self._mock_diagnostic(ctx)
        return self._mock_guided(ctx)

    def _mock_guided(self, ctx: dict) -> AgentDecision:
        step = ctx.get("step")
        probe = ctx.get("probe")
        devices = list(getattr(probe, "i2c_devices", []) or [])
        code = str(ctx.get("code_snippet") or "")

        if step is None:  # session complete
            return AgentDecision(
                mode=Mode.GUIDED_ASSEMBLY,
                state=AgentState.COMPLETE,
                fault_hypothesis=None,
                confidence=1.0,
                evidence=["all 8 assembly steps verified by sensor tests"],
                next_instruction="Assembly complete. Passport issued.",
                speak_to_tech="Assembly complete. All tests passed.",
                verification_test=VerificationTest.NONE,
            )

        test = step.verification_test
        fault, evidence = self._detect_fault(step, devices, probe, code)
        ver = ctx.get("verification") or {}
        measured = ""
        if ver.get("test"):
            measured = "%s: %s" % (ver.get("test"), ver.get("measured", ""))
        if fault is None and ver.get("passed") is False:
            # The test failed but no rule fired: map the failed test itself.
            fault, extra = self._fault_from_failed_test(ver, code)
            evidence = extra + evidence
        if measured:
            evidence = [measured] + evidence
        if fault is not None:
            return AgentDecision(
                mode=Mode.GUIDED_ASSEMBLY,
                state=AgentState.FAULT_FOUND,
                fault_hypothesis=fault,
                confidence=0.85,
                evidence=evidence,
                next_instruction=self._fix_hint(fault),
                speak_to_tech=self._fix_hint(fault),
                verification_test=test,
            )
        return AgentDecision(
            mode=Mode.GUIDED_ASSEMBLY,
            state=AgentState.INSTRUCTING,
            fault_hypothesis=None,
            confidence=0.9,
            evidence=["probe: %d I2C device(s) [%s]" % (len(devices), ", ".join(devices) or "none")],
            next_instruction=step.instruction_student,
            speak_to_tech=step.instruction_student,
            verification_test=test,
        )

    def _detect_fault(self, step, devices, probe, code) -> tuple:
        """Map observable evidence to one of the 5 known faults."""
        has_bme = any(a in devices for a in BME_ADDRS)
        has_oled = OLED_ADDR in devices
        needs_bme = any("0x76" in g or "0x77" in g for g in (step.expect_i2c_any or []))
        needs_oled = "0x3C" in (step.expect_i2c or []) or step.expect_oled_lit

        if step.expect_led and getattr(probe, "led_ok", True) is False:
            pin = self._gpio_from_code(code)
            if pin is not None and pin != 2:
                return "CODE_PIN_MISMATCH", [
                    "config wires the LED to GPIO 2 but the code drives GPIO %d" % pin,
                    "GPIO 2 toggles with no light; wiring matches the guide",
                ]
            return "LED_REVERSED", [
                "GPIO 2 toggles but the LED never lights",
                "wiring matches the guide; flipping the LED is the known fix",
            ]
        if needs_oled and not has_oled:
            return "SENSOR_DISCONNECTED", [  # OLED missing from the bus
                "I2C scan is missing 0x3C (OLED not on the bus)",
                "scan found [%s]" % (", ".join(devices) or "nothing"),
            ]
        if needs_bme and not has_bme:
            if not devices and not getattr(probe, "connected", False):
                return "LOOSE_GND", [
                    "no I2C devices and no serial signs of life",
                    "ground rail is the first suspect; press on the GND wire",
                ]
            if not devices:
                return "SDA_SCL_SWAPPED", [
                    "bus is completely silent though power wiring looks done",
                    "SDA and SCL may be swapped at the module",
                ]
            return "SENSOR_DISCONNECTED", [
                "I2C scan finds 0x3C but neither 0x76 nor 0x77",
                "BME280 module is not on the bus",
            ]
        if step.expect_oled_lit and getattr(probe, "oled_ok", True) is False:
            return "LOOSE_GND", [
                "OLED answers on I2C but the screen stays dark",
                "VCC or GND to the display is suspect",
            ]
        return None, []

    def _fault_from_failed_test(self, ver: dict, code: str) -> tuple:
        """Map a failed verification test to a fault when no rule fired."""
        test = ver.get("test", "")
        if test == "RUN_I2C_SCAN":
            return "SENSOR_DISCONNECTED", ["expected device missing from the I2C scan"]
        if test == "READ_SENSOR":
            return "SENSOR_DISCONNECTED", ["sensor read failed although wiring looks done"]
        if test == "CHECK_OLED":
            return "LOOSE_GND", ["display test failed; VCC or GND to the OLED is suspect"]
        if test in ("BLINK_LED", "SELFTEST"):
            pin = self._gpio_from_code(code)
            if pin is not None and pin != 2:
                return "CODE_PIN_MISMATCH", [
                    "self-test LED check failed; code drives GPIO %d, circuit wires GPIO 2" % pin
                ]
            return "LED_REVERSED", ["self-test LED check failed; LED may be reversed"]
        return "LOOSE_GND", ["PING failed: board not answering over USB serial"]

    @staticmethod
    def _gpio_from_code(code: str):
        m = re.search(r"(?:GPIO|LED[_\s-]?PIN|pin)\D{0,8}(\d{1,2})", code, re.IGNORECASE)
        return int(m.group(1)) if m else None

    @staticmethod
    def _fix_hint(fault: str) -> str:
        return {
            "SDA_SCL_SWAPPED": "The I2C bus is silent. Swap the SDA and SCL wires at the module, then press Check Now.",
            "LOOSE_GND": "Power or ground looks loose. Reseat the black GND wire on both ends, then press Check Now.",
            "CODE_PIN_MISMATCH": "Your code drives a different pin than the wired LED. Change the code to GPIO 2, re-flash, then press Check Now.",
            "SENSOR_DISCONNECTED": "The BME280 is missing from the bus. Reseat its four wires, then press Check Now.",
            "LED_REVERSED": "The LED is backwards. Flip it so the long leg faces GPIO 2 through the resistor, then press Check Now.",
        }.get(fault, "Check the wiring for this step, then press Check Now.")

    def _mock_diagnostic(self, ctx: dict) -> AgentDecision:
        from ..fault_ranker import FAULT_TESTS, RANK_THRESHOLD, rank_faults

        probe = ctx.get("probe")
        devices = list(getattr(probe, "i2c_devices", []) or [])
        ranked = ctx.get("ranked") or rank_faults(
            probe, ctx.get("sam") or {}, str(ctx.get("code_snippet") or ""), ctx.get("graph"))
        top = ranked[0] if ranked else None
        if top is None or top["score"] < RANK_THRESHOLD:
            return AgentDecision(
                mode=Mode.DIAGNOSTIC,
                state=AgentState.VERIFIED_FIXED,
                fault_hypothesis=None,
                confidence=0.95,
                evidence=["scan [%s]" % ", ".join(devices), "sensor reads valid", "OLED lit", "LED blinks"],
                next_instruction="No fault found. All measurements nominal.",
                speak_to_tech="No fault found. Everything checks out.",
                verification_test=VerificationTest.SELFTEST,
            )
        fault = top["fault_id"]
        return AgentDecision(
            mode=Mode.DIAGNOSTIC,
            state=AgentState.FAULT_FOUND,
            fault_hypothesis=fault,
            confidence=top["score"],
            evidence=list(top["evidence"]),
            next_instruction=self._fix_hint(fault),
            speak_to_tech=self._fix_hint(fault),
            verification_test=FAULT_TESTS.get(fault, VerificationTest.SELFTEST),
        )


# Muse Spark naming alias (same adapter, same behavior).
MetaMuseAdapter = MetaClaudeAdapter
