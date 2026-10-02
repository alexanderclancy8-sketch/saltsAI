"""Van and equipment compliance dates (MOT, service, insurance, tax, ladders, harnesses, PAT): the owner can tell Jarvis
a date, it queues for approval, lands in the real (git-ignored) register and then feeds the Alerts timeline - and the
example file's placeholder vans, drivers and dates never become 'real' data."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from jarvis.brain.tools import (TOOLS, TOOLS_BY_NAME, EquipmentRemoveIn, EquipmentUpdateIn, VehicleRemoveIn,
                                VehicleUpdateIn, dispatch)
from jarvis.core import Jarvis
from jarvis.services import standing_approvals as sa
from jarvis.services.accreditations import Accreditations, parse_date
from tests.fakes import FakeClient

TODAY = date(2026, 10, 2)
FLEET_TOOLS = ("vehicle_update", "vehicle_remove", "equipment_update", "equipment_remove")


def make_acc(tmp_path):
    return Accreditations(SimpleNamespace(data_dir=tmp_path), None, None, None, None, None, None)


def real(acc):
    return yaml.safe_load(acc.path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- dates
def test_parse_date_accepts_iso_uk_and_spoken_forms():
    assert parse_date("2026-11-02", TODAY) == date(2026, 11, 2)
    assert parse_date("02/11/2026", TODAY) == date(2026, 11, 2)
    assert parse_date("2/11/26", TODAY) == date(2026, 11, 2)
    assert parse_date("2 November 2026", TODAY) == date(2026, 11, 2)
    assert parse_date("2nd Nov 2026", TODAY) == date(2026, 11, 2)
    assert parse_date("November 2, 2026", TODAY) == date(2026, 11, 2)
    assert parse_date(date(2026, 11, 2), TODAY) == date(2026, 11, 2)


def test_a_date_with_no_year_means_the_next_time_it_comes_round():
    assert parse_date("2 November", TODAY) == date(2026, 11, 2)
    assert parse_date("2 October", TODAY) == date(2026, 10, 2)  # today counts
    assert parse_date("1 October", TODAY) == date(2027, 10, 1)  # already gone this year -> next year


@pytest.mark.parametrize("bad", ["", "soon", "2026-13-01", "2026-02-30", "31 February 2026", "32/01/2026",
                                 "1999-01-01", "2090-01-01", "next tuesday", "2 Smarch 2026", "11/2026"])
def test_parse_date_rejects_what_is_not_a_real_sensible_date(bad):
    with pytest.raises(ValueError):
        parse_date(bad, TODAY)


# --------------------------------------------------------------------------- vehicles: upsert
def test_vehicle_upsert_creates_then_updates_only_the_fields_given(tmp_path):
    acc = make_acc(tmp_path)
    out = acc.update_vehicle("YD71 SFS", {"driver": "Dan Harper", "mot_due": "2026-11-02", "service_due": None})
    assert out["action"] == "added" and out["vehicle"]["mot_due"] == "2026-11-02"
    out = acc.update_vehicle("YD71 SFS", {"service_due": "20 October 2026", "mot_due": "", "driver": None})
    assert out["action"] == "updated"
    van = real(acc)["vehicles"][0]
    assert van["registration"] == "YD71 SFS" and van["driver"] == "Dan Harper"  # untouched fields stay
    assert van["mot_due"] == date(2026, 11, 2) and van["service_due"] == date(2026, 10, 20)
    assert "insurance_due" not in van and len(real(acc)["vehicles"]) == 1
    assert "2026-11-02" in acc.path.read_text(encoding="utf-8")  # a plain YAML date, like the example file's


@pytest.mark.parametrize("typed", ["yd71 sfs", "YD71SFS", "  yd71   sfs ", "Yd71-Sfs"])
def test_registration_is_matched_case_and_spacing_blind(tmp_path, typed):
    acc = make_acc(tmp_path)
    acc.update_vehicle("YD71 SFS", {"mot_due": "2026-11-02"})
    out = acc.update_vehicle(typed, {"tax_due": "2027-03-01"})
    assert out["action"] == "updated"
    vans = real(acc)["vehicles"]
    assert len(vans) == 1 and vans[0]["registration"] == "YD71 SFS" and vans[0]["tax_due"] == date(2027, 3, 1)


def test_a_new_registration_is_stored_upper_case_with_the_usual_space(tmp_path):
    acc = make_acc(tmp_path)
    acc.update_vehicle("yd72sfs", {"mot_due": "2027-02-14"})
    acc.update_vehicle("k1 ngs", {})  # an older/private plate: kept as typed, just tidied
    assert [v["registration"] for v in real(acc)["vehicles"]] == ["YD72 SFS", "K1 NGS"]


@pytest.mark.parametrize("reg", ["", "   ", "---", "YD71 SFS; rm -rf", "A" * 30, "YD71\nSFS<script>"])
def test_nonsense_registrations_are_refused(tmp_path, reg):
    acc = make_acc(tmp_path)
    with pytest.raises(ValueError):
        acc.update_vehicle(reg, {"mot_due": "2026-11-02"})
    assert not acc.path.exists()


@pytest.mark.parametrize("fields", [{"mot_due": "soon"}, {"tax_due": "2026-02-30"}, {"service_due": "1999-01-01"},
                                    {"insurance_due": "32/13/2026"}])
def test_bad_dates_are_rejected_and_nothing_is_written(tmp_path, fields):
    acc = make_acc(tmp_path)
    with pytest.raises(ValueError):
        acc.update_vehicle("YD71 SFS", fields)
    assert not acc.path.exists()
    acc.update_vehicle("YD71 SFS", {"mot_due": "2026-11-02"})
    before = acc.path.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        acc.update_vehicle("YD71 SFS", {"mot_due": "2026-11-09", **fields})  # one bad field spoils the whole change
    assert acc.path.read_text(encoding="utf-8") == before


def test_unknown_fields_are_refused(tmp_path):
    acc = make_acc(tmp_path)
    with pytest.raises(ValueError, match="colour"):
        acc.update_vehicle("YD71 SFS", {"colour": "white", "mot_due": "2026-11-02"})
    with pytest.raises(ValueError, match="holder"):
        acc.update_equipment("Ladders", {"holder": "Dan", "next_due": "2026-11-02"})
    assert not acc.path.exists()


# --------------------------------------------------------------------------- equipment
def test_equipment_upsert_matches_by_name_without_duplicating(tmp_path):
    acc = make_acc(tmp_path)
    acc.update_equipment("Ladders and steps (all vans)", {"check": "inspection due", "next_due": "2026-10-15"})
    acc.update_equipment("Harnesses / fall arrest", {"check": "6-monthly inspection due", "next_due": "2026-11-20"})
    assert acc.update_equipment("ladders and steps (ALL vans)", {"next_due": "2027-04-15"})["action"] == "updated"
    assert acc.update_equipment("Ladders", {"next_due": "2027-05-01"})["action"] == "updated"  # one clear match
    rows = real(acc)["equipment"]
    assert [r["item"] for r in rows] == ["Ladders and steps (all vans)", "Harnesses / fall arrest"]
    assert rows[0]["next_due"] == date(2027, 5, 1) and rows[0]["check"] == "inspection due"
    assert acc.update_equipment("PAT testing", {"check": "PAT test due", "next_due": "1 December"})["action"] == "added"
    assert len(real(acc)["equipment"]) == 3
    # a longer name than one already recorded is a different item, not a rename of it
    assert acc.update_equipment("Harnesses / fall arrest (spare set)", {"next_due": "2027-01-01"})["action"] == "added"
    assert len(real(acc)["equipment"]) == 4


def test_an_ambiguous_equipment_name_is_refused_not_guessed(tmp_path):
    acc = make_acc(tmp_path)
    acc.update_equipment("Ladders (vans)", {"next_due": "2026-10-15"})
    acc.update_equipment("Ladders (office)", {"next_due": "2026-10-16"})
    with pytest.raises(ValueError, match="several"):
        acc.update_equipment("Ladders", {"next_due": "2027-01-01"})
    assert [r["next_due"] for r in real(acc)["equipment"]] == [date(2026, 10, 15), date(2026, 10, 16)]


def test_equipment_bad_date_rejected(tmp_path):
    acc = make_acc(tmp_path)
    with pytest.raises(ValueError):
        acc.update_equipment("Ladders", {"next_due": "whenever"})
    assert not acc.path.exists()


# --------------------------------------------------------------------------- placeholders stay out of the real file
def test_the_example_placeholders_are_never_copied_into_the_real_file(tmp_path):
    acc = make_acc(tmp_path)
    acc.update_vehicle("AB12 CDE", {"driver": "Sam Real", "mot_due": "2026-12-01"})
    text = acc.path.read_text(encoding="utf-8")
    data = real(acc)
    for placeholder in ("YD71", "YD72", "Dan Harper", "Priya Shah", "Ladders and steps", "Harnesses", "BAFE", "SSAIB",
                        "Public liability", "Quality manual", "Insulation resistance"):
        assert placeholder not in text
    assert [v["registration"] for v in data["vehicles"]] == ["AB12 CDE"]
    # same layout as the example, every other section present but empty
    for section in ("accreditations", "calibration", "equipment", "insurance", "policies"):
        assert data[section] == []
    assert text.startswith("# Accreditations register")


def test_equipment_and_scheme_updates_do_not_copy_placeholders_either(tmp_path):
    acc = make_acc(tmp_path / "a")
    acc.update_equipment("Ladders", {"next_due": "2026-10-15"})
    assert real(acc)["vehicles"] == [] and real(acc)["accreditations"] == []
    acc = make_acc(tmp_path / "b")
    acc.update("CHAS", {"renewal_date": "2027-01-31"})  # the pre-existing scheme tool shares the safe starting point
    data = real(acc)
    assert [a["scheme"] for a in data["accreditations"]] == ["CHAS"] and data["vehicles"] == []


def test_status_reads_the_real_file_once_it_exists_not_the_example(tmp_path):
    acc = make_acc(tmp_path)
    before = acc.status(TODAY)
    assert before["source"].startswith("example") and "PLACEHOLDER" in before["note"]
    assert any(t["what"].startswith("Van YD71") for t in before["timeline"])
    acc.update_vehicle("AB12 CDE", {"driver": "Sam Real", "mot_due": "2026-11-02", "tax_due": "2026-10-01"})
    acc.update_equipment("Ladders", {"check": "inspection due", "next_due": "2026-10-15"})
    after = acc.status(TODAY)
    assert "example" not in after["source"] and "note" not in after
    assert [t["what"] for t in after["timeline"]] == [
        "Van AB12 CDE (Sam Real) - road tax due", "Ladders - inspection due", "Van AB12 CDE (Sam Real) - MOT due"]
    tax = after["timeline"][0]
    assert tax["overdue"] is True and tax["days_left"] == -1
    assert after["vehicles"][0]["registration"] == "AB12 CDE" and after["equipment"][0]["item"] == "Ladders"
    assert not any("YD71" in t["what"] or "Harness" in t["what"] for t in after["timeline"])


async def test_daily_reminders_use_the_recorded_dates(tmp_path):
    sent = []

    class Notifier:
        async def notify(self, title, body="", **kw):
            sent.append(title)

    acc = Accreditations(SimpleNamespace(data_dir=tmp_path), None, None, None, Notifier(), None, None)
    acc.update_vehicle("AB12 CDE", {"driver": "Sam Real", "mot_due": date.today().isoformat()})
    assert await acc.daily_reminders() == 1
    assert sent == ["Van AB12 CDE (Sam Real) - MOT due is today"]


# --------------------------------------------------------------------------- the file itself
def test_a_hand_written_header_survives_and_the_write_is_atomic(tmp_path):
    acc = make_acc(tmp_path)
    acc.path.write_text("# Our own notes\n# keep these\n\nvehicles:\n  - registration: AB12 CDE\n    mot_due: 2026-11-02\n"
                        "equipment: []\n", encoding="utf-8")
    acc.update_vehicle("ab12cde", {"service_due": "2026-12-01"})
    text = acc.path.read_text(encoding="utf-8")
    assert text.startswith("# Our own notes\n# keep these\n")
    assert real(acc)["vehicles"][0]["service_due"] == date(2026, 12, 1)
    assert [p.name for p in tmp_path.iterdir()] == ["accreditations.yaml"]  # no temp file left behind


def test_an_unreadable_real_file_is_never_overwritten(tmp_path):
    acc = make_acc(tmp_path)
    acc.path.write_text("vehicles: [unclosed\n  - nope: : :", encoding="utf-8")
    original = acc.path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="nothing was changed"):
        acc.update_vehicle("AB12 CDE", {"mot_due": "2026-11-02"})
    acc.path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="nothing was changed"):
        acc.update_equipment("Ladders", {"next_due": "2026-11-02"})
    assert acc.path.read_text(encoding="utf-8") == "- just\n- a list\n" and original.startswith("vehicles:")


def test_an_empty_section_in_a_hand_edited_file_is_fine(tmp_path):
    acc = make_acc(tmp_path)
    acc.path.write_text("accreditations:\nvehicles:\nequipment:\n", encoding="utf-8")
    assert acc.status(TODAY)["timeline"] == []
    acc.update_vehicle("AB12 CDE", {"mot_due": "2026-11-02"})
    assert len(real(acc)["vehicles"]) == 1


# --------------------------------------------------------------------------- remove
def test_a_sold_van_and_a_retired_item_can_be_removed(tmp_path):
    acc = make_acc(tmp_path)
    acc.update_vehicle("AB12 CDE", {"mot_due": "2026-11-02"})
    acc.update_vehicle("XY34 ZZZ", {"mot_due": "2026-12-02"})
    acc.update_equipment("Ladders", {"next_due": "2026-10-15"})
    acc.update_equipment("Ladders (old)", {"next_due": "2026-10-16"})
    assert acc.remove_vehicle("ab12cde")["vehicle"]["registration"] == "AB12 CDE"
    assert [v["registration"] for v in real(acc)["vehicles"]] == ["XY34 ZZZ"]
    with pytest.raises(ValueError, match="No van AB12 CDE"):
        acc.remove_vehicle("AB12 CDE")
    # equipment removal is exact: 'Ladders' must not take 'Ladders (old)' with it
    acc.remove_equipment("LADDERS")
    assert [e["item"] for e in real(acc)["equipment"]] == ["Ladders (old)"]
    with pytest.raises(ValueError, match="recorded: Ladders \\(old\\)"):
        acc.remove_equipment("Lad")
    assert all("AB12" not in t["what"] for t in acc.status(TODAY)["timeline"])


def test_removing_with_no_real_register_does_nothing_and_never_touches_the_placeholders(tmp_path):
    acc = make_acc(tmp_path)
    with pytest.raises(ValueError, match="none recorded"):
        acc.remove_vehicle("YD71 SFS")  # that's the example's van - not a record
    with pytest.raises(ValueError, match="none recorded"):
        acc.remove_equipment("Harnesses / fall arrest")
    assert not acc.path.exists()


# --------------------------------------------------------------------------- the tools
def test_tool_inputs_validate_dates_normalise_them_and_refuse_extras():
    ok = VehicleUpdateIn(registration="yd71 sfs", mot_due="2 November 2026", tax_due="2027-03-01")
    assert ok.mot_due == "2026-11-02" and ok.tax_due == "2027-03-01" and ok.service_due is None
    assert EquipmentUpdateIn(item="Ladders", next_due="15/10/2026").next_due == "2026-10-15"
    assert VehicleUpdateIn(registration="AB12 CDE", mot_due="").mot_due is None
    with pytest.raises(ValidationError):
        VehicleUpdateIn(registration="YD71 SFS", mot_due="next week")
    with pytest.raises(ValidationError):
        EquipmentUpdateIn(item="Ladders", next_due="2026-02-30")
    assert set(VehicleUpdateIn.model_fields) == {"registration", "driver", "mot_due", "service_due", "insurance_due",
                                                 "tax_due"}
    assert set(EquipmentUpdateIn.model_fields) == {"item", "check", "next_due"}
    assert set(VehicleRemoveIn.model_fields) == {"registration"} and set(EquipmentRemoveIn.model_fields) == {"item"}


def test_the_four_tools_exist_and_all_need_approval():
    for name in FLEET_TOOLS:
        assert name in TOOLS_BY_NAME and TOOLS_BY_NAME[name].approval is True
    assert len([t for t in TOOLS if t.name in FLEET_TOOLS]) == 4


def make_jarvis(settings, **overrides):
    for key, value in overrides.items():
        setattr(settings, key, value)
    return Jarvis(settings, client=FakeClient())


CALLS = {
    "vehicle_update": {"registration": "yd71 sfs", "driver": "Dan Harper", "mot_due": "2026-11-02"},
    "vehicle_remove": {"registration": "YD71 SFS"},
    "equipment_update": {"item": "Ladders", "check": "inspection due", "next_due": "2026-10-15"},
    "equipment_remove": {"item": "Ladders"},
}


async def test_the_owner_can_say_a_date_it_queues_and_only_lands_once_approved(settings):
    j = make_jarvis(settings)
    try:
        tool = TOOLS_BY_NAME["vehicle_update"]
        result = await dispatch(j, tool, tool.model.model_validate(CALLS["vehicle_update"]))
        assert "not done" in result.lower() and "approves it" in result
        assert not j.accreditations.path.exists()  # nothing written before approval
        pending = j.db.pending_actions()
        assert len(pending) == 1 and pending[0]["kind"] == "tool:vehicle_update"
        assert pending[0]["summary"] == "Record van YD71 SFS: driver=Dan Harper, mot_due=2026-11-02"
        # before approval the Alerts still say 'placeholder example'
        assert j.accreditations.status()["source"].startswith("example")

        await j.actions.approve(pending[0]["id"])
        await asyncio.sleep(0.05)
        assert j.db.get_action(pending[0]["id"])["status"] == "done"
        status = j.accreditations.status()
        assert "example" not in status["source"]
        assert [t["what"] for t in status["timeline"]] == ["Van YD71 SFS (Dan Harper) - MOT due"]
        assert status["timeline"][0]["date"] == "2026-11-02"
    finally:
        await j.http.aclose()


async def test_every_fleet_tool_queues_and_never_runs(settings):
    j = make_jarvis(settings)
    try:
        for name in FLEET_TOOLS:
            tool = TOOLS_BY_NAME[name]
            out = await dispatch(j, tool, tool.model.model_validate(CALLS[name]))
            assert "Suggested, not done" in out
        await asyncio.sleep(0.05)
        assert [a["status"] for a in j.db.pending_actions()] == ["pending"] * 4
        assert not j.accreditations.path.exists()
    finally:
        await j.http.aclose()


async def test_an_approved_bad_removal_fails_visibly_instead_of_pretending(settings):
    j = make_jarvis(settings)
    try:
        tool = TOOLS_BY_NAME["vehicle_remove"]
        await dispatch(j, tool, tool.model.model_validate(CALLS["vehicle_remove"]))
        action_id = j.db.pending_actions()[0]["id"]
        await j.actions.approve(action_id)
        await asyncio.sleep(0.05)
        action = j.db.get_action(action_id)
        assert action["status"] == "failed" and "No van YD71 SFS" in str(action.get("result"))
    finally:
        await j.http.aclose()


# --------------------------------------------------------------------------- standing approvals (PR #66)
async def test_standing_approvals_never_cover_the_fleet_tools_even_with_both_switches_on(settings):
    j = make_jarvis(settings, standing_record_keeping=True, standing_acknowledgements=True)
    try:
        assert j.settings.standing_record_keeping and j.settings.standing_acknowledgements
        for name in FLEET_TOOLS:
            tool = TOOLS_BY_NAME[name]
            await dispatch(j, tool, tool.model.model_validate(CALLS[name]))
        await asyncio.sleep(0.1)
        rows = [j.db.get_action(i) for i in range(1, 5)]
        assert [r["kind"] for r in rows] == [f"tool:{n}" for n in FLEET_TOOLS]
        assert all(r["status"] == "pending" and r["approved_by"] == "" for r in rows)
        assert not j.accreditations.path.exists()  # and nothing was written
        for r in rows:
            assert sa.classify(r["kind"], r["payload"], j.db) is None
    finally:
        await j.http.aclose()


def test_the_standing_approval_allowlist_still_names_no_fleet_tool():
    source = open(sa.__file__, encoding="utf-8").read()
    for name in ("vehicle_", "equipment_", "accreditation", "fleet"):
        assert f'"{name}' not in source and f"'{name}" not in source
    assert set(sa.CATEGORIES) == {"record keeping", "routine acknowledgements"}
