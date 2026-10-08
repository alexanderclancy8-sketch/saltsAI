"""``customer_balance``: what ONE customer owes - for the office answering a customer who rings about their account.

The owner's decision (2026-10-08): office staff (the ``office`` kind of team member, see ``jarvis/access.py``) may be told, for
ONE customer at a time, exactly three things and nothing else:

* the total currently owed,
* the total overdue,
* the oldest overdue invoice: its number, due date, days overdue and what is still outstanding on it.

Not invoice lists, payments, credit notes, other customers' figures, company-wide finance (cash, debtors across customers,
margins, costing, supplier prices), staff pay or anything else in the FSM's finance / people groups. The owner and managers may
use it too (it is a subset of what they can already see); an engineer may not (``access.OFFICE_EXTRA_TOOLS``).

How that is enforced here, server-side, whatever the model asks for:

* This is a DEDICATED read path. It never goes through ``fsm_data`` / ``fsm_analyse`` (whose finance resources stay owner-only,
  unchanged) and it asks the FSM only for the fields it needs: customers ``id,name,account_ref,billing_address`` (the address only
  to name a town when two customers are alike - it is never returned), sites ``id,name,customer_id,postcode,invoice_to_site``
  and invoices ``id,invoice_no,due_at,outstanding,status,site_id,bill_to``, always filtered to the one customer. The invoice rows
  are aggregated page by page and dropped; the answer carries the three figures and one invoice, never a row.
* The customer is resolved by its FSM id. A name or account reference is searched; an exact account reference or an exact name
  (or a single search hit whose name / reference contains what was asked) is that customer. Anything else - two or more alike, or
  a hit that only matched on a contact's phone or email - is NOT guessed: the tool returns the candidates (name, town and account
  reference only, at most ``MAX_CANDIDATES``) and asks which one; the model calls again with ``customer_id``.
* Money is ``Decimal`` from the FSM's own ``outstanding`` field (its balance rule: gross total less credit grossed up less
  payments, zero when unissued or cancelled - the same as its Invoicing screen). "Overdue" is outstanding on an invoice whose due
  date is before today (the company's today, injectable - nothing here reads the clock at import).
* An office caller is rate-limited per session (``OFFICE_LOOKUPS_PER_HOUR``), and every lookup that reaches a customer is written
  to "What Jarvis did" with the customer, who asked and their role - never a figure.
* Sample (demo) FSM data is not an answer: the tool says so and gives nothing. A finance (or customers) group switched off in the
  FSM's Jarvis access ("scope off") is a plain message.

Sites invoiced to the site ("Invoices go to this site", the FSM's Sites and Trusts rule): an invoice addressed to a site still
carries the CUSTOMER's id (the customer stays the account; the invoice's ``bill_to`` is ``site``). So:

* asked about a customer -> the customer's whole account, every invoice with its id, including any addressed to its sites (the
  answer says when some are, so the office can ask which site is calling);
* asked about one of its sites (``site``) that IS invoiced to the site -> that site's own account: only the customer's invoices
  for that site that are addressed to the site;
* asked about a site that is NOT invoiced to the site -> its invoices go to the customer, so the answer is the customer's account
  and says so.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .. import access
from ..events import quiet_turn
from ..integrations.fsm_data import FsmDataError, ScanStats, clean_text

log = logging.getLogger(__name__)

OFFICE_LOOKUPS_PER_HOUR = 30      # per office session
MAX_CANDIDATES = 8                # customers (or sites) offered when the name is ambiguous
SEARCH_ROWS = 25                  # rows one customer / site search reads
CUSTOMER_FIELDS = ("id", "name", "account_ref", "billing_address")
SITE_FIELDS = ("id", "name", "customer_id", "postcode", "invoice_to_site")
INVOICE_FIELDS = ("id", "invoice_no", "due_at", "outstanding", "status", "site_id", "bill_to")
INVOICE_NEEDS = ("invoice_no", "due_at", "outstanding")   # without these there is no honest answer
TWO_PLACES = Decimal("0.01")

DEMO_MESSAGE = ("Salts FSM isn't connected - it is showing sample data - so there is no real account to look up. Tell the caller "
                "you can't see their account right now; don't quote any figures.")
HANDLING = ("Say these figures only to the person you are talking to, about this one customer, in this chat. Never give another "
            "customer's figures, invoice lists, payments or company finances. Payment arrangements, disputes or anything that "
            "needs a decision go to the owner.")
_POSTCODE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$", re.I)


class Refused(Exception):
    """A plain answer instead of figures (scope off, nothing found, rate limited...)."""

    def __init__(self, message: str, kind: str, **extra: Any) -> None:
        super().__init__(message)
        self.out = {"error": message, "kind": kind, **extra}


def _norm(text: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower()).split())


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).replace(",", "").replace("£", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _day(value: Any) -> date | None:
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _gbp(d: Decimal) -> str:
    return str(d.quantize(TWO_PLACES))


_NOT_TOWNS = {"uk", "united kingdom", "england", "west yorkshire", "north yorkshire", "south yorkshire", "east yorkshire",
              "yorkshire", "lancashire", "greater manchester"}
_STREETISH = {"street", "st", "road", "rd", "lane", "ln", "avenue", "ave", "way", "close", "drive", "court", "place", "row",
              "terrace", "crescent", "grove", "park", "estate", "house", "mill", "unit", "yard", "walk", "gardens", "square", "hill"}
_TRAILING_POSTCODE = re.compile(r"[\s,]*\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\s*$", re.I)


def town_of(address: Any) -> str:
    """The town of a billing address, for telling two customers apart. The FSM's address arrives as one cleaned line (its line
    breaks become spaces), so: drop the postcode and any county, then take the last comma part - or, with no commas, the last
    word if it does not look like part of a street. "" when there is no telling."""
    text = _TRAILING_POSTCODE.sub("", str(address or "")).strip(" ,")
    parts = [p.strip() for p in re.split(r"[\n,]+", text) if p.strip()]
    while parts and (parts[-1].lower() in _NOT_TOWNS or _POSTCODE.match(parts[-1])):
        parts.pop()
    if not parts:
        return ""
    last = parts[-1]
    for county in sorted(_NOT_TOWNS, key=len, reverse=True):
        if last.lower().endswith(" " + county):
            last = last[: -len(county) - 1].strip()
            break
    if len(parts) >= 2 and not re.match(r"^\d", last):
        return clean_text(last, 40)
    word = last.split()[-1] if last.split() else ""
    return clean_text(word, 40) if word.isalpha() and word.lower() not in _STREETISH else ""


def _outward(postcode: Any) -> str:
    text = str(postcode or "").strip().upper()
    return text.split()[0] if " " in text else text[:-3].strip() if len(text) > 4 else text


class CustomerBalance:
    def __init__(self, j: Any, clock: Callable[[], float] = time.monotonic, today: Callable[[], date] | None = None) -> None:
        self.j = j
        self._clock = clock
        self._today = today
        self._lookups: dict[str, deque] = {}     # office session id -> times of recent lookups

    # ------------------------------------------------------------------ helpers
    def today(self) -> date:
        """The company's today. Injectable for tests; read when a question is asked, never at import."""
        if self._today is not None:
            return self._today()
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(self.j.settings.timezone)).date()
        except Exception:  # noqa: BLE001 - an unknown time zone name must not stop a lookup
            return datetime.now().date()

    @property
    def client(self):
        return self.j.fsm_data

    @staticmethod
    def _who(j: Any, caller: access.Caller | None) -> tuple[str, str]:
        """(who asked, their role) for the audit line."""
        if caller is not None:
            return (caller.name if caller.role == access.MANAGER and caller.name else caller.label), caller.role_label
        asked = str(getattr(j, "asked_by", "") or "")
        return (asked or ("automation" if quiet_turn.get() else "the owner")), access.ROLE_LABEL[access.OWNER]

    def _rate_limit(self, caller: access.Caller | None) -> None:
        """An office session may look up OFFICE_LOOKUPS_PER_HOUR balances an hour (the owner and managers are not limited)."""
        if caller is None or not caller.is_team:
            return
        key = caller.sid or caller.requester
        window, now = self._lookups.setdefault(key, deque()), self._clock()
        while window and now - window[0] >= 3600:
            window.popleft()
        if len(window) >= OFFICE_LOOKUPS_PER_HOUR:
            wait = max(1, round((3600 - (now - window[0])) / 60))
            raise Refused(f"That's {OFFICE_LOOKUPS_PER_HOUR} account look-ups in the last hour, which is the limit for the office "
                          f"console. Try again in about {wait} minute{'' if wait == 1 else 's'}, or ask the owner.", "rate_limited")
        window.append(now)

    def _audit(self, caller: access.Caller | None, customer: dict[str, Any], site: dict[str, Any] | None) -> None:
        """One line in 'What Jarvis did': which customer, who asked and their role. Never a figure. Never raises."""
        who, role = self._who(self.j, caller)
        what = (f"Looked up the account balance of {customer['name']} (FSM customer {customer['id']})"
                + (f", site {site['name']}" if site else "") + f" for {who} - role {role}")
        try:
            self.j.activity_feed.record("balance_lookup", who, what, f"customer {customer['id']}")
        except Exception:  # noqa: BLE001
            log.exception("Could not record a balance lookup")

    async def _catalog(self):
        try:
            cat = await self.client.catalog()
        except FsmDataError as e:
            raise Refused(e.message, e.kind) from None
        for name in ("customers", "invoices"):
            res = cat.resources.get(name)
            if res is None:
                raise Refused("The FSM doesn't let Jarvis read customer accounts yet, so I can't see a balance.", "unavailable")
            group = cat.groups.get(res.group)
            if group is not None and not group.enabled:
                raise self._scope_off(res.group)
        inv = cat.resources["invoices"]
        missing = [f for f in INVOICE_NEEDS if f not in inv.field_names] + ([] if "customer_id" in inv.filters else ["customer_id"])
        if missing:
            raise Refused("The FSM's invoices don't give Jarvis what it needs for a balance (" + ", ".join(missing) + ").",
                          "unavailable")
        return cat

    @staticmethod
    def _scope_off(group: str | None) -> Refused:
        if group == "finance" or not group:
            return Refused("Account balances are switched off in the FSM for Jarvis (its finance access is off), so I can't see "
                           "what this customer owes. The owner can switch it on in the FSM's Jarvis access settings.", "scope_off",
                           group=group or "finance")
        return Refused(f"Customer records are switched off in the FSM for Jarvis (the '{group}' group is off), so I can't look up an "
                       "account. The owner can switch it on in the FSM's Jarvis access settings.", "scope_off", group=group)

    async def _fetch(self, resource: str, **kw) -> list[dict[str, Any]]:
        try:
            return (await self.client.fetch(resource, **kw)).items
        except FsmDataError as e:
            if e.kind == "scope_off":
                raise self._scope_off(e.group) from None
            raise Refused(e.message, e.kind) from None

    @staticmethod
    def _fields(res, wanted: tuple[str, ...]) -> list[str]:
        names = res.field_names
        return [f for f in wanted if not names or f in names]

    # ------------------------------------------------------------------ resolving the customer
    async def _customer(self, cat, customer: str, customer_id: str | None) -> dict[str, Any]:
        res = cat.resources["customers"]
        fields = self._fields(res, CUSTOMER_FIELDS)
        if customer_id:
            if "id" not in res.filters:
                raise Refused("The FSM won't let Jarvis look a customer up by id.", "unavailable")
            rows = await self._fetch("customers", filters={"id": clean_text(customer_id, 80)}, fields=fields, limit=2, max_rows=2)
            rows = [r for r in rows if str(r.get("id")) == str(customer_id).strip()]
            if len(rows) != 1:
                raise Refused("There's no customer with that id in the FSM. Ask the caller for the account name or reference again.",
                              "not_found")
            return rows[0]
        wanted = _norm(customer)
        if not wanted:
            raise Refused("Which customer? Give me their name or account reference.", "bad_request")
        rows = await self._fetch("customers", q=clean_text(customer, 100), fields=fields, limit=SEARCH_ROWS, max_rows=SEARCH_ROWS)
        by_ref = [r for r in rows if wanted and _norm(r.get("account_ref")) == wanted]
        by_name = [r for r in rows if _norm(r.get("name")) == wanted]
        for exact in (by_ref, by_name):
            if len(exact) == 1:
                return exact[0]
        if len(rows) == 1 and not by_ref and not by_name:
            only = rows[0]
            if wanted in _norm(only.get("name")) or (wanted and wanted in _norm(only.get("account_ref"))):
                return only
        if not rows:
            raise Refused(f"I can't find a customer called '{clean_text(customer, 60)}' in the FSM. Ask the caller for the exact "
                          "account name or their account reference.", "not_found")
        pool = by_ref or by_name or rows
        candidates = [{"customer_id": str(r.get("id")), "name": r.get("name") or "", "town": town_of(r.get("billing_address")),
                       "account_ref": r.get("account_ref") or ""} for r in pool[:MAX_CANDIDATES]]
        more = len(pool) > MAX_CANDIDATES
        raise Refused("More than one customer could be meant, so I haven't picked one. Ask the caller which of these they are "
                      "(by name, town or account reference), then ask again with that customer_id. Don't read out more of this "
                      "list than you need to." + (" There are more matches than these - a fuller name or the account reference "
                                                  "will narrow it." if more else ""),
                      "ambiguous", candidates=candidates)

    async def _site(self, cat, customer: dict[str, Any], site: str) -> dict[str, Any]:
        res = cat.resources.get("sites")
        if res is None or "customer_id" not in res.filters:
            raise Refused("The FSM doesn't let Jarvis read sites, so I can only give the customer's whole account.", "unavailable")
        group = cat.groups.get(res.group)
        if group is not None and not group.enabled:
            raise self._scope_off(res.group)
        wanted = _norm(site)
        rows = await self._fetch("sites", filters={"customer_id": str(customer["id"])}, q=clean_text(site, 100),
                                 fields=self._fields(res, SITE_FIELDS), limit=SEARCH_ROWS, max_rows=SEARCH_ROWS)
        rows = [r for r in rows if str(r.get("customer_id", customer["id"])) == str(customer["id"])]
        exact = [r for r in rows if _norm(r.get("name")) == wanted]
        if len(exact) == 1:
            return exact[0]
        if len(rows) == 1 and wanted and wanted in _norm(rows[0].get("name")):
            return rows[0]
        if not rows:
            raise Refused(f"{customer['name']} has no site matching '{clean_text(site, 60)}' in the FSM. Ask the caller which site, "
                          "or look up the whole account without a site.", "not_found")
        pool = exact or rows
        raise Refused(f"{customer['name']} has more than one site that could be meant, so I haven't picked one. Ask the caller which, "
                      "then ask again with the site's exact name.", "ambiguous_site",
                      sites=[{"name": r.get("name") or "", "postcode_area": _outward(r.get("postcode"))} for r in pool[:MAX_CANDIDATES]])

    # ------------------------------------------------------------------ the figures
    async def _figures(self, cat, customer: dict[str, Any], site: dict[str, Any] | None, site_account: bool) -> dict[str, Any]:
        res = cat.resources["invoices"]
        fields = self._fields(res, INVOICE_FIELDS)
        filters = {"customer_id": str(customer["id"])}
        if site is not None and "site_id" in res.filters:
            filters["site_id"] = str(site["id"])
        today = self.today()
        owed = overdue = Decimal("0")
        oldest: tuple[date, str, Decimal] | None = None
        to_sites = unreadable = 0
        stats = ScanStats()
        try:
            async for page in self.client.scan("invoices", stats, filters=filters, fields=fields):
                for row in page:
                    if str(row.get("customer_id", customer["id"])) != str(customer["id"]):
                        continue   # (an FSM that ignored the filter can't put another customer's invoice in this answer)
                    if site is not None and str(row.get("site_id", site["id"])) != str(site["id"]):
                        continue
                    addressed_to_site = str(row.get("bill_to") or "").lower() == "site"
                    if site_account and "bill_to" in row and not addressed_to_site:
                        continue   # the site's own account: only what is addressed to the site
                    amount = _money(row.get("outstanding"))
                    if amount is None:
                        unreadable += 1
                        continue
                    if amount <= 0:
                        continue
                    owed += amount
                    to_sites += addressed_to_site
                    due = _day(row.get("due_at"))
                    if due is not None and due < today:
                        overdue += amount
                        key = (due, str(row.get("invoice_no") or row.get("id") or ""), amount)
                        if oldest is None or key[:2] < oldest[:2]:
                            oldest = key
        except FsmDataError as e:
            if e.kind == "scope_off":
                raise self._scope_off(e.group) from None
            raise Refused(e.message, e.kind) from None
        out: dict[str, Any] = {
            "owed": _gbp(owed), "overdue": _gbp(overdue), "currency": "GBP", "as_of": today.isoformat(),
            "oldest_overdue_invoice": None if oldest is None else {
                "invoice_no": clean_text(oldest[1], 40), "due_date": oldest[0].isoformat(),
                "days_overdue": (today - oldest[0]).days, "outstanding": _gbp(oldest[2])},
        }
        notes = []
        if stats.truncated:
            notes.append("The FSM has more invoices for this account than Jarvis reads in one go, so these figures may be "
                         "incomplete - say so, and suggest the owner checks the account in the FSM.")
        if unreadable:
            notes.append("Some invoices had no readable balance, so the figures may be incomplete - say so.")
        if site is None and to_sites:
            notes.append("Some of what is owed is on invoices addressed to this customer's sites (sites that are invoiced "
                         "directly). If the caller is from one site, ask which and look up that site.")
        if notes:
            out["notes"] = notes
        return out

    # ------------------------------------------------------------------ the tool
    async def lookup(self, customer: str = "", customer_id: str | None = None, site: str | None = None) -> dict[str, Any]:
        caller = access.current_caller.get()
        if caller is not None and caller.is_team and not caller.is_office:
            return {"error": access.OFFICE_ONLY_REFUSAL, "kind": "office_only"}   # (belt and braces: tool_allowed refuses first)
        if self.client is None or self.client.demo:
            return {"error": DEMO_MESSAGE, "kind": "demo", "demo": True}
        try:
            self._rate_limit(caller)
            cat = await self._catalog()
            cust = await self._customer(cat, customer, customer_id)
            the_site = await self._site(cat, cust, site) if site and site.strip() else None
            site_account = bool(the_site and the_site.get("invoice_to_site") is True)
            self._audit(caller, cust, the_site)
            figures = await self._figures(cat, cust, the_site if site_account else None, site_account)
        except Refused as r:
            return r.out
        account: dict[str, Any] = {"customer_id": str(cust.get("id")), "customer": cust.get("name") or "",
                                   "account_ref": cust.get("account_ref") or ""}
        if the_site is not None:
            account["site"] = the_site.get("name") or ""
            account["account_is"] = "site" if site_account else "customer"
            account["why"] = ("This site is invoiced directly ('Invoices go to this site'), so these are the site's own invoices."
                              if site_account else
                              f"Invoices for this site go to {cust.get('name') or 'the customer'}, so this is the customer's "
                              "whole account, not just this site.")
        out = {**account, **figures, "handling": HANDLING}
        # The figures are as sensitive as the invoices they came from: the owner's / a manager's `remember` refuses them too.
        try:
            self.j.fsm_read.note_sensitive_text(f"{figures['owed']} {figures['overdue']}"
                                                + (" " + " ".join(str(v) for v in figures["oldest_overdue_invoice"].values())
                                                   if figures["oldest_overdue_invoice"] else ""))
        except Exception:  # noqa: BLE001
            log.exception("Could not note a balance as sensitive")
        return out
