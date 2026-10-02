"""Jarvis speaking up on his own: the push event, quiet hours, rate limit, change-only findings, background jobs,
the pull request watch, redaction, the session mute, and that none of it can approve or change anything."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jarvis.brain.tools import TOOLS_BY_NAME, WatchActionIn
from jarvis.core import Jarvis
from jarvis.events import EventBus, quiet_turn
from jarvis.integrations.github_pr import PRClient
from jarvis.main import create_app
from jarvis.services import proactive as proactive_mod
from jarvis.services.proactive import MAX_BACKGROUND, NOTHING, in_quiet_hours
from jarvis.services.recruiter import NO_RECURSE
from jarvis.settings_store import FIELDS, SettingsStore
from tests.fakes import FakeClient, message, text_block

WEB = Path(__file__).resolve().parent.parent / "jarvis" / "web"
NIGHT = datetime(2026, 10, 1, 23, 30)
NOON = datetime(2026, 10, 1, 12, 0)


def make(settings, script=None, **over):
    """A Jarvis with proactive chat on and no quiet hours (start == end), unless told otherwise."""
    settings.proactive_chat_enabled = True
    settings.proactive_quiet_start = settings.proactive_quiet_end = "00:00"
    for key, value in over.items():
        setattr(settings, key, value)
    return Jarvis(settings, client=FakeClient(script))


def drain(q) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def types(events) -> list[str]:
    return [e["type"] for e in events]


@pytest.fixture
def instant_sleep(monkeypatch):
    async def fast(_seconds):
        return None

    monkeypatch.setattr("jarvis.services.proactive.asyncio.sleep", fast)


@pytest.fixture
def teams(monkeypatch):
    """Record what would go to Teams instead of sending it."""
    def install(j):
        sent = []

        async def fake(subject, body, channels=("teams",), **kw):
            sent.append((subject, body, tuple(channels)))
            return "Teams"

        monkeypatch.setattr(j.notifier, "send_owner_update", fake)
        return sent
    return install


# --------------------------------------------------------------------------- quiet hours
def test_quiet_hours_run_past_midnight_and_handle_odd_values():
    assert in_quiet_hours(NIGHT, "21:00", "07:30")
    assert in_quiet_hours(datetime(2026, 10, 2, 6, 59), "21:00", "07:30")
    assert not in_quiet_hours(datetime(2026, 10, 2, 7, 30), "21:00", "07:30")  # end is exclusive
    assert not in_quiet_hours(NOON, "21:00", "07:30")
    assert in_quiet_hours(NOON, "09:00", "17:00")  # a same-day window works too
    assert not in_quiet_hours(NOON, "12:00", "12:00")  # identical times: no quiet hours
    assert in_quiet_hours(NIGHT, "nonsense", "25:99")  # unreadable falls back to the 21:00-07:30 defaults


# --------------------------------------------------------------------------- the event bus
def test_quiet_turn_drops_chat_events_but_not_everything_else():
    bus = EventBus()
    q = bus.subscribe()
    token = quiet_turn.set(True)
    try:
        for kind in ("user_message", "thinking", "delta", "tool", "reply", "error"):
            bus.publish(kind, {})
        bus.publish("approvals", [])
    finally:
        quiet_turn.reset(token)
    bus.publish("reply", {"text": "hi"})
    assert types(drain(q)) == ["approvals", "reply"]
    assert "thinking" not in bus.last_event and "reply" in bus.last_event
    assert bus.subscriber_count == 1


# --------------------------------------------------------------------------- one message
async def test_post_does_nothing_while_the_setting_is_off(settings):
    j = Jarvis(settings, client=FakeClient())
    q = j.bus.subscribe()
    result = await j.proactive.post("Hello")
    assert result["delivered"] is False and drain(q) == [] and j.db.recent_notifications() == []
    await j.http.aclose()


async def test_post_pushes_a_redacted_event_and_records_it(settings):
    j = make(settings)
    q = j.bus.subscribe()
    token = "ghp_" + "a" * 30
    result = await j.proactive.post(f"CI is green. The token {token} is in the log. The alarm code is 4821.",
                                    source="CI watch")
    assert result["delivered"] is True
    [event] = drain(q)
    assert event["type"] == "proactive" and event["data"]["source"] == "CI watch" and event["data"]["speak"] is True
    text = event["data"]["text"]
    assert token not in text and "4821" not in text and "[REDACTED]" in text and "CI is green" in text
    assert token not in j.db.recent_transcript(5)[-1]["text"]  # the stored conversation is redacted too
    await j.http.aclose()


async def test_post_is_held_in_quiet_hours_and_kept_as_a_quiet_notification(settings):
    j = make(settings, proactive_quiet_start="21:00", proactive_quiet_end="07:30")
    j.proactive._local_now = lambda: NIGHT
    q = j.bus.subscribe()
    result = await j.proactive.post("The build finished.", source="Build")
    assert result == {"delivered": False, "reason": "quiet hours"}
    assert drain(q) == []  # nothing pushed, no toast either
    [note] = j.db.recent_notifications()
    assert "quiet hours" in note["title"] and note["body"] == "The build finished."
    j.proactive._local_now = lambda: NOON
    assert (await j.proactive.post("The build finished."))["delivered"] is True
    await j.http.aclose()


async def test_post_respects_the_hourly_limit(settings):
    j = make(settings, proactive_max_per_hour=2)
    q = j.bus.subscribe()
    results = [await j.proactive.post(f"Update {n}") for n in range(3)]
    assert [r["delivered"] for r in results] == [True, True, False]
    assert results[2]["reason"] == "hourly limit reached"
    assert types(drain(q)) == ["proactive", "proactive"]
    # an hour later the window has moved on
    j.proactive._sent.clear()
    assert (await j.proactive.post("Update 4"))["delivered"] is True
    await j.http.aclose()


async def test_a_limit_of_zero_means_no_limit(settings):
    j = make(settings, proactive_max_per_hour=0)
    j.bus.subscribe()
    assert all([(await j.proactive.post(f"Update {n}"))["delivered"] for n in range(10)])
    await j.http.aclose()


async def test_post_waits_for_the_owner_to_finish_and_never_talks_over_him(settings, instant_sleep):
    j = make(settings)
    q = j.bus.subscribe()
    j.bus.publish("user_message", {"text": "hang on", "mode": "typed"})
    result = await j.proactive.post("Something came up.")
    assert result["delivered"] is False and "middle of a conversation" in result["reason"]
    assert types(drain(q)) == ["user_message"]
    await j.http.aclose()


async def test_a_turn_still_in_flight_counts_as_busy_until_it_replies(settings):
    j = make(settings)
    p = j.proactive
    assert not p.user_busy()
    j.bus.last_event["thinking"] = time.monotonic() - 100  # started a while ago, long enough not to count as "just spoke"
    assert p.user_busy()
    j.bus.publish("reply", {"text": "done"})
    assert not p.user_busy()
    j.bus.last_event["thinking"] = time.monotonic() - 10_000  # an abandoned turn doesn't block forever
    j.bus.last_event.pop("reply")
    assert not p.user_busy()
    await j.http.aclose()


async def test_with_no_chat_open_the_message_is_kept_not_lost(settings):
    j = make(settings)
    result = await j.proactive.post("Nobody is looking.")
    assert result["delivered"] is False and result["reason"] == "no chat is open"
    assert j.db.recent_notifications()[0]["body"] == "Nobody is looking."
    await j.http.aclose()


# --------------------------------------------------------------------------- findings that repeat
async def test_announce_only_when_something_changed_and_also_goes_to_teams(settings, teams):
    j = make(settings)
    sent = teams(j)
    q = j.bus.subscribe()
    first = await j.proactive.announce("check:1", "Overdue jobs", "3 jobs are overdue.")
    again = await j.proactive.announce("check:1", "Overdue jobs", "3 jobs   are OVERDUE.")  # same, other spacing/case
    changed = await j.proactive.announce("check:1", "Overdue jobs", "4 jobs are overdue.")
    assert (first["delivered"], again["reason"], changed["delivered"]) == (True, "unchanged", True)
    events = drain(q)
    assert types(events) == ["proactive", "proactive"]
    assert events[0]["data"]["text"].startswith("**Overdue jobs**") and "3 jobs are overdue." in events[0]["data"]["text"]
    assert [s[1] for s in sent] == ["3 jobs are overdue.", "4 jobs are overdue."]
    assert all(s[2] == ("teams",) for s in sent)
    await j.http.aclose()


async def test_announce_is_held_in_quiet_hours_and_tried_again_later(settings, teams):
    j = make(settings, proactive_quiet_start="21:00", proactive_quiet_end="07:30")
    sent = teams(j)
    q = j.bus.subscribe()
    j.proactive._local_now = lambda: NIGHT
    held = await j.proactive.announce("check:1", "Overdue jobs", "3 jobs are overdue.")
    assert held == {"delivered": False, "reason": "quiet hours"} and sent == [] and drain(q) == []
    j.proactive._local_now = lambda: NOON  # not remembered as seen, so the next run delivers it
    assert (await j.proactive.announce("check:1", "Overdue jobs", "3 jobs are overdue."))["delivered"] is True
    assert len(sent) == 1 and types(drain(q)) == ["proactive"]
    await j.http.aclose()


async def test_announce_says_nothing_for_nothing_to_report_and_the_finding_returning_is_news(settings, teams):
    j = make(settings)
    sent = teams(j)
    j.bus.subscribe()
    assert (await j.proactive.announce("c", "Check", "3 jobs are overdue."))["delivered"] is True
    quiet = await j.proactive.announce("c", "Check", f"{NOTHING}: all clear")
    assert quiet == {"delivered": False, "reason": "nothing to report"}
    assert (await j.proactive.announce("c", "Check", "3 jobs are overdue."))["delivered"] is True
    assert len(sent) == 2
    await j.http.aclose()


async def test_announce_redacts_and_does_nothing_when_off(settings, teams):
    j = make(settings)
    sent = teams(j)
    j.bus.subscribe()
    await j.proactive.announce("c", "Check", "password = 'hunter2hunter2' was found")
    assert "hunter2hunter2" not in sent[0][1]
    settings.proactive_chat_enabled = False
    assert (await j.proactive.announce("c2", "Check", "Something new"))["delivered"] is False
    assert len(sent) == 1
    await j.http.aclose()


# --------------------------------------------------------------------------- automations
async def test_an_automation_runs_silently_and_posts_only_what_it_found(settings, teams):
    j = make(settings, [message([text_block("3 jobs are overdue.")]), message([text_block("3 jobs are overdue.")])])
    sent = teams(j)
    q = j.bus.subscribe()
    created = j.automations.create("Overdue jobs check", "0 8 * * 1-5", "Check for overdue jobs.")
    reply = await j.automations.run(created["id"])
    assert reply == "3 jobs are overdue."
    events = drain(q)
    assert types(events) == ["proactive"]  # not the whole headless turn typed into the chat
    assert "Overdue jobs check" in events[0]["data"]["text"] and "3 jobs are overdue." in events[0]["data"]["text"]
    assert len(sent) == 1 and sent[0][2] == ("teams",)
    sent_prompt = j.brain.messages[0]["content"][-1]["text"]
    assert NOTHING in sent_prompt
    # same finding next time: nothing new to say
    await j.automations.run(created["id"])
    assert drain(q) == [] and len(sent) == 1
    assert j.db.get_automation(created["id"])["last_result"] == "3 jobs are overdue."
    await j.http.aclose()


async def test_an_automation_behaves_as_before_when_proactive_is_off(settings):
    j = Jarvis(settings, client=FakeClient([message([text_block("All clear.")])]))
    q = j.bus.subscribe()
    created = j.automations.create("Check", "0 8 * * 1-5", "Check something.")
    await j.automations.run(created["id"])
    assert "reply" in types(drain(q))  # the turn shows in the chat exactly as it always did
    assert NOTHING not in j.brain.messages[0]["content"][-1]["text"]
    await j.http.aclose()


async def test_an_automation_failing_still_resets_the_silent_flag(settings):
    j = make(settings)
    created = j.automations.create("Broken", "0 8 * * *", "do something")

    async def boom(*a, **k):
        raise RuntimeError("down")

    j.brain.ask = boom
    await j.automations._run_guarded(created["id"])  # noqa: SLF001
    assert quiet_turn.get() is False
    assert "Failed" in j.db.get_automation(created["id"])["last_result"]
    await j.http.aclose()


# --------------------------------------------------------------------------- background jobs
async def test_start_refuses_while_the_setting_is_off(settings):
    j = Jarvis(settings, client=FakeClient())

    async def work():
        return "x"

    assert "switched off" in j.proactive.start("Job", work)["error"]
    await j.http.aclose()


async def test_a_background_job_posts_a_follow_up_when_it_finishes(settings):
    j = make(settings)
    q = j.bus.subscribe()

    async def work():
        await asyncio.sleep(0)
        return "all 12 files rewritten."

    started = j.proactive.start("Rewriting the PR", work)
    assert started["started"] is True and "background" in started["message"]
    assert drain(q) == []  # the reply that started it came first; nothing yet
    await asyncio.gather(*j.proactive._tasks.values())
    [event] = drain(q)
    assert event["data"]["text"] == "Rewriting the PR: all 12 files rewritten."
    await j.http.aclose()


async def test_a_background_job_that_fails_says_so(settings):
    j = make(settings)
    q = j.bus.subscribe()

    async def work():
        raise RuntimeError("GitHub said no")

    j.proactive.start("Retrying the push", work)
    await asyncio.gather(*j.proactive._tasks.values())
    [event] = drain(q)
    assert "didn't finish" in event["data"]["text"] and "GitHub said no" in event["data"]["text"]
    await j.http.aclose()


async def test_only_a_few_background_jobs_at_once(settings):
    j = make(settings)
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return None

    for n in range(MAX_BACKGROUND):
        assert j.proactive.start(f"Job {n}", work)["started"] is True
    assert "already keeping an eye" in j.proactive.start("One more", work)["error"]
    assert len(j.proactive.running()) == MAX_BACKGROUND
    await j.proactive.stop()  # shutting down cancels them rather than leaving them running
    assert j.proactive.running() == []
    await j.http.aclose()


async def test_a_poller_posts_changes_and_the_final_result(settings, instant_sleep):
    j = make(settings)
    q = j.bus.subscribe()
    script = iter([(False, "CI is running"), (False, "CI is running"), (False, "CI is running; one failing"),
                   (True, "CI passed.")])

    async def check():
        return next(script)

    result = await j.proactive.poller("CI", check, interval_s=0)()
    assert result == "CI passed."
    assert [e["data"]["text"] for e in drain(q)] == ["CI: CI is running; one failing"]  # only the change, not the baseline
    await j.http.aclose()


async def test_a_poller_gives_up_after_repeated_errors_and_after_its_timeout(settings, instant_sleep):
    j = make(settings)

    async def broken():
        raise ConnectionError("no route")

    assert "stopped watching" in await j.proactive.poller("CI", broken, interval_s=0, max_errors=3)()

    async def never():
        return False, "still going"

    out = await j.proactive.poller("CI", never, interval_s=1, timeout_s=3)()
    assert "still not finished" in out and "still going" in out
    await j.http.aclose()


async def test_watch_action_reports_the_outcome_but_never_decides_it(settings, instant_sleep):
    j = make(settings)
    q = j.bus.subscribe()
    action_id = j.actions.queue("email_send", "Email the customer", {"to": ["a@b.co"], "subject": "s", "body": "b"})
    drain(q)
    assert "no action #999" in j.proactive.watch_action(999)["error"].lower()

    # still pending when the watch times out: the watcher has not approved (or denied) anything
    j.proactive.watch_action(action_id, interval_s=1, timeout_s=2)
    await asyncio.gather(*j.proactive._tasks.values())
    assert j.db.get_action(action_id)["status"] == "pending"
    assert "waiting for approval" in drain(q)[-1]["data"]["text"]

    # decided by the owner (here, directly in the store) -> the watcher reports it
    j.proactive.watch_action(action_id, interval_s=0)
    j.db.set_action_status(action_id, "done", "Email sent to a@b.co")
    await asyncio.gather(*j.proactive._tasks.values())
    assert "approved and done" in drain(q)[-1]["data"]["text"]
    assert j.db.pending_actions() == []
    await j.http.aclose()


async def test_watch_ci_needs_the_repository_and_a_sane_branch(settings):
    j = make(settings)
    assert "isn't connected" in j.proactive.watch_ci("main")["error"]
    j.self_github = SimpleNamespace(repo="salts/jarvis", headers={}, default_branch="main")
    assert "error" in j.proactive.watch_ci("bad branch;rm -rf")
    await j.http.aclose()


async def test_the_watch_tools_are_read_only_and_not_available_to_recruited_agents(settings):
    assert not TOOLS_BY_NAME["watch_ci"].approval and not TOOLS_BY_NAME["watch_action"].approval
    assert {"watch_ci", "watch_action"} <= NO_RECURSE
    j = Jarvis(settings, client=FakeClient())  # setting off
    result = await TOOLS_BY_NAME["watch_action"].handler(j, WatchActionIn(action_id=1))
    assert "switched off" in result["error"]
    await j.http.aclose()


# --------------------------------------------------------------------------- the pull request watch
def pr(number, ci="success", merge="mergeable", sha="aaaaaaaaaa1", title="Add a thing", draft=False, failed=()):
    return {"number": number, "title": title, "head_sha": sha, "ci": ci, "ci_failed": list(failed),
            "merge_status": merge, "draft": draft}


def fake_prs(monkeypatch, state):
    async def list_open_prs(self, limit=20):
        return {"count": len(state["prs"]), "pull_requests": state["prs"]}

    monkeypatch.setattr(PRClient, "list_open_prs", list_open_prs)


async def test_pr_watch_records_a_baseline_then_speaks_only_about_changes(settings, monkeypatch, teams):
    j = make(settings)
    j.self_github = SimpleNamespace(repo="salts/jarvis", headers={}, default_branch="main")
    sent = teams(j)
    q = j.bus.subscribe()
    state = {"prs": [pr(7, ci="pending")]}
    fake_prs(monkeypatch, state)

    assert (await j.proactive.pr_watch()) == {"baseline": 1}
    assert (await j.proactive.pr_watch()) == {"changed": False}
    assert drain(q) == [] and sent == []

    state["prs"] = [pr(7, ci="failure", failed=["pytest"]), pr(8, title="Fix `the` *bug*")]
    out = await j.proactive.pr_watch()
    assert out["changed"] is True and out["delivered"] is True
    [event] = drain(q)
    text = event["data"]["text"]
    assert "CI failed on PR #7" in text and "pytest" in text and "New pull request: PR #8 (Fix 'the' bug)" in text
    assert "Nothing has been merged or changed" in text
    assert len(sent) == 1 and sent[0][2] == ("teams",)

    assert (await j.proactive.pr_watch()) == {"changed": False}  # same state again: quiet
    state["prs"] = [pr(8, title="Fix `the` *bug*")]
    await j.proactive.pr_watch()
    assert "PR #7 (Add a thing) is no longer open." in drain(q)[-1]["data"]["text"]
    await j.http.aclose()


async def test_pr_watch_holds_changes_in_quiet_hours_and_reports_them_afterwards(settings, monkeypatch, teams):
    j = make(settings, proactive_quiet_start="21:00", proactive_quiet_end="07:30")
    j.self_github = SimpleNamespace(repo="salts/jarvis", headers={}, default_branch="main")
    sent = teams(j)
    q = j.bus.subscribe()
    state = {"prs": [pr(7, ci="pending")]}
    fake_prs(monkeypatch, state)
    await j.proactive.pr_watch()

    state["prs"] = [pr(7, ci="success")]
    j.proactive._local_now = lambda: NIGHT
    assert (await j.proactive.pr_watch())["delivered"] is False
    assert drain(q) == [] and sent == []
    j.proactive._local_now = lambda: NOON
    assert (await j.proactive.pr_watch())["delivered"] is True
    assert "CI passed on PR #7" in drain(q)[-1]["data"]["text"]
    await j.http.aclose()


async def test_pr_watch_ignores_a_ci_run_merely_starting_and_skips_when_off_or_unconnected(settings, monkeypatch):
    j = make(settings)
    assert await j.proactive.pr_watch() == {"skipped": True}  # no repository connected
    j.self_github = SimpleNamespace(repo="salts/jarvis", headers={}, default_branch="main")
    state = {"prs": [pr(7, ci="success")]}
    fake_prs(monkeypatch, state)
    j.bus.subscribe()
    await j.proactive.pr_watch()
    state["prs"] = [pr(7, ci="pending", sha="bbbbbbbbbb2")]  # a new push: CI restarting is not news
    assert await j.proactive.pr_watch() == {"changed": False}
    settings.proactive_chat_enabled = False
    assert await j.proactive.pr_watch() == {"skipped": True}
    await j.http.aclose()


async def test_pr_watch_is_only_scheduled_when_proactive_is_on_and_the_repo_is_connected(settings):
    from jarvis.services.scheduler import build_scheduler

    j = Jarvis(settings, client=FakeClient())
    assert build_scheduler(j).get_job("pr_watch") is None
    settings.proactive_chat_enabled = True
    assert build_scheduler(j).get_job("pr_watch") is None  # still no repository
    j.self_github = SimpleNamespace(repo="salts/jarvis")
    assert build_scheduler(j).get_job("pr_watch") is not None
    await j.http.aclose()


# --------------------------------------------------------------------------- settings
def test_the_settings_are_editable_and_the_times_are_validated(settings):
    for key in ("proactive_chat_enabled", "proactive_quiet_start", "proactive_quiet_end", "proactive_max_per_hour",
                "proactive_pr_watch_min"):
        assert key in FIELDS
    assert FIELDS["proactive_chat_enabled"].kind == "bool"
    store = SettingsStore(settings)
    assert store.validate("proactive_quiet_start", "21:00") == ("21:00", "")
    assert store.validate("proactive_quiet_end", "7:30")[1] == ""
    assert store.validate("proactive_quiet_start", "9pm")[1]
    assert store.validate("proactive_quiet_end", "24:00")[1]
    assert store.validate("proactive_max_per_hour", "0") == (0, "")
    assert store.validate("proactive_max_per_hour", "-1")[1]
    assert store.validate("proactive_pr_watch_min", "0")[1]


def test_proactive_chat_is_off_by_default(settings):
    assert settings.proactive_chat_enabled is False


# --------------------------------------------------------------------------- the session mute (websocket)
def test_a_muted_session_is_not_sent_proactive_messages(settings):
    j = make(settings)
    app = create_app(settings, j)
    with TestClient(app) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}  # the server side is up and subscribed
            c.portal.call(j.bus.publish, "proactive", {"id": "1", "text": "Hello", "source": "", "speak": False})
            assert ws.receive_json()["type"] == "proactive"

            ws.send_json({"type": "proactive_mute", "muted": True})
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}  # the mute message was handled before this
            c.portal.call(j.bus.publish, "proactive", {"id": "2", "text": "Muted", "source": "", "speak": False})
            c.portal.call(j.bus.publish, "stopped", {"stopped": False})
            assert ws.receive_json()["type"] == "stopped"  # the proactive message in between was not sent

            ws.send_json({"type": "proactive_mute", "muted": False})
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
            c.portal.call(j.bus.publish, "proactive", {"id": "3", "text": "Back", "source": "", "speak": False})
            assert ws.receive_json()["data"]["text"] == "Back"


# --------------------------------------------------------------------------- the display
def test_the_display_has_a_mute_button_and_a_proactive_case_that_only_speaks_when_it_may():
    index = (WEB / "index.html").read_text(encoding="utf-8")
    hud = (WEB / "hud.js").read_text(encoding="utf-8")
    assert 'id="btn-proactive-mute"' in index
    assert 'case "proactive": proactive(d)' in hud and '"proactive_mute"' in hud and "sessionStorage" in hud
    may = hud[hud.index("const proactiveMaySpeak"):hud.index("function proactive(d)")]
    for guard in ('S.lastMode === "voice"', 'shouldSpeak("voice")', 'S.hudState === "idle"', "!speaker.active",
                  "!S.voiceTurn", "$(\"#input\").value.trim()"):
        assert guard in may
    body = hud[hud.index("function proactive(d)"):hud.index("let toolsSeen")]
    assert "if (S.proactiveMuted) return" in body and "d.speak && proactiveMaySpeak()" in body
    # a plain notification still never speaks by itself
    assert "never speaks unprompted" in hud


def test_proactive_never_touches_the_approval_path():
    source = Path(proactive_mod.__file__).read_text(encoding="utf-8")
    for forbidden in (".approve(", ".deny(", "set_action_status", "actions.queue", "dispatch("):
        assert forbidden not in source
