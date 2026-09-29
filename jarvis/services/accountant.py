"""Company accountant: cash, debtors/creditors, VAT, corporation tax, cash-flow and deadlines.

These are management-accounting estimates to help run the business day to day.
Statutory filings (VAT returns, CT600, accounts) should still be reviewed by the
company's qualified accountant.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from ..config import Settings
from ..integrations.finance import Invoice

BUCKETS = (("current", -10**6, 0), ("1-30", 1, 30), ("31-60", 31, 60), ("61-90", 61, 90), ("90+", 91, 10**6))
SMALL_PROFITS_LIMIT = 50_000
MAIN_RATE_LIMIT = 250_000
SMALL_RATE, MAIN_RATE, MARGINAL_FRACTION = 0.19, 0.25, 3 / 200


def _money(x: float) -> float:
    return round(x + 0.0, 2)


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


# ---------------------------------------------------------------------------
# Pure calculations (unit tested)
# ---------------------------------------------------------------------------

def age_invoices(invoices: list[Invoice], today: date) -> dict[str, Any]:
    buckets = {name: 0.0 for name, _, _ in BUCKETS}
    by_contact: dict[str, float] = defaultdict(float)
    overdue = []
    for inv in invoices:
        if inv.amount_due <= 0 or inv.status in ("voided", "deleted", "draft"):
            continue
        days = (today - inv.due_date).days
        for name, lo, hi in BUCKETS:
            if lo <= days <= hi:
                buckets[name] += inv.amount_due
                break
        by_contact[inv.contact] += inv.amount_due
        if days > 0:
            overdue.append({**inv.to_dict(), "days_overdue": days})
    total = sum(buckets.values())
    overdue.sort(key=lambda r: (-r["days_overdue"], -r["amount_due"]))
    top = sorted(by_contact.items(), key=lambda kv: -kv[1])[:10]
    return {
        "total_outstanding": _money(total),
        "total_overdue": _money(total - buckets["current"]),
        "buckets": {k: _money(v) for k, v in buckets.items()},
        "top_contacts": [{"contact": c, "amount": _money(a)} for c, a in top],
        "overdue_invoices": overdue[:25],
        "overdue_count": len(overdue),
    }


def vat_quarter(today: date, quarter_end_months: list[int], offset: int = 0) -> tuple[date, date, date]:
    """(start, end, payment/filing due) of the VAT quarter containing ``today``, shifted by ``offset`` quarters."""
    ends = []
    for y in (today.year - 2, today.year - 1, today.year, today.year + 1):
        ends += [_month_end(y, m) for m in quarter_end_months]
    ends.sort()
    idx = next(i for i, e in enumerate(ends) if e >= today) + offset
    end = ends[idx]
    start = ends[idx - 1] + timedelta(days=1)
    nxt = _add_months(end.replace(day=1), 1)
    due = _month_end(nxt.year, nxt.month) + timedelta(days=7)  # 1 calendar month + 7 days after period end
    return start, end, due


def vat_estimate(sales: list[Invoice], purchases: list[Invoice], start: date, end: date) -> dict[str, Any]:
    """Invoice (accrual) basis: VAT on invoices dated in the period."""
    def in_period(inv: Invoice) -> bool:
        return start <= inv.date <= end and inv.status not in ("voided", "deleted", "draft")

    output_vat = sum(i.tax for i in sales if in_period(i))
    input_vat = sum(i.tax for i in purchases if in_period(i))
    net_sales = sum(i.total - i.tax for i in sales if in_period(i))
    net_purchases = sum(i.total - i.tax for i in purchases if in_period(i))
    return {
        "period_start": start.isoformat(), "period_end": end.isoformat(),
        "box1_output_vat": _money(output_vat), "box4_input_vat": _money(input_vat),
        "box5_net_vat_payable": _money(output_vat - input_vat),
        "box6_net_sales": _money(net_sales), "box7_net_purchases": _money(net_purchases),
        "note": "Estimate on the invoice basis from invoices dated in the period. Excludes journals, "
                "reverse-charge (CIS) adjustments and expenses not entered as bills.",
    }


def corporation_tax(profit: float, associated_companies: int = 0, period_months: int = 12) -> dict[str, Any]:
    """UK corporation tax with marginal relief (rates from 1 April 2023)."""
    divisor = (1 + associated_companies) * (12 / period_months)
    lower, upper = SMALL_PROFITS_LIMIT / divisor, MAIN_RATE_LIMIT / divisor
    if profit <= 0:
        tax, band = 0.0, "loss / nil"
    elif profit <= lower:
        tax, band = profit * SMALL_RATE, "small profits rate 19%"
    elif profit >= upper:
        tax, band = profit * MAIN_RATE, "main rate 25%"
    else:
        tax, band = profit * MAIN_RATE - (upper - profit) * MARGINAL_FRACTION, "main rate with marginal relief"
    return {"taxable_profit": _money(profit), "tax": _money(tax), "band": band,
            "effective_rate_pct": round(100 * tax / profit, 2) if profit > 0 else 0.0,
            "lower_limit": _money(lower), "upper_limit": _money(upper)}


def late_payment_claim(amount: float, days_overdue: int, base_rate_pct: float) -> dict[str, float]:
    """Late Payment of Commercial Debts (Interest) Act 1998: 8% over base + fixed compensation."""
    interest = amount * (0.08 + base_rate_pct / 100) * max(days_overdue, 0) / 365
    fixed = 40.0 if amount < 1000 else 70.0 if amount < 10000 else 100.0
    return {"statutory_interest": _money(interest), "fixed_compensation": fixed}


def credit_control_stage(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "not yet due"
    if days_overdue <= 7:
        return "friendly reminder email"
    if days_overdue <= 21:
        return "second reminder + phone call to accounts payable"
    if days_overdue <= 45:
        return "final notice; consider pausing non-urgent work (keep life-safety call-outs going)"
    return "letter before action with statutory interest and compensation; consider small claims / collections"


def cashflow_forecast(opening: float, receivables: list[Invoice], payables: list[Invoice], today: date,
                      weeks: int, weekly_fixed_costs: float, collection_delay_days: int,
                      vat_payment: tuple[date, float] | None = None) -> list[dict[str, Any]]:
    rows = []
    balance = opening
    week_start = today
    for w in range(weeks):
        week_end = week_start + timedelta(days=6)

        def due_in_week(inv: Invoice, delay: int) -> bool:
            expected = max(inv.due_date + timedelta(days=delay), today)
            return week_start <= expected <= week_end

        receipts = sum(i.amount_due for i in receivables if i.amount_due > 0 and due_in_week(i, collection_delay_days))
        payments = sum(i.amount_due for i in payables if i.amount_due > 0 and due_in_week(i, 0))
        other = weekly_fixed_costs
        if vat_payment and week_start <= vat_payment[0] <= week_end:
            other += max(vat_payment[1], 0)
        balance += receipts - payments - other
        rows.append({"week_commencing": week_start.isoformat(), "receipts": _money(receipts),
                     "supplier_payments": _money(payments), "payroll_overheads_tax": _money(other),
                     "closing_balance": _money(balance)})
        week_start = week_end + timedelta(days=1)
    return rows


def average_days_to_pay(open_receivables: list[Invoice], today: date) -> int:
    """Rough collection delay: half the average lateness of invoices that are currently overdue."""
    lates = [(today - i.due_date).days for i in open_receivables if i.amount_due > 0 and i.due_date < today]
    if not lates:
        return 7
    return max(0, min(60, int(sum(lates) / len(lates) / 2)))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class Accountant:
    def __init__(self, settings: Settings, finance, fsm=None, staff=None):
        self.s = settings
        self.finance = finance
        self.fsm = fsm
        self.staff = staff

    @property
    def demo(self) -> bool:
        return getattr(self.finance, "demo", False)

    def _today(self) -> date:
        return date.today()

    async def aged(self, kind: str = "receivable") -> dict[str, Any]:
        invoices = await self.finance.invoices(kind, outstanding_only=True)
        return {"kind": kind, **age_invoices(invoices, self._today())}

    async def vat(self, quarter_offset: int = 0) -> dict[str, Any]:
        start, end, due = vat_quarter(self._today(), self.s.vat_quarter_months, quarter_offset)
        sales = await self.finance.invoices("receivable", outstanding_only=False, since=start - timedelta(days=1))
        purchases = await self.finance.invoices("payable", outstanding_only=False, since=start - timedelta(days=1))
        est = vat_estimate(sales, purchases, start, end)
        est["return_and_payment_due"] = due.isoformat()
        if self.s.vat_scheme == "cash":
            est["note"] += " Company uses cash accounting: the real figure depends on payment dates, not invoice dates."
        return est

    async def trailing_profit(self, months: int = 12) -> dict[str, Any]:
        since = _add_months(self._today(), -months)
        sales = await self.finance.invoices("receivable", outstanding_only=False, since=since)
        purchases = await self.finance.invoices("payable", outstanding_only=False, since=since)
        revenue = sum(i.total - i.tax for i in sales)
        costs = sum(i.total - i.tax for i in purchases)
        payroll = self.s.monthly_payroll_estimate * months
        overheads = self.s.monthly_overheads_estimate * months
        return {"from": since.isoformat(), "to": self._today().isoformat(), "revenue_net": _money(revenue),
                "bills_net": _money(costs), "payroll_estimate": _money(payroll), "overheads_estimate": _money(overheads),
                "profit_estimate": _money(revenue - costs - payroll - overheads),
                "note": "Built from sales invoices and supplier bills only, plus configured payroll/overhead "
                        "estimates. Depreciation, accruals, stock/WIP and journals are not included."}

    async def corporation_tax(self, profit: float | None = None) -> dict[str, Any]:
        basis = None
        if profit is None:
            basis = await self.trailing_profit(12)
            profit = basis["profit_estimate"]
        result = corporation_tax(profit, self.s.associated_companies)
        month, day = (int(x) for x in self.s.financial_year_end.split("-"))
        today = self._today()
        year_end = date(today.year, month, day)
        if year_end < today:
            year_end = date(today.year + 1, month, day)
        result["next_year_end"] = year_end.isoformat()
        result["payment_due"] = (_add_months(year_end, 9) + timedelta(days=1)).isoformat()
        result["ct600_due"] = _add_months(year_end, 12).isoformat()
        if basis:
            result["profit_basis"] = basis
        return result

    async def profit_and_loss(self, date_from: date, date_to: date) -> dict[str, Any]:
        report = await self.finance.profit_and_loss(date_from, date_to)
        if report:
            return {"source": self.finance.name, "report": report}
        sales = [i for i in await self.finance.invoices("receivable", outstanding_only=False, since=date_from)
                 if i.date <= date_to]
        bills = [i for i in await self.finance.invoices("payable", outstanding_only=False, since=date_from)
                 if i.date <= date_to]
        revenue = sum(i.total - i.tax for i in sales)
        costs = sum(i.total - i.tax for i in bills)
        return {"source": f"{self.finance.name} invoices", "from": date_from.isoformat(), "to": date_to.isoformat(),
                "revenue_net": _money(revenue), "supplier_costs_net": _money(costs),
                "gross_profit": _money(revenue - costs),
                "gross_margin_pct": round(100 * (revenue - costs) / revenue, 1) if revenue else None,
                "note": "Approximate: excludes payroll, depreciation and journals."}

    async def cashflow(self, weeks: int = 13) -> dict[str, Any]:
        today = self._today()
        banks = await self.finance.bank_balances()
        opening = sum(b.balance for b in banks)
        receivables = await self.finance.invoices("receivable", outstanding_only=True)
        payables = await self.finance.invoices("payable", outstanding_only=True)
        delay = average_days_to_pay(receivables, today)
        weekly_fixed = (self.s.monthly_payroll_estimate + self.s.monthly_overheads_estimate) * 12 / 52
        vat = await self.vat(0)
        rows = cashflow_forecast(opening, receivables, payables, today, weeks, weekly_fixed, delay,
                                 (date.fromisoformat(vat["return_and_payment_due"]), vat["box5_net_vat_payable"]))
        lowest = min(rows, key=lambda r: r["closing_balance"]) if rows else None
        return {"opening_cash": _money(opening), "assumed_collection_delay_days": delay,
                "weekly_payroll_and_overheads": _money(weekly_fixed), "weeks": rows,
                "lowest_point": lowest,
                "note": "Receipts assume customers pay their due date plus the typical delay. Set "
                        "MONTHLY_PAYROLL_ESTIMATE / MONTHLY_OVERHEADS_ESTIMATE for realistic outgoings."}

    async def credit_control(self) -> dict[str, Any]:
        aged = await self.aged("receivable")
        actions = []
        for inv in aged["overdue_invoices"]:
            claim = late_payment_claim(inv["amount_due"], inv["days_overdue"], self.s.boe_base_rate)
            actions.append({"invoice": inv["number"], "customer": inv["contact"], "amount_due": inv["amount_due"],
                            "days_overdue": inv["days_overdue"], "action": credit_control_stage(inv["days_overdue"]),
                            **(claim if inv["days_overdue"] > 30 else {})})
        return {"total_overdue": aged["total_overdue"], "actions": actions,
                "note": "Statutory interest/compensation only applies to business customers."}

    def deadlines(self) -> list[dict[str, str]]:
        today = self._today()
        items = []
        start, end, due = vat_quarter(today, self.s.vat_quarter_months, -1)
        if due < today:
            start, end, due = vat_quarter(today, self.s.vat_quarter_months, 0)
        items.append({"what": f"VAT return + payment (quarter {start:%d %b}–{end:%d %b %Y})", "due": due.isoformat()})
        nxt = _add_months(today.replace(day=1), 1 if today.day > 22 else 0).replace(day=22)
        items.append({"what": "PAYE/NIC payment to HMRC (electronic)", "due": nxt.isoformat()})
        cis = _add_months(today.replace(day=1), 1 if today.day > 19 else 0).replace(day=19)
        items.append({"what": "CIS monthly return (if registered as a contractor)", "due": cis.isoformat()})
        month, day = (int(x) for x in self.s.financial_year_end.split("-"))
        last_ye = date(today.year, month, day)
        if last_ye >= today:
            last_ye = date(today.year - 1, month, day)
        items.append({"what": "Corporation tax payment (last year end)",
                      "due": (_add_months(last_ye, 9) + timedelta(days=1)).isoformat()})
        items.append({"what": "Annual accounts to Companies House", "due": _add_months(last_ye, 9).isoformat()})
        items.append({"what": "CT600 corporation tax return", "due": _add_months(last_ye, 12).isoformat()})
        for it in items:
            it["days_left"] = (date.fromisoformat(it["due"]) - today).days
        return sorted(items, key=lambda x: x["due"])

    async def snapshot(self) -> dict[str, Any]:
        banks = await self.finance.bank_balances()
        debtors = await self.aged("receivable")
        creditors = await self.aged("payable")
        vat = await self.vat(0)
        since = self._today() - timedelta(days=90)
        sales_90 = sum(i.total for i in await self.finance.invoices("receivable", outstanding_only=False, since=since))
        debtor_days = round(debtors["total_outstanding"] / sales_90 * 90, 1) if sales_90 else None
        return {
            "source": self.finance.name, "demo": self.demo,
            "cash_at_bank": _money(sum(b.balance for b in banks)),
            "bank_accounts": [{"name": b.name, "balance": _money(b.balance)} for b in banks],
            "debtors_total": debtors["total_outstanding"], "debtors_overdue": debtors["total_overdue"],
            "debtors_overdue_count": debtors["overdue_count"], "debtor_days": debtor_days,
            "creditors_total": creditors["total_outstanding"], "creditors_overdue": creditors["total_overdue"],
            "vat_quarter_estimate": vat["box5_net_vat_payable"], "vat_due": vat["return_and_payment_due"],
            "top_debtors": debtors["top_contacts"][:5],
        }

    # ------------------------------------------------------------------ business health
    async def health_check(self, days: int = 90) -> dict[str, Any]:
        """Is the business productive and healthy? KPIs vs targets, with concrete improvement steps."""
        today = self._today()
        start, prev_start = today - timedelta(days=days), today - timedelta(days=2 * days)
        sales_all = await self.finance.invoices("receivable", outstanding_only=False, since=prev_start)
        bills_all = await self.finance.invoices("payable", outstanding_only=False, since=prev_start)
        sales = [i for i in sales_all if i.date > start]
        prev_sales = [i for i in sales_all if prev_start < i.date <= start]
        bills = [i for i in bills_all if i.date > start]
        revenue = sum(i.total - i.tax for i in sales)
        prev_revenue = sum(i.total - i.tax for i in prev_sales)
        bill_costs = sum(i.total - i.tax for i in bills)
        payroll = self.s.monthly_payroll_estimate * days / 30.4
        gross_margin = 100 * (revenue - bill_costs) / revenue if revenue else None
        debtors = await self.aged("receivable")
        debtor_days = debtors["total_outstanding"] / (sum(i.total for i in sales) / days) if sales else None
        overdue_pct = 100 * debtors["total_overdue"] / debtors["total_outstanding"] if debtors["total_outstanding"] else 0
        cash = sum(b.balance for b in await self.finance.bank_balances())
        monthly_out = (bill_costs * 1.2 / days * 30.4) + self.s.monthly_payroll_estimate + self.s.monthly_overheads_estimate
        runway = cash / monthly_out if monthly_out else None

        kpis: dict[str, Any] = {
            "period": f"last {days} days", "revenue_net": _money(revenue), "previous_period_revenue_net": _money(prev_revenue),
            "revenue_growth_pct": round(100 * (revenue - prev_revenue) / prev_revenue, 1) if prev_revenue else None,
            "gross_margin_pct": round(gross_margin, 1) if gross_margin is not None else None,
            "gross_margin_note": "sales less supplier bills" + ("" if payroll else "; payroll not included (set MONTHLY_PAYROLL_ESTIMATE)"),
            "net_margin_pct_estimate": round(100 * (revenue - bill_costs - payroll - self.s.monthly_overheads_estimate * days / 30.4) / revenue, 1)
                                       if revenue and (payroll or self.s.monthly_overheads_estimate) else None,
            "debtor_days": round(debtor_days, 1) if debtor_days is not None else None,
            "overdue_pct_of_debtors": round(overdue_pct, 1), "overdue_debt": debtors["total_overdue"],
            "cash_at_bank": _money(cash), "cash_runway_months": round(runway, 1) if runway else None,
        }

        if self.fsm is not None:
            try:
                contracts = await self.fsm.contracts()
                recurring = sum(float(c.get("annual_value") or 0) for c in contracts
                                if str(c.get("status") or "active").lower() in ("active", "live", ""))
                lapsed = [c for c in contracts if str(c.get("renewal_date") or "9999")[:10] < today.isoformat()]
                annualised_revenue = revenue * 365 / days if revenue else 0
                kpis["recurring_contract_value_annual"] = _money(recurring)
                kpis["recurring_revenue_pct"] = round(100 * recurring / annualised_revenue, 1) if annualised_revenue else None
                kpis["contracts_past_renewal"] = len(lapsed)
                kpis["revenue_at_risk_from_lapsed_contracts"] = _money(sum(float(c.get("annual_value") or 0) for c in lapsed))
                quotes = [q for q in await self.fsm.quotes() if str(q.get("sent_date") or "")[:10] >= start.isoformat()]
                won = [q for q in quotes if str(q.get("status")).lower() in ("accepted", "won", "approved")]
                lost = [q for q in quotes if str(q.get("status")).lower() in ("declined", "lost", "rejected")]
                open_q = [q for q in quotes if q not in won and q not in lost]
                kpis["quote_conversion_pct"] = round(100 * len(won) / (len(won) + len(lost)), 1) if won or lost else None
                kpis["open_quote_pipeline"] = _money(sum(float(q.get("value") or 0) for q in open_q))
                kpis["open_quotes"] = len(open_q)
                jobs = await self.fsm.jobs(start, today)
                done_value = sum(float(j.get("value") or 0) for j in jobs
                                 if str(j.get("status") or "").lower() in ("completed", "complete", "done", "closed"))
                kpis["fsm_completed_work_value"] = _money(done_value)
                kpis["possible_unbilled_work"] = _money(max(done_value - revenue, 0))
            except Exception as e:  # noqa: BLE001
                kpis["fsm_error"] = str(e)[:200]
        if self.staff is not None:
            try:
                prod = await self.staff.productivity(min(days, 90))
                kpis["engineer_utilisation_pct"] = prod["team"].get("utilisation_pct")
                engineers = max(len(prod["engineers"]), 1)
                kpis["revenue_per_engineer_per_month"] = _money(prod["team"]["total_revenue"] / engineers * 30.4 / min(days, 90))
            except Exception as e:  # noqa: BLE001
                kpis["staff_error"] = str(e)[:200]

        findings = self._findings(kpis)
        return {"demo": self.demo, "kpis": kpis, "targets": self._targets(),
                "findings": findings, "score": f"{sum(f['status'] == 'good' for f in findings)}/{len(findings)} on target",
                "note": "Management estimates from Sage/CSV invoices and Salts FSM - not statutory accounts."}

    def _targets(self) -> dict[str, float]:
        return {k.removeprefix("target_"): v for k, v in self.s.model_dump().items() if k.startswith("target_")}

    def _findings(self, k: dict[str, Any]) -> list[dict[str, Any]]:
        t = self.s
        rules = [
            ("Revenue growth", k.get("revenue_growth_pct"), t.target_revenue_growth_pct, "min",
             ["Contact every customer with an open remedial from their last service report - these are pre-qualified sales.",
              "Offer multi-system maintenance bundles (fire alarm + emergency lighting + extinguishers) to existing sites.",
              "Chase every quote over 7 days old this week; ask for the decision date."]),
            ("Gross margin", k.get("gross_margin_pct"), t.target_gross_margin_pct, "min",
             ["Review labour rate and materials mark-up against current supplier prices (panels and detectors have risen).",
              "Compare quoted vs actual hours on the last 20 installs to find jobs that overran.",
              "Stop giving remedial work away on service visits - quote it separately."]),
            ("Debtor days", k.get("debtor_days"), t.target_debtor_days, "max",
             ["Invoice within 48 hours of job completion and put a payment link on every invoice.",
              "Weekly credit-control run: reminder at 7 days overdue, call at 14, final notice at 30.",
              "Move repeat late payers to pro-forma or card-on-file for non-contract work."]),
            ("Overdue debt share", k.get("overdue_pct_of_debtors"), t.target_overdue_pct, "max",
             ["Work the credit-control list oldest first and add statutory late-payment interest on business debts over 30 days.",
              "Pause non-urgent work for accounts over 60 days (keep life-safety call-outs going)."]),
            ("Cash runway (months)", k.get("cash_runway_months"), t.target_cash_runway_months, "min",
             ["Build a VAT/corporation-tax reserve account and sweep the estimate into it monthly.",
              "Ask for stage payments / deposits on installation projects over £5k.",
              "Review direct debits and subscriptions for savings."]),
            ("Recurring contract revenue %", k.get("recurring_revenue_pct"), t.target_recurring_revenue_pct, "min",
             ["Offer a maintenance contract at handover of every new installation.",
              "Price renewals with an annual uplift and renew 30+ days before the renewal date.",
              "Win back lapsed contracts - call each customer whose renewal date has passed."]),
            ("Quote win rate", k.get("quote_conversion_pct"), t.target_quote_conversion_pct, "min",
             ["Follow up every quote within 7 days and again at 21 days.",
              "Offer good/better/best options on larger quotes.",
              "Review lost quotes for price vs response-time reasons."]),
            ("Engineer utilisation", k.get("engineer_utilisation_pct"), t.target_utilisation_pct, "min",
             ["Cluster jobs geographically when scheduling to cut travel time.",
              "Keep a list of PPM visits that can fill gaps when a job cancels.",
              "Make sure engineers log start/finish times in Salts FSM so utilisation is measured accurately."]),
        ]
        out = []
        for name, value, target, direction, actions in rules:
            if value is None:
                out.append({"kpi": name, "status": "not measured", "value": None, "target": target, "actions": []})
                continue
            good = value >= target if direction == "min" else value <= target
            out.append({"kpi": name, "value": value, "target": target, "status": "good" if good else "needs attention",
                        "actions": [] if good else actions})
        if k.get("possible_unbilled_work", 0) > 500:
            out.append({"kpi": "Unbilled completed work", "value": k["possible_unbilled_work"], "target": 0,
                        "status": "needs attention",
                        "actions": ["Reconcile completed jobs in Salts FSM against invoices in Sage and bill anything missing.",
                                    "Set a rule: no job is closed in the FSM until it's invoiced."]})
        if k.get("contracts_past_renewal"):
            out.append({"kpi": "Lapsed contracts", "value": k["contracts_past_renewal"], "target": 0,
                        "status": "needs attention",
                        "actions": [f"Renew or re-quote the {k['contracts_past_renewal']} contracts past their renewal "
                                    f"date (about £{k.get('revenue_at_risk_from_lapsed_contracts', 0):,.0f} a year at risk)."]})
        return out
