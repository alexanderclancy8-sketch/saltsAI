"""Tidying spoken messages before they reach the model.

Two speech-to-text problems are handled here, on the server, as a backstop to what web/hud.js already does:

* Stutter: some engines hand over growing partial transcripts and they end up joined into one message
  ("I I I just I just asked I just asked you"). `collapse_cumulative()` keeps only the longest, final version
  ("I just asked you"), so the model - and the stored transcript - see what was actually said.
* Echo: without headphones the mic hears Jarvis's own voice. `is_echo_of_reply()` spots a "message" that is just
  Jarvis's last reply coming back in, so `screen_voice()` can drop it instead of answering Jarvis with Jarvis.

Only spoken (mode "voice") messages are touched; typed text is never altered or dropped.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

log = logging.getLogger("jarvis")

ECHO_WINDOW_S = 30.0       # a heard message is only compared with a reply Jarvis gave this recently
ECHO_MIN_WORDS = 3         # a bare "yes" / "go on" is never treated as echo
ECHO_SIMILARITY = 0.85     # fraction of the heard words found, in order, inside the last reply

_NON_WORD = re.compile(r"[^a-z0-9]+")


def _key(word: str) -> str:
    return _NON_WORD.sub("", word.lower())


def collapse_cumulative(text: str) -> str:
    """Collapses a stuttering cumulative transcript to its longest (final) version.

    A cumulative transcript is a run of growing prefixes of the same sentence: each chunk starts with the whole of the
    one before it. Case and punctuation are ignored when comparing. Ordinary speech is left alone: plain repetition
    ("no no no", "very very good") isn't growth, so it only counts when a repeated chunk is at least two words long
    and the pattern shows up twice (or the repeated chunk is three or more words)."""
    words = str(text or "").split()
    keys = [_key(w) for w in words]
    n = len(words)
    seg, i = 0, 1                 # `seg` is where the latest chunk starts
    restarts = longest = 0
    while i < n:
        size = i - seg
        if keys[i:i + size] == keys[seg:i]:   # the latest chunk begins again here, so it is being extended
            restarts += 1
            longest = max(longest, size)
            seg, i = i, i + size
        else:
            i += 1
    if longest >= 2 and (restarts >= 2 or longest >= 3):
        return " ".join(words[seg:])
    return " ".join(words)


def is_echo_of_reply(text: str, reply: str) -> bool:
    """True if `text` (heard on the mic) is mostly just words from Jarvis's last `reply` coming back in order."""
    heard = [k for k in (_key(w) for w in str(text or "").split()) if k]
    said = [k for k in (_key(w) for w in str(reply or "").split()) if k]
    if len(heard) < ECHO_MIN_WORDS or not said:
        return False
    blocks = SequenceMatcher(None, heard, said, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) / len(heard) >= ECHO_SIMILARITY


def _last_reply(j, now: datetime) -> str:
    """Jarvis's most recent reply if it was given within ECHO_WINDOW_S, else an empty string."""
    try:
        row = j.db.query_one("SELECT created_at, text FROM transcript WHERE role = 'assistant' ORDER BY id DESC LIMIT 1")
        if not row:
            return ""
        said_at = datetime.fromisoformat(row["created_at"])
        if said_at.tzinfo is None:
            said_at = said_at.replace(tzinfo=timezone.utc)
        return row["text"] if 0 <= (now - said_at).total_seconds() <= ECHO_WINDOW_S else ""
    except Exception as e:  # noqa: BLE001 - screening must never break a chat turn
        log.warning("echo check skipped: %s", e)
        return ""


def screen_voice(j, text: str, mode: str, now: datetime | None = None) -> str | None:
    """The text to send to the model for this message, or None if it should be ignored as Jarvis's own voice.

    Typed messages come back unchanged. A dropped message publishes "stopped" so the display goes back to idle."""
    if mode != "voice":
        return text
    cleaned = collapse_cumulative(text)
    now = now or datetime.now(timezone.utc)
    if is_echo_of_reply(cleaned, _last_reply(j, now)):
        log.info("Ignored a spoken message that is an echo of Jarvis's last reply")
        j.bus.publish("stopped", {"stopped": False})
        return None
    return cleaned or text
