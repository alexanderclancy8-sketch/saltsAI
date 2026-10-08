"""Real-browser checks of the office / engineer split (tests/test_office_role.py has the backend rules).

Headless Chrome through Playwright (skipped when it is not installed, like test_team_console_browser.py whose helpers it shares),
at 1280 and 400px in both themes:

* the owner's Settings > Team access shows two sections - Office code and Engineer code - each with its own status, code box and
  buttons; setting the office code from there turns office sign-in on and leaves the engineer code as it was;
* the sign-in page is the same "Team sign-in" form for both; the CODE decides the role, and the top bar says so subtly
  ("Office · Pat", "Engineer · Sam");
* the office console is the engineer console - the same rail, top bar and Settings, no Finance / Approvals / Connections /
  Activity - and a balance only ever appears as a chat answer, in that office member's own conversation: not in the owner's
  console, not in an engineer's.

Set JARVIS_SHOTS=<folder> to also save a screenshot of each surface there.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from tests.fakes import FakeClient, message, text_block, tool_block  # noqa: E402
from tests.live_server import LiveServer  # noqa: E402
from tests.test_console_browser import THEMES, _no_hscroll, _settings, browser  # noqa: E402,F401
from tests.test_team_console_browser import (FORBIDDEN_API, OWNER_PW, TEAM_RAIL, _context, _shot, _sign_in_owner,  # noqa: E402
                                             _sign_in_team, _watch)

OFFICE_CODE = "office-code-for-the-browser"
ENGINEER_CODE = "engineer-code-for-the-browser"
BALANCE_REPLY = "Kestrel Alarms, account KES001, owe 350.30 in total and 100.30 of that is overdue."
SUMMARY = {"customer_id": "1", "customer": "Kestrel Alarms Ltd", "account_ref": "KES001", "owed": "350.30", "overdue": "100.30",
           "currency": "GBP", "as_of": "2026-10-08", "oldest_overdue_invoice": {"invoice_no": "INV-1001", "due_date": "2026-07-01",
                                                                              "days_overdue": 99, "outstanding": "100.10"},
           "handling": "x"}
SIZES = [(1280, 800), (400, 820)]


@pytest.fixture(scope="module")
def serve(tmp_path_factory, restore_process_timezone):
    """A real server with both codes on and a scripted balance answer for the office."""
    settings = _settings(tmp_path_factory, jarvis_owner_password=OWNER_PW)
    j = Jarvis(settings, client=FakeClient(default_text="Two jobs are on today."))
    j.team_codes.set_code("office", OFFICE_CODE)
    j.team_codes.set_code("engineer", ENGINEER_CODE)
    srv = LiveServer(create_app(settings, j))
    yield srv, j
    srv.stop()


@pytest.fixture
def fresh(tmp_path_factory):
    made = []

    def build():
        settings = _settings(tmp_path_factory, jarvis_owner_password=OWNER_PW)
        j = Jarvis(settings, client=FakeClient())
        j.team_codes.set_code("engineer", ENGINEER_CODE)    # what an upgraded install has: the old team code, now the engineer code
        srv = LiveServer(create_app(settings, j))
        made.append(srv)
        return srv, j

    yield build
    for srv in made:
        srv.stop()


def _open_settings(page):
    page.click('.tb-btn[data-pop="settings"]')
    page.wait_for_function("document.getElementById('drawer').classList.contains('open')")


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_settings_shows_an_office_code_and_an_engineer_code(browser, fresh, width, height, scheme):
    srv, j = fresh()
    ctx = _context(browser, width, height, scheme)
    owner = ctx.new_page()
    try:
        _sign_in_owner(owner, srv.url)
        _open_settings(owner)
        owner.wait_for_function("document.getElementById('team-access-status-office').textContent.startsWith('Off')")
        owner.wait_for_function("document.getElementById('team-access-status-engineer').textContent.startsWith('On')")
        heads = owner.eval_on_selector_all("#team-access-sec h4", "els => els.map(e => e.textContent.trim())")
        assert heads == ["Office code", "Engineer code"]
        assert owner.is_visible("#btn-team-set-office") and owner.is_hidden("#btn-team-off-office")
        assert owner.is_visible("#btn-team-off-engineer") and owner.text_content("#btn-team-set-engineer") == "Change code"
        for role in ("office", "engineer"):
            assert owner.evaluate(f"document.getElementById('team-code-{role}').type") == "password"
        owner.locator("#team-access-sec").scroll_into_view_if_needed()
        _shot(owner, f"office-settings-before-{width}-{scheme}")
        # the engineer code can't be reused for the office: refused with a plain message, nothing changes
        owner.fill("#team-code-office", ENGINEER_CODE)
        owner.click("#btn-team-set-office")
        owner.wait_for_selector(".toast")
        assert not j.office_access.enabled
        owner.fill("#team-code-office", OFFICE_CODE)
        owner.click("#btn-team-set-office")
        owner.wait_for_function("document.getElementById('team-access-status-office').textContent.startsWith('On')")
        assert owner.input_value("#team-code-office") == "" and OFFICE_CODE not in owner.content()
        assert owner.is_visible("#btn-team-off-office") and owner.text_content("#btn-team-set-office") == "Change code"
        assert j.team_codes.match(OFFICE_CODE) == "office" and j.team_codes.match(ENGINEER_CODE) == "engineer"
        owner.wait_for_timeout(400)
        owner.locator("#team-access-office").scroll_into_view_if_needed()
        _shot(owner, f"office-settings-after-{width}-{scheme}")
        doc, body, vw = _no_hscroll(owner)
        assert doc <= vw and body <= vw, (doc, body, vw)
        for sel in ("#team-access-office", "#team-access-engineer", "#btn-team-off-office", "#team-code-engineer"):
            box = owner.locator(sel).bounding_box()
            assert box and box["x"] >= 0 and box["x"] + box["width"] <= width, (sel, box)
        # switching the office code off leaves the engineer code on
        owner.once("dialog", lambda d: d.accept())
        owner.click("#btn-team-off-office")
        owner.wait_for_function("document.getElementById('team-access-status-office').textContent.startsWith('Off')")
        assert not j.office_access.enabled and j.team_access.enabled
        assert owner.inner_text("#team-access-status-engineer").startswith("On")
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_the_code_decides_the_role_and_the_top_bar_says_which(browser, serve, width, height, scheme):
    srv, j = serve
    seen_pages = {}
    contexts = []
    try:
        for role, name, code in (("office", "Pat Office", OFFICE_CODE), ("engineer", "Sam Walker", ENGINEER_CODE)):
            ctx = _context(browser, width, height, scheme)
            contexts.append(ctx)
            page = ctx.new_page()
            seen = _watch(page)
            _sign_in_team(page, srv.url, name=name, code=code)
            page.wait_for_timeout(800)
            label = "Office" if role == "office" else "Engineer"
            chip = page.locator("#role-chip")
            assert chip.is_visible() and chip.text_content() == f"{label} · {name}"
            assert role in chip.get_attribute("title")
            box = chip.bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= width
            assert page.evaluate("[...document.querySelectorAll('.rail-item')].map(b => b.dataset.pop)") == TEAM_RAIL
            assert page.evaluate("[...document.querySelectorAll('.tb-btn')].map(b => b.textContent.trim())") == ["Voice on", "Settings"]
            for pop in ("finance", "approvals", "connections", "activity", "memory"):
                assert page.evaluate(f"document.getElementById('pop-{pop}')") is None, (role, pop)
            doc, body, vw = _no_hscroll(page)
            assert doc <= vw and body <= vw
            _shot(page, f"{role}-console-{width}-{scheme}")
            calls = [(m, u, st) for m, u, st, _ in seen]
            assert not [c for c in calls if c[2] >= 400] and not [c for c in calls if FORBIDDEN_API.search(c[1])]
            seen_pages[role] = page.evaluate("document.body.dataset.teamRole")
            assert not page.errors, page.errors
        assert seen_pages == {"office": "office", "engineer": "engineer"}
    finally:
        for ctx in contexts:
            ctx.close()


def test_a_balance_is_only_a_chat_answer_in_the_office_members_own_conversation(browser, tmp_path_factory, monkeypatch):
    settings = _settings(tmp_path_factory, jarvis_owner_password=OWNER_PW)
    script = [message([tool_block("customer_balance", {"customer": "KES001"})], "tool_use"), message([text_block(BALANCE_REPLY)])]
    j = Jarvis(settings, client=FakeClient(script, default_text="Two jobs are on today."))
    j.team_codes.set_code("office", OFFICE_CODE)
    j.team_codes.set_code("engineer", ENGINEER_CODE)
    asked = []

    async def lookup(customer="", customer_id=None, site=None):
        asked.append(customer)
        return SUMMARY

    monkeypatch.setattr(j.customer_balance, "lookup", lookup)
    srv = LiveServer(create_app(settings, j))
    ctxs = [_context(browser, 1280, 800, "dark") for _ in range(3)]
    owner, office, engineer = (c.new_page() for c in ctxs)
    try:
        _sign_in_owner(owner, srv.url)
        _sign_in_team(engineer, srv.url, name="Sam Walker", code=ENGINEER_CODE)
        _sign_in_team(office, srv.url, name="Pat Office", code=OFFICE_CODE)
        office.fill("#input", "Kestrel Alarms are on the phone, account KES001 - what do they owe?")
        office.keyboard.press("Enter")
        office.wait_for_function(f"[...document.querySelectorAll('#conversation .msg.assistant .md')].some(e => e.textContent.includes('350.30'))",
                                 timeout=15000)
        assert asked == ["KES001"]
        assert office.locator("#conversation .appr-card").count() == 0
        _shot(office, "office-balance-answer-1280-dark")
        owner.wait_for_timeout(1000)
        for other in (owner, engineer):
            assert "350.30" not in other.content() and other.locator("#conversation .msg").count() == 0
        assert j.db.recent_transcript(20) == [] and j.brain.messages == []
    finally:
        for c in ctxs:
            c.close()
        srv.stop()
