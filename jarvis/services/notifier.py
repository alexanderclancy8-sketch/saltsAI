"""Tells the owner about things: HUD alert always, plus Teams and email for anything important.

Every notification carries an importance (info < normal < important < urgent). The shared inbox
(SHARED_INBOX, info@) is for operational items that matter only: automated email to it is held back unless the
importance reaches SHARED_INBOX_MIN_IMPORTANCE (default "important"), repeats of the same alert are collapsed
inside a window, and the number per hour is capped. Anything held back still shows on the display and goes to
Teams. Urgent (life-safety) alerts are never rate-limited, and anything unrecognised is treated as important so
that a doubtful alert is delivered rather than lost.
"""

from __future__ import annotations

import logging
from collections import deque
from time import monotonic as _now  # a module-level name so tests can move the clock

from ..config import Settings
from ..db import Database
from ..events import EventBus
from ..integrations.mail_guard import MANAGEMENT, MailGuardError
from ..integrations.microsoft365 import TeamsNotifier, text_to_html

log = logging.getLogger(__name__)

IMPORTANCE = ("info", "normal", "important", "urgent")
# What a notification's old-style level means when no importance is given.
_LEVEL_IMPORTANCE = {"info": "info", "warning": "important", "critical": "urgent"}
_URGENT_DEDUPE_CAP_S = 15 * 60  # an urgent alert is repeated after at most this, whatever the window says


def importance_rank(value: str | None) -> int:
    """Position in IMPORTANCE. Unknown values count as "important" - when in doubt, deliver."""
    v = (value or "").strip().lower()
    return IMPORTANCE.index(v) if v in IMPORTANCE else IMPORTANCE.index("important")


def importance_for(level: str, importance: str | None = None) -> str:
    if importance:
        return IMPORTANCE[importance_rank(importance)]
    return _LEVEL_IMPORTANCE.get(level, "normal")


class Notifier:
    def __init__(self, settings: Settings, db: Database, bus: EventBus, mail, teams: TeamsNotifier):
        self.s = settings
        self.db = db
        self.bus = bus
        self.mail = mail
        self.teams = teams
        self._shared_seen: dict[str, float] = {}  # alert key -> when it last reached the shared inbox
        self._shared_sent: deque[float] = deque()  # when each recent email reached the shared inbox

    async def notify(self, title: str, body: str = "", level: str = "info", push: bool | None = None,

                      speak: bool = False, importance: str | None = None, dedupe_key: str | None = None,
                      management_only: bool = False, engineering: bool = False, fix: bool = False,
                      issue_id: int | None = None) -> None:
        """`engineering=True` marks fix / pull request / deploy / triage / security-review notifications: those go
        to Teams only (see send_engineering_update) instead of Teams + email. `issue_id` lets a Teams delivery
        failure be shown against that issue on the issues list."""
        importance = importance_for(level, importance)
        nid = self.db.add_notification(level, title, body)
        self.bus.publish("notification", {"id": nid, "level": level, "importance": importance, "title": title,
                                            "body": body, "speak": speak})
        if push is None:
            push = level in ("warning", "critical")
        if push:
            if engineering:
                await self.send_engineering_update(title, body, issue_id=issue_id)
            elif fix:
                channels = self.s.fix_channels
                if channels:
                    await self.send_owner_update(title, body, channels=channels,
                                                   email_to=self.s.fix_notify_email or None)
            else:
                await self.send_owner_update(title, body, channels=("teams", "email"), importance=importance,
                                               dedupe_key=dedupe_key, management_only=management_only)

    def is_shared_inbox(self, address: str) -> bool:
        shared = (self.s.shared_inbox or "").strip().lower()
        return bool(shared) and address.strip().lower() == shared

    def shared_inbox_decision(self, subject: str, importance: str, dedupe_key: str | None = None) -> tuple[bool, str]:
        """May this alert be emailed to the shared inbox right now? Returns (allowed, reason if not)."""
        rank = importance_rank(importance)
        if rank < importance_rank(self.s.shared_inbox_min_importance):
            return False, "below the minimum importance for the shared inbox"
        now = _now()
        urgent = rank >= IMPORTANCE.index("urgent")
        window = max(0, self.s.shared_inbox_dedupe_minutes) * 60
        if urgent:
            window = min(window, _URGENT_DEDUPE_CAP_S)
        last = self._shared_seen.get(self._alert_key(subject, dedupe_key))
        if last is not None and now - last < window:
            return False, "same alert already sent recently"
        while self._shared_sent and now - self._shared_sent[0] >= 3600:
            self._shared_sent.popleft()
        cap = self.s.shared_inbox_max_per_hour
        if not urgent and cap > 0 and len(self._shared_sent) >= cap:
            return False, "shared inbox rate limit reached"
        return True, ""

    @staticmethod
    def _alert_key(subject: str, dedupe_key: str | None) -> str:
        return (dedupe_key or subject).strip().lower()

    def _record_shared_send(self, subject: str, dedupe_key: str | None) -> None:
        now = _now()
        self._shared_seen[self._alert_key(subject, dedupe_key)] = now
        self._shared_sent.append(now)
        if len(self._shared_seen) > 500:  # forget alerts that can no longer be duplicates
            horizon = max(self.s.shared_inbox_dedupe_minutes * 60, _URGENT_DEDUPE_CAP_S)
            self._shared_seen = {k: t for k, t in self._shared_seen.items() if now - t < horizon}

    async def send_email(self, to: list[str], subject: str, body_html: str, importance: str = "normal",
                         dedupe_key: str | None = None, management_only: bool = False,
                      sensitivity: str = MANAGEMENT) -> tuple[list[str], list[str]]:
        """Email automated updates, through the shared inbox guard.

        Returns (addresses emailed, addresses held back). management_only items (finance, pay, performance) are
        only ever sent to the owner or partner, never to the shared inbox or anyone else."""
        if getattr(self.mail, "demo", True):
            return [], []
        deliver: list[str] = []
        held: list[str] = []
        private = {a.lower() for a in (self.s.owner_email, self.s.partner_email) if a}
        for addr in to:
            if management_only and (addr.strip().lower() not in private or self.is_shared_inbox(addr)):
                held.append(addr)
            elif self.is_shared_inbox(addr):
                ok, why = self.shared_inbox_decision(subject, importance, dedupe_key)
                if ok:
                    deliver.append(addr)
                else:
                    log.info("Not emailing %r to the shared inbox: %s", subject, why)
                    held.append(addr)
            else:
                deliver.append(addr)
        self._last_owner_file_note = None
        if deliver:
            if (deliver == [self.s.owner_email] and not self.is_shared_inbox(deliver[0])
                    and hasattr(self.mail, "send_to_owner")):
                filed, warning = await self.mail.send_to_owner(deliver[0], subject, body_html)
                if filed:
                    self._last_owner_file_note = f"('{self.s.owner_mail_folder.strip()}' folder)"
                elif warning:
                    self._last_owner_file_note = f"(Inbox - {warning})"
            else:
                await self.mail.send_mail(deliver, subject, body_html, sensitivity=sensitivity)
            if any(self.is_shared_inbox(a) for a in deliver):
                self._record_shared_send(subject, dedupe_key)
        return deliver, held
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
                # Jarvis's own mail to the owner is filed into his Jarvis folder, not the Inbox (see
                # GraphMail.send_to_owner). Filing problems never fail the send: the mail just stays in the Inbox.
                filed, warning = await self.mail.send_to_owner(
                    self.s.owner_email, f"[Jarvis] {subject}", text_to_html(body))
                if filed:
                    sent.append(f"email ('{self.s.owner_mail_folder.strip()}' folder)")
                elif warning:
                    sent.append(f"email (Inbox - {warning})")
                else:
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

    async def send_owner_update(self, subject: str, body: str, channels: tuple[str, ...] | list[str] = ("teams",),
                                  email_to: str | None = None, sensitivity: str = MANAGEMENT,
                                  importance: str = "normal", dedupe_key: str | None = None,
                                  management_only: bool = False) -> str:
        """Owner updates are treated as management content by default (briefings, wrap-up, finance, HR...), so the
        email only goes out if OWNER_EMAIL is a real management address - never a shared inbox."""
        sent = []
        recipient = email_to or self.s.owner_email
        held_back = False
        if "email" in channels and recipient and not getattr(self.mail, "demo", True):
            try:
                deliver, _held = await self.send_email([recipient], f"[Jarvis] {subject}", text_to_html(body),
                                                          importance=importance, dedupe_key=dedupe_key,
                                                          management_only=management_only,
                                                          sensitivity=sensitivity)
                if deliver:
                    note = getattr(self, "_last_owner_file_note", None)
                    sent.append(f"email {note}" if note else "email")
                held_back = not deliver
            except MailGuardError as e:
                log.warning("Owner update email blocked by the management-only mail rule: %s", e)
            except Exception as e:  # noqa: BLE001
                log.warning("Email update failed: %s", e)
        # Something the shared inbox wouldn't take still reaches Teams, even if only email was asked for.
        if ("teams" in channels or held_back) and self.teams.enabled:
            try:
                await self.teams.post(subject, body)
                sent.insert(0, "Teams")
            except Exception as e:  # noqa: BLE001 - never let a notification failure break the caller
                log.warning("Teams update failed: %s", e)
        self.bus.publish("owner_update", {"subject": subject, "body": body, "channels": sent})
        return ", ".join(sent) if sent else "the display only (Teams/email not configured)"
