"""Real-browser check of how a recording reaches Azure Speech. Azure's short-audio endpoint takes 16 kHz mono PCM WAV, not the
WebM/Opus (or MP4) that MediaRecorder produces, so the console decodes the recording and re-encodes it as WAV before it
uploads. Headless Chrome is started with its fake microphone, so MediaRecorder really records (a test tone), the page really
converts it, and the upload is intercepted at the network layer and inspected byte by byte. Azure itself is never called.
Skipped when Playwright or a Chrome it can launch is missing."""
from __future__ import annotations

import struct

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from tests.fakes import FakeClient  # noqa: E402
from tests.live_server import LiveServer  # noqa: E402
from tests.test_console_browser import _settings  # noqa: E402

FAKE_MIC = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"]


@pytest.fixture(scope="module")
def chrome():
    with sync_api.sync_playwright() as p:
        b = None
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                b = p.chromium.launch(headless=True, args=FAKE_MIC, **kwargs)
                break
            except Exception:  # noqa: BLE001 - that browser is not installed here
                continue
        if b is None:
            pytest.skip("no Chrome/Chromium that Playwright can launch")
        yield b
        b.close()


@pytest.fixture(scope="module")
def azure_console(tmp_path_factory, restore_process_timezone):
    settings = _settings(tmp_path_factory, azure_speech_key="az-test-key-0123456789", stt_provider="azure")
    j = Jarvis(settings, client=FakeClient())
    srv = LiveServer(create_app(settings, j))
    yield srv, j
    srv.stop()


def test_the_console_uploads_a_16khz_mono_wav_to_the_azure_engine(chrome, azure_console):
    srv, j = azure_console
    ctx = chrome.new_context(viewport={"width": 1280, "height": 800}, permissions=["microphone"])
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    seen = {}

    def fake_stt(route):
        request = route.request
        seen["url"] = request.url
        seen["body"] = request.post_data_buffer
        route.fulfill(status=200, content_type="application/json", body='{"text": "hello jarvis", "engine": "azure"}')

    page.route("**/api/stt?engine=*", fake_stt)
    try:
        page.goto(srv.url + "/", wait_until="domcontentloaded")
        page.wait_for_selector("#needs-list .need, #needs-list .needs-clear:not(:has-text('Loading'))", timeout=15000)
        assert page.evaluate("fetch('/api/status').then(r => r.json()).then(s => [s.voice.stt, s.voice.stt_chain])") == [
            "azure", ["azure", "browser"]]
        assert page.is_hidden("#stt-status")  # Azure configured and chosen: healthy, nothing to warn about
        page.click("#btn-mic")
        page.wait_for_function("document.getElementById('btn-mic').classList.contains('on')", timeout=10000)
        page.wait_for_timeout(1800)  # let the fake microphone record a couple of seconds
        page.click("#btn-mic")
        page.wait_for_function("document.querySelectorAll('.msg.user').length >= 1", timeout=20000)
        assert "hello jarvis" in page.inner_text(".msg.user")  # the transcript came back and was sent as the turn
    finally:
        ctx.close()
    assert seen["url"].endswith("/api/stt?engine=azure")
    body = seen["body"]
    assert b'filename="speech.wav"' in body, body[:300]
    at = body.index(b"RIFF")
    wav = body[at:]
    assert wav[8:12] == b"WAVE" and wav[12:16] == b"fmt "
    audio_format, channels, rate, _, _, bits = struct.unpack("<HHIIHH", wav[20:36])
    assert (audio_format, channels, rate, bits) == (1, 1, 16000, 16)  # PCM, mono, 16 kHz, 16-bit: what Azure takes
    data_at = wav.index(b"data")
    (size,) = struct.unpack("<I", wav[data_at + 4:data_at + 8])
    assert size >= 16000 * 2  # at least a second of audio
    count = size // 2
    samples = struct.unpack(f"<{count}h", wav[data_at + 8:data_at + 8 + count * 2])
    assert max(samples) - min(samples) > 200  # the fake microphone's beep somewhere in the clip, not all silence
    assert not page.errors, page.errors
