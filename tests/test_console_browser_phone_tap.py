"""Real-browser checks: on a phone, a tap on anything above the message box lands even while the box has the keyboard up.

The bug (found by an earlier agent): at about 400px the message box adds `body.composing` on focus, which hides the large core
(`.composing .core-block { display: none }`) so the conversation has room above the on-screen keyboard. Tapping a control above the
box blurs the box at the press, and the blur used to remove `composing` immediately, so the core came back and everything under it
(the "Scheduled checks" row, the conversation, its Good / Wrong chips) moved down BETWEEN the press and the release. A browser click
needs the press and the release on the same element, so the tap was swallowed: nothing happened and the owner had to tap twice.

Headless Chrome through Playwright with a touch-emulating context (has_touch + is_mobile; skipped when it is not installed, like
test_console_browser.py, whose fixtures it shares). Taps are REAL touch events (page.touchscreen.tap and CDP touchStart/touchEnd
with a hold), so Chrome synthesises the compat mouse events exactly as on a phone; the on-screen keyboard is simulated by
shrinking the viewport after the box has focus, which is what a phone does. At 360 / 400 / 430 wide, in both themes, with the
"Scheduled checks" panel closed and open: the summary row, the drawer rail chips, Good and Wrong each activate exactly once; the
Needs-you chips (hidden by design while the box is up) work the moment the box lets go and the core is never left hidden.

Set JARVIS_SHOTS=<folder> to also save a screenshot of each state there.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from tests.test_console_browser import THEMES, browser  # noqa: E402,F401
from tests.test_console_browser_phase3 import _page, _quiet_jobs, _shot, serve  # noqa: E402,F401

WIDTHS = [(360, 740), (400, 800), (430, 860)]
CONTROLS = {
    "summary": "#activity-toggle",
    "rail": '.rail-item[data-pop="activity"]',
    "good": ".fb button[data-rating=good]",
    "wrong": ".fb button[data-rating=wrong]",
    "need": ".need",
}
_COUNT_JS = """(controls) => {
    window.__clicks = Object.fromEntries(Object.keys(controls).map(k => [k, 0]));
    document.addEventListener('click', e => {
        for (const [k, sel] of Object.entries(controls)) if (e.target.closest && e.target.closest(sel)) window.__clicks[k]++;
    }, true);
}"""
# where is the control, and is it really what a finger there would hit (not hidden, not covered, above the message box)?
_LOOK_JS = """([sel, first]) => {
    const els = [...document.querySelectorAll(sel)].filter(e => e.getClientRects().length)
        .filter(e => { const r = e.getBoundingClientRect(); return r.left + r.width / 2 >= 0 && r.left + r.width / 2 <= innerWidth; });
    const el = first ? els[0] : els[els.length - 1]; if (!el) return null;
    const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
    const hit = document.elementFromPoint(x, y);
    return { x, y, hit: !!(hit && el.contains(hit)), top: r.top, bottom: r.bottom, vh: innerHeight,
             composer: document.getElementById('composer').getBoundingClientRect().top };
}"""
_COMPOSING = "document.body.classList.contains('composing')"


def _prepare(page, panel_open=False):
    page.evaluate(_COUNT_JS, CONTROLS)
    page.wait_for_selector("#activity-toggle:not([hidden])")
    if panel_open:
        page.tap("#activity-toggle")
        page.wait_for_selector("#activity", state="visible")
        page.evaluate("window.__clicks.summary = 0")


def _keyboard_up(page, width, height):
    """Focus the message box (a real tap), then let the 'keyboard' take the lower ~40% of the screen, like a phone does."""
    page.set_viewport_size({"width": width, "height": height})
    page.wait_for_timeout(150)
    page.touchscreen.tap(width / 2, page.evaluate("document.getElementById('input').getBoundingClientRect().top + 20"))
    page.wait_for_function("document.activeElement && document.activeElement.id === 'input'")
    page.set_viewport_size({"width": width, "height": int(height * 0.6)})
    page.wait_for_function(_COMPOSING)
    page.wait_for_timeout(250)
    assert page.evaluate("getComputedStyle(document.querySelector('.core-block')).display") == "none"   # the intended behaviour is intact


def _send(page, text, n):
    page.fill("#input", text)
    page.keyboard.press("Enter")
    page.wait_for_function(f"document.querySelectorAll('#conversation .msg.assistant .fb').length >= {n}", timeout=10000)


def _tap(page, key, hold_ms=0, mouse=False):
    """Press the control the way a finger (or, for `mouse`, a pointer) would; returns (clicks before, clicks after)."""
    look = page.evaluate(_LOOK_JS, [CONTROLS[key], key == "need"])
    assert look, f"{key} is not on screen"
    assert look["hit"], f"{key} is covered by something else at its centre: {look}"
    assert 0 <= look["top"] and look["bottom"] <= look["composer"] and look["bottom"] <= look["vh"], (key, look)   # above the box
    before = page.evaluate(f"window.__clicks['{key}']")
    x, y = look["x"], look["y"]
    if mouse:
        page.mouse.move(x, y)
        page.mouse.down()
        page.wait_for_timeout(hold_ms)
        page.mouse.up()
    elif hold_ms:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        page.wait_for_timeout(hold_ms)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        cdp.detach()
    else:
        page.touchscreen.tap(x, y)
    page.wait_for_timeout(250)
    return before, page.evaluate(f"window.__clicks['{key}']")


def _settled(page):
    """Once the press is over the core must be back: nothing is left collapsed."""
    page.wait_for_function(f"!({_COMPOSING})", timeout=3000)


def _close_drawer(page):
    page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
    page.wait_for_timeout(350)
    page.tap("#drawer-close")
    page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
    page.wait_for_timeout(350)


@pytest.mark.parametrize("panel_open", [False, True], ids=["checks-closed", "checks-open"])
@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", WIDTHS)
def test_a_tap_above_the_message_box_lands_while_the_keyboard_is_up(serve, browser, width, height, scheme, panel_open):
    srv, j, _ = serve()
    _quiet_jobs(j, n=9)
    j.db.create_action("note", "Send the Kestrel quote", {})      # a Needs-you chip and an Approvals badge
    ctx, page = _page(browser, srv.url, width, height, scheme, has_touch=True, is_mobile=True)
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/api/feedback") else None)
    try:
        _prepare(page, panel_open)
        _send(page, "How are things?", 1)
        _keyboard_up(page, width, height)
        _shot(page, f"phone-tap-{width}-{scheme}-{'open' if panel_open else 'closed'}")

        # the drawer rail chip: one tap opens it
        assert _tap(page, "rail") == (0, 1)
        _close_drawer(page)
        _settled(page)

        # Good under the reply: one tap records ONE verdict
        _keyboard_up(page, width, height)
        assert _tap(page, "good") == (0, 1)
        page.wait_for_selector("#conversation .msg.assistant .fb-done")
        assert page.inner_text("#conversation .msg.assistant .fb-done") == "Marked good"
        assert len(posts) == 1, posts
        _settled(page)

        # Wrong under a second reply (the tap above blurred the box: focus it again, send, tap Wrong)
        _keyboard_up(page, width, height)
        _send(page, "And now?", 2)
        assert _tap(page, "wrong") == (0, 1)
        page.wait_for_selector("#conversation .fb-note")
        assert len(posts) == 2, posts
        _settled(page)

        # the summary row opens (or, with the panel already open, closes) the checks - once
        _keyboard_up(page, width, height)
        assert _tap(page, "summary") == (0, 1)
        page.wait_for_function(f"document.getElementById('activity-toggle').getAttribute('aria-expanded') === '{'false' if panel_open else 'true'}'")
        _settled(page)

        # Needs you: hidden while the keyboard is up (by design); the moment the box lets go of focus it is one tap away
        _keyboard_up(page, width, height)
        assert page.evaluate("!document.querySelector('.need').getClientRects().length")
        page.evaluate("document.getElementById('input').blur()")
        assert not page.evaluate(_COMPOSING)             # a blur that no press caused un-collapses at once, as before
        page.wait_for_timeout(250)
        assert _tap(page, "need") == (0, 1)
        page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
        assert not page.errors, page.errors
    finally:
        ctx.close()


@pytest.mark.parametrize("hold_ms", [1, 160, 700])
def test_a_slow_touch_press_above_the_message_box_still_clicks(serve, browser, hold_ms):
    """Down and up are separate touch events with a gap (a finger resting a moment): the press must not be broken in between."""
    srv, j, _ = serve()
    _quiet_jobs(j, n=9)
    ctx, page = _page(browser, srv.url, 400, 800, "dark", has_touch=True, is_mobile=True)
    try:
        _prepare(page)
        _keyboard_up(page, 400, 800)
        assert _tap(page, "summary", hold_ms=hold_ms) == (0, 1)
        assert page.get_attribute("#activity-toggle", "aria-expanded") == "true"
        _settled(page)
    finally:
        ctx.close()


@pytest.mark.parametrize("key", ["summary", "rail"])
def test_a_mouse_press_in_a_narrow_window_clicks_too(serve, browser, key):
    """The same layout applies to a narrow desktop window (no touch): mousedown blurs the box, mouseup must still land."""
    srv, j, _ = serve()
    _quiet_jobs(j, n=9)
    ctx, page = _page(browser, srv.url, 400, 800, "dark")
    try:
        _prepare(page)
        page.click("#input")
        page.wait_for_function(_COMPOSING)
        assert _tap(page, key, hold_ms=120, mouse=True) == (0, 1)
        _settled(page)
    finally:
        ctx.close()


_PRESS_THEN_BLUR = "document.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true})); document.getElementById('input').blur();"


def test_the_core_is_never_left_hidden_when_a_press_never_becomes_a_click(serve, browser):
    """A finger that lands above the box and is dragged away fires no click: the core must still come back (safety timer)."""
    srv, j, _ = serve()
    ctx, page = _page(browser, srv.url, 400, 800, "dark", has_touch=True, is_mobile=True)
    try:
        page.tap("#input")
        page.wait_for_function(_COMPOSING)
        page.evaluate(_PRESS_THEN_BLUR)
        assert page.evaluate(_COMPOSING)                 # held while the press is in progress...
        page.wait_for_function(f"!({_COMPOSING})", timeout=4000)   # ...but only for a moment
        # a cancelled press (the browser took it for a scroll) releases at once
        page.tap("#input")
        page.wait_for_function(_COMPOSING)
        page.evaluate(_PRESS_THEN_BLUR)
        page.evaluate("document.dispatchEvent(new PointerEvent('pointercancel', {bubbles: true}))")
        page.wait_for_function(f"!({_COMPOSING})", timeout=1000)
    finally:
        ctx.close()


def test_refocusing_the_box_during_a_press_keeps_it_collapsed(serve, browser):
    srv, j, _ = serve()
    ctx, page = _page(browser, srv.url, 400, 800, "dark", has_touch=True, is_mobile=True)
    try:
        page.tap("#input")
        page.wait_for_function(_COMPOSING)
        page.evaluate(_PRESS_THEN_BLUR + "document.getElementById('input').focus();")
        page.wait_for_timeout(1600)
        assert page.evaluate(_COMPOSING)                 # the pending release must not un-collapse a box that is focused again
    finally:
        ctx.close()


# ------------------------------------------------------------------------------------- desktop: no behaviour change
@pytest.mark.parametrize("scheme", THEMES)
def test_desktop_layout_is_untouched_by_focusing_and_clicking_around_the_message_box(serve, browser, scheme):
    srv, j, _ = serve()
    _quiet_jobs(j, n=9)
    ctx, page = _page(browser, srv.url, 1280, 800, scheme)
    try:
        _prepare(page)
        positions = """() => Object.fromEntries(['#activity-toggle', '.core-block', '#composer', '#core-btn'].map(s => {
            const r = document.querySelector(s).getBoundingClientRect(); return [s, [r.left, r.top, r.width, r.height]]; }))"""
        before = page.evaluate(positions)
        page.click("#input")
        assert page.evaluate(_COMPOSING)
        assert page.evaluate("getComputedStyle(document.querySelector('.core-block')).display") != "none"   # the core never collapses on desktop
        assert page.evaluate(positions) == before                                                            # and nothing moves
        # a click elsewhere activates once, the class goes as soon as the click is done, and nothing moves
        assert _tap(page, "summary", mouse=True) == (0, 1)
        page.wait_for_function(f"!({_COMPOSING})", timeout=2000)
        assert page.evaluate(positions)["#core-btn"] == before["#core-btn"]
        # a blur with no press un-collapses synchronously, exactly as before
        page.click("#input")
        assert page.evaluate(f"(() => {{ document.getElementById('input').blur(); return {_COMPOSING}; }})()") is False
        assert not page.errors, page.errors
    finally:
        ctx.close()
