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
- Never overwrite, upload, or flash firmware.
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
- Run: `backend\.venv\Scripts\python.exe scripts\learning_chat.py` (MOCK_META=true rehearses offline).
- Next: Tier 3 (ears/mouth) when the user says go. No Tier 3+ code written yet.
