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
PROMPT_CHAR_BUDGET = 120_000  # transcript text per reflection; anything beyond waits for the next one
MAX_ROW_CHARS = 4_000  # one enormous turn (a pasted document) must not crowd out everything else


class SelfLearning:
    def __init__(self, j):
        self.j = j

    async def reflect(self) -> str:
        j = self.j
        last_id = int(j.db.get_kv(LAST_ID_KEY) or 0)
        rows = j.db.query("SELECT * FROM transcript WHERE id > ? ORDER BY id LIMIT ?", (last_id, BATCH_LIMIT))
        if not rows:
            return "Nothing new since the last reflection."
        # Only reflect on what actually fits the prompt budget, and only advance the cursor past what was shown:
        # cutting the joined text at the budget used to drop the tail silently and then mark it as reflected on.
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
        prompt = (
            "[Scheduled self-reflection - nobody typed this, it's you looking back over recent conversations]\n"
            "Here's everything said since your last reflection. Look for anything durable worth remembering "
            "long-term: a stated preference, a correction to something you got wrong, a recurring pattern, a "
            "fact about how the business or a person works - that isn't already covered by what you already "
            "remember. Call `remember` once for each thing worth keeping, in your own words. Don't remember "
            "one-off requests, small talk, or anything already in your memory. If nothing durable stands out, "
            "don't call remember at all - just say so briefly.\n\n"
            f"<transcript>\n{transcript}\n</transcript>"
        )
        reply = await j.brain.ask(prompt, "typed")
        # brain.ask() itself writes this reflection's own prompt and reply into the same transcript table -
        # advance past those too (the real current max, not just rows[-1]), or the next reflection would find
        # its own last turn waiting for it and reflect on itself forever. But if this batch didn't cover
        # everything that was waiting (a very busy day), stop at the end of the batch so the remainder gets its
        # own reflection next time rather than being skipped.
        if included < len(rows) or len(rows) >= BATCH_LIMIT:
            j.db.set_kv(LAST_ID_KEY, str(batch_end))
        else:
            latest = j.db.query_one("SELECT MAX(id) AS latest FROM transcript")["latest"]
            j.db.set_kv(LAST_ID_KEY, str(latest))
        return reply
