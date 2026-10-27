"""Muse Spark diagnostic client."""
import json
import os

BASE_URL = "https://api.meta.ai/v1"
MODEL = "muse-spark-1.3"

SYSTEM_PROMPT = """You are FundiLoop, a diagnostic agent for low-voltage DC hardware.
A technician is repairing a dead circuit with live sensor data and one camera frame.
Return ONLY valid JSON matching this schema:
{"state": "DIAGNOSING|INSTRUCTING|VERIFYING|DONE",
 "fault_hypothesis": str, "confidence": 0.0-1.0,
 "evidence": [str], "next_instruction": str,
 "verification_test": "RUN_I2C_SCAN|CHECK_SERIAL|CHECK_OLED|NONE",
 "speak_to_tech": str}
Rules: never declare success yourself. The backend runs verification_test
and the sensor reading decides. Keep speak_to_tech short and speakable."""


def diagnose(req) -> "object":
    """Call Muse Spark and parse the result into DiagnosticResponse."""
    from openai import OpenAI

    from .schemas import DiagnosticResponse

    api_key = os.environ.get("MODEL_API_KEY", "")
    if not api_key:
        raise RuntimeError("MODEL_API_KEY is not set")
    client = OpenAI(base_url=BASE_URL, api_key=api_key)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in req.conversation_history or []:
        if isinstance(turn, dict) and "role" in turn and "content" in turn:
            messages.append({"role": turn["role"], "content": str(turn["content"])})
    messages.append(
        {
            "role": "user",
            "content": json.dumps(
                {
                    "frame_path": req.frame_path,
                    "probe_result": req.probe_result.model_dump(),
                }
            ),
        }
    )
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            response_format={"type": "json_object"},
        )
    except Exception:
        resp = client.chat.completions.create(model=MODEL, messages=messages)
    raw = (resp.choices[0].message.content or "").strip()
    try:
        return DiagnosticResponse.model_validate_json(raw)
    except Exception as exc:
        raise ValueError(f"Bad model JSON: {exc}\nRAW:\n{raw}") from exc
