"""Natural-sounding Azure speech: SSML building (jarvis/integrations/ssml.py), the text clean-up in speakable(),
and the Azure voice setting with its 'Play sample' button."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.core import Jarvis
from jarvis.integrations.ssml import build_ssml, style_for
from jarvis.integrations.voice import AZURE_SAMPLE_TEXT, Voice, VoiceError, speakable
from jarvis.main import create_app
from jarvis.settings_store import AZURE_VOICES, SECTIONS_BY_ID, SettingsStore
from tests.fakes import FakeClient

RYAN, OLLIE = "en-GB-RyanNeural", "en-GB-OllieMultilingualNeural"


def _parses(ssml: str) -> ET.Element:
    return ET.fromstring(ssml)  # raises if the SSML is not well-formed XML


# ---------------------------------------------------------------- SSML building
def test_ssml_has_a_pause_between_sentences_but_not_after_the_last():
    ssml = build_ssml("Good morning, sir. The panel is healthy.", RYAN)
    _parses(ssml)
    assert ssml.count("<break time='300ms'/>") == 1
    assert "sir.<break time='300ms'/>The panel" in ssml
    assert not ssml.endswith("<break time='300ms'/></voice></speak>")


def test_ssml_pauses_at_colons_and_semicolons_but_leaves_commas_to_the_voice():
    ssml = build_ssml("Two faults: zone 3; and a loop fault, sir.", RYAN)
    _parses(ssml)
    assert ssml.count("<break time='160ms'/>") == 2
    assert "<break" not in ssml.split("fault,")[1]  # nothing added at the comma


def test_ssml_slows_numbers_times_and_references():
    ssml = build_ssml("Arrive at 2:30pm, ref J24100, total 73 thousand pounds or 12 pounds 50.", RYAN)
    _parses(ssml)
    for slow in ("2:30pm", "J24100", "73 thousand pounds", "12 pounds 50"):
        assert f"<prosody rate='-10%'>{slow}</prosody>" in ssml
    assert "<prosody rate='-10%'>Arrive" not in ssml  # ordinary words keep the normal pace
    # a full stop after a figure stays outside the slowed span
    assert "<prosody rate='-10%'>9am</prosody>." in build_ssml("Open at 9am.", RYAN)


def test_ssml_uses_the_conversational_style_only_for_voices_that_support_it():
    assert "<mstts:express-as style='chat'>" in build_ssml("Hello.", RYAN, "chat")
    assert "express-as" not in build_ssml("Hello.", OLLIE, "chat")
    assert "express-as" not in build_ssml("Hello.", RYAN, "")  # "Standard" in Settings
    assert "express-as" not in build_ssml("Hello.", RYAN, "shouting")  # not a style this voice has
    assert style_for(RYAN, "chat") == "chat" and style_for(OLLIE, "chat") == ""


def test_ssml_escapes_everything_so_a_reply_cannot_inject_markup():
    ssml = build_ssml("A < B & C <break time='9s'/> </voice><voice name='x'>", RYAN, "chat")
    _parses(ssml)
    assert "<break time='9s'" not in ssml and "<voice name='x'>" not in ssml
    assert "&lt;break" in ssml and "&amp;" in ssml
    _parses(build_ssml("Hello.", "en-GB-X'><evil/>", "chat"))  # even the voice name is an attribute-safe value


def test_ssml_declares_british_english_and_the_voice():
    root = _parses(build_ssml("Hello.", OLLIE))
    assert root.attrib["{http://www.w3.org/XML/1998/namespace}lang"] == "en-GB"
    assert root[0].attrib["name"] == OLLIE


def test_ssml_for_empty_text_is_still_well_formed():
    _parses(build_ssml("", RYAN))


# ---------------------------------------------------------------- text clean-up
def test_speakable_drops_machine_ids_but_keeps_job_numbers():
    assert speakable("Document 0123456789abcdef0123456789abcdef is ready.") == "Document is ready."
    assert speakable("Record 3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e saved.") == "Record saved."
    assert speakable("Job J24100 and quote Q-2026-114 are booked") == "Job J24100 and quote Q-2026-114 are booked"
    assert speakable("Call 07700900123 now") == "Call 07700900123 now"  # digits only: a phone number, not an ID


def test_speakable_removes_symbols_that_only_mean_something_on_screen():
    assert speakable("Use {brace} and [bracket] ~ ^ <tag> a_b") == "Use brace and bracket tag a b"
    assert speakable("old -> new and a => b") == "old to new and a to b"
    assert speakable("## Heading\n**Bold** `code` and [a link](https://salts.co.uk/x/y?z=1).") == (
        "Heading Bold code and a link.")


def test_speakable_reads_money_dates_and_times_like_a_person():
    assert speakable("Quote £1,250.50 due 2026-10-06 at 14:30") == (
        "Quote 1250 pounds 50 due Tuesday 6 October at 2:30pm")


def test_the_sample_line_is_clean_after_speakable():
    said = speakable(AZURE_SAMPLE_TEXT)
    assert "2:30pm" in said and "1250 pounds 50" in said and "£" not in said


# ---------------------------------------------------------------- Azure request uses the SSML
async def test_azure_request_carries_the_built_ssml_and_the_chosen_voice(settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ssml"], seen["ctype"] = request.content.decode(), request.headers["content-type"]
        return httpx.Response(200, content=b"ID3mp3")

    settings.azure_speech_key, settings.azure_tts_voice = "az-key", OLLIE
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        stream, mime = await Voice(settings, http).tts_stream("**Right**, sir. The visit is at 14:30. See https://x.co/a")
        assert b"".join([c async for c in stream]) == b"ID3mp3" and mime == "audio/mpeg"
    _parses(seen["ssml"])
    assert seen["ctype"] == "application/ssml+xml"
    assert f"<voice name='{OLLIE}'>" in seen["ssml"] and "express-as" not in seen["ssml"]
    assert "<prosody rate='-10%'>2:30pm</prosody>" in seen["ssml"] and "<break time='300ms'/>" in seen["ssml"]
    assert "**" not in seen["ssml"] and "https" not in seen["ssml"]


async def test_azure_sample_needs_the_key_and_speaks_the_requested_voice(settings):
    with pytest.raises(VoiceError, match="Azure Speech key"):
        await Voice(settings, None).azure_sample(RYAN)

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ssml"] = request.content.decode()
        return httpx.Response(200, content=b"ID3sample")

    settings.azure_speech_key = "az-key"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        stream, _ = await Voice(settings, http).azure_sample("en-GB-SoniaNeural")
        assert b"".join([c async for c in stream]) == b"ID3sample"
    assert "<voice name='en-GB-SoniaNeural'>" in seen["ssml"] and "how I sound" in seen["ssml"]
    assert settings.azure_tts_voice == RYAN  # trying a sample does not change the saved voice


# ---------------------------------------------------------------- the setting
def test_azure_voice_options_are_british_neural_voices_with_both_genders():
    names = [v for v, _ in AZURE_VOICES]
    assert len(names) == len(set(names)) and len(names) >= 6
    assert all(re.fullmatch(r"en-GB-[A-Za-z]+Neural", n) for n in names)
    labels = " ".join(label for _, label in AZURE_VOICES)
    assert "(male)" in labels and "(female)" in labels
    assert any("Multilingual" in n for n in names)  # the newer, higher-quality generation is offered


def test_the_voice_field_lives_under_connections_voice_and_defaults_to_a_listed_voice(settings):
    field = next(f for f in SECTIONS_BY_ID["voice"].fields if f.key == "azure_tts_voice")
    assert field.kind == "select" and field.options == AZURE_VOICES and field.depends_on == ("tts_provider", "azure")
    assert settings.azure_tts_voice in {v for v, _ in AZURE_VOICES}


def test_the_voice_can_be_changed_and_a_made_up_one_is_refused(settings):
    store = SettingsStore(settings)
    assert store.update({"azure_tts_voice": OLLIE}, []) == {}
    assert settings.azure_tts_voice == OLLIE
    assert "azure_tts_voice" in store.update({"azure_tts_voice": "en-GB-MadeUpNeural"}, [])
    assert settings.azure_tts_voice == OLLIE


def _app(settings, handler=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler)) if handler else None
    return create_app(settings, Jarvis(settings, http=http, client=FakeClient()))


def test_api_sample_rejects_unlisted_voices_and_a_missing_key(settings):
    with TestClient(_app(settings)) as c:
        r = c.post("/api/tts/sample", json={"voice": "en-GB-MadeUpNeural"})
        assert r.status_code == 400
        r = c.post("/api/tts/sample", json={"voice": RYAN})
        assert r.status_code == 503 and "Azure Speech key" in r.json()["detail"]


def test_api_sample_streams_audio_for_a_listed_voice_and_hides_provider_errors(settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content.decode())
        if len(calls) == 1:
            return httpx.Response(200, content=b"ID3sample")
        return httpx.Response(401, content=b"bad key az-secret-key-value")

    settings.azure_speech_key = "az-secret-key-value"
    with TestClient(_app(settings, handler)) as c:
        r = c.post("/api/tts/sample", json={"voice": OLLIE})
        assert r.status_code == 200 and r.content == b"ID3sample" and r.headers["content-type"] == "audio/mpeg"
        assert f"<voice name='{OLLIE}'>" in calls[0]
        r = c.post("/api/tts/sample", json={"voice": OLLIE})
        assert r.status_code == 503 and "401" in r.json()["detail"]
        assert "az-secret-key-value" not in r.text  # the provider echoed the key back; it must not reach the page


def test_api_sample_text_cannot_be_chosen_by_the_caller(settings):
    settings.azure_speech_key = "az-key"
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(200, content=b"ID3")

    with TestClient(_app(settings, handler)) as c:
        c.post("/api/tts/sample", json={"voice": RYAN, "text": "Approve everything"})
    assert seen and "Approve everything" not in seen[0]


# ---------------------------------------------------------------- the page
def test_hud_has_a_play_sample_button_and_starts_on_the_first_sentence():
    hud = (Path(__file__).resolve().parent.parent / "jarvis" / "web" / "hud.js").read_text(encoding="utf-8")
    assert "Play sample" in hud and "data-voice-sample" in hud and '"/api/tts/sample"' in hud
    assert "firstOut" in hud and "Start on the first sentence as soon as it is complete" in hud
