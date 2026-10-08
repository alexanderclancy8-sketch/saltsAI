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

from . import llm
from .. import access
from ..events import quiet_turn
from ..redact import redact_text
from ..services.conversation_quality import TurnRecord
from ..services.tracking import requester_label
from .prompts import build_system, build_team_system
from .repeats import RepeatDetector, repeat_note
from .tools import SERVER_TOOLS, TOOLS, dispatch, serialise

log = logging.getLogger(__name__)
MAX_STEPS = 25


class JarvisBrain:
    """``caller``/``bus`` are only given for a Team-mode session (services/team_sessions.py): that brain has its own
    conversation, its own event bus (so nothing it says reaches the owner's console and nothing of the owner's reaches
    it), only the tools ``access.tool_allowed`` lets a team caller use, no web tools, a prompt that says who it is talking
    to, and it never writes the owner's transcript or metrics. With neither given it is the owner's brain exactly as before."""

    def __init__(self, j, caller: access.Caller | None = None, bus=None):
        self.j = j
        self.s = j.settings
        self.caller = caller
        self.team = caller is not None and caller.is_team
        self.bus = bus or j.bus
        self.client: anthropic.AsyncAnthropic = j.client
        self.messages: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._active: set[asyncio.Task] = set()
        self._repeats = RepeatDetector()
        self.tools_by_name = {t.name: t for t in TOOLS if access.tool_allowed(t.name, caller)}
        self.tools = [t.definition() for t in self.tools_by_name.values()] + (
            SERVER_TOOLS if self.s.web_search_enabled and not self.team else [])
        self._history_before = self.j.db.last_transcript_id()  # turns up to here are "earlier sessions"
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
        if not self.team:
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
            bus.publish("tool", {"id": block.id, "name": tool.name, "label": tool.label, "state": "error"})
            return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": json.dumps({"INVALID_INPUT": json.dumps(block.input, default=str),
                                           "errors": e.errors(include_url=False)}, default=str)}
        try:
            result = await dispatch(self.j, tool, args, caller=self.caller)
            bus.publish("tool", {"id": block.id, "name": tool.name, "label": tool.label, "state": "done"})
            return {"type": "tool_result", "tool_use_id": block.id, "content": serialise(result)}
        except Exception as e:  # noqa: BLE001 - report tool failures back to Claude so it can adapt
            log.exception("Tool %s failed", tool.name)
            bus.publish("tool", {"id": block.id, "name": tool.name, "label": tool.label, "state": "error"})
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
        note = repeat_note(self._repeats.check(text))
        content = self._attachment_blocks(attachments) + [{"type": "text", "text": f"{tag}\n{note}{text}"}]
        rollback_to = len(self.messages)
        self.messages.append({"role": "user", "content": content})
        if not self.team:  # a team session is never written to the owner's transcript or metrics
            db.add_transcript("user", text)
        qt = (TurnRecord(self.j.quality, None, mode) if self.team  # a record with no id measures nothing
              else self.j.quality.begin(text, mode))  # conversation-quality metrics; never raises (see conversation_quality.py)
        bus.publish("user_message", {"text": text, "mode": mode,
                                     "attachments": [a.get("name") for a in attachments or []]})
        bus.publish("thinking", {"mode": mode, "turn_id": qt.turn_id})

        effort = self.s.voice_effort if mode == "voice" else self.s.chat_effort
        params = llm.request_params(self.s, effort, compaction=self.s.jarvis_compaction, model=self.s.model_for(mode))
        reply_parts: list[str] = []
        json_retries = 0
        try:
            for _ in range(MAX_STEPS):
                try:
                    async with self.client.beta.messages.stream(
                        max_tokens=32000, system=self.system, messages=self.messages, tools=self.tools,
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
                except ValueError:
                    # Tool-input JSON the SDK could not parse at all: no tool_use id to answer, so retry the step.
                    json_retries += 1
                    if json_retries > 2:
                        raise
                    continue

                if response.stop_reason == "refusal":
                    del self.messages[rollback_to:]
                    msg = "I'm afraid I can't help with that one."
                    bus.publish("reply", {"text": msg, "mode": mode, "replace": True, "turn_id": qt.turn_id,
                                          **self._trace_extras()})
                    if not self.team:
                        db.add_transcript("assistant", msg)
                    qt.finish(msg)
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
        if not self.team:
            db.add_transcript("assistant", reply)
        qt.finish(reply)
        bus.publish("reply", {"text": reply, "mode": mode, "turn_id": qt.turn_id, **self._trace_extras()})
        return reply

    def _trace_extras(self) -> dict[str, Any]:
        """Source line / pop-up button / follow-ups for the reply: the owner's trace, or the team session's own."""
        trace = self.trace if self.team else self.j.trace
        return trace.finish() if trace is not None else {}
