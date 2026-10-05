"""Real-browser checks of Settings > Engineer homes and the Fleet pop-up's home / away / no home set wording.

Headless Chrome through Playwright (skipped when it is not installed, like test_console_browser.py whose fixtures it shares), at
1280 / 800 / 400px in both themes. The postcode service is faked inside the app's own HTTP client, so the whole stack runs: the
owner types a postcode in the page, the server looks it up (against the fake), keeps a rounded point and drops the postcode.

* the section is there for the owner with the plain privacy note, lists the engineers, and saves / replaces / removes a home;
  after saving the page shows only "Home set (date)", the postcode box is empty again and the postcode (and the point) is in
  nothing the page was sent, nothing it stored and nothing it shows; a bad postcode gets a clear message and saves nothing;
* the distance setting works and stays within 50-300 m; "Remove all" asks first;
* no horizontal scroll, and every control is at least 44px tall on a phone;
* the Fleet list says "home" for a van at its engineer's home point (never the street even when RAM sends one), "away" for one
  that is elsewhere and "no home set" for an engineer with no point and no RAM label.

JARVIS_SHOTS=<folder> saves a screenshot of each state.
"""
from __future__ import annotations

import json
import os
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
from tests.test_console_browser_phase3 import _fleet_text  # noqa: E402

SHOTS = os.environ.get("JARVIS_SHOTS")
OWNER_PW = "a-local-test-password"
POINTS = {"BD161AA": (53.912345, -1.654321), "LS14AP": (53.799712, -1.549187)}   # what the fake postcode service knows
HOME = (53.9123, -1.6543)
SECRET_BITS = ("BD16", "BD161AA", "1AA", "53.9123", "1.6543", "53.912345", "1.654321")
STREET = "14 Acacia Avenue, Bradford"


def _shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def _http(vehicles=()):
    """One fake internet: postcodes.io (the bulk endpoint) and RAM Tracking (sign-in + vehicles)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.postcodes.io":
            pc = json.loads(request.content)["postcodes"][0]
            point = POINTS.get(pc.replace(" ", ""))
            body = None if point is None else {"postcode": pc, "latitude": point[0], "longitude": point[1]}
            return httpx.Response(200, json={"status": 200, "result": [{"query": pc, "result": body}]})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 28799})
        return httpx.Response(200, json=list(vehicles))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ram_vehicle(n, driver, at, label=None):
    loc = {"latitude": at[0], "longitude": at[1]}
    if label:
        loc["formattedAddress"] = label
    return {"id": 100 + n, "registration": f"YD71 SF{n}", "vehicle_driver": {"name": driver},
            "vehicle_status": {"event_date": "2026-10-02T09:00:00Z", "location": loc, "last_event": "TRANSIT_STOP"}}


@pytest.fixture
def serve(tmp_path_factory, restore_process_timezone):
    made = []

    def build(vehicles=(), **settings_kw):
        settings = _settings(tmp_path_factory, jarvis_owner_password=OWNER_PW, **settings_kw)
        j = Jarvis(settings, client=FakeClient(), http=_http(vehicles))
        srv = LiveServer(create_app(settings, j))
        made.append(srv)
        return srv, j

    yield build
    for srv in made:
        srv.stop()


@pytest.fixture(scope="module")
def shared(tmp_path_factory, restore_process_timezone):
    settings = _settings(tmp_path_factory, jarvis_owner_password=OWNER_PW)
    j = Jarvis(settings, client=FakeClient(), http=_http())
    srv = LiveServer(create_app(settings, j))
    yield srv, j
    srv.stop()


def _owner_page(browser, url, width, height, scheme):
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = ctx.new_page()
    page.errors, page.api = [], []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.on("response", lambda r: page.api.append((r.request.method, r.url, r.status, _text(r))) if "/api/" in r.url else None)
    page.goto(url + "/", wait_until="domcontentloaded")
    page.fill("#pw", OWNER_PW)
    page.click(".btn-signin")
    page.wait_for_selector("#needs-list .need, #needs-list .needs-clear:not(:has-text('Loading'))", timeout=15000)
    return ctx, page


def _text(response):
    try:
        return response.text()
    except Exception:  # noqa: BLE001 - streamed / already closed
        return ""


def _open_homes(page):
    page.click('.tb-btn[data-pop="settings"]')
    page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
    page.wait_for_function("document.querySelectorAll('#homes-list li').length > 0")
    page.locator("#homes-sec").scroll_into_view_if_needed()
    page.wait_for_timeout(450)


def _row(page, name):
    return page.locator(f'#homes-list li[data-eng="{name}"]')


def _save_home(page, name, postcode):
    row = _row(page, name)
    row.locator("[data-home-pc]").fill(postcode)
    row.locator('[data-home-act="save"]').click()


def _toast_text(page):
    page.wait_for_selector(".toast")
    return page.inner_text("#toasts")


# --------------------------------------------------------------------------- Settings > Engineer homes
@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_the_owner_sets_replaces_and_removes_a_home_and_the_postcode_never_comes_back(browser, shared, width, height, scheme):
    srv, j = shared
    j.homes.clear_all()
    ctx, page = _owner_page(browser, srv.url, width, height, scheme)
    try:
        _open_homes(page)
        note = " ".join(page.inner_text("#homes-sec").split())
        assert "Only a rounded map point is stored, not the postcode." in note
        assert "It is used only to show whether a van is at home. Tell the engineer first." in note
        assert page.locator("#homes-list li").count() == 6                       # the demo FSM's engineers, never office staff
        assert "Hannah Cole" not in page.inner_text("#homes-list")
        assert "Not set" in _row(page, "Dan Harper").inner_text() and _row(page, "Dan Harper").locator("[data-home-act=remove]").count() == 0
        assert page.is_hidden("#btn-homes-clear-all")
        _shot(page, f"homes-empty-{width}-{scheme}")

        _save_home(page, "Dan Harper", "bd16  1aa")
        page.wait_for_function("document.querySelector('#homes-list li[data-eng=\"Dan Harper\"] .home-state').textContent.startsWith('Home set')")
        row = _row(page, "Dan Harper")
        state = row.locator(".home-state").inner_text()
        assert state.startswith("Home set (") and state.endswith(")") and "Not set" not in state
        assert row.locator("[data-home-pc]").input_value() == ""                 # the box is empty again
        assert row.locator("[data-home-act=remove]").is_visible() and page.is_visible("#btn-homes-clear-all")
        assert "Home saved" in _toast_text(page)
        assert [r["engineer"] for r in j.db.engineer_homes_set()] == ["Dan Harper"]
        assert j.db.query("SELECT lat, lng FROM engineer_homes") == [{"lat": HOME[0], "lng": HOME[1]}]
        _shot(page, f"homes-one-set-{width}-{scheme}")

        # nothing on the page, in what it stored, or in anything the server sent it, holds the postcode or the point
        shown = page.content() + page.inner_text("body")
        stored = page.evaluate("JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])")
        received = "\n".join(body for method, url, status, body in page.api)
        for bit in SECRET_BITS:
            assert bit.lower() not in (shown + stored + received).lower().replace("1aa", "") or bit.lower() == "1aa", bit
        assert not [m for m in page.api if m[2] >= 400 and "/api/engineer-homes" in m[1]]

        # replace it with another postcode
        _save_home(page, "Dan Harper", "LS1 4AP")
        page.wait_for_function("document.querySelector('#toasts').textContent.includes('Home saved')")
        assert j.db.query("SELECT lat, lng FROM engineer_homes") == [{"lat": 53.7997, "lng": -1.5492}]

        # remove it (a confirm first)
        page.once("dialog", lambda d: d.accept())
        _row(page, "Dan Harper").locator("[data-home-act=remove]").click()
        page.wait_for_function("document.querySelector('#homes-list li[data-eng=\"Dan Harper\"] .home-state').textContent === 'Not set'")
        assert j.db.engineer_homes_set() == []
        assert _no_hscroll(page)[0] <= width
        assert not page.errors
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_a_bad_or_unknown_postcode_gets_a_clear_message_and_saves_nothing(browser, shared, width, height, scheme):
    srv, j = shared
    j.homes.clear_all()
    ctx, page = _owner_page(browser, srv.url, width, height, scheme)
    try:
        _open_homes(page)
        _save_home(page, "Priya Shah", "not a postcode")
        assert "doesn't look like a UK postcode" in _toast_text(page)
        page.evaluate("document.getElementById('toasts').innerHTML = ''")
        _save_home(page, "Priya Shah", "ZZ9 9ZZ")                                 # well-formed, but the service does not know it
        page.wait_for_selector(".toast")
        assert "doesn't know that postcode" in page.inner_text("#toasts")
        assert "Not set" in _row(page, "Priya Shah").inner_text() and j.db.engineer_homes_set() == []
        page.evaluate("document.getElementById('toasts').innerHTML = ''")
        _row(page, "Priya Shah").locator('[data-home-act="save"]').click()       # nothing typed
        assert "Type a postcode first" in _toast_text(page)
        _shot(page, f"homes-error-{width}-{scheme}")
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
def test_the_distance_can_be_changed_within_50_to_300_and_remove_all_asks_first(browser, shared, scheme):
    srv, j = shared
    j.homes.clear_all()
    j.homes.set_radius(100)
    ctx, page = _owner_page(browser, srv.url, 1280, 800, scheme)
    try:
        _open_homes(page)
        assert page.input_value("#homes-radius") == "100"
        page.fill("#homes-radius", "180")
        page.click("#btn-homes-radius")
        page.wait_for_function("document.querySelector('#toasts').textContent.includes('Distance saved')")
        assert j.homes.radius_m == 180
        page.evaluate("document.getElementById('toasts').innerHTML = ''")
        page.fill("#homes-radius", "20")
        page.click("#btn-homes-radius")
        assert "from 50 to 300" in _toast_text(page) and j.homes.radius_m == 180   # refused: unchanged
        _save_home(page, "Dan Harper", "BD16 1AA")
        page.wait_for_function("document.querySelectorAll('#homes-list .home-state.ok').length === 1")
        _save_home(page, "Priya Shah", "LS1 4AP")
        page.wait_for_function("document.querySelectorAll('#homes-list .home-state.ok').length === 2")
        page.once("dialog", lambda d: d.dismiss())                               # "Cancel": nothing is removed
        page.click("#btn-homes-clear-all")
        page.wait_for_timeout(300)
        assert len(j.db.engineer_homes_set()) == 2
        page.once("dialog", lambda d: d.accept())
        page.click("#btn-homes-clear-all")
        page.wait_for_function("document.querySelectorAll('#homes-list .home-state.ok').length === 0")
        assert j.db.engineer_homes_set() == [] and page.is_hidden("#btn-homes-clear-all")
        j.homes.set_radius(100)
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
def test_every_control_in_the_section_is_at_least_44px_tall_on_a_phone(browser, shared, scheme):
    srv, j = shared
    j.homes.clear_all()
    j.db.set_engineer_home("Dan Harper", *HOME, "the owner")
    ctx, page = _owner_page(browser, srv.url, 400, 820, scheme)
    try:
        _open_homes(page)
        assert page.is_visible("#btn-homes-clear-all")
        small = page.evaluate("""() => [...document.querySelectorAll('#homes-sec button, #homes-sec input')]
            .filter(e => e.offsetParent !== null)
            .map(e => [e.id || e.getAttribute('data-home-act') || e.getAttribute('aria-label'), e.getBoundingClientRect().height])
            .filter(([n, h]) => h < 43.5)""")
        assert small == [], small
        assert _no_hscroll(page)[0] <= 400
        page.locator("#homes-list li").first.scroll_into_view_if_needed()
        _shot(page, f"homes-phone-{scheme}")
    finally:
        ctx.close()


# --------------------------------------------------------------------------- Fleet: home / away / no home set
@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_fleet_says_home_away_or_no_home_set_and_never_a_street_or_a_point(browser, serve, monkeypatch, width, height, scheme):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: True))
    vans = [_ram_vehicle(1, "Dan Harper", HOME, STREET),                  # at his home point; RAM sends a street anyway
            _ram_vehicle(2, "Priya Shah", (53.8300, -1.7800)),            # has a home set, but the van is elsewhere
            _ram_vehicle(3, "Kay Lund", (53.8400, -1.7800))]              # no home set and no label
    srv, j = serve(vans, ram_client_id="c", ram_api_key="s", ram_username="u", ram_password="p")
    ctx, page = _owner_page(browser, srv.url, width, height, scheme)
    try:
        _open_homes(page)
        assert page.locator("#homes-list li").count() == 7                        # six demo engineers + Kay Lund, RAM's driver
        _save_home(page, "Dan Harper", "BD16 1AA")
        page.wait_for_function("document.querySelector('#homes-list li[data-eng=\"Dan Harper\"] .home-state.ok')")
        _save_home(page, "Priya Shah", "LS1 4AP")
        page.wait_for_function("document.querySelector('#homes-list li[data-eng=\"Priya Shah\"] .home-state.ok')")
        page.evaluate("document.getElementById('toasts').innerHTML = ''")      # so a screenshot shows every row
        page.click("#drawer-close")
        page.click('.rail-item[data-pop="fleet"]')
        page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
        page.wait_for_function("document.querySelectorAll('#fleet-list li').length === 3")
        assert "Live from RAM Tracking" in page.inner_text("#fleet-status")
        rows = {li.split("\n")[0]: " ".join(li.split()) for li in page.locator("#fleet-list li").all_inner_texts()}
        assert rows["Dan Harper"] == "Dan Harper home"
        assert rows["Priya Shah"].startswith("Priya Shah away") and "home" not in rows["Priya Shah"].replace("Priya Shah away", "")
        assert rows["Kay Lund"].startswith("Kay Lund no home set") and "no address label from RAM" in rows["Kay Lund"]
        body = page.inner_text("body") + page.content()
        assert STREET not in body and "Acacia" not in body
        for bit in ("53.9123", "1.6543", "53.7997", "BD16", "BD161AA"):
            assert bit not in body, bit
        assert page.locator("#fleet-list li.ok").count() == 1                     # only the van at home is marked
        assert _no_hscroll(page)[0] <= width
        _shot(page, f"fleet-home-{width}-{scheme}")
    finally:
        ctx.close()
