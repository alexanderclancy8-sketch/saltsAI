"""Contract renewals seen through the customer health watch: which maintenance contracts renew soon, what they are worth and how
healthy each customer is - so at-risk customers get a call before any price rise.

Renewals themselves are done IN SALTS FSM (services/fsm_renewals.py: ``fsm_renewals_due``, ``fsm_renewal_prepare``,
``fsm_renewal_send``). Jarvis no longer writes its own renewal letters: the old ``prepare_renewal`` tool and its letter template were
retired so there is one way of doing renewals. (Old ``renewal:<contract>:<date>`` kv markers it left behind are inert.)"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any


class Renewals:
    def __init__(self, j):
        self.j = j

    async def due(self, days: int | None = None, today: date | None = None) -> dict[str, Any]:
        j = self.j
        today = today or date.today()
        days = days or j.settings.renewal_notice_days
        health = {c["customer"].lower(): c for c in (await j.customers.scores()).get("customers", [])}
        rows = []
        for ct in await j.fsm.contracts():
            try:
                renewal = date.fromisoformat(str(ct.get("renewal_date"))[:10])
            except ValueError:
                continue
            if not (today - timedelta(days=30) <= renewal <= today + timedelta(days=days)):
                continue
            h = health.get(str(ct.get("customer")).lower(), {})
            rows.append({
                "contract": ct.get("id"), "customer": ct.get("customer"), "site": ct.get("site"),
                "renewal_date": renewal.isoformat(), "days_left": (renewal - today).days,
                "annual_value": float(ct.get("annual_value") or 0), "systems": ct.get("systems"),
                "contact_email": ct.get("contact_email"), "customer_health": h.get("status"),
                "health_score": h.get("score"), "health_reasons": h.get("reasons", [])[:3],
            })
        rows.sort(key=lambda r: r["days_left"])
        return {"window_days": days, "demo": getattr(j.fsm, "demo", False), "renewals": rows,
                "value_up_for_renewal": round(sum(r["annual_value"] for r in rows), 2),
                "note": "Renewals are prepared and sent in Salts FSM: fsm_renewals_due, fsm_renewal_prepare, fsm_renewal_send."}
