import asyncio
import json

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
    # The tool loop itself is what this tests, so the accounts here are a CONNECTED source (the sample ledger standing in
    # for Sage): a tool built on sample data is withheld from the model instead - see tests/test_demo_guard.py.
    j.finance.demo = False
    reply = await j.brain.ask("How's cash?", "voice")
    assert reply == "Cash is healthy, sir."
    roles = [m["role"] for m in j.brain.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    tool_result = j.brain.messages[2]["content"][0]
    assert tool_result["type"] == "tool_result" and "cash_at_bank" in tool_result["content"]
    call = j.client.beta.messages.calls[0]
    assert call["model"] == "claude-sonnet-5-5" and call["fallbacks"] == "default"  # voice defaults to the quicker model
    assert call["output_config"] == {"effort": "low"}  # spoken turns use the quickest effort
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


async def test_interrupt_stops_a_running_turn_and_frees_the_lock(settings):
    import asyncio

    from tests.fakes import message, text_block

    class NeverEnds:
        """An async context manager that hangs until cancelled - stands in for a stuck Claude stream."""
        def __init__(self, started):
            self.started = started

        async def __aenter__(self):
            self.started.set()
            await asyncio.sleep(10)
            raise AssertionError("should have been cancelled before this")

        async def __aexit__(self, *exc):
            return False

    j = make(settings, [message([text_block("Cash is healthy, sir.")])])
    started = asyncio.Event()
    j.client.beta.messages.stream = lambda *a, **k: NeverEnds(started)  # type: ignore[method-assign]

    task = asyncio.create_task(j.brain.ask("How's cash?"))
    await asyncio.wait_for(started.wait(), timeout=2)
    assert await j.brain.interrupt() is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not j.brain._lock.locked()  # freed straight away, so the next message doesn't queue behind it
    await j.http.aclose()


async def test_max_brain_interrupt_calls_the_sdk_and_recovers_from_failure(settings, monkeypatch):
    import claude_agent_sdk

    settings.llm_backend = "max"
    settings.claude_code_oauth_token = "sk-ant-oat-test"

    class FakeClient:
        def __init__(self, options):
            self.options, self.interrupted, self.fail = options, False, False

        async def connect(self, prompt=None):
            pass

        async def query(self, prompt, session_id="default"):
            pass

        async def receive_response(self):
            return
            yield  # pragma: no cover - makes this an async generator

        async def interrupt(self):
            if self.fail:
                raise RuntimeError("connection gone")
            self.interrupted = True

        async def disconnect(self):
            pass

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClient)
    j = Jarvis(settings)
    assert await j.brain.interrupt() is False  # nothing connected yet

    await j.brain.warm()
    client = j.brain._client
    assert await j.brain.interrupt() is True and client.interrupted is True

    await j.brain.warm()  # a fresh client after the clean interrupt above
    j.brain._client.fail = True
    assert await j.brain.interrupt() is True  # recovers instead of raising
    assert j.brain._client is None  # disconnected so the next message reconnects

    await j.brain.close()
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


def test_microsoft_sign_in_lets_managers_in(settings, monkeypatch):
    settings.jarvis_owner_password = "s3cret"
    settings.manager_emails = "alex@example.com, Partner@Example.com"
    settings.owner_email, settings.partner_email, settings.partner_name = "alex@example.com", "partner@example.com", "Sam"
    j = make(settings, [message([text_block("Morning, Sam.")])])
    app = create_app(settings, j)
    partner = {"X-MS-CLIENT-PRINCIPAL-IDP": "aad", "X-MS-CLIENT-PRINCIPAL-NAME": "partner@example.com"}
    stranger = {"X-MS-CLIENT-PRINCIPAL-IDP": "aad", "X-MS-CLIENT-PRINCIPAL-NAME": "engineer@example.com"}
    with TestClient(app) as c:
        # Without App Service sign-in switched on, the headers could be forged, so they count for nothing.
        assert c.get("/api/status", headers=partner).status_code == 401
        monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "True")
        assert c.get("/api/status", headers=stranger).status_code == 401
        assert c.get("/api/status", headers=partner).status_code == 200
        assert c.post("/api/chat", json={"text": "Morning"}, headers=partner).json()["reply"] == "Morning, Sam."
        assert c.post("/logout", headers=partner, follow_redirects=False).headers["location"] == "/.auth/logout"
    assert j.brain.messages[0]["content"][-1]["text"].split("\n")[0].endswith("· from Sam]")


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


async def test_max_brain_keeps_one_claude_code_running(settings, monkeypatch):
    import claude_agent_sdk
    from claude_agent_sdk import ResultMessage, StreamEvent

    settings.llm_backend = "max"
    settings.claude_code_oauth_token = "sk-ant-oat-test"
    started = []

    class FakeClient:
        def __init__(self, options):
            self.options, self.prompts, self.closed = options, [], False
            started.append(self)

        async def connect(self, prompt=None):
            pass

        async def query(self, prompt, session_id="default"):
            self.prompts.append(prompt)

        async def receive_response(self):
            delta = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Right, all quiet."}}
            yield StreamEvent(uuid="u1", session_id="s1", event=delta)
            yield ResultMessage(subtype="success", duration_ms=5, duration_api_ms=5, is_error=False, num_turns=1,
                                session_id="s1", result="Right, all quiet.")

        async def disconnect(self):
            self.closed = True

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClient)
    j = Jarvis(settings)
    await j.brain.warm()
    assert await j.brain.ask("Anything urgent?", "voice") == "Right, all quiet."
    assert await j.brain.ask("And the inbox?", "voice") == "Right, all quiet."
    assert len(started) == 1 and len(started[0].prompts) == 2  # one Claude Code process for both messages
    assert started[0].options.effort == "low" and started[0].options.resume is None

    await j.brain.ask("Draft the tender answers", "typed")  # typed chat thinks harder: restart, same conversation
    assert len(started) == 2 and started[0].closed
    assert started[1].options.effort == "medium" and started[1].options.resume == "s1"

    j.brain.reset()
    await j.brain.ask("Start again", "typed")  # a reset starts a new conversation
    assert len(started) == 3 and started[2].options.resume is None

    await j.brain.close()
    assert started[2].closed
    await j.http.aclose()


def test_interrupt_endpoint(settings):
    app = create_app(settings, make(settings))
    with TestClient(app) as c:
        r = c.post("/api/interrupt")
        assert r.status_code == 200 and r.json() == {"stopped": False}  # nothing running


def test_chat_and_tts_endpoints(settings, monkeypatch):
    # No ElevenLabs/Azure key configured -> falls back to Piper (free, local, no API), not the browser's own
    # voice - real synthesis is exercised in test_voice.py; here just prove the endpoint actually returns
    # audio rather than hitting the network/onnxruntime for a full model download in every test run.
    import wave
    from io import BytesIO

    def fake_synthesize(self, model_path, text):
        buf = BytesIO()
        with wave.open(buf, "wb") as wav_file:
            wav_file.setnchannels(1); wav_file.setsampwidth(2); wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00\x00" * 10)
        return buf.getvalue()

    from pathlib import Path

    monkeypatch.setattr("jarvis.integrations.voice.Voice._ensure_piper_voice",
                        lambda self, voice, quality: _async_return(Path("fake.onnx")))
    monkeypatch.setattr("jarvis.integrations.voice.Voice._piper_synthesize", fake_synthesize)

    app = create_app(settings, make(settings, [message([text_block("Hello, sir.")])]))
    with TestClient(app) as c:
        r = c.post("/api/chat", json={"text": "hello", "mode": "voice"})
        assert r.status_code == 200 and r.json()["reply"] == "Hello, sir."
        r = c.post("/api/tts", json={"text": "Good evening"})
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content.startswith(b"RIFF")
        assert c.post("/api/chat", json={"text": ""}).status_code == 422


async def _async_return(value):
    return value


def test_chat_stream_endpoint_streams_the_same_events_the_websocket_does(settings):
    """The WebSocket-down fallback: still word-by-word, not a wait for the whole reply."""
    app = create_app(settings, make(settings, [message([text_block("Good afternoon, sir.")])]))
    with TestClient(app) as c:
        r = c.post("/api/chat/stream", json={"text": "hello", "mode": "voice"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[len("data: "):]) for line in r.text.split("\n\n") if line.startswith("data: ")]
        types = [e["type"] for e in events]
        assert types[0] == "user_message" and "thinking" in types and types[-1] == "reply"
        assert len(events) >= 3  # more than one line arrived - it wasn't buffered into a single reply
        assert events[-1]["data"]["text"] == "Good afternoon, sir."
        assert events[0]["data"]["text"] == "hello" and events[0]["data"]["mode"] == "voice"


def test_status_survives_a_broken_connection(settings):
    """A misconfigured FSM (or any other live integration) must not take the whole display down."""
    app = create_app(settings, make(settings))
    with TestClient(app) as c:
        j = app.state.j

        async def boom(*a, **k):
            raise ConnectionError("could not connect")

        j.fsm.jobs = boom  # a real integration would fail like this against a wrong address
        r = c.get("/api/status")
        assert r.status_code == 200
        assert r.json()["customer_watch"] == []  # degraded, not crashed


def test_all_body_endpoints_declare_json_bodies(settings):
    app = create_app(settings, make(settings))
    schema = app.openapi()
    for path in ("/api/chat", "/api/chat/stream", "/api/tts"):
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
