# J.A.R.V.I.S. for Salts Fire and Security

A Tony Stark-style AI assistant for Salts Fire and Security. It has a real British voice and a live heads-up
display, and it runs the business alongside you. It reads your email, knows Salts FSM inside out, watches staff,
stock, vans and money, fixes software problems, deploys the fixes to Azure, and advises like a consultant. Its
brain is Claude, so it is also a fully capable general AI you can ask anything.

```
         ┌──────────────── HUD (browser / wall screen / phone) ────────────────┐
voice ⇄  │ ElevenLabs voice ◀─ Jarvis ─▶ Deepgram / Whisper mic · live panels · map │
         └───────────────────────────────┬──────────────────────────────────────┘
                                         │ WebSocket + REST (FastAPI)
      ┌──────────────────────────────────┴───────────────────────────────────┐
      │ Brain: Claude (your Max subscription via Agent SDK, or the API) + 80 tools │
      └───┬──────────┬──────────┬──────────┬──────────┬──────────┬───────────┘
      Outlook/   Salts FSM   Sage /    RAM       GitHub →   Socials,
      Teams      (jobs, staff, Sage 50  Tracking  Azure      Google, web
                 stock, sites) CSV      (vans)    (fixes)    search
```

## What Jarvis does

| Area | What it does |
|---|---|
| **Conversation** | Talk ("Jarvis, …") or type. Full Claude-quality answers to anything, with web search, photo/PDF attachments and a memory of what you tell it. Short, natural spoken replies; detail goes up on the display. |
| **Out-of-hours calls** | Reads your answering service's emailed call reports, tells you in the morning briefing what came in overnight and what was done, and suggests booking a call-out for anything that still needs a visit but has no job in Salts FSM. |
| **Email & updates** | Reads, searches and summarises Outlook. Drafts replies. Sends you updates on Teams or email when you ask, a spoken morning briefing, and an end-of-day wrap-up at 5pm (what got done, what slipped, what's waiting on you, tomorrow's first jobs). |
| **Salts FSM** | Knows the app (knowledge base + live API + source code on GitHub): jobs, engineers, sites, systems, contracts, quotes, service schedules, renewals. Watches the remedial quotes the FSM raises from service visits and chases any that stall. |
| **Issues → fixes → Azure** | Staff report problems at `/report` or by email with `[ISSUE]` in the subject. Jarvis tells you at once and triages the report. For Salts FSM bugs, its engineering agent writes the fix as a pull request and CI tests it. **You approve** it, then Jarvis merges it, deploys it to Azure, re-runs the smoke tests and tells the reporter it's fixed. |
| **Routine tests** | Every 15 minutes: Salts FSM uptime, key pages, TLS certificate and every integration. Every morning: fire & security compliance (overdue service visits, lapsed or renewing contracts, overdue call-outs, expiring qualifications). |
| **Staff** | A register of every person's role, duties and expected targets. Measures engineers (jobs per day, utilisation, on-time starts, repeat call-outs, revenue per hour) and office staff (quotes, win rate, bookings, email and Teams activity). A weekly review flags anyone falling short, with the evidence. |
| **Vans (RAM Tracking)** | Live map, nearest engineer to a call-out, and "when did Dan set off and get home on Tuesday?". Checks timesheets against the tracker. |
| **Stores** | Stock in stores and on vans (from Salts FSM), goods in, parts used per job, transfers, stocktakes, reorder list and purchase orders (sent only after you approve). |
| **Accountant** | Cash, aged debtors and creditors, credit control with statutory late-payment interest, VAT return estimate, corporation tax, a 13-week cash flow and deadlines. Finds completed-but-unbilled jobs and drafts the Sage invoices for your approval. |
| **Business consultant** | Business health check against targets, monthly board-style advisory report, and deep dives (pricing, growth, SWOT, hiring, acquisitions) with a 90-day plan. |
| **Law & tax watch** | A weekly web-researched update for you and your business partner on UK tax, employment law, company law and fire & security regulation changes, with sources. |
| **Accreditations** | BAFE, SSAIB, CHAS (and NSI etc.) renewal and audit reminders, and audit-ready evidence packs with draft questionnaire answers. |
| **Customer health watch** | A 0-100 score for every customer from spend trend, payment behaviour, repeat faults, declined quotes, service visits we're behind on, logged problems, inactivity and lapsed renewals. At-risk customers (especially within 90 days of renewal) are flagged with the reasons and a plan to keep them, and it warns if one customer is too big a share of revenue. |
| **Renewals & fleet safety** | Renewal letters with the standard uplift prepared 60 days ahead for approval (at-risk customers get "call first" instead). Van MOT, service, insurance and tax, ladder, harness and PAT inspection reminders. Lone-worker checks when an engineer is still on a job long after it should have finished. |
| **Meetings & paperwork** | Teams meeting transcripts (or pasted notes) become minutes and tracked actions, with chasers suggested when they're overdue. RAMS drafted per job. Tender / PQQ / Constructionline answers drafted from your real accreditation, insurance, policy and competency evidence. |
| **Marketing** | Follower growth on Facebook, Instagram, LinkedIn and TikTok, Google reviews, Search Console rankings, a website SEO audit and weekly suggestions. Review requests after each job (approved in one tap). |

**Jarvis suggests; you decide.** It never changes anything on its own. A few times a day it looks through
everything and puts suggestions on the display, for example "Invoice 14 completed jobs (£8,065)?", "Assign call-out
J24099 - Priya is nearest?" or "Reorder 4 items from Fire Alarm Wholesale?". "Do it" makes Jarvis prepare the work,
which then waits for your Approve.

Anything not connected yet runs on clearly labelled **demo data**, so you can try it straight away.

## Try it now (demo mode)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add a Claude credential (see below); leave the rest blank for demo data
python -m jarvis                # open http://localhost:8000
```

Without `JARVIS_OWNER_PASSWORD` Jarvis only answers on the machine it runs on. Set a password before putting it
anywhere else. Use Chrome or Edge for voice.

## Claude: your Max plan or the API

- **Max/Pro subscription (no API credits).** On your own computer run `npm i -g @anthropic-ai/claude-code`, then
  `claude setup-token`, and put the token in `CLAUDE_CODE_OAUTH_TOKEN`. Jarvis then runs through the
  [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview), which comes with Claude Code. Usage counts
  against your plan's limits, and the scheduled reports use it too. Anthropic's docs say developers may not offer
  claude.ai login in products for other people. This is intended as **your own** tool on your own subscription. If
  you ever want to offer it more widely, switch to an API key.
- **API key.** Set `ANTHROPIC_API_KEY` (and optionally `LLM_BACKEND=api`). You pay per use, and you get prompt
  caching and automatic fallback if a request is refused.

## Connecting the real systems

Do these in any order; each one replaces demo data as soon as it's set.

1. **Microsoft 365 (Outlook + Teams)**
   - In Entra ID, register an app, add a client secret, and grant the *application* permissions `Mail.ReadWrite`,
     `Mail.Send`, `Reports.Read.All` (office activity), `Calendars.Read` and `OnlineMeetingTranscript.Read.All`
     (meeting write-ups), with admin consent. For transcripts also run
     `New-CsApplicationAccessPolicy -Identity Jarvis -AppIds <id>` and `Grant-CsApplicationAccessPolicy` for your user.
   - Restrict it to your mailbox (plus the shared mailbox your out-of-hours reports arrive in, e.g. info@):
     `New-ApplicationAccessPolicy -AppId <id> -PolicyScopeGroupId <mail-enabled group> -AccessRight RestrictAccess`.
   - To see names in activity reports, turn off *"Display concealed user, group, and site names"* in the M365
     admin centre (Settings → Org settings → Reports).
   - For Teams updates, create a Teams **Workflows** "post to a channel when a webhook request is received" flow
     and put its URL in `TEAMS_WEBHOOK_URL`.
2. **Salts FSM.** Set `FSM_BASE_URL` and `FSM_API_KEY`, and edit `fsm_endpoints.yaml` to match the FSM's API
   routes: jobs, engineers, systems, contracts, quotes, sites, timesheets, stock, tracking. Field names are matched
   flexibly. If the FSM has no API yet, add read-only JSON endpoints for these and Jarvis will pick them up.
3. **Sage.**
   - *Sage Accounting (cloud):* create an app at developerselfservice.sageone.com with the callback
     `https://<jarvis>/auth/sage/callback`, set `SAGE_CLIENT_ID` and `SAGE_CLIENT_SECRET`, then use **Settings →
     Connect Sage** on the display. To let Jarvis create invoices you've approved, set `SAGE_WRITE_ENABLED=true`
     and reconnect.
   - *Sage 50 (desktop):* export sales and purchase invoices (and bank balances) to CSV into `finance_data/`, as
     `sales_invoices.csv`, `purchase_invoices.csv` and `bank.csv`. Sage's usual column names are recognised.
4. **RAM Tracking.** Ask RAM for External API access, set `RAM_API_BASE_URL` and `RAM_API_KEY`, and match
   `ram_endpoints.yaml` to the paths in RAM's Swagger docs. Put each engineer's van registration in the staff
   register.
5. **Staff register.** Copy `staff_roles.example.yaml` to `data/staff_roles.yaml` and describe everyone's role,
   duties and targets, or just tell Jarvis ("Jarvis, Josh should be sending 14 quotes a week").
6. **Accreditations.** Copy `accreditations.example.yaml` to `data/accreditations.yaml` and add your real
   certificate numbers and dates.
7. **Auto-fix and deploy.**
   - Put the Salts FSM code on GitHub.
   - Create a fine-grained token for that repo (contents, pull requests, issues, actions: read and write) and set
     `GITHUB_TOKEN` and `FSM_REPO`.
   - Add `templates/fsm-repo/.github/workflows/deploy-azure.yml` to the FSM repo (edit its build step) and set
     `FSM_DEPLOY_WORKFLOW`.
   - Or, to deploy straight to App Service instead of through that workflow, set `AZURE_DEPLOY_MODE=kudu` and
     `AZURE_FSM_SCM_URL`.
8. **Voice.**
   - Put your ElevenLabs key in `ELEVENLABS_API_KEY`. The default voice is "Daniel", a deep, authoritative British
     male; "George" is warmer. You can pick any voice under Settings.
   - Speech-to-text: set `DEEPGRAM_API_KEY` for live, always-listening mode, or `OPENAI_API_KEY` for Whisper
     push-to-talk.
   - Wispr Flow and other dictation apps also work straight into the chat box.
9. **Marketing.** `GOOGLE_REVIEW_URL`; `GOOGLE_PLACES_API_KEY` + `GOOGLE_PLACE_ID` (reviews); a Search Console
   service account (rankings); Meta Page token (Facebook/Instagram); LinkedIn and TikTok tokens.

## Deploying to Azure

```bash
az group create -n rg-jarvis -l uksouth
az deployment group create -g rg-jarvis -f infra/main.bicep \
  -p appName=salts-jarvis ownerPassword='<strong password>' staffReportKey='<random>' \
     claudeCodeOauthToken='<from claude setup-token>'
```

Then add the rest of your `.env` values as App Service application settings. Set up the GitHub OIDC secrets
described in `.github/workflows/deploy-azure.yml`, and every push to `main` runs the tests and deploys. Run a
single instance: the scheduler and live display state live in the app. Data (SQLite, uploads, register files)
lives in `/home/data`.

## Safety, privacy and trust

- **Nothing happens without your approval.** Jarvis reads, checks, analyses, advises and suggests freely.
  Anything that *changes* something is queued as an approval card: emails (other than updates to you), invoices,
  purchase orders, review requests, stock movements and stocktakes, staff-register and accreditation edits,
  Salts FSM changes, Azure uploads, starting a code fix, and deployments. It only happens when you tap Approve or say
  "approve". There is no auto-deploy. The AI itself cannot approve anything, and nor can anything in an email,
  document or web page. The only things it does without asking are sending *you* the updates and reports you
  asked for, saving email *drafts* for you to review, and keeping its own notes.
- The auto-fix engineer can only read and edit a copy of the code, with no shell and no secrets, and every
  change goes through a pull request and CI.
- **Staff monitoring:** tell staff in writing what is monitored and why (job data, timesheets, vehicle tracking
  during working hours, Microsoft 365 activity *counts*, never message content). This keeps you within UK GDPR
  and ICO employment guidance. Jarvis treats flags as prompts for a conversation, not verdicts.
- **This repository is public.** Never commit `.env`, `data/`, `finance_data/` or `knowledge/private/` (all
  git-ignored). Better still, make the repo private.
- Financial and legal outputs are management estimates and research. Have your accountant or solicitor check
  statutory filings and HR decisions.

## Project layout

```
jarvis/
  brain/          Claude conversation loop, Max-subscription backend, tools, persona
  integrations/   Microsoft 365, Salts FSM, Sage/CSV, RAM Tracking, GitHub, Azure, voice, socials
  services/       accountant, staff, performance, stores, tracking, issues, fixer, routine tests,
                  billing, marketing, advisor, accreditations, regulatory watch, scheduler
  web/            the HUD, login and staff report pages
knowledge/        what Jarvis knows (standards, law, tax, company, Salts FSM; private/ is git-ignored)
infra/            Azure Bicep     templates/   workflow for the Salts FSM repo     tests/   pytest suite
```

Run the tests with `pip install -r requirements-dev.txt && python -m pytest`.
