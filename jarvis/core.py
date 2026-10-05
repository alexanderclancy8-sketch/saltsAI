"""Wires every integration and service together into one Jarvis instance."""

from __future__ import annotations

import asyncio
import logging
from datetime import date

import httpx

from .brain import llm, plugins
from .brain.agent import JarvisBrain
from .brain.trace import TurnTrace
from .config import Settings, apply_timezone
from .db import Database
from .events import EventBus
from .humanize import cron_to_english
from .integrations.azure import BlobArchive, KuduDeployer
from .integrations.finance import build_finance
from .integrations.fsm import FSMRouter
from .integrations.github import GitHub
from .integrations.marketing import PresenceSources
from .integrations.microsoft365 import DemoMail, GraphMail, TeamsNotifier
from .integrations.teamsbot import TeamsBot
from .integrations.ramtracking import DemoRamTracking, RamTracking, missing_credentials
from .integrations.voice import SpeechToTextCheck, Voice
from .knowledge import KnowledgeBase
from .services.accountant import Accountant
from .services.accreditations import Accreditations
from .services.actions import ActionExecutor
from .services.advisor import Advisor
from .services.standing_approvals import StandingApprovals
from .services.teams_approvals import TeamsApprovals
from .services.activity import CHANGED, ActivityLog
from .services.automations import AutomationService
from .services.billing import Billing
from .services.customer_comms import CustomerComms
from .services.customers import CustomerHealth
from .services.digest import WeeklyDigest
from .services.documents import Documents
from .services.false_alarms import FalseAlarmLog
from .services.images import ImageGenerator
from .services.council_intake import CouncilIntake
from .services.job_intake import JobIntake
from .services.meetings import Meetings
from .services.ooh import OutOfHours
from .services.briefing import Briefings
from .services.fixer import Fixer
from .services.issues import IssueService
from .services.marketing import MarketingTracker
from .services.notifier import Notifier
from .services.performance import PerformanceReviewer, StaffRegister
from .services.po_intake import PoIntake
from .services.supplier_bills import PurchaseOrderBook, SupplierBills
from .services.async_tools import AsyncTools
from .services.proactive import Proactive
from .services.ppm_planner import PPMPlanner
from .services.route_advisor import RouteAdvisor
from .services.recruiter import Recruiter
from .services.regulatory import RegulatoryWatch
from .services.renewals import Renewals
from .services.reply_suggestions import ReplySuggestions
from .services.service_inbox import ServiceInbox
from .services.routine_tests import RoutineTester
from .services.fsm_engineer import FsmEngineer
from .services.security_watch import SecurityWatch
from .services.conversation_quality import ConversationQuality
from .services.self_improve import SelfImprove
from .services.self_learning import SelfLearning
from .services.site_access import SiteAccessCodes
from .services.staff import StaffMonitor
from .services.stores import Stores
from .services.suggestions import Suggestions
from .services.team_access import TeamAccess
from .services.team_sessions import TeamSessions
from .services.verification import ActionVerifier
from .services.wrapup import WrapUp
from .services.engineer_homes import EngineerHomes
from .services.tracking import Tracker

log = logging.getLogger(__name__)


class Jarvis:
    def __init__(self, settings: Settings, db: Database | None = None, http: httpx.AsyncClient | None = None,
                 client=None):
        s = self.settings = settings
        apply_timezone(s.timezone)
        self.db = db or Database(settings.db_path)
        self.db.maintain_transcript()  # 2-year retention; older rows are redacted once
        self.bus = EventBus()
        self.http = http or httpx.AsyncClient(timeout=30, headers={"User-Agent": "salts-jarvis/1.0"})
        self.client = client or llm.make_client(settings)
        self.kb = KnowledgeBase(settings.knowledge_dir)

        # integrations (demo stand-ins where not configured)
        self.mail = GraphMail(s, self.http) if s.graph_configured else DemoMail(s)
        self.teams = TeamsNotifier(s.teams_webhook_url, self.http)
        self.teamsbot = TeamsBot(s, self.http)
        self.fsm = FSMRouter(s, self.http)
        self.ram = (RamTracking(s, self.http) if s.ram_client_id and s.ram_api_key and s.ram_username and s.ram_password
                    else DemoRamTracking(self.fsm))
        self.finance = build_finance(s, self.http, self.db)
        self.github = GitHub(s.github_token, s.fsm_repo, self.http, s.fsm_default_branch) if s.github_configured else None
        self.self_github = (GitHub(s.jarvis_github_token or s.github_token, s.jarvis_repo, self.http,
                                   s.jarvis_default_branch, follow_remote_default=s.jarvis_follow_default_branch)
                            if s.jarvis_self_improve_configured else None)
        self.blob = BlobArchive(s)
        self.kudu = KuduDeployer(s, self.http)
        self.voice = Voice(s, self.http)
        self.presence = PresenceSources(s, self.http)

        # services
        self.notifier = Notifier(s, self.db, self.bus, self.mail, self.teams)
        self.staff = StaffMonitor(self.fsm)
        self.register = StaffRegister(s.staff_roles_file)
        self.reviewer = PerformanceReviewer(self.register, self.staff, self.fsm, self.mail, self.notifier)
        self.accountant = Accountant(s, self.finance, self.fsm, self.staff)
        self.fixer = Fixer(s, self.db, self.bus, self.notifier, self.client, self.github, self.kudu, self.http)
        self.issues = IssueService(s, self.db, self.bus, self.notifier, self.client, self.mail, self.fixer)
        self.tester = RoutineTester(s, self.db, self.http, fsm=self.fsm, staff=self.staff,
                                    integrations=self._integrations(), notifier=self.notifier, issues=self.issues)
        self.fixer.issues, self.fixer.tester = self.issues, self.tester
        self.security_watch = SecurityWatch(s, self.db, self.bus, self.notifier, self.client, self.github, self.issues)
        self.self_improve = SelfImprove(s, self.db, self.bus, self.notifier, self.client, self.self_github)
        # Engineering runs a restart/crash cut off stay 'running' forever; close the ones no live process can own
        # (age-gated, so a second process during a rolling deploy keeps its own run). Never raises.
        self.self_improve.runs.interrupt_stale()
        self.actions = ActionExecutor(self.db, self.bus, self.notifier, self.mail, self.fixer, self.fsm)
        self.billing = Billing(s, self.db, self.fsm, self.finance, self.actions, self.notifier)
        self.actions.billing = self.billing
        self.actions.j = self
        self.actions.standing = StandingApprovals(s, self.db)  # the owner's switches, default off; read-only here
        self.actions.teams_approvals = TeamsApprovals(s, self.db, self.teamsbot)  # no-op until the bot is set up
        self.po_intake = PoIntake(s, self.db, self.bus, self.notifier, self.client, self.mail, self.fsm, self.actions)
        # voicemail / call-transcript emails -> proposed jobs; each is queued for approval, never created directly
        self.job_intake = JobIntake(s, self.db, self.bus, self.notifier, self.client, self.mail, self.fsm, self.actions)
        # the second shared mailbox (service@): read for Comms / the Test button; council portal requests in it -> proposed jobs
        self.service_inbox = ServiceInbox(self)
        self.council_intake = CouncilIntake(s, self.db, self.bus, self.notifier, self.client, self.mail, self.fsm,
                                            self.actions)
        self.verifier = ActionVerifier(s)  # optional ThoughtProof check on approved actions (off by default)
        self.issues.actions = self.actions
        self.fsm_engineer = FsmEngineer(self)  # read-only FSM audit; hands failures to issue_fix (still needs approval)
        self.briefings = Briefings(s, self.db, self.mail, self.staff, self.accountant, self.notifier, self.client)
        self.marketing = MarketingTracker(s, self.db, self.http, self.presence, self.notifier, self.client)
        self.advisor = Advisor(s, self.db, self.accountant, self.reviewer, self.staff, self.marketing, self.notifier,
                               self.client, self.bus)
        self.accreditations = Accreditations(s, self.db, self.staff, self.fsm, self.notifier, self.client, self.bus)
        self.stores = Stores(self.db, demo_seed=self.fsm.demo, fsm=self.fsm)
        self.regwatch = RegulatoryWatch(s, self.db, self.notifier, self.client, self.bus, self.mail)
        self.regwatch.actions = self.actions
        # settings + db: the owner's out-of-hours van-location setting, its look-up log and the on-call roster
        # The owner's engineer home points (a rounded map point each, never a postcode): what lets a van be "home" when RAM
        # supplies no address labels. Owner-only routes in main.py; deliberately not a tool, so the model can't reach it.
        self.homes = EngineerHomes(self.db, self.http, self.fsm, self.register, self.ram)
        self.tracker = Tracker(self.fsm, self.http, self.ram, self.register, s.timesheet_tolerance_min,
                               settings=s, db=self.db, homes=self.homes)
        self.oncall = self.tracker.roster
        self.asked_by = ""  # who is asking in the current chat turn (set by the brains); "" outside a turn
        self.ppm = PPMPlanner(self.fsm, self.register)  # read-only advisory scheduling plan
        self.route_advisor = RouteAdvisor(self.fsm, self.tracker, self.register)  # read-only route advice
        self.customers = CustomerHealth(self)
        self.advisor.j_customers = self.customers
        self.renewals = Renewals(self)
        self.customer_comms = CustomerComms(self)  # drafts lifecycle emails; each is queued for approval, never sent
        self.meetings = Meetings(self)
        self.ooh = OutOfHours(self)
        self.briefings.ooh = self.ooh
        self.briefings.j = self
        self.po_book = PurchaseOrderBook(self.db)  # purchase orders raised via log_purchase_order
        self.supplier_bills = SupplierBills(self)
        self.documents = Documents(self)
        self.images = ImageGenerator(self)  # draft social media graphics; never posted anywhere
        self.suggestions = Suggestions(self)
        self.wrapup = WrapUp(self)
        self.scheduler = None
        self.activity = ActivityLog(self)  # every scheduled check's runs; the chat shows one quiet line per check
        # Owner set / clear of an engineer's home point: engineer name and time only, never a postcode or a point.
        self.homes.audit = lambda action, detail: self.activity.record("engineer_homes", "Engineer homes", CHANGED, detail)
        self.proactive = Proactive(self)  # Jarvis posting into the open chat by himself; tells, never acts
        self.async_tools = AsyncTools(self)  # slow tools run in the background; results delivered via self.proactive
        self.automations = AutomationService(self)
        self.quality = ConversationQuality(self)  # per-turn metrics + feedback; must exist before the brain below
        self.quality.prune()  # retention for its tables (default 90 days); also run daily by the scheduler
        self.self_learning = SelfLearning(self)
        self.weekly_digest = WeeklyDigest(self)
        self.reply_suggestions = ReplySuggestions(self)
        self.site_access = SiteAccessCodes(self)
        self.false_alarms = FalseAlarmLog(self)  # BS 5839-1 false alarm log; writes to FSM never happen from here
        self.recruiter = Recruiter(self)
        # Describes each chat turn from the tool events (source line, pop-up button, follow-ups) - see brain/trace.py.
        self.trace = TurnTrace(self)
        self.bus.add_tap(self.trace.on_event)
        # Team mode: the access code for engineers/office staff (hashed, in the database) and one cut-down brain per team session.
        self.team_access = TeamAccess(self.db)
        self.team_sessions = TeamSessions(self)
        self._seed_notes()
        if s.effective_llm_backend == "max":
            from .brain.max_backend import MaxBrain

            self.brain = MaxBrain(self)
        else:
            self.brain = JarvisBrain(self)
        self._startup_tasks: set[asyncio.Task] = set()

    def _seed_notes(self) -> None:
        known = {m["fact"].strip().lower() for m in self.db.memories()}
        for note in (n.strip() for n in self.settings.jarvis_notes.split("|")):
            if note and note.lower() not in known:
                self.db.remember(note)
                known.add(note.lower())

    def _integrations(self) -> dict:
        out = {}
        if not self.mail.demo:
            out["Microsoft 365 mail"] = self.mail
        if not self.fsm.demo:
            out["Salts FSM API"] = self.fsm
        if not getattr(self.finance, "demo", False):
            out[f"Accounts ({self.finance.name})"] = self.finance
        if self.github:
            out["GitHub (FSM source)"] = self.github
        if not self.ram.demo:
            out["RAM Tracking"] = self.ram
        if self.settings.effective_stt in ("deepgram", "whisper"):  # browser STT has no server path to test
            out["Speech-to-text"] = SpeechToTextCheck(self.voice)
        return out

    def service_inbox_status(self) -> str:
        """One honest line for the Connections list: off, waiting for Microsoft 365, or what Jarvis does with the mailbox.
        Never says DEMO: there is no sample service inbox, so it doesn't add to the console's sample-data count."""
        s = self.settings
        if not self.service_inbox.enabled:
            return "off - no address set (Settings > Service inbox)"
        if self.mail.demo:
            return f"{self.service_inbox.address} - waiting for Microsoft 365 to be connected"
        scan = (f"council portal requests are proposed as jobs every {s.inbox_check_interval_min} min, for your approval"
                if s.council_intake_enabled else "council request scan is off")
        return f"{self.service_inbox.address} - readable from Comms; {scan}"

    def vehicle_tracking_status(self) -> str:
        """One honest line for the Connections list and the Fleet pop-up: sample data (and exactly which RAM detail is
        still missing), RAM entered but not working (with the reason), or live. "NOT CONNECTED" never contains the word
        DEMO, so the console counts it as a failing connection, not as sample data."""
        if self.ram.demo:
            missing = missing_credentials(self.settings)
            return ("DEMO journeys - still missing: " + ", ".join(missing) + " (Connections > RAM Tracking)"
                    if missing else "DEMO journeys - RAM Tracking is not connected")
        health = getattr(self.ram, "health", None) or {}
        if health.get("rate_limited"):  # RAM is busy, not broken: never reported as "not connected"
            return "RAM Tracking (rate limited just now, retry shortly)"
        if health.get("ok") is False:
            return f"NOT CONNECTED - RAM Tracking is failing: {health.get('detail') or 'it is not answering'}"
        return "RAM Tracking"

    def connections(self) -> dict[str, str]:
        s = self.settings
        presence = self.presence.configured()
        return {
            "Email (Outlook)": "connected" if not self.mail.demo else "DEMO data - connect Microsoft 365",
            "Teams updates": "connected" if self.teams.enabled else "not set up",
            "Teams chat": "connected" if self.teamsbot.configured else "not set up",
            "Standing approvals": ", ".join(
                n for n, on in (("record keeping", s.standing_record_keeping),
                                ("routine acknowledgements", s.standing_acknowledgements)) if on) or "off",
            "Salts FSM": "connected" if not self.fsm.demo else "DEMO data - set FSM_BASE_URL",
            "Accounts": (f"{self.finance.name}" if not getattr(self.finance, "demo", False)
                         else "DEMO data - connect Sage or add CSV exports"),
            "FSM source / auto-fix": f"{s.fsm_repo} ({s.fixer_mode})" if self.github else "not connected",
            "Security watch": (f"reviewing {s.fsm_repo} on a schedule" if self.security_watch.enabled
                               else "not set up (needs the same GitHub connection as auto-fix)"),
            "Self-improvement": (f"can propose PRs against {s.jarvis_repo} (never merges or deploys them)"
                                 if self.self_improve.enabled else "not set up (add a repo + token on Settings)"),
            "Automations": (f"{len(self.automations.list_all())} you've set up"
                            if self.automations.list_all() else "none set up yet - just ask"),
            "PO intake": (f"scans the inbox every {s.inbox_check_interval_min} min for customer purchase orders, "
                         "matches them to a sent quote and queues the job for your approval" if not self.mail.demo
                         else "DEMO data - connect Microsoft 365"),
            "Service inbox (service@)": self.service_inbox_status(),
            "Speaking up in chat": (f"on - quiet {s.proactive_quiet_start} to {s.proactive_quiet_end}, at most "
                                    f"{s.proactive_max_per_hour or 'any number'} an hour"
                                    if s.proactive_chat_enabled else "off - Jarvis only answers when you ask"),
            "Self-learning": f"reflects on recent conversations {cron_to_english(s.self_learning_cron)}",
            "Weekly digest": (f"routine engineering notices sent to Teams {cron_to_english(s.weekly_digest_cron)}"
                              if s.weekly_digest_enabled else "off - every notice is sent straight away"),
            "Azure deploy": s.azure_deploy_mode if self.github or self.kudu.enabled else "not set up",
            "Azure archive": "connected" if self.blob.enabled else "not set up",
            "Voice": f"TTS {s.effective_tts}, STT {s.effective_stt}",
            "Image generation": self.images.status(),
            "Socials / Google": ", ".join(k for k, v in presence.items() if v) or
                                "DEMO data - connect Facebook, Instagram, LinkedIn, TikTok or Google reviews",
            "Stores / stock": (self.stores.source if not self.stores.demo else
                               "DEMO stock - connect Salts FSM, or clear the sample items and enter your own"),
            "Staff register": ("connected" if not self.register.demo else
                               "DEMO data - tell me each person's role, duties and targets (you approve each one)"),
            "Vehicle tracking": self.vehicle_tracking_status(),
            "Web search": "on" if s.web_search_enabled else "off",
            "Plugins": plugins.status_line(s, self.verifier),
            "Claude": ("your Claude Max subscription (Agent SDK)" if s.effective_llm_backend == "max"
                       else f"Claude API ({s.jarvis_model})"),
        }

    async def business_review(self) -> None:
        await self.advisor.report(deliver=True)

    async def start(self) -> None:
        if hasattr(self.brain, "warm"):  # start Claude Code now so the first message is quick too
            task = asyncio.create_task(self.brain.warm())
            self._startup_tasks.add(task)
            task.add_done_callback(self._startup_tasks.discard)
        if self.settings.scheduler_enabled:
            from .services.scheduler import build_scheduler

            self.scheduler = build_scheduler(self)
            self.scheduler.start()
            self.automations.register_all()
            task = asyncio.create_task(self._first_run())
            self._startup_tasks.add(task)
            task.add_done_callback(self._startup_tasks.discard)

    async def _first_run(self) -> None:
        await asyncio.sleep(3)
        try:
            await self.tester.run("all")
            await self.marketing.snapshot()
            await self.suggestions.sweep(announce=False)
        except Exception:  # noqa: BLE001
            log.exception("Initial routine test run failed")

    async def stop(self) -> None:
        await self.proactive.stop()
        await self.async_tools.stop()
        await self.team_sessions.close()
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        if hasattr(self.brain, "close"):
            await self.brain.close()
        await self.http.aclose()

    async def daily_billing(self) -> None:
        result = await self.billing.queue_invoices()
        if result.get("queued"):
            await self.notifier.notify(f"{result['queued']} completed jobs not invoiced",
                                       "Draft invoices are waiting for your approval on the display.", level="warning",
                                       push=True, speak=True, importance="normal", management_only=True)  # finance

    async def daily_reviews(self) -> None:
        result = await self.billing.queue_review_requests()
        if result.get("queued"):
            await self.notifier.notify(f"{result['queued']} review requests ready", "Approve them on the display.",
                                       importance="normal")

    async def customer_comms_sweep(self) -> None:
        result = await self.customer_comms.draft_all()
        if result.get("queued"):
            await self.notifier.notify(f"{len(result['queued'])} customer email(s) drafted",
                                       "Approve or cancel them on the display - nothing has been sent.",
                                       importance="normal")

    async def lone_worker_sweep(self) -> int:
        """Alerts for engineers still on a job well past its end; returns how many new ones were raised."""
        raised = 0
        for c in await self.tracker.lone_worker_check(self.settings.lone_worker_overrun_min):
            key = f"lone:{c['job']}:{date.today().isoformat()}"
            if self.db.get_kv(key):
                continue
            self.db.set_kv(key, "alerted")
            raised += 1
            await self.notifier.notify(
                f"Safety check: {c['engineer']} is still on job {c['job']}",
                f"{c['site']} - booked to finish {c['booked_end']}, now {c['overrun_minutes']} minutes over. "
                "Might be worth a quick call to check they're OK.", level="warning", push=True, speak=True,
                importance="urgent")  # lone-worker safety: never held back
        return raised
