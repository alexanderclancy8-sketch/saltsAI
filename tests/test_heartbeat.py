"""Heartbeat stop rules for automations: no-change streaks, stepping the interval up and resetting it, the ask-once rule,
the overnight rule, the safety exemptions, and the optional HEARTBEAT.md. Everything runs on a fake clock."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from jarvis.brain.tools import TOOLS_BY_NAME, AutomationOptionsIn, CreateAutomationIn, NoInput
from jarvis.core import Jarvis
from jarvis.db import Database
from jarvis.services import heartbeat
from jarvis.services.heartbeat import iso
from tests.fakes import FakeClient

UK = ZoneInfo("Europe/London")
DAY = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)      # 10:00 UK (BST)
NIGHT = datetime(2026, 10, 6, 23, 30, tzinfo=timezone.utc)  # 00:30 UK the next morning
QUIET = "NOTHING_TO_REPORT - all clear."
EVERY_10 = "*/10 * * * *"


class FakeClock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def setup(settings, monkeypatch, *, cron=EVERY_10, description="Pull request watch",
          prompt="Check the open pull requests and tell me what changed", start=DAY, never_slow=False):
    """A Jarvis whose automation runner uses a fake clock, whose brain replies from ``state['reply']`` and whose Teams
    messages are captured. Returns a namespace with the pieces."""
    j = Jarvis(settings, client=FakeClient())
    clock = FakeClock(start)
    j.automations.clock = clock
    state = {"reply": QUIET, "calls": 0, "prompts": []}
    sent: list[tuple] = []

    async def ask(prompt_text, *a, **k):
        state["calls"] += 1
        state["prompts"].append(prompt_text)
        return state["reply"]

    async def fake_send(subject, body, channels=("teams",), **kw):
        sent.append((subject, body, tuple(channels)))
        return "Teams"

    monkeypatch.setattr(j.brain, "ask", ask)
    monkeypatch.setattr(j.notifier, "send_owner_update", fake_send)
    aid = j.automations.create(description, cron, prompt, never_slow_down=never_slow)["id"]

    def asks():
        return [m for m in sent if m[0].startswith("[Jarvis]")]

    def row():
        return j.db.get_automation(aid)

    def listed():
        return next(a for a in j.automations.list_all() if a["id"] == aid)

    async def fire(minutes=10):
        """The scheduler's cron fires `minutes` after the last one."""
        clock.advance(minutes=minutes)
        await j.automations._run_guarded(aid)  # noqa: SLF001 - exactly what the scheduler calls

    return SimpleNamespace(j=j, clock=clock, state=state, aid=aid, asks=asks, row=row, listed=listed, fire=fire)


# --------------------------------------------------------------------------- pure helpers
def test_the_ladder_never_goes_below_the_configured_interval():
    assert heartbeat.ladder(10) == [10, 30, 60, 180, 1440]
    assert heartbeat.ladder(45) == [45, 60, 180, 1440]  # configured 45 min: the 10 and 30 steps are not used
    assert heartbeat.ladder(1440) == [1440]              # already daily: nothing slower to step to
    assert [heartbeat.effective_minutes(10, s) for s in (0, 5, 6, 11, 12, 18, 24, 500)] == [10, 10, 30, 30, 60, 180, 1440, 1440]
    assert heartbeat.effective_minutes(45, 6) == 60
    assert heartbeat.effective_minutes(1440, 100) == 1440
    assert heartbeat.effective_minutes(10, 100, exempt=True) == 10


def test_configured_minutes_comes_from_the_cron():
    assert heartbeat.configured_minutes("*/10 * * * *", "Europe/London", DAY) == 10
    assert heartbeat.configured_minutes("0 * * * *", "Europe/London", DAY) == 60
    assert heartbeat.configured_minutes("*/5 9-17 * * *", "Europe/London", DAY) == 5
    assert heartbeat.configured_minutes("0 8 * * 1-5", "Europe/London", DAY) == 1440
    assert heartbeat.configured_minutes("not a cron", "Europe/London", DAY) == 1440  # unknown: never slowed


def test_overnight_window_is_22_to_06_uk_time():
    at = lambda h, m: datetime(2026, 10, 6, h, m, tzinfo=UK)  # noqa: E731
    assert heartbeat.is_overnight(at(22, 0), UK) and heartbeat.is_overnight(at(5, 59), UK)
    assert not heartbeat.is_overnight(at(21, 59), UK) and not heartbeat.is_overnight(at(6, 0), UK)
    assert heartbeat.is_overnight(datetime(2026, 10, 6, 21, 30, tzinfo=timezone.utc), UK)  # 22:30 BST


def test_the_state_table_survives_an_old_database(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE automations (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
                "description TEXT NOT NULL, cron TEXT NOT NULL, prompt TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
                "last_run_at TEXT DEFAULT '', last_result TEXT DEFAULT '')")
    old.execute("INSERT INTO automations (created_at, description, cron, prompt) VALUES ('2026-01-01', 'Old', '0 8 * * *', 'x')")
    old.commit()
    old.close()
    row = Database(path).get_automation(1)
    assert row["nochange_streak"] == 0 and row["never_slow"] == 0 and row["nochange_since"] == ""


# --------------------------------------------------------------------------- streak, step up, reset
async def test_streak_counts_quiet_runs_and_steps_up_after_six(settings, monkeypatch):
    t = setup(settings, monkeypatch)
    for n in range(1, 6):
        await t.fire()
        assert t.row()["nochange_streak"] == n
    assert t.listed()["effective_interval"] == "every 10 minutes" and t.listed()["slowed_because"] == ""
    await t.fire()
    shown = t.listed()
    assert t.row()["nochange_streak"] == 6
    assert shown["effective_interval"] == "every 30 minutes" and shown["configured_interval"] == "every 10 minutes"
    assert shown["no_change_streak"] == 6 and "6 checks in a row" in shown["slowed_because"]
    await t.j.http.aclose()


async def test_slowed_automation_skips_early_fires_and_runs_when_due(settings, monkeypatch):
    t = setup(settings, monkeypatch)
    for _ in range(6):
        await t.fire()
    assert t.state["calls"] == 6
    await t.fire()  # 10 minutes after the last real run: skipped
    await t.fire()  # 20 minutes: still skipped
    assert t.state["calls"] == 6 and t.row()["nochange_streak"] == 6
    await t.fire()  # 30 minutes: due
    assert t.state["calls"] == 7 and t.row()["nochange_streak"] == 7
    await t.j.http.aclose()


async def test_every_six_more_quiet_runs_steps_up_again(settings, monkeypatch):
    t = setup(settings, monkeypatch)
    t.j.db.update_automation(t.aid, nochange_streak=11, nochange_since=iso(DAY))
    await t.fire(minutes=60)  # 12th quiet run
    assert t.listed()["effective_interval"] == "hourly"
    t.j.db.update_automation(t.aid, nochange_streak=17)
    await t.fire(minutes=60)  # 18th
    assert t.listed()["effective_interval"] == "every 3 hours"
    await t.j.http.aclose()


async def test_a_real_change_resets_to_the_configured_interval(settings, monkeypatch):
    t = setup(settings, monkeypatch)
    for _ in range(7):
        await t.fire()  # the 7th is skipped (slowed), so the streak is 6
    assert t.listed()["effective_interval"] == "every 30 minutes"
    t.state["reply"] = "PR #12 failed CI on the lint step."
    await t.fire(minutes=30)
    row = t.row()
    assert row["nochange_streak"] == 0 and row["nochange_since"] == ""
    assert t.listed()["effective_interval"] == "every 10 minutes" and t.listed()["slowed_because"] == ""
    calls = t.state["calls"]
    t.state["reply"] = QUIET
    await t.fire()  # back at the configured 10 minutes: runs straight away
    assert t.state["calls"] == calls + 1 and t.row()["nochange_streak"] == 1
    await t.j.http.aclose()


async def test_a_failed_run_neither_counts_nor_resets(settings, monkeypatch):
    t = setup(settings, monkeypatch)
    t.j.db.update_automation(t.aid, nochange_streak=4, nochange_since=iso(DAY))

    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(t.j.brain, "ask", boom)
    await t.fire()
    assert t.row()["nochange_streak"] == 4 and "Failed" in t.row()["last_result"]
    await t.j.http.aclose()


async def test_a_daily_automation_is_never_slowed(settings, monkeypatch):
    t = setup(settings, monkeypatch, cron="0 8 * * *")
    t.j.db.update_automation(t.aid, nochange_streak=50, nochange_since=iso(DAY - timedelta(days=50)))
    assert t.listed()["effective_interval"] == "daily" and t.listed()["slowed_because"] == ""
    await t.j.http.aclose()


# --------------------------------------------------------------------------- the 12-hour ask-once rule
async def test_asks_once_after_12_hours_then_not_for_24(settings, monkeypatch):
    t = setup(settings, monkeypatch)
    t.j.db.update_automation(t.aid, nochange_streak=3, nochange_since=iso(DAY - timedelta(hours=13)))
    await t.j.automations.run(t.aid)
    assert len(t.asks()) == 1
    subject, body, channels = t.asks()[0]
    assert subject.startswith("[Jarvis]") and channels == ("teams",)  # Teams only
    assert "Pull request watch" in subject and "keep" in body.lower() and "delete" in body.lower()
    assert t.row()["last_asked_at"] == iso(DAY)

    t.clock.advance(hours=1)
    await t.j.automations.run(t.aid)
    t.clock.advance(hours=22, minutes=58)  # 23h58m after the question
    await t.j.automations.run(t.aid)
    assert len(t.asks()) == 1
    t.clock.advance(minutes=2)  # 24h
    await t.j.automations.run(t.aid)
    assert len(t.asks()) == 2
    await t.j.http.aclose()


async def test_no_question_before_12_hours_or_after_a_change(settings, monkeypatch):
    t = setup(settings, monkeypatch)
    t.j.db.update_automation(t.aid, nochange_streak=3, nochange_since=iso(DAY - timedelta(hours=11)))
    await t.j.automations.run(t.aid)
    assert t.asks() == []
    t.j.db.update_automation(t.aid, nochange_streak=3, nochange_since=iso(DAY - timedelta(hours=20)))
    t.state["reply"] = "PR #3 now has merge conflicts."  # a change resets the streak first: nothing to ask about
    await t.j.automations.run(t.aid)
    assert t.asks() == [] and t.row()["nochange_streak"] == 0
    await t.j.http.aclose()


async def test_no_question_for_a_check_that_only_runs_daily(settings, monkeypatch):
    t = setup(settings, monkeypatch, cron="0 8 * * *")
    t.j.db.update_automation(t.aid, nochange_streak=3, nochange_since=iso(DAY - timedelta(days=3)))
    await t.j.automations.run(t.aid)
    assert t.asks() == []
    await t.j.http.aclose()


async def test_the_question_waits_until_morning(settings, monkeypatch):
    t = setup(settings, monkeypatch, start=NIGHT)
    t.j.db.update_automation(t.aid, nochange_streak=3, nochange_since=iso(NIGHT - timedelta(hours=14)))
    await t.j.automations.run(t.aid)
    assert t.asks() == []
    t.clock.advance(hours=7)  # 07:30 UK
    await t.j.automations.run(t.aid)
    assert len(t.asks()) == 1
    await t.j.http.aclose()


# --------------------------------------------------------------------------- overnight
async def test_overnight_runs_are_at_most_hourly(settings, monkeypatch):
    t = setup(settings, monkeypatch, start=NIGHT)
    await t.fire(minutes=0)  # never run before: goes ahead
    assert t.state["calls"] == 1
    await t.fire()           # 10 minutes later, overnight: skipped
    await t.fire(minutes=40)  # 50 minutes after the last run: still skipped
    assert t.state["calls"] == 1
    await t.fire(minutes=10)  # an hour: runs
    assert t.state["calls"] == 2
    await t.j.http.aclose()


async def test_the_same_fire_by_day_is_not_skipped(settings, monkeypatch):
    t = setup(settings, monkeypatch)
    t.j.db.update_automation(t.aid, last_run_at=iso(DAY - timedelta(minutes=10)))
    await t.j.automations._run_guarded(t.aid)  # noqa: SLF001
    assert t.state["calls"] == 1
    await t.j.http.aclose()


# --------------------------------------------------------------------------- exemptions
@pytest.mark.parametrize("prompt", [
    "Check the life-safety systems have reported in",
    "Watch the lone worker check-ins and tell me about any missed",
    "Look for out-of-hours alarms that were not answered",
    "Chase anything on the keyholder list that has changed",
    "Review Out of hours alarm activations",
])
async def test_safety_automations_are_never_slowed_or_skipped_overnight(settings, monkeypatch, prompt):
    t = setup(settings, monkeypatch, prompt=prompt, start=NIGHT)
    t.j.db.update_automation(t.aid, nochange_streak=60, nochange_since=iso(NIGHT - timedelta(hours=20)),
                             last_run_at=iso(NIGHT - timedelta(minutes=10)))
    shown = t.listed()
    assert shown["effective_interval"] == "every 10 minutes" and shown["slowed_because"].startswith("Not slowed")
    await t.j.automations._run_guarded(t.aid)  # noqa: SLF001 - overnight, 10 minutes after the last run
    assert t.state["calls"] == 1
    assert t.asks() == []  # and never asked about either
    await t.j.http.aclose()


async def test_the_same_streak_slows_an_ordinary_automation(settings, monkeypatch):
    t = setup(settings, monkeypatch)
    t.j.db.update_automation(t.aid, nochange_streak=60, last_run_at=iso(DAY - timedelta(minutes=10)))
    assert t.listed()["effective_interval"] == "daily"
    await t.j.automations._run_guarded(t.aid)  # noqa: SLF001
    assert t.state["calls"] == 0
    await t.j.http.aclose()


async def test_never_slow_down_flag_is_an_override(settings, monkeypatch):
    t = setup(settings, monkeypatch, never_slow=True)
    assert t.listed()["never_slow_down"] is True
    t.j.db.update_automation(t.aid, nochange_streak=60, nochange_since=iso(DAY - timedelta(hours=20)),
                             last_run_at=iso(DAY - timedelta(minutes=10)))
    assert t.listed()["effective_interval"] == "every 10 minutes"
    await t.j.automations._run_guarded(t.aid)  # noqa: SLF001
    assert t.state["calls"] == 1 and t.asks() == []
    await t.j.http.aclose()


async def test_the_tools_set_and_show_the_override(settings, monkeypatch):
    t = setup(settings, monkeypatch)
    tool = TOOLS_BY_NAME["set_automation_options"]
    assert not tool.approval  # a preference about how often a read-only check runs; nothing is sent or changed
    t.j.db.update_automation(t.aid, nochange_streak=30)
    assert t.listed()["effective_interval"] == "daily"
    msg = await tool.handler(t.j, AutomationOptionsIn(automation_id=t.aid, never_slow_down=True))
    assert "never be slowed" in msg
    listed = await TOOLS_BY_NAME["list_automations"].handler(t.j, NoInput())
    assert listed[0]["never_slow_down"] is True and listed[0]["effective_interval"] == "every 10 minutes"
    await tool.handler(t.j, AutomationOptionsIn(automation_id=t.aid, never_slow_down=False))
    assert t.listed()["effective_interval"] == "daily"
    assert "No automation" in await tool.handler(t.j, AutomationOptionsIn(automation_id=999, never_slow_down=True))
    created = await TOOLS_BY_NAME["create_automation"].handler(t.j, CreateAutomationIn(
        description="Keep going", cron=EVERY_10, prompt="look", never_slow_down=True))
    assert t.j.db.get_automation(created["id"])["never_slow"] == 1
    await t.j.http.aclose()


# --------------------------------------------------------------------------- HEARTBEAT.md
async def test_scheduled_runs_read_the_heartbeat_checklist(settings, monkeypatch):
    t = setup(settings, monkeypatch)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "HEARTBEAT.md").write_text("- Only message on change.\n- Never mention the weather.", encoding="utf-8")
    await t.j.automations.run(t.aid)
    sent = t.state["prompts"][0]
    assert "Only message on change" in sent and "Never mention the weather" in sent and "approval rules" in sent
    await t.j.http.aclose()


async def test_no_checklist_means_no_extra_prompt(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(heartbeat, "ROOT_DIR", tmp_path / "nowhere")
    t = setup(settings, monkeypatch)
    await t.j.automations.run(t.aid)
    assert "House rules" not in t.state["prompts"][0]
    assert heartbeat.read_checklist(settings.data_dir) == ""
    await t.j.http.aclose()


def test_the_checklist_falls_back_to_the_repo_copy_and_is_capped(tmp_path, monkeypatch):
    repo, data = tmp_path / "repo", tmp_path / "data"
    repo.mkdir()
    data.mkdir()
    monkeypatch.setattr(heartbeat, "ROOT_DIR", repo)
    (repo / "HEARTBEAT.md").write_text("repo rules " + "x" * 5000, encoding="utf-8")
    assert heartbeat.read_checklist(data).startswith("repo rules") and len(heartbeat.read_checklist(data)) == heartbeat.MAX_CHECKLIST_CHARS
    (data / "HEARTBEAT.md").write_text("data rules", encoding="utf-8")
    assert heartbeat.read_checklist(data) == "data rules"  # the data dir wins
