from agentic_harness.harness import Worker, MockFS, ToolRegistry
from agentic_harness.harness.tools import build_default_registry
from agentic_harness.harness.approval import AutoApprovalGate, ApprovalDecision
from .fakes import FakeMessages, text_message, tool_message, ToolUseBlock


def make_worker(script, gate=None):
    fs = MockFS({"a.txt": "hello"})
    registry = build_default_registry(fs)
    gate = gate or AutoApprovalGate(ApprovalDecision.APPROVED)
    client = FakeMessages(script)
    worker = Worker(client, "test-model", "system", registry, gate, max_turns=5)
    return worker, fs, client, gate


def test_read_only_tool_runs_without_approval():
    script = [
        tool_message(ToolUseBlock(id="1", name="read_file", input={"path": "a.txt"})),
        text_message("the file says hello"),
    ]
    worker, fs, client, gate = make_worker(script)
    result = worker.run("read a.txt")

    assert result.success
    assert result.final_text == "the file says hello"
    assert result.turns_used == 2
    assert gate.requests == []  # never gated - read_file isn't a write


def test_write_tool_is_gated_and_applied_on_approval():
    script = [
        tool_message(ToolUseBlock(id="1", name="write_file", input={"path": "a.txt", "content": "bye"})),
        text_message("done"),
    ]
    worker, fs, client, gate = make_worker(script)
    result = worker.run("overwrite a.txt")

    assert result.success
    assert fs.files["a.txt"] == "bye"
    assert len(gate.requests) == 1
    assert gate.requests[0].name == "write_file"


def test_denied_write_is_not_applied():
    script = [
        tool_message(ToolUseBlock(id="1", name="write_file", input={"path": "a.txt", "content": "bye"})),
        text_message("could not write, was denied"),
    ]
    worker, fs, client, gate = make_worker(script, gate=AutoApprovalGate(ApprovalDecision.DENIED))
    result = worker.run("overwrite a.txt")

    assert result.success  # the loop itself completes - the denial is data, not a crash
    assert fs.files["a.txt"] == "hello"  # unchanged
    assert len(result.denied_calls) == 1


def test_gives_up_after_max_turns():
    # every turn asks for another tool call, never a final text - should hit the budget
    script = [tool_message(ToolUseBlock(id=str(i), name="read_file", input={"path": "a.txt"}))
              for i in range(10)]
    worker, fs, client, gate = make_worker(script)
    result = worker.run("loop forever")

    assert not result.success
    assert result.turns_used == 5
