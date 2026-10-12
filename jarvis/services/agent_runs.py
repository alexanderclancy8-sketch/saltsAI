"""Progress records for the background engineering agents (self_improve, fixer, security_watch).

Each run also has a wall-clock cap (RUN_CAP, status 'timed_out'), is flagged 'looping' when its trail shows the same call
repeated or one file edited again and again with no change in the failing test output, and stores a short 'lesson' when it
ends gave_up / failed / timed_out. The trail keeps a tool name and a short hash of the arguments - never the arguments.

Those loops run for minutes with nothing reported until they finish. This keeps one small row per run - what was
asked, when it started, its status, and a short trail of the tool calls made so far - so the read-only
`agent_runs` tool can say what an agent is doing or where it stopped.

Pure observability: nothing here changes what an agent does, and every recording call swallows its own errors, so a
database hiccup can never break a run. The trail holds one-line summaries (tool name, path, search pattern) and never
file contents or edit text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .. import access
from ..db import Database, now_iso

log = logging.getLogger(__name__)

STALL_AFTER = timedelta(minutes=30)  # a "running" run with no activity for this long is reported as stalled
MAX_TRAIL = 60  # steps kept per run (the newest); `steps` still counts them all
KEEP_RUNS = 200  # rows kept in total
LINE_CHARS = 140
# Runs are bounded by turns (60 API turns / 80 Claude Code turns) and by RUN_CAP below, so this is the assumed ceiling
# on how long one can legitimately take. A row still 'running' after twice that, with no
# heartbeat (updated_at is bumped by every step) in the last STALL_AFTER, belongs to a process that is gone.
MAX_RUN_TIME = timedelta(hours=1)
INTERRUPTED_AFTER = 2 * MAX_RUN_TIME
INTERRUPTED_NOTE = "The process stopped before this run finished (restart, crash or cancellation)."
NO_RESULT_NOTE = "The run finished without submitting a change or recording an analysis."
# Hard wall-clock cap per run. Read at use (tests shrink it). Past it a run is stopped ('timed_out'): the task running it
# is cancelled by track(), and a step recorded after it raises RunTimedOut. A row still running past it that nothing has
# stopped (another process owns it) reads as 'timed_out' too, unless it is already 'stalled'.
RUN_CAP = MAX_RUN_TIME
# Stuck-loop signals (practitioner guidance for coding agents, not a standard): the same tool with the same arguments this
# many times in a row, or one file edited more than MAX_FILE_EDITS times while the failing test output has not changed.
LOOP_REPEAT = 3
MAX_FILE_EDITS = 5
LESSON_CHARS = 600
LESSON_STATUSES = ("gave_up", "failed", "timed_out")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")  # Claude Code's own editing tools

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


class RunTimedOut(Exception):
    """Raised by AgentRuns.step() once a run has passed RUN_CAP: the hard stop for a loop that records its steps."""


def short_hash(value: Any) -> str:
    """8 hex characters standing for `value` (tool arguments, test output). Only the digest is ever stored, so the trail
    can show that two calls were identical without keeping what was in them (paths, patterns, file text, secrets)."""
    try:
        raw = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(value)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:8]


def edited_file(name: str, args: Any) -> str:
    """The path a tool call writes to, or '' if it is not an edit."""
    a = args if isinstance(args, dict) else {}
    if name == "str_replace_based_edit_tool":
        return str(a.get("path") or "")[:200] if a.get("command") in ("str_replace", "create", "insert") else ""
    if name in EDIT_TOOLS:
        return str(a.get("file_path") or a.get("path") or "")[:200]
    return ""


def detect_loop(trail: list[dict[str, Any]]) -> str:
    """Why the trail looks like a stuck loop, or '' if it does not. Only entries recorded with a call hash count."""
    calls = [e for e in trail if isinstance(e, dict) and e.get("h")]
    if not calls:
        return ""
    last, run = calls[-1], 0
    for e in reversed(calls):
        if (e.get("tool"), e.get("h")) != (last.get("tool"), last.get("h")):
            break
        run += 1
    if run >= LOOP_REPEAT:
        return f"{last.get('tool')} called {run} times in a row with the same arguments"
    # edits since the failing test output last changed (an entry without "out" inherits the one before it)
    outs, cur = [], ""
    for e in calls:
        cur = e.get("out", cur)
        outs.append(cur)
    counts: dict[str, int] = {}
    for e, out in zip(calls, outs):
        if out == outs[-1] and e.get("file"):
            counts[e["file"]] = counts.get(e["file"], 0) + 1
    for path, n in counts.items():
        if n > MAX_FILE_EDITS:
            return f"{path} edited {n} times with no change in the failing test output"
    return ""


def derive_lesson(status: str, outcome: str, trail: list[dict[str, Any]] | None = None, looping: str = "") -> str:
    """One paragraph for a run that did not succeed: the likely cause and what information or tool would have let it
    succeed. Worked out from the recorded outcome and trail (no model call), so it is a starting point for a person
    reading agent_runs, not a diagnosis. Never raises."""
    try:
        what = _clip(outcome, 200) or "no reason was recorded"
        low = (outcome or "").lower()
        steps = [e.get("step", "") for e in (trail or []) if isinstance(e, dict)]
        last = steps[-1] if steps else "nothing recorded"
        if status == "timed_out":
            cause = f"The run hit the wall-clock cap before finishing (last known activity: {last})."
            need = ("A narrower request naming the files and the behaviour wanted, or a smaller first step, would let "
                    "it finish inside the cap.")
        elif looping:
            cause = f"The run got stuck repeating itself: {looping}."
            need = ("The exact failing test or CI output (the CI log tool) and the file that should change, and a tool "
                    "that runs the tests so it can see whether an edit helped, would have let it break out of the loop.")
        elif "turn budget" in low or "stopped after" in low:
            cause = f"The run used its whole turn budget without finishing ({what})."
            need = "A narrower request that names the files to change would have let it get there in fewer turns."
        elif any(w in low for w in ("approval", "safety", "authentication", "declined", "refus")):
            cause = f"The run declined because the request touched something protected ({what})."
            need = ("Nothing was missing - declining was the right call. The request would need rewording so it does "
                    "not weaken a safety check.")
        elif status == "failed" and any(w in low for w in ("404", "401", "403", "not found", "permission", "token")):
            cause = f"The run failed on a GitHub or access error ({what})."
            need = ("A working GitHub token with access to the repository, and a check of that access before the run "
                    "starts, would have let it succeed.")
        elif status == "failed":
            cause = f"The run failed with an error ({what})."
            need = ("The full error and the step it came from (see the trail), and a retry once the cause is fixed, "
                    "would let it succeed.")
        elif NO_RESULT_NOTE.lower() in low or not outcome:
            cause = f"The run finished without submitting a change or saying why (last step: {last})."
            need = ("A clearer request, and a requirement that the agent always ends by submitting or giving up with an "
                    "analysis, would have let it succeed.")
        else:
            cause = f"The run gave up: {what}"
            need = ("A more specific request (the files or behaviour wanted) and the failing output or log it needed "
                    "would have let it make a safe change.")
        return _clip(f"{cause} {need}", LESSON_CHARS)
    except Exception:  # noqa: BLE001 - observability must never break a run
        return ""


class AgentRuns:
    def __init__(self, db: Database):
        self.db = db
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """Add the lesson / looping columns to an agent_runs table that lacks them (safe to repeat; never raises)."""
        try:
            have = {r["name"] for r in self.db.query("PRAGMA table_info(agent_runs)")}
            for col in ("lesson", "looping"):
                if have and col not in have:
                    self.db.execute(f"ALTER TABLE agent_runs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        except Exception:  # noqa: BLE001 - e.g. another process added it first
            log.warning("Could not add the lesson/looping columns to agent_runs", exc_info=True)

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
        # who asked (the owner's own turn - no caller - is the owner's): recorded so a run can be seen to be a manager's request
        run_id = self.db.execute(
            "INSERT INTO agent_runs (kind, request, started_at, updated_at, status, requested_role) VALUES (?,?,?,?,'running',?)",
            (kind, _clip(request, 500), ts, ts, access.role_of(access.current_caller.get())))
        self.db.execute("DELETE FROM agent_runs WHERE id <= ?", (run_id - KEEP_RUNS,))
        return run_id

    def step(self, tool: str, args: Any, ok: bool = True, run_id: int | None = None,
             test_output: str | None = None) -> str | None:
        """Add one line to the trail of the run this task is working on (a no-op outside a tracked run).

        Besides the one-line summary the trail keeps the tool name and a short hash of the arguments (never the arguments),
        the path for an edit, and - when the caller passes the latest failing test / CI output - a hash of that output.
        From those a run is flagged 'looping' (see detect_loop). Returns a one-line nudge for the agent the first time
        the run is flagged (a caller may pass it on; ignoring it is fine), else None. Raises RunTimedOut - the one thing
        here that does raise - once the run is past RUN_CAP, after marking it timed_out."""
        run_id = run_id or _current.get()
        if not run_id:
            return None
        meta: dict[str, Any] = {"tool": _clip(tool, 60), "h": short_hash(args)}
        path = edited_file(tool, args)
        if path:
            meta["file"] = path
        if test_output is not None:
            meta["out"] = short_hash(test_output)
        nudge = self.note(describe_call(tool, args, ok), run_id, meta)
        self.enforce_cap(run_id)
        return nudge or None

    def record_message(self, message: Any, run_id: int | None = None) -> str | None:
        """For a backend that streams messages (e.g. Claude Code): record every tool call in one message as a step.
        Duck-typed - anything with a list `content` whose blocks have `name` and `input` - so it needs no SDK import.
        Returns the first nudge, if any. Text blocks and tool results are ignored."""
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return None
        nudge = None
        for block in content:
            name = getattr(block, "name", None)
            if isinstance(name, str) and name and hasattr(block, "input"):
                nudge = nudge or self.step(name, block.input, run_id=run_id)
        return nudge

    def note(self, line: str, run_id: int | None = None, meta: dict[str, Any] | None = None) -> str:
        """Append a trail line (with optional `meta` keys stored beside it). Returns the loop reason if this call newly
        flagged the run as looping, else ''."""
        run_id = run_id or _current.get()
        if not run_id:
            return ""
        try:
            row = self.db.query_one("SELECT trail, steps, looping FROM agent_runs WHERE id = ?", (run_id,))
            if not row:
                return ""
            ts = now_iso()
            trail = json.loads(row["trail"] or "[]")
            trail.append({"at": ts, "step": _clip(line, LINE_CHARS), **(meta or {})})
            trail = trail[-MAX_TRAIL:]
            was = row.get("looping") or ""
            reason = was or (detect_loop(trail) if meta else "")
            self.db.execute("UPDATE agent_runs SET trail = ?, steps = ?, looping = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(trail), row["steps"] + 1, reason, ts, run_id))
            if reason and not was:
                log.warning("Engineering run #%s looks stuck: %s", run_id, reason)
                return f"Looks stuck: {reason}. Try a different approach, or stop and give up with what you found."
        except Exception:  # noqa: BLE001 - observability must never break a run
            log.warning("Could not record agent run step", exc_info=True)
        return ""

    def _timeout_note(self, run_id: int) -> str:
        row = self.db.query_one("SELECT trail, steps, updated_at FROM agent_runs WHERE id = ?", (run_id,)) or {}
        trail = json.loads(row.get("trail") or "[]")
        last = trail[-1]["step"] if trail else "none recorded"
        return (f"Stopped at the {int(RUN_CAP.total_seconds() // 60)}-minute wall-clock cap. Last known activity: "
                f"{last} (at {row.get('updated_at', '?')}, {row.get('steps', 0)} steps).")

    def timeout(self, run_id: int) -> bool:
        """Mark a still-running run timed_out, with its last-known activity. True if this call did it. Never raises."""
        try:
            note = self._timeout_note(run_id)
            if not self.db.query_one("SELECT 1 AS x FROM agent_runs WHERE id = ? AND status = 'running'", (run_id,)):
                return False
            self.finish("timed_out", note, run_id, only_if_running=True)
            return True
        except Exception:  # noqa: BLE001
            log.warning("Could not record agent run timeout", exc_info=True)
            return False

    def enforce_cap(self, run_id: int, now: datetime | None = None) -> None:
        """Raise RunTimedOut (after recording timed_out) if the run is still running past RUN_CAP."""
        now = now or datetime.now(timezone.utc)
        try:
            row = self.db.query_one("SELECT started_at, status FROM agent_runs WHERE id = ?", (run_id,))
            over = bool(row) and row["status"] == "running" and now - _parse(row["started_at"], now) >= RUN_CAP
        except Exception:  # noqa: BLE001
            log.warning("Could not check the run time cap", exc_info=True)
            return
        if over:
            self.timeout(run_id)
            raise RunTimedOut(f"Run #{run_id} passed its {int(RUN_CAP.total_seconds() // 60)}-minute wall-clock cap")

    def finish(self, status: str, outcome: str = "", run_id: int | None = None, only_if_running: bool = False,
               lesson: str | None = None) -> None:
        """status is submitted / gave_up / failed / timed_out / interrupted.

        A run that ends gave_up, failed or timed_out also stores a one-paragraph `lesson` (cause, and what information
        or tool would have let it succeed): the one passed in, else one worked out by derive_lesson. A run already
        marked timed_out keeps that status - the error its own hard stop causes must not overwrite it."""
        run_id = run_id or _current.get()
        if not run_id:
            return
        try:
            sets, params = ["status = ?", "outcome = ?"], [status, _clip(outcome, 500)]
            if status in LESSON_STATUSES:
                if lesson is None:
                    row = self.db.query_one("SELECT trail, looping FROM agent_runs WHERE id = ?", (run_id,)) or {}
                    lesson = derive_lesson(status, outcome, json.loads(row.get("trail") or "[]"),
                                           row.get("looping") or "")
                sets.append("lesson = ?")
                params.append(_clip(lesson, LESSON_CHARS))
            sets.append("updated_at = ?")
            params += [now_iso(), run_id]
            where = "id = ?"
            if only_if_running:
                where += " AND status = 'running'"
            elif status != "timed_out":
                where += " AND status != 'timed_out'"
            self.db.execute(f"UPDATE agent_runs SET {', '.join(sets)} WHERE {where}", tuple(params))
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
        timer = self._arm_cap(run_id)
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
            if timer is not None:
                timer.cancel()
            _current.reset(token)

    def _arm_cap(self, run_id: int | None) -> asyncio.TimerHandle | None:
        """The hard stop for a backend that reports no steps (Claude Code): when RUN_CAP passes, mark the run timed_out and
        cancel the task running it. track() then re-raises the cancellation and leaves the timed_out status alone. Does
        nothing outside a running event loop task."""
        if not run_id:
            return None
        try:
            loop, task = asyncio.get_running_loop(), asyncio.current_task()
            if task is None:
                return None

            def expire() -> None:
                if self.timeout(run_id):
                    log.warning("Engineering run #%s passed its wall-clock cap; cancelling it", run_id)
                    task.cancel()

            return loop.call_later(RUN_CAP.total_seconds(), expire)
        except RuntimeError:  # no running event loop (a plain synchronous caller)
            return None

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
            elapsed = now - _parse(r["started_at"], now)
            running = r["status"] == "running"
            looping = r.get("looping") or ""
            if running and idle >= STALL_AFTER:
                status = "stalled"
            elif running and elapsed >= RUN_CAP:  # past the cap and nothing has stopped it (it is not this process's)
                status = "timed_out"
            elif running and looping:
                status = "looping"
            else:
                status = r["status"]
            trail = json.loads(r["trail"] or "[]")
            out.append({
                "id": r["id"], "kind": r["kind"], "request": r["request"], "status": status,
                "started_at": r["started_at"], "last_activity_at": r["updated_at"],
                "last_step": trail[-1]["step"] if trail else None,
                "idle_minutes": max(0, int(idle.total_seconds() // 60)),
                "elapsed_minutes": max(0, int(elapsed.total_seconds() // 60)), "steps": r["steps"],
                "outcome": r["outcome"], "lesson": r.get("lesson") or None, "looping": looping or None,
                "requested_role": r["requested_role"] or None,
                "trail": trail if run_id is not None else trail[-10:],
            })
        return out
