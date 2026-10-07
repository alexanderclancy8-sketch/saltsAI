"""Console redesign, Phase 2: what sits around a reply - the working line, the source and time line, the button for the
matching pop-up and up to two follow-up questions.

All of it is built from what really happened in the turn (the tool events both brains publish), never from text the
model wrote, and none of it can write or send anything: the approval gate is untouched.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from jarvis.brain.tools import NextStepsIn, TOOLS_BY_NAME, dispatch
from jarvis.brain.trace import PANELS, clean_follow_ups
from jarvis.core import Jarvis
from jarvis.events import quiet_turn
from jarvis.services.recruiter import NO_RECURSE, Recruiter
from tests.fakes import FakeClient, message, text_block, tool_block


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def reply_of(events):
    return next(e["data"] for e in reversed(events) if e["type"] == "reply")


async def run(settings, script, text="What's on today?"):
    j = Jarvis(settings, client=FakeClient(script))
    q = j.bus.subscribe()
    await j.brain.ask(text, "typed")
    events = drain(q)
    await j.http.aclose()
    return j, events


# ------------------------------------------------------------------ working line, source and time
async def test_a_tool_call_names_its_source_and_offers_the_matching_pop_up(settings):
    _, events = await run(settings, [message([tool_block("fsm_jobs", {})], "tool_use"),
                                     message([text_block("Four jobs are on today.")])])
    # The working line is driven by the real tool events, in order, before the words.
    types = [e["type"] for e in events]
    tool_start = next(e for e in events if e["type"] == "tool" and e["data"]["state"] == "start")
    assert tool_start["data"]["label"] == "Checking jobs in Salts FSM" and tool_start["data"]["name"] == "fsm_jobs"
    assert types.index("tool") < types.index("reply")
    r = reply_of(events)
    assert r["sources"] == ["Salts FSM (demo data)"]  # the test app runs on demo FSM data, and says so
    assert r["panel"] == "ops" and r["panel_title"] == "Ops"
    assert isinstance(r["elapsed_ms"], int) and r["elapsed_ms"] >= 0
    assert "follow_ups" not in r


async def test_no_tools_means_no_source_and_no_pop_up(settings):
    _, events = await run(settings, [message([text_block("Morning.")])], "Morning")
    r = reply_of(events)
    assert "sources" not in r and "panel" not in r and "follow_ups" not in r and "elapsed_ms" in r


async def test_sources_are_deduplicated_in_order_and_the_pop_up_is_the_most_used(settings):
    _, events = await run(settings, [
        message([tool_block("fsm_jobs", {}, "t1"), tool_block("email_inbox", {}, "t2")], "tool_use"),
        message([tool_block("staff_today", {}, "t3")], "tool_use"),
        message([text_block("Done.")]),
    ])
    r = reply_of(events)
    assert r["sources"] == ["Salts FSM (demo data)", "Outlook (demo data)"]
    assert r["panel"] == "ops"  # two ops tools to one comms tool


async def test_an_unlisted_tool_leaves_no_source(settings):
    _, events = await run(settings, [message([tool_block("remember", {"fact": "Dan prefers mornings"})], "tool_use"),
                                     message([text_block("Noted.")])])
    r = reply_of(events)
    assert "sources" not in r and "panel" not in r


async def test_something_queued_for_approval_points_at_approvals_whatever_else_was_read(settings):
    j, events = await run(settings, [
        message([tool_block("fsm_jobs", {}, "t1"),
                 tool_block("email_send", {"to": ["someone@example.com"], "subject": "Hi", "body": "Hello"}, "t2")],
                "tool_use"),
        message([text_block("I've queued the email for your approval.")]),
    ])
    assert reply_of(events)["panel"] == "approvals"
    assert j.db.pending_actions()  # it queued; the extras themselves approved nothing


async def test_a_background_turn_leaves_no_trace_and_no_events(settings):
    j = Jarvis(settings, client=FakeClient([message([tool_block("fsm_jobs", {})], "tool_use"),
                                            message([text_block("Quiet.")])]))
    q = j.bus.subscribe()
    token = quiet_turn.set(True)
    try:
        await j.brain.ask("check", "typed")
    finally:
        quiet_turn.reset(token)
    assert [e["type"] for e in drain(q) if e["type"] in ("thinking", "delta", "tool", "reply")] == []
    assert j.trace.active is False
    await j.http.aclose()


# ------------------------------------------------------------------ follow-ups
async def test_offer_next_steps_puts_up_to_two_follow_ups_on_the_reply(settings):
    _, events = await run(settings, [
        message([text_block("Four jobs are on today."),
                 tool_block("offer_next_steps", {"follow_ups": ["Who is running late?", "  Any overdue jobs?  "]})],
                "tool_use"),
        message([text_block("")]),
    ])
    r = reply_of(events)
    assert r["follow_ups"] == ["Who is running late?", "Any overdue jobs?"]
    assert r["text"] == "Four jobs are on today."
    # The bookkeeping tool has no label, so the working line never names it.
    assert all(e["data"]["label"] != "" or e["data"]["name"] == "offer_next_steps" for e in events if e["type"] == "tool")


async def test_offer_next_steps_can_name_a_pop_up_when_it_is_not_obvious(settings):
    _, events = await run(settings, [
        message([text_block("The numbers look healthy."), tool_block("offer_next_steps", {"panel": "finance"})], "tool_use"),
        message([text_block("")]),
    ])
    assert reply_of(events)["panel"] == "finance"


def test_the_schema_allows_two_follow_ups_and_known_pop_ups_only():
    NextStepsIn(follow_ups=["a?", "b?"], panel="ops")
    for bad in ({"follow_ups": ["a", "b", "c"]}, {"follow_ups": [""]}, {"follow_ups": ["x" * 91]},
                {"panel": "settings"}, {"panel": "wiring"}):
        with pytest.raises(ValidationError):
            NextStepsIn(**bad)
    assert NextStepsIn(panel="").panel is None
    assert clean_follow_ups(["Same?", "same?", "Other?", "Third?"]) == ["Same?", "Other?"]
    # (+ "activity": a reply to "what did you do today?" may offer the What Jarvis did pop-up)
    assert set(PANELS) == {"approvals", "activity", "comms", "issues", "health", "ops", "fleet", "finance", "presence", "upcoming"}


async def test_offer_next_steps_is_not_an_approval_and_changes_nothing(settings):
    tool = TOOLS_BY_NAME["offer_next_steps"]
    assert tool.approval is False
    j = Jarvis(settings, client=FakeClient())
    q = j.bus.subscribe()
    j.trace.begin()
    result = await dispatch(j, tool, NextStepsIn(follow_ups=["Anything else?"]))
    assert "End your turn" in result
    assert j.db.pending_actions() == [] and drain(q) == []  # no event, nothing queued
    await j.http.aclose()


async def test_offer_next_steps_outside_a_turn_is_ignored(settings):
    j = Jarvis(settings, client=FakeClient())
    j.trace.offer("ops", ["Stray?"])
    j.trace.begin()
    assert j.trace.finish().get("follow_ups") is None
    await j.http.aclose()


def test_recruited_sub_agents_cannot_attach_buttons_to_the_owners_reply(settings):
    assert "offer_next_steps" in NO_RECURSE
    j = Jarvis(settings, client=FakeClient())
    assert all(t.name != "offer_next_steps" for t in Recruiter(j)._tool_set(None))


def test_every_panel_a_tool_can_point_at_exists_in_the_console():
    from pathlib import Path

    hud = (Path(__file__).resolve().parent.parent / "jarvis" / "web" / "hud.js").read_text(encoding="utf-8")
    pops = hud.split("const POPS = [")[1].split("]")[0]
    for name in PANELS:
        assert f'"{name}"' in pops
    from jarvis.brain.tools import TOOLS
    from jarvis.brain.trace import _TOOL_INFO

    names = {t.name for t in TOOLS}
    assert set(_TOOL_INFO) - names <= {"web_search", "web_fetch", "WebSearch", "WebFetch"}  # no typos in the mapping
    assert {p for _, p in _TOOL_INFO.values() if p} <= set(PANELS)
