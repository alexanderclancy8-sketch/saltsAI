"""Draft fire alarm design estimator: counts, draft marking, no clause citations, tool registration."""

from __future__ import annotations

import pytest

from jarvis.brain.tools import FireDesignIn, TOOLS_BY_NAME, fire_alarm_design_draft
from jarvis.core import Jarvis
from jarvis.services.fire_design import (DRAFT_STATUS, _grid_count, cites_clause_numbers, design)
from tests.fakes import FakeClient


def room(name, **kw):
    return {"name": name, "floor": "Ground", "use": "office", **kw}


OFFICE = [
    room("Open office", length_m=20, width_m=10),
    room("Corridor", use="corridor", length_m=30, width_m=2),
    room("Kitchen", use="kitchen", length_m=4, width_m=4),
    room("WC", use="toilet", area_m2=4, opens_onto_escape_route=True),
]


def total(r, key):
    return r["summary"]["devices"][key]


def test_grid_count_basics():
    assert _grid_count(10, 10, 7.5) == 1  # corner is 7.07 m from the centre
    assert _grid_count(20, 10, 7.5) == 2
    assert _grid_count(30, 2, 7.5) == 3  # narrow corridor: spacing along the axis is ~14.9 m
    assert _grid_count(10, 10, 5.3) == 4  # heat radius is smaller, so a 10 m square needs more


def test_l1_covers_every_room_with_sensible_types():
    r = design("Office", "L1", OFFICE)
    by = {x["name"]: x for x in r["rooms"]}
    assert all(x["detection_required"] for x in r["rooms"])
    assert by["Open office"]["detectors"] == 2 and by["Open office"]["detector_type"] == "smoke"
    assert by["Corridor"]["detectors"] == 3
    assert by["Kitchen"]["detector_type"] == "heat" and by["Kitchen"]["detectors"] == 1
    assert total(r, "smoke") == 6 and total(r, "heat") == 1


def test_l3_only_escape_routes_and_rooms_opening_onto_them():
    r = design("Office", "L3", OFFICE)
    by = {x["name"]: x for x in r["rooms"]}
    assert by["Corridor"]["detection_required"] and by["WC"]["detection_required"]
    assert not by["Open office"]["detection_required"] and not by["Kitchen"]["detection_required"]


def test_l4_corridors_only_and_l2_adds_high_risk():
    r4 = design("Office", "L4", OFFICE)
    assert [x["name"] for x in r4["rooms"] if x["detection_required"]] == ["Corridor"]
    rooms = OFFICE + [room("Server room", area_m2=20, high_risk=True)]
    r2 = design("Office", "L2", rooms)
    assert {x["name"] for x in r2["rooms"] if x["detection_required"]} == {"Corridor", "WC", "Server room"}


def test_category_m_has_no_detectors_but_still_has_call_points_and_sounders():
    r = design("Shed", "M", OFFICE)
    assert total(r, "smoke") == total(r, "heat") == total(r, "multi") == 0
    assert total(r, "mcp") >= 2 and total(r, "sounder") >= 1


def test_call_points_follow_exits_and_floor_area():
    small = design("A", "L1", [room("Hall", area_m2=100)], exits_by_floor={"Ground": 3})
    assert total(small, "mcp") == 3
    big = design("B", "L1", [room("Warehouse", area_m2=4000, ceiling_height_m=6)], exits_by_floor={"Ground": 2})
    assert total(big, "mcp") == 5  # 4000 / 800
    assumed = design("C", "L1", [room("Hall", area_m2=100)])
    assert total(assumed, "mcp") == 2 and any("assumed" in a and "exits" in a for a in assumed["assumptions"])


def test_zones_split_by_floor_unless_building_is_small():
    small = design("S", "L1", [room("A", area_m2=50), room("B", area_m2=50, floor="First")])
    assert total(small, "zones") == 1
    rooms = [room("A", area_m2=400), room("B", area_m2=400, floor="First")]
    assert total(design("S", "L1", rooms), "zones") == 2
    assert total(design("S", "L1", [room("Big", area_m2=2500, ceiling_height_m=5)]), "zones") == 2


def test_high_ceiling_is_flagged_not_counted_as_point_detectors():
    r = design("Warehouse", "L1", [room("Hall", area_m2=400, ceiling_height_m=12)])
    x = r["rooms"][0]
    assert x["beam_or_aspirating"] and x["detectors"] == 0 and any("above the point-detector limit" in f for f in x["flags"])
    assert any("High-ceiling" in line["item"] for line in r["schedule"])


def test_vads_only_where_needed_or_everywhere():
    some = design("V", "L1", [room("Office", area_m2=80), room("Disabled WC", use="toilet", area_m2=5, needs_vad=True)])
    assert total(some, "vad") == 1
    allv = design("V", "L1", [room("Office", area_m2=50), room("Hall", area_m2=50)], vads_throughout=True)
    assert total(allv, "vad") == 2


def test_sleeping_rooms_get_a_sounder_each_and_a_category_warning():
    rooms = [room(f"Bed {i}", use="bedroom", area_m2=12) for i in range(3)]
    r = design("Care", "L3", rooms)
    assert total(r, "sounder") == 3
    assert all(any("sleeping" in f for f in x["flags"]) for x in r["rooms"])


def test_l5_and_p2_warn_that_the_designer_must_define_coverage():
    assert any("engineered" in w for w in design("X", "L5", OFFICE)["warnings"])
    assert any("P2" in w for w in design("X", "P2", OFFICE)["warnings"])


def test_output_is_always_marked_as_draft_and_never_certified():
    r = design("Office", "L1", OFFICE)
    assert r["status"] == DRAFT_STATUS and r["draft"] is True and r["certified"] is False
    assert "competent fire alarm designer" in r["requires_review_by"]
    spec = r["specification_markdown"]
    assert spec.startswith("**DRAFT - NOT A CERTIFIED DESIGN**")
    assert spec.count("DRAFT - NOT A CERTIFIED DESIGN") >= 2  # top and bottom banners
    assert "NOT A CERTIFIED DESIGN" in r["disclaimer"] and spec.endswith(r["disclaimer"])
    assert "not a design certificate" in spec
    assert r["verify"] and r["assumptions"]
    assert all(line["notes"] for line in r["schedule"])


def test_no_clause_numbers_are_cited():
    r = design("Office", "L1", OFFICE)
    assert not cites_clause_numbers(r["specification_markdown"])
    assert not cites_clause_numbers(r["standard_reference"])
    assert cites_clause_numbers("see clause 22.3")  # the guard itself works


def test_schedule_has_project_lines_and_floor_lines():
    r = design("Office", "L1", OFFICE)
    items = [line["item"] for line in r["schedule"]]
    assert any("control and indicating equipment" in i for i in items)
    assert any("batter" in i.lower() for i in items)
    assert any(line["floor"] == "Ground" and "smoke" in line["item"] for line in r["schedule"])


def test_dimensions_assumed_square_when_only_area_given():
    r = design("Office", "L1", [room("Corridor", use="corridor", area_m2=60)])
    assert any("assumed square" in f for f in r["rooms"][0]["flags"])


@pytest.mark.parametrize("category,rooms,msg", [
    ("Z9", OFFICE, "Unknown category"),
    ("L1", [], "No rooms"),
    ("L1", [{"name": "Mystery"}], "area_m2"),
])
def test_bad_input_raises(category, rooms, msg):
    with pytest.raises(ValueError, match=msg):
        design("P", category, rooms)


# --------------------------------------------------------------------------- tool
def test_tool_is_registered_read_only_and_says_draft():
    tool = TOOLS_BY_NAME["fire_alarm_design_draft"]
    assert tool.approval is False
    assert "DRAFT" in tool.description and "competent fire alarm designer" in tool.description
    schema = tool.definition()["input_schema"]
    assert "rooms" in schema["properties"] and "category" in schema["properties"]


async def test_tool_runs_end_to_end_and_reports_errors_as_drafts(settings):
    j = Jarvis(settings, client=FakeClient([]))
    a = FireDesignIn(project="Unit 4", category="L2", rooms=[
        {"name": "Corridor", "use": "corridor", "length_m": 20, "width_m": 2},
        {"name": "Office", "area_m2": 90, "opens_onto_escape_route": True}])
    r = await fire_alarm_design_draft(j, a)
    assert r["status"] == DRAFT_STATUS and r["summary"]["devices"]["smoke"] >= 2
    bad = await fire_alarm_design_draft(j, FireDesignIn(project="x", category="L1", rooms=[{"name": "No size"}]))
    assert bad["certified"] is False and "area_m2" in bad["error"] and bad["status"] == DRAFT_STATUS
    await j.http.aclose()
