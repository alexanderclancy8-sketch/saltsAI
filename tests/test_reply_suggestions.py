"""Learned reply suggestions: learning, context matching, recency, privacy, forgetting, the API, and the rule
that a suggestion is only ever text - it never sends a message or touches the approval queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from jarvis.core import Jarvis
from jarvis.main import create_app
from jarvis.services import reply_suggestions as rs
from jarvis.services.reply_suggestions import MIN_USES, classify_context, learnable, normalise
from tests.fakes import FakeClient

OFFER = "I've pulled the overdue invoices together. Shall I draft the chasers?"


def make(settings):
    return Jarvis(settings, client=FakeClient())


def say(j, text):
    j.db.add_transcript("assistant", text)


def learn(j, text, times=MIN_USES, mode="typed"):
    for _ in range(times):
        j.reply_suggestions.record(text, mode)


# -- pure helpers ---------------------------------------------------------------------------------------
def test_normalise_ignores_case_punctuation_and_whitespace():
    assert normalise("  Yes,   do THAT! ") == "yes do that"
    assert normalise("Don't") == normalise("dont") == "dont"
    assert normalise("!!!") == ""


def test_context_classification():
    assert classify_context(None) == "none"
    assert classify_context("") == "none"
    assert classify_context(OFFER) == "offer"
    assert classify_context("Would you like me to send it?") == "offer"
    assert classify_context("Which customer do you mean?") == "question"
    assert classify_context("I've queued that for your approval.") == "approval"
    assert classify_context("Cash is fine, sir.") == "statement"
    # only the last sentence decides whether a question is an offer
    assert classify_context("Shall I carry on? Actually, which site was it?") == "question"


def test_sensitive_or_long_text_is_not_learnable():
    assert learnable("yes do that")
    for bad in ("the alarm code is 4821", "my password is hunter2", "sk-ant-abc", "call 07700 900123",
                "email me at a@b.co", "see https://x.example", "", "   ", "line one\nline two",
                "a " * 30, "iban GB29 NWBK"):
        assert not learnable(bad), bad


# -- learning + suggesting --------------------------------------------------------------------------------
def test_nothing_is_suggested_until_used_enough_times(settings):
    j = make(settings)
    say(j, OFFER)
    learn(j, "Yes", MIN_USES - 1)
    assert j.reply_suggestions.suggest()["text"] is None
    learn(j, "yes.", 1)  # same reply once normalised
    got = j.reply_suggestions.suggest()
    assert got["text"] == "yes." and got["context"] == "offer"  # most recent wording is shown


def test_prefers_the_reply_for_the_same_context_then_falls_back_to_overall(settings):
    j = make(settings)
    say(j, OFFER)
    learn(j, "yes do that", 3)
    say(j, "Which site do you mean?")
    learn(j, "the main one", 4)
    learn(j, "ok", 5, )  # in the question context too, most common overall
    say(j, OFFER)
    assert j.reply_suggestions.suggest()["text"] == "yes do that"  # same situation beats the overall favourite
    say(j, "Cash is fine, sir.")  # a context nothing was learned in: fall back to the most common overall
    fallback = j.reply_suggestions.suggest()
    assert fallback["text"] == "ok" and "common" in fallback["why"]


def test_prefix_matching_as_you_type(settings):
    j = make(settings)
    say(j, OFFER)
    learn(j, "yes do that", 3)
    learn(j, "no thanks", 3)
    assert j.reply_suggestions.suggest("ye")["text"] == "yes do that"
    assert j.reply_suggestions.suggest("YES D")["text"] == "yes do that"
    assert j.reply_suggestions.suggest("n")["text"] == "no thanks"
    assert j.reply_suggestions.suggest("maybe")["text"] is None
    assert j.reply_suggestions.suggest("yes do that")["text"] is None  # already fully typed


def test_voice_sensitive_and_long_messages_are_never_stored(settings):
    j = make(settings)
    learn(j, "yes", 5, mode="voice")  # could be the mic hearing Jarvis itself
    learn(j, "the code is 123456", 5)
    learn(j, "please " * 20, 5)
    assert j.db.query("SELECT * FROM reply_habits") == []
    assert j.reply_suggestions.suggest()["text"] is None


def test_old_habits_fade(settings):
    j = make(settings)
    say(j, OFFER)
    learn(j, "yes", 3)
    assert j.reply_suggestions.suggest()["text"] == "yes"
    old = (datetime.now(timezone.utc) - timedelta(days=rs.HALF_LIFE_DAYS * 6)).isoformat(timespec="seconds")
    j.db.execute("UPDATE reply_habits SET last_used = ?", (old,))
    assert j.reply_suggestions.suggest()["text"] is None


def test_recent_use_outweighs_an_older_favourite(settings):
    j = make(settings)
    say(j, OFFER)
    learn(j, "yes", 5)
    old = (datetime.now(timezone.utc) - timedelta(days=rs.HALF_LIFE_DAYS * 2)).isoformat(timespec="seconds")
    j.db.execute("UPDATE reply_habits SET last_used = ?", (old,))  # 5 uses, now worth ~1.25
    learn(j, "go ahead", 3)  # 3 fresh uses, worth 3
    assert j.reply_suggestions.suggest()["text"] == "go ahead"


def test_store_is_capped_dropping_the_weakest(settings, monkeypatch):
    monkeypatch.setattr(rs, "MAX_ROWS", 3)
    j = make(settings)
    learn(j, "yes", 5)
    for word in ("alpha", "bravo", "charlie", "delta"):
        learn(j, word, 1)
    kept = {r["norm"] for r in j.db.query("SELECT norm FROM reply_habits")}
    assert len(kept) == 3 and "yes" in kept


def test_forget_and_clear(settings):
    j = make(settings)
    say(j, OFFER)
    learn(j, "yes", 3)
    learn(j, "go ahead", 3)
    assert j.reply_suggestions.forget("Yes!") == 1
    assert {r["norm"] for r in j.db.query("SELECT norm FROM reply_habits")} == {"go ahead"}
    assert j.reply_suggestions.forget("never seen") == 0
    assert j.reply_suggestions.clear() == 1
    assert j.reply_suggestions.suggest()["text"] is None


def test_setting_off_stops_learning_and_suggesting(settings):
    j = make(settings)
    say(j, OFFER)
    learn(j, "yes", 3)
    settings.reply_suggestions_enabled = False
    assert j.reply_suggestions.suggest() == {"text": None, "enabled": False}
    assert j.reply_suggestions.record("yes") is False
    assert j.db.query_one("SELECT uses FROM reply_habits")["uses"] == 3


# -- API + safety -----------------------------------------------------------------------------------------
def _client(settings):
    settings.jarvis_owner_password = "s3cret"
    j = make(settings)
    app = create_app(settings, j)
    return app, j


def test_chat_learns_only_typed_composer_text_and_suggestion_is_served(settings):
    app, j = _client(settings)
    with TestClient(app) as c:
        c.post("/login", data={"password": "s3cret"})
        for _ in range(MIN_USES):
            assert c.post("/api/chat", json={"text": "Yes do that", "compose": True}).status_code == 200
        # quick buttons (no compose flag), spoken text and attachments are not learned
        for _ in range(MIN_USES):
            c.post("/api/chat", json={"text": "Give me my briefing"})
            c.post("/api/chat", json={"text": "go ahead", "mode": "voice", "compose": True})
            c.post("/api/chat", json={"text": "with file", "compose": True,
                                      "attachments": [{"name": "a.txt", "mime": "text/plain", "data": "eA=="}]})
        assert {r["norm"] for r in j.db.query("SELECT norm FROM reply_habits")} == {"yes do that"}

        got = c.get("/api/reply-suggestion", params={"prefix": "ye"}).json()
        assert got["text"] == "Yes do that"
        assert c.get("/api/reply-suggestion", params={"prefix": "zz"}).json()["text"] is None
        # (counted per context: the very first reply had no Jarvis message before it)
        assert sum(r["uses"] for r in c.get("/api/reply-suggestions").json()) == MIN_USES

        assert c.post("/api/reply-suggestions/forget", json={"text": "yes do that"}).json()["forgotten"] >= 1
        assert c.get("/api/reply-suggestion", params={"prefix": "ye"}).json()["text"] is None
        cleared = c.delete("/api/reply-suggestions")
        assert cleared.status_code == 200 and cleared.json() == {"forgotten": 0}


def test_suggestion_endpoints_need_login(settings):
    app, _ = _client(settings)
    with TestClient(app) as c:
        assert c.get("/api/reply-suggestion").status_code == 401
        assert c.delete("/api/reply-suggestions").status_code == 401
        assert c.post("/api/reply-suggestions/forget", json={"text": "yes"}).status_code == 401


def test_suggesting_never_sends_or_approves_anything(settings):
    app, j = _client(settings)
    say(j, "I've queued that for your approval.")
    learn(j, "approve", 3)
    pending_id = j.db.create_action("note", "Do a thing", {})
    with TestClient(app) as c:
        c.post("/login", data={"password": "s3cret"})
        assert c.get("/api/reply-suggestion").json()["text"] == "approve"
    assert j.brain.messages == []  # no chat turn was started by merely being offered a suggestion
    assert j.db.get_action(pending_id)["status"] == "pending"  # the approval is still waiting for a real tap
