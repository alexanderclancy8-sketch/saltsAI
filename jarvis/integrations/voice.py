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
from xml.sax.saxutils import escape

import httpx

from ..config import Settings

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


WHISPER_API = "https://api.openai.com/v1/audio/transcriptions"
# Push-to-talk STT limits. A short clip normally comes back in 1-3 s, so 20 s is generous without leaving the
# owner staring at "Transcribing…" for a minute; OpenAI rejects uploads over 25 MB.
STT_TIMEOUT_S = 20.0
STT_CONNECT_TIMEOUT_S = 5.0
STT_RETRY_DELAY_S = 0.5
STT_MAX_BYTES = 25 * 1024 * 1024


class VoiceError(RuntimeError):
    pass


class STTError(VoiceError):
    """A speech-to-text provider failure with a message that is safe and useful to show the owner (no secrets)."""

    def __init__(self, message: str, provider: str = "", status: int | None = None, *, transient: bool = False):
        super().__init__(message)
        self.provider, self.status, self.transient = provider, status, transient


_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-*.]{4,}|Token\s+\S+|Bearer\s+\S+)", re.I)


def _redact(text: str) -> str:
    """Strip anything key-shaped from upstream text before it is logged or shown."""
    return _SECRET_RE.sub("[redacted]", text or "")


def _upstream_message(r: httpx.Response) -> str:
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
    return _redact(" ".join(str(msg).split()))[:200]


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
    text = text.replace("&", " and ").replace(" e.g. ", " for example ").replace(" i.e. ", " that is ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


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
        resp = await self.http.send(request, stream=True)
        if resp.status_code >= 400:
            body = (await resp.aread())[:300]
            await resp.aclose()
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

    async def _azure_tts(self, text: str) -> AsyncIterator[bytes]:
        def attr(value: str) -> str:
            return escape(value, {"'": "&apos;"})

        spoken = f"<prosody rate='+4%'>{escape(text)}</prosody>"
        if self.s.azure_tts_style:
            spoken = f"<mstts:express-as style='{attr(self.s.azure_tts_style)}'>{spoken}</mstts:express-as>"
        ssml = (
            "<speak version='1.0' xml:lang='en-GB' xmlns='http://www.w3.org/2001/10/synthesis' "
            "xmlns:mstts='https://www.w3.org/2001/mstts'>"
            f"<voice name='{attr(self.s.azure_tts_voice)}'>{spoken}</voice></speak>"
        )
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
    async def transcribe(self, audio: bytes, mime: str) -> str:
        """Push-to-talk transcription. One retry on a transient failure (timeout, network error, upstream 5xx,
        non-quota 429); anything else - bad key, billing, bad audio - fails at once with a clear STTError."""
        provider = self.s.effective_stt
        log.info("STT request: provider=%s bytes=%d mime=%s", provider, len(audio), mime)
        if provider not in ("deepgram", "whisper"):
            raise VoiceError("No server speech-to-text configured - use the browser microphone")
        if len(audio) > STT_MAX_BYTES:
            raise STTError(f"The recording is too large ({len(audio) // (1024 * 1024)} MB; the limit is "
                           f"{STT_MAX_BYTES // (1024 * 1024)} MB) - record a shorter message.", provider, 413)
        for attempt in (1, 2):
            try:
                return await self._transcribe_once(provider, audio, mime)
            except STTError as e:
                if not e.transient or attempt == 2:
                    raise
                log.warning("STT transient failure (attempt 1 of 2), retrying: %s", e)
                await asyncio.sleep(STT_RETRY_DELAY_S)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _transcribe_once(self, provider: str, audio: bytes, mime: str) -> str:
        label = "Deepgram" if provider == "deepgram" else "OpenAI Whisper"
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
            r = await self.http.post(timeout=httpx.Timeout(STT_TIMEOUT_S, connect=STT_CONNECT_TIMEOUT_S), **request)
        except httpx.TimeoutException as e:
            log.warning("STT timeout: provider=%s after %.1fs (%s)", provider, time.perf_counter() - t0,
                        type(e).__name__)
            raise STTError(f"{label} did not answer within {STT_TIMEOUT_S:.0f} seconds.", provider, None,
                           transient=True) from e
        except httpx.HTTPError as e:
            log.warning("STT network error: provider=%s %s: %s", provider, type(e).__name__, e)
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
            log.warning("STT unexpected response shape: provider=%s body=%r", provider, _redact(r.text[:300]))
            raise STTError(f"{label} returned a response Jarvis couldn't read.", provider, r.status_code) from e

    def _stt_error(self, provider: str, label: str, r: httpx.Response) -> STTError:
        """Logs the real upstream error and turns it into a message that says what to check. Never includes the
        API key; for 401/403 the upstream text is withheld altogether because providers echo part of the key."""
        status = r.status_code
        upstream = _upstream_message(r)
        log.warning("STT upstream error: provider=%s status=%s model=%s body=%s", provider, status,
                    self.s.whisper_model if provider == "whisper" else self.s.deepgram_model,
                    _redact(r.text[:500]).replace("\n", " "))
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
                        control = json.loads(msg["text"])
                        if control.get("type") in ("KeepAlive", "Finalize", "CloseStream"):
                            await dg.send(json.dumps({"type": control["type"]}))
                with contextlib.suppress(Exception):
                    await dg.send(json.dumps({"type": "CloseStream"}))

            async def downstream() -> None:
                async for raw in dg:
                    data = json.loads(raw)
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
        t0 = time.perf_counter()
        await self.voice.transcribe(silent_wav(), "audio/wav")
        return f"{provider} accepted a test clip in {int((time.perf_counter() - t0) * 1000)} ms"
