"""Learning coach, tiers 1+2 (core) + Tier 4 (memory) + Tier 5 (heartbeat)
+ Tier 6 (rails): text brain loop, tool registry, disk persistence,
proactive nudges, confirmation gate, audit trail, and kill switch
(teach-only).

Tier 1 (brain): CoachSession keeps the full conversation in memory and sends
it plus the system prompt to Muse Spark on every turn. Tier 2 (hands):
read-only teaching tools the brain can call mid-turn; results (including
failures) go back into the conversation so the model can react to them.

Streaming note: decisions are single JSON calls, like
MetaClaudeAdapter._real_decide, because the Meta API's support for the
OpenAI `tools` parameter is unverified and JSON mode works on any chat
model. ask_stream therefore replays the decided reply incrementally —
live token streaming can't apply because the JSON decision must validate
whole before anything speaks. Tier 3 voice gets its early speech from
that incremental replay plus frontend clause streaming, not from the
model socket.

The coach teaches; it never gates. The learner advances by saying next or
done. Tools are read-only bench notes: no firmware writes, no config or
hardware changes. Adding a capability means register_tool() only — the
core loop is never edited for a new tool.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .adapters import MetaAPIError
from .adapters.muse_adapter import MetaClaudeAdapter
from .capabilities import KNOWN_PARTS, SENSOR_INTERFACES
from .circuit_graph import load_circuit_graph

COACH_SYSTEM = """You are FundiLoop Learning, a warm, plain-spoken, brief teacher for hands-on electronics builds.
You teach one step at a time: what to do, why it matters, then the physical task. The learner does the work at their own pace and says next or done when ready.
You never mark a step complete yourself, never flash or overwrite firmware, and never change serial, camera, or config settings. Reading the bench notes (tools) is always allowed; acting on the world is not.
Reply with ONLY valid JSON, no prose: {"thought": str, "tool": {"name": str, "args": object} | null, "reply": str | null}
Rules: set exactly one of tool or reply. Call a tool when the learner asks about a specific build step or a part; use the exact tool names and input names below. When tool results arrive, weave them into a warm spoken answer grounded in the result. If a tool failed, explain the problem plainly and say what to try instead — never invent the missing fact. Keep reply under 60 words of plain spoken English (no ALL-CAPS words, no code, no parenthetical numbers); say ground for GND and three v three for 3V3. The full conversation history arrives with every request: remember earlier turns."""

_STEP_RE = re.compile(r"step\s*#?\s*(\d+)", re.IGNORECASE)
_STEP_WORD_RE = re.compile(r"\bstep\b", re.IGNORECASE)
_GREETINGS = {"hi", "hello", "hey", "yo", "howdy", "good morning",
              "good afternoon", "good evening"}
_TOOL_PREFIX = "Tool '"
_CONFIRM_PREFIX = "[confirmation] "

_GRAPH = None


def _graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = load_circuit_graph()
    return _GRAPH


# -- memory (Tier 4) -------------------------------------------------------
COACH_MEMORY_VERSION = 1
COACH_MEMORY_KEEP = 32


def coach_memory_dir() -> str:
    """Directory holding one JSON file per remembered conversation."""
    override = os.environ.get("COACH_MEMORY_DIR", "").strip()
    if override:
        return override
    return str(Path(__file__).resolve().parent.parent / "data" / "coach_memory")


def sanitize_session_id(session_id) -> str:
    """Make a client-supplied id safe as a dict key and filename.

    Keeps letters, digits, dash, underscore; anything else becomes "_".
    Empty or meaningless ids mint a random hex id instead.
    """
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id or "").strip())[:64]
    if not clean.strip("_") or clean == "killswitch":
        return uuid.uuid4().hex
    return clean


def _memory_path(session_id: str) -> str:
    return os.path.join(coach_memory_dir(), sanitize_session_id(session_id) + ".json")


def load_coach_history(session_id: str) -> list:
    """Restore one conversation. Missing/corrupt files start fresh. Never raises."""
    try:
        with open(_memory_path(session_id), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    history = payload.get("history")
    if not isinstance(history, list):
        return []
    out = []
    for turn in history:
        if (isinstance(turn, dict) and turn.get("role") in ("user", "assistant")
                and isinstance(turn.get("content"), str)):
            out.append({"role": turn["role"], "content": turn["content"]})
    return out


def _prune_coach_memory(directory: str, keep: int = COACH_MEMORY_KEEP) -> None:
    """Drop the oldest session files past the cap. Never raises."""
    try:
        paths = [os.path.join(directory, n) for n in os.listdir(directory)
                 if n.endswith(".json") and n != "killswitch.json"]
    except OSError:
        return
    if len(paths) <= keep:
        return

    def mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    paths.sort(key=mtime)
    for victim in paths[:len(paths) - keep]:
        try:
            os.remove(victim)
        except OSError:
            pass


def save_coach_history(session_id: str, history, last_nudge_at=None) -> str:
    """Atomically persist one conversation (tmp file + rename). Never raises.

    Returns the file path, or "" when nothing was written.
    """
    sid = sanitize_session_id(session_id)
    try:
        directory = coach_memory_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, sid + ".json")
        tmp = "%s.tmp-%d" % (path, os.getpid())
        payload = {"version": COACH_MEMORY_VERSION, "session_id": sid,
                   "updated_at": datetime.now(timezone.utc).isoformat(),
                   "history": list(history or []),
                   "nudge": {"last_nudge_at": (
                       last_nudge_at.isoformat() if last_nudge_at else None)}}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
        _prune_coach_memory(directory)
        return path
    except OSError:
        return ""


def forget_coach_history(session_id: str) -> bool:
    """Delete one persisted conversation. Never raises."""
    try:
        os.remove(_memory_path(session_id))
        return True
    except OSError:
        return False


# -- heartbeat (Tier 5) ------------------------------------------------------
_HEARTBEAT_PREFIX = "[nudge] "

DEFAULT_IDLE_NUDGE_SECONDS = 600
DEFAULT_NUDGE_COOLDOWN_SECONDS = 900
DEFAULT_QUIET_START = 22
DEFAULT_QUIET_END = 7


def _env_int(name: str, default: int, lo: int | None = None,
             hi: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    if lo is not None and value < lo:
        return default
    if hi is not None and value > hi:
        return default
    return value


def nudge_config() -> dict:
    """Heartbeat rules from env (never hardcoded callers)."""
    return {
        "idle_seconds": _env_int("COACH_IDLE_NUDGE_SECONDS",
                                 DEFAULT_IDLE_NUDGE_SECONDS, 1),
        "cooldown_seconds": _env_int("COACH_NUDGE_COOLDOWN_SECONDS",
                                     DEFAULT_NUDGE_COOLDOWN_SECONDS, 1),
        "quiet_start": _env_int("QUIET_HOURS_START", DEFAULT_QUIET_START, 0, 23),
        "quiet_end": _env_int("QUIET_HOURS_END", DEFAULT_QUIET_END, 0, 23),
    }


def in_quiet_hours(now, start: int, end: int) -> bool:
    """True when hour `now` falls in [start, end), overnight-aware."""
    hour = now.hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def load_last_nudge_at(session_id: str):
    """Restore the last-nudge timestamp. Missing/corrupt → None. Never raises."""
    try:
        with open(_memory_path(session_id), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    section = payload.get("nudge")
    if not isinstance(section, dict):
        return None
    raw = section.get("last_nudge_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class CoachKilledError(MetaAPIError):
    """The kill switch is tripped: the coach refuses all turns until re-armed."""


def rails_config() -> dict:
    """Rail switches from env. Strict mode gates even safe tools."""
    return {"gate_strict": os.environ.get("COACH_GATE_STRICT", "").lower()
            in ("1", "true", "yes")}


def _killswitch_path() -> str:
    return os.path.join(coach_memory_dir(), "killswitch.json")


def coach_killed() -> bool:
    """True when the kill switch is tripped. Missing/corrupt → False."""
    try:
        with open(_killswitch_path(), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return False
    return bool(isinstance(payload, dict) and payload.get("tripped"))


def set_killswitch(tripped: bool, actor: str = "") -> dict:
    """Trip or re-arm the kill switch (persisted). Returns {"tripped": bool}."""
    try:
        directory = coach_memory_dir()
        os.makedirs(directory, exist_ok=True)
        path = _killswitch_path()
        tmp = "%s.tmp-%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"tripped": bool(tripped),
                       "at": datetime.now(timezone.utc).isoformat(),
                       "actor": actor or ""}, fh)
        os.replace(tmp, path)
    except OSError:
        return {"tripped": coach_killed()}
    audit_rail("kill_tripped" if tripped else "kill_rearmed", detail=actor or "")
    return {"tripped": bool(tripped)}


# -- audit trail (Tier 6) ------------------------------------------------------
# Hash-chained JSON lines mirroring agent_orchestrator attempts: seq, at,
# prev_hash, body, hash. Rail events only (proposals, confirmations,
# denials, gated runs, nudges, kill flips) — safe tools flowing freely
# are normal operation and live in history, not here.
GENESIS_HASH = "0" * 64


def audit_log_path() -> str:
    override = os.environ.get("COACH_AUDIT_LOG", "").strip()
    if override:
        return override
    return str(Path(__file__).resolve().parent.parent / "logs" / "coach_audit.jsonl")


def _audit_hash(entry: dict) -> str:
    blob = json.dumps({k: v for k, v in entry.items() if k != "hash"},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def read_audit_log() -> list:
    """Read every audit entry. Missing/corrupt file → []. Never raises."""
    try:
        with open(audit_log_path(), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError):
        return []


def verify_audit_chain(events: list) -> bool:
    """Recompute the hash chain; True only if every link is intact."""
    prev = GENESIS_HASH
    for i, entry in enumerate(events, start=1):
        if not isinstance(entry, dict):
            return False
        if entry.get("seq") != i or entry.get("prev_hash") != prev:
            return False
        if entry.get("hash") != _audit_hash(entry):
            return False
        prev = entry["hash"]
    return True


def audit_rail(event: str, session_id: str = "", tool: str = "",
               args=None, ok=None, reason: str = "",
               detail: str = "") -> None:
    """Append one rail event. Logging must never break a turn."""
    try:
        path = audit_log_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        prev, seq = GENESIS_HASH, 1
        try:
            with open(path, encoding="utf-8") as fh:
                lines = [line for line in fh if line.strip()]
            if lines:
                last = json.loads(lines[-1])
                prev = last.get("hash", GENESIS_HASH)
                seq = int(last.get("seq", 0)) + 1
        except (OSError, ValueError):
            pass
        entry = {"seq": seq, "at": datetime.now(timezone.utc).isoformat(),
                 "prev_hash": prev, "session_id": session_id, "event": event,
                 "tool": tool, "args": args or {}, "ok": ok,
                 "reason": reason, "detail": detail}
        entry["hash"] = _audit_hash(entry)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# -- tools ----------------------------------------------------------------
@dataclass
class Tool:
    """One callable capability. `inputs` maps names to {"type", "description"}."""

    name: str
    description: str
    inputs: dict = field(default_factory=dict)
    func: object = None
    safe: bool = True  # False = needs the Tier 6 confirmation gate
    risk: str = ""  # human-readable stakes shown at the gate when unsafe


def explain_step(step_number: int) -> dict:
    """Teach card for one build step from the circuit YAML. Raises ValueError."""
    graph = _graph()
    total = graph.total_steps
    if not 1 <= step_number <= total:
        raise ValueError("Step %d is outside the %d build steps. Say a number from 1 to %d."
                         % (step_number, total, total))
    step = graph.get_step(step_number - 1)
    return {"n": step_number, "total": total, "step_id": step.step_id,
            "task": step.instruction_student, "part": step.expected_component,
            "why": step.success_criteria}


def explain_part(part_name: str) -> dict:
    """Wiring + verification notes for one vocabulary part. Raises ValueError."""
    norm = re.sub(r"[\s\-]+", "_", str(part_name or "").strip().lower())
    if norm not in KNOWN_PARTS:
        raise ValueError("Unknown part %r. Known parts: %s."
                         % (part_name, ", ".join(KNOWN_PARTS)))
    spec = SENSOR_INTERFACES.get(norm, {})
    sensor = norm in SENSOR_INTERFACES
    return {"part": norm, "kind": "sensor" if sensor else "part",
            "interface": spec.get("interface", "basic part (no sensor interface)"),
            "addresses": list(spec.get("addresses", [])),
            "pins": spec.get("pins", ""),
            "verify": spec.get("verify", "visual check only — no sensor test")}


TOOL_REGISTRY: dict[str, Tool] = {}


def register_tool(tool: Tool) -> None:
    """Add one capability. The core loop needs no edits for a new tool."""
    TOOL_REGISTRY[tool.name] = tool


register_tool(Tool(
    name="explain_step", safe=True,
    description=("Use this to look up the teach card for one numbered build "
                 "step: the task, the part it needs, and why it matters."),
    inputs={"step_number": {"type": "int",
                             "description": "Build step number, 1 to 8."}},
    func=explain_step))
register_tool(Tool(
    name="explain_part", safe=True,
    description=("Use this to explain what an electronics part is, how it "
                 "wires, and which sensor test verifies it."),
    inputs={"part_name": {"type": "str",
                           "description": "Exact part name, e.g. bme280, led, breadboard."}},
    func=explain_part))


def tool_catalog() -> str:
    """Registry rendered for the model; rebuilt every turn (Tier 2 rule)."""
    lines = []
    for tool in TOOL_REGISTRY.values():
        args = ", ".join("%s (%s): %s" % (name, spec.get("type", "?"),
                                          spec.get("description", ""))
                         for name, spec in tool.inputs.items())
        lines.append("- %s(%s): %s" % (tool.name, args, tool.description))
    return "\n".join(lines)


def run_tool(name: str, args: object) -> dict:
    """Run one registry tool. Never raises: failures are plain-text errors."""
    tool = TOOL_REGISTRY.get(str(name or ""))
    if tool is None:
        return {"ok": False, "result": None,
                "error": "unknown tool %r. Available tools: %s."
                         % (name, ", ".join(sorted(TOOL_REGISTRY)))}
    if not isinstance(args, dict):
        return {"ok": False, "result": None,
                "error": "tool '%s' needs an object of named inputs." % tool.name}
    coerced: dict = {}
    for param, spec in tool.inputs.items():
        if param not in args:
            return {"ok": False, "result": None,
                    "error": "tool '%s' needs '%s' (%s)."
                             % (tool.name, param, spec.get("description", ""))}
        value = args[param]
        want = spec.get("type", "str")
        if want == "int":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return {"ok": False, "result": None,
                        "error": "tool '%s': '%s' must be an integer, got %r."
                                 % (tool.name, param, value)}
            if isinstance(value, float):
                if not value.is_integer():
                    return {"ok": False, "result": None,
                            "error": "tool '%s': '%s' must be an integer, got %r."
                                     % (tool.name, param, value)}
                value = int(value)
        elif not isinstance(value, str):
            return {"ok": False, "result": None,
                    "error": "tool '%s': '%s' must be text, got %r."
                             % (tool.name, param, value)}
        coerced[param] = value
    for key in args:
        if key not in tool.inputs:
            return {"ok": False, "result": None,
                    "error": "tool '%s' has no input '%s'. Expected: %s."
                             % (tool.name, key, ", ".join(tool.inputs))}
    try:
        return {"ok": True, "result": tool.func(**coerced), "error": ""}
    except ValueError as exc:
        return {"ok": False, "result": None, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a tool must never crash a turn
        return {"ok": False, "result": None,
                "error": "tool '%s' failed: %s" % (tool.name, exc)}


# -- decisions -------------------------------------------------------------
def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    return text


def _parse_decision(raw: str) -> tuple:
    """Validate one decision object. Returns (tool|None, reply|None)."""
    try:
        data = json.loads(_strip_fences(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("not JSON: %s" % exc)
    if not isinstance(data, dict):
        raise ValueError("decision must be a JSON object")
    tool, reply = data.get("tool"), data.get("reply")
    if tool is not None:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise ValueError("tool must be {name: str, args: object}")
        if not isinstance(tool.get("args", {}), dict):
            raise ValueError("tool args must be an object")
        tool = {"name": tool["name"], "args": tool.get("args", {})}
    if reply is not None and not isinstance(reply, str):
        raise ValueError("reply must be text")
    if tool is None and not (reply or "").strip():
        raise ValueError("neither tool nor reply is set")
    return tool, (reply or "").strip()


def _user_texts(history: list) -> list[str]:
    skipped = (_TOOL_PREFIX, _HEARTBEAT_PREFIX, _CONFIRM_PREFIX)
    return [m["content"] for m in history
            if m.get("role") == "user"
            and not str(m.get("content") or "").startswith(skipped)]


def _memory_line(history: list) -> str:
    firsts = _user_texts(history)
    if len(firsts) < 2:
        return ""
    return " (Turn %d — you first asked about '%s'.)" % (len(firsts), firsts[0][:60])


def _find_part(text: str) -> str | None:
    norm = re.sub(r"[\s\-]+", " ", str(text or "").lower())
    for part in sorted(KNOWN_PARTS, key=len, reverse=True):
        spoken = part.replace("_", " ")
        if re.search(r"\b%s\b" % re.escape(spoken), norm):
            return part
    return None


def _mock_decide(history: list) -> tuple:
    """Deterministic offline brain: proves memory + tool use with no keys."""
    last = history[-1]["content"] if history else ""
    if str(last).startswith(_TOOL_PREFIX):
        failed = " failed: " in str(last)
        if failed:
            err = str(last).split(" failed: ", 1)[1]
            return None, ("That lookup did not work — %s Tell me a step number "
                           "from 1 to 8, or name a part like the BME280.%s"
                           % (err if err.endswith(" ") else err + " ", _memory_line(history)))
        payload = str(last).split(" result: ", 1)[1]
        try:
            result = json.loads(payload)
        except ValueError:
            result = {}
        name = str(last)[len(_TOOL_PREFIX):].split("'", 1)[0]
        if name == "explain_step" and isinstance(result, dict):
            reply = ("Step %(n)s of %(total)s: %(task)s You will need: %(part)s. "
                     "Why it matters: %(why)s" % result)
        elif name == "explain_part" and isinstance(result, dict):
            addrs = result.get("addresses") or []
            addr = " (at %s)" % "/".join(addrs) if addrs else (
                " (%s)" % result["pins"] if result.get("pins") else "")
            if result.get("kind") == "sensor":
                reply = ("The %(part)s talks over %(interface)s%(addr)s, and you verify "
                         "it with %(verify)s." % {**result, "addr": addr})
            else:
                reply = ("The %(part)s needs no sensor wiring (%(interface)s). "
                         "%(verify)s." % result)
        else:
            reply = "Here is what I found: %s" % payload[:300]
        return None, reply + _memory_line(history)
    text = str(last)
    num = _STEP_RE.search(text)
    if num:
        return {"name": "explain_step", "args": {"step_number": int(num.group(1))}}, None
    if _STEP_WORD_RE.search(text):
        return None, ("Which step? There are 8 in the build — say 'teach me "
                       "step 3'.%s" % _memory_line(history))
    part = _find_part(text)
    if part:
        return {"name": "explain_part", "args": {"part_name": part}}, None
    words = re.sub(r"[^\w\s]", "", text.strip().lower())
    if words in _GREETINGS:
        return None, ("Hello! I teach the 8-step ESP32 build. Say 'teach me "
                       "step 1' or ask about a part.%s" % _memory_line(history))
    return None, ("Happy to teach that. I can walk you through any of the 8 "
                   "build steps or explain a part — try 'teach me step 4' or "
                   "'what is the BME280?'%s" % _memory_line(history))


# -- session ---------------------------------------------------------------
class CoachSession:
    """Tier 1+2 agent core: one history, model + tools, text in/out.

    With a session_id (Tier 4), history loads from disk at init and saves
    after every turn. Without one, the session stays in-memory only.
    """

    def __init__(self, Muse: MetaClaudeAdapter | None = None,
                 max_tool_rounds: int = 3,
                 session_id: str | None = None) -> None:
        self.Muse = Muse or MetaClaudeAdapter()
        self.max_tool_rounds = max(1, int(max_tool_rounds))
        self.session_id = sanitize_session_id(session_id) if session_id else ""
        self.history: list[dict] = (
            load_coach_history(self.session_id) if self.session_id else [])
        self.last_nudge_at = (
            load_last_nudge_at(self.session_id) if self.session_id else None)
        self.pending: dict | None = None  # gated tool awaiting confirmation

    def _save(self) -> None:
        if self.session_id:
            save_coach_history(self.session_id, self.history, self.last_nudge_at)

    def nudge(self, idle_seconds=0, step_label: str = "", now=None,
              step_text: str = ""):
        """Maybe produce a proactive nudge. Returns {text, reason} or None.

        Rules (idle threshold, quiet hours, cooldown) decide WHETHER; the
        brain writes WHAT — a short template offline, the model live. Times
        are local laptop time. Nudge turns join history with the [nudge]
        prefix so memory stays coherent.
        """
        if coach_killed():
            raise CoachKilledError("coach stopped by kill switch")
        cfg = nudge_config()
        try:
            idle = max(0.0, float(idle_seconds or 0))
        except (TypeError, ValueError):
            idle = 0.0
        moment = now or datetime.now()
        if idle < cfg["idle_seconds"]:
            return None
        if in_quiet_hours(moment, cfg["quiet_start"], cfg["quiet_end"]):
            return None
        if self.last_nudge_at is not None:
            try:
                elapsed = (moment - self.last_nudge_at).total_seconds()
            except TypeError:  # mixed naive/aware stamps: treat as expired
                elapsed = float("inf")
            if elapsed < cfg["cooldown_seconds"]:
                return None
        label = (step_label or "").strip().lower() or "this step"
        detail = (step_text or "").strip()[:200]
        context = ("%sThe learner has been idle %d minutes on %s%s. Briefly "
                   "nudge them back in under 25 words of plain spoken "
                   "English; invite 'check' or a question."
                   % (_HEARTBEAT_PREFIX, int(idle // 60), label,
                      (": %s" % detail) if detail else ""))
        if self.Muse.mock:
            text = ("Still on %s? Say check when it's done, or ask me anything."
                    % label)
            self.history.append({"role": "user", "content": context})
            self.history.append({"role": "assistant", "content": text})
        else:
            text = self.ask(context)["reply"]
        self.last_nudge_at = moment
        self._save()
        audit_rail("nudge_sent", session_id=self.session_id, reason="idle",
                   detail=text)
        return {"text": text, "reason": "idle"}

    def _system(self) -> str:
        return (COACH_SYSTEM + "\n\nAvailable tools (use these exact names):\n"
                + tool_catalog())

    def _decide(self) -> tuple:
        if self.Muse.mock:
            return _mock_decide(self.history)
        system = self._system()
        raw = self.Muse.chat_text(system, self.history, json_mode=True)
        try:
            return _parse_decision(raw)
        except ValueError:
            pass
        raw = self.Muse.chat_text(
            system + "\nYour last reply was not valid JSON matching the schema. "
                     "Reply with ONLY the JSON object.",
            self.history, json_mode=True)
        try:
            return _parse_decision(raw)
        except ValueError as exc:
            raise MetaAPIError("coach received invalid decision JSON twice: %s" % exc)

    @staticmethod
    def _gated(tool: dict) -> bool:
        """True when a proposed tool call must pause for confirmation."""
        proc = TOOL_REGISTRY.get(tool["name"]) if tool else None
        if proc is None:
            return False  # unknown tools fail plainly in run_tool, ungated
        if not proc.safe:
            return True
        return rails_config()["gate_strict"]

    def _record_tool_result(self, name: str, args: dict, res: dict,
                            tool_calls: list) -> None:
        tool_calls.append({"name": name, "args": args, "ok": res["ok"],
                           "result": res["result"], "error": res["error"]})
        if res["ok"]:
            self.history.append({"role": "user", "content":
                "Tool '%s' result: %s" % (name, json.dumps(res["result"]))})
        else:
            self.history.append({"role": "user", "content":
                "Tool '%s' failed: %s" % (name, res["error"])})

    def _run_loop(self, tool_calls: list, rounds_left: int | None = None) -> dict:
        """Decide → (gate | run tool) → reply. Shared by ask() and confirm()."""
        if rounds_left is None:
            rounds_left = self.max_tool_rounds
        while rounds_left > 0:
            rounds_left -= 1
            tool, reply = self._decide()
            if tool is None:
                final = reply or "I have nothing to add — try asking about a step or a part."
                self.history.append({"role": "assistant", "content": final})
                self._save()
                return {"reply": final, "tool_calls": tool_calls,
                        "confirmation_pending": None}
            if self._gated(tool):
                proc = TOOL_REGISTRY.get(tool["name"])
                risk = (proc.risk if proc and proc.risk
                        else "run %s" % tool["name"])
                call_id = uuid.uuid4().hex
                question = ("To continue I need to: %s. "
                            "Confirm to allow it, deny to cancel." % risk)
                self.pending = {"call_id": call_id, "name": tool["name"],
                                "args": tool["args"], "rounds_left": rounds_left,
                                "tool_calls": tool_calls}
                audit_rail("tool_proposed", session_id=self.session_id,
                           tool=tool["name"], args=tool["args"], reason=risk)
                self._save()
                return {"reply": question, "tool_calls": tool_calls,
                        "confirmation_pending": {
                            "call_id": call_id, "tool": tool["name"],
                            "args": tool["args"], "question": question,
                            "risk": risk}}
            res = run_tool(tool["name"], tool["args"])
            self._record_tool_result(tool["name"], tool["args"], res, tool_calls)
        found = []
        for call in tool_calls:
            if call["ok"]:
                found.append("%s: %s" % (call["name"], json.dumps(call["result"])[:400]))
            else:
                found.append("%s failed: %s" % (call["name"], call["error"]))
        final = ("I found this in the bench notes: %s What should we do next?"
                 % " ".join(found))
        self.history.append({"role": "assistant", "content": final})
        self._save()
        return {"reply": final, "tool_calls": tool_calls,
                "confirmation_pending": None}

    def ask(self, text: str) -> dict:
        """One text turn: tool loop, then a reply. Raises MetaAPIError only."""
        if coach_killed():
            raise CoachKilledError("coach stopped by kill switch")
        clean = (text or "").strip()
        if not clean:
            return {"reply": "What would you like to learn? Name a step number or a part.",
                    "tool_calls": [], "confirmation_pending": None}
        self.history.append({"role": "user", "content": clean})
        return self._run_loop([])

    def confirm(self, call_id: str, confirmed: bool) -> dict:
        """Resolve a pending gated tool. Returns an ask-shaped dict.

        Yes runs the tool and continues the turn; no aborts it plainly.
        Pending confirmations are ephemeral: a restart clears them (409).
        """
        if coach_killed():
            raise CoachKilledError("coach stopped by kill switch")
        pending = self.pending
        if not pending or pending.get("call_id") != call_id:
            raise ValueError("no confirmation pending for %r" % (call_id,))
        self.pending = None
        name, args = pending["name"], pending["args"]
        if not confirmed:
            audit_rail("tool_denied", session_id=self.session_id,
                       tool=name, args=args)
            self.history.append({"role": "user", "content":
                "%sTool '%s' denied by technician." % (_CONFIRM_PREFIX, name)})
            if self.Muse.mock:
                final = ("Understood — I won't do that. "
                         "What should we do instead?%s"
                         % _memory_line(self.history))
                self.history.append({"role": "assistant", "content": final})
                self._save()
                return {"reply": final, "tool_calls": pending["tool_calls"],
                        "confirmation_pending": None}
            return self._run_loop(pending["tool_calls"], pending["rounds_left"])
        res = run_tool(name, args)
        audit_rail("tool_confirmed", session_id=self.session_id,
                   tool=name, args=args)
        audit_rail("tool_ran", session_id=self.session_id, tool=name,
                   args=args, ok=res["ok"], reason="confirmed")
        self._record_tool_result(name, args, res, pending["tool_calls"])
        return self._run_loop(pending["tool_calls"], pending["rounds_left"])

    def ask_stream(self, text: str):
        """Yield tool_call/tool_result events, then the reply word by word.

        A gated tool yields confirmation_pending instead of tokens; the
        caller resolves it with confirm() and speaks the final reply.
        """
        out = self.ask(text)
        for call in out["tool_calls"]:
            yield {"event": "tool_call", "name": call["name"], "args": call["args"]}
            yield {"event": "tool_result", "name": call["name"], "ok": call["ok"]}
        if out["confirmation_pending"]:
            yield {"event": "confirmation_pending",
                   **out["confirmation_pending"]}
            yield {"event": "done", "reply": out["reply"],
                   "tool_calls": out["tool_calls"],
                   "confirmation_pending": out["confirmation_pending"]}
            return
        for chunk in re.findall(r"\S+\s*|\s+", out["reply"]):
            yield {"event": "token", "text": chunk}
        yield {"event": "done", "reply": out["reply"],
               "tool_calls": out["tool_calls"], "confirmation_pending": None}
