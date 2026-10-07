"""Real-browser checks of the Fleet pop-up's van states and the owner-only Fleet diagnostics section.

Headless Chrome through Playwright (skipped when it is not installed, like test_console_browser.py whose fixtures it shares), at
1280 / 800 / 400px in both themes. RAM is a mocked HTTP service whose vans the test moves; RamTracking's wall clock is pinned, so
nothing depends on the time of day or on today's date. Nothing here has been run against RAM's real API.

* the Fleet list says what each van is doing in words - "Moving", "Moving (about 20 mph)" (an estimate from two polls),
  "Stopped, engine on", "Parked", "No recent position (last seen N min ago)" - and never an exact speed RAM did not give;
* Fleet diagnostics (owner only) lists each van's raw last_event, its age, engine RPM, our classification and the reason, with no
  coordinates, names or homes anywhere in it; no horizontal scroll, and a tap target of at least 44px on a phone;
* a team session has the Fleet list with the same words but no diagnostics section at all.

JARVIS_SHOTS=<folder> saves a screenshot of each state.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright.sync_api")

from jarvis.core import Jarvis  # noqa: E402
from jarvis.integrations.ram_motion import MotionTracker  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from jarvis.services.tracking import Tracker  # noqa: E402
from tests.fakes import FakeClient  # noqa: E402
from tests.live_server import LiveServer  # noqa: E402
from tests.test_console_browser import SIZES, THEMES, _no_hscroll, _settings, browser  # noqa: E402,F401
from tests.test_team_console_browser import TEAM_CODE, _sign_in_team  # noqa: E402

SHOTS = os.environ.get("JARVIS_SHOTS")
OWNER_PW = "a-local-test-password"
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 2, 9, 0, 0, tzinfo=UTC)
START = (53.8300, -1.7800)
M_PER_DEG_LAT = 111194.93


def _shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def _van(n, driver, event, minutes_ago, rpm=None, metres_north=0.0):
    point = (START[0] + n * 0.03 + metres_north / M_PER_DEG_LAT, START[1])
    when = (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {"id": 100 + n, "registration": f"YD7{n} SFS", "vehicle_driver": {"name": driver},
           "vehicle_status": {"event_date": when, "last_event": event,
                              "location": {"latitude": point[0], "longitude": point[1]}}}
    if rpm is not None:
        out["engineRpm"] = rpm
    return out


def _vans(a_north=0.0):
    return [_van(1, "Dan Harper", "HARSH_BRAKING", 2, metres_north=a_north),  # set off earlier; braked 2 minutes ago
            _van(2, "Priya Shah", "ZONE_OUT", 3, rpm=900),                     # neutral event, engine running
            _van(3, "Kay Lund", "IGNITION_OFF", 4),
            _van(4, "Ian Frost", "TRANSIT_START", 30),                         # a "moving" event too old to trust
            _van(5, "Mo Khan", "IDLE_START", 5, rpm=750)]


class World:
    def __init__(self):
        self.vans = _vans()
        self.wall = NOW
        self.mono = 1000.0
        self.vehicle_requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 28799})
        self.vehicle_requests += 1
        return httpx.Response(200, json=self.vans)


@pytest.fixture(scope="module")
def stack(tmp_path_factory, restore_process_timezone):
    mp = pytest.MonkeyPatch()
    mp.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: True))
    world = World()
    settings = _settings(tmp_path_factory, jarvis_owner_password=OWNER_PW, ram_client_id="c", ram_api_key="s",
                         ram_username="u", ram_password="p")
    j = Jarvis(settings, client=FakeClient(), http=httpx.AsyncClient(transport=httpx.MockTransport(world.handler)))
    j.team_access.set_code(TEAM_CODE)
    j.ram._wall = lambda: world.wall
    j.ram._now = lambda: world.mono
    srv = LiveServer(create_app(settings, j))
    yield srv, j, world
    srv.stop()
    mp.undo()


def _reset(j, world):
    world.vans, world.wall = _vans(), NOW
    world.mono += 1000                       # the 60 s vehicle cache from an earlier test has long expired
    j.ram.motion = MotionTracker()           # and no position history is left over


def _owner_page(browser, url, width, height, scheme):
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(url + "/", wait_until="domcontentloaded")
    page.fill("#pw", OWNER_PW)
    page.click(".btn-signin")
    page.wait_for_selector("#needs-list .need, #needs-list .needs-clear:not(:has-text('Loading'))", timeout=15000)
    return ctx, page


def _open_fleet(page, vans=5):
    page.click('.rail-item[data-pop="fleet"]')
    page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
    page.wait_for_function(f"document.querySelectorAll('#fleet-list li').length === {vans}")
    page.wait_for_timeout(450)


def _rows(page):
    return {li.split("\n")[0]: " ".join(li.split()) for li in page.locator("#fleet-list li").all_inner_texts()}


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_fleet_says_what_each_van_is_doing_in_words(browser, stack, width, height, scheme):
    srv, j, world = stack
    _reset(j, world)
    ctx, page = _owner_page(browser, srv.url, width, height, scheme)
    try:
        _open_fleet(page)
        rows = _rows(page)
        assert rows["Dan Harper"].endswith("· Moving")                      # no speed known from one reading: none claimed
        assert rows["Priya Shah"].endswith("· Stopped, engine on")
        assert rows["Kay Lund"].endswith("· Parked")
        assert rows["Ian Frost"].endswith("· No recent position (last seen 30 min ago)")
        assert rows["Mo Khan"].endswith("· Stopped, engine on")
        assert "mph" not in " ".join(rows.values())                         # RAM sends no speed: no speed is made up
        assert _no_hscroll(page)[0] <= width
        _shot(page, f"fleet-states-{width}-{scheme}")

        # A minute on, the first van is 610 m further along: an estimated speed appears, worded as an estimate.
        world.wall += dt.timedelta(seconds=61)
        world.mono += 61
        world.vans = _vans(a_north=610)
        page.evaluate("window.dispatchEvent(new Event('resize'))")
        page.wait_for_function("[...document.querySelectorAll('#fleet-list li')].some(li => li.innerText.includes('about'))")
        assert _rows(page)["Dan Harper"].endswith("· Moving (about 20 mph)")
        assert page.errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_the_owner_sees_how_each_van_was_classified_and_why_with_no_positions(browser, stack, width, height, scheme):
    srv, j, world = stack
    _reset(j, world)
    ctx, page = _owner_page(browser, srv.url, width, height, scheme)
    try:
        _open_fleet(page)
        assert page.locator("#fleet-diag").count() == 1 and not page.locator("#fleet-diag").evaluate("d => d.open")
        summary = page.locator("#fleet-diag > summary")
        summary.scroll_into_view_if_needed()
        if width <= 480:
            assert summary.bounding_box()["height"] >= 44
        summary.click()
        page.wait_for_function("document.querySelectorAll('#fleet-diag-list li').length === 5")
        page.wait_for_timeout(300)
        status = page.inner_text("#fleet-diag-status")
        assert "5 vans" in status and "RAM's portal" in status
        by = {li.split("\n")[0].split(" ")[0] + " " + li.split("\n")[0].split(" ")[1]: " ".join(li.split())
              for li in page.locator("#fleet-diag-list li").all_inner_texts()}
        assert "Moving" in by["YD71 SFS"] and "last_event HARSH_BRAKING · 2 min ago · engine RPM not reported" in by["YD71 SFS"]
        assert "engine RPM 900" in by["YD72 SFS"] and "Stopped, engine on" in by["YD72 SFS"]
        assert "Parked" in by["YD73 SFS"] and "last_event IGNITION_OFF" in by["YD73 SFS"]
        assert "No recent position (last seen 30 min ago)" in by["YD74 SFS"] and "older than 15 min" in by["YD74 SFS"]
        assert "idling" in by["YD75 SFS"] and "engine RPM 750" in by["YD75 SFS"]
        section = page.inner_text("#fleet-diag") + page.inner_html("#fleet-diag")
        for leaked in ("53.8", "53.9", "-1.78", "latitude", "lng", "Dan Harper", "Priya Shah", "Kay Lund", "home"):
            assert leaked not in section, leaked
        assert _no_hscroll(page)[0] <= width
        _shot(page, f"fleet-diagnostics-{width}-{scheme}")
        assert page.errors == []
    finally:
        ctx.close()


def test_a_team_session_has_the_fleet_words_but_no_diagnostics(browser, stack):
    srv, j, world = stack
    _reset(j, world)
    ctx = browser.new_context(viewport={"width": 400, "height": 820})
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    try:
        _sign_in_team(page, srv.url)
        assert page.locator("#fleet-diag").count() == 0 and "Fleet diagnostics" not in page.content()
        _open_fleet(page)
        assert _rows(page)["Kay Lund"].endswith("· Parked")
        assert page.request.get(srv.url + "/api/fleet/diagnostics").status == 403
        assert page.errors == []
    finally:
        ctx.close()
