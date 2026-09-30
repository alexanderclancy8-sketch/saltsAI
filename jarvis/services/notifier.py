"""Tells the owner about things: HUD alert always, plus Teams and email for anything important."""

from __future__ import annotations

import logging

from ..config import Settings
from ..db import Database
from ..events import EventBus
from ..integrations.microsoft365 import TeamsNotifier, text_to_html

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, settings: Settings, db: Database, bus: EventBus, mail, teams: TeamsNotifier):
        self.s = settings
        self.db = db
        self.bus = bus
        self.mail = mail
        self.teams = teams

    async def notify(self, title: str, body: str = "", level: str = "info", push: bool | None = None,
                     speak: bool = False, engineering: bool = False, issue_id: int | None = None) -> None:
        """`engineering=True` marks fix / pull request / deploy / triage / security-review notifications: those go
        to Teams only (see send_engineering_update) instead of Teams + email. `issue_id` lets a Teams delivery
        failure be shown against that issue on the issues list."""
        nid = self.db.add_notification(level, title, body)
        self.bus.publish("notification", {"id": nid, "level": level, "title": title, "body": body, "speak": speak})
        if push is None:
            push = level in ("warning", "critical")
        if push:
            if engineering:
                await self.send_engineering_update(title, body, issue_id=issue_id)
            else:
                await self.send_owner_update(title, body, channels=("teams", "email"))

    def engineering_channels(self) -> tuple[str, ...]:
        """Channels for engineering notifications, from ENGINEERING_NOTIFY_CHANNELS (default Teams only)."""
        wanted = [c.strip().lower() for c in (self.s.engineering_notify_channels or "").split(",")]
        channels = tuple(dict.fromkeys(c for c in wanted if c in ("teams", "email")))
        return channels or ("teams",)

    async def send_engineering_update(self, subject: str, body: str, issue_id: int | None = None) -> str:
        """Teams only by default. Email is used only if listed in the engineering channels setting, or as a
        fallback when Teams delivery fails AND ENGINEERING_EMAIL_FALLBACK is explicitly on. Otherwise a Teams
        failure is logged and shown on the display (and against the issue, if there is one) - never silently
        re-routed to email."""
        channels = self.engineering_channels()
        sent: list[str] = []
        teams_failed = ""
        if "teams" in channels:
            if not self.teams.enabled:
                teams_failed = "Teams updates aren't configured"
            else:
                try:
                    await self.teams.post(subject, body)
                    sent.append("Teams")
                except Exception as e:  # noqa: BLE001 - never let a notification failure break the caller
                    teams_failed = f"Teams delivery failed: {e}"
                    log.warning("Teams engineering update failed (%s): %s", subject, e)
        want_email = "email" in channels or (teams_failed and self.s.engineering_email_fallback)
        if want_email and self.s.owner_email and not getattr(self.mail, "demo", True):
            try:
                await self.mail.send_mail([self.s.owner_email], f"[Jarvis] {subject}", text_to_html(body))
                sent.append("email")
            except Exception as e:  # noqa: BLE001
                log.warning("Email engineering update failed (%s): %s", subject, e)
        if teams_failed and "email" not in sent:
            self._surface_delivery_failure(subject, teams_failed, issue_id)
        self.bus.publish("owner_update", {"subject": subject, "body": body, "channels": sent})
        return ", ".join(sent) if sent else "the display only (Teams not delivered)"

    def _surface_delivery_failure(self, subject: str, reason: str, issue_id: int | None) -> None:
        msg = f"{reason[:300]}. Not emailed. Update was: {subject}"
        log.warning("Engineering update not delivered: %s", msg)
        nid = self.db.add_notification("warning", "Update not delivered to Teams", msg)
        self.bus.publish("notification", {"id": nid, "level": "warning", "title": "Update not delivered to Teams",
                                          "body": msg, "speak": False})
        if issue_id is not None:
            issue = self.db.get_issue(issue_id)
            if issue:
                notes = f"{issue.get('notes') or ''}\n\n[Teams delivery] {msg}".strip()
                self.db.update_issue(issue_id, notes=notes[:4000])
                self.bus.publish("issue", self.db.get_issue(issue_id))

    async def send_owner_update(self, subject: str, body: str, channels: tuple[str, ...] | list[str] = ("teams",)) -> str:
        sent = []
        if "teams" in channels and self.teams.enabled:
            try:
                await self.teams.post(subject, body)
                sent.append("Teams")
            except Exception as e:  # noqa: BLE001 - never let a notification failure break the caller
                log.warning("Teams update failed: %s", e)
        if "email" in channels and self.s.owner_email and not getattr(self.mail, "demo", True):
            try:
                await self.mail.send_mail([self.s.owner_email], f"[Jarvis] {subject}", text_to_html(body))
                sent.append("email")
            except Exception as e:  # noqa: BLE001
                log.warning("Email update failed: %s", e)
        self.bus.publish("owner_update", {"subject": subject, "body": body, "channels": sent})
        return ", ".join(sent) if sent else "the display only (Teams/email not configured)"
