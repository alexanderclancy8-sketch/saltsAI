"""Route-optimised scheduling advice: read-only re-sequencing, SLA priority, working-hours-only locations, urgent slot."""

from __future__ import annotations

from datetime import date

from jarvis.brain.tools import RouteAdviceIn, TOOLS_BY_NAME, route_optimise_advice
from jarvis.core import Jarvis
from jarvis.services.route_advisor import RouteAdvisor, build_route_advice, sla_hours
from tests.fakes import FakeClient

TODAY = date(2026, 10, 5)  # a Monday

# Sites on one north-south line (same longitude) so the best order is easy to see: A < C < D < B going north.
SITES = [
    {"name": "Site A", "lat": 53.800, "lng": -1.750},
    {"name": "Site C", "lat": 53.820, "lng": -1.750},
    {"name": "Site D", "lat": 53.880, "lng": -1.750},
    {"name": "Site B", "lat": 53.900, "lng": -1.750},
    {"name": "Urgent Site", "lat": 53.905, "lng": -1.750},  # right next to Site B
    {"name": "Lost Site", "lat": None, "lng": None},
]
STAFF = [
    {"name": "Dan Harper", "role": "Senior Fire Engineer",
     "certifications": [{"name": "FIA Fire Detection & Alarm - Maintenance", "expires": "2030-01-01"}]},
    {"name": "Priya Shah", "role": "Fire & Security Engineer", "certifications": []},
    {"name": "Tom Wilkinson", "role": "Security Engineer",
     "certifications": [{"name": "NSI Gold Intruder Alarm Installer", "expires": "2030-01-01"}]},
    {"name": "Kyle Brennan", "role": "Apprentice Engineer", "certifications": []},
]
REGISTER = [
    {"name": "Dan Harper", "type": "engineer", "role": "Senior Fire Engineer",
     "duties": ["Service and maintain fire detection & alarm systems to BS 5839-1"],
     "expectations": {"jobs_per_day": 3}},
    {"name": "Priya Shah", "type": "engineer", "role": "Fire & Security Engineer",
     "duties": ["Fire alarm, emergency lighting and intruder alarm servicing"], "expectations": {"jobs_per_day": 2.5}},
    {"name": "Tom Wilkinson", "type": "engineer", "role": "Security Engineer",
     "duties": ["Intruder alarm (BS EN 50131 / PD 6662), CCTV and access control"],
     "expectations": {"jobs_per_day": 2.5}},
    {"name": "Kyle Brennan", "type": "engineer", "role": "Apprentice Engineer", "duties": [],
     "expectations": {"jobs_per_day": 1.5}},
]


def job(ref, site, hour, engineer="Dan Harper", priority="PPM", status="scheduled", jtype="service"):
    return {"ref": ref, "site": site, "engineer": engineer, "priority": priority, "status": status, "type": jtype,
            "scheduled_start": f"{TODAY.isoformat()}T{hour:02d}:00:00"}


def advice(jobs, positions=(), working_hours=True, **kw):
    args = dict(jobs=jobs, sites=SITES, staff=STAFF, register_people=REGISTER, positions=list(positions),
                day=TODAY, today=TODAY, working_hours=working_hours)
    args.update(kw)
    return build_route_advice(**args)


def route_for(result, engineer):
    return next(r for r in result["routes"] if r["engineer"] == engineer)


def refs(seq):
    return [s["job"] for s in seq]


# --------------------------------------------------------------------------- re-sequencing and savings
def test_sla_hours_parsing():
    assert sla_hours("4h") == 4 and sla_hours("24 hours") == 24 and sla_hours("PPM") is None
    assert sla_hours("") is None and sla_hours(None) is None


def test_zigzag_route_is_resequenced_and_the_saving_is_reported():
    jobs = [job("J1", "Site A", 9), job("J2", "Site B", 10), job("J3", "Site C", 11), job("J4", "Site D", 12)]
    r = advice(jobs)
    row = route_for(r, "Dan Harper")
    assert refs(row["current_sequence"]) == ["J1", "J2", "J3", "J4"]
    assert refs(row["proposed_sequence"]) == ["J1", "J3", "J4", "J2"]  # one sweep north instead of zig-zagging
    assert row["changed"] is True
    assert row["drive_minutes_saved"] == row["current_drive_minutes"] - row["proposed_drive_minutes"] > 0
    assert r["summary"]["total_drive_minutes_saved"] == row["drive_minutes_saved"]
    assert r["summary"]["engineers_with_a_better_order"] == 1
    assert {m["job"] for m in row["moves"]} == {"J2", "J3", "J4"}


def test_an_already_efficient_route_is_left_alone():
    jobs = [job("J1", "Site A", 9), job("J2", "Site C", 10), job("J3", "Site B", 11)]
    row = route_for(advice(jobs), "Dan Harper")
    assert row["changed"] is False and row["moves"] == [] and row["drive_minutes_saved"] == 0
    assert refs(row["proposed_sequence"]) == ["J1", "J2", "J3"]
    assert advice(jobs)["suggested_changes"] == []


def test_short_sla_job_is_kept_ahead_of_flexible_work_even_if_it_costs_driving():
    jobs = [job("J1", "Site A", 9), job("J2", "Site B", 10), job("J3", "Site C", 11, priority="4h")]
    row = route_for(advice(jobs), "Dan Harper")
    assert refs(row["proposed_sequence"])[0] == "J3"
    assert sorted(refs(row["proposed_sequence"])) == ["J1", "J2", "J3"]
    assert row["changed"] is True


def test_24h_and_ppm_jobs_are_flexible():
    jobs = [job("J1", "Site A", 9), job("J2", "Site B", 10, priority="24h"), job("J3", "Site C", 11)]
    row = route_for(advice(jobs), "Dan Harper")
    assert refs(row["proposed_sequence"]) == ["J1", "J3", "J2"]


def test_job_in_progress_is_not_resequenced_and_sets_the_start_point():
    jobs = [job("J0", "Site B", 8, status="in_progress"), job("J1", "Site A", 9), job("J2", "Site C", 10)]
    row = route_for(advice(jobs), "Dan Harper")
    assert row["jobs_in_progress"] == ["J0"]
    assert "J0" not in refs(row["proposed_sequence"])
    assert row["start_point"] == "site of the job in progress"
    assert refs(row["proposed_sequence"]) == ["J2", "J1"]  # from Site B, Site C is nearer than Site A


def test_completed_jobs_and_other_days_are_ignored_and_unassigned_jobs_are_listed():
    other = {**job("JX", "Site A", 9), "scheduled_start": "2026-10-06T09:00:00"}
    jobs = [job("J1", "Site A", 9), job("J2", "Site B", 10, status="completed"), other,
            job("J3", "Site C", 11, engineer="")]
    r = advice(jobs)
    assert refs(route_for(r, "Dan Harper")["current_sequence"]) == ["J1"]
    assert [u["job"] for u in r["unassigned_jobs"]] == ["J3"]


def test_route_with_an_unlocated_site_is_left_in_order_and_says_why():
    jobs = [job("J1", "Site A", 9), job("J2", "Lost Site", 10), job("J3", "Site C", 11)]
    row = route_for(advice(jobs), "Dan Harper")
    assert row["optimised"] is False and row["changed"] is False
    assert refs(row["proposed_sequence"]) == ["J1", "J2", "J3"]
    assert row["drive_minutes_saved"] is None
    assert any("Lost Site" in n for n in row["notes"])


# --------------------------------------------------------------------------- working-hours-only locations
POSITIONS = [{"engineer": "Dan Harper", "lat": 53.900, "lng": -1.750, "last_seen_mins": 2}]


def test_live_position_is_used_as_the_start_point_in_working_hours():
    jobs = [job("J1", "Site A", 9), job("J2", "Site B", 10)]
    r = advice(jobs, positions=POSITIONS, working_hours=True)
    row = route_for(r, "Dan Harper")
    assert row["start_point"].startswith("live van position")
    assert refs(row["proposed_sequence"]) == ["J2", "J1"]  # he is already at Site B
    assert r["locations"]["live_positions_used"] == 1


def test_live_positions_are_ignored_outside_working_hours():
    jobs = [job("J1", "Site A", 9), job("J2", "Site B", 10)]
    r = advice(jobs, positions=POSITIONS, working_hours=False)
    row = route_for(r, "Dan Harper")
    assert row["start_point"].startswith("unknown")
    assert refs(row["proposed_sequence"]) == ["J1", "J2"]
    assert r["locations"]["live_positions_used"] == 0 and "not read or used" in r["locations"]["note"]


def test_live_positions_are_ignored_when_planning_another_day():
    jobs = [{**job("J1", "Site A", 9), "scheduled_start": "2026-10-06T09:00:00"},
            {**job("J2", "Site B", 10), "scheduled_start": "2026-10-06T10:00:00"}]
    r = advice(jobs, positions=POSITIONS, working_hours=True, day=date(2026, 10, 6))
    assert r["locations"]["live_positions_used"] == 0
    assert route_for(r, "Dan Harper")["start_point"].startswith("unknown")


def test_stale_live_positions_are_not_trusted():
    stale = [{**POSITIONS[0], "last_seen_mins": 240}]
    r = advice([job("J1", "Site A", 9), job("J2", "Site B", 10)], positions=stale)
    assert r["locations"]["live_positions_used"] == 0


# --------------------------------------------------------------------------- urgent call-out
def _fleet_jobs():
    return [job("J1", "Site A", 9), job("J2", "Site B", 10),  # Dan: A then B
            job("J3", "Site A", 9, engineer="Priya Shah"),  # Priya: A only
            job("J4", "Site D", 9, engineer="Tom Wilkinson")]


def test_urgent_call_out_goes_to_the_skilled_engineer_with_the_cheapest_slot():
    r = advice(_fleet_jobs(), urgent={"site": "Urgent Site", "description": "Fire alarm panel fault",
                                      "priority": "4h"})
    u = r["urgent_callout"]
    assert u["system_types"] == ["fire_alarm"] and "GUESSED" in u["system_types_basis"]
    rec = u["recommended"]
    assert rec["engineer"] == "Dan Harper"  # certificate on record AND the call-out is next to his last job
    assert rec["slot_after"].startswith("J2") and rec["slot_before"] is None
    assert rec["extra_drive_minutes"] <= 2
    assert rec["short_sla_jobs_pushed_back"] == []
    assert "certificate" in rec["skills"]
    excluded = {e["engineer"] for e in u["excluded_engineers"]}
    assert {"Tom Wilkinson", "Kyle Brennan"} <= excluded  # no fire evidence while others have some / apprentice
    assert [a["engineer"] for a in u["alternatives"]] == ["Priya Shah"]
    assert any("role/duties" in w for w in u["alternatives"][0]["warnings"])


def test_urgent_slot_is_the_cheapest_gap_and_does_not_jump_a_short_sla_job():
    jobs = [job("J1", "Site A", 9), job("J2", "Site B", 10, priority="4h"),
            job("J3", "Site A", 9, engineer="Priya Shah")]  # Priya is busy far away, so she is no cheaper than Dan
    r = advice(jobs, urgent={"site": "Urgent Site", "description": "intruder alarm tamper"},
               staff=[STAFF[2], STAFF[0]])  # whichever order, Dan has no intruder evidence, Tom has
    u = r["urgent_callout"]
    assert u["system_types"] == ["intruder"]
    assert u["recommended"]["engineer"] == "Tom Wilkinson"  # the only one with intruder evidence

    r = advice(jobs, urgent={"site": "Urgent Site", "description": "Fire alarm fault", "priority": "4h"})
    rec = r["urgent_callout"]["recommended"]
    assert rec["engineer"] == "Dan Harper"
    # proposed route is J2 (4h) then J1; the cheapest gap is straight after J2, so J2 is not pushed back
    assert rec["slot_after"].startswith("J2") and rec["slot_before"].startswith("J1")
    assert rec["short_sla_jobs_pushed_back"] == []


def test_urgent_call_out_with_no_locatable_site_says_so():
    r = advice(_fleet_jobs(), urgent={"site": "Nowhere In Particular", "description": "fire alarm"})
    assert "Couldn't locate" in r["urgent_callout"]["error"]


def test_urgent_call_out_uses_supplied_coordinates():
    r = advice(_fleet_jobs(), urgent={"site": "BD1 1AA", "description": "fire alarm", "coords": (53.905, -1.75)})
    assert r["urgent_callout"]["recommended"]["engineer"] == "Dan Harper"


def test_expired_certificate_engineer_is_not_proposed_for_the_call_out():
    staff = [{"name": "Dan Harper", "role": "Fire Engineer",
              "certifications": [{"name": "FIA Fire Detection", "expires": "2025-01-01"}]}]
    r = advice([job("J1", "Site A", 9)], staff=staff, register_people=[],
               urgent={"site": "Urgent Site", "description": "fire alarm"})
    u = r["urgent_callout"]
    assert u["recommended"] is None and "problem" in u
    assert "expired" in u["excluded_engineers"][0]["reason"]


def test_urgent_booking_is_only_a_named_approval_gated_suggestion():
    r = advice(_fleet_jobs(), urgent={"site": "Urgent Site", "description": "Fire alarm fault", "priority": "4h"})
    sb = r["urgent_callout"]["suggested_booking"]
    assert sb["tool"].startswith("log_job") and "NOT done" in sb["tool"]
    assert sb["args"]["engineer"] == "Dan Harper" and sb["args"]["type"] == "callout"
    assert "Nothing has been booked" in r["note"] and r["advisory_only"] is True


# --------------------------------------------------------------------------- async advisor: read-only, partial data
class FakeFSM:
    demo = True

    def __init__(self, jobs, fail=()):
        self._jobs = jobs
        self.fail = set(fail)
        self.writes = []

    async def jobs(self, date_from=None, date_to=None, status=None, engineer=None):
        if "jobs" in self.fail:
            raise RuntimeError("boom")
        return self._jobs

    async def sites(self):
        if "sites" in self.fail:
            raise RuntimeError("boom")
        return SITES

    async def staff(self):
        return STAFF

    async def write(self, *a, **k):
        self.writes.append((a, k))
        raise AssertionError("the route advisor must never write to FSM")


class FakeRegister:
    def people(self, kind=None):
        return REGISTER


class FakeTracker:
    def __init__(self, working_hours=True, fail=False):
        self.working_hours = working_hours
        self.fail = fail

    async def live(self):
        if self.fail:
            raise RuntimeError("no feed")
        # a misbehaving feed that still returns positions outside working hours must not be used
        return {"working_hours": self.working_hours, "engineers": POSITIONS}

    async def _geocode(self, place):
        for s in SITES:
            if place.lower() in s["name"].lower() and s["lat"] is not None:
                return (s["lat"], s["lng"]), s["name"]
        return None


async def test_advisor_never_writes_and_uses_positions_only_in_working_hours():
    jobs = [job("J1", "Site A", 9), job("J2", "Site B", 10)]
    fsm = FakeFSM(jobs)
    r = await RouteAdvisor(fsm, FakeTracker(True), FakeRegister()).advise(today=TODAY)
    assert fsm.writes == [] and r["advisory_only"] is True
    assert r["locations"]["live_positions_used"] == 1
    r = await RouteAdvisor(fsm, FakeTracker(False), FakeRegister()).advise(today=TODAY)
    assert r["locations"]["live_positions_used"] == 0 and r["locations"]["working_hours"] is False


async def test_advisor_geocodes_the_urgent_site_and_survives_missing_data():
    fsm = FakeFSM(_fleet_jobs(), fail={"sites"})
    r = await RouteAdvisor(fsm, FakeTracker(fail=True), None).advise(
        today=TODAY, urgent_site="Urgent Site", urgent_description="fire alarm fault")
    missing = " ".join(r["data_quality"]["missing_or_failed"])
    assert "Sites" in missing and "Live engineer locations" in missing and "Staff register unavailable" in missing
    assert r["urgent_callout"]["site"] == "Urgent Site"


async def test_advisor_reports_an_error_if_jobs_cannot_be_read():
    r = await RouteAdvisor(FakeFSM([], fail={"jobs"}), FakeTracker(), None).advise(today=TODAY)
    assert "nothing to route" in r["error"]


# --------------------------------------------------------------------------- tool registration
def test_tool_is_registered_as_read_only():
    tool = TOOLS_BY_NAME["route_optimise_advice"]
    assert tool.approval is False
    assert "READ-ONLY" in tool.description and "log_job" in tool.description and "fsm_change" in tool.description


async def test_tool_runs_end_to_end_on_demo_data(settings):
    j = Jarvis(settings, client=FakeClient([]))
    result = await route_optimise_advice(j, RouteAdviceIn())
    assert result["advisory_only"] is True and result["demo"] is True
    assert "routes" in result and "locations" in result
    bad = await route_optimise_advice(j, RouteAdviceIn(plan_date="not-a-date"))
    assert "error" in bad
    await j.http.aclose()
