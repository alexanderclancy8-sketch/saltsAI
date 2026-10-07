"""Fleet: is a van moving? (integrations/ram_motion.py, RamTracking, Tracker.live / who_is_home / van_day, Fleet diagnostics.)

The bug: every van read "parked" because "moving" meant "the LAST event is TRANSIT_START or OVER_SPEED", and a van that set off
twenty minutes ago and has since logged a HARSH_BRAKING / IDLE_END / ZONE_OUT is no longer in either. RAM's vehicle list has no
speed, so the state is now worked out from the change in position between polls, the class of the last event (only while it is
recent) and engine RPM. All of it is exercised here against a mocked RAM shaped exactly like the published VehicleDTO
(last_event a string, event_date a date-time, location nested, engineRpm on the vehicle). NONE of it has been run against RAM's
real API - there are no credentials here - so the event tables are a best reading of RAM's documented event names.

Every clock is pinned: ``now`` is passed in, or the RamTracking wall clock is a variable the test moves. Nothing reads the time.
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis import access
from jarvis.brain.tools import TOOLS_BY_NAME, dispatch, fleet_diagnostics
from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.db import Database
from jarvis.integrations import ram_motion as rm
from jarvis.integrations.ram_motion import (IDLE_EVENTS, MOVING, MOVING_EVENTS, NEUTRAL, NEUTRAL_EVENTS, STOPPED,
                                            STOPPED_EVENTS, Evidence, MotionTracker, classify, classify_event)
from jarvis.integrations.ramtracking import RamError, RamTracking
from jarvis.main import create_app
from jarvis.services.tracking import Tracker
from tests.fakes import FakeClient

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 2, 9, 0, 0, tzinfo=UTC)
M_PER_DEG_LAT = 111194.93
HOME_PT = (53.9123, -1.6543)


def at(seconds: float = 0, minutes: float = 0) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds, minutes=minutes)


def north(point, metres):
    return (point[0] + metres / M_PER_DEG_LAT, point[1])


def iso(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================================================ the event table
EXPECTED_MOVING = {
    "TRANSIT_START", "HOME_MOVING", "ROAMING_MOVING", "UNKNOWN_MOVING", "CELL_ID_MOVING", "MOVEMENT_NO_IGNITION",
    "OVER_SPEED", "UNDER_SPEED", "TRIP_START", "TRIP_START_FM", "HARSH_ACCELERATION", "HARSH_BRAKING", "HARSH_CORNERING",
    "UNKNOWN_HARSH_EVENT", "HARSH_EVENT_END", "ACCELERATION_END", "DECELERATION_END", "IDLE_END", "IDLE_END_FM",
    "FIRST_IGNITION_OF_DAY"}
EXPECTED_STOPPED = {
    "TRANSIT_STOP", "IGNITION_OFF", "IDLE_START", "IDLE_START_FM", "EXTENDED_STOP", "EXTENDED_IDLE_", "HOME_STOPPED",
    "ROAMING_STOPPED", "UNKNOWN_STOPPED", "STATIONARY_NO_IGNITION", "CELL_ID_NOT_MOVING", "TRIP_END", "TRIP_END_FM",
    "TRAILER_DISCONNECTED_PARKED"}
EXPECTED_NEUTRAL = {
    "ZONE_OUT", "GEOFENCE_OUT", "LCZ_OUT", "CUSTOM_OUT", "TOLL_OUT", "DATA_CONNECTION_REPORT", "DEVICE_STATUS",
    "INFORMATION_REPORT", "LOCATION_REPLY", "MANUEL_LOCATION_UPDATE", "GPS_LOST", "DRIVER_ALLOCATED"}


def test_the_event_tables_are_the_agreed_ones_and_do_not_overlap():
    assert set(MOVING_EVENTS) == EXPECTED_MOVING
    assert EXPECTED_STOPPED <= set(STOPPED_EVENTS) and set(STOPPED_EVENTS) - EXPECTED_STOPPED == {"EXTENDED_IDLE"}  # + the spelling without "_"
    assert set(NEUTRAL_EVENTS) == EXPECTED_NEUTRAL
    assert not (MOVING_EVENTS & STOPPED_EVENTS) and not (MOVING_EVENTS & NEUTRAL_EVENTS) and not (STOPPED_EVENTS & NEUTRAL_EVENTS)
    assert IDLE_EVENTS <= STOPPED_EVENTS


@pytest.mark.parametrize("name", sorted(EXPECTED_MOVING))
def test_every_moving_event_is_classified_moving(name):
    assert classify_event(name) == MOVING


@pytest.mark.parametrize("name", sorted(EXPECTED_STOPPED))
def test_every_stopped_event_is_classified_stopped(name):
    assert classify_event(name) == STOPPED


@pytest.mark.parametrize("name", sorted(EXPECTED_NEUTRAL) + ["SOME_EVENT_RAM_ADDS_NEXT_YEAR", "", None, 42, "  "])
def test_neutral_and_unknown_events_say_nothing_either_way(name):
    assert classify_event(name) == NEUTRAL


def test_event_names_are_read_ignoring_case_and_space():
    assert classify_event(" harsh_braking ") == MOVING and classify_event("Ignition_Off") == STOPPED


# ============================================================================================ the verdict (pure)
def verdict(event, minutes_ago, rpm=None, ev=None, has_position=True):
    when = at() - dt.timedelta(minutes=minutes_ago) if minutes_ago is not None else None
    return classify(event, when, rpm, ev or Evidence("first"), at(), has_position)


def test_a_recent_moving_event_is_moving_with_no_speed_claimed():
    v = verdict("TRANSIT_START", 3)
    assert v["state"] == "moving" and v["label"] == "Moving" and v["moving"] is True
    assert v["speed_mph"] is None and v["speed_estimated"] is False  # RAM sends no speed: none is made up
    assert "TRANSIT_START" in v["reason"] and "3 min ago" in v["reason"]


@pytest.mark.parametrize("event", ["HARSH_BRAKING", "IDLE_END", "OVER_SPEED", "UNDER_SPEED", "FIRST_IGNITION_OF_DAY",
                                   "HARSH_EVENT_END", "TRIP_START"])
def test_the_events_that_follow_a_set_off_still_read_as_moving_while_recent(event):
    """The reported bug: after TRANSIT_START the van logs these, and the old rule called it parked."""
    assert verdict(event, 2)["state"] == "moving"


def test_a_moving_event_older_than_the_recency_limit_is_never_shown_as_live_motion():
    v = verdict("TRANSIT_START", 20)
    assert v["state"] == "no_position" and v["label"] == "No recent position (last seen 20 min ago)" and v["moving"] is False
    assert verdict("TRANSIT_START", rm.EVENT_FRESH_MIN)["state"] == "moving"            # the limit itself is still fresh
    assert verdict("TRANSIT_START", rm.EVENT_FRESH_MIN + 1)["state"] == "no_position"   # one minute past it is not
    assert "older than 15 min" in v["reason"]


@pytest.mark.parametrize("event", sorted(EXPECTED_STOPPED - {"IDLE_START", "IDLE_START_FM", "EXTENDED_IDLE_"}))
def test_a_stopped_event_is_parked_and_stays_parked_when_old(event):
    assert verdict(event, 2)["label"] == "Parked"
    assert verdict(event, 14 * 60)["label"] == "Parked"  # parked overnight is still parked, not "no position"


def test_ignition_off_ignores_a_stale_engine_rpm():
    assert verdict("IGNITION_OFF", 1, rpm=900)["label"] == "Parked"


@pytest.mark.parametrize("event", ["IDLE_START", "IDLE_START_FM", "EXTENDED_IDLE_"])
def test_an_idling_event_is_stopped_with_the_engine_on(event):
    assert verdict(event, 10)["label"] == "Stopped, engine on"
    assert verdict(event, 90)["label"] == "Parked"  # an hour-old idle claim is not repeated as fact


def test_a_neutral_event_with_engine_rpm_is_engine_on_not_parked():
    v = verdict("ZONE_OUT", 4, rpm=850)
    assert v["state"] == "engine_on" and v["label"] == "Stopped, engine on" and v["moving"] is False
    assert "RPM 850" in v["reason"]
    assert verdict("DEVICE_STATUS", 4, rpm=0)["label"] == "Parked"
    assert verdict("DEVICE_STATUS", 4, rpm=None)["label"] == "Parked"


def test_engine_rpm_alone_never_says_moving_and_a_stale_one_is_not_believed():
    assert verdict("GEOFENCE_OUT", 4, rpm=3000)["moving"] is False
    assert verdict("GEOFENCE_OUT", 30, rpm=3000)["label"] == "Parked"  # the event (and its RPM) is half an hour old
    assert verdict("GEOFENCE_OUT", 4, rpm=True)["label"] == "Parked"   # a bool is not a number of revolutions
    assert verdict("GEOFENCE_OUT", 4, rpm="900")["label"] == "Parked"  # nor is text


def test_a_neutral_event_older_than_an_hour_says_nothing_about_now():
    v = verdict("ZONE_OUT", 95)
    assert v["state"] == "no_position" and v["label"] == "No recent position (last seen 95 min ago)"
    assert verdict("ZONE_OUT", 300)["label"] == "No recent position (last seen 5 h ago)"


def test_an_unknown_event_name_is_neutral():
    assert verdict("BRAND_NEW_EVENT", 3, rpm=700)["label"] == "Stopped, engine on"
    assert verdict("BRAND_NEW_EVENT", 3)["label"] == "Parked"


def test_without_an_event_time_nothing_is_claimed_from_the_event():
    v = verdict("TRANSIT_START", None)
    assert v["state"] == "no_position" and "no event time" in v["label"] and v["event_age_min"] is None


def test_an_event_dated_in_the_future_is_age_zero_not_negative():
    v = classify("TRANSIT_START", at(minutes=2), None, Evidence("first"), at())
    assert v["event_age_min"] == 0 and v["state"] == "moving"


def test_no_position_at_all():
    v = verdict("TRANSIT_START", 1, has_position=False)
    assert v["state"] == "no_position" and v["moving"] is False


def test_observed_movement_beats_the_event_and_carries_an_estimated_speed():
    ev = Evidence("moved", True, 30.4, "moved 600 m in 45 s")
    v = verdict("IGNITION_OFF", 5, ev=ev)          # the event says stopped, but we SAW it move
    assert v["state"] == "moving" and v["label"] == "Moving (about 30 mph)"
    assert v["speed_mph"] == 30 and v["speed_estimated"] is True and v["reason"] == "moved 600 m in 45 s"
    assert verdict("ZONE_OUT", None, ev=ev)["label"] == "Moving (about 30 mph)"  # even with no event time at all


def test_estimated_speeds_are_rounded_to_five_mph():
    assert [rm.round_speed(x) for x in (2.1, 7.4, 12.6, 29.9, 33.0)] == [5, 5, 15, 30, 35]


# ============================================================================================ position change
def sample(m: MotionTracker, when, point, event=None, key="v1", gps_ok=True) -> Evidence:
    return m.observe(key, point[0], point[1], event, when, gps_ok)


def test_the_first_reading_has_nothing_to_compare_with_and_is_not_movement():
    ev = sample(MotionTracker(), at(), HOME_PT)
    assert ev.kind == "first" and ev.moving is False and "first reading" in ev.reason


def test_moving_between_two_polls_is_movement_with_an_estimated_speed():
    m = MotionTracker()
    sample(m, at(), HOME_PT)
    ev = sample(m, at(40), north(HOME_PT, 180))
    assert ev.kind == "moved" and ev.moving is True and ev.reason == "moved 180 m in 40 s"
    assert ev.speed_mph == pytest.approx(180 / 40 * rm.MPS_TO_MPH, rel=0.01)


@pytest.mark.parametrize("metres", [0, 10, 20, 49])
def test_gps_jitter_of_a_few_metres_is_not_movement(metres):
    m = MotionTracker()
    sample(m, at(), HOME_PT)
    ev = sample(m, at(60), north(HOME_PT, metres))
    assert ev.moving is False and ev.kind in ("unchanged", "still", "jitter")
    if metres:
        assert "GPS drift" in ev.reason


def test_just_over_the_threshold_is_movement_and_drift_does_not_accumulate_into_it():
    m = MotionTracker()
    sample(m, at(), HOME_PT)
    assert sample(m, at(45), north(HOME_PT, 60)).moving is True
    m = MotionTracker()
    here = HOME_PT
    for n in range(1, 6):  # 18 m of drift every minute for five minutes: each step is drift, none is a journey
        here = north(here, 18)
        assert sample(m, at(60 * n), here).moving is False


def test_a_slow_creep_over_a_long_gap_is_not_a_van_moving():
    """60 m in 4.5 minutes is 0.5 mph: more than 50 m, but nowhere near a vehicle's pace."""
    m = MotionTracker()
    sample(m, at(), HOME_PT)
    ev = sample(m, at(270), north(HOME_PT, 60))
    assert ev.moving is False


def test_an_impossible_jump_is_ignored_and_the_new_point_becomes_the_baseline():
    m = MotionTracker()
    sample(m, at(), HOME_PT)
    ev = sample(m, at(40), north(HOME_PT, 20_000))  # 20 km in 40 s = 1100 mph
    assert ev.kind == "jump" and ev.moving is False and "bad fix" in ev.reason
    assert sample(m, at(100), north(HOME_PT, 20_010)).moving is False  # compared with the NEW point, not the old one


def test_readings_less_than_thirty_seconds_apart_are_not_compared():
    m = MotionTracker()
    sample(m, at(), HOME_PT)
    ev = sample(m, at(10), north(HOME_PT, 400))
    assert ev.moving is False and "too soon" in ev.reason
    assert sample(m, at(50), north(HOME_PT, 400)).moving is True   # 50 s after the baseline, which was kept


def test_a_baseline_older_than_the_window_is_replaced_not_compared():
    m = MotionTracker()
    sample(m, at(), HOME_PT)
    ev = sample(m, at(minutes=20), north(HOME_PT, 5_000))
    assert ev.kind == "gap" and ev.moving is False


def test_the_same_cached_list_read_twice_is_no_new_evidence():
    m = MotionTracker()
    start = north(HOME_PT, 0)
    sample(m, at(), start, event=at())
    sample(m, at(50), north(start, 200), event=at())
    again = sample(m, at(55), north(start, 200), event=at())  # the cached answer, handed out again
    assert again.kind == "held" and again.moving is True      # inside the hold, still counted
    later = sample(m, at(200), north(start, 200), event=at())
    assert later.moving is False and later.kind == "unchanged"


def test_a_van_that_has_just_stopped_at_the_lights_stays_moving_for_a_short_hold_only():
    m = MotionTracker()
    here = HOME_PT
    sample(m, at(), here)
    here = north(here, 300)
    assert sample(m, at(40), here).moving is True
    held = sample(m, at(100), here)                 # 60 s later, no further movement
    assert held.moving is True and held.kind == "held"
    assert sample(m, at(160), here).moving is False  # 120 s after the last movement: stopped


def test_a_poor_gps_fix_is_neither_movement_nor_proof_of_staying_put():
    m = MotionTracker()
    sample(m, at(), HOME_PT)
    ev = sample(m, at(40), north(HOME_PT, 500), gps_ok=False)
    assert ev.kind == "poor_gps" and ev.moving is False
    assert sample(m, at(80), north(HOME_PT, 500)).moving is True  # the baseline was not replaced by the poor fix


def test_vehicles_are_tracked_separately():
    m = MotionTracker()
    sample(m, at(), HOME_PT, key="a")
    sample(m, at(), north(HOME_PT, 9000), key="b")
    assert sample(m, at(40), north(HOME_PT, 200), key="a").moving is True
    assert sample(m, at(40), north(HOME_PT, 9000), key="b").moving is False


def test_bad_coordinates_are_no_position():
    m = MotionTracker()
    assert m.observe("v", None, None, None, at()).kind == "none"
    assert m.observe("v", "x", 1, None, at()).kind == "none"
    assert m.observe("v", 95, 0, None, at()).kind == "none"
    assert m.observe("v", float("nan"), 0, None, at()).kind == "none"


# ============================================================================================ saved history
class Store:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.writes = 0

    def get_kv(self, key, default=None):
        return self.kv.get(key, default)

    def set_kv(self, key, value):
        self.kv[key] = value
        self.writes += 1


def rows_for(point, event="ZONE_OUT", when=None, vid=1):
    return [{"id": vid, "registration": "YD71 SFS", "lat": point[0], "lng": point[1],
             "timestamp": iso(when or at()), "event": event, "engine_rpm": None, "gps_ok": True}]


def test_a_van_seen_moving_survives_a_restart_so_the_next_poll_can_still_compare():
    store = Store()
    first = MotionTracker(store)
    first.apply(rows_for(HOME_PT), at())
    first.apply(rows_for(north(HOME_PT, 300)), at(40))
    assert json.loads(store.kv[rm.PERSIST_KEY]).keys() == {"1"}
    restarted = MotionTracker(store)
    row = restarted.apply(rows_for(north(HOME_PT, 600)), at(80))[0]
    assert row["motion"]["position_evidence"] == "moved" and row["moving"] is True


def test_a_parked_vans_point_is_never_written_to_the_database():
    """At night the van's point is somebody's home: only vans seen moving in the last few minutes are saved."""
    store = Store()
    m = MotionTracker(store)
    for n in range(4):
        m.apply(rows_for(HOME_PT), at(60 * n))
    assert store.kv.get(rm.PERSIST_KEY, "{}") == "{}"
    assert "53.91" not in json.dumps(store.kv)


def test_a_saved_point_is_dropped_once_it_stops_being_recent():
    store = Store()
    m = MotionTracker(store)
    m.apply(rows_for(HOME_PT), at())
    m.apply(rows_for(north(HOME_PT, 300)), at(40))
    assert json.loads(store.kv[rm.PERSIST_KEY])
    m.apply(rows_for(north(HOME_PT, 300)), at(minutes=10))   # still, ten minutes on: the next save is empty
    assert json.loads(store.kv[rm.PERSIST_KEY]) == {}
    stale = MotionTracker(store)
    store.kv[rm.PERSIST_KEY] = json.dumps({"1": {"lat": 53.0, "lng": -1.0, "at": at().isoformat(), "moved_at": at().isoformat(),
                                                 "event": None, "speed_mph": 20}})
    assert stale.apply(rows_for(HOME_PT), at(minutes=30))[0]["motion"]["position_evidence"] == "first"  # not compared with it


def test_a_broken_saved_value_or_store_is_survived():
    class Broken(Store):
        def get_kv(self, key, default=None):
            return "{not json"

        def set_kv(self, key, value):
            raise OSError("disk full")

    m = MotionTracker(Broken())
    assert m.apply(rows_for(HOME_PT), at())[0]["motion"]["position_evidence"] == "first"
    assert m.apply(rows_for(north(HOME_PT, 300)), at(40))[0]["moving"] is True


def test_an_unchanged_save_is_not_rewritten():
    store = Store()
    m = MotionTracker(store)
    m.apply(rows_for(HOME_PT), at())
    m.apply(rows_for(north(HOME_PT, 300)), at(40))
    writes = store.writes
    m.apply(rows_for(north(HOME_PT, 300)), at(41))
    assert store.writes == writes


def test_a_van_ram_stops_listing_is_forgotten():
    m = MotionTracker()
    m.apply(rows_for(HOME_PT, vid=1) + rows_for(HOME_PT, vid=2), at())
    m.apply(rows_for(HOME_PT, vid=2), at(60))
    assert set(m._seen) == {"2"}


# ============================================================================================ RamTracking against a mocked RAM
SETTINGS = dict(ram_client_id="Alex Clancy", ram_api_key="secret-123", ram_username="api-user", ram_password="api-pass",
                _env_file=None)


def vehicle(vid, reg, driver, point, event, event_at, rpm=None, accurate=None):
    """Shaped exactly like RAM's published VehicleDTO: last_event a STRING, event_date a date-time, location nested."""
    status = {"event_date": iso(event_at), "last_event": event,
              "location": {"latitude": point[0], "longitude": point[1]}, "rawLocation": {"latitude": point[0], "longitude": point[1]}}
    if accurate is not None:
        status["sufficientGpsAccuracy"] = accurate
    out = {"id": vid, "registration": reg, "vehicle_driver": {"name": driver}, "odometer": 41230.5, "vehicle_status": status}
    if rpm is not None:
        out["engineRpm"] = rpm
    return out


class World:
    """A mocked RAM whose vans the test moves, with the wall clock and the cache clock advanced together."""

    def __init__(self, vans=None):
        self.vans = vans or []
        self.wall = at()
        self.mono = 1000.0
        self.requests: list[str] = []
        self.fail = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 28799})
        if self.fail:
            return self.fail
        return httpx.Response(200, json=self.vans)

    def vehicle_calls(self) -> int:
        return sum(1 for p in self.requests if p == "/api/v1/vehicle/for-account")

    def advance(self, seconds: float) -> None:
        self.wall += dt.timedelta(seconds=seconds)
        self.mono += seconds


def make_ram(world: World, store=None) -> RamTracking:
    ram = RamTracking(Settings(**SETTINGS), httpx.AsyncClient(transport=httpx.MockTransport(world.handler)), store=store)
    ram._wall = lambda: world.wall
    ram._now = lambda: world.mono
    return ram


async def test_a_van_that_set_off_long_ago_and_has_since_braked_is_moving_not_parked():
    """THE reported case: TRANSIT_START was 20 minutes ago; the latest event is HARSH_BRAKING 2 minutes ago."""
    world = World([vehicle(1, "YD71 SFS", "Dan Harper", HOME_PT, "HARSH_BRAKING", at(minutes=-2))])
    ram = make_ram(world)
    (row,) = await ram.positions()
    assert row["status"] == "driving" and row["motion_label"] == "Moving" and row["moving"] is True
    assert row["speed_mph"] is None   # one reading: a direction of travel is known, a speed is not


async def test_position_change_between_polls_gives_moving_and_an_estimated_speed_with_no_extra_requests():
    world = World([vehicle(1, "YD71 SFS", "Dan Harper", HOME_PT, "ZONE_OUT", at(minutes=-1))])
    ram = make_ram(world)
    first = (await ram.positions())[0]
    assert first["status"] == "parked" and first["motion_label"] == "Parked"        # first poll: no history, neutral event
    assert (await ram.positions())[0]["status"] == "parked"                          # the same cached poll again
    assert world.vehicle_calls() == 1
    world.advance(61)                                                                # the 60 s cache has expired
    world.vans = [vehicle(1, "YD71 SFS", "Dan Harper", north(HOME_PT, 610), "ZONE_OUT", at(minutes=-1, seconds=61))]
    second = (await ram.positions())[0]
    assert world.vehicle_calls() == 2                                                # one request per minute, as before
    assert second["status"] == "driving" and second["moving"] is True
    assert second["motion_label"] == "Moving (about 20 mph)" and second["speed_mph"] == 20
    again = await ram.positions()
    await ram.vehicles()
    await ram.diagnostics()
    assert world.vehicle_calls() == 2 and again[0]["motion_label"].startswith("Moving")  # reading it again adds no request


async def test_diagnostics_and_positions_agree_and_share_the_one_cached_poll():
    world = World([vehicle(1, "YD71 SFS", "Dan Harper", HOME_PT, "TRANSIT_START", at(minutes=-3), rpm=1800)])
    ram = make_ram(world)
    (pos,) = await ram.positions()
    (diag,) = await ram.diagnostics()
    assert world.vehicle_calls() == 1
    assert diag["classification"] == pos["motion_label"] == "Moving" and diag["last_event"] == "TRANSIT_START"
    assert diag["event_age_min"] == 3 and diag["engine_rpm"] == 1800 and diag["state"] == "moving"
    assert set(diag) == {"registration", "driver", "last_event", "event_age_min", "engine_rpm", "classification", "state",
                         "event_class", "reason"}


async def test_a_stale_moving_event_with_no_movement_is_not_shown_as_live():
    world = World([vehicle(1, "YD71 SFS", "Dan Harper", HOME_PT, "TRANSIT_START", at(minutes=-25))])
    (row,) = await make_ram(world).positions()
    assert row["status"] == "no_position" and row["motion_label"] == "No recent position (last seen 25 min ago)"
    assert row["moving"] is False and row["speed_mph"] is None


async def test_engine_rpm_marks_a_stopped_van_with_a_neutral_event_as_engine_on():
    world = World([vehicle(1, "A", "Dan", HOME_PT, "GEOFENCE_OUT", at(minutes=-2), rpm=900),
                   vehicle(2, "B", "Priya", north(HOME_PT, 3000), "GEOFENCE_OUT", at(minutes=-2), rpm=0),
                   vehicle(3, "C", "Ian", north(HOME_PT, 6000), "GEOFENCE_OUT", at(minutes=-2))])
    rows = {r["registration"]: r for r in await make_ram(world).positions()}
    assert (rows["A"]["status"], rows["A"]["motion_label"]) == ("idling", "Stopped, engine on")
    assert rows["B"]["motion_label"] == rows["C"]["motion_label"] == "Parked"


async def test_the_old_wrong_answers_are_gone_every_van_is_no_longer_parked_or_a_flat_15_mph():
    world = World([vehicle(1, "A", "Dan", HOME_PT, "TRANSIT_START", at(minutes=-1)),
                   vehicle(2, "B", "Priya", north(HOME_PT, 3000), "IGNITION_OFF", at(minutes=-1)),
                   vehicle(3, "C", "Ian", north(HOME_PT, 6000), "IDLE_END", at(minutes=-4))])
    rows = {r["registration"]: r for r in await make_ram(world).positions()}
    assert [rows[k]["status"] for k in "ABC"] == ["driving", "parked", "driving"]
    assert all(r["speed_mph"] is None for r in rows.values())   # not 15, not 0: unknown


async def test_jitter_and_a_bad_fix_across_polls_are_not_movement():
    world = World([vehicle(1, "A", "Dan", HOME_PT, "ZONE_OUT", at(minutes=-1))])
    ram = make_ram(world)
    await ram.positions()
    world.advance(61)
    world.vans = [vehicle(1, "A", "Dan", north(HOME_PT, 15), "ZONE_OUT", at(minutes=-1, seconds=61))]
    assert (await ram.positions())[0]["status"] == "parked"
    world.advance(61)
    world.vans = [vehicle(1, "A", "Dan", north(HOME_PT, 30_000), "ZONE_OUT", at(minutes=-1, seconds=122))]
    jump = (await ram.diagnostics())[0]
    assert jump["state"] == "parked" and "bad fix" in jump["reason"]
    world.advance(61)
    world.vans = [vehicle(1, "A", "Dan", north(HOME_PT, 30_700), "ZONE_OUT", at(minutes=-1, seconds=183), accurate=False)]
    assert (await ram.diagnostics())[0]["reason"].endswith("not accurate enough to compare")


async def test_a_van_with_no_position_is_not_in_positions_but_is_in_vehicles_and_diagnostics():
    world = World([vehicle(1, "A", "Dan", (None, None), "TRANSIT_START", at(minutes=-1))])
    ram = make_ram(world)
    assert await ram.positions() == []
    (v,) = await ram.vehicles()
    assert v["motion"]["state"] == "no_position"
    assert (await ram.diagnostics())[0]["classification"].startswith("No recent position")


async def test_malformed_vehicles_do_not_break_the_classification():
    world = World([{"id": 1, "registration": "A", "vehicle_status": {"event_date": "not a date", "last_event": 42,
                                                                     "location": {"latitude": 53.9, "longitude": -1.6}},
                    "engineRpm": "lots"}, "junk", {"id": 2, "vehicle_status": "nope"}])
    rows = await make_ram(world).positions()
    assert len(rows) == 1 and rows[0]["status"] == "no_position" and rows[0]["motion_label"].startswith("No recent position")


async def test_rate_limiting_still_reaches_the_caller_unchanged():
    world = World()
    world.fail = httpx.Response(429, headers={"Retry-After": "30"}, json={})
    with pytest.raises(RamError) as e:
        await make_ram(world).positions()
    assert e.value.rate_limited


async def test_the_persisted_history_is_used_through_ramtracking():
    """A restart between two polls of a van that is on the move must not forget where it was."""
    store = Store()
    world = World([vehicle(1, "A", "Dan", HOME_PT, "ZONE_OUT", at(minutes=-1))])
    first = make_ram(world, store)
    await first.positions()
    world.advance(61)
    world.vans = [vehicle(1, "A", "Dan", north(HOME_PT, 600), "ZONE_OUT", at(minutes=-1, seconds=61))]
    assert (await first.positions())[0]["status"] == "driving"   # saved: it was seen moving
    world.advance(61)
    world.vans = [vehicle(1, "A", "Dan", north(HOME_PT, 1200), "ZONE_OUT", at(minutes=-1, seconds=122))]
    amnesiac = make_ram(world)                                    # restarted with nowhere to read the history from
    assert (await amnesiac.positions())[0]["status"] == "parked"   # a first reading proves nothing
    restarted = make_ram(world, store)
    assert (await restarted.positions())[0]["status"] == "driving"


# ============================================================================================ Tracker: live / who_is_home / van_day
class RealFSM:
    demo = False

    async def sites(self):
        return []

    async def locations(self):
        return []

    async def jobs(self, *a, **k):
        return []

    async def staff(self):
        return []


def tracker_for(tmp_path, monkeypatch, world, *, working=True, mode="off", store=None):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: working))
    s = Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None, van_locations_out_of_hours=mode)
    db = Database(tmp_path / "t.sqlite")
    ram = make_ram(world, store)
    return Tracker(RealFSM(), http=None, ram=ram, settings=s, db=db), db, ram


NOW_LOCAL = dt.datetime(2026, 10, 2, 9, 5)  # naive, as Tracker.live's own clock is; only used for last_seen arithmetic


async def test_live_rows_carry_the_state_label_and_an_estimated_speed(tmp_path, monkeypatch):
    world = World([vehicle(1, "YD71 SFS", "Dan Harper", HOME_PT, "ZONE_OUT", at(minutes=-1)),
                   vehicle(2, "YD72 SFS", "Priya Shah", north(HOME_PT, 5000), "IDLE_START", at(minutes=-6)),
                   vehicle(3, "YD73 SFS", "Ian Frost", north(HOME_PT, 9000), "TRANSIT_START", at(minutes=-40))])
    tracker, _, _ = tracker_for(tmp_path, monkeypatch, world)
    await tracker.live(now=NOW_LOCAL)
    world.advance(61)
    world.vans[0] = vehicle(1, "YD71 SFS", "Dan Harper", north(HOME_PT, 610), "ZONE_OUT", at(seconds=1))
    out = await tracker.live(now=NOW_LOCAL)
    by = {e["vehicle"]: e for e in out["engineers"]}
    assert (by["YD71 SFS"]["status"], by["YD71 SFS"]["motion_label"], by["YD71 SFS"]["speed_mph"]) == (
        "driving", "Moving (about 20 mph)", 20)
    assert (by["YD72 SFS"]["status"], by["YD72 SFS"]["motion_label"], by["YD72 SFS"]["speed_mph"]) == (
        "idling", "Stopped, engine on", None)
    # RAM's own event age, from the pinned clock (61 s further on than the first poll)
    assert by["YD73 SFS"]["motion_label"] == "No recent position (last seen 41 min ago)" and by["YD73 SFS"]["last_seen_mins"] == 41
    assert by["YD72 SFS"]["last_seen_mins"] == 7


async def test_who_is_home_names_each_vans_state(tmp_path, monkeypatch):
    world = World([vehicle(1, "YD71 SFS", "Dan Harper", HOME_PT, "TRANSIT_START", at(minutes=-1)),
                   vehicle(2, "YD72 SFS", "Priya Shah", north(HOME_PT, 5000), "IGNITION_OFF", at(minutes=-1))])
    tracker, db, _ = tracker_for(tmp_path, monkeypatch, world)
    res = await tracker.home_status("Sam")
    states = {e["vehicle"]: e["motion"] for k in ("out", "at_home", "no_address_label", "no_recent_position") for e in res[k]}
    assert states == {"YD71 SFS": "Moving", "YD72 SFS": "Parked"}


async def test_van_day_gives_todays_current_state(tmp_path, monkeypatch):
    world = World([vehicle(1, "YD71 SFS", "Dan Harper", HOME_PT, "TRANSIT_START", at(minutes=-1))])
    tracker, _, ram = tracker_for(tmp_path, monkeypatch, world)

    async def no_journeys(vehicle_id, day):
        return []

    ram.journeys = no_journeys
    today = dt.date.today()  # clock-ok: only compared with the same call inside van_day, which asks for "today"
    out = await tracker.van_day("Dan Harper", today)
    assert out["current_motion"] == "Moving"
    assert "53.91" not in json.dumps(out)


async def test_a_van_at_home_is_still_just_home_and_the_tool_gives_the_model_no_position_but_the_state(tmp_path, monkeypatch):
    world = World([vehicle(1, "YD71 SFS", "Dan Harper", HOME_PT, "IDLE_START", at(minutes=-1))])
    tracker, _, _ = tracker_for(tmp_path, monkeypatch, world)
    monkeypatch.setattr(tracker, "_home_state", lambda engineer, here: True)
    published = []
    j = SimpleNamespace(tracker=tracker, bus=SimpleNamespace(publish=lambda k, d: published.append(d)), asked_by="Sam")
    from jarvis.brain.tools import engineer_locations
    out = await engineer_locations(j, None)
    (row,) = out["engineers"]
    assert row["at_home"] is True and row["address_label"] == "home" and row["lat"] is None and row["lng"] is None
    assert row["motion_label"] == "Stopped, engine on"
    assert "53.9123" not in json.dumps(out)


async def test_out_of_hours_the_state_is_as_hidden_as_the_position(tmp_path, monkeypatch):
    world = World([vehicle(1, "YD71 SFS", "Dan Harper", HOME_PT, "TRANSIT_START", at(minutes=-1))])
    tracker, _, _ = tracker_for(tmp_path, monkeypatch, world, working=False, mode="off")
    out = await tracker.live("Sam")
    assert out["visible"] is False and out["engineers"] == [] and "TRANSIT" not in json.dumps(out)
    assert (await tracker.fleet_diagnostics("Sam"))["vans"] == []


# ============================================================================================ Fleet diagnostics
async def test_diagnostics_list_each_van_with_its_reason_and_no_coordinates_or_names(tmp_path, monkeypatch):
    world = World([vehicle(1, "YD71 SFS", "Dan Harper", HOME_PT, "TRANSIT_START", at(minutes=-3), rpm=1800),
                   vehicle(2, "YD72 SFS", "Priya Shah", north(HOME_PT, 5000), "ZONE_OUT", at(minutes=-30), rpm=700),
                   vehicle(3, "YD73 SFS", "Ian Frost", north(HOME_PT, 9000), "TRANSIT_START", at(minutes=-25))])
    tracker, _, _ = tracker_for(tmp_path, monkeypatch, world)
    out = await tracker.fleet_diagnostics("Sam", now=NOW_LOCAL)
    by = {v["registration"]: v for v in out["vans"]}
    assert by["YD71 SFS"]["last_event"] == "TRANSIT_START" and by["YD71 SFS"]["event_age_min"] == 3
    assert by["YD71 SFS"]["engine_rpm"] == 1800 and by["YD71 SFS"]["classification"] == "Moving"
    assert by["YD71 SFS"]["reason"].startswith("last_event TRANSIT_START 3 min ago")
    assert by["YD72 SFS"]["classification"] == "Parked"
    assert by["YD73 SFS"]["classification"] == "No recent position (last seen 25 min ago)" and "older than 15 min" in by["YD73 SFS"]["reason"]
    text = json.dumps(out)
    for secret in ("53.9", "53.95", "-1.65", "lat", "lng", "latitude", "longitude", "Dan Harper", "Priya", "Ian Frost", "home", "Home"):
        assert secret not in text, secret
    assert all(set(v) == {"registration", "last_event", "event_age_min", "engine_rpm", "classification", "state", "event_class",
                          "reason"} for v in out["vans"])


async def test_diagnostics_say_what_they_saw_when_a_van_moved(tmp_path, monkeypatch):
    world = World([vehicle(1, "YD71 SFS", "Dan Harper", HOME_PT, "ZONE_OUT", at(minutes=-1))])
    tracker, _, _ = tracker_for(tmp_path, monkeypatch, world)
    await tracker.fleet_diagnostics("Sam")
    world.advance(61)   # the vehicle list is cached for 60 s, so the next poll is a minute on
    world.vans = [vehicle(1, "YD71 SFS", "Dan Harper", north(HOME_PT, 400), "ZONE_OUT", at(seconds=61))]
    (van,) = (await tracker.fleet_diagnostics("Sam"))["vans"]
    assert van["reason"] == "moved 400 m in 61 s" and van["classification"] == "Moving (about 15 mph)"


async def test_diagnostics_need_a_real_connection(tmp_path, monkeypatch):
    class Demo:
        demo = True

    tracker = Tracker(RealFSM(), http=None, ram=Demo())
    out = await tracker.fleet_diagnostics("Sam")
    assert out["connected"] is False and out["vans"] == []
    assert (await Tracker(RealFSM(), http=None, ram=None).fleet_diagnostics("Sam"))["connected"] is False


async def test_diagnostics_report_a_rate_limit_instead_of_failing(tmp_path, monkeypatch):
    world = World()
    world.fail = httpx.Response(429, json={})
    tracker, _, _ = tracker_for(tmp_path, monkeypatch, world)
    out = await tracker.fleet_diagnostics("Sam")
    assert out["vans"] == [] and out["rate_limited"] is True


async def test_out_of_hours_diagnostics_follow_the_owners_rule_and_are_logged(tmp_path, monkeypatch):
    world = World([vehicle(1, "YD71 SFS", "Dan Harper", HOME_PT, "ZONE_OUT", at(minutes=-1)),
                   vehicle(2, "YD72 SFS", "Priya Shah", north(HOME_PT, 5000), "ZONE_OUT", at(minutes=-1))])
    tracker, db, _ = tracker_for(tmp_path, monkeypatch, world, working=False, mode="always")
    assert (await tracker.fleet_diagnostics(""))["vans"] == []          # nobody named as asking: nothing, whatever the setting
    out = await tracker.fleet_diagnostics("Sam")
    assert [v["registration"] for v in out["vans"]] == ["YD71 SFS", "YD72 SFS"] and out["working_hours"] is False
    logged = db.location_lookups()
    assert sorted(r["engineer"] for r in logged if r["tool"] == "fleet_diagnostics") == ["Dan Harper", "Priya Shah"]
    assert "Dan Harper" not in json.dumps(out)


# ============================================================================================ tool, route, team exclusion
def test_the_fleet_diagnostics_tool_is_a_read_only_tool_with_no_inputs():
    tool = TOOLS_BY_NAME["fleet_diagnostics"]
    assert tool.approval is False and tool.definition()["input_schema"].get("properties", {}) == {}
    assert "owner" in tool.description.lower() and "No positions" in tool.description


def test_the_team_role_cannot_use_the_tool_and_the_team_toolset_is_unchanged():
    assert "fleet_diagnostics" not in access.TEAM_TOOLS
    assert not access.tool_allowed("fleet_diagnostics", access.Caller(access.TEAM, "Sam", "x"))
    assert access.tool_allowed("fleet_diagnostics", None) and access.tool_allowed("fleet_diagnostics", access.Caller(access.OWNER))


async def test_a_manager_asking_the_tool_is_refused_and_the_owner_is_answered(tmp_path, monkeypatch):
    world = World([vehicle(1, "YD71 SFS", "Dan Harper", HOME_PT, "TRANSIT_START", at(minutes=-1))])
    tracker, _, _ = tracker_for(tmp_path, monkeypatch, world)
    j = SimpleNamespace(tracker=tracker, asked_by="Sam")
    tool = TOOLS_BY_NAME["fleet_diagnostics"]
    manager = await dispatch(j, tool, tool.model(), access.Caller(access.MANAGER))
    assert "owner only" in json.dumps(manager).lower()
    owner = await fleet_diagnostics(j, None)
    assert owner["vans"][0]["registration"] == "YD71 SFS"


def test_the_route_is_owner_only():
    assert access.ROUTE_POLICY["GET /api/fleet/diagnostics"] == access.OWNER_ONLY


OWNER_PW = "owner-pass-1234"
TEAM_CODE = "team-code-5678"


@pytest.fixture
def app(settings, monkeypatch):
    monkeypatch.setattr("jarvis.main.LOGIN_DELAY_S", 0)
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: True))
    settings.jarvis_owner_password = OWNER_PW
    world = World([vehicle(1, "YD71 SFS", "Dan Harper", HOME_PT, "TRANSIT_START", at(minutes=-1), rpm=1500)])
    j = Jarvis(settings, client=FakeClient())
    j.ram = make_ram(world)
    j.tracker.ram = j.ram
    a = create_app(settings, j)
    with TestClient(a) as base:
        owner = TestClient(a)
        assert owner.post("/login", data={"password": OWNER_PW}, follow_redirects=False).status_code == 303
        assert owner.post("/api/team-access", json={"code": TEAM_CODE}).status_code == 200
        yield SimpleNamespace(app=a, owner=owner, anon=lambda: TestClient(a), j=j, settings=settings, world=world)


def test_the_owner_gets_the_diagnostics_and_nobody_else_does(app, monkeypatch):
    r = app.owner.get("/api/fleet/diagnostics")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    (van,) = r.json()["vans"]
    assert van["registration"] == "YD71 SFS" and van["classification"] == "Moving" and van["engine_rpm"] == 1500
    assert "53.91" not in r.text and "Dan" not in r.text
    assert app.anon().get("/api/fleet/diagnostics").status_code == 401
    team = app.anon()
    assert team.post("/login/team", data={"name": "Sam", "code": TEAM_CODE}, follow_redirects=False).status_code == 303
    assert team.get("/api/fleet/diagnostics").status_code == 403
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "true")
    app.settings.manager_emails = "manager@salts.example"
    manager = {"x-ms-client-principal-idp": "aad", "x-ms-client-principal-name": "manager@salts.example"}
    assert app.anon().get("/api/settings", headers=manager).status_code == 200
    assert app.anon().get("/api/fleet/diagnostics", headers=manager).status_code == 403


def test_the_fleet_drawers_diagnostics_section_is_in_the_owners_page_only(app, monkeypatch):
    assert 'id="fleet-diag"' in app.owner.get("/").text
    team = app.anon()
    team.post("/login/team", data={"name": "Sam", "code": TEAM_CODE}, follow_redirects=False)
    page = team.get("/").text
    assert "fleet-diag" not in page and "Fleet diagnostics" not in page and "pop-fleet" in page   # Fleet itself stays for team
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "true")
    app.settings.manager_emails = "manager@salts.example"
    manager = {"x-ms-client-principal-idp": "aad", "x-ms-client-principal-name": "manager@salts.example"}
    assert "fleet-diag" not in app.anon().get("/", headers=manager).text


def test_the_tracking_route_a_team_session_gets_carries_labels_only(app):
    team = app.anon()
    team.post("/login/team", data={"name": "Sam", "code": TEAM_CODE}, follow_redirects=False)
    body = team.get("/api/tracking").json()
    assert body["engineers"][0]["motion_label"] == "Moving" and "reason" not in json.dumps(body)
