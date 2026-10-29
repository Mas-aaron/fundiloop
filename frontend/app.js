/* FundiLoop mobile UI: session control, camera preview, speech, checklist. */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
let mode = "GUIDED_ASSEMBLY";
let busy = false;
let lastSpeak = "";
let lastTranscript = "";   // last thing heard (fed to the live loop)
let lastInstruction = "";  // current step text (fed to the live loop)
let muted = false;
try { muted = localStorage.getItem("fundiloop-muted") === "1"; } catch (e) { /* private mode */ }
let cameraConfigured = false;

// Speech: pick the best English voice (defaults are often robotic), speak
// sentence-by-sentence so phones don't cut long instructions off.
let voiceChoice = "";
try { voiceChoice = localStorage.getItem("fundiloop-voice") || ""; } catch (e) { /* private mode */ }

function pickVoice() {
  const vs = speechSynthesis.getVoices().filter((v) => v.lang && v.lang.toLowerCase().startsWith("en"));
  if (!vs.length) return null;
  if (voiceChoice) {
    const named = vs.find((v) => v.name === voiceChoice);
    if (named) return named;
  }
  const prefs = [/google us english/i, /google uk english/i,
    /online/i, /premium/i, /enhanced/i, /natural/i, /neural/i,
    /samantha/i, /zira/i, /david/i, /aria/i, /jenny/i, /guy/i, /google/i];
  for (const p of prefs) {
    const hit = vs.find((v) => p.test(v.name));
    if (hit) return hit;
  }
  return vs.find((v) => v.default) || vs[0];
}

function populateVoices() {
  const sel = $("voice");
  if (!sel || !("speechSynthesis" in window)) return;
  const vs = speechSynthesis.getVoices().filter((v) => v.lang && v.lang.toLowerCase().startsWith("en"));
  sel.innerHTML = '<option value="">Auto (best available)</option>' +
    vs.map((v) => '<option value="' + esc(v.name) + '"' +
      (v.name === voiceChoice ? " selected" : "") + ">" +
      esc(v.name) + "</option>").join("");
}

// speakable:normalizer:start
function speakable(text) {
  // Spoken form of instruction text. Displayed text keeps its technical
  // spelling; this form stops phone voices from spelling ALL-CAPS tokens
  // ("VISUAL" -> "V I S U A L") and from reading punctuation as words.
  const WORDS = {
    "I2C": "I squared C", "GPIO": "G P I O", "VCC": "V C C",
    "GND": "ground", "TX": "T X", "RX": "R X", "USB": "U S B",
    "LED": "led", "OLED": "oh led", "RGB": "R G B",
    "UART": "U A R T", "PWM": "P W M", "ADC": "A D C",
    "SDA": "S D A", "SCL": "S C L", "PIR": "P I R",
    "OK": "okay", "HC": "H C", "SR": "S R", "DHT": "D H T",
    "BME": "B M E", "SSD": "S S D", "LCD": "L C D",
    "ESP": "E S P", "AC": "A C", "DC": "D C",
    "WIFI": "wye fye", "WiFi": "wye fye",
  };
  let s = String(text || "");
  s = s.replace(/```[a-zA-Z]*|```/g, " ");
  s = s.replace(/[`*#]/g, "");
  s = s.replace(/\b0[xX]([0-9A-Fa-f]+)\b/g,
    (m, h) => "hex " + h.toUpperCase().split("").join(" "));
  s = s.replace(/\b([A-Z]{2,})(\d+)\b/g,
    (m, letters, digits) => letters.split("").join(" ") + " " + digits);
  s = s.replace(/[()[\]{}]/g, " ");
  s = s.replace(/[_/\\|~-]+/g, " ");
  s = s.split(/\s+/).map((tok) => {
    if (!tok) return tok;
    if (WORDS[tok]) return WORDS[tok];
    const edge = tok.match(/^([^A-Za-z0-9]*)([A-Za-z0-9]+)([^A-Za-z0-9]*)$/);
    if (!edge) return tok;
    const pre = edge[1], core = edge[2], post = edge[3];
    if (WORDS[core]) return pre + WORDS[core] + post;
    const m = core.match(/^([A-Z]{2,})(\d+)$/);
    if (m) return pre + m[1].split("").join(" ") + " " + m[2] + post;
    if (/^[A-Z]{2,}$/.test(core))
      return pre + core.charAt(0) + core.slice(1).toLowerCase() + post;
    return tok;
  }).join(" ");
  return s.replace(/\s+/g, " ").trim();
}
// speakable:normalizer:end

function speak(text, onend) {
  const done = () => { if (onend) { try { onend(); } catch (e) { console.log("speak onend failed", e); } } };
  if (!text || muted) { done(); return; }
  try {
    if (!("speechSynthesis" in window)) { done(); return; }
    speechSynthesis.cancel();
    if (speechSynthesis.paused) speechSynthesis.resume();
    const voice = pickVoice();
    // Sentence chunks: some mobile browsers stall on single long utterances.
    // Decimals are protected so "0.55" is never split mid-number.
    const safe = speakable(text).replace(/(\d)\.(\d)/g, "$1<DOT>$2");
    const parts = (safe.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [safe])
      .map((p) => p.replace(/<DOT>/g, "."));
    parts.forEach((p, i) => {
      const u = new SpeechSynthesisUtterance(p.trim());
      u.lang = "en-US";
      u.rate = 0.98;
      u.pitch = 1.0;
      if (voice) u.voice = voice;
      if (i === parts.length - 1) u.onend = done;
      speechSynthesis.speak(u);
    });
  } catch (e) { console.log("speech failed", e); done(); }
}

function refreshCamera() {
  const url = "/api/camera/latest?t=" + Date.now();
  $("preview").src = url;
  if (cookMode) $("cook-preview").src = url;
}

// Header badge: LIVE (green) only when the real camera AND the real Meta
// models are connected; MOCK (red) otherwise.
function paintBadge(cfg) {
  const badge = $("mode-badge");
  const live = !!cfg && cfg.mock_meta === false && cfg.camera_online === true &&
    cfg.sam_mode === "real" && cfg.muse_mode === "real" && cfg.meta_api_configured === true;
  badge.textContent = live ? "LIVE" : "MOCK";
  badge.classList.toggle("live", live);
  badge.classList.toggle("mock", !live);
  badge.title = live
    ? "Real camera + real Meta models connected"
    : "Not fully live: " + [
      cfg.mock_meta ? "MOCK_META=true" : null,
      !cfg.camera_online ? "camera offline" : null,
      cfg.sam_mode !== "real" ? "SAM is mock" : null,
      cfg.muse_mode !== "real" ? "Muse is mock" : null,
      !cfg.meta_api_configured ? "META_API_KEY missing" : null,
    ].filter(Boolean).join("; ");
}

async function refreshBadge() {
  try {
    const r = await fetch("/api/status");
    if (!r.ok) return;
    const v = await r.json();
    if (v.config) {
      paintBadge(v.config);
      cameraConfigured = !!v.config.camera_capture_url;
      if (v.config.camera_error) $("cam-src").textContent = "camera: " + v.config.camera_error;
    }
  } catch (e) { /* badge keeps its last state when offline */ }
}

function showError(msg) {
  $("instruction").textContent = "Error: " + msg;
  $("instruction").classList.add("fault");
  $("instruction").classList.remove("done");
}

async function startSession() {
  const r = await fetch("/api/session/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  const view = await r.json();
  if (!r.ok) {
    showError((view && view.detail) || "session start failed");
    return;
  }
  render(view);
  // No auto-speak here: speech fires on check results, replay, and talk.
}

const CHECK_STAGES = { ack: "Checking…", sense: "Sensing…", reason: "Reasoning…", verify: "Verifying…" };
let checkAbort = null; // AbortController for the in-flight turn (CANCEL)
let pendingCheck = ""; // voice trigger heard mid-turn, run when free
let pendingConfirmSid = ""; // learning session waiting on CONFIRM

function fmtTimings(t) {
  if (!t) return "";
  const s = (ms) => ((ms || 0) / 1000).toFixed(1) + "s";
  return "turn " + s(t.total_ms) + " (sense " + s(t.sense_ms) +
    " · sam " + s(t.sam_ms) + " · probe " + s(t.probe_ms) +
    " · reason " + s(t.reason_ms) + ")";
}

function paintConfirm(v) {
  const pend = v && v.confirm_pending;
  $("confirm-bar").hidden = !pend;
  $("cook-confirm").hidden = !pend;
  if (pend) {
    pendingConfirmSid = v.id || learnSid || "";
    $("confirm-q").textContent = pend.question || "Confirm this step?";
  } else {
    pendingConfirmSid = "";
  }
}

async function sendConfirm(confirmed) {
  if (!pendingConfirmSid || busy) return;
  busy = true;
  try {
    const r = await fetch("/api/session/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: pendingConfirmSid, confirmed: confirmed }),
    });
    const v = await r.json();
    if (!r.ok) {
      showError((v && v.detail) || "confirm failed");
      return;
    }
    if (cookMode && v.learning) {
      renderCook(v);
      cookSpeak(v.speak);
    } else {
      renderLearning(v);
      speak(v.speak);
    }
  } catch (e) {
    showError(e.message);
  } finally {
    busy = false;
  }
}
$("confirm-yes").onclick = () => sendConfirm(true);
$("confirm-no").onclick = () => sendConfirm(false);
$("cook-confirm").onclick = () => sendConfirm(true);
$("cancel").onclick = () => { if (checkAbort) checkAbort.abort(); };
$("rescan").onclick = async () => {
  if (busy) return;
  $("instruction").textContent = "Re-scanning equipment…";
  try {
    await fetch("/api/session/rescan", { method: "POST" });
  } catch (e) { showError(e.message); return; }
  checkNow("");
};

function renderCheckView(view) {
  if (cookMode && view.learning) {
    renderCook(view);
    cookSpeak(view.speak);
  } else {
    if (view.learning) renderLearning(view); else render(view);
    speak(view.speak);
  }
}

// Streaming turn: instant ack, then one status line per stage, then the
// same verified view the blocking endpoint returns. Returns true when the
// stream finished (done or error), false when the caller should fall back.
async function checkNowStream(payload, onStage) {
  const r = await fetch("/api/session/check-now/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: checkAbort ? checkAbort.signal : undefined,
  });
  if (!r.ok || !r.body || !r.body.getReader) return false;
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (value) buf += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      let name = "", data = "";
      for (const line of frame.split("\n")) {
        if (line.indexOf("event:") === 0) name = line.slice(6).trim();
        else if (line.indexOf("data:") === 0) data = line.slice(5).trim();
      }
      if (!name) continue;
      if (name === "done") {
        renderCheckView(JSON.parse(data));
        return true;
      }
      if (name === "error") {
        const detail = (JSON.parse(data) || {}).detail || "check-now failed";
        showError(detail);
        refreshBadge();
        return true;
      }
      onStage(name);
    }
    if (done) return false; // stream died mid-turn: caller falls back
  }
}

async function checkNow(transcript) {
  if (busy) return;
  busy = true;
  checkAbort = new AbortController();
  $("check").disabled = true;
  $("cancel").hidden = false;
  const t0 = Date.now();
  let stage = "Working…";
  const paint = () => {
    $("instruction").textContent = stage + " " + Math.round((Date.now() - t0) / 1000) + "s";
  };
  $("instruction").textContent = "Checking…";
  speak("Checking."); // instant ack, cut off when the verdict speech starts
  const tick = setInterval(paint, 1000);
  try {
    const fault = $("fault").value || null;
    const payload = {
      code_snippet: $("code").value || "",
      simulate_fault: fault,
      transcript: transcript || "",
    };
    try {
      const onStage = (name) => {
        if (CHECK_STAGES[name]) { stage = CHECK_STAGES[name]; paint(); }
      };
      if (await checkNowStream(payload, onStage)) return;
    } catch (e) {
      if (checkAbort && checkAbort.signal.aborted) {
        pendingCheck = "";
        $("instruction").textContent = "Cancelled.";
        try { speechSynthesis.cancel(); } catch (x) { /* no speech */ }
        return;
      }
      console.log("stream turn failed, falling back", e);
    }
    const r = await fetch("/api/session/check-now", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: checkAbort ? checkAbort.signal : undefined,
    });
    const view = await r.json();
    if (!r.ok) {
      // Real camera/Meta error: show it, do not render a faked turn.
      showError((view && view.detail) || "check-now failed");
      refreshBadge();
      return;
    }
    renderCheckView(view);
  } catch (e) {
    if (checkAbort && checkAbort.signal.aborted) {
      pendingCheck = "";
      $("instruction").textContent = "Cancelled.";
    } else {
      showError(e.message);
    }
  } finally {
    clearInterval(tick);
    busy = false;
    checkAbort = null;
    $("check").disabled = false;
    $("cancel").hidden = true;
    refreshCamera();
    const q = pendingCheck;
    pendingCheck = "";
    if (q) checkNow(q); // voice trigger heard mid-turn: run it now
  }
}

function drawEvidence(frameFile, detections, mock) {
  const sec = $("evidence-sec"), img = $("evidence-img"), svg = $("evidence-svg");
  if (!frameFile) {
    sec.hidden = true;
    img.removeAttribute("src");
    svg.innerHTML = "";
    return;
  }
  const name = String(frameFile).split(/[\\/]/).pop();
  const dets = detections || [];
  sec.hidden = false;
  img.onload = () => {
    const w = img.naturalWidth || 640, h = img.naturalHeight || 480;
    svg.setAttribute("viewBox", "0 0 " + w + " " + h);
    svg.innerHTML = dets.map((d) => {
      const b = d.box || [0, 0, 0, 0];
      const rw = Math.max(1, b[2] - b[0]), rh = Math.max(1, b[3] - b[1]);
      return '<rect x="' + b[0] + '" y="' + b[1] + '" width="' + rw + '" height="' + rh + '"/>' +
        '<text x="' + b[0] + '" y="' + Math.max(12, b[1] - 4) + '">' + esc(d.label || "") + "</text>";
    }).join("");
    $("evidence-cap").textContent = "analyzed frame " + name + "  ·  " +
      dets.length + " region(s)" + (mock ? " (mock)" : "");
  };
  img.onerror = () => {
    svg.innerHTML = "";
    $("evidence-cap").textContent = "frame unavailable: " + name;
  };
  img.src = "/api/frames/" + encodeURIComponent(name);
}

function render(v) {
  paintConfirm(v);
  const box = $("instruction");
  box.textContent = v.instruction || "(no instruction)";
  lastInstruction = v.instruction || "";
  box.classList.toggle("fault", v.state === "FAULT_FOUND");
  box.classList.toggle("done", v.state === "COMPLETE" || v.state === "VERIFIED_FIXED");
  if (v.speak) lastSpeak = v.speak;
  if (v.camera_source) {
    const src = v.camera_source;
    $("cam-src").textContent = src + (v.frame_file ? "  ·  " + v.frame_file.split(/[\\/]/).pop() : "");
    const ov = $("cam-overlay");
    if (src.indexOf("snap (override)") === 0) {
      ov.hidden = false;
      ov.textContent = "Phone snap pinned — press CHECK NOW to analyze it.";
    } else {
      ov.hidden = true;
    }
  }
  $("state-line").textContent =
    "state: " + (v.state || "—") + "  ·  mode: " + (v.mode || "—") +
    (v.step ? "  ·  step " + (v.step_index + 1) + ": " + v.step.step_id : "") +
    (v.attempts_total ? "  ·  attempts: " + v.attempts_total : "") +
    (v.camera_source ? "  ·  " + v.camera_source : "") +
    (v.frame_settled === false ? "  ·  frame settling…" : "") +
    (v.timings ? "  ·  " + fmtTimings(v.timings) : "");
  if (v.cam_src) $("cam-src").textContent = v.cam_src;
  if (v.handoff) {
    pendingHandoff = v.handoff;
    $("handoff-learn").hidden = false;
  } else {
    pendingHandoff = null;
    $("handoff-learn").hidden = true;
  }

  const p = v.probe || {};
  const ver = v.verification || {};
  let html =
    "I2C devices: [" + esc((p.i2c_devices || []).join(", ") || "none") + "]\n" +
    "sensor: " + (p.temperature_c == null ? "—" :
      esc(p.temperature_c + " C, " + p.humidity_pct + " %, " + p.pressure_hpa + " hPa")) + "\n" +
    "OLED: " + (p.oled_ok == null ? "—" : p.oled_ok ? "lit" : "dark") +
    "   LED: " + (p.led_ok == null ? "—" : p.led_ok ? "blinks" : "dead") + "\n" +
    (ver.test ? "last test: " + esc(ver.test) + " → " + (ver.passed ? "PASS" : "FAIL") +
      "\nmeasured: " + esc(ver.measured || "") +
      (v.decision && v.decision.verification_test
        ? "\nmodel proposed: " + esc(v.decision.verification_test) +
          "  ·  sensor decided: " + (ver.passed ? "PASS" : "FAIL")
        : "") : "no test run yet") +
    (p.mock ? "\n(mock data)" : "");
  if (v.sam) {
    const labels = (v.sam.detections || []).map((d) =>
      d.label + " " + Math.round((d.confidence || 0) * 100) + "%").join(", ");
    html += "\nsees: " + (labels || "nothing usable") + (v.sam.mock ? " (mock)" : "");
    if (v.sam.error) html += "\nvision error: " + v.sam.error;
    else if (v.sam.notes && (v.sam.wires_ok === false || (v.sam.detections || []).length === 0 ||
        v.sam.notes.indexOf("NOT visible") >= 0 || v.sam.notes.indexOf("dark") >= 0)) {
      html += "\nvision note: " + v.sam.notes;
    }
  }
  if (v.terminal) {
    const t = v.terminal;
    html += "\nend criteria: READ_SENSOR " +
      (t.READ_SENSOR && t.READ_SENSOR.passed ? "PASS" : "FAIL") + ", CHECK_OLED " +
      (t.CHECK_OLED && t.CHECK_OLED.passed ? "PASS" : "FAIL");
  }
  if (v.ranking && v.ranking.length) {
    html += "\nranked faults:\n" + v.ranking.map((r, i) =>
      " " + (i + 1) + ". " + esc(r.fault_id) + " (" + esc(String(r.score)) + ")" +
      (r.evidence && r.evidence.length ? " — " + esc(r.evidence[0]) : "")
    ).join("\n");
  }
  $("verify").innerHTML = html;

  const ol = $("checklist");
  ol.innerHTML = (v.checklist || []).map((c) =>
    '<li><span class="' + esc(c.status) + '">' +
    (c.status === "pass" ? "PASS" : c.status === "fail" ? "FAIL" : "····") +
    "</span> <b>" + esc(c.step_id) + "</b>" +
    (c.measured ? "<br><span class='fine'>" + esc(c.measured) + "</span>" : "") + "</li>"
  ).join("");

  if (v.passport) {
    $("passport-sec").hidden = false;
    const pp = v.passport;
    const fmt = (m) => "I2C [" + ((m.i2c_devices || []).join(", ") || "none") + "]" +
      (m.temperature_c == null ? "" : ", T=" + m.temperature_c + "C");
    $("passport").innerHTML =
      "mode: " + esc(pp.mode) + "\n" +
      "steps: " + pp.steps_completed + "/" + pp.steps_total + "\n" +
      "faults found: " + esc((pp.faults_found || []).join(", ") || "none") + "\n" +
      "before: " + esc(fmt(pp.measurements_before || {})) + "\n" +
      "after: " + esc(fmt(pp.measurements_after || {})) + "\n" +
      "started: " + esc(pp.started_at || "") + "\n" +
      "completed: " + esc(pp.completed_at || "");
  } else {
    $("passport-sec").hidden = true;
  }
  drawEvidence(v.frame_file, (v.sam && v.sam.detections) || [], v.sam && v.sam.mock);
}

// Mode toggle restarts the session in that mode.
$("mode-guided").onclick = () => {
  mode = "GUIDED_ASSEMBLY";
  $("mode-guided").classList.add("active");
  $("mode-diag").classList.remove("active");
  $("mode-learn").classList.remove("active");
  $("learn-sec").hidden = true;
  $("code-sec").hidden = true;
  startSession().catch((e) => { showError(e.message); });
};
$("mode-diag").onclick = () => {
  mode = "DIAGNOSTIC";
  $("mode-diag").classList.add("active");
  $("mode-guided").classList.remove("active");
  $("mode-learn").classList.remove("active");
  $("learn-sec").hidden = true;
  $("code-sec").hidden = true;
  startSession().catch((e) => { showError(e.message); });
};
$("mode-learn").onclick = () => {
  mode = "LEARNING";
  $("mode-learn").classList.add("active");
  $("mode-guided").classList.remove("active");
  $("mode-diag").classList.remove("active");
  $("learn-sec").hidden = false;
  $("code-sec").hidden = true;
  $("instruction").textContent = "Learning mode: SHOW EQUIPMENT, name a project (type or PUSH TO TALK), then SUGGEST PROJECTS.";
};
$("check").onclick = () => checkNow("");

let learnParts = [], learnConns = [], learnUnc = [], learnProjects = [], learnSid = "";
let codeFetchedSid = ""; // session whose .ino is already on screen (cook + learn share)
let learnBoard = "esp32_devkit";

function renderLearnParts(parts) {
  $("learn-parts").innerHTML = (parts || []).map((p) => {
    const cls = p.name === "unknown" ? "fail" : (p.sam_confirmed ? "pass" : "pending");
    const tag = p.name === "unknown" ? "UNKNOWN" : (p.sam_confirmed ? "SEEN" : "····");
    return '<li><span class="' + cls + '">' + tag + '</span> <b>' + esc(p.name) + "</b>" +
      (p.state ? '<br><span class="fine">' + esc(p.state) + "</span>" : "") + "</li>";
  }).join("");
}

function renderLearnCards(projects) {
  learnProjects = projects || [];
  $("learn-cards").innerHTML = learnProjects.map((p, i) => {
    const alts = (p.alternatives || []).map((a, j) =>
      '<li data-p="' + i + '" data-a="' + j + '"><b>ALT: ' + esc(a.title || "alternative") + "</b><br><span class='fine'>" + esc(a.summary || "") + "</span> — Start</li>"
    ).join("");
    return '<li data-p="' + i + '"><b>' + (i + 1) + ". " + esc(p.title || "project") + "</b><br><span class='fine'>" + esc(p.summary || "") + "</span>" +
      ((p.missing_parts || []).length ? "<br><span class='fine'>missing: " + esc(p.missing_parts.join(", ")) + "</span>" : "") +
      " — Start</li>" + alts;
  }).join("");
}

function renderLearning(v) {
  paintConfirm(v);
  if (v.id) learnSid = v.id;
  const box = $("instruction");
  box.textContent = v.instruction || "(no instruction)";
  lastInstruction = v.instruction || "";
  box.classList.toggle("fault", v.state === "VERIFYING");
  box.classList.toggle("done", v.state === "DONE");
  if (v.speak) lastSpeak = v.speak;
  $("state-line").textContent =
    "state: " + (v.state || "—") + "  ·  mode: LEARNING" +
    (v.project_title ? "  ·  project: " + v.project_title : "") +
    (v.board ? "  ·  board: " + v.board : "") +
    "  ·  step " + (v.step_index + 1) + "/" + v.steps_total +
    (v.timings ? "  ·  " + fmtTimings(v.timings) : "");
  const ver = v.verification || {};
  let html = ver.test
    ? "test: " + esc(ver.test) + " → " + (ver.passed ? "PASS" : "FAIL") +
      "\nmeasured: " + esc(ver.measured || "")
    : "no test run yet — press CHECK NOW";
  const p = v.probe || {};
  if (p.i2c_devices) html += "\nI2C: [" + esc((p.i2c_devices || []).join(", ") || "none") + "]";
  $("verify").innerHTML = html;
  $("code-sec").hidden = v.state !== "DONE";
  $("cook-resume").hidden = !learnSid;
  drawEvidence(); // learning turns are probe-only: no analyzed frame
  if (v.state !== "DONE") {
    if ($("code-out").textContent) $("code-out").textContent = "";
  } else if (v.id && codeFetchedSid !== v.id) {
    // DONE: the project code arrives on its own; SEND CODE stays as retry.
    setTimeout(() => { if (!cookMode && learnSid && codeFetchedSid !== learnSid) fetchCode(true); }, 150);
  }
}

function enterLearning(v) {
  mode = "LEARNING";
  $("mode-learn").classList.add("active");
  $("mode-guided").classList.remove("active");
  $("mode-diag").classList.remove("active");
  $("learn-sec").hidden = false;
  renderLearning(v);
}

async function runPartsOnFrame(framePath) {
  const t0 = Date.now();
  $("instruction").textContent = "Analyzing…";
  speak("Analyzing."); // ack at once; the verdict speech replaces it
  const tick = setInterval(() => {
    $("instruction").textContent = "Analyzing… " + Math.round((Date.now() - t0) / 1000) + "s";
  }, 1000);
  try {
    const pr = await fetch("/discover/parts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ frame_path: framePath }),
    });
    const v = await pr.json();
    if (!pr.ok) { showError("discovery failed"); return; }
    learnConns = v.connections_observed || [];
    learnUnc = v.uncertainties || [];
    learnParts = [...new Set((v.visible_parts || [])
      .filter((p) => p.name && p.name !== "unknown").map((p) => p.name))];
    renderLearnParts(v.visible_parts || []);
    drawEvidence(framePath, [], false);
    prefetchPlans(); // plan in the background so SUGGEST answers instantly
    const names = learnParts.length ? learnParts.join(", ") : "nothing I can name";
    $("instruction").textContent = "I see: " + names +
      (learnUnc.length ? ". Uncertain: " + learnUnc.join("; ") : "");
    lastSpeak = learnParts.length
      ? "I see " + learnParts.join(", ").replace(/_/g, " ")
      : "I could not identify any parts.";
    speak(lastSpeak);
  } finally {
    clearInterval(tick);
  }
}

async function showEquipment() {
  if (busy) return;
  busy = true;
  $("instruction").textContent = "Capturing the bench…";
  try {
    const capR = await fetch("/discover/capture", { method: "POST" });
    const cap = await capR.json();
    if (!capR.ok) { showError((cap && cap.detail) || "capture failed"); return; }
    await runPartsOnFrame(cap.frame_path);
  } catch (e) { showError(e.message); } finally { busy = false; }
}

$("learn-upload").onclick = () => { if (!busy) $("learn-file").click(); };
$("learn-file").onchange = async () => {
  const f = $("learn-file").files[0];
  $("learn-file").value = "";
  if (!f || busy) return;
  busy = true;
  $("instruction").textContent = "Uploading photo…";
  try {
    const fd = new FormData();
    fd.append("file", f, f.name || "equipment.jpg");
    const upR = await fetch("/discover/upload", { method: "POST", body: fd });
    const up = await upR.json();
    if (!upR.ok) { showError((up && up.detail) || "upload failed"); return; }
    await runPartsOnFrame(up.frame_path);
  } catch (e) { showError(e.message); } finally { busy = false; }
};

// plancache:block:start
let planCache = { key: "", projects: null, hint: "", promise: null };

function planCacheKey(parts, want, board, conns, uncs) {
  return JSON.stringify({
    parts: [...parts].sort(),
    want: want || "",
    board: board || "esp32_devkit",
    conns: conns || [],
    uncs: uncs || [],
  });
}

function planPayload(parts, want, board, conns, uncs) {
  return {
    visible_parts: parts,
    user_project: want || null,
    connections_observed: conns || [],
    uncertainties: uncs || [],
    board: board || "esp32_devkit",
  };
}

async function fetchPlansIntoCache(key, payload) {
  // One flight per key, shared by prefetch and suggest; the result sticks
  // only if the key is still current when it lands.
  const r = await fetch("/discover/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const v = await r.json();
  if (!r.ok) throw new Error((v && v.detail) || "planning failed");
  if (planCache.key === key) {
    planCache.projects = v.projects || [];
    planCache.hint = v.hint || "";
  }
  return v;
}

function prefetchPlans() {
  // Warm the cache in the background once equipment is known. Never sets
  // busy and never surfaces errors: a failure just leaves SUGGEST to
  // fetch on demand.
  if (!learnParts.length) return;
  const board = $("learn-board").value || "esp32_devkit";
  const key = planCacheKey(learnParts, "", board, learnConns, learnUnc);
  if (planCache.key === key && (planCache.projects || planCache.promise)) return;
  planCache = { key: key, projects: null, hint: "", promise: null };
  planCache.promise = fetchPlansIntoCache(
    key, planPayload(learnParts, "", board, learnConns, learnUnc))
    .catch(() => null)
    .finally(() => { if (planCache.key === key) planCache.promise = null; });
}
// plancache:block:end

async function suggestProjects() {
  if (busy) return;
  const want = ($("learn-project").value || "").trim();
  if (!learnParts.length && !want) {
    $("talk-hint").textContent = "Show equipment or name a project first.";
    return;
  }
  busy = true;
  learnBoard = $("learn-board").value || "esp32_devkit";
  const key = planCacheKey(learnParts, want, learnBoard, learnConns, learnUnc);
  const renderPlans = (projects, hint) => {
    renderLearnCards(projects || []);
    if (!learnProjects.length) {
      $("instruction").textContent = hint || "No buildable project found.";
      speak("I could not plan a project with these parts.");
      return;
    }
    const titles = learnProjects.map((p) => p.title).join("; ");
    $("instruction").textContent = "Tap a project to start: " + titles;
    speak("I found " + learnProjects.length + " projects: " + titles + ". Tap one to start.");
  };
  try {
    if (planCache.key === key && planCache.projects) {
      renderPlans(planCache.projects, planCache.hint); // prefetched: instant
      return;
    }
    $("instruction").textContent = "Planning projects for " + learnBoard + "…";
    let v = null;
    if (planCache.key === key && planCache.promise) {
      v = await planCache.promise; // prefetch in flight: ride it
    }
    if (!v) {
      planCache = { key: key, projects: null, hint: "", promise: null };
      v = await fetchPlansIntoCache(
        key, planPayload(learnParts, want, learnBoard, learnConns, learnUnc));
    }
    renderPlans(v.projects, v.hint);
  } catch (e) { showError(e.message); } finally { busy = false; }
}

async function startLearning(project) {
  if (busy) return;
  busy = true;
  learnBoard = $("learn-board").value || "esp32_devkit";
  $("instruction").textContent = "Starting " + (project.title || "project") + "…";
  try {
    const r = await fetch("/diagnose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: "learning",
        project_id: project.id,
        project_title: project.title,
        plan: project.steps || [],
        session_id: project.session_id || "",
        visible_parts: learnParts,
        board: learnBoard,
      }),
    });
    const v = await r.json();
    if (!r.ok) { showError((v && v.detail) || "could not start project"); return; }
    enterCookMode(v);
  } catch (e) { showError(e.message); } finally { busy = false; }
}

$("learn-equip").onclick = () => showEquipment();
$("learn-suggest").onclick = () => suggestProjects();
$("learn-board").onchange = () => prefetchPlans();
$("learn-cards").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-p]");
  if (!li) return;
  const proj = learnProjects[+li.dataset.p];
  if (!proj) return;
  if (li.dataset.a !== undefined && li.dataset.a !== "") {
    const alt = (proj.alternatives || [])[+li.dataset.a];
    if (!alt) return;
    startLearning({ id: proj.id + "-alt-" + li.dataset.a, title: alt.title,
      steps: alt.steps || [], session_id: proj.session_id });
  } else {
    startLearning(proj);
  }
});
async function fetchCode(auto) {
  if (busy || !learnSid) return;
  busy = true;
  try {
    const r = await fetch("/project/" + encodeURIComponent(learnSid) + "/code", { method: "POST" });
    const v = await r.json();
    if (!r.ok) {
      const msg = (v && v.detail) || "code not ready";
      if (auto) {
        $("talk-hint").textContent = msg + " — press SEND CODE to retry.";
        speak("Code is not ready yet. Press Send Code to retry.");
      } else showError(msg);
      return;
    }
    $("code-out").textContent = v.code || "";
    codeFetchedSid = learnSid;
    if (!auto) speak("Code ready.");
  } catch (e) {
    if (auto) {
      $("talk-hint").textContent = e.message + " — press SEND CODE to retry.";
      speak("Code fetch failed. Press Send Code to retry.");
    } else showError(e.message);
  } finally { busy = false; }
}
$("send-code").onclick = () => fetchCode(false);

async function copyCode(preId, heardId) {
  const text = $(preId).textContent || "";
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    if (heardId) $(heardId).textContent = "Code copied.";
  } catch (e) {
    if (heardId) $(heardId).textContent = "Copy failed: " + e.message;
  }
}
$("copy-code").onclick = () => copyCode("code-out", "talk-hint");
$("cook-copy").onclick = () => copyCode("cook-code-out", "cook-heard");

// Hands-free voice loop: tap PUSH TO TALK to record (MediaRecorder), tap
// again to stop, upload to /api/voice/transcribe, show the transcript, and
// auto-run one loop turn when it contains done / fixed / moved it.
const TRIGGER_WORDS = ["done", "fixed", "moved it"];
let pttRec = null, pttChunks = [], pttRecording = false;

async function transcribeUpload(blob) {
  const fd = new FormData();
  fd.append("file", blob, "talk.webm");
  return (await fetch("/api/voice/transcribe", { method: "POST", body: fd })).json();
}

function pttToggle(btn) {
  // Recording stays available mid-turn (transcribe is independent of the
  // sense loop); a trigger heard while busy queues instead of doubling.
  if (pttRecording) { try { pttRec.stop(); } catch (e) { /* onstop resets */ } return; }
  if (!("MediaRecorder" in window) || !(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
    $("talk-hint").textContent = "Audio capture unavailable — use CHECK NOW.";
    return;
  }
  navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
    pttChunks = [];
    pttRec = new MediaRecorder(stream);
    pttRec.ondataavailable = (e) => { if (e.data && e.data.size) pttChunks.push(e.data); };
    pttRec.onstop = async () => {
      pttRecording = false;
      btn.classList.remove("rec");
      btn.textContent = "PUSH TO TALK";
      stream.getTracks().forEach((t) => t.stop());
      $("talk-hint").textContent = "Transcribing…";
      try {
        const t = await transcribeUpload(new Blob(pttChunks, { type: "audio/webm" }));
        const text = (t.text || "").trim();
        if (text) lastTranscript = text;
        const tag = t.note ? " (" + t.note + ")" : "";
        if (!text) {
          $("talk-hint").textContent = "Transcription empty" + (t.error ? ": " + t.error : "") + " — use CHECK NOW.";
          return;
        }
        if (mode === "LEARNING") {
          $("learn-project").value = text;
          $("talk-hint").textContent = 'heard: "' + text + '"' + tag + " → suggesting…";
          suggestProjects();
          return;
        }
        const hit = TRIGGER_WORDS.some((w) => text.toLowerCase().includes(w));
        if (hit && busy) {
          pendingCheck = text;
          $("talk-hint").textContent = 'heard: "' + text + '"' + tag + " → queued, will check when ready.";
        } else {
          $("talk-hint").textContent = 'heard: "' + text + '"' + tag + (hit ? " → checking…" : " — say done / fixed / moved it, or CHECK NOW.");
          if (hit) checkNow(text);
        }
      } catch (e) { $("talk-hint").textContent = "Transcription failed — use CHECK NOW."; }
    };
    pttRec.start();
    pttRecording = true;
    btn.classList.add("rec");
    btn.textContent = "LISTENING — TAP TO STOP";
  }).catch(() => { $("talk-hint").textContent = "Microphone blocked — use CHECK NOW."; });
}
$("talk").onclick = () => pttToggle($("talk"));

// Live guide: the technician talks and shows the bench, the agent coaches.
// Meta has no single audio+video streaming model, so a live turn is a short
// camera clip (video + the technician's voice in its audio track) sent over
// one WebSocket: SAM 3.1 tracks the parts, Muse Spark reads the clip, and the
// next instruction is spoken. The probe stays the only thing that confirms.
const LIVE_SEGMENT_MS = 4000;
let liveWs = null, liveStream = null, liveOn = false, liveTurnResolve = null;

function liveHint(text) { $("live-hint").textContent = text; }

function newLiveSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(proto + "//" + location.host + "/api/live/session");
}

function recordSegment(stream, ms) {
  return new Promise((resolve) => {
    let rec;
    try { rec = new MediaRecorder(stream); } catch (e) { resolve(null); return; }
    const chunks = [];
    rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = () => resolve(chunks.length
      ? new Blob(chunks, { type: rec.mimeType || "video/webm" }) : null);
    rec.start();
    setTimeout(() => { try { rec.stop(); } catch (e) { /* already stopped */ } }, ms);
  });
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result).split(",")[1] || "");
    fr.onerror = () => reject(fr.error);
    fr.readAsDataURL(blob);
  });
}

function renderLive(res) {
  const box = $("instruction");
  box.textContent = res.speak || "(no guidance)";
  box.classList.remove("fault", "done");
  lastInstruction = res.speak || lastInstruction;
  if (res.speak) lastSpeak = res.speak;
  const sam = res.sam || {};
  const p = res.probe || {};
  liveHint("live " + (res.mode || "") +
    "  ·  SAM tracks: " + (sam.track_count || 0) + (sam.media ? " over " + (sam.frame_count || 0) + " frames" : "") +
    (sam.mock || res.mock ? " (mock)" : "") +
    "  ·  I2C: [" + ((p.i2c_devices || []).join(", ") || "none") + "]");
}

function handleLiveMessage(ev) {
  let msg;
  try { msg = JSON.parse(ev.data); } catch (e) { return; }
  if (msg.type === "ready") {
    liveHint("live ready — mode " + (msg.mode || "") + (msg.mock ? " (mock vision)" : ""));
    return;
  }
  if (liveTurnResolve && (msg.type === "guidance" || msg.type === "error")) {
    const resolve = liveTurnResolve;
    liveTurnResolve = null;
    resolve(msg);
  }
}

async function liveLoop() {
  while (liveOn) {
    liveHint("Recording a short clip — hold the board up and say what you are doing…");
    const blob = await recordSegment(liveStream, LIVE_SEGMENT_MS);
    if (!liveOn) break;
    if (!blob || !blob.size) continue;
    let clip;
    try { clip = await blobToBase64(blob); } catch (e) { continue; }
    if (!liveWs || liveWs.readyState !== WebSocket.OPEN) break;
    liveHint("Thinking… (" + Math.round(blob.size / 1024) + " KB clip sent)");
    const answer = new Promise((resolve) => { liveTurnResolve = resolve; });
    liveWs.send(JSON.stringify({
      type: "turn", clip, mime: blob.type.split(";")[0] || "video/webm",
      transcript: lastTranscript, step_text: lastInstruction,
    }));
    const res = await answer;
    if (!liveOn) break;
    if (res.type === "error") { liveHint("Live error: " + (res.detail || "unknown")); continue; }
    renderLive(res);
    speak(res.speak);
  }
}

async function startLive() {
  if (liveOn) return;
  if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) || !("MediaRecorder" in window)) {
    liveHint("Live guide needs a camera and a microphone.");
    return;
  }
  $("live").textContent = "STARTING…";
  try {
    liveStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment" }, audio: true });
  } catch (e) {
    $("live").textContent = "START LIVE GUIDE";
    liveHint("Camera and microphone are blocked — live guide needs both.");
    return;
  }
  liveWs = newLiveSocket();
  liveWs.onmessage = handleLiveMessage;
  liveWs.onerror = () => liveHint("Live connection error.");
  liveWs.onclose = () => { if (liveOn) stopLive("Live connection closed."); };
  liveWs.onopen = () => {
    liveOn = true;
    $("live").textContent = "STOP LIVE GUIDE";
    $("live").classList.add("on");
    liveWs.send(JSON.stringify({ type: "hello", mode, step_text: lastInstruction }));
    liveLoop();
  };
}

function stopLive(note) {
  liveOn = false;
  liveTurnResolve = null;
  if (liveWs) { try { liveWs.close(); } catch (e) { /* already closed */ } liveWs = null; }
  if (liveStream) { liveStream.getTracks().forEach((t) => t.stop()); liveStream = null; }
  $("live").textContent = "START LIVE GUIDE";
  $("live").classList.remove("on");
  liveHint(note || "Live guide stopped.");
}

$("live").onclick = () => { if (liveOn) stopLive(); else startLive(); };

// Cook Mode: NYT-style guided step runner for learning projects.
// One step at a time on a full-screen card (step X of Y, progress bar,
// parts chips, live camera with SAM boxes), driven by voice: TALK to ask
// or command (check / repeat / look / exit), everything spoken back.
// Verification still runs through checkNow: the sensor alone advances.
let cookMode = false, cookWs = null, cookView = null;
let cookRec = null, cookChunks = [], cookRecording = false, cookTurnResolve = null;
const COOK_LISTEN_MS = 6000;

function cookSpeak(text) {
  speak(text, () => {
    if (cookMode && !muted && !busy && $("cook-handsfree").checked) cookListen();
  });
}

function renderCook(v) {
  paintConfirm(v);
  cookView = v;
  if (v.id) learnSid = v.id;
  lastInstruction = v.instruction || "";
  if (v.speak) lastSpeak = v.speak;
  $("cook-project").textContent = (v.project_title || "Project") + (v.board ? " (" + v.board + ")" : "");
  const idx = v.step_index || 0, total = v.steps_total || 1;
  const done = v.state === "DONE";
  $("cook-step").textContent = done
    ? "DONE — all " + total + " steps verified"
    : "STEP " + Math.min(idx + 1, total) + " OF " + total;
  $("cook-bar").style.width = Math.round(100 * (done ? total : idx) / total) + "%";
  const box = $("cook-instruction");
  box.textContent = v.instruction || "(no instruction)";
  box.classList.toggle("fault", v.state === "VERIFYING");
  box.classList.toggle("done", done);
  const deps = (v.step && v.step.depends_on_parts) || [];
  $("cook-parts").innerHTML = deps.map((d) => "<li>" + esc(d) + "</li>").join("");
  const ver = v.verification || {};
  let html = ver.test
    ? "test: " + esc(ver.test) + " → " + (ver.passed ? "PASS" : "FAIL") +
      "\nmeasured: " + esc(ver.measured || "")
    : "no test run yet — press CHECK or say “check”";
  const p = v.probe || {};
  if (p.i2c_devices) html += "\nI2C: [" + esc((p.i2c_devices || []).join(", ") || "none") + "]";
  if (v.timings) html += "\n" + esc(fmtTimings(v.timings));
  $("cook-status").innerHTML = html;
  $("cook-done").hidden = !done;
  if (!done) {
    $("cook-code-out").textContent = "";
  } else if (v.id && codeFetchedSid !== v.id) {
    // DONE: the project code arrives on its own; SEND CODE stays as retry.
    setTimeout(() => {
      if (cookMode && cookView && cookView.state === "DONE" && codeFetchedSid !== cookView.id) cookSendCode(true);
    }, 150);
  }
}

function drawCookBoxes(detections) {
  const img = $("cook-preview"), svg = $("cook-svg");
  const w = img.naturalWidth || 640, h = img.naturalHeight || 480;
  svg.setAttribute("viewBox", "0 0 " + w + " " + h);
  svg.innerHTML = (detections || []).map((d) => {
    const b = d.box || [0, 0, 0, 0];
    const rw = Math.max(1, b[2] - b[0]), rh = Math.max(1, b[3] - b[1]);
    return '<rect x="' + b[0] + '" y="' + b[1] + '" width="' + rw + '" height="' + rh + '"/>' +
      '<text x="' + b[0] + '" y="' + Math.max(12, b[1] - 4) + '">' + esc(d.label || "") + "</text>";
  }).join("");
}

function cookConnect() {
  cookClose();
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  cookWs = new WebSocket(proto + "//" + location.host + "/api/live/session");
  cookWs.onopen = () => cookWs.send(JSON.stringify(
    { type: "hello", mode: "LEARNING", step_text: lastInstruction }));
  cookWs.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === "ready") return;
    if (cookTurnResolve) { const r = cookTurnResolve; cookTurnResolve = null; r(msg); }
  };
  cookWs.onerror = () => { if (cookMode) $("cook-heard").textContent = "Coach connection error."; };
  cookWs.onclose = () => { if (cookMode) $("cook-heard").textContent = "Coach disconnected."; };
}

function cookClose() {
  cookTurnResolve = null;
  if (cookWs) { try { cookWs.close(); } catch (e) { /* already closed */ } cookWs = null; }
}

function cookAsk(transcript) {
  return new Promise((resolve) => {
    if (!cookWs || cookWs.readyState !== WebSocket.OPEN) {
      resolve({ type: "error", detail: "coach offline" });
      return;
    }
    cookTurnResolve = resolve;
    cookWs.send(JSON.stringify({ type: "turn", transcript, step_text: lastInstruction }));
  });
}

function cookListen() {
  const btn = $("cook-talk");
  if (cookRecording) { try { cookRec.stop(); } catch (e) { /* already stopped */ } return; }
  if (!cookMode || busy) return;
  if (!("MediaRecorder" in window) || !(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
    $("cook-heard").textContent = "Audio capture unavailable — use CHECK.";
    return;
  }
  navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
    if (!cookMode) { stream.getTracks().forEach((t) => t.stop()); return; }
    cookChunks = [];
    cookRec = new MediaRecorder(stream);
    cookRec.ondataavailable = (e) => { if (e.data && e.data.size) cookChunks.push(e.data); };
    cookRec.onstop = async () => {
      cookRecording = false;
      btn.classList.remove("rec");
      btn.textContent = "TALK";
      stream.getTracks().forEach((t) => t.stop());
      if (!cookMode) return;
      $("cook-heard").textContent = "Transcribing…";
      try {
        const t = await transcribeUpload(new Blob(cookChunks, { type: "audio/webm" }));
        const text = (t.text || "").trim();
        if (!cookMode) return;
        if (!text) {
          $("cook-heard").textContent = "Heard nothing" + (t.error ? ": " + t.error : "") + ".";
          return;
        }
        lastTranscript = text;
        $("cook-heard").textContent = 'heard: "' + text + '"';
        await cookCommand(text);
      } catch (e) { $("cook-heard").textContent = "Transcription failed."; }
    };
    cookRec.start();
    cookRecording = true;
    btn.classList.add("rec");
    btn.textContent = "LISTENING…";
    $("cook-heard").textContent = "Listening…";
    setTimeout(() => { if (cookRecording && cookRec) { try { cookRec.stop(); } catch (e) {} } }, COOK_LISTEN_MS);
  }).catch(() => { $("cook-heard").textContent = "Microphone blocked."; });
}

async function cookCommand(text) {
  const t = text.toLowerCase();
  if (/(repeat|say again|again)/.test(t)) {
    cookSpeak(lastSpeak || (cookView && cookView.instruction) || "");
    return;
  }
  if (/(send code|get code|show code|read code)/.test(t)) { cookSendCode(); return; }
  if (/(check|next|verify|done|test)/.test(t)) { cookCheck(); return; }
  if (/(look|describe|what do you see|show)/.test(t)) { cookLook(); return; }
  if (/(exit|quit|close|stop cooking)/.test(t)) { exitCookMode(); return; }
  const res = await cookAsk(text);
  if (!cookMode) return;
  if (res.type === "error") {
    $("cook-heard").textContent = "Coach: " + (res.detail || "unavailable");
    return;
  }
  $("cook-heard").textContent = 'coach: "' + (res.speak || "") + '"';
  if (res.speak) lastSpeak = res.speak;
  cookSpeak(res.speak || "");
}

function cookCheck() {
  if (!cookMode || busy) return;
  checkNow("");
}

async function cookLook() {
  if (!cookMode || busy) return;
  busy = true;
  $("cook-heard").textContent = "Looking…";
  speak("Looking.");
  try {
    const r = await fetch("/api/vision/describe", { method: "POST" });
    const v = await r.json();
    if (!r.ok) {
      $("cook-heard").textContent = "Look failed: " + ((v && v.detail) || "");
      return;
    }
    drawCookBoxes((v.sam && v.sam.detections) || []);
    $("cook-heard").textContent = v.description || "";
    if (v.description) lastSpeak = v.description;
    cookSpeak(v.description || "");
  } catch (e) {
    $("cook-heard").textContent = "Look failed: " + e.message;
  } finally {
    busy = false;
  }
}

async function cookSendCode(auto) {
  if (!cookMode || busy || !learnSid) return;
  busy = true;
  try {
    const r = await fetch("/project/" + encodeURIComponent(learnSid) + "/code", { method: "POST" });
    const v = await r.json();
    if (!r.ok) {
      $("cook-heard").textContent = ((v && v.detail) || "code not ready") + " — SEND CODE retries.";
      cookSpeak("Code is not ready yet. Press Send Code to retry.");
      return;
    }
    $("cook-code-out").textContent = v.code || "";
    codeFetchedSid = learnSid;
    if (!auto) cookSpeak("Code ready. It is shown on screen.");
  } catch (e) {
    $("cook-heard").textContent = e.message + " — SEND CODE retries.";
    cookSpeak("Code fetch failed. Press Send Code to retry.");
  } finally {
    busy = false;
  }
}

function enterCookMode(v) {
  if (liveOn) stopLive("Live guide paused — cook mode running.");
  cookMode = true;
  cookView = v;
  $("cook").hidden = false;
  $("cook-heard").textContent = "";
  $("cook-svg").innerHTML = "";
  renderCook(v);
  cookConnect();
  refreshCamera();
  window.scrollTo(0, 0);
  cookSpeak(v.speak || v.instruction || "");
}

function exitCookMode() {
  cookMode = false;
  cookClose();
  if (cookRecording && cookRec) { try { cookRec.stop(); } catch (e) { /* already stopped */ } }
  cookRecording = false;
  try { speechSynthesis.cancel(); } catch (e) { /* no TTS */ }
  $("cook-talk").classList.remove("rec");
  $("cook-talk").textContent = "TALK";
  $("cook").hidden = true;
  if (cookView) renderLearning(cookView);
}

$("cook-talk").onclick = () => cookListen();
$("cook-check").onclick = () => cookCheck();
$("cook-repeat").onclick = () => cookSpeak(lastSpeak || (cookView && cookView.instruction) || "");
$("cook-look").onclick = () => cookLook();
$("cook-exit").onclick = () => exitCookMode();
$("cook-code").onclick = () => cookSendCode();
$("cook-resume").onclick = async () => {
  if (busy) return;
  busy = true;
  try {
    const v = await (await fetch("/api/status")).json();
    if (v.learning) enterCookMode(v);
    else $("talk-hint").textContent = "No learning session to resume.";
  } catch (e) { showError(e.message); } finally { busy = false; }
};

// Board handoff: guided/diagnostic fit the ESP32 kit only. When the bench
// holds another board, CHECK NOW returns a handoff instead of a lecture,
// and one tap carries its parts into Learning on the right board.
let pendingHandoff = null;
$("handoff-learn").onclick = () => {
  if (busy || !pendingHandoff) return;
  const h = pendingHandoff;
  pendingHandoff = null;
  $("handoff-learn").hidden = true;
  mode = "LEARNING";
  $("mode-learn").classList.add("active");
  $("mode-guided").classList.remove("active");
  $("mode-diag").classList.remove("active");
  $("learn-sec").hidden = false;
  $("code-sec").hidden = true;
  if (h.board) {
    $("learn-board").value = h.board;
    learnBoard = h.board;
  } else {
    $("talk-hint").textContent = "Board '" + (h.board_label || "?") +
      "' has no profile yet — pick the closest board, then SUGGEST PROJECTS.";
  }
  learnParts = (h.parts || []).filter((n) => n && n !== "unknown");
  learnConns = [];
  learnUnc = [];
  renderLearnParts(learnParts.map((n) =>
    ({ name: n, sam_confirmed: false, state: "seen in passing — unconfirmed" })));
  if (h.board && learnParts.length) {
    $("instruction").textContent = "I see " + (h.board_label || h.board) + " — suggesting projects…";
    suggestProjects();
  } else if (!h.board) {
    $("instruction").textContent = "I see " + (h.board_label || "a new board") +
      ". Pick the closest board above, name your project, then SUGGEST PROJECTS.";
  } else {
    $("instruction").textContent = "I see " + (h.board_label || h.board) +
      " but no parts yet — tap SHOW EQUIPMENT, then SUGGEST PROJECTS.";
  }
};

async function pinSnapResult(r, kind) {
  const j = await r.json();
  if (!r.ok || !j.ok) throw new Error((j && j.detail) || "snap failed");
  $("cam-src").textContent = kind + " snap (" + Math.round(j.bytes / 1024) +
    " KB) pinned — press CHECK NOW to analyze what it shows";
  const ov = $("cam-overlay");
  ov.hidden = false;
  ov.textContent = kind + " snap pinned — press CHECK NOW to analyze it.";
  refreshCamera();
}

$("snap").onclick = async () => {
  if (!cameraConfigured) { $("snap-file").click(); return; } // no overhead cam: phone upload
  $("cam-src").textContent = "Snapping overhead camera…";
  try {
    await pinSnapResult(await fetch("/api/camera/snap", { method: "POST" }), "Overhead");
  } catch (e) { $("cam-src").textContent = "Snap failed: " + e.message; }
};
$("snap-file").onchange = async () => {
  const f = $("snap-file").files[0];
  $("snap-file").value = "";
  if (!f) return;
  $("cam-src").textContent = "Uploading photo…";
  try {
    const fd = new FormData();
    fd.append("file", f, f.name || "snap.jpg");
    await pinSnapResult(await fetch("/api/camera/snap", { method: "POST", body: fd }), "Phone");
  } catch (e) { $("cam-src").textContent = "Snap failed: " + e.message; }
};
$("save-cam").onclick = async () => {
  const stream = $("cam-stream").value.trim();
  const capture = $("cam-capture").value.trim();
  $("cam-msg").textContent = "Saving…";
  try {
    const r = await fetch("/api/config/camera", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stream_url: stream, capture_url: capture }),
    });
    const j = await r.json();
    cameraConfigured = !!j.camera_frame_url_set;
    $("cam-msg").textContent = cameraConfigured
      ? "Saved — overhead camera live (SNAP now uses it)."
      : "Cleared — camera offline until URLs are set.";
    refreshCamera();
    refreshBadge();
  } catch (e) { $("cam-msg").textContent = "Save failed: " + e.message; }
};
$("clear-snap").onclick = async () => {
  try { await fetch("/api/camera/snap", { method: "DELETE" }); } catch (e) { /* offline */ }
  refreshCamera();
  $("cam-src").textContent = "Phone snap cleared.";
};

$("replay").onclick = () => speak(lastSpeak);
$("describe").onclick = async () => {
  if (busy) return;
  busy = true;
  $("instruction").textContent = "Looking…";
  speak("Looking.");
  try {
    const r = await fetch("/api/vision/describe", { method: "POST" });
    const d = await r.json();
    if (!r.ok) {
      showError((d && d.detail) || "describe failed");
      return;
    }
    $("instruction").textContent = d.description || "(no description)";
    lastSpeak = d.description || "";
    if (d.camera_source) $("cam-src").textContent = d.camera_source;
    drawEvidence(d.frame_file, (d.sam && d.sam.detections) || [], d.sam && d.sam.mock);
    speak(d.description);
  } catch (e) { showError(e.message); }
  finally { busy = false; refreshCamera(); }
};

function updateMuteBtn() {
  $("mute").textContent = muted ? "UNMUTE" : "MUTE";
  $("mute").classList.toggle("muted", muted);
}
$("mute").onclick = () => {
  muted = !muted;
  try { localStorage.setItem("fundiloop-muted", muted ? "1" : "0"); } catch (e) { /* private mode */ }
  if (muted) { try { speechSynthesis.cancel(); } catch (e) { /* no speech */ } }
  updateMuteBtn();
};
updateMuteBtn();
$("preview").onerror = () => {
  const ov = $("cam-overlay");
  if (ov.hidden) {
    ov.hidden = false;
    ov.textContent = "Camera preview failed to load — check CAMERA_STREAM_URL / CAMERA_CAPTURE_URL in .env.";
  }
};
$("voice").onchange = () => {
  voiceChoice = $("voice").value || "";
  try { localStorage.setItem("fundiloop-voice", voiceChoice); } catch (e) { /* private mode */ }
  speak(lastSpeak || "Voice updated. This is how I will sound.");
};
if ("speechSynthesis" in window) {
  populateVoices();
  speechSynthesis.onvoiceschanged = populateVoices; // voices load async
}

// Boot: load status (or start a session), then keep the preview fresh.
fetch("/api/status").then((r) => r.json()).then((v) => {
  if (v.config) {
    paintBadge(v.config);
    cameraConfigured = !!v.config.camera_capture_url;
    if (v.config.camera_stream_url) $("cam-stream").value = v.config.camera_stream_url;
    if (v.config.camera_capture_url) $("cam-capture").value = v.config.camera_capture_url;
    if (cameraConfigured) {
      $("cam-src").textContent = "camera: configured — waiting for first frame…";
    } else {
      $("cam-src").textContent = "camera: offline (set CAMERA_STREAM_URL / CAMERA_CAPTURE_URL in .env)";
      const ov = $("cam-overlay");
      ov.hidden = false;
      ov.textContent = "Camera offline — set CAMERA_STREAM_URL / CAMERA_CAPTURE_URL in .env for the live overhead feed.";
    }
  }
  if (v.learning) { enterLearning(v); return; }
  if (!v.session && !v.step) return startSession();
  render(v);
}).catch((e) => { showError("backend unreachable: " + e.message); });
refreshCamera();
setInterval(refreshCamera, 3000);
setInterval(refreshBadge, 5000);
