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
from .prompts import build_system
from .tools import SERVER_TOOLS, TOOLS, TOOLS_BY_NAME, serialise

log = logging.getLogger(__name__)
MAX_STEPS = 25


class JarvisBrain:
    def __init__(self, j):
        self.j = j
        self.s = j.settings
        self.client: anthropic.AsyncAnthropic = j.client
        self.messages: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self.tools = [t.definition() for t in TOOLS] + (SERVER_TOOLS if self.s.web_search_enabled else [])
        self.refresh_system()

    # ------------------------------------------------------------------ setup
    def refresh_system(self) -> None:
        self.system = build_system(self.s, self.j.kb, self.j.db, self.j.connections(),
                                   self.j.register.prompt_summary())

    def reset(self) -> None:
        self.messages = []
        self.refresh_system()
        self.j.bus.publish("conversation_reset", None)

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
        tool = TOOLS_BY_NAME.get(block.name)
        bus = self.j.bus
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
            result = await tool.handler(self.j, args)
            bus.publish("tool", {"id": block.id, "name": tool.name, "label": tool.label, "state": "done"})
            return {"type": "tool_result", "tool_use_id": block.id, "content": serialise(result)}
        except Exception as e:  # noqa: BLE001 - report tool failures back to Claude so it can adapt
            log.exception("Tool %s failed", tool.name)
            bus.publish("tool", {"id": block.id, "name": tool.name, "label": tool.label, "state": "error"})
            return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": f"{type(e).__name__}: {e}"[:2000]}

    # ------------------------------------------------------------------ main entry
    async def ask(self, text: str, mode: str = "typed", attachments: list[dict[str, str]] | None = None) -> str:
        async with self._lock:
            return await self._turn(text, mode, attachments)

    async def _turn(self, text: str, mode: str, attachments: list[dict[str, str]] | None) -> str:
        bus, db = self.j.bus, self.j.db
        now = datetime.now(ZoneInfo(self.s.timezone))
        tag = f"[{'spoken' if mode == 'voice' else 'typed'} · {now:%A %d %B %Y, %H:%M} UK time]"
        content = self._attachment_blocks(attachments) + [{"type": "text", "text": f"{tag}\n{text}"}]
        rollback_to = len(self.messages)
        self.messages.append({"role": "user", "content": content})
        db.add_transcript("user", text)
        bus.publish("user_message", {"text": text, "mode": mode,
                                     "attachments": [a.get("name") for a in attachments or []]})
        bus.publish("thinking", {"mode": mode})

        effort = self.s.voice_effort if mode == "voice" else self.s.chat_effort
        params = llm.request_params(self.s, effort, compaction=self.s.jarvis_compaction)
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
                    bus.publish("reply", {"text": msg, "mode": mode, "replace": True})
                    db.add_transcript("assistant", msg)
                    return msg

                self.messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason == "pause_turn":
                    continue
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if not tool_uses:
                    break
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
        except anthropic.APIError as e:
            del self.messages[rollback_to:]
            log.exception("Claude API error")
            status = getattr(e, "status_code", None)
            msg = ("I can't reach my language model right now - check the ANTHROPIC_API_KEY." if status in (401, 403)
                   else "I'm being rate limited - give me a moment and ask again." if status == 429
                   else "Something went wrong talking to my language model. Please try again.")
            bus.publish("error", {"message": msg, "detail": str(e)[:300]})
            return msg
        except Exception as e:  # noqa: BLE001
            del self.messages[rollback_to:]
            log.exception("Turn failed")
            bus.publish("error", {"message": "Sorry, something went wrong on my side.", "detail": str(e)[:300]})
            return "Sorry, something went wrong on my side."

        reply = "".join(reply_parts).strip()
        db.add_transcript("assistant", reply)
        bus.publish("reply", {"text": reply, "mode": mode})
        return reply
