"""What Jarvis did: the one read model behind the console's activity page and the voice tool.

Pinned here:

* every SOURCE maps to the one item shape - actions (waiting / approved / declined / failed / dismissed / edited, standing-approval
  auto-runs, edit and retry chains), scheduled checks (quiet ones collapse), suggestions, engineering runs, background calls,
  memory, audit lines, drafted documents and adverts, upsell wordings, automations, and the owner-only engineer-home audit lines;
* filters, free-text search, paging (server side, 200 a page at most, a hard cap on rows read) and the time windows;
* permissions: signed out 401, team 403, a manager may read but not export, the owner exports, cross-site clicks are refused;
* redaction: secret-looking strings, the live settings secrets, sensitive payload keys, coordinates and postcodes never reach a
  response or the CSV;
* the CSV: spreadsheet formulas are neutralised, quoting survives a round trip;
* the audit trail the page adds (settings saved - names only, memory, team access, exports);
* the `what_did_you_do` tool: counts first, then up to five notable items, never a secret, sample data left out and said so,
  never for a team session;
* it only READS: a grep keeps every verb that could approve, send or change out of the module.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis import access
from jarvis.brain.tools import TOOLS_BY_NAME, WhatDidYouDoIn, dispatch
from jarvis.core import Jarvis
from jarvis.main import create_app
from jarvis.services import activity_feed as af
from jarvis.services.activity_feed import Query
from tests.fakes import FakeClient, message, text_block, tool_block

ROOT = Path(__file__).resolve().parent.parent
OWNER_PW = "owner-pass-1234"
TEAM_CODE = "team-code-5678"
MANAGER = "manager@salts.example"
SECRET_BEARER = "Bearer abcdefghijklmnop1234567890"
SECRET_ANT = "sk-ant-api03-zzzzzzzzzzzzzzzzzzzz"
SECRET_SIG = "sv=2020&sig=SUPERSECRETSIGNATURE123"
SECRET_HOOK = "https://prod-12.westeurope.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=HOOKSIGNATURE999"
STAFF_KEY = "staff-report-key-8765"
HOME_LAT, HOME_LNG = 53.912345, -1.654321
POSTCODE = "BD16 1AA"


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


@pytest.fixture
def j(settings):
    return Jarvis(settings, client=FakeClient())


def feed(j) -> af.ActivityFeed:
    return j.activity_feed


def query(j, rng="today", **kw) -> Query:
    since, until, _ = feed(j).window(rng)
    return Query(since, until, **kw)


def items(j, rng="today", **kw) -> list[dict]:
    return feed(j).page(query(j, rng, **kw), limit=200, summary=False, facets=False)["items"]


def one(j, ident: str, rng="today", **kw) -> dict:
    (it,) = [i for i in items(j, rng, **kw) if i["id"] == ident]
    return it


def email(db, to="dan@kestrel.example.com", subject="Quote Q-1042", body="Hi Dan, the quote is attached.", summary=None, **kw):
    return db.create_action("email_send", summary or f"Send '{subject}' to {to}", {"to": [to], "subject": subject, "body": body}, **kw)


def backdate(db, table: str, row_id, days: float, cols=("created_at", "decided_at")):
    stamp = iso(now() - timedelta(days=days))
    for c in cols:
        db.execute(f"UPDATE {table} SET {c} = ? WHERE id = ? AND {c} != ''" if c == "decided_at" else f"UPDATE {table} SET {c} = ? WHERE id = ?",
                   (stamp, row_id))


# ====================================================================================================== actions: statuses
def test_a_waiting_email_is_a_draft_waiting_for_you_with_a_link_to_approvals(j):
    a = email(j.db)
    it = one(j, f"action:{a}")
    assert (it["kind"], it["status"], it["status_label"]) == ("draft", "waiting", "Waiting for you")
    assert it["requested_by"] == "Jarvis" and it["decided_by"] == "" and it["attention"] is True
    assert it["link"] == {"pop": "approvals", "label": "Open in Approvals"}
    assert it["source"] == "action" and it["source_ref"] == f"action #{a}"
    assert {d["label"] for d in it["detail"]} >= {"To", "Subject", "Message"}


def test_an_approved_and_sent_email_is_an_email_with_who_approved_it_and_when(j):
    a = email(j.db)
    assert j.db.decide_pending_action(a, "approved", "Sam Taylor")
    j.db.set_action_status(a, "done", "Email sent to dan@kestrel.example.com")
    it = one(j, f"action:{a}")
    assert (it["kind"], it["status"]) == ("email", "approved")
    assert it["decided_by"] == "Sam Taylor" and it["decided_at"] and "approved by Sam Taylor" in it["who"]
    assert it["link"] is None and it["attention"] is False
    assert any(d["label"] == "Result" and "Email sent" in d["value"] for d in it["detail"])


def test_a_declined_action_says_who_declined_it_and_stays_a_draft(j):
    a = email(j.db)
    assert j.db.decide_pending_action(a, "denied", "Sam Taylor", "Denied by Sam Taylor")
    it = one(j, f"action:{a}")
    assert (it["kind"], it["status"], it["decided_by"]) == ("draft", "declined", "Sam Taylor")
    assert "declined by Sam Taylor" in it["who"] and it["link"] is None


def test_a_standing_approval_auto_run_is_labelled_as_such_and_never_as_a_person(j):
    a = j.db.create_action("fsm_write", "Create customer 'Acme'", {"method": "POST", "path": "/customers", "body": {"name": "Acme"}},
                           status="approved", approved_by="standing approval: record_keeping")
    j.db.set_action_status(a, "done", "Salts FSM updated: ok")
    it = one(j, f"action:{a}")
    assert it["status"] == "auto_approved" and it["status_label"] == "Auto-approved (standing)" and it["auto"] is True
    assert it["decided_by"] == "standing approval (record_keeping)" and it["kind"] == "fsm_change"
    assert af.summary_line(feed(j).counts("today"))  # and it is counted as automatic
    assert "1 approved (1 automatically)" in feed(j).summary()["line"]


def test_a_failed_standing_approval_run_is_a_failure_that_needs_a_look_but_still_flagged_automatic(j):
    a = j.db.create_action("fsm_write", "Create customer 'Acme'", {"method": "POST", "path": "/customers", "body": {}},
                           status="approved", approved_by="standing approval: record_keeping")
    j.db.set_action_status(a, "failed", "Salts FSM did not accept the change (HTTP 422): name already exists")
    it = one(j, f"action:{a}")
    assert it["status"] == "failed" and it["auto"] is True and it["attention"] is True
    assert "HTTP 422" in it["error"] and it["link"]["pop"] == "approvals"


def test_a_failed_action_shows_its_error_and_needs_a_look(j):
    a = email(j.db)
    j.db.decide_pending_action(a, "approved", "the owner")
    j.db.set_action_status(a, "failed", "Graph said 403: the mailbox is not allowed to send")
    it = one(j, f"action:{a}")
    assert (it["kind"], it["status"], it["attention"]) == ("email", "failed", True)      # it was attempted, so it is an email
    assert it["error"] == "Graph said 403: the mailbox is not allowed to send"
    assert it["link"] and feed(j).summary()["failed"] == 1


def test_an_action_blocked_by_the_security_check_after_approval_is_a_failure_not_a_decline(j):
    a = email(j.db)
    j.db.decide_pending_action(a, "approved", "Sam")
    j.db.set_action_status(a, "denied", "Blocked by the security check: mandate 3")
    it = one(j, f"action:{a}")
    assert it["status"] == "failed" and it["decided_by"] == "the security check" and "mandate 3" in it["error"]


def test_a_retried_failure_links_to_its_retry_and_stops_asking_for_a_look(j):
    a = email(j.db)
    j.db.decide_pending_action(a, "approved", "Sam")
    j.db.set_action_status(a, "failed", "timeout")
    new = j.db.retry_failed_action(a, f"Retry of #{a}: send it")
    old, fresh = one(j, f"action:{a}"), one(j, f"action:{new}")
    assert old["status"] == "failed" and old["attention"] is False and old["link"] is None and f"Retried as action #{new}" in old["chain"]
    assert fresh["status"] == "waiting" and f"retry of failed action #{a}" in fresh["chain"] and fresh["requested_by"] == f"Jarvis (retry of #{a})"


def test_an_edited_action_is_closed_as_edited_and_the_new_one_waits(j):
    a = email(j.db)
    new = j.db.supersede_pending_action(a, "email_send", "Send 'Quote' (edited)", {"to": ["dan@kestrel.example.com"], "subject": "Quote v2", "body": "x"}, "Sam")
    old, fresh = one(j, f"action:{a}"), one(j, f"action:{new}")
    assert old["status"] == "edited" and old["decided_by"] == "Sam" and f"Replaced by action #{new}" in old["chain"]
    assert "edited by Sam" in old["who"] and old["link"] is None
    assert fresh["status"] == "waiting" and f"Edited from action #{a}" in fresh["chain"] and "edited from" in fresh["requested_by"]


def test_a_dismissed_failure_is_dismissed_with_who_and_keeps_its_error(j):
    a = j.db.create_action("fsm_write", "Remove van 'AB1 CDE'", {"method": "DELETE", "path": "/vehicles/AB1", "body": None})
    j.db.set_action_status(a, "failed", "HTTP 404: no such vehicle")
    assert j.db.dismiss_failed_action(a, "Sam")
    it = one(j, f"action:{a}")
    assert it["status"] == "dismissed" and it["attention"] is False and it["link"] is None
    assert "HTTP 404" in it["error"] and "dismissed by Sam" in it["chain"]
    assert [i["id"] for i in items(j, statuses=[af.NEEDS_LOOK])] == []


def test_a_team_requesters_name_is_the_requester(j):
    a = j.db.create_action("fsm_write", "Log a job (asked for by Sam (team))", {"method": "POST", "path": "/jobs", "body": {"d": "x"}, "requested_by": "Sam (team)"})
    it = one(j, f"action:{a}")
    assert it["requested_by"] == "Sam (team)" and it["kind"] == "job_proposal"


@pytest.mark.parametrize("kind,payload,status,expected", [
    ("email_send", {}, "waiting", "draft"), ("email_send", {}, "approved", "email"), ("email_send", {}, "failed", "email"),
    ("email_send", {}, "declined", "draft"), ("tool:email_send", {}, "auto_approved", "email"), ("po_acknowledgement", {}, "waiting", "draft"),
    ("po_acknowledgement", {}, "approved", "email"), ("review_requests", {}, "waiting", "draft"),
    ("accept_quote", {}, "waiting", "job_proposal"), ("accept_quote_from_po", {}, "approved", "job_proposal"), ("tool:log_job", {}, "waiting", "job_proposal"),
    ("fsm_write", {"method": "POST", "path": "/jobs"}, "waiting", "job_proposal"),
    ("fsm_write", {"method": "POST", "path": "/api/jobs/"}, "waiting", "job_proposal"),
    ("fsm_write", {"method": "POST", "path": "/customers"}, "waiting", "fsm_change"),
    ("fsm_write", {"method": "PATCH", "path": "/jobs"}, "waiting", "fsm_change"), ("tool:fsm_change", {}, "waiting", "fsm_change"),
    ("deploy_fix", {}, "waiting", "code_change"), ("tool:issue_fix", {}, "waiting", "code_change"),
    ("sage_invoices", {}, "waiting", "other"), ("tool:stock_move", {}, "waiting", "other"), ("tool:oncall_add", {}, "waiting", "other"),
])
def test_action_kinds(kind, payload, status, expected):
    assert af._classify_action(kind, payload, status) == expected


def test_an_access_code_update_shows_no_code_not_even_redacted(j):
    a = j.db.create_action("tool:site_access_code_update", "Record access code for Acme House - fire alarm",
                           {"tool": "site_access_code_update", "args": {"site": "Acme House", "system": "fire alarm", "code": "4821", "notes": ""}})
    it = one(j, f"action:{a}")
    assert "4821" not in json.dumps(it) and any("never listed" in d["value"] for d in it["detail"])


# ====================================================================================================== other sources
def test_quiet_checks_collapse_into_one_line_and_changed_or_failed_ones_are_rows(j):
    for _ in range(5):
        j.activity.record("pr_watch", "Pull request watch", "no_change", "Nothing new.")
    j.activity.record("po_intake_scan", "Purchase order scan", "changed", "2 new.")
    j.activity.record("lone_worker", "Lone-worker check", "failed", "Failed: TimeoutError")
    page = feed(j).page(query(j))
    kinds = {i["what"]: i for i in page["items"]}
    assert set(kinds) == {"Purchase order scan found something: 2 new.", "Lone-worker check didn't run properly"}
    assert kinds["Lone-worker check didn't run properly"]["status"] == "failed" and kinds["Lone-worker check didn't run properly"]["error"] == "Failed: TimeoutError"
    assert kinds["Purchase order scan found something: 2 new."]["requested_by"] == "Scheduled job"
    assert page["quiet"]["count"] == 5 and page["quiet"]["jobs"][0]["name"] == "Pull request watch" and page["quiet"]["jobs"][0]["count"] == 5
    assert "5 checks with nothing to report" in page["summary"]["line"]
    full = feed(j).page(query(j, everything=True))
    assert len(full["items"]) == 7 and "quiet" not in full
    assert sum(1 for i in full["items"] if i["quiet"]) == 5


def test_the_owner_only_audit_lines_are_the_principal_owners_alone(j):
    j.activity.record("engineer_homes", "Engineer homes", "changed", "Home point set for Dan Harper by the owner")
    j.activity.record("pr_watch", "Pull request watch", "no_change", "x")
    assert [i["what"] for i in items(j, owner=False)] == []
    mine = items(j, owner=True)
    assert len(mine) == 1 and mine[0]["what"] == "Home point set for Dan Harper by the owner"
    assert mine[0]["kind"] == "settings_change" and mine[0]["requested_by"] == "the owner"
    assert feed(j).quiet(query(j, owner=False))["count"] == 1 and feed(j).quiet(query(j, owner=True))["count"] == 1  # only the pr_watch run is quiet
    assert [x["what"] for x in items(j, owner=False, everything=True)] == ["Pull request watch: nothing to report"]


def test_an_audit_line_with_a_postcode_or_a_point_in_it_is_cleaned(j):
    j.activity.record("engineer_homes", "Engineer homes", "changed", f"Home point set for Dan Harper ({POSTCODE}, 53.9123, -1.6543) by the owner")
    (it,) = items(j, owner=True)
    text = json.dumps(it)
    assert POSTCODE not in text and "53.9123" not in text and "1.6543" not in text


def test_suggestions_open_dismissed_and_prepared(j):
    j.db.upsert_suggestion("quote_followup:Q1180", "Chase quote Q1180", "Sent 12 days ago", "chase it", 2, kind="quote_followup")
    j.db.upsert_suggestion("unbilled:1", "Invoice job J-1", "Completed, not billed", "bill it", 2)
    j.db.set_suggestion_status("unbilled:1", "dismissed")
    j.db.upsert_suggestion("quote_followup:Q1181", "Chase quote Q1181", "x", "y", 2, kind="quote_followup")
    j.db.set_suggestion_status("quote_followup:Q1181", "prepared")
    by = {i["id"]: i for i in items(j)}
    assert by["suggestion:quote_followup:Q1180"]["status"] == "waiting" and by["suggestion:quote_followup:Q1180"]["link"]["pop"] == "approvals"
    assert by["suggestion:unbilled:1"]["status"] == "dismissed" and by["suggestion:unbilled:1"]["kind"] == "suggestion"
    assert by["suggestion:quote_followup:Q1181"]["status"] == "done"
    assert any("draft was prepared" in d["value"] for d in by["suggestion:quote_followup:Q1181"]["detail"])


def test_an_open_suggestion_that_keeps_being_refreshed_stays_as_old_as_it_was_raised(j):
    j.db.upsert_suggestion("quote_followup:Q1", "Chase Q1", "d", "p", 2, kind="quote_followup")
    j.db.execute("UPDATE suggestions SET created_at = ?, updated_at = ? WHERE key = ?", (iso(now() - timedelta(days=3)), iso(now()), "quote_followup:Q1"))
    assert items(j, "today") == [] and [i["id"] for i in items(j, "7d")] == ["suggestion:quote_followup:Q1"]


def test_engineering_runs_background_calls_memory_documents_adverts_automations(j):
    run = j.self_improve.runs.start("self_improve", "Make the rail count clearer")
    j.self_improve.runs.finish("submitted", "https://github.com/alexanderclancy8-sketch/saltsAI/pull/321", run)
    gave_up = j.self_improve.runs.start("fixer", "Issue #4: x")
    j.self_improve.runs.finish("gave_up", "Could not reproduce", gave_up)
    failed = j.self_improve.runs.start("security_watch", "Security review of the Salts FSM codebase")
    j.self_improve.runs.finish("failed", "GitHub said no", failed)
    bg = j.db.add_background_call("fsm_jobs", json.dumps({"date_from": "2026-10-01"}), "silent")
    j.db.finish_background_call(bg, "failed", "The FSM did not answer")
    bg_ok = j.db.add_background_call("fsm_jobs", "{}", "silent")
    j.db.finish_background_call(bg_ok, "done", "6 jobs")
    j.db.remember("The Ilkley key safe is held by reception on Mondays.")
    j.db.add_document("d1", "quote_scope", "Scope of works for Acme", "# text")
    j.db.execute("INSERT INTO adverts (id, created_at, updated_at, platform, width, height, headline, subtext, visual, revision, designer, html)"
                 " VALUES ('ad1', ?, ?, 'facebook', 1200, 630, 'Fire safety checks', '', '', 2, 'claude', '<p/>')", (iso(now()), iso(now())))
    j.db.create_automation("Weekday overdue-jobs check", "0 8 * * 1-5", "check overdue")
    by = {i["id"]: i for i in items(j)}
    pr = by[f"run:{run}"]
    assert (pr["kind"], pr["status"]) == ("code_change", "done") and pr["link"] == {"href": "https://github.com/alexanderclancy8-sketch/saltsAI/pull/321", "label": "Open the pull request"}
    assert f"run:{gave_up}" not in by                                       # it changed nothing: quiet
    assert by[f"run:{failed}"]["status"] == "failed" and by[f"run:{failed}"]["error"] == "GitHub said no"
    assert by[f"background:{bg}"]["status"] == "failed" and "FSM did not answer" in by[f"background:{bg}"]["error"]
    assert f"background:{bg_ok}" not in by                                  # finished fine: quiet
    assert any(i["kind"] == "memory" and "Ilkley" in i["what"] for i in by.values())
    assert by["document:d1"]["kind"] == "draft" and "Scope of works" in by["document:d1"]["what"]
    assert by["advert:ad1"]["kind"] == "draft" and "Revised" in by["advert:ad1"]["what"]
    assert any(i["id"].startswith("automation:") and "Weekday overdue-jobs check" in i["what"] for i in by.values())
    every = {i["id"] for i in items(j, everything=True)}
    assert {f"run:{gave_up}", f"background:{bg_ok}"} <= every              # the toggle brings them in


def test_a_pull_request_link_must_be_a_github_pull_request(j):
    run = j.self_improve.runs.start("self_improve", "x")
    j.self_improve.runs.finish("submitted", "javascript:alert(1)", run)
    assert one(j, f"run:{run}")["link"] is None


def test_a_run_that_stopped_reporting_progress_shows_as_failed(j):
    run = j.self_improve.runs.start("self_improve", "x")
    j.db.execute("UPDATE agent_runs SET updated_at = ? WHERE id = ?", (iso(now() - timedelta(hours=2)), run))
    # "7d", not "today": two hours ago is still on the previous LOCAL day between midnight and 02:00 (BST: 23:00-01:00 UTC), when
    # the "today" window (whole local days) would not contain the row at all
    it = one(j, f"run:{run}", "7d")
    assert it["status"] == "failed" and "stopped reporting progress" in it["error"]


def test_upsell_wordings_jarvis_improved_are_listed_but_refusals_and_rejections_are_not(j):
    at = now().astimezone().isoformat(timespec="seconds")
    j.db.set_kv("upsell:done:U-9:abc", json.dumps({"state": "improved", "at": at}))
    j.db.set_kv("upsell:done:U-8:abc", json.dumps({"state": "rejected", "at": at}))
    j.db.set_kv("upsell:done:U-7:abc", json.dumps({"state": "refused_422", "at": at}))
    j.db.set_kv("upsell:stop:U-6", json.dumps({"why": "409", "at": at}))
    (it,) = [i for i in items(j) if i["source"] == "upsell"]
    assert it["kind"] == "draft" and "U-9" in it["what"] and it["status"] == "done"
    assert any("FSM Action Centre" in d["value"] for d in it["detail"])


def test_audit_lines_and_a_forgotten_memory_are_listed_by_name_and_time(j):
    feed(j).record("settings", "Sam Taylor", "Changed settings: Voice (on)")
    j.activity_feed.record("memory", "Jarvis", "Forgot remembered fact #4")
    by = {i["what"]: i for i in items(j)}
    assert by["Changed settings: Voice (on)"]["kind"] == "settings_change" and by["Changed settings: Voice (on)"]["requested_by"] == "Sam Taylor"
    assert by["Forgot remembered fact #4"]["kind"] == "memory"


def test_every_item_has_the_common_shape(j):
    email(j.db)
    j.activity.record("pr_watch", "PR watch", "changed", "1 new.")
    feed(j).record("settings", "x", "Changed settings: y")
    for it in items(j):
        assert set(it) == {"id", "when", "kind", "kind_label", "what", "status", "status_label", "who", "requested_by", "decided_by", "decided_at",
                           "created_at", "source", "source_ref", "detail", "error", "link", "quiet", "attention", "auto", "chain", "sample"}
        assert it["kind"] in af.KIND_LABELS and it["status"] in af.STATUS_LABELS and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", it["when"])


# ====================================================================================================== windows, filters, search
def test_the_time_windows_are_whole_local_days(j):
    a_today, a_yday, a_week, a_month, a_old = (email(j.db, subject=f"S{i}") for i in range(5))
    for aid, days in ((a_yday, 1), (a_week, 4), (a_month, 20), (a_old, 45)):
        backdate(j.db, "pending_actions", aid, days)
    ids = lambda rng: {i["id"] for i in items(j, rng)}  # noqa: E731
    assert ids("today") == {f"action:{a_today}"}
    assert ids("yesterday") == {f"action:{a_yday}"}
    assert ids("7d") == {f"action:{a_today}", f"action:{a_yday}", f"action:{a_week}"}
    assert ids("30d") == {f"action:{a_today}", f"action:{a_yday}", f"action:{a_week}", f"action:{a_month}"}
    with pytest.raises(ValueError):
        feed(j).window("fortnight")


def test_the_window_follows_the_company_timezone_not_the_servers(j):
    from zoneinfo import ZoneInfo

    j.settings.timezone = "Pacific/Auckland"
    at = datetime(2026, 10, 7, 11, 0, tzinfo=timezone.utc)                           # 00:00 on 8 Oct in Auckland (UTC+13)
    since, until, _ = af.window("today", ZoneInfo("Pacific/Auckland"), now=at)
    assert since == "2026-10-07T11:00:00+00:00" and until == ""
    since, until, _ = af.window("yesterday", ZoneInfo("Pacific/Auckland"), now=at)
    assert (since, until) == ("2026-10-06T11:00:00+00:00", "2026-10-07T11:00:00+00:00")


def test_filters_kind_status_who_and_the_needs_a_look_shortcut(j):
    waiting = email(j.db, subject="Waiting one")
    sent = email(j.db, subject="Sent one")
    j.db.decide_pending_action(sent, "approved", "Sam")
    j.db.set_action_status(sent, "done", "sent")
    failed = j.db.create_action("fsm_write", "Create site 'X'", {"method": "POST", "path": "/sites", "body": {}})
    j.db.set_action_status(failed, "failed", "boom")
    declined = email(j.db, subject="Declined one")
    j.db.decide_pending_action(declined, "denied", "Priya", "Denied by Priya")
    ids = lambda **kw: {i["id"] for i in items(j, **kw)}  # noqa: E731
    assert ids(kinds=["email"]) == {f"action:{sent}"}
    assert ids(kinds=["draft"]) == {f"action:{waiting}", f"action:{declined}"}
    assert ids(statuses=["failed"]) == {f"action:{failed}"}
    assert ids(statuses=[af.NEEDS_LOOK]) == {f"action:{failed}", f"action:{waiting}"}
    assert ids(statuses=["declined", "approved"]) == {f"action:{declined}", f"action:{sent}"}
    assert ids(who="priya") == {f"action:{declined}"}
    assert ids(who="Sam") == {f"action:{sent}"}
    assert ids(who="jarvis") == {f"action:{waiting}", f"action:{sent}", f"action:{failed}", f"action:{declined}"}
    assert ids(kinds=["nonsense"]) == ids()                                     # an unknown kind is ignored, not an error
    assert ids(kinds=["memory"]) == set()


def test_free_text_search_covers_what_who_error_and_the_detail_and_ignores_case(j):
    a = email(j.db, to="pat@brightwell.example.com", subject="Annual service", body="Please confirm the Thursday visit for Brightwell Mill")
    b = j.db.create_action("fsm_write", "Create site 'Unit 4'", {"method": "POST", "path": "/sites", "body": {"name": "Unit 4"}})
    j.db.set_action_status(b, "failed", "Salts FSM did not accept the change (HTTP 422): duplicate")
    ids = lambda text: {i["id"] for i in items(j, text=text)}  # noqa: E731
    assert ids("brightwell mill") == {f"action:{a}"}                  # in the message body (detail)
    assert ids("ANNUAL") == {f"action:{a}"}                           # in the subject, any case
    assert ids("http 422") == {f"action:{b}"}                         # in the error
    assert ids("unit 4") == {f"action:{b}"}
    assert ids("scheduled job") == set() and ids("nothing like this") == set()


def test_search_text_is_bounded_and_a_sql_looking_search_is_harmless(j):
    email(j.db)
    assert items(j, text="'; DROP TABLE pending_actions; --") == []
    assert j.db.query_one("SELECT COUNT(*) AS n FROM pending_actions")["n"] == 1
    assert len(Query("a", text="x" * 5000).text) == 100


# ====================================================================================================== paging, caps, scale
def bulk_actions(db, n, *, when=None, status="pending", kind="email_send", summary="Bulk email"):
    stamp = when or iso(now())
    rows = [(stamp, kind, f"{summary} {i}", json.dumps({"to": ["a@b.example.com"], "subject": f"S{i}", "body": f"Body {i}"}), status, "",
             stamp if status != "pending" else "") for i in range(n)]
    with db._lock:
        db._conn.executemany("INSERT INTO pending_actions (created_at, kind, summary, payload_json, status, result, decided_at) VALUES (?,?,?,?,?,?,?)", rows)
        db._conn.commit()


def test_paging_is_server_side_contiguous_newest_first_and_capped_at_200_a_page(j):
    for i in range(130):
        email(j.db, subject=f"S{i}")                       # (many land in the same second: the record's own number breaks the tie)
    q = query(j)
    seen, offset, pages = [], 0, 0
    while offset is not None:
        page = feed(j).page(q, limit=50, offset=offset, summary=False, facets=False)
        seen += [i["id"] for i in page["items"]]
        assert page["limit"] == 50 and page["offset"] == offset
        offset, pages = page["next_offset"], pages + 1
    assert pages == 3 and len(seen) == len(set(seen)) == 130
    page = feed(j).page(q, limit=200, summary=False, facets=False)["items"]
    assert [i["id"] for i in page] == [f"action:{n}" for n in range(130, 0, -1)]
    whens = [i["when"] for i in page]
    assert whens == sorted(whens, reverse=True)
    assert feed(j).page(q, limit=5000, summary=False, facets=False)["limit"] == 200 and feed(j).page(q, limit=0, summary=False, facets=False)["limit"] == 1
    assert feed(j).page(q, limit=200, offset=-5, summary=False, facets=False)["offset"] == 0
    assert feed(j).page(q, limit=50, offset=130, summary=False, facets=False)["items"] == []


def test_the_summary_and_the_quiet_line_come_only_with_the_first_page(j):
    bulk_actions(j.db, 60)
    q = query(j)
    first = feed(j).page(q, limit=50, offset=0)
    later = feed(j).page(q, limit=50, offset=50)
    assert "summary" in first and "facets" in first and "quiet" in first
    assert "summary" not in later and "facets" not in later and "quiet" not in later


def test_thousands_of_actions_page_quickly_read_a_bounded_number_of_rows_and_run_no_query_per_item(j):
    bulk_actions(j.db, 4000)
    bulk_actions(j.db, 1500, status="done", summary="Done email")
    with j.db._lock:
        j.db._conn.executemany("INSERT INTO check_runs (ran_at, job_key, job_name, outcome, detail) VALUES (?,?,?,?,?)",
                               [(iso(now()), f"job{i % 12}", f"Job {i % 12}", "no_change", "Nothing new.") for i in range(6000)])
        j.db._conn.commit()
    statements: list[str] = []
    j.db._conn.set_trace_callback(statements.append)
    t0 = time.perf_counter()
    page = feed(j).page(query(j), limit=50, offset=0)
    page2 = feed(j).page(query(j), limit=50, offset=2000, summary=False, facets=False)
    took = time.perf_counter() - t0
    j.db._conn.set_trace_callback(None)
    assert len(page["items"]) == 50 and len(page2["items"]) == 50
    assert page["scanned"] <= af.SCAN_CAP * len(af.SOURCES)
    assert page["quiet"]["count"] == 6000 and len(statements) < 80, len(statements)    # one query per source (a few refetches), never one per item
    assert took < 20, took                                                              # (generous: CI machines are slow; typically well under a second)
    plan = " ".join(str(r) for r in j.db.query(
        "EXPLAIN QUERY PLAN SELECT * FROM pending_actions WHERE COALESCE(NULLIF(decided_at, ''), created_at) >= ? "
        "ORDER BY COALESCE(NULLIF(decided_at, ''), created_at) DESC, id DESC LIMIT 50", ("2000",)))
    assert "idx_actions_when" in plan and "TEMP B-TREE" not in plan


def test_a_filter_that_matches_only_old_rows_stops_at_the_scan_cap_and_says_so(j):
    bulk_actions(j.db, af.SCAN_CAP + 300, summary="Chatter")
    odd = email(j.db, subject="Needle", summary="The needle")
    j.db.execute("UPDATE pending_actions SET created_at = ? WHERE id = ?", (iso(now() - timedelta(minutes=5)), odd))
    page = feed(j).page(query(j, "7d", text="needle"), limit=50, summary=False, facets=False)
    assert page["capped"] is True and page["scanned"] <= af.SCAN_CAP * len(af.SOURCES)


def test_paging_beyond_the_reach_cap_ends_with_a_note_not_an_error(j):
    bulk_actions(j.db, af.REACH_CAP + 50)
    page = feed(j).page(query(j), limit=200, offset=af.REACH_CAP - 100, summary=False, facets=False)
    assert page["next_offset"] is None and page["capped"] is True and len(page["items"]) <= 200


def test_sources_a_kind_filter_cannot_match_are_not_read_at_all(j):
    statements: list[str] = []
    j.db._conn.set_trace_callback(statements.append)
    feed(j).page(query(j, kinds=["memory"]), summary=False, facets=False)
    j.db._conn.set_trace_callback(None)
    text = " ".join(statements)
    assert "FROM memory" in text and "FROM pending_actions" not in text and "FROM check_runs" not in text


# ====================================================================================================== redaction
def seed_secrets(j):
    j.settings.staff_report_key = STAFF_KEY
    j.settings.anthropic_api_key = "sk-live-this-is-the-anthropic-key-0000"
    j.settings.jarvis_owner_password = "owner-pass-1234"
    leaky = (f"Hello. {SECRET_BEARER} {SECRET_ANT} {SECRET_HOOK} https://x.example.com/p?{SECRET_SIG} "
             f"link /report?key={STAFF_KEY} token=plain-token-value-99 the alarm code is 4821 {j.settings.anthropic_api_key} "
             f"site at {POSTCODE} home {HOME_LAT}, {HOME_LNG} lat: {HOME_LAT}")
    a = j.db.create_action("email_send", f"Send mail about {POSTCODE} {SECRET_BEARER}",
                           {"to": ["dan@kestrel.example.com"], "subject": f"Re {SECRET_ANT}", "body": leaky})
    b = j.db.create_action("fsm_write", "Create site", {"method": "POST", "path": "/sites",
                           "body": {"name": "X", "lat": HOME_LAT, "lng": HOME_LNG, "password": "hunter2hunter2", "code": "4821", "api_key": "abcd1234efgh",
                                    "nested": {"token": "tok-12345678", "latitude": HOME_LAT}, "note": leaky}})
    j.db.set_action_status(b, "failed", f"HTTP 500 from {SECRET_HOOK} using {SECRET_BEARER} and {STAFF_KEY} at {POSTCODE}")
    j.db.upsert_suggestion("quote_followup:Q9", f"Chase {SECRET_ANT}", f"detail {leaky}", "p", 2, kind="quote_followup")
    j.db.remember(f"remember the code is 4821 and {STAFF_KEY} {POSTCODE}")
    j.activity.record("pr_watch", "PR watch", "failed", f"Failed: {SECRET_BEARER} {STAFF_KEY}")
    j.activity.record("engineer_homes", "Engineer homes", "changed", f"Home point set for Dan ({POSTCODE} {HOME_LAT}, {HOME_LNG}) by the owner")
    bg = j.db.add_background_call("fsm_jobs", json.dumps({"password": "pw-pw-pw-pw", "x": leaky}), "silent")
    j.db.finish_background_call(bg, "failed", f"boom {SECRET_BEARER} {STAFF_KEY}")
    return a, b


FORBIDDEN = ["abcdefghijklmnop1234567890", "zzzzzzzzzzzzzzzzzzzz", "SUPERSECRETSIGNATURE123", "HOOKSIGNATURE999", STAFF_KEY, "plain-token-value-99",
             "hunter2hunter2", "abcd1234efgh", "tok-12345678", "this-is-the-anthropic-key", "owner-pass-1234", POSTCODE, "BD16", "53.912345",
             "1.654321", "4821", "pw-pw-pw-pw"]


def test_no_secret_postcode_or_point_reaches_any_item_in_any_view(j):
    seed_secrets(j)
    for kw in ({}, {"everything": True}, {"owner": True, "everything": True}):
        text = json.dumps(feed(j).page(query(j, **kw), limit=200), ensure_ascii=False)
        for needle in FORBIDDEN:
            assert needle not in text, (needle, kw)
    assert "[REDACTED]" in text                                              # and the redaction is visible, not silent


def test_no_secret_postcode_or_point_reaches_the_csv_or_the_spoken_answer(j):
    seed_secrets(j)
    body, _ = feed(j).export_csv(query(j, owner=True, everything=True), "the owner")
    spoken = feed(j).spoken("today")
    for needle in FORBIDDEN:
        assert needle not in body, needle
        assert needle not in spoken, needle


def test_the_text_cleaner_directly(j):
    c = af.Cleaner(["live-secret-value-123"])
    for raw, gone in ((f"x {SECRET_BEARER}", "abcdefghijklmnop1234567890"), ("pw live-secret-value-123 end", "live-secret-value-123"),
                      ("password: correct-horse-battery", "correct-horse-battery"), (f"see {POSTCODE}.", "BD16"), (f"{HOME_LAT},{HOME_LNG}", "53.912"),
                      ("lng=-1.654321", "1.654321"), ("the pin is 1234", "1234")):
        assert gone not in c.text(raw), raw
    assert c.obj({"Code": "5521", "Home": {"lat": 1.2345}, "ok": "fine"}) == {"Code": "[REDACTED]", "ok": "fine"}
    assert c.text("plain words stay put") == "plain words stay put"
    assert c.text("a" * 5000, 50).endswith("…") and len(c.text("a" * 5000, 50)) <= 50


# ====================================================================================================== CSV
def parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text[1:] if text.startswith(chr(0xFEFF)) else text)))


def test_csv_cells_that_look_like_formulas_are_neutralised(j):
    for i, evil in enumerate(['=HYPERLINK("http://evil","click")', "+1+1", "-2+3", "@SUM(1+1)", "\t=cmd|' /C calc'!A0", "  =1+1", "\r=1+1"]):
        j.db.create_action("fsm_write", evil, {"method": "POST", "path": "/sites", "body": {"name": evil}})
    body, _ = feed(j).export_csv(query(j, owner=True), "the owner")
    rows = parse_csv(body)
    assert rows[0] == af.CSV_COLUMNS and len(rows) == 8
    for row in rows[1:]:
        for cell in row:
            assert not cell.lstrip().startswith(("=", "+", "@")) and not cell.startswith(("\t", "\r")), cell
            assert not (cell.lstrip().startswith("-") and not cell.startswith("'")), cell
    what = [r[5] for r in rows[1:]]
    assert any(w.startswith("'=HYPERLINK") for w in what) and any(w.startswith("'+1") for w in what) and any(w.startswith("'@SUM") for w in what)


def test_csv_quotes_commas_newlines_and_unicode_survive_a_round_trip(j):
    j.db.create_action("fsm_write", 'Create "Smith, Jones & Co" site\nsecond line é', {"method": "POST", "path": "/sites", "body": {"name": "x"}})
    rows = parse_csv(feed(j).export_csv(query(j, owner=True), "the owner")[0])
    assert 'Create "Smith, Jones & Co" site' in rows[1][5] and "é" in rows[1][5]


def test_csv_cell_helper():
    assert af.csv_cell("=1") == "'=1" and af.csv_cell("@x") == "'@x" and af.csv_cell("-1") == "'-1" and af.csv_cell("+1") == "'+1"
    assert af.csv_cell("safe") == "safe" and af.csv_cell(None) == "" and af.csv_cell(5) == "5" and af.csv_cell("a-b") == "a-b"
    assert af.csv_cell("x\x00y") == "xy"


def test_an_export_is_itself_audited_and_reports_when_it_was_cut(j):
    bulk_actions(j.db, 60)
    body, cut = feed(j).export_csv(query(j), "the owner")
    assert cut is False and len(parse_csv(body)) == 61
    assert any("Exported the activity list as CSV (60 rows)" in i["what"] for i in items(j))


# ====================================================================================================== summary line
def test_the_summary_line(j):
    a = email(j.db)
    j.db.decide_pending_action(a, "approved", "Sam")
    j.db.set_action_status(a, "done", "sent")
    b = email(j.db, subject="b")
    j.db.decide_pending_action(b, "denied", "Sam", "Denied by Sam")
    email(j.db, subject="c")
    d = j.db.create_action("fsm_write", "x", {"method": "POST", "path": "/sites", "body": {}})
    j.db.set_action_status(d, "failed", "boom")
    for _ in range(3):
        j.activity.record("pr_watch", "PR watch", "no_change", "x")
    feed(j).record("settings", "the owner", "Changed settings: Voice")
    line = feed(j).summary()["line"]
    assert line == "Today: 4 proposed, 1 approved, 1 declined, 1 waiting, 1 failed, 1 other change, 3 checks with nothing to report"


def test_the_summary_line_when_nothing_has_happened(j):
    assert feed(j).summary()["line"] == "Today: nothing proposed or changed"
    j.activity.record("pr_watch", "PR watch", "no_change", "x")
    assert feed(j).summary()["line"] == "Today: nothing proposed or changed, 1 check with nothing to report"


# ====================================================================================================== the routes
class App:
    def __init__(self, settings, monkeypatch):
        monkeypatch.setattr("jarvis.main.LOGIN_DELAY_S", 0)
        settings.jarvis_owner_password = OWNER_PW
        self.settings = settings
        self.j = Jarvis(settings, client=FakeClient())
        self.app = create_app(settings, self.j)

    def anon(self) -> TestClient:
        return TestClient(self.app)

    def owner(self) -> TestClient:
        c = self.anon()
        assert c.post("/login", data={"password": OWNER_PW}, follow_redirects=False).status_code == 303
        return c

    def team(self) -> TestClient:
        c = self.anon()
        assert c.post("/login/team", data={"name": "Sam", "code": TEAM_CODE}, follow_redirects=False).status_code == 303
        return c


@pytest.fixture
def app(settings, monkeypatch):
    a = App(settings, monkeypatch)
    with TestClient(a.app) as base:
        a.base = base
        assert a.owner().post("/api/team-access", json={"code": TEAM_CODE}).status_code == 200
        yield a


def manager_headers(app, monkeypatch):
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "true")
    app.settings.manager_emails = MANAGER
    return {"x-ms-client-principal-idp": "aad", "x-ms-client-principal-name": MANAGER}


def test_the_routes_are_classified_manager_and_owner_only():
    assert access.ROUTE_POLICY["GET /api/activity"] == access.MANAGER_OK
    assert access.ROUTE_POLICY["GET /api/activity/export.csv"] == access.OWNER_ONLY
    assert access.FEATURES[access.OWNER]["activity"] and access.FEATURES[access.MANAGER]["activity"] and not access.FEATURES[access.TEAM]["activity"]
    assert access.FEATURES[access.OWNER]["activity_export"] and not access.FEATURES[access.MANAGER]["activity_export"]


@pytest.mark.parametrize("path", ["/api/activity", "/api/activity/export.csv"])
def test_signed_out_is_401_and_team_is_403_on_both_routes(app, path):
    assert app.anon().get(path).status_code == 401
    r = app.team().get(path)
    assert r.status_code == 403 and "Activity" not in r.text


def test_a_manager_may_read_but_not_export(app, monkeypatch):
    headers = manager_headers(app, monkeypatch)
    email(app.j.db)
    r = app.anon().get("/api/activity", headers=headers)
    assert r.status_code == 200 and r.json()["can_export"] is False and any(i["kind"] == "draft" for i in r.json()["items"])
    assert app.anon().get("/api/activity/export.csv", headers=headers).status_code == 403


def test_the_owner_reads_and_exports(app):
    c = app.owner()
    email(app.j.db, subject="Hello")
    r = c.get("/api/activity?range=today&limit=10")
    d = r.json()
    assert r.status_code == 200 and d["can_export"] is True and any(i["kind"] == "draft" for i in d["items"]) and r.headers["cache-control"] == "no-store"
    assert d["summary"]["line"].startswith("Today: 1 proposed") and d["limits"] == {"page_max": 200, "reach": af.REACH_CAP}
    x = c.get("/api/activity/export.csv?range=today")
    assert x.status_code == 200 and x.headers["content-type"].startswith("text/csv") and "attachment" in x.headers["content-disposition"]
    assert x.headers["x-content-type-options"] == "nosniff" and x.headers["x-export-truncated"] == "false"
    assert x.text.startswith(chr(0xFEFF)) and len(parse_csv(x.text)) >= 2


def test_the_list_route_takes_filters_and_pages(app):
    c = app.owner()
    bulk_actions(app.j.db, 120)
    a = email(app.j.db, subject="Findable")
    j1 = c.get("/api/activity?limit=100").json()
    assert len(j1["items"]) == 100 and j1["next_offset"] == 100
    j2 = c.get("/api/activity?limit=100&offset=100").json()
    assert len(j2["items"]) == 22 and j2["next_offset"] is None and "summary" not in j2     # (+ the team-code line the fixture wrote)
    assert [i["id"] for i in c.get("/api/activity?q=findable").json()["items"]] == [f"action:{a}"]
    assert len(c.get("/api/activity?kind=email").json()["items"]) == 0
    assert len(c.get("/api/activity?status=waiting,failed&kind=draft&limit=200").json()["items"]) == 121
    assert c.get("/api/activity?range=fortnight").status_code == 400
    assert c.get("/api/activity?limit=banana").status_code == 422
    assert c.get("/api/activity?limit=9999").json()["limit"] == 200


def test_a_request_from_another_site_is_refused(app):
    c = app.owner()
    for path in ("/api/activity", "/api/activity/export.csv"):
        assert c.get(path, headers={"sec-fetch-site": "cross-site"}).status_code == 403
        assert c.get(path, headers={"origin": "https://evil.example"}).status_code == 403
        assert c.get(path, headers={"sec-fetch-site": "same-origin"}).status_code == 200


def test_the_engineer_home_audit_lines_are_the_principal_owners_alone_over_http(app, monkeypatch):
    headers = manager_headers(app, monkeypatch)
    app.j.activity.record("engineer_homes", "Engineer homes", "changed", "Home point set for Dan Harper by the owner")
    owner_text = app.owner().get("/api/activity").text
    mgr_text = app.anon().get("/api/activity", headers=headers).text
    assert "Dan Harper" in owner_text and "Dan Harper" not in mgr_text and "Home point" not in mgr_text
    assert "Dan Harper" in app.owner().get("/api/activity/export.csv").text


def test_secrets_never_reach_the_http_responses_or_the_page(app, caplog):
    seed_secrets(app.j)
    c = app.owner()
    seen = c.get("/api/activity?everything=true&limit=200").text + c.get("/api/activity/export.csv?everything=true").text + c.get("/").text
    for needle in FORBIDDEN:
        assert needle not in seen, needle
    assert "engineer_homes" not in c.get("/api/activity?everything=true").text


# ====================================================================================================== the audit trail the page adds
def audit_lines(j) -> list[str]:
    return [i["what"] for i in items(j, owner=True, kinds=["settings_change", "memory", "other"])]


def test_saving_settings_records_which_settings_changed_by_name_and_never_their_values(app):
    c = app.owner()
    r = c.post("/api/settings", json={"values": {"staff_report_key": "brand-new-staff-key-7777", "standing_record_keeping": True}})
    assert r.status_code == 200, r.text
    lines = audit_lines(app.j)
    mine = [l for l in lines if l.startswith("Changed settings")]
    assert len(mine) == 1 and "Staff report key" in mine[0] and "Record keeping (on)" in mine[0]
    assert "brand-new-staff-key-7777" not in json.dumps(c.get("/api/activity?everything=true").json())
    # saving the same thing again changes nothing, so it records nothing
    c.post("/api/settings", json={"values": {"standing_record_keeping": True}})
    assert len([l for l in audit_lines(app.j) if l.startswith("Changed settings")]) == 1
    c.post("/api/settings", json={"values": {"standing_record_keeping": False}})
    assert any("Record keeping (off)" in l for l in audit_lines(app.j))
    assert c.post("/api/settings", json={"values": {"nonsense_key": 1}}).status_code == 400
    assert len([l for l in audit_lines(app.j) if l.startswith("Changed settings")]) == 2


def test_team_access_and_memory_changes_are_recorded_without_the_code_or_the_text(app):
    c = app.owner()
    c.post("/api/team-access", json={"code": "another-team-code-4444"})
    c.delete("/api/team-access")
    mid = app.j.db.remember("The gate code is private and secret-ish")
    assert c.post(f"/api/memory/facts/{mid}", json={"text": "A new wording that mentions nothing"}).status_code == 200
    assert c.delete(f"/api/memory/facts/{mid}").status_code == 200
    lines = audit_lines(app.j)
    # (the role-less /api/team-access is the ENGINEER code since the office / engineer split, and the line now names which
    # code changed and that only that role was signed out - tests/test_office_role.py)
    assert "Set a new engineer access code (everyone signed in as engineer was signed out)" in lines
    assert "Switched engineer access off (everyone signed in as engineer was signed out)" in lines
    c.post("/api/team-access/office", json={"code": "an-office-code-5555"})
    c.delete("/api/team-access/office")
    lines = audit_lines(app.j)
    assert "Set a new office access code (everyone signed in as office was signed out)" in lines
    assert "Switched office access off (everyone signed in as office was signed out)" in lines
    assert "an-office-code-5555" not in json.dumps(lines)
    assert f"Reworded remembered fact #{mid}" in lines and f"Removed remembered fact #{mid}" in lines
    assert "another-team-code-4444" not in json.dumps(lines) and "A new wording" not in json.dumps(lines)


async def test_the_forget_tool_leaves_an_audit_line_with_the_number_only(settings):
    j = Jarvis(settings, client=FakeClient())
    mid = j.db.remember("Something to forget later")
    await dispatch(j, TOOLS_BY_NAME["forget"], TOOLS_BY_NAME["forget"].model(memory_id=mid))
    lines = [i["what"] for i in items(j)]
    assert f"Forgot remembered fact #{mid}" in lines and not any("Something to forget" in l and "Forgot" in l for l in lines)
    await j.http.aclose()


# ====================================================================================================== the tool
def test_the_tool_is_read_only_and_never_a_team_tool():
    tool = TOOLS_BY_NAME["what_did_you_do"]
    assert tool.approval is False and tool.model is WhatDidYouDoIn
    assert "what_did_you_do" not in access.TEAM_TOOLS
    assert access.tool_allowed("what_did_you_do", None) and not access.tool_allowed("what_did_you_do", access.Caller(access.TEAM, "Sam", "k"))
    assert set(WhatDidYouDoIn.model_json_schema()["properties"]["when"]["enum"]) == {"today", "yesterday", "7d", "30d"}


async def test_a_team_member_cannot_ask_what_jarvis_did(settings):
    j = Jarvis(settings, client=FakeClient())
    email(j.db)
    result = await dispatch(j, TOOLS_BY_NAME["what_did_you_do"], WhatDidYouDoIn(), access.Caller(access.TEAM, "Sam", "k"))
    assert "isn't available to you" in result and "Kestrel" not in result
    await j.http.aclose()


async def test_the_spoken_answer_gives_counts_first_then_notable_items_failures_and_waiting_first(settings, monkeypatch):
    j = Jarvis(settings, client=FakeClient())
    monkeypatch.setattr(af.ActivityFeed, "sample_sources", lambda self: set())              # everything here is "real"
    sent = email(j.db, subject="Sent one")
    j.db.decide_pending_action(sent, "approved", "Sam")
    j.db.set_action_status(sent, "done", "ok")
    declined = email(j.db, subject="Declined one")
    j.db.decide_pending_action(declined, "denied", "Sam", "Denied by Sam")
    for i in range(3):
        email(j.db, subject=f"Waiting {i}")
    failed = j.db.create_action("fsm_write", "Create site 'Unit 4' in Salts FSM", {"method": "POST", "path": "/sites", "body": {}})
    j.db.set_action_status(failed, "failed", "boom")
    for _ in range(4):
        j.activity.record("pr_watch", "PR watch", "no_change", "x")
    text = await dispatch(j, TOOLS_BY_NAME["what_did_you_do"], WhatDidYouDoIn())
    assert text.startswith("Today I put forward 6 things: 1 approved, 1 declined, 3 waiting for you.")
    assert "1 thing failed." in text and "4 checks had nothing to report." in text
    worth = text.split("Worth knowing: ")[1]
    assert worth.startswith("fsm change, Create site 'Unit 4' in Salts FSM, failed;")
    assert worth.index("failed") < worth.index("waiting for you")                          # failures before the waiting ones
    assert len(worth.split(";")) == 5 and "And 1 more." in text                             # at most five items, then "and N more"
    assert "Activity" in text and "What Jarvis did" in text                                 # says where to look
    await j.http.aclose()


async def test_the_spoken_answer_for_a_quiet_day_and_other_periods(settings, monkeypatch):
    j = Jarvis(settings, client=FakeClient())
    monkeypatch.setattr(af.ActivityFeed, "sample_sources", lambda self: set())
    quiet = await dispatch(j, TOOLS_BY_NAME["what_did_you_do"], WhatDidYouDoIn())
    assert quiet.startswith("I haven't put anything forward for approval today.") and "What Jarvis did" in quiet and "Worth knowing" not in quiet
    a = email(j.db)
    backdate(j.db, "pending_actions", a, 1)
    y = await dispatch(j, TOOLS_BY_NAME["what_did_you_do"], WhatDidYouDoIn(when="yesterday"))
    assert y.startswith("Yesterday I put forward 1 thing: 1 waiting for you.")
    w = await dispatch(j, TOOLS_BY_NAME["what_did_you_do"], WhatDidYouDoIn(when="7d"))
    assert w.startswith("In the last seven days I put forward 1 thing")
    await j.http.aclose()


async def test_the_spoken_answer_never_speaks_a_secret_a_link_a_postcode_or_a_point(settings, monkeypatch):
    j = Jarvis(settings, client=FakeClient())
    monkeypatch.setattr(af.ActivityFeed, "sample_sources", lambda self: set())
    seed_secrets(j)
    text = await dispatch(j, TOOLS_BY_NAME["what_did_you_do"], WhatDidYouDoIn())
    for needle in FORBIDDEN + ["http://", "https://", "logic.azure.com"]:
        assert needle not in text, needle
    await j.http.aclose()


async def test_the_spoken_answer_leaves_out_what_only_involved_sample_data_and_says_so(settings):
    j = Jarvis(settings, client=FakeClient())
    assert j.fsm.demo and j.mail.demo                                      # the test stand-ins are the demo sources
    email(j.db, subject="A sample email")
    j.db.create_action("fsm_write", "Create site 'Sample Site'", {"method": "POST", "path": "/sites", "body": {}})
    j.db.remember("A real fact that does not rest on sample data.")
    text = await dispatch(j, TOOLS_BY_NAME["what_did_you_do"], WhatDidYouDoIn())
    assert "Sample Site" not in text and "A sample email" not in text and "left out 2 items that only involved sample data" in text
    assert "I also made 1 other change." in text
    page = feed(j).page(query(j), summary=False, facets=False)["items"]
    assert sum(1 for i in page if i["sample"]) == 2 and len(page) == 3        # the console still lists them, flagged
    await j.http.aclose()


async def test_the_tool_is_reachable_through_a_conversation(settings, monkeypatch):
    monkeypatch.setattr(af.ActivityFeed, "sample_sources", lambda self: set())
    j = Jarvis(settings, client=FakeClient([message([tool_block("what_did_you_do", {"when": "today"})], "tool_use"),
                                            message([text_block("Nothing waiting.")])]))
    email(j.db, subject="Chat one")
    reply = await j.brain.ask("What did you do today?")
    assert "Nothing waiting" in str(reply)
    assert j.db.pending_actions()[0]["summary"].endswith("dan@kestrel.example.com")          # asking changed nothing
    await j.http.aclose()


# ====================================================================================================== it only reads
def test_the_module_has_no_way_to_approve_decline_send_retry_edit_dismiss_or_write_actions():
    src = (ROOT / "jarvis" / "services" / "activity_feed.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith(("#", '"""')))
    for verb in (r"\.approve\(", r"\.deny\(", r"\.dismiss", r"\.retry\(", r"\.edit\(", r"\.queue\(", r"send_mail", r"\.notify\(", r"create_action",
                 r"set_action_status", r"decide_pending_action", r"supersede_pending", r"retry_failed", r"dismiss_failed", r"\.write\(", r"jarvis_call",
                 r"engineer_homes", r"EngineerHomes", r"engineer_home_points", r"\bUPDATE\b", r"\bDELETE\b", r"\bDROP\b"):
        assert not re.search(verb, code), verb
    for stmt in re.findall(r'"(INSERT[^"]*)"', code):
        assert "audit_events" in stmt or stmt == "", stmt                      # the only table it ever writes is its own audit trail


def test_the_activity_module_and_tool_only_write_through_the_audit_helper():
    code = (ROOT / "jarvis" / "services" / "activity_feed.py").read_text(encoding="utf-8")
    assert code.count("add_audit_event(") == 1 and "prune_audit_events(" in code


def test_claude_md_and_readme_describe_the_page():
    text = " ".join((ROOT / "CLAUDE.md").read_text(encoding="utf-8").split())
    assert "What Jarvis did" in text and "services/activity_feed.py" in text and "what_did_you_do" in text and "/api/activity/export.csv" in text
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert "What Jarvis did" in readme
