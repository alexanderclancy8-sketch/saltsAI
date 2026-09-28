"""Small SQLite store for Jarvis' own state (issues, alerts, approvals, memory).

Business data (jobs, invoices, emails) is never copied here wholesale - it is
read live from the source systems.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    read INTEGER DEFAULT 0
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
CREATE TABLE IF NOT EXISTS processed_emails (
    message_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""


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
            self._conn.commit()

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
    def remember(self, fact: str) -> int:
        return self.execute("INSERT INTO memory (created_at, fact) VALUES (?,?)", (now_iso(), fact))

    def forget(self, memory_id: int) -> None:
        self.execute("DELETE FROM memory WHERE id = ?", (memory_id,))

    def memories(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM memory ORDER BY id")

    # -- approvals ------------------------------------------------------------------
    def create_action(self, kind: str, summary: str, payload: dict[str, Any]) -> int:
        return self.execute("INSERT INTO pending_actions (created_at, kind, summary, payload_json) VALUES (?,?,?,?)",
                            (now_iso(), kind, summary, json.dumps(payload)))

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

    def set_action_status(self, action_id: int, status: str, result: str = "") -> None:
        self.execute("UPDATE pending_actions SET status = ?, result = ?, decided_at = ? WHERE id = ?",
                     (status, result, now_iso(), action_id))

    # -- key/value -------------------------------------------------------------------
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))

    # -- transcript --------------------------------------------------------------------
    def add_transcript(self, role: str, text: str) -> None:
        self.execute("INSERT INTO transcript (created_at, role, text) VALUES (?,?,?)", (now_iso(), role, text))

    def recent_transcript(self, limit: int = 30) -> list[dict[str, Any]]:
        return list(reversed(self.query("SELECT * FROM transcript ORDER BY id DESC LIMIT ?", (limit,))))

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

    # -- processed emails -----------------------------------------------------------------
    def mark_email_processed(self, message_id: str) -> bool:
        """Returns True if newly marked, False if it had already been processed."""
        with self._lock:
            cur = self._conn.execute("INSERT OR IGNORE INTO processed_emails (message_id, created_at) VALUES (?,?)",
                                     (message_id, now_iso()))
            self._conn.commit()
            return cur.rowcount == 1
