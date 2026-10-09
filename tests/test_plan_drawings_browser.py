"""Real-browser checks of the Drawings editor (headless Chrome through Playwright; skipped when Playwright or a Chrome it can launch is
missing, like tests/test_charts_browser.py). The app is the real FastAPI app on a real local port; the model is faked.

Covered at desktop (1280) and phone (400) widths, in dark and light themes: the rail item opens the list, a drawing opens in the editor
(the drawer widens on a desktop and is never wider than the window; the page never scrolls sideways), drag a device with the mouse and
with a real touch, add one from the palette (mouse click and touch tap), delete one (button and keyboard), undo, save (the server has it),
export PDF and PNG (real downloads), draw a zone, place "You are here" and turn a zone chart, a view-only (office) editor, and a
draft drawn by Jarvis through the chat (draw_on_plan) opening in the editor from the display.

Set JARVIS_SHOTS=<folder> to also keep a screenshot of each step."""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from jarvis.services import plan_drawings as pd  # noqa: E402
from jarvis.services.plan_drawings import PlanDrawings, render_plan  # noqa: E402
from tests.fakes import FakeClient, message, text_block, tool_block  # noqa: E402
from tests.live_server import LiveServer  # noqa: E402
from tests.test_console_browser import _settings, browser  # noqa: E402,F401
from tests.test_plan_drawings import plan_png  # noqa: E402

SHOTS = os.environ.get("JARVIS_SHOTS")
DEVICES = [{"type": "smoke", "x": 0.25, "y": 0.3, "label": "Office"}, {"type": "call_point", "x": 0.06, "y": 0.9, "label": "Exit"},
           {"type": "sounder", "x": 0.75, "y": 0.5, "label": "Workshop"}]


@pytest.fixture(scope="module")
def stack(tmp_path_factory, restore_process_timezone):
    settings = _settings(tmp_path_factory)
    client = FakeClient()
    j = Jarvis(settings, client=client)
    j.drawings = PlanDrawings(j, now=lambda: datetime(2026, 10, 9, 9, 30, tzinfo=timezone.utc), today=lambda: date(2026, 10, 9))
    srv = LiveServer(create_app(settings, j))
    yield srv, j, client
    srv.stop()


def fresh(j, kind="devices", content=None, job_ref=""):
    """A new drawing on a synthetic plan, so every test starts from a known state."""
    pid = j.drawings.add_plan(render_plan(plan_png(), "unit-4.png"), "Uploaded: unit-4.png", "the owner")
    row = j.drawings.create(kind=kind, plan_id=pid, by="the owner", content=content if content is not None else {"devices": [dict(d) for d in DEVICES]},
                            meta={"title": f"Test {kind}", "site_name": "Unit 4 Test Park", "job_ref": job_ref})
    return row["id"]


def shot(page, tmp_path, name):
    folder = Path(SHOTS) if SHOTS else tmp_path
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / f"{name}.png"))


def opened(browser, srv, width, height, scheme="dark", accept_dialogs=True, **ctx):
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, accept_downloads=True, **ctx)
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    if accept_dialogs:
        page.on("dialog", lambda d: d.accept())
    page.goto(srv.url + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#needs-list .need, #needs-list .needs-clear", timeout=15000)
    return context, page


def open_drawing(page, did):
    page.click('.rail-item[data-pop="drawings"]')
    page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
    page.wait_for_selector(f'[data-drw-open="{did}"]', timeout=10000)
    page.click(f'[data-drw-open="{did}"]')
    page.wait_for_selector("#drw-editor:not([hidden]) svg.drw-svg image", timeout=10000)
    page.wait_for_timeout(250)


def no_sideways_scroll(page):
    doc, vw = page.evaluate("[document.documentElement.scrollWidth, innerWidth]")
    assert doc <= vw, (doc, vw)
    d = page.evaluate("(() => { const d = document.getElementById('drawer'); return [d.getBoundingClientRect().right, d.scrollWidth, d.offsetWidth]; })()")
    assert d[0] <= page.viewport_size["width"] + 1 and d[1] <= d[2] + 1, d


def centre(page, selector):
    b = page.locator(selector).first.bounding_box()
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2


def show_plan(page):
    """Bring the whole plan on screen (a no-op when it already is): on a phone the side panel can push it up out of view."""
    page.evaluate("document.querySelector('#drw-editor .drw-stage').scrollIntoView({block: 'nearest'})")


def dev_centre(page, i):
    """Screen coordinates of device i's centre (its symbol's origin), even when the symbol hangs over the plan's edge."""
    show_plan(page)
    return tuple(page.evaluate("""(i) => { const g = document.querySelector(`#drw-editor .drw-dev[data-i="${i}"]`);
        const p = new DOMPoint(0, 0).matrixTransform(g.getScreenCTM()); return [p.x, p.y]; }""", i))


def plan_point(page, fx, fy):
    """Screen coordinates of a point a fraction across / down the visible plan (the plan is scrolled into view first: on a phone the
    pop-up's own Close bar sits over the bottom of the screen)."""
    show_plan(page)
    b = page.locator("#drw-editor svg.drw-svg").bounding_box()
    return b["x"] + b["width"] * fx, b["y"] + b["height"] * fy


def device_count(page):
    return page.evaluate("document.querySelectorAll('#drw-editor .drw-dev').length")


def save(page):
    page.click('#drw-editor [data-drw="save"]')
    page.wait_for_function("document.querySelector('#drw-editor [data-f=\"status\"]').dataset.state === 'saved'", timeout=10000)


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("width,height", [(1280, 860), (400, 820)])
def test_drag_add_delete_undo_save_and_export_with_a_mouse(browser, stack, tmp_path, width, height, scheme):
    srv, j, _ = stack
    did = fresh(j)
    ctx, page = opened(browser, srv, width, height, scheme)
    try:
        open_drawing(page, did)
        no_sideways_scroll(page)
        dw = page.evaluate("document.getElementById('drawer').offsetWidth")
        assert dw == width if width < 760 else dw > 900, dw              # full-screen on a phone, widened on a desktop
        assert device_count(page) == 3
        bg = page.evaluate("getComputedStyle(document.querySelector('#drw-editor .drw-sel')).backgroundColor")
        assert bg == ("rgb(16, 31, 56)" if scheme == "dark" else "rgb(255, 255, 255)")   # the theme's panel colour
        shot(page, tmp_path, f"drawings-open-{width}-{scheme}")

        # drag the smoke detector from (0.25, 0.3) to the middle of the plan
        x0, y0 = dev_centre(page, 0)
        x1, y1 = plan_point(page, 0.5, 0.5)
        page.mouse.move(x0, y0)
        page.mouse.down()
        page.mouse.move((x0 + x1) / 2, (y0 + y1) / 2, steps=4)
        page.mouse.move(x1, y1, steps=4)
        page.mouse.up()
        assert page.inner_text('#drw-editor [data-f="status"]') == "Unsaved changes"

        # add a heat detector from the palette
        page.click('#drw-editor [data-mode="add"]')
        page.click('#drw-editor .drw-pal[data-type="heat"]')
        no_sideways_scroll(page)
        page.mouse.click(*plan_point(page, 0.3, 0.75))
        assert device_count(page) == 4
        assert "Heat detector" in page.inner_text('#drw-editor [data-f="legend"]')
        page.click('#drw-editor [data-mode="move"]')

        # delete the sounder with the button, then the call point with the keyboard, then undo the last one
        page.mouse.click(*dev_centre(page, 2))
        page.click('#drw-editor [data-drw="deldev"]')
        assert device_count(page) == 3
        page.mouse.click(*dev_centre(page, 1))
        page.focus('#drw-editor [data-f="stage"]')
        page.keyboard.press("Delete")
        assert device_count(page) == 2
        page.click('#drw-editor [data-drw="undo"]')
        assert device_count(page) == 3
        shot(page, tmp_path, f"drawings-edited-{width}-{scheme}")

        save(page)
        row = j.drawings._row(did)
        devs = row["content"]["devices"]
        assert sorted(d["type"] for d in devs) == ["heat", "mcp", "smoke"] and row["version"] == 2
        smoke = next(d for d in devs if d["type"] == "smoke")
        assert abs(smoke["x"] - 0.5) < 0.04 and abs(smoke["y"] - 0.5) < 0.04, smoke      # it went where it was dragged to

        for fmt, magic in (("pdf", b"%PDF"), ("png", b"\x89PNG")):
            with page.expect_download(timeout=30000) as dl:
                page.click(f'#drw-editor [data-drw="{fmt}"]')
            path = dl.value.path()
            assert dl.value.suggested_filename == f"D{did}-Test-devices-rev-A-A3.{fmt}"
            assert Path(path).read_bytes()[:4] == magic
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_touch_tap_adds_and_touch_drag_moves_on_a_phone(browser, stack, tmp_path):
    srv, j, _ = stack
    did = fresh(j)
    ctx, page = opened(browser, srv, 400, 820, "light", has_touch=True, is_mobile=True)
    try:
        open_drawing(page, did)
        no_sideways_scroll(page)
        page.tap('#drw-editor [data-mode="add"]')
        page.tap('#drw-editor .drw-pal[data-type="vad"]')
        page.touchscreen.tap(*plan_point(page, 0.6, 0.25))
        assert device_count(page) == 4
        page.tap('#drw-editor [data-mode="move"]')
        cdp = ctx.new_cdp_session(page)
        x0, y0 = dev_centre(page, 3)
        x1, y1 = plan_point(page, 0.85, 0.8)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x0, "y": y0}]})
        for k in range(1, 9):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x0 + (x1 - x0) * k / 8, "y": y0 + (y1 - y0) * k / 8}]})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        shot(page, tmp_path, "drawings-touch")
        save(page)
        vad = next(d for d in j.drawings._row(did)["content"]["devices"] if d["type"] == "vad")
        assert abs(vad["x"] - 0.85) < 0.05 and abs(vad["y"] - 0.8) < 0.05, vad
        no_sideways_scroll(page)
        assert not page.errors, page.errors
    finally:
        ctx.close()


@pytest.mark.parametrize("width,height", [(1280, 860), (400, 820)])
def test_draw_a_zone_place_you_are_here_and_turn_the_chart(browser, stack, tmp_path, width, height):
    srv, j, _ = stack
    did = fresh(j, "zones", {"zones": [{"number": 1, "name": "Office", "polygon": [[0.05, 0.05], [0.5, 0.05], [0.5, 0.5], [0.05, 0.5]]}]})
    ctx, page = opened(browser, srv, width, height)
    try:
        open_drawing(page, did)
        assert page.evaluate("document.querySelectorAll('#drw-editor .drw-zone').length") == 1
        page.click('#drw-editor [data-mode="zone"]')
        for fx, fy in ((0.55, 0.1), (0.9, 0.1), (0.9, 0.9), (0.55, 0.9)):
            page.mouse.click(*plan_point(page, fx, fy))
        page.click('#drw-editor [data-drw="finish"]')
        assert page.evaluate("document.querySelectorAll('#drw-editor .drw-zone').length") == 2
        page.fill('#drw-editor [data-zone="name"]', "Workshop")
        page.fill('#drw-editor [data-zone="number"]', "4")
        assert "Workshop" in page.inner_text('#drw-editor [data-f="legend"]')
        page.click('#drw-editor [data-mode="here"]')
        page.mouse.click(*plan_point(page, 0.1, 0.9))
        before = page.evaluate("document.querySelector('#drw-editor svg.drw-svg').getAttribute('viewBox')")
        page.click('#drw-editor [data-drw="rotr"]')
        after = page.evaluate("document.querySelector('#drw-editor svg.drw-svg').getAttribute('viewBox')")
        assert before.split()[2:] == after.split()[2:][::-1]          # turned a quarter: width and height swap
        no_sideways_scroll(page)
        shot(page, tmp_path, f"drawings-zones-{width}")
        save(page)
        c = j.drawings._row(did)["content"]
        assert c["rotation"] == 90 and [z["number"] for z in c["zones"]] == [1, 4] and c["zones"][1]["name"] == "Workshop"
        assert c["you_are_here"] and abs(c["you_are_here"]["x"] - 0.1) < 0.03 and abs(c["you_are_here"]["y"] - 0.9) < 0.03
        with page.expect_download(timeout=30000) as dl:
            page.click('#drw-editor [data-drw="pdf"]')
        assert Path(dl.value.path()).read_bytes()[:4] == b"%PDF"
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_closing_with_unsaved_changes_asks_and_the_chat_is_not_covered_once_closed(browser, stack):
    srv, j, _ = stack
    did = fresh(j)
    ctx, page = opened(browser, srv, 1280, 860, accept_dialogs=False)
    try:
        open_drawing(page, did)
        page.click('#drw-editor [data-drw="rotr"]')
        asked, answer = [], {"yes": False}
        page.on("dialog", lambda d: (asked.append(d.message), d.accept() if answer["yes"] else d.dismiss()))
        page.click("#drawer-close")
        assert any("Discard unsaved drawing changes?" in m for m in asked)
        assert page.evaluate("document.getElementById('drawer').classList.contains('open')")   # kept open: the person said no
        answer["yes"] = True
        page.click("#drawer-close")
        page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
        assert page.is_visible("#conversation") and page.is_visible("#input")
        assert page.evaluate("document.getElementById('drawer').classList.contains('drw-wide')") is False
    finally:
        ctx.close()


@pytest.fixture(scope="module")
def team_stack(tmp_path_factory, restore_process_timezone):
    from tests.test_team_console_browser import OWNER_PW

    settings = _settings(tmp_path_factory, jarvis_owner_password=OWNER_PW)
    j = Jarvis(settings, client=FakeClient())
    j.drawings = PlanDrawings(j, now=lambda: datetime(2026, 10, 9, 9, 30, tzinfo=timezone.utc), today=lambda: date(2026, 10, 9))
    j.team_codes.set_code("office", "office-code-drawings-browser")
    j.team_codes.set_code("engineer", "engineer-code-drawings-browser")
    srv = LiveServer(create_app(settings, j))
    yield srv, j
    srv.stop()


@pytest.mark.parametrize("width,height", [(1280, 860), (400, 820)])
def test_engineers_edit_and_office_only_views_the_drawings_for_a_job(browser, team_stack, tmp_path, width, height):
    from tests.test_team_console_browser import _sign_in_team

    srv, j = team_stack
    hidden = fresh(j)                               # no job: not for the team
    did = fresh(j, job_ref="J-3001")
    for who, code in (("office", "office-code-drawings-browser"), ("engineer", "engineer-code-drawings-browser")):
        ctx = browser.new_context(viewport={"width": width, "height": height}, accept_downloads=True)
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        try:
            _sign_in_team(page, srv.url, name="Sam Walker", code=code)
            assert page.evaluate("document.getElementById('drw-new')") is None        # no upload form for team
            page.click('.rail-item[data-pop="drawings"]')
            page.wait_for_selector(f'[data-drw-open="{did}"]', timeout=10000)
            assert page.evaluate(f"document.querySelector('[data-drw-open=\"{hidden}\"]')") is None
            page.click(f'[data-drw-open="{did}"]')
            page.wait_for_selector("#drw-editor:not([hidden]) svg.drw-svg image", timeout=10000)
            page.wait_for_timeout(200)
            no_sideways_scroll(page)
            visible = page.evaluate("""[...document.querySelectorAll('#drw-editor [data-drw], #drw-editor [data-mode]')]
                                       .filter(b => b.offsetParent !== null).map(b => b.dataset.drw || b.dataset.mode)""")
            assert "pdf" in visible and "png" in visible and "delete" not in visible and "propose" not in visible
            if who == "office":
                assert "save" not in visible and "add" not in visible and "rotr" not in visible
                assert "view only" in page.inner_text('#drw-editor [data-f="sub"]')
            else:
                assert "save" in visible and "add" in visible
                x0, y0 = dev_centre(page, 2)
                x1, y1 = plan_point(page, 0.6, 0.8)
                page.mouse.move(x0, y0)
                page.mouse.down()
                page.mouse.move(x1, y1, steps=6)
                page.mouse.up()
                save(page)
                moved = j.drawings._row(did)["content"]["devices"][2]
                assert abs(moved["x"] - 0.6) < 0.05 and abs(moved["y"] - 0.8) < 0.05 and j.drawings._row(did)["updated_by"] == "Sam Walker (engineer)"
            with page.expect_download(timeout=30000) as dl:
                page.click('#drw-editor [data-drw="pdf"]')
            assert Path(dl.value.path()).read_bytes()[:4] == b"%PDF"
            shot(page, tmp_path, f"drawings-{who}-{width}")
            assert not page.errors, page.errors
        finally:
            ctx.close()


def test_a_jarvis_draft_from_the_chat_opens_in_the_editor(browser, stack, tmp_path, monkeypatch):
    srv, j, client = stack
    did = fresh(j)

    async def structured(client_, settings, schema, *, system, prompt, **kw):
        return schema.model_validate({"readable": True, "devices": [{"type": "multi", "x": 0.4, "y": 0.4, "label": "Store"}],
                                      "notes": ["Positions are approximate"]})
    monkeypatch.setattr(pd.llm, "structured", structured)
    client.beta.messages.script += [message([tool_block("draw_on_plan", {"plan_ref": f"drawing:{did}", "kind": "devices",
                                                                         "brief": "multi-sensors"})], "tool_use"),
                                    message([text_block("Draft drawing is on the display.")])]
    ctx, page = opened(browser, srv, 1280, 860)
    try:
        page.fill("#input", "mark the detectors on the unit 4 plan")
        page.click("#btn-send")
        page.wait_for_selector("#display.open .drw-open-from-display", timeout=30000)
        assert "competent person" in page.inner_text("#display-body")
        page.click(".drw-open-from-display")
        page.wait_for_selector("#drw-editor:not([hidden]) svg.drw-svg .drw-dev", timeout=10000)
        assert device_count(page) == 1 and "draft by Jarvis" in page.inner_text('#drw-editor [data-f="heading"]')
        page.wait_for_timeout(450)                                       # the drawer has finished sliding in
        no_sideways_scroll(page)
        assert page.evaluate("document.getElementById('display').classList.contains('open')") is False
        shot(page, tmp_path, "drawings-from-chat")
        assert not page.errors, page.errors
    finally:
        ctx.close()
