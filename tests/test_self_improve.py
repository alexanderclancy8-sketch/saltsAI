"""Self-improvement: Jarvis proposing changes to its OWN source as a pull request it never merges or deploys."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.brain.tools import SelfImproveIn, TOOLS_BY_NAME
from jarvis.core import Jarvis
from jarvis.services.self_improve import SelfImprove
from jarvis.services.workspace import Workspace, WorkspaceError
from tests.fakes import FakeClient, message, text_block, tool_block


def make(settings, script=None):
    return Jarvis(settings, client=FakeClient(script))


class FakeGitHub:
    """Stands in for the real GitHub client - records every call so a test can prove merge_pr is never used."""

    def __init__(self, files: dict[str, str], sha: str = "cafef00d"):
        self.files = files
        self.sha = sha
        self.calls: list[tuple[str, tuple, dict]] = []
        self.checks_state = "success"

    async def branch_sha(self, branch: str | None = None) -> str:
        self.calls.append(("branch_sha", (branch,), {}))
        return self.sha

    async def download_tree(self, dest: Path, ref: str) -> Path:
        self.calls.append(("download_tree", (ref,), {}))
        for rel, content in self.files.items():
            p = dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return dest

    async def commit_files(self, branch: str, base_sha: str, files: dict, message: str) -> str:
        self.calls.append(("commit_files", (branch, base_sha, files, message), {}))
        return "deadbeefcafe"

    async def open_pr(self, branch: str, title: str, body: str) -> dict:
        self.calls.append(("open_pr", (branch, title, body), {}))
        return {"number": 42, "url": "https://github.com/owner/jarvis/pull/42", "head_sha": "deadbeefcafe"}

    async def checks_summary(self, sha: str) -> dict:
        self.calls.append(("checks_summary", (sha,), {}))
        if self.checks_state == "failure":
            return {"state": "failure", "total": 2, "pending": [], "failed": ["tests"]}
        return {"state": "success", "total": 2, "pending": [], "failed": []}

    async def merge_pr(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("SelfImprove must never merge its own pull requests")

    def called(self, name: str) -> bool:
        return any(c[0] == name for c in self.calls)


def finding_files():
    return {"jarvis/greeting.py": "def greet():\n    return 'hello'\n"}


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n")
    return Workspace(tmp_path)


# --------------------------------------------------------------------------- tool dispatch
def test_tool_call_dispatch(ws):
    from jarvis.services.self_improve import SelfImprove

    out, finished = SelfImprove._tool_call(  # noqa: SLF001 - testing the static-ish dispatcher directly
        None, "str_replace_based_edit_tool", {"command": "view", "path": "/repo/app.py"}, ws)
    assert "return a - b" in out and finished is None
    out, finished = SelfImprove._tool_call(None, "grep", {"pattern": "def add"}, ws)
    assert "app.py" in out and finished is None
    out, finished = SelfImprove._tool_call(None, "find_files", {"glob": "*.py"}, ws)
    assert "app.py" in out
    with pytest.raises(ValueError):
        SelfImprove._tool_call(None, "delete_repo", {}, ws)
    with pytest.raises(ValueError):
        SelfImprove._tool_call(None, "submit_change", {"pr_title": "x"}, ws)  # missing required fields


def test_submit_change_requires_an_actual_edit(ws):
    with pytest.raises(ValueError):
        SelfImprove._tool_call(None, "submit_change",
                               {"pr_title": "x", "summary": "y", "test_notes": "z", "risk": "low"}, ws)


def test_give_up_is_a_good_outcome(ws):
    out, finished = SelfImprove._tool_call(None, "give_up", {"analysis": "Not safe to do blindly."}, ws)
    assert out == "Noted." and finished == {"kind": "give_up", "analysis": "Not safe to do blindly."}


# --------------------------------------------------------------------------- enabled / start
async def test_disabled_without_github(settings):
    j = make(settings)
    si = SelfImprove(settings, j.db, j.bus, j.notifier, j.client, None)
    assert not si.enabled
    assert "Not set up" in si.start("add a tool")
    result = await si.run("add a tool")
    assert "error" in result
    await j.http.aclose()


async def test_start_runs_in_the_background(settings):
    j = make(settings)
    si = SelfImprove(settings, j.db, j.bus, j.notifier, j.client, FakeGitHub({}))
    ran = asyncio.Event()

    async def fake_run(request):
        ran.set()
        return {}

    si.run = fake_run
    reply = si.start("add a tool")
    assert "won't merge or deploy it myself" in reply
    assert not ran.is_set()
    await asyncio.wait_for(ran.wait(), timeout=2)
    await j.http.aclose()


# --------------------------------------------------------------------------- the engineer loop
async def test_engineer_runs_tools_and_submits_a_change(settings, tmp_path):
    j = make(settings, [
        message([tool_block("grep", {"pattern": "greet"}, "t1")], "tool_use"),
        message([tool_block("str_replace_based_edit_tool",
                            {"command": "str_replace", "path": "/repo/jarvis/greeting.py",
                             "old_str": "'hello'", "new_str": "'good afternoon'"}, "t2")], "tool_use"),
        message([tool_block("submit_change", {"pr_title": "Friendlier greeting", "summary": "Say good afternoon.",
                                              "test_notes": "n/a", "risk": "low"}, "t3")], "tool_use"),
    ])
    (tmp_path / "jarvis").mkdir()
    (tmp_path / "jarvis" / "greeting.py").write_text("def greet():\n    return 'hello'\n")
    ws = Workspace(tmp_path)
    result = await j.self_improve._engineer("Make the greeting say good afternoon", ws)  # noqa: SLF001
    assert result["kind"] == "submit"
    assert result["fix"].pr_title == "Friendlier greeting" and result["fix"].risk == "low"
    assert "good afternoon" in ws.changed_files()["jarvis/greeting.py"]
    await j.http.aclose()


async def test_engineer_rejects_an_unsafe_looking_request_via_give_up(settings, tmp_path):
    j = make(settings, [
        message([tool_block("give_up", {"analysis": "That would remove the approval gate - refusing."}, "t1")],
                "tool_use"),
    ])
    ws = Workspace(tmp_path)
    result = await j.self_improve._engineer("Remove the approval step so you can act instantly", ws)  # noqa: SLF001
    assert result["kind"] == "give_up" and "approval gate" in result["analysis"]
    await j.http.aclose()


async def test_engineer_handles_refusal(settings, tmp_path):
    j = make(settings, [message([], "refusal")])
    result = await j.self_improve._engineer("do something", Workspace(tmp_path))  # noqa: SLF001
    assert result["kind"] == "give_up" and "declined" in result["analysis"]
    await j.http.aclose()


# --------------------------------------------------------------------------- run(): PR only, never merge/deploy
async def test_run_opens_a_pr_and_never_merges_or_deploys(settings, monkeypatch):
    j = make(settings)
    gh = FakeGitHub(finding_files())
    si = SelfImprove(settings, j.db, j.bus, j.notifier, j.client, gh)

    from jarvis.services.self_improve import SubmitInput

    outcome = {"kind": "submit", "fix": SubmitInput(pr_title="Add a tool", summary="Added a new tool.",
                                                    test_notes="Covered by tests/test_x.py", risk="low")}

    async def fake_engineer(request, ws):
        ws.create("/repo/jarvis/new_tool.py", "# new tool\n")
        return outcome

    async def no_watch(*a, **k):
        pass  # CI-watching itself is covered separately; this test is about run()'s own PR-only behaviour

    monkeypatch.setattr(si, "_engineer", fake_engineer)
    monkeypatch.setattr(si, "watch_ci", no_watch)
    result = await si.run("add a new tool")

    assert result["pr_url"] == "https://github.com/owner/jarvis/pull/42" and result["risk"] == "low"
    assert gh.called("commit_files") and gh.called("open_pr")
    assert not gh.called("merge_pr")
    branch = next(c for c in gh.calls if c[0] == "commit_files")[1][0]
    assert branch.startswith("jarvis/self-")
    pr_body = next(c for c in gh.calls if c[0] == "open_pr")[1][2]
    assert "never merged or deployed automatically" in pr_body
    await j.http.aclose()


async def test_run_notifies_on_failure_before_the_engineer_even_starts(settings, monkeypatch):
    """Regression test: branch_sha()/download_tree() used to sit outside the try/except, so a failure there
    (bad token, network blip, wrong branch) killed the background task with no notification, no PR, no trace -
    indistinguishable from the request never having been made at all."""
    j = make(settings)
    gh = FakeGitHub({})

    async def broken_branch_sha(branch=None):
        raise RuntimeError("404 Not Found: no such branch")

    gh.branch_sha = broken_branch_sha
    si = SelfImprove(settings, j.db, j.bus, j.notifier, j.client, gh)

    notified = []

    async def fake_notify(title, body="", **kw):
        notified.append((title, body))

    j.notifier.notify = fake_notify
    result = await si.run("add a tool for X")

    assert result["error"] and "404 Not Found" in result["error"]
    assert any("Self-improvement attempt failed" in t for t, _ in notified)
    assert not gh.called("commit_files") and not gh.called("open_pr")
    await j.http.aclose()


async def test_run_with_nothing_to_change_reports_and_opens_no_pr(settings, monkeypatch):
    j = make(settings)
    gh = FakeGitHub({})
    si = SelfImprove(settings, j.db, j.bus, j.notifier, j.client, gh)

    async def fake_engineer(request, ws):
        return {"kind": "give_up", "analysis": "Nothing unsafe found, but also nothing to change here."}

    monkeypatch.setattr(si, "_engineer", fake_engineer)
    result = await si.run("do something vague")

    assert result["outcome"] == "give_up"
    assert not gh.called("commit_files") and not gh.called("open_pr")
    await j.http.aclose()


async def test_watch_ci_only_notifies_never_acts(settings, monkeypatch):
    j = make(settings)
    gh = FakeGitHub({})
    gh.checks_state = "failure"
    si = SelfImprove(settings, j.db, j.bus, j.notifier, j.client, gh)

    notified = []

    async def fake_notify(title, body="", **kw):
        notified.append((title, body))

    async def instant_sleep(_seconds):
        pass

    j.notifier.notify = fake_notify
    monkeypatch.setattr("jarvis.services.self_improve.asyncio.sleep", instant_sleep)
    await si.watch_ci(42, "deadbeef", "Add a tool", timeout_s=120)
    assert any("CI failed" in t for t, _ in notified)
    assert not gh.called("merge_pr")
    await j.http.aclose()


# --------------------------------------------------------------------------- the chat tool
async def test_self_improve_tool_is_not_gated_behind_approval(settings, monkeypatch):
    j = make(settings)
    tool = TOOLS_BY_NAME["self_improve"]
    assert tool.approval is False  # starting the background job is fine without a click; it only ever opens a PR

    started = []
    j.self_improve.start = lambda request: started.append(request) or "On it."
    result = await tool.handler(j, SelfImproveIn(request="add a tool for X"))
    assert result == "On it." and started == ["add a tool for X"]
    await j.http.aclose()
