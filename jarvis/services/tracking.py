"""Engineer / van tracking from Salts FSM: live map, nearest engineer for a call-out,
check-in verification and idle / late-arrival flags.

Only used for work purposes during working hours. Staff must be told vehicles are
tracked and why (UK GDPR transparency; ICO employment practices guidance).
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx

ONSITE_METRES = 400
ROAD_FACTOR = 1.35  # straight line -> road distance
AVG_MPH = 26  # urban/suburban West Yorkshire average
WORK_START, WORK_END = time(7, 0), time(18, 30)
POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.I)
HOME_WORD = re.compile(r"\bhome\b", re.I)
STALE_POSITION_MINS = 60  # a fix older than this isn't "where they are now"
MAX_LABEL_CHARS = 120


def label_status(label: Any, site_names: Any = ()) -> tuple[str | None, bool | None]:
    """(label to show, at_home) from the address label RAM already supplies - nothing is stored.

    at_home is True when the label contains the word "home" (case-insensitive), False for any other label and None
    when RAM supplied none (unknown - not the same as "out"). A home label is shown as just "home": the rest of it
    could be a street address and must never reach chat, the display or the logs. A customer site whose own name
    has the word in it (a care home) is not somebody's house.
    """
    text = " ".join(str(label).split()) if label else ""
    if not text:
        return None, None
    low = text.lower()
    if any(n and HOME_WORD.search(n) and str(n).lower() in low for n in site_names):
        return text[:MAX_LABEL_CHARS], False
    if HOME_WORD.search(text):
        return "home", True
    return text[:MAX_LABEL_CHARS], False


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def drive_minutes(metres: float) -> int:
    return max(1, round(metres * ROAD_FACTOR / 1609.34 / AVG_MPH * 60))


def _ts(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


class Tracker:
    def __init__(self, fsm, http: httpx.AsyncClient, ram=None, register=None, tolerance_min: int = 30):
        self.fsm = fsm
        self.http = http
        self.ram = ram  # RAM Tracking (or demo stand-in) - journeys and positions from the vans
        self.register = register
        self.tolerance = tolerance_min

    @property
    def demo(self) -> bool:
        return getattr(self.fsm, "demo", False)

    @staticmethod
    def in_working_hours(now: datetime | None = None) -> bool:
        now = now or datetime.now()
        return now.weekday() < 5 and WORK_START <= now.time() <= WORK_END

    async def _sites(self) -> dict[str, tuple[float, float]]:
        out = {}
        for s in await self.fsm.sites():
            try:
                out[str(s.get("name"))] = (float(s["lat"]), float(s["lng"]))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def live(self) -> dict[str, Any]:
        now = datetime.now()
        if not self.in_working_hours(now) and not self.demo:
            return {"working_hours": False, "engineers": [], "sites": [],
                    "note": "Outside working hours - locations are not shown (private use)."}
        if self.ram is not None and not getattr(self.ram, "demo", True):
            positions = [{"engineer": p.get("driver") or await self._driver_for(p), "vehicle": p.get("registration"),
                          "lat": p.get("lat"), "lng": p.get("lng"), "timestamp": p.get("timestamp"),
                          "speed_mph": p.get("speed_mph"), "address_label": p.get("address_label"),
                          "status": "driving" if (p.get("speed_mph") or 0) > 3 else "parked"}
                         for p in await self.ram.positions()]
        else:
            positions = await self.fsm.locations()
        sites = await self._sites()
        jobs = await self.fsm.jobs(now.date(), now.date())
        rows = []
        for p in positions:
            try:
                here = (float(p["lat"]), float(p["lng"]))
            except (KeyError, TypeError, ValueError):
                continue
            seen = _ts(p.get("timestamp"))
            current = next((j for j in jobs if j.get("engineer") == p.get("engineer")
                            and str(j.get("status")).lower() in ("in_progress", "started", "on_site")), None)
            nxt = next((j for j in sorted(jobs, key=lambda j: str(j.get("scheduled_start")))
                        if j.get("engineer") == p.get("engineer")
                        and str(j.get("status")).lower() in ("scheduled", "assigned", "booked")), None)
            label, at_home = label_status(p.get("address_label"), sites)
            row = {"engineer": p.get("engineer"), "vehicle": p.get("vehicle"), "lat": here[0], "lng": here[1],
                   "address_label": label, "at_home": at_home,
                   "status": p.get("status"), "speed_mph": p.get("speed_mph"),
                   "last_seen_mins": int((now - seen).total_seconds() // 60) if seen else None,
                   "current_job": f"{current.get('ref')} {current.get('site')}" if current else None,
                   "next_job": f"{str(nxt.get('scheduled_start'))[11:16]} {nxt.get('site')}" if nxt else None}
            if current and current.get("site") in sites:
                d = haversine_m(here, sites[current["site"]])
                row["distance_from_job_site_m"] = round(d)
                row["on_site"] = d <= ONSITE_METRES
            if nxt and nxt.get("site") in sites:
                row["eta_next_job_mins"] = drive_minutes(haversine_m(here, sites[nxt["site"]]))
            rows.append(row)
        today_sites = {j.get("site") for j in jobs}
        out = {"working_hours": True, "demo": self.demo, "engineers": rows,
               "sites": [{"name": n, "lat": c[0], "lng": c[1]} for n, c in sites.items() if n in today_sites]}
        # Mismatches worth knowing about rather than silently showing: one engineer on two vans gives two positions.
        vans_by_engineer: dict[str, list[str]] = {}
        for r in rows:
            if r.get("engineer"):
                vans_by_engineer.setdefault(str(r["engineer"]), []).append(str(r.get("vehicle")))
        warnings = [f"{name} is listed as the driver of {len(vans)} vans ({', '.join(vans)}) in RAM Tracking, so "
                    "they show here at more than one position - check which van they are actually using."
                    for name, vans in vans_by_engineer.items() if len(vans) > 1]
        if warnings:
            out["warnings"] = warnings
        unlabelled = [str(r.get("engineer") or r.get("vehicle")) for r in rows if r["address_label"] is None]
        if unlabelled:
            out["address_label_note"] = ("RAM Tracking didn't supply an address label for: "
                                         f"{', '.join(unlabelled)} - can't say whether they're at home.")
        return out

    async def home_status(self) -> dict[str, Any]:
        """Who is at home, who is out, who has no recent position (working hours only, via live())."""
        live = await self.live()
        res: dict[str, Any] = {"working_hours": live.get("working_hours", False), "at_home": [], "out": [],
                               "no_address_label": [], "no_recent_position": []}
        if not live.get("working_hours"):
            res["note"] = live.get("note")
            return res
        seen_names = set()
        for e in live["engineers"]:
            seen_names.add(str(e.get("engineer")))
            entry = {"engineer": e.get("engineer"), "vehicle": e.get("vehicle"), "address_label": e.get("address_label"),
                     "last_seen_mins": e.get("last_seen_mins")}
            mins = e.get("last_seen_mins")
            if mins is None or mins > STALE_POSITION_MINS:
                res["no_recent_position"].append({**entry, "reason": "no timestamp on the last position" if mins is None
                                                  else f"last position was {mins} minutes ago"})
            elif e.get("at_home") is None:
                res["no_address_label"].append(entry)
            elif e["at_home"]:
                res["at_home"].append(entry)
            else:
                res["out"].append(entry)
        if self.ram is not None and not getattr(self.ram, "demo", True):
            for v in await self.ram.vehicles():
                if v.get("lat") is None or v.get("lng") is None:
                    name = v.get("driver") or await self._driver_for(v) or v.get("registration")
                    if str(name) not in seen_names:
                        res["no_recent_position"].append({"engineer": name, "vehicle": v.get("registration"),
                                                          "address_label": None, "last_seen_mins": None,
                                                          "reason": "RAM Tracking has no position for this van"})
        res["note"] = ("'At home' means RAM's address label for the van contains the word home; a van with no label "
                       "is listed separately because it can't be told either way. Working hours only.")
        return res

    async def _geocode(self, place: str) -> tuple[tuple[float, float], str] | None:
        sites = await self._sites()
        for name, coords in sites.items():
            if place.lower() in name.lower():
                return coords, name
        m = POSTCODE.search(place)
        if m:
            r = await self.http.get(f"https://api.postcodes.io/postcodes/{m.group(1).replace(' ', '')}", timeout=15)
            if r.status_code == 200:
                res = r.json()["result"]
                return (res["latitude"], res["longitude"]), m.group(1).upper()
        return None

    async def nearest(self, place: str) -> dict[str, Any]:
        target = await self._geocode(place)
        if not target:
            return {"error": f"Couldn't locate '{place}'. Give a site name from Salts FSM or a UK postcode."}
        coords, label = target
        live = await self.live()
        # Rank on the exact distance, not the rounded display figure, and break any remaining tie by engineer name
        # so the suggestion never depends on the order the position feed happened to return.
        measured = sorted(((haversine_m((e["lat"], e["lng"]), coords), e) for e in live["engineers"]),
                          key=lambda de: (de[0], str(de[1].get("engineer") or "")))
        ranked = [{"engineer": e["engineer"], "distance_miles": round(d / 1609.34, 1), "eta_mins": drive_minutes(d),
                   "currently": e.get("current_job") or e.get("status"), "next_job": e.get("next_job"),
                   "address_label": e.get("address_label"), "at_home": e.get("at_home")}
                  for d, e in measured]
        return {"destination": label, "demo": self.demo, "engineers": ranked,
                "note": "ETAs are straight-line estimates at typical local speeds, not live traffic."}

    async def attendance(self, day: datetime | None = None) -> dict[str, Any]:
        """Check that job check-ins happened at the job site and flag late arrivals."""
        day = day or datetime.now()
        jobs = await self.fsm.jobs(day.date(), day.date())
        sites = await self._sites()
        flags, checked = [], 0
        for j in jobs:
            started, sched = _ts(j.get("started_at")), _ts(j.get("scheduled_start"))
            if started and sched and started - sched > timedelta(minutes=30):
                flags.append({"job": j.get("ref"), "engineer": j.get("engineer"), "site": j.get("site"),
                              "issue": f"arrived {int((started - sched).total_seconds() // 60)} min after booked time"})
            if j.get("checkin_lat") and j.get("site") in sites:
                checked += 1
                d = haversine_m((float(j["checkin_lat"]), float(j["checkin_lng"])), sites[j["site"]])
                if d > ONSITE_METRES:
                    flags.append({"job": j.get("ref"), "engineer": j.get("engineer"), "site": j.get("site"),
                                  "issue": f"checked in {d / 1609.34:.1f} miles from the site"})
        return {"date": day.date().isoformat(), "jobs": len(jobs), "checkins_with_location": checked, "flags": flags,
                "note": "Late arrival can be traffic or a previous job overrunning - ask before assuming."}

    # ------------------------------------------------------------------ RAM Tracking journeys
    async def _driver_for(self, position: dict[str, Any]) -> str | None:
        reg = str(position.get("registration") or "").replace(" ", "").upper()
        if self.register is not None:
            for p in self.register.people("engineer"):
                if str(p.get("vehicle") or "").replace(" ", "").upper() == reg and reg:
                    return p["name"]
        return None

    async def _vehicle_for(self, engineer: str) -> dict[str, Any] | None:
        vehicles = await self.ram.vehicles()
        wanted_reg = None
        if self.register is not None:
            person = self.register.find(engineer)
            if person:
                engineer = person["name"]
                wanted_reg = str(person.get("vehicle") or "").replace(" ", "").upper() or None
        # The van registered to them wins outright. (Matching on RAM's driver name in the same pass let an earlier
        # van that also lists them as driver shadow their own van, so van_day looked at the wrong vehicle.)
        match, name = None, engineer
        if wanted_reg:
            match = next((v for v in vehicles
                          if str(v.get("registration") or "").replace(" ", "").upper() == wanted_reg), None)
        if match is None:
            match = next((v for v in vehicles if v.get("driver") and engineer.lower() in str(v["driver"]).lower()),
                         None)
            if match is not None:
                name = match["driver"]
        if match is None:
            return None
        others = [str(v.get("registration")) for v in vehicles if v is not match and v.get("driver")
                  and str(v["driver"]).strip().lower() == str(name).strip().lower()]
        return {**match, "engineer": name, "other_vehicles": others}

    async def _van_extras(self, vehicle: dict[str, Any], day: date, no_journeys: bool) -> dict[str, Any]:
        """Mismatch warnings plus (today, working hours only) RAM's current address label for the van."""
        out: dict[str, Any] = {}
        warnings = []
        if vehicle.get("other_vehicles"):
            warnings.append(f"RAM lists {vehicle['engineer']} as driver of more than one van "
                            f"({', '.join([str(vehicle.get('registration'))] + vehicle['other_vehicles'])}), so "
                            "positions and journeys can differ between vans - check which one they used.")
        fix = _ts(vehicle.get("timestamp"))
        if no_journeys and fix and fix.date() == day:
            warnings.append(f"RAM has a position for this van at {fix.strftime('%H:%M')} on {day.isoformat()} but "
                            "no journeys: it may be mid-journey (a trip only counts once RAM has both its start and "
                            "its stop), or the start/stop events were missed. Treat 'didn't move' with caution.")
        if warnings:
            out["warnings"] = warnings
        if day == date.today() and (self.in_working_hours() or self.demo):
            label, at_home = label_status(vehicle.get("address_label"), await self._sites())
            out["current_address_label"] = label
            out["at_home"] = at_home
            if label is None:
                out["address_label_note"] = "RAM Tracking supplied no address label for this van."
        return out

    async def van_day(self, engineer: str, day: date) -> dict[str, Any]:
        """When did they set off, where did they go, how long on site, when did they get home."""
        if self.ram is None:
            return {"error": "RAM Tracking isn't connected (RAM_API_BASE_URL / RAM_API_KEY)."}
        vehicle = await self._vehicle_for(engineer)
        if not vehicle:
            return {"error": f"No van found for {engineer}. Add 'vehicle: <registration>' for them in the staff "
                             "register, or assign the driver in RAM Tracking."}
        legs = await self.ram.journeys(str(vehicle["id"]), day)
        if not legs:
            return {"engineer": vehicle["engineer"], "date": day.isoformat(), "vehicle": vehicle.get("registration"),
                    "summary": "No journeys recorded - the van didn't move (day off, sick, or used another vehicle).",
                    **await self._van_extras(vehicle, day, True)}
        sites = await self._sites()

        def site_name(lat: Any, lng: Any, fallback: Any) -> str:
            try:
                here = (float(lat), float(lng))
            except (TypeError, ValueError):
                return str(fallback or "unknown")
            best = min(sites.items(), key=lambda kv: haversine_m(here, kv[1]), default=None)
            if best and haversine_m(here, best[1]) <= ONSITE_METRES:
                return best[0]
            return str(fallback or f"{here[0]:.4f},{here[1]:.4f}")

        timeline, driving, stopped, miles = [], 0.0, 0.0, 0.0
        for n, leg in enumerate(legs):
            st, en = _ts(leg.get("start_time")), _ts(leg.get("end_time"))
            if not st or not en:
                continue
            driving += (en - st).total_seconds() / 60
            miles += float(leg.get("distance_miles") or 0)
            dest = site_name(leg.get("end_lat"), leg.get("end_lng"), leg.get("end_address"))
            nxt = _ts(legs[n + 1].get("start_time")) if n + 1 < len(legs) else None
            stay = round((nxt - en).total_seconds() / 60) if nxt else None
            if stay:
                stopped += stay
            timeline.append({"depart": st.strftime("%H:%M"), "arrive": en.strftime("%H:%M"), "to": dest,
                             "drive_mins": round((en - st).total_seconds() / 60),
                             "stayed_mins": stay, "miles": leg.get("distance_miles")})
        set_off, home = _ts(legs[0].get("start_time")), _ts(legs[-1].get("end_time"))
        working_mins = (home - set_off).total_seconds() / 60 if set_off and home else None
        return {"engineer": vehicle["engineer"], "vehicle": vehicle.get("registration"), "date": day.isoformat(),
                "set_off": set_off.strftime("%H:%M") if set_off else None,
                "first_site_arrival": timeline[0]["arrive"] if timeline else None,
                "left_last_site": timeline[-1]["depart"] if timeline else None,
                "got_home": home.strftime("%H:%M") if home else None,
                "working_day_hours": round(working_mins / 60, 2) if working_mins else None,
                "driving_hours": round(driving / 60, 2), "time_stopped_hours": round(stopped / 60, 2),
                "miles": round(miles, 1), "timeline": timeline, "demo": getattr(self.ram, "demo", False),
                **await self._van_extras(vehicle, day, False)}

    async def timesheet_check(self, day: date) -> dict[str, Any]:
        """Compare RAM Tracking working day (set off -> home) with Salts FSM timesheet hours."""
        sheets = {t.get("engineer"): float(t.get("hours") or 0)
                  for t in await self.fsm.timesheets(day, day) if str(t.get("date"))[:10] == day.isoformat()}
        rows, flags = [], []
        for s in await self.fsm.staff():
            van = await self.van_day(s["name"], day)
            tracked = van.get("working_day_hours")
            claimed = sheets.get(s["name"])
            row = {"engineer": s["name"], "set_off": van.get("set_off"), "got_home": van.get("got_home"),
                   "tracked_hours": tracked, "timesheet_hours": claimed, "miles": van.get("miles")}
            if tracked is not None and claimed is not None:
                diff = round(claimed - tracked, 2)
                row["difference_hours"] = diff
                if abs(diff) * 60 >= self.tolerance:
                    flags.append({**row, "issue": f"timesheet {'over' if diff > 0 else 'under'} tracked day by "
                                                  f"{abs(diff):.1f}h"})
            elif claimed and tracked is None:
                flags.append({**row, "issue": "timesheet hours but no van movement recorded"})
            elif tracked and not claimed:
                flags.append({**row, "issue": "van used but no timesheet entry"})
            rows.append(row)
        return {"date": day.isoformat(), "tolerance_minutes": self.tolerance, "engineers": rows, "flags": flags,
                "demo": getattr(self.ram, "demo", False) or getattr(self.fsm, "demo", False),
                "note": "Tracked day = first journey start to last journey end, so it includes travel from home. "
                        "Check your policy on whether first/last journeys are paid before raising a difference."}

    # ------------------------------------------------------------------ lone-worker safety
    async def lone_worker_check(self, overrun_min: int = 90, now: datetime | None = None) -> list[dict[str, Any]]:
        """Jobs still in progress well past their booked end - a prompt to check the engineer is OK."""
        now = now or datetime.now()
        if not self.in_working_hours(now) and not self.demo:
            return []
        concerns = []
        for j in await self.fsm.jobs(now.date(), now.date()):
            if str(j.get("status") or "").lower() not in ("in_progress", "started", "on_site"):
                continue
            end = _ts(j.get("scheduled_end"))
            if not end and _ts(j.get("started_at")):
                end = _ts(j.get("started_at")) + timedelta(hours=float(j.get("hours") or 3))
            if end and (now - end).total_seconds() / 60 >= overrun_min:
                concerns.append({"engineer": j.get("engineer"), "job": j.get("ref"), "site": j.get("site"),
                                 "booked_end": end.strftime("%H:%M"),
                                 "overrun_minutes": int((now - end).total_seconds() // 60)})
        return concerns
