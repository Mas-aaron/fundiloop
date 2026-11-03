/* Karaoke wire speech + TTS prefetch + flash retry: focused check for
 * frontend/app.js.
 *
 * Loads the REAL app.js with minimal DOM stubs (no framework, node only).
 * Narrator checks drive speakWireList / wireSpeechItems / speakGuideStep
 * with a fake voice: phrases must sound in order, each wire must light
 * up exactly as it is named, and a cancel or a newer speech must retire
 * the rest. The prefetch check drives playServerChunks with deferred
 * synthesis + stub audio: chunk 3 must be requested while chunk 1 plays.
 * The retry check drives webFlash guards + closeWebPortQuiet with a fake
 * port: a running check is stopped with a retap prompt, the selection
 * survives checks, empty routes to the picker, and a failed attempt
 * leaves the button free to retry.
 *
 * Run: node frontend/test_karaoke.js   (exit 0 = pass)
 */
const fs = require("fs");
const path = require("path");
const assert = require("node:assert");

// --- Minimal DOM stubs: just enough for app.js top-level code to run. ---
function makeEl(tag) {
  const store = {
    tagName: String(tag || "div").toUpperCase(),
    style: {},
    dataset: {},
    textContent: "",
    innerHTML: "",
    value: "",
    checked: false,
    hidden: false,
    disabled: false,
  };
  return new Proxy(store, {
    get(o, k) {
      if (k in o) return o[k];
      if (k === "classList") {
        if (!o._cls) o._cls = new Set();
        const s = o._cls;
        return {
          add(...c) { c.forEach((x) => s.add(x)); },
          remove(...c) { c.forEach((x) => s.delete(x)); },
          toggle(c, f) {
            const on = f !== undefined ? !!f : !s.has(c);
            if (on) s.add(c); else s.delete(c);
            return on;
          },
          contains(c) { return s.has(c); },
        };
      }
      if (k === "querySelectorAll") return () => [];
      if (k === "querySelector") return () => null;
      if (k === "getAttribute") return () => null;
      if (k === "closest") return () => null;
      if (k === "appendChild") return (c) => c;
      if (k === "addEventListener") return () => {};
      if (k === "removeEventListener") return () => {};
      if (k === "remove" || k === "click" || k === "focus" || k === "blur") return () => {};
      if (k === "removeAttribute" || k === "setAttribute") return () => {};
      if (k === "scrollIntoView") return () => {};
      return undefined;
    },
    set(o, k, v) { o[k] = v; return true; },
  });
}

const elsById = {};
globalThis.document = {
  getElementById: (id) => elsById[id] || (elsById[id] = makeEl()),
  createElement: (t) => makeEl(t),
  querySelectorAll: () => [],
  querySelector: () => null,
  body: makeEl("body"),
  documentElement: { style: { setProperty() {}, removeProperty() {} } },
  addEventListener: () => {},
  removeEventListener: () => {},
};
globalThis.window = {};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.navigator = {};
globalThis.location = { href: "http://localhost/", reload() {} };
globalThis.fetch = async () => ({ ok: false, status: 0, json: async () => ({}), text: async () => "" });
globalThis.requestAnimationFrame = () => 0;
globalThis.WebSocket = class { constructor() { this.readyState = 3; } send() {} close() {} };
// Controllable audio: instances are driven by hand via .onended().
globalThis.Audio = class {
  constructor(url) {
    this.url = url;
    this.onended = null; this.onplaying = null; this.onerror = null;
    globalThis.Audio.instances.push(this);
  }
  play() { return Promise.resolve(); }
  pause() {}
};
globalThis.Audio.instances = [];
// Controllable browser speech: utterances are recorded with a sync
// onend (same timing as the old no-speechSynthesis early return).
globalThis.__spoken = [];
globalThis.SpeechSynthesisUtterance = class {
  constructor(text) { this.text = text; }
};
globalThis.window.speechSynthesis = {
  speaking: false, pending: false, paused: false,
  getVoices: () => [],
  cancel() { this.speaking = false; this.pending = false; },
  resume() {},
  speak(u) {
    globalThis.__spoken.push(u && u.text !== undefined ? u.text : String(u));
    if (u && u.onend) u.onend();
  },
};
// Browsers alias window props to globals; app.js uses both spellings.
globalThis.speechSynthesis = globalThis.window.speechSynthesis;

// --- Probe: appended to the real app source, same scope. ---
const probe = `
;(async function karaokeProbe() {
  const calls = { say: [], isolate: [] };
  const hold = []; // pending onend continuations, in phrase order
  const fakeSay = (text, onend) => { calls.say.push(text); hold.push(onend); };
  isolateSchematicWires = (...a) => { calls.isolate.push(a); };
  const reset = () => { calls.say.length = 0; calls.isolate.length = 0; hold.length = 0; };

  const wires = [
    { part: "bme280", part_pin: "SDA", board_pin: "GPIO21", step_added: 2 },
    { part: "bme280", part_pin: "SCL", board_pin: "GPIO22", step_added: 2 },
  ];

  // 1. Items: ids match the diagram, phrases read part/pin/board, cap + tail.
  const items = wireSpeechItems(wires, 2);
  assert.equal(items.length, 2);
  assert.equal(items[0].id, schematicWireId(wires[0]));
  assert.equal(items[0].phrase, "bme280 SDA to GPIO21");
  assert.ok(items[0].label.indexOf("added step 2") >= 0);
  const many = [];
  for (let k = 0; k < 18; k++) {
    many.push({ part: "p", part_pin: "P" + k, board_pin: "GPIO" + k, step_added: 1 });
  }
  const capped = wireSpeechItems(many, 1);
  assert.equal(capped.length, 17);
  assert.equal(capped[16].id, "");
  assert.ok(capped[16].phrase.indexOf("2 more wires") >= 0);
  console.log("PASS items");

  // 2. Order: intro sounds with no highlight, then each wire lights as named.
  reset();
  speakWireList(
    [{ id: "", phrase: "Step 2 wiring" }].concat(items), fakeSay, "svg", "sel");
  assert.deepEqual(calls.say, ["Step 2 wiring"]);
  assert.deepEqual(calls.isolate[0].slice(0, 3), ["svg", "sel", []]); // start clears taps
  hold[0]();
  assert.deepEqual(calls.isolate[1].slice(0, 3), ["svg", "sel", [items[0].id]]);
  assert.deepEqual(calls.say, ["Step 2 wiring", "bme280 SDA to GPIO21"]);
  hold[1]();
  assert.deepEqual(calls.isolate[2].slice(0, 3), ["svg", "sel", [items[1].id]]);
  assert.equal(calls.say[2], "bme280 SCL to GPIO22");
  hold[2]();
  assert.equal(wireSpeech, null);
  assert.deepEqual(calls.isolate[3].slice(0, 3), ["svg", "sel", []]);
  assert.equal(calls.say.length, 3);
  console.log("PASS order");

  // 3. Cancel: a retired sequence speaks and lights nothing more.
  reset();
  speakWireList(items, fakeSay, "svg", "sel");
  const stale = hold[hold.length - 1];
  const cs0 = calls.say.length, ci0 = calls.isolate.length;
  cancelWireSpeech();
  stale();
  assert.equal(calls.say.length, cs0);
  assert.equal(calls.isolate.length, ci0);
  assert.equal(wireSpeech, null);
  console.log("PASS cancel");

  // 4. Supersede: newer speech from anywhere else retires the sequence.
  reset();
  speakWireList(items, fakeSay, "svg", "sel");
  const bumped = hold[hold.length - 1];
  const s0 = calls.say.length, i0 = calls.isolate.length;
  speakSeq++; // another speak() stole the mic
  bumped();
  assert.equal(calls.say.length, s0);
  assert.equal(calls.isolate.length, i0);
  assert.equal(wireSpeech, null);
  console.log("PASS supersede");

  // 5. Guide card: wire cards narrate in card order, check cards speak whole.
  cookSchematicState.wires = wires;
  cookSchematicState.n = 2;
  const realCookSpeak = cookSpeak;
  cookSpeak = fakeSay;
  reset();
  speakGuideStep({ n: 2, title: "Wire the sensor", text: "do it",
    wire_ids: [items[1].id, items[0].id] });
  assert.deepEqual(calls.say, ["Step 2: Wire the sensor"]);
  hold[0]();
  assert.equal(calls.say[1], "bme280 SCL to GPIO22");
  assert.deepEqual(calls.isolate[1].slice(0, 3), ["cook-schematic-svg", "cook-schematic-sel", [items[1].id]]);
  reset();
  speakGuideStep({ n: 3, title: "Check", text: "look", wire_ids: [] });
  assert.deepEqual(calls.say, ["Check. look"]);
  cookSpeak = realCookSpeak;
  console.log("PASS guide");

  // 6. Prefetch: chunk 3 synthesis starts while chunk 1 still plays, so
  // a short chunk never ends before the next one is ready (dead air).
  const fetchCalls = [];
  const fetchResolvers = [];
  const realFetchSpeech = fetchServerSpeech;
  fetchServerSpeech = (t) => {
    fetchCalls.push(t);
    return new Promise((res) => fetchResolvers.push(res));
  };
  globalThis.Audio.instances.length = 0;
  const tick = () => new Promise((r) => setTimeout(r, 0));
  let prefetchDone = 0;
  playServerChunks(["alpha one", "bravo two", "charlie three"], speakSeq,
    () => { prefetchDone++; });
  assert.deepEqual(fetchCalls, ["alpha one", "bravo two"]); // primed pre-sound
  fetchResolvers[0]("u0");
  await tick();
  assert.deepEqual(fetchCalls, ["alpha one", "bravo two", "charlie three"]);
  assert.equal(globalThis.Audio.instances.length, 1);
  globalThis.Audio.instances[0].onended();
  fetchResolvers[1]("u1");
  await tick();
  assert.equal(globalThis.Audio.instances.length, 2);
  globalThis.Audio.instances[1].onended();
  fetchResolvers[2]("u2");
  await tick();
  assert.equal(globalThis.Audio.instances.length, 3);
  globalThis.Audio.instances[2].onended();
  await tick();
  assert.equal(prefetchDone, 1);
  assert.equal(fetchCalls.length, 3); // each chunk fetched exactly once
  fetchServerSpeech = realFetchSpeech;
  console.log("PASS prefetch");

  // 7. Flash retry: a busy turn asks for another tap (never a
  // silent drop), an empty selection routes to the picker, and
  // releasing the port keeps the selection for next time.
  learnSid = "test-sid";
  flashBusy = false;
  busy = true;
  webPort = { readable: null, writable: null };
  await webFlash("retry-status", "retry-prog");
  assert.ok($("retry-status").textContent.indexOf("press FLASH IN BROWSER again") >= 0);
  busy = false;
  webPort = null;
  $("retry-status").textContent = "";
  await webFlash("retry-status", "retry-prog");
  assert.ok($("retry-status").textContent.indexOf("CONNECT BOARD") >= 0);
  const kept = { readable: {}, writable: {},
    close: async function () { this.readable = null; this.writable = null; } };
  webPort = kept;
  await closeWebPortQuiet();
  assert.equal(webPort, kept); // closed, still selected
  assert.equal(kept.readable, null);
  learnSid = "";
  console.log("PASS retry");

  // 8. Flash failure resets: a failed attempt leaves the button free to
  // retry (guard off, buttons enabled, failure on screen).
  learnSid = "test-sid";
  flashBusy = false;
  busy = false;
  webPort = { readable: {}, writable: {}, close: async () => {} };
  $("retry-status").textContent = "";
  await webFlash("retry-status", "retry-prog");
  assert.equal(flashBusy, false);
  assert.ok($("retry-status").textContent.indexOf("flasher unavailable") >= 0);
  assert.equal($("web-flash").disabled, false);
  learnSid = "";
  webPort = null;
  console.log("PASS flash-reset");

  // 9. Failure kinds: expired sessions, dead handles, and sync misses
  // each route to their own recovery guidance.
  assert.equal(flashFailureKind("unknown learning session id"), "expired");
  assert.equal(flashFailureKind("The device was disconnected."), "dead");
  assert.equal(flashFailureKind("No such device"), "dead");
  assert.equal(flashFailureKind("Failed to execute 'open' on 'SerialPort': Failed to open serial port."), "busy");
  assert.equal(flashFailureKind("Packet content transfer stopped"), "link");
  assert.equal(flashFailureKind("Invalid head of packet (0x80): Possible serial noise or corruption."), "sync");
  assert.equal(flashFailureKind("Failed to connect with the device"), "sync");
  assert.equal(flashFailureKind("Timed out waiting for packet header"), "sync");
  assert.equal(flashFailureKind("No serial data received."), "sync");
  assert.equal(flashFailureKind("compile failed: nope"), "generic");
  assert.equal(flashFailureKind(""), "generic");
  console.log("PASS failure-kinds");

  // 10. Cook voice feedback: TALK stays available mid-check like the
  // home mic; transcripts heard while busy queue and drain after.
  cookMode = true;
  busy = true;
  $("cook-heard").textContent = "";
  cookListen();
  assert.equal($("cook-heard").textContent, "Audio capture unavailable — use CHECK.");
  cookMode = false;
  $("cook-heard").textContent = "";
  cookListen();
  assert.equal($("cook-heard").textContent, "");
  cookMode = true;
  await handleCookTranscript("why is it dark?", "");
  assert.equal(cookPendingVoice, "why is it dark?");
  assert.ok($("cook-heard").textContent.indexOf("queued") >= 0);
  const realCookCommand = cookCommand;
  let drained = "";
  cookCommand = (t) => { drained = t; };
  drainCookVoice();
  assert.equal(drained, "why is it dark?");
  assert.equal(cookPendingVoice, "");
  cookPendingVoice = "stale";
  cookMode = false;
  drained = "";
  drainCookVoice();
  assert.equal(drained, "");
  cookCommand = realCookCommand;
  cookMode = false;
  busy = false;
  cookPendingVoice = "";
  console.log("PASS cook-voice");

  // 11. Quiet auto-listens: hands-free silence stays off-screen
  // while manual taps still report it.
  cookMode = true;
  cookListenAuto = true;
  cookSilentStreak = 0;
  $("cook-heard").textContent = "";
  await handleCookTranscript("", "");
  assert.equal($("cook-heard").textContent, "");
  cookListenAuto = false;
  await handleCookTranscript("", "");
  assert.equal($("cook-heard").textContent, "Heard nothing.");
  cookMode = false;
  learnSid = "";
  console.log("PASS cook-quiet");

  // 12. Wave orb: taps are guarded no-ops without Web Audio, and the
  // mode/level API stays callable from every speech path.
  OrbWave.setMode("speak");
  OrbWave.blip(0.7);
  OrbWave.tapElement({});
  OrbWave.tapStream(null);
  OrbWave.releaseMic();
  assert.equal(OrbWave.level(), 0); // rAF never fires under stubs
  OrbWave.setMode("idle");
  console.log("PASS orb-wave");

  // 13. Speak failure: the server's reason reaches the console and the
  // utterance still resolves to null for browser fallback.
  const realFetch = globalThis.fetch;
  const realConsoleLog = console.log;
  const logged = [];
  console.log = (...a) => { logged.push(a.join(" ")); };
  globalThis.fetch = async () => ({ ok: false,
    json: async () => ({ detail: "kaboom" }) });
  assert.equal(await fetchServerSpeech("hi"), null);
  assert.ok(logged.some((l) => l.indexOf("kaboom") >= 0));
  globalThis.fetch = async () => { throw new Error("down"); };
  assert.equal(await fetchServerSpeech("hi"), null);
  globalThis.fetch = realFetch;
  console.log = realConsoleLog;
  console.log("PASS speak-log");

  // 14. Speak retry: a failed attempt retries once before falling back,
  // healing transient model-file reads.
  const realFetch2 = globalThis.fetch;
  const realConsoleLog2 = console.log;
  const logged2 = [];
  console.log = (...a) => { logged2.push(a.join(" ")); };
  let speakCalls = 0;
  globalThis.fetch = async () => {
    speakCalls++;
    if (speakCalls === 1) {
      return { ok: false, json: async () => ({ detail: "flaky" }) };
    }
    return { ok: true, blob: async () => new Blob(["x"]) };
  };
  const healed = await fetchServerSpeech("hi");
  assert.ok(healed && healed.indexOf("blob:") === 0);
  assert.equal(speakCalls, 2);
  assert.ok(logged2.some((l) => l.indexOf("flaky") >= 0));
  globalThis.fetch = realFetch2;
  console.log = realConsoleLog2;
  console.log("PASS speak-retry");

  // 15. Schematic tabs: the Diagram tab always shows pin cards, the
  // Breadboard tab always shows the breadboard view, and one fetch
  // feeds both tabs so they can never disagree about the step.
  schematicState.svg = "<svg>cards</svg>";
  schematicState.breadboardSvg = "<svg>bb</svg>";
  cookSchematicState.svg = "<svg>cards</svg>";
  cookSchematicState.breadboardSvg = "<svg>bb</svg>";
  paintSchematic("schematic-svg", "schematic-sel", schematicState, false, 0);
  paintSchematic("cook-schematic-svg", "cook-schematic-sel", cookSchematicState, false, 0);
  assert.equal($("schematic-svg").innerHTML, "<svg>cards</svg>");
  assert.equal($("cook-schematic-svg").innerHTML, "<svg>bb</svg>");
  setSchematicView("breadboard"); // the legacy toggle must not leak across tabs
  assert.equal($("schematic-svg").innerHTML, "<svg>cards</svg>");
  assert.equal($("cook-schematic-svg").innerHTML, "<svg>bb</svg>");
  setSchematicView("cards");
  const realFetch3 = globalThis.fetch;
  learnSid = "tab-sid";
  learnStepN = 2;
  schematicState.sid = "";
  cookSchematicState.sid = "";
  globalThis.fetch = async () => ({ ok: true, json: async () => ({
    n: 2, svg: "<svg>cards2</svg>", breadboard_svg: "<svg>bb2</svg>",
    netlist: { wires: [], notes: [] }, unresolved: [] }) });
  await fetchSchematic("cook-schematic-svg", "cook-schematic-wires",
    cookSchematicState, "cook-schematic-sel", 2);
  assert.equal(cookSchematicState.n, 2);
  assert.equal(schematicState.n, 2); // sibling tab shares the step
  assert.equal($("schematic-svg").innerHTML, "<svg>cards2</svg>");
  assert.equal($("cook-schematic-svg").innerHTML, "<svg>bb2</svg>");
  globalThis.fetch = realFetch3;
  learnSid = "";
  console.log("PASS schematic-tabs");

  // 16. Schematic zoom: each tab zooms independently (50-300%), both
  // come up at 50% so the whole circuit fits, and repaints keep the
  // adjustment until Fit resets it.
  assert.equal(schematicZoom["schematic-svg"], 0.5); // default 50%
  assert.equal(schematicZoom["cook-schematic-svg"], 0.5);
  setSchematicZoom("schematic-svg", 1.5);
  assert.equal($("schematic-zoom-label").textContent, "150%");
  setSchematicZoom("cook-schematic-svg", 0.5);
  assert.equal($("cook-schematic-zoom-label").textContent, "50%");
  setSchematicZoom("schematic-svg", 9); // clamps to 300%
  assert.equal($("schematic-zoom-label").textContent, "300%");
  paintSchematic("schematic-svg", "schematic-sel", schematicState, false, 0);
  assert.equal($("schematic-zoom-label").textContent, "300%"); // repaint keeps zoom
  resetSchematicZoom();
  assert.equal($("schematic-zoom-label").textContent, "50%");
  assert.equal($("cook-schematic-zoom-label").textContent, "50%");
  setSchematicZoom("schematic-svg", 2);
  $("schematic-zoom-fit").onclick();
  assert.equal($("schematic-zoom-label").textContent, "50%"); // Fit restores 50%
  setSchematicZoom("cook-schematic-svg", 2);
  $("cook-schematic-zoom-fit").onclick();
  assert.equal($("cook-schematic-zoom-label").textContent, "50%");
  console.log("PASS schematic-zoom");

  // 17. Step summary: one flowing narration with transitions, ids
  // matching the diagram, and a single Next Step action per step.
  appRevealed = true;
  cookView = { state: "BUILDING", instruction: "wire it" };
  const sumWires = [
    { part: "bme280", part_pin: "SDA", board_pin: "GPIO21", step_added: 2,
      part_title: "BME280 Sensor", color_name: "red" },
    { part: "bme280", part_pin: "SCL", board_pin: "GPIO22", step_added: 2,
      part_title: "BME280 Sensor", color_name: "" },
    { part: "oled", part_pin: "SDA", board_pin: "GPIO21", step_added: 2,
      part_title: "OLED Display", color_name: "blue" },
  ];
  const summary = stepSummaryItems(sumWires, 2, "ESP32 DevKit");
  assert.equal(summary.length, 5); // intro + 3 wires + outro
  assert.equal(summary[0].phrase, "Step 2 has 3 connections.");
  assert.ok(summary[1].phrase.indexOf("First, connect the GPIO21") === 0);
  assert.ok(summary[1].phrase.indexOf("using a red wire") >= 0);
  assert.equal(summary[1].id, schematicWireId(sumWires[0]));
  assert.ok(summary[2].phrase.indexOf("Next, connect the GPIO22") === 0);
  assert.ok(summary[3].phrase.indexOf("Finally, connect the GPIO21") === 0);
  assert.equal(summary[4].phrase, "Tap Next Step when everything is wired.");
  const single = stepSummaryItems([sumWires[0]], 1, "ESP32 DevKit");
  assert.equal(single.length, 3);
  assert.equal(single[0].phrase, "Step 1 has one connection.");
  assert.ok(single[1].phrase.indexOf("connect the GPIO21") === 0); // no lead word
  assert.deepEqual(stepSummaryItems([], 1, "ESP32 DevKit"), []);
  // The primary action skips per-wire stops: one tap per step.
  assert.deepEqual(
    coachPrimaryAction({ phase: "BUILDING", serverState: "BUILDING",
      wiresTotal: 4, wireIdx: 4, wireAcked: false, stepN: 2, stepsTotal: 4 }),
    { id: "next-step", label: "Next Step" });
  assert.deepEqual(
    coachPrimaryAction({ phase: "BUILDING", serverState: "DONE",
      wiresTotal: 0, wireIdx: 0, wireAcked: false, stepN: 4, stepsTotal: 4 }),
    { id: "deploy", label: "Deploy Firmware" });
  assert.deepEqual(
    coachPrimaryAction({ phase: "BUILDING", serverState: "BUILDING",
      wiresTotal: 0, wireIdx: 0, wireAcked: false, stepN: 4, stepsTotal: 4 }),
    { id: "deploy", label: "Deploy Firmware" });
  console.log("PASS step-summary");

  // 18. Code tab + visible-tab animation: the stage code panel fills
  // from any fetch, and only the active tab's diagram animates.
  learnSid = "code-sid";
  paintStageCode("void setup() {}", "draft (1 of 4 steps verified)");
  assert.equal($("stage-code-out").textContent, "void setup() {}");
  assert.equal($("stage-code-kind").textContent, "draft (1 of 4 steps verified)");
  assert.equal(stageCodeSid, "code-sid");
  learnSid = "";
  assert.equal(schematicActiveSvgId(), "cook-schematic-svg"); // no tabs under stubs
  console.log("PASS code-panel");

  // 19. Narration self-heals: naming a wire clears any parked dash
  // hiding so dots-without-wires can never strand a step.
  ensureWireDrawn("nope-svg", "wire-x"); // missing box: silent no-op
  ensureWireDrawn("svg", ""); // empty id: silent no-op
  const drawBox = $("draw-svg");
  const hiddenPath = { style: { strokeDasharray: "180",
    strokeDashoffset: "180", animation: "none" } };
  const drawGroup = { classList: { remove() {}, add() {}, toggle() {},
      contains() { return false; } },
    querySelectorAll: () => [hiddenPath] };
  drawBox.querySelector = () => drawGroup;
  ensureWireDrawn("draw-svg", "wire-bme280-SDA");
  assert.equal(hiddenPath.style.strokeDasharray, "none");
  assert.equal(hiddenPath.style.strokeDashoffset, "0");
  console.log("PASS ensure-drawn");

  // 20. Background narration: the leaf bubble hides while building
  // (voice carries it), returns for reveal/done, clears on exit.
  cookMode = true;
  learnSid = "narr-sid";
  appRevealed = true;
  appFault = false;
  monitorOn = false;
  flashBusy = false;
  $("flash-dialog").hidden = true;
  learnStepN = 2;
  learnStepTotal = 4;
  cookView = { state: "BUILDING", steps_total: 4, instruction: "wire it" };
  paintCoachBar();
  assert.equal(document.body.classList.contains("hide-narration"), true);
  cookView = { state: "DONE", steps_total: 4, instruction: "done" };
  paintCoachBar();
  assert.equal(document.body.classList.contains("hide-narration"), false);
  cookMode = false;
  paintCoachBar();
  assert.equal(document.body.classList.contains("hide-narration"), false);
  learnSid = "";
  cookView = null;
  console.log("PASS hide-narration");

  // 21. Dynamic port pick: keep live selections, auto-pick a lone
  // (or lone-USB) board, never invent a phantom default.
  const portList = [
    { port: "COM3", description: "Communications Port", hwid: "ACPI" },
    { port: "COM4", description: "USB-SERIAL CH340", hwid: "USB VID:PID=1A86" },
  ];
  assert.equal(pickFlashPort(portList, "COM4"), "COM4"); // live keep wins
  assert.equal(pickFlashPort(portList, ""), "COM4"); // lone USB board
  assert.equal(pickFlashPort([{ port: "COM9" }], ""), "COM9"); // lone port
  assert.equal(pickFlashPort(portList, "COM7"), "COM4"); // stale keep reseats
  assert.equal(pickFlashPort([], ""), "");
  const twoUsb = [{ port: "COM4", description: "USB Serial", hwid: "USB" },
    { port: "COM5", description: "USB Serial", hwid: "USB" }];
  assert.equal(pickFlashPort(twoUsb, ""), ""); // ambiguous stays explicit
  assert.equal(pickFlashPort(["COM3"], ""), "COM3"); // plain names work
  const loneBt = [{ port: "COM3",
    description: "Standard Serial over Bluetooth link",
    hwid: "BTHENUM\\VID&0000\\0" }];
  assert.equal(pickFlashPort(loneBt, ""), ""); // lone BT phantom never auto-picks
  const loneLegacy = [{ port: "COM1", description: "Communications Port",
    hwid: "ACPI\\PNP0501\\0" }];
  assert.equal(pickFlashPort(loneLegacy, ""), ""); // lone legacy UART never auto-picks
  assert.equal(pickFlashPort(portList, "COM3"), "COM3"); // explicit keep wins
  console.log("PASS pick-port");

  // 22. Enter in the project box suggests, like the button.
  busy = false;
  $("learn-project").value = "";
  $("talk-hint").textContent = "";
  $("learn-project").onkeydown({ key: "Enter", preventDefault() {} });
  assert.equal($("talk-hint").textContent,
    "Upload a parts photo or name a project first.");
  $("talk-hint").textContent = "";
  $("learn-project").onkeydown({ key: "x", preventDefault() {} });
  assert.equal($("talk-hint").textContent, "");
  console.log("PASS enter-suggest");

  // 23. Browser flash is visible: the bar shows the latest step, the
  // dialog keeps the terminal, progress drives both meters.
  $("flash-console").textContent = "";
  webLog("cook-flash-status", "Compiling on server…");
  assert.equal($("cook-flash-status").textContent, "Compiling on server…");
  assert.ok($("flash-console").textContent.indexOf("Compiling on server…") >= 0);
  webLog("cook-flash-status", "Build abc123 — downloading 2 file(s)…");
  assert.equal($("cook-flash-status").textContent,
    "Build abc123 — downloading 2 file(s)…");
  flashDialogOpen("Flashing from the browser…");
  assert.equal($("flash-console").textContent, "");
  assert.equal($("flash-dialog").hidden, false);
  flashDialogProgress(0.5);
  assert.equal($("flash-progress").value, 50);
  flashDialogDone(true, "Browser flash complete.");
  assert.equal($("flash-progress").value, 100);
  assert.equal($("flash-summary").textContent, "Browser flash complete.");
  console.log("PASS webflash-dialog");

  // 24. Chunk sizing: server synthesis runs ~50ms/char, so chunks stay
  // small enough that first audio sounds in ~2s warm; monster
  // sentences split at word gaps and silent pieces stay dropped.
  const sized = splitSpeakChunks("Nice work. Next: wire the sensor to GPIO4 " +
    "and ground, then flash the firmware and watch the monitor.");
  assert.ok(sized.length >= 2);
  assert.ok(sized.every((c) => c.length <= 60),
    "chunk over 60 chars: " + JSON.stringify(sized));
  assert.ok(sized[0].length <= 55);
  const monster = splitSpeakChunks(
    "Wire the HC-SR04 above the bin opening facing forward for hand " +
    "detection with VCC to VIN and ground to ground and TRIG to GPIO4.");
  assert.ok(monster.length >= 2);
  assert.ok(monster.every((c) => c.length <= 60));
  assert.deepEqual(splitSpeakChunks("Okay... ... Yes."), ["Okay... Yes."]);
  console.log("PASS chunk-sizing");

  // 25. Mute is total: fallback speech never sounds while muted (a
  // leaking utterance masks the muted state), and sounds again after
  // unmuting.
  const spokenBefore = globalThis.__spoken.length;
  muted = true;
  speakBrowserNow("You should not hear this.");
  assert.equal(globalThis.__spoken.length, spokenBefore);
  muted = false;
  speakBrowserNow("Audible.");
  assert.equal(globalThis.__spoken.length, spokenBefore + 1);
  console.log("PASS mute-total");
})();
`;

const appSrc = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const fail = (e) => {
  console.error("karaoke FAILED:", e && e.stack ? e.stack : e);
  process.exit(1);
};
let result;
try {
  result = eval(appSrc + probe); // async probe: wait for it below
} catch (e) { fail(e); }
Promise.resolve(result).then(() => {
  console.log("karaoke: all checks passed");
  process.exit(0);
}, fail);
