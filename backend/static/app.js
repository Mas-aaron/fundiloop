/* FundiLoop voice-first UI: orb state machine + legacy backend calls.
 *
 * Backend calls (unchanged shapes):
 *   GET/POST /config, POST /frame, POST /probe, POST /diagnose,
 *   POST /transcribe, GET /frames/{name}
 * State machine: IDLE -> LISTENING -> THINKING -> SPEAKING -> IDLE (ERROR brief).
 */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// ---------- state ----------
const State = { IDLE: "IDLE", LISTENING: "LISTENING", THINKING: "THINKING", SPEAKING: "SPEAKING", ERROR: "ERROR" };
let state = State.IDLE;
let mode = "fix"; // "fix" | "learn" (presentation only; diagnose body unchanged)
let history = [];
let turns = [];
let learnSteps = 0;
let camUrl = "http://fundiloop.local/capture";
let camHost = "fundiloop.local";
let muted = false;
let errorTimer = null;
const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function setOrb(name) {
  const orb = $("orb");
  orb.classList.remove("is-listening", "is-thinking", "is-speaking", "is-error");
  if (name) orb.classList.add(name);
}

function setState(next, statusText) {
  state = next;
  if (next === State.IDLE) { setOrb(null); setStatus(statusText || "Ready"); }
  else if (next === State.LISTENING) { setOrb("is-listening"); setStatus("Listening..."); }
  else if (next === State.THINKING) { setOrb("is-thinking"); setStatus("Analyzing..."); }
  else if (next === State.SPEAKING) { setOrb("is-speaking"); if (statusText) setStatus(statusText); }
  else if (next === State.ERROR) {
    setOrb("is-error");
    setStatus(statusText || "Something went wrong");
    clearTimeout(errorTimer);
    errorTimer = setTimeout(() => { if (state === State.ERROR) setState(State.IDLE); }, 1400);
  }
}

function setStatus(t) { $("status").textContent = t; }

function shortLabel(text) {
  const words = String(text || "").split(/\s+/).filter(Boolean);
  if (!words.length) return "(no instruction)";
  return words.slice(0, 4).join(" ") + (words.length > 4 ? "..." : "");
}

// ---------- orb speech vibration (JS loop, transform-only for 60fps) ----------
let vibeTimer = null;
function startVibe() {
  stopVibe();
  if (reduceMotion) return;
  const wrap = $("orb-vibe");
  vibeTimer = setInterval(() => {
    if (state !== State.SPEAKING) { stopVibe(); return; }
    const x = (Math.random() * 5 - 2.5).toFixed(1);
    const y = (Math.random() * 5 - 2.5).toFixed(1);
    wrap.style.transform = "translate(" + x + "px," + y + "px)";
  }, 65);
}
function stopVibe() {
  if (vibeTimer) { clearInterval(vibeTimer); vibeTimer = null; }
  const wrap = $("orb-vibe");
  if (wrap) wrap.style.transform = "";
}

// ---------- speech ----------
function speechSupported() { return "speechSynthesis" in window; }

function speak(text, done) {
  const finish = () => { if (done) done(); };
  if (muted || !text) { finish(); return; }
  if (!speechSupported()) { finish(); return; } // caller shows full text
  try {
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "en-US";
    u.onstart = startVibe;
    u.onend = () => { stopVibe(); finish(); };
    u.onerror = () => { stopVibe(); finish(); };
    speechSynthesis.speak(u);
  } catch (e) { console.log("speech failed", e); stopVibe(); finish(); }
}

function stopSpeech() {
  stopVibe();
  try { if (speechSupported()) speechSynthesis.cancel(); } catch (e) { /* no speech */ }
}

// ---------- settings modal (/config unchanged) ----------
async function loadConfig() {
  try {
    const c = await (await fetch("/config")).json();
    if (c.esp32_host) { camHost = c.esp32_host; $("host").value = camHost; }
    if (c.cam_url) camUrl = c.cam_url;
  } catch (e) { console.log("config load failed", e); }
  $("cam-line").textContent = "cam: " + camUrl;
}

function openSettings() {
  $("settings-msg").textContent = "";
  $("settings-modal").hidden = false;
}
function closeSettings() { $("settings-modal").hidden = true; }

// ---------- log panel (old text UI, hidden by default) ----------
function toggleLog(force) {
  const panel = $("log-panel");
  const open = force !== undefined ? force : panel.hidden;
  panel.hidden = !open;
  $("log-chevron").textContent = open ? "▾" : "▸";
}

function addTurn(t) {
  turns.unshift(t);
  turns = turns.slice(0, 4);
  renderTurns();
}

function renderTurns() {
  if (!turns.length) {
    $("turns").innerHTML = '<div class="turn empty">No turns yet.</div>';
    return;
  }
  $("turns").innerHTML = turns.map((t) =>
    '<div class="turn">' +
    "<div><b>You:</b> " + esc(t.tech || "—") + "</div>" +
    "<div><b>Loop:</b> " + esc(t.loop || "—") + "</div>" +
    "<div><b>Probe:</b> found [" + esc((t.found || []).join(", ") || "—") +
    "] missing [" + esc((t.missing || []).join(", ") || "—") + "]</div>" +
    "<div><b>State:</b> " + esc(t.state || "—") +
    " (" + esc(String(t.confidence ?? "—")) + ")</div>" +
    "</div>"
  ).join("");
}

function setLogStatus(d) {
  $("log-status").textContent =
    "state: " + (d.state || "—") + "\n" +
    "fault: " + (d.fault_hypothesis || "—") + "\n" +
    "confidence: " + (d.confidence ?? "—") + "\n" +
    "verify: " + (d.verification_test || "—");
}

function logLine(msg) {
  const el = $("log-status");
  el.textContent += "\n" + msg;
}

// ---------- learning progress ----------
function renderLearn() {
  const show = mode === "learn";
  $("learn-progress").hidden = !show;
  if (!show) return;
  $("learn-step").textContent = learnSteps > 0 ? "Step " + learnSteps : "Step 1";
  const dots = Math.min(learnSteps, 12);
  $("learn-dots").innerHTML = dots
    ? Array.from({ length: dots }, () => "<span></span>").join("")
    : '<span class="todo"></span><span class="todo"></span><span class="todo"></span>';
}

// ---------- camera + frame upload (unchanged 3-tier flow) ----------
async function placeholderBlob() {
  const c = document.createElement("canvas");
  c.width = 320; c.height = 240;
  c.getContext("2d").fillStyle = "#333";
  c.getContext("2d").fillRect(0, 0, 320, 240);
  return new Promise((res) => c.toBlob(res, "image/jpeg"));
}

function previewBlob(blob, label) {
  const img = $("log-preview");
  if (img.src.startsWith("blob:")) URL.revokeObjectURL(img.src);
  img.onload = null;
  img.onerror = null;
  img.hidden = false;
  img.src = URL.createObjectURL(blob);
  if (label) $("log-cam-status").textContent = label;
}

function previewBackendFrame(framePath, label) {
  const name = String(framePath).split(/[/\\]/).pop();
  const img = $("log-preview");
  if (img.src.startsWith("blob:")) URL.revokeObjectURL(img.src);
  img.onload = null;
  img.onerror = () => { $("log-cam-status").textContent = "frame saved but preview failed"; };
  img.hidden = false;
  img.src = "/frames/" + encodeURIComponent(name);
  if (label) $("log-cam-status").textContent = label;
}

async function uploadFrame() {
  try { // 1. phone fetches the camera directly
    const r = await fetch(camUrl);
    if (r.ok) {
      const blob = await r.blob();
      const fd = new FormData();
      fd.append("file", blob, "frame.jpg");
      const f = await (await fetch("/frame", { method: "POST", body: fd })).json();
      if (f.path) { previewBlob(blob, "frame: phone-direct"); return f; }
    }
  } catch (e) { console.log("phone cam fetch failed", e); }
  try { // 2. backend grabs from the camera (laptop-side mDNS)
    const f = await (await fetch("/frame", { method: "POST" })).json();
    if (f.path) { previewBackendFrame(f.path, "frame: backend grab"); return f; }
    console.log("backend grab failed", f.error || f);
  } catch (e) { console.log("backend grab failed", e); }
  const fd = new FormData(); // 3. placeholder so the demo never stalls
  const ph = await placeholderBlob();
  fd.append("file", ph, "frame.jpg");
  previewBlob(ph, "frame: placeholder (camera unreachable)");
  return (await fetch("/frame", { method: "POST", body: fd })).json();
}

async function testCamera() {
  const img = $("log-preview");
  if (img.src.startsWith("blob:")) URL.revokeObjectURL(img.src);
  img.hidden = false;
  $("log-cam-status").textContent = "Testing phone → camera…";
  const url = camUrl + (camUrl.includes("?") ? "&" : "?") + "t=" + Date.now();
  img.onload = () => { $("log-cam-status").textContent = "OK — phone reached " + camUrl; };
  img.onerror = async () => {
    $("log-cam-status").textContent = "Phone can't reach camera, trying backend…";
    try {
      const f = await (await fetch("/frame", { method: "POST" })).json();
      if (f.path) {
        previewBackendFrame(f.path, "OK — backend grabbed frame (phone could not)");
        return;
      }
      $("log-cam-status").textContent = "Backend grab failed: " + (f.error || "unknown error");
    } catch (e) {
      $("log-cam-status").textContent = "Camera test failed: " + e.message;
    }
  };
  img.src = url;
}

// ---------- main pipeline (/probe + /diagnose unchanged) ----------
function manualProbe() {
  const t = $("manual").value.trim();
  if (!t) return null;
  try { return JSON.parse(t); } catch (e) { console.log("bad manual probe JSON", e); return null; }
}

function deliverReply(loopText) {
  // Voice-first: minimal label, full text spoken. Fallback: full text shown.
  if (mode === "learn") {
    learnSteps += 1;
    renderLearn();
    setState(State.SPEAKING, "Step " + learnSteps);
  } else {
    setState(State.SPEAKING, shortLabel(loopText));
  }
  if (!speechSupported()) {
    setStatus(loopText); // fallback: full text when nothing can speak
    setState(State.IDLE, loopText);
    return;
  }
  speak(loopText, () => { if (state === State.SPEAKING) setState(State.IDLE); });
}

async function pipeline(techText) {
  if (state === State.LISTENING) return;
  if (techText) history.push({ role: "user", content: techText });
  setState(State.THINKING);
  try {
    const frame = await uploadFrame();
    console.log("frame", frame);
    let probe = manualProbe();
    if (!probe) {
      probe = await (await fetch("/probe", { method: "POST" })).json();
    }
    console.log("probe", probe);
    const body = { frame_path: frame.path || "", probe_result: probe, conversation_history: history };
    const diag = await (await fetch("/diagnose", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    })).json();
    console.log("diagnose", diag);
    const loopText = diag.speak_to_tech || "(no instruction)";
    history.push({ role: "assistant", content: loopText });
    addTurn({
      tech: techText || "—", loop: loopText,
      found: probe.found || [], missing: probe.missing || [],
      state: diag.state, confidence: diag.confidence,
    });
    setLogStatus(diag);
    deliverReply(loopText);
  } catch (e) {
    console.log("pipeline failed", e);
    logLine("error: " + e.message);
    setState(State.ERROR);
  }
}

// ---------- push to talk (/transcribe unchanged) ----------
let recorder = null, chunks = [], recording = false, starting = false, recStream = null;

function micSupported() {
  return ("MediaRecorder" in window) && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
}

function showTypedFallback(msg) {
  $("typed-fallback").hidden = false;
  if (msg) $("typed-msg").textContent = msg;
}

async function startRec() {
  if (recording || starting) return;
  if (state === State.THINKING || state === State.SPEAKING) return;
  if (!micSupported()) { showTypedFallback("Microphone not available in this browser — type instead."); return; }
  stopSpeech();
  starting = true;
  try {
    recStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    console.log("mic denied", e);
    starting = false;
    showTypedFallback("Microphone blocked — type instead.");
    return;
  }
  chunks = [];
  recorder = new MediaRecorder(recStream);
  recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
  recorder.onstop = onRecStop;
  recorder.start();
  starting = false;
  recording = true;
  setState(State.LISTENING);
  $("talk").classList.add("rec");
}

function stopRec() {
  if (!recording || !recorder) return;
  recording = false;
  $("talk").classList.remove("rec");
  setState(State.THINKING);
  try { recorder.stop(); } catch (e) { console.log("recorder stop failed", e); }
  if (recStream) { recStream.getTracks().forEach((t) => t.stop()); recStream = null; }
}

async function onRecStop() {
  const data = chunks.slice();
  chunks = [];
  try {
    const fd = new FormData();
    fd.append("file", new Blob(data, { type: "audio/webm" }), "talk.webm");
    const t = await (await fetch("/transcribe", { method: "POST", body: fd })).json();
    console.log("transcribe", t);
    if (t.text) {
      pipeline(t.text).catch(() => setState(State.ERROR));
    } else {
      showTypedFallback("Transcription empty" + (t.error ? ": " + t.error : "") + " — type instead.");
      setState(State.ERROR, "Didn't catch that");
    }
  } catch (e) {
    console.log("transcribe failed", e);
    showTypedFallback("Transcription failed — type instead.");
    setState(State.ERROR);
  }
}

// ---------- wiring ----------
$("settings-btn").onclick = openSettings;
$("settings-close").onclick = closeSettings;
$("settings-modal").addEventListener("click", (e) => {
  if (e.target === $("settings-modal")) closeSettings();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("settings-modal").hidden) closeSettings();
});

$("save-host").onclick = async () => {
  const v = $("host").value.trim();
  if (!v) { $("settings-msg").textContent = "Enter a host first."; return; }
  try {
    const c = await (await fetch("/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ esp32_host: v }),
    })).json();
    if (c.esp32_host) camHost = c.esp32_host;
    if (c.cam_url) camUrl = c.cam_url;
    $("cam-line").textContent = "cam: " + camUrl;
    $("settings-msg").textContent = "Saved — no restart needed.";
  } catch (e) { $("settings-msg").textContent = "Save failed: " + e.message; }
};

$("check").onclick = () => {
  if (state === State.THINKING || state === State.LISTENING) return;
  stopSpeech();
  pipeline(null).catch(() => setState(State.ERROR));
};

$("mute").onclick = () => {
  muted = !muted;
  if (muted) stopSpeech();
  $("mute").textContent = muted ? "UNMUTE" : "MUTE";
  $("mute").classList.toggle("off", muted);
};

$("mode-fix").onclick = () => {
  mode = "fix";
  $("mode-fix").classList.add("active");
  $("mode-learn").classList.remove("active");
  renderLearn();
};
$("mode-learn").onclick = () => {
  mode = "learn";
  $("mode-learn").classList.add("active");
  $("mode-fix").classList.remove("active");
  renderLearn();
};

$("log-toggle").onclick = () => toggleLog();
$("log-test-cam").onclick = () => testCamera();
$("log-reset").onclick = () => {
  history = [];
  turns = [];
  learnSteps = 0;
  renderTurns();
  renderLearn();
  $("manual").value = "";
  setLogStatus({});
  stopSpeech();
  setState(State.IDLE);
};

const talk = $("talk");
talk.addEventListener("pointerdown", (e) => { e.preventDefault(); startRec(); });
talk.addEventListener("pointerup", (e) => { e.preventDefault(); stopRec(); });
talk.addEventListener("pointercancel", stopRec);
talk.addEventListener("pointerleave", () => { if (recording) stopRec(); });
talk.addEventListener("touchstart", (e) => { e.preventDefault(); startRec(); }, { passive: false });
talk.addEventListener("touchend", (e) => { e.preventDefault(); stopRec(); });
talk.addEventListener("contextmenu", (e) => e.preventDefault());

$("send-typed").onclick = () => {
  const v = $("typed").value.trim();
  if (!v) return;
  $("typed").value = "";
  stopSpeech();
  pipeline(v).catch(() => setState(State.ERROR));
};
$("typed").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("send-typed").click();
});

// ---------- boot ----------
loadConfig();
renderTurns();
renderLearn();
setLogStatus({});
setState(State.IDLE);
if (!micSupported()) showTypedFallback("Microphone not available in this browser — type instead.");
if (!speechSupported()) logLine("note: speechSynthesis unavailable — replies show as full text.");
