"""End-to-end mock-mode rehearsal: one full Guided Assembly run plus one
Diagnostic run against an SDA_SCL_SWAPPED fault. No hardware, no keys,
no sockets.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe scripts\\e2e_mock_demo.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.app.adapters.muse_adapter import MetaClaudeAdapter  # noqa: E402
from backend.app.adapters.sam_adapter import MetaSAMAdapter  # noqa: E402
from backend.app.adapters.voice_adapter import MetaVoiceAdapter  # noqa: E402
from backend.app.agent_orchestrator import AgentOrchestrator  # noqa: E402
from backend.app.camera_client import CameraClient  # noqa: E402
from backend.app.change_detector import MotionSettler  # noqa: E402
from backend.app.circuit_graph import load_circuit_graph  # noqa: E402
from backend.app.models import CheckNowRequest, Mode  # noqa: E402
from backend.app.serial_probe import SerialProbe  # noqa: E402


def make_orchestrator(tmp):
    return AgentOrchestrator(
        graph=load_circuit_graph(),
        camera=CameraClient(mock=True),
        settler=MotionSettler(settle_seconds=1.0),
        probe=SerialProbe(mock=True),
        sam=MetaSAMAdapter(mock=True),
        Muse=MetaClaudeAdapter(mock=True),
        voice=MetaVoiceAdapter(mock=True),
        attempt_log_path=os.path.join(tmp, "attempts.jsonl"),
    )


def guided_run(orc):
    print("== Guided Assembly ==")
    v = orc.start_session(Mode.GUIDED_ASSEMBLY)
    assert v["step"]["step_id"] == "S1_MOUNT_ESP32", v["step"]
    print("start: %s ..." % v["instruction"][:60])
    for i in range(8):
        v = orc.check_now(CheckNowRequest(transcript="done"))
        assert v["verification"]["passed"], v["verification"]
        print("turn %d: %-16s PASS (%s)" % (
            i + 1, v["attempts"][-1]["step_id"], v["verification"]["measured"][:50]))
    assert v["state"] == "COMPLETE", v["state"]
    assert v["passport"]["steps_completed"] == 8
    log = orc.get_passport(v["id"])
    assert log["chain_valid"] and log["event_count"] == 8, log
    print("COMPLETE: passport issued, hash chain valid (%d events)" % log["event_count"])


def diagnostic_swapped_run(orc):
    print("== Diagnostic (SDA_SCL_SWAPPED) ==")
    v = orc.start_session(Mode.DIAGNOSTIC)
    assert v["baseline"]["i2c_devices"] == ["0x3C", "0x76"], v["baseline"]
    print("baseline: I2C %s" % v["baseline"]["i2c_devices"])
    v = orc.check_now(CheckNowRequest(simulate_fault="SDA_SCL_SWAPPED", transcript="fixed"))
    assert v["state"] == "FAULT_FOUND", v["state"]
    assert v["ranking"][0]["fault_id"] == "SDA_SCL_SWAPPED", v["ranking"][0]
    assert v["decision"]["fault_hypothesis"] == "SDA_SCL_SWAPPED"
    print("turn 1: top fault %s (%.2f), guided one fix" % (
        v["ranking"][0]["fault_id"], v["ranking"][0]["score"]))
    v = orc.check_now(CheckNowRequest(transcript="moved it"))
    assert v["state"] == "VERIFIED_FIXED", v["state"]
    assert v["passport"]["faults_found"] == ["SDA_SCL_SWAPPED"]
    log = orc.get_passport(v["id"])
    assert log["chain_valid"] and log["event_count"] == 2, log
    print("VERIFIED_FIXED: repair passport issued, hash chain valid")


def main():
    tmp = tempfile.mkdtemp(prefix="fundiloop-e2e-")
    guided_run(make_orchestrator(tmp))
    diagnostic_swapped_run(make_orchestrator(tmp))
    print("E2E MOCK REHEARSAL PASSED")


if __name__ == "__main__":
    main()
