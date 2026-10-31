"""Throwaway TTS latency probe: breaks down Kokoro synthesis cost.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe probe-tts-timing.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["MOCK_META"] = "false"

from backend.app.tts import _kokoro_engine, _samples_to_wav  # noqa: E402

SHORT = "Planning your projects."
MED = ("We'll build the blinking light project. You need arduino uno and "
       "led. Follow each step, then press check now.")
LONG = ("We'll build the line follower car. You need esp32 devkit, motor "
        "driver, and infrared sensors. Wire the motors first, then the "
        "sensors, then upload the code and press check now to verify.")

print("kokoro texts: short=%d med=%d long=%d chars"
      % (len(SHORT), len(MED), len(LONG)))

t0 = time.monotonic()
engine = _kokoro_engine()
print("engine load (cold): %.2fs" % (time.monotonic() - t0))
print("has create_stream: %s" % hasattr(engine, "create_stream"))

# Warm-up (first create pays one-time init inside the engine too).
t0 = time.monotonic()
engine.create(SHORT, voice="af_heart", speed=1.0, lang="en-us")
print("first create (warm-up): %.2fs" % (time.monotonic() - t0))

for name, text in (("short", SHORT), ("med", MED), ("long", LONG)):
    t0 = time.monotonic()
    samples, rate = engine.create(text, voice="af_heart", speed=1.0,
                                  lang="en-us")
    t_create = time.monotonic() - t0
    n = len(samples)
    t0 = time.monotonic()
    wav = _samples_to_wav(samples, rate)
    t_wav = time.monotonic() - t0
    print("%s: create=%.2fs wav=%.2fs total=%.2fs audio=%.1fs bytes=%d"
          % (name, t_create, t_wav, t_create + t_wav, n / rate, len(wav)))

# Pure-Python loop cost on 10s of audio (240k samples).
big = [0.1] * 240000
t0 = time.monotonic()
_samples_to_wav(big, 24000)
print("wav-convert 240k samples (loop): %.2fs" % (time.monotonic() - t0))

# Phase 2: is the ORT session thread-starved? Compare default session vs
# explicit intra/inter-op threads, plus lone phonemize cost.
import onnxruntime as rt  # noqa: E402
from kokoro_onnx import Kokoro  # noqa: E402

from backend.app.tts import (  # noqa: E402
    KOKORO_MODEL_FILE,
    KOKORO_VOICES_FILE,
    kokoro_model_dir,
)

t0 = time.monotonic()
engine.tokenizer.phonemize(MED, "en-us")
print("phonemize med alone: %.2fs" % (time.monotonic() - t0))

d = kokoro_model_dir()
opts = rt.SessionOptions()
opts.intra_op_num_threads = 16
opts.inter_op_num_threads = 4
threaded = Kokoro.from_session(
    rt.InferenceSession(os.path.join(d, KOKORO_MODEL_FILE),
                        sess_options=opts,
                        providers=["CPUExecutionProvider"]),
    os.path.join(d, KOKORO_VOICES_FILE))
for name, eng in (("default-threads", engine), ("16-threads", threaded)):
    t0 = time.monotonic()
    eng.create(MED, voice="af_heart", speed=1.0, lang="en-us")
    print("create med [%s]: %.2fs" % (name, time.monotonic() - t0))
