# Agentic Harness

A small, type-safe reference implementation of three agent-architecture patterns, built directly on the
`anthropic` Python SDK. No framework, no hidden magic - every loop is readable start to finish in one file.

## The three patterns

**Orchestrator-Worker** ([harness/orchestrator.py](harness/orchestrator.py)) - `Orchestrator.plan()` asks Claude
to break a task into subtasks (or leave it as one, if it's already small), spawns one `Worker` per subtask, then
`Orchestrator.synthesize()` asks Claude to turn all the worker results into a single final answer.

**ReAct tool loop** ([harness/worker.py](harness/worker.py)) - `Worker.run()` is the loop: call Claude with the
tool schemas, intercept every `tool_use` content block, run the matching handler, append a `tool_result` block,
and go round again until Claude replies with plain text and no more tool calls, or the turn budget
(`max_turns`) runs out.

**Human-in-the-loop gate** ([harness/approval.py](harness/approval.py)) - before running a tool flagged
`is_write=True` (see [harness/tools.py](harness/tools.py)), the worker calls `ApprovalGate.request()` and blocks
on the answer. `CLIApprovalGate` is a real terminal `y/N` prompt; `AutoApprovalGate` is a scripted stand-in for
tests. Swap in a web/Slack/queue-backed gate without touching `Worker` at all - that's why it's a `Protocol`.

## File structure

```
agentic_harness/
  harness/
    state.py        # ToolCall, ToolResult, SessionState, TurnState - the typed shapes everything passes around
    tools.py         # Tool, ToolRegistry, MockFS + the three mock tools (read_file/list_files/write_file)
    approval.py       # ApprovalGate protocol, CLIApprovalGate, AutoApprovalGate
    worker.py          # Worker - the ReAct loop
    orchestrator.py     # Orchestrator - plan -> delegate -> synthesize
    harness.py            # AgenticHarness - the one class most callers need
  examples/
    run_demo.py       # runnable end-to-end demo against the real Anthropic API
  tests/
    fakes.py          # scripted fake `messages.create`, no network call
    test_worker_loop.py
    test_approval_gate.py
    test_orchestrator.py
```

## Running it

```bash
pip install anthropic pydantic
python -m pytest agentic_harness/tests -q     # no API key needed - fully scripted

export ANTHROPIC_API_KEY=sk-...
python -m agentic_harness.examples.run_demo    # real API call; watch the y/N prompt when it tries to write
```

## Mock tools

`MockFS` ([harness/tools.py](harness/tools.py)) is an in-memory `dict[str, str]` standing in for a real
filesystem - `read_file`/`list_files` are plain reads (never gated), `write_file` is flagged `is_write=True` and
always goes through the approval gate first. Point the same `Tool` dataclass at real disk I/O (or an HTTP call,
a DB write, anything) to take this from demo to production; nothing else in the harness needs to change, since
`Worker` only ever sees the `Tool` interface, never the filesystem directly.

## How this maps onto Jarvis's own architecture

Jarvis (the production assistant this harness sits next to, in `jarvis/`) already runs all three patterns for
real, just under different names - this package is the clean, minimal version of the same ideas, useful as a
reference or a starting point for a new agent rather than a replacement for anything in `jarvis/`:

| Here | In Jarvis |
|---|---|
| `Worker.run()`'s ReAct loop | `jarvis/brain/agent.py`'s `JarvisBrain._turn()` |
| `ApprovalGate` | `jarvis/services/actions.py`'s `ActionExecutor.queue()/approve()/deny()` |
| `Orchestrator` delegating to `Worker`s | `jarvis/services/recruiter.py` (`recruit_agent`), `self_improve.py`, `fixer.py` |
