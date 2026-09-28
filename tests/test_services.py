from datetime import date, timedelta

import pytest

from jarvis.db import Database
from jarvis.integrations.finance import CsvFinance
from jarvis.integrations.fsm import DemoFSM
from jarvis.integrations.voice import speakable
from jarvis.knowledge import KnowledgeBase
from jarvis.config import ROOT_DIR
from jarvis.services.performance import StaffRegister, _assess
from jarvis.services.staff import StaffMonitor
from jarvis.services.stores import Stores
from jarvis.services.tracking import Tracker, drive_minutes, haversine_m


def test_assess_flags_material_shortfalls_only():
    ok = _assess({"jobs_per_day": 2.45, "utilisation_pct": 69}, {"jobs_per_day": 2.5, "utilisation_pct": 70})
    assert ok["status"] == "on track" and ok["minor_only"]
    below = _assess({"jobs_per_day": 2.0}, {"jobs_per_day": 2.5})
    assert below["status"] == "below expectations"
    concern = _assess({"jobs_per_day": 1.0, "revisit_rate_pct": 30}, {"jobs_per_day": 2.5, "revisit_rate_pct_max": 10})
    assert concern["status"] == "concern"
    assert _assess({}, {"quotes_per_week": 10})["not_measured"]


def test_register_upsert_roundtrip(tmp_path):
    reg = StaffRegister(tmp_path / "staff.yaml")
    assert reg.load()["_source"].startswith("example")
    reg.upsert("Jo Bloggs", role="Estimator", type_="office", add_duties=["Quote remedials"],
               expectations={"quotes_per_week": 12})
    assert reg.find("jo")["name"] == "Jo Bloggs"  # exact first name beats "Josh"
    person = reg.find("Jo Bloggs")
    assert person["role"] == "Estimator" and person["expectations"]["quotes_per_week"] == 12
    assert person["expectations"]["emails_sent_per_day"] == 12  # office default merged in


async def test_productivity_and_board():
    staff = StaffMonitor(DemoFSM())
    prod = await staff.productivity(30)
    names = {r["engineer"] for r in prod["engineers"]}
    assert "Dan Harper" in names and prod["team"]["total_jobs_completed"] > 50
    dan = next(r for r in prod["engineers"] if r["engineer"] == "Dan Harper")
    assert 0 < dan["utilisation_pct"] <= 100 and dan["jobs_per_day"] > 2
    board = await staff.board()
    assert len(board["engineers"]) == 6


def test_stores_ledger(tmp_path):
    stores = Stores(Database(tmp_path / "db.sqlite"))
    stores.upsert_item("BAT-7", name="12V 7Ah battery", unit_cost=15, reorder_level=10, reorder_qty=20, supplier="Acme")
    stores.move("receive", "BAT-7", 12)
    stores.move("transfer", "battery", 5, from_loc="Stores", to_loc="Van - Dan")
    stores.move("issue", "BAT-7", 2, from_loc="van - dan", job_ref="J1")
    with pytest.raises(ValueError):
        stores.move("issue", "BAT-7", 50, from_loc="Stores")
    lv = stores.levels()["items"][0]
    assert lv["stores"] == 7 and lv["vans"] == {"Van - Dan": 3} and lv["below_reorder"]
    po = stores.reorder_list()["purchase_orders"][0]
    assert po["supplier"] == "Acme" and po["lines"][0]["order_qty"] == 20
    take = stores.stocktake("Stores", {"BAT-7": 6})
    assert take["lines"][0]["variance"] == -1 and take["net_variance_value"] == -15
    assert stores.job_materials("J1")["materials_cost"] == 30


def test_csv_finance_reads_sage50_style_export(tmp_path):
    (tmp_path / "sales_invoices.csv").write_text(
        "Invoice No,A/C Name,Date,Due Date,Net,VAT,Gross,Outstanding\n"
        "1001,Acme Ltd,01/08/2026,31/08/2026,\"1,000.00\",200.00,\"1,200.00\",\"1,200.00\"\n"
        "1002,Beta plc,05/08/2026,04/09/2026,500.00,100.00,600.00,0\n")
    (tmp_path / "bank.csv").write_text("account,balance\nCurrent,\"12,500.50\"\n")
    import asyncio

    fin = CsvFinance(tmp_path)
    outstanding = asyncio.run(fin.invoices("receivable"))
    assert len(outstanding) == 1 and outstanding[0].total == 1200 and outstanding[0].due_date == date(2026, 8, 31)
    assert len(asyncio.run(fin.invoices("receivable", outstanding_only=False))) == 2
    assert asyncio.run(fin.bank_balances())[0].balance == 12500.5


async def test_tracking_van_day_and_nearest():
    fsm = DemoFSM()
    from jarvis.integrations.ramtracking import DemoRamTracking

    tracker = Tracker(fsm, http=None, ram=DemoRamTracking(fsm))
    day = date.today() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    van = await tracker.van_day("Priya", day)
    if "timeline" in van:  # engineer may have had the day off in the demo data
        assert van["set_off"] < van["got_home"] and van["timeline"][-1]["to"] == "Home"
    near = await tracker.nearest("Aire Valley")
    assert near["destination"] == "Aire Valley Care Home" and near["engineers"]


def test_geo_helpers():
    leeds, bradford = (53.7997, -1.5492), (53.7960, -1.7594)
    assert 13_000 < haversine_m(leeds, bradford) < 15_000
    assert 25 < drive_minutes(haversine_m(leeds, bradford)) < 40


def test_speakable_strips_markdown():
    text = "## Cash\n**£73k** at bank - see [report](http://x).\n| a | b |\n|---|---|\n```py\nprint(1)\n```\ne.g. more"
    out = speakable(text)
    assert "**" not in out and "##" not in out and "http" not in out and "|" not in out
    assert "report" in out and "code shown on screen" in out


def test_knowledge_search_finds_standards():
    kb = KnowledgeBase(ROOT_DIR / "knowledge")
    hits = kb.search("how often should emergency lighting have a full duration test")
    assert hits and any("emergency" in h["doc"] for h in hits)
    assert "Salts FSM" in kb.core_documents()


async def test_unbilled_jobs_and_invoice_approval(tmp_path):
    from jarvis.config import Settings
    from jarvis.integrations.finance import DemoFinance
    from jarvis.services.billing import Billing

    class Actions:
        def __init__(self):
            self.queued = []

        def queue(self, kind, summary, payload):
            self.queued.append((kind, payload))
            return len(self.queued)

    settings = Settings(data_dir=tmp_path, _env_file=None)
    actions = Actions()
    billing = Billing(settings, Database(tmp_path / "db"), DemoFSM(), DemoFinance(), actions, None)
    unbilled = await billing.unbilled_jobs(30)
    assert unbilled["count"] > 0 and unbilled["net_total"] > 0
    result = await billing.queue_invoices(30, limit=5)
    assert result["queued"] == 5 and actions.queued[0][0] == "sage_invoices"
    msg = await billing.create_invoices(actions.queued[0][1]["jobs"])
    assert "raise it manually" in msg  # demo accounts can't create invoices
    assert (await billing.queue_review_requests())["queued"] == 0  # no review link configured


async def test_elevenlabs_request_and_deepgram_url():
    import json as _json

    import httpx

    from jarvis.config import Settings
    from jarvis.integrations.voice import Voice

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("xi-api-key")
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, content=b"ID3fake-mp3")

    s = Settings(elevenlabs_api_key="el-key", deepgram_api_key="dg", _env_file=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        voice = Voice(s, http)
        stream, mime = await voice.tts_stream("**Good evening**, sir.")
        audio = b"".join([chunk async for chunk in stream])
    assert mime == "audio/mpeg" and audio.startswith(b"ID3")
    assert "/text-to-speech/onwK4e9ZLuTAKqWW03F9/stream" in seen["url"] and "mp3_44100_128" in seen["url"]
    assert seen["key"] == "el-key" and seen["body"]["text"] == "Good evening, sir."
    assert seen["body"]["model_id"] == "eleven_flash_v2_5"
    url = voice.deepgram_live_url()
    assert url.startswith("wss://api.deepgram.com/v1/listen?model=nova-3") and "language=en-GB" in url
    assert "keyterm=Jarvis" in url and "interim_results=true" in url


async def test_remedial_pipeline_flags_stalled_quotes():
    from jarvis.services.remedials import remedial_pipeline

    data = await remedial_pipeline(DemoFSM())
    assert data["open"] and data["open_value"] > 0
    chased = {r["quote"]: r["action"] for r in data["needs_chasing"]}
    assert any("second chase" in a for a in chased.values()) and any("first chase" in a for a in chased.values())
    assert all(r["age_days"] >= 7 for r in data["needs_chasing"])
    assert data["win_rate_pct"] is not None


async def test_customer_health_flags_the_drifting_customer(tmp_path):
    from jarvis.config import Settings
    from jarvis.core import Jarvis
    from tests.fakes import FakeClient

    j = Jarvis(Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None), client=FakeClient())
    health = await j.customers.scores()
    kestrel = next(c for c in health["customers"] if c["customer"] == "Kestrel Retail")
    assert kestrel["status"] == "at risk" and kestrel["renewal_in_days"] == 38
    assert any("work down" in r for r in kestrel["reasons"]) and any("overdue" in r for r in kestrel["reasons"])
    assert kestrel["suggested_actions"][0].startswith("call them before the renewal")
    assert any(c["status"] == "healthy" for c in health["customers"])
    suggestions = await j.suggestions.sweep(announce=False)
    assert any(s["key"] == "customer:Kestrel Retail" for s in suggestions)
    assert (await j.customers.customer("kestrel"))["customer"] == "Kestrel Retail"
    await j.http.aclose()
