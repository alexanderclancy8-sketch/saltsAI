"""Progress records for the background engineering agents: the trail is written as a run goes, a finished run shows
its outcome, and a long-silent run is reported as stalled."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.brain.tools import TOOLS_BY_NAME, AgentRunsIn
from jarvis.core import Jarvis
from jarvis.services.agent_runs import MAX_TRAIL, STALL_AFTER, AgentRuns, describe_call
from jarvis.services.security_watch import SecurityWatch
from jarvis.services.self_improve import SelfImprove
from tests.fakes import FakeClient, message, tool_block
from tests.test_security_watch import FakeGitHub as SecurityGitHub, FakeIssues
from tests.test_self_improve import FakeGitHub, finding_files


def make(settings, script=None):
    return Jarvis(settings, client=FakeClient(script))


def age(db, run_id: int, minutes: int) -> None:
    """Make a run look like its last activity was `minutes` ago."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    db.execute("UPDATE agent_runs SET updated_at = ? WHERE id = ?", (past, run_id))


# --------------------------------------------------------------------------- the one-line summaries
def test_describe_call_is_one_short_line_and_never_carries_file_contents():
    secret = "API_KEY = 'sk-very-secret'"
    line = describe_call("str_replace_based_edit_tool",
                         {"command": "create", "path": "/repo/a.py", "file_text": secret, "old_str": secret,
                          "new_str": secret})
    assert line == "editor create /repo/a.py" and "sk-very-secret" not in line
    assert describe_call("grep", {"pattern": "def add", "glob": "*.py"}) == "grep 'def add' in *.py"
    assert describe_call("find_files", {"glob": "*tools*"}) == "find_files *tools*"
    assert describe_call("submit_change", {"pr_title": "Add X", "summary": "long text"}) == "submit_change: Add X"
    assert describe_call("grep", {"pattern": "x"}, ok=False).endswith("(failed)")
    assert describe_call("grep", "not a dict") == "grep '' in *"  # malformed input doesn't crash it
    long = describe_call("grep", {"pattern": "a\n" * 500})
    assert "\n" not in long and len(long) <= 140


# --------------------------------------------------------------------------- recording and reading
async def test_trail_grows_step_by_step_and_a_finished_run_shows_its_outcome(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    with runs.track("self_improve", "add a tool") as run_id:
        assert [r["status"] for r in runs.recent()] == ["running"]
        runs.step("grep", {"pattern": "greet"})
        runs.step("str_replace_based_edit_tool", {"command": "view", "path": "/repo/a.py"})
        mid = runs.recent()[0]
        assert mid["status"] == "running" and mid["steps"] == 2
        assert [s["step"] for s in mid["trail"]] == ["grep 'greet' in *", "editor view /repo/a.py"]
        runs.finish("submitted", "https://github.com/o/r/pull/1")
    done = runs.recent()[0]
    assert done["id"] == run_id and done["status"] == "submitted"
    assert done["outcome"] == "https://github.com/o/r/pull/1" and done["request"] == "add a tool"
    await j.http.aclose()


async def test_a_run_that_raises_is_marked_failed_and_the_error_still_propagates(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    with pytest.raises(RuntimeError):
        with runs.track("fixer", "Issue #1: boom"):
            runs.step("grep", {"pattern": "x"})
            raise RuntimeError("github fell over")
    r = runs.recent()[0]
    assert r["status"] == "failed" and "github fell over" in r["outcome"] and r["steps"] == 1
    await j.http.aclose()


async def test_steps_outside_a_tracked_run_are_ignored(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    runs.step("grep", {"pattern": "x"})  # must not raise or create anything
    assert runs.recent() == []
    await j.http.aclose()


async def test_long_idle_run_is_stalled_but_finished_and_active_ones_are_not(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    quiet = runs.start("self_improve", "quiet one")
    runs.step("grep", {"pattern": "x"}, run_id=quiet)
    busy = runs.start("fixer", "busy one")
    runs.step("grep", {"pattern": "y"}, run_id=busy)
    finished = runs.start("self_improve", "old but finished")
    runs.finish("gave_up", "nothing to do", finished)
    for rid in (quiet, finished):
        age(j.db, rid, int(STALL_AFTER.total_seconds() // 60) + 15)

    by_id = {r["id"]: r for r in runs.recent()}
    assert by_id[quiet]["status"] == "stalled" and by_id[quiet]["idle_minutes"] >= 45
    assert by_id[quiet]["trail"][-1]["step"] == "grep 'x' in *"  # shows where it got stuck
    assert by_id[busy]["status"] == "running"
    assert by_id[finished]["status"] == "gave_up"

    runs.step("grep", {"pattern": "z"}, run_id=quiet)  # it wakes up again -> no longer stalled
    assert runs.recent(run_id=quiet)[0]["status"] == "running"
    # and the same run reads as stalled purely by the clock, without anything stored
    later = datetime.now(timezone.utc) + STALL_AFTER + timedelta(minutes=1)
    assert runs.recent(run_id=quiet, now=later)[0]["status"] == "stalled"
    await j.http.aclose()


async def test_trail_is_capped_but_the_step_count_is_not(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    rid = runs.start("self_improve", "lots of steps")
    for i in range(MAX_TRAIL + 5):
        runs.step("grep", {"pattern": f"p{i}"}, run_id=rid)
    full = runs.recent(run_id=rid)[0]
    assert full["steps"] == MAX_TRAIL + 5 and len(full["trail"]) == MAX_TRAIL
    assert full["trail"][-1]["step"] == f"grep 'p{MAX_TRAIL + 4}' in *"
    assert len(runs.recent()[0]["trail"]) == 10  # the list view only shows the latest few
    await j.http.aclose()


# --------------------------------------------------------------------------- the real engineer loops record themselves
async def test_self_improve_run_records_each_tool_call_as_it_goes(settings, monkeypatch):
    secret_text = "SECRET_TOKEN_VALUE = 1\n"
    j = make(settings, [
        message([tool_block("grep", {"pattern": "greet"}, "t1")], "tool_use"),
        message([tool_block("str_replace_based_edit_tool",
                            {"command": "create", "path": "/repo/jarvis/new_tool.py", "file_text": secret_text},
                            "t2")], "tool_use"),
        message([tool_block("find_files", {"glob": "*.nope"}, "t3"),
                 tool_block("grep", {"pattern": "("}, "t3b")], "tool_use"),  # second one is a bad regex -> error
        message([tool_block("submit_change", {"pr_title": "Add a tool", "summary": "s", "test_notes": "n",
                                              "risk": "low"}, "t4")], "tool_use"),
    ])
    si = SelfImprove(settings, j.db, j.bus, j.notifier, j.client, FakeGitHub(finding_files()))

    async def no_watch(*a, **k):
        pass

    monkeypatch.setattr(si, "watch_ci", no_watch)

    seen: list[tuple[str, list[str]]] = []
    real_stream = j.client.beta.messages.stream

    def spy(**kwargs):  # what the owner would see at the start of each turn of the agent
        r = si.runs.recent()[0]
        seen.append((r["status"], [s["step"] for s in r["trail"]]))
        return real_stream(**kwargs)

    j.client.beta.messages.stream = spy
    result = await si.run("add a tool for X")
    assert "pr_url" in result

    assert seen[0] == ("running", [])
    assert seen[1] == ("running", ["grep 'greet' in *"])
    assert seen[2][1][-1] == "editor create /repo/jarvis/new_tool.py"
    assert seen[3][0] == "running" and len(seen[3][1]) == 4  # + find_files and the failed grep
    final = si.runs.recent()[0]
    assert final["status"] == "submitted" and final["outcome"] == result["pr_url"]
    assert final["request"] == "add a tool for X" and final["kind"] == "self_improve"
    trail = [s["step"] for s in final["trail"]]
    assert trail[-3:] == ["find_files *.nope", "grep '(' in * (failed)", "submit_change: Add a tool"]
    assert secret_text.strip() not in json.dumps(final)  # never file contents
    await j.http.aclose()


async def test_self_improve_give_up_and_failure_are_recorded(settings, monkeypatch):
    j = make(settings)
    si = SelfImprove(settings, j.db, j.bus, j.notifier, j.client, FakeGitHub({}))

    async def quiet_notify(*a, **k):
        pass

    j.notifier.notify = quiet_notify

    async def give_up(request, ws):
        si.runs.step("give_up", {})
        return {"kind": "give_up", "analysis": "Would remove the approval gate."}

    monkeypatch.setattr(si, "_engineer", give_up)
    await si.run("remove the approval step")
    r = si.runs.recent()[0]
    assert r["status"] == "gave_up" and "approval gate" in r["outcome"] and r["steps"] == 1

    async def broken(branch=None):
        raise RuntimeError("404 Not Found: no such branch")

    si.gh.branch_sha = broken
    await si.run("add a tool")
    r = si.runs.recent()[0]
    assert r["status"] == "failed" and "404 Not Found" in r["outcome"]
    assert len(si.runs.recent()) == 2
    await j.http.aclose()


async def test_fixer_run_records_steps_and_outcome(settings):
    j = make(settings, [
        message([tool_block("grep", {"pattern": "login"}, "t1")], "tool_use"),
        message([tool_block("give_up", {"analysis": "It's a data problem.", "recommended_action": "Fix the data."},
                            "t2")], "tool_use"),
    ])
    j.fixer.gh = FakeGitHub(finding_files())

    async def quiet_notify(*a, **k):
        pass

    j.notifier.notify = quiet_notify
    issue_id = j.db.create_issue(reporter="Sam", title="Login is slow", description="Takes ages", source="web")
    await j.fixer.attempt(issue_id)
    r = j.fixer.runs.recent()[0]
    assert r["kind"] == "fixer" and "Login is slow" in r["request"]
    assert [s["step"] for s in r["trail"]] == ["grep 'login' in *", "give_up"]
    assert r["status"] == "gave_up" and "data problem" in r["outcome"]
    await j.http.aclose()


async def test_security_review_records_a_run(settings):
    j = make(settings, [
        message([tool_block("grep", {"pattern": "password"}, "t1")], "tool_use"),
        message([tool_block("submit_findings", {"findings": [], "summary": "Nothing exploitable."}, "t2")],
                "tool_use"),
    ])

    async def quiet_notify(*a, **k):
        pass

    j.notifier.notify = quiet_notify
    sw = SecurityWatch(settings, j.db, j.bus, j.notifier, j.client, SecurityGitHub({"app.py": "x = 1\n"}),
                       FakeIssues(j.db))
    await sw.run()
    r = sw.runs.recent()[0]
    assert r["kind"] == "security_watch" and r["status"] == "submitted"
    assert [s["step"] for s in r["trail"]] == ["grep 'password' in *", "submit_findings: 0 finding(s)"]
    assert "0 new finding(s)" in r["outcome"]
    await j.http.aclose()


# --------------------------------------------------------------------------- the tool
async def test_agent_runs_tool_is_read_only_and_reports_status(settings):
    j = make(settings)
    tool = TOOLS_BY_NAME["agent_runs"]
    assert tool.approval is False
    runs = AgentRuns(j.db)
    stuck = runs.start("self_improve", "add a tool")
    runs.step("grep", {"pattern": "x"}, run_id=stuck)
    age(j.db, stuck, 90)
    out = await tool.handler(j, AgentRunsIn())
    assert out["runs"][0]["status"] == "stalled" and out["stalled_after_minutes"] == 30
    one = await tool.handler(j, AgentRunsIn(run_id=stuck))
    assert one["runs"][0]["trail"][0]["step"] == "grep 'x' in *"
    assert (await tool.handler(j, AgentRunsIn(run_id=9999)))["runs"] == []
    await j.http.aclose()
