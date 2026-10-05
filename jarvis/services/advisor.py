"""Business advisor: pulls finance, people, sales, operations, compliance and marketing together
into board-level advice with a prioritised 90-day plan."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from typing import Any

from .. import demo_guard
from ..brain import llm

log = logging.getLogger(__name__)

ADVISOR_SYSTEM = """You are Jarvis acting as a seasoned business consultant, advisor and non-executive director to {owner}, director of
{company}, a growing fire and security company in West Yorkshire (fire alarms, emergency lighting, intruder,
CCTV, access control, extinguishers; installation + maintenance contracts).

Write a candid advisory report from the data provided. Structure:
1. Headline - two or three sentences on how the business is really doing.
2. What's working.
3. Biggest risks (cash, concentration on a few customers, people, compliance/accreditation, systems).
4. Biggest opportunities (recurring maintenance revenue, remedials from service visits, pricing, sectors such as
   schools, care, housing, logistics; accreditation; acquisitions; marketing).
5. A prioritised 90-day plan: 5-8 concrete actions, each with an owner (by role), a measurable target and why it
   matters in pounds where possible.
6. Questions {owner} should be asking.

Also work through these steps in your analysis and show them in the report:
- Cost segmentation (diagnostic): split costs and margin into labour, hardware/materials and maintenance-contract
  work, and say which segment is driving the problem or the opportunity. Where the data doesn't split them, say
  so rather than inventing figures.
- UK compliance check (its own section): note the HMRC and wider UK-compliance points that bear on the advice
  (VAT and the construction domestic reverse charge, CIS, PAYE/NIC, corporation tax, employment law, accreditation
  requirements). Final decisions should be checked with the qualified accountant.
- Always finish with exactly three concrete recommendations, each framed around one of: risk mitigation, tax efficiency,
  or business development. Label each with its frame and give the action, the expected effect in pounds where
  possible, and the risk.

If a focus area is given, run a consultant-style deep dive on it (the relevant framework, benchmarks for UK fire &
security SMEs where you know them - label them as typical ranges - options with cost, payback and risk, then a
recommendation) before the general sections.
Be direct and specific to this business and these numbers - no generic MBA filler. Where figures are estimates or
demo data, say so. Use markdown headings and bullets. {focus}"""


async def _safe(coro, label: str) -> Any:
    try:
        return await coro
    except Exception as e:  # noqa: BLE001
        log.warning("advisor input %s failed: %s", label, e)
        return {"error": f"{type(e).__name__}: {e}"[:200]}


class Advisor:
    def __init__(self, settings, db, accountant, reviewer, staff, marketing, notifier, client, bus):
        self.s = settings
        self.db = db
        self.accountant = accountant
        self.reviewer = reviewer
        self.staff = staff
        self.marketing = marketing
        self.notifier = notifier
        self.client = client
        self.bus = bus
        self.j_customers = None  # CustomerHealth, set after construction

    async def gather(self) -> dict[str, Any]:
        health, team, prod, snapshot, cashflow, credit, socials = await asyncio.gather(
            _safe(demo_guard.section(self.accountant.health_check(90)), "health"),
            _safe(demo_guard.section(self.reviewer.review(30)), "team"),
            _safe(self.staff.productivity(30), "productivity"),
            _safe(demo_guard.section(self.accountant.snapshot()), "snapshot"),
            _safe(demo_guard.section(self.accountant.cashflow(13)), "cashflow"),
            _safe(demo_guard.section(self.accountant.credit_control()), "credit"),
            _safe(demo_guard.section(self.marketing.overview(30)), "marketing"),
        )
        customers = (await _safe(demo_guard.section(self.j_customers.scores()), "customers")
                     if self.j_customers else None)
        if isinstance(customers, dict) and "customers" in customers:
            customers = {"at_risk": customers["at_risk"], "watch": customers["watch"][:8],
                         "concentration": customers["concentration"]}
        if isinstance(cashflow, dict) and "weeks" in cashflow:
            cashflow = {k: v for k, v in cashflow.items() if k != "weeks"}
        if isinstance(credit, dict) and "actions" in credit:
            credit = {"total_overdue": credit.get("total_overdue"), "worst": credit["actions"][:8]}
        return {"date": date.today().isoformat(), "business_health": health, "finance_snapshot": snapshot,
                "cashflow_summary": cashflow, "credit_control": credit, "team_review_30d": team,
                "engineer_productivity_30d": prod.get("team") if isinstance(prod, dict) else prod,
                "marketing": socials, "customer_health": customers, "open_issues": len(self.db.list_issues("open", 200)),
                "failing_routine_tests": [t for t in self.db.latest_test_results() if not t["ok"]],
                "deadlines": self.accountant.deadlines()}

    async def report(self, focus: str | None = None, deliver: bool = False) -> str:
        data = await self.gather()
        focus_line = f"Pay particular attention to: {focus}." if focus else ""
        text = await llm.write(self.client, self.s,
                               system=ADVISOR_SYSTEM.format(owner=self.s.owner_name, company=self.s.company_name,
                                                            focus=focus_line),
                               prompt="Business data (JSON):\n" + json.dumps(data, default=str)[:80000],
                               effort="high", max_tokens=16000)
        self.bus.publish("display", {"title": "Business advisory report", "markdown": text})
        if deliver:
            await self.notifier.notify("Monthly business advisory report", text[:3000], level="info", push=True,
                                       importance="info", management_only=True)  # finance: Alex/Chun only
        return text
