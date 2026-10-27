"""FundiLoop FastAPI backend.

Serves the mobile web UI at / and the session API:
  GET  /api/status           session + config status
  POST /api/session/start    start a guided/diagnostic session
  POST /api/session/check-now run one Sense->Reason->Instruct->Verify turn
  GET  /api/camera/latest    latest overhead frame (JPEG/PNG)
  POST /api/voice/transcribe transcribe push-to-talk audio upload
  GET  /api/session/{id}/passport full verifiable log for a session
  POST /api/config/demo      toggle demo-mode fallback ({"enabled": bool})

Run from the repo root:
  uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT / ".env")

from .adapters import MetaAPIError  # noqa: E402
from .agent_orchestrator import AgentOrchestrator  # noqa: E402
from .models import CheckNowRequest, StartSessionRequest  # noqa: E402

FRONTEND = ROOT / "frontend"

app = FastAPI(title="FundiLoop")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = AgentOrchestrator()


def _mock_meta() -> bool:
    return os.environ.get("MOCK_META", "true").lower() in ("1", "true", "yes")


# -- frontend -------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(str(FRONTEND / "index.html"), media_type="text/html")


@app.get("/app.js")
def app_js():
    return FileResponse(str(FRONTEND / "app.js"), media_type="text/javascript")


@app.get("/style.css")
def style_css():
    return FileResponse(str(FRONTEND / "style.css"), media_type="text/css")


# -- API ------------------------------------------------------------------
@app.get("/api/status")
def api_status():
    view = orchestrator.status()
    view["config"] = {
        "mock_meta": _mock_meta(),
        "meta_api_key_set": bool(os.environ.get("META_API_KEY")),
        "camera_frame_url_set": bool(os.environ.get("CAMERA_FRAME_URL")),
        "serial_port": os.environ.get("SERIAL_PORT") or "(default)",
        "muse_model": os.environ.get("MUSE_MODEL", "muse-spark-1.3"),
        "sam_model": os.environ.get("SAM_MODEL", "sam-3.1"),
        "voice_model": os.environ.get("VOICE_MODEL", "meta-voice-1"),
        "voice_live": not orchestrator.voice.mock,
        "demo_mode": orchestrator.demo_mode,
    }
    return view


@app.post("/api/session/start")
def api_session_start(req: StartSessionRequest):
    return orchestrator.start_session(mode=req.mode)


@app.post("/api/session/check-now")
def api_check_now(req: CheckNowRequest | None = None):
    return orchestrator.check_now(req or CheckNowRequest())


@app.post("/api/voice/transcribe")
async def api_voice_transcribe(file: UploadFile = File(...)):
    try:
        return orchestrator.transcribe(await file.read(), file.filename or "talk.webm")
    except MetaAPIError as exc:
        return {"text": "", "error": str(exc)}


@app.get("/api/session/{session_id}/passport")
def api_session_passport(session_id: str):
    data = orchestrator.get_passport(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="unknown session id")
    return data


@app.post("/api/config/demo")
def api_config_demo(body: dict):
    orchestrator.demo_mode = bool(body.get("enabled", True))
    return {"demo_mode": orchestrator.demo_mode}


@app.get("/api/camera/latest")
def api_camera_latest():
    from .camera_client import media_type_for_frame

    frame, source = orchestrator.camera.fetch_latest()
    return Response(
        content=frame,
        media_type=media_type_for_frame(frame),
        headers={"X-FundiLoop-Camera-Source": source},
    )
