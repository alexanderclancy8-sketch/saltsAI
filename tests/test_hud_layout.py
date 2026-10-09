"""Static checks on the console's layout, tokens and pop-up drawer (no JS runner in this repo, so - like
test_hud_bargein.py - these read index.html / hud.css / hud.js and assert the guards that keep the layout usable are
present). The real-browser checks at 1280 / 800 / 400px in both themes live in test_console_browser.py (skipped when
Playwright is not installed)."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "jarvis" / "web"
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
LOGIN = (WEB / "login.html").read_text(encoding="utf-8")
CSS = (WEB / "hud.css").read_text(encoding="utf-8")
HUD = (WEB / "hud.js").read_text(encoding="utf-8")
CORE = (WEB / "core.js").read_text(encoding="utf-8")
THEME = (WEB / "theme.js").read_text(encoding="utf-8")
ASK_CSS = (WEB / "ask.css").read_text(encoding="utf-8")

# The rail inventory, in order. "Activity" (What Jarvis did) joined in the activity-page change, right after Approvals: the two are used
# together (what is waiting, then what has been done), and the phone chip strip keeps Approvals pinned first. The roles are part of the
# pin since then: a team console keeps exactly the four it always had, and Activity (like Approvals) is for owner and manager only.
# "Drawings" (floor-plan device layouts and zone charts, services/plan_drawings.py) is the twelfth, last, for every role and with no
# count: engineers open the drawings linked to their jobs on site.
RAIL = ["approvals", "activity", "comms", "issues", "health", "faults", "ops", "fleet", "finance", "presence", "upcoming", "drawings"]
RAIL_MANAGER_ONLY = ["approvals", "activity", "comms", "issues", "health", "faults", "finance"]
RAIL_NO_COUNT = ["activity", "presence", "drawings"]
# Everything the old dashboard showed must be reachable in a pop-up: element id -> the pop-up that holds it.
DASHBOARD_PANELS = {
    "approvals": ["approvals", "suggestions"],
    "activity": ["activity-summary", "activity-list", "activity-kind", "activity-status", "activity-who", "activity-search", "activity-everything"],
    "comms": ["inbox"],
    "issues": ["issues"],
    "health": ["tests", "btn-run-tests", "notifications"],
    "ops": ["ops-kpis", "ops"],
    "fleet": ["map", "fleet-status"],
    "finance": ["finance", "customers"],
    "presence": ["presence"],
    "upcoming": ["deadlines"],
}


def _pop(name):
    start = INDEX.index(f'id="pop-{name}"')
    m = re.search(r'<section class="pop"', INDEX[start:])
    return INDEX[start:start + m.start()] if m else INDEX[start:INDEX.index("</aside>")]


def _block(text, opener):
    """The body of the CSS rule/at-rule that starts with `opener` (balanced braces)."""
    i = text.index(opener) + len(opener)
    depth, j = 1, i
    while depth:
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        j += 1
    return text[i:j - 1]


def _tokens(block):
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))


def _lum(hex_colour):
    h = hex_colour.strip().lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


DARK = _tokens(_block(CSS, ":root {"))
LIGHT_ATTR = _tokens(_block(CSS, ':root[data-theme="light"] {'))
LIGHT_MEDIA = _tokens(_block(_block(CSS, "@media (prefers-color-scheme: light) {"), ':root:not([data-theme="dark"]) {'))


def test_viewport_handles_notches_and_the_on_screen_keyboard():
    assert "viewport-fit=cover" in INDEX and "interactive-widget=resizes-content" in INDEX
    # The app shell is dvh-tall (URL bar) and follows visualViewport (iOS keyboard); never a fixed 100vh-62px grid.
    assert "100dvh" in CSS and "--app-h" in CSS and "calc(100vh - 62px)" not in CSS
    assert "visualViewport" in HUD and '--app-h' in HUD


# ------------------------------------------------------------------------------------------ design tokens
MOCKUP = (Path(__file__).resolve().parent.parent / "docs" / "redesign" / "jarvis-console-mockup.html").read_text(encoding="utf-8")
SURFACES = ("--navy-deepest", "--navy", "--panel", "--panel-hi")


def test_dark_tokens_are_the_mockups_root_block_exactly():
    mock = _tokens(_block(MOCKUP, ":root{"))
    assert mock["--navy-deepest"] == "#060d1a" and mock["--core"] == "#4fd8ff"           # sanity: parsed the right block
    for name, value in mock.items():
        assert re.sub(r"\s+", "", DARK[name]) == re.sub(r"\s+", "", value), name
    assert DARK["--f-display"].startswith("'Oxanium'") and DARK["--f-body"].startswith("'IBM Plex Sans'") and DARK["--f-mono"].startswith("'IBM Plex Mono'")


def test_design_tokens_live_on_root_and_both_themes_exist():
    for name in ("--navy-deepest", "--navy", "--panel", "--panel-hi", "--line", "--text", "--muted", "--core", "--core-dim", "--ember", "--ok", "--warn",
                 "--f-display", "--f-body", "--f-mono", "--r", "--rail-w", "--col-max", "--drawer-w", "--gap", "--pad",
                 "--core-rgb", "--core-dim-rgb", "--ember-rgb", "--ok-rgb", "--warn-rgb", "--on-core"):
        assert name in DARK, name
    assert "system-ui" in DARK["--f-display"] and "sans-serif" in DARK["--f-body"] and "monospace" in DARK["--f-mono"]
    assert DARK["--rail-w"] == "190px" and DARK["--col-max"] == "720px"
    for page in (INDEX, LOGIN):
        assert "fonts.googleapis.com/css2" in page and "Oxanium" in page and "IBM+Plex+Sans" in page and "IBM+Plex+Mono" in page
    # Auto follows the device; an explicit Light/Dark choice sets data-theme on <html>. The light block is written
    # twice (media query + attribute) and must not drift apart.
    assert LIGHT_ATTR and LIGHT_ATTR == LIGHT_MEDIA
    assert set(LIGHT_ATTR) >= {"--navy-deepest", "--navy", "--panel", "--panel-hi", "--line", "--text", "--muted", "--core", "--ember", "--ok", "--warn",
                               "--core-rgb", "--ember-rgb", "--on-core"}
    assert LIGHT_ATTR["--navy-deepest"] != DARK["--navy-deepest"]
    assert "color-scheme: dark" in CSS and "color-scheme: light" in CSS


def test_text_has_sufficient_contrast_in_both_themes():
    for label, tokens in (("dark", DARK), ("light", {**DARK, **LIGHT_ATTR})):
        surfaces = [tokens[k] for k in SURFACES]
        for name in ("--text", "--muted", "--core", "--ember", "--ok", "--warn"):
            for surface in surfaces:
                assert _contrast(tokens[name], surface) >= 4.5, (label, name, tokens[name], surface)
        # filled controls: the label on the core-coloured fill (SEND, SIGN IN)
        assert _contrast(tokens["--on-core"], tokens["--core"]) >= 4.5, label


def test_theme_choice_is_auto_light_dark_and_stored_safely():
    assert "jarvis.theme" in THEME and "prefers-color-scheme" in THEME and "data-theme" in THEME
    assert THEME.count("try {") >= 2 and "catch" in THEME            # every localStorage access is wrapped
    assert INDEX.index("/static/theme.js") < INDEX.index("/static/hud.css")  # applied before first paint
    assert LOGIN.index("/static/theme.js") < LOGIN.index("/static/hud.css")
    for value in ('value="auto"', 'value="light"', 'value="dark"'):
        assert value in _pop("settings")
    assert 'id="set-theme"' in INDEX and "JarvisTheme?.set(" in HUD


# ------------------------------------------------------------------------------------------ shell
def test_top_bar_buttons_are_all_text_labelled():
    bar = INDEX[INDEX.index('<header class="topbar">'):INDEX.index("</header>")]
    order = [bar.index(x) for x in ('class="brand"', 'class="state"', 'id="pills"', 'id="clock"', 'id="btn-voice"', 'id="btn-proactive-mute"', 'id="btn-connections"', 'id="btn-settings"')]
    assert order == sorted(order)                      # the mockup's order
    for ident, word in (("btn-voice", "Voice"), ("btn-proactive-mute", "Speaks up"), ("btn-connections", "Connections"), ("btn-settings", "Settings")):
        m = re.search(rf'<button[^>]*id="{ident}"[^>]*>([^<]+)</button>', bar)
        assert m and word in m.group(1), ident        # visible text, not an icon
    assert 'id="state"' in bar and 'id="pills"' in bar and 'id="clock"' in bar and "Online" in bar
    assert "🔔" not in HUD[HUD.index("function renderProactiveMute"):HUD.index("renderProactiveMute();")]
    assert '"Working"' in HUD and '"Listening"' in HUD and '"Speaking"' in HUD and '"Online"' in HUD


def test_rail_has_the_ten_sections_each_with_a_popup_and_the_right_roles():
    assert 'class="label">On request<' in INDEX
    items = re.findall(r'<button type="button" class="rail-item" data-pop="(\w+)">[^<]+?(?:<span class="rail-count" id="rc-(\w+)">[^<]*</span>)?</button>', INDEX)
    assert [a for a, _ in items] == RAIL
    assert len(RAIL) == len(set(RAIL)) == 12       # nine, ten (Activity), eleven (Faults), then twelve (Drawings), deliberately
    assert [a for a, b in items if not b] == RAIL_NO_COUNT and all(a == b for a, b in items if b)   # Activity, Presence, Drawings: no count
    # who gets each item: exactly the manager-only ones sit inside a manager region, so a team console keeps ops, fleet, presence, coming up
    for name in RAIL:
        wrapped = re.search(rf'<!--role:manager--><button type="button" class="rail-item" data-pop="{name}">', INDEX) is not None
        assert wrapped == (name in RAIL_MANAGER_ONLY), name
    for name in RAIL:
        assert f'id="pop-{name}"' in INDEX
    assert "function renderRail()" in HUD and "function setRail(" in HUD
    # mono counts, amber for "needs a look", red for failures - and "Needs you" is capped at three, most urgent first
    assert '[data-level="warn"] .rail-count' in CSS and '[data-level="bad"] .rail-count' in CSS and "font-family: var(--f-mono)" in _block(CSS, ".rail-count {")
    assert '"off"' in HUD and "tests.length - failing" in HUD                      # Fleet "off"; Health passed/total
    assert "needs.sort((a, b) => b.weight - a.weight)" in HUD and "needs.slice(0, 3)" in HUD


def test_every_dashboard_panel_is_reachable_in_a_popup():
    for name, ids in DASHBOARD_PANELS.items():
        pop = _pop(name)
        for ident in ids:
            assert f'id="{ident}"' in pop, (name, ident)
    for name in ("demo", "settings", "connections"):
        assert f'id="pop-{name}"' in INDEX
    # nothing from the old dashboard is left behind on the page itself
    for gone in ('id="dtabs"', 'id="btn-dashboard"', 'data-dtab=', "dash-open"):
        assert gone not in INDEX
    assert "Awaiting your approval" in _pop("approvals") and "Run tests now" in _pop("health")
    assert "Customer watch" in _pop("finance") and "Vehicle tracking" in _pop("fleet")
    assert "Followers and reviews" in _pop("presence") and "Dated reminders" in _pop("upcoming")


def test_centre_column_has_core_hint_needs_you_conversation_and_message_box():
    assert "--col-max: 720px" in CSS and "min(var(--col-max), 100%)" in CSS
    order = [INDEX.index(s) for s in ('id="core-btn"', 'id="caption"', 'id="needs-list"', 'id="conversation"', 'id="composer"')]
    assert order == sorted(order)
    comp = INDEX[INDEX.index('<form class="composer"'):INDEX.index("</form>", INDEX.index('<form class="composer"'))]
    order = [comp.index(x) for x in ('id="btn-shortcuts"', 'id="input"', 'id="btn-mic"', 'id="btn-send"')]
    assert order == sorted(order) and "M4 7h16M4 12h16M4 17h10" in comp               # hamburger menu button first, SEND last
    assert ">SEND</button>" in comp and ">STOP</button>" in comp
    for ident in ("btn-shortcuts", "quick", "input", "btn-mic", "btn-send", "btn-stop", "btn-attach", "reply-hint", "reply-hint-forget", "orb-badge"):
        assert f'id="{ident}"' in INDEX, ident
    owners_page = re.sub(r"<!--role:team-->.*?<!--/role:team-->", "", INDEX, flags=re.S)  # (the team version has its own shortcuts)
    labels = re.findall(r'role="menuitem" class="chip-btn" data-q="[^"]*">([^<]+)<', owners_page)
    assert labels == ["Briefing", "Wrap-up", "Team review", "Business health", "Cash flow", "Where's everyone?", "Stock", "Customers", "Marketing"]
    # Send becomes Stop while a reply streams; Stop aborts the in-flight request on the HTTP fallback path
    assert '$("#btn-send").hidden = busy' in HUD and '$("#btn-stop").hidden = !busy' in HUD
    assert "streamCtl.abort()" in HUD and "signal: ctl.signal" in HUD
    # the existing learned-reply suggestion and attach button are still wired
    assert '$("#btn-attach").addEventListener("click"' in HUD and "async function rsFetch()" in HUD


def test_core_follows_state_clicks_to_listen_and_is_static_under_reduced_motion():
    assert 'id="reactor"' in INDEX and "JarvisCore?.mount(" in HUD and '$("#core-btn").addEventListener("click"' in HUD
    for state in ("idle", "listening", "thinking", "speaking"):
        assert state in CORE
    assert "prefers-reduced-motion" in CORE and "prefers-reduced-motion" in HUD
    assert "--core-rgb" in CORE and "--ember-rgb" in CORE and "--ok-rgb" in CORE and "--core-dim-rgb" in CORE   # colours come from the tokens
    assert "Math.sin(a * 6 + t * 5) * Math.sin(a * 3 - t * 3)" in CORE                # the mockup's waveform ring, ported
    assert "JarvisCore.mount(" in LOGIN and 'id="login-core"' in LOGIN             # the sign-in page animates it too


def test_sign_in_page_keeps_the_same_authentication():
    assert 'method="post" action="/login"' in LOGIN and 'name="password"' in LOGIN and 'type="password"' in LOGIN
    assert "Identify yourself, sir." in LOGIN and ">SIGN IN<" in LOGIN and "SALTS FIRE AND SECURITY" in LOGIN and 'placeholder="Password"' in LOGIN
    assert "width: 180px" in _block(CSS, ".login-core {")


# ------------------------------------------------------------------------------------------ the drawer
def test_one_drawer_that_can_never_be_wider_than_the_window():
    assert INDEX.count('<aside class="drawer"') == 1 and 'id="drawer-close"' in INDEX and ">Close</button>" in INDEX
    rule = _block(CSS, ".drawer {")
    assert "width: min(var(--drawer-w), 100%)" in rule and "max-width: 100%" in rule and "overflow: hidden" in rule
    assert "min-width: 0" in rule or ".drawer > * { min-width: 0; }" in CSS
    assert "overflow-x: hidden" in _block(CSS, ".drawer-body {")
    # closed: off-screen, and out of the tab order
    assert "visibility: hidden" in rule and "visibility: visible" in _block(CSS, ".drawer.open {")
    assert ".scrim {" in CSS and 'id="scrim"' in INDEX
    # closed by the Close button, Escape, or a click outside; focus is trapped while open and restored after
    assert '$("#drawer-close").addEventListener("click"' in HUD and '$("#scrim").addEventListener("click"' in HUD
    assert 'if (Drawer.isOpen()) { e.preventDefault(); Drawer.close(); }' in HUD
    assert "e.key !== \"Tab\"" in HUD and "back.focus(" in HUD
    assert "Discard unsaved connection changes?" in HUD
    # the rail, top-bar buttons and any [data-pop] element open pop-ups through that one component
    assert 'document.addEventListener("click", (e) => { const b = e.target.closest("[data-pop]")' in HUD
    for pop in ("approvals", "comms", "issues", "health", "ops", "fleet", "finance", "presence", "upcoming", "demo", "settings", "connections"):
        assert f'"{pop}"' in HUD[HUD.index("const POPS"):HUD.index("const Drawer")]
    # Escape stops a streaming reply only when it did not just close something
    assert "!e.defaultPrevented" in HUD


def test_connections_is_a_list_of_integrations_each_opening_its_own_form_with_a_back_link():
    assert "data-open-section" in HUD and "data-back" in HUD and "‹ All connections" in HUD
    assert "showList()" in HUD and "openSection(id)" in HUD and "backToList()" in HUD
    # the existing forms/fields/test/save machinery is reused, not rewritten
    for needle in ("renderField(sectionId, f)", "async test(sectionId)", "async save()", '"/api/settings"', "data-show-advanced", "cronFromParts"):
        assert needle in HUD, needle
    assert "set-save-bar" in INDEX and "btn-settings-save" in INDEX and "btn-settings-cancel" in INDEX


def test_staff_report_link_is_copied_never_shown():
    assert 'id="btn-copy-report"' in INDEX and "Copy staff report link" in INDEX
    assert 'id="report-link"' not in INDEX and "report-link" not in HUD
    assert "navigator.clipboard.writeText(link)" in HUD
    assert "staffReportLink" in HUD


def test_fleet_shows_vehicles_or_a_clear_not_connected_state():
    assert "Vehicle tracking is not connected" in HUD and "function renderFleetStatus()" in HUD
    assert 'connections?.["Vehicle tracking"]' in HUD


def test_narrow_layout_turns_the_rail_into_a_scrolling_pill_strip():
    phone = CSS[CSS.index("@media (max-width: 760px) {"):]
    rail = re.search(r"\.rail \{([^}]*)\}", phone).group(1)
    assert "flex-direction: row" in rail and "overflow-x: auto" in rail
    assert ".rail .label { display: none; }" in phone and ".clock { display: none; }" in phone
    # Phase 4a: the phone core is no longer pinned to the mockup's 110px - it is LARGE ("a large core to talk to") and grows
    # with the screen height, with the mockup's 110px kept as its floor. (Stricter: a floor AND a fluid size, not a constant.)
    assert "--core-size: clamp(110px," in phone and "width: var(--core-size)" in phone
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in phone     # Voice / Speaks up / Connections / Settings: one row
    assert "grid-template-columns: minmax(0, 1fr)" in phone       # one column: no side rail
    assert "font-size: 16px" in phone                              # 16px: iOS zooms on focus below it
    assert "env(safe-area-inset-bottom)" in phone
    # the top bar wraps instead of overflowing
    assert "flex-wrap: wrap" in _block(CSS, "\n.topbar {")


def test_phone_keeps_the_conversation_and_message_box_in_view():
    assert 'classList.add("has-chat")' in HUD and 'classList.remove("has-chat")' in HUD
    assert ".has-chat .core-btn" in CSS and ".composing .core-block" in CSS
    assert 'classList.add("composing")' in HUD

def test_unpinning_the_core_waits_for_the_press_that_blurred_the_message_box():
    """A tap above the box blurs it at mousedown; un-collapsing then (the core reappears and everything under it moves down) made
    the release land on a different element, so the click was swallowed. The blur handler must defer while a press is open."""
    block = HUD[HUD.index("const compose = {"):HUD.index('$("#input").addEventListener("keydown"')]
    assert 'document.addEventListener(t, pressStart, true)' in block and 'document.addEventListener(t, pressEnd, true)' in block
    assert '"pointerdown", "touchstart", "mousedown"' in block and '"click", "pointercancel", "touchcancel"' in block
    assert "if (!compose.open || performance.now() - compose.at > 3000)" in block      # no press: un-collapse at once, as before
    assert "setTimeout(() => finish(), 1200)" in block                                 # a press that never clicks cannot strand the core
    assert 'classList.remove("composing")' in block and 'classList.add("composing")' in block
    # the blur handler must not remove the class directly any more
    assert '$("#input").addEventListener("blur", () => document.body.classList.remove("composing"))' not in HUD
    assert ".composing .core-block { display: none; }" in CSS                        # the keyboard-open collapse itself is unchanged


def test_touch_targets_and_motion_and_contrast_guards():
    assert "min-height: 44px" in CSS[CSS.index("(pointer: coarse)"):]
    assert "prefers-reduced-motion" in CSS and "prefers-reduced-motion" in HUD
    assert ":focus-visible" in CSS
    # Closed overlays must leave the tab order, not just fade out.
    assert "visibility: hidden" in CSS[CSS.index(".display {"):CSS.index(".display.open")]
    assert "visibility: hidden" in CSS[CSS.index(".drawer {"):CSS.index(".drawer.open")]


def test_ask_popup_stays_on_screen_and_above_the_phone_composer():
    phone = ASK_CSS[ASK_CSS.index("@media (max-width: 760px)"):]
    assert "max-height: calc(100dvh" in phone and "overflow-y: auto" in phone


# ------------------------------------------------------------------ scheduled checks: ONE summary row, a bounded panel
def test_scheduled_checks_are_one_summary_row_in_the_conversation_head_with_a_separate_bounded_panel():
    convo = INDEX[INDEX.index('<section class="convo"'):INDEX.index("</section>", INDEX.index('<section class="convo"'))]
    head = convo[convo.index('<div class="convo-head">'):convo.index('<div class="activity"')]
    assert 'id="activity-toggle"' in head and 'aria-controls="activity"' in head and 'aria-expanded="false"' in head   # the one row, above the log
    assert convo.count('id="activity-toggle"') == 1
    assert convo.index('id="activity"') < convo.index('id="conversation"')                # the panel is not a child of the chat log
    log = convo[convo.index('id="conversation"'):]
    assert "activity" not in log                                                           # ...and the lines are never inside it
    panel = CSS[CSS.index(".activity {"):CSS.index("\n", CSS.index(".activity {") + 200)]
    assert "position: absolute" in panel and "max-height: 40%" in panel and "overflow-y: auto" in panel   # bounded, scrolls inside itself
    assert "overscroll-behavior: contain" in panel
    assert ".activity[hidden] { display: none; }" in CSS
    assert "max-height: 40vh" not in CSS                                                    # the old unbounded-in-flow list is gone


def test_the_checks_summary_row_and_lines_are_44px_tap_targets_and_flag_trouble_in_the_accent_colours():
    assert ".auto-all, .auto summary { min-height: 44px; }" in CSS[CSS.index("(pointer: coarse)"):]
    assert '.auto-all[data-level="bad"] .auto-all-text { color: var(--warn); }' in CSS
    assert '.auto-all[data-level="warn"] .auto-all-text { color: var(--ember); }' in CSS
    assert "-webkit-line-clamp: 2" in CSS[CSS.index(".auto-all .auto-all-text"):]          # long text wraps to two lines, never a third


def test_the_checks_refresh_never_moves_the_conversation_and_keeps_what_the_owner_opened():
    body = HUD[HUD.index("function renderActivity("):HUD.index('$("#activity-toggle")?.addEventListener("click"')]
    assert "#conversation" not in body and "scrollTop = 1e9" not in body                   # it does not touch the conversation's scroll
    assert "const stamp = JSON.stringify(jobs); if (stamp === activityShown) return;" in body   # nothing new: nothing rebuilt
    assert "const keep = box.scrollTop;" in body and "box.scrollTop = keep;" in body       # the panel's own scroll survives a rebuild
    assert "activityOpen.has(job.key)" in body and "syncActivityOpen();" in body           # open lines and the open panel are restored
    # the conversation sticks to its newest message across size changes, unless the owner scrolled up
    assert "new ResizeObserver(() => { if (pinned) log.scrollTop = 1e9; })" in HUD
    # the summary says which check needs a look
    assert "needs a look" in HUD and "nothing to report" in HUD
