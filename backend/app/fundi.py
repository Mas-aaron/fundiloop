"""Fundi chapter delivery: teach-first lesson chapters over validated plans.

The plan steps (instruction + verify test) already exist and are validated.
This module asks Muse Spark — in the Fundi host persona — to wrap each step
in a lesson chapter (title, teach, task, celebration). Chapters are
validated 1:1 against the plan (same count, same n, same verify echo);
anything else is retried once, then replaced by honest template chapters
that teach nothing rather than inventing. The sensor still decides every
step; chapters only narrate.
"""
from __future__ import annotations

import json
import re

from .prompts import fundi_chapters_system, fundi_chapters_user

CHAPTER_FIELDS = ("n", "title", "teach", "task", "verify", "celebration")


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
    """Honest fallback: no model teaching, the plan task stands alone."""
    out = []
    for i, step in enumerate(plan):
        step = step if isinstance(step, dict) else {}
        out.append({
            "n": step.get("n", i + 1),
            "title": "Step %s" % step.get("n", i + 1),
            "teach": "",
            "task": str(step.get("instruction", "")),
            "verify": str(step.get("verify", "")),
            "celebration": "",
        })
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
            return [{k: ch.get(k, "") for k in CHAPTER_FIELDS} for ch in chapters]
        user_msg = (base_user + "\nYour last reply was rejected:\n- "
                    + "\n- ".join(reasons)
                    + "\nReply with ONLY the corrected JSON object.")
    return template_chapters(plan)


def chapter_for(chapters: list, index: int) -> dict | None:
    """Chapter at a plan index (None past the end)."""
    if 0 <= index < len(chapters or []):
        return dict(chapters[index])
    return None


def speak_start(chapter: dict, project_title: str) -> str:
    """Brief opening: the idea, then the task. No ceremony (template-safe)."""
    teach = str(chapter.get("teach") or "").strip()
    task = str(chapter.get("task") or "").strip()
    if not teach:
        return "Starting %s. Step %s: %s" % (
            project_title, chapter.get("n", "?"), task)
    return "%s %s" % (teach, task)


def speak_passed(chapter: dict, measured: str, next_chapter: dict | None,
                 project_title: str, praise: str) -> str:
    """Celebration for a sensor-verified chapter, then the next opening."""
    cheer = str(chapter.get("celebration") or "").strip() or praise
    text = "%s Verified: %s." % (cheer, measured)
    if next_chapter is None:
        return text
    if str(next_chapter.get("teach") or "").strip():
        return text + " Next: " + speak_start(next_chapter, project_title)
    return "%s Next step %s: %s" % (
        text, next_chapter.get("n", "?"),
        str(next_chapter.get("task") or "").strip())
