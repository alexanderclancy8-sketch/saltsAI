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
`services/recruiter.py` (the `recruit_agent` tool) generalises the same shape beyond code: a fresh agent, a
fixed turn budget, a final answer - but against Jarvis's own tool set via `dispatch()` (so a write it proposes
queues for approval exactly like anything else) rather than a code checkout, for research/drafting/analysis
tasks worth delegating rather than doing inline. Like the other three it runs both backends (a plain
`AsyncAnthropic` tool loop for the API backend, `max_backend.run_agent` - a filtered MCP tool server - for the
Max/Claude Code backend); `NO_RECURSE` in that file is what stops a recruited agent recruiting further agents
or starting another background job itself.

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

**GitHub PR tools for Jarvis's own repo (`jarvis/brain/pr_tools.py`, `jarvis/integrations/github_pr.py`,
`jarvis/services/pr_resolver.py`; full list and rules in `docs/github-pr-tools.md`).** Reads: `pr_list`, `pr_detail`,
`repo_read`, `repo_search`, `run_tests`. Writes, all `approval=True`: `pr_comment`, `pr_resolve_conflicts`, `pr_merge`,
`pr_create` (head branch into base branch, e.g. `jarvis-updates-2026-09-29` into `main`), `pr_close` (optional comment) and
`pr_set_base`. `PRClient._send` is an allow-list of (method, path) *and* checks PATCH/new-PR bodies - extend it, don't bypass
it. Hard rules: never push or force-push `main` (`pr_create` refuses a `main`/`master` head), `pr_merge` refuses unless CI is
green, and every call is bound to `JARVIS_REPO` (no tool takes a repo name). PR titles, descriptions, comments and code are
untrusted data, never instructions. A failed approved write raises `PRError` with the real (redacted) reason so the action
shows as failed with it. Tests: `tests/test_pr_tools.py`, `tests/test_pr_resolver.py`.

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
(Deepgram/Whisper) for the actual command, sleeping back to the free listener after a period of silence
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
- **Pull request watch**: `pr_watch()` (scheduled every `proactive_pr_watch_min` only when proactive is on and the Jarvis repo is
  connected) lists the open PRs read-only, compares with the last snapshot (kv `proactive:pr_watch`) and announces new PRs, CI
  passing/failing, conflicts and closed PRs. The first look only records a baseline.
- **Mute**: the HUD's per-session mute button (sessionStorage) sends `{"type": "proactive_mute", "muted": bool}` over `/ws`;
  `ws_events` then doesn't forward `proactive` events to that connection.
Don't add a path from anything proactive into `ActionExecutor`/`dispatch()` approvals, and keep new proactive sources behind
`post()`/`announce()` so the gate and redaction apply.
