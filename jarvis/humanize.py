"""Small formatting helpers so machine-shaped values never get shown or spoken to the owner verbatim.

A crontab string ("0 8 * * 1-5") or a raw ISO timestamp ("2026-10-06T09:00") is exactly what an API wants,
but reading either one back on the HUD's approval cards or over TTS is useless - "weekdays at 8am" and
"Tuesday 6 October at 9:00am" are what was actually meant. Keep the machine format for anything that still
has to go to an API (the FSM job body, the scheduler); use these only for text a person will actually read
or hear.
"""

from __future__ import annotations

from datetime import datetime

_DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DOW_ALIASES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def _dow_name(token: str) -> str | None:
    token = token.strip().lower()
    if token in _DOW_ALIASES:
        return _DOW[_DOW_ALIASES[token]]
    if token.isdigit():
        n = int(token) % 7  # crontab: both 0 and 7 mean Sunday
        return _DOW[6] if n == 0 else _DOW[n - 1]
    return None


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _format_time(hour: int, minute: int) -> str:
    suffix = "am" if hour < 12 else "pm"
    h12 = hour % 12 or 12
    return f"{h12}{'' if minute == 0 else f':{minute:02d}'}{suffix}"


def _days_phrase(dow: str, dom: str) -> str:
    if dow == "*" and dom == "*":
        return "every day"
    if dow != "*":
        if dow.lower() in ("1-5", "mon-fri"):
            return "every weekday"
        names = [n for n in (_dow_name(t) for t in dow.split(",")) if n]
        if names:
            if set(names) == {"Saturday", "Sunday"}:
                return "every weekend"
            if len(names) == 1:
                return f"every {names[0]}"
            return "every " + ", ".join(names[:-1]) + f" and {names[-1]}"
    if dom != "*" and dom.isdigit():
        return f"on the {_ordinal(int(dom))} of the month"
    return ""


def cron_to_english(cron: str) -> str:
    """Best-effort natural-language rendering of a standard 5-field crontab schedule, e.g. "0 8 * * 1-5" ->
    "every weekday at 8am". Falls back to naming the raw schedule for anything too unusual to describe
    cleanly, rather than guessing wrong."""
    parts = cron.split()
    if len(parts) != 5:
        return f"schedule '{cron}'"
    minute, hour, dom, month, dow = parts
    if month != "*":
        return f"schedule '{cron}'"  # month-specific schedules are rare enough not to special-case

    if minute.startswith("*/") and hour == "*" and dom == "*" and dow == "*":
        return f"every {minute[2:]} minutes"
    if minute == "0" and hour.startswith("*/") and dom == "*" and dow == "*":
        return f"every {hour[2:]} hours"

    if minute.isdigit() and hour.isdigit():
        time_str = _format_time(int(hour), int(minute))
        days = _days_phrase(dow, dom)
        return f"{days} at {time_str}" if days else f"at {time_str}"

    return f"schedule '{cron}'"


def human_datetime(value: str) -> str:
    """Renders an ISO date or date+time string the way a person would say it, e.g. "2026-10-06T09:00" ->
    "Tuesday 6 October at 9:00am". Falls back to the original text unchanged if it isn't a date Jarvis
    recognises, so this never breaks on a value that just happens to pass through here."""
    if not value:
        return value
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return value
    date_part = f"{_DOW[dt.weekday()]} {dt.day} {_MONTHS[dt.month - 1]}"
    if "T" not in text and ":" not in text:
        return date_part
    return f"{date_part} at {_format_time(dt.hour, dt.minute)}"
