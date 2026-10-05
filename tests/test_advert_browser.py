"""Real-browser checks of the Claude-designed adverts on the display: the design sits in an iframe with sandbox="" and the
Content-Security-Policy page, Download PNG really produces a picture of exactly the platform's size with pixels in it (the
headline, the navy panel and the logo), Download HTML hands back the same sandboxed page, and "Ask for changes" revises the
same design. The model is mocked; everything else is the real app in headless Chrome through Playwright (skipped when
Playwright or a Chrome it can launch is missing, like tests/test_console_browser.py).

Set JARVIS_SHOTS=<folder> to also save a screenshot of the display for each preset."""
from __future__ import annotations

import io
import os
import re
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from PIL import Image  # noqa: E402

from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from jarvis.services import adverts, images  # noqa: E402
from tests.fakes import FakeClient, message, text_block, tool_block  # noqa: E402
from tests.live_server import LiveServer  # noqa: E402
from tests.test_adverts import HEADLINE, _good  # noqa: E402
from tests.test_console_browser import _settings, browser  # noqa: E402,F401

SHOTS = os.environ.get("JARVIS_SHOTS")


async def _designer(client, settings, schema, *, system, prompt, effort="low", max_tokens=8000):
    w, h = map(int, re.search(r"exactly (\d+) x (\d+) px", system).groups())
    html = _good(w=w, h=h)
    if "REVISION" in system:
        html = html.replace("font-size:84px", "font-size:120px")
    return schema(html=html)


@pytest.fixture(scope="module")
def stack(tmp_path_factory, restore_process_timezone):
    mp = pytest.MonkeyPatch()
    mp.setattr(adverts.llm, "structured", _designer)
    settings = _settings(tmp_path_factory)
    client = FakeClient()
    j = Jarvis(settings, client=client)
    srv = LiveServer(create_app(settings, j))
    yield srv, j, client
    srv.stop()
    mp.undo()


def _open(browser, url, width=1280, height=900):
    ctx = browser.new_context(viewport={"width": width, "height": height}, accept_downloads=True)
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(url + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#needs-list .need, #needs-list .needs-clear:not(:has-text('Loading'))", timeout=15000)
    return ctx, page


def _draft(page, client, platform):
    client.beta.messages.script += [
        message([tool_block("generate_image", {"headline": HEADLINE, "platform": platform, "subtext": "BAFE accredited"})],
                "tool_use"),
        message([text_block("The draft is on the display.")]),
    ]
    page.fill("#input", f"Make a {platform} graphic")
    page.click("#btn-send")
    page.wait_for_selector(".advert iframe", timeout=30000)
    page.wait_for_function("(() => { const f = document.querySelector('.advert iframe'); return !!f && !!f.srcdoc; })()")


def _png(download) -> Image.Image:
    return Image.open(io.BytesIO(Path(download.path()).read_bytes())).convert("RGB")


@pytest.mark.parametrize("platform", sorted(images.PLATFORM_SIZES))
def test_download_png_is_exactly_the_platform_size_with_the_design_painted_in(browser, stack, platform):
    srv, j, client = stack
    w, h = images.PLATFORM_SIZES[platform]
    ctx, page = _open(browser, srv.url)
    try:
        _draft(page, client, platform)
        iframe = page.locator(".advert iframe")
        assert iframe.get_attribute("sandbox") == ""  # present and empty: no scripts, same-origin, forms or popups
        srcdoc = iframe.get_attribute("srcdoc")
        assert ('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; '
                'style-src \'unsafe-inline\'; font-src data:">') in srcdoc
        assert page.evaluate("document.querySelector('.advert iframe').contentDocument") is None  # opaque origin
        assert (iframe.get_attribute("width"), iframe.get_attribute("height")) == (str(w), str(h))
        assert "designed graphic" in page.inner_text(".advert-facts") and "not an AI photograph" in page.inner_text(".advert-facts")
        assert page.inner_text("#display-body").count("not posted anywhere") == 1
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(SHOTS) / f"advert-display-{platform}.png"))

        with page.expect_download() as dl:
            page.click("#advert-png")
        assert dl.value.suggested_filename.endswith(".png") and dl.value.suggested_filename.startswith(f"salts-draft-{platform}-")
        im = _png(dl.value)
        assert im.size == (w, h)

        # not blank: the navy gradient, white headline text, and the logo's dark lettering on its white panel
        colours = im.getcolors(maxcolors=1_000_000)
        assert colours is not None and len(colours) > 40
        r, g, b = im.getpixel((w // 2, h - 8))
        assert b > r and b > g and r < 80  # navy, not white or transparent-black
        short = min(w, h)
        head_box = im.crop((64, int(h * 0.45), w - 64, int(h * 0.80)))
        white = sum(1 for p in head_box.getdata() if min(p) > 235)
        assert white > 400, white  # headline text
        logo_box = im.crop((64, 64, 64 + int(short * 0.2), 64 + int(short * 0.1)))
        dark = sum(1 for p in logo_box.getdata() if max(p) < 120)
        light = sum(1 for p in logo_box.getdata() if min(p) > 235)
        assert light > 200 and dark > 30, (light, dark)  # the logo image itself drew (white panel + navy lettering)
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_download_html_is_the_same_sandboxed_page(browser, stack):
    srv, j, client = stack
    ctx, page = _open(browser, srv.url)
    try:
        _draft(page, client, "linkedin")
        with page.expect_download() as dl:
            page.click("#advert-html")
        assert dl.value.suggested_filename.endswith(".html")
        text = Path(dl.value.path()).read_text(encoding="utf-8")
        assert text.startswith("<!doctype html>") and adverts.CSP in text
        assert HEADLINE in text and "<script" not in text.lower() and "SALTS_LOGO" not in text
        assert re.search(r'src="data:image/(jpeg|png);base64,', text)  # the logo travels inside the file
    finally:
        ctx.close()


def test_ask_for_changes_revises_the_same_design(browser, stack):
    srv, j, client = stack
    ctx, page = _open(browser, srv.url)
    try:
        _draft(page, client, "facebook")
        before = page.get_attribute(".advert iframe", "srcdoc")
        design_id = page.get_attribute(".advert", "data-advert-id")
        assert "font-size:84px" in before and "version 1" in page.inner_text(".advert-facts")
        page.fill("#advert-changes", "bigger headline, add 10% off")
        page.click("#advert-revise")
        page.wait_for_function("document.querySelector('.advert-facts') && document.querySelector('.advert-facts').textContent.includes('version 2')",
                               timeout=30000)
        after = page.get_attribute(".advert iframe", "srcdoc")
        assert "font-size:120px" in after and "font-size:84px" not in after
        assert page.get_attribute(".advert", "data-advert-id") == design_id  # the same design, revised in place
        assert page.locator(".advert iframe").get_attribute("sandbox") == ""
        assert j.db.query_one("SELECT revision FROM adverts WHERE id=?", (design_id,))["revision"] == 2
        # an empty request says so instead of calling the model
        page.fill("#advert-changes", "")
        page.click("#advert-revise")
        assert "Say what to change" in page.inner_text(".advert-status")
    finally:
        ctx.close()


@pytest.mark.parametrize("width,height", [(1280, 900), (800, 900), (400, 820)])
def test_the_preview_fits_the_display_at_every_width_without_sideways_scrolling(browser, stack, width, height):
    srv, j, client = stack
    ctx, page = _open(browser, srv.url, width, height)
    try:
        _draft(page, client, "tiktok")  # the tallest preset
        box = page.locator(".advert-frame").bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1, box
        assert box["height"] <= height, box  # scaled to fit the window, not taller than it
        doc = page.evaluate("[document.documentElement.scrollWidth, document.body.scrollWidth, innerWidth]")
        assert doc[0] <= doc[2] and doc[1] <= doc[2], doc
        assert page.is_visible("#advert-png") and page.is_visible("#advert-changes")
    finally:
        ctx.close()
