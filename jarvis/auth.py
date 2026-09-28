"""Owner authentication for the HUD/API and the staff reporting key.

The HUD exposes email, finances and staff data, so it is always protected:
with JARVIS_OWNER_PASSWORD set, a signed session cookie is required; without
one, Jarvis only answers requests from the local machine.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request, WebSocket

from .config import Settings

COOKIE = "jarvis_session"
SESSION_DAYS = 30
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _sign(settings: Settings, payload: str) -> str:
    key = (settings.jarvis_secret_key + settings.jarvis_owner_password).encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_session(settings: Settings) -> str:
    expires = str(int(time.time()) + SESSION_DAYS * 86400)
    return f"{expires}.{_sign(settings, expires)}"


def valid_session(settings: Settings, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expires, sig = token.split(".", 1)
    return (expires.isdigit() and int(expires) > time.time()
            and hmac.compare_digest(sig, _sign(settings, expires)))


def check_password(settings: Settings, password: str) -> bool:
    return bool(settings.jarvis_owner_password) and hmac.compare_digest(
        password.encode(), settings.jarvis_owner_password.encode())


def _client_host(conn: Request | WebSocket) -> str:
    return conn.client.host if conn.client else ""


def is_owner(settings: Settings, conn: Request | WebSocket) -> bool:
    if not settings.jarvis_owner_password:
        # Local-only mode. Anything that came through a proxy is treated as remote, so a spoofed
        # X-Forwarded-For can't pass as localhost.
        proxied = any(h in conn.headers for h in ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-arr-log-id"))
        return _client_host(conn) in LOCAL_HOSTS and not proxied
    return valid_session(settings, conn.cookies.get(COOKIE))


def require_owner(settings: Settings, request: Request) -> None:
    if not is_owner(settings, request):
        raise HTTPException(status_code=401, detail="Not signed in")


def staff_key_ok(settings: Settings, key: str | None, conn: Request) -> bool:
    if is_owner(settings, conn):
        return True
    return bool(settings.staff_report_key) and bool(key) and hmac.compare_digest(key, settings.staff_report_key)


def new_state() -> str:
    return secrets.token_urlsafe(24)
