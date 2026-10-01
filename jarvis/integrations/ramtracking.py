"""RAM Tracking (vehicle trackers) connector: live positions and journey history.

RAM's External API (https://api.qaifn.co.uk, Swagger at /swagger/docs) is OAuth2, not a
static API key: POST https://auth.qaifn.co.uk/oauth/token with the client ID/secret as
HTTP Basic auth and `grant_type=password&username=&password=` in the body returns a
short-lived bearer token (sent as `Authorization: bearer <token>` on every call after).
Tokens are cached here and refreshed a little before they expire.

There's no bulk "all vehicle positions" endpoint - each vehicle's current fix comes back
nested inside /api/v1/vehicle/for-account's vehicle_status.location, so positions() just
reshapes vehicles(). Journey legs aren't returned as legs either: /api/v1/history/{id}/
{from}/{to} returns a raw stream of location/ignition events, which journeys() groups
into legs between a TRANSIT_START and the TRANSIT_STOP that follows it.
"""

from __future__ import annotations

import logging
import random
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import yaml

from ..config import ROOT_DIR, Settings

log = logging.getLogger(__name__)

DEFAULT_ENDPOINTS = {
    "vehicles": "/api/v1/vehicle/for-account",
    "history": "/api/v1/history/{id}/{from}/{to}",  # {from}/{to} are ISO8601, URL-encoded
}


def _vehicle_row(v: dict[str, Any]) -> dict[str, Any]:
    status = v.get("vehicle_status") or {}
    loc = status.get("location") or {}
    driver = v.get("vehicle_driver") or {}
    last_event = status.get("last_event") or {}
    return {
        "id": v.get("id"),
        "registration": v.get("registration"),
        "driver": driver.get("name"),
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "timestamp": status.get("event_date"),
        # RAM's vehicle status doesn't include a live speed figure - the last ignition/transit
        # event is the closest signal available for a driving-vs-parked guess.
        "moving": last_event.get("event") in ("TRANSIT_START", "OVER_SPEED"),
    }


class RamTracking:
    demo = False

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.s = settings
        self.http = http
        self.endpoints = dict(DEFAULT_ENDPOINTS)
        path = ROOT_DIR / "ram_endpoints.yaml"
        if path.exists():
            self.endpoints.update(yaml.safe_load(path.read_text()) or {})
        self._token: str | None = None
        self._token_expires: datetime | None = None

    async def _access_token(self) -> str:
        if self._token and self._token_expires and datetime.utcnow() < self._token_expires:
            return self._token
        r = await self.http.post(
            self.s.ram_auth_url,
            auth=(self.s.ram_client_id, self.s.ram_api_key),
            data={"grant_type": "password", "username": self.s.ram_username, "password": self.s.ram_password},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        self._token = data["access_token"]
        self._token_expires = datetime.utcnow() + timedelta(seconds=max(data.get("expires_in", 3600) - 60, 30))
        return self._token

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        token = await self._access_token()
        r = await self.http.get(self.s.ram_api_base_url.rstrip("/") + path, params=params,
                                headers={"Authorization": f"bearer {token}"}, timeout=30)
        r.raise_for_status()
        return r.json()

    async def _vehicles_raw(self) -> list[dict[str, Any]]:
        data = await self._get(self.endpoints["vehicles"])
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("content", "items", "data", "results", "value"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    async def vehicles(self) -> list[dict[str, Any]]:
        return [_vehicle_row(v) for v in await self._vehicles_raw()]

    async def positions(self) -> list[dict[str, Any]]:
        rows = []
        for v in await self._vehicles_raw():
            row = _vehicle_row(v)
            if row["lat"] is None or row["lng"] is None:
                continue
            rows.append({**row, "vehicle_id": row["id"], "speed_mph": 15 if row["moving"] else 0, "address": None})
        return rows

    async def journeys(self, vehicle_id: str, day: date) -> list[dict[str, Any]]:
        start = datetime.combine(day, time(0, 0))
        end = start + timedelta(days=1)
        path = self.endpoints["history"].format(id=vehicle_id, **{"from": quote(start.isoformat(), safe=""),
                                                                   "to": quote(end.isoformat(), safe="")})
        data = await self._get(path)
        events = data.get("history") if isinstance(data, dict) else data
        events = sorted(events or [], key=lambda e: str(e.get("event_date")))
        legs: list[dict[str, Any]] = []
        leg_start: dict[str, Any] | None = None
        for e in events:
            name = e.get("event_name")
            if name == "TRANSIT_START":
                leg_start = e
            elif name == "TRANSIT_STOP" and leg_start:
                legs.append({
                    "vehicle_id": vehicle_id,
                    "start_time": leg_start.get("event_date"), "end_time": e.get("event_date"),
                    "start_lat": leg_start.get("latitude"), "start_lng": leg_start.get("longitude"),
                    "end_lat": e.get("latitude"), "end_lng": e.get("longitude"),
                    "start_address": leg_start.get("formattedAddress"), "end_address": e.get("formattedAddress"),
                    # RAM's history odometer reading has no documented unit, so a distance figure here
                    # would be a guess - leave it unset rather than risk a wrong number.
                    "distance_miles": None,
                })
                leg_start = None
        return legs

    async def check(self) -> str:
        return f"RAM Tracking: {len(await self.vehicles())} vehicles"


class DemoRamTracking:
    """Plausible van journeys built from the demo FSM jobs."""

    demo = True

    def __init__(self, fsm):
        self.fsm = fsm

    async def vehicles(self) -> list[dict[str, Any]]:
        return [{"id": f"V{n}", "registration": f"YD{71 + n} SFS", "driver": s["name"]}
                for n, s in enumerate(await self.fsm.staff())]

    async def positions(self) -> list[dict[str, Any]]:
        out = []
        for p in await self.fsm.locations():
            out.append({"vehicle_id": p["vehicle"], "registration": p["vehicle"], "driver": p["engineer"], "lat": p["lat"],
                        "lng": p["lng"], "timestamp": p["timestamp"], "speed_mph": p["speed_mph"],
                        "ignition": p["status"] != "parked", "address": None})
        return out

    async def journeys(self, vehicle_id: str, day: date) -> list[dict[str, Any]]:
        from .fsm import demo_coords

        vehicles = {v["id"]: v for v in await self.vehicles()}
        driver = vehicles.get(vehicle_id, {}).get("driver")
        if not driver or day.weekday() >= 5:
            return []
        rng = random.Random(f"{driver}{day}")
        site_coords = [(s["lat"], s["lng"]) for s in await self.fsm.sites()]

        def pick_home() -> tuple[float, float]:
            # Stay clear of every demo site: van_day snaps a leg to the nearest site within 400m, so a "home"
            # that happens to land that close to a real site would wrongly show up as that site instead of "Home".
            for _ in range(20):
                candidate = (53.80 + rng.uniform(-0.05, 0.05), -1.80 + rng.uniform(-0.08, 0.08))
                if all(abs(candidate[0] - lat) > 0.006 or abs(candidate[1] - lng) > 0.009 for lat, lng in site_coords):
                    return candidate
            return candidate

        home = pick_home()
        jobs = sorted((j for j in await self.fsm.jobs(day, day, engineer=driver) if j.get("started_at")),
                      key=lambda j: j["started_at"])
        if not jobs:
            return []
        legs, here, t = [], home, None
        for jb in jobs:
            arrive = datetime.fromisoformat(jb["started_at"]) - timedelta(minutes=rng.randint(2, 8))
            depart = arrive - timedelta(minutes=rng.randint(15, 40))
            if t and depart < t:
                depart = t + timedelta(minutes=5)
            site = demo_coords(jb["site"])
            legs.append({"vehicle_id": vehicle_id, "driver": driver, "start_time": depart.isoformat(),
                         "end_time": arrive.isoformat(), "start_lat": here[0], "start_lng": here[1], "end_lat": site[0],
                         "end_lng": site[1], "start_address": "Home" if here == home else None,
                         "end_address": jb["site"], "distance_miles": round(rng.uniform(3, 16), 1)})
            here = site
            t = datetime.fromisoformat(jb["completed_at"]) if jb.get("completed_at") else arrive + timedelta(hours=2)
        back = t + timedelta(minutes=rng.randint(3, 15))
        legs.append({"vehicle_id": vehicle_id, "driver": driver, "start_time": back.isoformat(),
                     "end_time": (back + timedelta(minutes=rng.randint(15, 45))).isoformat(), "start_lat": here[0],
                     "start_lng": here[1], "end_lat": home[0], "end_lng": home[1], "start_address": None,
                     "end_address": "Home", "distance_miles": round(rng.uniform(3, 16), 1)})
        return legs

    async def check(self) -> str:
        return "demo RAM Tracking"
