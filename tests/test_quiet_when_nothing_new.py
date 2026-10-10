"""If there's nothing new, Jarvis says nothing (the owner's "stop Jarvis sending 'nothing new' all the time").

One test (or a few) per source: a run that found nothing posts nothing into the chat, raises no alert, sends nothing to Teams
or push and says nothing aloud - it is only a row in the activity log ("What Jarvis did"). Real news and failures still go out.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jarvis.core import Jarvis
from jarvis.main import create_app
from jarvis.services import daily_rhythm
from jarvis.services.nothing_new import NOTHING, is_nothing_new, quiet_note, visible_conversation
from jarvis.services.scheduler import build_scheduler
from jarvis.services.security_watch import SecurityWatch, SubmitFindings
from jarvis.settings_store import SECTIONS_BY_ID
from tests.fakes import FakeClient, message, text_block
from tests.test_security_watch import FakeGitHub, FakeIssues, finding

CHAT = {"user_message", "thinking", "delta", "tool", "reply", "error", "proactive", "notification"}


def make(settings, script=None, *, proactive=True, default_text="Certainly, sir."):
    settings.proactive_chat_enabled = proactive
    settings.proactive_quiet_start = settings.proactive_quiet_end = "00:00"  # no quiet hours
    return Jarvis(settings, client=FakeClient(script, default_text=default_text))


def drain(q) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def said(events) -> list[str]:
    """Everything that would reach the owner: chat events, proactive posts, alerts (toasts / voice)."""
    return [e["type"] for e in events if e["type"] in CHAT]


@pytest.fixture
def outbox(monkeypatch):
    """Everything sent to the owner on Teams / email / push, instead of sending it."""
    def install(j):
        sent = []

        async def fake(subject, body, channels=("teams",), **kw):
            sent.append((subject, body, tuple(channels)))
            return "Teams"

        async def fake_eng(subject, body, issue_id=None):
            sent.append((subject, body, ("engineering",)))
            return "Teams"

        monkeypatch.setattr(j.notifier, "send_owner_update", fake)
        monkeypatch.setattr(j.notifier, "send_engineering_update", fake_eng)
        return sent
    return install


def runs(j, key):
    return [r for r in j.db.check_runs_since("2000-01-01T00:00:00+00:00") if r["job_key"] == key]


# --------------------------------------------------------------------------- the detector
@pytest.mark.parametrize("text", [
    "", NOTHING, f"{NOTHING}: checked 14 jobs", f"**{NOTHING}** - all fine", f"  > {NOTHING.lower()}",
    "Nothing new.", "Nothing new since 09:30.", "No changes.", "No changes since last time.", "Nothing to report.",
    "All quiet.", "All quiet - I'll check again later.", "No new emails.", "No new jobs have been booked for tomorrow.",
    "Everything is as before.", "Everything looks fine.", "Same as last time.", "Nothing new to report, all good.",
    "**Nothing new.** Checked the inbox and the FSM.", "There is nothing new.", "Still nothing.", "It's quiet today.",
    "Nothing worth keeping today.",
])
def test_a_reply_that_amounts_to_nothing_new_is_recognised(text):
    assert is_nothing_new(text)


@pytest.mark.parametrize("text", [
    "No new faults, but the panel at Kestrel Retail is offline.",
    "No new faults, the panel at Kestrel Retail is offline.",
    "Nothing new except the alarm at Bradford Road is still sounding.",
    "No new emails. Job 123 is now overdue.",
    "No new jobs and the van is broken.",
    "All quiet. One quote was accepted by Kestrel Retail.",
    "Nothing heard from Dave since 8am - he is lone working.",
    "None of the engineers have checked in.",
    "Clear signs of a leak at the Bradford Road site.",
    "Checked the inbox and the van is broken.",
    "Nothing new. Checked the inbox and a PO came in from Kestrel.",
    "The council portal has no new requests; one request is still open.",
    "PR #3 now has merge conflicts.",
    "No new findings. " + "The CCTV recorder at the depot has been offline since yesterday and needs a visit. " * 4,
])
def test_a_reply_with_real_news_in_it_is_never_treated_as_nothing_new(text):
    assert not is_nothing_new(text)


def test_the_note_kept_for_the_activity_log():
    assert quiet_note(f"{NOTHING}: checked 14 jobs") == "checked 14 jobs"
    assert quiet_note(NOTHING, "Nothing to report.") == "Nothing to report."
    assert quiet_note("All quiet.") == "All quiet."


# --------------------------------------------------------------------------- 1. scheduled checks (scheduler._check)
async def test_a_check_that_finds_nothing_says_nothing_anywhere_and_is_only_logged(settings, outbox):
    j = make(settings)
    sent = outbox(j)
    q = j.bus.subscribe()

    async def nothing(minutes):
        return []

    j.tracker.lone_worker_check = nothing
    await build_scheduler(j).get_job("lone_worker").func()
    assert said(drain(q)) == [] and sent == [] and j.db.recent_notifications() == []
    [run] = runs(j, "lone_worker")
    assert run["outcome"] == "no_change"
    await j.http.aclose()


async def test_a_check_that_finds_something_or_fails_still_tells_the_owner(settings, outbox):
    j = make(settings)
    sent = outbox(j)

    async def overrun(minutes):
        return [{"job": "J1", "engineer": "Dave", "site": "Kestrel Retail", "booked_end": "15:00", "overrun_minutes": 45}]

    j.tracker.lone_worker_check = overrun
    await build_scheduler(j).get_job("lone_worker").func()
    assert any("Safety check" in n["title"] for n in j.db.recent_notifications())  # a real finding still alerts
    assert runs(j, "lone_worker")[-1]["outcome"] == "changed"

    async def boom(minutes):
        raise RuntimeError("RAM is down")

    j.tracker.lone_worker_check = boom
    await build_scheduler(j).get_job("lone_worker").func()
    assert runs(j, "lone_worker")[-1]["outcome"] == "failed"
    assert j.faults.open_count() >= 1  # a failure is still a fault report (services/faults.py), unchanged
    await j.http.aclose()


def test_the_chat_row_leaves_out_checks_that_found_nothing_unless_the_owner_asks(settings):
    j = make(settings)
    j.activity.record("pr_watch", "Pull request watch", "no_change", "No change.")
    j.activity.record("pr_watch", "Pull request watch", "baseline", "First look.")
    with TestClient(create_app(settings, j)) as c:
        assert c.get("/api/status").json()["activity"]["jobs"] == []  # no "Scheduled checks ... nothing to report" line
        settings.tell_when_nothing_new = True
        [job] = c.get("/api/status").json()["activity"]["jobs"]
        assert job["name"] == "Pull request watch" and job["no_change"] == 2
    assert j.activity.summary()["jobs"]  # still all in the log, for anyone who goes looking


# --------------------------------------------------------------------------- 2. automations
@pytest.mark.parametrize("reply", ["No changes since last time.", "**Nothing new.**", "All quiet - I'll check again later.",
                                   f"\n\n> {NOTHING}", "Everything is as before."])
async def test_an_automation_reply_that_amounts_to_nothing_new_is_never_posted(settings, outbox, reply):
    for proactive in (True, False):
        j = make(settings, [message([text_block(reply)])], proactive=proactive)
        sent = outbox(j)
        q = j.bus.subscribe()
        created = j.automations.create("Overdue jobs check", "*/10 * * * *", "Tell me about overdue jobs.")
        await j.automations.run(created["id"])
        assert said(drain(q)) == [] and sent == [] and j.db.recent_notifications() == []
        [run] = runs(j, f"automation_{created['id']}")
        assert run["outcome"] == "no_change"
        assert j.db.get_automation(created["id"])["nochange_streak"] == 1  # counts towards slowing down
        await j.http.aclose()


async def test_an_automation_with_real_news_still_posts_it(settings, outbox):
    j = make(settings, [message([text_block("No new faults, but the panel at Kestrel Retail is offline.")])])
    sent = outbox(j)
    q = j.bus.subscribe()
    created = j.automations.create("Fault watch", "*/10 * * * *", "Tell me about new faults.")
    await j.automations.run(created["id"])
    posted = [e for e in drain(q) if e["type"] == "proactive"]
    assert len(posted) == 1 and "Kestrel Retail is offline" in posted[0]["data"]["text"]
    assert sent and "Kestrel Retail is offline" in sent[0][1]  # and on Teams, as before
    assert runs(j, f"automation_{created['id']}")[-1]["outcome"] == "changed"
    await j.http.aclose()


async def test_the_automation_prompt_asks_for_the_marker_and_nothing_else(settings):
    j = make(settings, [message([text_block(NOTHING)])])
    created = j.automations.create("Check", "0 8 * * *", "Check something.")
    await j.automations.run(created["id"])
    prompt = j.brain.messages[0]["content"][-1]["text"]
    assert f"reply with {NOTHING} and nothing else" in prompt
    await j.http.aclose()


def test_a_background_turn_never_shows_up_when_the_console_opens(settings):
    j = make(settings)
    j.db.add_transcript("user", "What's on today?")
    j.db.add_transcript("assistant", "Three jobs, sir.")
    j.db.add_transcript("user", '[Scheduled check you set up: "Overdue jobs"]\nTell me about overdue jobs.')
    j.db.add_transcript("assistant", NOTHING)
    j.db.add_transcript("user", "[Scheduled self-reflection - nobody typed this]\n<transcript>...</transcript>")
    j.db.add_transcript("assistant", "Nothing durable stood out.")
    j.db.add_transcript("assistant", "**Fault watch**\n\nThe panel at Kestrel Retail is offline.")  # a finding: its own row
    with TestClient(create_app(settings, j)) as c:
        texts = [r["text"] for r in c.get("/api/transcript").json()]
    assert texts == ["What's on today?", "Three jobs, sir.", "**Fault watch**\n\nThe panel at Kestrel Retail is offline."]
    rows = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}]
    assert visible_conversation(rows) == rows


# --------------------------------------------------------------------------- 3. security review
def _watch(j, review, monkeypatch):
    sw = SecurityWatch(j.settings, j.db, j.bus, j.notifier, j.client, FakeGitHub({"app.py": "x = 1\n"}), FakeIssues(j.db))

    async def fake_review(ws):
        return review

    monkeypatch.setattr(sw, "_review", fake_review)
    j.security_watch = sw
    return sw


async def test_a_clean_scheduled_security_review_notifies_nothing_and_is_logged(settings, outbox, monkeypatch):
    j = make(settings)
    sent = outbox(j)
    _watch(j, SubmitFindings(findings=[], summary="Clean pass."), monkeypatch)
    q = j.bus.subscribe()
    await build_scheduler(j).get_job("security_watch").func()
    assert said(drain(q)) == [] and sent == [] and j.db.recent_notifications() == []
    assert j.db.pending_digest_items() == []  # not even a digest line
    assert runs(j, "security_watch")[-1]["outcome"] == "no_change"
    await j.http.aclose()


async def test_security_findings_and_failures_still_alert(settings, outbox, monkeypatch):
    j = make(settings)
    outbox(j)
    _watch(j, SubmitFindings(findings=[finding(severity="high")], summary="One issue."), monkeypatch)
    await build_scheduler(j).get_job("security_watch").func()
    assert any("new finding" in n["title"] for n in j.db.recent_notifications())
    assert runs(j, "security_watch")[-1]["outcome"] == "changed"

    sw = j.security_watch

    async def broken(ws):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(sw, "_review", broken)
    await sw.run()
    assert any(n["title"] == "Security review failed" for n in j.db.recent_notifications())
    await j.http.aclose()


async def test_a_clean_review_someone_asked_for_gets_one_quiet_line(settings, outbox, monkeypatch):
    j = make(settings)
    sent = outbox(j)
    sw = _watch(j, SubmitFindings(findings=[], summary="Clean pass."), monkeypatch)
    await sw.run(scheduled=False)  # what start() runs: someone asked, so they get an answer
    [note] = j.db.recent_notifications()
    assert note["title"] == "Security review: no new findings" and sent == []  # Alerts only, no push / Teams
    await j.http.aclose()


# --------------------------------------------------------------------------- 5. the weekly digest
async def test_a_digest_of_only_nothing_new_items_is_not_sent(settings, outbox):
    j = make(settings)
    sent = outbox(j)
    j.db.add_digest_item("security_review_clean", "Security review: nothing new", "Clean pass.")
    j.db.add_digest_item("self_learning_summary", "Self-reflection on recent conversations", "Remembered one thing.")
    j.db.add_digest_item("routine_test_recovered", "Recovered: HTTP home", "200")
    j.db.add_digest_item("automation_slowed", '"Overdue jobs" found nothing new for 13+ hours', "keep it or delete",
                         ref="automation:1")
    result = await j.weekly_digest.run("scheduled")
    assert result["sent"] is False and sent == [] and j.db.list_digests() == []
    await j.http.aclose()


async def test_a_digest_with_real_news_drops_clean_items_and_lists_the_minor_ones(settings, outbox):
    j = make(settings)
    sent = outbox(j)
    j.db.add_digest_item("security_review_clean", "Security review: nothing new", "Clean pass.")
    j.db.add_digest_item("automation_slowed", '"Overdue jobs" found nothing new for 13+ hours', "x", ref="automation:1")
    j.db.add_digest_item("automation_slowed", '"Overdue jobs" found nothing new for 40+ hours', "x", ref="automation:1")
    j.db.add_digest_item("deploy_succeeded", "Issue #3 fixed and live", ref="fix:7")
    result = await j.weekly_digest.run("scheduled")
    text = result["text"]
    assert j.db.list_digests() and sent == []  # stored and shown (Teams is not set up in tests)
    assert "Issue #3 fixed and live" in text and "nothing new" not in text.split("Automations slowed")[0].lower()
    assert "Security review" not in text  # a clean review is not news
    assert text.count("Overdue jobs") == 1 and "40+ hours" in text  # mentioned once, the latest note
    assert j.db.pending_digest_items() == []
    await j.http.aclose()


# --------------------------------------------------------------------------- self-reflection
async def test_the_reflection_is_never_typed_into_the_chat_and_nothing_new_is_not_kept(settings):
    j = make(settings, [message([text_block("Nothing durable stood out.")])])
    j.db.add_transcript("user", "What's the weather like")
    j.db.add_transcript("assistant", "No idea, sir.")
    q = j.bus.subscribe()
    await j.self_learning.reflect()
    assert said(drain(q)) == []  # neither the (huge) prompt nor "nothing stood out" appears in the open chat
    assert j.db.pending_digest_items() == []
    await j.http.aclose()

    j = make(settings, [message([text_block("Remembered that Alex wants figures ex-VAT.")])])
    j.db.add_transcript("user", "Always show me figures ex-VAT")
    await j.self_learning.reflect()
    assert [i["kind"] for i in j.db.pending_digest_items()] == ["self_learning_summary"]  # real news: kept for the digest
    await j.http.aclose()


# --------------------------------------------------------------------------- weekly watches, FSM engineer
async def test_a_weekly_watch_with_nothing_new_posts_nothing(settings, outbox):
    settings.partner_email = "partner@example.com"
    j = make(settings, [message([text_block(NOTHING)]), message([text_block(NOTHING)])])
    sent = outbox(j)
    j.db.set_kv("regwatch_last", "Last week's update.")
    q = j.bus.subscribe()
    sched = build_scheduler(j)
    await sched.get_job("regwatch").func()
    await sched.get_job("technical_watch").func()
    assert said(drain(q)) == [] and sent == [] and j.db.recent_notifications() == [] and j.db.pending_actions() == []
    assert j.db.get_kv("regwatch_last") == "Last week's update."  # what's known is kept for next week
    assert runs(j, "regwatch")[-1]["outcome"] == "no_change" and runs(j, "technical_watch")[-1]["outcome"] == "no_change"
    asked = [c for c in j.client.beta.messages.calls if "legal and tax changes" in str(c.get("system"))]
    assert "never write 'no change'" in asked[0]["system"]
    await j.http.aclose()


async def test_a_weekly_watch_with_news_still_posts_it(settings, outbox):
    news = "The VAT registration threshold rises to £95,000 from 1 April 2027 - check our quarterly returns."
    j = make(settings, [message([text_block(news)])])
    outbox(j)
    await build_scheduler(j).get_job("regwatch").func()
    assert any(n["title"] == "Weekly tax & employment law watch" for n in j.db.recent_notifications())
    assert runs(j, "regwatch")[-1]["outcome"] == "changed"
    await j.http.aclose()


async def test_the_fsm_engineer_bot_with_nothing_new_is_logged_not_sent(settings, outbox, monkeypatch):
    settings.fsm_engineer_enabled = True
    j = make(settings)
    sent = outbox(j)

    async def run(*, scheduled=True):
        return NOTHING

    monkeypatch.setattr(j.fsm_engineer, "run", run)
    await build_scheduler(j).get_job("fsm_engineer").func()
    assert sent == [] and runs(j, "fsm_engineer")[-1]["outcome"] == "no_change"
    await j.http.aclose()


# --------------------------------------------------------------------------- briefing / wrap-up, the setting
async def test_the_briefing_and_wrap_up_leave_empty_areas_out(settings):
    j = make(settings, default_text="Good morning. Two jobs are running late.")
    await j.briefings.morning_briefing(deliver=False)
    await j.wrapup.run(deliver=False)
    systems = [str(c.get("system")) for c in j.client.beta.messages.calls]
    for marker in ("briefing that will be read aloud", "end-of-day wrap-up"):
        system = next(s for s in systems if marker in s)
        assert daily_rhythm.QUIET_PARTS_RULE in system and daily_rhythm.WORD_BUDGET_RULE in system
    assert "quiet day" in daily_rhythm.QUIET_PARTS_RULE  # one short line when the whole day is empty
    await j.http.aclose()


def test_the_owner_setting_is_off_by_default_and_lives_with_jarvis_speaking_up(settings):
    assert settings.tell_when_nothing_new is False
    field = next(f for f in SECTIONS_BY_ID["proactive"].fields if f.key == "tell_when_nothing_new")
    assert field.kind == "bool" and field.label == "Tell me when checks find nothing"

