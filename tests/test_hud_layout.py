"""Static checks on the HUD's responsive layout (no JS/browser runner in this repo, so - like test_hud_bargein.py -
these read index.html / hud.css / hud.js and assert the guards that keep the phone layout usable are present)."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "jarvis" / "web"
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
CSS = (WEB / "hud.css").read_text(encoding="utf-8")
HUD = (WEB / "hud.js").read_text(encoding="utf-8")
ASK_CSS = (WEB / "ask.css").read_text(encoding="utf-8")


def test_viewport_handles_notches_and_the_on_screen_keyboard():
    assert "viewport-fit=cover" in INDEX and "interactive-widget=resizes-content" in INDEX
    # The app shell is dvh-tall (URL bar) and follows visualViewport (iOS keyboard); never a fixed 100vh-62px grid.
    assert "100dvh" in CSS and "--app-h" in CSS and "calc(100vh - 62px)" not in CSS
    assert "visualViewport" in HUD and '--app-h' in HUD


def test_every_dashboard_panel_belongs_to_a_tab_and_every_tab_has_panels():
    tabs = set(re.findall(r'class="dtab"[^>]*data-dtab="(\w+)"', INDEX))
    assert tabs == {"chat", "comms", "issues", "ops", "fleet", "finance"}
    panel_tabs = set(re.findall(r'class="panel" data-dtab="(\w+)"', INDEX))
    assert panel_tabs == tabs - {"chat"}
    assert INDEX.count('class="panel" data-dtab=') == 12  # nothing left out of the tabbed layout
    assert 'data-dash-tab="chat"' in INDEX
    assert "function setDashTab(" in HUD and '#dtabs' in HUD


def test_narrow_layout_is_not_defeated_by_selector_specificity():
    # The three-column rule is `.dash-open .hud`; the narrow override must be at least as specific or it never applies.
    narrow = CSS[CSS.index("@media (max-width: 1149px)"):]
    assert ".dash-open .hud {" in narrow[:600]
    assert re.search(r"\.dash-open \.hud \{ grid-template-columns: minmax\(240px", CSS)  # tracks can shrink (no overlap)


def test_phone_shows_the_conversation_and_pins_the_composer():
    assert 'classList.add("has-chat")' in HUD and 'classList.remove("has-chat")' in HUD
    phone = CSS[CSS.index("@media (max-width: 767px), (max-height: 520px)"):]
    assert ".has-chat #transcript-wrap" in phone and "font-size: 16px" in phone  # 16px: iOS zooms on focus below it
    assert "env(safe-area-inset-bottom)" in phone


def test_touch_targets_and_motion_and_contrast_guards():
    assert "min-height: 44px" in CSS[CSS.index("(pointer: coarse)"):]
    assert "prefers-reduced-motion" in CSS and "prefers-reduced-motion" in HUD
    assert ":focus-visible" in CSS
    # Closed overlays must leave the tab order, not just fade out.
    assert "visibility: hidden" in CSS[CSS.index(".display {"):CSS.index(".display.open")]
    assert "visibility: hidden" in CSS[CSS.index(".drawer {"):CSS.index(".drawer.open")]
    assert "--muted-dim: #7d8797" in CSS


def test_ask_popup_stays_on_screen_and_above_the_phone_composer():
    phone = ASK_CSS[ASK_CSS.index("@media (max-width: 767px)"):]
    assert "max-height: calc(100dvh" in phone and "overflow-y: auto" in phone
