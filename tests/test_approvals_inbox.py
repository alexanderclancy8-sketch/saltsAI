"""Phase 4a: the Approvals inbox. What a card shows (and that secrets never reach it), and the safety properties of
Approve / Don't send / Edit / Retry:

* nothing runs without a human click - Edit and Retry queue a plain PENDING action and never run anything;
* the payload that is approved is exactly the payload that was shown - a stored payload is never changed in place, an
  edit is a new action and the old one is closed in the same transaction, so it can no longer be approved;
* an edit or a retry never auto-runs, even when it would match a standing approval;
* the model has no way to edit, retry, approve or deny - and the endpoints need the owner's session and a same-origin click.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import jarvis
from jarvis import auth
from jarvis.brain.tools import TOOLS
from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.db import Database
from jarvis.events import EventBus
from jarvis.main import create_app
from jarvis.services import approval_inbox as inbox
from jarvis.services.actions import ActionExecutor, ActionRefused
from jarvis.services.standing_approvals import StandingApprovals
from tests.fakes import FakeClient

SECRET = "sk-ant-api03-abcdefghijklmnop1234567890"
EMAIL = {"to": ["dan@kestrel.example.com"], "cc": [], "subject": "Quote Q-1042", "body": "Hi Dan,\n\nQuote attached.\n\nSalts"}


class Notices:
    def __init__(self):
        self.items = []

    async def notify(self, title, body="", **kw):
        self.items.append((title, body, kw))


class FakeFSM:
    demo = False

    def __init__(self, fail=False):
        self.writes: list[tuple] = []
        self.fail = fail

    async def write(self, method, path, body=None):
        self.writes.append((method, path, body))
        if self.fail:
            raise RuntimeError("Salts FSM did not accept the change (HTTP 422): name already exists")
        return {"id": "new-1"}


class FakeMail:
    demo = False

    def __init__(self, fail=False):
        self.sent: list[tuple] = []
        self.fail = fail

    async def send_mail(self, to, subject, body_html, cc=None, bcc=None, sensitivity=None):
        if self.fail:
            raise RuntimeError("Mailbox unavailable")
        self.sent.append((to, subject, body_html, cc))


class FakeTeams:
    """Stands in for services/teams_approvals.TeamsApprovals: records what it was asked to do."""

    def __init__(self):
        self.offered: list[int] = []
        self.decided: list[tuple[int, str]] = []

    async def offer(self, action):
        self.offered.append(action["id"])

    async def mark_decided(self, action, outcome):
        self.decided.append((action["id"], outcome))

    def stamp(self, who, verb):
        return f"{verb} by {who} at 12:00"


def make_executor(settings, record=False, fsm_fail=False, mail_fail=False):
    settings.standing_record_keeping = record
    settings.standing_acknowledgements = False
    settings.standing_max_per_hour = 20
    db = Database(":memory:")
    notices, fsm, mail = Notices(), FakeFSM(fsm_fail), FakeMail(mail_fail)
    ex = ActionExecutor(db, EventBus(), notices, mail, None, fsm)
    ex.j = SimpleNamespace(verifier=None, settings=settings, mail=mail, fsm=fsm)
    ex.standing = StandingApprovals(settings, db)
    ex.teams_approvals = FakeTeams()
    return ex, db, notices, fsm, mail


async def drain(ex):
    for _ in range(5):
        if not ex._tasks:
            break
        await asyncio.gather(*list(ex._tasks))


# ----------------------------------------------------------------------------------------------------- what a card shows
def test_a_card_shows_exactly_what_will_be_sent_built_from_the_payload():
    action = {"id": 7, "kind": "email_send", "summary": "Quote for Dan", "status": "pending", "payload": EMAIL,
              "created_at": "x", "decided_at": ""}
    v = inbox.view(action)
    rows = {r["label"]: r["value"] for r in v["details"]}
    assert v["kind_label"] == "Email" and rows["To"] == "dan@kestrel.example.com"
    assert rows["Subject"] == "Quote Q-1042" and rows["Message"] == EMAIL["body"]
    assert [f["key"] for f in v["editable_fields"]] == ["to", "cc", "subject", "body"] and v["can_retry"] is False


def test_a_misleading_model_summary_cannot_hide_the_real_recipient():
    action = {"id": 1, "kind": "email_send", "summary": "Just a quick note to the owner", "status": "pending",
              "payload": {**EMAIL, "to": ["someone.else@elsewhere.example.org"]}}
    assert "someone.else@elsewhere.example.org" in json.dumps(inbox.view(action)["details"])


def test_no_secret_reaches_a_card_or_the_data_the_console_loads():
    action = {"id": 3, "kind": "fsm_write", "summary": f"Update with {SECRET}", "status": "failed",
              "result": f"HTTP 401 for https://x.example/api?access_token=abc123def456&x=1 using key {SECRET}",
              "payload": {"method": "POST", "path": "/notes", "body": {"text": f"token {SECRET}",
                                                                      "url": "https://x.example/p?sig=SUPERSECRETSIG"}}}
    text = json.dumps(inbox.view(action))
    assert SECRET not in text and "SUPERSECRETSIG" not in text and "abc123def456" not in text
    assert "[REDACTED]" in text


def test_every_kind_has_a_card_and_only_the_closed_list_can_be_edited():
    samples = {
        "email_send": EMAIL, "tool:email_send": {"tool": "email_send", "args": {**EMAIL, "management_only": False}},
        "fsm_write": {"method": "POST", "path": "/customers", "body": {"name": "Acme"}},
        "sage_invoices": {"jobs": [{"job": "J1", "customer": "C", "net_value": 10, "site": "S"}]},
        "review_requests": {"requests": [{"email": "a@b.co", "site": "S"}]},
        "accept_quote": {"quote_id": "Q1", "job_body": {"x": 1}},
        "accept_quote_from_po": {"quote_id": "Q1", "job_body": {"x": 1}, "po_number": "PO9", "ack_to": "a@b.co"},
        "po_acknowledgement": {"to": "a@b.co", "quote_id": "Q1", "source_message_id": "m"},
        "deploy_fix": {"issue_id": 4, "pr_number": 9}, "tool:pr_merge": {"tool": "pr_merge", "args": {"number": 3}},
        "fsm_renewal_send": {"renewal_id": "ren-1", "version": "a" * 64, "recipients": ["a@b.co"], "customer": "C",
                             "pdf": "/api/fsm/renewals/ren-1/pdf"},
        "something_new": {"a": 1},
    }
    editable = set()
    for kind, payload in samples.items():
        v = inbox.view({"id": 1, "kind": kind, "summary": "s", "status": "pending", "payload": payload})
        assert v["details"], kind
        if v["editable_fields"]:
            editable.add(kind)
    assert editable == {"email_send", "tool:email_send", "fsm_write", "tool:pr_merge"}  # money, bookings, deploys: never


# ----------------------------------------------------------------------------------------------------- Edit
async def test_edit_queues_a_new_pending_action_and_closes_the_old_one_and_sends_nothing(settings):
    ex, db, notices, fsm, mail = make_executor(settings)
    old = ex.queue("email_send", "Quote for Dan", EMAIL)
    events = ex.bus.subscribe()
    new_id, message = ex.edit(old, {"subject": "Revised quote Q-1042", "to": "dan@kestrel.example.com, ops@kestrel.example.com"}, by="Alex")
    await drain(ex)
    assert new_id != old and "Nothing has been sent" in message
    before, after = db.get_action(old), db.get_action(new_id)
    assert before["status"] == "denied" and before["superseded_by"] == new_id and "Edited by Alex" in before["result"]
    assert before["payload"] == EMAIL                                   # the stored payload is never changed in place
    assert after["status"] == "pending" and after["approved_by"] == "" and after["supersedes"] == old
    assert after["supersede_kind"] == "edit" and after["summary"].endswith("(edited)")
    assert after["payload"]["subject"] == "Revised quote Q-1042" and after["payload"]["to"] == [
        "dan@kestrel.example.com", "ops@kestrel.example.com"]
    assert after["payload"]["body"] == EMAIL["body"] and after["payload"]["cc"] == []   # untouched fields kept exactly
    assert mail.sent == [] and notices.items == []                      # editing sends nothing
    assert [a["id"] for a in db.pending_actions()] == [new_id]
    kinds = []
    while not events.empty():
        kinds.append(events.get_nowait()["type"])
    assert "approvals" in kinds
    # the Teams cards for the OLD action were turned into "Edited", and the NEW one was offered
    assert ex.teams_approvals.decided == [(old, "Edited by Alex at 12:00")] and ex.teams_approvals.offered == [old, new_id]


async def test_the_old_action_can_no_longer_be_approved_and_the_edited_payload_is_what_runs(settings):
    ex, db, notices, fsm, mail = make_executor(settings)
    old = ex.queue("email_send", "Quote for Dan", EMAIL)
    new_id, _ = ex.edit(old, {"body": "Hi Dan, revised figures attached."}, by="Alex")
    stale = await ex.approve(old, by="Alex")
    assert "already denied" in stale
    await drain(ex)
    assert mail.sent == []                                              # approving the stale card did not send it
    assert "Approved action" in await ex.approve(new_id, by="Alex")
    await drain(ex)
    assert len(mail.sent) == 1
    to, subject, html, cc = mail.sent[0]
    assert to == EMAIL["to"] and subject == EMAIL["subject"] and "revised figures" in html and "Quote attached" not in html
    assert db.get_action(new_id)["status"] == "done"


async def test_edit_never_auto_runs_even_when_the_edited_payload_matches_a_standing_approval(settings):
    ex, db, notices, fsm, mail = make_executor(settings, record=False)
    base = {"method": "POST", "path": "/customers", "body": {"name": "Acme Alarms", "contact": "Pat"}}  # no created_by: never eligible
    old = ex.queue("fsm_write", "Create customer Acme Alarms", base)
    assert db.get_action(old)["status"] == "pending"
    settings.standing_record_keeping = True                             # the owner switches record keeping on
    eligible = {"name": "Acme Alarms", "created_by": "Jarvis", "contact": "Pat"}
    assert ex.standing.decide("fsm_write", {**base, "body": eligible}).category == "record keeping"   # it WOULD qualify
    new_id, _ = ex.edit(old, {"body": json.dumps(eligible)}, by="Alex")
    await drain(ex)
    assert db.get_action(new_id)["status"] == "pending" and db.get_action(new_id)["approved_by"] == ""
    assert fsm.writes == []                                             # nothing ran: it waits for a click
    await ex.approve(new_id, by="Alex")
    await drain(ex)
    assert len(fsm.writes) == 1 and db.get_action(new_id)["approved_by"] == "Alex"


async def test_edit_is_refused_for_anything_outside_the_closed_list_and_nothing_is_created(settings):
    ex, db, *_ = make_executor(settings)
    mail_id = ex.queue("email_send", "Quote", EMAIL)
    fsm_id = ex.queue("fsm_write", "Create", {"method": "POST", "path": "/customers", "body": {"name": "Acme"}})
    inv_id = ex.queue("sage_invoices", "Invoices", {"jobs": [{"job": "J1", "customer": "C", "net_value": 10, "site": "S"}]})
    cases = [
        (mail_id, {"management_only": True}, 422), (mail_id, {"kind": "fsm_write"}, 422), (mail_id, {}, 422),
        (mail_id, {"subject": "x\r\nBcc: attacker@evil.example"}, 422), (mail_id, {"subject": "  "}, 422),
        (mail_id, {"to": ""}, 422), (mail_id, {"to": "not-an-address"}, 422), (mail_id, {"to": "a@b.co, <x@y.co>"}, 422),
        (mail_id, {"body": "x" * 25000}, 422), (mail_id, {"body": "hidden‮flip"}, 422),
        (mail_id, {"body": f"keep [REDACTED] here"}, 422), (mail_id, {"subject": EMAIL["subject"]}, 422),   # nothing changed
        (mail_id, {"to": ",".join(f"u{i}@x.co" for i in range(30))}, 422),
        (fsm_id, {"path": "/jobs"}, 422), (fsm_id, {"method": "DELETE"}, 422), (fsm_id, {"body": "not json"}, 422),
        (fsm_id, {"body": "[1,2]"}, 422), (fsm_id, {"body": {"name": "x"}, "path": "/jobs"}, 422),
        (fsm_id, {"body": {"name": "[REDACTED]"}}, 422),
        (inv_id, {"jobs": []}, 422), (999, {"a": 1}, 404),
    ]
    for action_id, changes, status in cases:
        with pytest.raises(ActionRefused) as e:
            ex.edit(action_id, changes, by="Alex")
        assert e.value.status == status, (changes, str(e.value))
    assert sorted(a["id"] for a in db.pending_actions()) == sorted([mail_id, fsm_id, inv_id])   # nothing created, nothing closed
    assert db.get_action(mail_id)["payload"] == EMAIL


async def test_edit_of_an_action_that_is_already_decided_is_refused(settings):
    ex, db, notices, fsm, mail = make_executor(settings)
    a = ex.queue("email_send", "Quote", EMAIL)
    await ex.approve(a, by="Alex")
    await drain(ex)
    with pytest.raises(ActionRefused) as e:
        ex.edit(a, {"subject": "Too late"}, by="Alex")
    assert e.value.status == 409 and len(mail.sent) == 1 and not db.pending_actions()
    b = ex.queue("email_send", "Quote 2", EMAIL)
    await ex.deny(b, by="Alex")
    with pytest.raises(ActionRefused):
        ex.edit(b, {"subject": "Too late"}, by="Alex")


def test_two_edits_of_the_same_action_cannot_both_win(settings):
    ex, db, *_ = make_executor(settings)
    a = ex.queue("email_send", "Quote", EMAIL)
    first = db.supersede_pending_action(a, "email_send", "s", {**EMAIL, "subject": "One"}, "Alex")
    second = db.supersede_pending_action(a, "email_send", "s", {**EMAIL, "subject": "Two"}, "Sam")
    assert first is not None and second is None and len(db.pending_actions()) == 1
    with pytest.raises(ActionRefused):
        ex.edit(a, {"subject": "Three"}, by="Sam")


async def test_editing_a_tool_action_revalidates_against_the_tools_own_input_model(settings):
    ex, db, notices, fsm, mail = make_executor(settings)
    args = {**EMAIL, "management_only": True}
    a = ex.queue("tool:email_send", "Send the cash-flow summary", {"tool": "email_send", "args": args})
    with pytest.raises(ActionRefused):
        ex.edit(a, {"to": "nobody"}, by="Alex")
    new_id, _ = ex.edit(a, {"subject": "Cash flow - October"}, by="Alex")
    stored = db.get_action(new_id)["payload"]
    assert stored["tool"] == "email_send" and stored["args"]["management_only"] is True       # the flag survives the edit
    assert stored["args"]["subject"] == "Cash flow - October" and set(stored["args"]) == {"to", "subject", "body", "cc", "management_only"}
    await ex.approve(new_id, by="Alex")
    await drain(ex)
    assert mail.sent and mail.sent[0][1] == "Cash flow - October"
    # a generic tool: its arguments as JSON, validated by the tool's model
    m = ex.queue("tool:pr_merge", "Merge #3", {"tool": "pr_merge", "args": {"number": 3, "expected_head_sha": "abc123"}})
    with pytest.raises(ActionRefused):
        ex.edit(m, {"args": json.dumps({"number": "three"})}, by="Alex")           # fails the model's validation
    with pytest.raises(ActionRefused):
        ex.edit(m, {"tool": "pr_close"}, by="Alex")                                # the tool itself can't be swapped
    with pytest.raises(ActionRefused):
        ex.edit(m, {"args": json.dumps({"number": 3, "expected_head_sha": "abc123"})}, by="Alex")   # unchanged
    new_m, _ = ex.edit(m, {"args": json.dumps({"number": 4, "expected_head_sha": "abc123"})}, by="Alex")
    assert db.get_action(new_m)["payload"]["args"]["number"] == 4 and db.get_action(new_m)["payload"]["tool"] == "pr_merge"


def test_the_editor_cannot_name_itself_as_the_standing_approval(settings):
    ex, db, *_ = make_executor(settings)
    a = ex.queue("email_send", "Quote", EMAIL)
    new_id, _ = ex.edit(a, {"subject": "Hi"}, by="standing approval: record keeping")
    assert not db.get_action(a)["approved_by"].startswith("standing approval:")
    assert db.count_standing_runs_since("2000-01-01") == 0


# ----------------------------------------------------------------------------------------------------- Retry
async def failed_email(ex, db, mail):
    a = ex.queue("email_send", "Quote for Dan", EMAIL)
    mail.fail = True
    await ex.approve(a, by="Alex")
    await drain(ex)
    mail.fail = False
    assert db.get_action(a)["status"] == "failed" and "Mailbox unavailable" in db.get_action(a)["result"]
    return a


async def test_retry_queues_a_copy_for_approval_and_does_not_run_anything(settings):
    ex, db, notices, fsm, mail = make_executor(settings)
    a = await failed_email(ex, db, mail)
    n_notices = len(notices.items)
    new_id, message = ex.retry(a, by="Alex")
    await drain(ex)
    assert "Nothing has run" in message
    assert mail.sent == [] and len(notices.items) == n_notices           # the retry itself ran nothing
    new = db.get_action(new_id)
    assert new["status"] == "pending" and new["approved_by"] == "" and new["supersedes"] == a and new["supersede_kind"] == "retry"
    assert new["kind"] == "email_send" and new["payload"] == EMAIL and new["summary"].startswith(f"Retry of #{a}: ")
    old = db.get_action(a)
    assert old["status"] == "failed" and old["superseded_by"] == new_id    # history is kept
    assert [x["id"] for x in db.failed_actions()] == []                   # and it is no longer listed as needing a look
    assert ex.teams_approvals.offered == [a, new_id]                      # the retry was offered to Teams as a pending action
    # only a human click runs it
    await ex.approve(new_id, by="Alex")
    await drain(ex)
    assert len(mail.sent) == 1 and db.get_action(new_id)["status"] == "done"


async def test_a_failed_action_can_be_retried_only_once_and_only_when_failed(settings):
    ex, db, notices, fsm, mail = make_executor(settings)
    a = await failed_email(ex, db, mail)
    ex.retry(a, by="Alex")
    with pytest.raises(ActionRefused) as e:
        ex.retry(a, by="Alex")
    assert e.value.status == 409 and "already been retried" in str(e.value)
    assert len(db.pending_actions()) == 1                                   # the double click made no second copy
    pending = ex.queue("email_send", "Other", EMAIL)
    done = ex.queue("email_send", "Done one", EMAIL)
    await ex.approve(done, by="Alex")
    await drain(ex)
    denied = ex.queue("email_send", "Denied one", EMAIL)
    await ex.deny(denied, by="Alex")
    for action_id in (pending, done, denied):
        with pytest.raises(ActionRefused) as e:
            ex.retry(action_id, by="Alex")
        assert e.value.status == 409
    with pytest.raises(ActionRefused) as e:
        ex.retry(12345, by="Alex")
    assert e.value.status == 404


async def test_a_retry_that_fails_again_can_be_retried_again_each_time_needing_a_click(settings):
    ex, db, notices, fsm, mail = make_executor(settings, fsm_fail=True)
    a = ex.queue("fsm_write", "Create site", {"method": "POST", "path": "/sites", "body": {"name": "Unit 4"}})
    await ex.approve(a, by="Alex")
    await drain(ex)
    assert db.get_action(a)["status"] == "failed" and "name already exists" in db.get_action(a)["result"]
    second, _ = ex.retry(a, by="Alex")
    assert len(fsm.writes) == 1                                             # no silent re-run
    await ex.approve(second, by="Alex")
    await drain(ex)
    assert len(fsm.writes) == 2 and db.get_action(second)["status"] == "failed"
    third, _ = ex.retry(second, by="Alex")
    assert len(fsm.writes) == 2 and db.get_action(third)["summary"] == f"Retry of #{second}: Create site"   # no prefix pile-up
    assert db.failed_actions() == [] and db.get_action(second)["superseded_by"] == third


async def test_a_retry_never_auto_runs_even_when_a_standing_approval_covers_the_payload(settings):
    ex, db, notices, fsm, mail = make_executor(settings, record=True, fsm_fail=True)
    record = {"method": "POST", "path": "/customers", "body": {"name": "Acme Alarms", "created_by": "Jarvis"}}
    a = ex.queue("fsm_write", "Create customer", record)                    # runs automatically under the owner's switch...
    await drain(ex)
    assert db.get_action(a)["status"] == "failed" and db.get_action(a)["approved_by"].startswith("standing approval:")
    assert len(fsm.writes) == 1
    new_id, _ = ex.retry(a, by="Alex")
    await drain(ex)
    assert db.get_action(new_id)["status"] == "pending" and len(fsm.writes) == 1     # ...but its retry waits for a person
    assert ex.standing.decide("fsm_write", record).category == "record keeping"      # (it would have qualified)


# ----------------------------------------------------------------------------------------------------- the web layer
def app_for(tmp_path, **kw):
    s = Settings(data_dir=tmp_path / "data", scheduler_enabled=False, _env_file=None, anthropic_api_key="test", **kw)
    j = Jarvis(s, client=FakeClient())
    j.actions.mail = FakeMail()
    j.mail = j.actions.mail
    return s, j, create_app(s, j)


def seed(j):
    pending = j.actions.queue("email_send", "Quote for Dan", EMAIL)
    failed = j.db.create_action("fsm_write", "Create site", {"method": "POST", "path": "/sites", "body": {"name": "Unit 4"}})
    j.db.set_action_status(failed, "failed", f"Salts FSM did not accept the change (HTTP 422) key {SECRET}")
    return pending, failed


def test_the_inbox_lists_pending_and_failed_actions_with_their_error_and_a_retry_flag(tmp_path):
    s, j, app = app_for(tmp_path)
    pending, failed = seed(j)
    with TestClient(app) as c:
        data = c.get("/api/approvals/inbox").json()
        assert [a["id"] for a in data["pending"]] == [pending] and data["pending"][0]["editable_fields"]
        (f,) = data["failed"]
        assert f["id"] == failed and f["can_retry"] is True and "HTTP 422" in f["error"] and f["status"] == "failed"
        assert SECRET not in json.dumps(data)
        for path in ("/api/approvals", "/api/status"):                      # the other ways the console loads them: redacted too
            assert SECRET not in c.get(path).text
        # retry through the API: the failed card is replaced by a pending one
        r = c.post(f"/api/approvals/{failed}/retry")
        assert r.status_code == 200 and "Nothing has run" in r.json()["result"]
        after = c.get("/api/approvals/inbox").json()
        assert [a["id"] for a in after["pending"]] == [pending, r.json()["id"]] and after["failed"] == []
        assert c.post(f"/api/approvals/{failed}/retry").status_code == 409


def test_edit_endpoint_flow_end_to_end(tmp_path):
    s, j, app = app_for(tmp_path)
    pending, _ = seed(j)
    with TestClient(app) as c:
        r = c.post(f"/api/approvals/{pending}/edit", json={"changes": {"body": "Hi Dan - revised."}})
        assert r.status_code == 200
        new_id = r.json()["id"]
        assert c.post(f"/api/approvals/{pending}/edit", json={"changes": {"body": "again"}}).status_code == 409
        assert c.post(f"/api/approvals/{pending}/approve").json()["result"].startswith(f"Action #{pending}")
        assert j.mail.sent == []                                            # approving the replaced card sent nothing
        assert c.post(f"/api/approvals/{new_id}/edit", json={"changes": {"to": "bad"}}).status_code == 422
        assert c.post(f"/api/approvals/{new_id}/edit", json={"changes": {"path": "/x"}}).status_code == 422
        assert c.post(f"/api/approvals/{new_id}/edit", json={}).status_code == 422                   # body is required
        assert c.post(f"/api/approvals/{new_id}/edit", json={"changes": {}}).status_code == 422
        assert c.post("/api/approvals/9999/edit", json={"changes": {"a": 1}}).status_code == 404
        assert c.post(f"/api/approvals/{new_id}/approve").status_code == 200


def test_every_approval_endpoint_needs_the_owners_session(tmp_path):
    s, j, app = app_for(tmp_path, jarvis_owner_password="a-long-password")
    pending, failed = seed(j)
    calls = [("get", "/api/approvals/inbox", None), ("get", "/api/approvals", None),
             ("post", f"/api/approvals/{pending}/approve", None), ("post", f"/api/approvals/{pending}/deny", None),
             ("post", f"/api/approvals/{pending}/edit", {"changes": {"subject": "Hi"}}),
             ("post", f"/api/approvals/{failed}/retry", None), ("get", "/api/memory", None),
             ("post", "/api/memory/facts/1", {"text": "hello there"}), ("delete", "/api/memory/facts/1", None),
             ("post", "/api/memory/replies/1", {"text": "yes"}), ("delete", "/api/memory/replies/1", None)]
    with TestClient(app, client=("203.0.113.5", 5000)) as c:                # not the local machine
        for method, path, body in calls:
            r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
            assert r.status_code == 401, (method, path, r.status_code)
        c.cookies.set(auth.COOKIE, "1.forged-signature")
        for method, path, body in calls:
            r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
            assert r.status_code == 401, (method, path, r.status_code)
        c.cookies.set(auth.COOKIE, auth.make_session(s))                    # the owner: fine
        assert c.get("/api/approvals/inbox").status_code == 200
    assert j.db.get_action(pending)["status"] == "pending" and j.db.get_action(failed)["superseded_by"] is None


def test_a_cross_site_request_is_refused_even_with_a_valid_session(tmp_path):
    s, j, app = app_for(tmp_path, jarvis_owner_password="a-long-password", public_base_url="https://jarvis.example.test")
    pending, failed = seed(j)
    with TestClient(app, base_url="https://jarvis.example.test") as c:
        c.cookies.set(auth.COOKIE, auth.make_session(s))
        evil = [{"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}, {"Origin": "https://evil.example.org"},
                {"Origin": "null"}, {"Origin": "https://jarvis.example.test.evil.org"}]
        for headers in evil:
            for path, body in ((f"/api/approvals/{pending}/approve", None), (f"/api/approvals/{pending}/deny", None),
                               (f"/api/approvals/{pending}/edit", {"changes": {"subject": "x"}}),
                               (f"/api/approvals/{failed}/retry", None), ("/api/memory/facts/1", {"text": "hello world"})):
                r = c.post(path, headers=headers, **({"json": body} if body is not None else {}))
                assert r.status_code == 403, (headers, path, r.status_code)
        assert c.delete("/api/memory/facts/1", headers={"Origin": "https://evil.example.org"}).status_code == 403
        assert j.db.get_action(pending)["status"] == "pending" and j.db.get_action(failed)["superseded_by"] is None
        # the console itself (same origin) and plain API clients (no browser headers) work
        assert c.post(f"/api/approvals/{pending}/edit", json={"changes": {"subject": "ok"}},
                      headers={"Sec-Fetch-Site": "same-origin", "Origin": "https://jarvis.example.test"}).status_code == 200
        assert c.post(f"/api/approvals/{failed}/retry").status_code == 200


def test_a_signed_in_manager_may_edit_and_retry_and_is_named_in_the_record(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "true")
    s, j, app = app_for(tmp_path, jarvis_owner_password="a-long-password", owner_email="alex@salts.example.com",
                        manager_emails="alex@salts.example.com,sam@salts.example.com")
    pending, failed = seed(j)
    sso = {"x-ms-client-principal-idp": "aad", "x-ms-client-principal-name": "sam@salts.example.com"}
    with TestClient(app, client=("203.0.113.5", 5000)) as c:
        r = c.post(f"/api/approvals/{pending}/edit", json={"changes": {"subject": "Sam's edit"}}, headers=sso)
        assert r.status_code == 200
        assert c.post(f"/api/approvals/{failed}/retry", headers=sso).status_code == 200
    assert j.db.get_action(pending)["approved_by"] == "sam@salts.example.com"
    assert "sam@salts.example.com" in j.db.get_action(pending)["result"]


# ----------------------------------------------------------------------------------------------------- the model has no way in
def _src(rel: str) -> str:
    return (Path(jarvis.__file__).parent / rel).read_text(encoding="utf-8")


def test_no_brain_tool_can_edit_retry_approve_or_deny():
    for t in TOOLS:
        assert not re.search(r"edit_action|retry|supersede|approv|deny|memory_book", t.name, re.I), t.name
        assert not [f for f in t.model.model_fields if re.search(r"supersede|retry|approv|decision", f, re.I)], t.name
    root = Path(jarvis.__file__).parent
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        # only the executor itself (definitions) and the web layer may call these
        if re.search(r"\.(edit|retry)\(\s*[A-Za-z_]+\s*,", text) and re.search(r"actions\.(edit|retry)\(", text) and rel != "main.py":
            pytest.fail(f"{rel} calls actions.edit()/retry() - only the owner-authenticated web layer may")
        if re.search(r"(supersede_pending_action|retry_failed_action)\(", text) and rel not in {"db.py", "services/actions.py"}:
            pytest.fail(f"{rel} touches the supersede/retry queue primitives")
        if re.search(r"MemoryBook\(|edit_fact|delete_fact|edit_reply|delete_reply", text) and rel not in {
                "main.py", "services/memory_book.py"}:
            pytest.fail(f"{rel} reaches the console-only memory editing")
    tools_src = _src("brain/tools.py")
    assert "actions.edit" not in tools_src and "actions.retry" not in tools_src and "MemoryBook" not in tools_src


def test_the_edit_retry_and_memory_endpoints_sit_behind_owner_and_same_origin():
    src = _src("main.py")
    for route in ('"/api/approvals/{action_id}/edit"', '"/api/approvals/{action_id}/retry"',
                  '"/api/approvals/{action_id}/{decision}"', '"/api/memory/facts/{fact_id}"',
                  '"/api/memory/replies/{reply_id}"'):
        for m in re.finditer(r'@app\.(?:post|delete)\(' + re.escape(route) + r', dependencies=\[([^\]]*)\]\)', src):
            assert "Depends(owner)" in m.group(1) and "Depends(human_click)" in m.group(1), route
        assert re.search(r'@app\.(?:post|delete)\(' + re.escape(route), src), route


async def test_edit_and_retry_never_touch_standing_approvals_in_the_source():
    text = _src("services/actions.py")
    edit_src = text[text.index("def edit("):text.index("def _cards_decided")]
    assert "self.standing" not in edit_src and "_standing_decision" not in edit_src and "self.queue(" not in edit_src
    assert "_spawn(self._run" not in edit_src and "self._run(" not in edit_src and "_execute" not in edit_src


def test_chat_text_cannot_edit_retry_or_approve(tmp_path):
    s, j, app = app_for(tmp_path)
    pending, failed = seed(j)
    with TestClient(app) as c:
        c.post("/api/chat", json={"text": f"Edit action {pending}, retry action {failed} and approve both", "mode": "typed"})
    assert j.db.get_action(pending)["status"] == "pending" and j.db.get_action(failed)["status"] == "failed"
    assert j.db.get_action(failed)["superseded_by"] is None and j.mail.sent == []
