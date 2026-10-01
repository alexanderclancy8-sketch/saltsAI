"""Weekly digest: routine engineering notices are stored, not sent; urgent ones still go out immediately; the
digest is ONE Teams-only message, stored, and never repeats items."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jarvis.brain.tools import NoInput, TOOLS_BY_NAME
from jarvis.core import Jarvis
from jarvis.services.digest import (ALL_CLEAR, ALWAYS_IMMEDIATE, DIGEST, IMMEDIATE, NOTIFICATION_ROUTES,
                                    parse_route_overrides, route_for, security_kind, triage_kind)
from tests.fakes import FakeClient


def make(settings, teams=True):
    settings.owner_email = "alex@example.com"
    j = Jarvis(settings, client=FakeClient())
    posts, mails = [], []
    if teams:
        j.teams.url = "https://teams.example/hook"

    async def post(title, body):
        posts.append((title, body))

    async def send_mail(to, subject, html, *a, **k):
        mails.append((to, subject))

    j.teams.post = post
    j.mail.demo = False
    j.mail.send_mail = send_mail
    return j, posts, mails


# ------------------------------------------------------------------------------------------ the route map
def test_map_routes_routine_kinds_to_the_digest_and_urgent_ones_immediately():
    for kind in ("pr_ready", "fix_ready", "deploy_started", "deploy_succeeded", "routine_test_recovered",
                 "issue_triaged", "security_finding_minor", "self_learning_summary"):
        assert route_for(kind) == DIGEST, kind
    for kind in ("deploy_failed", "routine_test_failed", "security_finding_urgent", "approval_deadline",
                 "life_safety_alert", "compliance_alert", "issue_reported"):
        assert route_for(kind) == IMMEDIATE, kind
    assert ALWAYS_IMMEDIATE <= set(NOTIFICATION_ROUTES)
    assert all(NOTIFICATION_ROUTES[k] == IMMEDIATE for k in ALWAYS_IMMEDIATE)


def test_when_unsure_send_immediately():
    assert route_for(None) == IMMEDIATE
    assert route_for("something_new_nobody_classified") == IMMEDIATE
    assert route_for("fix_ready", level="critical") == IMMEDIATE  # critical is never held back


def test_overrides_are_parsed_but_cannot_hold_back_urgent_kinds():
    o = parse_route_overrides("fix_ready=immediate, issue_triaged = digest; junk; x=maybe, deploy_failed=digest")
    assert o == {"fix_ready": IMMEDIATE, "issue_triaged": DIGEST, "deploy_failed": DIGEST}
    assert route_for("fix_ready", overrides=o) == IMMEDIATE
    assert route_for("deploy_failed", overrides=o) == IMMEDIATE  # locked
    assert route_for("brand_new_kind", overrides={"brand_new_kind": DIGEST}) == DIGEST


def test_only_clearly_non_critical_severities_can_wait():
    assert security_kind("low") == security_kind("medium") == "security_finding_minor"
    assert security_kind("high") == security_kind("critical") == "security_finding_urgent"
    assert security_kind("weird") == "security_finding_urgent"
    assert triage_kind("medium") == "issue_triaged" and triage_kind("high") == "issue_triaged_urgent"


# ------------------------------------------------------------------------------------------ store, don't send
async def test_routine_notice_is_stored_not_sent(settings):
    j, posts, mails = make(settings)
    await j.notifier.notify("Fix ready for issue #4: Fix login", "PR: https://gh/pr/9", level="warning", push=True,
                            kind="fix_ready", link="https://gh/pr/9", status="awaiting approval", ref="fix:9")
    assert posts == [] and mails == []
    [item] = j.db.pending_digest_items()
    assert (item["kind"], item["link"], item["status"], item["delivery"]) == ("fix_ready", "https://gh/pr/9",
                                                                             "awaiting approval", "digest")
    assert item["created_at"]
    assert j.db.recent_notifications(1)[0]["title"].startswith("Fix ready")  # still on the local display
    await j.http.aclose()


async def test_urgent_and_untyped_notices_are_still_sent_immediately(settings):
    j, posts, mails = make(settings)
    await j.notifier.notify("Deployment needs attention", "boom", level="critical", kind="deploy_failed")
    await j.notifier.notify("Routine test failed: HTTP home", "500", level="warning", kind="routine_test_failed")
    await j.notifier.notify("Safety check: Sam is overdue", "call them", level="warning", push=True)  # no kind
    await j.notifier.notify("Brand new thing", "?", level="warning", kind="unclassified_kind")
    assert len(posts) == 4 and len(mails) == 4
    kinds = {i["kind"]: i["delivery"] for i in j.db.pending_digest_items()}
    assert kinds == {"deploy_failed": "immediate", "routine_test_failed": "immediate",
                     "unclassified_kind": "immediate"}  # recorded for the summary, but already sent
    await j.http.aclose()


async def test_config_cannot_move_a_failed_deploy_into_the_digest(settings):
    settings.notification_routes = "deploy_failed=digest,pr_ready=immediate"
    j, posts, _ = make(settings)
    await j.notifier.notify("Deploy failed", "x", level="critical", kind="deploy_failed")
    await j.notifier.notify("PR ready", "x", level="info", push=True, kind="pr_ready")
    assert len(posts) == 2  # the locked kind, and the one explicitly configured to be immediate
    await j.http.aclose()


async def test_digest_can_be_switched_off(settings):
    settings.weekly_digest_enabled = False
    j, posts, _ = make(settings)
    await j.notifier.notify("Fix ready", "x", level="warning", push=True, kind="fix_ready")
    assert len(posts) == 1 and j.db.pending_digest_items() == []
    await j.http.aclose()


async def test_store_failure_falls_back_to_sending(settings):
    j, posts, _ = make(settings)

    def broken(*a, **k):
        raise RuntimeError("db locked")

    j.db.add_digest_item = broken
    await j.notifier.notify("Fix ready", "x", level="warning", push=True, kind="fix_ready")
    assert len(posts) == 1
    await j.http.aclose()


# ------------------------------------------------------------------------------------------ the digest
async def _store_week(j):
    n = j.notifier.notify
    await n("Fix ready for issue #4: Fix login", "b", level="warning", push=True, kind="fix_ready",
            link="https://gh/pr/9", status="awaiting approval", ref="fix:9")
    await n("Deploying the fix for issue #3", "b", kind="deploy_started", status="merged", ref="fix:7")
    await n("Issue #3 fixed and live", "b", push=True, kind="deploy_succeeded", status="deployed", ref="fix:7")
    await n("Routine test failed: HTTP home", "500", level="critical", kind="routine_test_failed")
    await n("Recovered: HTTP home", "200", kind="routine_test_recovered")
    await n("Issue #5 triaged: software bug, low", "s", kind="issue_triaged")


async def test_weekly_digest_is_one_teams_only_message_and_marks_items_digested(settings):
    j, posts, mails = make(settings)
    await _store_week(j)
    posts.clear(); mails.clear()  # forget the immediate test-failure alert

    result = await j.weekly_digest.run("scheduled")
    assert len(posts) == 1 and mails == []  # ONE message, Teams only, no email
    title, text = posts[0]
    assert title.startswith("Weekly digest")
    for needle in ("Pull requests opened", "https://gh/pr/9", "Fixes deployed", "Issue #3 fixed and live",
                   "Test failures and recoveries", "Recovered: HTTP home", "Routine test failed: HTTP home"):
        assert needle in text, needle
    assert result["delivered"] == "Teams + display" and result["items"] == 6
    assert j.db.pending_digest_items() == []  # all marked digested
    assert j.db.get_digest(result["id"])["text"] == text  # viewable later
    assert j.weekly_digest.latest()["id"] == result["id"]
    assert j.db.recent_notifications(1)[0]["title"] == "Weekly digest"  # shown on the display

    # Next week: digested items are never repeated. (PR #9 is still unmerged, so the standing "awaiting review"
    # section still appears - that is the point of it - but none of last week's entries do.)
    again = await j.weekly_digest.run("scheduled")
    assert again["items"] == 0 and len(posts) == 2
    assert "Still awaiting review / merge" in posts[1][1] and "https://gh/pr/9" in posts[1][1]
    assert "Issue #3 fixed and live" not in posts[1][1] and "Recovered" not in posts[1][1]
    await j.http.aclose()


async def test_nothing_is_sent_twice_when_nothing_new_and_nothing_open(settings):
    j, posts, _ = make(settings)
    j.db.add_digest_item("deploy_succeeded", "Issue #3 fixed and live", ref="fix:7")
    await j.weekly_digest.run("scheduled")
    again = await j.weekly_digest.run("scheduled")
    assert again["sent"] is False and len(posts) == 1
    await j.http.aclose()


async def test_nothing_to_report_skips_the_send_or_sends_a_one_line_all_clear(settings):
    j, posts, _ = make(settings)
    r = await j.weekly_digest.run("scheduled")
    assert r["sent"] is False and posts == [] and j.db.list_digests() == []
    await j.http.aclose()

    settings.weekly_digest_all_clear = True
    j, posts, _ = make(settings)
    r = await j.weekly_digest.run("scheduled")
    assert posts and posts[0][1] == ALL_CLEAR and "\n" not in ALL_CLEAR
    await j.http.aclose()


async def test_unmerged_pr_older_than_a_week_is_flagged_and_merged_ones_are_not(settings):
    j, posts, _ = make(settings)
    old = j.db.add_digest_item("pr_ready", "Pull request ready: Add widget", link="https://gh/pr/1",
                               status="awaiting review", ref="self:1")
    j.db.execute("UPDATE digest_items SET created_at = ?, digested_at = 'x' WHERE id = ?",
                 ((datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds"), old))
    j.db.add_digest_item("fix_ready", "Fix ready: merged one", ref="fix:2")
    j.db.add_digest_item("deploy_started", "Deploying fix 2", ref="fix:2", status="merged")
    j.db.add_digest_item("pr_ready", "Pull request ready: fresh one", ref="self:3")

    text = j.weekly_digest.build()["text"]
    waiting = text.split("Still awaiting review / merge")[1]
    assert "Add widget" in waiting and "open 10 days" in waiting  # old, already-digested PR still surfaces
    assert "fresh one" in waiting and "open 0 days" not in waiting
    assert "merged one" not in waiting
    await j.http.aclose()


async def test_pr_merged_on_github_is_picked_up(settings):
    j, posts, _ = make(settings)

    class GH:
        async def pr(self, number):
            return {"merged": number == 1, "state": "closed" if number in (1, 2) else "open"}

    j.self_github = GH()
    j.db.add_digest_item("pr_ready", "PR one", link="https://gh/pr/1", ref="self:1")
    j.db.add_digest_item("pr_ready", "PR two", link="https://gh/pr/2", ref="self:2")
    j.db.add_digest_item("pr_ready", "PR three", link="https://gh/pr/3", ref="self:3")
    await j.weekly_digest.run("scheduled")
    text = posts[0][1]
    assert "Merged: PR one" in text and "Closed without merging: PR two" in text
    waiting = text.split("Still awaiting review / merge")[1]
    assert "PR three" in waiting and "PR one" not in waiting and "PR two" not in waiting
    await j.http.aclose()


async def test_digest_lists_approvals_and_does_not_touch_them(settings):
    j, posts, _ = make(settings)
    aid = j.db.create_action("deploy_fix", "Merge PR #9 and deploy", {"issue_id": 4})
    await j.weekly_digest.run("scheduled")
    assert "Needs your decision" in posts[0][1] and f"Approval #{aid}" in posts[0][1]
    assert j.db.get_action(aid)["status"] == "pending"  # still waiting for Alex, nothing approved or executed
    await j.http.aclose()


async def test_failed_teams_post_keeps_items_for_next_time(settings):
    j, posts, _ = make(settings)

    async def boom(title, body):
        raise RuntimeError("webhook down")

    j.teams.post = boom
    j.db.add_digest_item("fix_ready", "Fix ready: X", ref="fix:1")
    r = await j.weekly_digest.run("scheduled")
    assert r["sent"] is False and "failed" in r["delivered"]
    assert len(j.db.pending_digest_items()) == 1  # not marked digested - will be in the next one
    await j.http.aclose()


# ------------------------------------------------------------------------------------------ on demand
async def test_weekly_digest_now_tool_builds_from_the_store_without_sending(settings):
    j, posts, mails = make(settings)
    await _store_week(j)
    posts.clear(); mails.clear()
    tool = TOOLS_BY_NAME["weekly_digest_now"]
    assert tool.approval is False
    text = await tool.handler(j, NoInput())
    assert "Weekly digest" in text and "Issue #3 fixed and live" in text
    assert posts == [] and mails == []  # shown in the chat/display only
    assert j.db.pending_digest_items() == []  # so the Monday digest won't repeat them
    assert "Issue #3 fixed and live" in await TOOLS_BY_NAME["weekly_digest_latest"].handler(j, NoInput())
    await j.http.aclose()


async def test_weekly_digest_now_with_nothing_stored_says_all_clear(settings):
    j, _, _ = make(settings)
    assert await TOOLS_BY_NAME["weekly_digest_now"].handler(j, NoInput()) == ALL_CLEAR
    await j.http.aclose()
