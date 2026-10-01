"""Predictive scoring for credit control and sales follow-up. Read-only: nothing here sends, queues or
changes anything - the scores only rank work and inform the wording of drafts the owner reviews.

* Late-payment risk: open and soon-due receivables, scored 0-100 on how likely they are to go (or stay)
  overdue, from the customer's own payment history.
* Quote win likelihood: open quotes, scored 5-95 on how likely they are to be won, with follow-ups ranked by
  expected value (win score x quote value).

These are simple, explainable heuristics - not a trained model. Every score comes with plain-English reasons.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from .remedials import LOST, WON, _is_remedial

EXCLUDED_STATUSES = ("voided", "deleted", "draft")
UPCOMING_DAYS = 14  # invoices falling due within this many days are scored as "upcoming"
HISTORY_DAYS = 365
MIN_HISTORY = 3  # earlier invoices (past their due date) needed before the history is trusted
HIGH_RISK, MEDIUM_RISK = 55, 30
LIKELY_WIN, POSSIBLE_WIN = 60, 35
FOLLOW_UP_AFTER_DAYS = 7  # matches the first chase in remedials.FOLLOW_UP_DAYS and the day-7 follow-up touch


def _key(name: Any) -> str:
    return " ".join(str(name or "").lower().split())


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Late-payment risk
# ---------------------------------------------------------------------------

def _risk_band(score: int) -> str:
    return "high" if score >= HIGH_RISK else "medium" if score >= MEDIUM_RISK else "low"


def score_payment_risk(invoices: list[Any], today: date, upcoming_days: int = UPCOMING_DAYS) -> dict[str, Any]:
    """Score open receivables that are overdue or fall due within ``upcoming_days``, worst first.

    ``invoices`` should include recently settled invoices as well as open ones (they are the history).
    The accounts feed has no payment dates, so history is measured as the share of the customer's earlier,
    past-due invoices that are still unpaid; settled invoices count as on time, which flatters slow-but-eventual
    payers. Treat the score as a prompt to look, not a verdict."""
    live = [i for i in invoices if i.status not in EXCLUDED_STATUSES]
    by_contact: dict[str, list[Any]] = defaultdict(list)
    for i in live:
        if (today - i.date).days <= HISTORY_DAYS:
            by_contact[_key(i.contact)].append(i)

    rows = []
    for inv in live:
        if inv.amount_due <= 0:
            continue
        days_overdue = (today - inv.due_date).days
        if days_overdue < -upcoming_days:
            continue
        earlier = [o for o in by_contact[_key(inv.contact)] if o.number != inv.number and o.due_date < today]
        unpaid = [o for o in earlier if o.amount_due > 0]
        score = 10.0
        reasons: list[str] = []
        if len(earlier) >= MIN_HISTORY:
            score += 60 * len(unpaid) / len(earlier)
            if unpaid:
                reasons.append(f"{len(unpaid)} of {len(earlier)} earlier invoices from this customer are still "
                               "unpaid past their due date")
            else:
                reasons.append(f"all {len(earlier)} earlier invoices from this customer are settled")
        else:
            score += 10
            reasons.append("little payment history for this customer (fewer than 3 earlier invoices past due)")
        if days_overdue > 0:
            score += 25 + min(days_overdue, 90) / 90 * 25
            reasons.append(f"already {days_overdue} days overdue")
        elif days_overdue == 0:
            reasons.append("due today")
        else:
            reasons.append(f"due in {-days_overdue} days")
        if inv.amount_due >= 5000:
            score += 5
            reasons.append("large balance")
        final = int(round(min(score, 100.0)))
        rows.append({"invoice": inv.number, "customer": inv.contact, "amount_due": round(inv.amount_due, 2),
                     "due_date": inv.due_date.isoformat(), "days_overdue": days_overdue,
                     "status": "overdue" if days_overdue > 0 else "upcoming", "score": final,
                     "band": _risk_band(final), "reasons": reasons})
    rows.sort(key=lambda r: (-r["score"], -r["amount_due"]))
    high = [r for r in rows if r["band"] == "high"]
    return {"as_at": today.isoformat(), "invoices": rows, "high_risk_count": len(high),
            "high_risk_value": round(sum(r["amount_due"] for r in high), 2),
            "note": "Heuristic estimate from the customer's payment history, not a prediction of any one payment. "
                    "Use it to decide who to contact first; it is not evidence of a dispute or of inability to pay."}


# ---------------------------------------------------------------------------
# Quote win likelihood
# ---------------------------------------------------------------------------

def _win_band(score: int) -> str:
    return "likely" if score >= LIKELY_WIN else "possible" if score >= POSSIBLE_WIN else "unlikely"


def score_quotes(quotes: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """Score every open quote's chance of being won and rank them for follow-up by expected value."""
    decided_count = won_count = 0
    history: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # customer -> [won, decided]
    open_quotes = []
    for q in quotes:
        status = str(q.get("status") or "").lower()
        if status in WON or status in LOST:
            decided_count += 1
            won_count += status in WON
            h = history[_key(q.get("customer"))]
            h[0] += status in WON
            h[1] += 1
        else:
            open_quotes.append(q)
    overall = 100 * won_count / decided_count if decided_count else None
    base = min(max(overall, 20.0), 60.0) if overall is not None and decided_count >= 5 else 40.0

    rows = []
    for q in open_quotes:
        try:
            age: int | None = (today - date.fromisoformat(str(q.get("sent_date"))[:10])).days
        except ValueError:
            age = None
        value = _num(q.get("value"))
        score = base
        reasons: list[str] = []
        won, decided = history.get(_key(q.get("customer")), [0, 0])
        if decided >= 2:
            score += (won / decided - 0.5) * 40
            reasons.append(f"won {won} of {decided} previous decided quotes for this customer")
        else:
            reasons.append("no meaningful quote history for this customer")
        if age is None:
            score -= 5
            reasons.append("no valid sent date recorded")
        elif age > 21:
            score -= min(age - 21, 40) / 40 * 20
            reasons.append(f"sent {age} days ago - interest fades after about three weeks")
        if value >= 10000:
            score -= 8
            reasons.append("large value, usually a slower and more contested decision")
        elif value >= 5000:
            score -= 4
            reasons.append("sizeable value")
        remedial = _is_remedial(q)
        if remedial:
            score += 8
            reasons.append("remedial work for a defect on a system we maintain")
        final = int(round(min(max(score, 5.0), 95.0)))
        if age is None:
            follow_up = "check sent date - none recorded"
        elif age >= FOLLOW_UP_AFTER_DAYS:
            follow_up = f"due - {age} days since sent"
        else:
            follow_up = f"not yet - day {FOLLOW_UP_AFTER_DAYS} is {FOLLOW_UP_AFTER_DAYS - age} days away"
        rows.append({"quote": q.get("id"), "customer": q.get("customer"), "title": q.get("title"), "value": value,
                     "age_days": age, "win_score": final, "band": _win_band(final),
                     "expected_value": round(final / 100 * value, 2), "follow_up": follow_up,
                     "remedial": remedial, "reasons": reasons})
    rows.sort(key=lambda r: (-r["expected_value"], -r["win_score"], str(r["quote"])))
    return {"as_at": today.isoformat(), "open": rows, "open_count": len(rows),
            "weighted_pipeline": round(sum(r["expected_value"] for r in rows), 2),
            "overall_win_rate_pct": round(overall, 1) if overall is not None else None,
            "note": "Heuristic estimate from quote age, value, type and this customer's past decisions - a way to "
                    "order follow-ups, not a forecast."}


async def quote_pipeline(fsm, today: date | None = None) -> dict[str, Any]:
    """Read open and decided quotes from Salts FSM and score them."""
    return score_quotes(await fsm.quotes(), today or date.today())
