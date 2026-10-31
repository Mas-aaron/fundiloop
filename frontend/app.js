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

// Speech: all output goes through VoiceEngine. Meta Voice TTS (server,
// warm female voice) is primary everywhere; default browser
// speechSynthesis is the per-utterance fallback when the server path
// fails, retried fresh next turn. Clauses stream sentence-by-sentence
// so phones don't cut off.
let voiceChoice = "";
try { voiceChoice = localStorage.getItem("fundiloop-voice") || ""; } catch (e) { /* private mode */ }

// Fallback voice ranking: an explicit pick always wins (even a robotic
// one); otherwise prefer the most natural-sounding English voice so Auto
// never settles for the cheap default. Never refuses: the last resort
// is the first English voice, then the browser default (null).
const FALLBACK_VOICE_PREFS = [/google us english/i, /google uk english female/i,
  /google uk english/i, /natural/i, /neural/i, /premium/i, /enhanced/i,
  /online/i, /google/i];
function rankFallbackVoice(voices, choice) {
  const vs = (voices || []).filter((v) => v && v.lang && String(v.lang).toLowerCase().startsWith("en"));
  if (choice) {
    const named = vs.find((v) => v.name === choice);
    if (named) return named;
  }
  for (const p of FALLBACK_VOICE_PREFS) {
    const hit = vs.find((v) => p.test(v.name || ""));
    if (hit) return hit;
  }
  return vs[0] || null;
}
function pickFallbackVoice(voices) {
  return rankFallbackVoice(voices, voiceChoice);
}

function browserVoices() {
  try {
    if (!("speechSynthesis" in window)) return [];
    return speechSynthesis.getVoices().filter((v) => v.lang && v.lang.toLowerCase().startsWith("en"));
  } catch (e) { return []; }
}

function populateVoices() {
  const sel = $("voice");
  if (!sel || !("speechSynthesis" in window)) return;
  const vs = browserVoices();
  sel.innerHTML = '<option value="">Auto (best available)</option>' +
    vs.map((v) => '<option value="' + esc(v.name) + '"' +
      (v.name === voiceChoice ? " selected" : "") + ">" +
      esc(v.name) + "</option>").join("");
  refreshVoiceStatus();
}

// Setup check: shows which browser voice backs Meta TTS right now, so a
// bad inventory is caught in Advanced, not mid-lesson.
function refreshVoiceStatus() {
  const el = $("voice-status");
  if (!el) return;
  if (!("speechSynthesis" in window)) {
    el.textContent = "Meta Voice TTS primary; no browser speechSynthesis for fallback on this device.";
    return;
  }
  const v = pickFallbackVoice(speechSynthesis.getVoices());
  el.textContent = v ? "Meta Voice TTS primary; browser fallback: " + v.name
    : "Meta Voice TTS primary; no English browser voice for fallback (default voice will try).";
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
  s = s.replace(/<->/g, " to ");
  s = s.replace(/<-|←/g, " from ");
  s = s.replace(/->|=>|→/g, " to ");
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

let speakSeq = 0; // newest speak() call wins; older queues stay silent
let clauseStreamSeq = 0; // live clause stream id; streams older than this are dead
let serverAudio = null; // current Meta TTS Audio element (speak path)
let serverAudioUrl = ""; // its object URL (revoked on stop/end)
let serverQueue = []; // queued Meta TTS clause clips: {id, url, text}
let serverPlaying = false; // a queued clause is sounding now
let streamServerOk = true; // this stream still tries Meta first
let streamFetchTail = Promise.resolve(); // clause fetches stay in order
let streamFetchPending = 0; // fetches in flight (streamEnd waits)

function revokeAudioUrl(url) {
  try { if (url) URL.revokeObjectURL(url); } catch (e) { /* already revoked */ }
}

function stopServerAudio() {
  if (serverAudio) {
    try { serverAudio.pause(); } catch (e) { /* already stopped */ }
    serverAudio = null;
  }
  if (serverAudioUrl) { revokeAudioUrl(serverAudioUrl); serverAudioUrl = ""; }
}

function clearServerQueue() {
  serverQueue.forEach((item) => revokeAudioUrl(item.url));
  serverQueue = [];
  serverPlaying = false;
}

// One Meta TTS fetch: object URL on playable audio, null on any failure
// (the caller falls back to browser speech for that utterance only).
async function fetchServerSpeech(text) {
  try {
    const r = await fetch("/api/voice/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) return null;
    const blob = await r.blob();
    if (!blob || !blob.size) return null;
    return URL.createObjectURL(blob);
  } catch (e) { return null; }
}

function speakBrowserNow(say, done) {
  // Default browser speech: any fallback voice, never refused.
  const finish = () => { if (done) { try { done(); } catch (e) { console.log("speak onend failed", e); } } };
  try {
    if (!("speechSynthesis" in window)) { finish(); return; }
    speechSynthesis.cancel();
    if (speechSynthesis.paused) speechSynthesis.resume();
    const voice = pickFallbackVoice(speechSynthesis.getVoices());
    // Sentence chunks: some mobile browsers stall on single long utterances.
    // Decimals are protected so "0.55" is never split mid-number.
    const safe = String(say).replace(/(\d)\.(\d)/g, "$1<DOT>$2");
    const parts = (safe.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [safe])
      .map((p) => p.replace(/<DOT>/g, "."));
    parts.forEach((p, i) => {
      const u = new SpeechSynthesisUtterance(p.trim());
      u.lang = "en-US";
      u.rate = 0.98;
      u.pitch = 1.0;
      if (voice) u.voice = voice;
      if (i === parts.length - 1) u.onend = finish;
      speechSynthesis.speak(u);
    });
  } catch (e) { console.log("speech failed", e); finish(); }
}

function speakBrowserClause(say) {
  // One streamed clause appended to the browser queue (no cancel).
  try {
    if (!("speechSynthesis" in window)) return;
    const voice = pickFallbackVoice(speechSynthesis.getVoices());
    const u = new SpeechSynthesisUtterance(say);
    u.lang = "en-US";
    u.rate = 0.98;
    u.pitch = 1.0;
    if (voice) u.voice = voice;
    speechSynthesis.speak(u);
  } catch (e) { console.log("speech failed", e); }
}

function pumpServerQueue(id) {
  if (serverPlaying) return;
  const item = serverQueue.shift();
  if (!item) return;
  if (item.id !== clauseStreamSeq || item.id !== id) {
    revokeAudioUrl(item.url); // superseded stream stays silent
    pumpServerQueue(id);
    return;
  }
  serverPlaying = true;
  let el;
  try {
    el = new Audio(item.url);
  } catch (e) {
    revokeAudioUrl(item.url);
    serverPlaying = false;
    speakBrowserClause(item.text);
    pumpServerQueue(id);
    return;
  }
  el.onended = () => {
    revokeAudioUrl(item.url);
    serverPlaying = false;
    pumpServerQueue(id);
  };
  el.onerror = () => {
    revokeAudioUrl(item.url);
    serverPlaying = false;
    speakBrowserClause(item.text); // broken clip still sounds
    pumpServerQueue(id);
  };
  try {
    const play = el.play();
    if (play && play.catch) play.catch(() => {
      revokeAudioUrl(item.url);
      serverPlaying = false;
      speakBrowserClause(item.text);
      pumpServerQueue(id);
    });
  } catch (e) {
    revokeAudioUrl(item.url);
    serverPlaying = false;
    speakBrowserClause(item.text);
    pumpServerQueue(id);
  }
}

function splitSpeakChunks(say) {
  const safe = String(say).replace(/(\d)\.(\d)/g, "$1<DOT>$2");
  const parts = (safe.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [safe])
    .map((p) => p.replace(/<DOT>/g, ".").trim()).filter((p) => p);
  // Pack small sentences into 60-140 char chunks: first audio sounds
  // after one short synthesis (~36ms/char server-side), and each
  // chunk's playback (~1s per 15 chars) hides the next chunk's
  // prefetch. Monster sentences split into even word-gap pieces.
  const chunks = [];
  let cur = "";
  const pushCur = () => { if (cur) { chunks.push(cur); cur = ""; } };
  const pushMonster = (p) => {
    // Evenly spaced word-gap cuts: greedy slicing leaves a tiny tail
    // chunk whose playback can't cover the next chunk's synthesis.
    const n = Math.ceil(p.length / 140);
    let start = 0;
    for (let i = 1; i < n; i++) {
      const target = Math.round((p.length * i) / n);
      let cut = p.lastIndexOf(" ", target);
      if (cut < start + 40) cut = p.indexOf(" ", target);
      if (cut < 0 || cut <= start) break; // no usable gap: rest goes whole
      const piece = p.slice(start, cut).trim();
      if (piece) chunks.push(piece);
      start = cut;
    }
    const tail = p.slice(start).trim();
    if (tail) chunks.push(tail);
  };
  for (const p of parts) {
    if (p.length > 140) { pushCur(); pushMonster(p); continue; }
    if (cur && (cur.length >= 60 || (cur + " " + p).length > 140)) pushCur();
    cur = cur ? cur + " " + p : p;
  }
  pushCur();
  return chunks.length ? chunks : [say];
}

// Long utterances synthesize chunk-by-chunk with the next chunk
// prefetched during playback: first audio sounds after one short
// synthesis instead of one long one. Any chunk failure speaks the
// REMAINDER via the browser fallback and stops the chain.
function playServerChunks(chunks, mySpeech, done) {
  const playUrl = (url, i) => {
    if (mySpeech !== speakSeq) { revokeAudioUrl(url); return; }
    if (i >= chunks.length) { done(); return; }
    if (!url) {
      speakBrowserNow(chunks.slice(i).join(" "), done);
      return;
    }
    let el;
    try {
      el = new Audio(url);
    } catch (e) {
      revokeAudioUrl(url);
      speakBrowserNow(chunks.slice(i).join(" "), done);
      return;
    }
    serverAudio = el;
    serverAudioUrl = url;
    const nextPromise = (i + 1 < chunks.length)
      ? fetchServerSpeech(chunks[i + 1]) : null;
    const advance = () => {
      if (mySpeech !== speakSeq) return;
      stopServerAudio();
      if (!nextPromise) { done(); return; } // last chunk played
      nextPromise.then((nextUrl) => playUrl(nextUrl, i + 1));
    };
    el.onended = advance;
    const broken = () => {
      if (mySpeech !== speakSeq) return;
      stopServerAudio();
      speakBrowserNow(chunks.slice(i).join(" "), done);
    };
    el.onerror = broken;
    try {
      const play = el.play();
      if (play && play.catch) play.catch(broken);
    } catch (e) {
      broken();
    }
  };
  fetchServerSpeech(chunks[0]).then((url) => playUrl(url, 0));
}

const VoiceEngine = {
  // Single choke point for all spoken output. Meta Voice TTS audio is
  // primary; any server failure falls back to default browser speech
  // for that utterance only, retried fresh next turn. speak() stays a
  // thin wrapper so existing call sites are unchanged.
  speak(text, onend) {
    const done = () => { if (onend) { try { onend(); } catch (e) { console.log("speak onend failed", e); } } };
    clauseStreamSeq++; // a full speak takes over: any live stream goes silent
    stopServerAudio();
    clearServerQueue();
    if (!text || muted) { done(); return; }
    const mySpeech = ++speakSeq;
    // One tick of delay lets a previous cancel() settle so rapid speaks
    // never overlap into noise; a superseded speech drops silently.
    setTimeout(() => {
      if (mySpeech !== speakSeq) return;
      playServerChunks(splitSpeakChunks(speakable(text)), mySpeech, done);
    }, 60);
  },
  // Clause streaming: speak answer tokens as they arrive without chopping.
  // streamStart cancels any older speech and owns the queue; streamClause
  // appends one clause (Meta audio queued in order, browser fallback per
  // clause once the server fails); streamEnd waits for the queue to drain.
  streamStart() {
    const id = ++clauseStreamSeq;
    speakSeq++; // an in-flight speak() drops; this stream owns speech now
    stopServerAudio();
    clearServerQueue();
    streamServerOk = true;
    streamFetchTail = Promise.resolve();
    streamFetchPending = 0;
    try {
      if (!("speechSynthesis" in window)) return id;
      speechSynthesis.cancel();
      if (speechSynthesis.paused) speechSynthesis.resume();
    } catch (e) { /* no speech */ }
    return id;
  },
  streamClause(id, clause) {
    if (id !== clauseStreamSeq) return; // superseded stream stays silent
    const say = speakable(clause);
    if (!say || muted) return;
    if (!streamServerOk) { speakBrowserClause(say); return; }
    streamFetchPending++;
    streamFetchTail = streamFetchTail.then(() =>
      fetchServerSpeech(say).then((url) => {
        streamFetchPending--;
        if (id !== clauseStreamSeq) { revokeAudioUrl(url); return; }
        if (url) {
          serverQueue.push({ id: id, url: url, text: say });
          pumpServerQueue(id);
        } else {
          streamServerOk = false; // rest of this stream speaks locally
          speakBrowserClause(say);
        }
      }).catch(() => {
        streamFetchPending--;
        if (id !== clauseStreamSeq) return;
        streamServerOk = false;
        speakBrowserClause(say);
      })
    );
  },
  streamEnd(id, onend) {
    const done = () => { if (onend) { try { onend(); } catch (e) { console.log("speak onend failed", e); } } };
    if (id !== clauseStreamSeq) return;
    const t0 = Date.now();
    const wait = () => {
      if (id !== clauseStreamSeq) return;
      let busySp = streamFetchPending > 0 || serverQueue.length > 0 || serverPlaying;
      try {
        busySp = busySp || (("speechSynthesis" in window) &&
          (speechSynthesis.speaking || speechSynthesis.pending));
      } catch (e) { /* no speech */ }
      if (!busySp || Date.now() - t0 > 30000) { done(); return; }
      setTimeout(wait, 250);
    };
    setTimeout(wait, 500); // let the queue start before judging it idle
  },
  // Interrupt everything: a new turn or a listen tap owns speech now.
  stop() {
    speakSeq++;
    clauseStreamSeq++;
    streamServerOk = true;
    stopServerAudio();
    clearServerQueue();
    try { if ("speechSynthesis" in window) speechSynthesis.cancel(); } catch (e) { /* no speech */ }
  },
};

function speak(text, onend) { VoiceEngine.speak(text, onend); }

// Orb: the visible voice of the app. One state at a time across every
// .orb element (main screen + cook overlay): error > speaking >
// listening > thinking > idle. Listening/thinking are explicit counters
// (nested turns overlap); speaking is polled from live audio state.
const Orb = (() => {
  let listenDepth = 0, thinkDepth = 0, errUntil = 0;
  function speakingNow() {
    try {
      if (serverAudio || serverPlaying) return true;
      if ("speechSynthesis" in window) {
        return !!(speechSynthesis.speaking || speechSynthesis.pending);
      }
    } catch (e) { /* no speech */ }
    return false;
  }
  function paint() {
    let state = "";
    if (Date.now() < errUntil) state = "is-error";
    else if (speakingNow()) state = "is-speaking";
    else if (listenDepth > 0) state = "is-listening";
    else if (thinkDepth > 0) state = "is-thinking";
    for (const el of document.querySelectorAll(".orb")) {
      if (el._orbState === state) continue;
      el._orbState = state;
      el.classList.remove("is-listening", "is-thinking", "is-speaking", "is-error");
      if (state) el.classList.add(state);
    }
  }
  setInterval(paint, 400);
  return {
    listenStart() { listenDepth++; paint(); },
    listenEnd() { listenDepth = Math.max(0, listenDepth - 1); paint(); },
    thinkStart() { thinkDepth++; paint(); },
    thinkEnd() { thinkDepth = Math.max(0, thinkDepth - 1); paint(); },
    error() { errUntil = Date.now() + 2500; paint(); },
  };
})();

// clausestream:splitter:start
const MIN_CLAUSE_LEN = 24; // start speaking once a clause passes this length
// Split streamed answer text at punctuation boundaries (. , ? ! newline)
// so clauses sound as they land. Decimals stay intact. Returns the clauses
// ready to speak plus the still-incomplete remainder.
function cutClauses(pending, flush) {
  const clauses = [];
  let rest = pending;
  for (;;) {
    let cut = -1;
    for (let i = 0; i < rest.length; i++) {
      const c = rest[i];
      if (c === "." && i > 0 && i + 1 < rest.length &&
        /\d/.test(rest[i - 1]) && /\d/.test(rest[i + 1])) continue;
      if (c === "." || c === "," || c === "?" || c === "!" || c === "\n") { cut = i; break; }
    }
    if (cut < 0) break;
    if (!flush && rest.length < MIN_CLAUSE_LEN) break;
    const head = rest.slice(0, cut + 1).trim();
    rest = rest.slice(cut + 1);
    if (head) clauses.push(head);
  }
  if (flush && rest.trim()) { clauses.push(rest.trim()); rest = ""; }
  return { clauses, rest };
}
// clausestream:splitter:end

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
  Orb.error();
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
      let detail = {};
      try { detail = data ? JSON.parse(data) : {}; } catch (e) { /* stage only */ }
      onStage(name, detail);
    }
    if (done) return false; // stream died mid-turn: caller falls back
  }
}

async function checkNow(transcript) {
  if (busy) return;
  busy = true;
  Orb.thinkStart();
  checkAbort = new AbortController();
  $("check").disabled = true;
  $("cancel").hidden = false;
  const t0 = Date.now();
  let stage = "Working…";
  const paint = () => {
    $("instruction").textContent = stage + " " + Math.round((Date.now() - t0) / 1000) + "s";
  };
  $("instruction").textContent = "Checking…";
  speak(transcript ? "Got it. Checking." : "Checking."); // ack the technician, then the verdict replaces it
  const tick = setInterval(paint, 1000);
  try {
    const fault = $("fault").value || null;
    const payload = {
      code_snippet: $("code").value || "",
      simulate_fault: fault,
      transcript: transcript || "",
    };
    try {
      const onStage = (name, detail) => {
        if (CHECK_STAGES[name]) { stage = CHECK_STAGES[name]; paint(); }
        else if (name === "CHAPTER_PASSED" || name === "CHAPTER_HINT") {
          paintFundi({ event: name, chapter: (detail && detail.chapter) || null,
            next_chapter: (detail && detail.next_chapter) || null,
            celebration: (detail && detail.celebration) || "" });
        }
      };
      if (await checkNowStream(payload, onStage)) return;
    } catch (e) {
      if (checkAbort && checkAbort.signal.aborted) {
        pendingCheck = "";
        $("instruction").textContent = "Cancelled.";
        VoiceEngine.stop();
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
    Orb.thinkEnd();
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
  paintFundi(null); // guided/diagnostic turns carry no lesson chapter
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
let learnStepN = 1; // current lesson step number (1-based) for STEP CODE
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

// Lesson chapter banner: the banner always names the chapter to work on
// (START/HINT: current; PASSED: the next one; DONE: the payoff line).
// Visual only — speech comes from the done view, never from live events.
function paintFundi(fundi) {
  const cook = cookMode;
  const banner = $(cook ? "cook-chapter" : "learn-chapter");
  const box = $(cook ? "cook-instruction" : "instruction");
  if (!fundi || !fundi.event) { banner.hidden = true; return; }
  const ev = fundi.event;
  const show = (ev === "CHAPTER_PASSED" ? fundi.next_chapter : fundi.chapter) || null;
  if (ev === "LESSON_DONE") {
    banner.textContent = "Lesson complete — your project code is below.";
  } else if (show && show.title) {
    banner.textContent = show.title;
  } else {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  if (ev === "CHAPTER_PASSED" || ev === "LESSON_DONE") {
    box.classList.remove("cheer");
    void box.offsetWidth; // restart the flash animation
    box.classList.add("cheer");
    setTimeout(() => box.classList.remove("cheer"), 1500);
  }
}

function renderLearning(v) {
  paintConfirm(v);
  paintFundi(v.fundi);
  if (v.id) learnSid = v.id;
  learnStepN = (v.step_index || 0) + 1;
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
  $("code-sec").hidden = false; // drafts available anytime, final at DONE
  $("cook-resume").hidden = !learnSid;
  drawEvidence(); // learning turns are probe-only: no analyzed frame
  if (v.state !== "DONE") {
    if ($("code-out").textContent) $("code-out").textContent = "";
  } else if (v.id && codeFetchedSid !== v.id) {
    // DONE: the project code arrives on its own; SEND CODE stays as retry.
    setTimeout(() => { if (!cookMode && learnSid && codeFetchedSid !== learnSid) fetchCode(true); }, 150);
  }
  // Silent board: surface the sketch to copy without hunting for it.
  const bs = ((v.verification) || {}).board_state || "";
  if (String(bs).indexOf("silent:") === 0) fetchSketch(true);
  if (v.step_code && v.step_code.code) {
    $("code-out").textContent = v.step_code.code;
    const ports = String(bs).indexOf("silent:") === 0 ? String(bs).slice(7) : "";
    $("code-kind").textContent = "Step " + v.step_code.n + " sketch — copy it into Arduino IDE, Upload" +
      (ports ? " to " + ports : "") + ", close Serial Monitor, then press CHECK NOW.";
  }
  refreshSchematics();
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
    if (!pr.ok) { showError("discovery failed"); return -1; }
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
    // The caller auto-continues into planning (its ack speaks next),
    // so only speak here when there is nothing to continue into.
    const want = ($("learn-project").value || "").trim();
    if (!learnParts.length && !want) speak(lastSpeak);
    return learnParts.length;
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
    const found = await runPartsOnFrame(cap.frame_path);
    busy = false;
    if (found >= 0 && (found > 0 || ($("learn-project").value || "").trim())) {
      await suggestProjects(); // analysis flows straight into suggestions
    }
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
    const found = await runPartsOnFrame(up.frame_path);
    busy = false;
    if (found >= 0 && (found > 0 || ($("learn-project").value || "").trim())) {
      await suggestProjects(); // analysis flows straight into suggestions
    }
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
    $("instruction").textContent = "Tap a project to start: " + titles +
      (hint ? " Note: " + hint : "");
    speak("I found " + learnProjects.length + " projects: " + titles +
      ". Say its name or number, or tap one to start." +
      (hint ? " Note: " + hint : ""),
      () => listenForProjectChoice());
  };
  let tick = null;
  try {
    if (planCache.key === key && planCache.projects) {
      renderPlans(planCache.projects, planCache.hint); // prefetched: instant
      return;
    }
    const label = "Planning projects for " + learnBoard;
    const t0 = Date.now();
    $("instruction").textContent = label + "…";
    speak("Planning your projects.");
    tick = setInterval(() => {
      $("instruction").textContent = label + "… " +
        Math.round((Date.now() - t0) / 1000) + "s";
    }, 500);
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
  } catch (e) { showError(e.message); }
  finally { if (tick) clearInterval(tick); busy = false; }
}

async function startLearning(project) {
  if (busy) return;
  busy = true;
  learnBoard = $("learn-board").value || "esp32_devkit";
  const startLabel = "Starting " + (project.title || "project");
  const startT0 = Date.now();
  $("instruction").textContent = startLabel + "…";
  speak("Starting " + (project.title || "your project") + ". Building your steps.");
  const startTick = setInterval(() => {
    $("instruction").textContent = startLabel + "… " +
      Math.round((Date.now() - startT0) / 1000) + "s";
  }, 500);
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
    const parts = learnParts.length ? learnParts.join(", ") : "your parts";
    v.speak = "We'll build " + (project.title || "this project") + ". You need: " + parts +
      ". " + (v.speak || v.instruction || "");
    enterCookMode(v);
  } catch (e) { showError(e.message); }
  finally { clearInterval(startTick); busy = false; }
}

// choice:matcher:start
function matchProjectChoice(text, projects) {
  const t = String(text || "").toLowerCase();
  if (!t.trim() || !projects || !projects.length) return null;
  const escRe = (w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const has = (w) => new RegExp("\\b" + escRe(w) + "\\b").test(t);
  const nums = [["first", "one", "1", "1st"], ["second", "two", "2", "2nd"],
                ["third", "three", "3", "3rd"]];
  // Specific ordinals first: "the second one" holds bare "one" too.
  for (const i of [1, 2, 0]) {
    if (i >= Math.min(nums.length, projects.length)) continue;
    if (nums[i].some(has)) return projects[i];
  }
  for (const p of projects) {
    const title = String((p && p.title) || "").toLowerCase().trim();
    if (!title) continue;
    if (has(title)) return p;
    const words = title.split(/\s+/).filter((w) => w.length > 3);
    if (words.length && words.every(has)) return p;
  }
  return null;
}
// choice:matcher:end

let suggestListening = false; // one-shot project pick after SUGGEST

function listenForProjectChoice() {
  if (suggestListening || !learnProjects.length || busy) return;
  suggestListening = true;
  const started = startBrowserRecognition({
    btn: $("talk"), activeText: "LISTENING…", idleText: "PUSH TO TALK",
    hintEl: $("talk-hint"), hintListening: "Say the name or number…",
    onText: (text) => {
      if (!suggestListening) return;
      suggestListening = false;
      if (!text) return; // cards stay tappable
      const pick = matchProjectChoice(text, learnProjects);
      if (pick && !busy) {
        startLearning(pick);
      } else if (pick) {
        $("talk-hint").textContent = "I'm busy — tap a card to start.";
      } else {
        $("talk-hint").textContent = 'heard: "' + text + '" — tap a card to start.';
        speak("Tap a card to start.");
      }
    },
    onEmpty: () => {
      suggestListening = false;
      $("talk-hint").textContent = "Tap a card to start.";
    },
    onFallback: () => { suggestListening = false; }, // cards stay tappable
  });
  if (!started) suggestListening = false;
}

$("learn-equip").onclick = () => showEquipment();
$("learn-suggest").onclick = () => suggestProjects();
$("learn-board").onchange = () => prefetchPlans();
$("learn-cards").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-p]");
  if (!li) return;
  if (busy) { $("talk-hint").textContent = "Working — one moment…"; return; }
  $("learn-cards").querySelectorAll(".tapped").forEach((t) => t.classList.remove("tapped"));
  li.classList.add("tapped");
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
    const r = await fetch("/project/" + encodeURIComponent(learnSid) + "/code", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft: true }),
    });
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
    const progress = (v.steps_passed || 0) + " of " + (v.steps_total || 0) + " steps verified";
    $("code-kind").textContent = v.verified ? "verified final" : "draft (" + progress + ")";
    if (auto) return;
    speak(v.verified ? "Code ready." :
      "Here's your draft code, " + progress + ". Keep building to verify the rest.");
  } catch (e) {
    if (auto) {
      $("talk-hint").textContent = e.message + " — press SEND CODE to retry.";
      speak("Code fetch failed. Press Send Code to retry.");
    } else showError(e.message);
  } finally { busy = false; }
}
async function fetchStepCode(preId, heardId) {
  if (busy || !learnSid) return;
  busy = true;
  $(heardId).textContent = "Fetching step " + learnStepN + " code…";
  try {
    const r = await fetch("/project/" + encodeURIComponent(learnSid) +
      "/step/" + learnStepN + "/code");
    const v = await r.json();
    if (!r.ok) { $(heardId).textContent = "Step code unavailable."; return; }
    $(preId).textContent = v.code || "";
    $(heardId).textContent = "Step " + v.n + " code below (" +
      (v.source || "template") + "). Upload it, then press CHECK NOW.";
  } catch (e) { $(heardId).textContent = "Step code failed: " + e.message; }
  finally { busy = false; }
}
$("send-code").onclick = () => fetchCode(false);
$("send-step-code").onclick = () => fetchStepCode("code-out", "code-kind");

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

// Per-step wiring schematic (Learning mode, ESP32 only). The panel loads
// quietly with each step render; SPEAK WIRES reads the wire list aloud
// and DOWNLOAD SVG saves the drawing. Failures stay local to the panel.
let schematicState = { svg: "", wires: [], n: 0, note: "" };
let cookSchematicState = { svg: "", wires: [], n: 0, note: "" };
let schematicBusy = false;

function wireListText(wires, unresolved, net) {
  const byPart = {};
  (wires || []).forEach((w) => {
    (byPart[w.part] = byPart[w.part] || []).push(
      w.part_pin + " to " + w.board_pin);
  });
  const lines = Object.keys(byPart).sort().map((p) =>
    p + ": " + byPart[p].join(", "));
  (unresolved || []).forEach((u) => {
    lines.push("unresolved: " + u.part + " (" + (u.reason || "no profile") + ")");
  });
  const notes = (net && net.notes) || [];
  if (!lines.length && !notes.length) {
    lines.push("No wiring yet for this step.");
  }
  notes.forEach((n) => lines.push(n));
  return lines.join("\n");
}

function wireSpeechText(wires, n) {
  const lead = (wires || []).slice(0, 8).map((w) =>
    w.part + " " + w.part_pin + " to " + w.board_pin);
  let text = "Step " + n + " wiring: " + lead.join(", ");
  if ((wires || []).length > 8) {
    text += ", and " + ((wires || []).length - 8) + " more wires";
  }
  return text;
}

async function fetchSchematic(svgId, wiresId, state) {
  if (schematicBusy || !learnSid) return;
  schematicBusy = true;
  try {
    const r = await fetch("/project/" + encodeURIComponent(learnSid) +
      "/step/" + learnStepN + "/schematic");
    const v = await r.json();
    if (!r.ok) {
      $(wiresId).textContent = "Schematic unavailable: " +
        ((v && v.detail) || "unknown error");
      return;
    }
    const net = v.netlist || {};
    state.svg = v.svg || "";
    state.wires = net.wires || [];
    state.n = v.n || learnStepN;
    state.note = ((net.notes || [])[0]) || "";
    $(svgId).innerHTML = state.svg;
    $(wiresId).textContent = wireListText(net.wires, v.unresolved, net);
  } catch (e) {
    $(wiresId).textContent = "Schematic failed: " + e.message;
  } finally {
    schematicBusy = false;
  }
}

function refreshSchematics() {
  if (!learnSid) return;
  if (cookMode) fetchSchematic("cook-schematic-svg", "cook-schematic-wires", cookSchematicState);
  else fetchSchematic("schematic-svg", "schematic-wires", schematicState);
  refreshFlashPorts();
}

function downloadSchematic(state) {
  if (!state.svg) return;
  const blob = new Blob([state.svg], { type: "image/svg+xml" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "step-" + (state.n || learnStepN) + "-schematic.svg";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

$("schematic-download").onclick = () => downloadSchematic(schematicState);
$("cook-schematic-download").onclick = () => downloadSchematic(cookSchematicState);
$("schematic-speak").onclick = () => {
  if (schematicState.wires.length) speak(wireSpeechText(schematicState.wires, schematicState.n));
  else if (schematicState.note) speak(schematicState.note);
};
$("cook-schematic-speak").onclick = () => {
  if (cookSchematicState.wires.length) cookSpeak(wireSpeechText(cookSchematicState.wires, cookSchematicState.n));
  else if (cookSchematicState.note) cookSpeak(cookSchematicState.note);
};

// One-click ESP32 flash (Learning mode only, explicit tap only). The
// port is user-picked from the dropdown — never automatic. Busy guard
// + elapsed ticker while esptool runs; ack speech at tap, verdict
// speech when it lands; busy-port guidance stays on screen.
let flashBusy = false;

async function refreshFlashPorts() {
  if (flashBusy) return;
  let ports = [], current = "";
  try {
    const r = await fetch("/api/serial/ports");
    const v = await r.json();
    ports = (v.ports || []).map((p) => p.port);
    current = (v.current || {}).port || "";
  } catch (e) { /* dropdown keeps its last options when offline */ }
  if (current && ports.indexOf(current) < 0) ports.unshift(current);
  if (!ports.length) ports = [current || "COM3"];
  ["flash-port", "cook-flash-port"].forEach((id) => {
    const sel = $(id);
    if (!sel) return;
    const keep = sel.value || current;
    sel.innerHTML = "";
    ports.forEach((p) => {
      const o = document.createElement("option");
      o.value = p;
      o.textContent = p;
      if (p === keep) o.selected = true;
      sel.appendChild(o);
    });
  });
}

async function flashStep(portId, statusId) {
  if (flashBusy || !learnSid) return;
  const port = ($(portId) && $(portId).value) || "";
  if (!port) {
    $(statusId).textContent = "Pick a serial port first, then FLASH ESP32.";
    return;
  }
  flashBusy = true;
  ["flash-esp32", "cook-flash-esp32"].forEach((id) => {
    if ($(id)) $(id).disabled = true;
  });
  const say = cookMode ? cookSpeak : speak;
  say("Flashing step " + learnStepN + " to ESP32 on " + port + ".");
  const t0 = Date.now();
  $(statusId).textContent = "Flashing step " + learnStepN + " to " + port + "…";
  const tick = setInterval(() => {
    $(statusId).textContent = "Flashing step " + learnStepN + " to " + port +
      "… " + Math.round((Date.now() - t0) / 1000) + "s";
  }, 1000);
  try {
    const r = await fetch("/project/" + encodeURIComponent(learnSid) +
      "/step/" + learnStepN + "/flash", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ port }),
    });
    const v = await r.json();
    if (!r.ok) {
      const detail = (v && v.detail) || "flash failed";
      $(statusId).textContent = detail;
      say(detail.indexOf("busy or blocked") >= 0
        ? "Flash failed: the port is busy. Close the Serial Monitor and try again."
        : "Flash failed. See the screen for the error.");
      return;
    }
    const ping = (v.verify_ping && v.verify_ping.answered)
      ? "Board answered PING."
      : "Board silent after flash (step sketches do not answer PING) — press CHECK NOW.";
    $(statusId).textContent = "Flashed step " + v.n + " to " + v.port +
      " (" + (v.cached ? "cached build " : "build ") + (v.build_hash || "") + "). " + ping;
    say("Flash complete. " + ping);
  } catch (e) {
    $(statusId).textContent = "Flash failed: " + e.message;
    say("Flash failed. " + e.message);
  } finally {
    clearInterval(tick);
    flashBusy = false;
    ["flash-esp32", "cook-flash-esp32"].forEach((id) => {
      if ($(id)) $(id).disabled = false;
    });
  }
}

$("flash-esp32").onclick = () => flashStep("flash-port", "flash-status");
$("cook-flash-esp32").onclick = () => flashStep("cook-flash-port", "cook-flash-status");

// Hands-free voice loop: tap PUSH TO TALK to record (MediaRecorder), tap
// again to stop, upload to /api/voice/transcribe, show the transcript, and
// auto-run one loop turn when it contains done / fixed / moved it.
const TRIGGER_WORDS = ["done", "fixed", "moved it"];
let pttRec = null, pttChunks = [], pttRecording = false;

async function transcribeUpload(blob) {
  Orb.thinkStart();
  try {
    const fd = new FormData();
    fd.append("file", blob, "talk.webm");
    return (await fetch("/api/voice/transcribe", { method: "POST", body: fd })).json();
  } finally {
    Orb.thinkEnd();
  }
}

// Browser-native speech recognition first: instant, on-device, no server
// round trip. Returns true when it takes over; silence reports heard-nothing
// through onEmpty (no server cost), other failures hand off to onFallback
// (the recorder+upload path). First handler to fire wins.
let voiceListening = false; // a browser recognition turn is active
let activeRec = null; // the live recognizer, so a tap can stop it
let voiceCancelFallback = false; // manual stop: reset UI, stay quiet

function startBrowserRecognition(opts) {
  const Ctor = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Ctor) return false;
  let rec;
  try {
    rec = new Ctor();
  } catch (e) { return false; }
  opts.btn.classList.add("rec");
  opts.btn.textContent = opts.activeText;
  opts.hintEl.textContent = opts.hintListening;
  voiceListening = true;
  activeRec = rec;
  Orb.listenStart();
  let done = false;
  const finish = (fn, isFallback) => {
    if (done) return;
    done = true;
    Orb.listenEnd();
    const mine = (activeRec === rec);
    if (mine) {
      voiceListening = false;
      activeRec = null;
      opts.btn.classList.remove("rec");
      opts.btn.textContent = opts.idleText;
    }
    try { rec.abort(); } catch (e) { /* already ended */ }
    if (isFallback && voiceCancelFallback) {
      voiceCancelFallback = false;
      return;
    }
    voiceCancelFallback = false;
    fn();
  };
  rec.lang = "en-US";
  rec.interimResults = false;
  rec.maxAlternatives = 1;
  rec.onresult = (e) => {
    const text = (((e.results || [])[0] || [])[0] || {}).transcript || "";
    finish(() => opts.onText(text.trim()), false);
  };
  rec.onerror = (e) => {
    if ((e.error || "") === "no-speech" && opts.onEmpty) {
      finish(opts.onEmpty, false);
    } else {
      finish(opts.onFallback, true);
    }
  };
  rec.onend = () => finish(opts.onFallback, true);
  try {
    rec.start();
  } catch (e) {
    finish(opts.onFallback, true);
  }
  return true;
}

function handlePttTranscript(text, tag, error) {
  if (text) lastTranscript = text;
  if (!text) {
    $("talk-hint").textContent = "Transcription empty" + (error ? ": " + error : "") + " — use CHECK NOW.";
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
}

function startPttRecorder(btn) {
  if (!("MediaRecorder" in window) || !(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
    $("talk-hint").textContent = "Audio capture unavailable — use CHECK NOW.";
    return;
  }
  navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
    pttChunks = [];
    pttRec = new MediaRecorder(stream);
    pttRec.ondataavailable = (e) => { if (e.data && e.data.size) pttChunks.push(e.data); };
    pttRec.onstop = async () => {
      Orb.listenEnd();
      pttRecording = false;
      btn.classList.remove("rec");
      btn.textContent = "PUSH TO TALK";
      stream.getTracks().forEach((t) => t.stop());
      $("talk-hint").textContent = "Transcribing…";
      try {
        const t = await transcribeUpload(new Blob(pttChunks, { type: "audio/webm" }));
        const text = (t.text || "").trim();
        const tag = t.note ? " (" + t.note + ")" : "";
        handlePttTranscript(text, tag, t.error || "");
      } catch (e) { $("talk-hint").textContent = "Transcription failed — use CHECK NOW."; }
    };
    pttRec.start();
    Orb.listenStart();
    pttRecording = true;
    btn.classList.add("rec");
    btn.textContent = "LISTENING — TAP TO STOP";
  }).catch(() => { $("talk-hint").textContent = "Microphone blocked — use CHECK NOW."; });
}

function pttToggle(btn) {
  // Recording stays available mid-turn (transcribe is independent of the
  // sense loop); a trigger heard while busy queues instead of doubling.
  if (suggestListening) {
    // Bail out of project selection into a normal talk turn.
    suggestListening = false;
    if (activeRec) { voiceCancelFallback = true; try { activeRec.stop(); } catch (e) {} }
  } else if (activeRec) {
    voiceCancelFallback = true;
    try { activeRec.stop(); } catch (e) { /* already stopped */ }
    return;
  }
  if (pttRecording) { try { pttRec.stop(); } catch (e) { /* onstop resets */ } return; }
  VoiceEngine.stop(); // never capture while speaking; the tap owns speech now
  if (startBrowserRecognition({
    btn: btn, activeText: "LISTENING…", idleText: "PUSH TO TALK",
    hintEl: $("talk-hint"), hintListening: "Listening… (browser voice)",
    onText: (text) => handlePttTranscript(text, " (browser voice)", ""),
    onEmpty: () => handlePttTranscript("", "", ""),
    onFallback: () => startPttRecorder(btn),
  })) return;
  startPttRecorder(btn);
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
let cookMode = false, cookView = null;
let cookRec = null, cookChunks = [], cookRecording = false;
const COOK_LISTEN_MS = 6000;

// Heartbeat: while Cook Mode sits open, a 60s timer reports idleness
// and the backend decides whether to nudge (threshold, quiet hours,
// cooldown). Dismiss snoozes locally for 30 minutes.
const HEARTBEAT_MS = 60000;
const NUDGE_SNOOZE_MS = 30 * 60 * 1000;
let cookLastActive = Date.now();
let cookNudgeSnoozeUntil = 0;

function markCookActive() { cookLastActive = Date.now(); }

// One coach conversation per page load: the brain remembers earlier
// turns under this id (a reload forgets, until Tier 4 persists).
let coachPending = null; // gated tool awaiting CONFIRM/DENY
let cookKilled = false; // kill switch state (synced from the backend)

let cookSessionId = "";
function coachSessionId() {
  if (!cookSessionId) {
    cookSessionId = "cook-" + Date.now().toString(36) + "-" +
      Math.random().toString(36).slice(2, 10);
  }
  return cookSessionId;
}

let cookSilentStreak = 0; // consecutive heard-nothings; caps the loop

function pumpHandsfree(tries) {
  // Hands-free continuation with retries: a busy moment or a dropped
  // speech-end event must not kill "keep listening" silently.
  if (!cookMode || muted || !$("cook-handsfree").checked) return;
  if (busy || cookRecording || voiceListening) {
    if ((tries || 0) > 0) setTimeout(() => pumpHandsfree(tries - 1), 1500);
    return;
  }
  cookListen();
}

function cookSpeak(text) {
  speak(text, () => {
    if (cookMode && !muted && $("cook-handsfree").checked) pumpHandsfree(3);
  });
}

async function heartbeatNudge() {
  // Quiet by default: skip while busy, listening, or snoozed; the
  // backend applies the idle threshold, quiet hours, and cooldown.
  if (!cookMode || cookKilled) return;
  if (busy || cookRecording || voiceListening) return;
  if (Date.now() < cookNudgeSnoozeUntil) return;
  const idleSeconds = Math.round((Date.now() - cookLastActive) / 1000);
  let v = null;
  try {
    const r = await fetch("/api/coach/nudge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: coachSessionId(), idle_seconds: idleSeconds,
        step_label: $("cook-step").textContent || "", step_text: lastInstruction }),
    });
    if (!r.ok) return;
    v = await r.json();
  } catch (e) { return; } // heartbeat failures stay silent
  if (!v || !v.nudge || !v.nudge.text || !cookMode) return;
  $("cook-nudge-text").textContent = v.nudge.text;
  $("cook-nudge").hidden = false;
  lastSpeak = v.nudge.text;
  cookSpeak(v.nudge.text);
}

function renderCook(v) {
  paintConfirm(v);
  paintFundi(v.fundi);
  cookView = v;
  if (v.id) learnSid = v.id;
  learnStepN = (v.step_index || 0) + 1;
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
  if (v.step_code && v.step_code.code) {
    $("cook-code-out").textContent = v.step_code.code;
    const cports = String(ver.board_state || "").indexOf("silent:") === 0
      ? String(ver.board_state).slice(7) : "";
    $("cook-heard").textContent = "Step " + v.step_code.n + " sketch — copy it into Arduino IDE, Upload" +
      (cports ? " to " + cports : "") + ", close Serial Monitor, then press CHECK NOW.";
  }
  refreshSchematics();
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

// Blocking coach turn: the fallback when streaming is unavailable.
// Same brain (memory + tools), one reply, spoken after it lands.
async function cookAsk(text) {
  let r;
  try {
    r = await fetch("/api/coach/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: coachSessionId(), text }),
    });
  } catch (e) { return { type: "error", detail: "coach unreachable" }; }
  let v = null;
  try { v = await r.json(); } catch (e) { /* non-JSON failure below */ }
  if (!r.ok || !v) {
    return { type: "error", detail: (v && v.detail) || "coach failed" };
  }
  if (v.confirmation_pending) return { pending: v.confirmation_pending };
  if (typeof v.reply !== "string") {
    return { type: "error", detail: "coach failed" };
  }
  return { speak: v.reply };
}

// Streamed coach answer: tokens paint live, clauses speak as they land.
// Returns {speak} on done, {type:"error",detail} on model error, or null
// when streaming itself is unavailable (caller falls back to cookAsk).
// The brain thinks for seconds before the first token, so a slow ack
// claims the gap; the stream starts lazily on first content so the ack
// can never kill it.
async function cookAskStream(text) {
  let r;
  try {
    r = await fetch("/api/coach/ask/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: coachSessionId(), text }),
    });
  } catch (e) { return null; }
  if (!r.ok || !r.body || !r.body.getReader) return null;
  let id = null;
  const ensureStream = () => {
    if (id === null) id = VoiceEngine.streamStart();
    return id;
  };
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = "", pending = "", full = "", gotText = false, finished = null;
  const ackTimer = setTimeout(() => {
    if (!gotText && !finished) cookSpeak("On it — thinking…");
  }, 1500);
  const paint = () => { $("cook-heard").textContent = 'coach: "' + full + '"'; };
  const emit = (flush) => {
    if (id === null) return;
    const cut = cutClauses(pending, flush);
    pending = cut.rest;
    for (const c of cut.clauses) VoiceEngine.streamClause(id, c);
  };
  const settle = (res) => {
    clearTimeout(ackTimer);
    try { reader.cancel(); } catch (e) { /* already closed */ }
    if (!gotText) return res; // caller speaks the full text itself
    emit(true);
    return finishStream(id, res);
  };
  try {
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
        if (name === "token") {
          const t = (JSON.parse(data) || {}).text || "";
          if (t) { gotText = true; full += t; pending += t; ensureStream(); paint(); emit(false); }
        } else if (name === "tool_call") {
          const d = JSON.parse(data) || {};
          $("cook-heard").textContent = "[looking up " + (d.name || "bench notes") + "…]";
        } else if (name === "confirmation_pending") {
          finished = { pending: JSON.parse(data) || {} };
        } else if (name === "done") {
          const d = JSON.parse(data) || {};
          if (d.session_id) cookSessionId = d.session_id;
          finished = { speak: d.reply || d.text || full };
        } else if (name === "error") {
          const detail = (JSON.parse(data) || {}).detail || "answer failed";
          finished = gotText ? { speak: full } : { type: "error", detail };
        }
        if (finished) break;
      }
      if (finished) return settle(finished);
      if (done) break;
    }
  } catch (e) { /* transport died mid-stream: partial stands, else fall back */ }
  try { reader.cancel(); } catch (e) { /* already closed */ }
  clearTimeout(ackTimer);
  if (!gotText) return null;
  emit(true);
  return finishStream(id, { speak: full });
}

function finishStream(id, res) {
  // Hands-free resumes after the streamed clauses finish sounding.
  return new Promise((resolve) => {
    VoiceEngine.streamEnd(id, () => {
      if (cookMode && !muted && $("cook-handsfree").checked) pumpHandsfree(3);
      resolve(Object.assign({}, res, { streamed: true }));
    });
  });
}

async function handleCookTranscript(text, error) {
  if (!cookMode) return;
  if (!text) {
    $("cook-heard").textContent = "Heard nothing" + (error ? ": " + error : "") + ".";
    cookSilentStreak += 1;
    if (cookSilentStreak < 3) pumpHandsfree(1); // brief silence keeps listening
    return;
  }
  cookSilentStreak = 0;
  lastTranscript = text;
  markCookActive();
  $("cook-heard").textContent = 'heard: "' + text + '"';
  await cookCommand(text);
}

function startCookRecorder(btn) {
  navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
    if (!cookMode) { stream.getTracks().forEach((t) => t.stop()); return; }
    cookChunks = [];
    cookRec = new MediaRecorder(stream);
    cookRec.ondataavailable = (e) => { if (e.data && e.data.size) cookChunks.push(e.data); };
    cookRec.onstop = async () => {
      Orb.listenEnd();
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
        handleCookTranscript(text, t.error || "");
      } catch (e) { $("cook-heard").textContent = "Transcription failed."; }
    };
    cookRec.start();
    Orb.listenStart();
    cookRecording = true;
    btn.classList.add("rec");
    btn.textContent = "LISTENING…";
    $("cook-heard").textContent = "Listening…";
    setTimeout(() => { if (cookRecording && cookRec) { try { cookRec.stop(); } catch (e) {} } }, COOK_LISTEN_MS);
  }).catch(() => { $("cook-heard").textContent = "Microphone blocked."; });
}

function cookListen() {
  const btn = $("cook-talk");
  if (activeRec) {
    voiceCancelFallback = true;
    try { activeRec.stop(); } catch (e) { /* already stopped */ }
    return;
  }
  if (cookRecording) { try { cookRec.stop(); } catch (e) { /* already stopped */ } return; }
  if (!cookMode || busy) return;
  VoiceEngine.stop(); // never capture while speaking; the tap owns speech now
  if (startBrowserRecognition({
    btn: btn, activeText: "LISTENING…", idleText: "TALK",
    hintEl: $("cook-heard"), hintListening: "Listening… (browser voice)",
    onText: (text) => handleCookTranscript(text, ""),
    onEmpty: () => handleCookTranscript("", ""),
    onFallback: () => startCookRecorder(btn),
  })) return;
  if (!("MediaRecorder" in window) || !(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
    $("cook-heard").textContent = "Audio capture unavailable — use CHECK.";
    return;
  }
  startCookRecorder(btn);
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
  if (cookKilled) {
    $("cook-heard").textContent = "Coach is stopped — press ARM to resume.";
    return;
  }
  Orb.thinkStart();
  let res;
  try {
    res = (await cookAskStream(text)) || await cookAsk(text);
  } finally {
    Orb.thinkEnd();
  }
  if (!cookMode) return;
  if (res.type === "error") {
    Orb.error();
    $("cook-heard").textContent = "Coach: " + (res.detail || "unavailable");
    cookSpeak("Sorry, the coach is unavailable right now.");
    return;
  }
  if (res.pending) { showCoachConfirm(res.pending); return; }
  $("cook-heard").textContent = 'coach: "' + (res.speak || "") + '"';
  if (res.speak) lastSpeak = res.speak;
  if (!res.streamed) cookSpeak(res.speak || ""); // streamed speech already sounded
}

// Gated tool: the brain paused for explicit CONFIRM/DENY. The reply
// speaks only after the technician resolves it.
function showCoachConfirm(pending) {
  coachPending = pending;
  $("cook-coach-confirm-text").textContent =
    (pending.question || "Confirm?") + " (" + (pending.tool || "") + ")";
  $("cook-coach-confirm").hidden = false;
}

async function sendCoachConfirm(confirmed) {
  if (!coachPending || !cookMode) return;
  const callId = coachPending.call_id;
  coachPending = null;
  $("cook-coach-confirm").hidden = true;
  let v = null;
  try {
    const r = await fetch("/api/coach/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: coachSessionId(),
        call_id: callId, confirmed: confirmed }),
    });
    v = await r.json();
    if (!r.ok || !v) {
      $("cook-heard").textContent = "Confirm failed: " + ((v && v.detail) || "");
      return;
    }
  } catch (e) {
    $("cook-heard").textContent = "Confirm failed: " + e.message;
    return;
  }
  if (!cookMode) return;
  if (v.confirmation_pending) { showCoachConfirm(v.confirmation_pending); return; }
  $("cook-heard").textContent = 'coach: "' + (v.reply || "") + '"';
  if (v.reply) lastSpeak = v.reply;
  cookSpeak(v.reply || "");
}

function cookCheck() {
  if (!cookMode || busy) return;
  markCookActive();
  checkNow("");
}

async function cookLook() {
  if (!cookMode || busy) return;
  markCookActive();
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
    const r = await fetch("/project/" + encodeURIComponent(learnSid) + "/code", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft: true }),
    });
    const v = await r.json();
    if (!r.ok) {
      $("cook-heard").textContent = ((v && v.detail) || "code not ready") + " — SEND CODE retries.";
      cookSpeak("Code is not ready yet. Press Send Code to retry.");
      return;
    }
    $("cook-code-out").textContent = v.code || "";
    codeFetchedSid = learnSid;
    const progress = (v.steps_passed || 0) + " of " + (v.steps_total || 0) + " steps verified";
    $("cook-heard").textContent = v.verified ? "Final code below." : "Draft code below (" + progress + ").";
    if (auto) return;
    cookSpeak(v.verified ? "Code ready. It is shown on screen." :
      "Here's your draft code, " + progress + " so far.");
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
  $("learn-sec").hidden = true; // one focused screen while cooking
  $("cook-heard").textContent = "";
  $("cook-svg").innerHTML = "";
  $("cook-nudge").hidden = true;
  $("cook-coach-confirm").hidden = true;
  coachPending = null;
  markCookActive();
  renderCook(v);
  refreshCamera();
  syncKillState();
  window.scrollTo(0, 0);
  cookSpeak(v.speak || v.instruction || "");
}

function exitCookMode() {
  cookMode = false;
  $("cook-nudge").hidden = true;
  $("cook-coach-confirm").hidden = true;
  $("cook-killed").hidden = true;
  coachPending = null;
  if (cookRecording && cookRec) { try { cookRec.stop(); } catch (e) { /* already stopped */ } }
  cookRecording = false;
  VoiceEngine.stop();
  $("cook-talk").classList.remove("rec");
  $("cook-talk").textContent = "TALK";
  $("cook").hidden = true;
  $("learn-sec").hidden = false;
  if (cookView) renderLearning(cookView);
}

function setKilled(killed) {
  cookKilled = killed;
  if (!cookMode) return;
  $("cook-killed").hidden = !killed;
  if (killed) {
    $("cook-nudge").hidden = true;
    $("cook-coach-confirm").hidden = true;
    coachPending = null;
  }
}

async function syncKillState() {
  try {
    const r = await fetch("/api/coach/killswitch");
    if (!r.ok) return;
    setKilled(!!(await r.json()).tripped);
  } catch (e) { /* keep last state when offline */ }
}

async function flipKillswitch(tripped) {
  try {
    const r = await fetch("/api/coach/killswitch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tripped: tripped }),
    });
    if (!r.ok) { $("cook-heard").textContent = "Kill switch failed."; return; }
    setKilled(!!(await r.json()).tripped);
    if (tripped) VoiceEngine.stop();
  } catch (e) { $("cook-heard").textContent = "Kill switch failed."; }
}

$("cook-talk").onclick = () => { markCookActive(); cookListen(); };
$("cook-coach-yes").onclick = () => sendCoachConfirm(true);
$("cook-coach-no").onclick = () => sendCoachConfirm(false);
$("cook-stop").onclick = () => flipKillswitch(true);
$("cook-arm").onclick = () => flipKillswitch(false);
$("cook-nudge-x").onclick = () => {
  $("cook-nudge").hidden = true;
  cookNudgeSnoozeUntil = Date.now() + NUDGE_SNOOZE_MS;
  VoiceEngine.stop(); // stop the nudge speech too
};

// Typed debug path: the same brain, without the microphone. Commands
// (check, look, exit) and free questions all work as if spoken.
async function cookAskTyped() {
  if (!cookMode || busy) return;
  const input = $("cook-ask-input");
  const text = (input.value || "").trim();
  if (!text) return;
  input.value = "";
  lastTranscript = text;
  markCookActive();
  $("cook-heard").textContent = 'asked: "' + text + '"';
  await cookCommand(text);
}
$("cook-ask-send").onclick = () => cookAskTyped();
$("cook-ask-input").onkeydown = (e) => {
  if (e.key === "Enter") { e.preventDefault(); cookAskTyped(); }
};
$("cook-check").onclick = () => cookCheck();
$("cook-repeat").onclick = () => { markCookActive(); cookSpeak(lastSpeak || (cookView && cookView.instruction) || ""); };
$("cook-look").onclick = () => cookLook();
$("cook-exit").onclick = () => exitCookMode();
$("cook-code").onclick = () => cookSendCode();
$("cook-step-code").onclick = () => fetchStepCode("cook-code-out", "cook-heard");
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

function paintSerialMsg(cur) {
  const err = (cur && cur.last_error) || "";
  $("serial-msg").textContent =
    "serial: " + ((cur && cur.port) || "?") + " @ " + ((cur && cur.baud) || "?") +
    (cur && cur.mock ? " (mock)" : "") +
    (err ? " — last error: " + err : " — no errors yet");
}

async function refreshSerialPorts() {
  try {
    const r = await fetch("/api/serial/ports");
    const v = await r.json();
    const sel = $("serial-port");
    sel.innerHTML = "";
    const ports = (v.ports || []).map((p) => p.port);
    const cur = (v.current || {}).port || "";
    if (cur && ports.indexOf(cur) < 0) ports.unshift(cur);
    if (!ports.length) ports.push(cur || "COM3");
    ports.forEach((p) => {
      const o = document.createElement("option");
      o.value = p;
      o.textContent = p;
      if (p === cur) o.selected = true;
      sel.appendChild(o);
    });
    const baud = String((v.current || {}).baud || "115200");
    const bsel = $("serial-baud");
    if (baud !== "115200" && baud !== "9600") {
      const o = document.createElement("option");
      o.value = baud;
      o.textContent = baud;
      bsel.appendChild(o);
    }
    bsel.value = baud;
    paintSerialMsg(v.current || {});
  } catch (e) { $("serial-msg").textContent = "Serial list unavailable."; }
}

async function fetchSketch(auto) {
  if ($("sketch-code").textContent) { $("sketch-panel").hidden = false; return; }
  if (!auto) $("serial-msg").textContent = "Loading sketch…";
  try {
    const r = await fetch("/api/patient-sketch");
    const v = await r.json();
    if (!r.ok) { if (!auto) $("serial-msg").textContent = "Sketch unavailable."; return; }
    $("sketch-code").textContent = v.code || "";
    $("sketch-panel").hidden = false;
    if (!auto) $("serial-msg").textContent = "Sketch ready — copy it into Arduino IDE.";
  } catch (e) { if (!auto) $("serial-msg").textContent = "Sketch failed: " + e.message; }
}
$("show-sketch").onclick = () => fetchSketch(false);
$("copy-sketch").onclick = () => copyCode("sketch-code", "serial-msg");
$("save-serial").onclick = async () => {
  $("serial-msg").textContent = "Saving…";
  try {
    const r = await fetch("/api/config/serial", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ port: $("serial-port").value, baud: $("serial-baud").value }),
    });
    const v = await r.json();
    paintSerialMsg(v.serial_live || {});
  } catch (e) { $("serial-msg").textContent = "Save failed: " + e.message; }
};

$("serial-detect").onclick = async () => {
  if (busy) return;
  busy = true;
  const t0 = Date.now();
  $("serial-msg").textContent = "Probing ports…";
  const tick = setInterval(() => {
    $("serial-msg").textContent = "Probing ports… " + Math.round((Date.now() - t0) / 1000) + "s";
  }, 1000);
  try {
    const r = await fetch("/api/serial/autodetect", { method: "POST" });
    const v = await r.json();
    if (v.found) {
      await refreshSerialPorts();
      $("serial-msg").textContent = "Found board on " + v.serial_live.port + " @ " + v.serial_live.baud + ".";
      speak("Found your board.");
    } else {
      paintSerialMsg(v.serial_live || {});
      $("serial-msg").textContent += " No patient board answered — check the USB cable, drivers, and that the board is plugged in.";
    }
  } catch (e) { $("serial-msg").textContent = "Auto-detect failed: " + e.message; }
  finally { clearInterval(tick); busy = false; }
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
$("settings-btn").onclick = () => { $("settings-modal").hidden = false; };
$("settings-close").onclick = () => { $("settings-modal").hidden = true; };
$("settings-modal").addEventListener("click", (e) => {
  if (e.target === $("settings-modal")) $("settings-modal").hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("settings-modal").hidden = true;
});

// Guided typed path: the same ears as PUSH TO TALK, without a mic.
async function guideAskTyped() {
  const input = $("guide-ask-input");
  const text = (input.value || "").trim();
  if (!text) return;
  input.value = "";
  handlePttTranscript(text, " (typed)", "");
}
$("guide-ask-send").onclick = () => guideAskTyped();
$("guide-ask-input").onkeydown = (e) => {
  if (e.key === "Enter") { e.preventDefault(); guideAskTyped(); }
};

$("mute").onclick = () => {
  muted = !muted;
  try { localStorage.setItem("fundiloop-muted", muted ? "1" : "0"); } catch (e) { /* private mode */ }
  if (muted) VoiceEngine.stop();
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
  refreshVoiceStatus();
  speak(lastSpeak || "Voice updated. This is how I will sound.");
};
$("test-voice").onclick = () => {
  refreshVoiceStatus();
  // Full path: Meta Voice TTS first, browser fallback on failure.
  speak("Fundi voice check. Wire ground to ground, then press check.");
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
refreshSerialPorts();
setInterval(refreshCamera, 3000);
setInterval(refreshBadge, 5000);
setInterval(heartbeatNudge, HEARTBEAT_MS);
