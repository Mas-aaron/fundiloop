"""Meta SAM adapter: segment the overhead camera frame.

MOCK mode (default) returns realistic sample detections so the whole loop
runs offline. Real mode (MOCK_META=false + META_API_KEY) sends the frame
to SAM 3.1 following the legacy backend/sam_client.py pattern: an
OpenAI-compatible chat call with the image attached. Model from SAM_MODEL.
"""
from __future__ import annotations

import base64
import json
import os
import re

from . import MetaAPIError, api_base_url, api_key, api_timeout


def _mock_enabled() -> bool:
    if os.environ.get("MOCK_META", "true").lower() in ("1", "true", "yes"):
        return True
    return not os.environ.get("META_API_KEY")


SAM_PROMPT = """Segment this overhead view of an electronics workbench.
Return ONLY valid JSON, no prose:
{"detections": [{"label": str, "box": [x1, y1, x2, y2], "confidence": 0.0-1.0}],
 "wire_count": int, "wires_ok": bool, "oled_lit": bool, "notes": str}
Boxes are pixel coordinates in the input image.
Labels of interest: ESP32 DevKit, BME280 module, SSD1306 OLED, LED (GPIO2), jumper wire."""


class MetaSAMAdapter:
    def __init__(self, model: str | None = None, mock: bool | None = None) -> None:
        self.model = model or os.environ.get("SAM_MODEL", "sam-3.1")
        self.mock = _mock_enabled() if mock is None else mock

    def analyze(self, frame: bytes, prompt: str = SAM_PROMPT) -> dict:
        """Segment one settled frame. Returns detections + wiring notes."""
        if self.mock or not frame:
            return self._mock_result()
        return self._real_analyze(frame, prompt)

    def _real_analyze(self, frame: bytes, prompt: str) -> dict:
        from openai import OpenAI

        if not api_key():
            raise MetaAPIError("META_API_KEY is not set")
        b64 = base64.b64encode(frame).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,%s" % b64}},
            ],
        }]
        try:
            client = OpenAI(base_url=api_base_url(), api_key=api_key(), timeout=api_timeout())
            try:
                resp = client.chat.completions.create(
                    model=self.model, messages=messages,
                    response_format={"type": "json_object"})
            except Exception:
                resp = client.chat.completions.create(model=self.model, messages=messages)
        except Exception as exc:
            raise MetaAPIError("SAM request failed: %s" % exc) from exc
        text = (resp.choices[0].message.content or "").strip()
        return self._parse_result(text)

    @staticmethod
    def _parse_result(text: str) -> dict:
        payload = text
        if "```" in payload:  # strip code fences if the model adds them
            payload = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", payload.strip())
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", payload, re.DOTALL)
            try:
                data = json.loads(m.group(0)) if m else None
            except json.JSONDecodeError:
                data = None
        if not isinstance(data, dict):
            raise MetaAPIError("SAM returned unparseable output: %.120s" % text)
        detections = []
        for item in data.get("detections", []) or []:
            try:
                box = [float(x) for x in item.get("box", [])][:4]
                if len(box) != 4:
                    continue
                detections.append({
                    "label": str(item.get("label", "component")),
                    "box": [int(v) for v in box],
                    "confidence": min(1.0, max(0.0, float(item.get("confidence", 0.5)))),
                })
            except (TypeError, ValueError, AttributeError):
                continue
        return {
            "mock": False,
            "detections": detections,
            "wire_count": int(data.get("wire_count", 0) or 0),
            "wires_ok": bool(data.get("wires_ok", True)),
            "oled_lit": bool(data.get("oled_lit", False)),
            "notes": str(data.get("notes", "")),
        }

    def _mock_result(self) -> dict:
        return {
            "model": self.model,
            "mock": True,
            "detections": [
                {"label": "ESP32 DevKit", "box": [70, 110, 310, 390], "confidence": 0.97},
                {"label": "BME280 module", "box": [360, 140, 470, 240], "confidence": 0.93},
                {"label": "SSD1306 OLED", "box": [360, 270, 500, 360], "confidence": 0.91},
                {"label": "LED (GPIO2)", "box": [520, 200, 560, 260], "confidence": 0.88},
            ],
            "wire_count": 8,
            "wires_ok": True,
            "oled_lit": True,
            "notes": "Mock segmentation: ESP32, BME280, OLED, and LED all visible; 8 jumpers seated.",
        }
