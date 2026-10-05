"""ci_log_excerpt: the read-only tool that lets the engineering agents see WHY a GitHub Actions run failed
(not just that it did), on both the direct-API loops and the Claude Agent SDK (Max) path."""

from __future__ import annotations

import httpx
import pytest

from jarvis.core import Jarvis
from jarvis.integrations.github import GitHub
from jarvis.services import ci_logs
from jarvis.services.ci_logs import MAX_EXCERPT_CHARS, ci_log_excerpt, excerpt_log
from jarvis.services.fixer import ENGINEER_TOOLS
from jarvis.services.security_watch import REVIEW_TOOLS
from jarvis.services.self_improve import SELF_IMPROVE_TOOLS
from jarvis.services.workspace import Workspace
from tests.fakes import FakeClient, message, tool_block

ASSERT_LINE = "E   AssertionError: assert add(1, 2) == 4"
SUMMARY_LINE = "=== 1 failed, 5 passed in 3.20s ==="


def big_failing_log() -> str:
    """Over 1.7MB: far more than a tool result may hold, with the real evidence mid-log and at the end."""
    noise = [f"2026-09-29T10:00:00.1234567Z collecting module {i} ok" for i in range(30_000)]
    mid = ["2026-09-29T10:01:00.0000000Z FAILED tests/test_greet.py::test_greet - AssertionError: assert 'hello' == 'hi'"]
    tail = ["2026-09-29T10:02:00.0000000Z ##[group]Run pytest",
            f"2026-09-29T10:02:01.0000000Z {ASSERT_LINE}",
            "2026-09-29T10:02:01.0000000Z E    +  where 3 = add(1, 2)",
            f"2026-09-29T10:02:02.0000000Z {SUMMARY_LINE}",
            "2026-09-29T10:02:02.0000000Z ##[error]Process completed with exit code 1."]
    return "\n".join(noise[:15_000] + mid + noise[15_000:] + tail)


class FakeCiGitHub:
    """Just the GitHub methods the CI-log tool uses. Records calls so a test can prove it only reads."""

    def __init__(self, log: str, run_id: int = 555):
        self.log = log
        self.run_id = run_id
        self.calls: list[tuple] = []

    async def latest_runs(self, head_sha=None, limit=5):
        self.calls.append(("latest_runs", head_sha))
        return [{"id": 777, "name": "lint", "status": "completed", "conclusion": "success", "url": "u", "created_at": "b"},
                {"id": self.run_id, "name": "tests", "status": "completed", "conclusion": "failure", "url": "u",
                 "created_at": "a"}]

    async def run_jobs(self, run_id):
        self.calls.append(("run_jobs", run_id))
        return [{"id": 1, "name": "lint", "status": "completed", "conclusion": "success", "failed_step": ""},
                {"id": 2, "name": "pytest", "status": "completed", "conclusion": "failure",
                 "failed_step": "Run pytest"}]

    async def job_log(self, job_id):
        self.calls.append(("job_log", job_id))
        assert job_id == 2, "must only read the failing job's log"
        return self.log


# --------------------------------------------------------------------------- excerpting
def test_small_log_is_returned_whole_without_timestamps():
    out = excerpt_log("2026-09-29T10:00:00.1234567Z \x1b[31mFAILED\x1b[0m test_a\n2026-09-29T10:00:01Z done\n")
    assert out == "FAILED test_a\ndone"


def test_huge_log_keeps_the_assertion_and_stays_under_the_limit():
    log = big_failing_log()
    assert len(log) > 1_000_000
    out = excerpt_log(log)
    assert len(out) <= MAX_EXCERPT_CHARS
    assert ASSERT_LINE in out and SUMMARY_LINE in out and "exit code 1" in out
    assert "assert 'hello' == 'hi'" in out  # the mid-log failure line is kept too, with its context
    assert "lines omitted" in out and "2026-09-29T" not in out


def test_log_that_is_nothing_but_failures_is_still_bounded_and_keeps_the_end():
    log = "\n".join([f"FAILED tests/test_n.py::test_{i} - AssertionError: boom" for i in range(6000)]
                    + ["=== 6000 failed in 99s ==="])
    out = excerpt_log(log)
    assert len(out) <= MAX_EXCERPT_CHARS
    assert "6000 failed in 99s" in out and "test_5999" in out


def test_very_long_lines_cannot_blow_the_limit():
    out = excerpt_log("\n".join("x" * 100_000 for _ in range(500)) + "\nError: final", MAX_EXCERPT_CHARS)
    assert len(out) <= MAX_EXCERPT_CHARS and "Error: final" in out


# --------------------------------------------------------------------------- the tool against a fake failing run
async def test_excerpt_for_a_run_id_reads_only_the_failing_job():
    gh = FakeCiGitHub(big_failing_log())
    out = await ci_log_excerpt(gh, run_id=555)
    assert "job pytest" in out and "Run pytest" in out  # which job and step failed
    assert ASSERT_LINE in out and SUMMARY_LINE in out
    assert len(out) <= MAX_EXCERPT_CHARS
    assert [c[0] for c in gh.calls] == ["run_jobs", "job_log"] and ("job_log", 2) in gh.calls


async def test_excerpt_for_a_head_sha_picks_the_newest_failed_run():
    gh = FakeCiGitHub(big_failing_log(), run_id=555)
    out = await ci_log_excerpt(gh, head_sha="deadbeef")
    assert ASSERT_LINE in out and "run 555" in out
    assert ("run_jobs", 555) in gh.calls and ("latest_runs", "deadbeef") in gh.calls


async def test_no_failed_run_says_so_instead_of_inventing_a_log():
    class Green(FakeCiGitHub):
        async def latest_runs(self, head_sha=None, limit=5):
            return [{"id": 1, "name": "tests", "status": "completed", "conclusion": "success", "url": "u",
                     "created_at": "a"}]

    out = await ci_log_excerpt(Green(""), head_sha="abc")
    assert "No failed run" in out


async def test_secrets_in_a_log_are_redacted():
    gh = FakeCiGitHub("FAILED test_x\nAuthorization: Bearer abcdefghijklmnop12345\ntoken ghp_abcdefghijklmnop1234\n")
    out = await ci_log_excerpt(gh, run_id=555)
    assert "abcdefghijklmnop12345" not in out and "ghp_abcdefghijklmnop1234" not in out and "FAILED test_x" in out


async def test_unreadable_log_is_reported_and_github_errors_become_tool_errors():
    class Expired(FakeCiGitHub):
        async def job_log(self, job_id):
            raise RuntimeError("GitHub GET job log 2 -> 410: logs have expired")

    out = await ci_log_excerpt(Expired(""), run_id=555)
    assert "log unavailable" in out and "expired" in out

    class Down(FakeCiGitHub):
        async def run_jobs(self, run_id):
            raise RuntimeError("boom")

    with pytest.raises(ValueError, match="Could not read CI logs"):
        await ci_log_excerpt(Down(""), run_id=555)
    with pytest.raises(ValueError):
        await ci_log_excerpt(FakeCiGitHub(""))  # neither run_id nor head_sha
    with pytest.raises(ValueError):
        await ci_logs.run_ci_log_tool(FakeCiGitHub(""), {"run_id": "not-a-number"})
    with pytest.raises(ValueError):
        await ci_logs.run_ci_log_tool(None, {"run_id": 1})


# --------------------------------------------------------------------------- the GitHub client calls (read-only GETs)
def github_with(handler) -> tuple[GitHub, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GitHub("tok", "owner/repo", http), http


async def test_run_jobs_and_job_log_are_plain_gets():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/actions/runs/555/jobs"):
            return httpx.Response(200, json={"jobs": [
                {"id": 2, "name": "pytest", "status": "completed", "conclusion": "failure",
                 "steps": [{"name": "Set up", "status": "completed", "conclusion": "success"},
                           {"name": "Run pytest", "status": "completed", "conclusion": "failure"}]}]})
        if request.url.path.endswith("/actions/jobs/2/logs"):
            return httpx.Response(200, content=b"A" * 100 + b"tail-end")
        return httpx.Response(404, text="nope")

    gh, http = github_with(handler)
    jobs = await gh.run_jobs(555)
    assert jobs == [{"id": 2, "name": "pytest", "status": "completed", "conclusion": "failure",
                     "failed_step": "Run pytest"}]
    assert (await gh.job_log(2, max_bytes=20)).endswith("tail-end") and len(await gh.job_log(2, max_bytes=20)) == 20
    with pytest.raises(RuntimeError):
        await gh.job_log(3)
    assert {m for m, _ in seen} == {"GET"}
    await http.aclose()


# --------------------------------------------------------------------------- wired into all three agents
def test_tool_is_offered_to_all_three_agents_with_a_schema():
    for tools in (SELF_IMPROVE_TOOLS, ENGINEER_TOOLS, REVIEW_TOOLS):
        entry = next(t for t in tools if t["name"] == "ci_log_excerpt")
        assert set(entry["input_schema"]["properties"]) == {"run_id", "head_sha"}


def _ci_result(client: FakeClient) -> str:
    """The tool_result content the model was sent back after its first (ci_log_excerpt) turn."""
    messages = client.beta.messages.calls[-1]["messages"]
    results = next(m["content"] for m in messages if m["role"] == "user" and isinstance(m["content"], list))
    assert len(results) == 1 and not results[0].get("is_error"), results
    return results[0]["content"]


async def test_self_improve_loop_can_call_it(settings, tmp_path):
    j = Jarvis(settings, client=FakeClient([
        message([tool_block("ci_log_excerpt", {"head_sha": "deadbeef"}, "t1")], "tool_use"),
        message([tool_block("give_up", {"analysis": "saw the failure"}, "t2")], "tool_use"),
    ]))
    j.self_improve.gh = FakeCiGitHub(big_failing_log())
    result = await j.self_improve._engineer("fix CI", Workspace(tmp_path))  # noqa: SLF001
    assert result["kind"] == "give_up"
    sent = _ci_result(j.client)
    assert ASSERT_LINE in sent and len(sent) <= MAX_EXCERPT_CHARS
    await j.http.aclose()


async def test_fixer_loop_can_call_it(settings, tmp_path):
    j = Jarvis(settings, client=FakeClient([
        message([tool_block("ci_log_excerpt", {"run_id": 555}, "t1")], "tool_use"),
        message([tool_block("give_up", {"analysis": "a", "recommended_action": "b"}, "t2")], "tool_use"),
    ]))
    j.fixer.gh = FakeCiGitHub(big_failing_log())
    issue = {"id": 1, "reporter": "Sam", "title": "t", "description": "d"}
    result = await j.fixer.run_engineer(issue, Workspace(tmp_path))
    assert result["kind"] == "give_up"
    assert ASSERT_LINE in _ci_result(j.client)
    await j.http.aclose()


async def test_security_watch_loop_can_call_it(settings, tmp_path):
    j = Jarvis(settings, client=FakeClient([
        message([tool_block("ci_log_excerpt", {"run_id": 555}, "t1")], "tool_use"),
        message([tool_block("submit_findings", {"findings": [], "summary": "clean"}, "t2")], "tool_use"),
    ]))
    j.security_watch.gh = FakeCiGitHub(big_failing_log())
    result = await j.security_watch._review(Workspace(tmp_path))  # noqa: SLF001
    assert result.summary == "clean"
    assert ASSERT_LINE in _ci_result(j.client)
    await j.http.aclose()


async def test_a_github_failure_is_a_tool_error_not_a_crash(settings, tmp_path):
    class Down(FakeCiGitHub):
        async def run_jobs(self, run_id):
            raise RuntimeError("boom")

    j = Jarvis(settings, client=FakeClient([
        message([tool_block("ci_log_excerpt", {"run_id": 1}, "t1")], "tool_use"),
        message([tool_block("give_up", {"analysis": "x"}, "t2")], "tool_use"),
    ]))
    j.self_improve.gh = Down("")
    await j.self_improve._engineer("fix CI", Workspace(tmp_path))  # noqa: SLF001
    messages = j.client.beta.messages.calls[-1]["messages"]
    results = next(m["content"] for m in messages if m["role"] == "user" and isinstance(m["content"], list))
    assert results[0]["is_error"] and "Could not read CI logs" in results[0]["content"]
    await j.http.aclose()


# --------------------------------------------------------------------------- the Claude Agent SDK (Max) path
async def test_max_paths_expose_only_the_ci_log_tool_as_an_mcp_server(settings, tmp_path, monkeypatch):
    j = Jarvis(settings, client=FakeClient())
    seen: list[dict] = []

    async def fake_run_once(s, **kw):
        seen.append(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr("jarvis.brain.max_backend.run_once", fake_run_once)
    j.self_improve.gh = j.fixer.gh = j.security_watch.gh = FakeCiGitHub("")
    issue = {"id": 1, "reporter": "Sam", "title": "t", "description": "d"}
    for call in (j.self_improve._engineer_max("x", Workspace(tmp_path)),  # noqa: SLF001
                 j.fixer._run_engineer_max(issue, Workspace(tmp_path)),  # noqa: SLF001
                 j.security_watch._review_max(Workspace(tmp_path))):  # noqa: SLF001
        with pytest.raises(RuntimeError):
            await call
    assert len(seen) == 3
    for kw in seen:
        assert ci_logs.MCP_SERVER_NAME in kw["mcp_servers"]
        assert ci_logs.MCP_ALLOWED_TOOL in kw["extra_allowed"]
        assert ci_logs.MCP_ALLOWED_TOOL == "mcp__jarvis_ci__ci_log_excerpt"
    await j.http.aclose()
