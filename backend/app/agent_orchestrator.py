"""Agent orchestrator: the Sense -> Reason -> Instruct -> Verify loop.

CRITICAL RULE: Muse never declares success alone. On every turn the
orchestrator runs the verification_test itself (SAM vision + serial probe)
and the sensor result decides pass/fail, step advance, and completion.

REAL-MODE RULE: camera and Meta API failures propagate as errors (the API
returns HTTP 502 and the step stops). Nothing is silently replaced with
mock data or a canned instruction.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ATTEMPT_LOG = Path(__file__).resolve().parent.parent / "logs" / "attempts.jsonl"
GENESIS_HASH = "0" * 64

from .adapters.muse_adapter import MetaClaudeAdapter
from .adapters.sam_adapter import MetaSAMAdapter
from .adapters.voice_adapter import MetaVoiceAdapter
from .camera_client import CameraClient
from .change_detector import MotionSettler
from .circuit_graph import CircuitGraph, load_circuit_graph
from .fault_ranker import RANK_THRESHOLD, rank_faults
from .fundi import chapter_for, plan_chapters, speak_passed, speak_start
from .models import (
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


def _settle_seconds() -> float:
    """Motion-settle gate, tunable via SETTLE_SECONDS (default 1.0s)."""
    try:
        return max(0.0, float(os.environ.get("SETTLE_SECONDS", "1.0")))
    except ValueError:
        return 1.0


def _settle_max_wait() -> float:
    """Cap for the settle poll, tunable via SETTLE_MAX_WAIT_S (default 3.0s)."""
    try:
        return max(0.2, float(os.environ.get("SETTLE_MAX_WAIT_S", "3.0")))
    except ValueError:
        return 3.0


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
        self.settler = settler or MotionSettler(settle_seconds=_settle_seconds())
        self.settle_max_wait_s = _settle_max_wait()
        self.probe = probe or SerialProbe()
        self.sam = sam or MetaSAMAdapter()
        self.Muse = Muse or MetaClaudeAdapter()
        self.voice = voice or MetaVoiceAdapter()
        self.attempt_log_path = Path(attempt_log_path) if attempt_log_path else DEFAULT_ATTEMPT_LOG
        self.sessions: dict[str, dict] = {}
        self.current_id: str | None = None
        self.session: dict | None = None

    # -- session --------------------------------------------------------
    def start_session(self, mode: Mode = Mode.GUIDED_ASSEMBLY,
                      board: str | None = None) -> dict:
        from .boards import GUIDED_BOARDS, board_key

        key = board_key(board)
        if key not in GUIDED_BOARDS:
            raise ValueError(
                "guided assembly and diagnostic modes support the ESP32 kit "
                "only; use Learning mode for %s." % key)
        self.settler.reset()
        session_id = uuid.uuid4().hex[:8]
        self.session = {
            "id": session_id,
            "mode": mode,
            "board": key,
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
            technician_confirmed=sum(1 for a in attempts if a.get("confirmed_by")),
        )

    def _capture_baseline(self) -> str:
        """Diagnostic start: SELFTEST + I2C scan + camera frame as baseline."""
        assert self.session is not None
        with ThreadPoolExecutor(max_workers=2,
                                thread_name_prefix="fundiloop-baseline") as ex:
            probe_fut = ex.submit(self.probe.read_all)
            frame, settled, source, frame_file = self._capture_settled_frame()
            sam_fut = ex.submit(
                lambda: self.sam.analyze(
                    frame, context={"frame_source": source},
                    frame_path=frame_file or None))
            sam0 = sam_fut.result()
            probe0 = probe_fut.result()
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
        if self.session.get("learning"):
            return self._learning_status_view()
        step = self.graph.get_step(self.session["step_index"])
        done = all(c.status == "pass" for c in self.session["checklist"])
        state = AgentState.COMPLETE if done and self.session["mode"] == Mode.GUIDED_ASSEMBLY \
            else AgentState.INSTRUCTING
        current = step.instruction_student if step else "Complete."
        return self._session_view(state=state, instruction=current, speak=current)

    # -- learning mode (generated plans, sensor-gated steps) ------------
    def start_learning_session(self, project_id: str, plan: list,
                               visible_parts: list | None = None,
                               session_id: str | None = None,
                               project_title: str = "",
                               board: str | None = None) -> dict:
        """Start (or restart) a learning session on a validated plan.

        Raises ValueError listing validator reasons when the plan is bad,
        or naming an unknown board.
        """
        from .boards import board_key, pin_map_for
        from .discovery import validate_project

        key = board_key(board)
        visible = [str(v) for v in (visible_parts or [])]
        steps = list(plan) if isinstance(plan, list) else []
        reasons = validate_project({"steps": steps}, visible) if steps else ["plan has no steps"]
        if reasons:
            raise ValueError("; ".join(reasons))
        sid = session_id or uuid.uuid4().hex
        self.sessions.pop(sid, None)
        self.session = {
            "id": sid,
            "mode": "LEARNING",
            "learning": True,
            "project_id": project_id,
            "project_title": project_title or project_id,
            "plan": [dict(s) for s in steps],
            "step_index": 0,
            "visible_parts": visible,
            "verified": {"i2c": [], "steps": []},
            "board": key,
            "pin_map": pin_map_for(key),
            "attempts": [],
            "checklist": [],
            "faults_found": [],
            "measurements_before": {},
            "measurements_after": {},
            "started_at": _now(),
            "completed_at": "",
            "turns": 0,
            "history": [],
            "current_hypothesis": None,
            "code_ready": False,
            "last_verification": None,
            "confirm_pending": None,
            "last_event_hash": GENESIS_HASH,
        }
        self.sessions[sid] = self.session
        self.current_id = sid
        chapters = plan_chapters(self.session["plan"], self.Muse,
                                 self.session["project_title"], key)
        self.session["chapters"] = chapters
        first = chapter_for(chapters, 0) or {}
        speak = speak_start(first, self.session["project_title"])
        return self._learning_view(
            state="INSTRUCTING", instruction=speak, speak=speak,
            verification=None,
            fundi={"event": "CHAPTER_START", "chapter": first})

    def _learning_turn(self, req, progress=None) -> dict:
        """One sensor-gated learning step. The sensor alone advances."""
        from .learning import build_hint, run_learning_verification

        assert self.session is not None
        self.session["turns"] += 1
        self.session["confirm_pending"] = None  # a fresh turn voids stale asks
        t0 = time.monotonic()
        timings: dict[str, float] = {"sam_ms": 0.0, "glance_ms": 0.0,
                                     "reason_ms": 0.0}
        if progress is not None:
            progress("sense", {})

        def done(view: dict) -> dict:
            timings["total_ms"] = (time.monotonic() - t0) * 1000
            view["timings"] = {k: round(v, 1) for k, v in timings.items()}
            if progress is not None:
                progress("reason", {})
                progress("verify", {})
                progress("done", {"timings": view["timings"]})
            return view

        plan = self.session["plan"]
        idx = self.session["step_index"]
        if idx >= len(plan):
            return done(self._learning_view(
                state="DONE",
                instruction="Project verified. Your project code is below.",
                speak="Project verified. Your project code is shown below.",
                verification=self.session.get("last_verification"),
                fundi={"event": "LESSON_DONE", "chapter": None,
                       "chapters_total": len(plan)}))
        step = plan[idx]
        probe_t0 = time.monotonic()
        probe = self.probe.read_all(simulate_fault=req.simulate_fault)
        timings["sense_ms"] = timings["probe_ms"] = \
            (time.monotonic() - probe_t0) * 1000
        summary = _probe_summary(probe)
        self.session["measurements_after"] = summary
        if not self.session["measurements_before"]:
            self.session["measurements_before"] = summary
        verification = run_learning_verification(
            step.get("verify", ""), step.get("expect", {}), probe,
            self.session["pin_map"])
        self.session["last_verification"] = verification
        seen = self.session["verified"]
        for addr in summary.get("i2c_devices", []):
            if addr not in seen["i2c"]:
                seen["i2c"].append(addr)
        if summary.get("temperature_c") is not None:
            seen["last_reading"] = {
                "temperature_c": summary.get("temperature_c"),
                "humidity_pct": summary.get("humidity_pct"),
                "pressure_hpa": summary.get("pressure_hpa"),
            }
        seen["steps"].append({"n": step.get("n", idx + 1),
                              "test": verification["test"],
                              "passed": verification["passed"],
                              "measured": verification["measured"]})
        chapters = self.session.get("chapters") or []
        current_ch = chapter_for(chapters, self.session["step_index"]) or {}
        if verification["passed"]:
            idx += 1
            self.session["step_index"] = idx
            if idx >= len(plan):
                self.session["completed_at"] = _now()
                self.session["code_ready"] = True
                speak = ("Boom, you finished it! Project verified: all "
                         "%d steps passed sensor checks. "
                         "Your project code is shown below." % len(plan))
                self.voice.speak(speak)
                if progress is not None:
                    progress("CHAPTER_PASSED", {"chapter": current_ch,
                                                "celebration": speak})
                return done(self._learning_view(
                    state="DONE",
                    instruction="Project verified. Your project code is below.",
                    speak=speak, verification=verification,
                    fundi={"event": "LESSON_DONE", "chapter": None,
                           "chapters_total": len(plan)}))
            praise = ["Nice work.", "Boom.", "Spot on.", "Well done.",
                        "Nailed it.", "Great.", "Let's go."][
                            self.session["turns"] % 7]
            next_ch = chapter_for(chapters, idx)
            text = speak_passed(current_ch, verification["measured"], next_ch,
                                self.session["project_title"], praise)
            self.voice.speak(text)
            if progress is not None:
                progress("CHAPTER_PASSED", {
                    "chapter": current_ch, "next_chapter": next_ch,
                    "celebration": str(current_ch.get("celebration") or praise)})
            return done(self._learning_view(
                state="INSTRUCTING", instruction=text, speak=text,
                verification=verification,
                fundi={"event": "CHAPTER_PASSED", "chapter": current_ch,
                       "next_chapter": next_ch,
                       "celebration": str(current_ch.get("celebration") or praise)}))
        if verification.get("needs_confirmation"):
            return done(self._offer_confirmation(step, verification))
        r_t0 = time.monotonic()
        hint = build_hint(step, verification, self.Muse)
        timings["reason_ms"] = (time.monotonic() - r_t0) * 1000
        speak = "%s. %s" % (verification["measured"], hint)
        self.voice.speak(speak)
        if progress is not None:
            progress("CHAPTER_HINT", {"chapter": current_ch, "hint": hint})
        return done(self._learning_view(
            state="VERIFYING", instruction=speak, speak=speak,
            verification=verification,
            fundi={"event": "CHAPTER_HINT", "chapter": current_ch,
                   "hint": hint}))

    def _offer_confirmation(self, step: dict, verification: dict) -> dict:
        """Ask the technician to confirm what the probe cannot assess.

        The step does NOT advance here; confirm_learning_step advances it
        with a hash-chained audit entry recording the attestation.
        """
        assert self.session is not None
        pending = {
            "n": step.get("n", self.session["step_index"] + 1),
            "test": verification.get("test", ""),
            "measured": verification.get("measured", ""),
            "question": ("I cannot verify this over serial: %s. "
                         "If you can see it working, press CONFIRM."
                         % verification.get("measured", "")),
        }
        self.session["confirm_pending"] = pending
        self.voice.speak(pending["question"])
        current_ch = chapter_for(self.session.get("chapters") or [],
                                 self.session["step_index"]) or {}
        return self._learning_view(
            state="VERIFYING", instruction=pending["question"],
            speak=pending["question"], verification=verification,
            fundi={"event": "CHAPTER_HINT", "chapter": current_ch,
                   "hint": pending["question"]})

    def confirm_learning_step(self, session_id: str, confirmed: bool) -> dict:
        """Advance a learning step on technician attestation.

        Only valid while confirm_pending is set (the probe reported the
        step unverifiable, not failed). Raises KeyError for an unknown or
        non-learning session, ValueError when nothing is pending.
        """
        s = self.sessions.get(session_id)
        if s is None or not s.get("learning"):
            raise KeyError("unknown learning session id")
        self.session = s
        self.current_id = session_id
        pending = s.get("confirm_pending")
        idx = s["step_index"]
        if not pending or idx >= len(s["plan"]):
            raise ValueError("no confirmation pending")
        step = s["plan"][idx]
        chapters = s.get("chapters") or []
        current_ch = chapter_for(chapters, idx) or {}
        if not confirmed:
            s["confirm_pending"] = None
            speak = "OK, not confirmed. Fix it up and press CHECK NOW."
            self.voice.speak(speak)
            return self._learning_view(
                state="VERIFYING", instruction=speak, speak=speak,
                verification=s.get("last_verification"),
                fundi={"event": "CHAPTER_HINT", "chapter": current_ch,
                       "hint": speak})
        s["confirm_pending"] = None
        verification = dict(s.get("last_verification") or {})
        verification["technician_confirmed"] = True
        s["last_verification"] = verification
        s["verified"]["steps"].append({
            "n": step.get("n", idx + 1),
            "test": verification.get("test", ""),
            "passed": True,
            "measured": verification.get("measured", ""),
            "confirmed_by": "technician"})
        self._log_learning_confirmation(s, step, verification)
        idx += 1
        s["step_index"] = idx
        if idx >= len(s["plan"]):
            s["completed_at"] = _now()
            s["code_ready"] = True
            speak = ("Boom, you finished it! Project verified: all "
                     "%d steps passed (%d confirmed "
                     "by you). Your project code is shown below."
                     % (len(s["plan"]), self._confirmed_count(s)))
            self.voice.speak(speak)
            return self._learning_view(
                state="DONE",
                instruction="Project verified. Your project code is below.",
                speak=speak, verification=verification,
                fundi={"event": "LESSON_DONE", "chapter": None,
                       "chapters_total": len(s["plan"])})
        next_ch = chapter_for(chapters, idx)
        text = "Confirmed. Next: %s" % speak_start(
            next_ch or {}, s.get("project_title", ""))
        if next_ch is not None and not str(next_ch.get("teach") or "").strip():
            text = "Confirmed. Next step %s: %s" % (
                next_ch.get("n", idx + 1),
                str(next_ch.get("task") or "").strip())
        self.voice.speak(text)
        return self._learning_view(
            state="INSTRUCTING", instruction=text, speak=text,
            verification=verification,
            fundi={"event": "CHAPTER_PASSED", "chapter": current_ch,
                   "next_chapter": next_ch,
                   "celebration": str(current_ch.get("celebration") or "Confirmed.")})

    @staticmethod
    def _confirmed_count(s: dict) -> int:
        return sum(1 for a in s.get("attempts", []) if a.get("confirmed_by"))

    def _log_learning_confirmation(self, s: dict, step: dict,
                                   verification: dict) -> None:
        """Hash-chained audit entry for a technician-confirmed step."""
        entry = {
            "seq": len(s["attempts"]) + 1,
            "at": _now(),
            "prev_hash": s.get("last_event_hash", GENESIS_HASH),
            "turn": s["turns"],
            "mode": "LEARNING",
            "step_id": "step-%s" % step.get("n", s["step_index"] + 1),
            "test": verification.get("test", ""),
            "passed": True,
            "measured": verification.get("measured", ""),
            "confirmed_by": "technician",
            "transcript": "",
        }
        entry["hash"] = _event_hash(entry)
        s["last_event_hash"] = entry["hash"]
        s["attempts"].append(entry)
        try:
            self.attempt_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.attempt_log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # logging must never break the loop

    def _learning_view(self, state: str, instruction: str, speak: str,
                       verification: dict | None,
                       fundi: dict | None = None) -> dict:
        s = self.session
        assert s is not None
        idx = s["step_index"]
        view = {
            "learning": True,
            "id": s["id"],
            "project_id": s["project_id"],
            "project_title": s.get("project_title", ""),
            "state": state,
            "mode": "LEARNING",
            "board": s.get("board", "esp32_devkit"),
            "step_index": idx,
            "steps_total": len(s["plan"]),
            "step": dict(s["plan"][idx]) if idx < len(s["plan"]) else None,
            "instruction": instruction,
            "speak": speak,
            "verification": verification,
            "probe": dict(s.get("measurements_after", {})),
            "verified": {k: (list(v) if isinstance(v, list) else v)
                         for k, v in s.get("verified", {}).items()},
            "turns": s["turns"],
            "code_ready": bool(s.get("code_ready")),
            "started_at": s["started_at"],
        }
        if s.get("confirm_pending"):
            view["confirm_pending"] = dict(s["confirm_pending"])
        if fundi is not None:
            view["fundi"] = fundi
        return view

    def _learning_status_view(self) -> dict:
        assert self.session is not None
        s = self.session
        idx = s["step_index"]
        done = idx >= len(s["plan"])
        if done:
            text = "Project verified."
            fundi = {"event": "LESSON_DONE", "chapter": None,
                     "chapters_total": len(s["plan"])}
        else:
            step = s["plan"][idx]
            text = "Step %s: %s" % (step.get("n", idx + 1), step.get("instruction", ""))
            fundi = {"event": "CHAPTER_START",
                     "chapter": chapter_for(s.get("chapters") or [], idx)}
        return self._learning_view(state="DONE" if done else "INSTRUCTING",
                                   instruction=text, speak=text,
                                   verification=s.get("last_verification"),
                                   fundi=fundi)

    # -- board handoff: any board gets guided, on the right mode --------
    def _detect_board_cached(self, frame: bytes) -> dict:
        """One cheap board glance per session; cached after the first turn."""
        from .prompts import (BOARD_GLANCE_SYSTEM, board_glance_prompt,
                              parse_board_glance)

        assert self.session is not None
        cached = self.session.get("detected_board")
        if isinstance(cached, dict) and cached.get("board"):
            return cached
        try:
            raw = self.Muse.complete_text(
                BOARD_GLANCE_SYSTEM, board_glance_prompt(),
                image_b64=base64.b64encode(frame).decode("ascii"))
        except Exception:
            raw = ""
        result = parse_board_glance(raw or "")
        self.session["detected_board"] = result
        return result

    def _detect_board_fresh(self, frame: bytes) -> dict:
        """Force one fresh board glance, refreshing the session cache."""
        assert self.session is not None
        self.session.pop("detected_board", None)
        return self._detect_board_cached(frame)

    @staticmethod
    def _i2c_signature(probe) -> tuple:
        return tuple(sorted(probe.i2c_devices or []))

    def _refresh_board_glance(self, frame: bytes, probe, glance: dict) -> dict:
        """Re-run the board glance when the I2C signature changed.

        The per-session cache still skips the call on steady benches; a
        changed bus (board swapped, module pulled) re-glances once on the
        current frame so the handoff follows the equipment on the bench.
        """
        assert self.session is not None
        signature = self._i2c_signature(probe)
        cached_sig = self.session.get("glance_i2c")
        if cached_sig is not None and tuple(cached_sig) != signature:
            glance = self._detect_board_fresh(frame)
        self.session["glance_i2c"] = list(signature)
        return glance

    def rescan_boards(self) -> dict:
        """Forget the cached board glance so the next turn re-detects."""
        if self.session is not None:
            self.session.pop("detected_board", None)
            self.session.pop("glance_i2c", None)
        return {"ok": True, "session_id": self.current_id}

    @staticmethod
    def _is_board_mismatch(glance: dict) -> bool:
        board = str(glance.get("board") or "none").strip().lower()
        try:
            conf = float(glance.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        return board not in ("", "none", "esp32", "esp32_devkit") and conf >= 0.6

    def _handoff_view(self, glance: dict, probe, sam_result: dict,
                      frame_settled: bool, camera_source: str,
                      frame_file: str = "") -> dict:
        """Route a non-ESP32 bench to Learning with its parts attached."""
        from .boards import BOARD_PROFILES

        raw = str(glance.get("board") or "none").strip().lower()
        board = raw if raw in BOARD_PROFILES else None
        seen = {"arduino_uno": "an Arduino Uno",
                "arduino_nano": "an Arduino Nano",
                "esp8266": "an ESP8266 board",
                "pico": "a Raspberry Pi Pico"}.get(raw, "a %s board" % raw)
        if board:
            text = ("I see %s on your bench. This mode only fits the ESP32 "
                    "kit — tap START LEARNING and I'll guide you on the %s "
                    "instead." % (seen, BOARD_PROFILES[raw]["label"]))
        else:
            text = ("I see %s on your bench, which has no profile yet. Tap "
                    "START LEARNING, pick the closest board, and I'll guide "
                    "you from what I can see." % seen)
        self.voice.speak(text)
        view = self._session_view(state=AgentState.INSTRUCTING, instruction=text,
                                  speak=text)
        parts = [p for p in (glance.get("parts") or []) if p and p != "unknown"]
        view.update({
            "probe": _probe_summary(probe),
            "sam": {"detections": sam_result.get("detections", []),
                    "notes": sam_result.get("notes", ""),
                    "mock": sam_result.get("mock", False),
                    "wires_ok": sam_result.get("wires_ok"),
                    "error": sam_result.get("error", "")},
            "verification": None,
            "decision": None,
            "handoff": {"board": board, "board_label": raw, "parts": parts,
                        "confidence": glance.get("confidence", 0.0)},
            "frame_settled": frame_settled,
            "camera_source": camera_source,
            "frame_file": frame_file,
        })
        return view

    # -- main loop: one technician turn -----------------------------------
    @staticmethod
    def _run_branch(fn, *args, **kwargs) -> tuple:
        """Run one sense branch, returning (result, own_duration_ms)."""
        t0 = time.monotonic()
        return fn(*args, **kwargs), (time.monotonic() - t0) * 1000

    def _sense_concurrent(self, req: CheckNowRequest) -> tuple:
        """Overlapped sense pass: probe reads while the camera settles.

        Returns (frame, settled, source, frame_file, sam, probe, glance,
        timings). Futures join in the old serial order (SAM, probe, glance)
        so error precedence is unchanged; the probe never raises in real
        mode (error replies are data), so a struggling board still yields
        an honest reading. A CameraError stops the step before any Meta
        call result is used, exactly as in the sequential loop.
        """
        timings: dict[str, float] = {}
        sense_t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=3,
                                thread_name_prefix="fundiloop-sense") as ex:
            probe_fut = ex.submit(self._run_branch, self.probe.read_all,
                                  simulate_fault=req.simulate_fault)
            frame, settled, source, frame_file = self._capture_settled_frame()
            timings["sense_ms"] = (time.monotonic() - sense_t0) * 1000
            sam_fut = ex.submit(
                self._run_branch, self.sam.analyze, frame,
                context={"simulate_fault": req.simulate_fault,
                         "frame_source": source},
                frame_path=frame_file or None)
            glance_fut = ex.submit(self._run_branch, self._detect_board_cached,
                                   frame)
            sam_result, timings["sam_ms"] = sam_fut.result()
            probe, timings["probe_ms"] = probe_fut.result()
            glance, timings["glance_ms"] = glance_fut.result()
        return (frame, settled, source, frame_file, sam_result, probe,
                glance, timings)

    @staticmethod
    def _finish_timings(timings: dict, turn_t0: float) -> dict:
        """Stamp total_ms and round every stage to 0.1ms for the view."""
        timings["total_ms"] = (time.monotonic() - turn_t0) * 1000
        return {k: round(v, 1) for k, v in timings.items()}

    def check_now(self, req: CheckNowRequest | None = None, progress=None) -> dict:
        """Run one Sense -> Reason -> Instruct -> Verify turn.

        The sense branches (camera still, SAM vision, serial probe, and the
        first-turn board glance) overlap in threads, so a turn costs the
        slowest branch instead of the sum. Every returned view carries
        per-stage `timings` in milliseconds.

        `progress`, when given, is called as progress(stage, detail) with
        stages "sense" -> "reason" -> "verify" -> "done".

        Raises CameraError/MetaAPIError on camera or Meta API failure: the
        API returns the real error and the step stops. Nothing is faked.
        """
        req = req or CheckNowRequest()
        if self.session is None:
            self.start_session()
        assert self.session is not None
        if self.session.get("learning"):
            return self._learning_turn(req, progress=progress)

        def emit(stage: str, detail: dict | None = None) -> None:
            if progress is not None:
                progress(stage, detail or {})

        mode = self.session["mode"]
        guided = mode == Mode.GUIDED_ASSEMBLY
        step = self.graph.get_step(self.session["step_index"]) if guided else None
        # Diagnostic has no assembly step: the final graph step is the
        # known-good reference that every test is checked against.
        ref_step = step if guided else self.graph.steps[-1]
        self.session["turns"] += 1
        turn_t0 = time.monotonic()

        # 1. SENSE: settled camera frame (saved to debug_frames) + SAM vision
        #    + serial probe, overlapped. Camera/SAM failures raise and stop
        #    the step.
        emit("sense")
        (frame, frame_settled, camera_source, frame_file, sam_result, probe,
         glance, timings) = self._sense_concurrent(req)
        if not self.session["measurements_before"]:
            self.session["measurements_before"] = _probe_summary(probe)
        self.session["measurements_after"] = _probe_summary(probe)

        # 1b. BOARD CHECK (guided/diagnostic fit the ESP32 kit only): the
        # glance ran with the sense branches and is cached while the I2C
        # signature is steady; a changed bus re-glances once. Any other
        # board hands off to Learning instead of demanding an ESP32 swap.
        glance = self._refresh_board_glance(frame, probe, glance)
        if self._is_board_mismatch(glance):
            view = self._handoff_view(glance, probe, sam_result, frame_settled,
                                      camera_source, str(frame_file or ""))
            view["timings"] = self._finish_timings(timings, turn_t0)
            self._append_history(req.transcript, view.get("speak", ""))
            emit("done", {"timings": view["timings"]})
            return view

        # 2. REASON: Muse proposes (advisory only). API errors raise.
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
            "frame_file": str(frame_file or ""),
            "frame_jpeg_b64": base64.b64encode(frame).decode("ascii"),
        }
        if not guided:
            ctx["ranked"] = rank_faults(probe, sam_result, req.code_snippet, self.graph)
        emit("reason")
        reason_t0 = time.monotonic()
        decision = self.Muse.decide(ctx)
        timings["reason_ms"] = (time.monotonic() - reason_t0) * 1000

        # 3. VERIFY: the sensor decides, not Muse.
        emit("verify")
        verification = self._run_test(decision.verification_test, ref_step, probe, sam_result)

        # 4. On guided failure, Muse takes a corrective second pass grounded
        # in the failed test's measured evidence; the step does not advance.
        if guided and step is not None and not verification["passed"]:
            second_t0 = time.monotonic()
            decision = self.Muse.decide({**ctx, "verification": verification})
            timings["reason_ms"] += (time.monotonic() - second_t0) * 1000

        self._log_attempt(step, decision, verification, req.transcript,
                          frame_settled, camera_source, str(frame_file or ""))
        if not guided:
            view = self._finish_diagnostic(decision, verification, probe, sam_result,
                                           frame_settled, camera_source,
                                           ctx["ranked"], str(frame_file or ""))
        else:
            view = self._finish_guided(decision, verification, probe, sam_result,
                                       frame_settled, camera_source, str(frame_file or ""))
        view["timings"] = self._finish_timings(timings, turn_t0)
        self._append_history(req.transcript, view.get("speak", ""))
        emit("done", {"timings": view["timings"]})
        return view

    def describe_view(self) -> dict:
        """Look at the current frame and describe it in plain words.

        Real mode: one SAM call for regions, then Muse reads the frame
        itself and describes whatever equipment is actually there — no
        fixed part list, no canned verdict. Mock mode keeps the
        label-based text (the mock cannot read pixels).
        """
        frame, settled, source, frame_file = self._capture_settled_frame()
        sam = self.sam.analyze(frame, context={"frame_source": source},
                               frame_path=frame_file or None)
        if sam.get("mock"):
            description = self._describe_sam(sam)
        else:
            description = self._describe_with_claude(frame, sam)
        self.voice.speak(description)
        return {"description": description, "sam": {
            "detections": sam.get("detections", []), "notes": sam.get("notes", ""),
            "mock": sam.get("mock", False), "wires_ok": sam.get("wires_ok"),
            "error": sam.get("error", "")},
            "camera_source": source, "frame_settled": settled,
            "frame_file": str(frame_file or "")}

    def _describe_with_claude(self, frame: bytes, sam: dict) -> str:
        """Generative description: Muse's own words about this frame."""
        from .prompts import DESCRIBE_SYSTEM, describe_scene_prompt

        text = self.Muse.complete_text(
            DESCRIBE_SYSTEM, describe_scene_prompt(sam.get("detections", [])),
            image_b64=base64.b64encode(frame).decode("ascii")).strip()
        if not text:
            return "Muse returned an empty description for this frame."
        return text

    @staticmethod
    def _describe_sam(sam: dict) -> str:
        if sam.get("error"):
            return "Vision is unavailable right now: %s" % sam["error"]
        labels = [d.get("label", "?") for d in sam.get("detections", [])]
        notes = sam.get("notes", "")
        if not labels:
            text = "I can't make out any known components on the bench."
            return text + (" " + notes if notes else "")
        text = "I see: " + ", ".join(labels) + "."
        if sam.get("wires_ok") is False and notes:
            text += " But the wiring looks suspect: " + notes
        if sam.get("mock"):
            text = "Simulated view: " + text
        return text

    def transcribe(self, audio: bytes, filename: str = "talk.webm") -> dict:
        """Transcribe push-to-talk audio. MetaAPIError propagates."""
        return self.voice.transcribe(audio, filename)

    def _append_history(self, transcript: str, speak: str) -> None:
        assert self.session is not None
        hist = self.session["history"]
        hist.append({"role": "user", "content": (transcript or "").strip() or "(pressed CHECK NOW)"})
        hist.append({"role": "assistant", "content": speak})
        del hist[:-20]  # keep the last 10 turns

    def _capture_settled_frame(self, max_wait_s: float | None = None) -> tuple:
        """Wait for a settled preview frame, then capture one saved still.

        Polls the cheap preview feed until the change detector settles, then
        takes exactly one analysis still via capture_analysis_frame() (which
        validates the JPEG and saves it to debug_frames/). Returns
        (frame, settled, source, frame_path). Raises CameraError when the
        camera cannot provide frames, so the step stops instead of faking.
        """
        assert self.session is not None
        if max_wait_s is None:
            max_wait_s = self.settle_max_wait_s
        session_id = self.session.get("id", "")
        frame = self.camera.get_latest_preview_frame()
        source = self.camera.describe_source()
        clean = self.settler.update(frame)
        if clean is None:
            deadline = time.monotonic() + max_wait_s
            while time.monotonic() < deadline:
                time.sleep(0.2)
                frame = self.camera.get_latest_preview_frame()
                clean = self.settler.update(frame)
                if clean is not None:
                    break
        settled = clean is not None
        still, saved_path = self.camera.capture_analysis_frame(session_id)
        return still, settled, source, saved_path

    def _log_attempt(self, step, decision, verification, transcript,
                     frame_settled, camera_source, frame_file: str = "") -> None:
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
            "frame_file": frame_file,
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
                       frame_settled, camera_source, frame_file: str = "") -> dict:
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
                speak = "Assembly complete: %s. All %d steps verified." % (
                    verification["measured"], self.graph.total_steps)
                self.voice.speak(speak)
                return self._turn_view(AgentState.COMPLETE, decision, verification, probe,
                                       sam_result, frame_settled, camera_source,
                                       instruction="Assembly complete. Passport issued below.",
                                       speak=speak, frame_file=frame_file)
            text = "Verified: %s. Next: %s" % (verification["measured"],
                                               nxt.instruction_student)
            self.voice.speak(text)
            return self._turn_view(AgentState.INSTRUCTING, decision, verification, probe,
                                   sam_result, frame_settled, camera_source,
                                   instruction=text, speak=text, frame_file=frame_file)

        # Failed verification: record the fault, stay on this step.
        if decision.fault_hypothesis and decision.fault_hypothesis not in self.session["faults_found"]:
            self.session["faults_found"].append(decision.fault_hypothesis)
        if step is not None:
            checks[idx].status = "fail"
            checks[idx].measured = verification["measured"]
            checks[idx].updated_at = _now()
        instruction = "%s. %s" % (verification["measured"], decision.next_instruction)
        speak = "%s. %s" % (verification["measured"], decision.speak_to_tech)
        self.voice.speak(speak)
        return self._turn_view(AgentState.FAULT_FOUND, decision, verification, probe,
                               sam_result, frame_settled, camera_source,
                               instruction=instruction, speak=speak, frame_file=frame_file)

    def _diagnostic_terminal(self, probe, sam_result) -> tuple:
        """End criteria: READ_SENSOR and CHECK_OLED both pass (vs known-good)."""
        ref = self.graph.steps[-1]
        sensor = self._run_test(VerificationTest.READ_SENSOR, ref, probe, sam_result)
        oled = self._run_test(VerificationTest.CHECK_OLED, ref, probe, sam_result)
        ok = sensor["passed"] and oled["passed"]
        return ok, {"READ_SENSOR": sensor, "CHECK_OLED": oled}

    def _finish_diagnostic(self, decision, verification, probe, sam_result,
                           frame_settled, camera_source, ranked,
                           frame_file: str = "") -> dict:
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
                                   ranking=ranked, terminal=terminal, frame_file=frame_file)

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
        lead = "%s. Suspect %s." % (verification["measured"], hypothesis) \
            if hypothesis else verification["measured"] + "."
        instruction = "%s %s" % (lead, decision.next_instruction)
        speak = "%s %s" % (lead, decision.speak_to_tech)
        self.voice.speak(speak)
        return self._turn_view(AgentState.FAULT_FOUND, decision, verification, probe,
                               sam_result, frame_settled, camera_source,
                               instruction=instruction, speak=speak,
                               ranking=ranked, terminal=terminal, frame_file=frame_file)

    # -- views --------------------------------------------------------------
    def _turn_view(self, state, decision, verification, probe, sam_result,
                   frame_settled, camera_source, instruction, speak,
                   ranking=None, terminal=None, frame_file: str = "") -> dict:
        assert self.session is not None
        view = self._session_view(state=state, instruction=instruction, speak=speak)
        view.update({
            "decision": decision.model_dump(),
            "verification": verification,
            "probe": _probe_summary(probe),
            "sam": {"detections": sam_result.get("detections", []),
                    "notes": sam_result.get("notes", ""),
                    "mock": sam_result.get("mock", False),
                    "wires_ok": sam_result.get("wires_ok", True),
                    "error": sam_result.get("error", "")},
            "frame_settled": frame_settled,
            "camera_source": camera_source,
            "frame_file": frame_file,
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
            "board": s.get("board", "esp32_devkit"),
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
        }
