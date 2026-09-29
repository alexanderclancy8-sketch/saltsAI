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
                          "speed_mph": p.get("speed_mph"),
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
            row = {"engineer": p.get("engineer"), "vehicle": p.get("vehicle"), "lat": here[0], "lng": here[1],
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
        return {"working_hours": True, "demo": self.demo, "engineers": rows,
                "sites": [{"name": n, "lat": c[0], "lng": c[1]} for n, c in sites.items() if n in today_sites]}

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
        ranked = sorted(({"engineer": e["engineer"], "distance_miles": round(haversine_m((e["lat"], e["lng"]), coords) / 1609.34, 1),
                          "eta_mins": drive_minutes(haversine_m((e["lat"], e["lng"]), coords)),
                          "currently": e.get("current_job") or e.get("status"), "next_job": e.get("next_job")}
                         for e in live["engineers"]), key=lambda r: r["distance_miles"])
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
        for v in vehicles:
            if wanted_reg and str(v.get("registration") or "").replace(" ", "").upper() == wanted_reg:
                return {**v, "engineer": engineer}
            if v.get("driver") and engineer.lower() in str(v["driver"]).lower():
                return {**v, "engineer": v["driver"]}
        return None

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
                    "summary": "No journeys recorded - the van didn't move (day off, sick, or used another vehicle)."}
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
                "miles": round(miles, 1), "timeline": timeline, "demo": getattr(self.ram, "demo", False)}

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
