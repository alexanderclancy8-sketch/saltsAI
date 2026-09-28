"""Proactive suggestions. Jarvis never acts on its own: it looks through the data a few times a
day and proposes useful next steps. "Do it" hands the suggestion to Jarvis, which prepares the
work and queues anything that changes something for the owner's approval."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)
SNOOZE = timedelta(hours=20)  # a dismissed suggestion stays quiet until tomorrow


class Suggestions:
    def __init__(self, j):
        self.j = j

    async def _candidates(self) -> list[dict[str, Any]]:
        j = self.j
        out: list[dict[str, Any]] = []

        def add(key: str, title: str, detail: str, prompt: str, priority: int = 2) -> None:
            out.append({"key": key, "title": title, "detail": detail, "prompt": prompt, "priority": priority})

        async def safe(coro):
            try:
                return await coro
            except Exception as e:  # noqa: BLE001 - one broken source mustn't stop the sweep
                log.info("suggestion source failed: %s", e)
                return None

        from .remedials import remedial_pipeline

        unbilled, remedials, overdue, credit, certs, health = await asyncio.gather(
            safe(j.billing.unbilled_jobs(30)), safe(remedial_pipeline(j.fsm)), safe(j.staff.overdue_jobs()),
            safe(j.accountant.credit_control()), safe(j.staff.expiring_certifications(30)),
            safe(j.customers.scores(refresh=True)))

        for cust in ((health or {}).get("customers") or []):
            renewal = cust.get("renewal_in_days")
            near_renewal = renewal is not None and renewal <= 90
            if cust["status"] == "at risk" or (cust["status"] == "watch" and near_renewal):
                when = (f" before renewal in {renewal} days" if near_renewal and renewal >= 0
                        else " - contract renewal has passed" if renewal is not None and renewal < 0 else "")
                add(f"customer:{cust['customer']}",
                    f"{cust['customer']} looks {cust['status']}{when} (score {cust['score']}) - plan a call?",
                    "; ".join(cust["reasons"][:3]),
                    f"{cust['customer']} is showing warning signs in the customer health watch. Explain what's going "
                    f"on, recommend how to win them back, and draft an email to arrange a call, for my approval.",
                    1 if cust["status"] == "at risk" else 2)
        conc = (health or {}).get("concentration")
        if conc and conc.get("warning"):
            add("concentration", f"{conc['customer']} is {conc['share_pct']}% of revenue - reduce the dependency?",
                "Losing one customer that size would hurt.",
                "We rely heavily on one customer. What's the risk and how should we diversify?", 3)

        if unbilled and unbilled["count"]:
            add("unbilled", f"Invoice {unbilled['count']} completed jobs (£{unbilled['net_total']:,.0f} + VAT)?",
                "Finished in Salts FSM but not found in the accounts.",
                "Draft invoices for the completed jobs that haven't been invoiced, for my approval.", 1)
        if remedials and remedials["needs_chasing"]:
            n = len(remedials["needs_chasing"])
            value = sum(r["value"] for r in remedials["needs_chasing"])
            add("remedials", f"Chase {n} remedial quote{'s' if n > 1 else ''} (£{value:,.0f})?",
                "Sent over 7 days ago with no decision.",
                "Which remedial quotes need chasing? Draft the chaser emails for my approval.", 2)
        for jb in (overdue or [])[:3]:
            if jb["engineer"] == "UNASSIGNED" and jb.get("site"):
                near = await safe(j.tracker.nearest(str(jb["site"])))
                best = (near or {}).get("engineers", [None])[0] if near and not near.get("error") else None
                who = f" - {best['engineer']} is nearest ({best['eta_mins']} min)" if best else ""
                add(f"assign:{jb['job']}", f"Assign overdue {jb.get('type') or 'job'} {jb['job']} at {jb['site']}{who}?",
                    f"{jb['hours_overdue']}h overdue for {jb.get('customer')}.",
                    f"Job {jb['job']} at {jb['site']} is unassigned and overdue. Who should take it? Prepare the "
                    f"assignment in Salts FSM for my approval.", 1)
        if credit and credit.get("actions"):
            late = [a for a in credit["actions"] if a["days_overdue"] > 30]
            if late:
                add("credit", f"Chase {len(late)} invoice{'s' if len(late) > 1 else ''} over 30 days overdue (£{sum(a['amount_due'] for a in late):,.0f})?",
                    "Worst first; statutory interest can be added on business debts.",
                    "Draft credit-control chasers for invoices more than 30 days overdue, worst first, for my approval.", 2)
        try:
            await j.stores.sync()
            for po in j.stores.reorder_list()["purchase_orders"][:3]:
                n = len(po["lines"])
                add(f"reorder:{po['supplier']}", f"Reorder {n} item{'s' if n > 1 else ''} from {po['supplier']} "
                                                 f"(£{po['total_ex_vat']:,.0f})?",
                    ", ".join(line["item"] for line in po["lines"][:3]) + ("…" if len(po["lines"]) > 3 else ""),
                    f"Prepare a purchase order to {po['supplier']} for the stock below reorder level, for my approval.", 3)
        except Exception as e:  # noqa: BLE001
            log.info("stock suggestion failed: %s", e)
        for c in (certs or [])[:3]:
            add(f"cert:{c['engineer']}:{c['certificate']}",
                f"{c['engineer']}'s {c['certificate']} {'has expired' if c['expired'] else 'expires ' + c['expires']}",
                "Book the renewal before it affects site access or accreditation.",
                f"{c['engineer']}'s {c['certificate']} {'has expired' if c['expired'] else 'is expiring'}. "
                "What do they need to do to renew it, and should I draft an email to arrange it?", 2)
        upcoming = [t for t in j.accreditations.status()["timeline"] if 0 <= t["days_left"] <= 45]
        for t in upcoming[:3]:
            is_audit = t["what"].endswith(("- audit", "- renewal"))
            add(f"accred:{t['what']}", f"{t['what']} in {t['days_left']} days - "
                                       f"{'start the evidence pack?' if is_audit else 'shall I help arrange it?'}",
                t["detail"], f"Help me prepare for this: {t['what']} on {t['date']}. What needs doing and what's missing?", 2)
        failing = [r for r in j.db.latest_test_results() if not r["ok"] and r["suite"] == "system"]
        if failing:
            add("tests", f"{len(failing)} routine check{'s' if len(failing) > 1 else ''} failing - investigate?",
                "; ".join(r["name"] for r in failing[:3]),
                "Some routine system checks are failing. What's wrong and what do you suggest?", 1)
        return out

    async def sweep(self, announce: bool = True) -> list[dict[str, Any]]:
        db = self.j.db
        candidates = await self._candidates()
        keys = {c["key"] for c in candidates}
        new = []
        now = datetime.now().astimezone()
        for c in candidates:
            existing = db.get_suggestion(c["key"])
            if existing and existing["status"] in ("dismissed", "done"):
                changed_at = datetime.fromisoformat(existing["updated_at"])
                if now - changed_at < SNOOZE:
                    continue
                db.reopen_suggestion(c["key"])
            if db.upsert_suggestion(c["key"], c["title"], c["detail"], c["prompt"], c["priority"]):
                new.append(c)
        for s in db.open_suggestions():  # the underlying problem went away
            if s["key"] not in keys:
                db.set_suggestion_status(s["key"], "resolved")
        current = db.open_suggestions()
        self.j.bus.publish("suggestions", current)
        if announce and new:
            await self.j.notifier.notify(
                f"I have {len(new)} new suggestion{'s' if len(new) > 1 else ''}",
                "; ".join(c["title"] for c in new[:4]), level="info", speak=True)
        return current

    def decide(self, key: str, status: str) -> dict[str, Any] | None:
        s = self.j.db.get_suggestion(key)
        if s:
            self.j.db.set_suggestion_status(key, status)
            self.j.bus.publish("suggestions", self.j.db.open_suggestions())
        return s
