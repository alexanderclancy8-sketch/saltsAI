"""Human-in-the-loop gate: the Worker checks with one of these before running any
tool flagged `is_write=True`. Swap CLIApprovalGate for a web/Slack/queue-backed one
in production without touching Worker at all - that's the point of the Protocol.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol

from .state import ToolCall


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"


class ApprovalGate(Protocol):
    def request(self, tool_call: ToolCall) -> ApprovalDecision: ...


class CLIApprovalGate:
    """Pauses the loop and blocks on a real terminal prompt - the reference
    implementation of the gate for a local/demo run."""

    def request(self, tool_call: ToolCall) -> ApprovalDecision:
        print(f"\n--- approval required ---\ntool: {tool_call.name}\ninput: {tool_call.input}")
        answer = input("approve? [y/N] ").strip().lower()
        return ApprovalDecision.APPROVED if answer == "y" else ApprovalDecision.DENIED


class AutoApprovalGate:
    """Records every request and answers from a fixed decision (or a per-call
    queue), for tests and non-interactive runs - never use this against real
    writes outside a test."""

    def __init__(self, default: ApprovalDecision = ApprovalDecision.APPROVED) -> None:
        self.default = default
        self.requests: list[ToolCall] = []

    def request(self, tool_call: ToolCall) -> ApprovalDecision:
        self.requests.append(tool_call)
        return self.default
