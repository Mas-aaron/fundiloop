"""Frontend regression checks for the reported issues.

No JS test runner ships with this repo, so these read frontend/app.js
and frontend/index.html directly: the dynamic project prompt is
executed under node when available, the rest assert the wiring that
keeps flashing, speech, and the schematic draw working. Both flash
paths keep UI buttons: native server flash (recommended, same engine
as Arduino IDE) next to browser flash.

Run from the repo root:
  backend\\.venv\\Scripts\\python.exe -m unittest backend.tests.test_frontend_regressions -v
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")


def _block(start_marker, end_marker):
    start = APP_JS.index(start_marker)
    end = APP_JS.index(end_marker, start)
    return APP_JS[start:end]


class SingleSpeechAtProjectStartTest(unittest.TestCase):
    """A new speak() must silence browser speech queued by the last one.

    Server audio already stops via stopServerAudio; without cancelling
    speechSynthesis, a browser-fallback utterance keeps talking under
    the next server-audio utterance (double talking at project start).
    """

    def test_speak_cancels_queued_browser_speech(self):
        body = _block("  speak(text, onend) {", "  streamStart() {")
        self.assertIn("speechSynthesis.cancel()", body)


class SpeechBridgeTest(unittest.TestCase):
    """The opening narration starts in the server voice with no quiet hole.

    Cold Kokoro costs ~10s (model load + first inference), which used to
    race the chunk timeout and drop the opening to browser speech after
    seconds of quiet. Two fixes: the app primes the server voice at
    boot and first intent (suggest, card tap) while the user reads,
    and playUrl cuts stale browser speech when server audio actually
    starts — never at speak time (quiet hole) and never late
    (double talk). There are no bridging acks; taps acknowledge
    visually.
    """

    def test_server_voice_primed_at_first_intent(self):
        self.assertIn("function warmServerVoice()", APP_JS)
        boot = _block("function warmServerVoice() {",
                      "function speakBrowserNow")
        self.assertIn("\nwarmServerVoice();", boot)
        suggest = _block("async function suggestProjects() {",
                         "async function startLearning(")
        self.assertIn("warmServerVoice()", suggest)
        start = _block("async function startLearning(",
                       "// choice:prompt:start")
        self.assertIn("warmServerVoice()", start)

    def test_speak_cuts_stale_browser_on_first_audio(self):
        speak_body = _block("  speak(text, onend) {", "  streamStart() {")
        self.assertGreater(
            speak_body.index("speechSynthesis.cancel()"),
            speak_body.index("if (!text || muted)"))
        chunks = _block("function playServerChunks(chunks, mySpeech, done) {",
                        "const VoiceEngine = {")
        self.assertIn("speechSynthesis.cancel()", chunks)


class DynamicProjectPromptTest(unittest.TestCase):
    """The pick-a-project prompt must match how many projects exist."""

    def test_prompt_helper_is_used_and_branches_on_count(self):
        body = _block("// choice:prompt:start", "// choice:prompt:end")
        self.assertIn("list.length === 1", body)
        self.assertIn("projectChoiceSpeech(learnProjects)", APP_JS)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_single_project_prompt_never_mentions_number(self):
        body = _block("// choice:prompt:start", "// choice:prompt:end")
        script = body + """
const one = projectChoiceSpeech([{title: "Blink an LED"}]);
const many = projectChoiceSpeech([{title: "A"}, {title: "B"}]);
console.log(one);
console.log(many);
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        single, multi = out.stdout.strip().splitlines()
        self.assertIn("one project", single)
        self.assertNotIn("number", single)
        self.assertIn("name or number", multi)


class SchematicPacingTest(unittest.TestCase):
    """Wires start when the speech reaches the wiring, not at second 0.

    The step utterance opens with preamble (title, parts list) before
    any wire is named, so the draw must offset by that lead; and a
    finished utterance must not leave speechAudible stale-true for the
    next step's draw.
    """

    def test_draw_offsets_by_the_wire_mention_lead(self):
        self.assertIn("speechLeadFrac", APP_JS)
        body = _block("function runParkedSchematicDraw",
                      "function paintSchematic")
        self.assertIn("speechLeadFrac", body)

    def test_speech_end_clears_the_audible_flag(self):
        body = _block("  speak(text, onend) {", "  streamStart() {")
        self.assertIn("markSpeechEnded", body)
        self.assertIn("function markSpeechEnded", APP_JS)


class NativeServerFlashTest(unittest.TestCase):
    """The Arduino-style native upload is the recommended flash path.

    Both panels expose the server endpoint with an explicit port
    choice (the backend refuses to guess). Deploy flashes the
    finished firmware once the wiring is done; proof follows in
    the monitor. Browser Web Serial is the fallback, not the
    default.
    """

    def test_native_controls_exist_in_both_panels(self):
        for element_id in ("flash-port", "flash-final", "flash-status",
                           "cook-flash-port", "cook-deploy",
                           "cook-flash-status"):
            self.assertIn('id="%s"' % element_id, INDEX_HTML)

    def test_deploy_posts_explicit_port_to_native_endpoint(self):
        body = _block("async function flashFinal(",
                      '$("flash-final").onclick')
        self.assertIn('"/flash/stream"', body)
        self.assertIn("pumpFlashStream(", body)
        self.assertIn("{ port }", body)
        pump = _block("async function pumpFlashStream(",
                      "async function flashFinal(")
        self.assertIn("JSON.stringify(body)", pump)

    def test_native_controls_share_busy_guard(self):
        guard = _block("function setFlashButtonsDisabled",
                       "async function refreshFlashPorts")
        self.assertIn("flash-final", guard)
        self.assertIn("cook-deploy", guard)
        self.assertIn("flash-port", guard)
        self.assertIn("cook-flash-port", guard)
        self.assertIn("web-connect", guard)
        self.assertIn("web-flash", guard)
        self.assertIn("cook-web-connect", guard)


class CookButtonAuditTest(unittest.TestCase):
    """Every cook button must visibly do something.

    CONFIRM used to return silently whenever a check (often the AUTO
    loop) was mid-flight, and its errors only reached a banner hidden
    behind the cook overlay. LOOK fetched the removed camera describe
    endpoint and could only fail.
    """

    def test_look_uses_the_diagram_not_the_camera(self):
        body = _block("async function cookLook", "async function cookSendCode")
        self.assertNotIn("/api/vision/describe", body)
        self.assertIn("speakWireList", body)

    def test_send_code_nags_when_busy(self):
        fetch = _block("async function fetchCode(auto) {",
                       '$("send-code").onclick')
        self.assertIn("Still working", fetch)
        cook = _block("async function cookSendCode(auto) {",
                      "function enterCookMode(v, opts) {")
        self.assertIn("Still working", cook)


class LearningFirstUiTest(unittest.TestCase):
    """Learning is the whole surface; camera UI is gone."""

    def test_no_mode_tabs_and_no_camera_elements(self):
        for gone in ("mode-guided", "mode-diag", "mode-learn",
                     "preview", "cook-preview", "cam-stream",
                     "cam-capture", "save-cam", "learn-equip",
                     "snap", "live"):
            self.assertNotIn('id="%s"' % gone, INDEX_HTML)

    def test_learning_lands_visible_and_wake_toggle_exists(self):
        self.assertIn('<section id="learn-sec" class="panel">', INDEX_HTML)
        self.assertIn('id="wake-toggle"', INDEX_HTML)
        self.assertIn("function startWake", APP_JS)
        self.assertNotIn("/api/camera/latest", APP_JS)
        self.assertNotIn("/discover/capture", APP_JS)

    def test_recognition_shows_interim_and_keeps_alternatives(self):
        body = _block("function startBrowserRecognition",
                      "function handlePttTranscript")
        self.assertIn("interimResults = true", body)
        self.assertIn("maxAlternatives = 3", body)
        self.assertIn("__lastRecognition", body)


class NarrationStyleTest(unittest.TestCase):
    """Narration must not sound like a script being read twice.

    Project start: one opening line, no spoken parts inventory, no
    repeated title. Guide tap: the step is announced once, even when
    the chapter title already is "Step 2".
    """

    def test_project_start_speaks_one_opening_line(self):
        body = _block("async function startLearning",
                      "// choice:prompt:start")
        self.assertIn("Let's build ", body)
        self.assertNotIn("You need:", body)

    def test_guide_intro_never_duplicates_the_step(self):
        body = _block("function stepIntroPhrase", "function speakGuideStep")
        self.assertIn("/^step\\s+\\d+/i", body)
        guide = _block("function speakGuideStep", "function markGuideStep")
        self.assertIn("stepIntroPhrase(s)", guide)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_step_intro_phrase_outputs(self):
        body = _block("function stepIntroPhrase", "function speakGuideStep")
        script = body + """
console.log(stepIntroPhrase({n: 2, title: "Step 2"}));
console.log(stepIntroPhrase({n: 2, title: "Wire the sensor"}));
console.log(stepIntroPhrase({n: 3, title: ""}));
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip().splitlines(),
                         ["Step 2", "Step 2: Wire the sensor", "Step 3"])


class SchematicFollowsChunksTest(unittest.TestCase):
    """Wires animate when their spoken chunk actually sounds.

    Timed spreads guess speech duration and drift. The draw must be
    driven by chunk-start events from both speech paths (server audio
    and browser fallback), and leftovers must flush when speech ends.
    """

    def test_chunk_hook_fires_from_both_speech_paths(self):
        server = _block("function playServerChunks", "const VoiceEngine")
        self.assertIn("speechChunkHook", server)
        browser = _block("function speakBrowserNow",
                         "function speakBrowserClause")
        self.assertIn("speechChunkHook", browser)

    def test_follow_draws_mentioned_wires_and_flushes_rest(self):
        self.assertIn("function followSpokenChunk", APP_JS)
        self.assertIn("function flushFollowDraw", APP_JS)
        body = _block("function markSpeechEnded", "function revokeAudioUrl")
        self.assertIn("flushFollowDraw", body)


class SchematicOrderTest(unittest.TestCase):
    """Animated wires follow the spoken order, not the drawing order.

    The visual schematic stacks parts top-to-bottom; the tutor names
    wires in instruction order. A wire named last must animate last,
    wherever it sits on screen.
    """

    def test_draw_order_follows_spoken_mentions(self):
        body = _block("function playSchematicDraw",
                      "function runParkedSchematicDraw")
        self.assertIn("spokenWireOrder", body)

    def test_guide_narration_speaks_teach_before_wires(self):
        body = _block("function speakGuideStep", "function markGuideStep")
        self.assertIn("s.teach", body)


class SchematicWaitsForSpeechTest(unittest.TestCase):
    """First-phase wires must not race ahead of a tutor still warming up.

    The parked draw may only run at default speed once no speech for
    this step is pending; markSpeechAudible paces it to the voice.
    """

    def test_parked_draw_waits_for_pending_speech(self):
        self.assertIn("scheduleParkedDrawFallback", APP_JS)
        body = _block("function scheduleParkedDrawFallback",
                      "function runParkedSchematicDraw")
        self.assertIn("speechAudible", body)
        self.assertIn("parkedAt", body)

    def test_no_short_unconditional_fallback_remains(self):
        self.assertNotIn("}, 8000);", APP_JS)


class BrowserFlashTest(unittest.TestCase):
    """Browser Web Serial is the fallback flash path: CONNECT BOARD
    picks the port in-page, then the finished firmware downloads
    from the final manifest and writes through esptool-js."""

    def test_browser_controls_exist_in_both_panels(self):
        for element_id in ("web-connect", "web-flash",
                           "webflash-progress", "webflash-status",
                           "cook-web-connect", "cook-webflash-progress"):
            self.assertIn('id="%s"' % element_id, INDEX_HTML)

    def test_web_flash_downloads_final_manifest(self):
        body = _block("async function webFlash(",
                      "async function cookDeploy()")
        self.assertIn('"/firmware"', body)
        self.assertNotIn('"/step/"', body)
        self.assertIn("ESPLoader", body)
        self.assertIn("/esptool-js/bundle.js", body)

    def test_web_flash_asks_for_connect_first(self):
        body = _block("async function webFlash(",
                      "async function cookDeploy()")
        self.assertIn("Press CONNECT BOARD first", body)

    def test_deploy_prefers_browser_then_server(self):
        body = _block("async function cookDeploy() {",
                      "function paintDeployBar()")
        self.assertIn("webPort", body)
        self.assertIn("webFlash(", body)
        self.assertIn("openDeploy()", body)
        self.assertLess(body.index("webFlash("), body.index("openDeploy()"))


class DeployBarTest(unittest.TestCase):
    """One deploy home: a bottom bar with the port pick, CONNECT,
    and DEPLOY — always visible while cooking, never buried."""

    def test_deploy_bar_markup_exists(self):
        for ident in ("deploy-bar", "deploy-label", "cook-deploy"):
            self.assertIn('id="%s"' % ident, INDEX_HTML)
        bar = INDEX_HTML[INDEX_HTML.index('id="deploy-bar"'):
                         INDEX_HTML.index("<!-- /deploy-bar -->")]
        for ident in ("cook-flash-status", "cook-flash-port",
                      "cook-web-connect", "cook-deploy"):
            self.assertIn('id="%s"' % ident, bar)

    def test_deploy_bar_shows_while_cooking(self):
        body = _block("function paintDeployBar() {",
                      "async function flashFinal(")
        self.assertIn("cookMode", body)
        self.assertIn("learnSid", body)
        self.assertIn("deploy-on", body)
        self.assertIn("deploy-bar", STYLE_CSS)
        self.assertIn("deploy-on", STYLE_CSS)

    def test_port_pick_is_explicit_no_phantom_default(self):
        body = _block("async function refreshFlashPorts() {",
                      "// flash:sse:start")
        self.assertIn("Select port", body)
        self.assertNotIn('"COM3"', body)
        self.assertIn("usbishPort(currentEntry)", body)
        self.assertNotIn("'COM3'", body)


class FlashDialogTest(unittest.TestCase):
    """Deploy opens a terminal dialog: live lines stream into one
    console for the native server flash."""

    def test_dialog_markup_exists(self):
        for ident in ("flash-dialog", "flash-title", "flash-console",
                      "flash-progress", "flash-close"):
            self.assertIn('id="%s"' % ident, INDEX_HTML)

    def test_deploy_streams_into_dialog(self):
        body = _block("async function flashFinal(",
                      '$("flash-final").onclick')
        self.assertIn("flashDialogOpen(", body)
        self.assertIn("/flash/stream", body)

    def test_deploy_logs_instantly_and_ticks_while_idle(self):
        final = _block("async function flashFinal(",
                       '$("flash-final").onclick')
        self.assertIn("preparing firmware on server", final)
        pump = _block("async function pumpFlashStream(url, body) {",
                      "// Deploy flash:")
        self.assertIn("Still working on server", pump)
        self.assertIn("lastFrameAt", pump)
        self.assertIn("clearInterval(idleTick)", pump)


class RollcallFlowTest(unittest.TestCase):
    """Chapter 1 waits for READY: the roll call renders with its own
    button, and transcripts route to /api/session/ready."""

    def test_cook_routes_rollcall_transcripts_to_ready(self):
        body = _block("async function cookCommand(text) {",
                      "function showCoachConfirm(pending) {")
        self.assertIn("ROLLCALL", body)
        self.assertIn("sendReady", body)

    def test_ready_posts_to_ready_endpoint(self):
        body = _block("async function sendReady(",
                      "function drainVoiceStep()")
        self.assertIn("/api/session/ready", body)
        self.assertIn("renderCook", body)
        self.assertIn("renderLearning", body)

    def test_ready_buttons_exist_and_wire(self):
        self.assertIn('id="cook-ready"', INDEX_HTML)
        self.assertIn('id="rollcall-ready"', INDEX_HTML)
        self.assertIn('$("cook-ready").onclick', APP_JS)
        self.assertIn('$("rollcall-ready").onclick', APP_JS)

    def test_rollcall_banner_paints(self):
        body = _block("function paintFundi(fundi) {",
                      "function renderLearning(v) {")
        self.assertIn("ROLLCALL", body)


class TroubleshootStretchRenderTest(unittest.TestCase):
    """Escalated failures show their fix chain and DONE shows stretch
    cards, in both cook and learn views."""

    def test_hint_chain_renders_in_both_modes(self):
        self.assertIn('id="cook-troubleshoot"', INDEX_HTML)
        self.assertIn('id="learn-troubleshoot"', INDEX_HTML)
        learn = _block("function renderLearning(v) {",
                       "function enterLearning(v) {")
        cook = _block("function renderCook(v) {",
                      "function drawCookBoxes(")
        self.assertIn("troubleshoot", learn)
        self.assertIn("troubleshoot", cook)

    def test_stretch_cards_render_in_both_modes(self):
        self.assertIn('id="cook-stretch"', INDEX_HTML)
        self.assertIn('id="learn-stretch"', INDEX_HTML)
        learn = _block("function renderLearning(v) {",
                       "function enterLearning(v) {")
        cook = _block("function renderCook(v) {",
                      "function drawCookBoxes(")
        self.assertIn("stretch", learn)
        self.assertIn("stretch", cook)


class DeployStreamParserTest(unittest.TestCase):
    """The deploy terminal parses SSE frames split across chunks."""

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_sse_parser_splits_frames_across_chunks(self):
        body = _block("// flash:sse:start", "// flash:sse:end")
        script = body + """
let acc = "";
let frames = [];
function feed(chunk) {
  const r = sseParse(acc + chunk);
  acc = r.rest;
  frames = frames.concat(r.frames);
}
feed('event: log\\ndata: {"line":"a"}\\n\\nevent: pro');
feed('gress\\ndata: {"fraction":0.5}\\n\\n');
console.log(frames.length);
console.log(frames[0].name + ":" + JSON.parse(frames[0].data).line);
console.log(frames[1].name + ":" + JSON.parse(frames[1].data).fraction);
console.log(JSON.stringify(acc));
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip().splitlines(),
                         ["2", "log:a", "progress:0.5", '""'])


class LearnFlowLayoutTest(unittest.TestCase):
    """Planning results land in view: a results header, tappable
    cards, a sticky resume bar, and a session progress bar."""

    def test_results_header_markup_and_scroll(self):
        self.assertIn('id="learn-results-head"', INDEX_HTML)
        body = _block("const renderPlans = (projects, hint) => {",
                      "async function startLearning(project) {")
        self.assertIn("learn-results-head", body)
        self.assertIn("scrollIntoView", body)
        self.assertIn("prefers-reduced-motion", body)

    def test_cards_have_start_affordance(self):
        body = _block("function renderLearnCards(projects) {",
                      "function paintFundi(fundi) {")
        self.assertIn("start-pill", body)
        self.assertIn("card-top", body)
        self.assertIn('data-p="', body)

    def test_resume_bar_wired(self):
        for ident in ("resume-bar", "resume-text", "resume-go"):
            self.assertIn('id="%s"' % ident, INDEX_HTML)
        body = _block("function paintResumeBar() {",
                      "async function resumeCook()")
        self.assertIn("resume-bar", body)
        self.assertIn("learnTitle", body)
        self.assertIn('"resume-go").onclick', APP_JS)

    def test_learn_progress_painted(self):
        self.assertIn('id="learn-progress"', INDEX_HTML)
        body = _block("function renderLearning(v) {",
                      "function enterLearning(v) {")
        self.assertIn("learn-progress", body)
        self.assertIn("steps_passed", body)
        self.assertIn("paintResumeBar()", body)


class SpeechPileupTest(unittest.TestCase):
    """Speech never talks over itself: the auto loop waits while the
    tutor sounds, cook mode starts its countdown after the opening,
    and a missed flash cue repeats on manual checks."""

    def test_speech_chunks_drop_silent_pieces(self):
        body = _block("function splitSpeakChunks(say) {",
                      "function playServerChunks(chunks, mySpeech, done) {")
        self.assertIn("[A-Za-z0-9]", body)

class SpeechWatchdogTest(unittest.TestCase):
    """A hung TTS fetch or stuck audio element must fall back to browser
    speech instead of losing the utterance silently (text shown, quiet)."""

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_fetch_timeout_aborts_hung_tts(self):
        body = _block("// speech:fetch:start", "// speech:fetch:end")
        script = body + """
global.fetch = (url, opts) => new Promise((_, reject) => {
  const sig = opts && opts.signal;
  if (sig) sig.addEventListener("abort", () => reject(new Error("aborted")));
});
(async () => {
  try { await fetchWithTimeout("http://x", {}, 50); console.log("resolved"); }
  catch (e) { console.log("rejected"); }
  global.fetch = async () => "ok";
  console.log(await fetchWithTimeout("http://x", {}, 500));
})();
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip().splitlines(),
                         ["rejected", "ok"])

    def test_voice_fetch_uses_timeout(self):
        body = _block("async function fetchServerSpeech(text) {",
                      "function speakBrowserNow(say, done) {")
        self.assertIn("fetchWithTimeout", body)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_playback_watchdog_fires_without_progress(self):
        body = _block("// speech:watchdog:start", "// speech:watchdog:end")
        script = body + """
const fired = [];
const settle = armPlaybackWatchdog(30, () => fired.push("stalled"));
armPlaybackWatchdog(30, () => fired.push("other"));
settle();
setTimeout(() => console.log(fired.join(",") || "none"), 80);
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip().splitlines(), ["other"])

    def test_chunk_player_arms_watchdog(self):
        body = _block("function playServerChunks", "const VoiceEngine")
        self.assertIn("armPlaybackWatchdog", body)

    def test_browser_fallback_avoids_cancel_speak_race(self):
        body = _block("function speakBrowserNow",
                      "function speakBrowserClause")
        # Cancel only when something is actually queued: a bare
        # cancel() in the same tick as speak() wedges Chrome's queue
        # and the utterance never sounds.
        self.assertIn("speechSynthesis.speaking", body)
        self.assertIn("speechSynthesis.pending", body)


class NoCheckinQuestionTest(unittest.TestCase):
    """No question after the steps: turns end when the speech ends."""

    def test_followup_machinery_is_gone(self):
        for name in ("tutorFollowup", "tutorFollowupPlan",
                     "matchTutorAnswer", "tutorAtoms",
                     "TUTOR_NUM_WORDS", "tutorListening"):
            self.assertNotIn(name, APP_JS)

    def test_beat_driver_speaks_turn_only(self):
        body = _block("function tutorBeat(",
                      "// phase:machine:start")
        self.assertIn("say(text)", body)
        self.assertNotIn("tutorFollowup", body)


class OneVoiceTest(unittest.TestCase):
    """No bridging acks: taps acknowledge visually (orb + tickers)
    and speech stays one voice — server first, browser fallback only
    when the server path fails."""

    def test_no_bridging_ack_calls(self):
        for ack in ("speakBrowserNow(\"Let's go.\")",
                    "speakBrowserNow(\"Moving on.\")",
                    "speakBrowserNow(\"Let's wire it up.\")",
                    "speakBrowserNow(\"Planning your projects.\")",
                    "speakBrowserNow(\"Starting \"",
                    "speakBrowserNow(\"Let's build \""):
            self.assertNotIn(ack, APP_JS)

    def test_fallback_path_survives(self):
        # The failure fallback is not an ack: server-audio failures
        # still speak the utterance in the browser voice.
        chunks = _block("function playServerChunks(chunks, mySpeech, done) {",
                        "const VoiceEngine = {")
        self.assertIn("speakBrowserNow(chunks.slice(i).join(\" \")", chunks)

class PaintGuardsTest(unittest.TestCase):
    """Rollcall, troubleshoot, and stretch paints never throw on a stale
    page: a render exception must not kill the speech that follows it."""

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_paints_survive_missing_elements(self):
        rollcall = _block("function paintRollcall(v) {",
                          "function paintTroubleshoot(")
        trouble = _block("function paintTroubleshoot(",
                         "function paintStretch(")
        stretch = _block("function paintStretch(",
                         "// READY: answer the parts roll call.")
        script = rollcall + trouble + stretch + """
global.$ = () => null;
global.esc = (s) => s;
paintRollcall({state: "ROLLCALL",
  fundi: {rollcall: {question: "Ready?"}}});
paintRollcall({state: "INSTRUCTING"});
paintTroubleshoot("learn-troubleshoot", ["fix it"]);
paintStretch("learn-stretch", [{title: "More", summary: "do more"}]);
const els = {};
global.$ = (id) => els[id] ||
  (els[id] = {hidden: true, textContent: "", innerHTML: ""});
paintRollcall({state: "ROLLCALL",
  fundi: {rollcall: {question: "Ready?"}}});
console.log(els["rollcall-bar"].hidden, els["rollcall-q"].textContent);
paintRollcall({state: "INSTRUCTING"});
console.log(els["rollcall-bar"].hidden);
paintTroubleshoot("learn-troubleshoot", ["fix it"]);
console.log(els["learn-troubleshoot"].hidden,
  /fix it/.test(els["learn-troubleshoot"].innerHTML));
paintTroubleshoot("learn-troubleshoot", []);
console.log(els["learn-troubleshoot"].hidden);
paintStretch("learn-stretch", [{title: "More", summary: "do more"}]);
console.log(els["learn-stretch"].hidden,
  /More/.test(els["learn-stretch"].innerHTML));
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip().splitlines(),
                         ["false Ready?", "true", "false true", "true",
                          "false true"])


class AppPhaseTest(unittest.TestCase):
    """The 5-phase flow resolves from session + UI state: ideation,
    reveal, building, deploying, verifying, rescue."""

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_phase_matrix(self):
        body = _block("// phase:machine:start", "// phase:machine:end")
        script = body + """
const cases = [
  [{}, "IDEATION"],
  [{hasSession: true}, "REVEAL"],
  [{hasSession: true, revealed: true}, "BUILDING"],
  [{hasSession: true, revealed: true, flashOpen: true}, "DEPLOYING"],
  [{hasSession: true, revealed: true, monitorOn: true}, "VERIFYING"],
  [{hasSession: true, revealed: true, monitorOn: true, fault: true},
   "RESCUE"],
  [{hasSession: true, revealed: true, fault: true}, "RESCUE"],
];
for (const [ctx, want] of cases) {
  console.log(resolveAppPhase(ctx) === want ? "ok" : "FAIL");
}
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip().splitlines(), ["ok"] * 7)


class CoachActionTest(unittest.TestCase):
    """The coach bar offers exactly one primary action per state."""

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_action_matrix(self):
        body = _block("// coach:action:start", "// coach:action:end")
        script = body + """
const cases = [
  [{phase: "REVEAL"}, "start-building"],
  [{phase: "BUILDING", serverState: "ROLLCALL"}, "ready"],
  [{phase: "BUILDING", wiresTotal: 4, wireIdx: 4, wireAcked: false,
    stepN: 2, stepsTotal: 4}, "next-step"],
  [{phase: "BUILDING", wiresTotal: 4, wireIdx: 4, wireAcked: false,
    stepN: 4, stepsTotal: 4}, "deploy"],
  [{phase: "BUILDING", wiresTotal: 0, stepN: 2, stepsTotal: 4},
   "next-step"],
  [{phase: "BUILDING", wiresTotal: 0, stepN: 4, stepsTotal: 4},
   "deploy"],
  [{phase: "BUILDING", serverState: "DONE"}, "deploy"],
  [{phase: "DEPLOYING"}, "none"],
  [{phase: "VERIFYING"}, "none"],
  [{phase: "RESCUE"}, "none"],
  [{phase: "IDEATION"}, "none"],
];
for (const [ctx, want] of cases) {
  const got = coachPrimaryAction(ctx);
  console.log(((got && got.id) || "none") === want ? "ok" : "FAIL");
}
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip().splitlines(), ["ok"] * 11)

    def test_coach_bar_paint_dispatches_primary(self):
        paint = _block("function paintCoachBar(", "function coachTapped(")
        self.assertIn("coachPrimaryAction", paint)
        self.assertIn("coach-primary", paint)
        self.assertIn("stepN", paint)
        self.assertIn("stepsTotal", paint)
        tap = _block("function coachTapped(", "function toggleCoachMore(")
        for action in ("start-building", "ready", "next-step", "deploy"):
            self.assertIn(action, tap)
        self.assertNotIn("wire-connected", tap)
        self.assertNotIn("wire-next", tap)
        self.assertIn("sendNextStep()", tap)


class RevealSpeechTest(unittest.TestCase):
    """The ideation reveal names the circuit and its parts aloud."""

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_reveal_names_parts(self):
        body = _block("// reveal:speech:start", "// reveal:speech:end")
        script = body + """
console.log(revealSpeech("Parking Sensor",
  ["ESP32 DevKit", "HC-SR04P Ultrasonic", "Buzzer"]));
console.log(revealSpeech("Blink", ["ESP32 DevKit", "LED"]));
console.log(revealSpeech("Mystery", []));
console.log(JSON.stringify(revealPartTitles(
  [{part_title: "HC-SR04P Ultrasonic"}, {part_title: "Buzzer"},
   {part_title: "Buzzer"}, {}], "esp32_devkit")));
console.log(JSON.stringify(revealPartTitles([], "arduino_uno")));
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        full, small, empty, titles, uno = out.stdout.strip().splitlines()
        self.assertIn("Here is the circuit", full)
        self.assertIn("an ESP32 DevKit", full)
        self.assertIn("an HC-SR04P Ultrasonic", full)
        self.assertIn("a Buzzer", full)
        self.assertIn("an LED", small)
        self.assertIn("Here is the circuit", empty)
        self.assertEqual(json.loads(titles),
                         ["ESP32 DevKit", "HC-SR04P Ultrasonic", "Buzzer"])
        self.assertEqual(json.loads(uno), ["Arduino Uno"])

    def test_reveal_flow_exists(self):
        self.assertIn("function enterReveal(", APP_JS)
        body = _block("function enterReveal(", "function revealToBuilding(")
        self.assertIn("Start Building", body)
        self.assertIn("revealSpeech", body)


class CoachLayoutTest(unittest.TestCase):
    """Three-zone layout: center stage, coach bar, collapsible drawers.
    Code, fix-it lists, and stretch cards live in the left drawer;
    deploy owns the bottom bar; secondary cook actions live in the
    coach overflow."""

    def test_coach_bar_exists_with_primary_voice_overflow(self):
        for ident in ("coach-bar", "coach-text", "coach-primary",
                      "coach-voice", "coach-more", "coach-overflow"):
            self.assertIn('id="%s"' % ident, INDEX_HTML)
        # Push-to-talk keeps its id and moves into the coach main row,
        # ahead of the overflow with the secondary actions.
        bar = INDEX_HTML.index('id="coach-bar"')
        talk = INDEX_HTML.index('id="cook-talk"')
        overflow = INDEX_HTML.index('id="coach-overflow"')
        self.assertTrue(bar < talk < overflow)

    def test_drawers_exist_with_monitor_and_rescue(self):
        for ident in ("left-drawer", "left-drawer-tab", "right-drawer",
                      "right-drawer-tab", "monitor-out", "monitor-stop",
                      "rescue-card", "coach-log"):
            self.assertIn('id="%s"' % ident, INDEX_HTML)

    def test_drawers_slide_in_when_opened(self):
        self.assertIn(".drawer.left:not([hidden])", STYLE_CSS)
        self.assertIn(".drawer.right:not([hidden])", STYLE_CSS)
        self.assertNotIn(".drawer-tab { display: none; }", STYLE_CSS)
        self.assertIn("top: var(--topbar-h)", STYLE_CSS)

    def test_code_and_fixit_live_in_left_drawer(self):
        start = INDEX_HTML.index('id="left-drawer"')
        end = INDEX_HTML.index("<!-- /left-drawer -->")
        for ident in ("code-sec", "learn-troubleshoot", "learn-stretch"):
            at = INDEX_HTML.index('id="%s"' % ident)
            self.assertTrue(start < at < end, ident)

    def test_cook_fixit_lives_in_cook_leaf(self):
        start = INDEX_HTML.index('id="cook-leaf"')
        end = INDEX_HTML.index("<!-- /cook-leaf -->")
        for ident in ("cook-troubleshoot", "cook-stretch", "rescue-card",
                      "rescue-msg", "rescue-suspect", "rescue-complete"):
            at = INDEX_HTML.index('id="%s"' % ident)
            self.assertTrue(start < at < end, ident)

    def test_fixit_ids_are_unique(self):
        for ident in ("cook-troubleshoot", "cook-stretch", "rescue-card",
                      "rescue-msg", "rescue-suspect", "rescue-complete",
                      "learn-troubleshoot", "learn-stretch"):
            self.assertEqual(INDEX_HTML.count('id="%s"' % ident), 1, ident)

    def test_cook_flash_moved_to_deploy_bar(self):
        start = INDEX_HTML.index('id="deploy-bar"')
        end = INDEX_HTML.index("<!-- /deploy-bar -->")
        for ident in ("cook-flash-port", "cook-deploy",
                      "cook-flash-status", "cook-web-connect"):
            at = INDEX_HTML.index('id="%s"' % ident)
            self.assertTrue(start < at < end, ident)

    def test_learn_flash_stays_in_drawer(self):
        start = INDEX_HTML.index('id="left-drawer"')
        end = INDEX_HTML.index("<!-- /left-drawer -->")
        for ident in ("flash-port", "flash-final", "flash-status",
                      "web-connect", "web-flash", "webflash-status"):
            at = INDEX_HTML.index('id="%s"' % ident)
            self.assertTrue(start < at < end, ident)

    def test_cook_code_panel_available_before_done(self):
        body = _block("function renderCook(v) {",
                      "function exitCookMode()")
        self.assertIn('$("cook-done").hidden = !learnSid', body)
        panel = INDEX_HTML[INDEX_HTML.index('id="cook-done"'):
                           INDEX_HTML.index('id="cook-code-out"')]
        self.assertIn("Project firmware", panel)

    def test_secondary_actions_moved_to_overflow(self):
        start = INDEX_HTML.index('id="coach-overflow"')
        end = INDEX_HTML.index("<!-- /coach-overflow -->")
        overflow = INDEX_HTML[start:end]
        for ident in ("cook-repeat", "cook-look",
                      "cook-schematic-download-overflow",
                      "cook-schematic-speak-overflow"):
            self.assertIn('id="%s"' % ident, overflow)
        for ident in ("cook-check", "cook-phase-done", "cook-auto",
                      "cook-confirm", "cook-move-on"):
            self.assertNotIn('id="%s"' % ident, INDEX_HTML)

    def test_stage_and_drawer_styles_exist(self):
        self.assertIn(".drawer", STYLE_CSS)
        self.assertIn("#coach-bar", STYLE_CSS)
        self.assertIn("#coach-primary", STYLE_CSS)
        self.assertIn("wire-active", STYLE_CSS)
        self.assertIn("wire-fault", STYLE_CSS)


class WireBeatsTest(unittest.TestCase):
    """Each step's new wires narrate as one flowing summary: pin
    endpoints and wire color with transitions — from netlist data,
    never invented."""

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_beats_narrate_each_wire(self):
        reveal = _block("// reveal:speech:start", "// reveal:speech:end")
        body = reveal + _block("// wire:beats:start", "// wire:beats:end")
        script = body + """
const wires = [
  {part: "hc_sr04", part_pin: "VCC", board_pin: "5V", signal: "power",
   color_name: "red", part_title: "HC-SR04P Ultrasonic", step_added: 1},
  {part: "hc_sr04", part_pin: "GND", board_pin: "GND", signal: "ground",
   color_name: "gray", part_title: "HC-SR04P Ultrasonic", step_added: 1},
  {part: "hc_sr04", part_pin: "TRIG", board_pin: "GPIO25",
   signal: "TRIG", color_name: "blue",
   part_title: "HC-SR04P Ultrasonic", step_added: 1},
  {part: "hc_sr04", part_pin: "ECHO", board_pin: "GPIO27",
   signal: "ECHO", color_name: "green",
   part_title: "HC-SR04P Ultrasonic", step_added: 1},
  {part: "buzzer", part_pin: "+", board_pin: "GPIO26", signal: "+",
   color_name: "amber", part_title: "Buzzer", step_added: 2},
];
const beats = wireBeatsForStep(wires, 1, "ESP32 DevKit");
console.log(beats.length);
console.log(beats[0].say);
console.log(beats[2].say);
console.log(beats[0].key);
console.log(wireBeatsForStep(wires, 9, "ESP32 DevKit").length);
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        count, first, trig, key, none = out.stdout.strip().splitlines()
        self.assertEqual(count, "4")
        self.assertIn("Wire 1 of 4", first)
        self.assertIn("5V", first)
        self.assertIn("VCC", first)
        self.assertIn("red wire", first)
        self.assertIn("HC-SR04P Ultrasonic", first)
        self.assertIn("Wire 3 of 4", trig)
        self.assertIn("TRIG", trig)
        self.assertIn("GPIO25", trig)
        self.assertIn("blue wire", trig)
        self.assertEqual(key, "hc_sr04:VCC")
        self.assertEqual(none, "0")

    def test_summary_wired(self):
        for name in ("buildWireBeats", "speakStepSummary",
                     "stepSummaryItems", "stepWirePhrase",
                     "paintWireBeat", "cancelBeatSpeech",
                     "coachBeatText", "schematicPartId"):
            self.assertIn("function %s(" % name, APP_JS)
        for gone in ("ackWireBeat", "nextWireBeat", "speakBeat"):
            self.assertNotIn("function %s(" % gone, APP_JS)
        fetch = _block("async function fetchSchematic(",
                       "function refreshSchematics()")
        self.assertIn("buildWireBeats", fetch)
        paint = _block("function paintWireBeat() {",
                       "// Check turns are gone")
        self.assertIn("schematicWireId(", paint)
        self.assertIn("schematicPartId(", paint)
        self.assertIn("animateWireNow(", paint)
        self.assertIn("wire-active", paint)
        self.assertIn("wire-dim", paint)
        self.assertIn("style.opacity", paint)

    def test_beat_styles_exist(self):
        for cls in ("part-active", "wire-active", "wire-dim"):
            self.assertIn(cls, STYLE_CSS)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_wire_ids_match_server_svg_id(self):
        from backend.app.schematic import svg_id
        script = _block("function schematicWireId(w) {",
                        "// A wire-speech sequence owns") + """
console.log(schematicWireId({part: "hc_sr04", part_pin: "VCC"}));
console.log(schematicWireId({part: "my sensor!", part_pin: "TRIG/ECHO"}));
console.log(schematicPartId("my sensor!"));
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        wire, tricky, part = out.stdout.strip().splitlines()
        self.assertEqual(wire, svg_id("wire-hc_sr04-VCC"))
        self.assertEqual(tricky, svg_id("wire-my sensor!-TRIG/ECHO"))
        self.assertEqual(part, svg_id("part-my sensor!"))


class OrbPhaseTest(unittest.TestCase):
    """The orb shows phase states: idle, thinking, listening, success,
    fault. Fault latches until cleared; success flashes briefly."""

    def test_palette_has_ok_and_fault(self):
        body = _block("const C = {", "};")
        self.assertIn("ok:", body)
        self.assertIn("fault:", body)

    def test_orb_api_has_ok_fault_monitor(self):
        body = _block("const Orb = (() => {", "})();")
        self.assertIn("ok(", body)
        self.assertIn("fault(", body)
        self.assertIn("monitor(", body)


class MonitorRescueTest(unittest.TestCase):
    """Live verification after the final flash: telemetry triage is
    deterministic and per device, stalls and persistent device errors
    latch rescue naming the device, the tutor speaks the reconnect,
    and the failed device's own healthy lines bring us back online."""

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_telemetry_triage(self):
        body = _block("// monitor:pure:start", "// monitor:pure:end")
        script = body + """
const cases = [
  "Distance: 45cm",
  "Distance: -1",
  "Error: Timeout",
  "rst:0x1 (POWERON_RESET),boot:0x13",
  "23.5 mm",
  "ok",
  "nan",
  "2 m",
  "-5 cm",
  "ECHO timeout on GPIO27",
  "Distance: 20cm",
  "STATUS ok",
  "System alive",
  "okay",
  "hello world",
  "boot:0x13",
];
for (const line of cases) {
  const c = classifyTelemetryLine(line);
  console.log(JSON.stringify({line: line, value: c.value, unit: c.unit,
    fault: c.fault, say: telemetryReadout(c),
    proof: telemetryProvesLife(line)}));
}
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = [json.loads(row) for row in out.stdout.strip().splitlines()]
        by_line = {g["line"]: g for g in got}
        good = by_line["Distance: 45cm"]
        self.assertEqual((good["value"], good["unit"], good["fault"]),
                         (45, "cm", False))
        self.assertIn("45 centimeters", good["say"])
        self.assertTrue(by_line["Distance: -1"]["fault"])
        self.assertIsNone(by_line["Distance: -1"]["value"])
        self.assertTrue(by_line["Error: Timeout"]["fault"])
        self.assertTrue(by_line["nan"]["fault"])
        self.assertTrue(by_line["ECHO timeout on GPIO27"]["fault"])
        self.assertTrue(by_line["-5 cm"]["fault"])
        boot = by_line["rst:0x1 (POWERON_RESET),boot:0x13"]
        self.assertEqual((boot["value"], boot["fault"]), (None, False))
        self.assertEqual(boot["say"], "")
        self.assertEqual(by_line["ok"]["say"], "")
        self.assertFalse(by_line["ok"]["fault"])
        mm = by_line["23.5 mm"]
        self.assertEqual((mm["value"], mm["unit"]), (23.5, "mm"))
        self.assertIn("millimeters", mm["say"])
        self.assertEqual(by_line["2 m"]["unit"], "m")
        self.assertIn("20 centimeters", by_line["Distance: 20cm"]["say"])
        self.assertTrue(by_line["Distance: 45cm"]["proof"])
        self.assertTrue(by_line["23.5 mm"]["proof"])
        self.assertTrue(by_line["STATUS ok"]["proof"])
        self.assertTrue(by_line["System alive"]["proof"])
        self.assertTrue(by_line["ok"]["proof"])
        self.assertFalse(by_line["okay"]["proof"])
        self.assertFalse(by_line["hello world"]["proof"])
        self.assertFalse(by_line["boot:0x13"]["proof"])
        self.assertFalse(by_line["Error: Timeout"]["proof"])
        self.assertFalse(by_line["Distance: -1"]["proof"])

    def test_monitor_wired(self):
        for name in ("startMonitor", "stopMonitor", "onMonitorLine",
                     "monitorWatchdog", "enterRescue", "exitRescue",
                     "requestRescueDiagnosis", "onFinalFlashSuccess"):
            self.assertIn("function %s(" % name, APP_JS)
        start = _block("function startMonitor(port) {",
                       "function stopMonitor(note) {")
        self.assertIn("telemetry/stream", start)
        self.assertIn("EventSource", start)
        self.assertIn("monitorWatchdog", start)
        self.assertIn("onMonitorLine", start)
        self.assertIn("Listening to your board", start)
        diag = _block("async function requestRescueDiagnosis() {",
                      "function enterRescue(reason, device) {")
        self.assertIn("telemetry/diagnose", diag)
        self.assertIn("wire-fault", diag)
        self.assertIn("schematicWireId(", diag)
        self.assertIn("rescue-msg", diag)
        enter = _block("function enterRescue(reason, device) {",
                       "function exitRescue(backOnline) {")
        self.assertIn("Orb.fault(true)", enter)
        self.assertIn("rescue-card", enter)
        self.assertIn("requestRescueDiagnosis()", enter)
        self.assertIn("I lost the signal", enter)
        self.assertIn("Rescue card", enter)
        exit = _block("function exitRescue(backOnline) {",
                      "function onFinalFlashSuccess(port) {")
        self.assertIn("wire-fault", exit)
        self.assertIn("back online", exit)
        self.assertIn("Back online. Your build works.", exit)
        final = _block("function onFinalFlashSuccess(port) {",
                       "// monitor:end")
        self.assertIn("startMonitor(", final)
        self.assertIn('$("monitor-stop").onclick', APP_JS)
        self.assertIn('$("monitor-start").onclick', APP_JS)
        reveal = _block("async function enterReveal(view, project) {",
                        "function revealToBuilding() {")
        self.assertIn("stopMonitor(", reveal)
        self.assertIn("exitRescue(", reveal)
        gone = _block("function exitCookMode() {",
                      "function setKilled(")
        self.assertIn("stopMonitor(", gone)
        self.assertIn("exitRescue(", gone)

    def test_monitor_markup_and_styles(self):
        for ident in ("monitor-start", "monitor-stop", "monitor-out",
                      "monitor-meta", "rescue-card", "rescue-msg",
                      "rescue-suspect", "rescue-complete"):
            self.assertIn(ident, INDEX_HTML)
        self.assertIn("wire-fault", STYLE_CSS)

    def test_proof_streak_posts_telemetry_verified(self):
        self.assertIn("const MONITOR_PROOF_LINES = 5", APP_JS)
        line = _block("function onMonitorLine(text) {",
                      "async function requestTelemetryProof() {")
        self.assertIn("monitorStreak", line)
        self.assertIn("MONITOR_PROOF_LINES", line)
        self.assertIn("requestTelemetryProof()", line)
        proof = _block("async function requestTelemetryProof() {",
                       "function monitorWatchdog() {")
        self.assertIn("telemetry-verified", proof)
        self.assertIn("renderCook(", proof)
        self.assertIn("monitorVerifyCooldownUntil", proof)
        self.assertIn("monitorVerified", proof)

    def test_rescue_escape_marks_complete(self):
        self.assertIn('$("rescue-complete").onclick', APP_JS)
        esc = _block('$("rescue-complete").onclick',
                     "// monitor:end")
        self.assertIn("mark complete", esc)

    def test_monitor_ids_are_unique(self):
        for ident in ("monitor-meta", "monitor-out", "monitor-start",
                      "monitor-stop"):
            self.assertEqual(INDEX_HTML.count('id="%s"' % ident), 1, ident)

    def test_monitor_start_adopts_port(self):
        self.assertIn("async function adoptMonitorPort() {", APP_JS)
        body = _block("async function adoptMonitorPort() {",
                      "async function startMonitorAfterWebFlash(say) {")
        self.assertIn("/api/serial/autodetect", body)
        self.assertIn("lastFlashPort", body)
        self.assertIn("|| await adoptMonitorPort()", APP_JS)

    def test_monitor_picker_lists_ports(self):
        self.assertIn('id="monitor-port"', INDEX_HTML)
        body = _block("async function refreshMonitorPorts() {",
                      "// flash:sse:start")
        self.assertIn("/api/serial/ports", body)
        self.assertIn(
            "pickFlashPort(ports, sel.value || lastFlashPort)", body)
        self.assertIn("await refreshMonitorPorts()", APP_JS)

    def test_monitor_guidance_names_the_picker(self):
        # Learning firmware never answers PING, so adoption alone
        # cannot find it: the picker is how a flashed board gets
        # heard, and the guidance must say so.
        self.assertIn("pick the board port", APP_JS)

    def test_transient_faults_do_not_trip_rescue(self):
        # One timeout line is a passing dip (hand out of range), not
        # a lost board: rescue trips only on consecutive faults, so
        # the tutor stops flapping lost-signal/back-online chatter.
        self.assertIn("const MONITOR_FAULT_LINES = 3", APP_JS)
        line = _block("function onMonitorLine(text) {",
                      "async function requestTelemetryProof() {")
        self.assertIn("monitorFaults", line)
        self.assertIn("MONITOR_FAULT_LINES", line)
        self.assertIn("enterRescue(", line)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_reconnect_speech_names_the_wire(self):
        # The suspect wire is spoken, not just painted: the tutor
        # tells the user exactly which wire to reseat and where it
        # runs, or stays silent when no wire was named.
        body = _block("// monitor:pure:start", "// monitor:pure:end")
        script = body + """
const cases = [
  [{part: "hc_sr04", part_pin: "TRIG", board_pin: "GPIO4"}, "wire"],
  [{part: "servo", part_pin: "SIG"}, "pin"],
  [null, "none"],
  [{part: "hc_sr04"}, "none"],
  [{part_pin: "TRIG"}, "none"],
];
for (const [suspect, kind] of cases) {
  console.log(JSON.stringify({kind: kind,
    say: suspectReconnectSpeech(suspect)}));
}
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = [json.loads(row) for row in out.stdout.strip().splitlines()]
        by_kind = {}
        for g in got:
            by_kind.setdefault(g["kind"], []).append(g["say"])
        wire = by_kind["wire"][0]
        self.assertIn("TRIG", wire)
        self.assertIn("GPIO4", wire)
        self.assertTrue(wire.strip().endswith("."))
        self.assertIn("SIG", by_kind["pin"][0])
        for say in by_kind["none"]:
            self.assertEqual(say, "")

    def test_diagnosis_speaks_the_reconnect(self):
        # The spoken rescue guidance carries the reconnect sentence:
        # the wire to reseat is heard, not only read on the card.
        diag = _block("async function requestRescueDiagnosis() {",
                      "function enterRescue(reason, device) {")
        self.assertIn("suspectReconnectSpeech(suspect)", diag)
        self.assertIn("cookSpeak(say)", diag)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_tagged_telemetry_attributes_devices(self):
        # Tagged lines attribute to their device; untagged lines keep
        # the legacy global shape (device "") and sentence.
        body = _block("// monitor:pure:start", "// monitor:pure:end")
        script = body + """
const cases = [
  "HC-SR04 | DIST | 45.0 | cm",
  "BME280 | TEMP | 24.5 | C",
  "HC-SR04 | DIST | TIMEOUT | cm",
  "SYS | HEARTBEAT | OK | -",
  "LED | STATE | HIGH | -",
  "Distance: 45cm",
];
for (const line of cases) {
  const c = classifyTelemetryLine(line);
  console.log(JSON.stringify({line: line, device: c.device,
    value: c.value, unit: c.unit, fault: c.fault,
    say: telemetryReadout(c), proof: telemetryProvesLife(line)}));
}
"""
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = [json.loads(row) for row in out.stdout.strip().splitlines()]
        by_line = {g["line"]: g for g in got}
        dist = by_line["HC-SR04 | DIST | 45.0 | cm"]
        self.assertEqual(dist["device"], "HC-SR04")
        self.assertEqual((dist["value"], dist["unit"], dist["fault"]),
                         (45, "cm", False))
        self.assertIn("45 centimeters", dist["say"])
        self.assertIn("HC", dist["say"])
        self.assertTrue(dist["proof"])
        temp = by_line["BME280 | TEMP | 24.5 | C"]
        self.assertEqual(temp["device"], "BME280")
        self.assertEqual(temp["value"], 24.5)
        self.assertFalse(temp["fault"])
        self.assertIn("24.5", temp["say"])
        self.assertIn("degrees", temp["say"])
        self.assertTrue(temp["proof"])
        bad = by_line["HC-SR04 | DIST | TIMEOUT | cm"]
        self.assertEqual(bad["device"], "HC-SR04")
        self.assertTrue(bad["fault"])
        self.assertIsNone(bad["value"])
        self.assertFalse(bad["proof"])
        beat = by_line["SYS | HEARTBEAT | OK | -"]
        self.assertEqual(beat["device"], "SYS")
        self.assertFalse(beat["fault"])
        self.assertEqual(beat["say"], "")
        self.assertTrue(beat["proof"])
        led = by_line["LED | STATE | HIGH | -"]
        self.assertEqual(led["device"], "LED")
        self.assertFalse(led["fault"])
        self.assertEqual(led["say"], "")
        legacy = by_line["Distance: 45cm"]
        self.assertFalse(legacy["device"])
        self.assertIn("You are at 45 centimeters", legacy["say"])

    def test_faults_tracked_per_device(self):
        # A healthy device's good lines must not mask a sick one's
        # faults: faults, streaks, and the rescue latch are per
        # device, and proof waits while any device is faulted.
        self.assertIn("let deviceState = {}", APP_JS)
        line = _block("function onMonitorLine(text) {",
                      "async function requestTelemetryProof() {")
        self.assertIn("deviceState", line)
        self.assertIn("rescueDevice", line)
        self.assertIn("deviceFaultActive()", line)

    def test_device_rescue_names_the_device(self):
        # Rescue carries the failed device: the spoken latch names
        # it, recovery needs its own good line, and the global latch
        # keeps the plain sentence.
        self.assertIn("function enterRescue(reason, device) {", APP_JS)
        enter = _block("function enterRescue(reason, device) {",
                       "function exitRescue(backOnline) {")
        self.assertIn("lost the signal from the", enter)
        self.assertIn("rescueDevice", enter)

    def test_diagnosis_sends_device_lines(self):
        # The diagnosis payload is device-scoped: trailing lines from
        # the failed device plus the healthy set, and every wire of
        # the suspect part highlights — not just the one pin.
        diag = _block("async function requestRescueDiagnosis() {",
                      "function enterRescue(reason, device) {")
        self.assertIn("rescueDevice", diag)
        self.assertIn("part_wires", diag)
        self.assertIn("healthy", diag)

    def test_quiet_device_trips_silence_rescue(self):
        # The bonus check: a device that spoke and then went quiet
        # while the stream flows is a silence fault naming it.
        watch = _block("function monitorWatchdog() {",
                       "function startMonitor(port) {")
        self.assertIn("deviceState", watch)
        self.assertIn("went quiet", watch)


class FlowStructureTest(unittest.TestCase):
    """The build loop holds its structure: late turns from a left
    session never repaint the live one, stale schematic fetches never
    paint over newer ones, and STOP hushes speech without killing
    the coach."""

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_stale_turn_helper(self):
        script = (
            _block("// turn:stale:start", "// turn:stale:end")
            + """
console.log(JSON.stringify([
  turnViewStale({id: "a"}, "a"),
  turnViewStale({id: "a"}, "b"),
  turnViewStale({}, "b"),
  turnViewStale({id: "a"}, ""),
  turnViewStale(null, "b"),
]));
""")
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout.strip()),
                         [False, True, False, False, False])

    def test_stale_turns_ignored(self):
        ready = _block("async function sendReady(",
                       "function drainVoiceStep()")
        self.assertIn("turnViewStale(v, wantSid)", ready)
        advance = _block("async function sendNextStep(transcript) {",
                         '$("rescan").onclick')
        self.assertIn("turnViewStale(v, wantSid)", advance)
        self.assertIn('"next step"', advance)

    def test_stale_schematic_never_paints(self):
        fetch = _block("async function fetchSchematic(",
                       "function refreshSchematics()")
        self.assertIn("fetchToken", fetch)
        self.assertIn("learnSid !== wantSid", fetch)

    def test_new_session_starts_clean(self):
        reveal = _block("async function enterReveal(view, project) {",
                        "function revealToBuilding() {")
        self.assertIn("view.steps_total", reveal)
        self.assertIn('codeFetchedSid = ""', reveal)
        self.assertIn("learnStepTotal = total", reveal)

    def test_stop_hushes_without_killing(self):
        self.assertIn("function hushCookSpeech() {", APP_JS)
        hush = _block("function hushCookSpeech() {",
                      "$(\"cook-stop\").onclick")
        self.assertIn("VoiceEngine.stop()", hush)
        self.assertIn("cancelBeatSpeech()", hush)
        self.assertIn("cancelWireSpeech()", hush)
        self.assertIn("$(\"cook-stop\").onclick = () => hushCookSpeech();",
                      APP_JS)
        self.assertNotIn("flipKillswitch(true)", APP_JS)


class BindingTargetTest(unittest.TestCase):
    """Every unguarded `$("id").onX = ...` binding must name an id that
    exists in index.html. A restyle that drops a button (cook-stop,
    cook-exit) otherwise throws TypeError at load and kills every
    binding after it. Bindings guarded by `if ($(id))` are exempt."""

    def test_unguarded_binding_targets_exist_in_html(self):
        bound = re.findall(
            r'^\s*\$\("([^"]+)"\)\.(?:onclick|onchange|onkeydown)\s*=',
            APP_JS, re.M)
        missing = sorted({i for i in bound
                           if 'id="%s"' % i not in INDEX_HTML})
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
