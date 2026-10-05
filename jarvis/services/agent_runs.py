"""Progress records for the background engineering agents (self_improve, fixer, security_watch).

Those loops run for minutes with nothing reported until they finish. This keeps one small row per run - what was
asked, when it started, its status, and a short trail of the tool calls made so far - so the read-only
`agent_runs` tool can say what an agent is doing or where it stopped.

Pure observability: nothing here changes what an agent does, and every recording call swallows its own errors, so a
database hiccup can never break a run. The trail holds one-line summaries (tool name, path, search pattern) and never
file contents or edit text.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from ..db import Database, now_iso

log = logging.getLogger(__name__)

STALL_AFTER = timedelta(minutes=30)  # a "running" run with no activity for this long is reported as stalled
MAX_TRAIL = 60  # steps kept per run (the newest); `steps` still counts them all
KEEP_RUNS = 200  # rows kept in total
LINE_CHARS = 140
# No wall-clock timeout exists on a run (they are bounded by turns: 60 API turns / 80 Claude Code turns), so this is
# the assumed ceiling on how long one can legitimately take. A row still 'running' after twice that, with no
# heartbeat (updated_at is bumped by every step) in the last STALL_AFTER, belongs to a process that is gone.
MAX_RUN_TIME = timedelta(hours=1)
INTERRUPTED_AFTER = 2 * MAX_RUN_TIME
INTERRUPTED_NOTE = "The process stopped before this run finished (restart, crash or cancellation)."
NO_RESULT_NOTE = "The run finished without submitting a change or recording an analysis."

# The run the current task is working on, so the engineer loops can record steps without it being threaded through
# their signatures. Each background run is its own asyncio task and so has its own value.
_current: ContextVar[int | None] = ContextVar("agent_run_id", default=None)


def _clip(text: Any, limit: int) -> str:
    one_line = " ".join(str(text).split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def describe_call(name: str, args: Any, ok: bool = True) -> str:
    """One line saying what a tool call did - never the file contents or edit text that came with it."""
    a = args if isinstance(args, dict) else {}
    if name == "str_replace_based_edit_tool":
        line = f"editor {a.get('command', '?')} {a.get('path', '')}"
    elif name == "grep":
        line = f"grep {a.get('pattern', '')!r} in {a.get('glob') or '*'}"
    elif name == "find_files":
        line = f"find_files {a.get('glob', '')}"
    elif name in ("submit_change", "submit_fix"):
        line = f"{name}: {a.get('pr_title', '')}"
    elif name == "submit_findings":
        findings = a.get("findings")
        line = f"submit_findings: {len(findings) if isinstance(findings, list) else '?'} finding(s)"
    else:
        line = str(name)
    line = _clip(line, LINE_CHARS - 9)
    return line if ok else f"{line} (failed)"


def _parse(ts: str, default: datetime) -> datetime:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return default
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class AgentRuns:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ recording
    def interrupt_stale(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Close rows still 'running' that no live process can own, as 'interrupted', and return them.

        Called once at start-up (and whenever a new run starts). It deliberately does NOT close every running row:
        during a rolling deploy two processes share this database, and the old one may be mid-run - marking its row
        interrupted would be wrong. So a row is only closed when it started more than INTERRUPTED_AFTER ago AND has
        had no step recorded within STALL_AFTER. A run that was actually alive can still overwrite the status with
        its real outcome when it finishes. Never raises."""
        now = now or datetime.now(timezone.utc)
        closed: list[dict[str, Any]] = []
        try:
            for r in self.db.query("SELECT id, kind, request, started_at, updated_at FROM agent_runs "
                                   "WHERE status = 'running'"):
                started, beat = _parse(r["started_at"], now), _parse(r["updated_at"], now)
                if now - started < INTERRUPTED_AFTER or now - beat < STALL_AFTER:
                    continue
                self.db.execute("UPDATE agent_runs SET status = 'interrupted', outcome = ?, updated_at = ? "
                                "WHERE id = ? AND status = 'running'",
                                (INTERRUPTED_NOTE, now_iso(), r["id"]))
                log.warning("Engineering run #%s (%s, started %s) never finished - the process stopped mid-run: %s",
                            r["id"], r["kind"], r["started_at"], (r["request"] or "")[:200])
                closed.append(dict(r))
        except Exception:  # noqa: BLE001 - observability must never break start-up or a run
            log.warning("Could not check for interrupted agent runs", exc_info=True)
        return closed

    def start(self, kind: str, request: str) -> int:
        self.interrupt_stale()
        ts = now_iso()
        run_id = self.db.execute(
            "INSERT INTO agent_runs (kind, request, started_at, updated_at, status) VALUES (?,?,?,?,'running')",
            (kind, _clip(request, 500), ts, ts))
        self.db.execute("DELETE FROM agent_runs WHERE id <= ?", (run_id - KEEP_RUNS,))
        return run_id

    def step(self, tool: str, args: Any, ok: bool = True, run_id: int | None = None) -> None:
        """Add one line to the trail of the run this task is working on (a no-op outside a tracked run)."""
        run_id = run_id or _current.get()
        if not run_id:
            return
        self.note(describe_call(tool, args, ok), run_id)

    def note(self, line: str, run_id: int | None = None) -> None:
        run_id = run_id or _current.get()
        if not run_id:
            return
        try:
            row = self.db.query_one("SELECT trail, steps FROM agent_runs WHERE id = ?", (run_id,))
            if not row:
                return
            ts = now_iso()
            trail = json.loads(row["trail"] or "[]")
            trail.append({"at": ts, "step": _clip(line, LINE_CHARS)})
            self.db.execute("UPDATE agent_runs SET trail = ?, steps = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(trail[-MAX_TRAIL:]), row["steps"] + 1, ts, run_id))
        except Exception:  # noqa: BLE001 - observability must never break a run
            log.warning("Could not record agent run step", exc_info=True)

    def finish(self, status: str, outcome: str = "", run_id: int | None = None, only_if_running: bool = False) -> None:
        """status is submitted / gave_up / failed / interrupted."""
        run_id = run_id or _current.get()
        if not run_id:
            return
        try:
            self.db.execute(
                "UPDATE agent_runs SET status = ?, outcome = ?, updated_at = ? WHERE id = ?"
                + (" AND status = 'running'" if only_if_running else ""),
                (status, _clip(outcome, 500), now_iso(), run_id))
        except Exception:  # noqa: BLE001
            log.warning("Could not record agent run outcome", exc_info=True)

    @contextmanager
    def track(self, kind: str, request: str) -> Iterator[int | None]:
        """Wrap a whole run: records it as running, makes steps recorded inside it land on it, and marks it failed if
        it raises (or interrupted if it is cancelled). A run that ends normally without anyone recording how it went (e.g. a fixer pass that neither
        submitted a fix nor gave an analysis) is closed as gave_up with a neutral note - it did not fail, it just
        produced nothing. Exceptions are re-raised untouched."""
        try:
            run_id: int | None = self.start(kind, request)
        except Exception:  # noqa: BLE001
            log.warning("Could not record agent run start", exc_info=True)
            run_id = None
        token = _current.set(run_id)
        try:
            yield run_id
        except BaseException as e:
            what = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            if isinstance(e, Exception):
                self.finish("failed", what, run_id, only_if_running=True)
            else:  # cancelled (CancelledError) or the process is being stopped: not an error in the run itself
                self.finish("interrupted", f"Cancelled before it finished ({what}).", run_id, only_if_running=True)
            raise
        else:
            self.finish("gave_up", NO_RESULT_NOTE, run_id, only_if_running=True)
        finally:
            _current.reset(token)

    # ------------------------------------------------------------------ reading
    def recent(self, limit: int = 5, run_id: int | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
        """Newest first. A run still marked running with no activity for STALL_AFTER shows as 'stalled' (worked out
        here, not stored, so one that wakes up again goes back to 'running')."""
        now = now or datetime.now(timezone.utc)
        if run_id is not None:
            rows = self.db.query("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
        else:
            rows = self.db.query("SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 50)),))
        out = []
        for r in rows:
            idle = now - _parse(r["updated_at"], now)
            status = "stalled" if r["status"] == "running" and idle >= STALL_AFTER else r["status"]
            trail = json.loads(r["trail"] or "[]")
            out.append({
                "id": r["id"], "kind": r["kind"], "request": r["request"], "status": status,
                "started_at": r["started_at"], "last_activity_at": r["updated_at"],
                "idle_minutes": max(0, int(idle.total_seconds() // 60)), "steps": r["steps"],
                "outcome": r["outcome"],
                "trail": trail if run_id is not None else trail[-10:],
            })
        return out
