"""The staff report key is never shown (console redesign phase 3, item 5).

The staff report address is /report?key=<staff_report_key>. Settings used to print it; Phase 1 replaced that with a
"Copy staff report link" button. These tests hold that nothing the console is given or shows, anywhere, contains the key
(or even its last four characters), that only the button's own request ever returns the link, and that the log redaction
covers the access-log line staff visits leave. The page as actually rendered is scanned in a real browser in
tests/test_console_browser_phase3.py.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi.testclient import TestClient

import jarvis.main  # noqa: F401 - installs the log redaction
from jarvis.core import Jarvis
from jarvis.main import create_app
from jarvis.settings_store import SettingsStore
from tests.fakes import FakeClient

KEY = "k9X-staff-secret-ZZ42"
WEB = Path(__file__).resolve().parent.parent / "jarvis" / "web"


def app_for(settings):
    settings.staff_report_key = KEY
    settings.public_base_url = "https://jarvis.example.test"
    return create_app(settings, Jarvis(settings, client=FakeClient()))


def assert_no_key(text: str, where: str) -> None:
    assert KEY not in text, f"the staff report key is in {where}"
    assert KEY[-4:] not in text, f"the tail of the staff report key is in {where}"
    assert "report?key=" not in text, f"a staff report link is in {where}"


def test_no_data_the_console_loads_contains_the_key_or_a_link_with_it(settings):
    app = app_for(settings)
    with TestClient(app) as c:
        for path in ("/api/settings", "/api/status", "/api/tracking", "/api/approvals", "/api/issues"):
            r = c.get(path)
            if r.status_code == 200:
                assert_no_key(r.text, path)
        data = c.get("/api/settings").json()
    assert data["context"]["staff_report_link_set"] is True  # the page may know a key exists, never what it is
    field = next(f for s in data["sections"] for f in s["fields"] if f["key"] == "staff_report_key")
    assert field["is_set"] is True and field["hint"] == "••••"  # not even "•••• ZZ42"
    assert "value" not in field


def test_the_display_password_gets_the_same_no_tail_treatment(settings):
    settings.jarvis_owner_password = "owner-password-Q7rT"
    store = SettingsStore(settings)
    view = store.view(type("Db", (), {"get_kv": staticmethod(lambda k: None)}), {"base_url": "https://x", "app_name": "x"})
    field = next(f for s in view["sections"] for f in s["fields"] if f["key"] == "jarvis_owner_password")
    assert field["hint"] == "••••" and "Q7rT" not in json.dumps(view)


def test_an_ordinary_secret_still_shows_its_last_four_so_you_can_tell_which_key_is_saved(settings):
    settings.fsm_api_key = "topsecret12345"
    store = SettingsStore(settings)
    view = store.view(type("Db", (), {"get_kv": staticmethod(lambda k: None)}), {"base_url": "https://x", "app_name": "x"})
    field = next(f for s in view["sections"] for f in s["fields"] if f["key"] == "fsm_api_key")
    assert "2345" in field["hint"] and "topsecret" not in json.dumps(field)


def test_the_link_comes_only_from_the_copy_button_endpoint_to_the_signed_in_owner_and_is_not_cached(settings):
    app = app_for(settings)
    with TestClient(app) as c:
        r = c.get("/api/staff-report-address")
        assert r.status_code == 200 and r.json()["link"] == f"https://jarvis.example.test/report?key={KEY}"
        assert r.headers["cache-control"] == "no-store"
        assert c.get("/api/staff-report-address", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 401  # not signed in
    settings.staff_report_key = ""
    with TestClient(app) as c:
        assert c.get("/api/staff-report-address").json() == {"link": ""}


def test_the_page_and_script_never_put_the_key_on_screen():
    index = (WEB / "index.html").read_text(encoding="utf-8")
    hud = (WEB / "hud.js").read_text(encoding="utf-8")
    assert 'id="btn-copy-report"' in index and "Copy staff report link" in index
    assert "report?key=" not in index and "report?key=" not in hud  # the address is built on the server, only
    # the only thing the script does with the link is put it on the clipboard
    start = hud.index('$("#btn-copy-report")')
    handler = hud[start:hud.index("let voicesLoaded", start)]
    assert "navigator.clipboard.writeText(link)" in handler and "textContent" not in handler and "innerHTML" not in handler
    assert "staff_report_link" not in hud  # no longer part of the settings payload the script reads
    assert "/api/staff-report-address" in hud


def test_the_access_log_line_for_a_staff_visit_does_not_carry_the_key(caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "10.0.0.7:5521", "GET",
                                                f"/report?key={KEY}", "1.1", 200)
    assert caplog.records
    for record in caplog.records:
        assert KEY not in record.getMessage() and KEY[-4:] not in record.getMessage()
        assert "/report?key=" in record.getMessage()  # the line is still useful: only the value is gone
