"""Engineer home points: Fleet / who_is_home say a van is "home" without RAM address labels.

RAM's API has no address labels, so the owner marks where each engineer lives (a postcode, once) and a van within ~100 m of its
driver's point is "home". Where someone lives is sensitive, so what is pinned here is mostly about what is NOT kept or shown:

* geocoding: one server-side lookup, the postcode normalised first, every failure (invalid, unknown, timeout, API down, rubbish
  answer) reported in words that never repeat it, and nothing saved;
* storage: only the engineer's name, a point rounded to 4 decimal places and who/when - the postcode is in no table, no file
  bytes, no response, no log and no audit line, and a deleted point is overwritten in the file;
* matching: haversine at 99 m / 101 m, the radius setting (50-300 m), name matching that refuses to guess;
* integration: at_home in live / who_is_home / nearest / van_day with and without a home, in every out-of-hours mode, the RAM
  label as a fallback only, a van at home shown as "home" and never a street or a point;
* access: principal owner only (anonymous 401, manager 403, team 403), a same-origin click for changes;
* not a tool, not exported: no brain code can reach it, and nothing that dumps tables or settings includes it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import struct
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis import access
from jarvis.brain.tools import TOOLS, TOOLS_BY_NAME
from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.db import Database
from jarvis.main import create_app
from jarvis.services.engineer_homes import (DEFAULT_RADIUS_M, EngineerHomes, GeocodeUnavailable, HomeError,
                                            PostcodeInvalid, PostcodeNotFound, UnknownEngineer, geocode_postcode,
                                            normalise_postcode, round_point)
from jarvis.services.tracking import Tracker, combine_home
from tests.fakes import FakeClient

ROOT = Path(__file__).resolve().parent.parent
OWNER_PW = "owner-pass-1234"
TEAM_CODE = "team-code-5678"
MANAGER = "manager@salts.example"
POSTCODE = "BD16 1AA"
PC_FORMS = ("BD16 1AA", "BD161AA", "bd16 1aa", "bd16  1aa")
HOME = (53.9123, -1.6543)          # the point the fake postcode service returns, already at 4 dp
HOME_RAW = (53.912345, -1.654321)  # what the service sends (6 dp)
M_PER_DEG_LAT = 111194.93


def north(point, metres):
    return (point[0] + metres / M_PER_DEG_LAT, point[1])


def postcode_service(result=HOME_RAW, status=200, seen=None):
    """A fake postcodes.io: records each request and answers like the real bulk endpoint."""
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"status": status, "error": "x"})
        queried = json.loads(request.content)["postcodes"][0]
        body = None if result is None else {"postcode": queried, "latitude": result[0], "longitude": result[1]}
        return httpx.Response(200, json={"status": 200, "result": [{"query": queried, "result": body}]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def raising(exc):
    def handler(request):
        raise exc
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def dump(db: Database) -> str:
    """Every table of the database, as text (so 'the postcode is nowhere in it' means nowhere)."""
    return "\n".join(db._conn.iterdump())


# =============================================================================================================== geocoding
@pytest.mark.parametrize("raw", PC_FORMS)
async def test_geocode_success_normalises_the_postcode_and_rounds_the_point(raw):
    seen = []
    async with postcode_service(seen=seen) as http:
        point = await geocode_postcode(http, raw)
    assert point == HOME and round_point(*HOME_RAW) == HOME
    (req,) = seen
    assert req.method == "POST" and json.loads(req.content) == {"postcodes": [POSTCODE]}  # normalised, in the body
    assert "BD16" not in str(req.url) and "BD161AA" not in str(req.url)                   # never in a URL (URL logs, proxies)


async def test_geocode_uses_a_short_timeout():
    seen = []
    async with postcode_service(seen=seen) as http:
        await geocode_postcode(http, POSTCODE)
    timeouts = seen[0].extensions["timeout"]
    assert 0 < max(timeouts.values()) <= 10


@pytest.mark.parametrize("bad", ["", "   ", "hello", "12345", "BD16", "BD16 1A", "BD16 1AAA", "1BD6 1AA", "BD16-1AA",
                                 "SELECT 1; --", "BD16 1AA; DROP TABLE x", None, 12345, "A" * 500])
async def test_an_invalid_postcode_is_refused_without_any_lookup_or_echo(bad):
    seen = []
    async with postcode_service(seen=seen) as http:
        with pytest.raises(PostcodeInvalid) as e:
            await geocode_postcode(http, bad)
    assert seen == []                                  # nothing was sent anywhere
    assert str(bad) not in str(e.value) or str(bad) in ("", "None")  # the message never repeats what was typed
    assert "UK postcode" in str(e.value) and "nothing was saved" in str(e.value)


def test_normalise_accepts_every_uk_format_and_keeps_the_incode_separate():
    for raw, want in (("sw1a2aa", "SW1A 2AA"), ("M1 1AE", "M1 1AE"), ("b33 8th", "B33 8TH"), ("CR2 6XH", "CR2 6XH"),
                      ("DN55 1PT", "DN55 1PT"), ("W1A 1HQ", "W1A 1HQ"), ("EC1A 1BB", "EC1A 1BB")):
        assert normalise_postcode(raw) == want


async def test_an_unknown_postcode_is_a_clear_message_for_404_and_for_a_null_result():
    for http in (postcode_service(status=404), postcode_service(result=None)):
        async with http:
            with pytest.raises(PostcodeNotFound) as e:
                await geocode_postcode(http, POSTCODE)
        assert "BD16" not in str(e.value) and "nothing was saved" in str(e.value)


async def test_a_terminated_postcode_with_no_coordinates_or_one_outside_the_uk_is_not_used():
    async with postcode_service(result=(None, None)) as http:
        with pytest.raises(PostcodeNotFound):
            await geocode_postcode(http, POSTCODE)
    async with postcode_service(result=(48.85, 2.35)) as http:  # Paris
        with pytest.raises(PostcodeNotFound):
            await geocode_postcode(http, POSTCODE)


@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.ConnectError("down"),
                                 httpx.RemoteProtocolError("bad")])
async def test_a_timeout_or_network_failure_is_reported_and_nothing_leaks_into_the_message(exc):
    async with raising(exc) as http:
        with pytest.raises(GeocodeUnavailable) as e:
            await geocode_postcode(http, POSTCODE)
    assert "Try again" in str(e.value) and "BD16" not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__  # no chained error that could carry the request


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_the_api_being_down_or_busy_is_reported_as_unavailable(status):
    async with postcode_service(status=status) as http:
        with pytest.raises(GeocodeUnavailable):
            await geocode_postcode(http, POSTCODE)


async def test_rubbish_from_the_api_is_unavailable_not_a_crash():
    for payload in ("not json", {"status": 200}, {"result": []}, {"result": [{"result": "x"}]}, {"result": [{}]}):
        def handler(request, payload=payload):
            return httpx.Response(200, content=payload if isinstance(payload, str) else json.dumps(payload))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises((GeocodeUnavailable, PostcodeNotFound)):
                await geocode_postcode(http, POSTCODE)


# ================================================================================================================= storage
class Staff:
    demo = True

    async def staff(self):
        return [{"name": "Dan Harper", "role": "Senior Fire Engineer"}, {"name": "Priya Shah", "role": "Fire Engineer"},
                {"name": "Hannah Cole", "role": "Office Manager / Scheduler"}]


def homes_for(tmp_path, http=None, fsm=None):
    db = Database(tmp_path / "h.sqlite")
    return EngineerHomes(db, http or postcode_service(), fsm or Staff()), db


async def test_saving_keeps_only_name_rounded_point_and_who_when_and_never_the_postcode(tmp_path):
    homes, db = homes_for(tmp_path)
    audit = []
    homes.audit = lambda action, detail: audit.append(detail)
    await homes.set_from_postcode("dan harper", "bd16  1aa", "the owner")
    rows = db.query("SELECT * FROM engineer_homes")
    assert len(rows) == 1 and set(rows[0]) == {"engineer", "lat", "lng", "set_by", "set_at"}
    assert rows[0]["engineer"] == "Dan Harper"                                   # the list's spelling, not what was typed
    assert (rows[0]["lat"], rows[0]["lng"]) == HOME and rows[0]["set_by"] == "the owner"
    text = dump(db)
    for form in PC_FORMS + ("BD161AA", "1AA"):
        assert form.lower() not in text.lower(), form                           # not in ANY table
    assert "53.912345" not in text and "1.654321" not in text                   # the unrounded point is not kept either
    assert audit == ["Home point set for Dan Harper by the owner"]              # name and who; no point, no postcode
    assert not re.search(r"\d{2}\.\d{3}", audit[0])


async def test_the_database_file_never_contains_the_postcode_and_a_deleted_point_is_overwritten(tmp_path):
    homes, db = homes_for(tmp_path)
    await homes.set_from_postcode("Dan Harper", POSTCODE)
    db._conn.commit()
    raw = Path(db.path).read_bytes()
    assert b"BD16" not in raw and b"bd16" not in raw.lower() and b"BD161AA" not in raw
    lat_bytes, lng_bytes = struct.pack(">d", HOME[0]), struct.pack(">d", HOME[1])
    assert lat_bytes in raw and lng_bytes in raw                                 # (so the check below can fail)
    homes.clear("Dan Harper")
    raw = Path(db.path).read_bytes()
    assert lat_bytes not in raw and lng_bytes not in raw                         # secure_delete: gone from the file


async def test_a_rejected_lookup_saves_nothing_and_leaves_an_existing_home_alone(tmp_path):
    homes, db = homes_for(tmp_path)
    await homes.set_from_postcode("Dan Harper", POSTCODE)
    before = db.query("SELECT * FROM engineer_homes")
    for http, exc in ((postcode_service(status=404), PostcodeNotFound), (raising(httpx.ConnectTimeout("t")), GeocodeUnavailable),
                      (postcode_service(status=503), GeocodeUnavailable)):
        homes.http = http
        with pytest.raises(exc):
            await homes.set_from_postcode("Dan Harper", "LS1 4AP")
    with pytest.raises(PostcodeInvalid):
        await homes.set_from_postcode("Dan Harper", "nonsense")
    assert db.query("SELECT * FROM engineer_homes") == before


async def test_an_engineer_who_is_not_in_the_list_cannot_be_given_a_home(tmp_path):
    homes, db = homes_for(tmp_path)
    seen = []
    homes.http = postcode_service(seen=seen)
    with pytest.raises(UnknownEngineer):
        await homes.set_from_postcode("Somebody Else", POSTCODE)
    with pytest.raises(UnknownEngineer):
        await homes.set_from_postcode("Hannah Cole", POSTCODE)  # office staff are not offered
    assert seen == [] and db.engineer_homes_set() == []


async def test_the_list_offers_engineers_not_office_staff_and_merges_register_and_ram_drivers(tmp_path):
    class Register:
        def people(self, kind=None):
            return [{"name": "Tom Wilkinson"}]

    class Ram:
        demo = False

        async def vehicles(self):
            return [{"driver": "Mo Khan"}, {"driver": "dan harper"}, {"driver": None}]

    db = Database(tmp_path / "x.sqlite")
    homes = EngineerHomes(db, None, Staff(), Register(), Ram())
    assert await homes.known_engineers() == ["Dan Harper", "Mo Khan", "Priya Shah", "Tom Wilkinson"]


async def test_the_listing_has_no_coordinates_postcodes_or_distances_and_still_offers_remove_for_someone_who_left(tmp_path):
    homes, db = homes_for(tmp_path)
    await homes.set_from_postcode("Dan Harper", POSTCODE)
    db.set_engineer_home("Former Colleague", 53.1, -1.1, "the owner")
    rows = homes.listing(await homes.known_engineers())
    assert [r["engineer"] for r in rows] == ["Dan Harper", "Priya Shah", "Former Colleague"]
    assert {k for r in rows for k in r} == {"engineer", "set", "set_at", "in_list"}
    assert [(r["set"], r["in_list"]) for r in rows] == [(True, True), (False, True), (True, False)]
    assert "53.9" not in json.dumps(rows) and "1.65" not in json.dumps(rows)


async def test_removing_one_or_all_deletes_the_points_and_is_audited_without_coordinates(tmp_path):
    homes, db = homes_for(tmp_path)
    audit = []
    homes.audit = lambda action, detail: audit.append((action, detail))
    await homes.set_from_postcode("Dan Harper", POSTCODE)
    await homes.set_from_postcode("Priya Shah", POSTCODE)
    assert homes.clear("dan harper") is True and homes.clear("Dan Harper") is False
    assert [r["engineer"] for r in db.engineer_homes_set()] == ["Priya Shah"]
    assert homes.clear_all() == 1 and db.engineer_homes_set() == [] and homes.clear_all() == 0
    assert [a for a, _ in audit] == ["set", "set", "clear", "clear_all"]
    assert all("53." not in d and "1.6" not in d for _, d in audit)


async def test_removing_an_engineer_from_the_staff_list_deletes_their_point_but_an_unreadable_list_deletes_nothing(tmp_path):
    homes, db = homes_for(tmp_path)
    await homes.set_from_postcode("Dan Harper", POSTCODE)
    await homes.set_from_postcode("Priya Shah", POSTCODE)
    assert homes.prune([]) == 0 and len(db.engineer_homes_set()) == 2           # list could not be read: keep everything
    assert homes.prune(["Priya Shah"]) == 1 and [r["engineer"] for r in db.engineer_homes_set()] == ["Priya Shah"]

    class Down:
        async def staff(self):
            raise RuntimeError("FSM is down")

    homes.fsm = Down()
    assert await homes.maintain() == 0 and len(db.engineer_homes_set()) == 1


# ================================================================================================================= matching
def test_99_metres_is_home_and_101_is_not_with_the_default_radius(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    homes = EngineerHomes(db)
    db.set_engineer_home("Dan Harper", *HOME, "the owner")
    assert homes.radius_m == DEFAULT_RADIUS_M == 100
    assert homes.state("Dan Harper", north(HOME, 99)) is True
    assert homes.state("Dan Harper", north(HOME, 101)) is False
    assert homes.state("Dan Harper", HOME) is True


def test_the_radius_setting_moves_the_boundary_and_is_limited_to_50_to_300(tmp_path):
    db = Database(tmp_path / "r.sqlite")
    homes = EngineerHomes(db)
    db.set_engineer_home("Dan Harper", *HOME, "the owner")
    assert homes.set_radius(150) == 150
    assert homes.state("Dan Harper", north(HOME, 149)) is True and homes.state("Dan Harper", north(HOME, 151)) is False
    homes.set_radius("50")
    assert homes.state("Dan Harper", north(HOME, 49)) is True and homes.state("Dan Harper", north(HOME, 51)) is False
    homes.set_radius(300)
    assert homes.state("Dan Harper", north(HOME, 299)) is True and homes.state("Dan Harper", north(HOME, 301)) is False
    for bad in (49, 301, 0, -100, "abc", None, "", float("nan")):
        with pytest.raises(HomeError):
            homes.set_radius(bad)
    assert homes.radius_m == 300  # a refused value changes nothing
    db.set_kv("engineer_home_radius_m", "9999")
    assert homes.radius_m == 300 and (db.set_kv("engineer_home_radius_m", "junk") or homes.radius_m) == 100  # bad stored value is safe


def test_matching_is_by_driver_name_and_refuses_to_guess(tmp_path):
    db = Database(tmp_path / "n.sqlite")
    homes = EngineerHomes(db)
    db.set_engineer_home("Ian Frost", *HOME, "the owner")
    here = north(HOME, 10)
    assert homes.state("Ian Frost", here) is True and homes.state("IAN FROST", here) is True
    assert homes.state("Ian", here) is True                      # RAM sometimes has just a first name
    assert homes.state("Christian Frost", here) is None          # not the same person
    assert homes.state("Priya Shah", here) is None               # no home set for her: unknown, not "away"
    assert homes.state("", here) is None and homes.state(None, here) is None and homes.state("Ian Frost", None) is None
    db.set_engineer_home("Ian Frost Jr", 53.0, -1.0, "the owner")
    assert homes.state("Ian Frost", here) is True                # an exact name still wins...
    assert homes.state("Ian", here) is None                      # ...but "Ian" now fits two people, so nobody is assumed


def test_combine_home_prefers_the_point_and_keeps_ram_labels_as_a_fallback_only():
    assert combine_home(("14 Mill Lane, Bingley", False), True) == ("home", True)       # a street label never shows at home
    assert combine_home(("home", True), None) == ("home", True)                         # label fallback
    assert combine_home(("home", True), False) == ("home", True)
    assert combine_home(("Otley Road", False), False) == ("Otley Road", False)
    assert combine_home((None, None), False) == (None, False)                           # a point makes "away" definite
    assert combine_home((None, None), None) == (None, None)                             # nothing to go on: unknown
    assert combine_home(("Otley Road", False), None) == ("Otley Road", False)


# ============================================================================================================= integration
def _now(minutes_ago=0):
    return (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()


class RealFSM:
    demo = False

    def __init__(self, sites=()):
        self._sites = list(sites)

    async def sites(self):
        return self._sites

    async def locations(self):
        return []

    async def jobs(self, *a, **k):
        return []


class FakeRam:
    demo = False

    def __init__(self, vehicles, legs=None):
        self._vehicles = vehicles
        self._legs = legs or {}

    async def vehicles(self):
        return self._vehicles

    async def positions(self):
        return [{**v, "vehicle_id": v["id"], "speed_mph": 0} for v in self._vehicles if v.get("lat") is not None]

    async def journeys(self, vehicle_id, day):
        return self._legs.get(str(vehicle_id), [])


def van(id_, reg, driver, at, label=None, minutes_ago=2):
    return {"id": id_, "registration": reg, "driver": driver, "lat": at[0], "lng": at[1], "timestamp": _now(minutes_ago),
            "moving": False, "address_label": label}


def world(tmp_path, monkeypatch, vehicles, *, homes_set=("Dan Harper",), mode="off", working=True, legs=None, sites=()):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: working))
    s = Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None, van_locations_out_of_hours=mode)
    db = Database(tmp_path / "t.sqlite")
    homes = EngineerHomes(db)
    for name in homes_set:
        db.set_engineer_home(name, *HOME, "the owner")
    tracker = Tracker(RealFSM(sites), http=None, ram=FakeRam(vehicles, legs), settings=s, db=db, homes=homes)
    return tracker, db, homes


async def test_live_at_home_from_the_point_with_no_ram_label_at_all(tmp_path, monkeypatch):
    tracker, _, _ = world(tmp_path, monkeypatch, [van(1, "SA51 LTS", "Dan Harper", north(HOME, 40)),
                                                  van(2, "YD72 SFS", "Priya Shah", north(HOME, 5000))],
                          homes_set=("Dan Harper", "Priya Shah"))
    by = {e["engineer"]: e for e in (await tracker.live())["engineers"]}
    assert by["Dan Harper"]["at_home"] is True and by["Dan Harper"]["address_label"] == "home"
    assert by["Priya Shah"]["at_home"] is False and by["Priya Shah"]["address_label"] is None  # away is a definite answer


async def test_a_van_at_home_shows_only_home_even_if_ram_sends_a_street(tmp_path, monkeypatch):
    street = "14 Acacia Avenue, Bradford"
    tracker, _, _ = world(tmp_path, monkeypatch, [van(1, "SA51 LTS", "Dan Harper", HOME, label=street)])
    live = await tracker.live()
    row = live["engineers"][0]
    assert row["at_home"] is True and row["address_label"] == "home"
    assert street not in json.dumps(live) and "Acacia" not in json.dumps(await tracker.home_status("x"))


async def test_no_home_set_and_no_label_is_unknown_and_listed_apart_in_who_is_home(tmp_path, monkeypatch):
    tracker, _, _ = world(tmp_path, monkeypatch, [van(1, "SA51 LTS", "Dan Harper", HOME), van(2, "YD72 SFS", "Priya Shah", HOME)],
                          homes_set=("Dan Harper",))
    home = await tracker.home_status("Sam")
    assert [e["engineer"] for e in home["at_home"]] == ["Dan Harper"]
    assert [e["engineer"] for e in home["no_address_label"]] == ["Priya Shah"] and home["out"] == []
    live = await tracker.live()
    assert "Priya Shah" in live["address_label_note"] and "No home set" in live["address_label_note"]
    assert "Dan Harper" not in live["address_label_note"]


async def test_a_ram_home_label_still_counts_when_no_home_is_set_and_never_shows_the_street(tmp_path, monkeypatch):
    tracker, _, _ = world(tmp_path, monkeypatch, [van(1, "SA51 LTS", "Priya Shah", (53.0, -1.0), label="Priya home 9 High St")],
                          homes_set=())
    row = (await tracker.live())["engineers"][0]
    assert row["at_home"] is True and row["address_label"] == "home"


async def test_a_ram_label_that_is_not_home_still_means_away_without_a_home_point(tmp_path, monkeypatch):
    tracker, _, _ = world(tmp_path, monkeypatch, [van(1, "SA51 LTS", "Priya Shah", HOME, label="Otley Road, Ilkley")],
                          homes_set=())
    row = (await tracker.live())["engineers"][0]
    assert row["at_home"] is False and row["address_label"] == "Otley Road, Ilkley"


async def test_who_is_home_buckets_home_out_unknown_and_stale(tmp_path, monkeypatch):
    stale = van(4, "YD74 SFS", "Mo Khan", HOME, minutes_ago=300)
    tracker, _, _ = world(tmp_path, monkeypatch, [van(1, "A", "Dan Harper", HOME), van(2, "B", "Priya Shah", north(HOME, 900)),
                                                  van(3, "C", "Tom Wilkinson", HOME), stale],
                          homes_set=("Dan Harper", "Priya Shah", "Mo Khan"))
    home = await tracker.home_status("Sam")
    assert [e["engineer"] for e in home["at_home"]] == ["Dan Harper"]
    assert [e["engineer"] for e in home["out"]] == ["Priya Shah"]
    assert [e["engineer"] for e in home["no_address_label"]] == ["Tom Wilkinson"]
    assert [e["engineer"] for e in home["no_recent_position"]] == ["Mo Khan"]       # a stale fix is not "at home"
    assert "home point" in home["note"]
    blob = json.dumps(home)
    assert "53.91" not in blob and "1.654" not in blob                                # no coordinates anywhere in the answer


async def test_nearest_carries_the_home_flag_and_no_home_point(tmp_path, monkeypatch):
    tracker, _, _ = world(tmp_path, monkeypatch, [van(1, "A", "Dan Harper", HOME)],
                          sites=[{"name": "Aire Valley Care Home", "lat": 53.844, "lng": -1.837}])
    near = await tracker.nearest("Aire Valley", "Sam")
    assert near["engineers"][0]["at_home"] is True and near["engineers"][0]["address_label"] == "home"
    assert "53.9123" not in json.dumps(near)


@pytest.mark.parametrize("mode,shown", [("off", False), ("on_call", False), ("always", True)])
async def test_out_of_hours_modes_still_gate_everything_and_the_lookup_is_still_logged(tmp_path, monkeypatch, mode, shown):
    tracker, db, _ = world(tmp_path, monkeypatch, [van(1, "A", "Dan Harper", HOME), van(2, "B", "Priya Shah", north(HOME, 800))],
                           homes_set=("Dan Harper", "Priya Shah"), mode=mode, working=False)
    live = await tracker.live("Sam")
    home = await tracker.home_status("Sam")
    if not shown:
        assert live["visible"] is False and live["engineers"] == []
        assert home["at_home"] == [] and home["out"] == [] and home["no_address_label"] == []
        assert db.location_lookups() == []
        return
    assert {e["engineer"]: e["at_home"] for e in live["engineers"]} == {"Dan Harper": True, "Priya Shah": False}
    assert [e["engineer"] for e in home["at_home"]] == ["Dan Harper"] and home["out_of_hours_access"] == "always"
    assert {r["engineer"] for r in db.location_lookups()} == {"Dan Harper", "Priya Shah"}
    assert all(r["tool"] in ("engineer_locations", "who_is_home") and r["asked_by"] == "Sam" for r in db.location_lookups())


async def test_out_of_hours_without_a_named_asker_is_off_whatever_the_home_points_say(tmp_path, monkeypatch):
    tracker, db, _ = world(tmp_path, monkeypatch, [van(1, "A", "Dan Harper", HOME)], mode="always", working=False)
    assert (await tracker.home_status(""))["at_home"] == [] and db.location_lookups() == []


async def test_on_call_mode_shows_home_only_for_the_engineer_on_call(tmp_path, monkeypatch):
    tracker, _, _ = world(tmp_path, monkeypatch, [van(1, "A", "Dan Harper", HOME), van(2, "B", "Priya Shah", HOME)],
                          homes_set=("Dan Harper", "Priya Shah"), mode="on_call", working=False)
    now = datetime.now()
    tracker.roster.add("Priya Shah", now - timedelta(hours=1), now + timedelta(hours=1))
    home = await tracker.home_status("Sam")
    assert [e["engineer"] for e in home["at_home"]] == ["Priya Shah"] and home["out"] == []


def leg(start_m, end_m, end_at, address=None):
    day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return {"start_time": (day + timedelta(minutes=start_m)).isoformat(), "end_time": (day + timedelta(minutes=end_m)).isoformat(),
            "end_lat": end_at[0], "end_lng": end_at[1], "end_address": address, "distance_miles": 5}


async def test_van_day_reports_home_from_the_point_and_never_prints_the_home_coordinates(tmp_path, monkeypatch):
    legs = {"1": [leg(1, 2, (53.7, -1.7)), leg(3, 4, north(HOME, 30))]}
    tracker, _, _ = world(tmp_path, monkeypatch, [van(1, "SA51 LTS", "Dan Harper", north(HOME, 20))], legs=legs)
    out = await tracker.van_day("Dan Harper", datetime.now().date(), "Sam")
    assert out["at_home"] is True and out["current_address_label"] == "home"
    assert [t["to"] for t in out["timeline"]] == ["53.7000,-1.7000", "home"]   # the home leg is "home", not a coordinate pair
    assert "53.9123" not in json.dumps(out) and "1.6543" not in json.dumps(out)


async def test_van_day_with_no_home_set_says_no_home_is_set(tmp_path, monkeypatch):
    tracker, _, _ = world(tmp_path, monkeypatch, [van(1, "SA51 LTS", "Priya Shah", HOME)], homes_set=())
    out = await tracker.van_day("Priya Shah", datetime.now().date(), "Sam")
    assert out["at_home"] is None and out["current_address_label"] is None
    assert "No home is set" in out["address_label_note"] and "no address label" in out["address_label_note"]


async def test_van_day_ignores_an_old_fix_when_judging_home(tmp_path, monkeypatch):
    tracker, _, _ = world(tmp_path, monkeypatch, [van(1, "SA51 LTS", "Dan Harper", HOME, minutes_ago=400)])
    assert (await tracker.van_day("Dan Harper", datetime.now().date(), "Sam"))["at_home"] is None


@pytest.mark.parametrize("mode,shows", [("off", False), ("always", True)])
async def test_van_day_out_of_hours_adds_home_only_where_the_setting_allows(tmp_path, monkeypatch, mode, shows):
    tracker, db, _ = world(tmp_path, monkeypatch, [van(1, "A", "Dan Harper", HOME)], mode=mode, working=False)
    out = await tracker.van_day("Dan Harper", datetime.now().date(), "Sam")
    assert ("at_home" in out) is shows
    assert len(db.location_lookups()) == 1                                        # looking at the van is logged either way


async def test_a_broken_home_table_does_not_take_fleet_down(tmp_path, monkeypatch):
    tracker, _, homes = world(tmp_path, monkeypatch, [van(1, "A", "Dan Harper", HOME, label="Dan home")])

    def boom(*a):
        raise RuntimeError("table is locked")

    homes.state = boom
    row = (await tracker.live())["engineers"][0]
    assert row["at_home"] is True                                                 # the RAM label still works


async def test_the_engineer_locations_tool_gives_the_model_no_position_for_a_van_at_home(tmp_path, monkeypatch):
    from jarvis.brain.tools import engineer_locations
    from types import SimpleNamespace

    tracker, _, _ = world(tmp_path, monkeypatch, [van(1, "A", "Dan Harper", HOME), van(2, "B", "Priya Shah", north(HOME, 900))],
                          homes_set=("Dan Harper",))
    published = []
    j = SimpleNamespace(tracker=tracker, bus=SimpleNamespace(publish=lambda k, d: published.append(d)), asked_by="Sam")
    out = await engineer_locations(j, None)
    by = {e["engineer"]: e for e in out["engineers"]}
    assert by["Dan Harper"]["at_home"] is True and by["Dan Harper"]["lat"] is None and by["Dan Harper"]["lng"] is None
    assert by["Priya Shah"]["lat"] is not None                                    # a van that is out keeps its position
    assert published[0]["engineers"][0]["lat"] is not None                        # the display's map still gets positions


# ===================================================================================================== the app: routes
class App:
    def __init__(self, settings, monkeypatch):
        monkeypatch.setattr("jarvis.main.LOGIN_DELAY_S", 0)
        settings.jarvis_owner_password = OWNER_PW
        self.settings = settings
        self.j = Jarvis(settings, client=FakeClient())
        self.j.homes.http = postcode_service()
        self.app = create_app(settings, self.j)

    def anon(self) -> TestClient:
        return TestClient(self.app)

    def owner(self) -> TestClient:
        c = self.anon()
        assert c.post("/login", data={"password": OWNER_PW}, follow_redirects=False).status_code == 303
        return c

    def team(self) -> TestClient:
        c = self.anon()
        assert c.post("/login/team", data={"name": "Sam", "code": TEAM_CODE}, follow_redirects=False).status_code == 303
        return c


@pytest.fixture
def app(settings, monkeypatch):
    a = App(settings, monkeypatch)
    with TestClient(a.app) as base:
        a.base = base
        assert a.owner().post("/api/team-access", json={"code": TEAM_CODE}).status_code == 200
        yield a


ROUTES = [("GET", "/api/engineer-homes", None), ("POST", "/api/engineer-homes", {"engineer": "Dan Harper", "postcode": POSTCODE}),
          ("POST", "/api/engineer-homes/radius", {"metres": 120}), ("DELETE", "/api/engineer-homes/Dan%20Harper", None),
          ("DELETE", "/api/engineer-homes", None)]


def test_every_home_route_is_classified_owner_only():
    for key in ("GET /api/engineer-homes", "POST /api/engineer-homes", "DELETE /api/engineer-homes",
                "POST /api/engineer-homes/radius", "DELETE /api/engineer-homes/{engineer}"):
        assert access.ROUTE_POLICY[key] == access.OWNER_ONLY, key
    assert access.FEATURES[access.OWNER]["engineer_homes"] is True
    assert access.FEATURES[access.MANAGER]["engineer_homes"] is False and access.FEATURES[access.TEAM]["engineer_homes"] is False


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_anonymous_gets_401_manager_403_and_team_403_and_nothing_changes(app, monkeypatch, method, path, body):
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "true")
    app.settings.manager_emails = MANAGER
    app.j.db.set_engineer_home("Dan Harper", *HOME, "the owner")
    kw = {"json": body} if body is not None else {}
    assert app.anon().request(method, path, **kw).status_code == 401
    manager = {"x-ms-client-principal-idp": "aad", "x-ms-client-principal-name": MANAGER}
    assert app.anon().get("/api/settings", headers=manager).status_code == 200            # (they are a real manager)
    assert app.anon().request(method, path, headers=manager, **kw).status_code == 403
    assert app.team().request(method, path, **kw).status_code == 403
    assert [r["engineer"] for r in app.j.db.engineer_homes_set()] == ["Dan Harper"]       # untouched by any of them


def test_the_owner_can_view_set_change_the_distance_and_remove(app):
    c = app.owner()
    info = c.get("/api/engineer-homes").json()
    assert info["radius_m"] == 100 and (info["min_radius_m"], info["max_radius_m"]) == (50, 300)
    assert [e["engineer"] for e in info["engineers"]][:2] == ["Dan Harper", "Kyle Brennan"] and not any(e["set"] for e in info["engineers"])
    r = c.post("/api/engineer-homes", json={"engineer": "Dan Harper", "postcode": POSTCODE})
    assert r.status_code == 200
    dan = next(e for e in r.json()["engineers"] if e["engineer"] == "Dan Harper")
    assert dan["set"] is True and dan["set_at"] and set(dan) == {"engineer", "set", "set_at", "in_list"}
    assert c.post("/api/engineer-homes/radius", json={"metres": 175}).json()["radius_m"] == 175
    assert c.post("/api/engineer-homes/radius", json={"metres": 20}).status_code == 400
    assert c.post("/api/engineer-homes/radius", json={"metres": "lots"}).status_code == 400
    assert app.j.homes.radius_m == 175
    r = c.delete("/api/engineer-homes/Dan%20Harper")
    assert r.status_code == 200 and r.json()["removed"] is True and app.j.db.engineer_homes_set() == []
    c.post("/api/engineer-homes", json={"engineer": "Dan Harper", "postcode": POSTCODE})
    c.post("/api/engineer-homes", json={"engineer": "Priya Shah", "postcode": POSTCODE})
    assert c.delete("/api/engineer-homes").json()["removed"] == 2 and app.j.db.engineer_homes_set() == []


def test_bad_requests_get_a_clear_message_that_never_repeats_what_was_typed(app):
    c = app.owner()
    r = c.post("/api/engineer-homes", json={"engineer": "Dan Harper", "postcode": "ZZZ 999 secret"})
    assert r.status_code == 400 and "UK postcode" in r.json()["detail"] and "secret" not in r.text and "ZZZ" not in r.text
    r = c.post("/api/engineer-homes", json={"engineer": "Nobody", "postcode": POSTCODE})
    assert r.status_code == 404 and "BD16" not in r.text
    for body in ({"engineer": 5, "postcode": ["SW1A 2AA"]}, {"postcode": {"SW1A 2AA": 1}}, ["SW1A 2AA"], "SW1A 2AA", None):
        r = c.post("/api/engineer-homes", content=json.dumps(body), headers={"content-type": "application/json"})
        assert r.status_code in (400, 404) and "SW1A" not in r.text, body                # no pydantic 422 echoing the input
    r = c.post("/api/engineer-homes", content=b"{not json SW1A 2AA", headers={"content-type": "application/json"})
    assert r.status_code in (400, 404) and "SW1A" not in r.text
    assert app.j.db.engineer_homes_set() == []


def test_a_postcode_service_outage_is_a_502_with_nothing_saved(app):
    app.j.homes.http = raising(httpx.ConnectTimeout("t"))
    r = app.owner().post("/api/engineer-homes", json={"engineer": "Dan Harper", "postcode": POSTCODE})
    assert r.status_code == 502 and "nothing was saved" in r.json()["detail"] and "BD16" not in r.text
    assert app.j.db.engineer_homes_set() == []


def test_changes_need_a_click_from_the_console(app):
    c = app.owner()
    evil = {"origin": "https://evil.example"}
    assert c.post("/api/engineer-homes", json={"engineer": "Dan Harper", "postcode": POSTCODE}, headers=evil).status_code == 403
    assert c.post("/api/engineer-homes/radius", json={"metres": 80}, headers=evil).status_code == 403
    c.post("/api/engineer-homes", json={"engineer": "Dan Harper", "postcode": POSTCODE})
    assert c.delete("/api/engineer-homes/Dan%20Harper", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert c.delete("/api/engineer-homes", headers=evil).status_code == 403
    assert len(app.j.db.engineer_homes_set()) == 1 and app.j.homes.radius_m == 100


def test_the_postcode_and_the_point_are_in_no_response_no_log_and_no_page(app, caplog):
    caplog.set_level("DEBUG")
    c = app.owner()
    bodies = [r.text for r in (
        c.post("/api/engineer-homes", json={"engineer": "Dan Harper", "postcode": "bd16  1aa"}),
        c.get("/api/engineer-homes"), c.post("/api/engineer-homes/radius", json={"metres": 130}),
        c.get("/api/status"), c.get("/api/settings"), c.get("/api/memory"), c.get("/api/tracking"), c.get("/api/me"),
        c.get("/"), c.delete("/api/engineer-homes/Nobody"),
        c.post("/api/engineer-homes", json={"engineer": "Dan Harper", "postcode": "nope"}))]
    team = app.team()
    bodies += [team.get("/api/status").text, team.get("/api/tracking").text, team.get("/").text]
    seen = "\n".join(bodies) + "\n" + caplog.text
    for needle in ("BD16", "BD161AA", "53.9123", "1.6543", "53.912345", "1.654321"):
        assert needle.lower() not in seen.lower(), needle                      # responses, pages and logs: neither postcode nor point
    assert "bd16" not in dump(app.j.db).lower() and "bd161aa" not in dump(app.j.db).lower()  # nor any table
    # the one place the point lives is the engineer_homes row
    assert app.j.db.query("SELECT lat, lng FROM engineer_homes") == [{"lat": HOME[0], "lng": HOME[1]}]


def test_setting_and_clearing_are_audited_in_the_activity_log_with_the_name_and_no_coordinates(app):
    c = app.owner()
    c.post("/api/engineer-homes", json={"engineer": "Dan Harper", "postcode": POSTCODE})
    c.post("/api/engineer-homes/radius", json={"metres": 120})
    c.delete("/api/engineer-homes/Dan%20Harper")
    c.post("/api/engineer-homes", json={"engineer": "Priya Shah", "postcode": POSTCODE})
    c.delete("/api/engineer-homes")
    details = [r["detail"] for r in app.j.db.check_runs_since("2000-01-01T00:00:00+00:00") if r["job_key"] == "engineer_homes"]
    assert details == ["Home point set for Dan Harper by the owner", "Home match distance set to 120 m by the owner",
                       "Home point removed for Dan Harper by the owner", "Home point set for Priya Shah by the owner",
                       "All 1 home points removed by the owner"]
    assert not any(re.search(r"\d+\.\d{3}", d) or "BD16" in d for d in details)


def test_the_settings_section_is_only_in_the_owners_page(app, monkeypatch):
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "true")
    app.settings.manager_emails = MANAGER
    manager = {"x-ms-client-principal-idp": "aad", "x-ms-client-principal-name": MANAGER}
    owner_page = " ".join(app.owner().get("/").text.split())
    assert 'id="homes-sec"' in owner_page and "Only a rounded map point is stored, not the postcode." in owner_page
    assert "It is used only to show whether a van is at home. Tell the engineer first." in owner_page
    for page in (app.anon().get("/", headers=manager).text, app.team().get("/").text):
        assert "homes-sec" not in page and "homes-list" not in page and "Engineer homes" not in page


def test_removing_an_engineer_from_the_list_prunes_the_point_through_the_scheduled_job(app):
    app.j.db.set_engineer_home("Dan Harper", *HOME, "the owner")
    app.j.db.set_engineer_home("Left The Company", *HOME, "the owner")
    import asyncio
    assert asyncio.run(app.j.homes.maintain()) == 1
    assert [r["engineer"] for r in app.j.db.engineer_homes_set()] == ["Dan Harper"]
    from jarvis.services.scheduler import build_scheduler
    app.settings.scheduler_enabled = True
    ids = {job.id for job in build_scheduler(app.j).get_jobs()}
    assert "engineer_homes_retention" in ids


# ==================================================================================== not a tool, and never exported
BRAIN_AND_SERVICE_FILES = [p for p in list((ROOT / "jarvis" / "brain").glob("*.py")) + list((ROOT / "jarvis" / "services").glob("*.py"))
                           + list((ROOT / "jarvis" / "integrations").glob("*.py")) + [ROOT / "jarvis" / "access.py"]]
ALLOWED_HOME_USERS = {"engineer_homes.py", "tracking.py", "scheduler.py", "access.py"}  # access.py: the route policy


def test_no_brain_code_service_or_integration_can_reach_the_home_points():
    """The model gets only what Tracker derives (at_home / 'home'). Only the owner routes in main.py, the tracker's matcher, the
    nightly retention job and the service itself touch the table or the EngineerHomes object - nothing a tool can call."""
    pattern = re.compile(r"engineer_homes|EngineerHomes|\bj\.homes\b|\.homes\.|self\.homes|engineer_home_points|set_engineer_home")
    offenders = {p.name: pattern.findall(p.read_text(encoding="utf-8")) for p in BRAIN_AND_SERVICE_FILES
                 if p.name not in ALLOWED_HOME_USERS and pattern.search(p.read_text(encoding="utf-8"))}
    assert offenders == {}, offenders


def test_the_only_home_related_tool_is_the_read_only_who_is_home_and_no_tool_takes_a_postcode():
    names = {t.name for t in TOOLS}
    assert {n for n in names if "home" in n} == {"who_is_home"}
    assert not any(re.search(r"home_(set|clear|point|location)|set_home|clear_home|engineer_home", n) for n in names)
    for tool in TOOLS:
        fields = set(tool.model.model_fields)
        assert not any("home" in f for f in fields), tool.name
    assert "who_is_home" not in access.TEAM_TOOLS  # unchanged: team has engineer_locations, not who_is_home


def test_the_prompt_and_tool_text_tell_the_model_never_to_say_where_anyone_lives():
    text = TOOLS_BY_NAME["who_is_home"].description
    assert "never read out or guess where anyone lives" in text and "home point" in text
    prompt = " ".join((ROOT / "jarvis" / "brain" / "prompts.py").read_text(encoding="utf-8").split())
    assert 'Say only "home", never where anyone lives' in prompt and "you are never given the point" in prompt


def test_nothing_that_dumps_tables_or_settings_exists_so_the_table_cannot_be_exported():
    """If a whole-database dump / backup / export is ever added this fails, and it must then exclude engineer_homes."""
    dumpers = re.compile(r"iterdump|sqlite_master|sqlite_schema|table_list|VACUUM INTO|\.backup\(|SELECT \* FROM engineer_homes")
    hits = {str(p.relative_to(ROOT)): dumpers.findall(p.read_text(encoding="utf-8")) for p in (ROOT / "jarvis").rglob("*.py")
            if dumpers.search(p.read_text(encoding="utf-8"))}
    assert hits == {}, hits
    src = {p.name: p.read_text(encoding="utf-8") for p in (ROOT / "jarvis").rglob("*.py")}
    users = sorted(n for n, t in src.items() if "engineer_homes" in t)
    assert users == ["access.py", "core.py", "db.py", "engineer_homes.py", "main.py", "scheduler.py", "tracking.py"] or \
        set(users) <= {"access.py", "core.py", "db.py", "engineer_homes.py", "main.py", "scheduler.py", "tracking.py"}, users


def test_the_staff_report_the_memory_book_the_status_and_the_archive_carry_none_of_it(app):
    app.j.db.set_engineer_home("Dan Harper", *HOME, "the owner")
    c = app.owner()
    for path in ("/api/status", "/api/memory", "/api/settings", "/api/transcript", "/api/issues", "/api/digests", "/report",
                 "/api/staff-report-address"):
        r = c.get(path)
        assert "53.9123" not in r.text and "1.6543" not in r.text and "engineer_homes" not in r.text, path
    assert "53.9123" not in json.dumps(app.app.state.j.__dict__.get("memory_book", ""), default=str)
    upload = TOOLS_BY_NAME["archive_to_azure"]
    assert "homes" not in upload.description.lower()


def test_claude_md_documents_the_data_and_how_to_purge_it():
    text = " ".join((ROOT / "CLAUDE.md").read_text(encoding="utf-8").split())
    assert "Engineer home points" in text and "DELETE FROM engineer_homes" in text and "engineer_homes_retention" in text
    assert "never a postcode" in text and "NOT a tool" in text
