# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Jarvis: a custom AI assistant for Salts Fire and Security (a UK fire/security installer/maintainer). FastAPI
backend + a browser HUD + voice, brain is Claude (either the API or the owner's Claude Max/Pro subscription via
the Claude Agent SDK), deployed to Azure App Service. See README.md for the full feature list and setup - it's
kept current and is the best starting point for "what does Jarvis do".

## Commands

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

python -m jarvis                       # run locally: http://localhost:8000 (demo data with no .env configured)

python -m pytest -q                    # full suite (what CI runs)
python -m pytest -q tests/test_agent_and_api.py            # one file
python -m pytest -q tests/test_agent_and_api.py::test_name # one test
```

There is no linter/formatter wired into CI (`.github/workflows/ci.yml` just installs `requirements-dev.txt` and
runs pytest) - match the surrounding code's style by eye.

`bash infra/deploy.sh <command>` drives Azure setup/deploys from Cloud Shell (App Service, Microsoft 365, Teams
bot, voice, etc.) - see README.md and the script's own usage comment for the full command list.

## Architecture

**`jarvis/core.py`'s `Jarvis` class is the composition root.** Every integration and service is constructed
once in `Jarvis.__init__` and hung off `self` (e.g. `j.fsm`, `j.stores`, `j.actions`, `j.brain`). Nearly every
service takes either specific dependencies or, for the newer/simpler ones, the whole `Jarvis` instance as `j`
and reaches into it (`self.j.db`, `self.j.notifier`, ...) - both patterns coexist; follow whichever the
neighbouring services in `services/` already use. `main.py`'s `create_app(settings, jarvis=None)` builds the
FastAPI app around a `Jarvis` instance (building one itself if not given); tests instead construct their own
`Jarvis(settings, client=FakeClient(...))` and pass it in, to inject a fake Claude client.

**Two interchangeable brains, one tool set.** `jarvis/brain/agent.py`'s `JarvisBrain` (plain Anthropic API,
`anthropic.AsyncAnthropic`, real tool-use loop you can read start to finish in `_turn`) and
`jarvis/brain/max_backend.py`'s `MaxBrain` (routes through the Claude Agent SDK / Claude Code so usage comes out
of the owner's Max/Pro subscription instead of API credits, keeping one persistent `ClaudeSDKClient` per
`(effort, model, system)` combination to avoid a cold start on every message) both implement the same
`ask(text, mode, attachments, speaker)` interface and are picked by `Settings.effective_llm_backend`
(`claude_code_oauth_token` set → `"max"`, else `"api"`). Anything added to the tool loop generally needs to work
through *both* backends - `fixer.py`, `security_watch.py` and `self_improve.py` each have a `_max` variant of
their engineer/review loop for exactly this reason (Claude Code's own Read/Edit/Glob/Grep instead of the
hand-rolled tool loop + `str_replace_based_edit_tool`/`grep`/`find_files`).

**Tools live in one file: `jarvis/brain/tools.py`.** Each is a `Tool(name, description, pydantic_input_model,
async_handler, label, approval=False, describe=None)` in the `TOOLS` list. `dispatch()` is the single chokepoint
both brains call through: if `approval=True` the call is queued as a `pending_actions` row and never actually
runs until the owner clicks Approve on the HUD; otherwise the handler runs immediately. Two ways a tool can be
gated:
- `approval=True` on the `Tool` itself - the safest default for anything that writes somewhere (`fsm_change`,
  `stock_move`, `accreditation_update`, ...).
- The handler does the safe read/prep work itself and then calls `j.actions.queue(kind, summary, payload)`
  explicitly before returning (`log_job`, `log_purchase_order`, `stock_purchase_order`) - used when most of the
  tool's work (resolving items, pricing, building the email/body) is safe and only the final write needs a
  human's eyes on it.
Background-job-style tools (`run_security_review`, `self_improve`, `create_automation`) need neither: they just
kick off `asyncio.create_task` and return immediately, since the thing they eventually *do* goes through its own
approval gate later (or, for `self_improve`, is human-merged on GitHub, never by Jarvis).

**Nothing changes anything without the owner's approval - this is the one rule everything else bends around.**
`jarvis/services/actions.py`'s `ActionExecutor.queue()`/`approve()`/`deny()` is the single gate; every write path
in the codebase (tool dispatch, `fixer.py`'s deploy step, `stock_purchase_order`, invoice creation) ends at a
`pending_actions` row rather than acting directly. When adding a new capability that changes something, use this
gate rather than inventing a new confirmation mechanism.

*Who can approve.* Only humans: the display (`/api/approvals/...`, owner-authenticated), and the owner/partner/managers
on Microsoft Teams - `services/teams_approvals.py` sends each approver who has said hello an Adaptive Card when
`queue()` runs, and `main.teams_messages` handles the button press (and the typed `approve 12` / `deny 12`) BEFORE and
separate from the brain: JWT check -> sender email -> `approver_emails()` allowlist -> `actions.approve/deny` directly.
No brain tool can approve, and `tests/test_standing_approvals.py` greps the code to keep it that way (only `main.py`
may call `approve()`/`deny()`).

*Approvals inbox, Edit and Retry (console Phase 4a).* The Approvals pop-up and the chat both draw ONE card per action from
`GET /api/approvals/inbox` (`services/approval_inbox.view()`: built from the stored payload, every string redacted; pending,
failed + retryable, recently decided). Edit and Retry are `ActionExecutor.edit()` / `.retry()`, called only from
`main.py`'s `/api/approvals/{id}/edit` and `/retry` (owner session + `auth.require_same_origin`, which also now guards
approve/deny and the memory endpoints). Neither ever runs anything or consults standing approvals, and neither changes a
stored payload: *Edit* validates the change (`approval_inbox.apply_edit` - a CLOSED list: `email_send` / `tool:email_send`
to, cc, subject, body; `fsm_write` body only; other `tool:*` args re-validated by the tool's own model; money, bookings and
deploys not editable), then in ONE transaction closes the old action as denied-by-edit and inserts the edited payload as a
NEW plain `pending` action (`db.supersede_pending_action`), so what is approved is exactly what was on the card and the old
card can no longer be approved. *Retry* of a `failed` action inserts a copy of its stored kind and payload as a NEW pending
action once (`db.retry_failed_action`; the failed row stays as history, marked `superseded_by`); it re-enters the normal
approval path and needs its own Approve click. `tests/test_approvals_inbox.py` pins all of this, including that no tool,
service or standing path can call them. *Dismiss* (`ActionExecutor.dismiss()` / `.dismiss_many()`, called only from
`main.py`'s `POST /api/approvals/{id}/dismiss` and `/api/approvals/dismiss-failed` - owner session + same-origin, routes
classified `MANAGER_OK`, so a team session gets 403) is a human putting a FAILED action away: it runs, queues and retries
nothing and consults no standing approval. `db.dismiss_failed_action` writes only `dismissed_at` / `dismissed_by` (the row
stays `failed`, payload, error and `superseded_by` untouched), once, and only for a failed row (anything else is 409;
repeating it is a harmless 200 `already`; an already-retried failure can be dismissed; a dismissed one can no longer be
retried). Dismissed rows drop out of `db.failed_actions`, `recent_decided_actions`, the rail count, "Needs you" and the chat
cards (the inbox returns their ids as `dismissed_ids` so a chat card is removed), and stay in `GET /api/approvals/history`
(`dismissed_label`: "dismissed by NAME at TIME") and the "Dismissed failures" list in the pop-up. "Dismiss all failed"
sends exactly the ids on screen after a confirm that says how many. Failed actions are not offered on Teams cards, so Teams
is unchanged. `tests/test_dismiss_failed.py` and `tests/test_console_browser_dismiss.py` pin it. When you add an action kind, decide deliberately whether it belongs in
`approval_inbox.editable_fields` (default: no).

*What Jarvis did (`services/activity_feed.py`, `j.activity_feed`; routes `GET /api/activity` and `GET /api/activity/export.csv`; tool
`what_did_you_do`; `web/activity.js`; tests `tests/test_activity_feed.py`, `tests/test_console_browser_activity.py`).* ONE read-only
model that UNIONS every record of something Jarvis proposed, prepared or changed - it keeps no copy of any of them and approves, declines,
sends, retries, edits or changes nothing (a test greps the module for those verbs, and for the only table it writes). Item shape: `{id, when
(UTC), kind, what, status, who, requested_by, decided_by, decided_at, created_at, source, source_ref, detail (rows, redacted), error,
link, quiet, attention, auto, chain, sample}`. Kinds: draft | email | job_proposal | fsm_change | settings_change | memory | code_change
(PR) | scheduled_check | suggestion | other. Statuses: waiting, approved, declined, done, failed, dismissed, edited, auto_approved
("Auto-approved (standing)"), plus running (a background run, a pull-request run in flight). `Needs a look` is a filter, not a status:
failed + waiting that are still open (a failure already retried or dismissed is not).
- **Sources and how each maps** (add a source = one `_src_*` function and one `_Source(...)` entry in `SOURCES`):
  `pending_actions` (the one approvals record: status pending/approved/done/failed/denied + `approved_by`, `superseded_by`/`supersedes`/
  `supersede_kind`, `dismissed_*`; `when` = `COALESCE(decided_at, created_at)`; an email action is a *draft* until it is approved/sent, then an
  *email*; `fsm_write` POST `/jobs`, `accept_quote*` and `log_job` are *job proposals*; `approved_by = "standing approval: <category>"` is
  `auto_approved`, and still `failed` if it failed; denied-by-edit is `edited`; denied with "Blocked by the security check" is `failed`),
  `check_runs` (changed/failed are rows; no_change/baseline are `quiet` and counted in SQL into the collapsed line, never read row by row),
  `suggestions` (an open one is as old as `created_at` - `updated_at` is bumped on every refresh), `agent_runs` (pull-request runs; a run that
  gave up is quiet; a "running" one with no step for 30 minutes shows failed), `background_calls` (failed ones only, unless "everything"),
  `memory`, `audit_events` (new, below), `documents`, `adverts`, the kv `upsell:done:*` markers whose state is `improved`, and `automations`.
  The engineer-home audit lines are the `check_runs` rows of the owner-only job (`activity.OWNER_ONLY_JOBS`): the module never names that table,
  shows them to the principal owner only (`Query.owner`) and cleans postcodes and points out of them.
- **What had no actor or time, and what was added (additive, cheap):** `audit_events(id, at, kind, actor, what, ref)` is written by
  `ActivityFeed.record()` for a settings save (the *labels* of the settings that really changed - "Record keeping (on)" for a switch - never a
  value; `main.save_settings`), the team access code set/cleared (never the code), a memory fact or learned reply reworded or removed (the number, never
  the text; also the `forget` tool) and a CSV export. `check_runs` keeps runs that found something or failed for 31 days (quiet ones still 7;
  `db.prune_check_runs(before, changed_before)`), so a 30-day view has them. Indexes: `idx_actions_when` (the very expression actions are
  ordered by), `idx_check_runs_at`, `idx_audit_events_at`. Limits of the data, not of this module: the requester of an action is "Jarvis" unless a
  team member asked (`payload.requested_by`) - the store does not say whether a chat turn or a scheduled job queued it; who *dismissed a suggestion*
  is not stored; the time of an approved action is when it finished (`set_action_status` rewrites `decided_at`).
- **Paging and limits:** `limit` 1-200 (default 50) and `offset`; each source is read newest-first with `LIMIT` and never more than `SCAN_CAP` (3000)
  rows; the merge reaches at most `REACH_CAP` (3000) items deep and then says so (`capped`); a filter is applied in Python on the mapped item, so the
  per-source read is widened (x4) until the page is full or the cap is hit; sources a kind filter cannot match are not read; the first page also
  carries the day's summary, the quiet line and the "who" choices (offset > 0 does not). CSV: at most `EXPORT_MAX` (5000) rows, `X-Export-Truncated`
  says when it was cut, a BOM for Excel, and every cell that starts with `= + - @` (even after spaces) or a tab/return gets a leading apostrophe.
- **Redaction (`Cleaner`) for every string:** the approval cards' own redaction (`approval_inbox.clean`: `integrations.redact` + `redact.redact_text`),
  spoken access codes (`history.redact_history`), `token=` / `password:` written into text, the LIVE secret values from Settings (every
  `secret` field plus the staff report key and display password), payload KEYS that name a secret (`redact.is_sensitive_param`: `code`, `password`,
  `pin`, `token`, `*_key`...), location keys dropped, coordinate pairs and UK postcodes (`HIDE_POSTCODES`; a site postcode is on the approval card
  itself, not here). The access-code tool's details are withheld entirely. Items rooted in sample data (`sample`: the demo FSM, demo mailbox, demo accounts/
  stock/staff) are flagged in the console and left out of the spoken answer.
- **Access:** `GET /api/activity` is `MANAGER_OK` (a team session gets 403), `GET /api/activity/export.csv` is `OWNER_ONLY` (a manager gets 403), both with
  `human_click` (a cross-site request is refused). `FEATURES[...]["activity"]` / `["activity_export"]` say which role has what.
- **Console:** a rail item **Activity** (second, after Approvals; manager region, no count) opens the `#pop-activity` drawer; "See everything Jarvis did" is also
  at the foot of Approvals, and the chat's "Open ..." chip can point at it (`trace.PANELS`). Summary line, Today / 7 / 30 day chips, Kind / Status / Who selects,
  search, "Include everything" toggle, the collapsed quiet-checks line, rows that expand to the cleaned detail, "Show more" (or scrolling to the end), and
  Export CSV (owner only). The only action in it is the ordinary drawer switch to Approvals.
- **Voice tool `what_did_you_do`** (`when`: today | yesterday | 7d | 30d; read-only, `approval=False`, **not** in `TEAM_TOOLS`, in
  `async_tools.UNTRUSTED_TOOLS`): counts first, then up to five notable things (failed ones, then waiting ones, then the newest), "And N more.", the
  place to look, and "I've left out N items that only involved sample data" when `sample_sources()` says so. Short plain descriptions only: links are
  stripped, nothing secret, no owner-only lines.

*Memory pop-up.* `services/memory_book.py` + `/api/memory...` (owner + same-origin, console only - deliberately not a brain
tool) list/reword/delete the `memory` table, the `jarvis_notes` setting lines (rewritten through the SettingsStore too, or
`Jarvis._seed_notes` would put them back on the next start) and learned replies (`reply_habits`, by id), then
`brain.refresh_system()` so the next turn reads the change. Tests: `tests/test_memory_popup.py`.

*Full read access to the FSM (`integrations/fsm_data.py`, `services/fsm_read.py`, `services/fsm_assets.py`; `j.fsm_data`, `j.fsm_read`; tests
`tests/test_fsm_data_client.py`, `tests/test_fsm_data_tools.py`, `tests/test_fsm_assets.py`).* "Jarvis is the brains of the FSM": it can READ every
module of the FSM, including finance and staff pay / HR, through the FSM's generic read-only data API. Read-only, GET only (a test greps
the modules for any other verb), and nothing here touches `actions.queue/approve` or the approval gate.
- **Contract (Jarvis -> FSM, the existing FSM key via `FSMClient.jarvis_call`, absolute paths under the FSM base URL):**
  `GET /api/jarvis/catalog` -> `{version, groups: {group: {enabled, description}}, resources: [{name, group, description, fields:
  [{name, type, description}], filters: [field names], sensitive}]}`; `GET /api/jarvis/data/{resource}` with `filter[field]=v` (also
  `filter[field][gte]` / `[lte]`), `q`, `updated_since` (ISO), `fields=a,b`, `limit` (default 100, max 500), `offset`, `order=field|-field`
  -> `{resource, items, total, next_offset (null at the end), truncated}`. Errors: 401 no/bad key, 403 `{error: "scope_off", group}`, 404
  unknown resource, 422 bad field/filter, 429 + Retry-After; writes answer 405. Groups: operations, customers_sites, assets, compliance,
  commercial, finance, people, comms, audit. Login material and raw card/bank numbers are never exposed by the FSM.
- **Client (`FsmData`):** catalog cached 10 minutes and replaced when its `version` changes (`on_change` rebuilds the system prompt); a
  query the FSM refuses as unknown/scope-off heals the cache. `fetch()` follows `next_offset` to a hard cap (500 rows per model call, 5000
  for internal jobs, 20 pages) and reports `truncated` + `next_offset`. 3 requests at once, 20 s timeout, 429 `Retry-After` slept if short
  (<=10 s) else reported and remembered. Every error is a plain `FsmDataError` (kind: demo, unavailable, unauthorized, scope_off,
  forbidden, not_found, bad_request, rate_limited, server, network, bad_response) with no URL, key or row in it. **A 404/405 on the catalog
  means the FSM has not shipped the API: it backs off 5 minutes to an hour, logs ONE warning per outage, and says "the FSM doesn't expose
  this yet"; an outage backs off 1 to 15 minutes the same way.** Rows are never logged. ALL returned text is untrusted: control and
  zero-width characters stripped, HTML tags removed, whitespace collapsed, secret-looking strings through `integrations/redact.redact`, strings
  capped at 500 chars, nesting flattened, a value under a credential-named key blanked.
- **Tools (read-only, `approval=False`, not in `TEAM_TOOLS`, `fsm_` prefix = untrusted output, `fsm_data` in `NOT_BACKGROUND`):**
  `fsm_catalog(group?, resource?)` (groups, resources and field names, compact; falls back to names only with a hint when huge) and
  `fsm_data(resource, filters, q, fields, order, updated_since, limit, offset)`, validated against the cached catalog - an unknown
  resource, field, filter or order is answered with the nearest valid names (nothing is sent to the FSM). The result is capped at 30k chars
  (`RESULT_CHARS`) with a "narrow your filters" hint and the `next_offset` to carry on from. The system prompt gets a SHORT generated block
  (`FsmRead.prompt_block()`: group -> resource NAMES only, `*` = owner only), and `connections()` an "FSM data (read-only)" line.
- **Who may hear what:** a resource the FSM flags `sensitive`, plus EVERY resource of the `finance` and `people` groups whatever the flag
  says (default deny), is **owner-only** - the principal owner, the owner's own conversation, the owner's own automations and Jarvis himself's system jobs (no caller; a MANAGER's automation runs as a manager, see below).
  A **manager** gets every other resource (and sees that owner-only ones exist, never their fields). A **team** session gets neither tool
  (default-deny `TEAM_TOOLS`; the team tests stay strict). A refusal is recorded and nothing is fetched. Scope-off groups are reported by name.
- **A manager's turn is marked, and so is everything a manager stores for later (`access.current_caller`, tests `tests/test_automation_roles.py`).**
  Managers share the owner's brain, so `main.mark_manager` sets `access.current_caller` to the manager's `Caller` for chat, stream, WebSocket and
  Teams chat turns (the owner's turn stays unmarked = None; MaxBrain's worker carries it in the `("ask", ...)` tuple). Role-dependent tools look in
  that ONE place (`FsmRead.may_read_sensitive`, `fleet_diagnostics`). **Stored work must run as whoever created it**, or a manager could use it
  to borrow the owner's context: an automation / a background call / a queued approval / a self-improvement run / the self-reflection each record
  the creator's role (`access.role_of(caller)`: None = owner) and the run re-creates that caller in `current_caller` (`access.caller_for_role`;
  always SET, even to None, so the context of whatever turn is running when the scheduler fires lends it nothing).
  - **Automations** (`automations.role` `owner|manager|team` NOT NULL DEFAULT `'manager'`, `created_by` = the creator's name): `AutomationService.create`
    reads the marker; `run()` sets the creator's caller around `brain.ask` (so MaxBrain's worker and the tool layer see it) and adds a line to the
    prompt saying so. A manager's automation is refused `fsm_data` finance / pay / HR and `fleet_diagnostics` with the same plain messages as in
    chat, whoever's turn or click fires it, and its finding is told in the console only (`Proactive.tell(..., teams=False)`; titled "(set up by a
    manager)"), never pushed on to Teams. `team` can never create one (`create_automation`/`edit_automation` are not in `TEAM_TOOLS`, `create()` refuses,
    and a row marked team is not run: "Not run: ..."). **Migration (`Database._migrate`, every start):** nothing recorded who created the rows that
    existed, so none is provably the owner's - `ADD COLUMN ... DEFAULT 'manager'` backfills them all (least privilege) and an UPDATE repairs any
    empty / unknown value to `manager`; it never touches a valid role. The owner takes an old one over on purpose: `edit_automation(take_over=True)`
    (owner only; the role is a column `db.update_automation` refuses to write - only `set_automation_role`). **Editing never raises a role:**
    `edit_automation` by a lower role of a higher role's automation is refused (so is delete / `set_automation_options`); by the same or a higher
    role it keeps the role (the owner rewording a manager's automation does not give the new wording the owner's access). `list_automations` shows
    `created_by_role` / `created_by` and hides a higher role's prompt and last result from a manager. Activity: the automation row says "Jarvis
    (created by a manager (Sam))" with "Created by" / "Runs with" rows, and its runs "Scheduled job (created by a manager)".
  - **Background calls** (`background_calls.role` was already there): now `owner` for the owner's own turn (was `''`; legacy rows stay `''`), `manager`,
    `team`; the call runs through `dispatch(..., caller=)` so it is the requester's; `fsm_data` / `fleet_diagnostics` stay in `NOT_BACKGROUND`.
  - **Approvals** (`pending_actions.requested_role`, `''` for old rows): `ActionExecutor.queue` records it; an approved `tool:` action runs its handler with
    the REQUESTER's caller (a legacy `''` row runs as a manager), not the approver's click. Edit and Retry copy the original's role. Approving, standing
    approvals and what they cover are untouched: nothing in this feature approves anything.
  - **Self-improvement** (`agent_runs.requested_role`): the run, its engineer brief and the pull request name who asked ("a manager (Sam)"), the brief
    lists `jarvis/access.py` and the owner-only checks among what a request can never weaken; Activity shows "Jarvis (asked by a manager)".
  - **Self-reflection** (`services/self_learning.py`) reads the shared transcript, which holds what managers typed, so it runs as `access.REFLECTION_CALLER` (a manager).
  - Not changed: suggestions' "Do it" is just a chat message typed by the clicker (so already their role); the live console (chat bus, transcript, Activity
    check lines) is ONE shared surface for the owner and managers - an owner's automation's finding is visible to a manager there, as the owner's chat always was.
- **Where the data may NOT go:** fsm_ tool output is untrusted, so chat/transcript/proactive text only ever gets a "finished" pointer, never
  rows; `fsm_data` cannot run in the background (the `background_calls` table would keep finance/pay/HR rows for 30 days); the `remember`
  tool (which self-learning also uses) refuses any fact that repeats a figure, date, id or long note from a sensitive read in the last hour
  (`FsmRead.contains_sensitive`, in memory only); "What Jarvis did" records `fsm_read` audit lines = resource name + row count + who, never a
  value. FSM demo data: both tools say so and return nothing.
*Number-crunching and charts, without running model-written code (`services/calc.py`, `services/fsm_analyse.py`, `services/charts.py`,
`web/charts.js` + `charts.css`; tests `tests/test_calc.py`, `tests/test_fsm_analyse.py`, `tests/test_charts.py`, `tests/test_charts_browser.py`).* Jarvis
used to do arithmetic "in its head" and answer only in sentences. Three read-only, `approval=False` tools fix that WITHOUT an interpreter: nothing the
model writes is ever executed - it picks from closed vocabularies and code does the sums. None of them is in `TEAM_TOOLS`; `fsm_analyse` and
`show_chart` are in `NOT_BACKGROUND` (the first reads finance / pay / HR, both publish to the display) and `fsm_analyse` is `fsm_`-prefixed = untrusted output.
- **`calculate(expression, values?)`** (`services/calc.py`): `ast.parse` + an evaluator that knows only numbers, `+ - * / // % **`, unary +/-, brackets,
  `round abs min max sum avg pct pct_change sqrt` (`min max sum avg` also take `[a, b]` list literals) and NAMES only from the `values` dict (letters/digits/_,
  starting with a letter, <= 40 chars). Everything else (attributes, subscripts, other calls, comprehensions, lambda, strings, comparisons, keyword args...) is
  refused with a sentence. `Decimal` (34 digits) throughout; the answer is rounded to 2 dp half-to-even with the unrounded `exact` value, the expression
  echoed and a rounding note. Caps: 500 chars, 200 nodes, depth 100, |exponent| <= 100 (so `9**9**9` is refused before anything is computed), numbers
  <= 1e60, lists <= 200 items. Division by zero says which operator. If its inputs repeat a figure from owner-only data (`contains_sensitive`) the result
  is itself noted as owner-only so `remember` refuses it.
- **`fsm_analyse(resource, filters, period, group_by, metrics, having, order, limit, chart, chart_metric, chart_title, display, sample_rows)`**: aggregates
  over the FSM read API page by page (`FsmData.scan`, an async generator - rows are never all held): metrics `count count(f) sum avg min max median distinct
  pct_of_total[(f)]` (money in `Decimal`, 2 dp; `value`/`total`/`amount`... names and money-typed fields format as £), `group_by` up to two fields with date
  buckets `field:day|week|month|quarter|year` (the calendar date AS WRITTEN, no time-zone conversion), `period` = a named range (`this_year`, `last_month`,
  `last_12_months`...) or start/end on one date field resolved against an injectable `FsmAnalyse.today` and sent to the FSM as `[gte]/[lte]` filters AND
  re-checked locally (an FSM that ignores a filter can't skew the answer), `having` (`count > 5`), `order`, top-N `limit` (20; 60 for dated; <= 100) with the
  rest merged exactly into an `Other` row. Every field is validated against the catalog (nearest names on error). Caps (`FsmAnalyse.max_rows/max_pages/max_seconds`,
  defaults `SCAN_MAX_ROWS` 50,000 / 120 pages / 60 s on the client's injectable clock; > 20,000 distinct groups refused): when it stops early the result says
  `INCOMPLETE: scanned N of M rows - narrow your filters` (`truncated`, `truncation`). Values that aren't numbers / dates are counted in `notes`, never silently
  dropped. Never returns raw rows (optional `sample_rows` <= 10, only the analysed fields). Privacy is `fsm_data`'s exactly (owner-only resources/groups,
  manager = the rest, team = neither tool, demo FSM = says so and returns nothing, 403 scope_off / 404 messages); "What Jarvis did" gets resource + group_by +
  rows scanned + who (`fsm_read` kind), never a value; a sensitive result's figures (in the spellings 49214 / 49,214 / 49,214.00) are noted so `remember` refuses them.
- **Charts** (`services/charts.py` validates, `web/charts.js` draws): spec `{type: bar|line|pie|donut|stacked_bar, title, x_label, y_label, unit: number|gbp|percent,
  series: [{name, points: [{label, value}]}]}`. Caps: bar 24 bars; line 6 series x 60 points; pie/donut 12 slices; stacked_bar 8 series x 24 categories; 240 points
  total; text stripped of control chars / HTML / secret-looking strings; values finite and <= 1e15; pie/donut/stacked can't be negative. The model reaches it via
  `fsm_analyse(chart=...)` (built from the groups: top-N + `Other`; a dated axis over the cap is an error saying use a coarser bucket) or `show_chart(spec)` for numbers it
  already has (`owner_only=true` for finance/pay/HR figures; numbers that match a recent owner-only read are forced owner-only, and a manager is refused them). The spec
  rides the existing `display` bus event (`{title, markdown, chart}`) into `openDisplay` in hud.js, above the table under it; nothing is stored or fetched. The chart is
  inline SVG built with DOM APIs (`textContent` only - a test greps charts.js for `innerHTML`/`eval`/...), re-validated client-side, theme-aware (reads `--panel/--text/--muted/--line`,
  a fixed 8-hue colour-vision-checked palette stepped per theme, redraws on `jarvis-theme` and on resize), `role="img"` + an `aria-label` summary, real `<button>` hit
  targets with arrow-key roving and a tooltip (hover, focus, touch), a collapsible data table, `Download PNG` (canvas, exactly 1600x900, like the advert export) and
  `Copy as CSV` (formula-looking labels get a leading `'`).
- **Who is sent a sensitive chart:** the owner's bus is shared by the owner's and every manager's console, so a display panel can carry `"audience": "owner"`
  (set by `fsm_analyse`/`show_chart` for owner-only data) and `access.event_visible()` is applied in `main.ws_events`' pump: only the principal owner's WebSocket
  receives it; a manager's does not; a team session never gets `display` events at all. Test: `tests/test_charts.py::test_an_owner_only_chart_reaches_the_owners_console_and_no_one_elses`.
  Caveat that predates this: managers share the owner's brain and conversation, so chat TEXT is still one shared surface (see "Not changed" above).
- Prompt guidance lives in `PERSONA` "# Numbers and charts" (use the tools for more than a couple of figures; state period / filters / truncation / sample data; headline
  in a sentence, detail on the display).

- **Vans and equipment (`services/fsm_assets.py`, `Accreditations.refresh_fsm_assets`):** when the catalog has an enabled `assets` group,
  vehicle MOT / road tax / service dates and equipment / test-kit calibration dates come from it and the daily reminders (90/60/30/14/7/1
  days, then due-today and weekly overdue) run from them; the fleet insurance policy means no per-vehicle insurance. The catalog does not
  promise asset field names, so `fsm_assets.roles()` maps the advertised field names/types to roles (registration, mot, tax, service,
  calibration, other due date, name, serial, driver, type, status) and classifies each ROW (a registration or a "vehicle" type = a van;
  sold/scrapped/inactive rows ignored) - **this is the one place to teach if the FSM's names differ**. No recorded date = `not_recorded`
  ("date not recorded": not compliant, not overdue, never reminded). Registrations are cross-checked against RAM Tracking's vans:
  `fleet_check` lists "check this" notes both ways (read-only; nothing changes). While the FSM is the source the YAML register
  (`accreditations.yaml`, and the example placeholders - YD71/YD72, ladders, harnesses, PAT) is ignored for vans/equipment/calibration, any
  real entry the FSM lacks is flagged, and `vehicle_update` / `vehicle_remove` / `equipment_update` / `equipment_remove` are refused
  (`Tool.precheck`, and again at approval time) with a plain "update it in the FSM". With NO assets group (or scope off, a demo or older FSM)
  nothing regresses: the register file and those four approval-gated tools work as before. The snapshot is refreshed by the 15-minute
  `fsm_data_catalog` scheduler job, on start-up, before the reminders, and by `accreditations_status`; one older than 36 h is dropped.
- **Doctor:** "FSM data access: N groups, M resources, scope off: x, y." (amber when the FSM does not expose the API yet or cannot be read).
- **Adding to it:** nothing per resource - a new FSM resource appears in the catalog and is readable with no Jarvis change. A new owner-only
  group is one entry in `fsm_read.OWNER_ONLY_GROUPS`. Tests mock the FSM with `tests/fsm_data_helpers.py` (`FakeFsmApi` behind the real
  `FSMClient` over `httpx.MockTransport`, a hand-wound `Clock`, `jarvis_with_fsm`).

*What is INSIDE an FSM document (`services/fsm_documents.py`, `j.fsm_documents`; the routes in `integrations/fsm_data.py`; tests
`tests/test_fsm_documents.py`).* The FSM (salts-fsm PR #9, its `docs/jarvis_data_api.md` "Document text and files") serves the text inside a stored
document and, for a scan or photo only, the file. One read-only tool, `fsm_document_read(document_id | query, category, attached_to, record_id, job_id)`:
- **Capability:** the catalog's `capabilities.document_text` / `document_files` and its `documents` section are parsed into `Catalog.capabilities` /
  `.documents` (`document_text`, `document_files`, `document_file_max_bytes` - never above 10 MB). An older FSM without them = "doesn't expose
  document reading yet" (nothing is requested); a capability change refreshes the prompt like a catalog change.
- **Finding it:** by id, or a search of the `documents` register resource (`q` over name/caption/type; exact `doc_type`, `entity_type`, `entity_id`,
  `source_job_id` filters validated like `fsm_data`'s). One match is read; several come back as candidates BY ID (name, type, attached to, date,
  `owner_only`) and nothing is read - never guessed (an exact file-name match is the only tie-break).
- **Client (`FsmData.document_text` / `document_file`):** the module still has exactly ONE `jarvis_call` (`_send`: gate, in-flight limit, 429 - and on
  document routes 503 busy - `Retry-After`, short waits slept); document errors map to plain kinds (`scope_off` with group, `files_off`,
  `file_not_available`, `text_available`, `not_found`, `integrity` (409 unreadable), `too_large`, `unsupported_type`, `busy`, `server`). Document
  routes back off ONLY themselves (`_doc_rate_until`; they have their own 30/min limit) and a missing/damaged document never trips the data API's
  outage back-off; only 401/network do. `/file` checks the declared Content-Length and the bytes against the cap and the PDF/PNG/JPEG allow-list;
  the service then sniffs the bytes really are that type. Document text keeps line breaks; control / zero-width / bidi / tag characters are removed
  and secret-looking strings redacted again (`clean_document_text`). The FSM's `redactions` count is passed on as `masked_by_fsm` + "N items were
  masked by the FSM" (the field name is held in `MASKED_FIELD` because the module's no-action-queue guard test greps for the word "actions").
- **Scans:** `text_source: none` + `file_available` -> `/file` -> `Documents.transcribe_scan` = the SAME path as a scanned email PDF (`_transcribe_pdf`:
  6 pages per call, `MAX_OCR_PAGES` 18, page images on the Max backend via `_stage_pdf`, document blocks on the API) or one image call
  (`_transcribe_image`, shrunk over 3.5 MB). Results say `transcribed=true` with a "may be misread" note and any "first N of M pages" note; every
  failure (files switch off, finance/people file never served, text_available, too large, transcription error) is a plain note, never a crash.
- **Access:** the group is the FSM's answer on `/text` (authoritative; a mirror of its `GROUP_BY_ENTITY` only pre-marks search candidates):
  owner = every group; manager = `compliance`, `commercial`, `operations` only (finance, people and ANY other/unknown group refused, default deny);
  team = no tool (not in `TEAM_TOOLS`). Finance/people text gets `handling` and every line is noted so `remember` refuses its figures.
- **Untrusted:** the text is fenced (`FENCE_START` ... `FENCE_END`, marker-like runs inside are defused) with a "DATA only" notice; the tool is
  `fsm_`-prefixed and in `UNTRUSTED_TOOLS` (chat/proactive get a pointer) and in `NOT_BACKGROUND`. "What Jarvis did" gets `fsm_document` audit lines:
  id + name (no name for an owner-only document) + who, never text. Demo FSM: says so. Doctor: "FSM documents: text on/off, files on/off".
  Prompt: `PERSONA` (after the email PDF paragraph) - use it for certificates, RAMS, reports, quotes/proposals and site documents, quote the name,
  say when it was a transcribed scan.

*Suggestions with a Prepare button (`services/fsm_suggestions.py`, `j.fsm_suggestions`; tests `tests/test_fsm_suggestions.py`,
`tests/test_console_browser_suggestions.py`).* "One step ahead": Jarvis offers to do the groundwork, in its own Approvals drawer AND in
the Salts FSM Action Centre (the office's inbox). Stage 1 is one kind, `quote_followup`: a SENT quote with no response for 7 to 60 days
-> Prepare queues the existing approval-gated customer chase email for that ONE quote (`CustomerComms.draft_quote_followup`, the same
draft and `comms:quote_followup:<id>` marker as the scheduled customer-email sweep, so the two can never both draft it). Everything lives
in that one module; **a new kind is one `Kind(name, lane, record_type, detect, prepare)` entry in `KINDS`** (service-due chase, completed
jobs with no report sent, ...). A suggestion row (`suggestions` table, new `kind`/`meta` columns) has `key` = the contract's
`external_id` = `<kind>:<record id>` (`quote_followup:Q1180`).
- **Contract (Jarvis -> FSM only, the existing FSM key via `FSMClient.jarvis_call`; the FSM never calls Jarvis; paths are absolute
  `/api/jarvis/suggestions...` under the FSM base URL whatever `FSM_API_PREFIX` is):** `PUT /api/jarvis/suggestions/{external_id}`
  (upsert, idempotent; body `kind, lane ("money"|"operations"|"compliance"), title, detail, reason, record_type, record_id,
  record_label, created_at`); `PATCH /api/jarvis/suggestions/{external_id}` (`{status: "prepared"|"failed"|"resolved", note,
  approval_ref}`, and `{status: "snoozed", snoozed_until, note}` for the console's Not now); `GET /api/jarvis/suggestions?status=requested`
  (a list of `{external_id, requested_by, requested_at, snoozed_until?...}`; a row with a future `snoozed_until` is skipped). Status:
  open -> requested (an FSM user pressed Prepare) -> prepared (Jarvis queued the draft; `approval_ref` = the Jarvis action id) ->
  resolved (the condition cleared, or the approved action completed). "Resolved" is always a PATCH, never a DELETE.
- **Schedule (`scheduler.py`):** `fsm_suggestions` (interval `suggestions_fsm_interval_min`, 15) detects, keeps the drawer true and
  publishes; in working hours (`suggestions_fsm_hours_start`/`_end`, Mon-Fri, `TIMEZONE`) every run, outside them hourly and quiet - no NEW
  push off-hours (the next working-hours run does it), only resolutions. A first run pushes at most 20 new ones. It is a quiet `_check`
  (one collapsed activity line, never a chat message or notification). `fsm_suggestion_requests` (every `suggestions_fsm_poll_s`, 60)
  is one cheap GET and, for each request, runs the kind's prepare handler and PATCHes `prepared` (with `approval_ref`) or `failed` (with
  a plain note, no stack trace or secret). The older `suggestions` sweep (3x a day) is untouched, except that it leaves rows with a kind
  alone (`fsm_suggestions.sync` keeps them true).
- **Switch:** `suggestions_publish_to_fsm` (Settings > Schedules, default ON, **owner-only** via `OWNER_ONLY_KEYS`). Off = nothing is
  pushed or polled; Jarvis's own drawer and its Prepare button still work. A FSM that is down, refuses the key (401/403/5xx) or does not
  have the endpoints yet (404/405 on the list or on PUT) is logged ONCE per outage and backed off (1 to 15 minutes; 5 to 60 for a 404);
  nothing reaches the UI. Sample data is never a source: with the demo FSM (`j.fsm.demo`) nothing is detected, pushed, polled or prepared.
- **Safety:** Prepare ONLY drafts. A handler queues through `ActionExecutor.queue` (`email_send`, which no standing approval can ever
  match - `standing_approvals` only looks at `fsm_write` and `po_acknowledgement`), so the draft waits for a human in the console or on
  Teams, even with both standing switches on (a test pins it). The module never approves, denies or sends (a test greps it). A request
  from the FSM is only a request to PREPARE. **Idempotent:** kv `fsmsug:prepared:<id>` is a one-time marker (a second request, from
  anyone, re-reports the same `approval_ref` and queues nothing; a per-suggestion lock covers two at once); a failure leaves no marker so
  it can be pressed again once fixed; a PATCH the FSM did not accept is repeated without redoing the work. FSM-requested prepares are
  capped at `suggestions_prepare_max_per_hour` (20; the rest stay `requested` for the next hour). An edited or retried draft is a new
  pending action: `approval_phase` follows `superseded_by`, so editing the wording never reads as "dealt with"; when the draft is approved
  or denied the suggestion becomes resolved (PATCH `resolved`).
- **Console:** `POST /api/suggestions/{key}/prepare` and `/snooze` (owner/manager + same-origin, `MANAGER_OK`, registered before the
  catch-all `/{decision}` route, so a team session gets 403). Prepare runs the same handler and also PATCHes the FSM if it has the
  suggestion; Not now sets the row `dismissed` (quiet for 20 hours, as before) and PATCHes `snoozed` with `snoozed_until`. In the
  Suggestions section of the Approvals drawer a suggestion with a kind shows **Prepare** and **Not now** (older ones keep Do it);
  more than four are behind "Show all"; the "Needs you" strip says "N suggestions from Jarvis - ready to prepare" and opens the drawer.
  `prepare_suggestion`/`snooze_suggestion` are called only from `main.py` and the poller - there is no brain tool (the `suggestions` tool
  only lists, and its text says a person has to press Prepare); a test greps for it.

*Upsell Opportunities, Jarvis's half (`services/upsell_drafts.py`, `j.upsell_drafts`; tool `upsell_opportunities`; tests
`tests/test_upsell_drafts.py`).* The FSM (separate repo) finds sites where Salts maintains only SOME of Fire Alarm, Intruder Alarm, Fire
Extinguishers, Access Control and Emergency Lighting, raises ONE Action Centre item per site with a fixed-template draft email, and an
office user approves, edits or declines it THERE; the FSM sends the email (M365) only on that human click. **Jarvis never sends, approves,
declines or edits-after-a-person.** It only (1) improves the draft wording and (2) answers "any upsell opportunities?" by reading the open
items. Everything is Jarvis -> FSM through `FSMClient.jarvis_call` (the existing key); the FSM never calls Jarvis.
- **Contract (absolute `/api/jarvis/upsells...` paths under the FSM base URL):** `GET /api/jarvis/upsells?status=open&draft_source=template`
  and `GET /api/jarvis/upsells?status=open` -> a list of `{id, site_id, customer_id, customer, site, services_we_hold: [..],
  services_not_maintained: [..], last_visit (ISO date | null), draft: {subject, body, draft_source: "template"|"jarvis"|"person"},
  contact_first_name, office_phone}` (no finance fields, no email addresses). `PATCH /api/jarvis/upsells/{id}/draft` with `{subject, body}`
  -> 200 `{ok: true}`; **409** = the item is no longer open or a person has edited the draft (Jarvis never retries that item); **422** = the
  text was rejected (it must still contain the opt-out line and the office phone number, plain text, length caps) - Jarvis leaves the template.
- **Draft improver (scheduler job `upsell_drafts`, a quiet `_check`: one collapsed activity line per run, never chat or a notification):**
  every `upsell_drafts_interval_min` (10) minutes in working hours (the suggestions' `suggestions_fsm_hours_*`, Mon-Fri, `TIMEZONE`), hourly
  outside them, and once at start-up (`Jarvis._first_run`). It GETs the open `template` drafts and, for each (at most 10 a run), makes one
  tool-less `llm.structured` call (schema `UpsellEmail{subject, body}`; same call as `po_intake`) and PATCHes the result. The FSM text is
  untrusted: control characters and the `<<<`/`>>>` fence are stripped, fields are clipped, and it goes into the prompt inside a
  `<<<FSM_DATA ... FSM_DATA>>>` block that the system prompt calls data, never instructions.
- **Hard rules, in the prompt AND re-checked in code (`finish()` / `problems()`):** short, plain British English, from the company
  (`COMPANY_NAME`), an offer and a question ("who looks after your emergency lighting?"), one visit and one invoice as the benefit; NO
  prices (pound/dollar/euro signs, "%", cost, price, discount, saving, "per year"...), NO link or email address, NO claim that the customer
  lacks a system or is non-compliant (wording is "we don't currently maintain...", never "you don't have..."; "non-compliant", "breach",
  "required by law", "at risk", "unprotected", "you must", "act now" all reject), plain text only (letters, digits and basic punctuation; no
  markdown, HTML, emoji), at least one question, at most 1,500 characters / 200 words, subject at most 90 characters. The contact's FIRST
  name only: the code writes the greeting (`Hi <first name>,`, or `Hello,` when the name is missing or not a plain name). The office phone and the opt-out
  line are the FSM template's own, parsed out of the current template draft (`template_lines`): the opt-out line is the template's line(s)
  matching the opt-out wording, the phone is `office_phone`; whatever the model wrote about opting out is dropped and the template's line
  is appended verbatim as the LAST line, a missing or re-formatted phone is put back exactly as the FSM has it, and a missing sign-off is added.
  A template with no recognisable opt-out line or no `office_phone` is left alone (no AI call). Wording that breaks a rule that cannot be
  repaired (price, link, claim, no question, too long) gets ONE retry with the broken rules named, then the template stays.
- **Idempotent and permanent stops (kv):** `upsell:done:<id>:<template hash>` (improved, rejected by our rules, or refused 422 - not tried
  again for that exact template), `upsell:stop:<id>` (409 or 404 on the PATCH: never touched again, whatever the template becomes),
  `upsell:tries:<id>` (AI failures; five and it gives up on that item). The model being away is not an error anyone sees: the template stays
  and it tries again next run. A 404/405 on the GET (the FSM has not shipped upsells yet), a refused key, a 5xx or the FSM being down backs off
  (1 to 15 minutes; 5 to 60 for a 404) with ONE log warning per outage and a quiet "reachable again" line; nothing reaches the UI. Sample data is
  never a source (`j.fsm.demo` -> nothing is read or sent). No key or FSM text is logged.
- **Switch:** `upsell_drafts_enabled` (Settings > Schedules next to `suggestions_publish_to_fsm`, default ON, **owner-only** via
  `OWNER_ONLY_KEYS`). Off = no polling, no AI calls, no PATCH; the voice tool still answers.
- **Voice tool `upsell_opportunities`** (`NoInput`, read-only, `approval=False`): `GET ...?status=open` and a short spoken-style answer - the count,
  then up to five sites ("Acme Ltd, Acme House: we don't maintain their emergency lighting and access control yet."), "And N more.", and always
  "Approve or decline them in the FSM Action Centre - I can't send these." Only customer, site and the five system names are spoken (no ids,
  phone numbers, drafts, dates or finance). With the demo FSM it says it can't see real data (never sample figures); a 404 says "The FSM doesn't
  have the upsell opportunities feature yet"; an outage says it couldn't reach the FSM. **Not in `TEAM_TOOLS`** (default deny: it lists customers
  and sites by name), so a team session cannot call it.
- **Safety:** the module has no code path that approves, declines, sends or emails and imports nothing from `actions`, mail, notifications or
  customer comms (tests grep it; its only FSM verbs are the two GETs and the one draft PATCH). Standing approvals are untouched. A reworded
  draft is still just a draft: a person approves and the FSM sends.

*Daily rhythm (`services/daily_rhythm.py`).* The morning briefing (09:00) and end-of-day wrap-up (17:30), Monday to Friday, UK
time, are intentional scheduled posts (not "checks": they always say something). `briefing_enabled` / `briefing_cron` /
`wrapup_enabled` / `wrapup_cron` are in Settings > Schedules. Each text has a word budget enforced in code
(`MAX_WORDS` 150, asked for as 130 to 150; over budget -> one "shorten it" retry -> trimmed to whole sentences), so it is under
a minute spoken. `daily_rhythm.deliver` posts the same text to the console (`Proactive.scheduled`: into the conversation record
always, pushed as a `proactive` event so a muted session never gets it, held as a notification in quiet hours, read aloud only
when "Jarvis speaking up" is on) and to Teams/email (`Notifier.send_owner_update`). A failed run is logged as failed and leaves a
warning, never silence. Tests: `tests/test_daily_rhythm.py`.

*Team mode (`jarvis/access.py`, `services/team_access.py`, `services/team_sessions.py`; tests `tests/test_team_mode.py`,
`tests/test_team_console_browser.py`).* Three roles: owner (the principal owner), manager (anyone `auth.is_owner` lets in today,
unchanged) and team (engineers and office staff). A team member signs in at `/login/team` with a name and ONE team access code
the owner sets in Settings > Team access (`/api/team-access`, principal-owner only, same-origin click); only a salted scrypt hash
is stored (kv `team_access`), the code is never returned or logged, and changing or clearing it signs every team session out
(the hash is part of the cookie's signing key). The team cookie (`jarvis_team_session`, 7 days) is separate from the owner's and
can never satisfy `is_owner`/`is_principal_owner`; `auth.role_of` is the one place a connection becomes a role. Enforcement is on
the backend, default deny, in two tables in `access.py`: `ROUTE_POLICY` classifies EVERY route (public / page / team / manager /
owner; `main.create_app`'s app-wide `guard` 401s/403s before any handler, an unlisted route is refused to everyone, and the
inventory test fails on one), and `TEAM_TOOLS` is the only tools a team caller may use - checked by `tools.dispatch` AND
`AsyncTools.start`, so a background call can't reach a tool its requester couldn't call (rows carry `requester`/`role`;
`background_results` shows a team member only their own; team background calls are forced SILENT). A team session talks to its OWN
brain (`TeamSessions`: same two backends, team tools only, no web/file tools, in-memory conversation, never the owner's
transcript or metrics, a prompt with nothing of the owner's) on its OWN event bus; its WebSocket reads only that bus plus
`reload` (`access.TEAM_EVENTS`) - never approvals, notifications, proactive posts or finance. `/api/status` for team is built
from `TEAM_STATUS_KEYS` and reads only staff, overdue jobs, presence and accreditations; the page itself is cut down
server-side (`<!--role:...-->` regions in `index.html`). A team request that queues an approval (`log_job`) always waits for a
human, never consults the standing approvals, and is stamped "asked for by NAME (team)" on the card. When you add a route, add
it to `ROUTE_POLICY`; when you add a tool, it is denied to team until you add it to `TEAM_TOOLS` on purpose.

*Engineer home points (`services/engineer_homes.py`; tests `tests/test_engineer_homes.py`, `tests/test_engineer_homes_browser.py`).* RAM's
public API has no address labels (only lat/lng, registration, driver), so "home" in Fleet / `who_is_home` / `van_day` comes from a point
the OWNER sets per engineer in Settings > Engineer homes: a van within the owner's radius (100 m default, 50-300 m, kv
`engineer_home_radius_m`) of its driver's point is `at_home` (`tracking.combine_home`; a RAM label containing "home" is only a fallback;
no point and no label = unknown, which `home_status` lists under `no_address_label`). Where someone lives is sensitive personal data,
so: the owner types a UK postcode ONCE; the server POSTs it to postcodes.io (in the request body, never a URL), rounds the answer to 4 dp
(~11 m) and stores ONLY engineer, lat, lng, set_by, set_at in the `engineer_homes` table - the postcode is discarded (not in the
database, any response, log or audit line; error messages never repeat it; the request body is read by hand so a 422 can't echo it).
The five `/api/engineer-homes...` routes are OWNER_ONLY (`access.ROUTE_POLICY`: a manager or team session gets 403) with a same-origin
click for changes, and it is deliberately NOT a tool: nothing under `brain/`, `services/` or `integrations/` except the tracker's matcher,
the service itself and the nightly retention job may mention it (a grep test pins that). The model only gets the derived answer: a van at
home is shown as just "home", `engineer_locations` strips its lat/lng, and `van_day` prints "home" instead of the home coordinates.
Set / clear / radius changes go in the activity log (engineer name, time, who - no coordinates). Nothing exports it: there is no
whole-database dump, backup or table export in the code (a test fails if one appears - it must then exclude `engineer_homes`), the memory
pop-up, staff report, status, settings and the Azure archive tool never read it, and `PRAGMA secure_delete` is on so a deleted point is
overwritten in the file. *Retention and erasure:* Remove (one) and Remove all in the Settings section delete rows; the nightly 03:25 job
(`engineer_homes_retention`) deletes the home of anyone who has left the staff list (and does nothing if the list could not be read).
To purge by hand: `sqlite3 data/jarvis.db "DELETE FROM engineer_homes; DELETE FROM kv WHERE key = 'engineer_home_radius_m';"` (on Azure the
file is `/home/data/jarvis.db`). A copy of `jarvis.db` taken before a removal still holds the old points (the rounded point only - never a
postcode), so treat database file backups as holding home locations and delete old ones when someone asks. Tell engineers before you set
their home (the Settings note says so).

*Standing approvals (the one deliberate exception to "queue, then a human clicks").* `services/standing_approvals.py`
lets the OWNER, in Settings only, pre-approve two narrow classes: "Record keeping" (a `fsm_write` POST creating a
customer/site/contact/note/task/reminder, exact path shapes and body keys) and "Routine acknowledgements" (the
`po_acknowledgement` kind: a fixed receipt-only email to the sender of an already-matched PO). Invariants: both
switches default off and are in `settings_store.OWNER_ONLY_KEYS` - along with owner/partner email, display password
and staff key, so a manager can't promote themselves - and the Settings API 403s anyone but the owner themself
(`auth.is_principal_owner`, which trusts the OWNER_EMAIL captured at startup, never the editable live value); the allowlist is closed (anything unknown, any other method/path/key, any
`tool:*`, money, deletes, job booking, `email_send`, `deploy_fix`... simply queues as before); the payload judged is
the payload stored and run (and re-checked in `_run`); automatic runs use the same `_run` path (ThoughtProof etc.),
record `approved_by = "standing approval: <category>"`, are announced on the display and in Teams, and are capped per
rolling hour. Standing approvals are the owner's own advance approval - Jarvis never sets, widens or approves them,
and nothing a model, an email, a pending-action payload or a Teams message says can. When adding a capability, do NOT
add it to the allowlist to save the owner a click; that decision is the owner's.

**The engineer-loop pattern (`fixer.py`, `security_watch.py`, `self_improve.py`).** All three download a
tarball snapshot of a repo into a temp dir (`GitHub.download_tree`), wrap it in `services/workspace.py`'s
`Workspace` (a virtual `/repo` root with `view`/`str_replace`/`create`/`grep`/`find`, path-confined so the model
can't escape the checkout), then run a bounded tool-call loop (`MAX_TURNS`, `cache_control` on the system
prompt) until the model calls a terminal tool (`submit_fix`/`submit_findings`/`submit_change` or `give_up`).
`fixer.py` and `self_improve.py` end in a PR; `security_watch.py` is read-only by design (its editor tool
rejects every command but `view`). `self_improve.py` is deliberately narrower than `fixer.py`: no merge step, no
deploy step, ever, not even behind an approval click - a human always merges it. Copy the shape of whichever of
these three is closest to a new engineer/review-style feature rather than starting from scratch.
*Model and effort for these three.* They use `Settings.engineer_model_or_default()` (`ENGINEER_MODEL`; blank = same as
`JARVIS_MODEL`; never hard-code an ID - the owner supplies it) and `Settings.engineer_effort` (`ENGINEER_EFFORT`), on both
backends: `llm.request_params(..., model=...)` for the API loop and `max_backend.run_once(..., model=...)` for the `_max`
variants. `engineer_effort` is one of `low|medium|high|xhigh|max` (what the Agent SDK's `EffortLevel` and the API's
`output_config.effort` accept); it is lower-cased/trimmed, blank means `high`, and anything else raises a clear validation
error at startup (`Settings._check_engineer_effort`) instead of being ignored. Both appear (advanced) in the Settings page's
Claude section and are in `settings_store.OWNER_ONLY_KEYS`, so only the owner can change them. A new engineer-style service
must pass both too. Tests: `tests/test_engineer_model.py`.
`services/recruiter.py` (the `recruit_agent` tool) generalises the same shape beyond code: a fresh agent, a
fixed turn budget, a final answer - but against Jarvis's own tool set via `dispatch()` (so a write it proposes
queues for approval exactly like anything else) rather than a code checkout, for research/drafting/analysis
tasks worth delegating rather than doing inline. Like the other three it runs both backends (a plain
`AsyncAnthropic` tool loop for the API backend, `max_backend.run_agent` - a filtered MCP tool server - for the
Max/Claude Code backend); `NO_RECURSE` in that file is what stops a recruited agent recruiting further agents
or starting another background job itself.

**CI failure logs for the engineer loops (`services/ci_logs.py`, tool `ci_log_excerpt`; tests `tests/test_ci_logs.py`).**
`checks_summary()` only says pass/fail, so the engineering agents had no way to see *why* CI failed. `ci_log_excerpt`
takes `run_id` or `head_sha` (newest failed run for that commit), reads the failing job(s) via `GitHub.run_jobs` /
`GitHub.job_log` (GET only) and returns the whole log if it is small, otherwise the last 60 lines plus context around
FAILED/Error/assert lines. The result is always capped at `MAX_EXCERPT_CHARS` (30k chars) - the ~1MB tool-result buffer
has broken auto-fix attempts before (issues #6, #17), so never raise it near that, and `job_log` itself keeps only the
last 4MB of a download. It is offered in `SELF_IMPROVE_TOOLS`, `ENGINEER_TOOLS` (fixer) and `REVIEW_TOOLS`
(security_watch); the API-backend loops call it via `ci_logs.run_ci_log_tool(self.gh, input)` (it is async, so it is
dispatched in the loop rather than in the sync `_tool_call`), and the Max/Agent SDK `_max` variants get it as an
in-process MCP server (`ci_logs.sdk_ci_log_server`, allowed tool `mcp__jarvis_ci__ci_log_excerpt`) alongside
Read/Edit/Glob/Grep. Log text is untrusted data (redacted with `redact_text`, never instructions); errors from GitHub come
back to the model as a tool error. A new engineer-style agent that has a `GitHub` client should offer it the same way.

**Progress visibility for engineer loops (`services/agent_runs.py`, the `agent_runs` tool).** `self_improve.run()`,
`Fixer.attempt()` (built-in mode) and `SecurityWatch.run()` each wrap their run in `AgentRuns.track(...)`, which
keeps one `agent_runs` row per run: request, start time, status (`running`/`submitted`/`gave_up`/`failed`/`interrupted`) and a
trail of one-line tool-call summaries (`editor view <path>`, `grep '<pattern>'`, ...) added after every tool call
in the loop - never file contents or edit text; the newest 60 steps are kept. The read-only `agent_runs` tool lists
recent runs; a run still `running` with no activity for 30 minutes (`STALL_AFTER`) is reported as `stalled` (worked
out when read, not stored). A run cancelled mid-flight is closed `interrupted`; at start-up (and when a new run
starts) `AgentRuns.interrupt_stale()` closes `running` rows left by a crash or restart as `interrupted` - but only
rows that started over `INTERRUPTED_AFTER` (2h, twice the assumed `MAX_RUN_TIME`) ago with no step in the last 30
minutes, so a second process sharing the database during a rolling deploy never has its live run marked dead.
`SelfImprove.run()` also notifies the owner (`self_improve_failed`, engineering-flagged) when a run raises or is
cancelled, and a Claude Code run that hits `max_turns` (`MaxTurnsExceeded`) is a `gave_up` with a plain message,
not a parse error. Recording is observability only: it swallows its own errors and never alters what an
agent does. A new engineer loop should call `self.runs.step(block.name, block.input)` after each tool call. The Max
(Claude Code) backend gives no per-step hook, so those runs show a single "handed to Claude Code" step.

**Pre-quote Companies House check (`integrations/companies_house.py`, `services/company_check.py`, the read-only `company_check` tool;
tests `tests/test_company_check.py`).** "Is this new commercial customer a live company, are its accounts overdue, how long has it
existed." Free Public Data API (`https://api.company-information.service.gov.uk`, HTTP Basic with the key as username and an empty
password): `/search/companies?q=...&items_per_page=5`, `/company/{number}` and only the COUNTS from `/company/{number}/charges` (called only
when the profile says `has_charges`; a 404 there means 0). Officers, PSC and "persons entitled" are never requested or read: `parse_profile`
copies an allowlist of company-level fields and cuts the registered office to town + outward postcode, so no individual's data is stored,
cached or returned (a test feeds profiles that carry such data and asserts none surfaces). `approval=False`, no write path, NOT in
`access.TEAM_TOOLS`; in `async_tools.NOT_BACKGROUND` (it puts its report card on the display, like `doctor`) and `UNTRUSTED_TOOLS` (register
text is someone else's). **Matching is by company number**: a number (8 chars, e.g. `01234567`, `SC123456`; 6-7 digits get their zeros
back) runs the check; a name is only a search and runs the check only when exactly ONE result matches exactly (`name_key`: case,
punctuation, `&`=`and`, Ltd=Limited; a dropped suffix is NOT equal) - otherwise up to 5 candidates (name, number, status, incorporated, town)
come back with an instruction to ask the owner to confirm by number. The profile is cached in `kv` under `company_check:{number}` for 6 hours
(searches are never cached; the age is recomputed from the injected `now` each time); the client keeps its own sliding window (500 / 5 min,
under Companies House's 600) and stops asking for 60 s after a 429. Every failure is a `CHError` with a plain message that carries no key
(not connected / key refused / limiting requests / not answering). Register text is untrusted: `clean_text` strips control and invisible
characters, URLs, e-mail addresses and link/tag syntax and caps the length before it reaches the model, the display card or the feed.
The report (`build_report`): status (anything but `active` is a RED FLAG), incorporation date and "existed for N years M months" (always
said to be the incorporation date, not proof of trading), accounts next due / overdue / last made up to and type, confirmation statement,
insolvency flag, charges outstanding (count), type, SIC codes, registered-office town + postcode area, a "things to check" list (not
active, strike-off proposed, accounts overdue, confirmation statement overdue, incorporated under 12 months ago, dormant accounts,
insolvency history, charges outstanding) and ALWAYS the limit sentence "Companies House shows filing status only - it is not a credit
score, and sole traders and partnerships aren't on it."; creditworthiness is never stated. `today` is the UK date of the injectable
`CompanyCheck._now` (tests never read the clock). **Approval-card line**: `create_customer` calls `CompanyCheck.card_line(name)` just before
`actions.queue` and appends it to the SUMMARY only (exact-name look-up: one match -> status/age/flags; several -> "ambiguous - ask me to run
company_check"; none -> "no exact name match / sole traders aren't on it"); the payload judged by the standing approvals and sent to the FSM
is untouched, the look-up is capped at `CARD_TIMEOUT_S` (6 s) and any failure or timeout becomes a short "not checked" note - queueing is
never blocked. It needs the key and the owner-only `companies_house_on_new_customers` switch (default ON). Settings: owner-only
`companies_house_api_key` (secret kind, so `doctor.secret_fields` scrubs it automatically) and the switch, in the "companieshouse" section with
a Test button (`connection_tests._companieshouse` -> `CompanyCheck.test`, one profile look-up of Tesco PLC `00445790`, chosen as a large,
long-established active company; its number is a constant to change if it ever stops being suitable). The `doctor` Keys check reports
`COMPANIES_HOUSE_API_KEY` set / not set (name only). Look-ups are logged to "What Jarvis did" (`activity_feed.record("company_check", ...)`:
company name and number only). Not verified against the live API in CI (every call is a `httpx.MockTransport`).

**Self-diagnostics (`services/doctor.py`, the read-only `doctor` tool; tests `tests/test_doctor.py`).** "What is quietly broken?": one
line per item, each `ok` / `amber` / `red` with a next step, put on the display (`bus.publish("display", ...)`, so `doctor` is in
`async_tools.NOT_BACKGROUND`) and returned as `{summary, items, shown_on_display}`. `approval=False`, deliberately NOT in
`access.TEAM_TOOLS`. Eight checks, all read-only from existing services: plugins on in Settings but inert or with no `mcp_plugins.yaml`
entry (the reason comes from the same code that decides whether a plugin starts - `launch_config`, `engineering_setup`, `chat_setup`,
`ActionVerifier.problem` - so it can never read ok while Jarvis treats the plugin as inert; a broken ThoughtProof is red because it fails
closed), data sources still on DEMO (`demo_guard.demo_now`), keys set or not by NAME, automations (last run, `NOTHING_STREAK` quiet
runs in a row from `check_runs`, running under every 30 minutes outside Mon-Fri working hours), agent runs (stalled / failed / gave up
in 24h), open issues and approvals untouched for 24h, open PRs red or conflicted (`PRClient.list_open_prs`, which now also returns
`created_at`/`updated_at`; "red for 24h" is judged from `updated_at`; no GitHub config is just an ok "not connected" line), failing
routine tests and `needs_human` issues. **Fail soft:** `Doctor.run` wraps every check, so one that raises (or returns junk) becomes a
single amber "could not check: <reason>" line and the rest still run. **Never print a secret:** a key is only asked "is it set?"
(`_is_set`); no setting is put in an f-string or a log line; error reasons have every configured credential value removed
(`_hide_secrets`; the list is `secret_fields()`: every secret-kind Settings field plus any credential-named setting, so a new secret is covered automatically) and are redacted; a grep test pins this. To add a check, add a `(name, method)` to `Doctor.CHECKS` returning
`Item`s, and a test with fakes.

**The FSM engineer bot (`services/fsm_engineer.py`, `j.fsm_engineer`, tool `fsm_engineer_audit`; tests
`tests/test_fsm_engineer.py`).** A read-only systems audit, scheduled by `fsm_engineer_cron` (and switched by
`fsm_engineer_enabled`, both env-only): it reads the latest routine test results, open issues, failed approved writes and two live
read-only probes of Salts FSM (`check()` and today's `jobs()`), and builds one JSON payload per failure (`PAYLOAD_KEYS`) with a
root-cause category from `classify()`. A cause is CONFIRMED only when recorded tool output itself says so; issue/report wording
can suggest a category but never confirm it, and the payload states that no logs are available - never invent log lines. It
hands each new/changed failure to the engineering agent by queueing the existing `tool:issue_fix` action (a human still approves
it; the payload is stored in kv `fsm_engineer:payload:<issue id>` and `Fixer.engineer_payload_note()` shows it to the engineer as
untrusted data). It never writes to FSM, merges, deploys, approves or changes settings (a test greps the module), tells Alex on
Teams only when a failure is new or changed (state in kv `fsm_engineer:state`; fingerprints ignore timings), and a scheduled run
with no change returns `NOTHING_TO_REPORT`. Everything it reads is untrusted data and is redacted before it is stored or sent.
`FSMClient.write` raises for any non-2xx (including 3xx) and the executor's `_fsm_ok` fails any result carrying a non-2xx status,
so a rejected approved write shows as failed with the error rather than done.

**Out-of-hours triage: keyholder notices (`services/ooh.py`, `j.ooh`, tool `out_of_hours_calls`; tests `tests/test_ooh_keyholder_notice.py`).**
Each event from the monitoring-centre reports gets a `follow_up_kind`: `engineer_visit` (genuine fault - comms/signalling failure, panel
fault, tamper, CCTV, battery/mains - or other work; sets `needs_job` when no FSM job exists, as before), `keyholder_notice` or `none`. Owner's
rule: when a call/signal was handled with no keyholder reached, the site/keyholders didn't answer, or the keyholder list is out of date or
missing, that is a customer conversation, NOT an engineer job - `needs_job` is False and `follow_up_needed` is reported False. A real fault always
wins (`genuine_fault` from the extraction, or fault words in `problem`), so keyholder wording can't talk a fault out of needing a job. So does a
real activation or emergency (`urgency == "emergency"`, or intruder/fire/break-in/activation wording in `problem` - `ooh._ACTIVATION`): with
no keyholder reached the site may be unsecured, so it stays `engineer_visit` (`needs_job` when no FSM job, listed in `needing_a_job` and the
suggestions) and gets no customer email; only the benign cases (e.g. keyholder list out of date, late to set) become notices. The reason
is the extraction's `keyholder_issue` (`not_reached`/`no_answer`/`list_out_of_date`/`list_missing`), backstopped by regexes on the report wording.
`calls()` is pure classification (also used by the briefing and suggestions); only the `out_of_hours_calls` tool then calls
`OutOfHours.draft_keyholder_notices()`, which queues ONE `email_send` action per notice (so it waits for the owner's approval like any email;
nothing here sends, approves or touches standing approvals) and de-duplicates with kv `comms:keyholder:<report id>:<site>:<time>`. The email is
plain British English: what happened and when, what the monitoring centre did (fixed wording per reason, not the report's own words), a request
to confirm the keyholder list and send changes, and a no-pressure offer to quote for the 24/7 keyholder response service; no prices. The recipient
is only ever the contract contact on record in Salts FSM (`contracts()` `contact_email`) for a contract whose SITE matches the event's site
(there is no customer-name-only fall-back, so the report can't choose a contact by naming a customer); an address from the report is never
used, and a shared inbox (`mail_guard.is_shared_mailbox`) or malformed address counts as no contact. With none, NOTHING is queued: the draft comes back under
`keyholder_drafts.needs_recipient` with `to: ""` and a "no contact on record" flag, for the owner to supply the address (sending it then goes
through `email_send` and approval as usual). Report text is untrusted: the draft only carries site/time/problem after `_clean()` (no links,
addresses, phone-number-like digit runs, National Insurance numbers, markup, short) and the report can choose a category and name a site (which
must match a contract site on record), never a recipient or an instruction.

**Second shared mailbox, service@ (`services/service_inbox.py`, `services/council_intake.py`, `integrations/microsoft365.mailbox_for`).**
Bradford Council portal job requests arrive in `service@`; Jarvis reads it through the SAME Graph app registration as `MS_MAILBOX`
(app-only `Mail.Read`/`Mail.ReadWrite` on that mailbox too, and `service@` added to any Exchange Application Access Policy). The address is
the setting `service_inbox` (blank = off; nothing reads it). It and the four council settings (`council_intake_enabled`,
`council_sender_patterns`, `council_subject_patterns`, `council_customer_name`) are one Settings section ("Service inbox (service@)") and ALL
in `OWNER_ONLY_KEYS`, so only the principal owner can change them, never a manager, a tool or a Teams message. No tool takes a mailbox
ADDRESS: the five read tools (`email_inbox`/`email_search`/`email_read`/`email_attachment_read`/`email_pdf_read`) take `mailbox:
Literal["owner","service"]` (default "owner" = the exact old code path and call shape) and `mailbox_for()` turns "service" into the saved
address; `GraphMail._base()` additionally refuses anything that is not one plain address before it goes in a URL. Sending, drafting replies and
`mark_read` still only ever use the owner's mailbox. The team role gets none of it: no email tool is in `access.TEAM_TOOLS`, `inbox` is not in
`TEAM_STATUS_KEYS`, and no new route exists (the Test button is the existing `POST /api/settings/test/serviceinbox`, the Comms list rides
`/api/status` -> `inbox.service`). The Test button (`ServiceInbox.test`) reads ONE message header and turns a Graph failure into a sentence with
the likely fix (`explain_graph_error`: 403 -> "the Azure app registration has no permission on this mailbox ... Application Access Policy ...",
401, 404 `ErrorInvalidUser`, `MailboxNotEnabledForRESTAPI`, throttling); Graph's own message text is only shown for an unrecognised code, after
every configured secret value has been scrubbed. The Comms drawer shows the unread list under its own "service@" heading (`ServiceInbox.unread`,
cached 30 s, errors shown as the sentence and not cached) and the rail count/label say how many are in it.
`CouncilIntake.scan_inbox` (scheduler job `council_intake_scan`, every `inbox_check_interval_min`, a no-op while `service_inbox` is blank) lists
the last 72 h of the service inbox (read or not), keeps the emails whose SENDER matches a sender pattern (a domain also matches its sub-domains,
never a look-alike) or whose SUBJECT contains a subject phrase, and has the brain classify each with the fixed `CouncilExtraction` schema
(fenced `<email>` text, redacted, control characters stripped, the email's own `<email>` tags neutralised). Fields are cleaned and capped, and
the reference, phone and email must appear in the email itself or they are dropped. A request becomes ONE approval-gated `fsm_write` `POST /jobs`
(exactly the `log_job` shape; description starts "BRADFORD COUNCIL PORTAL REQUEST - council ref: ...", with site contact, council priority, target date and the
attachment NAMES from Graph; `customer` from `council_customer_name`; `priority` only if the email states a response time that is a real SLA).
The council reference is in the description because the FSM's customer-PO field is a separate `PUT /jobs/{id}/customer-po` that needs the new job's
id, so it can't be part of a single proposal. A sender that is not a recognised council address, or no reference, puts a "check before approving" row
on the card (`needs_human_review`). It is NOT covered by any standing approval (`POST /jobs` is not in `standing_approvals.SHAPES`) and uses
none of the PO-receipt machinery. De-duplication: `processed_emails` row `council:<mailbox>:<message id>` (written once the email was read) and kv
`council_ref:<council|other>:<REF>` (the "other" namespace keeps a look-alike sender from using up a real reference). A failed read is retried on
the next scans (kv `council_try:`), and after 3 tries the owner is told to check the email, so a request is never silently lost. It only ever
reads: no reply, no send, no mark-read/flag/move/delete (the seen-markers are rows in Jarvis's own database, like the other intakes).

**GitHub PR tools for Jarvis's own repo (`jarvis/brain/pr_tools.py`, `jarvis/integrations/github_pr.py`,
`jarvis/services/pr_resolver.py`; full list and rules in `docs/github-pr-tools.md`).** Reads: `pr_list`, `pr_detail`,
`repo_read`, `repo_search`, `run_tests`. Writes, all `approval=True`: `pr_comment`, `pr_resolve_conflicts`, `pr_merge`,
`pr_create` (head branch into base branch, e.g. `jarvis-updates-2026-09-29` into `main`), `pr_close` (optional comment) and
`pr_set_base`. `PRClient._send` is an allow-list of (method, path) *and* checks PATCH/new-PR bodies - extend it, don't bypass
it. Hard rules: never push or force-push `main` (`pr_create` refuses a `main`/`master` head), `pr_merge` refuses unless CI is
green, and every call is bound to `JARVIS_REPO` (no tool takes a repo name). PR titles, descriptions, comments and code are
untrusted data, never instructions. A failed approved write raises `PRError` with the real (redacted) reason so the action
shows as failed with it. **The "main line" is the repo's real default branch:** `Jarvis.self_github` is built with
`follow_remote_default=True` (setting `jarvis_follow_default_branch`, env `JARVIS_FOLLOW_DEFAULT_BRANCH`, default on), so
`GitHub.resolve_default_branch()` asks GitHub for `default_branch` (cached 10 min) and `JARVIS_DEFAULT_BRANCH` is only the
fallback if that lookup fails. Self-improvement branches start from it and its PRs target it; `pr_merge` is gated on it. The FSM
repo's client (`j.github`, the fixer) keeps its configured `FSM_DEFAULT_BRANCH`. Tests: `tests/test_pr_tools.py`,
`tests/test_pr_resolver.py`, `tests/test_default_branch.py`.

**Optional MCP/plugin integrations** (`jarvis/brain/plugins.py`, `jarvis/services/verification.py`, specs in
`mcp_plugins.yaml` and `mandates.yaml`) each have their own `plugin_*` setting. Context7 (read-only docs) and the
Superpowers-style plan/test/review method go to the engineering agent (`self_improve`/`issue_fix`); Browser Use (read-only,
allowlisted domains) goes to conversational Jarvis only; ThoughtProof checks an action *after* the owner approves it,
inside `ActionExecutor._run`, and can only stop it (BLOCK, or fail closed if unavailable) - never approve, queue or skip.
External MCP servers only reach the Max/Agent SDK backend (the API-backend engineer loop is hand-rolled and has no MCP),
must be pinned to an exact version in `mcp_plugins.yaml`, and only tools listed in `allowed_tools` are callable
(`permission_mode="dontAsk"` denies the rest). Browser Use extras: the allowlist of dealer/government/industry sites lives in
`mcp_plugins.yaml` (`allowed_domains`, a ceiling the Settings field can only narrow); login/credential/checkout/payment/
download/script/cookie/agent tools, such web-address paths and file types are denied in code (`DENIED_TOOL_WORDS`,
`BLOCKED_PATH_WORDS`); a listed typing tool (`search_tools`) may only be given a UK number plate in a real format (no
whitespace/newline, no numbers except element indexes); `sandbox_confirmed` stays false until a human confirms a real
sandbox (the code can't create one); the version stays blank until verified on PyPI. Web addresses are parsed strictly
(ASCII hostnames only - no backslash, `@`, port, IP, `%` or punycode - exact allowlisted hosts, query strings of at most
64 characters made of plate-shaped or short plain values) and EVERY string in a call is searched for hosts, whatever the
argument is called. The PreToolUse hook only sees the call about to be made, not where a redirect ended up, so the
sandbox's network-egress allowlist (same hosts as `allowed_domains`) is the second wall. `www.gov.uk` and the DVLA
vehicle-enquiry host are listed individually, never all of `gov.uk`. The single on-switch is `plugin_browser_use_enabled`
(with `plugin_browser_allowed_domains`, both owner-only in `settings_store.OWNER_ONLY_KEYS`). Never add a plugin tool that can change something without going through
`dispatch()`'s approval gate.

**Everything not in the local SQLite (`jarvis/db.py`) is read live from its source system**, normalised through
alias tables so small API differences don't break things - e.g. `jarvis/integrations/fsm.py`'s `ALIASES` maps
`jobNumber`/`job_number`/`reference`/`number` all onto one `ref` field. `jarvis/db.py` itself only holds Jarvis's
own state: issues, notifications, memory, pending actions, the stock ledger, automations. Every integration with
external, optional config (FSM, Sage, GitHub, RAM Tracking, Microsoft 365, ...) has a `demo`/fallback
implementation (`DemoFSM`, `DemoMail`, `DemoRamTracking`, `CsvFinance`) used whenever the real one isn't
configured, so the whole app runs believably with zero setup - preserve this when adding a new integration.

**Settings are triple-layered**: `jarvis/config.py`'s `Settings` (pydantic-settings, reads `.env`/env vars) is
the base/fallback layer; `jarvis/settings_store.py`'s `SettingsStore` lets the owner override any field from the
web Settings page, Fernet-encrypted at rest, applied back onto the live `Settings` object (`apply()`) and
hot-reloaded without a redeploy. Adding a new configurable value means: a field on `Settings`, a `Field(...)`
inside a `Section(...)` in `settings_store.py`'s `SECTIONS` tuple (this alone makes it editable, encrypted,
hot-reloadable - no extra plumbing), and usually a matching entry in `Jarvis.connections()` so it shows up on
the HUD. If a section's "is this configured" check isn't simply "all its required fields are set" (an
either/or, like `github_token or jarvis_github_token`), add a special case in `SettingsStore.configured()`
rather than relying on the generic fallback.

**Tests use a fake Anthropic client, never real network calls.** `tests/fakes.py`'s `FakeClient` /
`FakeMessages` scripts `client.beta.messages.stream()` responses (`message([tool_block(...)], "tool_use")` then
a final `message([text_block(...)])`); `Jarvis(settings, client=FakeClient(script))` wires it in via
`tests/conftest.py`'s `settings` fixture (an isolated tmp_path, `_env_file=None` so a developer's real `.env`
never leaks into a test run). For engineer-loop services, build a small `FakeGitHub` matching the specific
`GitHub` methods used (`branch_sha`/`download_tree`/`commit_files`/`open_pr`/`checks_summary` - see
`test_security_watch.py`/`test_self_improve.py`) rather than hitting real GitHub. Any code path with a real
`asyncio.sleep()` polling loop (CI-watching, deploy-waiting) needs that patched out in tests
(`monkeypatch.setattr("jarvis.services.x.asyncio.sleep", instant_sleep)`) or it will actually wait.

**Reading files from outside: email attachments and console uploads (`services/file_reader.py`, `services/chat_files.py`, `Documents.read_*`).**
Everything is pure Python (pypdf, Pillow, python-docx, openpyxl, zipfile) because App Service Linux has no tesseract / poppler / ffmpeg - never
add a dependency that needs a system package. `file_reader` is the shared toolbox: `sniff()` (what a file IS from its first bytes and, for
zips, its parts), `classify_upload()` (the name's extension must agree with the bytes; old `.doc/.xls/.ppt` and macro-enabled files are
refused with a plain "Save As .docx" sentence), `open_office_zip()` (entry-count and inflated-size cap; XML with a DOCTYPE/ENTITY is refused;
only XML is ever read, a `vbaProject.bin` is never touched), `pptx_to_markdown()` (slide titles, text, tables, notes; no python-pptx),
and the PDF helpers. `FileProblem` (a `ValueError`) carries a plain sentence and a `code`; `describe_failure()` turns any exception into
"I couldn't read 'X.pdf': ..." - every failure must name the file and the reason, never a generic "can't open".
**Email path.** `GraphMail._fetch_attachments` lists attachments WITHOUT `contentBytes` (`$select=id,name,size,contentType,isInline`) and
fetches each file from `/attachments/{id}/$value` (works at any size; the JSON `contentBytes` is unreliable above ~3 MB, which silently
dropped scanned POs and made the tool answer "no PDF attachments"). `pdf_attachments` / `office_attachments` return `{name, data}` for a
readable file and `{name, problem, size[, detail]}` for one that is a OneDrive/SharePoint link (`referenceAttachment`), an attached email
(`itemAttachment`), over the limit (PDF 25 MB, Office 15 MB), an empty or failed download, or an old/macro Office format - so every caller
that needs bytes (po_intake, supplier_bills, ooh) filters on `data`. `Documents.read_pdf_attachments` / `read_attachments` turn those into
owner-facing sentences, and when nothing matched say what the email DOES carry (`attachment_overview`). The tool result is kept under the
60,000-character `serialise()` cut (`Documents._fit`) so the JSON is never chopped mid-file.
**PDF chain** (`Documents.read_pdf_bytes`): the text layer (`pdf_to_text`, first 30 pages) -> if it is a scan (under 40 characters of text, or
over half the pages have none) the model transcribes it, 6 pages per call, up to 18 pages (`_transcribe_pdf`), flagged `ocr=true` -> else a
plain message ("password protected", "damaged", "a scan and the transcription step failed on this server (...)", "missing component").
On the API backend the PDF goes to the model as a `document` block. On the **Claude Max backend** `run_once` cannot give a model a PDF
natively: it can only `Read` a file, and Claude Code reads a PDF of more than a few pages only by page range, which needs poppler
(`pdftoppm`). So `max_backend._stage_pdf` puts a text PDF's text in the prompt (plus the first 10 pages as a file), turns a scan into
JPEG page images with pypdf + Pillow (`file_reader.pdf_page_images`, up to 8 pages; `Read` opens pictures anywhere), and only falls back
to writing the PDF. `llm.structured(..., max_turns=)` exists because each page image is a separate `Read` turn. This staging also serves
po_intake, supplier_bills and the out-of-hours reports (all pass PDF `document` blocks to `llm.structured`). Not verifiable offline: that
Claude Code on App Service really opens the staged JPEGs (the tests assert what is staged, not what the CLI does with it).
**Console attach path.** The attach button (`role:manager` markup, owner + manager only; a team session's attachments are dropped before
anything is read) accepts photos, PDFs, Word, Excel, PowerPoint and text. `hud.js` checks first (extension vs first bytes, 5 files, 20 MB each,
25 MB total) and shows each refusal as an error chip under the attachment strip; `main.files_for_turn` -> `chat_files.prepare` re-checks on
the server (authoritative) and READS each file with the same readers: a PDF/Word/Excel/PowerPoint file reaches the brain as a
`text/plain` attachment fenced as untrusted data (`<file_content>`, the closing marker cannot be forged, control characters stripped,
`save_as` = `<name>.txt` so the Max brain's `Read` can open it), images and txt/csv/md/json behave as before. A refused file is reported in
`attachment_errors` (HTTP), a `notification` event (WebSocket) and a note on the turn so the reply says why. Nothing read is stored in
memory or the transcript. An upload over ~12 MB is sent over `POST /api/chat/stream` instead of the WebSocket (16 MB message cap).
Tests: `tests/test_email_pdf_failures.py`, `tests/test_console_attachments.py`, `tests/test_console_attach_browser.py` (Playwright),
fixtures in `tests/file_fixtures.py` (real tiny PDF / scanned PDF / docx / xlsx / pptx built in code).

### Testing rules: clocks and time zones

CI runs on ubuntu in UTC, developers run the suite on Windows in a local zone, and the real date keeps moving. Three "red on
every PR" incidents came from tests that quietly depended on one of those. The rules:

- **No wall-clock reads at import time.** Never put `date.today()`, `datetime.now()`/`utcnow()`, `time.time()` (or a helper that
  calls them) at module level, in a class body, a decorator/`parametrize` or a default argument of a test module. That freezes
  "now" at collection and builds fixtures around the day the run started. Read the clock inside the test or fixture.
  `tests/test_no_import_time_clock.py` AST-scans `tests/*.py` and fails on it; the escape hatch is a `# clock-ok: <reason>`
  comment on that line (a reason is required).
- **Pass `today` explicitly, or pin the module clock.** Code that needs "today" should take it as an argument (the tools that
  do, e.g. in `services/customer_comms.py`, default to the real date only when the caller passes nothing). A test then passes a
  fixed `today` and builds its fixtures relative to it, or `monkeypatch`es the module's clock. Never assert against a fixture
  date that only works while the real date is before it.
- **Never rely on `apply_timezone` leaking.** `Jarvis.__init__` calls `apply_timezone("Europe/London")`, which sets `TZ` and
  calls `time.tzset()` for the WHOLE process on Linux (and does nothing on Windows). A test must neither depend on that having
  happened nor on it having not happened: pin the zone it needs (`monkeypatch.setenv("TZ", ...)` + `time.tzset()` where it
  exists) or compute with explicit `zoneinfo`/`tzinfo`. `tests/conftest.py`'s autouse `_isolate_process_timezone` restores `TZ`
  after every test so one test's `Jarvis()` can't shift the clock for the next; do not remove it.
- **Tests must pass on Linux/UTC and on Windows.** No hard-coded backslash paths, no reliance on `tzset`, the local zone or the
  developer's locale. The scheduled `.github/workflows/nightly.yml` (daily 03:00 UTC, or run it by hand from the Actions tab)
  runs the full suite normally, under libfaketime at +40 days and with `TZ=Pacific/Auckland`; it never blocks a PR, but a red
  nightly means a test is about to start failing for everyone - fix it the same day.

**The console's shape (`jarvis/web/`).** `index.html` is a top bar, a left rail (a chip strip on phones), and one centre column
(core, one-line hint, "Needs you" strip, conversation, message box). Every dashboard section is a `.pop` inside the ONE
`#drawer` (`Drawer.show(name)` in hud.js, opened by any element with `data-pop`; closed by Close, Escape or the scrim), so a
new section is a new `<section class="pop" id="pop-x">` plus its name in `POPS` - never a second drawer. The rail counts and
the "Needs you" strip are computed in `renderRail()` from the same `/api/status` data the pop-ups render. All colours, fonts and
spacing are custom properties on `:root` in `hud.css`; the light theme block exists twice (media query for Auto, `[data-theme]`
for an explicit choice) and `tests/test_hud_layout.py` keeps them identical and checks contrast. `theme.js` (loaded in `<head>`)
owns the Auto/Light/Dark choice; `core.js` draws the core canvas on the console and the sign-in page. Real-browser checks:
`tests/test_console_browser.py` (Playwright; skipped when it is not installed).

**The HUD (`jarvis/web/hud.js`) talks to the backend over both a WebSocket (`/ws`, live/streaming - `thinking`/
`delta`/`tool`/`reply` events pushed through `jarvis/events.py`'s `EventBus`) and plain REST fallbacks
(`/api/chat/stream`, an SSE endpoint that forwards the same bus events, used only when the WebSocket is down).**
`handle(ev)` in hud.js is the single place both paths render into, so a new bus event type needs a case there
once, not per-transport. Voice wake-word listening for cost-free "always listening" runs the browser's own free
`SpeechRecognition` (`sentry` in hud.js) until it hears the wake word, then hands off to the configured paid STT
(Azure Speech/Deepgram/Whisper) for the actual command, sleeping back to the free listener after a period of silence
(`extendFollowUp`/`checkSleep`) - don't reintroduce a fully continuous paid stream for "always listening" mode.
Conversation quality (`services/conversation_quality.py`, `j.quality`): both brains call `begin()` at the top of `_turn`
and poke the returned record (`first_delta`/`tools`/`finish`) - keep that in any new brain path. It logs per-turn metrics,
the HUD's Good/Wrong buttons and "that was wrong" phrases (`/api/feedback`), browser-only signals (`/api/voice-events`),
and feeds the nightly reflection plus a weekly summary. The regression suite to run after every change is
`tests/test_conversation_regression.py`. **Privacy:** its tables (`turn_metrics`, `voice_events`, `turn_feedback`) hold
conversation text - a short (<= 300 chars), credential/access-code-redacted excerpt of what was said and Jarvis's reply, plus
the owner's feedback note; the full conversation is only in `transcript` (2-year retention). Rows older than the
`conversation_quality_retention_days` setting (default 90) are deleted at start-up and by a daily job (`ConversationQuality.prune()`,
id `conversation_quality_retention`). To purge on demand, as the signed-in owner call `DELETE /api/quality` (everything) or
`DELETE /api/quality?older_than_days=N`, or in SQL `DELETE FROM turn_metrics; DELETE FROM voice_events; DELETE FROM turn_feedback;`.
It is deliberately not a brain tool. Feedback phrases ("that was wrong") are verdicts, not turns; STT timing is matched to its turn
by transcript (`note_stt(ms, text)`), and a first-audio report without a turn id is dropped rather than guessed. Barge-in (talking over Jarvis) lives in `utterance()`'s echo-window block: the window stays a strict allowlist (wake
word or stop phrase only); stop phrases always cut the turn via `stopEverything()`, the wake word additionally needs
the `bargein` setting on, `bargeInAllowed()`, and to not match Jarvis's own recent speech. Tests: `tests/test_hud_bargein.py`.
Pressing the mic/Space while he speaks cuts the whole turn (`micPressBargeIn()`). The push-to-talk silence timeout is the
`voice_silence_ms` setting plus a little extra after trailing fillers (`endOfTurnMs()`); `looksLikeSelfEcho()` also drops
fuzzy copies of what he said in the last 10s. A user message near-identical to the previous one within 60s gets a
"[possible repeat: ...]" line under its tag from both brains (`jarvis/brain/repeats.py`). Tests: `tests/test_voice_flow.py`.
Decisions use the `ask_user` tool (`brain/tools.py`) and the small question pop-up in `jarvis/web/ask.js`/`ask.css`: the tool
only publishes an `ask` bus event and returns at once (no blocking); the chosen/typed/spoken answer comes back as an ordinary
chat message. It is separate from, and must never call or imitate, the approval path (`decide()`, `/api/approvals`,
`ActionExecutor`). Tests: `tests/test_ask_user.py`.
Phase 3 of the console redesign ("fixes found on 2 Oct"):
(1) **Sample data is never reasoned from** (`jarvis/demo_guard.py`). A demo source (`DemoFinance`, the seeded stock, sample social figures, the
example staff register, `DemoRamTracking`) calls `demo_guard.touch(source)` where it serves sample figures; inside `tools.dispatch()` that raises
`DemoDataBlocked` (a BaseException, so no `except Exception` fallback can swallow it), the tool's result is replaced by `demo_guard.refusal()`
(`demo_data_withheld`, which source, what to connect) and the persona's "Sample data is never an answer" rule tells him to say so. Composite answers
(briefing, wrap-up, business advice) wrap each source in `demo_guard.section()` so only the sample part is dropped; stored suggestions that rest on
sample data are filtered (`visible_suggestions`). `StaffRegister.prompt_summary()` returns a neutral line while the register is the example. Nothing
outside a tool call changes: the console's pop-ups keep their sample data and "demo" labels. When adding a demo source, add a `touch()` there and a
`Source` in `demo_guard.SOURCES`. Salts FSM and Outlook demo are NOT gated (every test uses them as its stand-in; the prompt rule still covers them).
(3) Speech-to-text: `stt_chain.stt_problem()` says why the chosen engine can't be used; `voice.client_config()` carries it as `stt_problem`;
the top bar's `#stt-status` shows "Voice input: browser fallback - <why>" (hud.js `sttStatus`, also fed by a failure while listening).
(4) RAM Tracking: a `RamError` per failure with plain-English text (no secrets), `RamTracking.health`/`probe()`, one cache and one request budget for
RAM's 3-requests-a-minute-per-endpoint limit (429 = "rate limited", never "not connected"), the API address reduced to its host (`origin_of`), all
parsing defensive against RAM's published schema (`last_event` is a STRING). `Jarvis.vehicle_tracking_status()` feeds the Connections line and the Fleet
pop-up ("DEMO ... still missing: ...", "NOT CONNECTED - ... <reason>", or live). Tests mock RAM; none has been run against the real API.
*Is a van moving? (`integrations/ram_motion.py`; tests `tests/test_fleet_motion.py`, `tests/test_fleet_motion_browser.py`).* RAM's vehicle list has no speed and
`last_event` is only the latest event's NAME, so "moving" is worked out, in order, from: (1) position change between two cached polls (> 50 m, >= 30 s apart, >= 2 mph, under
100 mph implied; GPS drift, jumps and `sufficientGpsAccuracy: false` fixes are not movement; an estimated speed is shown to the nearest 5 mph and only ever as "about N mph"); (2) the
class of `last_event` from the MOVING / STOPPED / neutral tables at the top of that module, counted only while the event is under 15 minutes old (older = "No recent position
(last seen N min ago)"); (3) `engineRpm` > 0 with a neutral recent event = "Stopped, engine on". No extra RAM request is made (it reads the shared cached list); the last position per van
is kept in memory and, only for vans seen moving in the last 5 minutes, in the kv table. Owner-only Fleet diagnostics (`GET /api/fleet/diagnostics`, tool `fleet_diagnostics`, a section in the Fleet
drawer) list each van's raw event, its age, RPM, our classification and the reason - no coordinates, names or homes - for tuning the tables against RAM's portal. Never claim a speed we don't have.
(5) The staff report key is never in any payload or page: the "Copy staff report link" button fetches `/api/staff-report-address` when pressed
(`NO_TAIL_HINT` also stops the key's and the display password's last four characters showing). Tests: `tests/test_demo_guard.py`,
`tests/test_activity_log.py`, `tests/test_stt_status.py`, `tests/test_ramtracking_connection.py`, `tests/test_ramtracking_schema.py`,
`tests/test_staff_report_link.py`, `tests/test_console_browser_phase3.py`.
Phase 2 of the console redesign ("how Jarvis talks"): the question pop-up is a centred dialog over a real backdrop element (`.ask-scrim`) whose answers, and "Type my own answer", are real `<button>`s; Escape (anywhere on the page), Dismiss or a click on the backdrop closes it without sending, and the reply keeps an "Answer" button to bring it back. Tests: `tests/test_question_popup.py` (plus the node harness `tests/ask_dom_harness.js`).
What surrounds a reply is built from what really happened, not from text the model wrote: `jarvis/brain/trace.py`'s `TurnTrace` (`j.trace`) listens to the event bus (`EventBus.add_tap`) from the `thinking` event, notes each `tool` start, and each brain merges `j.trace.finish()` into its final `reply` event: `sources` (named from the tools used, with "(demo data)" where that source is still demo), `elapsed_ms`, `panel` (the pop-up with the detail: approvals if the turn queued something, else what the model asked for via `offer_next_steps`, else the pop-up of the tools used - the mapping is `_TOOL_INFO` there, so a new read tool should be added to it) and up to two `follow_ups` (only from the `offer_next_steps` tool, which changes nothing, is not an approval and is in `NO_RECURSE`). hud.js shows a working line above the reply from the live `tool` events (`.step`), then the source-and-time line, the pop-up button and follow-up chips under it. Stop (`stopEverything()`, or sending a new message) abandons the reply being written and sets `S.stopped` so late events of that turn are ignored until the next `user_message`; server-side it cancels the API-brain task (the whole turn is rolled back so the history stays valid) or interrupts Claude Code (`MaxBrain._stop_requested` - no half-answer is published or stored). "How Jarvis talks" is the `talk_style` setting (`natural` default = `owner_name`, `formal` = `owner_salutation`), shown in Settings (`#set-talk`, saved through the same Save changes bar) and under Connections > You and the business; `prompts.address_for()` picks the name and `TALK_NATURAL`/`TALK_FORMAL` are appended to the persona. It is a display preference like `owner_salutation`, not an owner-only setting. Tests: `tests/test_talk_style.py`, `tests/test_reply_extras.py`, `tests/test_streaming_stop.py`, `tests/test_console_browser_phase2.py`.

**Jarvis speaking up unprompted (`jarvis/services/proactive.py`, `j.proactive`; tests `tests/test_proactive.py`).** There is no
new transport: a Jarvis-initiated message is a `proactive` event on the same `EventBus`/`/ws` the chat already uses, handled by
`proactive(d)` in hud.js (shown as a message tagged "on my own"; read aloud by `say()` only when the session is in voice mode and
idle - see `proactiveMaySpeak()` - and never while the owner has text in the box). Everything proactive goes through
`Proactive`, which can only *tell*: it never approves, sends, queues or changes anything (a test greps the module for the
approval path), and every message is run through `history.redact_history` (the same redaction as the stored conversation) first.
- **Gate** (`held_reason()`/`_clear_to_speak()`): the `proactive_chat_enabled` setting (off by default; Settings > "Jarvis speaking
  up"), quiet hours (`proactive_quiet_start`/`_end`, HH:MM in `TIMEZONE`, may span midnight), `proactive_max_per_hour`, and not
  while the owner is mid-conversation (`user_busy()`, from `EventBus.last_event` timestamps: he spoke in the last 45s or a turn is
  still in flight; a message waits up to two minutes for that). A held message is not lost: `post()` leaves it as a quiet
  "Held back (...)" notification (no toast), and `announce()` does not remember it as seen, so the next run says it.
- **`post(text)`** = one message. **`announce(key, title, body)`** = a recurring finding: posted to the chat *and* Teams
  (`notifier.send_owner_update`, Teams only) only when its fingerprint differs from the last one (kv `proactive:seen:<key>`); an
  automation's reply starting `NOTHING_TO_REPORT` counts as nothing. Automations (`services/automations.py`) run silently
  when proactive is on - `events.quiet_turn` (a ContextVar, carried through `MaxBrain`'s worker explicitly) drops the
  `user_message`/`thinking`/`delta`/`tool`/`reply`/`error` events of that headless turn - and then `announce()` the result;
  with it off they behave exactly as before.
- **Background jobs**: `start(name, work)` runs a coroutine after the reply has been sent (max 5 at once, cancelled by
  `Jarvis.stop()`) and posts the result or the failure; `poller(...)` builds a `work` that polls a `check()` and posts status
  changes; the tools `watch_ci` (GitHub Actions on a branch of Jarvis's repo) and `watch_action` (a queued action's outcome - it
  only reads its status) use it. Both are read-only, are in `recruiter.NO_RECURSE`, and say so if proactive messages are off.
- **Quiet scheduled checks and the activity log** (`services/activity.py`, `j.activity`; console redesign phase 3): a scheduled
  check that finds nothing new posts NOTHING into the conversation. Every run is a row in `check_runs` (`ActivityLog.record(key, name,
  outcome, detail)`; outcomes `no_change`/`changed`/`failed`/`baseline`, kept 7 days) and `/api/status` -> `activity` (read every minute and after
  each reply; recording a run publishes nothing on the bus) gives today's runs per check; hud.js `renderActivity()` draws ONE collapsed `details.auto` line per check
  ("Pull request watch · 7 checks since 09:30, no change", opening to list each run with its time), above the conversation and never a
  chat message. Recorded by `pr_watch()`, by `AutomationService.run()` and by the scheduler's `_check()` wrapper (lone-worker and inbox
  sweeps). Automations now ALWAYS run as a `quiet_turn` and are always told to start with `NOTHING_TO_REPORT` when there is nothing new;
  a finding goes through `Proactive.tell()` (= `announce()` when speaking up is on; otherwise one message in the open chat, once, or a
  quiet notification if no chat is open). A check that DOES find a change still posts normally and is logged as `changed`.
- **Heartbeat stop rules** (`services/heartbeat.py`, applied by `AutomationService`): an automation that keeps returning
  `NOTHING_TO_REPORT` backs off instead of running forever. `automations` columns `nochange_streak` / `nochange_since` /
  `last_asked_at` / `never_slow` (migrated in `Database._migrate`). Every 6 consecutive no-change runs slows the *effective*
  interval one step up 10 min -> 30 min -> hourly -> 3 h -> daily (only steps above the owner's cron interval, which is the shortest
  gap between its next fires); it is done by `_run_guarded` skipping cron fires that come too soon after `last_run_at`, never by
  rewriting the schedule, so it can't go faster than configured. Any real change resets the streak. After 12 h with no change, ONE
  Teams-only message (subject `[Jarvis] ...`, via `notifier.send_owner_update(..., channels=("teams",))`) asks keep / slow down /
  delete, not repeated for 24 h, not overnight, and only for automations configured to run more often than every 12 h. Overnight
  (22:00-06:00 `TIMEZONE`) a non-urgent automation runs at most hourly. Exempt from all of it: a description/prompt mentioning
  life-safety, lone worker, out-of-hours alarms or keyholder (`EXEMPT_RE`), or the owner's `never_slow` flag (`create_automation`
  `never_slow_down`, or the `set_automation_options` tool - the answer to "keep it"). `list_automations` shows `effective_interval`,
  `no_change_streak`, `slowed_because`. The clock is `AutomationService.clock` so tests use a fake one. This covers owner-created
  automations only; the built-in pull request watch (`proactive_pr_watch_min`) is a separate interval job and is not slowed.
- **`HEARTBEAT.md`** (optional): house rules read at the start of every automation run (`heartbeat.read_checklist`: `<data_dir>/HEARTBEAT.md`,
  else the repo-root one; first 2000 chars) and appended to the run's prompt as guidance that never overrides the approval rules.
  Edit it to change rules like "only message on change" without a deploy.
- **Pull request watch**: `pr_watch()` (scheduled every `proactive_pr_watch_min` only when proactive is on and the Jarvis repo is
  connected) lists the open PRs read-only, compares with the last snapshot (kv `proactive:pr_watch`) and announces new PRs, CI
  passing/failing, conflicts and closed PRs. The first look only records a baseline.
- **Mute**: the HUD's per-session mute button (sessionStorage) sends `{"type": "proactive_mute", "muted": bool}` over `/ws`;
  `ws_events` then doesn't forward `proactive` events to that connection.
Don't add a path from anything proactive into `ActionExecutor`/`dispatch()` approvals, and keep new proactive sources behind
`post()`/`announce()` so the gate and redaction apply.

**Asynchronous tool calls (`jarvis/services/async_tools.py`, `j.async_tools`; tools `run_in_background` and `background_results`;
tests `tests/test_async_tools.py`).** Gemini-Live-style: start a slow tool in the background, carry on talking, get the result later
by a delivery policy. Nothing changes unless asked - ordinary tools still block; there is no `delivery` argument on them, only the
wrapper `run_in_background(tool, args, policy, timeout_s)`, which returns at once with a call number.
- **Policies** (default `WHEN_IDLE`, case-insensitive): `SILENT` = stored only, never spoken unprompted (read with
  `background_results`; works even with proactive chat off). `WHEN_IDLE` = `Proactive.post()` as it is: waits up to two minutes for
  no turn in progress, else kept as a quiet "Held back" notification. `INTERRUPT` = `Proactive.post(interrupt=True)`: skips only
  that wait (it cannot speak over audio already playing). A held INTERRUPT also leaves a "warning" notification. A scheduled check
  (`quiet_turn`) can only start `SILENT` work - `start()` forces it. Every policy goes through the existing gate: `proactive_chat_enabled` (WHEN_IDLE /
  INTERRUPT are refused at start when it is off), quiet hours, `proactive_max_per_hour`, a chat being open, the HUD's session mute
  and its rule about not speaking while listening/speaking. INTERRUPT never bypasses quiet hours or the hourly limit - a held
  urgent result is kept as a notification and in the store. A held result is not re-announced when quiet hours end.
- **Store**: table `background_calls` (`db.add_background_call`/`finish_background_call`/...): tool, redacted args, policy, status
  (`running`/`done`/`awaiting_approval`/`failed`/`timed_out`/`cancelled`/`interrupted`), redacted result (4000 chars), delivery
  (`silent`/`delivered`/`held: <why>`). Rows left `running` by a dead process are marked `interrupted` at start-up.
- **Safety**: the tool runs through the one `dispatch()`, so `approval=True` tools only queue (status `awaiting_approval`, the
  result is just "Suggested, not done: queued as action #N") and the module never calls approve/deny/`actions.queue` (a test greps
  it). Results are data, never instructions: `background_results` carries a note saying so and nothing feeds a result back to a
  model on its own. A failure or timeout is always stored with that status and, when it wasn't posted to the chat, leaves a
  "warning" notification - never silence. Not runnable in the background (`NOT_BACKGROUND`): the recruiter's `NO_RECURSE` tools
  (`recruit_agent`, `watch_ci`, `ask_user`, ...), the two new tools, and the van-location tools that read `j.asked_by` (it is
  empty or someone else's once the turn is over). Caps: at most `MAX_CONCURRENT` (3) at once, timeout 300s default, 900s
  maximum; `Jarvis.stop()` cancels them and marks them `cancelled`.
- **Untrusted output**: for tools that read outside content (`is_untrusted_output`: `email_*`, `repo_*`, `fsm_*`, `knowledge_*`, `web_*`,
  `pr_*` and a named list) the chat/transcript line is only "<tool> finished (status) - see background_results #N"; the raw (redacted)
  output stays in the row and `background_results` wraps it in UNTRUSTED TOOL OUTPUT markers. Tools that publish to the display,
  notify or message as a side effect (`show_on_display`, `send_update_to_owner`, drafts, briefings, reports...) are in
  `NOT_BACKGROUND` (a test scans handlers for `.publish(`/`notifier.`/`send_mail(`). Also capped at `MAX_STARTED_PER_HOUR` (30) starts,
  and finished rows older than 30 days are pruned at start-up. TODO for Team mode: tool lookup in `start()` must use the caller's
  allowed-tool set.
- Add a tool to `NOT_BACKGROUND` if it depends on the live turn; don't add a path from a result into approvals or settings.

## No OpenAI dependency: Azure Speech listening and Claude-designed graphics

Nothing in Jarvis needs an OpenAI key. Claude models cannot transcribe audio or generate images, so these are NOT Claude
equivalents - they are the accounts the company already has, and the docs/UI must say so honestly.

- **Speech-to-text** (`integrations/stt_chain.py`, `voice.py`): engines `azure` -> `deepgram` -> `whisper` -> browser
  (`DEFAULT_ORDER`; the chosen engine goes first). Azure Speech reuses `azure_speech_key` / `azure_speech_region` (the voice's
  own settings) and the short-audio REST endpoint `https://<region>.stt.speech.microsoft.com/...?language=en-GB`; it takes
  16 kHz mono PCM WAV or Ogg/Opus, NOT the WebM/MP4 a browser records, so hud.js `toWav16k()` converts in the browser
  before upload (decodeAudioData + OfflineAudioContext), `voice.prepare_azure_audio()` passes WAV/Ogg through and converts
  anything else with `ffmpeg` only if the server has one (else a plain non-transient error and the chain moves on), and clips
  over 60 s are refused. That endpoint has no phrase-list support, so `VOCAB` hints are not sent to Azure. `Settings.effective_stt`:
  `auto` -> Deepgram (live streaming exists only there) -> Azure -> Whisper -> browser; a chosen Whisper/Deepgram with no key
  falls through to Azure when Azure is configured (an old "Whisper" choice no longer breaks voice input). Whisper is optional
  and never required: `stt_problem` / the routine test / the voice connection test only mention a missing OpenAI key when
  Whisper is chosen and nothing else is configured. Keys and audio are never logged.
- **Graphics** (`services/images.py`, `services/adverts.py`): `image_provider` defaults to `claude`: `AdvertDesigner` asks the
  brain (`llm.structured`, tool-less) for ONE complete HTML document using a Salts brand brief (palette, size, margins, minimum
  text sizes, logo rules, company details from settings, accreditations from the register), with the headline / sub-text /
  visual / change requests quoted as untrusted data. The reply is rebuilt by `adverts.sanitise()` from an allowlist
  (elements, attributes, CSS; no script/link/form/frame/foreignObject/external reference of any kind; size, node and depth
  caps) and only the sanitised form is stored (table `adverts`, last version per design id, newest 40 kept, 20 revisions
  each). The model writes `SALTS_LOGO` for the logo; the server substitutes the downscaled data: URI after checking. The
  headline must survive verbatim or the design is retried, then replaced by a standard layout built in code (and the result
  says so). Nothing renders server-side: hud.js shows `document` in an iframe with `sandbox=""` and a CSP meta
  (`adverts.CSP`) and `advertToPng()` draws the XHTML `fragment` through an SVG foreignObject onto a canvas of the exact
  platform size (no library, nothing from a CDN). Routes: `GET /api/adverts/{id}` (JSON), `/{id}.html[?download=1]`,
  `POST /api/adverts/{id}/revise` (owner, same-origin). The `generate_image` tool is unchanged (no approval; drafts only;
  nothing posted) and its result says "designed graphic (HTML), not an AI photograph". The OpenAI path (`image_provider=openai`
  AND `image_api_key` set) is kept for backward compatibility; without a key an `openai` choice quietly uses Claude.
  Tests: `tests/test_adverts.py` (sanitiser, service), `tests/test_advert_browser.py` (Playwright: iframe sandbox/CSP, PNG
  export size and pixels per preset, revise), `tests/test_images.py` (OpenAI path), `tests/test_stt_azure.py`.
