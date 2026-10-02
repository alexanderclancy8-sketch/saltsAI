"""Spoken-conversation regression suite - run after every change (`python -m pytest -q tests/test_conversation_regression.py`).

~40 realistic phrases, each with the behaviour it must produce:

  * SPOKEN_CASES   - what the owner says (fire & security jargon, spoken numbers, "sir", short commands) paired with
                     a reply that has the right *shape*. Each goes through the real conversation loop with the scripted
                     fake client, and the turn must be logged and pass the spoken-format check: concise, no markdown,
                     no URL, no wake phrase, no chatbot filler, figures phrased for speech, at most one question.
  * BAD_REPLIES    - replies that break the format, to prove the checker (`lint_spoken_reply`) actually catches each kind.
  * ECHO_CASES     - Jarvis's own voice coming back through the microphone must be treated as echo - and the owner's
                     real interruptions ("stop") and new requests must not be.
  * FEEDBACK_CASES - "that was wrong" / "that was spot on" are verdicts on the last reply; ordinary sentences that
                     merely contain the words are not.
  * repeated questions and interruptions have their own tests below.

What this can and can't prove: with a scripted client it pins the *plumbing and the rules* (what is logged, flagged,
treated as echo, how the prompt is framed), not the model's own wording. To check the real model against the same phrases
run with a live key, deliberately and sparingly (it spends API credit; write tools only ever queue for approval):

    JARVIS_LIVE_REGRESSION=1 ANTHROPIC_API_KEY=... python -m pytest -q tests/test_conversation_regression.py -k live

The browser's own echo guard (hud.js `looksLikeSelfEcho`) has no JS test runner here, so - like test_voice_fillers.py - it is
pinned structurally below; `conversation_quality.looks_like_self_echo` is its Python mirror and the source of truth for
the expected behaviour in ECHO_CASES.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jarvis.brain.prompts import PERSONA
from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.services.conversation_quality import detect_feedback_phrase, lint_spoken_reply, looks_like_self_echo
from tests.fakes import FakeClient

HUD = (Path(__file__).resolve().parent.parent / "jarvis" / "web" / "hud.js").read_text(encoding="utf-8")

# (id, what is said, a correctly shaped spoken reply)
SPOKEN_CASES = [
    # --- fire and security jargon
    ("bs5839_service", "Jarvis, when is the BS 5839-1 service due at Kestrel Retail?",
     "The Kestrel Retail service is due on the fourteenth, sir - detectors and call points this time round."),
    ("panel_faults", "Any zone faults on the fire panel at Baildon Mills?",
     "Two, sir - zone three and zone seven. I'd look at zone seven first, it's the stairwell."),
    ("bs5266_emergency_lighting", "Is the BS 5266 emergency lighting test at Shipley done?",
     "Done last Tuesday, sir. Three luminaires failed the duration test, so there's a quote to raise."),
    ("pd6662_grade", "What's the PD 6662 grade for the Halifax intruder alarm?",
     "Grade two, sir, which is what their insurer asked for."),
    ("bafe_renewal", "Has the BAFE SP203-1 renewal been booked in?",
     "Not yet, sir. The audit window opens next month, so I'd book it this week."),
    ("apollo_stock", "Do we have any Apollo XP95 heat detectors in the van stock?",
     "Six on Dave's van and a dozen in stores, sir."),
    ("loop_address", "Is the EN 54 sounder at unit fourteen on loop two?",
     "Loop two, address forty-one, sir."),
    ("bs8243_confirmation", "What's the BS 8243 confirmation set-up at the jeweller's?",
     "Sequential confirmation, sir - two separate triggers before the police are called."),
    # --- spoken numbers
    ("invoice_number", "What does invoice four seven two one come to?",
     "Just over three grand, sir."),
    ("chase_invoice", "Chase the two thousand five hundred pound invoice from Kestrel",
     "I've drafted the chaser for your approval, sir - it's forty days overdue."),
    ("percentage", "What's twenty percent of eleven thousand nine hundred and forty seven?",
     "Just under two thousand four hundred, sir."),
    ("purchase_order", "Raise a purchase order for twenty four smoke detectors",
     "Purchase order drafted for your approval, sir - twenty four smoke detectors."),
    ("booking_time", "Book the Leeds PPM for ten thirty on Tuesday the sixth",
     "Queued for your approval, sir - Leeds PPM, Tuesday the sixth at half ten."),
    ("call_points", "How many call points are on the Halifax job, forty or fifty?",
     "Forty-two, sir."),
    # --- 'sir' and greetings
    ("whats_on_tomorrow", "What's on tomorrow, sir?",
     "Two service visits and a call-out in Wakefield, sir."),
    ("morning", "Morning Jarvis",
     "Morning, sir. Two call-outs and a service visit today, and one approval waiting."),
    ("evening", "Good evening, sir",
     "Evening, sir. All quiet on the panels."),
    ("wake_in_sentence", "Hey Jarvis, what's our cash position?",
     "Just under twelve grand in the bank, sir."),
    # --- short commands
    ("yes", "Yes", "Done - queued for your approval, sir."),
    ("approve", "Approve", "Approved and sent, sir."),
    ("next", "Next", "That's the last of them, sir."),
    ("read_email", "Read my emails", "Four unread, sir. The Kestrel one wants a reply today."),
    ("nearest_engineer", "Who's nearest to the Wakefield call-out?", "Sam, about ten minutes away, sir."),
    ("show_rams", "Put the RAMS for Keighley Road on the display", "They're on the display, sir."),
    ("on_call", "Who's on call tonight?", "Priya's on call tonight, sir."),
]

BAD_REPLIES = [
    ("Certainly! Cash is fine, sir.", "chatbot_filler"),
    ("Great question, sir. Cash is fine.", "chatbot_filler"),
    ("I'd be happy to help - cash is fine, sir.", "chatbot_filler"),
    ("I hope this helps. Let me know if you need anything else.", "chatbot_filler"),
    ("Hey Jarvis, cash is fine.", "wake_phrase"),
    ("Jarvis here. Cash is fine.", "wake_phrase"),
    ("Here are the faults:\n- Zone three\n- Zone seven", "markdown"),
    ("**Zone three** is in fault, sir.", "markdown"),
    ("| Zone | State |\n|---|---|\n| 3 | Fault |", "markdown"),
    ("See https://salts.example/report for the detail.", "url"),
    ("Cash is £11,947.32, sir.", "raw_figure"),
    ("Do you want the faults? Or the jobs? Or the invoices?", "multiple_questions"),
    ("word " * 90, "too_long"),
    ("", "empty"),
]

# (what the microphone heard, what Jarvis recently said, was he still speaking / just stopped, is it echo?)
OWN_REPLY = "Two zone faults on the panel, sir - zone three and zone seven."
ECHO_CASES = [
    ("Hey Jarvis hey Jarvis hey Jarvis", [], False, True),                                    # overlapping playback
    ("two zone faults on the panel sir zone three and zone seven", [OWN_REPLY], False, True),  # his exact words back
    ("Two zone faults on the panel, sir, zone three and zone seven.", [OWN_REPLY], True, True),
    ("two faults panel zone three zone seven sir", [OWN_REPLY], True, True),                  # garbled while speaking
    ("zone three and", [OWN_REPLY], True, True),                                              # a short garbled fragment
    ("two faults panel zone three zone seven sir", [OWN_REPLY], False, False),                # same words, long after
    ("What about the inbox?", [OWN_REPLY], True, False),                                      # a real follow-up, mid-reply
    ("How's cash?", [OWN_REPLY], False, False),                                               # an ordinary question
]

INTERRUPTIONS = ["Stop", "Jarvis, stop", "Quiet", "Jarvis stop talking", "Cancel that"]

FEEDBACK_CASES = [
    ("That was wrong", "wrong"),
    ("No, that's not right, sir", "wrong"),
    ("Jarvis, that was wrong", "wrong"),
    ("That's not what I asked", "wrong"),
    ("Wrong answer, it's the Halifax site", "wrong"),
    ("That was spot on", "good"),
    ("Good answer, thanks", "good"),
    ("That's right", "good"),
    ("That's good, now email them the quote", None),       # a request, not a verdict
    ("Why was that wrong?", None),                          # a question
    ("What's wrong with the Kestrel panel?", None),         # about the job, not about Jarvis's reply
    ("Is that right?", None),
]


async def _ask_spoken(settings, phrase: str, reply: str):
    j = Jarvis(settings, client=FakeClient(default_text=reply))
    try:
        got = await j.brain.ask(phrase, "voice")
        return j, got
    finally:
        await j.http.aclose()


@pytest.mark.parametrize("case_id,phrase,reply", SPOKEN_CASES, ids=[c[0] for c in SPOKEN_CASES])
async def test_spoken_phrase_is_framed_logged_and_answered_in_the_spoken_format(settings, case_id, phrase, reply):
    j, got = await _ask_spoken(settings, phrase, reply)
    assert got == reply
    # framed as a spoken turn, the words reaching the model untouched
    sent = j.brain.messages[0]["content"][-1]["text"]
    assert sent.startswith("[spoken") and sent.endswith(phrase)
    # logged once, cleanly: not failed, not a duplicate, not mistaken for echo, not treated as feedback
    [row] = j.db.query("SELECT * FROM turn_metrics")
    assert row["mode"] == "voice" and row["total_ms"] is not None and row["tool_calls"] == 0
    assert (row["failed"], row["interrupted"], row["duplicate"], row["echo_suspect"]) == (0, 0, 0, 0)
    assert j.db.query("SELECT * FROM turn_feedback") == []
    # concise spoken format: no markdown, URLs, wake phrase, chatbot filler, raw figures, stacked questions
    assert row["format_flags"] == "" and lint_spoken_reply(reply) == [], lint_spoken_reply(reply)
    assert len(reply.split()) <= 40  # the cases themselves stay "one to three short sentences"


@pytest.mark.parametrize("reply,flag", BAD_REPLIES, ids=[f"{f}-{i}" for i, (_, f) in enumerate(BAD_REPLIES)])
def test_the_format_checker_catches_each_kind_of_bad_spoken_reply(reply, flag):
    assert flag in lint_spoken_reply(reply)


@pytest.mark.parametrize("heard,recent,in_window,is_echo", ECHO_CASES, ids=[f"echo{i}" for i in range(len(ECHO_CASES))])
def test_echo_of_jarvis_own_voice_is_treated_as_echo_and_real_speech_is_not(heard, recent, in_window, is_echo):
    assert looks_like_self_echo(heard, recent, "jarvis", in_window) is is_echo


@pytest.mark.parametrize("phrase", INTERRUPTIONS)
def test_an_interruption_is_never_mistaken_for_echo_even_if_jarvis_just_said_those_words(phrase):
    said = ["Right, I'll stop there and stay quiet unless you want me to cancel that, sir."]
    assert looks_like_self_echo(phrase, said, "jarvis", True) is False


@pytest.mark.parametrize("phrase,expected", FEEDBACK_CASES, ids=[f"fb{i}" for i in range(len(FEEDBACK_CASES))])
def test_feedback_phrases_are_verdicts_and_ordinary_sentences_are_not(phrase, expected):
    verdict = detect_feedback_phrase(phrase, "jarvis")
    assert (verdict[0] if verdict else None) == expected
    if verdict:
        assert verdict[1] == phrase  # the note is what was said, for the nightly reflection to read


async def test_a_repeated_question_is_counted_and_both_answers_are_still_given(settings):
    j = Jarvis(settings, client=FakeClient(default_text="Just under twelve grand in the bank, sir."))
    a = await j.brain.ask("How's cash?", "voice")
    b = await j.brain.ask("How's cash?", "voice")
    assert a == b == "Just under twelve grand in the bank, sir."
    assert [r["duplicate"] for r in j.db.query("SELECT * FROM turn_metrics ORDER BY id")] == [0, 1]
    await j.http.aclose()


async def test_jarvis_own_reply_arriving_as_a_spoken_message_is_logged_as_echo(settings):
    j = Jarvis(settings, client=FakeClient(default_text=OWN_REPLY))
    await j.brain.ask("Any faults at Baildon?", "voice")
    await j.brain.ask("Two zone faults on the panel, sir, zone three and zone seven", "voice")
    assert [r["echo_suspect"] for r in j.db.query("SELECT * FROM turn_metrics ORDER BY id")] == [0, 1]
    await j.http.aclose()


async def test_an_interrupted_reply_is_logged_as_interrupted_not_failed(settings):
    import asyncio

    started = asyncio.Event()

    class NeverEnds:
        async def __aenter__(self):
            started.set()
            await asyncio.sleep(10)

        async def __aexit__(self, *exc):
            return False

    j = Jarvis(settings, client=FakeClient())
    j.client.beta.messages.stream = lambda *a, **k: NeverEnds()  # type: ignore[method-assign]
    task = asyncio.create_task(j.brain.ask("Read me the whole invoice list", "voice"))
    await asyncio.wait_for(started.wait(), timeout=2)
    await j.brain.interrupt()
    with pytest.raises(asyncio.CancelledError):
        await task
    [row] = j.db.query("SELECT * FROM turn_metrics")
    assert (row["interrupted"], row["failed"]) == (1, 0)
    await j.http.aclose()


# --------------------------------------------------------------------------- the rules the model is given
def test_the_system_prompt_still_states_the_spoken_rules_this_suite_checks():
    for rule in ("no markdown", "no lists", "no URLs", "free of wake phrases", "Certainly!", "Great question",
                 "I'd be happy to", "As an AI", "at most one question", "just under twelve grand", "sir"):
        assert rule in PERSONA, rule


def test_the_browser_still_has_its_echo_guard_and_reports_to_the_quality_log():
    assert "function looksLikeSelfEcho(text) {" in HUD
    assert "if (looksLikeSelfEcho(text)) { voiceEvent({ kind: \"echo_suppressed\"" in HUD
    assert "if (!item.filler) noteFirstAudio();" in HUD            # a filler is not "the first audio" of the answer
    assert "turnClock.sentAt = spoken && mode === \"voice\"" in HUD  # only spoken turns are timed
    assert "/api/feedback" in HUD and "feedbackHtml(d.turn_id)" in HUD and 'data-rating="wrong"' in HUD


# --------------------------------------------------------------------------- optional: the real model
@pytest.mark.skipif(not os.environ.get("JARVIS_LIVE_REGRESSION"),
                    reason="live model check - set JARVIS_LIVE_REGRESSION=1 (spends API credit)")
@pytest.mark.parametrize("case_id,phrase,_reply", SPOKEN_CASES, ids=[c[0] for c in SPOKEN_CASES])
async def test_live_model_keeps_to_the_spoken_format(tmp_path, case_id, phrase, _reply):
    live = Settings(data_dir=tmp_path / "data", scheduler_enabled=False, _env_file=None)
    j = Jarvis(live)
    try:
        reply = await j.brain.ask(phrase, "voice")
        assert lint_spoken_reply(reply, live.wake_word) == [], reply
    finally:
        await j.http.aclose()
