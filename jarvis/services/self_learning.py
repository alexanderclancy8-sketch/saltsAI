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

from .. import access

log = logging.getLogger(__name__)
LAST_ID_KEY = "self_learning:last_transcript_id"
BATCH_LIMIT = 400  # generous - a busy day's worth of turns, kept bounded so the prompt doesn't balloon
PROMPT_CHAR_BUDGET = 120_000  # transcript text per reflection; anything beyond waits for the next one
MAX_ROW_CHARS = 4_000  # one enormous turn (a pasted document) must not crowd out everything else
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
        # Only reflect on what actually fits the prompt budget, and only advance the cursor past what was shown:
        # cutting the joined text at the budget used to drop the tail silently and then mark it as reflected on.
        newest = j.db.query_one("SELECT MAX(id) AS latest FROM transcript")["latest"]
        lines: list[str] = []
        used = 0
        included = 0
        for r in rows:
            line = f"{r['role']}: {r['text'][:MAX_ROW_CHARS]}"
            if lines and used + len(line) + 1 > PROMPT_CHAR_BUDGET:
                break
            lines.append(line)
            used += len(line) + 1
            included += 1
        batch_end = rows[included - 1]["id"]
        transcript = "\n".join(lines)
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
            "don't call remember at all - just say so briefly. Something durable about ONE customer or site (how they like "
            "to be contacted, a recurring access problem) goes in entity_note_propose instead - it only becomes a note when a "
            "person accepts it.\n\n"
            f"<transcript>\n{transcript}\n</transcript>"
            f"{quality_part}"
        )
        # The transcript it reads holds what a manager typed too, so the reflection runs with a manager's access, never the owner's: text
        # in a chat message can't make it read the owner-only FSM data (finance, pay, HR) or use an owner-only tool.
        who = access.current_caller.set(access.REFLECTION_CALLER)
        try:
            reply = await j.brain.ask(prompt, "typed")
        finally:
            access.current_caller.reset(who)
        if newest_turn is not None:
            try:
                j.quality.mark_reflected(newest_turn, newest_event, reply if brief else "")
            except Exception as e:  # noqa: BLE001
                log.warning("conversation quality watermark not saved: %s", e)
        # brain.ask() itself writes this reflection's own prompt and reply into the same transcript table -
        # advance past those too (the real current max, not just rows[-1]), or the next reflection would find
        # its own last turn waiting for it and reflect on itself forever. The exception is a batch that didn't
        # cover everything waiting (a very busy day, or a transcript over the prompt budget): jumping to the max
        # would silently skip the turns that weren't shown, so stop at the end of the batch and let the next run
        # pick up the rest.
        if included < len(rows) or (len(rows) >= BATCH_LIMIT and newest > rows[-1]["id"]):
            cursor = batch_end
        else:
            cursor = j.db.query_one("SELECT MAX(id) AS latest FROM transcript")["latest"]
        j.db.set_kv(LAST_ID_KEY, str(cursor))
        if j.settings.weekly_digest_enabled:  # a routine summary: stored for the weekly digest, never sent
            j.db.add_digest_item("self_learning_summary", "Self-reflection on recent conversations",
                                 (reply or "")[:500], status="reflected")
        return reply
