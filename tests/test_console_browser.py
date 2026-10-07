"""Real-browser checks of the console redesign (Phase 1: layout and look).

These drive the actual page in headless Chrome through Playwright, at 1280 / 800 / 400px wide in both themes. They are
skipped when Playwright or a Chrome/Chromium it can launch is not installed (CI installs neither), so the static
checks in test_hud_layout.py remain the always-on guard. To run them locally:

    python -m venv .pw && .pw/Scripts/pip install playwright pytest pytest-asyncio -r requirements.txt
    (uses the system Chrome: no browser download is needed)

The app is the real FastAPI app on a real local port with a scripted fake Claude client and demo data, seeded with one
pending approval, one failing routine test and one critical issue, so the rail counts and the "Needs you" strip have
something to show.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

import uvicorn  # noqa: E402

from jarvis.config import Settings  # noqa: E402
from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from tests.fakes import FakeClient  # noqa: E402

SIZES = [(1280, 800), (800, 900), (400, 820)]
THEMES = ["dark", "light"]
POPS = {  # rail item -> (drawer title, an element that proves the right content is showing)
    "approvals": ("Approvals", "#approvals"),
    "comms": ("Comms", "#inbox"),
    "issues": ("Issues and fixes", "#issues"),
    "health": ("Health", "#btn-run-tests"),
    "ops": ("Operations", "#ops-kpis"),
    "fleet": ("Fleet", "#fleet-status"),
    "finance": ("Finance", "#finance"),
    "presence": ("Presence", "#presence"),
    "upcoming": ("Coming up", "#deadlines"),
}


class _Server:
    def __init__(self, app):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 30
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        assert self.server.started, "test server did not start"
        self.url = f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _settings(tmp_path_factory, **extra):
    data = tmp_path_factory.mktemp("console")
    return Settings(data_dir=data / "data", scheduler_enabled=False, anthropic_api_key="test", _env_file=None, **extra)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    settings = _settings(tmp_path_factory)
    j = Jarvis(settings, client=FakeClient())
    j.db.create_action("note", "Send the Kestrel quote", {})
    j.db.add_test_run("voice", "Speech-to-text", False, "No API key", 5)
    j.db.create_issue(reporter="Dan", title="Panel will not arm", description="x", source="staff", severity="critical")
    srv = _Server(create_app(settings, j))
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def password_server(tmp_path_factory):
    settings = _settings(tmp_path_factory, jarvis_owner_password="a-local-test-password")
    srv = _Server(create_app(settings, Jarvis(settings, client=FakeClient())))
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        b = None
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                b = p.chromium.launch(headless=True, **kwargs)
                break
            except Exception:  # noqa: BLE001 - that browser is not installed here
                continue
        if b is None:
            pytest.skip("no Chrome/Chromium that Playwright can launch")
        yield b
        b.close()


def _open(browser, url, width, height, scheme="dark", **ctx):
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, **ctx)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#needs-list .need", timeout=15000)  # the first /api/status has rendered
    page.errors = errors
    return context, page


def _no_hscroll(page):
    return page.evaluate("[document.documentElement.scrollWidth, document.body.scrollWidth, innerWidth]")


def _drawer_fits(page):
    return page.evaluate("""() => {
      const d = document.getElementById('drawer'), c = document.getElementById('drawer-close').getBoundingClientRect();
      return { width: d.offsetWidth, vw: innerWidth, closeRight: c.right, closeLeft: c.left, scrollW: d.scrollWidth };
    }""")


def _close_button_reachable(page, width):
    f = _drawer_fits(page)
    assert f["width"] <= f["vw"], f
    assert f["scrollW"] <= f["width"] + 1, f   # nothing inside pushes the drawer wider than itself
    # the Close button ends up on screen once the slide-in has finished
    page.wait_for_timeout(450)
    f = _drawer_fits(page)
    assert 0 <= f["closeLeft"] and f["closeRight"] <= f["vw"], f


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_no_horizontal_scroll_and_every_rail_item_opens_its_popup(browser, server, width, height, scheme):
    ctx, page = _open(browser, server.url, width, height, scheme)
    try:
        doc, body, vw = _no_hscroll(page)
        assert doc <= vw and body <= vw, (doc, body, vw)
        for name, (title, proof) in POPS.items():
            page.click(f'.rail-item[data-pop="{name}"]')
            page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
            assert page.inner_text("#drawer-title") == title
            assert page.is_visible(f"#pop-{name}") and page.is_visible(proof), name
            assert page.evaluate("[...document.querySelectorAll('#drawer-body .pop')].filter(p => !p.hidden).length") == 1
            _close_button_reachable(page, width)
            doc, body, vw = _no_hscroll(page)
            assert doc <= vw and body <= vw, (name, doc, body, vw)
            page.click("#drawer-close")
            page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
        assert not page.errors, page.errors
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_settings_drawer_never_exceeds_the_window_even_mid_slide(browser, server, width, height, scheme):
    """The old Settings panel briefly rendered wider than the window with its Close button off-screen."""
    ctx, page = _open(browser, server.url, width, height, scheme)
    try:
        for pop in ("settings", "connections"):
            page.click(f'.tb-btn[data-pop="{pop}"]')
            # sample the drawer's width through the whole slide-in, not just once it has settled
            widths = [page.evaluate("document.getElementById('drawer').offsetWidth") for _ in range(6)]
            assert max(widths) <= width, widths
            assert _no_hscroll(page)[0] <= width
            _close_button_reachable(page, width)
            page.keyboard.press("Escape")
            page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
    finally:
        ctx.close()


def test_drawer_closes_with_close_button_escape_and_clicking_outside(browser, server):
    ctx, page = _open(browser, server.url, 1280, 800)
    try:
        for how in ("button", "escape", "outside"):
            page.click('.rail-item[data-pop="health"]')
            page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
            if how == "button":
                page.click("#drawer-close")
            elif how == "escape":
                page.keyboard.press("Escape")
            else:
                page.mouse.click(40, 400)  # over the dimmed console, left of the drawer
            page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
            assert page.evaluate("getComputedStyle(document.getElementById('drawer')).visibility") in ("hidden", "visible")
        # a closed drawer is out of the tab order
        # (visibility flips to hidden when the slide-out transition ends; a fixed 400 ms was not enough on a loaded machine)
        page.wait_for_function("getComputedStyle(document.getElementById('drawer')).visibility === 'hidden'", timeout=10000)
        assert page.evaluate("getComputedStyle(document.getElementById('drawer')).visibility") == "hidden"
    finally:
        ctx.close()


def test_rail_counts_needs_you_strip_and_colours(browser, server):
    ctx, page = _open(browser, server.url, 1280, 800)
    try:
        counts = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.rail-item')].map(
            b => [b.dataset.pop, [b.querySelector('.rail-count')?.textContent ?? null, b.dataset.level]]))""")
        assert set(counts) == set(POPS)
        assert counts["presence"][0] is None                 # Presence has no count
        assert counts["fleet"][0] == "off"                   # vehicle tracking is not connected in this app
        assert counts["approvals"] == ["1", "warn"]          # amber: needs a look
        assert counts["health"] == ["0/1", "bad"]            # red: passed/total, a routine test is failing
        assert counts["issues"] == ["1", "bad"]              # red: a critical issue is open
        assert page.inner_text("#rail .label").lower() == "on request"
        items = page.eval_on_selector_all("#needs-list .need", "els => els.map(e => [e.dataset.pop, e.dataset.level])")
        assert len(items) <= 3
        assert items[0] == ["health", "bad"]                 # most urgent first: the failing test...
        assert [i[0] for i in items[:3]] == ["health", "issues", "approvals"]  # ...then the critical issue, then the approval
        page.click("#needs-list .need >> nth=2")             # each item opens its pop-up
        page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
        assert page.inner_text("#drawer-title") == "Approvals"
        assert "Send the Kestrel quote" in page.inner_text("#approvals")
        assert page.is_visible('#approvals [data-act="approve"]') and page.is_visible('#approvals [data-act="deny"]')
    finally:
        ctx.close()


def test_top_bar_buttons_are_text_labelled_and_the_phone_rail_is_a_scrolling_strip(browser, server):
    ctx, page = _open(browser, server.url, 1280, 800)
    try:
        for sel, word in (("#btn-voice", "Voice"), ("#btn-proactive-mute", "Speaks up"), ("#btn-connections", "Connections"),
                          ("#btn-settings", "Settings")):
            assert word in page.inner_text(sel)
        assert page.text_content("#state") == "Online"
        # the mockup's order, left to right: brand, status, demo pill, clock, Voice, Speaks up, Connections, Settings
        xs = [page.eval_on_selector(sel, "e => e.getBoundingClientRect().left") for sel in
              (".topbar .brand", ".topbar .state", "#pills .pill", "#clock", "#btn-voice", "#btn-proactive-mute", "#btn-connections", "#btn-settings")]
        assert xs == sorted(xs), xs
        assert page.evaluate("document.getElementById('rail').getBoundingClientRect().width") == 190
        assert page.evaluate("document.getElementById('core-btn').getBoundingClientRect().width") == 150
        assert "Demo data" in page.inner_text("#pills") and page.inner_text("#clock").strip()
        assert page.evaluate("getComputedStyle(document.getElementById('rail')).flexDirection") == "column"
        # Voice toggles on/off
        page.click("#btn-voice")
        assert page.inner_text("#btn-voice") == "Voice off"
        page.click("#btn-voice")
        assert page.inner_text("#btn-voice") == "Voice on"
    finally:
        ctx.close()
    ctx, page = _open(browser, server.url, 400, 820, has_touch=True, is_mobile=True)
    try:
        assert page.evaluate("getComputedStyle(document.getElementById('rail')).flexDirection") == "row"
        assert page.evaluate("getComputedStyle(document.getElementById('rail')).overflowX") == "auto"
        assert page.evaluate("document.getElementById('rail').scrollWidth > document.getElementById('rail').clientWidth")
        assert _no_hscroll(page)[0] <= 400
    finally:
        ctx.close()


def test_every_connections_row_opens_its_form_with_a_back_link(browser, server):
    ctx, page = _open(browser, server.url, 1280, 800)
    try:
        page.click("#btn-connections")
        page.wait_for_selector(".conn-row")
        ids = page.eval_on_selector_all(".conn-row", "els => els.map(e => e.dataset.openSection)")
        assert len(ids) >= 10 and "fsm" in ids and "ram" in ids
        for sid in ids:
            page.click(f'.conn-row[data-open-section="{sid}"]')
            page.wait_for_selector(f'.set-section[data-section="{sid}"]')
            assert page.is_visible(".back-link") and page.is_visible(".form-head h3")
            assert page.is_visible(f'.set-section[data-section="{sid}"] .set-field')
            page.click(".back-link")
            page.wait_for_selector(".conn-row")
        assert page.evaluate("document.getElementById('pop-connections').scrollWidth <= document.getElementById('pop-connections').clientWidth + 1")
    finally:
        ctx.close()


def test_settings_still_save_from_a_connections_form(browser, server):
    ctx, page = _open(browser, server.url, 1280, 800)
    try:
        page.click("#btn-connections")
        page.click('.conn-row[data-open-section="profile"]')
        field = page.locator("#f-owner_name")
        field.fill("Alexander")
        assert page.is_visible("#settings-savebar")
        assert "1 change" in page.inner_text("#settings-status")
        with page.expect_response(lambda r: r.url.endswith("/api/settings") and r.request.method == "POST") as resp:
            page.click("#btn-settings-save")
        assert resp.value.status == 200
        page.wait_for_function("document.getElementById('settings-savebar').hidden")
        assert page.input_value("#f-owner_name") == "Alexander"
        # closing with unsaved edits asks first, and Cancel keeps the form as it was
        field.fill("Changed again")
        page.once("dialog", lambda d: d.dismiss())
        page.keyboard.press("Escape")
        assert page.evaluate("document.getElementById('drawer').classList.contains('open')")
        page.click("#btn-settings-cancel")
        assert page.input_value("#f-owner_name") == "Alexander"
    finally:
        ctx.close()


def test_voice_settings_and_theme_choice_persist(browser, server):
    ctx, page = _open(browser, server.url, 1280, 800, scheme="dark")
    try:
        assert page.evaluate("document.documentElement.getAttribute('data-theme')") is None   # Auto follows the device
        page.click("#btn-settings")
        page.select_option("#set-theme", "light")
        assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
        assert page.evaluate("localStorage.getItem('jarvis.theme')") == "light"
        page.wait_for_timeout(450)  # the background cross-fades
        bg_light = page.evaluate("getComputedStyle(document.body).backgroundColor")
        page.select_option("#set-theme", "dark")
        page.wait_for_timeout(450)
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") != bg_light
        page.select_option("#set-theme", "auto")
        assert page.evaluate("document.documentElement.getAttribute('data-theme')") is None
        # the existing voice options still save to localStorage
        page.select_option("#set-speak", "always")
        page.select_option("#set-listen", "ptt")
        page.select_option("#set-bargein", "0")
        assert page.evaluate("[localStorage.getItem('jarvis.speak'), localStorage.getItem('jarvis.listen'), localStorage.getItem('jarvis.bargein')]") == ["always", "ptt", "0"]
        assert page.inner_text("#btn-voice") == "Voice on"
        page.select_option("#set-speak", "off")
        assert page.inner_text("#btn-voice") == "Voice off"
        # the staff report key is never put on the page as text
        assert "key=" not in page.inner_text("#pop-settings")
        # reloading keeps the explicit choice
        page.select_option("#set-theme", "light")
        page.reload(wait_until="domcontentloaded")
        assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
    finally:
        ctx.close()


def test_light_follows_the_device_and_both_themes_have_readable_text(browser, server):
    for scheme, expect_light in (("light", True), ("dark", False)):
        ctx, page = _open(browser, server.url, 1280, 800, scheme)
        try:
            lum = page.evaluate("""() => { const m = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
              const c = document.createElement('canvas').getContext('2d'); c.fillStyle = m; c.fillRect(0,0,1,1);
              const d = c.getImageData(0,0,1,1).data; return (0.2126*d[0] + 0.7152*d[1] + 0.0722*d[2]) / 255; }""")
            assert (lum > 0.5) == expect_light, (scheme, lum)
        finally:
            ctx.close()


def test_shortcuts_menu_lists_the_nine_shortcuts_and_sends(browser, server):
    ctx, page = _open(browser, server.url, 1280, 800)
    try:
        assert not page.is_visible("#quick")
        page.click("#btn-shortcuts")
        labels = page.eval_on_selector_all("#quick .chip-btn", "els => els.map(e => e.textContent.trim())")
        assert labels == ["Briefing", "Wrap-up", "Team review", "Business health", "Cash flow", "Where's everyone?", "Stock", "Customers", "Marketing"]
        page.keyboard.press("Escape")
        assert not page.is_visible("#quick")
        page.click("#btn-shortcuts")
        page.click('#quick [data-q="What stock do we need to reorder?"]')
        assert not page.is_visible("#quick")
        page.wait_for_selector(".msg.user")
        assert "reorder" in page.inner_text(".msg.user")
        page.wait_for_selector(".msg.assistant .md:not(.typing)", timeout=15000)
    finally:
        ctx.close()


def test_send_becomes_stop_while_a_reply_streams_and_stop_aborts_the_request(browser, server):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    # No WebSocket, so the message goes over the plain-HTTP streaming fallback, which we then hold open.
    page.add_init_script("window.WebSocket = class { constructor() { this.readyState = 3; setTimeout(() => this.onclose && this.onclose({ code: 1006 }), 0); } send() {} close() {} };")
    held = {"aborted": False}

    def hold(route):
        held["seen"] = True
        # never fulfilled: the reply is 'still streaming' until the page aborts the request
    page.route("**/api/chat/stream", hold)
    page.goto(server.url + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#needs-list .need")
    try:
        assert page.is_visible("#btn-send") and not page.is_visible("#btn-stop")
        page.fill("#input", "Give me the long answer")
        page.click("#btn-send")
        page.wait_for_selector("#btn-stop", state="visible")
        assert not page.is_visible("#btn-send")
        assert page.text_content("#state") == "Working"
        with page.expect_request(lambda r: r.url.endswith("/api/interrupt")):
            page.click("#btn-stop")
        page.wait_for_selector("#btn-send", state="visible")
        assert not page.is_visible("#btn-stop") and page.text_content("#state") == "Online"
    finally:
        ctx.close()


def test_core_is_a_button_that_follows_state_and_is_static_under_reduced_motion(browser, server):
    ctx, page = _open(browser, server.url, 1280, 800, reduced_motion="no-preference")
    try:
        assert page.get_attribute("#core-btn", "aria-label")
        a = page.evaluate("document.getElementById('reactor').toDataURL()")
        page.wait_for_timeout(700)
        b = page.evaluate("document.getElementById('reactor').toDataURL()")
        assert a != b, "the core should animate"
    finally:
        ctx.close()
    ctx, page = _open(browser, server.url, 1280, 800, reduced_motion="reduce")
    try:
        a = page.evaluate("document.getElementById('reactor').toDataURL()")
        page.wait_for_timeout(700)
        b = page.evaluate("document.getElementById('reactor').toDataURL()")
        assert a == b, "under prefers-reduced-motion the core is a static frame"
    finally:
        ctx.close()


def test_sign_in_page_and_same_authentication(browser, password_server):
    for width, height in ((1280, 800), (400, 820)):
        for scheme in THEMES:
            ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
            page = ctx.new_page()
            page.goto(password_server.url + "/", wait_until="domcontentloaded")   # redirected to /login
            page.wait_for_selector("#pw")
            assert page.url.endswith("/login")
            assert page.inner_text("h1") == "Identify yourself, sir."
            assert page.is_visible("#login-core") and page.is_visible(".btn-signin") and page.text_content(".btn-signin") == "SIGN IN"
            assert page.evaluate("document.documentElement.scrollWidth") <= width
            # brand, then the heading, then the form - top to bottom
            ys = [page.eval_on_selector(sel, "e => e.getBoundingClientRect().top") for sel in (".login-core", ".brand", "h1", "#pw", ".btn-signin")]
            assert ys == sorted(ys), ys
            ctx.close()
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    try:
        page.goto(password_server.url + "/login", wait_until="domcontentloaded")
        page.fill("#pw", "not-the-password")
        page.click(".btn-signin")
        page.wait_for_selector("#err:has-text('not right')", timeout=15000)
        page.fill("#pw", "a-local-test-password")
        page.click(".btn-signin")
        page.wait_for_selector("#core-btn", timeout=15000)
        page.wait_for_selector("#needs-list .need, #needs-list .needs-clear", timeout=15000)
        assert page.url.rstrip("/") == password_server.url
    finally:
        ctx.close()


def test_clicking_the_core_starts_and_stops_listening(browser, server):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    # A stand-in speech recogniser, so the test does not depend on a real microphone.
    page.add_init_script("""window.webkitSpeechRecognition = window.SpeechRecognition = class {
      start() { this.running = true; } stop() { this.running = false; setTimeout(() => this.onend && this.onend(), 0); } abort() { this.stop(); } };""")
    page.goto(server.url + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#needs-list .need")
    try:
        assert page.text_content("#state") == "Online"
        page.click("#core-btn")
        page.wait_for_function("document.getElementById('state').textContent === 'Listening'")
        assert page.evaluate("document.getElementById('btn-mic').classList.contains('on')")
        assert page.evaluate("document.body.dataset.hud") == "listening"
        page.click("#core-btn")
        page.wait_for_function("document.getElementById('state').textContent === 'Online'")
        assert not page.evaluate("document.getElementById('btn-mic').classList.contains('on')")
    finally:
        ctx.close()
