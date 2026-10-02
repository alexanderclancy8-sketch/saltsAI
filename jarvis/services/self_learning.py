"""Continuous, low-key learning: on a schedule, Jarvis looks back over what's been said since the last time
it checked and remembers anything durable worth keeping - a stated preference, a correction to something it
got wrong, a recurring pattern - without needing to be told "remember that" every time.

This reuses the existing `remember` tool and memory store rather than inventing a second one: the prompt
below just asks the ordinary conversational loop to review a stretch of transcript and call `remember` for
whatever's worth it, exactly as if the owner had asked it to. The same prompt also carries the conversation-quality
measurements and any replies the owner marked wrong (services/conversation_quality.py), and asks for concrete prompt
or memory improvements - prompt changes are only ever proposals in its reply, which the weekly quality summary
repeats; nothing edits the prompt. Nothing here writes to Salts FSM, sends
anything, or changes external state - it only ever adds to Jarvis's own memory, so it needs no approval gate.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)
LAST_ID_KEY = "self_learning:last_transcript_id"
BATCH_LIMIT = 400  # generous - a busy day's worth of turns, kept bounded so the prompt doesn't balloon
SELF_PROMPT_TAG = "[Scheduled self-reflection"  # start of the prompt this service sends (and so of its transcript row)


class SelfLearning:
    def __init__(self, j):
        self.j = j

    async def reflect(self) -> str:
        j = self.j
        last_id = int(j.db.get_kv(LAST_ID_KEY) or 0)
        # Earlier reflections' own prompts (each carries a whole transcript) are never fed back in.
        rows = j.db.query("SELECT * FROM transcript WHERE id > ? AND NOT (role = 'user' AND text LIKE ?) "
                          "ORDER BY id LIMIT ?", (last_id, SELF_PROMPT_TAG + "%", BATCH_LIMIT))
        if not rows:
            return "Nothing new since the last reflection."
        newest = j.db.query_one("SELECT MAX(id) AS latest FROM transcript")["latest"]
        transcript = "\n".join(f"{r['role']}: {r['text']}" for r in rows)
        # Conversation-quality measurements and any replies the owner marked wrong since the last reflection.
        # Measurement only: never allowed to stop the reflection itself.
        try:
            brief, newest_turn, newest_event = j.quality.reflection_brief()
        except Exception as e:  # noqa: BLE001
            log.warning("conversation quality brief skipped: %s", e)
            brief, newest_turn, newest_event = "", None, None
        quality_part = (
            f"\n\n<conversation_quality>\n{brief[:20000]}\n</conversation_quality>\n"
            "Also look at the conversation-quality measurements above. Where the numbers or the flagged replies "
            "point at a pattern (slow replies, replies that broke the spoken format, echo, questions asked twice, "
            "replies the owner marked wrong), propose concrete fixes in your reply: for each, the exact wording "
            "of a change to your system prompt, or a fact worth keeping in memory. Call `remember` for a durable "
            "fact or preference; for a prompt change just write the proposed wording out - you can't edit your "
            "own prompt here, it is only a proposal for the owner to review. Skip this if nothing stands out."
        ) if brief else ""
        prompt = (
            f"{SELF_PROMPT_TAG} - nobody typed this, it's you looking back over recent conversations]\n"
            "Here's everything said since your last reflection. Look for anything durable worth remembering "
            "long-term: a stated preference, a correction to something you got wrong, a recurring pattern, a "
            "fact about how the business or a person works - that isn't already covered by what you already "
            "remember. Call `remember` once for each thing worth keeping, in your own words. Don't remember "
            "one-off requests, small talk, or anything already in your memory. If nothing durable stands out, "
            "don't call remember at all - just say so briefly.\n\n"
            f"<transcript>\n{transcript[:120000]}\n</transcript>"
            f"{quality_part}"
        )
        reply = await j.brain.ask(prompt, "typed")
        if newest_turn is not None:
            try:
                j.quality.mark_reflected(newest_turn, newest_event, reply if brief else "")
            except Exception as e:  # noqa: BLE001
                log.warning("conversation quality watermark not saved: %s", e)
        # brain.ask() itself writes this reflection's own prompt and reply into the same transcript table -
        # advance past those too (the real current max, not just rows[-1]), or the next reflection would find
        # its own last turn waiting for it and reflect on itself forever. The exception is a backlog bigger than
        # one batch: jumping to the max would silently skip the turns that didn't fit, so stop at the batch end
        # and let the next run pick up the rest.
        if len(rows) >= BATCH_LIMIT and newest > rows[-1]["id"]:
            cursor = rows[-1]["id"]
        else:
            cursor = j.db.query_one("SELECT MAX(id) AS latest FROM transcript")["latest"]
        j.db.set_kv(LAST_ID_KEY, str(cursor))
        if j.settings.weekly_digest_enabled:  # a routine summary: stored for the weekly digest, never sent
            j.db.add_digest_item("self_learning_summary", "Self-reflection on recent conversations",
                                 (reply or "")[:500], status="reflected")
        return reply
