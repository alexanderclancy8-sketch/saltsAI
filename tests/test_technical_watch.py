"""The fire & security technical/standards watch - deepens Jarvis's own expertise over time, separately
from the tax/employment-law regulatory watch it's modelled on. Keeps its own 'what's already known' memory
so the two don't tread on each other."""

from __future__ import annotations

from jarvis.brain.tools import RegWatchIn, TOOLS_BY_NAME, technical_watch
from jarvis.core import Jarvis
from tests.fakes import FakeClient, message, text_block


def make(settings, script=None):
    return Jarvis(settings, client=FakeClient(script))


async def test_technical_watch_researches_and_remembers_separately_from_the_law_watch(settings):
    j = make(settings, [message([text_block("BS 5839-1 remains current; nothing new this week.")])])
    text = await j.regwatch.technical(window="week", deliver=False)
    assert "BS 5839" in text
    assert j.db.get_kv("technical_watch_last") == text
    assert j.db.get_kv("regwatch_last", "") == ""  # doesn't collide with the tax/law watch's own memory
    await j.http.aclose()


async def test_technical_watch_tool_is_registered_and_reachable():
    assert "technical_watch" in TOOLS_BY_NAME
    tool = TOOLS_BY_NAME["technical_watch"]
    assert tool.approval is False  # read/research only, nothing to approve


async def test_technical_watch_tool_calls_through_to_the_service(settings):
    j = make(settings, [message([text_block("Nothing new to report.")])])
    result = await technical_watch(j, RegWatchIn(focus="BS 5266"))
    assert result["shown_on_display"] is True and "Nothing new" in result["update"]
    await j.http.aclose()
