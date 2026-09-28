"""Situation reports: the live numbers behind the display, and the spoken morning briefing."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from ..brain import llm

log = logging.getLogger(__name__)

BRIEFING_SYSTEM = """You are Jarvis, the AI assistant of {company}, briefing {owner}, the director. Write a morning
briefing that will be read aloud: warm, natural, confident British English, like a trusted chief of staff.
Lead with anything urgent (life-safety faults, systems down, overdue call-outs, cash problems), then today's
jobs and staff, inbox highlights, money, and anything due soon. Round numbers sensibly for speech (say
"about twelve thousand pounds"). No lists, headings or markdown - flowing speech, 150-250 words. If the data is
marked demo, mention once that it's demo data. Only use the data provided; never invent facts."""


async def _safe(coro, label: str) -> Any:
    try:
        return await coro
    except Exception as e:  # noqa: BLE001
        log.warning("status %s failed: %s", label, e)
        return {"error": f"{type(e).__name__}: {e}"[:200]}


class Briefings:
    def __init__(self, settings, db, mail, staff, accountant, notifier, client):
        self.s = settings
        self.db = db
        self.mail = mail
        self.staff = staff
        self.accountant = accountant
        self.notifier = notifier
        self.client = client

    async def status(self) -> dict[str, Any]:
        """Everything the HUD panels show, gathered in parallel."""
        inbox, board, overdue, finance = await asyncio.gather(
            _safe(self.mail.list_messages(unread_only=True, top=8), "inbox"),
            _safe(self.staff.board(), "staff"),
            _safe(self.staff.overdue_jobs(), "overdue"),
            _safe(self.accountant.snapshot(), "finance"),
        )
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "inbox": {"demo": getattr(self.mail, "demo", False), "unread": inbox},
            "staff": board,
            "overdue_jobs": overdue,
            "finance": finance,
            "issues": [i for i in self.db.list_issues("open", 20)],
            "tests": self.db.latest_test_results(),
            "approvals": self.db.pending_actions(),
            "notifications": self.db.recent_notifications(15),
            "deadlines": self.accountant.deadlines(),
        }

    async def morning_briefing(self, deliver: bool = True) -> str:
        data = await self.status()
        data["certs_expiring"] = await _safe(self.staff.expiring_certifications(30), "certs")
        data.pop("notifications", None)
        text = await llm.write(
            self.client, self.s,
            system=BRIEFING_SYSTEM.format(company=self.s.company_name, owner=self.s.owner_name),
            prompt=f"Today is {datetime.now():%A %d %B %Y}. Current data:\n{json.dumps(data, default=str)[:60000]}",
            effort="medium")
        if deliver:
            await self.notifier.notify("Morning briefing", text, level="info", push=False, speak=True)
            await self.notifier.send_owner_update(f"Morning briefing {datetime.now():%a %d %b}", text,
                                                  channels=("teams", "email"))
        return text
