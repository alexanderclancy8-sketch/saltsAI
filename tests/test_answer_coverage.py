"""The coverage line under a reply (brain/coverage.py): what was really checked, what wasn't and why, and a High / Medium / Low
confidence decided by fixed rules from the turn's real tool calls - never by the model.

Covers every gap type (sample data withheld, the FSM on demo, scope off, the FSM's 404 "doesn't expose this yet", owner-only
withholding, a truncated scan, an error / a timeout, a source the question needed but nothing read), the confidence rules, the
spoken one-sentence gap note, the "[Coverage: ...]" line the model gets before it answers, and what is stored with the transcript
row (labels and counts, never a value).
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from jarvis.brain import coverage as cov
from jarvis.brain.coverage import HIGH, LOW, MEDIUM, call_facts, confidence, error_facts, line, spoken, summarise, turn_note
from jarvis.brain.tools import TOOLS_BY_NAME
from jarvis.core import Jarvis
from tests.fakes import FakeClient, message, text_block, tool_block
from tests.fsm_data_helpers import FakeFsmApi, jarvis_with_fsm, rows

REAL = {"Salts FSM": False, "Sage": False, "RAM Tracking": False, "Outlook": False, "Stock records": False}


def real(**demo):
    return {**REAL, **demo}


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def reply_of(events):
    return next(e["data"] for e in reversed(events) if e["type"] == "reply")


# --------------------------------------------------------------------------- one tool call -> facts, per gap type
def test_a_withheld_sample_data_result_names_the_source_not_the_tool():
    refusal = {"demo_data_withheld": True, "tool": "finance_aged",
               "not_connected": [{"source": "the accounts (Sage)", "to_connect": "Connect Sage"}]}
    assert call_facts("finance_aged", {}, refusal) == [{"src": "Sage", "status": cov.WITHHELD}]


def test_fsm_data_api_error_kinds_map_to_their_gap():
    assert call_facts("fsm_data", {"resource": "jobs"}, {"error": "x", "kind": "demo", "demo": True}) == [
        {"src": "Salts FSM", "status": cov.NOT_CONNECTED, "detail": "jobs"}]
    [f] = call_facts("fsm_analyse", {"resource": "invoices"}, {"error": "x", "kind": "scope_off", "group": "finance"})
    assert f == {"src": "Salts FSM", "status": cov.SCOPE_OFF, "detail": "invoices", "note": "finance"}
    [f] = call_facts("fsm_data", {"resource": "vans"}, {"error": "The FSM doesn't expose this yet (HTTP 404)", "kind": "unavailable"})
    assert f["status"] == cov.NOT_EXPOSED
    [f] = call_facts("fsm_data", {"resource": "payslips"}, {"error": "owner only", "kind": "owner_only", "resource": "payslips"})
    assert f["status"] == cov.OWNER_ONLY and f["detail"] == "payslips"
    [f] = call_facts("fsm_data", {"resource": "jobs"}, {"error": "busy", "kind": "rate_limited"})
    assert f["status"] == cov.RATE_LIMITED
    [f] = call_facts("fsm_data", {"resource": "jobz"}, {"error": "no such resource", "kind": "not_found", "did_you_mean": ["jobs"]})
    assert f["status"] == cov.BAD_INPUT          # the model's own typo is not a gap in the data
    [f] = call_facts("fsm_data", {"resource": "vans"}, {"error": "no resource", "kind": "not_found", "resource": "vans"})
    assert f["status"] == cov.NOT_EXPOSED        # listed in the catalog, but the FSM answers 404 for its rows
    [f] = call_facts("fsm_data", {"resource": "jobs"}, {"error": "server error", "kind": "server"})
    assert f["status"] == cov.ERROR


def test_a_truncated_scan_says_how_much_was_read_in_counts_only():
    [f] = call_facts("fsm_analyse", {"resource": "invoices"},
                     {"truncated": True, "rows_scanned": 50000, "rows_matching_in_fsm": 64200, "results": [{"total": 99999.5}]})
    assert f == {"src": "Salts FSM", "status": cov.PARTIAL, "detail": "invoices", "note": "scanned 50,000 of 64,200 rows"}
    [f] = call_facts("fsm_data", {"resource": "jobs"}, {"truncated": True, "returned": 120, "total": 900, "items": []})
    assert f["note"] == "showing 120 of 900 rows"


def test_errors_and_timeouts_from_a_raised_tool_call():
    assert error_facts("fsm_jobs", RuntimeError("boom")) == [{"src": "Salts FSM", "status": cov.ERROR, "detail": "jobs"}]
    assert error_facts("finance_aged", asyncio.TimeoutError())[0]["status"] == cov.TIMEOUT
    assert error_facts("remember", RuntimeError("x")) == []   # a tool that reads no business source names nothing


def test_composite_sections_on_sample_data_and_other_refusals():
    briefing = {"jobs": [1, 2], "accounts": {"error": "Not connected: ...", "not_connected": ["the accounts (Sage)"]}}
    facts = call_facts("business_health", {}, briefing)
    assert {"src": "Sage", "status": cov.WITHHELD} in facts and any(f["src"] == "Salts FSM" and f["status"] == cov.OK for f in facts)
    team = "finance_snapshot isn't available to you here. This is the team version of Jarvis, which covers jobs ..."
    assert call_facts("finance_snapshot", {}, team)[0]["status"] == cov.REFUSED
    blocked = {"blocked_in_check_mode": True, "tool": "email_inbox", "error": "..."}
    assert call_facts("email_inbox", {}, blocked)[0]["status"] == cov.BLOCKED
    assert call_facts("fsm_jobs", {}, {"demo": True, "jobs": []})[0]["status"] == cov.DEMO


# --------------------------------------------------------------------------- the confidence rules
def test_confidence_rules_in_order():
    assert confidence(relied_on_demo=True, failed=0, missing=0, truncated=False, business=True, read_any=True)[0] == LOW
    assert confidence(relied_on_demo=False, failed=1, missing=0, truncated=False, business=True, read_any=True)[0] == LOW
    assert confidence(relied_on_demo=False, failed=0, missing=0, truncated=False, business=True, read_any=False)[0] == LOW
    assert confidence(relied_on_demo=False, failed=0, missing=2, truncated=False, business=True, read_any=True)[0] == LOW
    assert confidence(relied_on_demo=False, failed=0, missing=0, truncated=True, business=True, read_any=True)[0] == MEDIUM
    assert confidence(relied_on_demo=False, failed=0, missing=1, truncated=False, business=True, read_any=True)[0] == MEDIUM
    assert confidence(relied_on_demo=False, failed=0, missing=0, truncated=False, business=True, read_any=True)[0] == HIGH


def test_everything_real_and_complete_is_high():
    c = summarise([{"src": "Salts FSM", "status": "ok", "detail": "jobs"}], "How many jobs have we got today?", demo=real())
    assert c["confidence"] == HIGH and c["checked"] == ["Salts FSM jobs"] and c["gaps"] == []


def test_the_owners_example_line_one_source_missing_is_medium():
    c = summarise([{"src": "Salts FSM", "status": "ok", "detail": "invoices"}], "How much is overdue on invoices?",
                  demo=real(Sage=True))
    assert line(c) == "Checked: Salts FSM invoices · Not checked: Sage (not connected) · Medium"


def test_a_connected_source_the_question_needed_but_nobody_read_is_not_checked():
    c = summarise([{"src": "Salts FSM", "status": "ok", "detail": "invoices"}], "How much is overdue on invoices?", demo=real())
    assert c["confidence"] == MEDIUM and c["gaps"] == [{"source": "Sage", "kind": "not_checked", "text": "Sage (not checked)"}]


def test_truncation_is_medium_and_named():
    c = summarise([{"src": "Salts FSM", "status": "partial", "detail": "jobs", "note": "scanned 50,000 of 64,200 rows"}],
                  "How many jobs did we do this year?", demo=real())
    assert c["confidence"] == MEDIUM and "Salts FSM jobs (scanned 50,000 of 64,200 rows)" in line(c)


def test_demo_data_relied_on_is_low():
    c = summarise([{"src": "Salts FSM", "status": "demo", "detail": "jobs"}], "What's on today?", demo=real(**{"Salts FSM": True}))
    assert c["confidence"] == LOW and c["gaps"][0]["kind"] == cov.DEMO and "sample data" in c["gaps"][0]["text"]


def test_an_error_is_low_unless_the_same_source_was_read_after_it():
    failed = summarise([{"src": "Salts FSM", "status": "error"}], "How many jobs today?", demo=real())
    assert failed["confidence"] == LOW and failed["gaps"][0]["text"] == "Salts FSM (error)"
    recovered = summarise([{"src": "Salts FSM", "status": "error"}, {"src": "Salts FSM", "status": "ok", "detail": "jobs"}],
                          "How many jobs today?", demo=real())
    assert recovered["confidence"] == HIGH and recovered["gaps"] == []
    assert summarise([{"src": "Sage", "status": "timeout"}], "cash?", demo=real())["gaps"][0]["text"] == "Sage (timed out)"


def test_scope_off_not_exposed_and_owner_only_are_missing_sources():
    c = summarise([{"src": "Salts FSM", "status": "scope_off", "detail": "invoices", "note": "finance"}],
                  "What's our margin this year?", demo=real())
    assert c["confidence"] == LOW  # the FSM finance group is off AND Sage wasn't read: two missing
    assert "Salts FSM invoices (switched off in the FSM: finance)" in line(c)
    c = summarise([{"src": "Salts FSM", "status": "not_exposed", "detail": "vehicles"}], "List the vans", demo=real())
    assert "Salts FSM vehicles (the FSM doesn't expose this yet)" in line(c)
    c = summarise([{"src": "Salts FSM", "status": "owner_only", "detail": "payslips"}], "Show me the payslips", demo=real())
    assert "(owner only)" in line(c)


def test_a_business_question_with_no_tools_is_low_and_small_talk_has_no_line():
    c = summarise([], "How many jobs are overdue?", demo=real())
    assert c["confidence"] == LOW and c["checked"] == [] and "No system was checked" in c["why"]
    assert summarise([], "Morning Jarvis, how are you?", demo=real()) is None


def test_a_team_turn_never_counts_sage_or_mail_as_missing():
    c = summarise([{"src": "Salts FSM", "status": "ok", "detail": "jobs"}], "How much have we invoiced on jobs today?",
                  demo=real(Sage=True), team=True)
    assert c["confidence"] == HIGH and not any(g["source"] == "Sage" for g in c["gaps"])


def test_the_mailbox_is_called_your_mailbox():
    c = summarise([{"src": "Outlook", "status": "ok"}], "Any emails from Kestrel?", demo=real())
    assert c["checked"] == ["your mailbox"]


# --------------------------------------------------------------------------- spoken and prompt notes
def test_spoken_low_confidence_gets_one_short_sentence_only_when_not_already_said():
    c = summarise([{"src": "Sage", "status": "withheld"}], "What's our cash position?", demo=real(Sage=True))
    assert c["confidence"] == LOW
    assert spoken(c, "Here's the position.") == "I couldn't check Sage, it isn't connected."
    assert spoken(c, "I can't give you that - Sage isn't connected yet.") == ""   # the reply already said it
    medium = summarise([{"src": "Salts FSM", "status": "ok"}], "How much is overdue on invoices?", demo=real(Sage=True))
    assert medium["confidence"] == MEDIUM and spoken(medium, "x") == ""
    assert spoken(summarise([], "How many jobs today?", demo=real()), "Four.") == "I didn't check Salts FSM for that."


def test_turn_note_names_needed_sources_that_are_not_connected():
    note = turn_note("How much is overdue on invoices?", real(Sage=True))
    assert note.startswith("[Coverage: this question needs Sage, which is not connected.") and "name the gap" in note
    assert turn_note("How much is overdue on invoices?", real()) == ""
    assert turn_note("Tell me a joke", real(Sage=True)) == ""
    assert turn_note("How much is overdue on invoices?", real(Sage=True), team=True) == ""


# --------------------------------------------------------------------------- through the real brain loop
async def test_a_money_question_on_sample_accounts_is_low_names_sage_and_stores_no_values(settings):
    script = [message([tool_block("fsm_jobs", {}, "t1"), tool_block("finance_aged", {}, "t2")], "tool_use"),
              message([text_block("I can't give you overdue invoices yet - Sage isn't connected.")])]
    j = Jarvis(settings, client=FakeClient(script))
    q = j.bus.subscribe()
    await j.brain.ask("How much is overdue on our invoices?", "typed")
    r = reply_of(drain(q))
    c = r["coverage"]
    assert c["confidence"] == LOW and "spoken" not in c                     # typed: no spoken sentence
    assert any(g["source"] == "Sage" for g in c["gaps"]) and any(g["kind"] == cov.DEMO for g in c["gaps"])
    assert r["sources"] == ["Salts FSM (demo data)", "Sage (demo data)"]    # the existing source line is unchanged
    # the model was told before answering that Sage isn't connected
    sent = j.brain.messages[0]["content"][-1]["text"]
    assert "[Coverage: this question needs" in sent and "Sage" in sent and sent.endswith("How much is overdue on our invoices?")
    # stored with the transcript row and the metrics row: labels and kinds only - no values, names or figures from the data
    [row] = j.db.query("SELECT * FROM transcript WHERE role = 'assistant'")
    stored = json.loads(row["coverage"])
    assert stored["confidence"] == LOW and "spoken" not in stored
    text = row["coverage"]
    for leak in ("£", "Bradford Council", "Northcliffe", "J24100", "Dan Harper"):
        assert leak not in text
    [m] = j.db.query("SELECT coverage FROM turn_metrics")
    assert json.loads(m["coverage"])["confidence"] == LOW
    # the user's own line carries no coverage
    [u] = j.db.query("SELECT coverage FROM transcript WHERE role = 'user'")
    assert u["coverage"] == ""
    await j.http.aclose()


async def test_a_spoken_low_confidence_reply_carries_its_gap_sentence(settings):
    script = [message([tool_block("finance_snapshot", {})], "tool_use"), message([text_block("Here's where we are.")])]
    j = Jarvis(settings, client=FakeClient(script))
    q = j.bus.subscribe()
    await j.brain.ask("What's our cash position?", "voice")
    c = reply_of(drain(q))["coverage"]
    assert c["confidence"] == LOW and c["spoken"] == "I couldn't check Sage, it isn't connected."
    await j.http.aclose()


async def test_small_talk_has_no_coverage_line(settings):
    j = Jarvis(settings, client=FakeClient([message([text_block("Morning.")])]))
    q = j.bus.subscribe()
    await j.brain.ask("Morning", "typed")
    assert "coverage" not in reply_of(drain(q))
    [row] = j.db.query("SELECT coverage FROM transcript WHERE role = 'assistant'")
    assert row["coverage"] == ""
    await j.http.aclose()


async def test_a_tool_that_raises_is_an_error_gap(settings, monkeypatch):
    async def boom(j, a):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(TOOLS_BY_NAME["staff_today"], "handler", boom)
    j = Jarvis(settings, client=FakeClient([message([tool_block("staff_today", {})], "tool_use"),
                                            message([text_block("I couldn't reach the FSM.")])]))
    q = j.bus.subscribe()
    await j.brain.ask("Which engineers are out today?", "typed")
    c = reply_of(drain(q))["coverage"]
    assert c["confidence"] == LOW and c["gaps"][0]["kind"] == cov.ERROR and c["gaps"][0]["text"] == "Salts FSM engineers (error)"
    await j.http.aclose()


async def test_a_truncated_fsm_analyse_scan_is_medium_through_the_loop(settings):
    api = FakeFsmApi(rows={"jobs": rows(1200, status="done")})
    script = [message([tool_block("fsm_analyse", {"resource": "jobs", "metrics": ["count"]})], "tool_use"),
              message([text_block("At least 1,000 jobs - the scan stopped early, so that's only part of them.")])]
    j, _ = jarvis_with_fsm(settings, api, script)
    j.fsm_analyse.max_rows = 1000
    q = j.bus.subscribe()
    await j.brain.ask("How many jobs have we done?", "typed")
    c = reply_of(drain(q))["coverage"]
    assert c["confidence"] == MEDIUM and c["checked"] == ["Salts FSM jobs"]
    assert c["gaps"] == [{"source": "Salts FSM jobs", "kind": "truncated", "text": "Salts FSM jobs (scanned 1,000 of 1,200 rows)"}]
    await j.http.aclose()


async def test_scope_off_404_and_owner_only_through_the_mocked_fsm(settings):
    from tests.fsm_data_helpers import catalog, resource

    cat = catalog(off=("audit", "finance"), resources=[
        resource("jobs", "operations", ["id", "status"]), resource("invoices", "finance", ["id", "total"]),
        resource("vehicles", "assets", ["id", "reg"]), resource("payslips", "people", ["id", "gross"], sensitive=True)])
    api = FakeFsmApi(cat, rows={"jobs": rows(3), "payslips": rows(2)})   # vehicles has no rows route: the FSM answers 404
    script = [message([tool_block("fsm_data", {"resource": "invoices"}, "t1"), tool_block("fsm_data", {"resource": "vehicles"}, "t2"),
                       tool_block("fsm_data", {"resource": "jobs"}, "t3")], "tool_use"),
              message([text_block("Partial picture only.")])]
    j, _ = jarvis_with_fsm(settings, api, script)
    q = j.bus.subscribe()
    await j.brain.ask("How are invoices and vans looking against jobs?", "typed")
    c = reply_of(drain(q))["coverage"]
    texts = [g["text"] for g in c["gaps"]]
    assert "Salts FSM invoices (switched off in the FSM: finance)" in texts
    assert "Salts FSM vehicles (the FSM doesn't expose this yet)" in texts
    assert "Salts FSM jobs" in c["checked"] and c["confidence"] == LOW
    await j.http.aclose()


async def test_a_managers_owner_only_read_is_named_as_owner_only(settings):
    from jarvis import access
    from tests.fsm_data_helpers import catalog

    api = FakeFsmApi(catalog(), rows={"payslips": rows(2)})
    script = [message([tool_block("fsm_data", {"resource": "payslips"})], "tool_use"),
              message([text_block("Only the owner can have that read out.")])]
    j, _ = jarvis_with_fsm(settings, api, script)
    q = j.bus.subscribe()
    token = access.current_caller.set(access.Caller(access.MANAGER, "Sam"))
    try:
        await j.brain.ask("Show me the payslips", "typed")
    finally:
        access.current_caller.reset(token)
    c = reply_of(drain(q))["coverage"]
    assert c["gaps"][0]["text"] == "Salts FSM payslips (owner only)"
    await j.http.aclose()


@pytest.mark.parametrize("bad", [None, "x", 3])
def test_line_and_spoken_are_safe_on_junk(bad):
    assert line(bad if isinstance(bad, dict) else None) == "" and spoken(None) == ""


# --------------------------------------------------------------------------- FSM documents and Jarvis's own customer / site notes
def test_an_fsm_document_read_names_the_document_and_its_caveats():
    ok = call_facts("fsm_document_read", {"document_id": "doc-1"},
                    {"document_id": "doc-1", "name": "RAMS ladder work.pdf", "transcribed": False, "truncated": False, "masked_by_fsm": 0})
    assert ok == [{"src": "Salts FSM", "status": cov.OK, "detail": "document 'RAMS ladder work.pdf'"}]
    [scan] = call_facts("fsm_document_read", {}, {"name": "Cert.jpg", "transcribed": True, "truncated": False, "masked_by_fsm": 2})
    assert scan["status"] == cov.TRANSCRIBED and scan["note"] == "2 items masked by the FSM"
    [part] = call_facts("fsm_document_read", {}, {"name": "Big.pdf", "transcribed": False, "truncated": True, "masked_by_fsm": 0})
    assert part["status"] == cov.PARTIAL
    assert call_facts("fsm_document_read", {}, {"ambiguous": True, "candidates": []})[0]["status"] == cov.BAD_INPUT
    assert call_facts("fsm_document_read", {}, {"error": "x", "kind": "not_found"})[0]["status"] == cov.BAD_INPUT
    assert call_facts("fsm_document_read", {}, {"error": "x", "kind": "unavailable"})[0]["status"] == cov.NOT_EXPOSED
    assert call_facts("fsm_document_read", {}, {"error": "x", "kind": "owner_only"})[0]["status"] == cov.OWNER_ONLY


def test_a_transcribed_scan_is_medium_unless_the_answer_says_so():
    facts = [{"src": "Salts FSM", "status": cov.TRANSCRIBED, "detail": "document 'Cert.jpg'"}]
    silent = summarise(facts, "When does the Keighley certificate expire?", demo=real(), reply="It expires on 3 March 2027.")
    assert silent["confidence"] == MEDIUM and "Salts FSM document 'Cert.jpg'" in silent["checked"]
    assert "transcribed scan" in silent["caveats"][0] and "doesn't say so" in silent["caveats"][0]
    said = summarise(facts, "When does the Keighley certificate expire?", demo=real(),
                     reply="It's a scan I transcribed, so check it, but it says 3 March 2027.")
    assert said["confidence"] == HIGH and "doesn't say so" not in said["caveats"][0]


def test_masked_items_are_listed_as_a_caveat_and_on_the_line():
    facts = [{"src": "Salts FSM", "status": "ok", "detail": "document 'Site pack.pdf'", "note": "3 items masked by the FSM"}]
    c = summarise(facts, "What does the Keighley site pack say about access?", demo=real())
    assert c["confidence"] == HIGH and c["caveats"] == ["Salts FSM document 'Site pack.pdf' (3 items masked by the FSM)"]
    assert "Caveats: Salts FSM document 'Site pack.pdf' (3 items masked by the FSM)" in line(c)


def test_jarvis_notes_are_notes_not_a_checked_system_and_never_raise_confidence():
    c = summarise([], "Anything I should know about Acme's jobs?", demo=real(), notes=["Jarvis's notes on Acme"])
    assert c["notes"] == ["Jarvis's notes on Acme"] and c["checked"] == []
    assert c["confidence"] == LOW                      # a business question answered from notes alone: nothing was checked
    with_fsm = summarise([{"src": "Salts FSM", "status": "ok", "detail": "jobs"}], "Anything about Acme's jobs?", demo=real(),
                         notes=["Jarvis's notes on Acme"])
    without = summarise([{"src": "Salts FSM", "status": "ok", "detail": "jobs"}], "Anything about Acme's jobs?", demo=real())
    assert with_fsm["confidence"] == without["confidence"] == HIGH and with_fsm["checked"] == without["checked"]
    assert line(with_fsm).endswith("· Notes: Jarvis's notes on Acme · High")
    assert summarise([], "Morning", demo=real(), notes=["Jarvis's notes on Acme"])["notes"] == ["Jarvis's notes on Acme"]


async def test_the_trace_counts_injected_notes_as_notes(settings):
    from jarvis.brain.trace import TurnTrace

    j = Jarvis(settings, client=FakeClient())
    t = TurnTrace(j)
    t.on_event("user_message", {"text": "What do we know about Acme's jobs?"})
    t.on_event("thinking", {"mode": "typed"})
    t.on_event("tool", {"name": "fsm_jobs", "state": "start"})
    t.on_event("tool", {"name": "fsm_jobs", "state": "done", "coverage": [{"src": "Salts FSM", "status": "ok", "detail": "jobs"}]})
    t.add_source("Jarvis's notes on Acme")
    out = t.finish("From the FSM jobs, and my notes on Acme.")
    assert "Jarvis's notes on Acme" in out["sources"]                      # the existing source line still names them
    assert out["coverage"]["notes"] == ["Jarvis's notes on Acme"] and "Jarvis's notes on Acme" not in out["coverage"]["checked"]
    stored = json.loads(cov.as_stored(out["coverage"]))
    assert stored["notes"] == ["Jarvis's notes on Acme"]
    await j.http.aclose()


def test_customer_balance_is_one_customers_invoices():
    [f] = call_facts("customer_balance", {"customer": "Acme"}, {"customer": "Acme Ltd", "owed": 1200.0, "overdue": 300.0})
    assert f == {"src": "Salts FSM", "status": cov.OK, "detail": "invoices (one customer)"}
    c = summarise([f], "How much does Acme owe us?", demo=real(Sage=True))
    assert c["checked"] == ["Salts FSM invoices (one customer)"] and "1200" not in json.dumps(c)
    kinds = {k: call_facts("customer_balance", {}, {"error": "x", "kind": k})[0]["status"]
             for k in ("not_found", "ambiguous", "bad_request", "office_only", "demo", "scope_off", "unavailable", "rate_limited")}
    assert kinds == {"not_found": cov.BAD_INPUT, "ambiguous": cov.BAD_INPUT, "bad_request": cov.BAD_INPUT, "office_only": cov.REFUSED,
                     "demo": cov.NOT_CONNECTED, "scope_off": cov.SCOPE_OFF, "unavailable": cov.NOT_EXPOSED,
                     "rate_limited": cov.RATE_LIMITED}
