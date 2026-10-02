"""Console redesign, Phase 2: the question pop-up (a centred dialog with real buttons for each answer).

It replaces the earlier question pop-up and fixes the symptom behind the reported bug: the options showed a text
cursor and could not be clicked. The behaviour is checked three ways: the real ask.js against a fake DOM in node
(click an answer -> it is sent; Escape -> closed, nothing sent), static checks of the markup and CSS that make them
real clickable buttons, and (in test_console_browser.py) a real headless Chrome clicking them with a real mouse.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
WEB = TESTS.parent / "jarvis" / "web"
ASK_JS = (WEB / "ask.js").read_text(encoding="utf-8")
ASK_CSS = (WEB / "ask.css").read_text(encoding="utf-8")
HUD = (WEB / "hud.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ran():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    run = subprocess.run(["node", str(TESTS / "ask_dom_harness.js")], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def reply(text):
    return [{"text": text, "mode": "typed", "opts": {"ask": True}}]


def test_clicking_an_answer_sends_it_as_the_reply_and_closes(ran):
    assert ran["clickSent"] == reply("Thursday")  # exactly one message, the answer as written
    assert ran["closedAfterClick"] and ran["focusBackInChat"]
    assert ran["optionTags"] == ["button", "button", "button"]  # two answers + "Type my own answer": all <button>


def test_escape_closes_without_sending_wherever_focus_is(ran):
    assert ran["escapeClosed"] and ran["dismissClosed"]
    assert ran["pageEscapeClosed"]  # focus on the page itself, not inside the pop-up
    assert ran["escapeInOwnAnswerClosed"]  # even with a half-typed "own answer"
    assert ran["dismissSent"] == [] and ran["noneOfThatSent"] == []


def test_the_dimmed_backdrop_is_a_real_element_that_dismisses_without_sending(ran):
    assert ran["scrimShownWithPopup"] and ran["scrimClickClosed"]
    assert ".ask-scrim" in ASK_CSS and "z-index: 39" in ASK_CSS


def test_a_dismissed_question_can_be_brought_back_but_an_answered_one_cannot(ran):
    assert ran["canReopenAfterDismiss"] and ran["reopened"]
    assert ran["cannotReopenAfterAnswer"] and ran["forgotten"]
    # hud.js puts an "Answer" button under the reply, and takes it away once a message has been sent.
    assert 'data-reask>Answer</button>' in HUD and "window.JarvisAsk?.reopen?.()" in HUD
    assert '$$("#conversation [data-reask]").forEach((b) => b.remove())' in HUD


def test_the_pop_up_is_modal_so_tab_stays_inside(ran):
    assert ran["tabWraps"]
    assert 'el.setAttribute("role", "dialog")' in ASK_JS and 'el.setAttribute("aria-modal", "true")' in ASK_JS


def test_it_asks_in_jarvis_voice_and_offers_type_my_own_answer():
    assert "Jarvis is asking" in ASK_JS
    assert '<span class="ask-label">Type my own answer</span>' in ASK_JS
    assert "Other…" not in ASK_JS


def test_options_are_real_buttons_with_a_hand_cursor_and_no_text_cursor():
    # The reported bug: a text I-beam over the answers and nothing happening on click.
    assert '<button type="button" class="ask-opt"' in ASK_JS
    css = " ".join(ASK_CSS.split())
    assert ".ask-opt { display: flex;" in css and "cursor: pointer;" in css
    assert ".ask button, .ask button * { cursor: pointer; }" in css
    assert "user-select: none" in css
    assert ".ask-opt > *, .ask-opt > * > * { pointer-events: none; }" in css  # the click always lands on the button
    # Only the own-answer text box shows a text cursor.
    assert ".ask-otherbox textarea" in css and "cursor: text" in css


def test_the_single_choice_click_path_is_unchanged_and_never_touches_approvals():
    assert "if (!multi) { submit(state.options[i].label); return; }" in ASK_JS
    for forbidden in ("/api/approvals", "fetch(", "api(", "S.approvals", "decide("):
        assert forbidden not in ASK_JS
