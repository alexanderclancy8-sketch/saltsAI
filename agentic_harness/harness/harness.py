"""Top-level entry point: wires a real Anthropic client, the mock filesystem
tools, and a CLI approval gate into an Orchestrator. This is the only class most
callers need - `AgenticHarness().run("...")`.
"""

from __future__ import annotations

import os

from .approval import ApprovalGate, CLIApprovalGate
from .orchestrator import Orchestrator, OrchestratorResult
from .tools import MockFS, ToolRegistry, build_default_registry

DEFAULT_WORKER_SYSTEM = """You are a worker agent inside an orchestrator-worker harness. You're given one \
subtask at a time. Use the read_file/list_files/write_file tools to complete it, then reply with plain text \
and no further tool calls once it's done. write_file requires human approval and may be denied - if it is, \
say so in your final reply rather than pretending it succeeded."""


class AgenticHarness:
    def __init__(self, *, api_key: str | None = None, model: str = "claude-sonnet-5-5",
                 fs: MockFS | None = None, tools: ToolRegistry | None = None,
                 gate: ApprovalGate | None = None, worker_system: str = DEFAULT_WORKER_SYSTEM) -> None:
        import anthropic  # imported lazily so tests can run without the package installed

        self.client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")).messages
        self.model = model
        self.fs = fs or MockFS()
        self.tools = tools or build_default_registry(self.fs)
        self.gate = gate or CLIApprovalGate()
        self.orchestrator = Orchestrator(self.client, self.model, worker_system, self.tools, self.gate)

    def run(self, task: str) -> OrchestratorResult:
        return self.orchestrator.run(task)
