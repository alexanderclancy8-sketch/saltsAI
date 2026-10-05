"""max_backend.py's run_once(): the one-shot Claude Agent SDK helper used for briefings, and by the
self_improve.py / fixer.py engineer loops to actually edit a checked-out repo."""

from __future__ import annotations

from jarvis.brain.max_backend import BLOCKED, ENGINEER_BLOCKED, run_once


def _fake_query(claude_agent_sdk, captured):
    async def fake_query(*, prompt, options=None, transport=None):
        captured["options"] = options
        yield claude_agent_sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                             is_error=False, num_turns=1, session_id="s1", result="ok")

    return fake_query


async def test_run_once_names_a_turn_limit_stop_explicitly(settings, monkeypatch):
    """Hitting max_turns ends the SDK run with an error_max_turns result (no structured output). That must not
    surface as an anonymous parse failure or an empty outcome - it raises MaxTurnsExceeded."""
    import claude_agent_sdk
    import pytest

    from jarvis.brain.max_backend import MaxTurnsExceeded

    async def fake_query(*, prompt, options=None, transport=None):
        yield claude_agent_sdk.ResultMessage(subtype="error_max_turns", duration_ms=1, duration_api_ms=1,
                                             is_error=False, num_turns=3, session_id="s1")

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    settings.claude_code_oauth_token = "sk-ant-oat-test"
    with pytest.raises(MaxTurnsExceeded, match="turn limit"):
        await run_once(settings, system="sys", prompt="hi", max_turns=3)


async def test_run_once_raises_when_the_stream_ends_without_a_result(settings, monkeypatch):
    import claude_agent_sdk
    import pytest

    async def fake_query(*, prompt, options=None, transport=None):
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    settings.claude_code_oauth_token = "sk-ant-oat-test"
    with pytest.raises(RuntimeError, match="without a final result"):
        await run_once(settings, system="sys", prompt="hi")


async def test_run_once_blocks_write_and_edit_by_default(settings, monkeypatch):
    import claude_agent_sdk

    captured: dict = {}
    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query(claude_agent_sdk, captured))
    settings.claude_code_oauth_token = "sk-ant-oat-test"

    await run_once(settings, system="sys", prompt="hi", tools=["Read", "Edit", "Write"])

    assert captured["options"].disallowed_tools == BLOCKED
    assert "Edit" in captured["options"].disallowed_tools and "Write" in captured["options"].disallowed_tools


async def test_run_once_lets_engineer_loops_override_disallowed_tools(settings, monkeypatch):
    """Regression test: self_improve.py's _engineer_max and fixer.py's _run_engineer_max both pass
    tools=["Read", "Edit", "Write", ...], expecting to actually edit files in a throwaway checkout - but
    run_once() used to hard-code disallowed_tools=BLOCKED (which includes Write and Edit) regardless of what
    `tools` asked for. disallowed_tools wins over allowed_tools in the SDK, so every self-improve/auto-fix
    Max-backend attempt could read files and draft a change but never actually apply one - exactly the
    "the Edit and Write tools were disabled" failure a live run hit. This checks the override actually reaches
    ClaudeAgentOptions."""
    import claude_agent_sdk

    captured: dict = {}
    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query(claude_agent_sdk, captured))
    settings.claude_code_oauth_token = "sk-ant-oat-test"

    await run_once(settings, system="sys", prompt="hi", tools=["Read", "Edit", "Write", "Glob", "Grep"],
                   disallowed_tools=ENGINEER_BLOCKED)

    assert captured["options"].disallowed_tools == ENGINEER_BLOCKED
    assert "Edit" not in captured["options"].disallowed_tools
    assert "Write" not in captured["options"].disallowed_tools
    assert "Bash" in captured["options"].disallowed_tools  # still blocked - only Edit/Write are freed up
