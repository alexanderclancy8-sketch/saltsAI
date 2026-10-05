"""Customer communication drafting for job lifecycle events.

Looks at Salts FSM for the moments a customer would expect to hear from us - engineer booked / on the way, job
complete (with a summary), certificate ready, service coming due, and a quote that's gone quiet - and writes a short
email for each. Every draft is queued as an ``email_send`` action, i.e. it waits for the owner's approval on the
display exactly like any other outbound email. Nothing here ever sends mail, approves an action or skips the queue.

Each event is only drafted once (a marker is stored in the key-value table when the draft is queued), so running
the sweep repeatedly - on a schedule or by asking - never queues duplicates.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

EVENTS = ("booked", "on_the_way", "complete", "certificate", "service_due", "quote_followup")

BOOKED_STATUSES = {"scheduled", "booked", "allocated", "assigned"}
ON_THE_WAY_STATUSES = {"en_route", "en route", "on_the_way", "on the way", "travelling", "traveling", "dispatched"}
DONE_STATUSES = {"completed", "complete", "done", "closed", "finished"}
CERT_KEYS = ("certificateUrl", "certificate_url", "certificateRef", "certificate_ref", "certificate",
             "certificateReady", "certificate_ready")
EMAIL_KEYS = ("contactEmail", "contact_email", "customerEmail", "customer_email")

BOOKED_LOOKAHEAD_DAYS = 2  # tell the customer about visits in the next couple of days
RECENT_DAYS = 3  # "complete" drafts only for jobs finished in the last few days
CERT_RECENT_DAYS = 14
QUOTE_MAX_AGE_DAYS = 60  # older than this is a different conversation, not a gentle follow-up


def _day(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "")[:19]).date()
    except ValueError:
        return None


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _first_name(name: Any) -> str:
    return str(name or "").split()[0] if str(name or "").strip() else "Our engineer"


def _status(row: dict[str, Any]) -> str:
    return _norm(row.get("status"))


def _extra_email(row: dict[str, Any]) -> str | None:
    extra = row.get("extra") or {}
    for key in EMAIL_KEYS:
        value = extra.get(key) or row.get(key)
        if value and "@" in str(value):
            return str(value).strip()
    return None


class CustomerComms:
    def __init__(self, j):
        self.j = j

    # ------------------------------------------------------------------ helpers
    def _sign_off(self) -> str:
        s = self.j.settings
        return f"Kind regards,\n{s.owner_name}\n{s.company_name}"

    def _contact_lookup(self, contracts: list[dict[str, Any]]):
        """Best known (email, contact name) for a customer: the contract for that site, else any for the customer."""
        def find(customer: Any, site: Any = None, contract_id: Any = None) -> tuple[str | None, str | None]:
            with_email = [c for c in contracts if c.get("contact_email")]
            for c in with_email:
                if contract_id and _norm(c.get("id")) == _norm(contract_id):
                    return c["contact_email"], c.get("contact_name")
            same = [c for c in with_email if _norm(c.get("customer")) == _norm(customer)]
            for c in same:
                if site and _norm(c.get("site")) == _norm(site):
                    return c["contact_email"], c.get("contact_name")
            if same:
                return same[0]["contact_email"], same[0].get("contact_name")
            return None, None
        return find

    def _greeting(self, name: str | None) -> str:
        return f"Hello {name}," if name and name.lower() != "facilities manager" else "Hello,"

    def _queue(self, result: dict[str, Any], event: str, marker: str, customer: Any, to: str | None,
               subject: str, body: str, ref: Any) -> None:
        if self.j.db.get_kv(marker):
            return
        if not to:
            result["skipped"].append({"event": event, "ref": ref, "customer": customer,
                                      "reason": "no customer email address known in Salts FSM"})
            return
        action_id = self.j.actions.queue(
            "email_send", f"Customer email ({event.replace('_', ' ')}) to {customer}: {subject}"[:200],
            {"to": [to], "cc": [], "subject": subject, "body": body})
        self.j.db.set_kv(marker, f"queued:{action_id}")
        result["queued"].append({"event": event, "ref": ref, "customer": customer, "to": to, "subject": subject,
                                 "action_id": action_id})

    # ------------------------------------------------------------------ main entry
    async def draft_all(self, events: list[str] | None = None, today: date | None = None) -> dict[str, Any]:
        """Queue a draft email (for approval) for every lifecycle event that needs one and hasn't had one yet."""
        j = self.j
        today = today or date.today()
        wanted = [e for e in (events or EVENTS) if e in EVENTS]
        unknown = [e for e in (events or []) if e not in EVENTS]
        result: dict[str, Any] = {"queued": [], "skipped": [],
                                  "note": "Drafts are queued for approval on the display - nothing has been sent."}
        if unknown:
            result["unknown_events"] = unknown
        if not wanted:
            result["note"] = f"No known events requested. Choose from: {', '.join(EVENTS)}."
            return result

        contracts = await j.fsm.contracts()
        find = self._contact_lookup(contracts)
        if {"booked", "on_the_way", "complete", "certificate"} & set(wanted):
            jobs = await j.fsm.jobs(today - timedelta(days=CERT_RECENT_DAYS), today + timedelta(days=BOOKED_LOOKAHEAD_DAYS))
            for job in jobs:
                await self._job_events(result, job, wanted, today, find)
        if "service_due" in wanted:
            await self._service_due(result, today, find)
        if "quote_followup" in wanted:
            await self._quote_followups(result, today, find)

        if result["queued"]:
            j.bus.publish("display", {"title": "Customer emails drafted", "markdown": "\n".join(
                f"- **{q['event'].replace('_', ' ')}** - {q['customer']} ({q['to']}): {q['subject']}"
                for q in result["queued"]) + "\n\nApprove or cancel each one in the approvals queue."})
        return result

    async def draft_quote_followup(self, quote_id: str, today: date | None = None) -> dict[str, Any]:
        """Queue the chase email for ONE sent quote (the suggestions' Prepare button, services/fsm_suggestions.py).

        The same draft, marker and approval as ``draft_all(["quote_followup"])`` - so the scheduled sweep and a Prepare press
        can never both draft the same quote - but for a single quote, whatever its age (the suggestion has its own threshold).
        Like everything here it only QUEUES an ``email_send`` for a human to approve; it never sends. ``already`` is set (to
        the marker, ``queued:<action id>``) when this quote was drafted before, and nothing new is queued."""
        today = today or date.today()
        result: dict[str, Any] = {"queued": [], "skipped": [],
                                  "note": "Draft queued for approval - nothing has been sent."}
        prior = self.j.db.get_kv(f"comms:quote_followup:{quote_id}")
        if prior:
            result["already"] = prior
            return result
        find = self._contact_lookup(await self.j.fsm.contracts())
        await self._quote_followups(result, today, find, only=str(quote_id))
        return result

    # ------------------------------------------------------------------ job events
    async def _job_events(self, result: dict[str, Any], job: dict[str, Any], wanted: list[str], today: date, find) -> None:
        j = self.j
        s = j.settings
        status = _status(job)
        ref = job.get("ref") or job.get("id")
        if not ref:
            return
        customer, site = job.get("customer"), job.get("site")
        email, contact = find(customer, site)
        email = _extra_email(job) or email
        hello = self._greeting(contact)
        sched = _day(job.get("scheduled_start"))
        engineer = _first_name(job.get("engineer"))
        job_type = str(job.get("type") or "visit").replace("_", " ")

        if "booked" in wanted and status in BOOKED_STATUSES and sched and today <= sched <= today + timedelta(days=BOOKED_LOOKAHEAD_DAYS):
            when = sched.strftime("%A %d %B")
            who = f"{engineer} will be" if job.get("engineer") else "One of our engineers will be"
            self._queue(result, "booked", f"comms:booked:{ref}:{sched.isoformat()}", customer, email,
                        f"Your {s.company_name} visit - {when}",
                        f"{hello}\n\nThis is to confirm that we have your {job_type} booked in at {site} on {when}. "
                        f"{who} attending. We'll let you know when they're on their way.\n\n"
                        f"If the date or access arrangements need to change, just reply to this email.\n\n"
                        f"{self._sign_off()}", ref)

        if "on_the_way" in wanted and status in ON_THE_WAY_STATUSES:
            self._queue(result, "on_the_way", f"comms:on_the_way:{ref}:{(sched or today).isoformat()}", customer, email,
                        f"{engineer} from {s.company_name} is on the way",
                        f"{hello}\n\n{engineer} is on the way to {site} for your {job_type} and should be with you "
                        f"shortly.\n\nIf there's any change to site access, please reply to this email or call us.\n\n"
                        f"{self._sign_off()}", ref)

        if status not in DONE_STATUSES:
            return
        done = _day(job.get("completed_at")) or sched
        if ("complete" in wanted and done and today - timedelta(days=RECENT_DAYS) <= done <= today
                and not j.db.get_kv(f"comms:complete:{ref}")):
            summary = await self._job_summary(job)
            self._queue(result, "complete", f"comms:complete:{ref}", customer, email,
                        f"Work completed at {site}",
                        f"{hello}\n\nThank you - {engineer} has completed your {job_type} at {site}"
                        f"{' on ' + done.strftime('%d %B') if done else ''}.\n\n"
                        f"{summary}\n\nIf you have any questions about the work, just reply to this email.\n\n"
                        f"{self._sign_off()}", ref)

        cert = next(((k, (job.get("extra") or {}).get(k)) for k in CERT_KEYS if (job.get("extra") or {}).get(k)), None)
        if "certificate" in wanted and cert and done and today - timedelta(days=CERT_RECENT_DAYS) <= done <= today:
            value = cert[1]
            link = (f"You can view it here: {value}\n\n" if str(value).lower().startswith("http")
                    else f"Certificate reference: {value}\n\n" if not isinstance(value, bool) else "")
            self._queue(result, "certificate", f"comms:certificate:{ref}", customer, email,
                        f"Your certificate for {site} is ready",
                        f"{hello}\n\nThe certificate for the {job_type} we carried out at {site} is now ready. "
                        f"{link}Please keep it with your fire safety / security records. If you'd like a copy "
                        f"sent in a different format, let us know.\n\n{self._sign_off()}", ref)

    async def _job_summary(self, job: dict[str, Any]) -> str:
        """Plain summary of the visit. Uses the engineer's last note when Salts FSM has one."""
        note = ""
        try:
            detail = await self.j.fsm.job_detail(str(job.get("id") or job.get("ref")))
            notes = (detail.get("extra") or {}).get("notes") or []
            if notes:
                last = notes[-1]
                note = str(last.get("text") if isinstance(last, dict) else last).strip()
        except Exception:  # noqa: BLE001 - the summary is a nicety; never lose the draft over it
            log.info("No job detail for %s", job.get("ref"), exc_info=True)
        if note:
            return f"Summary of the visit: {note}"
        return "Summary of the visit: the work was completed as booked."

    # ------------------------------------------------------------------ service due
    async def _service_due(self, result: dict[str, Any], today: date, find) -> None:
        j = self.j
        window = j.settings.customer_comms_service_notice_days
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for sy in await j.fsm.systems():
            due = _day(sy.get("next_service_due"))
            if not due or not (today - timedelta(days=30) <= due <= today + timedelta(days=window)):
                continue
            groups.setdefault((str(sy.get("customer") or ""), str(sy.get("site") or "")), []).append({**sy, "_due": due})
        for (customer, site), systems in groups.items():
            earliest = min(sy["_due"] for sy in systems)
            email, contact = find(customer, site, systems[0].get("contract_id"))
            lines = "\n".join(f"- {str(sy.get('type') or 'system').replace('_', ' ')}"
                              f"{' (' + str(sy['make_model']) + ')' if sy.get('make_model') else ''} - due {sy['_due']:%d %B %Y}"
                              for sy in sorted(systems, key=lambda x: x["_due"]))
            self._queue(result, "service_due", f"comms:service_due:{_norm(customer)}|{_norm(site)}:{earliest.isoformat()}",
                        customer, email, f"Service due at {site}",
                        f"{self._greeting(contact)}\n\nThe next scheduled service at {site} is coming up:\n\n{lines}\n\n"
                        "Regular servicing keeps your systems compliant and reliable. Please reply to this email with a "
                        f"day that suits and we'll get it booked in.\n\n{self._sign_off()}", f"{customer} / {site}")

    # ------------------------------------------------------------------ quote follow-up
    async def _quote_followups(self, result: dict[str, Any], today: date, find, only: str | None = None) -> None:
        j = self.j
        min_age = 0 if only is not None else j.settings.customer_comms_quote_followup_days
        for q in await j.fsm.quotes("sent"):
            if _status(q) != "sent" or not q.get("id"):
                continue
            if only is not None and str(q["id"]) != only:
                continue
            sent = _day(q.get("sent_date"))
            if not sent or not (min_age <= (today - sent).days <= QUOTE_MAX_AGE_DAYS):
                continue
            email, contact = find(q.get("customer"), q.get("site"))
            email = _extra_email(q) or email
            value = f" (£{float(q['value']):,.2f} + VAT)" if q.get("value") else ""
            title = q.get("title") or "your quotation"
            self._queue(result, "quote_followup", f"comms:quote_followup:{q['id']}", q.get("customer"), email,
                        f"Following up on quote {q['id']}",
                        f"{self._greeting(contact)}\n\nI just wanted to follow up on our quote {q['id']} for {title}"
                        f"{value}, sent on {sent:%d %B}. Have you had a chance to look it over?\n\n"
                        "If you have any questions, would like anything changed, or are ready to go ahead, just reply to "
                        f"this email and we'll take it from there.\n\n{self._sign_off()}", q["id"])
