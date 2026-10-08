"""Real-browser check of the numbered web sources under a reply (brain/web_research.py, hud.js webSourcesHtml).

Headless Chrome through Playwright (skipped when it is not installed, sharing test_console_browser.py's fixtures), at 1280 and 400 px
wide, dark and light: the list sits in the message's own flow under the reply text (never over it - earlier panels covered replies),
each source is a numbered link that opens in a new tab without handing the page a window.opener, a long title wraps instead of
scrolling the page sideways, and the coverage line still names the web. Set JARVIS_SHOTS=<folder> to save screenshots.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from tests.fakes import message  # noqa: E402
from tests.live_server import LiveServer, SlowClient  # noqa: E402
from tests.test_console_browser import _no_hscroll, _settings, browser  # noqa: E402,F401
from tests.test_web_research import BSI, FIA, cited, fetch_result, search_result, search_use  # noqa: E402

SHOTS = os.environ.get("JARVIS_SHOTS")
CASES = [(1280, 800, "dark"), (1280, 800, "light"), (400, 820, "dark"), (400, 820, "light")]
LONG = ("https://www.example-standards-body.org.uk/" + "very-long-path-segment/" * 8,
        "A very long page title that goes on and on about cause and effect, audibility and zoning " * 2)


@pytest.fixture
def server(tmp_path_factory):
    made = []

    def build(script):
        settings = _settings(tmp_path_factory)
        j = Jarvis(settings, client=SlowClient(script, delay=0.002))
        srv = LiveServer(create_app(settings, j))
        made.append(srv)
        return srv, j

    yield build
    for srv in made:
        srv.stop()


def research_script():
    return [message([search_use(1), search_result(BSI, FIA), search_use(2, "web_fetch"), fetch_result(LONG[0], LONG[1]),
                     cited("The 2025 edition tightens the rules on cause and effect ", BSI),
                     cited("and the FIA's guidance agrees.", FIA)])]


@pytest.mark.parametrize("width,height,scheme", CASES)
def test_numbered_web_sources_under_a_reply(browser, server, width, height, scheme):
    srv, _ = server(research_script())
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(srv.url + "/", wait_until="domcontentloaded")
        page.wait_for_selector("#needs-list .need", timeout=15000)
        page.fill("#input", "What does BS 5839-1:2025 change for us?")
        page.click("#btn-send")
        page.wait_for_selector(".msg.assistant .web-src li", timeout=15000)
        items = page.eval_on_selector_all(".msg.assistant .web-src li", """els => els.map(li => {
            const a = li.querySelector('a');
            return {value: li.value, text: a.textContent, href: a.href, target: a.target, rel: a.rel};
        })""")
        assert [i["value"] for i in items] == [1, 2, 3]
        assert [i["text"] for i in items[:2]] == [BSI[1], FIA[1]] and items[2]["text"].startswith("A very long page title")
        assert [i["href"] for i in items] == [BSI[0], FIA[0], LONG[0]]
        assert all(i["target"] == "_blank" and "noopener" in i["rel"] and "noreferrer" in i["rel"] for i in items)
        # in the message's flow, under the reply's words - never on top of them - and above the source line and coverage line
        boxes = page.evaluate("""() => {
            const m = [...document.querySelectorAll('.msg.assistant')].pop();
            const r = (s) => { const b = m.querySelector(s).getBoundingClientRect(); return {top: b.top, bottom: b.bottom}; };
            return {md: r('.md'), list: r('.web-src'), src: r('.src'), cov: r('.cov'),
                    pos: getComputedStyle(m.querySelector('.web-src')).position};
        }""")
        assert boxes["pos"] == "static"
        assert boxes["list"]["top"] >= boxes["md"]["bottom"] - 1
        assert boxes["src"]["top"] >= boxes["list"]["bottom"] - 1 and boxes["cov"]["top"] >= boxes["src"]["bottom"] - 1
        assert "The web (3 sources)" in page.inner_text(".msg.assistant .cov summary")
        doc, body, vw = _no_hscroll(page)
        assert doc <= vw and body <= vw
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(SHOTS) / f"web-sources-{width}-{scheme}.png"))
        assert errors == []
    finally:
        ctx.close()
