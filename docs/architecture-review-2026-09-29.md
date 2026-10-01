# Architecture Review: Salts Jarvis

**Status:** Accepted (living document — describes the system as built, not a proposal)
**Date:** 2026-09-29
**Deciders:** Alex Clancy (owner/operator), Claude Code (ongoing implementation agent)
**Branch reviewed:** `jarvis-updates-2026-09-29` @ `c681cf7`

## Context

Jarvis is a custom AI assistant for Salts Fire and Security, a UK fire/security installer and maintainer. It is
a FastAPI backend with a single-page vanilla-JS "HUD" display (`jarvis/web/hud.js`, 1,250 lines), voice
input/output, and a Claude-based conversational brain that can read the business's live systems (job
management, accounts, vehicle tracking, email, socials) and take limited write actions behind a human approval
gate. It is deployed to one Azure App Service instance and run day to day by a single non-engineer owner, with
Claude Code sessions doing essentially all of the engineering.

**Maturity.** This is a working, deployed internal tool, not a prototype — it has 98 tools
(`jarvis/brain/tools.py`), 21 test files, two interchangeable LLM backends, a scheduler running 16 recurring
jobs, and a real deployment history (current HEAD is the 5th visible commit tightening SDK error handling, mic
behaviour, and CI). It is *not* yet architected for unattended, multi-week autonomous operation: every
consequential action still routes through a human clicking Approve on the display, which is by design (see
"Key Decisions Already Made" and "Consequences" below) rather than a gap to close.

**Who runs it.** One person (the owner) who is not a software engineer, plus a business partner and a small
number of managers who can sign in via Azure AD. The owner directs changes conversationally to Claude Code
sessions; there is no separate engineering team, no on-call rotation, and no staging environment. This shapes
several of the recommendations below: the right fix is often "make the failure visible on the HUD" rather than
"add SRE tooling" the owner has no one to operate.

## Current Architecture

**Composition root.** `jarvis/core.py`'s `Jarvis.__init__` constructs every integration and service exactly
once and hangs them off `self` (`j.fsm`, `j.actions`, `j.brain`, …). Order matters: `ActionExecutor` is built
before `Billing`/`Fixer`/`RegulatoryWatch`, which then get back-references patched in (`self.actions.billing =
self.billing`, `self.actions.j = self`, `self.issues.actions = self.actions`) because of circular dependencies
between them. `main.py`'s `create_app()` builds the FastAPI app around one `Jarvis` instance held in
`app.state.j`; there is exactly one live instance per process.

**Web layer.** `jarvis/main.py` (507 lines) exposes: session-cookie-authenticated REST endpoints for chat,
settings, approvals, issues, tests, briefings; a `/ws` WebSocket that streams `thinking`/`delta`/`tool`/`reply`
events from an `EventBus` and also accepts inbound `chat`/`stop`/`ping` messages (`jarvis/main.py:270-306`); an
`/api/chat/stream` SSE fallback that re-publishes the same bus events for when the socket is down
(`jarvis/main.py:152-176`); a `/ws/stt` relay to Deepgram; and webhook endpoints for Teams (Bot Framework,
bearer-token verified, `jarvis/main.py:404-432`) and staff issue reports (rate-limited by IP,
`jarvis/main.py:339-365`).

**Data flow for a chat turn:** browser → `/ws` → `JarvisBrain.ask()`/`MaxBrain.ask()` → Claude (tool-use loop) →
`dispatch()` in `jarvis/brain/tools.py` → either a live read against an integration (FSM, Sage, RAM Tracking,
Graph) or a queued row in `pending_actions` → `EventBus.publish()` → every subscribed WebSocket/SSE connection
renders the same event through `hud.js`'s `handle(ev)`.

**Settings.** Three layers, applied in order: `jarvis/config.py`'s `Settings` (env vars / `.env`, pydantic-
settings) is the base; `jarvis/settings_store.py`'s `SettingsStore` holds owner-edited overrides, Fernet-
encrypted at rest in `DATA_DIR/connections.enc` (key derived from `JARVIS_SECRET_KEY`,
`jarvis/settings_store.py:386`); `SettingsStore.apply()` writes the merged result back onto the live `Settings`
object, and `main.py:76-87`'s `reload_jarvis()` rebuilds the whole `Jarvis` instance in place after a settings
save — no redeploy needed. `SECTIONS` (`jarvis/settings_store.py:61-357`) is simultaneously the settings-page
schema, the validation rules, and the encryption boundary: adding a field to one `Section` makes it editable,
encrypted and hot-reloadable with no other plumbing.

**Storage.** `jarvis/db.py` is a single SQLite file (`DATA_DIR/jarvis.db`, `check_same_thread=False`, one
`threading.Lock` around every query — `jarvis/db.py:168-184`) holding only Jarvis's own state: issues,
notifications, test runs, memory, `pending_actions`, key/value, transcript, metrics, a stock ledger,
suggestions, action items, site access codes (encrypted ciphertext only), and automations. Sixteen tables, no
migrations framework (`CREATE TABLE IF NOT EXISTS` only — a schema change is additive-only unless someone
writes an ad hoc `ALTER TABLE`). Everything else — jobs, invoices, emails, vehicle positions — is read live from
its source system every time, normalised through alias tables (e.g. `jarvis/integrations/fsm.py`'s `ALIASES`
mapping `jobNumber`/`job_number`/`reference`/`number` onto one `ref` field) so small upstream API differences
don't ripple through the codebase.

**Two brains, one tool interface.** `jarvis/brain/agent.py`'s `JarvisBrain` runs a hand-rolled `anthropic.
AsyncAnthropic` tool-use loop (`_turn`, `jarvis/brain/agent.py:116-201`) with server-side prompt caching, JSON-
retry handling, and a `MAX_STEPS = 25` bound. `jarvis/brain/max_backend.py`'s `MaxBrain` instead runs the Claude
Agent SDK (Claude Code as a library) so usage comes out of the owner's Max/Pro subscription; it keeps one
persistent `ClaudeSDKClient` per `(effort, model, system)` combination via a single-worker asyncio queue
(`_run_worker`, `max_backend.py:186-203`) to avoid a cold start on every message, and exposes Jarvis's 98 tools
to Claude Code as an in-process MCP server (`build_mcp_server`) with Claude Code's own shell/file tools
explicitly disallowed (`BLOCKED = ["Bash", "Write", "Edit", "NotebookEdit", "KillShell", "Task"]`,
`max_backend.py:36`). `Settings.effective_llm_backend` (`config.py:257-260`) picks between them automatically:
`"max"` if `CLAUDE_CODE_OAUTH_TOKEN` is set, else `"api"`.

**Tools and the approval gate.** All 98 tools live in one `TOOLS` list in `jarvis/brain/tools.py`, each a
`Tool(name, description, pydantic_model, handler, label, approval, describe)`. `dispatch()`
(`tools.py:37-44`) is the single chokepoint both brains call through: `approval=True` tools (e.g. `email_send`,
`tools.py:1025-1027`) never run — they're serialised into a `pending_actions` row via `j.actions.queue()` and
only actually execute if the owner clicks Approve, which calls `ActionExecutor.approve()`
(`jarvis/services/actions.py:76-86`) and runs the deferred call in a bare `asyncio.create_task`. Some tools gate
themselves mid-handler instead (`log_job`, `stock_purchase_order`) after doing the safe prep work. Background-
job tools (`run_security_review`, `self_improve`, `create_automation`) need neither, because whatever they
eventually produce goes through its own gate later.

**Engineer-loop services** (`fixer.py`, `security_watch.py`, `self_improve.py`) share one shape: download a
tarball of a GitHub repo (`GitHub.download_tree`), wrap it in `jarvis/services/workspace.py`'s `Workspace` (a
path-confined virtual `/repo`), run a bounded Claude tool-call loop (`MAX_TURNS` 50-60, `cache_control` on the
system prompt) until a terminal tool is called, then act on the outcome. They differ exactly as designed:
`fixer.py` opens a PR and, only after owner approval of a `deploy_fix` action, merges and deploys to Azure
(`fixer.py:320-360`); `self_improve.py` opens a PR against Jarvis's own repo and stops — no merge/deploy call
exists anywhere in the file, by design (`self_improve.py:7-10`); `security_watch.py` never writes at all — its
editor tool wrapper (`_view_only`, `security_watch.py:94-98`) raises on anything but `view`. `recruiter.py`
generalises the same loop shape beyond code, running against Jarvis's own live tool set via `dispatch()`
(so any write it proposes still queues normally) rather than a code checkout, with `NO_RECURSE`
(`recruiter.py:23`) blocking a recruited agent from recruiting further agents or starting another background
job.

**Demo fallbacks.** Every optional external integration has a demo stand-in constructed in `core.py`'s
`__init__` based on a `configured` check: `DemoMail` vs `GraphMail` (`s.graph_configured`), `DemoRamTracking` vs
`RamTracking` (`s.ram_api_base_url and s.ram_api_key`), `CsvFinance`/demo vs `SageFinance`
(`build_finance`), `DemoFSM` inside `FSMRouter`. `SettingsStore.configured(section)` decides per-section on the
Settings page whether to show "connected" vs "DEMO data" (`settings_store.py:492-503`), with hand-written
exceptions for sections whose "configured" isn't a simple all-required-fields check (Claude: either key works;
self-improvement: either GitHub token works). This lets the whole app run and demo believably with zero setup.

**HUD transport.** `hud.js` connects a `WebSocket` at `/ws` with auto-reconnect (`hud.js:395-401`) and falls
back to `/api/chat/stream`'s SSE when it's down, deliberately shaped so each SSE line is the exact same
`{type, data}` event the socket already sends (`hud.js:345-364`) — `handle(ev)` (`hud.js:407+`) is the one
switch statement both transports render into.

**Knowledge base.** `jarvis/knowledge.py` is a from-scratch BM25 ranker (94 lines, no vector store, no external
dependency) over markdown files under `knowledge/`. `company/` and `fsm/` sub-folders are always injected whole
into the system prompt (`core_documents()`); everything else is chunked on `## ` headings and retrieved on
demand via `search()`.

**Deployment.** `infra/main.bicep` provisions one Linux App Service (`B1` SKU by default, `alwaysOn: true`,
`DATA_DIR=/home/data` on App Service's persistent storage) plus a storage account for the report archive.
`.github/workflows/ci.yml` runs `pytest` on every push/PR. `.github/workflows/deploy-azure.yml` is a real,
written CD workflow — OIDC login, a tag check that refuses to deploy over anything not tagged `app=jarvis`, zip
+ `azure/webapps-deploy` — but it is gated on `vars.AZURE_WEBAPP_NAME != ''`
(`deploy-azure.yml:20`) and that variable, along with the OIDC secrets it needs, is not configured. In practice
every deploy today is `bash infra/deploy.sh update` run by hand from Azure Cloud Shell
(`infra/deploy.sh:49-82`), which zips `jarvis/ knowledge/ requirements.txt *.yaml` and calls `az webapp deploy
--async`.

**`agentic_harness/`** is a separate, small (few hundred lines across `harness/state.py`, `tools.py`,
`approval.py`, `worker.py`, `orchestrator.py`) reference implementation of the same three patterns Jarvis
already runs in production — a ReAct tool loop, an orchestrator delegating to workers, and a human-in-the-loop
approval gate — built as a teaching artefact and explicitly *not* wired into `jarvis/` (its own README says so
and maps each piece onto its production equivalent: `Worker.run()` ↔ `JarvisBrain._turn()`, `ApprovalGate` ↔
`ActionExecutor`, `Orchestrator` ↔ `recruiter.py`/`self_improve.py`/`fixer.py`). It is useful as a clean
onboarding reference for a future engineer session, but nothing in it should be assumed to run, or to reflect
the current state of, the live app.

## Key Decisions Already Made

### 1. Single SQLite file for Jarvis's own state; everything else read live

**Chosen:** `jarvis/db.py` — one SQLite file, no ORM, hand-written SQL, holding only Jarvis-owned state.
**Alternative:** a real database server (Postgres) and/or caching/mirroring business data (jobs, invoices)
locally for speed and offline resilience.
**Why (inferred from CLAUDE.md and the code):** the business's real state already lives in FSM/Sage/RAM/Graph;
mirroring it locally creates a second source of truth that can drift, and at SME scale (a handful of staff, low
tens of jobs/day) there's no throughput problem live reads can't handle. SQLite needs no server to operate,
matching a solo non-engineer owner who cannot run infrastructure.
**Trade-off:** every chat turn that touches business data makes a live HTTP call to FSM/Sage/RAM — normal
latency is fine, but there is no cache to fall back on if one of those systems is briefly down (the `_safe()`
wrapper in `main.py:194-201` degrades that gracefully on the status page, but the assistant genuinely can't
answer the question in the meantime). SQLite's file-level lock (`db.py:170`, one `threading.Lock` shared by
every query) means Jarvis's own writes serialise under load, which is a non-issue at this scale but would not
scale past one process.

### 2. Two interchangeable LLM backends (plain API loop vs. Claude Agent SDK on the Max/Pro subscription)

**Chosen:** `JarvisBrain` (API) and `MaxBrain` (Agent SDK/subscription) behind one `ask()` interface, picked
automatically by whether a Max token is configured.
**Alternative:** pick one backend permanently, or make the owner choose once at setup and never revisit it.
**Why:** cost. Running everything through the API means paying per token; running through the owner's existing
Max/Pro subscription is effectively free marginal cost for the same capability. Keeping both means the owner
isn't locked out if the subscription lapses or API credits are preferred for some reason.
**Trade-off:** real, ongoing maintenance cost — CLAUDE.md is explicit that "anything added to the tool loop
generally needs to work through *both* backends," and every engineer-loop service (`fixer.py`, `security_
watch.py`, `self_improve.py`) carries a full second implementation (`_run_engineer_max`, `_review_max`,
`_engineer_max`) that must be kept behaviourally in sync with its API counterpart by hand — there is no shared
abstraction enforcing this, only convention and whoever notices in code review.

### 3. The approval gate as the one hard invariant (`ActionExecutor`)

**Chosen:** every write anywhere in the system — tool dispatch, `fixer.py`'s deploy step, invoice creation,
stock purchase orders — ends at `jarvis/services/actions.py`'s `ActionExecutor.queue()`, and nothing executes
until the owner clicks Approve on the display.
**Alternative:** trusted auto-execution for "safe" categories, rate-limited autonomy, or a tiered
approval/audit model.
**Why:** stated directly in the code's own docstring (`actions.py:1-7`): a malicious email or issue report must
not be able to talk Jarvis into sending mail or shipping code. Untrusted input (customer emails, staff issue
reports, web content) flows into the same conversation and tool loop as trusted owner instructions, so the only
reliable boundary is "nothing happens without a human clicking a button," not content-based filtering.
**This is a permanent invariant, not a piece of technical debt to be optimised away.** No recommendation below
proposes autonomous execution of any write path; several engineer-loop system prompts (e.g. `self_improve.py:
45-47`) explicitly instruct the model never to weaken this gate even if asked.

### 4. Settings hot-reload by rebuilding the whole `Jarvis` object

**Chosen:** `main.py:76-87`'s `reload_jarvis()` constructs an entirely new `Jarvis` instance (sharing the same
`Database`) and atomically swaps `app.state.j`, rather than mutating fields on the live instance.
**Alternative:** targeted field updates on each already-constructed integration/service.
**Why:** far simpler and more correct — `Jarvis.__init__` is the single place that knows how settings map to
constructed objects (which brain, which integration vs. demo stand-in), so re-running it guarantees the new
settings are fully and consistently applied, at the cost of a full reconstruction on every settings save.
**Trade-off:** `carry_conversation()` (`main.py:52-57`) has to manually thread the in-flight chat history across
the swap, and there's a real double-connection window before `old.stop()` runs (`main.py:80-86`) — acceptable
for a low-traffic single-owner tool, not for concurrent multi-user load.

### 5. Demo/fallback implementation for every optional integration

**Chosen:** every external system has a `Demo*` class that produces plausible fake data when not configured,
selected automatically in `core.py`'s constructor.
**Alternative:** fail loudly / show an empty state / refuse to start until configured.
**Why:** stated in CLAUDE.md — "the whole app runs believably with zero setup," which matters for a non-
engineer owner evaluating or gradually adopting the product, and for demos/screenshots.
**Trade-off:** a section that silently degrades to demo data (e.g. a lapsed API key that still passes the
"configured" check but fails at call time) could show plausible-looking numbers on the display without being
obviously fake — `_safe()` in `main.py` surfaces call *failures*, but a `configured() == True` section quietly
returning demo semantics rather than erroring is a narrower risk than it looks, since `demo` is fixed at
construction time from settings, not decided per-call.

## Trade-off Analysis

**Complexity vs. team reality.** The codebase is larger and more architecturally varied than a typical solo-
maintained internal tool (two full LLM backends, three near-identical engineer-loop services, 98 tools, 16
DB tables, its own settings framework) — but each piece of that variety is legible and grep-able by design:
one `TOOLS` list, one `SECTIONS` tuple, one `dispatch()` chokepoint, one composition root. That specific shape
— everything discoverable from a small number of entry points, `CLAUDE.md` documenting *why* each pattern
exists — is what makes it workable for a non-engineer owner directing Claude Code sessions rather than reading
the code directly: an agent (or the next Claude Code session) can hold the "where does X live" model quickly.
The real cost is the API/Max dual-implementation burden (Decision 2) and the near-duplication across the three
engineer-loop services, which is a deliberate copy-the-pattern choice (CLAUDE.md says as much) rather than an
oversight, trading DRY-ness for each service being readable start-to-finish in one file.

**Cost.** Marginal LLM cost is close to zero when the Max backend is active (Decision 2). Azure cost is one
B1 App Service (~£10-13/month) plus a Standard_LRS storage account for the report archive — inexpensive, but
also minimal headroom (see Scale & Reliability).

**Maintainability.** Strong in the places that matter most for this owner: adding a new setting is one `Field`
entry (CLAUDE.md, confirmed at `settings_store.py:61-357`); adding a tool is one `Tool(...)` entry in one list;
the demo-fallback convention means a half-configured integration degrades rather than crashing the app. Weaker
in the places a solo owner is least likely to notice: no schema migration tooling for `db.py` (additive-only),
no automated formatter/linter in CI (`ci.yml` only runs pytest — noted in CLAUDE.md itself), and the API/Max
parity burden above, which is exactly the kind of drift that's invisible until someone is on the backend that
wasn't exercised.

## Scale & Reliability

**Realistic load.** A small UK fire/security SME: a handful of office staff plus field engineers, at most a
few simultaneous HUD sessions (owner, partner, a couple of managers), tens of jobs a day through FSM, occasional
voice/chat bursts. Nothing in this system is anywhere near CPU- or memory-bound at that scale; SQLite under a
single lock (`db.py:170`) and live per-request calls to FSM/Sage/RAM are non-issues here specifically because
concurrency is low by construction, not because the design would hold up at higher scale.

**Single points of failure.**
- **One App Service instance, no staging slot** (`infra/main.bicep:48-54`, `skuName='B1'` by default, no scale-
  out configured). A deploy (`infra/deploy.sh update`) or a crash takes the whole app down with no fallback;
  `alwaysOn: true` and `healthCheckPath: /healthz` help App Service restart a crashed process, but there is no
  blue/green or canary path — every deploy is directly to production.
- **One SQLite file, no backup/snapshot strategy.** `DATA_DIR=/home/data` on Azure App Service's persistent
  storage (`main.bicep:79`, `WEBSITES_ENABLE_APP_SERVICE_STORAGE: 'true'`) means the file *does* survive
  restarts and redeploys — it is not lost on every deploy the way ephemeral `/tmp` would be — but nothing in
  the repo or bicep template takes a backup, snapshot, or point-in-time-recoverable copy of it. A corrupted
  file, a bad migration, or an accidental delete has no recovery path beyond whatever manual copy the owner
  happens to have made.
- **No CD path actually wired up.** `deploy-azure.yml` exists, is well-written (OIDC, a hard tag check refusing
  to overwrite a non-Jarvis app), and does nothing today because `AZURE_WEBAPP_NAME` isn't set as a repo
  variable and the OIDC secrets aren't configured. Every deploy is a manual `bash infra/deploy.sh update` run
  from Cloud Shell by whoever has access — no PR-gated release, no record of what was deployed when beyond git
  history and Azure's own deployment log.
- **One shared owner password / a short Azure-AD manager allowlist**, no per-user roles (`jarvis/auth.py`) —
  anyone with the password or on the `MANAGER_EMAILS` list has full access including approvals; there's no
  audit trail of *which* manager approved a given action beyond `speaker(request)`'s best-effort name lookup.

**What breaks first as usage grows.** In rough order: (1) the shared SQLite lock becomes a real bottleneck only
if Jarvis is driving genuinely concurrent write-heavy automation (not chat — chat is naturally serial per
owner); far more likely before that is (2) the in-process engineer-loop tasks (next section) simply losing work
silently on a routine App Service restart, and (3) the single B1 instance running out of headroom if voice
STT/TTS and multiple engineer-loop reviews overlap — there's no autoscale rule, so the fix today is manually
bumping to P0v3 (the bicep parameter already anticipates this: `main.bicep:12-13`).

## Risks & Technical Debt

1. **Background engineer-loop work has no persistence or recovery (concrete, verified).** `self_improve.start()`
   (`jarvis/services/self_improve.py:113-122`), `SecurityWatch.start()`
   (`jarvis/services/security_watch.py:116-123`), `Fixer._spawn`/`attempt`
   (`jarvis/services/fixer.py:112-186`), and `IssueService._spawn` (used by the `issue_fix` tool,
   `jarvis/brain/tools.py:961-965`) all launch bare `asyncio.create_task()` calls held only in an in-process
   `set[asyncio.Task]` on the owning service object. There is **no job-tracking table anywhere in
   `jarvis/db.py`'s 16-table schema** — no row is created when a review/fix/self-improve run starts, only when
   it finishes (an issue row, a PR, a notification). If the App Service process recycles mid-loop — which Azure
   does routinely for deploys, and can do for platform maintenance — the task is simply gone: no error, no
   partial result, no retry, and no visible record that anything was ever running. The owner's only signal is
   the *absence* of an eventual notification, which is indistinguishable from "nothing was asked of it." The
   scheduler-driven equivalents (`security_watch.run` on a weekly cron, `scheduler.py:55-57`) are lower-risk
   because they simply run again next cycle, but a chat-triggered `self_improve` or `issue_fix` request has no
   such second chance.
2. **API/Max backend parity is enforced only by convention.** Nothing fails CI if `_engineer_max` and the API
   `_engineer` loop diverge in behaviour (e.g. a new safety instruction added to `SELF_IMPROVE_SYSTEM` but not
   propagated to the Max variant's string-replaced version at `self_improve.py:223-229`, which is already a
   fragile string-substitution rather than a shared template). This is the single most likely place a future
   change silently only half-applies.
3. **No DB migration tooling.** `db.py`'s `SCHEMA` is `CREATE TABLE IF NOT EXISTS` only (`db.py:16-156`);
   changing an existing column's type or adding a `NOT NULL` constraint to existing data has no supported path
   and would need a hand-written one-off script, easy to forget under time pressure.
4. **No automated linting/formatting in CI** (`.github/workflows/ci.yml:1-18` runs only `pytest`), acknowledged
   in `CLAUDE.md` itself ("match the surrounding code's style by eye") — fine today with one contributor
   (Claude Code sessions), but style drift compounds with no enforcement mechanism.
5. **Deploy has no rollback mechanism beyond re-deploying an older commit.** `infra/deploy.sh update` overwrites
   the running app directly; there's no kept artifact of the previous zip and no staging slot to swap back to
   if a deploy is bad — recovery means finding the last-good commit and re-running the deploy script.
6. **`deploy-azure.yml` is unmaintained-by-neglect, not unmaintained-by-choice**, which is worth flagging
   precisely because it looks finished: a reader skimming `.github/workflows/` would reasonably assume CD is
   live. It is real, tested-shaped code sitting inert behind one unset repository variable.
7. **Single shared credential model** (`jarvis/auth.py`) — one owner password plus a flat Azure-AD manager
   list, no per-manager scoping of what they can approve or see. At current team size this is a reasonable
   trade for simplicity, but it will not extend gracefully if the manager list grows.

## Consequences

**Easy now:** adding a new setting, a new tool, a new demo-fallback integration, or a new scheduled job — each
follows one well-worn, documented pattern with a single point of registration. Settings changes apply without a
redeploy. A non-engineer owner can meaningfully direct changes because the codebase's own `CLAUDE.md` encodes
the "why" behind each pattern for whichever Claude Code session picks up the next request.

**Hard now:** verifying that a change to the tool loop, the system prompt, or an engineer-loop's safety
instructions has been correctly mirrored across both the API and Max backends — this depends entirely on the
implementer remembering to check, with no test or lint catching a missed spot. Recovering a lost background
engineer-loop run, or a corrupted `jarvis.db`, is also hard today: neither has a supported recovery path.

**To revisit:**
- Whether `deploy-azure.yml` should be finished (repo variable + OIDC secrets) or deliberately removed if
  manual deploys via Cloud Shell remain the intended long-term path — right now it's neither.
- Whether background engineer-loop tasks need even minimal DB-row tracking (started/finished/lost) before the
  system is trusted with more of them running unattended, independent of and prior to any question of loosening
  the approval gate itself (which should not be revisited).
- SQLite's suitability if Jarvis ever needs true concurrent multi-writer throughput — not indicated by anything
  in current usage patterns, but worth a conscious check-in rather than an unnoticed slide into contention.

## Recommendations

Prioritised for a solo non-engineer owner working through Claude Code sessions — cheapest, highest-value first;
nothing here proposes loosening the approval gate.

1. **Add a minimal `background_jobs` table** (kind, started_at, status, finished_at, result summary) written at
   the *start* of `self_improve.start()`, `SecurityWatch.start()`, `Fixer.attempt()`, and `issue_fix`'s spawn —
   not to add retry/recovery logic (out of scope for a solo owner to operate), just so a process recycle leaves
   a visible "this was running and never finished" row instead of silence. Surface stuck rows (status still
   "running" after, say, 20 minutes) as a HUD notification. This is the single highest-value fix given how
   invisible the current failure mode is.
2. **Either finish or explicitly retire `deploy-azure.yml`.** Finishing means setting `AZURE_WEBAPP_NAME` as a
   repo variable and configuring the OIDC federated credential per the workflow's own header comment
   (`deploy-azure.yml:2-6`) — genuinely low effort since the workflow logic is already written and tag-guarded.
   Retiring means deleting it or adding a comment explaining manual deploy is the deliberate choice, so a future
   reader isn't misled.
3. **Take a scheduled backup of `jarvis.db`.** Given the persistent-storage setup already in place
   (`WEBSITES_ENABLE_APP_SERVICE_STORAGE`), the cheapest version is a small scheduled job (reuse the existing
   `AutomationService`/scheduler machinery, or Azure's own App Service backup feature) that copies `jarvis.db`
   into the already-provisioned blob storage account daily. Low effort, directly closes the one true data-loss
   gap identified above.
4. **Add a lightweight parity check between the API and Max engineer-loop variants** — even just a test that
   asserts both `_engineer`/`_engineer_max` (and the security/self-improve equivalents) are invoked with system
   prompts containing the same safety-critical substrings (e.g. "never weaken... the approval gate") would catch
   the most dangerous class of silent drift without requiring a deeper refactor.
5. **When there's appetite for a bigger change,** consider extracting the shared engineer-loop shape (tarball
   checkout → Workspace → bounded tool loop → terminal tool) that `fixer.py`, `security_watch.py`, and
   `self_improve.py` currently each hand-roll, into one parameterised helper — but only after (4) above,
   because collapsing three files that are each "readable start to finish" (a property CLAUDE.md explicitly
   values) into a shared abstraction is a real trade-off against the codebase's current legibility, not a pure
   win, and should be a deliberate call rather than opportunistic cleanup.
6. **Not recommended:** do not weaken, bypass, or add an autonomy tier around `ActionExecutor`. Every
   recommendation above is about making failures *visible*, never about letting more things happen without the
   owner's click.
