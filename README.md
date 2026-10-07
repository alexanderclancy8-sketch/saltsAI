# J.A.R.V.I.S. for Salts Fire and Security

A Tony Stark-style AI assistant for Salts Fire and Security. It has a real British voice and a live heads-up
display, and it runs the business alongside you. It reads your email, knows Salts FSM inside out, watches staff,
stock, vans and money, fixes software problems, deploys the fixes to Azure, and advises like a consultant. Its
brain is Claude, so it is also a fully capable general AI you can ask anything.

```
         ┌──────────────── HUD (browser / wall screen / phone) ────────────────┐
voice ⇄  │ Piper/ElevenLabs voice ◀─ Jarvis ─▶ Azure / Deepgram mic · live panels · map │
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
| **Your own automations** | Set up recurring checks yourself, in plain English - "every weekday at 8am, check for jobs with no engineer assigned and tell me", "every 30 minutes, check for a supplier email about the delayed order". It runs itself from then on with the same tools and the same approval rules as talking to it live - looking things up is automatic, but anything it wants to change still needs your approval. Ask it to list or remove what you've set up any time. |
| **Recruiting agents** | For a one-off chunk of work worth doing on its own - a focused piece of research, a draft, an analysis - Jarvis can recruit a fresh sub-agent with its own narrow brief and tools, and report back with what it found. Same rules apply to it as to Jarvis: anything it proposes writing queues for your approval, never happens on its own, and it can never recruit further agents or start another background job itself. |
| **Word & Excel** | Reads `.docx` / `.xlsx` attachments on emails, builds Word and Excel deliverables (reports, schedules, tender documents, stock and finance exports) and edits an attachment or earlier draft into a new copy. Everything is saved as a draft on the display with a download link (PDF / Word / Excel) for you to review - nothing is ever emailed without your approval, and the original file is never changed. PDF and Word documents are Salts-branded: `COMPANY_ADDRESS` is printed in the footer, and the header shows the bundled Salts logo unless `COMPANY_LOGO_PATH` points at another PNG/JPEG (max 2 MB). Edited copies are rebuilt from text and tables, so formulas, images and styling aren't carried over. |
| **Out-of-hours calls** | Reads your answering service's / monitoring centre's emailed reports (including PDF attachments) - calls taken plus alarm faults, comms failures and activations - tells you in the morning briefing what came in overnight and what was done, and suggests booking a call-out for anything that still needs a visit but has no job in Salts FSM. |
| **Company check before a quote** | Free Companies House look-up of a new commercial customer by name or number: active or not, how long it has existed, accounts or confirmation statement overdue, dormant accounts, insolvency history, charges outstanding. Filing status only - not a credit score, and sole traders and partnerships aren't on it. Read-only; needs the free API key (Settings → Companies House). |
| **Email & updates** | Reads, searches and summarises Outlook. Drafts replies. Sends you updates on Teams or email when you ask, a spoken morning briefing at 9am and an end-of-day wrap-up at 5:30pm on weekdays (each under a minute: what got done, what slipped, what's waiting on you, tomorrow's first jobs), posted to the console and Teams; the times are editable in Settings > Schedules. |
| **What Jarvis did** | One place to check everything Jarvis drafted, emailed, proposed or changed, and what you approved, declined or left waiting: **Activity** on the left of the console (or "See everything Jarvis did" in Approvals). A one-line summary for today ("Today: 4 proposed, 2 approved, 1 declined, 1 waiting, 0 failed, 3 checks with nothing to report"), filters for today / 7 / 30 days, kind, status ("Needs a look" = failed + waiting), who and a free-text search, newest first, expandable rows with the cleaned-up details, the error for a failure and who approved it and when. Quiet scheduled checks with nothing to report are one collapsed line unless you tick "Include everything". Only you (the owner) can **Export CSV** of what the filters show. It only reads: it can't approve, send or change anything. Ask "what did you do today / yesterday / this week?" and Jarvis answers aloud with the counts first and the few things worth a look. |
| **Team mode** | A cut-down console for engineers and office staff: no Finance, Approvals or Connections, and a Jarvis limited to jobs, engineers, systems due, fleet (under the van-privacy rule) and logging a job (which only queues for a manager's approval). The owner turns it on in Settings > Team access by choosing a team access code - no code change - and the team signs in with their name and that code (login page > Team sign-in). Enforced on the server for every route, tool and live event, not just hidden in the page. |
| **Salts FSM** | Knows the app (knowledge base + live API + source code on GitHub): jobs, engineers, sites, systems, contracts, quotes, service schedules, renewals. Watches the remedial quotes the FSM raises from service visits and chases any that stall. Set up a new customer or site in plain English ("create a customer Brightwell Dental, then a site for their Roundhay surgery") - it checks for an existing or similar record first and tells you instead of making a duplicate, and nothing is created until you approve it. Log a job in plain English ("log an intruder alarm fault for Beckfoot Upper Heaton") - or assign it to a named engineer for a date - and it's queued in Salts FSM's own booking form, ready for your approval. Ask about one specific job ("what happened on J24100?") for the full picture - materials used, notes, status history, linked quote/invoice - not just the summary fields the job list has. Won a quote? "Accept Q1180" marks it accepted and books the resulting job together, ready for your approval - then order the materials it needs with the usual purchase order tools, referencing the new job. Jarvis also watches the inbox for the customer's own purchase order confirming a quote: it matches the PO to the quote, and queues the same accept-and-book action with the PO number recorded on the job - approve it and the customer gets an acknowledgement email automatically. |
| **Issues → fixes → Azure** | Staff report problems at `/report` or by email with `[ISSUE]` in the subject. Jarvis tells you at once and triages the report. For Salts FSM bugs, its engineering agent writes the fix as a pull request and CI tests it. **You approve** it, then Jarvis merges it, deploys it to Azure, re-runs the smoke tests and tells the reporter it's fixed. A weekly **security watch** reviews the whole codebase itself for real vulnerabilities the same way. |
| **Routine tests** | Every 15 minutes: Salts FSM uptime, key pages, TLS certificate and every integration. Every morning: fire & security compliance (overdue service visits, lapsed or renewing contracts, overdue call-outs, expiring qualifications). |
| **Staff** | A register of every person's role, duties and expected targets. Measures engineers (jobs per day, utilisation, on-time starts, repeat call-outs, revenue per hour) and office staff (quotes, win rate, bookings, email and Teams activity). A weekly review flags anyone falling short, with the evidence. |
| **Vans (RAM Tracking)** | Live map, nearest engineer to a call-out, and "when did Dan set off and get home on Tuesday?". Checks timesheets against the tracker. "Who is home" works from a home point you set per engineer in Settings > Engineer homes (owner only: a rounded map point is kept, never the postcode - tell the engineer first). |
| **Stores** | Stock in stores and on vans (from Salts FSM), goods in, parts used per job, transfers, stocktakes, reorder list and purchase orders (sent only after you approve). Ask for an ad-hoc order too - any items and quantities, any supplier, not just what's below reorder level - and it prices them from Salts FSM's own (monthly-updated) stock records. |
| **Accountant** | Cash, aged debtors and creditors, credit control with statutory late-payment interest, VAT return estimate, corporation tax, a 13-week cash flow and deadlines. Finds completed-but-unbilled jobs and drafts the Sage invoices for your approval. |
| **Business consultant** | Business health check against targets, monthly board-style advisory report, and deep dives (pricing, growth, SWOT, hiring, acquisitions) with a 90-day plan. |
| **Law & tax watch** | A weekly web-researched update for you and your business partner on UK tax, employment law, company law and fire & security regulation changes, with sources. |
| **Fire & security technical watch** | A separate weekly web-researched digest deepening Jarvis's own technical expertise - not legal changes, but standard revisions and what they mean practically, FIA/BAFE/NSI/SSAIB technical guidance, manufacturer bulletins and installer best practice, always sourced. Ask for it any time on a specific standard or topic too. |
| **Engineer/access codes** | A secure, encrypted replacement for the paper site-code book - engineer/access codes for systems Salts itself installs or maintains, recorded (queued for approval, like anything else) and looked up by site. Never a lookup or search for a system Salts doesn't hold the maintenance relationship for - see "Safety, privacy and trust" below for the takeover-access process instead. |
| **Accreditations** | BAFE, SSAIB, CHAS (and NSI etc.) renewal and audit reminders, and audit-ready evidence packs with draft questionnaire answers. |
| **Everything in the FSM (read-only)** | Jarvis can read every module of Salts FSM - jobs, customers and sites, assets, compliance, quotes and contracts, finance, staff pay and HR, comms and the audit trail - through the FSM's read-only data API (`fsm_catalog` shows what exists, `fsm_data` reads it). Finance, pay, HR and customer contact details are for you alone: a manager can read the rest, the team console none of it. Nothing it reads is written to memory or sent anywhere, and every read is listed in "What Jarvis did" (which resource, how many rows, who - never the values). If the FSM hasn't switched the API on yet, Jarvis says so and keeps using its older tools. |
| **Customer health watch** | A 0-100 score for every customer from spend trend, payment behaviour, repeat faults, declined quotes, service visits we're behind on, logged problems, inactivity and lapsed renewals. At-risk customers (especially within 90 days of renewal) are flagged with the reasons and a plan to keep them, and it warns if one customer is too big a share of revenue. |
| **Renewals & fleet safety** | Renewal letters with the standard uplift prepared 60 days ahead for approval (at-risk customers get "call first" instead). Van MOT, service, insurance and tax, ladder, harness and PAT inspection reminders - just tell Jarvis the date ("the YD71 SFS van's MOT is due 2 November"; it asks you to approve the change, then it lands in the Alerts timeline), and it can drop a van or item that was sold or retired. Lone-worker checks when an engineer is still on a job long after it should have finished. |
| **Meetings & paperwork** | Teams meeting transcripts (or pasted notes) become minutes and tracked actions, with chasers suggested when they're overdue. RAMS drafted per job. Tender / PQQ / Constructionline answers drafted from your real accreditation, insurance, policy and competency evidence. |
| **Marketing** | Follower growth on Facebook, Instagram, LinkedIn and TikTok, Google reviews, Search Console rankings, a website SEO audit and weekly suggestions. Review requests after each job (approved in one tap). Name local competitors and get a side-by-side audit: Google rating/review count (needs a Places key) and the same SEO snapshot run on both sites - ask it to web-search alongside this for anything not covered, like pricing or search ranking position. |

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

**Easiest: the Settings page.** Open the gear icon on the display, go to **Connections**, and fill each service
in - a card per system, with the fields it needs, plain-English help text, a **Test connection** button that
checks it for real, and setup steps for the fiddly ones (Microsoft 365, Sage, Teams). Nothing needs redeploying:
saving reloads Jarvis with the new settings straight away, and secrets are stored encrypted. This replaces
editing `.env` / App Service settings by hand for everything except the two YAML endpoint files below.

Do these in any order; each one replaces demo data as soon as it's set.

1. **Microsoft 365 (Outlook + Teams).** Quickest: in Azure Cloud Shell run
   `APP_NAME=<your app name> bash infra/deploy.sh m365` - it registers Jarvis in Entra ID, requests the
   permissions (`Mail.ReadWrite`, `Mail.Send`, `Calendars.Read`, `Reports.Read.All`,
   `OnlineMeetingTranscript.Read.All`), asks for admin consent, and saves the settings for you. Or do it by hand
   in the Settings page's Microsoft 365 card, which shows the same steps.
   - Whichever way you set it up, it's worth restricting the app to your mailbox (and any shared mailbox, e.g.
     info@) in Exchange Online PowerShell:
     `New-ApplicationAccessPolicy -AppId <id> -PolicyScopeGroupId <mail-enabled group> -AccessRight RestrictAccess`.
     `deploy.sh m365` prints this command with your own values filled in.
   - **Second shared mailbox (service@, Bradford Council portal requests).** Settings → Service inbox (service@): enter the
     address (only the owner can change it) and press **Test**, which reads one message header and tells you plainly what to
     fix if Microsoft refuses. Jarvis reads it through the *same* app registration, so you need no new app or secret - only
     (1) a Graph **application** permission of `Mail.Read` or `Mail.ReadWrite` with admin consent (the `Mail.ReadWrite` above
     already covers it), and (2) if you restricted the app with an Application Access Policy as above, add `service@` to the
     mail-enabled group that policy points at (`Add-DistributionGroupMember -Identity <group> -Member service@...`), then wait
     up to 30 minutes and Test again. A 403 `ErrorAccessDenied` on Test means one of those two is missing. Once readable, its
     unread mail shows in **Comms** under a "service@" heading, and every few minutes Jarvis reads new Bradford Council portal
     emails there and *proposes* a job for each in your Approvals (nothing is created, sent, replied to or deleted without
     your click). What counts as a council email (sender domain, subject phrases) is configurable on the same card.
   - **Companies House (free pre-quote company check).** Settings → *Companies House (free check on new customers)*. Only
     you can change it. To get the free key: (1) go to **developer.company-information.service.gov.uk** and register for a
     free account (or sign in); (2) choose **Create an application** (any name, e.g. "Salts Jarvis"); (3) in the application
     choose **Create new key** and pick the key type **REST**; (4) copy the key into the box and save, then press **Test**,
     which looks up one large public company and says plainly whether the key works. Then ask Jarvis "check Acme Fire Ltd at
     Companies House" (a name or an 8-character company number). It reports whether the company is active, how long it has
     existed (its incorporation date - not proof of trading), whether its accounts or confirmation statement are overdue,
     dormant accounts, insolvency history and charges outstanding (counts only), and ends every answer with the limit:
     *Companies House shows filing status only - it is not a credit score, and sole traders and partnerships aren't on it.*
     A name that is not an exact unique match returns up to five candidates and Jarvis asks you to confirm one by number -
     it never guesses. With the key set, each new customer Jarvis queues for your approval also carries one "Companies House"
     line on the card (switch it off on the same card). It is read-only: it never changes what is sent to Salts FSM, and it
     reads no directors' or individuals' details. Without a key Jarvis just says it isn't connected yet.
   - To see names in activity reports, turn off *"Display concealed user, group, and site names"* in the M365
     admin centre (Settings → Org settings → Reports).
   - For Teams updates (one-way, posted to a channel), create a Teams **Workflows** "post to a channel when a
     webhook request is received" flow and put its URL in the Settings page's Teams card.
   - For **Teams chat** - messaging Jarvis from your phone and getting a real reply, the same conversation as
     the web display - run `APP_NAME=<your app name> bash infra/deploy.sh teamsbot`. It registers Jarvis as a
     Bot Framework bot, turns on the Teams channel, saves the settings, and builds a Teams app package
     (`jarvis-teams-app.zip`) to sideload: Teams > Apps > Manage your apps > Upload a custom app. Only the
     owner's and business partner's email addresses (set under "You and the business") get a reply - anyone
     else who messages the bot is ignored.
   - **Approving from Teams.** Once the bot is set up (the `teamsbot` command saves the three Teams values
     itself; if you ever need to, paste *Bot app ID*, *Bot app secret* and *Directory (tenant) ID* on Settings →
     Teams chat), the owner, partner and managers each open the Jarvis chat in Teams and **say hello once** -
     that's how Jarvis learns where to reach them. From then on, whenever it queues something for approval it
     sends each of them an Adaptive Card (the summary, the action number, **Approve** and **Deny** buttons), and
     they can also reply `approve 12` / `deny 12`. The card is updated to "Approved by Alex at 14:02" whoever
     decides (Teams or the display); if it was already decided you're told so. Until someone has said hello they
     just don't get cards; if Teams is down or not set up, approvals work exactly as before on the display.
     Only one-to-one chats are used - never a group chat or channel.
2. **Salts FSM.** Fill in the web address and API key on the Settings page, and edit `fsm_endpoints.yaml` to
   match the FSM's API routes: jobs, engineers, systems, contracts, quotes, sites, timesheets, stock, tracking.
   Field names are matched flexibly. If the FSM has no API yet, add read-only JSON endpoints for these and
   Jarvis will pick them up.
3. **Sage.**
   - *Sage Accounting (cloud):* create an app at developerselfservice.sageone.com with the callback
     `https://<jarvis>/auth/sage/callback`, put its client ID and secret on the Settings page, then use
     **Connect Sage** there. To let Jarvis create invoices you've approved, turn on "Let Jarvis create invoices"
     and reconnect.
   - *Sage 50 (desktop):* export sales and purchase invoices (and bank balances) to CSV into `finance_data/`, as
     `sales_invoices.csv`, `purchase_invoices.csv` and `bank.csv`. Sage's usual column names are recognised.
4. **RAM Tracking.** In the RAM Tracking portal, go to profile > integrations > API Keys for the Client ID and
   Client secret, and set up a dedicated username/password for the API (RAM's own recommendation - don't use
   your own login; give it no two-step verification). Put all four on the Settings page (the API address stays as
   `https://api.qaifn.co.uk`; only the host is ever used). RAM allows 3 requests a minute per kind of request, so Jarvis
   caches vehicle positions for a minute and journeys for a few; "rate limited" in the Fleet pop-up is that limit, not a
   fault. Put each engineer's van registration in the staff register.
5. **Staff register.** Copy `staff_roles.example.yaml` to `data/staff_roles.yaml` and describe everyone's role,
   duties and targets, or just tell Jarvis ("Jarvis, Josh should be sending 14 quotes a week").
6. **Accreditations.** Copy `accreditations.example.yaml` to `data/accreditations.yaml` and add your real
   certificate numbers and dates - or just tell Jarvis ("our CHAS renews on 14 March", "the YD71 SFS van's MOT is
   due 2 November", "the ladders are inspected again on 15 October"). Each change waits for your approval, and the
   first one creates `data/accreditations.yaml` from the example's layout *without* its placeholder schemes, vans,
   drivers or dates, so the Alerts only ever show your real dates once you start recording them (until then they
   show the example data, and `accreditations_status` says so).
   Once Salts FSM exposes its Company Assets (vehicles with MOT and road tax, test-kit calibration) to Jarvis, those dates
   come from the FSM instead - the reminders at 90/60/30/14/7/1 days run from them, a van or item with no date recorded is
   listed as "date not recorded", vans RAM Tracking and the FSM disagree about are listed as "check this", and this file's
   vans, equipment and calibration (and the example placeholders) are ignored. The file stays as the fallback for when the FSM
   can't supply them.
7. **Auto-fix and deploy.** On the Settings page's Auto-fix card: a fine-grained GitHub token for the FSM repo
   (contents, pull requests, issues, actions: read and write), the repo (`owner/repo`), and either a deploy
   workflow name (add `templates/fsm-repo/.github/workflows/deploy-azure.yml` to the FSM repo first) or, to
   deploy straight to App Service instead, switch "Deploy through" to Kudu and add the FSM app's Kudu address.
   The same GitHub connection also powers the **security watch**: once a week (`security_watch_cron` on the
   Settings page's Schedules card, default Monday 6am) Jarvis reads the whole FSM codebase looking for real,
   exploitable vulnerabilities - never editing anything itself. Findings become ordinary issues, so a genuine
   one goes through the same approve-then-deploy pipeline as any other fix. Ask any time for an ad-hoc one
   ("Jarvis, run a security review now").
8. **Self-improvement.** On the Settings page's Self-improvement card: this repository (`owner/repo`) and a
   GitHub token for it (leave the token blank to reuse the Auto-fix one above, if that PAT already covers this
   repo too). Ask Jarvis to add or fix something about itself ("Jarvis, add a tool that...", "there's a bug
   where...") and it investigates, writes the change and opens a pull request - CI runs on it same as any other
   PR. This is deliberately narrower than the FSM auto-fix: there's no merge step and no deploy step at all: the
   PR just sits there for you to review and merge yourself, whenever you're ready, through your own tooling.
   Jarvis never merges or redeploys itself, under any circumstances.
9. **Voice.** Jarvis speaks with [Piper](https://github.com/OHF-Voice/piper1-gpl) by default - a free, local
   neural voice with no API key and no cost, downloaded once and run on the server itself - so it never falls
   back to the browser's robotic voice even with nothing configured. On the Settings page's Voice card: pick
   from a few free Piper voices, or add an ElevenLabs key for the most natural voice (or run
   `bash infra/deploy.sh voice` for a free Azure one) if you'd rather pay for something better, and a Deepgram key for always-listening speech-to-text.
   **Listening needs no OpenAI key**: with an Azure Speech key set (the one that powers the Azure voice) Jarvis uses
   Azure Speech for push-to-talk - same key and region, nothing extra to sign up for - and the order it tries is Azure
   Speech, then Deepgram, then OpenAI Whisper only if an OpenAI key happens to be set, then the browser's own speech
   recognition. (The console converts the recording to 16 kHz WAV in the browser, which is what Azure's short-audio
   endpoint accepts.) "Always listening" doesn't mean always streaming to Azure/Deepgram/
   Whisper: Jarvis only wakes the paid microphone once it hears "Jarvis" (using the browser's own free wake-word
   spotting the rest of the time), and puts it back to sleep after a few seconds of silence. Wispr Flow and other dictation apps work straight into the chat
   box too.
10. **Marketing.** On the Settings page's "Google and socials" card: a Google Places key and Place ID (reviews),
    a Search Console service account (rankings), a Facebook Page token (Facebook/Instagram), and LinkedIn/TikTok
    tokens.

## Deploying to Azure

Follow **[infra/AZURE_SETUP.md](infra/AZURE_SETUP.md)**. It's written so you can do it yourself in Azure Cloud
Shell or hand it to Claude Desktop working in Chrome. In short:

```bash
git clone -b claude/jarvis-company-ai-assistant-gaj3mj https://github.com/alexanderclancy8-sketch/saltsAI.git
cd saltsAI
bash infra/deploy.sh
```

- **Its plan.** Azure charges per App Service plan, not per app, so Jarvis can run as a second web app on an
  existing *Linux* plan at no extra cost. It can't share a *Windows* plan (the script refuses one), because
  Jarvis needs Linux and Azure can't mix the two on one plan. Salts FSM is on Windows, so Jarvis gets its own
  Linux B1 plan (about £10 a month): run `PLAN= bash infra/deploy.sh`.
- **Never overwrites Salts FSM.** Everything Jarvis creates is tagged `app=jarvis`. The script and the GitHub
  workflow only upload to, or change settings on, a web app with that tag. Jarvis only ever deploys to Salts FSM
  when you approve a specific fix, and only after you've set up the auto-fix settings above.
- **Only management can open it.** Name the Microsoft 365 accounts allowed in (you and your business partner).
  Azure's built-in Microsoft sign-in then blocks everyone else before they reach Jarvis, and Jarvis checks the
  same list itself. Staff can still use the `/report` page with its key. Jarvis knows who is talking, so it
  greets your partner by name. You both share one Jarvis: the same conversation, display and approvals.
- **Always on.** It runs in Azure, so it doesn't depend on anyone's laptop.

Other commands: `bash infra/deploy.sh update` (new code, keeps settings), `bash infra/deploy.sh settings FILE`
(copy an env file into the app), `bash infra/deploy.sh signin EMAIL...` (set who can sign in; also renews the
2-year sign-in secret). Don't re-run the Bicep template by hand on a live app: it resets the app settings.

**Automatic deploys (optional):** once this is merged to `main`, set up the GitHub OIDC secrets described in
`.github/workflows/deploy-azure.yml` and set the `AZURE_WEBAPP_NAME` variable.

Run a single instance: the scheduler and the live display state live in the app. Data (the SQLite database,
uploads and register files) lives in `/home/data`, which survives restarts and redeploys.

## Safety, privacy and trust

- **Nothing happens without your approval.** Jarvis reads, checks, analyses, advises and suggests freely.
  Anything that *changes* something is queued as an approval card: emails (other than updates to you), invoices,
  purchase orders, review requests, stock movements and stocktakes, staff-register, accreditation and van/equipment-date edits,
  Salts FSM changes, Azure uploads, starting a code fix, and deployments. It only happens when you tap Approve or say
  "approve". There is no auto-deploy. The AI itself cannot approve anything, and nor can anything in an email,
  document or web page. The only things it does without asking are sending *you* the updates and reports you
  asked for, saving email *drafts* for you to review, and keeping its own notes - plus whatever *you* have
  switched on under standing approvals (next point).
- **The service@ inbox is read-only.** Jarvis only ever reads it (Comms, the email tools' `mailbox` choice, the council-request
  scan); it never sends from it, replies, marks, moves or deletes anything in it. Which address it reads is a setting only you can
  change - no tool, email or Teams message can point Jarvis at another mailbox - and engineers/office staff on the team console
  can't read it at all. Council requests only ever become *proposed* jobs for your approval, and no standing approval covers them.
- **The Companies House check is read-only public data.** `company_check` only reads a company's own register entry (status,
  filing dates, counts of insolvency history and charges). It never reads directors, officers or persons with significant control,
  stores nothing about an individual (only a six-hour cache of the company profile, keyed by company number), and a name is never
  trusted on its own: it only finds candidates and you confirm one by number. It never says whether a company is creditworthy.
  The key is a secret only you can set, and the team console cannot use the tool.
- **The Approvals inbox.** Everything Jarvis wants to send or change waits in the Approvals pop-up and shows up as a card
  in the chat, spelling out exactly what will happen (the real recipient, subject and message; the real change in Salts
  FSM), with **Approve**, **Edit** and **Don't send**. Nothing goes without a click. **Edit** (emails, Salts FSM changes
  and the details of most other actions) saves your changed version as a *new* waiting request - the old one can no
  longer be approved and the new one still needs your Approve. If an approved action fails, its card shows the error and
  **Retry**, which puts a fresh copy back in your queue (it never re-runs by itself), and **Dismiss** for a failure that
  needs nothing more (say, removing something that was never on the register): it hides the card and the red count, runs
  nothing, and keeps the action in the history under "Dismissed failures", marked with who dismissed it and when.
  **Dismiss all failed** does the same for the whole list, after asking. Secrets are masked on every card.
  Edit/Retry/Dismiss/Approve/Don't send only work from your signed-in console (and Approve/Deny from Teams).
- **What Jarvis did (Activity).** One read-only list of everything Jarvis drafted, emailed, proposed or changed and what a
  person decided, built from the records that already exist (the approvals, the scheduled checks, suggestions, engineering
  runs, memory, and a small audit line for each settings save, memory edit, team-code change and CSV export - the *names* of
  what changed, never a value, a code or a message). Everything shown is cleaned like the approval cards, and also has live
  secrets, map points and postcodes taken out. Owners and managers see it; the team console doesn't; only the owner can export
  it (a spreadsheet-safe CSV), and the engineer home-point audit lines are the owner's alone.
- **The Memory pop-up (Settings → Memory).** Lists what Jarvis has learned - "Things Jarvis should know", things he
  remembered himself, and the short replies he has learned - each one editable and deletable. A change is used from his very
  next message, and deleting a note also removes it from Settings so it can't come back on a restart.
- **Standing approvals (off by default, owner-only).** Settings → Standing approvals has two switches, which are
  your own approval given in advance for two narrow things, and nothing else:
  - *Record keeping*: creating a **new** customer, site or contact in Salts FSM, and adding a note, task or
    reminder (the `create_customer` / `create_site` tools, and `fsm_create_record` for contacts, notes, tasks and
    reminders). Never an edit, a delete, or anything touching jobs, quotes, invoices, prices or stock. A customer
    or site that deliberately shares an existing name (`confirmSharedName`) always waits for you.
  - *Routine acknowledgements*: when Jarvis has matched a customer's PO email to a quote you sent, it
    immediately emails that customer a fixed, receipt-only "we've received your purchase order" reply. It does not
    say a job is booked: accepting the quote and booking the job still wait for your approval, and that approval
    then sends a second "your job is booked" email. With this switch off, nothing is sent until you approve, as before.

  Everything else - money, deletions, job booking or scheduling, supplier orders, stock, other emails, code
  changes and deploys, accreditation, van/equipment-date, staff and settings edits - still queues for you. An automatic action goes
  through exactly the same path as an approved one (including the optional ThoughtProof check), is recorded as
  approved by "standing approval: record keeping" (or "...routine acknowledgements"), and is announced on the display
  and to your Teams approvers as "Done automatically (standing approval - ...)", with how to undo it. At most 20
  run automatically per rolling hour (adjustable, 0 turns them off); beyond that they wait for you and you get a
  warning. Text containing a link, angle brackets or control characters is never run automatically, and every
  automatically-written note, task or reminder starts "[Added automatically by Jarvis]" so staff can tell it
  wasn't typed by a person. Only the owner (display password, or a Microsoft sign-in matching the `OWNER_EMAIL`
  app setting) can change these switches - and the owner/partner email, display password and staff key, which
  decide who counts as the owner. Another signed-in manager can't, and Jarvis, a queued action or a Teams message
  never can. See `jarvis/services/standing_approvals.py`.
- **Learned reply suggestions (typed chat only).** Jarvis counts the short replies you type in the chat box
  ("yes", "yes do that") against the kind of thing it had just said (an offer, a question, something awaiting
  approval...). Once a reply has been used 3 times it is shown as a grey hint; Right Arrow at the end of the box
  copies it in, Enter sends as normal, Esc dismisses, ✕ forgets it. It only ever fills the box - it never sends
  anything or approves anything. Spoken messages and anything that looks sensitive (codes, numbers, emails, links,
  passwords) are never stored; it stays in Jarvis's own database. Turn it off on Settings → You and the business,
  or forget everything with `DELETE /api/reply-suggestions`. See `jarvis/services/reply_suggestions.py`.
- **Conversation quality records hold conversation text.** To spot slow, wrong or badly-spoken replies Jarvis keeps
  per-turn measurements in three tables of its own database (`turn_metrics`, `voice_events`, `turn_feedback`): timings,
  your Good/Wrong verdicts and notes, and a short excerpt (up to 300 characters, credentials and access codes
  redacted) of what you asked and what Jarvis replied. The full conversation is kept only in the transcript (2 years).
  These records are deleted after 90 days (Settings > Schedules, "Keep conversation quality records (days)",
  `CONVERSATION_QUALITY_RETENTION_DAYS`), checked at start-up and daily. Nothing in them leaves the system. To purge
  now, while signed in as the owner call `DELETE /api/quality` (everything) or `DELETE /api/quality?older_than_days=30`;
  or, with database access, `DELETE FROM turn_metrics; DELETE FROM voice_events; DELETE FROM turn_feedback;`. Jarvis
  himself has no tool to read these tables out or to purge them. See `jarvis/services/conversation_quality.py`.
- The auto-fix engineer can only read and edit a copy of the code, with no shell and no secrets, and every
  change goes through a pull request and CI.
- **Self-improvement is PR-only, always.** Jarvis can write changes to its own source and open a pull request,
  but it has no merge step and no deploy step at all - not even behind an approval click. A human always merges
  and redeploys it, through their own tooling, never Jarvis. The engineer is also instructed to refuse (and
  explain why, via `give_up`) any request that would weaken the approval gate, authentication or settings
  encryption, whatever the request says.
- **Staff monitoring:** tell staff in writing what is monitored and why (job data, timesheets, vehicle tracking
  during working hours, Microsoft 365 activity *counts*, never message content). This keeps you within UK GDPR
  and ICO employment guidance. Jarvis treats flags as prompts for a conversation, not verdicts.
- **Van locations outside working hours (Mon-Fri 07:00-18:30) are hidden by default.** Only the owner can change
  Settings > RAM Tracking > "Show van locations outside working hours" to *On-call only* (just the engineers on
  the on-call roster - ask Jarvis to add or remove periods; each change waits for approval) or *Always*. Keep your
  staff notice and contracts in line with whichever you choose. Every out-of-hours look-up (who asked, when, which
  tool, which engineer) is written to the `location_lookup_log` table and shown by the `location_lookup_log` tool;
  if the record can't be written, nothing is shown. Background jobs that don't name who is asking never see
  out-of-hours positions.
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

### Draft social media graphics (no image key needed)

Ask Jarvis for a Facebook, Instagram, LinkedIn or TikTok graphic and his own Claude designs the advert as a complete
HTML + CSS + inline-SVG page (Salts navy branding, your logo, your exact headline - he never rewords it). It appears on the
display in a sandboxed frame with **Download PNG** (made in your browser at the platform's exact size), **Download HTML**
and **Ask for changes** ("bigger headline, add 10% off" revises the same design). These are *designed graphics* - layout,
shapes, gradients, text and the logo - **not AI photographs**: Claude cannot generate photographs, and nothing here pretends
otherwise. Drafts only: nothing is ever posted. If an `IMAGE_API_KEY` for OpenAI is already set you can still choose OpenAI
picture backgrounds under Settings > Image generation (advanced); nothing needs it.
