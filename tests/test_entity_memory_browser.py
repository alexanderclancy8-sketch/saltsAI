"""Real-browser checks of the Memory pop-up's "Customers & sites" tab (jarvis/web/entity_notes.js, services/entity_memory.py).

Headless Chrome through Playwright (skipped when it is not installed, like the other console browser tests), at 1280 / 800 / 400px
in both themes: the tab lists the customers and sites with notes and how many suggestions wait; one opens to its summary, the
suggested notes (flagged when they followed an email) with Accept / Discard, an Add note field and the notes with Edit / Delete;
everything fits the drawer (full-screen on a phone) with 44px targets, and every change really changes what Jarvis keeps.

Set JARVIS_SHOTS=<folder> to also save a screenshot of each surface there.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("playwright.sync_api")

from jarvis import access  # noqa: E402
from jarvis.services import entity_memory as em  # noqa: E402
from tests.test_console_browser import SIZES, THEMES, _no_hscroll, browser  # noqa: E402,F401
from tests.test_console_browser_phase3 import _page, _shot, serve  # noqa: E402,F401
from tests.test_entity_memory import FakeFsm  # noqa: E402

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
FLAG = "From an email/document - check it (this was suggested after reading an email or attachment)."


class ConsoleFsm(FakeFsm):
    """FakeFsm's customers and sites; everything else the console asks for (jobs, quotes...) from the demo FSM."""

    def __init__(self):
        super().__init__()
        from jarvis.integrations.fsm import DemoFSM

        self._rest = DemoFSM(now=datetime(2026, 10, 8, 10, 0))

    async def jobs(self, *a, **k):
        return await self._rest.jobs(*a, **k)

    def __getattr__(self, name):
        return getattr(self._rest, name)


def _seed(j, *, with_acme=True):
    j.fsm = ConsoleFsm()
    m = j.entity_memory
    m._now = lambda: NOW
    if with_acme:
        acme = m._ensure_entity("customer", "C1", "Acme Alarms Ltd")
        j.db.execute("UPDATE entity_notes SET summary = ? WHERE id = ?",
                     ("Ring the office before sending anyone; they pay at the end of the month.", acme["id"]))
        for text in ("Wants a call before an engineer is sent", "Prefers morning visits", "Car park is pay and display"):
            m._insert(acme["id"], text=text, status=em.ACTIVE, source=em.SRC_OWNER, by="Alex", role=access.OWNER)
        m._insert(acme["id"], text="Invoices to go to the new accounts address", status=em.PENDING, source=em.SRC_PROPOSAL,
                  by="Jarvis", role="jarvis", flag=FLAG)
    kestrel = m._ensure_entity("customer", "C3", "Kestrel Retail")
    m._insert(kestrel["id"], text="Unit manager prefers emails after 4pm", status=em.ACTIVE, source=em.SRC_MANAGER, by="Pat",
              role=access.MANAGER)


def _open_tab(page, width):
    tap = width < 760
    page.tap("#btn-settings") if tap else page.click("#btn-settings")
    page.wait_for_timeout(350)
    page.tap("#btn-open-memory") if tap else page.click("#btn-open-memory")
    page.wait_for_selector("#memory-notes .mem-item, #memory-notes .empty", timeout=10000)
    page.tap("#mem-tab-entities") if tap else page.click("#mem-tab-entities")
    page.wait_for_selector("#ent-list .ent-row", timeout=10000)


def _fits(page, width):
    assert _no_hscroll(page)[0] <= width
    fit = page.evaluate("() => { const d = document.getElementById('drawer'); return [d.scrollWidth <= d.offsetWidth + 1, d.offsetWidth <= innerWidth]; }")
    assert fit == [True, True]


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_customers_and_sites_tab_lists_and_opens_notes(serve, browser, width, height, scheme):
    srv, j, _ = serve()
    _seed(j)
    touch = {"has_touch": True, "is_mobile": True} if width < 760 else {}
    ctx, page = _page(browser, srv.url, width, height, scheme, **touch)
    try:
        _open_tab(page, width)
        assert page.inner_text("#drawer-title") == "Memory"
        assert page.get_attribute("#mem-tab-entities", "aria-selected") == "true" and page.is_hidden("#mem-panel-general")
        assert page.inner_text("#ent-pending-count") == "1"
        rows = page.locator("#ent-list .ent-row")
        assert rows.count() == 2
        names = [r.inner_text() for r in rows.all()]
        assert any("Acme Alarms Ltd" in n and "1 waiting" in n and "3 notes" in n for n in names)
        assert any("Kestrel Retail" in n and "1 note" in n for n in names)
        assert page.is_hidden("#ent-demo")
        _fits(page, width)
        _shot(page, f"ent-list-{width}-{scheme}")
        page.locator('#ent-list .ent-row[data-id="C1"]').click()
        page.wait_for_selector("#ent-view .ent-title")
        view = page.inner_text("#ent-view")
        assert "Acme Alarms Ltd" in page.inner_text("#ent-view .ent-title") and "Customer · Salts FSM C1" in view
        assert "Ring the office before sending anyone" in view
        sug = page.locator("#ent-view .ent-suggestion")
        assert sug.count() == 1 and "From an email/document - check it" in sug.inner_text()
        assert sug.locator('[data-ent="accept"]').is_visible() and sug.locator('[data-ent="discard"]').is_visible()
        assert page.locator("#ent-notes .ent-item").count() == 3 and "3/40" in view
        for item in page.locator("#ent-notes .ent-item").all():
            assert item.locator('[data-ent="edit"]').is_visible() and item.locator('[data-ent="ask-delete"]').is_visible()
        assert page.is_visible('[data-ent="ask-forget"]')                       # the owner's console
        _fits(page, width)
        if width < 760:   # 44px targets on a phone
            small = page.evaluate("""() => [...document.querySelectorAll('#pop-memory .mem-tab, #ent-view .btn, #ent-view textarea')]
                .filter(e => e.offsetParent && e.getBoundingClientRect().height < 44).map(e => e.outerHTML.slice(0, 80))""")
            assert small == []
            assert page.evaluate("document.getElementById('drawer').offsetWidth") == width   # full-screen drawer
        _shot(page, f"ent-view-{width}-{scheme}")
        page.locator("#ent-view .ent-suggestion").scroll_into_view_if_needed()
        _shot(page, f"ent-view-suggestion-{width}-{scheme}")
        page.locator("#ent-view .ent-forget").scroll_into_view_if_needed()
        _shot(page, f"ent-view-notes-{width}-{scheme}")
        assert page.errors == []
    finally:
        ctx.close()


def test_add_accept_discard_edit_delete_and_forget_from_the_tab(serve, browser):
    srv, j, _ = serve()
    _seed(j)
    ctx, page = _page(browser, srv.url, 1280, 800)
    entries = lambda status: [e["text"] for e in j.db.query(  # noqa: E731
        "SELECT text FROM entity_note_entries WHERE status = ? ORDER BY id", (status,))]
    try:
        _open_tab(page, 1280)
        page.locator('#ent-list .ent-row[data-id="C1"]').click()
        page.wait_for_selector("#ent-view .ent-suggestion")
        # Accept the suggestion: it becomes a note Jarvis reads
        page.locator('#ent-view .ent-suggestion [data-ent="accept"]').click()
        page.wait_for_function("document.querySelectorAll('#ent-notes .ent-item').length === 4", timeout=10000)
        assert "Invoices to go to the new accounts address" in entries("active") and page.is_hidden("#ent-pending-sec")
        # Add a note; a code is refused with the reason and nothing stored
        page.fill("#ent-add-text", "Door code is 4471#")
        page.click("#ent-view .ent-add [type=submit]")
        page.wait_for_selector("#ent-view .ent-add .appr-error:not([hidden])")
        assert "access codes" in page.inner_text("#ent-view .ent-add .appr-error")
        assert not any("4471" in t for t in entries("active") + entries("pending"))
        page.fill("#ent-add-text", "Site contact is the facilities manager")
        page.click("#ent-view .ent-add [type=submit]")
        page.wait_for_function("document.getElementById('ent-notes').innerText.includes('facilities manager')", timeout=10000)
        assert "Site contact is the facilities manager" in entries("active")
        # Edit one
        item = page.locator("#ent-notes .ent-item", has_text="Prefers morning visits")
        item.locator('[data-ent="edit"]').click()
        item.locator(".mem-input").fill("Prefers visits before 10am")
        item.locator("[type=submit]").click()
        page.wait_for_function("document.getElementById('ent-notes').innerText.includes('before 10am')", timeout=10000)
        assert "Prefers visits before 10am" in entries("active") and "Prefers morning visits" not in entries("active")
        # Delete asks first; Keep it keeps it
        item = page.locator("#ent-notes .ent-item", has_text="Car park")
        item.locator('[data-ent="ask-delete"]').click()
        assert "Delete this note?" in item.inner_text()
        item.locator('[data-ent="keep"]').click()
        assert "Car park is pay and display" in entries("active")
        item.locator('[data-ent="ask-delete"]').click()
        item.locator('[data-ent="delete"]').click()
        # (the note's own text is hidden behind the confirmation, so wait for the list to be drawn again without it)
        page.wait_for_function("document.querySelectorAll('#ent-notes .ent-item').length === 4", timeout=10000)
        assert "Car park" not in page.text_content("#ent-notes")
        assert "Car park is pay and display" not in entries("active")
        # Summary edit
        page.click('[data-ent="edit-summary"]')
        page.fill("#ent-view .ent-summary-edit .mem-input", "Ring first. Mornings only.")
        page.click("#ent-view .ent-summary-edit [type=submit]")
        page.wait_for_function("document.querySelector('#ent-view .ent-summary').innerText.includes('Mornings only')", timeout=10000)
        # a new suggestion arrives; Discard it
        row = j.db.query_one("SELECT id FROM entity_notes WHERE fsm_id = 'C1'")
        j.entity_memory._insert(row["id"], text="Owner plays golf on Fridays", status=em.PENDING, source=em.SRC_PROPOSAL, by="Jarvis",
                                role="jarvis")
        page.click('[data-ent="back"]')
        page.wait_for_function("document.getElementById('ent-pending-count').innerText === '1'", timeout=10000)
        page.locator('#ent-list .ent-row[data-id="C1"]').click()
        page.wait_for_selector("#ent-view .ent-suggestion")
        page.locator('#ent-view .ent-suggestion [data-ent="discard"]').click()
        page.wait_for_selector("#ent-pending-sec[hidden]", state="attached", timeout=10000)
        assert entries("discarded") == ["Owner plays golf on Fridays"]
        # Forget everything (owner): asks first
        page.click('[data-ent="ask-forget"]')
        assert "can't be undone" in page.inner_text(".ent-forget")
        page.click('[data-ent="keep-all"]')
        assert j.db.query_one("SELECT id FROM entity_notes WHERE fsm_id = 'C1'") is not None
        page.click('[data-ent="ask-forget"]')
        page.click('[data-ent="forget"]')
        page.wait_for_function("document.querySelectorAll('#ent-list .ent-row').length === 1", timeout=10000)
        assert j.db.query_one("SELECT id FROM entity_notes WHERE fsm_id = 'C1'") is None
        # "What Jarvis did" names the customer and who, never a note
        whats = " | ".join(a["what"] for a in j.db.query("SELECT what FROM audit_events WHERE kind = 'memory'"))
        assert "Acme Alarms Ltd" in whats
        for text in ("facilities manager", "before 10am", "golf", "Invoices", "Mornings only"):
            assert text not in whats
        assert page.errors == []
    finally:
        ctx.close()


def test_a_first_note_for_a_customer_found_in_salts_fsm(serve, browser):
    srv, j, _ = serve()
    _seed(j, with_acme=False)
    ctx, page = _page(browser, srv.url, 400, 820, "light", has_touch=True, is_mobile=True)
    try:
        _open_tab(page, 400)
        page.fill("#ent-search", "acme")
        page.wait_for_selector("#ent-fsm .ent-row", timeout=10000)
        assert "no notes yet" in page.inner_text("#ent-fsm") and "No notes match that." in page.inner_text("#ent-list")
        _shot(page, "ent-search-400-light")
        page.locator("#ent-fsm .ent-row").first.tap()
        page.wait_for_selector("#ent-add-text")
        assert page.is_hidden("#ent-view .ent-summary-sec")
        page.fill("#ent-add-text", "Wants a call before anyone is sent")
        page.tap("#ent-view .ent-add [type=submit]")
        page.wait_for_function("document.getElementById('ent-notes').innerText.includes('Wants a call')", timeout=10000)
        row = j.db.query_one("SELECT * FROM entity_notes WHERE fsm_id = 'C1'")
        assert row["name"] == "Acme Alarms Ltd" and row["entity_type"] == "customer"
        _fits(page, 400)
        assert page.errors == []
    finally:
        ctx.close()


def test_sample_fsm_data_shows_the_banner(serve, browser):
    srv, j, _ = serve()                       # Salts FSM not configured: sample data
    ctx, page = _page(browser, srv.url, 1280, 800, "dark")
    try:
        page.click("#btn-settings")
        page.wait_for_timeout(350)
        page.click("#btn-open-memory")
        page.click("#mem-tab-entities")
        page.wait_for_selector("#ent-demo:not([hidden])", timeout=10000)
        assert "isn't connected" in page.inner_text("#ent-demo") and "No notes yet" in page.inner_text("#ent-list")
        # keyboard: arrows move between the two tabs
        page.focus("#mem-tab-entities")
        page.keyboard.press("ArrowLeft")
        assert page.get_attribute("#mem-tab-general", "aria-selected") == "true" and page.is_visible("#memory-notes")
        assert page.errors == []
    finally:
        ctx.close()
