"""Customer health watch: spots customers drifting away before their contract renewal.

Each customer starts at 100 and loses points for warning signs across Salts FSM, the
accounts and Jarvis' issue log. The score is a prompt to pick up the phone, not a verdict.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

CACHE_SECONDS = 900
DONE = {"completed", "complete", "done", "closed", "signed_off"}
CALLOUT = {"callout", "call-out", "reactive"}


def norm(name: Any) -> str:
    text = "".join(ch for ch in str(name or "").lower() if ch.isalnum() or ch == " ")
    for suffix in (" limited", " ltd", " plc", " llp"):
        text = text.removesuffix(suffix)
    return text.replace(" ", "")


def _d(value: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


class CustomerHealth:
    def __init__(self, j):
        self.j = j
        self._cache: tuple[float, dict[str, Any]] | None = None

    async def scores(self, refresh: bool = False, today: date | None = None) -> dict[str, Any]:
        if not refresh and self._cache and time.time() - self._cache[0] < CACHE_SECONDS:
            return self._cache[1]
        j = self.j
        today = today or date.today()
        d90, d180, d365 = today - timedelta(days=90), today - timedelta(days=180), today - timedelta(days=365)
        jobs, quotes, contracts, systems, sales, outstanding = await asyncio.gather(
            j.fsm.jobs(d180, today), j.fsm.quotes(), j.fsm.contracts(), j.fsm.systems(),
            j.finance.invoices("receivable", outstanding_only=False, since=d365),
            j.finance.invoices("receivable", outstanding_only=True))

        c: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "name": "", "spend_recent": 0.0, "spend_prior": 0.0, "revenue_12m": 0.0, "last_work": None,
            "callouts_90d": defaultdict(int), "declined_quotes": 0, "open_quotes": 0, "overdue_debt": 0.0,
            "worst_days_overdue": 0, "overdue_services": 0, "renewal": None, "contract_value": 0.0, "complaints": []})

        def rec(name: Any) -> dict[str, Any]:
            r = c[norm(name)]
            r["name"] = r["name"] or str(name)
            return r

        for jb in jobs:
            if not jb.get("customer"):
                continue
            r = rec(jb["customer"])
            when = _d(jb.get("completed_at") or jb.get("scheduled_start"))
            if str(jb.get("status") or "").lower() in DONE and when:
                r["last_work"] = max(filter(None, [r["last_work"], when]))
                value = float(jb.get("value") or 0)
                r["fsm_recent" if when > d90 else "fsm_prior"] = r.get("fsm_recent" if when > d90 else "fsm_prior", 0) + value
            if str(jb.get("type") or "").lower() in CALLOUT and when and when > d90:
                r["callouts_90d"][str(jb.get("site"))] += 1
        for inv in sales:
            if inv.status in ("voided", "deleted", "draft"):
                continue
            r = rec(inv.contact)
            net = inv.total - inv.tax
            r["revenue_12m"] += net
            if inv.date > d90:
                r["spend_recent"] += net
            elif inv.date > d180:
                r["spend_prior"] += net
        for r in c.values():  # no invoice history (e.g. not in the accounts yet): fall back to FSM job values
            if not r["spend_recent"] and not r["spend_prior"]:
                r["spend_recent"], r["spend_prior"] = r.get("fsm_recent", 0.0), r.get("fsm_prior", 0.0)
        for inv in outstanding:
            days = (today - inv.due_date).days
            if days > 0:
                r = rec(inv.contact)
                r["overdue_debt"] += inv.amount_due
                r["worst_days_overdue"] = max(r["worst_days_overdue"], days)
        for q in quotes:
            sent = _d(q.get("sent_date"))
            if not q.get("customer") or not sent or sent < d180:
                continue
            status = str(q.get("status") or "").lower()
            r = rec(q["customer"])
            if status in ("declined", "lost", "rejected"):
                r["declined_quotes"] += 1
            elif status in ("accepted", "won", "approved"):
                r["won_quotes"] = r.get("won_quotes", 0) + 1
            else:
                r["open_quotes"] += 1
        for ct in contracts:
            if not ct.get("customer"):
                continue
            r = rec(ct["customer"])
            renewal = _d(ct.get("renewal_date"))
            if renewal and (r["renewal"] is None or renewal < r["renewal"]):
                r["renewal"] = renewal
            r["contract_value"] += float(ct.get("annual_value") or 0)
        for s in systems:
            due = _d(s.get("next_service_due"))
            if s.get("customer") and due and due < today:
                rec(s["customer"])["overdue_services"] += 1
        for issue in j.db.list_issues(None, 300):
            text = f"{issue['title']} {issue['description']}".lower()
            for r in list(c.values()):
                if r["name"] and r["name"].lower() in text and issue["created_at"][:10] >= d90.isoformat():
                    r["complaints"].append(issue["title"])

        total_rev = sum(r["revenue_12m"] for r in c.values()) or 1.0
        rows = []
        for r in c.values():
            score, reasons, actions = 100, [], []
            if r["spend_prior"] >= 1500:  # too little history to judge a trend below this
                change = (r["spend_recent"] - r["spend_prior"]) / r["spend_prior"] * 100
                r["spend_change_pct"] = round(change)
                if change <= -60:
                    score -= 30; reasons.append(f"work down {abs(round(change))}% on the previous 3 months")
                elif change <= -30:
                    score -= 20; reasons.append(f"work down {abs(round(change))}% on the previous 3 months")
            if r["worst_days_overdue"] > 60:
                score -= 25; reasons.append(f"£{r['overdue_debt']:,.0f} overdue, oldest {r['worst_days_overdue']} days")
                actions.append("chase the overdue balance (personal call, not just a reminder)")
            elif r["worst_days_overdue"] > 30:
                score -= 15; reasons.append(f"£{r['overdue_debt']:,.0f} over 30 days overdue")
                actions.append("chase the overdue invoices")
            repeat_sites = {site: n for site, n in r["callouts_90d"].items() if n >= 3}
            if repeat_sites:
                score -= 15; reasons.append("repeat call-outs at " + ", ".join(f"{s} ({n})" for s, n in repeat_sites.items()))
                actions.append("send a senior engineer to find the root cause of the repeat faults")
            decided = r["declined_quotes"] + r.get("won_quotes", 0)
            if r["declined_quotes"] >= 3 and r["declined_quotes"] / decided >= 0.5:
                score -= 10; reasons.append(f"{r['declined_quotes']} of {decided} quotes declined in 6 months")
                actions.append("ask why recent quotes were declined (price or response time?)")
            if r["overdue_services"]:
                score -= min(20, 10 * r["overdue_services"])
                reasons.append(f"{r['overdue_services']} service visit(s) overdue - we're behind on their compliance")
                actions.append("book the overdue service visits this week")
            if r["complaints"]:
                score -= min(20, 10 * len(r["complaints"]))
                reasons.append(f"{len(r['complaints'])} problem(s) logged mentioning them")
            if r["last_work"] and (today - r["last_work"]).days > 120 and r["contract_value"]:
                score -= 15; reasons.append(f"no work for {(today - r['last_work']).days} days despite a contract")
            renewal_days = (r["renewal"] - today).days if r["renewal"] else None
            if renewal_days is not None and renewal_days < 0:
                score -= 15; reasons.append(f"contract renewal date passed {abs(renewal_days)} days ago - may have lapsed")
                actions.append("renew or re-quote the contract now")
            score = max(0, min(100, score))
            status = "healthy" if score >= 75 else "watch" if score >= 50 else "at risk"
            if renewal_days is not None and 0 <= renewal_days <= 90 and status != "healthy":
                actions.insert(0, f"call them before the renewal in {renewal_days} days and address the issues first")
            share = round(100 * r["revenue_12m"] / total_rev, 1)
            rows.append({
                "customer": r["name"], "score": score, "status": status, "reasons": reasons,
                "suggested_actions": actions or (["keep doing what we're doing"] if status == "healthy" else []),
                "renewal_date": r["renewal"].isoformat() if r["renewal"] else None, "renewal_in_days": renewal_days,
                "contract_value": round(r["contract_value"], 2), "revenue_12m": round(r["revenue_12m"], 2),
                "revenue_share_pct": share, "spend_change_pct": r.get("spend_change_pct"),
                "overdue_debt": round(r["overdue_debt"], 2),
                "last_work": r["last_work"].isoformat() if r["last_work"] else None,
            })
        rows.sort(key=lambda x: (x["score"], -(x["contract_value"] or 0)))
        top = max(rows, key=lambda x: x["revenue_share_pct"], default=None)
        result = {
            "demo": getattr(j.fsm, "demo", False) or getattr(j.finance, "demo", False),
            "customers": rows,
            "at_risk": [x for x in rows if x["status"] == "at risk"],
            "watch": [x for x in rows if x["status"] == "watch"],
            "concentration": ({"customer": top["customer"], "share_pct": top["revenue_share_pct"],
                               "warning": top["revenue_share_pct"] >= 25}
                              if top else None),
            "how_scored": "Starts at 100; points off for falling spend, overdue debt, repeat call-outs, declined "
                          "quotes, overdue service visits, logged problems and inactivity. 75+ healthy, 50-74 watch, "
                          "under 50 at risk.",
        }
        self._cache = (time.time(), result)
        return result

    async def customer(self, name: str) -> dict[str, Any]:
        data = await self.scores()
        key = norm(name)
        hits = [x for x in data["customers"] if key in norm(x["customer"])]
        return hits[0] if len(hits) == 1 else {"matches": [x["customer"] for x in hits] or "no match"}
