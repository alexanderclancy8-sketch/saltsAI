"""AGENT BUFFER GUARD: a run that reads a huge file hits the Claude Agent SDK's 1 MB cap on one CLI message
(CLIJSONDecodeError 'JSON message exceeded maximum buffer size'). The engineer loops get a bigger limit and one retry, the
failure is classified, and the run is recorded as buffer_exceeded instead of the raw traceback."""

from __future__ import annotations

import pytest

from jarvis.brain.max_backend import (BUFFER_RETRY_NOTE, ENGINEER_BUFFER_BYTES, RETRY_BUFFER_BYTES, BufferExceeded,
                                      run_once)
from jarvis.core import Jarvis
from jarvis.services import fixer, security_watch, self_improve
from jarvis.services.agent_runs import BUFFER_EXCEEDED_OUTCOME, AgentRuns, derive_lesson, is_buffer_exceeded
from tests.fakes import FakeClient


def sdk_buffer_error(limit: int = 1048576):
    """The error the SDK raised in issue_fix runs #15 and #16, built the way its transport builds it."""
    from claude_agent_sdk._errors import CLIJSONDecodeError

    return CLIJSONDecodeError(f"JSON message exceeded maximum buffer size of {limit} bytes",
                              ValueError(f"Buffer size {limit + 1} exceeds limit {limit}"))


# --------------------------------------------------------------------------- classification
def test_the_sdk_buffer_error_is_classified_as_buffer_exceeded():
    err = sdk_buffer_error()
    assert "exceeded maximum buffer size" in str(err)
    assert is_buffer_exceeded(err)
    assert is_buffer_exceeded(str(err))


def test_a_wrapped_buffer_error_is_still_classified():
    try:
        try:
            raise sdk_buffer_error()
        except Exception as inner:  # noqa: BLE001
            raise RuntimeError("Claude run failed") from inner
    except RuntimeError as outer:
        assert is_buffer_exceeded(outer)


def test_the_recorded_outcome_text_is_recognised_and_other_errors_are_not():
    assert is_buffer_exceeded(BUFFER_EXCEEDED_OUTCOME)
    assert is_buffer_exceeded(BufferExceeded(BUFFER_EXCEEDED_OUTCOME))
    assert not is_buffer_exceeded(RuntimeError("github fell over"))
    assert not is_buffer_exceeded(ValueError("Failed to decode JSON: unexpected token"))
    assert not is_buffer_exceeded("Stopped after 80 turns without finishing")
    assert not is_buffer_exceeded(None)


async def test_a_run_that_fails_with_the_buffer_error_is_recorded_as_buffer_exceeded(settings):
    j = Jarvis(settings, client=FakeClient())
    runs = AgentRuns(j.db)
    with pytest.raises(Exception):
        with runs.track("fixer", "Issue #24: security"):
            raise sdk_buffer_error()
    r = runs.recent()[0]
    assert r["status"] == "failed"
    assert r["outcome"].startswith("buffer_exceeded")
    assert "Traceback" not in r["outcome"] and "CLIJSONDecodeError" not in r["outcome"]
    assert "line ranges" in r["lesson"]
    await j.http.aclose()


async def test_an_ordinary_failure_keeps_its_own_outcome(settings):
    j = Jarvis(settings, client=FakeClient())
    runs = AgentRuns(j.db)
    with pytest.raises(RuntimeError):
        with runs.track("fixer", "Issue #1: boom"):
            raise RuntimeError("github fell over")
    assert "github fell over" in runs.recent()[0]["outcome"]
    assert "buffer_exceeded" not in derive_lesson("failed", "github fell over")
    await j.http.aclose()


# --------------------------------------------------------------------------- the larger limit and the single retry
def _install(monkeypatch, plan):
    """Fake SDK query: each call takes the next entry of `plan` ('ok' or an exception to raise) and records its options."""
    import claude_agent_sdk

    calls: list = []

    async def fake_query(*, prompt, options=None, transport=None):
        calls.append((prompt, options))
        step = plan[len(calls) - 1]
        if isinstance(step, BaseException):
            raise step
        yield claude_agent_sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                             num_turns=1, session_id="s1", result="ok")

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    return calls


async def test_a_guarded_run_gets_a_ten_megabyte_buffer(settings, monkeypatch):
    settings.claude_code_oauth_token = "sk-ant-oat-test"
    calls = _install(monkeypatch, ["ok"])
    await run_once(settings, system="s", prompt="hi", buffer_guard=True)
    assert len(calls) == 1 and calls[0][1].max_buffer_size == ENGINEER_BUFFER_BYTES == 10 * 1024 * 1024


async def test_other_runs_keep_the_sdk_default_buffer(settings, monkeypatch):
    settings.claude_code_oauth_token = "sk-ant-oat-test"
    calls = _install(monkeypatch, ["ok"])
    await run_once(settings, system="s", prompt="hi")
    assert calls[0][1].max_buffer_size is None


async def test_a_buffer_failure_is_retried_once_with_a_larger_limit(settings, monkeypatch):
    settings.claude_code_oauth_token = "sk-ant-oat-test"
    calls = _install(monkeypatch, [sdk_buffer_error(ENGINEER_BUFFER_BYTES), "ok"])
    result = await run_once(settings, system="s", prompt="hi", buffer_guard=True)
    assert result.result == "ok"
    assert [c[1].max_buffer_size for c in calls] == [ENGINEER_BUFFER_BYTES, RETRY_BUFFER_BYTES]
    assert RETRY_BUFFER_BYTES > ENGINEER_BUFFER_BYTES
    assert calls[0][0] == "hi" and calls[1][0] == "hi" + BUFFER_RETRY_NOTE


async def test_two_buffer_failures_raise_buffer_exceeded_with_the_clear_outcome(settings, monkeypatch):
    settings.claude_code_oauth_token = "sk-ant-oat-test"
    calls = _install(monkeypatch, [sdk_buffer_error(ENGINEER_BUFFER_BYTES), sdk_buffer_error(RETRY_BUFFER_BYTES)])
    with pytest.raises(BufferExceeded) as raised:
        await run_once(settings, system="s", prompt="hi", buffer_guard=True)
    assert len(calls) == 2  # exactly one retry
    assert str(raised.value) == BUFFER_EXCEEDED_OUTCOME and is_buffer_exceeded(raised.value)


async def test_a_different_error_is_not_retried(settings, monkeypatch):
    settings.claude_code_oauth_token = "sk-ant-oat-test"
    calls = _install(monkeypatch, [RuntimeError("cli crashed"), "ok"])
    with pytest.raises(RuntimeError, match="cli crashed"):
        await run_once(settings, system="s", prompt="hi", buffer_guard=True)
    assert len(calls) == 1


# --------------------------------------------------------------------------- the prompts
@pytest.mark.parametrize("prompt", [fixer.ENGINEER_SYSTEM, self_improve.SELF_IMPROVE_SYSTEM,
                                    security_watch.SECURITY_SYSTEM])
def test_engineer_prompts_say_to_read_big_files_in_ranges_and_never_the_index_html(prompt):
    assert "line ranges" in prompt and "grep" in prompt
    assert "app/static/index.html" in prompt and "never cat" in prompt
