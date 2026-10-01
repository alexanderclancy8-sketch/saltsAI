"""ThoughtProof: an extra check in front of approved write actions.

An action only reaches here AFTER the owner has clicked Approve (services/actions.py). Before it runs, it is
described to a local ThoughtProof MCP server together with the written mandates in mandates.yaml, and the
server must answer ALLOW or BLOCK.

This layer can only ever make an action LESS likely to run:
- it never approves, queues or skips anything - the approval click is still required first;
- BLOCK cancels the action and tells the owner why;
- anything other than a clear ALLOW (an error, a timeout, a missing or badly configured server, an unparseable
  or mixed answer) is treated as BLOCK. It fails closed: a write is never run unchecked while this is switched on.
When it's switched off (the default) none of this code runs and nothing changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from ..brain.plugins import launch_config, load_specs

log = logging.getLogger(__name__)
MAX_PAYLOAD_CHARS = 8000


@dataclass(frozen=True)
class Verdict:
    decision: str  # "ALLOW" or "BLOCK"
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == "ALLOW"


def block(reason: str) -> Verdict:
    return Verdict("BLOCK", reason)


def load_mandates(path: Path | str) -> list[dict[str, str]]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        log.warning("Couldn't read the mandates at %s: %s", path, e)
        return []
    items = data.get("mandates") if isinstance(data, dict) else None
    return [{"id": str(m["id"]), "rule": " ".join(str(m["rule"]).split())}
            for m in items or [] if isinstance(m, dict) and m.get("id") and m.get("rule")]


def parse_verdict(text: str) -> Verdict:
    """Strict: only a clear ALLOW is ALLOW. Everything else is BLOCK."""
    text = (text or "").strip()
    if not text:
        return block("the verifier gave no answer")
    reason = ""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        raw = data.get("verdict", data.get("decision", data.get("result")))
        decision = str(raw).strip().upper() if isinstance(raw, str) else ""
        reason = str(data.get("reason") or data.get("explanation") or data.get("rationale") or "")[:500]
    else:
        upper = text.upper()
        first = re.match(r"[A-Z]+", upper)
        decision = first.group(0) if first else ""
        if decision == "ALLOW" and "BLOCK" in upper:
            decision = ""  # mixed signals
        reason = text[:500]
    if decision == "ALLOW":
        return Verdict("ALLOW", reason)
    if decision == "BLOCK":
        return block(reason or "blocked by the security mandates")
    return block("the verifier's answer wasn't a clear ALLOW or BLOCK")


async def call_mcp_tool(launch: dict[str, Any], tool: str, arguments: dict[str, Any]) -> str:
    """Run one tool on a local stdio MCP server (started for this call and stopped after it)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=launch["command"], args=list(launch["args"]), env=dict(launch.get("env") or {}))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
    text = "\n".join(getattr(c, "text", "") or "" for c in (result.content or []))
    if getattr(result, "isError", False):
        raise RuntimeError(f"the verifier reported an error: {text[:200]}")
    return text


Caller = Callable[[dict[str, Any], str, dict[str, Any]], Awaitable[str]]


class ActionVerifier:
    def __init__(self, settings, caller: Caller | None = None):
        self.s = settings
        self._call = caller or call_mcp_tool

    @property
    def enabled(self) -> bool:
        return bool(self.s.plugin_thoughtproof_enabled)

    def _spec(self) -> dict[str, Any]:
        return load_specs(self.s.plugins_file).get("thoughtproof") or {}

    def problem(self) -> str:
        """Why verification can't currently run ("" if it can). Only meaningful while it's switched on."""
        spec = self._spec()
        if not spec:
            return "no thoughtproof entry in mcp_plugins.yaml"
        _, why = launch_config(spec)
        if why:
            return why
        if not str(spec.get("tool") or "").strip():
            return "no verify tool named in mcp_plugins.yaml"
        if not load_mandates(self.s.mandates_file):
            return "no mandates found in mandates.yaml"
        return ""

    async def verify(self, action: dict[str, Any]) -> Verdict:
        """Never raises; anything short of a clear ALLOW comes back as BLOCK."""
        try:
            why = self.problem()
            if why:
                return block(f"verification is unavailable ({why})")
            spec = self._spec()
            launch, _ = launch_config(spec)
            payload = json.dumps(action.get("payload"), default=str, ensure_ascii=False)
            described = {
                "kind": action.get("kind"), "summary": action.get("summary"),
                "payload": payload[:MAX_PAYLOAD_CHARS] + ("…[truncated]" if len(payload) > MAX_PAYLOAD_CHARS else ""),
                "context": {"allowed_finance_recipients": [e for e in (self.s.owner_email, self.s.partner_email) if e],
                            "owner": self.s.owner_name, "partner": self.s.partner_name,
                            # Set by Jarvis itself, never taken from the action's own content.
                            "came_through_approval_queue": True},
            }
            mandates = load_mandates(self.s.mandates_file)
            arguments = {str(spec.get("action_arg") or "action"): json.dumps(described, ensure_ascii=False),
                         str(spec.get("mandates_arg") or "mandates"): json.dumps(mandates, ensure_ascii=False)}
            try:
                timeout = float(spec.get("timeout_s") or 20)
            except (TypeError, ValueError):
                timeout = 20.0
            text = await asyncio.wait_for(self._call(launch, str(spec["tool"]).strip(), arguments), timeout=timeout)
            return parse_verdict(text)
        except asyncio.TimeoutError:
            return block("verification timed out")
        except Exception as e:  # noqa: BLE001 - fail closed on absolutely anything
            log.warning("ThoughtProof verification failed: %s", e)
            return block(f"verification is unavailable ({type(e).__name__})")
