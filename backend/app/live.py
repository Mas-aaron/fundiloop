"""Live guidance: the technician talks and shows the bench; the agent guides.

This is FundiLoop's Gemini-Live-shaped loop, assembled from the parts Meta
actually streams -- Meta has no audio-out model and no streaming video
endpoint, so each half is a different Meta model:

  hearing   Muse Voice Transcribe realtime ASR  (voice_adapter.RealtimeVoiceStream)
  seeing    SAM 3.1 video tracking              (sam_adapter.analyze_clip)
  reading   Muse Spark video understanding      (muse_adapter.complete_text)
  speaking  the browser's speechSynthesis       (voice_adapter.speak)

guide() runs one live turn: probe (ground truth) -> SAM tracks -> Muse
guidance text -> speak(). Exactly as in the check-now loop, only the probe
may confirm anything; SAM and Muse stay advisory.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor

from .adapters.muse_adapter import MetaClaudeAdapter
from .adapters.sam_adapter import DEFAULT_VIDEO_MIME, MetaSAMAdapter
from .adapters.voice_adapter import MetaVoiceAdapter
from .agent_orchestrator import _probe_summary
from .prompts import LIVE_SYSTEM, live_guidance_prompt
from .serial_probe import SerialProbe

MOCK_GUIDANCE = ("Live guidance is offline: start the backend with MOCK_META=false "
                 "and a Meta API key for spoken coaching.")


class LiveGuide:
    def __init__(self, sam=None, Muse=None, voice=None, probe=None) -> None:
        self.sam = sam or MetaSAMAdapter()
        self.Muse = Muse or MetaClaudeAdapter()
        self.voice = voice or MetaVoiceAdapter()
        self.probe = probe or SerialProbe()

    def guide(self, clip: bytes | None = None, transcript: str = "",
              mode: str = "GUIDED_ASSEMBLY", step_text: str = "",
              simulate_fault: str | None = None,
              clip_mime: str = DEFAULT_VIDEO_MIME) -> dict:
        """One live turn: keep the sensor honest, then let Muse coach.

        `clip` is a short video of the work in progress (optional). Raises
        MetaAPIError/CameraError when a real Meta call fails -- the turn stops
        rather than coaching from nothing.
        """
        with ThreadPoolExecutor(max_workers=2,
                                thread_name_prefix="fundiloop-live") as ex:
            probe_fut = ex.submit(self.probe.read_all, simulate_fault)
            if clip:
                tracks_fut = ex.submit(
                    lambda: self.sam.analyze_clip(
                        clip, context={"clip_source": "live clip"},
                        mime=clip_mime))
            else:
                tracks_fut = None
            probe = probe_fut.result()
            tracks = tracks_fut.result() if tracks_fut is not None else None
        summary = _probe_summary(probe)
        if self.Muse.mock:
            text = MOCK_GUIDANCE
        else:
            prompt = live_guidance_prompt(transcript, summary, tracks, mode, step_text)
            text = self.Muse.complete_text(
                LIVE_SYSTEM, prompt,
                video_b64=base64.b64encode(clip).decode("ascii") if clip else None,
                video_mime=clip_mime).strip()
        if not text:
            text = ("I could not see or hear that clearly. Hold the board still "
                    "and tell me what you are doing.")
        self.voice.speak(text)
        return {
            "speak": text,
            "transcript": transcript or "",
            "mode": mode,
            "probe": summary,
            "sam": None if tracks is None else {
                "media": tracks.get("media"),
                "concept": tracks.get("concept"),
                "track_count": tracks.get("track_count", 0),
                "frame_count": tracks.get("frame_count", 0),
                "tracks": tracks.get("tracks", []),
                "notes": tracks.get("notes", ""),
                "mock": tracks.get("mock", False),
            },
            "mock": bool(self.Muse.mock or self.sam.mock),
        }
