"""Near-duplicate detection for incoming user messages.

Speech-to-text hiccups, a double press of the mic, or an impatient "did you get that?" often make the owner
send the same request twice within a few seconds. Redoing the work (or re-running tools) for the second copy is
wasteful, so the brain is told when a message is near-identical to the previous one and can check whether it's a
repeat instead. Purely advisory: the message is never dropped or altered, only annotated for the model.
"""

from __future__ import annotations

import re
import time
from difflib import SequenceMatcher

REPEAT_WINDOW_S = 60.0       # how long the previous message counts as "just sent"
REPEAT_SIMILARITY = 0.9      # SequenceMatcher ratio on normalised text at or above which it's a near-duplicate

_NON_WORD = re.compile(r"[^a-z0-9\s]+")


def normalise(text: str) -> str:
    return " ".join(_NON_WORD.sub(" ", str(text or "").lower()).split())


def similarity(a: str, b: str) -> float:
    """0..1 similarity of two messages, ignoring case, punctuation and spacing."""
    a, b = normalise(a), normalise(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


class RepeatDetector:
    """Remembers the previous message and says whether a new one is a near-identical repeat of it."""

    def __init__(self, window_s: float = REPEAT_WINDOW_S, threshold: float = REPEAT_SIMILARITY):
        self.window_s = window_s
        self.threshold = threshold
        self._last: tuple[str, float] | None = None

    def check(self, text: str, now: float | None = None) -> float | None:
        """Records `text` as the latest message. Returns how many seconds ago the near-identical previous message
        was sent, or None if this isn't a repeat."""
        now = time.monotonic() if now is None else now
        prev, self._last = self._last, (text, now)
        if prev is None:
            return None
        age = now - prev[1]
        if 0 <= age <= self.window_s and similarity(prev[0], text) >= self.threshold:
            return age
        return None


def repeat_note(age_s: float | None) -> str:
    """The line added under the message tag when `check()` flagged a repeat (empty string otherwise)."""
    if age_s is None:
        return ""
    return (f"[possible repeat: near-identical to the previous message, sent {int(age_s)}s ago - check whether "
            "they're asking again because they didn't get or hear your answer, rather than redoing the work]\n")
