"""RAM address labels on the tracker: at_home flag, who's home/out/no position, redaction of home labels,
the no-label path, and the van mismatch warnings (same driver on two vans, journeys vs live position)."""

from datetime import date, datetime, timedelta

import httpx

from jarvis.brain.tools import TOOLS_BY_NAME
from jarvis.config import Settings
from jarvis.integrations.ramtracking import RamTracking, _vehicle_row
from jarvis.services.tracking import Tracker, label_status

SETTINGS = dict(ram_client_id="c", ram_api_key="k", ram_username="u", ram_password="p", _env_file=None)


def _now(minutes_ago=0):
    return (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()


class FakeFSM:
    demo = True  # bypasses the working-hours gate so the tests don't depend on the clock

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


def _van(id_, reg, driver, label, minutes_ago=2, lat=53.8, lng=-1.8):
    return {"id": id_, "registration": reg, "driver": driver, "lat": lat, "lng": lng,
            "timestamp": _now(minutes_ago), "moving": False, "address_label": label}


class FakeRegister:
    def __init__(self, people):
        self.people_list = people

    def find(self, name):
        return next((p for p in self.people_list if name.lower() in p["name"].lower()), None)

    def people(self, kind=None):
        return self.people_list


# ---------------------------------------------------------------- label_status
def test_label_status_flags_the_word_home_case_insensitively_and_redacts_it():
    assert label_status("Ian Frost HOME", []) == ("home", True)
    assert label_status("14 Mill Lane, Bingley - home", []) == ("home", True)  # never echo the address part
    assert label_status("Otley Road, Ilkley", []) == ("Otley Road, Ilkley", False)
    assert label_status("Homerton Road", []) == ("Homerton Road", False)  # a word match, not a substring match


def test_label_status_without_a_label_is_unknown_not_away():
    assert label_status(None, []) == (None, None)
    assert label_status("   ", []) == (None, None)


def test_label_status_does_not_treat_a_care_home_site_as_somebodys_house():
    assert label_status("Aire Valley Care Home, Otley Rd", ["Aire Valley Care Home"]) == (
        "Aire Valley Care Home, Otley Rd", False)


# ---------------------------------------------------------------- RAM connector
def test_vehicle_row_reads_the_address_label_when_ram_supplies_one():
    v = {"id": 1, "registration": "SA51 LTS", "vehicle_status": {"event_date": "2026-10-01T09:00:00Z",
         "location": {"latitude": 53.8, "longitude": -1.8, "formattedAddress": "Ian Frost home"}}}
    assert _vehicle_row(v)["address_label"] == "Ian Frost home"


def test_vehicle_row_has_no_label_when_ram_supplies_none():
    v = {"id": 1, "registration": "SA51 LTS", "vehicle_status": {"location": {"latitude": 53.8, "longitude": -1.8}}}
    assert _vehicle_row(v)["address_label"] is None


async def test_positions_carry_the_label_through():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 28799})
        return httpx.Response(200, json=[{"id": 1, "registration": "SA51 LTS", "vehicle_driver": {"name": "Ian Frost"},
                                           "vehicle_status": {"location": {"latitude": 53.8, "longitude": -1.8,
                                                                            "formattedAddress": "Ian Frost home"}}}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        positions = await RamTracking(Settings(**SETTINGS), http).positions()
    assert positions[0]["address_label"] == "Ian Frost home"


# ---------------------------------------------------------------- live / nearest
async def test_live_shows_label_and_at_home_and_redacts_home_addresses():
    ram = FakeRam([_van(1, "SA51 LTS", "Ian Frost", "Ian Frost home"),
                   _van(2, "YD72 SFS", "Priya Shah", "Otley Road, Ilkley")])
    live = await Tracker(FakeFSM(), http=None, ram=ram).live()
    by_name = {e["engineer"]: e for e in live["engineers"]}
    assert by_name["Ian Frost"]["address_label"] == "home" and by_name["Ian Frost"]["at_home"] is True
    assert by_name["Priya Shah"]["address_label"] == "Otley Road, Ilkley" and by_name["Priya Shah"]["at_home"] is False
    assert "address_label_note" not in live


async def test_live_says_plainly_when_ram_supplies_no_label():
    ram = FakeRam([_van(1, "SA51 LTS", "Ian Frost", None)])
    live = await Tracker(FakeFSM(), http=None, ram=ram).live()
    row = live["engineers"][0]
    assert row["address_label"] is None and row["at_home"] is None
    assert "Ian Frost" in live["address_label_note"] and "didn't supply" in live["address_label_note"]


async def test_live_flags_one_engineer_on_two_vans():
    ram = FakeRam([_van(1, "AB12 XYZ", "Ian Frost", "Otley Road"), _van(2, "SA51 LTS", "Ian Frost", "Ian Frost home")])
    live = await Tracker(FakeFSM(), http=None, ram=ram).live()
    assert len(live["warnings"]) == 1
    assert "Ian Frost" in live["warnings"][0] and "AB12 XYZ" in live["warnings"][0] and "SA51 LTS" in live["warnings"][0]


async def test_nearest_includes_label_and_home_flag():
    ram = FakeRam([_van(1, "SA51 LTS", "Ian Frost", "Ian Frost home"), _van(2, "YD72 SFS", "Priya Shah", None)])
    fsm = FakeFSM([{"name": "Aire Valley Care Home", "lat": 53.844, "lng": -1.837}])
    near = await Tracker(fsm, http=None, ram=ram).nearest("Aire Valley")
    by_name = {e["engineer"]: e for e in near["engineers"]}
    assert by_name["Ian Frost"]["address_label"] == "home" and by_name["Ian Frost"]["at_home"] is True
    assert by_name["Priya Shah"]["address_label"] is None and by_name["Priya Shah"]["at_home"] is None


# ---------------------------------------------------------------- who is home
async def test_home_status_splits_home_out_no_label_and_no_recent_position():
    ram = FakeRam([
        _van(1, "SA51 LTS", "Ian Frost", "Ian Frost home"),
        _van(2, "YD72 SFS", "Priya Shah", "Otley Road, Ilkley"),
        _van(3, "YD73 SFS", "Dan Harper", None),
        _van(4, "YD74 SFS", "Sam Ward", "Sam Ward home", minutes_ago=300),  # stale fix - can't say where he is now
        {"id": 5, "registration": "YD75 SFS", "driver": "Mo Khan", "lat": None, "lng": None, "timestamp": None,
         "moving": False, "address_label": None},
    ])
    res = await Tracker(FakeFSM(), http=None, ram=ram).home_status()
    assert [e["engineer"] for e in res["at_home"]] == ["Ian Frost"]
    assert [e["engineer"] for e in res["out"]] == ["Priya Shah"]
    assert [e["engineer"] for e in res["no_address_label"]] == ["Dan Harper"]
    assert sorted(e["engineer"] for e in res["no_recent_position"]) == ["Mo Khan", "Sam Ward"]
    # nothing in the answer carries more than the word 'home' for a home label
    assert "Ian Frost home" not in str(res) and "Sam Ward home" not in str(res)


async def test_home_status_shows_nothing_outside_working_hours(monkeypatch):
    class StrictFSM(FakeFSM):
        demo = False

    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: False))
    ram = FakeRam([_van(1, "SA51 LTS", "Ian Frost", "Ian Frost home")])
    res = await Tracker(StrictFSM(), http=None, ram=ram).home_status()
    assert res["working_hours"] is False
    assert res["at_home"] == [] and res["out"] == [] and res["no_recent_position"] == []


def test_who_is_home_tool_is_registered_and_read_only():
    tool = TOOLS_BY_NAME["who_is_home"]
    assert tool.approval is False


# ---------------------------------------------------------------- van_day
async def test_van_day_prefers_the_registered_van_over_another_van_with_the_same_driver_name():
    """The Ian Frost bug: an earlier van that also lists him as driver was picked, hiding the journeys of SA51 LTS."""
    legs = {"2": [{"start_time": "2026-10-01T07:05:00", "end_time": "2026-10-01T07:25:00", "start_lat": 53.8,
                   "start_lng": -1.8, "end_lat": 53.83, "end_lng": -1.78, "start_address": None,
                   "end_address": "Saltaire Court Offices", "distance_miles": 5}]}
    ram = FakeRam([_van(1, "AB12 XYZ", "Ian Frost", "Otley Road"), _van(2, "SA51 LTS", "Ian Frost", "Ian Frost home")],
                  legs)
    register = FakeRegister([{"name": "Ian Frost", "vehicle": "SA51 LTS"}])
    van = await Tracker(FakeFSM(), http=None, ram=ram, register=register).van_day("Ian", date(2026, 10, 1))
    assert van["vehicle"] == "SA51 LTS" and van["timeline"][0]["to"] == "Saltaire Court Offices"
    assert "AB12 XYZ" in van["warnings"][0]  # and the duplicate assignment is called out


async def test_van_day_flags_no_journeys_when_the_van_reported_a_position_that_day():
    vehicle = _van(2, "SA51 LTS", "Ian Frost", "Ian Frost home", minutes_ago=5)
    # "That day" is the day of the position itself: in the first five minutes after midnight a position from five minutes
    # ago is yesterday's, and comparing it with date.today() made this fail then.
    day = datetime.fromisoformat(vehicle["timestamp"]).date()
    van = await Tracker(FakeFSM(), http=None, ram=FakeRam([vehicle])).van_day("Ian Frost", day)
    assert van["summary"].startswith("No journeys recorded")
    assert any("position" in w and "no journeys" in w.lower() for w in van["warnings"])


async def test_van_day_no_journeys_and_no_fix_today_has_no_mismatch_warning():
    ram = FakeRam([_van(2, "SA51 LTS", "Ian Frost", None, minutes_ago=60 * 24 * 3)])
    van = await Tracker(FakeFSM(), http=None, ram=ram).van_day("Ian Frost", date.today())
    assert "warnings" not in van


async def test_van_day_shows_current_label_for_today_and_says_when_there_is_none():
    ram = FakeRam([_van(2, "SA51 LTS", "Ian Frost", "Ian Frost home"), _van(3, "YD73 SFS", "Dan Harper", None)])
    tracker = Tracker(FakeFSM(), http=None, ram=ram)
    ian = await tracker.van_day("Ian Frost", date.today())
    assert ian["current_address_label"] == "home" and ian["at_home"] is True
    dan = await tracker.van_day("Dan Harper", date.today())
    assert dan["current_address_label"] is None and "no address label" in dan["address_label_note"]
