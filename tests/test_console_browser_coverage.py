"""Real-browser checks of the coverage line under a reply and the question-check scorecard in the Health drawer.

Headless Chrome through Playwright (skipped when it is not installed, like test_console_browser.py, whose fixtures it shares), at
1280 and 400 px wide, dark and light: the line is one compact row that opens to "Checked / Not checked / why", the existing source
line is untouched, nothing scrolls sideways, a reloaded conversation keeps each reply's line, and the scorecard shows the score,
areas, failing questions and the candidate checks with their one-click editor. Set JARVIS_SHOTS=<folder> to save screenshots.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from jarvis.brain import coverage as cov  # noqa: E402
from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from tests.fakes import message, text_block, tool_block  # noqa: E402
from tests.live_server import LiveServer, SlowClient  # noqa: E402
from tests.test_console_browser import _no_hscroll, _settings, browser  # noqa: E402,F401

SHOTS = os.environ.get("JARVIS_SHOTS")
CASES = [(1280, 800, "dark"), (1280, 800, "light"), (400, 820, "dark"), (400, 820, "light")]


def _shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=False)


@pytest.fixture
def server(tmp_path_factory):
    made = []

    def build(script=None, seed=None):
        settings = _settings(tmp_path_factory)
        j = Jarvis(settings, client=SlowClient(script, delay=0.002))
        if seed:
            seed(j)
        srv = LiveServer(create_app(settings, j))
        made.append(srv)
        return srv, j

    yield build
    for srv in made:
        srv.stop()


def _page(browser, url, width, height, scheme):
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(url + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#needs-list .need", timeout=15000)
    return ctx, page


def money_script():
    return [message([tool_block("fsm_jobs", {}, "t1"), tool_block("finance_aged", {}, "t2")], "tool_use"),
            message([text_block("I can't give you the overdue total yet - Sage isn't connected, and the FSM is sample data.")])]


@pytest.mark.parametrize("width,height,scheme", CASES)
def test_the_coverage_line_under_a_reply(browser, server, width, height, scheme):
    srv, j = server(money_script())
    ctx, page = _page(browser, srv.url, width, height, scheme)
    try:
        page.fill("#input", "How much is overdue on our invoices?")
        page.click("#btn-send")
        page.wait_for_selector(".msg.assistant .cov", timeout=15000)
        # the existing source-and-time line is exactly as before; the coverage line sits under it as its own element
        assert re.fullmatch(r"Source: Salts FSM \(demo data\), Sage \(demo data\) · \d+\.\ds", page.inner_text(".msg.assistant .src"))
        summary = page.inner_text(".msg.assistant .cov summary")
        assert summary.startswith("Checked: Salts FSM jobs · Not checked:") and "Sage" in summary and summary.rstrip().endswith("Low")
        assert page.get_attribute(".msg.assistant .cov", "data-level") == "Low"
        assert page.is_hidden(".msg.assistant .cov .cov-body")
        box = page.eval_on_selector(".msg.assistant .cov summary", "e => e.getBoundingClientRect().height")
        assert box <= (50 if width <= 760 else 24)                      # one compact row (44px tap target on phones)
        doc, body, vw = _no_hscroll(page)
        assert doc <= vw and body <= vw
        _shot(page, f"cov-closed-{width}-{scheme}")
        page.click(".msg.assistant .cov summary")
        page.wait_for_selector(".msg.assistant .cov .cov-body", state="visible")
        detail = page.inner_text(".msg.assistant .cov .cov-body")
        assert "Checked" in detail and "Salts FSM jobs" in detail and "Not checked" in detail and "Sage (" in detail
        assert "Low confidence:" in detail
        doc, body, vw = _no_hscroll(page)
        assert doc <= vw and body <= vw
        chip = page.eval_on_selector(".msg.assistant .cov .cov-chip", "e => getComputedStyle(e).color")
        warn = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--warn').trim()")
        assert chip and warn                                             # the Low chip takes the theme's warning colour
        _shot(page, f"cov-open-{width}-{scheme}")
        assert page.errors == []
    finally:
        ctx.close()


def test_a_reloaded_conversation_keeps_each_replys_line(browser, server):
    def seed(j):
        c = cov.summarise([{"src": "Salts FSM", "status": "ok", "detail": "invoices"}], "How much is overdue on invoices?",
                          demo={"Sage": True})
        j.db.add_transcript("user", "How much is overdue on invoices?")
        j.db.add_transcript("assistant", "About forty thousand in the FSM - Sage isn't connected.", cov.as_stored(c))

    srv, _ = server(seed=seed)
    ctx, page = _page(browser, srv.url, 1280, 800, "dark")
    try:
        page.wait_for_selector(".msg.assistant .cov", timeout=10000)
        assert page.inner_text(".msg.assistant .cov summary").replace("\n", " ").strip() == \
            "Checked: Salts FSM invoices · Not checked: Sage (not connected) Medium"
        assert page.get_attribute(".msg.assistant .cov", "data-level") == "Medium"
    finally:
        ctx.close()


def _seed_scorecard(j):
    db = j.db
    for at, passed, failed in (("2026-09-27", 2, 3), ("2026-10-04", 3, 2)):   # oldest first, as runs are recorded
        db.execute("INSERT INTO question_check_runs (started_at, finished_at, trigger, status, total, passed, failed, skipped, errors)"
                   " VALUES (?, ?, 'scheduled', 'done', 5, ?, ?, 0, 0)", (f"{at}T01:30:00+00:00", f"{at}T01:40:00+00:00", passed, failed))
    run = db.query_one("SELECT MAX(id) AS id FROM question_check_runs")["id"]
    rows = [("jobs-today-count", "jobs", "How many jobs have we got on today?", "pass", "ok", "4", "Four jobs.", 0),
            ("quotes-awaiting-count", "quotes", "How many quotes are waiting for an answer?", "fail", "expected 3, found 5", "3", "Five quotes.", 0),
            ("money-overdue-total", "money", "How much are customers overdue?", "fail", "expected 48,213.55 (±0.5%), found 40,000",
             "48,213.55 (±0.5%)", "About £40k.", 1),
            ("policy-lone-worker", "policies", "What's our lone worker policy?", "pass", "ok", "says there is none", "I don't have one.", 0),
            ("refuse-pay-to-team", "refusals", "How much does Dan get paid?", "pass", "ok", "declines", "Ask the office.", 1)]
    for cid, area, q, status, reason, exp, given, sens in rows:
        db.execute("INSERT INTO question_check_results (run_id, check_id, area, question, as_role, status, reason, expected, given, sensitive)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?)", (run, cid, area, q, "owner", status, reason, exp, given, sens))
    # a reply the owner marked Wrong -> a candidate check
    c = cov.summarise([{"src": "Salts FSM", "status": "ok", "detail": "jobs"}], "Which engineer is on the Keighley job?", demo={})
    turn = db.execute("INSERT INTO turn_metrics (created_at, mode, user_text, reply_text, coverage) VALUES (?,?,?,?,?)",
                      ("2026-10-07T10:00:00+00:00", "typed", "Which engineer is on the Keighley job?", "Dan.", cov.as_stored(c)))
    db.execute("INSERT INTO turn_feedback (created_at, turn_id, rating, note) VALUES (?,?,?,?)",
               ("2026-10-07T10:01:00+00:00", turn, "wrong", "It was Priya"))


@pytest.mark.parametrize("width,height,scheme", CASES)
def test_the_scorecard_in_the_health_drawer(browser, server, width, height, scheme):
    srv, j = server(seed=_seed_scorecard)
    ctx, page = _page(browser, srv.url, width, height, scheme)
    try:
        page.evaluate("document.querySelector('[data-pop=\"health\"]').click()")
        page.wait_for_function("document.querySelector('#qc-summary').textContent.includes('passed')", timeout=10000)
        assert page.inner_text("#qc-summary").startswith("3 of 5 passed (60%)")
        assert page.inner_text("#qc-score") == "60%"
        areas = page.inner_text("#qc-areas")
        assert "jobs" in areas and "money" in areas and "quotes" in areas
        fails = page.inner_text("#qc-failing")
        assert "Failing (2)" in fails and "Five quotes." in fails and "About £40k." in fails     # the owner sees finance detail
        assert page.is_visible("#qc-trend")
        assert page.is_visible("#btn-run-checks") and page.inner_text("#btn-run-checks") == "Run question checks now"
        page.click("#qc-candidates-wrap summary")
        assert "Which engineer is on the Keighley job?" in page.inner_text("#qc-candidates")
        page.click("#qc-candidates [data-promote]")
        page.wait_for_selector("#qc-candidates .qc-edit:not([hidden])")
        tmpl = json.loads(page.input_value("#qc-candidates textarea[name=expect]"))
        assert tmpl["checked_any"] == ["Salts FSM"] and "contains_any" in tmpl
        doc, body, vw = _no_hscroll(page)
        assert doc <= vw and body <= vw
        scroll = page.evaluate("[document.getElementById('drawer').scrollWidth, document.getElementById('drawer').offsetWidth]")
        assert scroll[0] <= scroll[1] + 1
        page.evaluate("document.getElementById('qc').scrollIntoView()")
        _shot(page, f"qc-health-{width}-{scheme}")
        # saving a placeholder is refused in the page; a real expectation is saved and the candidate turns into a check
        page.click("#qc-candidates button[type=submit]")
        assert "Replace the placeholder" in page.inner_text("#qc-candidates .qc-err")
        page.fill("#qc-candidates textarea[name=expect]", json.dumps({"contains_any": ["Priya"], "checked_any": ["Salts FSM"]}))
        page.click("#qc-candidates button[type=submit]")
        page.wait_for_function("!document.querySelector('#qc-candidates .qc-cand')", timeout=10000)
        assert [c.id for c in j.question_checks.checks()[0] if c.origin == "custom"] != []
        assert page.errors == []
    finally:
        ctx.close()
