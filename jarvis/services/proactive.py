"""Jarvis speaking up on his own: messages into the open chat (and read aloud in voice mode) that nobody asked for.

Three things use it, all through ``Proactive``:
- ``start`` / ``poller`` / ``watch_ci`` / ``watch_action``: background jobs that keep going after the reply has been
  sent (waiting on CI, polling an approval's result, a long rewrite) and post a follow-up when they finish or change.
- ``announce``: findings from automations and the pull request watch (``pr_watch``). Posted only when they differ from
  what was said last time, and also sent to Teams.
- ``post``: a single message.

It can only TELL the owner something. Nothing here approves, sends, merges or changes anything, and the text it posts
is never fed back to a model as an instruction. Every message goes through the same redaction as the stored
conversation (``history.redact_history``: credentials and access codes), and is held back - kept as a quiet entry in
the notifications list instead - when proactive chat is switched off, it is quiet hours, the hourly limit has been
reached, or the owner is in the middle of a conversation. The HUD adds its own checks (session mute, not while it is
listening or speaking) before anything is shown or spoken - see ``case "proactive"`` in web/hud.js.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from ..history import redact_history
from ..integrations.redact import truncate

log = logging.getLogger(__name__)

MAX_CHARS = 1200          # longest message posted into the chat
USER_QUIET_S = 45         # don't speak up this soon after the owner's last message
TURN_STALE_S = 300        # a turn with no reply after this long is treated as abandoned, not still running
WAIT_BUSY_S = 120         # how long a message waits for the owner to finish talking before it is held back
BUSY_POLL_S = 5
MAX_BACKGROUND = 5        # background jobs at once
NOTHING = "NOTHING_TO_REPORT"  # what an automation starts its reply with when there is nothing worth saying
OFF_MESSAGE = ("Proactive messages are switched off, so I couldn't promise to follow up in the chat. "
               "They can be switched on under Settings > Jarvis speaking up.")
SEEN_KEY = "proactive:seen:"
PR_SNAPSHOT_KEY = "proactive:pr_watch"
PR_WATCH_KEY = "pr_watch"
PR_WATCH_NAME = "Pull request watch"


def _minutes(value: str, default: int) -> int:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return default
    return int(m.group(1)) * 60 + int(m.group(2))


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    """Is ``now`` inside the quiet window [start, end)? The window may run past midnight (21:00 to 07:30).
    Unreadable times fall back to the defaults; identical times mean no quiet hours."""
    a, b = _minutes(start, 21 * 60), _minutes(end, 7 * 60 + 30)
    if a == b:
        return False
    m = now.hour * 60 + now.minute
    return a <= m < b if a < b else (m >= a or m < b)


def _fingerprint(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()[:16]


def _line(text: Any, limit: int = 80) -> str:
    """One short plain line of someone else's text (a PR title): no newlines, no markdown that would change layout."""
    return " ".join(str(text or "").split()).replace("`", "'").replace("*", "")[:limit]


class Proactive:
    def __init__(self, j):
        self.j = j
        self._sent: deque[float] = deque()  # when each recent message went out (monotonic), for the hourly cap
        self._tasks: dict[int, asyncio.Task] = {}
        self._names: dict[int, str] = {}
        self._next_id = 1

    # ------------------------------------------------------------------ state
    @property
    def enabled(self) -> bool:
        return bool(self.j.settings.proactive_chat_enabled)

    def _local_now(self) -> datetime:
        return datetime.now(ZoneInfo(self.j.settings.timezone))

    def quiet_now(self) -> bool:
        s = self.j.settings
        return in_quiet_hours(self._local_now(), s.proactive_quiet_start, s.proactive_quiet_end)

    def user_busy(self) -> bool:
        """The owner is talking to Jarvis right now: he sent something moments ago, or a turn is still in flight."""
        seen, now = self.j.bus.last_event, time.monotonic()
        said = seen.get("user_message")
        if said is not None and now - said < USER_QUIET_S:
            return True
        thinking = seen.get("thinking")
        if thinking is not None and now - thinking < TURN_STALE_S:
            ended = max((seen.get(k, float("-inf")) for k in ("reply", "error", "stopped")))
            return ended < thinking
        return False

    def _rate_limited(self) -> bool:
        now = time.monotonic()
        while self._sent and now - self._sent[0] >= 3600:
            self._sent.popleft()
        cap = self.j.settings.proactive_max_per_hour
        return cap > 0 and len(self._sent) >= cap

    def held_reason(self) -> str:
        """Why a message can't go out right now ("" when it can), not counting the owner being mid-conversation."""
        if not self.enabled:
            return "proactive messages are off"
        if self.quiet_now():
            return "quiet hours"
        if self._rate_limited():
            return "hourly limit reached"
        return ""

    async def _clear_to_speak(self) -> str:
        """Wait (a little) for the owner to finish talking, then say whether the message may go: "" or the reason not."""
        waited = 0
        while True:
            reason = self.held_reason()
            if reason or not self.user_busy() or waited >= WAIT_BUSY_S:
                break
            await asyncio.sleep(BUSY_POLL_S)
            waited += BUSY_POLL_S
        return reason or ("you're in the middle of a conversation" if self.user_busy() else "")

    @staticmethod
    def _clean(text: str | None) -> str:
        return truncate(redact_history(text).strip(), MAX_CHARS)[0]

    def _keep(self, source: str, text: str, reason: str) -> None:
        """A message that couldn't go out is not lost: it is left as a quiet entry in the notifications list."""
        try:
            self.j.db.add_notification("info", f"Held back ({reason}): {source or 'Jarvis'}", text)
        except Exception:  # noqa: BLE001
            log.exception("Could not keep a held-back proactive message")

    def _emit(self, text: str, source: str, speak: bool) -> None:
        self._sent.append(time.monotonic())
        self.j.db.add_transcript("assistant", text)
        self.j.bus.publish("proactive", {"id": uuid.uuid4().hex, "text": text, "source": source, "speak": speak})

    # ------------------------------------------------------------------ one message
    async def post(self, text: str, *, source: str = "", speak: bool = True) -> dict[str, Any]:
        """Say ``text`` in the open chat. Returns {"delivered": bool, "reason": why not}."""
        if not self.enabled:
            return {"delivered": False, "reason": self.held_reason()}
        clean = self._clean(text)
        if not clean:
            return {"delivered": False, "reason": "nothing to say"}
        reason = await self._clear_to_speak()
        if not reason and self.j.bus.subscriber_count == 0:
            reason = "no chat is open"
        if reason:
            self._keep(source, clean, reason)
            return {"delivered": False, "reason": reason}
        self._emit(clean, source, speak)
        return {"delivered": True, "reason": ""}

    # ------------------------------------------------------------------ findings that repeat
    async def announce(self, key: str, title: str, body: str, *, teams: bool = True, speak: bool = True) -> dict[str, Any]:
        """Tell the owner what a recurring check found, but only if it differs from last time: into the open chat and,
        unless ``teams`` is False, Teams. Held back (not remembered, so the next run tries again) when it is quiet
        hours, over the hourly limit or the owner is mid-conversation."""
        if not self.enabled:
            return {"delivered": False, "reason": self.held_reason()}
        clean = self._clean(body)
        stored = self.j.db.get_kv(SEEN_KEY + key)
        if not clean or clean.upper().startswith(NOTHING):
            self.j.db.set_kv(SEEN_KEY + key, "")  # so the same finding turning up again later is news again
            return {"delivered": False, "reason": "nothing to report"}
        fingerprint = _fingerprint(clean)
        if stored == fingerprint:
            return {"delivered": False, "reason": "unchanged"}
        reason = await self._clear_to_speak()
        if reason:
            return {"delivered": False, "reason": reason}
        if self.j.bus.subscriber_count:
            self._emit(f"**{_line(title, 120)}**\n\n{clean}", key, speak)
        else:
            self._sent.append(time.monotonic())
        if teams:
            try:
                await self.j.notifier.send_owner_update(_line(title, 120), clean, channels=("teams",))
            except Exception:  # noqa: BLE001 - Teams being down never breaks a check
                log.exception("Teams delivery of %s failed", key)
        self.j.db.set_kv(SEEN_KEY + key, fingerprint)
        return {"delivered": True, "reason": ""}

    async def tell(self, key: str, title: str, body: str) -> dict[str, Any]:
        """What a scheduled check found, for a check the owner asked for. With "Jarvis speaking up" on this is exactly
        ``announce`` (change-only, quiet hours, hourly limit, Teams). With it off the owner still wanted to hear about
        a change, so it is posted to the open chat as one message - once: the same finding as last time is not news -
        and, when no chat is open, kept as a quiet notification. Never speaks aloud when speaking up is off."""
        if self.enabled:
            return await self.announce(key, title, body)
        clean = self._clean(body)
        if not clean or clean.upper().startswith(NOTHING):
            self.j.db.set_kv(SEEN_KEY + key, "")
            return {"delivered": False, "reason": "nothing to report"}
        fingerprint = _fingerprint(clean)
        if self.j.db.get_kv(SEEN_KEY + key) == fingerprint:
            return {"delivered": False, "reason": "unchanged"}
        text = f"**{_line(title, 120)}**\n\n{clean}"
        if self.j.bus.subscriber_count:
            self._emit(text, key, False)
        else:
            self._keep(title, clean, "no chat is open")
        self.j.db.set_kv(SEEN_KEY + key, fingerprint)
        return {"delivered": True, "reason": ""}

    # ------------------------------------------------------------------ background jobs
    def start(self, name: str, work: Callable[[], Awaitable[str | None]]) -> dict[str, Any]:
        """Run ``work`` in the background and post what it returns when it finishes (or why it failed). The reply that
        started it is sent straight away; this carries on afterwards."""
        if not self.enabled:
            return {"error": OFF_MESSAGE}
        live = [i for i, t in self._tasks.items() if not t.done()]
        if len(live) >= MAX_BACKGROUND:
            return {"error": f"I'm already keeping an eye on {len(live)} things in the background - "
                             "let one finish first."}
        task_id = self._next_id
        self._next_id += 1
        task = asyncio.create_task(self._run(name, work))
        self._tasks[task_id] = task
        self._names[task_id] = name
        task.add_done_callback(lambda t, i=task_id: self._tasks.pop(i, None))
        return {"started": True, "id": task_id,
                "message": f"Started in the background: {name}. I'll post here when there's something to say."}

    async def _run(self, name: str, work: Callable[[], Awaitable[str | None]]) -> None:
        try:
            result = await work()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Background job %r failed", name)
            await self.post(f"{name} didn't finish: {type(e).__name__}: {str(e)[:200]}", source=name)
            return
        if result:
            await self.post(f"{name}: {result}", source=name)

    def poller(self, name: str, check: Callable[[], Awaitable[tuple[bool, str]]], *, interval_s: float = 30,
               timeout_s: float = 3600, max_errors: int = 3) -> Callable[[], Awaitable[str]]:
        """A ``work`` function for ``start`` that calls ``check()`` -> (finished, status) every ``interval_s``. A changed
        status is posted as it happens; the final one is returned. A check that keeps failing (``max_errors`` in a row)
        or never finishing within ``timeout_s`` ends the watch with a plain explanation rather than running forever."""
        async def work() -> str:
            last: str | None = None
            waited, errors = 0.0, 0
            while True:
                status: str | None = last
                try:
                    done, status = await check()
                    errors = 0
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    log.warning("Check for %r failed (%d/%d): %s", name, errors, max_errors, e)
                    if errors >= max_errors:
                        return f"I couldn't keep checking ({type(e).__name__}), so I've stopped watching."
                    done = False
                if done:
                    return status or "finished."
                if last is not None and status and status != last:
                    await self.post(f"{name}: {status}", source=name)
                if status:
                    last = status
                if waited >= timeout_s:
                    return (f"still not finished after {int(timeout_s // 60)} minutes, so I've stopped watching. "
                            f"Last status: {last or 'unknown'}")
                await asyncio.sleep(interval_s)
                waited += interval_s
        return work

    def watch_ci(self, branch: str, interval_s: float = 60, timeout_s: float = 3600) -> dict[str, Any]:
        """Follow the GitHub Actions result on a branch of Jarvis's own repository until it passes or fails. Read-only."""
        from ..brain.pr_tools import NOT_CONNECTED, _client
        from ..integrations.github_pr import PRError, check_ref

        if not self.enabled:
            return {"error": OFF_MESSAGE}
        pc = _client(self.j)
        if pc is None:
            return {"error": NOT_CONNECTED}
        try:
            branch = check_ref(branch)
        except PRError as e:
            return {"error": str(e)}

        async def check() -> tuple[bool, str]:
            r = await pc.ci_results(branch)
            failed = ", ".join(r["failed"][:5])
            if r["state"] == "success":
                return True, f"CI passed on {branch}."
            if r["state"] == "failure":
                return True, f"CI failed on {branch}" + (f" ({failed})." if failed else ".")
            return False, (f"CI is still running on {branch}" if r["state"] == "pending"
                           else f"waiting for CI to start on {branch}") + (f"; already failing: {failed}" if failed else "")

        return self.start(f"Watching CI on {branch}",
                          self.poller(f"CI on {branch}", check, interval_s=interval_s, timeout_s=timeout_s))

    def watch_action(self, action_id: int, interval_s: float = 20, timeout_s: float = 6 * 3600) -> dict[str, Any]:
        """Follow a queued action until it has been decided and run, then report how it went. It only READS the action's
        status: approving, denying or running it stays with the owner's click on the display."""
        if not self.enabled:
            return {"error": OFF_MESSAGE}
        db = self.j.db
        if not db.get_action(action_id):
            return {"error": f"There is no action #{action_id}."}

        async def check() -> tuple[bool, str]:
            a = db.get_action(action_id) or {}
            status, result = a.get("status", "gone"), (a.get("result") or "")[:300]
            if status == "done":
                return True, f"action #{action_id} was approved and done. {result}".strip()
            if status == "failed":
                return True, f"action #{action_id} was approved but failed. {result}".strip()
            if status == "denied":
                return True, f"action #{action_id} was cancelled." + (f" {result}" if result else "")
            if status == "gone":
                return True, f"action #{action_id} no longer exists."
            return False, f"action #{action_id} is still " + ("running" if status == "approved" else "waiting for approval")

        return self.start(f"Action #{action_id}", self.poller(f"Action #{action_id}", check, interval_s=interval_s,
                                                              timeout_s=timeout_s))

    def running(self) -> list[str]:
        return [self._names[i] for i, t in self._tasks.items() if not t.done()]

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)  # each was cancelled, or already failed and logged

    # ------------------------------------------------------------------ the pull request watch
    @staticmethod
    def _pr_state(p: dict[str, Any]) -> dict[str, Any]:
        return {"title": _line(p.get("title")), "sha": str(p.get("head_sha") or "")[:10], "ci": p.get("ci"),
                "failed": list(p.get("ci_failed") or [])[:5], "merge": p.get("merge_status"), "draft": bool(p.get("draft"))}

    @staticmethod
    def _pr_changes(before: dict[str, dict], after: dict[str, dict]) -> list[str]:
        out: list[str] = []
        for num, new in after.items():
            old = before.get(num)
            label = f"PR #{num} ({new['title']})"
            if old is None:
                out.append(f"New pull request: {label}.")
                continue
            if new["ci"] != old["ci"] and new["ci"] == "success":
                out.append(f"CI passed on {label}.")
            elif new["ci"] != old["ci"] and new["ci"] == "failure":
                out.append(f"CI failed on {label}" + (f": {', '.join(new['failed'])}." if new["failed"] else "."))
            if new["merge"] != old["merge"] and new["merge"] == "conflicts":
                out.append(f"{label} now has merge conflicts.")
            elif new["merge"] != old["merge"] and new["merge"] == "mergeable" and old["merge"] == "conflicts":
                out.append(f"{label} no longer has merge conflicts.")
        for num, old in before.items():
            if num not in after:
                out.append(f"PR #{num} ({old['title']}) is no longer open.")
        return out

    async def pr_watch(self) -> dict[str, Any]:
        """Look at the open pull requests on Jarvis's own repository (read-only) and announce what changed since the last
        look: new, CI passed or failed, conflicts, closed. The first look only records where things stand."""
        from ..integrations.github_pr import PRClient

        if not self.enabled or getattr(self.j, "self_github", None) is None:
            return {"skipped": True}
        activity = self.j.activity
        try:
            data = await PRClient(self.j.self_github).list_open_prs(30)
        except Exception as e:  # noqa: BLE001
            log.warning("Pull request watch couldn't read GitHub: %s", e)
            activity.record(PR_WATCH_KEY, PR_WATCH_NAME, "failed", f"Couldn't read GitHub ({type(e).__name__}).")
            return {"error": type(e).__name__}
        after = {str(p["number"]): self._pr_state(p) for p in data["pull_requests"]}
        try:
            before = json.loads(self.j.db.get_kv(PR_SNAPSHOT_KEY) or "null")
        except ValueError:
            before = None
        if not isinstance(before, dict):
            self.j.db.set_kv(PR_SNAPSHOT_KEY, json.dumps(after))
            activity.record(PR_WATCH_KEY, PR_WATCH_NAME, "baseline", f"First look: {len(after)} open.")
            return {"baseline": len(after)}
        changes = self._pr_changes(before, after)
        if not changes:
            self.j.db.set_kv(PR_SNAPSHOT_KEY, json.dumps(after))
            activity.record(PR_WATCH_KEY, PR_WATCH_NAME, "no_change", "No change.")  # logged, never posted
            return {"changed": False}
        result = await self.announce("pr_watch", "Pull requests", "\n".join(f"- {c}" for c in changes) +
                                     "\n\n(Titles come from GitHub. Nothing has been merged or changed.)")
        if result["delivered"] or result["reason"] == "unchanged":
            self.j.db.set_kv(PR_SNAPSHOT_KEY, json.dumps(after))
        held = "" if result["delivered"] else f" (held back: {result['reason']})"
        activity.record(PR_WATCH_KEY, PR_WATCH_NAME, "changed", changes[0] + held)
        return {"changed": True, **result}
