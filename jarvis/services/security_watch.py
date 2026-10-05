"""Continuous security review of the Salts FSM codebase.

On a schedule, Jarvis pulls the current FSM source and has Claude look it over specifically for real,
exploitable vulnerabilities - not general bugs, and not generic advice - using the same read-only tools as
the auto-fix engineer (grep, find_files, the text editor's `view`), but it never edits anything itself; this
is a review, not a fix.

Findings become ordinary Jarvis issues, so they sit right next to staff-reported problems on the display, get
triaged the same way, and - only with the owner's approval - go through the exact same pull-request pipeline as
any other fix (see fixer.py). Nothing is ever changed or deployed by the review itself. A finding that's still
open (or being worked on) from a previous review isn't reported again every time - it would just be noise -
but a finding that was fixed and later comes back is treated as new.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ..brain import llm
from .digest import security_kind
from .workspace import Workspace, WorkspaceError

log = logging.getLogger(__name__)
MAX_TURNS = 50
# Anything still in one of these states was already flagged and hasn't been resolved (or shown to be a
# non-issue) - reporting it again on the next scheduled review would just be noise.
ACTIVE_ISSUE_STATUSES = {"new", "open", "triaged", "needs_human", "fixing", "fix_ready", "deploying"}

SECURITY_SYSTEM = """You are a security reviewer inside Jarvis, the AI assistant of {company}. You review the
source of Salts FSM, the company's field service management web app, checked out at /repo - looking for real,
concrete, exploitable vulnerabilities, not style opinions or generic hardening advice.

How you work:
- Get your bearings first: find the web entry points (controllers, routes, request handlers), anything that
  touches authentication or access control, anywhere a database query, file path or shell command is built
  from user input, and anywhere a secret or credential might be hardcoded. Use `grep` and `find_files`, and
  read files with the editor's `view` command.
- Only report something you can point at a specific file and explain how it could actually be exploited and
  what someone could do with it. Skip generic advice ("use HTTPS", "validate input" in the abstract) unless
  you found a concrete instance of it missing.
- Rate severity honestly: critical/high for things that would let someone bypass authentication, read or
  change data that isn't theirs, run arbitrary code, or get at secrets; medium/low for real but harder-to-
  exploit or lower-impact issues. Don't inflate severity to seem thorough.
- You cannot run code, and you must not edit anything - this is a review, not a fix. If you can see a clear,
  safe fix, describe it in a sentence or two; the actual change happens later, only with the owner's approval.
- Finish by calling `submit_findings`, even if you found nothing - a clean pass is a good outcome and worth
  saying so plainly, not a reason to invent findings."""


class Finding(BaseModel):
    title: str = Field(description="Short, specific, e.g. 'SQL built from unescaped job search text'")
    file: str = Field(description="Path within the repository")
    severity: Literal["low", "medium", "high", "critical"]
    description: str = Field(description="What the issue is and how it could actually be exploited")
    suggested_fix: str = ""


class SubmitFindings(BaseModel):
    findings: list[Finding] = []
    summary: str = Field(description="One or two sentences: what was reviewed and the overall picture")


class GrepInput(BaseModel):
    pattern: str
    glob: str = "*"


class FindInput(BaseModel):
    glob: str


def _tool(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return {"name": name, "description": description, "input_schema": schema, "eager_input_streaming": True}


REVIEW_TOOLS = [
    {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool", "max_characters": 30000},
    _tool("grep", "Regex search across the repository's text files. `glob` filters file names, e.g. '*.cs' or "
                  "'src/*'. Returns path:line: text.", GrepInput),
    _tool("find_files", "List files whose name or path matches a glob, e.g. '*Auth*' or '*.razor'.", FindInput),
    _tool("submit_findings", "Call once you've finished the review, with everything you found (or an empty "
                             "list - that's a good outcome).", SubmitFindings),
]


def _view_only(args: Any, ws: Workspace) -> str:
    """The reviewer gets the text editor tool for reading only - `view`, never the edit commands."""
    if not isinstance(args, dict) or args.get("command") != "view":
        raise WorkspaceError("This is a review, not a fix - you can only view files here, not change them.")
    return ws.view(args.get("path", ""), args.get("view_range"))


class SecurityWatch:
    def __init__(self, settings, db, bus, notifier, client, github, issues):
        self.s = settings
        self.db = db
        self.bus = bus
        self.notifier = notifier
        self.client = client
        self.gh = github
        self.issues = issues
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self.gh is not None

    def start(self) -> str:
        """Kick off a review in the background - it can take a few minutes, so chat doesn't wait on it."""
        if not self.enabled:
            return "Not set up - the security review needs the same GitHub connection as auto-fix."
        task = asyncio.create_task(self.run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return "Started - I'll let you know what I find."

    # ------------------------------------------------------------------ entry point
    async def run(self) -> dict[str, Any]:
        if not self.enabled:
            return {"error": "Not configured (needs GITHUB_TOKEN and FSM_REPO)."}
        sha = await self.gh.branch_sha()
        with tempfile.TemporaryDirectory(prefix="jarvis-security-") as tmp:
            root = await self.gh.download_tree(Path(tmp), sha)
            ws = Workspace(root)
            try:
                result = await self._review(ws)
            except Exception as e:  # noqa: BLE001
                log.exception("Security review failed")
                await self.notifier.notify("Security review failed", str(e)[:500], level="warning",
                                           importance="normal", engineering=True, kind="security_review_failed")
                return {"error": str(e)[:500]}

        new_issue_ids = []
        urgent = False
        for finding in result.findings:
            key = hashlib.sha256(f"{finding.file}:{finding.title}".lower().encode()).hexdigest()[:16]
            existing_id = self.db.get_kv(f"security_finding:{key}")
            if existing_id:
                existing = self.db.get_issue(int(existing_id))
                if existing and existing["status"] in ACTIVE_ISSUE_STATUSES:
                    continue  # already flagged and still being dealt with
            issue = await self.issues.report(
                reporter="Jarvis (security watch)", title=f"Security: {finding.title}",
                description=(f"{finding.description}\n\nFile: {finding.file}\n"
                            f"Suggested fix: {finding.suggested_fix or 'see description above'}"),
                severity=finding.severity, system="Salts FSM", source="security_watch", notify=True, process=True,
                kind=security_kind(finding.severity), engineering=True)  # critical/high go out now; low/medium wait for the digest
            self.db.set_kv(f"security_finding:{key}", str(issue["id"]))
            new_issue_ids.append(issue["id"])
            if security_kind(finding.severity) == "security_finding_urgent":
                urgent = True

        if new_issue_ids:
            await self.notifier.notify(f"Security review: {len(new_issue_ids)} new finding(s)", result.summary,
                                       level="warning", push=True, speak=True, engineering=True,
                                       kind="security_finding_urgent" if urgent else "security_finding_minor",
                                       status="new findings")
        else:
            await self.notifier.notify("Security review: nothing new", result.summary, level="info",
                                       importance="info", engineering=True, kind="security_review_clean")
        return {"reviewed_sha": sha, "new_issues": new_issue_ids, "summary": result.summary,
                "findings": [f.model_dump() for f in result.findings]}

    # ------------------------------------------------------------------ the review itself
    async def _review(self, ws: Workspace) -> SubmitFindings:
        if self.s.effective_llm_backend == "max":
            return await self._review_max(ws)
        system = SECURITY_SYSTEM.format(company=self.s.company_name)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "Review the whole repository at /repo for security vulnerabilities."}]
        params = llm.request_params(self.s, self.s.engineer_effort, model=self.s.engineer_model_or_default())
        json_retries = 0
        for _ in range(MAX_TURNS):
            try:
                async with self.client.beta.messages.stream(max_tokens=64000, system=system, messages=messages,
                                                            tools=REVIEW_TOOLS, cache_control={"type": "ephemeral"},
                                                            **params) as stream:
                    response = await stream.get_final_message()
                json_retries = 0
            except ValueError:
                # Tool input JSON the SDK could not parse at all; no tool_use id to answer, so retry the step.
                json_retries += 1
                if json_retries > 2:
                    raise
                continue
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason == "refusal":
                return SubmitFindings(findings=[], summary="The model declined to review this.")
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                text = "".join(b.text for b in response.content if b.type == "text")
                return SubmitFindings(findings=[], summary=text or "Stopped without submitting findings.")
            results = []
            finished: SubmitFindings | None = None
            for block in tool_uses:
                if response.stop_reason == "max_tokens":
                    results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                    "content": "Your tool input was cut off (max_tokens). Ask for less at once."})
                    continue
                try:
                    content = self._tool_call(block.name, block.input, ws)
                    if block.name == "submit_findings":
                        finished = SubmitFindings.model_validate(block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
                except (WorkspaceError, ValidationError, ValueError) as e:
                    results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                    "content": str(e)[:2000]})
            if finished:
                return finished
            messages.append({"role": "user", "content": results})
        return SubmitFindings(findings=[], summary=f"Stopped after {MAX_TURNS} steps without finishing.")

    @staticmethod
    def _tool_call(name: str, args: Any, ws: Workspace) -> str:
        if name == "str_replace_based_edit_tool":
            return _view_only(args, ws)
        if name == "grep":
            a = GrepInput.model_validate(args)
            return ws.grep(a.pattern, a.glob)
        if name == "find_files":
            return ws.find(FindInput.model_validate(args).glob)
        if name == "submit_findings":
            return "Submitted."
        raise ValueError(f"Unknown tool {name}")

    async def _review_max(self, ws: Workspace) -> SubmitFindings:
        """Same review on the Claude subscription: Claude Code's own read-only tools, confined to the checkout."""
        from ..brain.max_backend import parse_structured, run_once

        system = SECURITY_SYSTEM.format(company=self.s.company_name).replace("/repo", "the current directory")
        result = await run_once(self.s, system=system,
                                prompt="Review the whole repository for security vulnerabilities.",
                                effort=self.s.engineer_effort, model=self.s.engineer_model_or_default(),
                                tools=["Read", "Glob", "Grep"],
                                output_schema=SubmitFindings.model_json_schema(), max_turns=80, cwd=str(ws.root))
        return parse_structured(result, SubmitFindings)
