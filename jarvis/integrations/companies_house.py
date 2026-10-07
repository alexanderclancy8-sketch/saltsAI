"""Companies House Public Data API (free): the company register's filing status, read-only.

https://api.company-information.service.gov.uk - HTTP Basic auth with the API key as the USERNAME and an empty password.
Three endpoints are used, all GET, all about the COMPANY (never a person):

    /search/companies?q=...&items_per_page=5   candidates for a name
    /company/{number}                          the company profile
    /company/{number}/charges                  only its COUNTS (outstanding / total) are read

The officers and persons-with-significant-control endpoints are deliberately never called and no field of a response that
names an individual (officers, PSCs, the people entitled to a charge) is read, stored or returned: ``parse_profile`` copies
a short allowlist of company-level fields and nothing else.

Rate limit: Companies House allows 600 requests per 5 minutes per key. ``CompaniesHouse`` keeps its own sliding window a
little under that and, after a 429, stops asking for a minute rather than hammering. Every failure is a ``CHError`` with a
plain message that carries no secret (the key is never put in a message, a log line or a URL).

Everything in a response is untrusted text (a company name can contain anything): ``clean_text`` strips control and
invisible characters, URLs and e-mail addresses and caps the length before any of it is shown or handed to the model.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import deque
from datetime import date
from typing import Any, Callable

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.company-information.service.gov.uk"
SEARCH_ITEMS = 5          # candidates asked for / shown
TIMEOUT_S = 8.0           # one request
LIMIT_REQUESTS, LIMIT_WINDOW_S = 500, 300   # our own cap, under Companies House's 600 per 5 minutes
BACKOFF_AFTER_429_S = 60

NOT_CONNECTED = "Companies House isn't connected yet - add the free API key in Settings."

_URL = re.compile(r"(?:https?://|ftp://|www\.)\S+", re.I)
_EMAIL = re.compile(r"\S+@\S+\.\S+")
_NUMBER_FULL = re.compile(r"[A-Z0-9]{2}\d{6}")
_DIGITS = re.compile(r"\d{6,8}")
_OUTWARD = re.compile(r"^([A-Z]{1,2}\d[A-Z\d]?)\s*\d[A-Z]{2}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CHError(Exception):
    """A Companies House call that did not give an answer. ``kind``: not_connected | auth | not_found | rate_limited |
    unavailable. ``str(e)`` is a plain sentence for the owner and never contains the key."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


MESSAGES = {
    "not_connected": NOT_CONNECTED,
    "auth": "Companies House refused the API key (it may be wrong or switched off). Check it in Settings.",
    "not_found": "Companies House has no record of that.",
    "rate_limited": "Companies House is limiting requests at the moment - try again in a few minutes.",
    "unavailable": "Companies House isn't answering right now - try again later.",
}


def clean_text(value: Any, limit: int = 80) -> str:
    """Text from the register made safe to show and to hand to the model: no control, invisible or bidirectional
    characters, no line breaks, no URL or e-mail address, collapsed spaces, capped at ``limit``."""
    text = unicodedata.normalize("NFKC", str(value if value is not None else ""))
    text = "".join(" " if unicodedata.category(c).startswith("Z") or c in "\t\r\n" else c
                   for c in text if not unicodedata.category(c).startswith("C") or c in "\t\r\n")
    text = _EMAIL.sub(" ", _URL.sub(" ", text))
    text = re.sub(r"[\[\]<>{}`]", " ", text)  # no link / tag / template syntax survives in a name
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"


def md_safe(text: str) -> str:
    """For the display card (markdown): the characters that could turn a company name into a link, emphasis or a table."""
    return re.sub(r"[\[\]<>*_`|\\~#]", " ", text).strip()


def name_key(name: Any) -> str:
    """A name reduced to what a person would call 'the same': case, punctuation and spacing gone, '&' = 'and',
    Ltd = Limited. The suffix is kept (Acme Fire and Acme Fire Ltd are different questions), so nothing is guessed."""
    text = clean_text(name, 300).lower().replace("&", " and ").replace("'", "").replace("’", "")
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    words = ["limited" if w == "ltd" else w for w in words]
    return " ".join(words)


def normalise_number(text: Any) -> str:
    """A Companies House number if ``text`` is one (8 characters: 01234567, SC123456; 6-7 digits get their leading
    zeros back), otherwise ''. A company NAME is never turned into a number."""
    raw = re.sub(r"\s+", "", str(text or "")).upper()
    if _DIGITS.fullmatch(raw):
        return raw.zfill(8)
    return raw if _NUMBER_FULL.fullmatch(raw) else ""


def _iso(value: Any) -> str:
    text = str(value or "")
    if not _ISO_DATE.match(text):
        return ""
    try:
        date.fromisoformat(text)
    except ValueError:
        return ""
    return text


def _outward(postcode: Any) -> str:
    """Only the first half of a postcode (BD1, LS12) - a region, never an address."""
    m = _OUTWARD.match(str(postcode or "").strip().upper())
    return m.group(1) if m else ""


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_candidate(item: Any) -> dict[str, str] | None:
    """One search hit, company-level fields only."""
    item = _as_dict(item)
    number = normalise_number(item.get("company_number"))
    name = clean_text(item.get("title"), 80)
    if not number or not name:
        return None
    address = _as_dict(item.get("address"))
    return {"number": number, "name": name, "status": clean_text(item.get("company_status"), 40).lower(),
            "incorporated": _iso(item.get("date_of_creation")), "town": clean_text(address.get("locality"), 40)}


def parse_profile(data: Any) -> dict[str, Any]:
    """The company profile reduced to an allowlist of company-level facts. Nothing about a person is copied, and the
    registered office is cut down to its town and the first half of its postcode."""
    d = _as_dict(data)
    number = normalise_number(d.get("company_number"))
    if not number:
        raise CHError("unavailable", MESSAGES["unavailable"])
    accounts, conf = _as_dict(d.get("accounts")), _as_dict(d.get("confirmation_statement"))
    last = _as_dict(accounts.get("last_accounts"))
    office = _as_dict(d.get("registered_office_address"))
    sic = [c for c in (re.sub(r"\D", "", str(x)) for x in (d.get("sic_codes") or []) if isinstance(x, (str, int)))
           if 4 <= len(c) <= 5][:6]
    return {
        "number": number,
        "name": clean_text(d.get("company_name"), 80),
        "status": clean_text(d.get("company_status"), 40).lower(),
        "status_detail": clean_text(d.get("company_status_detail"), 80).lower(),
        "type": clean_text(d.get("type"), 60).lower(),
        "created": _iso(d.get("date_of_creation")),
        "ceased": _iso(d.get("date_of_cessation")),
        "accounts_next_due": _iso(accounts.get("next_due")),
        "accounts_overdue": accounts.get("overdue") is True,
        "accounts_last_made_up_to": _iso(last.get("made_up_to")),
        "accounts_last_type": clean_text(last.get("type"), 40).lower(),
        "confirmation_next_due": _iso(conf.get("next_due")),
        "confirmation_overdue": conf.get("overdue") is True,
        "sic_codes": sic,
        "town": clean_text(office.get("locality"), 40),
        "postcode_area": _outward(office.get("postal_code")),
        "insolvency_history": d.get("has_insolvency_history") is True,
        "has_charges": d.get("has_charges") is True,
        "charges_outstanding": None,
    }


def parse_charges(data: Any) -> int:
    """Charges still outstanding (unsatisfied + part-satisfied) - a COUNT. The people or organisations entitled to a charge
    are never read."""
    d = _as_dict(data)
    unsatisfied, part = _count(d.get("unsatisfied_count")), _count(d.get("part_satisfied_count"))
    if unsatisfied is None and part is None:
        total, satisfied = _count(d.get("total_count")), _count(d.get("satisfied_count"))
        if total is None:
            raise CHError("unavailable", MESSAGES["unavailable"])
        return max(total - (satisfied or 0), 0)
    return (unsatisfied or 0) + (part or 0)


class CompaniesHouse:
    """The three read-only calls, behind a rate limiter. ``key`` is a callable so a key saved in Settings is picked up
    without rebuilding anything."""

    def __init__(self, key: Callable[[], str], http: httpx.AsyncClient,
                 clock: Callable[[], float] = time.monotonic):
        self._key = key
        self.http = http
        self._clock = clock
        self._sent: deque[float] = deque()
        self._blocked_until = 0.0

    @property
    def configured(self) -> bool:
        return bool(str(self._key() or "").strip())

    def _take_slot(self) -> None:
        now = self._clock()
        if now < self._blocked_until:
            raise CHError("rate_limited", MESSAGES["rate_limited"])
        while self._sent and now - self._sent[0] > LIMIT_WINDOW_S:
            self._sent.popleft()
        if len(self._sent) >= LIMIT_REQUESTS:
            raise CHError("rate_limited", MESSAGES["rate_limited"])
        self._sent.append(now)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        key = str(self._key() or "").strip()
        if not key:
            raise CHError("not_connected", NOT_CONNECTED)
        self._take_slot()
        try:
            r = await self.http.get(BASE_URL + path, params=params, auth=httpx.BasicAuth(key, ""),
                                    headers={"Accept": "application/json"}, timeout=TIMEOUT_S)
        except httpx.HTTPError as e:
            # the exception text can carry the request URL; say only what kind of failure it was
            log.info("Companies House request failed (%s)", type(e).__name__)
            raise CHError("unavailable", MESSAGES["unavailable"]) from None
        code = r.status_code
        if code in (401, 403):
            raise CHError("auth", MESSAGES["auth"])
        if code == 404:
            raise CHError("not_found", MESSAGES["not_found"])
        if code == 429:
            self._blocked_until = self._clock() + BACKOFF_AFTER_429_S
            raise CHError("rate_limited", MESSAGES["rate_limited"])
        if code >= 400:
            log.info("Companies House answered HTTP %s", code)
            raise CHError("unavailable", MESSAGES["unavailable"])
        try:
            return r.json()
        except ValueError:
            raise CHError("unavailable", MESSAGES["unavailable"]) from None

    async def search(self, name: str) -> list[dict[str, str]]:
        query = clean_text(name, 160)
        if not query:
            return []
        data = await self._get("/search/companies", {"q": query, "items_per_page": SEARCH_ITEMS})
        items = _as_dict(data).get("items")
        found = [c for c in (parse_candidate(i) for i in (items if isinstance(items, list) else [])) if c]
        return found[:SEARCH_ITEMS]

    async def profile(self, number: str) -> dict[str, Any]:
        n = normalise_number(number)
        if not n:
            raise CHError("not_found", MESSAGES["not_found"])
        return parse_profile(await self._get(f"/company/{n}"))

    async def charges_outstanding(self, number: str) -> int:
        """0 when the company has no charges register (Companies House answers 404 for that)."""
        n = normalise_number(number)
        try:
            return parse_charges(await self._get(f"/company/{n}/charges"))
        except CHError as e:
            if e.kind == "not_found":
                return 0
            raise
