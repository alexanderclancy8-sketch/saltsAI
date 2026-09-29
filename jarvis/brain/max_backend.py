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

from .prompts import build_system
from .tools import TOOLS, TOOLS_BY_NAME, dispatch, serialise

log = logging.getLogger(__name__)
SERVER = "jarvis"
CHAT_BUILTINS = ["WebSearch", "WebFetch", "Read"]
BLOCKED = ["Bash", "Write", "Edit", "NotebookEdit", "KillShell", "Task"]


def sdk_env(settings) -> dict[str, str]:
    env = {}
    if settings.claude_code_oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token
    if os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY is set; Claude Code may bill the API instead of your subscription.")
    return env


def base_options(settings, **kw: Any):
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(model=settings.jarvis_model, env=sdk_env(settings), setting_sources=[],
                              permission_mode="dontAsk", **kw)


def build_sdk_tools(j) -> list:
    from claude_agent_sdk import tool

    sdk_tools = []
    for t in TOOLS:
        async def handler(args: dict[str, Any], _t=t) -> dict[str, Any]:
            call_id = uuid.uuid4().hex[:8]
            j.bus.publish("tool", {"id": call_id, "name": _t.name, "label": _t.label, "state": "start"})
            try:
                parsed = _t.model.model_validate(args or {})
                result = await dispatch(j, _t, parsed)
                j.bus.publish("tool", {"id": call_id, "name": _t.name, "label": _t.label, "state": "done"})
                return {"content": [{"type": "text", "text": serialise(result)}]}
            except ValidationError as e:
                j.bus.publish("tool", {"id": call_id, "name": _t.name, "label": _t.label, "state": "error"})
                return {"content": [{"type": "text", "text": json.dumps({"INVALID_INPUT": e.errors(include_url=False)},
                                                                         default=str)}], "is_error": True}
            except Exception as e:  # noqa: BLE001
                log.exception("Tool %s failed", _t.name)
                j.bus.publish("tool", {"id": call_id, "name": _t.name, "label": _t.label, "state": "error"})
                return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"[:2000]}], "is_error": True}

        sdk_tools.append(tool(t.name, t.description, t.definition()["input_schema"])(handler))
    return sdk_tools


def build_mcp_server(j):
    from claude_agent_sdk import create_sdk_mcp_server

    return create_sdk_mcp_server(SERVER, tools=build_sdk_tools(j))


def _tool_label(name: str) -> str:
    short = name.removeprefix(f"mcp__{SERVER}__")
    if short in TOOLS_BY_NAME:
        return TOOLS_BY_NAME[short].label
    return {"WebSearch": "Searching the web", "WebFetch": "Reading a web page", "Read": "Reading the attachment"}.get(
        short, short)


class MaxBrain:
    """Same interface as JarvisBrain, backed by the Claude Agent SDK on a subscription.

    One Claude Code process is kept running between messages (ClaudeSDKClient), so a reply doesn't pay for
    starting Claude Code, loading the tools and reloading the conversation each time. The SDK client must be used
    from the task that connected it, so a single worker task owns it and handles messages one at a time.
    """

    def __init__(self, j):
        self.j = j
        self.s = j.settings
        self.server = build_mcp_server(j)
        self.session_id: str | None = None
        self.messages: list[dict[str, Any]] = []  # kept for interface parity (history lives in the session)
        self.uploads = self.s.data_dir / "uploads"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self._jobs: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._client = None
        self._client_key: tuple[str, str] | None = None  # (effort, system prompt) the client was started with
        self._fresh_start = False
        self.refresh_system()

    def refresh_system(self) -> None:
        blocks = build_system(self.s, self.j.kb, self.j.db, self.j.connections(), self.j.register.prompt_summary())
        self.system = "\n\n".join(b["text"] for b in blocks)

    def reset(self) -> None:
        self.session_id = None
        self._fresh_start = True  # the worker restarts Claude Code without the old conversation
        self.refresh_system()
        self.j.bus.publish("conversation_reset", None)

    def _save_attachments(self, attachments: list[dict[str, str]] | None) -> list[str]:
        import base64

        paths = []
        for a in attachments or []:
            name = re.sub(r"[^A-Za-z0-9._-]", "_", a.get("name") or "file")[:80]
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
        return await self._submit(("ask", text, mode, attachments, speaker))

    async def warm(self) -> None:
        """Start Claude Code ahead of the first message, so that one is quick too."""
        try:
            await self._submit(("warm",))
        except Exception as e:  # noqa: BLE001
            log.warning("Couldn't start Claude Code in advance: %s", e)

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
                        await self._connected(self.s.voice_effort)
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

    async def _connected(self, effort: str):
        """The running Claude Code client, restarted only if effort, instructions or the conversation changed."""
        key = (effort, self.system)
        if self._client is not None and self._client_key == key and not self._fresh_start:
            return self._client
        from claude_agent_sdk import ClaudeSDKClient

        await self._disconnect()
        if self._fresh_start:
            self.session_id, self._fresh_start = None, False
        options = base_options(
            self.s, system_prompt=self.system, effort=effort,
            tools=CHAT_BUILTINS, mcp_servers={SERVER: self.server},
            allowed_tools=[f"mcp__{SERVER}__{t.name}" for t in TOOLS] + CHAT_BUILTINS, disallowed_tools=BLOCKED,
            include_partial_messages=True, resume=self.session_id, max_turns=30, cwd=str(self.uploads))
        started = time.monotonic()
        client = ClaudeSDKClient(options=options)
        await client.connect()
        log.info("Claude Code started in %.1fs (effort %s)", time.monotonic() - started, effort)
        self._client, self._client_key = client, key
        return client

    async def _disconnect(self) -> None:
        client, self._client, self._client_key = self._client, None, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:  # noqa: BLE001
                log.debug("Claude Code client disconnect: %s", e)

    async def _turn(self, text: str, mode: str, attachments: list[dict[str, str]] | None,
                    speaker: str | None = None) -> str:
        from claude_agent_sdk import ResultMessage, StreamEvent

        bus, db = self.j.bus, self.j.db
        now = datetime.now(ZoneInfo(self.s.timezone))
        who = f" · from {speaker}" if speaker else ""
        tag = f"[{'spoken' if mode == 'voice' else 'typed'} · {now:%A %d %B %Y, %H:%M} UK time{who}]"
        files = self._save_attachments(attachments)
        note = ("\n\nAttached files (open them with the Read tool): " + ", ".join(files)) if files else ""
        db.add_transcript("user", text)
        bus.publish("user_message", {"text": text, "mode": mode, "attachments": [a.get("name") for a in attachments or []]})
        bus.publish("thinking", {"mode": mode})

        started = time.monotonic()
        first_words: float | None = None
        parts: list[str] = []
        result = None
        try:
            client = await self._connected(self.s.voice_effort if mode == "voice" else self.s.chat_effort)
            await client.query(f"{tag}\n{text}{note}")
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    ev = msg.event or {}
                    etype = ev.get("type")
                    if etype == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                        chunk = ev["delta"].get("text", "")
                        if first_words is None:
                            first_words = time.monotonic() - started
                        parts.append(chunk)
                        bus.publish("delta", {"text": chunk, "mode": mode})
                    elif etype == "content_block_start":
                        block = ev.get("content_block") or {}
                        if block.get("type") in ("tool_use", "server_tool_use") and not str(block.get("name", "")).startswith("mcp__"):
                            bus.publish("tool", {"id": block.get("id"), "name": block.get("name"),
                                                 "label": _tool_label(block.get("name", "")), "state": "start"})
                    elif etype == "message_start" and parts and not parts[-1].endswith("\n"):
                        parts.append("\n\n")
                        bus.publish("delta", {"text": "\n\n", "mode": mode})
                elif isinstance(msg, ResultMessage):
                    result = msg
                    self.session_id = msg.session_id or self.session_id
        except Exception as e:  # noqa: BLE001
            log.exception("Claude Agent SDK turn failed")
            await self._disconnect()
            msg = ("I couldn't reach Claude through your subscription - check CLAUDE_CODE_OAUTH_TOKEN "
                   "(run `claude setup-token`)." if "auth" in str(e).lower() or "login" in str(e).lower()
                   else "Something went wrong talking to Claude. Please try again.")
            bus.publish("error", {"message": msg, "detail": str(e)[:300]})
            return msg
        log.info("%s reply: first words after %s, finished after %.1fs", mode,
                 f"{first_words:.1f}s" if first_words is not None else "-", time.monotonic() - started)
        if result is not None and result.is_error:
            limit = "limit" in str(result.result or result.errors or "").lower()
            msg = ("I've hit the usage limit on your Claude plan for now - it resets shortly." if limit
                   else f"Sorry, that didn't work: {str(result.result or result.errors)[:200]}")
            bus.publish("error", {"message": msg, "detail": str(result.errors)[:300]})
            return msg
        reply = "".join(parts).strip() or (result.result if result else "") or ""
        db.add_transcript("assistant", reply)
        bus.publish("reply", {"text": reply, "mode": mode})
        return reply


# ---------------------------------------------------------------------------
# One-shot helpers (briefings, triage, research) on the subscription
# ---------------------------------------------------------------------------

async def run_once(settings, *, system: str, prompt: str | list[dict[str, Any]], effort: str = "medium",
                   tools: list[str] | None = None, output_schema: dict[str, Any] | None = None,
                   max_turns: int = 10, cwd: str | None = None):
    """Single headless Claude Code run; returns the ResultMessage."""
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
                path = tmp / f"attachment-{n}.pdf"
                path.write_bytes(base64.b64decode(block["source"]["data"]))
                text += f"\n[Attached PDF '{block.get('title', '')}': {path} - read it with the Read tool]\n"
    kw: dict[str, Any] = {"system_prompt": system, "effort": effort, "tools": tools or [],
                          "allowed_tools": tools or [], "disallowed_tools": BLOCKED, "max_turns": max_turns}
    if output_schema:
        kw["output_format"] = {"type": "json_schema", "schema": output_schema}
    if cwd:
        kw["cwd"] = cwd
    result = None
    try:
        async for msg in query(prompt=text, options=base_options(settings, **kw)):
            if isinstance(msg, ResultMessage):
                result = msg
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    if result is None or result.is_error:
        raise RuntimeError(f"Claude run failed: {getattr(result, 'errors', None) or getattr(result, 'result', None)}")
    return result


def parse_structured(result, schema: type[BaseModel]) -> BaseModel:
    if result.structured_output:
        return schema.model_validate(result.structured_output)
    match = re.search(r"\{.*\}", result.result or "", re.S)
    if not match:
        raise RuntimeError("No JSON in Claude's reply")
    return schema.model_validate_json(match.group(0))
