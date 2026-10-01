"""Speech-output text preparation (speakable) and TTS retry in jarvis/integrations/voice.py. STT error handling
is covered in tests/test_voice.py."""

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


@pytest.mark.parametrize("stamp", [
    "2026-10-06T09:00:00Z", "2026-10-06T09:00Z", "2026-10-06T09:00:00.250Z",
    "2026-10-06T09:00:00+01:00", "2026-10-06T09:00+01:00", "2026-10-06T09:00:00+0100",
    "2026-10-06T09:00:00-05:00", "2026-10-06T09:00-0500", "2026-10-06T09:00+01",
    "2026-10-06 09:00:00+01:00", "2026-10-06 09:00:00-05:00", "2026-10-06 09:00Z",
])
def test_speakable_leaves_timezone_qualified_timestamps_alone(stamp):
    # humanize ignores UTC offsets, so guessing would speak the wrong hour - and no later rule may touch the
    # "09:00" inside either ("2026-10-06T9amZ", "...T09:00:00+1am")
    assert speakable(f"Logged {stamp} by the panel") == f"Logged {stamp} by the panel"


@pytest.mark.parametrize("written, spoken", [
    ("Arrive 09:00Z sharp", "Arrive 09:00Z sharp"),
    ("Arrive 09:00+01:00 sharp", "Arrive 09:00+01:00 sharp"),
    # "09:00-05:00" is deliberately a range ("22:00-06:00" is a real night shift); only an unambiguous offset stays
    ("Arrive 09:00-0500 sharp", "Arrive 09:00-0500 sharp"),
    ("Arrive at UTC+01:00", "Arrive at UTC+01:00"),
])
def test_speakable_leaves_bare_times_with_an_offset_alone(written, spoken):
    assert speakable(written) == spoken


def test_speakable_reads_space_separated_timestamps_without_an_offset():
    assert speakable("Booked 2026-10-06 09:00 for the visit") == "Booked Tuesday 6 October at 9am for the visit"
    assert speakable("Slot 2026-10-06 09:00-10:30 today") == "Slot Tuesday 6 October at 9am to 10:30am today"
    assert speakable("From 2026-10-06T09:00 to 2026-10-06T17:30") == (
        "From Tuesday 6 October at 9am to Tuesday 6 October at 5:30pm")
    assert speakable("Visit 2026-10-06 and 2026-10-07 14:00.") == (
        "Visit Tuesday 6 October and Wednesday 7 October at 2pm.")


@pytest.mark.parametrize("ref", [
    "PO 2026-10-01", "PO: 2026-10-01", "po 2026-10-01", "PO#2026-10-01", "INV 2026-10-01", "Invoice 2026-10-01",
    "REF 2026-10-01", "Ref. 2026-10-01", "JOB 2026-10-01", "Job 2026-10-01", "QUOTE 2026-10-01", "Q 2026-10-01",
    "PO-2026-10-01", "INV-2026-10-01",
])
def test_speakable_does_not_read_a_reference_number_as_a_date(ref):
    assert speakable(f"Raise {ref} today") == f"Raise {ref} today"


def test_speakable_still_reads_dates_that_only_sit_near_a_reference_word():
    assert speakable("PO 2026-10-01 is due 2026-10-06") == "PO 2026-10-01 is due Tuesday 6 October"
    assert speakable("PO 2026-10-01 delivered at 14:30") == "PO 2026-10-01 delivered at 2:30pm"
    assert speakable("The job is booked for 2026-10-06") == "The job is booked for Tuesday 6 October"
    assert speakable("Quote sent on 2026-10-06.") == "Quote sent on Tuesday 6 October."
    assert speakable("Due Q4 2026") == "Due Q4 2026"


@pytest.mark.parametrize("written, spoken", [
    ("Open 12:30-14:00", "Open 12:30pm to 2pm"),
    ("Open 12:30 - 14:00 daily", "Open 12:30pm to 2pm daily"),
    ("Open 12:30–14:00", "Open 12:30pm to 2pm"),
    ("Mon-Fri 09:00-17:30.", "Mon-Fri 9am to 5:30pm."),
    ("Window 08:00-12:00, then 13:00-17:00", "Window 8am to 12pm, then 1pm to 5pm"),
    ("Open 12:30 to 14:00", "Open 12:30pm to 2pm"),
])
def test_speakable_reads_a_time_range_consistently(written, spoken):
    assert speakable(written) == spoken


@pytest.mark.parametrize("written, spoken", [
    ("It costs £12,50 each", "It costs 12 pounds 50 each"),
    ("It costs £12,50.", "It costs 12 pounds 50."),
    ("£12,00 flat", "12 pounds flat"),
    ("£1,250.50 total", "1250 pounds 50 total"),
    ("£12,500 total", "12500 pounds total"),
    ("£1,234,567 total", "1234567 pounds total"),
    ("Parts £5,10,15", "Parts 5 pounds,10,15"),  # a list of numbers is not a decimal comma
])
def test_speakable_never_leaves_a_stray_comma_after_pounds(written, spoken):
    assert speakable(written) == spoken


@pytest.mark.parametrize("text", [
    "Job J24100 is booked",
    "Postcode LS1 4AB",
    "Call 0113 496 0000 or +44 113 496 0000 or 07700 900123",
    "Panel at 192.168.1.10:8080 and 10.0.0.5:2200 and 127.0.0.1:0900",
    "Aspect ratio 3:2 and 16:9",
    "Zone 3 of 4 on loop 2",
    "Quote Q-2026-114 for 24 devices",
    "Version 1.2.3-rc1",
])
def test_speakable_leaves_codes_postcodes_phone_numbers_ip_ports_and_ratios_alone(text):
    assert speakable(text) == text


def test_speakable_keeps_british_standard_numbers_sensible():
    assert speakable("Per BS EN 54-13:2017.") == "Per BS EN 54 part 13:2017."
    assert speakable("Per BS 5839-1:2017") == "Per BS 5839 part 1:2017"
    assert speakable("BS EN 54-2 and BS EN 54-4") == "BS EN 54 part 2 and BS EN 54 part 4"
    assert "am" not in speakable("BS EN 54-13:2017") and "pm" not in speakable("BS EN 54-13:2017")


def test_speakable_leaves_invalid_dates_and_times_alone():
    assert speakable("Odd 2026-13-45 date") == "Odd 2026-13-45 date"
    assert speakable("Odd 2026-10-06T25:00 stamp") == "Odd 2026-10-06T25:00 stamp"
    assert speakable("Score 25:61") == "Score 25:61"


# ---------------------------------------------------------------- live Deepgram relay survives garbage frames
async def test_deepgram_relay_ignores_garbled_frames_in_both_directions(monkeypatch):
    import json

    import websockets

    sent_upstream: list = []

    class FakeDeepgram:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, data):
            sent_upstream.append(data)

        def __aiter__(self):
            async def frames():
                yield "this is not json"
                yield b"\xff\xfe"
                yield "[1, 2, 3]"  # valid JSON, but not an event object
                yield json.dumps({"type": "Results", "is_final": True,
                                  "channel": {"alternatives": [{"transcript": "hello"}]}})
            return frames()

    monkeypatch.setattr(websockets, "connect", lambda *a, **k: FakeDeepgram())

    class FakeBrowser:
        def __init__(self):
            self.sent: list = []
            self._incoming = iter([{"type": "websocket.receive", "text": "{broken"},
                                   {"type": "websocket.receive", "text": json.dumps({"type": "KeepAlive"})},
                                   {"type": "websocket.disconnect"}])

        async def receive(self):
            return next(self._incoming)

        async def send_json(self, payload):
            self.sent.append(payload)

    browser = FakeBrowser()
    async with httpx.AsyncClient() as http:
        await Voice(Settings(deepgram_api_key="dg", _env_file=None), http).relay_deepgram(browser)
    assert [p["text"] for p in browser.sent if p["type"] == "transcript"] == ["hello"]
    assert json.dumps({"type": "KeepAlive"}) in sent_upstream  # the valid control message still got through


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


async def test_tts_network_failure_is_retried_once_then_raised(monkeypatch):
    monkeypatch.setattr(voice_mod, "TTS_RETRY_DELAY_S", 0)
    calls = []

    def boom(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("no route")

    s, http = _eleven(boom)
    async with http:
        with pytest.raises(httpx.TransportError):  # main.py's /api/tts turns this into the browser-voice fallback
            await Voice(s, http).tts_stream("Hello")
    assert len(calls) == 2


@pytest.mark.parametrize("status, attempts", [(429, 2), (500, 2), (502, 2), (503, 2), (504, 2),
                                              (400, 1), (401, 1), (403, 1), (404, 1), (422, 1), (501, 1)])
async def test_tts_retries_only_the_listed_statuses_and_never_more_than_once(monkeypatch, status, attempts):
    monkeypatch.setattr(voice_mod, "TTS_RETRY_DELAY_S", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, content=b"nope")

    s, http = _eleven(handler)
    async with http:
        with pytest.raises(VoiceError, match=str(status)):
            await Voice(s, http).tts_stream("Hello")
    assert len(calls) == attempts
