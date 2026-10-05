"""Azure Speech as the primary server-side speech-to-text engine: it reuses the key and region that already power the Azure
voice, so listening needs no OpenAI key. Everything here uses a mocked HTTP transport - real Azure is never called."""

from __future__ import annotations

import io
import logging
import wave
from types import SimpleNamespace

import httpx
import pytest

from jarvis.core import Jarvis
from jarvis.integrations import voice as voice_mod
from jarvis.integrations.stt_chain import ENGINE_LABELS, KEY_NAMES, stt_chain, stt_problem
from jarvis.integrations.voice import STTError, SpeechToTextCheck, Voice, prepare_azure_audio, silent_wav
from jarvis.services.connection_tests import TESTS
from tests.fakes import FakeClient

AZ_KEY = "az0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    monkeypatch.setattr("jarvis.integrations.voice.STT_RETRY_DELAY_S", 0)


def _azure(settings, handler):
    settings.azure_speech_key = AZ_KEY
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Voice(settings, http), http


def _wav(seconds=1.0, rate=16000, channels=1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * channels * int(rate * seconds))
    return buf.getvalue()


# --------------------------------------------------------------------------- the request and the answer
async def test_azure_success_sends_wav_to_the_regional_endpoint_with_the_key_in_a_header(settings):
    settings.azure_speech_region = "uksouth"
    seen = {}

    def handler(request):
        seen.update(url=request.url, headers=request.headers, body=request.content)
        return httpx.Response(200, json={"RecognitionStatus": "Success", "DisplayText": "Check the Gent panel.",
                                         "Offset": 1, "Duration": 2})

    voice, http = _azure(settings, handler)
    audio = _wav(1.5)
    assert await voice.transcribe(audio, "audio/wav") == "Check the Gent panel."
    url = seen["url"]
    assert url.host == "uksouth.stt.speech.microsoft.com"
    assert url.path == "/speech/recognition/conversation/cognitiveservices/v1"
    assert url.params["language"] == "en-GB" and url.params["format"] == "simple" and url.params["profanity"] == "raw"
    assert seen["headers"]["ocp-apim-subscription-key"] == AZ_KEY
    assert seen["headers"]["content-type"] == "audio/wav; codecs=audio/pcm; samplerate=16000"
    assert seen["body"] == audio and AZ_KEY.encode() not in seen["body"]
    assert "authorization" not in seen["headers"]  # the key only travels in Azure's own header
    await http.aclose()


@pytest.mark.parametrize("status", ["NoMatch", "InitialSilenceTimeout", "BabbleTimeout"])
async def test_azure_hearing_nothing_is_an_empty_transcript_not_a_fault(settings, status):
    voice, http = _azure(settings, lambda r: httpx.Response(200, json={"RecognitionStatus": status}))
    assert await voice.transcribe(silent_wav(), "audio/wav") == ""
    await http.aclose()


async def test_azure_error_status_in_a_200_body_is_a_clear_error(settings):
    voice, http = _azure(settings, lambda r: httpx.Response(200, json={"RecognitionStatus": "Error"}))
    with pytest.raises(STTError) as exc:
        await voice.transcribe(silent_wav(), "audio/wav")
    assert "Azure Speech couldn't process the recording" in str(exc.value)
    await http.aclose()


async def test_an_unreadable_answer_is_reported(settings):
    voice, http = _azure(settings, lambda r: httpx.Response(200, text="<html>not json</html>"))
    with pytest.raises(STTError) as exc:
        await voice.transcribe(silent_wav(), "audio/wav")
    assert "couldn't read" in str(exc.value)
    await http.aclose()


# --------------------------------------------------------------------------- failures in plain English
@pytest.mark.parametrize("code", [401, 403])
async def test_a_rejected_key_names_the_setting_and_the_region_and_never_echoes_the_key(settings, code, caplog):
    body = {"error": {"code": str(code), "message": f"Access denied: invalid subscription key {AZ_KEY}"}}
    voice, http = _azure(settings, lambda r: httpx.Response(code, json=body))
    with caplog.at_level(logging.DEBUG), pytest.raises(STTError) as exc:
        await voice.transcribe(silent_wav(), "audio/wav")
    text = str(exc.value)
    assert f"Azure Speech returned {code}: the API key was rejected" in text
    assert "AZURE_SPEECH_KEY" in text and "AZURE_SPEECH_REGION" in text
    assert AZ_KEY not in text and AZ_KEY not in caplog.text and "OPENAI" not in text
    assert exc.value.provider == "azure" and exc.value.status == code and not exc.value.transient
    await http.aclose()


async def test_the_free_tier_running_out_is_a_quota_message_not_a_bad_key(settings):
    voice, http = _azure(settings, lambda r: httpx.Response(403, text="Out of call volume quota. Quota will be replenished"))
    with pytest.raises(STTError) as exc:
        await voice.transcribe(silent_wav(), "audio/wav")
    assert "quota or billing problem" in str(exc.value) and "rejected" not in str(exc.value)
    await http.aclose()


@pytest.mark.parametrize("code,fragment,transient", [
    (404, "region not found - check AZURE_SPEECH_REGION", False),
    (429, "rate limited", True),
    (400, "the audio was rejected", False),
    (500, "problem on its side", True),
])
async def test_other_statuses_map_to_a_plain_sentence(settings, code, fragment, transient):
    voice, http = _azure(settings, lambda r: httpx.Response(code, text="upstream"))
    with pytest.raises(STTError) as exc:
        await voice.transcribe(silent_wav(), "audio/wav", retry=False)
    assert fragment in str(exc.value) and exc.value.transient is transient
    await http.aclose()


async def test_a_transient_failure_is_retried_once(settings):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"RecognitionStatus": "Success", "DisplayText": "Hello."})

    voice, http = _azure(settings, handler)
    assert await voice.transcribe(silent_wav(), "audio/wav") == "Hello." and len(calls) == 2
    await http.aclose()


async def test_a_bad_region_never_becomes_part_of_a_hostname(settings):
    settings.azure_speech_region = "evil.example.com/x"
    voice, http = _azure(settings, lambda r: pytest.fail("must not call out"))
    with pytest.raises(STTError) as exc:
        await voice.transcribe(silent_wav(), "audio/wav")
    assert "AZURE_SPEECH_REGION" in str(exc.value)
    await http.aclose()


async def test_audio_and_keys_never_reach_the_log(settings, caplog):
    audio = _wav(1)
    voice, http = _azure(settings, lambda r: httpx.Response(200, json={"RecognitionStatus": "Success", "DisplayText": "secret words"}))
    with caplog.at_level(logging.DEBUG):
        await voice.transcribe(audio, "audio/wav")
    assert AZ_KEY not in caplog.text and "secret words" not in caplog.text and repr(audio[:20]) not in caplog.text
    await http.aclose()


# --------------------------------------------------------------------------- audio formats
async def test_wav_ogg_and_other_formats_are_handled():
    wav = _wav(1)
    assert await prepare_azure_audio(wav, "audio/wav") == (wav, "audio/wav; codecs=audio/pcm; samplerate=16000")
    wav8 = _wav(1, rate=8000)
    assert (await prepare_azure_audio(wav8, "audio/wav"))[1].endswith("samplerate=8000")
    ogg = b"OggS" + b"\x00" * 100
    assert await prepare_azure_audio(ogg, "audio/ogg;codecs=opus") == (ogg, "audio/ogg; codecs=opus")


async def test_a_recording_azure_cannot_read_is_a_clear_non_transient_error_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(voice_mod.shutil, "which", lambda name: None)
    with pytest.raises(STTError) as exc:
        await prepare_azure_audio(b"\x1aE\xdf\xa3" + b"\x00" * 100, "audio/webm;codecs=opus")
    assert "can't read this kind of recording (audio/webm)" in str(exc.value) and not exc.value.transient
    # a stereo 44.1 kHz WAV also needs converting
    with pytest.raises(STTError):
        await prepare_azure_audio(_wav(1, rate=44100, channels=2), "audio/wav")


async def test_other_formats_are_converted_with_ffmpeg_when_the_server_has_it(monkeypatch):
    calls = []
    monkeypatch.setattr(voice_mod.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return SimpleNamespace(returncode=0, stdout=b"\x01\x00" * 16000, stderr=b"")

    monkeypatch.setattr(voice_mod.subprocess, "run", fake_run)
    webm = b"\x1aE\xdf\xa3" + b"\x00" * 50
    body, ctype = await prepare_azure_audio(webm, "audio/webm")
    assert ctype == "audio/wav; codecs=audio/pcm; samplerate=16000" and body[:4] == b"RIFF"
    cmd, kw = calls[0]
    assert isinstance(cmd, list) and cmd[0] == "/usr/bin/ffmpeg" and kw["input"] == webm and "shell" not in kw
    assert "-ar" in cmd and cmd[cmd.index("-ar") + 1] == "16000"


async def test_ffmpeg_failing_is_the_same_clear_error(monkeypatch):
    monkeypatch.setattr(voice_mod.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(voice_mod.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"", stderr=b"x"))
    with pytest.raises(STTError):
        await prepare_azure_audio(b"junk" * 10, "audio/mp4")


async def test_a_clip_over_azures_sixty_second_limit_is_refused_clearly(settings):
    voice, http = _azure(settings, lambda r: pytest.fail("must not upload an over-long clip"))
    with pytest.raises(STTError) as exc:
        await voice.transcribe(_wav(61), "audio/wav")
    assert "60 seconds" in str(exc.value) and exc.value.status == 413 and not exc.value.transient
    await http.aclose()


# --------------------------------------------------------------------------- the chain and the selection
@pytest.mark.parametrize("provider,keys,expected", [
    ("auto", {"azure_speech_key": "k"}, ["azure", "browser"]),
    ("auto", {"azure_speech_key": "k", "deepgram_api_key": "d"}, ["deepgram", "azure", "browser"]),
    ("auto", {"azure_speech_key": "k", "openai_api_key": "s"}, ["azure", "whisper", "browser"]),
    ("azure", {"azure_speech_key": "k", "deepgram_api_key": "d", "openai_api_key": "s"},
     ["azure", "deepgram", "whisper", "browser"]),
    ("whisper", {"azure_speech_key": "k", "deepgram_api_key": "d", "openai_api_key": "s"},
     ["whisper", "azure", "deepgram", "browser"]),
    ("browser", {"azure_speech_key": "k"}, ["browser"]),
])
def test_chain_order_azure_deepgram_whisper_browser(settings, provider, keys, expected):
    settings.stt_provider = provider
    for k, v in keys.items():
        setattr(settings, k, v)
    assert stt_chain(settings) == expected


def test_no_openai_key_is_ever_required(settings):
    settings.azure_speech_key = "k"
    assert settings.openai_api_key == ""
    assert settings.effective_stt == "azure" and stt_problem(settings) == ""
    assert stt_chain(settings) == ["azure", "browser"]


@pytest.mark.parametrize("old_choice", ["whisper", "deepgram"])
def test_an_old_keyless_choice_falls_through_to_azure_instead_of_breaking_voice_input(settings, old_choice):
    settings.stt_provider = old_choice  # chosen back when Azure listening didn't exist, key never set
    assert settings.effective_stt != "azure"
    settings.azure_speech_key = "k"
    assert settings.effective_stt == "azure" and stt_problem(settings) == ""


def test_whisper_chosen_on_purpose_with_no_key_and_no_azure_is_still_reported(settings):
    settings.stt_provider = "whisper"
    assert stt_problem(settings) == "OpenAI Whisper has no API key"  # explicit and nothing else to use: still visible


def test_labels_and_key_names(settings):
    assert ENGINE_LABELS["azure"] == "Azure Speech" and KEY_NAMES["azure"] == "AZURE_SPEECH_KEY"


async def test_azure_selected_without_a_key_says_which_setting_to_fill_in(settings):
    settings.stt_provider = "azure"
    voice = Voice(settings, SimpleNamespace(post=lambda *a, **k: pytest.fail("must not call the provider")))
    with pytest.raises(STTError) as exc:
        await voice.transcribe(silent_wav(), "audio/wav")
    assert "AZURE_SPEECH_KEY" in str(exc.value) and "OPENAI" not in str(exc.value)


def test_the_console_is_told_the_azure_first_chain_to_walk_when_an_engine_fails(settings):
    settings.azure_speech_key = AZ_KEY
    config = Voice(settings, None).client_config()
    assert config["stt"] == "azure" and config["stt_chain"] == ["azure", "browser"] and config["stt_problem"] == ""
    settings.deepgram_api_key = "dg-key-value-1234"  # an existing Deepgram key keeps its live streaming
    config = Voice(settings, None).client_config()
    assert config["stt"] == "deepgram" and config["stt_chain"] == ["deepgram", "azure", "browser"]


# --------------------------------------------------------------------------- routine test and connection test
async def test_the_routine_test_is_healthy_with_azure_configured_and_no_openai_key(settings):
    settings.azure_speech_key = AZ_KEY
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"RecognitionStatus": "InitialSilenceTimeout"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    j = Jarvis(settings, http=http, client=FakeClient())
    out = await SpeechToTextCheck(j.voice).check()
    assert out.startswith("Azure Speech accepted a test clip") and "OpenAI" not in out
    assert calls and calls[0].url.host.endswith(".stt.speech.microsoft.com")
    await http.aclose()


async def test_the_routine_tester_passes_speech_to_text_with_azure_and_never_mentions_openai(settings):
    settings.azure_speech_key = AZ_KEY
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"RecognitionStatus": "NoMatch"})))
    j = Jarvis(settings, http=http, client=FakeClient())
    results = await j.tester.run_system()
    stt = next(r for r in results if r.name == "Integration: Speech-to-text")
    assert stt.ok is True and "OpenAI" not in stt.detail and "API key" not in stt.detail
    await http.aclose()


async def test_the_routine_tester_with_an_old_whisper_choice_and_azure_set_does_not_fail(settings):
    settings.stt_provider = "whisper"  # the owner's stored choice from before
    settings.azure_speech_key = AZ_KEY
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"RecognitionStatus": "NoMatch"})))
    j = Jarvis(settings, http=http, client=FakeClient())
    stt = next(r for r in await j.tester.run_system() if r.name == "Integration: Speech-to-text")
    assert stt.ok is True and "No OpenAI" not in stt.detail
    await http.aclose()


async def test_the_voice_connection_test_checks_azure_listening(settings):
    settings.azure_speech_key, settings.tts_provider = AZ_KEY, "browser"
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"RecognitionStatus": "InitialSilenceTimeout"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    j = Jarvis(settings, http=http, client=FakeClient())
    ok, text = await TESTS["voice"](j)
    assert ok and "Listening: Azure Speech works" in text and "OpenAI" not in text
    assert seen[0].headers["ocp-apim-subscription-key"] == AZ_KEY and AZ_KEY not in text
    await http.aclose()


async def test_the_voice_connection_test_with_a_bad_azure_key_says_so_plainly(settings):
    settings.azure_speech_key, settings.tts_provider = AZ_KEY, "browser"
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "x"})))
    j = Jarvis(settings, http=http, client=FakeClient())
    with pytest.raises(STTError) as exc:
        await TESTS["voice"](j)
    assert "AZURE_SPEECH_KEY" in str(exc.value) and AZ_KEY not in str(exc.value)
    await http.aclose()


def test_the_listening_setting_offers_azure_and_describes_whisper_as_optional():
    from jarvis.settings_store import FIELDS

    stt = FIELDS["stt_provider"]
    values = [o[0] for o in stt.options]
    assert values[:2] == ["auto", "azure"] and "whisper" in values
    assert "Azure Speech" in stt.help and "no extra account" in stt.help
    assert "optional" in FIELDS["openai_api_key"].label.lower() and "Not needed" in FIELDS["openai_api_key"].help
