"""Phase 4a: the Memory pop-up's backend - list, edit and delete what Jarvis has learned ("Things Jarvis should know",
things he has learned by himself, and learned replies). Deleting must really remove it from what Jarvis reads next turn,
including after a restart, and none of it is reachable by the model."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from jarvis import auth
from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.main import create_app
from jarvis.services.reply_suggestions import MIN_USES
from tests.fakes import FakeClient

NOTES = "First County Monitoring handle our out-of-hours. | Alex prefers quotes sent before 10am"


def make(tmp_path, notes=NOTES, **kw):
    s = Settings(data_dir=tmp_path / "data", scheduler_enabled=False, _env_file=None, anthropic_api_key="test",
                 jarvis_notes=notes, **kw)
    j = Jarvis(s, client=FakeClient())
    j.db.remember("The Ilkley site key safe is held by reception on Mondays.")        # learned by Jarvis, not from the setting
    return s, j, create_app(s, j)


def system_text(j) -> str:
    j.brain.refresh_system()
    system = j.brain.system
    return system if isinstance(system, str) else json.dumps(system)


def learn(j, text, times=MIN_USES, context="offer"):
    for _ in range(times):
        j.reply_suggestions.record(text, "typed", context)


def test_listing_separates_notes_learned_facts_and_learned_replies(tmp_path):
    s, j, app = make(tmp_path)
    learn(j, "yes do that")
    with TestClient(app) as c:
        data = c.get("/api/memory").json()
    assert [f["text"] for f in data["notes"]] == ["First County Monitoring handle our out-of-hours.", "Alex prefers quotes sent before 10am"]
    assert [f["text"] for f in data["learned"]] == ["The Ilkley site key safe is held by reception on Mondays."]
    assert all(isinstance(f["id"], int) and f["added"] for f in data["notes"] + data["learned"])
    (reply,) = data["replies"]
    assert reply["text"] == "yes do that" and reply["uses"] == MIN_USES and reply["context"] == "offer" and isinstance(reply["id"], int)


def test_editing_a_learned_fact_changes_what_jarvis_reads_next_turn(tmp_path):
    s, j, app = make(tmp_path)
    fact = j.db.memories()[-1]
    assert "held by reception on Mondays" in system_text(j)
    with TestClient(app) as c:
        r = c.post(f"/api/memory/facts/{fact['id']}", json={"text": "The Ilkley site key safe is held by reception on Fridays."})
        assert r.status_code == 200 and r.json()["text"] == "The Ilkley site key safe is held by reception on Fridays."
        text = system_text(j)
        assert "held by reception on Fridays" in text and "held by reception on Mondays" not in text
        assert j.db.get_memory(fact["id"])["fact"] == "The Ilkley site key safe is held by reception on Fridays."
        assert s.jarvis_notes == NOTES                                              # not a note: the setting is untouched


def test_deleting_a_learned_fact_removes_it_from_the_next_prompt(tmp_path):
    s, j, app = make(tmp_path)
    fact = j.db.memories()[-1]
    with TestClient(app) as c:
        assert c.delete(f"/api/memory/facts/{fact['id']}").status_code == 200
        assert c.delete(f"/api/memory/facts/{fact['id']}").status_code == 404
    assert j.db.get_memory(fact["id"]) is None and "Ilkley" not in system_text(j)
    assert all(m["id"] != fact["id"] for m in j.db.memories())


def test_deleting_a_note_also_removes_it_from_the_setting_so_a_restart_cannot_bring_it_back(tmp_path):
    s, j, app = make(tmp_path)
    note = next(m for m in j.db.memories() if m["fact"].startswith("First County"))
    with TestClient(app) as c:
        assert c.delete(f"/api/memory/facts/{note['id']}").status_code == 200
        assert "First County" not in system_text(j)
        assert s.jarvis_notes == "Alex prefers quotes sent before 10am"            # the setting was rewritten too
        listing = c.get("/api/memory").json()
        assert [f["text"] for f in listing["notes"]] == ["Alex prefers quotes sent before 10am"]
    # restart / settings reload: a new Jarvis seeds its notes again from the setting - the deleted one stays gone
    j2 = Jarvis(s, db=j.db, client=FakeClient())
    assert not any("First County" in m["fact"] for m in j2.db.memories())
    assert "First County" not in system_text(j2)
    # ...and the saved setting survives a fresh process reading the encrypted settings file
    from jarvis.settings_store import SettingsStore

    s3 = Settings(data_dir=tmp_path / "data", scheduler_enabled=False, _env_file=None, anthropic_api_key="test", jarvis_notes=NOTES)
    SettingsStore(s3).apply()
    assert s3.jarvis_notes == "Alex prefers quotes sent before 10am"


def test_deleting_the_last_note_leaves_the_setting_empty(tmp_path):
    s, j, app = make(tmp_path, notes="Only one note.")
    note = next(m for m in j.db.memories() if m["fact"] == "Only one note.")
    with TestClient(app) as c:
        assert c.delete(f"/api/memory/facts/{note['id']}").status_code == 200
    assert s.jarvis_notes == "" and not any(m["fact"] == "Only one note." for m in Jarvis(s, db=j.db, client=FakeClient()).db.memories())


def test_editing_a_note_rewrites_the_setting_and_survives_a_restart(tmp_path):
    s, j, app = make(tmp_path)
    note = next(m for m in j.db.memories() if m["fact"].startswith("Alex prefers"))
    with TestClient(app) as c:
        r = c.post(f"/api/memory/facts/{note['id']}", json={"text": "Alex prefers quotes sent before 9am."})
        assert r.status_code == 200
        assert s.jarvis_notes == "First County Monitoring handle our out-of-hours. | Alex prefers quotes sent before 9am."
        assert "before 9am" in system_text(j) and "before 10am" not in system_text(j)
        # the | separator can't be typed into a note (it would split it in two on the next start)
        assert c.post(f"/api/memory/facts/{note['id']}", json={"text": "a | b both"}).status_code == 422
    j2 = Jarvis(s, db=j.db, client=FakeClient())
    facts = [m["fact"] for m in j2.db.memories()]
    assert "Alex prefers quotes sent before 9am." in facts and "Alex prefers quotes sent before 10am" not in facts
    assert facts.count("Alex prefers quotes sent before 9am.") == 1


def test_memory_edit_validation(tmp_path):
    s, j, app = make(tmp_path)
    a, b = j.db.memories()[0], j.db.memories()[-1]
    with TestClient(app) as c:
        assert c.post(f"/api/memory/facts/{b['id']}", json={"text": "  "}).status_code == 422
        assert c.post(f"/api/memory/facts/{b['id']}", json={"text": "ok"}).status_code == 422           # too short
        assert c.post(f"/api/memory/facts/{b['id']}", json={"text": "x" * 1001}).status_code == 422
        assert c.post(f"/api/memory/facts/{b['id']}", json={"text": "bad‮text here"}).status_code == 422
        assert c.post(f"/api/memory/facts/{b['id']}", json={}).status_code == 422
        # another fact already says that (case and a full stop don't make it different)
        r = c.post(f"/api/memory/facts/{b['id']}", json={"text": a["fact"].upper().rstrip(".")})
        assert r.status_code == 409
        assert c.post("/api/memory/facts/9999", json={"text": "hello there"}).status_code == 404
        # rewording a fact to the same thing it already is, is fine
        assert c.post(f"/api/memory/facts/{b['id']}", json={"text": b["fact"]}).status_code == 200
    assert j.db.get_memory(b["id"])["fact"] == b["fact"] and j.db.get_memory(a["id"])["fact"] == a["fact"]


def test_learned_replies_can_be_edited_merged_and_deleted(tmp_path):
    s, j, app = make(tmp_path)
    learn(j, "yes do that", 3)
    learn(j, "go ahead", 4)
    with TestClient(app) as c:
        rows = {r["text"]: r for r in c.get("/api/memory").json()["replies"]}
        yes, go = rows["yes do that"], rows["go ahead"]
        assert j.reply_suggestions.suggest("")["text"] in ("go ahead", "yes do that")
        # reword
        assert c.post(f"/api/memory/replies/{yes['id']}", json={"text": "yes please do"}).status_code == 200
        assert {r["text"] for r in c.get("/api/memory").json()["replies"]} == {"yes please do", "go ahead"}
        assert j.reply_suggestions.suggest("yes p", context="offer")["text"] == "yes please do"       # still offered, uses kept
        # editing into another learned reply (same context) merges the two
        assert c.post(f"/api/memory/replies/{yes['id']}", json={"text": "Go ahead!"}).status_code == 200
        (merged,) = c.get("/api/memory").json()["replies"]
        assert merged["uses"] == 7 and merged["text"] == "Go ahead!"
        # a reply that looks like a secret, or isn't short, is refused like it would be when learning
        for bad in ("my password is hunter2", "pin 4821 please", "this is a very long reply that is not a habit at all ok", "line one\nline two", "call me on 07700900123"):
            assert c.post(f"/api/memory/replies/{merged['id']}", json={"text": bad}).status_code == 422, bad
        assert c.post("/api/memory/replies/9999", json={"text": "yes"}).status_code == 404
        # delete: gone from the list and no longer suggested
        assert c.delete(f"/api/memory/replies/{merged['id']}").status_code == 200
        assert c.delete(f"/api/memory/replies/{merged['id']}").status_code == 404
        assert c.get("/api/memory").json()["replies"] == []
    assert j.reply_suggestions.suggest("")["text"] is None


def test_memory_endpoints_need_the_owner_and_a_same_origin_click(tmp_path):
    s, j, app = make(tmp_path, jarvis_owner_password="a-long-password", public_base_url="https://jarvis.example.test")
    fact = j.db.memories()[-1]
    with TestClient(app, base_url="https://jarvis.example.test") as c:
        assert c.get("/api/memory").status_code == 401
        assert c.delete(f"/api/memory/facts/{fact['id']}").status_code == 401
        c.cookies.set(auth.COOKIE, auth.make_session(s))
        assert c.delete(f"/api/memory/facts/{fact['id']}", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert c.post(f"/api/memory/facts/{fact['id']}", json={"text": "edited elsewhere"}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert j.db.get_memory(fact["id"]) is not None
        assert c.delete(f"/api/memory/facts/{fact['id']}", headers={"Origin": "https://jarvis.example.test"}).status_code == 200


def test_the_model_has_no_tool_to_reword_or_clear_memory_through_the_console_path():
    from jarvis.brain.tools import TOOLS

    names = {t.name for t in TOOLS}
    assert {"remember", "forget"} <= names                     # the model's own existing, narrow tools are unchanged
    assert not {n for n in names if "memory" in n or "learned" in n or n in ("reply_suggestions", "edit_memory")}
    import re

    from jarvis.brain import tools

    src = open(tools.__file__, encoding="utf-8").read()
    assert not re.search(r"reply_suggestions\.(edit|delete|rows|clear|forget)|update_memory|MemoryBook", src)
