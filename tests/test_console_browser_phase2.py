"""Real-browser checks of console redesign Phase 2: how Jarvis talks.

Headless Chrome through Playwright (skipped when it is not installed, like test_console_browser.py, whose fixtures it
shares): the working line, the source-and-time line, the pop-up button, follow-ups, the question pop-up (click an answer
-> sent; Escape -> closed, nothing sent), Send becoming Stop while a reply streams, and the "How Jarvis talks" setting.

Each test gets its own real server whose Claude stand-in writes a word at a time and can be held open
(tests/live_server.py), so the page can be looked at while a reply is still streaming and the server's reaction to Stop
can be observed. Set JARVIS_SHOTS=<folder> to also save screenshots of each surface there.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from jarvis.brain.tools import TOOLS_BY_NAME  # noqa: E402
from jarvis.core import Jarvis  # noqa: E402
from jarvis.main import create_app  # noqa: E402
from tests.fakes import message, text_block, tool_block  # noqa: E402
from tests.live_server import LiveServer, SlowClient  # noqa: E402
from tests.test_console_browser import SIZES, THEMES, _no_hscroll, _settings, browser  # noqa: E402,F401

SHOTS = os.environ.get("JARVIS_SHOTS")


def _shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


@pytest.fixture
def chat(tmp_path_factory, monkeypatch):
    """build(script, **slow_client_kwargs) -> (server, jarvis, client, tool_gate). tool_gate holds the fsm_jobs tool
    open until set, so the "checking..." line can be seen."""
    made = []
    tool_gate = threading.Event()

    async def slow_jobs(j, a):
        import asyncio

        while not tool_gate.is_set():
            await asyncio.sleep(0.02)
        return [{"id": 1, "status": "booked"}]

    def build(script=None, **kw):
        settings = _settings(tmp_path_factory)
        client = SlowClient(script, **kw)
        j = Jarvis(settings, client=client)
        j.db.create_action("note", "Send the Kestrel quote", {})
        srv = LiveServer(create_app(settings, j))
        made.append((srv, client))
        return srv, j, client, tool_gate

    monkeypatch.setattr(TOOLS_BY_NAME["fsm_jobs"], "handler", slow_jobs)
    yield build
    tool_gate.set()
    for srv, client in made:
        client.gate.set()
        srv.stop()


def _page(browser, url, width=1280, height=800, scheme="dark"):
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(url + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#needs-list .need", timeout=15000)
    return ctx, page


def _say(page, text):
    page.fill("#input", text)
    page.click("#btn-send")


def jobs_script():
    return [
        message([tool_block("fsm_jobs", {})], "tool_use"),
        message([text_block("Four jobs are on today and two engineers are out already."),
                 tool_block("offer_next_steps", {"follow_ups": ["Who is running late?", "Any overdue jobs?"]}, "toolu_2")],
                "tool_use"),
        message([text_block("")]),
    ]


def ask_script():
    return [
        message([text_block("The letter is ready."),
                 tool_block("ask_user", {"question": "Send it now or hold it?",
                                         "options": [{"label": "Send now", "description": "Goes out this morning", "recommended": True},
                                                     {"label": "Hold until Monday"}, {"label": "Let me read it first"}]})],
                "tool_use"),
        message([text_block("")]),
    ]


def test_working_line_then_source_time_pop_up_button_and_follow_ups(browser, chat):
    srv, j, client, tool_gate = chat(jobs_script(), delay=0.005)
    ctx, page = _page(browser, srv.url)
    try:
        _say(page, "What's on today?")
        # While the tool is running the line above the reply names what he is checking - from the real tool event.
        page.wait_for_selector(".msg.assistant .step:not([hidden])", timeout=10000)
        assert page.inner_text(".msg.assistant .step") == "Checking jobs in Salts FSM…"
        assert page.is_visible("#btn-stop") and page.text_content("#state") == "Working"
        _shot(page, "p2-working-line")
        tool_gate.set()
        page.wait_for_selector(".msg.assistant .src", timeout=15000)
        assert page.query_selector(".msg.assistant .step") is None  # the working line is gone once the reply is done
        assert page.inner_text(".msg.assistant .md").strip() == "Four jobs are on today and two engineers are out already."
        assert re.fullmatch(r"Source: Salts FSM \(demo data\) · \d+\.\ds", page.inner_text(".msg.assistant .src"))
        chips = page.eval_on_selector_all(".msg.assistant .extras .reply-chip", "els => els.map(e => [e.tagName, e.textContent.trim()])")
        assert chips == [["BUTTON", "Open Ops"], ["BUTTON", "Who is running late?"], ["BUTTON", "Any overdue jobs?"]]
        _shot(page, "p2-reply-extras")
        # The pop-up button opens the same drawer as the rail.
        page.click(".msg.assistant .reply-chip.panel")
        page.wait_for_function("document.getElementById('drawer').classList.contains('open')")
        assert page.inner_text("#drawer-title") == "Operations" and page.is_visible("#ops-kpis")
        page.click("#drawer-close")
        # A follow-up goes back as an ordinary chat message.
        client.script.append(message([text_block("Nobody is late.")]))
        page.click('.msg.assistant .reply-chip[data-follow="Who is running late?"]')
        page.wait_for_function("document.querySelectorAll('.msg.user').length === 2")
        assert page.inner_text(".msg.user >> nth=1").endswith("Who is running late?")
        page.wait_for_function("document.querySelectorAll('.msg.assistant .src').length === 2", timeout=15000)
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_stop_ends_the_streaming_reply_and_the_server_cancels_the_work(browser, chat):
    srv, j, client, _ = chat(delay=0.01, hold_after=3)
    ctx, page = _page(browser, srv.url)
    try:
        assert page.is_visible("#btn-send") and not page.is_visible("#btn-stop")
        _say(page, "Give me the long answer")
        # Words are arriving, the reply is still being written, and Send has become Stop.
        page.wait_for_function("(document.querySelector('.msg.assistant .md')?.textContent.trim().split(/\\s+/).length || 0) >= 3", timeout=10000)
        page.wait_for_selector("#btn-stop", state="visible")
        assert not page.is_visible("#btn-send") and page.query_selector(".msg.assistant .md.typing")
        partial = page.inner_text(".msg.assistant .md").strip()
        _shot(page, "p2-streaming")
        page.click("#btn-stop")
        page.wait_for_selector("#btn-send", state="visible")
        assert not page.is_visible("#btn-stop") and page.text_content("#state") == "Online"
        assert page.query_selector(".msg.assistant .md.typing") is None
        assert page.inner_text(".msg.assistant .src") == "Stopped."
        # The server really stopped: the stream was cancelled and the conversation lock is free ...
        assert client.cancelled.wait(8), "the server did not cancel the reply"
        client.gate.set()
        page.wait_for_timeout(600)
        # ... and nothing more arrives afterwards: no extra words, no duplicate reply, no error message.
        assert page.inner_text(".msg.assistant .md").strip() == partial
        assert page.locator(".msg.assistant").count() == 1 and page.locator(".msg.assistant.error").count() == 0
        assert not j.brain._lock.locked()
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_question_popup_options_are_real_buttons_that_can_be_clicked(browser, chat):
    srv, j, client, _ = chat(ask_script(), delay=0.005)
    ctx, page = _page(browser, srv.url)
    try:
        _say(page, "Should the Kestrel letter go out?")
        page.wait_for_selector("#ask:not([hidden])", timeout=15000)
        assert page.text_content("#ask .ask-title") == "Jarvis is asking"
        assert page.inner_text("#ask .ask-q") == "Send it now or hold it?"
        opts = page.eval_on_selector_all("#ask .ask-opt", "els => els.map(e => [e.tagName, e.type, e.textContent.replace(/\\s+/g,' ').trim()])")
        assert [o[0] for o in opts] == ["BUTTON"] * 4 and [o[1] for o in opts] == ["button"] * 4
        assert opts[3][2].endswith("Type my own answer") and "Hold until Monday" in opts[1][2]
        # Centred, over a dimmed backdrop, and the options show a hand - never the text I-beam.
        box = page.eval_on_selector("#ask", "e => { const r = e.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2, innerWidth, innerHeight]; }")
        assert abs(box[0] - box[2] / 2) < 2 and abs(box[1] - box[3] / 2) < 2, box
        assert page.is_visible("#ask-scrim")
        style = page.eval_on_selector("#ask .ask-opt", "e => { const s = getComputedStyle(e); return [s.cursor, s.userSelect, s.pointerEvents]; }")
        assert style == ["pointer", "none", "auto"], style
        # Nothing sits on top of an option: the element under its centre is that very button (what a real click hits).
        hit = page.evaluate("""() => [...document.querySelectorAll('#ask .ask-opt')].map(b => {
            const r = b.getBoundingClientRect(); return document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)?.closest('.ask-opt') === b; })""")
        assert hit == [True] * 4
        _shot(page, "p2-question-popup")
        # A real mouse click on the second answer sends exactly that answer, and the pop-up goes.
        client.script.append(message([text_block("Right, I'll hold it until Monday.")]))
        page.click("#ask .ask-opt >> nth=1")
        page.wait_for_selector("#ask", state="hidden")
        page.wait_for_function("document.querySelectorAll('.msg.user').length === 2")
        assert page.inner_text(".msg.user >> nth=1").endswith("Hold until Monday")
        page.wait_for_function("document.querySelectorAll('.msg.assistant .src').length === 2", timeout=15000)
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_question_popup_escape_closes_without_sending_and_the_answer_button_brings_it_back(browser, chat):
    srv, j, client, _ = chat(ask_script(), delay=0.005)
    ctx, page = _page(browser, srv.url)
    try:
        _say(page, "Should the Kestrel letter go out?")
        page.wait_for_selector("#ask:not([hidden])", timeout=15000)
        page.wait_for_selector(".msg.assistant .src", timeout=15000)
        calls_before = len(client.calls)
        page.keyboard.press("Escape")
        page.wait_for_selector("#ask", state="hidden")
        assert not page.is_visible("#ask-scrim")
        page.wait_for_timeout(500)
        assert page.locator(".msg.user").count() == 1          # nothing was sent
        assert len(client.calls) == calls_before                # and nothing new reached Claude
        assert page.is_visible("#btn-send")
        # The reply keeps an "Answer" button for the question that was put away.
        page.click(".msg.assistant [data-reask]")
        page.wait_for_selector("#ask:not([hidden])")
        # "Type my own answer" opens a box; Enter sends what was typed.
        client.script.append(message([text_block("Understood.")]))
        page.click("#ask .ask-other")
        page.fill("#ask .ask-otherbox textarea", "Send it Thursday at nine")
        page.keyboard.press("Enter")
        page.wait_for_selector("#ask", state="hidden")
        page.wait_for_function("document.querySelectorAll('.msg.user').length === 2")
        assert page.inner_text(".msg.user >> nth=1").endswith("Send it Thursday at nine")
        assert page.query_selector(".msg.assistant [data-reask]") is None  # answered: the button goes
        assert not page.errors, page.errors
    finally:
        ctx.close()


def test_settings_drawer_how_jarvis_talks_saves_and_changes_the_prompt(browser, chat):
    srv, j, client, _ = chat()
    ctx, page = _page(browser, srv.url)
    try:
        page.click('.tb-btn[data-pop="settings"]')
        page.wait_for_function("!document.getElementById('set-talk').disabled", timeout=10000)
        assert page.input_value("#set-talk") == "natural"                     # the default
        assert "Alex" in page.inner_text("#set-talk-note")                     # Natural uses the first name
        assert page.eval_on_selector_all("#set-talk option", "els => els.map(e => e.value)") == ["natural", "formal"]
        page.select_option("#set-talk", "formal")
        assert page.is_visible("#settings-savebar") and "sir" in page.inner_text("#set-talk-note")
        _shot(page, "p2-settings-talk")
        page.click("#btn-settings-save")
        page.wait_for_function("document.getElementById('settings-savebar').hidden", timeout=15000)
        assert page.input_value("#set-talk") == "formal"
        saved = page.evaluate("fetch('/api/settings').then(r => r.json()).then(d => d.sections.find(s => s.id === 'profile').fields.find(f => f.key === 'talk_style').value)")
        assert saved == "formal"
        # Jarvis was rebuilt with it: his live system prompt now follows the Formal setting.
        assert "(their setting: Formal)" in "\n".join(b["text"] for b in srv.server.config.app.state.j.brain.system)
        # It also shows (and edits the same value) under Connections > You and the business.
        page.click("#drawer-close")
        page.click('.tb-btn[data-pop="connections"]')
        page.click('[data-open-section="profile"]')
        assert page.input_value("#f-talk_style") == "formal"
        assert not page.errors, page.errors
    finally:
        ctx.close()


@pytest.mark.parametrize("scheme", THEMES)
@pytest.mark.parametrize("width,height", SIZES)
def test_phase2_surfaces_fit_every_screen(browser, chat, width, height, scheme):
    srv, j, client, tool_gate = chat(jobs_script(), delay=0.002)
    tool_gate.set()
    ctx, page = _page(browser, srv.url, width, height, scheme)
    try:
        _say(page, "What's on today?")
        page.wait_for_selector(".msg.assistant .extras", timeout=15000)
        doc, body, vw = _no_hscroll(page)
        assert doc <= vw and body <= vw, (doc, body, vw)
        # the reply, its source line and every chip stay inside the chat column
        inside = page.evaluate("""() => { const c = document.getElementById('conversation').getBoundingClientRect();
            return [...document.querySelectorAll('.msg.assistant .src, .msg.assistant .reply-chip')].every(e => { const r = e.getBoundingClientRect();
              return r.left >= c.left - 1 && r.right <= c.right + 1; }); }""")
        assert inside
        if width < 500:
            heights = page.eval_on_selector_all(".msg.assistant .reply-chip", "els => els.map(e => e.getBoundingClientRect().height)")
            assert min(heights) >= 44, heights  # comfortable to tap
        _shot(page, f"p2-chat-{width}-{scheme}")
        # now a question pop-up: wholly on screen, nothing wider than the window
        client.script.append(message([text_block("One thing."), tool_block("ask_user", {
            "question": "Which day suits the Kestrel visit best?",
            "options": [{"label": "Tuesday", "description": "Dan is free all day", "recommended": True},
                        {"label": "Thursday"}, {"label": "Next week"}]})], "tool_use"))
        client.script.append(message([text_block("")]))
        _say(page, "Book Kestrel in")
        page.wait_for_selector("#ask:not([hidden])", timeout=15000)
        r = page.eval_on_selector("#ask", "e => { const b = e.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom, innerWidth, innerHeight]; }")
        assert r[0] >= 0 and r[1] >= 0 and r[2] <= r[4] and r[3] <= r[5], r
        assert _no_hscroll(page)[0] <= vw
        _shot(page, f"p2-ask-{width}-{scheme}")
        page.keyboard.press("Escape")
        assert not page.errors, page.errors
    finally:
        ctx.close()
