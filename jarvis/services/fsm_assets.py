"""Van and equipment compliance dates, read from the Salts FSM's ``assets`` group (Company Assets) instead of typed in by hand.

Until now Jarvis kept its own register (``accreditations.yaml``: vans with MOT / service / tax dates, ladders, harnesses, PAT,
test-kit calibration) and the owner told it each date. The FSM already holds those - vehicles with their MOT and road tax,
test kit with its calibration - so when the FSM catalog has an ``assets`` group Jarvis reads them from there, and the daily
reminders (90/60/30/14/7/1 days) run from the FSM's dates. The company has a fleet insurance policy, so there are no
per-vehicle insurance dates.

The catalog does not (yet) promise particular resource or field names for assets, so this module finds them the way a person
would: it looks at the field names and types the catalog advertises for each resource in the ``assets`` group (``mot_due``,
``road_tax_expiry``, ``registration``, ``calibration_due``...) and maps them to roles (``roles()``). A resource may hold vans,
equipment or both in one table - each ROW is classified (a registration or a "vehicle" type means a van). Rows the FSM marks sold,
scrapped or inactive are ignored. When the FSM's names turn out different, ``roles()`` is the one place to teach.

A van or item with no recorded date is reported as "date not recorded" - NOT as compliant and NOT as overdue, exactly as the FSM
treats it - and nothing is reminded about it. Registrations are cross-checked against RAM Tracking's vans: a van RAM knows that
is not in the FSM register (and the reverse) is listed as a 'check this' item. That is a read-only note; nothing is changed.

Read-only throughout: the only calls are ``FsmData.fetch`` (GET) and RAM Tracking's vehicle list.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..integrations.fsm_data import Catalog, FsmData, FsmDataError, Resource

log = logging.getLogger(__name__)

ASSETS_GROUP = "assets"
MAX_ASSET_ROWS = 2000
MAX_LISTED = 8                     # registrations named in one cross-check line

_WORDS = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
_DATE_TYPES = ("date", "datetime", "timestamp")
DATEISH = {"due", "expiry", "expires", "expire", "expiration", "next", "until", "renewal", "by"}
PAST = {"last", "previous", "prev", "tested", "done", "completed", "result", "status", "notes", "note", "reminder", "sent",
        "issued", "created", "updated", "paid", "payment", "taxed"}
VEHICLE_WORDS = ("vehicle", "van", "car", "truck", "motor", "fleet", "lorry")
EQUIPMENT_WORDS = ("equipment", "tool", "kit", "test", "ladder", "harness", "pat", "meter", "tester", "calibrat", "gauge")
RETIRED_WORDS = {"sold", "scrapped", "disposed", "retired", "inactive", "archived", "written off", "written_off", "decommissioned",
                 "stolen", "lost"}
NAME_FIELDS = ("name", "item", "asset_name", "title", "label", "equipment", "description", "make_model", "model")
DRIVER_FIELDS = ("driver", "assigned_to", "allocated_to", "assigned_engineer", "engineer", "employee", "user", "holder", "custodian")


def words(name: str) -> list[str]:
    return [w.lower() for w in _WORDS.findall(name)]


def reg_key(registration: Any) -> str:
    """Case/spacing-blind identity of a registration: 'yd71 sfs', 'YD71SFS' and 'YD71-SFS' are one van."""
    return re.sub(r"[^A-Z0-9]", "", str(registration or "").upper())


def _datelike(name: str, ftype: str) -> bool:
    w = set(words(name))
    return ftype.lower() in _DATE_TYPES or bool(w & DATEISH) or "date" in w


def _date_score(name: str, ftype: str) -> int:
    w = set(words(name))
    score = 0
    if ftype.lower() in _DATE_TYPES:
        score += 3
    if w & DATEISH:
        score += 2
    if "date" in w:
        score += 1
    if w & PAST:
        score -= 5       # 'last_service_date' / 'mot_status' are history, not a due date: never picked
    return score


@dataclass(frozen=True)
class Roles:
    """Which of a resource's fields plays which part. A role the resource has no field for is None."""
    reg: str | None = None
    mot: str | None = None
    tax: str | None = None
    service: str | None = None
    cal: str | None = None
    due: str | None = None
    name: str | None = None
    serial: str | None = None
    driver: str | None = None
    kind: str | None = None
    status: str | None = None
    active: str | None = None


def roles(res: Resource) -> Roles:
    fields = [(f.name, f.type) for f in res.fields]

    def best(pred, scorer=_date_score, need_date: bool = True) -> str | None:
        cands = []
        for i, (n, t) in enumerate(fields):
            w = set(words(n))
            if pred(w, n) and (not need_date or (_datelike(n, t) and scorer(n, t) >= 1)):
                cands.append((scorer(n, t), -i, n))
        return max(cands)[2] if cands else None

    reg = best(lambda w, n: bool(w & {"registration", "vrm", "plate", "reg"}),
               lambda n, t: 3 if "registration" in words(n) else 2 if "vrm" in words(n) else 1, need_date=False)
    mot = best(lambda w, n: "mot" in w)
    tax = best(lambda w, n: bool(w & {"tax", "ved"}) and "vat" not in w)
    service = best(lambda w, n: bool(w & {"service", "servicing"}) and not w & {"contract", "engineer", "type", "agent"})
    taken = {mot, tax, service}
    cal = best(lambda w, n: bool(w & {"calibration", "calibrated", "calib", "cal"}) and n not in taken)
    taken.add(cal)
    due = best(lambda w, n: bool(w & {"due", "expiry", "expires", "expire", "expiration", "next", "renewal"})
               and not w & {"insurance", "vat", "mot", "tax", "service"} and n not in taken)

    def first(names: tuple[str, ...]) -> str | None:
        have = {n.lower(): n for n, _ in fields}
        return next((have[x] for x in names if x in have), None)

    return Roles(reg=reg, mot=mot, tax=tax, service=service, cal=cal, due=due, name=first(NAME_FIELDS),
                 serial=best(lambda w, n: "serial" in w, lambda n, t: 0, need_date=False),
                 driver=first(DRIVER_FIELDS),
                 kind=first(("type", "asset_type", "category", "kind", "class")),
                 status=first(("status", "state")), active=first(("active", "is_active", "isactive", "in_service")))


def _text_has(text: str, needles: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(re.search(r"\b" + re.escape(n), low) for n in needles)


def classify_resource(res: Resource) -> tuple[bool, bool]:
    """(holds vans, holds dated equipment) judged from the catalog entry alone."""
    r = roles(res)
    blurb = f"{res.name} {res.description}"
    vehicles = bool(r.reg or (r.mot and r.tax)) or _text_has(blurb, VEHICLE_WORDS) and bool(r.mot or r.tax or r.service)
    equipment = bool(r.cal) or (bool(r.due) and (_text_has(blurb, EQUIPMENT_WORDS) or bool(r.kind)))
    return vehicles, equipment


def asset_resources(cat: Catalog) -> list[Resource]:
    """The resources of the (enabled) assets group that carry vehicle or equipment dates."""
    group = cat.groups.get(ASSETS_GROUP)
    if group is None or not group.enabled:
        return []
    return [r for r in cat.in_group(ASSETS_GROUP) if any(classify_resource(r))]


# ----------------------------------------------------------------------------------------------------------- the rows
def to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", text[:10])
    if m:
        try:
            return date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            return None
    return None


def _label(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("displayName") or value.get("title") or ""
    return " ".join(str(value or "").split())[:80]


def _retired(row: dict[str, Any], r: Roles) -> bool:
    if r.active and row.get(r.active) is False:
        return True
    if r.status and str(row.get(r.status) or "").strip().lower() in RETIRED_WORDS:
        return True
    return False


def _check_label(field_name: str) -> str:
    w = set(words(field_name))
    if w & {"calibration", "calibrated", "calib", "cal"}:
        return "calibration due"
    if "pat" in w:
        return "PAT test due"
    if "inspection" in w or "inspect" in w:
        return "inspection due"
    if "test" in w:
        return "test due"
    return "check due"


@dataclass
class AssetSnapshot:
    loaded_at: float
    version: str
    resources: list[str]
    manages_vehicles: bool
    manages_equipment: bool
    vehicles: list[dict[str, Any]] = field(default_factory=list)
    equipment: list[dict[str, Any]] = field(default_factory=list)
    calibration: list[dict[str, Any]] = field(default_factory=list)
    not_recorded: list[dict[str, str]] = field(default_factory=list)
    fleet_check: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def interpret(res: Resource, items: list[dict[str, Any]], snap: AssetSnapshot) -> None:
    """Fold one resource's rows into the snapshot (vans, dated equipment, calibration, and the 'date not recorded' list)."""
    r = roles(res)
    res_vehicle, _ = classify_resource(res)
    for row in items:
        if _retired(row, r):
            continue
        kind = str(row.get(r.kind) or "").lower() if r.kind else ""
        reg_value = _label(row.get(r.reg)) if r.reg else ""
        if kind and _text_has(kind, VEHICLE_WORDS):
            is_vehicle = True
        elif kind and _text_has(kind, EQUIPMENT_WORDS):
            is_vehicle = False
        elif reg_value:
            is_vehicle = True
        else:
            is_vehicle = res_vehicle and not (r.cal or r.due)
        if is_vehicle:
            reg = reg_value.upper() or _label(row.get(r.name)).upper() or ""
            if not reg:
                continue
            driver = _label(row.get(r.driver)) if r.driver else ""
            van = {"registration": reg, "driver": driver or None, "source": res.name}
            for role, key, what in ((r.mot, "mot_due", "MOT"), (r.tax, "tax_due", "road tax"), (r.service, "service_due", "service")):
                if not role:
                    continue
                d = to_date(row.get(role))
                van[key] = d
                if d is None:
                    snap.not_recorded.append({"what": f"Van {reg} ({driver or 'pool'}) - {what}", "detail": "date not recorded"})
            snap.vehicles.append(van)
            continue
        item = _label(row.get(r.name)) or _label(row.get(r.serial))
        if not item:
            continue
        holder = _label(row.get(r.driver)) if r.driver else ""
        serial = _label(row.get(r.serial)) if r.serial else ""
        for role in (r.cal, r.due):
            if not role:
                continue
            d = to_date(row.get(role))
            check = _check_label(role)
            if role == r.cal:
                snap.calibration.append({"item": item, "serial": serial, "calibrated_until": d, "source": res.name})
            else:
                snap.equipment.append({"item": item, "check": check, "next_due": d, "holder": holder, "source": res.name})
            if d is None:
                snap.not_recorded.append({"what": f"{item} - {check}", "detail": "date not recorded"})


def cross_check(fsm_vans: list[dict[str, Any]], ram_vans: list[dict[str, Any]]) -> list[str]:
    """'Check this' lines where RAM Tracking's vans and the FSM's vehicle register disagree. Read-only notes."""
    fsm_keys = {reg_key(v["registration"]): v["registration"] for v in fsm_vans if reg_key(v["registration"])}
    ram_keys = {reg_key(v.get("registration")): str(v.get("registration")).strip() for v in ram_vans if reg_key(v.get("registration"))}
    out = []
    only_ram = [ram_keys[k] for k in ram_keys if k not in fsm_keys]
    only_fsm = [fsm_keys[k] for k in fsm_keys if k not in ram_keys]

    def listed(regs: list[str]) -> str:
        shown = ", ".join(regs[:MAX_LISTED])
        return shown + (f" and {len(regs) - MAX_LISTED} more" if len(regs) > MAX_LISTED else "")

    if only_ram:
        out.append(f"RAM Tracking has {len(only_ram)} van{'' if len(only_ram) == 1 else 's'} that "
                   f"{'is' if len(only_ram) == 1 else 'are'} not in the FSM vehicle register: {listed(only_ram)} - check this.")
    if only_fsm:
        out.append(f"The FSM vehicle register has {len(only_fsm)} van{'' if len(only_fsm) == 1 else 's'} that RAM Tracking doesn't "
                   f"know: {listed(only_fsm)} - check this.")
    return out


async def load(fsm_data: FsmData, ram: Any, now: float) -> AssetSnapshot | None:
    """The van / equipment snapshot from the FSM, or None when the FSM has no usable ``assets`` group (so the caller keeps its own
    register). Raises FsmDataError when the FSM could not be read (the caller decides whether to keep an older snapshot)."""
    cat = await fsm_data.catalog()
    resources = asset_resources(cat)
    if not resources:
        return None
    snap = AssetSnapshot(loaded_at=now, version=cat.version, resources=[r.name for r in resources],
                         manages_vehicles=any(classify_resource(r)[0] for r in resources),
                         manages_equipment=any(classify_resource(r)[1] for r in resources))
    unreadable = 0
    for res in resources:
        try:
            result = await fsm_data.fetch(res.name, max_rows=MAX_ASSET_ROWS, limit=MAX_ASSET_ROWS)
        except FsmDataError as e:
            if e.kind in ("network", "server", "rate_limited", "unauthorized", "unavailable", "demo"):
                raise
            unreadable += 1
            snap.problems.append(f"{res.name}: {e.message}")
            continue
        if result.truncated:
            snap.problems.append(f"{res.name}: more than {MAX_ASSET_ROWS} rows, so some were not read")
        interpret(res, result.items, snap)
    if unreadable == len(resources):
        raise FsmDataError("server", "None of the FSM's asset resources could be read.")
    if snap.manages_vehicles:
        await _check_against_ram(snap, ram)
    return snap


async def _check_against_ram(snap: AssetSnapshot, ram: Any) -> None:
    if ram is None or getattr(ram, "demo", True):
        return  # RAM Tracking isn't connected (or is sample data): nothing real to compare with
    try:
        ram_vans = await ram.vehicles()
    except Exception as e:  # noqa: BLE001 - RAM being down must not stop the reminders
        log.warning("Could not cross-check vans against RAM Tracking (%s)", type(e).__name__)
        snap.problems.append("RAM Tracking could not be read, so the van list was not cross-checked")
        return
    snap.fleet_check.extend(cross_check(snap.vehicles, ram_vans))
