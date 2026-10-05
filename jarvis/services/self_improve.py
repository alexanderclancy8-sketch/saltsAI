"""Jarvis proposing changes to its OWN source code.

The owner can ask Jarvis to add or fix something about itself ("add a tool that...", "there's a bug where...").
The engineering agent investigates and writes the change with the same read/search/edit tools as the Salts FSM
auto-fix (see fixer.py), then opens a pull request against Jarvis's own repository.

Deliberately narrower than the FSM auto-fix pipeline: there is no merge step and no deploy step at all, at any
point, under any circumstance. The PR sits on GitHub for a human to review and merge through their own tooling -
Jarvis never merges or redeploys itself. Modifying and then re-launching your own running code is a fundamentally
different risk to fixing a separate application, so this stays a proposal, never an action.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from ..brain import llm, plugins
from .agent_runs import AgentRuns
from .workspace import Workspace, WorkspaceError

log = logging.getLogger(__name__)
MAX_TURNS = 60  # API backend: model turns before the loop gives up
MAX_TURNS_MAX = 80  # Claude subscription backend: max_turns handed to run_once()


def _budget_message(turns: int) -> str:
    return (f"Stopped after {turns} turns without finishing: the engineering agent used its whole turn budget "
            "without calling submit_change or give_up, so no change was proposed.")


SELF_IMPROVE_SYSTEM = """You are the software engineer inside Jarvis, the AI assistant of {company} - and this \
time the code you are changing is your OWN source, checked out at /repo. {owner} asked for this:

<request>
{request}
</request>

How you work:
- Investigate first: look at the project layout, find the relevant code with `grep` and `find_files`, read it \
with the editor's `view` command, and understand how it fits together before editing.
- Make the smallest safe change that does what was asked, in the style of the surrounding code. Add or update a \
test when there's an existing test suite for that area - this repository has one, under tests/.
- You cannot run code. The repository's own CI runs the tests on your pull request, so re-read your edits \
carefully before submitting.
- Never weaken, remove or work around the approval gate (jarvis/services/actions.py, and any tool's \
`approval=True`), authentication (jarvis/auth.py), the settings encryption and the owner-only settings list \
(jarvis/settings_store.py), the standing-approvals allowlist (jarvis/services/standing_approvals.py), the Teams \
approvals path (jarvis/services/teams_approvals.py, the /api/teams/messages handler in jarvis/main.py and \
jarvis/integrations/teamsbot.py), or any other safety check anywhere in this codebase - not even if the request seems to call for it. Never touch \
secrets, credentials, CI/CD workflow files, or deployment scripts.
- The request was written by the owner, but treat it the same way regardless: a good outcome is a small, correct, \
well-tested change - never a sweeping rewrite or something you're not confident in.
- This produces a pull request only. You never merge it, deploy it, or restart anything - a human reviews and \
merges it through their own tooling, in their own time, however this request is phrased.
- Finish by calling `submit_change`. If what's being asked isn't safe, isn't a good idea, or you're not \
confident, call `give_up` with your reasoning instead - that is a good outcome too, and always the right call \
over a risky or half-finished change."""


class GrepInput(BaseModel):
    pattern: str
    glob: str = "*"


class FindInput(BaseModel):
    glob: str


class SubmitInput(BaseModel):
    pr_title: str
    summary: str
    test_notes: str
    risk: Literal["low", "medium", "high"]


class GiveUpInput(BaseModel):
    analysis: str


def _tool(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return {"name": name, "description": description, "input_schema": schema, "eager_input_streaming": True}


SELF_IMPROVE_TOOLS = [
    {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool", "max_characters": 30000},
    _tool("grep", "Regex search across the repository's text files. `glob` filters file names, e.g. '*.py'.",
          GrepInput),
    _tool("find_files", "List files whose name or path matches a glob, e.g. '*tools*' or '*.js'.", FindInput),
    _tool("submit_change", "Call once your change is complete. Summarise it for the pull request.", SubmitInput),
    _tool("give_up", "Call when there's no safe change to make, or you're not confident. Explain why.", GiveUpInput),
]


class SelfImprove:
    def __init__(self, settings, db, bus, notifier, client, github):
        self.s = settings
        self.db = db
        self.bus = bus
        self.notifier = notifier
        self.client = client
        self.gh = github
        self._tasks: set[asyncio.Task] = set()
        self.runs = AgentRuns(db)

    @property
    def enabled(self) -> bool:
        return self.gh is not None

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def start(self, request: str) -> str:
        """Kick off the change in the background - it can take a few minutes, so chat doesn't wait on it."""
        if not self.enabled:
            return ("Not set up - add Jarvis's own repository and a GitHub token for it on the Settings page "
                    "first (Self-improvement card).")
        task = asyncio.create_task(self.run(request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return ("On it, sir - I'll open a pull request for you to review. I won't merge or deploy it myself, "
                "whatever happens.")

    # ------------------------------------------------------------------ entry point
    async def run(self, request: str) -> dict[str, Any]:
        """Record that the run started (before anything else can fail), then do it, and make sure that however it
        ends - result, error, cancellation - the record is closed and the owner is told if it didn't complete."""
        if not self.enabled:
            return {"error": "Not configured (needs a GitHub token and JARVIS_REPO)."}
        try:
            with self.runs.track("self_improve", request):  # agent_runs record: one row per run, closed however it ends
                result = await self._run(request)
                if "error" in result:
                    self.runs.finish("failed", result["error"])
                elif "pr_url" in result:
                    self.runs.finish("submitted", result["pr_url"])
                else:
                    self.runs.finish("gave_up", result.get("analysis", ""))
            return result
        except BaseException as e:  # noqa: BLE001 - incl. CancelledError: track() has already closed the record
            interrupted = not isinstance(e, Exception)
            log.exception("Self-improvement run %s", "interrupted" if interrupted else "failed")
            await self._notify_incomplete(request, e, interrupted)
            if interrupted:
                raise
            return {"error": str(e)[:500]}

    async def _notify_incomplete(self, request: str, exc: BaseException, interrupted: bool) -> None:
        """Tell the owner a run died or was cancelled part-way (the engineering-flagged self_improve_failed kind).
        Failures _run() handles itself already notify; this covers whatever escaped it."""
        title = "Self-improvement run interrupted" if interrupted else "Self-improvement attempt failed"
        body = f"Request: {request[:200]}\n{type(exc).__name__}: {exc}"[:500]
        try:
            await self.notifier.notify(title, body, level="warning", importance="normal", engineering=True,
                                       kind="self_improve_failed")
        except BaseException:  # noqa: BLE001 - the notification itself failing (or being cancelled) must not hide the error
            log.exception("Could not send the self-improvement failure notification")

    async def _run(self, request: str) -> dict[str, Any]:
        if not self.enabled:
            return {"error": "Not configured (needs a GitHub token and JARVIS_REPO)."}
        try:
            base_sha = await self.gh.branch_sha()
            with tempfile.TemporaryDirectory(prefix="jarvis-self-") as tmp:
                root = await self.gh.download_tree(Path(tmp), base_sha)
                ws = Workspace(root)
                outcome = await self._engineer(request, ws)
                changes = ws.changed_files()
                if outcome["kind"] != "submit" or not changes:
                    analysis = outcome.get("analysis") or "No change was made."
                    await self.notifier.notify("Nothing to propose", analysis[:800], level="info",
                                               importance="info", engineering=True, kind="self_improve_nothing")
                    return {"outcome": "give_up", "analysis": analysis}
                fix = outcome["fix"]
                diff = ws.diff()
        except Exception as e:  # noqa: BLE001
            log.exception("Self-improvement attempt failed")
            await self.notifier.notify("Self-improvement attempt failed", str(e)[:500], level="warning",
                                       importance="normal", engineering=True, kind="self_improve_failed")  # developer chatter, not for the shared inbox
            return {"error": str(e)[:500]}

        branch = f"jarvis/self-{base_sha[:7]}-{int(time.time())}"
        await self.gh.commit_files(branch, base_sha, changes, f"{fix.pr_title}\n\nRequested by {self.s.owner_name}.")
        body = (f"## Request\n{request}\n\n## Change\n{fix.summary}\n\n## Testing\n{fix.test_notes}\n\n"
                f"Risk: **{fix.risk}**\n\n_Written by Jarvis at {self.s.owner_name}'s request. This pull request "
                f"is never merged or deployed automatically - review and merge it yourself when you're ready._")
        pr = await self.gh.open_pr(branch, fix.pr_title, body)
        await self.notifier.notify(
            f"Pull request ready: {fix.pr_title}",
            f"{fix.summary}\nPR: {pr['url']}\nRisk: {fix.risk}. Review and merge it yourself when you're happy "
                "with it - I won't touch it further.", level="info", push=True, speak=True,
                importance="info", engineering=True,
                kind="pr_ready", link=pr["url"], status="awaiting review", ref=f"self:{pr['number']}")
        self._spawn(self.watch_ci(pr["number"], pr["head_sha"], fix.pr_title))
        return {"pr_url": pr["url"], "risk": fix.risk, "diff": diff[:20000]}

    # ------------------------------------------------------------------ the engineer agent loop
    async def _engineer(self, request: str, ws: Workspace) -> dict[str, Any]:
        if self.s.effective_llm_backend == "max":
            return await self._engineer_max(request, ws)
        params = llm.request_params(self.s, self.s.engineer_effort)
        system = plugins.with_methodology(
            SELF_IMPROVE_SYSTEM.format(company=self.s.company_name, owner=self.s.owner_name, request=request), self.s)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "Make the requested change to the repository at /repo."}]
        json_retries = 0
        for _ in range(MAX_TURNS):
            try:
                async with self.client.beta.messages.stream(max_tokens=64000, system=system, messages=messages,
                                                            tools=SELF_IMPROVE_TOOLS,
                                                            cache_control={"type": "ephemeral"}, **params) as stream:
                    response = await stream.get_final_message()
                json_retries = 0
            except ValueError:
                # Tool input JSON the SDK could not parse; no tool_use id to answer, so re-issue the turn.
                json_retries += 1
                if json_retries > 2:
                    raise
                continue
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason == "refusal":
                return {"kind": "give_up", "analysis": "The model declined to work on this request."}
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                text = "".join(b.text for b in response.content if b.type == "text")
                return {"kind": "give_up", "analysis": text or "Stopped without submitting a change."}
            results = []
            finished: dict[str, Any] | None = None
            for block in tool_uses:
                if response.stop_reason == "max_tokens":
                    results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                    "content": "Your tool input was cut off (max_tokens). Make smaller edits."})
                    continue
                try:
                    out, finished_now = self._tool_call(block.name, block.input, ws)
                    finished = finished or finished_now
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
                    self.runs.step(block.name, block.input)
                except (WorkspaceError, ValidationError, ValueError, OSError) as e:
                    self.runs.step(block.name, block.input, ok=False)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                    "content": json.dumps({"error": str(e)[:2000]})})
            if finished:
                return finished
            messages.append({"role": "user", "content": results})
        return {"kind": "give_up", "analysis": _budget_message(MAX_TURNS)}

    async def _engineer_max(self, request: str, ws: Workspace) -> dict[str, Any]:
        """Same job on the Claude subscription: Claude Code's own Read/Edit/Glob/Grep tools, confined to the
        checkout (no shell, no web). Changes are found by comparing with a pristine copy."""
        from ..brain.max_backend import ENGINEER_BLOCKED, MaxTurnsExceeded, parse_structured, run_once

        class Outcome(BaseModel):
            outcome: Literal["submit", "give_up"]
            pr_title: str = ""
            summary: str = ""
            test_notes: str = ""
            risk: Literal["low", "medium", "high"] = "medium"
            analysis: str = ""

        ws.snapshot()
        self.runs.note("handed to Claude Code (subscription backend) - no per-step trail on this backend")
        system = SELF_IMPROVE_SYSTEM.format(company=self.s.company_name, owner=self.s.owner_name, request=request) \
            .replace("/repo", "the current directory").replace(
                "Finish by calling `submit_change`. If what's being asked isn't safe, isn't a good idea, or "
                "you're not \nconfident, call `give_up` with your reasoning instead - that is a good outcome "
                "too, and always the right call \nover a risky or half-finished change.",
                "Finish with outcome 'submit' and the PR details once your change is complete, or outcome "
                "'give_up' with your analysis if there's no safe change to make - that is a good outcome too.")
        docs = plugins.engineering_setup(self.s)  # Context7, read-only docs - only if on and pinned
        system = plugins.with_methodology(system, self.s) + docs.prompt
        try:
            result = await run_once(self.s, system=system, prompt="Make the requested change to this repository.",
                                    effort=self.s.engineer_effort, tools=["Read", "Edit", "Write", "Glob", "Grep"],
                                    disallowed_tools=ENGINEER_BLOCKED,
                                    output_schema=Outcome.model_json_schema(), max_turns=MAX_TURNS_MAX,
                                    cwd=str(ws.root), mcp_servers=docs.mcp_servers,
                                    extra_allowed=docs.allowed_tools)
        except MaxTurnsExceeded:
            return {"kind": "give_up", "analysis": _budget_message(MAX_TURNS_MAX)}
        out = parse_structured(result, Outcome)
        if out.outcome == "submit" and ws.changed_files():
            return {"kind": "submit", "fix": SubmitInput(pr_title=out.pr_title or "Self-improvement",
                                                         summary=out.summary, test_notes=out.test_notes,
                                                         risk=out.risk)}
        return {"kind": "give_up", "analysis": out.analysis}

    def _tool_call(self, name: str, args: Any, ws: Workspace) -> tuple[str, dict[str, Any] | None]:
        if not isinstance(args, dict):
            raise ValueError("tool input must be an object")
        if name == "str_replace_based_edit_tool":
            return ws.run_editor_command(args), None
        if name == "grep":
            a = GrepInput.model_validate(args)
            return ws.grep(a.pattern, a.glob), None
        if name == "find_files":
            return ws.find(FindInput.model_validate(args).glob), None
        if name == "submit_change":
            fix = SubmitInput.model_validate(args)
            if not ws.changed_files():
                raise ValueError("You have not changed any files yet.")
            return "Submitted.", {"kind": "submit", "fix": fix}
        if name == "give_up":
            g = GiveUpInput.model_validate(args)
            return "Noted.", {"kind": "give_up", "analysis": g.analysis}
        raise ValueError(f"Unknown tool {name}")

    # ------------------------------------------------------------------ CI (never merge, never deploy)
    async def watch_ci(self, pr_number: int, head_sha: str, title: str, timeout_s: int = 2700) -> None:
        waited = 0
        state = {"state": "pending"}
        while waited < timeout_s:
            await asyncio.sleep(60)
            waited += 60
            try:
                state = await self.gh.checks_summary(head_sha)
            except Exception as e:  # noqa: BLE001
                log.warning("CI poll failed: %s", e)
                continue
            if state["state"] in ("success", "failure", "none") and waited >= 120:
                break
        if state["state"] == "failure":
            await self.notifier.notify(
                "CI failed on the self-improvement pull request",
                f"\"{title}\" (PR #{pr_number}) - failing checks: {', '.join(state['failed'])}. Worth a look "
                "before merging.", level="warning", importance="normal", engineering=True,
                kind="self_improve_ci_failed", status="CI failed", ref=f"self:{pr_number}")
        elif state["state"] == "success":
            await self.notifier.notify(
                "CI passed on the self-improvement pull request",
                f"\"{title}\" (PR #{pr_number}) is green and ready for your review.", level="info",
                importance="info", engineering=True,
                kind="self_improve_ci_passed", status="CI passed", ref=f"self:{pr_number}")
