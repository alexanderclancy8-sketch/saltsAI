"""Daily rhythm: the morning briefing and the end-of-day wrap-up.

Two intentional, scheduled posts (not "checks": a check that finds nothing posts nothing, these always say something):

* **Short enough to say in a minute.** The text is written for speech, so it has a word budget - at a relaxed 160 words
  a minute, ``MAX_WORDS`` (150) is under a minute. The prompt asks for 130 to 150; whatever comes back is measured and,
  if it is over, the model is asked once to cut it, and if it is still over it is trimmed to whole sentences. The budget
  is enforced here, in code, not just asked for.
* **Posted twice, the same text.** To the console (``Proactive.scheduled``: into the conversation, so it is waiting when
  the owner opens the console, and pushed to any open session unless that session has muted Jarvis speaking up) and to
  Teams (``Notifier.send_owner_update``, as before; the owner's email copy is unchanged).
* **Switchable and re-timed in Settings > Schedules.** ``briefing_enabled`` / ``briefing_cron`` and ``wrapup_enabled`` /
  ``wrapup_cron`` (UK time). Defaults: 09:00 and 17:30, Monday to Friday.

It only tells the owner something: it never approves, sends to a customer or changes anything.
"""

from __future__ import annotations

import logging
import re

from ..brain import llm

log = logging.getLogger(__name__)

MAX_WORDS = 150           # hard ceiling for what goes out
TARGET_WORDS = "130 to 150"
SPOKEN_WORDS_PER_MINUTE = 160
MAX_SPOKEN_SECONDS = 60.0

WORD_BUDGET_RULE = (f"LENGTH IS A HARD LIMIT: {TARGET_WORDS} words, never more than {MAX_WORDS}. It is read aloud and "
                    "must take under a minute, so pick the few things that matter most and leave the rest out.")

_WORD = re.compile(r"\S+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def word_count(text: str) -> int:
    return len(_WORD.findall(text or ""))


def spoken_seconds(text: str) -> float:
    """How long ``text`` takes to say at a relaxed speaking pace."""
    return word_count(text) * 60.0 / SPOKEN_WORDS_PER_MINUTE


def fit_to_minute(text: str, max_words: int = MAX_WORDS) -> str:
    """``text`` unchanged if it is within the word budget, else cut after the last whole sentence that fits (or at the
    word limit if there is no sentence break, ending with a full stop)."""
    text = (text or "").strip()
    if word_count(text) <= max_words:
        return text
    kept: list[str] = []
    used = 0
    for sentence in _SENTENCE_END.split(text):
        n = word_count(sentence)
        if used + n > max_words:
            break
        kept.append(sentence)
        used += n
    if kept:
        return " ".join(kept).strip()
    return " ".join(_WORD.findall(text)[:max_words]).rstrip(",;:- ") + "."


async def write_short(client, settings, *, system: str, prompt: str, effort: str = "medium") -> str:
    """Write the briefing/wrap-up and make sure it fits in a minute of speech."""
    text = await llm.write(client, settings, system=system, prompt=prompt, effort=effort)
    if word_count(text) <= MAX_WORDS:
        return text.strip()
    log.info("Daily rhythm text came back at %d words; asking for a shorter one", word_count(text))
    try:
        text = await llm.write(
            client, settings, system=system, effort=effort,
            prompt=f"{prompt}\n\nYour draft was {word_count(text)} words, which is too long to say in a minute. Write it again "
                   f"in {TARGET_WORDS} words (absolutely no more than {MAX_WORDS}), keeping only what matters most:\n\n{text}")
    except Exception:  # noqa: BLE001 - the trim below still gives a usable text
        log.exception("Shortening the daily rhythm text failed; trimming it instead")
    return fit_to_minute(text)


async def deliver(j, key: str, title: str, text: str) -> dict[str, str]:
    """Post one daily-rhythm text to the console and to Teams. Each leg is independent: one failing never stops the other,
    and nothing here raises. Returns what happened, for logs and tests."""
    outcome: dict[str, str] = {}
    try:
        console = await j.proactive.scheduled(f"daily:{key}", title, text)
        outcome["console"] = "posted" if console.get("delivered") else f"held back: {console.get('reason')}"
    except Exception:  # noqa: BLE001
        log.exception("Posting the %s to the console failed", key)
        outcome["console"] = "failed"
    try:
        outcome["teams"] = await j.notifier.send_owner_update(title, text, channels=("teams", "email"), importance="info")
    except Exception:  # noqa: BLE001
        log.exception("Sending the %s to Teams failed", key)
        outcome["teams"] = "failed"
    return outcome
