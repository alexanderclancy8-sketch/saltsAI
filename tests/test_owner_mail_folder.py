"""Jarvis's own emails to the owner are filed into the "salts jarvis" Outlook folder (Graph calls mocked)."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from jarvis.integrations import microsoft365 as m365
from jarvis.integrations.microsoft365 import GraphMail
from jarvis.services.notifier import Notifier

MAILBOX = "alex@example.co.uk"
SUBJECT = "[Jarvis] Morning briefing"


class FakeMsalApp:
    def __init__(self, *a, **k):
        pass

    def acquire_token_for_client(self, scopes):
        return {"access_token": "tok"}


class FakeGraph:
    """Records calls and plays a scripted mailbox."""

    def __init__(self, folders=None, inbox=None, send_status=202, list_folders_status=200, move_status=201):
        self.folders = folders if folders is not None else [{"id": "F1", "displayName": "Salts Jarvis"}]
        self.child_folders: list[dict] = []
        self.inbox = inbox if inbox is not None else [[{"id": "M1", "subject": SUBJECT}]]  # one list per poll
        self.send_status = send_status
        self.list_folders_status = list_folders_status
        self.move_status = move_status
        self.calls: list[tuple[str, str]] = []
        self.polls = 0
        self.moves: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path.endswith("/sendMail"):
            return httpx.Response(self.send_status)
        if path.endswith("/mailFolders/inbox/childFolders"):
            return httpx.Response(200, json={"value": self.child_folders})
        if path.endswith("/mailFolders"):
            return httpx.Response(self.list_folders_status, json={"value": self.folders})
        if path.endswith("/mailFolders/inbox/messages"):
            batch = self.inbox[min(self.polls, len(self.inbox) - 1)]
            self.polls += 1
            return httpx.Response(200, json={"value": batch})
        if path.endswith("/move"):
            import json

            self.moves.append((path.split("/")[-2], json.loads(request.content)["destinationId"]))
            return httpx.Response(self.move_status, json={"id": "M1-moved"})
        return httpx.Response(404)

    def count(self, suffix: str) -> int:
        return sum(1 for _, p in self.calls if p.endswith(suffix))


@pytest.fixture
def make_mail(settings, monkeypatch):
    monkeypatch.setattr(m365.msal, "ConfidentialClientApplication", FakeMsalApp)
    settings.ms_mailbox = MAILBOX
    settings.owner_email = MAILBOX

    def build(graph: FakeGraph) -> GraphMail:
        mail = GraphMail(settings, httpx.AsyncClient(transport=httpx.MockTransport(graph)))
        mail.owner_folder_retry_delays = (0.0, 0.0, 0.0)  # no real waiting in tests
        return mail

    return build


async def test_folder_name_is_configurable_and_defaults_to_salts_jarvis(settings):
    assert settings.owner_mail_folder == "salts jarvis"


async def test_sends_then_moves_into_folder_case_insensitively(make_mail):
    g = FakeGraph()
    mail = make_mail(g)
    filed, warning = await mail.send_to_owner(MAILBOX, SUBJECT, "<p>hi</p>")
    assert (filed, warning) == (True, "")
    assert g.moves == [("M1", "F1")]
    assert g.calls[0] == ("POST", f"/v1.0/users/{MAILBOX}/sendMail")  # sent normally first


async def test_folder_id_is_cached(make_mail):
    g = FakeGraph()
    mail = make_mail(g)
    await mail.send_to_owner(MAILBOX, SUBJECT, "a")
    await mail.send_to_owner(MAILBOX, SUBJECT, "b")
    assert g.count("/mailFolders") == 1
    assert len(g.moves) == 2


async def test_finds_folder_under_inbox(make_mail):
    g = FakeGraph(folders=[{"id": "X", "displayName": "Other"}])
    g.child_folders = [{"id": "F2", "displayName": "SALTS JARVIS"}]
    filed, _ = await make_mail(g).send_to_owner(MAILBOX, SUBJECT, "a")
    assert filed and g.moves == [("M1", "F2")]


async def test_retries_until_message_arrives(make_mail):
    g = FakeGraph(inbox=[[], [], [{"id": "M9", "subject": SUBJECT}]])
    filed, _ = await make_mail(g).send_to_owner(MAILBOX, SUBJECT, "a")
    assert filed and g.polls == 3 and g.moves == [("M9", "F1")]


async def test_only_moves_the_matching_message(make_mail):
    g = FakeGraph(inbox=[[{"id": "OTHER", "subject": "Quote request"}, {"id": "M1", "subject": SUBJECT}]])
    await make_mail(g).send_to_owner(MAILBOX, SUBJECT, "a")
    assert g.moves == [("M1", "F1")]


async def test_message_never_arrives_stays_in_inbox_with_warning(make_mail):
    g = FakeGraph(inbox=[[]])
    filed, warning = await make_mail(g).send_to_owner(MAILBOX, SUBJECT, "a")
    assert not filed and "stays in the Inbox" in warning
    assert g.polls == 4 and not g.moves


async def test_folder_not_found_falls_back_to_inbox_and_caches_miss(make_mail):
    g = FakeGraph(folders=[{"id": "X", "displayName": "Other"}])
    mail = make_mail(g)
    filed, warning = await mail.send_to_owner(MAILBOX, SUBJECT, "a")
    assert not filed and "not found" in warning and "'salts jarvis'" in warning
    assert g.count("/sendMail") == 1 and g.polls == 0 and not g.moves
    await mail.send_to_owner(MAILBOX, SUBJECT, "b")  # still delivered, without re-listing folders
    assert g.count("/sendMail") == 2 and g.count("/mailFolders") == 1


async def test_graph_failure_in_folder_step_never_fails_the_send(make_mail):
    g = FakeGraph(list_folders_status=500)
    filed, warning = await make_mail(g).send_to_owner(MAILBOX, SUBJECT, "a")
    assert not filed and "Couldn't file" in warning
    assert g.count("/sendMail") == 1


async def test_move_failure_falls_back_and_forgets_folder(make_mail):
    g = FakeGraph(move_status=404)
    mail = make_mail(g)
    filed, warning = await mail.send_to_owner(MAILBOX, SUBJECT, "a")
    assert not filed and "Couldn't file" in warning
    assert mail._folder_ids == {}  # re-resolved next time


async def test_failed_send_still_raises(make_mail):
    g = FakeGraph(send_status=500)
    with pytest.raises(httpx.HTTPStatusError):
        await make_mail(g).send_to_owner(MAILBOX, SUBJECT, "a")
    assert g.polls == 0 and not g.moves


async def test_blank_folder_setting_leaves_mail_in_inbox(make_mail, settings):
    settings.owner_mail_folder = "  "
    g = FakeGraph()
    filed, warning = await make_mail(g).send_to_owner(MAILBOX, SUBJECT, "a")
    assert (filed, warning) == (False, "")
    assert g.count("/sendMail") == 1 and g.count("/mailFolders") == 0


async def test_other_recipient_is_not_touched(make_mail):
    g = FakeGraph()
    filed, _ = await make_mail(g).send_to_owner("someone@else.co.uk", SUBJECT, "a")
    assert not filed and g.count("/mailFolders") == 0 and not g.moves


# --------------------------------------------------------------------------- notifier
class RecordingMail:
    demo = False

    def __init__(self, result=(True, ""), error: Exception | None = None):
        self.result = result
        self.error = error
        self.owner_calls: list[tuple] = []
        self.sent: list[tuple] = []

    async def send_to_owner(self, to, subject, body_html):
        self.owner_calls.append((to, subject))
        if self.error:
            raise self.error
        return self.result

    async def send_mail(self, *a, **k):
        self.sent.append((a, k))


def _notifier(settings, mail):
    settings.owner_email = MAILBOX
    bus = SimpleNamespace(published=[], publish=lambda ev, data: bus.published.append((ev, data)))
    return Notifier(settings, None, bus, mail, SimpleNamespace(enabled=False)), bus


async def test_notifier_email_channel_goes_via_owner_folder_path(settings):
    mail = RecordingMail((True, ""))
    n, bus = _notifier(settings, mail)
    via = await n.send_owner_update("Morning briefing", "body", channels=("teams", "email"))
    assert mail.owner_calls == [(MAILBOX, "[Jarvis] Morning briefing")] and not mail.sent
    assert "'salts jarvis' folder" in via


async def test_notifier_surfaces_inbox_fallback_warning(settings):
    mail = RecordingMail((False, "Outlook folder 'salts jarvis' not found - the email was left in the Inbox"))
    n, bus = _notifier(settings, mail)
    via = await n.send_owner_update("Alert", "body", channels=("email",))
    assert "Inbox" in via and "not found" in via
    assert bus.published[-1][1]["channels"] == [via]


async def test_notifier_send_failure_does_not_raise(settings):
    n, _ = _notifier(settings, RecordingMail(error=RuntimeError("boom")))
    via = await n.send_owner_update("Alert", "body", channels=("email",))
    assert "display only" in via
