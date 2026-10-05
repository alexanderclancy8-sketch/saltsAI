"""Speech-to-text failing (console redesign phase 3, item 3): an engine that is selected but has no key is never a silent
fallback. The server says why in /api/status -> voice.stt_problem, the routine test names the cause and the fallback, the
chain still ends at the browser's own speech recognition, and the console puts a plain text label in the top bar.
The label itself is checked in a real browser in tests/test_console_browser_phase3.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.core import Jarvis
from jarvis.integrations.stt_chain import stt_chain, stt_problem
from jarvis.integrations.voice import SpeechToTextCheck, VoiceError
from jarvis.main import create_app
from tests.fakes import FakeClient

WEB = Path(__file__).resolve().parent.parent / "jarvis" / "web"


@pytest.mark.parametrize("provider,keys,problem,chain", [
    ("whisper", {}, "OpenAI Whisper has no API key", ["browser"]),
    ("deepgram", {}, "Deepgram has no API key", ["browser"]),
    ("whisper", {"openai_api_key": "sk-test"}, "", ["whisper", "browser"]),
    ("deepgram", {"deepgram_api_key": "dg-test"}, "", ["deepgram", "browser"]),
    ("browser", {}, "", ["browser"]),
    ("auto", {}, "", ["browser"]),  # nothing chosen and nothing configured is simply the browser, not a fault
    ("auto", {"openai_api_key": "sk-test"}, "", ["whisper", "browser"]),
])
def test_the_problem_is_named_only_when_the_chosen_engine_cannot_be_used(settings, provider, keys, problem, chain):
    settings.stt_provider = provider
    for key, value in keys.items():
        setattr(settings, key, value)
    assert stt_problem(settings) == problem
    assert stt_chain(settings) == chain and chain[-1] == "browser"  # always ends at browser speech recognition


async def test_the_voice_config_the_console_reads_carries_the_problem(settings):
    settings.stt_provider = "whisper"
    j = Jarvis(settings, client=FakeClient())
    config = j.voice.client_config()
    assert config["stt"] == "whisper" and config["stt_chain"] == ["browser"]
    assert config["stt_problem"] == "OpenAI Whisper has no API key"
    with TestClient(create_app(settings, j)) as c:
        assert c.get("/api/status").json()["voice"]["stt_problem"] == config["stt_problem"]
    settings.openai_api_key = "sk-test"
    assert j.voice.client_config()["stt_problem"] == ""
    await j.http.aclose()


async def test_the_routine_test_names_the_missing_key_and_says_what_voice_input_does_instead(settings):
    settings.stt_provider = "whisper"
    j = Jarvis(settings, client=FakeClient())
    with pytest.raises(VoiceError) as e:
        await SpeechToTextCheck(j.voice).check()
    text = str(e.value)
    assert "OpenAI Whisper has no API key (OPENAI_API_KEY)" in text and "browser's speech recognition" in text
    assert "Connections > Voice" in text and "sk-" not in text
    await j.http.aclose()


async def test_the_routine_tester_records_that_as_a_failure_not_silence(settings):
    settings.stt_provider = "whisper"
    j = Jarvis(settings, client=FakeClient())
    results = await j.tester.run_system()
    stt = next(r for r in results if r.name == "Integration: Speech-to-text")
    assert stt.ok is False and "no API key" in stt.detail and "browser" in stt.detail
    await j.http.aclose()


def test_the_top_bar_has_a_text_status_for_voice_input_and_the_script_fills_it_from_both_sources():
    index = (WEB / "index.html").read_text(encoding="utf-8")
    hud = (WEB / "hud.js").read_text(encoding="utf-8")
    bar = index[index.index('<header class="topbar">'):index.index("</header>")]
    assert 'id="stt-status"' in bar and 'role="status"' in bar  # in the top bar, announced to screen readers
    assert "const sttStatus" in hud and "S.voice.stt_problem" in hud  # the server's reason ...
    assert "sttStatus.engine(engine, note, warn)" in hud  # ... and a failure that happens while listening
    assert "Voice input: browser fallback - " in hud
    css = (WEB / "hud.css").read_text(encoding="utf-8")
    assert ".stt-status" in css and ".stt-status[hidden]" in css
