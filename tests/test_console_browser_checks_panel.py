"""Real-browser checks: the expanded "Scheduled checks" panel never covers the newest Jarvis reply or its Good / Wrong chips.

The bug (found testing PR #114): the panel (PR #112) is laid over the TOP of the conversation, at most 40% of it. At 360px wide
with the on-screen keyboard up (the viewport shrinks to ~55% of its height) the conversation is only ~100px tall, 40% of it plus
the row above reaches the newest reply, and the panel sat on top of the Good / Wrong chips at the bottom of the log. (Behind it a
second fault hid the chips even with the panel closed: the shrinking box made the browser fire a `scroll`, which was read as the
owner scrolling up, so the log stopped following its newest message.)

The rule now, at any size: the panel is bounded by the room above the END of the newest reply (its chips and last lines); with no
room for two check lines it is not squeezed into a slot over the reply - while the message box has the keyboard up it steps aside,
and a tap on Show with no keyboard opens a sheet (own title bar and Close, Escape, the message box still usable).

Headless Chrome through Playwright (touch-emulating for the phone sizes; skipped when it is not installed, like
test_console_browser.py, whose fixtures it shares). The keyboard is simulated the way a phone does it: open the panel, focus the
message box, then shrink the viewport; closing it grows the viewport back. Set JARVIS_SHOTS=<folder> to save a screenshot of
each state there.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from tests.fakes import FakeClient  # noqa: E402
from tests.test_console_browser import THEMES, _no_hscroll, browser  # noqa: E402,F401
from tests.test_console_browser_phase3 import LONG_REPLY, _page, _quiet_jobs, _shot, serve  # noqa: E402,F401

SHORT_REPLY = "You have 41 assets."
# (width, height, fraction of the height left once the keyboard is up - None: no keyboard)
ROOMY = [
    pytest.param(1280, 800, id="1280x800"),
    pytest.param(800, 900, id="800x900"),
    pytest.param(400, 820, id="400x820"),
    pytest.param(360, 800, id="360x800"),
]
KEYBOARD = [
    pytest.param(400, 820, id="400x820-keyboard"),
    pytest.param(360, 800, id="360x800-keyboard"),
    pytest.param(320, 640, id="320x640-keyboard"),
    pytest.param(740, 360, id="740x360-landscape-keyboard"),
]
TWO_LINES = 80   # an open panel shows at least two of the job lines (36-44px each) and scrolls for the rest

_GEOMETRY_JS = """() => {
    const box = (e) => { const r = e.getBoundingClientRect(); return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, height: r.height }; };
    const log = document.getElementById('conversation'), panel = document.getElementById('activity'), tog = document.getElementById('activity-toggle');
    const msgs = [...log.querySelectorAll(':scope > .msg')], last = msgs[msgs.length - 1], fb = last.querySelector('.fb');
    const chips = [...last.querySelectorAll('.fb button')];
    const lineH = parseFloat(getComputedStyle(last).lineHeight) || 21;
    const newest = box(last);
    // the part of the newest reply the owner must be able to read: its last three lines and the chips (all of it when it is shorter)
    const tailH = Math.min(newest.height, fb.offsetHeight + 4 + 3 * lineH), tail = { ...newest, top: newest.bottom - tailH, height: tailH };
    const vis = panel.getClientRects().length > 0;
    return {
        vw: innerWidth, vh: innerHeight, log: box(log), convo: box(document.getElementById('transcript-wrap')), panel: vis ? box(panel) : null, composer: box(document.getElementById('composer')),
        panelScrolls: panel.scrollHeight > panel.clientHeight, jobs: panel.querySelectorAll('details.auto').length, sheet: panel.classList.contains('sheet'),
        expanded: tog.getAttribute('aria-expanded'), newest, tail, logGap: log.scrollHeight - log.scrollTop - log.clientHeight,
        chips: chips.map((c) => { const b = box(c); const at = document.elementFromPoint((b.left + b.right) / 2, (b.top + b.bottom) / 2);
            return { ...b, hit: !!(at && c.contains(at)), rating: c.dataset.rating }; }),
    };
}"""


def _overlap(a, b):
    return a["left"] < b["right"] and b["left"] < a["right"] and a["top"] < b["bottom"] and b["top"] < a["bottom"]


def _setup(browser, serve, width, height, scheme, reply, touch=True):
    srv, j, _ = serve(client=FakeClient(default_text=reply))
    _quiet_jobs(j, 12, runs=11)
    for i in range(3):
        j.db.add_transcript("user" if i % 2 == 0 else "assistant", f"Earlier message number {i}")
    ctx, page = _page(browser, srv.url, width, height, scheme, **({"has_touch": True, "is_mobile": True} if touch else {}))
    page.wait_for_selector("#activity-toggle:not([hidden])")
    page.fill("#input", "How many assets does the FSM have?")
    page.keyboard.press("Enter")
    page.wait_for_selector("#conversation .msg.assistant .fb")
    page.evaluate("document.activeElement?.blur()")
    page.wait_for_timeout(300)
    return ctx, page


def _tap_toggle(page, touch=True):
    page.tap("#activity-toggle") if touch else page.click("#activity-toggle")


def _keyboard_up(page, width, height, fraction=0.55):
    page.set_viewport_size({"width": width, "height": height})
    page.wait_for_timeout(150)
    page.touchscreen.tap(width / 2, page.evaluate("document.getElementById('input').getBoundingClientRect().top + 20"))
    page.wait_for_function("document.activeElement && document.activeElement.id === 'input'")
    page.set_viewport_size({"width": width, "height": int(height * fraction)})
    page.wait_for_function("document.body.classList.contains('composing')")
    page.wait_for_timeout(300)


def _keyboard_down(page, width, height):
    page.evaluate("document.activeElement?.blur()")
    page.set_viewport_size({"width": width, "height": height})
    page.wait_for_function("!document.body.classList.contains('composing')")
    page.wait_for_timeout(350)


def _assert_chips_clear(page, where, panel_may_show=True):
    g = page.evaluate(_GEOMETRY_JS)
    assert [c["rating"] for c in g["chips"]] == ["good", "wrong"], where
    for c in g["chips"]:
        assert c["top"] >= 0 and c["bottom"] <= g["vh"] and c["left"] >= 0 and c["right"] <= g["vw"], (where, "chip off screen", c, g["vh"])
        assert c["bottom"] <= g["composer"]["top"] + 1, (where, "chip under the message box", c, g["composer"])
        assert c["top"] >= g["log"]["top"] - 1 and c["bottom"] <= g["log"]["bottom"] + 1, (where, "chip outside the log", c, g["log"])
        assert c["hit"], (where, "a finger on the chip would hit something else", c)
        assert not g["panel"] or not _overlap(c, g["panel"]), (where, "the panel covers a chip", c, g["panel"])
    assert g["logGap"] <= 2, (where, "the log is not on its newest message", g["logGap"])
    return g


@pytest.mark.parametrize("reply", [LONG_REPLY, SHORT_REPLY], ids=["long-reply", "short-reply"])
@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", ROOMY)
def test_with_room_the_open_panel_lies_over_the_older_messages_only(browser, serve, width, height, scheme, reply):
    """Overlay: the end of the newest reply (its last three lines and chips, all of a short one) is never under the panel."""
    touch = width <= 760
    ctx, page = _setup(browser, serve, width, height, scheme, reply, touch)
    name = f"checks-panel-{width}x{height}-{'long' if reply is LONG_REPLY else 'short'}-{scheme}"
    try:
        _tap_toggle(page, touch)
        page.wait_for_selector("#activity", state="visible")
        page.wait_for_timeout(300)
        _shot(page, name)
        g = _assert_chips_clear(page, name)
        assert g["jobs"] == 12 and g["panel"] and not g["sheet"], (name, "an overlay, not a sheet, when there is room", g["sheet"])
        assert not _overlap(g["tail"], g["panel"]), (name, "the panel covers the end of the newest reply", g["tail"], g["panel"])
        assert g["panel"]["height"] >= TWO_LINES and g["panelScrolls"], (name, g["panel"])
        assert g["panel"]["height"] <= g["convo"]["height"] * 0.4 + 2 or g["panel"]["height"] <= 110, (name, g["panel"], g["convo"])
        doc, body, vw = _no_hscroll(page)
        assert doc <= vw and body <= vw, (doc, body, vw)
        assert not page.errors, page.errors
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", KEYBOARD)
def test_with_the_keyboard_up_the_open_panel_never_covers_the_chips_and_comes_back(browser, serve, width, height, scheme):
    """The reported case: panel open, then the message box is focused and the viewport shrinks to 55%."""
    ctx, page = _setup(browser, serve, width, height, scheme, LONG_REPLY)
    name = f"checks-panel-{width}x{height}-keyboard-{scheme}"
    try:
        _tap_toggle(page)
        page.wait_for_selector("#activity", state="visible")
        _keyboard_up(page, width, height)
        _shot(page, name)
        g = page.evaluate(_GEOMETRY_JS)
        assert g["expanded"] == "true"                       # what the owner asked for is remembered ...
        assert g["panel"] is None, (name, "... but with no room for two lines the panel steps aside for the keyboard", g["panel"])
        if width <= 400 and height >= 640:                    # (the 740x360 landscape layout has no conversation room even without the panel)
            g = _assert_chips_clear(page, name)
        # the keyboard closes: the viewport grows back and the panel is back, clear of the chips
        _keyboard_down(page, width, height)
        g = _assert_chips_clear(page, name + "-after")
        assert g["panel"] and g["jobs"] == 12 and g["expanded"] == "true", (name, g["panel"])
        assert not _overlap(g["tail"], g["panel"]), (name, "back over the end of the reply", g["tail"], g["panel"])
        assert not page.errors, page.errors
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height,touch", [(360, 568, True), (320, 568, True), (740, 360, True)], ids=["360x568", "320x568", "740x360-landscape"])
def test_with_no_room_a_tap_on_show_opens_a_sheet_with_a_close(browser, serve, width, height, touch, scheme):
    """No keyboard, but a short screen: two lines will not fit above the reply, so Show opens a sheet instead of a slot."""
    ctx, page = _setup(browser, serve, width, height, scheme, LONG_REPLY, touch)
    name = f"checks-sheet-{width}x{height}-{scheme}"
    try:
        # (a short landscape phone lays its message box over the summary row - a long-standing fault of that layout, not of the
        # panel - so the row is activated the way a keyboard does it)
        page.evaluate("document.getElementById('activity-toggle').click()")
        page.wait_for_selector("#activity", state="visible")
        page.wait_for_timeout(300)
        _shot(page, name)
        g = page.evaluate(_GEOMETRY_JS)
        assert g["sheet"] and g["panel"], (name, "a sheet when there is no room", g["sheet"])
        p = g["panel"]
        assert p["top"] >= 0 and p["left"] >= 0 and p["right"] <= g["vw"] and p["bottom"] <= g["composer"]["top"] + 1, (name, "the sheet fits above the message box", p, g["composer"])
        assert p["height"] >= TWO_LINES * 2 or p["height"] >= g["composer"]["top"] - 2, (name, "room for a useful list", p)
        assert page.get_attribute("#activity", "role") == "dialog"
        close = page.locator("#activity-close").bounding_box()
        assert close and close["y"] >= 0 and close["y"] + close["height"] <= g["vh"] and close["height"] >= 44, (name, close)
        assert page.evaluate("document.activeElement.id") == "activity-close"       # focus moves into it
        assert g["panelScrolls"] and g["jobs"] == 12
        assert page.locator("details.auto summary").first.bounding_box()["height"] >= 44
        doc, body, vw = _no_hscroll(page)
        assert doc <= vw and body <= vw, (doc, body, vw)
        # Escape closes it and hands focus back to the row; Close does the same
        page.keyboard.press("Escape")
        page.wait_for_function("document.getElementById('activity').hidden")
        assert page.get_attribute("#activity-toggle", "aria-expanded") == "false" and page.evaluate("document.activeElement.id") == "activity-toggle"
        assert not page.evaluate("document.body.classList.contains('checks-sheet')")
        page.evaluate("document.getElementById('activity-toggle').click()")
        page.wait_for_selector("#activity", state="visible")
        page.evaluate("document.getElementById('activity-close').click()")
        page.wait_for_function("document.getElementById('activity').hidden")
        assert page.get_attribute("#activity-toggle", "aria-expanded") == "false"
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_a_resize_alone_never_throws_a_sheet_over_the_screen(browser, serve):
    """Nothing asked for and no room: the panel closes (Show opens it as a sheet); no sheet pops up on its own."""
    ctx, page = _setup(browser, serve, 360, 800, "dark", LONG_REPLY)
    try:
        _tap_toggle(page)
        page.wait_for_selector("#activity", state="visible")
        assert not page.evaluate("document.getElementById('activity').classList.contains('sheet')")
        page.set_viewport_size({"width": 360, "height": 440})            # e.g. a rotation or a split screen, no keyboard
        page.wait_for_timeout(400)
        assert page.evaluate("document.getElementById('activity').hidden")
        assert page.get_attribute("#activity-toggle", "aria-expanded") == "false"
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_the_log_keeps_following_its_newest_message_when_the_keyboard_takes_its_room(browser, serve):
    """The second fault: a shrinking box fires `scroll`, which read as 'the owner scrolled up' and un-pinned the log."""
    ctx, page = _setup(browser, serve, 360, 800, "dark", LONG_REPLY)
    try:
        _keyboard_up(page, 360, 800)
        g = _assert_chips_clear(page, "no panel at all")
        assert g["panel"] is None
        # ... while a real scroll up to read is still left alone
        page.evaluate("document.getElementById('conversation').scrollTop = 0")
        page.wait_for_timeout(200)
        page.set_viewport_size({"width": 360, "height": 420})
        page.wait_for_timeout(300)
        assert page.evaluate("document.getElementById('conversation').scrollTop") < 40
        assert not page.errors, page.errors
    finally:
        ctx.close()
