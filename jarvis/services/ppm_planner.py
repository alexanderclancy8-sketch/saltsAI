"""PPM scheduling / dispatch intelligence: a READ-ONLY advisory plan for the office.

Given Salts FSM data (systems with service frequency and due dates, sites, staff, booked jobs) this works out
which planned-preventative-maintenance visits are due, which can sensibly be bundled into one visit to a site,
how to group a day's visits geographically and which engineer could do them, then proposes a per-day /
per-engineer plan with the reasoning and flags for anything at risk.

It NEVER books, moves or assigns anything. It has no write path at all - booking still goes through the
approval-gated ``log_job`` / ``fsm_change`` tools, which the plan only *suggests* (see ``suggested_bookings``).

Honest limits, repeated in the output so nobody relies on them by mistake:
- The FSM API exposes engineer certifications only as a free-text list (name + optional expiry) on the staff
  record. There is NO per-system-type competence data. Skill matching is therefore keyword matching on the
  certificate names that exist (labelled as such) with the staff register's role/duties as a clearly labelled
  inference. Nothing is ever invented: no evidence means "unverified", not "qualified".
- Leave/holidays, job durations and live traffic aren't available. Capacity is the register's expected
  jobs-per-day (a target, not a hard limit) and one site visit counts as one job.
- Distances are straight-line estimates (same helpers as nearest_engineer); no paid external services.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .staff import OPEN_STATUSES, _status
from .tracking import POSTCODE, drive_minutes, haversine_m

DAYS_PER_MONTH = 30.4  # same conversion the FSM demo data uses for next_service_due
EARLY_FRACTION = 0.15  # a visit may be pulled forward by at most this share of the service interval ...
DEFAULT_EARLY_CAP_DAYS = 28  # ... and never by more than this many days (configurable per run)
DEFAULT_JOBS_PER_DAY = 2.5  # only used for engineers with no entry in the staff register
METRES_PER_MILE = 1609.34
SERVICE_JOB_TYPES = {"service", "ppm", "maintenance", "planned_maintenance", "planned"}

AREA_NAMES = {"BD": "Bradford", "LS": "Leeds", "HX": "Halifax", "HD": "Huddersfield", "WF": "Wakefield",
              "HG": "Harrogate", "YO": "York", "S": "Sheffield", "OL": "Oldham", "BB": "Blackburn"}

# Certificate-name keywords per system type. This is how the free-text certifications that FSM holds are
# *matched*; it is not a competence framework and it is labelled as keyword matching in the output.
CERT_KEYWORDS: dict[str, list[str]] = {
    "fire_alarm": ["fia", "fire detection", "fire alarm", "5839"],
    "emergency_lighting": ["emergency lighting", "5266", "ell"],
    "intruder": ["intruder", "50131", "pd 6662", "nsi", "ssaib"],
    "cctv": ["cctv", "surveillance"],
    "access_control": ["access control"],
}
# Role / duties keywords used ONLY as a labelled inference when no certificate matches.
ROLE_KEYWORDS: dict[str, list[str]] = {
    "fire_alarm": ["fire alarm", "fire detection", "5839", "fire"],
    "emergency_lighting": ["emergency lighting", "5266"],
    "intruder": ["intruder", "50131", "pd 6662", "security"],
    "cctv": ["cctv", "security"],
    "access_control": ["access control", "security"],
}
SKILL_PENALTY = {3: 0, 2: 50, 1: 80}
GEO_COST = {0: 0, 1: 10, 2: 30}
EMPTY_DAY_COST = 5

OUTWARD = re.compile(r"^\s*([A-Z]{1,2}\d[A-Z\d]?)\s*$", re.I)


# --------------------------------------------------------------------------- small helpers
def parse_date(value: Any) -> date | None:
    """Same parsing rule fsm_systems_due has always used: the first 10 characters as an ISO date."""
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def systems_due(systems: list[dict[str, Any]], today: date, days_ahead: int) -> list[dict[str, Any]]:
    """Systems overdue or due within ``days_ahead`` days, soonest first. Shared by the ``fsm_systems_due`` tool
    and the PPM planner so the two can never disagree about what is 'due'."""
    out = []
    for s in systems:
        due = parse_date(s.get("next_service_due"))
        if due is None:
            continue
        if due <= today + timedelta(days=days_ahead):
            out.append({**s, "days_until_due": (due - today).days})
    return sorted(out, key=lambda s: s["days_until_due"])


def _key(name: Any) -> str:
    return str(name or "").strip().casefold()


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_type(value: Any) -> str:
    return re.sub(r"[\s\-/]+", "_", str(value or "").strip().lower()) or "unknown"


def _fmt(d: date) -> str:
    return d.strftime("%a %d %b %Y")


def _next_working_day(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _has_keyword(text: str, keywords: list[str]) -> bool:
    text = text.casefold()
    return any(re.search(r"\b" + re.escape(k) + r"\b", text) for k in keywords)


def _postcode_district(text: Any) -> str | None:
    m = POSTCODE.search(str(text or ""))
    if not m:
        return None
    raw = re.sub(r"\s+", "", m.group(1)).upper()
    return raw[:-3]


def _area(district: str) -> str:
    m = re.match(r"[A-Z]+", district)
    return m.group(0) if m else district


def _area_label(district: str) -> str:
    area = _area(district)
    return f"{district} ({AREA_NAMES[area]} area)" if area in AREA_NAMES else district


# --------------------------------------------------------------------------- places and proximity
@dataclass
class Place:
    name: str
    coords: tuple[float, float] | None = None
    district: str | None = None
    source: str = "unknown"

    @property
    def located(self) -> bool:
        return self.coords is not None or self.district is not None


def _coords(row: dict[str, Any]) -> tuple[float, float] | None:
    lat, lng = _float(row.get("lat")), _float(row.get("lng"))
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None
    return lat, lng


def build_places(sites: list[dict[str, Any]]) -> dict[str, Place]:
    out: dict[str, Place] = {}
    for s in sites:
        name = s.get("name")
        if not name:
            continue
        district = _postcode_district(s.get("postcode")) or _postcode_district(s.get("address")) \
            or _postcode_district(name)
        if not district and s.get("postcode"):  # a bare outward code such as "BD17" is still useful
            m = OUTWARD.match(str(s["postcode"]))
            district = m.group(1).upper() if m else None
        coords = _coords(s)
        out[_key(name)] = Place(str(name), coords, district, "site record" if (coords or district) else "unknown")
    return out


def resolve_place(name: Any, places: dict[str, Place]) -> Place:
    found = places.get(_key(name))
    if found:
        return found
    district = _postcode_district(name)  # the job/system 'site' is sometimes just an address
    return Place(str(name or "unknown"), None, district, "postcode in text" if district else "unknown")


def proximity_tier(a: Place, b: Place, radius_miles: float) -> int:
    """0 = close (within the radius, or same postcode district), 1 = same general area, 2 = scattered or
    unknown. Unknown is deliberately treated as scattered so the plan never pretends it knows."""
    if a.coords and b.coords:
        miles = haversine_m(a.coords, b.coords) / METRES_PER_MILE
        return 0 if miles <= radius_miles else 1 if miles <= 2 * radius_miles else 2
    if a.district and b.district:
        if a.district == b.district:
            return 0
        return 1 if _area(a.district) == _area(b.district) else 2
    return 2


def _drive_estimate(places: list[Place]) -> dict[str, Any]:
    located = [p for p in places if p.coords]
    out: dict[str, Any] = {"sites_without_coordinates": len(places) - len(located)}
    if len(places) < 2:
        out["est_drive_minutes_between_sites"] = 0
    elif len(located) < 2:
        out["est_drive_minutes_between_sites"] = None
    else:
        todo, here, total = located[1:], located[0], 0
        while todo:
            nxt = min(todo, key=lambda p: haversine_m(here.coords, p.coords))
            total += drive_minutes(haversine_m(here.coords, nxt.coords))
            todo.remove(nxt)
            here = nxt
        out["est_drive_minutes_between_sites"] = total
    return out


# --------------------------------------------------------------------------- engineers and skills
def _cert_list(staff_row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = staff_row.get("certifications")
    if isinstance(raw, (str, dict)):
        raw = [raw]
    out = []
    for c in raw or []:
        if isinstance(c, str):
            out.append({"name": c, "expires": None})
        elif isinstance(c, dict):
            out.append({"name": c.get("name") or c.get("title"),
                        "expires": c.get("expires") or c.get("expiry") or c.get("expiryDate")})
    return [c for c in out if c["name"]]


def build_engineers(staff: list[dict[str, Any]], register_people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reg = {_key(p.get("name")): p for p in register_people}
    out = []
    for s in staff:
        name = s.get("name")
        if not name:
            continue
        rp = reg.get(_key(name))
        if rp and rp.get("type") and rp["type"] != "engineer":
            continue  # office staff, not a dispatchable engineer
        jpd = _float(((rp or {}).get("expectations") or {}).get("jobs_per_day"))
        source = "staff register" if jpd else "assumed default (not in the staff register)"
        jpd = jpd or DEFAULT_JOBS_PER_DAY
        role = " ".join(str(x) for x in (s.get("role"), (rp or {}).get("role")) if x)
        duties = " ".join(str(d) for d in ((rp or {}).get("duties") or []))
        out.append({
            "name": str(name), "key": _key(name), "role": role or None, "certs": _cert_list(s),
            "role_text": f"{role} {duties}", "jobs_per_day": jpd, "jobs_per_day_source": source,
            "cap": max(1, math.ceil(jpd)),
            "apprentice": "apprentice" in role.casefold(),
        })
    return out


def skill_for(eng: dict[str, Any], system_type: str, on_day: date) -> tuple[int, str]:
    """(rank, basis). 3 = matching certificate on record, 2 = inferred from role/duties only, 1 = no evidence,
    0 = the only matching certificate has expired by ``on_day``."""
    kw = CERT_KEYWORDS.get(system_type)
    if kw is None:
        return 1, f"no certificate mapping exists for system type '{system_type}' - unverified"
    matches = [c for c in eng["certs"] if _has_keyword(str(c["name"]), kw)]
    if matches:
        expired = []
        for c in matches:
            exp = parse_date(c["expires"]) if c["expires"] else None
            if exp is None or exp >= on_day:  # no/unreadable expiry is treated as valid but say so
                note = f" (expires {exp.isoformat()})" if exp else " (no readable expiry recorded)"
                return 3, f"certificate '{c['name']}' on record{note} - matched by keyword on the name"
            expired.append((c, exp))
        c, exp = expired[0]
        return 0, f"certificate '{c['name']}' expired {exp.isoformat()}"
    if _has_keyword(eng["role_text"], ROLE_KEYWORDS.get(system_type, [])):
        return 2, "INFERRED from role/duties in the staff register - not a verified qualification"
    return 1, "no matching certificate or role/duty evidence - unverified"


# --------------------------------------------------------------------------- visits
@dataclass
class Visit:
    vid: str
    site: str
    place: Place
    systems: list[dict[str, Any]]
    lo: date
    hi: date
    planned: date | None = None
    engineer: str | None = None
    rank: int | None = None
    skill_basis: list[str] = field(default_factory=list)
    flags: list[dict[str, str]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    unschedulable_reason: str | None = None
    split_from: str | None = None
    cluster: str | None = None

    @property
    def types(self) -> list[str]:
        return sorted({s["type"] for s in self.systems})

    def flag(self, code: str, detail: str) -> None:
        self.flags.append({"flag": code, "detail": detail})


def _window(s: dict[str, Any], n: int, early_cap: int) -> dict[str, Any] | None:
    due = parse_date(s.get("next_service_due"))
    if due is None:
        return None
    freq = _float(s.get("service_frequency_months"))
    freq = freq if freq and freq > 0 else None
    interval = round(freq * DAYS_PER_MONTH) if freq else None
    early = min(early_cap, round(interval * EARLY_FRACTION)) if interval else 0
    return {"n": n, "id": s.get("id"), "site": s.get("site"), "customer": s.get("customer"),
            "type": _norm_type(s.get("type")), "make_model": s.get("make_model"), "freq": freq,
            "last_service": s.get("last_service"), "due": due, "early_days": early,
            "open": due - timedelta(days=early)}


def _remove(items: list[dict[str, Any]], item: dict[str, Any]) -> None:
    for i, x in enumerate(items):
        if x is item:
            del items[i]
            return


def bundle_site(primaries: list[dict[str, Any]], joiners: list[dict[str, Any]], start: date
                ) -> list[tuple[list[dict[str, Any]], date, date]]:
    """Group a site's systems into visits. A system joins a visit only if the visit window (latest 'earliest
    permitted date', earliest 'due date') stays non-empty - so nothing is pulled earlier than its early
    tolerance and nothing is pushed past its due date. Overdue groups are ASAP, so they accept anything whose
    early window is already open by the first plannable day."""
    pend = sorted(primaries, key=lambda s: (s["due"], s["n"]))
    pool = sorted(joiners, key=lambda s: (s["due"], s["n"]))
    groups = []
    while pend:
        anchor = pend.pop(0)
        grp, lo, hi = [anchor], anchor["open"], anchor["due"]
        for cand in list(pend) + list(pool):
            nlo, nhi = max(lo, cand["open"]), min(hi, cand["due"])
            if nlo <= max(nhi, start):
                grp.append(cand)
                lo, hi = nlo, nhi
                if any(cand is p for p in pend):
                    _remove(pend, cand)
                else:
                    _remove(pool, cand)
        groups.append((grp, lo, hi))
    return groups


def _cluster_visits(visits: list[Visit], radius: float) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    unlocated: list[Visit] = []
    for v in sorted(visits, key=lambda v: (v.hi, v.vid)):
        if not v.place.located:
            unlocated.append(v)
            continue
        for c in clusters:
            if proximity_tier(c["seed"], v.place, radius) == 0:
                c["visits"].append(v)
                break
        else:
            clusters.append({"seed": v.place, "visits": [v]})
    out = []
    for c in clusters:
        label = _area_label(c["seed"].district) if c["seed"].district else f"around {c['seed'].name}"
        for v in c["visits"]:
            v.cluster = label
        out.append({"cluster": label, "visits": [v.vid for v in c["visits"]],
                    "sites": sorted({v.site for v in c["visits"]}),
                    "basis": "coordinates (within the cluster radius of the first site)"
                             if c["seed"].coords else "postcode district"})
    if unlocated:
        for v in unlocated:
            v.cluster = "location unknown"
        out.append({"cluster": "location unknown", "visits": [v.vid for v in unlocated],
                    "sites": sorted({v.site for v in unlocated}),
                    "basis": "no coordinates or postcode for these sites - cannot group or estimate drive time"})
    return out


# --------------------------------------------------------------------------- the plan
def build_plan(*, systems: list[dict[str, Any]], sites: list[dict[str, Any]], staff: list[dict[str, Any]],
               register_people: list[dict[str, Any]], jobs: list[dict[str, Any]], today: date,
               days_ahead: int = 28, start: date | None = None, early_cap: int = DEFAULT_EARLY_CAP_DAYS,
               radius_miles: float = 4.0, risk_margin_days: int = 5, missing: list[str] | None = None,
               demo: bool = False) -> dict[str, Any]:
    missing = list(missing or [])
    horizon_end = today + timedelta(days=days_ahead)
    start = start or _next_working_day(today)
    while start.weekday() >= 5:
        start += timedelta(days=1)
    last_day = max(horizon_end, start + timedelta(days=14))
    days = [start + timedelta(days=n) for n in range((last_day - start).days + 1)
            if (start + timedelta(days=n)).weekday() < 5]

    places = build_places(sites)
    engineers = build_engineers(staff, register_people)
    usable = [e for e in engineers if not e["apprentice"]]

    # 1. Which systems are due - the same rule as fsm_systems_due.
    due_index = {s["_n"] for s in systems_due([{**s, "_n": n} for n, s in enumerate(systems)], today, days_ahead)}
    windows, no_date, no_site = [], [], []
    for n, s in enumerate(systems):
        w = _window(s, n, early_cap)
        if w is None:
            no_date.append({"system": s.get("id"), "site": s.get("site"), "type": s.get("type"),
                            "problem": "no readable next_service_due date - can't be planned"})
        elif not w["site"]:
            no_site.append(w)
        else:
            w["primary"] = n in due_index
            windows.append(w)

    # 2. Existing open service jobs - so the plan doesn't double-book a site.
    service_jobs: dict[str, list[tuple[date, str]]] = defaultdict(list)
    booked_places: dict[tuple[str, date], list[Place]] = defaultdict(list)
    for j in jobs:
        if _status(j) not in OPEN_STATUSES:
            continue
        jd = parse_date(j.get("scheduled_start"))
        if jd is None:
            continue
        if j.get("engineer"):
            booked_places[(_key(j["engineer"]), jd)].append(resolve_place(j.get("site"), places))
        is_ppm = _norm_type(j.get("type")) in SERVICE_JOB_TYPES or _key(j.get("priority")) == "ppm"
        if is_ppm and j.get("site"):
            service_jobs[_key(j["site"])].append((jd, str(j.get("ref") or j.get("id"))))

    def booked_in_window(w: dict[str, Any]) -> tuple[date, str] | None:
        # jobs are only fetched from today on, so for an already-overdue system any open service job counts
        return next(((d, r) for d, r in service_jobs.get(_key(w["site"]), [])
                     if d >= w["open"] and (d <= w["due"] or w["due"] < today)), None)

    check_booked, plannable = [], []
    for w in windows:
        hit = booked_in_window(w)
        if hit and w["primary"]:
            check_booked.append({"system": w["id"], "site": w["site"], "type": w["type"],
                                 "due": w["due"].isoformat(),
                                 "existing_job": hit[1], "existing_job_date": hit[0].isoformat(),
                                 "booked_after_due_date": hit[0] > w["due"],
                                 "note": "An open service-type job is already booked at this site inside this "
                                         "system's window, so no new visit is proposed. FSM jobs aren't linked to "
                                         "systems - check the job covers THIS system; if not, book it separately."})
        elif not hit:
            plannable.append(w)

    # 3. Bundle per site into visits.
    by_site: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"p": [], "j": []})
    for w in plannable:
        by_site[_key(w["site"])]["p" if w["primary"] else "j"].append(w)
    visits: list[Visit] = []
    for skey in sorted(by_site):
        for grp, lo, hi in bundle_site(by_site[skey]["p"], by_site[skey]["j"], start):
            site_name = str(grp[0]["site"])
            visits.append(Visit(f"V{len(visits) + 1}", site_name, resolve_place(site_name, places), grp, lo, hi))
    for w in no_site:
        no_date.append({"system": w["id"], "site": None, "type": w["type"],
                        "problem": "no site recorded - can't be grouped or located"})

    # 4. Place visits: most urgent first.
    planned_places: dict[tuple[str, date], list[Place]] = defaultdict(list)
    evidence = {t for v in visits for t in v.types
                if any(skill_for(e, t, start)[0] >= 2 for e in usable)}

    def candidate_days(v: Visit) -> list[date]:
        if v.hi >= start:
            return [d for d in days if max(v.lo, start) <= d <= v.hi]
        return list(days)  # already past its latest date: as soon as capacity allows

    def place(v: Visit) -> str | None:
        if not usable:
            return "no_engineers"
        cdays = candidate_days(v)
        if not cdays:
            return "window"
        options = []
        for d in cdays:
            for e in usable:
                ranks, basis, ok = [], [], True
                for t in v.types:
                    r, b = skill_for(e, t, d)
                    if r == 0 or (r == 1 and t in evidence):
                        ok = False
                        break
                    ranks.append(r)
                    basis.append(f"{t}: {b}")
                if ok:
                    options.append((e, d, min(ranks), basis))
        if not options:
            return "skills"
        viable = [o for o in options
                  if len(booked_places[(o[0]["key"], o[1])]) + len(planned_places[(o[0]["key"], o[1])]) + 1
                  <= o[0]["cap"]]
        if not viable:
            return "capacity"

        def cost(o) -> tuple[int, date, str]:
            e, d, rank, _ = o
            here = booked_places[(e["key"], d)] + planned_places[(e["key"], d)]
            c = SKILL_PENALTY[rank]
            c += GEO_COST[min(proximity_tier(p, v.place, radius_miles) for p in here)] if here else EMPTY_DAY_COST
            if v.hi < start:
                c += (d - start).days * 4
            else:
                pref = max(v.lo, start, v.hi - timedelta(days=risk_margin_days))
                c += 2 * abs((d - pref).days)
            c += round(3 * (len(here) + 1) / e["cap"])
            return c, d, e["name"]

        e, d, rank, basis = min(viable, key=cost)
        here = booked_places[(e["key"], d)] + planned_places[(e["key"], d)]
        v.planned, v.engineer, v.rank, v.skill_basis = d, e["name"], rank, basis
        if v.hi < start:
            v.reasons.append(f"{_fmt(d)} is the first day with capacity - the latest permitted date ({_fmt(v.hi)}) "
                             "has already passed, so this is as-soon-as-possible")
        else:
            v.reasons.append(f"{_fmt(d)} is {(v.hi - d).days} day(s) before the latest permitted date "
                             f"({_fmt(v.hi)}) and not earlier than {_fmt(max(v.lo, start))}")
        if here:
            tier = min(proximity_tier(p, v.place, radius_miles) for p in here)
            near = min(here, key=lambda p: proximity_tier(p, v.place, radius_miles))
            if tier == 0:
                v.reasons.append(f"grouped with {e['name']}'s other visit that day at {near.name} (same area)")
            elif tier == 1:
                v.reasons.append(f"same general area as {near.name} on {e['name']}'s day (not the same district)")
            else:
                v.flag("scattered_day", f"no other visit on {e['name']}'s {_fmt(d)} is known to be nearby "
                                        f"(closest is {near.name}) - location data may be missing")
        else:
            v.reasons.append(f"first visit on {e['name']}'s {_fmt(d)}")
        v.reasons.append(f"{e['name']} load {len(here) + 1}/{e['cap']} that day "
                         f"(expected {e['jobs_per_day']:g} jobs/day, {e['jobs_per_day_source']})")
        planned_places[(e["key"], d)].append(v.place)
        return None

    def split(v: Visit) -> list[Visit]:
        parts = []
        for t in v.types:
            grp = [s for s in v.systems if s["type"] == t]
            lo, hi = max(s["open"] for s in grp), min(s["due"] for s in grp)
            parts.append(Visit(f"{v.vid}{chr(97 + len(parts))}", v.site, v.place, grp, lo, hi, split_from=v.vid))
        return parts

    final: list[Visit] = []
    for v in sorted(visits, key=lambda v: (v.hi, v.vid)):
        reason = place(v)
        if reason == "skills" and len(v.types) > 1:
            for p in split(v):
                p.flag("split_visit", f"No single engineer matches all of {', '.join(v.types)} at {v.site}, "
                                      "so it is proposed as separate visits by system type")
                pr = place(p)
                if pr:
                    p.unschedulable_reason = pr
                final.append(p)
            continue
        if reason:
            v.unschedulable_reason = reason
        final.append(v)
    visits = final
    clusters = _cluster_visits(visits, radius_miles)

    # 5. Flags and explanations.
    reason_text = {
        "no_engineers": "no engineer records came back from Salts FSM, so nobody can be proposed",
        "window": "no working day falls inside the permitted window",
        "skills": "no engineer has evidence of competence for this system type (expired/missing certificates)",
        "capacity": "every suitably skilled engineer is at their expected jobs-per-day on all days in the window",
    }
    for v in visits:
        strict = [s for s in v.systems if s["type"] == "fire_alarm" or (s["freq"] and s["freq"] <= 6)]
        if v.hi < today:
            v.flag("overdue", f"latest permitted date {_fmt(v.hi)} has already passed by {(today - v.hi).days} "
                              "day(s) - service window already breached; prioritise")
        elif v.hi < start:
            v.flag("due_before_first_plannable_day", f"latest permitted date {_fmt(v.hi)} is before the first "
                                                     f"planning day ({_fmt(start)}) - check today's diary")
        if v.unschedulable_reason:
            v.flag("unschedulable", reason_text.get(v.unschedulable_reason, v.unschedulable_reason))
        elif v.planned and v.hi >= start and (v.hi - v.planned).days <= risk_margin_days:
            v.flag("at_risk_low_slack", f"only {(v.hi - v.planned).days} day(s) between the planned visit and the "
                                        "latest permitted date - a cancellation or no-access could breach it")
        if strict:
            v.flag("hard_limit", "Fire-alarm / 6-monthly (or shorter) service intervals (e.g. BS 5839-1) are treated "
                                 f"as a hard limit: do not push this past {_fmt(v.hi)}; bringing it forward also "
                                 "shortens the next cycle - check your contract and the standard's wording")
        if not v.place.located:
            v.flag("location_missing", f"no coordinates or postcode for '{v.site}' - can't cluster by area or "
                                       "estimate drive time")
        if any(s["freq"] is None for s in v.systems):
            v.flag("frequency_missing", "a system has no service frequency in FSM, so it was never pulled "
                                        "forward to bundle with other work")
        pulled = [s for s in v.systems if v.planned and s["due"] > v.planned and not s["primary"]]
        if pulled:
            v.flag("bundled_early", "; ".join(
                f"{s['type']} (due {s['due'].isoformat()}) brought forward {(s['due'] - v.planned).days} day(s) to "
                "share the visit - within its early tolerance, but its next cycle moves earlier" for s in pulled))
        if v.rank == 2:
            v.flag("skills_inferred_from_role", "engineer chosen from role/duties in the staff register only - "
                                                "confirm they are qualified for this system type")
        elif v.rank == 1:
            v.flag("skills_unverified", "no certificate or role evidence for anyone - confirm the engineer's "
                                        "competence before booking")

    # 6. By day / by engineer.
    by_day: dict[date, dict[str, list[Visit]]] = defaultdict(lambda: defaultdict(list))
    for v in visits:
        if v.planned and v.engineer:
            by_day[v.planned][v.engineer].append(v)
    eng_by_name = {e["name"]: e for e in engineers}
    day_rows = []
    for d in sorted(by_day):
        rows = []
        for name in sorted(by_day[d]):
            e = eng_by_name[name]
            booked = booked_places[(e["key"], d)]
            mine = by_day[d][name]
            total = len(booked) + len(mine)
            row = {"engineer": name, "planned_visits": [v.vid for v in mine],
                   "sites": [v.site for v in mine], "already_booked_jobs": len(booked),
                   "load": f"{total} of expected {e['jobs_per_day']:g} jobs/day", "above_expected": total > e["jobs_per_day"],
                   **_drive_estimate(booked + [v.place for v in mine])}
            tiers = [proximity_tier(a, b, radius_miles) for i, a in enumerate(booked + [v.place for v in mine])
                     for b in (booked + [v.place for v in mine])[i + 1:]]
            row["scattered"] = bool(tiers) and max(tiers) == 2
            rows.append(row)
        day_rows.append({"date": d.isoformat(), "day": d.strftime("%A"), "engineers": rows})

    # 7. Output.
    def view(v: Visit) -> dict[str, Any]:
        return {
            "visit": v.vid, "site": v.site, "cluster": v.cluster, "split_from": v.split_from,
            "status": "unschedulable" if v.unschedulable_reason else "proposed",
            "planned_date": v.planned.isoformat() if v.planned else None,
            "engineer": v.engineer,
            "visit_window": {"earliest_permitted": v.lo.isoformat(), "latest_permitted": v.hi.isoformat()},
            "systems": [{"system": s["id"], "type": s["type"], "make_model": s["make_model"],
                         "service_frequency_months": s["freq"], "last_service": s["last_service"],
                         "due": s["due"].isoformat(), "early_tolerance_days": s["early_days"],
                         "days_until_due": (s["due"] - today).days,
                         "due_category": ("overdue" if s["due"] < today else
                                          "due_within_window" if s["primary"] else "brought_forward_to_bundle")}
                        for s in v.systems],
            "skills": v.skill_basis, "reasoning": v.reasons,
            "flags": v.flags,
        }

    shown = sorted(visits, key=lambda v: (v.hi, v.vid))
    suggested = [{
        "tool": "log_job (approval-gated - NOT done by this plan)",
        "args": {"site": v.site, "type": "service", "priority": "PPM", "engineer": v.engineer,
                 "scheduled_start": "",
                 "description": f"PPM service visit: {', '.join(v.types)} "
                                f"(visit {v.vid}, proposed {v.planned.isoformat()}; latest permitted {v.hi.isoformat()})"},
        "note": "Pick the time of day yourself; the plan does not know job durations."}
        for v in shown if v.planned and v.engineer and not v.unschedulable_reason]
    skills_available = sum(1 for e in engineers if e["certs"])
    unsched = [v.vid for v in visits if v.unschedulable_reason]
    at_risk = [v.vid for v in visits if any(f["flag"] in ("overdue", "at_risk_low_slack",
                                                          "due_before_first_plannable_day", "unschedulable")
                                            for f in v.flags)]
    due_systems = [w for w in windows if w["primary"]]
    located_sites = {v.site for v in visits if v.place.located}
    all_sites = {v.site for v in visits}
    return {
        "advisory_only": True,
        "demo": demo,
        "note": "READ-ONLY plan. Nothing has been booked, moved or assigned. To act on it, use log_job or "
                "fsm_change - both are queued for the owner's approval first.",
        "as_of": today.isoformat(),
        "planning_window": {"due_within_days": days_ahead, "due_up_to": horizon_end.isoformat(),
                            "first_planning_day": start.isoformat(), "last_planning_day": last_day.isoformat()},
        "assumptions": {
            "early_tolerance": f"a visit may be brought forward by up to {int(EARLY_FRACTION * 100)}% of the service "
                               f"interval, capped at {early_cap} days (planning assumption, not a contract term); "
                               "the due date is never pushed later",
            "capacity": "an engineer's expected jobs_per_day from the staff register (rounded up) is the daily "
                        "limit; one site visit = one job; job durations, leave and holidays are not known",
            "distance": f"straight-line estimates (same helpers as nearest_engineer); 'close' = within "
                        f"{radius_miles:g} miles or the same postcode district",
            "risk_margin": f"'at risk' = planned within {risk_margin_days} days of the latest permitted date",
            "apprentices": "engineers whose role says apprentice are not proposed to lead visits (inferred from role)",
        },
        "summary": {
            "systems_due_or_overdue": len(due_systems),
            "of_which_overdue": sum(1 for w in due_systems if w["due"] < today),
            "visits_proposed": len(visits) - len(unsched), "visits_unschedulable": len(unsched),
            "visits_flagged_at_risk": len(at_risk),
            "systems_needing_a_booking_check": len(check_booked),
            "systems_that_cannot_be_planned": len(no_date),
        },
        "data_quality": {
            "missing_or_failed": missing,
            "locations": {"visit_sites": len(all_sites), "with_coordinates_or_postcode": len(located_sites),
                          "without": sorted(all_sites - located_sites)},
            "engineer_skills": {
                "what_the_fsm_api_exposes": "a free-text list of certifications (name + optional expiry) on each "
                                            "engineer record. It does NOT expose skills or competences per system "
                                            "type, so a certificate can only be matched to a system type by "
                                            "keywords in its name.",
                "engineers_total": len(engineers), "engineers_with_certifications_on_record": skills_available,
                "statement": ("No engineer has any certification on record in Salts FSM: skill matching is based "
                              "only on the staff register's roles/duties (an inference) or is unverified."
                              if engineers and not skills_available else
                              "Matches marked 'certificate' come from certificate names on the FSM record; matches "
                              "marked INFERRED come from the staff register's role/duties and are NOT verified "
                              "qualifications. Anything else is shown as unverified - never assumed qualified."),
                "never_invented": True,
            },
        },
        "engineers_considered": [{"engineer": e["name"], "expected_jobs_per_day": e["jobs_per_day"],
                                  "source": e["jobs_per_day_source"], "certifications_on_record": len(e["certs"]),
                                  "proposed_for_visits": not e["apprentice"]} for e in engineers],
        "area_clusters": clusters,
        "plan_by_day": day_rows,
        "visits": [view(v) for v in shown],
        "unschedulable": [{"visit": v.vid, "site": v.site, "latest_permitted": v.hi.isoformat(),
                           "reason": reason_text.get(v.unschedulable_reason, v.unschedulable_reason)}
                          for v in shown if v.unschedulable_reason],
        "needs_booking_check": check_booked,
        "cannot_be_planned": no_date,
        "suggested_bookings": suggested,
    }


class PPMPlanner:
    """Read-only. Fetches from Salts FSM (never writes) and hands the lot to ``build_plan``."""

    def __init__(self, fsm, register=None):
        self.fsm = fsm
        self.register = register

    @property
    def demo(self) -> bool:
        return getattr(self.fsm, "demo", False)

    @staticmethod
    async def _fetch(label: str, call, missing: list[str]) -> list[dict[str, Any]]:
        try:
            return list(await call())
        except Exception as e:  # noqa: BLE001 - partial data is fine, say what was missing
            missing.append(f"{label} could not be read from Salts FSM ({type(e).__name__}) - the plan continues "
                           "without it")
            return []

    async def plan(self, days_ahead: int = 28, start_date: date | None = None,
                   early_window_days: int = DEFAULT_EARLY_CAP_DAYS, cluster_radius_miles: float = 4.0,
                   risk_margin_days: int = 5, today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        missing: list[str] = []
        try:
            systems = list(await self.fsm.systems())
        except Exception as e:  # noqa: BLE001
            return {"advisory_only": True, "error": f"Couldn't read the maintained systems from Salts FSM "
                                                    f"({type(e).__name__}), so there is nothing to plan."}
        horizon_end = today + timedelta(days=days_ahead)
        sites = await self._fetch("Sites (locations/postcodes)", self.fsm.sites, missing)
        staff = await self._fetch("Engineers/staff", self.fsm.staff, missing)
        jobs = await self._fetch("Booked jobs", lambda: self.fsm.jobs(date_from=today, date_to=horizon_end), missing)
        people: list[dict[str, Any]] = []
        if self.register is not None:
            try:
                people = self.register.people()
            except Exception as e:  # noqa: BLE001
                missing.append(f"Staff register could not be read ({type(e).__name__})")
        else:
            missing.append("Staff register unavailable - expected jobs-per-day assumed and role/duty skill "
                           "inference not possible")
        if not sites:
            missing.append("No site records - clustering falls back to postcodes found in site names only")
        if not staff:
            missing.append("No engineer records - nobody can be proposed for visits")
        elif not any(s.get("certifications") for s in staff):
            missing.append("No engineer has certification data in Salts FSM")
        if not jobs:
            missing.append("No booked jobs returned - capacity assumes empty diaries and existing PPM bookings "
                           "can't be detected")
        return build_plan(systems=systems, sites=sites, staff=staff, register_people=people, jobs=jobs,
                          today=today, days_ahead=days_ahead, start=start_date, early_cap=early_window_days,
                          radius_miles=cluster_radius_miles, risk_margin_days=risk_margin_days, missing=missing,
                          demo=self.demo)
