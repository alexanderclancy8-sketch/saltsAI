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

Everything is read defensively against RAM's published schema (https://api.qaifn.co.uk/v2/api-docs): for one,
``vehicle_status.last_event`` is a plain string, and a field of an unexpected type is treated as blank instead of
crashing the call. RAM allows 3 requests a minute per endpoint per token, so all callers share one cache and one
budget (see ``RamTracking._call``) and a 429 is reported as "rate limited", never as "not connected". Every failure is
a ``RamError`` whose message names the step that failed (sign-in, address, permissions) and carries no secret; the
console shows it in the Fleet pop-up and the Connections test.
"""

from __future__ import annotations

import logging
import random
import re
import time as _time
from collections import deque
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import yaml

from .. import demo_guard
from ..config import ROOT_DIR, Settings
from ..redact import redact_text

log = logging.getLogger(__name__)

DEFAULT_ENDPOINTS = {
    "vehicles": "/api/v1/vehicle/for-account",
    "history": "/api/v1/history/{id}/{from}/{to}",  # {from}/{to} are ISO8601, URL-encoded
}
STANDARD_API = "https://api.qaifn.co.uk"
STANDARD_AUTH = "https://auth.qaifn.co.uk/oauth/token"
# The four things RAM needs, as (settings key, what the Connections form calls it): used to say exactly which is missing.
CREDENTIALS = (("ram_client_id", "Client ID"), ("ram_api_key", "Client secret"), ("ram_username", "API username"),
               ("ram_password", "API password"))
PROBE_OK_S, PROBE_FAIL_S = 600, 120  # how long a health check is trusted: a working link 10 minutes, a failing one 2
# RAM allows 3 requests per minute per endpoint per bearer token. Jarvis stays under that by sharing one cache and one
# budget between everything that wants RAM data (see RamTracking._call).
RATE_LIMIT, RATE_WINDOW_S = 3, 60
VEHICLES_TTL_S = 60                                  # at most one vehicle-list request a minute
HISTORY_TTL_TODAY_S, HISTORY_TTL_PAST_S = 120, 3600  # a day's journeys: today's still change, a past day's never do
CACHE_MAX = 200
RATE_LIMITED = ("RAM Tracking is rate limited just now (RAM allows 3 requests a minute for each kind of request), so "
                "this is not a connection fault. Retry in a minute.")


def missing_credentials(settings: Settings) -> list[str]:
    """What the owner still has to enter for RAM Tracking to connect, by the labels on the Connections form."""
    return [label for key, label in CREDENTIALS if not (getattr(settings, key, "") or "").strip()]


def origin_of(url: str) -> str:
    """``https://host`` of an address. RAM's endpoints are all absolute paths (``/api/v1/...``), so an "API address" that
    was saved with a path on it (``https://api.qaifn.co.uk/swagger/docs``, ``.../api/v1``) would turn every call into a
    404; only the scheme and host are ever used."""
    parts = urlsplit((url or "").strip())
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else (url or "").strip().rstrip("/")


class RamError(RuntimeError):
    """RAM Tracking could not be reached or refused us. The message is plain English, says which step failed and what to
    check, and carries no secret (it names hosts and paths, never a client secret, password or token)."""

    def __init__(self, message: str, stage: str = "", status: int | None = None, rate_limited: bool = False):
        super().__init__(message)
        self.stage, self.status, self.rate_limited = stage, status, rate_limited


def _retry_after(r: httpx.Response) -> float:
    """How long RAM asked us to wait after a 429 (the Retry-After header, in seconds), else a full rate window."""
    try:
        return min(max(float(r.headers.get("Retry-After", "")), 1.0), 300.0)
    except ValueError:
        return float(RATE_WINDOW_S)


def _obj(x: Any) -> dict[str, Any]:
    """``x`` if it is an object, else an empty one: RAM's JSON is read field by field and a field of an unexpected type
    (a string where an object was expected) must never take the whole call down."""
    return x if isinstance(x, dict) else {}


def _event_name(x: Any) -> str:
    """The name of a vehicle event. RAM's published VehicleStatusDTO has ``last_event`` as a plain STRING
    ("TRANSIT_START"); older/other shapes carry an object with the name inside, so both are read."""
    if isinstance(x, str):
        return x.strip().upper()
    if isinstance(x, dict):
        for key in ("event", "event_name", "name"):
            if isinstance(x.get(key), str):
                return x[key].strip().upper()
    return ""


def _vehicle_row(v: Any) -> dict[str, Any] | None:
    """One vehicle from /api/v1/vehicle/for-account (RAM's VehicleDTO), or None when the entry is not a vehicle at all."""
    if not isinstance(v, dict):
        return None
    status = _obj(v.get("vehicle_status"))
    loc = _obj(status.get("location"))
    driver = _obj(v.get("vehicle_driver"))
    return {
        "id": v.get("id"),
        "registration": v.get("registration"),
        "driver": driver.get("name"),
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "timestamp": status.get("event_date"),
        # RAM's vehicle status doesn't include a live speed figure - the last ignition/transit
        # event is the closest signal available for a driving-vs-parked guess.
        "moving": _event_name(status.get("last_event")) in ("TRANSIT_START", "OVER_SPEED"),
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
        self._now = _time.monotonic  # the clock the cache and the rate limiter use (a test can wind it forward)
        self._cache: dict[str, tuple[float, Any]] = {}          # request path -> (when stored, parsed answer)
        self._hits: dict[str, deque[float]] = {}                # endpoint -> when each recent request was sent
        self._blocked_until: dict[str, float] = {}              # endpoint -> RAM said 429: no requests before this
        # What the last real call said, so the console can show "not connected: why" instead of an empty Fleet. ok is None
        # until RAM has been asked; ``probe()`` fills it in and keeps it fresh without the Fleet pop-up being open.
        self.health: dict[str, Any] = {"ok": None, "detail": "", "at": 0.0, "rate_limited": False}

    # ------------------------------------------------------------------ health
    def _note(self, ok: bool, detail: str = "") -> None:
        self.health = {"ok": ok, "detail": detail, "at": self._now(), "rate_limited": False,
                       "when": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def _note_rate_limited(self) -> None:
        """RAM is busy, not broken: the connection is neither failing nor proven, so ``ok`` is left as it was."""
        self.health = {**self.health, "rate_limited": True, "at": self._now(),
                       "when": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    @property
    def signed_in(self) -> bool:
        return bool(self._token and self._token_expires and datetime.utcnow() < self._token_expires)

    @property
    def base_url(self) -> str:
        return origin_of(self.s.ram_api_base_url or STANDARD_API)

    @property
    def address_note(self) -> str:
        """Said alongside a test result when the saved API address had more than the host on it (a pasted endpoint)."""
        saved = (self.s.ram_api_base_url or "").strip()
        if saved and saved.rstrip("/") != self.base_url:
            return f"The saved API address had a path on it; only {self.base_url} is used."
        return ""

    @property
    def auth_url(self) -> str:
        url = (self.s.ram_auth_url or STANDARD_AUTH).strip()
        return url if urlsplit(url).path not in ("", "/") else origin_of(url) + "/oauth/token"

    async def probe(self, max_age: float | None = None) -> dict[str, Any]:
        """Ask RAM for the vehicle list (at most once per ``max_age`` seconds) and return ``self.health``. Never raises."""
        age = self._now() - self.health["at"]
        trusted = PROBE_OK_S if self.health["ok"] else PROBE_FAIL_S
        if self.health["ok"] is None or age >= (trusted if max_age is None else max_age):
            try:
                await self._vehicles_raw()
            except RamError:
                pass  # already recorded in self.health
            except Exception as e:  # noqa: BLE001
                self._note(False, f"RAM Tracking failed unexpectedly ({type(e).__name__}).")
        return self.health

    # ------------------------------------------------------------------ signing in
    # RAM's token endpoint is Spring Security OAuth2: HTTP Basic client ID/secret plus grant_type=password in a form body.
    # A wrong CLIENT id/secret is answered 401 {"error": "Unauthorized", "message": "Bad credentials"}; a client that was
    # accepted but a wrong API USER is answered 400 {"error": "invalid_grant", "error_description": "Bad credentials"} (or
    # invalid_scope / unsupported_grant_type). So the status and error code say which step failed, and the message says so
    # instead of a bare "HTTP 400". The token lasts about 8 hours and is reused until then.
    @staticmethod
    def _clean(value: str | None) -> str:
        return (value or "").strip()  # a stray space or newline from a paste is the commonest reason a right value fails

    def _token_request(self) -> dict[str, Any]:
        # httpx form-encodes the dict, so a password containing + & % = # or a space reaches RAM exactly as typed.
        return {"auth": (self._clean(self.s.ram_client_id), self._clean(self.s.ram_api_key)),
                "data": {"grant_type": "password", "username": self._clean(self.s.ram_username),
                         "password": self._clean(self.s.ram_password)}}

    @staticmethod
    def _oauth_error(r: httpx.Response) -> tuple[str, str]:
        """(error code, short description) from an OAuth2 error body, e.g. ("invalid_grant", "Bad credentials"). Both are
        RAM's own words, run through the redaction filter and cut short; neither is ever a secret we sent."""
        try:
            body = r.json()
        except ValueError:  # not OAuth JSON (a gateway or firewall page, say): show a little of what it said
            snippet = " ".join(re.sub(r"<[^>]+>", " ", r.text or "").split())
            return "", redact_text(snippet)[:100]
        if not isinstance(body, dict):
            return "", ""
        code = re.sub(r"[^A-Za-z_ ]", "", str(body.get("error") or "")).strip()[:40]
        text = " ".join(str(body.get("error_description") or body.get("message") or "").split())
        text = re.sub(r"\S+@\S+", "[email]", redact_text(text))[:120]
        return code, text

    def _sign_in_error(self, status: int, code: str, description: str) -> RamError:
        said = f"HTTP {status}" + (f", {code}" if code else "") + (f": {description}" if description else "")
        low = code.lower()
        if status == 401:
            why = ("RAM did not recognise the Client ID and Client secret. Copy both again from RAM's API Keys page "
                   "(the Client ID can contain a space; the API username and password are not what is wrong here).")
        elif low == "invalid_grant":
            why = ("RAM accepted the Client ID and Client secret but refused the API user's username or password. Check "
                   "the RAM username and password, and that the API user is switched on and has no two-step "
                   "verification (MFA).")
        elif low == "invalid_scope":
            why = ("RAM rejected the sign-in's scope (invalid_scope). Jarvis asks for none; RAM may now require one for "
                   "this client.")
        elif low == "unsupported_grant_type":
            why = "RAM's sign-in doesn't accept a username and password for this client (unsupported_grant_type)."
        elif status == 400:
            why = "Check the RAM username and password."
        else:
            why = "RAM's sign-in service returned an unexpected error."
        return RamError(f"RAM refused the sign-in ({said}). {why}{self._sent_note()}", "sign-in", status)

    def _sent_note(self) -> str:
        """What Jarvis actually sent, as lengths only (never the values), so the owner can see at a glance whether the saved
        details are the ones they meant: a password they know has 15 characters showing as 6 means the saved one is wrong."""
        size = {key: len(self._clean(getattr(self.s, key))) for key, _ in CREDENTIALS}
        return (f" Jarvis sent a {size['ram_username']}-character username, a {size['ram_password']}-character "
                f"password, a {size['ram_client_id']}-character Client ID and a {size['ram_api_key']}-character "
                "Client secret.")

    def _transport_error(self, e: Exception) -> RamError:
        host = urlsplit(self.auth_url)
        if isinstance(e, httpx.TimeoutException):
            return RamError(f"RAM's sign-in service ({host.netloc}) didn't answer in time.", "sign-in")
        if isinstance(e, httpx.HTTPError):
            return RamError(f"Couldn't reach RAM's sign-in service ({host.netloc}).", "sign-in")
        return RamError("RAM's sign-in answered, but not with a token Jarvis could read.", "sign-in")

    def _fail(self, err: RamError) -> RamError:
        log.warning("RAM Tracking sign-in failed: %s", err)
        self._note(False, str(err))
        return err

    async def _access_token(self) -> str:
        if self.signed_in:
            return self._token  # type: ignore[return-value]
        where = urlsplit(self.auth_url)
        try:
            r = await self.http.post(self.auth_url, timeout=30, **self._token_request())
        except httpx.HTTPError as e:
            raise self._fail(self._transport_error(e)) from None
        if r.status_code == 404:
            raise self._fail(RamError(f"RAM's sign-in address wasn't found (HTTP 404 at {where.netloc}{where.path}); "
                                      f"it should be {STANDARD_AUTH}.", "sign-in", 404))
        if r.status_code == 429:
            self._note_rate_limited()
            raise RamError(RATE_LIMITED, "sign-in", 429, rate_limited=True)
        if r.status_code >= 400:
            raise self._fail(self._sign_in_error(r.status_code, *self._oauth_error(r)))
        try:
            data = r.json()
            token = data["access_token"]
        except (ValueError, KeyError, TypeError):
            raise self._fail(self._transport_error(ValueError("no token"))) from None
        self._token = token
        self._token_expires = datetime.utcnow() + timedelta(seconds=max(data.get("expires_in", 3600) - 60, 30))
        return token

    # ------------------------------------------------------------------ data calls (cached, and kept under RAM's limit)
    # RAM allows 3 requests a minute per endpoint per token. Everything that wants RAM data shares one cache and one
    # budget, so the map, the chat tools, the routine test, the connection test and the scheduled jobs together can never
    # go over it: an answer younger than its time-to-live is reused, and with no budget left the last answer is served
    # (or, with none, a plain "rate limited, retry shortly") rather than another request being sent.
    def _budget_left(self, endpoint: str) -> bool:
        now = self._now()
        if now < self._blocked_until.get(endpoint, 0.0):
            return False
        hits = self._hits.setdefault(endpoint, deque())
        while hits and now - hits[0] >= RATE_WINDOW_S:
            hits.popleft()
        return len(hits) < RATE_LIMIT

    async def _call(self, endpoint: str, path: str, ttl: float) -> Any:
        now = self._now()
        cached = self._cache.get(path)
        if cached and now - cached[0] < ttl:
            return cached[1]
        if not self._budget_left(endpoint):
            self._note_rate_limited()
            if cached:
                return cached[1]  # the last answer is better than none, and it is at most a few minutes old
            raise RamError(RATE_LIMITED, "data", 429, rate_limited=True)
        try:
            data = await self._get(endpoint, path)
        except RamError as e:
            if e.rate_limited and cached:
                return cached[1]
            raise
        if len(self._cache) >= CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))
        self._cache[path] = (self._now(), data)
        return data

    async def _get(self, endpoint: str, path: str) -> Any:
        url = self.base_url + path
        for attempt in (1, 2):
            token = await self._access_token()
            self._hits.setdefault(endpoint, deque()).append(self._now())
            try:
                r = await self.http.get(url, headers={"Authorization": f"bearer {token}"}, timeout=30)
                r.raise_for_status()
                data = r.json()
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code == 401 and attempt == 1:
                    self._token = None  # the token ran out early or was revoked: sign in again, once
                    continue
                if code == 429:
                    pause = _retry_after(e.response)
                    self._blocked_until[endpoint] = self._now() + pause
                    self._note_rate_limited()
                    raise RamError(RATE_LIMITED, "data", 429, rate_limited=True) from None
                if code in (401, 403):
                    self._token = None
                    err = RamError(f"RAM signed us in but refused the request (HTTP {code}); the API account may not "
                                   "have access to vehicles.", "data", code)
                elif code == 404:
                    custom = self.base_url != STANDARD_API
                    err = RamError(
                        f"RAM's vehicle service wasn't found (HTTP 404 at {urlsplit(url).netloc}{path}). Sign-in worked, "
                        "so " + (f"the cause is the API address: {self.base_url} is not RAM's standard {STANDARD_API}. "
                                 "Clear the API address under Connections > RAM Tracking > Advanced to use the standard "
                                 "one." if custom else "the path RAM publishes for vehicles may have changed "
                                                       "(ram_endpoints.yaml)."), "data", code)
                else:
                    err = RamError(f"RAM's vehicle service returned an error (HTTP {code}).", "data", code)
                log.warning("RAM Tracking request failed: %s", err)
                self._note(False, str(err))
                raise err from None
            except httpx.TimeoutException:
                err = RamError("RAM's vehicle service didn't answer in time.", "data")
                self._note(False, str(err))
                raise err from None
            except (httpx.HTTPError, ValueError) as e:
                err = RamError(f"Couldn't read RAM's vehicle service ({type(e).__name__}).", "data")
                self._note(False, str(err))
                raise err from None
            self._note(True, "")
            return data
        raise RamError("RAM kept refusing the sign-in token.", "data", 401)  # pragma: no cover - the loop returns or raises

    async def _vehicles_raw(self) -> list[dict[str, Any]]:
        data = await self._call("vehicles", self.endpoints["vehicles"], VEHICLES_TTL_S)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("content", "items", "data", "results", "value"):
                if isinstance(data.get(key), list):
                    return data[key]
            if "registration" in data or "vehicle_status" in data:
                return [data]  # RAM's Swagger page documents the response as a single VehicleDTO
        return []

    @staticmethod
    def _rows(raw: list[Any]) -> list[dict[str, Any]]:
        return [row for row in map(_vehicle_row, raw) if row is not None]

    async def vehicles(self) -> list[dict[str, Any]]:
        return self._rows(await self._vehicles_raw())

    async def positions(self) -> list[dict[str, Any]]:
        rows = []
        for row in self._rows(await self._vehicles_raw()):
            if row["lat"] is None or row["lng"] is None:
                continue
            rows.append({**row, "vehicle_id": row["id"], "speed_mph": 15 if row["moving"] else 0, "address": None})
        return rows

    async def journeys(self, vehicle_id: str, day: date) -> list[dict[str, Any]]:
        start = datetime.combine(day, time(0, 0))
        end = start + timedelta(days=1)
        path = self.endpoints["history"].format(id=vehicle_id, **{"from": quote(start.isoformat(), safe=""),
                                                                   "to": quote(end.isoformat(), safe="")})
        # A finished day never changes; today's is still being written.
        data = await self._call("history", path, HISTORY_TTL_TODAY_S if day >= date.today() else HISTORY_TTL_PAST_S)
        events = data.get("history") if isinstance(data, dict) else data
        events = sorted((e for e in (events if isinstance(events, list) else []) if isinstance(e, dict)),
                        key=lambda e: str(e.get("event_date")))
        legs: list[dict[str, Any]] = []
        leg_start: dict[str, Any] | None = None
        for e in events:
            name = _event_name(e.get("event_name"))
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

    def _sample(self) -> None:
        if self.demo:
            demo_guard.touch(demo_guard.VEHICLES)

    async def vehicles(self) -> list[dict[str, Any]]:
        self._sample()  # sample vans: never handed to the model as the real fleet
        return [{"id": f"V{n}", "registration": f"YD{71 + n} SFS", "driver": s["name"]}
                for n, s in enumerate(await self.fsm.staff())]

    async def positions(self) -> list[dict[str, Any]]:
        self._sample()
        out = []
        for p in await self.fsm.locations():
            out.append({"vehicle_id": p["vehicle"], "registration": p["vehicle"], "driver": p["engineer"], "lat": p["lat"],
                        "lng": p["lng"], "timestamp": p["timestamp"], "speed_mph": p["speed_mph"],
                        "ignition": p["status"] != "parked", "address": None})
        return out

    async def journeys(self, vehicle_id: str, day: date) -> list[dict[str, Any]]:
        self._sample()
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
