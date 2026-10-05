"""Engineer home points: lets Fleet / `who_is_home` say a van is "home" without any address labels from RAM.

RAM's public API has no address labels, only a position, so the owner says where each engineer lives and Jarvis shows "home"
when that engineer's van is within a short distance (100 m by default) of that point. Where someone lives is sensitive
personal data, so this is built to hold as little of it as possible:

* **The postcode is never stored.** The owner types a UK postcode once in Settings > Engineer homes. The server looks it up
  once (postcodes.io, POSTed in the request body rather than put in a URL, so it is not in any URL log), rounds the result to
  4 decimal places (about 11 m - plenty for a 100 m radius, too coarse to pick out a front door) and keeps ONLY the engineer's
  name, that rounded point, and who set it and when. The postcode text is dropped as soon as the lookup returns: it is not in
  the database, any response, the logs or an audit line, and no error message repeats it.
* **Owner-only, and not a tool.** The routes are principal-owner only (``access.ROUTE_POLICY``) and no brain tool lists, reads,
  sets or clears homes (``tests/test_engineer_homes.py`` greps for that). The model only ever receives the derived answer
  that ``Tracker`` computes - "home" / not - never a coordinate, postcode or distance.
* **Not exported.** Nothing that dumps tables or settings (memory pop-up, staff report, Azure archive, status, settings page)
  touches this table; the test suite pins that.
* **Erasable.** Remove (one), Remove all, and removing an engineer from the staff list (``prune``) delete the point.

Matching is by the van's driver name (``oncall.name_matches``: whole-word, so "Ian" is "Ian Frost" but not "Christian Smith"); if
a driver name fits more than one stored engineer the answer is "unknown" rather than a guess.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .oncall import name_matches
from .tracking import haversine_m

log = logging.getLogger(__name__)

POSTCODES_URL = "https://api.postcodes.io/postcodes"  # POST {"postcodes": [...]} (the postcode goes in the body, not the URL)
GEOCODE_TIMEOUT_S = 5.0
COORD_DP = 4                      # about 11 m: fine for a 100 m radius, coarse for a house
RADIUS_KEY = "engineer_home_radius_m"
DEFAULT_RADIUS_M, MIN_RADIUS_M, MAX_RADIUS_M = 100, 50, 300
MAX_NAME_CHARS = 80
UK_POSTCODE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?[0-9][A-Z]{2}$")
# A rough box around the UK (Shetland to Scilly, Northern Ireland to East Anglia): a result outside it is not a UK postcode.
UK_LAT, UK_LNG = (49.5, 61.0), (-8.7, 2.0)
ENGINEER_ROLE = re.compile(r"engineer|install|technician|field|surveyor|commission|operative|apprentice", re.I)


class HomeError(Exception):
    """Something the owner should be told, in words that never repeat the postcode. ``status`` is the HTTP code to use."""
    status = 400


class PostcodeInvalid(HomeError):
    status = 400


class PostcodeNotFound(HomeError):
    status = 400


class GeocodeUnavailable(HomeError):
    status = 502


class UnknownEngineer(HomeError):
    status = 404


MESSAGES = {
    PostcodeInvalid: "That doesn't look like a UK postcode. Check it and try again - nothing was saved.",
    PostcodeNotFound: "The postcode service doesn't know that postcode. Check it and try again - nothing was saved.",
    GeocodeUnavailable: "Couldn't reach the postcode service just now, so nothing was saved. Try again in a minute.",
}


def normalise_postcode(raw: Any) -> str:
    """'bd16  1aa' -> 'BD16 1AA'. Raises PostcodeInvalid (never quoting the input) unless it is shaped like a UK postcode."""
    text = re.sub(r"\s+", "", str(raw or "")).upper()
    if not UK_POSTCODE.match(text):
        raise PostcodeInvalid(MESSAGES[PostcodeInvalid])
    return f"{text[:-3]} {text[-3:]}"


def round_point(lat: float, lng: float) -> tuple[float, float]:
    return round(float(lat), COORD_DP), round(float(lng), COORD_DP)


async def geocode_postcode(http: httpx.AsyncClient, raw: Any) -> tuple[float, float]:
    """One server-side lookup of a UK postcode -> (lat, lng), already rounded. Raises PostcodeInvalid, PostcodeNotFound or
    GeocodeUnavailable; none of their messages (or any log line) contains the postcode."""
    postcode = normalise_postcode(raw)
    try:
        r = await http.post(POSTCODES_URL, json={"postcodes": [postcode]}, timeout=httpx.Timeout(GEOCODE_TIMEOUT_S))
    except httpx.HTTPError:
        raise GeocodeUnavailable(MESSAGES[GeocodeUnavailable]) from None
    if r.status_code == 404:
        raise PostcodeNotFound(MESSAGES[PostcodeNotFound])
    if r.status_code != 200:  # 429, 5xx...
        raise GeocodeUnavailable(MESSAGES[GeocodeUnavailable])
    try:
        found = r.json()["result"][0]["result"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise GeocodeUnavailable(MESSAGES[GeocodeUnavailable]) from None
    if not isinstance(found, dict):  # postcodes.io answers 200 with "result": null for a postcode it does not have
        raise PostcodeNotFound(MESSAGES[PostcodeNotFound])
    try:
        lat, lng = float(found["latitude"]), float(found["longitude"])  # a terminated postcode has these null
    except (KeyError, TypeError, ValueError):
        raise PostcodeNotFound(MESSAGES[PostcodeNotFound]) from None
    if not (UK_LAT[0] <= lat <= UK_LAT[1] and UK_LNG[0] <= lng <= UK_LNG[1]):
        raise PostcodeNotFound(MESSAGES[PostcodeNotFound])
    return round_point(lat, lng)


def _clean_name(raw: Any) -> str:
    return " ".join(str(raw or "").split())[:MAX_NAME_CHARS]


class EngineerHomes:
    def __init__(self, db, http: httpx.AsyncClient | None = None, fsm=None, register=None, ram=None) -> None:
        self.db = db
        self.http = http
        self.fsm = fsm
        self.register = register
        self.ram = ram
        self.audit = None  # set by Jarvis: callable(action: str, detail: str) -> None, writes to the activity log

    # ------------------------------------------------------------------ the radius
    @property
    def radius_m(self) -> int:
        try:
            value = int(float(self.db.get_kv(RADIUS_KEY) or DEFAULT_RADIUS_M))
        except (TypeError, ValueError):
            return DEFAULT_RADIUS_M
        return max(MIN_RADIUS_M, min(MAX_RADIUS_M, value))

    def set_radius(self, metres: Any, by: str = "the owner") -> int:
        try:
            value = int(float(metres))
        except (TypeError, ValueError):
            raise HomeError(f"Give a whole number of metres from {MIN_RADIUS_M} to {MAX_RADIUS_M}.") from None
        if not MIN_RADIUS_M <= value <= MAX_RADIUS_M:
            raise HomeError(f"Give a whole number of metres from {MIN_RADIUS_M} to {MAX_RADIUS_M}.")
        self.db.set_kv(RADIUS_KEY, str(value))
        self._audit("radius", f"Home match distance set to {value} m by {by}")
        return value

    # ------------------------------------------------------------------ who the owner can pick
    async def known_engineers(self) -> list[str]:
        """The engineers Jarvis knows of: Salts FSM staff who are engineers (or have no role to say otherwise), people the
        staff register lists as engineers, and the drivers RAM names on the vans. Sorted, no duplicates. [] when none of
        those can be read, which makes ``prune`` do nothing rather than wipe the lot."""
        names: dict[str, str] = {}

        def add(name: Any) -> None:
            clean = _clean_name(name)
            if clean:
                names.setdefault(clean.lower(), clean)

        if self.fsm is not None:
            try:
                for s in await self.fsm.staff():
                    role = str(s.get("role") or "")
                    if not role or ENGINEER_ROLE.search(role):
                        add(s.get("name"))
            except Exception as e:  # noqa: BLE001 - an FSM outage must not break the page
                log.warning("Engineer homes: couldn't read the FSM staff list (%s)", type(e).__name__)
        if self.register is not None:
            try:
                for p in self.register.people("engineer"):
                    add(p.get("name"))
            except Exception as e:  # noqa: BLE001
                log.warning("Engineer homes: couldn't read the staff register (%s)", type(e).__name__)
        if self.ram is not None and not getattr(self.ram, "demo", True):
            try:
                for v in await self.ram.vehicles():
                    add(v.get("driver"))
            except Exception as e:  # noqa: BLE001
                log.warning("Engineer homes: couldn't read the RAM drivers (%s)", type(e).__name__)
        return sorted(names.values(), key=str.lower)

    def listing(self, known: list[str]) -> list[dict[str, Any]]:
        """What the Settings page shows: each engineer, whether a home is set and when. Never a point, never a postcode.
        An engineer who has a stored home but is no longer in the list still appears, so it can be removed."""
        stored = {r["engineer"].lower(): r for r in self.db.engineer_homes_set()}
        rows = []
        for name in known:
            r = stored.pop(name.lower(), None)
            rows.append({"engineer": name, "set": r is not None, "set_at": r["set_at"] if r else None,
                         "in_list": True})
        rows += [{"engineer": r["engineer"], "set": True, "set_at": r["set_at"], "in_list": False}
                 for r in stored.values()]
        return rows

    # ------------------------------------------------------------------ owner actions
    async def set_from_postcode(self, engineer: Any, postcode: Any, by: str = "the owner") -> dict[str, Any]:
        """Look the postcode up once, keep the rounded point, forget the postcode."""
        known = await self.known_engineers()
        chosen = next((n for n in known if n.lower() == _clean_name(engineer).lower()), None)
        if chosen is None:
            raise UnknownEngineer("That engineer isn't in Jarvis's list.")
        if self.http is None:
            raise GeocodeUnavailable(MESSAGES[GeocodeUnavailable])
        lat, lng = await geocode_postcode(self.http, postcode)
        del postcode
        self.db.set_engineer_home(chosen, lat, lng, by)
        self._audit("set", f"Home point set for {chosen} by {by}")
        return {"engineer": chosen, "set": True}

    def clear(self, engineer: Any, by: str = "the owner") -> bool:
        name = _clean_name(engineer)
        removed = bool(self.db.delete_engineer_home(name))
        if removed:
            self._audit("clear", f"Home point removed for {name} by {by}")
        return removed

    def clear_all(self, by: str = "the owner") -> int:
        n = self.db.delete_all_engineer_homes()
        if n:
            self._audit("clear_all", f"All {n} home points removed by {by}")
        return n

    def prune(self, known: list[str]) -> int:
        """Delete the home of anyone no longer in the staff list. Does nothing when the list is empty (it could not be read)."""
        if not known:
            return 0
        gone = [r["engineer"] for r in self.db.engineer_homes_set()
                if not any(r["engineer"].lower() == k.lower() or name_matches(r["engineer"], k) for k in known)]
        for name in gone:
            self.db.delete_engineer_home(name)
            self._audit("clear", f"Home point removed for {name}: no longer in the staff list")
        return len(gone)

    async def maintain(self) -> int:
        """The scheduled retention job: forget the home of anyone who has left the list."""
        return self.prune(await self.known_engineers())

    # ------------------------------------------------------------------ the matcher (used by Tracker only)
    def state(self, engineer: Any, position: tuple[float, float] | None) -> bool | None:
        """True: within the radius of this engineer's home. False: they have a home point and the van is elsewhere. None:
        no home point for them (or no way to tell who/where) - which is not the same as away."""
        if position is None or not str(engineer or "").strip():
            return None
        points = self.db.engineer_home_points()
        mine = [p for p in points if p["engineer"].lower() == str(engineer).strip().lower()]
        if not mine:
            mine = [p for p in points if name_matches(p["engineer"], engineer)]
        if len(mine) != 1:  # none, or a driver name that fits several people: don't guess
            return None
        return haversine_m(position, (mine[0]["lat"], mine[0]["lng"])) <= self.radius_m

    # ------------------------------------------------------------------ audit
    def _audit(self, action: str, detail: str) -> None:
        """One line in the activity log (engineer name and time only - no postcode, no point). Never raises."""
        log.info("Engineer homes: %s", detail)
        if self.audit is not None:
            try:
                self.audit(action, detail)
            except Exception:  # noqa: BLE001
                log.warning("Engineer homes: couldn't write the activity line")
