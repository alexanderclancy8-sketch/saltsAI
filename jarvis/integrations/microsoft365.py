"""Microsoft 365: read/send Outlook mail via Microsoft Graph and post Teams updates.

Graph uses app-only auth (client credentials). Required application permissions:
Mail.ReadWrite and Mail.Send (Mail.ReadWrite also covers listing folders and moving Jarvis's own emails to the
owner into the "salts jarvis" folder - no extra permission needed). Restrict the app to the owner's mailbox with an
Exchange Online application access policy (see README).

Other mailboxes (the out-of-hours reports mailbox, the service@ shared inbox) are read through the same app: pass
``mailbox=`` to the read methods, which only ever takes an address the owner saved in Settings (``mailbox_for``). Sending,
reply drafts, folder filing and mark-as-read stay on the owner's own mailbox.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import html
import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import msal

from ..config import Settings
from ..redact import describe_http_error
from .mail_guard import GuardedMessage, guard_message

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"
_MAILBOX_RE = re.compile(r"[A-Za-z0-9._%+'\-]{1,64}@[A-Za-z0-9.\-]{1,100}\.[A-Za-z]{2,24}")
FOLDER_MISS_TTL_S = 300  # how long a "folder not found" answer is trusted before looking again
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


SERVICE_SOURCE = "service"  # the value of a tool's `mailbox` argument that means the configured service inbox


def mailbox_for(settings: Settings, source: str | None) -> tuple[str | None, str]:
    """(Graph mailbox address or None for the owner's own, error message). The ONLY way a tool or the Comms drawer picks
    a mailbox: ``source`` is the enumerated word "owner" (default) or "service", never an address, so nothing a model
    or an email writes can point Jarvis at some other mailbox. "service" is the address the owner saved in Settings."""
    if not source or source == "owner":
        return None, ""
    if source == SERVICE_SOURCE:
        address = (settings.service_inbox or "").strip().lower()
        if not address:
            return None, ("The service inbox isn't set up. The owner can add its address in Settings > Service "
                          "inbox (service@).")
        return address, ""
    return None, "mailbox must be 'owner' or 'service'."


OFFICE_EXTENSIONS = (".docx", ".xlsx", ".pptx")  # macro-enabled (.docm/.xlsm) and legacy formats are deliberately not read
# Office names that are listed (so the owner is told why) but never downloaded: legacy binary formats and macro-enabled files
_UNREADABLE_OFFICE = (".doc", ".xls", ".ppt", ".docm", ".dotm", ".xlsm", ".xlsb", ".xltm", ".pptm", ".potm", ".ppsm")
FILE_ATTACHMENT = "#microsoft.graph.fileAttachment"
ITEM_ATTACHMENT = "#microsoft.graph.itemAttachment"        # an Outlook item (email, event, contact) attached to the email
REFERENCE_ATTACHMENT = "#microsoft.graph.referenceAttachment"  # a LINK to a OneDrive / SharePoint file, not the file itself
ATTACHMENT_LIST_SELECT = "id,name,size,contentType,isInline"  # never contentBytes: files are fetched one by one with /$value
MAX_ATTACHMENTS_FETCHED = 10


def attachment_kind(item: dict[str, Any]) -> str:
    """'file', 'item' or 'reference' for one entry of a Graph /attachments listing."""
    t = item.get("@odata.type")
    if t == ITEM_ATTACHMENT:
        return "item"
    if t == REFERENCE_ATTACHMENT:
        return "reference"
    return "file"


def is_pdf_attachment(name: str, content_type: str = "") -> bool:
    return (name or "").strip().lower().endswith(".pdf") or "pdf" in (content_type or "").lower()


def is_office_attachment(name: str, content_type: str = "") -> bool:
    return (name or "").strip().lower().endswith(OFFICE_EXTENSIONS + _UNREADABLE_OFFICE)


PLAN_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")


def is_plan_image_attachment(name: str, content_type: str = "") -> bool:
    """A PNG / JPEG picture attached to an email (a photo or export of a floor plan for services/plan_drawings.py)."""
    return (name or "").strip().lower().endswith(PLAN_IMAGE_EXTENSIONS) or (content_type or "").lower() in ("image/png", "image/jpeg")


def select_office_attachments(items: list[dict[str, Any]], max_bytes: int = 15_000_000) -> list[dict[str, str]]:
    """The Word/Excel file attachments (name + base64 data) from a Graph /attachments listing."""
    out = []
    for a in items:
        name = a.get("name") or ""
        if a.get("@odata.type") == "#microsoft.graph.fileAttachment" and name.lower().endswith(OFFICE_EXTENSIONS) \
                and a.get("contentBytes") and int(a.get("size") or 0) <= max_bytes:
            out.append({"name": name, "data": a["contentBytes"]})
    return out


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
        self._folder_ids: dict[str, str] = {}  # lower-cased folder name -> Graph folder id
        self._folder_missing_until: dict[str, float] = {}  # lower-cased folder name -> monotonic time to retry
        # Seconds to wait between looks for a freshly sent email to arrive in the Inbox (so: up to ~10s in all).
        self.owner_folder_retry_delays: tuple[float, ...] = (1.0, 2.0, 3.0, 4.0)

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

    def _base(self, mailbox: str | None) -> str:
        """The Graph URL root of ``mailbox`` (None = the owner's own mailbox, exactly as before). The address is only ever
        one the owner saved in Settings (ooh_mailbox, service_inbox) - callers resolve it with ``mailbox_for`` - but it is
        still checked to be one plain address before it goes into a URL path."""
        if not mailbox:
            return self._mbx
        if not _MAILBOX_RE.fullmatch(mailbox):
            raise ValueError("That is not a mailbox address.")
        return f"{GRAPH}/users/{mailbox}"

    async def list_messages(self, unread_only: bool = False, top: int = 15, since_hours: int | None = None,
                            folder: str = "inbox", mailbox: str | None = None) -> list[dict[str, Any]]:
        base = self._base(mailbox)
        filters = []
        if unread_only:
            filters.append("isRead eq false")
        if since_hours:
            since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            filters.append(f"receivedDateTime ge {since}")
        params = {"$top": str(top), "$select": MESSAGE_FIELDS, "$orderby": "receivedDateTime desc"}
        if filters:
            params["$filter"] = " and ".join(filters)
        r = await self.http.get(f"{base}/mailFolders/{folder}/messages", params=params,
                                headers=await self._headers())
        r.raise_for_status()
        return [_summarise(m) for m in r.json().get("value", [])]

    async def search_messages(self, query: str, top: int = 15, mailbox: str | None = None) -> list[dict[str, Any]]:
        params = {"$search": f'"{query.replace(chr(34), "")}"', "$top": str(top), "$select": MESSAGE_FIELDS}
        r = await self.http.get(f"{self._base(mailbox)}/messages", params=params,
                                headers=await self._headers({"ConsistencyLevel": "eventual"}))
        r.raise_for_status()
        return [_summarise(m) for m in r.json().get("value", [])]

    async def get_message(self, message_id: str, mailbox: str | None = None) -> dict[str, Any]:
        base = self._base(mailbox)
        r = await self.http.get(
            f"{base}/messages/{message_id}",
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

    async def _attachment_items(self, message_id: str, mailbox: str | None) -> list[dict[str, Any]]:
        """The attachments of a message WITHOUT their content (names, sizes, kinds): the listing alone can't carry a
        multi-megabyte file reliably, so each file is then fetched on its own with ``_attachment_bytes``."""
        r = await self.http.get(f"{self._base(mailbox)}/messages/{message_id}/attachments",
                                params={"$select": ATTACHMENT_LIST_SELECT, "$top": "50"}, headers=await self._headers())
        r.raise_for_status()
        return r.json().get("value", [])

    async def _attachment_bytes(self, message_id: str, attachment_id: str, mailbox: str | None) -> bytes:
        """The raw bytes of one file attachment through ``/$value``, which works for any size (the JSON ``contentBytes``
        form is only reliable under ~3 MB)."""
        r = await self.http.get(f"{self._base(mailbox)}/messages/{message_id}/attachments/{attachment_id}/$value",
                                headers=await self._headers())
        r.raise_for_status()
        return r.content

    async def _fetch_attachments(self, message_id: str, mailbox: str | None, wanted, max_bytes: int,
                                 readable) -> list[dict[str, Any]]:
        """The attachments ``wanted(name, content_type)`` picks, as ``{"name", "data": base64}`` - or, for one that can't be
        fetched as file bytes, ``{"name", "problem": <code>, "size", "detail"}`` saying why (link, item, format,
        too_large, download_failed, empty). Nothing is silently dropped, so the owner can be told exactly what happened."""
        out: list[dict[str, Any]] = []
        for a in await self._attachment_items(message_id, mailbox):
            name = a.get("name") or ""
            if not wanted(name, a.get("contentType") or ""):
                continue
            if len(out) >= MAX_ATTACHMENTS_FETCHED:
                break
            size = int(a.get("size") or 0)
            kind = attachment_kind(a)
            if kind == "reference":
                out.append({"name": name, "problem": "link", "size": size})
            elif kind == "item":
                out.append({"name": name, "problem": "item", "size": size})
            elif not readable(name):
                out.append({"name": name, "problem": "format", "size": size})
            elif size > max_bytes:
                out.append({"name": name, "problem": "too_large", "size": size})
            else:
                try:
                    raw = await self._attachment_bytes(message_id, a.get("id") or "", mailbox)
                except Exception as e:  # noqa: BLE001 - one attachment failing mustn't hide the others
                    out.append({"name": name, "problem": "download_failed", "size": size,
                                "detail": describe_http_error(e)})
                    continue
                if not raw:
                    out.append({"name": name, "problem": "empty", "size": size})
                elif len(raw) > max_bytes:
                    out.append({"name": name, "problem": "too_large", "size": len(raw)})
                else:
                    out.append({"name": name, "data": base64.b64encode(raw).decode()})
        return out

    async def pdf_attachments(self, message_id: str, mailbox: str | None = None,
                              max_bytes: int = 15_000_000) -> list[dict[str, Any]]:
        """PDF attachments of a message as base64 (e.g. an answering service's call report or a customer's PO). Files of
        any size are fetched through /$value; one that can't be read as file bytes (a OneDrive / SharePoint link, an
        attached email, a download failure, over ``max_bytes``) is returned as an entry with a ``problem`` and no
        ``data``, so a caller that wants the bytes must skip entries without ``data``."""
        return await self._fetch_attachments(message_id, mailbox, is_pdf_attachment, max_bytes, lambda n: True)

    async def office_attachments(self, message_id: str, mailbox: str | None = None,
                                 max_bytes: int = 15_000_000) -> list[dict[str, Any]]:
        """Word (.docx), Excel (.xlsx) and PowerPoint (.pptx) attachments of a message as base64 - read-only, nothing is
        changed. Same contract as ``pdf_attachments`` for files that can't be read (including the old .doc / .xls / .ppt
        and macro-enabled formats, which are listed with a problem and never downloaded)."""
        return await self._fetch_attachments(message_id, mailbox, is_office_attachment, max_bytes,
                                             lambda n: n.strip().lower().endswith(OFFICE_EXTENSIONS))

    async def image_attachments(self, message_id: str, mailbox: str | None = None,
                                max_bytes: int = 15_000_000) -> list[dict[str, Any]]:
        """PNG / JPEG attachments of a message as base64 (a floor plan sent as a picture). Read-only; the same contract as
        ``pdf_attachments`` for a file that can't be read (an entry with a ``problem`` and no ``data``)."""
        return await self._fetch_attachments(message_id, mailbox, is_plan_image_attachment, max_bytes, lambda n: True)

    async def attachment_overview(self, message_id: str, mailbox: str | None = None) -> list[dict[str, Any]]:
        """Every attachment of a message, content not downloaded: name, size, kind (file / item / reference) and type. Used
        to tell the owner what an email DOES carry when there was nothing readable of the kind asked for."""
        return [{"name": a.get("name") or "", "size": int(a.get("size") or 0), "kind": attachment_kind(a),
                 "content_type": a.get("contentType") or "", "inline": bool(a.get("isInline"))}
                for a in await self._attachment_items(message_id, mailbox)]

    async def attachment_names(self, message_id: str, mailbox: str | None = None, limit: int = 20) -> list[str]:
        """File names of a message's attachments (names only - no content is downloaded)."""
        r = await self.http.get(f"{self._base(mailbox)}/messages/{message_id}/attachments",
                                params={"$select": "name,size,contentType", "$top": str(limit)},
                                headers=await self._headers())
        r.raise_for_status()
        return [str(a.get("name") or "") for a in r.json().get("value", []) if a.get("name")][:limit]

    async def mark_read(self, message_id: str) -> None:
        r = await self.http.patch(f"{self._mbx}/messages/{message_id}", json={"isRead": True},
                                  headers=await self._headers())
        r.raise_for_status()

    async def send_mail(self, to: list[str], subject: str, body_html: str, cc: list[str] | None = None,
                        bcc: list[str] | None = None, sensitivity: str | None = None) -> GuardedMessage:
        """Send via Graph. The management-only recipient rule (mail_guard) is enforced here for every caller."""
        g = guard_message(self.s, to, cc, bcc, sensitivity)
        message = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": a}} for a in g.to],
        }
        if g.cc:
            message["ccRecipients"] = [{"emailAddress": {"address": a}} for a in g.cc]
        if g.bcc:
            message["bccRecipients"] = [{"emailAddress": {"address": a}} for a in g.bcc]
        r = await self.http.post(f"{self._mbx}/sendMail", json={"message": message, "saveToSentItems": True},
                                 headers=await self._headers())
        r.raise_for_status()
        return g

    # -- Jarvis's own emails to the owner: filed into a dedicated Outlook folder ------------
    async def _folder_pages(self, url: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        params: dict[str, str] | None = {"$top": "100", "$select": "id,displayName"}
        for _ in range(5):  # a mailbox has a handful of folders; don't page forever
            r = await self.http.get(url, params=params, headers=await self._headers())
            r.raise_for_status()
            data = r.json()
            out.extend(data.get("value", []))
            url = data.get("@odata.nextLink") or ""
            if not url:
                break
            params = None  # the next link already carries the query
        return out

    async def owner_folder_id(self, name: str) -> str | None:
        """Id of the mail folder with this display name (case-insensitive), or None if there isn't one.

        Looks at the top-level folders, then the Inbox's sub-folders. The id is cached; a miss is also cached for
        a few minutes so a missing folder doesn't cost extra Graph calls on every email. Graph errors propagate
        (and are not cached)."""
        wanted = name.strip().lower()
        if wanted in self._folder_ids:
            return self._folder_ids[wanted]
        if self._folder_missing_until.get(wanted, 0.0) > time.monotonic():
            return None
        for url in (f"{self._mbx}/mailFolders", f"{self._mbx}/mailFolders/inbox/childFolders"):
            for f in await self._folder_pages(url):
                if (f.get("displayName") or "").strip().lower() == wanted and f.get("id"):
                    self._folder_ids[wanted] = f["id"]
                    return f["id"]
        self._folder_missing_until[wanted] = time.monotonic() + FOLDER_MISS_TTL_S
        return None

    async def _file_into_folder(self, folder_id: str, subject: str, sent_at: datetime) -> bool:
        """Move the just-delivered Inbox copy of `subject` into the folder. Delivery is asynchronous, so retry
        briefly until it turns up. Moves (not copies), so nothing is left behind in the Inbox."""
        since = (sent_at - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for delay in (0.0, *self.owner_folder_retry_delays):
            if delay:
                await asyncio.sleep(delay)
            r = await self.http.get(
                f"{self._mbx}/mailFolders/inbox/messages", headers=await self._headers(),
                params={"$top": "15", "$select": "id,subject,receivedDateTime",
                        "$filter": f"receivedDateTime ge {since}", "$orderby": "receivedDateTime desc"})
            r.raise_for_status()
            hits = [m for m in r.json().get("value", []) if m.get("subject") == subject and m.get("id")]
            if hits:
                for m in hits:  # identical subject sent twice in the same window: they're all ours
                    mv = await self.http.post(f"{self._mbx}/messages/{m['id']}/move",
                                              json={"destinationId": folder_id}, headers=await self._headers())
                    if mv.status_code == 404:
                        self._folder_ids.clear()  # folder deleted/renamed since we cached it
                    mv.raise_for_status()
                return True
        return False

    async def send_to_owner(self, to: str, subject: str, body_html: str) -> tuple[bool, str]:
        """Email the owner and file it straight into the owner's Jarvis folder instead of the Inbox.

        Sends normally (so it is always delivered), then moves the delivered copy out of the Inbox - creating the
        message in the folder via POST /mailFolders/{id}/messages would only make an unsent *draft*, not a received
        message. Returns (filed, warning): filed=False means it is sitting in the Inbox, and `warning` says why
        (empty when filing wasn't wanted). Only a failure of the send itself raises, exactly as send_mail does;
        nothing in the folder step can stop or lose the message."""
        sent_at = datetime.now(timezone.utc)
        await self.send_mail([to], subject, body_html)
        name = (self.s.owner_mail_folder or "").strip()
        if not name:
            return False, ""
        if to.strip().lower() != (self.s.ms_mailbox or "").strip().lower():
            return False, ""  # delivered to somebody else's mailbox, which Jarvis's mailbox can't file into
        try:
            folder_id = await self.owner_folder_id(name)
            if not folder_id:
                msg = f"Outlook folder '{name}' not found - the email was left in the Inbox"
                log.warning(msg)
                return False, msg
            if await self._file_into_folder(folder_id, subject, sent_at):
                return True, ""
            msg = f"Email not seen in the Inbox in time to file into '{name}' - it stays in the Inbox"
        except Exception as e:  # noqa: BLE001 - the message is already sent; never fail the caller over filing
            msg = f"Couldn't file the email into '{name}' ({e}) - it stays in the Inbox"
        log.warning(msg)
        return False, msg

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

    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings
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
            {"id": "demo-5", "subject": "Out of hours call report (DEMO)", "from_name": "Night Answering Service",
             "from_email": "reports@example-answering.co.uk", "received": (now - timedelta(hours=9)).isoformat(),
             "is_read": False, "importance": "normal", "has_attachments": False, "link": "",
             "preview": "2 calls taken overnight for Salts Fire and Security.",
             "body": "Calls taken 17:30-08:00:\n\n1) 22:14 - Aire Valley Care Home (Aire Valley Care Ltd). Caller: night "
                     "manager. Fire panel showing fault on zone 3, buzzer silenced. Advised to monitor, engineer to "
                     "call back in the morning.\n\n2) 03:40 - Riverside Mill Apartments (Pennine Housing). Caller: "
                     "resident. Smoke alarm sounding in the corridor, no fire. On-call engineer Tom Wilkinson "
                     "attended 04:30 and reset the system; faulty detector to be replaced."},
            {"id": "demo-4", "subject": "[ISSUE] Can't attach photos to job sheet (DEMO)", "from_name": "Field Engineer",
             "from_email": "engineer@example.co.uk", "received": (now - timedelta(hours=7)).isoformat(),
             "is_read": False, "importance": "normal", "has_attachments": False, "link": "",
             "preview": "When I try to add photos to a job sheet on my phone it spins forever.",
             "body": "When I try to add photos to a job sheet in Salts FSM on my phone the upload spinner never "
                     "finishes. Happens on every job today."},
        ]

    async def list_messages(self, unread_only: bool = False, top: int = 15, since_hours: int | None = None,
                            folder: str = "inbox", mailbox: str | None = None) -> list[dict[str, Any]]:
        msgs = [m for m in self._messages if not unread_only or not m["is_read"]]
        return [{k: v for k, v in m.items() if k != "body"} for m in msgs[:top]]

    async def attachment_names(self, message_id: str, mailbox: str | None = None, limit: int = 20) -> list[str]:
        return []

    async def search_messages(self, query: str, top: int = 15, mailbox: str | None = None) -> list[dict[str, Any]]:
        q = query.lower()
        hits = [m for m in self._messages if q in (m["subject"] + m["body"]).lower()]
        return [{k: v for k, v in m.items() if k != "body"} for m in hits[:top]]

    async def get_message(self, message_id: str, mailbox: str | None = None) -> dict[str, Any]:
        for m in self._messages:
            if m["id"] == message_id:
                return {**m, "to": [], "cc": []}
        raise KeyError(f"No message {message_id}")

    async def pdf_attachments(self, message_id: str, mailbox: str | None = None,
                              max_bytes: int = 15_000_000) -> list[dict[str, Any]]:
        return []

    async def office_attachments(self, message_id: str, mailbox: str | None = None,
                                 max_bytes: int = 15_000_000) -> list[dict[str, Any]]:
        return []

    async def image_attachments(self, message_id: str, mailbox: str | None = None,
                                max_bytes: int = 15_000_000) -> list[dict[str, Any]]:
        return []

    async def attachment_overview(self, message_id: str, mailbox: str | None = None) -> list[dict[str, Any]]:
        return []

    async def mark_read(self, message_id: str) -> None:
        for m in self._messages:
            if m["id"] == message_id:
                m["is_read"] = True

    async def send_mail(self, to: list[str], subject: str, body_html: str, cc: list[str] | None = None,
                        bcc: list[str] | None = None, sensitivity: str | None = None) -> GuardedMessage:
        # Same guard as the real mailbox, so the rule is exercised (and logged) in demo mode too.
        g = guard_message(self.s, to, cc, bcc, sensitivity) if self.s is not None else GuardedMessage(
            list(to), list(cc or []), list(bcc or []), sensitive=False)
        log.info("[DEMO] would send email to %s: %s", g.to, subject)
        return g

    async def send_to_owner(self, to: str, subject: str, body_html: str) -> tuple[bool, str]:
        await self.send_mail([to], subject, body_html)
        return False, ""

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


class TeamsDeliveryError(RuntimeError):
    """A Teams webhook post failed. The message never contains the webhook URL."""


class TeamsNotifier:
    """Posts to a Teams channel through a Workflows ("When a Teams webhook request is received") URL."""

    def __init__(self, webhook_url: str, http: httpx.AsyncClient):
        self.url = webhook_url  # a secret: the trigger URL carries its own access signature (sig=)
        self.http = http

    def __repr__(self) -> str:  # never put the webhook URL in a log line or traceback by accident
        return f"TeamsNotifier(enabled={self.enabled})"

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
        try:
            r = await self.http.post(self.url, json=card)
            r.raise_for_status()
        except httpx.HTTPError as e:
            # httpx's own message for these contains the full request URL - i.e. the webhook signature.
            raise TeamsDeliveryError(f"Teams webhook failed: {describe_http_error(e)}") from None


def text_to_html(text: str) -> str:
    return "<div style='font-family:Segoe UI,Arial,sans-serif'>" + html.escape(text).replace("\n", "<br>") + "</div>"
