"""Salts FSM (field service management) connector.

Salts FSM is the company's own web app on Azure App Service. Jarvis talks to its
REST API (read-only) using the paths in ``fsm_endpoints.yaml``. Field names are
normalised through alias lists so small differences in the API's JSON shape
don't break staff monitoring or compliance checks.

If FSM_BASE_URL is not set, a deterministic demo dataset is used instead.
"""

from __future__ import annotations

import logging
import random
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx
import yaml

from ..config import Settings

log = logging.getLogger(__name__)

DEFAULT_ENDPOINTS = {
    "jobs": "/jobs",
    "staff": "/engineers",
    "systems": "/systems",
    "contracts": "/contracts",
    "quotes": "/quotes",
    "sites": "/sites",
    "customers": "/customers",
    "timesheets": "/timesheets",
    "locations": "/tracking/locations",
    "stock": "/stock",
    "stock_movements": "/stock/movements",
    "health": "/health",
}

ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "job": {
        "id": ("id", "jobId", "job_id"),
        "ref": ("ref", "reference", "jobNumber", "job_number", "number"),
        "type": ("type", "jobType", "job_type", "category"),
        "status": ("status", "state", "jobStatus"),
        "customer": ("customer", "customerName", "customer_name", "client"),
        "site": ("site", "siteName", "site_name", "address", "location"),
        "engineer": ("engineer", "engineerName", "engineer_name", "assignedTo", "assigned_to", "technician"),
        "scheduled_start": ("scheduled_start", "scheduledStart", "start", "startTime", "date", "scheduledDate"),
        "scheduled_end": ("scheduled_end", "scheduledEnd", "end", "endTime"),
        "started_at": ("started_at", "startedAt", "actualStart", "arrived_at", "arrivedAt"),
        "completed_at": ("completed_at", "completedAt", "actualEnd", "finishedAt"),
        "value": ("value", "price", "total", "amount", "invoiceValue"),
        "hours": ("hours", "labourHours", "labour_hours", "duration_hours"),
        "priority": ("priority", "slaPriority", "sla"),
        "created_by": ("created_by", "createdBy", "bookedBy", "booked_by", "owner", "author"),
        "created_at": ("created_at", "createdAt", "created", "bookedAt"),
        "invoice_ref": ("invoice_ref", "invoiceRef", "invoiceNumber", "invoice_number", "invoiceNo", "invoiced"),
        "checkin_lat": ("checkin_lat", "checkInLat", "startLat", "start_lat"),
        "checkin_lng": ("checkin_lng", "checkInLng", "startLng", "start_lng"),
    },
    "staff": {
        "id": ("id", "engineerId", "userId", "staff_id"),
        "name": ("name", "fullName", "full_name", "displayName"),
        "role": ("role", "jobTitle", "job_title", "position"),
        "status": ("status", "availability", "state"),
        "current_job": ("current_job", "currentJob", "currentJobRef"),
        "certifications": ("certifications", "qualifications", "certs", "training"),
        "hours_this_week": ("hours_this_week", "hoursThisWeek", "weekHours"),
    },
    "system": {
        "id": ("id", "systemId", "system_id"),
        "site": ("site", "siteName", "site_name"),
        "customer": ("customer", "customerName", "customer_name"),
        "type": ("type", "systemType", "system_type", "category"),
        "make_model": ("make_model", "panel", "makeModel", "model", "manufacturer"),
        "service_frequency_months": ("service_frequency_months", "serviceFrequencyMonths", "frequency_months",
                                     "serviceInterval"),
        "last_service": ("last_service", "lastService", "lastServiceDate", "last_service_date"),
        "next_service_due": ("next_service_due", "nextServiceDue", "nextServiceDate", "next_service_date", "due"),
        "contract_id": ("contract_id", "contractId", "contract"),
    },
    "contract": {
        "id": ("id", "contractId", "contract_id", "reference"),
        "customer": ("customer", "customerName", "customer_name"),
        "site": ("site", "siteName", "site_name"),
        "renewal_date": ("renewal_date", "renewalDate", "renewal", "endDate", "end_date"),
        "annual_value": ("annual_value", "annualValue", "value", "price"),
        "visits_per_year": ("visits_per_year", "visitsPerYear", "visits"),
        "status": ("status", "state"),
        "systems": ("systems", "systemCount", "system_count"),
    },
    "quote": {
        "id": ("id", "quoteId", "quote_id", "reference", "number"),
        "title": ("title", "description", "summary"),
        "customer": ("customer", "customerName", "customer_name"),
        "site": ("site", "siteName"),
        "value": ("value", "total", "amount", "price"),
        "status": ("status", "state"),
        "sent_date": ("sent_date", "sentDate", "date", "created", "createdAt"),
        "created_by": ("created_by", "createdBy", "owner", "author", "salesperson", "preparedBy"),
        "type": ("type", "quoteType", "quote_type", "category", "source"),
        "source_job": ("source_job", "sourceJob", "jobRef", "job_ref", "fromJob", "job"),
    },
    "location": {
        "engineer": ("engineer", "engineerName", "name", "driver", "user"),
        "vehicle": ("vehicle", "registration", "reg", "van"),
        "lat": ("lat", "latitude"),
        "lng": ("lng", "lon", "long", "longitude"),
        "timestamp": ("timestamp", "time", "recordedAt", "recorded_at", "lastSeen", "last_seen"),
        "speed_mph": ("speed_mph", "speed", "speedMph"),
        "status": ("status", "state", "ignition"),
    },
    "site": {
        "id": ("id", "siteId", "site_id"),
        "name": ("name", "siteName", "site_name", "title"),
        "customer": ("customer", "customerName"),
        "address": ("address", "fullAddress", "address1"),
        "postcode": ("postcode", "postCode", "zip"),
        "lat": ("lat", "latitude"),
        "lng": ("lng", "lon", "long", "longitude"),
    },
    "stock": {
        "sku": ("sku", "code", "partNumber", "part_number", "itemCode", "productCode"),
        "name": ("name", "description", "itemName", "product"),
        "category": ("category", "group", "type"),
        "location": ("location", "locationName", "store", "van", "warehouse"),
        "qty": ("qty", "quantity", "onHand", "on_hand", "stock", "level"),
        "unit_cost": ("unit_cost", "unitCost", "cost", "costPrice", "buyPrice"),
        "reorder_level": ("reorder_level", "reorderLevel", "minLevel", "min", "minimum"),
        "reorder_qty": ("reorder_qty", "reorderQty", "reorderQuantity"),
        "supplier": ("supplier", "supplierName", "vendor"),
    },
    "stock_move": {
        "sku": ("sku", "code", "partNumber", "itemCode"),
        "qty": ("qty", "quantity"),
        "kind": ("kind", "type", "movementType"),
        "job_ref": ("job_ref", "job", "jobRef", "jobNumber"),
        "date": ("date", "created_at", "createdAt", "timestamp"),
        "location": ("location", "from", "fromLocation"),
    },
    "timesheet": {
        "engineer": ("engineer", "engineerName", "name", "staff"),
        "date": ("date", "day"),
        "hours": ("hours", "total_hours", "totalHours"),
    },
}


def _pick(d: dict[str, Any], names: tuple[str, ...]) -> Any:
    for n in names:
        if n in d and d[n] not in (None, ""):
            v = d[n]
            if isinstance(v, dict):  # e.g. {"name": "..."} for customer/site/engineer
                return v.get("name") or v.get("displayName") or v.get("title") or v
            return v
    return None


def normalise(kind: str, raw: dict[str, Any]) -> dict[str, Any]:
    out = {field: _pick(raw, names) for field, names in ALIASES[kind].items()}
    extra = {k: v for k, v in raw.items() if not any(k in names for names in ALIASES[kind].values())}
    if extra:
        out["extra"] = extra
    return out


def _unwrap(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data", "results", "value", "records"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


class FSMClient:
    demo = False

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.s = settings
        self.http = http
        self.endpoints = dict(DEFAULT_ENDPOINTS)
        if settings.fsm_endpoints_file.exists():
            self.endpoints.update(yaml.safe_load(settings.fsm_endpoints_file.read_text()) or {})

    def _headers(self) -> dict[str, str]:
        if not self.s.fsm_api_key:
            return {}
        if self.s.fsm_api_key_header.lower() == "authorization":
            return {"Authorization": f"Bearer {self.s.fsm_api_key}"}
        return {self.s.fsm_api_key_header: self.s.fsm_api_key}

    def _url(self, path: str) -> str:
        base = self.s.fsm_base_url.rstrip("/")
        prefix = self.s.fsm_api_prefix.rstrip("/")
        return f"{base}{prefix}{path if path.startswith('/') else '/' + path}"

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Read-only GET against the FSM API. Used by Jarvis to explore any endpoint."""
        if ".." in path or "://" in path:
            raise ValueError("path must be a relative API path")
        r = await self.http.get(self._url(path), params=params, headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    async def write(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Create/update records in Salts FSM. Only ever called after the owner approves the action."""
        if method.upper() not in ("POST", "PUT", "PATCH") or ".." in path or "://" in path:
            raise ValueError("Only POST/PUT/PATCH to a relative API path is allowed")
        r = await self.http.request(method.upper(), self._url(path), json=body, headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json() if r.content else {"status": r.status_code}

    async def _list(self, kind_key: str, kind: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        payload = await self.get(self.endpoints[kind_key], params)
        return [normalise(kind, row) for row in _unwrap(payload)]

    async def jobs(self, date_from: date | None = None, date_to: date | None = None,
                   status: str | None = None, engineer: str | None = None) -> list[dict[str, Any]]:
        params = {k: v for k, v in {
            "from": date_from.isoformat() if date_from else None,
            "to": date_to.isoformat() if date_to else None,
            "status": status, "engineer": engineer}.items() if v}
        return await self._list("jobs", "job", params)

    async def staff(self) -> list[dict[str, Any]]:
        return await self._list("staff", "staff")

    async def systems(self) -> list[dict[str, Any]]:
        return await self._list("systems", "system")

    async def contracts(self) -> list[dict[str, Any]]:
        return await self._list("contracts", "contract")

    async def quotes(self, status: str | None = None) -> list[dict[str, Any]]:
        return await self._list("quotes", "quote", {"status": status} if status else None)

    async def timesheets(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        return await self._list("timesheets", "timesheet", {"from": date_from.isoformat(), "to": date_to.isoformat()})

    async def stock(self) -> list[dict[str, Any]]:
        return await self._list("stock", "stock")

    async def sites(self) -> list[dict[str, Any]]:
        return await self._list("sites", "site")

    async def locations(self, engineer: str | None = None, since: datetime | None = None) -> list[dict[str, Any]]:
        params = {k: v for k, v in {"engineer": engineer, "since": since.isoformat() if since else None}.items() if v}
        return await self._list("locations", "location", params or None)

    async def stock_movements(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        return await self._list("stock_movements", "stock_move", {"from": date_from.isoformat(), "to": date_to.isoformat()})

    async def record_stock_movement(self, movement: dict[str, Any]) -> Any:
        return await self.write("POST", self.endpoints["stock_movements"], movement)

    async def check(self) -> str:
        r = await self.http.get(self._url(self.endpoints["health"]), headers=self._headers(), timeout=15)
        r.raise_for_status()
        return f"FSM API healthy ({r.status_code})"


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------

_ENGINEERS = [
    ("E1", "Dan Harper", "Senior Fire Engineer"),
    ("E2", "Priya Shah", "Fire & Security Engineer"),
    ("E3", "Tom Wilkinson", "Security Engineer"),
    ("E4", "Megan Lowe", "Fire Engineer"),
    ("E5", "Kyle Brennan", "Apprentice Engineer"),
    ("E6", "Sam Oduya", "Commissioning Engineer"),
]
_OFFICE = [("Hannah Cole", "Office Manager / Scheduler"), ("Josh Pryce", "Sales & Estimating"),
           ("Rachel Gill", "Accounts Administrator")]
_SITE_COORDS = {
    "Northcliffe Primary School": (53.8295, -1.7780), "Aire Valley Care Home": (53.8440, -1.8370),
    "Riverside Mill Apartments": (53.8380, -1.7900), "Shipley Retail Park Unit 4": (53.8330, -1.7700),
    "Baildon Medical Centre": (53.8490, -1.7870), "Keighley Distribution Centre": (53.8680, -1.9110),
    "Saltaire Court Offices": (53.8380, -1.7900), "Otley Road Hotel": (53.8200, -1.7300),
    "Bingley Leisure Centre": (53.8480, -1.8380), "Ilkley Grammar Annexe": (53.9250, -1.8230),
}
_BASE = (53.8500, -1.7690)  # Baildon office


def demo_coords(site: str) -> tuple[float, float]:
    """Known demo sites have fixed coordinates; others get a stable spot around Bradford/Leeds."""
    if site in _SITE_COORDS:
        return _SITE_COORDS[site]
    h = sum(ord(c) * (i + 1) for i, c in enumerate(site))
    return 53.76 + (h % 1000) / 1000 * 0.16, -1.95 + (h // 1000 % 1000) / 1000 * 0.40
_SITES = [
    ("Northcliffe Primary School", "Bradford Council"), ("Aire Valley Care Home", "Aire Valley Care Ltd"),
    ("Riverside Mill Apartments", "Pennine Housing"), ("Shipley Retail Park Unit 4", "Kestrel Retail"),
    ("Baildon Medical Centre", "Baildon Health Partnership"), ("Keighley Distribution Centre", "Moorside Logistics"),
    ("Saltaire Court Offices", "Saltaire Estates"), ("Otley Road Hotel", "Wharfe Hospitality"),
    ("Bingley Leisure Centre", "Bradford Council"), ("Ilkley Grammar Annexe", "Wharfedale Academy Trust"),
]
_SYSTEM_TYPES = [("fire_alarm", "Gent Vigilon 4-loop", 6), ("fire_alarm", "Kentec Syncro AS", 6),
                 ("emergency_lighting", "Self-test EL system", 12), ("intruder", "Grade 2 intruder alarm", 6),
                 ("cctv", "8-camera NVR system", 12), ("access_control", "Paxton Net2 (6 doors)", 12)]


class DemoFSM:
    demo = True

    def __init__(self, today: date | None = None):
        self.today = today or date.today()
        rng = random.Random(self.today.toordinal())
        self._staff = []
        for i, (eid, name, role) in enumerate(_ENGINEERS):
            certs = [
                {"name": "FIA Fire Detection & Alarm - Maintenance", "expires": (self.today + timedelta(days=rng.randint(-5, 700))).isoformat()},
                {"name": "ECS / CSCS card", "expires": (self.today + timedelta(days=[21, 400, 90, 12, 300, 500][i])).isoformat()},
                {"name": "IPAF (MEWP)", "expires": (self.today + timedelta(days=rng.randint(40, 900))).isoformat()},
            ]
            self._staff.append({"id": eid, "name": name, "role": role, "status": "on_job", "current_job": None,
                                "certifications": certs, "hours_this_week": 0})

        start = datetime.combine(self.today, time(8, 0))
        job_types = ["service", "service", "callout", "remedial", "install", "service", "survey"]
        self._jobs = []
        for n in range(11):
            eng = _ENGINEERS[n % len(_ENGINEERS)]
            site, customer = _SITES[n % len(_SITES)]
            sched = start + timedelta(hours=(n // len(_ENGINEERS)) * 4 + rng.choice([0, 0, 1]))
            now = datetime.now()
            status = "scheduled"
            started = completed = None
            if sched + timedelta(hours=3) < now:
                status, started, completed = "completed", sched + timedelta(minutes=10), sched + timedelta(hours=3)
            elif sched < now:
                status, started = "in_progress", sched + timedelta(minutes=rng.choice([5, 15, 70]))
            self._jobs.append({
                "id": f"J{24100 + n}", "ref": f"J{24100 + n}", "type": job_types[n % len(job_types)],
                "status": status, "customer": customer, "site": site, "engineer": eng[1],
                "scheduled_start": sched.isoformat(), "scheduled_end": (sched + timedelta(hours=3)).isoformat(),
                "started_at": started.isoformat() if started else None,
                "completed_at": completed.isoformat() if completed else None,
                "value": rng.choice([185, 240, 320, 450, 1250, 2890]), "hours": 3, "priority": rng.choice(["4h", "24h", "PPM"]),
            })
        # an overdue callout left from yesterday
        self._jobs.append({
            "id": "J24099", "ref": "J24099", "type": "callout", "status": "scheduled", "customer": "Aire Valley Care Ltd",
            "site": "Aire Valley Care Home", "engineer": None,
            "scheduled_start": datetime.combine(self.today - timedelta(days=1), time(14, 0)).isoformat(),
            "scheduled_end": None, "started_at": None, "completed_at": None, "value": 185, "hours": 2, "priority": "4h",
        })
        # 60 days of completed history so productivity reports have something to chew on
        self._timesheets = []
        pace = {"Dan Harper": 1.15, "Priya Shah": 1.05, "Tom Wilkinson": 0.9, "Megan Lowe": 1.0,
                "Kyle Brennan": 0.75, "Sam Oduya": 0.95}
        streets = ["Manningham Lane", "Leeds Road", "Otley Road", "Bingley Road", "Harrogate Road", "Kirkgate",
                   "Wakefield Road", "Keighley Road", "Halifax Road", "Canal Road", "Thornton Road", "Allerton Road"]
        kinds = ["Primary School", "Care Home", "Offices", "Surgery", "Warehouse", "Flats", "Hotel", "Church Hall"]
        for back in range(1, 61):
            day = self.today - timedelta(days=back)
            if day.weekday() >= 5:
                continue
            for eid, name, _ in _ENGINEERS:
                if rng.random() < 0.05:  # holiday / sick day
                    continue
                n_jobs = max(1, round(rng.gauss(2.7 * pace[name], 0.5)))
                t = datetime.combine(day, time(8, 0))
                first_start = last_end = None
                for _ in range(n_jobs):
                    if rng.random() < 0.12:
                        site, customer = rng.choice(_SITES)
                    else:
                        site = f"{rng.randint(2, 180)} {rng.choice(streets)} {rng.choice(kinds)}"
                        customer = rng.choice([c for _, c in _SITES])
                    late = rng.random() < (0.3 if name == "Kyle Brennan" else 0.08)
                    started = t + timedelta(minutes=rng.randint(35, 70) if late else rng.randint(0, 15))
                    dur = timedelta(minutes=int(rng.uniform(100, 150)))
                    jtype = rng.choice(["service", "service", "service", "callout", "remedial", "install"])
                    self._jobs.append({
                        "id": f"H{len(self._jobs)}", "ref": f"J{23000 + len(self._jobs)}", "type": jtype,
                        "status": "completed", "customer": customer, "site": site, "engineer": name,
                        "scheduled_start": t.isoformat(), "scheduled_end": (t + timedelta(hours=3)).isoformat(),
                        "started_at": started.isoformat(), "completed_at": (started + dur).isoformat(),
                        "value": rng.choice([185, 240, 320, 450, 760, 1250]), "hours": round(dur.total_seconds() / 3600, 1),
                        "priority": "PPM" if jtype == "service" else "24h",
                        # nearly everything is invoiced; a few recent jobs slipped through (demo story)
                        "invoice_ref": None if back <= 6 and rng.random() < 0.15 else f"INV-{30000 + len(self._jobs)}"})
                    first_start = first_start or started
                    last_end = started + dur
                    t = last_end + timedelta(minutes=rng.randint(25, 45))
                hours = (last_end - first_start).total_seconds() / 3600 + 0.9 + rng.uniform(-0.2, 0.2)
                if name == "Tom Wilkinson" and rng.random() < 0.45:
                    hours += rng.uniform(1.0, 1.8)  # demo story: timesheets that don't match the tracker
                self._timesheets.append({"engineer": name, "date": day.isoformat(), "hours": round(hours, 1)})
        week_start = self.today - timedelta(days=self.today.weekday())
        for s in self._staff:
            current = next((j for j in self._jobs if j["engineer"] == s["name"] and j["status"] == "in_progress"), None)
            s["current_job"] = current["ref"] if current else None
            s["status"] = "on_job" if current else "available"
            s["hours_this_week"] = round(sum(t["hours"] for t in self._timesheets
                                             if t["engineer"] == s["name"] and t["date"] >= week_start.isoformat()), 1)

        self._systems, self._contracts = [], []
        for n, (site, customer) in enumerate(_SITES):
            for k in range(1 + n % 3):
                stype, model, freq = _SYSTEM_TYPES[(n + k) % len(_SYSTEM_TYPES)]
                last = self.today - timedelta(days=rng.randint(20, 230))
                self._systems.append({
                    "id": f"S{500 + len(self._systems)}", "site": site, "customer": customer, "type": stype,
                    "make_model": model, "service_frequency_months": freq, "last_service": last.isoformat(),
                    "next_service_due": (last + timedelta(days=int(freq * 30.4))).isoformat(), "contract_id": f"C{300 + n}",
                })
            self._contracts.append({
                "id": f"C{300 + n}", "customer": customer, "site": site,
                "renewal_date": (self.today + timedelta(days=rng.randint(-10, 330))).isoformat(),
                "annual_value": rng.choice([380, 520, 760, 1100, 1650, 2400]), "visits_per_year": 2,
                "status": "active", "systems": 1 + n % 3,
            })
        self._quotes = [
            {"id": "Q1180", "title": "Vigilon panel upgrade, block B", "customer": "Wharfedale Academy Trust",
             "site": "Ilkley Grammar Annexe", "value": 14850, "status": "sent", "sent_date": (self.today - timedelta(days=12)).isoformat()},
            {"id": "Q1184", "title": "Replace 12 failed EL fittings", "customer": "Pennine Housing",
             "site": "Riverside Mill Apartments", "value": 2160, "status": "sent", "sent_date": (self.today - timedelta(days=4)).isoformat()},
            {"id": "Q1175", "title": "CCTV extension - yard cameras", "customer": "Moorside Logistics",
             "site": "Keighley Distribution Centre", "value": 6420, "status": "accepted", "sent_date": (self.today - timedelta(days=20)).isoformat()},
            {"id": "Q1169", "title": "Intruder alarm upgrade to Grade 3", "customer": "Kestrel Retail",
             "site": "Shipley Retail Park Unit 4", "value": 3380, "status": "declined", "sent_date": (self.today - timedelta(days=31)).isoformat()},
        ]

        # office staff activity: who raised quotes and booked jobs
        office = [n for n, _ in _OFFICE]
        for q in self._quotes:
            q["created_by"] = office[len(q["id"]) % 2]
        titles = ["Annual service renewal", "Replace failed detectors", "Emergency lighting upgrade", "Additional CCTV camera",
                  "Access control door add", "Panel battery replacement", "New fire alarm install", "Intruder upgrade"]
        for back in range(1, 61):
            day = self.today - timedelta(days=back)
            if day.weekday() >= 5:
                continue
            for name, rate in (("Hannah Cole", 2.2), ("Josh Pryce", 3.4)):
                for _ in range(max(0, int(rng.gauss(rate, 1)))):
                    site, customer = rng.choice(_SITES)
                    self._quotes.append({
                        "id": f"Q{900 + len(self._quotes)}", "title": rng.choice(titles), "customer": customer,
                        "site": site, "value": rng.choice([260, 480, 950, 1850, 3200, 7400]),
                        "status": rng.choices(["accepted", "sent", "declined"], [0.45 if name == "Josh Pryce" else 0.3, 0.4, 0.25])[0],
                        "sent_date": day.isoformat(), "created_by": name})
        for jb in self._jobs:
            jb["created_by"] = rng.choice(["Hannah Cole", "Hannah Cole", "Rachel Gill"])

        # remedial quotes raised from service visits (Salts FSM creates these from the job sheet)
        defects = ["Replace 3 failed smoke detectors", "Replace standby batteries (2 x 12V 7Ah)",
                   "Replace faulty sounder on zone 4", "Emergency lighting: 6 fittings failed duration test",
                   "Replace damaged call point glass/element", "Add detector to new partitioned office"]
        for n in range(9):
            site, customer = _SITES[(n * 3) % len(_SITES)]
            sent = self.today - timedelta(days=[2, 5, 9, 12, 16, 21, 30, 38, 45][n])
            self._quotes.append({"id": f"RQ{700 + n}", "title": defects[n % len(defects)], "customer": customer,
                                 "site": site, "value": [145, 210, 260, 320, 395, 480, 185, 540, 230][n],
                                 "status": ["sent", "sent", "sent", "accepted", "sent", "declined", "sent", "accepted", "sent"][n],
                                 "sent_date": sent.isoformat(), "created_by": "Hannah Cole", "type": "remedial",
                                 "source_job": f"J{23100 + n * 7}"})

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        key = path.strip("/").split("/")[0]
        mapping = {"jobs": self._jobs, "engineers": self._staff, "staff": self._staff, "systems": self._systems,
                   "contracts": self._contracts, "quotes": self._quotes, "timesheets": self._timesheets}
        if key not in mapping:
            raise ValueError(f"Demo FSM has no endpoint '{path}'. Available: {', '.join(mapping)}")
        return mapping[key]

    async def sites(self) -> list[dict[str, Any]]:
        return [{"id": f"SITE{n}", "name": name, "customer": cust, "lat": _SITE_COORDS[name][0],
                 "lng": _SITE_COORDS[name][1]} for n, (name, cust) in enumerate(_SITES)]

    async def locations(self, engineer: str | None = None, since: datetime | None = None) -> list[dict[str, Any]]:
        rng = random.Random(int(datetime.now().timestamp() // 300))  # positions drift every 5 minutes
        out = []
        for eid, name, _ in _ENGINEERS:
            if engineer and engineer.lower() not in name.lower():
                continue
            job = next((j for j in self._jobs if j["engineer"] == name and j["status"] == "in_progress"), None)
            lat, lng = _SITE_COORDS[job["site"]] if job else _BASE
            moving = job is None and rng.random() < 0.5
            out.append({"engineer": name, "vehicle": f"YD{70 + int(eid[1:])} SFS", "lat": lat + rng.uniform(-0.004, 0.004),
                        "lng": lng + rng.uniform(-0.006, 0.006),
                        "timestamp": (datetime.now() - timedelta(minutes=rng.randint(0, 6))).isoformat(timespec="seconds"),
                        "speed_mph": rng.randint(18, 42) if moving else 0, "status": "driving" if moving else "parked"})
        return out

    async def write(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        log.info("[DEMO] FSM %s %s %s", method, path, body)
        return {"demo": True, "status": "not applied - demo FSM"}

    async def jobs(self, date_from: date | None = None, date_to: date | None = None,
                   status: str | None = None, engineer: str | None = None) -> list[dict[str, Any]]:
        out = []
        for j in self._jobs:
            d = datetime.fromisoformat(j["scheduled_start"]).date()
            if date_from and d < date_from or date_to and d > date_to:
                continue
            if status and j["status"] != status:
                continue
            if engineer and (j["engineer"] or "").lower() != engineer.lower():
                continue
            out.append(dict(j))
        return out

    async def staff(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self._staff]

    async def systems(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self._systems]

    async def contracts(self) -> list[dict[str, Any]]:
        return [dict(c) for c in self._contracts]

    async def quotes(self, status: str | None = None) -> list[dict[str, Any]]:
        return [dict(q) for q in self._quotes if not status or q["status"] == status]

    async def timesheets(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        return [dict(t) for t in self._timesheets if date_from.isoformat() <= t["date"] <= date_to.isoformat()]

    async def check(self) -> str:
        return "demo FSM"
