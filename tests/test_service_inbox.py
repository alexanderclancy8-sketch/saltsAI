"""The second shared mailbox (service@): reading it through Graph (mocked), the Settings > Service inbox section and its Test
button, the mail tools' `mailbox` choice, the Comms drawer's labelling, and the safety rules around all of it (owner-only
configuration, no model-chosen address, the team role kept out, nothing sent)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jarvis import access, auth
from jarvis.brain import tools
from jarvis.brain.tools import (TOOLS_BY_NAME, AttachmentReadIn, InboxIn, MessageIn, PdfReadIn, SearchIn, dispatch)
from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.integrations import microsoft365 as m365
from jarvis.integrations.microsoft365 import GraphMail, mailbox_for
from jarvis.main import create_app
from jarvis.services import connection_tests
from jarvis.services.service_inbox import explain_graph_error, label_for
from jarvis.settings_store import FIELDS, OWNER_ONLY_KEYS, SECTIONS_BY_ID, SettingsStore
from tests.fakes import FakeClient

OWNER_BOX = "alex@example.co.uk"
SERVICE = "service@example.co.uk"
SECRET = "Zq9~very-secret-client-value-1234567890"


class FakeMsalApp:
    def __init__(self, *a, **k):
        pass

    def acquire_token_for_client(self, scopes):
        return {"access_token": "tok"}


def message(id_, subject="Work order WO-1001", sender="noreply@bradford.gov.uk", read=False):
    return {"id": id_, "subject": subject, "from": {"emailAddress": {"name": "Bradford Council", "address": sender}},
            "receivedDateTime": "2026-10-05T09:30:00Z", "isRead": read, "importance": "normal",
            "bodyPreview": "A new request", "hasAttachments": True, "webLink": "https://outlook.example/x"}


class FakeGraph:
    """A scripted tenant with two mailboxes. Records every request so a test can see which mailbox was touched."""

    def __init__(self, status=200, error_code="", error_message="", inbox=None):
        self.status = status
        self.error_code = error_code
        self.error_message = error_message
        self.inbox = inbox if inbox is not None else [message("S1")]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if f"/users/{SERVICE}/" in path:
            if self.status != 200:
                return httpx.Response(self.status, json={"error": {"code": self.error_code,
                                                                  "message": self.error_message}})
            if path.endswith("/attachments"):
                return httpx.Response(200, json={"value": [{"name": "work-order.pdf", "size": 10},
                                                           {"name": "site-plan.docx", "size": 20}]})
            if "/messages/" in path:
                return httpx.Response(200, json={**message("S1"), "body": {"content": "Hello"}, "toRecipients": [],
                                                 "ccRecipients": []})
            return httpx.Response(200, json={"value": self.inbox})
        if f"/users/{OWNER_BOX}/" in path:
            return httpx.Response(200, json={"value": [message("O1", "Owner mail", "x@y.example")]})
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def build(settings, monkeypatch, graph, **extra):
    monkeypatch.setattr(m365.msal, "ConfidentialClientApplication", FakeMsalApp)
    settings.ms_tenant_id, settings.ms_client_id, settings.ms_client_secret = "t", "c", SECRET
    settings.ms_mailbox = OWNER_BOX
    settings.owner_email = OWNER_BOX
    settings.service_inbox = SERVICE
    for k, v in extra.items():
        setattr(settings, k, v)
    http = httpx.AsyncClient(transport=httpx.MockTransport(graph))
    return Jarvis(settings, http=http, client=FakeClient())


# --------------------------------------------------------------------------- multi-mailbox read (Graph mocked)
async def test_graph_reads_the_requested_mailbox_and_the_default_is_unchanged(settings, monkeypatch):
    g = FakeGraph()
    j = build(settings, monkeypatch, g)
    assert isinstance(j.mail, GraphMail)

    own = await j.mail.list_messages(unread_only=True, top=5)
    assert [m["id"] for m in own] == ["O1"] and g.paths()[-1] == f"/v1.0/users/{OWNER_BOX}/mailFolders/inbox/messages"

    svc = await j.mail.list_messages(unread_only=True, top=5, mailbox=SERVICE)
    assert [m["id"] for m in svc] == ["S1"] and g.paths()[-1] == f"/v1.0/users/{SERVICE}/mailFolders/inbox/messages"
    assert g.requests[-1].url.params["$filter"] == "isRead eq false"
    assert (await j.mail.get_message("S1", mailbox=SERVICE))["body"] == "Hello"
    assert g.paths()[-1] == f"/v1.0/users/{SERVICE}/messages/S1"
    assert await j.mail.attachment_names("S1", mailbox=SERVICE) == ["work-order.pdf", "site-plan.docx"]
    await j.mail.search_messages("WO-1001", mailbox=SERVICE)
    assert g.paths()[-1] == f"/v1.0/users/{SERVICE}/messages"
    await j.mail.search_messages("anything")
    assert g.paths()[-1] == f"/v1.0/users/{OWNER_BOX}/messages"  # no mailbox given: the owner's, as always
    await j.http.aclose()


async def test_only_one_plain_address_can_go_into_a_graph_url(settings, monkeypatch):
    g = FakeGraph()
    j = build(settings, monkeypatch, g)
    for bad in ("service@example.co.uk/../alex@example.co.uk", "a@b.co?$x=1", "not an address", "x@y.co#frag",
                "a@b.co,c@d.co"):
        with pytest.raises(ValueError):
            await j.mail.list_messages(mailbox=bad)
    assert g.requests == []
    await j.http.aclose()


def test_mailbox_for_only_ever_resolves_the_two_words(settings):
    settings.service_inbox = "Service@Example.co.uk"
    assert mailbox_for(settings, None) == (None, "") and mailbox_for(settings, "owner") == (None, "")
    assert mailbox_for(settings, "service") == ("service@example.co.uk", "")  # the saved address, lower-cased
    address, err = mailbox_for(settings, "alex@elsewhere.example")  # an address is never accepted
    assert address is None and "'owner' or 'service'" in err
    settings.service_inbox = ""
    address, err = mailbox_for(settings, "service")
    assert address is None and "isn't set up" in err


# --------------------------------------------------------------------------- the mail tools
class RecordingMail:
    demo = False

    def __init__(self):
        self.calls: list[tuple] = []

    async def list_messages(self, *a, **kw):
        self.calls.append(("list", a, kw))
        return [{"id": "x", "subject": "s"}]

    async def search_messages(self, *a, **kw):
        self.calls.append(("search", a, kw))
        return []

    async def get_message(self, *a, **kw):
        self.calls.append(("get", a, kw))
        return {"id": "x", "body": "b"}

    async def office_attachments(self, *a, **kw):
        self.calls.append(("office", a, kw))
        return []

    async def pdf_attachments(self, *a, **kw):
        self.calls.append(("pdf", a, kw))
        return []


async def test_mail_tools_default_to_the_owners_mailbox_with_the_old_call_shape(settings):
    j = Jarvis(settings, client=FakeClient())
    j.mail = RecordingMail()
    j.documents.j = j
    settings.service_inbox = SERVICE
    out = await tools.email_inbox(j, InboxIn())
    assert out == {"demo": False, "emails": [{"id": "x", "subject": "s"}]}  # no new keys for the owner's own mailbox
    await tools.email_search(j, SearchIn(query="q"))
    await tools.email_read(j, MessageIn(message_id="m"))
    await tools.email_attachment_read(j, AttachmentReadIn(message_id="m"))
    await tools.email_pdf_read(j, PdfReadIn(message_id="m"))
    assert [c[0] for c in j.mail.calls] == ["list", "search", "get", "office", "pdf"]
    assert all("mailbox" not in kw for _, _, kw in j.mail.calls)  # exactly the calls made before this feature existed
    await j.http.aclose()


async def test_mail_tools_read_the_service_inbox_when_asked(settings):
    j = Jarvis(settings, client=FakeClient())
    j.mail = RecordingMail()
    j.documents.j = j
    settings.service_inbox = SERVICE
    inbox = await tools.email_inbox(j, InboxIn(mailbox="service", unread_only=False, limit=3))
    assert inbox["mailbox"] == "service" and inbox["emails"]
    await tools.email_search(j, SearchIn(query="WO-1", mailbox="service"))
    read = await tools.email_read(j, MessageIn(message_id="m", mailbox="service"))
    assert read["mailbox"] == "service"
    await tools.email_attachment_read(j, AttachmentReadIn(message_id="m", mailbox="service"))
    await tools.email_pdf_read(j, PdfReadIn(message_id="m", mailbox="service"))
    assert [c[2].get("mailbox") for c in j.mail.calls] == [SERVICE] * 5
    await j.http.aclose()


async def test_service_mailbox_tools_say_so_when_it_isnt_set_up_or_the_mail_is_demo(settings):
    j = Jarvis(settings, client=FakeClient())  # no Microsoft 365: demo mail
    assert j.mail.demo
    settings.service_inbox = ""
    out = await tools.email_inbox(j, InboxIn(mailbox="service"))
    assert "isn't set up" in out["error"]
    settings.service_inbox = SERVICE
    out = await tools.email_inbox(j, InboxIn(mailbox="service"))
    assert out["demo"] is True and out["emails"] == [] and "isn't connected" in out["note"]
    # the owner's own (demo) inbox is untouched
    assert (await tools.email_inbox(j, InboxIn()))["emails"]
    await j.http.aclose()


def test_a_model_cannot_name_a_mailbox_address_in_a_tool_call():
    for model in (InboxIn, SearchIn, MessageIn, AttachmentReadIn, PdfReadIn):
        with pytest.raises(ValidationError):
            model(**{"mailbox": "ceo@elsewhere.example", **({"query": "q"} if model is SearchIn else
                                                           {"message_id": "m"} if model is not InboxIn else {})})
    schema = TOOLS_BY_NAME["email_inbox"].model.model_json_schema()
    assert schema["properties"]["mailbox"]["enum"] == ["owner", "service"]


def test_no_tool_can_change_the_service_inbox_setting():
    """The setting is only reachable through the owner's Settings page: no tool takes it or any council setting."""
    keys = {f.key for f in SECTIONS_BY_ID["serviceinbox"].fields}
    for tool in TOOLS_BY_NAME.values():
        assert not keys & set(tool.model.model_json_schema().get("properties", {})), tool.name
    assert not [n for n in TOOLS_BY_NAME if "service_inbox" in n or "council" in n]


# --------------------------------------------------------------------------- team role
def test_the_team_role_gets_no_mail_tools_at_all():
    team = access.Caller(role=access.TEAM, name="Pat")
    for name in ("email_inbox", "email_search", "email_read", "email_attachment_read", "email_pdf_read",
                 "email_draft_reply", "email_send"):
        assert not access.tool_allowed(name, team), name
    assert not any(n.startswith("email") for n in access.TEAM_TOOLS)
    assert access.tool_allowed("email_inbox", None)  # the owner's own conversation: as before


async def test_a_team_dispatch_of_the_service_inbox_is_refused_and_reads_nothing(settings):
    j = Jarvis(settings, client=FakeClient())
    j.mail = RecordingMail()
    settings.service_inbox = SERVICE
    tool = TOOLS_BY_NAME["email_inbox"]
    token = access.current_caller.set(access.Caller(role=access.TEAM, name="Pat", sid="s1"))
    try:
        out = await dispatch(j, tool, tool.model(mailbox="service"))
    finally:
        access.current_caller.reset(token)
    assert "isn't available to you" in str(out) and j.mail.calls == []
    await j.http.aclose()


def test_a_team_console_status_carries_no_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.main.LOGIN_DELAY_S", 0)
    s = Settings(data_dir=tmp_path / "data", scheduler_enabled=False, _env_file=None, anthropic_api_key="test",
                 service_inbox=SERVICE, jarvis_owner_password="owner-pass-1234")
    j = Jarvis(s, client=FakeClient())
    assert "inbox" not in access.TEAM_STATUS_KEYS
    j.team_access.set_code("a-long-team-code")
    app = create_app(s, j)
    with TestClient(app) as c:
        r = c.post("/login/team", data={"name": "Pat", "code": "a-long-team-code"}, follow_redirects=False)
        assert r.status_code == 303
        status = c.get("/api/status").json()
        assert status["role"] == "team" and "inbox" not in status and SERVICE not in json.dumps(status)


# --------------------------------------------------------------------------- settings
def test_the_service_inbox_section_exists_and_every_field_is_owner_only():
    sec = SECTIONS_BY_ID["serviceinbox"]
    assert sec.test is True and "service_inbox" in sec.required
    keys = {f.key for f in sec.fields}
    assert {"service_inbox", "council_intake_enabled", "council_sender_patterns", "council_subject_patterns",
            "council_customer_name"} == keys
    assert keys <= OWNER_ONLY_KEYS
    assert SECTIONS_BY_ID["serviceinbox"].fields[0].kind == "email"
    assert FIELDS["service_inbox"].kind == "email"
    assert Settings(_env_file=None).service_inbox == ""  # off by default


def test_the_service_inbox_setting_validates_and_applies(settings):
    store = SettingsStore(settings)
    assert store.update({"service_inbox": "not an email"}, []) == {"service_inbox": "That doesn't look like an email address."}
    assert store.update({"council_sender_patterns": "bradford.gov.uk, https://evil/x"}, [])  # not a domain/address
    assert store.update({"service_inbox": "Service@Example.co.uk",
                         "council_sender_patterns": " bradford.gov.uk , portal@vendor.example ",
                         "council_subject_patterns": "work order, repair request"}, []) == {}
    assert settings.service_inbox == "service@example.co.uk"
    assert settings.council_sender_patterns == "bradford.gov.uk,portal@vendor.example"
    assert settings.council_subject_patterns == "work order,repair request"
    assert "service@example.co.uk" in settings.shared_mailbox_entries  # management emails are never sent there either
    store.update({}, ["service_inbox"])
    assert settings.service_inbox == ""


def settings_app(tmp_path, **kw):
    s = Settings(data_dir=tmp_path / "data", scheduler_enabled=False, _env_file=None, anthropic_api_key="test", **kw)
    return s, Jarvis(s, client=FakeClient())


def sso(who):
    return {"x-ms-client-principal-idp": "aad", "x-ms-client-principal-name": who}


def test_only_the_owner_can_set_the_service_inbox_through_the_settings_api(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "true")
    owner, manager = "alex@salts.example.com", "sam@salts.example.com"
    s, j = settings_app(tmp_path, owner_email=owner, manager_emails=f"{owner},{manager}",
                        jarvis_owner_password="a-long-password")
    app = create_app(s, j)
    with TestClient(app) as c:
        for body in ({"values": {"service_inbox": SERVICE}}, {"values": {"council_intake_enabled": False}},
                     {"values": {"council_sender_patterns": "evil.example"}}, {"values": {}, "clear": ["service_inbox"]},
                     {"values": {"council_subject_patterns": "x"}}, {"values": {"council_customer_name": "x"}}):
            assert c.post("/api/settings", json=body, headers=sso(manager)).status_code == 403, body
        assert s.service_inbox == "" and s.council_sender_patterns == "bradford.gov.uk"
        assert c.post("/api/settings", json={"values": {"service_inbox": SERVICE}}).status_code == 401
        r = c.post("/api/settings", json={"values": {"service_inbox": SERVICE}}, headers=sso(owner))
        assert r.status_code == 200 and s.service_inbox == SERVICE
        page = c.get("/api/settings", headers=sso(owner)).json()
        section = next(x for x in page["sections"] if x["id"] == "serviceinbox")
        assert section["test"] is True and section["configured"] is True and section["guide"]
        assert any("Application Access Policy" in step for step in section["guide"])


# --------------------------------------------------------------------------- the Test button
async def run_test(j):
    return await connection_tests.run(j, "serviceinbox")


async def test_connection_test_success_reads_one_header(settings, monkeypatch):
    g = FakeGraph()
    j = build(settings, monkeypatch, g)
    ok, detail = await run_test(j)
    assert ok and SERVICE in detail and "works" in detail and "noreply@bradford.gov.uk" in detail
    assert g.requests[-1].url.params["$top"] == "1" and len(g.requests) == 1
    assert g.requests[-1].method == "GET"
    await j.http.aclose()


async def test_connection_test_on_an_empty_inbox_is_still_a_success(settings, monkeypatch):
    j = build(settings, monkeypatch, FakeGraph(inbox=[]))
    ok, detail = await run_test(j)
    assert ok and "empty" in detail
    await j.http.aclose()


@pytest.mark.parametrize("status, code, expect", [
    (403, "ErrorAccessDenied", ["no permission on this mailbox", "Mail.Read", "application permission",
                                "admin consent", "Application Access Policy", SERVICE]),
    (403, "", ["Application Access Policy"]),
    (401, "InvalidAuthenticationToken", ["sign-in", "client secret"]),
    (404, "ErrorInvalidUser", ["can't find a mailbox", SERVICE]),
    (404, "MailboxNotEnabledForRESTAPI", ["isn't an active Exchange Online mailbox"]),
    (429, "TooManyRequests", ["busy"]),
    (503, "ServiceNotAvailable", ["busy or unavailable"]),
    (400, "SomethingNew", ["HTTP 400", "SomethingNew"]),
])
async def test_connection_test_maps_graph_errors_to_a_plain_fix(settings, monkeypatch, status, code, expect):
    # Graph's own message deliberately carries the client secret: it must never reach the owner's screen.
    g = FakeGraph(status=status, error_code=code, error_message=f"denied for client secret {SECRET}")
    j = build(settings, monkeypatch, g)
    ok, detail = await run_test(j)
    assert ok is False
    for text in expect:
        assert text.lower() in detail.lower(), (text, detail)
    assert SECRET not in detail and "tok" not in detail.split()
    stored = SettingsStore(settings).record_test(j.db, "serviceinbox", ok, detail)
    assert SECRET not in json.dumps(stored)
    await j.http.aclose()


def test_the_403_message_is_the_documented_fix():
    e = httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://g/x"),
                              response=httpx.Response(403, json={"error": {"code": "ErrorAccessDenied", "message": "m"}}))
    text = explain_graph_error(e, "service@x.co.uk")
    assert ("the Azure app registration has no permission on this mailbox. It needs a Microsoft Graph Mail.Read or "
            "Mail.ReadWrite APPLICATION permission with admin consent") in text
    assert "service@x.co.uk must be added to it" in text


async def test_connection_test_graph_sign_in_failure_is_explained(settings, monkeypatch):
    class BadApp(FakeMsalApp):
        def acquire_token_for_client(self, scopes):
            return {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."}

    j = build(settings, monkeypatch, FakeGraph())
    monkeypatch.setattr(j.mail, "_app", BadApp())
    ok, detail = await run_test(j)
    assert not ok and "couldn't sign in" in detail and "Microsoft 365" in detail and SECRET not in detail
    await j.http.aclose()


async def test_connection_test_without_an_address_or_without_microsoft_365(settings, monkeypatch):
    j = Jarvis(settings, client=FakeClient())  # demo mail
    ok, detail = await run_test(j)
    assert not ok and "address" in detail
    settings.service_inbox = SERVICE
    ok, detail = await run_test(j)
    assert not ok and "Connect Microsoft 365 first" in detail
    await j.http.aclose()


def test_the_test_button_route_is_registered_and_is_the_settings_test_route(tmp_path):
    s, j = settings_app(tmp_path, service_inbox=SERVICE)
    app = create_app(s, j)
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, auth.make_session(s))
        r = c.post("/api/settings/test/serviceinbox")
        assert r.status_code == 200 and r.json()["ok"] is False and "Connect Microsoft 365" in r.json()["detail"]


# --------------------------------------------------------------------------- Comms drawer data
async def test_comms_labels_the_service_inbox_and_keeps_the_owners_apart(settings, monkeypatch):
    g = FakeGraph(inbox=[message("S1", "Work order WO-1"), message("S2", "Work order WO-2")])
    j = build(settings, monkeypatch, g)
    svc = await j.service_inbox.unread()
    assert svc["enabled"] and svc["label"] == "service@" and svc["address"] == SERVICE
    assert [m["id"] for m in svc["unread"]] == ["S1", "S2"] and "error" not in svc
    assert label_for("Service@x.co.uk") == "service@" and label_for("") == "service@"
    own = await j.briefings.status()  # the owner's inbox, untouched by the second mailbox
    assert [m["id"] for m in own["inbox"]["unread"]] == ["O1"] and "service" not in own["inbox"]
    await j.http.aclose()


async def test_comms_service_inbox_off_demo_and_error_states(settings, monkeypatch):
    j = Jarvis(settings, client=FakeClient())
    assert await j.service_inbox.unread() == {"enabled": False}
    settings.service_inbox = SERVICE
    demo = await j.service_inbox.unread()
    assert demo["enabled"] and demo["demo"] and demo["unread"] == []
    await j.http.aclose()

    j = build(Settings(data_dir=settings.data_dir / "b", scheduler_enabled=False, _env_file=None, anthropic_api_key="t"),
              monkeypatch, FakeGraph(status=403, error_code="ErrorAccessDenied", error_message="x"))
    out = await j.service_inbox.unread()
    assert out["unread"] == [] and "Application Access Policy" in out["error"] and out["label"] == "service@"
    await j.http.aclose()


async def test_comms_unread_is_cached_briefly_but_errors_are_not(settings, monkeypatch):
    g = FakeGraph()
    j = build(settings, monkeypatch, g)
    await j.service_inbox.unread()
    n = len(g.requests)
    await j.service_inbox.unread()
    assert len(g.requests) == n  # the console polls often: one Graph read per half minute, not per poll
    await j.http.aclose()


def test_the_status_endpoint_carries_the_labelled_service_inbox(tmp_path):
    s, j = settings_app(tmp_path, service_inbox=SERVICE)
    j.service_inbox.unread = _async_value({"enabled": True, "label": "service@", "address": SERVICE, "demo": False,
                                           "unread": [{"id": "S1", "subject": "Work order WO-9"}]})
    app = create_app(s, j)
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, auth.make_session(s))
        inbox = c.get("/api/status").json()["inbox"]
        assert inbox["service"]["label"] == "service@" and inbox["service"]["unread"][0]["id"] == "S1"
        assert "unread" in inbox  # the owner's own list is still there, separately
        assert "Service inbox (service@)" in c.get("/api/status").json()["connections"]


def _async_value(value):
    async def fn(*a, **kw):
        return value

    return fn


async def test_connections_line_never_adds_to_the_sample_data_count(settings, monkeypatch):
    j = Jarvis(settings, client=FakeClient())
    assert "off" in j.connections()["Service inbox (service@)"]
    settings.service_inbox = SERVICE
    assert "DEMO" not in j.connections()["Service inbox (service@)"]
    await j.http.aclose()


# --------------------------------------------------------------------------- read-only
async def test_reading_the_service_inbox_only_ever_issues_get_requests(settings, monkeypatch):
    g = FakeGraph()
    j = build(settings, monkeypatch, g)
    await j.service_inbox.unread()
    await run_test(j)
    await j.mail.list_messages(mailbox=SERVICE)
    await j.mail.get_message("S1", mailbox=SERVICE)
    await j.mail.attachment_names("S1", mailbox=SERVICE)
    assert {r.method for r in g.requests} == {"GET"}
    await j.http.aclose()


def test_the_route_table_gained_no_route_for_the_service_inbox():
    """No new HTTP route was added for this feature: the Test button is the existing /api/settings/test/{section}, the
    drawer rides /api/status, and both are already classified in access.ROUTE_POLICY."""
    assert access.ROUTE_POLICY["POST /api/settings/test/{section}"] == access.MANAGER_OK
    assert access.ROUTE_POLICY["GET /api/status"] == access.TEAM_OK
    assert not [k for k in access.ROUTE_POLICY if "service" in k.lower() or "council" in k.lower()]
    assert SimpleNamespace  # (keeps the import used if the file is trimmed)
