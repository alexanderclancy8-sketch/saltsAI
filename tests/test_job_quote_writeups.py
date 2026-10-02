"""AI-assisted write-ups: a customer-facing summary of a completed job and a plain-English scope for a quote.
Drafts only - shown on the display, never written to Salts FSM or sent."""

import json

from jarvis.services.documents import build_job_summary_context, build_quote_scope_context

JOB = {
    "id": "J1", "ref": "J1", "type": "service", "status": "completed", "customer": "Acme Ltd", "site": "Acme HQ",
    "engineer": "Dan Harper", "scheduled_start": "2026-09-29T08:00:00", "started_at": "2026-09-29T08:10:00",
    "completed_at": "2026-09-29T11:00:00", "value": 450, "created_by": "Hannah Cole", "invoice_ref": "INV-9",
    "checkin_lat": 53.1, "checkin_lng": -1.7,
    "extra": {
        "notes": [{"at": "2026-09-29T10:50:00", "by": "Dan Harper", "text": "Replaced 2 failed smoke detectors."}],
        "materials_used": [{"sku": "DET-1", "qty": 2, "unit_cost": 11.5}],
        "status_history": [{"status": "scheduled", "at": "2026-09-29T08:00:00"},
                           {"status": "completed", "at": "2026-09-29T11:00:00"}],
        # None of these may ever reach the model:
        "internal_comments": "Customer is a nightmare, never discount",
        "access_code": "4821",
        "labour_cost": 120,
        "margin_pct": 31,
    },
}
PRIVATE_EXTRA_KEYS = ("internal_comments", "access_code", "labour_cost", "margin_pct")


# ------------------------------------------------------------------------------------------ job context
def test_job_context_keeps_only_customer_safe_fields_and_the_evidence():
    ctx, err = build_job_summary_context(JOB, "J1")
    assert err is None
    assert ctx["job"]["ref"] == "J1" and ctx["job"]["site"] == "Acme HQ" and ctx["job"]["completed_at"]
    for private in ("value", "created_by", "invoice_ref", "checkin_lat", "checkin_lng"):
        assert private not in ctx["job"], private
    assert ctx["details"]["notes"][0]["text"] == "Replaced 2 failed smoke detectors."
    assert ctx["details"]["materials_used"] == [{"sku": "DET-1", "qty": 2}]  # nested unit_cost scrubbed too
    assert ctx["missing"] == []


def test_job_context_only_passes_allowlisted_extra_keys():
    ctx, err = build_job_summary_context(JOB, "J1")
    assert err is None
    assert set(ctx["details"]) == {"notes", "materials_used", "status_history"}
    blob = json.dumps(ctx)
    for private in (*PRIVATE_EXTRA_KEYS, "4821", "nightmare", "unit_cost"):
        assert private not in blob, private


def test_job_context_ignores_private_keys_when_deciding_there_is_enough_to_summarise():
    only_private = JOB | {"extra": {"internal_notes": "do not tell the customer", "access_code": "4821",
                                    "status_history": [{"status": "completed"}]}}
    ctx, err = build_job_summary_context(only_private, "J1")  # "internal_notes" is not an allowlisted key
    assert ctx is None and "nothing to base a summary on" in err


def test_allowlist_matches_keys_case_and_underscore_insensitively_but_exactly():
    camel = JOB | {"extra": {"materialsUsed": [{"sku": "X", "qty": 1}], "Notes": [{"text": "ok"}],
                             "staffNotes": "gossip", "notes_internal": "secret plan"}}
    ctx, err = build_job_summary_context(camel, "J1")
    assert err is None and set(ctx["details"]) == {"materialsUsed", "Notes"}


def test_job_context_refuses_jobs_that_are_not_complete():
    for status in ("in_progress", "scheduled", None):
        ctx, err = build_job_summary_context(JOB | {"status": status}, "J1")
        assert ctx is None and "not completed" in err
    assert build_job_summary_context(JOB | {"status": "Signed_Off"}, "J1")[1] is None  # case-insensitive, any "done" state


def test_job_context_unknown_job_and_nothing_to_summarise():
    ctx, err = build_job_summary_context(None, "J99")
    assert ctx is None and "couldn't find" in err
    bare = JOB | {"extra": {"status_history": [{"status": "completed"}]}}
    ctx, err = build_job_summary_context(bare, "J1")
    assert ctx is None and "nothing to base a summary on" in err
    ctx, err = build_job_summary_context(JOB | {"extra": None}, "J1")
    assert ctx is None and err


def test_job_context_flags_missing_pieces_instead_of_inventing():
    notes_only = JOB | {"completed_at": None,
                        "extra": {"notes": [{"text": "Tested all call points."}]}}
    ctx, err = build_job_summary_context(notes_only, "J1")
    assert err is None
    missing = " ".join(ctx["missing"])
    assert "materials" in missing and "status history" in missing and "completed" in missing
    assert "engineer notes" not in missing


# ----------------------------------------------------------------------------------------- quote context
QUOTES = [
    {"id": "Q1", "title": "Replace 3 failed smoke detectors", "customer": "Acme Ltd", "site": "Acme HQ",
     "value": 1500, "status": "sent", "type": "remedial", "source_job": "J1", "created_by": "Josh"},
    {"id": "Q2", "title": "", "customer": "Beta Ltd", "site": None, "value": 10, "status": "sent"},
    {"id": "Q3", "title": "CCTV extension", "customer": "Gamma", "site": "Yard", "value": 6420,
     "extra": {"line_items": [{"item": "Camera", "qty": 4, "unit_price": 99, "supplier_cost": 60}],
               "staff_comments": "Josh says they will pay anything", "alarm_code": "1234", "margin": 0.4}},
]


def test_quote_scope_context_excludes_price_and_uses_source_job_notes():
    ctx, err = build_quote_scope_context(QUOTES, "q1", JOB)
    assert err is None
    assert ctx["quote"]["title"] == "Replace 3 failed smoke detectors" and ctx["quote"]["source_job"] == "J1"
    assert "value" not in ctx["quote"] and "created_by" not in ctx["quote"]
    assert ctx["source_job_notes"]["notes"][0]["text"].startswith("Replaced 2")
    assert not any("source job" in m for m in ctx["missing"])
    blob = json.dumps(ctx)
    for private in (*PRIVATE_EXTRA_KEYS, "4821", "nightmare"):  # the source job's private extras are filtered too
        assert private not in blob, private


def test_quote_scope_context_flags_gaps():
    ctx, err = build_quote_scope_context(QUOTES, "Q1", None)  # source job couldn't be read
    assert err is None and "source_job_notes" not in ctx
    missing = " ".join(ctx["missing"])
    assert "line items" in missing and "source job J1" in missing
    ctx, err = build_quote_scope_context(QUOTES, "Q3", None)
    assert err is None and ctx["details"]["line_items"][0]["qty"] == 4
    assert not any("line items" in m for m in ctx["missing"])
    assert ctx["details"] == {"line_items": [{"item": "Camera", "qty": 4}]}  # allowlisted key, private nested dropped
    blob = json.dumps(ctx)
    for private in ("staff_comments", "pay anything", "alarm_code", "1234", "margin", "unit_price", "supplier_cost"):
        assert private not in blob, private


def test_quote_scope_context_unknown_quote_or_nothing_to_go_on():
    ctx, err = build_quote_scope_context(QUOTES, "Q99")
    assert ctx is None and "couldn't find" in err
    assert build_quote_scope_context(QUOTES, "")[0] is None
    ctx, err = build_quote_scope_context(QUOTES, "Q2")
    assert ctx is None and "nothing to base a scope on" in err


# ---------------------------------------------------------------------------- end to end (scripted model)
def _jarvis(tmp_path):
    from jarvis.config import Settings
    from jarvis.core import Jarvis
    from tests.fakes import FakeClient

    return Jarvis(Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None), client=FakeClient())


def _last_prompt(j):
    call = j.client.beta.messages.calls[-1]
    return call["system"], json.loads(call["messages"][0]["content"])


def _stub_fsm(j, monkeypatch, jobs=None, quotes=None):
    jobs = jobs or {}

    async def job_detail(ref):
        if ref not in jobs:
            raise ValueError(f"No job '{ref}'")
        return jobs[ref]

    async def list_quotes(status=None):
        return list(quotes or [])

    monkeypatch.setattr(j.fsm, "job_detail", job_detail)
    monkeypatch.setattr(j.fsm, "quotes", list_quotes)


async def test_job_summary_end_to_end_is_display_only(tmp_path, monkeypatch):
    j = _jarvis(tmp_path)
    _stub_fsm(j, monkeypatch, jobs={"J1": JOB})
    writes = []

    async def no_write(*a, **k):
        writes.append((a, k))

    monkeypatch.setattr(j.fsm, "write", no_write)
    events = j.bus.subscribe()
    assert await j.documents.job_summary("J1") == "Certainly, sir."
    system, data = _last_prompt(j)
    assert "DRAFT ONLY" in system and "access, alarm, door or key codes" in system
    assert data["job"]["ref"] == "J1" and "value" not in data["job"]
    assert data["details"]["materials_used"][0]["sku"] == "DET-1"
    assert "4821" not in json.dumps(data) and "nightmare" not in json.dumps(data)  # private extras never sent
    shown = events.get_nowait()
    assert shown["type"] == "display" and "J1" in shown["data"]["title"] and shown["data"]["markdown"] == "Certainly, sir."
    assert writes == [] and j.db.pending_actions() == []  # nothing written to FSM, nothing queued either

    # not-found, not-completed and empty jobs: a plain message and no model call
    _stub_fsm(j, monkeypatch, jobs={"J2": JOB | {"ref": "J2", "status": "in_progress"},
                                    "J3": JOB | {"ref": "J3", "extra": {}}})
    before = len(j.client.beta.messages.calls)
    assert "couldn't read job" in await j.documents.job_summary("J99")
    assert "not completed" in await j.documents.job_summary("J2")
    assert "nothing to base a summary on" in await j.documents.job_summary("J3")
    assert len(j.client.beta.messages.calls) == before
    await j.http.aclose()


async def test_job_summary_works_on_the_demo_fsm(tmp_path):
    j = _jarvis(tmp_path)
    jobs = await j.fsm.jobs(status="completed")  # demo history always has completed jobs
    assert await j.documents.job_summary(jobs[0]["ref"]) == "Certainly, sir."
    await j.http.aclose()


async def test_quote_scope_end_to_end_is_display_only(tmp_path, monkeypatch):
    j = _jarvis(tmp_path)
    _stub_fsm(j, monkeypatch, jobs={"J1": JOB}, quotes=QUOTES)
    events = j.bus.subscribe()
    assert await j.documents.quote_scope("Q1") == "Certainly, sir."
    system, data = _last_prompt(j)
    assert "DRAFT ONLY" in system and "Do not state a price" in system
    assert data["quote"]["id"] == "Q1" and "value" not in data["quote"]
    assert data["source_job_notes"]["notes"][0]["by"] == "Dan Harper"
    shown = events.get_nowait()
    assert shown["type"] == "display" and "Q1" in shown["data"]["title"]

    # an unreadable source job doesn't stop the draft, it is flagged as missing
    _stub_fsm(j, monkeypatch, jobs={}, quotes=QUOTES)
    assert await j.documents.quote_scope("Q1") == "Certainly, sir."
    assert any("source job J1" in m for m in _last_prompt(j)[1]["missing"])

    before = len(j.client.beta.messages.calls)
    assert "couldn't find" in await j.documents.quote_scope("Q-NOPE")
    assert "nothing to base a scope on" in await j.documents.quote_scope("Q2")
    assert len(j.client.beta.messages.calls) == before
    await j.http.aclose()


async def test_quote_scope_works_on_the_demo_fsm(tmp_path):
    j = _jarvis(tmp_path)
    assert await j.documents.quote_scope("Q1180") == "Certainly, sir."
    await j.http.aclose()


async def test_writeup_tools_are_registered_draft_only(tmp_path, monkeypatch):
    from jarvis.brain.tools import (JobSummaryIn, QuoteScopeIn, TOOLS_BY_NAME, draft_job_summary,
                                    draft_quote_scope)

    for name in ("draft_job_summary", "draft_quote_scope"):
        tool = TOOLS_BY_NAME[name]
        assert tool.approval is False  # nothing to approve: it only drafts on the display
        assert "DRAFTS ONLY" in tool.description and "never written to Salts FSM" in tool.description
    assert TOOLS_BY_NAME["email_send"].approval is True and TOOLS_BY_NAME["fsm_change"].approval is True
    j = _jarvis(tmp_path)
    _stub_fsm(j, monkeypatch, jobs={"J1": JOB}, quotes=QUOTES)
    result = await draft_job_summary(j, JobSummaryIn(job_ref="J1"))
    assert result["shown_on_display"] is True and result["draft"] == "Certainly, sir."
    assert "nothing has been written to Salts FSM" in result["note"]
    result = await draft_quote_scope(j, QuoteScopeIn(quote_ref="Q3"))
    assert result["shown_on_display"] is True and result["draft"] == "Certainly, sir."
    await j.http.aclose()
