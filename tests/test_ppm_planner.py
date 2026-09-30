"""PPM scheduling / dispatch advisor: read-only plan built from sample Salts FSM data."""

from __future__ import annotations

from datetime import date, timedelta

from jarvis.brain.tools import PPMPlanIn, TOOLS_BY_NAME, ppm_schedule_plan
from jarvis.core import Jarvis
from jarvis.services.ppm_planner import (PPMPlanner, Place, build_places, build_plan, proximity_tier,
                                         resolve_place, systems_due)
from tests.fakes import FakeClient

TODAY = date(2026, 10, 5)  # a Monday; the first planning day is therefore Tue 6 Oct


def iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def system(sid, site, stype, due_in, freq=6, **extra):
    return {"id": sid, "site": site, "customer": "Cust", "type": stype, "make_model": "x",
            "service_frequency_months": freq, "last_service": iso(due_in - 180),
            "next_service_due": iso(due_in) if due_in is not None else None, **extra}


SITES = [
    {"name": "Alpha School", "lat": 53.850, "lng": -1.770, "postcode": "BD17 7AB"},
    {"name": "Beta Care Home", "lat": 53.855, "lng": -1.775, "postcode": "BD17 1XY"},  # ~0.4 miles from Alpha
    {"name": "Gamma Mill", "lat": 53.800, "lng": -1.550, "postcode": "LS12 3AA"},  # ~9 miles away
    {"name": "Delta Unit", "lat": None, "lng": None, "postcode": None},  # no location at all
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
    {"name": "Hannah Cole", "type": "office", "role": "Office Manager / Scheduler", "duties": [], "expectations": {}},
]
SYSTEMS = [
    system("S1", "Alpha School", "fire_alarm", 10),
    system("S2", "Alpha School", "emergency_lighting", 25, freq=12),
    system("S3", "Alpha School", "cctv", 120, freq=12),  # far too early to bundle
    system("S4", "Beta Care Home", "fire_alarm", 12),
    system("S5", "Gamma Mill", "intruder", -3),  # overdue
    system("S6", "Delta Unit", "fire_alarm", 20),  # no location
    system("S7", "Gamma Mill", "cctv", None),  # no due date at all
]


def plan(**kw):
    args = dict(systems=SYSTEMS, sites=SITES, staff=STAFF, register_people=REGISTER, jobs=[], today=TODAY,
                days_ahead=30)
    args.update(kw)
    return build_plan(**args)


def visits_by_site(result):
    out = {}
    for v in result["visits"]:
        out.setdefault(v["site"], []).append(v)
    return out


def flags(v):
    return {f["flag"] for f in v["flags"]}


# --------------------------------------------------------------------------- what's due
def test_systems_due_is_the_same_rule_fsm_systems_due_uses():
    due = systems_due(SYSTEMS, TODAY, 15)
    assert [s["id"] for s in due] == ["S5", "S1", "S4"]  # overdue first, S2/S3/S6 beyond 15 days, S7 has no date
    assert due[0]["days_until_due"] == -3 and due[0]["site"] == "Gamma Mill"


def test_plan_reports_overdue_and_undatable_systems():
    r = plan()
    assert r["advisory_only"] is True
    assert r["summary"]["of_which_overdue"] == 1
    assert [c["system"] for c in r["cannot_be_planned"]] == ["S7"]
    gamma = visits_by_site(r)["Gamma Mill"][0]
    assert "overdue" in flags(gamma)
    assert gamma["planned_date"] == iso(1)  # as soon as possible: the first planning day
    assert gamma["engineer"] == "Tom Wilkinson"


# --------------------------------------------------------------------------- bundling
def test_same_site_systems_are_bundled_inside_their_windows():
    r = plan()
    alpha = visits_by_site(r)["Alpha School"]
    assert len(alpha) == 1
    v = alpha[0]
    assert {s["type"] for s in v["systems"]} == {"fire_alarm", "emergency_lighting"}
    assert v["visit_window"]["latest_permitted"] == iso(10)  # the fire alarm's date is never pushed
    planned = date.fromisoformat(v["planned_date"])
    assert date.fromisoformat(v["visit_window"]["earliest_permitted"]) <= planned <= TODAY + timedelta(days=10)
    assert planned.weekday() < 5
    assert "hard_limit" in flags(v)  # fire alarm 6-monthly


def test_a_system_is_not_pulled_forward_beyond_its_tolerance():
    r = plan()
    every_system = {s["system"] for v in r["visits"] for s in v["systems"]}
    assert "S3" not in every_system  # cctv due in 120 days is not dragged onto a visit next week


def test_not_yet_due_system_can_join_a_visit_and_is_labelled_brought_forward():
    r = plan(days_ahead=15)  # S2 (due in 25 days) is no longer 'due' itself, but its early window is open
    v = visits_by_site(r)["Alpha School"][0]
    cats = {s["system"]: s["due_category"] for s in v["systems"]}
    assert cats == {"S1": "due_within_window", "S2": "brought_forward_to_bundle"}
    assert "bundled_early" in flags(v)


def test_system_with_no_frequency_is_never_pulled_early():
    systems = [system("A", "Alpha School", "fire_alarm", 10), system("B", "Alpha School", "intruder", 25, freq=None)]
    v = visits_by_site(plan(systems=systems, days_ahead=15))["Alpha School"][0]
    assert [s["system"] for s in v["systems"]] == ["A"]  # B has no tolerance, so it stays on its own date
    systems = [system("A", "Alpha School", "fire_alarm", 10), system("B", "Alpha School", "intruder", 8, freq=None)]
    v = visits_by_site(plan(systems=systems, days_ahead=15))["Alpha School"][0]
    assert "frequency_missing" in flags(v)


# --------------------------------------------------------------------------- geography
def test_clusters_group_nearby_sites_and_separate_distant_ones():
    r = plan()
    by = visits_by_site(r)
    assert by["Alpha School"][0]["cluster"] == by["Beta Care Home"][0]["cluster"] == "BD17 (Bradford area)"
    assert by["Gamma Mill"][0]["cluster"] != by["Alpha School"][0]["cluster"]
    assert proximity_tier(Place("a", (53.85, -1.77)), Place("b", (53.80, -1.55)), 4.0) == 2
    assert proximity_tier(Place("a", (53.85, -1.77)), Place("b", (53.855, -1.775)), 4.0) == 0


def test_postcode_only_sites_cluster_by_district():
    sites = [{"name": "One", "postcode": "BD17 7AB"}, {"name": "Two", "postcode": "BD17 2XX"},
             {"name": "Three", "postcode": "LS29 6AA"}, {"name": "Four", "postcode": "BD3 1AA"}]
    systems = [system(f"P{i}", n, "fire_alarm", 10) for i, n in enumerate(("One", "Two", "Three", "Four"))]
    r = plan(systems=systems, sites=sites)
    labels = {v["site"]: v["cluster"] for v in r["visits"]}
    assert labels["One"] == labels["Two"] == "BD17 (Bradford area)"
    assert labels["Three"] == "LS29 (Leeds area)"
    assert labels["Four"] == "BD3 (Bradford area)"
    assert r["data_quality"]["locations"]["without"] == []


def test_missing_location_is_flagged_not_guessed():
    r = plan()
    delta = visits_by_site(r)["Delta Unit"][0]
    assert "location_missing" in flags(delta)
    assert delta["cluster"] == "location unknown"
    assert "Delta Unit" in r["data_quality"]["locations"]["without"]
    assert any(c["cluster"] == "location unknown" for c in r["area_clusters"])


def test_site_not_in_the_site_list_uses_a_postcode_in_its_name_if_present():
    places = build_places(SITES)
    p = resolve_place("Unit 4, Shipley, BD18 3QQ", places)
    assert p.district == "BD18" and p.coords is None
    assert resolve_place("Somewhere vague", places).located is False


# --------------------------------------------------------------------------- skills
def test_skills_are_labelled_certificate_versus_inference_and_never_invented():
    r = plan()
    by = visits_by_site(r)
    beta = by["Beta Care Home"][0]  # fire alarm: Dan has a matching certificate
    assert beta["engineer"] == "Dan Harper"
    assert "certificate" in beta["skills"][0] and "flags" in beta
    alpha = by["Alpha School"][0]  # emergency lighting: no certificate anywhere, only Priya's duties mention it
    assert alpha["engineer"] == "Priya Shah"
    assert "skills_inferred_from_role" in flags(alpha)
    assert any("INFERRED" in s for s in alpha["skills"])
    statement = r["data_quality"]["engineer_skills"]
    assert "does NOT expose skills" in statement["what_the_fsm_api_exposes"]
    assert statement["never_invented"] is True


def test_apprentices_are_not_proposed_to_lead_visits():
    r = plan()
    assert "Kyle Brennan" not in {v["engineer"] for v in r["visits"]}
    kyle = next(e for e in r["engineers_considered"] if e["engineer"] == "Kyle Brennan")
    assert kyle["proposed_for_visits"] is False


def test_no_skill_data_at_all_is_stated_plainly_and_visits_are_marked_unverified():
    staff = [{"name": "Sam", "role": "Engineer"}, {"name": "Alex", "role": "Engineer"}]
    r = plan(staff=staff, register_people=[], systems=[system("A", "Alpha School", "fire_alarm", 10)])
    assert "No engineer has any certification on record" in r["data_quality"]["engineer_skills"]["statement"]
    v = r["visits"][0]
    assert v["status"] == "proposed" and "skills_unverified" in flags(v)
    assert all("certificate '" not in s for s in v["skills"])  # nothing claimed that isn't there
    assert r["engineers_considered"][0]["source"].startswith("assumed default")


def test_expired_certificate_makes_the_visit_unschedulable_rather_than_assuming():
    staff = [{"name": "Dan Harper", "role": "Fire Engineer",
              "certifications": [{"name": "FIA Fire Detection", "expires": iso(-30)}]}]
    r = plan(staff=staff, register_people=[], systems=[system("A", "Alpha School", "fire_alarm", 10)])
    v = r["visits"][0]
    assert v["status"] == "unschedulable" and v["planned_date"] is None and "unschedulable" in flags(v)
    assert r["summary"]["visits_unschedulable"] == 1


def test_bundle_is_split_when_no_single_engineer_covers_every_system_type():
    sites = [SITES[0]]
    systems = [system("A", "Alpha School", "fire_alarm", 10), system("B", "Alpha School", "intruder", 10)]
    r = plan(sites=sites, systems=systems, staff=[STAFF[0], STAFF[2]])  # Dan (fire) and Tom (security) only
    assert len(r["visits"]) == 2
    assert {v["engineer"] for v in r["visits"]} == {"Dan Harper", "Tom Wilkinson"}
    assert all("split_visit" in flags(v) for v in r["visits"])


# --------------------------------------------------------------------------- load and risk
def test_capacity_follows_expected_jobs_per_day_and_overflow_is_flagged():
    staff = [STAFF[0]]
    register = [{**REGISTER[0], "expectations": {"jobs_per_day": 1}}]
    sites = [{"name": f"Site {i}", "lat": 53.8 + i * 0.001, "lng": -1.7} for i in range(4)]
    systems = [system(f"X{i}", f"Site {i}", "fire_alarm", 3) for i in range(4)]  # due Thu 8 Oct: only 3 days to use
    r = plan(staff=staff, register_people=register, sites=sites, systems=systems)
    assert r["summary"]["visits_proposed"] == 3 and r["summary"]["visits_unschedulable"] == 1
    assert "jobs-per-day" in r["unschedulable"][0]["reason"]
    assert len({v["planned_date"] for v in r["visits"] if v["planned_date"]}) == 3
    assert all(d["engineers"][0]["load"].startswith("1 of expected 1") for d in r["plan_by_day"])


def test_existing_bookings_use_up_capacity_and_block_double_booking():
    jobs = [{"ref": "J1", "type": "service", "status": "scheduled", "site": "Alpha School",
             "engineer": "Dan Harper", "scheduled_start": iso(7) + "T09:00:00"}]
    r = plan(jobs=jobs)
    assert "Alpha School" not in visits_by_site(r)  # already has an open service job in the window
    assert {c["system"] for c in r["needs_booking_check"]} == {"S1", "S2"}
    assert "check the job covers THIS system" in r["needs_booking_check"][0]["note"]


def test_low_slack_visits_are_flagged_at_risk():
    r = plan(systems=[system("A", "Alpha School", "fire_alarm", 2)])  # due Wed 7 Oct, planning starts Tue 6th
    v = r["visits"][0]
    assert "at_risk_low_slack" in flags(v)
    assert r["summary"]["visits_flagged_at_risk"] == 1


def test_due_before_first_plannable_day_is_flagged():
    r = plan(systems=[system("A", "Alpha School", "fire_alarm", 0)])  # due today, first planning day is tomorrow
    v = r["visits"][0]
    assert {"due_before_first_plannable_day"} <= flags(v)
    assert v["planned_date"] == iso(1)


def test_suggested_bookings_are_suggestions_only():
    r = plan()
    assert r["suggested_bookings"]
    assert all(s["tool"].startswith("log_job") and "NOT done" in s["tool"] for s in r["suggested_bookings"])
    assert "Nothing has been booked" in r["note"]


# --------------------------------------------------------------------------- async planner: partial data, read-only
class FakeFSM:
    demo = True

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.writes = []

    async def systems(self):
        return SYSTEMS

    async def sites(self):
        if "sites" in self.fail:
            raise RuntimeError("boom")
        return SITES

    async def staff(self):
        return STAFF

    async def jobs(self, date_from=None, date_to=None, status=None, engineer=None):
        return []

    async def write(self, *a, **k):
        self.writes.append((a, k))
        raise AssertionError("the planner must never write to FSM")


class FakeRegister:
    def people(self, kind=None):
        return REGISTER


async def test_planner_never_writes_and_survives_a_missing_endpoint():
    fsm = FakeFSM(fail={"sites"})
    r = await PPMPlanner(fsm, FakeRegister()).plan(today=TODAY)
    assert fsm.writes == []
    assert r["advisory_only"] is True
    assert any("Sites" in m for m in r["data_quality"]["missing_or_failed"])
    assert all("location_missing" in flags(v) for v in r["visits"])  # no site data -> nothing located, and it says so


async def test_planner_without_a_register_says_so():
    r = await PPMPlanner(FakeFSM(), None).plan(today=TODAY)
    assert any("Staff register unavailable" in m for m in r["data_quality"]["missing_or_failed"])


async def test_planner_returns_an_error_if_systems_cannot_be_read():
    class Broken(FakeFSM):
        async def systems(self):
            raise RuntimeError("down")

    r = await PPMPlanner(Broken(), None).plan(today=TODAY)
    assert "nothing to plan" in r["error"]


# --------------------------------------------------------------------------- tool registration
def test_tool_is_registered_as_read_only():
    tool = TOOLS_BY_NAME["ppm_schedule_plan"]
    assert tool.approval is False
    assert "READ-ONLY" in tool.description and "log_job" in tool.description


async def test_tool_runs_end_to_end_on_demo_data(settings):
    j = Jarvis(settings, client=FakeClient([]))
    result = await ppm_schedule_plan(j, PPMPlanIn(days_ahead=60))
    assert result["advisory_only"] is True and result["demo"] is True
    assert "plan_by_day" in result and "data_quality" in result
    bad = await ppm_schedule_plan(j, PPMPlanIn(start_date="not-a-date"))
    assert "error" in bad
    await j.http.aclose()
