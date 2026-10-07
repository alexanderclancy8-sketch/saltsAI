"""Is this van moving? RAM's vehicle list has no speed, so "moving" is worked out from three weaker signals, never one.

What RAM's published schema (https://api.qaifn.co.uk/v2/api-docs) gives for a vehicle is ``vehicle_status.last_event`` (the NAME
of the latest event only), ``vehicle_status.event_date`` (when), ``vehicle_status.location`` and ``engineRpm`` - no speed and
no heading. (The history endpoint has ``speedKph``, but RAM allows 3 requests a minute for it, so it can't be asked per van.)
The old rule, "moving if the last event is TRANSIT_START or OVER_SPEED", read a van that set off 20 minutes ago and has since
logged a HARSH_BRAKING, an IDLE_END or a ZONE_OUT as parked. The rule is now, in order:

1. **Position change** between two successive polls of the shared (cached) vehicle list: more than ``MOVE_MIN_M`` metres at least
   ``MOTION_MIN_S`` seconds apart is movement, and distance / time is an *estimated* speed. GPS drift (10-20 m), a jump no
   vehicle could make, a poor-accuracy fix and a first reading with nothing to compare against are all NOT movement.
2. **The last event's class** (``MOVING_EVENTS`` / ``STOPPED_EVENTS`` / everything else = neutral) - counted only while the
   event is recent (``EVENT_FRESH_MIN``); an old "moving" event is shown as "No recent position (last seen N min ago)", never
   as live motion.
3. **engineRpm** above zero with a neutral recent event means the engine is on: "Stopped, engine on", not "Parked".

No request is made here: this reads the rows ``RamTracking`` already fetched, so RAM's 3-requests-a-minute limit is untouched.
Nothing in a result is a coordinate. A speed is only ever an estimate from successive fixes ("about 30 mph") or absent.

The last position per van is kept in memory and (for vans seen moving in the last few minutes only) in the key-value table so a
restart in the middle of a journey doesn't forget it. A parked van's point - which at night is somebody's home - is never
written there, and anything older than ``MOTION_WINDOW_S`` is discarded on load.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------------- the event table
# Data, in one place, so it can be tuned after comparing with RAM's own portal (see Tracker.fleet_diagnostics). A name in
# none of these sets is NEUTRAL: it says nothing about whether the van is moving.
MOVING_EVENTS = frozenset({
    "TRANSIT_START", "HOME_MOVING", "ROAMING_MOVING", "UNKNOWN_MOVING", "CELL_ID_MOVING", "MOVEMENT_NO_IGNITION",
    "OVER_SPEED", "UNDER_SPEED", "TRIP_START", "TRIP_START_FM",
    "HARSH_ACCELERATION", "HARSH_BRAKING", "HARSH_CORNERING", "UNKNOWN_HARSH_EVENT", "HARSH_EVENT_END",
    "ACCELERATION_END", "DECELERATION_END", "IDLE_END", "IDLE_END_FM", "FIRST_IGNITION_OF_DAY",
})
STOPPED_EVENTS = frozenset({
    "TRANSIT_STOP", "IGNITION_OFF", "IDLE_START", "IDLE_START_FM", "EXTENDED_STOP", "EXTENDED_IDLE_", "EXTENDED_IDLE",
    "HOME_STOPPED", "ROAMING_STOPPED", "UNKNOWN_STOPPED", "STATIONARY_NO_IGNITION", "CELL_ID_NOT_MOVING",
    "TRIP_END", "TRIP_END_FM", "TRAILER_DISCONNECTED_PARKED",
})
# Can happen either way (a van drives out of a zone and sits in one). Listed for the record: anything unlisted is neutral too.
NEUTRAL_EVENTS = frozenset({
    "ZONE_OUT", "GEOFENCE_OUT", "LCZ_OUT", "CUSTOM_OUT", "TOLL_OUT", "DATA_CONNECTION_REPORT", "DEVICE_STATUS",
    "INFORMATION_REPORT", "LOCATION_REPLY", "MANUEL_LOCATION_UPDATE", "GPS_LOST", "DRIVER_ALLOCATED",
})
# A stopped event that means the engine is RUNNING while the van is still (idling), as opposed to ignition off.
IDLE_EVENTS = frozenset({"IDLE_START", "IDLE_START_FM", "EXTENDED_IDLE_", "EXTENDED_IDLE"})

MOVING, STOPPED, NEUTRAL = "moving", "stopped", "neutral"

# --------------------------------------------------------------------------------------------- thresholds
EVENT_FRESH_MIN = 15       # a "moving" event only counts as live motion for this long after event_date
LAST_SEEN_MIN = 60         # an idling claim, or a neutral/unknown state, is not made from an event older than this
MOVE_MIN_M = 50            # more than this between two polls is movement; 10-20 m of GPS drift is not
MOVE_MIN_MPH = 2.0         # ...and the distance must be at a walking pace or faster over the time between the polls
MOTION_MIN_S = 30          # two polls closer together than this are not compared (distance over a few seconds is noise)
MOTION_WINDOW_S = 300      # a baseline older than this is too old to compare with: start again from the new reading
HOLD_S = 90                # after movement is seen, a still reading within this long (a red light) still counts as moving
MAX_PLAUSIBLE_MPH = 100    # a displacement implying more than this is a bad fix, not a van: ignored
PERSIST_KEY = "ram_motion_v1"
MPS_TO_MPH = 2.2369363


def classify_event(name: Any) -> str:
    """MOVING, STOPPED or NEUTRAL for a RAM event name (case and surrounding space ignored; unknown / blank = NEUTRAL)."""
    key = str(name or "").strip().upper()
    if key in MOVING_EVENTS:
        return MOVING
    if key in STOPPED_EVENTS:
        return STOPPED
    return NEUTRAL


def parse_when(value: Any) -> datetime | None:
    """An ISO 8601 date-time as an aware UTC datetime. RAM publishes them with a trailing Z; one with no zone is read as UTC."""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def _point(lat: Any, lng: Any) -> tuple[float, float] | None:
    try:
        la, ln = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    return (la, ln) if math.isfinite(la) and math.isfinite(ln) and -90 <= la <= 90 and -180 <= ln <= 180 else None


def ago(minutes: float | None) -> str:
    """'7 min', '3 h', '2 days': how long ago, kept short."""
    if minutes is None:
        return "an unknown time"
    m = int(minutes)
    if m < 120:
        return f"{m} min"
    return f"{m // 60} h" if m < 2880 else f"{m // 1440} days"


def round_speed(mph: float) -> int:
    """An estimate is shown to the nearest 5 mph: finer would claim precision two GPS fixes can't give."""
    return max(5, int(round(mph / 5.0)) * 5)


# --------------------------------------------------------------------------------------------- position change
@dataclass
class Evidence:
    """What the change in position since the last poll says. ``moving`` is True only for real, plausible movement (or the
    short hold after it); ``kind`` and ``reason`` say why, in words, with no coordinates."""
    kind: str                  # moved | held | still | jitter | jump | first | gap | waiting | unchanged | poor_gps | none
    moving: bool = False
    speed_mph: float | None = None
    reason: str = ""


@dataclass
class _Seen:
    lat: float
    lng: float
    at: datetime                       # when WE took the reading (the poll), not RAM's event_date
    event: datetime | None             # RAM's event_date at that reading (to tell a repeat of the cached list from news)
    moved_at: datetime | None = None   # when movement was last seen
    speed_mph: float | None = None     # the estimate from that movement


class MotionTracker:
    """Last position per van from successive polls, turned into ``Evidence``. Pure apart from the optional ``store`` (anything
    with ``get_kv(key)`` / ``set_kv(key, value)``), which only ever receives vans seen moving in the last few minutes."""

    def __init__(self, store: Any = None):
        self.store = store
        self._seen: dict[str, _Seen] = {}
        self._loaded = False
        self._saved = ""

    # -- persistence ------------------------------------------------------------------------------
    def _load(self, now: datetime) -> None:
        self._loaded = True
        if self.store is None:
            return
        try:
            data = json.loads(self.store.get_kv(PERSIST_KEY) or "{}")
            for key, v in (data if isinstance(data, dict) else {}).items():
                at, moved = parse_when(v.get("at")), parse_when(v.get("moved_at"))
                pt = _point(v.get("lat"), v.get("lng"))
                if at is None or pt is None or not 0 <= (now - at).total_seconds() <= MOTION_WINDOW_S:
                    continue  # too old to compare with: dropped, never used
                self._seen[str(key)] = _Seen(pt[0], pt[1], at, parse_when(v.get("event")), moved,
                                             v.get("speed_mph") if isinstance(v.get("speed_mph"), (int, float)) else None)
        except Exception as e:  # noqa: BLE001 - a broken saved value just means "no history yet"
            log.warning("RAM motion history could not be read (%s)", type(e).__name__)

    def _save(self, now: datetime) -> None:
        if self.store is None:
            return
        keep = {k: {"lat": s.lat, "lng": s.lng, "at": s.at.isoformat(), "moved_at": s.moved_at.isoformat(),
                    "event": s.event.isoformat() if s.event else None, "speed_mph": s.speed_mph}
                for k, s in self._seen.items()
                if s.moved_at is not None and 0 <= (now - s.moved_at).total_seconds() <= MOTION_WINDOW_S}
        text = json.dumps(keep, sort_keys=True)
        if text == self._saved or (text == "{}" and not self._saved):
            return
        try:
            self.store.set_kv(PERSIST_KEY, text)
            self._saved = text
        except Exception as e:  # noqa: BLE001
            log.warning("RAM motion history could not be saved (%s)", type(e).__name__)

    # -- the comparison ---------------------------------------------------------------------------
    @staticmethod
    def _held(prev: _Seen, now: datetime, note: str = "") -> Evidence:
        """No new movement: still 'moving' for HOLD_S after the last (a van stopped at lights), else not."""
        if prev.moved_at is not None and 0 <= (now - prev.moved_at).total_seconds() <= HOLD_S:
            secs = int((now - prev.moved_at).total_seconds())
            return Evidence("held", True, prev.speed_mph, f"moved {secs} s ago, still counted as moving for {HOLD_S} s")
        return Evidence("unchanged" if note else "still", False, None, note or "position has not changed")

    def observe(self, key: str, lat: Any, lng: Any, event: datetime | None, now: datetime, gps_ok: bool = True) -> Evidence:
        here = _point(lat, lng)
        if here is None:
            return Evidence("none", reason="no position from RAM")
        if not gps_ok:  # RAM flagged this fix as not accurate enough: not a basis for 'moved' or 'stayed'
            return Evidence("poor_gps", reason="RAM marked this GPS fix as not accurate enough to compare")
        prev = self._seen.get(key)
        if prev is None or not 0 <= (now - prev.at).total_seconds() <= MOTION_WINDOW_S:
            gap = prev is not None
            self._seen[key] = _Seen(here[0], here[1], now, event)
            return Evidence("gap" if gap else "first", reason=(
                "last reading was too long ago to compare with" if gap else "first reading, nothing earlier to compare with"))
        if (prev.lat, prev.lng) == here and prev.event == event:
            return self._held(prev, now, "no new reading since the last poll")
        secs = (now - prev.at).total_seconds()
        if secs < MOTION_MIN_S:
            return self._held(prev, now, f"only {round(secs)} s since the last reading, too soon to compare")
        metres = distance_m((prev.lat, prev.lng), here)
        mph = metres / secs * MPS_TO_MPH
        had_moved, had_speed = prev.moved_at, prev.speed_mph
        self._seen[key] = _Seen(here[0], here[1], now, event, had_moved, had_speed)
        if mph > MAX_PLAUSIBLE_MPH:
            return Evidence("jump", reason=f"ignored a jump of {round(metres)} m in {round(secs)} s (faster than {MAX_PLAUSIBLE_MPH} mph "
                                           "is a bad fix, not a van)")
        if metres > MOVE_MIN_M and mph >= MOVE_MIN_MPH:
            self._seen[key].moved_at, self._seen[key].speed_mph = now, mph
            return Evidence("moved", True, mph, f"moved {round(metres)} m in {round(secs)} s")
        held = self._held(self._seen[key], now)
        if held.moving:
            return held
        if metres > 5:
            return Evidence("jitter", reason=f"moved only {round(metres)} m in {round(secs)} s (GPS drift, not movement; "
                                             f"it takes more than {MOVE_MIN_M} m)")
        return Evidence("still", reason=f"did not move in {round(secs)} s")

    # -- a whole poll -----------------------------------------------------------------------------
    def apply(self, rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
        """Add ``motion`` (see ``classify``), ``moving`` and ``status`` to every row from RAM's vehicle list. ``now`` is aware UTC."""
        if not self._loaded:
            self._load(now)
        live_keys = set()
        for row in rows:
            key = str(row.get("id") if row.get("id") is not None else row.get("registration") or "")
            live_keys.add(key)
            ev = (self.observe(key, row.get("lat"), row.get("lng"), parse_when(row.get("timestamp")), now,
                               row.get("gps_ok", True)) if key else Evidence("none", reason="no vehicle id"))
            motion = classify(row.get("event"), parse_when(row.get("timestamp")), row.get("engine_rpm"), ev, now,
                              has_position=_point(row.get("lat"), row.get("lng")) is not None)
            row["motion"] = motion
            row["moving"] = motion["moving"]
            row["status"] = {"moving": "driving", "engine_on": "idling", "parked": "parked"}.get(motion["state"], "no_position")
        for key in [k for k in self._seen if k not in live_keys]:
            del self._seen[key]  # a van RAM no longer lists
        self._save(now)
        return rows


# --------------------------------------------------------------------------------------------- the verdict
def classify(event_name: Any, event_dt: datetime | None, engine_rpm: Any, ev: Evidence, now: datetime,
             has_position: bool = True) -> dict[str, Any]:
    """One van's final state from its last event, how old that is, its engine RPM and the position evidence.

    ``state`` is moving | engine_on | parked | no_position, ``label`` the words for the Fleet list and the tools, ``reason`` how it
    was decided (for the owner's diagnostics). ``speed_mph`` is an estimate from successive fixes, or None - never invented.
    """
    name = str(event_name or "").strip().upper()
    cls = classify_event(name)
    age = max(0.0, (now - event_dt).total_seconds() / 60) if event_dt else None
    rpm = engine_rpm if isinstance(engine_rpm, (int, float)) and not isinstance(engine_rpm, bool) and engine_rpm > 0 else None
    shown = name or "no event"
    base = {"event": name or None, "event_class": cls, "event_age_min": int(age) if age is not None else None,
            "engine_rpm": int(engine_rpm) if isinstance(engine_rpm, (int, float)) and not isinstance(engine_rpm, bool) else None,
            "position_evidence": ev.kind, "speed_estimated": False}

    def out(state: str, label: str, reason: str, speed: float | None = None) -> dict[str, Any]:
        return {**base, "state": state, "label": label, "moving": state == "moving", "engine_on": state == "engine_on",
                "speed_mph": round_speed(speed) if speed is not None else None, "speed_estimated": speed is not None,
                "reason": reason}

    def last_seen() -> str:
        return f"last_event {shown} {ago(age)} ago" if age is not None else f"last_event {shown}, no event time"

    if not has_position:
        return out("no_position", "No recent position" + (f" (last seen {ago(age)} ago)" if age is not None else ""),
                   "RAM sent no position for this van")
    if ev.moving:  # the strongest signal: we SAW it move between polls
        speed = ev.speed_mph
        label = f"Moving (about {round_speed(speed)} mph)" if speed else "Moving"
        return out("moving", label, ev.reason + (f"; {last_seen()}" if ev.kind == "held" else ""), speed)
    gap = f"; position check: {ev.reason}" if ev.reason else ""
    if age is None:
        return out("no_position", "No recent position (no event time from RAM)", f"{last_seen()}, so its age is unknown" + gap)
    fresh = age <= EVENT_FRESH_MIN
    if cls == MOVING:
        if fresh:
            return out("moving", "Moving", f"{last_seen()} (a moving event, within {EVENT_FRESH_MIN} min); speed not available" + gap)
        return out("no_position", f"No recent position (last seen {ago(age)} ago)",
                   f"{last_seen()}: a moving event older than {EVENT_FRESH_MIN} min no longer counts as moving" + gap)
    if cls == STOPPED:
        if name in IDLE_EVENTS and age <= LAST_SEEN_MIN:
            return out("engine_on", "Stopped, engine on", f"{last_seen()} (idling: engine running, van still)" + gap)
        return out("parked", "Parked", f"{last_seen()} (a stopped event)" + gap)
    # neutral or unknown: the name says nothing either way
    if age > LAST_SEEN_MIN:
        return out("no_position", f"No recent position (last seen {ago(age)} ago)",
                   f"{last_seen()}: a neutral event older than {LAST_SEEN_MIN} min says nothing about now" + gap)
    if rpm is not None and fresh:
        return out("engine_on", "Stopped, engine on", f"{last_seen()} (neutral) with engine RPM {int(rpm)} above zero, no movement seen" + gap)
    why = "engine RPM is zero or not reported" if rpm is None else f"engine RPM {int(rpm)} but the event is {ago(age)} old"
    return out("parked", "Parked", f"{last_seen()} (neutral); {why}; no movement seen" + gap)
