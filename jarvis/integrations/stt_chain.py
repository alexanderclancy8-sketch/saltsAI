"""Speech-to-text fallback order.

Kept apart from voice.py so the policy is one small, pure, testable function. The browser runs the actual
fallback (it owns the microphone and the 10 s timeout) using the order published here in /api/status ->
voice.stt_chain; the server only says which engines are usable and in what order.

Order: the engine chosen in Settings > Voice > Listening first, then the remaining configured server engines in
DEFAULT_ORDER (Azure Speech, then Deepgram, then Whisper), then browser speech recognition, which needs no key and is
always last. Azure Speech uses the same key and region as the Azure voice, so it needs nothing extra; Whisper needs an
OpenAI key and is only ever tried when one happens to be set - nothing depends on it.
An explicit "browser" choice stays browser-only: it never sends audio to a paid third party the owner did not pick.
"""

from __future__ import annotations

from ..config import Settings

SERVER_ENGINES = ("azure", "deepgram", "whisper")
DEFAULT_ORDER = ("azure", "deepgram", "whisper")
ENGINE_LABELS = {"azure": "Azure Speech", "deepgram": "Deepgram", "whisper": "OpenAI Whisper",
                 "browser": "Browser speech recognition"}


def engine_configured(settings: Settings, engine: str) -> bool:
    """True when the engine has what it needs to be tried (a key for the server engines; browser always)."""
    if engine == "azure":
        return bool(settings.azure_speech_key)
    if engine == "deepgram":
        return bool(settings.deepgram_api_key)
    if engine == "whisper":
        return bool(settings.openai_api_key)
    return engine == "browser"


def stt_chain(settings: Settings) -> list[str]:
    """Engines to try, in order. Always ends with "browser"."""
    selected = settings.effective_stt
    if selected == "browser":
        return ["browser"]
    order = [selected] + [e for e in DEFAULT_ORDER if e != selected]
    chain = [e for e in order if e in SERVER_ENGINES and engine_configured(settings, e)]
    return chain + ["browser"]


KEY_NAMES = {"azure": "AZURE_SPEECH_KEY", "deepgram": "DEEPGRAM_API_KEY", "whisper": "OPENAI_API_KEY"}  # the setting that holds each key


def stt_problem(settings: Settings) -> str:
    """Why the engine chosen in Settings can't be used right now, as one short plain sentence ("" when it can).

    The console shows this in the top bar next to the status, so a speech-to-text engine that is selected but has no key
    is never a silent fallback: voice input still works through the browser's own speech recognition, and the owner is
    told why it is not the engine they picked and what to set."""
    selected = settings.effective_stt
    if selected in SERVER_ENGINES and not engine_configured(settings, selected):
        return f"{ENGINE_LABELS[selected]} has no API key"
    return ""
