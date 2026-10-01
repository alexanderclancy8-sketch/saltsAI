"""pr_resolve_conflicts on a host WITHOUT a git binary: the whole job goes through a mocked GitHub REST API (merges
endpoint, contents API, git data API, CI check-runs). Runs everywhere - git is never needed (or touched) here."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

import jarvis.brain.pr_tools as pr_tools
import jarvis.services.pr_resolver as pr_resolver
from jarvis.brain.tools import TOOLS_BY_NAME, dispatch
from jarvis.core import Jarvis
from jarvis.integrations.github_pr import PRClient, PRError
from jarvis.services.pr_resolver import resolve_pr
from tests.fakes import FakeClient
from tests.pr_helpers import BASE, GREEN, TOKEN, Mock, pr, respond, runs

HEAD = f"{5:040x}"  # the PR branch tip pr() reports
MAIN_SHA = "b" * 40
NEW = "c" * 40  # the merge commit
FAILING = runs(("tests", "completed", "failure"))
RUNNING = runs(("tests", "in_progress", None))


@pytest.fixture(autouse=True)
def no_git(monkeypatch):
    """This host has no git; any attempt to run a subprocess (git or tests) fails the test."""
    monkeypatch.setattr(pr_resolver, "git_available", lambda: False)

    async def boom(*a, **k):
        raise AssertionError("no subprocess may be run on the API route")

    monkeypatch.setattr(pr_resolver, "_run", boom)


def b64(text: str) -> dict:
    return {"type": "file", "encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


def api(*, ci=GREEN, **over) -> Mock:
    m = Mock()
    m.routes[("GET", f"{BASE}/pulls/5")] = pr(5, ref="feature", **over)
    m.routes[("GET", f"{BASE}/commits/{NEW}/check-runs")] = ci
    return m


def add_conflict(m: Mock) -> None:
    """app.py changed on both sides; run.sh and old.txt only on main; own.py only on the PR branch."""
    m.routes[("GET", f"{BASE}/compare/main...feature")] = {"files": [
        {"filename": "app.py", "status": "modified", "sha": "m1"}, {"filename": "own.py", "status": "added", "sha": "o1"}]}
    m.routes[("GET", f"{BASE}/compare/feature...main")] = {"files": [
        {"filename": "app.py", "status": "modified", "sha": "t1"}, {"filename": "run.sh", "status": "modified", "sha": "r1"},
        {"filename": "old.txt", "status": "removed", "sha": "x"}]}
    m.routes[("POST", f"{BASE}/merges")] = respond(409, {"message": "Merge conflict"})

    def contents(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=b64("PR version\n" if request.url.params["ref"] == "feature" else "main version\n"))

    m.routes[("GET", f"{BASE}/contents/app.py")] = contents


def add_data_api(m: Mock, ref_response=None) -> None:
    m.routes[("GET", f"{BASE}/git/commits/{HEAD}")] = {"tree": {"sha": "h" * 40}}
    m.routes[("GET", f"{BASE}/commits/main")] = {"sha": MAIN_SHA, "commit": {"tree": {"sha": "t" * 40}}}
    m.routes[("GET", f"{BASE}/git/trees/{'t' * 40}")] = {"truncated": False, "tree": [
        {"path": "app.py", "mode": "100644", "type": "blob", "sha": "t1"},
        {"path": "run.sh", "mode": "100755", "type": "blob", "sha": "r1"}]}
    m.routes[("POST", f"{BASE}/git/blobs")] = respond(201, {"sha": "blob1"})
    m.routes[("POST", f"{BASE}/git/trees")] = respond(201, {"sha": "tree1"})
    m.routes[("POST", f"{BASE}/git/commits")] = respond(201, {"sha": NEW})
    m.routes[("PATCH", f"{BASE}/git/refs/heads/feature")] = ref_response or respond(200, {"object": {"sha": NEW}})


def sent(m: Mock, method: str, path: str) -> list[dict]:
    return [json.loads(body) for (meth, p), body in zip(m.calls, m.bodies) if meth == method and p == BASE + path]


async def run(m: Mock, resolutions=None, **kw):
    return await resolve_pr(PRClient(m.gh), 5, resolutions, ci_wait=0, **kw)


# --------------------------------------------------------------------------- (1) the merges endpoint
async def test_clean_merge_uses_the_merges_api_and_reports_ci():
    m = api()
    m.routes[("POST", f"{BASE}/merges")] = respond(201, {"sha": NEW})
    out = await run(m)
    assert out["status"] == "pushed" and out["method"] == "github-api" and out["merge_commit"] == NEW, out
    assert out["ci_state"] == "success" and "CI passed" in out["summary"] and "no git" in out["summary"]
    assert sent(m, "POST", "/merges") == [{"base": "feature", "head": "main", "commit_message": "Merge main into feature"}]
    assert m.writes() == [("POST", f"{BASE}/merges")]  # nothing but the one merge
    await m.client.aclose()


async def test_nothing_to_merge_is_up_to_date():
    m = api()
    m.routes[("POST", f"{BASE}/merges")] = lambda request: httpx.Response(204)
    out = await run(m)
    assert out["status"] == "up_to_date" and m.writes() == [("POST", f"{BASE}/merges")]
    await m.client.aclose()


@pytest.mark.parametrize("ci,state,needle", [
    (FAILING, "failure", "CI FAILED"), (RUNNING, "pending", "still running"), ({"check_runs": []}, "none", "any CI results")])
async def test_ci_result_is_reported_after_the_push(ci, state, needle):
    m = api(ci=ci)
    m.routes[("POST", f"{BASE}/merges")] = respond(201, {"sha": NEW})
    out = await run(m)
    assert out["status"] == "pushed" and out["ci_state"] == state and needle in out["summary"], out
    await m.client.aclose()


async def test_ci_that_cannot_be_read_does_not_undo_the_push():
    m = api()
    m.routes[("GET", f"{BASE}/commits/{NEW}/check-runs")] = respond(500, {"message": "boom"})
    m.routes[("POST", f"{BASE}/merges")] = respond(201, {"sha": NEW})
    out = await run(m)
    assert out["status"] == "pushed" and out["ci_state"] == "unknown" and "Couldn't read the CI results" in out["summary"]
    await m.client.aclose()


# --------------------------------------------------------------------------- (2) conflicts
async def test_409_reports_the_conflicted_files_and_pushes_nothing():
    m = api()
    add_conflict(m)
    out = await run(m)
    assert out["status"] == "conflicts" and out["conflicted_files"] == ["app.py"], out
    assert "PR version" in out["conflict_hunks"]["app.py"] and "main version" in out["conflict_hunks"]["app.py"]
    assert "resolutions" in out["summary"] and "Nothing was pushed" in out["summary"]
    assert m.writes() == [("POST", f"{BASE}/merges")]  # only the attempted merge; no blobs, trees, commits or refs
    await m.client.aclose()


async def test_resolutions_become_a_two_parent_merge_commit_on_the_pr_branch_only():
    m = api()
    add_conflict(m)
    add_data_api(m)
    out = await run(m, {"app.py": "both\n"})
    assert out["status"] == "pushed" and out["conflicts_resolved"] == ["app.py"] and out["ci_state"] == "success", out
    assert out["merge_commit"] == NEW
    assert sent(m, "POST", "/git/blobs") == [{"content": "both\n", "encoding": "utf-8"}]
    (tree,) = sent(m, "POST", "/git/trees")
    assert tree["base_tree"] == "h" * 40  # starts from the PR branch's own tree
    entries = {e["path"]: e for e in tree["tree"]}
    assert entries["app.py"]["sha"] == "blob1" and entries["app.py"]["mode"] == "100644"  # the resolution
    assert entries["run.sh"]["sha"] == "r1" and entries["run.sh"]["mode"] == "100755"  # main-only change carried over
    assert entries["old.txt"]["sha"] is None  # main-only deletion carried over
    assert "own.py" not in entries  # PR-only change is already in the base tree
    (commit,) = sent(m, "POST", "/git/commits")
    assert commit["parents"] == [HEAD, MAIN_SHA] and commit["tree"] == "tree1"
    assert sent(m, "PATCH", "/git/refs/heads/feature") == [{"sha": NEW, "force": False}]  # moved forward, never forced
    assert all("/refs/heads/main" not in p for _, p in m.writes())
    assert ("POST", f"{BASE}/merges") not in m.calls  # resolutions go straight to the commit; no second merge attempt
    await m.client.aclose()


async def test_a_branch_that_moved_meanwhile_is_reported_and_not_overwritten():
    m = api()
    add_conflict(m)
    add_data_api(m, respond(422, {"message": "Update is not a fast forward"}))
    out = await run(m, {"app.py": "both\n"})
    assert out["status"] == "error" and "422" in out["summary"] and "moved" in out["summary"], out
    assert "committing the merge" in out["summary"]
    await m.client.aclose()


async def test_bad_resolutions_are_refused_before_anything_is_written():
    m = api()
    add_conflict(m)
    add_data_api(m)
    marked = await run(m, {"app.py": "<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> main\n"})
    assert marked["status"] == "refused" and "conflict markers" in marked["summary"]
    extra = await run(m, {"app.py": "ok\n", "other.txt": "sneaky\n"})
    assert extra["status"] == "refused" and "don't conflict" in extra["summary"]
    escape = await run(m, {"../../etc/passwd": "x", "app.py": "ok\n"})
    assert escape["status"] == "refused"
    assert m.writes() == []
    await m.client.aclose()


async def test_workflow_file_conflicts_are_left_to_a_human():
    m = api()
    ci = ".github/workflows/ci.yml"
    m.routes[("GET", f"{BASE}/compare/main...feature")] = {"files": [{"filename": ci, "status": "modified", "sha": "a"}]}
    m.routes[("GET", f"{BASE}/compare/feature...main")] = {"files": [{"filename": ci, "status": "modified", "sha": "b"}]}
    m.routes[("POST", f"{BASE}/merges")] = respond(409, {"message": "Merge conflict"})
    out = await run(m, {ci: "a: both\n"})
    assert out["status"] == "conflicts" and "never edits" in out["summary"] and m.writes() == []
    await m.client.aclose()


async def test_a_file_deleted_on_one_side_needs_a_person():
    m = api()
    m.routes[("GET", f"{BASE}/compare/main...feature")] = {"files": [{"filename": "a.py", "status": "removed", "sha": None}]}
    m.routes[("GET", f"{BASE}/compare/feature...main")] = {"files": [{"filename": "a.py", "status": "modified", "sha": "x"}]}
    m.routes[("POST", f"{BASE}/merges")] = respond(409, {"message": "Merge conflict"})
    out = await run(m)
    assert out["status"] == "conflicts" and "deleted on one side" in out["summary"] and m.writes() == [("POST", f"{BASE}/merges")]
    await m.client.aclose()


async def test_identical_changes_on_both_sides_are_not_a_conflict():
    m = api()
    same = {"files": [{"filename": "a.py", "status": "modified", "sha": "same"}]}
    m.routes[("GET", f"{BASE}/compare/main...feature")] = same
    m.routes[("GET", f"{BASE}/compare/feature...main")] = same
    plan = await PRClient(m.gh).merge_plan("feature", "main")
    assert plan["conflicts"] == [] and plan["theirs_only"] == {}
    await m.client.aclose()


# --------------------------------------------------------------------------- errors say plainly what failed
async def test_a_github_refusal_is_reported_with_the_step_and_status_and_no_token():
    m = api()
    m.routes[("POST", f"{BASE}/merges")] = respond(403, {"message": f"Resource not accessible, token {TOKEN}"})
    out = await run(m)
    assert out["status"] == "error", out
    assert "was not updated" in out["summary"] and "merging through the GitHub API" in out["summary"] and "403" in out["summary"]
    assert "isn't installed" not in out["summary"]
    assert TOKEN not in str(out)
    await m.client.aclose()


async def test_guards_still_apply_on_the_api_route():
    for over, needle in [({"pr_state": "closed"}, "isn't open"), ({"head_repo": "someone/fork"}, "fork")]:
        m = api(**over)
        out = await run(m)
        assert out["status"] == "refused" and needle in out["summary"] and m.writes() == []
    m = Mock()
    m.routes[("GET", f"{BASE}/pulls/5")] = pr(5, ref="main")
    out = await resolve_pr(PRClient(m.gh), 5)
    assert out["status"] == "refused" and "never pushes to main" in out["summary"] and m.writes() == []


# --------------------------------------------------------------------------- the write allow-list
async def test_api_writes_can_never_touch_main_or_be_forced():
    m = Mock()
    m.gh.default_branch = "develop"
    pc = PRClient(m.gh)
    bad = [("PATCH", "/git/refs/heads/main", {"sha": "a", "force": False}, "not an allowed"),
           ("PATCH", "/git/refs/heads/master", {"sha": "a", "force": False}, "not an allowed"),
           ("PATCH", "/git/refs/heads/develop", {"sha": "a", "force": False}, "never moves"),
           ("PATCH", "/git/refs/heads/feature", {"sha": "a", "force": True}, "never forced"),
           ("PATCH", "/git/refs/heads/feature", {"sha": "a"}, "never forced"),
           ("PATCH", "/git/refs/heads/feature", {"sha": "a", "force": False, "x": 1}, "never forced"),
           ("POST", "/merges", {"base": "develop", "head": "feature"}, "never merges into"),
           ("POST", "/merges", {"base": "main", "head": "feature"}, "not an allowed|never merges into"),
           ("POST", "/merges", {"base": "feature", "head": "main", "extra": 1}, "needs exactly"),
           ("DELETE", "/git/refs/heads/feature", {}, "not an allowed"),
           ("POST", "/git/refs", {"ref": "refs/heads/x", "sha": "a"}, "not an allowed")]
    for method, path, payload, match in bad:
        with pytest.raises(PRError, match=match):
            await pc._send(method, path, payload)  # noqa: SLF001
    for branch in ("main", "master", "develop"):
        with pytest.raises(PRError, match="Refused"):
            await pc.update_branch(branch, "feature", "msg")
    assert m.calls == []
    await m.client.aclose()


# --------------------------------------------------------------------------- through the approval-gated tool
async def _run_tool(settings, monkeypatch, m: Mock, **args):
    j = Jarvis(settings, client=FakeClient())
    j.self_github = m.gh
    tool = TOOLS_BY_NAME["pr_resolve_conflicts"]
    assert tool.approval is True
    real = pr_tools.resolve_pr
    monkeypatch.setattr(pr_tools, "resolve_pr", lambda pc, n, r: real(pc, n, r, ci_wait=0))
    notified = []

    async def fake_notify(title, body="", **kw):
        notified.append(title)

    j.notifier.notify = fake_notify
    text = await dispatch(j, tool, tool.model(number=5, **args))
    assert "queued" in text.lower() and m.writes() == []  # nothing happens until approval
    action_id = j.db.pending_actions()[0]["id"]
    await j.actions.approve(action_id)
    for _ in range(100):
        if j.db.get_action(action_id)["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.1)
    action = j.db.get_action(action_id)
    await j.http.aclose()
    await m.client.aclose()
    return action, notified


async def test_tool_works_without_git_and_warns_when_ci_fails(settings, monkeypatch):
    m = api(ci=FAILING)
    m.routes[("POST", f"{BASE}/merges")] = respond(201, {"sha": NEW})
    action, notified = await _run_tool(settings, monkeypatch, m)
    assert action["status"] == "done" and '"status": "pushed"' in action["result"], action
    assert "git isn't installed" not in action["result"]
    assert any("CI failed" in t for t in notified)


async def test_tool_reports_a_failed_api_update_with_the_real_reason(settings, monkeypatch):
    m = api()
    m.routes[("POST", f"{BASE}/merges")] = respond(403, {"message": "Forbidden by policy"})
    action, _ = await _run_tool(settings, monkeypatch, m)
    assert action["status"] == "failed"
    assert "403" in action["result"] and "Forbidden by policy" in action["result"] and TOKEN not in action["result"]
