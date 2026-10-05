"""Helpers for tests that need the real app on a real local port plus a Claude stand-in that streams slowly.

`SlowClient` is a scripted stand-in for the Anthropic client whose reply arrives a word at a time and can be held
open (a `threading.Event` gate) so a test can look at the page / the HTTP stream while "Claude" is still writing,
and can see whether the server really cancelled the work when the owner pressed Stop.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from types import SimpleNamespace
from typing import Any

import uvicorn

from tests.fakes import message, text_block


class LiveServer:
    def __init__(self, app):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 30
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        assert self.server.started, "test server did not start"
        self.url = f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


class _SlowStream:
    def __init__(self, client: "SlowClient", msg: SimpleNamespace):
        self.client, self.msg = client, msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            try:
                for block in self.msg.content:
                    if block.type == "text":
                        words = block.text.split(" ")
                        for n, word in enumerate(words):
                            yield SimpleNamespace(type="text", text=word + (" " if n < len(words) - 1 else ""))
                            self.client.words_sent += 1
                            # Hold the reply open after the first few words until the test lets it finish.
                            if n + 1 == self.client.hold_after and not self.client.gate.is_set():
                                while not self.client.gate.is_set():
                                    await asyncio.sleep(0.02)
                            else:
                                await asyncio.sleep(self.client.delay)
                    elif block.type == "tool_use":
                        yield SimpleNamespace(type="content_block_start", content_block=block)
            except asyncio.CancelledError:
                self.client.cancelled.set()  # the server really stopped working on this reply
                raise
        return gen()

    async def get_final_message(self):
        return self.msg


class _SlowMessages:
    def __init__(self, client: "SlowClient"):
        self.client = client

    def stream(self, **kwargs):
        self.client.calls.append(kwargs)
        c = self.client
        msg = c.script.pop(0) if c.script else message([text_block(c.default_text)])
        return _SlowStream(c, msg)


class SlowClient:
    """Streams its scripted replies a word at a time. If `hold_after` is set, the reply stops after that many words
    until `gate` is set; `cancelled` is set if the server cancels the stream (Stop / client disconnect)."""

    def __init__(self, script: list[Any] | None = None, default_text: str = "Right, that is all sorted for you today.",
                 delay: float = 0.01, hold_after: int | None = None):
        self.script = list(script or [])
        self.default_text, self.delay, self.hold_after = default_text, delay, hold_after
        self.gate, self.cancelled = threading.Event(), threading.Event()
        if hold_after is None:
            self.gate.set()
        self.words_sent = 0
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=_SlowMessages(self))
