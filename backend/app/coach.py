"""Learning coach, tiers 1+2: text brain loop + tool registry (teach-only).

Tier 1 (brain): CoachSession keeps the full conversation in memory and sends
it plus the system prompt to Muse Spark on every turn. Tier 2 (hands):
read-only teaching tools the brain can call mid-turn; results (including
failures) go back into the conversation so the model can react to them.

Streaming note: decisions are single JSON calls, like
MetaClaudeAdapter._real_decide, because the Meta API's support for the
OpenAI `tools` parameter is unverified and JSON mode works on any chat
model. ask_stream therefore replays the decided reply incrementally for
the terminal; true token streaming via complete_text_stream is reserved
for Tier 3 voice, where early speech matters.

The coach teaches; it never gates. The learner advances by saying next or
done. Tools are read-only bench notes: no firmware writes, no config or
hardware changes. Adding a capability means register_tool() only — the
core loop is never edited for a new tool.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

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

_GRAPH = None


def _graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = load_circuit_graph()
    return _GRAPH


# -- tools ----------------------------------------------------------------
@dataclass
class Tool:
    """One callable capability. `inputs` maps names to {"type", "description"}."""

    name: str
    description: str
    inputs: dict = field(default_factory=dict)
    func: object = None
    safe: bool = True  # False = needs the Tier 6 confirmation gate


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
    return [m["content"] for m in history
            if m.get("role") == "user" and not str(m.get("content") or "").startswith(_TOOL_PREFIX)]


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
    """Tier 1+2 agent core: one history, model + tools, text in/out."""

    def __init__(self, Muse: MetaClaudeAdapter | None = None,
                 max_tool_rounds: int = 3) -> None:
        self.Muse = Muse or MetaClaudeAdapter()
        self.max_tool_rounds = max(1, int(max_tool_rounds))
        self.history: list[dict] = []

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

    def ask(self, text: str) -> dict:
        """One text turn: tool loop, then a reply. Raises MetaAPIError only."""
        clean = (text or "").strip()
        if not clean:
            return {"reply": "What would you like to learn? Name a step number or a part.",
                    "tool_calls": []}
        self.history.append({"role": "user", "content": clean})
        tool_calls: list[dict] = []
        for _ in range(self.max_tool_rounds):
            tool, reply = self._decide()
            if tool is None:
                final = reply or "I have nothing to add — try asking about a step or a part."
                self.history.append({"role": "assistant", "content": final})
                return {"reply": final, "tool_calls": tool_calls}
            res = run_tool(tool["name"], tool["args"])
            tool_calls.append({"name": tool["name"], "args": tool["args"],
                               "ok": res["ok"], "result": res["result"],
                               "error": res["error"]})
            if res["ok"]:
                self.history.append({"role": "user", "content":
                    "Tool '%s' result: %s" % (tool["name"], json.dumps(res["result"]))})
            else:
                self.history.append({"role": "user", "content":
                    "Tool '%s' failed: %s" % (tool["name"], res["error"])})
        found = []
        for call in tool_calls:
            if call["ok"]:
                found.append("%s: %s" % (call["name"], json.dumps(call["result"])[:400]))
            else:
                found.append("%s failed: %s" % (call["name"], call["error"]))
        final = ("I found this in the bench notes: %s What should we do next?"
                 % " ".join(found))
        self.history.append({"role": "assistant", "content": final})
        return {"reply": final, "tool_calls": tool_calls}

    def ask_stream(self, text: str):
        """Yield tool_call/tool_result events, then the reply word by word."""
        out = self.ask(text)
        for call in out["tool_calls"]:
            yield {"event": "tool_call", "name": call["name"], "args": call["args"]}
            yield {"event": "tool_result", "name": call["name"], "ok": call["ok"]}
        for chunk in re.findall(r"\S+\s*|\s+", out["reply"]):
            yield {"event": "token", "text": chunk}
        yield {"event": "done", "reply": out["reply"], "tool_calls": out["tool_calls"]}
