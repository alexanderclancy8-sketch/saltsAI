"""Runtime configuration, loaded from environment variables / .env.

Every integration is optional. Anything left unset falls back to demo data
(clearly labelled as DEMO on the display) so the assistant runs out of the box.
"""

from __future__ import annotations

import logging
import os
import time
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


def apply_timezone(name: str) -> None:
    """Make the process's own local time the business's.

    Azure App Service runs in UTC, and a lot of the code asks for "today" or "now" with a plain `date.today()` /
    `datetime.now()` (working hours, briefings, "overdue", "due this week"...). Without this they are an hour behind
    London all summer, and "today" flips an hour late. Platforms without `tzset` (Windows dev machines) already run
    on the user's own clock, so there is nothing to do there.
    """
    if not name or not hasattr(time, "tzset"):
        return
    if not Path("/usr/share/zoneinfo", name).exists():
        logging.getLogger(__name__).warning("Timezone %r isn't installed on this machine - times will follow the "
                                            "server clock (UTC). Install tzdata.", name)
        return
    os.environ["TZ"] = name
    time.tzset()

# ElevenLabs premade voices with a British accent. "daniel" is the default
# Jarvis voice: a deep, authoritative British male.
ELEVENLABS_BRITISH_VOICES = {
    "daniel": "onwK4e9ZLuTAKqWW03F9",
    "george": "JBFqnCBsd6RMkjVDRZzb",
    "alice": "Xb7hH8MSUJpSbSDYk0k2",
    "lily": "pFZP5JQG7iQjIQuC4Bku",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Identity -------------------------------------------------------
    company_name: str = "Salts Fire and Security"
    company_domain: str = "saltsfireandsecurity.co.uk"
    owner_name: str = "Alex"
    owner_salutation: str = "sir"
    owner_email: str = ""
    # Private facts Jarvis should know from day one, separated by "|" (kept in .env, never in the repo).
    # They are added to its notes the first time it starts; after that just tell Jarvis new things.
    jarvis_notes: str = ""
    partner_name: str = ""  # business partner / co-director - also receives the regulatory watch
    partner_email: str = ""
    # Microsoft 365 accounts let in through Azure's Microsoft sign-in, comma-separated (set by
    # `bash infra/deploy.sh signin ...`). Everyone listed gets full access, including approvals.
    manager_emails: str = ""
    # Management-only mail rule (jarvis/integrations/mail_guard.py). Finance/management/HR content may only be
    # emailed to management: OWNER_EMAIL, PARTNER_EMAIL (Chun) and any extra addresses here (comma-separated).
    management_emails: str = ""
    # Shared inboxes that must never receive (or be cc'd/bcc'd on) management content. "info@" means that mailbox on
    # COMPANY_DOMAIN; a full address or "@domain" also works. OOH_MAILBOX is always treated as shared.
    shared_mailboxes: str = "info@,accounts@,admin@,office@,sales@,enquiries@,support@,hello@,service@"
    timezone: str = "Europe/London"
    public_base_url: str = "http://localhost:8000"

    # --- Security -------------------------------------------------------
    # Password for the HUD. If unset, Jarvis only answers requests from localhost.
    jarvis_owner_password: str = ""
    jarvis_secret_key: str = "change-me"
    # Shared key staff use on the /report page (put it in the link you give them).
    staff_report_key: str = ""

    # --- Storage --------------------------------------------------------
    data_dir: Path = ROOT_DIR / "data"
    knowledge_dir: Path = ROOT_DIR / "knowledge"

    # --- Claude ---------------------------------------------------------
    # "max"  = run on your Claude Max/Pro subscription through the Claude Agent SDK (Claude Code), using the
    #          token from `claude setup-token` in CLAUDE_CODE_OAUTH_TOKEN. No API credits needed.
    # "api"  = pay-as-you-go Claude API with ANTHROPIC_API_KEY (prompt caching, server-side fallbacks).
    # "auto" = max if CLAUDE_CODE_OAUTH_TOKEN is set, otherwise api.
    llm_backend: str = "auto"
    claude_code_oauth_token: str = ""
    anthropic_api_key: str = ""
    jarvis_model: str = "claude-sonnet-5-5"
    voice_model: str = "claude-sonnet-5-5"  # spoken replies default to the quicker model; blank = same as JARVIS_MODEL
    voice_effort: str = "low"  # spoken conversation: quick, natural replies
    chat_effort: str = "medium"  # typed chat: thorough answers without long waits (raise to high for deep work)
    engineer_effort: str = "high"  # code fixes
    jarvis_fallbacks: bool = True
    jarvis_compaction: bool = True
    web_search_enabled: bool = True  # lets Jarvis search/fetch the web like Claude chat
    # Learn the owner's usual typed replies and offer them as ghost text in the chat box (Right Arrow accepts).
    # Only ever prefills the input - never sends or approves anything. Off = nothing learned or suggested.
    reply_suggestions_enabled: bool = True

    # --- MCP / plugin integrations (see mcp_plugins.yaml, mandates.yaml and jarvis/brain/plugins.py) ------------
    # Each is its own switch. Context7 and Superpowers are low risk (read-only docs / a written-down method) so
    # default on; Browser Use and ThoughtProof stay off until a human has checked and pinned them.
    plugins_file: Path = ROOT_DIR / "mcp_plugins.yaml"
    mandates_file: Path = ROOT_DIR / "mandates.yaml"
    plugin_context7_enabled: bool = True  # engineering agent only: current library docs, read-only
    plugin_superpowers_enabled: bool = True  # engineering agent only: plan -> test -> review method
    plugin_browser_use_enabled: bool = False  # conversational Jarvis only, read-only browsing, domain allowlist
    plugin_browser_allowed_domains: str = ""  # comma-separated; Browser Use may only visit these (and subdomains)
    plugin_thoughtproof_enabled: bool = False  # extra verification in front of approved write actions

    # --- Microsoft 365 (Graph, app-only) --------------------------------
    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""
    ms_mailbox: str = ""  # e.g. alex.clancy@saltsfireandsecurity.co.uk
    # Outlook folder (in MS_MAILBOX) that Jarvis's own emails to the owner are filed into instead of the Inbox.
    # Matched by display name, case-insensitive. Blank = leave them in the Inbox.
    owner_mail_folder: str = "salts jarvis"
    teams_webhook_url: str = ""  # Teams "Workflows" incoming webhook for updates
    # Teams chat: a proper conversational bot (Bot Framework) so the owner/partner can message Jarvis from their
    # phone in Teams, not just post one-way updates. Its own Entra app - see `deploy.sh teamsbot`.
    teams_bot_app_id: str = ""
    teams_bot_app_password: str = ""
    teams_bot_tenant_id: str = ""
    # Standing approvals: the OWNER's advance approval for two narrow classes of action (see
    # services/standing_approvals.py). Both default OFF. Changeable only from the owner-authenticated Settings
    # page (settings_store.OWNER_ONLY_KEYS) - never by the AI, a pending action or a Teams message.
    standing_record_keeping: bool = False
    standing_acknowledgements: bool = False
    standing_max_per_hour: int = 20  # most automatic runs per rolling hour, across both categories
    teams_cards_per_hour: int = 30  # most approval cards sent to one approver per rolling hour (then one summary)
    # Where engineering-agent notifications go (pull request ready, fix ready, deploy/merge results, issue triage,
    # security review): comma-separated "teams" and/or "email". Default is Teams only - no email at all.
    engineering_notify_channels: str = "teams"
    # If Teams delivery fails, also email the owner. Off by default: a failure is logged and shown on the display.
    engineering_email_fallback: bool = False
    issue_email_tag: str = "[ISSUE]"

    # Fix / pull-request notifications (PR ready, CI results, fix live, security-review findings). They always
    # appear on the display and the issues list; this picks the extra channels, comma-separated from
    # "teams" and "email" ("none" = display only). Default Teams only - email is off.
    fix_notify_channels: str = "teams"
    # Where the fix emails go IF "email" is enabled above. Blank = the owner's email.
    fix_notify_email: str = ""
    # Out-of-hours answering service: the address/domain (or a subject word) of their call-report emails
    ooh_email_from: str = ""
    ooh_mailbox: str = ""  # mailbox the reports arrive in (e.g. info@...); defaults to MS_MAILBOX
    ooh_subject_keyword: str = "out of hours"
    # The shared inbox is for OPERATIONAL items that matter only. Automated email to it is held back unless
    # the notification's importance (info < normal < important < urgent) reaches the minimum below - see
    # jarvis/services/notifier.py. Repeats of the same alert are collapsed and the inbox is rate-limited.
    shared_inbox: str = "info@saltsfireandsecurity.co.uk"
    shared_inbox_min_importance: str = "important"  # info | normal | important | urgent
    shared_inbox_dedupe_minutes: int = 240  # the same alert isn't emailed to the shared inbox again inside this
    shared_inbox_max_per_hour: int = 6  # cap on automated emails to it (urgent alerts are never capped)

    # --- Salts FSM --------------------------------------------------------
    fsm_base_url: str = ""
    fsm_api_prefix: str = "/api"
    fsm_api_key: str = ""
    fsm_api_key_header: str = "Authorization"  # "Authorization" sends "Bearer <key>"
    fsm_endpoints_file: Path = ROOT_DIR / "fsm_endpoints.yaml"
    routine_checks_file: Path = ROOT_DIR / "routine_checks.yaml"

    # --- GitHub (FSM source + auto-fix) ----------------------------------
    github_token: str = ""
    fsm_repo: str = ""  # owner/repo
    fsm_default_branch: str = "main"
    fsm_deploy_workflow: str = ""  # e.g. deploy-azure.yml (workflow_dispatch)
    fixer_mode: str = "builtin"  # builtin | claude_action | off

    # --- Self-improvement (Jarvis's own source) --------------------------
    jarvis_repo: str = ""  # owner/repo - this repository, so Jarvis can propose changes to itself
    jarvis_github_token: str = ""  # blank reuses github_token if that PAT already covers this repo too
    jarvis_default_branch: str = "main"

    # --- Azure ------------------------------------------------------------
    azure_storage_connection_string: str = ""
    azure_storage_container: str = "jarvis-reports"
    azure_deploy_mode: str = "github"  # github | kudu
    # Kudu (SCM) URL of the FSM App Service, e.g. https://<app>-<hash>.scm.uksouth-01.azurewebsites.net
    azure_fsm_scm_url: str = ""
    azure_kudu_user: str = ""
    azure_kudu_password: str = ""
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""

    # --- Finance ----------------------------------------------------------
    finance_provider: str = "auto"  # auto | sage | csv | demo
    # Sage Accounting (cloud) API app from developerselfservice.sageone.com. Connect once
    # from the display (Settings -> Connect Sage); the rotating refresh token is stored in Jarvis' DB.
    sage_client_id: str = ""
    sage_client_secret: str = ""
    sage_business_id: str = ""  # only needed if the Sage login has several businesses
    sage_write_enabled: bool = False  # allow Jarvis to create (approved) sales invoices - needs full_access scope
    sage_sales_nominal_code: str = "4000"
    sage_default_tax_rate: str = "GB_STANDARD"
    # Sage 50 (desktop) / anything else: drop CSV exports in this folder instead.
    finance_csv_dir: Path = ROOT_DIR / "finance_data"
    financial_year_end: str = "03-31"  # MM-DD
    vat_quarter_end_months: str = "3,6,9,12"
    vat_scheme: str = "invoice"  # invoice | cash
    associated_companies: int = 0
    monthly_payroll_estimate: float = 0.0
    monthly_overheads_estimate: float = 0.0
    renewal_uplift_pct: float = 5.0  # default price rise on contract renewals
    renewal_notice_days: int = 60  # prepare renewal letters this far ahead
    # Customer lifecycle emails (booked, on the way, complete, certificate, service due, quote follow-up). Drafts are
    # always queued for the owner's approval - never sent automatically. The scheduled sweep is off until enabled.
    customer_comms_enabled: bool = False
    customer_comms_cron: str = "*/30 7-18 * * 1-5"
    customer_comms_service_notice_days: int = 30  # draft a "service due" email this far ahead
    customer_comms_quote_followup_days: int = 5  # follow up a sent quote after this many days
    lone_worker_overrun_min: int = 90  # safety check when a job runs this long past its booked end
    boe_base_rate: float = 4.0  # Bank of England base rate % - keep current for late-payment interest
    # Targets for the business health check (tune to your own plan)
    target_gross_margin_pct: float = 45.0
    target_debtor_days: float = 45.0
    target_overdue_pct: float = 20.0
    target_cash_runway_months: float = 3.0
    target_recurring_revenue_pct: float = 30.0
    target_quote_conversion_pct: float = 35.0
    target_utilisation_pct: float = 70.0
    target_revenue_growth_pct: float = 0.0

    # --- RAM Tracking (vehicle trackers) ----------------------------------------
    # RAM's External API is OAuth2 (see jarvis/integrations/ramtracking.py's docstring) - client ID
    # and secret authenticate the token request, not the data requests themselves.
    ram_auth_url: str = "https://auth.qaifn.co.uk/oauth/token"
    ram_api_base_url: str = "https://api.qaifn.co.uk"
    ram_client_id: str = ""
    ram_api_key: str = ""  # this is RAM's "Client Secret" - kept as ram_api_key so existing saved settings still apply
    ram_username: str = ""
    ram_password: str = ""
    timesheet_tolerance_min: int = 30

    # --- Marketing: socials, Google reviews, search ranking -------------------
    website_url: str = "https://www.saltsfireandsecurity.co.uk"
    seo_target_keywords: str = ("fire alarm installation Bradford, fire alarm servicing Leeds, fire alarm company "
                                "West Yorkshire, emergency lighting testing Bradford, intruder alarm installers Shipley, "
                                "CCTV installation Bradford, access control Leeds, BAFE fire alarm company Yorkshire")
    meta_graph_version: str = "v23.0"
    meta_page_id: str = ""
    meta_page_token: str = ""  # long-lived Page access token (Facebook + Instagram)
    instagram_business_id: str = ""
    linkedin_org_id: str = ""
    linkedin_access_token: str = ""
    linkedin_version: str = "202506"
    tiktok_access_token: str = ""
    google_review_url: str = ""  # your Google Business Profile "ask for reviews" link
    google_places_api_key: str = ""
    google_place_id: str = ""
    google_service_account_file: str = ""  # JSON key of a service account added to Search Console
    search_console_site: str = "sc-domain:saltsfireandsecurity.co.uk"
    pagespeed_api_key: str = ""
    social_snapshot_cron: str = "20 6 * * *"
    marketing_report_cron: str = "50 7 * * 1"

    # --- Voice --------------------------------------------------------------
    tts_provider: str = "auto"  # auto | elevenlabs | azure | piper | browser
    piper_voice: str = "alan"  # free, local TTS (jarvis/integrations/voice.py's PIPER_VOICES) - the default
    # whenever no paid ElevenLabs/Azure key is set, since it beats the browser's own robotic voice for free
    elevenlabs_api_key: str = ""
    elevenlabs_voice: str = "daniel"  # preset name from ELEVENLABS_BRITISH_VOICES or a raw voice id
    elevenlabs_model: str = "eleven_multilingual_v2"  # most natural; eleven_flash_v2_5 answers a little faster
    # Lower stability reads as more natural, varied speech - JARVIS-like warmth rather than a flat, robotic
    # monotone; too low starts to wander/mumble. 0.35 is ElevenLabs' own commonly recommended conversational
    # sweet spot. Style stays low: pushed up it exaggerates delivery and introduces artifacts.
    elevenlabs_stability: float = 0.35
    elevenlabs_similarity: float = 0.8
    elevenlabs_style: float = 0.15
    elevenlabs_speed: float = 1.0
    azure_speech_key: str = ""
    azure_speech_region: str = "uksouth"
    azure_tts_voice: str = "en-GB-RyanNeural"
    azure_tts_style: str = "chat"  # relaxed, conversational delivery; voices without it just ignore it

    stt_provider: str = "auto"  # auto | deepgram | whisper | browser
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"
    stt_language: str = "en-GB"
    openai_api_key: str = ""  # only for Whisper speech-to-text
    whisper_model: str = "whisper-1"
    wake_word: str = "jarvis"
    # Voice mode only: if a spoken question hasn't started being answered after ~1.8s, say ONE short
    # acknowledgment ("Let me check the accounts."). Turn-scoped and off for typed turns - see hud.js's `filler`.
    voice_ack_fillers: bool = True
    # Push-to-talk / browser-mic only: how long (ms) after the last final speech result counts as the end of the
    # turn. hud.js adds a little extra after trailing fillers ("and", "so", "um") and clamps this to 600-5000.
    voice_silence_ms: int = 1200

    # --- Schedules ----------------------------------------------------------
    briefing_cron: str = "45 7 * * 1-5"
    routine_test_interval_min: int = 15
    compliance_check_cron: str = "0 7 * * 1-5"
    staff_review_cron: str = "30 16 * * 5"  # weekly team performance review (Friday 16:30)
    business_review_cron: str = "45 7 1 * *"  # monthly business health report
    regulatory_watch_cron: str = "40 7 * * 1"  # weekly tax / employment law / fire regulation watch
    technical_watch_cron: str = "30 6 * * 2"  # weekly fire & security technical/standards deep-dive
    security_watch_cron: str = "0 6 * * 1"  # weekly review of the Salts FSM codebase for vulnerabilities
    suggestions_cron: str = "5 9,13,16 * * 1-5"  # proactive suggestion sweeps
    lone_worker_check_min: int = 30  # how often to look for jobs running dangerously long
    wrapup_cron: str = "0 17 * * 1-5"  # end-of-day wrap-up at 5pm (after the billing and review checks)
    billing_check_cron: str = "45 16 * * 1-5"  # unbilled completed jobs -> draft invoices for approval
    review_requests_cron: str = "50 16 * * 1-5"  # thank-you + Google review requests for the day's jobs
    self_learning_cron: str = "0 21 * * *"  # nightly reflection: remember anything durable from the day's chats
    inbox_check_interval_min: int = 10
    # Weekly digest of Jarvis' own routine engineering notices (PRs, fixes, deploys, test results, triage,
    # non-critical security findings, self-learning). Default Monday 08:00 (in TIMEZONE). Urgent/safety items
    # are never held back - see services/digest.py for the map. NOTIFICATION_ROUTES overrides single entries,
    # e.g. "ci_passed=immediate,issue_triaged=digest"; anything unknown is sent immediately.
    weekly_digest_enabled: bool = True  # False = every notice is sent straight away, as before
    weekly_digest_cron: str = "0 8 * * 1"
    weekly_digest_all_clear: bool = False  # True = send a one-line "all clear" when there is nothing to report
    notification_routes: str = ""
    scheduler_enabled: bool = True

    # --- Proactive chat (services/proactive.py) -----------------------------
    # Jarvis posting into the open chat on its own (background-task follow-ups, automations, the pull request
    # watch). Off until the owner switches it on. Quiet hours are HH:MM in TIMEZONE and may run past midnight.
    proactive_chat_enabled: bool = False
    proactive_quiet_start: str = "21:00"
    proactive_quiet_end: str = "07:30"
    proactive_max_per_hour: int = 6  # at most this many proactive messages an hour; 0 = no limit
    proactive_pr_watch_min: int = 15  # how often the pull request watch looks, in minutes

    # --- Derived ------------------------------------------------------------
    @property
    def managers(self) -> set[str]:
        return {e.strip().lower() for e in self.manager_emails.split(",") if e.strip()}

    @property
    def management_address_entries(self) -> set[str]:
        """Owner + business partner + MANAGEMENT_EMAILS, lower-cased (mail_guard removes any shared inbox)."""
        raw = [self.owner_email, self.partner_email, *self.management_emails.split(",")]
        return {e.strip().lower() for e in raw if e and e.strip()}

    @property
    def shared_mailbox_entries(self) -> set[str]:
        raw = [*self.shared_mailboxes.split(","), self.ooh_mailbox]
        return {e.strip().lower() for e in raw if e and e.strip()}

    @property
    def fix_channels(self) -> tuple[str, ...]:
        """Extra channels (besides the display) for fix / PR notifications - a subset of teams and email."""
        wanted = {c.strip().lower() for c in self.fix_notify_channels.replace(";", ",").replace("+", ",").split(",")}
        return tuple(c for c in ("teams", "email") if c in wanted)

    def person(self, email: str) -> str:
        """Friendly name for a signed-in manager."""
        e = email.lower()
        if self.owner_email and e == self.owner_email.lower():
            return self.owner_name
        if self.partner_email and e == self.partner_email.lower():
            return self.partner_name or e
        return e

    @property
    def staff_roles_file(self) -> Path:
        return self.data_dir / "staff_roles.yaml"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jarvis.db"

    def model_for(self, mode: str) -> str:
        """The Claude model for a spoken ("voice") or typed turn."""
        return (self.voice_model or self.jarvis_model) if mode == "voice" else self.jarvis_model

    @property
    def effective_llm_backend(self) -> str:
        if self.llm_backend in ("max", "api"):
            return self.llm_backend
        return "max" if self.claude_code_oauth_token else "api"

    @property
    def graph_configured(self) -> bool:
        return bool(self.ms_tenant_id and self.ms_client_id and self.ms_client_secret and self.ms_mailbox)

    @property
    def teams_bot_configured(self) -> bool:
        return bool(self.teams_bot_app_id and self.teams_bot_app_password and self.teams_bot_tenant_id)

    @property
    def fsm_configured(self) -> bool:
        return bool(self.fsm_base_url)

    @property
    def github_configured(self) -> bool:
        return bool(self.github_token and self.fsm_repo)

    @property
    def jarvis_self_improve_configured(self) -> bool:
        return bool((self.jarvis_github_token or self.github_token) and self.jarvis_repo)

    @property
    def elevenlabs_voice_id(self) -> str:
        return ELEVENLABS_BRITISH_VOICES.get(self.elevenlabs_voice.lower(), self.elevenlabs_voice)

    @property
    def effective_tts(self) -> str:
        if self.tts_provider != "auto":
            return self.tts_provider
        if self.elevenlabs_api_key:
            return "elevenlabs"
        if self.azure_speech_key:
            return "azure"
        return "piper"  # free and local - better than the browser's robotic voice, and needs no key

    @property
    def effective_stt(self) -> str:
        if self.stt_provider != "auto":
            return self.stt_provider
        if self.deepgram_api_key:
            return "deepgram"
        if self.openai_api_key:
            return "whisper"
        return "browser"

    @property
    def effective_finance(self) -> str:
        if self.finance_provider != "auto":
            return self.finance_provider
        if self.sage_client_id and self.sage_client_secret:
            return "sage"
        if (self.finance_csv_dir / "invoices.csv").exists():
            return "csv"
        return "demo"

    vat_quarter_months: list[int] = Field(default_factory=list, exclude=True)

    def model_post_init(self, __context) -> None:  # noqa: D401
        self.vat_quarter_months = sorted(int(m) for m in self.vat_quarter_end_months.split(",") if m.strip())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # On App Service, fall back to the app's own address (needed for the Sage sign-in callback).
        host = os.environ.get("WEBSITE_HOSTNAME", "")
        if self.public_base_url == "http://localhost:8000" and host and "localhost" not in host:
            self.public_base_url = f"https://{host}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
