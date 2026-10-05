"""Engineer / van tracking from Salts FSM: live map, nearest engineer for a call-out,
check-in verification and idle / late-arrival flags.

Only used for work purposes during working hours. Staff must be told vehicles are
tracked and why (UK GDPR transparency; ICO employment practices guidance).

Outside working hours van locations stay hidden ("private use") unless the OWNER has switched on the setting
`van_locations_out_of_hours` ("on_call" = only engineers on the on-call roster, "always" = every van). Even then the
caller must say who is asking (`asked_by` - an unattributed background caller is treated as "off"), the look-up is
logged per engineer (db.location_lookup_log) BEFORE anything is returned, and if the log can't be written nothing is
shown. A missing setting, missing database or unknown value all mean "off".

"At home": RAM's public API supplies no address labels, so `at_home` comes from the owner's engineer home points
(services/engineer_homes.py - a rounded map point per engineer, set by the owner in Settings, never a postcode): a van
within the owner's radius of its driver's point is "home". A RAM label containing the word home still counts, but only as a
fallback when RAM supplies one. Whatever the signal, a van at home is shown as just "home" - never a street, a point or a
distance - and the point itself never leaves `EngineerHomes` (nothing here returns it).
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx

from ..integrations.ramtracking import RamError
from .oncall import OnCallRoster, name_matches

log = logging.getLogger(__name__)

OOH_MODES = ("off", "on_call", "always")
PANEL_LOG_MINUTES = 10  # the Fleet panel refreshes every minute: log it once per engineer per this many minutes
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


def combine_home(shown: tuple[str | None, bool | None], state: bool | None) -> tuple[str | None, bool | None]:
    """(label to show, at_home) from the RAM label result (`label_status`) and the home-point result (`state`: True within the
    radius of the driver's stored home, False elsewhere, None no home point for them). At home by either signal is shown as
    just "home" when it came from the point (RAM's label for a van parked at home could be the street); a home point makes
    "not at home" a definite answer, no home point and no label leaves it unknown (None)."""
    label, at_home = shown
    if state is True:
        return "home", True
    if at_home is True:
        return label, True            # RAM's own label says home (fallback; only exists when RAM supplies labels)
    if state is False:
        return label, False
    return label, at_home             # no home point: whatever the label said (False) or unknown (None)


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


def requester_label(settings: Any, speaker: str | None, quiet: bool = False) -> str:
    """Who is asking, for the out-of-hours look-up log: the named person if the chat knows them, a background
    automation, or else the signed-in display (the owner's own session)."""
    if speaker:
        return str(speaker)
    return "automation" if quiet else f"{getattr(settings, 'owner_name', '') or 'owner'} (display)"


class Tracker:
    def __init__(self, fsm, http: httpx.AsyncClient, ram=None, register=None, tolerance_min: int = 30,
                 settings=None, db=None, homes=None):
        self.fsm = fsm
        self.http = http
        self.ram = ram  # RAM Tracking (or demo stand-in) - journeys and positions from the vans
        self.register = register
        self.tolerance = tolerance_min
        self.settings = settings  # for van_locations_out_of_hours; None = always "off"
        self.db = db  # for the out-of-hours look-up log and the on-call roster; None = always "off"
        self.roster = OnCallRoster(db) if db is not None else None
        self.homes = homes  # EngineerHomes: the owner's home points; None = only RAM's label can say "home"

    @property
    def demo(self) -> bool:
        return getattr(self.fsm, "demo", False)

    @staticmethod
    def in_working_hours(now: datetime | None = None) -> bool:
        now = now or datetime.now()
        return now.weekday() < 5 and WORK_START <= now.time() <= WORK_END

    # ------------------------------------------------------------------ out-of-hours policy
    @property
    def ooh_mode(self) -> str:
        """The owner's setting. Anything unexpected - or no way to keep the log - is "off"."""
        if self.settings is None or self.db is None:
            return "off"
        mode = str(getattr(self.settings, "van_locations_out_of_hours", "off") or "off").strip().lower()
        return mode if mode in OOH_MODES else "off"

    def _ooh_policy(self, now: datetime, asked_by: str) -> tuple[str, list[str]]:
        """(mode, names on call) that apply to this look-up outside working hours. "off" when nobody is named as
        asking, so a background caller that doesn't identify itself never sees positions out of hours."""
        mode = self.ooh_mode
        if mode == "off" or not str(asked_by or "").strip():
            return "off", []
        return mode, (self.roster.on_call(now) if mode == "on_call" else [])

    @staticmethod
    def _on_call_match(on_call: list[str], engineer: Any) -> bool:
        return any(name_matches(n, engineer) for n in on_call)

    def _log_lookups(self, asked_by: str, tool: str, engineers: list[str], mode: str) -> None:
        """Record who looked at which engineer out of hours. Raises if it can't - the caller then shows nothing."""
        if self.db is None:
            return
        for name in dict.fromkeys(str(e) for e in engineers if e):
            if tool == "fleet_panel" and self.db.recent_location_lookup(asked_by, tool, name, PANEL_LOG_MINUTES):
                continue
            self.db.log_location_lookup(asked_by, tool, name, mode)

    @staticmethod
    def _blocked(mode: str) -> dict[str, Any]:
        if mode == "on_call":
            why = "Outside working hours - van locations are shown only for engineers on call, and nobody is on call now."
        else:
            why = "Outside working hours - locations are not shown (private use)."
        return {"working_hours": False, "visible": False, "engineers": [], "sites": [], "note": why}

    def _home_state(self, engineer: Any, here: tuple[float, float] | None) -> bool | None:
        """Within the radius of this engineer's home point? (True / False, or None with no point.) Never raises."""
        if self.homes is None:
            return None
        try:
            return self.homes.state(engineer, here)
        except Exception as e:  # noqa: BLE001 - a broken home table must not take the Fleet panel down
            log.warning("Home check failed (%s)", type(e).__name__)
            return None

    async def _sites(self) -> dict[str, tuple[float, float]]:
        out = {}
        for s in await self.fsm.sites():
            try:
                out[str(s.get("name"))] = (float(s["lat"]), float(s["lng"]))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def live(self, asked_by: str = "", tool: str = "engineer_locations") -> dict[str, Any]:
        """Where the vans are. `working_hours` is True in working hours (and for demo data); `visible` says whether
        positions are being shown at all - outside working hours that needs the owner's setting AND `asked_by`."""
        now = datetime.now()
        out_of_hours = not self.in_working_hours(now) and not self.demo
        mode, on_call = "working_hours", []
        if out_of_hours:
            mode, on_call = self._ooh_policy(now, asked_by)
            if mode == "off" or (mode == "on_call" and not on_call):
                return self._blocked(mode)
        if self.ram is not None and not getattr(self.ram, "demo", True):
            try:
                ram_positions = await self.ram.positions()
            except RamError as e:
                if e.rate_limited:  # RAM is busy (3 requests a minute), not broken: no vans this moment, and no alarm
                    return {"working_hours": True, "demo": False, "engineers": [], "sites": [], "rate_limited": True,
                            "note": str(e)}
                # RAM is set up but isn't answering (wrong address, refused sign-in...): say so, and show no vans. The
                # console turns this into "not connected" with the reason; it must never be a blank, working-looking map.
                return {"working_hours": True, "demo": False, "engineers": [], "sites": [], "ram_error": str(e),
                        "note": f"Vehicle tracking isn't working: {e}"}
            positions = [{"engineer": p.get("driver") or await self._driver_for(p), "vehicle": p.get("registration"),
                          "lat": p.get("lat"), "lng": p.get("lng"), "timestamp": p.get("timestamp"),
                          "speed_mph": p.get("speed_mph"), "address_label": p.get("address_label"),
                          "status": "driving" if (p.get("speed_mph") or 0) > 3 else "parked"}
                         for p in ram_positions]
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
            label, at_home = combine_home(label_status(p.get("address_label"), sites),
                                          self._home_state(p.get("engineer"), here))
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
        if out_of_hours and mode == "on_call":
            rows = [r for r in rows if self._on_call_match(on_call, r.get("engineer"))]
        if out_of_hours:
            # Logged before anything is returned: if the record can't be kept, nothing is shown.
            self._log_lookups(asked_by, tool, [str(r.get("engineer") or r.get("vehicle") or "") for r in rows], mode)
        today_sites = {j.get("site") for j in jobs}
        out = {"working_hours": not out_of_hours, "visible": True, "demo": self.demo, "engineers": rows,
               "sites": [{"name": n, "lat": c[0], "lng": c[1]} for n, c in sites.items() if n in today_sites]}
        if out_of_hours:
            out["out_of_hours_access"] = mode
            out["note"] = ("Outside working hours: shown because the owner has allowed it (" +
                           ("only engineers on call: " + ", ".join(on_call) if mode == "on_call" else "all vans") +
                           "). This look-up has been logged.")
            if mode == "on_call":
                out["on_call"] = on_call
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
        unknown = [str(r.get("engineer") or r.get("vehicle")) for r in rows if r["at_home"] is None]
        if unknown:
            out["address_label_note"] = ("No home set for, and RAM Tracking didn't supply an address label for: "
                                         f"{', '.join(unknown)} - can't say whether they're at home.")
        return out

    async def home_status(self, asked_by: str = "") -> dict[str, Any]:
        """Who is at home, who is out, who has no recent position (working hours, or outside them only when the
        owner's setting allows it - see live(), which also logs the look-up)."""
        live = await self.live(asked_by, tool="who_is_home")
        res: dict[str, Any] = {"working_hours": live.get("working_hours", False), "at_home": [], "out": [],
                               "no_address_label": [], "no_recent_position": []}
        if not live.get("visible"):
            res["note"] = live.get("note")
            return res
        out_of_hours = not live.get("working_hours")
        if out_of_hours:
            res["out_of_hours_access"] = live.get("out_of_hours_access")
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
                    if out_of_hours and live.get("out_of_hours_access") == "on_call" \
                            and not self._on_call_match(live.get("on_call") or [], name):
                        continue  # out of hours, only the engineers on call are named at all
                    if str(name) not in seen_names:
                        res["no_recent_position"].append({"engineer": name, "vehicle": v.get("registration"),
                                                          "address_label": None, "last_seen_mins": None,
                                                          "reason": "RAM Tracking has no position for this van"})
                        if out_of_hours:  # live() logged the engineers it showed; log these named ones too
                            self._log_lookups(asked_by, "who_is_home", [str(name)], str(live.get("out_of_hours_access")))
        res["note"] = ("'At home' means the van is within the owner's set distance of that engineer's home point (or RAM's "
                       "address label for it says home). 'no_address_label' lists vans for which no home is set and RAM "
                       "supplies no label, so it can't be told either way - the owner sets homes in Settings. Say only "
                       "'home', never where. " +
                       ("Outside working hours this is shown only because the owner has allowed it, and the look-up "
                        "was logged." if out_of_hours else "Working hours only."))
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

    async def nearest(self, place: str, asked_by: str = "") -> dict[str, Any]:
        target = await self._geocode(place)
        if not target:
            return {"error": f"Couldn't locate '{place}'. Give a site name from Salts FSM or a UK postcode."}
        coords, label = target
        live = await self.live(asked_by, tool="nearest_engineer")
        # Rank on the exact distance, not the rounded display figure, and break any remaining tie by engineer name
        # so the suggestion never depends on the order the position feed happened to return.
        measured = sorted(((haversine_m((e["lat"], e["lng"]), coords), e) for e in live["engineers"]),
                          key=lambda de: (de[0], str(de[1].get("engineer") or "")))
        ranked = [{"engineer": e["engineer"], "distance_miles": round(d / 1609.34, 1), "eta_mins": drive_minutes(d),
                   "currently": e.get("current_job") or e.get("status"), "next_job": e.get("next_job"),
                   "address_label": e.get("address_label"), "at_home": e.get("at_home")}
                  for d, e in measured]
        note = "ETAs are straight-line estimates at typical local speeds, not live traffic."
        if not live.get("visible", True) or live.get("out_of_hours_access"):
            note = f"{live.get('note')} {note}"  # why the list is empty, or that it is out-of-hours and logged
        return {"destination": label, "demo": self.demo, "engineers": ranked, "note": note}

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

    async def _van_extras(self, vehicle: dict[str, Any], day: date, no_journeys: bool, asked_by: str = "",
                          tool: str = "van_day") -> dict[str, Any]:
        """Mismatch warnings plus (today) RAM's current address label for the van - in working hours, or outside
        them only when the owner's setting allows it for this engineer (and the look-up is then logged)."""
        out: dict[str, Any] = {}
        now = datetime.now()
        out_of_hours = not self.in_working_hours(now) and not self.demo
        show_label = not out_of_hours
        if out_of_hours and str(asked_by or "").strip():
            # Looking at an engineer's van out of hours is always recorded, whatever the setting says.
            mode, on_call = self._ooh_policy(now, asked_by)
            self._log_lookups(asked_by, tool, [str(vehicle.get("engineer") or vehicle.get("registration"))],
                              self.ooh_mode)
            show_label = mode == "always" or (mode == "on_call" and self._on_call_match(on_call, vehicle.get("engineer")))
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
        if day == date.today() and show_label:
            here = None
            try:
                fix_age = (now - fix).total_seconds() / 60 if fix else None
                if fix_age is not None and fix_age <= STALE_POSITION_MINS:  # an old fix isn't where the van is now
                    here = (float(vehicle["lat"]), float(vehicle["lng"]))
            except (KeyError, TypeError, ValueError):
                here = None
            label, at_home = combine_home(label_status(vehicle.get("address_label"), await self._sites()),
                                          self._home_state(vehicle.get("engineer"), here))
            out["current_address_label"] = label
            out["at_home"] = at_home
            if at_home is None:
                out["address_label_note"] = ("No home is set for this engineer and RAM Tracking supplied no address "
                                             "label for this van, so it can't be said whether they are at home.")
        return out

    async def van_day(self, engineer: str, day: date, asked_by: str = "", tool: str = "van_day") -> dict[str, Any]:
        """When did they set off, where did they go, how long on site, when did they get home. A look-up made
        outside working hours (with `asked_by` given) is logged; today's address label is only added out of hours
        when the owner's setting allows it for this engineer."""
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
                    **await self._van_extras(vehicle, day, True, asked_by, tool)}
        sites = await self._sites()

        def site_name(lat: Any, lng: Any, fallback: Any) -> str:
            try:
                here = (float(lat), float(lng))
            except (TypeError, ValueError):
                return str(fallback or "unknown")
            best = min(sites.items(), key=lambda kv: haversine_m(here, kv[1]), default=None)
            if best and haversine_m(here, best[1]) <= ONSITE_METRES:
                return best[0]
            if self._home_state(vehicle.get("engineer"), here):
                return "home"  # never the engineer's coordinates, which would be their home point
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
                **await self._van_extras(vehicle, day, False, asked_by, tool)}

    async def timesheet_check(self, day: date, asked_by: str = "") -> dict[str, Any]:
        """Compare RAM Tracking working day (set off -> home) with Salts FSM timesheet hours."""
        sheets = {t.get("engineer"): float(t.get("hours") or 0)
                  for t in await self.fsm.timesheets(day, day) if str(t.get("date"))[:10] == day.isoformat()}
        rows, flags = [], []
        for s in await self.fsm.staff():
            van = await self.van_day(s["name"], day, asked_by, tool="timesheet_check")
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
