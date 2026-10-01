"""Build an APScheduler `CronTrigger` from a standard 5-field crontab string, correctly.

Every cron string in this codebase (the `*_cron` settings in `config.py`, automations the owner creates,
the Settings page's cron picker in `hud.js`, and `humanize.cron_to_english`'s rendering of them) is standard
crontab notation for the weekday field: 0-6 (or 7) where 0 and 7 both mean Sunday, 1 Monday, ... 6 Saturday.

APScheduler's own `CronTrigger.from_crontab()` looks like the right way to parse that, but its weekday field
uses a *different* convention internally - 0 Monday ... 6 Sunday, matching Python's `date.weekday()` - and
`from_crontab` does no translation, it just hands the digits straight to the constructor. So
`CronTrigger.from_crontab("30 16 * * 5")`, written and displayed everywhere else as "every Friday", actually
fires every Saturday; a `1-5` "weekdays" field actually runs Tuesday-Saturday. This is a documented
APScheduler quirk, not a one-off mistake - see the "day_of_week" note in their changelog/docs.

`cron_trigger()` below is the one place that gap gets closed: it shifts only the weekday field into
APScheduler's convention before constructing the trigger, so every cron string in this codebase keeps
meaning what `cron_to_english()` says it means and what the Settings page picker shows. Every call that
turns a stored cron string into a live schedule should go through this, not `CronTrigger.from_crontab`
directly.
"""

from __future__ import annotations

import re

from apscheduler.triggers.cron import CronTrigger


def _shift_dow_token(tok: str) -> str:
    if not tok.isdigit():
        return tok  # a weekday name ("mon", "tue", ...) already matches APScheduler's own vocabulary
    return str((int(tok) - 1) % 7)  # crontab 0-7 (0/7=Sun,1=Mon,...) -> APScheduler 0=Mon...6=Sun


def _shift_dow_field(field: str) -> str:
    if field == "*" or "/" in field:  # a step field ('*/2') is rare here and not worth remapping
        return field
    parts = []
    for tok in field.split(","):
        m = re.match(r"^(\d+)-(\d+)$", tok)
        parts.append(f"{_shift_dow_token(m.group(1))}-{_shift_dow_token(m.group(2))}" if m else _shift_dow_token(tok))
    return ",".join(parts)


def cron_trigger(cron: str, timezone=None) -> CronTrigger:
    """The `CronTrigger.from_crontab(cron, timezone=timezone)` every caller actually wants."""
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(f"Wrong number of fields; got {len(parts)}, expected 5")
    minute, hour, day, month, dow = parts
    return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=_shift_dow_field(dow),
                        timezone=timezone)
