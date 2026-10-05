"""The second shared mailbox (service@): where Bradford Council portal job requests arrive.

What lives here is only the READ side that the console and the Settings page need:

* ``ServiceInbox.unread()`` - the unread messages the Comms drawer shows, labelled "service@" so the owner can tell the two
  inboxes apart;
* ``ServiceInbox.test()`` - the Settings > Service inbox "Test" button: reads one message header through Microsoft Graph and
  says plainly what worked, or what Microsoft said and the most likely fix;
* ``explain_graph_error`` - the Graph error -> plain English mapping both use.

The address is whatever the OWNER saved in Settings (``service_inbox``; owner-only, see settings_store.OWNER_ONLY_KEYS).
Nothing here - and no tool - accepts a mailbox address from a model or from an email: callers choose the enumerated word
"owner" or "service" and ``microsoft365.mailbox_for`` turns it into the saved address. Everything here is read-only: it never
sends, replies, moves, flags or deletes anything in the mailbox.

The council-request scan that proposes jobs from this mailbox is in services/council_intake.py.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..redact import redact_text

log = logging.getLogger(__name__)

UNREAD_CACHE_S = 30  # the console polls /api/status often; don't double the Graph traffic for a second mailbox
NOT_PERMITTED = ("the Azure app registration has no permission on this mailbox. It needs a Microsoft Graph "
                 "Mail.Read or Mail.ReadWrite APPLICATION permission with admin consent, and if an Exchange Application "
                 "Access Policy limits which mailboxes the app can use, {address} must be added to it (then allow up to "
                 "30 minutes for Exchange to apply the change)")


def label_for(address: str) -> str:
    """"service@saltsfireandsecurity.co.uk" -> "service@" - the short name the console shows for this inbox."""
    local = (address or "").strip().lower().split("@")[0]
    return f"{local}@" if local else "service@"


def _graph_error(e: httpx.HTTPStatusError) -> tuple[str, str]:
    """(code, message) Microsoft put in the error body, or ("", "") if it isn't the usual Graph JSON."""
    try:
        err = e.response.json().get("error") or {}
        return str(err.get("code") or ""), str(err.get("message") or "")
    except Exception:  # noqa: BLE001 - not JSON, or not the shape we expect
        return "", ""


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    """``text`` with token-shaped strings and every configured secret value (by exact match) blanked out."""
    out = redact_text(text)
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "[REDACTED]")
    return out


def explain_graph_error(e: Exception, address: str, secrets: tuple[str, ...] = ()) -> str:
    """A plain sentence for what went wrong reading ``address``, and the likely fix. Never includes a secret: Graph's own
    message is only used for a code we don't recognise, and then only after redaction (token shapes, plus every configured
    secret value in ``secrets``) and a length cap."""
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        code, message = _graph_error(e)
        if status == 403 or code in ("ErrorAccessDenied", "Authorization_RequestDenied"):
            return f"Microsoft refused access (403 {code or 'ErrorAccessDenied'}): " + NOT_PERMITTED.format(address=address) + "."
        if status == 401:
            return ("Microsoft rejected Jarvis's sign-in (401). Check the tenant ID, application ID and client secret "
                    "under Connections > Microsoft 365 - the client secret may have expired.")
        if code == "MailboxNotEnabledForRESTAPI":
            return (f"{address} isn't an active Exchange Online mailbox that Graph can read (MailboxNotEnabledForRESTAPI). "
                    "A user mailbox needs a licence; a shared mailbox doesn't. If it was only just created, wait a few "
                    "minutes and test again.")
        if status == 404 or code in ("ErrorInvalidUser", "ResourceNotFound", "MailboxNotFound"):
            return (f"Microsoft can't find a mailbox called {address} in this tenant (404 {code or 'not found'}). "
                    "Check the spelling, and that it is a mailbox or shared mailbox in the same Microsoft 365 tenant.")
        if status == 429 or status >= 500:
            return f"Microsoft was busy or unavailable (HTTP {status}). Try again in a minute."
        extra = f": {_scrub(message, secrets)[:160]}" if message else ""
        return f"Microsoft returned an error (HTTP {status} {code}){extra}".strip()
    if isinstance(e, ValueError):
        return "That doesn't look like a mailbox address."
    text = _scrub(str(e) or type(e).__name__, secrets)
    if text.startswith("Graph auth failed"):
        return ("Jarvis couldn't sign in to Microsoft 365 - check the tenant ID, application ID and client secret "
                f"under Connections > Microsoft 365. ({text[:200]})")
    if isinstance(e, httpx.TimeoutException):
        return "Microsoft didn't answer in time. Try again in a minute."
    if isinstance(e, httpx.ConnectError):
        return "Jarvis couldn't reach Microsoft Graph - check the server's internet access."
    return text[:300]


class ServiceInbox:
    def __init__(self, j):
        self.j = j
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._cache_for = ""

    def _secrets(self) -> tuple[str, ...]:
        """Every secret value Jarvis holds (by the Settings page's own list of secret fields): none may reach the screen."""
        from ..settings_store import FIELDS

        return tuple(str(getattr(self.j.settings, k, "") or "") for k, f in FIELDS.items() if f.kind == "secret")

    @property
    def address(self) -> str:
        return (self.j.settings.service_inbox or "").strip().lower()

    @property
    def enabled(self) -> bool:
        return bool(self.address)

    async def unread(self, top: int = 8) -> dict[str, Any]:
        """What the Comms drawer shows for this inbox. ``{"enabled": False}`` when it isn't set up. Never raises: a Graph
        problem comes back as an ``error`` the drawer prints, in words (not a stack trace)."""
        address = self.address
        if not address:
            return {"enabled": False}
        out: dict[str, Any] = {"enabled": True, "label": label_for(address), "address": address,
                               "demo": bool(getattr(self.j.mail, "demo", True)), "unread": []}
        if out["demo"]:
            return out  # Microsoft 365 isn't connected, so there is nothing real to read
        now = time.monotonic()
        if self._cache and self._cache_for == address and now - self._cache[0] < UNREAD_CACHE_S:
            return dict(self._cache[1])
        try:
            out["unread"] = await self.j.mail.list_messages(unread_only=True, top=top, mailbox=address)
        except Exception as e:  # noqa: BLE001 - shown to the owner as a sentence
            log.info("Service inbox unread read failed: %s", type(e).__name__)
            out["error"] = explain_graph_error(e, address, self._secrets())
            return out  # an error is not cached: fixing the permission shows at once
        self._cache, self._cache_for = (now, out), address
        return dict(out)

    async def test(self) -> tuple[bool, str]:
        """The Settings > Service inbox Test button: read ONE message header and report. Read-only."""
        address = self.address
        if not address:
            return False, "Enter the service inbox address and save first."
        if getattr(self.j.mail, "demo", True):
            return False, ("Connect Microsoft 365 first (tenant ID, application ID, client secret and your mailbox, "
                           "under Connections > Microsoft 365) - Jarvis reads this mailbox through the same app.")
        try:
            messages = await self.j.mail.list_messages(top=1, mailbox=address)
        except Exception as e:  # noqa: BLE001 - every failure becomes a readable sentence
            log.info("Service inbox test failed: %s", type(e).__name__)
            return False, explain_graph_error(e, address, self._secrets())
        self._cache = None
        if not messages:
            return True, f"Reading {address} works. Its inbox is empty right now."
        m = messages[0]
        sender = " ".join(str(m.get("from_email") or "unknown sender").split())[:80]
        when = str(m.get("received") or "")[:19].replace("T", " ")
        return True, f"Reading {address} works. Latest message: received {when or 'at an unknown time'} from {sender}."
