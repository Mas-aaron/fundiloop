/* Karaoke wire speech: focused check for frontend/app.js.
 *
 * Loads the REAL app.js with minimal DOM stubs (no framework, node only)
 * and drives speakWireList / wireSpeechItems / speakGuideStep with a fake
 * narrator: phrases must sound in order, each wire must light up exactly
 * as it is named, and a cancel or a newer speech must retire the rest.
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

// --- Probe: appended to the real app source, same scope. ---
const probe = `
;(function karaokeProbe() {
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
})();
`;

const appSrc = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
try {
  eval(appSrc + probe);
  console.log("karaoke: all checks passed");
  process.exit(0);
} catch (e) {
  console.error("karaoke FAILED:", e && e.stack ? e.stack : e);
  process.exit(1);
}
