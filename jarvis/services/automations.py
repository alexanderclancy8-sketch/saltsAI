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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..cron import cron_trigger
from ..events import quiet_turn
from ..humanize import cron_to_english, human_datetime
from . import heartbeat
from .activity import CHANGED, NO_CHANGE
from .proactive import NOTHING

log = logging.getLogger(__name__)
MAX_AUTOMATIONS = 25  # generous, but stops a runaway conversation from scheduling hundreds of jobs


class AutomationService:
    def __init__(self, j, clock=None):
        self.j = j
        # "now" for the heartbeat stop rules (back-off, overnight, ask-once); tests pass a fake clock. Always timezone-aware.
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.j.settings.timezone)
        except Exception:  # noqa: BLE001
            return ZoneInfo("Europe/London")

    def _configured(self, automation: dict, now: datetime) -> int:
        return heartbeat.configured_minutes(automation["cron"], self.j.settings.timezone, now)

    def validate_cron(self, cron: str) -> str | None:
        try:
            cron_trigger(cron, timezone=self.j.settings.timezone)
        except Exception as e:  # noqa: BLE001
            return str(e)
        return None

    def create(self, description: str, cron: str, prompt: str, never_slow_down: bool = False) -> dict:
        error = self.validate_cron(cron)
        if error:
            return {"error": f"That schedule doesn't parse ({error}). Use standard 5-field cron, "
                             "e.g. '0 8 * * 1-5' for weekdays at 8am."}
        if len(self.j.db.list_automations()) >= MAX_AUTOMATIONS:
            return {"error": f"Already at the limit of {MAX_AUTOMATIONS} automations - remove one first."}
        automation_id = self.j.db.create_automation(description, cron, prompt)
        if never_slow_down:
            self.j.db.update_automation(automation_id, never_slow=1)
        self._register(self.j.db.get_automation(automation_id))
        return {"id": automation_id, "description": description, "cron": cron,
                "schedule": cron_to_english(cron)}

    def list_all(self) -> list[dict]:
        """Every automation, with its effective interval, no-change streak and why it was slowed (heartbeat stop rules)."""
        now = self.clock()
        return [{**a, "schedule": cron_to_english(a["cron"]), "last_run_at": human_datetime(a["last_run_at"]),
                 **heartbeat.describe(a, self._configured(a, now))} for a in self.j.db.list_automations()]

    def set_never_slow_down(self, automation_id: int, never_slow_down: bool) -> str:
        """The owner's per-automation override: True stops it ever being slowed, skipped overnight or asked about."""
        automation = self.j.db.get_automation(automation_id)
        if not automation:
            return f"No automation #{automation_id}."
        self.j.db.update_automation(automation_id, never_slow=1 if never_slow_down else 0)
        return (f"Automation #{automation_id} (\"{automation['description']}\") will "
                + ("never be slowed down now." if never_slow_down else "slow down again when it keeps finding nothing."))

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
        sched.add_job(self._run_guarded, cron_trigger(automation["cron"], timezone=self.j.settings.timezone),
                     id=self._job_id(automation["id"]), args=[automation["id"]], max_instances=1, coalesce=True)

    def _unregister(self, automation_id: int) -> None:
        sched = self.j.scheduler
        if sched is not None and sched.get_job(self._job_id(automation_id)):
            sched.remove_job(self._job_id(automation_id))

    def _skip_reason(self, automation_id: int) -> str:
        """Heartbeat stop rules: is this cron fire to be skipped (slowed down, or overnight)? Fails open - a problem working
        it out runs the check rather than silently never running it."""
        try:
            automation = self.j.db.get_automation(automation_id)
            if not automation:
                return ""
            now = self.clock()
            return heartbeat.skip_reason(automation, self._configured(automation, now), now, self._tz())
        except Exception:  # noqa: BLE001
            log.exception("Heartbeat check for automation %s failed", automation_id)
            return ""

    async def _run_guarded(self, automation_id: int) -> None:
        why = self._skip_reason(automation_id)
        if why:
            log.info("Automation %s skipped this time: %s", automation_id, why)  # not logged as a check: it didn't run
            return
        try:
            await self.run(automation_id)
        except Exception as e:  # noqa: BLE001
            log.exception("Automation %s failed", automation_id)
            self.j.db.update_automation(automation_id, last_run_at=heartbeat.iso(self.clock()),
                                        last_result=f"Failed: {e}"[:2000])
            automation = self.j.db.get_automation(automation_id) or {}
            self.j.activity.record(self._job_id(automation_id), automation.get("description") or f"Automation {automation_id}",
                                   "failed", f"Failed: {type(e).__name__}")

    async def run(self, automation_id: int) -> str:
        automation = self.j.db.get_automation(automation_id)
        if not automation:
            return f"No automation #{automation_id}."
        prompt = (f"[Scheduled check you set up: \"{automation['description']}\"]\n{automation['prompt']}\n\n"
                 "This is an automation running on its own schedule, not something typed live - if there's "
                 "nothing worth mentioning, say so briefly rather than manufacturing a finding. "
                 f"If there is nothing new to report, start your reply with {NOTHING}.")
        checklist = heartbeat.read_checklist(self.j.settings.data_dir)
        if checklist:  # the owner's optional HEARTBEAT.md: house rules for scheduled runs (never overrides approvals)
            prompt += ("\n\nHouse rules for scheduled runs (from HEARTBEAT.md; they never override the approval rules):\n"
                       + checklist)
        # A scheduled check always runs silently: the headless turn is never typed into whatever chat happens to be
        # open. Only what it found is posted (below), and only when it is new; every run is in the activity log.
        token = quiet_turn.set(True)
        try:
            reply = await self.j.brain.ask(prompt, "typed")
        finally:
            quiet_turn.reset(token)
        self.j.db.update_automation(automation_id, last_run_at=heartbeat.iso(self.clock()), last_result=reply[:2000])
        title, key = automation["description"], f"automation:{automation_id}"
        if not reply.strip() or reply.strip().upper().startswith(NOTHING):
            outcome, detail = NO_CHANGE, reply.strip()[len(NOTHING):].lstrip(" :-.") or "Nothing to report."
        else:
            result = await self.j.proactive.tell(key, title, reply)
            if result["delivered"]:
                outcome, detail = CHANGED, reply
            elif result["reason"] in ("unchanged", "nothing to report"):
                outcome, detail = NO_CHANGE, "Same as last time."
            else:  # something new, but it could not be said right now (quiet hours, you were mid-conversation)
                outcome, detail = CHANGED, f"{reply} (held back: {result['reason']})"
        self.j.activity.record(self._job_id(automation_id), title, outcome, detail)
        await self._track(automation, outcome == NO_CHANGE)
        return reply

    async def _track(self, automation: dict, no_change: bool) -> None:
        """Heartbeat stop rules after a run: count the no-change streak (or reset it), and ask the owner once if it has been
        quiet for 12 hours. Never raises - bookkeeping must not turn a good run into a failed one."""
        try:
            now = self.clock()
            fields = heartbeat.next_state(automation, no_change, now)
            self.j.db.update_automation(automation["id"], **fields)
            updated = {**automation, **fields}
            configured = self._configured(updated, now)
            if heartbeat.should_ask(updated, configured, now, self._tz()):
                effective = heartbeat.effective_minutes(configured, fields["nochange_streak"])
                subject, body = heartbeat.ask_message(updated, effective, now)
                await self.j.notifier.send_owner_update(subject, body, channels=("teams",))  # Teams only, one short message
                self.j.db.update_automation(automation["id"], last_asked_at=heartbeat.iso(now))
        except Exception:  # noqa: BLE001
            log.exception("Heartbeat bookkeeping for automation %s failed", automation.get("id"))
