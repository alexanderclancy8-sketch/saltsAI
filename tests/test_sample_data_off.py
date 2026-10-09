"""Sample data OFF (production): an unconnected source is simply "not connected", never sample data.

The suite runs with sample data on (tests/conftest.py). These tests build Jarvis with ``sample_data=False``, which is what
Azure gets (WEBSITE_SITE_NAME is set there), and check every place the owner could otherwise have seen sample data: the
tools, the connection lines, the pop-up data, the briefing / wrap-up, the trace and coverage line, the question checks, the
doctor, the prompt and the stored suggestions - and that the one-off database clean-up removes only seeded sample rows.
"""

from __future__ import annotations

import json
import re

import pytest

from jarvis import demo_guard
from jarvis.brain import coverage as cov
from jarvis.brain import prompts
from jarvis.brain.tools import TOOLS_BY_NAME, dispatch, serialise
from jarvis.config import Settings, default_sample_data
from jarvis.core import Jarvis
from jarvis.integrations.finance import NoFinance
from jarvis.integrations.fsm import NoFSM
from jarvis.integrations.microsoft365 import NoMail
from jarvis.integrations.ramtracking import NoRamTracking
from jarvis.services import sample_cleanup
from jarvis.services.stores import DEMO_ITEMS, DEMO_VANS, STORES, Stores
from tests.fakes import FakeClient, text_block, message

DEMO_WORDS = re.compile(r"\bdemo\b|\bsample\b|DEMO", re.I)


@pytest.fixture
def off_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Settings(data_dir=tmp_path / "data", scheduler_enabled=False, anthropic_api_key="test", web_search_enabled=True,
                    sample_data=False, _env_file=None)


def make(settings, script=None):
    return Jarvis(settings, client=FakeClient(script))


async def run_tool(j, name, **args):
    tool = TOOLS_BY_NAME[name]
    return await dispatch(j, tool, tool.model.model_validate(args))


def sent_to_model(j) -> str:
    return json.dumps([(c.get("system"), c.get("messages")) for c in j.client.beta.messages.calls], default=str)


# --------------------------------------------------------------------------- the switch
def test_the_default_is_off_on_azure_and_with_a_env_file_and_on_for_a_bare_local_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JARVIS_SAMPLE_DATA", raising=False)
    for name in ("WEBSITE_SITE_NAME", "WEBSITE_INSTANCE_ID"):
        monkeypatch.delenv(name, raising=False)
    assert default_sample_data() is True                       # a bare checkout, no .env: the README's demo
    assert Settings(data_dir=tmp_path / "d", _env_file=None).sample_data is True
    (tmp_path / ".env").write_text("OWNER_NAME=Alex\n")
    assert default_sample_data() is False                      # a configured install
    (tmp_path / ".env").unlink()
    monkeypatch.setenv("WEBSITE_SITE_NAME", "jarvis-salts")    # Azure App Service, even with nothing else configured
    assert default_sample_data() is False
    assert Settings(data_dir=tmp_path / "d", _env_file=None).sample_data is False
    monkeypatch.setenv("JARVIS_SAMPLE_DATA", "1")              # explicitly switched on wins
    assert Settings(data_dir=tmp_path / "d", _env_file=None).sample_data is True
    monkeypatch.setenv("JARVIS_SAMPLE_DATA", "0")
    monkeypatch.delenv("WEBSITE_SITE_NAME")
    assert Settings(data_dir=tmp_path / "d", _env_file=None).sample_data is False


async def test_every_unconnected_source_is_a_not_connected_stand_in(off_settings):
    j = make(off_settings)
    try:
        assert isinstance(j.mail, NoMail) and isinstance(j.finance, NoFinance) and isinstance(j.ram, NoRamTracking)
        assert j.fsm.demo and await j.fsm.staff() == [] and isinstance(j.fsm._demo, NoFSM)
        # what the console reads (no tool call): nothing at all, never a sample row
        assert await j.fsm.jobs() == [] and await j.fsm.staff() == [] and await j.mail.list_messages() == []
        assert await j.finance.invoices("receivable") == [] and await j.ram.positions() == []
        assert j.stores.levels()["items"] == [] and not j.stores.demo and j.register.people() == []
        assert (await j.marketing.overview(30))["platforms"] == {}
        assert j.accreditations.status()["timeline"] == []
        assert demo_guard.demo_now(j) == set()   # nothing is sample, so the safety net never fires
        assert demo_guard.not_connected_now(j) == set(demo_guard.SOURCES)
    finally:
        await j.http.aclose()


# --------------------------------------------------------------------------- tools
@pytest.mark.parametrize("name,args,source", [
    ("finance_snapshot", {}, "accounts"), ("customer_health", {}, None), ("email_inbox", {}, "mail"),
    ("fsm_jobs", {}, "fsm"), ("stock_levels", {}, "stock"), ("marketing_overview", {}, "socials"),
    ("staff_roles", {}, "staff"), ("van_day", {"engineer": "anyone"}, None), ("fsm_data", {"resource": "jobs"}, "fsm"),
])
async def test_a_tool_says_plainly_what_is_not_connected_with_no_demo_wording(off_settings, name, args, source):
    j = make(off_settings)
    try:
        out = await run_tool(j, name, **args)
        assert demo_guard.is_not_connected(out), out
        assert "demo_data_withheld" not in out and out["where"] == "Settings → Connections"
        if source:
            assert demo_guard.SOURCES[source].label in [n["source"] for n in out["not_connected"]]
        assert not DEMO_WORDS.search(serialise(out)), serialise(out)
        # the coverage line calls it "not connected", never "sample data"
        facts = cov.call_facts(name, args, out)
        assert facts and all(f["status"] == cov.NOT_CONNECTED for f in facts)
    finally:
        await j.http.aclose()


async def test_the_connection_lines_say_not_connected_and_never_demo(off_settings):
    j = make(off_settings)
    try:
        conns = j.connections()
        assert not any("DEMO" in v for v in conns.values())
        for key in ("Email (Outlook)", "Salts FSM", "Accounts", "Socials / Google", "Stores / stock", "Vehicle tracking"):
            assert conns[key].startswith("not connected"), (key, conns[key])
        assert conns["Staff register"].startswith("not set up")
        blocks = prompts.build_system(off_settings, j.kb, j.db, conns, j.register.prompt_summary())
        text = "\n".join(b["text"] for b in blocks)
        assert "Sample data is never an answer" not in text and "Systems that aren't connected" in text
        assert "DEMO" not in text and "demo_data_withheld" not in text and "Say it once" in text
    finally:
        await j.http.aclose()


# --------------------------------------------------------------------------- the console's data
async def test_the_pop_ups_get_a_tidy_not_connected_state(off_settings):
    j = make(off_settings)
    try:
        st = await j.briefings.status()
        assert st["finance"] == {"not_connected": "Not connected yet - connect Sage in Settings → Connections"}
        assert st["staff"]["not_connected"].startswith("Not connected yet - connect Salts FSM")
        assert st["inbox"]["unread"]["not_connected"].endswith("Microsoft 365 in Settings → Connections")
        panels = demo_guard.panel_messages(j)
        assert set(panels) >= {"inbox", "staff", "finance", "presence", "fleet"}
        names = [s["name"] for s in demo_guard.not_connected_sources(j)]
        assert "Salts FSM" in names and "The accounts (Sage)" in names and len(names) == len(set(names))
    finally:
        await j.http.aclose()


async def test_the_status_api_carries_the_switch_and_the_panel_messages(off_settings):
    from fastapi.testclient import TestClient

    from jarvis.main import create_app

    j = make(off_settings)
    with TestClient(create_app(off_settings, j)) as c:
        st = c.get("/api/status").json()
    assert st["sample_data"] is False and st["not_connected"]["finance"].startswith("Not connected yet")
    assert st["finance"] == {"not_connected": st["not_connected"]["finance"]}
    assert any(s["key"] == "vehicles" for s in st["not_connected_sources"])
    assert not any("DEMO" in v for v in st["connections"].values())


async def test_with_sample_data_on_the_console_is_exactly_as_before(settings):
    j = make(settings)
    try:
        st = await j.briefings.status()
        assert "not_connected" not in st["finance"] and st["finance"]["cash_at_bank"]
        assert demo_guard.panel_messages(j) == {} and demo_guard.not_connected_now(j) == set()
        assert "DEMO" in j.connections()["Accounts"]
    finally:
        await j.http.aclose()


# --------------------------------------------------------------------------- briefing, wrap-up, advice
async def test_the_briefing_says_what_is_not_connected_in_one_line(off_settings):
    j = make(off_settings, [message([text_block("Morning. Sage and Salts FSM aren't connected yet.")])])
    try:
        await j.briefings.morning_briefing(deliver=False)
        sent = sent_to_model(j)
        assert sent.count("Not connected yet:") == 1 and "Sage" in sent
        assert not re.search(r"\bdemo\b|sample data", sent, re.I)
        assert "cash_at_bank" not in sent and '"finance"' not in sent
    finally:
        await j.http.aclose()


async def test_the_wrap_up_and_advice_leave_unconnected_parts_out_without_a_nag(off_settings):
    j = make(off_settings, [message([text_block("Wrap-up.")]), message([text_block("Advice.")])])
    try:
        data = await j.wrapup.gather()
        assert "demo" not in data and "money" not in data and "today" not in data and "unread_email" not in data
        assert "Not connected" not in json.dumps(data, default=str)
        await run_tool(j, "end_of_day_wrap_up")
        advice = await j.advisor.gather()
        assert "finance_snapshot" not in advice and "marketing" not in advice
        assert not re.search(r"\bdemo\b|sample data", json.dumps(advice, default=str), re.I)
    finally:
        await j.http.aclose()


# --------------------------------------------------------------------------- trace, coverage, checks, doctor
async def test_the_source_line_says_not_connected_and_confidence_is_not_demo(off_settings):
    j = make(off_settings)
    try:
        demo = cov.demo_map(j)
        facts = [cov._fact(cov.FSM, cov.OK, "jobs")]
        c = cov.summarise(facts, "How many jobs today?", demo=demo, sample=False)
        assert c["checked"] == [] and any(g["kind"] == cov.NOT_CONNECTED for g in c["gaps"])
        assert "sample" not in c["why"].lower() and "demo" not in json.dumps(c).lower()
        c_on = cov.summarise(facts, "How many jobs today?", demo=demo, sample=True)
        assert any(g["kind"] == cov.DEMO for g in c_on["gaps"])   # sample data on: unchanged
    finally:
        await j.http.aclose()


async def test_question_checks_are_skipped_as_not_connected(off_settings):
    j = make(off_settings)
    try:
        check = next(c for c in j.question_checks.checks()[0] if c.needs)
        assert j.question_checks.skip_reason(check, j.question_checks._demo(), {}) == "not connected"
    finally:
        await j.http.aclose()


async def test_the_doctor_lists_the_unconnected_sources_once_neutrally(off_settings):
    from jarvis.services.doctor import OK
    from tests.test_doctor import report

    j = make(off_settings)
    try:
        items = await report(j, "Data sources")
        assert len(items) == 1 and items[0].status == OK
        assert items[0].line.startswith("Not connected yet: ") and "DEMO" not in items[0].line
    finally:
        await j.http.aclose()


async def test_suggestions_are_never_built_from_sample_data(off_settings):
    j = make(off_settings)
    try:
        await j.suggestions.sweep(announce=False)
        keys = {s["key"].split(":")[0] for s in j.db.open_suggestions()}
        assert not keys & {"customer", "credit", "payrisk", "concentration", "reorder", "unbilled", "assign", "cert"}, keys
    finally:
        await j.http.aclose()


# --------------------------------------------------------------------------- the one-off clean-up
def _seed_like_a_demo_run(settings):
    """A database as an old demo run left it (sample data on), plus things a person and a real integration added."""
    s_on = settings.model_copy(update={"sample_data": True})
    j = Jarvis(s_on, client=FakeClient())
    db = j.db
    assert j.stores.demo  # seeded
    # real rows: an item a person entered, a real move on it, and a seeded item somebody has since counted
    j.stores.upsert_item("REAL-1", name="Real detector", category="Fire", unit_cost=10, reorder_level=1, reorder_qty=2)
    j.stores._add("REAL-1", STORES, 5)
    db.execute("INSERT INTO stock_moves (created_at, sku, qty, kind, from_loc, to_loc, job_ref, note) VALUES (?,?,?,?,?,?,?,?)",
               ("2026-10-01T10:00:00+00:00", "REAL-1", 1, "issue", STORES, "job", "J1", "real"))
    counted = DEMO_ITEMS[0][0]
    db.execute("INSERT INTO stock_moves (created_at, sku, qty, kind, from_loc, to_loc, job_ref, note) VALUES (?,?,?,?,?,?,?,?)",
               ("2026-10-01T10:00:00+00:00", counted, 2, "adjust", STORES, "adjustment", "", "stocktake"))
    edited = DEMO_ITEMS[1][0]
    db.execute("UPDATE stock_items SET unit_cost = 99 WHERE sku = ?", (edited,))
    # suggestions: some built from sample sources, one a real check, one with a Prepare handler
    for key in ("credit", "payrisk", "customer:Kestrel Retail", "reorder:Security Distribution UK", "assign:J23001",
                "tests", "renewal:C9"):
        db.upsert_suggestion(key, key, "", "prompt", 2)
    db.upsert_suggestion("fsm:prep:1", "Prepare", "", "prompt", 2, kind="prepare")
    db.remember("Kestrel Retail is a real customer now")   # a person's own note is never touched
    return j, counted, edited


async def test_the_clean_up_removes_only_seeded_sample_rows_once(off_settings):
    old, counted, edited = _seed_like_a_demo_run(off_settings)
    await old.http.aclose()
    seeded = {sku for sku, *_ in DEMO_ITEMS}
    j = make(off_settings)   # production start: the clean-up runs here
    try:
        db = j.db
        skus = {r["sku"] for r in db.query("SELECT sku FROM stock_items")}
        assert "REAL-1" in skus and counted in skus and edited in skus          # real and touched rows survive
        assert not (skus & seeded) - {counted, edited}                           # every untouched seed item is gone
        assert db.query_one("SELECT qty FROM stock_levels WHERE sku = 'REAL-1'")["qty"] == 5
        assert db.query("SELECT note FROM stock_moves WHERE note = 'demo'") == []
        assert {r["note"] for r in db.query("SELECT note FROM stock_moves")} == {"real", "stocktake"}
        assert not db.query(f"SELECT * FROM stock_levels WHERE location IN ({','.join('?' * len(DEMO_VANS))}) "
                            "AND sku NOT IN (?, ?)", (*DEMO_VANS, counted, edited))
        assert db.get_kv("stock_demo_seeded") == "cleared" and not j.stores.demo
        keys = {r["key"] for r in db.query("SELECT key FROM suggestions")}
        assert keys == {"tests", "fsm:prep:1"}, keys                              # FSM, Sage, mail, stock: none connected
        assert any("Kestrel" in m["fact"] for m in db.memories())
        assert db.get_kv(sample_cleanup.CLEANUP_KEY) == "done"
        # it runs once: a later row is never touched by it again
        db.upsert_suggestion("credit", "credit", "", "p", 2)
        assert sample_cleanup.remove_seeded_sample_data(db, j) == {}
        assert db.get_suggestion("credit")
    finally:
        await j.http.aclose()


async def test_the_clean_up_keeps_suggestions_whose_source_is_connected(off_settings):
    s = off_settings.model_copy(update={"fsm_base_url": "https://fsm.example.test"})
    old, _, _ = _seed_like_a_demo_run(s.model_copy(update={"fsm_base_url": ""}))
    await old.http.aclose()
    j = make(s)
    try:
        keys = {r["key"] for r in j.db.query("SELECT key FROM suggestions")}
        assert {"assign:J23001", "renewal:C9", "tests"} <= keys       # rest on Salts FSM, which is connected: real
        assert not keys & {"credit", "payrisk", "customer:Kestrel Retail"}   # rest on Sage, which isn't
    finally:
        await j.http.aclose()


async def test_with_sample_data_on_nothing_is_cleaned(settings):
    j = make(settings)
    try:
        assert j.stores.demo and j.stores.levels()["items"]
        assert j.db.get_kv(sample_cleanup.CLEANUP_KEY) is None
    finally:
        await j.http.aclose()


def test_stores_seed_only_with_sample_data(tmp_path):
    from jarvis.db import Database

    db = Database(tmp_path / "x.db")
    Stores(db, demo_seed=False, sample=False)
    assert not db.query("SELECT * FROM stock_items")
