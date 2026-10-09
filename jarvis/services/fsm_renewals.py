"""Renewals through Salts FSM: Jarvis's half of the FSM renewals contract (salts-fsm ``docs/jarvis_renewals.md``).

Jarvis no longer writes its own renewal letters (the old ``prepare_renewal`` tool is retired). The FSM owns renewals - the draft,
the branded PDF, the customer's accept link and the email - and Jarvis drives them through five key-authenticated routes, all
absolute paths under the FSM base URL via ``FSMClient.jarvis_call``:

* ``GET  /api/jarvis/renewals/due?within_days=N``  contracts due, open renewals by status, what is missing before a send.
* ``POST /api/jarvis/renewals/prepare``  ``{contract_id, uplift_percent? | lines?: [{id, proposed_value}], note?}`` -> the FSM's own
  draft, created or the open one returned (idempotent). It never sends.
* ``GET  /api/jarvis/renewals/{id}/preview``  exactly what the customer would get, and its ``version``.
* ``GET  /api/jarvis/renewals/{id}/pdf``  the PDF exactly as it would be attached.
* ``POST /api/jarvis/renewals/{id}/send``  ``{expected_version, approved_by, recipients?}`` - the FSM's own send, only for a draft,
  only that version (409 ``changed`` otherwise), recorded in the FSM with the person who approved it.

The rules here, in one place:

* **Preparing a draft runs without approval**, exactly like Jarvis's other FSM draft writes (the upsell draft wording and the Action
  Centre suggestions are written with no approval card): a draft is something the office already makes freely, it is visible in
  the FSM, and it sends nothing. The tool is owner / manager only (not in ``TEAM_TOOLS``) and blocked in check mode.
* **Sending ALWAYS waits for a person.** ``queue_send`` only builds the card (customer, recipients, subject, an excerpt of the email,
  the money now and next year, a link to the actual FSM PDF, the preview version) and queues kind ``fsm_renewal_send`` through
  ``ActionExecutor.queue``. Standing approvals never match it (they only look at ``fsm_write`` and ``po_acknowledgement``), a team
  request is never even offered one, and ``execute_send`` refuses an action that was not approved by a person. When the approval
  runs, the FSM is asked to send THAT version: if anything changed since (409), the action fails saying so and asks for a fresh
  preview - nothing is sent.
* Nothing here approves, denies or retries anything; there is no email code here (the FSM sends its own email).
* A FSM without these routes yet (404 / 405 with no error code of ours) is "FSM renewals API not available yet", never an error;
  sample data is never a source (``j.fsm.demo``).
* Everything the FSM returns is untrusted text: cleaned, capped and redacted before it reaches a prompt or a card.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from ..integrations.fsm_data import clean_document_text, clean_text

log = logging.getLogger(__name__)

RENEWALS_PATH = "/api/jarvis/renewals"
SEND_KIND = "fsm_renewal_send"            # the approval kind; never in the standing-approvals allowlist
PDF_ROUTE = "/api/fsm/renewals/{id}/pdf"  # Jarvis's own owner/manager route that serves the FSM's PDF (main.py)
UNAVAILABLE = "FSM renewals API not available yet"
NOT_CONNECTED = "Salts FSM isn't connected to me, so I can't see or prepare its renewals."
MAX_PDF_BYTES = 10 * 1024 * 1024
TIMEOUT_S = 30.0
EXCERPT_CHARS = 700
MAX_DEPTH = 6
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,79}")
_VERSION = re.compile(r"[0-9a-f]{64}")
# The FSM's own error codes on a 404: anything else on a 404 / 405 means the route itself is not there yet.
_KNOWN_404 = {"contract_not_found", "renewal_not_found", "document_not_found", "not_found"}


class RenewalsError(Exception):
    """The FSM did not do it. ``message`` is plain English for the owner and carries no URL or key."""

    def __init__(self, kind: str, message: str, *, status: int | None = None, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.kind, self.message, self.status, self.extra = kind, message, status, extra or {}

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.message, "kind": self.kind, **self.extra}


def valid_id(value: Any) -> str:
    v = str(value or "").strip()
    if not _ID.fullmatch(v):
        raise RenewalsError("bad_request", "That is not a Salts FSM id.")
    return v


def clean(value: Any, depth: int = 0) -> Any:
    """FSM data made safe to hand on (strings cleaned, capped and redacted; nesting kept a few levels deep)."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if depth >= MAX_DEPTH:
        return clean_text(value)
    if isinstance(value, dict):
        return {clean_text(k, 64): clean(v, depth + 1) for k, v in list(value.items())[:80] if clean_text(k, 64)}
    if isinstance(value, (list, tuple)):
        return [clean(v, depth + 1) for v in list(value)[:200]]
    return clean_text(value)


def money(v: Any) -> str:
    try:
        return f"£{float(v):,.2f}"
    except (TypeError, ValueError):
        return "£?"


def approver_name(approved_by: str, owner_name: str) -> str:
    """Who approved it, as the FSM records it and signs the email: the console's owner session is "the owner" - send the owner's name."""
    who = re.sub(r"\s+", " ", str(approved_by or "")).strip()
    if not who or who.lower() in ("the owner", "owner"):
        who = (owner_name or "").strip() or "the owner"
    return who[:80]


class FsmRenewals:
    def __init__(self, j) -> None:
        self.j = j

    # ---------------------------------------------------------------------------------------------- talking to the FSM
    def _ready(self) -> None:
        if getattr(self.j.fsm, "demo", True) or not self.j.settings.fsm_configured:
            raise RenewalsError("demo", NOT_CONNECTED)

    async def _response(self, method: str, path: str, body: dict[str, Any] | None = None,
                        params: dict[str, Any] | None = None) -> httpx.Response:
        self._ready()
        try:
            r = await self.j.fsm.jarvis_call(method, path, body, params, timeout=TIMEOUT_S)
        except (httpx.HTTPError, OSError) as e:
            raise RenewalsError("unreachable", f"I couldn't reach Salts FSM just now ({type(e).__name__}). Try again in a minute.") from None
        if 200 <= r.status_code < 300:
            return r
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        code = str(payload.get("error") or "")
        words = clean_text(payload.get("message") or payload.get("detail") or "", 400)
        if r.status_code in (404, 405) and code not in _KNOWN_404:
            raise RenewalsError("unavailable", UNAVAILABLE + ": Salts FSM needs the update that adds the Jarvis renewals routes.",
                                status=r.status_code)
        if r.status_code == 401:
            raise RenewalsError("refused", "Salts FSM refused my key, so I couldn't do that.", status=401)
        if r.status_code == 403 and code == "scope_off":
            raise RenewalsError("scope_off", "Commercial is switched off for me in Salts FSM (Settings > Integrations > Jarvis access).",
                                status=403, extra={"group": "commercial"})
        if r.status_code == 403 and code == "send_off":
            raise RenewalsError("send_off", "Salts FSM has \"Jarvis may send renewals\" switched off (Settings > Integrations > Jarvis "
                                            "access), so it won't let me send it. Send it from Salts FSM, or switch that on first.",
                                status=403)
        if r.status_code == 429:
            raise RenewalsError("rate_limited", "Salts FSM asked me to slow down. Try again in a minute.", status=429)
        if r.status_code == 404:
            raise RenewalsError("not_found", words or "Salts FSM has no such record.", status=404)
        if r.status_code == 409:
            kind = code if code in ("changed", "already_sent", "not_sendable") else "conflict"
            extra = {"current_version": str(payload.get("current_version") or "")[:64]} if kind == "changed" else {}
            raise RenewalsError(kind, words or "Salts FSM refused it because the renewal is not in a state to do that.",
                                status=409, extra=extra)
        if r.status_code == 400:
            raise RenewalsError("bad_request", words or "Salts FSM refused that request.", status=400)
        if r.status_code == 502 and code == "send_failed":
            raise RenewalsError("send_failed", words or "The email did not go, so nothing was changed.", status=502)
        raise RenewalsError("unreachable", f"Salts FSM answered {r.status_code}" + (f": {words}" if words else "") + ".",
                            status=r.status_code)

    async def _raw(self, method: str, path: str, body: dict[str, Any] | None = None,
                   params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = await self._response(method, path, body, params)
        try:
            payload = r.json()
        except ValueError:
            raise RenewalsError("unreachable", "Salts FSM sent back something I couldn't read.") from None
        if not isinstance(payload, dict):
            raise RenewalsError("unreachable", "Salts FSM sent back something I couldn't read.")
        return payload

    async def _json(self, method: str, path: str, body: dict[str, Any] | None = None,
                    params: dict[str, Any] | None = None) -> dict[str, Any]:
        return clean(await self._raw(method, path, body, params))

    # ---------------------------------------------------------------------------------------------- the five calls
    async def due(self, within_days: int | None = None) -> dict[str, Any]:
        days = within_days or self.j.settings.renewal_notice_days or 60
        days = max(1, min(int(days), 365))
        return await self._json("GET", f"{RENEWALS_PATH}/due", params={"within_days": days})

    async def prepare(self, contract_id: str, uplift_percent: float | None = None,
                      lines: list[dict[str, Any]] | None = None, note: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"contract_id": valid_id(contract_id)}
        if uplift_percent is not None:
            body["uplift_percent"] = round(float(uplift_percent), 2)
        if lines:
            body["lines"] = [{"id": valid_id(l.get("id")), "proposed_value": round(float(l.get("proposed_value")), 2)} for l in lines]
        if note:
            body["note"] = clean_text(note, 300)
        return await self._json("POST", f"{RENEWALS_PATH}/prepare", body)

    async def preview(self, renewal_id: str) -> dict[str, Any]:
        rid = valid_id(renewal_id)
        raw = await self._raw("GET", f"{RENEWALS_PATH}/{quote(rid, safe='')}/preview")
        out = clean({k: v for k, v in raw.items() if k not in ("body_html", "body_text")})
        out["body_text"] = clean_document_text(raw.get("body_text") or "", 6000)   # the email keeps its line breaks
        return out

    async def pdf(self, renewal_id: str) -> bytes:
        rid = valid_id(renewal_id)
        r = await self._response("GET", f"{RENEWALS_PATH}/{quote(rid, safe='')}/pdf")
        data = r.content or b""
        if not data.startswith(b"%PDF") or len(data) > MAX_PDF_BYTES:
            raise RenewalsError("unreachable", "Salts FSM did not send back a PDF I can show.")
        return data

    async def send(self, renewal_id: str, expected_version: str, approved_by: str,
                   recipients: list[str] | None = None) -> dict[str, Any]:
        rid = valid_id(renewal_id)
        if not _VERSION.fullmatch(str(expected_version or "")):
            raise RenewalsError("bad_request", "The approval card has no preview version, so I won't send it. Ask me to send it again.")
        body: dict[str, Any] = {"expected_version": expected_version, "approved_by": clean_text(approved_by, 80)}
        if recipients:
            body["recipients"] = [clean_text(x, 254) for x in recipients]
        return await self._json("POST", f"{RENEWALS_PATH}/{quote(rid, safe='')}/send", body)

    # ---------------------------------------------------------------------------------------------- the approval card
    def _pending_for(self, renewal_id: str, version: str) -> int | None:
        for a in self.j.db.pending_actions():
            p = a.get("payload") or {}
            if a.get("kind") == SEND_KIND and p.get("renewal_id") == renewal_id and p.get("version") == version:
                return a["id"]
        return None

    async def queue_send(self, renewal_id: str, recipients: list[str] | None = None) -> dict[str, Any]:
        """Preview the renewal in the FSM and queue ONE approval card for sending exactly that. Sends nothing."""
        v = await self.preview(renewal_id)
        rid = str(v.get("id") or renewal_id)
        why = [str(w) for w in (v.get("why_not") or [])]
        if why:
            raise RenewalsError("not_sendable", "Salts FSM says it can't be sent yet: " + " ".join(why), extra={"why_not": why})
        if not v.get("send_enabled"):
            raise RenewalsError("send_off", "Salts FSM has \"Jarvis may send renewals\" switched off (Settings > Integrations > Jarvis "
                                            "access), so I haven't queued it. The renewal is ready in Salts FSM - send it from there, "
                                            "or switch that on and ask me again.")
        version = str(v.get("version") or "")
        if not _VERSION.fullmatch(version):
            raise RenewalsError("unreachable", "Salts FSM's preview had no version, so I won't queue a send.")
        to = [clean_text(x, 254) for x in (recipients or v.get("recipients") or [])]
        if not to:
            raise RenewalsError("not_sendable", "There is nobody to send it to.")
        existing = self._pending_for(rid, version)
        if existing is not None:
            return {"queued_action": existing, "already_queued": True,
                    "note": f"It is already waiting for approval as action #{existing}. Nothing has been sent."}
        customer = str(v.get("customer") or "")
        body_text = str(v.get("body_text") or "")
        excerpt = body_text if len(body_text) <= EXCERPT_CHARS else body_text[:EXCERPT_CHARS].rstrip() + " …"
        payload = {
            "renewal_id": rid, "version": version, "recipients": to,
            "recipients_are_own": not recipients, "customer": customer,
            "contract": str(v.get("contract_name") or ""), "site": str(v.get("site") or ""),
            "subject": str(v.get("subject") or ""), "body_excerpt": excerpt,
            "current_total": v.get("current_total"), "proposed_total": v.get("proposed_total"),
            "change_percent": v.get("change_percent"), "vat": v.get("vat"), "total_inc_vat": v.get("total_inc_vat"),
            "new_term_start": str(v.get("new_term_start") or ""), "renewal_date": str(v.get("renewal_date") or ""),
            "pdf": PDF_ROUTE.format(id=quote(rid, safe="")),
            "pdf_filename": str((v.get("pdf") or {}).get("filename") or "renewal.pdf"),
        }
        summary = (f"Send the Salts FSM renewal for {customer or 'the customer'}"
                   + (f" ({payload['contract']})" if payload["contract"] else "")
                   + f": {money(v.get('current_total'))} now -> {money(v.get('proposed_total'))} a year + VAT, to {', '.join(to)}")
        action_id = self.j.actions.queue(SEND_KIND, summary[:300], payload)
        return {"queued_action": action_id, "renewal_id": rid, "customer": customer, "recipients": to,
                "subject": payload["subject"], "current_total": v.get("current_total"), "proposed_total": v.get("proposed_total"),
                "preview_version": version[:12],
                "note": "Waiting for your approval - nothing has been sent. Salts FSM sends its own renewal email with its PDF when "
                        "you approve, and only if nothing has changed since this preview."}

    async def execute_send(self, action: dict[str, Any], standing_prefix: str) -> str:
        """Run an APPROVED ``fsm_renewal_send``: ask the FSM to send exactly the version on the card. Raises with a plain reason."""
        by = str(action.get("approved_by") or "")
        if not by or by.startswith(standing_prefix):
            raise RuntimeError("A renewal is only ever sent after a person approves it - this one was not, so nothing was sent.")
        p = action.get("payload") or {}
        who = approver_name(by, self.j.settings.owner_name)
        try:
            out = await self.send(p.get("renewal_id"), p.get("version"), who,
                                  None if p.get("recipients_are_own") else p.get("recipients"))
        except RenewalsError as e:
            if e.kind == "changed":
                raise RuntimeError("Not sent: the renewal has changed in Salts FSM since this card was made (its prices, lines, "
                                   "recipients, dates or email are different now). Ask me to send the renewal again so you can "
                                   "check the new version and approve that one.") from None
            if e.kind == "already_sent":
                raise RuntimeError("Not sent: Salts FSM says this renewal has already been sent. Jarvis never sends a renewal "
                                   "twice - send it again from Salts FSM if you need to.") from None
            raise RuntimeError(f"Not sent: {e.message}") from None
        sent_to = ", ".join(str(x) for x in (out.get("sent_to") or [])) or ", ".join(p.get("recipients") or [])
        return (f"Salts FSM sent the renewal for {p.get('customer') or 'the customer'} to {sent_to} "
                f"(approved by {who}); it is marked Sent in Salts FSM with the PDF kept.")


def card_rows(p: dict[str, Any]) -> list[tuple[str, Any, bool, str]]:
    """(label, value, block, href) rows for the approval card, from the stored payload alone."""
    change = p.get("change_percent")
    change_txt = f" ({change:+.1f}%)" if isinstance(change, (int, float)) else ""
    start = f" from {p.get('new_term_start')}" if p.get("new_term_start") else ""
    rows: list[tuple[str, Any, bool, str]] = [
        ("Customer", p.get("customer") or "", False, ""),
        ("Contract", " - ".join(x for x in (p.get("contract") or "", p.get("site") or "") if x), False, ""),
        ("Send to", ", ".join(map(str, p.get("recipients") or [])), False, ""),
        ("Annual price", f"{money(p.get('current_total'))} now -> {money(p.get('proposed_total'))}{start}{change_txt}, plus VAT",
         False, ""),
        ("Subject", p.get("subject") or "", False, ""),
        ("Message (start)", p.get("body_excerpt") or "", True, ""),
        ("PDF", f"{p.get('pdf_filename') or 'renewal.pdf'} - the PDF exactly as Salts FSM will attach it", False, str(p.get("pdf") or "")),
        ("Preview version", str(p.get("version") or "")[:12], False, ""),
        ("What happens", "Salts FSM sends its own renewal email with this PDF and marks it Sent - only if nothing has changed since "
                         "this preview, and never a second time.", True, ""),
    ]
    return rows


def teams_text(p: dict[str, Any]) -> str:
    return (f"Renewal for {p.get('customer', '')} ({p.get('contract', '')}): {money(p.get('current_total'))} -> "
            f"{money(p.get('proposed_total'))} a year + VAT - to {', '.join(map(str, p.get('recipients') or []))} - subject: "
            f"{p.get('subject', '')} - {p.get('body_excerpt', '')} - PDF in the Jarvis console - preview version "
            f"{str(p.get('version') or '')[:12]}")

