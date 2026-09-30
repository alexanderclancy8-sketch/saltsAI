"""Conversation quality: per-turn metrics, the good/wrong feedback mechanism, and how both feed the nightly
self-reflection and the weekly summary. Offline - the Claude client is the scripted fake from tests/fakes.py."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from jarvis.core import Jarvis
from jarvis.integrations.voice import VoiceError
from jarvis.main import create_app
from jarvis.services import conversation_quality as cq
from tests.fakes import FakeClient, message, text_block, tool_block


def make(settings, script=None, default_text="Right, all quiet, sir."):
    return Jarvis(settings, client=FakeClient(script, default_text))


def turns(j):
    return j.db.query("SELECT * FROM turn_metrics ORDER BY id")


# --------------------------------------------------------------------------- per-turn metrics
async def test_a_spoken_turn_logs_timings_tool_calls_and_its_reply(settings):
    j = make(settings, [message([tool_block("finance_snapshot", {}), tool_block("finance_snapshot", {}, "toolu_2")],
                                "tool_use"),
                        message([text_block("Just under twelve grand in the bank, sir.")])])
    events = j.bus.subscribe()
    await j.brain.ask("How's cash?", "voice")
    [row] = turns(j)
    assert row["mode"] == "voice" and row["user_text"] == "How's cash?"
    assert row["tool_calls"] == 2
    assert row["total_ms"] is not None and row["first_delta_ms"] is not None
    assert row["first_delta_ms"] <= row["total_ms"]
    assert row["failed"] == 0 and row["interrupted"] == 0 and row["format_flags"] == ""
    assert row["reply_text"] == "Just under twelve grand in the bank, sir."
    # the HUD learns the turn id from the thinking and reply events (feedback buttons, first-audio report)
    seen = {}
    while not events.empty():
        ev = events.get_nowait()
        seen[ev["type"]] = ev["data"]
    assert seen["thinking"]["turn_id"] == row["id"] and seen["reply"]["turn_id"] == row["id"]
    await j.http.aclose()


async def test_scheduled_prompts_are_not_measured(settings):
    j = make(settings)
    await j.brain.ask("[Scheduled self-reflection - nobody typed this]\nlook back", "typed")
    await j.brain.ask("[Scheduled check you set up: \"x\"]\ndo it", "typed")
    assert turns(j) == []
    await j.http.aclose()


async def test_a_spoken_reply_that_breaks_the_format_is_flagged(settings):
    j = make(settings, default_text="Certainly! **Cash** is £11,947.32 - see https://example.com")
    await j.brain.ask("How's cash?", "voice")
    flags = set(turns(j)[0]["format_flags"].split(","))
    assert {"markdown", "url", "chatbot_filler", "raw_figure"} <= flags
    await j.http.aclose()


async def test_typed_replies_are_not_format_checked(settings):
    j = make(settings, default_text="**Cash** is fine - see the table.")
    await j.brain.ask("How's cash?", "typed")
    assert turns(j)[0]["format_flags"] == ""
    await j.http.aclose()


async def test_the_same_message_twice_in_a_row_is_flagged_as_a_duplicate(settings):
    j = make(settings)
    await j.brain.ask("Any faults on the panel at Baildon?", "voice")
    await j.brain.ask("any faults on the panel at baildon", "voice")
    await j.brain.ask("And the inbox?", "voice")
    assert [r["duplicate"] for r in turns(j)] == [0, 1, 0]
    await j.http.aclose()


async def test_the_users_text_being_jarvis_own_reply_is_flagged_as_echo(settings):
    j = make(settings, default_text="Two zone faults on the panel, sir - zone three and zone seven.")
    await j.brain.ask("Any faults at Baildon?", "voice")
    await j.brain.ask("two zone faults on the panel sir zone three and zone seven", "voice")
    assert [r["echo_suspect"] for r in turns(j)] == [0, 1]
    await j.http.aclose()


async def test_a_failed_turn_is_recorded_as_failed_and_a_cancelled_one_as_interrupted(settings):
    class Boom:
        async def __aenter__(self):
            raise RuntimeError("boom")

        async def __aexit__(self, *exc):
            return False

    j = make(settings)
    j.client.beta.messages.stream = lambda *a, **k: Boom()  # type: ignore[method-assign]
    await j.brain.ask("How's cash?", "voice")
    assert turns(j)[0]["failed"] == 1 and turns(j)[0]["format_flags"] == ""

    started = asyncio.Event()

    class NeverEnds:
        async def __aenter__(self):
            started.set()
            await asyncio.sleep(10)

        async def __aexit__(self, *exc):
            return False

    j.client.beta.messages.stream = lambda *a, **k: NeverEnds()  # type: ignore[method-assign]
    task = asyncio.create_task(j.brain.ask("Anything urgent?", "voice"))
    await asyncio.wait_for(started.wait(), timeout=2)
    await j.brain.interrupt()
    with pytest.raises(asyncio.CancelledError):
        await task
    last = turns(j)[-1]
    assert last["interrupted"] == 1 and last["failed"] == 0
    await j.http.aclose()


async def test_a_metrics_failure_never_breaks_the_conversation(settings):
    j = make(settings)
    j.db.execute("DROP TABLE turn_metrics")
    assert await j.brain.ask("How's cash?", "voice") == "Right, all quiet, sir."
    await j.http.aclose()


async def test_a_timed_transcription_is_attached_to_the_next_spoken_turn_only(settings):
    j = make(settings)
    j.quality.note_stt(850)
    await j.brain.ask("How's cash?", "voice")
    await j.brain.ask("And the inbox?", "voice")
    assert [r["stt_ms"] for r in turns(j)] == [850, None]
    j.quality.note_stt(400)
    await j.brain.ask("typed question", "typed")  # typed turns never consume (or get) an STT time
    assert turns(j)[-1]["stt_ms"] is None
    await j.http.aclose()


async def test_a_stale_transcription_time_is_ignored(settings):
    j = make(settings)
    j.quality._pending_stt = (850, time.monotonic() - cq.PENDING_STT_TTL_S - 5)  # noqa: SLF001
    await j.brain.ask("How's cash?", "voice")
    assert turns(j)[0]["stt_ms"] is None
    await j.http.aclose()


async def test_voice_events_and_first_audio(settings):
    j = make(settings)
    q = j.quality
    assert q.record_event("echo_suppressed", "self-echo") and q.record_event("stt_empty") and q.record_event("stt_failure", "503")
    assert q.record_event("nonsense") is False
    await j.brain.ask("How's cash?", "voice")
    await j.brain.ask("Anything else?", "voice")
    first, second = turns(j)
    q.record_first_audio(1200, first["id"])
    q.record_first_audio(9999, first["id"])       # only the first report counts
    q.record_first_audio(800)                     # no id: the latest spoken turn
    first, second = turns(j)
    assert first["first_audio_ms"] == 1200 and second["first_audio_ms"] == 800
    s = q.stats(7)
    assert s["events"] == {"stt_failure": 1, "stt_empty": 1, "echo_suppressed": 1}
    assert s["first_audio_ms_p50"] is not None and s["turns"] == 2
    await j.http.aclose()


# --------------------------------------------------------------------------- feedback
async def test_feedback_defaults_to_the_latest_turn_and_keeps_one_verdict_per_turn(settings):
    j = make(settings)
    assert j.quality.feedback("wrong") is None  # nothing to mark yet
    await j.brain.ask("Who's on call?", "voice")
    await j.brain.ask("Is the Kestrel service booked?", "voice")
    saved = j.quality.feedback("wrong", "It's booked for Thursday")
    assert saved["turn_id"] == turns(j)[1]["id"]
    j.quality.feedback("wrong")                       # no note: the earlier one is kept
    row = j.db.query_one("SELECT * FROM turn_feedback")
    assert row["note"] == "It's booked for Thursday" and row["user_text"] == "Is the Kestrel service booked?"
    assert row["reply_text"] == "Right, all quiet, sir."
    j.quality.feedback("good", turn_id=turns(j)[1]["id"])  # changed their mind: replaced, old note dropped
    row = j.db.query_one("SELECT * FROM turn_feedback")
    assert row["rating"] == "good" and row["note"] == "" and j.db.query_one("SELECT COUNT(*) AS n FROM turn_feedback")["n"] == 1
    with pytest.raises(ValueError):
        j.quality.feedback("meh")
    await j.http.aclose()


async def test_saying_that_was_wrong_flags_the_previous_turn_and_is_still_answered(settings):
    j = make(settings, [message([text_block("It's Keighley Road, sir.")]),
                        message([text_block("Apologies - Halifax Road, then.")])])
    await j.brain.ask("Where's the Kestrel job?", "voice")
    reply = await j.brain.ask("That was wrong, sir, it's Halifax Road", "voice")
    assert reply == "Apologies - Halifax Road, then."   # still a normal turn: Jarvis replies and corrects
    first, second = turns(j)
    fb = j.db.query("SELECT * FROM turn_feedback")
    assert len(fb) == 1 and fb[0]["turn_id"] == first["id"] and fb[0]["rating"] == "wrong"
    assert "Halifax Road" in fb[0]["note"] and fb[0]["user_text"] == "Where's the Kestrel job?"
    assert second["id"] != first["id"]
    await j.http.aclose()


async def test_a_spoken_good_verdict_and_ordinary_sentences(settings):
    j = make(settings)
    await j.brain.ask("Where's the Kestrel job?", "voice")
    await j.brain.ask("That was spot on, thanks", "voice")
    await j.brain.ask("That's good, now email them the quote", "voice")   # a request, not a verdict
    await j.brain.ask("Why was that wrong?", "voice")                     # a question, not a verdict
    ratings = [(f["turn_id"], f["rating"]) for f in j.db.query("SELECT * FROM turn_feedback")]
    assert ratings == [(turns(j)[0]["id"], "good")]
    await j.http.aclose()


# --------------------------------------------------------------------------- endpoints
def test_feedback_and_voice_event_endpoints_and_the_stt_timing(settings):
    j = make(settings)
    app = create_app(settings, j)

    async def fake_transcribe(data, mime):
        if data == b"speech":
            return "how is cash"
        if data == b"silence":
            return ""
        raise VoiceError("Deepgram down")

    j.voice.transcribe = fake_transcribe  # type: ignore[method-assign]
    with TestClient(app) as c:
        assert c.post("/api/feedback", json={"rating": "good"}).status_code == 404  # no turn yet
        assert c.post("/api/chat", json={"text": "How's cash?", "mode": "voice"}).status_code == 200
        turn_id = turns(j)[0]["id"]
        r = c.post("/api/feedback", json={"rating": "wrong", "note": "Wrong figure", "turn_id": turn_id})
        assert r.status_code == 200 and r.json() == {"turn_id": turn_id, "rating": "wrong", "note": "Wrong figure"}
        assert c.post("/api/feedback", json={"rating": "meh"}).status_code == 422
        assert c.post("/api/feedback", json={"rating": "good", "turn_id": 9999}).status_code == 404

        assert c.post("/api/voice-events", json={"kind": "echo_suppressed"}).status_code == 200
        assert c.post("/api/voice-events", json={"kind": "first_audio", "ms": 640, "turn_id": turn_id}).status_code == 200
        assert c.post("/api/voice-events", json={"kind": "first_audio"}).status_code == 400
        assert c.post("/api/voice-events", json={"kind": "bogus"}).status_code == 400
        assert turns(j)[0]["first_audio_ms"] == 640

        # /api/stt: a good transcript is timed, an empty one and a failure are counted
        assert c.post("/api/stt", files={"audio": ("s.webm", b"speech", "audio/webm")}).json() == {"text": "how is cash"}
        assert j.quality._pending_stt is not None  # noqa: SLF001
        assert c.post("/api/stt", files={"audio": ("s.webm", b"silence", "audio/webm")}).json() == {"text": ""}
        assert c.post("/api/stt", files={"audio": ("s.webm", b"x", "audio/webm")}).status_code == 503

        data = c.get("/api/quality").json()
        assert data["stats"]["events"] == {"stt_failure": 1, "stt_empty": 1, "echo_suppressed": 1}
        assert data["stats"]["wrong"] == 1 and "1 turns" in data["summary"]


# --------------------------------------------------------------------------- nightly reflection + weekly summary
async def test_the_nightly_reflection_gets_the_metrics_and_flagged_turns_once(settings):
    j = make(settings, [message([text_block("It's Keighley Road, sir.")]),                # the spoken turn
                        message([text_block("Proposal: always name the site first.")]),   # first reflection
                        message([text_block("Nothing to add.")])])                        # second reflection
    await j.brain.ask("Where's the Kestrel job?", "voice")
    j.quality.feedback("wrong", "It was Halifax Road")
    j.quality.record_event("echo_suppressed")
    j.quality.record_event("stt_empty")

    reply = await j.self_learning.reflect()
    assert reply == "Proposal: always name the site first."
    prompt = j.brain.messages[2]["content"][-1]["text"]
    assert "<conversation_quality>" in prompt and "WRONG" in prompt and "It was Halifax Road" in prompt
    assert "1 echoes suppressed" in prompt and "1 empty transcripts" in prompt
    assert "proposal" in prompt.lower() and "remember" in prompt
    assert j.db.get_kv(cq.LAST_REFLECTION_KEY) == "Proposal: always name the site first."
    # the reflection's own turn isn't counted, and what it already saw isn't shown again
    assert len(turns(j)) == 1
    j.db.add_transcript("user", "a later chat")
    await j.self_learning.reflect()
    assert "<conversation_quality>" not in j.brain.messages[-2]["content"][-1]["text"]
    await j.http.aclose()


async def test_the_weekly_summary_is_a_short_display_note_with_the_latest_proposals(settings):
    j = make(settings)
    assert await j.quality.weekly_summary() == ""   # nothing happened: no note
    await j.brain.ask("How's cash?", "voice")
    j.quality.feedback("wrong", "bad figure")
    j.db.set_kv(cq.LAST_REFLECTION_KEY, "Proposal: round the figures.")
    text = await j.quality.weekly_summary()
    assert "1 turns" in text and "0 good, 1 wrong" in text and "Proposal: round the figures." in text
    [note] = j.db.recent_notifications(1)
    assert note["title"] == "Conversation quality - this week" and note["level"] == "info"
    await j.http.aclose()
