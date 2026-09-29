"""Staff register (roles, duties, expectations) and performance reviews.

Every member of staff - engineers and office - is measured against the
expectations set for *their* role in the register. Anyone meaningfully below
target is flagged to the owner with the evidence. Flags are a prompt for a
conversation, not a verdict: holidays, training days, difficult jobs or data
not being logged in Salts FSM can all explain a dip.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from ..config import ROOT_DIR

EXAMPLE_FILE = ROOT_DIR / "staff_roles.example.yaml"

METRIC_LABELS = {
    "jobs_per_day": "jobs completed per day",
    "utilisation_pct": "utilisation %",
    "on_time_start_pct": "on-time starts %",
    "revisit_rate_pct_max": "revisit rate % (max)",
    "revenue_per_hour_worked": "revenue per hour worked (£)",
    "quotes_per_week": "quotes raised per week",
    "quote_conversion_pct": "quote win rate %",
    "jobs_booked_per_week": "jobs booked per week",
    "emails_sent_per_day": "emails sent per working day",
    "teams_messages_per_day": "Teams messages per working day",
}


def working_days(start: date, end: date) -> int:
    return sum(1 for n in range((end - start).days + 1) if (start + timedelta(days=n)).weekday() < 5) or 1


class StaffRegister:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        src = self.path if self.path.exists() else EXAMPLE_FILE
        data = yaml.safe_load(src.read_text()) if src.exists() else {}
        data = data or {}
        data.setdefault("defaults", {})
        data.setdefault("staff", [])
        data["_source"] = "example (demo)" if src == EXAMPLE_FILE else str(src)
        return data

    def save(self, data: dict[str, Any]) -> None:
        data = {k: v for k, v in data.items() if not k.startswith("_")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    def people(self, kind: str | None = None) -> list[dict[str, Any]]:
        data = self.load()
        out = []
        for p in data["staff"]:
            if kind and p.get("type") != kind:
                continue
            expectations = {**data["defaults"].get(p.get("type", ""), {}), **(p.get("expectations") or {})}
            out.append({**p, "expectations": expectations})
        return out

    def find(self, name: str) -> dict[str, Any] | None:
        name = name.lower().strip()
        people = self.people()
        for test in (lambda p: p["name"].lower() == name or str(p.get("email", "")).lower() == name,
                     lambda p: p["name"].lower().split()[0] == name,
                     lambda p: name in p["name"].lower()):
            hits = [p for p in people if test(p)]
            if len(hits) == 1:
                return hits[0]
        return None

    def upsert(self, name: str, *, role: str | None = None, type_: str | None = None, email: str | None = None,
               add_duties: list[str] | None = None, remove_duties: list[str] | None = None,
               expectations: dict[str, float] | None = None) -> dict[str, Any]:
        data = self.load()
        person = next((p for p in data["staff"] if p["name"].lower() == name.lower()), None)
        if person is None:
            person = {"name": name, "type": type_ or "office", "role": role or "", "duties": []}
            data["staff"].append(person)
        if role:
            person["role"] = role
        if type_:
            person["type"] = type_
        if email:
            person["email"] = email
        duties = person.setdefault("duties", [])
        for d in add_duties or []:
            if d not in duties:
                duties.append(d)
        for d in remove_duties or []:
            person["duties"] = duties = [x for x in duties if d.lower() not in x.lower()]
        if expectations:
            person.setdefault("expectations", {}).update(expectations)
        self.save(data)
        return person

    def prompt_summary(self) -> str:
        lines = []
        for p in self.people():
            duties = "; ".join(p.get("duties") or []) or "duties not recorded"
            targets = ", ".join(f"{METRIC_LABELS.get(k, k)} {v}" for k, v in p["expectations"].items())
            lines.append(f"- {p['name']} ({p.get('type')}, {p.get('role') or 'role not set'}): {duties}. "
                         f"Expected: {targets or 'no targets set'}.")
        return "\n".join(lines) or "- No staff register yet."


async def office_productivity(fsm, mail, register: StaffRegister, days: int = 30,
                              today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    start = today - timedelta(days=days - 1)
    wdays = working_days(start, today)
    weeks = max(days / 7, 1)

    async def safe(coro, default):
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            return {"_error": f"{type(e).__name__}: {e}"[:200]} if default is None else default

    quotes, jobs, activity = await asyncio.gather(safe(fsm.quotes(), []), safe(fsm.jobs(start, today), []),
                                                  safe(mail.activity(days), None))
    activity_error = activity.pop("_error", None) if isinstance(activity, dict) else None

    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"quotes_raised": 0, "quote_value": 0.0, "quotes_won": 0,
                                                            "quotes_lost": 0, "jobs_booked": 0})
    for q in quotes:
        try:
            sent = date.fromisoformat(str(q.get("sent_date"))[:10])
        except ValueError:
            continue
        if not (start <= sent <= today) or not q.get("created_by"):
            continue
        st = stats[str(q["created_by"])]
        st["quotes_raised"] += 1
        st["quote_value"] += float(q.get("value") or 0)
        status = str(q.get("status") or "").lower()
        st["quotes_won"] += status in ("accepted", "won", "approved")
        st["quotes_lost"] += status in ("declined", "lost", "rejected")
    for jb in jobs:
        if jb.get("created_by"):
            stats[str(jb["created_by"])]["jobs_booked"] += 1

    office = register.people("office")
    names = {p["name"] for p in office} | set(stats)
    by_email = {k.lower(): v for k, v in (activity or {}).items()}
    by_name = {str(v.get("name", "")).lower(): v for v in (activity or {}).values()}
    engineers = {p["name"].lower() for p in register.people("engineer")}
    rows = []
    for name in sorted(names):
        if name.lower() in engineers:
            continue
        person = next((p for p in office if p["name"] == name), {})
        act = by_email.get(str(person.get("email", "")).lower()) or by_name.get(name.lower()) or {}
        st = stats.get(name, stats.default_factory())
        decided = st["quotes_won"] + st["quotes_lost"]
        rows.append({
            "name": name, "role": person.get("role"),
            "quotes_raised": st["quotes_raised"], "quote_value": round(st["quote_value"], 2),
            "quotes_per_week": round(st["quotes_raised"] / weeks, 1),
            "quote_conversion_pct": round(100 * st["quotes_won"] / decided, 1) if decided else None,
            "jobs_booked": st["jobs_booked"], "jobs_booked_per_week": round(st["jobs_booked"] / weeks, 1),
            "emails_sent": act.get("emails_sent"), "emails_received": act.get("emails_received"),
            "emails_sent_per_day": round(act["emails_sent"] / wdays, 1) if act.get("emails_sent") is not None else None,
            "teams_messages": act.get("teams_chat_messages"), "teams_calls": act.get("teams_calls"),
            "teams_meetings": act.get("teams_meetings"),
            "teams_messages_per_day": (round(act["teams_chat_messages"] / wdays, 1)
                                       if act.get("teams_chat_messages") is not None else None),
        })
    return {"from": start.isoformat(), "to": today.isoformat(), "working_days": wdays,
            "demo": getattr(fsm, "demo", False) or getattr(mail, "demo", False), "office_staff": rows,
            "m365_activity_note": activity_error or "Microsoft 365 figures are activity counts only (no content).",
            }


def _assess(metrics: dict[str, Any], expectations: dict[str, float]) -> dict[str, Any]:
    shortfalls, met, unmeasured = [], [], []
    for key, target in expectations.items():
        is_max = key.endswith("_max")
        metric = key[:-4] if is_max else key
        value = metrics.get(metric)
        if value is None:
            unmeasured.append(METRIC_LABELS.get(key, key))
            continue
        gap = (value - target) / target if target else 0
        below = value > target if is_max else value < target
        entry = {"metric": METRIC_LABELS.get(key, key), "actual": value, "expected": target,
                 "gap_pct": round(100 * gap, 1)}
        (shortfalls if below else met).append(entry)
    material = [s for s in shortfalls if abs(s["gap_pct"]) >= 10]
    big = [s for s in material if abs(s["gap_pct"]) >= 20]
    status = ("concern" if any(abs(s["gap_pct"]) >= 30 for s in material) or len(big) >= 2 else
              "below expectations" if material else "on track")
    return {"status": status, "shortfalls": shortfalls, "minor_only": bool(shortfalls) and not material,
            "met": met, "not_measured": unmeasured}


class PerformanceReviewer:
    def __init__(self, register: StaffRegister, staff_monitor, fsm, mail, notifier=None):
        self.register = register
        self.staff = staff_monitor
        self.fsm = fsm
        self.mail = mail
        self.notifier = notifier

    async def review(self, days: int = 7, person: str | None = None) -> dict[str, Any]:
        field = await self.staff.productivity(days)
        office = await office_productivity(self.fsm, self.mail, self.register, days)
        field_by = {r["engineer"]: r for r in field["engineers"]}
        office_by = {r["name"]: r for r in office["office_staff"]}
        people = []
        for p in self.register.people():
            if person and person.lower() not in p["name"].lower():
                continue
            metrics = field_by.get(p["name"]) if p.get("type") == "engineer" else office_by.get(p["name"])
            if not metrics:
                people.append({"name": p["name"], "role": p.get("role"), "status": "no activity recorded",
                               "note": "Nothing logged in Salts FSM / Microsoft 365 for this period - on leave, "
                                       "or not recording work?"})
                continue
            people.append({"name": p["name"], "role": p.get("role"), "type": p.get("type"),
                           **_assess(metrics, p["expectations"]), "metrics": metrics})
        flagged = [p for p in people if p["status"] not in ("on track",)]
        return {"period": f"{field['from']} to {field['to']}", "demo": field.get("demo"),
                "register": self.register.load()["_source"], "flagged": [p["name"] for p in flagged],
                "people": people,
                "guidance": "Treat flags as a prompt for a conversation. Check for holidays, training, sickness, "
                            "difficult jobs, or work not being logged before drawing conclusions."}

    async def weekly_review(self) -> str:
        result = await self.review(7)
        flagged = [p for p in result["people"] if p["status"] != "on track"]
        if not flagged:
            body = "Everyone is on track against their expectations this week."
        else:
            lines = []
            for p in flagged:
                gaps = "; ".join(f"{s['metric']} {s['actual']} vs {s['expected']}" for s in p.get("shortfalls", [])[:4])
                lines.append(f"• {p['name']} ({p.get('role')}): {p['status']}. {gaps or p.get('note', '')}")
            body = "\n".join(lines) + "\n\n" + result["guidance"]
        if self.notifier:
            await self.notifier.notify(f"Weekly team review: {len(flagged)} flagged", body,
                                       level="warning" if flagged else "info", push=True)
        return body
