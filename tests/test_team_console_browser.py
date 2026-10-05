"""Real-browser checks of console redesign Phase 4b, item 5: the team console.

Headless Chrome through Playwright (skipped when it is not installed, like test_console_browser.py, whose fixtures it shares),
at 1280 / 800 / 400px in both themes:

* the owner turns team sign-in on from Settings (no code change), and a team member signs in on the login page with a name and
  that code - the whole flow through the real pages;
* the team console is the cut-down one: Ops, Fleet, Presence and Coming up in the rail; Voice and Settings in the top bar; the
  role shown subtly; no Finance, Approvals, Comms, Issues, Health, Memory or Connections anywhere in the page, in Settings
  (display and voice preferences and Sign out only) or in the "Needs you" strip;
* nothing for a hidden section is even requested: every /api call the page makes is allowed (no 401/403), none is for
  approvals, finance, settings, memory, issues, the transcript or the owner's metrics, and no owner-only text is in the page
  or in anything it loaded;
* no horizontal scroll, every pop-up opens, and a team member can talk to their own Jarvis without it showing in the owner's.

Set JARVIS_SHOTS=<folder> to also save a screenshot of each surface there.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from tests.fakes import FakeClient  # noqa: E402
from tests.live_server import LiveServer  # noqa: E402
from tests.test_console_browser import SIZES, THEMES, _no_hscroll, _settings, browser  # noqa: E402,F401

SHOTS = os.environ.get("JARVIS_SHOTS")
OWNER_PW = "a-local-test-password"
TEAM_CODE = "team-code-for-the-browser"
SECRETS = ("SECRET-FINANCE-APPROVAL", "SECRET-ISSUE-TITLE", "SECRET-MEMORY-NOTE", "SECRET-NOTIFICATION", "SECRET-INBOX-MAIL")
TEAM_RAIL = ["ops", "fleet", "presence", "upcoming"]
OWNER_ONLY_POPS = ["approvals", "comms", "issues", "health", "finance", "demo", "memory", "connections"]
FORBIDDEN_API = re.compile(r"/api/(approvals|settings|memory|transcript|issues|feedback|reply-suggestion|voice-events|quality|"
                           r"team-access|staff-report-address|tests|briefing|wrapup|digests|suggestions|documents|images)")


def _shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def _seed_owner_data(j):
    """Things only the owner may see - each carries a marker no team page or response may ever contain."""
    j.db.create_action("note", "SECRET-FINANCE-APPROVAL pay the supplier invoice", {"x": 1})
    j.db.create_issue(reporter="Dan", title="SECRET-ISSUE-TITLE", description="x", source="staff", severity="critical")
    j.db.remember("SECRET-MEMORY-NOTE the director's pension")
    j.db.add_notification("warning", "SECRET-NOTIFICATION cash is low", "x")
    j.db.add_test_run("voice", "Speech-to-text", False, "No API key", 5)


@pytest.fixture(scope="module")
def serve_team(tmp_path_factory):
    """A real server with an owner password and team sign-in already on."""
    settings = _settings(tmp_path_factory, jarvis_owner_password=OWNER_PW)
    j = Jarvis(settings, client=FakeClient(default_text="Two jobs are on today, and one is running late."))
    j.team_access.set_code(TEAM_CODE)
    _seed_owner_data(j)
    srv = LiveServer(create_app(settings, j))
    yield srv, j
    srv.stop()


@pytest.fixture
def fresh(tmp_path_factory):
    """A real server where team sign-in is NOT on yet: the owner turns it on from Settings."""
    made = []

    def build():
        settings = _settings(tmp_path_factory, jarvis_owner_password=OWNER_PW)
        j = Jarvis(settings, client=FakeClient())
        srv = LiveServer(create_app(settings, j))
        made.append(srv)
        return srv, j

    yield build
    for srv in made:
        srv.stop()


def _context(browser, width, height, scheme):
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    return ctx


def _sign_in_team(page, url, name="Sam Walker", code=TEAM_CODE):
    page.goto(url + "/", wait_until="domcontentloaded")          # redirected to /login
    page.wait_for_selector("#team-toggle")
    page.click("#team-toggle")
    page.wait_for_selector("#team-form:not([hidden])")
    page.fill("#team-name", name)
    page.fill("#team-code", code)
    page.click(".btn-team-signin")
    page.wait_for_selector("#needs-list .need, #needs-list .needs-clear:not(:has-text('Loading'))", timeout=15000)


def _sign_in_owner(page, url):
    page.goto(url + "/", wait_until="domcontentloaded")
    page.fill("#pw", OWNER_PW)
    page.click(".btn-signin")
    page.wait_for_selector("#needs-list .need, #needs-list .needs-clear:not(:has-text('Loading'))", timeout=15000)


def _watch(page):
    """Record every request the page makes to /api and what came back (status, body)."""
    seen = []

    def on_response(r):
        if "/api/" in r.url:
            try:
                body = r.text()
            except Exception:  # noqa: BLE001 - streamed or already closed
                body = ""
            seen.append((r.request.method, re.sub(r"^https?://[^/]+", "", r.url), r.status, body))

    page.on("response", on_response)
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    return seen


def _open_pop(page, name):
    page.click(f'.rail-item[data-pop="{name}"]')
    page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
    page.wait_for_timeout(350)


# ------------------------------------------------------------------------------- the owner sets it up, the team signs in
@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", [(1280, 800), (400, 820)])
def test_the_owner_turns_team_access_on_in_settings_and_a_team_member_signs_in(browser, fresh, width, height, scheme):
    srv, j = fresh()
    owner_ctx = _context(browser, width, height, scheme)
    owner = owner_ctx.new_page()
    try:
        _sign_in_owner(owner, srv.url)
        assert owner.text_content("#role-chip") == "Owner"
        owner.click('.tb-btn[data-pop="settings"]')
        owner.wait_for_function("document.getElementById('drawer').classList.contains('open')")
        owner.wait_for_function("document.getElementById('team-access-status').textContent.startsWith('Off')")
        assert owner.is_visible("#btn-team-set") and owner.is_hidden("#btn-team-off")
        assert owner.evaluate("document.getElementById('team-code').type") == "password"
        owner.locator("#team-access-sec").scroll_into_view_if_needed()
        _shot(owner, f"owner-team-access-off-{width}-{scheme}")
        owner.fill("#team-code", "short")                       # too short: refused, nothing saved
        owner.click("#btn-team-set")
        owner.wait_for_selector(".toast.warning, .toast")
        assert not j.team_access.enabled
        owner.fill("#team-code", TEAM_CODE)
        owner.click("#btn-team-set")
        owner.wait_for_function("document.getElementById('team-access-status').textContent.startsWith('On')")
        assert owner.input_value("#team-code") == ""             # the code is never shown again
        assert owner.is_visible("#btn-team-off") and owner.text_content("#btn-team-set") == "Change code"
        assert TEAM_CODE not in owner.content()
        owner.locator("#team-access-sec").scroll_into_view_if_needed()
        _shot(owner, f"owner-team-access-on-{width}-{scheme}")
        doc, body, vw = _no_hscroll(owner)
        assert doc <= vw and body <= vw, (doc, body, vw)
        assert j.team_access.enabled and j.team_access.verify(TEAM_CODE)

        team_ctx = _context(browser, width, height, scheme)
        team = team_ctx.new_page()
        try:
            team.goto(srv.url + "/", wait_until="domcontentloaded")
            team.wait_for_selector("#team-toggle")
            assert team.is_visible("#owner-form") and team.is_hidden("#team-form")
            team.click("#team-toggle")
            assert team.is_visible("#team-form") and team.is_hidden("#owner-form")
            assert team.text_content("#team-toggle") == "Owner sign-in"
            doc, body, vw = _no_hscroll(team)
            assert doc <= vw and body <= vw
            _shot(team, f"team-login-{width}-{scheme}")
            team.fill("#team-name", "Sam Walker")
            team.fill("#team-code", "not-the-team-code")          # wrong code: stays on the login page with a plain message
            team.click(".btn-team-signin")
            team.wait_for_selector("#team-err:not(:empty)")
            assert team.url.endswith("/login?team=1&error=1") and "isn't right" in team.inner_text("#team-err")
            assert team.is_visible("#team-form")
            _shot(team, f"team-login-wrong-{width}-{scheme}")
            team.fill("#team-name", "Sam Walker")
            team.fill("#team-code", TEAM_CODE)
            team.click(".btn-team-signin")
            team.wait_for_selector("#needs-list .need, #needs-list .needs-clear:not(:has-text('Loading'))", timeout=15000)
            assert team.text_content("#role-chip") == "Team · Sam Walker"
        finally:
            team_ctx.close()
    finally:
        owner_ctx.close()


def test_switching_team_access_off_from_settings_signs_everyone_out(browser, fresh):
    srv, j = fresh()
    j.team_access.set_code(TEAM_CODE)
    owner_ctx, team_ctx = _context(browser, 1280, 800, "dark"), _context(browser, 1280, 800, "dark")
    owner, team = owner_ctx.new_page(), team_ctx.new_page()
    try:
        _sign_in_owner(owner, srv.url)
        _sign_in_team(team, srv.url)
        owner.once("dialog", lambda d: d.accept())
        owner.click('.tb-btn[data-pop="settings"]')
        owner.wait_for_function("document.getElementById('team-access-status').textContent.startsWith('On')")
        owner.click("#btn-team-off")
        owner.wait_for_function("document.getElementById('team-access-status').textContent.startsWith('Off')")
        assert not j.team_access.enabled
        team.reload(wait_until="domcontentloaded")
        team.wait_for_url(re.compile(r"/login"))                 # the old session is no longer any good
    finally:
        owner_ctx.close()
        team_ctx.close()


# --------------------------------------------------------------------------------------------------- the team console
@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_the_team_console_is_cut_down_everywhere(browser, serve_team, width, height, scheme):
    srv, j = serve_team
    ctx = _context(browser, width, height, scheme)
    page = ctx.new_page()
    seen = _watch(page)
    try:
        _sign_in_team(page, srv.url)
        page.wait_for_timeout(1500)  # let the first status / fleet / voices requests finish
        # the rail: Ops, Fleet, Presence, Coming up - and nothing else
        assert page.evaluate("[...document.querySelectorAll('.rail-item')].map(b => b.dataset.pop)") == TEAM_RAIL
        # the top bar: Voice and Settings; the role, shown subtly; no Connections, no Speaks up
        assert page.evaluate("[...document.querySelectorAll('.tb-btn')].map(b => b.textContent.trim())") == ["Voice on", "Settings"]
        chip = page.locator("#role-chip")
        assert chip.is_visible() and chip.text_content() == "Team · Sam Walker"
        box = chip.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width
        # the markup of the hidden sections is not in the page at all
        for pop in OWNER_ONLY_POPS:
            assert page.evaluate(f"document.getElementById('pop-{pop}')") is None, pop
        assert page.evaluate("['btn-connections','btn-proactive-mute','orb-badge','btn-attach','reply-hint','settings-savebar',"
                             "'team-access-sec','btn-open-memory','btn-copy-report','btn-sage','set-talk'].filter(i => document.getElementById(i))") == []
        html = page.content()
        for secret in SECRETS:
            assert secret not in html, secret
        # Needs-you shows nothing about approvals, finance, issues, health or comms
        needs = page.inner_text("#needs-list")
        assert not re.search(r"approv|finance|overdue from customers|issue|check|failing|unread|suggestion", needs, re.I), needs
        assert page.evaluate("[...document.querySelectorAll('#needs-list .need')].every(n => ['ops','fleet','upcoming'].includes(n.dataset.pop))")
        # the message box: team shortcuts, no attachments
        page.click("#btn-shortcuts")
        assert [b.strip() for b in page.eval_on_selector_all("#quick .chip-btn", "els => els.map(e => e.textContent)")] == [
            "Today's jobs", "Overdue jobs", "Systems due", "Where's everyone?", "Reviews"]
        page.keyboard.press("Escape")
        _shot(page, f"team-console-{width}-{scheme}")
        # every pop-up the rail offers opens and fits; none mentions the owner's data
        for pop in TEAM_RAIL:
            _open_pop(page, pop)
            assert page.is_visible(f"#pop-{pop}") and page.evaluate("[...document.querySelectorAll('#drawer-body .pop')].filter(p => !p.hidden).length") == 1
            d = page.evaluate("(() => { const d = document.getElementById('drawer'), c = document.getElementById('drawer-close').getBoundingClientRect();"
                              " return {w: d.offsetWidth, vw: innerWidth, right: c.right, left: c.left}; })()")
            assert d["w"] <= d["vw"] and 0 <= d["left"] and d["right"] <= d["vw"], d
            doc, body, vw = _no_hscroll(page)
            assert doc <= vw and body <= vw, (pop, doc, body, vw)
            if pop in ("ops", "fleet"):
                _shot(page, f"team-{pop}-{width}-{scheme}")
            page.click("#drawer-close")
            page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
        assert "Vehicle tracking is not connected" in (_open_pop(page, "fleet") or page.inner_text("#fleet-status"))
        assert "Connections" not in page.inner_text("#fleet-status")
        page.click("#drawer-close")
        # Settings: display and voice preferences and Sign out only
        page.click('.tb-btn[data-pop="settings"]')
        page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
        page.wait_for_timeout(350)
        assert page.inner_text("#drawer-title") == "Settings"
        headings = page.eval_on_selector_all("#pop-settings h3", "els => els.map(e => e.textContent.trim())")
        assert headings == ["Display", "Voice", "Account"], headings
        buttons = page.eval_on_selector_all("#pop-settings button, #pop-settings a.btn", "els => els.map(e => e.textContent.trim())")
        assert "Sign out" in buttons and not [b for b in buttons if re.search(r"Connections|Memory|staff report|Sage", b, re.I)], buttons
        text = page.inner_text("#pop-settings")
        assert not re.search(r"staff report|How Jarvis talks|Team access|Connections", text, re.I), text
        d = page.evaluate("(() => { const d = document.getElementById('drawer'), c = document.getElementById('drawer-close').getBoundingClientRect(); return {w: d.offsetWidth, vw: innerWidth, right: c.right}; })()")
        assert d["w"] <= d["vw"] and d["right"] <= d["vw"]
        _shot(page, f"team-settings-{width}-{scheme}")
        page.click("#drawer-close")
        # no page error, and every /api call was allowed and none was for a hidden section
        assert not page.errors, page.errors
        calls = [(m, u, st) for m, u, st, _ in seen]
        assert calls, "the console made no API calls at all?"
        assert not [c for c in calls if c[2] >= 400], [c for c in calls if c[2] >= 400]
        assert not [c for c in calls if FORBIDDEN_API.search(c[1])], [c for c in calls if FORBIDDEN_API.search(c[1])]
        for m, u, st, body in seen:
            for secret in SECRETS:
                assert secret not in body, (u, secret)
        assert any(u.startswith("/api/status") for _, u, _ in calls)
    finally:
        ctx.close()


def test_the_owners_console_still_has_everything_and_the_team_controls(browser, serve_team):
    srv, j = serve_team
    ctx = _context(browser, 1280, 800, "dark")
    page = ctx.new_page()
    seen = _watch(page)
    try:
        _sign_in_owner(page, srv.url)
        assert page.evaluate("[...document.querySelectorAll('.rail-item')].map(b => b.dataset.pop)") == [
            "approvals", "comms", "issues", "health", "ops", "fleet", "finance", "presence", "upcoming"]
        assert page.evaluate("[...document.querySelectorAll('.tb-btn')].map(b => b.textContent.trim())") == [
            "Voice on", "Speaks up", "Connections", "Settings"]
        assert page.text_content("#role-chip") == "Owner"
        assert page.evaluate("document.getElementById('quick').querySelectorAll('.chip-btn').length") == 9  # the owner's shortcuts
        page.click('.tb-btn[data-pop="settings"]')
        page.wait_for_function("document.getElementById('team-access-status').textContent.startsWith('On')")
        page.wait_for_timeout(300)
        for pop in ("approvals", "finance"):
            assert page.evaluate(f"document.getElementById('pop-{pop}') !== null")
        assert any(u.startswith("/api/approvals/inbox") for _, u, _, _ in seen)
        assert not page.errors
    finally:
        ctx.close()


def test_a_team_conversation_is_private_and_has_no_owner_controls(browser, serve_team):
    srv, j = serve_team
    owner_ctx, team_ctx = _context(browser, 1280, 800, "dark"), _context(browser, 1280, 800, "dark")
    owner, team = owner_ctx.new_page(), team_ctx.new_page()
    seen = _watch(team)
    try:
        _sign_in_owner(owner, srv.url)
        _sign_in_team(team, srv.url)
        team.fill("#input", "What jobs are on today?")
        team.keyboard.press("Enter")
        team.wait_for_selector("#conversation .msg.assistant .md:not(.typing)", timeout=15000)
        team.wait_for_function("document.querySelector('#conversation .msg.assistant .md').textContent.includes('Two jobs are on today')")
        assert team.locator("#conversation .msg.user").count() == 1
        assert team.text_content("#conversation .msg.user .meta").startswith("Sam Walker")
        assert team.locator("#conversation .fb").count() == 0               # no good/wrong buttons: not the owner's metrics
        assert team.locator("#conversation .appr-card").count() == 0        # no approval cards in a team conversation
        _shot(team, "team-conversation-1280-dark")
        owner.wait_for_timeout(800)
        assert owner.locator("#conversation .msg").count() == 0             # nothing of it reached the owner's console
        assert j.db.recent_transcript(20) == [] and j.brain.messages == []  # nor the owner's record
        # a quick shortcut sends the team question as a message too
        team.click("#btn-shortcuts")
        team.click("#quick .chip-btn >> text=Overdue jobs")
        team.wait_for_function("document.querySelectorAll('#conversation .msg.user').length === 2")
        assert not team.errors if hasattr(team, "errors") else True
        assert not [1 for m, u, st, b in seen if st >= 400]
        assert not [u for m, u, st, b in seen if FORBIDDEN_API.search(u)]
    finally:
        owner_ctx.close()
        team_ctx.close()


def test_a_team_member_can_sign_out_and_is_sent_back_to_the_login_page(browser, serve_team):
    srv, _ = serve_team
    ctx = _context(browser, 1280, 800, "dark")
    page = ctx.new_page()
    try:
        _sign_in_team(page, srv.url)
        page.click('.tb-btn[data-pop="settings"]')
        page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
        page.click('#pop-settings button:has-text("Sign out")')
        page.wait_for_url(re.compile(r"/login$"))
        page.goto(srv.url + "/", wait_until="domcontentloaded")
        page.wait_for_url(re.compile(r"/login"))
    finally:
        ctx.close()
