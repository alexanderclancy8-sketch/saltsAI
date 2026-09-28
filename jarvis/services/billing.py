"""Closing the money loop: completed Salts FSM jobs that haven't been invoiced, draft invoices
for approval (created in Sage once approved), and customer review requests after each job."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)
DONE = {"completed", "complete", "done", "closed", "signed_off"}


def _norm(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


class Billing:
    def __init__(self, settings, db, fsm, finance, actions, notifier):
        self.s = settings
        self.db = db
        self.fsm = fsm
        self.finance = finance
        self.actions = actions
        self.notifier = notifier

    async def unbilled_jobs(self, days: int = 30) -> dict[str, Any]:
        today = date.today()
        jobs = [j for j in await self.fsm.jobs(today - timedelta(days=days), today)
                if str(j.get("status") or "").lower() in DONE and float(j.get("value") or 0) > 0]
        invoices = await self.finance.invoices("receivable", outstanding_only=False, since=today - timedelta(days=days + 45))
        unbilled = []
        for jb in jobs:
            ref = str(jb.get("ref") or jb.get("id") or "")
            net = float(jb.get("value") or 0)
            matched = any(ref and ref.lower() in str(i.number).lower() for i in invoices) or any(
                _norm(i.contact) == _norm(str(jb.get("customer"))) and abs((i.total - i.tax) - net) <= max(1.0, net * 0.01)
                for i in invoices)
            if not matched and not self.db.get_kv(f"invoiced:{ref}"):
                unbilled.append({"job": ref, "customer": jb.get("customer"), "site": jb.get("site"), "type": jb.get("type"),
                                 "completed": str(jb.get("completed_at") or "")[:10], "net_value": net,
                                 "engineer": jb.get("engineer")})
        unbilled.sort(key=lambda r: r["completed"])
        return {"demo": getattr(self.fsm, "demo", False) or getattr(self.finance, "demo", False),
                "jobs": unbilled, "count": len(unbilled), "net_total": round(sum(r["net_value"] for r in unbilled), 2),
                "note": "Matched against invoices by job reference, or by customer and net value. VAT treatment "
                        "(e.g. domestic reverse charge for CIS contractors) must be checked before issuing."}

    async def queue_invoices(self, days: int = 30, limit: int = 20) -> dict[str, Any]:
        data = await self.unbilled_jobs(days)
        lines = data["jobs"][:limit]
        if not lines:
            return {"queued": 0, "message": "Everything completed in Salts FSM appears to be invoiced."}
        action_id = self.actions.queue(
            "sage_invoices", f"Create {len(lines)} invoices in Sage for completed jobs "
                             f"(£{sum(line['net_value'] for line in lines):,.2f} + VAT)", {"jobs": lines})
        return {"queued": len(lines), "action_id": action_id, "jobs": lines}

    async def create_invoices(self, jobs: list[dict[str, Any]]) -> str:
        create = getattr(self.finance, "create_sales_invoice", None)
        done, failed = [], []
        for jb in jobs:
            if create is None:
                failed.append(f"{jb['job']} (accounts system is {self.finance.name} - raise it manually)")
                continue
            try:
                number = await create(customer=jb["customer"], reference=jb["job"], net=jb["net_value"],
                                      description=f"{str(jb.get('type') or 'Works').title()} at {jb.get('site')} - job {jb['job']}")
                self.db.set_kv(f"invoiced:{jb['job']}", number or "created")
                done.append(f"{jb['job']} → {number}")
            except Exception as e:  # noqa: BLE001
                failed.append(f"{jb['job']} ({e})"[:200])
        return (f"Created {len(done)} invoice(s): " + ", ".join(done) if done else "No invoices created.") + (
            f" Not created: {'; '.join(failed)}" if failed else "")

    # ------------------------------------------------------------------ reviews
    async def queue_review_requests(self, day: date | None = None) -> dict[str, Any]:
        if not self.s.google_review_url:
            return {"queued": 0, "message": "Set GOOGLE_REVIEW_URL (your Google Business Profile review link) first."}
        day = day or date.today()
        requests = []
        for jb in await self.fsm.jobs(day, day):
            email = (jb.get("extra") or {}).get("contactEmail") or jb.get("contact_email") or (jb.get("extra") or {}).get("contact_email")
            ref = str(jb.get("ref") or jb.get("id"))
            if str(jb.get("status") or "").lower() in DONE and email and not self.db.get_kv(f"review_asked:{email.lower()}"):
                requests.append({"job": ref, "email": email, "customer": jb.get("customer"), "site": jb.get("site"),
                                 "engineer": jb.get("engineer")})
        if not requests:
            return {"queued": 0, "message": "No completed jobs with a customer email that haven't been asked already."}
        action_id = self.actions.queue("review_requests", f"Send {len(requests)} thank-you + Google review requests "
                                                          f"for today's completed jobs", {"requests": requests})
        return {"queued": len(requests), "action_id": action_id, "requests": requests}

    def review_email(self, req: dict[str, Any]) -> tuple[str, str]:
        subject = f"Thank you from {self.s.company_name}"
        body = (f"Hello,\n\nThank you for choosing {self.s.company_name}. "
                f"{(req.get('engineer') or 'Our engineer').split()[0]} completed our visit to {req.get('site')} today.\n\n"
                "If you were happy with the service, a quick Google review would mean a great deal to a local team "
                f"like ours - it takes less than a minute:\n{self.s.google_review_url}\n\n"
                "And if anything wasn't right, just reply to this email and we'll put it right.\n\n"
                f"Kind regards,\n{self.s.owner_name}\n{self.s.company_name}")
        return subject, body

    async def send_review_requests(self, requests: list[dict[str, Any]], mail) -> str:
        from ..integrations.microsoft365 import text_to_html

        sent = 0
        for req in requests:
            subject, body = self.review_email(req)
            await mail.send_mail([req["email"]], subject, text_to_html(body))
            self.db.set_kv(f"review_asked:{req['email'].lower()}", date.today().isoformat())
            sent += 1
        return f"Sent {sent} review request(s)."
