"""Standing approvals: the OWNER's own advance approval for two narrow classes of action.

The rule everything bends around still holds: nothing changes anything without the owner's approval. A standing
approval is that approval given IN ADVANCE, by the owner, on the Settings page, for a closed list of low-risk
actions - it is not Jarvis approving itself. Hence:

* The two switches (`standing_record_keeping`, `standing_acknowledgements`) default OFF and can only be changed on
  the owner-authenticated Settings page (`settings_store.OWNER_ONLY_KEYS`, enforced in `main.save_settings`).
  Nothing in this module, in `brain/tools.py`, in a pending-action payload or in the Teams path writes them.
* The allowlist below is CLOSED and matches narrowly on (action kind, method, exact path shape, body keys, field
  values). Anything unknown, or anything that is not exactly an allowed shape, simply returns None here and is
  queued for a human exactly as before. There is no "else: allow".
* Money (invoices, Sage, supplier POs, stock), deletes, job creation/assignment/scheduling (`log_job`,
  `accept_quote`, `accept_quote_from_po`), `email_send`, `deploy_fix`, every `tool:*` action, and settings /
  accreditation / staff edits are never matched, because only `fsm_write` and `po_acknowledgement` are even looked at.
* The payload that matches is the payload that runs: `ActionExecutor.queue()` decides on the canonical JSON
  round-trip of the payload, stores that, and `_run` re-checks the stored row before executing it.
* A rolling per-hour cap (`standing_max_per_hour`, default 20) blunts a runaway loop or an injected flood: past it
  actions queue for a human and a warning is raised.

Free-text fields are length-limited, and anything with a URL, HTML angle brackets, control characters or invisible /
direction-override characters is NOT auto-run (it queues for a human instead). That is a refusal heuristic, not a
guarantee: stored text is still data from an untrusted source and Jarvis treats it as such when reading it back.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

RECORD_KEEPING = "record keeping"
ACKNOWLEDGEMENTS = "routine acknowledgements"
CATEGORIES = (RECORD_KEEPING, ACKNOWLEDGEMENTS)
APPROVER_PREFIX = "standing approval: "  # what pending_actions.approved_by holds for an automatic run
PO_ACK_KIND = "po_acknowledgement"

_ID = r"[A-Za-z0-9][A-Za-z0-9_\-]{0,63}"
# All of these are used with .fullmatch(): `$` would let a trailing newline through ("a@b.co\n").
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-']{1,64}@[A-Za-z0-9.\-]{1,100}\.[A-Za-z]{2,24}")
_PHONE = re.compile(r"[0-9 +()\-]{5,25}")
_DUE = re.compile(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?", re.ASCII)
_ID_RE = re.compile(_ID)
AUTO_MARK = "[Added automatically by Jarvis]"  # fixed visible prefix on every auto-written note / task / reminder
_URL_LIKE = re.compile(r"(?i)(://|\bwww\.|\bmailto:|\bjavascript:|\bdata:|\bfile:)")
# C0/C1 controls except tab/newline/carriage return; zero-width, bidi and other invisible formatting characters.
_ODD_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏  ‪-‮⁠-⁯﻿]")
_MAX_PAYLOAD_CHARS = 6000
# multi-line allowed (a billing address is "1 High Street\nLeeds\nLS1 2AB"):
LONG_FIELDS = {"text", "description", "note", "notes", "address", "billingAddress"}
SHORT_MAX, LONG_MAX = 200, 2000


@dataclass(frozen=True)
class Shape:
    """One allowed `fsm_write` POST: the exact path pattern, the only body keys it may carry, and the one it needs."""
    label: str
    path: re.Pattern[str]
    keys: frozenset[str]
    required: str
    undo: str
    marked: str = ""  # a field that must start with AUTO_MARK so staff can tell Jarvis wrote it, not a person
    fixed: tuple[tuple[str, str], ...] = ()  # (key, value) pairs that must be present with exactly this value


SHAPES: tuple[Shape, ...] = (
    # Exactly the bodies the create_customer / create_site tools queue (brain/tools.py): `created_by` is always
    # "Jarvis" (the visible authorship marker). `confirmSharedName` - the flag that deliberately creates a namesake
    # of an existing customer/site - is NOT in the key list, so a payload carrying it is never auto-run.
    Shape("customer", re.compile(r"/customers"),
          frozenset({"name", "created_by", "contact", "phone", "email", "billingAddress", "notes"}), "name",
          "Undo: it is only a new record - remove it in Salts FSM (Jarvis cannot delete things).",
          fixed=(("created_by", "Jarvis"),)),
    Shape("site", re.compile(r"/sites"),
          frozenset({"name", "created_by", "customer", "address", "postcode", "notes"}), "name",
          "Undo: it is only a new record - remove it in Salts FSM (Jarvis cannot delete things).",
          fixed=(("created_by", "Jarvis"),)),
    Shape("contact", re.compile(rf"/(?:customers|sites)/{_ID}/contacts"),
          frozenset({"name", "email", "phone", "role"}), "name",
          "Undo: it is only a new record - remove it in Salts FSM (Jarvis cannot delete things)."),
    Shape("note", re.compile(rf"/(?:jobs|customers|sites)/{_ID}/notes"), frozenset({"text", "author"}), "text",
          "Undo: it is only a note - remove it in Salts FSM (Jarvis cannot delete things).", marked="text"),
    Shape("task", re.compile(r"/tasks"), frozenset({"title", "description", "due"}), "title",
          "Undo: it is only a task - remove it in Salts FSM (Jarvis cannot delete things).", marked="title"),
    Shape("reminder", re.compile(r"/reminders"), frozenset({"title", "note", "due"}), "title",
          "Undo: it is only a reminder - remove it in Salts FSM (Jarvis cannot delete things).", marked="title"),
)


@dataclass(frozen=True)
class Decision:
    category: str | None = None  # set only when the action may run automatically right now
    rate_limited: bool = False  # it matched an ENABLED category but the hourly cap is used up
    matched: str | None = None  # the category it matched, whether or not that category is switched on


def _clean_text(value: Any, key: str) -> str | None:
    """The string, or None if it fails any of the rules for this field (type, length, characters, URLs)."""
    if not isinstance(value, str):
        return None
    limit = LONG_MAX if key in LONG_FIELDS else SHORT_MAX
    if not value.strip() or len(value) > limit or _ODD_CHARS.search(value):
        return None
    if key not in LONG_FIELDS and ("\n" in value or "\r" in value or "\t" in value):
        return None
    if "<" in value or ">" in value:
        return None
    if key == "email":
        return value if _EMAIL.fullmatch(value) else None
    if _URL_LIKE.search(value):
        return None
    if key == "phone" and not _PHONE.fullmatch(value):
        return None
    if key == "due" and not _DUE.fullmatch(value):
        return None
    return value


def _match_record(payload: dict[str, Any]) -> tuple[Shape, str] | None:
    """(shape, description) if this fsm_write payload is exactly one of the allowed record-keeping creations."""
    if set(payload) - {"method", "path", "body"}:
        return None
    if payload.get("method") != "POST":  # exact: no PUT/PATCH/DELETE, no lower-case variants
        return None
    path, body = payload.get("path"), payload.get("body")
    if not isinstance(path, str) or not isinstance(body, dict) or not body:
        return None
    if len(json.dumps(payload, default=str)) > _MAX_PAYLOAD_CHARS:
        return None
    shape = next((s for s in SHAPES if s.path.fullmatch(path)), None)
    if shape is None:
        return None
    if not set(body) <= shape.keys or shape.required not in body:
        return None
    for key, value in body.items():
        if _clean_text(value, key) is None:
            return None
    if any(body.get(k) != v for k, v in shape.fixed):
        return None
    if shape.marked and not str(body.get(shape.marked, "")).startswith(AUTO_MARK + " "):
        return None  # a note/task/reminder must visibly say Jarvis wrote it, or it waits for a human
    target = path.strip("/").split("/")
    where = f" ({target[0][:-1]} {target[1]})" if len(target) == 3 and shape.label in ("contact", "note") else ""
    name = str(body.get(shape.required)).removeprefix(AUTO_MARK).strip()[:60].replace("\n", " ")
    return shape, f"Created {shape.label} '{name}'{where}" if shape.label in ("customer", "site", "contact") \
        else f"Added {shape.label} '{name}'{where}"


def _match_acknowledgement(payload: dict[str, Any], db) -> bool:
    # Exactly these three keys, and no free text at all: the email is a fixed template (acknowledgement_email), so
    # nothing an outsider wrote (display name, PO number, subject) can end up in it.
    if set(payload) != {"to", "quote_id", "source_message_id"}:
        return False
    to, source, quote_id = payload.get("to"), payload.get("source_message_id"), payload.get("quote_id")
    if not (isinstance(to, str) and _EMAIL.fullmatch(to) and isinstance(source, str) and 0 < len(source) <= 300
            and isinstance(quote_id, str) and _ID_RE.fullmatch(quote_id)):
        return False  # `to` is one plain address: no CR/LF, commas, spaces, angle brackets or display name
    # "An already-matched PO email": po_intake records which sender / quote it matched under this message id before
    # queueing anything. The reply may only go to that same sender, about that same quote.
    try:
        match = json.loads(db.get_kv(f"po_match:{source}") or "null")
    except (ValueError, TypeError):
        return False
    return (isinstance(match, dict) and str(match.get("from_email", "")).lower() == to.lower()
            and str(match.get("quote_id", "")) == quote_id)


def safe_address(value: Any) -> bool:
    """A single plain email address (what the receipt may be sent to)."""
    return isinstance(value, str) and bool(_EMAIL.fullmatch(value))


def classify(kind: str, payload: Any, db) -> str | None:
    """Which standing category this exact action falls in, if any - ignoring whether it is switched on or not.
    A closed allowlist: only `fsm_write` record creations and `po_acknowledgement` can ever return a category."""
    if not isinstance(payload, dict):
        return None
    if kind == "fsm_write":
        return RECORD_KEEPING if _match_record(payload) else None
    if kind == PO_ACK_KIND:
        return ACKNOWLEDGEMENTS if _match_acknowledgement(payload, db) else None
    return None


def describe(kind: str, payload: dict[str, Any]) -> str:
    """What the action does, built from the payload itself (never the model-written summary)."""
    if kind == "fsm_write":
        hit = _match_record(payload)
        return hit[1] if hit else "Record created"
    if kind == PO_ACK_KIND:
        return f"Sent a receipt-only purchase order acknowledgement to {payload.get('to', '?')}"
    return kind


def undo_hint(kind: str, payload: dict[str, Any]) -> str:
    if kind == "fsm_write":
        hit = _match_record(payload)
        return hit[0].undo if hit else "Undo: remove it in Salts FSM."
    if kind == PO_ACK_KIND:
        return "Undo: an email can't be recalled - if it was wrong, reply to the customer."
    return ""


def acknowledgement_email(payload: dict[str, Any]) -> tuple[str, str]:
    """(subject, html) for a receipt-only PO acknowledgement. A completely fixed template: it interpolates NOTHING
    from the incoming email (no display name, no PO number), and claims nothing beyond "we have your order" - in
    particular never that a job is booked."""
    return ("Purchase order received",
            "<p>Hello,</p><p>Thanks - we've received your purchase order. We're just checking it over "
            "and will confirm separately once it has been processed.</p><p>Kind regards</p>")


class StandingApprovals:
    """Decides, at queue time, whether the owner's standing approval covers an action. Reads the live Settings
    object (so a Settings-page change applies at once) and NEVER writes it."""

    def __init__(self, settings, db):
        self.s = settings
        self.db = db

    def enabled(self, category: str) -> bool:
        flag = {RECORD_KEEPING: self.s.standing_record_keeping, ACKNOWLEDGEMENTS: self.s.standing_acknowledgements}
        return bool(flag.get(category, False))

    def runs_in_last_hour(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        return self.db.count_standing_runs_since(cutoff)

    def decide(self, kind: str, payload: Any) -> Decision:
        category = classify(kind, payload, self.db)
        if category is None or not self.enabled(category):
            return Decision(matched=category)
        limit = max(0, int(self.s.standing_max_per_hour))
        if self.runs_in_last_hour() >= limit:
            return Decision(rate_limited=True, matched=category)
        return Decision(category=category, matched=category)

    def still_valid(self, action: dict[str, Any]) -> bool:
        """Re-checked just before an automatic run executes: the stored row must still be exactly what the owner's
        switch covers."""
        by = str(action.get("approved_by") or "")
        if not by.startswith(APPROVER_PREFIX):
            return False
        category = by[len(APPROVER_PREFIX):]
        return classify(action["kind"], action["payload"], self.db) == category and self.enabled(category)
