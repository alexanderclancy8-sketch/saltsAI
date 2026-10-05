"""Real-browser checks of Prepare / Not now on the Approvals drawer's Suggestions section.

Headless Chrome through Playwright (skipped when it is not installed), at 1280 / 800 / 400px in both themes. A suggestion with a
Prepare handler (a quote to chase) shows Prepare and Not now; an older one keeps Do it and Not now. Prepare DRAFTS the chase and
queues it as an approval card in the same drawer - nothing is sent - and says why in plain words when it cannot. Not now hides it.
More than four are behind "Show all". Set JARVIS_SHOTS=<folder> to also save a screenshot of each state.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.sync_api")

from tests.test_console_browser import SIZES, THEMES, _no_hscroll, browser  # noqa: E402,F401
from tests.test_console_browser_phase3 import _page, _shot, serve  # noqa: E402,F401
from tests.test_console_browser_phase4a import _open_pop, _wait_cards  # noqa: E402
from tests.test_fsm_suggestions import KEY, FsmServer  # noqa: E402


def _configure(settings):
    settings.fsm_base_url = "https://fsm.example.test"
    settings.fsm_api_prefix = "/api/jarvis"
    settings.fsm_api_key = KEY


def _seed(j):
    """One older suggestion and four with a Prepare handler (Q1 can be drafted, Q4 has no email, ZZ is not a quote any more)."""
    j.db.upsert_suggestion("unbilled", "Invoice 3 completed jobs (£1,250 + VAT)?", "Finished in Salts FSM but not found in the accounts.",
                           "Draft invoices.", 1)
    for qid, title in (("Q1", "Chase quote Q1 for Acme Ltd (£1,200 + VAT)?"), ("Q4", "Chase quote Q4 for Beta Ltd (£5,000 + VAT)?"),
                       ("ZZ", "Chase quote ZZ for Gone Ltd (£90 + VAT)?"), ("Q9", "Chase quote Q9 for Niner Ltd (£40 + VAT)?")):
        j.db.upsert_suggestion(f"quote_followup:{qid}", title, "Work for them; sent 01 Sep, no reply yet.", "chase", 2, "quote_followup", "{}")


def _buttons(page, key):
    return [b.inner_text() for b in page.locator(f'#suggestions .suggestion[data-key="{key}"] .row .btn').all()]


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_prepare_and_not_now_on_a_suggestion(browser, serve, width, height, scheme):
    fsm = FsmServer()
    srv, j, _ = serve(configure=_configure, http=httpx.AsyncClient(transport=httpx.MockTransport(fsm.handler)))
    _seed(j)
    touch = {"has_touch": True, "is_mobile": True} if width < 760 else {}
    ctx, page = _page(browser, srv.url, width, height, scheme, **touch)
    try:
        # the Needs you strip points at them
        page.wait_for_function("document.querySelector('#needs-list').innerText.includes('suggestions from Jarvis')", timeout=10000)
        assert "ready to prepare" in page.inner_text("#needs-list")
        _open_pop(page, "approvals")
        page.wait_for_selector("#suggestions .suggestion")
        assert page.locator("#suggestions .suggestion").count() == 4                       # capped, like the other panels
        more = page.locator("#suggestions .sug-more")
        assert more.inner_text() == "Show all 5"
        assert _buttons(page, "unbilled") == ["Do it", "Not now"]                          # the older kind is unchanged
        assert _buttons(page, "quote_followup:Q1") == ["Prepare", "Not now"]
        if width < 760:                                                                    # 44px touch targets on a phone
            for b in page.locator("#suggestions .suggestion .btn").all() + [more.element_handle()]:
                box = b.bounding_box()
                assert box["height"] >= 43.5 and box["width"] >= 43.5, box
        page.evaluate("document.getElementById('suggestions-panel').scrollIntoView()")
        _shot(page, f"suggestions-prepare-{width}-{scheme}")

        more.click()
        assert page.locator("#suggestions .suggestion").count() == 5 and page.locator("#suggestions .sug-more").inner_text() == "Show fewer"

        # Prepare on one that cannot be drafted: a plain reason, buttons come back, nothing queued
        card = page.locator('#suggestions .suggestion[data-key="quote_followup:ZZ"]')
        card.locator('[data-sug="prepare"]').click()
        page.wait_for_function("document.querySelector('#suggestions .suggestion[data-key=\"quote_followup:ZZ\"] .sug-note').innerText.includes('no longer waiting')",
                               timeout=10000)
        assert card.locator(".btn").first.is_enabled() and j.db.pending_actions() == []
        card.scroll_into_view_if_needed()
        _shot(page, f"suggestions-prepare-failed-{width}-{scheme}")

        # Prepare on Q1: an approval card appears in the same drawer, the suggestion leaves the list, nothing was sent
        page.locator('#suggestions .suggestion[data-key="quote_followup:Q1"] [data-sug="prepare"]').click()
        _wait_cards(page, "#approvals", 1)
        page.wait_for_function("!document.querySelector('#suggestions .suggestion[data-key=\"quote_followup:Q1\"]')", timeout=10000)
        (action,) = j.db.pending_actions()
        assert action["kind"] == "email_send" and action["payload"]["to"] == ["ops@acme.example.com"] and action["status"] == "pending"
        assert j.db.get_suggestion("quote_followup:Q1")["status"] == "prepared"
        assert "Following up on quote Q1" in page.inner_text("#approvals")
        assert page.locator("#approvals .appr-card").first.locator('[data-act="approve"]').is_visible()   # waits for Approve
        assert [b for b in fsm.sent if b[0] == "PATCH"] == []                               # (Prepare in the console doesn't report to the FSM)
        page.evaluate("document.getElementById('approvals').scrollIntoView()")
        _shot(page, f"suggestions-prepared-{width}-{scheme}")

        # Not now hides it for today
        page.locator('#suggestions .suggestion[data-key="quote_followup:Q4"] [data-sug="snooze"]').click()
        page.wait_for_function("!document.querySelector('#suggestions .suggestion[data-key=\"quote_followup:Q4\"]')", timeout=10000)
        assert j.db.get_suggestion("quote_followup:Q4")["status"] == "dismissed"

        doc, body, vw = _no_hscroll(page)
        assert doc <= vw and body <= vw
        fit = page.evaluate("""() => { const d = document.getElementById('drawer'); return [d.scrollWidth <= d.offsetWidth + 1, d.offsetWidth <= innerWidth]; }""")
        assert fit == [True, True]
        assert page.errors == []
    finally:
        ctx.close()
