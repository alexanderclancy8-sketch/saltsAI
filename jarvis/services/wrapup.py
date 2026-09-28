"""End-of-day wrap-up: what got done, what slipped, what's waiting on the owner, and what's first
tomorrow - spoken on the display and sent to the owner on Teams/email."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

from ..brain import llm

log = logging.getLogger(__name__)
DONE = {"completed", "complete", "done", "closed", "signed_off"}

WRAPUP_SYSTEM = """You are Jarvis, the AI assistant of {company}, giving {owner} his end-of-day wrap-up. It will be
read aloud: natural, warm British English, calm and to the point, like a trusted chief of staff closing the day.
Cover, in this order:
1. What got done today - jobs completed against booked, and anything notable.
2. What slipped - jobs not finished, overdue or unassigned call-outs, late starts - and what should happen to them.
3. What's waiting on him - approvals and the most useful suggestions.
4. Tomorrow - how many jobs, the first starts, anything unassigned or risky, deadlines coming up.
Finish with one "Shall I...?" offer for the single most useful next step (you never act without his approval).
Round numbers for speech. No lists, headings or markdown - flowing speech, 150-250 words. If the data is demo
data, say so once. Only use the data provided; never invent facts."""


async def _safe(coro, label: str) -> Any:
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 - one broken source mustn't spoil the wrap-up
        log.warning("wrap-up source %s failed: %s", label, e)
        return {"error": f"{type(e).__name__}: {e}"[:200]}


class WrapUp:
    def __init__(self, j):
        self.j = j

    async def gather(self, day: date | None = None) -> dict[str, Any]:
        j = self.j
        day = day or date.today()
        tomorrow = day + timedelta(days=1)
        while tomorrow.weekday() >= 5:  # Friday's wrap-up looks ahead to Monday
            tomorrow += timedelta(days=1)

        board, overdue, jobs_today, jobs_next, inbox, fleet, finance = await asyncio.gather(
            _safe(j.staff.board(), "board"), _safe(j.staff.overdue_jobs(), "overdue"),
            _safe(j.fsm.jobs(day, day), "jobs today"), _safe(j.fsm.jobs(tomorrow, tomorrow), "jobs tomorrow"),
            _safe(j.mail.list_messages(unread_only=True, top=25), "inbox"), _safe(j.tracker.live(), "fleet"),
            _safe(j.accountant.snapshot(), "finance"))

        slipped = []
        if isinstance(jobs_today, list):
            slipped = [{"job": x.get("ref"), "site": x.get("site"), "engineer": x.get("engineer") or "UNASSIGNED",
                        "status": x.get("status"), "type": x.get("type")}
                       for x in jobs_today if str(x.get("status") or "").lower() not in DONE]

        tomorrow_view: dict[str, Any] = {"date": tomorrow.isoformat()}
        if isinstance(jobs_next, list):
            first: dict[str, str] = {}
            for x in sorted(jobs_next, key=lambda x: str(x.get("scheduled_start"))):
                eng = x.get("engineer") or "UNASSIGNED"
                first.setdefault(eng, f"{str(x.get('scheduled_start'))[11:16]} {x.get('site')}")
            tomorrow_view.update(jobs=len(jobs_next), first_jobs=first,
                                 unassigned=[x.get("ref") for x in jobs_next if not x.get("engineer")])

        today_iso = day.isoformat()
        issues = j.db.list_issues(None, 200)
        still_out = []
        if isinstance(fleet, dict):
            still_out = [e["engineer"] for e in fleet.get("engineers", [])
                         if e.get("status") == "driving" or e.get("current_job")]
        unread = inbox if isinstance(inbox, list) else []
        return {
            "date": today_iso,
            "demo": getattr(j.fsm, "demo", False),
            "today": {k: board.get(k) for k in ("jobs_today", "completed_today", "late_starts", "unassigned_jobs_today")}
                     if isinstance(board, dict) and "error" not in board else board,
            "engineers": board.get("engineers") if isinstance(board, dict) else None,
            "slipped_today": slipped,
            "overdue_jobs": overdue,
            "engineers_still_out": still_out,
            "issues_raised_today": [i["title"] for i in issues if i["created_at"][:10] == today_iso],
            "issues_fixed_today": [i["title"] for i in issues if i["status"] == "resolved" and i["updated_at"][:10] == today_iso],
            "awaiting_approval": [a["summary"] for a in j.db.pending_actions()],
            "suggestions": [s["title"] for s in j.db.open_suggestions()],
            "unread_email": {"count": len(unread),
                             "important": [m["subject"] for m in unread if m.get("importance") == "high"][:5]},
            "money": {k: finance.get(k) for k in ("cash_at_bank", "debtors_overdue", "vat_due")}
                     if isinstance(finance, dict) and "error" not in finance else finance,
            "failing_checks": [t["name"] for t in j.db.latest_test_results() if not t["ok"]],
            "tomorrow": tomorrow_view,
            "deadlines_next_14_days": [d for d in j.accountant.deadlines() if 0 <= d["days_left"] <= 14],
        }

    async def run(self, deliver: bool = True) -> str:
        j = self.j
        try:
            await j.suggestions.sweep(announce=False)  # make sure the suggestions are current
        except Exception as e:  # noqa: BLE001
            log.info("suggestion refresh before wrap-up failed: %s", e)
        data = await self.gather()
        text = await llm.write(
            j.client, j.settings,
            system=WRAPUP_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name),
            prompt=f"It is {datetime.now():%A %d %B %Y, %H:%M}. End-of-day data:\n{json.dumps(data, default=str)[:60000]}",
            effort="medium")
        if deliver:
            await j.notifier.notify("End-of-day wrap-up", text, level="info", push=False, speak=True)
            await j.notifier.send_owner_update(f"End-of-day wrap-up {datetime.now():%a %d %b}", text,
                                               channels=("teams", "email"))
        return text
