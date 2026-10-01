"""Spoken messages: stuttering cumulative partials collapse to the longest version, and Jarvis's own voice coming
back in through the mic is ignored (server side; web/hud.js has the matching client-side checks, pinned below)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jarvis.brain.utterance import ECHO_WINDOW_S, collapse_cumulative, is_echo_of_reply, screen_voice
from jarvis.core import Jarvis
from tests.fakes import FakeClient, message, text_block

HUD = (Path(__file__).resolve().parent.parent / "jarvis" / "web" / "hud.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------- de-duplication
def test_collapse_keeps_only_the_longest_version_of_a_cumulative_transcript():
    assert collapse_cumulative("I I I just I just asked I just asked you") == "I just asked you"
    assert collapse_cumulative("What's What's my What's my cash What's my cash position?") == "What's my cash position?"


def test_collapse_ignores_case_and_punctuation_when_matching_chunks():
    assert collapse_cumulative("book book a, Book a service book a service visit") == "book a service visit"


def test_collapse_handles_a_single_repeated_chunk_of_three_or_more_words():
    assert collapse_cumulative("I just asked I just asked you today") == "I just asked you today"


def test_collapse_leaves_ordinary_speech_and_deliberate_repetition_alone():
    for text in ("What is my cash position", "no no no", "very very good", "go go go now", "I I just",
                 "please please send send the email", "Is the 5 pm visit at 5 pm or 6 pm", "", "yes"):
        assert collapse_cumulative(text) == text.strip()


def test_collapse_keeps_the_text_after_the_stutter():
    assert collapse_cumulative("Send Send the Send the invoice Send the invoice to Kestrel today") \
        == "Send the invoice to Kestrel today"


# ---------------------------------------------------------------- echo detection
REPLY = "Cash is healthy at forty thousand pounds, sir, and four jobs are overdue."


def test_echo_of_the_last_reply_is_detected_even_when_garbled_or_partial():
    assert is_echo_of_reply("Cash is healthy at forty thousand pounds sir", REPLY)
    assert is_echo_of_reply("cash is healthy at for tea thousand pounds sir and four jobs are overdue", REPLY)
    assert is_echo_of_reply("four jobs are overdue", REPLY)


def test_a_genuine_new_request_or_a_short_reply_is_not_echo():
    assert not is_echo_of_reply("Which jobs are overdue this week?", REPLY)
    assert not is_echo_of_reply("yes", REPLY)
    assert not is_echo_of_reply("go on", REPLY)
    assert not is_echo_of_reply("Cash is healthy at forty thousand pounds sir", "")


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, kind, data=None):
        self.events.append((kind, data))


class _DB:
    def __init__(self, text, at):
        self.row = {"created_at": at.isoformat(timespec="seconds"), "text": text}

    def query_one(self, sql, params=()):
        return self.row


def _j(text, at):
    return type("J", (), {"db": _DB(text, at), "bus": _Bus()})()


def test_screen_voice_drops_echo_only_shortly_after_the_reply_and_only_for_voice():
    now = datetime(2026, 10, 1, 12, 0, 30, tzinfo=timezone.utc)
    heard = "cash is healthy at forty thousand pounds sir"
    fresh = _j(REPLY, now - timedelta(seconds=5))
    assert screen_voice(fresh, heard, "voice", now=now) is None
    assert fresh.bus.events == [("stopped", {"stopped": False})]
    assert screen_voice(fresh, heard, "typed", now=now) == heard        # typed text is never dropped
    stale = _j(REPLY, now - timedelta(seconds=ECHO_WINDOW_S + 5))
    assert screen_voice(stale, heard, "voice", now=now) == heard        # an old reply can't be echoing
    assert stale.bus.events == []


def test_screen_voice_collapses_the_stutter_and_leaves_typed_text_alone():
    now = datetime(2026, 10, 1, 12, 0, 30, tzinfo=timezone.utc)
    j = _j("Something unrelated entirely.", now - timedelta(seconds=2))
    assert screen_voice(j, "I I I just I just asked I just asked you", "voice", now=now) == "I just asked you"
    assert screen_voice(j, "I I I just I just asked I just asked you", "typed", now=now) \
        == "I I I just I just asked I just asked you"


# ---------------------------------------------------------------- through the brain
async def test_brain_sends_and_stores_only_the_final_utterance(settings):
    j = Jarvis(settings, client=FakeClient([message([text_block("Four overdue.")])]))
    await j.brain.ask("Which Which jobs Which jobs are Which jobs are overdue", "voice")
    sent = [m["content"][-1]["text"] for m in j.brain.messages if m["role"] == "user"]
    assert len(sent) == 1 and sent[0].endswith("Which jobs are overdue") and "Which Which" not in sent[0]
    assert [r["text"] for r in j.db.recent_transcript(10) if r["role"] == "user"] == ["Which jobs are overdue"]
    await j.http.aclose()


async def test_brain_ignores_its_own_reply_picked_up_by_the_mic(settings):
    j = Jarvis(settings, client=FakeClient([message([text_block("Cash is healthy at forty thousand pounds, sir.")])]))
    await j.brain.ask("How's cash looking?", "voice")
    queue = j.bus.subscribe()
    before = len(j.brain.messages)
    reply = await j.brain.ask("cash is healthy at forty thousand pounds sir", "voice")
    assert reply == ""
    assert len(j.brain.messages) == before                                    # never reached the model
    assert [r["text"] for r in j.db.recent_transcript(10) if r["role"] == "user"] == ["How's cash looking?"]
    assert queue.get_nowait()["type"] == "stopped"
    await j.http.aclose()


# ---------------------------------------------------------------- the browser side (no JS runner: pin the wiring)
def test_hud_replaces_a_growing_partial_instead_of_appending_it():
    i = HUD.index("function appendFinal(finals, heard)")
    fn = HUD[i:HUD.index("\n  }\n", i)]
    assert "startsWith(next, held)" in fn and "startsWith(held, next)" in fn
    assert "this.finals = appendFinal(this.finals, heard)" in HUD      # browser speech recognition
    assert "this.finals = appendFinal(this.finals, m.text)" in HUD     # Deepgram live stream
    assert "this.finals += heard" not in HUD and "this.finals += m.text" not in HUD


def test_hud_submits_only_once_the_speaker_pauses():
    # Deepgram: interim results are shown as a caption only; a turn is committed on speech_final / utterance_end.
    live = HUD[HUD.index("    onDeepgram(m) {"):HUD.index("    // The live Deepgram stream failed")]
    assert "if (m.speech_final && this.finals.trim()) this.commit();" in live
    assert 'm.type === "utterance_end" && this.finals.trim()) this.commit()' in live
    assert "utterance(m.text)" not in live


@pytest.mark.parametrize("needle", ['case "stopped": filler.end();'])
def test_hud_ends_a_pending_filler_when_the_server_drops_an_echo(needle):
    assert needle in HUD
