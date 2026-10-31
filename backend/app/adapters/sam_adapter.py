"""Meta SAM adapter: segment the overhead camera frame, and track a clip.

Real mode (MOCK_META=false) follows the official Meta cookbook
(meta-models/meta-model-cookbook, 07_sam/01_api_basics): one streamed
/v1/responses request per concept noun phrase, decoded with meta_sam_parser
(the SAM 3 output grammar is never parsed by hand). Requests run
sequentially exactly like the cookbook; boxes carry source-pixel
coordinates and per-record confidence (via metadata include_confidence).
Model from SAM_MODEL, base URL from META_API_BASE_URL. Every concept call
is logged to backend/data/logs/model_calls.jsonl. Mock mode answers offline
but raises when MOCK_META=false so real runs can never simulate vision.

`analyze` segments one still (input_image). `analyze_clip` is the live-video
half: the same model, the same request, an `input_video` part instead. SAM
3.1 keeps each match's object id stable across frames, so the video parser
(video_segmentation_format) yields per-frame box records that group into
tracks -- that is what lets the agent follow a part through a clip.

The transport is the synchronous OpenAI client and the parser's direct
push()/finish() API, so no event loop is ever needed.
"""
from __future__ import annotations

import base64
import os
import threading
import time
from pathlib import Path

from ..frames import PreparedFrame, prepare_frame, rescale_detections
from . import MetaAPIError, api_base_url, api_key, api_timeout, log_model_call, mock_meta_enabled


def _mock_enabled() -> bool:
    return mock_meta_enabled()


# (concept noun phrase sent to SAM, display label for detections)
#
# The default is deliberately ONE generic prompt, not a fixed list of part
# names: whatever equipment is on the bench gets boxed, and Muse names what
# it actually sees from the frame. Callers that need per-part precision
# (scene understanding, probes) pass explicit concepts.
SAM_CONCEPTS: tuple[tuple[str, str], ...] = (
    ("electronic components on the bench", "component"),
)

# Live-video tracking uses the same single generic prompt for the same
# reason: name one concept, follow it across the clip.
DEFAULT_VIDEO_CONCEPT = "electronic components on the bench"
DEFAULT_VIDEO_MIME = "video/mp4"


def to_data_url(mime: str, raw: bytes) -> str:
    """Base64 data URL accepted by input_image / input_video media parts."""
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


class MetaSAMAdapter:
    def __init__(self, model: str | None = None, mock: bool | None = None) -> None:
        self.model = model or os.environ.get("SAM_MODEL", "sam-3.1")
        self.mock = _mock_enabled() if mock is None else mock
        self._client = None  # persistent OpenAI client (lazy, thread-safe)
        self._client_lock = threading.Lock()

    def _api_client(self):
        """One shared client per adapter: keeps the TLS session warm.

        Base URL, key, and timeout are read on first use; a backend restart
        picks up changed values. The client is safe for concurrent calls.
        """
        if self._client is None:
            from openai import OpenAI

            with self._client_lock:
                if self._client is None:
                    # No SDK retries: the app owns retry policy, and client
                    # retries multiply tail latency (30s -> 90s hangs).
                    self._client = OpenAI(base_url=api_base_url(), api_key=api_key(),
                                          timeout=api_timeout(), max_retries=0)
        return self._client

    def analyze(
        self,
        frame: bytes,
        context: dict | None = None,
        frame_path: str | Path | None = None,
        concepts: list[tuple[str, str]] | None = None,
    ) -> dict:
        """Segment one settled frame, one streamed request per concept.

        When frame_path is given, the bytes are read back from that saved
        debug frame so the API provably receives the file on disk.
        """
        if self.mock:
            if not mock_meta_enabled():
                raise MetaAPIError("mock SAM adapter called with MOCK_META=false")
            return self._mock_result(context or {})
        if frame_path:
            frame = Path(frame_path).read_bytes()
        if not frame:
            raise MetaAPIError("SAM called with an empty frame")
        return self._real_analyze(frame, str(frame_path or ""), concepts or list(SAM_CONCEPTS))

    def analyze_clip(
        self,
        clip: bytes,
        context: dict | None = None,
        video_path: str | Path | None = None,
        concept: str | None = None,
        mime: str = DEFAULT_VIDEO_MIME,
    ) -> dict:
        """Track one concept through a video clip (SAM 3.1, input_video).

        Same request as analyze(), an input_video part instead of input_image.
        Returns per-object tracks (box per frame) plus the last frame's
        detections. When video_path is given the bytes are read back from that
        saved clip so the API provably receives the file on disk.
        """
        if self.mock:
            if not mock_meta_enabled():
                raise MetaAPIError("mock SAM adapter called with MOCK_META=false")
            return self._mock_clip_result(context or {}, concept or DEFAULT_VIDEO_CONCEPT)
        if video_path:
            clip = Path(video_path).read_bytes()
        if not clip:
            raise MetaAPIError("SAM called with an empty clip")
        return self._real_clip(clip, str(video_path or ""), concept or DEFAULT_VIDEO_CONCEPT, mime)

    def _real_clip(self, clip: bytes, clip_file: str, concept: str, mime: str) -> dict:
        try:
            import meta_sam_parser as parser_mod
        except ImportError:
            raise MetaAPIError(
                "meta_sam_parser is not installed "
                "(pip install meta-sam-parser): SAM output cannot be decoded") from None
        if not api_key():
            raise MetaAPIError("META_API_KEY is not set")
        started = time.monotonic()
        try:
            return self._track_one(self._api_client(), parser_mod, clip, concept,
                                   mime, clip_file, started)
        except MetaAPIError:
            raise
        except Exception as exc:
            latency_ms = (time.monotonic() - started) * 1000
            log_model_call(self.model, latency_ms, "error", clip_file,
                           raw_summary="", ok=False, error=str(exc))
            raise MetaAPIError("SAM clip request failed: %s" % exc) from exc

    def _real_analyze(self, frame: bytes, frame_file: str,
                      concepts: list[tuple[str, str]]) -> dict:
        try:
            import meta_sam_parser as parser_mod
        except ImportError:
            raise MetaAPIError(
                "meta_sam_parser is not installed "
                "(pip install meta-sam-parser): SAM output cannot be decoded") from None
        if not api_key():
            raise MetaAPIError("META_API_KEY is not set")
        prep = prepare_frame(frame)
        b64 = base64.b64encode(prep.payload).decode("ascii")
        started = time.monotonic()
        try:
            per_concept = self._segment_concepts(b64, concepts, parser_mod)
        except MetaAPIError:
            raise
        except Exception as exc:
            latency_ms = (time.monotonic() - started) * 1000
            log_model_call(self.model, latency_ms, "error", frame_file,
                           raw_summary="", ok=False, error=str(exc))
            raise MetaAPIError("SAM request failed: %s" % exc) from exc
        total_ms = (time.monotonic() - started) * 1000
        return self._combine(per_concept, frame_file, total_ms, parser_mod, prep)

    def _segment_concepts(self, b64: str, concepts: list[tuple[str, str]],
                          parser_mod) -> list[dict]:
        """Run one streamed cookbook request per concept, sequentially."""
        client = self._api_client()
        return [self._segment_one(client, parser_mod, b64, concept, label)
                for concept, label in concepts]

    def _segment_one(self, client, parser_mod, b64: str,
                     concept: str, label: str) -> dict:
        """One concept request: streamed responses call, parser push/finish."""
        started = time.monotonic()
        stream = client.responses.create(
            model=self.model,
            stream=True,
            timeout=api_timeout(),
            input=[{
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": concept},
                    {"type": "input_image",
                     "image_url": "data:image/jpeg;base64,%s" % b64},
                ],
            }],
            metadata={"include_confidence": "true"},
        )
        texts, response_id = self._collect_output_texts(
            stream, deadline_s=api_timeout())
        parser = parser_mod.image_segmentation_format().create_parser()
        for text in texts:
            for _snapshot in parser.push(text):
                pass
        result = parser.finish(parser_mod.CompletedOutcome()).result
        latency_ms = (time.monotonic() - started) * 1000
        outcome = getattr(result, "outcome", None)
        status = getattr(outcome, "status", "completed")
        if status != "completed":
            raise MetaAPIError("SAM outcome for %r: %s (%s)" % (
                concept, status, getattr(outcome, "reason", "unknown")))
        return {"concept": concept, "label": label, "result": result,
                "response_id": response_id, "latency_ms": latency_ms}

    def _track_one(self, client, parser_mod, clip: bytes, concept: str,
                   mime: str, clip_file: str, started: float) -> dict:
        """One video concept request, decoded with the video grammar."""
        stream = client.responses.create(
            model=self.model,
            stream=True,
            timeout=api_timeout(),
            input=[{
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": concept},
                    {"type": "input_video",
                     "video_url": to_data_url(mime, clip)},
                ],
            }],
            metadata={"include_confidence": "true"},
        )
        texts, response_id = self._collect_output_texts(
            stream, deadline_s=api_timeout())
        parser = parser_mod.video_segmentation_format().create_parser()
        for text in texts:
            for _snapshot in parser.push(text):
                pass
        result = parser.finish(parser_mod.CompletedOutcome()).result
        latency_ms = (time.monotonic() - started) * 1000
        outcome = getattr(result, "outcome", None)
        status = getattr(outcome, "status", "completed")
        if status != "completed":
            raise MetaAPIError("SAM clip outcome for %r: %s (%s)" % (
                concept, status, getattr(outcome, "reason", "unknown")))
        return self._combine_tracks(result, concept, clip_file, latency_ms, response_id)

    @staticmethod
    def _collect_output_texts(stream, deadline_s: float | None = None) -> tuple:
        """Drain one streamed responses call into (texts, response_id).

        The SAM cookbook emits a single output_text lane; deltas and the
        finalized content part are both read so either shape decodes.
        deadline_s caps the TOTAL drain wall time (the SDK timeout only
        bounds each idle read, so a slow-dripping stream could otherwise
        run for many minutes); on expiry TimeoutError propagates and the
        caller degrades that prompt.
        """
        lanes: dict = {}
        parts: dict = {}
        response_id = ""
        started = time.monotonic()
        with stream:
            for event in stream:
                if deadline_s is not None \
                        and time.monotonic() - started > deadline_s:
                    raise TimeoutError(
                        "SAM stream exceeded its total deadline")
                data = event.model_dump() if hasattr(event, "model_dump") else dict(event)
                resp = data.get("response") or {}
                if isinstance(resp, dict) and resp.get("id") and not response_id:
                    response_id = resp["id"]
                key = (data.get("output_index"), data.get("content_index"))
                if (data.get("type") == "response.output_text.delta"
                        and isinstance(data.get("delta"), str)):
                    lanes.setdefault(key, []).append(data["delta"])
                elif data.get("type") == "response.content_part.done":
                    part = data.get("part") or {}
                    if (part.get("type") == "output_text"
                            and isinstance(part.get("text"), str)):
                        parts[key] = part["text"]
        texts = []
        for key in dict.fromkeys([*lanes, *parts]):
            text = "".join(lanes.get(key, [])) or parts.get(key, "")
            if text:
                texts.append(text)
        return texts, response_id

    def _combine_tracks(self, result, concept: str, clip_file: str,
                        total_ms: float, response_id: str) -> dict:
        """Group per-frame box records into per-object tracks."""
        records = list(getattr(result, "records", []) or [])
        tracks: dict = {}
        frame_indexes: list[int] = []
        for rec in records:
            if getattr(rec, "kind", "") != "box":
                continue
            frame = getattr(rec, "frame", None)
            index = int(getattr(frame, "frame_index", 0) or 0)
            try:
                coords = [int(rec.left), int(rec.top), int(rec.right), int(rec.bottom)]
            except (TypeError, ValueError, AttributeError):
                continue
            conf = getattr(rec, "confidence", None)
            object_id = str(getattr(rec, "object_id", "0"))
            entry = tracks.setdefault(object_id, {"object_id": object_id, "frames": []})
            entry["frames"].append({
                "frame": index,
                "box": coords,
                "confidence": min(1.0, max(0.0, float(conf))) if conf is not None else 0.5,
            })
            frame_indexes.append(index)
        ordered: list[dict] = []
        for entry in tracks.values():
            seen = sorted(entry["frames"], key=lambda f: f["frame"])
            ordered.append({
                "object_id": entry["object_id"],
                "frames": seen,
                "first_frame": seen[0]["frame"],
                "last_frame": seen[-1]["frame"],
                "frames_seen": len(seen),
                "mean_confidence": round(
                    sum(f["confidence"] for f in seen) / len(seen), 3),
            })
        ordered.sort(key=lambda t: (-t["frames_seen"], t["object_id"]))
        last_index = max(frame_indexes) if frame_indexes else 0
        detections = [{"label": "component", "box": f["box"], "confidence": f["confidence"]}
                      for track in ordered for f in track["frames"] if f["frame"] == last_index]
        log_model_call(self.model, total_ms, 200, clip_file, response_id,
                       raw_summary="%s: %d track(s) over %d frame(s)" % (
                           concept, len(ordered), last_index + 1))
        return {
            "model": self.model,
            "mock": False,
            "media": "video",
            "concept": concept,
            "tracks": ordered,
            "track_count": len(ordered),
            "frame_count": last_index + 1,
            "detections": detections,
            "wire_count": 0,
            # Tracking alone cannot judge wiring or lit state: unknown.
            "wires_ok": None,
            "oled_lit": None,
            "notes": ("SAM 3.1 video: %d object(s) tracked across %d frame(s) "
                      "for %r in %.0fms; clip %s" % (
                          len(ordered), last_index + 1, concept, total_ms,
                          clip_file or "unresolved")),
            "clip_file": clip_file,
            "latency_ms": round(total_ms, 1),
            "http_status": 200,
            "request_id": response_id,
        }

    def _combine(self, per_concept: list[dict], frame_file: str,
                 total_ms: float, parser_mod,
                 prep: PreparedFrame | None = None) -> dict:
        """Map parser records to detections; log one entry per concept call.

        Boxes arrive in shipped-pixel coordinates; rescale_detections maps
        them back to source pixels so callers keep the source-pixel invariant.
        """
        detections: list[dict] = []
        counts: list[str] = []
        mask_total = 0
        for item in per_concept:
            label = item["label"]
            records = list(getattr(item["result"], "records", []) or [])
            boxes = [r for r in records if getattr(r, "kind", "") == "box"]
            masks = {getattr(r, "object_id", None): r
                     for r in records if getattr(r, "kind", "") == "mask"}
            mask_total += len(masks)
            for box in boxes:
                try:
                    coords = [int(box.left), int(box.top),
                              int(box.right), int(box.bottom)]
                except (TypeError, ValueError, AttributeError):
                    continue
                conf = getattr(box, "confidence", None)
                det: dict = {
                    "label": label,
                    "box": coords,
                    "confidence": min(1.0, max(0.0, float(conf)))
                    if conf is not None else 0.5,
                }
                mask_rec = masks.get(getattr(box, "object_id", None))
                if mask_rec is not None:
                    try:
                        det["mask_svg"] = parser_mod.decode_mask_to_svg_path(
                            mask_rec.mask)
                    except Exception:
                        pass  # box + confidence still stand without the SVG
                detections.append(det)
            counts.append("%s x%d" % (label, len(boxes)))
            log_model_call(self.model, item["latency_ms"], 200, frame_file,
                           item["response_id"],
                           raw_summary="%s: %d boxes, %d masks" % (
                               item["concept"], len(boxes), len(masks)))
        detections = rescale_detections(detections, prep)
        wire_count = sum(1 for d in detections if d["label"] == "jumper wire")
        return {
            "model": self.model,
            "mock": False,
            "detections": detections,
            "wire_count": wire_count,
            # Segmentation alone cannot judge wiring or lit state: unknown,
            # not a hardcoded verdict. The probe measures, Muse describes.
            "wires_ok": None,
            "oled_lit": None,
            "notes": ("SAM 3.1: %d regions from %d prompt(s) in %.0fms "
                      "(%s; %d masks)%s; frame %s" % (
                          len(detections), len(per_concept), total_ms,
                          "; ".join(counts), mask_total,
                          ("; shipped %dx%d from %dx%d" % (
                              prep.dst_w, prep.dst_h, prep.src_w, prep.src_h)
                           if prep is not None and prep.resized else ""),
                          frame_file or "unresolved")),
            "frame_file": frame_file,
            "latency_ms": round(total_ms, 1),
            "http_status": 200,
            "request_id": per_concept[0]["response_id"] if per_concept else "",
        }

    def _mock_clip_result(self, context: dict, concept: str) -> dict:
        if "(override)" in (context.get("clip_source") or ""):
            return {"model": self.model, "mock": True, "media": "video",
                    "concept": concept, "tracks": [], "track_count": 0,
                    "frame_count": 0, "detections": [], "wire_count": 0,
                    "wires_ok": True, "oled_lit": False,
                    "notes": "Mock vision cannot read pixels. "
                             "Set MOCK_META=false with a Meta API key for real video tracking."}
        # One object holds still across three frames; a second appears later.
        tracks = [
            {"object_id": "0", "first_frame": 0, "last_frame": 2, "frames_seen": 3,
             "mean_confidence": 0.95,
             "frames": [{"frame": i, "box": [70, 110, 310, 390], "confidence": 0.95}
                        for i in range(3)]},
            {"object_id": "1", "first_frame": 1, "last_frame": 2, "frames_seen": 2,
             "mean_confidence": 0.9,
             "frames": [{"frame": i, "box": [360, 140, 470, 240], "confidence": 0.9}
                        for i in (1, 2)]},
        ]
        return {
            "model": self.model,
            "mock": True,
            "media": "video",
            "concept": concept,
            "tracks": tracks,
            "track_count": len(tracks),
            "frame_count": 3,
            "detections": [{"label": "component", "box": t["frames"][-1]["box"],
                            "confidence": t["mean_confidence"]} for t in tracks],
            "wire_count": 0,
            "wires_ok": True,
            "oled_lit": False,
            "notes": "Mock video tracking: 2 objects followed across 3 frames.",
        }

    def _mock_result(self, context: dict) -> dict:
        if "(override)" in (context.get("frame_source") or ""):
            # A real photo the mock cannot read: say so instead of inventing parts.
            return {"model": self.model, "mock": True, "detections": [],
                    "wire_count": 0, "wires_ok": True, "oled_lit": False,
                    "notes": "Mock vision cannot read pixels. "
                             "Set MOCK_META=false with a Meta API key for real image analysis."}
        fault = (context.get("simulate_fault") or "").upper()
        detections = [
            {"label": "ESP32 DevKit", "box": [70, 110, 310, 390], "confidence": 0.97},
                {"label": "BME280 module", "box": [360, 140, 470, 240], "confidence": 0.93},
                {"label": "SSD1306 OLED", "box": [360, 270, 500, 360], "confidence": 0.91},
                {"label": "LED (GPIO2)", "box": [520, 200, 560, 260], "confidence": 0.88},
        ]
        wires_ok, oled_lit = True, True
        notes = "ESP32, BME280, OLED, and LED all visible; 8 jumpers seated."
        if fault == "SENSOR_DISCONNECTED":
            detections = [d for d in detections if "BME280" not in d["label"]]
            notes = "BME280 module NOT visible in frame; OLED and ESP32 present."
        elif fault in ("SDA_SCL_SWAPPED", "LOOSE_GND"):
            wires_ok = False
            notes = ("I2C jumpers look crossed at the module; modules visible but bus "
                     "wiring suspect." if fault == "SDA_SCL_SWAPPED" else
                     "a ground jumper looks loose; modules visible but power suspect.")
        elif fault in ("LED_REVERSED", "CODE_PIN_MISMATCH"):
            notes = "LED visible but dark while its GPIO toggles."
        return {
            "model": self.model,
            "mock": True,
            "detections": detections,
            "wire_count": 8,
            "wires_ok": wires_ok,
            "oled_lit": oled_lit,
            "notes": "Mock segmentation: " + notes,
        }
