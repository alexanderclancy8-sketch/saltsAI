"""Voice conversation flow (Phase 2): near-duplicate detection (server side, unit-tested), the silence-timeout
setting, and structural checks on hud.js for the fuzzy echo drop, end-of-turn tolerance and mic-press barge-in
(there is no JS test runner in this repo, so - like test_hud_bargein.py - these pin the wiring)."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from jarvis.brain.repeats import REPEAT_WINDOW_S, RepeatDetector, repeat_note, similarity
from jarvis.core import Jarvis
from jarvis.integrations.voice import Voice
from jarvis.settings_store import SECTIONS, SettingsStore
from tests.fakes import FakeClient, message, text_block

HUD = (Path(__file__).resolve().parent.parent / "jarvis" / "web" / "hud.js").read_text(encoding="utf-8")


def _block(start: str, end: str) -> str:
    i = HUD.index(start)
    return HUD[i:HUD.index(end, i)]


# ---------------------------------------------------------------- near-duplicate detection
def test_similarity_ignores_case_punctuation_and_spacing():
    assert similarity("What's my cash position?", "whats my cash  position") > 0.9
    assert similarity("What's my cash position?", "Book a service visit for Kestrel") < 0.5
    assert similarity("", "anything") == 0.0


def test_repeat_detector_flags_near_identical_message_within_the_window():
    d = RepeatDetector()
    assert d.check("How is cash looking today?", now=100.0) is None          # first message: never a repeat
    age = d.check("how is cash looking today", now=112.0)
    assert age == 12.0
    assert "possible repeat" in repeat_note(age) and "12s" in repeat_note(age)


def test_repeat_detector_ignores_different_or_stale_messages():
    d = RepeatDetector()
    d.check("How is cash looking today?", now=0.0)
    assert d.check("Which jobs are overdue this week?", now=5.0) is None     # different
    d.check("How is cash looking today?", now=10.0)
    assert d.check("How is cash looking today?", now=10.0 + REPEAT_WINDOW_S + 1) is None  # too long ago
    assert repeat_note(None) == ""


def test_repeat_detector_compares_against_the_previous_message_only():
    d = RepeatDetector()
    d.check("How is cash looking today?", now=0.0)
    d.check("Which jobs are overdue?", now=5.0)
    assert d.check("How is cash looking today?", now=10.0) is None


async def test_brain_tags_a_repeated_message_but_not_a_fresh_one(settings):
    j = Jarvis(settings, client=FakeClient([message([text_block("Healthy, sir.")]),
                                            message([text_block("As I said, healthy.")]),
                                            message([text_block("Four overdue.")])]))
    await j.brain.ask("How's cash looking?", "voice")
    await j.brain.ask("How's cash looking", "voice")
    await j.brain.ask("Which jobs are overdue?", "voice")
    texts = [m["content"][-1]["text"] for m in j.brain.messages if m["role"] == "user"]
    assert "possible repeat" not in texts[0]
    assert "possible repeat" in texts[1] and texts[1].startswith("[spoken") and texts[1].endswith("How's cash looking")
    assert "possible repeat" not in texts[2]
    # the stored transcript is the owner's words only, never the annotation
    assert all("possible repeat" not in r["text"] for r in j.db.recent_transcript(50))
    await j.http.aclose()


def test_system_prompt_explains_the_repeat_note():
    src = (Path(__file__).resolve().parent.parent / "jarvis" / "brain" / "prompts.py").read_text(encoding="utf-8")
    assert "[possible repeat: ...]" in src


# ---------------------------------------------------------------- silence timeout setting
def test_silence_timeout_is_a_setting_that_reaches_the_hud(settings):
    assert settings.voice_silence_ms == 1200
    voice = Voice(settings, SimpleNamespace())
    assert voice.client_config()["silence_ms"] == 1200
    settings.voice_silence_ms = 2000
    assert voice.client_config()["silence_ms"] == 2000
    fields = {f.key: f for s in SECTIONS if s.id == "voice" for f in s.fields}
    assert fields["voice_silence_ms"].kind == "number"
    assert SettingsStore(settings).update({"voice_silence_ms": 1500}, []) == {}


def test_hud_end_of_turn_uses_the_setting_and_waits_longer_after_trailing_fillers():
    assert "silence_ms: 1200" in HUD
    fn = _block("function endOfTurnMs(text)", "const ECHO_SIMILAR_MS")
    assert "S.voice.silence_ms" in fn and "SILENCE_MIN_MS" in fn and "SILENCE_MAX_MS" in fn
    assert "TRAILING_FILLERS.has(" in fn and "TRAILING_EXTRA_MS" in fn
    for word in ('"and"', '"so"', '"um"'):
        assert word in HUD[HUD.index("const TRAILING_FILLERS"):HUD.index("function endOfTurnMs")]
    final_heard = _block("    finalHeard() {", "    // keepSpeaking:")
    assert "endOfTurnMs(this.finals)" in final_heard and "PTT_SILENCE_MS" not in final_heard


# ---------------------------------------------------------------- fuzzy echo drop
def test_hud_drops_fuzzy_matches_of_the_last_reply_within_ten_seconds():
    assert re.search(r"const ECHO_SIMILAR_MS = 10000;", HUD)
    assert re.search(r"const ECHO_SIMILARITY = 0\.\d+;", HUD)
    assert "function similarToRecentReply(words)" in HUD
    echo = _block("  function looksLikeSelfEcho(text) {", "  // Jarvis never speaks unprompted")
    # a short, explicit stop phrase is still let through before the fuzzy check can drop it
    assert echo.index("STOP_PHRASE_TEST_RE.test(content.join") < echo.index("similarToRecentReply(content)")
    # utterance() (every listener's last line of defence) still runs looksLikeSelfEcho before anything is sent
    utt = HUD[HUD.index("function utterance(raw)"):]
    # (the quality log is told about each drop: voiceEvent echo_suppressed - see tests/test_conversation_regression.py)
    assert utt.index("if (looksLikeSelfEcho(text)) {") < utt.index("send(")
    assert 'voiceEvent({ kind: "echo_suppressed"' in utt[:utt.index("send(")]


# ---------------------------------------------------------------- mic-press barge-in
def test_mic_press_while_speaking_cuts_the_whole_turn_and_opens_the_mic():
    fn = _block("  function micPressBargeIn() {", '  $("#btn-stop")')
    assert "speaker.active" in fn and "speaker.queue.length" in fn and "speaker.browserSpeaking" in fn
    assert "stopEverything()" in fn and "stt.start()" in fn
    click = _block('mic.addEventListener("click"', "  function stopEverything()")
    assert click.index("micPressBargeIn()") < click.index("stt.on")
    assert "if (!micPressBargeIn()) stt.start();" in HUD   # hold-Space push-to-talk too
