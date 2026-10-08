"""Proactive suggestions with a Prepare button (services/fsm_suggestions.py): Jarvis's half of the Salts FSM Action Centre contract.

What is pinned here:

* Jarvis PUSHES a suggestion per sent quote with no response for 7+ days (idempotent), tells the FSM when it stops being true, and
  POLLS for Prepare presses; a press only makes Jarvis DRAFT the chase email and queue it for approval, reported back as 'prepared'
  with the approval's id - or as 'failed' with a plain note. Never twice for the same suggestion;
* nothing here approves or sends: the draft waits for a human even with both standing-approval switches on;
* the FSM down, or without the endpoints yet (404), is tolerated quietly; sample data is never a source; the owner's switch turns it all off;
* only the owner/manager console (same-origin) and the poller can Prepare or snooze - never the model, never a team session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

import jarvis
from jarvis.brain.tools import TOOLS, TOOLS_BY_NAME
from jarvis.core import Jarvis
from jarvis.db import Database
from jarvis.main import create_app
from jarvis.services import fsm_suggestions as fs
from jarvis.services.scheduler import build_scheduler
from jarvis.settings_store import OWNER_ONLY_KEYS
from tests.fakes import FakeClient

KEY = "sk-fsm-secret-key-0123456789"


class FsmServer:
    """A stand-in for the Salts FSM: its quotes and contracts, and the /api/jarvis/suggestions endpoints under test."""

    def __init__(self, today: date | None = None):
        # "Today" is the BUSINESS day (settings.timezone), passed in or read here at call time, never from the process's own zone:
        # Jarvis() moves the process from UTC to Europe/London (Linux), so a date.today() taken BEFORE it was built is the UTC date
        # and one taken after is the London date - an hour either side of British midnight those differ by a day and the "9 days"
        # quote became 10 days old (this is what failed CI around 23:41 UTC). Tests hand the same `today` to sync() (see build()).
        today = today or datetime.now(ZoneInfo("Europe/London")).date()
        self.today = today

        def quote(qid, days, value, customer, status="sent"):
            return {"id": qid, "title": f"Work for {customer}", "customer": customer, "site": f"{customer} House",
                    "value": value, "status": status, "sent_date": (today - timedelta(days=days)).isoformat()}

        self.quotes = [quote("Q1", 9, 1200, "Acme Ltd"), quote("Q2", 3, 300, "Acme Ltd"),
                       quote("Q3", 12, 700, "Acme Ltd", status="accepted"), quote("Q4", 30, 5000, "Beta Ltd")]
        self.contracts = [{"id": "C1", "customer": "Acme Ltd", "site": "Acme Ltd House",
                           "contact_email": "ops@acme.example.com", "contact_name": "Sam"}]  # Beta Ltd: no email known
        self.suggestions: dict[str, dict] = {}
        self.requested: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str, dict | None, dict]] = []   # (method, path, body, headers)
        self.mode = "ok"            # ok | missing (no suggestions endpoints: 404) | down | broken (503)
        self.sticky = False         # True: the FSM keeps listing a request as 'requested' even after Jarvis reported on it

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else None
        self.sent.append((method, path, body, dict(request.headers)))
        if self.mode == "down":
            raise httpx.ConnectError("connection refused")
        if path.startswith("/api/jarvis/suggestions"):
            if self.mode == "missing":
                return httpx.Response(404, text="Not Found")
            if self.mode == "broken":
                return httpx.Response(503, text="unavailable")
            ext = unquote(path.removeprefix("/api/jarvis/suggestions")).lstrip("/")
            if method == "GET":
                return httpx.Response(200, json=[dict(r) for r in self.requested])
            if method == "PUT":
                self.suggestions[ext] = {**body, "external_id": ext, "status": "open"}
                return httpx.Response(200, json={"ok": True})
            if method == "PATCH":
                row = self.suggestions.setdefault(ext, {"external_id": ext})
                row.update(body)
                if body.get("status") in ("prepared", "failed", "resolved") and not self.sticky:
                    self.requested = [r for r in self.requested if r["external_id"] != ext]
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(405)
        if path == "/api/jarvis/quotes":
            status = request.url.params.get("status")
            return httpx.Response(200, json=[q for q in self.quotes if not status or q["status"] == status])
        if path == "/api/jarvis/contracts":
            return httpx.Response(200, json=self.contracts)
        return httpx.Response(404)

    def press_prepare(self, ext, by="Hannah Cole", **extra):
        self.requested.append({"external_id": ext, "requested_by": by, "requested_at": datetime.now().isoformat(), **extra})

    def patches(self, ext=None):
        return [(p, b) for m, p, b, _ in self.sent if m == "PATCH" and (ext is None or p.endswith(ext))]

    def puts(self):
        return [(p, b) for m, p, b, _ in self.sent if m == "PUT"]

    def sugg_calls(self):
        return [c for c in self.calls if c[1].startswith("/api/jarvis/suggestions")]


def build(settings, **overrides) -> tuple[Jarvis, FsmServer]:
    settings.fsm_base_url = "https://fsm.example.test"
    settings.fsm_api_prefix = "/api/jarvis"
    settings.fsm_api_key = KEY
    for k, v in overrides.items():
        setattr(settings, k, v)
    server = FsmServer(datetime.now(ZoneInfo(settings.timezone)).date())
    j = Jarvis(settings, client=FakeClient(), http=httpx.AsyncClient(transport=httpx.MockTransport(server.handler)))
    real_sync = j.fsm_suggestions.sync

    async def sync(today: date | None = None, force_hours: bool | None = None) -> int:   # every sweep sees the server's day, not the clock's
        return await real_sync(today or server.today, force_hours)

    j.fsm_suggestions.sync = sync
    return j, server


@pytest.fixture
async def world(settings):
    j, server = build(settings)
    yield j, server
    await j.http.aclose()


Q1 = "quote_followup:Q1"
Q4 = "quote_followup:Q4"


# ------------------------------------------------------------------------------------------------ the model and the registry
def test_every_kind_has_a_stable_external_id_a_lane_and_both_handlers():
    assert set(fs.KINDS) == {"quote_followup"}  # stage 1
    for name, kind in fs.KINDS.items():
        assert kind.name == name and ":" not in name
        assert kind.lane in fs.LANES and kind.record_type
        assert callable(kind.detect) and callable(kind.prepare)
    assert fs.external_id("quote_followup", "Q1180") == "quote_followup:Q1180"
    assert fs.split_external_id("quote_followup:Q1180") == ("quote_followup", "Q1180")
    assert fs.split_external_id("quote_followup:A:B") == ("quote_followup", "A:B")


# ------------------------------------------------------------------------------------------------ publishing
async def test_a_quote_unanswered_for_seven_days_is_pushed_once_with_the_contract_body(world):
    j, server = world
    assert await j.fsm_suggestions.sync(force_hours=True) == 4   # two new in the drawer + two pushed
    puts = dict(server.puts())
    assert set(puts) == {"/api/jarvis/suggestions/quote_followup:Q1", "/api/jarvis/suggestions/quote_followup:Q4"}  # not Q2 (3 days), not Q3 (accepted)
    body = puts["/api/jarvis/suggestions/quote_followup:Q1"]
    assert set(body) == {"kind", "lane", "title", "detail", "reason", "record_type", "record_id", "record_label", "created_at"}
    assert (body["kind"], body["lane"], body["record_type"], body["record_id"]) == ("quote_followup", "money", "quote", "Q1")
    assert body["record_label"] == "Q1 - Acme Ltd" and "Acme Ltd" in body["title"] and "9 days" in body["reason"]
    assert datetime.fromisoformat(body["created_at"])
    # the same suggestions are in the Approvals drawer
    assert {s["key"]: s["kind"] for s in j.db.open_suggestions()} == {Q1: "quote_followup", Q4: "quote_followup"}
    # every call carried the FSM key, and the key never appears in what was sent as a body
    assert all(h.get("authorization") == f"Bearer {KEY}" for m, p, b, h in server.sent if p.startswith("/api/jarvis/sugg"))
    assert KEY not in json.dumps([b for *_, b, _h in [(m, p, b, h) for m, p, b, h in server.sent]])


async def test_pushing_is_idempotent_and_only_a_change_is_pushed_again(world):
    j, server = world
    await j.fsm_suggestions.sync(force_hours=True)
    n = len(server.puts())
    assert await j.fsm_suggestions.sync(force_hours=True) == 0
    assert len(server.puts()) == n and len(j.db.open_suggestions()) == 2   # nothing re-sent, nothing duplicated
    server.quotes[0]["value"] = 1500                                        # the quote changed
    assert await j.fsm_suggestions.sync(force_hours=True) == 2   # re-worded in the drawer + re-pushed
    assert len(server.puts()) == n + 1 and "1,500" in server.puts()[-1][1]["title"]


async def test_a_suggestion_that_stops_being_true_is_resolved_in_the_fsm_and_goes_from_the_drawer(world):
    j, server = world
    await j.fsm_suggestions.sync(force_hours=True)
    server.quotes[0]["status"] = "accepted"                                 # the customer said yes
    assert await j.fsm_suggestions.sync(force_hours=True) == 2   # gone from the drawer + the FSM told
    assert server.patches("quote_followup:Q1") == [("/api/jarvis/suggestions/quote_followup:Q1",
                                                    {"status": "resolved", "note": "No longer needed."})]
    assert [s["key"] for s in j.db.open_suggestions()] == [Q4]
    assert j.db.get_suggestion(Q1)["status"] == "resolved"
    assert await j.fsm_suggestions.sync(force_hours=True) == 0              # told once, not every sweep
    assert len(server.patches("quote_followup:Q1")) == 1
    server.quotes[0]["status"] = "sent"                                     # ...and if it comes back, it is offered again
    await j.fsm_suggestions.sync(force_hours=True)
    assert j.db.get_suggestion(Q1)["status"] == "open" and server.suggestions[Q1]["status"] == "open"


async def test_a_new_suggestion_waits_for_working_hours_but_a_resolution_does_not(world):
    j, server = world
    await j.fsm_suggestions.sync(force_hours=False)                          # 3am: nothing NEW is pushed...
    assert server.puts() == [] and len(j.db.open_suggestions()) == 2        # ...but Jarvis's own drawer is already right
    await j.fsm_suggestions.sync(force_hours=True)
    assert len(server.puts()) == 2
    server.quotes[0]["status"] = "accepted"
    await j.fsm_suggestions.sync(force_hours=False)
    assert len(server.patches("quote_followup:Q1")) == 1                     # clearing up is always allowed


def test_working_hours_are_weekdays_between_the_configured_hours(settings):
    j = Jarvis(settings, client=FakeClient())
    tz = __import__("zoneinfo").ZoneInfo(settings.timezone)
    monday = datetime(2026, 10, 5, tzinfo=tz)                                # a Monday, built here, not at import
    assert j.fsm_suggestions._working_hours(monday.replace(hour=9))
    assert not j.fsm_suggestions._working_hours(monday.replace(hour=6))
    assert not j.fsm_suggestions._working_hours(monday.replace(hour=19))
    assert not j.fsm_suggestions._working_hours(monday.replace(day=10, hour=9))   # Saturday


async def test_a_first_run_does_not_flood_the_action_centre(world, monkeypatch):
    j, server = world
    monkeypatch.setattr(fs, "MAX_NEW_PUSHES_PER_RUN", 1)
    await j.fsm_suggestions.sync(force_hours=True)
    assert len(server.puts()) == 1
    await j.fsm_suggestions.sync(force_hours=True)
    assert len(server.puts()) == 2                                           # the rest follow on the next run


# ------------------------------------------------------------------------------------------------ poll -> prepare -> report
async def test_a_prepare_press_drafts_the_chase_queues_it_for_approval_and_reports_prepared(world):
    j, server = world
    await j.fsm_suggestions.sync(force_hours=True)
    sent = []

    async def no_send(*a, **k):
        sent.append(a)

    j.mail.send_mail = no_send
    server.press_prepare(Q1)
    assert await j.fsm_suggestions.poll() == 1
    (action,) = j.db.pending_actions()
    assert action["kind"] == "email_send" and action["status"] == "pending"
    assert action["payload"]["to"] == ["ops@acme.example.com"] and "Q1" in action["payload"]["subject"]
    assert sent == []                                                        # drafted, never sent
    ((path, body),) = server.patches("quote_followup:Q1")
    assert body == {"status": "prepared", "note": "Draft ready in Jarvis, waiting for approval.",
                    "approval_ref": str(action["id"])}
    assert j.db.get_suggestion(Q1)["status"] == "prepared"
    assert [s["key"] for s in j.db.open_suggestions()] == [Q4]               # the draft is in Approvals now, not offered twice
    assert j.db.get_action(action["id"])["approved_by"] in ("", None)        # nobody approved it


async def test_the_same_request_twice_never_queues_a_second_draft(world):
    j, server = world
    await j.fsm_suggestions.sync(force_hours=True)
    server.sticky = True                                                     # the FSM keeps saying 'requested'
    server.press_prepare(Q1)
    await j.fsm_suggestions.poll()
    await j.fsm_suggestions.poll()
    server.press_prepare(Q1, by="Someone else")                              # a second press, from someone else
    await j.fsm_suggestions.poll()
    assert len(j.db.pending_actions()) == 1
    refs = {b["approval_ref"] for _, b in server.patches("quote_followup:Q1")}
    assert len(refs) == 1 and all(b["status"] == "prepared" for _, b in server.patches("quote_followup:Q1"))


async def test_two_prepares_at_the_same_moment_make_one_draft(world):
    j, server = world
    await j.fsm_suggestions.sync(force_hours=True)
    a, b = await asyncio.gather(j.fsm_suggestions.prepare_suggestion(Q1), j.fsm_suggestions.prepare_suggestion(Q1))
    assert a["status"] == b["status"] == "prepared" and a["approval_id"] == b["approval_id"]
    assert len(j.db.pending_actions()) == 1


async def test_nothing_sensible_to_prepare_is_reported_failed_in_plain_words(world):
    j, server = world
    await j.fsm_suggestions.sync(force_hours=True)
    server.press_prepare(Q4)                                                 # Beta Ltd: no email address anywhere
    server.press_prepare("quote_followup:Q3")                                # accepted in the meantime
    server.press_prepare("quote_followup:NOPE")                              # no such quote
    server.press_prepare("something_else:Q1")                                # a kind Jarvis does not have
    await j.fsm_suggestions.poll()
    assert j.db.pending_actions() == []
    notes = {p.rsplit("/", 1)[1]: b for p, b in server.patches()}
    assert all(b["status"] == "failed" and "approval_ref" not in b for b in notes.values()) and len(notes) == 4
    assert "no email address" in notes["quote_followup:Q4"]["note"]
    assert "no longer waiting" in notes["quote_followup:Q3"]["note"] and "no longer waiting" in notes["quote_followup:NOPE"]["note"]
    assert "can't prepare that kind" in notes["something_else:Q1"]["note"]
    for b in notes.values():
        assert KEY not in b["note"] and "Traceback" not in b["note"] and len(b["note"]) < 200
    # a failure leaves no marker, so the press can be made again once it is fixed
    assert j.fsm_suggestions.prepared_marker(Q4) is None
    server.contracts.append({"id": "C2", "customer": "Beta Ltd", "site": "Beta Ltd House", "contact_email": "a@beta.example.com"})
    server.press_prepare(Q4)
    await j.fsm_suggestions.poll()
    assert len(j.db.pending_actions()) == 1 and server.patches(Q4)[-1][1]["status"] == "prepared"


async def test_a_handler_that_blows_up_is_reported_failed_not_raised(world, monkeypatch):
    j, server = world

    async def boom(j_, record_id):
        raise RuntimeError(f"secret {KEY} exploded")

    monkeypatch.setitem(fs.KINDS, "quote_followup", fs.Kind("quote_followup", "money", "quote", fs.KINDS["quote_followup"].detect, boom))
    server.press_prepare(Q1)
    assert await j.fsm_suggestions.poll() == 1
    (_, body), = server.patches(Q1)
    assert body["status"] == "failed" and KEY not in json.dumps(body)


async def test_a_report_the_fsm_did_not_accept_is_repeated_without_doing_the_work_twice(world):
    j, server = world
    await j.fsm_suggestions.sync(force_hours=True)
    server.sticky = True
    server.press_prepare(Q1)
    real = server.handler
    state = {"drop": True}

    def flaky(request):
        if request.method == "PATCH" and state["drop"]:
            return httpx.Response(500, text="oops")
        return real(request)

    j.http._transport = httpx.MockTransport(flaky)
    await j.fsm_suggestions.poll()
    assert len(j.db.pending_actions()) == 1
    state["drop"] = False
    j.fsm_suggestions._backoff_until = 0.0                                   # (the outage backoff, tested separately)
    await j.fsm_suggestions.poll()
    assert len(j.db.pending_actions()) == 1                                  # still one draft
    assert server.patches(Q1)[-1][1]["status"] == "prepared"                 # but the FSM was finally told


async def test_a_request_whose_snoozed_until_is_in_the_future_is_left_alone(world):
    j, server = world
    await j.fsm_suggestions.sync(force_hours=True)
    soon = (datetime.now().astimezone() + timedelta(hours=3)).isoformat()
    server.press_prepare(Q1, snoozed_until=soon)
    assert await j.fsm_suggestions.poll() == 0
    assert j.db.pending_actions() == [] and server.patches() == []
    server.requested[0]["snoozed_until"] = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()   # over
    assert await j.fsm_suggestions.poll() == 1


async def test_fsm_requested_prepares_are_rate_limited_per_hour(settings):
    j, server = build(settings, suggestions_prepare_max_per_hour=2)
    today = server.today
    server.contracts.append({"id": "C2", "customer": "Beta Ltd", "site": "x", "contact_email": "a@beta.example.com"})
    server.quotes = [{"id": f"X{i}", "title": "t", "customer": "Beta Ltd", "site": "s", "value": 10 + i, "status": "sent",
                      "sent_date": (today - timedelta(days=10)).isoformat()} for i in range(5)]
    for i in range(5):
        server.press_prepare(f"quote_followup:X{i}")
    await j.fsm_suggestions.poll()
    await j.fsm_suggestions.poll()
    assert len(j.db.pending_actions()) == 2                                  # two this hour, the rest wait
    assert len(server.requested) == 3                                        # still requested, not failed
    stamps = json.loads(j.db.get_kv(fs.RATE_KEY))
    j.db.set_kv(fs.RATE_KEY, json.dumps([t - 3700 for t in stamps]))         # an hour later
    await j.fsm_suggestions.poll()
    assert len(j.db.pending_actions()) == 4
    await j.http.aclose()


# ------------------------------------------------------------------------------------------------ safety of what Prepare queues
async def test_a_prepared_draft_waits_for_a_human_even_with_both_standing_approvals_on(settings):
    j, server = build(settings, standing_record_keeping=True, standing_acknowledgements=True)
    await j.fsm_suggestions.sync(force_hours=True)
    assert j.actions.standing is not None and j.actions.standing.s.standing_record_keeping is True
    server.press_prepare(Q1)
    await j.fsm_suggestions.poll()
    await asyncio.sleep(0.05)
    (action,) = j.db.pending_actions()
    assert action["status"] == "pending" and not str(action["approved_by"] or "").startswith("standing")
    # the standing allowlist never even looks at an email
    assert j.actions.standing.decide("email_send", action["payload"]).category is None
    await j.http.aclose()


def test_the_prepare_path_never_approves_denies_or_runs_anything():
    src = (Path(jarvis.__file__).parent / "services" / "fsm_suggestions.py").read_text(encoding="utf-8")
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    for forbidden in (".approve(", ".deny(", "_standing_decision", "standing.", "send_mail", "set_action_status",
                      "create_action", "_run(", "mail."):
        assert forbidden not in code, forbidden
    cc = (Path(jarvis.__file__).parent / "services" / "customer_comms.py").read_text(encoding="utf-8")
    assert ".approve(" not in cc and "send_mail" not in cc


async def test_editing_the_draft_does_not_make_the_suggestion_look_dealt_with(world):
    j, server = world
    await j.fsm_suggestions.sync(force_hours=True)
    server.press_prepare(Q1)
    await j.fsm_suggestions.poll()
    (action,) = j.db.pending_actions()
    new_id, _ = j.actions.edit(action["id"], {"subject": "Quick word about the quote"})
    await j.fsm_suggestions.sync(force_hours=True)
    assert j.db.get_suggestion(Q1)["status"] == "prepared" and not server.patches(Q1)[1:]   # not resolved by an edit
    await j.actions.approve(new_id)                                           # the human approves the edited draft
    await asyncio.sleep(0.05)
    await j.fsm_suggestions.sync(force_hours=True)
    assert j.db.get_suggestion(Q1)["status"] == "resolved"
    assert server.patches(Q1)[-1][1]["status"] == "resolved"                  # ...and only then does the FSM hear it is finished


async def test_a_chase_the_scheduled_sweep_already_drafted_is_not_offered_again(world):
    j, server = world
    j.settings.customer_comms_quote_followup_days = 5
    await j.customer_comms.draft_all(["quote_followup"], today=server.today)
    assert [a["payload"]["to"] for a in j.db.pending_actions()] == [["ops@acme.example.com"]]    # Q1; Q4 has no email
    await j.fsm_suggestions.sync(force_hours=True)
    assert {s["key"] for s in j.db.open_suggestions()} == {Q4}                  # Q1 already has its draft waiting
    server.press_prepare(Q1)                                                    # a stale press still doesn't draft it again
    await j.fsm_suggestions.poll()
    assert len(j.db.pending_actions()) == 1 and server.patches(Q1)[-1][1]["status"] == "prepared"


# ------------------------------------------------------------------------------------------------ Not now
async def test_not_now_snoozes_here_and_in_the_fsm_and_comes_back_tomorrow(world):
    j, server = world
    await j.fsm_suggestions.sync(force_hours=True)
    row = await j.fsm_suggestions.snooze_suggestion(Q1)
    assert row and j.db.get_suggestion(Q1)["status"] == "dismissed" and Q1 not in {s["key"] for s in j.db.open_suggestions()}
    ((_, body),) = server.patches(Q1)
    assert body["status"] == "snoozed" and datetime.fromisoformat(body["snoozed_until"]) > datetime.now().astimezone() + timedelta(hours=19)
    await j.fsm_suggestions.sync(force_hours=True)                            # still true, but quiet: not re-pushed, not resolved
    assert len(server.patches(Q1)) == 1 and j.db.get_suggestion(Q1)["status"] == "dismissed"
    j.db.execute("UPDATE suggestions SET updated_at = ? WHERE key = ?",
                 ((datetime.now().astimezone() - timedelta(hours=21)).isoformat(), Q1))
    await j.fsm_suggestions.sync(force_hours=True)
    assert j.db.get_suggestion(Q1)["status"] == "open" and Q1 in {s["key"] for s in j.db.open_suggestions()}
    assert await j.fsm_suggestions.snooze_suggestion("renewal:C1") is None    # only a suggestion with a kind


# ------------------------------------------------------------------------------------------------ the FSM away or not ready
async def test_an_fsm_without_the_endpoints_yet_is_logged_once_and_backed_off(world, caplog):
    j, server = world
    server.mode = "missing"
    caplog.set_level(logging.INFO, logger="jarvis.services.fsm_suggestions")
    for _ in range(4):
        assert await j.fsm_suggestions.poll() in (0,)
    await j.fsm_suggestions.sync(force_hours=True)                            # publishing is held back by the same backoff
    assert len(server.sugg_calls()) == 1                                      # one GET, then it left the FSM alone
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "404" in warnings[0].getMessage() and KEY not in caplog.text
    assert len(j.db.open_suggestions()) == 2                                  # Jarvis's own drawer still works
    # once it ships, Jarvis finds it again after the backoff
    server.mode = "ok"
    j.fsm_suggestions._backoff_until = 0.0
    await j.fsm_suggestions.sync(force_hours=True)
    assert len(server.puts()) == 2


async def test_a_missing_put_route_backs_off_too_and_does_not_warn_per_suggestion(world, caplog):
    j, server = world
    server.mode = "missing"
    await j.fsm_suggestions.sync(force_hours=True)
    assert len(server.sugg_calls()) == 1 and len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


@pytest.mark.parametrize("mode", ["down", "broken"])
async def test_an_fsm_that_is_down_never_raises_into_the_ui_or_the_scheduler(world, caplog, mode):
    j, server = world
    await j.fsm_suggestions.sync(force_hours=True)
    server.mode = mode
    j.fsm_suggestions._backoff_until = 0.0
    server.press_prepare(Q1)
    assert await j.fsm_suggestions.poll() == 0
    server.quotes[0]["status"] = "accepted"
    j.fsm_suggestions._backoff_until = 0.0
    await j.fsm_suggestions.sync(force_hours=True)                            # tries to resolve; fails quietly
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    server.mode = "ok"
    j.fsm_suggestions._backoff_until = 0.0
    await j.fsm_suggestions.sync(force_hours=True)
    assert server.patches(Q1)[-1][1]["status"] == "resolved"                  # caught up once it was back


async def test_an_fsm_that_refuses_one_suggestion_does_not_stop_the_rest(world, caplog):
    j, server = world
    real = server.handler

    def picky(request):
        if request.method == "PUT" and request.url.path.endswith("Q1"):
            return httpx.Response(422, text="bad")
        return real(request)

    j.http._transport = httpx.MockTransport(picky)
    await j.fsm_suggestions.sync(force_hours=True)
    assert "quote_followup:Q4" in server.suggestions and Q1 not in server.suggestions


# ------------------------------------------------------------------------------------------------ sample data, switches
async def test_sample_data_is_never_a_source_and_nothing_is_sent_without_an_fsm(settings):
    j = Jarvis(settings, client=FakeClient(), http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: pytest.fail(f"no request may be made: {r.url}"))))
    assert j.fsm.demo is True
    assert await j.fsm_suggestions.sync(force_hours=True) == 0
    assert await j.fsm_suggestions.poll() == 0
    assert j.db.kind_suggestions() == [] and j.fsm_suggestions.active() is False
    res = await j.fsm_suggestions.prepare_suggestion("quote_followup:Q1180")   # even if asked to (it is a demo quote)
    assert res["status"] == "failed" and "isn't connected" in res["note"] and j.db.pending_actions() == []
    await j.http.aclose()


async def test_the_owners_switch_turns_publishing_and_polling_off_but_not_the_local_drawer(settings):
    j, server = build(settings, suggestions_publish_to_fsm=False)
    await j.fsm_suggestions.sync(force_hours=True)
    server.press_prepare(Q1)
    assert await j.fsm_suggestions.poll() == 0
    assert server.sugg_calls() == [] and j.db.pending_actions() == []
    assert {s["key"] for s in j.db.open_suggestions()} == {Q1, Q4}            # the drawer (and its Prepare button) still works
    assert (await j.fsm_suggestions.prepare_suggestion(Q1))["status"] == "prepared"
    assert server.sugg_calls() == []
    await j.http.aclose()


def test_the_switch_defaults_on_and_is_owner_only(settings):
    assert settings.suggestions_publish_to_fsm is True
    assert "suggestions_publish_to_fsm" in OWNER_ONLY_KEYS
    from jarvis.settings_store import FIELDS
    assert FIELDS["suggestions_publish_to_fsm"].kind == "bool"


def test_a_manager_cannot_change_the_switch_only_the_owner_can(tmp_path, monkeypatch):
    from jarvis.config import Settings

    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "true")
    owner, manager = "alex@salts.example.com", "sam@salts.example.com"
    s = Settings(data_dir=tmp_path / "data", scheduler_enabled=False, anthropic_api_key="t", _env_file=None,
                 owner_email=owner, manager_emails=f"{owner},{manager}")
    j = Jarvis(s, client=FakeClient())
    sso = {"x-ms-client-principal-idp": "aad", "x-ms-client-principal-name": manager}
    with TestClient(create_app(s, j)) as c:
        r = c.post("/api/settings", json={"values": {"suggestions_publish_to_fsm": False}}, headers=sso)
        assert r.status_code == 403
    assert s.suggestions_publish_to_fsm is True


# ------------------------------------------------------------------------------------------------ the schedule
def test_the_schedule_runs_the_sync_every_15_minutes_and_the_poll_every_minute_by_default(settings):
    settings.scheduler_enabled = False
    j = Jarvis(settings, client=FakeClient())
    jobs = {job.id: job for job in build_scheduler(j).get_jobs()}
    sync, poll = jobs["fsm_suggestions"], jobs["fsm_suggestion_requests"]
    assert sync.trigger.interval == timedelta(minutes=15) and poll.trigger.interval == timedelta(seconds=60)
    assert sync.max_instances == poll.max_instances == 1 and sync.coalesce and poll.coalesce
    assert (settings.suggestions_fsm_interval_min, settings.suggestions_fsm_poll_s,
            settings.suggestions_fsm_hours_start, settings.suggestions_fsm_hours_end,
            settings.suggestions_prepare_max_per_hour) == (15, 60, 7, 19, 20)
    assert "suggestions" in jobs                                              # the existing sweep is untouched


async def test_outside_working_hours_the_sync_only_runs_hourly(world, monkeypatch):
    j, server = world
    svc = j.fsm_suggestions
    ran = []

    async def fake_sync(*a, **k):
        ran.append(1)
        return 0

    monkeypatch.setattr(svc, "sync", fake_sync)
    monkeypatch.setattr(svc, "_working_hours", lambda now=None: False)
    await svc.scheduled_sync()                                                # the first run after start always goes
    assert ran == [1]
    svc._last_full_run = time.monotonic() - 900                               # 15 minutes ago, at night: skipped
    await svc.scheduled_sync()
    assert ran == [1]
    svc._last_full_run = time.monotonic() - 3600                              # an hour ago: goes
    await svc.scheduled_sync()
    assert ran == [1, 1]
    monkeypatch.setattr(svc, "_working_hours", lambda now=None: True)
    svc._last_full_run = time.monotonic() - 900                               # in working hours every run goes
    await svc.scheduled_sync()
    assert ran == [1, 1, 1]


# ------------------------------------------------------------------------------------------------ quiet, and the legacy sweep
async def test_nothing_is_said_in_the_chat_or_pushed_as_a_notification(world):
    j, server = world
    q = j.bus.subscribe()
    await j.fsm_suggestions.sync(force_hours=True)
    server.press_prepare(Q1)
    await j.fsm_suggestions.poll()
    await j.fsm_suggestions.snooze_suggestion(Q4)
    events = []
    while not q.empty():
        events.append(q.get_nowait()["type"])
    j.bus.unsubscribe(q)
    assert events and set(events) <= {"suggestions", "approvals"}            # the drawer refreshing - no reply, display, alert or speech
    assert j.db.recent_notifications(50) == []
    assert j.db.query("SELECT 1 FROM transcript") == []


async def test_the_older_sweep_leaves_a_suggestion_with_a_prepare_button_alone(world, monkeypatch):
    j, server = world
    await j.fsm_suggestions.sync(force_hours=True)

    async def none():
        return []

    monkeypatch.setattr(j.suggestions, "_candidates", none)
    await j.suggestions.sweep(announce=False)
    assert {s["key"] for s in j.db.open_suggestions()} == {Q1, Q4}            # not "resolved" just because the sweep didn't list them


def test_the_briefing_wrapup_and_tool_text_know_about_prepare():
    assert "Prepare" in TOOLS_BY_NAME["suggestions"].description and "queues" in TOOLS_BY_NAME["suggestions"].description


def test_an_older_database_gains_the_kind_and_meta_columns(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE suggestions (key TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                      "title TEXT NOT NULL, detail TEXT DEFAULT '', prompt TEXT NOT NULL, priority INTEGER DEFAULT 2, "
                      "status TEXT NOT NULL DEFAULT 'open'); INSERT INTO suggestions VALUES ('unbilled','a','b','t','d','p',1,'open');")
    con.commit()
    con.close()
    db = Database(path)
    assert db.get_suggestion("unbilled")["kind"] == "" and db.kind_suggestions() == []
    db.upsert_suggestion("quote_followup:Q1", "t", "d", "p", 2, "quote_followup", "{}")
    assert [r["key"] for r in db.kind_suggestions()] == ["quote_followup:Q1"]


# ------------------------------------------------------------------------------------------------ the console endpoints
def app_for(settings, **kw):
    j, server = build(settings, **kw)
    return j, server, create_app(settings, j)


def test_the_console_prepare_button_runs_the_same_handler_and_is_idempotent(settings):
    j, server, app = app_for(settings, public_base_url="https://jarvis.example.test")
    asyncio.run(j.fsm_suggestions.sync(force_hours=True))
    with TestClient(app, base_url="https://jarvis.example.test") as c:
        ok = {"Sec-Fetch-Site": "same-origin", "Origin": "https://jarvis.example.test"}
        r = c.post(f"/api/suggestions/{Q1}/prepare", headers=ok)
        assert r.status_code == 200 and r.json()["status"] == "prepared" and r.json()["approval_id"]
        again = c.post(f"/api/suggestions/{Q1}/prepare", headers=ok).json()
        assert again["approval_id"] == r.json()["approval_id"] and len(j.db.pending_actions()) == 1
        assert c.post("/api/suggestions/unbilled/prepare", headers=ok).status_code == 404          # not a Prepare-able one
        assert c.post("/api/suggestions/quote_followup:NOPE/prepare", headers=ok).status_code == 404
        s = c.post(f"/api/suggestions/{Q4}/snooze", headers=ok)
        assert s.status_code == 200 and j.db.get_suggestion(Q4)["status"] == "dismissed"
        assert c.post("/api/suggestions/unbilled/snooze", headers=ok).status_code == 404
        assert KEY not in r.text + s.text
    assert j.db.get_action(r.json()["approval_id"])["status"] == "pending"


def test_a_console_prepare_of_a_suggestion_the_fsm_has_tells_the_fsm(settings):
    j, server, app = app_for(settings)
    asyncio.run(j.fsm_suggestions.sync(force_hours=True))                     # Q1 and Q4 are in the FSM now
    with TestClient(app) as c:
        assert c.post(f"/api/suggestions/{Q1}/prepare").json()["status"] == "prepared"
        assert c.post(f"/api/suggestions/{Q4}/prepare").json()["status"] == "failed"
    aid = j.db.pending_actions()[0]["id"]
    assert server.patches(Q1)[-1][1] == {"status": "prepared", "note": "Draft ready in Jarvis, waiting for approval.",
                                         "approval_ref": str(aid)}
    assert server.patches(Q4)[-1][1]["status"] == "failed"


def test_prepare_and_snooze_need_the_signed_in_owner_and_a_same_origin_click(settings):
    j, server, app = app_for(settings, jarvis_owner_password="a-long-password", public_base_url="https://jarvis.example.test")
    asyncio.run(j.fsm_suggestions.sync(force_hours=True))
    from jarvis import auth
    calls = [f"/api/suggestions/{Q1}/prepare", f"/api/suggestions/{Q1}/snooze"]
    with TestClient(app, base_url="https://jarvis.example.test", client=("203.0.113.5", 5000)) as c:
        for path in calls:
            assert c.post(path).status_code == 401
        c.cookies.set(auth.COOKIE, auth.make_session(settings))
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example.org"}, {"Origin": "null"}):
            for path in calls:
                assert c.post(path, headers=headers).status_code == 403
        assert j.db.pending_actions() == [] and j.db.get_suggestion(Q1)["status"] == "open"
        ok = {"Sec-Fetch-Site": "same-origin", "Origin": "https://jarvis.example.test"}
        assert c.post(calls[0], headers=ok).status_code == 200


def test_the_two_routes_sit_behind_owner_and_same_origin_and_before_the_catch_all():
    src = (Path(jarvis.__file__).parent / "main.py").read_text(encoding="utf-8")
    for tail in ("prepare", "snooze"):
        route = '"/api/suggestions/{key:path}/' + tail + '"'
        m = re.search(r"@app\.post\(" + re.escape(route) + r", dependencies=\[([^\]]*)\]\)", src)
        assert m and "Depends(owner)" in m.group(1) and "Depends(human_click)" in m.group(1), tail
        assert src.index(route) < src.index('"/api/suggestions/{key:path}/{decision}"')


# ------------------------------------------------------------------------------------------------ the model has no way in
def test_no_brain_tool_can_prepare_or_snooze_and_only_main_and_the_poller_call_them():
    for t in TOOLS:
        assert not re.search(r"prepare_sugg|snooze|fsm_sugg", t.name, re.I), t.name
        assert not [f for f in t.model.model_fields if re.search(r"prepare|snooze", f, re.I)], t.name
    root = Path(jarvis.__file__).parent
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        for needle in (r"\.prepare_suggestion\(", r"\.snooze_suggestion\("):
            if re.search(needle, text) and rel not in {"main.py", "services/fsm_suggestions.py"}:
                pytest.fail(f"{rel} calls {needle} - only the console routes and the poller may")
        if rel not in {"core.py", "services/scheduler.py", "main.py"} and re.search(r"j\.fsm_suggestions|\.fsm_suggestions\.", text):
            pytest.fail(f"{rel} reaches for j.fsm_suggestions")
    tools_src = (root / "brain" / "tools.py").read_text(encoding="utf-8")
    assert "fsm_suggestions" not in tools_src and "prepare_suggestion" not in tools_src


def test_a_team_member_is_refused_both_routes():
    from jarvis.access import MANAGER_OK, ROUTE_POLICY
    assert ROUTE_POLICY["POST /api/suggestions/{key:path}/prepare"] == MANAGER_OK
    assert ROUTE_POLICY["POST /api/suggestions/{key:path}/snooze"] == MANAGER_OK
