"""Jarvis' tools. Each tool has a pydantic input model (used both for the JSON schema
sent to Claude and to validate what comes back) and an async handler."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field

MAX_RESULT_CHARS = 60_000


@dataclass
class Tool:
    name: str
    description: str
    model: type[BaseModel]
    handler: Callable[[Any, Any], Awaitable[Any]]
    label: str  # shown on the display while it runs
    approval: bool = False  # changes something -> queued as a suggestion for the owner to approve
    describe: Callable[[Any], str] | None = None  # plain-English summary for the approval card

    def definition(self) -> dict[str, Any]:
        schema = self.model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": schema,
                "eager_input_streaming": True}


async def dispatch(j, tool: Tool, args: BaseModel) -> Any:
    """Run a read-only tool now; anything that changes something is queued for the owner's approval."""
    if tool.approval:
        summary = tool.describe(args) if tool.describe else f"{tool.label}: {args.model_dump_json()}"
        action_id = j.actions.queue(f"tool:{tool.name}", summary, {"tool": tool.name, "args": args.model_dump()})
        return (f"Suggested, not done: queued as action #{action_id} ('{summary}'). It will only happen when "
                f"{j.settings.owner_name} approves it on the display.")
    return await tool.handler(j, args)


def serialise(result: Any) -> str:
    text = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "…[truncated]"


def _d(value: str | None, default: date) -> date:
    return date.fromisoformat(value) if value else default


# --------------------------------------------------------------------------- inputs
class NoInput(BaseModel):
    pass


class InboxIn(BaseModel):
    unread_only: bool = True
    limit: int = Field(10, description="Max emails, up to 25")
    since_hours: int | None = Field(None, description="Only emails received in the last N hours")


class SearchIn(BaseModel):
    query: str
    limit: int = 10


class MessageIn(BaseModel):
    message_id: str


class DraftIn(BaseModel):
    message_id: str
    reply: str = Field(description="Reply text to put in an Outlook draft (not sent)")


class SendIn(BaseModel):
    to: list[str]
    subject: str
    body: str = Field(description="Plain text body")
    cc: list[str] = []


class OwnerUpdateIn(BaseModel):
    subject: str
    message: str
    channels: list[Literal["teams", "email"]] = ["teams", "email"]


class DisplayIn(BaseModel):
    title: str
    markdown: str = Field(description="Markdown content: tables, lists, drafts, figures")


class JobsIn(BaseModel):
    date_from: str | None = Field(None, description="YYYY-MM-DD, default today")
    date_to: str | None = Field(None, description="YYYY-MM-DD, default same as date_from")
    status: str | None = None
    engineer: str | None = None


class FsmQueryIn(BaseModel):
    path: str = Field(description="API path relative to the FSM API prefix, e.g. /jobs/123 or /customers")
    params: dict[str, str] = {}


class DaysAheadIn(BaseModel):
    days_ahead: int = 30


class QuotesIn(BaseModel):
    status: str | None = Field(None, description="e.g. sent, accepted, declined")


class SourceSearchIn(BaseModel):
    query: str = Field(description="Code search terms, e.g. a class, route or error message")


class SourceReadIn(BaseModel):
    path: str


class ProductivityIn(BaseModel):
    days: int = Field(30, description="Length of the period in days (7 = last week, 30 = last month)")
    engineer: str | None = Field(None, description="Limit to one engineer (partial name ok)")


class PersonIn(BaseModel):
    name: str | None = Field(None, description="Staff member (partial name ok); omit for everyone")


class ReviewIn(BaseModel):
    days: int = Field(7, description="Review period in days")
    name: str | None = Field(None, description="One person (partial name ok); omit for the whole team")


class RoleUpdateIn(BaseModel):
    name: str = Field(description="Full name of the staff member")
    role: str | None = None
    type: Literal["engineer", "office"] | None = None
    email: str | None = None
    add_duties: list[str] = []
    remove_duties: list[str] = Field([], description="Duties to remove (matched by text)")
    expectations: dict[str, float] = Field({}, description="Targets, e.g. {'jobs_per_day': 3, 'quotes_per_week': 12, "
                                                         "'utilisation_pct': 75, 'revisit_rate_pct_max': 8}")


class OfficeIn(BaseModel):
    days: int = 30


class FsmChangeIn(BaseModel):
    method: Literal["POST", "PUT", "PATCH"]
    path: str = Field(description="FSM API path, e.g. /jobs/123")
    body: dict[str, Any] = {}
    summary: str = Field(description="Plain-English description of the change for the owner to approve")


class LogJobIn(BaseModel):
    site: str = Field(description="Salts FSM site name, or the site/customer address if it's not in FSM yet")
    type: Literal["service", "callout", "remedial", "install", "commissioning", "survey"] = "callout"
    description: str = Field(description="What's wrong or what's needed, e.g. 'Intruder alarm fault - zone 3 tamper'")
    priority: str = Field("", description="SLA if known, e.g. '4h', '24h', 'PPM' - leave blank if not given")
    engineer: str = Field("", description="Engineer to assign - leave blank to book it unassigned")
    scheduled_start: str = Field("", description="When to book it for (ISO date or date+time) - blank = unscheduled")
    customer: str = Field("", description="Customer name, only if different from the site name")


class HealthIn(BaseModel):
    days: int = Field(90, description="Period to assess, in days")


class SocialIn(BaseModel):
    days: int = 30


class UrlIn(BaseModel):
    url: str | None = Field(None, description="Page to audit; defaults to the company website")


class AdviceIn(BaseModel):
    focus: str | None = Field(None, description="Optional area to focus on, e.g. 'cash', 'growth', 'hiring', "
                                                "'pricing', 'acquisition'")


class SchemeIn(BaseModel):
    scheme: str = Field(description="e.g. 'BAFE SP203-1', 'SSAIB', 'CHAS', 'NSI'")


class AccreditationUpdateIn(BaseModel):
    scheme: str
    certification_body: str | None = None
    certificate_number: str | None = None
    renewal_date: str | None = Field(None, description="YYYY-MM-DD")
    next_audit: str | None = Field(None, description="YYYY-MM-DD")
    audit_type: str | None = None


class StockLevelsIn(BaseModel):
    location: str | None = Field(None, description="'Stores' or a van, e.g. 'Van - Dan Harper'")
    search: str | None = Field(None, description="Filter by part code, name or category")


class StockMoveIn(BaseModel):
    kind: Literal["receive", "issue", "transfer", "return"] = Field(
        description="receive = delivery from supplier; issue = used on a job; transfer = stores<->van or van<->van; "
                    "return = unused stock back from a job")
    item: str = Field(description="Part code or unique part of the item name")
    qty: float
    from_location: str = Field("", description="Where it comes from (issue/transfer), e.g. 'Van - Dan Harper'")
    to_location: str = Field("", description="Where it goes (receive/transfer/return), default Stores")
    job_ref: str = ""
    note: str = ""


class StocktakeIn(BaseModel):
    location: str
    counts: dict[str, float] = Field(description="Counted quantity per part code or item name")


class StockItemIn(BaseModel):
    sku: str
    name: str | None = None
    category: str | None = None
    unit_cost: float | None = None
    reorder_level: float | None = None
    reorder_qty: float | None = None
    supplier: str | None = None


class PurchaseOrderIn(BaseModel):
    supplier: str
    supplier_email: str
    extra_note: str = ""


class PurchaseOrderLineIn(BaseModel):
    item: str = Field(description="Part code, or enough of the name to be unique, e.g. 'PIR-QUAD' or 'smoke detector'")
    qty: float = Field(gt=0)


class LogPurchaseOrderIn(BaseModel):
    supplier: str
    supplier_email: str
    items: list[PurchaseOrderLineIn] = Field(description="What to order and how many of each - any items, not just "
                                                         "ones below reorder level")
    note: str = Field("", description="Anything extra for the supplier, e.g. a delivery date or site address")


class JobRefIn(BaseModel):
    job_ref: str


class PlaceIn(BaseModel):
    place: str = Field(description="Salts FSM site name or a UK postcode")


class DateOptIn(BaseModel):
    date: str | None = Field(None, description="YYYY-MM-DD, default today")


class VanDayIn(BaseModel):
    engineer: str
    date: str | None = Field(None, description="YYYY-MM-DD, default today")


class RegWatchIn(BaseModel):
    focus: str | None = Field(None, description="Optional topic, e.g. 'employment rights changes', 'VAT', "
                                                "'minimum wage April', 'BS 5839 2025'")


class CustomerIn(BaseModel):
    customer: str | None = Field(None, description="One customer (partial name ok); omit for the whole book")


class RenewalsIn(BaseModel):
    days: int = Field(60, description="Look ahead this many days")


class PrepareRenewalIn(BaseModel):
    contract_id: str
    uplift_pct: float | None = Field(None, description="Price rise %, default from settings (usually 5)")


class MeetingIn(BaseModel):
    meeting: str | None = Field(None, description="Part of a recent Teams meeting's title; omit for the latest")
    transcript: str | None = Field(None, description="Pasted notes/transcript instead of a Teams meeting")
    title: str | None = Field(None, description="Name for pasted notes")


class ActionItemsIn(BaseModel):
    status: Literal["open", "done", "all"] = "open"


class ActionItemDoneIn(BaseModel):
    item_id: int


class RamsIn(BaseModel):
    job_ref: str | None = Field(None, description="Salts FSM job reference")
    description: str | None = Field(None, description="Scope of works if there's no job yet")


class QuestionnaireIn(BaseModel):
    questions: str = Field(description="The questions (copied from the tender / PQQ / attachment)")
    buyer: str | None = None


class HoursIn(BaseModel):
    hours: int | None = Field(None, description="Look back this many hours; default is since the office last "
                                               "closed (so Monday covers the weekend)")


class WithinDaysIn(BaseModel):
    within_days: int = 60


class AgedIn(BaseModel):
    kind: Literal["receivable", "payable"] = "receivable"


class VatIn(BaseModel):
    quarter_offset: int = Field(0, description="0 = current VAT quarter, -1 = previous")


class CashflowIn(BaseModel):
    weeks: int = 13


class CorpTaxIn(BaseModel):
    profit: float | None = Field(None, description="Taxable profit; omit to estimate from the last 12 months")


class PeriodIn(BaseModel):
    date_from: str
    date_to: str


class IssuesIn(BaseModel):
    status: Literal["open", "all", "resolved", "needs_human", "fix_ready"] = "open"


class IssueReportIn(BaseModel):
    title: str
    description: str
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    system: str = "Salts FSM"


class IssueIdIn(BaseModel):
    issue_id: int


class SuiteIn(BaseModel):
    suite: Literal["system", "compliance", "all"] = "all"


class KnowledgeIn(BaseModel):
    query: str


class RememberIn(BaseModel):
    fact: str


class ForgetIn(BaseModel):
    memory_id: int


class ArchiveIn(BaseModel):
    filename: str = Field(description="e.g. vat-estimate-q3.md")
    content: str


# --------------------------------------------------------------------------- handlers
async def email_inbox(j, a: InboxIn):
    return {"demo": j.mail.demo, "emails": await j.mail.list_messages(a.unread_only, min(a.limit, 25), a.since_hours)}


async def email_search(j, a: SearchIn):
    return {"demo": j.mail.demo, "emails": await j.mail.search_messages(a.query, min(a.limit, 25))}


async def email_read(j, a: MessageIn):
    return await j.mail.get_message(a.message_id)


async def email_draft_reply(j, a: DraftIn):
    return {"draft": await j.mail.create_reply_draft(a.message_id, a.reply),
            "note": "Saved as a draft in Outlook - not sent."}


async def email_send(j, a: SendIn):
    await j.mail.send_mail(a.to, a.subject, _html(a.body), a.cc or None)
    return f"Email sent to {', '.join(a.to)}."


def _html(text: str) -> str:
    from ..integrations.microsoft365 import text_to_html

    return text_to_html(text)


async def send_update_to_owner(j, a: OwnerUpdateIn):
    via = await j.notifier.send_owner_update(a.subject, a.message, channels=a.channels)
    return f"Update delivered via {via}."


async def show_on_display(j, a: DisplayIn):
    j.bus.publish("display", {"title": a.title, "markdown": a.markdown})
    return "Shown on the display."


async def fsm_jobs(j, a: JobsIn):
    start = _d(a.date_from, date.today())
    end = _d(a.date_to, start)
    return {"demo": j.fsm.demo, "jobs": await j.fsm.jobs(start, end, a.status, a.engineer)}


async def fsm_query(j, a: FsmQueryIn):
    return await j.fsm.get(a.path, a.params or None)


async def fsm_systems_due(j, a: DaysAheadIn):
    today = date.today()
    out = []
    for s in await j.fsm.systems():
        try:
            due = date.fromisoformat(str(s.get("next_service_due"))[:10])
        except ValueError:
            continue
        if due <= today + timedelta(days=a.days_ahead):
            out.append({**s, "days_until_due": (due - today).days})
    return {"demo": j.fsm.demo, "systems": sorted(out, key=lambda s: s["days_until_due"])}


async def fsm_contracts_renewing(j, a: DaysAheadIn):
    today = date.today()
    out = []
    for c in await j.fsm.contracts():
        try:
            rd = date.fromisoformat(str(c.get("renewal_date"))[:10])
        except ValueError:
            continue
        if rd <= today + timedelta(days=a.days_ahead):
            out.append({**c, "days_until_renewal": (rd - today).days})
    return {"demo": j.fsm.demo, "contracts": sorted(out, key=lambda c: c["days_until_renewal"])}


async def fsm_quotes(j, a: QuotesIn):
    return {"demo": j.fsm.demo, "quotes": await j.fsm.quotes(a.status)}


async def fsm_source_search(j, a: SourceSearchIn):
    if not j.github:
        return "The Salts FSM source repository isn't connected (set GITHUB_TOKEN and FSM_REPO)."
    return await j.github.search_code(a.query)


async def fsm_source_read(j, a: SourceReadIn):
    if not j.github:
        return "The Salts FSM source repository isn't connected (set GITHUB_TOKEN and FSM_REPO)."
    return await j.github.read_file(a.path)


async def staff_today(j, a: NoInput):
    return await j.staff.board()


async def staff_productivity(j, a: ProductivityIn):
    return await j.staff.productivity(max(1, min(a.days, 365)), a.engineer)


async def staff_roles(j, a: PersonIn):
    if a.name:
        return j.register.find(a.name) or f"{a.name} isn't in the staff register yet."
    return {"source": j.register.load()["_source"], "staff": j.register.people()}


async def staff_review(j, a: ReviewIn):
    return await j.reviewer.review(max(1, min(a.days, 180)), a.name)


async def staff_update_role(j, a: RoleUpdateIn):
    person = j.register.upsert(a.name, role=a.role, type_=a.type, email=a.email, add_duties=a.add_duties,
                               remove_duties=a.remove_duties, expectations=a.expectations or None)
    j.brain.refresh_system()
    return {"updated": person}


async def office_productivity(j, a: OfficeIn):
    from ..services.performance import office_productivity as run

    return await run(j.fsm, j.mail, j.register, max(1, min(a.days, 180)))


async def fsm_change(j, a: FsmChangeIn):
    result = await j.fsm.write(a.method, a.path, a.body)
    return f"Salts FSM updated: {str(result)[:300]}"


async def run_security_review(j, a: NoInput):
    return j.security_watch.start()


async def log_job(j, a: LogJobIn):
    body: dict[str, Any] = {"site": a.site, "type": a.type, "description": a.description, "created_by": "Jarvis"}
    for key, value in (("priority", a.priority), ("engineer", a.engineer),
                       ("scheduled_start", a.scheduled_start), ("customer", a.customer)):
        if value:
            body[key] = value
    summary = f"Log a {a.type} job at {a.site}: {a.description}"
    if a.engineer:
        summary += f" - assign to {a.engineer}"
    if a.scheduled_start:
        summary += f" for {a.scheduled_start}"
    action_id = j.actions.queue("fsm_write", summary, {"method": "POST", "path": "/jobs", "body": body})
    return {"queued_action": action_id, "job": body, "note": "Queued for approval on the display."}


async def business_health(j, a: HealthIn):
    return await j.accountant.health_check(max(30, min(a.days, 365)))


async def marketing_overview(j, a: SocialIn):
    return await j.marketing.overview(max(7, min(a.days, 365)))


async def search_rankings(j, a: SocialIn):
    return await j.marketing.search_rankings(max(7, min(a.days, 90)))


async def seo_audit(j, a: UrlIn):
    return await j.marketing.seo_audit(a.url)


async def business_advice(j, a: AdviceIn):
    text = await j.advisor.report(a.focus)
    return {"report_shown_on_display": True, "report": text}


async def accreditations_status(j, a: NoInput):
    return j.accreditations.status()


async def accreditation_update(j, a: AccreditationUpdateIn):
    return j.accreditations.update(a.scheme, a.model_dump(exclude={"scheme"}))


async def audit_evidence(j, a: SchemeIn):
    return await j.accreditations.gather_evidence(a.scheme)


async def audit_evidence_pack(j, a: SchemeIn):
    text = await j.accreditations.evidence_pack(a.scheme)
    return {"shown_on_display": True, "pack": text}


async def stock_levels(j, a: StockLevelsIn):
    await j.stores.sync()
    data = j.stores.levels(a.location, a.search)
    data["locations"] = j.stores.locations()
    return data


async def stock_move(j, a: StockMoveIn):
    return await j.stores.record(a.kind, a.item, a.qty, from_loc=a.from_location, to_loc=a.to_location,
                                 job_ref=a.job_ref, note=a.note)


async def stock_stocktake(j, a: StocktakeIn):
    await j.stores.sync()
    return j.stores.stocktake(a.location, a.counts)


async def stock_item_update(j, a: StockItemIn):
    return j.stores.upsert_item(a.sku, **a.model_dump(exclude={"sku"}))


async def stock_reorder(j, a: NoInput):
    await j.stores.sync()
    return j.stores.reorder_list()


async def stock_purchase_order(j, a: PurchaseOrderIn):
    await j.stores.sync()
    order = next((o for o in j.stores.reorder_list()["purchase_orders"]
                  if a.supplier.lower() in o["supplier"].lower()), None)
    if not order:
        return f"Nothing below reorder level from {a.supplier}."
    lines = "\n".join(f"- {l['order_qty']:g} x {l['item']} ({l['sku']}) @ £{l['unit_cost']:.2f}" for l in order["lines"])
    body = (f"Hello,\n\nPlease supply the following for {j.settings.company_name}:\n\n{lines}\n\n"
            f"Order value (ex VAT): £{order['total_ex_vat']:,.2f}\n{a.extra_note}\n\n"
            f"Please confirm prices and delivery date.\n\nKind regards,\n{j.settings.owner_name}\n{j.settings.company_name}")
    action_id = j.actions.queue("email_send", f"Purchase order to {order['supplier']} (£{order['total_ex_vat']:,.2f} ex VAT)",
                                {"to": [a.supplier_email], "cc": [], "subject": f"Purchase order - {j.settings.company_name}",
                                 "body": body})
    return {"queued_action": action_id, "order": order, "note": "Queued for approval on the display."}


async def log_purchase_order(j, a: LogPurchaseOrderIn):
    await j.stores.sync()  # the parts prices Salts FSM holds, refreshed monthly - as current as the business keeps them
    lines: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for entry in a.items:
        try:
            it = j.stores.resolve_item(entry.item)
        except ValueError as e:
            unresolved.append(str(e))
            continue
        line_cost = round(entry.qty * it["unit_cost"], 2)
        lines.append({"sku": it["sku"], "name": it["name"], "qty": entry.qty, "unit_cost": it["unit_cost"],
                      "line_cost": line_cost})
    if not lines:
        return {"error": "None of those items matched anything in the stock records.", "not_ordered": unresolved}
    total = round(sum(l["line_cost"] for l in lines), 2)
    body_lines = "\n".join(f"- {l['qty']:g} x {l['name']} ({l['sku']}) @ £{l['unit_cost']:.2f} = £{l['line_cost']:.2f}"
                           for l in lines)
    body = (f"Hello,\n\nPlease supply the following for {j.settings.company_name}:\n\n{body_lines}\n\n"
            f"Order value (ex VAT): £{total:,.2f} - prices are from our own records, please confirm before "
            f"dispatch.\n{a.note}\n\nKind regards,\n{j.settings.owner_name}\n{j.settings.company_name}")
    action_id = j.actions.queue("email_send", f"Purchase order to {a.supplier} (£{total:,.2f} ex VAT)",
                                {"to": [a.supplier_email], "cc": [], "subject": f"Purchase order - {j.settings.company_name}",
                                 "body": body})
    result: dict[str, Any] = {"queued_action": action_id, "supplier": a.supplier, "lines": lines,
                              "total_ex_vat": total, "note": "Queued for approval on the display."}
    if unresolved:
        result["not_ordered"] = unresolved
    return result


async def stock_usage(j, a: OfficeIn):
    await j.stores.sync()
    return j.stores.usage(max(7, min(a.days, 365)))


async def stock_job_materials(j, a: JobRefIn):
    await j.stores.sync()
    return j.stores.job_materials(a.job_ref)


async def engineer_locations(j, a: NoInput):
    data = await j.tracker.live()
    j.bus.publish("map", data)
    return data


async def nearest_engineer(j, a: PlaceIn):
    return await j.tracker.nearest(a.place)


async def attendance_check(j, a: DateOptIn):
    from datetime import datetime as _dt

    return await j.tracker.attendance(_dt.fromisoformat(a.date) if a.date else None)


async def van_day(j, a: VanDayIn):
    from datetime import date as _date

    return await j.tracker.van_day(a.engineer, _date.fromisoformat(a.date) if a.date else _date.today())


async def timesheet_check(j, a: DateOptIn):
    from datetime import date as _date

    return await j.tracker.timesheet_check(_date.fromisoformat(a.date) if a.date else _date.today())


async def regulatory_watch(j, a: RegWatchIn):
    text = await j.regwatch.briefing(a.focus)
    return {"shown_on_display": True, "update": text}


async def unbilled_jobs(j, a: OfficeIn):
    return await j.billing.unbilled_jobs(max(1, min(a.days, 120)))


async def raise_invoices(j, a: OfficeIn):
    return await j.billing.queue_invoices(max(1, min(a.days, 120)))


async def review_requests(j, a: NoInput):
    return await j.billing.queue_review_requests()


async def remedial_quotes(j, a: NoInput):
    from ..services.remedials import remedial_pipeline

    return await remedial_pipeline(j.fsm)


async def suggestions_list(j, a: NoInput):
    return await j.suggestions.sweep(announce=False)


async def end_of_day_wrap_up(j, a: NoInput):
    return await j.wrapup.run(deliver=False)


async def customer_health(j, a: CustomerIn):
    if a.customer:
        return await j.customers.customer(a.customer)
    return await j.customers.scores(refresh=True)


async def contract_renewals(j, a: RenewalsIn):
    return await j.renewals.due(max(7, min(a.days, 365)))


async def prepare_renewal(j, a: PrepareRenewalIn):
    return await j.renewals.prepare(a.contract_id, a.uplift_pct)


async def lone_worker_check(j, a: NoInput):
    return await j.tracker.lone_worker_check(j.settings.lone_worker_overrun_min) or "Nobody is overrunning."


async def meeting_actions(j, a: MeetingIn):
    return await j.meetings.process(meeting=a.meeting, transcript=a.transcript, title=a.title)


async def action_items(j, a: ActionItemsIn):
    return j.db.action_items(None if a.status == "all" else a.status)


async def action_item_done(j, a: ActionItemDoneIn):
    j.db.set_action_item_status(a.item_id, "done")
    return f"Marked action #{a.item_id} done."


async def draft_rams(j, a: RamsIn):
    return {"shown_on_display": True, "rams": await j.documents.rams(a.job_ref, a.description)}


async def answer_questionnaire(j, a: QuestionnaireIn):
    return {"shown_on_display": True, "answers": await j.documents.questionnaire(a.questions, a.buyer)}


async def out_of_hours_calls(j, a: HoursIn):
    return await j.ooh.calls(max(1, min(a.hours, 240)) if a.hours else None)


async def staff_overdue_jobs(j, a: NoInput):
    return await j.staff.overdue_jobs()


async def staff_certifications(j, a: WithinDaysIn):
    return await j.staff.expiring_certifications(a.within_days)


async def finance_snapshot(j, a: NoInput):
    return await j.accountant.snapshot()


async def finance_aged(j, a: AgedIn):
    return await j.accountant.aged(a.kind)


async def finance_vat(j, a: VatIn):
    return await j.accountant.vat(a.quarter_offset)


async def finance_cashflow(j, a: CashflowIn):
    return await j.accountant.cashflow(max(1, min(a.weeks, 52)))


async def finance_corporation_tax(j, a: CorpTaxIn):
    return await j.accountant.corporation_tax(a.profit)


async def finance_profit_and_loss(j, a: PeriodIn):
    return await j.accountant.profit_and_loss(date.fromisoformat(a.date_from), date.fromisoformat(a.date_to))


async def finance_deadlines(j, a: NoInput):
    return j.accountant.deadlines()


async def finance_credit_control(j, a: NoInput):
    return await j.accountant.credit_control()


async def issues_list(j, a: IssuesIn):
    rows = j.db.list_issues(None if a.status == "all" else a.status, 50)
    return [j.issues.summary(r) for r in rows]


async def issue_report(j, a: IssueReportIn):
    issue = await j.issues.report(reporter=j.settings.owner_name, title=a.title, description=a.description,
                                  severity=a.severity, system=a.system, source="jarvis", notify=False)
    return f"Logged as issue #{issue['id']}; triage has started."


async def issue_fix(j, a: IssueIdIn):
    if not j.fixer.enabled:
        return "Auto-fix isn't configured (needs GITHUB_TOKEN and FSM_REPO)."
    j.issues._spawn(j.fixer.attempt(a.issue_id))
    return f"The engineering agent has started on issue #{a.issue_id}. The fix will come back to you as a pull request."


async def routine_tests_run(j, a: SuiteIn):
    results = await j.tester.run(a.suite)
    return {"passed": sum(r["ok"] for r in results), "failed": [r for r in results if not r["ok"]],
            "all": results}


async def routine_tests_status(j, a: NoInput):
    return j.db.latest_test_results()


async def knowledge_search(j, a: KnowledgeIn):
    return j.kb.search(a.query) or "Nothing relevant in the knowledge base."


async def remember(j, a: RememberIn):
    mid = j.db.remember(a.fact)
    j.brain.refresh_system()
    return f"Remembered (#{mid})."


async def forget(j, a: ForgetIn):
    j.db.forget(a.memory_id)
    j.brain.refresh_system()
    return "Forgotten."


async def archive_to_azure(j, a: ArchiveIn):
    if not j.blob.enabled:
        return "Azure Blob Storage isn't configured (AZURE_STORAGE_CONNECTION_STRING)."
    url = await j.blob.upload(a.filename, a.content)
    return f"Uploaded to Azure: {url}"


async def morning_briefing(j, a: NoInput):
    return await j.briefings.morning_briefing(deliver=False)


TOOLS: list[Tool] = [
    Tool("email_inbox", "List recent emails in the owner's Outlook inbox (sender, subject, preview, id).",
         InboxIn, email_inbox, "Checking the inbox"),
    Tool("email_search", "Search the owner's mailbox by keywords, sender name, company or subject.",
         SearchIn, email_search, "Searching email"),
    Tool("email_read", "Read one email in full by id.", MessageIn, email_read, "Reading email"),
    Tool("email_draft_reply", "Save a reply to an email as a draft in Outlook for the owner to review and send.",
         DraftIn, email_draft_reply, "Drafting a reply"),
    Tool("email_send", "Send an email from the owner's mailbox. Emails to anyone except the owner are queued for "
                       "his approval on the display rather than sent immediately.", SendIn, email_send, "Preparing email",
         approval=True, describe=lambda a: f"Send email '{a.subject}' to {', '.join(a.to)}"),
    Tool("send_update_to_owner", "Send the owner an update on Microsoft Teams and/or email. Use when he asks you to "
                                 "send him something or keep him posted.", OwnerUpdateIn, send_update_to_owner,
         "Sending you an update"),
    Tool("show_on_display", "Put detailed content (tables, figures, drafts, lists) on the owner's screen. Use for "
                            "anything too detailed to say aloud.", DisplayIn, show_on_display, "Updating the display"),
    Tool("fsm_jobs", "Jobs from Salts FSM in a date range (default today), optionally by status or engineer.",
         JobsIn, fsm_jobs, "Checking jobs in Salts FSM"),
    Tool("fsm_query", "Read-only GET against any Salts FSM API path, for details not covered by other tools "
                      "(e.g. a customer record or a single job).", FsmQueryIn, fsm_query, "Querying Salts FSM"),
    Tool("fsm_systems_due", "Maintained systems (fire alarm, emergency lighting, intruder, CCTV, access control) "
                            "overdue or due a service visit within N days.", DaysAheadIn, fsm_systems_due,
         "Checking service schedules"),
    Tool("fsm_contracts_renewing", "Maintenance contracts due for renewal within N days (or already past renewal).",
         DaysAheadIn, fsm_contracts_renewing, "Checking contract renewals"),
    Tool("fsm_quotes", "Quotes in Salts FSM, optionally filtered by status.", QuotesIn, fsm_quotes, "Checking quotes"),
    Tool("fsm_source_search", "Search the Salts FSM source code on GitHub - use to explain how a feature works.",
         SourceSearchIn, fsm_source_search, "Searching the FSM code"),
    Tool("fsm_source_read", "Read a file (or list a folder) from the Salts FSM source code on GitHub.",
         SourceReadIn, fsm_source_read, "Reading the FSM code"),
    Tool("staff_today", "Live staff board: where each engineer is, current and next job, jobs done today, late "
                        "starts and unassigned jobs.", NoInput, staff_today, "Checking on the team"),
    Tool("staff_productivity", "Productivity per engineer over a period: jobs completed, hours worked vs hours on "
                               "jobs (utilisation), revenue and revenue per hour, jobs per day, average job time, "
                               "on-time starts and revisit rate, compared with the team average and the previous "
                               "period.", ProductivityIn, staff_productivity, "Analysing staff productivity"),
    Tool("staff_roles", "The staff register: each person's role, duties and expected targets. Use it to "
                        "understand who is responsible for what.", PersonIn, staff_roles, "Checking roles and duties"),
    Tool("staff_review", "Review every member of staff (engineers and office) against the expectations for their "
                         "role and flag anyone falling short, with the evidence.", ReviewIn, staff_review,
         "Reviewing team performance"),
    Tool("staff_update_role", "Add or update a staff member's role, duties or expected targets in the register "
                              "when the owner tells you about them.", RoleUpdateIn, staff_update_role,
         "Updating the staff register",
         approval=True, describe=lambda a: f"Update staff register for {a.name}" + (f": role {a.role}" if a.role else "") + (f"; add duties {a.add_duties}" if a.add_duties else "") + (f"; targets {a.expectations}" if a.expectations else "")),
    Tool("office_productivity", "Office staff productivity: quotes raised/value/win rate and jobs booked (Salts "
                                "FSM) plus Microsoft 365 activity counts (emails sent/received, Teams messages, "
                                "calls, meetings).", OfficeIn, office_productivity, "Analysing office productivity"),
    Tool("fsm_change", "Create or update something in Salts FSM (book or reassign a job, update a record). Always "
                       "queued for the owner's approval first.", FsmChangeIn, fsm_change, "Preparing an FSM change",
         approval=True, describe=lambda a: f"Salts FSM: {a.summary}"),
    Tool("run_security_review", "Have the auto-fix engineer review the whole Salts FSM codebase for security "
                                "vulnerabilities right now, rather than waiting for the weekly scheduled one. "
                                "Runs in the background and can take a few minutes; findings become issues and "
                                "the owner's notified, same as the scheduled review.", NoInput, run_security_review,
         "Starting a security review"),
    Tool("log_job", "Log a new job in Salts FSM from a plain description - a fault report, call-out or booking. "
                    "Use this rather than fsm_change whenever it's specifically about logging or booking a job; "
                    "give the site, what's wrong/needed, and the engineer and date if named. Queued for the "
                    "owner's approval, never booked straight away.", LogJobIn, log_job, "Logging a job"),
    Tool("business_health", "Business health check: revenue growth, margins, debtor days, overdue debt, cash "
                            "runway, recurring contract revenue, quote win rate, utilisation and unbilled work vs "
                            "targets, with recommended actions.", HealthIn, business_health,
         "Running a business health check"),
    Tool("marketing_overview", "Social media followers (Facebook, Instagram, LinkedIn, TikTok) and Google reviews "
                               "with growth over the last 7 days and the period.", SocialIn, marketing_overview,
         "Checking the socials"),
    Tool("search_rankings", "Google Search Console: top search queries and pages with clicks, impressions and "
                            "average position, plus the company's target keywords.", SocialIn, search_rankings,
         "Checking Google rankings"),
    Tool("seo_audit", "Audit the website for local SEO: titles, descriptions, headings, structured data, local "
                      "keywords, accreditations, speed, sitemap - with fixes.", UrlIn, seo_audit,
         "Auditing the website"),
    Tool("business_advice", "Business consultant report: board-level review across finance, team, sales, "
                            "operations, compliance and marketing with risks, opportunities and a 90-day plan, or a "
                            "consultant deep dive on a focus area (pricing, growth, hiring, efficiency, SWOT, "
                            "acquisition...). Shown on the display.",
         AdviceIn, business_advice, "Preparing business advice"),
    Tool("accreditations_status", "BAFE, SSAIB, CHAS, NSI etc.: certificates, renewal and audit dates, plus "
                                  "calibration, insurance and policy review dates, soonest first.", NoInput,
         accreditations_status, "Checking accreditations"),
    Tool("accreditation_update", "Record accreditation details the owner gives you (certificate number, renewal "
                                 "or audit date, certification body).", AccreditationUpdateIn, accreditation_update,
         "Updating accreditations",
         approval=True, describe=lambda a: f"Update {a.scheme}: " + ", ".join(f"{k}={v}" for k, v in a.model_dump(exclude={"scheme"}).items() if v)),
    Tool("audit_evidence", "Raw evidence for a scheme's audit/renewal from live data: competency, qualifications, "
                           "maintenance compliance, job sample, complaints log, calibration, insurance, policies.",
         SchemeIn, audit_evidence, "Gathering audit evidence"),
    Tool("audit_evidence_pack", "Write a full audit-ready evidence pack and draft questionnaire answers for BAFE, "
                                "SSAIB, CHAS etc. Shown on the display.", SchemeIn, audit_evidence_pack,
         "Building the evidence pack"),
    Tool("stock_levels", "Stock on hand in the stores and on each van, with value and reorder flags.",
         StockLevelsIn, stock_levels, "Checking stock"),
    Tool("stock_move", "Record a stock movement: goods received, parts used on a job, stores/van transfers, "
                       "returns. Use whenever the owner or an engineer says stock came in, was taken or used.",
         StockMoveIn, stock_move, "Updating stock",
         approval=True, describe=lambda a: f"Stock: {a.kind} {a.qty:g} x {a.item}" + (f" from {a.from_location}" if a.from_location else "") + (f" to {a.to_location}" if a.to_location else "") + (f" for job {a.job_ref}" if a.job_ref else "")),
    Tool("stock_stocktake", "Record a stocktake count for a location and report variances (value of shrinkage).",
         StocktakeIn, stock_stocktake, "Recording the stocktake",
         approval=True, describe=lambda a: f"Record stocktake at {a.location} ({len(a.counts)} lines) and adjust stock levels"),
    Tool("stock_item_update", "Add a new stock item or change its cost, reorder level, reorder quantity or "
                              "supplier.", StockItemIn, stock_item_update, "Updating the stock item",
         approval=True, describe=lambda a: f"Stock item {a.sku}: " + ", ".join(f"{k}={v}" for k, v in a.model_dump(exclude={"sku"}).items() if v is not None)),
    Tool("stock_reorder", "Items below reorder level grouped by supplier with suggested order quantities and cost.",
         NoInput, stock_reorder, "Building the reorder list"),
    Tool("stock_purchase_order", "Draft a purchase order email to a supplier for everything below reorder level "
                                 "from them - queued for the owner's approval.", PurchaseOrderIn,
         stock_purchase_order, "Drafting a purchase order"),
    Tool("log_purchase_order", "Raise a purchase order for specific items and quantities, for any supplier - not "
                               "just what's below reorder level. Prices come from Salts FSM's own stock records "
                               "(updated monthly). Queued as an email for the owner's approval, never sent "
                               "straight away.", LogPurchaseOrderIn, log_purchase_order, "Drafting a purchase order"),
    Tool("stock_usage", "Stock usage over N days: fast movers, weeks of cover, slow/dead stock and its value.",
         OfficeIn, stock_usage, "Analysing stock usage"),
    Tool("stock_job_materials", "Materials issued to a job and their cost (for job costing).", JobRefIn,
         stock_job_materials, "Costing job materials"),
    Tool("engineer_locations", "Live engineer/van locations from Salts FSM tracking: where everyone is, on site or "
                               "not, ETA to next job. Also puts the map on the display.", NoInput,
         engineer_locations, "Locating the team"),
    Tool("nearest_engineer", "Which engineers are closest to a site or postcode, with estimated drive time - use "
                             "for dispatching call-outs.", PlaceIn, nearest_engineer, "Finding the nearest engineer"),
    Tool("attendance_check", "Check job check-ins against site locations and flag late arrivals for a day.",
         DateOptIn, attendance_check, "Checking attendance"),
    Tool("van_day", "From RAM Tracking: an engineer's day in the van - when they set off, each site visited with "
                    "arrival/departure and time on site, driving time, miles, and when they got home.", VanDayIn,
         van_day, "Checking the van tracker"),
    Tool("timesheet_check", "Compare each engineer's working day from RAM Tracking (set off to home) with their "
                            "Salts FSM timesheet for a date and flag differences.", DateOptIn, timesheet_check,
         "Checking timesheets against the trackers"),
    Tool("regulatory_watch", "Research current and upcoming UK tax, employment law, company law and fire & "
                             "security regulation changes that affect the business and its directors, with dates, "
                             "impact and actions (web-researched, sourced). Shown on the display.", RegWatchIn,
         regulatory_watch, "Researching tax and law changes"),
    Tool("unbilled_jobs", "Completed Salts FSM jobs in the last N days that don't appear to have been invoiced in "
                          "Sage - money being left on the table.", OfficeIn, unbilled_jobs, "Looking for unbilled work"),
    Tool("raise_invoices", "Draft Sage invoices for completed-but-unbilled jobs and queue them for the owner's "
                           "approval (created in Sage once approved).", OfficeIn, raise_invoices, "Drafting invoices"),
    Tool("review_requests", "Prepare thank-you + Google review request emails for today's completed jobs, queued as "
                            "one approval.", NoInput, review_requests, "Preparing review requests"),
    Tool("remedial_quotes", "Remedial quotes Salts FSM raised from service-visit defects: open pipeline and value, "
                            "which need chasing (7 and 21 days), and win rate.", NoInput, remedial_quotes,
         "Checking remedial quotes"),
    Tool("suggestions", "Refresh and list your current proactive suggestions (unbilled work, quotes to chase, "
                        "overdue jobs to assign, debts to chase, stock to reorder, expiring qualifications, audits).",
         NoInput, suggestions_list, "Reviewing suggestions"),
    Tool("end_of_day_wrap_up", "The end-of-day wrap-up: what got done, what slipped, what's awaiting approval, "
                               "and tomorrow's first jobs and risks.", NoInput, end_of_day_wrap_up,
         "Preparing your wrap-up"),
    Tool("customer_health", "Customer health watch: a 0-100 score per customer from spend trend, overdue debt, "
                            "repeat call-outs, declined quotes, overdue service visits, logged problems, inactivity "
                            "and lapsed renewals - who is at risk (especially before renewal), why, and what to do. "
                            "Also flags revenue concentration.", CustomerIn, customer_health,
         "Checking customer health"),
    Tool("contract_renewals", "Maintenance contracts renewing soon, with value, customer health and whether the "
                              "renewal letter has been prepared.", RenewalsIn, contract_renewals,
         "Checking contract renewals"),
    Tool("prepare_renewal", "Write the renewal letter for a contract with the price uplift and queue it for the "
                            "owner's approval (warns if the customer is at risk).", PrepareRenewalIn, prepare_renewal,
         "Preparing the renewal"),
    Tool("lone_worker_check", "Engineers still on a job well past its booked end - a safety check prompt.", NoInput,
         lone_worker_check, "Checking on lone workers"),
    Tool("meeting_actions", "Turn the latest (or a named) Teams meeting's transcript - or pasted notes - into a "
                            "summary, decisions and tracked action items with owners and due dates.", MeetingIn,
         meeting_actions, "Writing up the meeting"),
    Tool("action_items", "Tracked action items from meetings (open, done or all), with owners and due dates.",
         ActionItemsIn, action_items, "Checking action items"),
    Tool("action_item_done", "Mark a tracked meeting action as done when the owner says it's finished.",
         ActionItemDoneIn, action_item_done, "Updating the action list"),
    Tool("draft_rams", "Draft a risk assessment & method statement (RAMS) for a Salts FSM job or described "
                       "works, shown on the display.", RamsIn, draft_rams, "Drafting the RAMS"),
    Tool("answer_questionnaire", "Draft answers to a tender / PQQ / Constructionline / supplier questionnaire from the "
                                 "company's accreditations, policies, insurance and competency evidence, with gaps "
                                 "marked. Shown on the display.", QuestionnaireIn, answer_questionnaire,
         "Answering the questionnaire"),
    Tool("out_of_hours_calls", "Overnight events from the out-of-hours / alarm monitoring reports emailed to info@ "
                               "(including PDF reports): calls taken and alarm faults, comms failures and "
                               "activations - site, urgency, what was done, and which still need a job in Salts FSM.", HoursIn, out_of_hours_calls, "Checking overnight calls"),
    Tool("staff_overdue_jobs", "Jobs and call-outs that are past their scheduled time and not completed.",
         NoInput, staff_overdue_jobs, "Checking overdue jobs"),
    Tool("staff_certifications", "Engineer qualifications/cards expiring within N days or already expired.",
         WithinDaysIn, staff_certifications, "Checking qualifications"),
    Tool("finance_snapshot", "Headline finances: cash at bank, debtors and overdue debt, creditors, debtor days, "
                             "current VAT estimate.", NoInput, finance_snapshot, "Checking the accounts"),
    Tool("finance_aged", "Aged debtors (receivable) or aged creditors (payable) with buckets and overdue invoices.",
         AgedIn, finance_aged, "Running the aged report"),
    Tool("finance_vat", "Estimate a VAT return (boxes 1, 4, 5, 6, 7) for the current or a previous quarter.",
         VatIn, finance_vat, "Estimating VAT"),
    Tool("finance_cashflow", "Weekly cash-flow forecast from bank balance, expected receipts and bills due.",
         CashflowIn, finance_cashflow, "Forecasting cash flow"),
    Tool("finance_corporation_tax", "Estimate UK corporation tax (19%/25% with marginal relief) and due dates.",
         CorpTaxIn, finance_corporation_tax, "Estimating corporation tax"),
    Tool("finance_profit_and_loss", "Profit and loss between two dates (YYYY-MM-DD).", PeriodIn,
         finance_profit_and_loss, "Building the P&L"),
    Tool("finance_deadlines", "Upcoming tax and statutory deadlines (VAT, PAYE, CIS, corporation tax, accounts).",
         NoInput, finance_deadlines, "Checking deadlines"),
    Tool("finance_credit_control", "Overdue customer invoices with the recommended chasing step and statutory "
                                   "late-payment interest.", NoInput, finance_credit_control, "Reviewing credit control"),
    Tool("issues_list", "Problems reported by staff or found by routine tests, with triage and fix status.",
         IssuesIn, issues_list, "Checking reported issues"),
    Tool("issue_report", "Log a new issue on the owner's behalf.", IssueReportIn, issue_report, "Logging the issue"),
    Tool("issue_fix", "Start the engineering agent on an issue: it prepares a code fix as a GitHub pull request "
                      "(deployment still needs approval).", IssueIdIn, issue_fix, "Starting a fix",
         approval=True, describe=lambda a: f"Prepare a code fix for issue #{a.issue_id} (opens a pull request for review)"),
    Tool("routine_tests_run", "Run routine tests now: 'system' (Salts FSM uptime, pages, TLS, integrations), "
                              "'compliance' (services overdue, renewals, overdue call-outs, qualifications) or 'all'.",
         SuiteIn, routine_tests_run, "Running routine tests"),
    Tool("routine_tests_status", "Latest result of every routine test.", NoInput, routine_tests_status,
         "Checking test results"),
    Tool("knowledge_search", "Search the company knowledge base: fire & security standards (BS 5839, BS 5266, "
                             "BS EN 50131...), legislation, certification (BAFE/NSI/SSAIB), UK tax and accounting, "
                             "and company procedures.", KnowledgeIn, knowledge_search, "Checking the knowledge base"),
    Tool("remember", "Save a fact or preference the owner wants you to remember long term.", RememberIn, remember,
         "Making a note"),
    Tool("forget", "Delete a remembered fact by its number.", ForgetIn, forget, "Forgetting that"),
    Tool("archive_to_azure", "Upload a report or document to the company's Azure Blob Storage archive.",
         ArchiveIn, archive_to_azure, "Uploading to Azure",
         approval=True, describe=lambda a: f"Upload {a.filename} to the Azure archive"),
    Tool("morning_briefing", "Generate the full morning briefing now (email, jobs, staff, money, issues).",
         NoInput, morning_briefing, "Preparing your briefing"),
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}

SERVER_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 5,
     "user_location": {"type": "approximate", "city": "Bradford", "region": "England", "country": "GB",
                       "timezone": "Europe/London"}},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5},
]
