"""Marking issues resolved (and reopening them): the service, the console endpoints + button, and the issue_resolve
tool. None of it goes through the approval gate - these tests also check nothing lands in the approvals queue."""
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.brain.tools import TOOLS, TOOLS_BY_NAME, IssueResolveIn, dispatch
from jarvis.core import Jarvis
from jarvis.db import Database
from jarvis.main import create_app
from tests.fakes import FakeClient, message, text_block, tool_block

WEB = Path(__file__).resolve().parent.parent / "jarvis" / "web"


def make(settings, script=None):
    return Jarvis(settings, client=FakeClient(script))


def new_issue(j, title="Photos hang", status=None):
    issue_id = j.db.create_issue(reporter="Sam", title=title, description="spinner", source="web")
    if status:
        j.db.update_issue(issue_id, status=status)
    return issue_id


# ------------------------------------------------------------------ database / service
def test_old_issues_table_gains_the_resolved_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE issues (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,"
                 " updated_at TEXT NOT NULL, reporter TEXT NOT NULL, reporter_email TEXT DEFAULT '',"
                 " source TEXT NOT NULL, system TEXT DEFAULT 'Salts FSM', severity TEXT DEFAULT 'medium',"
                 " title TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',"
                 " triage_json TEXT DEFAULT '', fix_pr_url TEXT DEFAULT '', fix_pr_number INTEGER,"
                 " fix_branch TEXT DEFAULT '', image_path TEXT DEFAULT '', notes TEXT DEFAULT '')")
    conn.commit()
    conn.close()
    db = Database(path)
    issue_id = db.create_issue(reporter="Sam", title="t", description="d", source="web")
    db.resolve_issue(issue_id, by="Alex", note="done")
    row = db.get_issue(issue_id)
    assert row["status"] == "resolved" and row["resolved_by"] == "Alex" and row["resolved_at"]


async def test_mark_resolved_records_status_note_who_and_when(settings):
    j = make(settings)
    issue_id = new_issue(j, status="triaged")
    out = j.issues.mark_resolved(issue_id, by="Alex", note="  Fixed on site  ")
    row = j.db.get_issue(issue_id)
    assert out["status"] == row["status"] == "resolved"
    assert row["notes"] == "Fixed on site"
    assert row["resolved_by"] == "Alex" and row["resolved_at"].startswith("20")
    assert j.db.list_issues("open") == [] and [i["id"] for i in j.db.list_issues("resolved")] == [issue_id]
    assert j.db.pending_actions() == []  # not an approval, and it queues nothing
    await j.http.aclose()


async def test_mark_resolved_rejects_unknown_and_already_closed(settings):
    j = make(settings)
    with pytest.raises(LookupError):
        j.issues.mark_resolved(999, by="Alex")
    done = new_issue(j, status="resolved")
    wont = new_issue(j, title="Other", status="wont_fix")
    for issue_id in (done, wont):
        with pytest.raises(ValueError):
            j.issues.mark_resolved(issue_id, by="Alex", note="again")
    assert j.db.get_issue(done)["notes"] == "" and j.db.get_issue(done)["resolved_by"] == ""
    await j.http.aclose()


async def test_reopen_puts_it_back_to_open_and_clears_who_when(settings):
    j = make(settings)
    issue_id = new_issue(j)
    j.issues.mark_resolved(issue_id, by="Alex", note="Fixed")
    out = j.issues.reopen(issue_id, by="Sam")
    assert out["status"] == "open" and out["resolved_by"] == "" and out["resolved_at"] == ""
    assert "Reopened by Sam" in out["notes"] and "Alex" in out["notes"] and "Fixed" in out["notes"]
    assert [i["id"] for i in j.db.list_issues("open")] == [issue_id]
    with pytest.raises(ValueError):
        j.issues.reopen(issue_id, by="Sam")  # it isn't resolved any more
    with pytest.raises(LookupError):
        j.issues.reopen(999, by="Sam")
    await j.http.aclose()


# ------------------------------------------------------------------ console endpoints
def test_resolve_and_reopen_endpoints(settings):
    settings.jarvis_owner_password = "pw"
    settings.manager_emails = "partner@example.com"
    settings.partner_email, settings.partner_name = "partner@example.com", "Sam"
    j = make(settings)
    issue_id = new_issue(j, status="triaged")
    app = create_app(settings, j)
    with TestClient(app) as c:
        # signed out: nothing happens
        assert c.post(f"/api/issues/{issue_id}/resolve", json={"note": "x"}).status_code == 401
        assert c.post(f"/api/issues/{issue_id}/reopen").status_code == 401
        assert j.db.get_issue(issue_id)["status"] == "triaged"

        c.post("/login", data={"password": "pw"}, follow_redirects=False)
        assert c.post("/api/issues/999/resolve", json={}).status_code == 404
        r = c.post(f"/api/issues/{issue_id}/resolve", json={"note": "Restarted the service"})
        assert r.status_code == 200 and r.json()["status"] == "resolved"
        assert r.json()["notes"] == "Restarted the service"
        assert r.json()["resolved_by"] == settings.owner_name and r.json()["resolved_at"]
        assert c.post(f"/api/issues/{issue_id}/resolve", json={}).status_code == 409  # already resolved
        assert c.post(f"/api/issues/{issue_id}/resolve", json={"note": "x" * 1001}).status_code == 422

        status = c.get("/api/status").json()
        assert issue_id not in [i["id"] for i in status["issues"]]
        assert [i["id"] for i in status["resolved_issues"]] == [issue_id]

        r = c.post(f"/api/issues/{issue_id}/reopen")
        assert r.status_code == 200 and r.json()["status"] == "open"
        assert c.post(f"/api/issues/{issue_id}/reopen").status_code == 409  # not resolved now
        assert [i["id"] for i in c.get("/api/status").json()["issues"]] == [issue_id]
    assert j.db.pending_actions() == []


def test_resolve_with_no_body_and_signed_in_manager_is_recorded_by_name(settings, monkeypatch):
    settings.jarvis_owner_password = "pw"
    settings.manager_emails = "partner@example.com"
    settings.partner_email, settings.partner_name = "partner@example.com", "Sam"
    j = make(settings)
    issue_id = new_issue(j)
    app = create_app(settings, j)
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "True")
    headers = {"X-MS-CLIENT-PRINCIPAL-IDP": "aad", "X-MS-CLIENT-PRINCIPAL-NAME": "partner@example.com"}
    with TestClient(app) as c:
        r = c.post(f"/api/issues/{issue_id}/resolve", headers=headers)
        assert r.status_code == 200 and r.json()["resolved_by"] == "Sam" and r.json()["notes"] == ""


def test_issues_panel_has_mark_resolved_and_reopen_buttons():
    hud = (WEB / "hud.js").read_text(encoding="utf-8")
    index = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'data-issue-act="resolve"' in hud and ">Mark resolved<" in hud
    assert 'data-issue-act="reopen"' in hud and ">Reopen<" in hud
    assert "/api/issues/${encodeURIComponent(id)}/${act}" in hud and "window.prompt(" in hud  # optional note
    assert 'id="issues-resolved"' in index and '$("#issues-resolved")' in hud
    # a button on each open issue's row, inside the row template renderIssues builds
    body = hud[hud.index("function renderIssues("):hud.index("function renderResolvedIssues(")]
    assert "Mark resolved" in body and "<li " in body


# ------------------------------------------------------------------ the tool
def test_issue_resolve_tool_is_not_an_approval_path():
    tool = TOOLS_BY_NAME["issue_resolve"]
    assert tool.approval is False and tool.model is IssueResolveIn
    assert set(IssueResolveIn.model_fields) == {"issue_id", "note"}
    assert len([t for t in TOOLS if t.name == "issue_resolve"]) == 1
    # the approval gate is untouched
    assert TOOLS_BY_NAME["issue_fix"].approval is True and TOOLS_BY_NAME["email_send"].approval is True


async def test_issue_resolve_tool_closes_the_issue_with_a_note_and_who(settings):
    j = make(settings)
    issue_id = new_issue(j, status="needs_human")
    result = await dispatch(j, TOOLS_BY_NAME["issue_resolve"],
                            IssueResolveIn(issue_id=issue_id, note="Owner confirmed it's sorted"))
    assert "marked resolved" in result
    row = j.db.get_issue(issue_id)
    assert row["status"] == "resolved" and row["notes"] == "Owner confirmed it's sorted"
    assert row["resolved_by"].startswith("Jarvis") and settings.owner_name in row["resolved_by"]
    assert row["resolved_at"]
    assert j.db.pending_actions() == []  # ran straight away; nothing queued for approval
    # a second go, or an unknown number, is explained rather than raised
    assert "already resolved" in await dispatch(j, TOOLS_BY_NAME["issue_resolve"], IssueResolveIn(issue_id=issue_id))
    assert "No issue #999" in await dispatch(j, TOOLS_BY_NAME["issue_resolve"], IssueResolveIn(issue_id=999))
    await j.http.aclose()


async def test_issue_resolve_through_the_chat_loop(settings):
    j = make(settings, [message([tool_block("issue_resolve", {"issue_id": 1, "note": "Done"})], "tool_use"),
                        message([text_block("Marked resolved, sir.")])])
    issue_id = new_issue(j)
    assert issue_id == 1
    reply = await j.brain.ask("Close issue %d, it's sorted" % issue_id)
    assert reply == "Marked resolved, sir."
    assert j.db.get_issue(issue_id)["status"] == "resolved"
    assert j.db.pending_actions() == []
    await j.http.aclose()
