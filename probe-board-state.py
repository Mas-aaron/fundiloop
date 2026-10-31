"""Throwaway: live end-to-end proof of instant no-board hints.

Runs a real serial sense pass, verifies CHECK_SERIAL, and builds the
hint with a scripted brain: the model must NOT be called.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe probe-board-state.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["MOCK_META"] = "false"

from backend.app.boards import pin_map_for  # noqa: E402
from backend.app.learning import build_hint, run_learning_verification  # noqa: E402
from backend.app.serial_probe import SerialProbe  # noqa: E402
from backend.tests.test_fundi import StubBrain  # noqa: E402

probe = SerialProbe(mock=False, timeout=4.0)
reading = probe.read_all()
ver = run_learning_verification("CHECK_SERIAL", {}, reading,
                                pin_map_for("esp32_devkit"))
brain = StubBrain(["model hint"])
t0 = time.monotonic()
hint = build_hint({"n": 1, "instruction": "x"}, ver, brain)
wall = time.monotonic() - t0
print("board_state=%s" % ver["board_state"])
print("hint=%s" % hint)
print("model_calls=%d hint_wall=%.3fs" % (len(brain.calls), wall))
print("speak=%s. %s" % (ver["measured"], hint))
