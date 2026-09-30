"""pr_resolve_conflicts: merge main into a PR branch, test, and push only if green - against real local git
repositories (a bare 'origin' on disk stands in for GitHub) and a mocked GitHub API."""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from jarvis.brain.tools import TOOLS_BY_NAME, dispatch
from jarvis.core import Jarvis
from jarvis.integrations.github_pr import PRClient
from jarvis.services.pr_resolver import resolve_pr
from tests.fakes import FakeClient
from tests.pr_helpers import BASE, TOKEN, Mock, pr

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git isn't installed")

PASS = [[sys.executable, "-c", "import sys; sys.exit(0)"]]
FAIL = [[sys.executable, "-c", "print('boom token ghp_abcdefghijklmnopqrstuvwxyz0123456789'); import sys; sys.exit(3)"]]
CLEAN_ROOM = [[sys.executable, "-c",
               "import os, sys; bad = 'ANTHROPIC_API_KEY' in os.environ or 'JARVIS_SECRET' in os.environ "
               "or os.path.exists('.git') or not os.path.exists('app.py'); sys.exit(1 if bad else 0)"]]


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


class Origin:
    """A bare repo plus a working 'seed' clone used to make commits on any branch."""

    def __init__(self, root: Path, files: dict[str, str]):
        self.bare, self.seed = root / "origin.git", root / "seed"
        self.bare.mkdir()
        git(self.bare, "init", "--bare")
        git(self.bare, "symbolic-ref", "HEAD", "refs/heads/main")
        self.seed.mkdir()
        git(self.seed, "init")
        git(self.seed, "symbolic-ref", "HEAD", "refs/heads/main")
        git(self.seed, "remote", "add", "origin", str(self.bare))
        self._write(files)
        git(self.seed, "commit", "-m", "initial")
        git(self.seed, "push", "origin", "main")

    def _write(self, files: dict[str, str]) -> None:
        for name, text in files.items():
            (self.seed / name).parent.mkdir(parents=True, exist_ok=True)
            (self.seed / name).write_text(text)
        git(self.seed, "add", "-A")

    def branch(self, name: str) -> None:
        git(self.seed, "checkout", "-B", name, "main")

    def commit(self, branch: str, files: dict[str, str]) -> None:
        git(self.seed, "checkout", branch)
        self._write(files)
        git(self.seed, "commit", "-m", f"change on {branch}")
        git(self.seed, "push", "origin", branch)

    def sha(self, ref: str) -> str:
        return git(self.bare, "rev-parse", f"refs/heads/{ref}")

    def show(self, ref: str, path: str) -> str:
        return git(self.bare, "show", f"refs/heads/{ref}:{path}")


FILES = {"app.py": "line1\nline2\nline3\n", "other.txt": "other\n"}


@pytest.fixture
def origin(tmp_path):
    return Origin(tmp_path, FILES)


def api(origin: Origin, **over) -> tuple[Mock, PRClient]:
    m = Mock()
    m.routes[("GET", f"{BASE}/pulls/5")] = lambda request: httpx.Response(
        200, json=pr(5, ref="feature", sha=origin.sha("feature"), **over))
    return m, PRClient(m.gh)


async def run(origin, pc, **kw):
    return await resolve_pr(pc, 5, remote_url=str(origin.bare), test_commands=kw.pop("tests", PASS), **kw)


def diverge_cleanly(origin: Origin) -> None:
    origin.branch("feature")
    origin.commit("feature", {"app.py": "line1\nline2\nline3\nfeature-line\n"})
    origin.commit("main", {"new.txt": "from main\n"})


def diverge_with_conflict(origin: Origin) -> None:
    origin.branch("feature")
    origin.commit("feature", {"app.py": "line1\nfeature\nline3\n"})
    origin.commit("main", {"app.py": "line1\nmain\nline3\n"})


async def test_clean_merge_with_passing_tests_is_pushed_to_the_pr_branch_only(origin):
    diverge_cleanly(origin)
    main_before, feature_before = origin.sha("main"), origin.sha("feature")
    m, pc = api(origin)
    out = await run(origin, pc, tests=CLEAN_ROOM)  # also proves the tests saw no secrets and no .git folder
    assert out["status"] == "pushed", out
    assert origin.sha("main") == main_before  # main untouched
    assert origin.sha("feature") != feature_before and out["merge_commit"] == origin.sha("feature")
    parents = git(origin.bare, "rev-list", "--parents", "-n1", "refs/heads/feature").split()[1:]
    assert len(parents) == 2 and feature_before in parents and main_before in parents  # a merge, history kept
    assert origin.show("feature", "new.txt") == "from main" and "feature-line" in origin.show("feature", "app.py")
    assert m.writes() == []  # the GitHub API itself was only read; the push goes via git


async def test_tests_run_without_jarvis_secrets(origin, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("JARVIS_SECRET", "nope")
    diverge_cleanly(origin)
    m, pc = api(origin)
    out = await run(origin, pc, tests=CLEAN_ROOM)
    assert out["status"] == "pushed", out


async def test_failing_tests_mean_nothing_is_pushed_and_failure_is_reported_redacted(origin):
    diverge_cleanly(origin)
    before = origin.sha("feature")
    m, pc = api(origin)
    out = await run(origin, pc, tests=FAIL)
    assert out["status"] == "tests_failed" and "Nothing was pushed" in out["summary"]
    assert "boom" in out["output_tail"] and "ghp_abcdef" not in out["output_tail"]
    assert origin.sha("feature") == before


async def test_a_test_timeout_is_a_failure_not_a_push(origin):
    diverge_cleanly(origin)
    before = origin.sha("feature")
    m, pc = api(origin)
    out = await run(origin, pc, tests=[[sys.executable, "-c", "import time; time.sleep(30)"]], test_timeout=1)
    assert out["status"] == "tests_failed" and "Timed out" in out["output_tail"]
    assert origin.sha("feature") == before


async def test_conflicts_are_reported_not_guessed_and_nothing_is_pushed(origin):
    diverge_with_conflict(origin)
    before = origin.sha("feature")
    m, pc = api(origin)
    out = await run(origin, pc)
    assert out["status"] == "conflicts" and out["conflicted_files"] == ["app.py"]
    assert "<<<<<<<" in out["conflict_hunks"]["app.py"] and "resolutions" in out["summary"]
    assert origin.sha("feature") == before


async def test_a_supplied_resolution_is_tested_then_pushed(origin):
    diverge_with_conflict(origin)
    m, pc = api(origin)
    out = await run(origin, pc, resolutions={"app.py": "line1\nboth\nline3\n"})
    assert out["status"] == "pushed" and out["conflicts_resolved"] == ["app.py"], out
    assert origin.show("feature", "app.py") == "line1\nboth\nline3"


async def test_a_resolution_whose_tests_fail_is_not_pushed(origin):
    diverge_with_conflict(origin)
    before = origin.sha("feature")
    m, pc = api(origin)
    out = await run(origin, pc, resolutions={"app.py": "line1\nboth\nline3\n"}, tests=FAIL)
    assert out["status"] == "tests_failed" and origin.sha("feature") == before


async def test_bad_resolutions_are_refused(origin):
    diverge_with_conflict(origin)
    before = origin.sha("feature")
    m, pc = api(origin)
    marked = await run(origin, pc, resolutions={"app.py": "<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> origin/main\n"})
    assert marked["status"] == "refused" and "conflict markers" in marked["summary"]
    extra = await run(origin, pc, resolutions={"app.py": "ok\n", "other.txt": "sneaky\n"})
    assert extra["status"] == "refused" and "don't conflict" in extra["summary"]
    escape = await run(origin, pc, resolutions={"../../etc/passwd": "x", "app.py": "ok\n"})
    assert escape["status"] == "refused"
    assert origin.sha("feature") == before


async def test_workflow_file_conflicts_are_left_to_a_human(tmp_path):
    origin = Origin(tmp_path, {**FILES, ".github/workflows/ci.yml": "a: 1\n"})
    origin.branch("feature")
    origin.commit("feature", {".github/workflows/ci.yml": "a: feature\n"})
    origin.commit("main", {".github/workflows/ci.yml": "a: main\n"})
    before = origin.sha("feature")
    m, pc = api(origin)
    out = await run(origin, pc, resolutions={".github/workflows/ci.yml": "a: both\n"})
    assert out["status"] == "conflicts" and "never edits" in out["summary"]
    assert origin.sha("feature") == before


async def test_already_up_to_date_does_nothing(origin):
    origin.branch("feature")
    origin.commit("feature", {"app.py": "line1\nline2\nline3\nmore\n"})
    before = origin.sha("feature")
    m, pc = api(origin)
    out = await run(origin, pc)
    assert out["status"] == "up_to_date" and origin.sha("feature") == before


@pytest.mark.parametrize("over,needle", [
    ({"pr_state": "closed"}, "isn't open"),
    ({"head_repo": "someone/fork"}, "fork"),
])
async def test_closed_and_fork_prs_are_refused(origin, over, needle):
    diverge_cleanly(origin)
    before = origin.sha("feature")
    m, pc = api(origin, **over)
    out = await run(origin, pc)
    assert out["status"] == "refused" and needle in out["summary"] and origin.sha("feature") == before


async def test_never_pushes_to_main_even_if_a_pr_claims_it_is_the_head(origin):
    m = Mock()
    m.routes[("GET", f"{BASE}/pulls/5")] = pr(5, ref="main", sha=origin.sha("main"))
    before = origin.sha("main")
    out = await resolve_pr(PRClient(m.gh), 5, remote_url=str(origin.bare), test_commands=PASS)
    assert out["status"] == "refused" and "never pushes to main" in out["summary"]
    assert origin.sha("main") == before


async def test_moved_branch_and_git_errors_do_not_leak_the_token(origin, tmp_path):
    diverge_cleanly(origin)
    m = Mock()
    m.routes[("GET", f"{BASE}/pulls/5")] = pr(5, ref="feature", sha="f" * 40)  # stale sha
    out = await resolve_pr(PRClient(m.gh), 5, remote_url=str(origin.bare), test_commands=PASS)
    assert out["status"] == "error" and "moved" in out["summary"]
    bad = await resolve_pr(PRClient(m.gh), 5, remote_url=str(tmp_path / "missing.git"), test_commands=PASS)
    assert bad["status"] == "error"
    auth = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert TOKEN not in str(bad) and auth not in str(bad)


async def test_tool_is_queued_for_approval_then_runs_and_notifies_on_failure(settings, origin, monkeypatch):
    diverge_with_conflict(origin)
    m, pc = api(origin)
    j = Jarvis(settings, client=FakeClient())
    j.self_github = m.gh
    tool = TOOLS_BY_NAME["pr_resolve_conflicts"]
    assert tool.approval is True

    import jarvis.brain.pr_tools as pr_tools

    real = pr_tools.resolve_pr
    monkeypatch.setattr(pr_tools, "resolve_pr", lambda pc_, n, r: real(pc_, n, r, remote_url=str(origin.bare),
                                                                      test_commands=PASS))
    notified = []

    async def fake_notify(title, body="", **kw):
        notified.append((title, body))

    j.notifier.notify = fake_notify
    before = origin.sha("feature")
    text = await dispatch(j, tool, tool.model(number=5))
    assert "queued" in text.lower() and m.calls == [] and origin.sha("feature") == before  # nothing ran yet
    action_id = j.db.pending_actions()[0]["id"]
    await j.actions.approve(action_id)
    for _ in range(100):
        if j.db.get_action(action_id)["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.1)
    assert j.db.get_action(action_id)["status"] == "done"
    assert '"status": "conflicts"' in j.db.get_action(action_id)["result"]
    assert any("conflicts" in t for t, _ in notified)
    assert origin.sha("feature") == before
    await j.http.aclose()
    await m.client.aclose()
