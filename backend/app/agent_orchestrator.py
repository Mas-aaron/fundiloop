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
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ATTEMPT_LOG = Path(__file__).resolve().parent.parent / "logs" / "attempts.jsonl"
GENESIS_HASH = "0" * 64
_log = logging.getLogger(__name__)

from .adapters.muse_adapter import MetaClaudeAdapter
from .adapters.sam_adapter import MetaSAMAdapter
from .adapters.voice_adapter import MetaVoiceAdapter
from .camera_client import CameraClient
from .change_detector import MotionSettler
from .circuit_graph import CircuitGraph, load_circuit_graph
from .fault_ranker import RANK_THRESHOLD, rank_faults
from .fundi import (
    chapter_for,
    complete_rollcall,
    next_step_opening,
    plan_chapters,
    plan_stretch,
    rollcall_accepts,
    speak_start,
    stretch_fallback,
    template_chapters,
)
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
        or naming an unknown board. Part availability never blocks the
        start: the user wires what the plan names, and the sensor
        decides each step honestly.
        """
        from .boards import board_key, pin_map_for
        from .discovery import validate_project

        key = board_key(board)
        visible = [str(v) for v in (visible_parts or [])]
        steps = list(plan) if isinstance(plan, list) else []
        reasons = validate_project({"steps": steps}, visible,
                                   enforce_visible=False) if steps else ["plan has no steps"]
        if reasons:
            raise ValueError("; ".join(reasons))
        # Canonical numbering: every consumer (speech, chapters,
        # schematic, guide, flash) keys steps by n, so the model never
        # gets to number them 0-based, gapped, or out of order.
        steps = [dict(s) if isinstance(s, dict) else s for s in steps]
        for i, step in enumerate(steps):
            if isinstance(step, dict):
                step["n"] = i + 1
        # One pin truth: spoken/written GPIOs are corrected to the
        # schematic allocator's assignment before anything narrates.
        from .schematic import repair_plan_pins

        steps = repair_plan_pins(steps, pin_map_for(key))
        sid = session_id or uuid.uuid4().hex
        self.sessions.pop(sid, None)
        # Parts the plan names that the bench never resolved: the roll
        # call reads them aloud, ordered, before chapter 1 begins.
        wanted: list[str] = []
        for st in steps:
            if not isinstance(st, dict):
                continue
            for dep in (st.get("depends_on_parts") or []):
                dep = str(dep).strip()
                if dep and dep not in wanted:
                    wanted.append(dep)
        missing = [d for d in wanted if d not in visible]
        self.session = {
            "id": sid,
            "mode": "LEARNING",
            "learning": True,
            "project_id": project_id,
            "project_title": project_title or project_id,
            "plan": [dict(s) for s in steps],
            "step_index": 0,
            "visible_parts": visible,
            "missing_parts": missing,
            "rollcall_done": False,
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
            "last_event_hash": GENESIS_HASH,
        }
        self.sessions[sid] = self.session
        self.current_id = sid
        # Firmware warms in the background from the tap: by the time
        # the learner opens the Code tab or deploys, checked firmware
        # is already cached (failures stay silent here; the fetch
        # retries honestly on demand).
        self._start_firmware_warmup(self.session)
        # Instant start: the parts roll call answers now with zero
        # model calls on the critical path, while a proven-real adapter
        # enriches every chapter in the background and swaps the full
        # set when it lands. READY begins chapter 1 (answer_rollcall).
        self.session["chapters"] = template_chapters(self.session["plan"])
        self.session["rollcall"] = complete_rollcall(
            None, visible, self.session["missing_parts"])
        self.session["stretch"] = stretch_fallback(
            self.session["project_title"], self.session["plan"])
        if not getattr(self.Muse, "mock", True):
            worker = threading.Thread(
                target=self._fill_chapters,
                args=(sid, self.session, self.session["plan"],
                      self.session["project_title"], key),
                name="fundiloop-chapters", daemon=True)
            worker.start()
            if len(self.session["plan"]) == 1:
                # Single-step builds never arrive anywhere else: warm
                # the DONE stretch goals now.
                self._start_stretch_prefetch()
        return self._rollcall_view(self.session)

    def _rollcall_view(self, s: dict) -> dict:
        """The parts roll call: read the bench aloud, wait for READY."""
        self.session = s
        rc = s.get("rollcall") or {}
        say = str(rc.get("say") or
                  "Say READY when your parts are on the bench.")
        return self._learning_view(
            state="ROLLCALL", instruction=say, speak=say,
            verification=None,
            fundi={"event": "ROLLCALL", "rollcall": {
                "say": say, "question": str(rc.get("question") or ""),
                "answers": [list(a) for a in (rc.get("answers") or [])]}})

    def _begin_chapters(self, s: dict) -> dict:
        """CHAPTER_START for the current step (after the READY gate)."""
        self.session = s
        idx = s["step_index"]
        first = chapter_for(s.get("chapters") or [], idx) or {}
        speak = speak_start(first, s.get("project_title", ""))
        return self._learning_view(
            state="INSTRUCTING", instruction=speak, speak=speak,
            verification=None,
            fundi={"event": "CHAPTER_START", "chapter": first})

    def answer_rollcall(self, session_id: str, transcript: str = "") -> dict:
        """Answer the parts roll call: READY begins chapter 1, anything
        else re-prompts without advancing. Raises KeyError for an
        unknown or non-learning session."""
        s = self.sessions.get(session_id)
        if s is None or not s.get("learning"):
            raise KeyError("unknown learning session id")
        self.session = s
        self.current_id = session_id
        if s.get("rollcall_done"):
            return self._learning_status_view()
        rc = s.get("rollcall") or {}
        if rollcall_accepts(transcript or "", rc.get("answers") or []):
            s["rollcall_done"] = True
            return self._begin_chapters(s)
        say = ("Take your time — say READY when everything is on the "
               "bench.")
        view = self._rollcall_view(s)
        view["instruction"] = view["speak"] = say
        return view

    def _fill_chapters(self, sid: str, session: dict, plan: list,
                       project_title: str, board: str,
                       Muse=None) -> None:
        """Background worker: swap template chapters for full Fundi ones.

        Runs off the request path after an instant template start. The
        swap lands only if this is still the live session object;
        anything unexpected leaves the templates standing (the adapter
        log shows the attempt).
        """
        try:
            full = plan_chapters(plan, Muse or MetaClaudeAdapter(),
                                 project_title, board)
            from .schematic import repair_chapters_pins

            full = repair_chapters_pins(full, plan,
                                        session.get("pin_map"))
        except Exception:
            _log.exception("fundi chapter fill failed; templates stand")
            return
        try:
            if self.sessions.get(sid) is session:
                session["chapters"] = full
        except Exception:
            pass

    def _prefetch_stretch(self) -> None:
        """Model-author the DONE stretch goals ahead of need.

        Overwrites the fallback stretch computed at start. Never
        raises; mock brains skip (the fallback already stands).
        """
        try:
            s = self.session
            if s is None or getattr(self.Muse, "mock", True):
                return
            s["stretch"] = plan_stretch(
                s.get("project_title", ""), s.get("plan") or [],
                s.get("visible_parts") or [], s.get("missing_parts") or [],
                self.Muse, s.get("board"))
        except Exception:
            pass

    def _start_stretch_prefetch(self) -> None:
        """Fire _prefetch_stretch on a daemon thread (off the request)."""
        worker = threading.Thread(
            target=self._prefetch_stretch,
            name="fundiloop-stretch", daemon=True)
        worker.start()

    def _warm_firmware(self, session: dict) -> None:
        """Generate the session's firmware ahead of need (worker).

        Runs off the request path after session start and every step
        advance. generate_code applies every gate (usable output,
        telemetry contract) and caches only passing firmware; any
        failure leaves the cache empty and the on-demand fetch stays
        the honest path (it retries and surfaces the error then).
        """
        try:
            from .adapters import MetaAPIError
            from .learning import generate_code

            generate_code(session, self.Muse)
        except MetaAPIError as exc:
            # Routine model failure (timeout, stub): one line, no
            # traceback flood — the on-demand fetch retries honestly.
            _log.warning("firmware warmup failed (%s); on-demand fetch stands",
                         exc)
        except Exception:
            _log.exception("firmware warmup failed; on-demand fetch stands")
        finally:
            try:
                session["_warming"] = False
            except Exception:
                pass

    def _start_firmware_warmup(self, session: dict) -> None:
        """Fire _warm_firmware on a daemon thread (off the request).

        One warmer per session at a time; it caches under the key it
        computes, so a step taken mid-warm simply refetches on demand.
        """
        if session.get("_warming"):
            return
        session["_warming"] = True
        worker = threading.Thread(
            target=self._warm_firmware, args=(session,),
            name="fundiloop-firmware", daemon=True)
        worker.start()

    def advance_learning_step(self, session_id: str,
                              transcript: str = "") -> dict:
        """Advance because the learner said they finished this step.

        Learning is learner-paced: saying done / next moves on; only
        telemetry proof (or the explicit escape) finishes the build.
        Raises KeyError for an unknown or non-learning session.
        """
        s = self.sessions.get(session_id)
        if s is None or not s.get("learning"):
            raise KeyError("unknown learning session id")
        self.session = s
        self.current_id = session_id
        if not s.get("rollcall_done", True):
            return self._rollcall_view(s)
        idx = s["step_index"]
        if idx >= len(s["plan"]):
            return self._learning_status_view()
        heard = str(transcript or "").strip()
        if idx == len(s["plan"]) - 1 and "mark complete" not in heard.lower():
            # Final step: taps advance steps, only telemetry proof (or
            # the explicit escape) finishes the build. A "next" here
            # holds with guidance instead of completing unverified.
            speak = ("This is the last step — tap Deploy Firmware to "
                     "flash your code, then prove it works in the monitor.")
            self.voice.speak(speak)
            chapters = s.get("chapters") or []
            return self._learning_view(
                state="INSTRUCTING", instruction=speak, speak=speak,
                verification=s.get("last_verification"),
                fundi={"event": "CHAPTER_HINT",
                       "chapter": chapter_for(chapters, idx),
                       "hint": speak})
        step = s["plan"][idx]
        chapters = s.get("chapters") or []
        current_ch = chapter_for(chapters, idx) or {}
        verification = {
            "test": "LEARNER_DONE",
            "passed": True,
            "measured": heard or "you said this step is done",
            "technician_confirmed": True,
        }
        praise = ["Nice work.", "Boom.", "Spot on.", "Well done.",
                  "Nailed it.", "Great.", "Let's go."][s["turns"] % 7]
        s["turns"] += 1
        return self._commit_learner_advance(
            s, step, verification, current_ch, praise, "learner")

    def telemetry_verify(self, session_id: str, lines: list) -> dict:
        """Mark a build DONE on telemetry proof, never on ask.

        The monitor submits its trailing lines; the deterministic
        proof rule admits only a healthy streak. Returns the DONE
        learning view, or {"verified": False, "proof": ...} when the
        evidence does not hold yet. Raises KeyError for an unknown or
        non-learning session.
        """
        from .telemetry import telemetry_proof

        s = self.sessions.get(session_id)
        if s is None or not s.get("learning"):
            raise KeyError("unknown learning session id")
        self.session = s
        self.current_id = session_id
        proof = telemetry_proof(lines or [])
        if not proof["verified"]:
            return {"verified": False, "proof": proof,
                    "session_id": session_id}
        plan = s.get("plan") or []
        s["completed_at"] = _now()
        s["code_ready"] = True
        steps = s.get("verified", {}).setdefault("steps", [])
        if not steps or steps[-1].get("test") != "TELEMETRY":
            steps.append({"n": "telemetry", "test": "TELEMETRY",
                          "passed": True,
                          "measured": "%d consecutive healthy lines"
                                      % proof["healthy"]})
            # Proof evidence changes the firmware key: rewarm for it.
            self._start_firmware_warmup(s)
        verification = {"test": "TELEMETRY", "passed": True,
                        "measured": "%d consecutive healthy lines"
                                    % proof["healthy"]}
        s["last_verification"] = verification
        speak = "It works! You built %s." % (
            s.get("project_title") or "your project")
        self.voice.speak(speak)
        return self._learning_view(
            state="DONE",
            instruction="Project verified — live telemetry proves the "
                        "build works. Your project code is below.",
            speak=speak, verification=verification,
            fundi={"event": "LESSON_DONE", "chapter": None,
                   "chapters_total": len(plan)})

    def _commit_learner_advance(self, s: dict, step: dict, verification: dict,
                                current_ch: dict, celebration: str,
                                confirmed_by: str) -> dict:
        """Shared tail: log the attestation, bump the step, speak the next."""
        s["last_verification"] = verification
        s["verified"]["steps"].append({
            "n": step.get("n", s["step_index"] + 1),
            "test": verification.get("test", ""),
            "passed": True,
            "measured": verification.get("measured", ""),
            "confirmed_by": confirmed_by})
        self._log_learning_confirmation(s, step, verification, confirmed_by)
        idx = s["step_index"] + 1
        s["step_index"] = idx
        # Evidence grew, so the firmware key changed: rewarm for it.
        self._start_firmware_warmup(s)
        if idx >= len(s["plan"]):
            s["completed_at"] = _now()
            s["code_ready"] = True
            speak = ("Boom, you finished it! All "
                     "%d steps are done. Your project code is shown below."
                     % len(s["plan"]))
            self.voice.speak(speak)
            return self._learning_view(
                state="DONE",
                instruction="Lesson complete. Your project code is below.",
                speak=speak, verification=verification,
                fundi={"event": "LESSON_DONE", "chapter": None,
                       "chapters_total": len(s["plan"])})
        next_ch = chapter_for(s.get("chapters") or [], idx)
        text = "%s Next: %s" % (
            celebration, speak_start(next_ch or {}, s.get("project_title", "")))
        if next_ch is not None and not str(next_ch.get("teach") or "").strip():
            text = "%s %s" % (celebration, next_step_opening(next_ch))
        self.voice.speak(text)
        view = self._learning_view(
            state="INSTRUCTING", instruction=text, speak=text,
            verification=verification,
            fundi={"event": "CHAPTER_PASSED", "chapter": current_ch,
                   "next_chapter": next_ch,
                   "celebration": str(current_ch.get("celebration") or celebration)})
        if not getattr(self.Muse, "mock", True):
            if idx == len(s["plan"]) - 1:
                self._start_stretch_prefetch()
        return view

    @staticmethod
    def _confirmed_count(s: dict) -> int:
        return sum(1 for a in s.get("attempts", []) if a.get("confirmed_by"))

    def _log_learning_confirmation(self, s: dict, step: dict,
                                   verification: dict,
                                   confirmed_by: str = "technician") -> None:
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
            "confirmed_by": confirmed_by,
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
        if fundi is not None:
            view["fundi"] = fundi
            if fundi.get("event") == "LESSON_DONE" \
                    and "stretch" not in fundi:
                # Every DONE view carries stretch goals: the model
                # upgrade when its prefetch landed, else the grounded
                # fallback computed at start. Speech teases the first.
                stretch = list(s.get("stretch") or [])
                view["fundi"] = dict(fundi, stretch=stretch)
                if stretch and stretch[0].get("summary"):
                    view["speak"] = "%s And when you want more: %s" % (
                        speak, stretch[0]["summary"])
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
        Raises ValueError for learning sessions: those advance with
        /api/session/advance and prove with telemetry-verified.
        """
        req = req or CheckNowRequest()
        if self.session is None:
            self.start_session()
        assert self.session is not None
        if self.session.get("learning"):
            raise ValueError(
                "check-now serves guided/diagnostic sessions; learning "
                "sessions advance with /api/session/advance and prove "
                "with /api/session/telemetry-verified")

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
            image_b64=base64.b64encode(frame).decode("ascii"),
            json_mode=False).strip()
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
