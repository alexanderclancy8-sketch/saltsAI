"""Console redesign Phase 4b, item 2: the daily rhythm (morning briefing and end-of-day wrap-up).

Registered at the right default times in UK time, each switchable off, editable in Settings > Schedules; each text
fits in a minute of speech (word budget enforced in code, not just asked for); each is posted to the console AND to
Teams, the same text; and the console post obeys the quiet hours and the per-session "Speaks up" mute."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jarvis.core import Jarvis
from jarvis.main import create_app
from jarvis.services import daily_rhythm
from jarvis.services.daily_rhythm import MAX_SPOKEN_SECONDS, MAX_WORDS, fit_to_minute, spoken_seconds, word_count
from jarvis.services.scheduler import build_scheduler
from jarvis.settings_store import FIELDS, SECTIONS_BY_ID, SettingsStore
from tests.fakes import FakeClient, message, text_block

UK = ZoneInfo("Europe/London")


def words(n: int, sentence: int = 10) -> str:
    """n words as sentences of `sentence` words each."""
    out = []
    for i in range(n):
        out.append("word" if (i + 1) % sentence else "end.")
    return " ".join(out)


def make(settings, script=None, default_text="Good morning. Two jobs are running late and nothing is waiting on you."):
    settings.proactive_quiet_start = settings.proactive_quiet_end = "00:00"  # no quiet hours unless a test sets them
    return Jarvis(settings, client=FakeClient(script, default_text=default_text))


def drain(q) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


@pytest.fixture
def teams(monkeypatch):
    """Record what goes to Teams (the owner update path), instead of sending it."""
    def install(j):
        sent = []

        async def fake(subject, body, channels=("teams",), **kw):
            sent.append({"subject": subject, "body": body, "channels": tuple(channels)})
            return "Teams"

        monkeypatch.setattr(j.notifier, "send_owner_update", fake)
        return sent
    return install


# --------------------------------------------------------------------------- schedules: defaults, UK time, off switches
def fire_times(job, start: datetime, count: int = 4) -> list[datetime]:
    times, prev = [], None
    now = start
    for _ in range(count):
        nxt = job.trigger.get_next_fire_time(prev, now)
        times.append(nxt)
        prev, now = nxt, nxt
    return times


def test_the_defaults_are_nine_and_half_past_five_on_weekdays_and_both_are_on(settings):
    assert settings.briefing_enabled is True and settings.wrapup_enabled is True
    assert settings.briefing_cron == "0 9 * * 1-5" and settings.wrapup_cron == "30 17 * * 1-5"
    assert settings.timezone == "Europe/London"


def test_both_jobs_are_registered_and_fire_at_the_right_uk_times_on_weekdays_only(settings):
    j = make(settings)
    sched = build_scheduler(j)
    briefing, wrapup = sched.get_job("briefing"), sched.get_job("wrapup")
    assert briefing is not None and wrapup is not None
    # Friday 2 October 2026, 08:00 UK -> Fri 09:00, then Mon 5, Tue 6, Wed 7 (never the weekend)
    start = datetime(2026, 10, 2, 8, 0, tzinfo=UK)
    assert [(t.strftime("%a %H:%M"), t.tzinfo.key if hasattr(t.tzinfo, "key") else str(t.tzinfo))
            for t in fire_times(briefing, start)][0][0] == "Fri 09:00"
    assert [t.strftime("%a %d %H:%M") for t in fire_times(briefing, start)] == ["Fri 02 09:00", "Mon 05 09:00",
                                                                                 "Tue 06 09:00", "Wed 07 09:00"]
    assert [t.strftime("%a %d %H:%M") for t in fire_times(wrapup, start)] == ["Fri 02 17:30", "Mon 05 17:30",
                                                                               "Tue 06 17:30", "Wed 07 17:30"]
    # the schedule's own time zone is the business's (UK time), whatever the server's clock says
    assert str(sched.timezone) == "Europe/London"
    assert all(t.utcoffset().total_seconds() == 3600 for t in fire_times(briefing, start))  # BST in October: 09:00 UK = 08:00 UTC


def test_the_hour_follows_uk_clock_changes(settings):
    j = make(settings)
    sched = build_scheduler(j)
    winter = datetime(2026, 11, 2, 8, 0, tzinfo=UK)  # GMT again
    t = sched.get_job("briefing").trigger.get_next_fire_time(None, winter)
    assert t.strftime("%H:%M") == "09:00" and t.utcoffset().total_seconds() == 0


def test_each_is_off_switchable(settings):
    settings.briefing_enabled = False
    sched = build_scheduler(make(settings))
    assert sched.get_job("briefing") is None and sched.get_job("wrapup") is not None
    settings.briefing_enabled, settings.wrapup_enabled = True, False
    sched = build_scheduler(make(settings))
    assert sched.get_job("briefing") is not None and sched.get_job("wrapup") is None


def test_times_and_switches_are_editable_in_settings_schedules(settings):
    schedules = [f.key for f in SECTIONS_BY_ID["schedules"].fields]
    assert {"briefing_enabled", "briefing_cron", "wrapup_enabled", "wrapup_cron"} <= set(schedules)
    assert FIELDS["briefing_enabled"].kind == "bool" and FIELDS["briefing_cron"].kind == "cron"
    store = SettingsStore(settings)
    assert store.update({"briefing_cron": "30 8 * * 1-5", "wrapup_cron": "0 18 * * 1-5", "wrapup_enabled": False}, []) == {}
    assert (settings.briefing_cron, settings.wrapup_cron, settings.wrapup_enabled) == ("30 8 * * 1-5", "0 18 * * 1-5", False)
    sched = build_scheduler(make(settings))
    t = sched.get_job("briefing").trigger.get_next_fire_time(None, datetime(2026, 10, 5, 7, 0, tzinfo=UK))
    assert t.strftime("%H:%M") == "08:30" and sched.get_job("wrapup") is None
    assert "briefing_cron" in store.update({"briefing_cron": "not a cron"}, [])  # a bad time is refused, nothing saved


def test_the_schedules_settings_view_offers_them(settings):
    store = SettingsStore(settings)
    view = store.view(type("Db", (), {"get_kv": staticmethod(lambda k: None)}), {"base_url": "https://x", "app_name": "x"})
    sec = next(s for s in view["sections"] if s["id"] == "schedules")
    keys = {f["key"]: f for f in sec["fields"]}
    assert keys["briefing_enabled"]["value"] is True and keys["briefing_cron"]["value"] == "0 9 * * 1-5"
    assert keys["wrapup_enabled"]["value"] is True and keys["wrapup_cron"]["value"] == "30 17 * * 1-5"
    assert any("9am on weekdays" in g for g in sec["guide"])


def test_saving_from_the_settings_page_retimes_the_live_scheduler(settings):
    j = make(settings)
    app = create_app(settings, j)
    with TestClient(app) as c:
        r = c.post("/api/settings", json={"values": {"briefing_cron": "15 8 * * 1-5", "briefing_enabled": True}})
        assert r.status_code == 200, r.text
        assert settings.briefing_cron == "15 8 * * 1-5"
        assert c.post("/api/settings", json={"values": {"briefing_cron": "nonsense"}}).status_code == 400


# --------------------------------------------------------------------------- under a minute when spoken
def test_the_word_budget_is_under_a_minute_of_speech():
    assert MAX_WORDS == 150 and daily_rhythm.TARGET_WORDS == "130 to 150"
    assert spoken_seconds(words(MAX_WORDS)) < MAX_SPOKEN_SECONDS == 60.0
    assert spoken_seconds(words(MAX_WORDS + 40)) > MAX_SPOKEN_SECONDS  # a longer one would not be


def test_fit_to_minute_cuts_at_a_whole_sentence_and_leaves_short_text_alone():
    short = "Two jobs ran late. Nothing needs you."
    assert fit_to_minute(short) == short
    long = words(400)
    cut = fit_to_minute(long)
    assert word_count(cut) <= MAX_WORDS and cut.endswith(".") and word_count(cut) >= MAX_WORDS - 10
    assert word_count(fit_to_minute("one " * 300)) <= MAX_WORDS  # no sentence breaks at all: cut at the limit
    assert fit_to_minute("") == ""


def test_both_prompts_state_the_budget_and_drop_the_old_longer_one():
    from jarvis.services.briefing import BRIEFING_SYSTEM
    from jarvis.services.wrapup import WRAPUP_SYSTEM

    for system in (BRIEFING_SYSTEM, WRAPUP_SYSTEM):
        assert "{budget}" in system and "150-250" not in system
    assert "130 to 150 words" in daily_rhythm.WORD_BUDGET_RULE and "never more than 150" in daily_rhythm.WORD_BUDGET_RULE


async def test_the_briefing_asks_for_a_minute_and_a_long_answer_is_cut_to_fit(settings):
    j = make(settings, default_text=words(420))  # the model ignores the budget every time
    text = await j.briefings.morning_briefing(deliver=False)
    assert word_count(text) <= MAX_WORDS and spoken_seconds(text) < MAX_SPOKEN_SECONDS
    sent = [c for c in j.client.beta.messages.calls if "briefing that will be read aloud" in str(c.get("system"))]
    assert "130 to 150 words" in sent[0]["system"] and "never more than 150" in sent[0]["system"]
    assert len(sent) == 2 and "too long to say in a minute" in sent[1]["messages"][0]["content"]  # asked once to shorten
    await j.http.aclose()


async def test_the_wrap_up_is_held_to_the_same_budget(settings):
    j = make(settings, default_text=words(300))
    text = await j.wrapup.run(deliver=False)
    assert word_count(text) <= MAX_WORDS and spoken_seconds(text) < MAX_SPOKEN_SECONDS
    sent = [c for c in j.client.beta.messages.calls if "end-of-day wrap-up" in str(c.get("system"))]
    assert sent and all("130 to 150 words" in c["system"] for c in sent)
    await j.http.aclose()


async def test_a_shorter_second_draft_is_used_as_it_is(settings):
    j = make(settings, script=[message([text_block(words(300))]), message([text_block(words(140))])])
    text = await daily_rhythm.write_short(j.client, settings, system="s", prompt="p")
    assert text == words(140)
    await j.http.aclose()


async def test_a_text_within_budget_is_asked_for_once_and_unchanged(settings):
    j = make(settings, default_text=words(135))
    text = await daily_rhythm.write_short(j.client, settings, system="s", prompt="p")
    assert text == words(135) and len(j.client.beta.messages.calls) == 1
    await j.http.aclose()


# --------------------------------------------------------------------------- posted to Teams and the console
async def test_the_briefing_goes_to_teams_and_the_console_with_the_same_text(settings, teams):
    j = make(settings)
    sent = teams(j)
    q = j.bus.subscribe()
    text = await j.briefings.morning_briefing(deliver=True)
    assert len(sent) == 1 and sent[0]["body"] == text and sent[0]["subject"].startswith("Morning briefing")
    assert "teams" in sent[0]["channels"]
    proactive = [e for e in drain(q) if e["type"] == "proactive"]
    assert len(proactive) == 1 and proactive[0]["data"]["text"] == text and proactive[0]["data"]["source"] == "daily:briefing"
    assert any(r["role"] == "assistant" and r["text"] == text for r in j.db.recent_transcript(5))  # waiting when the console opens
    assert any(n["title"] == "Morning briefing" for n in j.db.recent_notifications())
    await j.http.aclose()


async def test_the_wrap_up_goes_to_teams_and_the_console_with_the_same_text(settings, teams):
    j = make(settings, default_text="That is the day done. One job slipped to tomorrow.")
    sent = teams(j)
    q = j.bus.subscribe()
    text = await j.wrapup.run(deliver=True)
    assert len(sent) == 1 and sent[0]["body"] == text and sent[0]["subject"].startswith("End-of-day wrap-up")
    proactive = [e for e in drain(q) if e["type"] == "proactive"]
    assert len(proactive) == 1 and proactive[0]["data"]["text"] == text and proactive[0]["data"]["source"] == "daily:wrapup"
    await j.http.aclose()


async def test_nothing_is_posted_when_only_asked_for(settings, teams):
    """Asking for the briefing in the chat (deliver=False) returns the text and posts nothing anywhere."""
    j = make(settings)
    sent = teams(j)
    q = j.bus.subscribe()
    await j.briefings.morning_briefing(deliver=False)
    await j.wrapup.run(deliver=False)
    assert sent == [] and not [e for e in drain(q) if e["type"] == "proactive"]
    await j.http.aclose()


async def test_the_console_post_works_with_speaking_up_off_but_is_then_never_read_aloud(settings, teams):
    j = make(settings)
    teams(j)
    assert settings.proactive_chat_enabled is False  # the default
    q = j.bus.subscribe()
    await j.briefings.morning_briefing(deliver=True)
    (event,) = [e for e in drain(q) if e["type"] == "proactive"]
    assert event["data"]["speak"] is False
    settings.proactive_chat_enabled = True
    await j.briefings.morning_briefing(deliver=True)
    (event,) = [e for e in drain(q) if e["type"] == "proactive"]
    assert event["data"]["speak"] is True
    await j.http.aclose()


async def test_quiet_hours_keep_it_off_the_open_console_but_it_still_reaches_teams_and_the_record(settings, teams, monkeypatch):
    j = make(settings)
    sent = teams(j)
    settings.proactive_quiet_start, settings.proactive_quiet_end = "00:00", "23:59"
    monkeypatch.setattr(j.proactive, "quiet_now", lambda: True)
    q = j.bus.subscribe()
    text = await j.briefings.morning_briefing(deliver=True)
    assert not [e for e in drain(q) if e["type"] == "proactive"]  # nothing pushed in quiet hours
    assert len(sent) == 1 and sent[0]["body"] == text
    assert any("quiet hours" in n["title"] for n in j.db.recent_notifications())
    assert any(r["text"] == text for r in j.db.recent_transcript(5))
    await j.http.aclose()


async def test_with_no_console_open_it_waits_in_the_conversation(settings, teams):
    j = make(settings)
    teams(j)
    assert j.bus.subscriber_count == 0
    result = await j.proactive.scheduled("daily:briefing", "Morning briefing", "Good morning.")
    assert result["delivered"] is False and "in the conversation" in result["reason"]
    assert any(r["text"] == "Good morning." for r in j.db.recent_transcript(5))
    await j.http.aclose()


def test_a_muted_session_is_not_sent_the_daily_post_but_an_unmuted_one_is(settings):
    j = make(settings)
    app = create_app(settings, j)
    with TestClient(app) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
            c.portal.call(j.proactive.scheduled, "daily:briefing", "Morning briefing", "Good morning, first one.")
            first = ws.receive_json()
            assert first["type"] == "proactive" and first["data"]["text"] == "Good morning, first one."
            ws.send_json({"type": "proactive_mute", "muted": True})
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
            c.portal.call(j.proactive.scheduled, "daily:wrapup", "Wrap-up", "Muted - you must not see this.")
            c.portal.call(j.bus.publish, "stopped", {"stopped": False})
            assert ws.receive_json()["type"] == "stopped"  # the muted session skipped the post
            ws.send_json({"type": "proactive_mute", "muted": False})
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
            c.portal.call(j.proactive.scheduled, "daily:wrapup", "Wrap-up", "Back again.")
            assert ws.receive_json()["data"]["text"] == "Back again."


# --------------------------------------------------------------------------- failures and safety
async def test_one_leg_failing_never_stops_the_other(settings, monkeypatch):
    j = make(settings)

    async def boom(*a, **k):
        raise RuntimeError("Teams is down")

    monkeypatch.setattr(j.notifier, "send_owner_update", boom)
    out = await daily_rhythm.deliver(j, "briefing", "Morning briefing", "Good morning.")
    assert out["console"].startswith("held back") and out["teams"] == "failed"
    assert any(r["text"] == "Good morning." for r in j.db.recent_transcript(5))
    await j.http.aclose()


async def test_a_failed_scheduled_run_is_not_silent(settings):
    from jarvis.services.scheduler import _daily

    j = make(settings)

    async def broken():
        raise RuntimeError("model unavailable")

    await _daily(j, "briefing", "Morning briefing", broken)()
    assert any(n["title"] == "Morning briefing didn't run" and n["level"] == "warning" for n in j.db.recent_notifications())
    jobs = j.activity.summary()["jobs"]
    assert jobs and jobs[0]["key"] == "briefing" and jobs[0]["failed"] == 1
    await j.http.aclose()


def test_the_daily_rhythm_can_only_tell_it_never_acts():
    src = (Path(__file__).resolve().parent.parent / "jarvis" / "services" / "daily_rhythm.py").read_text(encoding="utf8")
    for forbidden in (".approve(", ".deny(", "actions.queue", "send_mail(", "dispatch("):
        assert forbidden not in src, forbidden
    scheduled = (Path(__file__).resolve().parent.parent / "jarvis" / "services" / "proactive.py").read_text(encoding="utf8")
    body = scheduled.split("async def scheduled", 1)[1].split("async def announce", 1)[0]
    assert "actions" not in body and "approve" not in body
