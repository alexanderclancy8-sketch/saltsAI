"""Authentication for the HUD/API and the staff reporting key.

The HUD exposes email, finances and staff data, so it is always protected:
with JARVIS_OWNER_PASSWORD set, a signed session cookie is required; without
one, Jarvis only answers requests from the local machine. On Azure, managers
listed in MANAGER_EMAILS can also get in through App Service's Microsoft sign-in.

Team mode adds a third kind of session for engineers and office staff: a team access code the owner sets (never stored
in the clear, see services/team_access.py) is exchanged at /login/team for a SEPARATE cookie (``TEAM_COOKIE``) signed with a
key of its own, so a team cookie can never satisfy ``is_owner`` / ``is_principal_owner`` and an owner cookie is not a team
one. ``role_of`` is the one place a connection is turned into a role (jarvis/access.py); ``is_owner`` and
``is_principal_owner`` are unchanged and still mean owner-or-manager / the owner themself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import HTTPException, Request, WebSocket

from . import access
from .config import Settings

COOKIE = "jarvis_session"
SESSION_DAYS = 30
TEAM_COOKIE = "jarvis_team_session"
TEAM_SESSION_DAYS = 7
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


def signed_in_manager(settings: Settings, conn: Request | WebSocket) -> str | None:
    """Email of a manager signed in through Azure App Service's Microsoft sign-in, if any.

    App Service removes X-MS-CLIENT-PRINCIPAL-* headers from outside requests and only adds them
    itself when its authentication is switched on (WEBSITE_AUTH_ENABLED), so they're only trusted then.
    """
    if os.environ.get("WEBSITE_AUTH_ENABLED", "").lower() != "true" or not settings.managers:
        return None
    if conn.headers.get("x-ms-client-principal-idp", "").lower() not in ("aad", "azureactivedirectory"):
        return None
    name = conn.headers.get("x-ms-client-principal-name", "").strip().lower()
    return name if name in settings.managers else None


def is_owner(settings: Settings, conn: Request | WebSocket) -> bool:
    if signed_in_manager(settings, conn):
        return True
    if not settings.jarvis_owner_password:
        # Local-only mode. Anything that came through a proxy is treated as remote, so a spoofed
        # X-Forwarded-For can't pass as localhost.
        proxied = any(h in conn.headers for h in ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-arr-log-id"))
        return _client_host(conn) in LOCAL_HOSTS and not proxied
    return valid_session(settings, conn.cookies.get(COOKIE))


def is_principal_owner(settings: Settings, conn: Request | WebSocket, owner_email: str) -> bool:
    """The owner themself - stricter than is_owner(), which also lets in any manager signed in through Microsoft.

    Used for settings that widen what Jarvis may do without asking (standing approvals) or that decide who counts
    as the owner (owner/partner email, display password, staff key): the display-password session, the owner's own
    Microsoft sign-in, or local-only mode with no password set. Another signed-in manager, the partner included,
    is not enough.

    `owner_email` must be the owner's address as configured OUTSIDE the Settings page (the OWNER_EMAIL app
    setting / .env value captured when the app started - see main.create_app), never the live, hot-reloaded
    `settings.owner_email`: that one can be edited on the Settings page, and a principal-owner check that trusts
    a value a manager can change is no check at all. Blank means no Microsoft sign-in counts as the owner."""
    manager = signed_in_manager(settings, conn)
    if manager and owner_email and manager == owner_email.strip().lower():
        return True
    if settings.jarvis_owner_password:
        return valid_session(settings, conn.cookies.get(COOKIE))
    return manager is None and is_owner(settings, conn)


def require_owner(settings: Settings, request: Request) -> None:
    if not is_owner(settings, request):
        raise HTTPException(status_code=401, detail="Not signed in")


def require_same_origin(settings: Settings, request: Request) -> None:
    """CSRF guard for the buttons that change what Jarvis does (Approve, Don't send, Edit, Retry, memory edits).

    The session cookie is SameSite=Lax, which already stops browsers attaching it to a cross-site POST; this is the
    second line (and covers the Microsoft sign-in cookie, which Jarvis does not control): a browser that says the
    request came from another site (Sec-Fetch-Site) or from another origin (Origin) is refused with 403. Requests
    carrying neither header (curl, the test client, server-to-server) are not browser clicks and pass - they still need
    the owner's session."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.lower() not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="That request didn't come from the console.")
    origin = request.headers.get("origin")
    if origin is not None:
        from urllib.parse import urlsplit

        host = urlsplit(origin).netloc.lower() if origin != "null" else ""
        ours = {h.strip().lower() for h in (request.headers.get("host", "") + "," + request.headers.get("x-forwarded-host", ""))
                .split(",") if h.strip()}
        ours.add(urlsplit(settings.public_base_url or "").netloc.lower())
        if not host or host not in ours:
            raise HTTPException(status_code=403, detail="That request didn't come from the console.")


def staff_key_ok(settings: Settings, key: str | None, conn: Request) -> bool:
    if is_owner(settings, conn):
        return True
    return bool(settings.staff_report_key) and bool(key) and hmac.compare_digest(key, settings.staff_report_key)


def new_state() -> str:
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------------------------------- team sessions
def _team_key(settings: Settings, code_digest: str) -> bytes:
    """The signing key for team cookies. It includes the stored digest of the team access code, so changing or switching
    off the code signs every team session out at once, and it is not derivable from JARVIS_SECRET_KEY alone (whose default
    is a known string)."""
    return hashlib.sha256(f"jarvis-team-session|{settings.jarvis_secret_key}|{code_digest}".encode()).digest()


def make_team_session(settings: Settings, code_digest: str, name: str) -> str:
    """A signed cookie value for a team member: name, a random session id and an expiry. Returns "" if team access is off."""
    if not code_digest:
        return ""
    body = base64.urlsafe_b64encode(json.dumps(
        {"n": access.clean_name(name), "s": secrets.token_hex(8), "e": int(time.time()) + TEAM_SESSION_DAYS * 86400},
        separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{body}.{hmac.new(_team_key(settings, code_digest), body.encode(), hashlib.sha256).hexdigest()}"


def read_team_session(settings: Settings, code_digest: str, token: str | None) -> access.Caller | None:
    """The team member a cookie value stands for, or None (no team access set, bad signature, expired, malformed)."""
    if not code_digest or not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(_team_key(settings, code_digest), body.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("e"), int) or data["e"] <= time.time():
        return None
    name, sid = access.clean_name(str(data.get("n") or "")), str(data.get("s") or "")
    if not name or not sid.isalnum():
        return None
    return access.Caller(access.TEAM, name, sid)


def role_of(settings: Settings, conn: Request | WebSocket, owner_email: str, team_digest: str = "") -> access.Caller | None:
    """What the signed-in person on this connection is: the owner, a manager, a team member, or None (not signed in).

    Owner and manager come from exactly the same checks as before (``is_principal_owner`` / ``is_owner``); a team session
    only counts when neither of those holds, so there is no way for a team cookie to be mistaken for more."""
    if is_principal_owner(settings, conn, owner_email):
        return access.Caller(access.OWNER)
    if is_owner(settings, conn):
        manager = signed_in_manager(settings, conn)
        return access.Caller(access.MANAGER, settings.person(manager) if manager else "")
    return read_team_session(settings, team_digest, conn.cookies.get(TEAM_COOKIE))
