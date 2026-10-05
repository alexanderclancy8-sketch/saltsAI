"""Suggestion wording is LLM-composed display text only: detection and structured data are unchanged,
and any LLM failure falls back to the original template text."""

import asyncio
from datetime import date, datetime, time

from jarvis.brain import llm
from jarvis.core import Jarvis
from jarvis.integrations import fsm as fsm_module
from jarvis.services import suggestions as sug
from jarvis.services import tracking as tracking_module
from tests.fakes import FakeClient


def make(settings):
    return Jarvis(settings, client=FakeClient())


def script_wording(j, candidates, transform):
    """Make the fake client's structured-output call return `transform(title)` for every candidate."""
    j.client.beta.messages.parse_result = {
        "lines": [{"id": str(i), "text": transform(c["title"])} for i, c in enumerate(candidates)]}


def parse_calls(j):
    """Only the wording-composition calls - _candidates() also triggers unrelated pre-existing LLM calls
    (e.g. j.ooh.calls()'s out-of-hours report extraction), which aren't what this counts."""
    return sum(1 for c in j.client.beta.messages.calls if c.get("output_format") is sug.WordedLines)


async def test_fallback_to_template_text_when_llm_output_unusable(settings):
    j = make(settings)  # the fake returns no structured output, so the LLM path fails
    candidates = await j.suggestions._candidates()  # noqa: SLF001
    assert candidates
    current = await j.suggestions.sweep(announce=False)
    titles = {c["key"]: c["title"] for c in candidates}
    assert {s["key"]: s["title"] for s in current} == titles
    await j.http.aclose()


async def test_fallback_when_llm_call_raises_or_times_out(settings, monkeypatch):
    # The demo FSM places engineers from the wall clock (positions drift every 5 minutes, job statuses follow the
    # time of day), so "nearest engineer" for J24099 changed if the clock crossed a boundary between the two
    # _candidates()/sweep() calls below. Pin the clock so the comparison is about wording fallback only.
    frozen = datetime.combine(date.today(), time(10, 7))

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(fsm_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(tracking_module, "datetime", FrozenDateTime)
    j = make(settings)
    candidates = await j.suggestions._candidates()  # noqa: SLF001

    async def boom(*a, **k):
        raise RuntimeError("API down")

    monkeypatch.setattr(llm, "structured", boom)
    current = await j.suggestions.sweep(announce=False)
    assert {s["key"]: s["title"] for s in current} == {c["key"]: c["title"] for c in candidates}

    j2 = make(settings)  # fresh state (no retry cool-down) - now a hang that hits the timeout

    async def hang(*a, **k):
        # Much longer than WORDING_TIMEOUT (0.05 s), so the wording call is cut off by the timeout. The out-of-hours call source
        # also hits this patched llm.structured and has no timeout, so it waits the whole hang out: 30 s here made this the
        # slowest test in the suite for no extra coverage.
        await asyncio.sleep(2)

    monkeypatch.setattr(llm, "structured", hang)
    monkeypatch.setattr(sug, "WORDING_TIMEOUT", 0.05)
    current = await j2.suggestions.sweep(announce=False)
    assert {s["key"]: s["title"] for s in current} == {c["key"]: c["title"] for c in candidates}
    await j.http.aclose()
    await j2.http.aclose()


async def test_composed_wording_changes_only_the_title(settings):
    j = make(settings)
    candidates = await j.suggestions._candidates()  # noqa: SLF001
    script_wording(j, candidates, lambda t: "Right then - " + t)
    current = await j.suggestions.sweep(announce=False)
    by_key = {s["key"]: s for s in current}
    assert set(by_key) == {c["key"] for c in candidates}  # what fires is unchanged
    for c in candidates:
        row = by_key[c["key"]]
        assert row["title"] == "Right then - " + c["title"]
        assert (row["detail"], row["prompt"], row["priority"]) == (c["detail"], c["prompt"], c["priority"])
    await j.http.aclose()


async def test_detection_output_is_independent_of_the_llm(settings):
    j = make(settings)
    before = await j.suggestions._candidates()  # noqa: SLF001
    script_wording(j, before, lambda t: "Noted: " + t)
    await j.suggestions.sweep(announce=False)
    after = await j.suggestions._candidates()  # noqa: SLF001
    assert after == before  # _candidates never touches the wording
    await j.http.aclose()


async def test_wording_is_cached_on_the_facts(settings):
    j = make(settings)
    candidates = await j.suggestions._candidates()  # noqa: SLF001
    script_wording(j, candidates, lambda t: "Right then - " + t)
    await j.suggestions.sweep(announce=False)
    calls = parse_calls(j)
    assert calls == 1  # every suggestion batched into one call
    again = await j.suggestions.sweep(announce=False)
    assert parse_calls(j) == calls  # unchanged facts: nothing regenerated
    assert all(s["title"].startswith("Right then - ") for s in again)
    await j.http.aclose()


async def test_invented_figures_are_rejected(settings):
    j = make(settings)
    candidates = await j.suggestions._candidates()  # noqa: SLF001
    script_wording(j, candidates, lambda t: t + " That's 9999 in all.")
    current = await j.suggestions.sweep(announce=False)
    assert {s["key"]: s["title"] for s in current} == {c["key"]: c["title"] for c in candidates}
    await j.http.aclose()


def test_acceptable_guard():
    assert sug._acceptable("Invoice 3 jobs, £1,200 + VAT?", "Invoice 3 completed jobs (£1,200 + VAT)?", "")
    assert not sug._acceptable("Invoice 3 jobs?", "Invoice 3 completed jobs (£1,200 + VAT)?", "")  # dropped a figure
    assert not sug._acceptable("Invoice 4 jobs, £1,200?", "Invoice 3 completed jobs (£1,200 + VAT)?", "")  # new figure
    assert not sug._acceptable("", "x", "")
    assert not sug._acceptable("one\ntwo", "x", "")
