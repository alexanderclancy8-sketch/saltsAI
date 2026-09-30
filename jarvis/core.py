"""Wires every integration and service together into one Jarvis instance."""

from __future__ import annotations

import asyncio
import logging
from datetime import date

import httpx

from .brain import llm, plugins
from .brain.agent import JarvisBrain
from .config import Settings
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
from .integrations.ramtracking import DemoRamTracking, RamTracking
from .integrations.voice import Voice
from .knowledge import KnowledgeBase
from .services.accountant import Accountant
from .services.accreditations import Accreditations
from .services.actions import ActionExecutor
from .services.advisor import Advisor
from .services.automations import AutomationService
from .services.billing import Billing
from .services.customers import CustomerHealth
from .services.documents import Documents
from .services.meetings import Meetings
from .services.ooh import OutOfHours
from .services.briefing import Briefings
from .services.fixer import Fixer
from .services.issues import IssueService
from .services.marketing import MarketingTracker
from .services.notifier import Notifier
from .services.performance import PerformanceReviewer, StaffRegister
from .services.ppm_planner import PPMPlanner
from .services.recruiter import Recruiter
from .services.regulatory import RegulatoryWatch
from .services.renewals import Renewals
from .services.reply_suggestions import ReplySuggestions
from .services.routine_tests import RoutineTester
from .services.security_watch import SecurityWatch
from .services.conversation_quality import ConversationQuality
from .services.self_improve import SelfImprove
from .services.self_learning import SelfLearning
from .services.site_access import SiteAccessCodes
from .services.staff import StaffMonitor
from .services.stores import Stores
from .services.suggestions import Suggestions
from .services.verification import ActionVerifier
from .services.wrapup import WrapUp
from .services.tracking import Tracker

log = logging.getLogger(__name__)


class Jarvis:
    def __init__(self, settings: Settings, db: Database | None = None, http: httpx.AsyncClient | None = None,
                 client=None):
        s = self.settings = settings
        self.db = db or Database(settings.db_path)
        self.bus = EventBus()
        self.http = http or httpx.AsyncClient(timeout=30, headers={"User-Agent": "salts-jarvis/1.0"})
        self.client = client or llm.make_client(settings)
        self.kb = KnowledgeBase(settings.knowledge_dir)

        # integrations (demo stand-ins where not configured)
        self.mail = GraphMail(s, self.http) if s.graph_configured else DemoMail()
        self.teams = TeamsNotifier(s.teams_webhook_url, self.http)
        self.teamsbot = TeamsBot(s, self.http)
        self.fsm = FSMRouter(s, self.http)
        self.ram = RamTracking(s, self.http) if s.ram_api_base_url and s.ram_api_key else DemoRamTracking(self.fsm)
        self.finance = build_finance(s, self.http, self.db)
        self.github = GitHub(s.github_token, s.fsm_repo, self.http, s.fsm_default_branch) if s.github_configured else None
        self.self_github = (GitHub(s.jarvis_github_token or s.github_token, s.jarvis_repo, self.http,
                                   s.jarvis_default_branch) if s.jarvis_self_improve_configured else None)
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
        self.actions = ActionExecutor(self.db, self.bus, self.notifier, self.mail, self.fixer, self.fsm)
        self.billing = Billing(s, self.db, self.fsm, self.finance, self.actions, self.notifier)
        self.actions.billing = self.billing
        self.actions.j = self
        self.verifier = ActionVerifier(s)  # optional ThoughtProof check on approved actions (off by default)
        self.issues.actions = self.actions
        self.briefings = Briefings(s, self.db, self.mail, self.staff, self.accountant, self.notifier, self.client)
        self.marketing = MarketingTracker(s, self.db, self.http, self.presence, self.notifier, self.client)
        self.advisor = Advisor(s, self.db, self.accountant, self.reviewer, self.staff, self.marketing, self.notifier,
                               self.client, self.bus)
        self.accreditations = Accreditations(s, self.db, self.staff, self.fsm, self.notifier, self.client, self.bus)
        self.stores = Stores(self.db, demo_seed=self.fsm.demo, fsm=self.fsm)
        self.regwatch = RegulatoryWatch(s, self.db, self.notifier, self.client, self.bus, self.mail)
        self.regwatch.actions = self.actions
        self.tracker = Tracker(self.fsm, self.http, self.ram, self.register, s.timesheet_tolerance_min)
        self.ppm = PPMPlanner(self.fsm, self.register)  # read-only advisory scheduling plan
        self.customers = CustomerHealth(self)
        self.advisor.j_customers = self.customers
        self.renewals = Renewals(self)
        self.meetings = Meetings(self)
        self.ooh = OutOfHours(self)
        self.briefings.ooh = self.ooh
        self.documents = Documents(self)
        self.suggestions = Suggestions(self)
        self.wrapup = WrapUp(self)
        self.scheduler = None
        self.automations = AutomationService(self)
        self.quality = ConversationQuality(self)  # per-turn metrics + feedback; must exist before the brain below
        self.self_learning = SelfLearning(self)
        self.reply_suggestions = ReplySuggestions(self)
        self.site_access = SiteAccessCodes(self)
        self.recruiter = Recruiter(self)
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
        return out

    def connections(self) -> dict[str, str]:
        s = self.settings
        presence = self.presence.configured()
        return {
            "Email (Outlook)": "connected" if not self.mail.demo else "DEMO data - connect Microsoft 365",
            "Teams updates": "connected" if self.teams.enabled else "not set up",
            "Teams chat": "connected" if self.teamsbot.configured else "not set up",
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
            "Self-learning": f"reflects on recent conversations {cron_to_english(s.self_learning_cron)}",
            "Azure deploy": s.azure_deploy_mode if self.github or self.kudu.enabled else "not set up",
            "Azure archive": "connected" if self.blob.enabled else "not set up",
            "Voice": f"TTS {s.effective_tts}, STT {s.effective_stt}",
            "Socials / Google": ", ".join(k for k, v in presence.items() if v) or "DEMO data - not connected",
            "Stores / stock": self.stores.source + (" (DEMO stock)" if self.stores.demo else ""),
            "Vehicle tracking": ("RAM Tracking" if not self.ram.demo else
                                 "DEMO journeys - set RAM_API_BASE_URL / RAM_API_KEY"),
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
                                       push=True, speak=True)

    async def daily_reviews(self) -> None:
        result = await self.billing.queue_review_requests()
        if result.get("queued"):
            await self.notifier.notify(f"{result['queued']} review requests ready", "Approve them on the display.")

    async def lone_worker_sweep(self) -> None:
        for c in await self.tracker.lone_worker_check(self.settings.lone_worker_overrun_min):
            key = f"lone:{c['job']}:{date.today().isoformat()}"
            if self.db.get_kv(key):
                continue
            self.db.set_kv(key, "alerted")
            await self.notifier.notify(
                f"Safety check: {c['engineer']} is still on job {c['job']}",
                f"{c['site']} - booked to finish {c['booked_end']}, now {c['overrun_minutes']} minutes over. "
                "Might be worth a quick call to check they're OK.", level="warning", push=True, speak=True)
