"""In-process pub/sub used to push live updates to every connected HUD display."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# True while a headless background turn (an automation, see services/proactive.py) is asking the brain. The events
# that make up a chat turn are then not published, so a background check never types into the open chat by itself:
# only what Proactive.post() decides to say reaches the conversation. Everything else (approvals, notifications,
# display panels, ...) is published as normal.
quiet_turn: contextvars.ContextVar[bool] = contextvars.ContextVar("jarvis_quiet_turn", default=False)
QUIET_EVENTS = frozenset({"user_message", "thinking", "delta", "tool", "reply", "error"})


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self.last_event: dict[str, float] = {}  # event type -> time.monotonic() when it was last published

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event_type: str, data: Any = None) -> None:
        if event_type in QUIET_EVENTS and quiet_turn.get():
            return
        self.last_event[event_type] = time.monotonic()
        message = {"type": event_type, "data": data}
        for q in list(self._subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                log.warning("HUD subscriber queue full; dropping %s event", event_type)
