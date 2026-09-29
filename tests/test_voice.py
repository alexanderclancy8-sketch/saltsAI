"""Voice: the effective_tts cascade prefers a paid, configured provider but always has a free fallback
(Piper, not the browser's robotic voice), and Piper's model download is cached - not re-fetched on every
reply."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from jarvis.integrations.voice import PIPER_VOICES, Voice


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


def test_piper_voices_registry_only_lists_voices_with_a_confirmed_quality_tier():
    assert PIPER_VOICES  # not empty
    assert all(isinstance(q, str) and q for q in PIPER_VOICES.values())
