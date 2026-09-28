"""Remedial quotes: Salts FSM raises a quote for defects found on service visits. Jarvis doesn't
duplicate that - it watches the pipeline and chases anything that stalls."""

from __future__ import annotations

from datetime import date
from typing import Any

WON = {"accepted", "won", "approved", "ordered"}
LOST = {"declined", "lost", "rejected", "cancelled"}
FOLLOW_UP_DAYS = (7, 21)  # first chase, second chase


def _is_remedial(q: dict[str, Any]) -> bool:
    text = f"{q.get('type') or ''} {q.get('title') or ''}".lower()
    return "remedial" in text or bool(q.get("source_job"))


async def remedial_pipeline(fsm, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    remedials = [q for q in await fsm.quotes() if _is_remedial(q)]
    open_rows, won, lost = [], [], []
    for q in remedials:
        status = str(q.get("status") or "").lower()
        try:
            age = (today - date.fromisoformat(str(q.get("sent_date"))[:10])).days
        except ValueError:
            age = None
        row = {"quote": q.get("id"), "title": q.get("title"), "customer": q.get("customer"), "site": q.get("site"),
               "value": float(q.get("value") or 0), "sent": str(q.get("sent_date"))[:10], "age_days": age,
               "from_job": q.get("source_job"), "raised_by": q.get("created_by")}
        if status in WON:
            won.append(row)
        elif status in LOST:
            lost.append(row)
        else:
            if age is not None and age >= FOLLOW_UP_DAYS[1]:
                row["action"] = "second chase - phone the customer, and mention any compliance risk of leaving the defect"
            elif age is not None and age >= FOLLOW_UP_DAYS[0]:
                row["action"] = "first chase - follow-up email"
            open_rows.append(row)
    open_rows.sort(key=lambda r: -(r["age_days"] or 0))
    decided = len(won) + len(lost)
    return {
        "demo": getattr(fsm, "demo", False),
        "open": open_rows, "open_value": round(sum(r["value"] for r in open_rows), 2),
        "needs_chasing": [r for r in open_rows if r.get("action")],
        "won_value": round(sum(r["value"] for r in won), 2),
        "win_rate_pct": round(100 * len(won) / decided, 1) if decided else None,
        "note": "Remedials protect the customer's compliance - an unrepaired defect on a fire alarm or emergency "
                "lighting system should be recorded as a variation/defect until it's fixed.",
    }
