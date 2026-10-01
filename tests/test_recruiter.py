"""Recruiting a sub-agent: it can actually call Jarvis's own tools (through the same dispatch() chokepoint,
so a write it proposes still queues for approval same as ever), it never gets a way to recurse into
recruiting further agents or starting another background job, and the tool is reachable from conversation."""

from __future__ import annotations

from jarvis.brain.tools import RecruitAgentIn, TOOLS_BY_NAME, recruit_agent
from jarvis.core import Jarvis
from tests.fakes import FakeClient, message, text_block, tool_block


def make(settings, script=None):
    return Jarvis(settings, client=FakeClient(script))


async def test_recruited_agent_can_call_a_real_tool_and_report_back(settings):
    j = make(settings, [
        message([tool_block("knowledge_search", {"query": "BS 5839"})], "tool_use"),
        message([text_block("Found the relevant standard and summarised it for you.")]),
    ])
    report = await j.recruiter.recruit("Standards researcher", "Look up BS 5839 and summarise it.")
    assert "summarised it" in report
    calls = j.client.beta.messages.stream.__self__.calls
    assert len(calls) == 2  # one turn to call the tool, one to answer
    await j.http.aclose()


async def test_a_write_the_agent_proposes_still_queues_for_approval(settings):
    j = make(settings, [
        message([tool_block("site_access_code_update", {"site": "Kestrel", "system": "Fire panel",
                                                         "code": "1234", "notes": ""})], "tool_use"),
        message([text_block("Recorded (queued for approval).")]),
    ])
    await j.recruiter.recruit("Site records clerk", "Record the code the owner gave you for Kestrel.")
    pending = j.db.pending_actions()
    assert len(pending) == 1 and pending[0]["kind"] == "tool:site_access_code_update"
    assert j.site_access.find("Kestrel") == []  # not actually written yet
    await j.http.aclose()


async def test_cannot_be_given_recursive_or_background_tools(settings):
    j = make(settings, [message([text_block("No tools needed for this one.")])])
    tools = j.recruiter._tool_set(["recruit_agent", "self_improve", "knowledge_search"])
    assert {t.name for t in tools} == {"knowledge_search"}
    await j.http.aclose()


async def test_default_tool_set_excludes_recursion_and_background_jobs(settings):
    j = make(settings)
    names = {t.name for t in j.recruiter._tool_set(None)}
    assert names.isdisjoint({"recruit_agent", "self_improve", "run_security_review", "create_automation"})
    assert "knowledge_search" in names  # still has the ordinary read tools
    await j.http.aclose()


async def test_running_out_of_turns_gives_a_best_effort_answer_not_nothing(settings):
    j = make(settings, [message([tool_block("knowledge_search", {"query": "x"}, block_id=f"t{i}")], "tool_use")
                        for i in range(5)])
    report = await j.recruiter.recruit("Researcher", "Keep looking forever.", max_turns=2)
    assert "ran out of turns" in report
    await j.http.aclose()


async def test_recruit_agent_tool_is_registered_and_reachable(settings):
    j = make(settings, [message([text_block("Done.")])])
    assert "recruit_agent" in TOOLS_BY_NAME and TOOLS_BY_NAME["recruit_agent"].approval is False
    result = await recruit_agent(j, RecruitAgentIn(role="Tester", brief="Say hello.", tools=None, max_turns=3))
    assert result["role"] == "Tester" and "Done" in result["report"]
    await j.http.aclose()
