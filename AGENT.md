# AGENT.md — FundiLoop Learning Coach (voice-first, teach-only)

## What this is
Voice-first teacher for FundiLoop **Learning Mode only**.
It teaches each project step out loud, answers questions, and shows code on request.
It does NOT gate progress on sensor checks. The learner does the step at their own pace
and says "next" / "done" when ready.

Guided Assembly and Diagnostic modes are unchanged (still sensor-verified).

## Identity
- Name: FundiLoop Learning (default — user did not pick a new name).
- For: just the user (single-user, no per-user state needed yet).
- Tone: warm, plain-spoken, brief. Spoken voice: warm female.

## First three capabilities (defaults — user did not override)
1. Explain the current step out loud, teach-style (what + why + how).
2. Answer "why / what is this part" questions about the step and parts.
3. Draft and show the project `.ino` code on request.

## Stack
- Keep current stack: Python FastAPI backend (`backend/app/`) + plain HTML/JS frontend (`frontend/`).
- No new framework. Harness stays small and readable.
- Brain: existing Meta Muse Spark adapter behind its current seam (swappable later).
- Runs laptop-first; phone browser is mic + speaker.

## Voice (applies to the ENTIRE project — Guided, Diagnostic, Learning, Cook Mode, Live Guide)
- Talk path: text first (same agent core), then push-to-talk. No open-mic / wake word yet.
- Speech-in (ears): keep existing path — browser speech recognition where available,
  else recorded upload to `POST /api/voice/transcribe` (Meta Voice Transcribe).
- Speech-out (mouth): **Meta Voice TTS, warm female voice, primary — everywhere the project speaks.**
- Offline/API-failure fallback: **fall back to default browser `speechSynthesis` for that utterance only**,
  then retry Meta Voice next turn. Speech never hard-stops on TTS failure.
- Voice choice + provider live in config/env, never hardcoded. API keys in `.env` only.
- Keep the typed interface alive forever as debug path and fallback.
- Transcript of what was heard stays visible in the UI while building.
- Never capture while speaking; new turn interrupts speech and listens.

## Boundaries (never without asking first)
- Never mark a step complete on its own — only the learner advances.
- Never overwrite, upload, or flash firmware — except the Learning-mode
  FLASH ESP32 button (explicit tap, user-picked port, ESP32 only).
- Never change serial/camera/config settings.
- Read-only actions (explain, describe camera view, draft code display) flow freely.

## Proactivity
- Quiet by default. Only nudge when truly useful (e.g. long idle mid-step,
  part looks missing). All nudges dismissible. No quiet-hours violation for non-urgent items.

## Architecture (one shared core, adapters on edges)
- One agent core: typed turn, spoken turn, and proactive nudge all flow through the same brain.
- Tiers: 1 brain (text loop) -> 2 hands (tool registry) -> 3 ears/mouth (voice wrap)
  -> 4 memory -> 5 heartbeat -> 6 rails. Build and verify one tier at a time.

## Status
- Tier 0 interview: partial answers received (TTS = Meta Voice, fallback = default, voice = warm female).
  All other questions used defaults listed above.
- Tier 1+2 DONE: `backend/app/coach.py` (CoachSession text loop + tool registry),
  `MetaClaudeAdapter.chat_text` thin seam, `scripts/learning_chat.py` terminal chat,
  `backend/tests/test_coach.py` (18 tests).
- Project-wide Meta Voice TTS DONE (all modes): `backend/app/tts.py`
  (MetaTTSProvider: mock WAV silence + real JSON POST, voice/model/endpoint
  from `META_TTS_VOICE`/`TTS_MODEL`/`META_TTS_URL`), `POST /api/voice/speak`
  (400 empty, 502 API failure), TTS fields on `/api/status`, frontend
  `VoiceEngine` (Meta audio primary, default browser speech fallback per
  utterance, interruptible streams), `backend/tests/test_tts.py` (16 tests).
  Full suite: 228 tests OK. NOTE: the public Meta API documents Transcribe
  but no TTS model today, so real-mode TTS 502s until Meta ships it; the
  fallback keeps every utterance audible and the primary lights up with no
  code change when the endpoint exists.
- Tier 3 DONE (ears/mouth): web voice wrap around the same CoachSession
  brain — `POST /api/coach/ask` (blocking) + `POST /api/coach/ask/stream`
  (SSE: ack → tool_call/tool_result → token* → done/error) in
  `backend/app/main.py` with a 32-entry in-memory session store (one id =
  one remembered conversation; fresh id or restart forgets). Cook Mode
  free questions flow through it (memory + tools, clauses spoken as they
  land via Meta TTS), the cook live-socket is removed, and a typed ASK
  box keeps the debug path alive. `backend/tests/test_coach_voice.py`
  (13 tests). Full suite: 241 tests OK.
- Tier 4 DONE (memory across restarts): one JSON file per coach session
  under `backend/data/coach_memory/` (git-ignored, `COACH_MEMORY_DIR`
  overrides). `CoachSession(session_id=...)` loads at init and saves
  atomically after every turn; corrupt files start fresh, ids sanitize
  to safe filenames, oldest files prune past 32. Web sessions restore
  after a backend restart; terminal chat takes an optional session id
  (default `terminal`) and gains `/forget`. `backend/tests/test_coach_memory.py`
  (10 tests). Full suite: 251 tests OK.
- Tier 5 DONE (heartbeat/proactive): `CoachSession.nudge()` — rules
  (idle threshold, quiet hours, cooldown, all env-configured) decide
  WHETHER, the same brain writes WHAT (short template offline, model
  live). Nudge state persists in the session file; nudge turns skip the
  mock memory-line. `POST /api/coach/nudge` serves Cook Mode's 60s
  heartbeat: spoken + dismissible banner, failures stay silent.
  `backend/tests/test_coach_nudge.py` (15 tests). Full suite: 266 tests OK.
- Tier 6 DONE (rails): unsafe tools (safe=False) pause for explicit
  CONFIRM/DENY via `CoachSession.confirm()` + `POST /api/coach/confirm`
  (409 on mismatch; pending is ephemeral across restarts);
  `COACH_GATE_STRICT=true` gates even safe tools. Rail events
  (proposals, confirmations, denials, gated runs, nudges, kill flips)
  append to hash-chained `backend/logs/coach_audit.jsonl`
  (`COACH_AUDIT_LOG` overrides). Persisted kill switch
  (`GET/POST /api/coach/killswitch`, STOP/ARM in Cook Mode, /stop//arm
  in terminal): tripped refuses all coach turns (503) until re-armed.
  All coach tools ship read-only, so the gate rests until an unsafe
  tool registers or strict mode flips on.
  `backend/tests/test_coach_rails.py` (20 tests). Full suite: 286 tests OK.
- Run: `backend\.venv\Scripts\python.exe scripts\learning_chat.py [session-id]`
  (MOCK_META=true rehearses offline).
- All six tiers built. Next: say what to harden or demo.
- Orb UI DONE: whole-app redesign around a state orb (idle/listening/
  thinking/speaking/error, wired to real voice+model activity), circular
  mic button, pill mode chips, settings modal (ex-Advanced), guided
  typed SEND row, mini orb in Cook Mode. Layout restructured with every
  live element id preserved; no backend changes.
- Kokoro TTS DONE: `TTS_PROVIDER=kokoro|meta` factory in `backend/app/tts.py`
  (`KokoroTTSProvider`, `af_heart` American female default, models in
  `backend/data/kokoro/`, `backend/requirements-kokoro.txt` for the
  optional install). Meta stays selectable but 502s (no TTS model on
  the API, verified live); browser fallback unchanged.
  `backend/tests/test_tts_kokoro.py` (17 tests, kokoro_onnx stubbed).
  Full suite: 303 tests OK.

## DONE: Learning-mode SVG schematic + ESP32 flash (all four phases)
- Phase 1 schematic DONE: `backend/app/schematic.py` (progressive
  netlist from plan parts + ESP32 pin map + `SENSOR_INTERFACES`,
  deterministic SVG, unknown parts "unresolved" with reasons, never
  invented wiring); `GET /project/{id}/step/{n}/schematic` ->
  `{netlist, svg, unresolved[]}` (404s + ESP32-only 400); frontend
  panel beside step code + SVG download + SPEAK WIRES wire list
  (learn + cook). `backend/tests/test_schematic.py` (15 tests).
- Phase 2 compile DONE: `backend/app/firmware_build.py` wrapping
  `arduino-cli compile` (ESP32 FQBN, builds cached by code hash under
  `backend/data/firmware_builds/`); CLI path/FQBN/timeouts via env;
  failures surface with log excerpts; ESP32-only. Hermetic tests with
  stubbed subprocess: `backend/tests/test_firmware_build.py`
  (13 tests).
- Phase 3 flash DONE: `POST /project/{id}/step/{n}/flash` `{port}` ->
  compile/cache -> release both probe handles -> esptool write_flash
  at build offsets -> settle + re-probe PING ->
  `{ok, build_hash, port, verify_ping}` (`backend/app/firmware_flash.py`;
  busy-port guidance, silent PING reported not failed, every flash
  audit-logged to `coach_audit.jsonl`); explicit FLASH ESP32 button +
  user-picked port dropdown (learn + cook), ticker + ack/verdict
  speech + busy guard. `backend/tests/test_firmware_flash.py`
  (18 tests).
- Phase 4 verification: `node --check frontend/app.js` + full backend
  suite in one batched run (see count below); `esptool` added to
  `backend/requirements.txt`; `arduino-cli` + ESP32 core documented
  as a one-time external install (README); manual ESP32 flash
  checklist in README (needs real hardware — run before trusting a
  new toolchain install).
- Full suite: 460 tests OK, `node --check frontend/app.js` clean —
  verified after the final edit.
- Log excerpts lead with root errors: toolchains print the cause
  early and the consequence late, so excerpts now surface `error`
  lines first, then the tail (build + flash). 1 test.
- Build auto-installs sketch libraries: includes map to Library
  Manager names (Adafruit SSD1306/GFX/BME280/Sensor, DHT,
  ArduinoJson, PubSubClient, NTPClient); missing ones install via
  `arduino-cli lib install` before compiling, best-effort (the
  compile still runs; unknown headers stay for the compile error to
  name). `ARDUINO_AUTO_LIBS=0` disables. Excerpts strip ANSI color
  codes. 10 tests.
- No wedged steps: Learn has the same MOVE ON escape as Cook (it
  aborts an in-flight check turn first, then advances audit-logged
  as unverified); every check turn aborts itself at 120s with a
  spoken timeout message instead of spinning forever; Cook
  CHECK/PHASE DONE/LOOK say they are still working instead of
  silently ignoring taps while busy.
- Browser flash API fix (verified against esptool-js 0.5.7 source):
  `ESPLoader` takes `{transport, baudrate, terminal}`, not a
  positional transport (the old call crashed in `getInfo`); CONNECT
  only picks the port while `main()` opens it; no `hardReset`
  exists — port release reboots the board. Loader chatter streams
  into the staged log, and CHECK vs FLASH now block each other both
  ways instead of fighting over the port. Sync failures print the
  hold-BOOT-and-retry recovery inline, and the picked port survives
  a failed attempt (closed, so CHECK still works) for instant retry.
  Progress you can actually see: the bar animates while connecting,
  shows % while writing, keeps its final state after failure, parks
  empty when no bytes flowed, and the panel scrolls into view at
  flash start. Baud fallback: corruption/timeout at 921600 retries
  automatically at 460800 then 115200 on the same open port.
  strings (it calls `.charCodeAt`), so downloads convert via a
  chunked `u8ToBstr` instead of passing Uint8Arrays.
- Auto-continue build loop (Cook Mode): CHECK refires every 20s so a
  PASS flows into the next phase untapped; repeats of an identical
  FAIL stay silent while panels update. AUTO ✓ button shows the
  countdown and toggles the loop; EXIT, DONE, or toggling off
  interrupts. Manual checks always speak.
- Louder tutor voice: Kokoro output peak-normalizes to 0.95 full
  scale (`_peak_normalize` in `tts.py`, gain capped at 5x so
  near-silence never bursts, silence untouched), so every utterance
  plays at a uniform loud level instead of ~30%. 3 pinned
  conversion tests updated to the new contract + 4 normalization
  tests. Needs a server restart.
- Spoken flash prompt + quiet auto-listens: a silent board with a
  step sketch now speaks "press FLASH IN BROWSER, then CHECK" once
  per step (even on a quiet repeat), and hands-free "Heard nothing"
  stays off-screen — only manual TALK taps report it. 1 more check
  in `frontend/test_karaoke.js`.
- Plain CONFIRM copy + sticky cook actions: attestation strings now
  read as a "quick eyeball check" ("I can't see whether GPIO2 is
  blinking from here…") instead of probe-firmware jargon, and the
  TALK/CHECK/PHASE DONE/CONFIRM row moved under the header with
  `position: sticky` so it never needs a scroll to reach.
- Cook mic feedback mid-check: TALK no longer dies silently while
  `busy` (with the auto-loop that was most of the time) — it records
  like the home mic and queues the transcript, answered from
  `checkNow`'s finally via `drainCookVoice`; exiting clears the
  queue. 1 more check in `frontend/test_karaoke.js`.
- Read-timeout retries: "No serial data received" (esptool's read
  timeout, sync-time or mid-write) joins the `sync` kind so it
  retries slower and earns the second 115200 shot; sync guidance now
  covers cable/port persistence, and progress names the file
  ("Flashing file 2 of 3…"). Harness assert added.
- Link-break retries: "Packet content transfer stopped" is now a
  retryable `link` failure kind (slower re-sync, own cable/port/BOOT
  guidance), 115200 gets a second shot, and packet corruption folds
  into the `sync` kind so the baud loop retries through one
  classifier. Harness asserts updated.
- Browser-flash port release: `POST /api/serial/release` drops both
  probe handles (same pair the server flash releases) and the page
  calls it after downloading binaries, fixing "Failed to open serial
  port" once any check adopts the port; the failure classifier gains
  a `busy` kind for leftover OS-level holders. 1 backend test + 1
  harness assert. Needs a server restart (new endpoint).
- Expired-session flash guidance: a stale page (server restarted,
  sessions are in-memory) 404s the firmware manifest, so the flash
  failure classifier (`flashFailureKind`: expired/dead/sync/generic)
  now tells the user to tap EXIT, start the project fresh, and flash
  again — spoken too. 1 more check in `frontend/test_karaoke.js`.
- Speak retry for transient reads: the new logging caught
  `Overlapped entries: 'af_heart.npy'` (numpy refusing a mid-change
  voices archive — download/cloud race, valid on recheck), so
  `fetchServerSpeech` retries once immediately before falling back
  to browser voice. 1 harness check. Keep model files stable (and
  OneDrive-pinned) while the server runs.
- Speak failures self-report: `/api/voice/speak` 502s now log
  `speak failed: <detail>` server-side and the page mirrors the
  detail to the console (fallback behavior unchanged), so the next
  transient-first-utterance failure names itself instead of showing
  a bare 502. Also fixed `test_confirm_race` (missing `use_mock_env`
  broke it once the suite loads the real `.env`). 2 tests.
- Trillion-style wave orb: `OrbWave` drives a new `.orb-wave` ring
  + orb brightness every animation frame from live levels — mic
  analyser while listening, speaker analyser while talking, word
  blips for browser speech, synthetic pulse where no tap exists
  (recognition path), slow breath at idle. Taps are guarded no-ops
  without Web Audio; reduced-motion skips the loop. Sync-failure
  guidance now closes by routing to FLASH ESP32. 1 harness check.
- Flash path decision (supersedes the removal notes below): an
  Arduino IDE upload at 921600 baud with full hash verify on the
  user's own board proved the hardware good and native esptool the
  working engine, while browser esptool-js 0.5.7 fails on the same
  link — so the user kept the server COM3/FLASH ESP32 buttons as the
  recommended path next to browser flash. `flashStep` now shares the
  abort-and-retap busy guard (the old "wait" text was a dead end
  under back-to-back checks) and sets the busy guard inside `try`;
  the spoken flash prompt names FLASH ESP32.
- Flash-takes-precedence fix: tapping either FLASH button mid-check
  now stops the check and asks for one more tap (checks can run
  back-to-back with the auto-loop, so "wait" was a dead end); CHECK
  tapped mid-flash says so on the active panel; the pre-check port
  release races a 2s timeout so a wedged port can't stick `busy` on
  with no cancel. 2 more checks in `frontend/test_karaoke.js`.
- Flash retry fix: releasing the page port for a check no longer
  drops the board selection (the auto-loop used to force a fresh
  CONNECT BOARD after every check); FLASH tapped mid-check now says
  so on screen instead of dying silently; a truly dead handle
  (unplugged USB) is the only path that sends the user back to the
  picker. 1 more check in `frontend/test_karaoke.js`.
- TTS dead-air fix: `playServerChunks` prefetches two chunks deep
  (chunk 3 synthesis starts while chunk 1 plays), so a short "You
  need: ..." chunk never ends before the next long one is ready. 1
  more check in `frontend/test_karaoke.js` (deferred synthesis + stub
  audio drive the real player).
- (Superseded: server-flash rows were removed here, then restored
  and kept per the flash-path decision above after Arduino IDE
  proved native esptool on the user's board.)
- Karaoke wire speech: guide cards and SPEAK WIRES now narrate wire
  by wire, lighting each wire on the diagram exactly as it is named
  (each phrase waits for the previous audio to finish); a check, tap,
  look, exit, or any newer speech retires the rest silently, and
  hands-free listening stays off mid-sequence. Sequencing, cancel,
  supersede, and guide paths verified by `node
  frontend/test_karaoke.js` (5 checks, loads the real app.js).
- PHASE DONE button (Cook Mode): one tap tells the system the phase
  is wired ("Got it — checking phase N"), then the sensor verifies
  as usual. Voice shares the path: done/finished/complete/ready/
  move on/next phase|step. Phrases verified by a node probe. (The
  server-flash buttons mentioned here were later restored and kept
  per the flash-path decision above; both flash paths keep UI.)
- Sketch layout fix: the staged sketch is now
  `step_sketch/step_sketch.ino` (Arduino requires folder and main
  file to share a name); previously `sketch/step_sketch.ino` failed
  real compiles with "main file missing". 1 test.
- Sequential-step fix: session start renumbers plan steps 1..N in
  list order (the model can no longer number them 0-based/gapped),
  so speech, chapters, schematic, guide, and flash always agree on
  the current step; the guide pairs chapters positionally and the UI
  tracks `step.n` directly. 3 tests.
- Bundle auto-fetch: `/esptool-js/bundle.js` downloads the pinned
  esptool-js release once on first use (`backend/app/esptool_js.py`,
  `ESPTOOL_JS_URL`/`ESPTOOL_JS_TIMEOUT` envs) and serves it locally;
  no manual copy step. Invalid/offline fetches 404 with manual
  fallback instructions. 5 more tests in `test_web_flash.py`.
- Browser flash (Web Serial, user-approved): CONNECT BOARD opens the
  browser's own port picker (Chrome/Edge; localhost is a secure
  context); FLASH IN BROWSER downloads the server-compiled binaries
  (`GET .../firmware` manifest + per-file downloads, build dir only)
  and flashes via the vendored esptool-js bundle
  (`/esptool-js/bundle.js`, one-time operator download, never a CDN)
  with progress bar, staged log, speech, and port release so CHECK
  NOW works right after. Server-side FLASH stays as the any-browser
  fallback; both share one busy guard. Browser flashes run outside
  the server audit trail. 9 tests in `test_web_flash.py`.
- Schematik-style build guide: `GET /project/{id}/guide` (parts
  table with qty/blurbs + numbered assembly cards naming exactly
  each step's new wires, with SVG ids); Cook Mode panel renders
  both, tapping a card highlights its wires and speaks it, current
  card stays marked. 10 tests in `test_build_guide.py`.
- Port UX fix: pre-flight distinguishes a missing port from a busy
  one (`port_missing` names available ports / says to plug the board
  in; `port_busy` keeps the Serial-Monitor guidance); the flash
  dropdown prefers a real enumerated port over a stale COM3.
- Schematic interactivity: hover tooltips on every group plus
  click-to-isolate (rest dim, selection described under the drawing,
  click again to restore). 6 flash tests + 1 schematic test.
- Requested builds ignore the bench: naming a project plans it
  exactly as asked (`validate_project(..., enforce_visible=False)`,
  prompt drops the visibility rule, session start never gates on
  parts). Open suggestions still use visible parts; sensor
  verification per step is unchanged. 5 tests.
- Alternatives-shape fix ("smart gate" case): the prompt now specifies
  the backup-plan object shape; the validator no longer rejects a
  buildable project over malformed backup plans (they only matter
  when parts are lacking, and then the reasons say so); accepted
  projects ship with unusable alternatives stripped. 5 tests.
- Flash pre-flight port check: the endpoint opens the picked port
  before compiling; a held port fails fast with 409 + Serial-Monitor
  guidance (audited as `port_busy`) instead of compiling first.
  4 tests in `test_firmware_flash.py`.
- Scope boundary: Uno flash and breadboard layout view still need
  their own approval. (The Web Serial browser-flash path was approved
  by the user and built — see below.)
- Empty-schematic UX fix: steps needing no hookup wires (built-in LED
  + USB serial) now draw the board box with an explicit "No external
  wiring — on-board only" message plus involved parts; netlist gains
  `requested[]` and an explanatory note, shown in the panel and spoken
  by SPEAK WIRES. Cook Mode also split into talk-bubbles | schematic
  columns on wide screens (stacked on phones).
- Planner honors the request: prompt requires the FIRST project to be
  the user's build, the retry keeps it (no silent swap), and an
  unmatched request returns an explanatory `hint` (validator reasons
  or visible parts to rephrase with), shown + spoken by the UI.
  5 tests in `test_discovery.py`.
- Progressive reveal schematic: every wire carries `step_added`; the
  SVG draws a breadboard strip with power/ground rails (node dots
  where wires tap them), highlights the current step's wires bright
  and dims earlier ones, and groups everything in stable `<g id>`
  (`esp32-board`, `breadboard`, `part-*`, `wire-*-*`,
  `unresolved-*`). No frontend changes needed. 5 tests.
- Real-mode test fix (environmental): installing opencv (declared
  dep) made `TEST_JPEG` a real JPEG, so two SAM tests asserting
  shipped-space boxes failed against the correct source-pixel
  rescale. Expectations now map through the same
  `prepare_frame`/`scale_box` contract (`expect_box` helper).

## SUPERSEDED (built above): Learning-mode SVG schematic + ESP32 flash plan
- Goal: Schematik-style project page in Learning mode — per-step wiring
  schematic (SVG) plus one-click push of the step's code to the board.
- Settled decisions (user, side chat): SVG schematic format; real
  board flashing (not copy-paste only); ESP32 only (Uno out of scope);
  backend-driven flash via Python esptool; `arduino-cli` + ESP32 core
  toolchain installed once on the laptop, offline after; per-step
  progressive schematic (wiring up to the current step), full project
  = last step.
- Boundary note: real flashing lifts the "never overwrite, upload, or
  flash firmware" rule for Learning mode only, explicit user action
  only — never automatic, always a user-picked port.
- Phase 1 — schematic: new `backend/app/schematic.py` (netlist from
  plan parts + ESP32 pin map + `SENSOR_INTERFACES`, deterministic SVG
  renderer, unknown parts render "unresolved", never invented wiring);
  `GET /project/{id}/step/{n}/schematic` -> `{netlist, svg,
  unresolved[]}`; frontend panel beside step code + SVG download +
  wire list into speech; hermetic tests (netlist, unresolved, SVG,
  endpoint 404s).
- Phase 2 — compile: new `backend/app/firmware_build.py` wrapping
  `arduino-cli compile` (ESP32 core), builds cached by code hash; CLI
  path via env; failures surface with log excerpt; hermetic tests with
  stubbed subprocess.
- Phase 3 — flash: `POST /project/{id}/step/{n}/flash` `{port}` ->
  compile/cache -> release probe handle -> esptool flash at build
  offsets -> re-probe PING -> `{ok, build_hash, port, verify_ping}`;
  explicit FLASH ESP32 button, busy-port guidance, every flash
  audit-logged; frontend ticker + ack speech + busy guard; tests
  (validation, stubbed esptool, handle release, audit).
- Phase 4 — verification: single batched run (`node --check`, full
  backend suite) + manual ESP32 flash checklist. New deps: `esptool`
  in requirements; `arduino-cli` as documented external binary.
- Scope boundary: this record covers the plan only. Saying "go" later
  authorizes these four phases; anything beyond (Uno flash, Web Serial
  path, breadboard layout view) needs its own approval.
- Done means: schematic renders per step with download; flash pushes a
  step sketch to ESP32 and re-verifies PING; suite green; manual flash
  checklist passed.
