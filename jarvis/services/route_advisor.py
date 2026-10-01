"""Route-optimised dynamic scheduling ADVICE for one day's jobs: a READ-ONLY re-sequencing proposal.

Given a day's open jobs (Salts FSM), engineer positions, skills and SLA priorities this proposes a re-sequenced
route per engineer with the drive-time saving against the current order, and suggests where to slot an urgent
call-out (which engineer, and between which two jobs) with the extra drive time it costs.

It NEVER books, moves or assigns anything. It has no write path at all - any change still goes through the
approval-gated ``log_job`` / ``fsm_change`` tools, which the output only *names* (see ``suggested_changes``).

Rules and honest limits (repeated in the output so nobody relies on them by mistake):
- Live engineer positions are used ONLY during working hours (the same gate as the live map / nearest_engineer) and
  only when planning today. Outside working hours, or for another day, no position is read or used at all.
- Distances are straight-line estimates with a road factor (same helpers as nearest_engineer): no live traffic.
- Customer-agreed appointment times and job durations aren't available, so only the *sequence* is proposed, never
  times - check with the customer before moving a job that has an agreed slot.
- SLA priority: a job whose SLA is 4 hours or less (e.g. '4h') is never moved behind a job without one. Other SLAs
  ('24h', 'PPM', blank) are treated as flexible within the day.
- Jobs stay with their current engineer. Only the urgent call-out suggestion compares engineers, using the same
  certificate / role keyword matching (and the same labelled limits) as the PPM planner.
- A job in progress is never re-sequenced. A route containing a job whose site can't be located is left alone.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .ppm_planner import (PPMPlanner, build_engineers, build_places, parse_date, resolve_place, skill_for, _coords,
                          _key, _norm_type)
from .staff import OPEN_STATUSES, _status
from .tracking import drive_minutes, haversine_m

SLA_URGENT_HOURS = 4  # a job with an SLA this short (or shorter) is never pushed behind a job without one
MAX_EXACT_STOPS = 7  # try every order up to this many stops; beyond it fall back to nearest-neighbour
STALE_POSITION_MINUTES = 60  # a live position older than this isn't trusted as a start point
SAME_PLACE_METRES = 100  # closer than this counts as no drive at all
IN_PROGRESS = {"in_progress", "started", "on_site", "travelling"}
UNASSIGNED = {"", "unassigned", "none", "tbc"}
NO_START = "unknown (no live position or job in progress) - drive time counts the legs between jobs only"

# Keywords used ONLY to guess the system type of a free-text call-out description. A guess is labelled as such.
URGENT_TYPE_KEYWORDS: dict[str, list[str]] = {
    "fire_alarm": ["fire alarm", "fire panel", "fire", "smoke detector", "detector", "sounder", "5839"],
    "emergency_lighting": ["emergency lighting", "emergency light", "luminaire", "5266"],
    "intruder": ["intruder", "burglar", "pir", "tamper", "50131"],
    "cctv": ["cctv", "camera", "dvr", "nvr"],
    "access_control": ["access control", "door entry", "door controller", "fob", "barrier"],
}
RANK_LABEL = {3: "certificate on record (keyword match)", 2: "INFERRED from role/duties - not verified",
              1: "no evidence - unverified"}


# --------------------------------------------------------------------------- small helpers
def sla_hours(priority: Any) -> float | None:
    """'4h' -> 4.0, '24 hours' -> 24.0, 'PPM' / blank -> None."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:hours|hour|hrs|hr|h)\b", str(priority or ""), re.I)
    return float(m.group(1)) if m else None


def leg_minutes(a: tuple[float, float], b: tuple[float, float]) -> int:
    d = haversine_m(a, b)
    return 0 if d < SAME_PLACE_METRES else drive_minutes(d)


@dataclass
class Stop:
    ref: str
    site: str
    priority: str
    sla: float | None
    coords: tuple[float, float] | None
    start: str | None
    order: int

    @property
    def urgent(self) -> bool:
        return self.sla is not None and self.sla <= SLA_URGENT_HOURS

    def view(self) -> dict[str, Any]:
        return {"job": self.ref, "site": self.site, "priority": self.priority or None,
                "scheduled_start": self.start}


def path_minutes(start: tuple[float, float] | None, stops) -> int:
    """Drive minutes from ``start`` (if known) through every stop in order."""
    total, prev = 0, start
    for s in stops:
        if prev is not None:
            total += leg_minutes(prev, s.coords)
        prev = s.coords
    return total


def _best_order(start: tuple[float, float] | None, group: list[Stop]) -> list[Stop]:
    if len(group) <= 1:
        return list(group)
    if len(group) <= MAX_EXACT_STOPS:
        best, best_cost = None, 0
        for perm in itertools.permutations(group):  # lexicographic, so a tie keeps the current order
            cost = path_minutes(start, perm)
            if best is None or cost < best_cost:
                best, best_cost = perm, cost
        return list(best)
    todo, out, here = list(group), [], start
    if here is None:
        first = todo.pop(0)
        out.append(first)
        here = first.coords
    while todo:
        nxt = min(todo, key=lambda s: (leg_minutes(here, s.coords), s.order))
        todo.remove(nxt)
        out.append(nxt)
        here = nxt.coords
    return out


def optimise(start: tuple[float, float] | None, stops: list[Stop]) -> list[Stop]:
    """Short-SLA jobs first (nothing urgent is pushed behind other work), shortest drive within each tier."""
    out, here = [], start
    for group in ([s for s in stops if s.urgent], [s for s in stops if not s.urgent]):
        ordered = _best_order(here, group)
        out += ordered
        if ordered:
            here = ordered[-1].coords
    return out


def _tiers_respected(seq: list[Stop]) -> bool:
    seen_flexible = False
    for s in seq:
        if not s.urgent:
            seen_flexible = True
        elif seen_flexible:
            return False
    return True


@dataclass
class Route:
    name: str
    key: str
    start: tuple[float, float] | None = None
    start_source: str = NO_START
    baseline: list[Stop] = field(default_factory=list)
    proposed: list[Stop] = field(default_factory=list)
    optimised: bool = False
    in_progress: list[str] = field(default_factory=list)
    cap: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def total_jobs(self) -> int:
        return len(self.baseline) + len(self.in_progress)


# --------------------------------------------------------------------------- urgent call-out
def infer_system_types(description: Any, explicit: Any = "") -> tuple[list[str], str]:
    if str(explicit or "").strip():
        return [_norm_type(explicit)], "given by the caller"
    text = str(description or "")
    found = [t for t, kws in URGENT_TYPE_KEYWORDS.items()
             if any(re.search(r"\b" + re.escape(k) + r"\b", text, re.I) for k in kws)]
    if found:
        return found, "GUESSED from keywords in the description"
    return [], "could not be worked out from the description"


def _urgent_advice(urgent: dict[str, Any], places, engineers: list[dict[str, Any]], routes: dict[str, Route],
                   day: date) -> dict[str, Any]:
    site = str(urgent.get("site") or "").strip()
    ucoords = urgent.get("coords") or resolve_place(site, places).coords
    if not ucoords:
        return {"site": site, "error": f"Couldn't locate '{site}'. Give a site name known to Salts FSM or a UK "
                                       "postcode, so a slot can be worked out."}
    priority = str(urgent.get("priority") or "")
    types, types_basis = infer_system_types(urgent.get("description"), urgent.get("system_type"))

    candidates, excluded = [], []
    for e in engineers:
        if e["apprentice"]:
            excluded.append({"engineer": e["name"], "reason": "apprentice - not proposed to attend alone"})
            continue
        if not types:
            rank, basis = 1, ["system type unknown, so competence can't be checked - unverified"]
        else:
            results = [skill_for(e, t, day) for t in types]
            if any(r == 0 for r, _ in results):
                bad = next(b for r, b in results if r == 0)
                excluded.append({"engineer": e["name"], "reason": bad})
                continue
            rank = min(r for r, _ in results)
            basis = [f"{t}: {b}" for t, (_, b) in zip(types, results)]
        candidates.append((e, rank, basis))
    if any(rank >= 2 for _, rank, _ in candidates):  # same rule as the PPM planner: never prefer 'no evidence'
        for e, rank, _ in [c for c in candidates if c[1] < 2]:
            excluded.append({"engineer": e["name"], "reason": "no evidence of competence while others have some"})
        candidates = [c for c in candidates if c[1] >= 2]

    options = []
    for e, rank, basis in candidates:
        route = routes.get(e["key"]) or Route(e["name"], e["key"], optimised=True)
        stops = route.proposed
        n = len(stops)
        best = None
        for k in (range(n + 1) if route.optimised else [n]):
            prev = stops[k - 1].coords if k > 0 else route.start
            nxt = stops[k].coords if k < n else None
            to_u = leg_minutes(prev, ucoords) if prev is not None else None
            if not route.optimised:
                extra = None
            elif prev is None and nxt is None:
                extra = 0
            elif prev is None:
                extra = leg_minutes(ucoords, nxt)
            elif nxt is None:
                extra = to_u
            else:
                extra = max(0, to_u + leg_minutes(ucoords, nxt) - leg_minutes(prev, nxt))
            pushed = [s.ref for s in stops[k:] if s.urgent]
            if k > 0:
                after = f"{stops[k - 1].ref} at {stops[k - 1].site}"
            else:
                after = "their current position" if route.start_source.startswith("live") else "the start of the route"
            opt = {"engineer": e["name"], "slot_after": after,
                   "slot_before": f"{stops[k].ref} at {stops[k].site}" if k < n else None,
                   "position_in_route": k + 1,
                   "extra_drive_minutes": extra, "drive_minutes_to_call_out": to_u,
                   "short_sla_jobs_pushed_back": pushed,
                   "over_expected_jobs_per_day": bool(route.cap and route.total_jobs + 1 > route.cap),
                   "skill_rank": rank, "skill_basis": basis, "skills": RANK_LABEL[rank],
                   "warnings": []}
            if extra is None:
                opt["warnings"].append("drive impact unknown - this engineer's route has a job at a site that "
                                       "couldn't be located, so it is suggested at the end of the route")
            if pushed:
                opt["warnings"].append("pushes back short-SLA job(s): " + ", ".join(pushed))
            if opt["over_expected_jobs_per_day"]:
                opt["warnings"].append(f"takes {e['name']} above their expected {e['jobs_per_day']:g} jobs/day")
            if rank == 2:
                opt["warnings"].append("engineer chosen from role/duties only - confirm they are qualified")
            elif rank == 1:
                opt["warnings"].append("competence can't be verified - confirm before sending anyone")
            if to_u is None:
                opt["warnings"].append("no start position known, so the arrival time at the call-out can't be "
                                       "estimated")
            key = (len(pushed), 10 ** 6 if extra is None else extra, k)
            if best is None or key < best[0]:
                best = (key, opt)
        options.append(best[1])

    options.sort(key=lambda o: (o["skill_rank"] <= 1, len(o["short_sla_jobs_pushed_back"]),
                                o["over_expected_jobs_per_day"],
                                10 ** 6 if o["extra_drive_minutes"] is None else o["extra_drive_minutes"],
                                -o["skill_rank"], o["engineer"]))
    out: dict[str, Any] = {
        "site": site, "priority": priority or None, "system_types": types,
        "system_types_basis": types_basis,
        "recommended": options[0] if options else None, "alternatives": options[1:3],
        "excluded_engineers": excluded,
    }
    if not options:
        out["problem"] = ("No engineer can be proposed (expired/missing competence evidence, apprentices only, or "
                          "no engineer records) - decide by hand.")
    else:
        top = options[0]
        out["suggested_booking"] = {
            "tool": "log_job (approval-gated - NOT done by this advice)",
            "args": {"site": site, "type": "callout", "priority": priority, "engineer": top["engineer"],
                     "scheduled_start": "",
                     "description": str(urgent.get("description") or "Urgent call-out")},
            "note": "Pick the time yourself; slot it " + (
                f"after {top['slot_after']}" + (f" and before {top['slot_before']}" if top["slot_before"] else "")
                + ". Any existing job that has to move needs an fsm_change, also approval-gated."),
        }
    return out


# --------------------------------------------------------------------------- the advice
def build_route_advice(*, jobs: list[dict[str, Any]], sites: list[dict[str, Any]], staff: list[dict[str, Any]],
                       register_people: list[dict[str, Any]], positions: list[dict[str, Any]], day: date,
                       today: date, working_hours: bool, urgent: dict[str, Any] | None = None,
                       demo: bool = False, missing: list[str] | None = None) -> dict[str, Any]:
    missing = list(missing or [])
    places = build_places(sites)
    engineers = build_engineers(staff, register_people)
    eng_by_key = {e["key"]: e for e in engineers}

    # Live positions: working hours and today only. Otherwise they are not looked at at all.
    live: dict[str, tuple[float, float]] = {}
    if working_hours and day == today:
        for p in positions:
            c = _coords(p)
            seen = p.get("last_seen_mins")
            if c is None or not p.get("engineer"):
                continue
            if isinstance(seen, (int, float)) and seen > STALE_POSITION_MINUTES:
                continue
            live[_key(p["engineer"])] = c
        location_note = (f"{len(live)} live van position(s) used as start points (working hours only)." if live else
                         "Working hours, but no usable live positions - start points come from jobs in progress.")
    elif day != today:
        location_note = "Planning another day, so no live locations are used."
    else:
        location_note = "Outside working hours - engineer locations are not read or used (private use)."

    names = {e["key"]: e["name"] for e in engineers}
    stops_by: dict[str, list[Stop]] = {}
    in_prog: dict[str, list[tuple[str, tuple[float, float] | None]]] = {}
    unassigned = []
    for n, j in enumerate(jobs):
        if _status(j) not in OPEN_STATUSES or parse_date(j.get("scheduled_start")) != day:
            continue
        ref = str(j.get("ref") or j.get("id") or "?")
        site = str(j.get("site") or "unknown")
        coords = resolve_place(j.get("site"), places).coords
        eng = str(j.get("engineer") or "").strip()
        if eng.casefold() in UNASSIGNED:
            unassigned.append({"job": ref, "site": site, "priority": j.get("priority") or None,
                               "type": j.get("type"), "scheduled_start": j.get("scheduled_start")})
            continue
        k = _key(eng)
        names.setdefault(k, eng)
        if _status(j) in IN_PROGRESS:
            in_prog.setdefault(k, []).append((ref, coords))
            continue
        prio = str(j.get("priority") or "")
        start = str(j.get("scheduled_start")) if j.get("scheduled_start") else None
        stops_by.setdefault(k, []).append(Stop(ref, site, prio, sla_hours(prio), coords, start, n))

    routes: dict[str, Route] = {}
    for k, name in names.items():
        r = Route(name, k, cap=eng_by_key[k]["cap"] if k in eng_by_key else None)
        r.baseline = sorted(stops_by.get(k, []), key=lambda s: (s.start is None, s.start or "", s.order))
        r.in_progress = [ref for ref, _ in in_prog.get(k, [])]
        if k in live:
            r.start, r.start_source = live[k], "live van position (working hours)"
        else:
            here = next((c for _, c in in_prog.get(k, []) if c), None)
            if here:
                r.start, r.start_source = here, "site of the job in progress"
        if all(s.coords for s in r.baseline):
            r.optimised = True
            r.proposed = optimise(r.start, r.baseline)
            if r.start is None:
                r.notes.append("Start point unknown: the first job's position is taken as given, so the saving "
                               "counts only the legs between jobs.")
        else:
            r.proposed = list(r.baseline)
            lost = [s.site for s in r.baseline if not s.coords]
            r.notes.append("Left in its current order: no location for " + ", ".join(sorted(set(lost)))
                           + " - add the site's postcode/coordinates in Salts FSM to include it.")
        routes[k] = r

    rows, changes = [], []
    saved_total, changed_count = 0, 0
    for r in sorted(routes.values(), key=lambda r: r.name):
        if not r.baseline and not r.in_progress:
            continue
        row: dict[str, Any] = {
            "engineer": r.name, "start_point": r.start_source, "jobs_in_progress": r.in_progress,
            "current_sequence": [s.view() for s in r.baseline], "optimised": r.optimised,
            "notes": list(r.notes)}
        if r.optimised:
            base = path_minutes(r.start, r.baseline)
            prop = path_minutes(r.start, r.proposed)
            keep = prop >= base and _tiers_respected(r.baseline)
            if keep:
                r.proposed, prop = list(r.baseline), base
            elif prop > base:
                row["notes"].append(f"Moving a short-SLA job ahead of other work costs {prop - base} more drive "
                                    "minute(s) than the current order, but keeps the SLA job first.")
            old = {id(s): i for i, s in enumerate(r.baseline)}
            moves = [{"job": s.ref, "site": s.site, "from_position": old[id(s)] + 1, "to_position": i + 1}
                     for i, s in enumerate(r.proposed) if old[id(s)] != i]
            saved = base - prop
            row.update({"proposed_sequence": [s.view() for s in r.proposed], "changed": bool(moves),
                        "moves": moves, "current_drive_minutes": base, "proposed_drive_minutes": prop,
                        "drive_minutes_saved": saved})
            if not moves:
                row["notes"].append("The current order is already the shortest found - no change suggested.")
            else:
                changed_count += 1
                saved_total += saved
                changes.append({
                    "tool": "fsm_change (approval-gated - NOT done by this advice)", "engineer": r.name,
                    "proposed_order": [s.ref for s in r.proposed],
                    "note": f"Re-time {r.name}'s jobs to follow this order (existing times stay as slots; "
                            "check any customer-agreed appointment first). Only fsm_change / log_job can do this, "
                            "and both wait for the owner's approval."})
        else:
            row.update({"proposed_sequence": [s.view() for s in r.baseline], "changed": False, "moves": [],
                        "current_drive_minutes": None, "proposed_drive_minutes": None,
                        "drive_minutes_saved": None})
        rows.append(row)

    result: dict[str, Any] = {
        "advisory_only": True, "demo": demo,
        "note": "READ-ONLY advice. Nothing has been booked, moved or assigned. To act on it, use log_job or "
                "fsm_change - both are queued for the owner's approval first.",
        "date": day.isoformat(),
        "locations": {"working_hours": bool(working_hours), "live_positions_used": len(live), "note": location_note},
        "assumptions": {
            "distance": "straight-line distance with a road factor at a typical local speed (same helpers as "
                        "nearest_engineer) - no live traffic",
            "sla": f"jobs with an SLA of {SLA_URGENT_HOURS}h or less are kept ahead of jobs without one; other "
                   "priorities are flexible within the day",
            "appointments": "customer-agreed appointment times and job durations aren't known - only the sequence "
                            "is proposed, never new times",
            "engineers": "jobs stay with their current engineer; only the urgent call-out compares engineers",
            "skills": "certificate-name keyword matching, with role/duties as a labelled inference - never assumed",
        },
        "summary": {"engineers_with_jobs": len(rows), "engineers_with_a_better_order": changed_count,
                    "total_drive_minutes_saved": saved_total, "unassigned_jobs": len(unassigned)},
        "routes": rows,
        "unassigned_jobs": unassigned,
        "suggested_changes": changes,
        "data_quality": {"missing_or_failed": missing},
    }
    if urgent and str(urgent.get("site") or "").strip():
        result["urgent_callout"] = _urgent_advice(urgent, places, engineers, routes, day)
    return result


class RouteAdvisor:
    """Read-only. Fetches from Salts FSM and the tracker (never writes) and hands the lot to ``build_route_advice``."""

    def __init__(self, fsm, tracker, register=None):
        self.fsm = fsm
        self.tracker = tracker
        self.register = register

    @property
    def demo(self) -> bool:
        return getattr(self.fsm, "demo", False)

    async def advise(self, day: date | None = None, urgent_site: str = "", urgent_description: str = "",
                     urgent_priority: str = "", urgent_system_type: str = "",
                     today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        day = day or today
        missing: list[str] = []
        try:
            jobs = list(await self.fsm.jobs(date_from=day, date_to=day))
        except Exception as e:  # noqa: BLE001
            return {"advisory_only": True, "error": f"Couldn't read the jobs from Salts FSM ({type(e).__name__}), "
                                                    "so there is nothing to route."}
        sites = await PPMPlanner._fetch("Sites (locations/postcodes)", self.fsm.sites, missing)
        staff = await PPMPlanner._fetch("Engineers/staff", self.fsm.staff, missing)
        people: list[dict[str, Any]] = []
        if self.register is not None:
            try:
                people = self.register.people()
            except Exception as e:  # noqa: BLE001
                missing.append(f"Staff register could not be read ({type(e).__name__})")
        else:
            missing.append("Staff register unavailable - expected jobs-per-day and role/duty skill inference "
                           "not available")
        positions: list[dict[str, Any]] = []
        working = False
        if day == today:
            try:
                live = await self.tracker.live()
                working = bool(live.get("working_hours"))
                positions = list(live.get("engineers") or []) if working else []
            except Exception as e:  # noqa: BLE001
                missing.append(f"Live engineer locations could not be read ({type(e).__name__}) - start points "
                               "come from jobs in progress")
        urgent = None
        if str(urgent_site or "").strip():
            urgent = {"site": urgent_site, "description": urgent_description, "priority": urgent_priority,
                      "system_type": urgent_system_type}
            try:
                found = await self.tracker._geocode(urgent_site)  # site records first, then a UK postcode lookup
                if found:
                    urgent["coords"] = found[0]
            except Exception as e:  # noqa: BLE001
                missing.append(f"The call-out location lookup failed ({type(e).__name__})")
        if not jobs:
            missing.append("No jobs returned for this day - there is nothing to re-sequence")
        return build_route_advice(jobs=jobs, sites=sites, staff=staff, register_people=people, positions=positions,
                                  day=day, today=today, working_hours=working, urgent=urgent, demo=self.demo,
                                  missing=missing)
