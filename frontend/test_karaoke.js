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
        return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
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

  // 7. Flash retry: a running check is stopped (never a silent tap),
  // an empty selection routes to the picker, and releasing the port
  // for a check keeps the selection for next time.
  learnSid = "test-sid";
  flashBusy = false;
  busy = true;
  let aborted = false;
  checkAbort = { abort: () => { aborted = true; } };
  webPort = { readable: null, writable: null };
  await webFlash("retry-status", "retry-prog");
  assert.equal(aborted, true);
  assert.ok($("retry-status").textContent.indexOf("press FLASH IN BROWSER again") >= 0);
  busy = false;
  checkAbort = null;
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

  // 11. Flash prompt + quiet auto-listens: a silent board with a step
  // sketch speaks the upload instruction once per step, and hands-free
  // silence stays off-screen while manual taps still report it.
  flashSpokenFor = "";
  learnSid = "s1";
  learnStepN = 2;
  const silentView = { id: "s1", step_code: { n: 2, code: "void setup(){}" },
    verification: { board_state: "silent:COM3" } };
  assert.ok(flashSpeech(silentView).indexOf("FLASH ESP32") >= 0);
  assert.equal(flashSpeech(silentView), ""); // once per step
  assert.ok(flashSpeech({ id: "s1", step_code: { n: 3, code: "x" },
    verification: { board_state: "silent:COM3" } }).indexOf("FLASH ESP32") >= 0);
  assert.equal(flashSpeech({ id: "s1",
    verification: { board_state: "silent:COM3" } }), ""); // no sketch
  assert.equal(flashSpeech({ id: "s1", step_code: { n: 4, code: "x" },
    verification: { board_state: "ok" } }), ""); // board talks
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
  console.log("PASS flash-prompt");

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
