"""The "Test" buttons on the Settings page: a small real request to each connected service."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..integrations.voice import silent_wav
from ..redact import redact_text

log = logging.getLogger(__name__)
TIMEOUT = 45


def _why(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        plain = {401: "the key or password was refused", 403: "access was refused (check permissions)",
                 404: "the address wasn't found"}.get(code, "the service returned an error")
        return f"{plain} (HTTP {code})"
    if isinstance(e, httpx.ConnectError):
        return "couldn't connect - check the address"
    if isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "the service didn't answer in time"
    text = redact_text(str(e) or type(e).__name__)
    return text[:300]


async def run(j, section: str) -> tuple[bool, str]:
    test = TESTS.get(section)
    if test is None:
        return False, "There's nothing to test here."
    try:
        return await asyncio.wait_for(test(j), timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001 - every failure becomes a readable message
        log.info("Connection test %s failed: %s", section, e)
        return False, _why(e)


async def _claude(j) -> tuple[bool, str]:
    s = j.settings
    if not (s.claude_code_oauth_token or s.anthropic_api_key):
        return False, "Add your Claude Max token (or an API key) first."
    from ..brain import llm

    reply = await llm.write(j.client, s, system="You are a connection test. Reply with exactly: OK",
                            prompt="Connection test", effort="low", max_tokens=50)
    how = "your Claude Max plan" if s.effective_llm_backend == "max" else "the Claude API"
    if "OK" not in reply.upper():
        return False, f"Claude answered through {how}, but not as expected: {reply[:120]}"
    return True, f"Claude is answering through {how} ({s.jarvis_model})."


async def _microsoft365(j) -> tuple[bool, str]:
    if getattr(j.mail, "demo", True):
        return False, "Fill in the tenant ID, application ID, client secret and mailbox first."
    await j.mail.list_messages(top=1)
    return True, f"Reading {j.settings.ms_mailbox} works."


async def _serviceinbox(j) -> tuple[bool, str]:
    return await j.service_inbox.test()


async def _teams(j) -> tuple[bool, str]:
    if not j.teams.enabled:
        return False, "Add the channel webhook URL first."
    await j.teams.post("Jarvis connection test", "If you can read this, Teams updates are working.")
    return True, "Sent a test message to the Teams channel - check it arrived."


async def _fsm(j) -> tuple[bool, str]:
    if j.fsm.demo:
        return False, "Add the FSM web address first."
    detail = await j.fsm.check()
    jobs = await j.fsm.jobs()
    return True, f"{detail}. {len(jobs)} jobs found."


async def _sage(j) -> tuple[bool, str]:
    from ..integrations.finance import SageFinance

    if not isinstance(j.finance, SageFinance):
        return False, "Add the Sage client ID and secret first."
    if not j.finance.connected:
        return False, "Saved. Now click Connect Sage and sign in to Sage."
    return True, await j.finance.check()


async def _ram(j) -> tuple[bool, str]:
    from ..integrations.ramtracking import RamError, missing_credentials

    if j.ram.demo:
        missing = missing_credentials(j.settings)
        return False, ("RAM Tracking still needs: " + ", ".join(missing) + ". All four are required: the Client ID and "
                       "Client secret from RAM's API Keys page, and the dedicated API username and password."
                       if missing else "RAM Tracking is not connected.")
    note = (" " + j.ram.address_note) if getattr(j.ram, "address_note", "") else ""
    try:
        vehicles = await j.ram.vehicles()
    except RamError as e:
        if e.rate_limited:  # RAM answered, just not this often: signed in means the details are right
            return j.ram.signed_in, (("Signed in to RAM. " if j.ram.signed_in else "") + str(e) + note)
        return False, str(e) + note
    return True, f"RAM Tracking answered: {len(vehicles)} vehicles.{note}"


async def _companieshouse(j) -> tuple[bool, str]:
    return await j.company_check.test()


async def _github(j) -> tuple[bool, str]:
    if not j.github:
        return False, "Add the GitHub token and FSM repository first."
    return True, await j.github.check()


async def _selfimprove(j) -> tuple[bool, str]:
    if not j.self_improve.enabled:
        return False, "Add Jarvis's own repository and a GitHub token for it first."
    return True, await j.self_github.check()


async def _storage(j) -> tuple[bool, str]:
    if not j.blob.enabled:
        return False, "Add the storage connection string first."

    def check() -> bool:
        return j.blob._client.get_container_client(j.settings.azure_storage_container).exists()

    exists = await asyncio.to_thread(check)
    return True, ("Storage reachable; the archive container is there." if exists
                  else "Storage reachable. The archive container will be created with the first report.")


async def _teamsbot(j) -> tuple[bool, str]:
    from ..integrations.teamsbot import TeamsBotError

    if not j.teamsbot.configured:
        return False, "Add the bot app ID, secret and tenant ID first (or run deploy.sh teamsbot)."
    try:
        return True, await j.teamsbot.check()
    except TeamsBotError as e:
        return False, redact_text(e)


async def _voice(j) -> tuple[bool, str]:
    s, parts, ok = j.settings, [], True
    tts = s.effective_tts
    if tts == "browser":
        parts.append("Speaking: the browser's built-in voice (add ElevenLabs or Azure for a natural one)")
    else:
        stream, _ = await j.voice.tts_stream("Connection test.")
        size = 0
        async for chunk in stream:
            size += len(chunk)
        name = s.elevenlabs_voice if tts == "elevenlabs" else s.azure_tts_voice
        parts.append(f"Speaking: {'ElevenLabs' if tts == 'elevenlabs' else 'Azure'} ({name}) works")
        ok = size > 0
    stt = s.effective_stt
    if stt == "azure":
        # The real push-to-talk path with half a second of silence: checks the key, the region and the endpoint at once.
        await j.voice.transcribe(silent_wav(), "audio/wav", "azure", retry=False)
        parts.append(f"Listening: Azure Speech works (key and region {s.azure_speech_region} accepted)")
    elif stt == "deepgram":
        r = await j.http.get("https://api.deepgram.com/v1/projects",
                             headers={"Authorization": f"Token {s.deepgram_api_key}"}, timeout=20)
        r.raise_for_status()
        parts.append("Listening: Deepgram key accepted")
    elif stt == "whisper":
        r = await j.http.get("https://api.openai.com/v1/models",
                             headers={"Authorization": f"Bearer {s.openai_api_key}"}, timeout=20)
        r.raise_for_status()
        parts.append("Listening: OpenAI key accepted")
    else:
        parts.append("Listening: the browser's speech recognition")
    return ok, ". ".join(parts) + "."


async def _marketing(j) -> tuple[bool, str]:
    p = j.presence
    checks: list[tuple[str, Any]] = []
    configured = p.configured()
    for name, key, fn in (("Facebook", "facebook", p.facebook), ("Instagram", "instagram", p.instagram),
                          ("LinkedIn", "linkedin", p.linkedin), ("TikTok", "tiktok", p.tiktok),
                          ("Google reviews", "google_reviews", p.google_reviews)):
        if configured.get(key):
            checks.append((name, fn()))
    if not checks:
        return False, "Nothing connected yet - add a Google Places key, Facebook token or LinkedIn token."
    results = await asyncio.gather(*(c for _, c in checks), return_exceptions=True)
    lines, ok = [], True
    for (name, _), result in zip(checks, results):
        if isinstance(result, Exception):
            ok = False
            lines.append(f"{name}: {_why(result)}")
        else:
            figures = ", ".join(f"{k.replace('_', ' ')} {v:,.0f}" if isinstance(v, (int, float)) else f"{k} {v}"
                                for k, v in (result or {}).items())
            lines.append(f"{name}: working" + (f" ({figures})" if figures else ""))
    return ok, "; ".join(lines) + "."


TESTS = {
    "claude": _claude, "microsoft365": _microsoft365, "serviceinbox": _serviceinbox, "companieshouse": _companieshouse, "teams": _teams, "teamsbot": _teamsbot, "fsm": _fsm,
    "sage": _sage, "ram": _ram, "github": _github, "selfimprove": _selfimprove, "storage": _storage,
    "voice": _voice, "marketing": _marketing,
}
