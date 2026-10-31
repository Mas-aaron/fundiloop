// Throwaway probe: exercise the REAL splitSpeakChunks from frontend/app.js.
// Fails loudly if the function cannot be extracted (no copied logic).
// Run from the repo root:  node probe-split-chunks.js
const fs = require("fs");

const src = fs.readFileSync("frontend/app.js", "utf8");
const start = src.indexOf("function splitSpeakChunks(say) {");
if (start < 0) { console.error("EXTRACT FAIL: function not found"); process.exit(1); }
let depth = 0, end = -1;
for (let i = src.indexOf("{", start); i < src.length; i++) {
  if (src[i] === "{") depth++;
  else if (src[i] === "}") { depth--; if (!depth) { end = i + 1; break; } }
}
if (end < 0) { console.error("EXTRACT FAIL: unbalanced braces"); process.exit(1); }
const splitSpeakChunks = new Function(src.slice(start, end) + "; return splitSpeakChunks;")();

const s1 = "We'll build the blinking light project soon";
const s2 = "You need the arduino uno and one red led";
const s3 = "Follow each step carefully and press check now to verify";
const decimal150 = "Set the value to 0.55 volts for the sensor threshold now please and " +
  "keep it steady for the full calibration routine today without touching it";
const monster = "word ".repeat(60).trim() + ".";
if (decimal150.length <= 140) { console.error("PROBE BUG: decimal case too short"); process.exit(1); }

const cases = [
  ["short single", "Planning your projects.", (c) =>
    c.length === 1 && c[0] === "Planning your projects."],
  ["packs smalls", s1 + ". " + s2 + ". " + s3 + ".", (c) =>
    c.length === 2 && c[0] === s1 + ". " + s2 + "." && c[1] === s3 + "."],
  ["decimals protected", decimal150 + ".", (c) =>
    c.length === 2 && c.every((x) => x.length <= 140) &&
    c.join(" ").includes("0.55")],
  ["monster splits even", monster, (c) =>
    c.length === 3 && c.every((x) => x.length <= 140 && x.length >= 60) &&
    (c.join(" ").match(/word/g) || []).length === 60],
  ["tinies pack", "First. Second! Third?", (c) =>
    c.length === 1 && c[0] === "First. Second! Third?"],
  ["empty", "", (c) => c.length === 1 && c[0] === ""],
];

let fails = 0;
for (const [name, input, check] of cases) {
  const got = splitSpeakChunks(input);
  const ok = check(got);
  if (!ok) fails++;
  console.log((ok ? "ok   " : "FAIL ") + name + " -> " + JSON.stringify(got).slice(0, 140));
}
process.exit(fails ? 1 : 0);
