"""Speech-to-text fallback order.

Kept apart from voice.py so the policy is one small, pure, testable function. The browser runs the actual
fallback (it owns the microphone and the 10 s timeout) using the order published here in /api/status ->
voice.stt_chain; the server only says which engines are usable and in what order.

Order: the engine chosen in Settings > Voice > Listening first, then the remaining configured server engines in
DEFAULT_ORDER (Deepgram, then Whisper), then browser speech recognition, which needs no key and is always last.
An explicit "browser" choice stays browser-only: it never sends audio to a paid third party the owner did not pick.
"""

from __future__ import annotations

from ..config import Settings

SERVER_ENGINES = ("deepgram", "whisper")
DEFAULT_ORDER = ("deepgram", "whisper")
ENGINE_LABELS = {"deepgram": "Deepgram", "whisper": "OpenAI Whisper", "browser": "Browser speech recognition"}


def engine_configured(settings: Settings, engine: str) -> bool:
    """True when the engine has what it needs to be tried (a key for the server engines; browser always)."""
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
