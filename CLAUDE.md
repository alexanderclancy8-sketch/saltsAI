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

**The engineer-loop pattern (`fixer.py`, `security_watch.py`, `self_improve.py`).** All three download a
tarball snapshot of a repo into a temp dir (`GitHub.download_tree`), wrap it in `services/workspace.py`'s
`Workspace` (a virtual `/repo` root with `view`/`str_replace`/`create`/`grep`/`find`, path-confined so the model
can't escape the checkout), then run a bounded tool-call loop (`MAX_TURNS`, `cache_control` on the system
prompt) until the model calls a terminal tool (`submit_fix`/`submit_findings`/`submit_change` or `give_up`).
`fixer.py` and `self_improve.py` end in a PR; `security_watch.py` is read-only by design (its editor tool
rejects every command but `view`). `self_improve.py` is deliberately narrower than `fixer.py`: no merge step, no
deploy step, ever, not even behind an approval click - a human always merges it. Copy the shape of whichever of
these three is closest to a new engineer/review-style feature rather than starting from scratch.

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

**The HUD (`jarvis/web/hud.js`) talks to the backend over both a WebSocket (`/ws`, live/streaming - `thinking`/
`delta`/`tool`/`reply` events pushed through `jarvis/events.py`'s `EventBus`) and plain REST fallbacks
(`/api/chat/stream`, an SSE endpoint that forwards the same bus events, used only when the WebSocket is down).**
`handle(ev)` in hud.js is the single place both paths render into, so a new bus event type needs a case there
once, not per-transport. Voice wake-word listening for cost-free "always listening" runs the browser's own free
`SpeechRecognition` (`sentry` in hud.js) until it hears the wake word, then hands off to the configured paid STT
(Deepgram/Whisper) for the actual command, sleeping back to the free listener after a period of silence
(`extendFollowUp`/`checkSleep`) - don't reintroduce a fully continuous paid stream for "always listening" mode.
