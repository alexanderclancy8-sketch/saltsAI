"""The activity log of scheduled checks: every run is recorded here, and the chat shows one collapsed line per check.

A scheduled check (the pull request watch, an automation the owner set up, the lone-worker and inbox sweeps) that finds
nothing new posts nothing into the conversation. It is still recorded, so the owner can see that it ran: the console
shows "Pull request watch · 7 checks since 09:30, no change" as one quiet line that opens to list each run with its time.
A check that DOES find something is recorded too, and still posts its message through ``Proactive`` as it always did.
Recording a run publishes nothing on the event bus (a quiet check is quiet all the way down); the console picks the log up
from /api/status, which it reads every minute and after every reply.

This only records and describes. It never posts a message itself, approves, sends or changes anything.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..history import redact_history
from ..redact import redact_text

log = logging.getLogger(__name__)

NO_CHANGE, CHANGED, FAILED, BASELINE = "no_change", "changed", "failed", "baseline"
OUTCOMES = (NO_CHANGE, CHANGED, FAILED, BASELINE)
RETENTION_DAYS = 7              # runs with nothing to report
CHANGED_RETENTION_DAYS = 31    # runs that found something or failed (what "What Jarvis did" looks back over)
MAX_RUNS_SHOWN = 40       # runs listed per check when its line is opened
DETAIL_CHARS = 200
PRUNE_EVERY = 50          # old rows are deleted once in this many recorded runs
# Jobs whose lines only the principal owner may see (summary(owner=True)): a manager or team member's console never lists them.
# "engineer_homes" is the audit of who had a home point set or cleared - the existence of a home is personal data.
OWNER_ONLY_JOBS = frozenset({"engineer_homes"})


def _clean(text: Any) -> str:
    """One short redacted line: no newlines, no credentials or access codes."""
    line = " ".join(str(text or "").split())
    return redact_text(redact_history(line))[:DETAIL_CHARS]  # no tokens, keys, credentials or access codes in the log


class ActivityLog:
    def __init__(self, j) -> None:
        self.j = j
        self._recorded = 0

    def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.j.settings.timezone)
        except Exception:  # noqa: BLE001 - an odd timezone name must not stop the log
            return ZoneInfo("UTC")

    def record(self, key: str, name: str, outcome: str, detail: str = "", error: BaseException | None = None) -> None:
        """Note one run of a scheduled check. Never raises: logging a run must not be able to break the run. A failed run opens (or
        bumps) an internal fault report and any other outcome closes it (services/faults.py); ``error`` gives it the traceback."""
        faults = getattr(self.j, "faults", None)
        if faults is not None:
            try:
                faults.check_run(key, name, outcome, detail, error)
            except Exception:  # noqa: BLE001
                log.exception("Could not update the fault log for %s", key)
        try:
            if outcome not in OUTCOMES:
                outcome = NO_CHANGE
            self.j.db.add_check_run(key, name, outcome, _clean(detail))
            self._recorded += 1
            if self._recorded % PRUNE_EVERY == 1:
                cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
                long_cutoff = datetime.now(timezone.utc) - timedelta(days=CHANGED_RETENTION_DAYS)
                self.j.db.prune_check_runs(cutoff.isoformat(timespec="seconds"), long_cutoff.isoformat(timespec="seconds"))
        except Exception:  # noqa: BLE001
            log.exception("Could not record the activity of %s", key)

    def summary(self, now: datetime | None = None, owner: bool = False) -> dict[str, Any]:
        """Today's runs (since local midnight) grouped per check, most recently run first. The owner-only jobs
        (OWNER_ONLY_JOBS) are left out unless ``owner`` is True, which only the principal owner's request passes."""
        tz = self._tz()
        local_now = (now or datetime.now(timezone.utc)).astimezone(tz)
        midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self.j.db.check_runs_since(midnight.astimezone(timezone.utc).isoformat(timespec="seconds"))
        jobs: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r["job_key"] in OWNER_ONLY_JOBS and not owner:
                continue
            try:
                at = datetime.fromisoformat(r["ran_at"]).astimezone(tz)
            except ValueError:
                continue
            job = jobs.setdefault(r["job_key"], {"key": r["job_key"], "name": r["job_name"], "checks": 0,
                                                 "no_change": 0, "changed": 0, "failed": 0, "since": at.strftime("%H:%M"),
                                                 "last": "", "runs": []})
            job["name"] = r["job_name"]
            job["checks"] += 1
            job["no_change" if r["outcome"] in (NO_CHANGE, BASELINE) else r["outcome"]] += 1
            job["last"] = at.strftime("%H:%M")
            job["runs"].append({"at": at.isoformat(timespec="seconds"), "time": at.strftime("%H:%M"),
                                "outcome": r["outcome"], "detail": r["detail"] or ""})
        out = sorted(jobs.values(), key=lambda j: j["runs"][-1]["at"], reverse=True)
        for job in out:
            job["runs"] = job["runs"][-MAX_RUNS_SHOWN:]
        return {"jobs": out}
