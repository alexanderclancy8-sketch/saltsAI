"""Continuous security review: the read-only tool loop, dedupe of already-flagged findings, and the
background start() entry point."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.core import Jarvis
from jarvis.services.security_watch import SecurityWatch, SubmitFindings, _view_only
from jarvis.services.workspace import Workspace, WorkspaceError
from tests.fakes import FakeClient, message, text_block, tool_block


def make(settings, script=None):
    return Jarvis(settings, client=FakeClient(script))


class FakeGitHub:
    """Stands in for the real GitHub client: writes fixed file contents instead of downloading a tarball."""

    def __init__(self, files: dict[str, str], sha: str = "deadbeef"):
        self.files = files
        self.sha = sha

    async def branch_sha(self, branch: str | None = None) -> str:
        return self.sha

    async def download_tree(self, dest: Path, ref: str) -> Path:
        for rel, content in self.files.items():
            p = dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return dest


class FakeIssues:
    """Records issues in the real DB without the triage/process pipeline a full IssueService would spawn."""

    def __init__(self, db):
        self.db = db
        self.reported: list[dict] = []

    async def report(self, *, reporter, title, description, severity="medium", system="Salts FSM",
                     source="web", notify=True, process=True, **kw):
        issue_id = self.db.create_issue(reporter=reporter, title=title[:200], description=description[:8000],
                                        source=source, system=system, severity=severity)
        issue = self.db.get_issue(issue_id)
        self.reported.append(issue)
        return issue


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "app.py").write_text("password = 'hunter2'\n\ndef add(a, b):\n    return a + b\n")
    return Workspace(tmp_path)


def finding(title="SQL built from unescaped search text", file="app.py", severity="high",
           description="User input is concatenated into a query.", suggested_fix="Use parameterised queries."):
    return {"title": title, "file": file, "severity": severity, "description": description,
            "suggested_fix": suggested_fix}


# --------------------------------------------------------------------------- _view_only
def test_view_only_allows_view_and_rejects_everything_else(ws):
    assert "hunter2" in _view_only({"command": "view", "path": "/repo/app.py"}, ws)
    for bad in ({"command": "str_replace", "path": "/repo/app.py", "old_str": "x", "new_str": "y"},
                {"command": "create", "path": "/repo/new.py", "file_text": "x"},
                {"command": "insert", "path": "/repo/app.py", "insert_line": 0, "insert_text": "x"}):
        with pytest.raises(WorkspaceError):
            _view_only(bad, ws)


def test_tool_call_dispatch(ws):
    assert "hunter2" in SecurityWatch._tool_call(
        "str_replace_based_edit_tool", {"command": "view", "path": "/repo/app.py"}, ws)
    assert "app.py" in SecurityWatch._tool_call("grep", {"pattern": "password"}, ws)
    assert "app.py" in SecurityWatch._tool_call("find_files", {"glob": "*.py"}, ws)
    assert SecurityWatch._tool_call("submit_findings", {"findings": [], "summary": "clean"}, ws) == "Submitted."
    with pytest.raises(ValueError):
        SecurityWatch._tool_call("delete_everything", {}, ws)


# --------------------------------------------------------------------------- enabled / start
async def test_disabled_without_github(settings):
    j = make(settings)
    sw = SecurityWatch(settings, j.db, j.bus, j.notifier, j.client, None, FakeIssues(j.db))
    assert not sw.enabled
    assert "Not set up" in sw.start()
    assert not sw._tasks
    result = await sw.run()
    assert "error" in result
    await j.http.aclose()


async def test_start_runs_in_the_background(settings):
    j = make(settings)
    sw = SecurityWatch(settings, j.db, j.bus, j.notifier, j.client, FakeGitHub({}), FakeIssues(j.db))
    ran = asyncio.Event()

    async def fake_run():
        ran.set()
        return {}

    sw.run = fake_run
    reply = sw.start()
    assert "Started" in reply
    assert not ran.is_set()  # hasn't run synchronously
    await asyncio.wait_for(ran.wait(), timeout=2)
    await j.http.aclose()


# --------------------------------------------------------------------------- the inline review loop
async def test_review_runs_tools_and_returns_submitted_findings(settings, tmp_path):
    j = make(settings, [
        message([tool_block("grep", {"pattern": "password"}, "t1")], "tool_use"),
        message([tool_block("str_replace_based_edit_tool", {"command": "view", "path": "/repo/app.py"}, "t2")],
                "tool_use"),
        message([tool_block("submit_findings", {"findings": [finding()], "summary": "One issue found."}, "t3")],
                "tool_use"),
    ])
    (tmp_path / "app.py").write_text("password = 'hunter2'\n")
    result = await j.security_watch._review(Workspace(tmp_path))
    assert isinstance(result, SubmitFindings)
    assert result.summary == "One issue found."
    assert len(result.findings) == 1 and result.findings[0].severity == "high"
    await j.http.aclose()


async def test_review_reports_a_rejected_edit_attempt_without_crashing(settings, tmp_path):
    j = make(settings, [
        message([tool_block("str_replace_based_edit_tool",
                            {"command": "str_replace", "path": "/repo/app.py", "old_str": "a", "new_str": "b"},
                            "t1")], "tool_use"),
        message([tool_block("submit_findings", {"findings": [], "summary": "Nothing else found."}, "t2")],
                "tool_use"),
    ])
    (tmp_path / "app.py").write_text("password = 'hunter2'\n")
    result = await j.security_watch._review(Workspace(tmp_path))
    assert isinstance(result, SubmitFindings) and result.findings == []
    # the rejected edit was reported back as a tool error, not raised - the loop kept going to submit_findings
    assert len(j.client.beta.messages.calls) == 2  # both turns actually ran
    await j.http.aclose()


async def test_review_handles_refusal(settings, tmp_path):
    j = make(settings, [message([], "refusal")])
    result = await j.security_watch._review(Workspace(tmp_path))
    assert result.findings == [] and "declined" in result.summary
    await j.http.aclose()


# --------------------------------------------------------------------------- run(): dedupe + reporting
async def test_run_reports_new_findings_and_dedupes_active_ones(settings, monkeypatch):
    j = make(settings)
    issues = FakeIssues(j.db)
    gh = FakeGitHub({"app.py": "password = 'hunter2'\n"})
    sw = SecurityWatch(settings, j.db, j.bus, j.notifier, j.client, gh, issues)

    first = SubmitFindings(findings=[finding(title="Hardcoded password"), finding(title="SQL injection in search")],
                           summary="Two issues found.")
    monkeypatch.setattr(sw, "_review", lambda ws: _async_return(first))
    result = await sw.run()
    assert len(result["new_issues"]) == 2
    assert len(issues.reported) == 2

    # Re-running with the same two findings again should report nothing new - both are still "open".
    second = SubmitFindings(findings=[finding(title="Hardcoded password"), finding(title="SQL injection in search")],
                            summary="Same two again.")
    monkeypatch.setattr(sw, "_review", lambda ws: _async_return(second))
    result2 = await sw.run()
    assert result2["new_issues"] == []
    assert len(issues.reported) == 2  # no new rows were created

    # Resolve the "Hardcoded password" issue, then it should be reported again as new work next time it's seen.
    resolved_id = next(i["id"] for i in issues.reported if i["title"] == "Security: Hardcoded password")
    j.db.update_issue(resolved_id, status="resolved")
    third = SubmitFindings(findings=[finding(title="Hardcoded password")], summary="Back again.")
    monkeypatch.setattr(sw, "_review", lambda ws: _async_return(third))
    result3 = await sw.run()
    assert len(result3["new_issues"]) == 1
    assert len(issues.reported) == 3
    await j.http.aclose()


async def test_run_reports_nothing_new_with_zero_findings(settings, monkeypatch):
    j = make(settings)
    issues = FakeIssues(j.db)
    sw = SecurityWatch(settings, j.db, j.bus, j.notifier, j.client, FakeGitHub({}), issues)
    monkeypatch.setattr(sw, "_review", lambda ws: _async_return(SubmitFindings(findings=[], summary="Clean pass.")))
    result = await sw.run()
    assert result["new_issues"] == [] and result["summary"] == "Clean pass."
    assert issues.reported == []
    await j.http.aclose()


async def _async_return(value):
    return value
