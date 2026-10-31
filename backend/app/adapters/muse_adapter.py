"""Meta Muse adapter: reason over sense data, return an AgentDecision.

MOCK mode (default) applies deterministic rules over the probe + vision
data so the loop runs offline. The decision is advisory: the orchestrator
runs verification_test itself and the sensor result decides pass/fail.
"""
from __future__ import annotations

import json
import os
import re
import threading

from . import (
    MetaAPIError,
    api_base_url,
    api_key,
    api_timeout,
    log_model_call,
    mock_meta_enabled,
)
from ..models import AgentDecision, AgentState, Mode, VerificationTest


def _mock_enabled() -> bool:
    return mock_meta_enabled()


BME_ADDRS = ("0x76", "0x77")
OLED_ADDR = "0x3C"

SYSTEM_PROMPT = """You are FundiLoop, a verifiable assembly and diagnostic agent for embedded hardware.
The target circuit is an ESP32 DevKit with a BME280 sensor over I2C (0x76 or 0x77), an SSD1306 OLED over I2C (0x3C), and an LED on GPIO 2.
A technician builds or repairs this circuit while you see the overhead frame photo, SAM vision regions, serial probe measurements, and an optional code snippet.
Look at the photo yourself and name only what you can actually see there; the bench may hold other equipment, and SAM regions are unlabeled boxes, not identifications. If the photo is too blurry or dark to identify parts, say so in your evidence instead of guessing.
Return ONLY valid JSON matching this schema, no prose:
{"mode": "GUIDED_ASSEMBLY|DIAGNOSTIC", "state": "INSTRUCTING|VERIFYING|FAULT_FOUND|VERIFIED_FIXED|COMPLETE",
 "fault_hypothesis": "SDA_SCL_SWAPPED|LOOSE_GND|CODE_PIN_MISMATCH|SENSOR_DISCONNECTED|LED_REVERSED|null",
 "confidence": 0.0-1.0, "evidence": [str], "next_instruction": str, "speak_to_tech": str,
 "verification_test": "RUN_I2C_SCAN|CHECK_OLED|BLINK_LED|READ_SENSOR|SELFTEST|PING|NONE"}
Rules: never declare success yourself. The backend runs verification_test and the sensor reading decides.
Guide exactly one step or one fix at a time. Keep speak_to_tech under 25 words of plain spoken English (no ALL-CAPS words, no code, no parenthetical numbers).
Ground every claim in the supplied SAM segments and probe readings, never in the expected circuit.
If the camera shows a different board than the target circuit (for example an Arduino), name it
and tell the user the Learning tab guides any board. If the bench is empty, say so and wait for parts."""


class MetaClaudeAdapter:
    """Reasoning adapter. Aliased as MetaMuseAdapter (Muse Spark)."""

    def __init__(self, model: str | None = None, mock: bool | None = None) -> None:
        self.model = model or os.environ.get("MUSE_MODEL", "muse-spark-1.3")
        self.mock = _mock_enabled() if mock is None else mock
        self._client = None  # persistent OpenAI client (lazy, thread-safe)
        self._client_lock = threading.Lock()

    def _api_client(self):
        """One shared client per adapter: keeps the TLS session warm.

        Base URL, key, and timeout are read on first use; a backend restart
        picks up changed values. The client is safe for concurrent calls.
        """
        if self._client is None:
            from openai import OpenAI

            with self._client_lock:
                if self._client is None:
                    # No SDK retries: the app owns retry policy, and client
                    # retries multiply tail latency (30s -> 90s hangs).
                    self._client = OpenAI(base_url=api_base_url(), api_key=api_key(),
                                          timeout=api_timeout(), max_retries=0)
        return self._client

    def decide(self, context: dict) -> AgentDecision:
        """Return the next AgentDecision for the sensed context."""
        if self.mock:
            if not mock_meta_enabled():
                raise MetaAPIError("mock Muse adapter called with MOCK_META=false")
            return self._mock_decide(context)
        return self._real_decide(context)

    def complete_text(self, system: str, user: str,
                      image_b64: str | None = None,
                      video_b64: str | None = None,
                      video_mime: str = "video/mp4") -> str:
        """Free-form Muse Spark completion for discovery/live (raw text out).

        A still goes in as image_url; a clip as a video_url part (Muse Spark
        reads frames and any embedded audio). Callers own JSON parsing and
        retry policy. Mock mode returns "{}".
        """
        if self.mock:
            if not mock_meta_enabled():
                raise MetaAPIError("mock Muse adapter called with MOCK_META=false")
            return "{}"
        return self._real_complete(system, user, image_b64, video_b64, video_mime)

    def complete_text_stream(self, system: str, user: str,
                             image_b64: str | None = None,
                             video_b64: str | None = None,
                             video_mime: str = "video/mp4",
                             deadline_s: float | None = None):
        """Yield free-form answer tokens as they arrive (prose, no JSON mode).

        Nothing depends on streaming existing: when the stream cannot even
        start, the full completion is yielded once instead. A failure after
        the first token raises MetaAPIError — the caller keeps the partial
        text already yielded. Mock mode yields "{}" once. The total stream
        wall time is capped (default api_timeout): a stalled stream raises
        instead of hanging for minutes.
        """
        import time

        if self.mock:
            if not mock_meta_enabled():
                raise MetaAPIError("mock Muse adapter called with MOCK_META=false")
            yield "{}"
            return
        if not api_key():
            raise MetaAPIError("META_API_KEY is not set")
        messages = self._complete_messages(system, user, image_b64,
                                           video_b64, video_mime)
        started = time.monotonic()
        limit = api_timeout() if deadline_s is None else deadline_s
        parts: list[str] = []
        try:
            client = self._api_client()
            stream = client.chat.completions.create(
                model=self.model, messages=messages, stream=True)
            try:
                for event in stream:
                    if time.monotonic() - started > limit:
                        raise TimeoutError(
                            "Muse stream exceeded its total deadline")
                    text = self._chunk_text(event)
                    if text:
                        parts.append(text)
                        yield text
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        except MetaAPIError:
            raise
        except Exception as exc:
            if parts:
                latency_ms = (time.monotonic() - started) * 1000
                log_model_call(self.model, latency_ms, "error", "",
                               raw_summary="".join(parts), ok=False,
                               error="stream failed: %s" % exc)
                raise MetaAPIError("Muse stream failed: %s" % exc) from exc
            yield self._real_complete(system, user, image_b64, video_b64,
                                      video_mime, json_mode=False)
            return
        latency_ms = (time.monotonic() - started) * 1000
        log_model_call(self.model, latency_ms, "stream", "",
                       raw_summary="".join(parts))

    def chat_text(self, system: str, messages: list[dict],
                  json_mode: bool = True) -> str:
        """Free-form chat over a full message history (raw text out).

        Tier 1 thin seam: one function that sends a conversation and gets a
        reply. Callers own the history and any JSON parsing. Mock mode "{}".
        """
        if self.mock:
            if not mock_meta_enabled():
                raise MetaAPIError("mock Muse adapter called with MOCK_META=false")
            return "{}"
        return self._real_chat([{"role": "system", "content": system},
                                *list(messages or [])], json_mode)

    @staticmethod
    def _chunk_text(event) -> str:
        """Content delta of one chat chunk ("" for role-only chunks)."""
        choices = getattr(event, "choices", None) or []
        if not choices:
            return ""
        content = getattr(getattr(choices[0], "delta", None), "content", None)
        return content if isinstance(content, str) else ""

    @staticmethod
    def _complete_messages(system: str, user: str,
                           image_b64: str | None, video_b64: str | None,
                           video_mime: str) -> list:
        """System + user messages for a free-form completion."""
        if video_b64 or image_b64:
            user_content: object = [{"type": "text", "text": user}]
            if video_b64:
                user_content.append({
                    "type": "video_url",
                    "video_url": {"url": "data:%s;base64,%s" % (video_mime, video_b64)},
                })
            if image_b64:
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,%s" % image_b64},
                })
        else:
            user_content = user
        return [{"role": "system", "content": system},
                {"role": "user", "content": user_content}]

    def _real_complete(self, system: str, user: str,
                       image_b64: str | None = None,
                       video_b64: str | None = None,
                       video_mime: str = "video/mp4",
                       json_mode: bool = True) -> str:
        return self._real_chat(self._complete_messages(system, user, image_b64,
                                                       video_b64, video_mime),
                               json_mode)

    def _real_chat(self, messages: list, json_mode: bool = True) -> str:
        """POST one chat message list; parse, log, and return the text."""
        import time

        if not api_key():
            raise MetaAPIError("META_API_KEY is not set")
        started = time.monotonic()
        try:
            client = self._api_client()
            if json_mode:
                try:
                    raw_resp = client.chat.completions.with_raw_response.create(
                        model=self.model, messages=messages,
                        response_format={"type": "json_object"})
                except Exception:
                    raw_resp = client.chat.completions.with_raw_response.create(
                        model=self.model, messages=messages)
            else:
                raw_resp = client.chat.completions.with_raw_response.create(
                    model=self.model, messages=messages)
            status = getattr(raw_resp, "status_code", "?")
            headers = getattr(raw_resp, "headers", {}) or {}
            request_id = (
                headers.get("x-request-id") or headers.get("request-id")
                or headers.get("x-requestid") or ""
            )
            resp = raw_resp.parse()
        except Exception as exc:
            latency_ms = (time.monotonic() - started) * 1000
            log_model_call(self.model, latency_ms, "error", "",
                           raw_summary="", ok=False, error=str(exc))
            raise MetaAPIError("Muse request failed: %s" % exc) from exc
        latency_ms = (time.monotonic() - started) * 1000
        text = (resp.choices[0].message.content or "").strip()
        if not request_id:
            request_id = getattr(resp, "id", "") or ""
        if not text:
            log_model_call(self.model, latency_ms, status, "", request_id,
                           raw_summary="", ok=False, error="empty completion")
            raise MetaAPIError("Muse returned an empty completion")
        log_model_call(self.model, latency_ms, status, "", request_id,
                       raw_summary=text)
        return text

    def _real_decide(self, context: dict) -> AgentDecision:
        """Call Muse Spark (legacy backend/muse_client.py pattern).

        Sends the frame photo + SAM regions + serial probe readings +
        circuit state, forces structured JSON, validates into AgentDecision,
        retries once on invalid output, then raises MetaAPIError with the
        real error. Every call is logged to model_calls.jsonl.
        """
        import time

        if not api_key():
            raise MetaAPIError("META_API_KEY is not set")
        frame_file = str(context.get("frame_file") or "")
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for turn in context.get("history") or []:
            if isinstance(turn, dict) and "role" in turn and "content" in turn:
                messages.append({"role": turn["role"], "content": str(turn["content"])})
        state_text = json.dumps(self._context_payload(context))
        image_b64 = context.get("frame_jpeg_b64") or ""
        if image_b64:
            user_content: object = [
                {"type": "text", "text": state_text},
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64,%s" % image_b64}},
            ]
        else:
            user_content = state_text
        messages.append({"role": "user", "content": user_content})
        last_err: Exception | None = None
        last_raw = ""
        for _ in range(2):  # initial try + one retry on invalid JSON
            started = time.monotonic()
            try:
                client = self._api_client()
                try:
                    raw_resp = client.chat.completions.with_raw_response.create(
                        model=self.model, messages=messages,
                        response_format={"type": "json_object"})
                except Exception:
                    raw_resp = client.chat.completions.with_raw_response.create(
                        model=self.model, messages=messages)
                status = getattr(raw_resp, "status_code", "?")
                headers = getattr(raw_resp, "headers", {}) or {}
                request_id = (
                    headers.get("x-request-id") or headers.get("request-id")
                    or headers.get("x-requestid") or ""
                )
                resp = raw_resp.parse()
            except Exception as exc:
                latency_ms = (time.monotonic() - started) * 1000
                log_model_call(self.model, latency_ms, "error", frame_file,
                               raw_summary="", ok=False, error=str(exc))
                raise MetaAPIError("Muse request failed: %s" % exc) from exc
            latency_ms = (time.monotonic() - started) * 1000
            last_raw = (resp.choices[0].message.content or "").strip()
            if not request_id:
                request_id = getattr(resp, "id", "") or ""
            if last_raw.startswith("```"):  # strip code fences if the model adds them
                last_raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", last_raw)
            try:
                decision = AgentDecision.model_validate_json(last_raw)
            except Exception as exc:
                last_err = exc
                log_model_call(self.model, latency_ms, status, frame_file, request_id,
                               raw_summary=last_raw, ok=False,
                               error="invalid AgentDecision JSON: %s" % exc)
                messages.append({"role": "user", "content":
                                 "Your last reply was not valid JSON matching the schema. "
                                 "Reply with ONLY the JSON object."})
                continue
            log_model_call(self.model, latency_ms, status, frame_file, request_id,
                           raw_summary=last_raw)
            return decision
        raise MetaAPIError("Muse returned invalid JSON twice: %s\nRAW:\n%.300s"
                           % (last_err, last_raw))

    @staticmethod
    def _context_payload(ctx: dict) -> dict:
        """Project the orchestrator context into JSON-serializable state."""
        step = ctx.get("step")
        probe = ctx.get("probe")
        graph = ctx.get("graph")
        sam = ctx.get("sam") or {}
        code = str(ctx.get("code_snippet") or "")
        mode = ctx.get("mode", Mode.GUIDED_ASSEMBLY)
        sam_dets = [
            {"label": d.get("label"), "box": d.get("box"),
             "confidence": d.get("confidence")}
            for d in (sam.get("detections", []) or []) if isinstance(d, dict)
        ]
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
            "sam": {"detections": sam_dets,
                    "wire_count": sam.get("wire_count"),
                    "wires_ok": sam.get("wires_ok"), "notes": sam.get("notes", "")},
            "ranked_faults": ctx.get("ranked"),
            "failed_verification": ctx.get("verification"),
            "code_snippet": code[:2000],
            "frame_file": str(ctx.get("frame_file") or ""),
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
        evidence = evidence + [self._vision_summary(ctx.get("sam") or {})]
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
            evidence=["probe: %d I2C device(s) [%s]" % (len(devices), ", ".join(devices) or "none")] + evidence,
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

    @staticmethod
    def _vision_summary(sam: dict) -> str:
        """One-line citation of what the camera segmentation reported."""
        labels = [d.get("label", "?") for d in (sam.get("detections") or [])]
        notes = sam.get("notes", "")
        text = "vision sees: " + (", ".join(labels) if labels else "nothing usable")
        if sam.get("wires_ok") is False or "NOT visible" in notes or "dark" in notes:
            text += " — " + notes
        return text

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
            "LOOSE_GND": "Power or ground looks loose. Reseat the GND wire on both ends, then press Check Now.",
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
