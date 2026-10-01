"""A fake Anthropic `messages.create` for tests - scripted responses, no network
call, no API key needed. Mirrors the shape of a real `anthropic.types.Message`
closely enough for Worker/Orchestrator to not know the difference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass
class FakeMessage:
    content: list[Any]
    stop_reason: str


def text_message(text: str) -> FakeMessage:
    return FakeMessage(content=[TextBlock(text)], stop_reason="end_turn")


def tool_message(*blocks: ToolUseBlock) -> FakeMessage:
    return FakeMessage(content=list(blocks), stop_reason="tool_use")


class FakeMessages:
    """Scripted `.create()`: pass a list of FakeMessage to return in order, one
    per call. Raises if the script runs out - a test that calls this more times
    than it scripted is a test that doesn't understand its own loop."""

    def __init__(self, script: list[FakeMessage]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeMessage:
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("FakeMessages script exhausted - the loop called create() more times than expected")
        return self.script.pop(0)
