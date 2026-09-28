"""Accreditations (BAFE, SSAIB, CHAS, NSI...): renewal and audit reminders, and evidence packs
built from live company data so audits and questionnaires take hours, not weeks."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from ..brain import llm
from ..config import ROOT_DIR

log = logging.getLogger(__name__)
EXAMPLE_FILE = ROOT_DIR / "accreditations.example.yaml"
REMIND_AT_DAYS = (90, 60, 30, 14, 7, 1)

PACK_SYSTEM = """You are Jarvis, preparing {company} for a {scheme} audit / renewal. Using ONLY the evidence data
provided, write an audit-ready evidence pack in markdown:
1. Readiness summary (red/amber/green) and the top gaps to close before the audit, with owners and dates.
2. Evidence checklist table: requirement | status (in place / gap / expiring) | where the evidence is.
3. Competency matrix table from the staff data (name, role, relevant qualifications, expiry).
4. Suggested sample of recent jobs for the auditor (mix of installs, services, call-outs).
5. Draft answers for the usual questionnaire/auditor questions for this scheme, written in the first person
   plural for the company, clearly marking anything that needs {owner} to confirm.
Be precise; never invent certificate numbers, dates or policies - mark them 'TO CONFIRM'."""


def _as_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class Accreditations:
    def __init__(self, settings, db, staff, fsm, notifier, client, bus):
        self.s = settings
        self.db = db
        self.staff = staff
        self.fsm = fsm
        self.notifier = notifier
        self.client = client
        self.bus = bus
        self.path: Path = settings.data_dir / "accreditations.yaml"

    def load(self) -> dict[str, Any]:
        src = self.path if self.path.exists() else EXAMPLE_FILE
        data = yaml.safe_load(src.read_text()) or {}
        data["_source"] = "example (demo) - copy to data/accreditations.yaml" if src == EXAMPLE_FILE else str(src)
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump({k: v for k, v in data.items() if not k.startswith("_")},
                                            sort_keys=False, allow_unicode=True))

    def update(self, scheme: str, fields: dict[str, Any]) -> dict[str, Any]:
        data = self.load()
        items = data.setdefault("accreditations", [])
        item = next((a for a in items if scheme.lower() in a["scheme"].lower()), None)
        if item is None:
            item = {"scheme": scheme}
            items.append(item)
        item.update({k: v for k, v in fields.items() if v not in (None, "")})
        self.save(data)
        return item

    def status(self, today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        data = self.load()
        upcoming = []

        def add(kind: str, name: str, when: Any, extra: str = "") -> None:
            d = _as_date(when)
            if d:
                upcoming.append({"what": f"{name} - {kind}", "date": d.isoformat(), "days_left": (d - today).days,
                                 "overdue": d < today, "detail": extra})

        for a in data.get("accreditations", []):
            add("renewal", a["scheme"], a.get("renewal_date"), a.get("certification_body", ""))
            add("audit", a["scheme"], a.get("next_audit"), a.get("audit_type", ""))
        for c in data.get("calibration", []):
            add("calibration due", c["item"], c.get("calibrated_until"), c.get("serial", ""))
        for i in data.get("insurance", []):
            add("insurance renewal", i["type"], i.get("expires"))
        for p in data.get("policies", []):
            reviewed = _as_date(p.get("last_reviewed"))
            if reviewed:
                add("annual review due", p["name"], (reviewed + timedelta(days=365)).isoformat())
        upcoming.sort(key=lambda x: x["date"])
        return {"source": data["_source"], "accreditations": data.get("accreditations", []), "timeline": upcoming}

    async def daily_reminders(self) -> int:
        sent = 0
        for item in self.status()["timeline"]:
            if item["days_left"] in REMIND_AT_DAYS or item["days_left"] == 0 or (item["overdue"] and item["days_left"] % 7 == 0):
                when = "is OVERDUE" if item["overdue"] else "is today" if item["days_left"] == 0 else f"in {item['days_left']} days"
                await self.notifier.notify(f"{item['what']} {when}", f"Due {item['date']}. {item['detail']}".strip(),
                                           level="warning" if item["days_left"] <= 30 else "info", push=True)
                sent += 1
        return sent

    async def gather_evidence(self, scheme: str) -> dict[str, Any]:
        today = date.today()
        data = self.load()
        acc = next((a for a in data.get("accreditations", []) if scheme.lower() in a["scheme"].lower()), None)
        staff, certs, systems, jobs = await asyncio.gather(
            self.fsm.staff(), self.staff.expiring_certifications(3650),
            self.fsm.systems(), self.fsm.jobs(today - timedelta(days=180), today))
        on_time = overdue = 0
        for s in systems:
            due = _as_date(s.get("next_service_due"))
            if due:
                overdue += due < today
                on_time += due >= today
        done = [j for j in jobs if str(j.get("status")).lower() in ("completed", "complete", "done", "closed")]
        sample = []
        for jtype in ("install", "service", "callout", "remedial"):
            sample += [{"job": j.get("ref"), "type": j.get("type"), "site": j.get("site"), "date": str(j.get("completed_at"))[:10],
                        "engineer": j.get("engineer")} for j in done if str(j.get("type")).lower() == jtype][:3]
        issues = self.db.list_issues(None, 200)
        return {
            "scheme": acc or {"scheme": scheme, "note": "Not in the accreditations register yet"},
            "register_source": data["_source"],
            "staff_competency": [{"name": s["name"], "role": s.get("role"), "certifications": s.get("certifications")}
                                 for s in staff],
            "qualifications_expired_or_expiring_90d": [c for c in certs if c["days_left"] <= 90],
            "maintenance_compliance": {"systems": len(systems), "in_date": on_time, "overdue": overdue,
                                       "in_date_pct": round(100 * on_time / max(len(systems), 1), 1)},
            "jobs_last_6_months": {"completed": len(done), "by_type": {t: sum(str(j.get('type')).lower() == t for j in done)
                                                                       for t in ("install", "service", "callout", "remedial")}},
            "suggested_audit_sample": sample,
            "complaints_and_issues_log": {"total": len(issues), "open": sum(i["status"] not in ("resolved", "wont_fix") for i in issues),
                                          "recent": [{"id": i["id"], "date": i["created_at"][:10], "title": i["title"],
                                                      "status": i["status"]} for i in issues[:15]]},
            "calibration": data.get("calibration", []), "insurance": data.get("insurance", []),
            "policies": data.get("policies", []),
            "upcoming_dates": [t for t in self.status()["timeline"] if t["days_left"] <= 120],
            "demo": getattr(self.fsm, "demo", False),
        }

    async def evidence_pack(self, scheme: str) -> str:
        evidence = await self.gather_evidence(scheme)
        text = await llm.write(self.client, self.s,
                               system=PACK_SYSTEM.format(company=self.s.company_name, scheme=scheme,
                                                         owner=self.s.owner_name),
                               prompt="Evidence data (JSON):\n" + json.dumps(evidence, default=str)[:80000],
                               effort="high", max_tokens=16000)
        self.bus.publish("display", {"title": f"{scheme} evidence pack", "markdown": text})
        return text
