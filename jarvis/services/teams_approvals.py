"""Approvals on Microsoft Teams: when an action is queued, each known approver is sent an Adaptive Card with
Approve / Deny buttons, so the owner (or partner, or a manager) can decide from a phone when away.

This is a convenience front-end to the SAME gate the display uses (`ActionExecutor.approve/deny`) - not a second
gate. Rules that keep it safe:

* Only people on the approver allowlist (owner, partner, MANAGER_EMAILS - exactly the list the chat webhook already
  uses) get a card, and only they can press the buttons: `main.teams_messages` authenticates the Bot Framework JWT,
  resolves the sender's email, checks it against `approver_emails()`, and then calls `actions.approve/deny`
  directly. The language model is never in that path and has no tool that can approve anything.
* A card button press and the typed commands `approve 12` / `deny 12` are parsed here with strict, deterministic
  rules (`parse_decision_value`, `parse_typed_command`); anything malformed is dropped, never passed on to the brain.
* Jarvis can only message someone who has first messaged the bot from a one-to-one Teams chat (their conversation
  reference is learned then) - and every serviceUrl is checked with `trusted_service_url` before anything is posted.
* Failures are never allowed to matter: not configured means silently no cards; a delivery error is logged (type
  and status only - no URLs, tokens or message text) and never blocks `queue()`. Each (action, approver) card is
  claimed in the database before sending, so a republish or retry never sends a second one.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..integrations.redact import redact
from ..integrations.teamsbot import trusted_service_url

log = logging.getLogger(__name__)

COMMAND_RE = re.compile(r"^(approve|deny)\s+#?(\d+)$", re.IGNORECASE | re.ASCII)  # ASCII: no look-alike digits
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁠-⁯﻿]")
MAX_ACTION_ID_DIGITS = 9
CARD_TYPE = "application/vnd.microsoft.card.adaptive"


def approver_emails(settings) -> set[str]:
    """Who may approve from Teams: the owner, the business partner and the managers (lower-cased, non-empty)."""
    return {e.strip().lower() for e in ({settings.owner_email, settings.partner_email} | settings.managers)
            if e and e.strip()}


def parse_typed_command(text: Any) -> tuple[str, int] | None:
    """("approve"|"deny", action id) for exactly `approve 12` / `deny #12` (any case); anything else is None.
    A leading Teams @mention of the bot is ignored."""
    if not isinstance(text, str):
        return None
    m = COMMAND_RE.fullmatch(re.sub(r"<at>.*?</at>", "", text, flags=re.IGNORECASE | re.DOTALL).strip())
    if not m or len(m.group(2)) > MAX_ACTION_ID_DIGITS:
        return None
    action_id = int(m.group(2))
    return (m.group(1).lower(), action_id) if action_id > 0 else None


def parse_decision_value(value: Any) -> tuple[str, str, int]:
    """What a card button sent back: ("ok", decision, id) for exactly {"jarvis_action": <id>, "decision":
    "approve"|"deny"}; ("bad", "", 0) if it claims to be ours but is malformed; ("other", "", 0) if it isn't ours."""
    if isinstance(value, str) and 0 < len(value) <= 2000:
        try:
            value = json.loads(value)
        except ValueError:
            return "other", "", 0
    if not isinstance(value, dict) or "jarvis_action" not in value:
        return "other", "", 0
    raw, decision = value.get("jarvis_action"), value.get("decision")
    if isinstance(raw, bool) or decision not in ("approve", "deny"):
        return "bad", "", 0
    if isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) <= MAX_ACTION_ID_DIGITS:
        raw = int(raw)
    if not isinstance(raw, int) or raw <= 0 or raw > 10 ** MAX_ACTION_ID_DIGITS:
        return "bad", "", 0
    return "ok", decision, raw


def invoke_value(activity: dict[str, Any]) -> Any:
    """The data of an `adaptiveCard/action` invoke (Action.Execute): value.action.data."""
    value = activity.get("value")
    action = value.get("action") if isinstance(value, dict) else None
    return action.get("data") if isinstance(action, dict) else None


def _tidy(text: Any, limit: int) -> str:
    """Single-line, credential-redacted, control-character-free and truncated - safe to put in a card."""
    out = _CONTROL.sub("", redact(str(text or "")))
    out = re.sub(r"\s+", " ", out).strip()
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def _details(action: dict[str, Any]) -> str:
    p = action.get("payload") or {}
    kind = str(action.get("kind", ""))
    try:
        if kind == "email_send":
            return _tidy(f"To {', '.join(map(str, p.get('to', [])))} - subject: {p.get('subject', '')} - "
                         f"{p.get('body', '')}", 420)
        if kind == "fsm_write":
            return _tidy(f"{p.get('method', '')} {p.get('path', '')} fields: {', '.join(map(str, (p.get('body') or {})))}", 200)
        if kind.startswith("tool:"):
            return _tidy(f"Tool {p.get('tool', '')}", 100)
    except Exception:  # noqa: BLE001 - details are decoration; never fail a card over them
        pass
    return _tidy(kind, 80)


def approval_card(action: dict[str, Any]) -> dict[str, Any]:
    aid = action["id"]
    return {
        "type": "AdaptiveCard", "version": "1.3", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [
            {"type": "TextBlock", "text": f"Jarvis needs your approval - #{aid}", "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": _tidy(action.get("summary"), 500), "wrap": True},
            {"type": "TextBlock", "text": _details(action), "wrap": True, "isSubtle": True, "size": "Small"},
            {"type": "TextBlock", "text": f"Or reply: approve {aid} / deny {aid}", "wrap": True, "isSubtle": True,
             "size": "Small"},
        ],
        "actions": [
            {"type": "Action.Submit", "title": "Approve", "style": "positive",
             "data": {"jarvis_action": aid, "decision": "approve"}},
            {"type": "Action.Submit", "title": "Deny", "style": "destructive",
             "data": {"jarvis_action": aid, "decision": "deny"}},
        ],
    }


def decided_card(action: dict[str, Any], outcome: str) -> dict[str, Any]:
    """The same card with the buttons gone and the outcome ("Approved by Alex at 14:02") in their place."""
    return {
        "type": "AdaptiveCard", "version": "1.3", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [
            {"type": "TextBlock", "text": f"#{action['id']} - {_tidy(outcome, 200)}", "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": _tidy(action.get("summary"), 500), "wrap": True, "isSubtle": True},
        ],
    }


def card_activity(card: dict[str, Any], text: str = "") -> dict[str, Any]:
    return {"type": "message", "text": text,
            "attachments": [{"contentType": CARD_TYPE, "content": card}]}


class TeamsApprovals:
    def __init__(self, settings, db, bot):
        self.s = settings
        self.db = db
        self.bot = bot

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.bot, "configured", False))

    # -- learning where each approver is ---------------------------------------------------------------------
    def remember(self, email: str, service_url: str, conversation_id: str, conversation_type: str | None) -> bool:
        """Store an allowlisted person's one-to-one chat so Jarvis can message them first. Group chats and channels
        are never stored: an approval posted there would be visible to everyone in them."""
        if (conversation_type or "").lower() != "personal":
            return False
        if email.strip().lower() not in approver_emails(self.s) or not trusted_service_url(service_url):
            return False
        if not conversation_id or len(conversation_id) > 300:
            return False
        self.db.save_teams_approver(email, service_url, conversation_id)
        return True

    def _recipients(self) -> list[dict[str, Any]]:
        allowed = approver_emails(self.s)
        return [r for r in self.db.teams_approvers() if r["email"].lower() in allowed
                and trusted_service_url(r["service_url"])]

    @staticmethod
    def _why(e: Exception) -> str:
        if isinstance(e, httpx.HTTPStatusError):
            return f"HTTP {e.response.status_code}"
        return type(e).__name__

    # -- outgoing -----------------------------------------------------------------------------------------------
    async def offer(self, action: dict[str, Any]) -> int:
        """Send the approval card for a pending action to every approver who has said hello. Idempotent per
        (action, approver). Never raises. Returns how many cards went out."""
        if not self.enabled:
            return 0
        sent = 0
        try:
            recipients = self._recipients()
        except Exception as e:  # noqa: BLE001
            log.warning("Teams approvals: couldn't list approvers (%s)", self._why(e))
            return 0
        for r in recipients:
            try:
                if not self.db.claim_teams_card(action["id"], r["email"]):
                    continue
                activity = card_activity(
                    approval_card(action), f"Approval needed (#{action['id']}). Tap Approve or Deny.")
                activity_id = await self.bot.send_activity(r["service_url"], r["conversation_id"], activity)
                if activity_id:
                    self.db.set_teams_card_activity(action["id"], r["email"], activity_id)
                sent += 1
            except Exception as e:  # noqa: BLE001 - Teams being down must never matter to the caller
                log.warning("Teams approval card not delivered (%s)", self._why(e))
        return sent

    async def info(self, text: str) -> int:
        """A plain information message to every approver (used for "Done automatically ..."). Never raises."""
        if not self.enabled:
            return 0
        sent = 0
        try:
            recipients = self._recipients()
        except Exception:  # noqa: BLE001
            return 0
        for r in recipients:
            try:
                await self.bot.send_activity(r["service_url"], r["conversation_id"],
                                             {"type": "message", "text": _tidy(text, 1500)})
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("Teams info message not delivered (%s)", self._why(e))
        return sent

    async def mark_decided(self, action: dict[str, Any], outcome: str) -> None:
        """Turn every approval card for this action into the outcome, whoever decided it and wherever. Never raises."""
        if not self.enabled:
            return
        try:
            cards = self.db.teams_cards_for(action["id"])
            known = {r["email"].lower(): r for r in self._recipients()}
        except Exception:  # noqa: BLE001
            return
        for c in cards:
            r = known.get(c["email"].lower())
            if not r or not c.get("activity_id"):
                continue
            try:
                await self.bot.update_activity(r["service_url"], r["conversation_id"], c["activity_id"],
                                               card_activity(decided_card(action, outcome), outcome))
            except Exception as e:  # noqa: BLE001
                log.warning("Teams approval card not updated (%s)", self._why(e))

    def stamp(self, who: str, verb: str) -> str:
        """"Approved by Alex at 14:02" in the business's time zone."""
        try:
            now = datetime.now(ZoneInfo(self.s.timezone))
        except Exception:  # noqa: BLE001
            now = datetime.now(timezone.utc)
        return f"{verb} by {_tidy(who, 60) or 'the owner'} at {now:%H:%M}"
