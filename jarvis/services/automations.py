"""User-defined automations: the owner sets up their own scheduled checks ("every weekday at 8am, check for
overdue jobs and tell me") without needing a code change or a redeploy.

Each automation runs Jarvis's own conversational loop, headlessly, on a cron schedule - the same tools and the
same "nothing changes without approval" rules as any other turn apply. An automation can look things up and
decide to tell the owner; anything it wants to write still goes through the ordinary approval queue exactly as
if the owner had asked for it live.

This is schedule-based, not truly event-driven: "whenever an email like X arrives" becomes "check regularly for
an email like X" under the hood. That's simpler and far more robust than a generic event-matching engine, and in
practice reads the same to the owner once it's running.

Whose permissions it runs with. An automation is created in one person's turn and runs later on the scheduler, where no one is
"asking". It runs with the permissions of whoever CREATED it - the creator's role (owner | manager | team) is stored with it and
its run re-creates that caller in ``access.current_caller``, the one place every role-dependent tool already looks - never with the
owner's by default. So a manager's automation can no more read the owner-only FSM resources (finance, staff pay, HR) or call an
owner-only tool than that manager could in the chat, whoever's turn or click sets it going, and what it finds is only ever told in
the console (never pushed on to Teams or email). A row with no recorded creator is read as a manager's (least privilege) and the
owner can take it over on purpose. A team member can never create one, and changing an automation never raises its role.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import access
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

    # ------------------------------------------------------------------ who may do what to which automation
    @staticmethod
    def _caller() -> access.Caller | None:
        """Who is asking right now: the marked manager / team caller of this turn, or None (the owner's own conversation)."""
        return access.current_caller.get()

    @staticmethod
    def role_of(automation: dict) -> str:
        """The role an automation runs with (a missing or unknown stored value is a manager's - never the owner's)."""
        return access.stored_role(automation.get("role"))

    @staticmethod
    def creator_label(automation: dict) -> str:
        """'the owner', 'a manager (Sam)', 'a manager (set up before roles were recorded)'."""
        role, who = access.stored_role(automation.get("role")), str(automation.get("created_by") or "").strip()
        if role == access.OWNER:
            return "the owner"
        if role == access.TEAM:
            return f"a team member ({who})" if who else "a team member"
        return f"a manager ({who})" if who else "a manager (set up before roles were recorded)"

    def _may_change(self, automation: dict) -> str:
        """"" when the asker may change / delete this automation, else why not. Nobody changes one that outranks them: that would
        let a manager reword the owner's check (and so what it runs with), or take it away."""
        if access.outranks(self.role_of(automation), access.role_of(self._caller())):
            return (f"Automation #{automation['id']} (\"{automation['description']}\") was set up by "
                    f"{self.creator_label(automation)} and runs with their permissions, so only they can change or remove it.")
        return ""

    def create(self, description: str, cron: str, prompt: str, never_slow_down: bool = False) -> dict:
        caller = self._caller()
        role = access.role_of(caller)
        if role not in access.AUTOMATION_ROLES:
            return {"error": "Team accounts can't set up automations - ask the office."}
        error = self.validate_cron(cron)
        if error:
            return {"error": f"That schedule doesn't parse ({error}). Use standard 5-field cron, "
                             "e.g. '0 8 * * 1-5' for weekdays at 8am."}
        if len(self.j.db.list_automations()) >= MAX_AUTOMATIONS:
            return {"error": f"Already at the limit of {MAX_AUTOMATIONS} automations - remove one first."}
        # It is stored with its creator's role and runs with that role's permissions (the owner's own turn - no caller - is the owner's).
        automation_id = self.j.db.create_automation(description, cron, prompt, role,
                                                    caller.name if caller is not None else "")
        if never_slow_down:
            self.j.db.update_automation(automation_id, never_slow=1)
        self._register(self.j.db.get_automation(automation_id))
        out = {"id": automation_id, "description": description, "cron": cron, "schedule": cron_to_english(cron), "created_by_role": role}
        if role != access.OWNER:
            out["note"] = ("It runs with a manager's permissions, not the owner's: it can't read the owner-only FSM data (finance, staff "
                           "pay, HR) or use owner-only tools, and what it finds is told in the console only.")
        return out

    def list_all(self) -> list[dict]:
        """Every automation, with its effective interval, no-change streak, why it was slowed (heartbeat stop rules) and who created
        it. What a higher role's automation prompt and last result say is for that role: a manager asking sees the owner's listed
        (so the limit makes sense) but not what they say or found."""
        now, asker = self.clock(), access.role_of(self._caller())
        out = []
        for a in self.j.db.list_automations():
            role = self.role_of(a)
            row = {**a, "role": role, "created_by_role": role, "created_by": self.creator_label(a),
                   "schedule": cron_to_english(a["cron"]), "last_run_at": human_datetime(a["last_run_at"]),
                   **heartbeat.describe(a, self._configured(a, now))}
            if access.outranks(role, asker):
                row["prompt"] = row["last_result"] = "(set up by a higher role - not shown to you)"
            out.append(row)
        return out

    def set_never_slow_down(self, automation_id: int, never_slow_down: bool) -> str:
        """The owner's per-automation override: True stops it ever being slowed, skipped overnight or asked about."""
        automation = self.j.db.get_automation(automation_id)
        if not automation:
            return f"No automation #{automation_id}."
        refused = self._may_change(automation)
        if refused:
            return refused
        self.j.db.update_automation(automation_id, never_slow=1 if never_slow_down else 0)
        return (f"Automation #{automation_id} (\"{automation['description']}\") will "
                + ("never be slowed down now." if never_slow_down else "slow down again when it keeps finding nothing."))

    def edit(self, automation_id: int, description: str | None = None, cron: str | None = None, prompt: str | None = None,
             take_over: bool = False) -> dict | str:
        """Change what an automation says, when it runs or how it is described. Editing NEVER raises its role: a lower role can't edit
        a higher role's automation at all (refused), and an edit by the same or a higher role leaves the role as it was - so an
        owner rewording a manager's automation does not quietly give the new wording the owner's permissions. Only the owner, saying
        so (``take_over``), makes an automation the owner's own - read it first, since it will then run with the owner's access."""
        automation = self.j.db.get_automation(automation_id)
        if not automation:
            return f"No automation #{automation_id}."
        refused = self._may_change(automation)
        if refused:
            return refused
        asker = self._caller()
        if take_over and access.role_of(asker) != access.OWNER:
            return "Only the owner can make an automation run with the owner's permissions."
        if cron is not None:
            error = self.validate_cron(cron)
            if error:
                return {"error": f"That schedule doesn't parse ({error}). Use standard 5-field cron, e.g. '0 8 * * 1-5' for weekdays at 8am."}
        fields = {k: v for k, v in (("description", description), ("cron", cron), ("prompt", prompt)) if v is not None}
        if fields:
            self.j.db.update_automation(automation_id, **fields)
        if take_over:
            self.j.db.set_automation_role(automation_id, access.OWNER, "")
        updated = self.j.db.get_automation(automation_id)
        self._register(updated)
        role = self.role_of(updated)
        return {"id": automation_id, "description": updated["description"], "schedule": cron_to_english(updated["cron"]),
                "created_by_role": role,
                "message": (f"Automation #{automation_id} updated." + (
                    "" if role == access.OWNER else f" It still runs with {'a manager' if role == access.MANAGER else 'a team'}'s "
                    "permissions" + (" - the owner can take it over to give it the owner's." if access.role_of(asker) == access.OWNER else ".")))}

    def delete(self, automation_id: int) -> str:
        automation = self.j.db.get_automation(automation_id)
        if not automation:
            return f"No automation #{automation_id}."
        refused = self._may_change(automation)
        if refused:
            return refused
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
        role = self.role_of(automation)
        if role not in access.AUTOMATION_ROLES:  # team accounts can't create one; a row that says otherwise is not run at all
            return self._not_run(automation, f"it is marked as a {role} automation, and those aren't allowed to run")
        # The run is the CREATOR's: their role goes into the context variable the brains and tools read (always set, even for the
        # owner's None, so whoever's turn happens to be running when the scheduler fires never lends it theirs).
        as_caller = access.caller_for_role(role, str(automation.get("created_by") or ""))
        who = access.current_caller.set(as_caller)
        try:
            return await self._run_as_creator(automation, role)
        finally:
            access.current_caller.reset(who)

    def _not_run(self, automation: dict, why: str) -> str:
        note = f"Not run: {why}."
        self.j.db.update_automation(automation["id"], last_run_at=heartbeat.iso(self.clock()), last_result=note)
        self.j.activity.record(self._job_id(automation["id"]), automation["description"], "failed", note)
        return note

    async def _run_as_creator(self, automation: dict, role: str) -> str:
        automation_id = automation["id"]
        limits = ("" if role == access.OWNER else
                  "\n\nThis check was set up by a manager, so it runs with a manager's access, not the owner's: the owner-only FSM data "
                  "(finance, staff pay, HR, customer contact details) and owner-only tools refuse it. If it needs those, say so plainly "
                  "in your reply instead of trying another way to them.")
        prompt = (f"[Scheduled check you set up: \"{automation['description']}\"]\n{automation['prompt']}{limits}\n\n"
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
            # What a manager's check finds is told in the console only (never pushed on to Teams): its audience is the console's, which
            # is where its creator reads it, and it holds nothing above their own access.
            result = await self.j.proactive.tell(key, title if role == access.OWNER else f"{title} (set up by a manager)", reply,
                                                 teams=role == access.OWNER)
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
