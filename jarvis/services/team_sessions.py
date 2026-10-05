"""One cut-down Jarvis per signed-in team member (Team mode).

The owner's brain has ONE conversation and ONE event bus that every connected console shares - which is right for the owner
and wrong for anyone else. So a team session does not use ``j.brain`` or ``j.bus`` at all. ``TeamSessions.get(caller)``
gives each team session (the session id inside its signed cookie) its own:

* **brain** - the same two backends as the owner's (the API loop or Claude Code on the subscription), built with the team
  caller, so it has only the tools ``access.tool_allowed`` lets a team caller use (and the Claude Code one has no file or web
  tools at all), the team prompt, no memories or earlier conversations, and an in-memory conversation that is never written
  to the owner's transcript or metrics;
* **bus** - a private ``EventBus``. Everything that team brain says, thinks or does is published there, so the owner's console
  never sees it, and the team console's live connection reads only this bus (plus the global "reload" signal) - never the
  owner's approvals, notifications, proactive posts, display panels or finance events.

Sessions are in memory only: a restart or a settings reload starts fresh conversations (the team console reconnects by
itself). Idle ones are dropped after ``IDLE_HOURS`` and the number is capped, so this can't grow without bound.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .. import access
from ..events import EventBus

log = logging.getLogger(__name__)

IDLE_HOURS = 8
MAX_SESSIONS = 12  # (on the Claude Code backend each one is a Claude Code process, so keep it modest)


@dataclass
class TeamSession:
    caller: access.Caller
    bus: EventBus
    brain: object
    last_used: float = field(default_factory=time.monotonic)


class TeamSessions:
    def __init__(self, j):
        self.j = j
        self._sessions: dict[str, TeamSession] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def _build(self, caller: access.Caller) -> TeamSession:
        from ..brain.trace import TurnTrace

        j = self.j
        bus = EventBus()
        if j.settings.effective_llm_backend == "max":
            from ..brain.max_backend import MaxBrain

            brain = MaxBrain(j, caller=caller, bus=bus)
        else:
            from ..brain.agent import JarvisBrain

            brain = JarvisBrain(j, caller=caller, bus=bus)
        # the source line / pop-up button under a reply, limited to the pop-ups a team console has
        brain.trace = TurnTrace(j, panels=access_panels())
        bus.add_tap(brain.trace.on_event)
        return TeamSession(caller, bus, brain)

    def get(self, caller: access.Caller) -> TeamSession:
        """The session for this team caller, created on first use. ``caller.sid`` identifies it."""
        if not caller.is_team or not caller.sid:
            raise ValueError("Only a team session has a team brain")
        self._sweep()
        s = self._sessions.get(caller.sid)
        if s is None:
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda k: self._sessions[k].last_used)
                self._drop(oldest)
            s = self._sessions[caller.sid] = self._build(caller)
        s.last_used = time.monotonic()
        return s

    def _sweep(self) -> None:
        cutoff = time.monotonic() - IDLE_HOURS * 3600
        for sid in [k for k, v in self._sessions.items() if v.last_used < cutoff]:
            self._drop(sid)

    def _drop(self, sid: str) -> None:
        s = self._sessions.pop(sid, None)
        if s is not None and hasattr(s.brain, "close"):
            try:
                asyncio.get_running_loop().create_task(s.brain.close())
            except RuntimeError:
                pass

    async def close(self) -> None:
        for sid in list(self._sessions):
            s = self._sessions.pop(sid)
            if hasattr(s.brain, "close"):
                try:
                    await s.brain.close()
                except Exception:  # noqa: BLE001
                    log.exception("Closing a team session's brain failed")


def access_panels() -> frozenset[str]:
    """The rail pop-ups a team console has (the ones a reply may point at): not approvals, comms, issues, health or finance."""
    return frozenset({"ops", "fleet", "presence", "upcoming"})
