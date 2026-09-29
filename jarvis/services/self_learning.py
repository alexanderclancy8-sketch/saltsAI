"""Continuous, low-key learning: on a schedule, Jarvis looks back over what's been said since the last time
it checked and remembers anything durable worth keeping - a stated preference, a correction to something it
got wrong, a recurring pattern - without needing to be told "remember that" every time.

This reuses the existing `remember` tool and memory store rather than inventing a second one: the prompt
below just asks the ordinary conversational loop to review a stretch of transcript and call `remember` for
whatever's worth it, exactly as if the owner had asked it to. Nothing here writes to Salts FSM, sends
anything, or changes external state - it only ever adds to Jarvis's own memory, so it needs no approval gate.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)
LAST_ID_KEY = "self_learning:last_transcript_id"
BATCH_LIMIT = 400  # generous - a busy day's worth of turns, kept bounded so the prompt doesn't balloon


class SelfLearning:
    def __init__(self, j):
        self.j = j

    async def reflect(self) -> str:
        j = self.j
        last_id = int(j.db.get_kv(LAST_ID_KEY) or 0)
        rows = j.db.query("SELECT * FROM transcript WHERE id > ? ORDER BY id LIMIT ?", (last_id, BATCH_LIMIT))
        if not rows:
            return "Nothing new since the last reflection."
        transcript = "\n".join(f"{r['role']}: {r['text']}" for r in rows)
        prompt = (
            "[Scheduled self-reflection - nobody typed this, it's you looking back over recent conversations]\n"
            "Here's everything said since your last reflection. Look for anything durable worth remembering "
            "long-term: a stated preference, a correction to something you got wrong, a recurring pattern, a "
            "fact about how the business or a person works - that isn't already covered by what you already "
            "remember. Call `remember` once for each thing worth keeping, in your own words. Don't remember "
            "one-off requests, small talk, or anything already in your memory. If nothing durable stands out, "
            "don't call remember at all - just say so briefly.\n\n"
            f"<transcript>\n{transcript[:120000]}\n</transcript>"
        )
        reply = await j.brain.ask(prompt, "typed")
        # brain.ask() itself writes this reflection's own prompt and reply into the same transcript table -
        # advance past those too (the real current max, not just rows[-1]), or the next reflection would find
        # its own last turn waiting for it and reflect on itself forever.
        latest = j.db.query_one("SELECT MAX(id) AS latest FROM transcript")["latest"]
        j.db.set_kv(LAST_ID_KEY, str(latest))
        return reply
