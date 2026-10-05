"""Real-browser check of the Comms drawer with a second inbox (service@): its items are shown in their own list labelled
"service@", the rail count and its label say how many of the unread are in it, a Graph problem is shown as a sentence, the
layout holds at phone width in both themes, and with no service inbox set the drawer is exactly as before.

Skipped when Playwright or a launchable Chrome is not installed (see test_console_browser.py)."""
from __future__ import annotations

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from jarvis.config import Settings  # noqa: E402
from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from tests.fakes import FakeClient  # noqa: E402
from tests.test_console_browser import THEMES, _no_hscroll, _open, _Server, browser  # noqa: E402,F401

ADDRESS = "service@example.co.uk"
LONG_SUBJECT = "New Work Order BC-WO-2026-04412 - Keighley Library - fire alarm panel fault zone 2 - " + "very long " * 12
OWN_UNREAD = 4  # the demo mailbox has four unread messages

SERVICE_OK = {"enabled": True, "label": "service@", "address": ADDRESS, "demo": False, "unread": [
    {"id": "S1", "subject": LONG_SUBJECT, "from_name": "Bradford Council Portal", "from_email": "noreply@bradford.gov.uk",
     "received": "2026-10-05T09:30:00Z", "importance": "high"},
    {"id": "S2", "subject": "<b>Work order</b> BC-WO-2026-04413", "from_name": "", "from_email": "noreply@bradford.gov.uk",
     "received": "2026-10-05T10:30:00Z", "importance": "normal"}]}
SERVICE_ERROR = {"enabled": True, "label": "service@", "address": ADDRESS, "demo": False, "unread": [],
                 "error": "Microsoft refused access (403 ErrorAccessDenied): the Azure app registration has no "
                          "permission on this mailbox."}


def _serve(tmp_path_factory, service):
    data = tmp_path_factory.mktemp("svc-console")
    settings = Settings(data_dir=data / "data", scheduler_enabled=False, anthropic_api_key="test", _env_file=None,
                        service_inbox=ADDRESS if service.get("enabled") else "")
    j = Jarvis(settings, client=FakeClient())

    async def unread(*a, **kw):
        return service

    j.service_inbox.unread = unread
    return _Server(create_app(settings, j))


@pytest.fixture(scope="module")
def svc_server(tmp_path_factory, restore_process_timezone):
    srv = _serve(tmp_path_factory, SERVICE_OK)
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def err_server(tmp_path_factory, restore_process_timezone):
    srv = _serve(tmp_path_factory, SERVICE_ERROR)
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def off_server(tmp_path_factory, restore_process_timezone):
    srv = _serve(tmp_path_factory, {"enabled": False})
    yield srv
    srv.stop()


def _open_comms(page):
    page.click('.rail-item[data-pop="comms"]')
    page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
    page.wait_for_timeout(450)


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", [(1280, 800), (400, 820)])
def test_comms_shows_the_service_inbox_labelled_and_fits(browser, svc_server, width, height, scheme):
    ctx, page = _open(browser, svc_server.url, width, height, scheme)
    try:
        label = page.get_attribute('.rail-item[data-pop="comms"]', "aria-label")
        assert f"{OWN_UNREAD + 2} unread messages (2 in service@)" in label, label
        assert page.inner_text("#rc-comms") == str(OWN_UNREAD + 2)
        _open_comms(page)
        assert page.is_visible("#svc-inbox-sec")
        assert page.inner_text("#svc-inbox-label") == "service@"
        assert page.inner_text("#svc-inbox-count") == "2 unread"
        assert page.inner_text("#inbox-title") == "Unread in your inbox"  # the two inboxes are told apart
        items = page.locator("#svc-inbox li")
        assert items.count() == 2
        first = items.nth(0).inner_text()
        assert "service@" in first and "Bradford Council Portal" in first and "BC-WO-2026-04412" in first
        assert page.locator("#svc-inbox li.hot").count() == 1  # high importance is marked like the owner's own
        # email text is data: an HTML-looking subject is shown as text, never as markup
        assert page.locator("#svc-inbox b").count() == 0 and "<b>Work order</b>" in items.nth(1).inner_text()
        assert page.locator("#inbox li").count() == OWN_UNREAD  # the owner's own list is unchanged and separate
        doc, body, vw = _no_hscroll(page)
        assert doc <= vw and body <= vw, (doc, body, vw)
        d = page.evaluate("(() => { const d = document.getElementById('drawer'); return [d.offsetWidth, d.scrollWidth, innerWidth]; })()")
        assert d[1] <= d[0] + 1 and d[0] <= d[2], d
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_comms_shows_a_service_inbox_problem_as_a_sentence(browser, err_server):
    ctx, page = _open(browser, err_server.url, 1280, 800)
    try:
        _open_comms(page)
        assert page.is_visible("#svc-inbox-sec")
        text = page.inner_text("#svc-inbox")
        assert "Microsoft refused access" in text and "no permission on this mailbox" in text
        assert page.inner_text("#svc-inbox-count") == ""
        assert page.inner_text("#rc-comms") == str(OWN_UNREAD)  # a failing second inbox adds nothing to the count
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_comms_is_exactly_as_before_without_a_service_inbox(browser, off_server):
    ctx, page = _open(browser, off_server.url, 1280, 800)
    try:
        _open_comms(page)
        assert not page.is_visible("#svc-inbox-sec")
        assert page.inner_text("#inbox-title") == "Unread messages"
        assert page.inner_text("#rc-comms") == str(OWN_UNREAD)
        assert "service@" not in page.get_attribute('.rail-item[data-pop="comms"]', "aria-label")
        assert not page.errors, page.errors
    finally:
        ctx.close()
