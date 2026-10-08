"""The Jarvis conversation loop: streaming Claude replies with tool use.

The conversation history is append-only (thinking blocks stay valid and the
prompt cache keeps hitting); server-side compaction summarises it when it gets
long. A failed turn is rolled back as a whole so the history is always valid.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
from pydantic import ValidationError

from . import checkmode, coverage, llm
from .. import access
from ..events import quiet_turn
from ..redact import redact_text
from ..services.conversation_quality import TurnRecord
from ..services.tracking import requester_label
from .prompts import build_system, build_team_system
from .repeats import RepeatDetector, repeat_note
from .tools import SERVER_TOOLS, TOOLS, dispatch, serialise
from .web_research import WebTurn

log = logging.getLogger(__name__)
MAX_STEPS = 25


class JarvisBrain:
    """``caller``/``bus`` are only given for a Team-mode session (services/team_sessions.py): that brain has its own
    conversation, its own event bus (so nothing it says reaches the owner's console and nothing of the owner's reaches
    it), only the tools ``access.tool_allowed`` lets a team caller use, no web tools, a prompt that says who it is talking
    to, and it never writes the owner's transcript or metrics. With neither given it is the owner's brain exactly as before.

    ``check=True``: the question-check runner's brain (services/question_checks.py, brain/checkmode.py). The owner's prompt and
    tools (or a team caller's), but its own conversation (no earlier sessions in the prompt), its own bus, no transcript, no
    conversation-quality record, and every tool call dispatched in check mode - only pure reads run; nothing is sent, queued
    or written."""

    def __init__(self, j, caller: access.Caller | None = None, bus=None, check: bool = False):
        self.j = j
        self.s = j.settings
        self.caller = caller
        self.team = caller is not None and caller.is_team
        self.check = check
        self.isolated = self.team or check   # never writes the owner's transcript, metrics or "who is asking"
        self.bus = bus or j.bus
        self.client: anthropic.AsyncAnthropic = j.client
        self.messages: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._active: set[asyncio.Task] = set()
        self._repeats = RepeatDetector()
        self.last_extras: dict[str, Any] = {}  # the extras of the latest reply (the question-check runner reads its coverage)
        self.tools_by_name = {t.name: t for t in TOOLS if access.tool_allowed(t.name, caller)}
        self.web = bool(self.s.web_search_enabled and not self.team)
        self._own_tools = [t.definition() for t in self.tools_by_name.values()]
        self.tools = self._own_tools + (SERVER_TOOLS if self.web else [])
        # turns up to here are "earlier sessions" (a check brain sees none of them: each question starts clean)
        self._history_before = 0 if check else self.j.db.last_transcript_id()
        self.trace = None  # a team session describes its own turns (set by TeamSessions); the owner's is j.trace
        self.refresh_system()

    # ------------------------------------------------------------------ setup
    def refresh_system(self) -> None:
        if self.team:
            self.system = build_team_system(self.s, self.j.kb, self.caller)
            return
        self.system = build_system(self.s, self.j.kb, self.j.db, self.j.connections(),
                                   self.j.register.prompt_summary(), history_before_id=self._history_before,
                                   fsm_data=self.j.fsm_read.prompt_block())

    def reset(self) -> None:
        self.messages = []
        if not self.isolated:
            self._history_before = self.j.db.last_transcript_id()
        self.refresh_system()
        self.bus.publish("conversation_reset", None)

    @staticmethod
    def _attachment_blocks(attachments: list[dict[str, str]] | None) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for a in attachments or []:
            mime, data, name = a.get("mime", ""), a.get("data", ""), a.get("name", "file")
            if mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
            elif mime == "application/pdf":
                blocks.append({"type": "document", "title": name,
                               "source": {"type": "base64", "media_type": "application/pdf", "data": data}})
            else:
                try:
                    text = base64.b64decode(data).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    continue
                blocks.append({"type": "document", "title": name,
                               "source": {"type": "text", "media_type": "text/plain", "data": text[:400_000]}})
        return blocks

    # ------------------------------------------------------------------ tools
    async def _run_tool(self, block) -> dict[str, Any]:
        tool = self.tools_by_name.get(block.name)  # only what this brain's caller may use (all of them for the owner)
        bus = self.bus
        if tool is None:
            return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": f"Unknown tool {block.name}"}
        bus.publish("tool", {"id": block.id, "name": tool.name, "label": tool.label, "state": "start"})
        try:
            args = tool.model.model_validate(block.input if isinstance(block.input, dict) else {})
        except ValidationError as e:
            bus.publish("tool", {"id": block.id, "name": tool.name, "label": tool.label, "state": "error", "coverage": []})
            return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": json.dumps({"INVALID_INPUT": json.dumps(block.input, default=str),
                                           "errors": e.errors(include_url=False)}, default=str)}
        try:
            result = await dispatch(self.j, tool, args, caller=self.caller, check=self.check)
            # what the result says about the sources it read (truncated, scope off, sample data withheld...): labels only
            bus.publish("tool", {"id": block.id, "name": tool.name, "label": tool.label, "state": "done",
                                 "coverage": _facts(tool.name, args, result)})
            return {"type": "tool_result", "tool_use_id": block.id, "content": serialise(result)}
        except Exception as e:  # noqa: BLE001 - report tool failures back to Claude so it can adapt
            log.exception("Tool %s failed", tool.name)
            bus.publish("tool", {"id": block.id, "name": tool.name, "label": tool.label, "state": "error",
                                 "coverage": coverage.error_facts(tool.name, e)})
            return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": redact_text(f"{type(e).__name__}: {e}")[:2000]}

    # ------------------------------------------------------------------ main entry
    async def ask(self, text: str, mode: str = "typed", attachments: list[dict[str, str]] | None = None,
                  speaker: str | None = None) -> str:
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        try:
            async with self._lock:
                if self.check:
                    # A question check: every tool call in this turn is dispatched in check mode (reads only).
                    token = checkmode.active.set(True)
                    who = access.current_caller.set(self.caller)
                    try:
                        return await self._turn(text, mode, None, speaker)
                    finally:
                        access.current_caller.reset(who)
                        checkmode.active.reset(token)
                if self.team:
                    # A team turn never touches the owner's global "who is asking": the caller travels in a context
                    # variable (tools read it through tools._asker), so a team turn and an owner turn can overlap.
                    token = access.current_caller.set(self.caller)
                    try:
                        return await self._turn(text, mode, None, speaker)  # no attachments for a team session
                    finally:
                        access.current_caller.reset(token)
                # who is asking, for the out-of-hours van look-up log (read by the tracking tools)
                self.j.asked_by = requester_label(self.s, speaker, quiet_turn.get())
                # customer / site notes: whether this is a live console turn, and what outside content it reads
                memory = getattr(self.j, "entity_memory", None)
                state = memory.begin_turn(attachments=bool(attachments), caller=access.current_caller.get()) if memory else None
                try:
                    return await self._turn(text, mode, attachments, speaker)
                finally:
                    self.j.asked_by = ""
                    if memory is not None:
                        memory.end_turn(state)
        finally:
            if task is not None:
                self._active.discard(task)

    async def interrupt(self) -> bool:
        """Stop whatever Jarvis is currently generating, so the next message can start straight away."""
        stopped = False
        for t in list(self._active):
            if not t.done():
                t.cancel()
                stopped = True
        return stopped

    async def _turn(self, text: str, mode: str, attachments: list[dict[str, str]] | None,
                    speaker: str | None = None) -> str:
        bus, db = self.bus, self.j.db
        now = datetime.now(ZoneInfo(self.s.timezone))
        who = f" · from {speaker}" if speaker else ""
        tag = f"[{'spoken' if mode == 'voice' else 'typed'} · {now:%A %d %B %Y, %H:%M} UK time{who}]"
        note = repeat_note(self._repeats.check(text)) + _coverage_note(self.j, text, self.team)
        content = self._attachment_blocks(attachments) + [{"type": "text", "text": f"{tag}\n{note}{text}"}]
        rollback_to = len(self.messages)
        self.messages.append({"role": "user", "content": content})
        if not self.isolated:  # a team session / a question check is never written to the owner's transcript or metrics
            db.add_transcript("user", text)
        qt = (TurnRecord(self.j.quality, None, mode) if self.isolated  # a record with no id measures nothing
              else self.j.quality.begin(text, mode))  # conversation-quality metrics; never raises (see conversation_quality.py)
        bus.publish("user_message", {"text": text, "mode": mode,
                                     "attachments": [a.get("name") for a in attachments or []]})
        bus.publish("thinking", {"mode": mode, "turn_id": qt.turn_id})

        effort = self.s.voice_effort if mode == "voice" else self.s.chat_effort
        params = llm.request_params(self.s, effort, compaction=self.s.jarvis_compaction, model=self.s.model_for(mode))
        reply_parts: list[str] = []
        json_retries = 0
        # this turn's web budget (more for a question that clearly needs research, a hard cap per turn) and the sources
        # its web tools really used (brain/web_research.py)
        web = WebTurn.for_question(text)
        try:
            for _ in range(MAX_STEPS):
                tools = self._own_tools + web.tools() if self.web else self.tools
                # once a kind's budget is spent its tool stays defined (max_uses 1) and a short note asks the model to answer
                # from what it has - after the cached system prompt, so the cache still holds
                spent = web.spent_note() if self.web else ""
                system = self.system + [{"type": "text", "text": spent}] if spent else self.system
                try:
                    async with self.client.beta.messages.stream(
                        max_tokens=32000, system=system, messages=self.messages, tools=tools,
                        cache_control={"type": "ephemeral"}, **params,
                    ) as stream:
                        async for event in stream:
                            if event.type == "text":
                                qt.first_delta()
                                reply_parts.append(event.text)
                                bus.publish("delta", {"text": event.text, "mode": mode})
                            elif event.type == "content_block_start" and event.content_block.type == "server_tool_use":
                                label = "Searching the web" if event.content_block.name == "web_search" else "Reading a web page"
                                bus.publish("tool", {"id": event.content_block.id, "name": event.content_block.name,
                                                     "label": label, "state": "start"})
                        response = await stream.get_final_message()
                    json_retries = 0
                    if self.web:
                        web.note_response(response.content)
                except ValueError:
                    # Tool-input JSON the SDK could not parse at all: no tool_use id to answer, so retry the step.
                    json_retries += 1
                    if json_retries > 2:
                        raise
                    continue

                if response.stop_reason == "refusal":
                    del self.messages[rollback_to:]
                    msg = "I'm afraid I can't help with that one."
                    extras = self._trace_extras(msg)
                    bus.publish("reply", {"text": msg, "mode": mode, "replace": True, "turn_id": qt.turn_id, **extras})
                    if not self.isolated:
                        db.add_transcript("assistant", msg, coverage.as_stored(extras.get("coverage")))
                    qt.finish(msg, coverage=extras.get("coverage"))
                    self.last_extras = extras
                    return msg

                self.messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason == "pause_turn":
                    continue
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if not tool_uses:
                    break
                qt.tools(len(tool_uses))
                if response.stop_reason == "max_tokens":
                    results = [{"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                "content": "Tool input was cut off by max_tokens; send a shorter input."}
                               for b in tool_uses]
                else:
                    results = list(await asyncio.gather(*(self._run_tool(b) for b in tool_uses)))
                self.messages.append({"role": "user", "content": results})
                if reply_parts and not reply_parts[-1].endswith(("\n", " ")):
                    reply_parts.append("\n\n")
                    bus.publish("delta", {"text": "\n\n", "mode": mode})
            else:
                reply_parts.append("\n\n(I stopped there - that took more steps than I allow myself in one go.)")
        except asyncio.CancelledError:
            # The owner cut this turn off (Stop / barge-in / a new message). Roll the whole turn back like any other
            # failed one: stopped half-way through a tool call, the history would end on a tool_use with no result,
            # which the API rejects on every later message.
            del self.messages[rollback_to:]
            qt.finish("", ok=False, interrupted=True)
            raise
        except anthropic.APIError as e:
            del self.messages[rollback_to:]
            qt.finish("", ok=False)
            log.exception("Claude API error")
            status = getattr(e, "status_code", None)
            msg = ("I can't reach my language model right now - check the ANTHROPIC_API_KEY." if status in (401, 403)
                   else "I'm being rate limited - give me a moment and ask again." if status == 429
                   else "Something went wrong talking to my language model. Please try again.")
            bus.publish("error", {"message": msg, "detail": redact_text(e)[:300]})
            return msg
        except Exception as e:  # noqa: BLE001
            del self.messages[rollback_to:]
            qt.finish("", ok=False)
            log.exception("Turn failed")
            bus.publish("error", {"message": "Sorry, something went wrong on my side.",
                                "detail": redact_text(e)[:300]})
            return "Sorry, something went wrong on my side."

        reply = "".join(reply_parts).strip()
        extras = self._trace_extras(reply, web)
        if not self.isolated:
            db.add_transcript("assistant", reply, coverage.as_stored(extras.get("coverage")))
        qt.finish(reply, coverage=extras.get("coverage"))
        bus.publish("reply", {"text": reply, "mode": mode, "turn_id": qt.turn_id, **extras})
        self.last_extras = extras
        return reply

    def _trace_extras(self, reply: str = "", web: WebTurn | None = None) -> dict[str, Any]:
        """Source line / pop-up button / follow-ups / coverage / numbered web sources for the reply: the owner's trace, or the
        team session's (or the question check's) own."""
        trace = self.trace if self.isolated else self.j.trace
        if trace is None:
            return {}
        if web is not None:
            trace.add_web(web)
        return trace.finish(reply)


def _facts(name: str, args: Any, result: Any) -> list[dict[str, str]]:
    """coverage.call_facts, never raising: describing a result must not be able to break the tool call."""
    try:
        return coverage.call_facts(name, args, result)
    except Exception:  # noqa: BLE001
        log.exception("coverage facts failed for %s", name)
        return []


def _coverage_note(j, text: str, team: bool) -> str:
    """The line telling the model, before it answers, which sources this question needs that aren't connected. Never raises."""
    try:
        return coverage.turn_note(text, coverage.demo_map(j), team)
    except Exception:  # noqa: BLE001
        return ""
