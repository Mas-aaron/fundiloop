"""FundiLoop backend: frame capture, probe, diagnose, transcribe."""
import os
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .muse_client import diagnose as run_diagnose
from .schemas import DiagnoseRequest
from .serial_probe import SERIAL_BAUD, SERIAL_PORT, read_last_serial, run_i2c_scan
from .transcribe import transcribe_upload

BASE_DIR = Path(__file__).resolve().parent
FRAMES_DIR = BASE_DIR / "frames"
STATIC_INDEX = BASE_DIR / "static" / "index.html"

# Fixed mDNS hostname; editable at runtime via POST /config.
ESP32_HOST = (
    os.environ.get("ESP32_CAM_HOST")
    or os.environ.get("ESP32_CAM_URL")
    or "fundiloop.local"
)

app = FastAPI(title="FundiLoop")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def cam_url_for(host: str) -> str:
    if host.startswith("http://") or host.startswith("https://"):
        return host
    return f"http://{host}/capture"


def _read_first_jpeg(url: str, timeout: float = 8.0) -> bytes:
    """Pull the first complete JPEG frame out of an MJPEG stream."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        buf = b""
        while len(buf) < 2_000_000:
            chunk = resp.read(8192)
            if not chunk:
                break
            buf += chunk
            start = buf.find(b"\xff\xd8")
            end = buf.find(b"\xff\xd9")
            if start >= 0 and end > start:
                return buf[start : end + 2]
    raise RuntimeError(f"no JPEG frame in {url}")


def grab_frame_from_esp32(host: str | None = None) -> bytes:
    """Fetch one still from the ESP32-CAM, trying each endpoint in turn."""
    h = (host or ESP32_HOST).strip()
    if h.startswith("http://") or h.startswith("https://"):
        with urllib.request.urlopen(h, timeout=5.0) as resp:
            return resp.read()
    errors = []
    for url in (f"http://{h}/capture", f"http://{h}/snapshot"):
        try:
            with urllib.request.urlopen(url, timeout=5.0) as resp:
                return resp.read()
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    try:
        return _read_first_jpeg(f"http://{h}:81/stream")
    except Exception as exc:
        errors.append(f"stream: {exc}")
    raise RuntimeError("; ".join(errors))


@app.get("/")
def root():
    return FileResponse(str(STATIC_INDEX), media_type="text/html")


@app.get("/config")
def get_config():
    return {"esp32_host": ESP32_HOST, "cam_url": cam_url_for(ESP32_HOST)}


@app.post("/config")
def set_config(body: dict):
    global ESP32_HOST
    host = str(body.get("esp32_host", "")).strip()
    if not host:
        return JSONResponse(status_code=400, content={"error": "esp32_host required"})
    ESP32_HOST = host
    return {"esp32_host": ESP32_HOST, "cam_url": cam_url_for(ESP32_HOST)}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "serial_port": SERIAL_PORT,
        "serial_baud": SERIAL_BAUD,
        "frames_dir": str(FRAMES_DIR),
        "esp32_host": ESP32_HOST,
    }


@app.post("/frame")
async def save_frame(file: UploadFile | None = File(None)):
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    dest = FRAMES_DIR / f"{int(time.time() * 1000)}.jpg"
    if file is not None and file.filename:
        dest.write_bytes(await file.read())
        return {"path": str(dest), "source": "upload"}
    try:
        dest.write_bytes(grab_frame_from_esp32())
        return {"path": str(dest), "source": "camera"}
    except Exception as exc:
        return JSONResponse(status_code=200, content={"path": "", "error": str(exc)})


@app.get("/frames/{name}")
def get_frame(name: str):
    safe = Path(name).name
    path = FRAMES_DIR / safe
    if not safe.endswith(".jpg") or not path.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})
    return FileResponse(str(path), media_type="image/jpeg")


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
        return transcribe_upload(await file.read(), file.filename or "", FRAMES_DIR)
    except Exception as exc:
        return JSONResponse(status_code=200, content={"text": "", "error": str(exc)})
