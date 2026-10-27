/* FundiLoop mobile UI: session control, camera preview, speech, checklist. */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
let mode = "GUIDED_ASSEMBLY";
let busy = false;
let lastSpeak = "";
let demoMode = true;
let muted = false;
try { muted = localStorage.getItem("fundiloop-muted") === "1"; } catch (e) { /* private mode */ }

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
  const prefs = [/google us english/i, /google uk english/i, /natural/i, /neural/i,
    /samantha/i, /zira/i, /david/i, /aria/i, /jenny/i, /guy/i];
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

function speak(text) {
  if (!text || muted) return;
  try {
    if (!("speechSynthesis" in window)) return;
    speechSynthesis.cancel();
    const voice = pickVoice();
    // Sentence chunks: some mobile browsers stall on single long utterances.
    const parts = String(text).match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [text];
    parts.forEach((p, i) => {
      const u = new SpeechSynthesisUtterance(p.trim());
      u.lang = "en-US";
      u.rate = 0.98;
      u.pitch = 1.0;
      if (voice) u.voice = voice;
      speechSynthesis.speak(u);
    });
  } catch (e) { console.log("speech failed", e); }
}

function refreshCamera() {
  $("preview").src = "/api/camera/latest?t=" + Date.now();
}

async function startSession() {
  const r = await fetch("/api/session/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  const view = await r.json();
  render(view);
  speak(view.speak);
}

function showFallback(which) {
  const badge = $("fallback-badge");
  if (which && which.length) {
    badge.hidden = false;
    badge.textContent = "FALLBACK: " + which.join(", ");
    badge.title = "Demo mode served mock " + which.join(", ") + " after a live failure";
  } else {
    badge.hidden = true;
  }
}

async function setDemo(enabled) {
  try {
    const r = await fetch("/api/config/demo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    demoMode = !!(await r.json()).demo_mode;
  } catch (e) { console.log("demo toggle failed", e); }
  $("demo").textContent = demoMode ? "DEMO: ON" : "DEMO: OFF";
  $("demo").classList.toggle("off", !demoMode);
}

async function checkNow(transcript) {
  if (busy) return;
  busy = true;
  $("check").disabled = true;
  $("instruction").textContent = "Working… sensing, reasoning, verifying.";
  try {
    const fault = $("fault").value || null;
    const r = await fetch("/api/session/check-now", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        code_snippet: $("code").value || "",
        simulate_fault: fault,
        transcript: transcript || "",
      }),
    });
    const view = await r.json();
    render(view);
    speak(view.speak);
  } catch (e) {
    $("instruction").textContent = "Error: " + e.message;
  } finally {
    busy = false;
    $("check").disabled = false;
    refreshCamera();
  }
}

function render(v) {
  const box = $("instruction");
  box.textContent = v.instruction || "(no instruction)";
  box.classList.toggle("fault", v.state === "FAULT_FOUND");
  box.classList.toggle("done", v.state === "COMPLETE" || v.state === "VERIFIED_FIXED");
  if (v.speak) lastSpeak = v.speak;
  if ("fallbacks" in v) showFallback(v.fallbacks);
  if (v.camera_source) {
    const src = v.camera_source;
    $("cam-src").textContent = src;
    const ov = $("cam-overlay");
    if (src.indexOf("mock-fallback") === 0) {
      ov.hidden = false;
      ov.textContent = "Camera unreachable — showing a mock frame. Check CAMERA_FRAME_URL in .env.";
    } else if (src.indexOf("mock") === 0) {
      ov.hidden = false;
      ov.textContent = "Mock camera — set CAMERA_FRAME_URL in .env for the live overhead feed.";
    } else {
      ov.hidden = true;
    }
  }
  if (typeof v.demo_mode === "boolean") {
    demoMode = v.demo_mode;
    $("demo").textContent = demoMode ? "DEMO: ON" : "DEMO: OFF";
    $("demo").classList.toggle("off", !demoMode);
  }
  $("state-line").textContent =
    "state: " + (v.state || "—") + "  ·  mode: " + (v.mode || "—") +
    (v.step ? "  ·  step " + (v.step_index + 1) + ": " + v.step.step_id : "") +
    (v.attempts_total ? "  ·  attempts: " + v.attempts_total : "") +
    (v.camera_source ? "  ·  " + v.camera_source : "") +
    (v.frame_settled === false ? "  ·  frame settling…" : "");
  if (v.cam_src) $("cam-src").textContent = v.cam_src;

  const p = v.probe || {};
  const ver = v.verification || {};
  let html =
    "I2C devices: [" + esc((p.i2c_devices || []).join(", ") || "none") + "]\n" +
    "sensor: " + (p.temperature_c == null ? "—" :
      esc(p.temperature_c + " C, " + p.humidity_pct + " %, " + p.pressure_hpa + " hPa")) + "\n" +
    "OLED: " + (p.oled_ok == null ? "—" : p.oled_ok ? "lit" : "dark") +
    "   LED: " + (p.led_ok == null ? "—" : p.led_ok ? "blinks" : "dead") + "\n" +
    (ver.test ? "last test: " + esc(ver.test) + " → " + (ver.passed ? "PASS" : "FAIL") +
      "\nmeasured: " + esc(ver.measured || "") : "no test run yet") +
    (p.mock ? "\n(mock data)" : "");
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
}

// Mode toggle restarts the session in that mode.
$("mode-guided").onclick = () => {
  mode = "GUIDED_ASSEMBLY";
  $("mode-guided").classList.add("active");
  $("mode-diag").classList.remove("active");
  startSession().catch((e) => { $("instruction").textContent = "Error: " + e.message; });
};
$("mode-diag").onclick = () => {
  mode = "DIAGNOSTIC";
  $("mode-diag").classList.add("active");
  $("mode-guided").classList.remove("active");
  startSession().catch((e) => { $("instruction").textContent = "Error: " + e.message; });
};
$("check").onclick = () => checkNow("");

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
  if (busy) return;
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
        const tag = t.note ? " (" + t.note + ")" : "";
        if (t.demo_fallback) showFallback(["voice"]);
        if (!text) {
          $("talk-hint").textContent = "Transcription empty" + (t.error ? ": " + t.error : "") + " — use CHECK NOW.";
          return;
        }
        const hit = TRIGGER_WORDS.some((w) => text.toLowerCase().includes(w));
        $("talk-hint").textContent = 'heard: "' + text + '"' + tag + (hit ? " → checking…" : " — say done / fixed / moved it, or CHECK NOW.");
        if (hit) checkNow(text);
      } catch (e) { $("talk-hint").textContent = "Transcription failed — use CHECK NOW."; }
    };
    pttRec.start();
    pttRecording = true;
    btn.classList.add("rec");
    btn.textContent = "LISTENING — TAP TO STOP";
  }).catch(() => { $("talk-hint").textContent = "Microphone blocked — use CHECK NOW."; });
}
$("talk").onclick = () => pttToggle($("talk"));

$("replay").onclick = () => speak(lastSpeak);
$("demo").onclick = () => setDemo(!demoMode);

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
    ov.textContent = "Camera frame failed to load — is the server still running?";
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
    if (typeof v.config.demo_mode === "boolean") {
      demoMode = v.config.demo_mode;
      $("demo").textContent = demoMode ? "DEMO: ON" : "DEMO: OFF";
    }
    if (v.config.camera_frame_url_set) {
      $("cam-src").textContent = "camera: configured — waiting for first frame…";
    } else {
      $("cam-src").textContent = "camera: mock frames (set CAMERA_FRAME_URL for live)";
      const ov = $("cam-overlay");
      ov.hidden = false;
      ov.textContent = "Mock camera — set CAMERA_FRAME_URL in .env for the live overhead feed.";
    }
  }
  if (!v.session && !v.step) return startSession();
  render(v);
}).catch((e) => { $("instruction").textContent = "Backend unreachable: " + e.message; });
refreshCamera();
setInterval(refreshCamera, 3000);
