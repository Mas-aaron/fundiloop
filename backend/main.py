"""FundiLoop backend: frame capture, probe, diagnose, transcribe."""
import os
import time
from pathlib import Path

from fastapi import FastAPI, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .muse_client import diagnose as run_diagnose
from .schemas import DiagnoseRequest
from .serial_probe import SERIAL_BAUD, SERIAL_PORT, read_last_serial, run_i2c_scan

BASE_DIR = Path(__file__).resolve().parent
FRAMES_DIR = BASE_DIR / "frames"
STATIC_INDEX = BASE_DIR / "static" / "index.html"

app = FastAPI(title="FundiLoop")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return FileResponse(str(STATIC_INDEX), media_type="text/html")


@app.get("/config")
def config():
    return {"cam_url": os.environ.get("ESP32_CAM_URL", "http://192.0.2.10/capture")}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "serial_port": SERIAL_PORT,
        "serial_baud": SERIAL_BAUD,
        "frames_dir": str(FRAMES_DIR),
    }


@app.post("/frame")
async def save_frame(file: UploadFile):
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    dest = FRAMES_DIR / f"{int(time.time() * 1000)}.jpg"
    dest.write_bytes(await file.read())
    return {"path": str(dest)}


@app.post("/probe")
def probe():
    return run_i2c_scan(port=SERIAL_PORT, baud=SERIAL_BAUD)


@app.post("/serial")
def serial_tail(n: int = 20):
    return {"text": read_last_serial(SERIAL_PORT, n)}


@app.post("/diagnose")
def diagnose(req: DiagnoseRequest):
    return run_diagnose(req)


@app.post("/transcribe")
async def transcribe(file: UploadFile):
    try:
        from openai import OpenAI

        api_key = os.environ.get("MODEL_API_KEY", "")
        if not api_key:
            raise RuntimeError("MODEL_API_KEY is not set")
        tmp = FRAMES_DIR / f"audio-{int(time.time() * 1000)}.webm"
        FRAMES_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(await file.read())
        client = OpenAI(base_url="https://api.meta.ai/v1", api_key=api_key)
        with open(tmp, "rb") as fh:
            result = client.audio.transcriptions.create(
                model="muse-voice-transcribe", file=fh
            )
        return {"text": getattr(result, "text", "") or ""}
    except Exception as exc:
        return JSONResponse(status_code=200, content={"text": "", "error": str(exc)})
