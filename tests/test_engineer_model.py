"""ENGINEER_MODEL / ENGINEER_EFFORT: the engineering agents (self_improve, fixer, security_watch) can run on their
own model, falling back to JARVIS_MODEL when it is blank, and an unsupported effort is rejected clearly.

No model ID is hard-coded in the code under test; the IDs below are arbitrary strings the tests make up."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jarvis.brain.max_backend import ENGINEER_BLOCKED, run_once
from jarvis.config import ENGINEER_EFFORT_LEVELS, Settings
from jarvis.core import Jarvis
from jarvis.services.workspace import Workspace
from jarvis.settings_store import FIELDS, OWNER_ONLY_KEYS, SettingsStore
from tests.fakes import FakeClient

CHAT = "chat-model-for-tests"
ENGINEER = "engineer-model-for-tests"


def make(settings):
    settings.jarvis_model = CHAT
    settings.plugin_context7_enabled = False
    settings.plugin_superpowers_enabled = False
    return Jarvis(settings, client=FakeClient())


def _fresh(tmp_path, **kw) -> Settings:
    return Settings(data_dir=tmp_path / "data", scheduler_enabled=False, anthropic_api_key="test", _env_file=None, **kw)


# --------------------------------------------------------------------------- the setting itself
def test_blank_engineer_model_falls_back_to_jarvis_model(settings):
    settings.jarvis_model = CHAT
    assert settings.engineer_model == ""
    assert settings.engineer_model_or_default() == CHAT
    settings.engineer_model = "   "  # whitespace only counts as blank
    assert settings.engineer_model_or_default() == CHAT


def test_explicit_engineer_model_is_used(settings):
    settings.jarvis_model = CHAT
    settings.engineer_model = f"  {ENGINEER} "
    assert settings.engineer_model_or_default() == ENGINEER


def test_engineer_model_is_read_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINEER_MODEL", ENGINEER)
    assert _fresh(tmp_path).engineer_model_or_default() == ENGINEER


def test_engineer_effort_accepts_every_level_the_sdk_and_api_support(tmp_path):
    assert set(ENGINEER_EFFORT_LEVELS) == {"low", "medium", "high", "xhigh", "max"}
    for level in ENGINEER_EFFORT_LEVELS:
        assert _fresh(tmp_path, engineer_effort=level).engineer_effort == level


def test_engineer_effort_default_and_normalising(tmp_path, monkeypatch):
    assert _fresh(tmp_path).engineer_effort == "high"
    assert _fresh(tmp_path, engineer_effort="  MAX ").engineer_effort == "max"
    assert _fresh(tmp_path, engineer_effort="").engineer_effort == "high"  # blank = the default
    monkeypatch.setenv("ENGINEER_EFFORT", "XHigh")
    assert _fresh(tmp_path).engineer_effort == "xhigh"


@pytest.mark.parametrize("bad", ["turbo", "maximum", "extra high", "0", "none"])
def test_invalid_engineer_effort_is_rejected_clearly(tmp_path, bad):
    with pytest.raises(ValidationError) as err:
        _fresh(tmp_path, engineer_effort=bad)
    text = str(err.value)
    assert "ENGINEER_EFFORT" in text and "low, medium, high, xhigh, max" in text


def test_invalid_engineer_effort_in_the_environment_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINEER_EFFORT", "turbo")
    with pytest.raises(ValidationError):
        _fresh(tmp_path)


# --------------------------------------------------------------------------- Settings page
def test_both_settings_are_on_the_page_and_owner_only():
    assert FIELDS["engineer_model"].kind == "text"  # free text: the owner supplies the exact ID
    assert FIELDS["engineer_effort"].kind == "select"
    assert {v for v, _ in FIELDS["engineer_effort"].options} == set(ENGINEER_EFFORT_LEVELS)
    assert {"engineer_model", "engineer_effort"} <= OWNER_ONLY_KEYS


def test_settings_page_rejects_an_unsupported_effort_and_keeps_a_model_id(settings):
    store = SettingsStore(settings)
    errors = store.update({"engineer_effort": "turbo"}, [])
    assert errors.get("engineer_effort")
    assert settings.engineer_effort == "high"
    assert store.update({"engineer_effort": "max", "engineer_model": ENGINEER}, []) == {}
    assert settings.engineer_effort == "max" and settings.engineer_model_or_default() == ENGINEER
    assert store.update({}, ["engineer_model"]) == {}  # cleared: back to following JARVIS_MODEL
    assert settings.engineer_model_or_default() == settings.jarvis_model


# --------------------------------------------------------------------------- Max backend plumbing
def _fake_query(claude_agent_sdk, captured):
    async def fake_query(*, prompt, options=None, transport=None):
        captured["options"] = options
        yield claude_agent_sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                             num_turns=1, session_id="s1", result="ok")

    return fake_query


async def test_run_once_defaults_to_jarvis_model_and_accepts_an_override(settings, monkeypatch):
    import claude_agent_sdk

    captured: dict = {}
    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query(claude_agent_sdk, captured))
    settings.claude_code_oauth_token = "sk-ant-oat-test"
    settings.jarvis_model = CHAT

    await run_once(settings, system="s", prompt="p")
    assert captured["options"].model == CHAT
    await run_once(settings, system="s", prompt="p", model=ENGINEER, effort="max")
    assert captured["options"].model == ENGINEER and captured["options"].effort == "max"


def _capture_run_once(monkeypatch):
    seen: dict = {}

    async def fake_run_once(s, **kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr("jarvis.brain.max_backend.run_once", fake_run_once)
    return seen


async def test_self_improve_max_path_uses_engineer_model(settings, monkeypatch, tmp_path):
    j = make(settings)
    seen = _capture_run_once(monkeypatch)
    with pytest.raises(RuntimeError):
        await j.self_improve._engineer_max("add a thing", Workspace(tmp_path))  # noqa: SLF001
    assert seen["model"] == CHAT  # blank ENGINEER_MODEL -> same as JARVIS_MODEL
    settings.engineer_model = ENGINEER
    settings.engineer_effort = "max"
    with pytest.raises(RuntimeError):
        await j.self_improve._engineer_max("add a thing", Workspace(tmp_path))  # noqa: SLF001
    assert seen["model"] == ENGINEER and seen["effort"] == "max"
    assert "Bash" in seen["disallowed_tools"] and seen["disallowed_tools"] == ENGINEER_BLOCKED
    await j.http.aclose()


async def test_fixer_max_path_uses_engineer_model(settings, monkeypatch, tmp_path):
    j = make(settings)
    settings.engineer_model = ENGINEER
    seen = _capture_run_once(monkeypatch)
    issue = {"id": 1, "reporter": "Sam", "title": "t", "description": "d"}
    with pytest.raises(RuntimeError):
        await j.fixer._run_engineer_max(issue, Workspace(tmp_path))  # noqa: SLF001
    assert seen["model"] == ENGINEER
    await j.http.aclose()


async def test_security_watch_max_path_uses_engineer_model(settings, monkeypatch, tmp_path):
    j = make(settings)
    seen = _capture_run_once(monkeypatch)
    with pytest.raises(RuntimeError):
        await j.security_watch._review_max(Workspace(tmp_path))  # noqa: SLF001
    assert seen["model"] == CHAT
    settings.engineer_model = ENGINEER
    with pytest.raises(RuntimeError):
        await j.security_watch._review_max(Workspace(tmp_path))  # noqa: SLF001
    assert seen["model"] == ENGINEER
    await j.http.aclose()


# --------------------------------------------------------------------------- API backend plumbing
async def test_self_improve_api_path_uses_engineer_model(settings, tmp_path):
    j = make(settings)
    settings.llm_backend = "api"
    await j.self_improve._engineer("add a thing", Workspace(tmp_path))  # noqa: SLF001
    assert j.client.beta.messages.calls[-1]["model"] == CHAT
    settings.engineer_model = ENGINEER
    settings.engineer_effort = "max"
    await j.self_improve._engineer("add a thing", Workspace(tmp_path))  # noqa: SLF001
    call = j.client.beta.messages.calls[-1]
    assert call["model"] == ENGINEER and call["output_config"] == {"effort": "max"}
    await j.http.aclose()


async def test_fixer_api_path_uses_engineer_model(settings, tmp_path):
    j = make(settings)
    settings.llm_backend = "api"
    issue = {"id": 1, "reporter": "Sam", "title": "t", "description": "d"}
    await j.fixer.run_engineer(issue, Workspace(tmp_path))
    assert j.client.beta.messages.calls[-1]["model"] == CHAT
    settings.engineer_model = ENGINEER
    await j.fixer.run_engineer(issue, Workspace(tmp_path))
    assert j.client.beta.messages.calls[-1]["model"] == ENGINEER
    await j.http.aclose()


async def test_security_watch_api_path_uses_engineer_model(settings, tmp_path):
    j = make(settings)
    settings.llm_backend = "api"
    await j.security_watch._review(Workspace(tmp_path))  # noqa: SLF001
    assert j.client.beta.messages.calls[-1]["model"] == CHAT
    settings.engineer_model = ENGINEER
    await j.security_watch._review(Workspace(tmp_path))  # noqa: SLF001
    assert j.client.beta.messages.calls[-1]["model"] == ENGINEER
    await j.http.aclose()
