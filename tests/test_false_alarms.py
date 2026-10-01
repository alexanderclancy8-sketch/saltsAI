"""False alarm log (BS 5839-1:2025): false-alarm and repeat call-outs are found per site and system, repeat
offenders flagged, cause/corrective action recorded only after approval, and the evidence report shows the
gaps as well as the records. Nothing here ever writes to Salts FSM."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from pydantic import ValidationError

from jarvis.brain.tools import (FalseAlarmAnalysisIn, FalseAlarmRecordIn, TOOLS_BY_NAME, dispatch,
                                false_alarm_analysis, false_alarm_evidence_report)
from jarvis.core import Jarvis
from jarvis.db import Database
from jarvis.services.false_alarms import analyse, build_report
from tests.fakes import FakeClient

TODAY = date(2026, 10, 1)


class FakeFSM:
    demo = False

    def __init__(self, jobs, systems=()):
        self._jobs, self._systems = jobs, list(systems)
        self.writes: list = []

    async def jobs(self, date_from=None, date_to=None, status=None, engineer=None):
        return [dict(j) for j in self._jobs]

    async def systems(self):
        return [dict(s) for s in self._systems]

    async def write(self, *args, **kwargs):  # must never be called by the false alarm log
        self.writes.append((args, kwargs))


def job(ref, site, jtype="callout", day="2026-09-01", status="completed", customer="Cust", **extra):
    return {"id": ref, "ref": ref, "type": jtype, "status": status, "site": site, "customer": customer,
            "engineer": "Dan Harper", "scheduled_start": f"{day}T09:00:00", "started_at": f"{day}T09:10:00",
            "extra": extra or None}


def system(sid, site, stype="fire_alarm", model="Kentec Syncro AS"):
    return {"id": sid, "site": site, "type": stype, "make_model": model}


def sites_by_name(result):
    return {s["site"]: s for s in result["sites"]}


async def test_false_alarms_are_recognised_from_job_text_and_negation_and_non_callouts_are_ignored():
    fsm = FakeFSM([
        job("J1", "Site A", description="Attended - false alarm, steam from shower"),
        job("J2", "Site A", day="2026-09-05", notes=[{"text": "Unwanted fire signal caused by cooking"}]),
        job("J3", "Site B", notes=[{"text": "Not a false alarm - genuine panel fault"}]),
        job("J4", "Site B", "service", notes="False alarm history reviewed"),
        job("J5", "Site C", status="cancelled", description="false alarm"),
        job("J6", "Site C", "false_alarm"),
        job("J7", "Site D", falseAlarm="False", description="battery fault"),
        job("J8", "Site E", "service", is_false_alarm=True),
    ])
    result = await analyse(fsm, None, today=TODAY)
    events = {e["job_ref"]: e for s in result["sites"] for e in s["events"]}
    assert {r for r, e in events.items() if e["is_false_alarm"]} == {"J1", "J2", "J6", "J8"}
    assert "J3" in events and not events["J3"]["is_false_alarm"]  # a call-out, but not a false alarm
    assert "J4" not in events and "J5" not in events  # routine service visit / cancelled job
    assert "J7" in events and not events["J7"]["is_false_alarm"]
    assert result["totals"]["false_alarms"] == 4 and result["totals"]["callouts"] == 6
    assert fsm.writes == []


async def test_repeat_offenders_flagged_per_site_and_system_with_gap_between_events():
    fsm = FakeFSM(
        [job(f"J{n}", "Site A", day=d, description="Smoke detector false alarm")
         for n, d in enumerate(("2026-07-01", "2026-08-01", "2026-09-01"), 1)]
        + [job("J9", "Site B", description="Smoke detector false alarm")],
        [system("S1", "Site A"), system("S2", "Site A", "intruder", "Texecom"), system("S3", "Site B")])
    result = await analyse(fsm, None, today=TODAY)
    a = sites_by_name(result)["Site A"]
    assert a["repeat_offender"] and a["false_alarms"] == 3
    fire = a["systems"][0]
    assert fire["system_id"] == "S1" and fire["repeat_false_alarms"] and fire["avg_days_between_callouts"] == 31.0
    assert not sites_by_name(result)["Site B"]["repeat_offender"]
    assert [r["site"] for r in result["repeat_offenders"]] == ["Site A"]
    assert result["repeat_offenders"][0]["systems"] == [fire["system"]]
    assert result["sites"][0]["site"] == "Site A"  # repeat offenders sort first


async def test_threshold_is_configurable():
    fsm = FakeFSM([job(f"J{n}", "Site A", day=f"2026-09-0{n}", description="false alarm") for n in (1, 2, 3)])
    assert (await analyse(fsm, None, repeat_threshold=4, today=TODAY))["totals"]["repeat_offender_sites"] == 0
    assert (await analyse(fsm, None, repeat_threshold=3, today=TODAY))["totals"]["repeat_offender_sites"] == 1


async def test_repeat_callouts_that_are_not_false_alarms_are_flagged_separately():
    fsm = FakeFSM([job("J1", "Site E", description="Intruder alarm battery fault"),
                   job("J2", "Site E", day="2026-09-10", description="Intruder panel battery fault again")],
                  [system("S9", "Site E", "intruder", "Texecom")])
    site = sites_by_name(await analyse(fsm, None, today=TODAY))["Site E"]
    assert site["repeat_callouts"] and not site["repeat_offender"] and site["false_alarms"] == 0
    assert site["systems"][0]["system_type"] == "intruder" and site["systems"][0]["repeat_callouts"]


async def test_system_is_named_only_when_it_can_be_identified():
    systems = [system("S3", "Site D"), system("S4", "Site D", model="Gent Vigilon")]
    fsm = FakeFSM([job("J1", "Site D", description="Smoke detector false alarm"),
                   job("J2", "Site D", day="2026-09-02", description="False alarm", systemId="S4")], systems)
    events = {e["job_ref"]: e for e in sites_by_name(await analyse(fsm, None, today=TODAY))["Site D"]["events"]}
    assert events["J1"]["system_id"] == "" and events["J1"]["system"] == "fire alarm"  # two panels: no guess
    assert events["J2"]["system_id"] == "S4" and "Gent Vigilon" in events["J2"]["system"]
    unknown = FakeFSM([job("J5", "Site D", description="false alarm")], systems)
    assert (await analyse(unknown, None, today=TODAY))["sites"][0]["events"][0]["system_type"] == "unknown"


async def test_site_filter_matches_site_or_customer():
    fsm = FakeFSM([job("J1", "Aire Valley Care Home", description="false alarm", customer="Aire Valley Care Ltd"),
                   job("J2", "Other Site", description="false alarm")])
    assert [s["site"] for s in (await analyse(fsm, None, site="aire valley", today=TODAY))["sites"]] == \
        ["Aire Valley Care Home"]


def test_log_records_merge_and_never_blank_earlier_fields():
    db = Database(":memory:")
    db.upsert_false_alarm_record("J1", "Site A", cause="Steam from shower", cause_category="cooking_steam_dust")
    row = db.upsert_false_alarm_record("J1", "Site A", corrective_action="Moved detector", cause="")
    assert row["cause"] == "Steam from shower" and row["corrective_action"] == "Moved detector"
    assert len(db.list_false_alarm_records()) == 1
    with pytest.raises(ValueError):
        db.upsert_false_alarm_record("J1", "Site A", not_a_column="x")


async def test_gaps_listed_until_cause_action_evidence_and_review_are_all_recorded():
    db = Database(":memory:")
    fsm = FakeFSM([job("J1", "Site A", description="false alarm")])

    async def gaps():
        result = await analyse(fsm, db, today=TODAY)
        return result["sites"][0]["events"][0]["gaps"], result["totals"]["false_alarms_with_open_gaps"]

    g, open_count = await gaps()
    assert len(g) == 3 and open_count == 1  # no cause, no action, no review
    db.upsert_false_alarm_record("J1", "Site A", cause="Steam", corrective_action="Moved detector")
    g, _ = await gaps()
    assert any("not evidenced" in x for x in g) and any("review" in x for x in g)
    db.upsert_false_alarm_record("J1", "Site A", evidence_ref="Q1184", reviewed_by="Alex", review_date="2026-09-20")
    g, open_count = await gaps()
    assert g == [] and open_count == 0


async def test_hand_logged_event_counts_as_a_false_alarm_even_if_fsm_text_does_not_say_so():
    db = Database(":memory:")
    db.upsert_false_alarm_record("J1", "Site A", cause="Contractor dust")
    fsm = FakeFSM([job("J1", "Site A", description="Attended, panel reset")])
    event = (await analyse(fsm, db, today=TODAY))["sites"][0]["events"][0]
    assert event["is_false_alarm"] and event["source"] == "logged by hand"


async def test_evidence_report_shows_records_and_open_gaps_and_escapes_tables():
    db = Database(":memory:")
    fsm = FakeFSM([job("J1", "Site A", description="false alarm"),
                   job("J2", "Site A", day="2026-09-10", description="false alarm")], [system("S1", "Site A")])
    db.upsert_false_alarm_record("J1", "Site A", cause="Steam | shower", corrective_action="Moved detector",
                                 evidence_ref="Q1184", investigated_by="Dan Harper", reviewed_by="Alex",
                                 review_date="2026-09-20")
    report = build_report(await analyse(fsm, db, today=TODAY), "Salts", TODAY)
    assert "## Site A" in report and "Steam / shower" in report and "Moved detector" in report
    assert "J2 (2026-09-10): cause not investigated/recorded" in report  # the gap is stated, not hidden
    assert "repeat offender" in report and "DRAFT" in report and "Reviewed by:" in report
    assert "J1 (2026-09-01)" not in report.split("**Open gaps**")[1].split("Suggested")[0]  # J1 is complete


async def test_report_is_none_when_there_are_no_callouts():
    assert build_report(await analyse(FakeFSM([job("J1", "Site A", "service")]), None, today=TODAY)) is None


# ---- tools and the approval gate -------------------------------------------------------------------
def make(settings):
    return Jarvis(settings, client=FakeClient())


def test_tools_registered_with_the_write_behind_approval():
    assert TOOLS_BY_NAME["false_alarm_analysis"].approval is False
    assert TOOLS_BY_NAME["false_alarm_evidence_report"].approval is False  # a draft on the display
    record = TOOLS_BY_NAME["false_alarm_record"]
    assert record.approval is True and record.describe is not None


def test_record_input_rejects_bad_dates_and_unknown_categories():
    with pytest.raises(ValidationError):
        FalseAlarmRecordIn(job_ref="J1", review_date="20/09/2026")
    with pytest.raises(ValidationError):
        FalseAlarmRecordIn(job_ref="J1", cause_category="aliens")
    assert FalseAlarmRecordIn(job_ref="J1", review_date="2026-09-20").review_date == "2026-09-20"


async def test_recording_is_queued_for_approval_and_only_written_to_the_log_once_approved(settings):
    j = make(settings)
    tool = TOOLS_BY_NAME["false_alarm_record"]
    args = FalseAlarmRecordIn(job_ref="J24099", cause="Contractor dust", cause_category="environmental",
                              corrective_action="Detector covers fitted during works")
    result = await dispatch(j, tool, args)
    assert "queued" in result.lower()
    assert j.db.list_false_alarm_records() == []  # nothing recorded before approval
    pending = j.db.pending_actions()
    assert len(pending) == 1 and pending[0]["kind"] == "tool:false_alarm_record"
    await j.actions.approve(pending[0]["id"])
    await asyncio.sleep(0.05)
    assert j.db.get_action(pending[0]["id"])["status"] == "done"
    rows = j.db.list_false_alarm_records()
    assert len(rows) == 1 and rows[0]["job_ref"] == "J24099" and rows[0]["site"] == "Aire Valley Care Home"
    assert rows[0]["cause"] == "Contractor dust"
    await j.http.aclose()


async def test_recording_against_an_unknown_job_needs_a_site(settings):
    j = make(settings)
    with pytest.raises(ValueError):
        await j.false_alarms.record("NOT-A-JOB", cause="x")
    row = await j.false_alarms.record("NOT-A-JOB", "Somewhere Else", cause="x")
    assert row["site"] == "Somewhere Else" and row["cause"] == "x"
    await j.http.aclose()


async def test_read_tools_work_on_demo_data_and_write_nothing(settings):
    j = make(settings)
    analysis = await false_alarm_analysis(j, FalseAlarmAnalysisIn())
    assert analysis["demo"] and analysis["totals"]["callouts"] >= 1
    report = await false_alarm_evidence_report(j, FalseAlarmAnalysisIn())
    assert report["shown_on_display"] and "DEMO DATA" in report["report"]
    assert j.db.pending_actions() == [] and j.db.list_false_alarm_records() == []
    await j.http.aclose()
