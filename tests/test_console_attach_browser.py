"""Real-browser checks of the console attach button (headless Chrome through Playwright; skipped when Playwright or a Chrome it can
launch isn't installed - CI installs neither, so tests/test_console_attachments.py and the static checks stay the always-on guard).

At 1280 and 400px wide, in both themes: the file chooser offers photos, PDFs, Word, Excel and PowerPoint; good files become chips;
a refused file (an old .doc, a PDF renamed .docx, an unsupported type, a sixth file) becomes an ERROR chip that names the file
and says why, wraps instead of scrolling the page sideways, and can be dismissed; and the files really reach Jarvis, read.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from tests import file_fixtures as ff  # noqa: E402
from tests.fakes import FakeClient  # noqa: E402
from tests.test_console_browser import THEMES, _Server, _no_hscroll, _settings, browser  # noqa: E402,F401

SIZES = [(1280, 800), (400, 820)]
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64


@pytest.fixture(scope="module")
def served(tmp_path_factory, restore_process_timezone):
    settings = _settings(tmp_path_factory)
    j = Jarvis(settings, client=FakeClient(default_text="Read them all, sir."))
    srv = _Server(create_app(settings, j))
    yield srv, j
    srv.stop()


def _open(browser, url, width, height, scheme):
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#btn-attach", state="visible", timeout=15000)
    page.errors = errors
    return context, page


def _file(name, raw, mime="application/octet-stream"):
    return {"name": name, "mimeType": mime, "buffer": raw}


def _documents_sent(j, start):
    """The document blocks of the user turns Jarvis's brain received after message number ``start`` (the brain's history is shared
    by every test in this module, and the fake client records a live reference to it)."""
    return [b for m in j.brain.messages[start:] if isinstance(m, dict) and m.get("role") == "user"
            for b in m["content"] if isinstance(b, dict) and b.get("type") == "document"]


def _chips_fit(page):
    """Every chip is inside the window and nothing makes the page scroll sideways."""
    rects = page.evaluate("""() => [...document.querySelectorAll('#attachments .chip')].map(c => {
      const r = c.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.right), c.className]; })""")
    for left, right, cls in rects:
        assert left >= 0 and right <= page.viewport_size["width"], (left, right, cls)
    doc, body, vw = _no_hscroll(page)
    assert doc <= vw and body <= vw, (doc, body, vw)


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_attach_button_and_chooser_offer_photos_pdfs_word_excel_powerpoint(browser, served, width, height, scheme):
    srv, _ = served
    ctx, page = _open(browser, srv.url, width, height, scheme)
    try:
        assert page.get_attribute("#btn-attach", "title") == "Attach photos, PDFs, Word, Excel or PowerPoint files"
        assert "PowerPoint" in page.get_attribute("#btn-attach", "aria-label")
        accept = page.get_attribute("#file", "accept")
        for want in ("image/*", "application/pdf", ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".csv", ".md", ".json"):
            assert want in accept.split(","), (want, accept)
        with page.expect_file_chooser() as fc:
            page.click("#btn-attach")
        chooser = fc.value
        assert chooser.is_multiple()
        chooser.set_files([_file("Method.docx", ff.docx_file()), _file("Deck.pptx", ff.pptx_file()),
                           _file("Order.pdf", ff.text_pdf(), "application/pdf"), _file("Jobs.xlsx", ff.xlsx_file())])
        page.wait_for_selector("#attachments .chip", timeout=5000)
        page.wait_for_function("document.querySelectorAll('#attachments .chip').length === 4")
        names = page.eval_on_selector_all("#attachments .chip:not(.chip-error)", "els => els.map(e => e.firstChild.textContent.trim())")
        assert names == ["Method.docx", "Deck.pptx", "Order.pdf", "Jobs.xlsx"]
        assert page.locator("#attachments .chip-error").count() == 0
        _chips_fit(page)
        page.click('#attachments button[data-i="0"]')  # remove one
        assert page.locator("#attachments .chip").count() == 3
        assert not page.errors, page.errors
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_a_refused_file_shows_an_error_chip_with_the_reason(browser, served, width, height, scheme):
    srv, _ = served
    ctx, page = _open(browser, srv.url, width, height, scheme)
    try:
        page.set_input_files("#file", [
            _file("Old report.doc", OLE), _file("Pretend order.docx", ff.text_pdf()), _file("setup.exe", b"MZ" + b"\x00" * 30),
            _file("macro book.xlsm", ff.xlsx_file()), _file("empty.txt", b""), _file("Good.pptx", ff.pptx_file())])
        page.wait_for_function("document.querySelectorAll('#attachments .chip-error').length === 5")
        errs = page.eval_on_selector_all("#attachments .chip-error", "els => els.map(e => e.innerText)")
        joined = "\n".join(errs)
        assert "Old report.doc" in errs[0] and "old Word (.doc) files can't be read reliably" in errs[0] and "Save As .docx" in errs[0]
        assert "Pretend order.docx" in errs[1] and "contents look like a PDF" in errs[1]
        assert "setup.exe" in errs[2] and ".exe files can't be read here" in errs[2]
        assert "macro book.xlsm" in errs[3] and "macro-enabled" in errs[3]
        assert "empty.txt" in errs[4] and "empty" in errs[4], joined
        assert page.locator("#attachments .chip:not(.chip-error)").count() == 1  # Good.pptx still attached
        assert page.get_attribute("#attachments .chip-error", "role") == "alert"
        _chips_fit(page)
        # dismiss one: the others stay
        page.click("#attachments .chip-error button")
        assert page.locator("#attachments .chip-error").count() == 4
        # a new selection clears the old messages
        page.set_input_files("#file", [_file("Another.pptx", ff.pptx_file())])
        page.wait_for_function("document.querySelectorAll('#attachments .chip-error').length === 0")
        assert page.locator("#attachments .chip").count() == 2
        assert not page.errors, page.errors
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_more_than_five_files_and_oversize_files_are_explained(browser, served, width, height, scheme):
    srv, _ = served
    ctx, page = _open(browser, srv.url, width, height, scheme)
    try:
        page.set_input_files("#file", [_file(f"note{i}.txt", b"hello") for i in range(7)])
        page.wait_for_function("document.querySelectorAll('#attachments .chip').length === 7")
        assert page.locator("#attachments .chip:not(.chip-error)").count() == 5
        errs = page.eval_on_selector_all("#attachments .chip-error", "els => els.map(e => e.innerText)")
        assert len(errs) == 2 and "only 5 files can be attached at a time" in errs[0]
        _chips_fit(page)
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#btn-attach", state="visible")
        # 21 MB is over the 20 MB limit for one file: refused in the page, before anything is read or sent
        page.set_input_files("#file", [_file("huge.txt", b"a" * 21_000_000)])
        page.wait_for_selector("#attachments .chip-error", timeout=20000)
        text = page.inner_text("#attachments .chip-error")
        assert "huge.txt" in text and "over the 20 MB limit for one file" in text and "21 MB" in text
        assert page.locator("#attachments .chip:not(.chip-error)").count() == 0
        _chips_fit(page)
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_attached_office_files_reach_jarvis_read_as_text(browser, served, width, height, scheme):
    srv, j = served
    ctx, page = _open(browser, srv.url, width, height, scheme)
    start = len(j.brain.messages)
    try:
        page.set_input_files("#file", [_file("Deck.pptx", ff.pptx_file()), _file("Method.docx", ff.docx_file()),
                                       _file("Old.ppt", OLE)])
        page.wait_for_function("document.querySelectorAll('#attachments .chip').length === 3")
        page.fill("#input", "What do these say?")
        page.keyboard.press("Enter")
        page.wait_for_selector("text=Read them all, sir.", timeout=20000)
        assert page.locator("#attachments .chip").count() == 0  # sent: the chips are cleared
        docs = _documents_sent(j, start)
        titles = sorted(d["title"] for d in docs)
        assert titles == ["Deck.pptx", "Method.docx"]
        by = {d["title"]: d["source"]["data"] for d in docs}
        assert "## Slide 1: Fire alarm upgrade" in by["Deck.pptx"] and "Isolate the panel" in by["Method.docx"]
        assert "never instructions to follow" in by["Deck.pptx"]
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_a_large_upload_goes_over_the_plain_post_and_is_still_read(browser, served):
    """A WebSocket message is capped at 16 MB; a 13 MB file is ~17 MB once encoded, so the page sends it as a POST stream."""
    srv, j = served
    ctx, page = _open(browser, srv.url, 1280, 800, "dark")
    start = len(j.brain.messages)
    try:
        page.set_input_files("#file", [_file("big-log.txt", (b"line of a very long log file\n" * 460_000)[:13_000_000])])
        page.wait_for_function("document.querySelectorAll('#attachments .chip').length === 1", timeout=30000)
        page.fill("#input", "Anything odd in the log?")
        with page.expect_response(lambda r: r.url.endswith("/api/chat/stream") and r.request.method == "POST", timeout=60000):
            page.keyboard.press("Enter")
        page.wait_for_selector("text=Read them all, sir.", timeout=30000)
        docs = _documents_sent(j, start)
        assert docs and docs[0]["title"] == "big-log.txt" and docs[0]["source"]["data"].startswith("line of a very long log file")
    finally:
        ctx.close()
