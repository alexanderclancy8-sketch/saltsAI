"""Real-browser checks that the features Jarvis added after the console redesign started (merged on top of it) work in the
redesigned console: rail + pop-up drawers, both themes, 1280 / 800 / 400px.

* Issues pop-up: every open issue has "Mark resolved" (with an optional note), a "Recently resolved" list appears with a
  "Reopen" button on each, and neither action ever touches the approvals queue.
* Fleet pop-up: with RAM connected each van shows RAM's address label ("home" - never the street address behind it - or
  where it is) and the list is not shown while RAM is not connected; outside working hours nothing shows unless the owner's
  setting allows it, and then the pop-up says the look-up is logged.
* Connections > RAM Tracking carries the owner-only "Show van locations outside working hours" choice, defaulting to Off.

Skipped without Playwright, like test_console_browser.py whose fixtures it shares. JARVIS_SHOTS=<folder> saves screenshots.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright.sync_api")

from jarvis.services.tracking import Tracker  # noqa: E402
from tests.test_console_browser import SIZES, THEMES, _no_hscroll, browser  # noqa: E402,F401
from tests.test_console_browser_phase3 import _fleet_text, _page, _ram_http, serve  # noqa: E402,F401

SHOTS = os.environ.get("JARVIS_SHOTS")


def _shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def _open_pop(page, pop):
    page.click(f'.rail-item[data-pop="{pop}"]')
    page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
    page.wait_for_timeout(500)  # let the drawer finish sliding in


# --------------------------------------------------------------------------- Issues: Mark resolved / Reopen
@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_issues_popup_can_mark_an_issue_resolved_and_reopen_it(browser, serve, width, height, scheme):
    srv, j, _ = serve()
    first = j.db.create_issue(reporter="Dan", title="Panel will not arm", description="x", source="staff", severity="high")
    j.db.create_issue(reporter="Kay", title="Door contact flaps", description="y", source="staff", severity="low")
    ctx, page = _page(browser, srv.url, width, height, scheme)
    try:
        _open_pop(page, "issues")
        assert page.locator("#issues [data-issue-act='resolve']").count() == 2
        assert page.is_hidden("#issues-resolved-sec")  # nothing resolved yet: no empty heading
        _shot(page, f"issues-open-{width}-{scheme}")
        page.once("dialog", lambda d: d.accept("fixed by Dan on site"))
        page.click(f"#issues [data-issue-act='resolve'][data-id='{first}']")
        page.wait_for_selector("#issues-resolved [data-issue-act='reopen']")
        assert page.locator("#issues [data-issue-act='resolve']").count() == 1
        assert page.is_visible("#issues-resolved-sec")
        resolved_text = page.inner_text("#issues-resolved")
        assert "Panel will not arm" in resolved_text and "fixed by Dan on site" in resolved_text
        assert j.db.get_issue(first)["status"] == "resolved"
        assert j.db.pending_actions() == []  # bookkeeping, not an approval
        assert _no_hscroll(page)[0] <= width
        _shot(page, f"issues-resolved-{width}-{scheme}")
        page.click(f"#issues-resolved [data-issue-act='reopen'][data-id='{first}']")
        page.wait_for_function("document.querySelectorAll('#issues [data-issue-act=resolve]').length === 2")
        assert page.is_hidden("#issues-resolved-sec")
        assert j.db.get_issue(first)["status"] == "open"
        assert j.db.pending_actions() == []
        assert page.errors == []
    finally:
        ctx.close()


def test_cancelling_the_note_prompt_leaves_the_issue_open(browser, serve):
    srv, j, _ = serve()
    iid = j.db.create_issue(reporter="Dan", title="Panel will not arm", description="x", source="staff", severity="high")
    ctx, page = _page(browser, srv.url)
    try:
        _open_pop(page, "issues")
        page.once("dialog", lambda d: d.dismiss())
        page.click(f"#issues [data-issue-act='resolve'][data-id='{iid}']")
        page.wait_for_timeout(400)
        assert j.db.get_issue(iid)["status"] == "new"  # untouched
        assert page.locator("#issues [data-issue-act='resolve']").count() == 1
    finally:
        ctx.close()


# --------------------------------------------------------------------------- Fleet: address label / at home
def _ram_vehicles(labels):
    vehicles = []
    for n, (driver, label) in enumerate(labels, start=1):
        loc = {"latitude": 53.83 + n / 100, "longitude": -1.78}
        if label:
            loc["formattedAddress"] = label
        vehicles.append({"id": 100 + n, "registration": f"YD71 SF{n}", "vehicle_driver": {"name": driver},
                         "vehicle_status": {"event_date": "2026-10-02T09:00:00Z", "location": loc, "last_event": "TRANSIT_STOP"}})

    def handler(request):
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 28799})
        return httpx.Response(200, json=vehicles)

    return _ram_http(handler)


RAM = dict(ram_client_id="c", ram_api_key="s", ram_username="u", ram_password="p")
STREET = "14 Acacia Avenue, Bradford BD1 1AA"


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_fleet_popup_lists_each_van_with_its_address_label_and_hides_a_home_street_address(
        browser, serve, monkeypatch, width, height, scheme):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: True))
    http = _ram_vehicles([("Dan Harper", f"Dan Harper home {STREET}"), ("Kay Lund", "M62 J24 Services"),
                          ("Sam Roe", None)])
    srv, _, _ = serve(http=http, **RAM)
    ctx, page = _page(browser, srv.url, width, height, scheme)
    try:
        assert "Live from RAM Tracking" in _fleet_text(page)
        page.wait_for_selector("#fleet-list li")
        rows = {li.split("\n")[0]: li for li in page.locator("#fleet-list li").all_inner_texts()}
        assert "home" in rows["Dan Harper"] and "Acacia" not in rows["Dan Harper"] and "BD1" not in rows["Dan Harper"]
        assert "M62 J24 Services" in rows["Kay Lund"]
        assert "no address label from RAM" in rows["Sam Roe"]
        assert STREET not in page.content() and "Acacia" not in page.inner_text("body")
        assert page.locator("#fleet-list li.ok").count() == 1  # only the van that is at home is marked
        assert _no_hscroll(page)[0] <= width
        _shot(page, f"fleet-labels-{width}-{scheme}")
    finally:
        ctx.close()


def test_fleet_popup_has_no_van_list_while_ram_is_not_connected(browser, serve):
    srv, _, _ = serve()  # no RAM details: sample data, whose positions are not vehicles
    ctx, page = _page(browser, srv.url)
    try:
        assert "not connected" in _fleet_text(page)
        page.wait_for_timeout(800)
        assert page.is_hidden("#fleet-list") and page.is_hidden("#map")
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_fleet_popup_out_of_hours_is_private_unless_the_owner_allowed_it(browser, serve, monkeypatch, width, height, scheme):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: False))
    monkeypatch.setattr(Tracker, "demo", property(lambda self: False))  # the privacy rule only applies to real data
    http = _ram_vehicles([("Dan Harper", "Dan Harper home")])
    srv, j, _ = serve(http=http, **RAM)  # setting left at its default: off
    ctx, page = _page(browser, srv.url, width, height, scheme)
    try:
        text = _fleet_text(page)
        assert "Outside working hours" in text and "Dan Harper" not in page.inner_text("#pop-fleet")
        assert j.db.location_lookups() == []
        _shot(page, f"fleet-ooh-off-{width}-{scheme}")
    finally:
        ctx.close()

    srv, j, _ = serve(http=http, van_locations_out_of_hours="always", **RAM)
    ctx, page = _page(browser, srv.url, width, height, scheme)
    try:
        text = _fleet_text(page)
        assert "the owner has allowed it" in text and "logged" in text
        page.wait_for_selector("#fleet-list li")
        assert "Dan Harper" in page.inner_text("#fleet-list")
        assert j.db.location_lookups()  # the look-up was recorded
        _shot(page, f"fleet-ooh-always-{width}-{scheme}")
    finally:
        ctx.close()


# --------------------------------------------------------------------------- Settings: van locations out of hours
@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_ram_connection_form_has_the_van_locations_out_of_hours_choice(browser, serve, width, height, scheme):
    srv, _, _ = serve()
    ctx, page = _page(browser, srv.url, width, height, scheme)
    try:
        page.click(".tb-btn[data-pop='connections']")
        page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
        page.click('[data-open-section="ram"]')
        page.wait_for_selector("#f-van_locations_out_of_hours")
        sel = page.locator("#f-van_locations_out_of_hours")
        assert sel.input_value() == "off"
        assert [o.strip() for o in sel.locator("option").all_inner_texts()] == ["Off", "On-call only", "Always"]
        assert "owner can change this" in page.inner_text("#settings-sections")
        assert _no_hscroll(page)[0] <= width
        sel.scroll_into_view_if_needed()
        page.wait_for_timeout(450)
        _shot(page, f"settings-ram-{width}-{scheme}")
        # picking a value is an unsaved change until the owner saves it: nothing is written by choosing
        sel.select_option("on_call")
        assert page.is_visible("#settings-savebar")
        assert page.errors == []
    finally:
        ctx.close()
