"""Connections and preferences edited on the display's Settings page.

What the owner saves there is kept encrypted in DATA_DIR/connections.enc (the key is derived from
JARVIS_SECRET_KEY) and applied over the App Service / .env settings whenever Jarvis starts or reloads.
Secrets are never sent back to the browser - only whether they are set and their last four characters.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .config import Settings
from .crypto import fernet

log = logging.getLogger(__name__)

OPUS = ("claude-opus-5-5", "Claude Opus 5.5 - most capable")
SONNET = ("claude-sonnet-5-5", "Claude Sonnet 5.5 - quicker")
EFFORT = (("low", "Quick"), ("medium", "Balanced"), ("high", "Thorough"))
# The engineering agents also accept the two highest levels the Agent SDK and API support (see config.ENGINEER_EFFORT_LEVELS).
ENGINEER_EFFORT = EFFORT + (("xhigh", "Extra thorough (some models only)"), ("max", "Maximum"))
# British English Azure neural voices offered under Connections > Voice. The Multilingual ones are Azure's newer,
# higher-quality generation; Ryan is first because it also supports the conversational style (jarvis/integrations/
# ssml.py). Add a name here only once it is confirmed in Azure's en-GB voice list - a wrong name makes every spoken
# reply fail over to the browser voice.
AZURE_VOICES = (
    ("en-GB-RyanNeural", "Ryan (male)"),
    ("en-GB-OllieMultilingualNeural", "Ollie - newer, high quality (male)"),
    ("en-GB-ThomasNeural", "Thomas (male)"),
    ("en-GB-OliverNeural", "Oliver (male)"),
    ("en-GB-AlfieNeural", "Alfie (male)"),
    ("en-GB-ElliotNeural", "Elliot (male)"),
    ("en-GB-EthanNeural", "Ethan (male)"),
    ("en-GB-NoahNeural", "Noah (male)"),
    ("en-GB-SoniaNeural", "Sonia (female)"),
    ("en-GB-AdaMultilingualNeural", "Ada - newer, high quality (female)"),
    ("en-GB-LibbyNeural", "Libby (female)"),
    ("en-GB-OliviaNeural", "Olivia (female)"),
)


@dataclass(frozen=True)
class Field:
    key: str  # attribute on Settings
    label: str
    kind: str = "text"  # text | secret | email | url | number | select | bool | textarea | notes | cron
    help: str = ""
    placeholder: str = ""
    options: tuple[tuple[str, str], ...] = ()
    advanced: bool = False
    # (other_field_key, value) - only shown once that field's current value equals this. Lets a section with
    # several providers (Voice: ElevenLabs/Azure/Piper) show only the one actually selected, not all of them
    # stacked up at once - see hud.js's Settings.renderField().
    depends_on: tuple[str, str] | None = None


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    blurb: str
    fields: tuple[Field, ...]
    required: tuple[str, ...] = ()
    test: bool = False
    guide: tuple[str, ...] = ()  # numbered steps; may use {base_url}, {app_name}
    links: tuple[tuple[str, str], ...] = field(default=())


SECTIONS: tuple[Section, ...] = (
    Section(
        "profile", "You and the business", "Who Jarvis works for, and what it should know from day one.",
        (
            Field("owner_name", "Your first name", placeholder="Alex"),
            Field("owner_salutation", "What Jarvis calls you", placeholder="sir, boss, or your first name"),
            Field("talk_style", "How Jarvis talks", "select",
                  "Natural uses your first name and plain, friendly wording. Formal uses \"What Jarvis calls you\" "
                  "(above) and a more formal tone.",
                  options=(("natural", "Natural - uses your first name"),
                           ("formal", "Formal - uses what Jarvis calls you"))),
            Field("owner_email", "Your email", "email", "Where your updates, briefings and approvals go."),
            Field("partner_name", "Business partner's name", placeholder="First name"),
            Field("partner_email", "Business partner's email", "email",
                  "Gets the weekly tax and employment-law watch too."),
            Field("company_name", "Company name"),
            Field("reply_suggestions_enabled", "Suggest my usual replies in the chat box", "bool",
                  "Jarvis learns the short replies you type often and shows the likeliest as grey text; press the "
                  "right arrow to accept it, Enter to send. Learned on this server only. Off stops learning and suggesting."),
            Field("jarvis_notes", "Things Jarvis should know", "notes",
                  "One fact per line, e.g. \"First County Monitoring handle our out-of-hours.\" Added to its memory."),
        ),
        required=("owner_email",),
    ),
    Section(
        "claude", "Claude", "The AI behind Jarvis. Your Claude Max plan, or an Anthropic API key.",
        (
            Field("claude_code_oauth_token", "Claude Max token", "secret",
                  "From `claude setup-token` (starts sk-ant-oat01-). Uses your Max plan instead of API credits."),
            Field("anthropic_api_key", "Anthropic API key", "secret",
                  "Only if you'd rather pay per use than use the Max plan. Leave blank otherwise.", advanced=True),
            Field("jarvis_model", "Model", "select", "Used for typed chat, reports and advice.", options=(OPUS, SONNET)),
            Field("voice_model", "Model for spoken replies", "select", "A quicker model makes conversation snappier.",
                  options=(("", "Same as above"), OPUS, SONNET)),
            Field("voice_effort", "Thinking for spoken replies", "select", "Quick is best for conversation.",
                  options=EFFORT),
            Field("chat_effort", "Thinking for typed chat", "select", "Thorough takes longer but digs deeper.",
                  options=EFFORT),
            Field("engineer_model", "Model for the engineering agents", "text",
                  "Used by self-improvement, auto-fix and the security review. Paste the exact model ID. "
                  "Blank = same as the model above. Only you can change this.",
                  placeholder="blank = same as above", advanced=True),
            Field("engineer_effort", "Thinking for the engineering agents", "select",
                  "How hard they think when writing or reviewing code. Only you can change this.",
                  options=ENGINEER_EFFORT, advanced=True),
            Field("web_search_enabled", "Let Jarvis search the web", "bool"),
        ),
        test=True,
    ),
    Section(
        "microsoft365", "Microsoft 365", "Reads and sends email, checks calendars and Teams meeting transcripts.",
        (
            Field("ms_tenant_id", "Directory (tenant) ID", placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"),
            Field("ms_client_id", "Application (client) ID", placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"),
            Field("ms_client_secret", "Client secret", "secret"),
            Field("ms_mailbox", "Your mailbox", "email", "The mailbox Jarvis reads and sends from."),
            Field("owner_mail_folder", "Folder for Jarvis's emails to you",
                  help="Outlook folder name (in your mailbox) Jarvis's own emails to you are filed into instead "
                       "of the Inbox. Blank = keep them in the Inbox. Falls back to the Inbox if not found."),
            Field("ooh_mailbox", "Out-of-hours reports mailbox", "email",
                  "Where the answering service's reports arrive, e.g. info@. Blank = your mailbox."),
            Field("ooh_email_from", "Out-of-hours reports come from",
                  help="Their email address or domain, e.g. firstcountymonitoring.co.uk"),
            Field("issue_email_tag", "Issue email tag", help="Staff emails with this in the subject become issues.",
                  advanced=True),
        ),
        required=("ms_tenant_id", "ms_client_id", "ms_client_secret", "ms_mailbox"),
        test=True,
        guide=(
            "Quickest: in Azure Cloud Shell run  APP_NAME={app_name} bash infra/deploy.sh m365  - it registers Jarvis "
            "with Microsoft 365, grants the permissions and fills these boxes in for you.",
            "Or by hand: portal.azure.com > Microsoft Entra ID > App registrations > New registration, name it "
            "Jarvis. Copy the Directory (tenant) ID and Application (client) ID here.",
            "API permissions > Add > Microsoft Graph > Application permissions: Mail.ReadWrite, Mail.Send, "
            "Calendars.Read, OnlineMeetingTranscript.Read.All, Reports.Read.All. Then Grant admin consent.",
            "Certificates & secrets > New client secret. Copy its Value here.",
        ),
    ),
    Section(
        "sharedinbox", "Shared inbox (info@)", "Keeps the shared inbox for important operational items only. "
        "Everything else goes to Teams and the display.",
        (
            Field("shared_inbox", "Shared inbox address", "email",
                  "Automated emails to this address are filtered by importance. Finance and management items "
                  "never go here."),
            Field("shared_inbox_min_importance", "Lowest importance emailed to it", "select",
                  "Anything below this goes to Teams and the display instead. Urgent (life-safety) alerts are "
                  "never rate-limited.",
                  options=(("info", "Info (everything)"), ("normal", "Normal"),
                           ("important", "Important (recommended)"), ("urgent", "Urgent only"))),
            Field("shared_inbox_dedupe_minutes", "Don't repeat the same alert within (minutes)", "number",
                  advanced=True),
            Field("shared_inbox_max_per_hour", "Most emails to it per hour", "number", advanced=True),
        ),
    ),
    Section(
        "teams", "Teams updates", "Jarvis posts your updates and alerts to a Teams channel.",
        (
            Field("teams_webhook_url", "Channel webhook URL", "secret"),
            Field("engineering_notify_channels", "Fix and pull request notifications", "select",
                  "Where fix-ready, pull request, deploy, triage and security-review updates go.",
                  options=(("teams", "Teams only"), ("teams,email", "Teams and email")), advanced=True),
            Field("engineering_email_fallback", "Email those if Teams fails", "bool",
                  "Off: a failed Teams post is shown on the display, not emailed.", advanced=True),
            Field("fix_notify_channels", "Fix and pull request alerts", "select",
                  "Fixes, pull requests and security findings always show on the display and issues list. "
                  "This picks what else gets told.",
                  options=(("teams", "Teams only"), ("teams,email", "Teams and email"),
                            ("none", "Display only"))),
            Field("fix_notify_email", "Email for fix alerts", "email",
                  "Only used if email is switched on above. Blank = your email.", advanced=True),
        ),
        required=("teams_webhook_url",),
        test=True,
        guide=(
            "In Teams, open the channel, click ... > Workflows > \"Post to a channel when a webhook request is "
            "received\".",
            "Finish the steps and copy the URL it gives you here. Test sends a short message to that channel.",
        ),
    ),
    Section(
        "teamsbot", "Teams chat", "Message Jarvis from your phone in Teams, and get a real reply back - not just "
        "one-way updates.",
        (
            Field("teams_bot_app_id", "Bot app ID"),
            Field("teams_bot_app_password", "Bot app secret", "secret"),
            Field("teams_bot_tenant_id", "Directory (tenant) ID"),
        ),
        required=("teams_bot_app_id", "teams_bot_app_password", "teams_bot_tenant_id"),
        test=True,
        guide=(
            "Quickest: in Azure Cloud Shell run  APP_NAME={app_name} bash infra/deploy.sh teamsbot  - it "
            "registers Jarvis as a Bot Framework bot, turns on the Teams channel, fills these boxes in, and "
            "builds an app package for you to add to Teams.",
            "Add the app in Teams (Apps > Manage your apps > Upload a custom app, then open the Jarvis chat) and "
            "say hello once. That's how Jarvis learns where to send you approvals: when it queues something it "
            "messages you an Approve / Deny card, so you can decide from your phone. You can also reply "
            "\"approve 12\" or \"deny 12\".",
            "Only you, the business partner and your managers can use it or approve - anyone else who messages "
            "the bot is ignored.",
        ),
    ),
    Section(
        "fsm", "Salts FSM", "Jobs, engineers, sites, systems, contracts, quotes, stock and timesheets.",
        (
            Field("fsm_base_url", "FSM web address", "url", placeholder="https://salts-fsm.azurewebsites.net"),
            Field("fsm_api_key", "API key", "secret"),
            Field("fsm_api_prefix", "API path prefix", placeholder="/api", advanced=True),
            Field("fsm_api_key_header", "API key header", help="\"Authorization\" sends \"Bearer <key>\".",
                  advanced=True),
        ),
        required=("fsm_base_url",),
        test=True,
    ),
    Section(
        "sage", "Sage Accounting", "Invoices, bank balances, debtors and profit and loss.",
        (
            Field("sage_client_id", "Client ID"),
            Field("sage_client_secret", "Client secret", "secret"),
            Field("sage_write_enabled", "Let Jarvis create invoices you approve", "bool",
                  "Reconnect Sage after turning this on."),
            Field("sage_business_id", "Business ID", help="Only if your Sage login has several businesses.",
                  advanced=True),
            Field("sage_sales_nominal_code", "Sales nominal code", advanced=True),
        ),
        required=("sage_client_id", "sage_client_secret"),
        test=True,
        guide=(
            "Create an app at developerselfservice.sageone.com with this callback URL: {base_url}/auth/sage/callback",
            "Paste its Client ID and Client secret here and save.",
            "Click Connect Sage and sign in to Sage. Jarvis keeps the connection fresh after that.",
        ),
    ),
    Section(
        "ram", "RAM Tracking", "Van locations, set-off and home times, journeys and timesheet checks.",
        (
            Field("ram_client_id", "Client ID", help="Shown on RAM's API Keys page. It can be a name, e.g. the account holder's."),
            Field("ram_api_key", "Client secret", "secret"),
            Field("ram_username", "API username",
                  help="The RAM username of the dedicated API user, exactly as RAM issued it."),
            Field("ram_password", "API password", "secret"),
            Field("ram_api_base_url", "API address", "url",
                  help="Leave as https://api.qaifn.co.uk. Only the address: anything after the host is ignored.",
                  advanced=True),
            Field("timesheet_tolerance_min", "Timesheet tolerance (minutes)", "number", advanced=True),
            Field("van_locations_out_of_hours", "Show van locations outside working hours", "select",
                  "Off (default): vans are hidden outside Mon-Fri 07:00-18:30. On-call only: just the engineers on "
                  "the on-call roster (set it by asking Jarvis) are shown. Always: every van, any hour. Only the "
                  "owner can change this. Every out-of-hours look-up is logged (who asked, when, which engineer). "
                  "Make sure engineers' contracts and tracking policy cover this.",
                  options=(("off", "Off"), ("on_call", "On-call only"), ("always", "Always"))),
        ),
        required=("ram_client_id", "ram_api_key", "ram_username", "ram_password"),
        test=True,
        guide=(
            "In the RAM Tracking portal: profile - integrations - API Keys shows the Client ID and Client secret.",
            "That page also says you need a dedicated account's username and password for the API - "
            "RAM recommend a separate login just for this, not your own.",
            "Give the API user no two-step verification (MFA): RAM's API sign-in is a plain username and password, so an "
            "account that asks for a code can't be used.",
        ),
    ),
    Section(
        "voice", "Voice", "How Jarvis sounds, and how it hears you.",
        (
            Field("tts_provider", "Voice", "select", options=(
                ("auto", "Best available"), ("elevenlabs", "ElevenLabs"), ("azure", "Azure"),
                ("piper", "Piper (free, local)"), ("browser", "Browser"))),
            Field("elevenlabs_api_key", "ElevenLabs API key", "secret",
                  "The most natural voice. elevenlabs.io > profile > API Keys.",
                  depends_on=("tts_provider", "elevenlabs")),
            Field("elevenlabs_voice", "ElevenLabs voice", "select", options=(
                ("daniel", "Daniel - deep British male"), ("george", "George - warm British male"),
                ("alice", "Alice - British female"), ("lily", "Lily - British female")),
                  depends_on=("tts_provider", "elevenlabs")),
            Field("elevenlabs_model", "ElevenLabs model", "select", options=(
                ("eleven_multilingual_v2", "Most natural"), ("eleven_flash_v2_5", "Fastest")), advanced=True,
                  depends_on=("tts_provider", "elevenlabs")),
            Field("elevenlabs_stability", "Voice stability (0-1)", "number",
                  "Lower sounds more natural and varied; higher sounds flatter and more consistent. "
                  "0.3-0.4 usually sounds least robotic.", advanced=True, depends_on=("tts_provider", "elevenlabs")),
            Field("elevenlabs_style", "Voice style exaggeration (0-1)", "number",
                  "Higher leans into the voice's character more, but can introduce odd artifacts if pushed "
                  "too far. Keep this low.", advanced=True, depends_on=("tts_provider", "elevenlabs")),
            Field("elevenlabs_speed", "Voice speed", "number", "1.0 is normal pace.", advanced=True,
                  depends_on=("tts_provider", "elevenlabs")),
            Field("azure_speech_key", "Azure Speech key", "secret",
                  "Free and very good. Cloud Shell: bash infra/deploy.sh voice sets this up.",
                  depends_on=("tts_provider", "azure")),
            Field("azure_speech_region", "Azure Speech region", placeholder="uksouth", advanced=True,
                  depends_on=("tts_provider", "azure")),
            Field("azure_tts_voice", "Azure voice", "select", options=AZURE_VOICES,
                  help="Press Play sample to hear the voice you've picked (needs the Azure Speech key saved).",
                  depends_on=("tts_provider", "azure")),
            Field("azure_tts_style", "Azure speaking style", "select",
                  options=(("chat", "Conversational"), ("", "Standard")), advanced=True,
                  depends_on=("tts_provider", "azure")),
            Field("piper_voice", "Piper voice (free)", "select", options=(
                ("alan", "Alan - British male (RP)"), ("northern_english_male", "Northern English male"),
                ("jenny_dioco", "Jenny - British female"), ("alba", "Alba - British female")),
                  help="No API key needed - downloaded once and run locally. This is the voice used whenever "
                       "no ElevenLabs or Azure key is set.", depends_on=("tts_provider", "piper")),
            Field("stt_provider", "Listening", "select", options=(
                ("auto", "Best available"), ("deepgram", "Deepgram"), ("whisper", "OpenAI Whisper"),
                ("browser", "Browser"))),
            Field("deepgram_api_key", "Deepgram API key", "secret", "For always-listening mode. deepgram.com",
                  depends_on=("stt_provider", "deepgram")),
            Field("openai_api_key", "OpenAI API key (Whisper)", "secret", advanced=True,
                  depends_on=("stt_provider", "whisper")),
            Field("wake_word", "Wake word", placeholder="jarvis"),
            Field("voice_ack_fillers", "Say a short acknowledgment while thinking", "bool",
                  "When you ask something out loud and the answer takes a couple of seconds, Jarvis says one "
                  "short line like \"Let me check the accounts.\" Never for typed questions.", advanced=True),
            Field("voice_silence_ms", "Pause before Jarvis takes your turn as finished (ms)", "number",
                  "How long a silence counts as the end of what you're saying when using push-to-talk. 1200 is "
                  "normal; Jarvis waits a little longer if you trail off on \"and\", \"so\" or \"um\".", advanced=True),
        ),
        test=True,
    ),
    Section(
        "marketing", "Google and socials", "Reviews, followers, search rankings and review requests.",
        (
            Field("website_url", "Website", "url"),
            Field("google_review_url", "Google review link", "url",
                  "From your Google Business Profile > Ask for reviews."),
            Field("google_places_api_key", "Google Places API key", "secret"),
            Field("google_place_id", "Google Place ID"),
            Field("meta_page_id", "Facebook Page ID"),
            Field("meta_page_token", "Facebook Page access token", "secret", "Also covers Instagram."),
            Field("instagram_business_id", "Instagram business account ID"),
            Field("linkedin_org_id", "LinkedIn organisation ID"),
            Field("linkedin_access_token", "LinkedIn access token", "secret"),
            Field("tiktok_access_token", "TikTok access token", "secret", advanced=True),
            Field("search_console_site", "Search Console property", advanced=True),
            Field("pagespeed_api_key", "PageSpeed API key", "secret", advanced=True),
            Field("seo_target_keywords", "Search terms to track", "textarea", "Comma-separated.", advanced=True),
        ),
        test=True,
    ),
    Section(
        "images", "Image generation", "Draft social media graphics (Facebook, Instagram, LinkedIn, TikTok) with "
        "your headline, navy branding and logo. Drafts only - Jarvis never posts them anywhere.",
        (
            Field("image_provider", "Image provider", "select", "Which service paints the background.",
                  options=(("openai", "OpenAI"),)),
            Field("image_api_key", "Image provider API key", "secret",
                  "Without this Jarvis says image generation isn't connected and makes nothing."),
            Field("image_model", "Image model", advanced=True, placeholder="gpt-image-1"),
        ),
        required=("image_api_key",),
    ),
    Section(
        "github", "Auto-fix", "Lets Jarvis prepare Salts FSM bug fixes as pull requests for you to approve.",
        (
            Field("github_token", "GitHub token", "secret",
                  "Fine-grained token for the FSM repo: contents, pull requests, issues, actions (read and write)."),
            Field("fsm_repo", "FSM repository", placeholder="owner/repo"),
            Field("fsm_deploy_workflow", "Deploy workflow", placeholder="deploy-azure.yml"),
            Field("fixer_mode", "Fix engine", "select", options=(
                ("builtin", "Jarvis prepares fixes"), ("claude_action", "GitHub Claude action"), ("off", "Off"))),
            Field("fsm_default_branch", "Main branch", advanced=True),
            Field("azure_deploy_mode", "Deploy through", "select",
                  options=(("github", "GitHub workflow"), ("kudu", "Direct to App Service")), advanced=True),
            Field("azure_fsm_scm_url", "FSM Kudu (SCM) address", "url", advanced=True),
            Field("azure_kudu_user", "Kudu user", advanced=True),
            Field("azure_kudu_password", "Kudu password", "secret", advanced=True),
        ),
        required=("github_token", "fsm_repo"),
        test=True,
    ),
    Section(
        "selfimprove", "Self-improvement", "Lets Jarvis propose changes to its OWN source code as a pull "
                                          "request. It never merges or deploys these itself - only you can.",
        (
            Field("jarvis_repo", "Jarvis's own repository", placeholder="owner/repo"),
            Field("jarvis_github_token", "GitHub token", "secret",
                  "Fine-grained token for this repo: contents, pull requests (read and write). Leave blank to "
                  "reuse the Auto-fix token above if it already covers this repo too."),
            Field("jarvis_default_branch", "Main branch", advanced=True),
        ),
        required=("jarvis_repo",),
        test=True,
    ),
    Section(
        "plugins", "Plugins and MCP tools", "Optional extras, each with its own switch. None of them can approve "
                                            "or send anything - approvals still need your click on the display.",
        (
            Field("plugin_context7_enabled", "Context7 (library docs for the engineering agent)", "bool",
                  "Read-only. Lets the agent that writes Jarvis's and Salts FSM's code look up current, "
                  "version-specific library documentation. Sends library names and questions to Context7's "
                  "service. Does nothing until a pinned version is set in mcp_plugins.yaml."),
            Field("plugin_superpowers_enabled", "Superpowers method (engineering agent)", "bool",
                  "Makes the engineering agent plan first, write the test first and review its own change "
                  "before opening a pull request. Adds written instructions only - no software is installed."),
            Field("plugin_browser_use_enabled", "Browser Use (read-only browsing)", "bool",
                  "Off by default. Jarvis may read pages on the domains below; it can never click, log in, "
                  "submit or buy. Needs a reviewed, pinned install in mcp_plugins.yaml before it does anything."),
            Field("plugin_browser_allowed_domains", "Browser Use allowed domains", "textarea",
                  "Comma-separated, e.g. bsigroup.com, gov.uk. Subdomains are included. Finance, Sage and bank "
                  "sites are always refused, even if listed.", advanced=True),
            Field("plugin_thoughtproof_enabled", "ThoughtProof (extra check before approved actions run)", "bool",
                  "Off by default. When on, every action you approve is first checked against the rules in "
                  "mandates.yaml; a BLOCK cancels it and tells you. If the checker can't be reached the action is "
                  "cancelled, never run unchecked. It adds to your approval click, never replaces it."),
        ),
    ),
    Section(
        "proactive", "Jarvis speaking up", "Lets Jarvis post into the open chat (and read it aloud in voice mode) when "
                                          "a background job finishes or something he's watching changes. It can "
                                          "only tell you things - approvals and changes still need your click.",
        (
            Field("proactive_chat_enabled", "Let Jarvis post into the chat by himself", "bool",
                  "Off by default. There is also a mute button on the chat for the session you have open."),
            Field("proactive_quiet_start", "Quiet from (HH:MM)", placeholder="21:00",
                  help="Nothing is posted into the chat or sent to Teams between these times. UK time."),
            Field("proactive_quiet_end", "Quiet until (HH:MM)", placeholder="07:30"),
            Field("proactive_max_per_hour", "Most messages an hour", "number",
                  "Anything over this waits for the next check. 0 means no limit.", advanced=True),
            Field("proactive_pr_watch_min", "Check pull requests every (minutes)", "number", advanced=True),
        ),
    ),
    Section(
        "storage", "Report archive", "Keeps a copy of reports and documents in Azure Storage.",
        (
            Field("azure_storage_connection_string", "Storage connection string", "secret"),
            Field("azure_storage_container", "Container", advanced=True),
        ),
        required=("azure_storage_connection_string",),
        test=True,
    ),
    Section(
        "finance", "Finance and targets", "Year end, VAT and the targets Jarvis measures the business against.",
        (
            Field("financial_year_end", "Financial year end (MM-DD)", placeholder="03-31"),
            Field("vat_quarter_end_months", "VAT quarter-end months", placeholder="3,6,9,12"),
            Field("vat_scheme", "VAT scheme", "select", options=(("invoice", "Standard (invoice)"), ("cash", "Cash"))),
            Field("monthly_payroll_estimate", "Monthly payroll (£)", "number"),
            Field("monthly_overheads_estimate", "Monthly overheads (£)", "number"),
            Field("target_gross_margin_pct", "Target gross margin %", "number"),
            Field("target_debtor_days", "Target debtor days", "number"),
            Field("target_quote_conversion_pct", "Target quote conversion %", "number"),
            Field("target_utilisation_pct", "Target engineer utilisation %", "number"),
            Field("target_recurring_revenue_pct", "Target recurring revenue %", "number"),
            Field("target_cash_runway_months", "Target cash runway (months)", "number"),
            Field("renewal_uplift_pct", "Contract renewal uplift %", "number"),
            Field("renewal_notice_days", "Prepare renewals (days ahead)", "number"),
            Field("associated_companies", "Associated companies", "number", advanced=True),
            Field("boe_base_rate", "Bank of England base rate %", "number", advanced=True),
            Field("target_overdue_pct", "Max overdue debt %", "number", advanced=True),
            Field("target_revenue_growth_pct", "Target revenue growth %", "number", advanced=True),
            Field("lone_worker_overrun_min", "Lone-worker check after (minutes over)", "number", advanced=True),
        ),
    ),
    Section(
        "schedules", "Schedules", "When Jarvis does its regular jobs. Times are UK time.",
        (
            Field("briefing_cron", "Morning briefing", "cron"),
            Field("wrapup_cron", "End-of-day wrap-up", "cron"),
            Field("suggestions_cron", "Suggestion sweeps", "cron"),
            Field("billing_check_cron", "Unbilled jobs check", "cron"),
            Field("review_requests_cron", "Review requests", "cron"),
            Field("staff_review_cron", "Weekly team review", "cron"),
            Field("business_review_cron", "Monthly business review", "cron"),
            Field("regulatory_watch_cron", "Tax and employment-law watch", "cron"),
            Field("technical_watch_cron", "Fire & security technical/standards watch", "cron"),
            Field("security_watch_cron", "Security review of Salts FSM's code", "cron"),
            Field("compliance_check_cron", "Compliance check", "cron", advanced=True),
            Field("self_learning_cron", "Self-reflection (what to remember)", "cron", advanced=True),
            Field("conversation_quality_cron", "Weekly conversation quality summary", "cron", advanced=True),
            Field("conversation_quality_retention_days", "Keep conversation quality records (days)", "number",
                  advanced=True),
            Field("routine_test_interval_min", "Routine tests every (minutes)", "number", advanced=True),
            Field("inbox_check_interval_min", "Check inbox every (minutes)", "number", advanced=True),
            Field("lone_worker_check_min", "Lone-worker sweep every (minutes)", "number", advanced=True),
        ),
        guide=("Schedules use cron format: minute hour day month weekday. \"45 7 * * 1-5\" means 7:45 on weekdays; "
               "\"0 17 * * 1-5\" means 5pm on weekdays.",),
    ),
    Section(
        "standing", "Standing approvals", "Things you let Jarvis do without asking each time. Both are OFF. Only "
        "you can change these, here - Jarvis, a pending action or a Teams message never can. Everything else "
        "(money, deletions, job booking or scheduling, supplier orders, stock, other emails, code changes and "
        "deploys, accreditations, staff and settings) still waits for your approval.",
        (
            Field("standing_record_keeping", "Record keeping", "bool",
                  "ON lets Jarvis go ahead by itself, without waiting for approval, with ONLY these: creating a "
                  "new customer, site or contact in Salts FSM, and adding a note, task or reminder. It never edits "
                  "or deletes anything, and never touches jobs, quotes, invoices, prices or stock. Each one shows "
                  "as \"Done automatically\" on the display and in Teams."),
            Field("standing_acknowledgements", "Routine acknowledgements", "bool",
                  "ON lets Jarvis immediately email a customer a receipt-only \"we've received your purchase "
                  "order\" reply when it has matched their PO email to a quote you sent. It doesn't say a job is "
                  "booked: the quote is still only accepted and the job still only booked when you approve. "
                  "That booking approval then sends a second, separate \"your job is booked\" email."),
            Field("standing_max_per_hour", "Most automatic actions per hour", "number",
                  "Safety limit across both switches. Past it, actions wait for your approval as normal and you "
                  "get a warning.", advanced=True),
        ),
    ),
    Section(
        "security", "Security and staff", "Your display password and the staff issue-report link.",
        (
            Field("jarvis_owner_password", "Display password", "secret",
                  "Changing it signs everyone out; sign back in with the new one."),
            Field("staff_report_key", "Staff report key", "secret",
                  "Part of the link staff use to report problems. Change it to stop an old link working."),
        ),
    ),
)

FIELDS: dict[str, Field] = {f.key: f for s in SECTIONS for f in s.fields}
# Settings that widen what Jarvis may do without asking, plus the ones that decide who counts as the owner (so a
# manager can't make themselves the owner and then flip the first group). The Settings API refuses to change any of
# these unless the request comes from the owner themselves (see main.save_settings / auth.is_principal_owner), not
# any signed-in manager. manager_emails / management_emails are env-only today (not on the page) but are listed so
# they stay protected if they are ever added.
OWNER_IDENTITY_KEYS = frozenset({"owner_email", "partner_email", "manager_emails"})  # who the approvers are
OWNER_ONLY_KEYS = frozenset(f.key for s in SECTIONS if s.id == "standing" for f in s.fields) | frozenset({
    "owner_email", "partner_email", "manager_emails", "management_emails", "jarvis_owner_password",
    "staff_report_key", "van_locations_out_of_hours",  # the last widens who can see where staff are out of hours
    "engineer_model", "engineer_effort"})  # which model / how hard the code-writing agents work: owner's call (cost)
SECTIONS_BY_ID = {s.id: s for s in SECTIONS}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# Secrets that are credentials for getting INTO Jarvis (or part of a link that does): not even their last four characters
# are shown on the page. Everything else shows "•••• abcd" so you can tell which key is saved.
NO_TAIL_HINT = frozenset({"staff_report_key", "jarvis_owner_password"})


def _hint(value: str) -> str:
    if "://" in value:
        return "••••"  # a secret URL (e.g. the Teams/Power Automate webhook) ends in its access signature
    return "•••• " + value[-4:] if len(value) >= 12 else "••••"


_PRISTINE_ATTR = "_jarvis_env_base"


class SettingsStore:
    FILE = "connections.enc"

    def __init__(self, settings: Settings):
        self.s = settings
        self.path = settings.data_dir / self.FILE
        # What App Service / .env / the code defaults say, before anything saved here is applied. Snapshotted once per
        # Settings object: building a second store on the same object (create_app called twice) must not read values
        # that an earlier store already overlaid with saved overrides, or "the configured owner" would drift to
        # whatever was saved on the Settings page.
        pristine = settings.__dict__.get(_PRISTINE_ATTR)
        if pristine is None:
            pristine = {k: getattr(settings, k) for k in FIELDS}
            object.__setattr__(settings, _PRISTINE_ATTR, pristine)
        self.base = dict(pristine)
        self.problem = ""
        self.overrides: dict[str, Any] = self._load()

    # ------------------------------------------------------------------ storage
    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        from cryptography.fernet import InvalidToken

        try:
            data = json.loads(fernet(self.s.jarvis_secret_key, "jarvis-settings").decrypt(self.path.read_bytes()))
        except (InvalidToken, ValueError) as e:
            log.warning("Saved settings couldn't be read (%s); keeping a copy and starting afresh.", type(e).__name__)
            self.path.replace(self.path.with_suffix(".unreadable"))
            self.problem = ("Settings saved earlier couldn't be read, because Jarvis's secret key has changed. "
                            "Please enter them again.")
            return {}
        return {k: v for k, v in data.items() if k in FIELDS}

    def _save(self) -> None:
        token = fernet(self.s.jarvis_secret_key, "jarvis-settings").encrypt(json.dumps(self.overrides).encode())
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(token)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # ------------------------------------------------------------------ applying
    def apply(self) -> None:
        """Put Settings back to base values, then apply what's been saved here."""
        for key in FIELDS:
            value = self.overrides.get(key, self.base[key])
            try:
                setattr(self.s, key, self._coerce(key, value))
            except (ValidationError, ValueError) as e:
                log.warning("Ignoring saved %s: %s", key, e)
                setattr(self.s, key, self.base[key])
        self.s.vat_quarter_months = sorted(int(m) for m in self.s.vat_quarter_end_months.split(",") if m.strip())

    @staticmethod
    def _coerce(key: str, value: Any) -> Any:
        annotation = Settings.model_fields[key].annotation
        return TypeAdapter(annotation).validate_python(value)

    def validate(self, key: str, value: Any) -> tuple[Any, str]:
        """(clean value, error message)."""
        f = FIELDS[key]
        if isinstance(value, str):
            value = value.strip()
        if f.kind == "notes":
            value = " | ".join(line.strip() for line in str(value).splitlines() if line.strip())
        if f.kind == "email" and value and not _EMAIL.match(value):
            return None, "That doesn't look like an email address."
        if f.kind == "url" and value:
            # A bare domain ("fsm.example.co.uk") is a much more likely mistake than someone actually
            # wanting a scheme-less value - fix it up rather than silently rejecting the save and leaving
            # them thinking it's configured when it never actually got saved.
            if not re.match(r"^https?://", value) and re.match(r"^[^\s/]+\.[^\s/]+", value):
                value = f"https://{value}"
            if not re.match(r"^https?://[^\s/]+", value):
                return None, "Start with https://"
        if key == "ram_api_base_url" and value:
            from .integrations.ramtracking import origin_of

            value = origin_of(value)  # RAM's calls are all absolute paths; a pasted endpoint or Swagger link would 404
        if f.kind == "select" and value not in {v for v, _ in f.options}:
            return None, "Pick one of the options."
        if f.kind == "cron" and value:
            from .cron import cron_trigger

            try:
                cron_trigger(value)
            except ValueError:
                return None, "Use cron format, e.g. 45 7 * * 1-5"
        if key in ("proactive_quiet_start", "proactive_quiet_end") and not re.fullmatch(
                r"([01]?\d|2[0-3]):[0-5]\d", str(value)):
            return None, "Use a time like 21:00"
        if key in ("proactive_max_per_hour", "proactive_pr_watch_min"):
            try:
                if int(value) < 0 or (key == "proactive_pr_watch_min" and int(value) < 1):
                    return None, "Enter a whole number, not less than " + ("1" if key == "proactive_pr_watch_min" else "0")
            except (TypeError, ValueError):
                return None, "Enter a number."
        if key == "jarvis_owner_password" and len(value) < 8:
            return None, "Use at least 8 characters."
        if key == "claude_code_oauth_token" and value and not value.startswith("sk-ant-oat"):
            return None, "A Claude Max token starts sk-ant-oat01-"
        if key == "vat_quarter_end_months" and value and not re.fullmatch(r"\s*\d{1,2}(\s*,\s*\d{1,2})*\s*", value):
            return None, "Month numbers separated by commas, e.g. 3,6,9,12"
        try:
            return self._coerce(key, value), ""
        except ValidationError:
            return None, "That isn't a valid value." if f.kind != "number" else "Enter a number."

    def update(self, values: dict[str, Any], clear: list[str]) -> dict[str, str]:
        """Validate and save. Returns {key: error}; nothing is saved if there are errors."""
        errors: dict[str, str] = {}
        clean: dict[str, Any] = {}
        for key, value in values.items():
            if key not in FIELDS:
                errors[key] = "Not a setting that can be changed here."
                continue
            if FIELDS[key].kind == "secret" and value in ("", None):
                continue  # a blank secret box means "leave it as it is"
            clean[key], error = self.validate(key, value)
            if error:
                errors[key] = error
        errors.update({k: "Not a setting that can be changed here." for k in clear if k not in FIELDS})
        if errors:
            return errors
        for key in clear:
            self.overrides.pop(key, None)
        for key, value in clean.items():
            if value == self._coerce(key, self.base[key]):
                self.overrides.pop(key, None)  # same as Azure/default: nothing to keep here
            else:
                self.overrides[key] = value
        self._save()
        self.problem = ""
        self.apply()
        return {}

    # ------------------------------------------------------------------ what the page shows
    def _source(self, key: str) -> str:
        if key in self.overrides:
            return "here"
        if key.upper() in os.environ:
            return "azure"
        return "default"

    def configured(self, section: Section) -> bool:
        s = self.s
        if section.id == "claude":
            return bool(s.claude_code_oauth_token or s.anthropic_api_key)
        if section.id == "marketing":
            return any(getattr(s, k) for k in ("google_places_api_key", "meta_page_token", "linkedin_access_token",
                                               "tiktok_access_token", "google_review_url"))
        if section.id == "voice":
            return s.effective_tts != "browser"
        if section.id == "selfimprove":
            return s.jarvis_self_improve_configured
        return all(getattr(s, k) for k in section.required) if section.required else True

    def fingerprint(self, section: Section) -> str:
        values = json.dumps({f.key: str(getattr(self.s, f.key)) for f in section.fields}, sort_keys=True)
        return hashlib.sha256(values.encode()).hexdigest()[:16]

    def view(self, db, context: dict[str, Any]) -> dict[str, Any]:
        sections = []
        for sec in SECTIONS:
            fields = []
            for f in sec.fields:
                value = getattr(self.s, f.key)
                item = {"key": f.key, "label": f.label, "kind": f.kind, "help": f.help, "placeholder": f.placeholder,
                        "options": [list(o) for o in f.options], "advanced": f.advanced, "source": self._source(f.key),
                        "depends_on": list(f.depends_on) if f.depends_on else None}
                if f.kind == "secret":
                    item.update(is_set=bool(value),
                                hint=("••••" if f.key in NO_TAIL_HINT else _hint(str(value))) if value else "")
                else:
                    shown = str(value).replace(" | ", "\n").replace("|", "\n") if f.kind == "notes" else value
                    item.update(value=shown, is_set=value not in ("", None))
                fields.append(item)
            test = json.loads(db.get_kv(f"conn_test:{sec.id}") or "null") if sec.test else None
            if test and test.get("fingerprint") != self.fingerprint(sec):
                test["stale"] = True
            sections.append({
                "id": sec.id, "title": sec.title, "blurb": sec.blurb, "fields": fields, "test": sec.test,
                # A badge only makes sense where there's something to be connected or missing - not for a
                # page of plain settings (finance targets, schedules) that's always "there" either way.
                "show_badge": sec.test or bool(sec.required),
                "configured": self.configured(sec), "last_test": test,
                "guide": [step.format(**context) for step in sec.guide],
            })
        return {"sections": sections, "problem": self.problem}

    def record_test(self, db, section_id: str, ok: bool, detail: str) -> dict[str, Any]:
        result = {"ok": ok, "detail": detail[:500], "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "fingerprint": self.fingerprint(SECTIONS_BY_ID[section_id])}
        db.set_kv(f"conn_test:{section_id}", json.dumps(result))
        return result
