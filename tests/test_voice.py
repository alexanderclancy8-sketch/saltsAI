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
