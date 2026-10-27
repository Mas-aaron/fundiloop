"""Agent orchestrator: the Sense -> Reason -> Instruct -> Verify loop.

CRITICAL RULE: Muse never declares success alone. On every turn the
orchestrator runs the verification_test itself (SAM vision + serial probe)
and the sensor result decides pass/fail, step advance, and completion.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ATTEMPT_LOG = Path(__file__).resolve().parent.parent / "logs" / "attempts.jsonl"
GENESIS_HASH = "0" * 64

from .adapters import MetaAPIError
from .adapters.muse_adapter import MetaClaudeAdapter
from .adapters.sam_adapter import MetaSAMAdapter
from .adapters.voice_adapter import MetaVoiceAdapter
from .camera_client import CameraClient
from .change_detector import MotionSettler
from .circuit_graph import CircuitGraph, load_circuit_graph
from .fault_ranker import RANK_THRESHOLD, rank_faults
from .models import (
    AgentDecision,
    AgentState,
    AssemblyStep,
    CheckNowRequest,
    Mode,
    Passport,
    ProbeReading,
    StepCheck,
    VerificationTest,
)
from .serial_probe import SerialProbe


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _probe_summary(probe: ProbeReading) -> dict:
    return {
        "connected": probe.connected,
        "i2c_devices": probe.i2c_devices,
        "temperature_c": probe.temperature_c,
        "humidity_pct": probe.humidity_pct,
        "pressure_hpa": probe.pressure_hpa,
        "oled_ok": probe.oled_ok,
        "led_ok": probe.led_ok,
        "mock": probe.mock,
    }


def _event_hash(entry: dict) -> str:
    """Hash a log entry (excluding its own "hash") for the hash chain."""
    blob = json.dumps({k: v for k, v in entry.items() if k != "hash"},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def verify_chain(events: list) -> bool:
    """Recompute the hash chain; True only if every link is intact."""
    prev = GENESIS_HASH
    for i, entry in enumerate(events, start=1):
        if entry.get("seq") != i or entry.get("prev_hash") != prev:
            return False
        if entry.get("hash") != _event_hash(entry):
            return False
        prev = entry["hash"]
    return True


class AgentOrchestrator:
    def __init__(
        self,
        graph: CircuitGraph | None = None,
        camera: CameraClient | None = None,
        settler: MotionSettler | None = None,
        probe: SerialProbe | None = None,
        sam: MetaSAMAdapter | None = None,
        Muse: MetaClaudeAdapter | None = None,
        voice: MetaVoiceAdapter | None = None,
        attempt_log_path: str | Path | None = None,
    ) -> None:
        self.graph = graph or load_circuit_graph()
        self.camera = camera or CameraClient()
        self.settler = settler or MotionSettler(settle_seconds=1.0)
        self.probe = probe or SerialProbe()
        self.sam = sam or MetaSAMAdapter()
        self.Muse = Muse or MetaClaudeAdapter()
        self.voice = voice or MetaVoiceAdapter()
        self.attempt_log_path = Path(attempt_log_path) if attempt_log_path else DEFAULT_ATTEMPT_LOG
        self.demo_mode = os.environ.get("DEMO_MODE", "true").lower() in ("1", "true", "yes")
        self._mock_sam = MetaSAMAdapter(mock=True)
        self._mock_claude = MetaClaudeAdapter(mock=True)
        self._mock_voice = MetaVoiceAdapter(mock=True)
        self.sessions: dict[str, dict] = {}
        self.current_id: str | None = None
        self.session: dict | None = None
        self._turn_fallbacks: list = []

    # -- session --------------------------------------------------------
    def start_session(self, mode: Mode = Mode.GUIDED_ASSEMBLY) -> dict:
        self.settler.reset()
        session_id = uuid.uuid4().hex[:8]
        self.session = {
            "id": session_id,
            "mode": mode,
            "step_index": 0,
            "checklist": [StepCheck(step_id=s.step_id) for s in self.graph.steps],
            "faults_found": [],
            "measurements_before": {},
            "measurements_after": {},
            "started_at": _now(),
            "completed_at": "",
            "turns": 0,
            "attempts": [],
            "current_hypothesis": None,
            "baseline": {},
            "history": [],
            "last_event_hash": GENESIS_HASH,
            "last_fallbacks": [],
        }
        self.sessions[session_id] = self.session
        self.current_id = session_id
        if mode == Mode.DIAGNOSTIC:
            speak = self._capture_baseline()
        else:
            step = self.graph.get_step(0)
            speak = step.instruction_student if step else "Press Check Now to begin."
        self.voice.speak(speak)
        return self._session_view(state=AgentState.INSTRUCTING, instruction=speak, speak=speak)

    def get_passport(self, session_id: str) -> dict | None:
        """Full verifiable log for one session (passport endpoint body)."""
        s = self.sessions.get(session_id)
        if s is None:
            return None
        events = list(s.get("attempts", []))
        return {
            "session_id": session_id,
            "mode": s["mode"].value if isinstance(s["mode"], Mode) else str(s["mode"]),
            "complete": bool(s.get("completed_at")),
            "passport": self._build_passport(s).model_dump(),
            "events": events,
            "event_count": len(events),
            "chain_valid": verify_chain(events),
        }

    def _build_passport(self, s: dict) -> Passport:
        attempts = list(s.get("attempts", []))
        return Passport(
            mode=str(s["mode"].value if isinstance(s["mode"], Mode) else s["mode"]),
            steps_completed=sum(1 for c in s["checklist"] if c.status == "pass"),
            steps_total=self.graph.total_steps,
            faults_found=list(s["faults_found"]),
            measurements_before=dict(s["measurements_before"]),
            measurements_after=dict(s["measurements_after"]),
            started_at=s["started_at"],
            completed_at=s["completed_at"] or "",
            total_attempts=len(attempts),
            failed_attempts=sum(1 for a in attempts if not a["passed"]),
        )

    def _capture_baseline(self) -> str:
        """Diagnostic start: SELFTEST + I2C scan + camera frame as baseline."""
        assert self.session is not None
        frame, settled, source = self._capture_settled_frame()
        sam0 = self._safe_sam(frame)
        probe0 = self.probe.read_all()
        summary = _probe_summary(probe0)
        self.session["measurements_before"] = summary
        self.session["measurements_after"] = summary
        self.session["baseline"] = {
            "at": _now(),
            "i2c_devices": summary["i2c_devices"],
            "connected": summary["connected"],
            "temperature_c": summary["temperature_c"],
            "oled_ok": summary["oled_ok"],
            "led_ok": summary["led_ok"],
            "sam_detections": [d.get("label") for d in sam0.get("detections", [])],
            "frame_settled": settled,
            "camera_source": source,
        }
        sensor = "" if summary["temperature_c"] is None else \
            ", sensor %.1f C" % summary["temperature_c"]
        return "Baseline captured: I2C [%s]%s. Press Check Now and I will rank the faults." % (
            ", ".join(summary["i2c_devices"]) or "nothing", sensor)

    def status(self) -> dict:
        if self.session is None:
            return {"session": None, "steps_total": self.graph.total_steps,
                    "circuit": self.graph.name}
        step = self.graph.get_step(self.session["step_index"])
        done = all(c.status == "pass" for c in self.session["checklist"])
        state = AgentState.COMPLETE if done and self.session["mode"] == Mode.GUIDED_ASSEMBLY \
            else AgentState.INSTRUCTING
        current = step.instruction_student if step else "Complete."
        return self._session_view(state=state, instruction=current, speak=current)

    # -- main loop: one technician turn -----------------------------------
    def check_now(self, req: CheckNowRequest | None = None) -> dict:
        req = req or CheckNowRequest()
        if self.session is None:
            self.start_session()
        assert self.session is not None
        self._turn_fallbacks = []
        mode = self.session["mode"]
        guided = mode == Mode.GUIDED_ASSEMBLY
        step = self.graph.get_step(self.session["step_index"]) if guided else None
        # Diagnostic has no assembly step: the final graph step is the
        # known-good reference that every test is checked against.
        ref_step = step if guided else self.graph.steps[-1]
        self.session["turns"] += 1

        # 1. SENSE: settled camera frame + serial probe + SAM vision.
        frame, frame_settled, camera_source = self._capture_settled_frame()
        if camera_source.startswith("mock-fallback"):
            self._turn_fallbacks.append("camera")
        sam_result = self._safe_sam(frame)
        probe = self.probe.read_all(simulate_fault=req.simulate_fault)
        if not self.session["measurements_before"]:
            self.session["measurements_before"] = _probe_summary(probe)
        self.session["measurements_after"] = _probe_summary(probe)

        # 2. REASON: Muse proposes (advisory only).
        ctx = {
            "mode": mode,
            "step": step,
            "step_index": self.session["step_index"],
            "steps_total": self.graph.total_steps,
            "probe": probe,
            "sam": sam_result,
            "code_snippet": req.code_snippet,
            "graph": self.graph,
            "history": list(self.session["history"][-10:]),
        }
        if not guided:
            ctx["ranked"] = rank_faults(probe, sam_result, req.code_snippet, self.graph)
        decision = self._safe_decide(ctx)

        # 3. VERIFY: the sensor decides, not Muse.
        verification = self._run_test(decision.verification_test, ref_step, probe, sam_result)

        # 4. On guided failure, Muse takes a corrective second pass grounded
        # in the failed test's measured evidence; the step does not advance.
        if guided and step is not None and not verification["passed"]:
            try:
                decision = self.Muse.decide({**ctx, "verification": verification})
            except MetaAPIError:
                pass  # keep the first-pass (or fallback) decision

        self._log_attempt(step, decision, verification, req.transcript,
                          frame_settled, camera_source)
        if not guided:
            view = self._finish_diagnostic(decision, verification, probe, sam_result,
                                           frame_settled, camera_source, ctx["ranked"])
        else:
            view = self._finish_guided(decision, verification, probe, sam_result,
                                       frame_settled, camera_source)
        self.session["last_fallbacks"] = list(self._turn_fallbacks)
        view["fallbacks"] = list(self._turn_fallbacks)
        view["fallback_active"] = bool(self._turn_fallbacks)
        self._append_history(req.transcript, view.get("speak", ""))
        return view

    # -- Meta API failure handling (safe fallbacks, never crash) ------------
    def _safe_sam(self, frame: bytes) -> dict:
        try:
            return self.sam.analyze(frame)
        except MetaAPIError as exc:
            if self.demo_mode and not self.sam.mock:
                self._turn_fallbacks.append("sam")
                out = self._mock_sam.analyze(frame)
                out["demo_fallback"] = True
                return out
            return {"mock": False, "error": str(exc), "detections": [],
                    "wire_count": 0, "wires_ok": True,
                    "notes": "vision unavailable (%s); continuing on serial probe" % exc}

    def _safe_decide(self, ctx: dict) -> AgentDecision:
        try:
            return self.Muse.decide(ctx)
        except MetaAPIError as exc:
            if self.demo_mode and not self.Muse.mock:
                self._turn_fallbacks.append("Muse")
                return self._mock_claude.decide(ctx)
            return self._fallback_decision(ctx, exc)

    def transcribe(self, audio: bytes, filename: str = "talk.webm") -> dict:
        """Transcribe push-to-talk audio, with demo-mode mock fallback."""
        try:
            return self.voice.transcribe(audio, filename)
        except MetaAPIError as exc:
            if self.demo_mode and not self.voice.mock:
                out = self._mock_voice.transcribe(audio, filename)
                out["demo_fallback"] = True
                return out
            raise

    @staticmethod
    def _fallback_decision(ctx: dict, exc: Exception) -> AgentDecision:
        """Safe instruction when the Meta API is unreachable: retry, verify."""
        mode = ctx.get("mode", Mode.GUIDED_ASSEMBLY)
        step = ctx.get("step")
        test = step.verification_test if step is not None else VerificationTest.SELFTEST
        msg = ("I lost contact with the Meta AI service, so nothing is verified yet. "
               "Press Check Now to retry.")
        return AgentDecision(
            mode=mode if isinstance(mode, Mode) else Mode(str(mode)),
            state=AgentState.INSTRUCTING,
            fault_hypothesis=None,
            confidence=0.0,
            evidence=["Meta API error: %s" % exc],
            next_instruction=msg,
            speak_to_tech=msg,
            verification_test=test,
        )

    def _append_history(self, transcript: str, speak: str) -> None:
        assert self.session is not None
        hist = self.session["history"]
        hist.append({"role": "user", "content": (transcript or "").strip() or "(pressed CHECK NOW)"})
        hist.append({"role": "assistant", "content": speak})
        del hist[:-20]  # keep the last 10 turns

    def _capture_settled_frame(self, max_wait_s: float = 3.0) -> tuple:
        """Poll the camera until the change detector reports a settled frame.

        Returns (frame, settled, source). After max_wait_s the latest frame
        is returned with settled=False so the loop never stalls.
        """
        frame, source = self.camera.fetch_latest()
        clean = self.settler.update(frame)
        if clean is not None:
            return clean, True, source
        deadline = time.monotonic() + max_wait_s
        while time.monotonic() < deadline:
            time.sleep(0.2)
            frame, source = self.camera.fetch_latest()
            clean = self.settler.update(frame)
            if clean is not None:
                return clean, True, source
        return frame, False, source

    def _log_attempt(self, step, decision, verification, transcript,
                     frame_settled, camera_source) -> None:
        """Record every verification attempt as a hash-chained event."""
        assert self.session is not None
        entry = {
            "seq": len(self.session["attempts"]) + 1,
            "at": _now(),
            "prev_hash": self.session.get("last_event_hash", GENESIS_HASH),
            "turn": self.session["turns"],
            "mode": self.session["mode"].value
            if isinstance(self.session["mode"], Mode) else str(self.session["mode"]),
            "step_id": step.step_id if step else None,
            "test": verification["test"],
            "passed": verification["passed"],
            "measured": verification["measured"],
            "fault_hypothesis": decision.fault_hypothesis,
            "confidence": decision.confidence,
            "evidence": list(decision.evidence),
            "decision": decision.model_dump(),
            "verification": dict(verification),
            "transcript": (transcript or "").strip(),
            "frame_settled": frame_settled,
            "camera_source": camera_source,
        }
        entry["hash"] = _event_hash(entry)
        self.session["last_event_hash"] = entry["hash"]
        self.session["attempts"].append(entry)
        try:
            self.attempt_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.attempt_log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # logging must never break the loop

    # -- verification tests (the sensor decides) --------------------------
    def _run_test(self, test: VerificationTest, step: AssemblyStep | None,
                  probe: ProbeReading, sam: dict) -> dict:
        devices = probe.i2c_devices or []
        if test == VerificationTest.PING:
            passed = probe.connected
            measured = "PING OK" if passed else "PING no reply"
        elif test == VerificationTest.RUN_I2C_SCAN:
            want = list(step.expect_i2c) if step else []
            want_any = list(step.expect_i2c_any) if step else []
            missing = [a for a in want if a not in devices]
            missing_any = [g for g in want_any if not any(a in devices for a in g)]
            passed = probe.connected and not missing and not missing_any
            measured = "I2C found [%s]" % (", ".join(devices) or "none")
            if missing or missing_any:
                measured += " MISSING %s" % (missing + ["|".join(g) for g in missing_any])
        elif test == VerificationTest.READ_SENSOR:
            t = probe.temperature_c
            passed = t is not None and 10.0 <= t <= 40.0
            measured = ("T=%.1fC H=%.1f%% P=%.1fhPa" % (
                t, probe.humidity_pct or 0.0, probe.pressure_hpa or 0.0)) if t is not None \
                else "sensor read failed"
        elif test == VerificationTest.CHECK_OLED:
            passed = probe.oled_ok is True
            measured = "OLED lit" if passed else "OLED dark or unreachable"
        elif test == VerificationTest.BLINK_LED:
            passed = probe.led_ok is True
            measured = "LED blinks on GPIO %d" % self.graph.led_gpio if passed \
                else "LED did not blink"
        elif test == VerificationTest.SELFTEST:
            sub = [
                ("PING", probe.connected),
                ("I2C", self._i2c_ok(step, devices) if step else bool(devices)),
                ("OLED", probe.oled_ok is True),
                ("SENSOR", probe.temperature_c is not None),
                ("LED", probe.led_ok is True),
            ]
            passed = all(ok for _, ok in sub)
            measured = "SELFTEST " + " ".join("%s:%s" % (n, "OK" if ok else "FAIL") for n, ok in sub)
        else:  # NONE or unknown: nothing measured, nothing proven
            passed, measured = False, "no verification test ran (cannot verify)"
        return {"test": test.value, "passed": passed, "measured": measured, "at": _now()}

    @staticmethod
    def _i2c_ok(step: AssemblyStep, devices: list[str]) -> bool:
        if any(a not in devices for a in step.expect_i2c):
            return False
        return all(any(a in devices for a in group) for group in step.expect_i2c_any)

    # -- guided assembly finish -------------------------------------------
    def _finish_guided(self, decision, verification, probe, sam_result,
                       frame_settled, camera_source) -> dict:
        assert self.session is not None
        idx = self.session["step_index"]
        step = self.graph.get_step(idx)
        checks: list[StepCheck] = self.session["checklist"]

        if verification["passed"] and step is not None:
            checks[idx].status = "pass"
            checks[idx].measured = verification["measured"]
            checks[idx].updated_at = _now()
            if decision.fault_hypothesis and decision.fault_hypothesis in self.session["faults_found"]:
                pass  # fault cleared by a passing re-test
            idx += 1
            self.session["step_index"] = idx
            nxt = self.graph.get_step(idx)
            if nxt is None:
                self.session["completed_at"] = _now()
                speak = "Assembly complete. All %d steps verified." % self.graph.total_steps
                self.voice.speak(speak)
                return self._turn_view(AgentState.COMPLETE, decision, verification, probe,
                                       sam_result, frame_settled, camera_source,
                                       instruction="Assembly complete. Passport issued below.",
                                       speak=speak)
            speak = "Step passed. Next: %s" % nxt.instruction_student
            self.voice.speak(speak)
            return self._turn_view(AgentState.INSTRUCTING, decision, verification, probe,
                                   sam_result, frame_settled, camera_source,
                                   instruction=nxt.instruction_student, speak=speak)

        # Failed verification: record the fault, stay on this step.
        if decision.fault_hypothesis and decision.fault_hypothesis not in self.session["faults_found"]:
            self.session["faults_found"].append(decision.fault_hypothesis)
        if step is not None:
            checks[idx].status = "fail"
            checks[idx].measured = verification["measured"]
            checks[idx].updated_at = _now()
        speak = decision.speak_to_tech
        self.voice.speak(speak)
        return self._turn_view(AgentState.FAULT_FOUND, decision, verification, probe,
                               sam_result, frame_settled, camera_source,
                               instruction=decision.next_instruction, speak=speak)

    def _diagnostic_terminal(self, probe, sam_result) -> tuple:
        """End criteria: READ_SENSOR and CHECK_OLED both pass (vs known-good)."""
        ref = self.graph.steps[-1]
        sensor = self._run_test(VerificationTest.READ_SENSOR, ref, probe, sam_result)
        oled = self._run_test(VerificationTest.CHECK_OLED, ref, probe, sam_result)
        ok = sensor["passed"] and oled["passed"]
        return ok, {"READ_SENSOR": sensor, "CHECK_OLED": oled}

    def _finish_diagnostic(self, decision, verification, probe, sam_result,
                           frame_settled, camera_source, ranked) -> dict:
        assert self.session is not None
        terminal_ok, terminal = self._diagnostic_terminal(probe, sam_result)
        top = ranked[0] if ranked else None
        hypothesis = None
        if top is not None and (top["score"] >= RANK_THRESHOLD or not terminal_ok):
            hypothesis = top["fault_id"]
        prev = self.session.get("current_hypothesis")
        self.session["current_hypothesis"] = hypothesis
        if hypothesis is not None and hypothesis not in self.session["faults_found"]:
            self.session["faults_found"].append(hypothesis)

        if terminal_ok and hypothesis is None:
            self.session["completed_at"] = _now()
            self.voice.speak(decision.speak_to_tech)
            return self._turn_view(AgentState.VERIFIED_FIXED, decision, verification, probe,
                                   sam_result, frame_settled, camera_source,
                                   instruction=decision.next_instruction,
                                   speak=decision.speak_to_tech,
                                   ranking=ranked, terminal=terminal)

        # Guide exactly one fix. If the re-test disproved the previous
        # hypothesis, say so and move to the next ranked fault.
        if hypothesis is not None and decision.fault_hypothesis != hypothesis:
            hint = MetaClaudeAdapter._fix_hint(hypothesis)
            decision = decision.model_copy(update={
                "state": AgentState.FAULT_FOUND,
                "fault_hypothesis": hypothesis,
                "evidence": ["end criteria not met: READ_SENSOR=%s CHECK_OLED=%s" % (
                    "PASS" if terminal["READ_SENSOR"]["passed"] else "FAIL",
                    "PASS" if terminal["CHECK_OLED"]["passed"] else "FAIL"),
                ] + list(top["evidence"] if top else []),
                "next_instruction": hint,
                "speak_to_tech": hint,
            })
        if prev is not None and hypothesis != prev:
            decision = decision.model_copy(update={
                "evidence": ["hypothesis %s disproven by re-verification; pursuing %s"
                             % (prev, hypothesis)] + list(decision.evidence),
            })
        self.voice.speak(decision.speak_to_tech)
        return self._turn_view(AgentState.FAULT_FOUND, decision, verification, probe,
                               sam_result, frame_settled, camera_source,
                               instruction=decision.next_instruction,
                               speak=decision.speak_to_tech,
                               ranking=ranked, terminal=terminal)

    # -- views --------------------------------------------------------------
    def _turn_view(self, state, decision, verification, probe, sam_result,
                   frame_settled, camera_source, instruction, speak,
                   ranking=None, terminal=None) -> dict:
        assert self.session is not None
        view = self._session_view(state=state, instruction=instruction, speak=speak)
        view.update({
            "decision": decision.model_dump(),
            "verification": verification,
            "probe": _probe_summary(probe),
            "sam": {"detections": sam_result.get("detections", []),
                    "notes": sam_result.get("notes", ""),
                    "mock": sam_result.get("mock", False),
                    "error": sam_result.get("error", "")},
            "frame_settled": frame_settled,
            "camera_source": camera_source,
        })
        if ranking is not None:
            view["ranking"] = ranking
        if terminal is not None:
            view["terminal"] = terminal
        return view

    def _session_view(self, state: AgentState, instruction: str, speak: str) -> dict:
        assert self.session is not None
        s = self.session
        step = self.graph.get_step(s["step_index"])
        checks = [c.model_dump() for c in s["checklist"]]
        passport = None
        attempts = list(s.get("attempts", []))
        if state in (AgentState.COMPLETE, AgentState.VERIFIED_FIXED):
            passport = self._build_passport(s).model_dump()
        return {
            "state": state.value,
            "mode": s["mode"].value if isinstance(s["mode"], Mode) else str(s["mode"]),
            "step_index": s["step_index"],
            "step": step.model_dump() if step else None,
            "instruction": instruction,
            "speak": speak,
            "checklist": checks,
            "faults_found": list(s["faults_found"]),
            "passport": passport,
            "turns": s["turns"],
            "attempts": attempts,
            "attempts_total": len(attempts),
            "current_hypothesis": s.get("current_hypothesis"),
            "baseline": s.get("baseline") or None,
            "history": list(s.get("history", [])),
            "started_at": s["started_at"],
            "id": s.get("id"),
            "demo_mode": self.demo_mode,
            "fallbacks": list(s.get("last_fallbacks", [])),
            "fallback_active": bool(s.get("last_fallbacks")),
        }
