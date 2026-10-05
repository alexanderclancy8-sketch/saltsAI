"""/api/chat/stream carries ONE turn: the one it was opened for.

The event bus carries every turn. A new message sent while the previous reply is still being written (the
HTTP-fallback path does not cancel the old turn first) opens a second stream that is subscribed while the old turn
is still publishing; the old turn's `reply` used to be taken for this stream's own end, so the new message got the OLD
answer's last event and never its own.
"""
from __future__ import annotations

import json
import threading
import time

import httpx

from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.main import create_app
from tests.fakes import message, text_block
from tests.live_server import LiveServer, SlowClient


def _events(lines):
    for line in lines:
        if line.startswith("data: "):
            yield json.loads(line[6:])


def test_a_second_stream_does_not_end_on_the_first_turns_reply(tmp_path):
    client = SlowClient([message([text_block("Alpha one is here. Alpha two is here.")]),
                         message([text_block("Bravo one is here. Bravo two is here.")])], hold_after=3)
    j = Jarvis(Settings(data_dir=tmp_path / "data", scheduler_enabled=False, anthropic_api_key="test", _env_file=None), client=client)
    srv = LiveServer(create_app(j.settings, j))
    first_started = threading.Event()
    try:
        def first():  # the old turn: stays connected (the fallback does not abort it) and is held open mid-reply
            with httpx.Client(timeout=30) as http:
                with http.stream("POST", srv.url + "/api/chat/stream", json={"text": "first", "mode": "typed"}) as r:
                    for ev in _events(r.iter_lines()):
                        if ev["type"] == "delta":
                            first_started.set()
                        if ev["type"] == "reply":
                            return

        t = threading.Thread(target=first, daemon=True)
        t.start()
        assert first_started.wait(10)
        seen = []
        with httpx.Client(timeout=30) as http:
            with http.stream("POST", srv.url + "/api/chat/stream", json={"text": "second", "mode": "typed"}) as r:
                time.sleep(0.5)       # subscribed, and the first turn is still going
                client.gate.set()     # the first turn finishes now, publishing its reply while this stream is open
                for ev in _events(r.iter_lines()):
                    seen.append(ev)
                    if ev["type"] == "reply":
                        break
        t.join(10)
        types = [e["type"] for e in seen]
        assert types[0] == "user_message" and seen[0]["data"]["text"] == "second", types
        assert seen[-1]["type"] == "reply" and seen[-1]["data"]["text"].startswith("Bravo one is here"), seen[-1]
        assert not any("Alpha" in e["data"].get("text", "") for e in seen if e["type"] in ("delta", "reply"))
    finally:
        client.gate.set()
        srv.stop()
