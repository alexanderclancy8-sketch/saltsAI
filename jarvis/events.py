"""In-process pub/sub used to push live updates to every connected HUD display."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

# True while a headless background turn (an automation, see services/proactive.py) is asking the brain. The events
# that make up a chat turn are then not published, so a background check never types into the open chat by itself:
# only what Proactive.post() decides to say reaches the conversation. Everything else (approvals, notifications,
# display panels, ...) is published as normal.
quiet_turn: contextvars.ContextVar[bool] = contextvars.ContextVar("jarvis_quiet_turn", default=False)
# True while a question check runs (the same variable as brain.checkmode.active, defined here so the bus has no brain import).
check_mode: contextvars.ContextVar[bool] = contextvars.ContextVar("jarvis_check_mode", default=False)
QUIET_EVENTS = frozenset({"user_message", "thinking", "delta", "tool", "reply", "error"})


class EventBus:
    def __init__(self, check_ok: bool = False) -> None:
        # check_ok: the private bus of a question-check brain (brain/checkmode.py). Every OTHER bus drops whatever is
        # published to it while a check is running, so a check can't reach a console, a display or a chat.
        self.check_ok = check_ok
        self._subscribers: set[asyncio.Queue] = set()
        # In-process listeners that see every published event (after the quiet-turn filter), synchronously. Used by
        # brain/trace.py to describe a chat turn from the tool events. A tap must be quick and must never raise.
        self._taps: list[Callable[[str, Any], None]] = []
        self.last_event: dict[str, float] = {}  # event type -> time.monotonic() when it was last published

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def add_tap(self, fn: Callable[[str, Any], None]) -> None:
        self._taps.append(fn)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event_type: str, data: Any = None) -> None:
        if event_type in QUIET_EVENTS and quiet_turn.get():
            return
        if not self.check_ok and check_mode.get():
            return
        self.last_event[event_type] = time.monotonic()
        for tap in self._taps:
            try:
                tap(event_type, data)
            except Exception:  # noqa: BLE001 - an observer can never break the thing it observes
                log.exception("Event tap failed for %s", event_type)
        message = {"type": event_type, "data": data}
        for q in list(self._subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                log.warning("HUD subscriber queue full; dropping %s event", event_type)
