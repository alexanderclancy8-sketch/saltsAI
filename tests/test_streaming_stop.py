"""Console redesign, Phase 2: replies stream into the chat as they are written, and Stop really stops.

The mechanism already existed (the live WebSocket and the /api/chat/stream server-sent-events fallback both carry
`delta` events as Claude writes). These tests pin it down end to end - against a real server on a real port with a
Claude stand-in that is held open mid-reply - and cover the part that was missing: that Stop genuinely cancels the
work on the server (client abort over HTTP, a "stop" message over the WebSocket), leaves the conversation history
valid, and never leaves a half-answer to show up afterwards.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.brain.tools import TOOLS_BY_NAME
from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.main import create_app
from tests.fakes import FakeClient, message, text_block, tool_block
from tests.live_server import LiveServer, SlowClient


def _settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", scheduler_enabled=False, anthropic_api_key="test", _env_file=None)


def _events(lines):
    for line in lines:
        if line.startswith("data: "):
            yield json.loads(line[6:])


def _wait(cond, seconds=8.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def live(tmp_path):
    client = SlowClient(hold_after=3)
    j = Jarvis(_settings(tmp_path), client=client)
    srv = LiveServer(create_app(j.settings, j))
    yield srv, j, client
    client.gate.set()
    srv.stop()


# ------------------------------------------------------------------ streaming
async def test_the_reply_arrives_as_deltas_before_the_final_reply_event(tmp_path):
    j = Jarvis(_settings(tmp_path), client=FakeClient(default_text="Two jobs are late this morning."))
    q = j.bus.subscribe()
    await j.brain.ask("Anything late?", "typed")
    got = []
    while not q.empty():
        got.append(q.get_nowait())
    types = [e["type"] for e in got]
    assert types[0] == "user_message" and types[1] == "thinking" and types[-1] == "reply"
    deltas = [e["data"]["text"] for e in got if e["type"] == "delta"]
    assert len(deltas) >= 4 and "".join(deltas).strip() == "Two jobs are late this morning."
    assert types.index("delta") < types.index("reply")
    await j.http.aclose()


def test_over_http_the_words_reach_the_browser_while_the_reply_is_still_being_written(live):
    srv, j, client = live
    seen = []
    with httpx.Client(timeout=15) as http:
        with http.stream("POST", srv.url + "/api/chat/stream", json={"text": "Tell me about today", "mode": "typed"}) as r:
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
            lines = r.iter_lines()
            for ev in _events(lines):
                seen.append(ev["type"])
                if ev["type"] == "delta" and seen.count("delta") == 2:
                    # Claude is held open after three words, so the stream cannot have finished yet.
                    assert "reply" not in seen and not client.gate.is_set()
                    client.gate.set()
                if ev["type"] == "reply":
                    assert ev["data"]["text"].startswith("Right, that is all sorted")
                    assert isinstance(ev["data"]["elapsed_ms"], int)  # the finished reply carries its time
                    break
    assert seen[:2] == ["user_message", "thinking"] and seen.count("delta") >= 4


# ------------------------------------------------------------------ Stop
def test_aborting_the_http_stream_cancels_the_server_side_work(live):
    srv, j, client = live
    with httpx.Client(timeout=15) as http:
        with http.stream("POST", srv.url + "/api/chat/stream", json={"text": "Tell me about today", "mode": "typed"}) as r:
            for ev in _events(r.iter_lines()):
                if ev["type"] == "delta":
                    break
        # leaving the `with` closes the connection - what the browser's AbortController does on Stop
    assert _wait(client.cancelled.is_set), "the server kept generating after the client went away"
    assert _wait(lambda: not j.brain._lock.locked()), "the turn still holds the conversation lock"
    assert not j.brain._active


def test_a_stop_message_on_the_websocket_cancels_the_turn_and_says_so(tmp_path):
    client = SlowClient(hold_after=2)
    j = Jarvis(_settings(tmp_path), client=client)
    app = create_app(j.settings, j)
    try:
        with TestClient(app) as c, c.websocket_connect("/ws") as ws:
            ws.send_json({"type": "chat", "text": "Give me the long answer", "mode": "typed"})
            while ws.receive_json()["type"] != "delta":
                pass
            ws.send_json({"type": "stop"})
            types = []
            while "stopped" not in types:
                types.append(ws.receive_json()["type"])
            assert _wait(client.cancelled.is_set)
            # The cancelled turn never produces a reply or an error afterwards.
            client.gate.set()
            time.sleep(0.3)
            assert "reply" not in types and "error" not in types
    finally:
        client.gate.set()


async def test_stop_in_the_middle_of_a_tool_call_leaves_the_history_valid(tmp_path, monkeypatch):
    """Stopped half-way through a tool call the history used to end on a tool_use with no result, and the API
    rejects that on every later message. The whole turn is now rolled back, so the next question just works."""
    j = Jarvis(_settings(tmp_path), client=FakeClient([
        message([text_block("Checking."), tool_block("fsm_jobs", {})], "tool_use"),
        message([text_block("Second question answered.")]),
    ]))
    started = asyncio.Event()

    async def slow(_j, _args):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(TOOLS_BY_NAME["fsm_jobs"], "handler", slow)
    task = asyncio.create_task(j.brain.ask("What jobs are on?", "typed"))
    await asyncio.wait_for(started.wait(), 5)
    assert await j.brain.interrupt() is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert j.brain.messages == []  # nothing half-finished is left in the conversation
    assert not j.brain._lock.locked()
    reply = await j.brain.ask("And now?", "typed")
    assert reply == "Second question answered."
    assert [m["role"] for m in j.brain.messages] == ["user", "assistant"]
    await j.http.aclose()


# ------------------------------------------------------------------ Stop on the Claude Max (Agent SDK) backend
def _max_jarvis(settings, monkeypatch, behaviour):
    import claude_agent_sdk

    settings.llm_backend = "max"
    settings.claude_code_oauth_token = "sk-ant-oat-test"

    class FakeSDKClient:
        def __init__(self, options):
            self.options = options
            self.interrupt_seen = asyncio.Event()

        async def connect(self, prompt=None):
            pass

        async def query(self, prompt, session_id="default"):
            pass

        async def receive_response(self):
            async for item in behaviour(self):
                yield item

        async def interrupt(self):
            self.interrupt_seen.set()

        async def disconnect(self):
            pass

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeSDKClient)
    return Jarvis(settings)


def _delta(text):
    from claude_agent_sdk import StreamEvent

    return StreamEvent(uuid="u", session_id="s", event={"type": "content_block_delta",
                                                         "delta": {"type": "text_delta", "text": text}})


def _result(**kw):
    from claude_agent_sdk import ResultMessage

    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                         session_id="s", **kw)


async def test_max_backend_streams_deltas_then_one_reply(tmp_path, monkeypatch):
    async def behaviour(client):
        yield _delta("Three jobs ")
        yield _delta("are on today.")
        yield _result(result="Three jobs are on today.")

    j = _max_jarvis(_settings(tmp_path), monkeypatch, behaviour)
    q = j.bus.subscribe()
    reply = await j.brain.ask("What's on?", "typed")
    got = []
    while not q.empty():
        got.append(q.get_nowait())
    assert reply == "Three jobs are on today."
    assert [e["data"]["text"] for e in got if e["type"] == "delta"] == ["Three jobs ", "are on today."]
    assert [e["type"] for e in got][-1] == "reply" and "elapsed_ms" in got[-1]["data"]
    await j.brain.close()
    await j.http.aclose()


async def test_max_backend_stop_publishes_no_half_answer_and_no_error(tmp_path, monkeypatch):
    async def behaviour(client):
        yield _delta("Let me have a look at ")
        await asyncio.wait_for(client.interrupt_seen.wait(), 5)  # held open until Stop is pressed
        yield _result(result="")

    j = _max_jarvis(_settings(tmp_path), monkeypatch, behaviour)
    q = j.bus.subscribe()
    task = asyncio.create_task(j.brain.ask("Long one please", "typed"))
    for _ in range(200):  # until the first words are out
        await asyncio.sleep(0.02)
        if not q.empty() and any(e["type"] == "delta" for e in list(q._queue)):
            break
    assert await j.brain.interrupt() is True
    await asyncio.wait_for(task, 5)
    types = []
    while not q.empty():
        types.append(q.get_nowait()["type"])
    assert "delta" in types and "reply" not in types and "error" not in types
    assert not any(m["text"].startswith("Let me have") for m in j.db.recent_transcript(10) if m["role"] == "assistant")
    await j.brain.close()
    await j.http.aclose()


async def test_max_backend_stop_before_the_question_is_sent_sends_nothing(tmp_path, monkeypatch):
    sent = []

    async def behaviour(client):
        sent.append("received")
        yield _result(result="should not happen")

    j = _max_jarvis(_settings(tmp_path), monkeypatch, behaviour)
    brain = j.brain
    real_connected = brain._connected

    async def connected_then_stop(*a, **kw):
        client = await real_connected(*a, **kw)
        await brain.interrupt()  # Stop arrives while Claude Code is still starting up
        return client

    monkeypatch.setattr(brain, "_connected", connected_then_stop)
    q = j.bus.subscribe()
    await brain.ask("Hello", "typed")
    types = []
    while not q.empty():
        types.append(q.get_nowait()["type"])
    assert sent == [] and "reply" not in types and "error" not in types
    await brain.close()
    await j.http.aclose()
