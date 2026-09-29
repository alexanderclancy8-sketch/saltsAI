"""Orchestrator-Worker: a master loop that breaks a task into subtasks (or leaves
it as one, if it's already small) and delegates each to its own bounded Worker,
then synthesizes their results into a single answer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel

from .approval import ApprovalGate
from .tools import ToolRegistry
from .worker import AnthropicClient, Worker, WorkerResult

log = logging.getLogger(__name__)

PLANNER_SYSTEM = """You are the planning step of an orchestrator agent. Given a task, decide whether it \
should be split into smaller, independently-executable subtasks, or left as one. Reply with ONLY a JSON array \
of subtask strings - `["do the whole thing"]` if it doesn't need splitting, or one entry per subtask if it \
does. No other text, no markdown fences."""

SYNTHESIS_SYSTEM = """You are the synthesis step of an orchestrator agent. You're given the original task and \
the results each worker produced for its subtask. Write one clear final answer to the original task using \
those results. If a worker failed or was denied a write it needed, say so plainly rather than papering over it."""


class OrchestratorResult(BaseModel):
    task: str
    subtasks: list[str]
    worker_results: list[WorkerResult]
    final_summary: str


class Orchestrator:
    def __init__(self, client: AnthropicClient, model: str, worker_system: str,
                 tools: ToolRegistry, gate: ApprovalGate, max_subtasks: int = 4,
                 worker_max_turns: int = 8) -> None:
        self.client = client
        self.model = model
        self.worker_system = worker_system
        self.tools = tools
        self.gate = gate
        self.max_subtasks = max_subtasks
        self.worker_max_turns = worker_max_turns

    def plan(self, task: str) -> list[str]:
        response = self.client.create(
            model=self.model, system=PLANNER_SYSTEM,
            messages=[{"role": "user", "content": task}], tools=[], max_tokens=1024,
        )
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        try:
            subtasks = json.loads(text)
            if not isinstance(subtasks, list) or not subtasks:
                raise ValueError("planner did not return a non-empty JSON array")
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("planner output unparseable (%s), falling back to a single subtask", exc)
            return [task]
        return [str(s) for s in subtasks[: self.max_subtasks]]

    def synthesize(self, task: str, results: list[WorkerResult]) -> str:
        summary_input = json.dumps(
            {"task": task, "results": [r.model_dump() for r in results]}, indent=2,
        )
        response = self.client.create(
            model=self.model, system=SYNTHESIS_SYSTEM,
            messages=[{"role": "user", "content": summary_input}], tools=[], max_tokens=1024,
        )
        return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")

    def run(self, task: str) -> OrchestratorResult:
        subtasks = self.plan(task)
        results = [
            Worker(self.client, self.model, self.worker_system, self.tools, self.gate,
                   max_turns=self.worker_max_turns).run(sub)
            for sub in subtasks
        ]
        final_summary = self.synthesize(task, results)
        return OrchestratorResult(task=task, subtasks=subtasks, worker_results=results,
                                   final_summary=final_summary)
