"""Natural-language job logging and ad-hoc purchase ordering - both reuse the existing approval-gated
action queue, so the tools themselves should only ever prepare and queue, never write or send directly."""

from __future__ import annotations

import asyncio

import pytest

from jarvis.brain.tools import (AcceptQuoteIn, JobRefIn, LogJobIn, LogPurchaseOrderIn, PurchaseOrderLineIn,
                                accept_quote, job_detail, log_job, log_purchase_order)
from jarvis.core import Jarvis
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
