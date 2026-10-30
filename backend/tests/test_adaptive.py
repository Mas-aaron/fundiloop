"""Adaptivity: baud auto-detect, probe fast-fail, re-detect, confirm flow.

- silent boards fail fast with honest short-circuited errors (no 4x timeout)
- the probe adopts whichever of 115200/9600 answers (ESP32 vs Arduino)
- the board glance re-runs when the I2C signature changes; rescan clears it
- learning GPIO/OLED steps the probe cannot assess ask the technician to
  confirm instead of failing; confirmations are hash-chained and counted

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_adaptive -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from backend.app.agent_orchestrator import verify_chain
from backend.app.learning import run_learning_verification
from backend.app.models import CheckNowRequest, Mode, ProbeReading
from backend.app.serial_probe import SerialProbe
from backend.tests.helpers import use_mock_env
from backend.tests.test_latency import make_orchestrator


class SilentHandle:
    """Serial handle that never replies (dead port, wrong protocol)."""

    def __init__(self, *args, **kwargs):
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def readline(self):
        return b""

    def close(self):
        pass


class BaudAwareHandle(SilentHandle):
    """Answers the patient protocol only at 9600 baud (stock Arduino)."""

    def __init__(self, port=None, baud=115200, **kwargs):
        super().__init__()
        self.baud = baud
        self._out = []

    def write(self, data):
        super().write(data)
        cmd = bytes(data).decode("ascii", "replace").strip()
        if self.baud == 9600 and cmd in (
                "PING", "I2C_SCAN", "READ_SENSOR", "SELFTEST"):
            reply = {"cmd": cmd}
            if cmd == "PING":
                reply["status"] = "OK"
            if cmd == "I2C_SCAN":
                reply["i2c_devices"] = []
            self._out.append((json.dumps(reply) + "\n").encode("ascii"))
        return len(data)

    def readline(self):
        if self._out:
            return self._out.pop(0)
        return b""


def install_serial_stub(tc, handle_cls):
    patcher = mock.patch.dict(
        sys.modules, {"serial": SimpleNamespace(Serial=handle_cls)})
    patcher.start()
    tc.addCleanup(patcher.stop)


class ProbeAdaptivityTest(unittest.TestCase):
    def test_silent_board_short_circuits_after_ping(self):
        install_serial_stub(self, SilentHandle)
        probe = SerialProbe(port="COM9", baud=115200, mock=False, timeout=0.5)
        t0 = time.monotonic()
        reading = probe.read_all()
        wall = time.monotonic() - t0
        self.assertFalse(reading.connected)
        self.assertEqual(reading.i2c_devices, [])
        raw = json.loads(reading.raw)
        for key in ("ping", "scan", "sensor", "selftest"):
            self.assertIn("error", raw[key], key)
        for key in ("scan", "sensor", "selftest"):
            self.assertTrue(raw[key].get("short_circuited"), key)
            self.assertIn("skipped", raw[key]["error"])
        # PING + one quick alternate-baud retry, not 4 full timeouts.
        self.assertLess(wall, 3.0)

    def test_probe_adopts_9600_when_115200_silent(self):
        install_serial_stub(self, BaudAwareHandle)
        probe = SerialProbe(port="COM9", baud=115200, mock=False, timeout=0.5)
        reading = probe.read_all()
        self.assertTrue(reading.connected)
        self.assertEqual(probe.baud, 9600)
        raw = json.loads(reading.raw)
        self.assertNotIn("error", raw["ping"])
        self.assertEqual(raw["ping"].get("baud"), 9600)

    def test_serial_timeout_env_override(self):
        with mock.patch.dict(os.environ, {"SERIAL_TIMEOUT": "0.2"}):
            self.assertEqual(
                SerialProbe(port="X", mock=True).timeout, 0.2)
        with mock.patch.dict(os.environ, {"SERIAL_TIMEOUT": "bogus"}):
            self.assertEqual(
                SerialProbe(port="X", mock=True).timeout, 4.0)


class BoardRedetectTest(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orc = make_orchestrator(self.tmp.name)
        self.orc.start_session(Mode.GUIDED_ASSEMBLY)

    def test_reglance_only_when_i2c_signature_changes(self):
        calls = []
        raw = '{"board": "esp32_devkit", "confidence": 0.95, "parts": ["esp32"]}'
        self.orc.Muse.complete_text = lambda *a, **k: (calls.append(1), raw)[1]
        self.orc.check_now(CheckNowRequest())
        self.assertEqual(len(calls), 1)
        self.orc.check_now(CheckNowRequest())
        self.assertEqual(len(calls), 1)  # steady bench: cache holds
        self.orc.check_now(CheckNowRequest(simulate_fault="LOOSE_GND"))
        self.assertEqual(len(calls), 2)  # bus changed: re-glance once
        self.orc.check_now(CheckNowRequest())
        self.assertEqual(len(calls), 3)  # changed back: re-glance again

    def test_rescan_endpoint_clears_cache(self):
        import backend.app.main as main_mod

        self.orc.session["detected_board"] = {"board": "esp32_devkit"}
        self.orc.session["glance_i2c"] = ["0x3C"]
        with mock.patch.object(main_mod, "orchestrator", self.orc):
            out = main_mod.api_session_rescan()
        self.assertTrue(out["ok"])
        self.assertNotIn("detected_board", self.orc.session)
        self.assertNotIn("glance_i2c", self.orc.session)


GPIO_PLAN = [{"n": 1, "instruction": "Blink the builtin LED.",
              "verify": "CHECK_GPIO", "expect": {},
              "depends_on_parts": []}]

UNVERIFIABLE = ProbeReading(
    connected=True, i2c_devices=[], temperature_c=None, humidity_pct=None,
    pressure_hpa=None, oled_ok=None, led_ok=None, mock=False, raw="{}")


def start_blink(orc, sid, board="arduino_uno"):
    return orc.start_learning_session(
        "blink", [dict(s) for s in GPIO_PLAN],
        visible_parts=["arduino_uno", "led"], session_id=sid,
        project_title="Blink", board=board)


class ConfirmFlowTest(unittest.TestCase):
    def setUp(self):
        use_mock_env(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orc = make_orchestrator(self.tmp.name)

    def test_unknown_gpio_asks_instead_of_failing(self):
        start_blink(self.orc, "learn-gpio")
        with mock.patch.object(self.orc.probe, "read_all",
                               return_value=UNVERIFIABLE):
            v = self.orc.check_now(CheckNowRequest())
        self.assertEqual(v["state"], "VERIFYING")
        self.assertEqual(v["step_index"], 0)  # nothing advanced on its own
        self.assertIn("GPIO13", v["confirm_pending"]["question"])
        self.assertIn("CONFIRM", v["confirm_pending"]["question"])

    def test_confirm_advances_and_audits(self):
        start_blink(self.orc, "learn-gpio")
        with mock.patch.object(self.orc.probe, "read_all",
                               return_value=UNVERIFIABLE):
            self.orc.check_now(CheckNowRequest())
        v = self.orc.confirm_learning_step("learn-gpio", True)
        self.assertEqual(v["state"], "DONE")
        self.assertTrue(v["code_ready"])
        steps = self.orc.session["verified"]["steps"]
        self.assertEqual(steps[-1]["confirmed_by"], "technician")
        attempts = self.orc.session["attempts"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["confirmed_by"], "technician")
        self.assertTrue(verify_chain(attempts))
        passport = self.orc.get_passport("learn-gpio")
        self.assertTrue(passport["chain_valid"])
        self.assertEqual(passport["passport"]["technician_confirmed"], 1)

    def test_decline_stays_without_advancing(self):
        start_blink(self.orc, "learn-gpio2")
        with mock.patch.object(self.orc.probe, "read_all",
                               return_value=UNVERIFIABLE):
            self.orc.check_now(CheckNowRequest())
        v = self.orc.confirm_learning_step("learn-gpio2", False)
        self.assertEqual(v["state"], "VERIFYING")
        self.assertEqual(v["step_index"], 0)
        self.assertIsNone(self.orc.session["confirm_pending"])
        self.assertEqual(self.orc.session["attempts"], [])

    def test_confirm_endpoints_map_errors(self):
        import backend.app.main as main_mod

        with mock.patch.object(main_mod, "orchestrator", self.orc):
            with self.assertRaises(HTTPException) as ctx:
                main_mod.api_session_confirm(
                    {"session_id": "nope", "confirmed": True})
            self.assertEqual(ctx.exception.status_code, 404)
            start_blink(self.orc, "learn-empty")
            with self.assertRaises(HTTPException) as ctx:
                main_mod.api_session_confirm(
                    {"session_id": "learn-empty", "confirmed": True})
            self.assertEqual(ctx.exception.status_code, 409)

    def test_verifier_marks_unknown_not_failed(self):
        from backend.app.boards import pin_map_for

        arduino = pin_map_for("arduino_uno")
        gpio_unknown = run_learning_verification(
            "CHECK_GPIO", {}, UNVERIFIABLE, arduino)
        self.assertFalse(gpio_unknown["passed"])
        self.assertTrue(gpio_unknown["needs_confirmation"])
        gpio_dead = run_learning_verification(
            "CHECK_GPIO", {},
            UNVERIFIABLE.model_copy(update={"led_ok": False}), arduino)
        self.assertFalse(gpio_dead["passed"])
        self.assertFalse(gpio_dead["needs_confirmation"])
        self.assertIn("did not toggle", gpio_dead["measured"])
        oled_unknown = run_learning_verification(
            "CHECK_OLED", {}, UNVERIFIABLE, arduino)
        self.assertTrue(oled_unknown["needs_confirmation"])


class ToggleHandle(SilentHandle):
    """Answers PING only while the class flag is set."""

    alive = False

    def __init__(self, *args, **kwargs):
        super().__init__()
        self._out = []

    def write(self, data):
        super().write(data)
        cmd = bytes(data).decode("ascii", "replace").strip()
        if ToggleHandle.alive and cmd == "PING":
            self._out.append(b'{"cmd": "PING", "status": "OK"}\n')
        return len(data)

    def readline(self):
        if self._out:
            return self._out.pop(0)
        return b""


class PortAwareHandle(SilentHandle):
    """Answers PING only on COM5 at 9600 baud."""

    def __init__(self, port=None, baud=115200, **kwargs):
        super().__init__()
        self._port = port
        self._baud = baud
        self._out = []

    def write(self, data):
        super().write(data)
        cmd = bytes(data).decode("ascii", "replace").strip()
        if (self._port, self._baud) == ("COM5", 9600) and cmd == "PING":
            self._out.append(b'{"cmd": "PING", "status": "OK"}\n')
        return len(data)

    def readline(self):
        if self._out:
            return self._out.pop(0)
        return b""


def install_port_list_stub(tc, devices):
    fake_mod = SimpleNamespace(list_ports=SimpleNamespace(
        comports=lambda: [
            SimpleNamespace(device=d, description="USB Serial", hwid="USB")
            for d in devices
        ]))
    patcher = mock.patch.dict(sys.modules, {"serial.tools": fake_mod})
    patcher.start()
    tc.addCleanup(patcher.stop)


class SerialSetupTest(unittest.TestCase):
    def test_configure_switches_port_and_baud(self):
        probe = SerialProbe(port="COM3", baud=115200, mock=True)
        out = probe.configure(port="COM5", baud="9600")
        self.assertEqual((probe.port, probe.baud), ("COM5", 9600))
        self.assertEqual(out["port"], "COM5")
        out = probe.configure(port="COM7", baud="bogus")
        self.assertEqual((probe.port, probe.baud), ("COM7", 9600))

    def test_send_tracks_and_clears_last_error(self):
        install_serial_stub(self, ToggleHandle)
        probe = SerialProbe(port="COM9", baud=115200, mock=False, timeout=0.2)
        ToggleHandle.alive = False
        self.addCleanup(setattr, ToggleHandle, "alive", False)
        reply = probe.send("PING")
        self.assertIn("error", reply)
        self.assertIn("PING", probe.last_error)
        ToggleHandle.alive = True
        reply = probe.send("PING")
        self.assertNotIn("error", reply)
        self.assertEqual(probe.last_error, "")

    def test_list_serial_ports(self):
        from backend.app.serial_probe import list_serial_ports

        install_port_list_stub(self, ["COM5"])
        self.assertEqual(list_serial_ports(),
                         [{"port": "COM5", "description": "USB Serial",
                           "hwid": "USB"}])

    def test_list_serial_ports_empty_when_unavailable(self):
        from backend.app.serial_probe import list_serial_ports

        patcher = mock.patch.dict(sys.modules, {"serial.tools": SimpleNamespace()})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(list_serial_ports(), [])

    def test_autodetect_adopts_live_port(self):
        install_serial_stub(self, PortAwareHandle)
        install_port_list_stub(self, ["COM3", "COM5"])
        probe = SerialProbe(port="COM3", baud=115200, mock=False, timeout=2.0)
        found = probe.autodetect(timeout=0.2)
        self.assertEqual(found, {"port": "COM5", "baud": 9600})
        self.assertEqual((probe.port, probe.baud), ("COM5", 9600))
        self.assertEqual(probe.timeout, 2.0)

    def test_autodetect_none_restores_original(self):
        install_serial_stub(self, SilentHandle)
        install_port_list_stub(self, ["COM3"])
        probe = SerialProbe(port="COM9", baud=115200, mock=False, timeout=2.0)
        self.assertIsNone(probe.autodetect(timeout=0.2))
        self.assertEqual((probe.port, probe.baud), ("COM9", 115200))
        self.assertEqual(probe.timeout, 2.0)
        self.assertEqual(probe._ser, None)

    def test_config_serial_endpoint_applies_to_both_probes(self):
        import backend.app.main as main_mod

        orc = SimpleNamespace(probe=SerialProbe(port="COM3", mock=True))
        live = SimpleNamespace(probe=SerialProbe(port="COM3", mock=True))
        persisted = {}
        with mock.patch.object(main_mod, "orchestrator", orc), mock.patch.object(
                main_mod, "live_guide", live), mock.patch.object(
                main_mod, "_persist_env_var",
                lambda n, v: persisted.update({n: v})), mock.patch.dict(os.environ, {}):
            out = main_mod.api_config_serial({"port": "COM5", "baud": "9600"})
            self.assertEqual(os.environ["SERIAL_PORT"], "COM5")
            self.assertEqual(os.environ["SERIAL_BAUD"], "9600")
        self.assertEqual(out["serial_live"]["port"], "COM5")
        self.assertEqual(live.probe.port, "COM5")
        self.assertEqual(live.probe.baud, 9600)
        self.assertEqual(persisted, {"SERIAL_PORT": "COM5", "SERIAL_BAUD": "9600"})

    def test_serial_ports_endpoint_shape(self):
        import backend.app.main as main_mod

        orc = SimpleNamespace(probe=SerialProbe(port="COM3", mock=True))
        with mock.patch.object(main_mod, "orchestrator", orc), mock.patch.object(
                main_mod, "list_serial_ports",
                return_value=[{"port": "COM5", "description": "USB", "hwid": ""}]):
            out = main_mod.api_serial_ports()
        self.assertEqual(out["ports"][0]["port"], "COM5")
        self.assertEqual(out["current"]["port"], "COM3")


if __name__ == "__main__":
    unittest.main()
