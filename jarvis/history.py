"""Conversation continuity helpers: what is safe to keep from past conversations, and how it is carried forward.

- ``redact_history`` strips credentials *and* spoken access codes/PINs. Everything is redacted before it is written
  to the transcript table, so the agreed 2-year retention only ever holds redacted text.
- ``collapse_cumulative`` / ``is_partial_of`` undo the stutter of cumulative speech-to-text partials
  ("ladder", "ladder inspection", "ladder inspection jobs") so only the final version of an utterance is kept.
- ``recent_context`` is the "what we were talking about" block put in the system prompt at the start of a session.
- ``open_requests`` is a short list of things asked for but not finished, carried forward until closed.
- ``search_history`` backs the "I just asked you..." / "did you do it?" lookups.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .integrations.redact import REDACTED, redact

RETENTION_DAYS = 730          # the agreed 2-year retention (redacted)
CONTEXT_HOURS = 24            # previous-session turns loaded into each new session
CONTEXT_MAX_TURNS = 60
CONTEXT_TURN_CHARS = 400
CONTEXT_MAX_CHARS = 12_000
PARTIAL_WINDOW_S = 30         # a longer version of the last unanswered user line within this is the same utterance
MAX_OPEN_REQUESTS = 20
OPEN_REQUEST_CHARS = 240
OPEN_KEY = "conversation:open_requests"
SCHEDULED_PREFIX = "[Scheduled"   # self-reflection / automation prompts: machine-written, not conversation

# A spoken or typed access code: "alarm code is 4821", "pin: 12-34-56", "password is hunter22".
_CODE_VALUE = r"(?:[#*]?\d[\d #*\-]+[\d#*]|(?=[A-Za-z0-9#*]*\d)[A-Za-z0-9#*]{4,})"
_ACCESS_CODE = re.compile(
    r"(?i)(\b(?:code|pin|passcode|passphrase|password|passwd|combination)\b"
    r"(?:\s+(?:number|no\.?|is|was|are|to|now|changed|has|been|reset|set|=|:))*\s*[:=]?\s*)" + _CODE_VALUE)


def redact_history(text: str | None) -> str:
    out = redact(text)
    return _ACCESS_CODE.sub(lambda m: m.group(1) + REDACTED, out)


# ---------------------------------------------------------------------------- stutter
def _word(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower())


def _tokens(text: str) -> list[str]:
    return [t for t in (_word(w) for w in text.split()) if t]


def collapse_cumulative(text: str) -> str:
    """Drop leading restatements: "a a b a b c" -> "a b c". A lone repeated word ("very very good") is left alone;
    only a repeated phrase, or a chain of two or more restatements, counts as stutter."""
    words = text.split()
    drops, widest = 0, 0
    while True:
        norm = [_word(w) for w in words]
        cut = next((k for k in range(1, len(words) // 2 + 1) if all(norm[:k]) and norm[:k] == norm[k:2 * k]), 0)
        if not cut:
            break
        words = words[cut:]
        drops += 1
        widest = max(widest, cut)
    return " ".join(words) if drops and (drops >= 2 or widest >= 2) else text


def is_partial_of(earlier: str, later: str) -> bool:
    """True if ``later`` is the same utterance as ``earlier``, just longer (or identical)."""
    a, b = _tokens(earlier), _tokens(later)
    return bool(a) and len(b) >= len(a) and b[:len(a)] == a


# ---------------------------------------------------------------------------- recent context
def _local(created_at: str, tz: str) -> str:
    try:
        return datetime.fromisoformat(created_at).astimezone(ZoneInfo(tz)).strftime("%a %d %b %H:%M")
    except (ValueError, KeyError):
        return created_at[:16]


def _conversation_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows without scheduled self-reflection/automation prompts (and the reply that immediately follows each)."""
    out: list[dict[str, Any]] = []
    skip_reply = False
    for r in rows:
        if r["role"] == "user" and r["text"].startswith(SCHEDULED_PREFIX):
            skip_reply = True
            continue
        if r["role"] == "assistant" and skip_reply:
            skip_reply = False
            continue
        skip_reply = False
        out.append(r)
    return out


def recent_context(db, *, owner: str, tz: str, before_id: int | None = None, hours: int = CONTEXT_HOURS) -> str:
    """Redacted turns from the last ``hours`` (only those at or before ``before_id``, i.e. earlier sessions)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = _conversation_rows(db.transcript_since(since, before_id=before_id, limit=CONTEXT_MAX_TURNS * 3))
    lines: list[str] = []
    for r in reversed(rows[-CONTEXT_MAX_TURNS:]):  # newest first, so the size cap drops the oldest
        text = redact_history(r["text"]).strip().replace("\n", " ")
        if len(text) > CONTEXT_TURN_CHARS:
            text = text[:CONTEXT_TURN_CHARS] + "…"
        lines.append(f"- [{_local(r['created_at'], tz)}] {owner if r['role'] == 'user' else 'Jarvis'}: {text}")
        if sum(len(x) + 1 for x in lines) > CONTEXT_MAX_CHARS:
            lines.pop()
            break
    return "\n".join(reversed(lines)) or "- nothing in the last 24 hours"


# ---------------------------------------------------------------------------- open requests
def _load(db) -> dict[str, Any]:
    try:
        data = json.loads(db.get_kv(OPEN_KEY) or "{}")
    except ValueError:
        data = {}
    items = [i for i in data.get("items", []) if isinstance(i, dict) and "id" in i and "text" in i]
    return {"next_id": int(data.get("next_id") or 1), "items": items}


def open_requests(db) -> list[dict[str, Any]]:
    return _load(db)["items"]


def add_open_request(db, text: str) -> int:
    data = _load(db)
    rid = max(data["next_id"], 1 + max((i["id"] for i in data["items"]), default=0))
    data["items"].append({"id": rid, "text": redact_history(text).strip()[:OPEN_REQUEST_CHARS],
                          "added": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    data["items"] = data["items"][-MAX_OPEN_REQUESTS:]
    data["next_id"] = rid + 1
    db.set_kv(OPEN_KEY, json.dumps(data))
    return rid


def close_open_request(db, request_id: int) -> bool:
    data = _load(db)
    kept = [i for i in data["items"] if i["id"] != request_id]
    if len(kept) == len(data["items"]):
        return False
    data["items"] = kept
    db.set_kv(OPEN_KEY, json.dumps(data))
    return True


def open_requests_text(db, tz: str) -> str:
    return "\n".join(f"- (#{i['id']}, asked {_local(i.get('added', ''), tz)}) {i['text']}"
                     for i in open_requests(db)) or "- none"


# ---------------------------------------------------------------------------- search
def search_history(db, query: str, hours: int = 48, limit: int = 10) -> list[dict[str, str]]:
    """Redacted transcript turns from the last ``hours`` matching any word of ``query`` (best matches first picked,
    shown oldest first). An empty query returns the most recent turns."""
    hours = max(1, min(hours, RETENTION_DAYS * 24))
    limit = max(1, min(limit, 30))
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = _conversation_rows(db.transcript_since(since, limit=2000))
    words = [w for w in _tokens(query) if len(w) >= 3]
    scored = []
    for r in rows:
        text = redact_history(r["text"])
        score = sum(w in text.lower() for w in words) if words else 1
        if score:
            scored.append((score, r["id"], r["created_at"], r["role"], text))
    best = sorted(scored, key=lambda s: (s[0], s[1]))[-limit:]
    return [{"when": created, "who": "owner" if role == "user" else "jarvis", "text": text[:1000]}
            for _, _, created, role, text in sorted(best, key=lambda s: s[1])]
