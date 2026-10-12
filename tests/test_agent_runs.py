"""Progress records for the background engineering agents: the trail is written as a run goes, a finished run shows
its outcome, and a long-silent run is reported as stalled."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jarvis.brain.tools import TOOLS_BY_NAME, AgentRunsIn
from jarvis.core import Jarvis
from jarvis.services import agent_runs as agent_runs_mod
from jarvis.services.agent_runs import (MAX_TRAIL, NO_RESULT_NOTE, RUN_CAP, STALL_AFTER, AgentRuns, RunTimedOut,
                                        describe_call, short_hash)
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


async def test_a_run_that_ends_without_a_recorded_outcome_is_not_labelled_a_failure(settings):
    """A fixer pass that neither submits nor gives up and doesn't raise produced nothing - that is not a failure,
    and must not carry the old 'Ended without recording an outcome' label."""
    j = make(settings)
    runs = AgentRuns(j.db)
    with runs.track("fixer", "Issue #2: quiet end"):
        runs.step("grep", {"pattern": "x"})
    r = runs.recent()[0]
    assert r["status"] == "gave_up" and r["steps"] == 1
    assert r["outcome"] == NO_RESULT_NOTE and "recording an outcome" not in r["outcome"]
    # an outcome recorded inside the run is never overwritten by the neutral note
    with runs.track("fixer", "Issue #3: ok"):
        runs.finish("submitted", "https://github.com/o/r/pull/9")
    assert runs.recent()[0]["status"] == "submitted"
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


async def test_agent_runs_tool_shows_the_lesson_and_the_cap(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    rid = runs.start("fixer", "Issue #4: crash")
    runs.finish("gave_up", "Could not reproduce the crash.", rid)
    out = await TOOLS_BY_NAME["agent_runs"].handler(j, AgentRunsIn(run_id=rid))
    assert out["run_cap_minutes"] == int(RUN_CAP.total_seconds() // 60)
    assert out["runs"][0]["lesson"] and "Could not reproduce" in out["runs"][0]["lesson"]
    await j.http.aclose()


# --------------------------------------------------------------------------- trail hashes and stuck-loop detection
def test_short_hash_is_stable_short_and_hides_the_value():
    a = short_hash({"path": "/x.py", "pattern": "SECRET_PASSWORD"})
    assert a == short_hash({"pattern": "SECRET_PASSWORD", "path": "/x.py"})  # key order does not matter
    assert a != short_hash({"path": "/x.py", "pattern": "other"})
    assert len(a) == 8 and "SECRET" not in a


async def test_trail_records_tool_name_and_argument_hash_but_never_the_arguments(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    rid = runs.start("self_improve", "x")
    args = {"command": "create", "path": "/repo/a.py", "file_text": "API_KEY = 'sk-very-secret'"}
    runs.step("str_replace_based_edit_tool", args, run_id=rid)
    step = runs.recent(run_id=rid)[0]["trail"][0]
    assert step["tool"] == "str_replace_based_edit_tool" and step["h"] == short_hash(args)
    assert step["file"] == "/repo/a.py"
    assert "sk-very-secret" not in json.dumps(runs.recent(run_id=rid))
    await j.http.aclose()


async def test_same_tool_and_arguments_three_times_in_a_row_flags_the_run_looping(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    rid = runs.start("self_improve", "x")
    assert runs.step("grep", {"pattern": "a"}, run_id=rid) is None
    assert runs.step("grep", {"pattern": "a"}, run_id=rid) is None
    assert runs.recent(run_id=rid)[0]["status"] == "running"
    nudge = runs.step("grep", {"pattern": "a"}, run_id=rid)  # the third
    assert nudge and "grep" in nudge
    r = runs.recent(run_id=rid)[0]
    assert r["status"] == "looping" and "grep" in r["looping"]
    assert runs.step("grep", {"pattern": "a"}, run_id=rid) is None  # the nudge is only given once
    # the flag stays on the record when the run ends
    runs.finish("gave_up", "stuck", rid)
    done = runs.recent(run_id=rid)[0]
    assert done["status"] == "gave_up" and done["looping"] and "stuck repeating" in done["lesson"]
    await j.http.aclose()


async def test_varied_or_interleaved_calls_are_not_a_loop(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    rid = runs.start("self_improve", "x")
    for pattern in ("a", "a", "b", "a", "a", "c"):
        runs.step("grep", {"pattern": pattern}, run_id=rid)
    runs.step("find_files", {"glob": "a"}, run_id=rid)
    runs.step("grep", {"pattern": "a"}, run_id=rid)
    assert runs.recent(run_id=rid)[0]["looping"] is None
    await j.http.aclose()


def _edit(runs, rid, n, path="/repo/a.py", **kw):
    return runs.step("str_replace_based_edit_tool",
                     {"command": "str_replace", "path": path, "old_str": f"v{n}", "new_str": f"v{n + 1}"},
                     run_id=rid, **kw)


async def test_a_file_edited_more_than_five_times_with_unchanged_test_output_is_looping(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    rid = runs.start("self_improve", "x")
    for n in range(5):
        _edit(runs, rid, n, test_output="FAILED test_a - assert 1 == 2")
    assert runs.recent(run_id=rid)[0]["looping"] is None  # five is allowed
    nudge = _edit(runs, rid, 5, test_output="FAILED test_a - assert 1 == 2")
    assert nudge
    r = runs.recent(run_id=rid)[0]
    assert r["status"] == "looping" and "/repo/a.py edited 6 times" in r["looping"]
    await j.http.aclose()


async def test_edits_that_change_the_failing_output_are_progress_not_a_loop(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    rid = runs.start("self_improve", "x")
    for n in range(8):
        _edit(runs, rid, n, test_output=f"FAILED test_{n}")  # a different failure each time
    assert runs.recent(run_id=rid)[0]["looping"] is None
    # six edits to different files are not one file edited again and again either
    other = runs.start("self_improve", "y")
    for n in range(6):
        _edit(runs, other, n, path=f"/repo/f{n}.py")
    assert runs.recent(run_id=other)[0]["looping"] is None
    await j.http.aclose()


async def test_record_message_turns_streamed_tool_calls_into_trail_steps(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    rid = runs.start("self_improve", "x")
    msg = SimpleNamespace(content=[
        SimpleNamespace(text="thinking out loud"),  # a text block: ignored
        SimpleNamespace(type="tool_use", name="Edit", input={"file_path": "/r/a.py", "new_string": "S3CRET"}),
        SimpleNamespace(type="tool_use", name="Grep", input={"pattern": "x"}),
    ])
    assert runs.record_message(msg, run_id=rid) is None
    assert runs.record_message(SimpleNamespace(content="just text"), run_id=rid) is None
    r = runs.recent(run_id=rid)[0]
    assert [(s["tool"], s.get("file")) for s in r["trail"]] == [("Edit", "/r/a.py"), ("Grep", None)]
    assert "S3CRET" not in json.dumps(r)
    await j.http.aclose()


# --------------------------------------------------------------------------- the wall-clock cap
async def test_a_step_after_the_cap_marks_the_run_timed_out_and_stops_it(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    rid = runs.start("self_improve", "slow one")
    runs.step("grep", {"pattern": "a"}, run_id=rid)
    past = (datetime.now(timezone.utc) - RUN_CAP - timedelta(minutes=5)).isoformat(timespec="seconds")
    j.db.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", (past, rid))
    with pytest.raises(RunTimedOut):
        runs.step("grep", {"pattern": "b"}, run_id=rid)
    r = runs.recent(run_id=rid)[0]
    assert r["status"] == "timed_out" and "wall-clock cap" in r["outcome"]
    assert "grep 'b' in *" in r["outcome"]  # the last-known activity
    assert r["last_step"] == "grep 'b' in *" and r["lesson"] and "cap" in r["lesson"]
    # the error the hard stop causes must not turn it into a plain failure
    runs.finish("failed", "RunTimedOut: ...", rid)
    assert runs.recent(run_id=rid)[0]["status"] == "timed_out"
    await j.http.aclose()


async def test_a_run_past_the_cap_that_nothing_stopped_reads_as_timed_out_but_is_not_rewritten(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    rid = runs.start("security_watch", "Claude Code run, no steps")
    past = (datetime.now(timezone.utc) - RUN_CAP - timedelta(minutes=5)).isoformat(timespec="seconds")
    j.db.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", (past, rid))
    r = runs.recent(run_id=rid)[0]
    assert r["status"] == "timed_out" and r["elapsed_minutes"] >= RUN_CAP.total_seconds() // 60
    assert j.db.query_one("SELECT status FROM agent_runs WHERE id = ?", (rid,))["status"] == "running"
    runs.finish("submitted", "https://github.com/o/r/pull/5", rid)  # it was alive after all
    assert runs.recent(run_id=rid)[0]["status"] == "submitted"
    await j.http.aclose()


async def test_track_cancels_a_task_that_runs_past_the_cap(settings, monkeypatch):
    j = make(settings)
    runs = AgentRuns(j.db)
    monkeypatch.setattr(agent_runs_mod, "RUN_CAP", timedelta(milliseconds=50))

    async def body():
        with runs.track("self_improve", "never finishes"):
            runs.note("grep 'a' in *")  # (note, not step: started_at has one-second resolution, so a step could trip the cap itself)
            await asyncio.sleep(10)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(body())
    r = runs.recent()[0]
    assert r["status"] == "timed_out" and "wall-clock cap" in r["outcome"] and "grep 'a' in *" in r["outcome"]
    assert r["lesson"]
    await j.http.aclose()


async def test_a_run_that_finishes_inside_the_cap_is_untouched_by_the_timer(settings, monkeypatch):
    j = make(settings)
    runs = AgentRuns(j.db)
    monkeypatch.setattr(agent_runs_mod, "RUN_CAP", timedelta(milliseconds=100))
    with runs.track("fixer", "quick"):
        runs.finish("submitted", "https://github.com/o/r/pull/6")
    await asyncio.sleep(0.2)  # the timer would have fired by now had it not been cancelled
    assert runs.recent()[0]["status"] == "submitted"
    await j.http.aclose()


# --------------------------------------------------------------------------- the lesson
async def test_gave_up_and_failed_runs_store_a_one_paragraph_lesson(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    a = runs.start("self_improve", "remove the approval step")
    runs.finish("gave_up", "Would remove the approval gate.", a)
    b = runs.start("fixer", "Issue #1")
    runs.finish("failed", "404 Not Found: no such branch", b)
    c = runs.start("fixer", "Issue #2")
    runs.finish("gave_up", "Cannot tell which screen is meant.", c)
    d = runs.start("fixer", "Issue #3")
    runs.finish("failed", "ValueError: odd", d, lesson="Needed the full traceback.\nPlease add it.")
    by_id = {r["id"]: r for r in runs.recent(10)}
    assert "protected" in by_id[a]["lesson"]
    assert "GitHub" in by_id[b]["lesson"] and "token" in by_id[b]["lesson"]
    assert "Cannot tell which screen" in by_id[c]["lesson"] and "would have let it" in by_id[c]["lesson"]
    assert by_id[d]["lesson"] == "Needed the full traceback. Please add it."  # an explicit lesson wins; one paragraph
    for r in by_id.values():
        assert "\n" not in r["lesson"] and len(r["lesson"]) <= agent_runs_mod.LESSON_CHARS
    await j.http.aclose()


async def test_a_submitted_run_has_no_lesson_and_a_quiet_end_gets_one(settings):
    j = make(settings)
    runs = AgentRuns(j.db)
    with runs.track("fixer", "ok"):
        runs.finish("submitted", "https://github.com/o/r/pull/7")
    assert runs.recent()[0]["lesson"] is None
    with runs.track("fixer", "quiet"):
        pass
    r = runs.recent()[0]
    assert r["status"] == "gave_up" and "without submitting" in r["lesson"]
    with pytest.raises(RuntimeError):
        with runs.track("fixer", "boom"):
            raise RuntimeError("github fell over")
    r = runs.recent()[0]
    assert r["status"] == "failed" and "github fell over" in r["lesson"]
    await j.http.aclose()
