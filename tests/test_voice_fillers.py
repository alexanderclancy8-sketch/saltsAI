"""Thinking-time acknowledgment fillers: the server-side flag, plus structural checks on hud.js.

The behaviour itself lives in the browser (hud.js) and there is no JS test runner in this repo, so - like
test_prompts.py's HUD checks - these pin the wiring that enforces each guarantee so a refactor can't drop it.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from jarvis.integrations.voice import Voice
from jarvis.settings_store import SECTIONS, SettingsStore

HUD = (Path(__file__).resolve().parent.parent / "jarvis" / "web" / "hud.js").read_text(encoding="utf-8")


def _block(start: str, end: str) -> str:
    i = HUD.index(start)
    return HUD[i:HUD.index(end, i)]


def test_filler_flag_defaults_on_reaches_the_hud_and_is_editable(settings):
    assert settings.voice_ack_fillers is True
    voice = Voice(settings, SimpleNamespace())
    assert voice.client_config()["ack_fillers"] is True
    settings.voice_ack_fillers = False
    assert voice.client_config()["ack_fillers"] is False

    fields = {f.key: f for s in SECTIONS if s.id == "voice" for f in s.fields}
    assert fields["voice_ack_fillers"].kind == "bool"
    store = SettingsStore(settings)
    assert store.update({"voice_ack_fillers": False}, []) == {}


def test_filler_delay_is_a_named_constant_in_the_1_5_to_2_second_range():
    m = re.search(r"const FILLER_DELAY_MS = (\d+);", HUD)
    assert m and 1500 <= int(m.group(1)) <= 2000
    assert "setTimeout(() => this.fire(turn), FILLER_DELAY_MS)" in HUD


def test_filler_phrases_follow_the_running_tool_with_a_generic_default():
    assert "Let me check the accounts." in HUD and "Let me look at the jobs." in HUD and "Checking your email." in HUD
    assert "One moment." in HUD and "Let me think about that." in HUD
    assert "FILLER_TOOL_PHRASES" in HUD and "FILLER_DEFAULT_PHRASES" in HUD
    # the tool-start event feeds the filler; it never speaks from there
    tool_case = _block('case "tool":', 'case "reply":')
    assert "filler.tool(d)" in tool_case and "speaker" not in tool_case


def test_filler_is_only_for_spoken_turns_and_only_begun_from_send():
    # PR #11 (reply suggestions) changed the third param from a bare `spoken` bool to an `opts` object
    # (it needs opts.compose); `true` is still accepted and normalized to { spoken: true } for old callers.
    assert "function send(text, mode = \"typed\", opts = {}) {" in HUD
    assert "if (opts === true) opts = { spoken: true };" in HUD
    assert "const spoken = !!opts.spoken;" in HUD
    assert "filler.begin(spoken && mode === \"voice\")" in HUD
    # utterance() is the only caller that marks a turn as spoken; typed/click shortcuts never do
    assert HUD.count(", \"voice\", true)") == 2
    assert "if (!spoken || !this.enabled()) return;" in HUD
    assert "S.voice.ack_fillers !== false" in HUD
    # the only place a filler is ever queued
    assert HUD.count("speaker.enqueue(this.phrase(turn.tool), true)") == 1


def test_filler_fires_at_most_once_and_rechecks_everything_at_the_moment_of_speaking():
    fire = _block("    fire(turn) {", "    // STT/VAD heard the owner")
    assert "this.turn !== turn || turn.blocked || turn.fired" in fire
    assert "turn.fired = true" in fire
    assert fire.index("turn.fired = true") < fire.index("speaker.enqueue(")
    for guard in ('S.hudState !== "thinking"', "current.dataset.raw", "speaker.active", "speaker.queue.length",
                  "stt.finals.trim()", "stt.silenceTimer"):
        assert guard in fire, guard


def test_reply_user_speech_and_stops_cancel_the_filler():
    assert "filler.block(); // the real reply has started" in HUD           # first delta
    assert HUD.count("filler.end()") >= 5                                    # reply, error, reset, Stop, speaker.stop()
    reply_case = _block('case "reply":', 'case "error":')
    assert reply_case.index("filler.end()") < reply_case.index("speaker.feed(")
    stop_fn = _block("    stop() {", "  const shouldSpeak")
    assert "filler.end()" in stop_fn
    # STT / VAD hooks: a transcript or speech_started that is not our own voice
    assert HUD.count("filler.userSpeech()") >= 4
    assert "speech_started\" && !echoWindowOpen()) filler.userSpeech()" in HUD
    # an unstarted filler is dropped from the queue and skipped, a playing one is left to finish
    block = _block("    block() {", "    end() {")
    assert "item.cancelled = true" in block and "!item.started" in block and "speaker.queue.splice" in block
    assert "if (item.cancelled)" in _block("    async next() {", "    speakBrowser(text)")


def test_filler_uses_the_normal_echo_path_and_is_not_an_exchange():
    enqueue = _block("    enqueue(sentence, filler = false) {", "    async fetchAudio")
    assert "this.recent.push({ text: clean, at: now })" in enqueue          # echo guard knows what was said
    nxt = _block("    async next() {", "    speakBrowser(text)")
    # a filler finishing mid-turn keeps the echo tail but does NOT extend the follow-up window or flip the HUD
    assert "if (wasFiller && filler.inFlight()) {" in nxt
    assert nxt.index("this.lastSpokeAt = Date.now()") < nxt.index("if (wasFiller && filler.inFlight())")
    assert nxt.index("if (wasFiller && filler.inFlight())") < nxt.index("extendFollowUp()")
    assert "if (!item.filler) setHud(\"speaking\")" in nxt
    # fillers never reach the conversation/transcript or the server
    fire = _block("    fire(turn) {", "    // STT/VAD heard the owner")
    for forbidden in ("addMessage", "send(", "S.ws", "extendFollowUp", "followUpUntil"):
        assert forbidden not in fire, forbidden


def test_unprompted_speech_rule_is_unchanged():
    assert "Jarvis never speaks unprompted: there is deliberately no on-load greeting." in HUD
    notif = _block('case "notification":', 'case "owner_update":')
    assert "speaker" not in notif and "say(" not in notif and "filler" not in notif  # pushes still never speak
