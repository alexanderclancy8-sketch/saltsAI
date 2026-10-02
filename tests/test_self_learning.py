"""Self-reflection: Jarvis looks back over recent conversation and remembers anything durable, without
being told to - using the ordinary remember tool and conversation loop, never a second memory mechanism."""

from __future__ import annotations

from jarvis.core import Jarvis
from jarvis.services.self_learning import LAST_ID_KEY
from tests.fakes import FakeClient, message, text_block, tool_block


def make(settings, script=None):
    return Jarvis(settings, client=FakeClient(script))


async def test_reflect_does_nothing_with_an_empty_transcript(settings):
    j = make(settings)
    reply = await j.self_learning.reflect()
    assert "nothing new" in reply.lower()
    assert j.brain.messages == []  # never asked the brain at all - nothing to reflect on
    await j.http.aclose()


async def test_reflect_lets_the_model_remember_things_from_the_transcript(settings):
    j = make(settings, [
        message([tool_block("remember", {"fact": "Alex prefers VAT figures quoted ex-VAT unless asked otherwise"})],
                "tool_use"),
        message([text_block("Remembered one thing from today's conversations, sir.")]),
    ])
    j.db.add_transcript("user", "Always show me figures ex-VAT unless I say otherwise")
    j.db.add_transcript("assistant", "Understood, ex-VAT it is.")

    reply = await j.self_learning.reflect()
    assert reply == "Remembered one thing from today's conversations, sir."
    memories = j.db.memories()
    assert len(memories) == 1 and "ex-VAT" in memories[0]["fact"]

    # the prompt it reflected on actually carried the real transcript, not a placeholder
    sent = j.brain.messages[0]["content"][-1]["text"]
    assert "Always show me figures ex-VAT" in sent and "self-reflection" in sent.lower()
    await j.http.aclose()


async def test_reflect_advances_the_high_water_mark_so_it_never_reprocesses(settings):
    j = make(settings, [message([text_block("Nothing worth keeping today.")])])
    j.db.add_transcript("user", "What's the weather like")
    j.db.add_transcript("assistant", "No idea, sir, I don't have a window.")

    await j.self_learning.reflect()
    last_id_after_first = j.db.get_kv(LAST_ID_KEY)
    assert last_id_after_first is not None

    # a second reflection with no new turns since should skip the brain entirely
    reply = await j.self_learning.reflect()
    assert "nothing new" in reply.lower()
    assert len(j.brain.messages) == 2  # still just the one exchange from the first reflection - no second call
    await j.http.aclose()


async def test_reflect_only_looks_at_turns_since_the_last_reflection(settings):
    j = make(settings, [
        message([text_block("Noted nothing new.")]),
        message([tool_block("remember", {"fact": "Kestrel Retail deals should loop in the business partner"})],
                "tool_use"),
        message([text_block("Remembered that one, sir.")]),
    ])
    j.db.add_transcript("user", "Old chat, already reflected on")
    await j.self_learning.reflect()

    j.db.add_transcript("user", "Kestrel Retail always needs the partner looped in on deals")
    reply = await j.self_learning.reflect()
    assert reply == "Remembered that one, sir."
    # only the new turn reached the second reflection's prompt, not the already-reflected-on one
    second_prompt = j.brain.messages[2]["content"][-1]["text"]
    assert "Kestrel Retail" in second_prompt and "Old chat" not in second_prompt
    await j.http.aclose()


async def test_reflect_does_not_skip_turns_that_did_not_fit_the_prompt(settings, monkeypatch):
    from jarvis.services import self_learning as sl

    monkeypatch.setattr(sl, "PROMPT_CHAR_BUDGET", 250)  # room for two of the six ~97-character lines below
    j = make(settings, [message([text_block("First batch done.")]), message([text_block("Second batch done.")])])
    for i in range(1, 7):
        j.db.add_transcript("user", f"marker{i} " + "a" * 84)
    ids = [r["id"] for r in j.db.query("SELECT id FROM transcript ORDER BY id")]

    await j.self_learning.reflect()
    first_prompt = j.brain.messages[0]["content"][-1]["text"]
    assert "marker1" in first_prompt and "marker2" in first_prompt and "marker3" not in first_prompt
    assert j.db.get_kv(LAST_ID_KEY) == str(ids[1])  # cursor stops after what was actually shown

    await j.self_learning.reflect()
    second_prompt = j.brain.messages[2]["content"][-1]["text"]
    assert "marker3" in second_prompt and "marker1" not in second_prompt
    await j.http.aclose()


async def test_reflect_truncates_one_enormous_turn_instead_of_dropping_the_rest(settings):
    j = make(settings, [message([text_block("Nothing to keep.")])])
    j.db.add_transcript("user", "huge " + "z" * 50_000)
    j.db.add_transcript("user", "Remember the tail marker")
    await j.self_learning.reflect()
    prompt = j.brain.messages[0]["content"][-1]["text"]
    assert "tail marker" in prompt and len(prompt) < 10_000
    await j.http.aclose()


def test_remember_ignores_duplicates_and_bounds_the_fact(tmp_path):
    import pytest
    from pydantic import ValidationError

    from jarvis.brain.tools import RememberIn
    from jarvis.db import Database

    db = Database(tmp_path / "db")
    first = db.remember("Quote ex-VAT unless asked")
    assert db.remember("  quote EX-VAT   unless asked ") == first
    assert len(db.memories()) == 1
    assert db.remember("Loop the partner in on Kestrel deals") != first
    assert len(db.memories()) == 2

    with pytest.raises(ValidationError):
        RememberIn(fact="")
    with pytest.raises(ValidationError):
        RememberIn(fact="x" * 1001)
    assert RememberIn(fact="Quote ex-VAT").fact == "Quote ex-VAT"


async def test_reflect_does_not_skip_turns_beyond_one_batch(settings, monkeypatch):
    monkeypatch.setattr("jarvis.services.self_learning.BATCH_LIMIT", 2)
    j = make(settings, [message([text_block("Nothing yet.")]), message([text_block("Nothing else.")])])
    j.db.add_transcript("user", "First message")
    j.db.add_transcript("assistant", "Second message")
    j.db.add_transcript("user", "Third message")

    await j.self_learning.reflect()
    first_prompt = j.brain.messages[0]["content"][-1]["text"]
    assert "First message" in first_prompt and "Third message" not in first_prompt

    await j.self_learning.reflect()  # the turn that didn't fit the first batch is picked up, not lost
    second_prompt = j.brain.messages[2]["content"][-1]["text"]
    assert "Third message" in second_prompt and "First message" not in second_prompt
    # the first reflection's own (huge) prompt is never fed back into the next one
    assert "Scheduled self-reflection" not in second_prompt.split("<transcript>", 1)[1]
    await j.http.aclose()


async def test_remember_does_not_store_the_same_fact_twice(settings):
    j = make(settings, [
        message([tool_block("remember", {"fact": "Kestrel Retail deals need the partner looped in."})], "tool_use"),
        message([text_block("Done.")]),
        message([tool_block("remember", {"fact": "  kestrel retail deals need the  partner looped in "})], "tool_use"),
        message([text_block("Done.")]),
    ])
    await j.brain.ask("Remember that Kestrel Retail deals need the partner looped in.", "typed")
    await j.brain.ask("Remember it again.", "typed")
    assert len(j.db.memories()) == 1
    first_id = j.db.memories()[0]["id"]
    assert j.db.remember("KESTREL RETAIL deals need the partner looped in") == first_id  # same row, no new one
    assert j.db.remember("A genuinely different fact") != first_id
    assert len(j.db.memories()) == 2
    await j.http.aclose()
