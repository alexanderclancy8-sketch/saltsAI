"""Speech-output text preparation, TTS retry and STT error handling in jarvis/integrations/voice.py."""

from __future__ import annotations

import httpx
import pytest

from jarvis.config import Settings
from jarvis.integrations import voice as voice_mod
from jarvis.integrations.voice import Voice, VoiceError, speakable


# ---------------------------------------------------------------- speakable(): prosody-friendly text
@pytest.mark.parametrize("written, spoken", [
    ("Cash is £73k.", "Cash is 73 thousand pounds."),
    ("Owed £2.5 million in total", "Owed 2.5 million pounds in total"),
    ("Invoice for £11,947.32 outstanding", "Invoice for 11947 pounds 32 outstanding"),
    ("A £1 fee", "A 1 pound fee"),
    ("Exactly £12.00 due", "Exactly 12 pounds due"),
    ("£12.5 each", "12 pounds 50 each"),
    ("VAT is 20% now", "VAT is 20 percent now"),
    ("Margin of 12.5 %", "Margin of 12.5 percent"),
    ("We handle 12,345 calls", "We handle 12345 calls"),
])
def test_speakable_expands_money_percentages_and_thousands(written, spoken):
    assert speakable(written) == spoken


def test_speakable_reads_dates_and_times_the_way_a_person_would():
    assert speakable("Due 2026-10-06T09:00 sharp") == "Due Tuesday 6 October at 9am sharp"
    assert speakable("Visit on 2026-10-06.") == "Visit on Tuesday 6 October."
    assert speakable("Arrive at 14:30") == "Arrive at 2:30pm"
    assert speakable("Gate opens 09:00") == "Gate opens 9am"
    # already-spoken times are not converted twice
    assert speakable("Arrive at 10:30am") == "Arrive at 10:30am"


def test_speakable_leaves_timezone_qualified_timestamps_alone():
    # humanize ignores UTC offsets, so guessing would speak the wrong hour
    assert "2026-10-06T09:00:00Z" in speakable("Logged 2026-10-06T09:00:00Z")


def test_speakable_handles_links_emails_standards_and_abbreviations():
    assert speakable("See https://www.salts.co.uk/about/us.") == "See salts dot co dot uk."
    assert speakable("Email bob@salts.co.uk today") == "Email bob at salts dot co dot uk today"
    assert speakable("Per BS 5839-1 and BS EN 54-2") == "Per BS 5839 part 1 and BS EN 54 part 2"
    assert speakable("Panels, e.g. Gent, i.e. addressable, etc.") == (
        "Panels, for example Gent, that is addressable, et cetera")
    assert speakable("Gent vs Advanced, approx. two days") == "Gent versus Advanced, approximately two days"
    assert speakable("Tested & signed off →  done ✅") == "Tested and signed off to done"


def test_speakable_leaves_ordinary_text_untouched():
    text = "Zone 3 of 4 is in fault, sir."
    assert speakable(text) == text


# ---------------------------------------------------------------- TTS retry
def _eleven(handler):
    s = Settings(elevenlabs_api_key="el-key", _env_file=None)
    return s, httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_tts_retries_once_on_a_transient_provider_error(monkeypatch):
    monkeypatch.setattr(voice_mod, "TTS_RETRY_DELAY_S", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, content=b"ID3ok")

    s, http = _eleven(handler)
    async with http:
        stream, _ = await Voice(s, http).tts_stream("Good evening, sir.")
        assert b"".join([c async for c in stream]) == b"ID3ok"
    assert len(calls) == 2


async def test_tts_does_not_retry_a_client_error_and_gives_up_after_one_retry(monkeypatch):
    monkeypatch.setattr(voice_mod, "TTS_RETRY_DELAY_S", 0)
    calls = []

    def unauthorised(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, content=b"bad key")

    s, http = _eleven(unauthorised)
    async with http:
        with pytest.raises(VoiceError, match="401"):
            await Voice(s, http).tts_stream("Hello")
    assert len(calls) == 1

    calls.clear()

    def down(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, content=b"oops")

    s, http = _eleven(down)
    async with http:
        with pytest.raises(VoiceError, match="500"):
            await Voice(s, http).tts_stream("Hello")
    assert len(calls) == 2


async def test_tts_network_failure_is_a_voice_error_after_one_retry(monkeypatch):
    monkeypatch.setattr(voice_mod, "TTS_RETRY_DELAY_S", 0)
    calls = []

    def boom(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("no route")

    s, http = _eleven(boom)
    async with http:
        with pytest.raises(VoiceError, match="unreachable"):
            await Voice(s, http).tts_stream("Hello")
    assert len(calls) == 2


# ---------------------------------------------------------------- STT errors
def _deepgram(handler):
    s = Settings(deepgram_api_key="dg", _env_file=None)
    return Voice(s, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_transcribe_returns_the_transcript():
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": {"channels": [{"alternatives": [{"transcript": "hello"}]}]}})

    v = _deepgram(ok)
    async with v.http:
        assert await v.transcribe(b"audio", "audio/webm") == "hello"
        assert await v.transcribe(b"", "audio/webm") == ""  # silence: nothing to send


async def test_transcribe_turns_provider_failures_into_voice_errors():
    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    v = _deepgram(server_error)
    async with v.http:
        with pytest.raises(VoiceError, match="502"):
            await v.transcribe(b"audio", "audio/webm")

    def odd_shape(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": {"channels": []}})

    v = _deepgram(odd_shape)
    async with v.http:
        with pytest.raises(VoiceError, match="Unexpected"):
            await v.transcribe(b"audio", "audio/webm")

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    v = _deepgram(unreachable)
    async with v.http:
        with pytest.raises(VoiceError, match="unreachable"):
            await v.transcribe(b"audio", "audio/webm")


async def test_transcribe_rejects_oversized_uploads(monkeypatch):
    monkeypatch.setattr(voice_mod, "MAX_STT_BYTES", 10)
    v = _deepgram(lambda request: httpx.Response(200, json={}))
    async with v.http:
        with pytest.raises(VoiceError, match="too long"):
            await v.transcribe(b"x" * 11, "audio/webm")
