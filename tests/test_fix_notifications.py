"""Fix / pull-request notifications follow FIX_NOTIFY_CHANNELS (default Teams only) - no email to the owner's
mailbox - while every other notification keeps its existing Teams + email behaviour."""

from __future__ import annotations

from jarvis.config import Settings
from jarvis.db import Database
from jarvis.events import EventBus
from jarvis.services.notifier import Notifier


class StubMail:
    demo = False

    def __init__(self):
        self.sent: list[tuple[list[str], str]] = []

    async def send_mail(self, to, subject, body_html, cc=None, bcc=None, sensitivity=None):
        self.sent.append((list(to), subject))


class StubTeams:
    enabled = True

    def __init__(self):
        self.posts: list[str] = []

    async def post(self, title, body):
        self.posts.append(title)


def make(tmp_path, **kw):
    s = Settings(data_dir=tmp_path / "data", scheduler_enabled=False, owner_email="info@example.co.uk",
                 _env_file=None, **kw)
    mail, teams = StubMail(), StubTeams()
    return Notifier(s, Database(s.db_path), EventBus(), mail, teams), mail, teams


def test_fix_channels_parsing(tmp_path):
    assert Settings(data_dir=tmp_path, _env_file=None).fix_channels == ("teams",)  # default: Teams only
    assert Settings(data_dir=tmp_path, fix_notify_channels="teams, email", _env_file=None).fix_channels == \
        ("teams", "email")
    assert Settings(data_dir=tmp_path, fix_notify_channels="none", _env_file=None).fix_channels == ()
    assert Settings(data_dir=tmp_path, fix_notify_channels="", _env_file=None).fix_channels == ()


async def test_fix_notification_defaults_to_teams_and_display_not_email(tmp_path):
    n, mail, teams = make(tmp_path)
    await n.notify("Pull request ready: X", "body", level="info", push=True, speak=True, fix=True)
    assert mail.sent == []
    assert teams.posts == ["Pull request ready: X"]
    assert any(r["title"] == "Pull request ready: X" for r in n.db.recent_notifications())  # display still told


async def test_fix_warning_level_no_longer_emails_by_default(tmp_path):
    n, mail, teams = make(tmp_path)
    await n.notify("CI failed", "x", level="warning", fix=True)  # push defaults on for warnings
    assert mail.sent == [] and teams.posts == ["CI failed"]


async def test_fix_email_can_be_reenabled_to_a_different_address(tmp_path):
    n, mail, teams = make(tmp_path, fix_notify_channels="teams,email", fix_notify_email="alex@example.co.uk")
    await n.notify("Pull request ready: X", "body", level="info", push=True, fix=True)
    assert mail.sent == [(["alex@example.co.uk"], "[Jarvis] Pull request ready: X")]
    assert teams.posts == ["Pull request ready: X"]


async def test_fix_email_falls_back_to_owner_when_enabled_without_address(tmp_path):
    n, mail, _ = make(tmp_path, fix_notify_channels="teams,email")
    await n.notify("Pull request ready: X", "body", level="info", push=True, fix=True)
    assert mail.sent == [(["info@example.co.uk"], "[Jarvis] Pull request ready: X")]


async def test_display_only_still_records_but_sends_nothing(tmp_path):
    n, mail, teams = make(tmp_path, fix_notify_channels="none")
    await n.notify("Pull request ready: X", "body", level="info", push=True, fix=True)
    assert mail.sent == [] and teams.posts == []
    assert any(r["title"] == "Pull request ready: X" for r in n.db.recent_notifications())


async def test_non_fix_notifications_still_email_the_owner(tmp_path):
    n, mail, teams = make(tmp_path)
    await n.notify("Weekly marketing report", "body", level="info", push=True)
    assert mail.sent == [(["info@example.co.uk"], "[Jarvis] Weekly marketing report")]
    assert teams.posts == ["Weekly marketing report"]


async def test_send_owner_update_email_unchanged(tmp_path):
    """Briefings / wrap-ups call send_owner_update directly with channels - untouched by the fix setting."""
    n, mail, _ = make(tmp_path)
    await n.send_owner_update("Morning briefing", "text", channels=("teams", "email"))
    assert mail.sent == [(["info@example.co.uk"], "[Jarvis] Morning briefing")]
