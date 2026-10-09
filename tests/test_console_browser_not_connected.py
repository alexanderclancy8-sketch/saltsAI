"""Real-browser check of the console with sample data OFF (production): every pop-up whose source isn't connected shows a tidy
"Not connected yet - connect X in Settings -> Connections" state - no sample rows, no "demo" badges, no figures - the top-bar
pill counts what isn't connected, and its pop-up lists each source once with what to connect. Nothing new covers the chat.

Skipped when Playwright or a launchable Chrome is not installed (see test_console_browser.py)."""
from __future__ import annotations

import re

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from jarvis.config import Settings  # noqa: E402
from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from tests.fakes import FakeClient  # noqa: E402
from tests.test_console_browser import THEMES, _no_hscroll, _Server, browser  # noqa: E402,F401

DEMO = re.compile(r"\bdemo\b|sample", re.I)


@pytest.fixture(scope="module")
def off_server(tmp_path_factory, restore_process_timezone):
    data = tmp_path_factory.mktemp("nc-console")
    settings = Settings(data_dir=data / "data", scheduler_enabled=False, anthropic_api_key="test", _env_file=None,
                        sample_data=False)
    srv = _Server(create_app(settings, Jarvis(settings, client=FakeClient())))
    yield srv
    srv.stop()


def _open(browser, url, width, height, scheme="dark"):
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#pills .pill", timeout=15000)   # the first /api/status has rendered
    page.wait_for_function("!!document.querySelector('#finance .nc-empty')", timeout=15000)
    page.errors = errors
    return context, page


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", [(1280, 800), (400, 820)])
def test_the_pop_ups_show_a_tidy_not_connected_state(browser, off_server, width, height, scheme):
    ctx, page = _open(browser, off_server.url, width, height, scheme)
    try:
        pill = page.inner_text("#pills")
        assert pill.startswith("Not connected: ") and "Demo" not in pill
        cases = {"finance": ("#finance", "connect Sage in Settings → Connections"),
                 "ops": ("#ops", "connect Salts FSM in Settings → Connections"),
                 "presence": ("#presence", "Settings → Connections"),
                 "comms": ("#inbox", "connect Microsoft 365 in Settings → Connections")}
        for name, (sel, words) in cases.items():
            page.click(f'.rail-item[data-pop="{name}"]')
            page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
            text = page.inner_text(sel)
            assert "Not connected yet" in text and words in text, (name, text)
            assert not DEMO.search(page.inner_text("#drawer")), (name, page.inner_text("#drawer"))
            assert page.locator(f"{sel} .kpi").count() == 0   # no figures at all
            page.keyboard.press("Escape")
            page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
        page.click('.rail-item[data-pop="fleet"]')
        page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
        assert "not connected" in page.inner_text("#fleet-status") and page.is_hidden("#map")
        page.keyboard.press("Escape")
        page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
        # the pill's pop-up: each unconnected source once, with what to connect
        page.click("#pills .pill")
        page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
        assert page.inner_text("#drawer-title") == "Not connected"
        rows = page.locator("#demo-list .demo-row b").all_inner_texts()
        assert "Salts FSM" in rows and "The accounts (Sage)" in rows and len(rows) == len(set(rows))
        assert not DEMO.search(page.inner_text("#pop-demo"))
        doc, body, inner = _no_hscroll(page)
        assert doc <= inner and body <= inner
        assert not page.errors, page.errors
    finally:
        ctx.close()
