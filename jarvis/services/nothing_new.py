"""If there's nothing new, Jarvis says nothing.

One place that decides whether something a check produced amounts to "nothing new", so no channel (the console chat, the
Alerts list, Teams, push or voice) is ever handed a "Nothing new" / "No changes" / "All quiet" message. A run that found
nothing is only recorded in the activity log ("What Jarvis did", ``check_runs``) for anyone who goes looking.

* ``is_nothing_new(text)`` - the reply of a scheduled run (an automation, the nightly self-reflection) says there is nothing
  new: it starts with the ``NOTHING_TO_REPORT`` marker (after any whitespace or markdown), or it is a short reply made up only
  of "nothing new" phrasing ("Nothing new.", "No changes since last time.", "All quiet - I'll check again later."). A reply
  with anything else in it - a second clause that says something, "but", "however", "except", a figure - is real news and is
  NOT treated as nothing new. When in doubt it is news: a missed "nothing new" is one message too many, a missed finding is
  worse.
* ``visible_conversation(rows)`` - the conversation as the console shows it: the prompts of background turns (an automation
  run, the self-reflection) and their replies are left out. They stay in the transcript; a finding a check reports is posted
  as its own message (``Proactive``), which stays visible.
"""

from __future__ import annotations

import re
from typing import Any

NOTHING = "NOTHING_TO_REPORT"   # what a scheduled run starts its reply with when there is nothing worth saying
MAX_QUIET_CHARS = 280           # a longer reply is never read as "nothing new" unless it starts with the marker

AUTOMATION_PROMPT_TAG = "[Scheduled check you set up"   # start of an automation run's prompt (services/automations.py)
REFLECTION_PROMPT_TAG = "[Scheduled self-reflection"    # start of the nightly reflection's prompt (services/self_learning.py)
BACKGROUND_PROMPT_TAGS = (AUTOMATION_PROMPT_TAG, REFLECTION_PROMPT_TAG)

# Leading / trailing whitespace and markdown: bold, italics, headings, quotes, bullets, code ticks, brackets, quote marks.
_LEAD = re.compile(r"^[\s>*_`#~\-+•\[\](){}\"'“”‘’.:;,!|]+")
_TRAIL = re.compile(r"[\s>*_`#~\-+•\[\](){}\"'“”‘’.:;,!|]+$")
_MARKER = re.compile(r"^nothing[\s_-]*to[\s_-]*report\b", re.IGNORECASE)

# Anything that turns "nothing new" into "nothing new, but ...": the reply has real news in it.
_CONTRAST = re.compile(r"\b(?:but|however|except|although|though|apart\s+from|other\s+than|aside\s+from|besides|whereas|"
                       r"still\s+(?:failing|down|broken|overdue|open|waiting|sounding|missing|not))\b", re.IGNORECASE)
_TIME = re.compile(r"\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b", re.IGNORECASE)  # "since 09:30" is not a figure

# Clauses are split on sentence ends, semicolons, colons, dashes and commas, then on "and", so "No new faults, the panel at X
# is offline" is two clauses and the second one is news.
_CLAUSES = re.compile(r"(?:[.!?;:\n]+|\s[-–—]+\s|,)\s*")
_AND = re.compile(r"\s+and\s+", re.IGNORECASE)
# A filler clause ("checked the inbox and the FSM") that also says something happened is not filler.
_SAYS_SOMETHING = re.compile(r"\b(?:is|are|was|were|has|have|had|needs?|failed|broken|down|overdue|late|missing|went|been|"
                             r"came|arrived|booked|sent|raised|accepted|declined|stopped|started)\b", re.IGNORECASE)

_TAIL = (r"(?:\s+(?:today|so\s+far|for\s+now|again|as\s+before|this\s+time|at\s+the\s+moment|right\s+now|"
         r"since\s+(?:the\s+)?last\s+(?:time|check|run|look)))*$")

# A clause that says "there is nothing new". The explicit forms ("nothing new ...", "no new ...", "no changes ...") may run on
# ("No new jobs have been booked for tomorrow"); the loose ones ("all quiet", "everything as before") must be the whole clause.
_NOTHING_CLAUSE = re.compile(
    r"^(?:"
    r"(?:there\s+(?:is|'s|was|has\s+been)\s+|i\s+(?:have\s+)?found\s+|i'?ve\s+found\s+|found\s+|still\s+)?"
    r"nothing\s+(?:new|else|further|more|significant|notable|durable|of\s+note|at\s+all|has\s+changed|changed|"
    r"stands\s+out|stood\s+out|"
    r"to\s+(?:report|mention|flag|add|say|note|update|tell)|worth\s+(?:mentioning|reporting|flagging|noting|keeping|remembering)|found)\b.*"
    r"|(?:there\s+(?:are|were|is|was|have\s+been|has\s+been)\s+|i\s+(?:have\s+)?found\s+|i'?ve\s+found\s+|found\s+)?"
    r"no\s+(?:new|changes?|changed|updates?|news|findings?|differences?|movement|further\s+(?:changes?|updates?))\b.*"
    r"|no\s+(?:issues?|problems?|faults?|alerts?)\s+(?:found|detected|to\s+report)\b.*"
    r"|no\s+action\s+(?:needed|required)" + _TAIL +
    r"|(?:it'?s\s+|it\s+is\s+|all\s+|still\s+)?(?:quiet|clear|good|fine|ok|okay|normal|unchanged|the\s+same)" + _TAIL +
    r"|everything\s+(?:is\s+|'s\s+|looks\s+|remains\s+|stays\s+|still\s+|seems\s+)*"
    r"(?:as\s+(?:it\s+was\s+)?(?:before|expected)|as\s+it\s+was|unchanged|the\s+same|quiet|clear|fine|ok|okay|normal|"
    r"in\s+order|on\s+track|good)" + _TAIL +
    r"|(?:the\s+)?same\s+as\s+(?:last\s+time|before|the\s+last\s+(?:check|run|time))" + _TAIL +
    r"|(?:still\s+)?nothing" + _TAIL +
    r"|none" + _TAIL +
    r")", re.IGNORECASE)

# A clause that adds nothing: "I'll check again later", "will keep watching", "checked the inbox".
_FILLER_CLAUSE = re.compile(
    r"^(?:"
    r"(?:i'?ll|i\s+will|will)\s+(?:keep|check|look|carry\s+on|let\s+you\s+know|tell\s+you|report|try\s+again|watch)\b.*"
    r"|(?:i\s+)?(?:have\s+|'ve\s+)?(?:checked|looked|reviewed|scanned|went\s+through)\b.*"
    r"|(?:keeping|still)\s+(?:an\s+eye|watching|monitoring)\b.*"
    r"|(?:as\s+before|as\s+expected|since\s+(?:the\s+)?last\s+(?:time|check|run|look)|this\s+time|for\s+now|today|so\s+far|"
    r"yet|again|all\s+good|all\s+fine|nothing\s+else)$"
    r")", re.IGNORECASE)


def _strip(text: str) -> str:
    return _TRAIL.sub("", _LEAD.sub("", text or ""))


def _quiet_clause(clause: str) -> bool:
    return bool(_NOTHING_CLAUSE.match(clause) or (_FILLER_CLAUSE.match(clause) and not _SAYS_SOMETHING.search(clause)))


def _parts(piece: str) -> list[str]:
    return [c for c in (_strip(x) for x in _AND.split(piece)) if c]


def is_nothing_new(text: Any) -> bool:
    """True when ``text`` (a scheduled run's reply, or a message about to go out) amounts to "nothing new": empty, starting
    with the NOTHING_TO_REPORT marker, or a short reply made only of "nothing new" phrasing. False whenever it holds real news."""
    t = _LEAD.sub("", str(text or "")).strip()
    if not t:
        return True
    if _MARKER.match(t):  # the marker is authoritative, whatever note follows it
        return True
    if len(t) > MAX_QUIET_CHARS or _CONTRAST.search(t) or re.search(r"\d", _TIME.sub("", t)):
        return False  # long, or "nothing new, but ...", or figures: real content
    pieces = [c for c in (_strip(x) for x in _CLAUSES.split(_TIME.sub("then", t))) if c]
    if not pieces:
        return False
    first = _parts(pieces[0])
    if not first or not _NOTHING_CLAUSE.match(first[0]) or not all(_quiet_clause(c) for c in first[1:]):
        return False
    return all(_quiet_clause(p) or all(_quiet_clause(c) for c in _parts(p)) for p in pieces[1:])


def quiet_note(text: Any, default: str = "Nothing new.") -> str:
    """The short note recorded in the activity log for a nothing-new run: what followed the marker, or the reply itself."""
    t = _LEAD.sub("", str(text or "")).strip()
    m = _MARKER.match(t)
    if m:
        t = t[m.end():].lstrip(" :-.*_`")
    return " ".join(t.split())[:300] or default


def is_background_prompt(text: Any) -> bool:
    return str(text or "").lstrip().startswith(BACKGROUND_PROMPT_TAGS)


def visible_conversation(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The conversation rows the console shows: a background turn's prompt and the reply that answered it are left out (a
    finding it reported is its own row and stays). Rows are oldest first."""
    out: list[dict[str, Any]] = []
    skip_reply = False
    for r in rows:
        if r.get("role") == "user" and is_background_prompt(r.get("text")):
            skip_reply = True
            continue
        if skip_reply and r.get("role") == "assistant":
            skip_reply = False
            continue
        skip_reply = False
        out.append(r)
    return out
