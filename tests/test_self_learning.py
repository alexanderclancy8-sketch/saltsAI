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
