# FundiLoop — verifiable assembly & diagnostic agent for embedded hardware

FundiLoop guides a technician by voice, watches the real circuit with an
overhead camera, verifies every step with real electrical measurements, and
only declares success when a sensor test passes. It is not a chatbot, not a
RAG app, and not a simple image analyzer.

Target circuit (the only one the MVP supports): ESP32 DevKit + BME280 (I2C
0x76/0x77) + SSD1306 OLED (I2C 0x3C) + LED on GPIO 2.

## Repo layout

- `backend/app/` — FastAPI app + orchestrator (`main.py`, `agent_orchestrator.py`,
  `circuit_graph.py`, `serial_probe.py`, `camera_client.py`, `change_detector.py`,
  `models.py`, `adapters/`)
- `backend/data/circuit_esp32_bme280_oled.yaml` — 8 assembly steps, LED wiring, faults
- `backend/requirements.txt` — Python deps
- `firmware/patient_board/patient_board.ino` — patient ESP32 firmware
  (`PING`, `I2C_SCAN`, `READ_SENSOR`, `SELFTEST` over USB serial as JSON lines)
- `frontend/` — mobile web UI served at `/` (plain HTML/JS, no framework)
- `.env.example` — config template (`CAMERA_STREAM_URL`, `CAMERA_CAPTURE_URL`, `SERIAL_PORT`, Meta keys)
- Legacy Day-1 files (`backend/main.py`, `backend/static/`, `esp32/`,
  `CameraWebServer_fundiloop/`) are kept for reference; the app runs from
  `backend/app/` + `frontend/`.
- `scripts/e2e_mock_demo.py` — mock-mode rehearsal: full guided run +
  SDA_SCL_SWAPPED diagnostic run.

## Architecture

```mermaid
flowchart LR
    Tech([Technician]) -->|CHECK NOW / PTT audio| UI[Mobile web UI]
    UI -->|speaks every instruction| TTS[(browser speechSynthesis)]
    UI -->|REST + audio upload| API[FastAPI backend]
    API --> ORCH[Agent orchestrator]
    ORCH --> CAM[ESP32-CAM overhead frame]
    CAM --> CD[change detector]
    CD -->|settled frame| SAM[Meta SAM 3.1]
    SAM -->|detections + boxes| ORCH
    ORCH --> SER[patient ESP32 serial probe]
    SER -->|I2C / sensor / OLED / LED| ORCH
    ORCH -->|state + evidence + history| Muse[Meta Muse Spark]
    Muse -->|AgentDecision JSON| ORCH
    ORCH -->|runs verification test| SER
    SER -->|pass / fail decides| ORCH
    ORCH -->|passport + hash-chained log| UI
    UI -->|PTT upload| ASR[Meta Voice Transcribe]
```

The loop is Sense → Reason → Instruct → Verify. Muse proposes; the
orchestrator runs each `verification_test` on real measurements and only a
passing sensor result advances a step or ends a session.

## How each Meta tool is used

- **SAM 3.1** (`MetaSAMAdapter`): segments each *settled* overhead frame
  (motion gating avoids per-frame calls) with one streamed cookbook request
  per concept noun phrase, decoded by `meta_sam_parser` into labeled boxes,
  masks, and confidences, plus the jumper-wire count.
- **Muse Spark** (`MetaClaudeAdapter`): receives circuit state, SAM
  segments, probe readings, ranked faults, recent history, and the optional
  code snippet; returns a validated `AgentDecision` (retried once on bad
  JSON). Advisory only — sensors decide pass/fail.
- **Voice Transcribe** (`MetaVoiceAdapter`): transcribes push-to-talk
  uploads from `POST /api/voice/transcribe`. Transcripts containing
  "done", "fixed", or "moved it" auto-trigger a loop turn. Speech *output*
  is the browser's `speechSynthesis`, with a replay button.

## Supported faults

- `SDA_SCL_SWAPPED` — live board, totally silent I2C bus (or swapped
  `Wire.begin` pins in pasted code).
- `LOOSE_GND` — no serial signs of life, or present-but-dark OLED.
- `CODE_PIN_MISMATCH` — firmware drives a different pin than the graph
  wires (LED or I2C pins).
- `SENSOR_DISCONNECTED` — OLED on the bus but no BME280 at 0x76/0x77.
- `LED_REVERSED` — GPIO toggles, LED stays dark, code pin matches.

## Safety scope

Low-voltage DC circuits only: USB-powered ESP32 logic at 3V3 (5V-tolerant
wiring errors are still bugs, not hazards). No mains voltage, no
high-current loads, no battery charging, no unattended operation. The LED
loop is current-limited (220 ohm series resistor). If anything smells hot
or the regulator overheats, unplug USB immediately — the agent cannot see
that; you are the safety interlock.

## Setup

1. Python 3.11+ with the project venv (or `python -m venv .venv`):
   `backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt`
2. `copy .env.example .env` — defaults run the full loop offline in mock mode
   (`MOCK_META=true`, no keys needed).
3. For real hardware, set in `.env`: `CAMERA_STREAM_URL` (e.g.
   `http://192.168.1.50:81/stream`), `CAMERA_CAPTURE_URL` (e.g.
   `http://192.168.1.50/capture`), `SERIAL_PORT` (e.g. `COM3`),
   `MOCK_META=false`, plus `META_API_KEY` for real Meta calls.

Patient board: flash `firmware/patient_board/patient_board.ino` with the
Arduino ESP32 core + Adafruit BME280 / SSD1306 / GFX libraries. The
overhead ESP32-CAM is pre-existing — set its still URL in `.env`, no
camera firmware in this repo.

## Run

From the repo root:

```
backend\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
```

Open `http://<laptop-ip>:8000` on your phone (same Wi-Fi), or
`http://localhost:8000` on the laptop.

API: `GET /api/status`, `POST /api/session/start`,
`POST /api/session/check-now` (or `/api/session/check-now/stream` for
ack + stage events), `GET /api/camera/latest`.

## Test the mock loop (no hardware, no keys)

Maintained suites (run first, socket-free):
`backend\.venv\Scripts\python.exe -m unittest backend.tests.test_guided_assembly backend.tests.test_diagnostic -v`

1. Start the server, open the page. Mode: Guided Assembly, step 1 instruction
   shown and spoken.
2. Press CHECK NOW (or push-to-talk and say "done") 8 times — each turn
   captures a settled frame, runs SAM + the step's serial verification test,
   and advances on pass; the checklist fills with PASS and an Assembly
   Passport appears.
3. Inject a fault: Advanced → Simulate fault → `SENSOR_DISCONNECTED` → CHECK
   NOW stays on the step with a corrective instruction grounded in the
   measured evidence. Set back to none → CHECK NOW verifies the fix.
4. Diagnostic mode: toggle Diagnostic (captures a SELFTEST + I2C + camera
   baseline), optionally paste Arduino code, inject `LED_REVERSED` + paste
   `#define LED_PIN 4` → CHECK NOW ranks all 5 faults by evidence and
   reports `CODE_PIN_MISMATCH`. Fix one thing per turn; if a re-test
   disproves the hypothesis it moves to the next ranked fault. It ends only
   when READ_SENSOR and CHECK_OLED pass with no fault outstanding, issuing
   a Repair Passport with before/after evidence.
5. Camera: `GET /api/camera/latest` serves the live preview frame, or
   HTTP 502 when the camera is unreachable — there are no synthetic frames.
6. Every attempt is logged to `backend/logs/attempts.jsonl` and counted on
   the Passport.

## 3-minute demo script

Rehearse offline first: `python scripts/e2e_mock_demo.py` (full guided run
+ SDA_SCL_SWAPPED diagnostic, hash chains verified).

1. **0:00 — Start.** Server up, page open on the phone. "FundiLoop guides
   a build and proves each step with live measurements."
2. **0:20 — Hands-free assembly.** Do a real wiring step on camera, hold
   PUSH TO TALK, say **"done"**. Transcript appears, the turn runs, the
   step passes, the next instruction is spoken. Hit REPLAY once to show it.
3. **1:10 — Fault + correction.** Pull the BME280 SDA wire (or pick a fault
   in Advanced). Say "done": FAULT_FOUND names the fault with measured
   evidence. Reseat, say "fixed": the re-test verifies the fix.
4. **2:00 — Diagnostic.** Toggle Diagnostic, inject `SDA_SCL_SWAPPED`,
   CHECK NOW: ranked fault list, one guided fix, clear, verify → Repair
   Passport.
5. **2:40 — Proof.** Open `GET /api/session/<id>/passport` (id is in any
   response): full hash-chained log, `chain_valid: true`.
6. **Proof it is real.** The header badge shows LIVE (green) only when
   the real camera and real Meta models are connected, MOCK (red)
   otherwise. Any camera/API failure returns the real error with HTTP 502
   and stops the step — nothing is silently simulated.

## Rule the code enforces

Muse never declares success alone: `agent_orchestrator.py` runs every
`verification_test` itself via the SAM adapter + serial probe, and only a
passing measurement advances the step or completes the session.

## Discovery layer

There is no fixed project catalog. Discovery is generative:

- `POST /discover/parts` (`{"frame_path": "..."}`) reports what equipment
  is visible in a saved frame: SAM regions plus a Muse Spark reading.
- `POST /discover/projects`
  (`{"visible_parts": [...], "user_project": "...", "constraints": "..."}`)
  returns model-generated build plans, each step mapped to a real test.
- The `AVAILABLE_TESTS` registry (`backend/app/capabilities.py`) lists the
  only verification tests plans may use: `RUN_I2C_SCAN`, `READ_SENSOR`,
  `CHECK_OLED`, `CHECK_GPIO`, `CHECK_SERIAL`.
- Rule: every plan step must map to a real test in `AVAILABLE_TESTS` and
  every `depends_on_parts` entry must be visible or listed in
  `missing_parts`, or the plan is rejected and the model is retried once
  with the reasons. Rejections land in `rejected_plans` for debugging.

### Scene understanding

- SAM provides precise regions for known part prompts.
- Muse Spark reads the frame and the SAM regions together and returns
  visible parts with state, observed connections, and uncertainties.
- A part Muse names but SAM did not box is marked `sam_confirmed: false`
  and shown as "unconfirmed" in the UI.
- A SAM box Muse does not name is shown as "unknown".
- Scene understanding is appearance only. It never states that the circuit
  works. The sensor decides correctness.

### Learning mode

There is no fixed project catalog. Plans are generated by the model but
validated and executed by the backend against the real verification module:

1. See parts with the camera: `POST /discover/capture` grabs a still,
   `POST /discover/parts` reports visible parts, observed connections,
   and uncertainties. No camera yet? `POST /discover/upload` (UPLOAD
   PHOTO in the Learning tab) runs the same pass on a photo file.
2. Name a project by text or voice, or let the system suggest one:
   `POST /discover/projects` returns up to 3 plans, each step mapped to
   an `AVAILABLE_TESTS` key, plus `missing_parts`/`alternatives`.
3. Start a plan with `POST /diagnose` (`mode: "learning"`), then loop
   CHECK NOW: the current step's sensor test runs, and the step advances
   only when the sensor confirms. Failures stay put with a targeted hint.
4. Code ships last and arrives on its own: when the final step passes,
   the UI fetches `POST /project/{session_id}/code` automatically (409
   until then) and shows the `.ino` with the pin numbers and addresses
   the sensors actually confirmed (SDA/SCL/LED, I2C devices). SEND CODE
   stays as a manual retry, plus a COPY button.
5. Plain sketches welcome: when the probe reports a GPIO/OLED step as
   *unverifiable* (no patient protocol on the board — not failed), the
   turn asks instead of failing, and CONFIRM advances it with a
   hash-chained `confirmed_by: technician` audit entry counted on the
   Passport. A measured negative still fails honestly.

The model proposes plans and instructions. The verification module
disposes: it alone advances steps, declares DONE, and gates firmware.

### Cook Mode (voice-guided builds)

Cook Mode is the NYT-Cooking-style runner for learning projects: starting
a project opens a full-screen step card with step X of Y, a progress bar,
the parts the step needs, and the live camera with SAM boxes. Everything
is spoken; the technician answers by voice:

- TALK (or hands-free auto-listen) records a short clip of speech.
- Say "check" to run the step's sensor verification, "repeat" to hear
  the step again, "look" to hear what the camera sees, or "exit" to
  leave. Anything else goes to the live coach over
  `WS /api/live/session`, which answers from the transcript, the probe
  readings, and the current step — spoken back.
- The sensor still alone advances steps and gates firmware; voice drives,
  it never declares success.

### Live guide (ambient coaching)

START LIVE GUIDE streams short camera clips over the same socket: SAM 3.1
tracks parts across each clip and Muse Spark coaches the next action out
loud. Entering Cook Mode pauses it so the two loops never talk over each
other. `scripts/vision_video_proof.py` and
`scripts/voice_realtime_proof.py` prove each live half against the real
Meta APIs.

## Boards & sensors

FundiLoop is board-general where it matters. `backend/app/boards.py`
holds one profile per patient board (I2C pins, GPIO/ADC maps, logic
voltage, serial baud); `SENSOR_INTERFACES` in `capabilities.py` maps
each sensor to its wiring and the test that verifies it.

- Learning mode (plan, Cook Mode, firmware) is board-general: pick the
  board in the Learning tab and plans, pin maps, and the generated `.ino`
  follow that profile. The sensor vocabulary covers DHT11/22, DS18B20,
  HC-SR04, PIR, MQ-2, LDR, LM35, soil, relay, RGB LED, LCD1602, and more.
- Guided Assembly and Diagnostic are fixed ESP32-kit flows. If the
  bench holds another board, the first CHECK NOW runs one cheap glance
  (cached while the I2C signature is steady) and hands off: it names
  the board, attaches the visible parts, and offers one-tap START
  LEARNING on that board instead of demanding an ESP32 swap. A changed
  bus re-glances automatically; RE-SCAN (Advanced) forces a re-detect
  on the next turn after swapping equipment.
- Latency budget per turn is deliberately minimal: guided/diagnostic pay
  one SAM call plus one Muse call (a second Muse pass only when a test
  fails); learning CHECK NOW runs on the probe alone and calls Muse only
  for a failure hint. The sense branches (camera settle, SAM, serial
  probe, first-turn board glance) overlap in threads, Meta HTTP clients
  persist across turns, and `POST /api/session/check-now/stream` acks
  immediately and ticks through sense → reason → verify. Frames are
  ~6 KB; per-stage latency rides in each view's `timings` (also shown
  on the state line), per-call latency in
  `backend/data/logs/model_calls.jsonl`. The serial probe adapts
  across 115200/9600 baud and short-circuits a silent board instead of
  burning a timeout per command. The UI stays interactive mid-turn:
  CANCEL stops waiting, and voice triggers heard while busy queue for
  the next turn. SHOW EQUIPMENT grounds 16 part prompts (overlapped
  8 at a time) plus one Muse reading, and runs only on demand.
- To add a board, add one entry to `BOARD_PROFILES` (label, mcu,
  voltage, i2c, led_builtin, gpio, adc, serial_baud) plus an option in
  the Learning tab selector. No other code changes needed.

## Real Meta API mode

Mocks are the default. For live calls, set in `.env`:

```
MOCK_META=false
META_API_KEY=<key>
META_API_BASE_URL=https://api.meta.ai/v1   # Muse chat + SAM vision base
META_API_TIMEOUT=30
MUSE_MODEL=muse-spark-1.3
SAM_MODEL=sam-3.1
VOICE_MODEL=meta-voice-1
# META_ASR_URL=                            # override only; default is base + /asr/transcribe
```

Call shapes mirror the legacy clients (`backend/muse_client.py`,
`backend/sam_client.py`, `backend/transcribe.py`): OpenAI-compatible chat
for Muse/SAM, multipart POST for voice. Muse output is forced to JSON,
validated into `AgentDecision`, and retried once; SAM boxes are parsed to
`{label, box, confidence}` detections.

Failure behavior: any camera error, API error, timeout, or unparseable
reply raises (`CameraError`/`MetaAPIError`), the API returns HTTP 502 with
the real error text, and the step stops. Nothing is replaced with mock
data or a canned instruction. Every model call is logged to
`backend/data/logs/model_calls.jsonl`, and every analysis still is saved to
`backend/data/debug_frames/`.

Voice: speech output is always the browser's `speechSynthesis`. Speech
input prefers the browser's speech recognition; where unavailable and live
voice is configured, push-to-talk records and uploads to
`POST /api/voice/transcribe` for Muse Voice Transcribe (WAV conversion via
pydub/ffmpeg when available, raw upload otherwise).
