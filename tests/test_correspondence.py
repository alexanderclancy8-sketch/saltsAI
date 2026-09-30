"""Credit-control and sales-follow-up drafting: the data helpers, the Documents methods and the tool registration."""

import json
from datetime import date, timedelta

from jarvis.services.documents import build_credit_control_context, build_followup_context

TODAY = date(2026, 9, 30)

CC = {"actions": [
    {"invoice": "INV-1", "customer": "Acme Ltd", "amount_due": 500.0, "days_overdue": 3,
     "action": "friendly reminder email"},
    {"invoice": "INV-2", "customer": "Beta Ltd", "amount_due": 1200.0, "days_overdue": 15,
     "action": "second reminder + phone call to accounts payable"},
    {"invoice": "INV-3", "customer": "Kestrel Retail", "amount_due": 4056.0, "days_overdue": 75,
     "action": "letter before action with statutory interest and compensation",
     "statutory_interest": 100.5, "fixed_compensation": 100.0},
    {"invoice": "INV-4", "customer": "Kestrel Retail", "amount_due": 300.0, "days_overdue": 40,
     "action": "final notice", "statutory_interest": 5.0, "fixed_compensation": 40.0},
    {"invoice": "INV-5", "customer": "Kestrel Holdings", "amount_due": 90.0, "days_overdue": 50,
     "action": "letter before action", "statutory_interest": 1.0, "fixed_compensation": 40.0},
    {"invoice": "INV-6", "customer": "Gamma Ltd", "amount_due": 800.0, "days_overdue": 60,
     "action": "letter before action"},  # no interest supplied
]}
AGED = {"overdue_invoices": [
    {"number": "INV-1", "date": "2026-08-01", "due_date": "2026-09-27", "total": 500.0},
    {"number": "INV-3", "date": "2026-06-01", "due_date": "2026-07-17", "total": 4056.0},
]}


def ctx(query, channel=None, aged=AGED):
    c, err = build_credit_control_context(CC, aged, query, channel, TODAY, 4.0)
    return c, err


def test_channel_defaults_follow_the_stage():
    assert ctx("INV-1")[0]["channel"] == "email" and ctx("INV-1")[0]["stage_key"] == "reminder"
    assert ctx("INV-2")[0]["channel"] == "call" and ctx("INV-2")[0]["stage_key"] == "second_reminder"
    assert ctx("INV-4")[0]["channel"] == "email" and ctx("INV-4")[0]["stage_key"] == "final_notice"
    assert ctx("INV-3")[0]["channel"] == "letter" and ctx("INV-3")[0]["stage_key"] == "letter_before_action"
    assert ctx("INV-3")[0]["lba_deadline_days"] == 14 and ctx("INV-1")[0]["lba_deadline_days"] is None
    chosen = ctx("INV-1", "Call")[0]
    assert chosen["channel"] == "call" and chosen["channel_defaulted"] is False


def test_only_supplied_figures_are_used():
    c = ctx("INV-3")[0]
    inv = c["invoices"][0]
    assert inv["amount_due"] == 4056.0 and inv["statutory_interest"] == 100.5 and inv["fixed_compensation"] == 100.0
    assert inv["invoice_date"] == "2026-06-01" and inv["due_date"] == "2026-07-17" and inv["days_overdue"] == 75
    assert c["missing"] and any("bank" in m for m in c["missing"])
    early = ctx("INV-1")[0]["invoices"][0]
    assert "statutory_interest" not in early and "fixed_compensation" not in early  # not supplied, so not quoted
    assert "statutory_interest" not in ctx("INV-1")[0]["totals"]


def test_missing_data_is_flagged_not_invented():
    c = ctx("INV-6")[0]
    assert c["invoices"][0]["invoice_date"] is None and c["invoices"][0]["due_date"] is None
    assert any("invoice date for INV-6" in m for m in c["missing"])
    assert any("statutory interest" in m and "not supplied" in m for m in c["missing"])
    assert "statutory_interest" not in c["totals"] and c["interest_basis"] is None
    # no aged detail at all -> dates flagged missing, drafting still possible
    c2 = ctx("INV-3", aged=None)[0]
    assert any("due date for INV-3" in m for m in c2["missing"])


def test_customer_with_several_invoices_uses_the_worst_stage():
    c = ctx("kestrel retail")[0]
    assert [i["invoice"] for i in c["invoices"]] == ["INV-3", "INV-4"]
    assert c["stage_key"] == "letter_before_action"
    assert c["totals"] == {"amount_due": 4356.0, "statutory_interest": 105.5, "fixed_compensation": 140.0}


def test_mixed_interest_supply_omits_totals():
    cc = {"actions": [CC["actions"][2], CC["actions"][5] | {"customer": "Kestrel Retail"}]}
    c, err = build_credit_control_context(cc, None, "Kestrel Retail", None, TODAY)
    assert err is None and "statutory_interest" not in c["totals"]
    assert any("only supplied for some" in m for m in c["missing"])


def test_unknown_ambiguous_and_bad_input():
    c, err = ctx("Nobody Ltd")
    assert c is None and "couldn't find" in err
    c, err = ctx("kestrel")  # partial match on two different customers
    assert c is None and "Kestrel Holdings" in err and "Kestrel Retail" in err
    assert ctx("")[0] is None
    c, err = ctx("INV-1", "fax")
    assert c is None and "Channel" in err
    c, err = build_credit_control_context({"actions": []}, None, "INV-1", None, TODAY)
    assert c is None and "couldn't find" in err


def test_channel_notes_for_awkward_combinations():
    assert any("in writing" in n for n in ctx("INV-3", "call")[0]["channel_notes"])
    assert any("heavier" in n for n in ctx("INV-1", "letter")[0]["channel_notes"])
    assert ctx("INV-3")[0]["channel_notes"] == []


QUOTES = [
    {"id": "Q1", "title": "Panel upgrade", "customer": "Acme Ltd", "site": "Acme HQ", "value": 1500,
     "status": "sent", "sent_date": (TODAY - timedelta(days=15)).isoformat(), "created_by": "Josh"},
    {"id": "Q2", "title": "", "customer": "Beta Ltd", "site": None, "value": None, "status": "sent",
     "sent_date": "not a date"},
    {"id": "Q3", "title": "Done deal", "customer": "Gamma", "status": "accepted", "sent_date": "2026-09-01"},
    {"id": "Q4", "title": "Lost", "customer": "Delta", "status": "Declined", "sent_date": "2026-09-01"},
]


def test_followup_context_from_quote():
    c, err = build_followup_context(QUOTES, "q1", None, TODAY)
    assert err is None and c["channel"] == "email" and c["channel_defaulted"] is True
    q = c["quote"]
    assert (q["ref"], q["customer"], q["site"], q["value"], q["days_since_sent"], q["scope"]) == \
        ("Q1", "Acme Ltd", "Acme HQ", 1500, 15, "Panel upgrade")
    assert [(t["day"], t["status"]) for t in c["touches"]] == [(7, "already_passed"), (14, "due_now"), (21, "upcoming")]
    assert build_followup_context(QUOTES, "Q1", "call", TODAY)[0]["channel"] == "call"


def test_followup_missing_data_flagged():
    c, err = build_followup_context(QUOTES, "Q2", "email", TODAY)
    assert err is None and c["quote"]["days_since_sent"] is None and c["quote"]["date_sent"] is None
    assert {t["status"] for t in c["touches"]} == {"unknown"}
    for word in ("scope", "site", "value", "date the quote was sent", "contact"):
        assert any(word in m for m in c["missing"]), word


def test_followup_unknown_decided_or_bad_channel():
    c, err = build_followup_context(QUOTES, "Q99", None, TODAY)
    assert c is None and "couldn't find" in err
    assert build_followup_context(QUOTES, "", None, TODAY)[0] is None
    for ref in ("Q3", "Q4"):
        c, err = build_followup_context(QUOTES, ref, None, TODAY)
        assert c is None and "nothing to chase" in err
    c, err = build_followup_context(QUOTES, "Q1", "letter", TODAY)
    assert c is None and "Channel" in err


def _jarvis(tmp_path):
    from jarvis.config import Settings
    from jarvis.core import Jarvis
    from tests.fakes import FakeClient

    return Jarvis(Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None), client=FakeClient())


def _last_prompt(j):
    call = j.client.beta.messages.calls[-1]
    return call["system"], call["messages"][0]["content"]


async def test_credit_control_draft_end_to_end(tmp_path):
    j = _jarvis(tmp_path)
    events = j.bus.subscribe()
    assert await j.documents.credit_control_draft("INV-10388") == "Certainly, sir."  # Kestrel's old demo invoice
    system, prompt = _last_prompt(j)
    data = json.loads(prompt)
    assert data["stage_key"] == "letter_before_action" and data["channel"] == "letter"
    assert data["invoices"][0]["amount_due"] == 4056.0 and "statutory_interest" in data["invoices"][0]
    assert "DRAFT ONLY" in system and "solicitor" in system and "Pre-Action" in system
    shown = events.get_nowait()
    assert shown["type"] == "display" and "Kestrel Retail" in shown["data"]["title"]
    # unknown customer / bad channel: a plain message and no model call at all
    before = len(j.client.beta.messages.calls)
    assert "couldn't find" in await j.documents.credit_control_draft("Nobody At All Ltd")
    assert "Channel" in await j.documents.credit_control_draft("INV-10388", "carrier pigeon")
    assert len(j.client.beta.messages.calls) == before
    await j.http.aclose()


async def test_sales_followup_end_to_end(tmp_path):
    j = _jarvis(tmp_path)
    assert await j.documents.sales_followup("Q1180", "call") == "Certainly, sir."
    system, prompt = _last_prompt(j)
    data = json.loads(prompt)
    assert data["quote"]["ref"] == "Q1180" and data["quote"]["value"] == 14850 and data["quote"]["days_since_sent"] == 12
    assert data["channel"] == "call" and "No pressure" in system and "DRAFT ONLY" in system
    before = len(j.client.beta.messages.calls)
    assert "couldn't find" in await j.documents.sales_followup("Q-NOPE")
    assert "nothing to chase" in await j.documents.sales_followup("Q1175")  # already accepted in the demo data
    assert len(j.client.beta.messages.calls) == before
    await j.http.aclose()


async def test_correspondence_tools_are_registered_draft_only(tmp_path):
    from jarvis.brain.tools import (CreditControlDraftIn, SalesFollowupIn, TOOLS_BY_NAME, draft_credit_control,
                                    draft_sales_followup)

    for name in ("draft_credit_control", "draft_sales_followup"):
        tool = TOOLS_BY_NAME[name]
        assert tool.approval is False  # a draft on the display; sending is the approval-gated email_send
        assert "email_send" in tool.description and "DRAFTS ONLY" in tool.description
    assert TOOLS_BY_NAME["email_send"].approval is True
    j = _jarvis(tmp_path)
    result = await draft_credit_control(j, CreditControlDraftIn(target="INV-10388", channel=None))
    assert result["shown_on_display"] is True and result["draft"] == "Certainly, sir."
    result = await draft_sales_followup(j, SalesFollowupIn(quote_ref="Q1180", channel=None))
    assert result["shown_on_display"] is True and result["draft"] == "Certainly, sir."
    await j.http.aclose()
