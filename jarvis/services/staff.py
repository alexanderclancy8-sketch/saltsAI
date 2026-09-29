"""Workforce oversight from Salts FSM data: who is where, late starts, overdue work,
expiring qualifications and performance.

This uses the job/timesheet data engineers already record in the FSM. Tell staff
how this data is used (UK GDPR transparency) - see README.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

LATE_START_GRACE = timedelta(minutes=30)
OPEN_STATUSES = {"scheduled", "assigned", "open", "pending", "booked", "in_progress", "started", "on_site", "travelling"}
DONE_STATUSES = {"completed", "complete", "done", "closed", "invoiced", "signed_off"}


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


def _status(job: dict[str, Any]) -> str:
    return str(job.get("status") or "").lower().replace(" ", "_")


class StaffMonitor:
    def __init__(self, fsm):
        self.fsm = fsm

    @property
    def demo(self) -> bool:
        return getattr(self.fsm, "demo", False)

    async def board(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now()
        today = now.date()
        staff = await self.fsm.staff()
        jobs = await self.fsm.jobs(date_from=today, date_to=today)
        by_eng: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for j in jobs:
            by_eng[(j.get("engineer") or "Unassigned")].append(j)

        rows, late_starts = [], []
        for s in staff:
            mine = sorted(by_eng.get(s["name"], []), key=lambda j: str(j.get("scheduled_start")))
            done = [j for j in mine if _status(j) in DONE_STATUSES]
            current = next((j for j in mine if _status(j) in ("in_progress", "started", "on_site")), None)
            for j in mine:
                start = _dt(j.get("scheduled_start"))
                if start and _status(j) in OPEN_STATUSES and not j.get("started_at") and now - start > LATE_START_GRACE:
                    late_starts.append({"engineer": s["name"], "job": j.get("ref") or j.get("id"), "site": j.get("site"),
                                        "scheduled": j.get("scheduled_start"),
                                        "minutes_late": int((now - start).total_seconds() // 60)})
            rows.append({
                "name": s["name"], "role": s.get("role"),
                "status": "on job" if current else ("finished for today" if mine and len(done) == len(mine)
                                                     else s.get("status") or "available"),
                "current_job": (f"{current.get('ref')} · {current.get('site')}" if current else None),
                "jobs_today": len(mine), "completed_today": len(done),
                "next_job": next((f"{j.get('scheduled_start', '')[11:16]} {j.get('site')}" for j in mine
                                  if _status(j) in OPEN_STATUSES and j is not current), None),
                "hours_this_week": s.get("hours_this_week"),
            })
        unassigned = [{"job": j.get("ref") or j.get("id"), "site": j.get("site"), "type": j.get("type"),
                       "scheduled": j.get("scheduled_start")} for j in by_eng.get("Unassigned", [])]
        return {"date": today.isoformat(), "demo": self.demo, "engineers": rows, "late_starts": late_starts,
                "unassigned_jobs_today": unassigned, "jobs_today": len(jobs),
                "completed_today": sum(r["completed_today"] for r in rows)}

    async def overdue_jobs(self, now: datetime | None = None, lookback_days: int = 30) -> list[dict[str, Any]]:
        now = now or datetime.now()
        jobs = await self.fsm.jobs(date_from=now.date() - timedelta(days=lookback_days), date_to=now.date())
        out = []
        for j in jobs:
            if _status(j) not in OPEN_STATUSES:
                continue
            end = _dt(j.get("scheduled_end")) or ((_dt(j.get("scheduled_start")) or now) + timedelta(hours=4))
            if end < now:
                out.append({"job": j.get("ref") or j.get("id"), "type": j.get("type"), "status": j.get("status"),
                            "site": j.get("site"), "customer": j.get("customer"),
                            "engineer": j.get("engineer") or "UNASSIGNED", "was_due": end.isoformat(timespec="minutes"),
                            "priority": j.get("priority"), "hours_overdue": int((now - end).total_seconds() // 3600)})
        return sorted(out, key=lambda r: -r["hours_overdue"])

    async def expiring_certifications(self, within_days: int = 60, today: date | None = None) -> list[dict[str, Any]]:
        today = today or date.today()
        out = []
        for s in await self.fsm.staff():
            for c in s.get("certifications") or []:
                if not isinstance(c, dict):
                    continue
                exp = c.get("expires") or c.get("expiry") or c.get("expiryDate")
                try:
                    d = date.fromisoformat(str(exp)[:10])
                except ValueError:
                    continue
                days = (d - today).days
                if days <= within_days:
                    out.append({"engineer": s["name"], "certificate": c.get("name") or c.get("title"),
                                "expires": d.isoformat(), "days_left": days, "expired": days < 0})
        return sorted(out, key=lambda r: r["days_left"])

    async def productivity(self, days: int = 30, engineer: str | None = None,
                           today: date | None = None) -> dict[str, Any]:
        """Per-engineer productivity for the last ``days`` days, compared with the team and the previous period."""
        today = today or date.today()
        current = await self._productivity_period(today - timedelta(days=days - 1), today)
        previous = await self._productivity_period(today - timedelta(days=2 * days - 1), today - timedelta(days=days))
        prev_by_name = {r["engineer"]: r for r in previous["engineers"]}
        for r in current["engineers"]:
            p = prev_by_name.get(r["engineer"])
            r["vs_previous_period"] = {
                k: (round(r[k] - p[k], 1) if r.get(k) is not None and p and p.get(k) is not None else None)
                for k in ("jobs_completed", "utilisation_pct", "revenue", "on_time_start_pct", "jobs_per_day")}
        if engineer:
            current["engineers"] = [r for r in current["engineers"] if engineer.lower() in r["engineer"].lower()]
        current["previous_period_team"] = previous["team"]
        current["how_to_read"] = ("utilisation = hours on jobs / hours on timesheets; on-time start = started within "
                                  "30 min of the booked time; revisit rate = call-outs followed by another call-out "
                                  "to the same site within 30 days (repeat faults - lower is better, a proxy for "
                                  "first-time fix; needs 8+ call-outs to be meaningful).")
        return current

    async def _productivity_period(self, start: date, end: date) -> dict[str, Any]:
        jobs = await self.fsm.jobs(date_from=start, date_to=end)
        try:
            sheets = await self.fsm.timesheets(start, end)
        except Exception:  # noqa: BLE001 - timesheets are optional
            sheets = []
        hours_worked: dict[str, float] = defaultdict(float)
        days_worked: dict[str, set] = defaultdict(set)
        for t in sheets:
            hours_worked[t.get("engineer") or "?"] += float(t.get("hours") or 0)
            days_worked[t.get("engineer") or "?"].add(str(t.get("date"))[:10])

        callouts_by_site: dict[str, list[datetime]] = defaultdict(list)
        for j in jobs:
            if str(j.get("type") or "").lower() in ("callout", "call-out", "reactive") and _dt(j.get("scheduled_start")):
                callouts_by_site[str(j.get("site"))].append(_dt(j.get("scheduled_start")))

        stats: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "jobs_completed": 0, "jobs_open": 0, "revenue": 0.0, "job_hours": 0.0, "callouts": 0,
            "started": 0, "on_time": 0, "durations": [], "revisits": 0, "callouts_completed": 0, "job_days": set()})
        for j in jobs:
            name = j.get("engineer") or "Unassigned"
            st = stats[name]
            sched, began, done = _dt(j.get("scheduled_start")), _dt(j.get("started_at")), _dt(j.get("completed_at"))
            if sched:
                st["job_days"].add(sched.date().isoformat())
            if str(j.get("type") or "").lower() in ("callout", "call-out", "reactive"):
                st["callouts"] += 1
            if _status(j) not in DONE_STATUSES:
                st["jobs_open"] += 1
                continue
            st["jobs_completed"] += 1
            st["revenue"] += float(j.get("value") or 0)
            if began and done and done > began:
                hours = (done - began).total_seconds() / 3600
                st["durations"].append(hours)
                st["job_hours"] += hours
            else:
                st["job_hours"] += float(j.get("hours") or 0)
            if sched and began:
                st["started"] += 1
                st["on_time"] += int(began - sched <= LATE_START_GRACE)
            is_callout = str(j.get("type") or "").lower() in ("callout", "call-out", "reactive")
            if done and is_callout:
                st["callouts_completed"] += 1
                later = [c for c in callouts_by_site.get(str(j.get("site")), []) if done < c <= done + timedelta(days=30)]
                st["revisits"] += int(bool(later))

        rows = []
        for name, st in stats.items():
            if name == "Unassigned":
                continue
            worked = hours_worked.get(name) or None
            ndays = len(days_worked.get(name) or st["job_days"]) or None
            rows.append({
                "engineer": name,
                "jobs_completed": st["jobs_completed"],
                "jobs_open": st["jobs_open"],
                "callouts": st["callouts"],
                "hours_worked": round(worked, 1) if worked else None,
                "hours_on_jobs": round(st["job_hours"], 1),
                "utilisation_pct": round(100 * st["job_hours"] / worked, 1) if worked else None,
                "revenue": round(st["revenue"], 2),
                "revenue_per_hour_worked": round(st["revenue"] / worked, 2) if worked else None,
                "jobs_per_day": round(st["jobs_completed"] / ndays, 2) if ndays else None,
                "avg_job_hours": round(sum(st["durations"]) / len(st["durations"]), 2) if st["durations"] else None,
                "on_time_start_pct": round(100 * st["on_time"] / st["started"], 1) if st["started"] else None,
                "callouts_completed": st["callouts_completed"],
                "revisit_rate_pct": (round(100 * st["revisits"] / st["callouts_completed"], 1)
                                     if st["callouts_completed"] >= 8 else None),
            })
        rows.sort(key=lambda r: -r["revenue"])

        def avg(key: str) -> float | None:
            vals = [r[key] for r in rows if r.get(key) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        team = {k: avg(k) for k in ("jobs_completed", "utilisation_pct", "revenue", "revenue_per_hour_worked",
                                    "jobs_per_day", "avg_job_hours", "on_time_start_pct", "revisit_rate_pct")}
        return {"from": start.isoformat(), "to": end.isoformat(), "demo": self.demo,
                "team": {**team, "total_jobs_completed": sum(r["jobs_completed"] for r in rows),
                         "total_revenue": round(sum(r["revenue"] for r in rows), 2)},
                "engineers": rows}
