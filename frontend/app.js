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
let speechClockStart = 0; // when the current utterance actually starts sounding
let speechClockMs = 0; // estimated duration of that utterance
let speechClockSeq = 0; // speakSeq that owns the clock
let speechAudible = false; // true only after audio/voice has begun
let parkedDraw = null; // schematic waiting for the tutor's voice

function estimateSpeechMs(text) {
  // Playback is ~15 chars/s (see splitSpeakChunks); pad for the first
  // chunk's synthesis so wires do not finish before the tutor does.
  const n = speakable(text || "").length;
  return Math.max(2200, Math.round((n / 13) * 1000) + 800);
}

let speechLeadFrac = 0; // fraction of the utterance before wires are named
let speechChunkHook = null; // called with each spoken chunk as it sounds

function armSpeechClock(text, seq) {
  speechClockSeq = seq;
  speechClockStart = 0;
  speechArmedAt = Date.now();
  speechClockMs = estimateSpeechMs(text);
  speechAudible = false;
  // Where does this utterance first name a wire? The schematic draw
  // waits out that preamble (title, parts list) instead of animating
  // connections while the tutor is still introducing the step.
  const raw = String(text || "");
  const hit = raw.search(/GPIO|SDA|SCL|GND|ground|connect|wire|3V3|\bpin\b/i);
  speechLeadFrac = (hit > 0 && raw.length) ? hit / raw.length : 0;
}

function markSpeechEnded() {
  // The current utterance finished: the next step's draw must not
  // mistake this stale flag for speech already sounding.
  if (speechClockSeq === speakSeq) speechAudible = false;
  flushFollowDraw(); // wires the narration never named still land
}

function speechRemainMs() {
  if (muted || !speechAudible || !speechClockMs || speechClockSeq !== speakSeq) return 0;
  return Math.max(0, speechClockMs - (Date.now() - speechClockStart));
}

let speechArmedAt = 0; // when the current utterance was armed (fetch gap)
function speechActive() {
  // True while the tutor owns the audio: sounding now, or still in the
  // synthesis/fetch gap before first audio. Bounded, so a hung fetch
  // never silences the loop forever; muted never blocks.
  if (muted || !speechClockMs || speechClockSeq !== speakSeq) return false;
  if (speechAudible) return speechRemainMs() > 0;
  return Date.now() - speechArmedAt < 4000;
}

function markSpeechAudible(durationMs) {
  if (speechClockSeq !== speakSeq) return;
  if (durationMs && durationMs > speechClockMs) speechClockMs = durationMs;
  if (speechAudible) return;
  speechAudible = true;
  speechClockStart = Date.now();
  runParkedSchematicDraw();
}

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

// speech:fetch:start
async function fetchWithTimeout(url, opts, ms) {
  // TTS fetch with a hard bound: a hung synthesis must fall back to
  // browser speech, never lose the utterance silently.
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  try {
    return await fetch(url, Object.assign({}, opts, { signal: ctrl.signal }));
  } finally {
    clearTimeout(timer);
  }
}
// speech:fetch:end

// One Meta TTS fetch: object URL on playable audio, null on any failure
// (the caller falls back to browser speech for that utterance only).
async function fetchServerSpeech(text) {
  // One immediate retry: model-file reads can fail transiently
  // (a download completing, cloud hydration) and heal within ms.
  // Persistent failures still fall back to browser speech fast.
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const r = await fetchWithTimeout("/api/voice/speak", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      }, 10000);
      if (!r.ok) {
        // Surface the server's reason in the console; the utterance
        // still falls back to browser speech if the retry also fails.
        try {
          const v = await r.json();
          if (v && v.detail) console.log("voice speak failed:", v.detail);
        } catch (e) { /* no body to report */ }
        continue;
      }
      const blob = await r.blob();
      if (!blob || !blob.size) return null;
      return URL.createObjectURL(blob);
    } catch (e) { /* network down: retry once, then null */ }
  }
  return null;
}

// Prime the server voice while the user reads: the first cold
// synthesis costs ~10s (model load + first inference), which races
// the chunk timeout and drops the opening narration to browser
// speech. Called at first intent (suggest, card tap); the audio is
// discarded, the warm engine is the point. Repeats are cache hits.
// Never throws, never blocks speech.
function warmServerVoice() {
  try {
    fetchWithTimeout("/api/voice/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: "Ready." }),
    }, 30000).then((r) => {
      if (r && r.ok && r.blob) r.blob().catch(() => null);
    }).catch(() => null);
  } catch (e) { /* warmup must never break speech */ }
}

// Prime on page boot too: fast tappers outrun the suggest/card primes
// (cold load is ~10s), but nobody outruns the load itself. Repeats
// are server cache hits; failures stay silent by design.
warmServerVoice();

function speakBrowserNow(say, done) {
  // Default browser speech: any fallback voice, never refused.
  const finish = () => { if (done) { try { done(); } catch (e) { console.log("speak onend failed", e); } } };
  // Mute is total: a leaking ack masks the muted state (acks audible,
  // narration silent) instead of staying quiet like everything else.
  if (muted) { finish(); return; }
  try {
    if (!("speechSynthesis" in window)) { finish(); return; }
    // Cancel only when something is actually queued: a bare cancel()
    // in the same tick as speak() wedges Chrome's queue and the
    // utterance never sounds (text shown, quiet kept).
    if (speechSynthesis.speaking || speechSynthesis.pending) {
      speechSynthesis.cancel();
    }
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
      u.onboundary = (e) => {
        OrbWave.blip(0.55);
        // Feed the text spoken-so-far to the wire-draw hook so each wire
        // lights up as the tutor actually names it (word by word), not
        // all at once when the chunk starts. charIndex is the cursor
        // into this utterance's text; charLength covers the current word.
        if (speechChunkHook && e && Number.isFinite(e.charIndex)) {
          try {
            const upto = e.charIndex + (e.charLength || 0);
            speechChunkHook(p.substring(0, upto));
          } catch (err) { /* draw only */ }
        }
      };
      u.onstart = () => {
        if (i === 0) markSpeechAudible(estimateSpeechMs(say));
        // No full-chunk hook here: onboundary drives word-by-word drawing.
        // Browsers without boundary events fall back to the parked-draw
        // timer schedule (runParkedSchematicDraw), so wires still land.
      };
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
    u.onboundary = () => OrbWave.blip(0.55);
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
  OrbWave.tapElement(el); // orb vibrates with the streamed clause
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
    .map((p) => p.replace(/<DOT>/g, ".").trim())
    // Drop punctuation-only pieces: they synthesize dead air that
    // vibrates the orb while nothing is heard, then speech seems to
    // "start midway" on the next chunk.
    .filter((p) => /[A-Za-z0-9]/.test(p));
  // Pack small sentences into 25-55 char chunks: server synthesis
  // runs ~50ms/char (measured), so first audio sounds after ~2s
  // instead of ~7s, and each chunk's playback (~1s per 15 chars)
  // still hides the next chunk's prefetch. Monster sentences split
  // into even word-gap pieces.
  const chunks = [];
  let cur = "";
  const pushCur = () => { if (cur) { chunks.push(cur); cur = ""; } };
  const pushMonster = (p) => {
    // Evenly spaced word-gap cuts: greedy slicing leaves a tiny tail
    // chunk whose playback can't cover the next chunk's synthesis.
    const n = Math.ceil(p.length / 55);
    let start = 0;
    for (let i = 1; i < n; i++) {
      const target = Math.round((p.length * i) / n);
      let cut = p.lastIndexOf(" ", target);
      if (cut < start + 12) cut = p.indexOf(" ", target);
      if (cut < 0 || cut <= start) break; // no usable gap: rest goes whole
      const piece = p.slice(start, cut).trim();
      if (piece) chunks.push(piece);
      start = cut;
    }
    const tail = p.slice(start).trim();
    if (tail) chunks.push(tail);
  };
  for (const p of parts) {
    if (p.length > 55) { pushCur(); pushMonster(p); continue; }
    if (cur && (cur.length >= 25 || (cur + " " + p).length > 55)) pushCur();
    cur = cur ? cur + " " + p : p;
  }
  pushCur();
  return chunks; // possibly empty: the player ends silently, no dead air
}

// speech:watchdog:start
function armPlaybackWatchdog(ms, onStall) {
  // One-shot stall guard: when audio never starts (hung play promise,
  // no events), the utterance must fall back instead of dying quiet.
  // Returns settle(): call it on any playback progress.
  let settled = false;
  const timer = setTimeout(() => {
    if (!settled) { settled = true; onStall(); }
  }, ms);
  return () => { if (!settled) { settled = true; clearTimeout(timer); } };
}
// speech:watchdog:end

// Long utterances synthesize chunk-by-chunk with the next chunk
// prefetched during playback: first audio sounds after one short
// synthesis instead of one long one. Any chunk failure speaks the
// REMAINDER via the browser fallback and stops the chain.
function playServerChunks(chunks, mySpeech, done) {
  if (!chunks.length) { done(); return; }
  // Two-deep prefetch: while chunk i plays, synthesis for i+1 AND i+2
  // is already running, so a short chunk ("You need: ...") never ends
  // before the next long one is ready — no dead air mid-speech.
  const pending = chunks.map(() => null);
  const prime = (i) => {
    if (i < chunks.length && !pending[i]) pending[i] = fetchServerSpeech(chunks[i]);
  };
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
    OrbWave.tapElement(el); // orb vibrates with this chunk
    prime(i + 1);
    prime(i + 2);
    const advance = () => {
      if (mySpeech !== speakSeq) return;
      stopServerAudio();
      if (i + 1 >= chunks.length) { done(); return; } // last chunk played
      pending[i + 1].then((nextUrl) => playUrl(nextUrl, i + 1));
    };
    const settleWatchdog = armPlaybackWatchdog(12000, () => {
      if (mySpeech === speakSeq) broken();
    });
    el.onended = () => { settleWatchdog(); advance(); };
    const restMs = estimateSpeechMs(chunks.slice(i).join(" "));
    let hooked = false;
    const noteStart = () => {
      settleWatchdog();
      const heard = (el.duration && isFinite(el.duration) && el.duration > 0)
        ? (el.duration * 1000 + estimateSpeechMs(chunks.slice(i + 1).join(" ")))
        : restMs;
      markSpeechAudible(heard);
      if (!hooked) {
        hooked = true;
        if (speechChunkHook) {
          try { speechChunkHook(chunks[i]); } catch (e) { /* draw only */ }
        }
      }
    };
    el.onplaying = noteStart;
    const broken = () => {
      settleWatchdog();
      if (mySpeech !== speakSeq) return;
      stopServerAudio();
      speakBrowserNow(chunks.slice(i).join(" "), done);
    };
    el.onerror = broken;
    // First audio is about to sound: cut stale browser speech
    // now. Earlier left quiet; later double-talks.
    try {
      if ("speechSynthesis" in window) speechSynthesis.cancel();
    } catch (e) { /* no speech */ }
    try {
      const play = el.play();
      if (play && play.then) play.then(noteStart).catch(broken);
      else if (play && play.catch) play.catch(broken);
    } catch (e) {
      broken();
    }
  };
  prime(0);
  prime(1);
  pending[0].then((url) => playUrl(url, 0));
}

const VoiceEngine = {
  // Single choke point for all spoken output. Meta Voice TTS audio is
  // primary; any server failure falls back to default browser speech
  // for that utterance only, retried fresh next turn. speak() stays a
  // thin wrapper so existing call sites are unchanged.
  speak(text, onend) {
    const done = () => {
      markSpeechEnded();
      if (onend) { try { onend(); } catch (e) { console.log("speak onend failed", e); } }
    };
    clauseStreamSeq++; // a full speak takes over: any live stream goes silent
    stopServerAudio();
    clearServerQueue();
    // Browser speech is NOT canceled here: a fallback utterance in
    // flight finishes instead of clipping, and playUrl cuts it the
    // moment server audio actually starts. Canceling here opened
    // a quiet hole; canceling late would double-talk.
    if (!text || muted) {
      // Nothing will sound: silence now.
      try {
        if ("speechSynthesis" in window) speechSynthesis.cancel();
      } catch (e) { /* no speech */ }
      speechAudible = true;
      speechClockStart = Date.now();
      runParkedSchematicDraw();
      done();
      return;
    }
    const mySpeech = ++speakSeq;
    armSpeechClock(text, mySpeech);
    // One tick of delay lets a superseding speak() land first so rapid
    // speaks never overlap into noise; a superseded speech drops silently.
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
    runParkedSchematicDraw();
  },
};

function speak(text, onend) { VoiceEngine.speak(text, onend); }

// OrbWave + MeshOrb: Trillion-style wireframe globe.
// Edges are the hero — thin teal mesh lines like Trillion's orb.
// Nodes are tiny bright dots at intersections only.
// Audio level drives: sphere scale pulse + per-node radial ripple.
// Public API unchanged: tapElement, tapStream, blip, setMode, releaseMic, level.

const OrbWave = (() => {
  // ── Audio analysers ────────────────────────────────────────────────────
  let actx = null, analyser = null, micAnalyser = null, micSource = null;
  let _mode = "idle";
  let _level = 0, blipLevel = 0;
  const scratch = new Uint8Array(512);

  function ensureCtx() {
    if (actx) return actx;
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      actx = new AC();
    } catch (e) { return null; }
    return actx;
  }

  function rmsOf(an) {
    if (!an) return -1;
    try {
      an.getByteTimeDomainData(scratch);
      let sum = 0;
      for (let i = 0; i < scratch.length; i++) {
        const v = (scratch[i] - 128) / 128;
        sum += v * v;
      }
      return Math.sqrt(sum / scratch.length);
    } catch (e) { return -1; }
  }

  // ── Globe geometry ─────────────────────────────────────────────────────
  // Fewer nodes = cleaner visible mesh like Trillion (not a dense cloud)
  const NODE_COUNT = 48;
  function fibSphere(n) {
    const pts = [], phi = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < n; i++) {
      const y = 1 - (i / (n - 1)) * 2;
      const r = Math.sqrt(Math.max(0, 1 - y * y));
      const th = phi * i;
      pts.push([Math.cos(th) * r, y, Math.sin(th) * r]);
    }
    return pts;
  }
  const BASE_NODES = fibSphere(NODE_COUNT);

  // Edges: connect nodes within chord distance — tight threshold = sparse clean mesh
  const CHORD_THRESH = 0.58;
  const EDGES = [];
  for (let i = 0; i < NODE_COUNT; i++) {
    for (let j = i + 1; j < NODE_COUNT; j++) {
      const [ax,ay,az] = BASE_NODES[i], [bx,by,bz] = BASE_NODES[j];
      const d = Math.sqrt((ax-bx)**2 + (ay-by)**2 + (az-bz)**2);
      if (d < CHORD_THRESH) EDGES.push([i, j]);
    }
  }

  // ── Colours ────────────────────────────────────────────────────────────
  // Trillion palette: teal/cyan mesh, purple glow when speaking
  const C = {
    idle:   { er:0,   eg:210, eb:180, nr:120, ng:255, nb:220, hr:30,  hg:0,   hb:120 },
    listen: { er:0,   eg:230, eb:230, nr:180, ng:255, nb:255, hr:0,   hg:80,  hb:160 },
    speak:  { er:140, eg:80,  eb:255, nr:210, ng:140, nb:255, hr:100, hg:0,   hb:200 },
    think:  { er:200, eg:160, eb:20,  nr:255, ng:220, nb:60,  hr:100, hg:50,  hb:0   },
    ok:     { er:20,  eg:220, eb:120, nr:150, ng:255, nb:180, hr:0,   hg:120, hb:60  },
    fault:  { er:255, eg:110, eb:20,  nr:255, ng:180, nb:80,  hr:160, hg:40,  hb:0   },
  };

  // ── Renderer ───────────────────────────────────────────────────────────
  let rotY = 0;
  const TILT = 0.30; // fixed X-axis tilt (radians) — tilts globe like Trillion
  const sinX = Math.sin(TILT), cosX = Math.cos(TILT);

  function drawMesh(canvas, lvl, t, state) {
    const W = canvas.width, H = canvas.height;
    if (!W || !H) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, W, H);
    const c = C[state] || C.idle;
    const cx = W / 2, cy = H / 2;

    // Globe radius — pulses strongly with audio
    const baseR = Math.min(W, H) * 0.37;
    const pulse = state === "speak"
      ? lvl * 0.45 + 0.04 * Math.sin(t / 50)
      : state === "listen"
        ? lvl * 0.28 + 0.03 * Math.sin(t / 70)
        : 0.03 * Math.sin(t / 1400);          // idle: tiny slow breath
    const R = baseR * (1 + pulse);

    // Background haze glow — grows with level
    const hazeR = Math.min(W, H) * (0.46 + lvl * 0.22);
    const hg = ctx.createRadialGradient(cx, cy, 0, cx, cy, hazeR);
    const hazeA = 0.30 + lvl * 0.40;
    hg.addColorStop(0,    `rgba(${c.hr},${c.hg},${c.hb},${hazeA.toFixed(2)})`);
    hg.addColorStop(0.55, `rgba(${c.hr},${c.hg},${c.hb},${(hazeA * 0.3).toFixed(2)})`);
    hg.addColorStop(1,    `rgba(${c.hr},${c.hg},${c.hb},0)`);
    ctx.fillStyle = hg;
    ctx.fillRect(0, 0, W, H);

    // Project all nodes — vibration: each node ripples outward with its own phase
    const sinY = Math.sin(rotY), cosY = Math.cos(rotY);
    const proj = BASE_NODES.map(([bx, by, bz], i) => {
      // Radial vibration — unique phase per node, stronger at equator
      const eq = Math.sqrt(bx * bx + bz * bz);
      const phase = i * 0.53 + t * 0.0025;
      const vibe = lvl * (state === "speak" ? 0.50 : 0.30)
                       * (0.5 + 0.5 * Math.sin(phase)) * (0.3 + 0.7 * eq);
      const x0 = bx * (1 + vibe);
      const y0 = by * (1 + vibe * 0.5);
      const z0 = bz * (1 + vibe);

      // Y rotation (spin)
      const x1 = x0 * cosY - z0 * sinY;
      const z1 = x0 * sinY + z0 * cosY;
      // X tilt (fixed)
      const y1 = y0 * cosX - z1 * sinX;
      const z2 = y0 * sinX + z1 * cosX;
      // Perspective divide
      const scale = 1 / (1 + 0.45 * (1 - z2) * 0.5);
      return { sx: cx + x1 * R * scale, sy: cy - y1 * R * scale, z: z2, sc: scale };
    });

    // ── Draw edges first (the hero element) ───────────────────────────────
    // Front edges are bright and crisp; back edges fade out
    const eBright = 1 + lvl * 1.2;
    for (const [i, j] of EDGES) {
      const a = proj[i], b = proj[j];
      const dz = (a.z + b.z) / 2;
      if (dz < -0.65) continue; // cull deep back face
      // Depth-based alpha: front = bright, back = faint but visible
      const depthFac = (dz + 1) / 2; // 0..1
      const alpha = Math.min(0.9, (0.12 + 0.75 * depthFac) * eBright);
      ctx.beginPath();
      ctx.moveTo(a.sx, a.sy);
      ctx.lineTo(b.sx, b.sy);
      ctx.strokeStyle = `rgba(${c.er},${c.eg},${c.eb},${alpha.toFixed(3)})`;
      // Front edges thicker for the Trillion crisp wireframe look
      ctx.lineWidth = depthFac > 0.55 ? 1.2 : 0.7;
      ctx.stroke();
    }

    // ── Draw nodes: small, sharp, bright — NOT big blurry blobs ──────────
    for (const p of proj) {
      if (p.z < -0.60) continue;
      const depthFac = (p.z + 1) / 2;
      const alpha = 0.35 + 0.65 * depthFac;
      // Node size: small fixed size + slight perspective scale — never huge
      const dotR = Math.max(0.9, (p.z > 0.2 ? 2.0 : 1.2) * p.sc);
      // Tight glow — only 2× dot size so it doesn't bleed into neighbours
      const glowR = Math.max(dotR + 1.5, dotR * 2.2);
      const grd = ctx.createRadialGradient(p.sx, p.sy, 0, p.sx, p.sy, glowR);
      grd.addColorStop(0,   `rgba(${c.nr},${c.ng},${c.nb},${(alpha * 0.85).toFixed(3)})`);
      grd.addColorStop(0.5, `rgba(${c.nr},${c.ng},${c.nb},${(alpha * 0.25).toFixed(3)})`);
      grd.addColorStop(1,   `rgba(${c.nr},${c.ng},${c.nb},0)`);
      ctx.beginPath();
      ctx.arc(p.sx, p.sy, glowR, 0, Math.PI * 2);
      ctx.fillStyle = grd;
      ctx.fill();
      // Solid core dot
      ctx.beginPath();
      ctx.arc(p.sx, p.sy, dotR, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${c.nr},${c.ng},${c.nb},${Math.min(1, alpha + 0.1).toFixed(3)})`;
      ctx.fill();
    }

    // Pulsing outer ring removed — it created an unwanted boundary halo
  }

  // ── Animation loop ─────────────────────────────────────────────────────
  function frame(t) {
    requestAnimationFrame(frame);
    // Target level from real audio or synthetic pulse
    let target;
    if (_mode === "speak") {
      const r = rmsOf(analyser);
      target = r >= 0 ? Math.min(1, r * 8) : 0;
      if (blipLevel > 0) target = Math.max(target, blipLevel);
      const synth = 0.5 + 0.4 * Math.abs(Math.sin(t / 80));
      target = Math.max(target, synth);
    } else if (_mode === "listen") {
      const r = rmsOf(micAnalyser);
      target = r >= 0 ? Math.min(1, r * 8) : 0;
      const synth = 0.4 + 0.4 * Math.abs(Math.sin(t / 100));
      target = Math.max(target, synth);
    } else if (_mode === "think") {
      target = 0.2 + 0.18 * Math.abs(Math.sin(t / 350));
    } else if (_mode === "fault") {
      target = 0.5 + 0.4 * Math.abs(Math.sin(t / 180));
    } else {
      target = 0.05 + 0.04 * Math.sin(t / 1400);
    }
    blipLevel *= 0.88;
    _level += (target - _level) * 0.15;

    rotY += _mode === "speak"  ? 0.016 + _level * 0.020
          : _mode === "listen" ? 0.012 + _level * 0.014
          : _mode === "think"  ? 0.007
          : _mode === "fault"  ? 0.030
          : 0.008;

    const state = _mode === "speak" ? "speak"
                : _mode === "listen" ? "listen"
                : _mode === "think" ? "think"
                : _mode === "ok" ? "ok"
                : _mode === "fault" ? "fault" : "idle";

    const c1 = document.getElementById("orb-canvas");
    const c2 = document.getElementById("cook-orb-canvas");
    if (c1) { try { drawMesh(c1, _level, t, state); } catch(e) { console.error("drawMesh c1:", e); } }
    if (c2) { try { drawMesh(c2, _level, t, state); } catch(e) { console.error("drawMesh c2:", e); } }
  }

  // ── Bootstrap ──────────────────────────────────────────────────────────
  // Start unconditionally. Reduced-motion check is best-effort only.
  (function startOrb() {
    // Always run — reduced-motion users get a slower, calmer orb rather than nothing
    requestAnimationFrame(frame);
  })();
  return {
    setMode(m) { _mode = m; },
    level() { return _level; },
    blip(v) { blipLevel = Math.max(blipLevel, v || 0.6); },
    tapElement(el) {
      const ctx = ensureCtx();
      if (!ctx || !el || el._orbTapped) return;
      try {
        const src = ctx.createMediaElementSource(el);
        const an = ctx.createAnalyser();
        an.fftSize = 512;
        src.connect(an);
        an.connect(ctx.destination);
        analyser = an;
        el._orbTapped = true;
        if (ctx.resume) try { ctx.resume().catch(()=>{}); } catch(e){}
      } catch (e) {}
    },
    tapStream(stream) {
      const ctx = ensureCtx();
      if (!ctx || !stream) return;
      this.releaseMic();
      try {
        const src = ctx.createMediaStreamSource(stream);
        const an = ctx.createAnalyser();
        an.fftSize = 512;
        src.connect(an);
        micSource = src; micAnalyser = an;
        if (ctx.resume) try { ctx.resume().catch(()=>{}); } catch(e){}
      } catch (e) { micAnalyser = null; }
    },
    releaseMic() {
      try { if (micSource) micSource.disconnect(); } catch(e){}
      micSource = null; micAnalyser = null;
    },
  };
})();

// Orb: the visible voice of the app. One state at a time across every
// .orb element (main screen + cook overlay): error > speaking >
// listening > thinking > idle. Listening/thinking are explicit counters
// (nested turns overlap); speaking is polled from live audio state.
const Orb = (() => {
  let listenDepth = 0, thinkDepth = 0, errUntil = 0, okUntil = 0;
  let faultLatched = false, monitorHealthy = false;
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
    if (faultLatched) state = "is-fault";
    else if (Date.now() < errUntil) state = "is-error";
    else if (speakingNow()) state = "is-speaking";
    else if (listenDepth > 0) state = "is-listening";
    else if (thinkDepth > 0) state = "is-thinking";
    else if (Date.now() < okUntil) state = "is-ok";
    else if (monitorHealthy) state = "is-ok";
    try {
      OrbWave.setMode(state === "is-listening" ? "listen"
        : state === "is-speaking" ? "speak"
        : state === "is-thinking" ? "think"
        : state === "is-ok" ? "ok"
        : state === "is-fault" ? "fault"
        : "idle");
    } catch (e) { /* static orb */ }
    for (const el of document.querySelectorAll(".orb")) {
      if (el._orbState === state) continue;
      el._orbState = state;
      el.classList.remove("is-listening", "is-thinking", "is-speaking",
        "is-error", "is-ok", "is-fault");
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
    ok() { okUntil = Date.now() + 2500; paint(); },
    fault(on) { faultLatched = !!on; paint(); },
    monitor(on) { monitorHealthy = !!on; paint(); },
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

// Header badge: LIVE (green) only when the real Meta models are
// connected; MOCK (red) otherwise.
function paintBadge(cfg) {
  const badge = $("mode-badge");
  const live = !!cfg && cfg.mock_meta === false &&
    cfg.sam_mode === "real" && cfg.muse_mode === "real" && cfg.meta_api_configured === true;
  badge.textContent = live ? "LIVE" : "MOCK";
  badge.classList.toggle("live", live);
  badge.classList.toggle("mock", !live);
  badge.title = live
    ? "Real Meta models connected"
    : "Not fully live: " + [
      cfg.mock_meta ? "MOCK_META=true" : null,
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
    if (v.config) paintBadge(v.config);
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

let pendingVoiceStep = ""; // voice "done" heard mid-turn, advance when free

function fmtTimings(t) {
  if (!t) return "";
  const s = (ms) => ((ms || 0) / 1000).toFixed(1) + "s";
  return "turn " + s(t.total_ms) + " (sense " + s(t.sense_ms) +
    " · sam " + s(t.sam_ms) + " · probe " + s(t.probe_ms) +
    " · reason " + s(t.reason_ms) + ")";
}

// Trillion-style status chips: render key/value pills into an element
// instead of a flat " · "-separated line, so the build state reads at a
// glance. Falsy chips are skipped. Each chip may carry a tone class
// (ok/warn/dot) and an optional label shown dim ahead of the value.
function stateChipClass(state) {
  if (state === "DONE" || state === "COMPLETE" || state === "VERIFIED_FIXED") return "ok";
  if (state === "FAULT_FOUND" || state === "VERIFYING") return "warn";
  return "";
}
function paintChips(el, chips) {
  if (!el) return;
  const parts = (chips || []).filter(Boolean).map((c) => {
    const cls = c.cls ? " " + c.cls : "";
    const label = c.label ? '<span>' + esc(c.label) + ' </span>' : "";
    return '<span class="chip' + cls + '">' + label +
      '<strong>' + esc(String(c.value)) + '</strong></span>';
  });
  el.innerHTML = parts.join("");
}

// Roll call owns the screen until READY: the question plus the button
// in learn view, the READY button in the cook action row.
function paintRollcall(v) {
  const roll = !!v && v.state === "ROLLCALL";
  const bar = $("rollcall-bar"), ready = $("cook-ready");
  // The rollcall bar serves the learn fallback; cook mode answers the
  // roll call from the coach bar instead.
  const inCook = (typeof cookMode !== "undefined") && cookMode;
  if (bar) bar.hidden = !roll || inCook;
  // cook-ready is the legacy fallback button inside coach-bar;
  // hide it during REVEAL so coach-primary owns the action.
  if (ready) ready.hidden = !roll || (typeof appPhase !== "undefined" && appPhase === "REVEAL");
  if (roll) {
    const rc = (v.fundi && v.fundi.rollcall) || {};
    const q = $("rollcall-q");
    if (q) {
      q.textContent =
        rc.question || "Say READY when your parts are on the bench.";
    }
  }
}

// Escalated failures carry their fix chain; DONE carries stretch
// goals. Both paint as plain lists and hide when absent.
function paintTroubleshoot(elId, tips) {
  const el = $(elId);
  if (!el) return;
  const list = (tips || []).filter((t) => t);
  el.hidden = !list.length;
  el.innerHTML = list.map((t) => "<li>" + esc(t) + "</li>").join("");
}

function paintStretch(elId, ideas) {
  const el = $(elId);
  if (!el) return;
  const list = (ideas || []).filter((s) => s && (s.title || s.summary));
  el.hidden = !list.length;
  el.innerHTML = list.map((s) =>
    "<h3>" + esc(s.title || "Keep going") + "</h3><p>" +
    esc(s.summary || "") + "</p>").join("");
}

// READY: answer the parts roll call. READY begins chapter 1; anything
// else re-prompts without advancing (the server decides the match).
async function sendReady(transcript) {
  if (!learnSid) return;
  if (busy) return;
  busy = true;
  const wantSid = learnSid;
  try {
    const r = await fetch("/api/session/ready", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: learnSid, transcript: transcript || "ready" }),
    });
    const v = await r.json();
    if (turnViewStale(v, wantSid)) return; // session moved on mid-turn
    if (!r.ok) {
      if (cookMode) $("cook-heard").textContent =
        "Ready failed: " + ((v && v.detail) || "unknown session");
      return;
    }
    if (cookMode && v.learning) {
      renderCook(v);
      tutorBeat(v, cookSpeak, { quiet: false, manual: true });
    } else {
      renderLearning(v);
      tutorBeat(v, speak, { quiet: false, manual: true });
    }
  } catch (e) {
    if (cookMode) $("cook-heard").textContent = "Ready failed: " + e.message;
  } finally {
    busy = false;
    drainVoiceStep();
    drainCookVoice();
  }
}
$("cook-ready").onclick = () => sendReady("ready");
$("rollcall-ready").onclick = () => sendReady("ready");

// NEXT STEP: the tap that moves the build forward. Steps advance on
// taps, not sensor passes — proof comes once, at the end, from live
// telemetry. The server holds the final step (only Deploy or the
// explicit escape finishes the build) and audit-logs every advance.
function drainVoiceStep() {
  // Voice "done" heard mid-turn advances once free.
  const q = pendingVoiceStep;
  pendingVoiceStep = "";
  if (q) sendNextStep(q);
}

async function sendNextStep(transcript) {
  if (!learnSid) return;
  let waited = 0;
  while (busy && waited < 10) {
    await new Promise((res) => setTimeout(res, 200));
    waited++;
  }
  if (busy) return;
  cancelBeatSpeech(); // the old step's summary stops here
  cancelWireSpeech();
  busy = true;
  const wantSid = learnSid;
  try {
    const r = await fetch("/api/session/advance", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: learnSid,
        transcript: transcript || "next step" }),
    });
    const v = await r.json();
    if (turnViewStale(v, wantSid)) return; // session moved on mid-turn
    if (!r.ok) {
      if (cookMode) $("cook-heard").textContent =
        "Next step failed: " + ((v && v.detail) || "unknown session");
      return;
    }
    if (cookMode && v.learning) {
      renderCook(v);
      $("cook-heard").textContent = "Next step below.";
      tutorBeat(v, cookSpeak, { quiet: false, manual: true });
    } else {
      renderLearning(v);
      tutorBeat(v, speak, { quiet: false, manual: true });
    }
  } catch (e) {
    if (cookMode) $("cook-heard").textContent = "Next step failed: " + e.message;
  } finally {
    busy = false;
    drainVoiceStep();
    drainCookVoice();
  }
}

$("rescan").onclick = async () => {
  if (busy) return;
  $("instruction").textContent = "Re-scanning equipment…";
  try {
    await fetch("/api/session/rescan", { method: "POST" });
  } catch (e) { showError(e.message); return; }
  $("instruction").textContent = "Equipment re-scanned.";
};

// turn:stale:start
function turnViewStale(view, wantSid) {
  // A turn response belongs to the session that was live when the
  // turn was sent; anything else is a late arrival from a session
  // the learner already left, and rendering it would flap the
  // instruction, the diagram, and the beats between two builds.
  const id = (view && view.id) || "";
  return !!(id && wantSid && id !== wantSid);
}
// turn:stale:end

// Beat driver: speaks the turn. Turns end when the speech ends —
// the follow-up check-in question was removed.
function tutorBeat(view, say, opts) {
  const text = (view && view.speak) || "";
  logCoach("Coach", text);
  say(text);
}

// ── Flow phases: ideation → reveal → building → deploying → ──
// ── verifying → rescue. The coach bar offers one primary action ──
// ── per state; drawers hold code, fix-it lists, and the monitor. ──

// phase:machine:start
function resolveAppPhase(ctx) {
  const c = ctx || {};
  if (!c.hasSession) return "IDEATION";
  if (!c.revealed) return "REVEAL";
  if (c.fault) return "RESCUE";
  if (c.monitorOn) return "VERIFYING";
  if (c.flashOpen) return "DEPLOYING";
  return "BUILDING";
}
// phase:machine:end

// coach:action:start
function coachPrimaryAction(ctx) {
  // Exactly one primary action per state (null hides the button and
  // lets a card own the moment: confirm ask, flash dialog, monitor).
  const c = ctx || {};
  if (c.phase === "REVEAL") {
    return { id: "start-building", label: "Start Building" };
  }
  if (c.phase !== "BUILDING") return null;
  if (c.serverState === "DONE") {
    return { id: "deploy", label: "Deploy Firmware" };
  }
  if (c.serverState === "ROLLCALL") {
    return { id: "ready", label: "Ready — Start Building" };
  }
  // The step summary names every connection at once, so steps
  // advance on one tap and the final step deploys. Proof comes later,
  // once, from live telemetry — there is no per-step check to offer.
  const stepN = c.stepN || 0, stepsTotal = c.stepsTotal || 0;
  if (stepsTotal > 0 && stepN >= stepsTotal) {
    return { id: "deploy", label: "Deploy Firmware" };
  }
  return { id: "next-step", label: "Next Step" };
}
// coach:action:end

// reveal:speech:start
function revealArticle(word) {
  // Spoken article for part titles: initialisms follow letter-name
  // sounds (an LED, an HC-SR04, a USB), plain words follow spelling.
  const w = String(word || "");
  const first = w.charAt(0);
  if (!first) return "a";
  const initialism = /[-0-9]/.test(w) ||
    (w.length > 1 && w === w.toUpperCase());
  if (initialism) {
    return "AEFHILMNORSX".indexOf(first.toUpperCase()) >= 0 ? "an" : "a";
  }
  return "aeiouAEIOU".indexOf(first) >= 0 ? "an" : "a";
}

function revealBoardName(board) {
  const names = { esp32_devkit: "ESP32 DevKit", arduino_uno: "Arduino Uno" };
  return names[board] || "";
}

function revealPartTitles(wires, board) {
  // Board first, then each wired part once, display titles verbatim.
  const titles = [];
  const boardName = revealBoardName(board);
  if (boardName) titles.push(boardName);
  for (const w of wires || []) {
    const t = w && (w.part_title || "");
    if (t && titles.indexOf(t) < 0) titles.push(t);
  }
  return titles;
}

function revealSpeech(title, parts) {
  const list = (parts || []).filter((p) => p);
  let need = "the parts on your bench";
  if (list.length === 1) need = revealArticle(list[0]) + " " + list[0];
  else if (list.length > 1) {
    const last = list[list.length - 1];
    need = list.slice(0, -1).map((p) => revealArticle(p) + " " + p)
      .join(", ") + ", and " + revealArticle(last) + " " + last;
  }
  return "Here is the circuit we are going to build" +
    (title ? " for " + title : "") + ". We need " + need + ".";
}
// reveal:speech:end

// wire:beats:start
function wireBeatSay(wire, idx, total, boardTitle) {
  // One wire, narrated: endpoints, color, position. Everything comes
  // from the netlist annotation; nothing is invented. (Article helper
  // shared with the reveal block, concatenated in tests.)
  const w = wire || {};
  const fromPin = w.board_pin || "?";
  const toPin = w.part_pin || "?";
  const part = w.part_title || "the part";
  const board = boardTitle || "the board";
  const color = w.color_name || "";
  return "Wire " + (idx + 1) + " of " + total + ": Connect the " + fromPin +
    " pin on " + board + " to the " + toPin + " pin on " + part +
    (color ? ". Use " + revealArticle(color) + " " + color + " wire." : ".");
}

function wireBeatsForStep(wires, stepN, boardTitle) {
  const stepWires = (wires || []).filter((w) => w && w.step_added === stepN);
  return stepWires.map((w, i) => ({
    key: (w.part || "") + ":" + (w.part_pin || ""),
    wire: w,
    say: wireBeatSay(w, i, stepWires.length, boardTitle),
  }));
}
// wire:beats:end

let appRevealed = false; // Start Building tapped for this session
let appPhase = "IDEATION";
let appFault = false; // rescue latched by the monitor
let monitorOn = false; // telemetry streaming
let wireBeats = []; // this step's wires, narrated as one summary
let wireBeatIdx = 0;
let wireBeatAcked = false;
let lastFlashPort = ""; // the monitor reuses the deploy port

function currentPhaseCtx() {
  const dialog = $("flash-dialog");
  return {
    hasSession: !!learnSid,
    revealed: appRevealed,
    fault: appFault,
    monitorOn: monitorOn,
    flashOpen: flashBusy || !!(dialog && !dialog.hidden),
  };
}

function paintCoachBar(instruction) {
  const bar = $("coach-bar");
  if (!bar) return;
  bar.hidden = !cookMode;
  if (!cookMode) {
    document.documentElement.style.removeProperty("--coachbar-measured");
    document.body.classList.remove("hide-narration");
    return;
  }
  appPhase = resolveAppPhase(currentPhaseCtx());
  // Narration runs in the background while building: the voice speaks
  // the long instruction, the leaf keeps glanceable state only.
  document.body.classList.toggle("hide-narration",
    appPhase === "BUILDING" && (cookView && cookView.state) !== "DONE");
  if (instruction !== undefined && instruction !== null) {
    // Write the current step summary to the slim coach-bar label.
    // The full instruction lives in #cook-instruction in the left leaf.
    const barText = $("coach-bar-text") || $("coach-text");
    if (barText) barText.textContent = instruction;
    const instrEl = $("cook-instruction");
    if (instrEl) instrEl.textContent = instruction;
  }
  const action = coachPrimaryAction({
    phase: appPhase,
    serverState: (cookView && cookView.state) || learnState,
    wiresTotal: wireBeats.length,
    wireIdx: wireBeatIdx,
    wireAcked: wireBeatAcked,
    stepN: learnStepN,
    stepsTotal: (cookView && cookView.steps_total) || learnStepTotal,
  });
  const btn = $("coach-primary");
  if (!action) {
    btn.hidden = true;
    btn.classList.remove("coach-deploy");
    measureCoachBar();
    return;
  }
  btn.hidden = false;
  btn.textContent = action.label;
  btn.dataset.action = action.id;
  btn.classList.toggle("coach-deploy", action.id === "deploy");
  measureCoachBar();
}

function measureCoachBar() {
  // The stage pads by this height so the diagram never slides under
  // the fixed bar (re-measured every paint: wrapping changes it).
  requestAnimationFrame(() => {
    const bar = $("coach-bar");
    const h = bar && !bar.hidden && bar.offsetHeight ? bar.offsetHeight : 0;
    if (h > 0) document.documentElement.style.setProperty("--coachbar-measured", h + "px");
    else document.documentElement.style.removeProperty("--coachbar-measured");
  });
}

function coachTapped() {
  const id = ($("coach-primary").dataset.action) || "";
  if (id === "start-building") {
    // If server already returned ROLLCALL during reveal, skip the
    // intermediate reveal screen and go straight to Ready/building.
    const serverState = (cookView && cookView.state) || learnState;
    if (serverState === "ROLLCALL") {
      revealToBuilding();
      sendReady("ready");
    } else {
      revealToBuilding();
    }
  } else if (id === "ready") sendReady("ready");
  else if (id === "next-step") sendNextStep();
  else if (id === "deploy") openDeploy();
}
$("coach-primary").onclick = () => coachTapped();

function toggleCoachMore() {
  const ov = $("coach-overflow");
  ov.hidden = !ov.hidden;
}
$("coach-more").onclick = () => toggleCoachMore();

function toggleMute() {
  muted = !muted;
  try { localStorage.setItem("fundiloop-muted", muted ? "1" : "0"); } catch (e) { /* private mode */ }
  if (muted) VoiceEngine.stop();
  updateMuteBtn();
}
$("coach-voice").onclick = () => toggleMute();

function setDrawer(which, open) {
  const el = $(which === "left" ? "left-drawer" : "right-drawer");
  if (el) el.hidden = !open;
  // The monitor picker lists the live ports every time the drawer
  // opens, so a board plugged in mid-build is pickable on sight.
  if (which === "right" && open) refreshMonitorPorts();
}

function toggleDrawer(which) {
  const el = $(which === "left" ? "left-drawer" : "right-drawer");
  if (el) setDrawer(which, el.hidden);
}
$("left-drawer-tab").onclick = () => toggleDrawer("left");
$("left-drawer-close").onclick = () => setDrawer("left", false);
$("right-drawer-tab").onclick = () => toggleDrawer("right");
$("right-drawer-close").onclick = () => setDrawer("right", false);

function maybeOpenDrawer(v) {
  // The drawer opens itself for moments that live inside it: DONE
  // stretch cards and escalated fix-it chains.
  const fundi = (v || {}).fundi || {};
  if (fundi.event === "LESSON_DONE") { setDrawer("left", true); switchStageTab("code"); return; }
  if (fundi.troubleshoot && fundi.troubleshoot.length) {
    setDrawer("left", true);
  }
}

function logCoach(who, text) {
  // Tiny coach history for the drawer (cap 20, newest last).
  const log = $("coach-log");
  if (!log || !text) return;
  while (log.children.length >= 20) log.removeChild(log.firstChild);
  const li = document.createElement("li");
  li.textContent = who + ": " + text;
  log.appendChild(li);
}

let revealView = null; // stored /diagnose view while revealing

async function enterReveal(view, project) {
  revealView = view;
  stopMonitor("New session.");
  exitRescue(false);
  appRevealed = false;
  wireBeats = [];
  wireBeatIdx = 0;
  wireBeatAcked = false;
  // Fresh-session hygiene: nothing from the last build leaks into
  // this one — step counters, code cache, cue memory, auto baseline.
  // The reveal total comes from THIS view, never the stale global.
  const total = (view && view.steps_total) || learnStepTotal || 1;
  learnStepN = 1;
  learnStepTotal = total;
  learnTitle = (view && view.project_title) || "";
  learnDone = false;
  codeFetchedSid = "";
  appPhase = "REVEAL";
  enterCookMode(view, { quiet: true });
  setSchematicView("cards");
  resetSchematicZoom(); // a new project starts at 50%: the whole circuit fits
  switchStageTab("diagram"); // Phase 1: show the circuit diagram
  // One voice: the diagram render covers the gap visually, then
  // the reveal line speaks in the tutor voice (no bridging ack).
  paintCoachBar("Review the circuit, then tap Start Building.");
  try {
    await fetchSchematic("cook-schematic-svg", "cook-schematic-wires",
      cookSchematicState, "cook-schematic-sel", total);
  } catch (e) { /* the diagram is a bonus; voice carries the reveal */ }
  const line = revealSpeech(project && project.title,
    revealPartTitles(cookSchematicState.wires, learnBoard));
  cookSpeak(line);
}

function revealToBuilding() {
  const v = revealView || cookView;
  if (!v) return;
  revealView = null;
  appRevealed = true;
  setSchematicView("breadboard");
  switchStageTab("breadboard"); // Phase 2: show the breadboard
  renderCook(v);
  tutorBeat(v, cookSpeak, { quiet: false, manual: true });
}

async function openDeploy() {
  // DONE primary: flash the finished firmware via the deploy bar's port.
  await refreshFlashPorts(); // deploy reads a live list, never a stale one
  const port = ($("cook-flash-port") && $("cook-flash-port").value) || "";
  if (!port) {
    $("cook-flash-status").textContent = "Pick the ESP32 serial port first, then DEPLOY — or CONNECT BOARD to flash from the browser.";
    cookSpeak("Pick the ESP32 serial port in the deploy bar first, then tap Deploy.");
    return;
  }
  flashFinal("cook-flash-port", "cook-flash-status");
}

let beatsKey = ""; // sid:step the current beats were built for
let beatSpeech = null; // live beat-narration token

function cancelBeatSpeech() { beatSpeech = null; }

function speakStepSummary(stepN, delayMs) {
  // Narrate the whole step at once, yielding to speech already in
  // flight (the step opening): retry briefly, then speak anyway so a
  // wedged utterance never leaves the step silent. Any tap cancels it.
  const mine = {};
  beatSpeech = mine;
  let tries = 0;
  const attempt = () => {
    if (beatSpeech !== mine) return;
    if (speechActive() && tries < 120) {
      tries++;
      setTimeout(attempt, 500);
      return;
    }
    const wires = wireBeats.map((b) => b.wire).filter(Boolean);
    const items = stepSummaryItems(wires, stepN || learnStepN,
      revealBoardName(learnBoard));
    if (items.length) speakWireList(items, cookSpeak,
      "cook-schematic-svg", "cook-schematic-sel");
  };
  setTimeout(attempt, delayMs || 0);
}

function coachBeatText() {
  if (!wireBeats.length || wireBeatIdx >= wireBeats.length) {
    return (cookView && cookView.instruction) || "";
  }
  return wireBeats[wireBeatIdx].say;
}

// step:summary:start
function stepWirePhrase(w, boardTitle) {
  // One connection inside the flowing step summary: short clause, no
  // numbering (the transition carries the order).
  const fromPin = (w && w.board_pin) || "?";
  const toPin = (w && w.part_pin) || "?";
  const part = (w && w.part_title) || "the part";
  const board = boardTitle || "the board";
  const color = (w && w.color_name) || "";
  return "connect the " + fromPin + " pin on " + board +
    " to the " + toPin + " pin on " + part +
    (color ? ", using " + revealArticle(color) + " " + color + " wire" : "");
}

function stepSummaryItems(wires, stepN, boardTitle) {
  // The whole step in one flowing narration: intro + each connection
  // with a transition + outro. Each wire lights as it is named.
  const list = (wires || []).filter(Boolean);
  if (!list.length) return [];
  const items = [{ id: "", phrase: list.length === 1
    ? "Step " + stepN + " has one connection."
    : "Step " + stepN + " has " + list.length + " connections." }];
  const mid = ["Next", "Then", "After that"];
  list.forEach((w, i) => {
    let lead = "";
    if (list.length > 1) {
      if (i === 0) lead = "First, ";
      else if (i === list.length - 1) lead = "Finally, ";
      else lead = mid[(i - 1) % mid.length] + ", ";
    }
    items.push({ id: schematicWireId(w),
      phrase: lead + stepWirePhrase(w, boardTitle),
      label: (w.part || "") + "." + (w.part_pin || "") +
        " → " + (w.board_pin || "") });
  });
  const done = cookView && cookView.state === "DONE";
  items.push({ id: "", phrase: !appRevealed
    ? "Tap Start Building when ready to wire it up."
    : (done ? "That is the whole build — nicely wired."
      : "Tap Next Step when everything is wired.") });
  return items;
}
// step:summary:end

function buildWireBeats() {
  // (Re)build this step's wire list from the cook netlist. Step and
  // session changes reset and speak the flowing step summary once;
  // re-renders never re-speak it.
  if (!cookMode) return;
  const key = learnSid + ":" + learnStepN;
  const st = cookSchematicState;
  const fresh = st.sid === learnSid && st.n === learnStepN;
  const build = () => {
    wireBeats = wireBeatsForStep(st.wires, learnStepN,
      revealBoardName(learnBoard));
    wireBeatIdx = wireBeats.length; // summary mode: no per-wire stop
    wireBeatAcked = false;
    paintWireBeat(); // past-end index restores the full diagram
    paintCoachBar(coachBeatText());
    if (wireBeats.length) speakStepSummary(learnStepN, 500);
  };
  if (key !== beatsKey) {
    cancelBeatSpeech();
    beatsKey = key;
    wireBeats = [];
    wireBeatIdx = 0;
    wireBeatAcked = false;
    if (!fresh) {
      paintWireBeat();
      paintCoachBar(coachBeatText());
      return;
    }
    build();
    return;
  }
  if (!wireBeats.length && fresh && (st.wires || []).length) build();
}

function paintWireBeat() {
  // Focus the active wire: it glows and redraws, its endpoint parts
  // glow, sibling wires of this step dim. Done or beatless steps
  // restore the full diagram.
  const svg = $("cook-schematic-svg");
  if (!svg) return;
  svg.removeAttribute("data-sel");
  const groups = svg.querySelectorAll('g[id^="wire-"]');
  groups.forEach((g) => {
    g.classList.remove("wire-active", "wire-dim");
    g.style.opacity = ""; // click-isolate is inline; beats own this diagram
  });
  svg.querySelectorAll(".schematic-part").forEach(
    (g) => g.classList.remove("part-active"));
  if (!wireBeats.length || wireBeatIdx >= wireBeats.length) return;
  const beat = wireBeats[wireBeatIdx];
  const activeId = schematicWireId(beat.wire);
  groups.forEach((g) => {
    if (g.id === activeId) {
      g.classList.add("wire-active");
      g.setAttribute("opacity", "1");
      animateWireNow(g, 0);
    } else if (g.classList.contains("schematic-new")) {
      g.classList.add("wire-dim");
    }
  });
  const partEl = document.getElementById(schematicPartId(beat.wire.part || ""));
  if (partEl) partEl.classList.add("part-active");
  const boardEl = document.getElementById("part-esp32");
  if (boardEl) boardEl.classList.add("part-active");
}

// monitor:start
const MONITOR_MAX_LINES = 200; // painted lines kept in the monitor panel
const MONITOR_CTX_LINES = 15; // recent lines sent to the tutor on rescue
const MONITOR_STALL_MS = 8000; // silence that means the stream stalled
const MONITOR_READOUT_MS = 12000; // slowest spoken telemetry readout
const MONITOR_PROOF_LINES = 5; // trailing healthy lines that prove the build
const MONITOR_FAULT_LINES = 3; // consecutive fault lines that trip rescue

// monitor:pure:start
function parseTaggedTelemetry(line) {
  // Strict DEVICE | METRIC | VALUE | UNIT split; null unless the
  // device and metric tokens qualify (prose with "|" stays on the
  // legacy path). Mirrors the backend's _parse_tagged exactly.
  const parts = String(line == null ? "" : line).split("|")
    .map((p) => p.trim());
  if (parts.length < 3) return null;
  if (!/^[A-Za-z0-9][A-Za-z0-9 _\-+]*$/.test(parts[0])) return null;
  if (!/^[A-Za-z0-9 _\-+%]+$/.test(parts[1])) return null;
  return { device: parts[0], metric: parts[1], value: parts[2],
    unit: parts.length > 3 ? parts[3] : "" };
}

function prettyDevice(tag) {
  return String(tag || "").replace(/[_-]+/g, " ").replace(/\s+/g, " ")
    .trim() || "the board";
}

function classifyTaggedLine(t) {
  // Tagged triage: fault words over metric + value, negatives veto,
  // any numeric value counts. Mirrors _proof_class exactly.
  const num = /^-?\d+(?:\.\d+)?$/.test(t.value) ? parseFloat(t.value) : null;
  const faultWord = /\b(error|timeout|time-out|fail|failed|nan|no echo|no response|not found|disconnect|offline|disconnected)\b/i
    .test(t.metric + " " + t.value);
  if (faultWord || (num !== null && num < 0)) {
    return { device: t.device, value: null, unit: "", fault: true };
  }
  let unit = String(t.unit || "").toLowerCase();
  if (unit === "meter" || unit === "meters") unit = "m";
  if (unit === "inch" || unit === "inches" || unit === "in") unit = "in";
  return { device: t.device, value: num, unit: unit, fault: false };
}

function classifyTelemetryLine(line) {
  // Deterministic triage for one device line: a spoken measurement
  // (value + canonical unit), or a fault flag. Boot chatter never
  // faults and never speaks; any negative reading is bogus. Tagged
  // lines attribute to their device; untagged lines keep device "".
  const text = String(line == null ? "" : line);
  if (!text.trim()) return { device: "", value: null, unit: "", fault: false };
  if (/rst:|boot:|load:|entry |ets [A-Z]/i.test(text)) {
    return { device: "", value: null, unit: "", fault: false };
  }
  const tagged = parseTaggedTelemetry(text);
  if (tagged) return classifyTaggedLine(tagged);
  const faultWord = /\b(error|timeout|time-out|fail|failed|nan|no echo|no response|not found|disconnect|offline|disconnected)\b/i.test(text);
  const sentinel = /(^|:\s*)-1(\D|$)/.test(text);
  let m = text.match(/distance\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*(cm|mm|meters?|inches?|in|m)?/i);
  let value = null, unit = "";
  if (m) {
    value = parseFloat(m[1]);
    unit = m[2] || "cm";
  } else if ((m = text.match(/(-?\d+(?:\.\d+)?)\s*(cm|mm|meters?|inches?|in|m)\b/i))) {
    value = parseFloat(m[1]);
    unit = m[2];
  }
  unit = unit.toLowerCase();
  if (unit === "meter" || unit === "meters") unit = "m";
  if (unit === "inch" || unit === "inches" || unit === "in") unit = "in";
  const fault = faultWord || sentinel || (value !== null && value < 0);
  if (fault) return { device: "", value: null, unit: "", fault: true };
  return { device: "", value: value, unit: unit, fault: false };
}

function telemetryReadout(c) {
  // One spoken sentence for a classified line, or "" when there is
  // nothing worth interrupting the tutor for. Tagged readings name
  // their device; legacy readings keep the plain sentence.
  if (!c || c.value === null || c.value === undefined || c.fault) return "";
  const word = { cm: "centimeters", mm: "millimeters", m: "meters",
    in: "inches", c: "degrees", f: "degrees", pct: "percent",
    "%": "percent" }[c.unit] || "";
  if (!word) return "";
  const reading = (Math.round(c.value * 10) / 10) + " " + word + ".";
  if (c.device) return prettyDevice(c.device) + " reads " + reading;
  return "You are at " + reading;
}

function telemetryProvesLife(line) {
  // A line counts toward telemetry proof when it carries a live
  // measurement or the firmware heartbeat. Boot chatter and bare
  // chatter never count; fault lines reset the streak instead.
  const c = classifyTelemetryLine(line);
  if (c.fault) return false;
  if (c.value !== null && c.value !== undefined) return true;
  return /(status|heartbeat|\bhb\b|^ok\b|alive)/i.test(String(line || ""));
}

function suspectReconnectSpeech(suspect) {
  // Spoken reconnect guidance for a suspect netlist wire: which wire
  // to reseat and where it runs. "" when no wire was named.
  const part = suspect && suspect.part;
  const pin = suspect && suspect.part_pin;
  if (!part || !pin) return "";
  const name = String(part).replace(/_/g, " ");
  let say = "Reseat the " + pin + " wire on the " + name;
  if (suspect.board_pin) say += " — it runs to board " + suspect.board_pin;
  return say + ". Push both ends firmly home.";
}
// monitor:pure:end

let monitorSource = null; // live EventSource, one stream at a time
let monitorStallTimer = null;
let monitorCtx = []; // recent raw lines for the rescue diagnosis
let monitorCount = 0;
let lastMonitorLineAt = 0;
let lastMonitorReadoutAt = 0;
let monitorStreak = 0; // trailing healthy-shape lines toward proof
let monitorFaults = 0; // consecutive untagged fault lines toward rescue
let deviceState = {}; // tag -> {faults, streak, lastSeen} per device
let rescueDevice = ""; // failed device tag, or "" for a global latch

function deviceStateFor(tag) {
  return deviceState[tag] ||
    (deviceState[tag] = { faults: 0, streak: 0, lastSeen: 0 });
}

function deviceFaultActive() {
  // Any device holding faults: proof waits until it recovers.
  return Object.keys(deviceState).some(
    (t) => (deviceState[t].faults || 0) > 0);
}

function healthyDevices() {
  // Observed devices that proved healthy and hold no faults. SYS is
  // the stream itself, never a device to exonerate aloud.
  return Object.keys(deviceState).filter((t) =>
    t.toUpperCase() !== "SYS" && (deviceState[t].faults || 0) === 0 &&
    (deviceState[t].streak || 0) > 0);
}
let monitorVerified = false; // proof accepted (one DONE per session)
let monitorVerifyCooldownUntil = 0;

function paintMonitorMeta() {
  const meta = $("monitor-meta");
  if (meta) meta.textContent = "Listening · " + monitorCount + " lines";
}

function appendMonitorLine(line) {
  const out = $("monitor-out");
  if (!out) return;
  out.textContent += line + "\n";
  const rows = out.textContent.split("\n");
  if (rows.length > MONITOR_MAX_LINES + 1) {
    out.textContent = rows.slice(rows.length - MONITOR_MAX_LINES - 1).join("\n");
  }
  out.scrollTop = out.scrollHeight;
}

function maybeSpeakReadout(c) {
  const say = telemetryReadout(c);
  if (!say || muted) return;
  const now = Date.now();
  if (now - lastMonitorReadoutAt < MONITOR_READOUT_MS) return;
  if (speechActive()) return; // readouts never queue behind the tutor
  lastMonitorReadoutAt = now;
  logCoach("Monitor", say);
  cookSpeak(say);
}

function onMonitorLine(text) {
  const line = String(text == null ? "" : text);
  monitorCtx.push(line);
  if (monitorCtx.length > MONITOR_CTX_LINES) monitorCtx.shift();
  monitorCount += 1;
  lastMonitorLineAt = Date.now();
  appendMonitorLine(line);
  paintMonitorMeta();
  const c = classifyTelemetryLine(line);
  const tag = c.device || "";
  if (c.fault) {
    monitorStreak = 0;
    // Faults count per device: a healthy device's good lines never
    // mask a sick one's streak. Untagged lines keep the global path.
    if (tag) {
      const st = deviceStateFor(tag);
      st.faults += 1;
      st.streak = 0;
      st.lastSeen = Date.now();
      // A lone fault line is a passing dip (hand out of range), not
      // a lost device: rescue trips only on consecutive faults.
      if (st.faults >= MONITOR_FAULT_LINES) {
        enterRescue("The " + prettyDevice(tag) + " reported: " + line, tag);
      }
    } else {
      monitorFaults += 1;
      if (monitorFaults >= MONITOR_FAULT_LINES) {
        enterRescue("The board reported: " + line);
      }
    }
    return;
  }
  if (tag) {
    const st = deviceStateFor(tag);
    st.faults = 0;
    st.lastSeen = Date.now();
    if (telemetryProvesLife(line)) st.streak += 1;
  } else {
    monitorFaults = 0;
  }
  if (appFault) {
    // Recovery needs the failed device's own good line; a global
    // latch still clears on any healthy line. Other devices talking
    // is not recovery.
    if (rescueDevice && tag !== rescueDevice) return;
    exitRescue(true);
    // fall through: the recovery line itself may count toward proof
  }
  Orb.monitor(true);
  maybeSpeakReadout(c);
  if (telemetryProvesLife(line)) {
    monitorStreak += 1;
    if (monitorStreak >= MONITOR_PROOF_LINES && monitorOn &&
        !monitorVerified && Date.now() >= monitorVerifyCooldownUntil &&
        !deviceFaultActive()) {
      monitorVerified = true; // one attempt per streak; a failed
      monitorStreak = 0; // attempt cools down, then the streak rebuilds
      requestTelemetryProof();
    }
  }
}

async function requestTelemetryProof() {
  // The build proves itself: trailing healthy lines go to the server,
  // which marks DONE only when the evidence holds (never on ask).
  let v = null;
  try {
    const r = await fetch("/api/session/telemetry-verified", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: learnSid,
        lines: monitorCtx.slice(-MONITOR_CTX_LINES) }),
    });
    v = await r.json();
    if (!r.ok || !v || v.verified === false) throw new Error("not yet");
  } catch (e) {
    monitorVerified = false;
    monitorVerifyCooldownUntil = Date.now() + 30000;
    return;
  }
  if (turnViewStale(v, learnSid)) return;
  if (!v.learning) return;
  renderCook(v);
  logCoach("Coach", v.speak || "It works!");
  cookSpeak(v.speak || "It works! You built it.");
}

function monitorWatchdog() {
  if (!monitorOn || appFault || !lastMonitorLineAt) return;
  if (Date.now() - lastMonitorLineAt > MONITOR_STALL_MS) {
    enterRescue("No data for " + Math.round(MONITOR_STALL_MS / 1000) +
      " seconds — the stream stalled.");
    return;
  }
  // Per-device silence: the stream flows but a known device went
  // quiet — its own latch, naming it.
  const tags = Object.keys(deviceState);
  for (let i = 0; i < tags.length; i++) {
    const tag = tags[i];
    if (tag.toUpperCase() === "SYS") continue;
    const st = deviceState[tag];
    if (st.lastSeen && Date.now() - st.lastSeen > MONITOR_STALL_MS) {
      enterRescue("No data from the " + prettyDevice(tag) + " for " +
        Math.round(MONITOR_STALL_MS / 1000) +
        " seconds — it went quiet.", tag);
      return;
    }
  }
}

function startMonitor(port) {
  // Live serial monitor after the final flash: the stream owns the
  // port (probes reopen lazily on demand).
  if (!learnSid) return;
  stopMonitor("");
  const usePort = port || lastFlashPort || "";
  if (!usePort) {
    // START with no board anywhere: say what fixes it (plug in, tap
    // START again — adoption retries on every tap).
    const meta = $("monitor-meta");
    if (meta) meta.textContent = "No board found — plug in USB, pick the board port, and press START.";
    cookSpeak("I can't find the board. Plug it in over USB, pick the board port, then press START.");
    return;
  }
  monitorOn = true;
  monitorCtx = [];
  monitorCount = 0;
  lastMonitorLineAt = 0;
  lastMonitorReadoutAt = 0;
  monitorStreak = 0;
  monitorFaults = 0;
  deviceState = {};
  rescueDevice = "";
  monitorVerified = false;
  monitorVerifyCooldownUntil = 0;
  setDrawer("right", true);
  const meta = $("monitor-meta");
  if (meta) meta.textContent = "Connecting to " + usePort + "…";
  const out = $("monitor-out");
  if (out) out.textContent = "";
  paintCoachBar("Deployed. Listening to your board — interact with " +
    "it and watch the monitor.");
  let src;
  try {
    src = new EventSource("/project/" + encodeURIComponent(learnSid) +
      "/telemetry/stream?port=" + encodeURIComponent(usePort) +
      "&baud=115200");
  } catch (e) {
    enterRescue("I could not open the monitor stream.");
    return;
  }
  monitorSource = src;
  src.addEventListener("ready", () => {
    lastMonitorLineAt = Date.now();
    const m = $("monitor-meta");
    if (m) m.textContent = "Listening on " + usePort + "…";
  });
  src.addEventListener("line", (ev) => {
    let text = "";
    try { text = String((JSON.parse(ev.data) || {}).line || ""); }
    catch (e) { text = String(ev.data || ""); }
    onMonitorLine(text);
  });
  src.addEventListener("error", (ev) => {
    let detail = "";
    try { detail = String((JSON.parse(ev.data) || {}).detail || ""); }
    catch (e) { detail = ""; }
    enterRescue(detail ? ("Monitor error: " + detail)
                       : "The monitor stream closed.");
  });
  src.onerror = () => {
    if (monitorSource !== src) return;
    enterRescue("I lost the connection to the board.");
  };
  monitorStallTimer = setInterval(monitorWatchdog, 2000);
}

function stopMonitor(note) {
  monitorOn = false;
  if (monitorSource) {
    try { monitorSource.close(); } catch (e) { /* already closed */ }
    monitorSource = null;
  }
  if (monitorStallTimer) {
    clearInterval(monitorStallTimer);
    monitorStallTimer = null;
  }
  Orb.monitor(false);
  const meta = $("monitor-meta");
  if (meta && note !== "") meta.textContent = note || "Stopped.";
  if (note !== "") paintCoachBar("Monitor stopped.");
}

async function requestRescueDiagnosis() {
  // Model diagnosis for the stalled stream: spoken guidance plus a
  // suspect netlist wire, or honest generic guidance when the model
  // names nothing real (the endpoint validates the suspect).
  const msg = $("rescue-msg"), susp = $("rescue-suspect");
  let data = null;
  try {
    const r = await fetch("/project/" + encodeURIComponent(learnSid) +
      "/telemetry/diagnose", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          // A device latch sends that device's own trailing lines
          // plus the healthy set; a global latch sends everything.
          lines: (rescueDevice ? monitorCtx.filter(
            (l) => classifyTelemetryLine(l).device === rescueDevice)
            : monitorCtx).slice(-MONITOR_CTX_LINES),
          device: rescueDevice || null,
          healthy: healthyDevices().filter((t) => t !== rescueDevice),
        }),
      });
    if (r.ok) data = await r.json();
  } catch (e) { data = null; }
  if (!appFault) return; // back online while the tutor thought
  const message = (data && data.message) || "";
  const suspect = (data && data.suspect) || null;
  if (msg) {
    msg.textContent = message ||
      "The stream stopped and I could not reach the tutor. Check that " +
      "the board is powered and the wires are seated, then tap START.";
  }
  if (suspect && suspect.part && suspect.part_pin) {
    document.querySelectorAll("#cook-schematic-svg g.wire-fault")
      .forEach((g) => g.classList.remove("wire-fault"));
    const g = document.getElementById(
      schematicWireId({ part: suspect.part, part_pin: suspect.part_pin }));
    if (g) g.classList.add("wire-fault");
    const partEl = document.getElementById(schematicPartId(suspect.part));
    if (partEl) partEl.classList.add("part-active");
    if (susp) {
      susp.textContent = "Suspect: " + suspect.part_pin + " on " +
        suspect.part +
        (suspect.board_pin ? (" (board " + suspect.board_pin + ")") : "") +
        " — check that wire.";
    }
    // Every wire on the suspect part highlights: the whole failed
    // device lights up, healthy devices stay dark.
    ((data && data.part_wires) || []).forEach((w) => {
      if (!w || !w.part || !w.part_pin) return;
      const g = document.getElementById(schematicWireId(w));
      if (g) g.classList.add("wire-fault");
    });
  } else if (susp) {
    susp.textContent = "No single wire stood out — check power and ground first.";
  }
  // The reconnect is heard, not only read: the suspect wire joins
  // the spoken guidance, and a bare generic covers the unreachable
  // tutor (message and suspect both empty) instead of silence.
  const reconnect = suspectReconnectSpeech(suspect);
  const say = [message, reconnect].filter(Boolean).join(" ") ||
    "The stream stopped. Push every wire firmly home — power and " +
    "ground first — then tap START.";
  logCoach("Coach", say);
  cookSpeak(say);
}

function enterRescue(reason, device) {
  if (appFault) return;
  appFault = true;
  rescueDevice = device || "";
  Orb.fault(true);
  Orb.monitor(false);
  const pretty = rescueDevice ? prettyDevice(rescueDevice) : "";
  paintCoachBar(rescueDevice
    ? "I lost the " + pretty + " — see the Rescue card."
    : "I lost the signal — see the Rescue card.");
  const card = $("rescue-card"), msg = $("rescue-msg"),
    susp = $("rescue-suspect");
  if (card) card.hidden = false;
  if (msg) msg.textContent = reason || "I lost the signal.";
  if (susp) susp.textContent = "Asking the tutor…";
  setDrawer("left", true);
  // A device latch names it and exonerates the healthy ones aloud;
  // a global latch keeps the plain sentence.
  let say = "Wait, I lost the signal. Let me check the last readings.";
  if (rescueDevice) {
    say = "Wait, I lost the signal from the " + pretty + ".";
    const healthy = healthyDevices().filter((t) => t !== rescueDevice);
    if (healthy.length) {
      say += " " + healthy.map(prettyDevice).join(" and ") +
        (healthy.length === 1 ? " is" : " are") + " still fine.";
    }
    say += " Let me check the last readings.";
  }
  logCoach("Coach", say);
  cookSpeak(say);
  requestRescueDiagnosis();
}

function exitRescue(backOnline) {
  if (!appFault) return;
  appFault = false;
  rescueDevice = "";
  Orb.fault(false);
  Orb.monitor(monitorOn);
  document.querySelectorAll("g.wire-fault")
    .forEach((g) => g.classList.remove("wire-fault"));
  const card = $("rescue-card");
  if (card) card.hidden = true;
  if (backOnline) paintCoachBar("Back online. Your build works.");
  else paintCoachBar();
  if (backOnline) {
    logCoach("Coach", "Back online.");
    cookSpeak("Got it! We're back online. Great job troubleshooting.");
  }
}

function onFinalFlashSuccess(port) {
  exitRescue(false);
  startMonitor(port || lastFlashPort);
}
$("monitor-stop").onclick = () => stopMonitor();
$("monitor-start").onclick = async () => {
  // Refresh first so a board plugged in since the drawer opened is
  // listed; the keep-first pick preserves an explicit selection.
  await refreshMonitorPorts();
  const sel = $("monitor-port");
  startMonitor((sel && sel.value) || await adoptMonitorPort());
};
$("monitor-refresh").onclick = () => refreshMonitorPorts();
$("rescue-complete").onclick = () => sendNextStep("mark complete");

async function refreshMonitorPorts() {
  // The monitor's own port picker. Learning firmware never answers
  // PING, so adoption alone cannot find a freshly flashed board: the
  // user picks the USB port they just flashed through. Mirrors the
  // deploy picker (keep-first, USB-preferred, never a phantom).
  const sel = $("monitor-port");
  if (!sel) return;
  let ports = [];
  try {
    const r = await fetch("/api/serial/ports");
    const v = await r.json();
    ports = v.ports || [];
  } catch (e) { /* keep the last options while the server is offline */ }
  const pick = pickFlashPort(ports, sel.value || lastFlashPort);
  const nameOf = (p) => (p && p.port) || p;
  const usable = (p) => usbishPort(p) || nameOf(p) === lastFlashPort;
  const pickEntry = ports.filter((p) => nameOf(p) === pick)[0];
  const usablePick = (pickEntry && usable(pickEntry)) ? pick : "";
  sel.innerHTML = "";
  const ph = document.createElement("option");
  ph.value = "";
  ph.textContent = ports.length ? "Select board port…" :
    "No ports found — plug in USB";
  sel.appendChild(ph);
  ports.forEach((p) => {
    const name = nameOf(p);
    if (!name) return;
    const o = document.createElement("option");
    o.value = name;
    const desc = (p && p.description) || "";
    o.textContent = desc ? name + " — " + desc : name;
    if (!usable(p)) {
      o.disabled = true;
      o.textContent += " (not usb)";
    }
    if (name === usablePick) o.selected = true;
    sel.appendChild(o);
  });
  sel.value = usablePick;
}
// monitor:end

// Check turns are gone: steps advance on taps (sendNextStep) and the
// build proves itself once, in the monitor. Guide-mode diagnosis keeps
// its own backend turn; the learning UI never checks.
function drawEvidence(frameFile, detections, mock) {
  // Evidence frame UI removed (Learning-first surface): no-op so the
  // guided render path can keep calling it harmlessly.
  return;
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
  paintFundi(null); // guided/diagnostic turns carry no lesson chapter
  const box = $("instruction");
  box.textContent = v.instruction || "(no instruction)";
  lastInstruction = v.instruction || "";
  box.classList.toggle("fault", v.state === "FAULT_FOUND");
  box.classList.toggle("done", v.state === "COMPLETE" || v.state === "VERIFIED_FIXED");
  if (v.speak) lastSpeak = v.speak;
  // Camera preview removed from the UI: frame sources are not shown.
  paintChips($("state-line"), [
    { value: v.state || "—", cls: stateChipClass(v.state) },
    { value: v.mode || "—" },
    v.step ? { label: "step", value: (v.step_index + 1) + " " + v.step.step_id } : null,
    v.attempts_total ? { label: "attempts", value: v.attempts_total } : null,
    v.camera_source ? { value: v.camera_source } : null,
    v.frame_settled === false ? { value: "frame settling…", cls: "warn" } : null,
    v.timings ? { value: fmtTimings(v.timings) } : null,
  ]);
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

// Learning is the only mode on this surface. Guided/Diagnostic
// sessions and their startSession/render implementations remain in
// the codebase, unreachable from the UI.
mode = "LEARNING";

let learnParts = [], learnConns = [], learnUnc = [], learnProjects = [], learnSid = "";
let learnDone = false; // last learning view reached DONE (stops the auto loop)
let learnState = ""; // last learning view state (holds auto checks during ROLLCALL)
let learnStepN = 1; // current lesson step number (1-based) for STEP CODE
let learnTitle = "", learnStepTotal = 0; // resume bar + progress paint

// Step views fetch the schematic by learnStepN; on DONE the
// session sits past the last step (step null, step_index = total),
// so clamp to the final step — its schematic IS the full project,
// and no step/{total+1} fetch 404s.
function clampLearnStep(v) {
  learnStepN = (v.step && v.step.n) || (v.step_index || 0) + 1;
  const total = v.steps_total || 0;
  if (total > 0 && learnStepN > total) learnStepN = total;
  if (learnStepN < 1) learnStepN = 1;
}
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
  const card = (num, title, summary, missing, attrs) =>
    '<li ' + attrs + '><span class="card-top"><b>' + num + esc(title) +
    '</b><span class="start-pill" aria-hidden="true">Start →</span></span>' +
    '<br><span class="fine">' + esc(summary || "") + "</span>" +
    (missing && missing.length
      ? "<br><span class='fine need'>needs: " + esc(missing.join(", ")) + "</span>"
      : "") + "</li>";
  $("learn-cards").innerHTML = learnProjects.map((p, i) => {
    const num = learnProjects.length > 1 ? (i + 1) + ". " : "";
    const main = card(num, p.title || "project", p.summary || "",
                      p.missing_parts, 'data-p="' + i + '"');
    const alts = (p.alternatives || []).map((a, j) =>
      card("Alt " + (j + 1) + ". ", a.title || "alternative",
           a.summary || "", a.missing_parts,
           'data-p="' + i + '" data-a="' + j + '"')
    ).join("");
    return main + alts;
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
    Orb.ok();
  } else if (ev === "ROLLCALL") {
    banner.textContent = "Roll call — gather your parts.";
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
  paintRollcall(v);
  paintFundi(v.fundi);
  const freshLearn = v.id && v.id !== learnSid;
  if (v.id) learnSid = v.id;
  clampLearnStep(v);
  learnDone = v.state === "DONE";
  learnState = v.state || "";
  paintCoachBar(v.instruction);
  maybeOpenDrawer(v);
  if (freshLearn && !cookMode) {
    // New build, no buttons: say the loop out loud once.
    $("talk-hint").textContent =
      "Wire along with the tutor — tap a project card to start building.";
  }
  const box = $("instruction");
  box.textContent = v.instruction || "(no instruction)";
  lastInstruction = v.instruction || "";
  box.classList.toggle("fault", v.state === "VERIFYING");
  box.classList.toggle("done", v.state === "DONE");
  if (v.speak) lastSpeak = v.speak;
  paintChips($("state-line"), [
    { value: v.state || "—", cls: stateChipClass(v.state) },
    { value: "LEARNING" },
    v.project_title ? { value: v.project_title } : null,
    v.board ? { value: v.board } : null,
    { label: "step", value: Math.min(v.step_index + 1, v.steps_total) + "/" + v.steps_total },
    v.timings ? { value: fmtTimings(v.timings) } : null,
  ]);
  learnTitle = v.project_title || "";
  learnStepTotal = v.steps_total || 0;
  const prog = $("learn-progress");
  if (learnStepTotal > 0) {
    prog.hidden = false;
    prog.value = v.state === "DONE" ? 100
      : Math.round(100 * (v.steps_passed || 0) / learnStepTotal);
  } else {
    prog.hidden = true;
  }
  paintResumeBar();
  const ver = v.verification || {};
  let html = ver.test
    ? "test: " + esc(ver.test) + " → " + (ver.passed ? "PASS" : "FAIL") +
      "\nmeasured: " + esc(ver.measured || "")
    : "no evidence yet — the monitor proves the finished build";
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
  paintTroubleshoot("learn-troubleshoot", (v.fundi || {}).troubleshoot);
  paintStretch("learn-stretch", (v.fundi || {}).stretch);
  refreshSchematics();
}

function enterLearning(v) {
  mode = "LEARNING";
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
  warmServerVoice(); // prime the server voice while planning runs
  const want = ($("learn-project").value || "").trim();
  if (!learnParts.length && !want) {
    $("talk-hint").textContent = "Upload a parts photo or name a project first.";
    return;
  }
  busy = true;
  Orb.thinkStart(); // orb enters think/planning mode
  learnBoard = $("learn-board").value || "esp32_devkit";
  const key = planCacheKey(learnParts, want, learnBoard, learnConns, learnUnc);
  const renderPlans = (projects, hint) => {
    renderLearnCards(projects || []);
    const head = $("learn-results-head");
    if (!learnProjects.length) {
      head.textContent = "";
      $("instruction").textContent = hint || "No buildable project found.";
      speak(hint || "I could not plan a project with these parts.");
      return;
    }
    head.textContent = learnProjects.length === 1
      ? "Found your project — tap it to start building."
      : learnProjects.length + " projects found — tap one to start building.";
    const reduced = window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    $("learn-cards").scrollIntoView({
      behavior: reduced ? "auto" : "smooth", block: "nearest",
    });
    const titles = learnProjects.map((p) => p.title).join("; ");
    $("instruction").textContent =
      (learnProjects.length === 1 ? "Tap the project to start: "
                                  : "Tap a project to start: ") + titles +
      (hint ? " Note: " + hint : "");
    speak(projectChoiceSpeech(learnProjects) +
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
    // No bridging ack: the Ns ticker covers the planning wait and
    // the first word lands in the tutor voice (one voice).
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
  finally { if (tick) clearInterval(tick); busy = false; Orb.thinkEnd(); }
}

async function startLearning(project) {
  if (busy) return;
  warmServerVoice(); // prime the server voice while the reveal renders
  busy = true;
  Orb.thinkStart(); // planning/loading a project
  learnBoard = $("learn-board").value || "esp32_devkit";
  const startLabel = "Starting " + (project.title || "project");
  const startT0 = Date.now();
  $("instruction").textContent = startLabel + "…";
  // No bridging ack on tap: the orb + ticker acknowledge it, and
  // the reveal line arrives in the tutor voice (one voice).
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
    // One natural opening line; the parts inventory stays on screen
    // (guide + parts list) instead of being read out like a script.
    v.speak = "Let's build " + (project.title || "this project") + ". " +
      (v.speak || v.instruction || "");
    enterReveal(v, project);
  } catch (e) { showError(e.message); }
  finally { clearInterval(startTick); busy = false; Orb.thinkEnd(); }
}

// choice:prompt:start
function projectChoiceSpeech(projects) {
  // The prompt matches the actual count: one project is picked by name
  // or tap (a "number" means nothing yet); several add number picking.
  const list = projects || [];
  const titles = list.map((p) => p.title).join("; ");
  if (list.length === 1) {
    return "I found one project: " + titles +
      ". Tap it to start, or say its name.";
  }
  return "I found " + list.length + " projects: " + titles +
    ". Say its name or number, or tap one to start.";
}
// choice:prompt:end

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
    hintEl: $("talk-hint"),
    hintListening: learnProjects.length === 1 ? "Say its name…"
                                             : "Say the name or number…",
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

$("learn-suggest").onclick = () => suggestProjects();
$("learn-project").onkeydown = (e) => {
  if (e.key === "Enter") { e.preventDefault(); suggestProjects(); }
};
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
  if (!learnSid) return;
  // A manual tap mid-turn must say something (screen only: the live
  // turn owns the audio). Auto fetches stay silent on overlap.
  if (busy) {
    if (!auto) $("talk-hint").textContent = "Still working — press SEND CODE again in a moment.";
    return;
  }
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
      paintStageCodeError(msg + " — Send to Board retries.");
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
    paintStageCode(v.code || "", $("code-kind").textContent);
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

// Per-step wiring schematic (Learning mode, ESP32 only). The panel loads
// quietly with each step render; SPEAK WIRES reads the wire list aloud
// and DOWNLOAD SVG saves the drawing. Failures stay local to the panel.
let schematicState = { svg: "", breadboardSvg: "", wires: [], n: 0, note: "", sid: "" };
let cookSchematicState = { svg: "", breadboardSvg: "", wires: [], n: 0, note: "", sid: "" };
let schematicBusy = false;
let schematicQueued = null;
let schematicView = "cards";
try { schematicView = localStorage.getItem("fundiloop-schematic-view") || "cards"; } catch (e) { /* private mode */ }
if (schematicView !== "breadboard") schematicView = "cards";

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

function schematicMarkup(state, view) {
  const v = view || schematicView;
  if (v === "breadboard" && state.breadboardSvg) return state.breadboardSvg;
  return state.svg || "";
}

// Per-tab renderer: the Diagram tab always shows pin cards, the
// Breadboard tab always shows the breadboard view. The legacy global
// view flag only moves the toggle buttons, never the drawings.
function schematicMarkupFor(svgId, state) {
  if (svgId === "cook-schematic-svg") return state.breadboardSvg || state.svg || "";
  return state.svg || "";
}

function syncSchematicViewButtons() {
  document.querySelectorAll(".schematic-view-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.getAttribute("data-view") === schematicView);
  });
}

function hideWireStroke(p) {
  let len = 180;
  try { if (p.getTotalLength) len = Math.max(1, p.getTotalLength()); }
  catch (e) { /* some SVG geometry has no length */ }
  p.style.strokeDasharray = String(len);
  p.style.strokeDashoffset = String(len);
  p.style.animation = "none";
}

function playSchematicDraw(svgId, stepN, prevN) {
  const box = $(svgId);
  const svg = box && box.querySelector("svg");
  if (!svg) return;
  const stepOf = (el) => {
    const n = +el.getAttribute("data-step");
    return Number.isFinite(n) && n > 0 ? n : stepN;
  };
  svg.querySelectorAll(".schematic-part, g[id^='part-']").forEach((g) => {
    if (stepOf(g) === stepN && stepOf(g) > prevN) g.classList.add("schematic-enter");
  });
  const groups = [];
  svg.querySelectorAll(".schematic-wire, g[id^='wire-']").forEach((g) => {
    if (stepOf(g) !== stepN || stepOf(g) <= prevN) return;
    groups.push(g);
    g.querySelectorAll("path, line").forEach(hideWireStroke);
  });
  // Animate in the order the tutor names the wires, not the order the
  // drawing stacks them top-to-bottom: a wire spoken last must move
  // last, wherever it sits on screen. Unnamed wires keep drawing
  // order, after every named one.
  const drawState = (svgId === "cook-schematic-svg")
    ? cookSchematicState : schematicState;
  const spokenText = String(lastSpeak || lastInstruction || "");
  const spokenWireOrder = (g, docIdx) => {
    const wire = (drawState.wires || []).find(
      (w) => schematicWireId(w) === g.getAttribute("id"));
    const titleEl = g.querySelector("title");
    const needles = wire
      ? [wire.board_pin, wire.part_pin, wire.part]
      : [(titleEl && titleEl.textContent) || ""];
    for (const needle of needles) {
      if (!needle) continue;
      const at = spokenText.indexOf(String(needle));
      if (at >= 0) return at;
    }
    return Number.MAX_SAFE_INTEGER - 10000 + docIdx;
  };
  const ordered = groups
    .map((g, i) => [spokenWireOrder(g, i), i, g])
    .sort((a, b) => (a[0] - b[0]) || (a[1] - b[1]))
    .map((entry) => entry[2]);
  parkedDraw = { svgId: svgId, groups: ordered, parkedAt: Date.now() };
  if (muted || speechAudible) {
    runParkedSchematicDraw();
    return;
  }
  scheduleParkedDrawFallback();
}

let parkedFallbackTimer = null;

function scheduleParkedDrawFallback() {
  // The draw paces itself to the tutor's voice (markSpeechAudible runs
  // it). Only fall back to untimed drawing when no speech for this step
  // is on its way — a cold first-phase TTS can take seconds to sound,
  // and drawing at default speed meanwhile finishes before the tutor
  // has said the wires. Later steps sound promptly, so they never wait.
  if (parkedFallbackTimer) clearTimeout(parkedFallbackTimer);
  const job = parkedDraw;
  if (!job) return;
  parkedFallbackTimer = setTimeout(() => {
    parkedFallbackTimer = null;
    if (parkedDraw !== job) return;
    const speechPending = !muted && !speechAudible && speechClockMs &&
      speechClockSeq === speakSeq;
    if (speechPending && Date.now() - job.parkedAt < 25000) {
      scheduleParkedDrawFallback();
      return;
    }
    runParkedSchematicDraw();
  }, 2000);
}

function runParkedSchematicDraw() {
  const job = parkedDraw;
  if (!job) return;
  parkedDraw = null;
  if (parkedFallbackTimer) {
    clearTimeout(parkedFallbackTimer);
    parkedFallbackTimer = null;
  }
  const groups = job.groups || [];
  const n = groups.length;
  if (!n) return;
  const remain = speechRemainMs();
  let startPad = 0.35, slot = 1.8, dur = 1.5;
  if (remain > 900) {
    // Hold the wires until the speech reaches its first wire mention
    // (preamble first), then spread them over what is left of it.
    const leadMs = Math.min(remain * 0.6, speechLeadFrac * remain);
    startPad = 0.3 + leadMs / 1000;
    const availMs = Math.max(800, remain - leadMs - 400);
    slot = Math.max(1.2, (availMs / 1000) / n);
    dur = Math.max(1.2, Math.min(4, slot * 0.9));
  }
  groups.forEach((g, i) => {
    g.classList.add("schematic-drawing");
    g.querySelectorAll("path, line").forEach((p) => {
      p.style.animation = "";
      p.style.setProperty("--wire-dur", dur + "s");
      p.style.animationDelay = (startPad + i * slot) + "s";
    });
    g._followAt = Date.now() + (startPad + i * slot) * 1000;
    g._followDur = dur;
  });
  // Follow mode: as each spoken chunk actually sounds, its wires
  // re-time to NOW (the spread above is only the fallback schedule).
  followJob = { groups: groups.slice(), drawn: new Set(), svgId: job.svgId };
}

let followJob = null;

function animateWireNow(g, delaySec) {
  g.classList.remove("schematic-drawing");
  g.querySelectorAll("path, line").forEach((p) => {
    p.style.animation = "none";
  });
  requestAnimationFrame(() => requestAnimationFrame(() => {
    g.querySelectorAll("path, line").forEach((p) => {
      p.style.animation = "";
      p.style.setProperty("--wire-dur", "1.15s");
      p.style.animationDelay = delaySec + "s";
    });
    g.classList.add("schematic-drawing");
  }));
}

function followWireNeedles(g, svgId) {
  const state = (svgId === "cook-schematic-svg")
    ? cookSchematicState : schematicState;
  const wire = (state.wires || []).find(
    (w) => schematicWireId(w) === g.getAttribute("id"));
  if (!wire) return [];
  const needles = [wire.board_pin, wire.part_pin, wire.part];
  if (wire.board_pin === "GND") needles.push("GROUND");
  return needles.filter(Boolean);
}

function followSpokenChunk(chunkText) {
  if (!followJob) return;
  const norm = (s) => String(s || "").toUpperCase()
    .replace(/[^A-Z0-9+]+/g, "");
  const hay = norm(chunkText);
  if (!hay) return;
  let slot = 0;
  followJob.groups.forEach((g) => {
    if (followJob.drawn.has(g)) return;
    if (g._followAt && Date.now() > g._followAt + (g._followDur || 1) * 1000) {
      followJob.drawn.add(g); // fallback schedule already drew it
      return;
    }
    const hit = followWireNeedles(g, followJob.svgId).some((nd) => {
      const n = norm(nd);
      return n.length >= 2 && hay.includes(n);
    });
    if (hit) {
      animateWireNow(g, slot * 0.3);
      followJob.drawn.add(g);
      slot++;
    }
  });
}

function flushFollowDraw() {
  if (!followJob) return;
  const job = followJob;
  followJob = null;
  let slot = 0;
  job.groups.forEach((g) => {
    if (job.drawn.has(g)) return;
    if (g._followAt && Date.now() > g._followAt + (g._followDur || 1) * 1000) {
      return; // already drawn on the fallback schedule
    }
    animateWireNow(g, slot * 0.25); // narration ended: land the rest
    slot++;
  });
}

speechChunkHook = followSpokenChunk;

function paintSchematic(svgId, selId, state, animate, prevN) {
  const box = $(svgId);
  if (!box) return;
  box.innerHTML = schematicMarkupFor(svgId, state);
  applySchematicZoom(svgId); // repaints keep the user's zoom
  box.removeAttribute("data-sel");
  if (selId && $(selId)) $(selId).textContent = "";
  if (svgId === "schematic-svg" && $("schematic-svg-drawer")) {
    $("schematic-svg-drawer").innerHTML = box.innerHTML; // drawer mirrors the diagram
  }
  if (animate) playSchematicDraw(svgId, state.n, prevN || 0);
  // Hide the empty-state placeholder once an SVG is present
  const emptyId = svgId === "cook-schematic-svg" ? "breadboard-empty" : "diagram-empty";
  const emptyEl = document.getElementById(emptyId);
  if (emptyEl && box.querySelector("svg")) emptyEl.hidden = true;
  // Also show the toolbar (download/speak buttons)
  const toolbarId = svgId === "cook-schematic-svg" ? "cook-schematic-toolbar" : "schematic-toolbar";
  const toolbar = document.getElementById(toolbarId);
  if (toolbar) toolbar.hidden = false;
}

function setSchematicView(view) {
  schematicView = (view === "breadboard") ? "breadboard" : "cards";
  try { localStorage.setItem("fundiloop-schematic-view", schematicView); } catch (e) { /* private mode */ }
  syncSchematicViewButtons();
  // Tabs own the views now: the toggle moves to the matching tab.
  switchStageTab(schematicView === "breadboard" ? "breadboard" : "diagram");
  if (schematicState.svg) paintSchematic("schematic-svg", "schematic-sel", schematicState, false, 0);
  if (cookSchematicState.svg || cookSchematicState.breadboardSvg) paintSchematic("cook-schematic-svg", "cook-schematic-sel", cookSchematicState, false, 0);
  paintWireBeat(); // full repaints strip beat classes — restore them
}

async function fetchSchematic(svgId, wiresId, state, selId, stepN) {
  if (!learnSid) return;
  const wantSid = learnSid, wantN = stepN || learnStepN;
  if (state.sid === wantSid && state.n === wantN && state.svg) return;
  if (schematicBusy) {
    schematicQueued = { svgId: svgId, wiresId: wiresId, state: state,
      selId: selId, stepN: wantN };
    return;
  }
  schematicBusy = true;
  state.fetchToken = (state.fetchToken || 0) + 1;
  const mine = state.fetchToken; // stale fetches never paint over newer ones
  const prevN = (state.sid === wantSid) ? (state.n || 0) : 0;
  try {
    const r = await fetch("/project/" + encodeURIComponent(wantSid) +
      "/step/" + wantN + "/schematic");
    const v = await r.json();
    if (state.fetchToken !== mine || learnSid !== wantSid) return;
    if (!r.ok) {
      $(wiresId).textContent = "Schematic unavailable: " +
        ((v && v.detail) || "unknown error");
      return;
    }
    const net = v.netlist || {};
    state.svg = v.svg || "";
    state.breadboardSvg = v.breadboard_svg || "";
    state.wires = net.wires || [];
    state.unresolved = v.unresolved || [];
    state.n = v.n || wantN;
    state.sid = wantSid;
    state.note = ((net.notes || [])[0]) || "";
    paintSchematic(svgId, selId, state, svgId === schematicActiveSvgId(), prevN);
    $(wiresId).textContent = wireListText(net.wires, v.unresolved, net);
    mirrorSchematic(svgId, state);
    if (svgId === "cook-schematic-svg") {
      buildWireBeats();
      paintWireBeat();
      paintCoachBar();
    }
  } catch (e) {
    $(wiresId).textContent = "Schematic failed: " + e.message;
  } finally {
    schematicBusy = false;
    if (schematicQueued) {
      const q = schematicQueued;
      schematicQueued = null;
      fetchSchematic(q.svgId, q.wiresId, q.state, q.selId, q.stepN);
    }
  }
}

// One fetch feeds both tabs: same netlist, per-tab renderer. Whichever
// container fetched, the sibling tab paints the same step immediately,
// so Diagram and Breadboard can never disagree about the step.
function schematicActiveSvgId() {
  // The draw animation parks one job globally: only the visible tab
  // animates, the hidden one paints statically. Otherwise the second
  // paint orphans the first job and its fresh wires stay invisible.
  const active = document.querySelector && document.querySelector(".stage-tab.active");
  const tab = active && active.getAttribute && active.getAttribute("data-tab");
  return tab === "diagram" ? "schematic-svg" : "cook-schematic-svg";
}

function mirrorSchematic(svgId, state) {
  const snap = { svg: state.svg, breadboardSvg: state.breadboardSvg,
    wires: state.wires, unresolved: state.unresolved || [], n: state.n,
    sid: state.sid, note: state.note };
  const wireText = (st) => wireListText(st.wires, st.unresolved,
    { notes: st.note ? [st.note] : [] });
  if (svgId === "cook-schematic-svg") {
    Object.assign(schematicState, snap);
    paintSchematic("schematic-svg", "schematic-sel", schematicState,
      schematicActiveSvgId() === "schematic-svg", 0);
    if ($("schematic-wires")) $("schematic-wires").textContent = wireText(schematicState);
    if ($("schematic-wires-drawer")) $("schematic-wires-drawer").textContent = wireText(schematicState);
  } else if (svgId === "schematic-svg") {
    Object.assign(cookSchematicState, snap);
    paintSchematic("cook-schematic-svg", "cook-schematic-sel", cookSchematicState,
      schematicActiveSvgId() === "cook-schematic-svg", 0);
    if ($("cook-schematic-wires")) $("cook-schematic-wires").textContent = wireText(cookSchematicState);
    buildWireBeats();
    paintWireBeat();
    paintCoachBar();
  }
}

function refreshSchematics() {
  if (!learnSid) return;
  refreshFlashPorts();
  // During the reveal the full-project diagram is loading (or loaded);
  // a step fetch now would race it and clobber the reveal.
  if (cookMode && !appRevealed) return;
  if (cookMode) fetchSchematic("cook-schematic-svg", "cook-schematic-wires", cookSchematicState, "cook-schematic-sel");
  else fetchSchematic("schematic-svg", "schematic-wires", schematicState, "schematic-sel");
}

// Click a wire to isolate it (rest dim, selection described below);
// click it again, or empty canvas, to restore. Hover tooltips come
// from the SVG <title> elements, no JS needed for those.
function schematicWireId(w) {
  return ("wire-" + w.part + "-" + w.part_pin).replace(/[^A-Za-z0-9_-]+/g, "_");
}

function schematicPartId(part) {
  return ("part-" + part).replace(/[^A-Za-z0-9_-]+/g, "_");
}

// A wire-speech sequence owns narration until it ends or something newer
// (a check, a tap, exit) cancels it. wireSpeech is the live token; any
// scheduled phrase whose token mismatches drops silently.
let wireSpeech = null;

function cancelWireSpeech() { wireSpeech = null; }

// A named wire must always be visible: if a parked draw animation
// never ran for it (orphaned job, missed cue), its paths sit hidden
// behind dash-offset while its dots show. Naming it draws it now.
function ensureWireDrawn(svgId, id) {
  if (!id) return;
  const box = $(svgId);
  const g = box && box.querySelector && box.querySelector('g[id="' + id + '"]');
  if (!g) return;
  g.classList.remove("schematic-drawing");
  g.querySelectorAll("path, line").forEach((p) => {
    p.style.strokeDasharray = "none";
    p.style.strokeDashoffset = "0";
    p.style.animation = "none";
  });
  if (followJob) followJob.drawn.add(g);
}

// Karaoke narration: speak phrases in order, lighting each wire on screen
// as it is named, so the diagram follows the tutor clause by clause.
// Later phrases wait for the earlier phrase's audio to finish (onend).
// A newer speech from anywhere else (speakSeq moved) or an explicit
// cancel retires the sequence; its late onend drops silently.
function speakWireList(items, say, svgId, selId) {
  const mine = {};
  wireSpeech = mine;
  const queue = (items || []).filter((it) => it && it.phrase);
  let i = 0;
  let lastSeq = speakSeq;
  const step = () => {
    if (wireSpeech !== mine || speakSeq !== lastSeq) {
      if (wireSpeech === mine) wireSpeech = null;
      return;
    }
    if (i >= queue.length) {
      wireSpeech = null;
      isolateSchematicWires(svgId, selId, [], "");
      return;
    }
    const it = queue[i++];
    if (it.id) {
      isolateSchematicWires(svgId, selId, [it.id], it.label || "");
      ensureWireDrawn(svgId, it.id);
    }
    say(it.phrase, step);
    lastSeq = speakSeq;
  };
  // Drop any tap highlight first: isolate toggles a repeated key off, and
  // without this a tapped wire named first would go dark instead of lit.
  isolateSchematicWires(svgId, selId, [], "");
  step();
}

function wireSpeechItems(wires, n) {
  const all = wires || [];
  const items = all.slice(0, 16).map((w) => ({
    id: schematicWireId(w),
    phrase: w.part + " " + w.part_pin + " to " + w.board_pin,
    label: w.part + "." + w.part_pin + " → " + w.board_pin +
      " (added step " + (w.step_added || n || "?") + ")",
  }));
  if (all.length > 16) {
    items.push({ id: "", phrase: "and " + (all.length - 16) + " more wires, shown on screen" });
  }
  return items;
}

function isolateSchematicWires(svgId, selId, ids, label) {
  const box = $(svgId);
  if (!box) return;
  const key = ids.slice().sort().join(",");
  const prev = box.getAttribute("data-sel") || "";
  box.querySelectorAll('g[id^="wire-"]').forEach((row) => {
    row.style.opacity = "";
  });
  if (!key || key === prev) {
    box.removeAttribute("data-sel");
    if ($(selId)) $(selId).textContent = "";
    return;
  }
  box.setAttribute("data-sel", key);
  box.querySelectorAll('g[id^="wire-"]').forEach((row) => {
    row.style.opacity = (ids.indexOf(row.getAttribute("id")) >= 0) ? "1" : "0.12";
  });
  if ($(selId)) $(selId).textContent = label;
}

function wireSchematicClicks(svgId, selId, state) {
  const box = $(svgId);
  if (!box) return;
  box.addEventListener("click", (e) => {
    cancelWireSpeech(); // a manual tap wins over narration
    const g = (e.target && e.target.closest)
      ? e.target.closest('g[id^="wire-"]') : null;
    const cur = g ? g.getAttribute("id") : "";
    if (!cur) {
      isolateSchematicWires(svgId, selId, [], "");
      return;
    }
    const wire = (state.wires || []).find((w) => schematicWireId(w) === cur);
    isolateSchematicWires(svgId, selId, [cur], wire
      ? wire.part + "." + wire.part_pin + " → " + wire.board_pin +
        " (added step " + (wire.step_added || state.n || "?") + ")"
      : cur);
  });
}

wireSchematicClicks("schematic-svg", "schematic-sel", schematicState);
wireSchematicClicks("cook-schematic-svg", "cook-schematic-sel", cookSchematicState);

// Schematik-style build guide: parts to gather plus numbered assembly
// cards. The guide loads once per session; the current step's card is
// marked as the build advances. Tapping a card narrates it wire by
// wire, lighting each wire on the diagram as it is named.
let guideState = { sid: "", parts: [], steps: [] };

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function fetchGuide() {
  if (!learnSid) return;
  if (guideState.sid === learnSid) {
    markGuideStep();
    return;
  }
  try {
    const r = await fetch("/project/" + encodeURIComponent(learnSid) + "/guide");
    const v = await r.json();
    if (!r.ok) {
      $("cook-guide-panel").hidden = true;
      return;
    }
    guideState = { sid: learnSid, parts: v.parts || [], steps: v.steps || [] };
    renderGuide();
  } catch (e) {
    $("cook-guide-panel").hidden = true;
  }
}

function renderGuide() {
  $("cook-guide-panel").hidden = false;
  $("cook-guide-parts").innerHTML = guideState.parts.map((p) =>
    '<div class="guide-part"><span class="guide-qty">' + p.qty + "×</span> " +
    "<strong>" + escapeHtml(p.part) + "</strong> — " + escapeHtml(p.blurb) + "</div>"
  ).join("");
  const ol = $("cook-guide-steps");
  ol.innerHTML = "";
  guideState.steps.forEach((s) => {
    const li = document.createElement("li");
    li.dataset.n = s.n;
    li.innerHTML = '<span class="guide-num">' + s.n + "</span> " +
      "<strong>" + escapeHtml(s.title) + "</strong>" +
      (s.teach ? "<div>" + escapeHtml(s.teach) + "</div>" : "") +
      "<div>" + escapeHtml(s.text) + "</div>";
    li.onclick = () => speakGuideStep(s);
    ol.appendChild(li);
  });
  markGuideStep();
}

function stepIntroPhrase(s) {
  // Say the step once: a chapter title that already IS "Step 2" must
  // not become "Step 2: Step 2".
  const title = String((s && s.title) || "").trim();
  if (!title) return "Step " + s.n;
  if (/^step\s+\d+/i.test(title)) return title;
  return "Step " + s.n + ": " + title;
}

// Tap a guide card: narrate it wire by wire, lighting each wire on the
// diagram as it is named. Check cards (no wires) speak as one turn.
function speakGuideStep(s) {
  const byId = {};
  (cookSchematicState.wires || []).forEach((w) => { byId[schematicWireId(w)] = w; });
  const wires = (s.wire_ids || []).map((id) => byId[id]).filter(Boolean);
  if (!wires.length) {
    cancelWireSpeech();
    cookSpeak(s.title + ". " + (s.teach ? s.teach + " " : "") + s.text);
    return;
  }
  // Teach first (what this step is and why), then wire by wire.
  const intro = [{ id: "", phrase: stepIntroPhrase(s) }];
  if (s.teach) intro.push({ id: "", phrase: s.teach });
  speakWireList(
    intro.concat(wireSpeechItems(wires, s.n)),
    cookSpeak, "cook-schematic-svg", "cook-schematic-sel");
}

function markGuideStep() {
  document.querySelectorAll("#cook-guide-steps li").forEach((li) => {
    li.classList.toggle("active", +li.dataset.n === learnStepN);
  });
}

function downloadSchematic(state, kind) {
  const svg = kind === "breadboard"
    ? (state.breadboardSvg || state.svg || "")
    : (state.svg || "");
  if (!svg) return;
  const blob = new Blob([svg], { type: "image/svg+xml" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "step-" + (state.n || learnStepN) +
    (kind === "breadboard" ? "-breadboard.svg" : "-schematic.svg");
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

$("schematic-download").onclick = () => downloadSchematic(schematicState, "cards");
$("cook-schematic-download").onclick = () => downloadSchematic(cookSchematicState, "breadboard");
if ($("schematic-download-drawer")) $("schematic-download-drawer").onclick = () => downloadSchematic(schematicState, "cards");
if ($("cook-schematic-download-overflow")) $("cook-schematic-download-overflow").onclick = () => downloadSchematic(cookSchematicState, "breadboard");
document.querySelectorAll(".schematic-view-btn").forEach((btn) => {
  btn.onclick = () => setSchematicView(btn.getAttribute("data-view"));
});
syncSchematicViewButtons();

// ── Stage tab switcher ──────────────────────────────────────────────
// The three tabs (Diagram / Breadboard / Code) switch the right-hand
// stage panel. switchStageTab() is also called programmatically to
// auto-navigate as the build progresses.
let stageCodeSid = ""; // session whose firmware fills the Code tab
function paintStageCode(code, kindLabel) {
  if ($("stage-code-out")) $("stage-code-out").textContent = code || "";
  if ($("stage-code-kind")) $("stage-code-kind").textContent = kindLabel || "";
  stageCodeSid = learnSid;
}
function paintStageCodeError(msg) {
  // The Code tab explains itself instead of sitting blank; the last
  // good code (if any) stays on screen and the sid stays stale so the
  // next open retries.
  if ($("stage-code-kind")) $("stage-code-kind").textContent = msg;
}
function switchStageTab(tabName) {
  const panels = {
    diagram:     "stage-diagram",
    breadboard:  "stage-breadboard",
    code:        "stage-code-panel",
  };
  document.querySelectorAll(".stage-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.getAttribute("data-tab") === tabName);
  });
  Object.entries(panels).forEach(([name, id]) => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle("active", name === tabName);
  });
  // The Code tab fetches its draft on first open (once per session),
  // so it is never an empty panel mid-build.
  if (tabName === "code" && learnSid && stageCodeSid !== learnSid) {
    if (cookMode) cookSendCode(true); else fetchCode(true);
  }
}
document.querySelectorAll(".stage-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    switchStageTab(btn.getAttribute("data-tab"));
  });
});
// The stored view predates tabs: open the matching tab on first load so
// the toggle highlight and the visible tab agree.
if (schematicView === "breadboard") switchStageTab("breadboard");

// ── Schematic zoom ────────────────────────────────────────────────
// Each tab zooms independently; paintSchematic re-applies the zoom so
// step changes keep the adjustment. Zoom scales the SVG width; the
// canvas scrolls to pan. Range 50–300%, both tabs come up at 50% so
// the whole circuit fits, and Fit restores that default.
const schematicZoom = { "schematic-svg": 0.5, "cook-schematic-svg": 0.5 };
function schematicZoomLabelId(svgId) {
  return svgId === "cook-schematic-svg" ? "cook-schematic-zoom-label" : "schematic-zoom-label";
}
function applySchematicZoom(svgId) {
  const z = schematicZoom[svgId] || 0.5;
  const box = $(svgId);
  const svg = box && box.querySelector("svg");
  if (svg) svg.style.width = Math.round(z * 100) + "%";
  const label = $(schematicZoomLabelId(svgId));
  if (label) label.textContent = Math.round(z * 100) + "%";
}
function setSchematicZoom(svgId, z) {
  schematicZoom[svgId] = Math.min(3, Math.max(0.5, z));
  applySchematicZoom(svgId);
}
function resetSchematicZoom() {
  setSchematicZoom("schematic-svg", 0.5);
  setSchematicZoom("cook-schematic-svg", 0.5);
}
$("schematic-zoom-out").onclick = () => setSchematicZoom("schematic-svg", (schematicZoom["schematic-svg"] || 0.5) - 0.25);
$("schematic-zoom-in").onclick = () => setSchematicZoom("schematic-svg", (schematicZoom["schematic-svg"] || 0.5) + 0.25);
$("schematic-zoom-fit").onclick = () => setSchematicZoom("schematic-svg", 0.5);
$("cook-schematic-zoom-out").onclick = () => setSchematicZoom("cook-schematic-svg", (schematicZoom["cook-schematic-svg"] || 0.5) - 0.25);
$("cook-schematic-zoom-in").onclick = () => setSchematicZoom("cook-schematic-svg", (schematicZoom["cook-schematic-svg"] || 0.5) + 0.25);
$("cook-schematic-zoom-fit").onclick = () => setSchematicZoom("cook-schematic-svg", 0.5);
function speakSchematicWires() {
  if (schematicState.wires.length) speakWireList(
    [{ id: "", phrase: "Step " + schematicState.n + " wiring" }]
      .concat(wireSpeechItems(schematicState.wires, schematicState.n)),
    speak, "schematic-svg", "schematic-sel");
  else if (schematicState.note) speak(schematicState.note);
}
function speakCookSchematicWires() {
  // Replays the flowing step summary; during the reveal (no beats yet)
  // it narrates the full project the diagram shows.
  const beatWires = wireBeats.map((b) => b.wire).filter(Boolean);
  const wires = beatWires.length ? beatWires : (cookSchematicState.wires || []);
  const items = stepSummaryItems(wires, cookSchematicState.n || learnStepN,
    revealBoardName(learnBoard));
  if (items.length) {
    cancelBeatSpeech();
    speakWireList(items, cookSpeak, "cook-schematic-svg", "cook-schematic-sel");
  } else if (cookSchematicState.note) cookSpeak(cookSchematicState.note);
}
$("schematic-speak").onclick = speakSchematicWires;
$("cook-schematic-speak").onclick = speakCookSchematicWires;
if ($("schematic-speak-drawer")) $("schematic-speak-drawer").onclick = speakSchematicWires;
if ($("cook-schematic-speak-overflow")) $("cook-schematic-speak-overflow").onclick = speakCookSchematicWires;
$("stage-copy-code").onclick = () => copyCode("stage-code-out", cookMode ? "cook-heard" : "talk-hint");
$("stage-send-code").onclick = () => { if (cookMode) cookSendCode(false); else fetchCode(false); };

// Shared flash guard. Deploy (FLASH FINAL) calls the server's native
// Python esptool at 921600 baud, matching Arduino IDE's auto-reset
// upload.
let flashBusy = false;

function setFlashButtonsDisabled(disabled) {
  ["flash-final", "cook-deploy", "flash-port", "cook-flash-port",
   "web-connect", "web-flash", "cook-web-connect"].forEach((id) => {
    if ($(id)) $(id).disabled = disabled;
  });
}

// flash:ports:start
function usbishPort(p) {
  // Mirrors the backend's _is_usb_port: USB enumeration carries
  // "USB" in the hwid or description; Bluetooth links (BTHENUM)
  // and legacy UARTs do not. Bare names carry no metadata, so
  // they stay pickable (explicit/test shapes, never phantoms).
  if (typeof p === "string") return true;
  const text = ((p && p.description) || "") + " " + ((p && p.hwid) || "");
  if (!text.trim()) return true;
  return /usb/i.test(text);
}
function pickFlashPort(ports, keep) {
  // Dynamic port pick, never a phantom default: keep the selection
  // while the OS still enumerates it; a lone USB serial adapter
  // (or lone bare name) is the connected board; a lone Bluetooth
  // or legacy port is a phantom, not a board; anything ambiguous
  // stays an explicit pick. ports: [{port, description, hwid}].
  const nameOf = (p) => (typeof p === "string" ? p : (p && p.port) || "");
  const names = (ports || []).map(nameOf).filter(Boolean);
  if (keep && names.indexOf(keep) >= 0) return keep;
  if (names.length === 1) {
    const only = (ports || []).filter((p) => nameOf(p))[0];
    if (usbishPort(only)) return names[0];
    return "";
  }
  const usb = (ports || []).filter((p) => typeof p !== "string" &&
    /usb/i.test(((p && p.description) || "") + " " + ((p && p.hwid) || "")));
  if (usb.length === 1) return nameOf(usb[0]);
  return "";
}
// flash:ports:end

async function refreshFlashPorts() {
  if (flashBusy) return;
  let ports = [], current = "";
  try {
    const r = await fetch("/api/serial/ports");
    const v = await r.json();
    ports = v.ports || [];
    current = (v.current || {}).port || "";
  } catch (e) { /* keep the last options while the server is offline */ }
  const names = ports.map((p) => (p && p.port) || p).filter(Boolean);
  // The server's current port is a candidate only while the OS
  // enumerates it on a USB adapter: a stale default — or an
  // enumerated Bluetooth/legacy phantom — must never preselect.
  const currentEntry = ports.filter(
    (p) => ((p && p.port) || p) === current)[0];
  const serverKeep = (names.indexOf(current) >= 0 &&
    usbishPort(currentEntry)) ? current : "";
  ["flash-port", "cook-flash-port"].forEach((id) => {
    const sel = $(id);
    if (!sel) return;
    const pick = pickFlashPort(ports, sel.value || serverKeep);
    sel.innerHTML = "";
    const ph = document.createElement("option");
    ph.value = "";
    ph.textContent = names.length ? "Select port…" : "No ports found";
    sel.appendChild(ph);
    ports.forEach((p) => {
      const name = (p && p.port) || p;
      if (!name) return;
      const o = document.createElement("option");
      o.value = name;
      const desc = (p && p.description) || "";
      o.textContent = desc ? name + " — " + desc : name;
      if (name === pick) o.selected = true;
      sel.appendChild(o);
    });
    sel.value = pick;
  });
}

// flash:sse:start
// Incremental SSE frame parser (pure: no DOM, safe under node).
// sseParse(buf) splits complete "\n\n"-terminated frames off the
// front, returning {frames: [{name, data}], rest} with any partial
// frame kept in rest for the next chunk.
function sseParse(buf) {
  const frames = [];
  let rest = String(buf || "");
  let cut;
  while ((cut = rest.indexOf("\n\n")) >= 0) {
    const frame = rest.slice(0, cut);
    rest = rest.slice(cut + 2);
    let name = "", data = "";
    for (const line of frame.split("\n")) {
      if (line.indexOf("event:") === 0) name = line.slice(6).trim();
      else if (line.indexOf("data:") === 0) data += line.slice(5).trim();
    }
    if (name || data) frames.push({ name: name, data: data });
  }
  return { frames: frames, rest: rest };
}
// flash:sse:end

// Flash terminal dialog: one console shared by server flash, browser
// flash, and final flash. Closing mid-stream aborts the wait (the
// server-side write runs to its timeout regardless); closing a
// browser write just hides the dialog while it finishes, with the
// panel status line carrying the same log.
let flashDialogAbort = null; // AbortController for the live stream, if any
function flashDialogOpen(title) {
  $("flash-title").textContent = title || "Flashing…";
  $("flash-console").textContent = "";
  const prog = $("flash-progress");
  prog.hidden = false;
  prog.removeAttribute("value"); // indeterminate until bytes flow
  $("flash-summary").textContent = "";
  $("flash-dialog").hidden = false;
}
function flashDialogLog(line) {
  if (line === undefined || line === null) return;
  const pre = $("flash-console");
  if (!pre) return;
  pre.textContent += String(line).replace(/\x1b\[[0-9;]*m/g, "") + "\n";
  const lines = pre.textContent.split("\n");
  if (lines.length > 400) pre.textContent = lines.slice(-400).join("\n");
  pre.scrollTop = pre.scrollHeight;
}
function flashDialogProgress(frac) {
  const prog = $("flash-progress");
  if (!prog) return;
  if (frac === null || frac === undefined || isNaN(frac)) {
    prog.removeAttribute("value");
    return;
  }
  prog.value = Math.round(100 * Math.min(1, Math.max(0, frac)));
}
function flashDialogDone(ok, summary) {
  flashDialogAbort = null;
  if (ok) {
    const prog = $("flash-progress");
    if (prog) prog.value = 100;
  }
  if (summary) $("flash-summary").textContent = summary;
}
function flashDialogClose() {
  if (flashDialogAbort) {
    try { flashDialogAbort.abort(); } catch (e) { /* already settled */ }
    flashDialogAbort = null;
  }
  $("flash-dialog").hidden = true;
}
$("flash-close").onclick = () => flashDialogClose();

// Pump one server flash stream into the dialog. Returns {ok, payload}
// on done, {ok: false, detail} on error/validation failure, or
// {cancelled: true} when the dialog was closed mid-stream.
async function pumpFlashStream(url, body) {
  const ctrl = new AbortController();
  flashDialogAbort = ctrl;
  let r;
  try {
    r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
  } catch (e) {
    flashDialogAbort = null;
    if (ctrl.signal.aborted) return { cancelled: true };
    throw e;
  }
  if (!r.ok || !r.body || !r.body.getReader) {
    flashDialogAbort = null;
    let detail = "flash failed";
    try {
      const v = await r.json();
      if (v && v.detail) detail = v.detail;
    } catch (e) { /* fall through with the default */ }
    return { ok: false, detail: detail, status: r.status };
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  // Idle ticker: the server can spend minutes generating firmware
  // between frames; a line every 5s of silence proves it is alive.
  // The clock resets on every frame, so a healthy stream stays clean.
  let lastFrameAt = Date.now();
  const idleT0 = lastFrameAt;
  const idleTick = setInterval(() => {
    if (Date.now() - lastFrameAt >= 5000) {
      lastFrameAt = Date.now();
      flashDialogLog("Still working on server… " +
        Math.round((Date.now() - idleT0) / 1000) + "s");
    }
  }, 1000);
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (value) buf += decoder.decode(value, { stream: true });
      const parsed = sseParse(buf);
      buf = parsed.rest;
      for (const f of parsed.frames) {
        lastFrameAt = Date.now();
        let data = null;
        try { data = JSON.parse(f.data || "null"); } catch (e) { /* ignore */ }
        if (f.name === "log" && data && data.line !== undefined) {
          flashDialogLog(data.line);
        } else if (f.name === "progress" && data) {
          flashDialogProgress(data.fraction);
        } else if (f.name === "done") {
          return { ok: true, payload: data };
        } else if (f.name === "error") {
          return { ok: false, detail: (data && data.detail) || "flash failed" };
        }
      }
      if (done) break;
    }
  } catch (e) {
    if (ctrl.signal.aborted) return { cancelled: true };
    throw e;
  } finally {
    clearInterval(idleTick);
    flashDialogAbort = null;
  }
  return { ok: false, detail: "flash stream ended unexpectedly" };
}

// Deploy flash: the finished firmware goes on once the wiring is
// done, via the server's native esptool path. Telemetry proof follows
// in the monitor; nothing here verifies per step.
// Browser flash: the finished firmware over Web Serial. Optional —
// FLASH FINAL stays the recommended path: the server's native
// esptool at 921600 baud with auto-reset, the same upload path as
// Arduino IDE. The browser path exists for machines where the
// server cannot see the USB port (remote browser, missing driver).
let webPort = null; // held SerialPort across CONNECT + FLASH taps
function webLog(statusId, line) {
  // The bar is one ellipsized line by design: it shows the latest
  // step. The dialog keeps the full terminal beside it.
  $(statusId).textContent = line;
  flashDialogLog(line);
}
function u8ToBstr(u8) {
  let s = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < u8.length; i += CHUNK) {
    s += String.fromCharCode.apply(null, u8.subarray(i, i + CHUNK));
  }
  return s;
}
// Classify a browser-flash failure so each kind gets its own recovery
// guidance: expired (server restarted, session gone) / dead (USB gone,
// re-pick) / busy (another program holds the port) / link (transfer
// broke mid-write, slower re-sync may fix it) / sync (BOOT retry on
// the same port, incl. packet corruption) / generic.
function flashFailureKind(msg) {
  const s = String(msg || "");
  if (/failed to fetch|networkerror|network error|load failed/i.test(s)) {
    return "unreachable";
  }
  if (/unknown learning session id/.test(s)) return "expired";
  if (/disconnect|detached|no such device/i.test(s)) return "dead";
  if (/failed to execute 'open'|failed to open serial port/i.test(s)) return "busy";
  if (/packet content transfer stopped/i.test(s)) return "link";
  if (/failed to connect|connect with the device|sync|timed out|timeout|no serial data received|invalid head of packet|serial noise|corruption/i.test(s)) return "sync";
  return "generic";
}
async function closeWebPortQuiet() {
  if (!webPort) return;
  if (!webPort.readable && !webPort.writable) return; // picked, never opened
  try {
    await webPort.close();
  } catch (e) { /* already closed or never opened */ }
  // Closed but KEPT: closing releases the OS port for the probe while
  // the selection survives, so the next FLASH IN BROWSER reopens it
  // with no picker round-trip. Only a dead handle (unplugged board)
  // drops the selection — see webFlash's catch.
}
async function webConnect(statusId, flashWord) {
  // CONNECT BOARD opens the browser's own port picker (Chrome/Edge;
  // localhost counts as a secure context), then FLASH IN BROWSER
  // (or DEPLOY, in cook mode) writes the finished firmware through
  // the vendored esptool-js bundle — no server port needed.
  if (!(navigator.serial && navigator.serial.requestPort)) {
    $(statusId).textContent = "Web Serial needs Chrome or Edge — " +
      "open this page there to flash the board.";
    return;
  }
  try {
    // Picker only — the port stays closed here; the flasher opens it
    // itself during main(), and opening twice would throw.
    webPort = await navigator.serial.requestPort();
    $(statusId).textContent = "Board selected. Press " +
      (flashWord || "FLASH IN BROWSER") + ". " +
      "(Pick the ESP32 entry — CP2102 / USB-to-UART — not Bluetooth.)";
  } catch (e) {
    $(statusId).textContent = (e && e.name === "NotFoundError")
      ? "Picker dismissed — press CONNECT BOARD and select the ESP32 entry."
      : "Connect failed: " + (e && e.message ? e.message : e);
  }
}
async function adoptMonitorPort() {
  // A COM name for the server-side monitor: reuse the last deploy
  // port, else ask the server to adopt the answering board. ""
  // when nothing answers (the caller guides the user).
  if (lastFlashPort) return lastFlashPort;
  try {
    const r = await fetch("/api/serial/autodetect", { method: "POST" });
    const v = await r.json();
    const port = v.found && (v.serial_live || {}).port;
    if (port) {
      lastFlashPort = port;
      return port;
    }
  } catch (e) { /* the caller guides the user */ }
  return "";
}
async function startMonitorAfterWebFlash(say) {
  // The browser owns no COM name, but the monitor needs one: adopt
  // it when possible, else open the drawer on START so one tap
  // retries the moment the board is plugged in.
  const port = await adoptMonitorPort();
  if (port) { startMonitor(port); return; }
  say("Browser flash complete. Plug the board in over USB, open the monitor, and press START to prove the build.");
  setDrawer("right", true);
}
async function webFlash(statusId, progId) {
  if (!learnSid || flashBusy) return; // not in a build, or already flashing
  if (busy) {
    // A turn must never trap the flash button: one more tap flashes
    // once it lands. Screen only: the turn's own speech owns the
    // audio right now.
    cancelWireSpeech();
    $(statusId).textContent = "Still working — press FLASH IN BROWSER " +
      "again in a moment; your board stays selected.";
    return;
  }
  if (!webPort) {
    $(statusId).textContent = "Press CONNECT BOARD first, then select the ESP32.";
    return;
  }
  $(progId).hidden = false;
  $(progId).removeAttribute("value"); // indeterminate until bytes flow
  $(statusId).textContent = "";
  $(statusId).scrollIntoView({ block: "nearest" });
  const say = cookMode ? cookSpeak : speak;
  say("Flashing the finished firmware from the browser.");
  flashDialogOpen("Flashing from the browser…");
  flashDialogProgress(null);
  let transport = null;
  let sawProgress = false;
  try {
    flashBusy = true;
    setFlashButtonsDisabled(true);
    webLog(statusId, "Loading flasher (first run fetches it once)…");
    let esp;
    try {
      esp = await import("/esptool-js/bundle.js");
    } catch (e) {
      throw new Error("flasher unavailable (offline on first run?): the " +
        "server fetches esptool-js once automatically — manual fallback: " +
        "save the ESM bundle as frontend/esptool-js/bundle.js, then reload.");
    }
    if (!esp.Transport || !esp.ESPLoader) {
      throw new Error("flasher bundle is not the esptool-js ESM build " +
        "(needs Transport + ESPLoader exports).");
    }
    webLog(statusId, "Compiling on server…");
    // The compile is one blocking fetch: a liveness ticker in the
    // dialog proves it is working instead of sitting static.
    const compileT0 = Date.now();
    const compileTick = setInterval(() => {
      flashDialogLog("Still compiling on server… " +
        Math.round((Date.now() - compileT0) / 1000) + "s");
    }, 5000);
    let r, v;
    try {
      r = await fetch("/project/" + encodeURIComponent(learnSid) +
        "/firmware");
      v = await r.json();
    } finally {
      clearInterval(compileTick);
    }
    if (!r.ok) throw new Error((v && v.detail) || "compile failed");
    const files = v.files || [];
    if (!files.length) throw new Error("server returned no binaries");
    webLog(statusId, "Build " + (v.build_hash || "?") +
      (v.cached ? " (cached)" : "") + " — downloading " +
      files.length + " file(s) before writing…");
    const fileArray = [];
    for (const f of files) {
      // Retried per file: a backend that restarts or hiccups
      // mid-download should cost a second, not the whole flash.
      let buf = null;
      for (let attempt = 0; attempt < 3 && !buf; attempt++) {
        try {
          const fr = await fetch(f.url);
          if (!fr.ok) throw new Error("download failed: " + f.url);
          buf = await fr.arrayBuffer();
        } catch (e) {
          if (attempt === 2) {
            throw new Error("download failed after retries: " + f.url +
              " (" + ((e && e.message) || e) + ")");
          }
          webLog(statusId, "Download hiccup on file " + (f.index + 1) +
            " — retrying.");
          await new Promise((res) => setTimeout(res, 1000));
        }
      }
      // esptool-js 0.5.7 wants binary STRINGS here (its write loop
      // calls .charCodeAt); a Uint8Array dies with "A.charCodeAt is
      // not a function". Chunked conversion avoids call-stack limits.
      fileArray.push({ data: u8ToBstr(new Uint8Array(buf)),
                       address: parseInt(f.offset, 16) });
    }
    // The server probe holds the port persistently once a session adopts
    // it; the browser can't open it until the server lets go (same
    // release the server-side flash does before esptool runs).
    webLog(statusId, "Releasing the server probe's port…");
    try {
      const rr = await fetch("/api/serial/release", { method: "POST" });
      if (!rr.ok) {
        webLog(statusId, "Note: the server predates port release — restart " +
          "it if the port stays busy.");
      }
    } catch (e) { /* the open attempt below reports the truth */ }
    const terminal = {
      writeLine: (s) => webLog(statusId, String(s).replace(/\r?\n$/, "")),
      write: (s) => webLog(statusId, String(s)),
      clean: () => {},
    };
    // Fixed 115200 baud: this link corrupts after the stub speed-up
    // at anything faster, and the ladder only wasted minutes proving
    // it again on every flash. One re-sync retry at the same speed,
    // since flaky links often succeed on the second connect. Only
    // sync/link failures retry — anything else throws immediately.
    const BAUDS = [115200, 115200];
    for (let bi = 0; bi < BAUDS.length; bi++) {
      const baud = BAUDS[bi];
      const last = bi === BAUDS.length - 1;
      try {
        webLog(statusId, "Entering bootloader at " + baud + " baud…");
        transport = new esp.Transport(webPort);
        const loader = new esp.ESPLoader({ transport: transport,
                                           baudrate: baud,
                                           terminal: terminal });
        await loader.main();
        await loader.writeFlash({
          fileArray: fileArray,
          flashSize: "keep",
          flashMode: "keep",
          flashFreq: "keep",
          eraseAll: false,
          compress: true,
          reportProgress: (fileIndex, written, total) => {
            const pct = total ? Math.floor((written / total) * 100) : 0;
            sawProgress = true;
            $(progId).value = pct;
            flashDialogProgress(total ? written / total : null);
            $(statusId).textContent = "Flashing file " +
              (fileIndex + 1) + " of " + fileArray.length + "… " + pct + "%";
          },
        });
        break;
      } catch (e) {
        const kind = flashFailureKind((e && e.message) || e);
        const retryable = kind === "sync" || kind === "link";
        try {
          if (transport && transport.disconnect) await transport.disconnect();
        } catch (e2) { /* port already gone */ }
        transport = null;
        if (!retryable || last) throw e;
        webLog(statusId, baud + " baud failed — trying once more at the same speed…");
        $(progId).removeAttribute("value");
      }
    }
    $(progId).value = 100;
    $(statusId).textContent = "Done — releasing the port (the board reboots " +
      "into the new firmware). The monitor proves the build.";
    flashDialogDone(true, "Browser flash complete.");
    say("Browser flash complete.");
    startMonitorAfterWebFlash(say);
  } catch (e) {
    const msg = e && e.message ? e.message : e;
    if (!sawProgress) $(progId).value = 0; // died before bytes: park it empty
    webLog(statusId, "Browser flash failed: " + msg);
    flashDialogDone(false, "Browser flash failed.");
    const kind = flashFailureKind(msg);
    if (kind === "unreachable") {
      // The server stopped answering mid-flash (window closed, process
      // crashed, or a --reload restart). Nothing on the board side can
      // fix this; a restarted server also forgets the session build.
      webLog(statusId, "The server stopped answering — check the backend " +
        "window is still running (restart it if it closed or crashed), " +
        "then tap EXIT, start the project fresh, and flash again.");
      say("The server stopped answering. Restart it, start the project " +
        "again, then flash.");
    } else if (kind === "expired") {
      // The page outlived the server: sessions live in server memory,
      // so a restart orphans this build. A fresh start is the only fix.
      webLog(statusId, "This build expired — the server restarted and " +
        "forgot it. Tap EXIT, tap your project again to start fresh, " +
        "then flash.");
      say("This build expired. Start the project again, then flash.");
    } else {
      if (kind === "dead") {
        // Stale handle (USB unplugged): the kept port would fail the same
        // way forever, so drop it and send the user back to the picker.
        webPort = null;
        webLog(statusId, "The board disconnected — replug USB, press CONNECT " +
          "BOARD, and select the ESP32 entry again.");
      } else if (kind === "busy") {
        webLog(statusId, "Something else is holding the port — close Arduino " +
          "IDE and any Serial Monitor, then press FLASH IN BROWSER again. " +
          "No reconnect needed.");
      } else if (kind === "link") {
        webLog(statusId, "The link broke mid-write: use a different USB cable " +
          "or port (avoid hubs), hold the BOOT button, and press FLASH IN " +
          "BROWSER again.");
      } else if (kind === "sync") {
        webLog(statusId, "The board stopped answering: hold its BOOT button, " +
          "press FLASH IN BROWSER again, and release BOOT once flashing " +
          "starts. Also close Arduino IDE and any Serial Monitor. If it " +
          "keeps failing, try another cable or port — or press DEPLOY " +
          "with a picked serial port (the server's native uploader).");
      }
      say("Browser flash failed. See the screen for the error.");
    }
  } finally {
    try {
      if (transport && transport.disconnect) await transport.disconnect();
    } catch (e2) { /* port already gone */ }
    // Closed but kept: the monitor probes right away, and a retry (e.g.
    // while holding BOOT) needs no fresh picker round-trip.
    try {
      if (webPort) await webPort.close();
    } catch (e3) { /* already closed */ }
    // Bar stays visible with its final state (100, last %, or still
    // spinning on an early failure) — next attempt resets it.
    flashBusy = false;
    setFlashButtonsDisabled(false);
  }
}
async function cookDeploy() {
  // One DEPLOY for both engines: a connected browser port flashes
  // from the browser, otherwise the server dialog flashes the
  // picked COM port. The bar's status line names the engine.
  if (!cookMode || !learnSid) return;
  if (webPort) {
    webFlash("cook-flash-status", "cook-webflash-progress");
    return;
  }
  openDeploy();
}
function paintDeployBar() {
  const show = cookMode && !!learnSid;
  $("deploy-bar").hidden = !show;
  document.body.classList.toggle("deploy-on", show);
  // The coach bar floats above the deploy bar: measure the real height
  // (it wraps on narrow screens) so the two never overlap.
  requestAnimationFrame(() => {
    const bar = $("deploy-bar");
    const h = (!show || !bar || !bar.offsetHeight) ? 0 : bar.offsetHeight;
    document.documentElement.style.setProperty("--deploybar-h", h + "px");
  });
}
if (typeof window !== "undefined" && window.addEventListener) {
  // Wrapping changes bar heights: re-measure so the bars never overlap.
  window.addEventListener("resize", () => { paintDeployBar(); paintCoachBar(); });
}
async function flashFinal(portId, statusId) {
  if (!learnSid || flashBusy) return;
  if (busy) {
    cancelWireSpeech();
    $(statusId).textContent = "Still working — press FLASH FINAL " +
      "again in a moment.";
    return;
  }
  const port = ($(portId) && $(portId).value) || "";
  if (!port) {
    $(statusId).textContent = "Pick the ESP32 serial port first, then press FLASH FINAL.";
    return;
  }
  const say = cookMode ? cookSpeak : speak;
  say("Flashing the finished project to the ESP32 on " + port + ".");
  flashDialogOpen("Flashing the finished project to " + port);
  flashDialogProgress(null);
  flashDialogLog("Flashing to " + port + " — preparing firmware on server…");
  try {
    flashBusy = true;
    setFlashButtonsDisabled(true);
    Orb.thinkStart(); // the orb spins while flashing
    const res = await pumpFlashStream(
      "/project/" + encodeURIComponent(learnSid) + "/flash/stream",
      { port });
    if (res.cancelled) {
      flashDialogLog("Cancelled — the board may be half-flashed; flash again to be sure.");
      flashDialogDone(false, "Cancelled.");
      $(statusId).textContent = "Flash cancelled.";
      return;
    }
    if (!res.ok) {
      const detail = res.detail || "native flash failed";
      flashDialogLog(detail);
      flashDialogDone(false, "Flash failed.");
      $(statusId).textContent = detail;
      say(detail.indexOf("busy or blocked") >= 0
        ? "Flash failed: the port is busy. Close Arduino IDE and Serial Monitor, then try again."
        : "Flash failed. See the screen for the error.");
      return;
    }
    const v = res.payload || {};
    const ping = (v.verify_ping && v.verify_ping.answered)
      ? "Board answered PING."
      : "Board is silent after the flash — your finished project is running.";
    const summary = "Flashed the finished project to " + v.port +
      " at 921600 baud (" + (v.cached ? "cached build " : "build ") +
      (v.build_hash || "") + "). " + ping;
    flashDialogLog(summary);
    flashDialogDone(true, summary);
    $(statusId).textContent = summary;
    lastFlashPort = port;
    say("Final flash complete. Your project is running.");
    onFinalFlashSuccess(port);
  } catch (e) {
    const msg = "Flash failed: " + e.message +
      " Check that the backend window is still running, then try again.";
    flashDialogLog(msg);
    flashDialogDone(false, "Flash failed.");
    $(statusId).textContent = msg;
    say("Flash failed. Check that the server is still running.");
  } finally {
    flashBusy = false;
    setFlashButtonsDisabled(false);
    Orb.thinkEnd();
  }
}

$("flash-final").onclick = async () => { await refreshFlashPorts(); flashFinal("flash-port", "flash-status"); };
$("web-connect").onclick = () => webConnect("webflash-status");
$("web-flash").onclick = () => webFlash("webflash-status", "webflash-progress");
$("cook-web-connect").onclick = () => webConnect("cook-flash-status", "DEPLOY");
$("cook-deploy").onclick = () => cookDeploy();

// Deploy uses the server's native esptool path only (flashFinal):
// browser-side step flashing went away with per-step checks.

// Hands-free voice loop: tap PUSH TO TALK to record (MediaRecorder), tap
// again to stop, upload to /api/voice/transcribe, show the transcript, and
// advance a step when it contains done / next / move on.
const TRIGGER_WORDS = ["done", "next", "move on"];
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
  stopWake(); // one recognizer at a time; the wake watchdog resumes later
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
  // Interim results on: the learner sees their words land live instead
  // of speaking into a void, and we keep every alternative so a
  // low-confidence best guess can hand off to server transcription
  // instead of acting on a mis-hear.
  rec.interimResults = true;
  rec.maxAlternatives = 3;
  let gotFinal = false;
  rec.onresult = (e) => {
    const results = e.results || [];
    let interim = "";
    for (let i = e.resultIndex || 0; i < results.length; i++) {
      const res = results[i];
      if (!res.isFinal) { interim += (res[0] || {}).transcript || ""; continue; }
      gotFinal = true;
      const alts = [];
      for (let j = 0; j < res.length; j++) {
        alts.push({ text: String((res[j] || {}).transcript || "").trim(),
                    confidence: (res[j] || {}).confidence || 0 });
      }
      const best = alts.slice().sort((a, b) => b.confidence - a.confidence)[0] ||
        { text: "", confidence: 0 };
      window.__lastRecognition = { text: best.text,
        confidence: best.confidence, alternatives: alts, at: Date.now() };
      console.log("recognition", window.__lastRecognition);
      if (!best.text) { finish(opts.onEmpty || opts.onFallback, false); return; }
      if (best.confidence && best.confidence < 0.5) {
        // Garbled: do not act on it. The recorder path transcribes on
        // the server; tell the learner why they are asked to repeat.
        opts.hintEl.textContent = "Not clear - heard \"" + best.text +
          "\" (" + Math.round(best.confidence * 100) +
          "% sure). Recording for a cleaner transcription: say it again.";
        finish(opts.onFallback, true);
        return;
      }
      finish(() => opts.onText(best.text), false);
      return;
    }
    if (interim) {
      opts.hintEl.textContent = "… " + interim.trim();
    }
  };
  rec.onerror = (e) => {
    if ((e.error || "") === "no-speech" && opts.onEmpty) {
      finish(opts.onEmpty, false);
    } else {
      finish(opts.onFallback, true);
    }
  };
  rec.onend = () => {
    // Silence with no final result is "heard nothing", not a reason
    // to make the learner repeat themselves into the recorder.
    if (!gotFinal && opts.onEmpty) finish(opts.onEmpty, false);
    else finish(opts.onFallback, true);
  };
  try {
    rec.start();
  } catch (e) {
    finish(opts.onFallback, true);
  }
  return true;
}

// Wake word (opt-in, off by default): continuous recognition listens
// for "hey Fundi", then opens an ordinary talk turn. The watchdog
// restarts listening after each command turn and stays quiet while
// the tutor is speaking so it never transcribes Fundi's own voice.
let wakeOn = false, wakeRec = null, wakeWatch = null;
try { wakeOn = localStorage.getItem("fundiloop-wake") === "1"; } catch (e) { /* private mode */ }

function paintWake() {
  const b = $("wake-toggle");
  if (!b) return;
  b.textContent = wakeOn ? "WAKE ON" : "WAKE OFF";
  b.setAttribute("aria-pressed", wakeOn ? "true" : "false");
}

function wakeSpeaking() {
  try {
    return !!(speechAudible || serverPlaying ||
      (("speechSynthesis" in window) && speechSynthesis.speaking));
  } catch (e) { return false; }
}

function stopWake() {
  if (!wakeRec) return;
  const r = wakeRec;
  wakeRec = null;
  try { r.abort(); } catch (e) { /* already ended */ }
}

function onWakeWord() {
  stopWake();
  speak("Yes?", () => {
    const b = cookMode ? $("cook-talk") : $("talk");
    if (b) b.click();
  });
}

function startWake() {
  if (!wakeOn || wakeRec || voiceListening || document.hidden) return;
  if (wakeSpeaking()) return;
  const Ctor = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Ctor) return;
  let rec;
  try { rec = new Ctor(); } catch (e) { return; }
  wakeRec = rec;
  rec.lang = "en-US";
  rec.continuous = true;
  rec.interimResults = true;
  rec.maxAlternatives = 1;
  rec.onresult = (e) => {
    const results = e.results || [];
    for (let i = e.resultIndex || 0; i < results.length; i++) {
      const t = (((results[i] || [])[0] || {}).transcript || "").toLowerCase();
      if (/\b(hey|ok|okay)?\s*(fundi|fundy|funday)\b/.test(t)) {
        onWakeWord();
        return;
      }
    }
  };
  rec.onerror = (e) => {
    if (e.error === "not-allowed" || e.error === "service-not-allowed") {
      wakeOn = false;
      paintWake();
      stopWakeWatch();
      $("talk-hint").textContent = "Wake word needs microphone permission - use the mic button instead.";
    }
  };
  rec.onend = () => { if (wakeRec === rec) wakeRec = null; };
  try { rec.start(); } catch (e) { wakeRec = null; }
}

function startWakeWatch() {
  if (wakeWatch) return;
  wakeWatch = setInterval(() => { if (wakeOn && !wakeRec) startWake(); }, 1500);
  startWake();
}

function stopWakeWatch() {
  if (wakeWatch) { clearInterval(wakeWatch); wakeWatch = null; }
  stopWake();
}

$("wake-toggle").onclick = () => {
  wakeOn = !wakeOn;
  try { localStorage.setItem("fundiloop-wake", wakeOn ? "1" : "0"); } catch (e) { /* private mode */ }
  paintWake();
  if (wakeOn) {
    startWakeWatch();
    $("talk-hint").textContent = "Wake word on - say \"hey Fundi\" to talk.";
  } else {
    stopWakeWatch();
    $("talk-hint").textContent = "Wake word off.";
  }
};
paintWake();
if (wakeOn) startWakeWatch();

function handlePttTranscript(text, tag, error) {
  if (text) lastTranscript = text;
  if (!text) {
    $("talk-hint").textContent = "Transcription empty" + (error ? ": " + error : "") + " — tap instead.";
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
    pendingVoiceStep = text;
    $("talk-hint").textContent = 'heard: "' + text + '"' + tag + " → queued, will advance when ready.";
  } else if (hit && learnSid) {
    $("talk-hint").textContent = 'heard: "' + text + '"' + tag + " → next step…";
    sendNextStep(text);
  } else {
    $("talk-hint").textContent = 'heard: "' + text + '"' + tag + " — say done or next, or tap a project card.";
  }
}

function startPttRecorder(btn) {
  if (!("MediaRecorder" in window) || !(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
    $("talk-hint").textContent = "Audio capture unavailable — tap the buttons instead.";
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
      OrbWave.releaseMic();
      $("talk-hint").textContent = "Transcribing…";
      try {
        const t = await transcribeUpload(new Blob(pttChunks, { type: "audio/webm" }));
        const text = (t.text || "").trim();
        const tag = t.note ? " (" + t.note + ")" : "";
        handlePttTranscript(text, tag, t.error || "");
      } catch (e) { $("talk-hint").textContent = "Transcription failed — tap instead."; }
    };
    pttRec.start();
    OrbWave.tapStream(stream); // orb ripples with the mic
    Orb.listenStart();
    pttRecording = true;
    btn.classList.add("rec");
    btn.textContent = "LISTENING — TAP TO STOP";
  }).catch(() => { $("talk-hint").textContent = "Microphone blocked — tap the buttons instead."; });
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

// Live Guide UI removed with the camera surface; the live session
// implementation (startLive/stopLive + /api/live/session) stays in
// the codebase, unreachable from this UI.

// Cook Mode: NYT-style guided step runner for learning projects.
// One step at a time on a full-screen card (step X of Y, progress bar,
// parts chips, live camera with SAM boxes), driven by voice: TALK to ask
// or command (next / repeat / look / exit), everything spoken back.
// Steps advance on taps; the monitor's telemetry alone proves the build.
let cookMode = false, cookView = null;
let cookRec = null, cookChunks = [], cookRecording = false;
let cookPendingVoice = ""; // cook feedback heard mid-turn, answered when free
let cookListenAuto = false; // this listen was hands-free, not a TALK tap
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

// No auto-check loop: steps advance on taps (sendNextStep) and the
// build proves itself once, in the monitor.

function pumpHandsfree(tries) {
  // Hands-free continuation with retries: a busy moment or a dropped
  // speech-end event must not kill "keep listening" silently.
  if (!cookMode || muted || !$("cook-handsfree").checked) return;
  if (busy || cookRecording || voiceListening) {
    if ((tries || 0) > 0) setTimeout(() => pumpHandsfree(tries - 1), 1500);
    return;
  }
  cookListen(true);
}

function cookSpeak(text, onend) {
  speak(text, () => {
    if (onend) { try { onend(); } catch (e) { console.log("speak onend failed", e); } }
    if (!wireSpeech && cookMode && !muted && $("cook-handsfree").checked) pumpHandsfree(3);
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
  paintRollcall(v);
  paintFundi(v.fundi);
  cookView = v;
  maybeOpenDrawer(v);
  if (v.id) learnSid = v.id;
  clampLearnStep(v);
  if (v.steps_total) learnStepTotal = v.steps_total;
  // Beats build after the step number settles (painting the coach
  // bar with the active beat, or the step instruction when beatless);
  // the explicit repaint keeps the primary action fresh on turns that
  // keep beat progress (failed checks, confirm asks).
  buildWireBeats();
  paintCoachBar();
  lastInstruction = v.instruction || "";
  if (v.speak) lastSpeak = v.speak;
  $("cook-project").textContent = (v.project_title || "Project") + (v.board ? " (" + v.board + ")" : "");
  const idx = v.step_index || 0, total = v.steps_total || 1;
  const done = v.state === "DONE";
  if (done && wireBeats.length) {
    wireBeats = []; wireBeatIdx = 0; wireBeatAcked = false;
    cancelBeatSpeech();
    paintWireBeat(); // restore the full diagram
    paintCoachBar(); // refresh the primary action (deploy)
  }
  $("cook-step").textContent = done
    ? "DONE — all " + total + " steps verified"
    : "STEP " + Math.min(idx + 1, total) + " OF " + total;
  $("cook-bar").style.width = Math.round(100 * (done ? total : idx) / total) + "%";
  const box = $("cook-instruction");
  // Beats own the bubble while wiring: only a beatless (or finished)
  // step shows the raw server instruction.
  if (done || !wireBeats.length) box.textContent = v.instruction || "(no instruction)";
  box.classList.toggle("fault", v.state === "VERIFYING");
  box.classList.toggle("done", done);
  const deps = (v.step && v.step.depends_on_parts) || [];
  $("cook-parts").innerHTML = deps.map((d) => "<li>" + esc(d) + "</li>").join("");
  const ver = v.verification || {};
  let html = ver.test
    ? "test: " + esc(ver.test) + " → " + (ver.passed ? "PASS" : "FAIL") +
      "\nmeasured: " + esc(ver.measured || "")
    : "no evidence yet — the monitor proves the finished build";
  const p = v.probe || {};
  if (p.i2c_devices) html += "\nI2C: [" + esc((p.i2c_devices || []).join(", ") || "none") + "]";
  if (v.timings) html += "\n" + esc(fmtTimings(v.timings));
  $("cook-status").innerHTML = html;
  $("cook-done").hidden = !learnSid; // drafts available anytime, final at DONE
  if (!done) {
    // A fetched draft survives re-renders; only a new session clears it.
    if (v.id && codeFetchedSid !== v.id) $("cook-code-out").textContent = "";
  } else if (v.id && codeFetchedSid !== v.id) {
    // DONE: the project code arrives on its own; SEND CODE stays as retry.
    setTimeout(() => {
      if (cookMode && cookView && cookView.state === "DONE" && codeFetchedSid !== cookView.id) cookSendCode(true);
    }, 150);
  }
  paintTroubleshoot("cook-troubleshoot", (v.fundi || {}).troubleshoot);
  paintStretch("cook-stretch", (v.fundi || {}).stretch);
  refreshSchematics();
  paintDeployBar();
  fetchGuide();
}

function drawCookBoxes(detections) {
  // Cook camera overlay removed with the camera UI: no-op.
  return;
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
    // Hands-free silence stays off-screen (it retries quietly); only a
    // manual TALK tap reports that it heard nothing.
    if (!cookListenAuto) {
      $("cook-heard").textContent = "Heard nothing" + (error ? ": " + error : "") + ".";
    }
    cookSilentStreak += 1;
    if (cookSilentStreak < 3) pumpHandsfree(1); // brief silence keeps listening
    return;
  }
  cookSilentStreak = 0;
  lastTranscript = text;
  markCookActive();
  if (busy) {
    cookPendingVoice = text; // a turn owns the floor; answered right after
    $("cook-heard").textContent = 'heard: "' + text + '" → queued, will answer when ready.';
    return;
  }
  $("cook-heard").textContent = 'heard: "' + text + '"';
  await cookCommand(text);
}

// A turn just closed with cook feedback waiting: answer it now.
function drainCookVoice() {
  if (!cookMode || !cookPendingVoice) return;
  const v = cookPendingVoice;
  cookPendingVoice = "";
  cookCommand(v);
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
      OrbWave.releaseMic();
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
    OrbWave.tapStream(stream); // orb ripples with the mic
    Orb.listenStart();
    cookRecording = true;
    btn.classList.add("rec");
    btn.textContent = "LISTENING…";
    $("cook-heard").textContent = "Listening…";
    setTimeout(() => { if (cookRecording && cookRec) { try { cookRec.stop(); } catch (e) {} } }, COOK_LISTEN_MS);
  }).catch(() => { $("cook-heard").textContent = "Microphone blocked."; });
}

function cookListen(auto) {
  const btn = $("cook-talk");
  if (activeRec) {
    voiceCancelFallback = true;
    try { activeRec.stop(); } catch (e) { /* already stopped */ }
    return;
  }
  if (cookRecording) { try { cookRec.stop(); } catch (e) { /* already stopped */ } return; }
  if (!cookMode) return;
  cookListenAuto = !!auto;
  // Recording stays available mid-check like the home mic; the transcript
  // queues and the coach answers it when the turn closes.
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
  logCoach("You", text);
  // The parts roll call owns every transcript until READY: nothing
  // else may swallow the answer.
  if (cookView && cookView.state === "ROLLCALL") {
    sendReady(text);
    return;
  }
  // Steps advance on taps or words: move on, done, next — all mean
  // the wiring is finished and the build moves forward. (The final
  // step holds server-side: only Deploy finishes the build.)
  if (/(move on|skip( this| the)? step|just continue|done|finish|complete|ready|next( step| phase)?)/.test(t)) {
    sendNextStep();
    return;
  }
  if (/(repeat|say again|again)/.test(t)) {
    cookSpeak(lastSpeak || (cookView && cookView.instruction) || "");
    return;
  }
  if (/(send code|get code|show code|read code)/.test(t)) { cookSendCode(); return; }
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

// Steps advance on taps (the coach bar's Next Step), never checks.

async function cookLook() {
  if (!cookMode) return;
  if (busy) {
    $("cook-heard").textContent = "Still working — try again in a moment.";
    return;
  }
  markCookActive();
  // No camera on this surface anymore: LOOK means look at the wiring
  // diagram - hear this step's wires as each one lights up.
  cancelWireSpeech();
  const st = cookSchematicState;
  if (st.wires && st.wires.length) {
    $("cook-heard").textContent = "Looking at the diagram…";
    speakWireList(wireSpeechItems(st.wires, st.n || learnStepN),
      cookSpeak, "cook-schematic-svg", "cook-schematic-sel");
  } else {
    cookSpeak((cookView && cookView.instruction) || "");
  }
}

async function cookSendCode(auto) {
  if (!cookMode || !learnSid) return;
  // A manual tap mid-turn must say something (screen only: the live
  // turn owns the audio). Auto fetches stay silent on overlap.
  if (busy) {
    if (!auto) $("cook-heard").textContent = "Still working — press SEND CODE again in a moment.";
    return;
  }
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
      $("cook-heard").textContent = msg + " — SEND CODE retries.";
      paintStageCodeError(msg + " — Send to Board retries.");
      cookSpeak("Code is not ready yet. Press Send Code to retry.");
      return;
    }
    $("cook-code-out").textContent = v.code || "";
    codeFetchedSid = learnSid;
    const progress = (v.steps_passed || 0) + " of " + (v.steps_total || 0) + " steps verified";
    $("cook-heard").textContent = v.verified ? "Final code below." : "Draft code below (" + progress + ").";
    paintStageCode(v.code || "", v.verified ? "verified final" : "draft (" + progress + ")");
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

function enterCookMode(v, opts) {
  if (liveOn) stopLive("Live guide paused — cook mode running.");
  cookMode = true;
  document.body.classList.add("cooking", "coach-active"); // hide home chrome + mic
  cookView = v;
  if ($("cook")) $("cook").hidden = false; // legacy shim
  $("learn-sec").hidden = true;
  // Show the learning leaf in the left panel
  if ($("cook-leaf")) $("cook-leaf").hidden = false;
  if ($("guide-hero")) $("guide-hero").hidden = true;
  paintResumeBar(); // hidden while cooking
  $("cook-heard").textContent = "";
  $("cook-nudge").hidden = true;
  $("cook-coach-confirm").hidden = true;
  coachPending = null;
  markCookActive();
  renderCook(v);
  paintDeployBar();
    syncKillState();
  window.scrollTo(0, 0);
  // Reveal callers speak their own summary.
  if (opts && opts.quiet) return;
  cookSpeak(v.speak || v.instruction || "");
}

function exitCookMode() {
  cookMode = false;
  document.body.classList.remove("cooking", "coach-active"); // restore home chrome
  document.body.classList.remove("hide-narration");
  paintDeployBar();
  cancelBeatSpeech(); // a pending summary must not sound after exit
  stopMonitor("Stopped.");
  exitRescue(false);
  cancelWireSpeech();
  cookPendingVoice = "";
  $("cook-nudge").hidden = true;
  $("cook-coach-confirm").hidden = true;
  $("cook-killed").hidden = true;
  coachPending = null;
  if (cookRecording && cookRec) { try { cookRec.stop(); } catch (e) { /* already stopped */ } }
  cookRecording = false;
  VoiceEngine.stop();
  $("cook-talk").classList.remove("rec");
  $("cook-talk").textContent = "TALK";
  if ($("cook")) $("cook").hidden = true; // legacy shim
  $("learn-sec").hidden = false;
  if ($("guide-hero")) $("guide-hero").hidden = false;
  // Hide learning leaf, restore home
  if ($("cook-leaf")) $("cook-leaf").hidden = true;
  $("cook-done").hidden = true; // learn mode shows code-sec instead
  switchStageTab("diagram");
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

$("cook-talk").onclick = () => { markCookActive(); cookListen(false); };
$("cook-coach-yes").onclick = () => sendCoachConfirm(true);
$("cook-coach-no").onclick = () => sendCoachConfirm(false);
function hushCookSpeech() {
  // STOP hushes output now: server audio, browser speech, and any
  // wire/beat narration queued behind it. It never trips the safety
  // rail — ARM stays for rails tripped outside this button.
  cancelBeatSpeech();
  cancelWireSpeech();
  VoiceEngine.stop();
  if (cookMode) $("cook-heard").textContent = "Stopped.";
}
if ($("cook-stop")) $("cook-stop").onclick = () => hushCookSpeech();
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
$("cook-repeat").onclick = () => { markCookActive(); cookSpeak(lastSpeak || (cookView && cookView.instruction) || ""); };
$("cook-look").onclick = () => cookLook();
if ($("cook-exit")) $("cook-exit").onclick = () => exitCookMode();
$("cook-code").onclick = () => cookSendCode();
// Sticky resume bar: a live session is one tap away without scrolling
// back through the setup panel. Hidden in cook mode and before any
// session starts.
function paintResumeBar() {
  const bar = $("resume-bar");
  if (!learnSid || !learnTitle || cookMode) { bar.hidden = true; return; }
  $("resume-text").textContent = learnDone
    ? "Finished: " + learnTitle + " — review or flash."
    : "Continue: " + learnTitle + " — step " +
      Math.min(learnStepN, learnStepTotal || learnStepN) +
      " of " + (learnStepTotal || "?") + ".";
  bar.hidden = false;
}

async function resumeCook() {
  if (busy) return;
  busy = true;
  try {
    const v = await (await fetch("/api/status")).json();
    if (v.learning) { appRevealed = true; enterCookMode(v); }
    else $("talk-hint").textContent = "No learning session to resume.";
  } catch (e) { showError(e.message); } finally { busy = false; }
}
$("cook-resume").onclick = () => resumeCook();
$("resume-go").onclick = () => resumeCook();

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
      " but no parts yet — upload a parts photo, then SUGGEST PROJECTS.";
  }
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
    drawEvidence(d.frame_file, (d.sam && d.sam.detections) || [], d.sam && d.sam.mock);
    speak(d.description);
  } catch (e) { showError(e.message); }
  finally { busy = false; }
};

function updateMuteBtn() {
  $("mute").textContent = muted ? "UNMUTE" : "MUTE";
  $("mute").classList.toggle("muted", muted);
  const cv = $("coach-voice");
  if (cv) cv.textContent = muted ? "Unmute" : "Mute";
}
$("settings-btn").onclick = () => { $("settings-modal").hidden = false; };
$("settings-close").onclick = () => { $("settings-modal").hidden = true; };
$("settings-modal").addEventListener("click", (e) => {
  if (e.target === $("settings-modal")) $("settings-modal").hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("settings-modal").hidden = true;
  if (e.key === "Escape" && !$("flash-dialog").hidden) flashDialogClose();
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

$("mute").onclick = () => toggleMute();
updateMuteBtn();
if ($("preview")) $("preview").onerror = () => {
  const ov = $("cam-overlay");
  if (ov && ov.hidden) {
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

// Boot: Learning is the landing surface. An existing learning
// session resumes; guided/diagnostic sessions are never started here.
fetch("/api/status").then((r) => r.json()).then((v) => {
  if (v.config) paintBadge(v.config);
  if (v.learning) { enterLearning(v); return; }
  mode = "LEARNING";
  $("learn-sec").hidden = false;
  $("instruction").textContent =
    "Hi! Upload a photo of your parts, name your project, then tap SUGGEST — or tap the mic and tell me what you want to build.";
}).catch((e) => { showError("backend unreachable: " + e.message); });
refreshSerialPorts();
setInterval(refreshBadge, 5000);
setInterval(heartbeatNudge, HEARTBEAT_MS);
