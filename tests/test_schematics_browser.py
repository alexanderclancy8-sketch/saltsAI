"""Real-browser checks of system schematics in the console (headless Chrome through Playwright; skipped when Playwright or a Chrome it
can launch is missing, like tests/test_charts_browser.py). The model is mocked: a scripted `draw_schematic` call goes through the real
app, the reply event carries the drawing's reference, and web/schematics.js fetches and draws it under the reply.

Covered: each kind at desktop (1280) and phone (390) width in both themes - the drawing sits under the reply text (never over it), the
page never scrolls sideways, the phone gets the narrow layout, the colours follow the theme tokens, the disclaimer and the download
links are there and a download really is a PDF; a label that tries to be HTML is shown as text. (That an engineer's own session's
reply carries the drawing is in tests/test_schematics.py.) Every fixture is synthetic. Set JARVIS_SHOTS=<folder> to keep a screenshot of each render."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from tests.fakes import FakeClient, message, text_block, tool_block  # noqa: E402
from tests.live_server import LiveServer  # noqa: E402
from tests.test_console_browser import _settings, browser  # noqa: E402,F401
from tests.test_schematics import ce_spec, fire_spec, net_spec  # noqa: E402

SHOTS = os.environ.get("JARVIS_SHOTS")
REPLY = "Drawn - a draft for a competent person to check."
SPECS = {"fire_loop": fire_spec(loops=2, per=18), "cause_effect": ce_spec(rows=10, cols=8), "network": net_spec()}


@pytest.fixture(scope="module")
def stack(tmp_path_factory, restore_process_timezone):
    settings = _settings(tmp_path_factory)
    client = FakeClient()
    j = Jarvis(settings, client=client)
    srv = LiveServer(create_app(settings, j))
    yield srv, j, client
    srv.stop()


class Session:
    def __init__(self, browser, srv, client, width, scheme="dark", height=900):
        self.client, self.done = client, 0
        self.ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, accept_downloads=True)
        self.page = self.ctx.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" and "favicon" not in m.text else None)
        self.page.goto(srv.url + "/", wait_until="domcontentloaded")
        self.page.wait_for_selector("#input", timeout=15000)

    def draw(self, kind, spec):
        self.client.beta.messages.script += [message([tool_block("draw_schematic", {"kind": kind, "spec": spec})], "tool_use"),
                                             message([text_block(REPLY)])]
        self.done += 1
        self.page.fill("#input", f"draw the {kind}")
        self.page.click("#btn-send")
        self.page.wait_for_function("(n) => document.querySelectorAll('figure.sch').length >= n", arg=self.done, timeout=30000)
        self.page.wait_for_function("(n) => document.querySelectorAll('figure.sch svg.sch-svg').length >= n", arg=self.done, timeout=15000)
        self.page.wait_for_timeout(150)

    def close(self):
        self.ctx.close()


def snap(page, tmp_path, name):
    folder = Path(SHOTS) if SHOTS else tmp_path
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / f"{name}.png"), full_page=False)


LAYOUT_JS = """() => {
  const figs = document.querySelectorAll('figure.sch'), fig = figs[figs.length - 1];
  const msg = fig.closest('.msg'), md = msg.querySelector('.md');
  const svg = fig.querySelector('svg.sch-svg'), view = fig.querySelector('.sch-view');
  const ink = svg.querySelector('.s-ink'), probe = document.createElement('span');
  probe.style.color = getComputedStyle(document.documentElement).getPropertyValue('--text'); document.body.appendChild(probe);
  const want = getComputedStyle(probe).color; probe.remove();
  const r = (e) => e.getBoundingClientRect();
  return { docW: document.documentElement.scrollWidth, vw: innerWidth, mode: fig.dataset.mode, figRight: r(fig).right, figTop: r(fig).top,
           mdBottom: r(md).bottom, viewW: r(view).width, svgW: r(svg).width, role: svg.getAttribute('role'), label: svg.getAttribute('aria-label'),
           inkStroke: ink ? getComputedStyle(ink).stroke : null, want, texts: svg.querySelectorAll('text').length,
           note: fig.querySelector('.sch-note').textContent, links: [...fig.querySelectorAll('a.sch-dl')].map((a) => a.getAttribute('href')),
           inConversation: !!fig.closest('#conversation'), imgs: document.querySelectorAll('#conversation img').length };
}"""


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("width", [1280, 390])
def test_each_kind_renders_under_the_reply(browser, stack, tmp_path, width, scheme):
    srv, _j, client = stack
    s = Session(browser, srv, client, width, scheme, height=900 if width > 600 else 820)
    try:
        for kind, spec in SPECS.items():
            s.draw(kind, spec)
            info = s.page.evaluate(LAYOUT_JS)
            snap(s.page, tmp_path, f"{kind}_{width}_{scheme}")
            where = f"{kind} @{width} {scheme}"
            assert info["inConversation"] and info["figTop"] >= info["mdBottom"] - 1, f"{where}: the drawing must sit under the reply text"
            assert info["docW"] <= info["vw"] + 1, f"{where}: the page scrolls sideways ({info['docW']} > {info['vw']})"
            assert info["figRight"] <= info["vw"] + 1, where
            assert info["mode"] == ("narrow" if width < 600 else "wide"), where
            assert info["role"] == "img" and spec["title"] in info["label"] and "competent person" in info["label"], where
            assert info["inkStroke"] == info["want"], f"{where}: lines follow the theme's text colour"
            assert info["texts"] > 10 and "competent person" in info["note"], where
            assert len(info["links"]) == 4 and all(h.startswith("/api/schematics/") for h in info["links"]), where
            assert info["svgW"] <= info["viewW"] + 1, f"{where}: fitted to its box"
            if width < 600 and kind != "cause_effect":
                assert info["svgW"] >= 280, f"{where}: the phone layout is drawn at (about) its natural size, not shrunk"
        assert s.errors == []
    finally:
        s.close()


def test_actual_size_scrolls_inside_the_box_and_downloads_work(browser, stack, tmp_path):
    srv, _j, client = stack
    s = Session(browser, srv, client, 1280, "light")
    try:
        s.draw("fire_loop", fire_spec(loops=1, per=30))
        fig = s.page.locator("figure.sch").last
        fig.locator("button.sch-zoom").click()
        assert fig.locator("button.sch-zoom").get_attribute("aria-pressed") == "true"
        assert s.page.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1")
        with s.page.expect_download() as dl:
            fig.locator("a.sch-dl", has_text="PDF A3").click()
        path = dl.value.path()
        assert Path(path).read_bytes().startswith(b"%PDF") and dl.value.suggested_filename.endswith("-a3.pdf")
        assert s.errors == []
    finally:
        s.close()


def test_a_label_that_tries_to_be_html_is_only_text(browser, stack):
    srv, j, client = stack
    s = Session(browser, srv, client, 1280, "dark")
    try:
        spec = fire_spec(loops=1, per=3)
        spec["loops"][0]["devices"][0]["label"] = '<img src=x onerror="window.pwned=1">Hall'
        s.draw("fire_loop", spec)
        assert s.page.evaluate("() => !window.pwned && document.querySelectorAll('figure.sch img').length === 0")
        assert s.errors == []
    finally:
        s.close()
