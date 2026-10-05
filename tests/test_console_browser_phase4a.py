"""Real-browser checks of console redesign Phase 4a: the Approvals inbox, the phone layout and the Memory pop-up.

Headless Chrome through Playwright (skipped when it is not installed, like test_console_browser.py, whose fixtures it
shares), at 1280 / 800 / 400px in both themes:

* Approvals: every pending action is a card in the pop-up AND in the conversation showing exactly what will happen, with
  Approve / Edit / Don't send; a failed action shows its error and a Retry. Approve, Don't send, Edit (a NEW pending action
  that still needs its own Approve) and Retry (a new pending action; nothing re-runs without a click) work from both places,
  and no secret ever reaches a card.
* Phone (400px): a large core, Approvals one tap away, full-screen pop-ups with a Close at the thumb, 44px touch targets,
  the message box controls all on screen, no horizontal scroll, and the message box still above the on-screen keyboard.
* Memory: the pop-up lists what Jarvis has learned, and each entry can be reworded or deleted (with a confirmation).

Set JARVIS_SHOTS=<folder> to also save a screenshot of each surface there.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright.sync_api")

from jarvis.services.reply_suggestions import MIN_USES  # noqa: E402
from tests.fakes import message, text_block, tool_block  # noqa: E402
from tests.test_console_browser import SIZES, THEMES, _no_hscroll, browser  # noqa: E402,F401
from tests.test_console_browser_phase3 import _page, _shot, serve  # noqa: E402,F401

SECRET = "sk-ant-api03-abcdefghijklmnop1234567890"
EMAIL = {"to": ["dan@kestrel.example.com"], "cc": [], "subject": "Quote Q-1042",
         "body": f"Hi Dan,\n\nThe quote is attached. (Ref token {SECRET})\n\nKind regards,\nSalts"}
NOTES = "First County Monitoring handle our out-of-hours. | Alex prefers quotes sent before 10am"


class FakeMail:
    demo = False

    def __init__(self):
        self.sent: list[tuple] = []

    async def send_mail(self, to, subject, body_html, cc=None, bcc=None, sensitivity=None):
        self.sent.append((to, subject, body_html))


class FakeFSM:
    demo = False

    def __init__(self):
        self.writes: list[tuple] = []
        self.fail = True

    async def write(self, method, path, body=None):
        self.writes.append((method, path, body))
        if self.fail:
            raise RuntimeError("Salts FSM did not accept the change (HTTP 422): site name already exists")
        return {"id": "site-9"}


def _seed(j, history=False):
    j.actions.mail, j.actions.fsm = FakeMail(), FakeFSM()
    ids = {
        "email": j.db.create_action("email_send", "Send the Kestrel quote to Dan", EMAIL),
        "fsm": j.db.create_action("fsm_write", "Create customer 'Acme Alarms'",
                                  {"method": "POST", "path": "/customers", "body": {"name": "Acme Alarms", "contact": "Pat"}}),
    }
    failed = []
    for n, name in ((71, "Unit 4"), (72, "Unit 5")):                                     # failed actions, like #71 and #72
        a = j.db.create_action("fsm_write", f"Create site '{name}'", {"method": "POST", "path": "/sites", "body": {"name": name}})
        j.db.set_action_status(a, "failed", f"Salts FSM did not accept the change (HTTP 422): site name already exists ({SECRET})")
        failed.append(a)
    ids["failed"] = failed
    if history:
        j.db.add_transcript("user", "Anything I need to chase today?")
        j.db.add_transcript("assistant", "Two quotes are waiting on replies. Want me to draft the chasers?")
    return ids


def _seed_memory(j):
    j.db.remember("The Ilkley site key safe is held by reception on Mondays.")
    for _ in range(MIN_USES):
        j.reply_suggestions.record("yes do that", "typed", "offer")


def _jarvis_wants_to_email(j, subject="Second"):
    """Queue the next chat turn so Jarvis calls the (approval-gated) email_send tool - the real path for 'Jarvis wants to send'."""
    j.client.beta.messages.script.extend([
        message([tool_block("email_send", {"to": ["a@b.example.com"], "subject": subject, "body": "Hello from Jarvis."})], "tool_use"),
        message([text_block("I've queued that email for your approval.")])])


def _open_pop(page, name):
    sel = f'.rail-item[data-pop="{name}"]'
    page.tap(sel) if page.evaluate("navigator.maxTouchPoints > 0") else page.click(sel)
    page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
    page.wait_for_timeout(350)


def _wait_cards(page, container="#approvals", n=1):
    page.wait_for_function(f"document.querySelectorAll('{container} .appr-card').length >= {n}", timeout=10000)


# ----------------------------------------------------------------------------------------------- Approvals: rendering
@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_the_approvals_pop_up_shows_pending_and_failed_cards(serve, browser, width, height, scheme):
    srv, j, _ = serve()
    _seed(j)
    ctx, page = _page(browser, srv.url, width, height, scheme)
    try:
        _open_pop(page, "approvals")
        _wait_cards(page, "#approvals", 2)
        email = page.locator('#approvals .appr-card[data-id="1"]')
        text = email.inner_text()
        assert "dan@kestrel.example.com" in text and "Quote Q-1042" in text and "The quote is attached" in text   # exactly what will be sent
        assert SECRET not in page.content() and "[REDACTED]" in text                                              # no secret on a card
        assert [b.inner_text() for b in email.locator(":scope > .row .btn").all()] == ["Approve", "Edit", "Don't send"]
        assert email.locator('[data-act="approve"]').is_visible() and email.locator('[data-act="deny"]').is_visible()
        assert "Nothing is sent or changed until you press Approve" in text
        # fsm_write: what changes, in words and the exact body
        assert "POST /customers" in page.locator('#approvals .appr-card[data-id="2"]').inner_text()
        # the failed actions show their error and a Retry - and no Approve
        failed = page.locator("#failed-actions .appr-card")
        assert failed.count() == 2
        first = failed.first.inner_text()
        assert "didn't go through" in first and "HTTP 422" in first and "site name already exists" in first and SECRET not in first
        assert failed.first.locator('[data-act="retry"]').is_visible() and failed.first.locator('[data-act="approve"]').count() == 0
        assert page.inner_text("#failed-count") == "2" and page.inner_text("#approvals-count") == "2"
        # the rail: pending + failed, red because something failed
        page.keyboard.press("Escape")
        assert page.eval_on_selector('.rail-item[data-pop="approvals"]', "e => [e.querySelector('.rail-count').textContent, e.dataset.level]") == ["4", "bad"]
        assert _no_hscroll(page)[0] <= width
        _open_pop(page, "approvals")
        _shot(page, f"p4a-approvals-{width}-{scheme}")
        page.locator("#failed-actions").scroll_into_view_if_needed()
        fit = page.evaluate("""() => { const d = document.getElementById('drawer'); return [d.scrollWidth <= d.offsetWidth + 1, d.offsetWidth <= innerWidth]; }""")
        assert fit == [True, True]
        assert page.errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_a_waiting_action_is_also_a_card_in_the_conversation(serve, browser, width, height, scheme):
    srv, j, _ = serve()
    _seed(j, history=True)
    ctx, page = _page(browser, srv.url, width, height, scheme)
    try:
        page.wait_for_selector("#conversation .appr-msg .appr-card", timeout=10000)
        cards = page.locator("#conversation .appr-msg .appr-card")
        assert cards.count() == 2                                       # the two waiting actions (failed ones stay in the pop-up)
        email = page.locator('#conversation .appr-card[data-id="1"]')
        email.scroll_into_view_if_needed()
        text = email.inner_text()
        assert "dan@kestrel.example.com" in text and "Quote Q-1042" in text and SECRET not in page.content()
        assert [b.inner_text() for b in email.locator(":scope > .row .btn").all()] == ["Approve", "Edit", "Don't send"]
        # history comes first, the cards after it
        order = page.evaluate("""() => [...document.querySelectorAll('#conversation > .msg')].map(m => m.classList.contains('appr-msg') ? 'card' : 'msg')""")
        assert order == ["msg", "msg", "card", "card"]
        # the rail and the badge on the core count what is waiting (+ failed)
        assert page.inner_text("#orb-badge") == "4"
        _shot(page, f"p4a-chat-card-{width}-{scheme}")
        assert _no_hscroll(page)[0] <= width
    finally:
        ctx.close()


def test_in_the_empty_welcome_state_waiting_actions_stay_out_of_the_conversation(serve, browser):
    srv, j, _ = serve()
    _seed(j, history=False)
    ctx, page = _page(browser, srv.url, 400, 820, "dark", has_touch=True, is_mobile=True)
    try:
        page.wait_for_function("document.getElementById('orb-badge').textContent === '4'")
        assert page.locator("#conversation .appr-msg").count() == 0     # the core is not crowded out by cards on a phone
        # ...but a NEW action Jarvis queues while you watch does appear as a card, right after his reply
        _jarvis_wants_to_email(j)
        page.fill("#input", "Email a@b.example.com a hello")
        page.keyboard.press("Enter")
        page.wait_for_selector('#conversation .appr-card:has-text("Second")', timeout=15000)
        assert page.locator("#conversation .appr-msg").count() == 1
        assert page.locator("#conversation .appr-card .appr-kind").text_content() == "Email"
        assert [b.inner_text() for b in page.locator("#conversation .appr-card > .row .btn").all()] == ["Approve", "Edit", "Don't send"]
        assert j.actions.mail.sent == []                                   # queued, not sent
    finally:
        ctx.close()


# ----------------------------------------------------------------------------------------------- Approvals: acting
def test_approve_from_the_chat_card_sends_once_and_the_card_becomes_done(serve, browser):
    srv, j, _ = serve()
    ids = _seed(j, history=True)
    ctx, page = _page(browser, srv.url, 1280, 800)
    try:
        page.wait_for_selector("#conversation .appr-card", timeout=10000)
        assert j.actions.mail.sent == []                                # nothing sent just by being on screen
        card = page.locator(f'#conversation .appr-card[data-id="{ids["email"]}"]')
        card.locator('[data-act="approve"]').click()
        page.wait_for_selector(f'#conversation .appr-card[data-id="{ids["email"]}"][data-status="done"]', timeout=10000)
        assert len(j.actions.mail.sent) == 1
        to, subject, html = j.actions.mail.sent[0]
        assert to == ["dan@kestrel.example.com"] and subject == "Quote Q-1042" and SECRET in html   # the real payload went out, redaction is display-only
        assert j.db.get_action(ids["email"])["status"] == "done"
        assert page.locator(f'#conversation .appr-card[data-id="{ids["email"]}"] [data-act]').count() == 0   # no buttons left
        assert "Done" in card.inner_text()
    finally:
        ctx.close()


def test_dont_send_cancels_and_the_card_says_not_sent(serve, browser):
    srv, j, _ = serve()
    ids = _seed(j, history=True)
    ctx, page = _page(browser, srv.url, 1280, 800)
    try:
        page.wait_for_selector("#conversation .appr-card", timeout=10000)
        page.locator(f'#conversation .appr-card[data-id="{ids["email"]}"] [data-act="deny"]').click()
        page.wait_for_selector(f'#conversation .appr-card[data-id="{ids["email"]}"][data-status="denied"]', timeout=10000)
        assert "Not sent" in page.inner_text(f'#conversation .appr-card[data-id="{ids["email"]}"]')
        assert j.actions.mail.sent == [] and j.db.get_action(ids["email"])["status"] == "denied"
        _open_pop(page, "approvals")
        assert page.locator(f'#approvals .appr-card[data-id="{ids["email"]}"]').count() == 0
    finally:
        ctx.close()


@pytest.mark.parametrize("width,height", [(1280, 800), (400, 820)])
def test_edit_saves_a_new_pending_action_and_only_the_edited_version_can_be_approved(serve, browser, width, height):
    srv, j, _ = serve()
    ids = _seed(j)
    touch = {"has_touch": True, "is_mobile": True} if width < 760 else {}
    ctx, page = _page(browser, srv.url, width, height, "dark", **touch)
    try:
        _open_pop(page, "approvals")
        _wait_cards(page, "#approvals", 2)
        card = page.locator(f'#approvals .appr-card[data-id="{ids["email"]}"]')
        card.locator('[data-act="edit"]').click()
        form = card.locator(".appr-edit")
        assert form.is_visible()
        assert page.locator(f"#ef-drawer-{ids['email']}-body").input_value().count("[REDACTED]") == 1   # the form is prefilled from the redacted view
        # a bad address is refused with a plain message, and nothing changes
        form.locator('[name="to"]').fill("not-an-address")
        form.locator('[data-act="edit-save"]').click()
        page.wait_for_selector(f'#approvals .appr-card[data-id="{ids["email"]}"] .appr-error:not([hidden])')
        assert "doesn't look like an email address" in form.locator(".appr-error").inner_text()
        assert j.db.get_action(ids["email"])["status"] == "pending"
        # a real edit: change only the subject
        form.locator('[name="to"]').fill("dan@kestrel.example.com")
        assert form.locator(".appr-error").is_hidden()                    # typing again clears the last message
        form.locator('[name="subject"]').fill("Revised quote Q-1042")
        _shot(page, f"p4a-edit-{width}")
        form.locator('[data-act="edit-save"]').click()
        page.wait_for_function(f"!document.querySelector('#approvals .appr-card[data-id=\"{ids['email']}\"]')", timeout=10000)
        new_id = j.db.get_action(ids["email"])["superseded_by"]
        assert new_id and j.actions.mail.sent == []                      # saved - and still nothing sent
        edited = page.locator(f'#approvals .appr-card[data-id="{new_id}"]')
        edited.wait_for(timeout=10000)
        assert "Revised quote Q-1042" in edited.inner_text() and "Edited" in edited.inner_text()
        assert j.db.get_action(new_id)["status"] == "pending" and j.db.get_action(ids["email"])["status"] == "denied"
        # the untouched body kept its real (unredacted) text on the server although the form only ever saw the redacted view
        assert SECRET in j.db.get_action(new_id)["payload"]["body"]
        edited.locator('[data-act="approve"]').click()
        for _ in range(50):
            if j.actions.mail.sent:
                break
            page.wait_for_timeout(100)
        assert len(j.actions.mail.sent) == 1 and j.actions.mail.sent[0][1] == "Revised quote Q-1042"
    finally:
        ctx.close()


def test_a_card_being_edited_keeps_its_text_when_a_live_update_arrives(serve, browser):
    srv, j, _ = serve()
    ids = _seed(j)
    ctx, page = _page(browser, srv.url, 1280, 800)
    try:
        _open_pop(page, "approvals")
        _wait_cards(page, "#approvals", 2)
        card = page.locator(f'#approvals .appr-card[data-id="{ids["email"]}"]')
        card.locator('[data-act="edit"]').click()
        card.locator('[name="subject"]').fill("Half-typed subj")
        _jarvis_wants_to_email(j)                                           # a live "approvals" event: Jarvis queues another email
        page.fill("#input", "Email a@b.example.com a hello")
        page.keyboard.press("Enter")
        _wait_cards(page, "#approvals", 3)
        assert page.locator(f'#approvals .appr-card[data-id="{ids["email"]}"] [name="subject"]').input_value() == "Half-typed subj"
    finally:
        ctx.close()


def test_retry_queues_a_new_pending_action_and_nothing_runs_until_approve(serve, browser):
    srv, j, _ = serve()
    ids = _seed(j)
    fsm = j.actions.fsm
    ctx, page = _page(browser, srv.url, 1280, 800)
    try:
        _open_pop(page, "approvals")
        _wait_cards(page, "#failed-actions", 2)
        old = ids["failed"][0]
        page.locator(f'#failed-actions .appr-card[data-id="{old}"] [data-act="retry"]').click()
        page.wait_for_function(f"!document.querySelector('#failed-actions .appr-card[data-id=\"{old}\"]')", timeout=10000)
        new_id = j.db.get_action(old)["superseded_by"]
        assert new_id and fsm.writes == []                              # Retry ran NOTHING
        card = page.locator(f'#approvals .appr-card[data-id="{new_id}"]')
        card.wait_for(timeout=10000)
        assert "Retry" in card.inner_text() and f"Retry of #{old}" in card.inner_text()
        assert j.db.get_action(new_id)["status"] == "pending" and j.db.get_action(new_id)["approved_by"] == ""
        assert page.locator("#failed-actions .appr-card").count() == 1  # the other failure is still there to retry
        # a person approves the retry: only now does it run (and, because the fake FSM still refuses, it fails again - with its error)
        card.locator('[data-act="approve"]').click()
        page.wait_for_selector(f'#failed-actions .appr-card[data-id="{new_id}"]', timeout=10000)
        assert len(fsm.writes) == 1 and "site name already exists" in page.inner_text(f'#failed-actions .appr-card[data-id="{new_id}"]')
        # ...and when the cause is fixed a second retry goes through, again only after a click
        fsm.fail = False
        page.locator(f'#failed-actions .appr-card[data-id="{new_id}"] [data-act="retry"]').click()
        third = None
        for _ in range(50):
            third = j.db.get_action(new_id)["superseded_by"]
            if third:
                break
            page.wait_for_timeout(100)
        assert third and len(fsm.writes) == 1
        page.locator(f'#approvals .appr-card[data-id="{third}"] [data-act="approve"]').click()
        for _ in range(50):
            if j.db.get_action(third)["status"] == "done":
                break
            page.wait_for_timeout(100)
        assert j.db.get_action(third)["status"] == "done" and len(fsm.writes) == 2
    finally:
        ctx.close()


# ----------------------------------------------------------------------------------------------- phone layout
def _phone(browser, srv, scheme="dark", height=820, width=400):
    return _page(browser, srv.url, width, height, scheme, has_touch=True, is_mobile=True)


@pytest.mark.parametrize("scheme", THEMES)
def test_phone_the_core_is_large_and_approvals_are_one_tap_away(serve, browser, scheme):
    srv, j, _ = serve()
    _seed(j)
    ctx, page = _phone(browser, srv, scheme)
    try:
        core = page.eval_on_selector("#core-btn", "e => { const r = e.getBoundingClientRect(); return [r.width, r.height, r.left, r.right]; }")
        assert 150 <= core[0] <= 220 and core[0] == core[1], core          # large (the old phone core was 110px), and still square
        assert core[2] >= 0 and core[3] <= 400
        _shot(page, f"p4a-phone-home-{scheme}")
        # Approvals is visible without scrolling anything, and is the first chip
        chip = page.eval_on_selector('.rail-item[data-pop="approvals"]', "e => { const r = e.getBoundingClientRect(); return [r.left, r.right, r.height]; }")
        assert chip[0] >= 0 and chip[1] <= 400 and chip[2] >= 44
        assert page.eval_on_selector("#rail .rail-item", "e => e.dataset.pop") == "approvals"
        # still reachable when the strip has been scrolled to its far end (it is pinned to the left edge)
        page.eval_on_selector("#rail", "e => { e.scrollLeft = e.scrollWidth; }")
        page.wait_for_timeout(100)
        assert page.eval_on_selector('.rail-item[data-pop="approvals"]', "e => e.getBoundingClientRect().left") >= 0
        # ONE tap opens it
        page.tap('.rail-item[data-pop="approvals"]')
        page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
        assert page.inner_text("#drawer-title") == "Approvals"
        page.wait_for_timeout(350)
        _wait_cards(page, "#approvals", 2)
        assert page.locator('#approvals [data-act="approve"]').first.is_visible()
        page.tap("#drawer-close-bottom")
        page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
        # the badge on the core is the other one-tap route
        page.tap("#orb-badge")
        page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
        assert page.inner_text("#drawer-title") == "Approvals"
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
def test_phone_popups_are_full_screen_with_a_close_at_the_thumb(serve, browser, scheme):
    srv, j, _ = serve()
    _seed(j)
    _seed_memory(j)
    ctx, page = _phone(browser, srv, scheme)
    try:
        for pop in ("approvals", "comms", "issues", "health", "ops", "fleet", "finance", "presence", "upcoming"):
            page.tap(f'.rail-item[data-pop="{pop}"]')
            page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
            page.wait_for_timeout(350)
            box = page.eval_on_selector("#drawer", "e => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.width, r.height, e.scrollWidth, innerWidth, innerHeight]; }")
            assert box[0] == 0 and box[1] == 0 and abs(box[2] - box[5]) <= 1 and abs(box[3] - box[6]) <= 1, (pop, box)   # edge to edge, top to bottom
            assert box[4] <= box[2] + 1, (pop, box)
            foot = page.eval_on_selector("#drawer-close-bottom", "e => { const r = e.getBoundingClientRect(); return [r.bottom, r.height, r.width]; }")
            assert foot[0] <= box[6] and foot[1] >= 44 and foot[2] > 200, (pop, foot)
            assert _no_hscroll(page)[0] <= 400
            page.tap("#drawer-close-bottom")                                 # Close at the bottom edge works
            page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
        # Settings and Memory (reached from Settings) are full-screen too, and Escape / the top Close still work
        page.tap("#btn-settings")
        page.wait_for_timeout(350)
        page.tap("#btn-open-memory")
        page.wait_for_function("document.getElementById('pop-memory') && !document.getElementById('pop-memory').hidden")
        page.wait_for_timeout(350)
        assert page.inner_text("#drawer-title") == "Memory"
        _shot(page, f"p4a-phone-memory-{scheme}")
        assert page.eval_on_selector("#drawer", "e => e.getBoundingClientRect().width") == 400
        page.keyboard.press("Escape")
        page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
        page.tap('.tb-btn[data-pop="connections"]')
        page.wait_for_timeout(350)
        page.tap("#drawer-close")
        page.wait_for_function("!document.getElementById('drawer').classList.contains('open')")
    finally:
        ctx.close()


_TOUCH_JS = """(root) => {
  const sel = 'button, a[href], summary, select, textarea, input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio])';
  const out = [];
  for (const e of document.querySelectorAll(sel)) {
    if (root && !e.closest(root)) continue;
    if (e.closest('[hidden], .display:not(.open), .scrim') || e.closest('.md') || e.classList.contains('orb-badge')) continue;   // 28px but padded: see the hit test
    const cs = getComputedStyle(e), r = e.getBoundingClientRect();
    if (cs.visibility === 'hidden' || cs.display === 'none' || r.width === 0 || r.height === 0) continue;
    if (e.closest('#drawer') && !document.getElementById('drawer').classList.contains('open')) continue;
    if (r.height < 43.5 || r.width < 43.5) out.push([e.tagName, e.id || e.className || e.textContent.trim().slice(0, 20), Math.round(r.width), Math.round(r.height)]);
  }
  return out;
}"""


@pytest.mark.parametrize("scheme", THEMES)
def test_phone_every_touch_target_is_at_least_44px(serve, browser, scheme):
    srv, j, _ = serve(configure=lambda s: setattr(s, "jarvis_notes", NOTES))
    _seed(j, history=True)
    _seed_memory(j)
    ctx, page = _phone(browser, srv, scheme)
    try:
        page.wait_for_selector("#conversation .appr-card", timeout=10000)
        assert page.evaluate(_TOUCH_JS, None) == []                          # home: top bar, rail, needs strip, chat card, composer
        page.wait_for_selector("#reply-hint:not([hidden])", timeout=10000)      # the learned-reply hint and its cross are checked below too
        assert page.evaluate(_TOUCH_JS, None) == []
        # the badge is drawn at 28px but its hit area is padded to 44px+
        for sel in ("#orb-badge",):
            hits = page.evaluate("""(sel) => { const b = document.querySelector(sel), r = b.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
              return [[-21, 0], [21, 0], [0, -21], [0, 21]].map(([dx, dy]) => { const el = document.elementFromPoint(cx + dx, cy + dy); return !!el && (el === b || b.contains(el)); }); }""", sel)
            assert hits == [True] * 4, (sel, hits)
        _open_pop(page, "approvals")
        _wait_cards(page, "#approvals", 2)
        page.locator('#approvals .appr-card [data-act="edit"]').first.tap()   # the edit form's inputs count too
        assert page.evaluate(_TOUCH_JS, "#drawer") == []
        page.keyboard.press("Escape")
        page.tap("#btn-settings")
        page.wait_for_timeout(350)
        page.tap("#btn-open-memory")
        page.wait_for_selector("#memory-notes .mem-item")
        page.locator('#pop-memory [data-mem="edit"]').first.tap()
        assert page.evaluate(_TOUCH_JS, "#drawer") == []
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
def test_phone_message_box_controls_are_all_on_screen_and_the_keyboard_keeps_it_visible(serve, browser, scheme):
    srv, j, _ = serve()
    ctx, page = _phone(browser, srv, scheme)
    try:
        rects = page.evaluate("""() => Object.fromEntries(['input', 'btn-shortcuts', 'btn-attach', 'btn-mic', 'btn-send'].map(id => {
            const r = document.getElementById(id).getBoundingClientRect(); return [id, [r.left, r.right, r.top, r.bottom]]; }))""")
        for name, (left, right, top, bottom) in rects.items():
            assert left >= 0 and right <= 400 and bottom <= 820, (name, rects[name])
        assert rects["btn-send"][1] - rects["btn-send"][0] >= 96                        # SEND fills the rest of the row, no longer a stub
        assert rects["input"][1] - rects["input"][0] >= 330                             # the field has the full width
        assert rects["btn-shortcuts"][2] == rects["btn-attach"][2] == rects["btn-mic"][2] == rects["btn-send"][2]   # one tidy row under it
        # opening the keyboard shrinks the visual viewport (here: a short window): the core gives way, the box stays reachable
        page.set_viewport_size({"width": 400, "height": 430})
        page.tap("#input")
        page.wait_for_timeout(300)
        assert page.evaluate("document.body.classList.contains('composing')")
        assert page.evaluate("getComputedStyle(document.querySelector('.core-block')).display") == "none"
        box = page.eval_on_selector("#composer", "e => { const r = e.getBoundingClientRect(); return [r.top, r.bottom]; }")
        assert box[0] >= 0 and box[1] <= 430, box
        assert page.evaluate("document.documentElement.scrollHeight <= innerHeight + 1")
        _shot(page, f"p4a-phone-keyboard-{scheme}")
        # typing and sending still works, and the conversation view then keeps the core small and the box in view
        page.fill("#input", "Hello Jarvis")
        page.keyboard.press("Enter")
        page.wait_for_selector("#conversation .msg.user", timeout=10000)
        page.set_viewport_size({"width": 400, "height": 820})
        page.wait_for_timeout(300)
        page.evaluate("document.getElementById('input').blur()")
        page.wait_for_timeout(200)
        page.wait_for_selector("#conversation .msg.assistant .fb", timeout=10000)
        assert page.evaluate(_TOUCH_JS, None) == []                                      # Good / Wrong under a reply are touch-sized too
        core = page.eval_on_selector("#core-btn", "e => e.getBoundingClientRect().width")
        assert 72 <= core <= 96                                                         # in conversation the core makes room...
        assert page.eval_on_selector("#composer", "e => e.getBoundingClientRect().bottom") <= 820
        conv = page.eval_on_selector("#conversation", "e => e.getBoundingClientRect().height")
        assert conv >= 250, conv                                                       # ...and the conversation gets real height
        _shot(page, f"p4a-phone-chat-{scheme}")
    finally:
        ctx.close()


@pytest.mark.parametrize("width,height", [(400, 820), (400, 667), (360, 740)])
def test_phone_sizes_never_scroll_sideways_and_keep_the_composer_on_screen(serve, browser, width, height):
    srv, j, _ = serve()
    _seed(j, history=True)
    ctx, page = _phone(browser, srv, "dark", height, width)
    try:
        page.wait_for_selector("#conversation .appr-card", timeout=10000)
        assert _no_hscroll(page)[0] <= width and _no_hscroll(page)[1] <= width
        assert page.eval_on_selector("#composer", "e => e.getBoundingClientRect().bottom") <= height
        assert page.eval_on_selector("#btn-send", "e => e.getBoundingClientRect().right") <= width
        assert page.eval_on_selector(".topbar", "e => e.getBoundingClientRect().height") <= 130      # the top bar is two compact rows
        assert page.evaluate("[...document.querySelectorAll('.tb-btn')].every(b => b.getBoundingClientRect().right <= innerWidth)")
    finally:
        ctx.close()


# ----------------------------------------------------------------------------------------------- Memory pop-up
def _memory_server(serve):
    srv, j, s = serve(configure=lambda s: setattr(s, "jarvis_notes", NOTES))
    _seed_memory(j)
    return srv, j, s


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_memory_pop_up_lists_what_jarvis_has_learned_reachable_from_settings(serve, browser, width, height, scheme):
    srv, j, _ = _memory_server(serve)
    touch = {"has_touch": True, "is_mobile": True} if width < 760 else {}
    ctx, page = _page(browser, srv.url, width, height, scheme, **touch)
    try:
        page.click("#btn-settings") if width > 760 else page.tap("#btn-settings")
        page.wait_for_timeout(350)
        assert page.is_visible("#btn-open-memory")
        page.click("#btn-open-memory") if width > 760 else page.tap("#btn-open-memory")
        page.wait_for_selector("#memory-notes .mem-item", timeout=10000)
        page.wait_for_timeout(350)
        assert page.inner_text("#drawer-title") == "Memory"
        assert [e.inner_text() for e in page.locator("#memory-notes .mem-text").all()] == [
            "First County Monitoring handle our out-of-hours.", "Alex prefers quotes sent before 10am"]
        assert [e.inner_text() for e in page.locator("#memory-learned .mem-text").all()] == ["The Ilkley site key safe is held by reception on Mondays."]
        assert page.inner_text("#memory-replies .mem-text") == "yes do that"
        assert "Used 3 times" in page.inner_text("#memory-replies")
        assert "Things Jarvis should know" in page.text_content("#pop-memory") and "Learned replies" in page.text_content("#pop-memory")
        for item in page.locator("#pop-memory .mem-item").all():
            assert item.locator('[data-mem="edit"]').is_visible() and item.locator('[data-mem="ask-delete"]').is_visible()
        assert _no_hscroll(page)[0] <= width
        fit = page.evaluate("() => { const d = document.getElementById('drawer'); return [d.scrollWidth <= d.offsetWidth + 1, d.offsetWidth <= innerWidth]; }")
        assert fit == [True, True]
        _shot(page, f"p4a-memory-{width}-{scheme}")
        assert page.errors == []
    finally:
        ctx.close()


def test_memory_entries_can_be_reworded_and_deleted_and_jarvis_reads_the_change(serve, browser):
    srv, j, s = _memory_server(serve)
    ctx, page = _page(browser, srv.url, 1280, 800)
    try:
        page.click("#btn-settings")
        page.click("#btn-open-memory")
        page.wait_for_selector("#memory-notes .mem-item")
        # edit a note: the setting and the memory both change, and Jarvis's next prompt has the new wording
        note = page.locator("#memory-notes .mem-item").nth(1)
        note.locator('[data-mem="edit"]').click()
        note.locator(".mem-input").fill("Alex prefers quotes sent before 9am.")
        note.locator('[data-mem="save"]').click()
        page.wait_for_function("document.getElementById('memory-notes').innerText.includes('before 9am')", timeout=10000)
        assert s.jarvis_notes == "First County Monitoring handle our out-of-hours. | Alex prefers quotes sent before 9am."
        j.brain.refresh_system()
        text = json.dumps(j.brain.system)
        assert "before 9am" in text and "before 10am" not in text
        # an unacceptable edit shows the reason and changes nothing
        learned = page.locator("#memory-learned .mem-item").first
        learned.locator('[data-mem="edit"]').click()
        learned.locator(".mem-input").fill("ok")
        learned.locator('[data-mem="save"]').click()
        page.wait_for_selector("#memory-learned .mem-edit .appr-error:not([hidden])")
        assert "too short" in page.inner_text("#memory-learned .mem-edit .appr-error")
        learned.locator('[data-mem="cancel"]').click()
        assert "held by reception on Mondays" in page.inner_text("#memory-learned")
        # delete asks first; "Keep it" keeps it
        learned.locator('[data-mem="ask-delete"]').click()
        assert "Delete this?" in learned.inner_text()
        learned.locator('[data-mem="keep"]').click()
        assert page.locator("#memory-learned .mem-item").count() == 1
        learned.locator('[data-mem="ask-delete"]').click()
        learned.locator('[data-mem="delete"]').click()
        page.wait_for_function("document.querySelectorAll('#memory-learned .mem-item').length === 0", timeout=10000)
        j.brain.refresh_system()
        assert "held by reception" not in json.dumps(j.brain.system) and not any("Ilkley" in m["fact"] for m in j.db.memories())
        # delete a note: it is gone from the setting too, so a restart can't bring it back
        page.locator("#memory-notes .mem-item").first.locator('[data-mem="ask-delete"]').click()
        page.locator("#memory-notes .mem-item").first.locator('[data-mem="delete"]').click()
        page.wait_for_function("document.querySelectorAll('#memory-notes .mem-item').length === 1", timeout=10000)
        assert "First County" not in s.jarvis_notes
        # learned replies: reword, then delete
        reply = page.locator("#memory-replies .mem-item").first
        reply.locator('[data-mem="edit"]').click()
        reply.locator(".mem-input").fill("yes please do")
        reply.locator('[data-mem="save"]').click()
        page.wait_for_function("document.getElementById('memory-replies').innerText.includes('yes please do')", timeout=10000)
        page.locator("#memory-replies .mem-item").first.locator('[data-mem="ask-delete"]').click()
        page.locator("#memory-replies .mem-item").first.locator('[data-mem="delete"]').click()
        page.wait_for_function("document.querySelectorAll('#memory-replies .mem-item').length === 0", timeout=10000)
        assert j.reply_suggestions.rows() == [] and "No learned replies yet" in page.inner_text("#memory-replies")
        assert page.errors == []
    finally:
        ctx.close()


def test_an_approved_action_that_fails_turns_its_chat_card_into_the_error_with_a_retry(serve, browser):
    srv, j, _ = serve()
    ids = _seed(j, history=True)
    fsm = j.actions.fsm                                                   # refuses (HTTP 422), like the failed actions above
    ctx, page = _page(browser, srv.url, 1280, 800)
    try:
        page.wait_for_selector("#conversation .appr-card", timeout=10000)
        card = f'#conversation .appr-card[data-id="{ids["fsm"]}"]'
        page.locator(f"{card} [data-act=approve]").click()
        page.wait_for_selector(f'{card}[data-status="failed"]', timeout=10000)
        text = page.inner_text(card)
        assert "didn't go through" in text and "site name already exists" in text and SECRET not in text
        assert page.locator(f"{card} [data-act=retry]").is_visible() and page.locator(f"{card} [data-act=approve]").count() == 0
        assert len(fsm.writes) == 1
        # Retry from the chat: the failed card says it was retried, and a NEW card waits for an Approve - nothing re-ran
        page.locator(f"{card} [data-act=retry]").click()
        page.wait_for_function(f"document.querySelector('{card}').innerText.includes('Retried as #')", timeout=10000)
        new_id = j.db.get_action(ids["fsm"])["superseded_by"]
        new_card = f'#conversation .appr-card[data-id="{new_id}"]'
        page.wait_for_selector(f'{new_card}[data-status="pending"]', timeout=10000)
        assert len(fsm.writes) == 1 and page.locator(f"{new_card} [data-act=approve]").is_visible()
        # the rail now counts it (one more waiting) and the old failure is no longer in the failed list
        assert page.locator(f"#failed-actions .appr-card[data-id='{ids['fsm']}']").count() == 0
    finally:
        ctx.close()
