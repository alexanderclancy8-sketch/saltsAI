"""Static checks on the HUD's barge-in wiring (there is no JS test runner in this repo, so - like the self-echo
test in test_prompts.py - these read hud.js/index.html and assert the guards are present)."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "jarvis" / "web"
HUD = (WEB / "hud.js").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")


def _utterance_body() -> str:
    start = HUD.index("function utterance(raw)")
    return HUD[start:HUD.index("mic.addEventListener(\"click\"", start)]


def test_mic_stream_requests_echo_cancellation_noise_suppression_and_agc():
    m = re.search(r"const MIC_CONSTRAINTS = \{([^}]*)\}", HUD)
    assert m, "MIC_CONSTRAINTS missing"
    for key in ("echoCancellation", "noiseSuppression", "autoGainControl"):
        assert f"{key}: true" in m.group(1)
    assert "getUserMedia({ audio: MIC_CONSTRAINTS })" in HUD


def test_barge_in_is_a_setting_default_on_and_gated_on_the_echo_guards():
    assert 'store.get("bargein", "1") !== "0"' in HUD
    assert 'id="set-bargein"' in INDEX
    body = HUD[HUD.index("function bargeInAllowed"):]
    body = body[:body.index("}")]
    assert "S.bargeIn" in body and "stt.echoCancelled !== false" in body and "BARGE_IN_MIN_WAKE_CHARS" in body


def test_echo_window_stays_a_strict_allowlist_and_stop_phrases_bypass_the_setting():
    body = _utterance_body()
    window = body[body.index('S.listenMode === "wake" && echoWindowOpen()'):body.index("const bare =")]
    # Stop phrase handled first, before (and regardless of) the barge-in setting.
    assert window.index("heard.stop") < window.index("bargeInAllowed()")
    # Anything without the wake word - or with barge-in unavailable - is dropped.
    assert "!heard.hasWake || !bargeInAllowed()" in window
    # Own-voice guard and lead-position guard.
    assert "matchesOwnSpeech(text)" in window and "heard.wakeInLead" in window
    # A barge-in stops the whole turn (not just the audio) and shows a cue.
    assert "stopEverything()" in window and "bargeInCue(" in window


def test_stop_phrase_cancels_the_turn_not_just_the_current_sentence():
    body = _utterance_body()
    assert "if (speaker.active) speaker.stop();" not in body
    assert 'stopEverything(); bargeInCue("stop")' in body


def test_capture_after_bare_wake_is_one_shot_short_lived_and_outside_the_echo_window():
    assert "BARGE_IN_CAPTURE_MS" in HUD
    body = _utterance_body()
    # The capture exception is consumed in the wake branch, which is only reached after the echo-window allowlist.
    assert body.index("echoWindowOpen()") < body.index("S.captureUntil > Date.now()")
    assert "S.captureUntil = 0; send(text" in body


def test_speaker_ignores_callbacks_from_a_cancelled_generation():
    assert "gen: 0" in HUD and "this.gen++" in HUD
    assert HUD.count("gen !== this.gen") >= 3 or HUD.count("gen === this.gen") >= 2
    assert "stopped while the audio was starting" in HUD


def test_background_mic_start_does_not_cut_off_speech():
    assert "async start({ keepSpeaking = false } = {})" in HUD
    assert "stt.start({ keepSpeaking: keepSpeaking || speaker.active })" in HUD
    assert "this.start({ keepSpeaking: true })" in HUD
