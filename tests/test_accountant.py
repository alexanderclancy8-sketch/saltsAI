from datetime import date, timedelta

from jarvis.integrations.finance import Invoice
from jarvis.services.accountant import (age_invoices, cashflow_forecast, corporation_tax, credit_control_stage,
                                        late_payment_claim, vat_estimate, vat_quarter)


def inv(kind="receivable", days_overdue=0, due=100.0, total=120.0, tax=20.0, d=None, today=date(2026, 9, 28)):
    due_date = today - timedelta(days=days_overdue)
    return Invoice(kind, "INV-1", "Customer", d or due_date - timedelta(days=30), due_date, total, tax, due, "authorised")


def test_corporation_tax_bands():
    assert corporation_tax(40_000)["tax"] == 7_600.0            # 19%
    assert corporation_tax(300_000)["tax"] == 75_000.0          # 25%
    assert corporation_tax(50_000)["tax"] == 9_500.0            # lower limit = 19%
    assert corporation_tax(250_000)["tax"] == 62_500.0          # upper limit = 25%
    # marginal relief: 100k * 25% - (250k - 100k) * 3/200 = 25,000 - 2,250
    assert corporation_tax(100_000)["tax"] == 22_750.0
    # one associated company halves the limits
    assert corporation_tax(40_000, associated_companies=1)["band"].startswith("main rate with marginal")
    assert corporation_tax(-5)["tax"] == 0


def test_vat_quarter_standard_stagger():
    start, end, due = vat_quarter(date(2026, 9, 28), [3, 6, 9, 12])
    assert (start, end, due) == (date(2026, 7, 1), date(2026, 9, 30), date(2026, 11, 7))
    start, end, _ = vat_quarter(date(2026, 9, 28), [3, 6, 9, 12], -1)
    assert (start, end) == (date(2026, 4, 1), date(2026, 6, 30))
    start, end, _ = vat_quarter(date(2027, 1, 5), [1, 4, 7, 10])
    assert (start, end) == (date(2026, 11, 1), date(2027, 1, 31))


def test_vat_estimate_boxes():
    q = (date(2026, 7, 1), date(2026, 9, 30))
    sales = [inv(d=date(2026, 8, 1), total=1200, tax=200), inv(d=date(2026, 6, 30), total=600, tax=100)]
    bills = [inv("payable", d=date(2026, 9, 2), total=240, tax=40)]
    est = vat_estimate(sales, bills, *q)
    assert est["box1_output_vat"] == 200 and est["box4_input_vat"] == 40 and est["box5_net_vat_payable"] == 160
    assert est["box6_net_sales"] == 1000


def test_ageing_buckets():
    today = date(2026, 9, 28)
    rows = [inv(days_overdue=-5, today=today), inv(days_overdue=10, today=today), inv(days_overdue=45, today=today),
            inv(days_overdue=120, today=today), inv(days_overdue=10, due=0, today=today)]
    aged = age_invoices(rows, today)
    assert aged["buckets"] == {"current": 100.0, "1-30": 100.0, "31-60": 100.0, "61-90": 0.0, "90+": 100.0}
    assert aged["total_overdue"] == 300.0 and aged["overdue_count"] == 3
    assert aged["overdue_invoices"][0]["days_overdue"] == 120


def test_late_payment_and_credit_control():
    claim = late_payment_claim(5000, 73, base_rate_pct=4.0)
    assert claim["fixed_compensation"] == 70.0
    assert claim["statutory_interest"] == round(5000 * 0.12 * 73 / 365, 2)
    assert late_payment_claim(500, 10, 4.0)["fixed_compensation"] == 40.0
    assert late_payment_claim(15000, 10, 4.0)["fixed_compensation"] == 100.0
    assert "reminder" in credit_control_stage(3)
    assert "letter before action" in credit_control_stage(60)


def test_cashflow_forecast_runs_down_with_fixed_costs():
    today = date(2026, 9, 28)
    rows = cashflow_forecast(10_000, [inv(days_overdue=-3, due=1_000, today=today)], [], today, 4, 2_000, 0)
    assert rows[0]["receipts"] == 1000 and rows[0]["closing_balance"] == 9000
    assert rows[-1]["closing_balance"] == 3000
