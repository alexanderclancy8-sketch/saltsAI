"""Teams chat: a real conversational bot (Microsoft Bot Framework), so the owner and business partner can
message Jarvis from their phone in Teams, not just receive one-way updates.

This is a different mechanism from TEAMS_WEBHOOK_URL in microsoft365.py (an outgoing "Workflows" webhook that
only posts to a channel). A Bot Framework bot has its own Entra app registration and an Azure Bot resource with
the Teams channel turned on (see `deploy.sh teamsbot`); Teams calls back into Jarvis's own /api/teams/messages
for every message, and Jarvis replies through the Bot Framework Connector API.

Security: every incoming request is a JWT signed by Microsoft, verified here against Bot Framework's published
keys before anything in the message is trusted (issuer, audience = our own app id, expiry, signature) - the
usual protection against someone simply POSTing a fake activity at the webhook. On top of that, only messages
from someone whose Teams account resolves (via the conversation's own member list) to the owner's or business
partner's email (or a manager's) are answered; everyone else is silently ignored.

The same connection is used proactively for approvals (services/teams_approvals.py): once an approver has messaged
the bot, Jarvis can post them an Adaptive Card with Approve / Deny buttons. `send_activity` / `update_activity` /
`reply` refuse any serviceUrl that isn't a Bot Framework / Teams host before the bearer token is ever sent.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from ..config import Settings

log = logging.getLogger(__name__)

OPENID_CONFIG = "https://login.botframework.com/v1/.well-known/openidconfiguration"
CONNECTOR_SCOPE = "https://api.botframework.com/.default"
# Bot Framework only ever calls back from these domains; anything else claiming to be Teams is rejected
# before we trust its serviceUrl enough to POST a reply (and the caller's real conversation history) to it.
TRUSTED_SERVICE_URL_SUFFIXES = (".botframework.com", ".trafficmanager.net", ".teams.microsoft.com")


class TeamsBotError(RuntimeError):
    pass


_jwks_client: PyJWKClient | None = None
_openid_cache: tuple[float, dict[str, Any]] | None = None


async def _openid_metadata(http: httpx.AsyncClient) -> dict[str, Any]:
    global _openid_cache
    if _openid_cache and time.time() - _openid_cache[0] < 3600:
        return _openid_cache[1]
    r = await http.get(OPENID_CONFIG, timeout=15)
    r.raise_for_status()
    _openid_cache = (time.time(), r.json())
    return _openid_cache[1]


async def verify_activity(auth_header: str | None, app_id: str, http: httpx.AsyncClient) -> None:
    """Raises TeamsBotError unless `auth_header` is a currently-valid Bot Framework token issued to us."""
    global _jwks_client
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise TeamsBotError("no bearer token")
    token = auth_header.split(None, 1)[1]
    try:
        metadata = await _openid_metadata(http)
        if _jwks_client is None or _jwks_client.uri != metadata["jwks_uri"]:
            _jwks_client = PyJWKClient(metadata["jwks_uri"])
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        jwt.decode(token, signing_key.key, algorithms=["RS256"], audience=app_id,
                  issuer=metadata.get("issuer", "https://api.botframework.com"))
    except jwt.PyJWTError as e:
        raise TeamsBotError(f"invalid token: {e}") from None
    except httpx.HTTPError as e:
        raise TeamsBotError(f"couldn't verify against Microsoft: {e}") from None


def trusted_service_url(url: str) -> bool:
    try:
        host = httpx.URL(url).host or ""
    except Exception:  # noqa: BLE001
        return False
    return host == "botframework.com" or any(host.endswith(suf) for suf in TRUSTED_SERVICE_URL_SUFFIXES)


class TeamsBot:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.s = settings
        self.http = http
        self._token: tuple[str, float] | None = None  # (access_token, expires_at)

    @property
    def configured(self) -> bool:
        return self.s.teams_bot_configured

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token[1] - 60:
            return self._token[0]
        url = f"https://login.microsoftonline.com/{self.s.teams_bot_tenant_id}/oauth2/v2.0/token"
        r = await self.http.post(url, data={
            "grant_type": "client_credentials", "client_id": self.s.teams_bot_app_id,
            "client_secret": self.s.teams_bot_app_password, "scope": CONNECTOR_SCOPE}, timeout=20)
        r.raise_for_status()
        data = r.json()
        self._token = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
        return self._token[0]

    async def check(self) -> str:
        if not self.configured:
            raise TeamsBotError("not set up")
        await self._access_token()
        return "Teams chat bot credentials accepted"

    async def sender_email(self, service_url: str, conversation_id: str, from_id: str) -> str | None:
        """The Teams sender's email, via the conversation's own member list (Bot Framework doesn't put email
        on the activity itself)."""
        token = await self._access_token()
        r = await self.http.get(
            f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/members",
            headers={"Authorization": f"Bearer {token}"}, timeout=20)
        r.raise_for_status()
        for member in r.json():
            if member.get("id") == from_id:
                return member.get("email") or member.get("userPrincipalName")
        return None

    async def reply(self, service_url: str, conversation_id: str, text: str) -> None:
        base = self._checked(service_url)
        token = await self._access_token()
        r = await self.http.post(
            f"{base}/v3/conversations/{conversation_id}/activities",
            headers={"Authorization": f"Bearer {token}"},
            json={"type": "message", "text": text}, timeout=30)
        r.raise_for_status()

    # -- proactive messages (approvals) ---------------------------------------------------------------------
    @staticmethod
    def _checked(service_url: str) -> str:
        """The serviceUrl we are about to POST a bearer token to - only ever a Bot Framework / Teams host."""
        if not trusted_service_url(service_url):
            raise TeamsBotError("untrusted service url")
        return service_url.rstrip("/")

    async def send_activity(self, service_url: str, conversation_id: str, activity: dict[str, Any]) -> str:
        """Post a message activity (text and/or an Adaptive Card attachment) into a conversation we already know.
        Returns the new activity's id ("" if Teams didn't give one)."""
        base = self._checked(service_url)
        token = await self._access_token()
        r = await self.http.post(f"{base}/v3/conversations/{conversation_id}/activities",
                                 headers={"Authorization": f"Bearer {token}"}, json=activity, timeout=30)
        r.raise_for_status()
        try:
            return str((r.json() or {}).get("id") or "")
        except ValueError:
            return ""

    async def update_activity(self, service_url: str, conversation_id: str, activity_id: str,
                              activity: dict[str, Any]) -> None:
        """Replace an earlier message of ours (used to turn an approval card into "Approved by ...")."""
        base = self._checked(service_url)
        token = await self._access_token()
        r = await self.http.put(f"{base}/v3/conversations/{conversation_id}/activities/{activity_id}",
                                headers={"Authorization": f"Bearer {token}"},
                                json={**activity, "id": activity_id}, timeout=30)
        r.raise_for_status()
