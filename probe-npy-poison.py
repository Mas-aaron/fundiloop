"""Throwaway: reproduce the numpy double-load poisoning step by step.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe probe-npy-poison.py
"""
import os
import sys
import tempfile
import traceback
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class FakeKokoro:
    def __init__(self, model_path, voices_path):
        pass

    def create(self, text, voice="af_heart", speed=1.0, lang="en-us"):
        return [0.1] * 240, 24000


def state(tag):
    keys = sorted(k for k in sys.modules if "numpy" in k or "multiarray" in k)
    print("%s: numpy-cached=%s submodules=%d %s"
          % (tag, "numpy" in sys.modules, len(keys), keys[:5]))


state("start")
import backend.app.main as app_main  # noqa: E402

state("after-main-import")

module = type(sys)("kokoro_onnx")
module.Kokoro = FakeKokoro
tmp = tempfile.TemporaryDirectory()
for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
    with open(os.path.join(tmp.name, name), "wb") as fh:
        fh.write(b"fake")

with mock.patch.dict(sys.modules, {"kokoro_onnx": module}):
    with mock.patch.dict(os.environ, {"MOCK_META": "false",
                                      "TTS_PROVIDER": "kokoro",
                                      "KOKORO_MODEL_DIR": tmp.name}):
        try:
            resp = app_main.api_voice_speak({"text": "hello"})
            print("speak OK bytes=%d" % len(bytes(resp.body)))
        except Exception as exc:
            print("speak raised: %s: %s" % (type(exc).__name__, exc))
        state("after-speak-inside-patch")
state("after-patch-exit")
try:
    import numpy  # noqa: F401
    print("second import OK");
except ImportError:
    traceback.print_exc()
