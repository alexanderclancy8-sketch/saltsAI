"""Voice: the effective_tts cascade prefers a paid, configured provider but always has a free fallback
(Piper, not the browser's robotic voice), and Piper's model download is cached - not re-fetched on every
reply."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from jarvis.integrations.voice import PIPER_VOICES, Voice, audio_extension


def test_effective_tts_prefers_a_paid_key_but_falls_back_to_piper_not_browser(settings):
    assert settings.effective_tts == "piper"  # nothing configured - free, not robotic
    settings.azure_speech_key = "key"
    assert settings.effective_tts == "azure"
    settings.elevenlabs_api_key = "key"
    assert settings.effective_tts == "elevenlabs"  # elevenlabs wins even with azure also set
    settings.tts_provider = "browser"
    assert settings.effective_tts == "browser"  # an explicit choice always wins over auto-detection


async def test_piper_voice_is_downloaded_once_and_then_cached(settings, monkeypatch):
    calls = []

    async def fake_get(url, **kw):
        calls.append(url)
        return httpx.Response(200, content=b"fake-model-bytes")

    http = SimpleNamespace(get=fake_get)
    voice = Voice(settings, http)

    path1 = await voice._ensure_piper_voice("alan", "medium")
    assert path1.exists() and path1.read_bytes() == b"fake-model-bytes"
    assert len(calls) == 2  # .onnx and .onnx.json

    path2 = await voice._ensure_piper_voice("alan", "medium")
    assert path2 == path1
    assert len(calls) == 2  # not downloaded again - already cached on disk


async def test_piper_synthesize_reuses_the_loaded_model_across_calls(settings, monkeypatch):
    loads = []

    class FakeVoice:
        def synthesize_wav(self, text, wav_file, syn_config=None):
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00\x00" * 10)

    class FakePiperVoice:
        @staticmethod
        def load(path):
            loads.append(path)
            return FakeVoice()

    monkeypatch.setattr("piper.PiperVoice", FakePiperVoice, raising=False)
    voice = Voice(settings, SimpleNamespace())
    model_path = settings.data_dir / "fake-model.onnx"

    wav_bytes_1 = voice._piper_synthesize(model_path, "Hello, sir.")
    wav_bytes_2 = voice._piper_synthesize(model_path, "Good morning.")

    assert wav_bytes_1.startswith(b"RIFF") and wav_bytes_2.startswith(b"RIFF")
    assert len(loads) == 1  # the second call reused the cached, already-loaded model


@pytest.mark.parametrize("mime,ext", [
    ("audio/webm;codecs=opus", "webm"), ("audio/webm", "webm"), ("audio/mp4", "mp4"),
    ("audio/mp4;codecs=mp4a.40.2", "mp4"), ("audio/x-m4a", "mp4"), ("audio/ogg;codecs=opus", "ogg"),
    ("audio/mpeg", "mp3"), ("audio/wav", "wav"), ("", "webm"),
])
def test_audio_extension_matches_the_recorded_container(mime, ext):
    assert audio_extension(mime) == ext


async def test_whisper_upload_uses_the_matching_extension_and_content_type(settings):
    settings.openai_api_key = "sk-test"
    seen = {}

    async def fake_post(url, **kw):
        seen.update(url=url, **kw)
        return httpx.Response(200, json={"text": "hello jarvis"}, request=httpx.Request("POST", url))

    voice = Voice(settings, SimpleNamespace(post=fake_post))
    assert settings.effective_stt == "whisper"
    assert await voice.transcribe(b"x" * 3000, "audio/mp4") == "hello jarvis"
    filename, _, content_type = seen["files"]["file"]
    assert filename == "speech.mp4" and content_type == "audio/mp4"

    await voice.transcribe(b"x" * 3000, "audio/webm;codecs=opus")
    filename, _, content_type = seen["files"]["file"]
    assert filename == "speech.webm" and content_type == "audio/webm"


# ---------------------------------------------------------------- STT failure handling (issue #5)
def _whisper_voice(settings, handler):
    settings.openai_api_key = "sk-test-secret-value"
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Voice(settings, http), http


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    monkeypatch.setattr("jarvis.integrations.voice.STT_RETRY_DELAY_S", 0)


async def test_stt_retries_once_on_a_transient_upstream_error(settings):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500, json={"error": {"message": "The server had an error"}})
        return httpx.Response(200, json={"text": "hello jarvis"})

    voice, http = _whisper_voice(settings, handler)
    assert await voice.transcribe(b"x" * 3000, "audio/webm;codecs=opus") == "hello jarvis"
    assert len(calls) == 2
    await http.aclose()


async def test_stt_gives_up_after_one_retry_with_a_clear_error(settings):
    from jarvis.integrations.voice import STTError

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={"error": {"message": "upstream exploded"}})

    voice, http = _whisper_voice(settings, handler)
    with pytest.raises(STTError) as exc:
        await voice.transcribe(b"x" * 3000, "audio/webm")
    assert len(calls) == 2  # original + exactly one retry
    assert exc.value.status == 500 and "OpenAI Whisper returned 500" in str(exc.value)
    assert "upstream exploded" in str(exc.value)
    await http.aclose()


async def test_stt_retries_a_timeout_once(settings):
    from jarvis.integrations.voice import STTError

    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("too slow", request=request)

    voice, http = _whisper_voice(settings, handler)
    with pytest.raises(STTError) as exc:
        await voice.transcribe(b"x" * 3000, "audio/webm")
    assert len(calls) == 2 and exc.value.status is None and "did not answer" in str(exc.value)
    await http.aclose()


async def test_stt_uses_a_bounded_timeout(settings):
    seen = {}

    async def fake_post(url, **kw):
        seen.update(kw)
        return httpx.Response(200, json={"text": "ok"}, request=httpx.Request("POST", url))

    settings.openai_api_key = "sk-test"
    await Voice(settings, SimpleNamespace(post=fake_post)).transcribe(b"x" * 3000, "audio/webm")
    timeout = seen["timeout"]
    assert isinstance(timeout, httpx.Timeout) and timeout.read <= 30 and timeout.connect <= 10


@pytest.mark.parametrize("status,body,expect_in,retried", [
    (401, {"error": {"message": "Incorrect API key provided: sk-test-secret-value"}}, "OPENAI_API_KEY", False),
    (429, {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}}, "billing", False),
    (404, {"error": {"message": "The model `whisper-9` does not exist"}}, "WHISPER_MODEL", False),
    (400, {"error": {"message": "Invalid file format"}}, "audio was rejected", False),
    (429, {"error": {"message": "Rate limit reached"}}, "rate limited", True),
])
async def test_stt_maps_upstream_errors_to_actionable_messages(settings, status, body, expect_in, retried):
    from jarvis.integrations.voice import STTError

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    voice, http = _whisper_voice(settings, handler)
    with pytest.raises(STTError) as exc:
        await voice.transcribe(b"x" * 3000, "audio/webm")
    message = str(exc.value)
    assert expect_in in message and str(status) in message
    assert "sk-test-secret-value" not in message  # never echo a key, even one the provider quoted back
    assert len(calls) == (2 if retried else 1)  # only transient failures are retried
    await http.aclose()


async def test_stt_rejects_oversized_audio_without_calling_the_provider(settings, monkeypatch):
    from jarvis.integrations.voice import STTError

    monkeypatch.setattr("jarvis.integrations.voice.STT_MAX_BYTES", 100)
    voice, http = _whisper_voice(settings, lambda request: pytest.fail("provider should not be called"))
    with pytest.raises(STTError) as exc:
        await voice.transcribe(b"x" * 101, "audio/webm")
    assert exc.value.status == 413
    await http.aclose()


async def test_stt_logs_the_real_upstream_error(settings, caplog):
    from jarvis.integrations.voice import STTError

    voice, http = _whisper_voice(settings, lambda r: httpx.Response(
        400, json={"error": {"message": "Audio file might be corrupted"}}))
    with caplog.at_level("WARNING", logger="jarvis.integrations.voice"), pytest.raises(STTError):
        await voice.transcribe(b"x" * 3000, "audio/webm")
    assert "status=400" in caplog.text and "Audio file might be corrupted" in caplog.text
    assert "sk-test-secret-value" not in caplog.text
    await http.aclose()


async def test_deepgram_errors_are_reported_too(settings):
    from jarvis.integrations.voice import STTError

    settings.deepgram_api_key = "dg-secret"
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(402, json={"err_code": "INSUFFICIENT_PERMISSIONS", "err_msg": "Out of credit"})))
    with pytest.raises(STTError) as exc:
        await Voice(settings, http).transcribe(b"x" * 3000, "audio/webm;codecs=opus")
    assert "Deepgram returned 402" in str(exc.value) and "Out of credit" in str(exc.value)
    assert "dg-secret" not in str(exc.value)
    await http.aclose()


def test_api_stt_returns_a_clear_502_not_a_bare_500(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from jarvis.core import Jarvis
    from jarvis.integrations.voice import STTError
    from jarvis.main import create_app
    from tests.fakes import FakeClient

    async def boom(self, audio, mime):
        raise STTError("OpenAI Whisper returned 401: the API key was rejected - check OPENAI_API_KEY", "whisper", 401)

    monkeypatch.setattr(Voice, "transcribe", boom)
    app = create_app(settings, Jarvis(settings, client=FakeClient()))
    with TestClient(app) as c:
        r = c.post("/api/stt", files={"audio": ("speech.webm", b"x" * 3000, "audio/webm")})
        assert r.status_code == 502
        assert "OPENAI_API_KEY" in r.json()["detail"] and r.json()["upstream_status"] == 401

        async def unexpected(self, audio, mime):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(Voice, "transcribe", unexpected)
        r = c.post("/api/stt", files={"audio": ("speech.webm", b"x" * 3000, "audio/webm")})
        assert r.status_code == 502 and "RuntimeError" in r.json()["detail"]


async def test_routine_tests_include_a_speech_to_text_check(settings):
    from jarvis.core import Jarvis
    from tests.fakes import FakeClient

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"text": ""})  # silence -> empty transcript is still a pass

    settings.openai_api_key = "sk-test"
    settings.fsm_base_url = ""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    j = Jarvis(settings, http=http, client=FakeClient())
    results = {r.name: r for r in await j.tester.run_system()}
    check = results["Integration: Speech-to-text"]
    assert check.ok, check.detail
    assert len(seen) == 1 and b"RIFF" in seen[0].content  # a real (tiny, silent) WAV went to the provider
    await http.aclose()


async def test_routine_speech_to_text_check_fails_when_the_provider_errors(settings):
    from jarvis.core import Jarvis
    from tests.fakes import FakeClient

    settings.openai_api_key = "sk-test"
    settings.fsm_base_url = ""
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(500, json={"error": {"message": "boom"}})))
    j = Jarvis(settings, http=http, client=FakeClient())
    results = {r.name: r for r in await j.tester.run_system()}
    check = results["Integration: Speech-to-text"]
    assert not check.ok and "500" in check.detail
    await http.aclose()


async def test_routine_speech_to_text_check_is_absent_with_browser_stt(settings):
    from jarvis.core import Jarvis
    from tests.fakes import FakeClient

    settings.stt_provider = "browser"
    j = Jarvis(settings, client=FakeClient())
    assert "Speech-to-text" not in j.tester.integrations
    await j.http.aclose()


def test_silent_wav_is_a_valid_tiny_clip():
    import io
    import wave

    from jarvis.integrations.voice import silent_wav

    with wave.open(io.BytesIO(silent_wav())) as w:
        assert w.getnchannels() == 1 and w.getframerate() == 16000 and w.getnframes() == 8000


def test_hud_shows_the_stt_error_text_next_to_the_indicator():
    from pathlib import Path

    web = Path(__file__).resolve().parent.parent / "jarvis" / "web"
    hud = (web / "hud.js").read_text(encoding="utf-8")
    assert "captionError(`${title}: ${body}`)" in hud and "data.detail" in hud
    assert ".caption.error" in (web / "hud.css").read_text(encoding="utf-8")


def test_hud_picks_a_supported_recorder_mime_and_reports_stt_errors():
    from pathlib import Path

    hud = (Path(__file__).resolve().parent.parent / "jarvis" / "web" / "hud.js").read_text(encoding="utf-8")
    assert "MediaRecorder.isTypeSupported(t)" in hud and '"audio/mp4"' in hud
    assert "`speech.${audioExtension(type)}`" in hud and '"speech.webm"' not in hud
    # The mic is released only once the recorder has delivered its final chunk.
    assert "rec.onstop = () => { stream.getTracks().forEach" in hud and "deferRelease" in hud
    # Empty recording, failed transcription and empty transcript are all surfaced to the user.
    for title in ("Nothing recorded", "Transcription failed", "Didn't catch that"):
        assert title in hud
    assert 'console.info("[stt] recording finished"' in hud and 'console.info("[stt] transcription response"' in hud


def test_piper_voices_registry_only_lists_voices_with_a_confirmed_quality_tier():
    assert PIPER_VOICES  # not empty
    assert all(isinstance(q, str) and q for q in PIPER_VOICES.values())
