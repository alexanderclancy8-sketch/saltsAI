"""Real-browser checks of console redesign Phase 3 (fixes found on 2 Oct).

Headless Chrome through Playwright (skipped when it is not installed, like test_console_browser.py, whose fixtures it
shares), at 1280 / 800 / 400px in both themes:

* the scheduled checks show as ONE summary row ("Scheduled checks - 9 jobs, 560 runs since 00:06, nothing to report") that opens a
  bounded, self-scrolling panel of one line per check ("Pull request watch · 7 checks since 09:30, no change"), each of which opens to
  its runs with their times; none of it is ever a chat message, and a check that needs a look is named on the summary row;
* a speech-to-text engine that is chosen but has no key shows a plain text label in the top bar and voice input falls back to
  the browser's speech recognition;
* the Fleet pop-up says "not connected" (still sample data, or entered but failing, with the reason) and never a blank map;
* the staff report key is nowhere in the page as rendered, in anything the page loaded, or in browser storage, yet the
  "Copy staff report link" button really puts the link on the clipboard.

Set JARVIS_SHOTS=<folder> to also save a screenshot of each surface there.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright.sync_api")

from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from jarvis.services.tracking import Tracker  # noqa: E402
from tests.fakes import FakeClient  # noqa: E402
from tests.live_server import LiveServer  # noqa: E402
from tests.test_console_browser import SIZES, THEMES, _no_hscroll, _settings, browser  # noqa: E402,F401

SHOTS = os.environ.get("JARVIS_SHOTS")
KEY = "k9X-staff-secret-ZZ42"


def _shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def _page(browser, url, width=1280, height=800, scheme="dark", **ctx):
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, **ctx)
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(url + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#needs-list .need, #needs-list .needs-clear:not(:has-text('Loading'))", timeout=15000)
    return context, page


@pytest.fixture
def serve(tmp_path_factory):
    made = []

    def build(configure=None, http=None, client=None, **settings_kw):
        settings = _settings(tmp_path_factory, **settings_kw)
        if configure:
            configure(settings)
        j = Jarvis(settings, client=client or FakeClient(), http=http) if http else Jarvis(settings, client=client or FakeClient())
        srv = LiveServer(create_app(settings, j))
        made.append(srv)
        return srv, j, settings

    yield build
    for srv in made:
        srv.stop()


# --------------------------------------------------------------------------- scheduled checks: ONE summary line
# Changed after the owner's screenshot (a console with nine scheduled jobs showed nine ~45px lines that covered Jarvis's reply and
# clipped its Good / Wrong chips): the chat now has ONE slim "Scheduled checks - ..." row; the per-check lines (each of which still
# opens to its runs) live inside a bounded, self-scrolling panel that opens from it, so they can never push the conversation away.
def _seven_quiet_runs(j):
    for n in range(7):
        j.activity.record("pr_watch", "Pull request watch", "baseline" if n == 0 else "no_change",
                          "First look: 0 open." if n == 0 else "No change.")


JOBS = ["Council portal request scan", "Voicemail job intake", "Issue email scan", "Purchase order scan", "Upsell drafts",
        "Suggestions sync", "Lone-worker check", "Pull request watch", "Overdue jobs check", "Quote follow-up scan",
        "Inbox sweep", "Fleet position check"]


def _quiet_jobs(j, n=9, runs=4):
    for i, name in enumerate(JOBS[:n]):
        for _ in range(runs):
            j.activity.record(f"job_{i}", name, "no_change", "Nothing to report.")


def _open_checks(page):
    page.click("#activity-toggle")
    page.wait_for_selector("#activity", state="visible")


def _text_is(page, selector, text):
    """The Show / Hide word follows the `toggle` event, which the browser fires a moment after the click: wait for it."""
    page.wait_for_function("([s, t]) => document.querySelector(s)?.textContent === t", arg=[selector, text], timeout=5000)


def _log_state(page):
    return page.evaluate("""() => { const l = document.getElementById('conversation'), r = l.getBoundingClientRect();
      return { top: l.scrollTop, gap: l.scrollHeight - l.scrollTop - l.clientHeight, h: r.height, bottom: r.bottom }; }""")


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_the_collapsed_activity_line_renders_and_expands_to_list_the_runs(browser, serve, width, height, scheme):
    srv, j, _ = serve()
    _seven_quiet_runs(j)
    ctx, page = _page(browser, srv.url, width, height, scheme)
    try:
        page.wait_for_selector("#activity-toggle:not([hidden])")
        # ONE summary row for all the checks, collapsed: the per-check lines are not in view until it is opened
        assert page.locator("#activity-toggle").count() == 1
        assert page.get_attribute("#activity-toggle", "aria-expanded") == "false"
        summary = page.text_content("#activity-toggle .auto-all-text")
        assert re.fullmatch(r"Scheduled checks - 1 job, 7 runs since \d\d:\d\d, nothing to report", summary), summary
        assert page.inner_text("#activity-toggle u") == "Show"
        assert not page.is_visible("#activity") and not page.is_visible("details.auto")
        assert page.locator("#conversation .msg").count() == 0  # it is not a chat message
        _shot(page, f"activity-collapsed-{width}-{scheme}")
        _open_checks(page)
        assert page.get_attribute("#activity-toggle", "aria-expanded") == "true"
        _text_is(page, "#activity-toggle u", "Hide")
        # inside the panel: ONE line for the check (not seven), collapsed, which opens to list each run
        assert page.locator("details.auto").count() == 1
        assert page.evaluate("document.querySelector('details.auto').open") is False
        line = page.inner_text("details.auto summary > span")
        assert re.fullmatch(r"Pull request watch · 7 checks since \d\d:\d\d, no change", line), line
        assert page.inner_text("details.auto summary u") == "Show"
        assert not page.is_visible("details.auto .auto-runs span")  # the runs are hidden until it is opened
        page.click("details.auto summary")
        assert page.evaluate("document.querySelector('details.auto').open") is True
        _text_is(page, "details.auto summary u", "Hide")
        runs = page.locator("details.auto .auto-runs span")
        assert runs.count() == 7
        assert re.match(r"\d\d:\d\d First look", runs.nth(0).inner_text())
        assert all(re.match(r"\d\d:\d\d No change\.", runs.nth(i).inner_text()) for i in range(1, 7))
        doc, body, vw = _no_hscroll(page)
        assert doc <= vw and body <= vw, (doc, body, vw)
        box = page.locator("#activity").bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width
        _shot(page, f"activity-expanded-{width}-{scheme}")
        page.click("details.auto summary")  # the line closes again
        assert page.evaluate("document.querySelector('details.auto').open") is False
        _text_is(page, "details.auto summary u", "Show")
        page.click("#activity-toggle")      # and so does the panel
        assert not page.is_visible("#activity") and page.get_attribute("#activity-toggle", "aria-expanded") == "false"
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_the_activity_line_is_keyboard_operable_and_is_left_alone_when_nothing_new_has_run(browser, serve):
    srv, j, _ = serve()
    _seven_quiet_runs(j)
    ctx, page = _page(browser, srv.url)
    try:
        page.wait_for_selector("#activity-toggle:not([hidden])")
        page.focus("#activity-toggle")
        page.keyboard.press("Enter")
        assert page.is_visible("#activity")
        page.focus("details.auto summary")
        page.keyboard.press("Enter")
        assert page.evaluate("document.querySelector('details.auto').open") is True
        page.evaluate("document.querySelector('details.auto').dataset.mark = 'same-element'")
        # a real round trip refreshes the console's status (the reply triggers it): the line is not rebuilt under the reader
        page.fill("#input", "hello")
        page.keyboard.press("Enter")
        page.wait_for_selector("#conversation .msg.assistant")
        page.wait_for_timeout(2500)
        assert page.evaluate("document.querySelector('details.auto').dataset.mark") == "same-element"
        assert page.evaluate("document.querySelector('details.auto').open") is True
        assert page.is_visible("#activity")  # and the panel is neither collapsed nor re-opened by the refresh
        page.focus("details.auto summary")
        page.keyboard.press("Space")  # Space also toggles it
        assert page.evaluate("document.querySelector('details.auto').open") is False
        page.keyboard.press("Escape")  # Escape closes the panel and hands focus back to its row
        assert not page.is_visible("#activity")
        assert page.evaluate("document.activeElement.id") == "activity-toggle"
    finally:
        ctx.close()


def test_a_check_that_found_something_shows_as_a_change_on_its_line(browser, serve):
    srv, j, _ = serve()
    for n in range(3):
        j.activity.record("automation_1", "Overdue jobs check", "no_change", "Nothing to report.")
    j.activity.record("automation_1", "Overdue jobs check", "changed", "3 jobs are overdue.")
    j.activity.record("automation_2", "Inbox sweep", "failed", "Failed: RuntimeError")
    ctx, page = _page(browser, srv.url)
    try:
        page.wait_for_selector("#activity-toggle:not([hidden])")
        _open_checks(page)
        lines = [t.strip() for t in page.locator("details.auto summary > span").all_inner_texts()]
        assert any(re.fullmatch(r"Overdue jobs check · 4 checks since \d\d:\d\d, 1 change", t) for t in lines), lines
        assert any(re.fullmatch(r"Inbox sweep · 1 check since \d\d:\d\d, 1 failed", t) for t in lines), lines
        assert page.locator('details.auto[data-level="warn"]').count() == 1
        assert page.locator('details.auto[data-level="bad"]').count() == 1
        _shot(page, "activity-change-and-failed")
    finally:
        ctx.close()


def test_the_activity_area_is_absent_until_something_has_run(browser, serve):
    srv, _, _ = serve()
    ctx, page = _page(browser, srv.url)
    try:
        assert page.locator("details.auto").count() == 0 and not page.is_visible("#activity") and not page.is_visible("#activity-toggle")
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
def test_the_summary_line_flags_a_failing_or_changed_check_by_name_in_the_red_or_amber_accent(browser, serve, scheme):
    srv, j, _ = serve()
    _quiet_jobs(j, 6)
    j.activity.record("job_1", "Voicemail job intake", "failed", "Failed: TimeoutError")
    ctx, page = _page(browser, srv.url, 1280, 800, scheme)
    try:
        page.wait_for_selector("#activity-toggle:not([hidden])")
        assert page.text_content("#activity-toggle .auto-all-text") == "Scheduled checks - 1 needs a look: Voicemail job intake"
        assert page.get_attribute("#activity-toggle", "data-level") == "bad"
        flagged = page.evaluate("getComputedStyle(document.querySelector('#activity-toggle .auto-all-text')).color")
        calm = page.evaluate("getComputedStyle(document.querySelector('#activity-toggle u')).color")
        assert flagged != calm  # the accent is real, not just an attribute
        assert page.locator("details.auto").count() == 6 and not page.is_visible("details.auto")
        _shot(page, f"summary-failed-{scheme}")
        _open_checks(page)
        assert page.locator('details.auto[data-level="bad"] summary > span').inner_text().startswith("Voicemail job intake")
    finally:
        ctx.close()


def test_several_checks_needing_a_look_are_named_and_counted_and_a_failure_outranks_a_change(browser, serve):
    srv, j, _ = serve()
    _quiet_jobs(j, 6)
    j.activity.record("job_1", "Voicemail job intake", "failed", "Failed: TimeoutError")
    j.activity.record("job_2", "Issue email scan", "changed", "2 new.")
    j.activity.record("job_3", "Purchase order scan", "changed", "1 new.")
    ctx, page = _page(browser, srv.url)
    try:
        page.wait_for_selector("#activity-toggle:not([hidden])")
        text = page.text_content("#activity-toggle .auto-all-text")
        assert text.startswith("Scheduled checks - 3 need a look: ") and text.endswith("+1 more"), text
        assert page.get_attribute("#activity-toggle", "data-level") == "bad"
    finally:
        ctx.close()
    srv2, j2, _ = serve()   # only a change, no failure: amber
    _quiet_jobs(j2, 3)
    j2.activity.record("job_0", "Council portal request scan", "changed", "1 new.")
    ctx, page = _page(browser, srv2.url)
    try:
        page.wait_for_selector("#activity-toggle:not([hidden])")
        assert page.text_content("#activity-toggle .auto-all-text") == "Scheduled checks - 1 needs a look: Council portal request scan"
        assert page.get_attribute("#activity-toggle", "data-level") == "warn"
    finally:
        ctx.close()


LONG_REPLY = ("I'll check what Salts FSM exposes for assets and systems before I answer, so I am not guessing. "
              "First the endpoint list, then which fields tell us the install date and the service interval. ") * 6


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_many_quiet_jobs_never_hide_jarvis_reply_or_its_good_wrong_chips(browser, serve, width, height, scheme):
    """The owner's screenshot: 12 scheduled jobs, a long reply and Good / Wrong chips under it."""
    srv, j, _ = serve(client=FakeClient(default_text=LONG_REPLY))
    _quiet_jobs(j, 12, runs=11)
    for i in range(3):
        j.db.add_transcript("user" if i % 2 == 0 else "assistant", f"Earlier message number {i}")
    ctx, page = _page(browser, srv.url, width, height, scheme)
    try:
        page.wait_for_selector("#activity-toggle:not([hidden])")
        toggle = page.locator("#activity-toggle").bounding_box()
        assert toggle["height"] <= 60, toggle   # ONE slim row, however many jobs ran (12 lines of ~45px was the bug)
        text = page.text_content("#activity-toggle .auto-all-text")
        assert text.startswith("Scheduled checks - 12 jobs, 132 runs"), text
        assert page.locator("#conversation .msg").count() >= 3  # the history is in the log, and the checks are not
        page.fill("#input", "How many assets does the FSM have?")
        page.keyboard.press("Enter")
        page.wait_for_selector("#conversation .msg.assistant .fb")
        page.wait_for_timeout(300)
        fb = page.locator("#conversation .msg.assistant .fb").last
        log = page.locator("#conversation").bounding_box()
        for sel in ('button[data-rating="good"]', 'button[data-rating="wrong"]'):
            b = fb.locator(sel).bounding_box()
            assert b["y"] >= log["y"] - 1 and b["y"] + b["height"] <= log["y"] + log["height"] + 1, (sel, b, log)
        if width <= 760:
            assert fb.locator('button[data-rating="good"]').bounding_box()["height"] >= 44
        assert _log_state(page)["gap"] <= 2          # scrolled to the newest real message
        _shot(page, f"many-jobs-reply-{width}-{scheme}")
        # (a phone drops the orb back in when the message box loses focus, which moves everything: let that settle first)
        page.evaluate("document.activeElement?.blur()")
        page.wait_for_timeout(400)
        assert _log_state(page)["gap"] <= 2          # and the newest message is still in view after that
        # opening the checks: the panel is bounded and scrolls inside itself; the log keeps its size and its place
        before = _log_state(page)
        _open_checks(page)
        panel = page.locator("#activity").bounding_box()
        convo = page.locator("#transcript-wrap").bounding_box()
        assert panel["height"] <= convo["height"] * 0.4 + 2 or panel["height"] <= 110, (panel, convo)
        assert page.evaluate("(() => { const a = document.getElementById('activity'); return a.scrollHeight > a.clientHeight; })()")  # 12 lines scroll inside it
        assert page.locator("details.auto").count() == 12
        after = _log_state(page)
        assert abs(after["h"] - before["h"]) < 2 and after["gap"] <= 2, (before, after)  # opening it did not resize or move the conversation
        doc, body, vw = _no_hscroll(page)
        assert doc <= vw and body <= vw, (doc, body, vw)
        if width <= 760:
            assert page.locator("#activity-toggle").bounding_box()["height"] >= 44
            assert page.locator("details.auto summary").first.bounding_box()["height"] >= 44
        _shot(page, f"many-jobs-expanded-{width}-{scheme}")
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_a_status_refresh_does_not_move_the_conversation_or_the_panel_the_owner_is_reading(browser, serve):
    srv, j, _ = serve()
    _quiet_jobs(j, 12, runs=3)
    for i in range(40):
        j.db.add_transcript("user" if i % 2 == 0 else "assistant", f"Earlier message number {i} " + "word " * 20)
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.clock.install()   # so the 60 s status poll can be fast-forwarded instead of waited for
    page.goto(srv.url + "/", wait_until="domcontentloaded")
    page.clock.resume()
    try:
        page.wait_for_selector("#activity-toggle:not([hidden])", timeout=15000)
        page.wait_for_timeout(500)
        assert _log_state(page)["gap"] <= 2                      # loaded on the newest message
        _open_checks(page)
        page.locator('details.auto[data-job="job_5"] summary').click()   # a line the owner has open
        page.locator("#activity").evaluate("el => el.scrollTop = 60")
        panel_top = page.evaluate("document.getElementById('activity').scrollTop")
        assert panel_top > 0
        page.locator("#conversation").evaluate("el => el.scrollTop = 300")   # the owner scrolls up to read
        page.wait_for_timeout(100)
        # a new check runs; the 60 s poll comes round and the lines are rebuilt
        j.activity.record("job_0", "Council portal request scan", "no_change", "Nothing to report.")
        page.clock.fast_forward(61000)
        page.wait_for_function("""() => [...document.querySelectorAll('details.auto summary > span')]
          .some((s) => s.textContent.includes('Council portal request scan · 4 checks'))""", timeout=15000)
        assert page.evaluate("document.getElementById('conversation').scrollTop") == 300           # the log did not move
        assert page.is_visible("#activity")                                                         # the panel stayed open
        assert page.evaluate("document.querySelector('details.auto[data-job=job_5]').open") is True          # and so did the line
        assert abs(page.evaluate("document.getElementById('activity').scrollTop") - panel_top) < 2
        assert not page.errors, page.errors
    finally:
        context.close()


# --------------------------------------------------------------------------- speech-to-text in the top bar
STT_TEXT = "Voice input: browser fallback - OpenAI Whisper has no API key"


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_a_chosen_engine_with_no_key_is_a_text_label_in_the_top_bar(browser, serve, width, height, scheme):
    srv, j, _ = serve(stt_provider="whisper")
    ctx, page = _page(browser, srv.url, width, height, scheme)
    try:
        page.wait_for_function("!document.getElementById('stt-status').hidden")
        assert page.inner_text("#stt-status") == STT_TEXT
        assert page.evaluate("!!document.querySelector('.topbar #stt-status')")  # in the top bar, with the status
        box = page.locator("#stt-status").bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1, box
        doc, body, vw = _no_hscroll(page)
        assert doc <= vw and body <= vw, (doc, body, vw)
        _shot(page, f"stt-status-{width}-{scheme}")
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_starting_voice_input_falls_back_to_browser_recognition_and_still_says_so(browser, serve):
    srv, j, _ = serve(stt_provider="whisper")
    ctx, page = _page(browser, srv.url)
    try:
        page.wait_for_function("!document.getElementById('stt-status').hidden")
        page.evaluate("""() => { window.webkitSpeechRecognition = window.SpeechRecognition = class {
            constructor() { window.__srStarted = false; } start() { window.__srStarted = true; } stop() {} abort() {} }; }""")
        page.click("#btn-mic")
        page.wait_for_function("window.__srStarted === true")  # the fallback really is the browser's own recognition
        assert "browser" in page.inner_text("#stt-engine").lower()
        assert page.inner_text("#stt-status") == STT_TEXT  # and the top bar still says why
        page.click("#btn-mic")
    finally:
        ctx.close()


def test_with_a_working_choice_the_top_bar_label_is_not_shown(browser, serve):
    srv, _, _ = serve(stt_provider="whisper", openai_api_key="sk-test-key")
    ctx, page = _page(browser, srv.url)
    try:
        page.wait_for_timeout(400)
        assert page.is_hidden("#stt-status") and page.inner_text("#stt-status") == ""
    finally:
        ctx.close()
    srv2, _, _ = serve()  # nothing chosen at all (auto) is just the browser, not a fault
    ctx, page = _page(browser, srv2.url)
    try:
        page.wait_for_timeout(400)
        assert page.is_hidden("#stt-status")
    finally:
        ctx.close()


# --------------------------------------------------------------------------- Fleet: an honest "not connected"
def _ram_http(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fleet_text(page):
    page.click('.rail-item[data-pop="fleet"]')
    page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
    page.wait_for_function("document.getElementById('fleet-status').innerText.length > 0")
    page.wait_for_timeout(500)  # let the drawer finish sliding in, so a screenshot shows it settled
    return page.inner_text("#fleet-status")


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_fleet_with_no_ram_details_says_which_are_missing(browser, serve, width, height, scheme):
    srv, _, _ = serve(ram_client_id="Alex Clancy", ram_api_key="s")  # username and password still missing
    ctx, page = _page(browser, srv.url, width, height, scheme)
    try:
        text = _fleet_text(page)
        assert "Vehicle tracking is not connected" in text and "Still missing: API username, API password" in text
        assert not page.is_visible("#map")
        assert page.inner_text('.rail-item[data-pop="fleet"] .rail-count') == "off"
        assert _no_hscroll(page)[0] <= width
        _shot(page, f"fleet-not-connected-{width}-{scheme}")
    finally:
        ctx.close()


def test_fleet_when_ram_refuses_the_sign_in_says_so_with_the_reason_and_never_shows_a_map(browser, serve, monkeypatch):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: True))

    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad credentials"})

    srv, _, _ = serve(http=_ram_http(handler), ram_client_id="Alex Clancy", ram_api_key="sek", ram_username="MAC",
                      ram_password="pw")
    ctx, page = _page(browser, srv.url)
    try:
        text = _fleet_text(page)
        assert "Vehicle tracking is not connected" in text and "invalid_grant" in text and "Bad credentials" in text
        assert "sek" not in text and "pw" not in text.split("Bad credentials")[0]
        assert not page.is_visible("#map")
        assert page.inner_text('.rail-item[data-pop="fleet"] .rail-count') == "failing"
        _shot(page, "fleet-ram-failing")
        page.click("#fleet-status button[data-pop='connections']")
        page.wait_for_function("document.getElementById('drawer-title').innerText === 'Connections'")
    finally:
        ctx.close()


def test_fleet_when_ram_works_shows_the_vehicles(browser, serve, monkeypatch):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: True))
    vehicle = {"id": 101, "registration": "YD71 SFS", "vehicle_driver": {"name": "Dan Harper"},
               "vehicle_status": {"event_date": "2026-10-02T09:00:00Z", "location": {"latitude": 53.83, "longitude": -1.78},
                                  "last_event": {"event": "TRANSIT_START"}}}

    def handler(request):
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 28799})
        return httpx.Response(200, json=[vehicle])

    srv, _, _ = serve(http=_ram_http(handler), ram_client_id="c", ram_api_key="s", ram_username="u", ram_password="p")
    ctx, page = _page(browser, srv.url)
    try:
        text = _fleet_text(page)
        assert "not connected" not in text and "Live from RAM Tracking" in text and "1 vehicle" in text
    finally:
        ctx.close()


# --------------------------------------------------------------------------- the staff report key is nowhere
def test_the_staff_report_key_is_nowhere_in_the_rendered_page_but_the_button_copies_the_link(browser, serve):
    srv, _, settings = serve(staff_report_key=KEY, public_base_url="https://jarvis.example.test")
    ctx, page = _page(browser, srv.url, 1280, 800, "dark")
    bodies: list[tuple[str, str]] = []
    page.on("response", lambda r: bodies.append((r.url, r.text()) if "/api/" in r.url and "staff-report-address" not in r.url
                                                 and r.request.resource_type in ("fetch", "xhr") else ("", "")))
    try:
        ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=srv.url)

        def scan(where):
            html = page.content()
            visible = page.evaluate("document.body.innerText")
            values = page.evaluate("[...document.querySelectorAll('input,textarea,select')].map(e => e.value).join('\\n')")
            attrs = page.evaluate("""[...document.querySelectorAll('*')].flatMap(e => [...e.attributes].map(a => a.value)).join('\\n')""")
            storage = page.evaluate("JSON.stringify([localStorage, sessionStorage])")
            for label, text in (("html", html), ("text", visible), ("fields", values), ("attributes", attrs),
                                ("storage", storage)):
                assert KEY not in text and KEY[-4:] not in text, (where, label)
                assert "report?key=" not in text, (where, label)

        scan("console")
        page.click(".tb-btn[data-pop='settings']")
        page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
        assert page.is_visible("#btn-copy-report")
        scan("settings")
        _shot(page, "settings-copy-staff-link")
        # Connections > Security and staff: the key's own field says it is set, and nothing more
        page.keyboard.press("Escape")  # the drawer covers the top bar while it is open
        page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
        page.click(".tb-btn[data-pop='connections']")
        page.wait_for_selector("[data-open-section='security']")
        page.click("[data-open-section='security']")
        page.wait_for_selector("[data-field='staff_report_key'], #settings-sections .set-hint")
        scan("connections > security and staff")
        assert "••••" in page.inner_text("#settings-sections")
        _shot(page, "connections-security-no-key")
        page.keyboard.press("Escape")
        page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
        page.click(".tb-btn[data-pop='settings']")
        page.wait_for_selector("#btn-copy-report")
        page.click("#btn-copy-report")
        page.wait_for_function("document.body.innerText.includes('Link copied')")
        assert page.evaluate("navigator.clipboard.readText()") == f"https://jarvis.example.test/report?key={KEY}"
        scan("after copying")  # the link went to the clipboard, not onto the page
        for url, body in bodies:
            assert KEY not in body and KEY[-4:] not in body, url
    finally:
        ctx.close()
