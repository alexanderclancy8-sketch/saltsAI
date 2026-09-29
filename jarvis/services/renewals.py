"""Contract renewals: ahead of each maintenance contract's renewal date Jarvis prepares the renewal
letter (with the agreed uplift) and queues it for the owner's approval. Customers that the health
watch flags as at risk get a "call them first" suggestion instead of a straight price rise."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

LETTER = """Dear {contact},

Your {company} maintenance contract for {site} is due for renewal on {renewal}.

Over the coming year we'll continue to look after your {systems} with {visits} planned service visit{visits_s}
a year, carried out to the relevant British Standards, with service certificates issued after every visit and
priority response for faults.

The annual contract price from {renewal} will be £{new:,.2f} + VAT (previously £{old:,.2f} + VAT), reflecting
increases in parts, labour and vehicle costs{uplift_note}.

To renew, simply reply to this email to confirm and we'll take care of the rest. If you'd like to talk anything
through - or add emergency lighting, extinguisher or security maintenance to the same contract - just let me know.

Kind regards,
{owner}
{company}"""


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
                "letter_prepared": bool(j.db.get_kv(f"renewal:{ct.get('id')}:{renewal.isoformat()}")),
            })
        rows.sort(key=lambda r: r["days_left"])
        return {"window_days": days, "demo": getattr(j.fsm, "demo", False), "renewals": rows,
                "value_up_for_renewal": round(sum(r["annual_value"] for r in rows), 2)}

    async def prepare(self, contract_id: str, uplift_pct: float | None = None) -> dict[str, Any]:
        j = self.j
        s = j.settings
        rows = (await self.due(days=365))["renewals"]
        row = next((r for r in rows if str(r["contract"]).lower() == str(contract_id).lower()), None)
        if not row:
            return {"error": f"No contract {contract_id} renewing in the next year."}
        uplift = s.renewal_uplift_pct if uplift_pct is None else uplift_pct
        new_price = round(row["annual_value"] * (1 + uplift / 100), 2)
        systems = f"{row['systems']} system{'s' if str(row['systems']) != '1' else ''}" if row["systems"] else "systems"
        letter = LETTER.format(contact="Sir or Madam" if not row.get("contact_email") else "Facilities Manager",
                               company=s.company_name, site=row["site"], renewal=row["renewal_date"],
                               systems=systems, visits=2, visits_s="s", new=new_price, old=row["annual_value"],
                               uplift_note=f" ({uplift:g}%)" if uplift else "", owner=s.owner_name)
        warning = None
        if row["customer_health"] in ("at risk", "watch"):
            warning = (f"{row['customer']} is '{row['customer_health']}' on the health watch "
                       f"({'; '.join(row['health_reasons'])}). Consider calling them before sending a price rise.")
        result: dict[str, Any] = {"contract": row["contract"], "customer": row["customer"], "old_price": row["annual_value"],
                                  "new_price": new_price, "uplift_pct": uplift, "letter": letter, "warning": warning}
        if row.get("contact_email"):
            action_id = j.actions.queue(
                "email_send", f"Send renewal for {row['customer']} ({row['site']}): £{row['annual_value']:,.0f} → "
                              f"£{new_price:,.0f} + VAT from {row['renewal_date']}" + (" ⚠ at-risk customer" if warning else ""),
                {"to": [row["contact_email"]], "cc": [], "subject": f"Maintenance contract renewal - {row['site']}",
                 "body": letter})
            result["queued_action"] = action_id
            result["note"] = "Queued for approval - nothing has been sent."
        else:
            result["note"] = "No contact email on the contract in Salts FSM - the letter is ready to copy."
        j.db.set_kv(f"renewal:{row['contract']}:{row['renewal_date']}", "prepared")
        j.bus.publish("display", {"title": f"Renewal - {row['customer']}", "markdown": (f"**⚠ {warning}**\n\n" if warning else "") + letter})
        return result
