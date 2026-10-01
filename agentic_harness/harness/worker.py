"""The ReAct tool loop: call Claude, intercept tool_use blocks, run them (or gate
them on a human first), feed tool_result blocks back, repeat until Claude stops
asking for tools or the turn budget runs out.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from pydantic import BaseModel

from .approval import ApprovalDecision, ApprovalGate
from .state import SessionState, ToolCall, ToolResult, TurnState
from .tools import ToolRegistry

log = logging.getLogger(__name__)


class AnthropicClient(Protocol):
    """The one method Worker needs - satisfied by `anthropic.Anthropic().messages`
    or by a fake in tests. Keeping this narrow is what makes Worker testable
    without a network call."""

    def create(self, *, model: str, system: str, messages: list[dict[str, Any]],
               tools: list[dict[str, Any]], max_tokens: int) -> Any: ...


class WorkerResult(BaseModel):
    task: str
    success: bool
    final_text: str
    turns_used: int
    tool_calls: list[ToolCall]
    denied_calls: list[ToolCall]


class Worker:
    """One bounded ReAct agent: a system prompt, a tool registry, an approval
    gate, and a turn budget. An Orchestrator spawns one of these per subtask."""

    def __init__(self, client: AnthropicClient, model: str, system: str,
                 tools: ToolRegistry, gate: ApprovalGate, max_turns: int = 8,
                 max_tokens: int = 2048) -> None:
        self.client = client
        self.model = model
        self.system = system
        self.tools = tools
        self.gate = gate
        self.max_turns = max_turns
        self.max_tokens = max_tokens

    def run(self, task: str) -> WorkerResult:
        state = SessionState(task=task, max_turns=self.max_turns)
        state.append("user", task)
        denied: list[ToolCall] = []

        while state.turn < state.max_turns:
            state.turn += 1
            state.state = TurnState.THINKING
            response = self.client.create(
                model=self.model,
                system=self.system,
                messages=state.messages,
                tools=self.tools.as_anthropic_tools(),
                max_tokens=self.max_tokens,
            )
            state.append("assistant", response.content)

            tool_use_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_use_blocks:
                final_text = "".join(
                    b.text for b in response.content if getattr(b, "type", None) == "text"
                )
                state.state = TurnState.DONE
                return WorkerResult(task=task, success=True, final_text=final_text,
                                     turns_used=state.turn, tool_calls=state.tool_calls,
                                     denied_calls=denied)

            state.state = TurnState.TOOL_CALL
            result_blocks = []
            for block in tool_use_blocks:
                call = ToolCall(id=block.id, name=block.name, input=block.input)
                state.tool_calls.append(call)
                result = self._execute(call, denied)
                state.tool_results.append(result)
                result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": result.tool_call_id,
                    "content": result.output,
                    "is_error": result.is_error,
                })
            state.append("user", result_blocks)

        state.state = TurnState.FAILED
        return WorkerResult(task=task, success=False,
                             final_text=f"gave up after {state.max_turns} turns without finishing",
                             turns_used=state.turn, tool_calls=state.tool_calls, denied_calls=denied)

    def _execute(self, call: ToolCall, denied: list[ToolCall]) -> ToolResult:
        try:
            tool = self.tools.get(call.name)
        except KeyError as exc:
            return ToolResult(tool_call_id=call.id, output=str(exc), is_error=True)

        if tool.is_write:
            decision = self.gate.request(call)
            if decision != ApprovalDecision.APPROVED:
                denied.append(call)
                return ToolResult(tool_call_id=call.id,
                                   output=f"denied by human operator: {call.name} was not run",
                                   is_error=True)

        try:
            output = tool.handler(call.input)
            return ToolResult(tool_call_id=call.id, output=output)
        except Exception as exc:  # a tool failing is data for the model, not a crash
            log.warning("tool %s failed: %s", call.name, exc)
            return ToolResult(tool_call_id=call.id, output=f"error running {call.name}: {exc}", is_error=True)
