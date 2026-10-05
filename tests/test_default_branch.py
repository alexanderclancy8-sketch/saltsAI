"""Jarvis's own repository: self-improvement / PR tools follow the repo's real default branch (GitHub API
``default_branch``) instead of a configured one that can go stale (it used to be an intermediate integration branch)."""

from __future__ import annotations

import json

import httpx
import pytest

from jarvis.core import Jarvis
from jarvis.integrations.github import GitHub
from jarvis.integrations.github_pr import PRClient, PRError
from tests.fakes import FakeClient
from tests.pr_helpers import BASE, REPO, TOKEN, Mock, pr, respond, runs

MAIN_LINE = "claude/jarvis-company-ai-assistant-gaj3mj"
STALE = "jarvis-updates-2026-09-29"
PR_RESPONSE = {"number": 7, "html_url": "https://github.com/o/r/pull/7", "head": {"sha": "a" * 40}}


def following(m: Mock, configured: str = STALE) -> GitHub:
    return GitHub(TOKEN, REPO, m.client, configured, follow_remote_default=True)


def pr_bodies(m: Mock) -> list[dict]:
    return [json.loads(b) for (method, path), b in zip(m.calls, m.bodies) if (method, path) == ("POST", f"{BASE}/pulls")]


async def test_open_pr_targets_the_repos_default_branch_not_the_configured_one():
    m = Mock({("GET", BASE): {"default_branch": MAIN_LINE}, ("POST", f"{BASE}/pulls"): PR_RESPONSE})
    gh = following(m)
    await gh.open_pr("jarvis/self-abc-1", "t", "b")
    assert pr_bodies(m)[0]["base"] == MAIN_LINE
    assert gh.default_branch == MAIN_LINE and gh.configured_default_branch == STALE
    await m.client.aclose()


async def test_branch_sha_and_reads_start_from_the_default_branch():
    m = Mock({("GET", BASE): {"default_branch": MAIN_LINE},
              ("GET", f"{BASE}/git/ref/heads/{MAIN_LINE}"): {"object": {"sha": "f" * 40}}})
    assert await following(m).branch_sha() == "f" * 40  # not .../heads/jarvis-updates-...
    await m.client.aclose()


async def test_lookup_is_cached_so_it_is_one_extra_request_not_one_per_call():
    m = Mock({("GET", BASE): {"default_branch": MAIN_LINE}, ("POST", f"{BASE}/pulls"): PR_RESPONSE})
    gh = following(m)
    await gh.open_pr("a", "t", "b")
    await gh.open_pr("b", "t", "b")
    assert m.calls.count(("GET", BASE)) == 1
    await m.client.aclose()


async def test_falls_back_to_the_configured_branch_when_github_cannot_say_and_retries_next_time():
    answers = iter([httpx.Response(500, json={"message": "boom"}), httpx.Response(200, json={"default_branch": MAIN_LINE})])
    m = Mock({("GET", BASE): lambda request: next(answers), ("POST", f"{BASE}/pulls"): PR_RESPONSE})
    gh = following(m, configured="develop")
    await gh.open_pr("a", "t", "b")
    assert pr_bodies(m)[0]["base"] == "develop"  # the request still went out, on the configured fallback
    await gh.open_pr("b", "t", "b")
    assert pr_bodies(m)[1]["base"] == MAIN_LINE  # a failed lookup is not cached
    await m.client.aclose()


async def test_missing_default_branch_in_the_reply_uses_the_configured_one():
    m = Mock({("GET", BASE): {"full_name": REPO}, ("POST", f"{BASE}/pulls"): PR_RESPONSE})
    await following(m, configured="develop").open_pr("a", "t", "b")
    assert pr_bodies(m)[0]["base"] == "develop"
    await m.client.aclose()


async def test_without_the_flag_nothing_changes_and_no_extra_request_is_made():
    m = Mock({("POST", f"{BASE}/pulls"): PR_RESPONSE})
    await m.gh.open_pr("a", "t", "b")  # Mock.gh is GitHub(..., "main") with the flag off (the FSM repo's client)
    assert pr_bodies(m)[0]["base"] == "main"
    assert m.calls == [("POST", f"{BASE}/pulls")]
    await m.client.aclose()


async def test_pr_merge_gate_follows_the_resolved_default_branch():
    sha = f"{3:040x}"
    on_main_line = Mock({("GET", BASE): {"default_branch": MAIN_LINE},
                         ("GET", f"{BASE}/pulls/3"): pr(3, base=MAIN_LINE),
                         ("GET", f"{BASE}/commits/{sha}/check-runs"): runs(("tests", "completed", "success")),
                         ("PUT", f"{BASE}/pulls/3/merge"): {"sha": "m" * 40, "merged": True}})
    assert (await PRClient(following(on_main_line)).merge(3))["merged"] is True
    # a PR still aimed at the old integration branch is no longer mergeable through the tool
    stale = Mock({("GET", BASE): {"default_branch": MAIN_LINE},
                  ("GET", f"{BASE}/pulls/3"): pr(3, base=STALE),
                  ("GET", f"{BASE}/commits/{sha}/check-runs"): runs(("tests", "completed", "success"))})
    with pytest.raises(PRError, match=f"targets {STALE}, not {MAIN_LINE}"):
        await PRClient(following(stale)).merge(3)
    assert stale.writes() == []
    await on_main_line.client.aclose()
    await stale.client.aclose()


async def test_the_default_branch_is_still_protected_after_resolution():
    m = Mock({("GET", BASE): {"default_branch": MAIN_LINE}})
    with pytest.raises(PRError, match="never merges into"):
        await PRClient(following(m))._send("POST", "/merges", {"base": MAIN_LINE, "head": "x", "commit_message": "m"})  # noqa: SLF001
    assert m.writes() == []
    await m.client.aclose()


def test_jarvis_wires_the_self_repo_to_follow_the_default_branch_but_not_the_fsm_repo(settings):
    s = settings.model_copy(update={"jarvis_repo": "o/jarvis", "jarvis_github_token": "t", "fsm_repo": "o/fsm",
                                    "github_token": "t", "jarvis_default_branch": "main"})
    j = Jarvis(s, client=FakeClient(None))
    assert j.self_github.follow_remote_default is True
    assert j.self_github.configured_default_branch == "main"
    assert j.github.follow_remote_default is False  # the FSM repo keeps its configured branch


def test_following_can_be_switched_off_and_defaults_on(settings):
    assert settings.jarvis_follow_default_branch is True
    s = settings.model_copy(update={"jarvis_repo": "o/jarvis", "jarvis_github_token": "t",
                                    "jarvis_follow_default_branch": False})
    assert Jarvis(s, client=FakeClient(None)).self_github.follow_remote_default is False
