"""Shared Claude client settings."""

from __future__ import annotations

from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from ..config import Settings

FALLBACK_BETA = "server-side-fallback-2026-07-01"
COMPACTION_BETA = "compact-2026-01-12"

T = TypeVar("T", bound=BaseModel)


def make_client(settings: Settings) -> anthropic.AsyncAnthropic | None:
    if settings.effective_llm_backend == "max":
        return None  # subscription mode goes through the Claude Agent SDK instead
    # With no key configured the SDK falls back to ANTHROPIC_API_KEY / an `ant auth login` profile.
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None, max_retries=3)


def request_params(settings: Settings, effort: str, *, compaction: bool = False, model: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"model": model or settings.jarvis_model, "output_config": {"effort": effort}}
    betas = []
    if settings.jarvis_fallbacks:
        # If a safety classifier declines, the API re-runs the request on Anthropic's recommended fallback model.
        betas.append(FALLBACK_BETA)
        params["fallbacks"] = "default"
    if compaction:
        betas.append(COMPACTION_BETA)
        params["context_management"] = {"edits": [{"type": "compact_20260112"}]}
    if betas:
        params["betas"] = betas
    return params


async def structured(client: anthropic.AsyncAnthropic, settings: Settings, schema: type[T], *, system: str,
                     prompt: str | list[dict[str, Any]], effort: str = "low", max_tokens: int = 8000,
                     max_turns: int = 4) -> T:
    """One-shot call that returns a validated pydantic object. ``max_turns`` only matters on the Claude Max backend (a
    prompt with several attached pages is read one file at a time there, each read being a turn)."""
    if settings.effective_llm_backend == "max":
        from .max_backend import parse_structured, run_once

        result = await run_once(settings, system=system, prompt=prompt, effort=effort,
                                output_schema=schema.model_json_schema(), max_turns=max_turns)
        return parse_structured(result, schema)
    params = request_params(settings, effort)
    content = prompt if isinstance(prompt, list) else [{"type": "text", "text": prompt}]
    response = await client.beta.messages.parse(
        max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": content}],
        output_format=schema, **params)
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request")
    if response.parsed_output is None:
        raise RuntimeError(f"No structured output (stop_reason={response.stop_reason})")
    return response.parsed_output


async def write(client: anthropic.AsyncAnthropic, settings: Settings, *, system: str, prompt: str,
                effort: str = "low", max_tokens: int = 8000) -> str:
    """One-shot text generation (briefings, summaries)."""
    if settings.effective_llm_backend == "max":
        from .max_backend import run_once

        return ((await run_once(settings, system=system, prompt=prompt, effort=effort, max_turns=2)).result or "").strip()
    params = request_params(settings, effort)
    async with client.beta.messages.stream(max_tokens=max_tokens, system=system,
                                           messages=[{"role": "user", "content": prompt}], **params) as stream:
        message = await stream.get_final_message()
    if message.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request")
    return "".join(b.text for b in message.content if b.type == "text").strip()


async def research(client: anthropic.AsyncAnthropic, settings: Settings, *, system: str, prompt: str,
                   effort: str = "high", max_tokens: int = 16000, max_searches: int = 12) -> str:
    """Web-researched answer using the server-side web search / fetch tools."""
    if settings.effective_llm_backend == "max":
        from .max_backend import run_once

        result = await run_once(settings, system=system, prompt=prompt, effort=effort,
                                tools=["WebSearch", "WebFetch"], max_turns=40)
        return (result.result or "").strip()
    params = request_params(settings, effort)
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches,
              "user_location": {"type": "approximate", "country": "GB", "timezone": "Europe/London"}},
             {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max_searches}]
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    text_parts: list[str] = []
    for _ in range(6):
        async with client.beta.messages.stream(max_tokens=max_tokens, system=system, messages=messages, tools=tools,
                                               **params) as stream:
            message = await stream.get_final_message()
        if message.stop_reason == "refusal":
            raise RuntimeError("Claude declined this request")
        text_parts.append("".join(b.text for b in message.content if b.type == "text"))
        if message.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": message.content})
    return "".join(text_parts).strip()
