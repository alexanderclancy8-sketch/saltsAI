"""The ask_user tool (small question pop-up: 2-4 options + an always-present "Other") and its HUD component.

Like the barge-in tests there is no JS runner in this repo, so the front-end checks read ask.js / hud.js / index.html
and assert the wiring (and, above all, that the question prompt stays apart from the approval mechanism)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from jarvis.brain.prompts import PERSONA
from jarvis.brain.tools import AskUserIn, TOOLS_BY_NAME, ask_user, dispatch
from jarvis.core import Jarvis
from jarvis.services.recruiter import NO_RECURSE, Recruiter
from tests.fakes import FakeClient, message, text_block, tool_block

WEB = Path(__file__).resolve().parent.parent / "jarvis" / "web"
ASK_JS = (WEB / "ask.js").read_text(encoding="utf-8")
HUD = (WEB / "hud.js").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")


def args(**over):
    base = {"question": "Which day for the Kestrel visit?",
            "options": [{"label": "Tuesday", "description": "Dan is free all day", "recommended": True},
                        {"label": "Thursday"}]}
    return AskUserIn.model_validate({**base, **over})


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ------------------------------------------------------------------ the tool
def test_tool_is_registered_and_is_not_an_approval_tool():
    tool = TOOLS_BY_NAME["ask_user"]
    assert tool.approval is False  # it only asks; it never queues, approves or performs anything
    schema = tool.definition()["input_schema"]
    assert {"question", "options", "allow_multiple"} <= set(schema["properties"])
    assert schema["properties"]["options"]["minItems"] == 2 and schema["properties"]["options"]["maxItems"] == 4
    assert TOOLS_BY_NAME["email_send"].approval is True  # the approval gate is untouched


def test_validation_accepts_a_good_question_and_normalises_whitespace():
    a = args(question="  Which   day?  ", options=[{"label": " Tuesday  ", "description": " a  b "}, {"label": "Thursday"}],
             allow_multiple=True)
    assert a.question == "Which day?" and a.allow_multiple is True
    assert a.options[0].label == "Tuesday" and a.options[0].description == "a b"


@pytest.mark.parametrize("bad", [
    {"options": [{"label": "Only one"}]},
    {"options": [{"label": str(i)} for i in range(5)]},
    {"options": [{"label": "Yes"}, {"label": "yes"}]},  # duplicate, case-insensitive
    {"options": [{"label": "Yes"}, {"label": "Other"}]},  # "Other" is always added by the display
    {"options": [{"label": "Yes"}, {"label": "Other..."}]},
    {"options": [{"label": "Approve"}, {"label": "Not yet"}]},  # approvals are only the Approve button
    {"options": [{"label": "Yes"}, {"label": "Reject"}]},
    {"options": [{"label": "A", "recommended": True}, {"label": "B", "recommended": True}]},
    {"options": [{"label": "  "}, {"label": "B"}]},
    {"options": [{"label": "x" * 81}, {"label": "B"}]},
    {"question": "   "},
    {"question": "q" * 301},
])
def test_validation_rejects_bad_input(bad):
    with pytest.raises(ValidationError):
        args(**bad)


async def test_handler_publishes_one_small_ask_event_and_nothing_else(settings):
    j = Jarvis(settings, client=FakeClient())
    q = j.bus.subscribe()
    result = await dispatch(j, TOOLS_BY_NAME["ask_user"], args(allow_multiple=True))
    events = drain(q)
    assert [e["type"] for e in events] == ["ask"]  # no "display" pop-up, no "approvals" event
    d = events[0]["data"]
    assert d["question"] == "Which day for the Kestrel visit?" and d["allow_multiple"] is True and d["id"]
    assert d["options"] == [{"label": "Tuesday", "description": "Dan is free all day", "recommended": True},
                            {"label": "Thursday", "description": "", "recommended": False}]
    assert "Other" not in [o["label"] for o in d["options"]]
    # Nothing was queued for approval, and the result tells the model to stop and wait for the reply.
    assert j.db.pending_actions() == []
    assert "wait" in result and "not an approval" in result
    await j.http.aclose()


async def test_two_asks_get_distinct_ids(settings):
    j = Jarvis(settings, client=FakeClient())
    q = j.bus.subscribe()
    await ask_user(j, args())
    await ask_user(j, args())
    ids = [e["data"]["id"] for e in drain(q)]
    assert len(set(ids)) == 2
    await j.http.aclose()


async def test_ask_user_through_the_agent_loop_emits_the_event_before_the_reply(settings):
    j = Jarvis(settings, client=FakeClient([
        message([tool_block("ask_user", {"question": "Send it now or hold?",
                                         "options": [{"label": "Send now"}, {"label": "Hold until Monday"}]})],
                "tool_use"),
        message([text_block("The detail is above, sir - which would you prefer?")]),
    ]))
    q = j.bus.subscribe()
    reply = await j.brain.ask("Should the Kestrel letter go out?", "typed")
    types = [e["type"] for e in drain(q)]
    assert "ask" in types and types.index("ask") < types.index("reply")
    assert "display" not in types and "approvals" not in types
    assert reply.startswith("The detail is above")
    assert j.db.pending_actions() == []
    await j.http.aclose()


async def test_a_bad_ask_is_reported_back_to_the_model_not_shown(settings):
    j = Jarvis(settings, client=FakeClient([
        message([tool_block("ask_user", {"question": "Fine?", "options": [{"label": "Only"}]})], "tool_use"),
        message([text_block("Right.")]),
    ]))
    q = j.bus.subscribe()
    await j.brain.ask("hello", "typed")
    assert "ask" not in [e["type"] for e in drain(q)]
    await j.http.aclose()


def test_recruited_sub_agents_cannot_ask_the_owner(settings):
    assert "ask_user" in NO_RECURSE
    j = Jarvis(settings, client=FakeClient())
    assert all(t.name != "ask_user" for t in Recruiter(j)._tool_set(None))
    assert Recruiter(j)._tool_set(["ask_user", "remember"]) == [TOOLS_BY_NAME["remember"]]


def test_chat_stream_fallback_forwards_ask_events():
    main = (WEB.parent / "main.py").read_text(encoding="utf-8")
    assert '"stopped", "ask"}' in main
    assert '"ask"' not in main.split("CHAT_STREAM_TERMINAL")[1].split("\n")[0]  # not a terminal event


# ------------------------------------------------------------------ system prompt
def test_persona_steers_decisions_to_ask_user_and_keeps_detail_in_chat(settings):
    text = PERSONA.format(owner=settings.owner_name, company=settings.company_name,
                          salutation=settings.owner_salutation, issue_tag=settings.issue_email_tag, core_docs="(docs)")
    assert "`ask_user`" in text and "2-4 options" in text
    assert "Put the detail" in text and "in your chat reply" in text
    assert "`recommended`" in text and "`allow_multiple`" in text and '"Other"' in text
    assert "never an approval" in text
    assert "Golden rule: suggest, never act on your own" in text  # existing guidance untouched
    assert "at most one question" in text


# ------------------------------------------------------------------ front end
def test_component_is_loaded_before_hud_and_hooked_in_minimally():
    assert INDEX.index('/static/ask.js') < INDEX.index('/static/hud.js')
    assert '/static/ask.css' in INDEX
    assert (WEB / "ask.css").exists()
    assert 'case "ask": window.JarvisAsk?.show(d); break;' in HUD
    assert "window.JarvisAsk?.close()" in HUD  # a new user message / reset dismisses an open question
    assert "window.JarvisAsk?.init(" in HUD


def test_answers_go_back_as_ordinary_chat_messages_never_through_approvals():
    assert "host.send(text, mode, { ask: true })" in ASK_JS
    # The component has no route to the approval mechanism at all.
    for forbidden in ("decide(", "/api/approvals", "api(", "fetch(", "S.approvals", "data-act"):
        assert forbidden not in ASK_JS
    # hud.js's approval path is unchanged and is not handed to the component.
    assert 'data-act="approve"' in HUD and "async function decide(id, act)" in HUD
    init = HUD[HUD.index("window.JarvisAsk?.init("):].split("\n")[0]
    assert "decide" not in init and "approv" not in init.lower()


def test_send_maps_spoken_choices_but_not_the_prompts_own_answers():
    assert 'if (mode === "voice" && !opts.ask && window.JarvisAsk) text = window.JarvisAsk.spokenReply(text);' in HUD
    assert HUD.index("window.JarvisAsk.spokenReply(text)") > HUD.index("text = text.trim();")


def test_component_supports_keyboard_other_multiselect_recommended_and_descriptions():
    for needle in ("ArrowDown", "ArrowUp", 'e.key === "Escape"', "/^[1-9]$/", 'data-other="1"', "<textarea",
                   "Type your own answer", "aria-pressed", "allow_multiple", "ask-rec", "Recommended", "ask-desc",
                   "e.shiftKey"):
        assert needle in ASK_JS, needle
    # Hold-Space-to-talk must not swallow Space on a focused option.
    assert 'e.key === " "' in ASK_JS and "e.stopPropagation()" in ASK_JS
    # Single-select: clicking an option answers at once; multi-select waits for Send.
    assert "if (!multi) { submit(state.options[i].label); return; }" in ASK_JS


def test_voice_reads_the_question_aloud_and_takes_a_spoken_choice():
    assert "host.speakNow()" in ASK_JS and "host.say(speechFor(state))" in ASK_JS
    assert "function spokenReply(text)" in ASK_JS and "MAX_SPOKEN_CHOICE_WORDS" in ASK_JS
    assert "NEGATIONS" in ASK_JS  # "not Friday" is free speech, not a pick of Friday
    # Wired to the voice state in hud.js (speak only in a session that is spoken, or when "always" is set).
    assert "speakNow: () => shouldSpeak(S.lastMode)" in HUD


def test_hud_follow_up_window_still_has_a_single_writer():
    # Guard for the existing voice-flow invariant (tests/test_prompts.py): the hooks above must not add another.
    assert HUD.count("S.followUpUntil = ") == 1
    assert "S.followUpUntil" not in ASK_JS
