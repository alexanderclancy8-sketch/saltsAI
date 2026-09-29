"""Teams chat: verifying Bot Framework's tokens, resolving who's messaging, and the webhook route."""

from __future__ import annotations

import base64
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from jarvis.core import Jarvis
from jarvis.integrations import teamsbot
from jarvis.integrations.teamsbot import TeamsBot, TeamsBotError, trusted_service_url, verify_activity
from jarvis.main import create_app
from tests.fakes import FakeClient, message, text_block

APP_ID = "bot-app-id"
ISSUER = "https://api.botframework.com"
JWKS_URL = "https://login.botframework.com/v1/.well-known/keys"


def make(settings, script=None):
    return Jarvis(settings, client=FakeClient(script))


def _b64(n: int, length: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()


def _jwk(public_key, kid: str = "test-kid") -> dict:
    numbers = public_key.public_numbers()
    return {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256",
            "n": _b64(numbers.n, (numbers.n.bit_length() + 7) // 8), "e": _b64(numbers.e, 3)}


def _sign(private_key, kid: str = "test-kid", **overrides) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": APP_ID, "iat": now, "exp": now + 300, **overrides}
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(autouse=True)
def stub_jwks(monkeypatch, keys):
    """Stands in for Bot Framework's real JWKS endpoint - PyJWKClient fetches over plain urllib, not httpx."""
    _, public_key = keys
    monkeypatch.setattr(teamsbot, "_jwks_client", None)
    monkeypatch.setattr(teamsbot, "_openid_cache", None)
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: {"keys": [_jwk(public_key)]})


def _http(handler=None) -> httpx.AsyncClient:
    def default_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler or default_handler))


async def test_verify_activity_accepts_a_genuine_token(keys):
    private_key, _ = keys
    await verify_activity(f"Bearer {_sign(private_key)}", APP_ID, _http())  # doesn't raise


async def test_verify_activity_rejects_missing_or_malformed_header(keys):
    with pytest.raises(TeamsBotError):
        await verify_activity(None, APP_ID, _http())
    with pytest.raises(TeamsBotError):
        await verify_activity("Basic dXNlcjpwYXNz", APP_ID, _http())


async def test_verify_activity_rejects_wrong_audience(keys):
    private_key, _ = keys
    with pytest.raises(TeamsBotError):
        await verify_activity(f"Bearer {_sign(private_key, aud='someone-elses-bot')}", APP_ID, _http())


async def test_verify_activity_rejects_expired_token(keys):
    private_key, _ = keys
    old = int(time.time()) - 3600
    with pytest.raises(TeamsBotError):
        await verify_activity(f"Bearer {_sign(private_key, iat=old, exp=old + 60)}", APP_ID, _http())


async def test_verify_activity_rejects_a_token_signed_by_the_wrong_key(keys):
    forger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(TeamsBotError):
        # Signed with a key that isn't in the (stubbed) JWKS at all - a forged token.
        await verify_activity(f"Bearer {_sign(forger)}", APP_ID, _http())


def test_trusted_service_url():
    assert trusted_service_url("https://smba.trafficmanager.net/uk/")
    assert trusted_service_url("https://europe.botframework.com/")
    assert trusted_service_url("https://something.teams.microsoft.com/")
    assert not trusted_service_url("https://evil.example.com/")
    assert not trusted_service_url("not a url")


async def test_sender_email_and_reply(settings):
    settings.teams_bot_app_id = "app"
    settings.teams_bot_app_password = "secret"
    settings.teams_bot_tenant_id = "tenant-1"
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})
        if request.url.path.endswith("/members"):
            return httpx.Response(200, json=[{"id": "other", "email": "someone@else.com"},
                                             {"id": "29:me", "userPrincipalName": "alex@salts.co.uk"}])
        if request.url.path.endswith("/activities"):
            seen["body"] = __import__("json").loads(request.content)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"id": "activity-1"})
        return httpx.Response(404)

    bot = TeamsBot(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await bot.sender_email("https://smba.trafficmanager.net/x", "conv-1", "29:me") == "alex@salts.co.uk"
    assert await bot.sender_email("https://smba.trafficmanager.net/x", "conv-1", "nobody") is None

    await bot.reply("https://smba.trafficmanager.net/x", "conv-1", "Good afternoon.")
    assert seen["auth"] == "Bearer tok-123" and seen["body"] == {"type": "message", "text": "Good afternoon."}
    await bot.http.aclose()


def test_webhook_ignores_an_unrecognised_sender(settings, monkeypatch):
    settings.owner_email = "alex@salts.co.uk"
    j = make(settings, [message([text_block("Hello!")])])
    app = create_app(settings, j)
    async def ok(*a, **k):
        return None

    monkeypatch.setattr("jarvis.main.verify_activity", ok)

    async def fake_sender_email(*a, **k):
        return "stranger@example.com"

    replies = []
    j.teamsbot.sender_email = fake_sender_email
    j.teamsbot.reply = lambda *a, **k: replies.append(a)

    with TestClient(app) as c:
        r = c.post("/api/teams/messages", json={
            "type": "message", "text": "hello", "serviceUrl": "https://smba.trafficmanager.net/x",
            "conversation": {"id": "conv-1"}, "from": {"id": "29:stranger"}})
        assert r.status_code == 200
        time.sleep(0.3)  # give any (wrongly-started) background task a chance to run
    assert replies == []  # never replied to - and never even asked Claude anything


def test_webhook_replies_to_the_owner(settings, monkeypatch):
    settings.owner_email = "alex@salts.co.uk"
    j = make(settings, [message([text_block("Right, all quiet on the Western front.")])])
    app = create_app(settings, j)
    async def ok(*a, **k):
        return None

    monkeypatch.setattr("jarvis.main.verify_activity", ok)

    async def fake_sender_email(*a, **k):
        return "Alex@Salts.co.uk"  # case shouldn't matter

    replies = []

    async def fake_reply(service_url, conversation_id, text):
        replies.append((service_url, conversation_id, text))

    j.teamsbot.sender_email = fake_sender_email
    j.teamsbot.reply = fake_reply

    with TestClient(app) as c:
        r = c.post("/api/teams/messages", json={
            "type": "message", "text": "Anything urgent?", "serviceUrl": "https://smba.trafficmanager.net/x",
            "conversation": {"id": "conv-1"}, "from": {"id": "29:owner"}})
        assert r.status_code == 200 and r.json() == {}
        for _ in range(40):
            if replies:
                break
            time.sleep(0.05)
    assert replies and replies[0] == ("https://smba.trafficmanager.net/x", "conv-1",
                                      "Right, all quiet on the Western front.")
    assert j.brain.messages[0]["content"][-1]["text"].endswith("· from Alex]") or "from Alex" in \
        j.brain.messages[0]["content"][-1]["text"]


def test_webhook_rejects_a_bad_token(settings, monkeypatch):
    j = make(settings, [message([text_block("Hi")])])
    app = create_app(settings, j)

    async def fail(*a, **k):
        raise TeamsBotError("bad token")

    monkeypatch.setattr("jarvis.main.verify_activity", fail)
    with TestClient(app) as c:
        r = c.post("/api/teams/messages", json={"type": "message", "text": "hi"})
        assert r.status_code == 401


def test_webhook_ignores_an_untrusted_service_url(settings, monkeypatch):
    j = make(settings, [message([text_block("Hi")])])
    app = create_app(settings, j)
    async def ok(*a, **k):
        return None

    monkeypatch.setattr("jarvis.main.verify_activity", ok)
    calls = []
    j.teamsbot.sender_email = lambda *a, **k: calls.append(1)

    with TestClient(app) as c:
        r = c.post("/api/teams/messages", json={
            "type": "message", "text": "hi", "serviceUrl": "https://evil.example.com",
            "conversation": {"id": "c"}, "from": {"id": "f"}})
        assert r.status_code == 200
    assert calls == []  # never even looked the sender up for an untrusted serviceUrl
