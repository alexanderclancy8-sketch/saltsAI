"""Natural-language job logging and ad-hoc purchase ordering - both reuse the existing approval-gated
action queue, so the tools themselves should only ever prepare and queue, never write or send directly."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from jarvis.brain.tools import (TOOLS_BY_NAME, AcceptQuoteIn, CreateCustomerIn, CreateSiteIn, JobRefIn, LogJobIn,
                                LogPurchaseOrderIn, PurchaseOrderLineIn, accept_quote, create_customer, create_site,
                                dispatch, job_detail, log_job, log_purchase_order)
from jarvis.core import Jarvis
from jarvis.integrations.fsm import DemoFSM, FSMClient
from tests.fakes import FakeClient


def make(settings):
    return Jarvis(settings, client=FakeClient())


# --------------------------------------------------------------------------- log_job
async def test_log_job_queues_an_fsm_write_with_only_the_given_fields(settings):
    j = make(settings)
    result = await log_job(j, LogJobIn(site="Beckfoot Upper Heaton", type="callout",
                                       description="Intruder alarm fault - zone 3 tamper"))
    pending = j.db.pending_actions()
    assert len(pending) == 1
    action = pending[0]
    assert action["kind"] == "fsm_write"
    assert action["payload"]["method"] == "POST" and action["payload"]["path"] == "/jobs"
    body = action["payload"]["body"]
    assert body == {"site": "Beckfoot Upper Heaton", "type": "callout",
                    "description": "Intruder alarm fault - zone 3 tamper", "created_by": "Jarvis"}
    assert "Beckfoot Upper Heaton" in action["summary"]
    assert result["queued_action"] == action["id"]
    await j.http.aclose()


async def test_log_job_includes_engineer_and_date_when_given(settings):
    j = make(settings)
    result = await log_job(j, LogJobIn(site="Aire Valley Care Home", type="remedial",
                                       description="Replace failed smoke detector", priority="24h",
                                       engineer="Dan Harper", scheduled_start="2026-10-06T09:00",
                                       customer="Aire Valley Care Ltd"))
    body = j.db.pending_actions()[0]["payload"]["body"]
    assert body["priority"] == "24h" and body["engineer"] == "Dan Harper"
    assert body["scheduled_start"] == "2026-10-06T09:00" and body["customer"] == "Aire Valley Care Ltd"
    summary = j.db.pending_actions()[0]["summary"]
    # the summary is read out loud / shown on the approval card, so it gets the human phrasing, not raw ISO
    assert "Dan Harper" in summary and "Tuesday 6 October at 9am" in summary
    assert result["note"] == "Queued for approval on the display."
    await j.http.aclose()


async def test_log_job_does_not_write_to_fsm_directly(settings, monkeypatch):
    j = make(settings)
    called = []
    monkeypatch.setattr(j.fsm, "write", lambda *a, **k: called.append((a, k)))
    await log_job(j, LogJobIn(site="Otley Road Hotel", description="Fire alarm panel fault"))
    assert called == []  # nothing but the queued action - approval hasn't happened yet
    await j.http.aclose()


# --------------------------------------------------------------------------- log_purchase_order
async def test_log_purchase_order_resolves_items_and_prices_from_stores(settings):
    j = make(settings)
    result = await log_purchase_order(j, LogPurchaseOrderIn(
        supplier="Security Distribution UK", supplier_email="orders@secdist.example.co.uk",
        items=[PurchaseOrderLineIn(item="BAT-12V7", qty=10), PurchaseOrderLineIn(item="PIR-G2", qty=4)]))
    assert "not_ordered" not in result
    lines = {l["sku"]: l for l in result["lines"]}
    assert lines["BAT-12V7"]["qty"] == 10 and lines["BAT-12V7"]["unit_cost"] == 14.5
    assert lines["BAT-12V7"]["line_cost"] == 145.0
    assert lines["PIR-G2"]["line_cost"] == pytest.approx(4 * 19.0)
    assert result["total_ex_vat"] == pytest.approx(145.0 + 4 * 19.0)

    pending = j.db.pending_actions()
    assert len(pending) == 1 and pending[0]["kind"] == "email_send"
    payload = pending[0]["payload"]
    assert payload["to"] == ["orders@secdist.example.co.uk"]
    assert "12V 7Ah SLA battery" in payload["body"] and "Grade 2 PIR detector" in payload["body"]
    assert f"{result['total_ex_vat']:,.2f}" in payload["body"]
    assert result["queued_action"] == pending[0]["id"]
    await j.http.aclose()


async def test_log_purchase_order_reports_items_that_do_not_resolve_but_still_orders_the_rest(settings):
    j = make(settings)
    result = await log_purchase_order(j, LogPurchaseOrderIn(
        supplier="Fire Alarm Wholesale Ltd", supplier_email="sales@fawl.example.co.uk",
        items=[PurchaseOrderLineIn(item="BAT-12V7", qty=2), PurchaseOrderLineIn(item="NONEXISTENT-SKU", qty=1)]))
    assert len(result["lines"]) == 1 and result["lines"][0]["sku"] == "BAT-12V7"
    assert len(result["not_ordered"]) == 1 and "NONEXISTENT-SKU" in result["not_ordered"][0]
    pending = j.db.pending_actions()
    assert len(pending) == 1  # still queues an order for the part that did resolve
    await j.http.aclose()


async def test_log_purchase_order_with_no_resolvable_items_queues_nothing(settings):
    j = make(settings)
    result = await log_purchase_order(j, LogPurchaseOrderIn(
        supplier="Nobody", supplier_email="nobody@example.com",
        items=[PurchaseOrderLineIn(item="TOTALLY-UNKNOWN", qty=1)]))
    assert "error" in result and result["not_ordered"]
    assert j.db.pending_actions() == []
    await j.http.aclose()


async def test_log_purchase_order_does_not_send_email_directly(settings, monkeypatch):
    j = make(settings)
    called = []
    monkeypatch.setattr(j.mail, "send_mail", lambda *a, **k: called.append((a, k)))
    await log_purchase_order(j, LogPurchaseOrderIn(
        supplier="Security Distribution UK", supplier_email="orders@secdist.example.co.uk",
        items=[PurchaseOrderLineIn(item="BAT-12V7", qty=1)]))
    assert called == []  # queued only - nothing sent until the owner approves
    await j.http.aclose()


# --------------------------------------------------------------------------- job_detail ("job 360")
async def test_job_detail_returns_the_full_picture_not_just_summary_fields(settings):
    j = make(settings)
    result = await job_detail(j, JobRefIn(job_ref="J24100"))
    assert result["id"] == "J24100" and result["ref"] == "J24100"
    # every demo job has at least been scheduled, whatever its current status is when the test happens to run
    assert "extra" in result and "status_history" in result["extra"]
    statuses = [h["status"] for h in result["extra"]["status_history"]]
    assert "scheduled" in statuses
    await j.http.aclose()


async def test_job_detail_works_by_ref_too(settings):
    j = make(settings)
    by_id = await job_detail(j, JobRefIn(job_ref="J24100"))
    by_ref = await job_detail(j, JobRefIn(job_ref=by_id["ref"]))
    assert by_id == by_ref
    await j.http.aclose()


async def test_job_detail_raises_a_clear_error_for_an_unknown_job(settings):
    j = make(settings)
    with pytest.raises(ValueError, match="J99999"):
        await job_detail(j, JobRefIn(job_ref="J99999"))
    await j.http.aclose()


async def test_fsm_router_reconnects_live_when_the_web_address_is_saved(settings):
    # self.fsm is only ever built once in Jarvis.__init__ - lots of other services capture a direct reference
    # to it. It must stay on demo data while unconfigured, then switch itself to the real client the moment
    # the address is set on the live Settings object, with no Jarvis rebuild and no restart needed.
    j = make(settings)
    assert j.fsm.demo is True
    assert (await j.fsm.jobs())  # demo data answers straight away

    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return []

    j.fsm._real.get = fake_get  # noqa: SLF001 - only reached once the router stops routing to demo
    settings.fsm_base_url = "https://fsm.saltsfireandsecurity.co.uk"
    assert j.fsm.demo is False
    await j.fsm.get("/jobs")  # missing on the router itself, so this proves __getattr__ now delegates here
    assert calls == ["/jobs"]
    await j.http.aclose()


async def test_stores_also_reconnects_live_instead_of_staying_stuck_on_demo(settings):
    # Stores decided once at construction time whether Salts FSM was configured and, if not, threw the
    # reference away for good - so stock never actually reconnected even after the owner saved the web
    # address, unlike every other service that holds the live FSMRouter. Same fix, same proof.
    j = make(settings)
    assert j.stores.fsm is j.fsm  # not resolved away to None just because FSM started out in demo mode
    await j.stores.sync()
    assert j.stores.source == "Jarvis stock ledger"  # still demo - nothing to sync from yet

    calls = []

    async def fake_stock():
        calls.append("stock")
        return []

    async def fake_moves(date_from, date_to):
        return []

    j.fsm._real.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should use fsm.stock(), not get()"))
    j.fsm._real.stock = fake_stock
    j.fsm._real.stock_movements = fake_moves
    settings.fsm_base_url = "https://fsm.saltsfireandsecurity.co.uk"
    assert j.fsm.demo is False

    await j.stores.sync(force=True)
    assert calls == ["stock"] and j.stores.source == "Salts FSM"
    await j.http.aclose()


async def test_real_fsm_client_calls_the_jobs_detail_path_and_normalises_extras():
    import httpx

    from jarvis.config import Settings
    from jarvis.integrations.fsm import FSMClient

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={
            "jobId": "J24100", "reference": "J24100", "jobStatus": "completed",
            "materials_used": [{"sku": "BAT-12V7", "qty": 2}], "linked_invoice": "INV-30412"})

    s = Settings(fsm_base_url="https://fsm.example.co.uk", fsm_api_key="test-key", _env_file=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await FSMClient(s, http).job_detail("J24100")
    assert seen["url"] == "https://fsm.example.co.uk/api/jobs/J24100"
    assert result["id"] == "J24100" and result["status"] == "completed"  # aliased from jobId/jobStatus
    assert result["extra"] == {"materials_used": [{"sku": "BAT-12V7", "qty": 2}], "linked_invoice": "INV-30412"}


# --------------------------------------------------------------------------- accept_quote
async def test_accept_quote_queues_a_combined_accept_and_book_action(settings):
    j = make(settings)
    result = await accept_quote(j, AcceptQuoteIn(quote_ref="Q1180", engineer="Dan Harper",
                                                 scheduled_start="2026-10-06T09:00"))
    assert "queued_action" in result
    pending = j.db.pending_actions()
    assert len(pending) == 1 and pending[0]["kind"] == "accept_quote"
    assert "Q1180" in pending[0]["summary"] and "Wharfedale Academy Trust" in pending[0]["summary"]
    payload = pending[0]["payload"]
    assert payload["quote_id"] == "Q1180"
    assert payload["job_body"] == {
        "site": "Ilkley Grammar Annexe", "type": "install", "description": "Vigilon panel upgrade, block B",
        "created_by": "Jarvis", "customer": "Wharfedale Academy Trust", "engineer": "Dan Harper",
        "scheduled_start": "2026-10-06T09:00"}
    await j.http.aclose()


async def test_accept_quote_rejects_a_quote_already_accepted(settings):
    j = make(settings)
    result = await accept_quote(j, AcceptQuoteIn(quote_ref="Q1175"))  # already "accepted" in demo data
    assert "error" in result and "already" in result["error"]
    assert j.db.pending_actions() == []
    await j.http.aclose()


async def test_accept_quote_reports_an_unknown_quote(settings):
    j = make(settings)
    result = await accept_quote(j, AcceptQuoteIn(quote_ref="Q99999"))
    assert "error" in result and "Q99999" in result["error"]
    assert j.db.pending_actions() == []
    await j.http.aclose()


async def test_accept_quote_handles_a_quote_with_no_value_or_title(settings):
    # normalise() always sets "value"/"title" for every quote, even to None when Salts FSM's own record
    # doesn't have one - .get(key, default) does NOT protect against that (the key is present, just None),
    # so building the summary must guard with "or" the same way the rest of the codebase does.
    j = make(settings)

    async def fake_quotes(status=None):
        return [{"id": "Q1", "title": None, "customer": "Acme Ltd", "site": "Acme Site", "value": None,
                "status": "sent"}]

    j.fsm.quotes = fake_quotes
    result = await accept_quote(j, AcceptQuoteIn(quote_ref="Q1"))
    assert "queued_action" in result
    assert "Q1" in j.db.pending_actions()[0]["summary"]
    await j.http.aclose()


async def test_approving_accept_quote_writes_both_the_status_and_the_job(settings):
    j = make(settings)
    calls = []

    async def fake_write(method, path, body=None):
        calls.append((method, path, body))
        return {"ok": True}

    j.fsm.write = fake_write
    result = await accept_quote(j, AcceptQuoteIn(quote_ref="Q1180"))
    await j.actions.approve(result["queued_action"])
    await asyncio.sleep(0.05)  # the approval runs the write in a spawned task

    assert calls[0] == ("PATCH", "/quotes/Q1180", {"status": "accepted"})
    assert calls[1][0] == "POST" and calls[1][1] == "/jobs" and calls[1][2]["site"] == "Ilkley Grammar Annexe"
    assert j.db.get_action(result["queued_action"])["status"] == "done"
    await j.http.aclose()


# --------------------------------------------------------------------------- create_customer / create_site
# Same rules as log_job: the tools check and prepare, and ONLY queue an `fsm_write` - nothing is created in Salts FSM
# until the owner approves it. The demo FSM stands in for Salts FSM (Aire Valley Care Ltd is one of its customers).
def _spy_write(j):
    calls = []
    real = j.fsm.write

    async def spy(method, path, body=None):
        calls.append((method, path, body))
        return await real(method, path, body)

    j.fsm.write = spy
    return calls


async def test_create_customer_queues_an_fsm_write_and_does_not_create_anything(settings):
    j = make(settings)
    calls = _spy_write(j)
    result = await create_customer(j, CreateCustomerIn(
        name="  Brightwell Dental Ltd ", contact="Dr Amy Brightwell", phone="0113 555 0100",
        email="reception@brightwell.example.co.uk", billing_address="1 High Street\nLeeds\nLS1 2AB"))
    pending = j.db.pending_actions()
    assert len(pending) == 1 and result["queued_action"] == pending[0]["id"]
    action = pending[0]
    assert action["kind"] == "fsm_write"
    assert action["payload"]["method"] == "POST" and action["payload"]["path"] == "/customers"
    assert action["payload"]["body"] == {
        "name": "Brightwell Dental Ltd", "created_by": "Jarvis", "contact": "Dr Amy Brightwell",
        "phone": "0113 555 0100", "email": "reception@brightwell.example.co.uk",
        "billingAddress": "1 High Street\nLeeds\nLS1 2AB"}
    assert action["summary"].startswith("Create customer Brightwell Dental Ltd")
    assert "Dr Amy Brightwell" in action["summary"]
    assert result["note"] == "Queued for approval on the display."
    assert calls == []  # nothing written - approval hasn't happened yet
    assert not any(c["name"] == "Brightwell Dental Ltd" for c in await j.fsm.customers())
    await j.http.aclose()


async def test_create_customer_omits_blank_fields(settings):
    j = make(settings)
    await create_customer(j, CreateCustomerIn(name="Brightwell Dental Ltd"))
    assert j.db.pending_actions()[0]["payload"]["body"] == {"name": "Brightwell Dental Ltd", "created_by": "Jarvis"}
    await j.http.aclose()


@pytest.mark.parametrize("name", ["", "   ", "n/a", "TBC", "Unknown", "new customer", "-"])
async def test_create_customer_refuses_an_empty_or_placeholder_name(settings, name):
    j = make(settings)
    result = await create_customer(j, CreateCustomerIn(name=name))
    assert "error" in result and j.db.pending_actions() == []
    await j.http.aclose()


async def test_create_customer_refuses_a_bad_email_before_queueing(settings):
    j = make(settings)
    result = await create_customer(j, CreateCustomerIn(name="Brightwell Dental Ltd", email="not-an-email"))
    assert "error" in result and "email" in result["error"] and j.db.pending_actions() == []
    await j.http.aclose()


@pytest.mark.parametrize("typed", ["Aire Valley Care Ltd", "aire valley care ltd ", "Aire Valley Care Limited",
                                   "Aire Valley Care", "The Aire Valley Care Ltd.", "Aire Vally Care Ltd"])
async def test_create_customer_reports_an_existing_match_instead_of_queueing_a_duplicate(settings, typed):
    j = make(settings)
    result = await create_customer(j, CreateCustomerIn(name=typed))
    assert result["queued"] is False and j.db.pending_actions() == []
    assert [c["name"] for c in result["likely_existing_customers"]] == ["Aire Valley Care Ltd"]
    assert result["likely_existing_customers"][0]["id"]
    assert "confirm_not_duplicate" in result["note"]
    await j.http.aclose()


async def test_a_clearly_different_customer_is_not_mistaken_for_a_duplicate(settings):
    j = make(settings)
    result = await create_customer(j, CreateCustomerIn(name="Airedale Plumbing Supplies"))
    assert "queued_action" in result
    await j.http.aclose()


async def test_confirmed_namesake_is_flagged_for_the_fsm_and_loud_in_the_summary(settings):
    j = make(settings)
    result = await create_customer(j, CreateCustomerIn(name="aire valley care ltd", confirm_not_duplicate=True))
    action = j.db.pending_actions()[0]
    assert result["queued_action"] == action["id"]
    # Salts FSM refuses an exact-name second customer unless it is told it's deliberate
    assert action["payload"]["body"]["confirmSharedName"] is True
    assert "already exists" in action["summary"] and "second" in action["summary"]
    await j.http.aclose()


async def test_confirmed_near_match_does_not_claim_a_shared_name(settings):
    j = make(settings)
    await create_customer(j, CreateCustomerIn(name="Aire Valley Care Limited", confirm_not_duplicate=True))
    assert "confirmSharedName" not in j.db.pending_actions()[0]["payload"]["body"]
    await j.http.aclose()


async def test_create_customer_does_not_queue_the_same_customer_twice(settings):
    j = make(settings)
    first = await create_customer(j, CreateCustomerIn(name="Brightwell Dental Ltd"))
    second = await create_customer(j, CreateCustomerIn(name="brightwell dental limited"))
    assert second["already_pending_action"] == first["queued_action"]
    assert len(j.db.pending_actions()) == 1
    await j.http.aclose()


async def test_create_customer_will_not_queue_blind_when_the_fsm_cannot_be_checked(settings):
    j = make(settings)

    async def boom():
        raise RuntimeError("connection refused")

    j.fsm.customers = boom
    result = await create_customer(j, CreateCustomerIn(name="Brightwell Dental Ltd"))
    assert "error" in result and "connection refused" in result["error"] and j.db.pending_actions() == []
    await j.http.aclose()


async def test_approving_create_customer_posts_the_right_body_to_the_fsm(settings):
    j = make(settings)
    calls = _spy_write(j)
    result = await create_customer(j, CreateCustomerIn(name="Brightwell Dental Ltd", phone="0113 555 0100"))
    await j.actions.approve(result["queued_action"])
    await asyncio.sleep(0.05)  # the approval runs the write in a spawned task
    assert calls == [("POST", "/customers", {"name": "Brightwell Dental Ltd", "created_by": "Jarvis",
                                             "phone": "0113 555 0100"})]
    assert j.db.get_action(result["queued_action"])["status"] == "done"
    # the (demo) FSM now has it, so asking again is reported as a duplicate rather than queued
    again = await create_customer(j, CreateCustomerIn(name="Brightwell Dental Ltd"))
    assert again["queued"] is False and again["likely_existing_customers"][0]["name"] == "Brightwell Dental Ltd"
    await j.http.aclose()


async def test_create_site_queues_an_fsm_write_for_an_existing_customer_by_id(settings):
    j = make(settings)
    calls = _spy_write(j)
    customer = next(c for c in await j.fsm.customers() if c["name"] == "Aire Valley Care Ltd")
    result = await create_site(j, CreateSiteIn(name="Aire Valley Care - Annexe", customer="aire valley care ltd",
                                               address="2 Mill Lane, Bingley", postcode=" bd16 1ab "))
    action = j.db.pending_actions()[0]
    assert result["queued_action"] == action["id"]
    assert action["kind"] == "fsm_write"
    assert action["payload"]["method"] == "POST" and action["payload"]["path"] == "/sites"
    assert action["payload"]["body"] == {"name": "Aire Valley Care - Annexe", "created_by": "Jarvis",
                                         "customer": customer["id"],  # the id, not the (shareable) name
                                         "address": "2 Mill Lane, Bingley", "postcode": "BD16 1AB"}
    assert "for Aire Valley Care Ltd" in action["summary"] and "BD16 1AB" in action["summary"]
    assert calls == []
    await j.http.aclose()


async def test_create_site_with_no_customer_warns_that_no_job_can_be_booked(settings):
    j = make(settings)
    result = await create_site(j, CreateSiteIn(name="Lone Warehouse Unit 4"))
    assert "queued_action" in result and "customer" not in j.db.pending_actions()[0]["payload"]["body"]
    assert "no customer" in j.db.pending_actions()[0]["summary"].lower() and "warning" in result
    await j.http.aclose()


@pytest.mark.parametrize("name", ["", "  ", "tbc", "New site"])
async def test_create_site_refuses_an_empty_or_placeholder_name(settings, name):
    j = make(settings)
    result = await create_site(j, CreateSiteIn(name=name, customer="Aire Valley Care Ltd"))
    assert "error" in result and j.db.pending_actions() == []
    await j.http.aclose()


async def test_create_site_for_an_unknown_customer_says_to_create_the_customer_first(settings):
    j = make(settings)
    result = await create_site(j, CreateSiteIn(name="Brightwell Dental - Roundhay", customer="Brightwell Dental Ltd"))
    assert result["queued"] is False and "create_customer" in result["error"]
    assert j.db.pending_actions() == []
    # a typo of a real customer is offered back as a close match
    result = await create_site(j, CreateSiteIn(name="Annexe", customer="Aire Valey Care Ltd"))
    assert [c["name"] for c in result["close_matches"]] == ["Aire Valley Care Ltd"]
    await j.http.aclose()


async def test_create_site_waits_for_a_customer_that_is_still_pending_approval(settings):
    j = make(settings)
    queued = await create_customer(j, CreateCustomerIn(name="Brightwell Dental Ltd"))
    result = await create_site(j, CreateSiteIn(name="Brightwell Dental - Roundhay", customer="Brightwell Dental Ltd"))
    assert result["queued"] is False and result["customer_pending_action"] == queued["queued_action"]
    assert len(j.db.pending_actions()) == 1  # only the customer - the site was NOT queued against a customer that
    await j.http.aclose()                    # doesn't exist yet


async def test_create_site_asks_which_customer_when_a_name_is_shared(settings):
    j = make(settings)
    await j.fsm.write("POST", "/customers", {"name": "Aire Valley Care Ltd", "confirmSharedName": True})
    result = await create_site(j, CreateSiteIn(name="New Wing", customer="Aire Valley Care Ltd"))
    assert result["queued"] is False and len(result["ambiguous_customer"]) == 2
    assert j.db.pending_actions() == []
    # ...and the id is accepted
    chosen = result["ambiguous_customer"][1]["id"]
    ok = await create_site(j, CreateSiteIn(name="New Wing", customer=chosen))
    assert "queued_action" in ok and j.db.pending_actions()[0]["payload"]["body"]["customer"] == chosen
    await j.http.aclose()


async def test_create_site_reports_an_existing_site_instead_of_queueing_a_duplicate(settings):
    j = make(settings)
    for typed in ("Aire Valley Care Home", "aire valley care home", "Aire Valley Care Homes"):
        result = await create_site(j, CreateSiteIn(name=typed, customer="Aire Valley Care Ltd"))
        assert result["queued"] is False
        assert [s["name"] for s in result["likely_existing_sites"]] == ["Aire Valley Care Home"]
    assert j.db.pending_actions() == []
    await j.http.aclose()


async def test_confirmed_twin_site_is_flagged_for_the_fsm(settings):
    j = make(settings)
    await create_site(j, CreateSiteIn(name="Aire Valley Care Home", customer="Aire Valley Care Ltd",
                                      confirm_not_duplicate=True))
    action = j.db.pending_actions()[0]
    assert action["payload"]["body"]["confirmSharedName"] is True and "second" in action["summary"]
    await j.http.aclose()


async def test_the_same_postcode_with_a_similar_name_is_flagged(settings):
    j = make(settings)
    await j.fsm.write("POST", "/sites", {"name": "Moorside Depot", "postcode": "BD1 1AA"})
    result = await create_site(j, CreateSiteIn(name="Moorside Depot Ltd Yard", postcode="bd1 1aa"))
    assert result["queued"] is False and result["likely_existing_sites"][0]["name"] == "Moorside Depot"
    await j.http.aclose()


async def test_create_site_does_not_queue_the_same_site_twice(settings):
    j = make(settings)
    first = await create_site(j, CreateSiteIn(name="Brightwell Dental - Roundhay", customer="Aire Valley Care Ltd"))
    second = await create_site(j, CreateSiteIn(name="brightwell dental - roundhay", customer="Aire Valley Care Ltd"))
    assert second["already_pending_action"] == first["queued_action"] and len(j.db.pending_actions()) == 1
    await j.http.aclose()


async def test_approving_create_site_posts_the_right_body_and_the_new_site_is_then_found(settings):
    j = make(settings)
    calls = _spy_write(j)
    result = await create_site(j, CreateSiteIn(name="Aire Valley Care - Annexe", customer="Aire Valley Care Ltd",
                                               postcode="BD16 1AB"))
    await j.actions.approve(result["queued_action"])
    await asyncio.sleep(0.05)
    assert len(calls) == 1 and calls[0][0] == "POST" and calls[0][1] == "/sites"
    assert calls[0][2]["name"] == "Aire Valley Care - Annexe" and calls[0][2]["postcode"] == "BD16 1AB"
    assert j.db.get_action(result["queued_action"])["status"] == "done"
    created = next(s for s in await j.fsm.sites() if s["name"] == "Aire Valley Care - Annexe")
    assert created["customer"] == "Aire Valley Care Ltd" and created["customer_id"]
    await j.http.aclose()


async def test_new_customer_then_its_site_end_to_end(settings):
    j = make(settings)
    c = await create_customer(j, CreateCustomerIn(name="Brightwell Dental Ltd"))
    assert "customer_pending_action" in await create_site(j, CreateSiteIn(name="Roundhay", customer="Brightwell Dental Ltd"))
    await j.actions.approve(c["queued_action"])
    await asyncio.sleep(0.05)
    s = await create_site(j, CreateSiteIn(name="Brightwell Dental - Roundhay", customer="Brightwell Dental Ltd"))
    assert "queued_action" in s
    await j.actions.approve(s["queued_action"])
    await asyncio.sleep(0.05)
    site = next(x for x in await j.fsm.sites() if x["name"] == "Brightwell Dental - Roundhay")
    assert site["customer"] == "Brightwell Dental Ltd"
    await j.http.aclose()


def test_the_new_tools_are_registered_and_say_they_need_approval():
    for name in ("create_customer", "create_site"):
        tool = TOOLS_BY_NAME[name]
        assert "approval" in tool.description.lower()
        assert tool.approval is False  # like log_job: the handler queues the write itself, after its checks


async def test_through_dispatch_a_customer_is_only_queued(settings):
    j = make(settings)
    calls = _spy_write(j)
    tool = TOOLS_BY_NAME["create_customer"]
    result = await dispatch(j, tool, tool.model.model_validate({"name": "Brightwell Dental Ltd"}))
    assert result["queued_action"] and calls == []
    assert [a["kind"] for a in j.db.pending_actions()] == ["fsm_write"]
    await j.http.aclose()


# ---- the demo FSM behaves like the real one for these two routes
async def test_demo_fsm_refuses_a_namesake_customer_unless_confirmed():
    fsm = DemoFSM()
    with pytest.raises(ValueError, match="CUSTOMER_NAME_SHARED"):
        await fsm.write("POST", "/customers", {"name": "aire valley care ltd"})
    made = await fsm.write("POST", "/customers", {"name": "aire valley care ltd", "confirmSharedName": True})
    assert made["ok"] is True and made["id"]
    assert len([c for c in await fsm.customers() if c["name"].lower() == "aire valley care ltd"]) == 2
    with pytest.raises(ValueError, match="required"):
        await fsm.write("POST", "/customers", {"name": " "})


async def test_demo_fsm_site_needs_a_real_unambiguous_customer():
    fsm = DemoFSM()
    with pytest.raises(ValueError, match="404"):
        await fsm.write("POST", "/sites", {"name": "X", "customer": "Nobody Ltd"})
    made = await fsm.write("POST", "/sites", {"name": "New Wing", "customer": "Aire Valley Care Ltd", "postcode": "bd1 1aa"})
    assert made["ok"] and made["customer"] == "Aire Valley Care Ltd"
    site = next(s for s in await fsm.sites() if s["name"] == "New Wing")
    assert site["postcode"] == "BD1 1AA" and site["lat"] and site["customer_id"] == made["customerId"]
    with pytest.raises(ValueError, match="SITE_NAME_SHARED"):
        await fsm.write("POST", "/sites", {"name": "new wing", "customer": "Aire Valley Care Ltd"})


async def test_demo_fsm_still_ignores_other_writes():
    result = await DemoFSM().write("PATCH", "/jobs/J1", {"status": "done"})
    assert result["demo"] is True and "not applied" in result["status"]


# ---- the real client: field mapping and error detail
def _client(settings, handler):
    settings.fsm_base_url = "https://fsm.example.test"
    settings.fsm_api_prefix = "/api/jarvis"
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FSMClient(settings, http), http


async def test_fsm_client_reads_customers_and_site_customer_ids(settings):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/customers"):
            return httpx.Response(200, json={"items": [{"id": "cust-1", "name": "Priory Care Homes Ltd",
                                                         "accountRef": "AC001", "billingAddress": "1 High St",
                                                         "phone": "0113", "email": "a@b.co", "status": "Live",
                                                         "onHold": False, "notes": None}]})
        return httpx.Response(200, json={"items": [{"id": "site-1", "name": "Oakfield", "customerId": "cust-1",
                                                     "customer": "Priory Care Homes Ltd", "postcode": "LS1 1AA"}]})

    fsm, http = _client(settings, handler)
    customers, sites = await fsm.customers(), await fsm.sites()
    assert seen == ["/api/jarvis/customers", "/api/jarvis/sites"]
    assert customers[0]["id"] == "cust-1" and customers[0]["account_ref"] == "AC001"
    assert customers[0]["billing_address"] == "1 High St" and customers[0]["on_hold"] is False
    assert sites[0]["customer_id"] == "cust-1" and sites[0]["customer"] == "Priory Care Homes Ltd"
    await http.aclose()


async def test_fsm_client_write_says_why_the_fsm_refused(settings):
    def handler(request):
        assert request.method == "POST" and request.url.path == "/api/jarvis/customers"
        return httpx.Response(409, json={"detail": '{"code": "CUSTOMER_NAME_SHARED", "error": "A customer called '
                                                   'X already exists."}'})

    fsm, http = _client(settings, handler)
    with pytest.raises(httpx.HTTPStatusError) as err:
        await fsm.write("POST", "/customers", {"name": "X"})
    assert "409" in str(err.value) and "CUSTOMER_NAME_SHARED" in str(err.value)
    await http.aclose()
