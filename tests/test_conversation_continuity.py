"""Conversation continuity: redacted history, stutter-free utterances, open requests carried forward and
history search - all on top of the existing transcript table (2-year retention, redacted at rest)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis import history
from jarvis.brain.prompts import build_system
from jarvis.brain.tools import TOOLS_BY_NAME, dispatch
from jarvis.core import Jarvis
from jarvis.db import Database
from tests.fakes import FakeClient, message, text_block

HUD = (Path(__file__).resolve().parent.parent / "jarvis" / "web" / "hud.js").read_text(encoding="utf-8")


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def _old_row(db: Database, role: str, text: str, age: timedelta) -> None:
    db.execute("INSERT INTO transcript (created_at, role, text) VALUES (?,?,?)", (_iso(age), role, text))


# ---------------------------------------------------------------- stutter
def test_cumulative_partials_collapse_to_the_final_version():
    stuttered = "ladder ladder inspection ladder inspection jobs ladder inspection jobs for all engineers tomorrow"
    assert history.collapse_cumulative(stuttered) == "ladder inspection jobs for all engineers tomorrow"


def test_ordinary_speech_is_left_alone():
    assert history.collapse_cumulative("that is very very good") == "that is very very good"
    assert history.collapse_cumulative("Raise a job for Kestrel\nplease") == "Raise a job for Kestrel\nplease"


def test_partial_utterances_replace_each_other_in_the_transcript(tmp_path):
    db = Database(tmp_path / "t.db")
    db.add_transcript("user", "ladder")
    db.add_transcript("user", "ladder inspection")
    db.add_transcript("user", "Ladder inspection jobs for all engineers tomorrow")
    rows = db.recent_transcript(10)
    assert [r["text"] for r in rows] == ["Ladder inspection jobs for all engineers tomorrow"]


def test_a_new_request_after_a_reply_is_not_merged(tmp_path):
    db = Database(tmp_path / "t.db")
    db.add_transcript("user", "ladder inspections")
    db.add_transcript("assistant", "Queued.")
    db.add_transcript("user", "ladder inspections for Friday")
    assert len(db.recent_transcript(10)) == 3


# ---------------------------------------------------------------- redaction and retention
def test_transcript_is_redacted_before_it_is_stored(tmp_path):
    db = Database(tmp_path / "t.db")
    db.add_transcript("user", "The alarm code is 4821 and the key is sk-abcdefghijkl1234")
    db.add_transcript("assistant", "Engineer code: 99-12-77 noted.")
    text = " ".join(r["text"] for r in db.recent_transcript(10))
    for secret in ("4821", "sk-abcdefghijkl1234", "99-12-77"):
        assert secret not in text
    assert history.REDACTED in text


def test_codes_are_redacted_but_ordinary_numbers_and_words_are_not():
    assert "1234" not in history.redact_history("the pin is 1234")
    assert history.redact_history("postcode LS1 4AP, 12 jobs, code 5 jobs") == "postcode LS1 4AP, 12 jobs, code 5 jobs"


def test_maintenance_purges_after_two_years_and_redacts_the_backlog(tmp_path):
    db = Database(tmp_path / "t.db")
    _old_row(db, "user", "ancient chat", timedelta(days=history.RETENTION_DAYS + 5))
    _old_row(db, "user", "the door code is 5566", timedelta(days=400))
    db.maintain_transcript()
    texts = [r["text"] for r in db.recent_transcript(10)]
    assert len(texts) == 1 and "5566" not in texts[0] and "door code" in texts[0]


# ---------------------------------------------------------------- loading recent history into a new session
def test_new_session_context_has_last_24h_but_not_older_or_scheduled_prompts(settings):
    db = Database(settings.db_path)  # rows from "earlier sessions", written before this Jarvis starts
    _old_row(db, "user", "three days ago chatter", timedelta(hours=72))
    _old_row(db, "user", "Ladder inspection jobs for all engineers tomorrow, code is 7788", timedelta(hours=20))
    _old_row(db, "assistant", "Queued for your approval, sir.", timedelta(hours=20))
    _old_row(db, "user", "[Scheduled self-reflection - nobody typed this]", timedelta(hours=3))
    _old_row(db, "assistant", "Nothing durable.", timedelta(hours=3))
    j = Jarvis(settings, db=db, client=FakeClient([]))
    system = "\n".join(b["text"] for b in j.brain.system)
    assert "Ladder inspection jobs for all engineers tomorrow" in system
    assert "Queued for your approval" in system
    assert "7788" not in system
    assert "three days ago" not in system
    assert "Scheduled self-reflection" not in system and "Nothing durable" not in system


def test_turns_in_the_current_session_are_not_repeated_in_the_system_prompt(settings):
    j = Jarvis(settings, client=FakeClient([]))
    j.db.add_transcript("user", "said during this session")
    j.brain.refresh_system()
    system = "\n".join(b["text"] for b in j.brain.system)
    assert "said during this session" not in system
    j.db.add_transcript("assistant", "reply")
    blocks = build_system(settings, j.kb, j.db, {}, "", history_before_id=None)
    assert "said during this session" in blocks[1]["text"]


# ---------------------------------------------------------------- open requests
async def test_open_requests_are_carried_forward_until_closed(settings):
    j = Jarvis(settings, client=FakeClient([]))
    note, done = TOOLS_BY_NAME["note_open_request"], TOOLS_BY_NAME["close_open_request"]
    assert note.approval is False and done.approval is False
    out = await dispatch(j, note, note.model(request="Raise ladder inspection jobs for all engineers for tomorrow"))
    assert "#1" in out
    system = "\n".join(b["text"] for b in j.brain.system)
    assert "(#1, asked " in system and "Raise ladder inspection jobs for all engineers for tomorrow" in system
    await dispatch(j, done, done.model(request_id=1))
    assert "Raise ladder inspection jobs" not in "\n".join(b["text"] for b in j.brain.system)
    await j.http.aclose()


def test_open_requests_are_redacted_capped_and_survive_a_new_brain(settings):
    j = Jarvis(settings, client=FakeClient([]))
    for i in range(history.MAX_OPEN_REQUESTS + 5):
        history.add_open_request(j.db, f"job {i}")
    items = history.open_requests(j.db)
    assert len(items) == history.MAX_OPEN_REQUESTS and items[-1]["text"].endswith(str(history.MAX_OPEN_REQUESTS + 4))
    history.add_open_request(j.db, "chase Kestrel, portal password is hunter22")
    assert "hunter22" not in history.open_requests(j.db)[-1]["text"]
    assert history.close_open_request(j.db, 9999) is False


# ---------------------------------------------------------------- searching history
async def test_history_search_finds_recent_requests_redacted(settings):
    j = Jarvis(settings, client=FakeClient([]))
    j.db.add_transcript("user", "Raise ladder inspection jobs for all engineers tomorrow")
    j.db.add_transcript("assistant", "Queued as action #4 for your approval.")
    j.db.add_transcript("user", "What's cash looking like?")
    tool = TOOLS_BY_NAME["search_conversation_history"]
    assert tool.approval is False
    hits = await dispatch(j, tool, tool.model(query="ladder"))
    assert [h["text"] for h in hits][0].startswith("Raise ladder inspection")
    assert all("cash" not in h["text"] for h in hits)
    latest = await dispatch(j, tool, tool.model(query=""))
    assert len(latest) == 3
    await j.http.aclose()


async def test_user_turns_reach_the_transcript_without_stutter(settings):
    j = Jarvis(settings, client=FakeClient([message([text_block("Noted.")])]))
    await j.brain.ask("jobs jobs for jobs for tomorrow", "voice")
    assert j.db.recent_transcript(5)[0]["text"] == "jobs for tomorrow"
    await j.http.aclose()


def test_system_prompt_tells_jarvis_to_search_history_and_track_requests():
    src = (Path(__file__).resolve().parent.parent / "jarvis" / "brain" / "prompts.py").read_text(encoding="utf-8")
    assert "search_conversation_history" in src and "note_open_request" in src and "close_open_request" in src


# ---------------------------------------------------------------- browser speech recognition
def test_hud_replaces_cumulative_finals_instead_of_stacking_them():
    assert "addFinal(text) {" in HUD
    assert "this.addFinal(heard); this.finalHeard();" in HUD
    assert "this.addFinal(m.text);" in HUD
    assert "this.finals += heard" not in HUD and "this.finals += m.text" not in HUD
