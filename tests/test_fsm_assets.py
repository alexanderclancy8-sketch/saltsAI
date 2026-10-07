"""Van and equipment dates from the FSM's `assets` group (services/fsm_assets.py + services/accreditations.py): which fields mean what,
'date not recorded' (neither compliant nor overdue), the 90/60/30/14/7/1-day reminders run from the FSM's dates, the RAM Tracking
cross-check, and the hand-typed register staying as a fallback (and the tools refusing once the FSM is the source). The FSM is
mocked (httpx.MockTransport); `today` is always injected."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest
import yaml

from jarvis.brain.tools import TOOLS_BY_NAME, dispatch
from jarvis.integrations.fsm_data import FsmData, FsmDataError, Resource, parse_catalog
from jarvis.services import fsm_assets as fa
from jarvis.services import standing_approvals as sa
from jarvis.services.accreditations import FSM_MANAGED, FSM_SNAPSHOT_MAX_AGE_S, REMIND_AT_DAYS, Accreditations
from tests.fsm_data_helpers import Clock, FakeFsmApi, RealishFsm, catalog, jarvis_with_fsm, resource

TODAY = date(2026, 10, 7)
D = lambda days: (TODAY + timedelta(days=days)).isoformat()  # noqa: E731


def vehicles_resource():
    return resource("vehicles", "assets", [("id", "integer"), "registration", "driver", ("mot_due", "date"), ("road_tax_due", "date"),
                                           ("service_due", "date"), ("last_service_date", "date"), "mot_status", "status"],
                    description="Company vehicles with MOT and road tax")


def kit_resource():
    return resource("test_equipment", "assets", [("id", "integer"), "name", "serial_number", ("calibration_due", "date"), "status",
                                                 "assigned_to"], description="Test kit and its calibration")


def combined_resource():
    return resource("company_assets", "assets", [("id", "integer"), "name", "asset_type", "registration", ("mot_expiry", "date"),
                                                 ("tax_expiry", "date"), ("calibration_due", "date"), ("next_inspection", "date"),
                                                 "serial_number"], description="Company assets: vehicles, test kit, ladders")


def van(reg, mot=None, tax=None, service=None, driver="Sam Real", status="active", **extra):
    return {"id": 1, "registration": reg, "driver": driver, "mot_due": mot, "road_tax_due": tax, "service_due": service,
            "last_service_date": "2020-01-01", "mot_status": "pass", "status": status, **extra}


def kit(name, cal=None, serial="SN1", status="active"):
    return {"id": 1, "name": name, "serial_number": serial, "calibration_due": cal, "status": status, "assigned_to": None}


def cat_of(*res, off=()):
    return catalog(off=off, resources=[resource("jobs", "operations", ["id"]), *res])


class Ram:
    def __init__(self, regs, demo=False, fail=False):
        self.regs, self.demo, self.fail = regs, demo, fail

    async def vehicles(self):
        if self.fail:
            raise RuntimeError("RAM down")
        return [{"id": f"R{n}", "registration": r, "driver": None} for n, r in enumerate(self.regs)]


class Notifier:
    def __init__(self):
        self.sent = []

    async def notify(self, title, body="", **kw):
        self.sent.append((title, body, kw.get("level")))


EMPTY_REGISTER = "\n".join(f"{section}: []" for section in ("accreditations", "calibration", "vehicles", "equipment", "insurance",
                                                             "policies")) + "\n"


def make_acc(tmp_path, api, ram=None, notifier=None, example=False):
    """An Accreditations wired to the mocked FSM. By default the register file is a real but EMPTY one, so the only dated items are the
    FSM's; ``example=True`` leaves no file, i.e. the example file's placeholders (what a fresh install shows)."""
    clock = Clock()
    fsm = RealishFsm(api, tmp_path / "http")
    data = FsmData(fsm, clock=clock, sleep=clock.sleep)
    acc = Accreditations(SimpleNamespace(data_dir=tmp_path), None, None, None, notifier or Notifier(), None, None, fsm_data=data, ram=ram)
    acc._clock = clock
    if not example:
        acc.path.write_text(EMPTY_REGISTER, encoding="utf-8")
    return acc, fsm, clock


def timeline(acc, today=TODAY):
    return {t["what"]: t for t in acc.status(today)["timeline"]}


# --------------------------------------------------------------------------- which field means what
def res_of(raw):
    return parse_catalog({"version": "x", "groups": {"assets": {"enabled": True}}, "resources": [raw]}).resources[raw["name"]]


def test_field_roles_are_found_from_the_catalog_names_and_types():
    r = fa.roles(res_of(vehicles_resource()))
    assert (r.reg, r.mot, r.tax, r.service, r.driver, r.status) == ("registration", "mot_due", "road_tax_due", "service_due", "driver",
                                                                    "status")
    k = fa.roles(res_of(kit_resource()))
    assert (k.cal, k.name, k.serial, k.driver, k.reg, k.mot) == ("calibration_due", "name", "serial_number", "assigned_to", None, None)
    c = fa.roles(res_of(combined_resource()))
    assert (c.reg, c.mot, c.tax, c.cal, c.due, c.kind) == ("registration", "mot_expiry", "tax_expiry", "calibration_due",
                                                          "next_inspection", "asset_type")


@pytest.mark.parametrize("name,expected", [("mot_due", "mot_due"), ("motExpiry", "motExpiry"), ("MOT_expiry_date", "MOT_expiry_date"),
                                           ("next_mot", "next_mot")])
def test_mot_field_spellings(name, expected):
    r = fa.roles(res_of(resource("v", "assets", ["registration", (name, "date")])))
    assert r.mot == expected


def test_history_fields_are_never_taken_for_a_due_date():
    r = fa.roles(res_of(resource("v", "assets", ["registration", ("last_service_date", "date"), "mot_status", ("mot_last_test", "date"),
                                                 ("vat_due", "date"), ("tax_paid_on", "date")])))
    assert r.service is None and r.mot is None and r.tax is None       # nothing here is a due date: no false 'overdue'


def test_insurance_fields_are_ignored_the_fleet_has_one_policy():
    r = fa.roles(res_of(resource("v", "assets", ["registration", ("insurance_due", "date"), ("mot_due", "date")])))
    assert r.mot == "mot_due" and r.due is None      # (never taken for a generic 'due' date either) ...
    snap = fa.AssetSnapshot(0, "v", ["v"], True, False)
    fa.interpret(res_of(resource("v", "assets", ["registration", ("insurance_due", "date"), ("mot_due", "date")])),
                 [{"registration": "AB12 CDE", "insurance_due": "2027-01-01", "mot_due": "2026-12-01"}], snap)
    assert "insurance_due" not in snap.vehicles[0] and snap.equipment == []     # ... so a van never gets one


def test_which_resources_hold_vans_or_dated_equipment():
    assert fa.classify_resource(res_of(vehicles_resource())) == (True, False)
    assert fa.classify_resource(res_of(kit_resource())) == (False, True)
    assert fa.classify_resource(res_of(combined_resource())) == (True, True)
    assert fa.classify_resource(res_of(resource("notes", "assets", ["id", "text"]))) == (False, False)
    cat = parse_catalog(cat_of(vehicles_resource(), resource("notes", "assets", ["id", "text"])))
    assert [r.name for r in fa.asset_resources(cat)] == ["vehicles"]
    assert fa.asset_resources(parse_catalog(cat_of(vehicles_resource(), off=("assets",)))) == []     # scope off: nothing usable


@pytest.mark.parametrize("raw,expected", [("2026-11-02", date(2026, 11, 2)), ("2026-11-02T00:00:00Z", date(2026, 11, 2)),
                                          ("02/11/2026", date(2026, 11, 2)), ("", None), (None, None), ("soon", None),
                                          ("31/02/2026", None)])
def test_dates_are_read_in_the_forms_the_fsm_might_send(raw, expected):
    assert fa.to_date(raw) == expected


# --------------------------------------------------------------------------- reading the rows
async def test_vans_equipment_and_missing_dates_are_read_from_two_resources(tmp_path):
    api = FakeFsmApi(cat_of(vehicles_resource(), kit_resource()), {
        "vehicles": [van("ab12cde", D(30), D(60), D(10)), van("XY34 ZZZ", None, D(400), None, driver=None),
                     van("OLD1 VAN", D(1), D(1), status="Sold")],
        "test_equipment": [kit("Loop tester", D(200), "L-1"), kit("Sound meter", None, "S-9"), kit("Scrapped meter", D(5), status="retired")],
        "jobs": []})
    acc, fsm, _ = make_acc(tmp_path, api)
    assert (await acc.refresh_fsm_assets())["source"] == "Salts FSM"
    st = acc.status(TODAY)
    assert st["fleet_source"].startswith("Salts FSM Company Assets (vehicles, test_equipment)")
    assert [v["registration"] for v in st["vehicles"]] == ["AB12CDE", "XY34 ZZZ"]       # the sold van is not a van any more
    tl = timeline(acc)
    assert tl["Van AB12CDE (Sam Real) - MOT due"]["days_left"] == 30 and tl["Van AB12CDE (Sam Real) - road tax due"]["days_left"] == 60
    assert tl["Van AB12CDE (Sam Real) - service due"]["days_left"] == 10
    assert tl["Loop tester - calibration due"]["date"] == D(200) and tl["Loop tester - calibration due"]["detail"] == "L-1"
    assert not any("insurance" in k for k in tl) and not any("Scrapped" in k for k in tl) and not any("OLD1" in k for k in tl)
    # no recorded date: reported as such, neither compliant nor overdue, and not a timeline entry (so no reminder)
    missing = {n["what"]: n["detail"] for n in st["not_recorded"]}
    assert missing == {"Van XY34 ZZZ (pool) - MOT": "date not recorded", "Van XY34 ZZZ (pool) - service": "date not recorded",
                       "Sound meter - calibration due": "date not recorded"}
    assert not any("XY34 ZZZ" in k and "MOT" in k for k in tl) and not any("Sound meter" in k for k in tl)
    assert tl["Van XY34 ZZZ (pool) - road tax due"]["overdue"] is False
    await fsm.aclose()


async def test_one_combined_assets_table_is_classified_row_by_row(tmp_path):
    api = FakeFsmApi(cat_of(combined_resource()), {"company_assets": [
        {"id": 1, "name": "Transit 1", "asset_type": "Vehicle", "registration": "AB12 CDE", "mot_expiry": D(20), "tax_expiry": D(40),
         "calibration_due": None, "next_inspection": None, "serial_number": None},
        {"id": 2, "name": "Insulation tester", "asset_type": "Test equipment", "registration": None, "mot_expiry": None, "tax_expiry": None,
         "calibration_due": D(90), "next_inspection": None, "serial_number": "IR-7"},
        {"id": 3, "name": "Step ladder", "asset_type": "Equipment", "registration": None, "mot_expiry": None, "tax_expiry": None,
         "calibration_due": None, "next_inspection": D(15), "serial_number": None}], "jobs": []})
    acc, fsm, _ = make_acc(tmp_path, api)
    await acc.refresh_fsm_assets()
    tl = timeline(acc)
    assert set(tl) == {"Van AB12 CDE (pool) - MOT due", "Van AB12 CDE (pool) - road tax due", "Insulation tester - calibration due",
                       "Step ladder - inspection due"}
    assert tl["Step ladder - inspection due"]["days_left"] == 15
    await fsm.aclose()


# --------------------------------------------------------------------------- the reminders run from the FSM's dates
@pytest.mark.parametrize("days_left", REMIND_AT_DAYS)
async def test_a_reminder_goes_out_at_each_of_90_60_30_14_7_1_days(tmp_path, days_left):
    api = FakeFsmApi(cat_of(vehicles_resource()), {"vehicles": [van("AB12 CDE", D(days_left), D(400), D(400))], "jobs": []})
    notifier = Notifier()
    acc, fsm, _ = make_acc(tmp_path, api, notifier=notifier)
    assert await acc.daily_reminders(TODAY) == 1
    title, body, level = notifier.sent[0]
    assert title == f"Van AB12 CDE (Sam Real) - MOT due in {days_left} days" and f"Due {D(days_left)}" in body
    assert level == ("warning" if days_left <= 30 else "info")
    await fsm.aclose()


async def test_no_reminder_between_the_milestones_and_none_for_a_missing_date(tmp_path):
    api = FakeFsmApi(cat_of(vehicles_resource(), kit_resource()), {
        "vehicles": [van("AB12 CDE", D(45), None, None)], "test_equipment": [kit("Sound meter", None)], "jobs": []})
    notifier = Notifier()
    acc, fsm, _ = make_acc(tmp_path, api, notifier=notifier)
    assert await acc.daily_reminders(TODAY) == 0 and notifier.sent == []
    await fsm.aclose()


async def test_today_and_overdue_follow_the_existing_rules_with_fsm_dates(tmp_path):
    api = FakeFsmApi(cat_of(vehicles_resource()), {"vehicles": [van("AB12 CDE", D(0), D(-7), D(-3))], "jobs": []})
    notifier = Notifier()
    acc, fsm, _ = make_acc(tmp_path, api, notifier=notifier)
    assert await acc.daily_reminders(TODAY) == 2
    assert sorted(t for t, _, _ in notifier.sent) == ["Van AB12 CDE (Sam Real) - MOT due is today",
                                                      "Van AB12 CDE (Sam Real) - road tax due is OVERDUE"]
    await fsm.aclose()


async def test_the_reminders_refresh_from_the_fsm_first(tmp_path):
    api = FakeFsmApi(cat_of(vehicles_resource()), {"vehicles": [van("AB12 CDE", D(30), D(400), D(400))], "jobs": []})
    notifier = Notifier()
    acc, fsm, clock = make_acc(tmp_path, api, notifier=notifier)
    await acc.daily_reminders(TODAY)
    assert len(notifier.sent) == 1
    api.rows["vehicles"] = [van("AB12 CDE", D(14), D(400), D(400))]     # the FSM's date changed overnight
    clock.now += 86400
    await acc.daily_reminders(TODAY)
    assert notifier.sent[-1][0].endswith("MOT due in 14 days")
    await fsm.aclose()


# --------------------------------------------------------------------------- the placeholders and the hand-typed register
async def test_the_demo_placeholders_are_ignored_once_the_fsm_supplies_the_dates(tmp_path):
    api = FakeFsmApi(cat_of(vehicles_resource(), kit_resource()), {"vehicles": [van("AB12 CDE", D(20))], "test_equipment": [], "jobs": []})
    acc, fsm, _ = make_acc(tmp_path, api, example=True)
    before = acc.status(TODAY)
    assert any("YD71 SFS" in t["what"] for t in before["timeline"]) and "PLACEHOLDER" in before["note"]   # no FSM yet: the example
    await acc.refresh_fsm_assets()
    after = acc.status(TODAY)
    whats = [t["what"] for t in after["timeline"]]
    assert not any(w for w in whats if "YD71" in w or "YD72" in w or "Ladders" in w or "Harness" in w or "Portable" in w
                   or "Insulation resistance" in w or "Sound level" in w)
    assert [v["registration"] for v in after["vehicles"]] == ["AB12 CDE"] and after["equipment"] == []
    assert "Vans and equipment come from the Salts FSM" in after["note"] and "PLACEHOLDER" in after["note"]   # the rest is still example
    assert any("BAFE" in w for w in whats)    # accreditation dates are not an FSM asset: unchanged
    await fsm.aclose()


async def test_real_register_entries_the_fsm_does_not_know_are_flagged_as_check_this_not_silently_lost(tmp_path):
    api = FakeFsmApi(cat_of(vehicles_resource(), kit_resource()), {"vehicles": [van("AB12 CDE", D(20))], "test_equipment": [], "jobs": []})
    acc, fsm, _ = make_acc(tmp_path, api)
    acc.update_vehicle("ZZ99 ZZZ", {"mot_due": D(5)})
    acc.update_equipment("Ladders", {"next_due": D(5)})
    await acc.refresh_fsm_assets()
    st = acc.status(TODAY)
    assert not any("ZZ99" in t["what"] or "Ladders" in t["what"] for t in st["timeline"])
    assert any("ZZ99 ZZZ" in c and "check this" in c for c in st["fleet_check"])
    assert any("1 equipment item" in c for c in st["fleet_check"])
    await fsm.aclose()


@pytest.mark.parametrize("how", ["no assets group", "scope off", "old fsm 404", "nothing recognisable"])
async def test_without_a_usable_assets_group_the_register_file_is_still_the_source(tmp_path, how):
    api = FakeFsmApi(cat_of(vehicles_resource(), kit_resource(), off=("assets",)) if how == "scope off"
                     else cat_of(resource("notes", "assets", ["id", "text"])) if how == "nothing recognisable" else cat_of(),
                     {"vehicles": [van("AB12 CDE", D(5))], "test_equipment": [], "jobs": []})
    if how == "old fsm 404":
        api.override = lambda req, n: httpx.Response(404, text="Not Found")
    acc, fsm, _ = make_acc(tmp_path, api)
    acc.update_vehicle("AB12 CDE", {"driver": "Sam Real", "mot_due": D(30)})
    acc.update_equipment("Ladders", {"check": "inspection due", "next_due": D(15)})
    out = await acc.refresh_fsm_assets()
    assert out["source"] == "register file" and not acc.fsm_manages("vehicles") and not acc.fsm_manages("equipment")
    st = acc.status(TODAY)
    assert "fleet_source" not in st and "example" not in st["source"] and "note" not in st
    assert {t["what"] for t in st["timeline"]} == {"Van AB12 CDE (Sam Real) - MOT due", "Ladders - inspection due"}
    await fsm.aclose()


async def test_the_example_placeholders_still_show_when_there_is_no_fsm_and_no_real_register(tmp_path):
    acc = Accreditations(SimpleNamespace(data_dir=tmp_path), None, None, None, None, None, None)     # as before this change
    st = acc.status(TODAY)
    assert st["source"].startswith("example") and "PLACEHOLDER" in st["note"] and any("YD71" in t["what"] for t in st["timeline"])
    assert (await acc.refresh_fsm_assets())["source"] == "register file"
    assert acc.fsm_manages("vehicles") is False


async def test_the_register_tools_are_refused_once_the_fsm_supplies_the_dates(settings):
    api = FakeFsmApi(cat_of(vehicles_resource(), kit_resource()), {"vehicles": [van("AB12 CDE", D(20))], "test_equipment": [], "jobs": []})
    j, _ = jarvis_with_fsm(settings, api)
    try:
        calls = {"vehicle_update": {"registration": "AB12 CDE", "mot_due": "2026-12-01"}, "vehicle_remove": {"registration": "AB12 CDE"},
                 "equipment_update": {"item": "Ladders", "next_due": "2026-12-01"}, "equipment_remove": {"item": "Ladders"}}
        # before the FSM is known: queued for approval exactly as always
        queued = await dispatch(j, TOOLS_BY_NAME["vehicle_update"], TOOLS_BY_NAME["vehicle_update"].model.model_validate(calls["vehicle_update"]))
        assert "Suggested, not done" in queued
        pending_id = j.db.pending_actions()[0]["id"]
        await j.accreditations.refresh_fsm_assets()
        for name, args in calls.items():       # now: said plainly, nothing new queued
            out = await dispatch(j, TOOLS_BY_NAME[name], TOOLS_BY_NAME[name].model.model_validate(args))
            assert out == FSM_MANAGED
        assert len(j.db.pending_actions()) == 1
        await j.actions.approve(pending_id)    # the one queued earlier fails visibly instead of writing an ignored date
        await asyncio.sleep(0.05)
        action = j.db.get_action(pending_id)
        assert action["status"] == "failed" and "read from the Salts FSM" in str(action.get("result"))
        assert not j.accreditations.path.exists()
        assert all(sa.classify(f"tool:{n}", {"tool": n, "args": a}, j.db) is None for n, a in calls.items())   # standing approvals: still no
    finally:
        await j.http.aclose()


# --------------------------------------------------------------------------- the snapshot's life
async def test_a_blip_keeps_the_snapshot_an_old_fsm_clears_it_and_a_stale_one_expires(tmp_path):
    api = FakeFsmApi(cat_of(vehicles_resource()), {"vehicles": [van("AB12 CDE", D(20))], "jobs": []})
    acc, fsm, clock = make_acc(tmp_path, api)
    await acc.refresh_fsm_assets()
    assert acc.fsm_manages("vehicles")
    api.override = lambda req, n: httpx.Response(503, text="blip")
    clock.now += 700
    out = await acc.refresh_fsm_assets()
    assert out["source"] == "Salts FSM" and "error" in out and acc.fsm_manages("vehicles")     # kept: better than going silent
    clock.now += FSM_SNAPSHOT_MAX_AGE_S + 1
    assert not acc.fsm_manages("vehicles")                                                     # ... but not for ever
    api.override = lambda req, n: httpx.Response(404, text="gone")
    clock.now += 4000
    await acc.refresh_fsm_assets()
    assert acc._assets is None
    await fsm.aclose()


async def test_refresh_is_skipped_while_the_snapshot_is_fresh(tmp_path):
    api = FakeFsmApi(cat_of(vehicles_resource()), {"vehicles": [van("AB12 CDE", D(20))], "jobs": []})
    acc, fsm, clock = make_acc(tmp_path, api)
    await acc.refresh_fsm_assets()
    n = len(api.requests)
    assert (await acc.refresh_fsm_assets(max_age_s=300)).get("cached") is True and len(api.requests) == n
    clock.now += 301
    await acc.refresh_fsm_assets(max_age_s=300)
    assert len(api.requests) > n
    await fsm.aclose()


async def test_a_demo_fsm_never_feeds_the_register(tmp_path):
    api = FakeFsmApi(cat_of(vehicles_resource()), {"vehicles": [van("AB12 CDE", D(20))]})
    clock = Clock()
    fsm = RealishFsm(api, tmp_path / "h", demo=True)
    acc = Accreditations(SimpleNamespace(data_dir=tmp_path), None, None, None, None, None, None, fsm_data=FsmData(fsm, clock=clock))
    assert (await acc.refresh_fsm_assets())["source"] == "register file" and api.requests == []
    await fsm.aclose()


async def test_an_unreadable_asset_resource_is_noted_but_the_others_still_count(tmp_path):
    api = FakeFsmApi(cat_of(vehicles_resource(), kit_resource()), {"vehicles": [van("AB12 CDE", D(20))], "jobs": []})   # no test_equipment rows: 404
    acc, fsm, _ = make_acc(tmp_path, api)
    await acc.refresh_fsm_assets()
    st = acc.status(TODAY)
    assert "Van AB12 CDE (Sam Real) - MOT due" in timeline(acc) and any("test_equipment" in p for p in st["fsm_problems"])
    await fsm.aclose()


# --------------------------------------------------------------------------- RAM Tracking cross-check
async def test_vans_ram_knows_and_the_fsm_does_not_and_the_reverse_are_listed_as_check_this(tmp_path):
    api = FakeFsmApi(cat_of(vehicles_resource()), {"vehicles": [van("AB12 CDE", D(20)), van("XY34 ZZZ", D(20))], "jobs": []})
    acc, fsm, _ = make_acc(tmp_path, api, ram=Ram(["ab12cde", "QQ55 QQQ", "RR66-RRR"]))
    await acc.refresh_fsm_assets()
    check = acc.status(TODAY)["fleet_check"]
    assert check == ["RAM Tracking has 2 vans that are not in the FSM vehicle register: QQ55 QQQ, RR66-RRR - check this.",
                     "The FSM vehicle register has 1 van that RAM Tracking doesn't know: XY34 ZZZ - check this."]
    # read-only: nothing queued or changed by noticing
    assert acc.path.read_text(encoding="utf-8") == EMPTY_REGISTER
    await fsm.aclose()


async def test_matching_registrations_give_no_check_items_whatever_the_spacing_or_case(tmp_path):
    api = FakeFsmApi(cat_of(vehicles_resource()), {"vehicles": [van("AB12 CDE", D(20))], "jobs": []})
    acc, fsm, _ = make_acc(tmp_path, api, ram=Ram(["ab12-cde"]))
    await acc.refresh_fsm_assets()
    assert acc.status(TODAY)["fleet_check"] == []
    await fsm.aclose()


async def test_ram_demo_or_down_skips_the_cross_check_quietly(tmp_path):
    api = FakeFsmApi(cat_of(vehicles_resource()), {"vehicles": [van("AB12 CDE", D(20))], "jobs": []})
    for ram in (Ram(["ZZ99 ZZZ"], demo=True), None):
        acc, fsm, _ = make_acc(tmp_path, api, ram=ram)
        await acc.refresh_fsm_assets()
        assert acc.status(TODAY)["fleet_check"] == [] and timeline(acc)
        await fsm.aclose()
    acc, fsm, _ = make_acc(tmp_path, api, ram=Ram([], fail=True))
    await acc.refresh_fsm_assets()
    st = acc.status(TODAY)
    assert st["fleet_check"] == [] and any("RAM Tracking could not be read" in p for p in st["fsm_problems"])
    await fsm.aclose()


def test_a_long_list_of_mismatches_is_summarised():
    regs = [f"AA{n:02d} AAA" for n in range(12)]
    (line,) = fa.cross_check([], [{"registration": r} for r in regs])
    assert "12 vans" in line and "and 4 more" in line and "AA00 AAA" in line and "AA11 AAA" not in line


# --------------------------------------------------------------------------- the status tool and the scheduler
async def test_accreditations_status_tool_reads_the_fsm_and_the_job_keeps_it_fresh(settings):
    api = FakeFsmApi(cat_of(vehicles_resource()), {"vehicles": [van("AB12 CDE", D(20))], "jobs": []})
    j, _ = jarvis_with_fsm(settings, api)
    try:
        out = await dispatch(j, TOOLS_BY_NAME["accreditations_status"], TOOLS_BY_NAME["accreditations_status"].model())
        assert out["fleet_source"].startswith("Salts FSM") and any("AB12 CDE" in t["what"] for t in out["timeline"])
        assert not any("YD71" in t["what"] for t in out["timeline"])
        from jarvis.services.scheduler import build_scheduler
        job = build_scheduler(j).get_job("fsm_data_catalog")
        assert job is not None
        n = len(api.requests)
        await job.func()
        assert len(api.requests) > n and j.accreditations.fsm_manages("vehicles")
    finally:
        await j.http.aclose()


async def test_the_old_tools_and_the_register_still_work_on_a_demo_fsm(settings):
    from jarvis.core import Jarvis
    from tests.fakes import FakeClient

    j = Jarvis(settings, client=FakeClient())      # demo FSM: no data API, register file is the source (nothing regresses)
    try:
        out = await dispatch(j, TOOLS_BY_NAME["vehicle_update"],
                             TOOLS_BY_NAME["vehicle_update"].model.model_validate({"registration": "AB12 CDE", "mot_due": "2026-12-01"}))
        assert "Suggested, not done" in out
        await j.actions.approve(j.db.pending_actions()[0]["id"])
        await asyncio.sleep(0.05)
        assert yaml.safe_load(j.accreditations.path.read_text(encoding="utf-8"))["vehicles"][0]["registration"] == "AB12 CDE"
    finally:
        await j.http.aclose()
