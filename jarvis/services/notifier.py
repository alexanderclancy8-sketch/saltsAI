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
                     speak: bool = False) -> None:
        nid = self.db.add_notification(level, title, body)
        self.bus.publish("notification", {"id": nid, "level": level, "title": title, "body": body, "speak": speak})
        if push is None:
            push = level in ("warning", "critical")
        if push:
            await self.send_owner_update(title, body, channels=("teams", "email"))

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
