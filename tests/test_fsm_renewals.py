"""Renewals through Salts FSM (services/fsm_renewals.py; tools fsm_renewals_due, fsm_renewal_prepare, fsm_renewal_send).

What is pinned here, against a fake FSM (synthetic data only - this repository is public):

* the client calls exactly the FSM contract (salts-fsm docs/jarvis_renewals.md) with the existing FSM key; a FSM that has not shipped
  the routes yet (404 with no error code of ours) is "FSM renewals API not available yet", never an exception; sample data is never a
  source; the FSM's own refusals come back as plain words;
* fsm_renewals_due is a read (allowed in check mode); fsm_renewal_prepare writes a DRAFT in the FSM with no approval card (like Jarvis's
  other FSM draft writes), gives a NEW draft the standard uplift and never reprices an existing one unasked, and leaves a "What Jarvis
  did" line; both writes are blocked in check mode; the old prepare_renewal tool is gone;
* fsm_renewal_send never sends: it previews and queues ONE card (customer, recipients, subject, the email's start, the money now and
  next year, a link to the FSM's PDF, the preview version); a repeat does not queue a second card; nothing is queued when the FSM says it
  cannot be sent or its send switch is off;
* only a person's Approve sends: the FSM is asked to send THAT version with the approver's name; a 409 "changed" fails the card and asks
  for a fresh preview; "already sent" fails clearly; a standing approval never runs it, even with every switch on;
* owner and managers only (team: refused); the card is not editable; the PDF route serves the FSM's PDF to the owner and refuses a team
  session; the Teams card text says who, how much and what;
* the proactive suggestion offers "prepare in Salts FSM" for a due contract and "send it?" for a drafted one; the activity feed and the
  coverage line name it.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis import access
from jarvis.access import Caller, tool_allowed
from jarvis.brain import checkmode, coverage
from jarvis.brain.tools import TOOLS_BY_NAME, dispatch
from jarvis.core import Jarvis
from jarvis.main import create_app
from jarvis.services import approval_inbox as inbox
from jarvis.services import fsm_renewals as fr
from jarvis.services import standing_approvals as sa
from jarvis.services import teams_approvals as ta
from tests.fakes import FakeClient

KEY = "sk-fsm-test-key-0000000000"
V1, V2 = "a" * 64, "b" * 64
PDF = b"%PDF-1.4 synthetic renewal\n%%EOF"


def renewal(rid="ren-1", status="DRAFT", proposed=525.0):
    return {"id": rid, "status": status, "contract_id": "k-1", "contract_name": "Maintenance - Example House",
            "customer_id": "c-1", "customer": "Example Homes Ltd", "site_id": "s-1", "site": "Example House",
            "renewal_date": "2026-11-18", "new_term_start": "2026-11-18", "new_term_end": "2027-11-18",
            "current_total": 500.0, "proposed_total": proposed, "change_value": proposed - 500, "change_percent": (proposed - 500) / 5,
            "vat_rate": 0.2, "vat": round(proposed * 0.2, 2), "total_inc_vat": round(proposed * 1.2, 2),
            "lines": [{"id": "rl-1", "action": "CHANGE", "description": "Fire alarm maintenance", "current_value": 500.0,
                       "proposed_value": proposed, "systems": ["Fire Alarm"]}],
            "recipients": ["accounts@example.invalid"], "recipient_source": "billing contact", "recipient_problem": "",
            "created_by": "Jarvis API", "sent_at": None, "sent_to": ""}


class Fsm:
    """A stand-in for the Salts FSM's /api/jarvis/renewals routes."""

    def __init__(self):
        self.mode = "ok"            # ok | missing (no routes: 404) | down
        self.send_enabled = True
        self.why_not: list[str] = []
        self.version = V1
        self.send_answer = "ok"     # ok | changed | already_sent | send_off
        self.open = False           # is there already an open draft for k-1
        self.calls: list[tuple[str, str, dict, dict | None, dict]] = []
        self.proposed = 500.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = unquote(request.url.path), request.method
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, dict(request.url.params), body, dict(request.headers)))
        if self.mode == "down":
            raise httpx.ConnectError("connection refused")
        if self.mode == "missing" or not path.startswith("/api/jarvis/renewals"):
            return httpx.Response(404, json={"detail": "Not Found"})
        if path == "/api/jarvis/renewals/due" and method == "GET":
            return httpx.Response(200, json={
                "within_days": int(request.url.params.get("within_days", 60)), "today": "2026-10-09", "send_enabled": self.send_enabled,
                "contracts": [
                    {"contract_id": "k-1", "contract_name": "Maintenance - Example House", "customer": "Example Homes Ltd",
                     "site": "Example House", "renewal_date": "2026-11-18", "days_left": 40, "current_value": 500.0,
                     "value_complete": True, "recipients": ["accounts@example.invalid"], "recipient_source": "billing contact",
                     "renewal": None, "needs": [{"code": "not_prepared", "message": "No renewal has been prepared yet."}],
                     "can_prepare": True, "ready_to_send": False},
                    {"contract_id": "k-2", "contract_name": "Maintenance - Sample Court", "customer": "Sample Trust",
                     "site": "Sample Court", "renewal_date": "2026-11-01", "days_left": 23, "current_value": 800.0,
                     "value_complete": True, "recipients": ["finance@sample.invalid"], "recipient_source": "site finance contact",
                     "renewal": {"id": "ren-2", "status": "DRAFT", "current_total": 800.0, "proposed_total": 840.0, "change_percent": 5.0},
                     "needs": [], "can_prepare": True, "ready_to_send": True}],
                "renewals": {"DRAFT": [], "SENT": []}, "counts": {"DRAFT": 1, "SENT": 0}})
        if path == "/api/jarvis/renewals/prepare" and method == "POST":
            if body.get("contract_id") == "k-none":
                return httpx.Response(404, json={"error": "contract_not_found", "message": "There is no contract with that id."})
            created = not self.open
            self.open = True
            applied = False
            if body.get("uplift_percent") is not None:
                new = round(500 * (1 + body["uplift_percent"] / 100), 2)
                applied, self.proposed = new != self.proposed, new
            return httpx.Response(201 if created else 200, json={"ok": True, "created": created, "already_open": not created,
                                                                 "pricing_applied": applied,
                                                                 "renewal": renewal(proposed=self.proposed)})
        if path.endswith("/preview") and method == "GET":
            rid = path.split("/")[-2]
            return httpx.Response(200, json={**renewal(rid, proposed=self.proposed), "subject": "Renewal of Maintenance - Example House",
                                             "body_html": "<p>Dear Example Homes Ltd,</p>",
                                             "body_text": "Dear Example Homes Ltd,\n\nPlease find attached the renewal.\n\nRegards,\n"
                                                          "[the name of the person who approves it]\nSalts",
                                             "pdf": {"path": f"/api/jarvis/renewals/{rid}/pdf", "filename": "Renewal - Example.pdf"},
                                             "version": self.version, "can_send": not self.why_not and self.send_enabled,
                                             "why_not": self.why_not, "send_enabled": self.send_enabled})
        if path.endswith("/pdf") and method == "GET":
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        if path.endswith("/send") and method == "POST":
            if self.send_answer == "changed":
                return httpx.Response(409, json={"error": "changed", "message": "This renewal has changed since it was previewed.",
                                                 "current_version": V2})
            if self.send_answer == "already_sent":
                return httpx.Response(409, json={"error": "already_sent", "message": "This renewal has already been sent."})
            if self.send_answer == "send_off":
                return httpx.Response(403, json={"error": "send_off", "message": "off"})
            return httpx.Response(200, json={"ok": True, "sent_to": body.get("recipients") or ["accounts@example.invalid"],
                                             "recipient_source": "billing contact", "approved_by": body["approved_by"],
                                             "renewal": renewal(status="SENT")})
        return httpx.Response(405)

    def of(self, method, suffix):
        return [c for c in self.calls if c[0] == method and c[1].endswith(suffix)]


def build(settings, **overrides):
    settings.fsm_base_url = "https://fsm.example.test"
    settings.fsm_api_key = KEY
    for k, v in overrides.items():
        setattr(settings, k, v)
    fsm = Fsm()
    j = Jarvis(settings, client=FakeClient(), http=httpx.AsyncClient(transport=httpx.MockTransport(fsm.handler)))
    return j, fsm


@pytest.fixture
async def world(settings):
    j, fsm = build(settings)
    yield j, fsm
    await j.http.aclose()


async def drain(j):
    for _ in range(5):
        if not j.actions._tasks:
            break
        await asyncio.gather(*list(j.actions._tasks))


async def call(j, name, caller=None, check=False, **args):
    tool = TOOLS_BY_NAME[name]
    return await dispatch(j, tool, tool.model(**args), caller=caller, check=check)


# ------------------------------------------------------------------------------------------------------------- the client
async def test_due_reads_the_fsm_contract_with_the_existing_key(world):
    j, fsm = world
    out = await call(j, "fsm_renewals_due", within_days=45)
    (method, path, params, _, headers), = fsm.calls
    assert (method, path, params) == ("GET", "/api/jarvis/renewals/due", {"within_days": "45"})
    assert headers.get("authorization") == f"Bearer {KEY}" or KEY in json.dumps(headers)
    assert [c["contract_id"] for c in out["contracts"]] == ["k-1", "k-2"] and out["contracts"][0]["needs"][0]["code"] == "not_prepared"
    assert j.db.pending_actions() == []


async def test_without_the_fsm_routes_it_says_not_available_yet_instead_of_erroring(world):
    j, fsm = world
    fsm.mode = "missing"
    for name, args in (("fsm_renewals_due", {}), ("fsm_renewal_prepare", {"contract_id": "k-1"}),
                       ("fsm_renewal_send", {"renewal_id": "ren-1"})):
        out = await call(j, name, **args)
        assert out["kind"] == "unavailable" and out["error"].startswith(fr.UNAVAILABLE), (name, out)
    assert j.db.pending_actions() == []
    facts = coverage.call_facts("fsm_renewals_due", {}, {"error": fr.UNAVAILABLE, "kind": "unavailable"})
    assert facts == [{"src": "Salts FSM", "status": coverage.NOT_EXPOSED, "detail": "renewals"}]


async def test_sample_data_is_never_a_source_and_an_outage_is_plain_words(settings):
    j = Jarvis(settings, client=FakeClient())          # no FSM configured: sample data
    out = await call(j, "fsm_renewals_due")
    assert out["kind"] == "demo" and "isn't connected" in out["error"]
    await j.http.aclose()
    j, fsm = build(settings)
    fsm.mode = "down"
    out = await call(j, "fsm_renewals_due")
    assert out["kind"] == "unreachable" and "couldn't reach" in out["error"] and "fsm.example" not in out["error"]
    out = await call(j, "fsm_renewal_prepare", contract_id="k-none", uplift_pct=3)
    assert out["kind"] == "unreachable"
    fsm.mode = "ok"
    out = await call(j, "fsm_renewal_prepare", contract_id="k-none", uplift_pct=3)
    assert out["kind"] == "not_found" and out["error"] == "There is no contract with that id."
    await j.http.aclose()


# ------------------------------------------------------------------------------------------------------------- prepare
async def test_prepare_writes_a_draft_in_the_fsm_with_no_card_and_the_standard_uplift_for_a_new_one(world):
    j, fsm = world
    out = await call(j, "fsm_renewal_prepare", contract_id="k-1", note="Annual renewal")
    posts = fsm.of("POST", "/prepare")
    assert [p[3] for p in posts] == [{"contract_id": "k-1", "note": "Annual renewal"},
                                     {"contract_id": "k-1", "uplift_percent": 5.0, "note": "Annual renewal"}]
    assert out["created"] is True and out["standard_uplift_applied"] == 5.0 and out["renewal"]["proposed_total"] == 525.0
    assert "nothing has been sent" in out["note"] and j.db.pending_actions() == [] and not fsm.of("POST", "/send")
    line = j.db.query("SELECT kind, actor, what FROM audit_events")
    assert [(r["kind"], r["actor"]) for r in line] == [("fsm_renewal", "Jarvis")] and "Example Homes Ltd" in line[0]["what"]


async def test_an_existing_draft_is_never_repriced_unasked_and_an_explicit_uplift_is_passed_on(world):
    j, fsm = world
    fsm.open = True
    out = await call(j, "fsm_renewal_prepare", contract_id="k-1")
    assert out["already_open"] is True and len(fsm.of("POST", "/prepare")) == 1 and "uplift_percent" not in fsm.of("POST", "/prepare")[0][3]
    assert j.db.query("SELECT * FROM audit_events") == []          # found, not changed: nothing to record
    out = await call(j, "fsm_renewal_prepare", contract_id="k-1", uplift_pct=3.5)
    assert fsm.of("POST", "/prepare")[-1][3] == {"contract_id": "k-1", "uplift_percent": 3.5} and out["pricing_applied"] is True
    out = await call(j, "fsm_renewal_prepare", contract_id="k-1", line_prices=[{"line_id": "rl-1", "proposed_value": 540}])
    assert fsm.of("POST", "/prepare")[-1][3] == {"contract_id": "k-1", "lines": [{"id": "rl-1", "proposed_value": 540.0}]}
    with pytest.raises(ValueError):
        TOOLS_BY_NAME["fsm_renewal_prepare"].model(contract_id="k-1", uplift_pct=3, line_prices=[{"line_id": "rl-1", "proposed_value": 1}])


async def test_check_mode_reads_due_and_blocks_both_writes(world):
    j, fsm = world
    assert "fsm_renewals_due" in checkmode.CHECK_TOOLS
    assert not {"fsm_renewal_prepare", "fsm_renewal_send"} & checkmode.CHECK_TOOLS
    assert "contracts" in await call(j, "fsm_renewals_due", check=True)
    for name, args in (("fsm_renewal_prepare", {"contract_id": "k-1"}), ("fsm_renewal_send", {"renewal_id": "ren-1"})):
        assert (await call(j, name, check=True, **args))["blocked_in_check_mode"] is True
    assert [c[0] for c in fsm.calls] == ["GET"] and j.db.pending_actions() == []


def test_the_old_letter_tool_is_retired():
    assert "prepare_renewal" not in TOOLS_BY_NAME
    assert {"fsm_renewals_due", "fsm_renewal_prepare", "fsm_renewal_send"} <= set(TOOLS_BY_NAME)
    assert not any(TOOLS_BY_NAME[n].approval for n in ("fsm_renewals_due", "fsm_renewal_prepare", "fsm_renewal_send"))


# ------------------------------------------------------------------------------------------------------------- send: the card
async def test_send_previews_and_queues_one_card_and_sends_nothing(world):
    j, fsm = world
    out = await call(j, "fsm_renewal_send", renewal_id="ren-1")
    pending = j.db.pending_actions()
    assert len(pending) == 1 and pending[0]["kind"] == fr.SEND_KIND and out["queued_action"] == pending[0]["id"]
    p = pending[0]["payload"]
    assert p["renewal_id"] == "ren-1" and p["version"] == V1 and p["recipients"] == ["accounts@example.invalid"]
    assert p["customer"] == "Example Homes Ltd" and p["subject"] == "Renewal of Maintenance - Example House"
    assert p["body_excerpt"].startswith("Dear Example Homes Ltd,\n\nPlease find attached") and p["recipients_are_own"] is True
    assert (p["current_total"], p["proposed_total"]) == (500.0, 500.0) and p["pdf"] == "/api/fsm/renewals/ren-1/pdf"
    assert not fsm.of("POST", "/send") and "nothing has been sent" in out["note"]
    again = await call(j, "fsm_renewal_send", renewal_id="ren-1")
    assert again["already_queued"] is True and again["queued_action"] == out["queued_action"] and len(j.db.pending_actions()) == 1


async def test_the_card_shows_who_how_much_what_and_the_pdf_and_cannot_be_edited(world):
    j, fsm = world
    await call(j, "fsm_renewal_send", renewal_id="ren-1")
    action = j.db.pending_actions()[0]
    v = inbox.view(action)
    rows = {r["label"]: r for r in v["details"]}
    assert v["kind_label"] == "Renewal to send (Salts FSM)" and v["editable_fields"] == []
    assert rows["Customer"]["value"] == "Example Homes Ltd" and rows["Send to"]["value"] == "accounts@example.invalid"
    assert "£500.00 now -> £500.00" in rows["Annual price"]["value"] and "plus VAT" in rows["Annual price"]["value"]
    assert rows["Message (start)"]["block"] and "Please find attached" in rows["Message (start)"]["value"]
    assert rows["PDF"]["href"] == "/api/fsm/renewals/ren-1/pdf" and rows["Preview version"]["value"] == V1[:12]
    assert sum(1 for r in v["details"] if "href" in r) == 1
    text = ta.approval_card(action)["body"][2]["text"]
    assert "Example Homes Ltd" in text and "accounts@example.invalid" in text and "£500.00" in text and V1[:12] in text


async def test_nothing_is_queued_when_the_fsm_says_it_cannot_be_sent_or_sending_is_off(world):
    j, fsm = world
    fsm.why_not = ["It has already been sent to accounts@example.invalid."]
    out = await call(j, "fsm_renewal_send", renewal_id="ren-1")
    assert out["kind"] == "not_sendable" and "already been sent" in out["error"]
    fsm.why_not, fsm.send_enabled = [], False
    out = await call(j, "fsm_renewal_send", renewal_id="ren-1")
    assert out["kind"] == "send_off" and "Jarvis may send renewals" in out["error"]
    assert j.db.pending_actions() == [] and not fsm.of("POST", "/send")


# ------------------------------------------------------------------------------------------------------------- send: approval
async def test_only_a_persons_approve_sends_that_version_with_their_name(world):
    j, fsm = world
    await call(j, "fsm_renewal_send", renewal_id="ren-1")
    aid = j.db.pending_actions()[0]["id"]
    assert not fsm.of("POST", "/send")
    await j.actions.approve(aid, by="Pat Manager")
    await drain(j)
    (_, path, _, body, _), = fsm.of("POST", "/send")
    assert path == "/api/jarvis/renewals/ren-1/send" and body == {"expected_version": V1, "approved_by": "Pat Manager"}
    action = j.db.get_action(aid)
    assert action["status"] == "done" and "sent the renewal for Example Homes Ltd" in action["result"] and "Pat Manager" in action["result"]


async def test_the_owners_console_approval_is_sent_with_the_owners_name(world):
    j, fsm = world
    await call(j, "fsm_renewal_send", renewal_id="ren-1", recipients=["facilities@example.invalid"])
    aid = j.db.pending_actions()[0]["id"]
    await j.actions.approve(aid)            # the owner's console: recorded as "the owner"
    await drain(j)
    body = fsm.of("POST", "/send")[0][3]
    assert body["approved_by"] == j.settings.owner_name and body["recipients"] == ["facilities@example.invalid"]


async def test_a_changed_renewal_fails_the_card_and_asks_for_a_fresh_preview(world):
    j, fsm = world
    await call(j, "fsm_renewal_send", renewal_id="ren-1")
    aid = j.db.pending_actions()[0]["id"]
    fsm.send_answer = "changed"
    await j.actions.approve(aid, by="Pat Manager")
    await drain(j)
    action = j.db.get_action(aid)
    assert action["status"] == "failed" and "has changed in Salts FSM" in action["result"] and "send the renewal again" in action["result"]
    assert j.db.pending_actions() == []     # nothing re-queued by itself
    fsm.send_answer, fsm.version = "ok", V2
    await call(j, "fsm_renewal_send", renewal_id="ren-1")
    assert j.db.pending_actions()[0]["payload"]["version"] == V2


async def test_already_sent_and_send_off_fail_clearly(world):
    j, fsm = world
    for answer, words in (("already_sent", "already been sent"), ("send_off", "Jarvis may send renewals")):
        fsm.calls.clear()
        fsm.version = V1 if answer == "already_sent" else V2
        await call(j, "fsm_renewal_send", renewal_id="ren-1")
        aid = j.db.pending_actions()[0]["id"]
        fsm.send_answer = answer
        await j.actions.approve(aid, by="Pat Manager")
        await drain(j)
        action = j.db.get_action(aid)
        assert action["status"] == "failed" and words in action["result"], action["result"]
        fsm.send_answer = "ok"


async def test_a_standing_approval_never_sends_a_renewal_even_with_every_switch_on(settings):
    j, fsm = build(settings, standing_record_keeping=True, standing_acknowledgements=True)
    await call(j, "fsm_renewal_send", renewal_id="ren-1")
    await drain(j)
    action = j.db.pending_actions()[0]
    assert action["status"] == "pending" and action["approved_by"] == "" and not fsm.of("POST", "/send")
    assert sa.classify(fr.SEND_KIND, action["payload"], j.db) is None
    # and if a row ever claimed a standing approval, the executor still refuses to send it
    with pytest.raises(RuntimeError, match="only ever sent after a person approves"):
        await j.fsm_renewals.execute_send({**action, "approved_by": sa.APPROVER_PREFIX + sa.RECORD_KEEPING}, sa.APPROVER_PREFIX)
    assert not fsm.of("POST", "/send")
    await j.http.aclose()


# ------------------------------------------------------------------------------------------------------------- roles, routes
async def test_owner_and_managers_only(world):
    j, fsm = world
    sam_engineer, sam_office = Caller(access.TEAM, "Sam", "sid", access.ENGINEER), Caller(access.TEAM, "Sue", "sid2", access.OFFICE)
    for name in ("fsm_renewals_due", "fsm_renewal_prepare", "fsm_renewal_send"):
        assert name not in access.TEAM_TOOLS and not tool_allowed(name, sam_engineer) and not tool_allowed(name, sam_office)
        assert tool_allowed(name, Caller(access.MANAGER, "Pat")) and tool_allowed(name, None)
    out = await call(j, "fsm_renewal_send", caller=sam_office, renewal_id="ren-1")
    assert isinstance(out, str) and "fsm_renewal_send isn't available to you here" in out
    assert fsm.calls == [] and j.db.pending_actions() == []
    out = await call(j, "fsm_renewal_prepare", caller=Caller(access.MANAGER, "Pat"), contract_id="k-1", uplift_pct=4)
    assert out["pricing_applied"] is True
    assert j.db.query("SELECT actor FROM audit_events")[0]["actor"] == "Jarvis (asked by Pat)"


def test_the_pdf_route_serves_the_fsms_pdf_to_the_owner_and_not_to_a_team_session(settings, monkeypatch):
    monkeypatch.setattr("jarvis.main.LOGIN_DELAY_S", 0)
    j, fsm = build(settings, jarvis_owner_password="owner-pw-for-tests-123")
    assert access.ROUTE_POLICY["GET /api/fsm/renewals/{renewal_id}/pdf"] == access.MANAGER_OK
    app = create_app(settings, j)
    with TestClient(app) as c:
        assert c.get("/api/fsm/renewals/ren-1/pdf").status_code in (401, 303, 307)
        assert c.post("/login", data={"password": "owner-pw-for-tests-123"}, follow_redirects=False).status_code == 303
        r = c.get("/api/fsm/renewals/ren-1/pdf")
        assert r.status_code == 200 and r.content == PDF and r.headers["content-type"] == "application/pdf"
        assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
        assert c.get("/api/fsm/renewals/bad%20id/pdf").status_code == 400
        j.team_access.set_code("team-code-for-tests-456")
        t = TestClient(app)
        assert t.post("/login/team", data={"name": "Sam", "code": "team-code-for-tests-456"}, follow_redirects=False).status_code == 303
        assert t.get("/api/fsm/renewals/ren-1/pdf").status_code == 403
    assert all(p[1] == "/api/jarvis/renewals/ren-1/pdf" for p in fsm.of("GET", "/pdf")) and len(fsm.of("GET", "/pdf")) == 1


# ------------------------------------------------------------------------------------------------------------- proactive, feed
async def test_the_suggestion_offers_prepare_in_the_fsm_for_a_due_contract_and_send_for_a_drafted_one(world):
    j, fsm = world
    candidates = await j.suggestions._candidates()
    ren = {c["key"]: c for c in candidates if c["key"].startswith("renewal:")}
    assert set(ren) == {"renewal:k-1", "renewal:k-2"}
    assert "in Salts FSM" in ren["renewal:k-1"]["title"] and "fsm_renewal_prepare" in ren["renewal:k-1"]["prompt"]
    assert "drafted in Salts FSM" in ren["renewal:k-2"]["title"] and "fsm_renewal_send" in ren["renewal:k-2"]["prompt"]
    assert "ren-2" in ren["renewal:k-2"]["prompt"] and not any("letter" in c["prompt"] for c in ren.values())
    assert not fsm.of("POST", "")       # a suggestion prepares and sends nothing by itself
    fsm.mode = "missing"
    assert not [c for c in await j.suggestions._candidates() if c["key"].startswith("renewal:")]


async def test_the_activity_feed_and_coverage_name_it(world):
    j, fsm = world
    await call(j, "fsm_renewal_send", renewal_id="ren-1")
    from jarvis.services import activity_feed as af

    assert fr.SEND_KIND in af.EMAIL_KINDS and af._classify_action(fr.SEND_KIND, {}, "pending") == "draft"
    assert af._classify_action(fr.SEND_KIND, {}, "done") == "email" and af._AUDIT_KINDS["fsm_renewal"] == "fsm_change"
    facts = coverage.call_facts("fsm_renewals_due", {}, {"contracts": []})
    assert facts == [{"src": "Salts FSM", "status": coverage.OK, "detail": "renewals"}]
    assert coverage._label(facts[0]["src"], facts[0]["detail"]) == "Salts FSM renewals"
