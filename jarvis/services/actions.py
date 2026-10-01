"""Approval gate for consequential actions.

Jarvis may *queue* an external email or a production deployment, but only the
owner can approve it - from the display (button or saying "approve"), never
through the AI itself. That way a malicious email or issue report can't talk
Jarvis into sending mail or shipping code.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..db import Database
from ..events import EventBus
from ..integrations.microsoft365 import text_to_html

log = logging.getLogger(__name__)


class ActionExecutor:
    def __init__(self, db: Database, bus: EventBus, notifier, mail, fixer, fsm=None):
        self.fsm = fsm
        self.billing = None  # set after construction
        self.j = None  # the Jarvis instance, for running approved tool calls
        self.db = db
        self.bus = bus
        self.notifier = notifier
        self.mail = mail
        self.fixer = fixer
        self._tasks: set[asyncio.Task] = set()

    def queue(self, kind: str, summary: str, payload: dict[str, Any]) -> int:
        action_id = self.db.create_action(kind, summary, payload)
        self.bus.publish("approvals", self.db.pending_actions())
        return action_id

    async def _execute(self, action: dict[str, Any]) -> str:
        p = action["payload"]
        if action["kind"].startswith("tool:"):
            from ..brain.tools import TOOLS_BY_NAME, serialise

            tool = TOOLS_BY_NAME[p["tool"]]
            result = await tool.handler(self.j, tool.model.model_validate(p["args"]))
            return serialise(result)[:600]
        if action["kind"] == "email_send":
            await self.mail.send_mail(p["to"], p["subject"], text_to_html(p["body"]), p.get("cc") or None)
            return f"Email sent to {', '.join(p['to'])}"
        if action["kind"] == "sage_invoices":
            return await self.billing.create_invoices(p["jobs"])
        if action["kind"] == "review_requests":
            return await self.billing.send_review_requests(p["requests"], self.mail)
        if action["kind"] == "fsm_write":
            result = await self.fsm.write(p["method"], p["path"], p.get("body"))
            return f"Salts FSM updated: {str(result)[:300]}"
        if action["kind"] == "accept_quote":
            await self.fsm.write("PATCH", f"/quotes/{p['quote_id']}", {"status": "accepted"})
            result = await self.fsm.write("POST", "/jobs", p["job_body"])
            return f"Quote {p['quote_id']} accepted; job booked: {str(result)[:250]}"
        if action["kind"] == "accept_quote_from_po":
            await self.fsm.write("PATCH", f"/quotes/{p['quote_id']}", {"status": "accepted"})
            result = await self.fsm.write("POST", "/jobs", p["job_body"])
            job = result.get("job", result) if isinstance(result, dict) else {}
            job_id = job.get("id")
            if job_id and p.get("po_number"):
                await self.fsm.write("PUT", f"/jobs/{job_id}/customer-po", {"poNumber": p["po_number"]})
            if p.get("ack_to"):
                await self.mail.send_mail(
                    [p["ack_to"]], f"Order received - {p['job_body'].get('description', '')[:80]}",
                    f"<p>Hi {p.get('ack_name') or 'there'},</p>"
                    f"<p>Thanks - we've received your purchase order"
                    f"{' (' + p['po_number'] + ')' if p.get('po_number') else ''} and the job is now booked in.</p>"
                    "<p>We'll be in touch to confirm scheduling.</p><p>Kind regards</p>")
            po_note = f" (PO {p['po_number']})" if p.get("po_number") else ""
            return f"Quote {p['quote_id']} accepted; job booked{po_note}: {str(result)[:250]}"
        if action["kind"] == "deploy_fix":
            return await self.fixer.deploy(p["issue_id"], p["pr_number"])
        raise ValueError(f"Unknown action kind {action['kind']}")

    async def _blocked_by_verifier(self, action: dict[str, Any]) -> str:
        """Empty string to go ahead, else why the optional ThoughtProof layer refused. Only ever runs AFTER the owner's
        approval and can only stop an action, never start, approve or skip one. Off by default."""
        verifier = getattr(self.j, "verifier", None)
        if verifier is None or not verifier.enabled:
            return ""
        verdict = await verifier.verify(action)
        return "" if verdict.allowed else (verdict.reason or "blocked by the security mandates")

    async def _run(self, action: dict[str, Any]) -> None:
        try:
            blocked = await self._blocked_by_verifier(action)
            if blocked:
                self.db.set_action_status(action["id"], "denied", f"Blocked by the security check: {blocked}"[:1000])
                await self.notifier.notify(
                    f"Blocked by the security check: {action['summary'][:100]}",
                    f"Action #{action['id']} was NOT carried out. Reason: {blocked[:400]}", level="warning",
                    push=True, speak=True)
            else:
                result = await self._execute(action)
                self.db.set_action_status(action["id"], "done", result)
                await self.notifier.notify(f"Done: {action['summary'][:120]}", result, level="info", speak=True,
                                             importance="info")
        except Exception as e:  # noqa: BLE001
            log.exception("Action %s failed", action["id"])
            self.db.set_action_status(action["id"], "failed", str(e)[:1000])
            await self.notifier.notify(f"Action #{action['id']} failed", str(e)[:500], level="warning",
                                       importance="normal")
        self.bus.publish("approvals", self.db.pending_actions())

    async def approve(self, action_id: int, by: str | None = None) -> str:
        action = self.db.get_action(action_id)
        if not action or action["status"] != "pending":
            return f"Action #{action_id} is not pending."
        self.db.set_action_status(action_id, "approved")
        log.info("Action #%s approved by %s", action_id, by or "the owner")
        self.bus.publish("approvals", self.db.pending_actions())
        task = asyncio.create_task(self._run(action))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return f"Approved action #{action_id}{f' ({by})' if by else ''}: {action['summary']}"

    async def deny(self, action_id: int) -> str:
        action = self.db.get_action(action_id)
        if not action or action["status"] != "pending":
            return f"Action #{action_id} is not pending."
        self.db.set_action_status(action_id, "denied")
        self.bus.publish("approvals", self.db.pending_actions())
        return f"Cancelled action #{action_id}."
