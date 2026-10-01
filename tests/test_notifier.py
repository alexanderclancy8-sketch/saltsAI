"""The shared inbox (info@) only takes important operational items; repeats are collapsed and rate-limited."""

from __future__ import annotations

import pytest

from jarvis.db import Database
from jarvis.events import EventBus
from jarvis.services.notifier import Notifier, importance_for, importance_rank

SHARED = "info@saltsfireandsecurity.co.uk"
OWNER = "alex@saltsfireandsecurity.co.uk"
PARTNER = "chun@saltsfireandsecurity.co.uk"


class FakeMail:
    demo = False

    def __init__(self):
        self.sent: list[tuple[list[str], str]] = []

    async def send_mail(self, to, subject, body_html, cc=None, bcc=None, sensitivity=None):
        self.sent.append((list(to), subject))


class FakeTeams:
    enabled = True

    def __init__(self):
        self.posts: list[str] = []

    async def post(self, title, body):
        self.posts.append(title)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("jarvis.services.notifier._now", lambda: now[0])
    return now


def make(settings, owner=OWNER):
    settings.owner_email = owner
    settings.partner_email = PARTNER
    settings.shared_inbox = SHARED
    mail, teams = FakeMail(), FakeTeams()
    return Notifier(settings, Database(settings.db_path), EventBus(), mail, teams), mail, teams


def to_shared(mail):
    return [subject for to, subject in mail.sent if SHARED in to]


# --------------------------------------------------------------------------- importance levels
def test_levels_default_from_the_old_style_level_and_unknown_is_delivered():
    assert importance_for("info") == "info"
    assert importance_for("warning") == "important"
    assert importance_for("critical") == "urgent"
    assert importance_for("info", "urgent") == "urgent"
    assert importance_rank("something odd") == importance_rank("important")  # when in doubt, deliver
    assert importance_rank(None) == importance_rank("important")
    assert importance_rank("info") < importance_rank("normal") < importance_rank("important") < importance_rank("urgent")


# --------------------------------------------------------------------------- the minimum level
async def test_routine_items_are_not_emailed_to_the_shared_inbox_but_reach_teams(settings):
    n, mail, teams = make(settings, owner=SHARED)
    for importance in ("info", "normal"):
        via = await n.send_owner_update(f"Weekly digest {importance}", "FYI", channels=("email",), importance=importance)
        assert "Teams" in via and "email" not in via
    assert mail.sent == []
    assert teams.posts == ["Weekly digest info", "Weekly digest normal"]


async def test_important_and_urgent_items_are_emailed_to_the_shared_inbox(settings):
    n, mail, _ = make(settings, owner=SHARED)
    await n.send_owner_update("Overnight alarm needs a job", "x", channels=("email",), importance="important")
    await n.send_owner_update("Fire alarm signalling fault", "x", channels=("email",), importance="urgent")
    assert to_shared(mail) == ["[Jarvis] Overnight alarm needs a job", "[Jarvis] Fire alarm signalling fault"]


async def test_minimum_level_is_configurable(settings):
    n, mail, _ = make(settings, owner=SHARED)
    settings.shared_inbox_min_importance = "urgent"
    await n.send_owner_update("Important thing", "x", channels=("email",), importance="important")
    assert mail.sent == []
    settings.shared_inbox_min_importance = "normal"
    await n.send_owner_update("Normal thing", "x", channels=("email",), importance="normal")
    assert to_shared(mail) == ["[Jarvis] Normal thing"]


async def test_notify_maps_warning_and_critical_levels(settings):
    n, mail, _ = make(settings, owner=SHARED)
    await n.notify("Something odd", "x", level="warning")  # important by default -> delivered
    await n.notify("Digest", "x", level="info", push=True)  # info -> held back
    await n.notify("Digest 2", "x", level="warning", push=True, importance="normal")
    assert to_shared(mail) == ["[Jarvis] Something odd"]


async def test_the_owners_own_mailbox_is_not_filtered(settings):
    n, mail, _ = make(settings)  # owner is alex@..., not the shared inbox
    await n.send_owner_update("Weekly marketing report", "x", channels=("email",), importance="info")
    assert mail.sent == [([OWNER], "[Jarvis] Weekly marketing report")]


async def test_shared_inbox_match_ignores_case_and_spaces(settings):
    n, mail, _ = make(settings, owner=" INFO@SaltsFireAndSecurity.co.uk ")
    await n.send_owner_update("Digest", "x", channels=("email",), importance="info")
    assert mail.sent == []


# --------------------------------------------------------------------------- de-duplication and rate limit
async def test_the_same_alert_is_collapsed_inside_the_window(settings, clock):
    n, mail, teams = make(settings, owner=SHARED)
    for _ in range(3):
        await n.send_owner_update("Routine test failed: FSM login", "x", channels=("teams", "email"),
                                  importance="important")
        clock[0] += 60
    assert len(to_shared(mail)) == 1
    assert len(teams.posts) == 3  # Teams still hears about every one

    clock[0] += settings.shared_inbox_dedupe_minutes * 60
    await n.send_owner_update("Routine test failed: FSM login", "x", channels=("email",), importance="important")
    assert len(to_shared(mail)) == 2  # a long-running fault is re-sent once the window passes


async def test_dedupe_key_groups_alerts_with_changing_wording(settings, clock):
    n, mail, _ = make(settings, owner=SHARED)
    await n.send_owner_update("Panel fault at Site A (3 events)", "x", channels=("email",), importance="important",
                              dedupe_key="panel:site-a")
    await n.send_owner_update("Panel fault at Site A (4 events)", "x", channels=("email",), importance="important",
                              dedupe_key="panel:site-a")
    await n.send_owner_update("Panel fault at Site B", "x", channels=("email",), importance="important",
                              dedupe_key="panel:site-b")
    assert len(to_shared(mail)) == 2


async def test_rate_limit_caps_important_items_but_never_urgent_ones(settings, clock):
    n, mail, teams = make(settings, owner=SHARED)
    settings.shared_inbox_max_per_hour = 2
    for i in range(4):
        await n.send_owner_update(f"Alert {i}", "x", channels=("email",), importance="important")
    assert len(to_shared(mail)) == 2
    assert len(teams.posts) == 2  # the two held back went to Teams instead
    await n.send_owner_update("Fire alarm fault", "x", channels=("email",), importance="urgent")
    assert len(to_shared(mail)) == 3
    clock[0] += 3601
    await n.send_owner_update("Alert 9", "x", channels=("email",), importance="important")
    assert len(to_shared(mail)) == 4


async def test_urgent_repeats_are_resent_after_a_short_window(settings, clock):
    n, mail, _ = make(settings, owner=SHARED)
    settings.shared_inbox_dedupe_minutes = 600
    await n.send_owner_update("Lone worker overdue", "x", channels=("email",), importance="urgent")
    await n.send_owner_update("Lone worker overdue", "x", channels=("email",), importance="urgent")
    assert len(to_shared(mail)) == 1
    clock[0] += 16 * 60  # an unanswered life-safety alert is not silenced for hours
    await n.send_owner_update("Lone worker overdue", "x", channels=("email",), importance="urgent")
    assert len(to_shared(mail)) == 2


async def test_a_failed_send_is_not_counted_as_delivered(settings, clock):
    n, mail, _ = make(settings, owner=SHARED)

    async def boom(*a, **k):
        raise RuntimeError("graph down")

    mail.send_mail = boom
    await n.send_owner_update("Alarm", "x", channels=("email",), importance="important")  # logged, not raised
    del mail.send_mail
    await n.send_owner_update("Alarm", "x", channels=("email",), importance="important")
    assert len(to_shared(mail)) == 1  # the retry wasn't treated as a duplicate


# --------------------------------------------------------------------------- management-only
async def test_management_only_items_never_reach_the_shared_inbox(settings):
    n, mail, _ = make(settings, owner=SHARED)
    await n.send_owner_update("Cash position", "x", channels=("email",), importance="urgent", management_only=True)
    assert mail.sent == []


async def test_management_only_items_go_only_to_owner_or_partner(settings):
    n, mail, _ = make(settings)
    emailed, held = await n.send_email([OWNER, PARTNER, SHARED, "someone@else.com"], "Payroll", "<p>x</p>",
                                       importance="urgent", management_only=True)
    assert emailed == [OWNER, PARTNER]
    assert held == [SHARED, "someone@else.com"]
    assert mail.sent == [([OWNER, PARTNER], "Payroll")]


# --------------------------------------------------------------------------- other routes
async def test_demo_mail_sends_nothing(settings):
    n, mail, _ = make(settings, owner=SHARED)
    mail.demo = True
    assert await n.send_email([SHARED], "x", "<p>x</p>", importance="urgent") == ([], [])
    assert mail.sent == []
