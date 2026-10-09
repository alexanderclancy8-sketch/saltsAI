"""find_similar_work (services/similar_work.py): "have we done something like this before?".

Pinned here:

* the description is reduced to plain features (system, manufacturer, building, size...) and every match says why it matched;
* past quotes and jobs come from the FSM's generic read API, bounded (one recent read + at most SEARCH_TERMS keyword searches a
  resource), and a read that stopped short says so; quote lines come from the quote itself or a separate lines resource;
* the pricing guide is worked out from the quotes' own values, says how many it rests on and is withheld below MIN_FOR_GUIDE;
* filters are hard filters; bad dates are refused;
* who sees what: the owner everything, a manager everything but an owner-only resource, an engineer / office member no money, no
  pricing guide and no email search at all;
* sample (demo) FSM data is never a source; the coverage line names Salts FSM quotes / jobs and the mailbox separately;
* it is a read for check mode, never runs in the background, its output is untrusted, and it is reachable through the brain.

Nothing here reads the wall clock; the FSM and the mailbox are fakes.
"""

from __future__ import annotations

import json
import re

import httpx

from jarvis import access
from jarvis.access import ENGINEER, OFFICE, Caller
from jarvis.brain import checkmode, coverage as cov
from jarvis.brain.tools import TOOLS_BY_NAME, dispatch
from jarvis.core import Jarvis
from jarvis.services import async_tools, similar_work as sw
from tests.fakes import FakeClient, message, text_block, tool_block
from tests.fsm_data_helpers import FakeFsmApi, catalog, jarvis_with_fsm, resource

SAM = Caller(access.TEAM, "Sam", "eng1", ENGINEER)
PAT = Caller(access.TEAM, "Pat", "office1", OFFICE)
MANAGER = Caller(access.MANAGER, "Morgan")
ASK = "Quote for a 12-zone Gent Vigilon addressable fire alarm in a 3-storey care home"

QUOTES = [
    {"id": 1, "quote_no": "Q1001", "title": "Fire alarm upgrade - Gent Vigilon, 10 zones, three storey care home",
     "customer": "Meadow Care Ltd", "site": "Meadow House", "status": "accepted", "total": 14200.00, "quote_date": "2025-03-04",
     "lines": [{"description": "Gent Vigilon 4 loop panel", "qty": 1}, {"description": "Gent S4-34 sounder", "qty": 30},
               {"description": "Commissioning", "qty": 1}]},
    {"id": 2, "quote_no": "Q1002", "title": "New Gent addressable fire alarm, care home, 12 zones", "customer": "Oak Lodge Care",
     "site": "Oak Lodge", "status": "declined", "total": "16,500.00", "quote_date": "2025-06-10",
     "lines": [{"description": "Gent Vigilon 4 loop panel", "qty": 1}, {"description": "Gent S4-34 sounder", "qty": 36},
               {"description": "Cabling and containment", "qty": 1}]},
    {"id": 3, "quote_no": "Q1003", "title": "Care home fire detection - Gent, 14 zones, 4 storeys", "customer": "Bramley Homes",
     "site": "Bramley Court", "status": "sent", "total": 18100, "quote_date": "2026-01-20"},
    {"id": 4, "quote_no": "Q1004", "title": "CCTV for a warehouse - 8 Hikvision cameras", "customer": "Northern Freight",
     "site": "Unit 4", "status": "accepted", "total": 4200, "quote_date": "2025-09-01"},
    {"id": 5, "quote_no": "Q1005", "title": "Intruder alarm, Texecom, office", "customer": "Meadow Care Ltd", "site": "Head office",
     "status": "accepted", "total": 2100, "quote_date": "2024-05-01"},
]
QUOTE_LINES = [
    {"id": 31, "quote_id": 3, "description": "Gent Vigilon 4 loop panel", "qty": 1},
    {"id": 32, "quote_id": 3, "description": "Gent S4-34 sounder", "qty": 40},
]
JOBS = [
    {"id": 70, "ref": "J7001", "description": "Install Gent Vigilon fire alarm at Meadow House care home, 10 zones",
     "customer": "Meadow Care Ltd", "site": "Meadow House", "status": "completed", "completed_at": "2025-05-02T15:00:00Z",
     "value": 14200, "materials": [{"name": "Gent S4-34 sounder", "quantity": 30}]},
    {"id": 71, "ref": "J7002", "description": "Service visit - emergency lighting", "customer": "Northern Freight", "site": "Unit 4",
     "status": "completed", "completed_at": "2025-07-01"},
]
FIGURES = ("14200.00", "16500.00", "18100.00")


def similar_catalog(*, quotes_sensitive: bool = False, off: tuple[str, ...] = ("audit",), lines: bool = True) -> dict:
    res = [
        resource("quotes", "commercial", ["id", "quote_no", "title", "customer", "site", "status", ("total", "money"),
                                          ("quote_date", "date"), "lines"], sensitive=quotes_sensitive),
        resource("jobs", "operations", ["id", "ref", "description", "customer", "site", "status", ("value", "money"),
                                        ("completed_at", "datetime"), "materials"]),
    ]
    if lines:
        res.append(resource("quote_lines", "commercial", ["id", "quote_id", "description", "qty"]))
    return catalog(off=off, resources=res)


class SimilarFsm(FakeFsmApi):
    """The data API with exact filters, [gte]/[lte] on dates and ``q`` (a substring of any text value) honoured."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/jarvis/catalog":
            return httpx.Response(200, json=self.cat)
        name = path.rsplit("/", 1)[1]
        if name not in self.rows:
            return httpx.Response(404, json={"error": "unknown_resource"})
        p = request.url.params
        rows = list(self.rows[name])
        for key, value in p.multi_items():
            m = re.fullmatch(r"filter\[([a-z_]+)\](?:\[(gte|lte)\])?", key)
            if m and m.group(2) == "gte":
                rows = [r for r in rows if str(r.get(m.group(1)) or "")[:10] >= value]
            elif m and m.group(2) == "lte":
                rows = [r for r in rows if str(r.get(m.group(1)) or "")[:10] <= value]
            elif m:
                rows = [r for r in rows if str(r.get(m.group(1))) == value]
        if p.get("q"):
            q = p["q"].lower()
            rows = [r for r in rows if q in json.dumps(r).lower()]
        limit, offset = min(int(p.get("limit", "100")), self.page_cap), int(p.get("offset", "0"))
        page = rows[offset:offset + limit]
        nxt = offset + limit if offset + limit < len(rows) else None
        return httpx.Response(200, json={"resource": name, "items": page, "total": len(rows), "next_offset": nxt, "truncated": False})

    def data(self, name: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == f"/api/jarvis/data/{name}"]


class FakeMail:
    demo = False

    def __init__(self, messages=None) -> None:
        self.messages = messages if messages is not None else [
            {"id": "m1", "subject": "Enquiry: Gent fire alarm for our care home", "from_name": "Facilities", "received": "2025-02-01T09:00:00Z",
             "preview": "We'd like a price for a Gent addressable system, 12 zones over three storeys. Budget about £15k."},
            {"id": "m2", "subject": "Lunch on Friday?", "from_name": "Pal", "received": "2025-02-02T09:00:00Z", "preview": "Usual place"},
        ]
        self.searches: list[str] = []

    async def search_messages(self, query: str, top: int = 15, mailbox: str | None = None):
        self.searches.append(query)
        return [m for m in self.messages if query.lower() in (m["subject"] + " " + m["preview"]).lower()][:top]


def make(settings, *, cat=None, rows=None, script=None, mail=None):
    api = SimilarFsm(cat or similar_catalog(), rows if rows is not None else {"quotes": QUOTES, "jobs": JOBS,
                                                                            "quote_lines": QUOTE_LINES})
    j, _ = jarvis_with_fsm(settings, api, script)
    j.mail = mail if mail is not None else FakeMail()
    return j, api


async def run(j, caller=None, **args):
    tool = TOOLS_BY_NAME["find_similar_work"]
    return await dispatch(j, tool, tool.model.model_validate({"description": ASK, **args}), caller=caller)


def audit(j):
    return j.db.query("SELECT kind, actor, what, ref FROM audit_events WHERE ref = 'find_similar_work' ORDER BY id")


# ============================================================================================================ features and scoring
def test_a_description_becomes_plain_features_and_a_score_explains_itself():
    want = sw.extract(ASK + ", 40 optical detectors and 12 call points, budget £15k", with_budget=True)
    assert want.systems == {"fire alarm"} and want.makers == {"Gent"} and want.buildings == {"care home"}
    assert (want.zones, want.storeys, want.devices) == (12, 3, 52) and want.panels == {"addressable"}
    assert str(want.budget) == "15000"
    assert not {"gent", "care", "home", "zone", "storey", "quote"} & want.words      # nothing counted twice
    got = sw.extract("Care home fire alarm upgrade - Gent Vigilon panel, 10 zones, three storey")
    points, why = sw.score(want, got)
    assert points >= 10
    assert "same manufacturer: Gent" in why and "same kind of building: care home" in why and "10 zones (asked about 12)" in why
    assert sw.score(want, sw.extract("CCTV for a warehouse"))[0] < sw.MIN_SCORE
    assert sw.extract("S4-34 sounder").devices is None or sw.extract("S4-34 sounder").devices != 34   # a part number isn't a count


def test_quote_status_reads_as_won_lost_or_open():
    assert [sw.outcome(s) for s in ("Accepted", "won", "Declined", "expired", "Sent", "draft", "", "weird")] == \
        ["won", "won", "lost", "lost", "open", "open", None, None]


# ============================================================================================================ the owner
async def test_the_owner_gets_the_closest_past_work_with_why_values_a_guide_and_emails(settings):
    j, api = make(settings)
    try:
        out = await run(j)
        refs = [m["ref"] for m in out["matches"]]
        assert refs[0] == "Q1002" and set(refs) >= {"Q1001", "Q1002", "Q1003", "J7001"}
        assert "Q1004" not in refs and "Q1005" not in refs and "J7002" not in refs          # CCTV / intruder / lighting: not similar
        top = out["matches"][0]
        assert top["kind"] == "quote" and top["customer"] == "Oak Lodge Care" and top["value"] == "16500.00"
        assert top["outcome"] == "lost" and top["status"] == "declined" and top["date"] == "2025-06-10"
        assert "same manufacturer: Gent" in top["why"] and any("12 zones" in w for w in top["why"])
        assert top["record"] == {"resource": "quotes", "id": 2}
        assert "1 x Gent Vigilon 4 loop panel" in top["key_items"]
        q3 = next(m for m in out["matches"] if m["ref"] == "Q1003")
        assert "40 x Gent S4-34 sounder" in q3["key_items"]                      # from the separate quote_lines resource
        job = next(m for m in out["matches"] if m["ref"] == "J7001")
        assert job["kind"] == "job" and "outcome" not in job and job["key_items"] == ["30 x Gent S4-34 sounder"]
        # the pricing guide: worked out from the three similar quotes' own totals, and says so
        g = out["pricing_guide"]
        assert g["based_on"] == 3 and sorted(g["quotes"]) == ["Q1001", "Q1002", "Q1003"]
        assert (g["low"], g["median"], g["high"]) == ("14200.00", "16500.00", "18100.00") and g["value_field"] == "total"
        assert "won" not in g                                                    # only one similar quote was won
        typical = {t["item"]: t for t in g["typical_line_items"]}
        assert typical["Gent Vigilon 4 loop panel"]["in_quotes"] == 3 and typical["Gent S4-34 sounder"]["typical_qty"] == 36
        assert "Commissioning" not in typical                                    # in one quote only
        # past emails: scored on subject + preview, named by id, never their text
        assert [e["id"] for e in out["emails"]] == ["m1"] and "preview" not in out["emails"][0]
        assert out["sources"]["quotes"]["status"] == "ok" and out["sources"]["emails"]["status"] == "ok"
        assert out["truncated"] is False and out["demo"] is False and "never adjust" in out["notice"]
        # 'What Jarvis did': counts, never a figure or a name from a row
        (row,) = audit(j)
        assert row["kind"] == "fsm_read" and "similar" in row["what"]
        assert not any(f in row["what"] for f in FIGURES) and "Oak Lodge" not in row["what"]
    finally:
        await j.http.aclose()


async def test_fewer_than_three_priced_similar_quotes_give_no_range(settings):
    j, _ = make(settings, rows={"quotes": QUOTES[:2] + QUOTES[3:], "jobs": JOBS, "quote_lines": []})
    try:
        g = (await run(j))["pricing_guide"]
        assert g["based_on"] == 2 and "too few" in g["not_enough"] and "low" not in g and "median" not in g
    finally:
        await j.http.aclose()


async def test_two_won_similar_quotes_get_their_own_range(settings):
    rows = [dict(q, status="accepted") if q["id"] in (2, 3) else q for q in QUOTES]
    j, _ = make(settings, rows={"quotes": rows, "jobs": [], "quote_lines": []})
    try:
        g = (await run(j))["pricing_guide"]
        assert g["won"] == {"based_on": 3, "low": "14200.00", "median": "16500.00", "high": "18100.00"}
    finally:
        await j.http.aclose()


# ============================================================================================================ filters and bounds
async def test_filters_are_hard_filters_and_bad_dates_are_refused(settings):
    j, _ = make(settings)
    try:
        out = await run(j, customer="Meadow Care")
        assert {m["ref"] for m in out["matches"]} == {"Q1001", "J7001"}
        out = await run(j, date_from="2025-06-01", date_to="2026-12-31")
        assert {m["ref"] for m in out["matches"]} == {"Q1002", "Q1003"}
        out = await run(j, manufacturer="Advanced")
        assert out["matches"] == [] and "filtered_out" in out
        out = await run(j, system_type="CCTV", description="cameras for a warehouse")
        assert [m["ref"] for m in out["matches"]] == ["Q1004"]
        assert (await run(j, date_from="last March"))["kind"] == "bad_request"
        assert (await run(j, date_from="2026-02-01", date_to="2025-01-01"))["kind"] == "bad_request"
        empty = await run(j, description="hello there")
        assert empty["kind"] == "bad_request" and "Describe the job" in empty["error"]
    finally:
        await j.http.aclose()


async def test_reads_are_bounded_and_a_long_history_is_reported_as_partly_searched(settings):
    many = [{"id": 1000 + i, "quote_no": f"Q{9000 + i}", "title": "Intruder alarm service", "status": "sent", "total": 100,
             "quote_date": "2020-01-01"} for i in range(900)] + QUOTES
    j, api = make(settings, rows={"quotes": many, "jobs": JOBS, "quote_lines": QUOTE_LINES})
    try:
        out = await run(j, include_emails=False)
        assert len(api.data("quotes")) <= 1 + sw.SEARCH_TERMS and len(api.data("jobs")) <= 1 + sw.SEARCH_TERMS
        assert len(api.data("quote_lines")) <= sw.MAX_LINE_READS
        for r in api.data("quotes"):
            assert int(r.url.params["limit"]) <= sw.RECENT_ROWS
        assert out["sources"]["quotes"]["status"] == "partial" and out["truncated"] is True
        assert "most recent" in out["sources"]["quotes"]["note"]
        assert {"Q1001", "Q1002", "Q1003"} <= {m["ref"] for m in out["matches"]}   # found by the keyword search
        assert "emails" not in out
        facts = cov.call_facts("find_similar_work", {}, out)
        assert {"src": "Salts FSM", "status": cov.PARTIAL, "detail": "quotes",
                "note": out["sources"]["quotes"]["note"][:120]} in facts
    finally:
        await j.http.aclose()


# ============================================================================================================ who sees what
async def test_an_engineer_and_the_office_get_past_work_without_any_money_or_email(settings):
    mail = FakeMail()
    j, _ = make(settings, mail=mail)
    try:
        for caller in (SAM, PAT):
            assert access.tool_allowed("find_similar_work", caller)
            out = await run(j, caller)
            assert out["matches"] and "pricing_guide" not in out and "emails" not in out and "team version" in out["prices"]
            assert all("value" not in m for m in out["matches"])
            text = json.dumps(out)
            assert not any(f in text for f in FIGURES) and "16,500" not in text and "14200" not in text
            assert "emails" not in out["sources"]
        assert mail.searches == []                                               # the mailbox was never searched
        assert any("(engineer)" in r["actor"] for r in audit(j)) and any("(office)" in r["actor"] for r in audit(j))
    finally:
        await j.http.aclose()


async def test_a_manager_is_refused_an_owner_only_quotes_resource_and_the_owner_reads_it_as_sensitive(settings):
    j, api = make(settings, cat=similar_catalog(quotes_sensitive=True))
    try:
        out = await run(j, MANAGER)
        assert out["sources"]["quotes"]["status"] == "owner_only" and api.data("quotes") == []
        assert {m["kind"] for m in out["matches"]} == {"job"} and "handling" not in out
        assert {"src": "Salts FSM", "status": cov.OWNER_ONLY, "detail": "quotes"} in cov.call_facts("find_similar_work", {}, out)
        engineer = await run(j, SAM)
        assert engineer["sources"]["quotes"]["status"] == "owner_only" and api.data("quotes") == []
        owner = await run(j)
        assert owner["sources"]["quotes"]["status"] == "ok" and "handling" in owner
        assert j.fsm_read.contains_sensitive("about 16500.00 last time")       # remember() refuses the figures
    finally:
        await j.http.aclose()


async def test_a_switched_off_group_or_a_missing_resource_is_said_plainly(settings):
    cat = catalog(off=("audit", "commercial"), resources=[
        resource("quotes", "commercial", ["id", "title", ("total", "money")]),
        resource("jobs", "operations", ["id", "ref", "description"])])
    j, api = make(settings, cat=cat, rows={"quotes": QUOTES, "jobs": JOBS})
    try:
        out = await run(j, include_emails=False)
        assert out["sources"]["quotes"] == {"status": "scope_off", "note": "commercial"} and api.data("quotes") == []
        facts = cov.call_facts("find_similar_work", {}, out)
        assert {"src": "Salts FSM", "status": cov.SCOPE_OFF, "detail": "quotes", "note": "commercial"} in facts
    finally:
        await j.http.aclose()
    cat = catalog(resources=[resource("jobs", "operations", ["id", "ref", "description"])])
    j, _ = make(settings, cat=cat, rows={"jobs": JOBS})
    try:
        out = await run(j, include_emails=False)
        assert out["sources"]["quotes"]["status"] == "not_exposed" and out["pricing_guide"]["based_on"] == 0
    finally:
        await j.http.aclose()


# ============================================================================================================ sample data
async def test_sample_fsm_and_mail_data_are_never_a_source(settings):
    j = Jarvis(settings, client=FakeClient())      # no FSM address, no Microsoft 365: both are sample data
    try:
        out = await run(j)
        assert out["demo"] is True and out["matches"] == [] and "isn't connected" in out["note"]
        assert out["sources"]["quotes"]["status"] == "demo" and out["sources"]["emails"]["status"] == "not_connected"
        facts = cov.call_facts("find_similar_work", {}, out)
        assert {"src": "Salts FSM", "status": cov.NOT_CONNECTED, "detail": "quotes"} in facts
        assert {"src": "Outlook", "status": cov.NOT_CONNECTED, "detail": "past emails"} in facts
        c = cov.summarise(facts, ASK, demo=cov.demo_map(j))
        assert c["confidence"] == cov.LOW and any("your mailbox past emails" in g["text"] for g in c["gaps"])
    finally:
        await j.http.aclose()


# ============================================================================================================ registration
def test_it_is_a_read_for_check_mode_never_backgrounded_and_its_output_is_untrusted():
    tool = TOOLS_BY_NAME["find_similar_work"]
    assert tool.approval is False and checkmode.tool_allowed(tool)
    assert "find_similar_work" in async_tools.NOT_BACKGROUND and async_tools.is_untrusted_output("find_similar_work")
    assert "find_similar_work" in access.TEAM_TOOLS and "find_similar_work" not in access.OFFICE_EXTRA_TOOLS
    assert cov.tool_sources("find_similar_work") == ("Salts FSM",)


async def test_check_mode_runs_it_and_it_changes_nothing(settings):
    j, _ = make(settings)
    try:
        tool = TOOLS_BY_NAME["find_similar_work"]
        out = await dispatch(j, tool, tool.model.model_validate({"description": ASK}), check=True)
        assert out["matches"] and j.db.pending_actions() == []
    finally:
        await j.http.aclose()


async def test_reachable_through_the_brain_and_the_coverage_line_names_each_part(settings):
    script = [message([tool_block("find_similar_work", {"description": ASK})], "tool_use"),
              message([text_block("Yes - Q1002 for Oak Lodge Care was the closest.")])]
    j, _ = make(settings, script=script)
    try:
        q = j.bus.subscribe()
        reply = await j.brain.ask("Have we done something like this before? " + ASK, "typed")
        assert "Q1002" in reply
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        r = next(e["data"] for e in reversed(events) if e["type"] == "reply")
        checked = r["coverage"]["checked"]
        assert "Salts FSM quotes" in checked and "Salts FSM jobs" in checked and "your mailbox past emails" in checked
        assert r["coverage"]["confidence"] == cov.HIGH
        sent = json.dumps(j.brain.messages, default=str)
        assert "Q1002" in sent and "pricing_guide" in sent
    finally:
        await j.http.aclose()
