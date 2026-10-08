"""Approval gate for consequential actions.

Jarvis may *queue* an external email or a production deployment, but only a human can approve it - from the display
(button or saying "approve") or, for the owner/partner/managers, from Microsoft Teams (an Adaptive Card button or
the typed command "approve 12") - never through the AI itself. That way a malicious email or issue report can't
talk Jarvis into sending mail or shipping code.

The one exception is a *standing approval* (services/standing_approvals.py): the owner, on the Settings page, may
pre-approve two narrow classes of action. That is still the owner's approval - given in advance, in a place only
they can reach - not Jarvis approving itself. `queue()` is where it is applied, and the action then goes through
the very same `_run` path (ThoughtProof check, "Done" notice) as a hand-approved one, marked as automatic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from .. import access
from ..brain import checkmode
from ..db import Database
from ..events import EventBus
from ..integrations.microsoft365 import text_to_html
from . import approval_inbox as inbox
from . import standing_approvals as sa

log = logging.getLogger(__name__)


class ActionRefused(Exception):
    """A human's Edit / Retry / Dismiss could not be carried out. `status` is the HTTP status the console endpoint answers with."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status

_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")
RATE_WARNING_EVERY_S = 15 * 60  # at most one "automatic actions paused" warning per this long


def _fsm_ok(result: Any) -> Any:
    """The FSM client raises for any non-2xx response. This is the second line of defence: a result that itself
    carries a non-2xx HTTP status (a client that handed the response back instead of raising) is a FAILED action with
    the error shown, never a silent "done"."""
    if isinstance(result, dict):
        for key in ("status", "status_code"):
            code = result.get(key)
            if isinstance(code, int) and not isinstance(code, bool) and not 200 <= code < 300:
                raise RuntimeError(f"Salts FSM did not accept the change (HTTP {code}): {str(result)[:300]}")
    return result


class ActionExecutor:
    def __init__(self, db: Database, bus: EventBus, notifier, mail, fixer, fsm=None):
        self.fsm = fsm
        self.billing = None  # set after construction
        self.j = None  # the Jarvis instance, for running approved tool calls
        self.standing = None  # StandingApprovals, set by Jarvis; None = nothing is ever auto-approved
        self.teams_approvals = None  # TeamsApprovals, set by Jarvis; None = no Teams cards
        self.db = db
        self.bus = bus
        self.notifier = notifier
        self.mail = mail
        self.fixer = fixer
        self._tasks: set[asyncio.Task] = set()
        self._last_rate_warning: float | None = None  # None: no warning yet, so the first one is never suppressed

    # ------------------------------------------------------------------ small helpers
    @staticmethod
    def _loop_running() -> bool:
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _who(by: str | None) -> str:
        """A person's name for the audit trail. Can't impersonate the automatic marker."""
        name = _CONTROL.sub("", by or "").strip()[:80] or "the owner"
        return f"person {name}" if name.lower().startswith("standing approval") else name

    # ------------------------------------------------------------------ queueing
    def queue(self, kind: str, summary: str, payload: dict[str, Any]) -> int:
        """Record an action for approval - or, if (and only if) the owner's standing approval exactly covers it,
        record it as approved by that standing approval and run it. Everything else waits for a human."""
        checkmode.guard("Queueing an approval")  # a question check (brain/checkmode.py) may only read
        # The payload that is judged is the payload that is stored and run: the canonical JSON form of it.
        payload = json.loads(json.dumps(payload))
        caller = access.current_caller.get()
        role = access.role_of(caller)  # recorded with the action: it runs with its requester's permissions when approved (see _execute)
        if caller is not None and caller.is_team:
            # Asked for by a team member (Team mode). It always waits for a human: the owner's standing approvals were
            # given for the owner's own requests, never for someone else's, so they are not even consulted. The requester is
            # recorded in the payload and named on the card, so whoever approves knows who asked.
            payload["requested_by"] = caller.label
            summary = f"{summary} (asked for by {caller.label})"
            decision = None
        else:
            decision = self._standing_decision(kind, payload)
        if decision is not None and decision.category:
            action_id = self.db.create_action(kind, summary, payload, status="approved",
                                              approved_by=sa.APPROVER_PREFIX + decision.category, requested_role=role)
            action = self.db.get_action(action_id)
            self.bus.publish("approvals", inbox.pending_for_display(self.db))
            self._spawn(self._run(action))
            return action_id
        action_id = self.db.create_action(kind, summary, payload, requested_role=role)
        self.bus.publish("approvals", inbox.pending_for_display(self.db))
        if decision is not None and decision.rate_limited:
            self._warn_rate_limited(kind)
        self._offer_to_teams(action_id)
        return action_id

    def _standing_decision(self, kind: str, payload: dict[str, Any]) -> sa.Decision | None:
        """Never raises; any doubt (no event loop to run it on, an error) means "ask a human"."""
        if self.standing is None or not self._loop_running():
            return None
        try:
            return self.standing.decide(kind, payload)
        except Exception:  # noqa: BLE001
            log.exception("Standing-approval check failed; queueing for a human instead")
            return None

    def _warn_rate_limited(self, kind: str) -> None:
        now = time.monotonic()
        last = self._last_rate_warning
        if self._loop_running() and (last is None or now - last >= RATE_WARNING_EVERY_S):
            self._last_rate_warning = now
            limit = getattr(getattr(self.standing, "s", None), "standing_max_per_hour", "?")
            self._spawn(self.notifier.notify(
                "Automatic actions paused - hourly limit reached",
                f"{limit} actions have already run automatically under your standing approvals in the last hour, "
                f"so this one ({kind}) is waiting for your approval instead. If you didn't expect this many, "
                "something may be looping or being fed bad input - check the recent \"Done automatically\" notices.",
                level="warning", push=True, importance="important", dedupe_key="standing-rate-limit"))

    def _offer_to_teams(self, action_id: int) -> None:
        if self.teams_approvals is None or not self._loop_running():
            return
        action = self.db.get_action(action_id)
        if action and action["status"] == "pending":
            self._spawn(self.teams_approvals.offer(action))

    # ------------------------------------------------------------------ running
    async def _execute(self, action: dict[str, Any]) -> str:
        p = action["payload"]
        if action["kind"].startswith("tool:"):
            from ..brain.tools import TOOLS_BY_NAME, serialise

            tool = TOOLS_BY_NAME[p["tool"]]
            # It runs with the permissions of whoever ASKED for it, recorded when it was queued - not the approver's click, and not
            # (for a row from before roles were kept) the owner's by default. Approving it still decides whether it happens at all.
            asked = access.caller_for_role(action.get("requested_role"), str(p.get("requested_by") or "").removesuffix(" (team)"))
            token = access.current_caller.set(asked)
            try:
                result = await tool.handler(self.j, tool.model.model_validate(p["args"]))
            finally:
                access.current_caller.reset(token)
            return serialise(result)[:600]
        if action["kind"] == "email_send":
            await self.mail.send_mail(p["to"], p["subject"], text_to_html(p["body"]), p.get("cc") or None)
            return f"Email sent to {', '.join(p['to'])}"
        if action["kind"] == "sage_invoices":
            return await self.billing.create_invoices(p["jobs"])
        if action["kind"] == "review_requests":
            return await self.billing.send_review_requests(p["requests"], self.mail)
        if action["kind"] == "fsm_write":
            result = _fsm_ok(await self.fsm.write(p["method"], p["path"], p.get("body")))
            return f"Salts FSM updated: {str(result)[:300]}"
        if action["kind"] == sa.PO_ACK_KIND:
            subject, html = sa.acknowledgement_email(p)
            await self.mail.send_mail([p["to"]], subject, html)
            return f"Receipt acknowledgement emailed to {p['to']}"
        if action["kind"] == "accept_quote":
            _fsm_ok(await self.fsm.write("PATCH", f"/quotes/{p['quote_id']}", {"status": "accepted"}))
            result = _fsm_ok(await self.fsm.write("POST", "/jobs", p["job_body"]))
            return f"Quote {p['quote_id']} accepted; job booked: {str(result)[:250]}"
        if action["kind"] == "accept_quote_from_po":
            _fsm_ok(await self.fsm.write("PATCH", f"/quotes/{p['quote_id']}", {"status": "accepted"}))
            result = _fsm_ok(await self.fsm.write("POST", "/jobs", p["job_body"]))
            job = result.get("job", result) if isinstance(result, dict) else {}
            job_id = job.get("id")
            if job_id and p.get("po_number"):
                _fsm_ok(await self.fsm.write("PUT", f"/jobs/{job_id}/customer-po", {"poNumber": p["po_number"]}))
            if p.get("ack_to"):
                po_ref = f" ({p['po_number']})" if p.get("po_number") else ""
                if p.get("receipt_sent"):
                    # A receipt-only acknowledgement already went out (standing approval), so this is the
                    # separate "job booked" confirmation, sent only now the owner has approved the booking.
                    subject = f"Order confirmed - {p['job_body'].get('description', '')[:80]}"
                    body = (f"<p>Hi {p.get('ack_name') or 'there'},</p>"
                            f"<p>Following our receipt of your purchase order{po_ref}: it's now confirmed and the "
                            "job is booked in.</p><p>We'll be in touch to confirm scheduling.</p><p>Kind regards</p>")
                else:
                    subject = f"Order received - {p['job_body'].get('description', '')[:80]}"
                    body = (f"<p>Hi {p.get('ack_name') or 'there'},</p>"
                            f"<p>Thanks - we've received your purchase order{po_ref} and the job is now booked in.</p>"
                            "<p>We'll be in touch to confirm scheduling.</p><p>Kind regards</p>")
                await self.mail.send_mail([p["ack_to"]], subject, body)
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

    def _automatic_category(self, action: dict[str, Any]) -> str:
        by = str(action.get("approved_by") or "")
        return by[len(sa.APPROVER_PREFIX):] if by.startswith(sa.APPROVER_PREFIX) else ""

    async def _run(self, action: dict[str, Any]) -> None:
        category = self._automatic_category(action)
        try:
            blocked = ""
            if category and not (self.standing is not None and self.standing.still_valid(action)):
                blocked = "the standing approval no longer covers this action"
            blocked = blocked or await self._blocked_by_verifier(action)
            if blocked:
                self.db.set_action_status(action["id"], "denied", f"Blocked by the security check: {blocked}"[:1000])
                await self.notifier.notify(
                    f"Blocked by the security check: {action['summary'][:100]}",
                    f"Action #{action['id']} was NOT carried out. Reason: {blocked[:400]}", level="warning",
                    push=True, speak=True)
            else:
                result = await self._execute(action)
                self.db.set_action_status(action["id"], "done", result)
                if category:
                    await self._announce_automatic(action, category, result)
                else:
                    await self.notifier.notify(f"Done: {action['summary'][:120]}", result, level="info", speak=True,
                                                 importance="info")
        except Exception as e:  # noqa: BLE001
            log.exception("Action %s failed", action["id"])
            self.db.set_action_status(action["id"], "failed", str(e)[:1000])
            await self.notifier.notify(f"Action #{action['id']} failed", str(e)[:500], level="warning",
                                       importance="normal")
        self.bus.publish("approvals", inbox.pending_for_display(self.db))

    async def _announce_automatic(self, action: dict[str, Any], category: str, result: str) -> None:
        """The visible marker that this ran without anyone clicking: a display notice and a Teams message to the
        approvers. Described from the payload itself, not from the model-written summary."""
        what = sa.describe(action["kind"], action["payload"])
        undo = sa.undo_hint(action["kind"], action["payload"])
        title = f"Done automatically (standing approval - {category}): {what}"[:200]
        await self.notifier.notify(title, f"{result}\n{undo}".strip(), level="info", speak=False, importance="info")
        if self.teams_approvals is not None:
            await self.teams_approvals.info(f"{title}. {undo} (action #{action['id']})")

    # ------------------------------------------------------------------ decisions (humans only)
    def _already(self, action: dict[str, Any] | None, action_id: int) -> str:
        if not action:
            return f"Action #{action_id} doesn't exist."
        status, by = action["status"], action.get("approved_by") or ""
        if status == "pending":
            return f"Action #{action_id} is not pending."
        if by.startswith(sa.APPROVER_PREFIX):
            return f"Action #{action_id} already ran automatically under a standing approval ({status})."
        verb = {"denied": "denied", "approved": "approved", "done": "approved and done",
                "failed": "approved (it then failed)"}.get(status, status)
        return f"Action #{action_id} was already {verb}{f' by {by}' if by else ''} - nothing more to do."

    async def approve(self, action_id: int, by: str | None = None) -> str:
        action = self.db.get_action(action_id)
        who = self._who(by)
        if not action or action["status"] != "pending" or not self.db.decide_pending_action(
                action_id, "approved", who):
            return self._already(self.db.get_action(action_id), action_id)
        log.info("Action #%s approved by %s", action_id, who)
        self.bus.publish("approvals", inbox.pending_for_display(self.db))
        action = {**action, "status": "approved", "approved_by": who}  # what the verifier is told: a person approved
        self._spawn(self._run(action))
        self._cards_decided(action, "Approved", who)
        return f"Approved action #{action_id} ({who}): {action['summary']}"

    async def deny(self, action_id: int, by: str | None = None) -> str:
        action = self.db.get_action(action_id)
        who = self._who(by)
        if not action or action["status"] != "pending" or not self.db.decide_pending_action(
                action_id, "denied", who, f"Denied by {who}"):
            return self._already(self.db.get_action(action_id), action_id)
        self.bus.publish("approvals", inbox.pending_for_display(self.db))
        self._cards_decided(action, "Denied", who)
        return f"Cancelled action #{action_id}."

    # ------------------------------------------------------------------ Edit, Retry and Dismiss (humans only, never auto-run)
    # All three are reached only from main.py's owner-authenticated, same-origin endpoints (the console's Edit, Retry and
    # Dismiss buttons). No brain tool, standing approval, Teams message or scheduled job calls them. Edit and Retry never
    # run anything: each leaves a PLAIN pending action that still waits for its own Approve click. Dismiss runs, queues
    # and retries nothing at all - it only hides a failed action from the inbox (see `dismiss`).
    def edit(self, action_id: int, changes: Any, by: str | None = None) -> tuple[int, str]:
        """Edit a PENDING action. The stored payload is never changed in place: the edited payload is validated
        (services/approval_inbox.apply_edit - a closed list of kinds and fields) and queued as a NEW pending action,
        and the old one is closed as 'denied' in the same database transaction, so it can no longer be approved. What
        gets approved is therefore always exactly the payload that was on the card. The new action is inserted directly as a
        plain pending row, never through `queue()`: standing approvals are not consulted, so an edit can never auto-run. Returns (new action id, message); raises ActionRefused."""
        action = self.db.get_action(action_id)
        if not action:
            raise ActionRefused(f"Action #{action_id} doesn't exist.", 404)
        if action["status"] != "pending":
            raise ActionRefused(self._already(action, action_id), 409)
        try:
            payload = inbox.apply_edit(action["kind"], action["payload"], changes)
        except inbox.EditError as e:
            raise ActionRefused(str(e), 422) from None
        who = self._who(by)
        summary = re.sub(r"( \(edited\))+$", "", action["summary"])[:280] + " (edited)"
        new_id = self.db.supersede_pending_action(action_id, action["kind"], summary, payload, who)
        if new_id is None:  # someone decided it between our read and the write
            raise ActionRefused(self._already(self.db.get_action(action_id), action_id), 409)
        log.info("Action #%s edited by %s; queued as #%s", action_id, who, new_id)
        self.bus.publish("approvals", inbox.pending_for_display(self.db))
        self._cards_decided(action, "Edited", who)
        self._offer_to_teams(new_id)
        return new_id, (f"Saved your edit as action #{new_id}. Nothing has been sent - it is waiting for you to approve it.")

    def retry(self, action_id: int, by: str | None = None) -> tuple[int, str]:
        """Retry a FAILED action. This does NOT run it again: it queues a copy of the failed action's own stored kind and
        payload as a new pending action (once - the failed row is marked as retried), which waits for a human Approve
        click like anything else. Standing approvals are never consulted. Returns (new action id, message)."""
        action = self.db.get_action(action_id)
        if not action:
            raise ActionRefused(f"Action #{action_id} doesn't exist.", 404)
        if action["status"] != "failed":
            raise ActionRefused(f"Action #{action_id} hasn't failed, so there is nothing to retry. "
                                + self._already(action, action_id), 409)
        if action.get("dismissed_at"):
            raise ActionRefused(f"Action #{action_id} was dismissed by {action.get('dismissed_by') or 'someone'}, so it can't be "
                                "retried. Ask Jarvis to do it again if it is still needed.", 409)
        if action.get("superseded_by"):
            raise ActionRefused(f"Action #{action_id} has already been retried as action #{action['superseded_by']}.", 409)
        who = self._who(by)
        summary = f"Retry of #{action_id}: {re.sub(r'^Retry of #[0-9]+: ', '', action['summary'])}"[:300]
        new_id = self.db.retry_failed_action(action_id, summary)
        if new_id is None:
            raise ActionRefused(f"Action #{action_id} has already been retried.", 409)
        log.info("Action #%s retried by %s; queued as #%s", action_id, who, new_id)
        self.bus.publish("approvals", inbox.pending_for_display(self.db))
        self._offer_to_teams(new_id)
        return new_id, (f"Queued a retry of #{action_id} as action #{new_id}. Nothing has run - it is waiting for you "
                        "to approve it.")

    def dismiss(self, action_id: int, by: str | None = None, *, publish: bool = True) -> tuple[bool, str]:
        """Dismiss a FAILED action: hide it from the failed list, the rail count, "Needs you" and the chat cards, because
        a person looked at it and it needs nothing more (e.g. it was for something that was never in the register).

        It runs nothing, queues nothing, retries nothing and changes nothing about the action itself: the row stays
        'failed' with its payload, error and retry link untouched, and only records who dismissed it and when
        (`db.dismiss_failed_action`); it stays in the full history, flagged. Only a failed action can be dismissed
        (anything else: ActionRefused 409). Dismissing twice is harmless: the first dismissal stands and (False, message)
        comes back. No standing approval, executor, verifier or Teams call is involved. Returns (newly dismissed, message)."""
        action = self.db.get_action(action_id)
        if not action:
            raise ActionRefused(f"Action #{action_id} doesn't exist.", 404)
        if action["status"] != "failed":
            raise ActionRefused(f"Action #{action_id} hasn't failed, so there is nothing to dismiss. "
                                + self._already(action, action_id), 409)
        if action.get("dismissed_at"):
            return False, f"Action #{action_id} was already dismissed by {action.get('dismissed_by') or 'someone'}."
        who = self._who(by)
        if not self.db.dismiss_failed_action(action_id, who):  # dismissed by someone else between our read and the write
            return False, f"Action #{action_id} was already dismissed."
        log.info("Failed action #%s dismissed by %s", action_id, who)
        if publish:
            self.bus.publish("approvals", inbox.pending_for_display(self.db))   # other open consoles refresh their lists
        return True, f"Dismissed action #{action_id}. It is hidden from the inbox and kept in the history."

    def dismiss_many(self, action_ids: list[int], by: str | None = None) -> dict[str, list[int]]:
        """Dismiss each of these FAILED actions (the ones a person saw listed and confirmed). Anything that is not a
        failed action, or is already dismissed, is skipped untouched. Returns {"dismissed": [...], "skipped": [...]}."""
        done: list[int] = []
        skipped: list[int] = []
        for action_id in dict.fromkeys(action_ids):
            try:
                newly, _ = self.dismiss(action_id, by, publish=False)
            except ActionRefused:
                newly = False
            (done if newly else skipped).append(action_id)
        if done:
            self.bus.publish("approvals", inbox.pending_for_display(self.db))
        return {"dismissed": done, "skipped": skipped}

    def _cards_decided(self, action: dict[str, Any], verb: str, who: str) -> None:
        """Whoever decided it, anywhere, the Teams cards for it stop offering buttons."""
        if self.teams_approvals is not None and self._loop_running():
            self._spawn(self.teams_approvals.mark_decided(action, self.teams_approvals.stamp(who, verb)))
