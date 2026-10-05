"""Scheduled checks stay quiet (console redesign phase 3, item 2).

A scheduled check that finds nothing new posts nothing into the conversation; every run goes in the activity log, and the
console shows one collapsed line per check ("Pull request watch · 7 checks since 09:30, no change") that opens to list the
runs with their times. A check that DOES find something still posts, as before, and is logged too. The browser side of
this (the collapsed line, expanding it, both themes, three widths) is in tests/test_console_browser_phase3.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jarvis.core import Jarvis
from jarvis.main import create_app
from jarvis.services.proactive import NOTHING
from tests.fakes import FakeClient, message, text_block
from tests.test_proactive import drain, fake_prs, pr, types

CHAT_EVENTS = {"user_message", "thinking", "delta", "tool", "reply", "error", "proactive"}


def make(settings, script=None, proactive=True):
    settings.proactive_chat_enabled = proactive
    settings.proactive_quiet_start = settings.proactive_quiet_end = "00:00"
    return Jarvis(settings, client=FakeClient(script))


def chat_events(events) -> list[str]:
    return [t for t in types(events) if t in CHAT_EVENTS]


def only_job(j) -> dict:
    jobs = j.activity.summary()["jobs"]
    assert len(jobs) == 1, jobs
    return jobs[0]


# --------------------------------------------------------------------------- the activity log itself
async def test_runs_are_grouped_per_check_with_counts_times_and_outcomes(settings):
    j = make(settings)
    j.activity.record("a", "Alpha check", "no_change", "No change.")
    j.activity.record("a", "Alpha check", "no_change", "No change.")
    j.activity.record("a", "Alpha check", "changed", "PR #7 now has merge conflicts.")
    j.activity.record("a", "Alpha check", "failed", "Couldn't read GitHub.")
    j.activity.record("b", "Beta check", "baseline", "First look: 2 open.")
    jobs = {job["key"]: job for job in j.activity.summary()["jobs"]}
    a, b = jobs["a"], jobs["b"]
    assert (a["checks"], a["no_change"], a["changed"], a["failed"]) == (4, 2, 1, 1)
    assert (b["checks"], b["no_change"], b["changed"], b["failed"]) == (1, 1, 0, 0)  # a baseline is a quiet check
    assert [r["outcome"] for r in a["runs"]] == ["no_change", "no_change", "changed", "failed"]
    assert all(len(r["time"]) == 5 and r["time"][2] == ":" for r in a["runs"]) and a["since"] == a["runs"][0]["time"]
    await j.http.aclose()


async def test_the_log_only_shows_today_and_keeps_detail_short_and_redacted(settings):
    j = make(settings)
    j.db.execute("INSERT INTO check_runs (ran_at, job_key, job_name, outcome, detail) VALUES (?,?,?,?,?)",
                 ("2026-01-01T09:00:00+00:00", "old", "Old check", "no_change", "ancient"))
    j.activity.record("c", "Check", "changed", "see https://jarvis.example.test/report?key=" + "x" * 500 + " access code 4417")
    summary = j.activity.summary()
    assert [job["key"] for job in summary["jobs"]] == ["c"]  # yesterday's runs are not in today's line
    detail = summary["jobs"][0]["runs"][0]["detail"]
    assert len(detail) <= 200 and "x" * 50 not in detail and "4417" not in detail  # no key, no access code, kept short
    await j.http.aclose()


async def test_an_unknown_outcome_never_breaks_a_run_and_old_rows_are_pruned(settings):
    j = make(settings)
    j.db.execute("INSERT INTO check_runs (ran_at, job_key, job_name, outcome, detail) VALUES (?,?,?,?,?)",
                 ("2020-01-01T00:00:00+00:00", "old", "Old", "no_change", ""))
    j.activity.record("c", "Check", "mystery")  # unknown outcome -> recorded as no change, not an error
    assert j.db.query("SELECT COUNT(*) AS n FROM check_runs WHERE job_key = 'old'")[0]["n"] == 0  # pruned on the first write
    assert only_job(j)["no_change"] == 1
    await j.http.aclose()


# --------------------------------------------------------------------------- the pull request watch
async def test_seven_quiet_pull_request_watch_runs_post_nothing_and_make_one_collapsed_line(settings, monkeypatch):
    j = make(settings)
    j.self_github = SimpleNamespace(repo="salts/jarvis", headers={}, default_branch="main")
    sent = []

    async def fake_send(subject, body, channels=("teams",), **kw):
        sent.append(subject)

    monkeypatch.setattr(j.notifier, "send_owner_update", fake_send)
    fake_prs(monkeypatch, {"prs": [pr(7, ci="pending")]})
    q = j.bus.subscribe()
    for _ in range(7):
        await j.proactive.pr_watch()
    events = drain(q)
    assert chat_events(events) == [] and sent == []  # nothing in the conversation, nothing sent anywhere
    job = only_job(j)
    assert job["name"] == "Pull request watch" and job["checks"] == 7 and job["changed"] == 0 and job["failed"] == 0
    assert [r["outcome"] for r in job["runs"]] == ["baseline"] + ["no_change"] * 6
    assert set(types(events)) == set()  # not even an event: the console reads the log from /api/status
    await j.http.aclose()


async def test_a_pull_request_change_still_posts_normally_and_is_logged(settings, monkeypatch):
    j = make(settings)
    j.self_github = SimpleNamespace(repo="salts/jarvis", headers={}, default_branch="main")

    async def fake_send(*a, **kw):
        return "Teams"

    monkeypatch.setattr(j.notifier, "send_owner_update", fake_send)
    state = {"prs": [pr(7, ci="pending")]}
    fake_prs(monkeypatch, state)
    q = j.bus.subscribe()
    await j.proactive.pr_watch()
    await j.proactive.pr_watch()
    state["prs"] = [pr(7, ci="failure", failed=["pytest"])]
    await j.proactive.pr_watch()
    events = drain(q)
    assert chat_events(events).count("proactive") == 1  # the change is a message in the chat
    job = only_job(j)
    assert job["checks"] == 3 and job["changed"] == 1
    assert job["runs"][-1]["outcome"] == "changed" and "CI failed on PR #7" in job["runs"][-1]["detail"]
    await j.http.aclose()


async def test_a_pull_request_watch_that_cannot_reach_github_is_logged_as_failed_not_posted(settings, monkeypatch):
    j = make(settings)
    j.self_github = SimpleNamespace(repo="salts/jarvis", headers={}, default_branch="main")

    async def boom(self, limit=30):
        raise RuntimeError("github down")

    monkeypatch.setattr("jarvis.integrations.github_pr.PRClient.list_open_prs", boom)
    q = j.bus.subscribe()
    assert (await j.proactive.pr_watch()) == {"error": "RuntimeError"}
    assert chat_events(drain(q)) == []
    job = only_job(j)
    assert job["failed"] == 1 and "GitHub" in job["runs"][0]["detail"]
    await j.http.aclose()


# --------------------------------------------------------------------------- automations (the owner's own scheduled checks)
@pytest.mark.parametrize("proactive", [True, False])
async def test_an_automation_that_finds_nothing_posts_nothing_with_speaking_up_on_or_off(settings, proactive):
    j = make(settings, [message([text_block(f"{NOTHING}: all clear.")])] * 3, proactive=proactive)
    created = j.automations.create("Pull request watch", "*/10 * * * *", "Check the pull requests. Never merge.")
    q = j.bus.subscribe()
    await j.automations.run(created["id"])
    await j.automations.run(created["id"])
    events = drain(q)
    assert chat_events(events) == []  # not the headless turn, not a "nothing to report" message
    assert NOTHING in j.brain.messages[0]["content"][-1]["text"]  # always asked to say so, with or without speaking up
    job = only_job(j)
    assert job["name"] == "Pull request watch" and job["checks"] == 2 and job["no_change"] == 2
    assert job["runs"][0]["detail"] == "all clear."
    # the stored instruction is untouched (it still says never merge)
    assert "Never merge" in j.db.get_automation(created["id"])["prompt"]
    await j.http.aclose()


async def test_an_automation_that_finds_something_posts_once_when_speaking_up_is_off(settings):
    j = make(settings, [message([text_block("PR 71 has merge conflicts.")])] * 2, proactive=False)
    created = j.automations.create("Pull request watch", "*/10 * * * *", "Check the pull requests.")
    q = j.bus.subscribe()
    await j.automations.run(created["id"])
    first = drain(q)
    assert chat_events(first) == ["proactive"]  # one message, not the whole turn (no user_message/thinking/reply)
    assert "PR 71 has merge conflicts." in first[0]["data"]["text"] and first[0]["data"]["speak"] is False
    await j.automations.run(created["id"])  # the same finding again is not news
    assert chat_events(drain(q)) == []
    job = only_job(j)
    assert job["checks"] == 2 and job["changed"] == 1 and job["no_change"] == 1
    assert job["runs"][1]["detail"] == "Same as last time."
    await j.http.aclose()


async def test_an_automation_finding_is_kept_as_a_notification_when_no_chat_is_open_and_speaking_up_is_off(settings):
    j = make(settings, [message([text_block("The Kestrel quote is overdue.")])], proactive=False)
    created = j.automations.create("Quote watch", "0 8 * * *", "Check quotes.")
    await j.automations.run(created["id"])
    assert any("The Kestrel quote is overdue." in n["body"] for n in j.db.recent_notifications(10))
    assert only_job(j)["changed"] == 1
    await j.http.aclose()


async def test_a_failing_automation_is_logged_and_posts_nothing(settings):
    j = make(settings, proactive=False)
    created = j.automations.create("Broken", "0 8 * * *", "do something")

    async def boom(*a, **k):
        raise RuntimeError("down")

    j.brain.ask = boom
    q = j.bus.subscribe()
    await j.automations._run_guarded(created["id"])  # noqa: SLF001
    assert chat_events(drain(q)) == []
    job = only_job(j)
    assert job["failed"] == 1 and "RuntimeError" in job["runs"][0]["detail"]
    await j.http.aclose()


# --------------------------------------------------------------------------- the other scheduled checks
async def test_the_lone_worker_and_inbox_sweeps_are_logged_as_checks(settings):
    from jarvis.services.scheduler import build_scheduler

    settings.scheduler_enabled = False
    j = Jarvis(settings, client=FakeClient())

    async def nothing(*a, **k):
        return []

    j.tracker.lone_worker_check = nothing
    sched = build_scheduler(j)
    await sched.get_job("lone_worker").func()
    job = only_job(j)
    assert job["key"] == "lone_worker" and job["no_change"] == 1 and job["changed"] == 0
    await j.http.aclose()


async def test_a_failing_sweep_is_logged_and_does_not_raise(settings):
    from jarvis.services.scheduler import build_scheduler

    j = Jarvis(settings, client=FakeClient())

    async def boom(*a, **k):
        raise RuntimeError("x")

    j.tracker.lone_worker_check = boom
    await build_scheduler(j).get_job("lone_worker").func()
    assert only_job(j)["failed"] == 1
    await j.http.aclose()


# --------------------------------------------------------------------------- what the console is given
def test_the_status_endpoint_carries_the_activity_and_the_pages_have_somewhere_to_show_it(settings):
    j = make(settings)
    j.activity.record("pr_watch", "Pull request watch", "no_change", "No change.")
    with TestClient(create_app(settings, j)) as c:
        status = c.get("/api/status").json()
    [job] = status["activity"]["jobs"]
    assert job["name"] == "Pull request watch" and job["checks"] == 1
    from pathlib import Path

    web = Path(__file__).resolve().parent.parent / "jarvis" / "web"
    assert 'id="activity"' in (web / "index.html").read_text(encoding="utf-8")
    hud = (web / "hud.js").read_text(encoding="utf-8")
    assert "function renderActivity" in hud and "renderActivity(st.activity)" in hud and "details class=\"auto\"" in hud
    # one collapsed line per check, never a chat message
    assert "addMessage" not in hud[hud.index("function renderActivity"):hud.index("function renderActivity") + 1200]
