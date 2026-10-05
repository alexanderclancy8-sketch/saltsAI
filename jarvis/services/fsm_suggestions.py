"""Proactive suggestions with a Prepare button: Jarvis's half of the Salts FSM Action Centre contract.

Jarvis looks one step ahead (a quote that has gone quiet) and offers to do the groundwork. It shows the offer in its own
Approvals drawer AND publishes it to the FSM's Action Centre, so the office sees it in the inbox they already work from. The
office user (or the owner, in the drawer) presses **Prepare**; Jarvis then DRAFTS the work and queues it in its normal Approvals
inbox. Preparing never approves, sends or runs anything: a human still approves in Jarvis (the console or Teams).

The contract (everything here is Jarvis -> FSM, using the existing FSM key; the FSM never calls Jarvis):

* ``PUT    /api/jarvis/suggestions/{external_id}``  upsert, idempotent. Body: kind, lane ("money"|"operations"|"compliance"),
  title, detail, reason, record_type, record_id, record_label, created_at.
* ``PATCH  /api/jarvis/suggestions/{external_id}``  Jarvis reports back. Body: status ("prepared"|"failed"|"resolved"; plus
  "snoozed" with ``snoozed_until`` for the console's Not now), note, approval_ref.
* ``GET    /api/jarvis/suggestions?status=requested``  the suggestions an office user pressed Prepare on: a list of
  {external_id, requested_by, requested_at, ...}; a row with a future ``snoozed_until`` is left alone.

``external_id`` is ``<kind>:<record id>`` (``quote_followup:Q1180``). Status: open -> requested (a person pressed Prepare in the
FSM) -> prepared (Jarvis queued the draft) -> resolved (the condition cleared, or the approved action completed).

Adding a kind of suggestion is ONE entry in ``KINDS`` below: a detector (what is true right now) and a prepare handler (what to
draft for ONE record). Everything else - the stable ids, the publishing, the polling, the Prepare/Not now buttons, the
idempotency, the rate limit - is generic.

Safety, in one place:

* ``prepare_suggestion`` / ``snooze_suggestion`` are reached only from main.py's owner-authenticated, same-origin console routes
  and from ``poll()`` here. No brain tool, standing approval, Teams message or other scheduled job calls them (a test greps).
* A handler only DRAFTS: it queues through ``ActionExecutor.queue`` (kind ``email_send``). ``email_send`` is not something a
  standing approval can ever match, so a prepared draft always waits for a human; nothing here approves, denies or sends.
* A request from the FSM is only ever a request to PREPARE. Each suggestion is prepared at most once (a kv marker), and FSM-driven
  prepares are rate limited per hour.
* Sample data is never a source (``j.fsm.demo``), and a down or not-yet-ready FSM is tolerated: back off, log once, no UI errors.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from .customer_comms import QUOTE_MAX_AGE_DAYS, _day, _status
from .suggestions import SNOOZE

log = logging.getLogger(__name__)

SUGGESTIONS_PATH = "/api/jarvis/suggestions"  # under the FSM base URL, whatever FSM_API_PREFIX is
LANES = ("money", "operations", "compliance")
QUOTE_CHASE_DAYS = 7  # a sent quote with no response for this long is worth chasing
MAX_NEW_PUSHES_PER_RUN = 20  # a first run must not flood the Action Centre; the rest follow on the next runs
MAX_REQUESTS_PER_POLL = 10
PREPARE_WINDOW_S = 3600.0
PUSHED = "fsmsug:pushed:"      # kv: external_id -> {"digest", "resolved", "at"}  (what the FSM was last told)
PREPARED = "fsmsug:prepared:"  # kv: external_id -> {"action_id", "at", "by"}      (the one-time Prepare marker)
RATE_KEY = "fsmsug:rate"       # kv: epoch seconds of the FSM-requested prepares in the last hour
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")
_NOTE_DRAFT = "Draft ready in Jarvis, waiting for approval."


def _clip(text: Any, n: int) -> str:
    return _CONTROL.sub(" ", str(text or "")).strip()[:n]


# --------------------------------------------------------------------------------------------------- the kind registry
@dataclass(frozen=True)
class Candidate:
    """One thing that is true right now and worth offering to do something about."""
    kind: str
    record_id: str
    title: str
    detail: str
    reason: str
    record_label: str
    prompt: str = ""
    priority: int = 2

    @property
    def external_id(self) -> str:
        return external_id(self.kind, self.record_id)


@dataclass(frozen=True)
class Prepared:
    """What a prepare handler reports. ``ok`` = a draft is queued for approval (``approval_id``); otherwise ``note`` says why not."""
    ok: bool
    note: str
    approval_id: int | None = None


@dataclass(frozen=True)
class Kind:
    name: str          # the prefix of the external id; no colon
    lane: str          # money | operations | compliance
    record_type: str   # quote | job | ppm | ...
    detect: Callable[[Any, date], Awaitable[list[Candidate]]]   # (j, today) -> what is true now
    prepare: Callable[[Any, str], Awaitable[Prepared]]          # (j, record_id) -> draft ONE record, queue for approval


def external_id(kind: str, record_id: str) -> str:
    return f"{kind}:{record_id}"


def split_external_id(ext: str) -> tuple[str, str]:
    kind, _, record_id = str(ext).partition(":")
    return kind, record_id


def approval_phase(j, action_id: int | None) -> str:
    """Where a queued draft is: 'waiting' (pending, running, or failed and not yet dealt with) or 'finished' (done, denied, or gone).
    An edited or retried draft is a NEW pending action; follow the link so editing the wording never reads as 'dealt with'."""
    seen = 0
    row = j.db.get_action(int(action_id)) if action_id else None
    while row is not None and seen < 8:
        seen += 1
        nxt = row.get("superseded_by")
        if nxt:
            row = j.db.get_action(int(nxt))
            continue
        if row["status"] in ("pending", "approved"):
            return "waiting"
        if row["status"] == "failed" and not row.get("dismissed_at"):
            return "waiting"
        return "finished"
    return "finished"


def _action_id_of(marker: str | None) -> int | None:
    m = re.fullmatch(r"queued:(\d+)", marker or "")
    return int(m.group(1)) if m else None


# ---- kind: quote_followup - a sent quote with no response for QUOTE_CHASE_DAYS+ days -> draft the customer chase email
async def _detect_quote_followup(j, today: date) -> list[Candidate]:
    out: list[tuple[float, Candidate]] = []
    for q in await j.fsm.quotes("sent"):
        if _status(q) != "sent" or not q.get("id"):
            continue
        sent = _day(q.get("sent_date"))
        if not sent or not (QUOTE_CHASE_DAYS <= (today - sent).days <= QUOTE_MAX_AGE_DAYS):
            continue
        qid = str(q["id"])
        marker = j.db.get_kv(f"comms:quote_followup:{qid}")  # a chase was already drafted (by Prepare or by the scheduled sweep)
        if marker:
            ours = j.db.get_kv(PREPARED + external_id("quote_followup", qid))
            if not ours or approval_phase(j, _action_id_of(marker)) != "waiting":
                continue  # drafted elsewhere, or approved/declined already: nothing left to offer
        age = (today - sent).days
        try:
            value = float(q.get("value") or 0)
        except (TypeError, ValueError):
            value = 0.0
        customer = _clip(q.get("customer"), 80) or "the customer"
        what = _clip(q.get("title"), 80)
        money = f" (£{value:,.0f} + VAT)" if value else ""
        out.append((value, Candidate(
            "quote_followup", qid,
            f"Chase quote {qid} for {customer}{money}?",
            f"{what + '; ' if what else ''}sent {sent:%d %b}, no reply yet.",
            f"Sent {age} days ago with no response.",
            f"{qid} - {customer}",
            prompt=f"Draft a chase email for quote {qid} ({customer}) for my approval.")))
    out.sort(key=lambda t: -t[0])  # the biggest quote first
    return [c for _, c in out]


async def _prepare_quote_followup(j, record_id: str) -> Prepared:
    res = await j.customer_comms.draft_quote_followup(record_id)
    prior = res.get("already")
    if prior:
        aid = _action_id_of(prior)
        if aid and approval_phase(j, aid) == "waiting":
            return Prepared(True, _NOTE_DRAFT, aid)
        return Prepared(False, "A chase email for this quote was already drafted in Jarvis and dealt with.")
    if res["queued"]:
        return Prepared(True, _NOTE_DRAFT, int(res["queued"][0]["action_id"]))
    reason = (res["skipped"][0]["reason"] if res["skipped"] else "")
    if "email" in reason:
        return Prepared(False, "Couldn't draft the chase: Salts FSM has no email address for this customer.")
    return Prepared(False, "That quote is no longer waiting for a reply, so there is nothing to chase.")


KINDS: dict[str, Kind] = {k.name: k for k in (
    Kind("quote_followup", "money", "quote", _detect_quote_followup, _prepare_quote_followup),
    # A later kind is one more line here, e.g. Kind("service_due", "operations", "ppm", detect, prepare)
    # or Kind("report_not_sent", "compliance", "job", detect, prepare).
)}


# ------------------------------------------------------------------------------------------------------------- service
class FsmSuggestions:
    def __init__(self, j):
        self.j = j
        self._run_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._backoff_until = 0.0   # time.monotonic()
        self._backoff_s = 0.0
        self._down = False          # already logged for this outage
        self._unreported: dict[str, dict[str, Any]] = {}  # external_id -> a PATCH body the FSM has not accepted yet
        self._last_full_run = 0.0
        self._rate_warned = False

    # ---- switches
    def active(self) -> bool:
        """True when Jarvis may talk to the FSM about suggestions: the owner's switch is on and a real FSM is connected."""
        s = self.j.settings
        return bool(s.suggestions_publish_to_fsm and s.fsm_configured and not getattr(self.j.fsm, "demo", True))

    def _working_hours(self, now: datetime | None = None) -> bool:
        s = self.j.settings
        try:
            now = (now or datetime.now(ZoneInfo(s.timezone))).astimezone(ZoneInfo(s.timezone))
        except Exception:  # noqa: BLE001 - an odd timezone name must not stop the job
            now = now or datetime.now()
        return now.weekday() < 5 and s.suggestions_fsm_hours_start <= now.hour < s.suggestions_fsm_hours_end

    # ---- talking to the FSM (never raises; a refusal or an outage backs off)
    async def _call(self, method: str, ext: str | None, body: dict[str, Any] | None = None,
                    params: dict[str, Any] | None = None) -> httpx.Response | None:
        if time.monotonic() < self._backoff_until:
            return None
        path = SUGGESTIONS_PATH + (f"/{quote(ext, safe=':')}" if ext else "")
        try:
            r = await self.j.fsm.jarvis_call(method, path, body, params)
        except (httpx.HTTPError, OSError) as e:
            self._back_off(f"the FSM could not be reached ({type(e).__name__})", first_s=60, cap_s=900)
            return None
        if r.status_code in (404, 405) and (not ext or method == "PUT"):  # no such route: the FSM has not shipped the endpoints yet
            self._back_off("the FSM has no /api/jarvis/suggestions endpoint yet (404)", first_s=300, cap_s=3600)
            return None
        if r.status_code in (401, 403) or r.status_code >= 500:
            self._back_off(f"the FSM answered {r.status_code}", first_s=60, cap_s=900)
            return None
        if self._down:
            log.info("Salts FSM suggestions are reachable again")
        self._down, self._backoff_s = False, 0.0
        return r

    def _back_off(self, why: str, first_s: float, cap_s: float) -> None:
        self._backoff_s = min(max(first_s, self._backoff_s * 2), cap_s)
        self._backoff_until = time.monotonic() + self._backoff_s
        if not self._down:  # said once per outage, not every minute
            log.warning("Suggestions are not being sent to Salts FSM: %s. Trying again in about %d minutes.",
                        why, max(1, round(self._backoff_s / 60)))
        self._down = True

    # ---- the detect + publish run (scheduled)
    async def _detect(self, today: date) -> tuple[dict[str, Candidate], set[str]]:
        found: dict[str, Candidate] = {}
        ok: set[str] = set()
        for name, kind in KINDS.items():
            try:
                for c in await kind.detect(self.j, today):
                    found[c.external_id] = c
                ok.add(name)
            except Exception as e:  # noqa: BLE001 - one broken source mustn't stop the others, nor resolve its suggestions
                log.info("suggestion kind %s could not be checked: %s", name, e)
        return found, ok

    def _body(self, c: Candidate, created_at: str) -> dict[str, Any]:
        kind = KINDS[c.kind]
        return {"kind": c.kind, "lane": kind.lane, "title": _clip(c.title, 200), "detail": _clip(c.detail, 500),
                "reason": _clip(c.reason, 300), "record_type": kind.record_type, "record_id": c.record_id,
                "record_label": _clip(c.record_label, 200), "created_at": created_at}

    async def sync(self, today: date | None = None, force_hours: bool | None = None) -> int:
        """Keep the stored suggestions true, and the FSM's copy in step. Returns how many changes were made (new, changed, resolved).

        Local: a suggestion appears in the Approvals drawer while it is true and goes when it stops being true. Publishing (only
        when the owner's switch is on and a real FSM is connected): each new or changed one is PUT, each one that stopped being true
        is PATCHed 'resolved'. Outside working hours nothing NEW is pushed - the next working-hours run does it."""
        j = self.j
        if getattr(j.fsm, "demo", True):  # sample data is never a source of suggestions
            return 0
        async with self._run_lock:
            today = today or date.today()
            found, ok_kinds = await self._detect(today)
            if not ok_kinds:
                return 0
            changes = self._update_local(found, ok_kinds)
            if self.active():
                changes += await self._publish(found, ok_kinds, working=self._working_hours() if force_hours is None else force_hours)
            self._last_full_run = time.monotonic()
            return changes

    def _update_local(self, found: dict[str, Candidate], ok_kinds: set[str]) -> int:
        db = self.j.db
        changes = 0
        rows = {r["key"]: r for r in db.kind_suggestions()}
        now = datetime.now().astimezone()
        for ext, c in found.items():
            row = rows.get(ext)
            reopened = False
            if row is not None:
                if row["status"] == "prepared":
                    continue  # the draft is waiting in the Approvals inbox; nothing more to offer
                if row["status"] in ("dismissed", "done"):
                    if now - datetime.fromisoformat(row["updated_at"]) < SNOOZE:
                        continue  # Not now: quiet until tomorrow
                if row["status"] != "open":
                    db.reopen_suggestion(ext)  # snooze over, or it came back after being resolved
                    reopened = True
            meta = json.dumps({"record_label": c.record_label, "reason": c.reason})
            is_new = db.upsert_suggestion(ext, c.title, c.detail, c.prompt, c.priority, c.kind, meta)
            if is_new or reopened or (row is not None and (row["title"], row["detail"]) != (c.title, c.detail)):
                changes += 1
        for ext, row in rows.items():
            if row["kind"] in ok_kinds and ext not in found and row["status"] != "resolved":
                db.set_suggestion_status(ext, "resolved")  # no longer true (answered, drafted and dealt with, withdrawn...)
                changes += 1
        if changes:
            self.j.bus.publish("suggestions", db.open_suggestions())
        return changes

    def _pushed(self) -> dict[str, dict[str, Any]]:
        out = {}
        for r in self.j.db.query("SELECT key, value FROM kv WHERE key LIKE ?", (PUSHED + "%",)):
            try:
                out[r["key"][len(PUSHED):]] = json.loads(r["value"])
            except ValueError:
                continue
        return out

    async def _publish(self, found: dict[str, Candidate], ok_kinds: set[str], working: bool) -> int:
        db = self.j.db
        pushed = self._pushed()
        rows = {r["key"]: r for r in db.kind_suggestions()}
        changes = new_pushes = 0
        for ext, c in found.items():
            row = rows.get(ext)
            if row is None or row["status"] in ("dismissed", "done", "prepared"):
                continue  # snoozed here, or already prepared: the FSM has its own state for it - don't disturb it
            body = self._body(c, row["created_at"])
            digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            state = pushed.get(ext)
            if state and not state.get("resolved") and state.get("digest") == digest:
                continue
            if not state or state.get("resolved"):
                if not working or new_pushes >= MAX_NEW_PUSHES_PER_RUN:
                    continue  # quiet outside working hours; a flood is spread over several runs
            r = await self._call("PUT", ext, body)
            if r is None:
                return changes  # the FSM is away: stop, try again at the next run
            if not 200 <= r.status_code < 300:
                log.warning("Salts FSM did not accept suggestion %s (HTTP %s)", ext, r.status_code)
                continue
            if not state or state.get("resolved"):
                new_pushes += 1
            self._set_pushed(ext, digest, resolved=False)
            changes += 1
        for ext, state in pushed.items():
            kind = split_external_id(ext)[0]
            if state.get("resolved") or ext in found or kind not in ok_kinds:
                continue
            changes += await self._tell_resolved(ext)
        return changes

    def _set_pushed(self, ext: str, digest: str, resolved: bool) -> None:
        self.j.db.set_kv(PUSHED + ext, json.dumps({"digest": digest, "resolved": resolved,
                                                   "at": datetime.now().astimezone().isoformat(timespec="seconds")}))

    async def _tell_resolved(self, ext: str) -> int:
        r = await self._call("PATCH", ext, {"status": "resolved", "note": "No longer needed."})
        if r is None:
            return 0
        if 200 <= r.status_code < 300 or r.status_code == 404:  # (404: the FSM does not know it - nothing to resolve)
            state = self._pushed().get(ext, {})
            self._set_pushed(ext, state.get("digest", ""), resolved=True)
            return 1
        log.warning("Salts FSM did not accept resolving %s (HTTP %s)", ext, r.status_code)
        return 0

    async def scheduled_sync(self) -> int:
        """The scheduler's entry point (every suggestions_fsm_interval_min): every run in working hours, hourly outside them."""
        if not self._working_hours() and self._last_full_run and time.monotonic() - self._last_full_run < 3300:
            return 0
        return await self.sync()

    # ---- Prepare (the one handler path: the FSM request, and the console's button)
    def _lock_for(self, ext: str) -> asyncio.Lock:
        return self._locks.setdefault(ext, asyncio.Lock())

    def prepared_marker(self, ext: str) -> dict[str, Any] | None:
        raw = self.j.db.get_kv(PREPARED + ext)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    async def prepare_suggestion(self, ext: str, by: str = "", report: bool = False) -> dict[str, Any]:
        """Draft the work for ONE suggestion and queue it for approval. Idempotent: a suggestion is prepared once.

        Returns {"status": "prepared"|"failed"|"resolved", "note", "approval_id"}. Only DRAFTS: nothing is approved, sent or run here.
        ``report`` (the console's button) also tells the FSM what happened, if the FSM has this suggestion; the poller reports for itself."""
        result = await self._prepare_once(ext, by)
        if report and self.active() and self._pushed().get(_clip(ext, 200)):
            await self._report(_clip(ext, 200), result)
        return result

    async def _prepare_once(self, ext: str, by: str) -> dict[str, Any]:
        j = self.j
        ext = _clip(ext, 200)
        kind_name, record_id = split_external_id(ext)
        kind = KINDS.get(kind_name)
        if kind is None or not record_id:
            return {"status": "failed", "note": "Jarvis can't prepare that kind of suggestion yet.", "approval_id": None}
        if getattr(j.fsm, "demo", True):  # sample data is never something Jarvis drafts from
            return {"status": "failed", "note": "Salts FSM isn't connected to Jarvis yet, so there is nothing real to prepare from.",
                    "approval_id": None}
        async with self._lock_for(ext):
            marker = self.prepared_marker(ext)
            if marker:
                if approval_phase(j, marker.get("action_id")) == "waiting":
                    return {"status": "prepared", "note": _NOTE_DRAFT, "approval_id": marker.get("action_id")}
                self._mark_local(ext, "resolved")
                return {"status": "resolved", "note": "That draft has already been approved or declined in Jarvis.",
                        "approval_id": marker.get("action_id")}
            try:
                result = await kind.prepare(j, record_id)
            except Exception as e:  # noqa: BLE001 - a handler failure is reported plainly, never raised into the poller or the UI
                log.warning("Preparing %s failed: %s: %s", ext, type(e).__name__, e)
                return {"status": "failed", "note": "Jarvis hit a problem preparing this. Try again in a minute, or ask Jarvis.",
                        "approval_id": None}
            if not result.ok:
                return {"status": "failed", "note": _clip(result.note, 300), "approval_id": None}
            j.db.set_kv(PREPARED + ext, json.dumps({"action_id": result.approval_id, "by": _clip(by, 80),
                                                    "at": datetime.now().astimezone().isoformat(timespec="seconds")}))
            self._mark_local(ext, "prepared")
            log.info("Prepared %s (approval #%s)%s", ext, result.approval_id, f" at the request of {_clip(by, 80)}" if by else "")
            return {"status": "prepared", "note": _clip(result.note, 300), "approval_id": result.approval_id}

    def _mark_local(self, ext: str, status: str) -> None:
        if self.j.db.get_suggestion(ext):
            self.j.db.set_suggestion_status(ext, status)
            self.j.bus.publish("suggestions", self.j.db.open_suggestions())

    async def snooze_suggestion(self, ext: str) -> dict[str, Any] | None:
        """Not now: quiet here until tomorrow, and the FSM's copy is snoozed for the same time. Returns the row, or None if unknown."""
        j = self.j
        row = j.db.get_suggestion(ext)
        if not row or not row.get("kind"):
            return None
        j.db.set_suggestion_status(ext, "dismissed")
        j.bus.publish("suggestions", j.db.open_suggestions())
        if self.active() and (self._pushed().get(ext) or {}).get("digest") is not None:
            until = (datetime.now().astimezone() + SNOOZE).isoformat(timespec="seconds")
            await self._call("PATCH", ext, {"status": "snoozed", "snoozed_until": until, "note": "Not now, from Jarvis."})
        return row

    # ---- the poller (scheduled every suggestions_fsm_poll_s)
    def _allow_prepare(self) -> bool:
        raw = self.j.db.get_kv(RATE_KEY)
        now = time.time()
        try:
            stamps = [t for t in json.loads(raw or "[]") if now - float(t) < PREPARE_WINDOW_S]
        except ValueError:
            stamps = []
        limit = max(1, int(self.j.settings.suggestions_prepare_max_per_hour))
        if len(stamps) >= limit:
            if not self._rate_warned:
                log.warning("Suggestion prepares are limited to %d an hour; the rest wait for the next hour.", limit)
                self._rate_warned = True
            return False
        self._rate_warned = False
        stamps.append(now)
        self.j.db.set_kv(RATE_KEY, json.dumps(stamps))
        return True

    @staticmethod
    def _future(value: Any) -> bool:
        try:
            when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return False
        if when.tzinfo is None:
            when = when.astimezone()
        return when > datetime.now().astimezone()

    async def _report(self, ext: str, result: dict[str, Any]) -> bool:
        body: dict[str, Any] = {"status": result["status"], "note": result["note"]}
        if result.get("approval_id") is not None:
            body["approval_ref"] = str(result["approval_id"])
        r = await self._call("PATCH", ext, body)
        if r is not None and (200 <= r.status_code < 300 or r.status_code == 404):
            self._unreported.pop(ext, None)
            return True
        self._unreported[ext] = result  # say it again next time, without doing the work twice
        return False

    async def poll(self) -> int:
        """Ask the FSM which suggestions were Prepare-d, prepare each (once) and report back. Returns how many were handled."""
        if not self.active():
            return 0
        r = await self._call("GET", None, params={"status": "requested"})
        if r is None or not 200 <= r.status_code < 300:
            return 0
        try:
            payload = r.json()
        except ValueError:
            return 0
        rows = payload if isinstance(payload, list) else next(
            (payload[k] for k in ("items", "data", "results", "suggestions") if isinstance(payload, dict)
             and isinstance(payload.get(k), list)), [])
        handled = 0
        for row in [x for x in rows if isinstance(x, dict)][:MAX_REQUESTS_PER_POLL]:
            ext = _clip(row.get("external_id"), 200)
            if not ext or self._future(row.get("snoozed_until")):
                continue
            pending = self._unreported.get(ext)
            if pending is None:
                known = self.prepared_marker(ext)
                if not known and not self._allow_prepare():
                    break  # over the hourly limit: leave the request for later
                pending = await self.prepare_suggestion(ext, by=_clip(row.get("requested_by"), 80))
            await self._report(ext, pending)
            handled += 1
        return handled
