"""Real-browser checks of the charts on the display (headless Chrome through Playwright; skipped when Playwright or a Chrome it can
launch is missing, like tests/test_advert_browser.py). The model is mocked: a scripted `show_chart` / `fsm_analyse` tool call goes
through the real app, the display event reaches the real console over its WebSocket and charts.js draws it.

Covered: every chart type at 1280 / 800 / 400 px in both themes (no clipped text, no sideways scroll, the theme's colours, the marks
and focusable points that belong to the data, the accessible summary, the data-table fallback), the tooltip on hover / keyboard / touch,
Download PNG (exact size, real pixels, the right background), Copy as CSV, a chart redrawing when the theme or the width changes, labels
that try to be HTML (nothing is created, the text is shown as text), hostile specs handed straight to the renderer, and a chart
built by fsm_analyse from the mocked FSM.

Set JARVIS_SHOTS=<folder> to also keep a screenshot of each render."""
from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from PIL import Image  # noqa: E402

from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from tests.fakes import FakeClient, message, text_block, tool_block  # noqa: E402
from tests.fsm_data_helpers import jarvis_with_fsm  # noqa: E402
from tests.live_server import LiveServer  # noqa: E402
from tests.test_console_browser import _settings, browser  # noqa: E402,F401
from tests.test_fsm_analyse import TODAY, make_api  # noqa: E402

SHOTS = os.environ.get("JARVIS_SHOTS")
DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SURFACE = {"dark": (16, 31, 56), "light": (255, 255, 255)}      # the console's --panel in each theme


def pts(*pairs):
    return [{"label": a, "value": b} for a, b in pairs]


ENG = ["Dan Harper", "Sam Okafor", "Priya Nair", "Lewis Carter", "Hannah Reid", "Tom Walsh"]
MONTHS = [f"2026-{m:02d}" for m in range(1, 10)]
SPECS = {
    "bar": {"type": "bar", "title": "Jobs completed per engineer, 2026", "x_label": "Engineer", "y_label": "Jobs",
            "series": [{"name": "Jobs", "points": pts(*zip(ENG, [41, 38, 33, 27, 19, 12]))}]},
    "bar_money_long_labels": {"type": "bar", "title": "Overdue invoices by customer", "unit": "gbp", "y_label": "Overdue (£)",
                              "series": [{"name": "Overdue", "points": pts(*[(f"Customer number {i} Holdings (Leeds) Limited", 48213.55 / (i + 1)) for i in range(15)])}]},
    "bar_24": {"type": "bar", "title": "Jobs by engineer, all 24", "series": [{"name": "Jobs", "points": pts(*[(f"Engineer {i + 1}", 50 - i) for i in range(24)])}]},
    "bar_negative": {"type": "bar", "title": "Margin by job type", "unit": "gbp",
                     "series": [{"name": "Margin", "points": pts(("Service", 4200), ("Install", 12800), ("Call-out", -1900), ("Remedial", -320.5))}]},
    "line": {"type": "line", "title": "Jobs per month, 2026", "x_label": "Month", "y_label": "Jobs",
             "series": [{"name": "Service", "points": pts(*zip(MONTHS, [30, 34, 29, 41, 47, 44, 52, 49, 55]))},
                        {"name": "Install", "points": pts(*zip(MONTHS, [8, 12, 9, 15, 11, 18, 14, 20, 22]))}]},
    "line_60": {"type": "line", "title": "Daily call-outs, last 60 days",
                "series": [{"name": "Call-outs", "points": pts(*[(f"Day {i + 1}", (i * 7) % 13 + 3) for i in range(60)])}]},
    "pie": {"type": "pie", "title": "Share of revenue by service type", "unit": "gbp",
            "series": [{"name": "Revenue", "points": pts(("Fire alarms", 123400.5), ("Emergency lighting", 56200), ("Intruder", 31150.25),
                                                         ("CCTV", 24800), ("Extinguishers", 9100), ("Other", 4200))}]},
    "donut": {"type": "donut", "title": "Job status mix",
              "series": [{"name": "Jobs", "points": pts(("Done", 412), ("Booked", 96), ("Overdue", 23), ("Cancelled", 14))}]},
    "stacked_bar": {"type": "stacked_bar", "title": "Visits per quarter by type", "x_label": "Quarter",
                    "series": [{"name": "Service", "points": pts(("2026-Q1", 120), ("2026-Q2", 140), ("2026-Q3", 155), ("2026-Q4", 90))},
                               {"name": "Install", "points": pts(("2026-Q1", 40), ("2026-Q2", 52), ("2026-Q3", 61), ("2026-Q4", 33))},
                               {"name": "Call-out", "points": pts(("2026-Q1", 22), ("2026-Q2", 19), ("2026-Q3", 28), ("2026-Q4", 11))}]},
}
TYPE_WORD = {"bar": "Bar chart", "line": "Line chart", "pie": "Pie chart", "donut": "Donut chart", "stacked_bar": "Stacked bar chart"}


@pytest.fixture(scope="module")
def stack(tmp_path_factory, restore_process_timezone):
    settings = _settings(tmp_path_factory)
    client = FakeClient()
    j = Jarvis(settings, client=client)
    srv = LiveServer(create_app(settings, j))
    yield srv, j, client
    srv.stop()


@pytest.fixture(scope="module")
def fsm_stack(tmp_path_factory, restore_process_timezone):
    settings = _settings(tmp_path_factory)
    j, _ = jarvis_with_fsm(settings, make_api())
    j.fsm_analyse._today = lambda: TODAY
    srv = LiveServer(create_app(settings, j))
    yield srv, j
    srv.stop()


class Session:
    """One page with a counter of how many scripted replies it has seen, so each turn waits for its own."""

    def __init__(self, browser, srv, client, width, scheme="dark", height=900, **ctx):
        self.client, self.done = client, 0
        self.ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, accept_downloads=True, **ctx)
        self.page = self.ctx.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" and "favicon" not in m.text else None)
        self.page.goto(srv.url + "/", wait_until="domcontentloaded")
        self.page.wait_for_selector("#needs-list .need, #needs-list .needs-clear:not(:has-text('Loading'))", timeout=15000)

    def say(self, calls, wait_for="svg.chart-svg", prompt="show me a chart"):
        self.client.beta.messages.script += [message([tool_block(n, a) for n, a in calls], "tool_use"), message([text_block("Chart is on the display.")])]
        self.done += 1
        self.page.evaluate("() => document.getElementById('display-close').click()")            # the last chart is closed, as a person would
        self.page.wait_for_function("() => !document.getElementById('display').classList.contains('open')")
        self.page.fill("#input", prompt)
        self.page.click("#btn-send")
        self.page.wait_for_function("(n) => document.body.innerText.split('Chart is on the display.').length - 1 >= n", arg=self.done, timeout=30000)
        self.page.wait_for_selector(f".chart {wait_for}", timeout=15000)
        self.page.wait_for_timeout(150)

    def show(self, spec, **kw):
        self.say([("show_chart", spec)], **kw)
        self.page.wait_for_function("(t) => [...document.querySelectorAll('.chart-title')].some((e) => e.textContent === t)", arg=spec["title"])

    def close(self):
        self.ctx.close()


def snap(page, tmp_path, name):
    folder = Path(SHOTS) if SHOTS else tmp_path
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / f"{name}.png"))


# --------------------------------------------------------------------------- every type, three widths, two themes
LAYOUT_JS = """() => {
  const svg = document.querySelector('.chart svg.chart-svg'), r = svg.getBoundingClientRect();
  const body = document.querySelector('#display-body').getBoundingClientRect(), vb = svg.viewBox.baseVal;
  const clipped = [...svg.querySelectorAll('text')].filter((t) => { const b = t.getBBox(); return b.x < -1 || b.x + b.width > vb.width + 1; }).map((t) => t.textContent);
  const fig = document.querySelector('.chart').getBoundingClientRect();
  const first = svg.querySelector('.chart-mark');
  return { svgW: r.width, svgH: r.height, vbW: vb.width, bodyW: body.width, docW: document.documentElement.scrollWidth, vw: innerWidth, figRight: fig.right,
           clipped, marks: svg.querySelectorAll('.chart-mark').length, hits: document.querySelectorAll('.chart-hit').length,
           tab0: document.querySelectorAll('.chart-hit[tabindex="0"]').length, firstFill: first ? first.getAttribute('fill') : null,
           role: svg.getAttribute('role'), label: svg.getAttribute('aria-label'), texts: svg.querySelectorAll('text').length,
           legend: !document.querySelector('.chart-legend').hidden, rows: document.querySelectorAll('.chart-data tbody tr').length };
}"""
POINTS = {k: sum(len(s["points"]) for s in v["series"]) for k, v in SPECS.items()}
CATS = {k: len({p["label"] for s in v["series"] for p in s["points"]}) for k, v in SPECS.items()}


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("width", [1280, 800, 400])
def test_every_chart_type_renders_cleanly(browser, stack, tmp_path, width, scheme):
    srv, _j, client = stack
    s = Session(browser, srv, client, width, scheme)
    try:
        for name, spec in SPECS.items():
            s.show(spec)
            info = s.page.evaluate(LAYOUT_JS)
            snap(s.page, tmp_path, f"{name}_{width}_{scheme}")
            where = f"{name} @{width} {scheme}"
            assert info["role"] == "img" and TYPE_WORD[spec["type"]] in info["label"] and spec["title"] in info["label"], where
            assert info["clipped"] == [], f"{where}: text sticks out of the chart: {info['clipped']}"
            assert info["docW"] <= info["vw"] + 1, f"{where}: the page scrolls sideways ({info['docW']} > {info['vw']})"
            assert info["svgW"] <= info["bodyW"] + 1 and info["figRight"] <= info["vw"] + 1, where
            assert info["svgW"] >= min(240, width - 60), f"{where}: the chart is squashed ({info['svgW']}px)"
            assert info["hits"] == CATS[name] and info["tab0"] == 1, f"{where}: focusable points"
            assert info["rows"] == CATS[name], f"{where}: the data table has every row"
            if spec["type"] in ("bar", "pie", "donut"):
                assert info["marks"] == POINTS[name], where
            if spec["type"] == "stacked_bar":
                assert info["marks"] == POINTS[name]
            if spec["type"] in ("bar", "stacked_bar", "line"):
                assert info["firstFill"] in (None, (DARK if scheme == "dark" else LIGHT)[0]), f"{where}: the theme's first colour {info['firstFill']}"
            assert info["legend"] == (spec["type"] in ("pie", "donut") or len(spec["series"]) > 1), where
            assert info["texts"] >= {"pie": 0, "donut": 2}.get(spec["type"], 3), where
            s.page.keyboard.press("Escape")
        assert s.errors == []
    finally:
        s.close()


def test_the_series_colours_are_the_checked_palette_in_order_and_follow_the_theme(browser, stack):
    srv, _j, client = stack
    for scheme, pal in (("dark", DARK), ("light", LIGHT)):
        s = Session(browser, srv, client, 1280, scheme)
        try:
            s.show(SPECS["stacked_bar"])
            fills = s.page.evaluate("() => [...new Set([...document.querySelectorAll('.chart-mark')].map((m) => m.getAttribute('fill')))]")
            assert fills == pal[:3]
            sw = s.page.evaluate("() => [...document.querySelectorAll('.chart-legend .sw')].map((e) => getComputedStyle(e).backgroundColor)")
            assert len(sw) == 3 and len(set(sw)) == 3
        finally:
            s.close()


def test_a_pie_other_slice_is_neutral_not_a_hue(browser, stack):
    srv, _j, client = stack
    s = Session(browser, srv, client, 1280, "dark")
    try:
        s.show(SPECS["pie"])
        fills = s.page.evaluate("() => [...document.querySelectorAll('.chart-mark')].map((m) => m.getAttribute('fill'))")
        assert fills[:5] == DARK[:5] and fills[5] not in DARK
    finally:
        s.close()


def test_the_chart_redraws_when_the_theme_or_the_width_changes(browser, stack):
    srv, _j, client = stack
    s = Session(browser, srv, client, 1100, "dark")
    try:
        s.show(SPECS["bar"])
        fill = lambda: s.page.evaluate("() => document.querySelector('.chart-mark').getAttribute('fill')")
        assert fill() == DARK[0]
        s.page.emulate_media(color_scheme="light")
        s.page.wait_for_function("(c) => document.querySelector('.chart-mark').getAttribute('fill') === c", arg=LIGHT[0])
        wide = s.page.evaluate("() => document.querySelector('.chart svg').getBoundingClientRect().width")
        s.page.set_viewport_size({"width": 420, "height": 900})
        s.page.wait_for_function("(w) => document.querySelector('.chart svg').getBoundingClientRect().width < w - 200", arg=wide)
        info = s.page.evaluate(LAYOUT_JS)
        assert info["clipped"] == [] and info["docW"] <= info["vw"] + 1
        assert s.errors == []
    finally:
        s.close()


# --------------------------------------------------------------------------- the accessible alternatives
def test_the_summary_the_data_table_and_the_title_are_there_for_screen_readers(browser, stack):
    srv, _j, client = stack
    s = Session(browser, srv, client, 1280, "dark")
    try:
        s.show(SPECS["bar"])
        label = s.page.get_attribute("svg.chart-svg", "aria-label")
        assert "Bar chart: Jobs completed per engineer, 2026" in label and "6 bars" in label and "highest Dan Harper 41" in label and "lowest Tom Walsh 12" in label
        assert "table below" in label
        details = s.page.locator("details.chart-data")
        assert details.get_attribute("open") is None                              # collapsed until asked for
        details.locator("summary").click()
        rows = s.page.evaluate("() => [...document.querySelectorAll('.chart-data tbody tr')].map((r) => [...r.children].map((c) => c.textContent))")
        assert rows[0] == ["Dan Harper", "41"] and rows[-1] == ["Tom Walsh", "12"] and len(rows) == 6
        assert s.page.evaluate("() => document.querySelector('.chart-data th[scope=row]') !== null")
        # the display header already says the title, so the chart's own caption is there for screen readers only
        assert s.page.evaluate("() => getComputedStyle(document.querySelector('.chart-title')).position") == "absolute"
        s.show(SPECS["donut"])
        pie_label = s.page.get_attribute("svg.chart-svg", "aria-label")
        assert "4 slices, total 545" in pie_label and "largest Done 412 (75.6%)" in pie_label
        head = s.page.evaluate("() => [...document.querySelectorAll('.chart-data thead th')].map((c) => c.textContent)")
        assert head == ["Slice", "Jobs", "Share"]
    finally:
        s.close()


def test_points_are_real_buttons_with_names_and_the_tooltip_follows_hover_keyboard_and_touch(browser, stack):
    srv, _j, client = stack
    s = Session(browser, srv, client, 1280, "dark")
    p = s.page
    try:
        s.show(SPECS["bar"])
        assert p.get_attribute('.chart-hit[data-idx="0"]', "aria-label") == "Dan Harper: 41"
        assert p.evaluate("() => document.querySelector('.chart-hit').tagName") == "BUTTON"
        tip = p.locator(".chart-tip")
        assert tip.is_hidden()
        p.focus('.chart-hit[data-idx="0"]')
        assert tip.is_visible() and "Dan Harper" in tip.inner_text() and "41" in tip.inner_text()
        p.keyboard.press("ArrowRight")
        assert p.evaluate("() => document.activeElement.dataset.idx") == "1" and "Sam Okafor" in tip.inner_text() and "38" in tip.inner_text()
        p.keyboard.press("End")
        assert p.evaluate("() => document.activeElement.dataset.idx") == "5" and "Tom Walsh" in tip.inner_text()
        p.keyboard.press("ArrowRight")
        assert p.evaluate("() => document.activeElement.dataset.idx") == "0"                     # wraps
        assert p.evaluate("() => document.querySelectorAll('.chart-hit[tabindex=\"0\"]').length") == 1
        assert p.evaluate("() => document.querySelectorAll('.chart-mark.is-active').length") == 1  # the focused bar is lifted
        p.keyboard.press("Escape")
        assert p.evaluate("() => document.querySelector('.chart-tip').hidden") is True
        # mouse: hovering a bar shows the same
        box = p.locator('.chart-hit[data-idx="2"]').bounding_box()
        p.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        p.wait_for_function("() => !document.querySelector('.chart-tip').hidden")
        assert "Priya Nair" in tip.inner_text()
        # money and several series in a tooltip: values lead, names follow
        s.show(SPECS["line"])
        p.focus('.chart-hit[data-idx="3"]')
        text = tip.inner_text()
        assert "2026-04" in text and "41" in text and "Service" in text and "15" in text and "Install" in text
        s.show(SPECS["pie"])
        p.focus('.chart-hit[data-idx="0"]')
        assert "£123,400.50" in tip.inner_text() and "49.6% of the total" in tip.inner_text()
        assert s.errors == []
    finally:
        s.close()


def test_a_tap_shows_the_tooltip_on_a_touch_screen(browser, stack):
    srv, _j, client = stack
    s = Session(browser, srv, client, 400, "light", has_touch=True, is_mobile=True)
    try:
        s.show(SPECS["stacked_bar"])
        box = s.page.locator('.chart-hit[data-idx="1"]').bounding_box()
        s.page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        s.page.wait_for_function("() => !document.querySelector('.chart-tip').hidden")
        text = s.page.locator(".chart-tip").inner_text()
        assert "2026-Q2" in text and "140" in text and "Install" in text
        s.show(SPECS["line_60"])
        box = s.page.locator(".chart-overlay").bounding_box()
        s.page.touchscreen.tap(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
        s.page.wait_for_function("() => !document.querySelector('.chart-tip').hidden")
        assert "Day " in s.page.locator(".chart-tip").inner_text()
    finally:
        s.close()


# --------------------------------------------------------------------------- Download PNG and Copy as CSV
def _png(download) -> Image.Image:
    return Image.open(io.BytesIO(Path(download.path()).read_bytes())).convert("RGB")


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("name", ["bar", "line", "pie", "donut", "stacked_bar", "bar_money_long_labels"])
def test_download_png_is_exactly_1600_by_900_with_the_chart_painted_in(browser, stack, tmp_path, name, scheme):
    srv, _j, client = stack
    s = Session(browser, srv, client, 1280, scheme)
    try:
        spec = SPECS[name]
        s.show(spec)
        with s.page.expect_download() as d:
            s.page.click(".chart-png")
        dl = d.value
        assert dl.suggested_filename.endswith(".png") and " " not in dl.suggested_filename
        img = _png(dl)
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            img.save(Path(SHOTS) / f"export_{name}_{scheme}.png")
        assert img.size == (1600, 900)
        bg = SURFACE[scheme]
        assert all(abs(a - b) <= 2 for a, b in zip(img.getpixel((4, 4)), bg)) and all(abs(a - b) <= 2 for a, b in zip(img.getpixel((1595, 895)), bg))
        pal = DARK if scheme == "dark" else LIGHT
        px = img.load()
        counts = {c: 0 for c in pal[:3]}
        rgb = {c: tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in counts}
        ink = 0
        for y in range(0, 900, 2):
            for x in range(0, 1600, 2):
                p = px[x, y]
                for c, t in rgb.items():
                    if all(abs(a - b) <= 6 for a, b in zip(p, t)):
                        counts[c] += 1
                if y < 80 and 30 <= x <= 900 and sum(abs(a - b) for a, b in zip(p, bg)) > 120:
                    ink += 1
        assert counts[pal[0]] > 400, f"the first series colour is painted: {counts}"        # sampled every 2nd pixel
        assert ink > 60, "the title is painted across the top"
        if spec["type"] in ("stacked_bar", "line"):
            assert counts[pal[1]] > 100
        assert s.page.locator(".chart-status").inner_text().startswith("Saved 1600 × 900")
        assert s.errors == []
    finally:
        s.close()


def test_copy_as_csv_puts_the_data_on_the_clipboard_and_defuses_formulas(browser, stack):
    srv, _j, client = stack
    s = Session(browser, srv, client, 1280, "dark", permissions=["clipboard-read", "clipboard-write"])
    try:
        spec = {"type": "bar", "title": "Quotes, by customer", "x_label": "Customer",
                "series": [{"name": "Value", "points": pts(("Kestrel, Ltd", 1250.5), ('Say "hi"', 3), ("=HYPERLINK(1)", 4), ("-cmd", 5))}]}
        s.show(spec)
        s.page.click(".chart-csv")
        s.page.wait_for_function("() => document.querySelector('.chart-status').textContent.startsWith('Copied')")
        clip = s.page.evaluate("() => navigator.clipboard.readText()")
        assert clip == 'Customer,Value\r\n"Kestrel, Ltd",1250.5\r\n"Say ""hi""",3\r\n\'=HYPERLINK(1),4\r\n\'-cmd,5\r\n'
        assert s.page.locator(".chart-status").inner_text() == "Copied 4 rows as CSV."
        s.show(SPECS["stacked_bar"])
        s.page.click(".chart-csv")
        s.page.wait_for_function("() => document.querySelector('.chart-status').textContent.startsWith('Copied')")
        assert s.page.evaluate("() => navigator.clipboard.readText()").splitlines()[:2] == ["Quarter,Service,Install,Call-out", "2026-Q1,120,40,22"]
    finally:
        s.close()


# --------------------------------------------------------------------------- hostile content
def test_labels_that_look_like_html_become_text_and_nothing_runs(browser, stack):
    srv, _j, client = stack
    s = Session(browser, srv, client, 1280, "dark")
    try:
        evil = {"type": "bar", "title": "Visits <img src=x onerror=window.__xss=1>by day",
                "series": [{"name": "<b>n</b>", "points": pts(("<img src=x onerror=window.__xss=1>Mon", 3), ("<script>window.__xss=1</script>Tue", 5),
                                                              ("&lt;b&gt;Wed&lt;/b&gt;", 4))}]}
        s.show({**evil, "title": "Visits by day"})                      # through the server: tags are stripped before it ever reaches the page
        assert s.page.evaluate("() => window.__xss") is None
        labels = s.page.evaluate("() => [...document.querySelectorAll('.chart-hit')].map((b) => b.getAttribute('aria-label'))")
        assert labels[0] == "Mon: 3" and labels[1] == "Tue: 5" or labels[1].endswith(": 5")
        assert "<" not in "".join(labels[:2])
        # and the renderer on its own, given the raw strings: they are drawn as characters
        out = s.page.evaluate("""() => {
          const host = document.createElement('div'); document.body.appendChild(host);
          const h = window.JarvisCharts.mount(host, %s);
          const bad = host.querySelectorAll('img,script,iframe,object,embed,link,style,a[href]').length;
          const txt = [...host.querySelectorAll('svg text')].map((t) => t.textContent).join('|');
          const tbl = [...host.querySelectorAll('.chart-data th, .chart-data td')].map((t) => t.textContent).join('|');
          host.querySelector('.chart-hit').focus();
          const tip = host.querySelector('.chart-tip').textContent;
          return { bad, txt, tbl, tip, xss: window.__xss === undefined, ok: !!h };
        }""" % __import__("json").dumps(evil))
        assert out["ok"] and out["bad"] == 0 and out["xss"] is True
        assert "<img src=x onerror=window.__xss=1>Mon" in out["tbl"] and "<script>window.__xss=1</script>Tue" in out["tbl"]
        assert "<img src=x onerror=window.__xss=1>Mon" in out["tip"]
        assert "&lt;b&gt;Wed&lt;/b&gt;" in out["tbl"]                    # an entity stays an entity, never decoded into markup
        assert s.page.evaluate("() => window.__xss") is None
        assert s.errors == []
    finally:
        s.close()


HOSTILE = [
    {"type": "radar", "title": "t", "series": [{"points": [{"label": "a", "value": 1}]}]},
    {"type": "bar", "title": "t", "series": [{"points": [{"label": "a", "value": "NaN"}]}]},
    {"type": "bar", "title": "t", "series": [{"points": [{"label": "a", "value": None}]}]},
    {"type": "bar", "title": "t", "series": [{"points": [{"label": "a", "value": 1e300}]}]},
    {"type": "bar", "title": "t", "series": [{"points": [{"label": "a", "value": True}]}]},
    {"type": "bar", "title": "", "series": [{"points": [{"label": "a", "value": 1}]}]},
    {"type": "bar", "title": "t", "series": []},
    {"type": "bar", "title": "t", "series": "abc"},
    {"type": "bar", "title": "t", "series": [{"points": [{"label": "a", "value": 1}, {"label": "a", "value": 2}]}]},
    {"type": "pie", "title": "t", "series": [{"points": [{"label": "a", "value": -1}, {"label": "b", "value": 2}]}]},
    {"type": "pie", "title": "t", "series": [{"points": [{"label": "a", "value": 0}]}]},
    {"type": "bar", "title": "t", "series": [{"points": [{"label": f"L{i}", "value": 1} for i in range(25)]}]},
    {"type": "line", "title": "t", "series": [{"points": [{"label": f"L{i}", "value": 1} for i in range(61)]}]},
    {"type": "bar", "title": "t", "series": [{"points": [{"label": "a", "value": 1}]}, {"points": [{"label": "a", "value": 1}]}]},
    None, 5, "bar", [], {"type": "__proto__", "title": "t", "series": []},
]


def test_hostile_specs_given_straight_to_the_renderer_are_refused_with_a_note(browser, stack):
    srv, _j, client = stack
    s = Session(browser, srv, client, 1000, "dark")
    try:
        res = s.page.evaluate("""(specs) => specs.map((spec) => {
          const host = document.createElement('div'); document.body.appendChild(host);
          let threw = false, h = null;
          try { h = window.JarvisCharts.mount(host, spec); } catch (e) { threw = true; }
          return { threw, handle: h === null, note: (host.querySelector('.chart-status') || {}).textContent || '', svg: host.querySelectorAll('svg').length };
        })""", HOSTILE)
        for spec, r in zip(HOSTILE, res):
            assert not r["threw"] and r["handle"] and r["svg"] == 0 and "can't be shown" in r["note"], (spec, r)
        v = s.page.evaluate("() => window.JarvisCharts.validate({type:'bar',title:'T',series:[{points:[{label:'a',value:'1,250.5'}]}]})")
        assert v["ok"] and v["spec"]["series"][0]["points"][0]["value"] == 1250.5
        giant = s.page.evaluate("""() => { const t0 = performance.now(); const r = window.JarvisCharts.validate({type:'line',title:'T',
            series:[{points: Array.from({length: 1000000}, (_, i) => ({label: 'L' + i, value: i}))}]}); return [r.ok, performance.now() - t0]; }""")
        assert giant[0] is False and giant[1] < 1500
        assert s.errors == []
    finally:
        s.close()


# --------------------------------------------------------------------------- the helpers, run for real in the browser
def test_nice_ticks_and_number_formats(browser, stack):
    srv, _j, client = stack
    s = Session(browser, srv, client, 1000, "dark")
    try:
        ticks = s.page.evaluate("() => [[0, 41], [0, 7], [0, 1], [-1900, 12800], [0, 123400], [3, 3], [0, 0], [0.12, 0.47], [5, 1]].map(([a, b]) => window.JarvisCharts.niceTicks(a, b))")
        assert ticks[0]["ticks"] == [0, 10, 20, 30, 40, 50] and ticks[1]["ticks"] == [0, 2, 4, 6, 8] and ticks[2]["ticks"] == [0, 0.2, 0.4, 0.6, 0.8, 1]
        assert ticks[3]["ticks"] == [-5000, 0, 5000, 10000, 15000] and ticks[4]["step"] in (20000, 50000)
        assert ticks[5]["ticks"][0] <= 3 <= ticks[5]["ticks"][-1] and len(ticks[5]["ticks"]) >= 2 and ticks[6]["ticks"] == [0, 0.2, 0.4, 0.6, 0.8, 1]
        assert ticks[7]["ticks"][0] <= 0.12 and ticks[7]["ticks"][-1] >= 0.47 and ticks[8]["ticks"][0] == 1 and ticks[8]["ticks"][-1] >= 5
        for t in ticks:
            assert all(a < b for a, b in zip(t["ticks"], t["ticks"][1:])) and 2 <= len(t["ticks"]) <= 8
        fmt = s.page.evaluate("""() => { const f = window.JarvisCharts.formatValue; return [f(1234.5, 'gbp'), f(-1234.5, 'gbp'), f(12500, 'gbp', 'tick', 5000), f(2500000, 'gbp', 'tick', 500000),
            f(12.34, 'percent'), f(25, 'percent', 'tick', 25), f(1234567.891234, 'number'), f(0.00005, 'number'), f(Infinity, 'number'), f(3, 'number', 'tick', 0.5)]; }""")
        assert fmt == ["£1,234.50", "-£1,234.50", "£12.5k", "£2.5m", "12.3%", "25%", "1,234,567.8912", "0.0001", "-", "3"]
    finally:
        s.close()


# --------------------------------------------------------------------------- owner-only and fsm_analyse through the real app
def test_an_owner_only_chart_still_shows_on_the_owners_own_console(browser, stack):
    srv, _j, client = stack
    s = Session(browser, srv, client, 1280, "dark")
    try:
        spec = {"type": "donut", "title": "Payroll by team", "owner_only": True, "unit": "gbp",
                "series": [{"name": "Pay", "points": pts(("Fitters", 31000), ("Office", 12500))}]}
        s.show(spec)
        assert "£43,500.00" in s.page.text_content(".chart svg")
    finally:
        s.close()


def test_a_chart_asked_of_fsm_analyse_is_drawn_from_the_fsm_numbers(browser, fsm_stack):
    srv, j = fsm_stack
    s = Session(browser, srv, j.client, 1280, "dark")
    try:
        j.client.beta.messages.script += [
            message([tool_block("fsm_analyse", {"resource": "jobs", "group_by": ["engineer"], "metrics": ["count", "sum(value)"], "chart": "bar",
                                                "chart_metric": "sum(value)", "chart_title": "Job value by engineer, 2026",
                                                "period": {"field": "completed_date", "preset": "this_year"}})], "tool_use"),
            message([text_block("Chart is on the display.")])]
        s.done += 1
        s.page.fill("#input", "job value by engineer this year")
        s.page.click("#btn-send")
        s.page.wait_for_selector(".chart svg.chart-svg", timeout=30000)
        labels = s.page.evaluate("() => [...document.querySelectorAll('.chart-hit')].map((b) => b.getAttribute('aria-label'))")
        assert labels == ["Priya: £1,200.50", "Sam: £1,125.05", "Dan: £600.90"] or sorted(labels) == sorted(["Priya: £1,200.50", "Sam: £1,125.05", "Dan: £600.90"])
        body = s.page.inner_text("#display-body")
        assert "this year" in body and "All rows" in body and "left out because completed_date" in body       # the table and the notes under the chart
        assert s.errors == []
    finally:
        s.close()
