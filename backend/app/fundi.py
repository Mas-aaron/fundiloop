"""Fundi chapter delivery: teach-first lesson chapters over validated plans.

The plan steps (instruction + verify test) already exist and are validated.
This module asks Muse Spark — in the Fundi host persona — to wrap each step
in a lesson chapter (title, teach, why, task, celebration) plus a tutor beat
(check-in question, accepted answers, diagnostic question, troubleshoot
chain). Chapters are validated 1:1 against the plan (same count, same n,
same verify echo); anything else is retried once, then replaced by honest
template chapters that teach nothing rather than inventing. Missing beat
and narration fields always fall back to step-derived values, so every
chapter carries a complete beat. The sensor still decides every step;
chapters only narrate. Roll call inventories the live parts data, and
stretch goals dream past DONE — model-written when possible, honestly
derived otherwise.
"""
from __future__ import annotations

import json
import logging
import re

from .prompts import (
    fundi_chapters_system,
    fundi_chapters_user,
    fundi_stretch_system,
    fundi_stretch_user,
)

CHAPTER_FIELDS = ("n", "title", "teach", "why", "task", "verify",
                  "celebration", "question", "answers", "diagnose",
                  "troubleshoot")

_log = logging.getLogger(__name__)

# Diagnostic check-in per verification test: one spoken question that
# points at the most likely cause. Generic across projects — the step's
# own hint carries the measured evidence, this asks the human question.
_DIAGNOSE = {
    "CHECK_GPIO": ("That pin stayed quiet — which GPIO is the signal wire "
                   "on, and is ground firmly connected?"),
    "CHECK_SERIAL": ("Nothing on serial — is the USB cable a data cable, "
                     "and did this step's sketch get flashed?"),
    "RUN_I2C_SCAN": ("The bus scan came back empty — are SDA and SCL "
                     "seated, or could they be swapped?"),
    "READ_SENSOR": ("The sensor isn't answering — is it powered, and are "
                    "SDA and SCL the right way round?"),
    "CHECK_OLED": ("The display didn't acknowledge — is it powered, and "
                   "are SDA and SCL the right way round?"),
}
_DIAGNOSE_DEFAULT = ("That check didn't pass — which wire did you just "
                     "touch, and is it seated firmly?")


def diagnose_for(verify: object) -> str:
    """Diagnostic question for a failed verification test."""
    return _DIAGNOSE.get(str(verify or ""), _DIAGNOSE_DEFAULT)


# Why this step matters: one spoken principle per verification test.
# Generic across projects — the model writes the project-specific
# version; this is the last-resort line that still teaches.
_WHY = {
    "CHECK_GPIO": ("This check proves each wire really reaches its "
                   "GPIO before the build trusts it."),
    "CHECK_SERIAL": ("If the board answers here, every later upload "
                     "and reading has a working channel."),
    "RUN_I2C_SCAN": ("Each address that answers proves its wiring "
                     "before any reading is trusted."),
    "READ_SENSOR": ("A live reading proves power, wiring, and code "
                    "all at once."),
    "CHECK_OLED": ("If the display acknowledges now, every later "
                   "message has somewhere to land."),
}
_WHY_DEFAULT = ("Measure first, then build on what the measurement "
                "proved.")


def _why_for(verify: object) -> str:
    """Fallback principle line for a verification test."""
    return _WHY.get(str(verify or ""), _WHY_DEFAULT)


# Ordered fix steps shown on screen after diagnose is spoken: concrete
# actions that follow the kept diagnostic question, per test.
_TROUBLE_FOLLOWUPS = {
    "CHECK_GPIO": ("Re-seat the signal wire on bare metal: a loose jumper "
                   "reads as a dead pin.",
                   "Confirm the sketch names the same GPIO number the wire "
                   "is on."),
    "CHECK_SERIAL": ("Unplug and re-plug the USB cable, then press CHECK "
                     "NOW again.",
                     "If no port appears, try another cable: charge-only "
                     "cables stay silent."),
    "RUN_I2C_SCAN": ("Power-cycle the board: some sensors only join the "
                     "bus on a fresh boot.",
                     "If one address answers and another doesn't, check "
                     "that sensor's power first."),
    "READ_SENSOR": ("Wait a few seconds and press CHECK NOW again: some "
                    "sensors need a warm-up read.",
                   "Re-seat the power wires: brown-outs read as silence."),
    "CHECK_OLED": ("Confirm the display's address in the scan: 0x3C and "
                   "0x78 are the same chip in different notations.",
                   "Shorten the wiring runs if the bus looks flaky."),
}
_TROUBLE_FOLLOWUPS_DEFAULT = (
    "Re-seat every wire this step touched, then press CHECK NOW again.",
    "If it still fails, describe what you see and I'll narrow it down.")


def _trouble_followups_for(verify: object) -> tuple:
    """Fallback fix steps trailing the diagnostic question."""
    return _TROUBLE_FOLLOWUPS.get(str(verify or ""),
                                 _TROUBLE_FOLLOWUPS_DEFAULT)


def _narration_from_step(step: dict) -> dict:
    """Deterministic narration derived from step data alone."""
    verify = step.get("verify") if isinstance(step, dict) else ""
    return {"why": _why_for(verify),
            "followups": list(_trouble_followups_for(verify))}


def _beat_from_step(step: dict) -> dict:
    """Deterministic tutor beat derived from step data alone.

    The check-in question targets this step's GPIO pins when the
    instruction names any (the learner just wired them), else the
    connected parts. Answers are keyword sets the frontend matches
    tolerantly: each pin as gpio+digit, bare digit, and number word.
    """
    step = step if isinstance(step, dict) else {}
    pins = re.findall(r"GPIO\s*(\d+)", str(step.get("instruction", "")),
                      flags=re.IGNORECASE)
    seen, ordered = set(), []
    for p in pins:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    num_words = {"0": "zero", "1": "one", "2": "two", "3": "three",
                 "4": "four", "5": "five", "6": "six", "7": "seven",
                 "8": "eight", "9": "nine", "10": "ten", "11": "eleven",
                 "12": "twelve", "13": "thirteen", "14": "fourteen",
                 "15": "fifteen", "16": "sixteen", "17": "seventeen",
                 "18": "eighteen", "19": "nineteen", "20": "twenty",
                 "21": "twentyone", "22": "twentytwo", "23": "twentythree"}
    if ordered:
        plural = "s" if len(ordered) != 1 else ""
        question = ("Quick check — which GPIO pin%s did this step's "
                    "wire%s land on?" % (plural, plural))
        answers = []
        for p in ordered:
            answers.append(["gpio", p])
            answers.append([p])
            if p in num_words:
                answers.append([num_words[p]])
    else:
        parts = [str(d) for d in (step.get("depends_on_parts") or [])
                 if str(d).strip()]
        question = "Quick check — what did you just connect?"
        answers = [[w for w in re.findall(r"[a-z0-9]+", part.lower())]
                   for part in parts]
        answers = [a for a in answers if a] or [["done"]]
    return {"question": question, "answers": answers,
            "diagnose": diagnose_for(step.get("verify"))}


def complete_beat(chapter: dict, step: dict) -> dict:
    """Chapter with a full tutor beat: model-written fields stand, any
    missing or empty beat field falls back to the step-derived one."""
    out = dict(chapter) if isinstance(chapter, dict) else {}
    derived = _beat_from_step(step if isinstance(step, dict) else {})
    if not str(out.get("question") or "").strip():
        out["question"] = derived["question"]
    ans = out.get("answers")
    if not isinstance(ans, list) or not [
            a for a in ans
            if isinstance(a, list) and any(str(k).strip() for k in a)]:
        out["answers"] = derived["answers"]
    else:
        out["answers"] = [[str(k).strip().lower() for k in a if str(k).strip()]
                          for a in ans if isinstance(a, list)]
        out["answers"] = [a for a in out["answers"] if a] or derived["answers"]
    if not str(out.get("diagnose") or "").strip():
        out["diagnose"] = derived["diagnose"]
    narration = _narration_from_step(step if isinstance(step, dict) else {})
    if not isinstance(out.get("why"), str) or not out["why"].strip():
        out["why"] = narration["why"]
    tips = out.get("troubleshoot")
    clean = [t.strip() for t in tips
             if isinstance(t, str) and t.strip()] \
        if isinstance(tips, list) else []
    if clean:
        out["troubleshoot"] = clean[:4]
    else:
        out["troubleshoot"] = [out["diagnose"]] + narration["followups"]
    return out


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    return text


def validate_chapters(chapters: object, plan: list) -> list[str]:
    """Return rejection reasons ([] == valid). Pure function of the draft."""
    reasons: list[str] = []
    if not isinstance(chapters, list) or not chapters:
        return ["no chapters returned"]
    if len(chapters) != len(plan):
        return ["%d chapters for %d plan steps (must match 1:1)"
                % (len(chapters), len(plan))]
    for ch, step in zip(chapters, plan):
        n = step.get("n", "?") if isinstance(step, dict) else "?"
        if not isinstance(ch, dict):
            reasons.append("chapter %s is not an object" % n)
            continue
        if ch.get("n") != (step.get("n") if isinstance(step, dict) else None):
            reasons.append("chapter n %r does not echo plan step %s"
                           % (ch.get("n"), n))
        if ch.get("verify") != (step.get("verify") if isinstance(step, dict) else None):
            reasons.append("chapter %s: verify %r does not echo plan %r"
                           % (n, ch.get("verify"),
                              step.get("verify") if isinstance(step, dict) else None))
        for field in ("title", "teach", "task"):
            if not str(ch.get(field) or "").strip():
                reasons.append("chapter %s: missing %s" % (n, field))
    return reasons


def template_chapters(plan: list) -> list[dict]:
    """Honest fallback: no model teaching, the plan task stands alone
    with a step-derived tutor beat (pins the step names, parts it uses)."""
    out = []
    for i, step in enumerate(plan):
        step = step if isinstance(step, dict) else {}
        out.append(complete_beat({
            "n": step.get("n", i + 1),
            "title": "Step %s" % step.get("n", i + 1),
            "teach": "",
            "task": str(step.get("instruction", "")),
            "verify": str(step.get("verify", "")),
            "celebration": "",
        }, step))
    return out


def plan_chapters(plan: list, Muse, project_title: str = "",
                  board: str | None = None) -> list[dict]:
    """One validated chapter per plan step. Never raises: template fallback."""
    if getattr(Muse, "mock", False):
        return template_chapters(plan)
    system = fundi_chapters_system(board)
    base_user = fundi_chapters_user(plan, project_title)
    user_msg = base_user
    for _ in (1, 2):
        try:
            data = json.loads(_strip_fences(Muse.complete_text(system, user_msg)))
            chapters = data.get("chapters") if isinstance(data, dict) else None
            reasons = validate_chapters(chapters, plan)
        except Exception as exc:
            reasons = ["invalid chapters JSON: %s" % exc]
            chapters = None
        if not reasons:
            return [complete_beat(
                        {k: ch.get(k, "") for k in CHAPTER_FIELDS}, step)
                    for ch, step in zip(chapters, plan)]
        user_msg = (base_user + "\nYour last reply was rejected:\n- "
                    + "\n- ".join(reasons)
                    + "\nReply with ONLY the corrected JSON object.")
    _log.warning("fundi chapters fell back to templates: %s",
                 "; ".join(reasons))
    return template_chapters(plan)


def chapter_for(chapters: list, index: int) -> dict | None:
    """Chapter at a plan index (None past the end)."""
    if 0 <= index < len(chapters or []):
        return dict(chapters[index])
    return None


def _task_first(task: str, why: str) -> str:
    """Fallback clause order: the action, then the principle."""
    return " ".join(p for p in (task, why) if p)


def next_step_opening(chapter: dict) -> str:
    """Template turn to the next step: action first, principle after."""
    chapter = chapter if isinstance(chapter, dict) else {}
    return "Next step %s: %s" % (
        chapter.get("n", "?"),
        _task_first(str(chapter.get("task") or "").strip(),
                    str(chapter.get("why") or "").strip()))


def speak_start(chapter: dict, project_title: str) -> str:
    """Brief opening: the idea, why it matters, then the task.

    Template chapters have no teach lead-in, so the task speaks
    first — the learner hears what to do before why it matters.
    """
    teach = str(chapter.get("teach") or "").strip()
    why = str(chapter.get("why") or "").strip()
    task = str(chapter.get("task") or "").strip()
    if not teach:
        return "Step %s: %s" % (chapter.get("n", "?"),
                                _task_first(task, why))
    return " ".join(p for p in (teach, why, task) if p)


_ROLLCALL_ANSWERS = [["ready"], ["yes"], ["yep"], ["done"], ["go"],
                     ["let's go"], ["all here"], ["we are ready"]]
_ROLLCALL_HOLD = re.compile(
    r"\b(not|no|never|n't|wait|hold|stop|later|minute|moment)\b")


def _normalize_speech(text: str) -> str:
    """Lowercase speech with punctuation folded to spaces."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def rollcall_accepts(message: str, answers: list) -> bool:
    """True when the learner's reply confirms readiness.

    Any hold/negation word vetoes first ("not ready", "wait"); then
    one accepted phrase must match whole ("go" must not fire on
    "good").
    """
    said = _normalize_speech(
        re.sub(r"n['\u2019]t\b", " not", str(message or "").lower()))
    if not said or _ROLLCALL_HOLD.search(said):
        return False
    hay = " %s " % said
    for alt in (answers or []):
        phrase = _normalize_speech(" ".join(str(k) for k in alt)
                                   if isinstance(alt, list) else alt)
        if phrase and (" %s " % phrase) in hay:
            return True
    return False


def complete_rollcall(rollcall: dict | None, visible_parts: list,
                      missing_parts: list) -> dict:
    """Roll call over the live parts inventory: read it aloud, get READY.

    Nothing to invent — the bench data IS the content — so the roll
    call is always derived. A model-written rollcall stands only when
    fully valid; anything else falls back to the derived inventory.
    """
    visible = [str(p).strip() for p in (visible_parts or [])
               if str(p).strip()]
    missing = [str(p).strip() for p in (missing_parts or [])
               if str(p).strip()]
    total = len(visible) + len(missing)
    if total == 0:
        say = ("Roll call: I can't see your parts list yet, so gather "
               "what the plan names. Say READY when everything is on "
               "the bench.")
        question = "Is everything on your bench?"
    elif missing:
        names = ", ".join(visible + missing)
        if visible:
            say = ("Roll call: you need %d things: %s. I can see %d: %s. "
                   "Still missing: %s. Say READY when everything is on "
                   "the bench."
                   % (total, names, len(visible), ", ".join(visible),
                      ", ".join(missing)))
        else:
            say = ("Roll call: you need %d things: %s. Nothing is on the "
                   "bench yet — still missing: %s. Say READY when "
                   "everything is on the bench."
                   % (total, names, ", ".join(missing)))
        question = "Are all %d parts on your bench?" % total
    else:
        say = ("Roll call: all %d things are here: %s. Say READY and we "
               "build." % (total, ", ".join(visible)))
        question = ("All %d parts are here — say READY and we build?"
                    % total)
    rc = rollcall if isinstance(rollcall, dict) else {}
    say_out = str(rc.get("say") or "").strip() or say
    question_out = str(rc.get("question") or "").strip() or question
    ans = rc.get("answers")
    if (isinstance(ans, list) and
            [a for a in ans
             if isinstance(a, list) and any(str(k).strip() for k in a)]):
        answers = [[str(k).strip().lower() for k in a if str(k).strip()]
                   for a in ans if isinstance(a, list)]
        answers = [a for a in answers if a] or _ROLLCALL_ANSWERS
    else:
        answers = [list(a) for a in _ROLLCALL_ANSWERS]
    return {"say": say_out, "question": question_out, "answers": answers}


def stretch_fallback(project_title: str, plan: list) -> list[dict]:
    """Honest stretch goals: grounded in the project, skipping ideas
    the plan already covers. Always returns 1-2 suggestions."""
    text = " ".join(
        "%s %s" % (s.get("instruction", ""), s.get("verify", ""))
        for s in (plan or []) if isinstance(s, dict)).lower()
    title = str(project_title or "").strip() or "your build"
    ideas = []
    if not any(k in text for k in ("display", "oled", "lcd", "screen",
                                  "tft")):
        ideas.append({
            "title": "Add a live display readout",
            "summary": ("Show what %s is sensing on a small OLED or LCD, "
                        "so the numbers live on the bench instead of the "
                        "serial monitor." % title)})
    if not any(k in text for k in ("log", "sd card", "sdcard", "csv",
                                  "record", "mqtt", "wifi", "cloud",
                                  "thingspeak")):
        ideas.append({
            "title": "Log readings over time",
            "summary": ("Save %s's timestamped readings so you can graph "
                        "a full day of data and spot patterns." % title)})
    ideas.append({
        "title": "Cut the USB cord",
        "summary": ("Take %s portable with a battery pack and a power "
                    "switch — same circuit, no laptop tether." % title)})
    return ideas[:2]


def _validate_stretch(stretch: object) -> tuple[list[dict], list[str]]:
    """Split a model stretch draft into usable ideas + rejection reasons."""
    if not isinstance(stretch, list) or not stretch:
        return [], ["no stretch ideas returned"]
    ideas = []
    for item in stretch:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if title and summary:
            ideas.append({"title": title, "summary": summary})
    if not ideas:
        return [], ["no usable stretch idea (need title + summary)"]
    return ideas[:2], []


def plan_stretch(project_title: str, plan: list, visible_parts: list,
                 missing_parts: list, brain,
                 board: str | None = None) -> list[dict]:
    """DONE-state stretch goals. Model-written when possible, grounded
    fallback otherwise. Never raises."""
    if getattr(brain, "mock", False):
        return stretch_fallback(project_title, plan)
    system = fundi_stretch_system(board)
    base_user = fundi_stretch_user(project_title, plan, visible_parts,
                                  missing_parts)
    user_msg = base_user
    for _ in (1, 2):
        try:
            data = json.loads(_strip_fences(
                brain.complete_text(system, user_msg)))
            stretch = data.get("stretch") if isinstance(data, dict) else None
            ideas, reasons = _validate_stretch(stretch)
        except Exception as exc:
            ideas, reasons = [], ["invalid stretch JSON: %s" % exc]
        if not reasons:
            return ideas
        user_msg = (base_user + "\nYour last reply was rejected:\n- "
                    + "\n- ".join(reasons)
                    + "\nReply with ONLY the corrected JSON object.")
    return stretch_fallback(project_title, plan)
