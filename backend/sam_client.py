"""Thin SAM 3.1 client. Streams one concept segmentation (cookbook flow)."""
import asyncio
import base64
import os

BASE_URL = "https://api.meta.ai/v1"
MODEL = "sam-3.1"


async def _segment(image_path: str, concept: str) -> dict:
    from meta_sam_parser import image_segmentation_format, parse_responses_stream
    from openai import AsyncOpenAI

    api_key = os.environ.get("MODEL_API_KEY", "")
    if not api_key:
        raise RuntimeError("MODEL_API_KEY is not set")
    with open(image_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    client = AsyncOpenAI(base_url=BASE_URL, api_key=api_key)
    async with client:
        stream = await client.responses.create(
            model=MODEL,
            stream=True,
            input=[{
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": concept},
                    {"type": "input_image",
                     "image_url": f"data:image/jpeg;base64,{b64}"},
                ],
            }],
            metadata={"include_confidence": "true"},
        )
        async with stream:
            result = await parse_responses_stream(
                stream, image_segmentation_format()).final_result()
    boxes = [r for r in result.records if r.kind == "box"]
    return {"model": MODEL, "concept": concept,
            "outcome": result.outcome.status,
            "boxes": [[r.left, r.top, r.right, r.bottom] for r in boxes]}


def segment(image_path: str, concept: str) -> dict:
    """Segment one concept in an image. Returns boxes in source pixels."""
    return asyncio.run(_segment(image_path, concept))
