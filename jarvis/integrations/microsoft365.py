"""Microsoft 365: read/send Outlook mail via Microsoft Graph and post Teams updates.

Graph uses app-only auth (client credentials). Required application permissions:
Mail.ReadWrite and Mail.Send. Restrict the app to the owner's mailbox with an
Exchange Online application access policy (see README).
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import msal

from ..config import Settings

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"
MESSAGE_FIELDS = "id,subject,from,toRecipients,receivedDateTime,isRead,importance,bodyPreview,hasAttachments,webLink"


def _strip(text: str, limit: int = 6000) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"


def _summarise(msg: dict[str, Any]) -> dict[str, Any]:
    sender = (msg.get("from") or {}).get("emailAddress") or {}
    return {
        "id": msg.get("id"),
        "subject": msg.get("subject") or "(no subject)",
        "from_name": sender.get("name", ""),
        "from_email": sender.get("address", ""),
        "received": msg.get("receivedDateTime"),
        "is_read": msg.get("isRead"),
        "importance": msg.get("importance"),
        "preview": (msg.get("bodyPreview") or "")[:300],
        "has_attachments": msg.get("hasAttachments"),
        "link": msg.get("webLink"),
    }


class GraphMail:
    demo = False

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.s = settings
        self.http = http
        self._app = msal.ConfidentialClientApplication(
            settings.ms_client_id,
            authority=f"https://login.microsoftonline.com/{settings.ms_tenant_id}",
            client_credential=settings.ms_client_secret,
        )

    async def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        result = await asyncio.to_thread(self._app.acquire_token_for_client,
                                         scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise RuntimeError(f"Graph auth failed: {result.get('error_description', result)}")
        headers = {"Authorization": f"Bearer {result['access_token']}"}
        headers.update(extra or {})
        return headers

    @property
    def _mbx(self) -> str:
        return f"{GRAPH}/users/{self.s.ms_mailbox}"

    async def list_messages(self, unread_only: bool = False, top: int = 15, since_hours: int | None = None,
                            folder: str = "inbox") -> list[dict[str, Any]]:
        filters = []
        if unread_only:
            filters.append("isRead eq false")
        if since_hours:
            since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            filters.append(f"receivedDateTime ge {since}")
        params = {"$top": str(top), "$select": MESSAGE_FIELDS, "$orderby": "receivedDateTime desc"}
        if filters:
            params["$filter"] = " and ".join(filters)
        r = await self.http.get(f"{self._mbx}/mailFolders/{folder}/messages", params=params,
                                headers=await self._headers())
        r.raise_for_status()
        return [_summarise(m) for m in r.json().get("value", [])]

    async def search_messages(self, query: str, top: int = 15) -> list[dict[str, Any]]:
        params = {"$search": f'"{query.replace(chr(34), "")}"', "$top": str(top), "$select": MESSAGE_FIELDS}
        r = await self.http.get(f"{self._mbx}/messages", params=params,
                                headers=await self._headers({"ConsistencyLevel": "eventual"}))
        r.raise_for_status()
        return [_summarise(m) for m in r.json().get("value", [])]

    async def get_message(self, message_id: str) -> dict[str, Any]:
        r = await self.http.get(
            f"{self._mbx}/messages/{message_id}",
            params={"$select": MESSAGE_FIELDS + ",body,ccRecipients"},
            headers=await self._headers({"Prefer": 'outlook.body-content-type="text"'}),
        )
        r.raise_for_status()
        msg = r.json()
        out = _summarise(msg)
        out["to"] = [t["emailAddress"]["address"] for t in msg.get("toRecipients", [])]
        out["cc"] = [t["emailAddress"]["address"] for t in msg.get("ccRecipients", [])]
        out["body"] = _strip((msg.get("body") or {}).get("content", ""))
        return out

    async def mark_read(self, message_id: str) -> None:
        r = await self.http.patch(f"{self._mbx}/messages/{message_id}", json={"isRead": True},
                                  headers=await self._headers())
        r.raise_for_status()

    async def send_mail(self, to: list[str], subject: str, body_html: str, cc: list[str] | None = None) -> None:
        message = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        }
        if cc:
            message["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
        r = await self.http.post(f"{self._mbx}/sendMail", json={"message": message, "saveToSentItems": True},
                                 headers=await self._headers())
        r.raise_for_status()

    async def create_reply_draft(self, message_id: str, comment: str) -> dict[str, Any]:
        r = await self.http.post(f"{self._mbx}/messages/{message_id}/createReply", json={"comment": comment},
                                 headers=await self._headers())
        r.raise_for_status()
        draft = r.json()
        return {"draft_id": draft.get("id"), "link": draft.get("webLink")}

    async def check(self) -> str:
        await self.list_messages(top=1)
        return "Graph mailbox reachable"

    # -- Teams meetings + transcripts ---------------------------------------------------
    async def recent_meetings(self, days: int = 7) -> list[dict[str, Any]]:
        """Teams meetings in the owner's calendar. Needs Calendars.Read (application)."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        r = await self.http.get(f"{self._mbx}/calendarView", headers=await self._headers(), params={
            "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "$select": "subject,start,end,isOnlineMeeting,onlineMeeting,attendees", "$top": "50",
            "$orderby": "start/dateTime desc"})
        r.raise_for_status()
        out = []
        for ev in r.json().get("value", []):
            if ev.get("isOnlineMeeting") and (ev.get("onlineMeeting") or {}).get("joinUrl"):
                out.append({"subject": ev.get("subject"), "start": (ev.get("start") or {}).get("dateTime"),
                            "join_url": ev["onlineMeeting"]["joinUrl"],
                            "attendees": [a["emailAddress"]["address"] for a in ev.get("attendees", [])]})
        return out

    async def meeting_transcript(self, join_url: str) -> str:
        """Latest transcript of a Teams meeting. Needs OnlineMeetingTranscript.Read.All plus a Teams
        application access policy for the app (see README)."""
        headers = await self._headers()
        r = await self.http.get(f"{self._mbx}/onlineMeetings", headers=headers,
                                params={"$filter": f"JoinWebUrl eq '{join_url}'"})
        r.raise_for_status()
        meetings = r.json().get("value", [])
        if not meetings:
            raise RuntimeError("Meeting not found (was it organised by this mailbox?)")
        mid = meetings[0]["id"]
        r = await self.http.get(f"{self._mbx}/onlineMeetings/{mid}/transcripts", headers=headers)
        r.raise_for_status()
        transcripts = r.json().get("value", [])
        if not transcripts:
            raise RuntimeError("No transcript - was transcription switched on in the meeting?")
        tid = transcripts[-1]["id"]
        r = await self.http.get(f"{self._mbx}/onlineMeetings/{mid}/transcripts/{tid}/content",
                                params={"$format": "text/vtt"}, headers=headers, follow_redirects=True)
        r.raise_for_status()
        lines = [ln for ln in r.text.splitlines() if ln and "-->" not in ln and ln != "WEBVTT" and not ln.isdigit()]
        return _strip("\n".join(lines), limit=150_000)

    # -- Microsoft 365 usage reports (activity counts only, never content) ------------
    async def _usage_report(self, report: str, days: int) -> list[dict[str, str]]:
        period = next((p for p in (7, 30, 90, 180) if days <= p), 180)
        r = await self.http.get(f"{GRAPH}/reports/{report}(period='D{period}')", headers=await self._headers(),
                                follow_redirects=True, timeout=60)
        r.raise_for_status()
        text = r.content.decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(text)))

    async def activity(self, days: int = 30) -> dict[str, dict[str, Any]]:
        """Per-user email + Teams activity counts. Needs the Reports.Read.All application permission, and the
        tenant setting that conceals user names in reports turned off."""
        people: dict[str, dict[str, Any]] = {}

        def row_for(r: dict[str, str]) -> dict[str, Any]:
            upn = (r.get("User Principal Name") or "").lower()
            return people.setdefault(upn, {"email": upn, "name": r.get("Display Name") or upn})

        def num(v: str | None) -> int:
            try:
                return int(float(v or 0))
            except ValueError:
                return 0

        for r in await self._usage_report("getEmailActivityUserDetail", days):
            if r.get("Is Deleted", "False") == "True":
                continue
            p = row_for(r)
            p.update(emails_sent=num(r.get("Send Count")), emails_received=num(r.get("Receive Count")),
                     emails_read=num(r.get("Read Count")), last_email_activity=r.get("Last Activity Date"))
        for r in await self._usage_report("getTeamsUserActivityUserDetail", days):
            if r.get("Is Deleted", "False") == "True":
                continue
            p = row_for(r)
            p.update(teams_chat_messages=num(r.get("Team Chat Message Count")) + num(r.get("Private Chat Message Count")),
                     teams_calls=num(r.get("Call Count")), teams_meetings=num(r.get("Meeting Count")),
                     last_teams_activity=r.get("Last Activity Date"))
        return people


class DemoMail:
    """Fictional inbox so the display works before Microsoft 365 is connected."""

    demo = True

    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self._messages = [
            {"id": "demo-1", "subject": "Quote request - Vigilon panel upgrade (DEMO)", "from_name": "Facilities Manager",
             "from_email": "facilities@example-school.org.uk", "received": (now - timedelta(minutes=35)).isoformat(),
             "is_read": False, "importance": "high", "has_attachments": True, "link": "",
             "preview": "Could you quote to replace our 4-loop panel with a Gent Vigilon and new detection in block B?",
             "body": "Hi,\n\nOur fire risk assessor flagged the ageing panel in block B. Could you quote to replace it "
                     "with a Gent Vigilon 4-loop panel, new optical detectors and sounders? Drawings attached.\n\n"
                     "We'd like works done over the October half term if possible.\n\nThanks"},
            {"id": "demo-2", "subject": "Fault on panel - zone 3 (DEMO)", "from_name": "Site Manager",
             "from_email": "manager@example-carehome.co.uk", "received": (now - timedelta(hours=2)).isoformat(),
             "is_read": False, "importance": "high", "has_attachments": False, "link": "",
             "preview": "The panel is showing a fault on zone 3 and beeping constantly.",
             "body": "Morning, the fire panel is showing 'Zone 3 open circuit' and the buzzer keeps going. "
                     "Can someone come out today? It's a care home so we need it sorting."},
            {"id": "demo-3", "subject": "Remittance advice - INV-10421 (DEMO)", "from_name": "Accounts Payable",
             "from_email": "ap@example-housing.co.uk", "received": (now - timedelta(hours=5)).isoformat(),
             "is_read": True, "importance": "normal", "has_attachments": True, "link": "",
             "preview": "Please find attached remittance for invoice INV-10421, £2,340.00.",
             "body": "Please find attached remittance for invoice INV-10421, £2,340.00 paid by BACS today."},
            {"id": "demo-4", "subject": "[ISSUE] Can't attach photos to job sheet (DEMO)", "from_name": "Field Engineer",
             "from_email": "engineer@example.co.uk", "received": (now - timedelta(hours=7)).isoformat(),
             "is_read": False, "importance": "normal", "has_attachments": False, "link": "",
             "preview": "When I try to add photos to a job sheet on my phone it spins forever.",
             "body": "When I try to add photos to a job sheet in Salts FSM on my phone the upload spinner never "
                     "finishes. Happens on every job today."},
        ]

    async def list_messages(self, unread_only: bool = False, top: int = 15, since_hours: int | None = None,
                            folder: str = "inbox") -> list[dict[str, Any]]:
        msgs = [m for m in self._messages if not unread_only or not m["is_read"]]
        return [{k: v for k, v in m.items() if k != "body"} for m in msgs[:top]]

    async def search_messages(self, query: str, top: int = 15) -> list[dict[str, Any]]:
        q = query.lower()
        hits = [m for m in self._messages if q in (m["subject"] + m["body"]).lower()]
        return [{k: v for k, v in m.items() if k != "body"} for m in hits[:top]]

    async def get_message(self, message_id: str) -> dict[str, Any]:
        for m in self._messages:
            if m["id"] == message_id:
                return {**m, "to": [], "cc": []}
        raise KeyError(f"No message {message_id}")

    async def mark_read(self, message_id: str) -> None:
        for m in self._messages:
            if m["id"] == message_id:
                m["is_read"] = True

    async def send_mail(self, to: list[str], subject: str, body_html: str, cc: list[str] | None = None) -> None:
        log.info("[DEMO] would send email to %s: %s", to, subject)

    async def create_reply_draft(self, message_id: str, comment: str) -> dict[str, Any]:
        return {"draft_id": "demo-draft", "link": ""}

    async def check(self) -> str:
        return "demo inbox"

    async def recent_meetings(self, days: int = 7) -> list[dict[str, Any]]:
        when = (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=9, minute=0).isoformat()
        return [{"subject": "Monday ops meeting (DEMO)", "start": when, "join_url": "demo-meeting",
                 "attendees": ["hannah.cole@example.co.uk", "josh.pryce@example.co.uk"]}]

    async def meeting_transcript(self, join_url: str) -> str:
        return ("<v Alex>Right, three things. Josh, can you get the revised Vigilon quote for the Ilkley annexe out by "
                "Wednesday?\n<v Josh>Yes, I'll send it Wednesday.\n<v Alex>Hannah, the care home call-out from "
                "yesterday is still unassigned - get someone on it today.\n<v Hannah>I'll book Priya this afternoon.\n"
                "<v Alex>And Rachel needs to chase Kestrel Retail about that old invoice before Friday.\n"
                "<v Hannah>I'll let her know. Also we agreed to trial the new van stock lists next month.")

    async def activity(self, days: int = 30) -> dict[str, dict[str, Any]]:
        scale = days / 30
        base = {"hannah cole": (410, 1320, 380, 44, 61), "josh pryce": (520, 980, 260, 71, 38),
                "rachel gill": (190, 640, 150, 12, 22)}
        out = {}
        for name, (sent, recv, chats, calls, meetings) in base.items():
            email = name.replace(" ", ".") + "@example.co.uk"
            out[email] = {"email": email, "name": name.title(), "emails_sent": int(sent * scale),
                          "emails_received": int(recv * scale), "teams_chat_messages": int(chats * scale),
                          "teams_calls": int(calls * scale), "teams_meetings": int(meetings * scale)}
        return out


class TeamsNotifier:
    """Posts to a Teams channel through a Workflows ("When a Teams webhook request is received") URL."""

    def __init__(self, webhook_url: str, http: httpx.AsyncClient):
        self.url = webhook_url
        self.http = http

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def post(self, title: str, body: str) -> None:
        if not self.url:
            return
        card = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": f"JARVIS · {title}", "weight": "Bolder", "size": "Medium",
                         "wrap": True},
                        {"type": "TextBlock", "text": body, "wrap": True},
                    ],
                },
            }],
        }
        r = await self.http.post(self.url, json=card)
        r.raise_for_status()


def text_to_html(text: str) -> str:
    return "<div style='font-family:Segoe UI,Arial,sans-serif'>" + html.escape(text).replace("\n", "<br>") + "</div>"
