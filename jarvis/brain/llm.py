"""Shared Claude client settings."""

from __future__ import annotations

from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from ..config import Settings

FALLBACK_BETA = "server-side-fallback-2026-07-01"
COMPACTION_BETA = "compact-2026-01-12"

T = TypeVar("T", bound=BaseModel)


def make_client(settings: Settings) -> anthropic.AsyncAnthropic:
    # With no key configured the SDK falls back to ANTHROPIC_API_KEY / an `ant auth login` profile.
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None, max_retries=3)


def request_params(settings: Settings, effort: str, *, compaction: bool = False) -> dict[str, Any]:
    params: dict[str, Any] = {"model": settings.claude_model, "output_config": {"effort": effort}}
    betas = []
    if settings.claude_fallbacks:
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
                     prompt: str | list[dict[str, Any]], effort: str = "low", max_tokens: int = 8000) -> T:
    """One-shot call that returns a validated pydantic object."""
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
    params = request_params(settings, effort)
    async with client.beta.messages.stream(max_tokens=max_tokens, system=system,
                                           messages=[{"role": "user", "content": prompt}], **params) as stream:
        message = await stream.get_final_message()
    if message.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request")
    return "".join(b.text for b in message.content if b.type == "text").strip()
