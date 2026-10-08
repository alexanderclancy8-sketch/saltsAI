"""Claude Max / Pro subscription backend.

Runs Jarvis through the Claude Agent SDK (Claude Code as a library), authenticated
with the long-lived token from `claude setup-token` (CLAUDE_CODE_OAUTH_TOKEN), so usage
comes out of the owner's subscription instead of API credits.

Jarvis' own tools are exposed to Claude as an in-process MCP server. Claude Code's
built-in shell and file-writing tools are switched off for the conversation; only
web search/fetch and reading uploaded attachments are allowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from . import checkmode, coverage, plugins
from .. import access
from ..redact import redact_text
from ..events import quiet_turn
from ..services.conversation_quality import TurnRecord
from ..services.tracking import requester_label
from .prompts import build_system, build_team_system, house_rules
from .repeats import RepeatDetector, repeat_note
from .tools import TOOLS, TOOLS_BY_NAME, dispatch, serialise
from .web_research import WebTurn
from ..services.entity_memory import turn_channel

log = logging.getLogger(__name__)
SERVER = "jarvis"
CHAT_BUILTINS = ["WebSearch", "WebFetch", "Read"]
BLOCKED = ["Bash", "Write", "Edit", "NotebookEdit", "KillShell", "Task"]
WEB_MATCHER = "WebSearch|WebFetch"   # Claude Code's own web tools: the per-turn web budget and source hooks
# For the engineer-loop callers of run_once() (self_improve.py, fixer.py) that request Write/Edit on purpose,
# confined to a throwaway Workspace checkout - everything BLOCKED disallows except those two.
ENGINEER_BLOCKED = ["Bash", "NotebookEdit", "KillShell", "Task"]


def sdk_env(settings) -> dict[str, str]:
    env = {}
    if settings.claude_code_oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token
    if os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY is set; Claude Code may bill the API instead of your subscription.")
    return env


def base_options(settings, *, model: str | None = None, **kw: Any):
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(model=model or settings.jarvis_model, env=sdk_env(settings), setting_sources=[],
                              permission_mode="dontAsk", **kw)


def build_sdk_tools(j, tools: list | None = None, caller: access.Caller | None = None, bus=None, check: bool = False) -> list:
    """``caller`` / ``bus``: for a Team-mode session's brain - every call is dispatched as that caller (so
    ``access.tool_allowed`` applies even if the SDK were somehow asked for another tool) and its tool events go to that
    session's own bus. The owner's brain passes neither. ``check``: a question check's brain - every call is dispatched in
    check mode (brain/checkmode.py: reads only)."""
    from claude_agent_sdk import tool

    events = bus or j.bus
    sdk_tools = []
    for t in (tools if tools is not None else TOOLS):
        async def handler(args: dict[str, Any], _t=t) -> dict[str, Any]:
            call_id = uuid.uuid4().hex[:8]
            events.publish("tool", {"id": call_id, "name": _t.name, "label": _t.label, "state": "start"})
            try:
                parsed = _t.model.model_validate(args or {})
                result = await dispatch(j, _t, parsed, caller=caller, check=check)
                events.publish("tool", {"id": call_id, "name": _t.name, "label": _t.label, "state": "done",
                                        "coverage": _facts(_t.name, parsed, result)})
                return {"content": [{"type": "text", "text": serialise(result)}]}
            except ValidationError as e:
                events.publish("tool", {"id": call_id, "name": _t.name, "label": _t.label, "state": "error", "coverage": []})
                return {"content": [{"type": "text", "text": json.dumps({"INVALID_INPUT": e.errors(include_url=False)},
                                                                         default=str)}], "is_error": True}
            except Exception as e:  # noqa: BLE001
                log.exception("Tool %s failed", _t.name)
                events.publish("tool", {"id": call_id, "name": _t.name, "label": _t.label, "state": "error",
                                        "coverage": coverage.error_facts(_t.name, e)})
                return {"content": [{"type": "text", "text": redact_text(f"{type(e).__name__}: {e}")[:2000]}],
                        "is_error": True}

        sdk_tools.append(tool(t.name, t.description, t.definition()["input_schema"])(handler))
    return sdk_tools


def build_mcp_server(j, tool_names: list[str] | None = None, caller: access.Caller | None = None, bus=None,
                     check: bool = False):
    from claude_agent_sdk import create_sdk_mcp_server

    tools = [t for t in TOOLS if tool_names is None or t.name in tool_names] if tool_names is not None else None
    if caller is not None:  # never expose a tool the caller may not use, whatever names were asked for
        tools = [t for t in (tools if tools is not None else TOOLS) if access.tool_allowed(t.name, caller)]
    return create_sdk_mcp_server(SERVER, tools=build_sdk_tools(j, tools, caller, bus, check))


def _facts(name: str, args: Any, result: Any) -> list[dict[str, str]]:
    """coverage.call_facts, never raising: describing a result must not be able to break the tool call."""
    try:
        return coverage.call_facts(name, args, result)
    except Exception:  # noqa: BLE001
        log.exception("coverage facts failed for %s", name)
        return []


def _tool_label(name: str) -> str:
    short = name.removeprefix(f"mcp__{SERVER}__")
    if short in TOOLS_BY_NAME:
        return TOOLS_BY_NAME[short].label
    return {"WebSearch": "Searching the web", "WebFetch": "Reading a web page", "Read": "Reading the attachment"}.get(
        short, short)


class _StopRequested(Exception):
    """The owner pressed Stop before the question was sent to Claude Code."""


class MaxBrain:
    """Same interface as JarvisBrain, backed by the Claude Agent SDK on a subscription.

    One Claude Code process is kept running between messages (ClaudeSDKClient), so a reply doesn't pay for
    starting Claude Code, loading the tools and reloading the conversation each time. The SDK client must be used
    from the task that connected it, so a single worker task owns it and handles messages one at a time.
    """

    def __init__(self, j, caller: access.Caller | None = None, bus=None, check: bool = False):
        """``check=True``: the question-check runner's brain (brain/checkmode.py) - its own bus and conversation, no
        transcript or metrics, no browsing plugin, and every tool call dispatched in check mode (reads only)."""
        self.j = j
        self.s = j.settings
        self.caller = caller
        self.team = caller is not None and caller.is_team
        self.check = check
        self.isolated = self.team or check
        self.bus = bus or j.bus
        self.trace = None  # a team session describes its own turns (set by TeamSessions); the owner's is j.trace
        self.last_extras: dict[str, Any] = {}
        self.tools = [t for t in TOOLS if access.tool_allowed(t.name, caller)]
        self.server = (build_mcp_server(j, [t.name for t in self.tools], caller, bus, check) if self.isolated
                       else build_mcp_server(j))
        self.session_id: str | None = None
        self.messages: list[dict[str, Any]] = []  # kept for interface parity (history lives in the session)
        self.uploads = self.s.data_dir / "uploads"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self._jobs: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._web = WebTurn()  # this turn's web budget and sources (replaced at the start of every turn)
        self._client = None
        self._client_key: tuple[str, str, str, str] | None = None  # (effort, model, system prompt, plugins) it started with
        self._fresh_start = False
        self._in_turn = False          # a chat turn is being generated right now
        self._stop_requested = False   # the owner pressed Stop during it: wind it down quietly, publish no reply
        self._repeats = RepeatDetector()
        self._history_before = 0 if check else self.j.db.last_transcript_id()  # turns up to here are "earlier sessions"
        self.refresh_system()

    def refresh_system(self) -> None:
        if self.team:
            blocks = build_team_system(self.s, self.j.kb, self.caller,
                                       rules=house_rules(self.j, self.caller))
        else:
            blocks = build_system(self.s, self.j.kb, self.j.db, self.j.connections(), self.j.register.prompt_summary(),
                                  history_before_id=self._history_before, fsm_data=self.j.fsm_read.prompt_block(),
                                  rules=house_rules(self.j, None))
        self.system = "\n\n".join(b["text"] for b in blocks)

    def reset(self) -> None:
        self.session_id = None
        self._fresh_start = True  # the worker restarts Claude Code without the old conversation
        if not self.isolated:
            self._history_before = self.j.db.last_transcript_id()
        self.refresh_system()
        self.bus.publish("conversation_reset", None)

    def _save_attachments(self, attachments: list[dict[str, str]] | None) -> list[str]:
        import base64

        paths = []
        for a in attachments or []:
            # (a PDF / Word / Excel / PowerPoint file arrives already read, as text, with the name to save it under)
            name = re.sub(r"[^A-Za-z0-9._-]", "_", a.get("save_as") or a.get("name") or "file")[:80]
            path = self.uploads / f"{datetime.now():%Y%m%d-%H%M%S}-{name}"
            try:
                path.write_bytes(base64.b64decode(a.get("data", "")))
                paths.append(str(path))
            except (ValueError, OSError):
                continue
        return paths

    # ------------------------------------------------------------------ worker
    def _ensure_worker(self) -> asyncio.Queue:
        if self._worker is None or self._worker.done():
            self._jobs = asyncio.Queue()
            self._worker = asyncio.create_task(self._run_worker(self._jobs))
        return self._jobs

    async def _submit(self, job: tuple) -> Any:
        future = asyncio.get_running_loop().create_future()
        await self._ensure_worker().put((*job, future))
        return await future

    async def ask(self, text: str, mode: str = "typed", attachments: list[dict[str, str]] | None = None,
                  speaker: str | None = None) -> str:
        # The worker task runs the turn, so a background (silent) turn has to say so explicitly - a context variable
        # set here would not reach it. See events.quiet_turn.
        if self.isolated:
            attachments = None  # a team session / a check has no attachments (and a team one no file reading at all)
        # (the asker's role travels the same way: a manager's turn on the shared brain is marked in a context variable by main.py)
        # (and so does whether it is a Teams chat turn: customer / site notes are never sent to Teams - services/entity_memory.py)
        return await self._submit(("ask", text, mode, attachments, speaker, quiet_turn.get(),
                                   None if self.isolated else access.current_caller.get(), turn_channel.get()))

    async def warm(self) -> None:
        """Start Claude Code ahead of the first message, so that one is quick too."""
        try:
            await self._submit(("warm",))
        except Exception as e:  # noqa: BLE001
            log.warning("Couldn't start Claude Code in advance: %s", e)

    async def interrupt(self) -> bool:
        """Stop whatever Claude Code is currently doing, so the next message can start straight away."""
        if self._in_turn:
            self._stop_requested = True  # also covers Stop pressed while Claude Code is still starting up
        if self._client is None:
            return self._in_turn
        try:
            await self._client.interrupt()
            return True
        except Exception as e:  # noqa: BLE001 - the connection may already be gone
            log.warning("Couldn't interrupt Claude Code (%s); reconnecting on the next message.", e)
            await self._disconnect()
            return True

    async def close(self) -> None:
        if self._worker and not self._worker.done() and self._jobs is not None:
            await self._jobs.put(None)
            try:
                await asyncio.wait_for(self._worker, timeout=10)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                self._worker.cancel()

    async def _run_worker(self, jobs: asyncio.Queue) -> None:
        try:
            while (job := await jobs.get()) is not None:
                *args, future = job
                try:
                    if args[0] == "warm":
                        await self._connected(self.s.voice_effort, self.s.model_for("voice"))
                        result = None
                    else:
                        result = await self._turn(*args[1:])
                    if not future.done():
                        future.set_result(result)
                except Exception as e:  # noqa: BLE001
                    await self._disconnect()  # start afresh (resuming the conversation) next time
                    if not future.done():
                        future.set_exception(e)
        finally:
            await self._disconnect()

    async def _connected(self, effort: str, model: str):
        """The running Claude Code client, restarted only if effort, model, instructions or the conversation changed."""
        # read-only browsing, only if switched on AND every safeguard is met; never for a team session or a check
        extra = plugins.PluginSetup() if self.isolated else plugins.chat_setup(self.s)
        key = (effort, model, self.system, extra.signature)
        if self._client is not None and self._client_key == key and not self._fresh_start:
            return self._client
        from claude_agent_sdk import ClaudeSDKClient

        await self._disconnect()
        if self._fresh_start:
            self.session_id, self._fresh_start = None, False
        hooks = extra.hooks() or {}
        if not self.team:   # (a team session has no web tools at all)
            hooks = self._with_web_hooks(hooks)
        more: dict[str, Any] = {"hooks": hooks} if hooks else {}
        options = base_options(
            self.s, model=model, system_prompt=self.system + extra.prompt, effort=effort,
            tools=[] if self.team else CHAT_BUILTINS, mcp_servers={SERVER: self.server, **extra.mcp_servers},
            allowed_tools=[f"mcp__{SERVER}__{t.name}" for t in self.tools] + ([] if self.team else CHAT_BUILTINS)
            + extra.allowed_tools,
            disallowed_tools=BLOCKED + (["WebSearch", "WebFetch", "Read"] if self.team else []),
            include_partial_messages=True, resume=self.session_id, max_turns=30, cwd=str(self.uploads), **more)
        started = time.monotonic()
        client = ClaudeSDKClient(options=options)
        await client.connect()
        log.info("Claude Code started in %.1fs (effort %s, model %s)", time.monotonic() - started, effort, model)
        self._client, self._client_key = client, key
        return client

    # ------------------------------------------------------------------ web budget and sources (brain/web_research.py)
    def _with_web_hooks(self, hooks: dict[str, Any]) -> dict[str, Any]:
        """Claude Code's own WebSearch / WebFetch get the same per-turn budget as the API brain's web tools (a PreToolUse hook
        that can only DENY, once the turn's cap is spent) and the same record of the sources they really returned (a
        PostToolUse hook that only reads). Both look at ``self._web``, which every turn replaces."""
        from claude_agent_sdk import HookMatcher

        out = dict(hooks)
        out["PreToolUse"] = list(out.get("PreToolUse") or []) + [HookMatcher(matcher=WEB_MATCHER, hooks=[self._web_guard])]
        out["PostToolUse"] = list(out.get("PostToolUse") or []) + [HookMatcher(matcher=WEB_MATCHER, hooks=[self._web_seen])]
        return out

    async def _web_guard(self, input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        try:
            reason = self._web.allow(str(input_data.get("tool_name", "")))
        except Exception:  # a bug here refuses the call, never lets it through uncounted  # noqa: BLE001
            reason = "the web budget couldn't be checked"
        if not reason:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": f"Refused by Jarvis: {reason}"}}

    async def _web_seen(self, input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        self._web.note_sdk_result(input_data.get("tool_name"), input_data.get("tool_input"), input_data.get("tool_response"))
        return {}

    async def _disconnect(self) -> None:
        client, self._client, self._client_key = self._client, None, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:  # noqa: BLE001
                log.debug("Claude Code client disconnect: %s", e)

    async def _turn(self, text: str, mode: str, attachments: list[dict[str, str]] | None,
                    speaker: str | None = None, quiet: bool = False, asker: access.Caller | None = None,
                    channel: str = "console") -> str:
        token = quiet_turn.set(quiet)
        if self.check:
            ctoken = checkmode.active.set(True)
            who = access.current_caller.set(self.caller)
            try:
                return await self._turn_events(text, mode, attachments, speaker)
            finally:
                access.current_caller.reset(who)
                checkmode.active.reset(ctoken)
                quiet_turn.reset(token)
        if self.team:
            # the caller travels with the turn, never in the shared j.asked_by that belongs to the owner's turn
            who = access.current_caller.set(self.caller)
            try:
                return await self._turn_events(text, mode, attachments, speaker)
            finally:
                access.current_caller.reset(who)
                quiet_turn.reset(token)
        # who is asking, for the out-of-hours van look-up log (read by the tracking tools)
        self.j.asked_by = requester_label(self.s, speaker, quiet)
        role = access.current_caller.set(asker)  # (always set, even to None: the worker task was created inside SOME turn's context)
        memory = getattr(self.j, "entity_memory", None)
        state = (memory.begin_turn(quiet=quiet, channel=channel, attachments=bool(attachments), caller=asker)
                 if memory is not None else None)
        try:
            return await self._turn_events(text, mode, attachments, speaker)
        finally:
            self.j.asked_by = ""
            if memory is not None:
                memory.end_turn(state)
            access.current_caller.reset(role)
            quiet_turn.reset(token)

    async def _turn_events(self, text: str, mode: str, attachments: list[dict[str, str]] | None,
                           speaker: str | None = None) -> str:
        from claude_agent_sdk import ResultMessage, StreamEvent

        bus, db = self.bus, self.j.db
        now = datetime.now(ZoneInfo(self.s.timezone))
        who = f" · from {speaker}" if speaker else ""
        tag = f"[{'spoken' if mode == 'voice' else 'typed'} · {now:%A %d %B %Y, %H:%M} UK time{who}]"
        repeat = repeat_note(self._repeats.check(text))
        try:  # which sources this question needs that aren't connected, so the answer names the gap (brain/coverage.py)
            repeat += coverage.turn_note(text, coverage.demo_map(self.j), self.team)
        except Exception:  # noqa: BLE001
            pass
        files = self._save_attachments(attachments)
        note =("\n\nAttached files (open them with the Read tool): " + ", ".join(files)) if files else ""
        if not self.isolated:  # a team session / a check is never written to the owner's transcript or metrics
            db.add_transcript("user", text)
        qt = (TurnRecord(self.j.quality, None, mode) if self.isolated  # a record with no id measures nothing
              else self.j.quality.begin(text, mode))  # conversation-quality metrics; never raises (see conversation_quality.py)
        bus.publish("user_message", {"text": text, "mode": mode, "attachments": [a.get("name") for a in attachments or []]})
        bus.publish("thinking", {"mode": mode, "turn_id": qt.turn_id})

        started = time.monotonic()
        first_words: float | None = None
        parts: list[str] = []
        result = None
        self._web = WebTurn.for_question(text)
        self._in_turn, self._stop_requested = True, False
        try:
            client = await self._connected(self.s.voice_effort if mode == "voice" else self.s.chat_effort,
                                           self.s.model_for(mode))
            if self._stop_requested:
                raise _StopRequested()  # stopped before it even began: don't send the question at all
            await client.query(f"{tag}\n{repeat}{text}{note}")
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    ev = msg.event or {}
                    etype = ev.get("type")
                    if etype == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                        chunk = ev["delta"].get("text", "")
                        if first_words is None:
                            first_words = time.monotonic() - started
                            qt.first_delta()
                        parts.append(chunk)
                        bus.publish("delta", {"text": chunk, "mode": mode})
                    elif etype == "content_block_start":
                        block = ev.get("content_block") or {}
                        if block.get("type") in ("tool_use", "server_tool_use"):
                            qt.tools()
                        if block.get("type") in ("tool_use", "server_tool_use") and not str(block.get("name", "")).startswith("mcp__"):
                            bus.publish("tool", {"id": block.get("id"), "name": block.get("name"),
                                                 "label": _tool_label(block.get("name", "")), "state": "start"})
                    elif etype == "message_start" and parts and not parts[-1].endswith("\n"):
                        parts.append("\n\n")
                        bus.publish("delta", {"text": "\n\n", "mode": mode})
                elif isinstance(msg, ResultMessage):
                    result = msg
                    self.session_id = msg.session_id or self.session_id
        except asyncio.CancelledError:
            qt.finish("", ok=False, interrupted=True)  # the owner cut this turn off (Stop / barge-in / a new message)
            raise
        except Exception as e:  # noqa: BLE001
            if self._stop_requested:  # the stop broke the connection on its way out: not an error worth showing
                qt.finish("", ok=False, interrupted=True)
                await self._disconnect()
                return "".join(parts).strip()
            qt.finish("", ok=False)
            log.exception("Claude Agent SDK turn failed")
            await self._disconnect()
            msg = ("I couldn't reach Claude through your subscription - check CLAUDE_CODE_OAUTH_TOKEN "
                   "(run `claude setup-token`)." if "auth" in str(e).lower() or "login" in str(e).lower()
                   else "Something went wrong talking to Claude. Please try again.")
            bus.publish("error", {"message": msg, "detail": redact_text(e)[:300]})
            return msg
        finally:
            self._in_turn = False
        if self._stop_requested:
            # Stop was pressed: the console already dropped this reply, so don't publish or store a half-answer.
            qt.finish("", ok=False, interrupted=True)
            log.info("%s reply stopped by the owner after %.1fs", mode, time.monotonic() - started)
            return "".join(parts).strip()
        log.info("%s reply: first words after %s, finished after %.1fs", mode,
                 f"{first_words:.1f}s" if first_words is not None else "-", time.monotonic() - started)
        if result is not None and result.is_error:
            detail = str(result.result or result.errors or "")
            limit = "limit" in detail.lower()
            # The SDK's own error/result text is an internal diagnostic (SDK error codes, stop reasons) meant
            # for logs, not something to read out to the owner - surfacing it raw once showed up as literally
            # "Sorry, that didn't work: ['[ede_diagnostic] result_type=user ...']" on the display/voice reply.
            qt.finish("", ok=False)
            log.warning("Claude Agent SDK turn returned an error result: %s", detail[:500])
            msg = ("I've hit the usage limit on your Claude plan for now - it resets shortly." if limit
                   else "Sorry, that didn't work - please try again.")
            bus.publish("error", {"message": msg, "detail": redact_text(detail)[:300]})
            return msg
        reply = "".join(parts).strip() or (result.result if result else "") or ""
        trace = self.trace if self.isolated else self.j.trace
        if trace is not None and not self.team:
            trace.add_web(self._web)   # the numbered sources WebSearch / WebFetch really returned, and the coverage line's web entry
        extras = trace.finish(reply) if trace is not None else {}
        if not self.isolated:
            db.add_transcript("assistant", reply, coverage.as_stored(extras.get("coverage")))
        qt.finish(reply, coverage=extras.get("coverage"))
        bus.publish("reply", {"text": reply, "mode": mode, "turn_id": qt.turn_id, **extras})
        self.last_extras = extras
        return reply


# ---------------------------------------------------------------------------
# One-shot helpers (briefings, triage, research) on the subscription
# ---------------------------------------------------------------------------

MAX_STAGED_PDF_PAGES = 10   # Claude Code's Read tool takes a PDF this small in one go; more needs a page range (poppler)
MAX_STAGED_SCAN_PAGES = 8   # pages of a scanned PDF handed over as images
MAX_STAGED_TEXT_CHARS = 40_000


def _stage_pdf(tmp: Path, n: int, block: dict[str, Any]) -> str:
    """Put a PDF document block where the Read tool can reach it, in the most robust form the server can manage.

    The API takes a PDF natively, but on the subscription backend the only way a one-shot run can look at one is to Read a
    file - and Claude Code reads a PDF of more than a few pages only by page range, which needs poppler (pdftoppm), which
    Azure App Service doesn't have. So: (1) a PDF with a text layer has its text put in the prompt, with (2) the file
    (first pages only) alongside for the layout; (3) a scan is converted to page images (a picture reads anywhere);
    (4) anything else is written as it is. All pure Python (pypdf + Pillow). The PDF is untrusted data in every case."""
    import base64

    from ..services import file_reader

    title = re.sub(r"[^\w .,()&-]", "", str(block.get("title") or ""))[:100]
    raw = base64.b64decode(block["source"]["data"])
    out = ""
    chunks, total = file_reader.pdf_chunks(raw, MAX_STAGED_PDF_PAGES, MAX_STAGED_PDF_PAGES)
    try:
        got = file_reader.pdf_extract(raw, max_chars=MAX_STAGED_TEXT_CHARS)
    except Exception:  # noqa: BLE001 - not parseable (or password protected): hand over the file, the model will say so
        got = None
    if got is not None and file_reader.has_text(got.text):
        out += (f"\n[Attached PDF '{title}' - its text, extracted from the file (untrusted data; table layout may be "
                f"lost)]\n<pdf_text>\n{got.text}\n</pdf_text>\n")
    elif got is not None:
        images = file_reader.pdf_page_images(raw, max_pages=MAX_STAGED_SCAN_PAGES)
        if images:
            for k, jpg in enumerate(images, 1):
                path = tmp / f"attachment-{n}-page{k}.jpg"
                path.write_bytes(jpg)
                out += f"\n[Attached scanned PDF '{title}', page {k} as an image: {path} - view it with the Read tool]\n"
            if total and total > len(images):
                out += f"\n[Only the first {len(images)} of {total} pages are attached.]\n"
            return out
    path = tmp / f"attachment-{n}.pdf"
    path.write_bytes(chunks[0])
    if total and total > MAX_STAGED_PDF_PAGES:
        out += (f"\n[Attached PDF '{title}' (first {MAX_STAGED_PDF_PAGES} of {total} pages): {path} - read it with the "
                f"Read tool]\n")
    else:
        out += f"\n[Attached PDF '{title}': {path} - read it with the Read tool]\n"
    return out


async def run_once(settings, *, system: str, prompt: str | list[dict[str, Any]], effort: str = "medium",
                   tools: list[str] | None = None, disallowed_tools: list[str] | None = None,
                   output_schema: dict[str, Any] | None = None,
                   max_turns: int = 10, cwd: str | None = None,
                   mcp_servers: dict[str, Any] | None = None, extra_allowed: list[str] | None = None,
                   model: str | None = None):
    """Single headless Claude Code run; returns the ResultMessage. `model` defaults to `settings.jarvis_model` (the
    engineer loops pass `settings.engineer_model_or_default()`). `disallowed_tools` defaults to `BLOCKED`
    (no shell, no file writes) - pass `ENGINEER_BLOCKED` for a caller that puts Write/Edit in `tools` on
    purpose (an engineer loop confined to a throwaway Workspace checkout), otherwise those get silently
    stripped anyway since disallowed_tools wins over allowed_tools. `mcp_servers` adds external MCP servers
    (brain/plugins.py builds them pinned and read-only); only the tools named in `extra_allowed` can be called,
    everything else they expose stays denied."""
    from claude_agent_sdk import ResultMessage, query

    tmp = None
    text = prompt if isinstance(prompt, str) else ""
    if isinstance(prompt, list):  # content blocks: write images to disk for the Read tool
        import base64

        tmp = Path(tempfile.mkdtemp(prefix="jarvis-"))
        cwd = str(tmp)
        tools = list(tools or []) + ["Read"]
        for n, block in enumerate(prompt):
            if block.get("type") == "text":
                text += block["text"] + "\n"
            elif block.get("type") == "image":
                ext = block["source"]["media_type"].split("/")[-1]
                path = tmp / f"attachment-{n}.{ext}"
                path.write_bytes(base64.b64decode(block["source"]["data"]))
                text += f"\n[Attached image: {path} - view it with the Read tool]\n"
            elif block.get("type") == "document" and block["source"].get("type") == "base64":
                text += await asyncio.to_thread(_stage_pdf, tmp, n, block)
    kw: dict[str, Any] = {"system_prompt": system, "effort": effort, "tools": tools or [],
                          "allowed_tools": tools or [],
                          "disallowed_tools": BLOCKED if disallowed_tools is None else disallowed_tools,
                          "max_turns": max_turns}
    if output_schema:
        kw["output_format"] = {"type": "json_schema", "schema": output_schema}
    if mcp_servers and extra_allowed:  # a server with nothing allowed would only add attack surface
        kw["mcp_servers"] = mcp_servers
        kw["allowed_tools"] = list(kw["allowed_tools"]) + list(extra_allowed)
    if cwd:
        kw["cwd"] = cwd
    result = None
    try:
        async for msg in query(prompt=text, options=base_options(settings, model=model, **kw)):
            if isinstance(msg, ResultMessage):
                result = msg
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    if result is not None and getattr(result, "subtype", None) == "error_max_turns":
        # Claude Code ends a run that hit max_turns with this result subtype - which may or may not set is_error,
        # and never carries structured output - so name it explicitly instead of letting it surface as a parse error.
        raise MaxTurnsExceeded(f"Claude run stopped after hitting its turn limit ({max_turns}) without finishing.")
    if result is None:
        raise RuntimeError("Claude run failed: the stream ended without a final result message")
    if result.is_error:
        raise RuntimeError(f"Claude run failed: {getattr(result, 'errors', None) or getattr(result, 'result', None)}")
    return result


class MaxTurnsExceeded(RuntimeError):
    """A one-shot Claude Agent SDK run used up its turn budget before producing a final answer."""


async def run_agent(settings, j, *, system: str, prompt: str, tool_names: list[str], effort: str = "medium",
                    max_turns: int = 12) -> str:
    """Headless Claude Code run against a filtered subset of Jarvis's own tools, exposed as its own in-process
    MCP server the same way `MaxBrain` exposes the full set - used by `services/recruiter.py` for a recruited
    sub-agent's Max-backend path. Any write one of those tools attempts still goes through `dispatch()`'s
    approval gate exactly as it would from the main conversation."""
    from claude_agent_sdk import ResultMessage, query

    server = build_mcp_server(j, tool_names)
    options = base_options(settings, system_prompt=system, effort=effort, tools=CHAT_BUILTINS,
                           mcp_servers={SERVER: server},
                           allowed_tools=[f"mcp__{SERVER}__{n}" for n in tool_names] + CHAT_BUILTINS,
                           disallowed_tools=BLOCKED, max_turns=max_turns)
    result = None
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            result = msg
    if result is None or result.is_error:
        raise RuntimeError(f"Recruited agent failed: {getattr(result, 'errors', None) or getattr(result, 'result', None)}")
    return (result.result or "").strip()


def parse_structured(result, schema: type[BaseModel]) -> BaseModel:
    if result.structured_output:
        return schema.model_validate(result.structured_output)
    match = re.search(r"\{.*\}", result.result or "", re.S)
    if not match:
        raise RuntimeError("No JSON in Claude's reply")
    return schema.model_validate_json(match.group(0))
