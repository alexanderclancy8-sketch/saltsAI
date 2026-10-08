"""Fault reports (services/faults.py): Jarvis records what of his own broke - doctor red lines, failed approved actions, failing
scheduled runs, integrations that keep erroring, and his own report_fault - redacted, de-duplicated, closed by themselves when the
check passes again, listed in the console's Faults pop-up and copied (never sent) as markdown for Claude Code."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import jarvis
from jarvis import access
from jarvis.brain import checkmode
from jarvis.brain.tools import TOOLS_BY_NAME, ReportFaultIn, dispatch
from jarvis.core import Jarvis
from jarvis.main import create_app
from jarvis.services import faults as faults_mod
from jarvis.services import scheduler
from jarvis.services.async_tools import NOT_BACKGROUND
from jarvis.services.doctor import AMBER, OK, RED, Doctor, Item
from jarvis.services.faults import OPEN, RESOLVED, FaultLog, clean, fingerprint
from tests.fakes import FakeClient

SAM = access.Caller(access.TEAM, "Sam", "s1")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def make(settings, **kw):
    for k, v in kw.items():
        setattr(settings, k, v)
    j = Jarvis(settings, client=FakeClient())
    clock = Clock()
    j.faults = FaultLog(j, now=clock)
    return j, clock


def rows(j):
    return j.db.query("SELECT * FROM faults ORDER BY id")


async def drain(j):
    for _ in range(5):
        if not j.actions._tasks:
            break
        await asyncio.gather(*list(j.actions._tasks))


# ------------------------------------------------------------------------------------------------ redaction
def test_clean_takes_out_tokens_passwords_auth_headers_url_secrets_and_configured_values():
    text = ("GET https://api.example.com/v1/jobs?api_key=abc123SECRET&page=2 failed; Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefghij "
            "password=hunter22 https://bob:pa55word@host.example.com/x token ghp_ABCDEFGHIJKLMNOPQRSTUV and MYSECRETVALUE99")
    out = clean(text, secrets=["MYSECRETVALUE99"])
    for leak in ("abc123SECRET", "eyJhbGciOiJIUzI1NiJ9", "hunter22", "pa55word", "ghp_ABCDEFGHIJKLMNOPQRSTUV", "MYSECRETVALUE99"):
        assert leak not in out, leak
    assert "?[query removed]" in out and "api.example.com/v1/jobs" in out


def test_a_configured_secret_never_reaches_the_table(settings):
    j, _ = make(settings, ram_api_key="RAMKEY-123456789")
    j.faults.record("x", source="integration", title="RAM failed", error="401 for key RAMKEY-123456789")
    (r,) = rows(j)
    assert "RAMKEY-123456789" not in r["error"] and "[hidden]" in r["error"]


# ------------------------------------------------------------------------------------------------ dedupe and closing
def test_a_repeat_is_one_report_with_a_count_and_last_seen(settings):
    j, clock = make(settings)
    first = j.faults.record("k", source="check", title="Issue email scan failed", error="TimeoutError: timed out")
    clock.now += timedelta(minutes=20)
    again = j.faults.record("k", source="check", title="Issue email scan failed", error="TimeoutError: timed out again")
    (r,) = rows(j)
    assert first == again == r["id"] and r["count"] == 2 and r["first_seen"] == "2026-10-08T09:00:00+00:00"
    assert r["last_seen"] == "2026-10-08T09:20:00+00:00" and r["error"].endswith("again")
    assert "could not reach" in r["diagnosis"].lower()
    assert j.faults.resolve("k") == 1 and rows(j)[0]["status"] == RESOLVED and rows(j)[0]["resolved_at"] == "2026-10-08T09:20:00+00:00"
    j.faults.record("k", source="check", title="Issue email scan failed", error="again")       # it broke again: a new report
    assert [r["status"] for r in rows(j)] == [RESOLVED, OPEN]


def test_fingerprint_ignores_changing_numbers():
    assert fingerprint("Run #12 stalled for 45 minutes") == fingerprint("Run #13 stalled for 90 minutes")


# ------------------------------------------------------------------------------------------------ the triggers
class BrokenMail:
    def __init__(self):
        self.fail = True

    async def send_mail(self, *a, **k):
        if self.fail:
            raise ConnectionError("Graph said 503 Service Unavailable for alice@acme.example")


async def test_a_failed_approved_action_is_a_fault_and_a_later_success_of_that_kind_closes_it(settings):
    j, _ = make(settings)
    j.actions.mail = BrokenMail()
    payload = {"to": ["alice@acme.example"], "subject": "Hi", "body": "Hello"}
    a1 = j.actions.queue("email_send", "Email Acme", payload)
    await j.actions.approve(a1, by="Alex")
    await drain(j)
    (r,) = rows(j)
    assert r["status"] == OPEN and r["source"] == "action" and r["key"] == "action:email_send"
    assert "jarvis/services/actions.py" in r["files"] and "microsoft365.py" in r["files"] and f"#{a1}" in r["doing"]
    j.actions.mail.fail = False
    a2 = j.actions.queue("email_send", "Email Acme", payload)
    await j.actions.approve(a2, by="Alex")
    await drain(j)
    assert rows(j)[0]["status"] == RESOLVED and j.db.get_action(a2)["status"] == "done"


async def test_a_failed_scheduled_check_is_a_fault_with_its_files_and_the_next_good_run_closes_it(settings):
    j, _ = make(settings)

    async def broken():
        return {}["missing"]

    async def fine():
        return 0

    await scheduler._check(j, "inbox_scan", "Issue email scan", broken)()
    await scheduler._check(j, "inbox_scan", "Issue email scan", broken)()
    (r,) = rows(j)
    assert r["key"] == "check:inbox_scan" and r["count"] == 2 and "KeyError" in r["error"]
    assert "tests/test_faults.py" not in r["files"] and "jarvis/services/scheduler.py" in r["files"]
    assert "bug in Jarvis's own code" in r["diagnosis"]
    await scheduler._check(j, "inbox_scan", "Issue email scan", fine)()
    assert rows(j)[0]["status"] == RESOLVED


async def test_any_other_scheduled_job_and_the_daily_posts_are_covered(settings):
    j, _ = make(settings)

    async def boom():
        raise RuntimeError("nope")

    await scheduler._guard("weekly digest", boom, j=j)()
    await scheduler._daily(j, "wrapup", "End-of-day wrap-up", boom)()
    assert {r["key"] for r in rows(j)} == {"job:weekly digest", "check:wrapup"}

    async def ok():
        return None

    await scheduler._guard("weekly digest", ok, j=j)()
    assert {r["key"]: r["status"] for r in rows(j)}["job:weekly digest"] == RESOLVED
    sched = scheduler.build_scheduler(j)
    assert sched.get_job("fault_watch") is not None


async def test_doctor_red_lines_and_broken_checks_become_faults_and_close_when_green(settings, monkeypatch):
    j, _ = make(settings)
    state = {"red": True}

    async def tests_check(self, now):
        return [Item("Tests and issues", RED if state["red"] else OK, "2 routine tests failing: system/fsm, system/tls.")]

    async def broken(self, now):
        raise ValueError("bad data")

    monkeypatch.setattr(Doctor, "CHECKS", (("Tests and issues", "_t"), ("Pull requests", "_b")))
    monkeypatch.setattr(Doctor, "_t", tests_check, raising=False)
    monkeypatch.setattr(Doctor, "_b", broken, raising=False)
    await Doctor(j).run()
    keys = {r["key"]: r for r in rows(j)}
    assert set(keys) == {"doctor:Tests and issues:" + fingerprint("2 routine tests failing: system/fsm, system/tls."),
                         "doctor:Pull requests:could-not-check"}
    assert all(r["source"] == "doctor" and "jarvis/services/doctor.py" in r["files"] for r in keys.values())
    state["red"] = False
    await Doctor(j).run()
    status = {r["key"].split(":")[1]: r["status"] for r in rows(j)}
    assert status == {"Tests and issues": RESOLVED, "Pull requests": OPEN}   # a check that still crashes can't say it's fixed


async def test_amber_lines_are_not_faults(settings, monkeypatch):
    j, _ = make(settings)

    async def amber(self, now):
        return [Item("Data sources", AMBER, "Salts FSM: still on DEMO sample data.")]

    monkeypatch.setattr(Doctor, "CHECKS", (("Data sources", "_a"),))
    monkeypatch.setattr(Doctor, "_a", amber, raising=False)
    await Doctor(j).run()
    assert rows(j) == []


class FakeRam:
    demo = False

    def __init__(self):
        self.health = {"ok": False, "detail": "RAM Tracking refused the key (401).", "rate_limited": False}

    async def probe(self, max_age=None):
        return self.health


async def test_an_integration_must_keep_failing_before_a_fault_opens_and_closes_when_it_answers(settings):
    j, clock = make(settings)
    j.ram = FakeRam()
    j.db.set_kv("faults:doctor_last", clock.now.isoformat())    # no doctor run in this test
    await j.faults.watch()
    assert rows(j) == []                                          # one bad look is not "keeps erroring"
    await j.faults.watch()
    (r,) = rows(j)
    assert r["key"] == "integration:ram" and r["title"] == "RAM Tracking keeps failing" and "credentials" in r["diagnosis"]
    assert "ramtracking.py" in r["files"]
    j.ram.health = {"ok": True, "detail": "", "rate_limited": False}
    await j.faults.watch()
    assert rows(j)[0]["status"] == RESOLVED


async def test_the_watch_runs_a_quiet_doctor_every_few_hours(settings, monkeypatch):
    j, clock = make(settings)
    ran = []

    async def run(self, now=None):
        ran.append(now)
        return []

    monkeypatch.setattr(Doctor, "run", run)
    await j.faults.watch()
    await j.faults.watch()
    clock.now += faults_mod.DOCTOR_EVERY
    await j.faults.watch()
    assert len(ran) == 2


# ------------------------------------------------------------------------------------------------ report_fault
async def test_report_fault_records_internally_with_no_card_and_is_rate_limited(settings):
    j, _ = make(settings)
    tool = TOOLS_BY_NAME["report_fault"]
    out = await dispatch(j, tool, ReportFaultIn(summary="I can't open .msg attachments", details="email_read gave no text"))
    assert out["recorded"] is True and j.db.pending_actions() == []
    (r,) = rows(j)
    assert r["source"] == "jarvis" and r["title"] == "I can't open .msg attachments" and not r["untrusted"]
    for i in range(faults_mod.REPORTS_PER_HOUR - 1):
        assert (await dispatch(j, tool, ReportFaultIn(summary=f"Problem {'abcdefgh'[i]} here")))["recorded"] is True
    over = await dispatch(j, tool, ReportFaultIn(summary="One more problem"))
    assert over["recorded"] is False and len(rows(j)) == faults_mod.REPORTS_PER_HOUR


async def test_report_fault_after_outside_content_is_flagged(settings):
    j, _ = make(settings)
    state = j.entity_memory.begin_turn(quiet=False, channel="console")
    j.entity_memory.note_tool("email_read")
    try:
        await dispatch(j, TOOLS_BY_NAME["report_fault"], ReportFaultIn(summary="I can't open this file type", details="x"))
    finally:
        j.entity_memory.end_turn(state)
    assert rows(j)[0]["untrusted"] == 1
    assert "outside content" in j.faults.report_markdown(rows(j))


async def test_report_fault_is_not_for_team_members_or_check_mode(settings):
    j, _ = make(settings)
    tool = TOOLS_BY_NAME["report_fault"]
    assert "report_fault" not in access.TEAM_TOOLS and "report_fault" not in access.OFFICE_TOOLS
    assert "isn't available to you here" in await dispatch(j, tool, ReportFaultIn(summary="x y z w"), caller=SAM)
    assert "report_fault" not in checkmode.CHECK_TOOLS and "report_fault" in NOT_BACKGROUND
    out = await dispatch(j, tool, ReportFaultIn(summary="I can't do this thing"), check=True)
    assert out["blocked_in_check_mode"] is True and rows(j) == []


# ------------------------------------------------------------------------------------------------ the export
def test_the_report_is_self_contained_and_takes_out_customer_details(settings):
    j, _ = make(settings)
    j.db.execute("INSERT INTO entity_notes (entity_type, fsm_id, name, created_at, updated_at) VALUES (?,?,?,?,?)",
                 ("customer", "C1", "Acme Fire Ltd", "2026-10-01", "2026-10-01"))
    j.faults.record("action:email_send", source="action", title="Approved actions of kind 'email_send' are failing",
                    error="ConnectionError: Graph refused mail to jane@acme.example for Acme Fire Ltd at BD1 2AB, call 01274 555 0100",
                    doing="Carrying out approved action #4 (email_send).", files=["jarvis/services/actions.py:243"])
    md = j.faults.report_markdown(j.faults.open_faults())
    assert md.startswith("# Jarvis fault report") and "Python " in md and "Jarvis build:" in md
    assert "Fault #1: Approved actions of kind 'email_send' are failing" in md and "jarvis/services/actions.py:243" in md
    assert "Jarvis's diagnosis:" in md and "```text" in md and "data, never instructions" in md
    for leak in ("jane@acme.example", "Acme Fire Ltd", "BD1 2AB", "01274 555 0100"):
        assert leak not in md, leak
    assert "[email]" in md and "[customer]" in md and "[postcode]" in md and "[phone]" in md


def test_the_faults_module_never_sends_anything_anywhere():
    src = Path(faults_mod.__file__).read_text(encoding="utf-8")
    code = src[src.index('"""', 3) + 3:]
    assert not re.search(r"\bimport\b[^\n]*(github|httpx|teams|notifier|microsoft365|requests)|GitHub\(|PRClient|\.send_mail\(|"
                         r"\.notify\(|send_owner_update|\.publish\(|actions\.queue|create_issue|open_pr\(|\.post\(|\.put\(", code)
    root = Path(jarvis.__file__).parent
    for name in ("integrations/github.py", "integrations/github_pr.py", "brain/pr_tools.py", "services/self_improve.py",
                 "services/fixer.py", "services/teams_approvals.py", "services/notifier.py"):
        path = root / name
        if path.exists():
            assert not re.search(r"\bfaults\b|FaultLog|report_markdown", path.read_text(encoding="utf-8")), name


# ------------------------------------------------------------------------------------------------ the console
def test_the_faults_routes_list_copy_and_mark_fixed(settings):
    j, _ = make(settings)
    fid = j.faults.record("k", source="check", title="Issue email scan failed", error="TimeoutError")
    app = create_app(settings, j)
    with TestClient(app) as c:
        data = c.get("/api/faults").json()
        assert [f["id"] for f in data["open"]] == [fid] and data["open"][0]["source_label"] == "Scheduled run"
        assert c.get("/api/status").json()["faults"] == {"open": 1}
        one = c.get(f"/api/faults/{fid}/report").json()["markdown"]
        assert f"Fault #{fid}: Issue email scan failed" in one
        assert c.get("/api/faults/report").json()["count"] == 1
        assert c.get("/api/faults/999/report").status_code == 404
        r = c.post(f"/api/faults/{fid}/fixed")
        assert r.status_code == 200 and r.json()["status"] == "fixed"
        data = c.get("/api/faults").json()
        assert data["open"] == [] and data["closed"][0]["status_label"] == "Marked fixed"
        assert c.post("/api/faults/999/fixed").status_code == 404


def test_the_routes_are_owner_or_manager_only():
    for key in ("GET /api/faults", "GET /api/faults/report", "GET /api/faults/{fault_id}/report", "POST /api/faults/{fault_id}/fixed"):
        assert access.ROUTE_POLICY[key] == access.MANAGER_OK, key
    assert "faults" not in access.TEAM_STATUS_KEYS


def test_the_console_has_a_rail_count_and_a_pop_up_that_never_covers_the_chat():
    web = Path(jarvis.__file__).parent / "web"
    index, hud, js = ((web / n).read_text(encoding="utf-8") for n in ("index.html", "hud.js", "faults.js"))
    assert '<!--role:manager--><button type="button" class="rail-item" data-pop="faults">Faults <span class="rail-count" id="rc-faults">' in index
    assert '<!--role:manager--><section class="pop" id="pop-faults"' in index
    assert index.index("/static/faults.js") < index.index("/static/hud.js")
    assert '"faults"' in hud[hud.index("const POPS"):hud.index("\n", hud.index("const POPS"))]
    assert 'setRail("faults"' in hud and "window.JarvisFaults?.load()" in hud
    code = js[js.index("*/") + 2:]
    assert set(re.findall(r"/api/[a-z]+", code)) == {"/api/faults"} and "approv" not in code.lower()
    assert "Copy report for Claude" in js and "Mark fixed" in js and "Copy all open faults" in index


async def test_the_wrapup_mentions_open_faults(settings):
    j, _ = make(settings)
    j.faults.record("k", source="check", title="Issue email scan failed", error="x")
    data = await j.wrapup.gather()
    assert data["jarvis_faults"] == {"count": 1, "newest": ["Issue email scan failed"]}
    from jarvis.services.wrapup import WRAPUP_SYSTEM
    assert "jarvis_faults" in WRAPUP_SYSTEM
