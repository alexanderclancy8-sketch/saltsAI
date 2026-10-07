"""Heartbeat stop rules for the owner's scheduled automations: back off when a check keeps finding nothing.

An automation that says NOTHING_TO_REPORT 60 times in a row is noise. This module is the (pure, clock-injected) policy
that ``services/automations.py`` applies to each run:

- a streak counts consecutive runs that found no change; every ``STREAK_PER_STEP`` (6) of them slows the effective interval
  one step up the ladder 10 min -> 30 min -> hourly -> 3 hours -> daily. Never faster than the owner's configured interval
  (the ladder only holds steps above it), and any run that reports a real change resets the streak - back to the configured
  interval at once. Slowing is done by skipping cron fires that come too soon after the last real run; the schedule itself
  is never rewritten.
- after ``ASK_AFTER`` (12 h) with no change, one short Teams-only message asks whether to keep, slow down or delete it, and
  is not repeated for ``ASK_COOLDOWN`` (24 h). Only automations that run more often than every 12 h are asked about.
- overnight (22:00-06:00 UK) a non-urgent automation runs at most hourly.
- an automation whose prompt mentions life-safety, lone workers, out-of-hours alarms or keyholders, or that the owner has
  flagged "never slow down", is exempt from all of the above: never slowed, never skipped overnight, never asked about.

Nothing here approves, sends or changes anything, and none of it touches the approval gate.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import ROOT_DIR
from ..cron import cron_trigger

STEPS_MIN = (10, 30, 60, 180, 1440)       # the ladder, in minutes; only steps above the configured interval are used
STREAK_PER_STEP = 6                       # consecutive no-change runs per step up
ASK_AFTER = timedelta(hours=12)           # no change for this long -> ask the owner once
ASK_COOLDOWN = timedelta(hours=24)        # ...and not again for this long
ASK_MAX_INTERVAL_MIN = 12 * 60            # only automations running more often than this are asked about
NIGHT_START_HOUR, NIGHT_END_HOUR = 22, 6  # overnight window, local (UK) time
NIGHT_GAP_MIN = 60                        # at most hourly overnight
FIRE_TOLERANCE_S = 300                    # a cron fire this much early (a run takes time) still counts as due
DAY_MIN = 1440
CHECKLIST_NAME = "HEARTBEAT.md"
MAX_CHECKLIST_CHARS = 2000

# Whole phrases that mark an automation as safety-critical. Matched against its description and prompt.
EXEMPT_RE = re.compile(
    r"life[\s-]*safety|lone[\s-]*worker|out[\s-]*of[\s-]*hours?\s+alarms?|key[\s-]*holder", re.IGNORECASE)


def parse_iso(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value or "").strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def human_interval(minutes: int) -> str:
    if minutes % DAY_MIN == 0:
        n = minutes // DAY_MIN
        return "daily" if n == 1 else f"every {n} days"
    if minutes % 60 == 0:
        n = minutes // 60
        return "hourly" if n == 1 else f"every {n} hours"
    return f"every {minutes} minutes"


# --------------------------------------------------------------------------- the configured interval
def configured_minutes(cron: str, timezone_name: str, now: datetime) -> int:
    """The owner's interval: the shortest gap between the next few fires of their cron. Unknown -> a day (never slowed)."""
    try:
        trigger = cron_trigger(cron, timezone=timezone_name)
        fire = trigger.get_next_fire_time(None, now)
        gaps: list[float] = []
        for _ in range(8):
            if fire is None:
                break
            nxt = trigger.get_next_fire_time(fire, fire + timedelta(seconds=1))
            if nxt is None:
                break
            gaps.append((nxt - fire).total_seconds() / 60)
            fire = nxt
        return max(1, int(min(gaps))) if gaps else DAY_MIN
    except Exception:  # noqa: BLE001 - an odd cron must never stop a run, it just isn't slowed
        return DAY_MIN


# --------------------------------------------------------------------------- the rules
def exemption(automation: dict) -> str:
    """Why this automation is never slowed ("" if it can be)."""
    if automation.get("never_slow"):
        return "you set it to never slow down"
    found = EXEMPT_RE.search(f"{automation.get('description') or ''} {automation.get('prompt') or ''}")
    return f"it mentions {' '.join(found.group(0).lower().split())}, so it is treated as safety-critical" if found else ""


def ladder(configured: int) -> list[int]:
    return [configured] + [s for s in STEPS_MIN if s > configured]


def effective_minutes(configured: int, streak: int, exempt: bool = False) -> int:
    steps = ladder(configured)
    if exempt:
        return configured
    return steps[min(max(streak, 0) // STREAK_PER_STEP, len(steps) - 1)]


def is_overnight(now: datetime, tz: ZoneInfo) -> bool:
    hour = now.astimezone(tz).hour
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR


def skip_reason(automation: dict, configured: int, now: datetime, tz: ZoneInfo) -> str:
    """Should this cron fire be skipped? "" to run, otherwise the reason. Fires are only ever skipped, never added, so the
    effective interval is never below the configured one."""
    if exemption(automation):
        return ""
    last = parse_iso(automation.get("last_run_at"))
    if last is None:
        return ""
    elapsed = (now - last).total_seconds()
    effective = effective_minutes(configured, int(automation.get("nochange_streak") or 0))
    if effective > configured and elapsed < effective * 60 - FIRE_TOLERANCE_S:
        return f"slowed to {human_interval(effective)}"
    if is_overnight(now, tz) and elapsed < NIGHT_GAP_MIN * 60 - FIRE_TOLERANCE_S:
        return "overnight, at most hourly"
    return ""


def next_state(automation: dict, no_change: bool, now: datetime) -> dict:
    """The streak columns after a run: one more quiet run, or a reset because something changed."""
    if not no_change:
        return {"nochange_streak": 0, "nochange_since": ""}
    return {"nochange_streak": int(automation.get("nochange_streak") or 0) + 1,
            "nochange_since": automation.get("nochange_since") or iso(now)}


def should_ask(automation: dict, configured: int, now: datetime, tz: ZoneInfo) -> bool:
    """Time for the one "keep, slow down or delete?" question? Never overnight, never for an exempt automation, and not
    again within ASK_COOLDOWN of the last time."""
    if exemption(automation) or configured >= ASK_MAX_INTERVAL_MIN:
        return False
    if int(automation.get("nochange_streak") or 0) < 1:
        return False
    since = parse_iso(automation.get("nochange_since"))
    if since is None or now - since < ASK_AFTER or is_overnight(now, tz):
        return False
    asked = parse_iso(automation.get("last_asked_at"))
    return asked is None or now - asked >= ASK_COOLDOWN


def describe(automation: dict, configured: int) -> dict:
    """What list_automations shows: the effective interval, the streak and why it was slowed."""
    streak = int(automation.get("nochange_streak") or 0)
    why_exempt = exemption(automation)
    effective = effective_minutes(configured, streak, bool(why_exempt))
    if why_exempt:
        note = f"Not slowed: {why_exempt}."
    elif effective > configured:
        note = (f"{streak} checks in a row found nothing new, so it now runs about {human_interval(effective)} "
                f"instead of {human_interval(configured)}. Any change puts it back.")
    else:
        note = ""
    return {"configured_interval": human_interval(configured), "effective_interval": human_interval(effective),
            "no_change_streak": streak, "slowed_because": note, "never_slow_down": bool(automation.get("never_slow"))}


def ask_message(automation: dict, effective: int, now: datetime) -> tuple[str, str]:
    """(subject, body) of the one Teams question. Subject always starts with '[Jarvis]'."""
    name = " ".join(str(automation.get("description") or f"Automation {automation['id']}").split())[:80]
    since = parse_iso(automation.get("nochange_since")) or now
    hours = max(1, int((now - since).total_seconds() // 3600))
    streak = int(automation.get("nochange_streak") or 0)
    body = (f"\"{name}\" (automation #{automation['id']}) has found nothing new for about {hours} hours "
            f"({streak} checks in a row) and now runs {human_interval(effective)}. "
            "Shall I keep it as it is (never slow down), let it keep slowing down, or delete it? "
            "I won't ask again for 24 hours.")
    return f"[Jarvis] Keep, slow down or delete \"{name}\"?", body


# --------------------------------------------------------------------------- the optional checklist file
def read_checklist(data_dir) -> str:
    """The owner's optional HEARTBEAT.md (house rules for scheduled runs): the data dir's copy, else the repo's."""
    for base in (Path(data_dir), ROOT_DIR):
        try:
            text = (base / CHECKLIST_NAME).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            return text[:MAX_CHECKLIST_CHARS]
    return ""
