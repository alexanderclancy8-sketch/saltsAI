"""Typed state objects shared by Worker and Orchestrator.

Nothing here talks to Anthropic or executes a tool - it's just the shape of a turn,
a tool call, and the running state of one ReAct loop, so both classes and their tests
can pass the same objects around instead of raw dicts.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TurnState(str, Enum):
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    FAILED = "failed"


class ToolCall(BaseModel):
    id: str
    name: str
    input: dict[str, Any]


class ToolResult(BaseModel):
    tool_call_id: str
    output: str
    is_error: bool = False


class SessionState(BaseModel):
    """One Worker's running ReAct loop: the message history plus where it's up to."""

    task: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    turn: int = 0
    max_turns: int = 8
    state: TurnState = TurnState.THINKING
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)

    def append(self, role: Literal["user", "assistant"], content: Any) -> None:
        self.messages.append({"role": role, "content": content})

    def turns_remaining(self) -> int:
        return max(self.max_turns - self.turn, 0)
