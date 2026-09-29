"""User-defined automations: schedule-based checks the owner sets up themselves via chat."""

from __future__ import annotations

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from jarvis.brain.tools import CreateAutomationIn, DeleteAutomationIn, NoInput, TOOLS_BY_NAME
from jarvis.core import Jarvis
from jarvis.services.automations import MAX_AUTOMATIONS
from tests.fakes import FakeClient, message, text_block


def make(settings, script=None):
    return Jarvis(settings, client=FakeClient(script))


# --------------------------------------------------------------------------- validation
def test_validate_cron_accepts_good_and_rejects_bad(settings):
    j = make(settings)
    assert j.automations.validate_cron("0 8 * * 1-5") is None
    assert j.automations.validate_cron("not a schedule") is not None


async def test_create_rejects_an_unparsable_schedule(settings):
    j = make(settings)
    result = j.automations.create("Bad one", "nonsense", "check something")
    assert "error" in result and j.automations.list_all() == []
    await j.http.aclose()


async def test_create_enforces_a_limit(settings):
    j = make(settings)
    for n in range(MAX_AUTOMATIONS):
        result = j.automations.create(f"Check {n}", "0 8 * * *", "look at something")
        assert "id" in result
    over = j.automations.create("One too many", "0 9 * * *", "look at something else")
    assert "error" in over and "limit" in over["error"]
    assert len(j.automations.list_all()) == MAX_AUTOMATIONS
    await j.http.aclose()


# --------------------------------------------------------------------------- scheduler wiring
async def test_create_and_delete_register_with_the_live_scheduler(settings):
    j = make(settings)
    j.scheduler = AsyncIOScheduler(timezone=settings.timezone)
    j.scheduler.start()
    try:
        result = j.automations.create("Overdue jobs check", "0 8 * * 1-5", "check for overdue jobs")
        job_id = f"automation_{result['id']}"
        assert j.scheduler.get_job(job_id) is not None

        msg = j.automations.delete(result["id"])
        assert "Removed" in msg
        assert j.scheduler.get_job(job_id) is None
        assert j.automations.list_all() == []
    finally:
        j.scheduler.shutdown(wait=False)
        await j.http.aclose()


async def test_register_all_brings_saved_automations_back_after_a_restart(settings):
    j = make(settings)
    j.automations.create("Overdue jobs check", "0 8 * * 1-5", "check for overdue jobs")
    j.automations.create("Supplier email watch", "*/30 * * * *", "check inbox for the delayed order")

    j.scheduler = AsyncIOScheduler(timezone=settings.timezone)
    j.scheduler.start()
    try:
        assert j.scheduler.get_job("automation_1") is None  # not registered until register_all runs
        j.automations.register_all()
        assert j.scheduler.get_job("automation_1") is not None
        assert j.scheduler.get_job("automation_2") is not None
    finally:
        j.scheduler.shutdown(wait=False)
        await j.http.aclose()


def test_delete_of_an_unknown_automation_is_reported_not_raised(settings):
    j = make(settings)
    assert "No automation" in j.automations.delete(999)


# --------------------------------------------------------------------------- running one
async def test_run_asks_the_brain_and_records_the_result(settings):
    j = make(settings, [message([text_block("Nothing overdue, sir - all clear.")])])
    result = j.automations.create("Overdue jobs check", "0 8 * * 1-5", "Check for overdue jobs and tell me.")
    reply = await j.automations.run(result["id"])
    assert reply == "Nothing overdue, sir - all clear."
    stored = j.db.get_automation(result["id"])
    assert stored["last_result"] == "Nothing overdue, sir - all clear." and stored["last_run_at"]
    # the brain was given the automation's own prompt, tagged as a scheduled check, not a live message
    sent = j.brain.messages[0]["content"][-1]["text"]
    assert "Overdue jobs check" in sent and "Check for overdue jobs" in sent
    await j.http.aclose()


async def test_run_of_a_deleted_automation_is_reported_not_raised(settings):
    j = make(settings)
    assert "No automation" in await j.automations.run(999)
    await j.http.aclose()


async def test_a_failing_run_is_recorded_not_raised(settings, monkeypatch):
    j = make(settings)
    result = j.automations.create("Broken check", "0 8 * * *", "do something")

    async def boom(*a, **k):
        raise RuntimeError("Claude is unavailable")

    monkeypatch.setattr(j.brain, "ask", boom)
    await j.automations._run_guarded(result["id"])  # noqa: SLF001 - this is exactly what the scheduler calls
    stored = j.db.get_automation(result["id"])
    assert "Failed" in stored["last_result"] and "Claude is unavailable" in stored["last_result"]
    await j.http.aclose()


# --------------------------------------------------------------------------- the chat tools
async def test_create_list_delete_automation_tools(settings):
    j = make(settings)
    create_tool = TOOLS_BY_NAME["create_automation"]
    list_tool = TOOLS_BY_NAME["list_automations"]
    delete_tool = TOOLS_BY_NAME["delete_automation"]
    assert not create_tool.approval and not list_tool.approval and not delete_tool.approval

    created = await create_tool.handler(j, CreateAutomationIn(
        description="Weekly review", cron="0 9 * * 1", prompt="Summarise last week's completed jobs"))
    assert created["id"] == 1
    assert created["schedule"] == "every Monday at 9am"  # plain English for Jarvis to read back, not the cron

    listed = await list_tool.handler(j, NoInput())
    assert len(listed) == 1 and listed[0]["description"] == "Weekly review"
    assert listed[0]["schedule"] == "every Monday at 9am"

    deleted = await delete_tool.handler(j, DeleteAutomationIn(automation_id=1))
    assert "Removed" in deleted
    await j.http.aclose()
