"""Voice I/O.

Speech output (TTS): ElevenLabs or Azure Neural TTS if a key is configured, otherwise Piper - a free,
local neural TTS engine (runs on the server itself, no external API, no cost) - which is why it, not the
browser's own robotic voice, is the default whenever nothing paid is set up. Speech input (STT): Deepgram
Nova-3 live streaming (proxied so the API key never reaches the browser), Deepgram pre-recorded, or OpenAI
Whisper for push-to-talk. If nothing is configured the browser's built-in speech engines are used.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import time
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import Settings
from .ssml import build_ssml
from .stt_chain import ENGINE_LABELS, KEY_NAMES, engine_configured, stt_chain, stt_problem

log = logging.getLogger(__name__)

ELEVEN_API = "https://api.elevenlabs.io/v1"
DEEPGRAM_API = "https://api.deepgram.com/v1/listen"
DEEPGRAM_WS = "wss://api.deepgram.com/v1/listen"
VOCAB = ["Jarvis", "Salts", "Salts FSM", "Vigilon", "Gent", "Kentec", "Apollo", "Paxton", "BAFE", "NSI", "SSAIB"]

# Free British English voices from the Piper project (github.com/OHF-Voice/piper1-gpl), model files hosted
# at huggingface.co/rhasspy/piper-voices - {voice: quality tier}. All confirmed to exist at "medium" quality
# as of writing; add more here (checking the quality folder actually exists first) rather than guessing one.
PIPER_VOICES = {"alan": "medium", "northern_english_male": "medium", "jenny_dioco": "medium", "alba": "medium"}
PIPER_VOICES_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB"
# The line the Settings page's "Play sample" button speaks - it has a time and a sum of money in it so the
# owner hears how the voice handles figures, not just a greeting.
AZURE_SAMPLE_TEXT = "Good morning, sir. Your next visit is at 14:30, and the quote comes to £1,250.50. This is how I sound."


WHISPER_API = "https://api.openai.com/v1/audio/transcriptions"
# Push-to-talk STT limits. A short clip normally comes back in 1-3 s, so 20 s is generous without leaving the
# owner staring at "Transcribing…" for a minute; OpenAI rejects uploads over 25 MB.
STT_TIMEOUT_S = 20.0
# Per-attempt limit when the browser drives the fallback (POST /api/stt?engine=...): it must be shorter than the
# browser's own ~10 s abort so the server answers with a proper error message rather than being cut off.
STT_ATTEMPT_TIMEOUT_S = 8.0
STT_CONNECT_TIMEOUT_S = 5.0
STT_RETRY_DELAY_S = 0.5
STT_MAX_BYTES = 25 * 1024 * 1024
TTS_RETRY_STATUSES = {429, 500, 502, 503, 504}  # transient upstream failures worth one retry
TTS_RETRY_DELAY_S = 0.5


class VoiceError(RuntimeError):
    pass


class STTError(VoiceError):
    """A speech-to-text provider failure with a message that is safe and useful to show the owner (no secrets)."""

    def __init__(self, message: str, provider: str = "", status: int | None = None, *, transient: bool = False):
        super().__init__(message)
        self.provider, self.status, self.transient = provider, status, transient


_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-*.]{4,}|Token\s+\S+|Bearer\s+\S+)", re.I)


def _redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Strip anything key-shaped - and any configured key value, whatever its format - from upstream text before
    it is logged or shown."""
    text = text or ""
    for secret in secrets:
        if secret and len(secret) >= 6:  # too-short values would mangle ordinary words
            text = text.replace(secret, "[redacted]")
    return _SECRET_RE.sub("[redacted]", text)


def _upstream_message(r: httpx.Response, secrets: tuple[str, ...] = ()) -> str:
    """The provider's own error text (OpenAI: error.message; Deepgram: err_msg / reason), shortened and redacted."""
    msg: Any = ""
    try:
        data = r.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or ""
        elif isinstance(err, str):
            msg = err
        msg = msg or data.get("err_msg") or data.get("reason") or data.get("message") or ""
    if not msg:
        msg = r.text or ""
    return _redact(" ".join(str(msg).split()), secrets)[:200]


# Machine identifiers nobody could say aloud: a UUID, or a long run of hex digits (a document or record ID). Job,
# quote and PO numbers are deliberately NOT matched - those are things the owner expects to hear.
_MACHINE_ID = re.compile(
    r"[ \t]*\b(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{12,})\b", re.I)
# Characters that only mean something on screen (markdown, brackets, code and diff marks).
_STRAY_SYMBOLS = re.compile(r"[\[\]{}<>\\^~|`*_]")


def speakable(text: str) -> str:
    """Turn chat-formatted text into something that sounds natural when read aloud."""
    text = re.sub(r"```.*?```", " (code shown on screen) ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s*\|.*\|\s*$", "", text, flags=re.M)  # tables are for the screen
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", text)
    text = _MACHINE_ID.sub("", text)
    text = _expand_for_speech(text)
    text = text.replace("=>", " to ").replace("->", " to ")
    text = _STRAY_SYMBOLS.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"\s+([,.;:!?])", r"\1", text).strip()  # no gap left before punctuation where an ID was removed


_MONEY = re.compile(
    r"£\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2})(?!\d)|,(\d{2})(?!\d|,\d))?"  # £12.50 and the £12,50 typo
    r"(?:(bn|k|m)\b|\s(thousand|million|billion)\b)?", re.I)
_MONEY_WORDS = {"k": "thousand", "m": "million", "bn": "billion"}

# Dates and times are matched in ONE pass (so a later rule never re-reads the inside of something an earlier rule
# deliberately left alone). Order of the alternatives matters:
#  - a date straight after a reference prefix ("PO 2026-10-01", "INV: 2026-10-01") is an identifier, not a date
#  - an ISO timestamp that carries a UTC offset ("...Z", "+01:00") is left exactly as written, because
#    humanize.human_datetime ignores the offset and would speak the wrong hour
#  - a bare 24-hour time is spoken as 12-hour, a range ("12:30-14:00") as "12:30pm to 2pm"; a time followed by an
#    offset is left alone, and so is anything that isn't a plausible clock ("3:2", "54-13:2017", "10.0.0.5:8080").
_HM = r"(?:[01]\d|2[0-3]):[0-5]\d"
_ISO_DATE_TIME = re.compile(
    rf"""(?<![\w:.+-])(?:
      (?P<ref>(?:(?i:po|inv|invoice|ref|reference|job|quote|quotation)|Q)[\s:#.]*\d{{4}}-\d{{2}}-\d{{2}}(?![\d-]))
    | (?<![\d-])(?P<t_date>\d{{4}}-\d{{2}}-\d{{2}})T(?P<t_hm>{_HM})(?::\d{{2}}(?:[.,]\d+)?)?
        (?P<t_zone>Z|[+-]\d{{2}}(?::?\d{{2}})?)?(?![\w:])
    | (?<![\d-])(?P<s_date>\d{{4}}-\d{{2}}-\d{{2}})\s(?P<s_hm>{_HM})(?P<s_sec>:\d{{2}}(?:[.,]\d+)?)?
        (?:(?P<s_zone>Z|\+\d{{2}}(?::?\d{{2}})?|(?(s_sec)-\d{{2}}(?::?\d{{2}})?|(?!)))
          |\s?[-–]\s?(?P<s_end>{_HM}))?(?![\w:])
    | (?<![\d-])(?P<date>\d{{4}}-\d{{2}}-\d{{2}})(?!\d|[TZ+-]|:\d|\.\d)
    | (?<!\d-)(?P<r_a>{_HM})\s?[-–]\s?(?P<r_b>{_HM})(?![\d:]|\s?[ap]m\b|Z\b)
    | (?<!\d-)(?P<clock>{_HM})(?![\d:]|\s?[ap]m\b|Z\b|\+\d{{2}}|-\d{{2}}:?\d{{2}})
    )""", re.X)
_URL = re.compile(r"https?://(?:www\.)?([^/\s?#)]*[^/\s?#).,;:!])(?:[^\s)]*[^\s).,;:!?])?")
_EMAIL = re.compile(r"\b([\w.+-]+)@([\w-]+(?:\.[\w-]+)+)\b")
_BS_PART = re.compile(r"\b(BS(?: EN)?(?: ISO)? \d+)-(\d+)\b")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿️]")


def _money(m: re.Match) -> str:
    whole, pence = m.group(1).replace(",", ""), m.group(2) or m.group(3)
    mult = (m.group(4) or m.group(5) or "").lower()
    if mult:  # "£73k" -> "73 thousand pounds"
        return f"{whole}{'.' + pence if pence else ''} {_MONEY_WORDS.get(mult, mult)} pounds"
    unit = "pound" if whole == "1" else "pounds"
    pence = (pence or "").ljust(2, "0")
    if int(pence or 0):  # "£12.50" -> "12 pounds 50"; "£12.00" -> "12 pounds"
        return f"{whole} {unit} {pence}"
    return f"{whole} {unit}"


def _when(m: re.Match) -> str:
    """One date/time match from _ISO_DATE_TIME -> how a person would say it (or the text unchanged)."""
    from ..humanize import _format_time, human_datetime

    g = m.groupdict()
    if g["ref"] or g["t_zone"] or g["s_zone"]:
        return m.group(0)  # a reference number, or a timestamp whose UTC offset humanize would drop
    if g["t_date"]:
        return human_datetime(f"{g['t_date']}T{g['t_hm']}")
    if g["s_date"]:
        said = human_datetime(f"{g['s_date']}T{g['s_hm']}")
        if g["s_end"]:
            said += " to " + _format_time(int(g["s_end"][:2]), int(g["s_end"][3:]))
        return said
    if g["date"]:
        return human_datetime(g["date"])
    if g["r_a"]:
        a, b = g["r_a"], g["r_b"]
        return f"{_format_time(int(a[:2]), int(a[3:]))} to {_format_time(int(b[:2]), int(b[3:]))}"
    return _format_time(int(g["clock"][:2]), int(g["clock"][3:]))


def _expand_for_speech(text: str) -> str:
    """Spell out the things a TTS engine reads badly: currency, percentages, ISO dates, 24-hour times, bare
    URLs and email addresses, British Standard part numbers ("BS 5839-1"), common abbreviations and emoji.
    Conservative on purpose - anything not clearly one of these is left exactly as written."""
    text = _MONEY.sub(_money, text)
    text = re.sub(r"\b(\d{1,3}(?:,\d{3})+)\b", lambda m: m.group(1).replace(",", ""), text)
    text = re.sub(r"(\d)\s?%", r"\1 percent", text)
    text = _ISO_DATE_TIME.sub(_when, text)
    text = _URL.sub(lambda m: m.group(1).replace(".", " dot "), text)
    text = _EMAIL.sub(lambda m: f"{m.group(1)} at {m.group(2).replace('.', ' dot ')}", text)
    text = _BS_PART.sub(r"\1 part \2", text)
    text = re.sub(r"\be\.g\.", "for example", text, flags=re.I)
    text = re.sub(r"\bi\.e\.", "that is", text, flags=re.I)
    text = re.sub(r"\betc\.", "et cetera", text, flags=re.I)
    text = re.sub(r"\bapprox\.", "approximately", text, flags=re.I)
    text = re.sub(r"\bvs\.?(?=\s)", "versus", text, flags=re.I)
    text = text.replace("&", " and ").replace("—", ", ").replace("→", " to ")
    return _EMOJI.sub("", text)


def audio_extension(mime: str) -> str:
    """File extension matching the recorded container - Whisper picks its decoder from the filename, so a Safari
    audio/mp4 recording sent as 'speech.webm' is rejected."""
    m = (mime or "").lower()
    if "mp4" in m or "m4a" in m or "aac" in m:
        return "mp4"
    if "ogg" in m:
        return "ogg"
    if "mpeg" in m or "mp3" in m:
        return "mp3"
    if "wav" in m:
        return "wav"
    return "webm"


class Voice:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.s = settings
        self.http = http
        self._piper_models: dict[str, Any] = {}  # model path -> loaded PiperVoice, expensive to (re)load

    # ------------------------------------------------------------------ status
    def client_config(self) -> dict[str, Any]:
        voice = (self.s.elevenlabs_voice if self.s.effective_tts == "elevenlabs" else
                self.s.piper_voice if self.s.effective_tts == "piper" else self.s.azure_tts_voice)
        return {
            "tts": self.s.effective_tts,
            "stt": self.s.effective_stt,
            "stt_chain": stt_chain(self.s),  # fallback order the browser walks if an engine fails (stt_chain.py)
            "stt_problem": stt_problem(self.s),  # "" or why the chosen engine can't be used (shown in the top bar)
            "wake_word": self.s.wake_word,
            "language": self.s.stt_language,
            "voice": voice,
            "ack_fillers": bool(self.s.voice_ack_fillers),
            "silence_ms": int(self.s.voice_silence_ms),
        }

    # ------------------------------------------------------------------ TTS
    async def tts_stream(self, text: str, voice_id: str | None = None) -> tuple[AsyncIterator[bytes], str]:
        text = speakable(text)
        if not text:
            raise VoiceError("nothing to say")
        provider = self.s.effective_tts
        if provider == "elevenlabs":
            return await self._elevenlabs(text, voice_id or self.s.elevenlabs_voice_id), "audio/mpeg"
        if provider == "azure":
            return await self._azure_tts(text), "audio/mpeg"
        if provider == "piper":
            return await self._piper(text, voice_id), "audio/wav"
        raise VoiceError("No server TTS configured - use the browser voice")

    async def _open_stream(self, request: httpx.Request) -> AsyncIterator[bytes]:
        """Opens the provider's audio stream. One retry on a transient failure (timeout, network error, upstream
        5xx, 429) before the caller falls back to the browser voice; anything else (bad key, bad request) fails
        at once. The request body is in memory, so the same request can safely be sent twice."""
        for attempt in (1, 2):
            try:
                resp = await self.http.send(request, stream=True)
            except httpx.TransportError as e:  # timeouts and connection failures
                if attempt == 2:
                    raise
                log.warning("TTS network error (attempt 1 of 2), retrying: %s", type(e).__name__)
                await asyncio.sleep(TTS_RETRY_DELAY_S)
                continue
            if resp.status_code < 400:
                break
            body = (await resp.aread())[:300]
            await resp.aclose()
            if attempt == 1 and resp.status_code in TTS_RETRY_STATUSES:
                log.warning("TTS provider returned %s (attempt 1 of 2), retrying", resp.status_code)
                await asyncio.sleep(TTS_RETRY_DELAY_S)
                continue
            raise VoiceError(f"TTS provider returned {resp.status_code}: {body!r}")

        async def gen() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        return gen()

    async def _elevenlabs(self, text: str, voice_id: str) -> AsyncIterator[bytes]:
        req = self.http.build_request(
            "POST", f"{ELEVEN_API}/text-to-speech/{voice_id}/stream",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": self.s.elevenlabs_api_key, "Accept": "audio/mpeg"},
            json={
                "text": text,
                "model_id": self.s.elevenlabs_model,
                "voice_settings": {
                    "stability": self.s.elevenlabs_stability,
                    "similarity_boost": self.s.elevenlabs_similarity,
                    "style": self.s.elevenlabs_style,
                    "use_speaker_boost": True,
                    "speed": self.s.elevenlabs_speed,
                },
            },
            timeout=60,
        )
        return await self._open_stream(req)

    async def azure_sample(self, voice: str) -> tuple[AsyncIterator[bytes], str]:
        """A fixed sample line in the given Azure voice, for the Settings page's 'Play sample' button. The caller
        has already checked `voice` is one of the listed voices; the text is never taken from the request."""
        if not self.s.azure_speech_key:
            raise VoiceError("Save the Azure Speech key first, then play the sample.")
        try:
            return await self._azure_tts(speakable(AZURE_SAMPLE_TEXT), voice), "audio/mpeg"
        except VoiceError as e:  # the provider's error body can echo the key back - never pass that on
            raise VoiceError(_redact(str(e), (self.s.azure_speech_key,))) from None

    async def _azure_tts(self, text: str, voice: str | None = None) -> AsyncIterator[bytes]:
        ssml = build_ssml(text, voice or self.s.azure_tts_voice, self.s.azure_tts_style)
        req = self.http.build_request(
            "POST", f"https://{self.s.azure_speech_region}.tts.speech.microsoft.com/cognitiveservices/v1",
            headers={"Ocp-Apim-Subscription-Key": self.s.azure_speech_key,
                     "Content-Type": "application/ssml+xml",
                     "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3",
                     "User-Agent": "salts-jarvis"},
            content=ssml.encode(), timeout=60,
        )
        return await self._open_stream(req)

    async def _piper(self, text: str, voice: str | None = None) -> AsyncIterator[bytes]:
        voice = voice or self.s.piper_voice
        quality = PIPER_VOICES.get(voice, "medium")
        model_path = await self._ensure_piper_voice(voice, quality)
        wav_bytes = await asyncio.to_thread(self._piper_synthesize, model_path, text)

        async def gen() -> AsyncIterator[bytes]:
            yield wav_bytes

        return gen()

    async def _ensure_piper_voice(self, voice: str, quality: str) -> Path:
        """Downloads a Piper voice model the first time it's used and caches it under data_dir, which
        survives restarts/redeploys - so this only ever costs real time once per voice, not once per reply."""
        cache_dir = self.s.data_dir / "piper-voices"
        cache_dir.mkdir(parents=True, exist_ok=True)
        stem = f"en_GB-{voice}-{quality}"
        model_path, config_path = cache_dir / f"{stem}.onnx", cache_dir / f"{stem}.onnx.json"
        if not model_path.exists() or not config_path.exists():
            base = f"{PIPER_VOICES_BASE}/{voice}/{quality}/{stem}"
            for suffix, path in ((".onnx", model_path), (".onnx.json", config_path)):
                r = await self.http.get(f"{base}{suffix}", timeout=120, follow_redirects=True)
                if r.status_code >= 400:
                    raise VoiceError(f"Couldn't download the Piper voice '{voice}' ({r.status_code}) - "
                                     "check it's a real voice name from huggingface.co/rhasspy/piper-voices.")
                path.write_bytes(r.content)
        return model_path

    def _piper_synthesize(self, model_path: Path, text: str) -> bytes:
        """Runs on a worker thread (asyncio.to_thread) - Piper's inference is synchronous CPU work and would
        otherwise block the event loop for every other request while a reply is being spoken."""
        from piper import PiperVoice

        key = str(model_path)
        voice = self._piper_models.get(key)
        if voice is None:
            voice = self._piper_models[key] = PiperVoice.load(key)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
        return buf.getvalue()

    async def list_voices(self) -> list[dict[str, Any]]:
        """ElevenLabs voices on the account, British accents first."""
        if not self.s.elevenlabs_api_key:
            return []
        r = await self.http.get(f"{ELEVEN_API}/voices", headers={"xi-api-key": self.s.elevenlabs_api_key}, timeout=30)
        r.raise_for_status()
        voices = []
        for v in r.json().get("voices", []):
            labels = v.get("labels") or {}
            voices.append({"voice_id": v["voice_id"], "name": v.get("name"), "accent": labels.get("accent", ""),
                           "gender": labels.get("gender", ""), "description": labels.get("description", "")})
        voices.sort(key=lambda v: ("british" not in (v["accent"] or "").lower(), v["name"] or ""))
        return voices

    # ------------------------------------------------------------------ STT (push-to-talk)
    def _secrets(self) -> tuple[str, ...]:
        return (self.s.deepgram_api_key, self.s.openai_api_key)

    async def transcribe(self, audio: bytes, mime: str, provider: str | None = None, *, retry: bool = True,
                         timeout_s: float | None = None) -> str:
        """Push-to-talk transcription. One retry on a transient failure (timeout, network error, upstream 5xx,
        non-quota 429); anything else - bad key, billing, bad audio - fails at once with a clear STTError.

        provider: a specific engine (used by the browser-driven fallback); default is the configured one.
        retry=False / timeout_s: a single bounded attempt - the browser does its own retry and fallback."""
        provider = provider or self.s.effective_stt
        log.info("STT request: provider=%s bytes=%d mime=%s", provider, len(audio), mime)
        if provider not in ("deepgram", "whisper"):
            raise VoiceError("No server speech-to-text configured - use the browser microphone")
        if not engine_configured(self.s, provider):
            key_env = "OPENAI_API_KEY" if provider == "whisper" else "DEEPGRAM_API_KEY"
            log.warning("STT engine %s selected but no API key is configured (%s is empty)", provider, key_env)
            raise STTError(f"{ENGINE_LABELS[provider]} has no API key - set {key_env}.", provider, None)
        if len(audio) > STT_MAX_BYTES:
            raise STTError(f"The recording is too large ({len(audio) // (1024 * 1024)} MB; the limit is "
                           f"{STT_MAX_BYTES // (1024 * 1024)} MB) - record a shorter message.", provider, 413)
        for attempt in (1, 2):
            try:
                return await self._transcribe_once(provider, audio, mime, timeout_s or STT_TIMEOUT_S)
            except STTError as e:
                if not retry or not e.transient or attempt == 2:
                    raise
                log.warning("STT transient failure (attempt 1 of 2), retrying: %s", e)
                await asyncio.sleep(STT_RETRY_DELAY_S)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _transcribe_once(self, provider: str, audio: bytes, mime: str,
                               timeout_s: float = STT_TIMEOUT_S) -> str:
        label = ENGINE_LABELS[provider]
        if provider == "deepgram":
            request = dict(
                url=DEEPGRAM_API,
                params={"model": self.s.deepgram_model, "language": self.s.stt_language, "smart_format": "true"},
                headers={"Authorization": f"Token {self.s.deepgram_api_key}", "Content-Type": mime},
                content=audio)
        else:
            ext = audio_extension(mime)
            base_mime = mime.split(";")[0].strip() or "audio/webm"
            request = dict(
                url=WHISPER_API,
                headers={"Authorization": f"Bearer {self.s.openai_api_key}"},
                data={"model": self.s.whisper_model, "language": self.s.stt_language.split("-")[0],
                      "prompt": ", ".join(VOCAB)},
                files={"file": (f"speech.{ext}", audio, base_mime)})
        t0 = time.perf_counter()
        try:
            r = await self.http.post(timeout=httpx.Timeout(timeout_s, connect=min(STT_CONNECT_TIMEOUT_S, timeout_s)),
                                     **request)
        except httpx.TimeoutException as e:
            log.warning("STT timeout: provider=%s after %.1fs (%s)", provider, time.perf_counter() - t0,
                        type(e).__name__)
            raise STTError(f"{label} did not answer within {timeout_s:.0f} seconds.", provider, None,
                           transient=True) from e
        except httpx.HTTPError as e:
            log.warning("STT network error: provider=%s %s: %s", provider, type(e).__name__,
                        _redact(str(e), self._secrets()))
            raise STTError(f"Couldn't reach {label} ({type(e).__name__}).", provider, None, transient=True) from e
        ms = int((time.perf_counter() - t0) * 1000)
        log.info("STT response: provider=%s status=%s in %d ms", provider, r.status_code, ms)
        if r.status_code >= 400:
            raise self._stt_error(provider, label, r)
        try:
            data = r.json()
            if provider == "deepgram":
                return data["results"]["channels"][0]["alternatives"][0]["transcript"]
            return data.get("text", "")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            log.warning("STT unexpected response shape: provider=%s body=%r", provider,
                        _redact(r.text[:300], self._secrets()))
            raise STTError(f"{label} returned a response Jarvis couldn't read.", provider, r.status_code) from e

    def _stt_error(self, provider: str, label: str, r: httpx.Response) -> STTError:
        """Logs the real upstream error and turns it into a message that says what to check. Never includes the
        API key; for 401/403 the upstream text is withheld altogether because providers echo part of the key."""
        status = r.status_code
        upstream = _upstream_message(r, self._secrets())
        log.warning("STT upstream error: provider=%s status=%s model=%s body=%s", provider, status,
                    self.s.whisper_model if provider == "whisper" else self.s.deepgram_model,
                    _redact(r.text[:500], self._secrets()).replace("\n", " "))
        key_env = "OPENAI_API_KEY" if provider == "whisper" else "DEEPGRAM_API_KEY"
        model_env = "WHISPER_MODEL" if provider == "whisper" else "DEEPGRAM_MODEL"
        quota = status == 402 or (status == 429 and any(
            w in (r.text or "").lower() for w in ("insufficient_quota", "quota", "billing")))
        if status in (401, 403):
            hint, upstream = f"the API key was rejected - check {key_env} (expired, revoked or wrong project).", ""
        elif quota:
            hint = f"quota or billing problem - check the {label} account's credit / billing limits."
        elif status == 429:
            hint = "rate limited - try again in a moment."
        elif status == 404:
            hint = f"endpoint or model not found - check {model_env}."
        elif status == 413:
            hint = "the recording is too large."
        elif status in (400, 415, 422):
            hint = "the audio was rejected (unsupported format or corrupt recording)."
        elif status >= 500:
            hint = "the service had a problem on its side."
        else:
            hint = "the request failed."
        message = f"{label} returned {status}: {hint}" + (f" ({upstream})" if upstream else "")
        transient = status >= 500 or (status == 429 and not quota)
        return STTError(message, provider, status, transient=transient)

    # ------------------------------------------------------------------ STT (live, Deepgram)
    def deepgram_live_url(self) -> str:
        params: list[tuple[str, str]] = [
            ("model", self.s.deepgram_model), ("language", self.s.stt_language), ("smart_format", "true"),
            ("interim_results", "true"), ("endpointing", "300"), ("utterance_end_ms", "1000"),
            ("vad_events", "true"),
        ]
        if self.s.deepgram_model.startswith("nova-3"):
            params += [("keyterm", term) for term in VOCAB]
        return f"{DEEPGRAM_WS}?{urlencode(params)}"

    async def relay_deepgram(self, client_ws) -> None:
        """Bridge a browser WebSocket (binary webm/opus chunks) to Deepgram live transcription."""
        import websockets

        async with websockets.connect(self.deepgram_live_url(), max_size=None,
                                      additional_headers={"Authorization": f"Token {self.s.deepgram_api_key}"}) as dg:

            async def upstream() -> None:
                while True:
                    msg = await client_ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("bytes"):
                        await dg.send(msg["bytes"])
                    elif msg.get("text"):
                        try:
                            control = json.loads(msg["text"])
                        except ValueError:
                            continue  # a garbled control message must not tear down the live transcription
                        if isinstance(control, dict) and control.get("type") in ("KeepAlive", "Finalize", "CloseStream"):
                            await dg.send(json.dumps({"type": control["type"]}))
                with contextlib.suppress(Exception):
                    await dg.send(json.dumps({"type": "CloseStream"}))

            async def downstream() -> None:
                async for raw in dg:
                    try:
                        data = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    kind = data.get("type")
                    if kind == "Results":
                        alt = (data.get("channel", {}).get("alternatives") or [{}])[0]
                        await client_ws.send_json({"type": "transcript", "text": alt.get("transcript", ""),
                                                   "is_final": bool(data.get("is_final")),
                                                   "speech_final": bool(data.get("speech_final"))})
                    elif kind == "UtteranceEnd":
                        await client_ws.send_json({"type": "utterance_end"})
                    elif kind == "SpeechStarted":
                        await client_ws.send_json({"type": "speech_started"})

            async def keepalive() -> None:
                while True:
                    await asyncio.sleep(8)
                    await dg.send(json.dumps({"type": "KeepAlive"}))

            tasks = [asyncio.create_task(c()) for c in (upstream, downstream, keepalive)]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


def silent_wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    """A tiny valid mono 16-bit WAV of silence, for probing the STT provider without a real recording."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class SpeechToTextCheck:
    """Routine-test probe ('Integration: Speech-to-text'): sends a short silent clip down the same path a
    push-to-talk recording takes (same endpoint, key, model, retry and timeout). Any provider error - including a
    500 - raises, which the routine tester records as a failure; an empty transcript is a pass."""

    def __init__(self, voice: Voice):
        self.voice = voice

    async def check(self) -> str:
        provider = self.voice.s.effective_stt
        problem = stt_problem(self.voice.s)
        if problem:  # chosen but not usable: fails (so it stays visible) and says what voice input does instead
            key = KEY_NAMES.get(self.voice.s.effective_stt, "the key")
            raise VoiceError(f"{problem} ({key}). Voice input is using the browser's speech recognition instead - add the "
                             "key under Connections > Voice, or choose the browser engine there.")
        t0 = time.perf_counter()
        await self.voice.transcribe(silent_wav(), "audio/wav")
        return f"{provider} accepted a test clip in {int((time.perf_counter() - t0) * 1000)} ms"
