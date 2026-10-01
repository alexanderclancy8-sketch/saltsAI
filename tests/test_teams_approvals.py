"""Approvals on Teams: proactive Adaptive Cards to each approver, and the deterministic, brain-free path that turns a
button press (or `approve 12`) into ActionExecutor.approve/deny."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.core import Jarvis
from jarvis.integrations.teamsbot import TeamsBotError
from jarvis.main import create_app
from jarvis.services.teams_approvals import (
    approval_card, approver_emails, parse_decision_value, parse_typed_command)
from tests.fakes import FakeClient, message, text_block

SERVICE = "https://smba.trafficmanager.net/uk/"
OWNER, PARTNER, MANAGER = "alex@salts.example.com", "sam@salts.example.com", "mo@salts.example.com"
EMAIL_ACTION = {"to": ["supplier@example.com"], "subject": "Purchase order PO-1", "body": "Please supply 4 x panels."}


class FakeMail:
    demo = False

    def __init__(self):
        self.sent = []

    async def send_mail(self, to, subject, body_html, cc=None, bcc=None, sensitivity=None):
        self.sent.append((to, subject))


def configure(settings):
    settings.owner_email, settings.owner_name = OWNER, "Alex"
    settings.partner_email, settings.partner_name = PARTNER, "Sam"
    settings.manager_emails = MANAGER
    settings.teams_bot_app_id, settings.teams_bot_app_password, settings.teams_bot_tenant_id = "app", "secret-pw", "t1"


class Wire:
    """A stand-in for Microsoft: records every request the bot makes, can be told to fail."""

    def __init__(self, fail=False):
        self.posts: list[tuple[str, dict]] = []
        self.puts: list[tuple[str, dict]] = []
        self.urls: list[str] = []
        self.fail = fail
        self.n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "tok-SECRET-123", "expires_in": 3600})
        if self.fail:
            return httpx.Response(500, json={"error": "down"})
        body = json.loads(request.content or b"{}")
        if request.method == "POST":
            self.n += 1
            self.posts.append((str(request.url), body))
            return httpx.Response(200, json={"id": f"act-{self.n}"})
        if request.method == "PUT":
            self.puts.append((str(request.url), body))
            return httpx.Response(200, json={"id": "x"})
        return httpx.Response(404)

    def cards(self):
        return [b for _, b in self.posts if b.get("attachments")]

    def texts(self):
        return [b.get("text", "") for _, b in self.posts]


def make(settings, wire=None, script=None):
    configure(settings)
    j = Jarvis(settings, client=FakeClient(script))
    j.actions.mail = FakeMail()
    if wire is not None:
        j.teamsbot.http = httpx.AsyncClient(transport=httpx.MockTransport(wire.handler))
    return j


def hello(j, email, conv, ctype="personal", url=SERVICE):
    return j.actions.teams_approvals.remember(email, url, conv, ctype)


async def drain(j):
    for _ in range(5):
        if not j.actions._tasks:
            break
        await asyncio.gather(*list(j.actions._tasks))


# --------------------------------------------------------------------------- parsing
@pytest.mark.parametrize("text,expected", [
    ("approve 12", ("approve", 12)), ("Approve #12", ("approve", 12)), ("DENY   7", ("deny", 7)),
    ("deny #7", ("deny", 7)), ("  approve 5  ", ("approve", 5)), ("<at>Jarvis</at> approve 5", ("approve", 5)),
    ("approve\t5", ("approve", 5)),
])
def test_typed_commands_are_recognised(text, expected):
    assert parse_typed_command(text) == expected


@pytest.mark.parametrize("text", [
    "approve", "approve 12 please", "please approve 12", "approve #", "approve -1", "approve 0", "approve 1.5",
    "approve 12, deny 13", "approve 12\ndeny 13", "approved 12", "approve all", "approve twelve",
    "approve 1234567890", "approve ١٢", "approve ##12", "", "   ", None, 12, ["approve 1"],
    "approve 12;DROP", "/approve 12",
])
def test_anything_else_is_not_a_command(text):
    assert parse_typed_command(text) is None


@pytest.mark.parametrize("value,expected", [
    ({"jarvis_action": 12, "decision": "approve"}, ("ok", "approve", 12)),
    ({"jarvis_action": 7, "decision": "deny"}, ("ok", "deny", 7)),
    ({"jarvis_action": "12", "decision": "approve"}, ("ok", "approve", 12)),
    ('{"jarvis_action": 3, "decision": "deny"}', ("ok", "deny", 3)),
    ({"jarvis_action": 3, "decision": "deny", "extra": "ignored"}, ("ok", "deny", 3)),
])
def test_card_values_that_are_ours(value, expected):
    assert parse_decision_value(value) == expected


@pytest.mark.parametrize("value", [
    {"jarvis_action": 1, "decision": "APPROVE"}, {"jarvis_action": 1, "decision": "approved"},
    {"jarvis_action": 1, "decision": ""}, {"jarvis_action": 1}, {"jarvis_action": 1, "decision": None},
    {"jarvis_action": True, "decision": "approve"}, {"jarvis_action": 0, "decision": "approve"},
    {"jarvis_action": -4, "decision": "approve"}, {"jarvis_action": 1.0, "decision": "approve"},
    {"jarvis_action": 10 ** 30, "decision": "approve"}, {"jarvis_action": "12abc", "decision": "approve"},
    {"jarvis_action": "١٢", "decision": "approve"}, {"jarvis_action": [1], "decision": "approve"},
    {"jarvis_action": {"id": 1}, "decision": "approve"}, {"jarvis_action": None, "decision": "approve"},
    {"jarvis_action": 1, "decision": ["approve"]},
])
def test_malformed_card_values_are_bad(value):
    assert parse_decision_value(value) == ("bad", "", 0)


@pytest.mark.parametrize("value", [None, {}, {"foo": "bar"}, "hello", "not json", 5, [], [{"jarvis_action": 1}], True])
def test_values_from_other_cards_are_not_ours(value):
    assert parse_decision_value(value)[0] == "other"


def test_the_approver_list_is_owner_partner_and_managers(settings):
    configure(settings)
    assert approver_emails(settings) == {OWNER, PARTNER, MANAGER}
    settings.partner_email, settings.manager_emails = "", ""
    assert approver_emails(settings) == {OWNER}


def test_the_card_has_the_summary_id_and_exact_submit_data(settings):
    card = approval_card({"id": 12, "kind": "email_send", "summary": "Send email 'x' to y " + "z" * 900,
                          "payload": EMAIL_ACTION})
    assert [a["type"] for a in card["actions"]] == ["Action.Submit", "Action.Submit"]
    assert [a["title"] for a in card["actions"]] == ["Approve", "Deny"]
    assert card["actions"][0]["data"] == {"jarvis_action": 12, "decision": "approve"}
    assert card["actions"][1]["data"] == {"jarvis_action": 12, "decision": "deny"}
    texts = [b["text"] for b in card["body"]]
    assert "#12" in texts[0] and len(texts[1]) <= 500 and texts[1].endswith("…")
    assert "supplier@example.com" in texts[2]


def test_the_card_redacts_credentials_and_control_characters():
    card = approval_card({"id": 1, "kind": "email_send", "summary": "key sk-abcdef1234567890XYZ\x07\x00 ok",
                          "payload": {"to": [], "subject": "", "body": "Bearer abcdefghijklmnopqrstuv"}})
    flat = json.dumps(card)
    assert "sk-abcdef1234567890XYZ" not in flat and "abcdefghijklmnopqrstuv" not in flat and "\\u0007" not in flat


# --------------------------------------------------------------------------- the webhook
def activity(**over):
    base = {"type": "message", "serviceUrl": SERVICE, "conversation": {"id": "conv-1", "conversationType": "personal"},
            "from": {"id": "29:someone"}}
    base.update(over)
    return base


class Harness:
    """A Jarvis with the JWT check stubbed out, the sender's email chosen, and replies recorded."""

    def __init__(self, settings, monkeypatch, sender=OWNER, script=None):
        self.j = make(settings, script=script or [message([text_block("(brain reply)")])])
        self.sender = sender
        self.replies: list[tuple] = []

        async def ok(*a, **k):
            return None

        async def who(*a, **k):
            return self.sender

        async def say(service_url, conversation_id, text):
            self.replies.append((service_url, conversation_id, text))

        monkeypatch.setattr("jarvis.main.verify_activity", ok)
        self.j.teamsbot.sender_email = who
        self.j.teamsbot.reply = say
        self.app = create_app(settings, self.j)

    def brain_calls(self):
        return len(self.j.client.beta.messages.calls)

    def queue(self, kind="email_send", payload=None):
        return self.j.actions.queue(kind, "Send the supplier PO", payload or EMAIL_ACTION)

    def wait(self, pred, secs=3.0):
        end = time.time() + secs
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.05)
        return pred()

    def status(self, action_id):
        return self.j.db.get_action(action_id)["status"]


def submit(action_id, decision, **over):
    return activity(value={"jarvis_action": action_id, "decision": decision}, **over)


def test_a_card_press_from_an_allowlisted_person_approves_directly_without_the_brain(settings, monkeypatch):
    h = Harness(settings, monkeypatch, sender="Alex@Salts.example.com")  # case doesn't matter
    action_id = h.queue()
    with TestClient(h.app) as c:
        assert c.post("/api/teams/messages", json=submit(action_id, "approve")).status_code == 200
        assert h.wait(lambda: h.status(action_id) == "done")
    row = h.j.db.get_action(action_id)
    assert row["approved_by"] == "Alex" and h.j.actions.mail.sent == [(["supplier@example.com"], "Purchase order PO-1")]
    assert len(h.replies) == 1 and h.replies[0][2].startswith("Approved by Alex at ") and f"#{action_id}" in h.replies[0][2]
    assert h.brain_calls() == 0


def test_a_card_press_can_deny(settings, monkeypatch):
    h = Harness(settings, monkeypatch, sender=PARTNER)
    action_id = h.queue()
    with TestClient(h.app) as c:
        c.post("/api/teams/messages", json=submit(action_id, "deny"))
    row = h.j.db.get_action(action_id)
    assert row["status"] == "denied" and "Sam" in row["approved_by"] and h.j.actions.mail.sent == []
    assert h.replies[0][2].startswith("Denied by Sam at ") and h.brain_calls() == 0


def test_a_manager_may_approve_too(settings, monkeypatch):
    h = Harness(settings, monkeypatch, sender=MANAGER)
    action_id = h.queue()
    with TestClient(h.app) as c:
        c.post("/api/teams/messages", json=submit(action_id, "approve"))
        assert h.wait(lambda: h.status(action_id) == "done")


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_a_sender_who_is_not_on_the_allowlist_is_ignored(settings, monkeypatch, decision):
    h = Harness(settings, monkeypatch, sender="stranger@example.com")
    action_id = h.queue()
    with TestClient(h.app) as c:
        r = c.post("/api/teams/messages", json=submit(action_id, decision))
        assert r.status_code == 200 and r.json() == {}
        r = c.post("/api/teams/messages", json=activity(text=f"{decision} {action_id}"))
        assert r.status_code == 200
        time.sleep(0.2)
    assert h.status(action_id) == "pending" and h.replies == [] and h.brain_calls() == 0
    assert h.j.db.teams_approvers() == []  # and a stranger never becomes someone Jarvis messages


def test_an_unresolvable_sender_is_ignored(settings, monkeypatch):
    h = Harness(settings, monkeypatch, sender=None)
    action_id = h.queue()
    with TestClient(h.app) as c:
        c.post("/api/teams/messages", json=submit(action_id, "approve"))
    assert h.status(action_id) == "pending" and h.replies == []


def test_a_bad_token_is_401_and_nothing_is_decided(settings, monkeypatch):
    h = Harness(settings, monkeypatch)
    action_id = h.queue()

    async def fail(*a, **k):
        raise TeamsBotError("bad token")

    monkeypatch.setattr("jarvis.main.verify_activity", fail)
    with TestClient(h.app) as c:
        r = c.post("/api/teams/messages", json=submit(action_id, "approve"), headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401
        assert c.post("/api/teams/messages", json=activity(text=f"approve {action_id}")).status_code == 401
    assert h.status(action_id) == "pending" and h.replies == [] and h.brain_calls() == 0


def test_a_missing_authorization_header_is_401_with_the_real_verifier(settings):
    j = make(settings)
    action_id = j.actions.queue("email_send", "x", EMAIL_ACTION)
    app = create_app(settings, j)
    with TestClient(app) as c:
        assert c.post("/api/teams/messages", json=submit(action_id, "approve")).status_code == 401
        assert c.post("/api/teams/messages", json=submit(action_id, "approve"),
                      headers={"Authorization": "Basic abc"}).status_code == 401
    assert j.db.get_action(action_id)["status"] == "pending"


def test_an_untrusted_service_url_is_dropped_before_the_sender_is_even_looked_up(settings, monkeypatch):
    h = Harness(settings, monkeypatch)
    action_id = h.queue()
    looked_up = []

    async def who(*a, **k):
        looked_up.append(1)
        return OWNER

    h.j.teamsbot.sender_email = who
    with TestClient(h.app) as c:
        c.post("/api/teams/messages", json=submit(action_id, "approve", serviceUrl="https://evil.example.com"))
    assert looked_up == [] and h.status(action_id) == "pending"


def test_deciding_an_action_already_decided_elsewhere_says_so(settings, monkeypatch):
    h = Harness(settings, monkeypatch)
    action_id = h.queue()
    assert h.j.db.decide_pending_action(action_id, "approved", "Sam")  # e.g. Sam tapped it on the HUD first
    with TestClient(h.app) as c:
        c.post("/api/teams/messages", json=submit(action_id, "approve"))
        c.post("/api/teams/messages", json=submit(action_id, "deny"))
    assert [r[2] for r in h.replies] == [f"Action #{action_id} was already approved by Sam - nothing more to do."] * 2
    assert h.status(action_id) == "approved"  # untouched: neither tap changed anything
    assert h.brain_calls() == 0


def test_an_action_that_ran_automatically_says_so(settings, monkeypatch):
    settings.standing_record_keeping = True
    h = Harness(settings, monkeypatch)

    async def go():
        return h.j.actions.queue("fsm_write", "x", {"method": "POST", "path": "/customers", "body": {"name": "A"}})

    # queue() inside a running loop so the standing approval applies
    action_id = asyncio.run(go())
    assert h.status(action_id) in ("approved", "done")
    assert h.j.db.get_action(action_id)["approved_by"] == "standing approval: record keeping"
    with TestClient(h.app) as c:
        c.post("/api/teams/messages", json=submit(action_id, "deny"))
    assert "already ran automatically" in h.replies[0][2]


def test_a_missing_action_is_reported(settings, monkeypatch):
    h = Harness(settings, monkeypatch)
    with TestClient(h.app) as c:
        c.post("/api/teams/messages", json=submit(999, "approve"))
    assert h.replies[0][2] == "Action #999 doesn't exist." and h.brain_calls() == 0


@pytest.mark.parametrize("typed,expected_status", [
    ("approve {id}", "done"), ("Approve #{id}", "done"), ("APPROVE   {id}", "done"),
    ("deny {id}", "denied"), ("DENY #{id}", "denied"),
])
def test_typed_commands_are_handled_deterministically_before_the_brain(settings, monkeypatch, typed, expected_status):
    h = Harness(settings, monkeypatch)
    action_id = h.queue()
    with TestClient(h.app) as c:
        c.post("/api/teams/messages", json=activity(text=typed.format(id=action_id)))
        assert h.wait(lambda: h.status(action_id) == expected_status)
    assert h.brain_calls() == 0 and len(h.replies) == 1
    assert h.replies[0][2].split(" by ")[0] in ("Approved", "Denied")


def test_a_typed_command_from_a_stranger_is_ignored(settings, monkeypatch):
    h = Harness(settings, monkeypatch, sender="stranger@example.com")
    action_id = h.queue()
    with TestClient(h.app) as c:
        c.post("/api/teams/messages", json=activity(text=f"approve {action_id}"))
    assert h.status(action_id) == "pending" and h.brain_calls() == 0 and h.replies == []


@pytest.mark.parametrize("value", [
    {"jarvis_action": "x", "decision": "approve"}, {"jarvis_action": True, "decision": "approve"},
    {"jarvis_action": 1, "decision": "APPROVE"}, {"jarvis_action": -3, "decision": "deny"},
    {"jarvis_action": 10 ** 40, "decision": "approve"}, {"jarvis_action": [1], "decision": "approve"},
    {"jarvis_action": 1}, {"jarvis_action": 1, "decision": {"x": 1}},
])
def test_malformed_button_values_do_nothing_and_never_reach_the_brain(settings, monkeypatch, value):
    h = Harness(settings, monkeypatch)
    action_id = h.queue()  # id 1: the value above could name it, but must not decide it
    with TestClient(h.app) as c:
        assert c.post("/api/teams/messages", json=activity(value=value)).status_code == 200
        time.sleep(0.15)
    assert h.status(action_id) == "pending" and h.brain_calls() == 0
    assert len(h.replies) == 1 and h.replies[0][2].startswith("I couldn't read that button press")


@pytest.mark.parametrize("act", [
    activity(value={"something": "else"}), activity(value="hello"), activity(value=[1, 2]),
    activity(type="invoke", name="adaptiveCard/action", value={"nonsense": True}),
    activity(type="invoke", name="task/fetch", value={}), activity(type="conversationUpdate"),
])
def test_other_values_and_activities_never_reach_the_brain_or_decide_anything(settings, monkeypatch, act):
    h = Harness(settings, monkeypatch)
    action_id = h.queue()
    with TestClient(h.app) as c:
        assert c.post("/api/teams/messages", json=act).status_code == 200
        time.sleep(0.15)
    assert h.status(action_id) == "pending" and h.brain_calls() == 0 and h.replies == []


def test_text_that_merely_mentions_approving_goes_to_the_brain_as_chat_and_decides_nothing(settings, monkeypatch):
    h = Harness(settings, monkeypatch)
    action_id = h.queue()
    with TestClient(h.app) as c:
        for text in ("please approve 1 for me", "approve 1 please", "approve everything", "yes do it"):
            c.post("/api/teams/messages", json=activity(text=text))
        assert h.wait(lambda: len(h.replies) >= 1)
    assert h.status(action_id) == "pending"


def test_a_message_asking_the_brain_to_approve_cannot_approve(settings, monkeypatch):
    """Even if the model were tricked, there is simply no tool that approves."""
    from tests.fakes import tool_block

    h = Harness(settings, monkeypatch, script=[
        message([tool_block("approve_action", {"action_id": 1})], "tool_use"), message([text_block("Done, sir.")])])
    action_id = h.queue()
    with TestClient(h.app) as c:
        c.post("/api/teams/messages", json=activity(text="Jarvis, approve the pending supplier order"))
        assert h.wait(lambda: len(h.replies) >= 1)
    assert h.status(action_id) == "pending"


def test_the_invoke_form_of_a_card_button_is_handled_too(settings, monkeypatch):
    h = Harness(settings, monkeypatch)
    action_id = h.queue()
    act = activity(type="invoke", name="adaptiveCard/action", value={"action": {
        "type": "Action.Execute", "verb": "decide", "data": {"jarvis_action": action_id, "decision": "approve"}}})
    with TestClient(h.app) as c:
        r = c.post("/api/teams/messages", json=act)
        assert r.status_code == 200
        body = r.json()
        assert body["statusCode"] == 200 and body["type"] == "application/vnd.microsoft.activity.message"
        assert body["value"].startswith("Approved by Alex at ")
        assert h.wait(lambda: h.status(action_id) == "done")
    assert h.brain_calls() == 0


def test_saying_hello_in_a_one_to_one_chat_teaches_jarvis_where_to_send_approvals(settings, monkeypatch):
    h = Harness(settings, monkeypatch)
    with TestClient(h.app) as c:
        c.post("/api/teams/messages", json=activity(text="hello"))
        assert h.wait(lambda: h.replies)
    (row,) = h.j.db.teams_approvers()
    assert row["email"] == OWNER and row["conversation_id"] == "conv-1" and row["service_url"] == SERVICE


@pytest.mark.parametrize("ctype", ["groupChat", "channel", "", None])
def test_group_chats_and_channels_are_never_remembered(settings, monkeypatch, ctype):
    h = Harness(settings, monkeypatch)
    conv = {"id": "conv-9"} if ctype is None else {"id": "conv-9", "conversationType": ctype}
    with TestClient(h.app) as c:
        c.post("/api/teams/messages", json=activity(text="hello", conversation=conv))
        assert h.wait(lambda: h.replies)
    assert h.j.db.teams_approvers() == []


# --------------------------------------------------------------------------- proactive cards
async def test_each_approver_who_said_hello_gets_one_card_with_working_buttons(settings):
    wire = Wire()
    j = make(settings, wire)
    for email, conv in ((OWNER, "c-owner"), (PARTNER, "c-partner"), (MANAGER, "c-mgr")):
        assert hello(j, email, conv)
    action_id = j.actions.queue("email_send", "Send the supplier PO", EMAIL_ACTION)
    await drain(j)
    assert len(wire.cards()) == 3
    assert {u.split("/conversations/")[1].split("/")[0] for u, b in wire.posts} == {"c-owner", "c-partner", "c-mgr"}
    card = wire.cards()[0]["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert wire.cards()[0]["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert [a["data"] for a in card["actions"]] == [{"jarvis_action": action_id, "decision": "approve"},
                                                    {"jarvis_action": action_id, "decision": "deny"}]
    assert "Send the supplier PO" in json.dumps(card)
    # a second queue of the same action id, a republish, or a retry never re-sends
    action = j.db.get_action(action_id)
    assert await j.actions.teams_approvals.offer(action) == 0
    j.bus.publish("approvals", j.db.pending_actions())
    await drain(j)
    assert len(wire.cards()) == 3
    # the next action is a new card for each
    j.actions.queue("email_send", "Another", EMAIL_ACTION)
    await drain(j)
    assert len(wire.cards()) == 6
    await j.http.aclose()


async def test_people_who_have_not_said_hello_or_who_are_not_approvers_get_nothing(settings):
    wire = Wire()
    j = make(settings, wire)
    hello(j, OWNER, "c-owner")
    j.db.save_teams_approver("stranger@example.com", SERVICE, "c-stranger")  # not on the allowlist
    j.db.save_teams_approver(PARTNER, "https://evil.example.com/", "c-evil")  # an untrusted serviceUrl
    assert not hello(j, "stranger@example.com", "c-x")
    assert not hello(j, MANAGER, "c-x", url="https://evil.example.com/")
    j.actions.queue("email_send", "x", EMAIL_ACTION)
    await drain(j)
    assert [u.split("/conversations/")[1].split("/")[0] for u, _ in wire.posts] == ["c-owner"]
    assert not [u for u in wire.urls if "evil.example.com" in u or "c-stranger" in u]
    await j.http.aclose()


async def test_no_card_when_the_bot_is_not_set_up_and_the_hud_is_unchanged(settings):
    wire = Wire()
    j = make(settings, wire)
    hello(j, OWNER, "c-owner")
    settings.teams_bot_app_id = ""  # not configured
    action_id = j.actions.queue("email_send", "x", EMAIL_ACTION)
    await drain(j)
    assert wire.posts == [] and wire.urls == []
    assert [a["id"] for a in j.db.pending_actions()] == [action_id]
    await j.http.aclose()


async def test_a_teams_failure_never_blocks_queueing_or_leaks_secrets(settings, caplog):
    wire = Wire(fail=True)
    j = make(settings, wire)
    hello(j, OWNER, "c-owner")
    hello(j, PARTNER, "c-partner")
    events = j.bus.subscribe()
    with caplog.at_level(logging.DEBUG):
        action_id = j.actions.queue("email_send", "x", EMAIL_ACTION)
        await drain(j)
    assert j.db.get_action(action_id)["status"] == "pending"
    assert events.get_nowait()["type"] == "approvals"  # the HUD was told
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "tok-SECRET-123" not in logged and "secret-pw" not in logged
    assert "smba.trafficmanager.net" not in logged and "c-owner" not in logged and "conversations" not in logged
    assert "HTTP 500" in logged
    await j.http.aclose()


async def test_an_exception_while_sending_is_swallowed(settings):
    j = make(settings, Wire())
    hello(j, OWNER, "c-owner")

    async def boom(*a, **k):
        raise RuntimeError("kaboom")

    j.teamsbot.send_activity = boom
    action_id = j.actions.queue("email_send", "x", EMAIL_ACTION)
    await drain(j)
    assert j.db.get_action(action_id)["status"] == "pending"
    await j.http.aclose()


async def test_the_bot_refuses_to_post_to_an_untrusted_service_url(settings):
    wire = Wire()
    j = make(settings, wire)
    with pytest.raises(TeamsBotError):
        await j.teamsbot.send_activity("https://evil.example.com", "c", {"type": "message", "text": "x"})
    with pytest.raises(TeamsBotError):
        await j.teamsbot.update_activity("https://evil.example.com", "c", "a", {"type": "message"})
    with pytest.raises(TeamsBotError):
        await j.teamsbot.reply("https://evil.example.com", "c", "x")
    assert wire.urls == []  # no token request, no POST - the credential never left
    await j.http.aclose()


async def test_the_cards_turn_into_the_outcome_whoever_decides(settings):
    wire = Wire()
    j = make(settings, wire)
    hello(j, OWNER, "c-owner")
    hello(j, PARTNER, "c-partner")
    action_id = j.actions.queue("email_send", "Send the supplier PO", EMAIL_ACTION)
    await drain(j)
    await j.actions.approve(action_id, by="Alex")  # e.g. on the HUD
    await drain(j)
    assert len(wire.puts) == 2
    url, body = wire.puts[0]
    assert "/activities/act-1" in url or "/activities/act-2" in url
    card = body["attachments"][0]["content"]
    assert "actions" not in card  # no buttons left
    assert body["text"].startswith("Approved by Alex at ")
    await j.http.aclose()


async def test_a_denial_updates_the_cards_too(settings):
    wire = Wire()
    j = make(settings, wire)
    hello(j, OWNER, "c-owner")
    action_id = j.actions.queue("email_send", "x", EMAIL_ACTION)
    await drain(j)
    await j.actions.deny(action_id, by="Sam")
    await drain(j)
    assert wire.puts and wire.puts[0][1]["text"].startswith("Denied by Sam at ")
    await j.http.aclose()


async def test_an_automatic_run_sends_an_info_message_not_an_approval_card(settings):
    settings.standing_record_keeping = True
    wire = Wire()
    j = make(settings, wire)
    j.actions.fsm = type("F", (), {"demo": False, "write": staticmethod(lambda *a, **k: _done())})()
    hello(j, OWNER, "c-owner")
    action_id = j.actions.queue("fsm_write", "x", {"method": "POST", "path": "/customers",
                                                    "body": {"name": "Acme Fire Ltd"}})
    await drain(j)
    assert j.db.get_action(action_id)["status"] == "done"
    assert wire.cards() == []
    (text,) = wire.texts()
    assert text.startswith("Done automatically (standing approval - record keeping): Created customer 'Acme Fire Ltd'")
    assert "Undo:" in text and f"#{action_id}" in text
    await j.http.aclose()


async def _done():
    return {"id": "1"}
