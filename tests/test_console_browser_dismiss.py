"""Real-browser checks of Dismiss on failed actions in the Approvals inbox.

Headless Chrome through Playwright (skipped when it is not installed), at 1280 / 800 / 400px in both themes. A failed card
has Retry and Dismiss; the top of the failed list has "Dismiss all failed" with a confirm step that says how many will be
hidden. Dismissing hides the action from the failed list, the rail count, the "Needs you" strip and the chat cards, runs
nothing, and leaves it in the history (listed in the pop-up, flagged with who and when). Set JARVIS_SHOTS=<folder> to also
save a screenshot of each surface there.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from tests.test_console_browser import SIZES, THEMES, _no_hscroll, browser  # noqa: E402,F401
from tests.test_console_browser_phase3 import _page, _shot, serve  # noqa: E402,F401
from tests.test_console_browser_phase4a import SECRET, _open_pop, _seed, _wait_cards  # noqa: E402


def _more_failed(j, n):
    """Stale failures like the owner's: removing things that were never in the register."""
    ids = []
    for i in range(n):
        a = j.db.create_action("fsm_write", f"Remove van 'AB{i} CDE'", {"method": "POST", "path": "/vehicles/remove", "body": {"reg": f"AB{i}"}})
        j.db.set_action_status(a, "failed", f"Salts FSM did not accept the change (HTTP 404): no such vehicle ({SECRET})")
        ids.append(a)
    return ids


def _rail(page):
    return page.eval_on_selector('.rail-item[data-pop="approvals"]', "e => [e.querySelector('.rail-count').textContent, e.dataset.level]")


def _needs(page):
    return page.inner_text("#needs-list")


def _touch(width):
    return {"has_touch": True, "is_mobile": True} if width < 760 else {}


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_dismiss_one_and_dismiss_all_failed(serve, browser, width, height, scheme):
    srv, j, _ = serve()
    ids = _seed(j)                                                  # 2 pending + 2 failed
    extra = _more_failed(j, 2)
    failed = ids["failed"] + extra
    ctx, page = _page(browser, srv.url, width, height, scheme, **_touch(width))
    try:
        assert _rail(page) == ["6", "bad"] and "4 actions failed" in _needs(page)
        _open_pop(page, "approvals")
        _wait_cards(page, "#failed-actions", 4)
        # every failed card has Retry and Dismiss, side by side; no Approve; the bulk button sits above the list
        first = page.locator(f'#failed-actions .appr-card[data-id="{failed[0]}"]')
        assert [b.inner_text() for b in first.locator(":scope > .row .btn").all()] == ["Retry", "Dismiss"]
        assert first.locator('[data-act="approve"]').count() == 0
        bulk = page.locator("#dismiss-all")
        assert bulk.is_visible() and bulk.inner_text() == "Dismiss all failed" and page.locator("#dismiss-confirm").is_hidden()
        assert page.evaluate("""() => document.getElementById('dismiss-all').getBoundingClientRect().bottom
            <= document.querySelector('#failed-actions .appr-card').getBoundingClientRect().top""")
        if width < 760:                                             # 44px touch targets on a phone
            for sel in ('#failed-actions .appr-card [data-act="dismiss"]', '#failed-actions .appr-card [data-act="retry"]', "#dismiss-all"):
                box = page.locator(sel).first.bounding_box()
                assert box["height"] >= 43.5 and box["width"] >= 43.5, (sel, box)
        assert page.locator("#history-panel").is_hidden()           # nothing dismissed yet
        first.scroll_into_view_if_needed()
        page.evaluate("document.getElementById('failed-panel').scrollIntoView()")
        _shot(page, f"dismiss-failed-list-{width}-{scheme}")

        # Dismiss one: it leaves the list, the counts and Needs you shrink, and NOTHING ran
        first.locator('[data-act="dismiss"]').click()
        page.wait_for_function(f"!document.querySelector('#failed-actions .appr-card[data-id=\"{failed[0]}\"]')", timeout=10000)
        row = j.db.get_action(failed[0])
        assert row["status"] == "failed" and row["dismissed_by"] == "the owner" and row["dismissed_at"] and row["superseded_by"] is None
        assert j.actions.fsm.writes == [] and j.actions.mail.sent == [] and len(j.db.pending_actions()) == 2
        assert page.inner_text("#failed-count") == "3"
        page.keyboard.press("Escape")
        assert _rail(page) == ["5", "bad"] and "3 actions failed" in _needs(page)
        _open_pop(page, "approvals")

        # Dismiss all failed: asks first, says how many, Cancel changes nothing
        page.click("#dismiss-all")
        confirm = page.locator("#dismiss-confirm")
        assert confirm.is_visible() and "Hide 3 failed actions" in confirm.inner_text() and "Nothing will run" in confirm.inner_text()
        assert page.locator("#dismiss-all-yes").inner_text() == "Dismiss 3"
        if width < 760:
            for sel in ("#dismiss-all-yes", "#dismiss-all-no"):
                assert page.locator(sel).bounding_box()["height"] >= 43.5, sel
        _shot(page, f"dismiss-failed-confirm-{width}-{scheme}")
        page.click("#dismiss-all-no")
        assert confirm.is_hidden() and page.locator("#failed-actions .appr-card").count() == 3
        assert all(not j.db.get_action(a)["dismissed_at"] for a in failed[1:])
        page.click("#dismiss-all")
        page.click("#dismiss-all-yes")
        page.wait_for_function("document.querySelectorAll('#failed-actions .appr-card').length === 0", timeout=10000)
        assert page.locator("#failed-panel").is_hidden() and page.locator("#dismiss-confirm").is_hidden()
        assert all(j.db.get_action(a)["dismissed_at"] and j.db.get_action(a)["status"] == "failed" for a in failed)
        assert j.actions.fsm.writes == [] and len(j.db.pending_actions()) == 2          # still nothing ran or was queued
        assert page.locator("#approvals .appr-card").count() == 2                       # the waiting ones are untouched
        # the history: a collapsed list under the inbox, each entry flagged with who and when
        history = page.locator("#history-panel")
        assert history.is_visible() and page.inner_text("#history-count") == "4"
        history.locator("summary").click()
        page.wait_for_selector("#history-list .appr-hist-item", timeout=10000)
        items = page.locator("#history-list .appr-hist-item")
        assert items.count() == 4
        text = items.first.inner_text()
        assert "Dismissed by the owner at " in text and "HTTP 404" in text and SECRET not in page.content()
        page.locator("#history-panel").scroll_into_view_if_needed()
        _shot(page, f"dismiss-history-{width}-{scheme}")
        assert _no_hscroll(page)[0] <= width
        fit = page.evaluate("""() => { const d = document.getElementById('drawer'); return [d.scrollWidth <= d.offsetWidth + 1, d.offsetWidth <= innerWidth]; }""")
        assert fit == [True, True]
        # ...and the rail and Needs you only count what is waiting for you now
        page.keyboard.press("Escape")
        assert _rail(page) == ["2", "warn"] and "failed" not in _needs(page)
        assert page.errors == []
    finally:
        ctx.close()


def test_dismiss_all_with_nothing_to_dismiss_is_hidden_and_a_single_failure_reads_naturally(serve, browser):
    srv, j, _ = serve()
    (only,) = _more_failed(j, 1)
    ctx, page = _page(browser, srv.url, 1280, 800)
    try:
        _open_pop(page, "approvals")
        _wait_cards(page, "#failed-actions", 1)
        page.click("#dismiss-all")
        assert "Hide 1 failed action from this list" in page.inner_text("#dismiss-confirm")
        assert page.inner_text("#dismiss-all-yes") == "Dismiss it"
        page.click("#dismiss-all-yes")
        page.wait_for_function("document.querySelectorAll('#failed-actions .appr-card').length === 0", timeout=10000)
        assert page.locator("#dismiss-all").is_hidden() and page.locator("#failed-panel").is_hidden()
        assert page.locator("#approvals-empty").is_visible() and j.db.get_action(only)["dismissed_at"]
        assert _rail(page) == ["0", ""] and "failed" not in _needs(page)
    finally:
        ctx.close()


def test_a_failed_chat_card_can_be_dismissed_and_leaves_the_conversation(serve, browser):
    srv, j, _ = serve()
    ids = _seed(j, history=True)
    ctx, page = _page(browser, srv.url, 1280, 800)
    try:
        page.wait_for_selector("#conversation .appr-card", timeout=10000)
        card = page.locator(f'#conversation .appr-card[data-id="{ids["fsm"]}"]')
        card.locator('[data-act="approve"]').click()                                   # the (fake) FSM refuses it: it fails
        page.wait_for_selector(f'#conversation .appr-card[data-id="{ids["fsm"]}"][data-status="failed"]', timeout=10000)
        assert [b.inner_text() for b in card.locator(":scope > .row .btn").all()] == ["Retry", "Dismiss"]
        _shot(page, "dismiss-chat-card-failed")
        card.locator('[data-act="dismiss"]').click()
        page.wait_for_function(f"!document.querySelector('#conversation .appr-msg[data-id=\"{ids['fsm']}\"]')", timeout=10000)
        assert j.db.get_action(ids["fsm"])["dismissed_at"] and len(j.actions.fsm.writes) == 1   # the one approved run, nothing more
        assert page.locator(f'#failed-actions .appr-card[data-id="{ids["fsm"]}"]').count() == 0
        assert page.locator(f'#conversation .appr-card[data-id="{ids["email"]}"]').count() == 1   # the other waiting card is still there
        assert page.errors == []
    finally:
        ctx.close()


def test_a_dismissal_made_elsewhere_refreshes_this_console_live(serve, browser):
    srv, j, _ = serve()
    ids = _seed(j)
    ctx, page = _page(browser, srv.url, 1280, 800)
    try:
        _open_pop(page, "approvals")
        _wait_cards(page, "#failed-actions", 2)
        target = ids["failed"][0]
        status = page.evaluate(f"fetch('/api/approvals/{target}/dismiss', {{ method: 'POST' }}).then(r => r.status)")   # as another tab would
        assert status == 200
        page.wait_for_function(f"!document.querySelector('#failed-actions .appr-card[data-id=\"{target}\"]')", timeout=10000)
        assert page.locator("#failed-actions .appr-card").count() == 1
        # a dismissal of something that is not failed is refused, and the card is unharmed
        status = page.evaluate(f"fetch('/api/approvals/{ids['email']}/dismiss', {{ method: 'POST' }}).then(r => r.status)")
        assert status == 409 and page.locator(f'#approvals .appr-card[data-id="{ids["email"]}"]').count() == 1
    finally:
        ctx.close()
