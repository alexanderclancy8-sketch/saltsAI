"""RAM Tracking (vehicle trackers) connector: live positions and journey history.

RAM provides an External API for customers (API key from RAM; endpoint list in their
Swagger docs). Paths and the auth header are configurable in ram_endpoints.yaml so they
can be matched to RAM's Swagger without code changes. Field names are normalised
through alias lists, like the Salts FSM connector.
"""

from __future__ import annotations

import logging
import random
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx
import yaml

from ..config import ROOT_DIR, Settings

log = logging.getLogger(__name__)

DEFAULT_ENDPOINTS = {
    "vehicles": "/vehicles",
    "positions": "/vehicles/positions",  # latest position per vehicle
    "journeys": "/journeys",  # ?vehicleId=&from=&to=
}

ALIASES = {
    "vehicle": {
        "id": ("id", "vehicleId", "vehicle_id", "assetId"),
        "registration": ("registration", "reg", "registrationNumber", "vrn", "name"),
        "driver": ("driver", "driverName", "driver_name", "assignedDriver"),
    },
    "position": {
        "vehicle_id": ("vehicleId", "vehicle_id", "assetId", "id"),
        "registration": ("registration", "reg", "vrn", "vehicleName"),
        "driver": ("driver", "driverName"),
        "lat": ("lat", "latitude"),
        "lng": ("lng", "lon", "longitude"),
        "timestamp": ("timestamp", "time", "gpsTime", "dateTime", "lastUpdate"),
        "speed_mph": ("speed", "speedMph", "speed_mph"),
        "ignition": ("ignition", "ignitionOn", "status"),
        "address": ("address", "location", "formattedAddress"),
    },
    "journey": {
        "vehicle_id": ("vehicleId", "vehicle_id", "assetId"),
        "driver": ("driver", "driverName"),
        "start_time": ("startTime", "start_time", "start", "startDateTime"),
        "end_time": ("endTime", "end_time", "end", "endDateTime"),
        "start_lat": ("startLat", "start_lat", "startLatitude"),
        "start_lng": ("startLng", "startLon", "start_lng", "startLongitude"),
        "end_lat": ("endLat", "end_lat", "endLatitude"),
        "end_lng": ("endLng", "endLon", "end_lng", "endLongitude"),
        "start_address": ("startAddress", "start_address", "startLocation"),
        "end_address": ("endAddress", "end_address", "endLocation"),
        "distance_miles": ("distanceMiles", "distance_miles", "distance", "mileage"),
    },
}


def _pick(d: dict[str, Any], names: tuple[str, ...]) -> Any:
    for n in names:
        v = d.get(n)
        if v not in (None, ""):
            return v.get("name") if isinstance(v, dict) and "name" in v else v
    return None


def _norm(kind: str, raw: dict[str, Any]) -> dict[str, Any]:
    return {k: _pick(raw, names) for k, names in ALIASES[kind].items()}


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data", "results", "vehicles", "journeys", "positions", "value"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


class RamTracking:
    demo = False

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.s = settings
        self.http = http
        self.endpoints = dict(DEFAULT_ENDPOINTS)
        path = ROOT_DIR / "ram_endpoints.yaml"
        if path.exists():
            self.endpoints.update(yaml.safe_load(path.read_text()) or {})

    def _headers(self) -> dict[str, str]:
        h = self.s.ram_api_key_header
        return {"Authorization": f"Bearer {self.s.ram_api_key}"} if h.lower() == "authorization" else {h: self.s.ram_api_key}

    async def _get(self, key: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        r = await self.http.get(self.s.ram_api_base_url.rstrip("/") + self.endpoints[key], params=params,
                                headers=self._headers(), timeout=30)
        r.raise_for_status()
        return _rows(r.json())

    async def vehicles(self) -> list[dict[str, Any]]:
        return [_norm("vehicle", v) for v in await self._get("vehicles")]

    async def positions(self) -> list[dict[str, Any]]:
        return [_norm("position", p) for p in await self._get("positions")]

    async def journeys(self, vehicle_id: str, day: date) -> list[dict[str, Any]]:
        start = datetime.combine(day, time(0, 0))
        rows = await self._get("journeys", {"vehicleId": vehicle_id, "from": start.isoformat(),
                                            "to": (start + timedelta(days=1)).isoformat()})
        return sorted((_norm("journey", j) for j in rows), key=lambda j: str(j.get("start_time")))

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
        home = (53.80 + rng.uniform(-0.05, 0.05), -1.80 + rng.uniform(-0.08, 0.08))
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
