"""A small on-call roster: who is on call, and from when to when.

Only used by the van-location setting "On-call only" (see services/tracking.py): outside working hours, just the
engineers on call right now have their van shown. The roster is Jarvis's own record, kept in the local database
(kv key `oncall_roster`) - it never changes anything in Salts FSM. Times are UK local time, like the rest of Jarvis.
Entries are added and removed through the approval-gated `oncall_add` / `oncall_remove` tools.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

KEY = "oncall_roster"
MAX_SPAN = timedelta(days=31)  # a rota is a week or two at a time; a month-plus entry is almost certainly a typo
KEEP_PAST = timedelta(days=30)  # finished entries are kept this long for reference, then dropped


def parse_when(value: str) -> datetime:
    """'2026-10-02 17:30' or '2026-10-02T17:30' (UK local time). A bare date means the start of that day."""
    text = str(value or "").strip().replace("T", " ")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"'{value}' isn't a date and time - use YYYY-MM-DD HH:MM, e.g. 2026-10-02 17:30") from None
    return when.replace(tzinfo=None)


def name_matches(rostered: Any, engineer: Any) -> bool:
    """Same person? Whole-word match either way round, so 'Ian' matches 'Ian Frost' but not 'Christian Smith'."""
    a = set(re.findall(r"[a-z0-9']+", str(rostered or "").lower()))
    b = set(re.findall(r"[a-z0-9']+", str(engineer or "").lower()))
    return bool(a) and bool(b) and (a <= b or b <= a)


class OnCallRoster:
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------------ storage
    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.db.get_kv(KEY) or "[]")
        except ValueError:
            return []
        return [e for e in data if isinstance(e, dict) and e.get("engineer") and e.get("start") and e.get("end")] \
            if isinstance(data, list) else []

    def _save(self, entries: list[dict[str, Any]]) -> None:
        self.db.set_kv(KEY, json.dumps(entries))

    # ------------------------------------------------------------------ read
    def entries(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Everything on the roster that hasn't long finished, soonest first."""
        now = now or datetime.now()
        out = []
        for e in self._load():
            try:
                end = datetime.fromisoformat(e["end"])
            except ValueError:
                continue
            if end >= now - KEEP_PAST:
                out.append(e)
        return sorted(out, key=lambda e: (e["start"], str(e["engineer"]).lower()))

    def on_call(self, now: datetime | None = None) -> list[str]:
        """Names of the engineers on call at `now` (start inclusive, end exclusive)."""
        now = now or datetime.now()
        names: list[str] = []
        for e in self._load():
            try:
                start, end = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
            except ValueError:
                continue
            if start <= now < end and e["engineer"] not in names:
                names.append(e["engineer"])
        return names

    # ------------------------------------------------------------------ write
    def add(self, engineer: str, start: datetime, end: datetime, added_by: str = "") -> dict[str, Any]:
        engineer = " ".join(str(engineer or "").split())
        if not engineer or len(engineer) > 80:
            raise ValueError("Give the engineer's name.")
        if end <= start:
            raise ValueError("The on-call period must end after it starts.")
        if end - start > MAX_SPAN:
            raise ValueError("An on-call period can be at most 31 days - add the next period separately.")
        entry = {"engineer": engineer, "start": start.isoformat(timespec="minutes"),
                 "end": end.isoformat(timespec="minutes"), "added_by": added_by}
        entries = [e for e in self.entries(datetime.now())
                   if not (e["engineer"] == engineer and e["start"] == entry["start"] and e["end"] == entry["end"])]
        entries.append(entry)
        self._save(entries)
        return entry

    def remove(self, engineer: str, start: datetime | None = None) -> int:
        """Remove an engineer's entries (just the one starting at `start` if given). Returns how many went."""
        keep, removed = [], 0
        wanted = " ".join(str(engineer or "").lower().split())
        for e in self._load():
            same = bool(wanted) and " ".join(str(e["engineer"]).lower().split()) == wanted and (
                start is None or e["start"] == start.isoformat(timespec="minutes"))
            if same:
                removed += 1
            else:
                keep.append(e)
        if removed:
            self._save(keep)
        return removed
