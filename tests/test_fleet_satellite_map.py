"""The Fleet map shows satellite imagery by default, with a "Satellite | Map" toggle for the grey street basemap.

Two halves:

* static checks (always on, no browser): the tile URLs and attribution, the toggle's markup and its keyboard / aria wiring,
  the persistence being wrapped in try/catch, the 44px phone rule, and that no Content-Security-Policy anywhere could block
  the Esri host (there is none: the console has never set one, and the existing tiles come from the same host);
* real-browser checks (skipped without Playwright and a Chrome it can launch, like test_console_browser.py): at 1280 / 800
  / 400px in both themes. Esri tile requests are intercepted and answered with a drawn tile, so nothing here needs the live
  tile servers; Leaflet itself is the app's own <script> from unpkg, so those tests skip when it cannot be loaded.

JARVIS_SHOTS=<folder> saves screenshots.
"""
from __future__ import annotations

import os
import random
import re
import struct
import zlib
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "jarvis" / "web"
ROOT = WEB.parent
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
CSS = (WEB / "hud.css").read_text(encoding="utf-8")
HUD = (WEB / "hud.js").read_text(encoding="utf-8")

IMAGERY = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
LABELS = "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
ATTRIBUTION = "Tiles © Esri - Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community"


# ------------------------------------------------------------------------------------------------------ static checks
def test_satellite_imagery_and_its_labels_use_the_esri_hosts_with_the_right_attribution():
    assert 'const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services";' in HUD
    assert "const SATELLITE_URL = `${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`;" in HUD
    assert "const LABELS_URL = `${ESRI}/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}`;" in HUD
    assert f'const SATELLITE_ATTRIBUTION = "{ATTRIBUTION}";' in HUD
    assert "L.tileLayer(SATELLITE_URL, { attribution: SATELLITE_ATTRIBUTION, maxZoom: 19" in HUD
    assert "L.tileLayer(LABELS_URL," in HUD


def test_the_street_basemap_is_still_the_theme_matched_grey_canvas():
    assert "Canvas/World_${window.JarvisTheme?.effective() === \"light\" ? \"Light\" : \"Dark\"}_Gray_Base/MapServer/tile/{z}/{y}/{x}" in HUD
    assert 'window.addEventListener("jarvis-theme", () => { if (tiles) tiles.setUrl(tileUrl()); });' in HUD
    assert "maxNativeZoom: 16" in HUD                                   # the grey canvas stops at 16: stretched beyond that


def test_satellite_is_the_default_and_only_an_explicit_street_choice_overrides_it():
    assert 'let basemap = store.get("basemap", "satellite") === "street" ? "street" : "satellite";' in HUD


def test_the_choice_is_kept_through_the_try_catch_wrapped_store():
    assert 'store.set("basemap", basemap)' in HUD
    # store.get / store.set are the wrapped localStorage accessors: private mode and blocked site data must not break the map
    head = HUD[:HUD.index("const S = {")]
    assert re.search(r"get\(k, d\) \{ try \{ const v = localStorage\.getItem\(", head)
    assert re.search(r"set\(k, v\) \{ try \{ localStorage\.setItem\(", head)
    assert "localStorage" not in HUD[HUD.index("function setBasemap("):HUD.index("// The Fleet pop-up")]


def test_the_toggle_is_two_labelled_native_buttons_with_aria_pressed():
    pop = INDEX[INDEX.index('id="pop-fleet"'):INDEX.index("</section>", INDEX.index('id="pop-fleet"'))]
    group = pop[pop.index('id="map-toggle"'):pop.index("</div>", pop.index('id="map-toggle"'))]
    assert 'role="group"' in group and 'aria-label="Map style"' in group
    assert re.findall(r'<button type="button" data-basemap="(\w+)" aria-pressed="(\w+)">(\w+)</button>', group) == [
        ("satellite", "true", "Satellite"), ("street", "false", "Map")]
    assert pop.index('id="fleet-status"') < pop.index('id="map-toggle"') < pop.index('id="map"')   # the toggle sits above the map
    assert "setAttribute(\"aria-pressed\", String(b.dataset.basemap === basemap))" in HUD


def test_the_toggle_is_hidden_with_the_map_so_the_privacy_rules_are_unchanged():
    assert '$("#map").hidden = !connected || !vans;' in HUD
    assert '$("#map-toggle").hidden = $("#map").hidden || !window.L;' in HUD
    assert 'id="map-toggle" role="group" aria-label="Map style" hidden>' in INDEX     # hidden until a map is really showing
    assert ".map-toggle[hidden] { display: none; }" in CSS


def test_space_on_the_toggle_is_a_click_not_hold_to_talk():
    assert '$("#map-toggle").addEventListener("keydown", (e) => { if (e.key === " ") e.stopPropagation(); });' in HUD


def test_the_toggle_has_a_focus_ring_and_44px_touch_targets_on_phones():
    assert ".map-toggle button:focus-visible { outline: 2px solid var(--core)" in CSS
    phone = CSS[CSS.index("@media (max-width: 760px), (pointer: coarse) {"):]
    assert ".map-toggle button { min-height: 44px; min-width: 44px;" in phone[:phone.index("\n}\n")]


def test_markers_get_a_halo_so_they_stay_readable_on_imagery():
    assert "const halo = (lat, lng, r) => L.circleMarker(" in HUD
    assert 'fillColor: "#fff"' in HUD and "interactive: false" in HUD.split("const halo")[1].split("\n")[0]


def test_the_fleet_map_privacy_behaviours_are_untouched():
    assert 'fleetList.hidden = !fleetState().live;' in HUD
    assert "sample positions are not vehicles" in HUD
    assert "Vehicle tracking is not connected, so there are no live vehicle positions." in HUD
    assert 'data.working_hours === false ? (data.note || "Outside working hours - locations are not shown.")' in HUD


def test_no_content_security_policy_could_block_the_esri_tile_host():
    """The console sets no CSP (header or meta tag), so img-src / connect-src are not restricted; if one is ever added it must
    name server.arcgisonline.com (the tiles are <img> requests) - this fails loudly the day a policy appears without it."""
    sources = [INDEX, (WEB / "login.html").read_text(encoding="utf-8")]
    # The ONE exemption: services/adverts.py defines the policy of the sandboxed advert document (default-src 'none'), which is a
    # different page from the console and never carries a map; it is held to its own stricter check just below.
    advert_module = ROOT / "services" / "adverts.py"
    sources += [p.read_text(encoding="utf-8") for p in ROOT.rglob("*.py") if p != advert_module]
    policy = [s for s in sources if re.search(r"content-security-policy", s, re.I)]
    for text in policy:
        assert "server.arcgisonline.com" in text, "a Content-Security-Policy exists but does not allow server.arcgisonline.com"
    advert = advert_module.read_text(encoding="utf-8")
    assert advert.count('CSP = "default-src \'none\'; img-src data:; style-src \'unsafe-inline\'; font-src data:"') == 1
    assert "arcgisonline" not in advert and "connect-src" not in advert  # sealed: it can reach nothing at all
    assert "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" in INDEX      # Leaflet itself comes from the other host already in use


# ------------------------------------------------------------------------------------------------------ browser checks
sync_api = pytest.importorskip("playwright.sync_api")

import httpx  # noqa: E402

from jarvis.services.tracking import Tracker  # noqa: E402
from tests.test_console_browser import SIZES, THEMES, _no_hscroll, browser  # noqa: E402,F401
from tests.test_console_browser_phase3 import _fleet_text, _page, _ram_http, serve  # noqa: E402,F401
from tests.test_console_browser_updates import RAM, _ram_vehicles  # noqa: E402

SHOTS = os.environ.get("JARVIS_SHOTS")
ESRI_HOST = "server.arcgisonline.com"


def _shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def _png(width, height, pixel):
    raw = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(width)) for y in range(height))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _tiles():
    """Stand-ins for the three Esri layers: patchy greens and browns for imagery, a clear tile for the labels, flat grey."""
    rng = random.Random(7)
    palette = [(52, 84, 41), (96, 118, 62), (128, 108, 74), (74, 70, 64), (38, 66, 48), (150, 140, 120), (66, 96, 120)]
    blocks = [[rng.choice(palette) for _ in range(16)] for _ in range(16)]
    return {
        "World_Imagery": _png(256, 256, lambda x, y: (*blocks[y // 16][x // 16], 255)),
        "World_Boundaries_and_Places": _png(256, 256, lambda x, y: (255, 255, 255, 0)),
        "Canvas": _png(256, 256, lambda x, y: (205, 205, 205, 255) if (x // 64 + y // 64) % 2 else (190, 190, 190, 255)),
    }


def _stub_esri(context):
    """Answer every Esri tile request with a drawn tile and keep the URLs asked for. Nothing leaves the machine."""
    tiles = _tiles()
    seen: list[str] = []

    def handle(route):
        url = route.request.url
        seen.append(url)
        for key, body in tiles.items():
            if f"/{key}" in url or f"/{key}/" in url:
                return route.fulfill(status=200, content_type="image/png", body=body)
        return route.fulfill(status=404, body=b"")

    context.route(f"**://{ESRI_HOST}/**", handle)
    return seen


def _kinds(urls):
    return {k for k in ("World_Imagery", "World_Boundaries_and_Places", "Canvas") if any(f"/{k}" in u for u in urls)}


def _vans(monkeypatch, serve):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: True))
    http = _ram_vehicles([("Dan Harper", "Dan Harper home 14 Acacia Avenue, Bradford BD1 1AA"), ("Kay Lund", "M62 J24 Services")])
    return serve(http=http, **RAM)


def _fleet(browser, serve, monkeypatch, width=1280, height=800, scheme="dark", **ctx_kw):
    srv, _, _ = _vans(monkeypatch, serve)
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, **ctx_kw)
    seen = _stub_esri(context)
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    return srv, context, page, seen


def _load(page, srv):
    page.goto(srv.url + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#needs-list .need, #needs-list .needs-clear:not(:has-text('Loading'))", timeout=15000)
    try:
        page.wait_for_function("!!window.L", timeout=15000)
    except Exception:  # noqa: BLE001
        pytest.skip("Leaflet (the app's own unpkg <script>) could not be loaded here")
    assert "Live from RAM Tracking" in _fleet_text(page)
    page.wait_for_selector("#map img.leaflet-tile-loaded", timeout=10000)
    page.wait_for_timeout(300)


def _pressed(page):
    return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('#map-toggle button')].map(b => [b.textContent.trim(), b.getAttribute('aria-pressed')]))""")


def _tile_srcs(page):
    return page.eval_on_selector_all("#map img.leaflet-tile", "els => els.map(e => e.src)")


def _stored(page):
    return page.evaluate("localStorage.getItem('jarvis.basemap')")


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_the_fleet_map_opens_on_satellite_imagery_with_labels_and_an_attribution(browser, serve, monkeypatch, width, height, scheme):
    srv, ctx, page, seen = _fleet(browser, serve, monkeypatch, width, height, scheme)
    try:
        _load(page, srv)
        assert _kinds(seen) == {"World_Imagery", "World_Boundaries_and_Places"}          # satellite + labels, no grey canvas
        assert page.get_attribute("#map", "data-basemap") == "satellite"
        assert _pressed(page) == {"Satellite": "true", "Map": "false"}
        assert page.is_visible("#map-toggle") and page.is_visible("#map")
        assert page.locator("#map-toggle button").all_inner_texts() == ["Satellite", "Map"]
        assert page.get_attribute("#map-toggle", "aria-label") == "Map style"
        assert ATTRIBUTION in page.inner_text("#map .leaflet-control-attribution")
        srcs = _tile_srcs(page)
        assert srcs and all(ESRI_HOST in s for s in srcs)
        assert any("/World_Imagery/MapServer/tile/" in s for s in srcs)
        assert not any("/Canvas/" in s for s in srcs)
        # the vans are on it, each with a halo underneath, and the van list is the same as before
        halos, markers = page.locator("#map path.map-halo").count(), page.locator("#map path.leaflet-interactive").count()
        assert halos == markers and halos >= 2                                           # one halo under every site and van marker
        assert page.locator("#fleet-list li").count() == 2
        doc, body, vw = _no_hscroll(page)
        assert doc <= vw and body <= vw, (doc, body, vw)
        box = page.locator("#map-toggle").bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width
        _shot(page, f"fleet-satellite-{width}-{scheme}")
        assert not page.errors, page.errors
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_choosing_map_switches_to_the_grey_street_basemap_and_satellite_switches_back(browser, serve, monkeypatch, width, height, scheme):
    srv, ctx, page, seen = _fleet(browser, serve, monkeypatch, width, height, scheme)
    try:
        _load(page, srv)
        seen.clear()
        page.click('#map-toggle button[data-basemap="street"]')
        page.wait_for_function("document.querySelector('#map img.leaflet-tile-loaded') && ![...document.querySelectorAll('#map img.leaflet-tile')].some(i => i.src.includes('World_Imagery'))")
        assert page.get_attribute("#map", "data-basemap") == "street"
        assert _pressed(page) == {"Satellite": "false", "Map": "true"}
        grey = "World_Light_Gray_Base" if scheme == "light" else "World_Dark_Gray_Base"
        srcs = _tile_srcs(page)
        assert srcs and all(f"/Canvas/{grey}/MapServer/tile/" in s for s in srcs), srcs[:3]
        assert not any("World_Boundaries_and_Places" in s for s in srcs)                 # the labels belong to imagery only
        assert "Esri, HERE, Garmin" in page.inner_text("#map .leaflet-control-attribution")
        assert "Maxar" not in page.inner_text("#map .leaflet-control-attribution")
        assert _stored(page) == "street"
        assert page.locator("#map path.map-halo").count() == page.locator("#map path.leaflet-interactive").count() >= 2   # markers unchanged either way
        _shot(page, f"fleet-street-{width}-{scheme}")
        page.click('#map-toggle button[data-basemap="satellite"]')
        page.wait_for_function("[...document.querySelectorAll('#map img.leaflet-tile')].some(i => i.src.includes('World_Imagery')) && ![...document.querySelectorAll('#map img.leaflet-tile')].some(i => i.src.includes('/Canvas/'))")
        assert _pressed(page) == {"Satellite": "true", "Map": "false"} and _stored(page) == "satellite"
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_the_street_basemap_follows_the_theme_while_the_satellite_does_not_change(browser, serve, monkeypatch):
    srv, ctx, page, seen = _fleet(browser, serve, monkeypatch, scheme="dark")
    try:
        _load(page, srv)
        page.click('#map-toggle button[data-basemap="street"]')
        page.wait_for_function("[...document.querySelectorAll('#map img.leaflet-tile')].some(i => i.src.includes('World_Dark_Gray_Base'))")
        page.evaluate("window.JarvisTheme.set('light')")
        page.wait_for_function("[...document.querySelectorAll('#map img.leaflet-tile')].some(i => i.src.includes('World_Light_Gray_Base'))")
        page.click('#map-toggle button[data-basemap="satellite"]')
        page.evaluate("window.JarvisTheme.set('dark')")
        page.wait_for_timeout(400)
        srcs = _tile_srcs(page)
        assert any("World_Imagery" in s for s in srcs) and not any("Gray_Base" in s for s in srcs)
    finally:
        ctx.close()


def test_the_choice_survives_a_reload_and_a_new_tab_in_the_same_browser(browser, serve, monkeypatch):
    srv, ctx, page, seen = _fleet(browser, serve, monkeypatch)
    try:
        _load(page, srv)
        page.click('#map-toggle button[data-basemap="street"]')
        page.wait_for_function("document.getElementById('map').dataset.basemap === 'street'")
        seen.clear()
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("!!window.L")
        _fleet_text(page)
        page.wait_for_selector("#map img.leaflet-tile-loaded")
        assert _pressed(page) == {"Satellite": "false", "Map": "true"}
        assert "World_Imagery" not in " ".join(seen) and "Canvas" in " ".join(seen)     # street from the first tile on: no flash of imagery
        other = ctx.new_page()
        other.goto(srv.url + "/", wait_until="domcontentloaded")
        other.wait_for_function("!!window.L")
        _fleet_text(other)
        other.wait_for_selector("#map img.leaflet-tile-loaded")
        assert _pressed(other) == {"Satellite": "false", "Map": "true"}
    finally:
        ctx.close()


def test_an_unknown_stored_value_falls_back_to_satellite(browser, serve, monkeypatch):
    srv, ctx, page, seen = _fleet(browser, serve, monkeypatch)
    try:
        ctx.add_init_script("try { localStorage.setItem('jarvis.basemap', 'hybrid'); } catch (e) {}")
        _load(page, srv)
        assert _pressed(page) == {"Satellite": "true", "Map": "false"} and _kinds(seen) == {"World_Imagery", "World_Boundaries_and_Places"}
    finally:
        ctx.close()


def test_the_map_works_without_localstorage_and_defaults_to_satellite(browser, serve, monkeypatch):
    srv, ctx, page, seen = _fleet(browser, serve, monkeypatch)
    try:
        ctx.add_init_script("""
            const boom = () => { throw new DOMException('blocked', 'SecurityError'); };
            Object.defineProperty(window, 'localStorage', { get: boom, configurable: true });""")
        _load(page, srv)
        assert _pressed(page) == {"Satellite": "true", "Map": "false"}
        page.click('#map-toggle button[data-basemap="street"]')                          # still switches for this visit
        page.wait_for_function("document.getElementById('map').dataset.basemap === 'street'")
        assert _pressed(page) == {"Satellite": "false", "Map": "true"}
        assert any("/Canvas/" in s for s in _tile_srcs(page))
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_the_toggle_works_from_the_keyboard_and_exposes_pressed_state_to_assistive_tech(browser, serve, monkeypatch):
    srv, ctx, page, seen = _fleet(browser, serve, monkeypatch)
    try:
        _load(page, srv)
        page.focus('#map-toggle button[data-basemap="satellite"]')
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.dataset.basemap") == "street"      # reachable by Tab, in reading order
        outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
        assert outline != "none"                                                         # a visible focus ring
        page.keyboard.press("Enter")
        page.wait_for_function("document.getElementById('map').dataset.basemap === 'street'")
        assert _pressed(page) == {"Satellite": "false", "Map": "true"}
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Space")
        page.wait_for_function("document.getElementById('map').dataset.basemap === 'satellite'")
        assert _pressed(page) == {"Satellite": "true", "Map": "false"}
        snap = page.locator("#map-toggle").aria_snapshot()
        assert "group" in snap and "Satellite" in snap and "Map" in snap
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
def test_on_a_phone_the_toggle_buttons_are_at_least_44px_and_fit_the_screen(browser, serve, monkeypatch, scheme):
    srv, ctx, page, seen = _fleet(browser, serve, monkeypatch, 400, 820, scheme, has_touch=True, is_mobile=True)
    try:
        _load(page, srv)
        rects = page.eval_on_selector_all("#map-toggle button", "els => els.map(e => { const r = e.getBoundingClientRect(); return [r.width, r.height, r.left, r.right]; })")
        assert len(rects) == 2 and all(w >= 43.5 and h >= 43.5 and l >= 0 and r <= 400 for w, h, l, r in rects), rects
        page.tap('#map-toggle button[data-basemap="street"]')
        page.wait_for_function("document.getElementById('map').dataset.basemap === 'street'")
        _shot(page, f"fleet-toggle-phone-{scheme}")
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
def test_the_toggle_is_legible_in_both_themes(browser, serve, monkeypatch, scheme):
    srv, ctx, page, seen = _fleet(browser, serve, monkeypatch, 1280, 800, scheme)
    try:
        _load(page, srv)

        def luminance(rgb):
            def chan(c):
                c /= 255
                return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            r, g, b = (chan(c) for c in rgb)
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        def ratio(a, b):
            la, lb = sorted((luminance(a), luminance(b)), reverse=True)
            return (la + 0.05) / (lb + 0.05)

        def colours(sel):
            """Text colour and the background it really sits on: every translucent layer up the tree blended over the page."""
            return page.evaluate(r"""(sel) => { const e = document.querySelector(sel);
                const parse = (s) => { const m = (s.match(/[\d.]+/g) || []).map(Number); return [m[0], m[1], m[2], m.length > 3 ? m[3] : 1]; };
                const layers = []; for (let n = e; n; n = n.parentElement) layers.push(parse(getComputedStyle(n).backgroundColor));
                let out = [255, 255, 255];
                for (const [r, g, b, a] of layers.reverse()) out = [r * a + out[0] * (1 - a), g * a + out[1] * (1 - a), b * a + out[2] * (1 - a)];
                return [parse(getComputedStyle(e).color).slice(0, 3), out]; }""", sel)

        for sel in ('#map-toggle button[aria-pressed="true"]', '#map-toggle button[aria-pressed="false"]'):
            fg, bg = colours(sel)
            assert ratio(fg[:3], bg[:3]) >= 4.5, (sel, scheme, fg, bg, ratio(fg[:3], bg[:3]))
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
def test_the_toggle_and_map_are_not_shown_when_vehicle_tracking_is_not_connected(browser, serve, scheme):
    srv, _, _ = serve()                                                                  # no RAM details: sample data only
    context = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme=scheme)
    _stub_esri(context)
    page = context.new_page()
    try:
        page.goto(srv.url + "/", wait_until="domcontentloaded")
        page.wait_for_selector("#needs-list .need, #needs-list .needs-clear:not(:has-text('Loading'))", timeout=15000)
        assert "Vehicle tracking is not connected" in _fleet_text(page)
        assert page.is_hidden("#map-toggle") and page.is_hidden("#map") and page.is_hidden("#fleet-list")
    finally:
        context.close()


def test_out_of_hours_with_locations_off_shows_no_map_and_no_toggle(browser, serve, monkeypatch):
    monkeypatch.setattr(Tracker, "in_working_hours", staticmethod(lambda now=None: False))
    monkeypatch.setattr(Tracker, "demo", property(lambda self: False))  # the privacy rule only applies to real data
    http = _ram_vehicles([("Dan Harper", "M62 J24 Services")])
    srv, _, _ = serve(http=http, **RAM)
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    _stub_esri(context)
    page = context.new_page()
    try:
        page.goto(srv.url + "/", wait_until="domcontentloaded")
        page.wait_for_selector("#needs-list .need, #needs-list .needs-clear:not(:has-text('Loading'))", timeout=15000)
        assert "Outside working hours" in _fleet_text(page)
        assert page.is_hidden("#map-toggle") and page.is_hidden("#map")
        assert "Dan Harper" not in page.inner_text("#pop-fleet")
    finally:
        context.close()


def test_with_leaflet_blocked_the_toggle_stays_hidden_and_the_list_fallback_shows(browser, serve, monkeypatch):
    srv, ctx, page, seen = _fleet(browser, serve, monkeypatch)
    try:
        ctx.route("**://unpkg.com/**", lambda route: route.abort())
        page.goto(srv.url + "/", wait_until="domcontentloaded")
        page.wait_for_selector("#needs-list .need, #needs-list .needs-clear:not(:has-text('Loading'))", timeout=15000)
        assert "Live from RAM Tracking" in _fleet_text(page)
        page.wait_for_selector("#map li")
        assert page.is_hidden("#map-toggle") and page.evaluate("typeof window.L") == "undefined"
        assert not [u for u in seen if ESRI_HOST in u]
    finally:
        ctx.close()
