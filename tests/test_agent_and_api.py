import asyncio

import pytest
from fastapi.testclient import TestClient

from jarvis.core import Jarvis
from jarvis.main import create_app
from tests.fakes import FakeClient, message, text_block, tool_block


def make(settings, script=None):
    j = Jarvis(settings, client=FakeClient(script))
    return j


async def test_tool_loop_and_history(settings):
    j = make(settings, [message([tool_block("finance_snapshot", {})], "tool_use"),
                        message([text_block("Cash is healthy, sir.")])])
    reply = await j.brain.ask("How's cash?", "voice")
    assert reply == "Cash is healthy, sir."
    roles = [m["role"] for m in j.brain.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    tool_result = j.brain.messages[2]["content"][0]
    assert tool_result["type"] == "tool_result" and "cash_at_bank" in tool_result["content"]
    call = j.client.beta.messages.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["output_config"] == {"effort": "medium"}  # spoken turns use the lower-latency effort
    assert j.brain.messages[0]["content"][-1]["text"].startswith("[spoken")
    await j.http.aclose()


async def test_invalid_tool_input_is_reported_not_run(settings):
    j = make(settings, [message([tool_block("staff_productivity", {"days": "lots"})], "tool_use"),
                        message([text_block("ok")])])
    await j.brain.ask("productivity?")
    result = j.brain.messages[2]["content"][0]
    assert result["is_error"] and "INVALID_INPUT" in result["content"]
    await j.http.aclose()


async def test_refusal_rolls_back_turn(settings):
    j = make(settings, [message([], "refusal")])
    reply = await j.brain.ask("something")
    assert "can't help" in reply and j.brain.messages == []
    await j.http.aclose()


async def test_external_email_needs_approval(settings):
    j = make(settings, [message([tool_block("email_send", {"to": ["someone@example.com"], "subject": "Hi",
                                                           "body": "Hello"})], "tool_use"),
                        message([text_block("Queued for your approval, sir.")])])
    await j.brain.ask("email them")
    pending = j.db.pending_actions()
    assert len(pending) == 1 and pending[0]["kind"] == "tool:email_send"
    assert "Queued" in j.brain.messages[2]["content"][0]["content"] or "queued" in j.brain.messages[2]["content"][0]["content"]
    result = await j.actions.approve(pending[0]["id"])
    assert result.startswith("Approved")
    await asyncio.sleep(0.05)
    assert j.db.get_action(pending[0]["id"])["status"] == "done"
    await j.http.aclose()


def test_api_requires_login_when_password_set(settings):
    settings.jarvis_owner_password = "s3cret"
    app = create_app(settings, make(settings))
    with TestClient(app) as c:
        assert c.get("/api/status").status_code == 401
        assert c.get("/", follow_redirects=False).status_code == 307
        assert c.post("/login", data={"password": "wrong"}, follow_redirects=False).headers["location"].endswith("error=1")
        r = c.post("/login", data={"password": "s3cret"}, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/api/status").status_code == 200


def test_local_mode_rejects_proxied_requests(settings):
    app = create_app(settings, make(settings))
    with TestClient(app) as c:
        assert c.get("/api/status").status_code == 200
        assert c.get("/api/status", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401


def test_staff_report_needs_key(settings):
    settings.jarvis_owner_password = "pw"
    settings.staff_report_key = "team-key"
    j = make(settings)
    j.issues.process = lambda issue_id: asyncio.sleep(0)  # skip triage in this test
    app = create_app(settings, j)
    with TestClient(app) as c:
        form = {"name": "Sam Test", "title": "Photos won't upload", "description": "Spinner forever",
                "severity": "high"}
        assert c.post("/api/issues/report", data={**form, "key": "nope"}).status_code == 403
        r = c.post("/api/issues/report", data={**form, "key": "team-key"})
        assert r.status_code == 200 and "logged" in r.json()["message"]
        assert j.db.get_issue(r.json()["id"])["severity"] == "high"


async def test_max_backend_builds_tools_without_api_key(settings):
    settings.llm_backend = "max"
    settings.claude_code_oauth_token = "sk-ant-oat-test"
    j = Jarvis(settings)
    from jarvis.brain.max_backend import MaxBrain, base_options

    assert isinstance(j.brain, MaxBrain) and j.client is None
    assert j.brain.server["name"] == "jarvis"
    opts = base_options(settings, tools=["WebSearch"])
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-test" and opts.setting_sources == []
    assert opts.permission_mode == "dontAsk"
    assert "your Claude Max subscription" in j.connections()["Claude"]
    await j.http.aclose()


def test_chat_and_tts_endpoints(settings):
    app = create_app(settings, make(settings, [message([text_block("Hello, sir.")])]))
    with TestClient(app) as c:
        r = c.post("/api/chat", json={"text": "hello", "mode": "voice"})
        assert r.status_code == 200 and r.json()["reply"] == "Hello, sir."
        # no ElevenLabs/Azure key configured -> tells the browser to use its own voice
        r = c.post("/api/tts", json={"text": "Good evening"})
        assert r.status_code == 503 and r.json()["fallback"] == "browser"
        assert c.post("/api/chat", json={"text": ""}).status_code == 422


def test_all_body_endpoints_declare_json_bodies(settings):
    app = create_app(settings, make(settings))
    schema = app.openapi()
    for path in ("/api/chat", "/api/tts"):
        assert "requestBody" in schema["paths"][path]["post"], path



async def test_changes_are_only_suggested_until_approved(settings):
    j = make(settings, [message([tool_block("stock_move", {"kind": "receive", "item": "BAT-12V7", "qty": 10})], "tool_use"),
                        message([text_block("I've queued that for your approval, sir.")])])
    before = next(i for i in j.stores.levels()["items"] if i["sku"] == "BAT-12V7")["stores"]
    await j.brain.ask("We've had 10 batteries delivered")
    after_suggest = next(i for i in j.stores.levels()["items"] if i["sku"] == "BAT-12V7")["stores"]
    assert after_suggest == before  # nothing changed yet
    action = j.db.pending_actions()[0]
    assert action["kind"] == "tool:stock_move" and "receive 10" in action["summary"]
    await j.actions.approve(action["id"])
    await asyncio.sleep(0.05)
    assert next(i for i in j.stores.levels()["items"] if i["sku"] == "BAT-12V7")["stores"] == before + 10
    await j.http.aclose()


async def test_software_bug_fix_is_suggested_not_started(settings):
    j = make(settings)
    j.fixer.gh = object()  # pretend the FSM repo is connected
    started = []
    j.fixer.attempt = lambda issue_id: started.append(issue_id)
    j.client.beta.messages.parse_result = {
        "summary": "Photo upload hangs", "category": "software_bug", "severity": "high", "software_fixable": True,
        "likely_area": "job sheets", "suggested_next_steps": ["fix upload"], "reply_to_reporter": "Thanks"}
    issue = await j.issues.report(reporter="Sam", title="Photos hang", description="spinner", notify=False, process=False)
    await j.issues.process(issue["id"])
    assert started == []
    action = j.db.pending_actions()[0]
    assert action["kind"] == "tool:issue_fix" and action["payload"]["args"] == {"issue_id": issue["id"]}
    await j.http.aclose()


async def test_suggestion_sweep_and_snooze(settings):
    j = make(settings)
    current = await j.suggestions.sweep(announce=False)
    keys = {s["key"] for s in current}
    assert "unbilled" in keys and "remedials" in keys
    assert all(s["prompt"] for s in current)
    j.suggestions.decide("unbilled", "dismissed")
    again = await j.suggestions.sweep(announce=False)
    assert "unbilled" not in {s["key"] for s in again}  # snoozed until tomorrow
    await j.http.aclose()


def test_suggestion_api(settings):
    j = make(settings)
    app = create_app(settings, j)
    with TestClient(app) as c:
        items = c.post("/api/suggestions/refresh").json()
        key = items[0]["key"]
        r = c.post(f"/api/suggestions/{key}/done")
        assert r.status_code == 200 and r.json()["prompt"]
        assert c.post(f"/api/suggestions/{key}/explode").status_code == 400


async def test_end_of_day_wrap_up(settings):
    j = make(settings)
    data = await j.wrapup.gather()
    for key in ("today", "slipped_today", "awaiting_approval", "suggestions", "tomorrow", "unread_email"):
        assert key in data, key
    assert data["tomorrow"]["jobs"] >= 0 and data["today"]["jobs_today"] > 0
    text = await j.wrapup.run(deliver=True)
    assert text == "Certainly, sir."  # the scripted stand-in model's reply
    assert any(n["title"] == "End-of-day wrap-up" for n in j.db.recent_notifications())
    await j.http.aclose()


async def test_startup_notes_are_remembered_once(settings):
    settings.jarvis_notes = "Acme Monitoring handle our out-of-hours | Vans park at the office overnight"
    j = make(settings)
    facts = [m["fact"] for m in j.db.memories()]
    assert facts == ["Acme Monitoring handle our out-of-hours", "Vans park at the office overnight"]
    j2 = Jarvis(settings, db=j.db, client=j.client)  # restart: no duplicates
    assert len(j2.db.memories()) == 2
    assert "Acme Monitoring" in j2.brain.system[1]["text"]
    await j.http.aclose()
    await j2.http.aclose()
