"""Thin SAM 3.1 client. Returns raw text for Day 1 inspection."""
import base64
import os

BASE_URL = "https://api.meta.ai/v1"
MODEL = "sam-3.1"


def segment(image_path: str, prompt: str) -> dict:
    """Send one image + prompt to SAM 3.1, return raw response text."""
    from openai import OpenAI

    api_key = os.environ.get("MODEL_API_KEY", "")
    if not api_key:
        raise RuntimeError("MODEL_API_KEY is not set")
    with open(image_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    client = OpenAI(base_url=BASE_URL, api_key=api_key)
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
    )
    text = resp.choices[0].message.content or ""
    # NOTE: box/mask parsing intentionally deferred; inspect raw text first.
    return {"text": text, "model": MODEL}
