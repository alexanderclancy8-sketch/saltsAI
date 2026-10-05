"""The Approvals inbox: what an approval card shows, and what a person may edit before approving it.

Two jobs, both pure functions of a stored action row (no I/O, no model):

* `view(action)` is the ONLY thing the console renders a card from. It is built from the stored payload itself - never
  from the model-written summary alone - so a card says exactly what will happen when Approve is pressed, and every
  string in it goes through the existing secret redaction first (no token, signature or key ever reaches a card).
* `apply_edit(kind, payload, changes)` validates a human's edit and returns the complete NEW payload. It is closed:
  only the kinds in `editable_fields` can be edited, only the listed fields of each, and nothing else in the payload
  (the FSM method and path, the tool name, any other key) can be changed by an edit. The caller
  (`ActionExecutor.edit`) then queues that payload as a brand-new PENDING action; the stored payload of an existing
  action is never modified, which is what makes "the payload that is approved is the payload that was shown" true.

Nothing here approves, denies, runs or queues anything.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from ..integrations.redact import REDACTED as _GITHUB_REDACTED, redact as redact_secrets
from ..redact import REDACTED, redact_text

MAX_TEXT = 20_000          # the longest email body / single text field a card shows or an edit may carry
MAX_JSON = 20_000          # the longest JSON body an edit may carry
MAX_RECIPIENTS = 20
_ADDRESS = re.compile(r"[^@\s<>,;\"'()\[\]\\]{1,64}@[A-Za-z0-9][A-Za-z0-9.\-]{0,200}\.[A-Za-z]{2,24}", re.ASCII)
_CONTROL = re.compile("[" + "".join(re.escape(chr(a)) + "-" + re.escape(chr(b)) for a, b in (
    (0, 8), (11, 12), (14, 31), (127, 159), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0xFEFF, 0xFEFF))) + "]")  # control and invisible characters; keeps tab, newline, carriage return
 

_MARKERS = (REDACTED, _GITHUB_REDACTED)


class EditError(ValueError):
    """The edit is not acceptable. The message is plain English, safe to show the person who made it."""


# ------------------------------------------------------------------------------------------------ redaction
def clean(value: Any) -> Any:
    """A deep copy with every string run through the redactors (and over-long strings cut). Dict keys are kept."""
    if isinstance(value, str):
        out = redact_text(redact_secrets(value))
        return out if len(out) <= MAX_TEXT else out[:MAX_TEXT] + "…[cut]"
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value[:200]]
    return value


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)


# ------------------------------------------------------------------------------------------------ what a card shows
def _tool_label(name: str) -> str:
    try:
        from ..brain.tools import TOOLS_BY_NAME

        tool = TOOLS_BY_NAME.get(name)
        return tool.label if tool else name
    except Exception:  # noqa: BLE001 - a label is decoration
        return name


def _rows(kind: str, p: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """(what kind of thing this is, the exact details), from the payload alone."""
    def row(label: str, value: Any, block: bool = False) -> dict[str, Any]:
        return {"label": label, "value": _text(value), "block": block}

    def email(to, cc, subject, body, extra=()):
        rows = [row("To", ", ".join(map(str, to or [])))]
        if cc:
            rows.append(row("Cc", ", ".join(map(str, cc))))
        rows += [row("Subject", subject or ""), row("Message", body or "", True)]
        return [*rows, *extra]

    if kind == "email_send":
        return "Email", email(p.get("to"), p.get("cc"), p.get("subject"), p.get("body"))
    if kind == "tool:email_send":
        a = p.get("args") or {}
        extra = [row("Management only", "yes")] if a.get("management_only") else []
        return "Email", email(a.get("to"), a.get("cc"), a.get("subject"), a.get("body"), extra)
    if kind == "fsm_write":
        rows = [row("Change", f"{p.get('method', '')} {p.get('path', '')}".strip())]
        if p.get("needs_human_review"):
            rows.append(row("Check first", p["needs_human_review"], True))
        if p.get("body") is not None:
            rows.append(row("Details", p["body"], True))
        return "Change in Salts FSM", rows
    if kind == "sage_invoices":
        jobs = [f"{j.get('job', '')}  {j.get('customer', '')}  £{j.get('net_value', '')} + VAT  ({j.get('site', '')})"
                for j in (p.get("jobs") or []) if isinstance(j, dict)]
        return "Invoices", [row("Invoices to raise", "\n".join(jobs), True)]
    if kind == "review_requests":
        reqs = [f"{r.get('email', '')}  {r.get('site', '')}" for r in (p.get("requests") or []) if isinstance(r, dict)]
        return "Review requests", [row("Emails to send", "\n".join(reqs), True)]
    if kind in ("accept_quote", "accept_quote_from_po"):
        rows = [row("Quote", p.get("quote_id", "")), row("Job to book", p.get("job_body"), True)]
        if p.get("po_number"):
            rows.insert(1, row("Customer PO", p["po_number"]))
        if p.get("ack_to"):
            rows.append(row("Confirmation email to", p["ack_to"]))
        return "Accept quote and book job", rows
    if kind == "po_acknowledgement":
        return "Acknowledgement email", [row("Receipt-only email to", p.get("to", ""))]
    if kind == "deploy_fix":
        rows = [row("Issue", f"#{p.get('issue_id', '?')}"), row("Pull request", f"#{p.get('pr_number', '?')}")]
        if p.get("diff"):
            rows.append(row("Code change", p["diff"], True))
        return "Deploy a fix", rows
    if kind.startswith("tool:"):
        name = str(p.get("tool", kind[5:]))
        return _tool_label(name), [row("Tool", name), row("Details", p.get("args"), True)]
    return kind, [row("Details", p, True)]


# What a human may edit. email_send / tool:email_send: the four email fields. fsm_write: the body only (the method and
# path are what the action IS). Any other tool: its arguments, re-validated against the tool's own input model.
EMAIL_FIELDS = (("to", "To (separate with commas)", "emails"), ("cc", "Cc (separate with commas)", "emails"),
                ("subject", "Subject", "text"), ("body", "Message", "textarea"))


def editable_fields(kind: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The fields an Edit form offers (empty list: this action cannot be edited), prefilled from the REDACTED payload."""
    if kind == "email_send":
        src = clean(payload)
    elif kind == "tool:email_send":
        src = clean(payload.get("args") or {})
    elif kind == "fsm_write":
        if payload.get("method") not in ("POST", "PUT", "PATCH") or not isinstance(payload.get("body"), dict):
            return []
        return [{"key": "body", "label": "Details (JSON)", "type": "json", "value": _text(clean(payload["body"]))}]
    elif kind.startswith("tool:") and _tool_model(kind) is not None:
        return [{"key": "args", "label": "Details (JSON)", "type": "json",
                 "value": _text(clean(payload.get("args") or {}))}]
    else:
        return []
    return [{"key": key, "label": label, "type": typ,
             "value": ", ".join(map(str, src.get(key) or [])) if typ == "emails" else str(src.get(key) or "")}
            for key, label, typ in EMAIL_FIELDS]


def view(action: dict[str, Any]) -> dict[str, Any]:
    """Everything the console needs to draw one card. Safe to send to the browser: secrets are redacted."""
    kind, payload = str(action.get("kind", "")), action.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    status = str(action.get("status", ""))
    label, rows = _rows(kind, clean(payload))
    by = str(action.get("approved_by") or "")
    result = clean(str(action.get("result") or ""))[:1000]
    superseded = action.get("superseded_by")
    return {
        "id": action["id"], "kind": kind, "kind_label": label, "summary": clean(str(action.get("summary", "")))[:500],
        "status": status, "details": rows, "result": result,
        "error": result if status == "failed" else "",
        "created_at": action.get("created_at", ""), "decided_at": action.get("decided_at", ""), "decided_by": by,
        "automatic": by.startswith("standing approval: "),
        "supersedes": action.get("supersedes"), "superseded_by": superseded, "supersede_kind": action.get("supersede_kind") or "",
        "editable_fields": editable_fields(kind, payload) if status == "pending" else [],
        "can_retry": status == "failed" and superseded is None,
    }


def pending_for_display(db) -> list[dict[str, Any]]:
    """The pending actions as the console receives them (WebSocket "approvals" event, /api/approvals, /api/status):
    the usual rows, with every payload redacted. Cards are drawn from `view()`, never from this raw-ish list."""
    return [{**a, "payload": clean(a["payload"])} for a in db.pending_actions()]


def inbox(db, failed_since: str, recent_since: str) -> dict[str, list[dict[str, Any]]]:
    """Everything the Approvals pop-up and the chat cards need in one go: what is waiting, what failed (and can be
    retried), and what was decided recently (so a chat card can turn into "Sent" / "Not sent" / "Failed")."""
    return {"pending": [view(a) for a in db.pending_actions()],
            "failed": [view(a) for a in db.failed_actions(failed_since, 20)],
            "recent": [view(a) for a in db.recent_decided_actions(recent_since, 30)]}


# ------------------------------------------------------------------------------------------------ editing
def _tool_model(kind: str):
    try:
        from ..brain.tools import TOOLS_BY_NAME

        tool = TOOLS_BY_NAME.get(kind[5:])
        return tool.model if tool else None
    except Exception:  # noqa: BLE001
        return None


def _check_text(value: Any, label: str, *, multiline: bool, maximum: int) -> str:
    if not isinstance(value, str):
        raise EditError(f"{label} must be text.")
    if _CONTROL.search(value):
        raise EditError(f"{label} contains characters that can't be sent.")
    if not multiline and ("\n" in value or "\r" in value):
        raise EditError(f"{label} must be on one line.")
    if not value.strip():
        raise EditError(f"{label} can't be empty.")
    if len(value) > maximum:
        raise EditError(f"{label} is too long.")
    if any(m in value for m in _MARKERS):
        raise EditError(f"{label} contains a hidden-secret placeholder. Type the real wording, or leave the field as it was.")
    return value


def _addresses(value: Any, label: str, *, required: bool) -> list[str]:
    items = [s.strip() for s in re.split(r"[,;\n]", value) if s.strip()] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        raise EditError(f"{label} must be a list of email addresses.")
    items = [i.strip() for i in items if i.strip()]
    if required and not items:
        raise EditError(f"{label} needs at least one email address.")
    if len(items) > MAX_RECIPIENTS:
        raise EditError(f"{label} has too many addresses.")
    for i in items:
        if not _ADDRESS.fullmatch(i):
            raise EditError(f"'{i[:60]}' doesn't look like an email address.")
    return items


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise EditError(f"{label} isn't valid JSON.") from None
    if not isinstance(value, dict):
        raise EditError(f"{label} must be a JSON object.")
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > MAX_JSON:
        raise EditError(f"{label} is too long.")
    if any(m in text for m in _MARKERS):
        raise EditError(f"{label} contains a hidden-secret placeholder. Type the real value, or leave it as it was.")
    if _CONTROL.search(text.replace("\\n", "").replace("\\r", "").replace("\\t", "")):
        raise EditError(f"{label} contains characters that can't be sent.")
    return value


def _apply_email(target: dict[str, Any], changes: dict[str, Any]) -> None:
    unknown = set(changes) - {k for k, _, _ in EMAIL_FIELDS}
    if unknown:
        raise EditError("You can only change the recipients, subject and message of an email.")
    if "to" in changes:
        target["to"] = _addresses(changes["to"], "To", required=True)
    if "cc" in changes:
        target["cc"] = _addresses(changes["cc"], "Cc", required=False)
    if "subject" in changes:
        target["subject"] = _check_text(changes["subject"], "The subject", multiline=False, maximum=300)
    if "body" in changes:
        target["body"] = _check_text(changes["body"], "The message", multiline=True, maximum=MAX_TEXT)


def apply_edit(kind: str, payload: dict[str, Any], changes: Any) -> dict[str, Any]:
    """The complete new payload after a human's edit, or EditError. `payload` is not modified."""
    if not isinstance(changes, dict) or not changes:
        raise EditError("There is nothing to change.")
    new = json.loads(json.dumps(payload))          # canonical copy; only the allowed fields are replaced below
    if kind == "email_send":
        _apply_email(new, changes)
    elif kind == "tool:email_send":
        args = new.setdefault("args", {})
        _apply_email(args, changes)
        model = _tool_model(kind)
        try:
            new["args"] = model.model_validate(args).model_dump()
        except ValidationError as e:
            raise EditError(_why(e)) from None
    elif kind == "fsm_write":
        if set(changes) != {"body"} or payload.get("method") not in ("POST", "PUT", "PATCH") \
                or not isinstance(payload.get("body"), dict):
            raise EditError("For a change in Salts FSM you can only edit its details.")
        new["body"] = _json_object(changes["body"], "The details")
    elif kind.startswith("tool:") and _tool_model(kind) is not None:
        if set(changes) != {"args"}:
            raise EditError("You can only edit the details of this action.")
        try:
            new["args"] = _tool_model(kind).model_validate(_json_object(changes["args"], "The details")).model_dump()
        except ValidationError as e:
            raise EditError(_why(e)) from None
    else:
        raise EditError("This kind of action can't be edited. Use Don't send, and ask Jarvis to do it again.")
    new = json.loads(json.dumps(new))
    if new == json.loads(json.dumps(payload)):
        raise EditError("Nothing was changed.")
    return new


def _why(e: ValidationError) -> str:
    """The first couple of validation problems, by field name only (never echoing the submitted value)."""
    parts = [f"{'.'.join(str(x) for x in err['loc']) or 'details'}: {err['msg']}" for err in e.errors()[:2]]
    return "Those details aren't valid - " + "; ".join(parts)
