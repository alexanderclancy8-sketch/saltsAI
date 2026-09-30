"""Learned reply suggestions for the typed chat box.

Jarvis notices the short replies the owner keeps typing ("yes", "yes do that", "go ahead") and, once one has been
used a few times in a similar situation, offers it as greyed-out text in the input so a Right Arrow accepts it.

How it works (deliberately simple, explainable, and local - nothing here leaves the Jarvis database):
- Learning: each message the owner *types in the chat box* is normalised (lower-case, punctuation stripped,
  whitespace collapsed) and counted against the *kind of thing Jarvis had just said* (its "context"):
  `offer` ("Shall I...?"), `question` (any other question), `approval` (something waiting for approval) or
  `statement`; `none` when there was no earlier reply. Repeats bump a count and refresh a recency-weighted score.
- Matching: prefer a reply learned in the same context; otherwise fall back to the most common replies overall.
  A reply is only offered once it has been used `MIN_USES` times and its recency-weighted score is still
  above `MIN_SCORE` (old habits fade). When the owner has typed something, only replies starting with it match.
- Privacy/safety: spoken messages and anything that looks sensitive are never stored. The store is capped at
  `MAX_ROWS`. `forget()` / `clear()` remove learned replies, and the `reply_suggestions_enabled` setting turns the
  whole thing off. A suggestion is only ever text for the input box - it never sends anything and has no
  connection to the approval gate.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from ..db import now_iso

log = logging.getLogger(__name__)

MIN_USES = 3            # a reply must have been used this many times before it is ever suggested
MIN_SCORE = 1.0         # ...and its recency-weighted score (each use = 1, halving every HALF_LIFE_DAYS) must stay this high
HALF_LIFE_DAYS = 45.0   # recency weighting: a use counts half as much every this many days
MAX_ROWS = 300          # cap on learned (reply, context) pairs; the weakest are dropped first
MAX_WORDS = 12          # only short replies are learned - long messages are requests, not habits
MAX_CHARS = 80
CONTEXT_TAIL = 400      # how much of Jarvis's last reply is looked at to work out the context

_SPACE = re.compile(r"\s+")
_NOT_WORD = re.compile(r"[^a-z0-9\s]")
_OFFER = re.compile(
    r"\b(shall i|should i|would you like me to|do you want me to|want me to|shall we|should we|"
    r"want to go ahead|go ahead and)\b", re.I)
_APPROVAL = re.compile(r"\b(approv\w*|queued|awaiting your|waiting for your|needs? your (?:ok|go-ahead|sign-off))\b", re.I)
# Anything that might be a secret, code, contact detail or identifier is never stored.
_SENSITIVE = re.compile(
    r"(pass(?:word|code|wd)?|\bpin\b|token|secret|api[\s_-]?key|credential|\bcvv\b|iban|sort\s*code|"
    r"account\s*(?:no|number|num)|card\s*(?:no|number)|access\s*code|alarm\s*code|door\s*code|key\s*code|"
    r"\bsk-|bearer|https?://|www\.|@)", re.I)
_LONG_NUMBER = re.compile(r"\d(?:[\s\-]?\d){3,}")  # 4+ digits, however separated: PINs, codes, card/phone numbers


def normalise(text: str) -> str:
    """Case, punctuation and whitespace-insensitive form of a reply, used to count repeats."""
    return _SPACE.sub(" ", _NOT_WORD.sub("", (text or "").lower().replace("'", ""))).strip()


def classify_context(last_reply: str | None) -> str:
    """The kind of thing Jarvis last said: offer | question | approval | statement | none."""
    text = (last_reply or "").strip()
    if not text:
        return "none"
    tail = text[-CONTEXT_TAIL:].rstrip(" \t\r\n\"'*_)`")
    if tail.endswith("?"):
        # Look at the last sentence only, so "I've done X. Shall I do Y?" is an offer.
        last_sentence = re.split(r"(?<=[.!?])\s+|\n+", tail[:-1])[-1]
        return "offer" if _OFFER.search(last_sentence) else "question"
    return "approval" if _APPROVAL.search(tail) else "statement"


def is_sensitive(text: str) -> bool:
    return bool(_SENSITIVE.search(text) or _LONG_NUMBER.search(text))


def learnable(text: str) -> bool:
    """Short, single-line, non-sensitive text worth counting."""
    text = (text or "").strip()
    if not text or len(text) > MAX_CHARS or "\n" in text or len(text.split()) > MAX_WORDS:
        return False
    return bool(normalise(text)) and not is_sensitive(text)


def _decayed(score: float, last_used: str, now: datetime) -> float:
    try:
        then = datetime.fromisoformat(last_used)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return score
    days = max((now - then).total_seconds() / 86400, 0.0)
    return score * 0.5 ** (days / HALF_LIFE_DAYS)


class ReplySuggestions:
    def __init__(self, j):
        self.j = j

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.j.settings, "reply_suggestions_enabled", True))

    # -- context --------------------------------------------------------------------------------------
    def current_context(self) -> str:
        """Context of the conversation right now, from Jarvis's most recent reply."""
        row = self.j.db.query_one("SELECT text FROM transcript WHERE role = 'assistant' ORDER BY id DESC LIMIT 1")
        return classify_context(row["text"] if row else None)

    # -- learning -------------------------------------------------------------------------------------
    def record(self, text: str, mode: str = "typed", context: str | None = None) -> bool:
        """Count a message the owner typed. Returns True if it was stored.

        Call this *before* the turn runs, so `context` is what Jarvis had said when the owner replied. Spoken
        messages (which can be the mic hearing Jarvis's own voice) and sensitive text are never stored."""
        if not self.enabled or mode != "typed" or not learnable(text):
            return False
        ctx = context or self.current_context()
        norm = normalise(text)
        now = datetime.now(timezone.utc)
        db = self.j.db
        row = db.query_one("SELECT * FROM reply_habits WHERE norm = ? AND context = ?", (norm, ctx))
        if row:
            score = _decayed(row["score"], row["last_used"], now) + 1.0
            db.execute("UPDATE reply_habits SET display = ?, uses = uses + 1, score = ?, last_used = ? WHERE id = ?",
                       (text.strip(), score, now_iso(), row["id"]))
        else:
            db.execute("INSERT INTO reply_habits (norm, context, display, uses, score, last_used) VALUES (?,?,?,?,?,?)",
                       (norm, ctx, text.strip(), 1, 1.0, now_iso()))
        self._prune(now)
        return True

    def _prune(self, now: datetime) -> None:
        db = self.j.db
        total = db.query_one("SELECT COUNT(*) AS n FROM reply_habits")["n"]
        if total <= MAX_ROWS:
            return
        rows = db.query("SELECT id, score, last_used FROM reply_habits")
        rows.sort(key=lambda r: _decayed(r["score"], r["last_used"], now))
        for r in rows[: total - MAX_ROWS]:
            db.execute("DELETE FROM reply_habits WHERE id = ?", (r["id"],))

    # -- suggesting -----------------------------------------------------------------------------------
    def suggest(self, prefix: str = "", context: str | None = None) -> dict[str, Any]:
        """Best learned reply for the current situation, as {"text", "why"} - or {"text": None}.

        With a non-empty `prefix` only replies that start with it (ignoring case) and are longer are considered."""
        if not self.enabled:
            return {"text": None, "enabled": False}
        ctx = context or self.current_context()
        now = datetime.now(timezone.utc)
        prefix = prefix.lstrip()
        rows = self.j.db.query("SELECT * FROM reply_habits")

        def matches(display: str) -> bool:
            return len(display) > len(prefix) and display.lower().startswith(prefix.lower())

        scored = [(r, _decayed(r["score"], r["last_used"], now)) for r in rows if matches(r["display"])]
        in_ctx = [(r, s) for r, s in scored if r["context"] == ctx and r["uses"] >= MIN_USES and s >= MIN_SCORE]
        if in_ctx:
            best, _ = max(in_ctx, key=lambda rs: rs[1])
            return {"text": best["display"], "enabled": True, "context": ctx,
                    "why": f"you usually reply this way in this situation ({ctx}); used {best['uses']} times"}
        overall: dict[str, dict[str, Any]] = {}
        for r, s in scored:
            o = overall.setdefault(r["norm"], {"display": r["display"], "uses": 0, "score": 0.0, "last": ""})
            o["uses"] += r["uses"]
            o["score"] += s
            if r["last_used"] >= o["last"]:
                o["display"], o["last"] = r["display"], r["last_used"]
        common = [o for o in overall.values() if o["uses"] >= MIN_USES and o["score"] >= MIN_SCORE]
        if common:
            best = max(common, key=lambda o: o["score"])
            return {"text": best["display"], "enabled": True, "context": ctx,
                    "why": f"one of your most common replies; used {best['uses']} times"}
        return {"text": None, "enabled": True, "context": ctx}

    # -- forgetting -----------------------------------------------------------------------------------
    def forget(self, text: str) -> int:
        """Forget a learned reply in every context. Returns how many rows were removed."""
        norm = normalise(text)
        if not norm:
            return 0
        n = self.j.db.query_one("SELECT COUNT(*) AS n FROM reply_habits WHERE norm = ?", (norm,))["n"]
        if n:
            self.j.db.execute("DELETE FROM reply_habits WHERE norm = ?", (norm,))
        return n

    def clear(self) -> int:
        """Forget everything learned."""
        n = self.j.db.query_one("SELECT COUNT(*) AS n FROM reply_habits")["n"]
        self.j.db.execute("DELETE FROM reply_habits")
        return n

    def summary(self) -> list[dict[str, Any]]:
        """What has been learned so far (for inspection): strongest first."""
        now = datetime.now(timezone.utc)
        rows = self.j.db.query("SELECT display, context, uses, score, last_used FROM reply_habits")
        for r in rows:
            r["score"] = round(_decayed(r.pop("score"), r["last_used"], now), 2)
        return sorted(rows, key=lambda r: -r["score"])
