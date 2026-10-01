from agentic_harness.harness import Orchestrator, MockFS
from agentic_harness.harness.tools import build_default_registry
from agentic_harness.harness.approval import AutoApprovalGate
from .fakes import FakeMessages, text_message, tool_message, ToolUseBlock


def make_orchestrator(script):
    fs = MockFS({"a.txt": "hello"})
    registry = build_default_registry(fs)
    gate = AutoApprovalGate()
    client = FakeMessages(script)
    return Orchestrator(client, "test-model", "worker system", registry, gate, worker_max_turns=5), fs


def test_plan_falls_back_to_single_subtask_on_bad_json():
    script = [
        text_message("not json at all"),                                       # plan()
        tool_message(ToolUseBlock(id="1", name="read_file", input={"path": "a.txt"})),  # worker turn 1
        text_message("it says hello"),                                          # worker turn 2
        text_message("final: the file says hello"),                             # synthesize()
    ]
    orch, fs = make_orchestrator(script)
    result = orch.run("read a.txt")

    assert result.subtasks == ["read a.txt"]
    assert len(result.worker_results) == 1
    assert result.worker_results[0].success
    assert result.final_summary == "final: the file says hello"


def test_plan_splits_into_multiple_subtasks():
    script = [
        text_message('["read a.txt", "list all files"]'),                      # plan()
        text_message("a.txt says hello"),                                       # worker 1, one turn
        text_message("only a.txt exists"),                                      # worker 2, one turn
        text_message("final: one file, a.txt, containing 'hello'"),             # synthesize()
    ]
    orch, fs = make_orchestrator(script)
    result = orch.run("describe the filesystem")

    assert result.subtasks == ["read a.txt", "list all files"]
    assert len(result.worker_results) == 2
    assert all(r.success for r in result.worker_results)
    assert "one file" in result.final_summary
