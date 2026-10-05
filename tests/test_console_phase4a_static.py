"""Phase 4a, always-on static checks (they need no browser, so CI runs them): the Approvals inbox and Memory pop-up are
wired into the one-drawer system, the phone layout rules are in the stylesheet, and the front end has no route to approve,
edit or retry anything except a person's click. The real-browser behaviour is in test_console_browser_phase4a.py."""
from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "jarvis" / "web"
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
CSS = (WEB / "hud.css").read_text(encoding="utf-8")
HUD = (WEB / "hud.js").read_text(encoding="utf-8")
MEMORY = (WEB / "memory.js").read_text(encoding="utf-8")


def _pop(name: str) -> str:
    start = INDEX.index(f'<section class="pop" id="pop-{name}"')
    return INDEX[start:INDEX.index("</section>", start)]


# ----------------------------------------------------------------------------------------------- the Approvals inbox
def test_the_approvals_pop_up_has_pending_failed_and_suggestions():
    pop = _pop("approvals")
    for ident in ("approvals", "approvals-panel", "failed-actions", "failed-panel", "suggestions", "approvals-empty"):
        assert f'id="{ident}"' in pop, ident
    assert "Awaiting your approval" in pop and "Failed - needs a look" in pop


def test_one_card_renderer_with_approve_edit_dont_send_and_retry():
    assert "function cardHtml(v, where" in HUD
    body = HUD[HUD.index("function cardHtml(v, where"):HUD.index("function syncCards(")]
    for needle in ('data-act="approve"', 'data-act="edit"', 'data-act="deny"', "Don't send", 'data-act="retry"', ">Retry<", ">Approve<", ">Edit<"):
        assert needle in body, needle
    assert "didn't go through" in body and "v.error" in body                         # a failed action shows its error
    assert "Nothing is sent or changed until you press Approve" in body
    # the same renderer fills the pop-up AND the conversation
    assert 'syncCards($("#approvals"), pending, "drawer")' in HUD and 'syncCards(wrap.querySelector(".appr-slot"), [v], "chat")' in HUD
    assert 'class = "msg assistant appr-msg"' in HUD.replace("className", "class")


def test_the_front_end_only_acts_on_a_click_and_every_call_goes_to_the_owner_endpoints():
    # the only places that POST to the approval endpoints
    calls = re.findall(r"api\(`/api/approvals/\$\{id\}/(\$\{act\}|edit|retry)`", HUD)
    assert sorted(calls) == ["${act}", "edit", "retry"]
    assert "async function decide(id, act)" in HUD and "async function saveEdit(" in HUD and "async function retryAction(" in HUD
    # decide / edit / retry are reached from the card click handler (and the existing spoken "approve")...
    click = HUD[HUD.index('const b = e.target.closest(".appr-card [data-act]")'):HUD.index("document.addEventListener(\"submit\"")]
    for needle in ("decide(id, b.dataset.act)", "retryAction(id, b)", 'saveEdit(id, card.querySelector(".appr-edit"), b)'):
        assert needle in click, needle
    assert len(re.findall(r"\bretryAction\(", HUD)) == 2 and len(re.findall(r"\bsaveEdit\(", HUD)) == 2     # a call + its definition
    # ...and nothing the page loads or receives (an event, an inbox payload) triggers any of them
    for event_handler in ('case "approvals": S.approvals = d; loadInboxSoon(); break;',):
        assert event_handler in HUD
    inbox = HUD[HUD.index("async function loadInbox()"):HUD.index("const loadInboxSoon")]
    assert "decide(" not in inbox and "retryAction(" not in inbox and "saveEdit(" not in inbox and "method: \"POST\"" not in inbox
    sync = HUD[HUD.index("function syncChatCards()"):HUD.index("let inboxTimer")]
    assert "decide(" not in sync and "api(" not in sync                              # drawing a card never calls the server
    # Edit sends only the fields that were changed, as JSON, to the owner endpoint
    assert "if (f.value !== f.defaultValue) changes[f.name] = f.value" in HUD


def test_the_pending_inbox_is_loaded_from_the_redacted_endpoint_not_the_raw_event():
    assert 'api("/api/approvals/inbox")' in HUD
    assert "payload" not in HUD[HUD.index("function cardHtml(v, where"):HUD.index("function syncCards(")]    # cards use the server's `details`, not raw payloads


def test_the_rail_still_has_exactly_the_nine_dashboard_sections_and_memory_lives_in_settings():
    rail = INDEX[INDEX.index('<nav class="rail"'):INDEX.index("</nav>", INDEX.index('<nav class="rail"'))]
    assert re.findall(r'data-pop="(\w+)"', rail) == ["approvals", "comms", "issues", "health", "ops", "fleet", "finance", "presence", "upcoming"]
    assert 'data-pop="memory"' not in rail and 'id="btn-open-memory"' in _pop("settings") and 'data-pop="memory"' in _pop("settings")
    assert '"memory"' in HUD[HUD.index("const POPS"):HUD.index("const Drawer")] and 'JarvisMemory?.load()' in HUD
    assert INDEX.index("/static/memory.js") < INDEX.index("/static/hud.js")


# ----------------------------------------------------------------------------------------------- the Memory pop-up
def test_memory_pop_up_markup_and_script():
    pop = _pop("memory")
    for ident in ("memory-notes", "memory-learned", "memory-replies", "memory-error"):
        assert f'id="{ident}"' in pop, ident
    for title in ("Things Jarvis should know", "Things Jarvis has learned", "Learned replies"):
        assert title in pop, title
    for needle in ('data-mem="edit"', 'data-mem="ask-delete"', 'data-mem="delete"', 'data-mem="keep"', 'data-mem="save"', "Delete this?", "/api/memory"):
        assert needle in MEMORY, needle
    # it has no connection to the approvals queue, and the approvals code none to the memory endpoints
    code = MEMORY[MEMORY.index("*/") + 2:]                                               # the file's code, not its header comment
    assert "/api/approvals" not in code and "approv" not in code.lower() and "decide" not in code
    assert "/api/memory" not in HUD


# ----------------------------------------------------------------------------------------------- phone layout
def test_phone_layout_rules():
    phone = CSS[CSS.index("@media (max-width: 760px) {"):]
    for edge in ("top", "right", "bottom", "left"):
        assert f"env(safe-area-inset-{edge})" in phone, edge                           # every edge has its safe-area inset
    assert "padding-block: 8px max(14px, env(safe-area-inset-bottom))" in phone       # the composer clears the home bar
    # a large core that grows with the screen (floor: the mockup's 110px)
    assert "--core-size: clamp(110px, calc(var(--app-h, 100dvh) * .22), 210px)" in phone
    # pop-ups are full-screen with a Close at the thumb as well as at the top
    assert ".drawer { width: 100%; max-width: 100%; border-left: 0;" in phone and ".drawer-foot { display: block; }" in phone
    assert 'id="drawer-close-bottom"' in INDEX and 'id="drawer-close"' in INDEX
    assert '$("#drawer-close-bottom").addEventListener("click", () => Drawer.close())' in HUD
    # Approvals stays pinned at the left of the scrolling strip; the top bar is two rows (four equal buttons)
    assert '.rail-item[data-pop="approvals"] { position: sticky; left: 0;' in phone
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in phone
    # the message box: field on its own row, SEND fills the rest of the row of controls
    narrow = CSS[CSS.index("@media (max-width: 480px) {"):]
    assert ".composer .field { flex: 1 1 100%; order: -1; }" in narrow and ".composer .btn-send { flex: 1 1 0;" in narrow
    # the keyboard behaviour from Phase 1/3 is kept
    assert "viewport-fit=cover" in INDEX and "interactive-widget=resizes-content" in INDEX
    assert 'document.documentElement.style.setProperty("--app-h", window.visualViewport.height + "px")' in HUD
    assert "body.app { height: 100vh; height: var(--app-h, 100dvh);" in CSS and ".composing .core-block { display: none; }" in CSS
    # 44px minimum on phones and coarse pointers, including the new card and memory controls and Good / Wrong
    touch = CSS[CSS.index("@media (max-width: 760px), (pointer: coarse)"):]
    assert "min-height: 44px" in touch and ".reply-hint button, .msg .fb button { min-width: 44px; min-height: 44px; }" in touch
    assert ".appr-edit input[type=text], input.mem-input { min-height: 44px; }" in touch
    # the new inputs are 16px on phones (iOS zooms the page on focus below that)
    assert ".appr-edit input, .appr-edit textarea, .mem-input { font-size: 16px; }" in phone


def test_light_and_dark_tokens_are_used_by_the_new_components_not_hard_coded_colours():
    new = CSS[CSS.index("/* ---------- approval cards"):CSS.index("/* ---------- responsive: phones")]
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", new), "new components must use the theme tokens"
