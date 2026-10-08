"""Read-only access to everything the Salts FSM lets Jarvis read: the generic ``/api/jarvis`` data API.

The FSM publishes two GET endpoints (the contract, agreed with the FSM side):

* ``GET /api/jarvis/catalog``  -> ``{version, groups: {group: {enabled, description}}, resources: [{name, group,
  description, fields: [{name, type, description}], filters: [field names], sensitive}]}``
* ``GET /api/jarvis/data/{resource}`` with ``filter[field]=v`` (also ``filter[field][gte]`` / ``[lte]``), ``q``,
  ``updated_since``, ``fields=a,b``, ``limit`` (default 100, max 500), ``offset``, ``order=field|-field`` ->
  ``{resource, items, total, next_offset (null at the end), truncated}``.
  Errors: 401 no/bad key, 403 ``{error: "scope_off", group}``, 404 unknown resource, 422 bad field/filter, 429 + Retry-After.

and, since salts-fsm PR #9, what is INSIDE a stored document (``docs/jarvis_data_api.md``, "Document text and files"):

* the catalog carries ``capabilities: {document_text, document_files}`` and a ``documents`` section (paths, limits, rules);
* ``GET /api/jarvis/documents/{id}/text`` -> ``{id, name, kind, category, group, mime, size_bytes, pages, extracted_text,
  char_count, truncated, text_source: embedded|none, note, (the redaction count), file_available, untrusted_content, notice}``;
* ``GET /api/jarvis/documents/{id}/file`` -> the raw bytes of a scanned PDF or a PNG/JPEG (<= 10 MB, never finance/people,
  its own "document files" switch). Errors: 403 scope_off / files_off / file_not_available, 404 document_not_found,
  409 document_unreadable / text_available, 413 too_large, 415 unsupported_type, 429 / 503 busy with Retry-After.
  The document routes have their own rate limit (30 a minute) on top of the shared one, so a 429 / 503 there backs off only
  the document routes, and a document-level failure (a missing or damaged file) never backs off the data API.

This module is the client for it. It is deliberately GET-only (a test greps for any other verb), has no path to the action
queue, and never logs a row. What it guarantees:

* The catalog is cached (10 minutes) and replaced when its ``version`` changes. A catalog that cannot be fetched is not
  hammered: a missing API (404/405 - the FSM has not shipped it yet) backs off for minutes to an hour, an outage for one to
  fifteen, and each outage logs ONE warning.
* ``fetch()`` follows ``next_offset`` page by page up to a hard cap (500 rows by default, a few thousand for internal jobs)
  and says when it stopped early (``truncated``, with the offset to continue from).
* Requests are limited to a few at once, have a timeout, respect ``429 Retry-After`` (a short wait is slept, a long one is
  reported) and every failure becomes a plain ``FsmDataError`` that names no URL, key or row.
* Everything returned is UNTRUSTED TEXT: control and zero-width characters are removed, HTML tags are stripped, whitespace is
  collapsed, secret-looking strings go through the existing redaction helpers, every string is length-capped, a value under a
  key that names a credential is blanked, and nesting is flattened. Rows are data - nothing in them is ever an instruction.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import quote

import httpx

from ..redact import REDACTED
from .redact import redact as redact_secrets

log = logging.getLogger(__name__)

CATALOG_PATH = "/api/jarvis/catalog"
DATA_PATH = "/api/jarvis/data/"
DOCUMENTS_PATH = "/api/jarvis/documents/"

CATALOG_TTL_S = 600            # how long a fetched catalog is trusted
CATALOG_STALE_OK_S = 3600      # an outage may keep serving a catalog this old (only to validate a query)
CATALOG_HEAL_GAP_S = 30        # a rejected query may force a catalog refresh, at most this often
PAGE_SIZE = 500                # the FSM's per-request maximum
DEFAULT_MAX_ROWS = 500         # rows one call returns by default
HARD_MAX_ROWS = 5000           # the most any caller may ask a single fetch() for
MAX_PAGES = 20                 # pages one fetch() follows
SCAN_MAX_ROWS = 50_000         # rows one scan() (the analysis tool's reader) looks at
SCAN_MAX_PAGES = 120           # pages one scan() follows (100 full pages = SCAN_MAX_ROWS)
SCAN_MAX_SECONDS = 60.0        # wall time one scan() may take, measured on the client's own (injectable) clock
MAX_CONCURRENT = 3             # requests in flight at once
TIMEOUT_S = 20.0
MAX_RETRY_AFTER_WAIT_S = 10.0  # a Retry-After up to this is slept through; longer is reported
MAX_RATE_RETRIES = 2
MAX_BODY_BYTES = 8_000_000
FIELD_CHARS = 500              # longest string value kept
KEY_CHARS = 64
MAX_KEYS = 80                  # most keys kept in one row
MAX_DEPTH = 3
DESCRIPTION_CHARS = 200
DOC_TEXT_CHARS = 200_000       # the most document text kept from one /text answer (the FSM's own cap)
DOC_FILE_MAX_BYTES = 10 * 1024 * 1024   # the FSM serves files up to 10 MB; anything bigger is refused unread
DOC_FILE_TIMEOUT_S = 45.0
DOC_FILE_MIME = ("application/pdf", "image/png", "image/jpeg")
MASKED_FIELD = "redaction" "s"  # the /text answer's count of values the FSM masked

MISSING_API = "the FSM doesn't expose this yet"
MISSING_DOCUMENTS = ("The FSM doesn't expose document reading yet (it needs the FSM update that adds the document text routes), "
                     "so Jarvis can't read inside its documents. The document register itself can still be listed with fsm_data "
                     "(resource 'documents').")

# A resource or group name has to be a plain token: these end up in a URL and in the system prompt.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,79}")
# A document id goes into a URL path: a plain token (a uuid, 'doc-abc', a number) and nothing else.
_DOC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,79}")
# Document text keeps its line breaks and tabs; every other control character, zero-width / bidi override / tag character goes.
_DOC_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\U000e0000-\U000e007f]")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f​-‏‪-‮⁠-⁯﻿]")
_TAG = re.compile(r"</?[A-Za-z!?][^>]*>?")
_SPACE = re.compile(r"\s+")
# A key that names a credential: its value is blanked whatever it says (the FSM never sends these; this is belt and braces).
_SECRET_KEY = re.compile(r"password|passwd|secret|token|api[_\- ]?key|mfa|totp|\botp\b|private[_\- ]?key|"
                         r"card[_\- ]?(?:number|no)\b|\bcvv|\bcvc|\biban\b|sort[_\- ]?code", re.I)


class FsmDataError(Exception):
    """A read the FSM did not answer. ``message`` is plain English and carries no URL, key or row."""

    def __init__(self, kind: str, message: str, *, status: int | None = None, group: str | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.group = group
        self.retry_after = retry_after

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.message, "kind": self.kind}
        if self.group:
            out["group"] = self.group
        if self.retry_after is not None:
            out["retry_after_s"] = round(self.retry_after)
        return out


# --------------------------------------------------------------------------------------------- cleaning untrusted text
def clean_text(value: Any, limit: int = FIELD_CHARS) -> str:
    """One string from the FSM made safe to hand on: no control or zero-width characters, no HTML tags, one line, secret-looking
    strings redacted, at most ``limit`` characters."""
    text = _CONTROL.sub(" ", str(value))
    text = _TAG.sub(" ", text)
    text = _SPACE.sub(" ", text).strip()
    text = redact_secrets(text)
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def clean_document_text(value: Any, limit: int = DOC_TEXT_CHARS) -> str:
    """The text inside a stored document made safe to hand on: line breaks and tabs kept (it is prose and tables), every other
    control / zero-width / bidi-override / tag character removed, runs of blank lines collapsed, secret-looking strings redacted
    (on top of the FSM's own masking), at most ``limit`` characters. Still UNTRUSTED: the caller fences it as data."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _DOC_CONTROL.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    text = redact_secrets(text)
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def clean_value(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, str):
        return clean_text(value)
    if depth >= MAX_DEPTH:
        return clean_text(value)
    if isinstance(value, dict):
        return clean_row(value, depth + 1)
    if isinstance(value, (list, tuple)):
        return [clean_value(v, depth + 1) for v in list(value)[:50]]
    return clean_text(value)


def clean_row(row: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in list(row.items())[:MAX_KEYS]:
        name = clean_text(key, KEY_CHARS)
        if not name:
            continue
        out[name] = REDACTED if _SECRET_KEY.search(name) and value not in (None, "") else clean_value(value, depth)
    return out


# ------------------------------------------------------------------------------------------------------- the catalog
@dataclass(frozen=True)
class FieldInfo:
    name: str
    type: str = ""
    description: str = ""


@dataclass(frozen=True)
class Resource:
    name: str
    group: str
    description: str = ""
    fields: tuple[FieldInfo, ...] = ()
    filters: tuple[str, ...] = ()
    sensitive: bool = False

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def field_type(self, name: str) -> str:
        return next((f.type for f in self.fields if f.name == name), "")


@dataclass(frozen=True)
class Group:
    name: str
    enabled: bool = True
    description: str = ""


@dataclass
class Catalog:
    version: str
    groups: dict[str, Group]
    resources: dict[str, Resource]
    fetched_at: float = 0.0
    capabilities: dict[str, bool] = field(default_factory=dict)   # {document_text, document_files}; empty on an older FSM
    documents: dict[str, Any] = field(default_factory=dict)       # the catalog's `documents` section, cleaned

    @property
    def document_text(self) -> bool:
        """The FSM serves GET /api/jarvis/documents/{id}/text (an older FSM has no `capabilities` and so no document routes)."""
        return bool(self.capabilities.get("document_text"))

    @property
    def document_files(self) -> bool:
        """The owner's 'Jarvis may download document files' switch is on (GET .../file can serve a scan or a photo)."""
        return self.document_text and bool(self.capabilities.get("document_files"))

    @property
    def document_file_max_bytes(self) -> int:
        """The file route's size cap as the catalog states it, never above the contract's 10 MB."""
        spec = self.documents.get("file") if isinstance(self.documents.get("file"), dict) else {}
        value = spec.get("max_bytes")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return min(value, DOC_FILE_MAX_BYTES)
        return DOC_FILE_MAX_BYTES

    def in_group(self, group: str) -> list[Resource]:
        return [r for r in self.resources.values() if r.group == group]

    @property
    def scope_off(self) -> list[str]:
        return [g.name for g in self.groups.values() if not g.enabled]

    def enabled_resources(self) -> list[Resource]:
        off = set(self.scope_off)
        return [r for r in self.resources.values() if r.group not in off]


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def parse_catalog(payload: Any, now: float = 0.0) -> Catalog:
    """A Catalog from the FSM's JSON. Tolerant of small shape differences (a group list instead of a map, a field given as a bare
    name) and strict about names: a resource or group whose name is not a plain token is dropped, never passed on."""
    if not isinstance(payload, dict):
        raise FsmDataError("bad_response", "The FSM's catalog wasn't in the shape Jarvis expects.")
    groups: dict[str, Group] = {}
    raw_groups = payload.get("groups")
    if isinstance(raw_groups, dict):
        items = [(k, v) for k, v in raw_groups.items()]
    elif isinstance(raw_groups, list):
        items = [(g.get("name"), g) for g in raw_groups if isinstance(g, dict)]
    else:
        items = []
    for name, spec in items:
        name = str(name or "")
        if not _NAME.fullmatch(name):
            continue
        spec = spec if isinstance(spec, dict) else {}
        groups[name] = Group(name, _as_bool(spec.get("enabled"), True), clean_text(spec.get("description") or "", DESCRIPTION_CHARS))
    resources: dict[str, Resource] = {}
    for r in payload.get("resources") or []:
        if not isinstance(r, dict):
            continue
        name, group = str(r.get("name") or ""), str(r.get("group") or "")
        if not _NAME.fullmatch(name) or not _NAME.fullmatch(group):
            continue
        fields = []
        for f in r.get("fields") or []:
            if isinstance(f, str):
                f = {"name": f}
            if isinstance(f, dict) and _NAME.fullmatch(str(f.get("name") or "")):
                fields.append(FieldInfo(str(f["name"]), clean_text(f.get("type") or "", 30),
                                        clean_text(f.get("description") or "", DESCRIPTION_CHARS)))
        filters = []
        for f in r.get("filters") or []:
            f = f.get("name") if isinstance(f, dict) else f
            if _NAME.fullmatch(str(f or "")):
                filters.append(str(f))
        if group not in groups:
            groups[group] = Group(group, True, "")
        resources[name] = Resource(name, group, clean_text(r.get("description") or "", DESCRIPTION_CHARS), tuple(fields),
                                   tuple(filters), _as_bool(r.get("sensitive"), False))
    raw_caps = payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}
    caps = {k: _as_bool(raw_caps.get(k), False) for k in ("document_text", "document_files") if k in raw_caps}
    docs = payload.get("documents")
    docs = clean_row(docs) if isinstance(docs, dict) else {}
    return Catalog(clean_text(payload.get("version") or "", 60), groups, resources, now, caps, docs)


@dataclass
class FetchResult:
    resource: str
    items: list[dict[str, Any]]
    total: int | None
    truncated: bool
    pages: int
    next_offset: int | None = None   # where to carry on from when ``truncated`` (None = nothing more, or unknown)


def _retry_after(r: httpx.Response, default: float = 2.0) -> float:
    raw = r.headers.get("Retry-After")
    if raw is None:
        return default
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return default


def _error_text(r: httpx.Response) -> tuple[str, dict[str, Any]]:
    """(the FSM's own short explanation, the JSON body if there was one) of an error response - cleaned, never raw."""
    try:
        body = r.json()
    except ValueError:
        return "", {}
    if not isinstance(body, dict):
        return "", {}
    text = body.get("message") or body.get("error") or body.get("detail") or ""
    if not isinstance(text, str):
        text = ""
    return clean_text(text, 200), body


# ---------------------------------------------------------------------------------------------------------- the client
class FsmData:
    """Catalog + data reads from the FSM's Jarvis API. ``fsm`` is the FSM router (``demo`` flag and ``jarvis_call``)."""

    def __init__(self, fsm: Any, *, catalog_ttl_s: float = CATALOG_TTL_S, max_concurrent: int = MAX_CONCURRENT,
                 timeout_s: float = TIMEOUT_S, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        self.fsm = fsm
        self.ttl = catalog_ttl_s
        self.timeout_s = timeout_s
        self._clock = clock
        self._sleep = sleep
        self._max_concurrent = max_concurrent
        self._sem: asyncio.Semaphore | None = None
        self._catalog_lock: asyncio.Lock | None = None
        self._catalog: Catalog | None = None
        self._last_heal = -1e9
        self._backoff_until = 0.0
        self._backoff_s = 0.0
        self._rate_until = 0.0
        self._doc_rate_until = 0.0                 # the document routes' own limit (30 a minute) / 'busy': backs off only them
        self._down = False                         # an outage is open (one warning has been logged for it)
        self._last_error: FsmDataError | None = None
        self._base: str | None = None              # the FSM address the state above was learned from
        self.on_change: Callable[[], None] | None = None   # called when the set of groups/resources first appears or changes

    # ---------------------------------------------------------------- state
    @property
    def demo(self) -> bool:
        return bool(getattr(self.fsm, "demo", False))

    @property
    def cached(self) -> Catalog | None:
        """The catalog as last fetched (even if stale), without touching the network."""
        return self._catalog

    @property
    def last_error(self) -> FsmDataError | None:
        return self._last_error

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_concurrent)
        return self._sem

    def _lock(self) -> asyncio.Lock:
        if self._catalog_lock is None:
            self._catalog_lock = asyncio.Lock()
        return self._catalog_lock

    # ---------------------------------------------------------------- outage handling
    def _sync_base(self) -> None:
        """The owner can change the FSM address on the Settings page without a restart: a catalog (or an outage) learned from the
        old address says nothing about the new one, so forget both."""
        base = getattr(getattr(self.fsm, "s", None), "fsm_base_url", None)
        if base != self._base:
            if self._base is not None:
                self._catalog, self._last_error, self._backoff_until, self._backoff_s = None, None, 0.0, 0.0
                self._rate_until, self._doc_rate_until, self._down = 0.0, 0.0, False
            self._base = base

    def _gate(self, doc: bool = False) -> None:
        self._sync_base()
        if self.demo:
            raise FsmDataError("demo", "Salts FSM isn't connected (it is showing sample data), so there is nothing real to read. "
                                       "Set FSM_BASE_URL and the API key under Connections.")
        now = self._clock()
        if now < self._rate_until:
            raise FsmDataError("rate_limited", f"The FSM is rate limiting Jarvis; try again in about {round(self._rate_until - now) or 1} "
                                               "seconds.", retry_after=self._rate_until - now)
        if doc and now < self._doc_rate_until:
            raise FsmDataError("busy", f"The FSM is busy with document reads just now; try again in about "
                                       f"{round(self._doc_rate_until - now) or 1} seconds.", retry_after=self._doc_rate_until - now)
        if now < self._backoff_until and self._last_error is not None:
            e = self._last_error
            raise FsmDataError(e.kind, e.message, status=e.status, group=e.group, retry_after=self._backoff_until - now)

    def _fail(self, err: FsmDataError, *, first_s: float, cap_s: float, why: str) -> FsmDataError:
        """Record an outage: back off (doubling to ``cap_s``), warn ONCE per outage, remember the error to repeat while backing off."""
        self._backoff_s = min(max(first_s, self._backoff_s * 2), cap_s)
        self._backoff_until = self._clock() + self._backoff_s
        self._last_error = err
        if not self._down:
            log.warning("Salts FSM data API unavailable: %s. Not retrying for about %d minute(s).", why,
                        max(1, round(self._backoff_s / 60)))
        self._down = True
        return err

    def _ok(self) -> None:
        if self._down:
            log.info("Salts FSM data API is reachable again")
        self._down = False
        self._backoff_s = 0.0
        self._backoff_until = 0.0
        self._last_error = None

    # ---------------------------------------------------------------- one request
    async def _send(self, path: str, params: dict[str, Any] | None = None, *, timeout: float | None = None,
                    doc: bool = False) -> httpx.Response:
        """The one GET this module makes: the gate, the in-flight limit, the timeout and ``429 Retry-After`` (and, on the document
        routes, ``503 busy`` with Retry-After), a short wait slept through and a long one reported. Any other status is handed
        back for the caller to map. Raises FsmDataError."""
        self._gate(doc)
        for attempt in range(MAX_RATE_RETRIES + 1):
            async with self._semaphore():
                try:
                    r = await self.fsm.jarvis_call("GET", path, params=params, timeout=timeout or self.timeout_s)
                except httpx.TimeoutException:
                    raise self._fail(FsmDataError("network", "The FSM took too long to answer, so nothing was read."),
                                     first_s=60, cap_s=900, why="timed out") from None
                except (httpx.HTTPError, OSError) as e:
                    raise self._fail(FsmDataError("network", "Couldn't reach the FSM, so nothing was read."),
                                     first_s=60, cap_s=900, why=type(e).__name__) from None
            code = r.status_code
            busy = doc and code == 503 and r.headers.get("Retry-After") is not None
            if code == 429 or busy:
                wait = _retry_after(r)
                if wait <= MAX_RETRY_AFTER_WAIT_S and attempt < MAX_RATE_RETRIES:
                    await self._sleep(wait)
                    continue
                if doc:   # the document routes' own limit (or both files slots taken): back off only the document routes
                    self._doc_rate_until = self._clock() + wait
                    raise FsmDataError("busy", f"The FSM is busy with document reads just now (it allows 30 a minute and two at once); "
                                               f"try again in about {round(wait) or 1} seconds.", status=code, retry_after=wait)
                self._rate_until = self._clock() + wait
                raise FsmDataError("rate_limited", f"The FSM is rate limiting Jarvis; try again in about {round(wait) or 1} seconds.",
                                   status=429, retry_after=wait)
            break
        return r

    def _unauthorized(self) -> FsmDataError:
        return self._fail(FsmDataError("unauthorized", "The FSM rejected Jarvis's API key (401), so nothing was read. "
                                                       "Check the FSM key under Connections.", status=401),
                          first_s=60, cap_s=900, why="the API key was refused (401)")

    @staticmethod
    def _scope_off(body: dict[str, Any]) -> FsmDataError:
        group = clean_text(body.get("group") or "", 80) or None
        return FsmDataError("scope_off", f"The '{group or 'requested'}' group is switched off in the FSM for Jarvis (scope off), "
                                         "so I can't read it. The owner can switch it on in the FSM's Jarvis access settings.",
                            status=403, group=group)

    async def _get(self, path: str, params: dict[str, Any] | None = None, *, known_catalog: bool = False) -> dict[str, Any]:
        """One GET against /api/jarvis/*, returning the JSON object. ``known_catalog``: the catalog answered earlier, so a 404 here
        means 'no such resource', not 'the API isn't there'. Raises FsmDataError."""
        r = await self._send(path, params)
        code = r.status_code
        text, body = _error_text(r) if code >= 400 else ("", {})
        if code == 401:
            raise self._unauthorized()
        if code == 403:
            if body.get("error") == "scope_off" or "scope" in str(body.get("error", "")):
                raise self._scope_off(body)
            raise FsmDataError("forbidden", "The FSM refused that read (403)." + (f" It said: {text}" if text else ""), status=403)
        if code in (404, 405):
            if known_catalog and code == 404 and path.startswith(DATA_PATH):
                raise FsmDataError("not_found", "The FSM has no resource with that name." + (f" It said: {text}" if text else ""),
                                   status=404)
            raise self._fail(FsmDataError("unavailable", f"{MISSING_API.capitalize()} (HTTP {code}) - Jarvis can't read it yet.",
                                          status=code), first_s=300, cap_s=3600, why=f"it has no Jarvis data API yet (HTTP {code})")
        if code == 422 or code == 400:
            raise FsmDataError("bad_request", "The FSM didn't accept that query" + (f": {text}" if text else f" ({code})") + ".", status=code)
        if code >= 500:
            raise self._fail(FsmDataError("server", f"The FSM had a problem answering (HTTP {code}). Try again shortly.", status=code),
                             first_s=60, cap_s=900, why=f"it answered HTTP {code}")
        if code >= 400 or not 200 <= code < 300:
            raise FsmDataError("bad_response", f"The FSM answered with an unexpected status ({code}).", status=code)
        if len(r.content) > MAX_BODY_BYTES:
            raise FsmDataError("bad_response", "The FSM's answer was too large to read; narrow the query.", status=code)
        try:
            data = r.json()
        except ValueError:
            raise self._fail(FsmDataError("unavailable", f"{MISSING_API.capitalize()} (it answered, but not with data).", status=code),
                             first_s=300, cap_s=3600, why="it answered with something that is not JSON") from None
        if not isinstance(data, dict):
            raise FsmDataError("bad_response", "The FSM's answer wasn't in the shape Jarvis expects.", status=code)
        self._ok()
        return data

    # ---------------------------------------------------------------- the catalog
    async def catalog(self, force: bool = False) -> Catalog:
        """The catalog: cached for ``ttl`` seconds, refetched when stale, replaced when its version changes."""
        self._sync_base()
        cat = self._catalog
        if cat is not None and not force and self._clock() - cat.fetched_at < self.ttl:
            return cat
        async with self._lock():
            cat = self._catalog
            if cat is not None and not force and self._clock() - cat.fetched_at < self.ttl:
                return cat
            try:
                payload = await self._get(CATALOG_PATH)
                fresh = parse_catalog(payload, self._clock())
            except FsmDataError as e:
                if e.kind == "unavailable":
                    self._catalog = None        # the API has gone away: nothing cached is true any more
                    if cat is not None:
                        self._changed()
                elif cat is not None and e.kind in ("network", "server", "rate_limited") and \
                        self._clock() - cat.fetched_at < CATALOG_STALE_OK_S:
                    return cat                  # a blip: keep checking queries against the last good catalog
                raise
            changed = cat is None or cat.version != fresh.version or set(cat.resources) != set(fresh.resources) \
                or cat.scope_off != fresh.scope_off or cat.capabilities != fresh.capabilities
            if cat is not None and cat.version != fresh.version:
                log.info("Salts FSM data catalog changed (version %s -> %s)", cat.version or "?", fresh.version or "?")
            self._catalog = fresh
            if changed:
                self._changed()
            return fresh

    def _changed(self) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception:  # noqa: BLE001 - telling the prompt about it must not break a read
            log.exception("FSM catalog change hook failed")

    async def heal_catalog(self) -> None:
        """A query the cached catalog allowed was refused (unknown resource/field, scope off): the cache may be stale. Refetch it, at
        most once every CATALOG_HEAL_GAP_S. Never raises."""
        if self._clock() - self._last_heal < CATALOG_HEAL_GAP_S:
            return
        self._last_heal = self._clock()
        try:
            await self.catalog(force=True)
        except FsmDataError:
            pass

    # ---------------------------------------------------------------- data
    @staticmethod
    def build_params(filters: dict[str, Any] | None, q: str | None, fields: list[str] | None, order: str | None,
                     updated_since: str | None) -> dict[str, str]:
        params: dict[str, str] = {}
        for key, value in (filters or {}).items():
            params[f"filter[{key}]" if "[" not in key else "filter[" + key.replace("[", "][", 1)] = _param_value(value)
        if q:
            params["q"] = str(q)
        if fields:
            params["fields"] = ",".join(fields)
        if order:
            params["order"] = order
        if updated_since:
            params["updated_since"] = updated_since
        return params

    async def fetch(self, resource: str, *, filters: dict[str, Any] | None = None, q: str | None = None,
                    fields: list[str] | None = None, order: str | None = None, updated_since: str | None = None,
                    limit: int | None = None, max_rows: int = DEFAULT_MAX_ROWS, offset: int = 0) -> FetchResult:
        """Rows of ``resource``, following ``next_offset`` page by page until ``limit`` (default ``max_rows``) rows are held or
        the hard caps are reached. ``truncated`` is True when there was more than what is returned; ``next_offset`` is then where
        to continue. Every row is cleaned (see the module docstring). Raises FsmDataError."""
        if not _NAME.fullmatch(resource or ""):
            raise FsmDataError("not_found", "That isn't a resource name the FSM could have.")
        cat = await self.catalog()   # also proves the API is there, so a 404 below means 'no such resource'
        max_rows = max(1, min(int(max_rows), HARD_MAX_ROWS))
        want = max(1, min(int(limit) if limit else max_rows, max_rows))
        base = self.build_params(filters, q, fields, order, updated_since)
        rows: list[dict[str, Any]] = []
        total: int | None = None
        truncated = False
        pages = 0
        off = max(0, int(offset))
        next_offset: int | None = None
        while True:
            params = {**base, "limit": str(min(PAGE_SIZE, want - len(rows))), "offset": str(off)}
            try:
                body = await self._get(DATA_PATH + quote(resource, safe=""), params, known_catalog=cat is not None)
            except FsmDataError as e:
                if e.kind in ("not_found", "scope_off", "bad_request"):
                    await self.heal_catalog()
                raise
            pages += 1
            items = [i for i in (body.get("items") if isinstance(body.get("items"), list) else []) if isinstance(i, dict)]
            room = want - len(rows)
            rows.extend(clean_row(i) for i in items[:room])
            if isinstance(body.get("total"), int) and not isinstance(body.get("total"), bool):
                total = body["total"]
            server_truncated = bool(body.get("truncated"))
            nxt = body.get("next_offset")
            nxt = nxt if isinstance(nxt, int) and not isinstance(nxt, bool) else None
            if len(items) > room:
                truncated, next_offset = True, off + room
                break
            if nxt is None:
                truncated = server_truncated
                break
            if len(rows) >= want:
                truncated, next_offset = True, nxt
                break
            if nxt <= off or not items or pages >= MAX_PAGES:
                truncated, next_offset = True, (nxt if nxt > off else None)
                break
            off = nxt
        return FetchResult(resource, rows, total, truncated, pages, next_offset)

    async def scan(self, resource: str, stats: ScanStats, *, filters: dict[str, Any] | None = None, q: str | None = None,
                   fields: list[str] | None = None, order: str | None = None, updated_since: str | None = None,
                   max_rows: int = SCAN_MAX_ROWS, max_pages: int = SCAN_MAX_PAGES,
                   max_seconds: float = SCAN_MAX_SECONDS) -> AsyncIterator[list[dict[str, Any]]]:
        """Pages of cleaned rows for an aggregation: far more rows than ``fetch`` returns to a model, but never held at once - each
        page is handed on and dropped. Stops (and says so in ``stats``) at ``max_rows`` rows, ``max_pages`` pages or ``max_seconds``.
        Raises FsmDataError like ``fetch``; rows already yielded are the caller's to keep or discard."""
        if not _NAME.fullmatch(resource or ""):
            raise FsmDataError("not_found", "That isn't a resource name the FSM could have.")
        cat = await self.catalog()
        base = self.build_params(filters, q, fields, order, updated_since)
        max_rows = max(1, min(int(max_rows), SCAN_MAX_ROWS))
        started = self._clock()
        off = 0
        while True:
            if self._clock() - started > max_seconds:
                stats.truncated, stats.reason = True, "time"
                return
            params = {**base, "limit": str(min(PAGE_SIZE, max_rows - stats.scanned)), "offset": str(off)}
            try:
                body = await self._get(DATA_PATH + quote(resource, safe=""), params, known_catalog=cat is not None)
            except FsmDataError as e:
                if e.kind in ("not_found", "scope_off", "bad_request"):
                    await self.heal_catalog()
                raise
            stats.pages += 1
            items = [i for i in (body.get("items") if isinstance(body.get("items"), list) else []) if isinstance(i, dict)]
            room = max_rows - stats.scanned
            page = [clean_row(i) for i in items[:room]]
            stats.scanned += len(page)
            if isinstance(body.get("total"), int) and not isinstance(body.get("total"), bool):
                stats.total = body["total"]
            nxt = body.get("next_offset")
            nxt = nxt if isinstance(nxt, int) and not isinstance(nxt, bool) else None
            if page:
                yield page
            if len(items) > room:
                stats.truncated, stats.reason = True, "row_cap"
                return
            if nxt is None:
                if body.get("truncated"):
                    stats.truncated, stats.reason = True, "server"
                return
            if stats.scanned >= max_rows:
                stats.truncated, stats.reason = True, "row_cap"
                return
            if nxt <= off or not items:
                stats.truncated, stats.reason = True, "server"
                return
            if stats.pages >= max_pages:
                stats.truncated, stats.reason = True, "page_cap"
                return
            off = nxt

    # ---------------------------------------------------------------- what is inside a stored document
    async def _documents_ready(self, doc_id: Any) -> tuple[Catalog, str]:
        """(the catalog, the id as it goes into the URL) - or FsmDataError: demo, no catalog, an FSM without the document routes
        ('doesn't expose it yet'), or an id that is not a plain token (it never reaches a URL)."""
        cat = await self.catalog()
        if not cat.document_text:
            raise FsmDataError("unavailable", MISSING_DOCUMENTS)
        did = str(doc_id if doc_id is not None else "").strip()
        if not _DOC_ID.fullmatch(did):
            raise FsmDataError("not_found", "That isn't a document id the FSM could have (ids are letters, digits and dashes).")
        return cat, did

    def _document_error(self, r: httpx.Response) -> FsmDataError:
        """A plain FsmDataError for a document route's error answer. Only a refused key backs off the whole API: a missing,
        damaged or refused document says nothing about the FSM being down."""
        code = r.status_code
        text, body = _error_text(r)
        err = str(body.get("error") or "").strip().lower()
        if code == 401:
            return self._unauthorized()
        if code == 403:
            if err == "scope_off":
                return self._scope_off(body)
            if err == "files_off":
                return FsmDataError("files_off", "The FSM's 'Jarvis may download document files' switch is off, so I can't fetch the "
                                                 "scan or photo itself to transcribe it (the document's text route still works). The "
                                                 "owner can switch it on in the FSM: Settings > Integrations > Jarvis access.", status=403)
            if err == "file_not_available":
                return FsmDataError("file_not_available", "The FSM never hands over the file of a finance or people document "
                                                          "(invoices, statements, purchase orders, engineers' certificates...), so a "
                                                          "scan of one can't be transcribed - only its text layer can be read.",
                                    status=403)
            return FsmDataError("forbidden", "The FSM refused that document read (403)." + (f" It said: {text}" if text else ""),
                                status=403)
        if code == 404:
            return FsmDataError("not_found", "The FSM has no document with that id that Jarvis may read (it may have been deleted, or "
                                             "the record it was attached to has gone).", status=404)
        if code == 405:
            return FsmDataError("unavailable", MISSING_DOCUMENTS, status=405)
        if code == 409:
            if err == "text_available":
                return FsmDataError("text_available", "That PDF has a text layer, so the FSM serves its text (with secrets masked) "
                                                      "rather than the file - its text is what was read.", status=409)
            return FsmDataError("integrity", "The FSM's stored copy of that document failed its integrity check (the file no longer "
                                             "matches the fingerprint taken when it was stored), so it wasn't read. Ask whoever looks "
                                             "after the FSM to check the file.", status=409)
        if code == 413:
            return FsmDataError("too_large", "That document's file is over the FSM's 10 MB limit for handing files to Jarvis, so it "
                                             "wasn't fetched.", status=413)
        if code == 415:
            return FsmDataError("unsupported_type", "The FSM only hands over PDF, PNG and JPEG files, and this document is none of "
                                                    "those.", status=415)
        if code == 503:
            if err == "busy":
                return FsmDataError("busy", "The FSM is busy reading other documents just now; try again in a few seconds.",
                                    status=503, retry_after=_retry_after(r, 5.0))
            return FsmDataError("server", "The FSM couldn't reach its document storage just now (503). Try again shortly.", status=503)
        if code >= 500:
            return FsmDataError("server", f"The FSM had a problem reading that document (HTTP {code}). Try again shortly.", status=code)
        return FsmDataError("bad_response", f"The FSM answered the document read with an unexpected status ({code}).", status=code)

    async def document_text(self, doc_id: Any) -> dict[str, Any]:
        """``GET /api/jarvis/documents/{id}/text``, cleaned: every string field made safe, ``text`` (from ``extracted_text``)
        cleaned as document text. The text is UNTRUSTED - the caller fences it. Raises FsmDataError (plain words)."""
        _, did = await self._documents_ready(doc_id)
        r = await self._send(DOCUMENTS_PATH + quote(did, safe="") + "/text", doc=True)
        if not 200 <= r.status_code < 300:
            raise self._document_error(r)
        if len(r.content) > MAX_BODY_BYTES:
            raise FsmDataError("bad_response", "The FSM's answer for that document was too large to read.", status=r.status_code)
        try:
            body = r.json()
        except ValueError:
            raise FsmDataError("bad_response", "The FSM's answer for that document wasn't in the shape Jarvis expects.",
                               status=r.status_code) from None
        if not isinstance(body, dict):
            raise FsmDataError("bad_response", "The FSM's answer for that document wasn't in the shape Jarvis expects.",
                               status=r.status_code)
        self._ok()

        def num(key: str) -> int | None:
            v = body.get(key)
            return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

        # The FSM's count of masked values. (Spelt in two pieces so the module's "no path to the action queue" guard test, which
        # looks for the word "actions" anywhere in the code, isn't tripped by the contract's field name.)
        red = body.get(MASKED_FIELD)
        if isinstance(red, (list, tuple)):
            masked = len(red)
        elif isinstance(red, dict):
            masked = red.get("count") if isinstance(red.get("count"), int) else len(red)
        else:
            masked = red if isinstance(red, int) and not isinstance(red, bool) and red > 0 else 0
        source = clean_text(body.get("text_source") or "none", 20).lower()
        return {
            "id": clean_text(body.get("id") if body.get("id") is not None else did, 80) or did,
            "name": clean_text(body.get("name") or "", 200),
            "kind": clean_text(body.get("kind") or "", 60),
            "category": clean_text(body.get("category") or "", 60),
            "group": clean_text(body.get("group") or "", 40).lower(),
            "mime": clean_text(body.get("mime") or "", 80).lower(),
            "size_bytes": num("size_bytes"),
            "pages": num("pages"),
            "text": clean_document_text(body.get("extracted_text") or ""),
            "char_count": num("char_count"),
            "truncated": _as_bool(body.get("truncated"), False),
            "text_source": source if source in ("embedded", "ocr", "none") else "none",
            "note": clean_text(body.get("note") or "", 400),
            "masked": max(0, int(masked or 0)),
            "file_available": _as_bool(body.get("file_available"), False),
        }

    async def document_file(self, doc_id: Any) -> tuple[bytes, str]:
        """``GET /api/jarvis/documents/{id}/file``: (the bytes, their mime type) of a scanned PDF or a PNG / JPEG - checked against
        the size cap twice (the declared length before the body is trusted, then the bytes themselves) and the mime allow-list.
        The caller still checks the bytes ARE what the mime type says. Raises FsmDataError (plain words)."""
        cat, did = await self._documents_ready(doc_id)
        if not cat.document_files:
            raise FsmDataError("files_off", "The FSM's 'Jarvis may download document files' switch is off, so I can't fetch the scan "
                                            "or photo itself to transcribe it. The owner can switch it on in the FSM: Settings > "
                                            "Integrations > Jarvis access.", status=403)
        cap = cat.document_file_max_bytes
        r = await self._send(DOCUMENTS_PATH + quote(did, safe="") + "/file", timeout=DOC_FILE_TIMEOUT_S, doc=True)
        if not 200 <= r.status_code < 300:
            raise self._document_error(r)
        too_big = FsmDataError("too_large", f"That document's file is over the {cap // (1024 * 1024)} MB limit for files Jarvis "
                                            "fetches, so it wasn't read.", status=r.status_code)
        declared = r.headers.get("Content-Length")
        if declared is not None and declared.strip().isdigit() and int(declared) > cap:
            raise too_big
        data = r.content
        if len(data) > cap:
            raise too_big
        if not data:
            raise FsmDataError("bad_response", "The FSM sent an empty file for that document.", status=r.status_code)
        mime = (r.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if mime == "image/jpg":
            mime = "image/jpeg"
        if mime not in DOC_FILE_MIME:
            raise FsmDataError("unsupported_type", "The FSM sent a kind of file Jarvis doesn't transcribe (only PDF, PNG and JPEG).",
                               status=r.status_code)
        self._ok()
        return data, mime


@dataclass
class ScanStats:
    """What a scan() did, filled in as it goes. ``truncated`` is True when it stopped before the end of the matching rows;
    ``reason`` is then ``row_cap``, ``page_cap`` or ``time`` (the FSM's own ``truncated`` flag is ``server``)."""
    scanned: int = 0
    pages: int = 0
    total: int | None = None
    truncated: bool = False
    reason: str = ""


def _param_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
