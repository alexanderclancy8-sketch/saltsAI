from agentic_harness.harness.approval import AutoApprovalGate, ApprovalDecision
from agentic_harness.harness.state import ToolCall


def test_auto_approval_gate_records_every_request():
    gate = AutoApprovalGate(ApprovalDecision.APPROVED)
    call = ToolCall(id="1", name="write_file", input={"path": "x", "content": "y"})

    decision = gate.request(call)

    assert decision == ApprovalDecision.APPROVED
    assert gate.requests == [call]


def test_auto_approval_gate_can_deny():
    gate = AutoApprovalGate(ApprovalDecision.DENIED)
    call = ToolCall(id="1", name="write_file", input={"path": "x", "content": "y"})

    assert gate.request(call) == ApprovalDecision.DENIED
