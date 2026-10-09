"""What Jarvis did: ONE read model over everything Jarvis proposed, prepared or changed - and what a person decided about it.

The owner asked for "one place listing every draft, email, job proposal and change it made, and what you approved or
declined, so you can check it quickly". This module is that place's backend, and it is deliberately only a *reader*:
it UNIONS the records that already exist, it never keeps a second copy of any of them, and it never approves, declines,
sends, retries or changes anything (a test greps it for every verb that could).

Sources (each is a read of an existing store, mapped to ONE common item shape):

    pending_actions   every email, FSM write, job proposal, invoice run, deploy... with its approval history
                      (waiting / approved / declined / failed / dismissed / edited, standing-approval auto-runs, edit and retry links)
    check_runs        the quiet scheduled checks (services/activity.py): collapsed into one line unless they found something
    suggestions       Jarvis's suggestions and the Prepare button's drafts (services/fsm_suggestions.py)
    agent_runs        the engineering agents (self-improve / fixer pull requests, security reviews)
    background_calls  tools run in the background (async_tools.py)
    memory            what Jarvis was told to remember
    audit_events      settings saved, team access changed, memory reworded or removed, CSV exports (names and times only)
    documents/adverts drafted documents and designed adverts
    kv upsell markers the FSM upsell draft wordings Jarvis improved (services/upsell_drafts.py)
    automations       scheduled automations set up in conversation
    engineer-home audit lines (check_runs rows of the owner-only job) - shown to the principal owner only

Item shape: {id, when, kind, what, status, who, requested_by, decided_by, decided_at, source, source_ref, detail, error, link, ...}.

Everything a person can read passes through ``Cleaner``: the approval cards' own redaction (webhook signatures, tokens, keys,
bearer headers, spoken access codes), the live secret values from Settings (the staff report key, passwords, API keys),
sensitive KEYS in a payload (code, password, pin, token...), map coordinates and UK postcodes. Nothing here reads, joins
or exports an engineer's home point: that data is not a source and nothing in this file names its table.

Performance: every source is read newest-first with ``LIMIT`` and never more than ``SCAN_CAP`` rows; the merge reads only as far
as the page needs (``REACH_CAP`` items deep); there is no query per item. Actions are paged through an index on the very
expression they are ordered by (``idx_actions_when``).
"""

from __future__ import annotations

import csv
import heapq
import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .. import access, demo_guard
from ..history import redact_history
from ..integrations.redact import redact as redact_secrets
from ..redact import REDACTED, is_sensitive_param, redact_text
from . import approval_inbox as inbox
from . import standing_approvals as sa
from .activity import BASELINE, FAILED, NO_CHANGE, OWNER_ONLY_JOBS

log = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------------------ vocabulary
KIND_LABELS = {"draft": "Draft", "email": "Email", "job_proposal": "Job proposal", "fsm_change": "FSM change",
               "settings_change": "Settings change", "memory": "Memory", "code_change": "PR / code change",
               "scheduled_check": "Scheduled check", "suggestion": "Suggestion", "other": "Other"}
STATUS_LABELS = {"waiting": "Waiting for you", "approved": "Approved", "declined": "Declined", "done": "Done",
                 "failed": "Failed", "dismissed": "Dismissed", "edited": "Edited", "auto_approved": "Auto-approved (standing)",
                 "running": "Running"}
NEEDS_LOOK = "needs_look"          # a filter, not a status: failed + waiting
RANGES = ("today", "yesterday", "7d", "30d")
RANGE_LABELS = {"today": "Today", "yesterday": "Yesterday", "7d": "Last 7 days", "30d": "Last 30 days"}

PAGE_MAX = 200                     # most items one request returns
DEFAULT_LIMIT = 50
SCAN_CAP = 3000                    # hard limit on rows read from any ONE source for ONE request
REACH_CAP = 3000                   # how deep (offset + limit) a request may page; beyond it the answer says so
EXPORT_MAX = 5000                  # most rows a CSV export holds
TEXT_MAX = 5000                    # longest single detail value shown (an email body)
AUDIT_RETENTION_DAYS = 400
SPOKEN_ITEMS = 5
HIDE_POSTCODES = True              # see Cleaner: the conservative reading of "postcodes/home points are never shown"

APPROVALS_LINK = {"pop": "approvals", "label": "Open in Approvals"}
_PR_URL = re.compile(r"^https://github\.com/[\w.\-]+/[\w.\-]+/pull/\d+$")

EMAIL_KINDS = {"email_send", "tool:email_send", sa.PO_ACK_KIND, "review_requests",
               "fsm_renewal_send"}   # a renewal Salts FSM emails once a person approves it (services/fsm_renewals.py)
JOB_KINDS = {"accept_quote", "accept_quote_from_po", "tool:log_job"}
FSM_KINDS = {"fsm_write", "tool:fsm_change"}
CODE_KINDS = {"deploy_fix", "tool:issue_fix"}
WITHHELD_KINDS = {"tool:site_access_code_update"}   # an access code is never shown here, not even redacted
SENT_STATES = {"approved", "auto_approved", "done", "failed"}   # an email in one of these was (or was being) sent: it is "an email", not "a draft"
_JOB_PATH = re.compile(r"^/(?:api/)?jobs/?$")

# What each kind of record rests on, so a record that only involved sample data can be told apart from a real one.
_SAMPLE_SOURCE = {**{k: "mail" for k in EMAIL_KINDS}, **{k: "fsm" for k in JOB_KINDS | FSM_KINDS},
                  "sage_invoices": demo_guard.ACCOUNTS, "tool:stock_move": demo_guard.STOCK,
                  "tool:stock_stocktake": demo_guard.STOCK, "tool:stock_item_update": demo_guard.STOCK,
                  "tool:staff_update_role": demo_guard.STAFF, "fsm_renewal_send": "fsm"}

# ------------------------------------------------------------------------------------------------ cleaning
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
_COORDS = re.compile(r"-?\d{1,3}\.\d{3,}\s*[,;/]\s*-?\d{1,3}\.\d{3,}")
_COORD_FIELD = re.compile(r"(?i)\b(?:lat|lng|lon|latitude|longitude)\b[\"']?\s*[:=]\s*-?\d+(?:\.\d+)?")
_POSTCODE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,2}\d[A-Za-z\d]?\s*\d[A-Za-z]{2}(?![A-Za-z0-9])")
_ASSIGNED_SECRET = re.compile(r"(?i)\b((?:password|passwd|pwd|passcode|secret|token|api[_\- ]?key|access[_\- ]?key|signature)\s*(?:is\s*)?[:=]\s*)[^\s,;\"'&]{4,}")
_URL = re.compile(r"https?://\S+")
_LOCATION_KEYS = frozenset({"lat", "lng", "lon", "latitude", "longitude", "coordinates", "coords", "home", "home_point", "location"})
_HIDDEN = "[hidden]"


class Cleaner:
    """Redacts for display. One instance per request: it carries the live secret values from Settings."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        self.secrets = [s for s in (secrets or []) if isinstance(s, str) and len(s) >= 6]

    def text(self, value: Any, limit: int = 300, multiline: bool = False) -> str:
        s = value if isinstance(value, str) else ("" if value is None else str(value))
        s = s[: limit * 4 + 2000]                       # bound the work first; the cut is made again after redaction
        for secret in self.secrets:
            s = s.replace(secret, REDACTED)
        s = redact_text(redact_secrets(s))
        s = _ASSIGNED_SECRET.sub(lambda m: m.group(1) + REDACTED, s)      # "token=..." / "password: ..." written into plain text
        s = redact_history(s)                            # spoken access codes / PINs
        s = _COORD_FIELD.sub("[location hidden]", _COORDS.sub("[location hidden]", s))
        if HIDE_POSTCODES:
            s = _POSTCODE.sub("[postcode hidden]", s)
        s = _CONTROL.sub("", s)
        if not multiline:
            s = " ".join(s.split())
        return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"

    def obj(self, value: Any, depth: int = 0) -> Any:
        """A deep copy of a payload that is safe to put on a card: sensitive KEYS blanked, location keys dropped, every string cleaned."""
        if depth > 6:
            return _HIDDEN
        if isinstance(value, str):
            return self.text(value, TEXT_MAX, multiline=True)
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                key = str(k)
                if key.lower() in _LOCATION_KEYS:
                    continue
                out[key] = REDACTED if (is_sensitive_param(key) and v not in (None, "", False, 0)) else self.obj(v, depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            return [self.obj(v, depth + 1) for v in list(value)[:200]]
        return value


def secret_values(settings: Any) -> list[str]:
    """The live secret values (API keys, tokens, passwords, the staff report key) so they can be struck out of any text wherever they appear."""
    from ..settings_store import FIELDS

    out: list[str] = []
    for key, field in FIELDS.items():
        if field.kind == "secret":
            v = getattr(settings, key, "")
            if isinstance(v, str) and len(v) >= 6:
                out.append(v)
    for key in ("staff_report_key", "jarvis_owner_password"):
        v = getattr(settings, key, "")
        if isinstance(v, str) and len(v) >= 6 and v not in out:
            out.append(v)
    return out


# ------------------------------------------------------------------------------------------------ time
def utc(ts: Any) -> str:
    """A stored timestamp as 'YYYY-MM-DDTHH:MM:SS+00:00' (the form every table here uses), or '' if it is not one."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - an odd timezone name must not stop the page
        return ZoneInfo("UTC")


def window(rng: str, tz: ZoneInfo, now: datetime | None = None) -> tuple[str, str, str]:
    """(since, until, label) in UTC for today / yesterday / the last 7 or 30 days, counted in whole local days. until '' = open ended."""
    if rng not in RANGES:
        raise ValueError(f"range must be one of: {', '.join(RANGES)}")
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    days_back = {"today": 0, "yesterday": 1, "7d": 6, "30d": 29}[rng]
    since = midnight - timedelta(days=days_back)
    until = midnight if rng == "yesterday" else None
    iso = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")  # noqa: E731
    return iso(since), iso(until) if until else "", RANGE_LABELS[rng]


# ------------------------------------------------------------------------------------------------ the query
class Query:
    def __init__(self, since: str, until: str = "", kinds: Any = (), statuses: Any = (), who: str = "", text: str = "",
                 everything: bool = False, owner: bool = False, drop_sample: bool = False) -> None:
        self.since, self.until = since, until
        self.kinds = frozenset(k for k in kinds if k in KIND_LABELS)
        self.statuses = frozenset(s for s in statuses if s in STATUS_LABELS or s == NEEDS_LOOK)
        self.who = " ".join(str(who or "").split())[:80].lower()
        self.text = " ".join(str(text or "").split())[:100].lower()
        self.everything = bool(everything)
        self.owner = bool(owner)                        # the principal owner: may also see the owner-only audit lines
        self.drop_sample = bool(drop_sample)            # the voice answer leaves out records that only involved sample data


class _Ctx:
    def __init__(self, feed: "ActivityFeed", q: Query) -> None:
        self.j = feed.j
        self.db = feed.j.db
        self.q = q
        self.tz = feed.tz()
        self.now = datetime.now(timezone.utc)
        self.clean = Cleaner(secret_values(feed.j.settings))
        self.demo = feed.sample_sources()
        self.memo: dict[str, Any] = {}


def _item(ctx: _Ctx, *, id: str, when: str, kind: str, what: str, status: str, source: str, ref: str, requested_by: str = "",
          decided_by: str = "", decided_at: str = "", error: str = "", link: dict[str, str] | None = None, quiet: bool = False,
          attention: bool = False, sample_source: str = "", created_at: str = "", detail: list[dict[str, Any]] | None = None,
          builder: Callable[[], list[dict[str, Any]]] | None = None, auto: bool = False, chain: str = "") -> dict[str, Any]:
    who = requested_by
    if decided_by:
        verb = {"declined": "declined", "failed": "approved", "edited": "edited", "dismissed": "dismissed"}.get(status, "approved")
        who = f"{requested_by + ' - ' if requested_by else ''}{verb} by {decided_by}"
    return {"id": id, "when": when, "kind": kind, "kind_label": KIND_LABELS[kind], "what": what, "status": status,
            "status_label": STATUS_LABELS[status], "who": who, "requested_by": requested_by, "decided_by": decided_by,
            "decided_at": decided_at, "created_at": created_at, "source": source, "source_ref": ref, "detail": detail,
            "error": error, "link": link, "quiet": quiet, "attention": attention, "auto": auto, "chain": chain,
            "sample": bool(sample_source and sample_source in ctx.demo), "_build": builder}


def _sort_key(item: dict[str, Any]) -> tuple[str, int, str]:
    """Newest first by time; within the same second by the record's own number (so action 10 sorts after action 9), then by id."""
    tail = item["id"].rsplit(":", 1)[-1]
    return item["when"], int(tail) if tail.isdigit() else 0, item["id"]


def _row(label: str, value: Any, block: bool = False) -> dict[str, Any]:
    return {"label": label, "value": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str),
            "block": block}


# ------------------------------------------------------------------------------------------------ sources
def _classify_action(kind: str, payload: dict[str, Any], status: str) -> str:
    if kind in EMAIL_KINDS:
        return "email" if status in SENT_STATES else "draft"
    if kind in JOB_KINDS:
        return "job_proposal"
    if kind == "fsm_write":
        return "job_proposal" if payload.get("method") == "POST" and _JOB_PATH.match(str(payload.get("path") or "")) else "fsm_change"
    if kind in FSM_KINDS:
        return "fsm_change"
    if kind in CODE_KINDS:
        return "code_change"
    return "other"


def _by_role(base: str, role: Any) -> str:
    """'Jarvis' -> 'Jarvis (asked by a manager)' when a manager's turn was behind it. The owner's, and a row from before roles were
    recorded, keep the plain wording."""
    return f"{base} (asked by a manager)" if str(role or "") == access.MANAGER else base


def _requester(kind: str, payload: dict[str, Any], row: dict[str, Any], clean: Cleaner) -> str:
    asker = payload.get("requested_by")
    if isinstance(asker, str) and asker.strip():
        return clean.text(asker, 80)
    base = "Jarvis (purchase order inbox)" if kind in (sa.PO_ACK_KIND, "accept_quote_from_po") else "Jarvis"
    if row.get("supersede_kind") == "retry" and row.get("supersedes"):
        return _by_role(f"Jarvis (retry of #{row['supersedes']})", row.get("requested_role"))
    if row.get("supersede_kind") == "edit" and row.get("supersedes"):
        return _by_role(f"{base} (edited from #{row['supersedes']})", row.get("requested_role"))
    return _by_role(base, row.get("requested_role"))


def _src_actions(ctx: _Ctx, limit: int) -> tuple[list[dict[str, Any]], bool]:
    q = ctx.q
    when = "COALESCE(NULLIF(decided_at, ''), created_at)"
    sql = f"SELECT * FROM pending_actions WHERE {when} >= ?" + (f" AND {when} < ?" if q.until else "")
    params: list[Any] = [q.since] + ([q.until] if q.until else [])
    rows = ctx.db.query(f"{sql} ORDER BY {when} DESC, id DESC LIMIT ?", tuple(params + [limit]))
    return [_map_action(ctx, r) for r in rows], len(rows) < limit


def _map_action(ctx: _Ctx, r: dict[str, Any]) -> dict[str, Any]:
    clean = ctx.clean
    try:
        payload = json.loads(r.get("payload_json") or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    kind = str(r["kind"])
    db_status, by, result = str(r["status"]), str(r.get("approved_by") or ""), str(r.get("result") or "")
    auto = by.startswith(sa.APPROVER_PREFIX)
    category = by[len(sa.APPROVER_PREFIX):] if auto else ""
    superseded, dismissed = r.get("superseded_by"), r.get("dismissed_at")
    blocked = db_status == "denied" and result.startswith("Blocked by the security check")
    if db_status == "pending":
        status = "waiting"
    elif db_status in ("approved", "done"):
        status = "auto_approved" if auto else "approved"
    elif db_status == "failed":
        status = "dismissed" if dismissed else "failed"
    elif db_status == "denied":
        status = "edited" if superseded and result.startswith("Edited by") else "failed" if blocked else "declined"
    else:
        status = "done"
    person = clean.text(by, 80)
    if auto:
        decided_by = f"standing approval ({clean.text(category, 60)})"
    elif blocked:
        decided_by = "the security check"
    else:
        decided_by = person if db_status != "pending" else ""
    created, decided = utc(r["created_at"]), utc(r.get("decided_at"))
    retried = db_status == "failed" and bool(superseded)
    error = clean.text(result, 600) if status in ("failed", "dismissed") else ""
    chain = ""
    if r.get("supersede_kind") == "edit" and r.get("supersedes"):
        chain = f"Edited from action #{r['supersedes']}"
    elif r.get("supersede_kind") == "retry" and r.get("supersedes"):
        chain = f"A retry of failed action #{r['supersedes']}"
    elif superseded and status == "edited":
        chain = f"Replaced by action #{superseded} after an edit"
    elif retried:
        chain = f"Retried as action #{superseded}"
    if dismissed:
        chain = (chain + "; " if chain else "") + f"dismissed by {clean.text(r.get('dismissed_by') or 'someone', 80)}"
    actionable = status == "waiting" or (status == "failed" and not retried)

    def build() -> list[dict[str, Any]]:
        rows_: list[dict[str, Any]] = []
        if kind in WITHHELD_KINDS:
            rows_.append(_row("Details", "Not shown here: access codes are never listed outside the site access records.", False))
        else:
            safe = clean.obj(payload)
            try:
                card = inbox.view({**r, "payload": safe})
                rows_ += [_row(d["label"], clean.text(d["value"], TEXT_MAX, multiline=True), bool(d.get("block")))
                          for d in card["details"]]
            except Exception:  # noqa: BLE001 - one odd payload must not take the page down
                rows_.append(_row("Details", "(this action's details could not be shown)"))
        if chain:
            rows_.append(_row("History", chain))
        if result and status not in ("failed", "dismissed"):
            rows_.append(_row("Result", clean.text(result, 800, multiline=True), True))
        return rows_

    return _item(ctx, id=f"action:{r['id']}", when=decided or created, kind=_classify_action(kind, payload, status),
                 what=clean.text(r.get("summary"), 300), status=status, source="action", ref=f"action #{r['id']}",
                 requested_by=_requester(kind, payload, r, clean), decided_by=decided_by, decided_at=decided, created_at=created,
                 error=error, link=APPROVALS_LINK if actionable else None, attention=actionable, auto=auto, chain=chain,
                 sample_source=_SAMPLE_SOURCE.get(kind, ""), builder=build)


def _src_checks(ctx: _Ctx, limit: int) -> tuple[list[dict[str, Any]], bool]:
    q = ctx.q
    sql = "SELECT * FROM check_runs WHERE ran_at >= ?"
    params: list[Any] = [q.since]
    if q.until:
        sql += " AND ran_at < ?"
        params.append(q.until)
    if not q.everything:
        sql += " AND outcome NOT IN ('no_change', 'baseline')"
    if not q.owner and OWNER_ONLY_JOBS:
        sql += f" AND job_key NOT IN ({','.join('?' * len(OWNER_ONLY_JOBS))})"
        params += sorted(OWNER_ONLY_JOBS)
    rows = ctx.db.query(sql + " ORDER BY ran_at DESC, id DESC LIMIT ?", tuple(params + [limit]))
    out = []
    # An automation's runs say who set it up (it ran with that role's permissions): one small read of the roles, only if any run is one.
    made_by: dict[str, str] = {}
    if any(str(r["job_key"]).startswith("automation_") for r in rows):
        made_by = {f"automation_{a['id']}": access.stored_role(a["role"]) for a in ctx.db.query("SELECT id, role FROM automations")}
    for r in rows:
        clean, name = ctx.clean, ctx.clean.text(r["job_name"], 80)
        detail = clean.text(r.get("detail"), 200)
        when = utc(r["ran_at"])
        if r["job_key"] in OWNER_ONLY_JOBS:      # an owner-only audit line: who did it is in the line itself ("... by the owner")
            m = re.search(r"\bby (.{1,60})$", detail)
            out.append(_item(ctx, id=f"check:{r['id']}", when=when, kind="settings_change", what=detail or name, status="done",
                             source="audit", ref=f"audit line #{r['id']}", requested_by=m.group(1).strip() if m else "the owner",
                             created_at=when, detail=[_row("What", detail or name)]))
            continue
        if r["outcome"] == FAILED:
            status, what, error, quiet = "failed", f"{name} didn't run properly", detail or "It failed.", False
        elif r["outcome"] in (NO_CHANGE, BASELINE):
            status, what, error, quiet = "done", f"{name}: nothing to report", "", True
        else:
            status, what, error, quiet = "done", f"{name} found something" + (f": {detail}" if detail else ""), "", False
        creator = made_by.get(str(r["job_key"]), "")
        by = "Scheduled job" if creator in ("", access.OWNER) else f"Scheduled job (created by a {creator})"
        out.append(_item(ctx, id=f"check:{r['id']}", when=when, kind="scheduled_check", what=what, status=status, source="check",
                         ref=f"check run #{r['id']}", requested_by=by, created_at=when, error=error, quiet=quiet,
                         attention=status == "failed", detail=[_row("Outcome", r["outcome"]), _row("Note", detail)] if detail else None))
    return out, len(rows) < limit


def _tool_label(name: str) -> str:
    try:
        from ..brain.tools import TOOLS_BY_NAME

        tool = TOOLS_BY_NAME.get(name)
        return tool.label if tool else name
    except Exception:  # noqa: BLE001 - a label is decoration
        return name


def _src_background(ctx: _Ctx, limit: int) -> tuple[list[dict[str, Any]], bool]:
    q = ctx.q
    when_expr = "COALESCE(NULLIF(finished_at, ''), created_at)"
    sql = f"SELECT * FROM background_calls WHERE {when_expr} >= ?"
    params: list[Any] = [q.since]
    if q.until:
        sql += f" AND {when_expr} < ?"
        params.append(q.until)
    if not q.everything:
        sql += " AND status NOT IN ('done', 'cancelled', 'awaiting_approval')"
    rows = ctx.db.query(sql + " ORDER BY id DESC LIMIT ?", tuple(params + [limit]))
    out = []
    for r in rows:
        clean = ctx.clean
        st = str(r["status"])
        status = {"running": "running", "awaiting_approval": "waiting", "done": "done", "cancelled": "dismissed"}.get(st, "failed")
        tool = clean.text(r["tool"], 80)
        label = clean.text(_tool_label(str(r["tool"])), 80)
        who = str(r.get("requester") or "")
        # (an engineer's requester key is the pre-split "team:<name>"; office is "office:<name>")
        who = (f"{clean.text(who.removeprefix('team:').title(), 40)} (engineer)" if who.startswith("team:")
               else f"{clean.text(who.removeprefix('office:').title(), 40)} (office)" if who.startswith("office:")
               else _by_role("Jarvis", r.get("role")))
        when = utc(r.get("finished_at") or r["created_at"])
        try:
            args = clean.obj(json.loads(r.get("args_json") or "{}"))
        except ValueError:
            args = {}
        error = clean.text(r.get("result"), 400) if status == "failed" else ""
        out.append(_item(ctx, id=f"background:{r['id']}", when=when, kind="other", what=f"Ran {label} in the background",
                         status=status, source="background", ref=f"background call #{r['id']}", requested_by=who,
                         created_at=utc(r["created_at"]), error=error, attention=status == "failed",
                         quiet=st in ("done", "cancelled", "awaiting_approval"), link=APPROVALS_LINK if st == "awaiting_approval" else None,
                         detail=[_row("Tool", tool), _row("Details", args, True),
                                 *([_row("Result", clean.text(r.get("result"), 600, multiline=True), True)] if status != "failed" and r.get("result") else [])]))
    return out, len(rows) < limit


def _src_agent_runs(ctx: _Ctx, limit: int) -> tuple[list[dict[str, Any]], bool]:
    from .agent_runs import STALL_AFTER

    q = ctx.q
    sql = "SELECT * FROM agent_runs WHERE updated_at >= ?" + (" AND updated_at < ?" if q.until else "")
    rows = ctx.db.query(sql + " ORDER BY updated_at DESC, id DESC LIMIT ?",
                        tuple([q.since] + ([q.until] if q.until else []) + [limit]))
    out = []
    for r in rows:
        clean = ctx.clean
        st, kind_name = str(r["status"]), str(r["kind"])
        updated = utc(r["updated_at"])
        stalled = False
        if st == "running":
            try:
                stalled = ctx.now - datetime.fromisoformat(updated) >= STALL_AFTER
            except ValueError:
                stalled = False
        status = ("failed" if stalled else "running") if st == "running" else "done" if st in ("submitted", "gave_up") else "failed"
        request = clean.text(r["request"], 200)
        what = {"self_improve": "Self-improvement run: ", "fixer": "Fix attempt: "}.get(kind_name, "Security review: ") + request
        outcome = clean.text(r.get("outcome"), 400)
        link = {"href": str(r["outcome"]), "label": "Open the pull request"} if st == "submitted" and _PR_URL.match(str(r["outcome"] or "")) else None
        err = ("It stopped reporting progress - check whether it is still running." if stalled else outcome) if status == "failed" else ""
        out.append(_item(ctx, id=f"run:{r['id']}", when=updated, kind="code_change" if kind_name in ("self_improve", "fixer") else "other",
                         what=what, status=status, source="agent_run", ref=f"engineering run #{r['id']}",
                         requested_by=_by_role("Jarvis", r.get("requested_role")),
                         created_at=utc(r["started_at"]), error=err, link=link, attention=status == "failed", quiet=st == "gave_up",
                         detail=[_row("Request", request), _row("Outcome", outcome), _row("Steps", str(r["steps"]))]))
    return out, len(rows) < limit


def _src_suggestions(ctx: _Ctx, limit: int) -> tuple[list[dict[str, Any]], bool]:
    q = ctx.q
    when = "CASE WHEN status = 'open' THEN created_at ELSE updated_at END"   # an open suggestion is refreshed constantly; it is as old as it was raised
    sql = f"SELECT * FROM suggestions WHERE {when} >= ?" + (f" AND {when} < ?" if q.until else "")
    rows = ctx.db.query(f"{sql} ORDER BY {when} DESC, key DESC LIMIT ?", tuple([q.since] + ([q.until] if q.until else []) + [limit]))
    out = []
    for r in rows:
        clean, st = ctx.clean, str(r["status"])
        status = {"open": "waiting", "dismissed": "dismissed"}.get(st, "done")
        when_ = utc(r["created_at"] if st == "open" else r["updated_at"])
        key = str(r["key"])
        root = demo_guard.SUGGESTION_SOURCES.get(key.split(":")[0], "fsm" if r.get("kind") else "")
        note = {"prepared": "A draft was prepared from this and is in Approvals.", "resolved": "It was no longer needed.",
                "done": "It was marked done.", "dismissed": "It was put aside."}.get(st, "")
        out.append(_item(ctx, id=f"suggestion:{key}", when=when_, kind="suggestion", what=f"Suggested: {clean.text(r['title'], 200)}",
                         status=status, source="suggestion", ref=f"suggestion {clean.text(key, 60)}", requested_by="Jarvis",
                         created_at=utc(r["created_at"]), link=APPROVALS_LINK if st == "open" else None, attention=st == "open",
                         sample_source=root, detail=[_row("Why", clean.text(r.get("detail"), 600, multiline=True), True),
                                                     *([_row("What happened", note)] if note else [])]))
    return out, len(rows) < limit


def _src_memory(ctx: _Ctx, limit: int) -> tuple[list[dict[str, Any]], bool]:
    q = ctx.q
    sql = "SELECT id, created_at, fact FROM memory WHERE created_at >= ?" + (" AND created_at < ?" if q.until else "")
    rows = ctx.db.query(sql + " ORDER BY id DESC LIMIT ?", tuple([q.since] + ([q.until] if q.until else []) + [limit]))
    out = []
    for r in rows:
        fact = ctx.clean.text(r["fact"], 400, multiline=False)
        out.append(_item(ctx, id=f"memory:{r['id']}", when=utc(r["created_at"]), kind="memory", what=f"Remembered: {fact[:160]}",
                         status="done", source="memory", ref=f"memory #{r['id']}", requested_by="Jarvis", created_at=utc(r["created_at"]),
                         detail=[_row("Remembered", fact, True)]))
    return out, len(rows) < limit


# "company_check": a Companies House look-up (the company's name and number only - never anything about a person)
# "fsm_read": a read of FSM records through fsm_data (the resource name and a row count - never a value)
# "balance_lookup": one customer's balance looked up with customer_balance (the customer and who asked - never a figure)
# "fsm_document": a read of a stored FSM document through fsm_document_read (its id, and its name unless it is owner-only - never its text)
# "rule": a house rule added (approved by the owner), changed, switched on / off or removed (services/rulebook.py) - its number and wording
# "drawing": a drawing on a floor plan created, proposed by Jarvis, saved, exported or deleted (services/plan_drawings.py) - its number,
#            title and who, never its contents
# "schematic": a system schematic drawn / revised (its drawing number, revision and title) or exported (the format) - services/schematics.py
# "fsm_renewal": a renewal draft prepared (or priced) in Salts FSM by fsm_renewal_prepare - the customer, contract and renewal id, and who asked
_AUDIT_KINDS = {"settings": "settings_change", "team_access": "settings_change", "memory": "memory", "export": "other",
                "company_check": "other", "fsm_read": "other", "balance_lookup": "other", "fsm_document": "other", "rule": "memory",
                "drawing": "draft", "schematic": "draft", "fsm_renewal": "fsm_change"}


def _src_audit(ctx: _Ctx, limit: int) -> tuple[list[dict[str, Any]], bool]:
    q = ctx.q
    sql = "SELECT * FROM audit_events WHERE at >= ?" + (" AND at < ?" if q.until else "")
    rows = ctx.db.query(sql + " ORDER BY at DESC, id DESC LIMIT ?", tuple([q.since] + ([q.until] if q.until else []) + [limit]))
    out = []
    for r in rows:
        when = utc(r["at"])
        out.append(_item(ctx, id=f"audit:{r['id']}", when=when, kind=_AUDIT_KINDS.get(str(r["kind"]), "other"),
                         what=ctx.clean.text(r["what"], 300), status="done", source="audit", ref=f"audit line #{r['id']}",
                         requested_by=ctx.clean.text(r["actor"], 80) or "the owner", created_at=when))
    return out, len(rows) < limit


def _src_documents(ctx: _Ctx, limit: int) -> tuple[list[dict[str, Any]], bool]:
    q = ctx.q
    sql = "SELECT id, created_at, kind, title FROM documents WHERE created_at >= ?" + (" AND created_at < ?" if q.until else "")
    rows = ctx.db.query(sql + " ORDER BY created_at DESC, id DESC LIMIT ?", tuple([q.since] + ([q.until] if q.until else []) + [limit]))
    return [_item(ctx, id=f"document:{r['id']}", when=utc(r["created_at"]), kind="draft",
                  what=f"Drafted a document: {ctx.clean.text(r['title'], 160)}", status="done", source="document",
                  ref="drafted document", requested_by="Jarvis", created_at=utc(r["created_at"]),
                  detail=[_row("Type", ctx.clean.text(r["kind"], 60))]) for r in rows], len(rows) < limit


def _src_adverts(ctx: _Ctx, limit: int) -> tuple[list[dict[str, Any]], bool]:
    q = ctx.q
    sql = "SELECT id, updated_at, platform, headline, revision FROM adverts WHERE updated_at >= ?" + (" AND updated_at < ?" if q.until else "")
    rows = ctx.db.query(sql + " ORDER BY updated_at DESC, id DESC LIMIT ?", tuple([q.since] + ([q.until] if q.until else []) + [limit]))
    out = []
    for r in rows:
        platform = ctx.clean.text(r["platform"], 40)
        out.append(_item(ctx, id=f"advert:{r['id']}", when=utc(r["updated_at"]), kind="draft",
                         what=f"{'Revised' if int(r['revision'] or 1) > 1 else 'Designed'} a {platform} advert draft",
                         status="done", source="advert", ref="advert draft", requested_by="Jarvis", created_at=utc(r["updated_at"]),
                         detail=[_row("Headline", ctx.clean.text(r["headline"], 200)), _row("Version", str(r["revision"]))]))
    return out, len(rows) < limit


def _src_upsell(ctx: _Ctx, limit: int) -> tuple[list[dict[str, Any]], bool]:
    """FSM upsell draft emails Jarvis re-worded (the kv marker services/upsell_drafts.py leaves; it has no time column, so the
    markers are read once per request - at most SCAN_CAP - and filtered here)."""
    if "upsell" not in ctx.memo:
        ctx.memo["upsell"] = ctx.db.query("SELECT key, value FROM kv WHERE key LIKE 'upsell:done:%' LIMIT ?", (SCAN_CAP,))
    q = ctx.q
    out = []
    for r in ctx.memo["upsell"]:
        try:
            marker = json.loads(r["value"])
        except ValueError:
            continue
        when = utc(marker.get("at")) if isinstance(marker, dict) else ""
        if not when or marker.get("state") != "improved" or when < q.since or (q.until and when >= q.until):
            continue
        item_id = ctx.clean.text(str(r["key"]).split(":")[2] if str(r["key"]).count(":") >= 2 else "", 60)
        out.append(_item(ctx, id=f"upsell:{r['key']}", when=when, kind="draft",
                         what=f"Improved the wording of an upsell email draft in the FSM (item {item_id})", status="done",
                         source="upsell", ref=f"FSM upsell item {item_id}", requested_by="Jarvis (upsell drafts check)",
                         created_at=when, sample_source="fsm",
                         detail=[_row("Where", "A person approves, edits or declines it in the FSM Action Centre; Jarvis never sends it.")]))
    out.sort(key=_sort_key, reverse=True)
    return out[:limit], len(ctx.memo["upsell"]) < SCAN_CAP


def _src_automations(ctx: _Ctx, limit: int) -> tuple[list[dict[str, Any]], bool]:
    q = ctx.q
    sql = ("SELECT id, created_at, description, cron, role, created_by FROM automations WHERE created_at >= ?"
           + (" AND created_at < ?" if q.until else ""))
    rows = ctx.db.query(sql + " ORDER BY id DESC LIMIT ?", tuple([q.since] + ([q.until] if q.until else []) + [limit]))
    out = []
    for r in rows:
        role = access.stored_role(r["role"])    # who created it, and so what it runs with (a row nobody recorded is a manager's)
        who = ctx.clean.text(r["created_by"], 60)
        creator = ("the owner" if role == access.OWNER else
                   f"a {role}" + (f" ({who})" if who else " (set up before roles were recorded)" if role == access.MANAGER else ""))
        out.append(_item(ctx, id=f"automation:{r['id']}", when=utc(r["created_at"]), kind="other",
                         what=f"Set up a scheduled automation: {ctx.clean.text(r['description'], 160)}", status="done", source="automation",
                         ref=f"automation #{r['id']}", requested_by="Jarvis" if role == access.OWNER else f"Jarvis (created by {creator})",
                         created_at=utc(r["created_at"]),
                         detail=[_row("Schedule", ctx.clean.text(r["cron"], 60)), _row("Created by", creator),
                                 _row("Runs with", "the owner's permissions" if role == access.OWNER else
                                      f"a {role}'s permissions: no owner-only FSM data (finance, pay, HR) or owner-only tools")]))
    return out, len(rows) < limit


class _Source:
    def __init__(self, name: str, kinds: frozenset[str], fetch: Callable[[_Ctx, int], tuple[list[dict[str, Any]], bool]]) -> None:
        self.name, self.kinds, self.fetch = name, kinds, fetch


_ALL = frozenset(KIND_LABELS)
SOURCES = (
    _Source("action", frozenset({"draft", "email", "job_proposal", "fsm_change", "code_change", "other"}), _src_actions),
    _Source("check", frozenset({"scheduled_check", "settings_change"}), _src_checks),
    _Source("suggestion", frozenset({"suggestion"}), _src_suggestions),
    _Source("agent_run", frozenset({"code_change", "other"}), _src_agent_runs),
    _Source("background", frozenset({"other"}), _src_background),
    _Source("memory", frozenset({"memory"}), _src_memory),
    _Source("audit", frozenset({"settings_change", "memory", "other", "draft"}), _src_audit),
    _Source("document", frozenset({"draft"}), _src_documents),
    _Source("advert", frozenset({"draft"}), _src_adverts),
    _Source("upsell", frozenset({"draft"}), _src_upsell),
    _Source("automation", frozenset({"other"}), _src_automations),
)


# ------------------------------------------------------------------------------------------------ the feed
class ActivityFeed:
    def __init__(self, j: Any) -> None:
        self.j = j
        self._recorded = 0

    # -- helpers
    def tz(self) -> ZoneInfo:
        return _zone(getattr(self.j.settings, "timezone", "UTC"))

    def sample_sources(self) -> set[str]:
        """Which sources are showing sample data right now (so a record built on one is flagged, and left out of what Jarvis says aloud)."""
        out = set(demo_guard.demo_now(self.j))
        if not demo_guard.sample_on(self.j):
            return out  # sample data off: nothing is sample (an unconnected FSM / mailbox serves nothing at all)
        for name, attr in (("fsm", "fsm"), ("mail", "mail")):
            try:
                if getattr(getattr(self.j, attr), "demo", False):
                    out.add(name)
            except Exception:  # noqa: BLE001
                continue
        return out

    def window(self, rng: str) -> tuple[str, str, str]:
        return window(rng, self.tz())

    # -- recording (the small audit trail for changes that left no trace anywhere else)
    def record(self, kind: str, actor: str, what: str, ref: str = "") -> None:
        """One audit line: who changed what. Names and times only - callers must never pass a value, a code or a message. Never raises."""
        try:
            clean = Cleaner(secret_values(self.j.settings))
            self.j.db.add_audit_event(kind, clean.text(actor or "the owner", 80), clean.text(what, 300), clean.text(ref, 80))
            self._recorded += 1
            if self._recorded % 50 == 1:
                cutoff = datetime.now(timezone.utc) - timedelta(days=AUDIT_RETENTION_DAYS)
                self.j.db.prune_audit_events(cutoff.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
        except Exception:  # noqa: BLE001 - writing a line must never break the change it describes
            log.exception("Could not record an audit line")

    # -- the merge
    def _keep(self, item: dict[str, Any], q: Query, ctx: _Ctx) -> bool:
        if item["quiet"] and not q.everything:
            return False
        if q.kinds and item["kind"] not in q.kinds:
            return False
        if q.statuses:
            ok = item["status"] in q.statuses or (NEEDS_LOOK in q.statuses and item["attention"])
            if not ok:
                return False
        if q.drop_sample and item["sample"]:
            return False
        if q.who:
            hay = f"{item['requested_by']} | {item['decided_by']}".lower()
            if q.who not in hay:
                return False
        if q.text:
            self._finish(item)
            parts = [item["what"], item["kind_label"], item["status_label"], item["requested_by"], item["decided_by"], item["error"],
                     item["source_ref"], *(f"{d['label']} {d['value']}" for d in item["detail"] or ())]
            if q.text not in " ".join(parts).lower():
                return False
        return True

    @staticmethod
    def _finish(item: dict[str, Any]) -> dict[str, Any]:
        build = item.pop("_build", None)
        if build is not None:
            item["detail"] = build()
        return item

    def _collect(self, q: Query, need: int) -> tuple[list[dict[str, Any]], bool, int]:
        """(the newest `need` items that match, whether a source ran into SCAN_CAP before it could say there were no more, rows read)."""
        ctx = _Ctx(self, q)
        lists: list[list[dict[str, Any]]] = []
        capped, scanned = False, 0
        for src in SOURCES:
            if q.kinds and not (q.kinds & src.kinds):
                continue
            limit = min(SCAN_CAP, max(need, 60))
            while True:
                rows, exhausted = src.fetch(ctx, limit)
                kept = [it for it in rows if self._keep(it, q, ctx)]
                if len(kept) >= need or exhausted or limit >= SCAN_CAP:
                    break
                limit = min(SCAN_CAP, limit * 4)
            scanned += len(rows)
            if not exhausted and len(kept) < need and limit >= SCAN_CAP:
                capped = True                      # more may lie beyond the rows we are allowed to read
            kept.sort(key=_sort_key, reverse=True)
            lists.append(kept[:need])
        merged: list[dict[str, Any]] = []
        for it in heapq.merge(*lists, key=_sort_key, reverse=True):
            merged.append(it)
            if len(merged) >= need:
                break
        return merged, capped, scanned

    # -- the page
    def page(self, q: Query, limit: int = DEFAULT_LIMIT, offset: int = 0, *, summary: bool = True, facets: bool = True) -> dict[str, Any]:
        limit = max(1, min(int(limit), PAGE_MAX))
        offset = max(0, int(offset))
        reach = min(offset + limit + 1, REACH_CAP + 1)
        items, capped, scanned = self._collect(q, reach)
        beyond = len(items) > offset + limit
        reachable = items[offset: offset + limit]
        past_reach = offset + limit >= REACH_CAP and (beyond or capped)
        out = {"items": [self._public(self._finish(it)) for it in reachable], "offset": offset, "limit": limit,
               "next_offset": offset + limit if beyond and offset + limit < REACH_CAP else None,
               "capped": bool(past_reach or (capped and not beyond)), "scanned": scanned}
        if offset == 0:
            # the collapsed "checks with nothing to report" line belongs to an unfiltered-by-text view of checks
            if not q.everything and not q.text and not q.who and (not q.kinds or "scheduled_check" in q.kinds) \
                    and (not q.statuses or "done" in q.statuses):
                out["quiet"] = self.quiet(q)
            if facets:
                out["facets"] = {"who": self.who_options(q)}
        if summary and offset == 0:
            out["summary"] = self.summary("today", owner=q.owner)
        return out

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in item.items() if not k.startswith("_")}

    # -- quiet checks: one collapsed line, counted in SQL (never read row by row)
    def quiet(self, q: Query) -> dict[str, Any]:
        sql = "SELECT job_key, job_name, COUNT(*) AS n, MAX(ran_at) AS last FROM check_runs WHERE ran_at >= ? AND outcome IN ('no_change', 'baseline')"
        params: list[Any] = [q.since]
        if q.until:
            sql += " AND ran_at < ?"
            params.append(q.until)
        if not q.owner and OWNER_ONLY_JOBS:
            sql += f" AND job_key NOT IN ({','.join('?' * len(OWNER_ONLY_JOBS))})"
            params += sorted(OWNER_ONLY_JOBS)
        rows = self.j.db.query(sql + " GROUP BY job_key, job_name ORDER BY last DESC LIMIT 60", tuple(params))
        clean = Cleaner(secret_values(self.j.settings))
        jobs = [{"key": clean.text(r["job_key"], 60), "name": clean.text(r["job_name"], 80), "count": int(r["n"]), "last": utc(r["last"])}
                for r in rows]
        total = sum(j["count"] for j in jobs)
        return {"count": total, "jobs": jobs, "note": "Checks that found nothing are kept for 7 days."}

    def who_options(self, q: Query) -> list[str]:
        names = ["Jarvis", "Scheduled job", "Standing approval"]
        sql = "SELECT DISTINCT approved_by AS n FROM pending_actions WHERE COALESCE(NULLIF(decided_at, ''), created_at) >= ? AND approved_by != '' LIMIT 40"
        clean = Cleaner(secret_values(self.j.settings))
        for r in self.j.db.query(sql, (q.since,)):
            n = str(r["n"])
            if not n.startswith(sa.APPROVER_PREFIX):
                names.append(clean.text(n, 60))
        for r in self.j.db.query("SELECT DISTINCT actor AS n FROM audit_events WHERE at >= ? LIMIT 40", (q.since,)):
            names.append(clean.text(r["n"], 60))
        seen: set[str] = set()
        return [n for n in names if n and not (n.lower() in seen or seen.add(n.lower()))][:60]

    # -- the one-line summary
    def counts(self, rng: str, owner: bool = False, drop_sample: bool = False) -> dict[str, Any]:
        since, until, label = self.window(rng)
        q = Query(since, until, owner=owner, drop_sample=drop_sample)
        items, capped, _ = self._collect(q, REACH_CAP)
        by_status: dict[str, int] = {}
        proposals = others = 0
        for it in items:
            by_status[it["status"]] = by_status.get(it["status"], 0) + 1
            if it["source"] == "action":
                proposals += 1
            else:
                others += 1
        quiet = self.quiet(q)["count"]
        return {"range": rng, "label": label, "since": since, "until": until, "proposals": proposals, "other_changes": others,
                "by_status": by_status, "quiet_checks": quiet, "capped": capped, "items": items}

    def summary(self, rng: str = "today", owner: bool = False) -> dict[str, Any]:
        c = self.counts(rng, owner=owner)
        s = c["by_status"]
        return {"range": rng, "label": c["label"], "line": summary_line(c), "proposals": c["proposals"],
                "approved": s.get("approved", 0) + s.get("auto_approved", 0), "auto_approved": s.get("auto_approved", 0),
                "declined": s.get("declined", 0), "waiting": s.get("waiting", 0), "failed": s.get("failed", 0),
                "quiet_checks": c["quiet_checks"], "other_changes": c["other_changes"]}

    # -- the voice answer
    def spoken(self, rng: str = "today") -> str:
        """The short spoken-style answer for "what did you do today / yesterday / this week". Records that only involved sample data
        (demo_guard) are left out and said to be; the owner-only audit lines are never part of it."""
        real = self.counts(rng, owner=False, drop_sample=True)
        everything = self.counts(rng, owner=False, drop_sample=False)
        return spoken_answer(real, len(everything["items"]) - len(real["items"]))

    # -- export
    def export_csv(self, q: Query, actor: str) -> tuple[str, bool]:
        """(CSV text, whether it was cut at EXPORT_MAX rows). The same redacted items the page shows, one row each; no coordinates, no secrets."""
        items, capped, _ = self._collect(q, EXPORT_MAX + 1)
        cut = len(items) > EXPORT_MAX
        tz = self.tz()
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(CSV_COLUMNS)
        for it in items[:EXPORT_MAX]:
            self._finish(it)
            try:
                local = datetime.fromisoformat(it["when"]).astimezone(tz).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                local = ""
            detail = " | ".join(f"{d['label']}: {' '.join(str(d['value']).split())}" for d in it["detail"] or ())[:CSV_DETAIL_CHARS]
            w.writerow([csv_cell(v) for v in (it["id"], it["when"], local, it["kind_label"], it["status_label"], it["what"],
                                              it["requested_by"], it["decided_by"], it["decided_at"], it["source_ref"], detail, it["error"])])
        self.record("export", actor, f"Exported the activity list as CSV ({min(len(items), EXPORT_MAX)} rows)")
        return buf.getvalue(), cut or capped


CSV_COLUMNS = ["id", "when (UTC)", "when (local)", "kind", "status", "what", "requested by", "decided by", "decided at (UTC)",
               "source", "detail", "error"]
CSV_DETAIL_CHARS = 600


def csv_cell(value: Any) -> str:
    """One CSV cell, safe to open in a spreadsheet: a cell that would be read as a formula (starts with = + - @, or a tab or return,
    even after spaces) gets a leading apostrophe, and control characters are removed."""
    s = _CONTROL.sub("", "" if value is None else str(value))
    if s.lstrip()[:1] in ("=", "+", "-", "@") or s[:1] in ("\t", "\r"):
        s = "'" + s
    return s


# ------------------------------------------------------------------------------------------------ words
def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def summary_line(c: dict[str, Any]) -> str:
    """'Today: 4 proposed, 2 approved, 1 declined, 1 waiting, 0 failed, 3 other changes, 5 checks with nothing to report'."""
    s = c["by_status"]
    parts: list[str] = []
    if c["proposals"]:
        approved = s.get("approved", 0) + s.get("auto_approved", 0)
        parts += [f"{c['proposals']} proposed", f"{approved} approved" + (f" ({s['auto_approved']} automatically)" if s.get("auto_approved") else ""),
                  f"{s.get('declined', 0)} declined", f"{s.get('waiting', 0)} waiting", f"{s.get('failed', 0)} failed"]
        for key, word in (("edited", "edited"), ("dismissed", "dismissed")):
            if s.get(key):
                parts.append(f"{s[key]} {word}")
    if not c["proposals"] and not c["other_changes"]:
        parts.append("nothing proposed or changed")
    if c["other_changes"]:
        parts.append(_n(c["other_changes"], "other change"))
    if c["quiet_checks"]:
        parts.append(f"{_n(c['quiet_checks'], 'check')} with nothing to report")
    return f"{c['label']}: " + (", ".join(parts) if parts else "nothing proposed or changed")


def _short(text: str, limit: int = 90) -> str:
    text = _URL.sub("", text or "")
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut.rstrip(" ,;:-") + "…"


_PHRASE = {"waiting": "waiting for you", "failed": "failed", "approved": "approved", "auto_approved": "went through automatically on a standing approval",
           "declined": "declined", "edited": "edited and re-queued", "dismissed": "dismissed", "done": "done", "running": "still running"}
_WHEN = {"today": "today", "yesterday": "yesterday", "7d": "in the last seven days", "30d": "in the last thirty days"}
WHERE_TO_LOOK = "The full list is under Activity on the left of the console, called What Jarvis did."


def spoken_answer(c: dict[str, Any], left_out: int = 0) -> str:
    """A short answer fit to be read aloud: the counts first, then up to five notable items (failed ones, then waiting ones, then the
    newest), then where to see the rest. Only short plain descriptions are used - no ids, addresses, links, payloads or anything secret."""
    when = _WHEN.get(c["range"], c["label"].lower())
    s, n = c["by_status"], c["proposals"]
    out: list[str] = []
    if n:
        approved = s.get("approved", 0) + s.get("auto_approved", 0)
        bits = []
        if approved:
            bits.append(f"{approved} approved" + (f", {s['auto_approved']} of them automatically under your standing approvals" if s.get("auto_approved") else ""))
        if s.get("declined"):
            bits.append(f"{s['declined']} declined")
        if s.get("edited"):
            bits.append(f"{s['edited']} edited")
        if s.get("waiting"):
            bits.append(f"{s['waiting']} waiting for you")
        out.append(f"{when.capitalize()} I put forward {_n(n, 'thing')}" + (": " + ", ".join(bits) + "." if bits else "."))
        out.append("Nothing failed." if not s.get("failed") else f"{_n(s['failed'], 'thing')} failed.")
    else:
        out.append(f"I haven't put anything forward for approval {when}.")
    if c["other_changes"]:
        out.append(f"I also made {_n(c['other_changes'], 'other change')}.")
    if c["quiet_checks"]:
        out.append(f"{_n(c['quiet_checks'], 'check')} had nothing to report.")
    items = c["items"]                       # newest first
    picks = ([it for it in items if it["status"] == "failed"] + [it for it in items if it["status"] == "waiting"]
             + [it for it in items if it["status"] not in ("failed", "waiting")])[:SPOKEN_ITEMS]
    if picks:
        out.append("Worth knowing: " + "; ".join(f"{it['kind_label'].lower()}, {_short(it['what'])}, {_PHRASE.get(it['status'], it['status'])}" for it in picks) + ".")
    if len(items) > len(picks):
        out.append(f"And {len(items) - len(picks)} more.")
    if left_out:
        out.append(f"I've left out {_n(left_out, 'item')} that only involved sample data, because it isn't real.")
    if c.get("capped"):
        out.append("That's only the newest part of a very long list.")
    out.append(WHERE_TO_LOOK)
    return " ".join(out)
