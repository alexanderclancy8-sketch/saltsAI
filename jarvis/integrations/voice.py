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


TTS_RETRY_STATUSES = {429, 500, 502, 503, 504}
TTS_RETRY_DELAY_S = 0.4
MAX_STT_BYTES = 25 * 1024 * 1024  # OpenAI's transcription upload limit; nothing legitimate is bigger


class VoiceError(RuntimeError):
    pass


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
    text = _expand_for_speech(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


_MONEY = re.compile(r"£\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?:(bn|k|m)\b|\s(thousand|million|billion)\b)?", re.I)
_MONEY_WORDS = {"k": "thousand", "m": "million", "bn": "billion"}
# Zone-less ISO dates/times only: humanize.human_datetime ignores any UTC offset, so a "...Z" or "+01:00" value
# is left alone rather than spoken as the wrong hour.
_ISO_DATE = re.compile(r"(?<![\d-])\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?(?![\dTZ+:-]|\.\d)")
_CLOCK_24H = re.compile(r"(?<![\d:.])([01]\d|2[0-3]):([0-5]\d)(?![\d:]|\s?[ap]m\b)", re.I)
_URL = re.compile(r"https?://(?:www\.)?([^/\s?#)]*[^/\s?#).,;:!])(?:[^\s)]*[^\s).,;:!?])?")
_EMAIL = re.compile(r"\b([\w.+-]+)@([\w-]+(?:\.[\w-]+)+)\b")
_BS_PART = re.compile(r"\b(BS(?: EN)?(?: ISO)? \d+)-(\d+)\b")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿️]")


def _money(m: re.Match) -> str:
    whole, pence = m.group(1).replace(",", ""), m.group(2)
    mult = (m.group(3) or m.group(4) or "").lower()
    if mult:  # "£73k" -> "73 thousand pounds"
        return f"{whole}{'.' + pence if pence else ''} {_MONEY_WORDS.get(mult, mult)} pounds"
    unit = "pound" if whole == "1" else "pounds"
    pence = (pence or "").ljust(2, "0")
    if int(pence or 0):  # "£12.50" -> "12 pounds 50"; "£12.00" -> "12 pounds"
        return f"{whole} {unit} {pence}"
    return f"{whole} {unit}"


def _clock(m: re.Match) -> str:
    from ..humanize import _format_time

    return _format_time(int(m.group(1)), int(m.group(2)))


def _expand_for_speech(text: str) -> str:
    """Spell out the things a TTS engine reads badly: currency, percentages, ISO dates, 24-hour times, bare
    URLs and email addresses, British Standard part numbers ("BS 5839-1"), common abbreviations and emoji.
    Conservative on purpose - anything not clearly one of these is left exactly as written."""
    from ..humanize import human_datetime

    text = _MONEY.sub(_money, text)
    text = re.sub(r"\b(\d{1,3}(?:,\d{3})+)\b", lambda m: m.group(1).replace(",", ""), text)
    text = re.sub(r"(\d)\s?%", r"\1 percent", text)
    text = _ISO_DATE.sub(lambda m: human_datetime(m.group(0)), text)
    text = _CLOCK_24H.sub(_clock, text)
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
        # One quick retry for a transient failure (rate limit, 5xx, dropped connection): a single hiccup used to
        # mean the whole reply fell back to the robotic browser voice. Nothing is read before the status is
        # known, so resending the already-built request is safe.
        for attempt in (1, 2):
            try:
                resp = await self.http.send(request, stream=True)
            except httpx.TransportError as e:
                if attempt == 2:
                    raise VoiceError(f"TTS provider unreachable: {type(e).__name__}") from e
                await asyncio.sleep(TTS_RETRY_DELAY_S)
                continue
            if resp.status_code in TTS_RETRY_STATUSES and attempt == 1:
                await resp.aclose()
                await asyncio.sleep(TTS_RETRY_DELAY_S)
                continue
            break
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
        """Push-to-talk transcription. Every provider failure (network, HTTP error, odd response shape) is
        raised as a VoiceError so the caller can tell the browser to fall back to its own speech engine, rather
        than surfacing a bare 500."""
        if not audio:
            return ""
        if len(audio) > MAX_STT_BYTES:
            raise VoiceError("That recording is too long to transcribe - try a shorter one.")
        try:
            return await self._transcribe(audio, mime)
        except VoiceError:
            raise
        except httpx.HTTPStatusError as e:
            raise VoiceError(f"Speech-to-text provider returned {e.response.status_code}") from e
        except httpx.HTTPError as e:
            raise VoiceError(f"Speech-to-text provider unreachable: {type(e).__name__}") from e
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise VoiceError(f"Unexpected speech-to-text response: {type(e).__name__}") from e

    async def _transcribe(self, audio: bytes, mime: str) -> str:
        provider = self.s.effective_stt
        if provider == "deepgram":
            r = await self.http.post(
                DEEPGRAM_API,
                params={"model": self.s.deepgram_model, "language": self.s.stt_language, "smart_format": "true"},
                headers={"Authorization": f"Token {self.s.deepgram_api_key}", "Content-Type": mime},
                content=audio, timeout=60)
            r.raise_for_status()
            return r.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
        if provider == "whisper":
            ext = "webm" if "webm" in mime else "ogg" if "ogg" in mime else "mp4" if "mp4" in mime else "wav"
            r = await self.http.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.s.openai_api_key}"},
                data={"model": self.s.whisper_model, "language": self.s.stt_language.split("-")[0],
                      "prompt": ", ".join(VOCAB)},
                files={"file": (f"speech.{ext}", audio, mime)}, timeout=60)
            r.raise_for_status()
            return r.json().get("text", "")
        raise VoiceError("No server speech-to-text configured - use the browser microphone")

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
