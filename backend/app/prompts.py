"""Model prompt strings for generative discovery (kept out of discovery.py)."""
from __future__ import annotations

import json

from .boards import describe_board
from .capabilities import KNOWN_PARTS, SENSOR_INTERFACES


def scene_system_prompt(sam_results: list, known_parts: list[str]) -> str:
    """System prompt for scene understanding (frame + SAM regions)."""
    return (
        "You are looking at a hardware workbench through an overhead camera. "
        "SAM has detected the following regions: %s. "
        "Look at the frame yourself and describe the scene. "
        "Return JSON with exactly these fields:\n"
        "{\n"
        '  visible_parts: [{name, confidence, evidence, state}],\n'
        "  connections_observed: [{from, to, confidence, evidence}],\n"
        "  uncertainties: [string],\n"
        "  notes: string\n"
        "}\n"
        "Rules:\n"
        "- name must be one of this vocabulary: %s, or 'unknown' if you see "
        "a part you cannot name.\n"
        "- state is a short phrase describing the apparent condition, e.g. "
        "'loose on bench', 'inserted on breadboard', 'wired to GPIO21'.\n"
        "- connections_observed only includes connections you can actually see. "
        "If a wire is present but you cannot tell where it goes, put it in "
        "uncertainties, not connections_observed.\n"
        "- Do not guess. If something is not clearly visible, say so in uncertainties.\n"
        "- confidence is 0.0 to 1.0. Anything below 0.6 must also appear in uncertainties.\n"
        "- Never state that the circuit works or is correct. You describe appearance "
        "only. The sensor decides correctness."
        % (json.dumps(sam_results)[:6000], ", ".join(known_parts))
    )


def plan_system_prompt(available_tests: dict, visible_parts: list[str],
                       connections_observed: list, uncertainties: list,
                       user_project: str | None, constraints: str | None,
                       board: str | None = None) -> str:
    """System prompt for project planning (validated plans only)."""
    tests = "\n".join(
        "- %s: %s Expects %s." % (name, spec.get("description", ""),
                                  spec.get("expects", ""))
        for name, spec in available_tests.items()
    )
    cheat = "\n".join(
        "- %s: %s%s; verify with %s" % (
            part, spec.get("interface", "?"),
            (" (%s)" % ", ".join(spec.get("addresses", [])))
            if spec.get("addresses") else
            (" (%s)" % spec.get("pins", "")) if spec.get("pins") else "",
            spec.get("verify", "?"))
        for part, spec in SENSOR_INTERFACES.items()
    )
    lines = [
        "You plan verifiable electronics builds. The ONLY verification tests "
        "that exist are:",
        tests,
        "",
        "Target board: %s" % describe_board(board),
        "Every instruction MUST use this board's pins and voltage. Never use "
        "another board's pin numbers.",
        "",
        "Sensor wiring cheat-sheet (interface -> test that verifies it):",
        cheat,
        "",
        "Current scene understanding:",
        "visible parts: %s" % ", ".join(visible_parts),
        "observed connections: %s" % json.dumps(connections_observed or [])[:2000],
        "uncertainties: %s" % json.dumps(uncertainties or [])[:2000],
        "",
        "Use the observed connections to decide which step comes first. If a "
        "connection is already observed, that step may be marked as possibly "
        "complete, but the backend will still run the verification test. If a "
        "part is in uncertainties, add a step that asks the user to confirm "
        "its identity or position before relying on it.",
        "",
        "Return JSON with exactly this shape, no prose:",
        '{"projects": [{id, title, summary, steps: '
        "[{n, instruction, verify, expect, depends_on_parts}], "
        "missing_parts: [], alternatives: []}]}",
        "Rules:",
        "- Every step's verify MUST be one of: %s." % ", ".join(available_tests),
        "- Every depends_on_parts entry MUST be in visible parts, or the part "
        "MUST be listed in that project's missing_parts.",
        "- expect states what the verification test should return.",
    ]
    if user_project:
        lines += [
            "The user asked to build: %s." % user_project,
            "Plan that project. If parts are missing, list them in missing_parts "
            "and include at least one alternative project that uses only visible parts.",
        ]
    else:
        lines.append("Suggest up to 3 projects buildable with the visible parts.")
    if constraints:
        lines.append("User constraints: %s." % constraints)
    return "\n".join(lines)


def learning_hint_prompt(step: dict, verification: dict) -> str:
    """Ask Muse for one targeted fix hint grounded in sensor evidence."""
    return (
        "A build step just FAILED its sensor verification. Write ONE short "
        "hint (under 25 words) telling the technician exactly what to check. "
        "Ground it only in this evidence, no new wiring claims:\n"
        "step: %s\ninstruction: %s\ntest: %s\nmeasured: %s"
        % (step.get("n", "?"), step.get("instruction", ""),
           verification.get("test", "?"), verification.get("measured", ""))
    )


def learning_code_prompt(project_title: str, plan: list, visible_parts: list,
                         verified: dict, pin_map: dict) -> str:
    """Ask Muse for the verified project's .ino firmware."""
    return (
        "Generate the complete Arduino (.ino) firmware for this VERIFIED "
        "electronics build. Return ONLY code, no prose or fences.\n"
        "project: %s\ntarget board: %s (%s logic)\nplan: %s\n"
        "visible parts: %s\nverified sensor evidence: %s\n"
        "board pin map (USE THESE EXACT NUMBERS): SDA=%s SCL=%s LED=%s. "
        "I2C addresses confirmed on the bus: %s. "
        "Every pin number and address in the code MUST come from the verified "
        "evidence above, never from memory."
        % (project_title, pin_map.get("label", pin_map.get("board", "?")),
           pin_map.get("voltage", "?"), json.dumps(plan)[:3000],
           ", ".join(visible_parts), json.dumps(verified)[:2000],
           pin_map.get("sda"), pin_map.get("scl"),
           pin_map.get("led_gpio"),
           ", ".join(verified.get("i2c", [])) or "none")
    )


DESCRIBE_SYSTEM = (
    "You are looking at a hardware workbench through an overhead camera. "
    "Describe what you actually see in two or three short sentences: the "
    "equipment present, its apparent state, and anything uncertain. Name "
    "only parts you can genuinely identify; if the photo is too blurry, "
    "dark, or empty to tell, say that plainly. Keep it under 40 words in "
    "plain spoken style that reads well aloud: no ALL-CAPS words and no "
    "parenthetical numbers. Never state that the "
    "circuit works or is correct: you describe appearance only."
)


def describe_scene_prompt(detections: list) -> str:
    """User prompt for DESCRIBE: frame + unlabeled SAM regions."""
    boxes = [
        {"box": d.get("box"), "confidence": d.get("confidence")}
        for d in (detections or []) if isinstance(d, dict)
    ]
    return (
        "SAM segmented %d region(s) in this frame: %s. "
        "These boxes are unlabeled locations, not identifications. "
        "Look at the photo yourself and describe the bench."
        % (len(boxes), json.dumps(boxes)[:2000])
    )


LIVE_SYSTEM = (
    "You are FundiLoop guiding a technician in a LIVE session. The technician "
    "is speaking to you and pointing the overhead camera at the bench, so you "
    "see a short clip of the work in progress and hear what they said. Coach "
    "them out loud, one action at a time, like an expert standing beside them. "
    "Speak in short, plain sentences (under 30 words) because your reply is "
    "read aloud; use conversational words, no ALL-CAPS shouting. "
    "Name only what the clip and the probe readings actually show; "
    "if the view is unclear, ask them to move the camera or hold still. Never "
    "claim a connection is correct or a fault is fixed -- what you can see is "
    "advisory and only the probe measurements can confirm it."
)


def live_guidance_prompt(transcript: str, probe_summary: dict, tracks: dict | None,
                         mode: str, step_text: str) -> str:
    """User prompt for one live turn: their words + what the camera tracked."""
    track_brief = [
        {"object_id": t.get("object_id"), "frames_seen": t.get("frames_seen"),
         "last_box": (t.get("frames") or [{}])[-1].get("box")}
        for t in ((tracks or {}).get("tracks") or [])
    ]
    return (
        "The technician just said: %s\n"
        "Session mode: %s\n"
        "Current step or goal: %s\n"
        "Live probe measurements (the only ground truth): %s\n"
        "SAM 3.1 followed %d object(s) across the clip: %s\n"
        "Give the next single instruction out loud."
        % ((transcript or "(said nothing)"), mode, step_text or "(none)",
           json.dumps(probe_summary)[:1500], len(track_brief),
           json.dumps(track_brief)[:1500])
    )


BOARD_GLANCE_SYSTEM = (
    "You identify the main microcontroller board on a workbench. Reply with "
    "exactly one JSON object, no prose: "
    '{"board": "esp32_devkit|arduino_uno|arduino_nano|esp8266|pico|none", '
    '"confidence": 0.0-1.0, "parts": ["visible part names"]}. '
    "Part names must come from this vocabulary: %s. "
    "If no board is clearly visible, use board none with low confidence."
    % ", ".join(KNOWN_PARTS)
)


def board_glance_prompt() -> str:
    """User prompt for the cheap board check (one call, cached per session)."""
    return ("What microcontroller board and parts are on this bench? "
            "Reply with only the JSON object.")


def parse_board_glance(raw: str) -> dict:
    """Parse a glance reply; anything unparseable means board unknown."""
    out: dict = {"board": "none", "board_label": "", "confidence": 0.0,
                 "parts": []}
    try:
        data = json.loads(raw or "")
    except (TypeError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    board = str(data.get("board") or "none").strip().lower() or "none"
    try:
        conf = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    parts = [str(p).strip().lower() for p in (data.get("parts") or [])
             if str(p).strip()][:20]
    out.update(board=board, board_label=str(data.get("board") or ""),
               confidence=min(1.0, max(0.0, conf)), parts=parts)
    return out


def plan_retry_suffix(reasons: list[str]) -> str:
    """Constraint restatement appended when a plan is rejected."""
    return (
        "Your previous plan was REJECTED by the backend validator:\n"
        + "\n".join("- %s" % r for r in reasons)
        + "\nFix every reason above and return the full corrected JSON again. "
        "Every step's verify must be a real test name and every "
        "depends_on_parts entry must be visible or listed in missing_parts."
    )
