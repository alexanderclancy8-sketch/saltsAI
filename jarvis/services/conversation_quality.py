"""Conversation and voice quality: per-turn metrics, a good/wrong feedback mechanism, and the summaries that feed
the nightly self-reflection and the weekly "conversation quality" note.

Everything here is measurement. It only ever *writes to Jarvis's own SQLite tables* (turn_metrics, voice_events,
turn_feedback) and never changes what a turn says or does: every hook is wrapped so a failure here can never break
a conversation, and nothing here sends, books or approves anything - so it needs no approval gate. The nightly
reflection (self_learning.py) is handed the numbers and flagged turns and may `remember` durable things as it
always could; any prompt changes it proposes are only *text in its reply* for a human to read - nothing is applied.

What is measured and where:
  - server side, per turn (both brains call `begin()` / the returned record's `first_delta()`, `tools()`,
    `finish()`): time to first words, total reply time, tool-call count, duplicate message, failed / interrupted
    turn, and - for spoken turns - whether the reply broke the spoken format (markdown, wake phrase, chatbot
    filler, too long...) and whether the "user" text looks like Jarvis's own reply coming back in (echo that got
    past the browser's guard).
  - server side, speech to text: `/api/stt` times the transcription (`note_stt`) and logs empty transcripts and
    failures (`record_event`).
  - browser side, reported to `/api/voice-events`: echo-suppression hits and time-to-first-audio (the browser is
    the only place that knows when sound actually started).
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import now_iso

log = logging.getLogger(__name__)

SCHEDULED_PREFIX = "[Scheduled"      # self-reflection / automations prompts: nobody said these, never measured
PENDING_STT_TTL_S = 60               # a timed transcription only belongs to the turn that follows it straight away
DUPLICATE_WINDOW_S = 120             # the same message again within this long counts as a duplicate
EVENT_KINDS = ("stt_failure", "stt_empty", "echo_suppressed")
TEXT_LIMIT = 2000
LAST_TURN_KEY = "quality:last_turn_id"
LAST_EVENT_KEY = "quality:last_event_id"
LAST_FEEDBACK_KEY = "quality:last_feedback_at"
LAST_REFLECTION_KEY = "quality:last_reflection"   # the nightly reflection's latest proposals, for the weekly summary
MAX_SPOKEN_WORDS = 80


# --------------------------------------------------------------------------- text helpers
def norm_words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9\s]+", " ", str(text or "").lower()).split()


def _phrase_words(text: str) -> list[str]:
    """Like norm_words, but apostrophes vanish instead of splitting ("that's" -> "thats", "wasn't" -> "wasnt")."""
    return norm_words(re.sub(r"['’]", "", str(text or "")))


# Phrases the owner may say (or type) to mark Jarvis's previous reply. Matched against the whole utterance once a
# wake word / "sir" / "no" is stripped from either end, so an ordinary sentence that merely contains one of these
# words is never treated as feedback.
WRONG_PHRASES = (
    "that was wrong", "that is wrong", "thats wrong", "that was incorrect", "thats incorrect",
    "that is incorrect", "that was not right", "that wasnt right", "that is not right", "thats not right",
    "that isnt right", "that was not correct", "that wasnt correct", "thats not correct", "that isnt correct",
    "that was a bad answer", "that was a bad reply", "that was rubbish", "wrong answer", "you got that wrong",
    "thats not what i asked", "that is not what i asked", "thats not what i said", "thats not what i meant",
    "not what i asked",
)
GOOD_PHRASES = (
    "that was good", "thats good", "that was right", "thats right", "that was correct", "thats correct",
    "that was perfect", "thats perfect", "that was spot on", "thats spot on", "that was helpful",
    "good answer", "good reply", "that was a good answer",
)
_GOOD_TAIL = {"thanks", "thank", "you", "cheers", "mate"}
_LEAD_WORDS = {"hey", "ok", "okay", "no", "sorry", "well", "right", "sir", "jarvis"}
_TRAIL_WORDS = {"sir", "jarvis"}
FEEDBACK_MAX_WORDS = 14


def detect_feedback_phrase(text: str, wake: str = "jarvis") -> tuple[str, str] | None:
    """("wrong" | "good", note) if the whole utterance is a short verdict on Jarvis's last reply, else None.
    "That was wrong, it's the Keighley site" -> ("wrong", <that text>): anything after the phrase is the note.
    A good verdict has to be *only* the verdict (plus "thanks"), so "that's good, now email them" is a normal
    request, not feedback."""
    lead = _LEAD_WORDS | set(norm_words(wake))
    trail = _TRAIL_WORDS | set(norm_words(wake))
    words = _phrase_words(text)
    while words and words[0] in lead:
        words.pop(0)
    while words and words[-1] in trail:
        words.pop()
    if not words or len(words) > FEEDBACK_MAX_WORDS:
        return None
    joined = " ".join(words)
    note = str(text).strip()[:500]
    for p in WRONG_PHRASES:
        if joined == p or joined.startswith(p + " "):
            return "wrong", note
    for p in GOOD_PHRASES:
        if joined.startswith(p) and set(joined[len(p):].split()) <= _GOOD_TAIL:
            return "good", note
    return None


def looks_like_self_echo(text: str, recent: list[str], wake: str = "jarvis", in_window: bool = False) -> bool:
    """True if `text` looks like Jarvis's own voice coming back in through the microphone. A Python mirror of the
    browser's looksLikeSelfEcho() (jarvis/web/hud.js) - the browser is what actually suppresses echo; this is the
    server-side safety net used to *measure* echo that got through, and to pin the expected behaviour in tests.
    `in_window` means "Jarvis was speaking (or just stopped)", which also enables the garbled-echo overlap check."""
    wake_words = norm_words(wake) or ["jarvis"]
    words = norm_words(text)
    if not words:
        return False
    if words.count(wake_words[0]) >= 2:          # "hey jarvis ... hey jarvis" mashed together is never a person
        return True
    filler = {wake_words[0], "hey", "ok", "okay"}
    content = [w for w in words if w not in filler]
    if not content:
        return False
    if len(content) <= 4 and re.search(r"\b(stop|quiet|enough|cancel|shut up)\b", " ".join(content)):
        return False                              # a short explicit stop is how the owner interrupts
    spoken = " ".join(" ".join(norm_words(r)) for r in recent if r)
    if not spoken:
        return False
    if len(content) >= 4 and f" {' '.join(content)} " in f" {spoken} ":
        return True
    if in_window and len(content) >= 3:
        vocab = set(spoken.split())
        if sum(1 for w in content if w in vocab) / len(content) >= 0.7:
            return True
    return False


_MARKDOWN_RE = re.compile(r"^\s*(?:#{1,6}\s|[-*•]\s|\d+[.)]\s)|\*\*|__|`|\|.*\||\[[^\]]+\]\([^)]+\)", re.M)
_URL_RE = re.compile(r"https?://|\bwww\.", re.I)
_FILLER_RE = re.compile(
    r"\b(certainly|great question|i'?d be happy to|absolutely|as an ai|i hope this helps|"
    r"let me know if you need anything else|here'?s a breakdown)\b", re.I)
_RAW_FIGURE_RE = re.compile(r"£\s?\d{1,3}(?:,\d{3})+(?:\.\d\d)?|£\s?\d+\.\d\d")


def lint_spoken_reply(text: str, wake: str = "jarvis") -> list[str]:
    """Ways a reply breaks the spoken format laid down in the system prompt (concise, no markdown, no URLs, no
    wake phrase, no chatbot filler, figures phrased for speech, at most one question). Empty list = fine."""
    t = str(text or "")
    if not t.strip():
        return ["empty"]
    flags: list[str] = []
    if _MARKDOWN_RE.search(t):
        flags.append("markdown")
    if _URL_RE.search(t):
        flags.append("url")
    w = re.escape((norm_words(wake) or ["jarvis"])[0])
    if re.search(rf"\b(?:hey|ok|okay)\s+{w}\b", t, re.I) or re.match(rf"\s*{w}\b", t, re.I):
        flags.append("wake_phrase")
    if _FILLER_RE.search(t):
        flags.append("chatbot_filler")
    if len(t.split()) > MAX_SPOKEN_WORDS:
        flags.append("too_long")
    if t.count("?") > 1:
        flags.append("multiple_questions")
    if _RAW_FIGURE_RE.search(t):
        flags.append("raw_figure")
    return flags


def _pct(values: list[float], p: float) -> int | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return int(vals[min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1))))])


def _age_s(iso: str) -> float:
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return 1e9
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


def _secs(ms: int | None) -> str:
    return "-" if ms is None else f"{ms / 1000:.1f}s"


# --------------------------------------------------------------------------- per-turn record
class TurnRecord:
    """Handed back by `begin()`; the brain pokes it as the turn progresses. Every method is safe to call in any
    order and any number of times, and does nothing for a turn that isn't being measured (turn_id None)."""

    def __init__(self, service: "ConversationQuality", turn_id: int | None, mode: str):
        self.service, self.turn_id, self.mode = service, turn_id, mode
        self.started = time.monotonic()
        self.first_delta_ms: int | None = None
        self.tool_count = 0
        self._done = False

    def first_delta(self) -> None:
        if self.first_delta_ms is None:
            self.first_delta_ms = int((time.monotonic() - self.started) * 1000)

    def tools(self, n: int = 1) -> None:
        self.tool_count += n

    def finish(self, reply: str = "", *, ok: bool = True, interrupted: bool = False) -> None:
        if self._done or self.turn_id is None:
            return
        self._done = True
        self.service._finish(self, reply, ok, interrupted)  # noqa: SLF001


class ConversationQuality:
    def __init__(self, j):
        self.j = j
        self._pending_stt: tuple[int, float] | None = None
        self._brief_at = ""

    @property
    def db(self):
        return self.j.db

    def _wake(self) -> str:
        return getattr(self.j.settings, "wake_word", "") or "jarvis"

    # -- recording --------------------------------------------------------------------------------------
    def begin(self, text: str, mode: str) -> TurnRecord:
        """Start measuring a turn. Also notices a spoken/typed verdict on the previous reply ("that was wrong")
        and flags that turn. Never raises."""
        try:
            if str(text).lstrip().startswith(SCHEDULED_PREFIX):
                return TurnRecord(self, None, mode)
            prev = self.db.query_one(
                "SELECT id, user_text, reply_text, created_at FROM turn_metrics ORDER BY id DESC LIMIT 1")
            wake = self._wake()
            verdict = detect_feedback_phrase(text, wake)
            if verdict and prev:
                self.feedback(verdict[0], verdict[1], turn_id=prev["id"])
            norm = " ".join(norm_words(text))
            duplicate = bool(prev and norm and " ".join(norm_words(prev["user_text"])) == norm
                             and _age_s(prev["created_at"]) <= DUPLICATE_WINDOW_S)
            voice = mode == "voice"
            echo = bool(voice and looks_like_self_echo(text, [prev["reply_text"]] if prev else [], wake))
            turn_id = self.db.execute(
                "INSERT INTO turn_metrics (created_at, mode, user_text, stt_ms, duplicate, echo_suspect)"
                " VALUES (?,?,?,?,?,?)",
                (now_iso(), mode, str(text)[:TEXT_LIMIT], self._take_stt() if voice else None,
                 int(duplicate), int(echo)))
            return TurnRecord(self, turn_id, mode)
        except Exception as e:  # noqa: BLE001 - measuring must never break a conversation
            log.warning("turn metrics skipped: %s", e)
            return TurnRecord(self, None, mode)

    def _finish(self, rec: TurnRecord, reply: str, ok: bool, interrupted: bool) -> None:
        try:
            flags = lint_spoken_reply(reply, self._wake()) if rec.mode == "voice" and ok and not interrupted else []
            self.db.execute(
                "UPDATE turn_metrics SET reply_text = ?, first_delta_ms = ?, total_ms = ?, tool_calls = ?,"
                " failed = ?, interrupted = ?, format_flags = ? WHERE id = ?",
                (str(reply or "")[:TEXT_LIMIT], rec.first_delta_ms, int((time.monotonic() - rec.started) * 1000),
                 rec.tool_count, int(not ok and not interrupted), int(interrupted), ",".join(flags), rec.turn_id))
        except Exception as e:  # noqa: BLE001
            log.warning("turn metrics not saved: %s", e)

    def note_stt(self, ms: float) -> None:
        """A transcription just took `ms`; it is attributed to the next spoken turn (if it follows promptly)."""
        self._pending_stt = (int(ms), time.monotonic())

    def _take_stt(self) -> int | None:
        pending, self._pending_stt = self._pending_stt, None
        if pending and time.monotonic() - pending[1] <= PENDING_STT_TTL_S:
            return pending[0]
        return None

    def record_event(self, kind: str, detail: str = "") -> bool:
        """An occurrence that isn't a turn: an STT failure, an empty transcript, an echo the browser suppressed."""
        if kind not in EVENT_KINDS:
            return False
        try:
            self.db.execute("INSERT INTO voice_events (created_at, kind, detail) VALUES (?,?,?)",
                            (now_iso(), kind, str(detail)[:300]))
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("voice event not saved: %s", e)
            return False

    def record_first_audio(self, ms: float, turn_id: int | None = None) -> None:
        """Browser-measured: how long after the request was sent the first sound of the reply began."""
        ms = int(max(0, min(ms, 600_000)))
        if turn_id is not None:
            self.db.execute("UPDATE turn_metrics SET first_audio_ms = ? WHERE id = ? AND first_audio_ms IS NULL",
                            (ms, int(turn_id)))
        else:
            self.db.execute(
                "UPDATE turn_metrics SET first_audio_ms = ? WHERE first_audio_ms IS NULL AND id ="
                " (SELECT MAX(id) FROM turn_metrics WHERE mode = 'voice')", (ms,))

    # -- feedback ---------------------------------------------------------------------------------------
    def feedback(self, rating: str, note: str = "", turn_id: int | None = None) -> dict[str, Any] | None:
        """Mark a turn (the latest by default) good or wrong, with an optional note. One verdict per turn: a second
        one replaces the first, and a note given earlier is kept if the rating hasn't changed. None if no such turn."""
        if rating not in ("good", "wrong"):
            raise ValueError("rating must be good or wrong")
        turn = (self.db.query_one("SELECT * FROM turn_metrics WHERE id = ?", (int(turn_id),)) if turn_id is not None
                else self.db.query_one("SELECT * FROM turn_metrics ORDER BY id DESC LIMIT 1"))
        if not turn:
            return None
        note = str(note or "").strip()[:500]
        existing = self.db.query_one("SELECT * FROM turn_feedback WHERE turn_id = ?", (turn["id"],))
        if existing:
            note = note or (existing["note"] if existing["rating"] == rating else "")
            self.db.execute("UPDATE turn_feedback SET rating = ?, note = ?, created_at = ? WHERE id = ?",
                            (rating, note, now_iso(), existing["id"]))
        else:
            self.db.execute(
                "INSERT INTO turn_feedback (created_at, turn_id, rating, note, user_text, reply_text)"
                " VALUES (?,?,?,?,?,?)",
                (now_iso(), turn["id"], rating, note, turn["user_text"], turn["reply_text"]))
        return {"turn_id": turn["id"], "rating": rating, "note": note}

    # -- statistics -------------------------------------------------------------------------------------
    @staticmethod
    def _summarise(rows: list[dict], events: list[dict], feedback: list[dict]) -> dict[str, Any]:
        voice = [r for r in rows if r["mode"] == "voice"]
        done = [r for r in rows if r["total_ms"] is not None and not r["failed"] and not r["interrupted"]]
        flag_counts: Counter[str] = Counter(f for r in voice for f in (r["format_flags"] or "").split(",") if f)
        return {
            "turns": len(rows), "voice_turns": len(voice),
            "failed": sum(r["failed"] for r in rows), "interrupted": sum(r["interrupted"] for r in rows),
            "duplicates": sum(r["duplicate"] for r in rows), "echo_suspects": sum(r["echo_suspect"] for r in rows),
            "tool_calls": sum(r["tool_calls"] for r in rows),
            "total_ms_p50": _pct([r["total_ms"] for r in done], 50), "total_ms_p90": _pct([r["total_ms"] for r in done], 90),
            "first_delta_ms_p50": _pct([r["first_delta_ms"] for r in done if r["first_delta_ms"] is not None], 50),
            "stt_ms_p50": _pct([r["stt_ms"] for r in voice if r["stt_ms"] is not None], 50),
            "stt_ms_p90": _pct([r["stt_ms"] for r in voice if r["stt_ms"] is not None], 90),
            "first_audio_ms_p50": _pct([r["first_audio_ms"] for r in voice if r["first_audio_ms"] is not None], 50),
            "first_audio_ms_p90": _pct([r["first_audio_ms"] for r in voice if r["first_audio_ms"] is not None], 90),
            "format_violations": sum(1 for r in voice if r["format_flags"]), "format_flags": dict(flag_counts),
            "events": {k: sum(1 for e in events if e["kind"] == k) for k in EVENT_KINDS},
            "good": sum(1 for f in feedback if f["rating"] == "good"),
            "wrong": sum(1 for f in feedback if f["rating"] == "wrong"),
        }

    def stats(self, days: int = 7) -> dict[str, Any]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        return self._summarise(
            self.db.query("SELECT * FROM turn_metrics WHERE created_at >= ? ORDER BY id", (since,)),
            self.db.query("SELECT * FROM voice_events WHERE created_at >= ? ORDER BY id", (since,)),
            self.db.query("SELECT * FROM turn_feedback WHERE created_at >= ? ORDER BY id", (since,)))

    @staticmethod
    def _lines(s: dict[str, Any]) -> list[str]:
        ev = s["events"]
        lines = [
            f"{s['turns']} turns ({s['voice_turns']} spoken), {s['tool_calls']} tool calls; "
            f"{s['failed']} failed, {s['interrupted']} interrupted.",
            f"Reply time: typical {_secs(s['total_ms_p50'])}, slowest tenth {_secs(s['total_ms_p90'])}; "
            f"first words {_secs(s['first_delta_ms_p50'])}.",
        ]
        if s["voice_turns"] or any(ev.values()):
            lines.append(
                f"Voice: speech-to-text {_secs(s['stt_ms_p50'])} (slowest tenth {_secs(s['stt_ms_p90'])}), "
                f"time to first audio {_secs(s['first_audio_ms_p50'])} (slowest tenth {_secs(s['first_audio_ms_p90'])}).")
            lines.append(
                f"Speech trouble: {ev['stt_failure']} STT failures, {ev['stt_empty']} empty transcripts, "
                f"{ev['echo_suppressed']} echoes suppressed, {s['echo_suspects']} possible echoes that got through.")
            if s["format_violations"]:
                lines.append(f"{s['format_violations']} spoken replies broke the format: "
                             + ", ".join(f"{k} x{v}" for k, v in sorted(s["format_flags"].items())) + ".")
        lines.append(f"Repeated messages: {s['duplicates']}. Your verdicts: {s['good']} good, {s['wrong']} wrong.")
        return lines

    def summary_text(self, days: int = 7) -> str:
        return "\n".join(self._lines(self.stats(days)))

    async def weekly_summary(self) -> str:
        """The short weekly note (display only - an info notification never pushes to Teams/email)."""
        s = self.stats(7)
        if not s["turns"] and not any(s["events"].values()):
            return ""
        text = "\n".join(self._lines(s))
        proposals = self.db.get_kv(LAST_REFLECTION_KEY)
        if proposals:
            text += "\n\nFrom my latest nightly reflection:\n" + proposals[:1200]
        await self.j.notifier.notify("Conversation quality - this week", text, level="info")
        return text

    # -- feed for the nightly self-reflection --------------------------------------------------------------
    def reflection_brief(self) -> tuple[str, int, int]:
        """(text, newest_turn_id, newest_event_id) covering everything since the last reflection; text is empty when
        there is nothing to say. The caller stores the two ids (`mark_reflected`) once it has actually reflected."""
        last_turn = int(self.db.get_kv(LAST_TURN_KEY) or 0)
        last_event = int(self.db.get_kv(LAST_EVENT_KEY) or 0)
        rows = self.db.query("SELECT * FROM turn_metrics WHERE id > ? ORDER BY id", (last_turn,))
        events = self.db.query("SELECT * FROM voice_events WHERE id > ? ORDER BY id", (last_event,))
        ids = [r["id"] for r in rows]
        # Verdicts are picked up by when they were given (a "wrong" tapped today on yesterday's reply still counts).
        self._brief_at = now_iso()
        feedback = self.db.query("SELECT * FROM turn_feedback WHERE created_at > ? ORDER BY id",
                                 (self.db.get_kv(LAST_FEEDBACK_KEY) or "",))
        newest_turn = max(ids + [last_turn])
        newest_event = max([e["id"] for e in events] + [last_event])
        if not rows and not events and not feedback:
            return "", newest_turn, newest_event
        parts = ["Conversation quality measurements since your last reflection:", *self._lines(
            self._summarise(rows, events, feedback))]
        wrong = [f for f in feedback if f["rating"] == "wrong"]
        if wrong:
            parts.append("\nReplies the owner marked WRONG (what was asked -> what you said -> their note):")
            parts += [f"- \"{f['user_text'][:300]}\" -> \"{f['reply_text'][:400]}\" -> note: {f['note'][:300] or '(none)'}"
                      for f in wrong[:20]]
        bad_format = [r for r in rows if r["mode"] == "voice" and r["format_flags"]]
        if bad_format:
            parts.append("\nSpoken replies that broke the spoken format (flags -> reply):")
            parts += [f"- {r['format_flags']} -> \"{r['reply_text'][:300]}\"" for r in bad_format[:10]]
        dupes = [r for r in rows if r["duplicate"]]
        if dupes:
            parts.append("\nQuestions asked twice in a row (the first answer may not have landed):")
            parts += [f"- \"{r['user_text'][:200]}\"" for r in dupes[:10]]
        return "\n".join(parts), newest_turn, newest_event

    def mark_reflected(self, turn_id: int, event_id: int, proposals: str) -> None:
        self.db.set_kv(LAST_TURN_KEY, str(turn_id))
        self.db.set_kv(LAST_EVENT_KEY, str(event_id))
        self.db.set_kv(LAST_FEEDBACK_KEY, self._brief_at or now_iso())
        if proposals and proposals.strip():
            self.db.set_kv(LAST_REFLECTION_KEY, proposals.strip()[:4000])
