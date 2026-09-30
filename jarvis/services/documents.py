"""Paperwork: risk assessments & method statements (RAMS) per job, tender / pre-qualification questionnaire
answers drafted from the company's real evidence, HR documents (job postings, interview questions,
disciplinary/performance letters), and bid support - a go/no-go + pricing assessment grounded in real
capacity/cash/win-rate data, and a full narrative proposal document grounded in real evidence and comparable
past jobs, for tenders bigger than a plain PQQ answer_questionnaire response covers. Also display-only correspondence drafts:
credit-control chasers (reminder / call script / Letter Before Action) built from the accountant's real overdue data,
and sales follow-up sequences for open Salts FSM quotes. None of these are ever sent by these tools."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

from ..brain import llm

log = logging.getLogger(__name__)


async def _safe(coro, label: str) -> Any:
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 - one failed data source shouldn't sink the whole draft
        log.warning("bid_assessment input %s failed: %s", label, e)
        return {"error": f"{type(e).__name__}: {e}"[:200]}

RAMS_SYSTEM = """You are Jarvis, drafting a Risk Assessment and Method Statement (RAMS) for {company}, a UK fire &
security contractor. Use the job details and the knowledge extracts provided. Output markdown with:
1. Job details (site, client, scope, date, engineers, emergency contacts: TO CONFIRM).
2. Scope and sequence of works (method statement) specific to the system type and job type, including isolation
   and liaison with the responsible person / ARC before testing, cause & effect testing, reinstatement and handover.
3. Risk assessment table: hazard | who is at risk | controls | residual risk (L/M/H). Cover working at height,
   electrical isolation, asbestos (check the asbestos register before drilling), lone working, occupied premises
   (false alarms, vulnerable occupants in care homes/schools), manual handling, dust, hot works if any, driving.
4. PPE, tools and access equipment; permits required.
5. Competence required (e.g. FIA / ECS cards) and emergency arrangements.
6. Sign-off block for engineer, supervisor and client.
Be specific and practical. Mark anything you don't know as TO CONFIRM - never invent site facts."""

PQQ_SYSTEM = """You are Jarvis, answering a tender / pre-qualification questionnaire (PQQ, SQ, Constructionline,
CHAS/SafeContractor-style) for {company}. Using ONLY the evidence provided, answer each question in the first
person plural, concisely and persuasively, and cite the evidence (accreditation, policy, insurance, competency,
records). Output markdown: a table | Q | Answer | Evidence | Status | where Status is Ready, Needs document, or
TO CONFIRM. Then list the documents to attach and the gaps to close. Never invent certificate numbers, figures,
policies or dates - mark them TO CONFIRM."""


RECRUITMENT_SYSTEM = """You are Jarvis, drafting recruitment material for {company}, a UK fire & security
installer/maintainer, for a role {owner} needs to fill. Use the staff register (how similar roles here are
actually described and measured) for tone and realistic expectations, and current UK employment law (right
to work, minimum wage, discrimination in job ads) where relevant.

Output markdown with two clearly headed parts:
1. **Job posting** - a real, publishable advert: role, what the job actually involves day to day at a UK fire
   & security SME (not generic corporate filler), person specification (essential vs desirable - qualifications
   like FIA/ECS where relevant, experience, driving licence if needed), what we offer, how to apply.
2. **Interview questions** - 8-10 questions mixing technical/role competence and behavioural, each with what a
   strong answer actually looks like for this role, plus 2-3 legally-safe questions to probe reliability/
   punctuality/safety attitude without straying into protected characteristics.
Be specific to the role given, not generic. Mark anything you're guessing at (salary, exact requirements) as
TO CONFIRM rather than inventing figures."""

HR_LETTER_SYSTEM = """You are Jarvis, drafting an HR letter/document for {company}, a UK fire & security
employer, for {owner} to review before it's sent - never send this yourself. Ground it in current UK
employment law (ACAS code of practice on disciplinary/grievance procedures, statutory notice, right to be
accompanied) and the real facts given; never invent dates, incidents or figures - mark anything missing as
TO CONFIRM.

Match the letter type requested (e.g. invite to a disciplinary/investigation meeting, written warning
confirmation, performance improvement plan, reference letter, probation outcome) to the correct ACAS-compliant
structure and tone: factual, proportionate, never pre-judging an outcome that hasn't been decided at a
meeting yet. Output markdown: the letter/document itself, then a short "Before sending" checklist of what
{owner} should double-check or that a solicitor should review this if the situation could end in dismissal or
looks legally contentious."""


BID_ASSESSMENT_SYSTEM = """You are Jarvis, giving {owner} a candid go/no-go and pricing recommendation for a
tender opportunity at {company}, a UK fire & security installer/maintainer - using the real business data
provided (cash position, team capacity/utilisation, quote win rate, customer concentration), not guesswork.

Structure your answer:
1. **Recommendation**: Bid or don't bid - one clear line, then why.
2. **Capacity**: Can the team actually deliver this on top of current work, based on the utilisation/workload
   data given? Flag if it would mean turning away or delaying other work.
3. **Pricing**: A suggested price range and margin, reasoned from typical UK fire & security margins (label
   these as typical ranges, not certainties) and the value/scope given - not a single invented number
   presented as precise.
4. **Risk**: Customer concentration (would winning this make one client too large a share of revenue?),
   payment/cash timing, competition if named, anything in the business data that raises a flag.
5. **If bidding, what to emphasise** - 2-3 concrete points that would make this bid actually win, given our
   real track record and quote conversion rate.
Be direct - this is a decision aid, not a cheerleading exercise. Mark anything you don't have real data for
as an assumption, not a fact."""

BID_DOCUMENT_SYSTEM = """You are Jarvis, drafting a full tender/proposal document for {company}, a UK fire &
security installer/maintainer, responding to a real opportunity - not just answering a PQQ's individual
questions (that's a separate tool), but writing the actual submission document.

Use ONLY the real evidence given: accreditations, company documents, and comparable past jobs (as case
studies - reference real job types/systems/scale, never invented client names or figures). Output markdown:
1. Cover letter / introduction - who we are, why we're a strong fit for this specific opportunity.
2. Understanding of requirements - reflect the brief back to show we've actually read it.
3. Proposed approach / methodology - how we'd deliver this, referencing our real accreditations and
   competencies.
4. Relevant experience - 2-4 case studies drawn from the comparable past jobs given, described generically
   enough to respect client confidentiality (system type, scale, outcome) unless the job data itself names
   the client.
5. Pricing summary - a placeholder structure (labour/materials/ongoing maintenance) for {owner} to fill in
   with real figures, not invented numbers.
6. Compliance & accreditation summary, and next steps.
Mark any gap as TO CONFIRM. Never invent a client name, contract value, or accreditation we don't hold."""


CREDIT_CONTROL_SYSTEM = """You are Jarvis, drafting credit-control correspondence for {company}, a UK fire &
security contractor, for {owner} to review. This is a DRAFT ONLY: you never send it - sending is a separate,
approval-gated step ({owner} approves it via the email tool).

Use ONLY the figures, invoice numbers and dates in the JSON provided. Never invent or estimate an amount, date,
invoice ref, PO number, bank detail, contact name, company number or interest figure. Anything you need that is not
provided goes in as a clearly marked placeholder, e.g. [TO CONFIRM: customer contact name], and is repeated in a short
"Missing / to confirm" list at the end. Quote statutory interest and fixed-sum compensation (Late Payment of
Commercial Debts (Interest) Act 1998) ONLY when the invoice data supplies those figures; if they are not supplied,
do not mention them at all (for a Letter Before Action, say in the checklist that they should be added once
calculated). The interest figures are the accountant tool's estimate as at today - say "as at [date]" and that
interest continues to accrue; do not compute a daily rate yourself.

Match the requested channel and the escalation stage (`stage_key`):
- reminder (1-7 days overdue): warm, friendly, assume an oversight. Short. Ask for payment or a payment date.
- second_reminder (8-21 days): polite but firmer. Reference the earlier reminder only if the data says one was
  sent, otherwise use [TO CONFIRM: date of earlier reminder]. Ask for payment, or a firm payment date, by a
  specific short deadline expressed relative to the letter date (e.g. "within 7 days").
- final_notice (22-45 days): firm, clear and businesslike. State that this is a final reminder before further
  action, request payment within 7 days, and invite them to contact us now if there is a dispute.
- letter_before_action (46+ days): formal, factual, unemotional - no adjectives, no threats beyond stating the
  next step. See below.
Never threaten to withhold or pause life-safety or emergency call-outs. If the stage text mentions pausing
non-urgent work, treat that only as an option for {owner} to consider in the checklist, not something to
threaten in the correspondence.

Channels:
- email: subject line, then body. Sign off from {owner} at {company}.
- call: a phone script for the accounts-payable contact: opening, purpose, the specific invoice(s) and amount(s),
  what to ask (payment date, any query/dispute, correct contact/PO), how to respond to likely answers ("in the
  post", "not received the invoice", "dispute"), what to agree and record, and a note-to-file template. Polite
  and factual, never aggressive.
- letter: a formal letter with sender/recipient placeholders, date placeholder, subject "Re: invoice(s) ...",
  and sign-off.

Letter Before Action (business-to-business): follow the expectations of the Practice Direction - Pre-Action
Conduct and Protocols (PD-PAC): (1) a concise summary of the claim (who owes what, for what work/invoice, when it
fell due); (2) what we want: payment of the sum(s) stated, plus statutory interest and fixed compensation where
the data supplies them; (3) a clear deadline of {deadline} days from the date of the letter (leave the calendar date
as a placeholder until the send date is fixed); (4) how to pay [TO CONFIRM: bank details]; (5) that if the debt is not
paid or a reasoned response given by the deadline, we intend to start court proceedings without further notice,
which may add court fees and costs; (6) invite the recipient to say if they dispute any part and why, and to send
any documents relied on; (7) mention we are willing to consider alternative dispute resolution or a payment
proposal. Note: the Pre-Action Protocol for Debt Claims applies where the debtor is an individual (including a sole
trader), not a limited company - if the customer might be a sole trader or individual, say so in the checklist,
because the Protocol then requires more (information sheet, reply form, longer response period) and a solicitor
must prepare it. Statutory interest/compensation only applies between businesses.

Output markdown: the draft itself, then a short "Before sending" checklist. For a Letter Before Action the
checklist MUST start with: "Have a solicitor (or {owner}'s qualified accountant) review this letter before it goes
out." and must also cover: confirm the customer is a business (and not a sole trader/individual); confirm the
work was done and invoiced correctly and there is no open dispute; confirm earlier reminders were sent and their
dates; confirm the interest/compensation figures and the base rate used; confirm the recipient and address; and
that no life-safety service is being withheld. Finally list "Missing / to confirm" items, including everything in
the `missing` array."""

SALES_FOLLOWUP_SYSTEM = """You are Jarvis, drafting a short, professional, non-pushy follow-up sequence for an open quote
at {company}, a UK fire & security installer/maintainer, for {owner} to review. This is a DRAFT ONLY: you never send
anything - sending is a separate, approval-gated step ({owner} approves it via the email tool).

Use ONLY the quote data in the JSON provided (quote ref, customer, site, value, date sent, days since sent, scope/
title). Never invent prices, discounts, dates, deadlines, stock levels, competitor activity, contact names or
scope details. Anything missing becomes a marked placeholder, e.g. [TO CONFIRM: contact name], and is repeated in a
short "Missing / to confirm" list at the end (including everything in the `missing` array).

Write one touch per entry in `touches` (in order), for the requested channel:
- Each touch names the specific quote (ref, site, scope, value as given) and is brief - an email of 4-8 lines, or
  a phone script of an opening, 2-3 talking points and a light close with voicemail wording.
- Day 7: a friendly check-in - did the quote arrive, is there anything unclear.
- Day 14: offer practical help - a site visit, walking through the scope, clarifying what's included, or adjusting
  options/phasing if the budget or scope needs to change (only offer, never promise a discount).
- Day 21: a polite close-out - say we'll assume the timing isn't right for now, that the quote stays on file, and
  that they are welcome to come back or ask for it to be refreshed; no guilt, no ultimatum.
No pressure tactics, no invented urgency or scarcity, no "last chance", no threats of price rises unless the data
says so. Where a touch's `status` is `already_passed`, still draft it but label it "only if not already sent".
Sign off from {owner} at {company}. If the quote's scope relates to life-safety systems you may say we are happy to
answer compliance questions, but do not scare or exaggerate.

Output markdown: a heading per touch (day and channel), the draft, then a short "Missing / to confirm" list."""

CC_CHANNELS = ("email", "call", "letter")
CC_DEFAULT_CHANNEL = {"reminder": "email", "second_reminder": "call", "final_notice": "email",
                      "letter_before_action": "letter"}
LBA_DEADLINE_DAYS = 14
SALES_CHANNELS = ("email", "call")
SALES_TOUCH_DAYS = (7, 14, 21)


def _stage_key(days_overdue: int) -> str:
    """Mirrors accountant.credit_control_stage's bands as a stable key."""
    if days_overdue <= 0:
        return "not_due"
    if days_overdue <= 7:
        return "reminder"
    if days_overdue <= 21:
        return "second_reminder"
    if days_overdue <= 45:
        return "final_notice"
    return "letter_before_action"


def _match_overdue(actions: list[dict[str, Any]], query: str) -> tuple[list[dict[str, Any]], str | None]:
    """Find credit-control actions by invoice ref (exact), else customer name (exact, then partial)."""
    q = (query or "").strip().lower()
    if not q:
        return [], "Tell me a customer name or an invoice reference."
    by_ref = [a for a in actions if str(a.get("invoice") or "").lower() == q]
    if by_ref:
        return by_ref, None
    exact = [a for a in actions if str(a.get("customer") or "").lower() == q]
    if exact:
        return exact, None
    partial = [a for a in actions if q in str(a.get("customer") or "").lower()]
    names = sorted({str(a.get("customer")) for a in partial})
    if len(names) > 1:
        return [], f"'{query}' matches several customers ({', '.join(names)}) - which one do you mean?"
    return partial, None


def build_credit_control_context(cc: dict[str, Any], aged: dict[str, Any] | None, query: str,
                                 channel: str | None, today: date, base_rate_pct: float | None = None
                                 ) -> tuple[dict[str, Any] | None, str | None]:
    """Turn accountant.credit_control() (+ aged detail) into the exact facts a chaser may use.

    Returns (context, None), or (None, message) when nothing suitable can be drafted."""
    if channel is not None:
        channel = channel.strip().lower()
        if channel not in CC_CHANNELS:
            return None, f"Channel must be one of: {', '.join(CC_CHANNELS)}."
    matched, err = _match_overdue(cc.get("actions") or [], query)
    if err:
        return None, err
    if not matched:
        return None, (f"I couldn't find an overdue invoice or customer matching '{query}' in credit control - "
                      "it may be paid, not yet due, or the name/reference is different. Nothing to draft.")
    details = {str(i.get("number")): i for i in ((aged or {}).get("overdue_invoices") or [])}
    invoices, missing = [], []
    for a in sorted(matched, key=lambda x: -(x.get("days_overdue") or 0)):
        ref = str(a.get("invoice"))
        d = details.get(ref, {})
        row = {"invoice": ref, "customer": a.get("customer"), "amount_due": a.get("amount_due"),
               "days_overdue": a.get("days_overdue"), "recommended_step": a.get("action"),
               "invoice_date": d.get("date"), "due_date": d.get("due_date"), "invoice_total": d.get("total")}
        for key in ("statutory_interest", "fixed_compensation"):
            if a.get(key) is not None:
                row[key] = a[key]
        for label, key in (("invoice date", "invoice_date"), ("due date", "due_date")):
            if not row.get(key):
                missing.append(f"{label} for {ref}")
        if a.get("amount_due") in (None, 0) or a.get("days_overdue") in (None,):
            missing.append(f"amount or days overdue for {ref}")
        invoices.append(row)
    worst = max((r.get("days_overdue") or 0) for r in invoices)
    stage = _stage_key(worst)
    chosen = channel or CC_DEFAULT_CHANNEL.get(stage, "email")
    notes = []
    if chosen == "call" and stage == "letter_before_action":
        notes.append("A Letter Before Action must be in writing - this call script is a courtesy warning only.")
    if chosen == "letter" and stage in ("reminder", "second_reminder"):
        notes.append("A formal letter is heavier than this stage normally warrants; consider an email or call first.")
    with_interest = [r for r in invoices if "statutory_interest" in r]
    totals: dict[str, Any] = {"amount_due": round(sum(float(r.get("amount_due") or 0) for r in invoices), 2)}
    if with_interest and len(with_interest) == len(invoices):
        totals["statutory_interest"] = round(sum(r["statutory_interest"] for r in with_interest), 2)
        totals["fixed_compensation"] = round(sum(r["fixed_compensation"] for r in with_interest), 2)
    elif with_interest:
        missing.append("statutory interest/compensation is only supplied for some invoices - quote it only for those")
    if stage == "letter_before_action" and not with_interest:
        missing.append("statutory interest/compensation figures (not supplied)")
    missing += ["customer contact name, email and postal address", "date(s) of any earlier reminders sent",
                "our bank/payment details", "confirmation the customer is a limited company/business "
                "(statutory interest only applies business-to-business)"]
    ctx = {"today": today.isoformat(), "query": query, "customer": invoices[0]["customer"],
           "stage_key": stage, "stage_text": invoices[0]["recommended_step"] if len(invoices) == 1 else
           f"most overdue invoice is {worst} days overdue", "channel": chosen,
           "channel_defaulted": channel is None, "channel_notes": notes, "invoices": invoices, "totals": totals,
           "lba_deadline_days": LBA_DEADLINE_DAYS if stage == "letter_before_action" else None,
           "interest_basis": (f"8% over Bank of England base rate (Jarvis configured base rate {base_rate_pct}%), "
                              "as estimated by the accountant tool - to be confirmed"
                              if with_interest and base_rate_pct is not None else None),
           "missing": missing}
    return ctx, None


def build_followup_context(quotes: list[dict[str, Any]], quote_ref: str, channel: str | None,
                           today: date) -> tuple[dict[str, Any] | None, str | None]:
    """Facts for a sales follow-up from an FSM quote; (None, message) if it can't/shouldn't be drafted."""
    from .remedials import LOST, WON

    if channel is not None:
        channel = channel.strip().lower()
        if channel not in SALES_CHANNELS:
            return None, f"Channel must be one of: {', '.join(SALES_CHANNELS)}."
    ref = (quote_ref or "").strip().lower()
    quote = next((q for q in quotes if ref and str(q.get("id") or "").lower() == ref), None)
    if not quote:
        return None, f"I couldn't find quote '{quote_ref}' in Salts FSM - check the reference and I'll draft the follow-up."
    status = str(quote.get("status") or "").lower()
    if status in WON or status in LOST:
        return None, (f"Quote {quote.get('id')} is already '{status}', so there's nothing to chase. "
                      "No follow-up drafted.")
    days_since = None
    sent = str(quote.get("sent_date") or "")[:10]
    try:
        days_since = (today - date.fromisoformat(sent)).days
    except ValueError:
        sent = ""
    missing = []
    for label, key in (("scope/title", "title"), ("customer", "customer"), ("site", "site"),
                       ("value", "value")):
        if quote.get(key) in (None, ""):
            missing.append(label)
    if not sent:
        missing.append("date the quote was sent (touch timing can't be worked out)")
    missing.append("customer contact name and email/phone")
    touches = []
    for d in SALES_TOUCH_DAYS:
        if days_since is None:
            state = "unknown"
        elif days_since > d + 3:
            state = "already_passed"
        elif days_since >= d:
            state = "due_now"
        else:
            state = "upcoming"
        touches.append({"day": d, "status": state,
                        "purpose": {7: "friendly check-in", 14: "offer help", 21: "polite close-out"}[d]})
    ctx = {"today": today.isoformat(), "channel": channel or "email", "channel_defaulted": channel is None,
           "quote": {"ref": quote.get("id"), "customer": quote.get("customer"), "site": quote.get("site"),
                     "value": quote.get("value"), "date_sent": sent or None, "days_since_sent": days_since,
                     "scope": quote.get("title"), "status": quote.get("status"),
                     "prepared_by": quote.get("created_by")},
           "touches": touches, "missing": missing}
    return ctx, None


class Documents:
    def __init__(self, j):
        self.j = j

    async def _find_job(self, job_ref: str) -> dict[str, Any] | None:
        today = date.today()
        for jb in await self.j.fsm.jobs(today - timedelta(days=60), today + timedelta(days=60)):
            if str(jb.get("ref") or jb.get("id")).lower() == job_ref.lower():
                return jb
        return None

    async def rams(self, job_ref: str | None = None, description: str | None = None) -> str:
        j = self.j
        job = await self._find_job(job_ref) if job_ref else None
        if job_ref and not job and not description:
            return f"I couldn't find job {job_ref} in Salts FSM - describe the work and I'll draft the RAMS."
        systems = []
        if job:
            systems = [s for s in await j.fsm.systems() if s.get("site") == job.get("site")]
        topic = " ".join(filter(None, [str((job or {}).get("type") or ""), description or "",
                                       " ".join(str(s.get("type")) for s in systems)]))
        knowledge = j.kb.search(topic + " working at height asbestos testing isolation", limit=6)
        text = await llm.write(
            j.client, j.settings, system=RAMS_SYSTEM.format(company=j.settings.company_name),
            prompt=json.dumps({"job": job, "description": description, "systems_on_site": systems,
                               "knowledge_extracts": knowledge}, default=str)[:60000],
            effort="medium", max_tokens=12000)
        j.bus.publish("display", {"title": f"RAMS - {(job or {}).get('ref') or 'draft'}", "markdown": text})
        return text

    async def questionnaire(self, questions: str, buyer: str | None = None) -> str:
        j = self.j
        evidence = {}
        for scheme in ("BAFE", "SSAIB", "CHAS"):
            try:
                evidence[scheme] = await j.accreditations.gather_evidence(scheme)
            except Exception as e:  # noqa: BLE001
                evidence[scheme] = {"error": str(e)[:200]}
        company = j.kb.core_documents()[:30000]
        text = await llm.write(
            j.client, j.settings, system=PQQ_SYSTEM.format(company=j.settings.company_name),
            prompt=(f"Buyer: {buyer or 'not stated'}\n\n<questions>\n{questions[:40000]}\n</questions>\n\n"
                    f"<company_documents>\n{company}\n</company_documents>\n\n"
                    f"<evidence>\n{json.dumps(evidence, default=str)[:60000]}\n</evidence>"),
            effort="high", max_tokens=16000)
        j.bus.publish("display", {"title": f"Questionnaire answers{' - ' + buyer if buyer else ''}", "markdown": text})
        return text

    async def recruitment(self, role: str, notes: str | None = None) -> str:
        j = self.j
        text = await llm.write(
            j.client, j.settings, system=RECRUITMENT_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name),
            prompt=json.dumps({"role": role, "notes": notes, "staff_register": j.register.prompt_summary()},
                              default=str)[:40000],
            effort="medium", max_tokens=8000)
        j.bus.publish("display", {"title": f"Recruitment - {role}", "markdown": text})
        return text

    async def hr_letter(self, kind: str, person: str, details: str) -> str:
        j = self.j
        knowledge = j.kb.search(f"{kind} disciplinary employment law ACAS", limit=4)
        text = await llm.write(
            j.client, j.settings, system=HR_LETTER_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name),
            prompt=json.dumps({"letter_type": kind, "person": person, "details": details,
                               "knowledge_extracts": knowledge}, default=str)[:40000],
            effort="medium", max_tokens=8000)
        j.bus.publish("display", {"title": f"HR - {kind} ({person})", "markdown": text})
        return text

    async def credit_control_draft(self, target: str, channel: str | None = None) -> str:
        """Draft a reminder email / call script / Letter Before Action for an overdue invoice or customer.
        Display only - never sent (sending goes through the approval-gated email_send tool)."""
        j = self.j
        cc = await _safe(j.accountant.credit_control(), "credit control")
        if "error" in cc:
            return f"I couldn't read the credit-control data just now ({cc['error']}), so I haven't drafted anything."
        aged = await _safe(j.accountant.aged("receivable"), "aged debtors")
        if "error" in aged:
            aged = None  # dates then show up as missing rather than being guessed
        ctx, problem = build_credit_control_context(cc, aged, target, channel, date.today(),
                                                    getattr(j.settings, "boe_base_rate", None))
        if problem:
            return problem
        text = await llm.write(
            j.client, j.settings,
            system=CREDIT_CONTROL_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name,
                                                deadline=LBA_DEADLINE_DAYS),
            prompt=json.dumps(ctx, default=str)[:40000], effort="medium", max_tokens=8000)
        j.bus.publish("display", {"title": f"Credit control ({ctx['channel']}) - {ctx['customer']}", "markdown": text})
        return text

    async def sales_followup(self, quote_ref: str, channel: str | None = None) -> str:
        """Draft a day 7 / 14 / 21 follow-up sequence for an open Salts FSM quote. Display only - never sent."""
        j = self.j
        try:
            quotes = await j.fsm.quotes()
        except Exception as e:  # noqa: BLE001
            return f"I couldn't read quotes from Salts FSM just now ({type(e).__name__}), so I haven't drafted anything."
        ctx, problem = build_followup_context(quotes, quote_ref, channel, date.today())
        if problem:
            return problem
        text = await llm.write(
            j.client, j.settings,
            system=SALES_FOLLOWUP_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name),
            prompt=json.dumps(ctx, default=str)[:30000], effort="medium", max_tokens=6000)
        j.bus.publish("display", {"title": f"Quote follow-up ({ctx['channel']}) - {ctx['quote']['ref']}",
                                  "markdown": text})
        return text

    async def bid_assessment(self, opportunity: str, value: float | None, notes: str | None = None) -> str:
        j = self.j
        business = await _safe(j.advisor.gather(), "business data")
        text = await llm.write(
            j.client, j.settings, system=BID_ASSESSMENT_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name),
            prompt=json.dumps({"opportunity": opportunity, "estimated_value": value, "notes": notes,
                               "business_data": business}, default=str)[:60000],
            effort="high", max_tokens=8000)
        j.bus.publish("display", {"title": f"Bid assessment - {opportunity}", "markdown": text})
        return text

    async def bid_document(self, opportunity: str, client: str | None, requirements: str,
                           notes: str | None = None) -> str:
        j = self.j
        evidence = {}
        for scheme in ("BAFE", "SSAIB", "CHAS"):
            try:
                evidence[scheme] = await j.accreditations.gather_evidence(scheme)
            except Exception as e:  # noqa: BLE001
                evidence[scheme] = {"error": str(e)[:200]}
        today = date.today()
        comparable_jobs = []
        try:
            jobs = await j.fsm.jobs(today - timedelta(days=730), today, status="completed")
            comparable_jobs = jobs[:15]  # a sample - the model picks what's actually relevant as case studies
        except Exception as e:  # noqa: BLE001
            comparable_jobs = [{"error": str(e)[:200]}]
        company = j.kb.core_documents()[:30000]
        text = await llm.write(
            j.client, j.settings, system=BID_DOCUMENT_SYSTEM.format(company=j.settings.company_name, owner=j.settings.owner_name),
            prompt=json.dumps({"opportunity": opportunity, "client": client, "requirements": requirements,
                               "notes": notes, "company_documents": company, "evidence": evidence,
                               "comparable_past_jobs": comparable_jobs}, default=str)[:70000],
            effort="high", max_tokens=16000)
        j.bus.publish("display", {"title": f"Bid document - {opportunity}", "markdown": text})
        return text
