"""Voice I/O.

Speech output (TTS): ElevenLabs (default British "Daniel" voice) or Azure Neural TTS.
Speech input (STT): Deepgram Nova-3 live streaming (proxied so the API key never
reaches the browser), Deepgram pre-recorded, or OpenAI Whisper for push-to-talk.
If nothing is configured the browser's built-in speech engines are used.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator
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
    text = text.replace("&", " and ").replace(" e.g. ", " for example ").replace(" i.e. ", " that is ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class Voice:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.s = settings
        self.http = http

    # ------------------------------------------------------------------ status
    def client_config(self) -> dict[str, Any]:
        return {
            "tts": self.s.effective_tts,
            "stt": self.s.effective_stt,
            "wake_word": self.s.wake_word,
            "language": self.s.stt_language,
            "voice": self.s.elevenlabs_voice if self.s.effective_tts == "elevenlabs" else self.s.azure_tts_voice,
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
        ssml = (
            "<speak version='1.0' xml:lang='en-GB' xmlns='http://www.w3.org/2001/10/synthesis'>"
            f"<voice name='{self.s.azure_tts_voice}'><prosody rate='+4%'>{escape(text)}</prosody></voice></speak>"
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
