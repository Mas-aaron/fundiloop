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
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APP_JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


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


class BrowserFlashBaudTest(unittest.TestCase):
    """Browser flash runs at fixed 115200 — the baud this link holds.

    The ladder wasted minutes proving 921600/460800 corrupt after the
    stub speed-up, and a well-meant flash-ID gate then blocked 115200
    even though the loader had already detected the chip (Manufacturer
    68, 4MB) inside main(). One speed, one re-sync retry, no gate:
    main()'s own detection is the authority.
    """

    def test_fixed_115200_only(self):
        body = _block("async function webFlash", "// Hands-free voice")
        self.assertIn("const BAUDS = [115200, 115200]", body)
        self.assertNotIn("921600", body)
        self.assertNotIn("savedBaud", body)

    def test_no_flash_id_gate_before_writing(self):
        body = _block("async function webFlash", "// Hands-free voice")
        self.assertNotIn("flash chip not answering", body)


class NativeServerFlashTest(unittest.TestCase):
    """The Arduino-style native upload is the primary flash path.

    Arduino IDE/native esptool flashes this board at 921600 with no
    BOOT press, while the Web Serial path is the fragile one. Both
    panels therefore expose the server endpoint with an explicit port
    choice, and the silent-board guidance names it first.
    """

    def test_native_controls_exist_in_both_panels(self):
        for element_id in ("flash-port", "flash-esp32", "flash-status",
                           "cook-flash-port", "cook-flash-esp32",
                           "cook-flash-status"):
            self.assertIn('id="%s"' % element_id, INDEX_HTML)

    def test_flash_step_posts_explicit_port_to_native_endpoint(self):
        body = _block("async function flashStep", "async function webFlash")
        self.assertIn('"/step/" + learnStepN + "/flash"', body)
        self.assertIn("JSON.stringify({ port })", body)
        self.assertIn("921600", body)

    def test_native_controls_share_busy_guard_and_guidance(self):
        guard = _block("function setFlashButtonsDisabled",
                       "async function refreshFlashPorts")
        self.assertIn("flash-esp32", guard)
        self.assertIn("cook-flash-esp32", guard)
        label = _block("function stepSketchLabel",
                       "// Click a wire to isolate it")
        self.assertLess(label.index("FLASH ESP32"),
                        label.index("FLASH IN BROWSER"))
        speech = _block("function flashSpeech", "function renderCheckView")
        self.assertIn("Press FLASH ESP32, then CHECK.", speech)


class BrowserFlashDownloadTest(unittest.TestCase):
    """Binary downloads must survive a brief server hiccup.

    A backend that restarts or drops mid-download used to surface as
    a bare "Failed to fetch" with no retry and no guidance.
    """

    def test_each_file_download_is_retried(self):
        body = _block("async function webFlash", "// Hands-free voice")
        self.assertIn("attempt < 3", body)
        self.assertIn("download failed after retries", body)

    def test_network_failure_gets_server_guidance(self):
        kind = _block("function flashFailureKind",
                      "async function closeWebPortQuiet")
        self.assertIn("unreachable", kind)
        catch = _block('webLog(statusId, "Browser flash failed: " + msg);',
                       "  } finally {")
        self.assertIn("unreachable", catch)
        self.assertIn("server stopped answering", catch)


class CookStepCodeVisibleTest(unittest.TestCase):
    """The step sketch the UI tells you to copy must be on screen.

    Cook mode wrote step_code into the Final Code panel, which stays
    hidden until DONE — instruction visible, code invisible.
    """

    def test_visible_step_code_pre_exists(self):
        self.assertIn('id="cook-step-out"', INDEX_HTML)
        self.assertIn('id="cook-stepcode"', INDEX_HTML)

    def test_render_writes_step_code_to_visible_pre(self):
        body = _block("function renderCook", "function drawCookBoxes")
        self.assertIn("cook-step-out", body)


class EyeballMoveOnTest(unittest.TestCase):
    """Confirming without a camera must be unmissable and hands-free.

    A pending eyeball ask gets a dedicated card (question + YES /
    NOT YET), voice answers route to the confirm endpoint while it is
    pending, and MOVE ON advances learner-paced from anywhere.
    """

    def test_eyeball_card_exists_and_paints(self):
        for element_id in ("cook-eyeball", "cook-eyeball-q",
                           "cook-eyeball-yes", "cook-eyeball-no",
                           "cook-move-on"):
            self.assertIn('id="%s"' % element_id, INDEX_HTML)
        paint = _block("function paintConfirm", "async function sendConfirm")
        self.assertIn("cook-eyeball", paint)

    def test_voice_answers_route_before_other_phrases(self):
        body = _block("async function cookCommand", "// Gated tool")
        self.assertIn("pendingConfirmSid", body)
        self.assertLess(body.index("sendConfirm(true)"),
                        body.index("cookPhaseDone()"))
        self.assertIn("/api/session/advance", APP_JS)


class CookButtonAuditTest(unittest.TestCase):
    """Every cook button must visibly do something.

    CONFIRM used to return silently whenever a check (often the AUTO
    loop) was mid-flight, and its errors only reached a banner hidden
    behind the cook overlay. LOOK fetched the removed camera describe
    endpoint and could only fail.
    """

    def test_confirm_waits_for_inflight_check_and_reports(self):
        body = _block("async function sendConfirm",
                      '$("confirm-yes").onclick')
        self.assertIn("while", body)
        self.assertIn("cook-heard", body)

    def test_look_uses_the_diagram_not_the_camera(self):
        body = _block("async function cookLook", "async function cookSendCode")
        self.assertNotIn("/api/vision/describe", body)
        self.assertIn("speakWireList", body)


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


if __name__ == "__main__":
    unittest.main()
