"""Van locations outside working hours: the owner's Off / On-call only / Always setting, the on-call roster, the
look-up log, and the wording the model is given. Working hours are faked with Tracker.in_working_hours so none of
this depends on the clock."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jarvis import auth
from jarvis.brain.prompts import van_policy
from jarvis.brain.tools import (TOOLS_BY_NAME, LocationLogIn, OnCallAddIn, OnCallRemoveIn, dispatch,
                                location_lookup_log)
from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.db import Database
from jarvis.main import create_app
from jarvis.services.oncall import OnCallRoster, name_matches, parse_when
from jarvis.services.tracking import Tracker, requester_label
from jarvis.settings_store import FIELDS, OWNER_ONLY_KEYS, SettingsStore
from tests.fakes import FakeClient, message, text_block, tool_block


# ---------------------------------------------------------------- fakes
def _now(minutes_ago=0):
    return (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()


class RealFSM:
    demo = False  # so the working-hours gate applies

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

    def __init__(self, vehicles):
        self._vehicles = vehicles

    async def vehicles(self):
        return self._vehicles

    async def positions(self):
        return [{**v, "vehicle_id": v["id"], "speed_mph": 0} for v in self._vehicles if v.get("lat") is not None]

    async def journeys(self, vehicle_id, day):
        return []


def _van(id_, reg, driver, label, lat=53.8, lng=-1.8):
    return {"id": id_, "registration": reg, "driver": driver, "lat": lat, "lng": lng, "timestamp": _now(2),
            "moving": False, "address_label": label}


def vans():
    """Fresh vans each call. Built when a test runs, never at import: the timestamps are 'seen 2 minutes ago', and
    pytest imports every test module (collection) well before the tests run - and Jarvis() moves the process clock to
    Europe/London - so a module-level list would look over an hour stale on a UTC runner in summer."""
    return [_van(1, "SA51 LTS", "Ian Frost", "Ian Frost home"), _van(2, "YD72 SFS", "Priya Shah", "Otley Road, Ilkley")]


def not_reporting():
    return {"id": 5, "registration": "YD75 SFS", "driver": "Mo Khan", "lat": None, "lng": None,
            "timestamp": None, "moving": False, "address_label": None}


def hours(monkeypatch, working: bool):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: working))


def make(tmp_path, monkeypatch, mode, fleet=None, working=False, sites=()):
    hours(monkeypatch, working)
    s = Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None, van_locations_out_of_hours=mode)
    db = Database(tmp_path / "t.sqlite")
    return Tracker(RealFSM(sites), http=None, ram=FakeRam(list(vans() if fleet is None else fleet)), settings=s, db=db), db


def on_call(tracker, name, start_h=-1, end_h=1):
    now = datetime.now()
    tracker.roster.add(name, now + timedelta(hours=start_h), now + timedelta(hours=end_h))


def names(result):
    return [e["engineer"] for e in result["engineers"]]


# ---------------------------------------------------------------- Off (the default)
def test_the_default_is_off_and_it_is_an_owner_only_select():
    assert Settings(_env_file=None).van_locations_out_of_hours == "off"
    f = FIELDS["van_locations_out_of_hours"]
    assert f.kind == "select" and [v for v, _ in f.options] == ["off", "on_call", "always"]
    assert "van_locations_out_of_hours" in OWNER_ONLY_KEYS
    assert "company_name" not in OWNER_ONLY_KEYS


async def test_off_hides_every_tool_outside_working_hours_and_logs_nothing(tmp_path, monkeypatch):
    tracker, db = make(tmp_path, monkeypatch, "off", [*vans(), not_reporting()],
                       sites=[{"name": "Aire Valley Care Home", "lat": 53.844, "lng": -1.837}])
    live = await tracker.live("Sam")
    assert live["visible"] is False and live["working_hours"] is False and live["engineers"] == []
    assert "private use" in live["note"]
    home = await tracker.home_status("Sam")
    assert home["at_home"] == [] and home["out"] == [] and home["no_recent_position"] == []
    near = await tracker.nearest("Aire Valley", "Sam")
    assert near["engineers"] == [] and "private use" in near["note"]
    assert db.location_lookups() == []


@pytest.mark.parametrize("setting", ["", "sometimes", "ALWAYS "])
async def test_an_unknown_value_is_treated_as_off(tmp_path, monkeypatch, setting):
    tracker, db = make(tmp_path, monkeypatch, "off")
    tracker.settings.van_locations_out_of_hours = setting
    live = await tracker.live("Sam")
    # "ALWAYS " is a sloppy spelling of a real mode and is tolerated; blank and nonsense are not
    assert (live["visible"] is True) == (setting == "ALWAYS ")


async def test_a_tracker_without_settings_or_a_database_is_always_off(tmp_path, monkeypatch):
    hours(monkeypatch, False)
    no_settings = Tracker(RealFSM(), http=None, ram=FakeRam(vans()))
    assert (await no_settings.live("Sam"))["visible"] is False
    s = Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None, van_locations_out_of_hours="always")
    no_db = Tracker(RealFSM(), http=None, ram=FakeRam(vans()), settings=s)  # nowhere to keep the log
    assert (await no_db.live("Sam"))["visible"] is False


async def test_working_hours_are_unchanged_and_not_logged(tmp_path, monkeypatch):
    tracker, db = make(tmp_path, monkeypatch, "off", working=True)
    live = await tracker.live()
    assert live["working_hours"] is True and live["visible"] is True and sorted(names(live)) == [
        "Ian Frost", "Priya Shah"]
    assert "out_of_hours_access" not in live
    assert db.location_lookups() == []


# ---------------------------------------------------------------- Always
async def test_always_shows_every_van_out_of_hours_with_the_home_flag_and_logs_each_engineer(tmp_path, monkeypatch):
    tracker, db = make(tmp_path, monkeypatch, "always")
    live = await tracker.live("Sam Ward")
    assert live["visible"] is True and live["working_hours"] is False and live["out_of_hours_access"] == "always"
    assert "logged" in live["note"]
    by_name = {e["engineer"]: e for e in live["engineers"]}
    assert by_name["Ian Frost"]["at_home"] is True and by_name["Ian Frost"]["address_label"] == "home"
    assert "Ian Frost home" not in str(live)  # a home label is still only ever "home"
    assert by_name["Priya Shah"]["at_home"] is False
    rows = db.location_lookups()
    assert sorted(r["engineer"] for r in rows) == ["Ian Frost", "Priya Shah"]
    assert {(r["asked_by"], r["tool"], r["mode"]) for r in rows} == {("Sam Ward", "engineer_locations", "always")}
    assert all(r["created_at"] for r in rows)


async def test_always_without_saying_who_is_asking_shows_nothing(tmp_path, monkeypatch):
    tracker, db = make(tmp_path, monkeypatch, "always")
    for asked_by in ("", "   "):
        assert (await tracker.live(asked_by))["visible"] is False
    assert db.location_lookups() == []


async def test_always_works_for_who_is_home_and_nearest_and_logs_them(tmp_path, monkeypatch):
    tracker, db = make(tmp_path, monkeypatch, "always", [*vans(), not_reporting()],
                       sites=[{"name": "Aire Valley Care Home", "lat": 53.844, "lng": -1.837}])
    home = await tracker.home_status("Sam Ward")
    assert [e["engineer"] for e in home["at_home"]] == ["Ian Frost"]
    assert [e["engineer"] for e in home["out"]] == ["Priya Shah"]
    assert [e["engineer"] for e in home["no_recent_position"]] == ["Mo Khan"]
    assert "Ian Frost home" not in str(home)
    near = await tracker.nearest("Aire Valley", "Sam Ward")
    assert sorted(e["engineer"] for e in near["engineers"]) == ["Ian Frost", "Priya Shah"]
    rows = db.location_lookups()
    assert {r["tool"] for r in rows} == {"who_is_home", "nearest_engineer"}
    assert sorted(r["engineer"] for r in rows if r["tool"] == "who_is_home") == ["Ian Frost", "Mo Khan", "Priya Shah"]


async def test_always_van_day_shows_todays_label_out_of_hours_and_logs(tmp_path, monkeypatch):
    tracker, db = make(tmp_path, monkeypatch, "always")
    van = await tracker.van_day("Ian Frost", date.today(), "Sam Ward")
    assert van["current_address_label"] == "home" and van["at_home"] is True
    [row] = db.location_lookups()
    assert (row["asked_by"], row["tool"], row["engineer"], row["mode"]) == ("Sam Ward", "van_day", "Ian Frost", "always")


# ---------------------------------------------------------------- On-call only
async def test_on_call_shows_only_the_engineer_on_call_and_logs_only_them(tmp_path, monkeypatch):
    tracker, db = make(tmp_path, monkeypatch, "on_call")
    on_call(tracker, "Ian Frost")
    live = await tracker.live("Sam Ward")
    assert names(live) == ["Ian Frost"] and live["out_of_hours_access"] == "on_call"
    assert live["on_call"] == ["Ian Frost"] and "Priya" not in str(live)
    assert [(r["engineer"], r["mode"]) for r in db.location_lookups()] == [("Ian Frost", "on_call")]


async def test_on_call_with_nobody_on_call_hides_everything_and_says_why(tmp_path, monkeypatch):
    tracker, db = make(tmp_path, monkeypatch, "on_call")
    now = datetime.now()
    tracker.roster.add("Ian Frost", now - timedelta(hours=5), now - timedelta(hours=1))  # finished
    tracker.roster.add("Priya Shah", now + timedelta(hours=1), now + timedelta(hours=9))  # not started
    live = await tracker.live("Sam Ward")
    assert live["visible"] is False and live["engineers"] == [] and "on call" in live["note"]
    assert db.location_lookups() == []


async def test_on_call_who_is_home_and_nearest_only_name_the_engineer_on_call(tmp_path, monkeypatch):
    tracker, db = make(tmp_path, monkeypatch, "on_call", [*vans(), not_reporting()],
                       sites=[{"name": "Aire Valley Care Home", "lat": 53.844, "lng": -1.837}])
    on_call(tracker, "Ian")  # a first name is enough
    home = await tracker.home_status("Sam Ward")
    assert [e["engineer"] for e in home["at_home"]] == ["Ian Frost"]
    assert home["out"] == [] and home["no_recent_position"] == []  # Priya and Mo Khan are not named at all
    near = await tracker.nearest("Aire Valley", "Sam Ward")
    assert [e["engineer"] for e in near["engineers"]] == ["Ian Frost"]
    assert {r["engineer"] for r in db.location_lookups()} == {"Ian Frost"}


async def test_on_call_van_day_adds_the_label_only_for_the_engineer_on_call_but_always_logs(tmp_path, monkeypatch):
    tracker, db = make(tmp_path, monkeypatch, "on_call")
    on_call(tracker, "Ian Frost")
    ian = await tracker.van_day("Ian Frost", date.today(), "Sam Ward")
    priya = await tracker.van_day("Priya Shah", date.today(), "Sam Ward")
    assert ian["current_address_label"] == "home"
    assert "current_address_label" not in priya and "at_home" not in priya
    assert sorted(r["engineer"] for r in db.location_lookups()) == ["Ian Frost", "Priya Shah"]


async def test_off_van_day_adds_no_label_out_of_hours_but_the_look_up_is_still_logged(tmp_path, monkeypatch):
    tracker, db = make(tmp_path, monkeypatch, "off")
    van = await tracker.van_day("Ian Frost", date.today(), "Sam Ward")
    assert "current_address_label" not in van
    [row] = db.location_lookups()
    assert (row["engineer"], row["mode"]) == ("Ian Frost", "off")


# ---------------------------------------------------------------- the log
async def test_the_fleet_panel_refresh_is_logged_once_per_engineer_not_every_minute(tmp_path, monkeypatch):
    tracker, db = make(tmp_path, monkeypatch, "always")
    for _ in range(3):
        await tracker.live("Alex (display)", tool="fleet_panel")
    assert len(db.location_lookups()) == 2
    await tracker.live("Sam Ward")  # a chat look-up is never collapsed
    await tracker.live("Sam Ward")
    assert len(db.location_lookups()) == 2 + 4


async def test_nothing_is_shown_if_the_look_up_cannot_be_logged(tmp_path, monkeypatch):
    tracker, db = make(tmp_path, monkeypatch, "always")

    def broken(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "log_location_lookup", broken)
    with pytest.raises(RuntimeError):
        await tracker.live("Sam Ward")


async def test_the_log_tool_lists_who_asked_when_and_which_engineer(tmp_path, monkeypatch):
    tracker, db = make(tmp_path, monkeypatch, "always")
    await tracker.live("Sam Ward")
    j = SimpleNamespace(db=db)
    out = await location_lookup_log(j, LocationLogIn())
    assert {(r["asked_by"], r["engineer"]) for r in out["lookups"]} == {("Sam Ward", "Ian Frost"),
                                                                       ("Sam Ward", "Priya Shah")}
    assert TOOLS_BY_NAME["location_lookup_log"].approval is False


def test_requester_label():
    s = SimpleNamespace(owner_name="Alex")
    assert requester_label(s, "Sam Ward") == "Sam Ward"
    assert requester_label(s, None) == "Alex (display)"
    assert requester_label(s, None, quiet=True) == "automation"


# ---------------------------------------------------------------- the roster
def test_roster_add_list_on_call_and_remove(tmp_path):
    roster = OnCallRoster(Database(tmp_path / "r.sqlite"))
    day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)  # relative, so it never goes stale
    now = day + timedelta(hours=22)
    roster.add("Ian Frost", day + timedelta(hours=17, minutes=30), day + timedelta(days=1, hours=7))
    roster.add("Priya Shah", day + timedelta(days=1, hours=17, minutes=30), day + timedelta(days=2, hours=7))
    assert roster.on_call(now) == ["Ian Frost"]
    assert roster.on_call(day + timedelta(days=1, hours=7)) == []  # the end is exclusive
    assert roster.on_call(day + timedelta(days=1, hours=18)) == ["Priya Shah"]
    assert [e["engineer"] for e in roster.entries(now)] == ["Ian Frost", "Priya Shah"]
    assert roster.remove("ian frost") == 1 and roster.remove("Nobody") == 0
    assert roster.on_call(now) == []


def test_roster_rejects_bad_periods_and_names_match_whole_words():
    roster = OnCallRoster(SimpleNamespace(get_kv=lambda k: None, set_kv=lambda k, v: None))
    t = datetime(2026, 10, 2, 17, 0)
    with pytest.raises(ValueError):
        roster.add("Ian", t, t)
    with pytest.raises(ValueError):
        roster.add("Ian", t, t + timedelta(days=40))
    with pytest.raises(ValueError):
        roster.add("  ", t, t + timedelta(hours=1))
    with pytest.raises(ValueError):
        parse_when("next Tuesday")
    assert name_matches("Ian", "Ian Frost") and name_matches("Ian Frost", "ian")
    assert not name_matches("Ian", "Christian Smith") and not name_matches("", "Ian") and not name_matches("Ian", None)


async def test_roster_changes_are_approval_gated_and_validated_at_the_door(tmp_path):
    j = Jarvis(Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None, anthropic_api_key="test"),
               client=FakeClient())
    add, remove = TOOLS_BY_NAME["oncall_add"], TOOLS_BY_NAME["oncall_remove"]
    assert add.approval is True and remove.approval is True and add.describe and remove.describe
    assert TOOLS_BY_NAME["oncall_roster"].approval is False
    args = OnCallAddIn(engineer="Ian Frost", start="2026-10-02 17:30", end="2026-10-03T07:00")
    assert (args.start, args.end) == ("2026-10-02 17:30", "2026-10-03 07:00")
    reply = await dispatch(j, add, args)
    assert "queued as action" in reply and j.oncall.entries(datetime(2026, 10, 2)) == []  # nothing changed yet
    await add.handler(j, args)  # what the approval runs
    assert [e["engineer"] for e in j.oncall.entries(datetime(2026, 10, 2))] == ["Ian Frost"]
    await remove.handler(j, OnCallRemoveIn(engineer="Ian Frost"))
    assert j.oncall.entries(datetime(2026, 10, 2)) == []
    for bad in ({"engineer": "Ian", "start": "tomorrow", "end": "2026-10-03 07:00"},
                {"engineer": "Ian", "start": "2026-10-03 07:00", "end": "2026-10-02 17:30"}):
        with pytest.raises(ValidationError):
            OnCallAddIn(**bad)
    await j.http.aclose()


# ---------------------------------------------------------------- who is asking, through a real chat turn
async def test_a_chat_turn_logs_the_named_speaker_and_clears_it_afterwards(tmp_path, monkeypatch):
    hours(monkeypatch, False)
    s = Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None, anthropic_api_key="test",
                 van_locations_out_of_hours="always")
    client = FakeClient([message([tool_block("engineer_locations", {})], "tool_use"),
                         message([text_block("Ian and Priya are both out.")])])
    j = Jarvis(s, client=client)
    j.tracker.fsm, j.tracker.ram = RealFSM(), FakeRam(vans())
    await j.brain.ask("Where are the vans?", "typed", speaker="Sam Ward")
    rows = j.db.location_lookups()
    assert sorted(r["engineer"] for r in rows) == ["Ian Frost", "Priya Shah"]
    assert {r["asked_by"] for r in rows} == {"Sam Ward"} and j.asked_by == ""
    await j.http.aclose()


async def test_a_tool_called_outside_any_turn_cannot_see_out_of_hours_positions(tmp_path, monkeypatch):
    hours(monkeypatch, False)
    s = Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None, anthropic_api_key="test",
                 van_locations_out_of_hours="always")
    j = Jarvis(s, client=FakeClient())
    j.tracker.fsm, j.tracker.ram = RealFSM(), FakeRam(vans())
    out = await TOOLS_BY_NAME["engineer_locations"].handler(j, TOOLS_BY_NAME["engineer_locations"].model())
    assert out["visible"] is False and j.db.location_lookups() == []
    await j.http.aclose()


# ---------------------------------------------------------------- the Settings page and the Fleet panel
def test_the_setting_validates_and_applies_live(settings):
    store = SettingsStore(settings)
    assert store.validate("van_locations_out_of_hours", "maybe")[1]
    assert store.update({"van_locations_out_of_hours": "on_call"}, []) == {}
    assert settings.van_locations_out_of_hours == "on_call"
    store.update({}, ["van_locations_out_of_hours"])
    assert settings.van_locations_out_of_hours == "off"


def _app(tmp_path, monkeypatch, **kw):
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "true")
    s = Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None, anthropic_api_key="test",
                 owner_email="alex@salts.example.com", partner_email="sam@salts.example.com",
                 manager_emails="alex@salts.example.com,sam@salts.example.com", jarvis_owner_password="a-long-password",
                 **kw)
    return s, Jarvis(s, client=FakeClient())


def test_only_the_owner_can_change_the_setting(tmp_path, monkeypatch):
    s, j = _app(tmp_path, monkeypatch)
    sso = lambda who: {"x-ms-client-principal-idp": "aad", "x-ms-client-principal-name": who}  # noqa: E731
    with TestClient(create_app(s, j)) as c:
        for body in ({"values": {"van_locations_out_of_hours": "always"}},
                     {"values": {}, "clear": ["van_locations_out_of_hours"]}):
            assert c.post("/api/settings", json=body, headers=sso("sam@salts.example.com")).status_code == 403
        assert s.van_locations_out_of_hours == "off"
        assert c.post("/api/settings", json={"values": {"van_locations_out_of_hours": "always"}}).status_code == 401
        r = c.post("/api/settings", json={"values": {"van_locations_out_of_hours": "on_call"}},
                   headers=sso("alex@salts.example.com"))
        assert r.status_code == 200 and s.van_locations_out_of_hours == "on_call"


def test_the_fleet_panel_works_out_of_hours_when_allowed_and_logs_the_signed_in_person(tmp_path, monkeypatch):
    hours(monkeypatch, False)
    s, j = _app(tmp_path, monkeypatch, van_locations_out_of_hours="always")
    j.tracker.fsm, j.tracker.ram = RealFSM(), FakeRam(vans())
    sso = {"x-ms-client-principal-idp": "aad", "x-ms-client-principal-name": "sam@salts.example.com"}
    with TestClient(create_app(s, j)) as c:
        data = c.get("/api/tracking", headers=sso).json()
    assert data["visible"] is True and sorted(e["engineer"] for e in data["engineers"]) == ["Ian Frost", "Priya Shah"]
    rows = j.db.location_lookups()
    assert {r["tool"] for r in rows} == {"fleet_panel"} and len(rows) == 2
    assert {r["asked_by"] for r in rows} == {s.person("sam@salts.example.com")}


def test_the_fleet_panel_stays_empty_out_of_hours_when_off(tmp_path, monkeypatch):
    hours(monkeypatch, False)
    s, j = _app(tmp_path, monkeypatch)
    j.tracker.fsm, j.tracker.ram = RealFSM(), FakeRam(vans())
    with TestClient(create_app(s, j)) as c:
        c.cookies.set(auth.COOKIE, auth.make_session(s))
        data = c.get("/api/tracking").json()
    assert data["visible"] is False and data["engineers"] == [] and j.db.location_lookups() == []


# ---------------------------------------------------------------- the wording the model is given
def test_the_prompt_says_hidden_only_when_the_setting_is_off(tmp_path):
    def text(mode):
        s = Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None, van_locations_out_of_hours=mode)
        return van_policy(s)

    assert "private use" in text("off") and "say so plainly" in text("off")
    assert text("bogus") == text("off")
    assert "ON CALL" in text("on_call") and "isn't shown outside working hours" in text("on_call")
    assert "any hour" in text("always") and "private use" not in text("always")
    assert "hidden" in text("always") and "don't tell anyone" in text("always")


def test_the_live_system_prompt_follows_the_setting(tmp_path):
    def system(mode):
        s = Settings(data_dir=tmp_path / mode, scheduler_enabled=False, _env_file=None, anthropic_api_key="test",
                     van_locations_out_of_hours=mode)
        return "".join(b["text"] for b in Jarvis(s, client=FakeClient()).brain.system)

    off, always = system("off"), system("always")
    assert "# Van locations outside working hours" in off and "private use" in off
    assert "private use" not in always and "at any hour" in always
