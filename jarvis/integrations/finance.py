"""Accounting data sources: Sage Accounting (cloud API), CSV exports (e.g. Sage 50), or demo data."""

from __future__ import annotations

import asyncio
import csv
import logging
import random
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from ..db import Database

log = logging.getLogger(__name__)


@dataclass
class Invoice:
    kind: str  # "receivable" (sales) | "payable" (bills)
    number: str
    contact: str
    date: date
    due_date: date
    total: float
    tax: float
    amount_due: float
    status: str  # draft | authorised | paid | voided

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["date"], d["due_date"] = self.date.isoformat(), self.due_date.isoformat()
        return d


@dataclass
class BankAccount:
    name: str
    balance: float


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y"):
        try:
            return datetime.strptime(value[:19] if "T" in value else value, fmt).date()
        except ValueError:
            continue
    log.warning("Unrecognised date %r", value)
    return None


# ---------------------------------------------------------------------------
# Sage Accounting (cloud) API v3.1
# ---------------------------------------------------------------------------

class SageFinance:
    """Sage Accounting (Sage Business Cloud) API v3.1, read-only.

    Sage uses the OAuth2 authorisation-code flow only. The owner connects once from
    the display (/auth/sage/start); access tokens last 5 minutes and the refresh
    token rotates on every use, so the latest one is persisted in Jarvis' DB.
    """

    name = "sage"
    demo = False
    AUTH_URL = "https://www.sageone.com/oauth2/auth/central"
    TOKEN_URL = "https://oauth.accounting.sage.com/token"
    API = "https://api.accounting.sage.com/v3.1"
    KV_REFRESH = "sage_refresh_token"

    def __init__(self, settings: Settings, http: httpx.AsyncClient, db: Database):
        self.s = settings
        self.http = http
        self.db = db
        self._token: str | None = None
        self._expires = datetime.min
        self._lock = asyncio.Lock()

    # -- OAuth ------------------------------------------------------------------
    def authorize_url(self, redirect_uri: str, state: str) -> str:
        from urllib.parse import urlencode

        return self.AUTH_URL + "?" + urlencode({
            "filter": "apiv3.1", "response_type": "code", "client_id": self.s.sage_client_id,
            "redirect_uri": redirect_uri, "scope": "full_access" if self.s.sage_write_enabled else "readonly",
            "state": state})

    async def exchange_code(self, code: str, redirect_uri: str) -> None:
        await self._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})

    async def _token_request(self, data: dict[str, str]) -> None:
        r = await self.http.post(self.TOKEN_URL, data={**data, "client_id": self.s.sage_client_id,
                                                       "client_secret": self.s.sage_client_secret},
                                 headers={"Accept": "application/json"})
        r.raise_for_status()
        tok = r.json()
        self._token = tok["access_token"]
        self._expires = datetime.utcnow() + timedelta(seconds=int(tok.get("expires_in", 300)) - 30)
        if tok.get("refresh_token"):
            self.db.set_kv(self.KV_REFRESH, tok["refresh_token"])

    @property
    def connected(self) -> bool:
        return bool(self.db.get_kv(self.KV_REFRESH))

    async def _headers(self) -> dict[str, str]:
        async with self._lock:  # refresh tokens are single-use; never refresh twice in parallel
            if not self._token or datetime.utcnow() >= self._expires:
                refresh = self.db.get_kv(self.KV_REFRESH)
                if not refresh:
                    raise RuntimeError("Sage is not connected yet - open the display and choose Connect Sage.")
                await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh})
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if self.s.sage_business_id:
            headers["X-Business"] = self.s.sage_business_id
        return headers

    async def _get_all(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        url: str | None = f"{self.API}{path}"
        query: dict[str, Any] | None = {**params, "attributes": "all", "items_per_page": 200}
        for _ in range(100):
            r = await self.http.get(url, params=query, headers=await self._headers(), timeout=60)
            r.raise_for_status()
            data = r.json()
            items.extend(data.get("$items", []))
            nxt = data.get("$next")
            if not nxt:
                break
            url, query = (nxt if nxt.startswith("http") else f"{self.API}{nxt}"), None
        return items

    @staticmethod
    def _invoice(kind: str, row: dict[str, Any]) -> Invoice:
        status = ((row.get("status") or {}).get("id") or "").lower()
        status = {"void": "voided", "unpaid": "authorised", "part_paid": "authorised"}.get(status, status)
        number = row.get("invoice_number") or row.get("vendor_reference") or row.get("reference") or row.get("displayed_as", "")
        contact = row.get("contact_name") or (row.get("contact") or {}).get("displayed_as", "")
        return Invoice(kind=kind, number=str(number), contact=contact,
                       date=_parse_date(row.get("date")) or date.today(),
                       due_date=_parse_date(row.get("due_date")) or _parse_date(row.get("date")) or date.today(),
                       total=float(row.get("total_amount") or 0), tax=float(row.get("tax_amount") or 0),
                       amount_due=float(row.get("outstanding_amount") or 0), status=status)

    async def invoices(self, kind: str, outstanding_only: bool = True, since: date | None = None) -> list[Invoice]:
        path = "/sales_invoices" if kind == "receivable" else "/purchase_invoices"
        since = since or (date.today() - timedelta(days=3 * 365 if outstanding_only else 365))
        rows = await self._get_all(path, {"from_date": since.isoformat()})
        out = [self._invoice(kind, r) for r in rows]
        out = [i for i in out if i.status not in ("voided", "draft", "deleted")]
        return [i for i in out if i.amount_due > 0] if outstanding_only else out

    async def bank_balances(self) -> list[BankAccount]:
        rows = await self._get_all("/bank_accounts", {})
        return [BankAccount(r.get("displayed_as") or (r.get("bank_account_details") or {}).get("account_name", "Bank"),
                            float(r.get("balance") or 0)) for r in rows]

    async def profit_and_loss(self, date_from: date, date_to: date) -> dict[str, Any] | None:
        return None  # v3.1 has no P&L report endpoint; the accountant builds one from invoices

    async def create_sales_invoice(self, *, customer: str, reference: str, net: float, description: str) -> str:
        """Create a sales invoice (only called after the owner approves it on the display)."""
        if not self.s.sage_write_enabled:
            raise RuntimeError("Sage write access is off (SAGE_WRITE_ENABLED=false)")
        headers = await self._headers()
        contacts = await self._get_all("/contacts", {"search": customer, "contact_type_id": "CUSTOMER"})
        contact = next((c for c in contacts if (c.get("name") or c.get("displayed_as", "")).lower() == customer.lower()),
                       contacts[0] if len(contacts) == 1 else None)
        if not contact:
            raise RuntimeError(f"No single Sage customer matches '{customer}'")
        ledgers = await self._get_all("/ledger_accounts", {"search": self.s.sage_sales_nominal_code})
        ledger = next((l for l in ledgers if str(l.get("nominal_code")) == self.s.sage_sales_nominal_code), None)
        if not ledger:
            raise RuntimeError(f"Sales ledger account {self.s.sage_sales_nominal_code} not found in Sage")
        body = {"sales_invoice": {
            "contact_id": contact["id"], "date": date.today().isoformat(), "reference": reference,
            "invoice_lines": [{"description": description, "ledger_account_id": ledger["id"], "quantity": 1,
                               "unit_price": round(net, 2), "tax_rate_id": self.s.sage_default_tax_rate}]}}
        r = await self.http.post(f"{self.API}/sales_invoices", json=body, headers=headers, timeout=60)
        r.raise_for_status()
        return r.json().get("invoice_number") or r.json().get("displayed_as") or "created"

    async def check(self) -> str:
        r = await self.http.get(f"{self.API}/businesses", headers=await self._headers(), timeout=30)
        r.raise_for_status()
        return "Sage Accounting connected"


# ---------------------------------------------------------------------------
# CSV (export from any accounting package)
# ---------------------------------------------------------------------------

class CsvFinance:
    """Reads CSV exports - e.g. from Sage 50 Accounts - dropped into FINANCE_CSV_DIR.

    invoices.csv  one row per invoice/bill. Recognised headers (case-insensitive):
                  kind/type (sales|purchase, SI|PI|receivable|payable), number/invoice no/ref,
                  contact/name/customer/supplier/a/c, date, due date, net, tax/vat, total/gross,
                  outstanding/amount due/balance/o/s, status
    bank.csv      account/name, balance
    Or sales_invoices.csv + purchase_invoices.csv (then no kind column is needed).
    """

    name = "csv"
    demo = False
    COLUMNS = {
        "kind": ("kind", "type", "tran type", "transaction type"),
        "number": ("number", "invoice no", "invoice number", "inv no", "ref", "reference", "no."),
        "contact": ("contact", "name", "customer", "supplier", "account name", "a/c name", "a/c"),
        "date": ("date", "invoice date"),
        "due_date": ("due_date", "due date", "due"),
        "net": ("net", "net amount"),
        "tax": ("tax", "vat", "tax amount", "vat amount"),
        "total": ("total", "gross", "gross amount", "amount", "invoice total"),
        "amount_due": ("amount_due", "amount due", "outstanding", "o/s", "balance", "outstanding amount"),
        "status": ("status",),
    }

    def __init__(self, directory: Path):
        self.dir = directory

    def _rows(self, name: str) -> list[dict[str, str]]:
        path = self.dir / name
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8-sig") as f:
            return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]

    def _col(self, row: dict[str, str], field: str) -> str:
        for alias in self.COLUMNS[field]:
            if row.get(alias):
                return row[alias]
        return ""

    @staticmethod
    def _num(value: str) -> float:
        value = value.replace("£", "").replace(",", "").strip()
        if value.startswith("(") and value.endswith(")"):
            value = "-" + value[1:-1]
        return float(value) if value else 0.0

    def _kind(self, row: dict[str, str], default: str | None) -> str | None:
        k = self._col(row, "kind").lower()
        if k in ("receivable", "sales", "sale", "si", "sales invoice", "accrec"):
            return "receivable"
        if k in ("payable", "purchase", "purchases", "pi", "purchase invoice", "bill", "accpay"):
            return "payable"
        return default

    def _all(self) -> list[Invoice]:
        sources = [("invoices.csv", None), ("sales_invoices.csv", "receivable"), ("purchase_invoices.csv", "payable")]
        out = []
        for filename, default_kind in sources:
            for row in self._rows(filename):
                kind = self._kind(row, default_kind)
                if not kind:
                    continue
                inv_date = _parse_date(self._col(row, "date"))
                if not inv_date:
                    continue
                tax = self._num(self._col(row, "tax"))
                total = self._num(self._col(row, "total")) or self._num(self._col(row, "net")) + tax
                due = self._col(row, "amount_due")
                out.append(Invoice(kind=kind, number=self._col(row, "number"), contact=self._col(row, "contact"),
                                   date=inv_date, due_date=_parse_date(self._col(row, "due_date")) or inv_date + timedelta(days=30),
                                   total=total, tax=tax, amount_due=self._num(due) if due else total,
                                   status=(self._col(row, "status") or "authorised").lower()))
        return out

    async def invoices(self, kind: str, outstanding_only: bool = True, since: date | None = None) -> list[Invoice]:
        return [i for i in self._all() if i.kind == kind and (not outstanding_only or i.amount_due > 0)
                and (not since or i.date >= since)]

    async def bank_balances(self) -> list[BankAccount]:
        return [BankAccount(r.get("account") or r.get("name") or "Bank", self._num(r.get("balance", "0")))
                for r in self._rows("bank.csv")]

    async def profit_and_loss(self, date_from: date, date_to: date) -> dict[str, Any] | None:
        return None

    async def check(self) -> str:
        files = [f.name for f in self.dir.glob("*.csv")] if self.dir.exists() else []
        if not files:
            raise RuntimeError(f"No CSV exports found in {self.dir}")
        return f"CSV finance data: {', '.join(sorted(files))}"


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

class DemoFinance:
    name = "demo"
    demo = True

    def __init__(self, today: date | None = None):
        self.today = today or date.today()
        rng = random.Random(20260928)
        customers = ["Bradford Council", "Aire Valley Care Ltd", "Pennine Housing", "Kestrel Retail",
                     "Baildon Health Partnership", "Moorside Logistics", "Saltaire Estates", "Wharfe Hospitality",
                     "Wharfedale Academy Trust"]
        suppliers = ["Fire Alarm Wholesale Ltd", "Security Distribution UK", "Van Leasing Co", "Fuel Card Services",
                     "Office Landlord", "Cable & Fixings Direct"]
        self._invoices: list[Invoice] = []
        typical = {c: rng.choice([450, 760, 1250, 1600]) for c in customers}
        for n in range(240):
            d = self.today - timedelta(days=int(n / 240 * 200))
            contact = customers[n % len(customers)]
            net = round(typical[contact] * rng.uniform(0.85, 1.15))
            if contact == "Kestrel Retail" and (self.today - d).days <= 90:  # demo story: a customer going quiet
                contact = "Bradford Council"
            paid = d < self.today - timedelta(days=rng.randint(25, 75))
            total = round(net * 1.2, 2)
            self._invoices.append(Invoice("receivable", f"INV-{10400 + n}", contact, d,
                                          d + timedelta(days=30), total, round(net * 0.2, 2),
                                          0.0 if paid else total, "paid" if paid else "authorised"))
        late = self.today - timedelta(days=105)  # demo story: Kestrel sitting on an old invoice
        self._invoices.append(Invoice("receivable", "INV-10388", "Kestrel Retail", late, late + timedelta(days=30),
                                      4056.0, 676.0, 4056.0, "authorised"))
        for n in range(45):
            d = self.today - timedelta(days=rng.randint(0, 200))
            net = rng.choice([95, 180, 420, 650, 1200, 2100, 3400])
            paid = d < self.today - timedelta(days=rng.randint(20, 50))
            total = round(net * 1.2, 2)
            self._invoices.append(Invoice("payable", f"BILL-{700 + n}", rng.choice(suppliers), d,
                                          d + timedelta(days=30), total, round(net * 0.2, 2),
                                          0.0 if paid else total, "paid" if paid else "authorised"))

    async def invoices(self, kind: str, outstanding_only: bool = True, since: date | None = None) -> list[Invoice]:
        return [i for i in self._invoices if i.kind == kind and (not outstanding_only or i.amount_due > 0)
                and (not since or i.date >= since)]

    async def bank_balances(self) -> list[BankAccount]:
        return [BankAccount("Business Current Account", 48213.55), BankAccount("Business Reserve", 25000.00)]

    async def profit_and_loss(self, date_from: date, date_to: date) -> dict[str, Any] | None:
        return None

    async def check(self) -> str:
        return "demo finance"


def build_finance(settings: Settings, http: httpx.AsyncClient, db: Database):
    provider = settings.effective_finance
    if provider == "sage":
        return SageFinance(settings, http, db)
    if provider == "csv":
        return CsvFinance(settings.finance_csv_dir)
    return DemoFinance()
