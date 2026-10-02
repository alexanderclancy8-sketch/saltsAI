"""Small SQLite store for Jarvis' own state (issues, alerts, approvals, memory).

Business data (jobs, invoices, emails) is never copied here wholesale - it is
read live from the source systems.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import history

TRANSCRIPT_REDACTED_KEY = "transcript:redacted_v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reporter TEXT NOT NULL,
    reporter_email TEXT DEFAULT '',
    source TEXT NOT NULL,
    system TEXT DEFAULT 'Salts FSM',
    severity TEXT DEFAULT 'medium',
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    triage_json TEXT DEFAULT '',
    fix_pr_url TEXT DEFAULT '',
    fix_pr_number INTEGER,
    fix_branch TEXT DEFAULT '',
    image_path TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    resolved_by TEXT DEFAULT '',
    resolved_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    read INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS digest_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    link TEXT DEFAULT '',
    status TEXT DEFAULT '',
    ref TEXT DEFAULT '',
    level TEXT DEFAULT 'info',
    delivery TEXT NOT NULL DEFAULT 'digest',
    digested_at TEXT DEFAULT '',
    digest_id INTEGER
);
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    item_count INTEGER DEFAULT 0,
    text TEXT NOT NULL,
    delivered TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    suite TEXT NOT NULL,
    name TEXT NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT DEFAULT '',
    duration_ms INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    fact TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT DEFAULT '',
    decided_at TEXT DEFAULT ''
);
-- Where each Teams approver's one-to-one chat with the bot lives, so Jarvis can message them first (a Bot Framework
-- "conversation reference"). Learned when an allowlisted person messages the bot; see services/teams_approvals.py.
CREATE TABLE IF NOT EXISTS teams_approvers (
    email TEXT PRIMARY KEY,
    service_url TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- One row per approval card sent to one approver for one action: the claim that makes sending idempotent, and the
-- Teams activity id used to update the card once the action is decided.
CREATE TABLE IF NOT EXISTS teams_approval_cards (
    action_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    activity_id TEXT DEFAULT '',
    sent_at TEXT NOT NULL,
    PRIMARY KEY (action_id, email)
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcript (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (day, source, metric)
);
-- turn_metrics / voice_events / turn_feedback: conversation-quality measurements. user_text / reply_text are short
-- redacted excerpts (<= 300 chars); the full conversation is `transcript`. Pruned after
-- conversation_quality_retention_days (default 90); see services/conversation_quality.py.
CREATE TABLE IF NOT EXISTS turn_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    user_text TEXT DEFAULT '',
    reply_text TEXT DEFAULT '',
    stt_ms INTEGER,
    first_delta_ms INTEGER,
    first_audio_ms INTEGER,
    total_ms INTEGER,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    duplicate INTEGER NOT NULL DEFAULT 0,
    echo_suspect INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    interrupted INTEGER NOT NULL DEFAULT 0,
    format_flags TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS voice_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS turn_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    turn_id INTEGER NOT NULL UNIQUE,
    rating TEXT NOT NULL,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stock_items (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT DEFAULT '',
    unit TEXT DEFAULT 'each',
    unit_cost REAL DEFAULT 0,
    reorder_level REAL DEFAULT 0,
    reorder_qty REAL DEFAULT 0,
    supplier TEXT DEFAULT '',
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stock_levels (
    sku TEXT NOT NULL,
    location TEXT NOT NULL,
    qty REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (sku, location)
);
CREATE TABLE IF NOT EXISTS stock_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    sku TEXT NOT NULL,
    qty REAL NOT NULL,
    kind TEXT NOT NULL,
    from_loc TEXT DEFAULT '',
    to_loc TEXT DEFAULT '',
    job_ref TEXT DEFAULT '',
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS suggestions (
    key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT DEFAULT '',
    prompt TEXT NOT NULL,
    priority INTEGER DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS action_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    owner TEXT NOT NULL,
    action TEXT NOT NULL,
    due TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS processed_emails (
    message_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_access_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    site TEXT NOT NULL,
    system TEXT NOT NULL,
    code_encrypted TEXT NOT NULL,
    notes TEXT DEFAULT '',
    UNIQUE(site, system)
);
CREATE TABLE IF NOT EXISTS automations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    description TEXT NOT NULL,
    cron TEXT NOT NULL,
    prompt TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT DEFAULT '',
    last_result TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reply_habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    norm TEXT NOT NULL,
    context TEXT NOT NULL,
    display TEXT NOT NULL,
    uses INTEGER NOT NULL DEFAULT 1,
    score REAL NOT NULL DEFAULT 1,
    last_used TEXT NOT NULL,
    UNIQUE(norm, context)
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    markdown TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS false_alarm_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    job_ref TEXT NOT NULL UNIQUE,
    site TEXT NOT NULL,
    system TEXT DEFAULT '',
    event_date TEXT DEFAULT '',
    cause_category TEXT DEFAULT '',
    cause TEXT DEFAULT '',
    corrective_action TEXT DEFAULT '',
    action_done_date TEXT DEFAULT '',
    evidence_ref TEXT DEFAULT '',
    investigated_by TEXT DEFAULT '',
    reviewed_by TEXT DEFAULT '',
    review_date TEXT DEFAULT ''
);
"""

# Columns of false_alarm_log a caller may set (never interpolated from user input - this is the whitelist).
FALSE_ALARM_FIELDS = ("system", "event_date", "cause_category", "cause", "corrective_action", "action_done_date",
                      "evidence_ref", "investigated_by", "reviewed_by", "review_date")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns that older databases lack. Safe to run on every start."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(pending_actions)").fetchall()}
        if "approved_by" not in cols:
            self._conn.execute("ALTER TABLE pending_actions ADD COLUMN approved_by TEXT DEFAULT ''")
        issue_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(issues)").fetchall()}
        for col in ("resolved_by", "resolved_at"):  # who closed an issue by hand, and when
            if col not in issue_cols:
                self._conn.execute(f"ALTER TABLE issues ADD COLUMN {col} TEXT DEFAULT ''")

    # -- low level ----------------------------------------------------------
    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid or cur.rowcount

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- issues -------------------------------------------------------------
    def create_issue(self, *, reporter: str, title: str, description: str, source: str,
                     reporter_email: str = "", system: str = "Salts FSM", severity: str = "medium",
                     image_path: str = "") -> int:
        ts = now_iso()
        return self.execute(
            "INSERT INTO issues (created_at, updated_at, reporter, reporter_email, source, system, severity,"
            " title, description, image_path) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, ts, reporter, reporter_email, source, system, severity, title, description, image_path),
        )

    def update_issue(self, issue_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE issues SET {cols} WHERE id = ?", (*fields.values(), issue_id))

    def get_issue(self, issue_id: int) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM issues WHERE id = ?", (issue_id,))

    def list_issues(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status == "open":
            return self.query(
                "SELECT * FROM issues WHERE status NOT IN ('resolved','wont_fix') ORDER BY id DESC LIMIT ?", (limit,))
        if status:
            return self.query("SELECT * FROM issues WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit))
        return self.query("SELECT * FROM issues ORDER BY id DESC LIMIT ?", (limit,))

    def resolve_issue(self, issue_id: int, *, by: str, note: str = "") -> None:
        """Close an issue by hand: status, note, and who/when. Not an approval - it decides nothing about any action."""
        ts = now_iso()
        self.execute("UPDATE issues SET status = 'resolved', notes = ?, resolved_by = ?, resolved_at = ?,"
                     " updated_at = ? WHERE id = ?", (note, by, ts, ts, issue_id))

    def reopen_issue(self, issue_id: int, *, note: str) -> None:
        self.execute("UPDATE issues SET status = 'open', notes = ?, resolved_by = '', resolved_at = '',"
                     " updated_at = ? WHERE id = ?", (note, now_iso(), issue_id))

    def find_open_issue_by_title(self, title: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM issues WHERE title = ? AND status NOT IN ('resolved','wont_fix') ORDER BY id DESC",
            (title,))

    # -- notifications --------------------------------------------------------
    def add_notification(self, level: str, title: str, body: str = "") -> int:
        return self.execute("INSERT INTO notifications (created_at, level, title, body) VALUES (?,?,?,?)",
                            (now_iso(), level, title, body))

    def recent_notifications(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))

    # -- weekly digest store ----------------------------------------------------
    # `delivery` is how the notice itself was handled: 'digest' (held for the weekly summary) or 'immediate'
    # (already sent straight away, kept only so the summary is complete).
    def add_digest_item(self, kind: str, title: str, body: str = "", link: str = "", status: str = "",
                        ref: str = "", level: str = "info", delivery: str = "digest") -> int:
        return self.execute(
            "INSERT INTO digest_items (created_at, kind, title, body, link, status, ref, level, delivery)"
            " VALUES (?,?,?,?,?,?,?,?,?)", (now_iso(), kind, title, body, link, status, ref, level, delivery))

    def pending_digest_items(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM digest_items WHERE digested_at = '' ORDER BY id")

    def digest_items_by_kind(self, kinds: list[str] | tuple[str, ...] | set[str]) -> list[dict[str, Any]]:
        kinds = list(kinds)
        if not kinds:
            return []
        marks = ",".join("?" for _ in kinds)
        return self.query(f"SELECT * FROM digest_items WHERE kind IN ({marks}) ORDER BY id", tuple(kinds))

    def mark_digested(self, item_ids: list[int], digest_id: int) -> None:
        if not item_ids:
            return
        marks = ",".join("?" for _ in item_ids)
        self.execute(f"UPDATE digest_items SET digested_at = ?, digest_id = ? WHERE id IN ({marks})"
                     f" AND digested_at = ''", (now_iso(), digest_id, *item_ids))

    def add_digest(self, source: str, item_count: int, text: str, delivered: str = "") -> int:
        return self.execute("INSERT INTO digests (created_at, source, item_count, text, delivered) VALUES (?,?,?,?,?)",
                            (now_iso(), source, item_count, text, delivered))

    def list_digests(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM digests ORDER BY id DESC LIMIT ?", (limit,))

    def get_digest(self, digest_id: int) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM digests WHERE id = ?", (digest_id,))

    # -- routine test runs ------------------------------------------------------
    def add_test_run(self, suite: str, name: str, ok: bool, detail: str, duration_ms: int) -> None:
        self.execute("INSERT INTO test_runs (created_at, suite, name, ok, detail, duration_ms) VALUES (?,?,?,?,?,?)",
                     (now_iso(), suite, name, int(ok), detail, duration_ms))

    def latest_test_results(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT t.* FROM test_runs t JOIN (SELECT suite, name, MAX(id) AS mid FROM test_runs GROUP BY suite, name)"
            " m ON t.id = m.mid ORDER BY t.suite, t.name")

    def previous_result(self, suite: str, name: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM test_runs WHERE suite = ? AND name = ? ORDER BY id DESC LIMIT 1",
                              (suite, name))

    # -- memory -------------------------------------------------------------------
    @staticmethod
    def _memory_key(fact: str) -> str:
        """Comparison form for spotting a fact that is already remembered: case, spacing and a trailing full stop
        don't make it a different fact."""
        return " ".join(fact.split()).lower().rstrip(".").strip()

    def find_memory(self, fact: str) -> int | None:
        """Id of an already-remembered fact that says the same thing as `fact`, if any."""
        key = self._memory_key(fact)
        for m in self.memories():
            if self._memory_key(m["fact"]) == key:
                return m["id"]
        return None

    def remember(self, fact: str) -> int:
        """Stores a fact and returns its id - or, if the same fact is already remembered, returns the existing id
        without adding a duplicate (the scheduled self-reflection can easily re-learn something it already knows)."""
        fact = fact.strip()
        existing = self.find_memory(fact)
        if existing is not None:
            return existing
        return self.execute("INSERT INTO memory (created_at, fact) VALUES (?,?)", (now_iso(), fact))

    def forget(self, memory_id: int) -> None:
        self.execute("DELETE FROM memory WHERE id = ?", (memory_id,))

    def memories(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM memory ORDER BY id")

    # -- automations ------------------------------------------------------------------
    def create_automation(self, description: str, cron: str, prompt: str) -> int:
        return self.execute("INSERT INTO automations (created_at, description, cron, prompt) VALUES (?,?,?,?)",
                            (now_iso(), description, cron, prompt))

    def get_automation(self, automation_id: int) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM automations WHERE id = ?", (automation_id,))

    def list_automations(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM automations ORDER BY id")

    def update_automation(self, automation_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE automations SET {cols} WHERE id = ?", (*fields.values(), automation_id))

    def delete_automation(self, automation_id: int) -> None:
        self.execute("DELETE FROM automations WHERE id = ?", (automation_id,))

    # -- approvals ------------------------------------------------------------------
    def create_action(self, kind: str, summary: str, payload: dict[str, Any], status: str = "pending",
                      approved_by: str = "") -> int:
        """`status`/`approved_by` are only ever set to "approved"/"standing approval: ..." by ActionExecutor.queue(),
        when the owner's own standing approval (services/standing_approvals.py) covers this exact action."""
        return self.execute(
            "INSERT INTO pending_actions (created_at, kind, summary, payload_json, status, approved_by, decided_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (now_iso(), kind, summary, json.dumps(payload), status, approved_by,
             now_iso() if status != "pending" else ""))

    def count_standing_runs_since(self, cutoff_iso: str) -> int:
        row = self.query_one("SELECT COUNT(*) AS n FROM pending_actions WHERE approved_by LIKE 'standing approval:%'"
                             " AND created_at >= ?", (cutoff_iso,))
        return int(row["n"]) if row else 0

    def claim_teams_card(self, action_id: int, email: str) -> bool:
        """True only for the first caller for this (action, approver) - the idempotency guard for approval cards."""
        with self._lock:
            cur = self._conn.execute("INSERT OR IGNORE INTO teams_approval_cards (action_id, email, sent_at)"
                                     " VALUES (?,?,?)", (action_id, email.lower(), now_iso()))
            self._conn.commit()
            return cur.rowcount == 1

    def count_teams_cards_since(self, email: str, cutoff_iso: str) -> int:
        row = self.query_one("SELECT COUNT(*) AS n FROM teams_approval_cards WHERE email = ? AND sent_at >= ?",
                             (email.lower(), cutoff_iso))
        return int(row["n"]) if row else 0

    def set_teams_card_activity(self, action_id: int, email: str, activity_id: str) -> None:
        self.execute("UPDATE teams_approval_cards SET activity_id = ? WHERE action_id = ? AND email = ?",
                     (activity_id, action_id, email.lower()))

    def teams_cards_for(self, action_id: int) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM teams_approval_cards WHERE action_id = ?", (action_id,))

    def save_teams_approver(self, email: str, service_url: str, conversation_id: str) -> None:
        self.execute(
            "INSERT INTO teams_approvers (email, service_url, conversation_id, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(email) DO UPDATE SET service_url = excluded.service_url,"
            " conversation_id = excluded.conversation_id, updated_at = excluded.updated_at",
            (email.lower(), service_url, conversation_id, now_iso()))

    def teams_approvers(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM teams_approvers ORDER BY email")

    def get_action(self, action_id: int) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM pending_actions WHERE id = ?", (action_id,))
        if row:
            row["payload"] = json.loads(row.pop("payload_json"))
        return row

    def pending_actions(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM pending_actions WHERE status = 'pending' ORDER BY id")
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows

    def failed_actions(self, since_iso: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """Approved actions that then failed (newest first), optionally only those decided at/after `since_iso`."""
        rows = self.query("SELECT * FROM pending_actions WHERE status = 'failed' AND decided_at >= ?"
                          " ORDER BY id DESC LIMIT ?", (since_iso, limit))
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows

    def set_action_status(self, action_id: int, status: str, result: str = "") -> None:
        self.execute("UPDATE pending_actions SET status = ?, result = ?, decided_at = ? WHERE id = ?",
                     (status, result, now_iso(), action_id))

    def decide_pending_action(self, action_id: int, status: str, by: str, result: str = "") -> bool:
        """Move a still-pending action to approved/denied. Atomic: False if someone else decided it first
        (two approvers tapping at once can't both win)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE pending_actions SET status = ?, result = ?, decided_at = ?, approved_by = ?"
                " WHERE id = ? AND status = 'pending'", (status, result, now_iso(), by, action_id))
            self._conn.commit()
            return cur.rowcount == 1

    # -- drafted documents (rendered to PDF/Word on request) ---------------------------------
    def add_document(self, doc_id: str, kind: str, title: str, markdown: str) -> str:
        self.execute("INSERT INTO documents (id, created_at, kind, title, markdown) VALUES (?,?,?,?,?)",
                     (doc_id, now_iso(), kind, title, markdown))
        return doc_id

    def get_document(self, doc_id: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))

    # -- key/value -------------------------------------------------------------------
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))

    # -- transcript --------------------------------------------------------------------
    def add_transcript(self, role: str, text: str) -> None:
        """Store one turn, redacted (credentials and access codes never reach the table). For the owner's words,
        cumulative speech-to-text partials are collapsed, and a longer version of the immediately preceding
        unanswered line replaces it - so only the final version of an utterance is kept."""
        text = history.redact_history(text)
        if role == "user":
            text = history.collapse_cumulative(text)
            last = self.query_one("SELECT id, role, text, created_at FROM transcript ORDER BY id DESC LIMIT 1")
            if last and last["role"] == "user" and history.is_partial_of(last["text"], text):
                try:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(last["created_at"])).total_seconds()
                except ValueError:
                    age = history.PARTIAL_WINDOW_S + 1
                if age <= history.PARTIAL_WINDOW_S:
                    self.execute("UPDATE transcript SET text = ? WHERE id = ?", (text, last["id"]))
                    return
        self.execute("INSERT INTO transcript (created_at, role, text) VALUES (?,?,?)", (now_iso(), role, text))

    def recent_transcript(self, limit: int = 30) -> list[dict[str, Any]]:
        return list(reversed(self.query("SELECT * FROM transcript ORDER BY id DESC LIMIT ?", (limit,))))

    def last_transcript_id(self) -> int:
        return (self.query_one("SELECT MAX(id) AS m FROM transcript") or {}).get("m") or 0

    def transcript_since(self, since_iso: str, before_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """The latest ``limit`` turns created at or after ``since_iso`` (optionally only ids <= ``before_id``),
        oldest first."""
        if before_id is None:
            rows = self.query("SELECT * FROM transcript WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
                              (since_iso, limit))
        else:
            rows = self.query("SELECT * FROM transcript WHERE created_at >= ? AND id <= ? ORDER BY id DESC LIMIT ?",
                              (since_iso, before_id, limit))
        return list(reversed(rows))

    def maintain_transcript(self) -> None:
        """Retention: drop turns older than the agreed 2 years, and (once) redact rows written before redaction
        at rest existed."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=history.RETENTION_DAYS)).isoformat(timespec="seconds")
        self.execute("DELETE FROM transcript WHERE created_at < ?", (cutoff,))
        if self.get_kv(TRANSCRIPT_REDACTED_KEY):
            return
        for row in self.query("SELECT id, text FROM transcript"):
            clean = history.redact_history(row["text"])
            if clean != row["text"]:
                self.execute("UPDATE transcript SET text = ? WHERE id = ?", (clean, row["id"]))
        self.set_kv(TRANSCRIPT_REDACTED_KEY, now_iso())

    # -- tracked metrics (followers, reviews, rankings) ----------------------------------
    def record_metric(self, day: str, source: str, metric: str, value: float) -> None:
        self.execute("INSERT INTO metrics (day, source, metric, value) VALUES (?,?,?,?) "
                     "ON CONFLICT(day, source, metric) DO UPDATE SET value = excluded.value",
                     (day, source, metric, value))

    def metric_history(self, source: str, metric: str, since_day: str) -> list[dict[str, Any]]:
        return self.query("SELECT day, value FROM metrics WHERE source = ? AND metric = ? AND day >= ? ORDER BY day",
                          (source, metric, since_day))

    def latest_metrics(self) -> list[dict[str, Any]]:
        return self.query("SELECT m.* FROM metrics m JOIN (SELECT source, metric, MAX(day) AS d FROM metrics "
                          "GROUP BY source, metric) x ON m.source = x.source AND m.metric = x.metric AND m.day = x.d "
                          "ORDER BY m.source, m.metric")

    # -- suggestions ----------------------------------------------------------------------------
    def open_suggestions(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM suggestions WHERE status = 'open' ORDER BY priority, updated_at DESC")

    def get_suggestion(self, key: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM suggestions WHERE key = ?", (key,))

    def upsert_suggestion(self, key: str, title: str, detail: str, prompt: str, priority: int) -> bool:
        """Insert or refresh an open suggestion. Returns True if it is new."""
        existing = self.get_suggestion(key)
        ts = now_iso()
        if existing is None:
            self.execute("INSERT INTO suggestions (key, created_at, updated_at, title, detail, prompt, priority) "
                         "VALUES (?,?,?,?,?,?,?)", (key, ts, ts, title, detail, prompt, priority))
            return True
        if existing["status"] == "open":
            self.execute("UPDATE suggestions SET title = ?, detail = ?, prompt = ?, priority = ?, updated_at = ? "
                         "WHERE key = ?", (title, detail, prompt, priority, ts, key))
        return False

    def set_suggestion_status(self, key: str, status: str) -> None:
        self.execute("UPDATE suggestions SET status = ?, updated_at = ? WHERE key = ?", (status, now_iso(), key))

    def reopen_suggestion(self, key: str) -> None:
        self.execute("UPDATE suggestions SET status = 'open', updated_at = ? WHERE key = ?", (now_iso(), key))

    # -- meeting action items ---------------------------------------------------------------------
    def add_action_item(self, source: str, owner: str, action: str, due: str = "") -> int:
        return self.execute("INSERT INTO action_items (created_at, source, owner, action, due) VALUES (?,?,?,?,?)",
                            (now_iso(), source, owner, action, due))

    def action_items(self, status: str | None = "open") -> list[dict[str, Any]]:
        if status:
            return self.query("SELECT * FROM action_items WHERE status = ? ORDER BY due = '', due, id", (status,))
        return self.query("SELECT * FROM action_items ORDER BY id DESC LIMIT 200")

    def set_action_item_status(self, item_id: int, status: str) -> None:
        self.execute("UPDATE action_items SET status = ? WHERE id = ?", (status, item_id))

    # -- site access codes ---------------------------------------------------------------
    # `code_encrypted` is opaque here - encryption/decryption is the caller's job
    # (services/site_access.py), this table just stores and returns the ciphertext.
    def upsert_site_access_code(self, site: str, system: str, code_encrypted: str, notes: str = "") -> int:
        ts = now_iso()
        return self.execute(
            "INSERT INTO site_access_codes (created_at, updated_at, site, system, code_encrypted, notes)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(site, system) DO UPDATE SET"
            " updated_at = excluded.updated_at, code_encrypted = excluded.code_encrypted, notes = excluded.notes",
            (ts, ts, site, system, code_encrypted, notes),
        )

    def list_site_access_codes(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM site_access_codes ORDER BY site, system")

    def find_site_access_codes(self, site: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM site_access_codes WHERE site LIKE ? ORDER BY system", (f"%{site}%",))

    def delete_site_access_code(self, record_id: int) -> None:
        self.execute("DELETE FROM site_access_codes WHERE id = ?", (record_id,))

    # -- false alarm log (BS 5839-1: log, investigate, review, evidence corrective action) --------------
    def get_false_alarm_record(self, job_ref: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM false_alarm_log WHERE job_ref = ?", (job_ref,))

    def list_false_alarm_records(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM false_alarm_log ORDER BY event_date, id")

    def upsert_false_alarm_record(self, job_ref: str, site: str, **fields: str) -> dict[str, Any]:
        """Create the record for a job, or update it. On update only non-empty values overwrite, so adding the
        review later never blanks the cause that was recorded earlier."""
        unknown = set(fields) - set(FALSE_ALARM_FIELDS)
        if unknown:
            raise ValueError(f"Unknown false alarm log field(s): {', '.join(sorted(unknown))}")
        values = {k: str(v).strip() for k, v in fields.items() if v is not None and str(v).strip()}
        ts = now_iso()
        if self.get_false_alarm_record(job_ref):
            if site:
                values["site"] = site
            if values:
                values["updated_at"] = ts
                cols = ", ".join(f"{k} = ?" for k in values)
                self.execute(f"UPDATE false_alarm_log SET {cols} WHERE job_ref = ?", (*values.values(), job_ref))
        else:
            row = {"created_at": ts, "updated_at": ts, "job_ref": job_ref, "site": site, **values}
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            self.execute(f"INSERT INTO false_alarm_log ({cols}) VALUES ({marks})", tuple(row.values()))
        return self.get_false_alarm_record(job_ref) or {}

    # -- processed emails -----------------------------------------------------------------
    def mark_email_processed(self, message_id: str) -> bool:
        """Returns True if newly marked, False if it had already been processed."""
        with self._lock:
            cur = self._conn.execute("INSERT OR IGNORE INTO processed_emails (message_id, created_at) VALUES (?,?)",
                                     (message_id, now_iso()))
            self._conn.commit()
            return cur.rowcount == 1
