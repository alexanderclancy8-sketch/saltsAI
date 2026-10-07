"""Accreditations (BAFE, SSAIB, CHAS, NSI...): renewal and audit reminders, and evidence packs
built from live company data so audits and questionnaires take hours, not weeks."""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from ..brain import llm
from ..config import ROOT_DIR
from ..integrations.fsm_data import FsmDataError
from . import fsm_assets

log = logging.getLogger(__name__)
EXAMPLE_FILE = ROOT_DIR / "accreditations.example.yaml"
REMIND_AT_DAYS = (90, 60, 30, 14, 7, 1)
FSM_SNAPSHOT_MAX_AGE_S = 36 * 3600   # an FSM asset snapshot older than this (the FSM unreachable for a day and a half) is dropped
FSM_MANAGED = ("Van and equipment dates are read from the Salts FSM (Company Assets) now, so a date recorded here would be "
               "ignored. Update it in the FSM instead - the reminders pick it up by themselves. (This register is only a fallback "
               "for when the FSM can't supply them.)")

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


# --------------------------------------------------------------------------- input cleaning for the register
SECTIONS = ("accreditations", "calibration", "vehicles", "equipment", "insurance", "policies")
VEHICLE_FIELDS = ("driver", "mot_due", "service_due", "insurance_due", "tax_due")
VEHICLE_DATE_FIELDS = ("mot_due", "service_due", "insurance_due", "tax_due")
EQUIPMENT_FIELDS = ("check", "next_due")
EQUIPMENT_DATE_FIELDS = ("next_due",)
_MAX_TEXT = 120
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f​-‏‪-‮⁠-⁯﻿]")
_MONTHS = {**{n.lower(): i for i, n in enumerate(calendar.month_name) if n},
           **{n.lower(): i for i, n in enumerate(calendar.month_abbr) if n}, "sept": 9}
_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})", re.ASCII)
_UK = re.compile(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4}|\d{2})", re.ASCII)
_DAY_MONTH = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)\.?(?:,?\s+(\d{4}))?", re.ASCII)
_MONTH_DAY = re.compile(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", re.ASCII)
_PLATE = re.compile(r"[A-Z]{2}\d{2}[A-Z]{3}", re.ASCII)  # current-style UK plate written without its space


def parse_date(value: Any, today: date | None = None) -> date:
    """A real calendar date from ISO ("2026-11-02"), UK numeric ("02/11/2026") or spoken ("2 November 2026",
    "Nov 2nd") form; raises ValueError otherwise. With no year ("2 November") it means the next time that day comes
    round (today counts). Years outside 2000..today+10 are refused as typos - past dates are fine (overdue)."""
    today = today or date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = " ".join(str(value).strip().lower().split())
    year: int | None
    if m := _ISO.fullmatch(text):
        year, month, day = int(m[1]), int(m[2]), int(m[3])
    elif m := _UK.fullmatch(text):
        day, month, year = int(m[1]), int(m[2]), int(m[3])
        year += 2000 if year < 100 else 0
    elif (m := _DAY_MONTH.fullmatch(text)) and m[2] in _MONTHS:
        day, month, year = int(m[1]), _MONTHS[m[2]], int(m[3]) if m[3] else None
    elif (m := _MONTH_DAY.fullmatch(text)) and m[1] in _MONTHS:
        month, day, year = _MONTHS[m[1]], int(m[2]), int(m[3]) if m[3] else None
    else:
        raise ValueError(f"'{value}' is not a date I can read - give it as YYYY-MM-DD (e.g. 2026-11-02)")
    try:
        if year is None:  # the next occurrence of that day
            year = today.year if date(today.year, month, day) >= today else today.year + 1
        result = date(year, month, day)
    except ValueError:
        raise ValueError(f"'{value}' is not a real calendar date") from None
    if not 2000 <= result.year <= today.year + 10:
        raise ValueError(f"'{value}' is outside any sensible range - check the year")
    return result


def _reg_key(registration: Any) -> str:
    """Case/spacing-blind identity of a registration: 'yd71 sfs', 'YD71SFS' and 'YD71-SFS' are one van."""
    return re.sub(r"[^A-Z0-9]", "", str(registration or "").upper())


def clean_registration(value: Any) -> str:
    """Upper-case, single-spaced registration; a current-style plate typed without its space gets one."""
    text = " ".join(str(value or "").upper().replace("-", " ").split())
    if not _reg_key(text) or len(text) > 12 or not re.fullmatch(r"[A-Z0-9 ]+", text):
        raise ValueError(f"'{value}' doesn't look like a vehicle registration")
    return f"{text[:4]} {text[4:]}" if _PLATE.fullmatch(text) else text


def clean_text(value: Any, what: str) -> str:
    text = " ".join(str(value).split())
    if not text or len(text) > _MAX_TEXT or _CONTROL.search(text):
        raise ValueError(f"{what} must be 1-{_MAX_TEXT} plain characters")
    return text


def _norm_name(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _clean_fields(fields: dict[str, Any], allowed: tuple[str, ...], dates: tuple[str, ...]) -> dict[str, Any]:
    """Refuse unknown fields; drop blanks (like Accreditations.update); validate dates; return what to write."""
    unknown = sorted(set(fields) - set(allowed))
    if unknown:
        raise ValueError(f"Can't record {', '.join(unknown)} - the only fields here are {', '.join(allowed)}")
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        out[key] = parse_date(value) if key in dates else clean_text(value, key)
    return out


def _jsonable(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v.isoformat() if isinstance(v, date) else v for k, v in item.items()}


def _items(data: dict[str, Any], section: str) -> list[dict[str, Any]]:
    """The section's list (created if missing, or if the YAML has an empty `section:` line)."""
    if not isinstance(data.get(section), list):
        data[section] = []
    return data[section]


class Accreditations:
    def __init__(self, settings, db, staff, fsm, notifier, client, bus, *, fsm_data=None, ram=None):
        self.s = settings
        self.db = db
        self.staff = staff
        self.fsm = fsm
        self.notifier = notifier
        self.client = client
        self.bus = bus
        self.path: Path = settings.data_dir / "accreditations.yaml"
        self.fsm_data = fsm_data       # the FSM's generic read-only data API (None = never use it; the register file is the source)
        self.ram = ram                 # RAM Tracking, for the van cross-check only
        self._assets: fsm_assets.AssetSnapshot | None = None
        self._clock = time.monotonic

    def load(self) -> dict[str, Any]:
        src = self.path if self.path.exists() else EXAMPLE_FILE
        data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
        for section in SECTIONS:  # an empty `vehicles:` line in a hand-edited file loads as None
            if data.get(section) is None and section in data:
                data[section] = []
        data["_source"] = "example (demo) - copy to data/accreditations.yaml" if src == EXAMPLE_FILE else str(src)
        return data

    def _load_for_write(self) -> dict[str, Any]:
        """What an edit starts from. The REAL file if there is one (refusing, rather than overwriting, one that
        can't be read). Otherwise the example's structure with every list emptied: its placeholder vans, drivers,
        equipment, schemes and dates must never be saved as if they were the company's real records."""
        if self.path.exists():
            try:
                data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            except (yaml.YAMLError, UnicodeDecodeError) as e:
                raise ValueError(f"{self.path.name} can't be read ({type(e).__name__}), so nothing was changed - "
                                 "fix or move that file first") from e
            if data is not None and not isinstance(data, dict):
                raise ValueError(f"{self.path.name} isn't a register Jarvis understands, so nothing was changed")
            return data or {}
        example = yaml.safe_load(EXAMPLE_FILE.read_text(encoding="utf-8")) or {}
        return {k: [] if isinstance(v, list) else v for k, v in example.items()}

    def _header(self) -> str:
        """The comment block at the top of the file, kept across saves (a YAML round trip drops other comments)."""
        if self.path.exists():
            lines: list[str] = []
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.startswith("#"):
                    break
                lines.append(line)
            if lines:
                return "\n".join(lines) + "\n\n"
        return ("# Accreditations register - this company's real dates (git-ignored, never the example data).\n"
                "# Jarvis keeps it up to date when you tell him a date (each change waits for your approval);\n"
                "# you can also edit it by hand. Sections: accreditations, calibration, vehicles, equipment,\n"
                "# insurance, policies - accreditations.example.yaml shows the shape of each.\n\n")

    def save(self, data: dict[str, Any]) -> None:
        """Write the register atomically (temp file beside it, then replace) so a crash, or a read during the save,
        can never see a half-written file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump({k: v for k, v in data.items() if not k.startswith("_")},
                              sort_keys=False, allow_unicode=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(self._header() + body, encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def update(self, scheme: str, fields: dict[str, Any]) -> dict[str, Any]:
        data = self._load_for_write()
        items = _items(data, "accreditations")
        item = next((a for a in items if scheme.lower() in a["scheme"].lower()), None)
        if item is None:
            item = {"scheme": scheme}
            items.append(item)
        item.update({k: v for k, v in fields.items() if v not in (None, "")})
        self.save(data)
        return item

    # ------------------------------------------------------------------ the FSM as the source of van and equipment dates
    def _snapshot(self) -> fsm_assets.AssetSnapshot | None:
        snap = self._assets
        if snap is not None and self._clock() - snap.loaded_at > FSM_SNAPSHOT_MAX_AGE_S:
            self._assets = snap = None
        return snap

    def fsm_manages(self, kind: str) -> bool:
        """True when the FSM supplies this register ("vehicles" or "equipment"): its dates are the source of truth and this file's
        entries (and the example file's placeholders) are ignored. False = the register file is still the source."""
        snap = self._snapshot()
        return bool(snap and (snap.manages_vehicles if kind == "vehicles" else snap.manages_equipment))

    async def refresh_fsm_assets(self, max_age_s: float = 0) -> dict[str, Any]:
        """Re-read the vans and equipment from the FSM's assets group (skipped if the snapshot is younger than ``max_age_s``).
        Never raises. A FSM with no usable assets group - or none at all - clears the snapshot, so the register file is used."""
        if self.fsm_data is None:
            return {"source": "register file"}
        snap = self._snapshot()
        if snap is not None and self._clock() - snap.loaded_at < max_age_s:
            return {"source": "Salts FSM", "cached": True}
        try:
            fresh = await fsm_assets.load(self.fsm_data, self.ram, self._clock())
        except FsmDataError as e:
            if e.kind in ("unavailable", "demo"):
                self._assets = None
            return {"source": "Salts FSM" if self._snapshot() else "register file", "error": e.message}
        except Exception:  # noqa: BLE001 - never let a bad row stop the reminders
            log.exception("Could not read van and equipment dates from the FSM")
            return {"source": "Salts FSM" if self._snapshot() else "register file", "error": "unexpected error"}
        self._assets = fresh
        return {"source": "Salts FSM" if fresh else "register file"}

    def effective(self) -> dict[str, Any]:
        """The register as used: the file, with vans / equipment / calibration replaced by the FSM's when it supplies them."""
        data = self.load()
        snap = self._snapshot()
        if snap is None:
            return data
        data["_fsm"] = snap
        if snap.manages_vehicles:
            data["_file_vehicles"] = (data.get("vehicles") or []) if self.path.exists() else []
            data["vehicles"] = snap.vehicles
        if snap.manages_equipment:
            data["_file_equipment"] = (data.get("equipment") or []) if self.path.exists() else []
            data["equipment"] = snap.equipment
            data["calibration"] = snap.calibration
        return data

    # ------------------------------------------------------------------ vans and equipment
    def update_vehicle(self, registration: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Add or update one van, keyed on its registration (case/spacing-blind). Only the fields given change."""
        reg = clean_registration(registration)
        clean = _clean_fields(fields, VEHICLE_FIELDS, VEHICLE_DATE_FIELDS)
        data = self._load_for_write()
        items = _items(data, "vehicles")
        item = next((v for v in items if _reg_key(v.get("registration")) == _reg_key(reg)), None)
        created = item is None
        if item is None:
            item = {"registration": reg}
            items.append(item)
        item.update(clean)
        self.save(data)
        return {"action": "added" if created else "updated", "vehicle": _jsonable(item), "saved_to": str(self.path)}

    def update_equipment(self, item: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Add or update one inspection item (ladders, harnesses, PAT...), keyed on its name. A name matches an
        existing one exactly (any case) or, failing that, by being part of one recorded name ("ladders" finds
        "Ladders and steps (all vans)") - but only if that picks a single item; an ambiguous name is refused rather
        than guessed, and a longer name than any recorded one ("Ladders (old)") is a new item."""
        name = clean_text(item, "item")
        clean = _clean_fields(fields, EQUIPMENT_FIELDS, EQUIPMENT_DATE_FIELDS)
        data = self._load_for_write()
        items = _items(data, "equipment")
        want = _norm_name(name)
        found = [e for e in items if _norm_name(e.get("item")) == want]
        if not found:
            found = [e for e in items if want in _norm_name(e.get("item"))]
        if len(found) > 1:
            raise ValueError(f"'{name}' matches several recorded items ({', '.join(str(e.get('item')) for e in found)})"
                             " - say which one")
        row = found[0] if found else {"item": name}
        if not found:
            items.append(row)
        row.update(clean)
        self.save(data)
        return {"action": "updated" if found else "added", "equipment": _jsonable(row), "saved_to": str(self.path)}

    def remove_vehicle(self, registration: str) -> dict[str, Any]:
        """Take a sold or scrapped van out of the register (the real file only - example placeholders aren't records)."""
        reg = clean_registration(registration)
        data = self._load_for_write() if self.path.exists() else {}
        items = _items(data, "vehicles")
        gone = [v for v in items if _reg_key(v.get("registration")) == _reg_key(reg)]
        if not gone:
            have = ", ".join(str(v.get("registration")) for v in items) or "none recorded"
            raise ValueError(f"No van {reg} in the register (recorded: {have})")
        data["vehicles"] = [v for v in items if v not in gone]
        self.save(data)
        return {"action": "removed", "vehicle": _jsonable(gone[0]), "saved_to": str(self.path)}

    def remove_equipment(self, item: str) -> dict[str, Any]:
        """Take a retired item out of the register. The name must match a recorded one exactly (any case)."""
        name = clean_text(item, "item")
        data = self._load_for_write() if self.path.exists() else {}
        items = _items(data, "equipment")
        gone = [e for e in items if _norm_name(e.get("item")) == _norm_name(name)]
        if not gone:
            have = ", ".join(str(e.get("item")) for e in items) or "none recorded"
            raise ValueError(f"No equipment item '{name}' in the register (recorded: {have})")
        data["equipment"] = [e for e in items if e not in gone]
        self.save(data)
        return {"action": "removed", "equipment": _jsonable(gone[0]), "saved_to": str(self.path)}

    def status(self, today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        data = self.effective()
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
        for v in data.get("vehicles") or []:
            label = f"{v.get('registration')} ({v.get('driver') or 'pool'})"
            for field, kind in (("mot_due", "MOT due"), ("service_due", "service due"),
                                ("insurance_due", "insurance renewal"), ("tax_due", "road tax due")):
                add(kind, f"Van {label}", v.get(field))
        for e in data.get("equipment") or []:
            add(e.get("check") or "inspection due", e["item"], e.get("next_due"), e.get("holder", ""))
        for p in data.get("policies", []):
            reviewed = _as_date(p.get("last_reviewed"))
            if reviewed:
                add("annual review due", p["name"], (reviewed + timedelta(days=365)).isoformat())
        upcoming.sort(key=lambda x: x["date"])
        out: dict[str, Any] = {"source": data["_source"], "accreditations": data.get("accreditations", []),
                               "timeline": upcoming,
                               "vehicles": [_jsonable(v) for v in data.get("vehicles") or []],
                               "equipment": [_jsonable(e) for e in data.get("equipment") or []]}
        snap = data.get("_fsm")
        if snap is not None:
            out.update(self._fsm_status(data, snap))
        if not self.path.exists():
            if snap is not None and (snap.manages_vehicles or snap.manages_equipment):
                out["note"] = ("Vans and equipment come from the Salts FSM (Company Assets). The accreditation, insurance and policy "
                               "dates are PLACEHOLDER example dates, not the company's, until recorded with accreditation_update "
                               "(each waits for the owner's approval).")
            else:
                out["note"] = ("No real register yet, so these are PLACEHOLDER example dates, not the company's. Real ones "
                               "are recorded with vehicle_update / equipment_update / accreditation_update (each waits "
                               "for the owner's approval).")
        return out

    @staticmethod
    def _fsm_status(data: dict[str, Any], snap: fsm_assets.AssetSnapshot) -> dict[str, Any]:
        """What the answer adds when the FSM is the source: where the dates came from, items with no date recorded (neither compliant
        nor overdue, and not reminded about), and the read-only 'check this' notes."""
        extra: dict[str, Any] = {"fleet_source": "Salts FSM Company Assets (" + ", ".join(snap.resources) + ")",
                                 "not_recorded": [dict(n) for n in snap.not_recorded], "fleet_check": list(snap.fleet_check)}
        if snap.manages_vehicles:
            fsm_keys = {fsm_assets.reg_key(v["registration"]) for v in snap.vehicles}
            stale = [str(v.get("registration")) for v in data.get("_file_vehicles") or []
                     if _reg_key(v.get("registration")) not in fsm_keys]
            if stale:
                extra["fleet_check"].append(f"The register file lists {', '.join(stale)} which the FSM doesn't - ignored, as the FSM "
                                            "is the source now. If the van is real, add it in the FSM - check this.")
        if snap.manages_equipment and data.get("_file_equipment"):
            extra["fleet_check"].append(f"The register file lists {len(data['_file_equipment'])} equipment item(s) that are ignored now "
                                        "that the FSM supplies equipment dates - check they are in the FSM.")
        if snap.problems:
            extra["fsm_problems"] = list(snap.problems)
        return extra

    async def daily_reminders(self, today: date | None = None) -> int:
        await self.refresh_fsm_assets()  # the reminders run from the FSM's current dates (never raises)
        sent = 0
        for item in self.status(today)["timeline"]:
            if item["days_left"] in REMIND_AT_DAYS or item["days_left"] == 0 or (item["overdue"] and item["days_left"] % 7 == 0):
                when = "is OVERDUE" if item["overdue"] else "is today" if item["days_left"] == 0 else f"in {item['days_left']} days"
                await self.notifier.notify(f"{item['what']} {when}", f"Due {item['date']}. {item['detail']}".strip(),
                                           level="warning" if item["days_left"] <= 30 else "info", push=True,
                                           importance="important")  # compliance: err on the side of delivering
                sent += 1
        return sent

    async def gather_evidence(self, scheme: str) -> dict[str, Any]:
        today = date.today()
        await self.refresh_fsm_assets(max_age_s=300)
        data = self.effective()
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
            "calibration": [_jsonable(c) for c in data.get("calibration", [])], "insurance": data.get("insurance", []),
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
