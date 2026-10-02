"""Predictive late-payment risk and quote win scoring: pure scoring, accountant wiring, suggestions and drafting.
Everything here is read-only; drafts are only ever shown, never sent."""

import json
from datetime import date, timedelta

from jarvis.integrations.finance import Invoice
from jarvis.services.documents import build_credit_control_context, build_followup_context
from jarvis.services.risk_scoring import score_payment_risk, score_quotes

TODAY = date(2026, 9, 30)


def inv(number, contact, due_in, amount_due=1000.0, status="authorised", total=None):
    """An invoice due `due_in` days from TODAY (negative = already overdue)."""
    due = TODAY + timedelta(days=due_in)
    total = total if total is not None else (amount_due or 1000.0)
    return Invoice("receivable", number, contact, due - timedelta(days=30), due, total, 0.0, amount_due, status)


def history(contact, paid, unpaid_overdue):
    """Earlier invoices for a customer: `paid` settled ones and `unpaid_overdue` still open past due."""
    rows = [inv(f"{contact[:3]}-P{n}", contact, -60 - n, amount_due=0.0, status="paid", total=500.0)
            for n in range(paid)]
    rows += [inv(f"{contact[:3]}-L{n}", contact, -20 - n) for n in range(unpaid_overdue)]
    return rows


def by_invoice(result):
    return {r["invoice"]: r for r in result["invoices"]}


# ---------------------------------------------------------------- late-payment risk

def test_habitual_late_payer_scores_higher_than_reliable_customer():
    invoices = (history("Slow Ltd", 1, 5) + history("Good Ltd", 6, 0)
                + [inv("S-NEW", "Slow Ltd", 5), inv("G-NEW", "Good Ltd", 5)])
    rows = by_invoice(score_payment_risk(invoices, TODAY))
    assert rows["S-NEW"]["score"] > rows["G-NEW"]["score"]
    assert rows["S-NEW"]["band"] == "high" and rows["G-NEW"]["band"] == "low"
    assert rows["S-NEW"]["status"] == "upcoming" and rows["S-NEW"]["days_overdue"] == -5
    assert any("still unpaid" in r for r in rows["S-NEW"]["reasons"])


def test_overdue_scores_higher_than_upcoming_and_worsens_with_age():
    invoices = [inv("A", "Acme", -3), inv("B", "Acme", -45), inv("C", "Acme", 4)]
    rows = by_invoice(score_payment_risk(invoices, TODAY))
    assert rows["B"]["score"] > rows["A"]["score"] > rows["C"]["score"]
    assert rows["A"]["status"] == "overdue" and rows["B"]["days_overdue"] == 45


def test_scores_are_bounded_and_sorted_worst_first():
    invoices = history("Slow Ltd", 0, 6) + [inv("BIG", "Slow Ltd", -90, amount_due=9000.0),
                                            inv("SMALL", "Fresh Ltd", 10, amount_due=50.0)]
    result = score_payment_risk(invoices, TODAY)
    scores = [r["score"] for r in result["invoices"]]
    assert all(0 <= s <= 100 for s in scores) and scores == sorted(scores, reverse=True)
    assert result["invoices"][0]["invoice"] == "BIG" and result["invoices"][0]["score"] == 100
    assert result["high_risk_count"] >= 1 and result["high_risk_value"] >= 9000.0


def test_paid_void_draft_and_far_future_invoices_are_not_scored():
    invoices = [inv("PAID", "Acme", -10, amount_due=0.0, status="paid", total=100.0),
                inv("VOID", "Acme", -10, status="voided"), inv("DRAFT", "Acme", -10, status="draft"),
                inv("LATER", "Acme", 60), inv("OPEN", "Acme", 3)]
    assert list(by_invoice(score_payment_risk(invoices, TODAY))) == ["OPEN"]


def test_thin_history_is_flagged_and_customers_are_not_mixed_up():
    invoices = history("Slow Ltd", 0, 5) + [inv("NEW", "Newbie Ltd", 2)]
    row = by_invoice(score_payment_risk(invoices, TODAY))["NEW"]
    assert row["band"] == "low" and any("little payment history" in r for r in row["reasons"])
    # contact names match ignoring case and spacing
    invoices = history("Slow Ltd", 0, 5) + [inv("NEW", "  slow  LTD ", 2)]
    assert by_invoice(score_payment_risk(invoices, TODAY))["NEW"]["band"] == "high"


def test_an_invoice_does_not_count_against_its_own_history():
    only = score_payment_risk([inv("ONE", "Solo Ltd", -10)], TODAY)["invoices"][0]
    assert any("little payment history" in r for r in only["reasons"])


def test_empty_input():
    result = score_payment_risk([], TODAY)
    assert result["invoices"] == [] and result["high_risk_count"] == 0 and result["high_risk_value"] == 0.0
    assert "estimate" in result["note"].lower()


# ---------------------------------------------------------------- quote scoring

def q(ref, customer, status="sent", age=10, value=2000, **extra):
    sent = (TODAY - timedelta(days=age)).isoformat() if age is not None else "not a date"
    return {"id": ref, "customer": customer, "status": status, "sent_date": sent, "value": value, "title": "Work",
            **extra}


def quote_rows(quotes):
    return {r["quote"]: r for r in score_quotes(quotes, TODAY)["open"]}


def test_customer_history_moves_the_win_score():
    past = ([q(f"W{n}", "Loyal Ltd", "accepted", 60) for n in range(3)]
            + [q(f"L{n}", "Picky Ltd", "declined", 60) for n in range(3)])
    rows = quote_rows(past + [q("A", "Loyal Ltd"), q("B", "Picky Ltd"), q("C", "Unknown Ltd")])
    assert rows["A"]["win_score"] > rows["C"]["win_score"] > rows["B"]["win_score"]
    assert rows["A"]["band"] == "likely" and rows["B"]["band"] == "unlikely"
    assert any("previous decided quotes" in r for r in rows["A"]["reasons"])


def test_stale_and_large_quotes_score_lower_and_remedials_higher():
    rows = quote_rows([q("FRESH", "X", age=5), q("STALE", "X", age=60), q("BIG", "X", age=5, value=20000),
                       q("REM", "X", age=5, type="remedial"), q("NODATE", "X", age=None)])
    assert rows["STALE"]["win_score"] < rows["FRESH"]["win_score"]
    assert rows["BIG"]["win_score"] < rows["FRESH"]["win_score"] < rows["REM"]["win_score"]
    assert rows["REM"]["remedial"] is True and rows["FRESH"]["remedial"] is False
    assert rows["NODATE"]["age_days"] is None and "sent date" in rows["NODATE"]["follow_up"]


def test_follow_ups_are_prioritised_by_expected_value_and_decided_quotes_excluded():
    result = score_quotes([q("SMALL", "X", value=300, age=10), q("MID", "X", value=3000, age=10),
                           q("NEW", "X", value=9000, age=2), q("WON", "X", "accepted", 5),
                           q("LOST", "X", "Declined", 5)], TODAY)
    refs = [r["quote"] for r in result["open"]]
    assert "WON" not in refs and "LOST" not in refs and result["open_count"] == 3
    assert refs[0] == "NEW" or refs.index("MID") < refs.index("SMALL")
    values = [r["expected_value"] for r in result["open"]]
    assert values == sorted(values, reverse=True)
    rows = {r["quote"]: r for r in result["open"]}
    assert rows["MID"]["follow_up"].startswith("due") and rows["NEW"]["follow_up"].startswith("not yet")
    assert result["weighted_pipeline"] == round(sum(values), 2)


def test_quote_scores_are_bounded_and_tolerate_bad_values():
    rows = quote_rows([q("X1", "A", value="not a number"), q("X2", "A", value=None, age=400)])
    assert all(5 <= r["win_score"] <= 95 for r in rows.values()) and rows["X1"]["value"] == 0.0
    assert score_quotes([], TODAY)["open"] == []


# ---------------------------------------------------------------- feeds into drafting

CC = {"actions": [{"invoice": "INV-1", "customer": "Acme Ltd", "amount_due": 500.0, "days_overdue": 3,
                   "action": "friendly reminder email"}]}


def test_credit_control_context_carries_the_risk_assessment():
    risk = [{"invoice": "INV-1", "customer": "Acme Ltd", "score": 72, "band": "high",
             "reasons": ["3 of 4 earlier invoices from this customer are still unpaid past their due date"]},
            {"invoice": "OTHER", "customer": "Zed", "score": 10, "band": "low", "reasons": []}]
    c, err = build_credit_control_context(CC, None, "INV-1", None, TODAY, 4.0, risk)
    assert err is None
    assessed = c["invoices"][0]["late_payment_risk"]
    assert assessed["score"] == 72 and assessed["band"] == "high" and assessed["reasons"]
    # without risk data the context is exactly as before
    c, _ = build_credit_control_context(CC, None, "INV-1", None, TODAY, 4.0)
    assert "late_payment_risk" not in c["invoices"][0]
    c, _ = build_credit_control_context(CC, None, "INV-1", None, TODAY, 4.0, [])
    assert "late_payment_risk" not in c["invoices"][0]


def test_followup_context_carries_the_win_assessment():
    quotes = [q("Q1", "Acme Ltd", age=15)]
    win = next(r for r in score_quotes(quotes, TODAY)["open"] if r["quote"] == "Q1")
    c, err = build_followup_context(quotes, "Q1", None, TODAY, win)
    assert err is None and c["internal_win_likelihood"]["score"] == win["win_score"]
    c, _ = build_followup_context(quotes, "Q1", None, TODAY)
    assert "internal_win_likelihood" not in c


# ---------------------------------------------------------------- wired into the running app

def _jarvis(tmp_path):
    from jarvis.config import Settings
    from jarvis.core import Jarvis
    from tests.fakes import FakeClient

    return Jarvis(Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None), client=FakeClient())


async def test_accountant_payment_risk_runs_on_demo_data_and_is_read_only(tmp_path):
    j = _jarvis(tmp_path)
    result = await j.accountant.payment_risk()
    assert result["invoices"] and all(0 <= r["score"] <= 100 for r in result["invoices"])
    kestrel = by_invoice(result)["INV-10388"]  # the demo's 105-days-overdue invoice
    assert kestrel["band"] == "high" and kestrel["status"] == "overdue"
    assert j.db.pending_actions() == []  # scoring queues nothing for approval
    await j.http.aclose()


async def test_suggestions_include_payment_risk_and_quote_priority(tmp_path, monkeypatch):
    from jarvis.services import risk_scoring

    j = _jarvis(tmp_path)

    async def fake_risk():
        return {"invoices": [
            {"invoice": "INV-9", "customer": "Slow Ltd", "amount_due": 2400.0, "due_date": "2026-10-03",
             "days_overdue": -3, "status": "upcoming", "score": 78, "band": "high",
             "reasons": ["4 of 5 earlier invoices from this customer are still unpaid past their due date"]},
            {"invoice": "INV-8", "customer": "Fine Ltd", "amount_due": 100.0, "due_date": "2026-10-03",
             "days_overdue": -3, "status": "upcoming", "score": 20, "band": "low", "reasons": []},
            {"invoice": "INV-7", "customer": "Ancient Ltd", "amount_due": 900.0, "due_date": "2026-06-01",
             "days_overdue": 120, "status": "overdue", "score": 100, "band": "high", "reasons": []}]}

    async def fake_pipeline(fsm, today=None):
        return {"open": [
            {"quote": "Q77", "customer": "Acme Ltd", "value": 8000.0, "win_score": 64, "band": "likely",
             "expected_value": 5120.0, "follow_up": "due - day 7+ since sent", "remedial": False, "age_days": 9,
             "reasons": ["won 3 of 4 previous decided quotes for this customer"]},
            {"quote": "Q78", "customer": "Early Ltd", "value": 5000.0, "win_score": 50, "band": "possible",
             "expected_value": 2500.0, "follow_up": "not yet - day 7 is 5 days away", "remedial": False,
             "age_days": 2, "reasons": []},
            {"quote": "RQ1", "customer": "Rem Ltd", "value": 400.0, "win_score": 70, "band": "likely",
             "expected_value": 280.0, "follow_up": "due - day 7+ since sent", "remedial": True, "age_days": 12,
             "reasons": []}]}

    monkeypatch.setattr(j.accountant, "payment_risk", fake_risk)
    monkeypatch.setattr(risk_scoring, "quote_pipeline", fake_pipeline)
    by_key = {c["key"]: c for c in await j.suggestions._candidates()}  # noqa: SLF001
    pay = by_key["payrisk"]
    assert "INV-9" in pay["prompt"] and "Slow Ltd" in pay["detail"]
    assert "INV-8" not in pay["prompt"] and "INV-7" not in pay["prompt"]  # low risk / already in the 30+ day chase
    assert "draft" in pay["prompt"].lower() and "approval" in pay["prompt"].lower()
    quote = by_key["quote-priority"]
    assert "Q77" in quote["prompt"] and "Q77" in quote["title"]
    assert "Q78" not in quote["prompt"] and "RQ1" not in quote["prompt"]  # not due yet / remedials have their own
    assert "draft_sales_followup" in quote["prompt"] and "approval" in quote["prompt"].lower()
    await j.http.aclose()


async def test_suggestions_survive_scoring_failures(tmp_path, monkeypatch):
    from jarvis.services import risk_scoring

    j = _jarvis(tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("source down")

    monkeypatch.setattr(j.accountant, "payment_risk", boom)
    monkeypatch.setattr(risk_scoring, "quote_pipeline", boom)
    keys = {c["key"] for c in await j.suggestions._candidates()}  # noqa: SLF001
    assert "payrisk" not in keys and "quote-priority" not in keys and "unbilled" in keys
    await j.http.aclose()


async def test_drafts_receive_the_scores_but_stay_drafts(tmp_path):
    j = _jarvis(tmp_path)
    assert await j.documents.credit_control_draft("INV-10388") == "Certainly, sir."
    call = j.client.beta.messages.calls[-1]
    data = json.loads(call["messages"][0]["content"])
    assert data["invoices"][0]["late_payment_risk"]["band"] == "high"
    assert "never" in call["system"].lower() and "late_payment_risk" in call["system"]
    assert await j.documents.sales_followup("Q1180", "email") == "Certainly, sir."
    call = j.client.beta.messages.calls[-1]
    data = json.loads(call["messages"][0]["content"])
    assert 5 <= data["internal_win_likelihood"]["score"] <= 95
    assert "internal_win_likelihood" in call["system"] and "DRAFT ONLY" in call["system"]
    assert j.db.pending_actions() == []
    await j.http.aclose()
