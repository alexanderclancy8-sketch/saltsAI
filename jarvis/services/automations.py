"""User-defined automations: the owner sets up their own scheduled checks ("every weekday at 8am, check for
overdue jobs and tell me") without needing a code change or a redeploy.

Each automation runs Jarvis's own conversational loop, headlessly, on a cron schedule - the same tools and the
same "nothing changes without approval" rules as any other turn apply. An automation can look things up and
decide to tell the owner; anything it wants to write still goes through the ordinary approval queue exactly as
if the owner had asked for it live.

This is schedule-based, not truly event-driven: "whenever an email like X arrives" becomes "check regularly for
an email like X" under the hood. That's simpler and far more robust than a generic event-matching engine, and in
practice reads the same to the owner once it's running.
"""

from __future__ import annotations

import logging

from apscheduler.triggers.cron import CronTrigger

from ..db import now_iso

log = logging.getLogger(__name__)
MAX_AUTOMATIONS = 25  # generous, but stops a runaway conversation from scheduling hundreds of jobs


class AutomationService:
    def __init__(self, j):
        self.j = j

    def validate_cron(self, cron: str) -> str | None:
        try:
            CronTrigger.from_crontab(cron, timezone=self.j.settings.timezone)
        except Exception as e:  # noqa: BLE001
            return str(e)
        return None

    def create(self, description: str, cron: str, prompt: str) -> dict:
        error = self.validate_cron(cron)
        if error:
            return {"error": f"That schedule doesn't parse ({error}). Use standard 5-field cron, "
                             "e.g. '0 8 * * 1-5' for weekdays at 8am."}
        if len(self.j.db.list_automations()) >= MAX_AUTOMATIONS:
            return {"error": f"Already at the limit of {MAX_AUTOMATIONS} automations - remove one first."}
        automation_id = self.j.db.create_automation(description, cron, prompt)
        self._register(self.j.db.get_automation(automation_id))
        return {"id": automation_id, "description": description, "cron": cron}

    def list_all(self) -> list[dict]:
        return self.j.db.list_automations()

    def delete(self, automation_id: int) -> str:
        automation = self.j.db.get_automation(automation_id)
        if not automation:
            return f"No automation #{automation_id}."
        self._unregister(automation_id)
        self.j.db.delete_automation(automation_id)
        return f"Removed automation #{automation_id} (\"{automation['description']}\")."

    def register_all(self) -> None:
        """Called once at startup, once the scheduler exists, to bring saved automations back to life."""
        for automation in self.j.db.list_automations():
            self._register(automation)

    def _job_id(self, automation_id: int) -> str:
        return f"automation_{automation_id}"

    def _register(self, automation: dict) -> None:
        sched = self.j.scheduler
        if not automation["enabled"] or sched is None:
            return
        self._unregister(automation["id"])
        sched.add_job(self._run_guarded, CronTrigger.from_crontab(automation["cron"], timezone=self.j.settings.timezone),
                     id=self._job_id(automation["id"]), args=[automation["id"]], max_instances=1, coalesce=True)

    def _unregister(self, automation_id: int) -> None:
        sched = self.j.scheduler
        if sched is not None and sched.get_job(self._job_id(automation_id)):
            sched.remove_job(self._job_id(automation_id))

    async def _run_guarded(self, automation_id: int) -> None:
        try:
            await self.run(automation_id)
        except Exception as e:  # noqa: BLE001
            log.exception("Automation %s failed", automation_id)
            self.j.db.update_automation(automation_id, last_run_at=now_iso(), last_result=f"Failed: {e}"[:2000])

    async def run(self, automation_id: int) -> str:
        automation = self.j.db.get_automation(automation_id)
        if not automation:
            return f"No automation #{automation_id}."
        prompt = (f"[Scheduled check you set up: \"{automation['description']}\"]\n{automation['prompt']}\n\n"
                 "This is an automation running on its own schedule, not something typed live - if there's "
                 "nothing worth mentioning, say so briefly rather than manufacturing a finding.")
        reply = await self.j.brain.ask(prompt, "typed")
        self.j.db.update_automation(automation_id, last_run_at=now_iso(), last_result=reply[:2000])
        return reply
