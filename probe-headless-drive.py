"""Headless verification drive for FundiLoop (throwaway diagnostic).

Starts a mock backend, drives the real UI in headless Chrome over CDP
(real typed text + Enter key + clicks), captures rendered frames and
console errors. Stdlib only (hand-rolled ws:// client for localhost CDP).

Run from the repo root (needs loopback sockets):
  backend\\.venv\\Scripts\\python.exe probe-headless-drive.py
"""
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
FRAMES = os.path.join(ROOT, "headless-frames")
PROFILE = os.path.join(ROOT, ".cdp-profile")
SERVER_LOG = os.path.join(ROOT, ".cdp-server.log")
PORT = 8124
CDP = 9224
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(msg):
    print("[drive] " + msg, flush=True)


class Ws:
    """Minimal ws:// client (localhost, no TLS, no extensions)."""

    def __init__(self, host, port, path):
        self.sock = socket.create_connection((host, port), timeout=15)
        self.sock.settimeout(60)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n"
               % (path, host, port, key))
        self.sock.sendall(req.encode())
        head = self._read_until(b"\r\n\r\n", 15)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError("ws upgrade refused: %r" % head[:120])
        accept = hashlib.sha1((key + WS_GUID).encode()).digest()
        if base64.b64encode(accept) not in head:
            raise RuntimeError("ws accept mismatch")

    def _read_until(self, marker, timeout):
        end = time.time() + timeout
        while marker not in self.buf:
            if time.time() > end:
                raise TimeoutError("ws read timeout")
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("ws closed")
            self.buf += chunk
        out, self.buf = self.buf.split(marker, 1)
        return out + marker

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("ws closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_text(self, text):
        data = text.encode()
        mask = os.urandom(4)
        frame = bytearray()
        frame.append(0x81)
        n = len(data)
        if n < 126:
            frame.append(0x80 | n)
        elif n < 65536:
            frame.append(0x80 | 126)
            frame += struct.pack("!H", n)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack("!Q", n)
        frame += mask
        frame += bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(frame))

    def recv_text(self):
        """Next complete text message (answers pings, skips control)."""
        while True:
            hdr = self._read_exact(2)
            b1, b2 = hdr[0], hdr[1]
            opcode, masked, ln = b1 & 15, b2 & 128, b2 & 127
            if ln == 126:
                ln = struct.unpack("!H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._read_exact(8))[0]
            if masked:
                self._read_exact(4)  # server must not mask; skip anyway
            payload = self._read_exact(ln) if ln else b""
            if opcode == 9:  # ping -> pong
                self.sock.sendall(b"\x8a\x00")
                continue
            if opcode == 8:
                raise ConnectionError("ws closed by peer")
            if opcode == 1:
                return payload.decode("utf-8", "replace")
            # continuation/binary/control: ignore (CDP uses text)

    def close(self):
        try:
            self.sock.sendall(b"\x88\x00")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class Cdp:
    def __init__(self, ws_url):
        assert ws_url.startswith("ws://")
        host_port, _, path = ws_url[5:].partition("/")
        host, _, port = host_port.partition(":")
        self.ws = Ws(host, int(port), "/" + path)
        self.next_id = 0
        self.events = []

    def call(self, method, params=None, timeout=60):
        self.next_id += 1
        my_id = self.next_id
        self.ws.send_text(json.dumps(
            {"id": my_id, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") == my_id:
                if "error" in msg:
                    raise RuntimeError("CDP %s: %s" % (method, msg["error"]))
                return msg.get("result", {})
            self.events.append(msg)
        raise TimeoutError("no CDP response for " + method)

    def evaluate(self, expr):
        res = self.call("Runtime.evaluate",
                        {"expression": expr, "returnByValue": True,
                         "awaitPromise": True})
        if res.get("exceptionDetails"):
            raise RuntimeError("page exception: %s" % json.dumps(
                res["exceptionDetails"])[:600])
        return (res.get("result") or {}).get("value")

    def shot(self, name):
        res = self.call("Page.captureScreenshot", {"format": "png"})
        path = os.path.join(FRAMES, name)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(res["data"]))
        log("frame: " + name)

    def wait_for(self, expr, seconds, label):
        end = time.time() + seconds
        while time.time() < end:
            try:
                if self.evaluate(expr):
                    return True
            except RuntimeError:
                pass
            time.sleep(0.4)
        raise TimeoutError("timed out waiting for: " + label)


def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def close_stale_chrome():
    port_file = os.path.join(PROFILE, "DevToolsActivePort")
    try:
        with open(port_file) as fh:
            port = int(fh.read().strip().splitlines()[0])
        tabs = http_json("http://127.0.0.1:%d/json/list" % port)
        for tab in tabs:
            if tab.get("type") == "page":
                try:
                    cdp = Cdp(tab["webSocketDebuggerUrl"])
                    cdp.call("Browser.close", timeout=10)
                    cdp.ws.close()
                except Exception:
                    pass
        time.sleep(2)
    except Exception:
        pass
    shutil.rmtree(PROFILE, ignore_errors=True)


def dump_problems(cdp):
    time.sleep(1)
    problems = []
    for ev in cdp.events:
        method = ev.get("method", "")
        if method == "Runtime.exceptionThrown":
            detail = json.dumps(ev["params"]["exceptionDetails"])[:2000]
            problems.append("EXCEPTION: " + detail)
        elif (method == "Log.entryAdded" and
              ev["params"]["entry"]["level"] == "error"):
            entry = ev["params"]["entry"]
            problems.append("CONSOLE: %s @ %s" % (
                entry.get("text"), entry.get("url")))
    if problems:
        log("PAGE PROBLEMS (%d):" % len(problems))
        for item in sorted(set(problems)):
            print(item, flush=True)
    else:
        log("no page errors logged")


def drive_steps(cdp):
    log("typing project + pressing Enter")
    cdp.evaluate("document.getElementById('learn-project').focus(); ''")
    cdp.call("Input.insertText", {"text": "Ultrasonic parking sensor"})
    cdp.call("Input.dispatchKeyEvent",
             {"type": "keyDown", "key": "Enter", "code": "Enter",
              "windowsVirtualKeyCode": 13})
    cdp.call("Input.dispatchKeyEvent",
             {"type": "keyUp", "key": "Enter", "code": "Enter",
              "windowsVirtualKeyCode": 13})
    try:
        cdp.wait_for("document.getElementById('learn-cards')"
                     ".children.length > 0", 25, "suggestion cards")
    except TimeoutError:
        log("CDP Enter produced no cards; trying synthetic key event")
        cdp.evaluate(
            "document.getElementById('learn-project').dispatchEvent("
            "new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); ''")
        try:
            cdp.wait_for("document.getElementById('learn-cards')"
                         ".children.length > 0", 25, "suggestion cards retry")
        except TimeoutError:
            pass
        state = cdp.evaluate(
            "JSON.stringify({value: document.getElementById('learn-project')"
            ".value, hint: document.getElementById('talk-hint').textContent,"
            " busy: (typeof busy !== 'undefined') ? busy : 'n/a',"
            " cards: document.getElementById('learn-cards').children.length,"
            " projects: (typeof learnProjects !== 'undefined')"
            " ? learnProjects.length : 'n/a'})")
        log("suggest state: " + state)
        if '"cards":0' in state.replace(" ", ""):
            raise
        log("retry recovered cards; continuing")
    cards = cdp.evaluate(
        "document.getElementById('learn-cards').children.length")
    hint = cdp.evaluate("document.getElementById('talk-hint').textContent")
    log("cards=%s hint=%s" % (cards, hint))
    cdp.shot("frame-2-suggest.png")

    log("clicking first project card")
    cdp.evaluate("document.querySelector('#learn-cards li').click(); ''")
    cdp.wait_for("!document.getElementById('cook-leaf').hidden", 30,
                 "cook leaf")
    cdp.wait_for("document.querySelector('#schematic-svg svg') !== null",
                 30, "diagram svg")
    chapter = cdp.evaluate(
        "document.getElementById('cook-chapter').textContent")
    coach = cdp.evaluate("document.getElementById('coach-text').textContent")
    log("chapter=" + chapter)
    log("coach=" + coach)
    ports = cdp.evaluate(
        "JSON.stringify({n: document.getElementById('cook-flash-port')"
        ".options.length, value: document.getElementById('cook-flash-port')"
        ".value, first: document.getElementById('cook-flash-port')"
        ".options[0].textContent})")
    log("deploy-port=" + ports)
    cdp.shot("frame-3-cook.png")
    default_zoom = cdp.evaluate(
        "JSON.stringify({label: document.getElementById("
        "'schematic-zoom-label').textContent, width: document"
        ".querySelector('#schematic-svg svg').style.width})")
    log("default-zoom=" + default_zoom)
    fit = cdp.evaluate(
        "JSON.stringify((() => {"
        " const box = document.getElementById('schematic-svg');"
        " const svg = box.querySelector('svg');"
        " const scroller = () => { let el = box, found = 'none';"
        "  while (el && el !== document.body) {"
        "   if (el.scrollHeight > el.clientHeight + 2)"
        "    { found = el.id || String(el.className); break; }"
        "   el = el.parentElement; } return found; };"
        " const asLoaded = {svgH: Math.round(svg.getBoundingClientRect()"
        "  .height), scroll: scroller()};"
        " setSchematicZoom('schematic-svg', 0.5);"
        " const at50 = {svgH: Math.round(svg.getBoundingClientRect()"
        "  .height), scroll: scroller(),"
        "  label: document.getElementById('schematic-zoom-label')"
        "   .textContent};"
        " return {asLoaded: asLoaded, at50: at50}; })())")
    log("zoom-fit=" + fit)
    time.sleep(0.4)
    cdp.shot("frame-3b-zoom50.png")
    monitor = cdp.evaluate(
        "JSON.stringify({outs: document.querySelectorAll('#monitor-out')"
        ".length, adopt: typeof adoptMonitorPort,"
        " startBound: typeof document.getElementById('monitor-start')"
        ".onclick})")
    log("monitor=" + monitor)
    cdp.evaluate("setDrawer('right', true); ''")
    time.sleep(0.4)
    drawer = cdp.evaluate(
        "JSON.stringify((() => {"
        " const r = document.getElementById('right-drawer')"
        "  .getBoundingClientRect();"
        " return {x: Math.round(r.x), w: Math.round(r.width),"
        "  vw: window.innerWidth}; })())")
    log("drawer=" + drawer)
    cdp.shot("frame-3c-monitor.png")
    cdp.evaluate("setDrawer('right', false); ''")
    cdp.evaluate("setSchematicZoom('schematic-svg', 0.5); ''")

    log("opening code tab")
    cdp.evaluate("switchStageTab('code'); ''")
    cdp.wait_for("document.getElementById('stage-code-out')"
                 ".textContent.length > 100", 25, "stage code")
    kind = cdp.evaluate(
        "document.getElementById('stage-code-kind').textContent")
    serial = cdp.evaluate(
        "document.getElementById('stage-code-out').textContent"
        ".indexOf('Serial.begin(') >= 0")
    log("code kind=%s hasSerial=%s" % (kind, serial))
    cdp.shot("frame-4-code.png")

    log("flash dialog stages")
    cdp.evaluate("flashDialogOpen('Flashing from the browser…');"
                 " flashDialogProgress(null); ''")
    cdp.evaluate("flashDialogLog('Compiling on server…');"
                 " flashDialogLog('Still compiling on server… 5s'); ''")
    time.sleep(0.6)
    cdp.shot("frame-5-flash-mid.png")
    cdp.evaluate("flashDialogProgress(0.5); ''")
    time.sleep(0.4)
    cdp.evaluate("flashDialogDone(true, 'Browser flash complete.'); ''")
    time.sleep(0.4)
    cdp.shot("frame-6-flash-done.png")


def main():
    os.makedirs(FRAMES, exist_ok=True)
    close_stale_chrome()
    log("starting mock backend on %d" % PORT)
    server_log = open(SERVER_LOG, "wb")
    server = subprocess.Popen(
        [os.path.join(ROOT, "backend", ".venv", "Scripts", "python.exe"),
         "-m", "uvicorn", "backend.app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=ROOT, stdout=server_log, stderr=subprocess.STDOUT,
        env=dict(os.environ, MOCK_META="true"))
    try:
        status = None
        for _ in range(80):
            try:
                status = http_json("http://127.0.0.1:%d/api/status" % PORT)
                break
            except Exception:
                time.sleep(0.5)
        if not status:
            raise RuntimeError("backend never came up")
        log("backend up, mock=%s" % status["config"]["mock_meta"])

        log("launching headless chrome")
        chrome = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--remote-debugging-port=%d" % CDP,
             "--remote-debugging-address=127.0.0.1",
             "--user-data-dir=" + PROFILE, "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            tabs = None
            for _ in range(80):
                try:
                    tabs = http_json("http://127.0.0.1:%d/json/list" % CDP)
                    break
                except Exception:
                    time.sleep(0.5)
            if not tabs:
                raise RuntimeError("CDP endpoint never came up")
            pages = [t for t in tabs if t.get("type") == "page"]
            cdp = Cdp(pages[0]["webSocketDebuggerUrl"])
            try:
                cdp.call("Page.enable")
                cdp.call("Runtime.enable")
                cdp.call("Log.enable")
                cdp.call("Emulation.setDeviceMetricsOverride",
                         {"width": 1600, "height": 900,
                          "deviceScaleFactor": 1, "mobile": False})
                cdp.call("Page.navigate",
                         {"url": "http://127.0.0.1:%d/" % PORT})
                cdp.wait_for("document.readyState === 'complete'", 30,
                             "page load")
                time.sleep(2)
                cdp.shot("frame-1-home.png")

                try:
                    drive_steps(cdp)
                finally:
                    dump_problems(cdp)
                log("DRIVE COMPLETE")
            finally:
                cdp.ws.close()
        finally:
            try:
                chrome.terminate()
                chrome.wait(timeout=15)
            except Exception:
                try:
                    chrome.kill()
                except Exception:
                    pass
    finally:
        try:
            server.terminate()
            server.wait(timeout=15)
        except Exception:
            try:
                server.kill()
            except Exception:
                pass
        try:
            server_log.close()
        except Exception:
            pass
        time.sleep(1)
        shutil.rmtree(PROFILE, ignore_errors=True)
        for path in (SERVER_LOG,):
            try:
                os.remove(path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
