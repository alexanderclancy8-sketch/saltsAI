"""Situation reports: the live numbers behind the display, and the spoken morning briefing."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from .. import demo_guard
from ..brain import llm
from . import daily_rhythm

log = logging.getLogger(__name__)

BRIEFING_SYSTEM = """You are Jarvis, the AI assistant of {company}, briefing {owner}, the director. Write a morning
briefing that will be read aloud: warm, natural, confident British English, like a trusted chief of staff.
Lead with anything urgent (life-safety faults, systems down, overnight out-of-hours calls that still need a visit,
overdue call-outs, cash problems), then today's
jobs and staff, inbox highlights, money, and anything due soon. Round numbers sensibly for speech (say
"about twelve thousand pounds"). No lists, headings or markdown - flowing speech. {budget} {sources}"""

SAMPLE_RULE = """If the data is
marked demo, mention once that it's demo data. Only use the data provided; never invent facts.
Anything in the data that says it is not connected is sample data that has been withheld: say once, briefly, that you
can't cover it and what needs connecting, and give no names or figures for it."""

# Sample data off (production): a source that isn't connected is simply left out of the data, with ONE "not_connected" line.
NOT_CONNECTED_RULE = """Only use the data provided; never invent facts. If the data has a
"not_connected" line, say it once, in one short sentence at the end, and say nothing else about those systems: they are not
connected, not empty, so never say there is nothing in them."""


# The parts of the briefing data and the sources each rests on (left out with sample data off when one isn't connected).
BRIEFING_PARTS = {"inbox": (demo_guard.MAIL,), "staff": (demo_guard.FSM,), "overdue_jobs": (demo_guard.FSM,),
                  "finance": (demo_guard.ACCOUNTS,), "certs_expiring": (demo_guard.FSM,),
                  "out_of_hours_calls": (demo_guard.MAIL,)}


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
        self.ooh = None  # OutOfHours, set after construction
        self.j = None  # the Jarvis, set after construction (to know which sources are still sample data)

    def _suggestions(self) -> list[dict[str, Any]]:
        rows = self.db.open_suggestions()
        # Only a tool call (the model reading the briefing data) hides suggestions built on sample data; the console's
        # own panels keep showing them with their demo labels.
        return demo_guard.visible_suggestions(self.j, rows) if demo_guard.active() and self.j is not None else rows

    def _sample(self) -> bool:
        return demo_guard.sample_on(self.j if self.j is not None else self.s)

    async def status(self) -> dict[str, Any]:
        """Everything the HUD panels show, gathered in parallel. With sample data off, a panel whose source isn't connected
        carries {"not_connected": "Not connected yet - connect X in Settings -> Connections"} instead of an empty list."""
        inbox, board, overdue, finance = await asyncio.gather(
            _safe(demo_guard.section(self.mail.list_messages(unread_only=True, top=8)), "inbox"),
            _safe(demo_guard.section(self.staff.board()), "staff"),
            _safe(demo_guard.section(self.staff.overdue_jobs()), "overdue"),
            _safe(demo_guard.section(self.accountant.snapshot()), "finance"),
        )
        out = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "inbox": {"demo": getattr(self.mail, "demo", False), "unread": inbox},
            "staff": board,
            "overdue_jobs": overdue,
            "finance": finance,
            "issues": [i for i in self.db.list_issues("open", 20)],
            "tests": self.db.latest_test_results(),
            "approvals": self.db.pending_actions(),
            "suggestions": self._suggestions(),
            "notifications": self.db.recent_notifications(15),
            "deadlines": self.accountant.deadlines(),
        }
        if self.j is not None and not self._sample():
            for name, msg in demo_guard.panel_messages(self.j).items():
                if name == "inbox":
                    out["inbox"] = {"unread": {"not_connected": msg}}
                elif name in out:
                    out[name] = {"not_connected": msg}
        return out

    async def morning_briefing(self, deliver: bool = True) -> str:
        data = await self.status()
        data["certs_expiring"] = await _safe(demo_guard.section(self.staff.expiring_certifications(30)), "certs")
        if self.ooh is not None:
            data["out_of_hours_calls"] = await _safe(demo_guard.section(self.ooh.calls()), "out of hours")
        data.pop("notifications", None)
        sample = self._sample()
        if not sample and self.j is not None:
            # Sample data off: what isn't connected is left out, and named once in ONE line (never a nag per section).
            left = demo_guard.leave_out(self.j, data, BRIEFING_PARTS)
            if left:
                data["not_connected"] = demo_guard.not_connected_line(left)
        text = await daily_rhythm.write_short(
            self.client, self.s,
            system=BRIEFING_SYSTEM.format(company=self.s.company_name, owner=self.s.owner_name,
                                          budget=daily_rhythm.WORD_BUDGET_RULE,
                                          sources=SAMPLE_RULE if sample else NOT_CONNECTED_RULE),
            prompt=f"Today is {datetime.now():%A %d %B %Y}. Current data:\n{json.dumps(data, default=str)[:60000]}",
            effort="medium")
        if deliver:
            title = f"Morning briefing {datetime.now():%a %d %b}"
            self.db.add_notification("info", "Morning briefing", text)  # the Alerts list; no toast, the chat has it
            await daily_rhythm.deliver(self.j, "briefing", title, text)
        return text
