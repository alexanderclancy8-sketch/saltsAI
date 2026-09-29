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
        if action["kind"] == "deploy_fix":
            return await self.fixer.deploy(p["issue_id"], p["pr_number"])
        raise ValueError(f"Unknown action kind {action['kind']}")

    async def _run(self, action: dict[str, Any]) -> None:
        try:
            result = await self._execute(action)
            self.db.set_action_status(action["id"], "done", result)
            await self.notifier.notify(f"Done: {action['summary'][:120]}", result, level="info", speak=True)
        except Exception as e:  # noqa: BLE001
            log.exception("Action %s failed", action["id"])
            self.db.set_action_status(action["id"], "failed", str(e)[:1000])
            await self.notifier.notify(f"Action #{action['id']} failed", str(e)[:500], level="warning")
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
