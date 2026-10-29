"""Generative discovery: scene understanding + validated project plans.

No project catalog: SAM grounds part regions, Muse Spark interprets the
scene and drafts plans, and Python validators reject anything that is not
verifiable against AVAILABLE_TESTS. understand_scene never raises; invalid
plans are retried once, then returned as rejected_plans for debugging.
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .adapters.muse_adapter import MetaClaudeAdapter
from .adapters.sam_adapter import MetaSAMAdapter
from .capabilities import AVAILABLE_TESTS, KNOWN_PARTS, SAM_PROMPT_PARTS
from .prompts import plan_retry_suffix, plan_system_prompt, scene_system_prompt

ROOT = Path(__file__).resolve().parent.parent.parent
MAX_FRAME_BYTES = 15 * 1024 * 1024


def _norm(name: str) -> str:
    return re.sub(r"\s+", "_", str(name or "").strip().lower())


def _match(part_id: str, label: str) -> bool:
    tokens = set(_norm(label).split("_"))
    return _norm(part_id) in tokens


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    return text


def _read_frame(frame_path: str) -> tuple[bytes, str]:
    """Return (raw_bytes, base64). Raises on missing/oversize files."""
    path = Path(frame_path)
    if not path.is_absolute():
        path = ROOT / frame_path
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_FRAME_BYTES:
        raise ValueError("frame missing or too large: %s" % frame_path)
    return raw, base64.b64encode(raw).decode("ascii")


def _empty_scene(reason: str) -> dict:
    return {"visible_parts": [], "connections_observed": [],
            "uncertainties": [reason], "notes": "",
            "sam_prompts_empty": [], "sam_confirmed_count": 0}


def _ground_prompts(sam, frame: bytes, frame_path: str,
                    prompts: list[str]) -> list[dict]:
    """One SAM call per prompt, overlapped; results in prompt order.

    ex.map preserves input order and raises the first worker exception,
    so the merged scene is identical to the sequential loop at roughly
    1/8th of the wall time.
    """

    def one(prompt: str) -> dict:
        out = sam.analyze(frame, frame_path=frame_path,
                          concepts=[(prompt, prompt)])
        boxes = [{"label": d.get("label", prompt), "box": d.get("box"),
                  "confidence": d.get("confidence", 0.0),
                  **({"mask": d["mask_svg"]} if d.get("mask_svg") else {})}
                 for d in out.get("detections", [])]
        return {"prompt": prompt, "boxes": boxes}

    workers = max(1, min(8, len(prompts)))
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="fundiloop-sam") as ex:
        return list(ex.map(one, prompts))


def understand_scene(frame_path: str) -> dict:
    """SAM regions + Muse reading merged into one scene description.

    Never raises on model or parse failure: uncertainties explains.
    """
    try:
        frame, b64 = _read_frame(frame_path)
    except Exception as exc:
        return _empty_scene("cannot read frame: %s" % exc)
    prompts: list[str] = []
    for part in SAM_PROMPT_PARTS + ["jumper wire", "breadboard"]:
        phrase = part.replace("_", " ")
        if phrase not in prompts:
            prompts.append(phrase)
    sam = MetaSAMAdapter()
    try:
        sam_results = _ground_prompts(sam, frame, frame_path, prompts)
    except Exception as exc:
        return _empty_scene("discovery unavailable: %s" % exc)
    empty = [r["prompt"] for r in sam_results if not r["boxes"]]
    brief = [{"prompt": r["prompt"],
              "boxes": [{k: b[k] for k in ("label", "box", "confidence") if k in b}
                        for b in r["boxes"]]} for r in sam_results]
    adapter = MetaClaudeAdapter()
    system = scene_system_prompt(brief, KNOWN_PARTS)
    try:
        try:
            raw = adapter.complete_text(system, "Describe the scene in the required JSON format.",
                                        image_b64=b64)
        except Exception:
            raw = adapter.complete_text(
                system + "\n(The frame image could not be attached; read the SAM regions only.)",
                "Describe the scene in the required JSON format.")
    except Exception as exc:
        return _empty_scene("discovery unavailable: %s" % exc)
    try:
        data = json.loads(_strip_fences(raw))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except Exception:
        return _empty_scene("scene understanding failed: %s" % raw[:200])
    parts, uncertainties = [], list(data.get("uncertainties") or [])
    sam_labels = [(r["prompt"], b) for r in sam_results for b in r["boxes"]]
    for entry in data.get("visible_parts") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        try:
            conf = min(1.0, max(0.0, float(entry.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        matched = [b for prompt, b in sam_labels if _match(entry["name"], b.get("label", prompt))]
        if conf < 0.6:
            uncertainties.append("%s below confidence threshold (%.2f)" % (entry["name"], conf))
        parts.append({"name": entry["name"], "confidence": conf,
                      "evidence": str(entry.get("evidence", "")),
                      "state": str(entry.get("state", "")),
                      "sam_confirmed": bool(matched), "sam_boxes": matched})
    named = {_norm(p["name"]) for p in parts}
    for prompt, box in sam_labels:
        if not any(_match(n, box.get("label", prompt)) or _norm(n) == _norm(prompt) for n in named):
            parts.append({"name": "unknown", "confidence": 0.5,
                          "evidence": "SAM box with no Muse label",
                          "state": "unidentified", "sam_confirmed": True,
                          "sam_boxes": [box]})
    conns = data.get("connections_observed") or []
    return {
        "visible_parts": parts,
        "connections_observed": conns if isinstance(conns, list) else [],
        "uncertainties": [str(u) for u in uncertainties],
        "notes": str(data.get("notes") or ""),
        "sam_prompts_empty": empty,
        "sam_confirmed_count": sum(1 for p in parts if p["sam_confirmed"]),
    }


def detect_parts(frame_path: str) -> dict:
    """Backward-compatible wrapper: visible part names only."""
    return {"visible_parts": understand_scene(frame_path)["visible_parts"]}


def validate_project(proj: dict, visible: list[str]) -> list[str]:
    """Return rejection reasons ([] == valid). Pure function of the plan."""
    reasons: list[str] = []
    if not isinstance(proj, dict) or not isinstance(proj.get("steps"), list) or not proj["steps"]:
        return ["project has no steps"]
    missing = [str(m) for m in (proj.get("missing_parts") or [])]

    def check_steps(steps: list, tag: str, allow_missing: bool) -> None:
        for i, step in enumerate(steps):
            label = "%sstep %s" % (tag, step.get("n", i + 1) if isinstance(step, dict) else i + 1)
            if not isinstance(step, dict):
                reasons.append("%s is not an object" % label)
                continue
            if step.get("verify") not in AVAILABLE_TESTS:
                reasons.append("%s: unknown verify %r (must be one of %s)"
                               % (label, step.get("verify"), ", ".join(AVAILABLE_TESTS)))
            if not step.get("instruction"):
                reasons.append("%s: missing instruction" % label)
            deps = step.get("depends_on_parts") or []
            if not isinstance(deps, list):
                reasons.append("%s: depends_on_parts is not a list" % label)
                continue
            for dep in deps:
                if dep not in visible and (not allow_missing or dep not in missing):
                    reasons.append("%s: part %r not in visible_parts%s"
                                   % (label, dep, " and not in missing_parts" if allow_missing else ""))
            unverified = [d for d in deps if d not in KNOWN_PARTS]
            if unverified and step.get("verify") not in ("CHECK_SERIAL", "CHECK_GPIO"):
                reasons.append("%s: unverified part(s) %s require CHECK_SERIAL or CHECK_GPIO"
                               % (label, ", ".join(unverified)))

    check_steps(proj["steps"], "", True)
    required = {d for s in proj["steps"] if isinstance(s, dict) for d in (s.get("depends_on_parts") or [])}
    lacking = sorted(required - set(visible))
    if lacking and not proj.get("alternatives"):
        reasons.append("parts %s not visible: list them in missing_parts and include an alternative"
                       % ", ".join(lacking))
    for alt in proj.get("alternatives") or []:
        if not isinstance(alt, dict):
            reasons.append("alternative is not an object")
            continue
        check_steps(alt.get("steps") or [], "alternative %r " % alt.get("title", "?"), False)
    return reasons


def plan_projects(visible_parts: list[str], user_project: str | None = None,
                  constraints: str | None = None, connections_observed: list | None = None,
                  uncertainties: list | None = None,
                  board: str | None = None) -> dict:
    """Generate validated build plans (model proposes, validator disposes)."""
    visible = [str(v) for v in (visible_parts or [])]
    system = plan_system_prompt(AVAILABLE_TESTS, visible, connections_observed or [],
                                uncertainties or [], user_project, constraints,
                                board)
    base_user = ("Plan this build: %s." % user_project) if user_project \
        else "Suggest up to 3 buildable projects."
    user_msg, rejected, session_id = base_user, [], uuid.uuid4().hex
    Muse = MetaClaudeAdapter()
    for attempt in (1, 2):
        try:
            data = json.loads(_strip_fences(Muse.complete_text(system, user_msg)))
            projects = data.get("projects") if isinstance(data, dict) else None
            if not projects:
                raise ValueError("model returned no projects")
        except Exception as exc:
            reasons = ["attempt %d: %s" % (attempt, exc)]
            rejected.append({"attempt": attempt, "title": "", "reasons": reasons})
            user_msg = base_user + "\n" + plan_retry_suffix(reasons)
            continue
        valid: list[dict] = []
        for proj in projects:
            title = proj.get("title", "?") if isinstance(proj, dict) else "?"
            reasons = validate_project(proj, visible) if isinstance(proj, dict) \
                else ["project is not an object"]
            if reasons:
                rejected.append({"attempt": attempt, "title": title, "reasons": reasons})
            else:
                proj["session_id"] = session_id
                valid.append(proj)
        if valid:
            return {"projects": valid, "rejected_plans": rejected}
        user_msg = base_user + "\n" + plan_retry_suffix(
            [r for entry in rejected if entry["attempt"] == attempt for r in entry["reasons"]])
    return {"projects": [], "rejected_plans": rejected}
