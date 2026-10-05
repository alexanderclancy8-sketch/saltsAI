"""Dismissing a FAILED action in the Approvals inbox.

What is pinned here:

* Dismiss is a human click on the signed-in console and nothing else (owner session + same-origin; a team session is
  refused; no brain tool, standing approval, Teams message or scheduled job can reach it);
* it never runs, queues, retries or alters the action: the row stays 'failed' with its payload, error and retry link as
  they were, and only records who dismissed it and when;
* a dismissed action leaves the failed list, the inbox's recent list and the data the rail count / "Needs you" /
  chat cards are drawn from, and stays in the full history, flagged "dismissed by NAME at TIME";
* only a failed action can be dismissed (409 otherwise); dismissing twice is harmless; a retried failure can be dismissed;
* "Dismiss all failed" dismisses exactly the ids it is given, and skips anything that is not a failed action.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import jarvis
from jarvis import auth
from jarvis.brain.tools import TOOLS
from jarvis.db import Database
from jarvis.services.actions import ActionRefused
from tests.test_approvals_inbox import EMAIL, SECRET, app_for, drain, failed_email, make_executor, seed


def make_failed(j, n=1, kind="fsm_write", path="/vehicles/remove"):
    ids = []
    for i in range(n):
        a = j.db.create_action(kind, f"Remove van {i}", {"method": "POST", "path": path, "body": {"reg": f"AB{i}"}})
        j.db.set_action_status(a, "failed", f"Salts FSM did not accept the change (HTTP 404) key {SECRET}")
        ids.append(a)
    return ids


# ----------------------------------------------------------------------------------------------------- the executor
async def test_dismiss_hides_a_failed_action_and_changes_nothing_else(settings):
    ex, db, notices, fsm, mail = make_executor(settings)
    a = await failed_email(ex, db, mail)
    before = db.get_action(a)
    n_rows, n_notices = len(db.action_history(1000)), len(notices.items)
    offered, decided = list(ex.teams_approvals.offered), list(ex.teams_approvals.decided)
    newly, message = ex.dismiss(a, by="Alex")
    await drain(ex)
    after = db.get_action(a)
    assert newly is True and "Dismissed action" in message
    assert after["dismissed_by"] == "Alex" and after["dismissed_at"]
    for key in ("status", "result", "payload", "kind", "summary", "approved_by", "decided_at", "created_at",
                "superseded_by", "supersedes", "supersede_kind"):
        assert after[key] == before[key], key                       # the failure's own record is untouched
    assert len(db.action_history(1000)) == n_rows                   # nothing was queued
    assert mail.sent == [] and fsm.writes == [] and len(notices.items) == n_notices
    assert ex.teams_approvals.offered == offered and ex.teams_approvals.decided == decided   # Teams is not involved


async def test_a_dismissed_action_leaves_the_lists_but_stays_in_the_history(settings):
    ex, db, _, _, mail = make_executor(settings)
    a = await failed_email(ex, db, mail)
    other = db.create_action("fsm_write", "Create site", {"method": "POST", "path": "/sites", "body": {"name": "X"}})
    db.set_action_status(other, "failed", "boom")
    assert [r["id"] for r in db.failed_actions("")] == [other, a]
    ex.dismiss(a, by="Alex")
    assert [r["id"] for r in db.failed_actions("")] == [other]
    assert a not in [r["id"] for r in db.recent_decided_actions("")]
    assert db.dismissed_action_ids("") == [a]
    assert [r["id"] for r in db.action_history(100)] == [other, a]               # still in the full record
    assert [r["id"] for r in db.action_history(100, dismissed_only=True)] == [a]


async def test_only_a_failed_action_can_be_dismissed(settings):
    ex, db, _, fsm, mail = make_executor(settings)
    pending = ex.queue("email_send", "Quote", EMAIL)
    approved = db.create_action("email_send", "x", EMAIL, status="approved")
    done = ex.queue("email_send", "Quote 2", EMAIL)
    await ex.approve(done, by="Alex")
    await drain(ex)
    denied = ex.queue("email_send", "Quote 3", EMAIL)
    await ex.deny(denied, by="Alex")
    assert db.get_action(done)["status"] == "done"
    for action_id, status in ((pending, "pending"), (approved, "approved"), (done, "done"), (denied, "denied")):
        with pytest.raises(ActionRefused) as e:
            ex.dismiss(action_id, by="Alex")
        assert e.value.status == 409 and "nothing to dismiss" in str(e.value)
        row = db.get_action(action_id)
        assert row["status"] == status and not row["dismissed_at"] and not row["dismissed_by"]
    with pytest.raises(ActionRefused) as e:
        ex.dismiss(12345, by="Alex")
    assert e.value.status == 404
    assert db.dismiss_failed_action(pending, "x") is False and db.get_action(pending)["dismissed_at"] is None


async def test_dismissing_twice_is_harmless_and_keeps_the_first_dismissal(settings):
    ex, db, _, _, mail = make_executor(settings)
    a = await failed_email(ex, db, mail)
    assert ex.dismiss(a, by="Alex")[0] is True
    first = db.get_action(a)
    newly, message = ex.dismiss(a, by="Sam")
    assert newly is False and "already dismissed by Alex" in message
    again = db.get_action(a)
    assert (again["dismissed_by"], again["dismissed_at"]) == (first["dismissed_by"], first["dismissed_at"]) == ("Alex", first["dismissed_at"])


async def test_a_retried_failure_can_still_be_dismissed_and_a_dismissed_one_can_not_be_retried(settings):
    ex, db, _, fsm, mail = make_executor(settings)
    a = await failed_email(ex, db, mail)
    new_id, _ = ex.retry(a, by="Alex")
    assert db.get_action(a)["superseded_by"] == new_id
    assert ex.dismiss(a, by="Alex")[0] is True                                    # already retried: still dismissable
    kept = db.get_action(a)
    assert kept["status"] == "failed" and kept["superseded_by"] == new_id          # the retry link is unchanged
    assert db.get_action(new_id)["status"] == "pending"                           # and the retry itself is untouched
    b = await failed_email(ex, db, mail)
    ex.dismiss(b, by="Alex")
    with pytest.raises(ActionRefused) as e:
        ex.retry(b, by="Alex")
    assert e.value.status == 409 and "dismissed by Alex" in str(e.value)
    assert db.get_action(b)["superseded_by"] is None and db.retry_failed_action(b, "x") is None
    assert mail.sent == [] and fsm.writes == []


async def test_dismiss_many_skips_what_is_not_failed_and_does_not_double_count(settings):
    ex, db, *_ = make_executor(settings)
    f1, f2, f3 = (db.create_action("fsm_write", f"w{i}", {"method": "POST", "path": "/x"}) for i in range(3))
    for a in (f1, f2, f3):
        db.set_action_status(a, "failed", "boom")
    pending = ex.queue("email_send", "Quote", EMAIL)
    ex.dismiss(f3, by="Alex")
    got = ex.dismiss_many([f1, f2, f1, f3, pending, 9999], by="Alex")
    assert got == {"dismissed": [f1, f2], "skipped": [f3, pending, 9999]}
    assert db.get_action(pending)["dismissed_at"] is None and db.get_action(pending)["status"] == "pending"


def test_an_old_database_gets_the_dismissal_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE pending_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, kind TEXT NOT NULL,"
                 " summary TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', result TEXT DEFAULT '',"
                 " decided_at TEXT DEFAULT '')")
    conn.execute("INSERT INTO pending_actions (created_at, kind, summary, payload_json, status, decided_at) VALUES"
                 " ('2026-01-01T00:00:00+00:00','fsm_write','old','{}','failed','2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert [r["id"] for r in db.failed_actions("")] == [1]
    assert db.dismiss_failed_action(1, "Alex") is True and db.failed_actions("") == []
    Database(path)                                                                 # reopening is safe


# ----------------------------------------------------------------------------------------------------- the web layer
def test_dismiss_endpoint_hides_the_card_everywhere_but_the_history(tmp_path):
    s, j, app = app_for(tmp_path)
    pending, failed = seed(j)
    with TestClient(app) as c:
        before = c.get("/api/approvals/inbox").json()
        assert [f["id"] for f in before["failed"]] == [failed] and before["failed"][0]["can_dismiss"] is True
        r = c.post(f"/api/approvals/{failed}/dismiss")
        assert r.status_code == 200 and r.json()["dismissed"] is True and r.json()["already"] is False
        after = c.get("/api/approvals/inbox").json()
        assert after["failed"] == [] and [p["id"] for p in after["pending"]] == [pending]   # counts, Needs you, chat cards
        assert failed not in [v["id"] for v in after["recent"]] and after["dismissed_ids"] == [failed]
        # nothing else moved: still failed, payload intact, nothing queued, nothing sent
        row = j.db.get_action(failed)
        assert row["status"] == "failed" and row["payload"]["path"] == "/sites" and row["superseded_by"] is None
        assert j.mail.sent == [] and len(j.db.pending_actions()) == 1
        assert failed not in [a["id"] for a in c.get("/api/approvals").json()]
        assert SECRET not in json.dumps(after) and SECRET not in c.get("/api/status").text


def test_dismissed_actions_are_in_the_history_flagged_with_who_and_when(tmp_path):
    s, j, app = app_for(tmp_path)
    pending, failed = seed(j)
    with TestClient(app) as c:
        c.post(f"/api/approvals/{failed}/dismiss")
        rows = {v["id"]: v for v in c.get("/api/approvals/history").json()}
        assert set(rows) == {pending, failed}
        d = rows[failed]
        assert d["dismissed"] is True and d["status"] == "failed" and d["can_dismiss"] is False and d["can_retry"] is False
        assert re.fullmatch(r"dismissed by the owner at \d{4}-\d\d-\d\d \d\d:\d\d UTC", d["dismissed_label"]), d["dismissed_label"]
        assert d["dismissed_by"] == "the owner" and d["dismissed_at"] and "HTTP 422" in d["error"]
        assert rows[pending]["dismissed"] is False and rows[pending]["dismissed_label"] == ""
        only = c.get("/api/approvals/history?dismissed=true").json()
        assert [v["id"] for v in only] == [failed]
        assert SECRET not in c.get("/api/approvals/history").text                      # the history is redacted too
        assert c.get("/api/approvals/history?limit=0").status_code == 200 and len(c.get("/api/approvals/history?limit=1").json()) == 1


def test_dismiss_endpoint_refuses_anything_that_has_not_failed_and_is_idempotent(tmp_path):
    s, j, app = app_for(tmp_path)
    pending, failed = seed(j)
    with TestClient(app) as c:
        r = c.post(f"/api/approvals/{pending}/dismiss")
        assert r.status_code == 409 and "nothing to dismiss" in r.json()["detail"]
        assert j.db.get_action(pending)["status"] == "pending" and j.db.get_action(pending)["dismissed_at"] is None
        assert c.post("/api/approvals/9999/dismiss").status_code == 404
        assert c.post(f"/api/approvals/{failed}/dismiss").status_code == 200
        stamp = j.db.get_action(failed)["dismissed_at"]
        again = c.post(f"/api/approvals/{failed}/dismiss")
        assert again.status_code == 200 and again.json()["already"] is True
        assert j.db.get_action(failed)["dismissed_at"] == stamp
        assert c.post(f"/api/approvals/{failed}/retry").status_code == 409            # a dismissed failure is not retryable
        assert j.db.get_action(failed)["superseded_by"] is None and len(j.db.pending_actions()) == 1


def test_a_retried_failure_can_be_dismissed_through_the_api(tmp_path):
    s, j, app = app_for(tmp_path)
    pending, failed = seed(j)
    with TestClient(app) as c:
        new_id = c.post(f"/api/approvals/{failed}/retry").json()["id"]
        assert c.post(f"/api/approvals/{failed}/dismiss").status_code == 200
        assert j.db.get_action(failed)["superseded_by"] == new_id and j.db.get_action(new_id)["status"] == "pending"
        assert c.post(f"/api/approvals/{new_id}/dismiss").status_code == 409          # the copy is pending, not failed


def test_dismiss_all_dismisses_exactly_the_ids_given(tmp_path):
    s, j, app = app_for(tmp_path)
    pending, failed = seed(j)
    more = make_failed(j, 3)
    with TestClient(app) as c:
        shown = [f["id"] for f in c.get("/api/approvals/inbox").json()["failed"]]
        assert sorted(shown) == sorted([failed, *more])
        r = c.post("/api/approvals/dismiss-failed", json={"ids": shown + [pending, 9999, shown[0]]})
        assert r.status_code == 200
        assert sorted(r.json()["dismissed"]) == sorted(shown) and r.json()["skipped"] == [pending, 9999]
        data = c.get("/api/approvals/inbox").json()
        assert data["failed"] == [] and [p["id"] for p in data["pending"]] == [pending]
        assert j.db.get_action(pending)["dismissed_at"] is None
        assert sorted(v["id"] for v in c.get("/api/approvals/history?dismissed=1").json()) == sorted(shown)
        # a new failure after the confirm is NOT swept up: only what was shown is dismissed
        late = make_failed(j, 1)[0]
        assert c.post("/api/approvals/dismiss-failed", json={"ids": shown}).json() == {"dismissed": [], "skipped": shown}
        assert [f["id"] for f in c.get("/api/approvals/inbox").json()["failed"]] == [late]
        for bad in ({}, {"ids": []}, {"ids": "all"}, {"ids": [1] * 201}, {"ids": ["x"]}):
            assert c.post("/api/approvals/dismiss-failed", json=bad).status_code == 422, bad
        assert [f["id"] for f in c.get("/api/approvals/inbox").json()["failed"]] == [late]


def test_dismiss_endpoints_need_the_owners_session_and_a_same_origin_click(tmp_path):
    s, j, app = app_for(tmp_path, jarvis_owner_password="a-long-password", public_base_url="https://jarvis.example.test")
    pending, failed = seed(j)
    calls = [("post", f"/api/approvals/{failed}/dismiss", None), ("post", "/api/approvals/dismiss-failed", {"ids": [failed]}),
             ("get", "/api/approvals/history", None)]
    def go(c, method, path, body, **kw):
        return getattr(c, method)(path, **({"json": body} if body is not None else {}), **kw)

    with TestClient(app, base_url="https://jarvis.example.test", client=("203.0.113.5", 5000)) as c:   # not the local machine
        for method, path, body in calls:
            assert go(c, method, path, body).status_code == 401, path
        c.cookies.set(auth.COOKIE, "1.forged-signature")
        for method, path, body in calls:
            assert go(c, method, path, body).status_code == 401, path
        assert j.db.get_action(failed)["dismissed_at"] is None
        c.cookies.set(auth.COOKIE, auth.make_session(s))
        evil = [{"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}, {"Origin": "https://evil.example.org"},
                {"Origin": "null"}, {"Origin": "https://jarvis.example.test.evil.org"}]
        for headers in evil:
            for method, path, body in calls[:2]:
                assert go(c, method, path, body, headers=headers).status_code == 403, (headers, path)
        assert j.db.get_action(failed)["dismissed_at"] is None
        ok = {"Sec-Fetch-Site": "same-origin", "Origin": "https://jarvis.example.test"}
        assert go(c, "post", f"/api/approvals/{failed}/dismiss", None, headers=ok).status_code == 200
        assert go(c, "get", "/api/approvals/history", None).status_code == 200
    assert j.db.get_action(failed)["status"] == "failed"


def test_a_signed_in_manager_may_dismiss_and_is_named_in_the_record(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "true")
    s, j, app = app_for(tmp_path, jarvis_owner_password="a-long-password", owner_email="alex@salts.example.com",
                        manager_emails="alex@salts.example.com,sam@salts.example.com")
    pending, failed = seed(j)
    sso = {"x-ms-client-principal-idp": "aad", "x-ms-client-principal-name": "sam@salts.example.com"}
    with TestClient(app, client=("203.0.113.5", 5000)) as c:
        assert c.post(f"/api/approvals/{failed}/dismiss", headers=sso).status_code == 200
        (row,) = [v for v in c.get("/api/approvals/history", headers=sso).json() if v["id"] == failed]
    assert j.db.get_action(failed)["dismissed_by"] == "sam@salts.example.com"
    assert row["dismissed_label"].startswith("dismissed by sam@salts.example.com at ")


def test_a_standing_approval_name_cannot_be_used_as_the_dismisser(settings):
    ex, db, *_ = make_executor(settings)
    a = db.create_action("fsm_write", "w", {"method": "POST", "path": "/x"})
    db.set_action_status(a, "failed", "boom")
    ex.dismiss(a, by="standing approval: record keeping")
    assert not db.get_action(a)["dismissed_by"].startswith("standing approval")


# ----------------------------------------------------------------------------------------------------- the model has no way in
def _src(rel: str) -> str:
    return (Path(jarvis.__file__).parent / rel).read_text(encoding="utf-8")


def test_no_brain_tool_can_dismiss_and_only_the_web_layer_calls_it():
    for t in TOOLS:
        assert not re.search(r"dismiss", t.name, re.I), t.name
        assert not [f for f in t.model.model_fields if re.search(r"dismiss", f, re.I)], t.name
    root = Path(jarvis.__file__).parent
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        if re.search(r"actions\.(dismiss|dismiss_many)\(", text) and rel != "main.py":
            pytest.fail(f"{rel} calls actions.dismiss() - only the owner-authenticated web layer may")
        if re.search(r"\bdismiss_failed_action\(", text) and rel not in {"db.py", "services/actions.py"}:
            pytest.fail(f"{rel} touches the dismiss primitive")
        if re.search(r"\.dismiss\(", text) and rel not in {"main.py", "services/actions.py"}:
            pytest.fail(f"{rel} calls .dismiss()")
    tools_src = _src("brain/tools.py")
    assert "actions.dismiss" not in tools_src and "dismiss_failed_action" not in tools_src
    # the Teams card path surfaces pending actions only; it never lists or dismisses a failed one
    assert "dismiss" not in _src("services/teams_approvals.py")


def test_the_dismiss_endpoints_sit_behind_owner_and_same_origin():
    src = _src("main.py")
    for route in ('"/api/approvals/{action_id}/dismiss"', '"/api/approvals/dismiss-failed"'):
        found = list(re.finditer(r'@app\.post\(' + re.escape(route) + r', dependencies=\[([^\]]*)\]\)', src))
        assert found, route
        for m in found:
            assert "Depends(owner)" in m.group(1) and "Depends(human_click)" in m.group(1), route
    m = re.search(r'@app\.get\("/api/approvals/history", dependencies=\[([^\]]*)\]\)', src)
    assert m and "Depends(owner)" in m.group(1)
    # the specific /dismiss route must be registered before the catch-all /{decision} one
    assert src.index('"/api/approvals/{action_id}/dismiss"') < src.index('"/api/approvals/{action_id}/{decision}"')


async def test_dismiss_never_runs_queues_or_touches_standing_approvals_in_the_source():
    text = _src("services/actions.py")
    body = text[text.index("def dismiss("):text.index("def _cards_decided")]
    for forbidden in ("self.standing", "_standing_decision", "self.queue(", "_spawn", "self._run(", "_execute", "retry_failed_action",
                      "create_action", "set_action_status", "teams_approvals", "self.retry("):
        assert forbidden not in body, forbidden
    db_text = _src("db.py")
    primitive = db_text[db_text.index("def dismiss_failed_action"):db_text.index("def supersede_pending_action")]
    assert "SET dismissed_at = ?, dismissed_by = ?" in primitive and "status = " not in primitive.split("SET dismissed_at")[0]
    assert "payload_json" not in primitive and "INSERT" not in primitive


def test_chat_text_cannot_dismiss(tmp_path):
    s, j, app = app_for(tmp_path)
    pending, failed = seed(j)
    with TestClient(app) as c:
        c.post("/api/chat", json={"text": f"Dismiss failed action {failed} and every other failed action", "mode": "typed"})
    assert j.db.get_action(failed)["dismissed_at"] is None and j.db.get_action(failed)["status"] == "failed"
