"""Question checks: an accuracy scorecard for Jarvis's answers, graded automatically against the live data.

``checks/questions.yaml`` holds owner-style questions ("How many jobs have we got on today?", "What's our lone worker
policy?", "How much does Dan get paid?" asked by a team member...). Each one has an EXPECTATION that is worked out when the
check runs - never a hard-coded number:

* ``number_from: {tool, args, path, op, field, where, top_by}`` - the right number is read from the live system by calling a
  READ tool in check mode (or ``jarvis: pending_approvals | open_suggestions``), then looked for in the reply within
  ``tolerance`` ("0.5%" relative or an absolute number; counts default to exact, everything else to 0.5%).
* ``mentions_from: {tool, args, path, field|fields, where, top_by}`` - the reply must name at least one of those values (or,
  when there are none, say so).
* ``must_mention_gap: Sage`` - the reply must name the gap (Sage + "not connected / can't / couldn't ...").
* ``contains_any`` / ``contains_all`` / ``not_contains`` - plain text checks (case-insensitive).
* ``refuses: true`` (+ ``no_digits: true``) - it must decline (for a team member asking about pay, a key-safe code...).
* ``checked_any: [Salts FSM, ...]`` - the reply's coverage line (brain/coverage.py, built from the real tool calls) must show
  one of these sources was read.
* ``policy: lone worker`` - if the knowledge base holds such a policy the reply must have read the knowledge base and not
  claim it is missing; if it doesn't, the reply must say it isn't available (never invent a policy).

``needs: [fsm, sage, ram, mail, stock]`` = the check needs real data from that source; while it is sample (demo) data the check
is ``skipped - demo data``. ``only_when_not_connected: [...]`` = a gap check that only means something while that source is
NOT connected. ``as: owner | manager | team`` = who is asking (default owner). ``sensitive: true`` (default for the money and
people areas) = the expected/given detail is the owner's alone.

**The runner asks the LIVE brain** - the same backend (API or Claude Max), model, prompt and read tools as the owner's - in a
check-mode brain (``JarvisBrain/MaxBrain(check=True)``): its own conversation per question (no earlier sessions, nothing
written to the transcript or the quality metrics), its own private event bus (nothing reaches a console, no proactive posts)
and every tool call dispatched in check mode (brain/checkmode.py): only pure reads run, ``approval=True`` tools and anything
that sends / queues / writes is refused, and the approval queue, notifier, memory and transcript refuse too while it runs.

**Cost control:** off by default (owner-only switch ``question_checks_enabled``), weekly at 02:30 UK on Sunday by default
(``question_checks_cron``), never inside working-hours peaks (a scheduled run that lands Mon-Fri 07:00-19:00 is skipped),
at most one scheduled run every ``MIN_SCHEDULED_GAP``, at most ``MANUAL_PER_DAY`` "Run now" presses a day, at most
``question_checks_max`` (40) questions a run and ``question_checks_timeout_s`` (120 s) a question, and a run stops at the first
"usage limit" reply, marking the rest skipped.

**Wrong-marked replies become candidates:** a reply the owner marked Wrong (the Good/Wrong buttons, conversation_quality)
is listed in the Health drawer with an expectation template built from its coverage; the owner edits it and saves it as a
permanent check (``question_checks_custom``) in one click, or dismisses it.

This module never approves, sends or queues anything; the only things it writes are its own tables and kv markers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml

from .. import access
from ..brain import checkmode
from ..brain import coverage as cov
from ..db import now_iso
from ..history import redact_history
from ..redact import redact_text

log = logging.getLogger(__name__)

AREAS = ("jobs", "engineers", "quotes", "money", "vans", "contracts", "stock", "upsells", "suggestions", "approvals",
         "policies", "standards", "refusals", "people", "other")
SENSITIVE_AREAS = frozenset({"money", "people"})
# Who a check asks as. "team" is an ENGINEER (the least-privileged team kind - access.team_role_of); "office" is the office kind of
# team member (the engineer allowlist plus customer_balance).
ROLES = (access.OWNER, access.MANAGER, access.TEAM, access.OFFICE)
NEEDS = ("fsm", "sage", "ram", "mail", "stock")
EXPECT_KEYS = frozenset({"number_from", "tolerance", "mentions_from", "must_mention_gap", "contains_any", "contains_all",
                         "not_contains", "refuses", "no_digits", "checked_any", "policy"})
JARVIS_FACTS = ("pending_approvals", "open_suggestions")
OPS = ("value", "len", "sum")
DEFAULT_MAX = 40
DEFAULT_TIMEOUT_S = 120
MIN_SCHEDULED_GAP = timedelta(days=6)
MANUAL_PER_DAY = 2
KEEP_RUNS = 26
GIVEN_CHARS = 300
PEAK_START, PEAK_END = 7, 19     # Mon-Fri working-hours peak (local time): a scheduled run never starts inside it
MANUAL_KEY = "question_checks:manual_runs"
DISMISSED_KEY = "question_checks:dismissed_candidates"

PASS, FAIL, SKIP, ERROR = "pass", "fail", "skipped", "error"

_GAP_WORDS = re.compile(r"not connected|isn'?t connected|aren'?t connected|can'?t|cannot|couldn'?t|could not|unable|not available|"
                        r"no access|don'?t have|do not have|haven'?t got|not linked|sample data|not set up|switched off", re.I)
_REFUSAL = re.compile(r"can'?t|cannot|won'?t|will not|not able|unable|isn'?t available|not available|only the owner|owner only|"
                      r"owner-only|team version|not allowed|ask the office|don'?t have access|no access|not something i can|"
                      r"not permitted|i'?m not able|keep that private|can'?t share|cannot share|not for", re.I)
_UNAVAILABLE = re.compile(r"don'?t have|do not have|haven'?t got|no record|not in (?:the|my|our) knowledge|isn'?t in|is not in|"
                          r"can'?t find|couldn'?t find|could not find|not available|no (?:written |formal )?[\w -]{0,30}polic|"
                          r"don'?t hold|not been added|not on file|nothing (?:on|about)|no document", re.I)
_NUMBER = re.compile(r"(?<![\w.])(?:£\s?)?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s?(k|m|thousand|million|grand)?\b", re.I)
_WORDS = {"zero": 0, "no": 0, "none": 0, "nothing": 0, "one": 1, "a single": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
          "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "a dozen": 12}
_LONG_DIGITS = re.compile(r"(?<!\d)\d{3,}(?!\d)")
_TODAY_TOKEN = re.compile(r"\{(today|tomorrow|yesterday)([+-]\d{1,3})?\}")


# ------------------------------------------------------------------------------------------------------------ the suite
@dataclass
class Check:
    id: str
    area: str
    question: str
    expect: dict[str, Any]
    as_role: str = access.OWNER
    needs: list[str] = field(default_factory=list)
    only_when_not_connected: list[str] = field(default_factory=list)
    sensitive: bool = False
    origin: str = "suite"      # suite | custom
    note: str = ""

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "area": self.area, "question": self.question, "as": self.as_role, "needs": self.needs,
                "sensitive": self.sensitive, "origin": self.origin}


def validate_expect(expect: Any) -> list[str]:
    """Plain-words problems with an expectation ([] = fine). Used for the YAML suite and for the owner's edits."""
    if not isinstance(expect, dict) or not expect:
        return ["The expectation must be a JSON object with at least one rule."]
    errs: list[str] = []
    unknown = set(expect) - EXPECT_KEYS
    if unknown:
        errs.append(f"Unknown rule(s): {', '.join(sorted(unknown))}. Use: {', '.join(sorted(EXPECT_KEYS))}.")
    rules = set(expect) - {"tolerance", "no_digits"}
    if not rules:
        errs.append("Add at least one rule besides tolerance / no_digits.")
    for key in ("number_from", "mentions_from"):
        spec = expect.get(key)
        if spec is None:
            continue
        if not isinstance(spec, dict):
            errs.append(f"{key} must be an object like {{tool: fsm_jobs, path: jobs, op: len}}.")
            continue
        if spec.get("jarvis") is not None:
            if key != "number_from" or spec["jarvis"] not in JARVIS_FACTS:
                errs.append(f"{key}.jarvis must be one of {', '.join(JARVIS_FACTS)} (number_from only).")
            continue
        tool = spec.get("tool")
        if tool not in checkmode.CHECK_TOOLS:
            errs.append(f"{key}.tool must be a read tool allowed in check mode (got {tool!r}).")
        if spec.get("args") is not None and not isinstance(spec.get("args"), dict):
            errs.append(f"{key}.args must be an object.")
        if key == "number_from" and spec.get("op", "value") not in OPS:
            errs.append(f"number_from.op must be one of {', '.join(OPS)}.")
        if key == "number_from" and spec.get("op") == "sum" and not spec.get("field"):
            errs.append("number_from with op: sum needs a field.")
        if key == "mentions_from" and not (spec.get("field") or spec.get("fields")):
            errs.append("mentions_from needs field (or fields).")
        if spec.get("where") is not None and not isinstance(spec.get("where"), dict):
            errs.append(f"{key}.where must be an object like {{status: sent}}.")
    if "tolerance" in expect:
        try:
            parse_tolerance(expect["tolerance"])
        except ValueError as e:
            errs.append(str(e))
    for key in ("contains_any", "contains_all", "not_contains", "checked_any"):
        v = expect.get(key)
        if v is not None and (not isinstance(v, list) or not v or not all(isinstance(x, str) and x.strip() for x in v)):
            errs.append(f"{key} must be a non-empty list of words or phrases.")
    for key in ("must_mention_gap", "policy"):
        v = expect.get(key)
        if v is not None and (not isinstance(v, str) or not v.strip()):
            errs.append(f"{key} must be a word or phrase.")
    for key in ("refuses", "no_digits"):
        if key in expect and not isinstance(expect[key], bool):
            errs.append(f"{key} must be true or false.")
    return errs


def _check_from(raw: dict[str, Any], origin: str = "suite") -> tuple[Check | None, str]:
    cid = str(raw.get("id") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,59}", cid):
        return None, f"check id {cid!r} must be 2-60 lower-case letters, digits, - or _"
    question = " ".join(str(raw.get("question") or "").split())
    if not 3 <= len(question) <= 400:
        return None, f"{cid}: the question must be 3-400 characters"
    area = str(raw.get("area") or "other").strip().lower()
    if area not in AREAS:
        return None, f"{cid}: unknown area {area!r} ({', '.join(AREAS)})"
    role = str(raw.get("as") or access.OWNER).strip().lower()
    if role not in ROLES:
        return None, f"{cid}: 'as' must be owner, manager, team (an engineer) or office"
    needs = [str(n).lower() for n in raw.get("needs") or []]
    only = [str(n).lower() for n in raw.get("only_when_not_connected") or []]
    if set(needs + only) - set(NEEDS):
        return None, f"{cid}: needs / only_when_not_connected must be from {', '.join(NEEDS)}"
    expect = raw.get("expect")
    errs = validate_expect(expect)
    if errs:
        return None, f"{cid}: " + " ".join(errs)
    sensitive = bool(raw.get("sensitive", area in SENSITIVE_AREAS))
    return Check(cid, area, question, expect, role, needs, only, sensitive, origin, str(raw.get("note") or "")[:200]), ""


def load_suite(path: Path | str) -> tuple[list[Check], list[str]]:
    """(the checks, problems). A missing or broken file is a problem, never an exception."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return [], [f"No question suite at {Path(path).name}"]
    except (OSError, yaml.YAMLError) as e:
        return [], [f"The question suite could not be read: {type(e).__name__}"]
    checks: list[Check] = []
    problems: list[str] = []
    seen: set[str] = set()
    for raw in data.get("checks") or []:
        if not isinstance(raw, dict):
            problems.append("a check that is not a mapping was ignored")
            continue
        check, why = _check_from(raw)
        if check is None:
            problems.append(why)
        elif check.id in seen:
            problems.append(f"duplicate check id {check.id}")
        else:
            seen.add(check.id)
            checks.append(check)
    return checks, problems


# ------------------------------------------------------------------------------------------------------------ grading
def parse_tolerance(value: Any) -> tuple[str, float]:
    """("rel", 0.005) for "0.5%", ("abs", 2.0) for 2. Raises ValueError with a plain message."""
    if isinstance(value, bool):
        raise ValueError("tolerance must be like '0.5%' or a number")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("tolerance can't be negative")
        return "abs", float(value)
    text = str(value).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", text)
    if m:
        return "rel", float(m.group(1)) / 100
    try:
        v = float(text)
    except ValueError:
        raise ValueError("tolerance must be like '0.5%' or a number") from None
    if v < 0:
        raise ValueError("tolerance can't be negative")
    return "abs", v


def numbers_in(text: str) -> list[float]:
    """Every number in a reply: '£48,213.55', '48.2k', '£1.2 million', '4', 'four', 'no jobs' (0)."""
    t = str(text or "")
    out: list[float] = []
    for m in _NUMBER.finditer(t):
        whole, frac, unit = m.group(1).replace(",", ""), m.group(2), (m.group(3) or "").lower()
        v = float(whole + ("." + frac if frac else ""))
        if unit in ("k", "thousand", "grand"):
            v *= 1_000
        elif unit in ("m", "million"):
            v *= 1_000_000
        out.append(v)
    low = t.lower()
    for word, v in _WORDS.items():
        if re.search(rf"\b{word}\b", low):
            out.append(float(v))
    return out


def within(given: float, expected: float, tol: tuple[str, float]) -> bool:
    kind, amount = tol
    if kind == "rel":
        return abs(given - expected) <= abs(expected) * amount + 1e-9
    return abs(given - expected) <= amount + 1e-9


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", str(text or "")) if s.strip()]


def mentions_gap(reply: str, name: str) -> bool:
    """The reply names ``name`` (any of its words, e.g. 'RAM' for 'RAM Tracking') AND says it couldn't be used, in one sentence."""
    words = [w for w in re.split(r"\W+", name.lower()) if len(w) >= 3] or [name.lower()]
    for s in _sentences(reply):
        low = s.lower()
        if any(re.search(rf"\b{re.escape(w)}", low) for w in words) and _GAP_WORDS.search(s):
            return True
    return False


def is_refusal(reply: str) -> bool:
    return bool(_REFUSAL.search(str(reply or "")))


def _get(obj: Any, path: str) -> Any:
    cur = obj
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("£", "").strip())
    except ValueError:
        return None


def _matches(item: Any, where: dict[str, Any] | None) -> bool:
    if not where:
        return True
    if not isinstance(item, dict):
        return False
    for key, want in where.items():
        have = item.get(key)
        w = str(want).strip()
        m = re.fullmatch(r"(>=|<=|>|<|!=)\s*(-?\d+(?:\.\d+)?)", w)
        if m:
            h = _num(have)
            if h is None:
                return False
            op, b = m.group(1), float(m.group(2))
            if not {">": h > b, "<": h < b, ">=": h >= b, "<=": h <= b, "!=": h != b}[op]:
                return False
        elif str(have).strip().lower() != w.lower():
            return False
    return True


def _items(result: Any, spec: dict[str, Any]) -> list[Any] | None:
    base = _get(result, spec.get("path", "")) if spec.get("path") else result
    if not isinstance(base, list):
        return None
    items = [x for x in base if _matches(x, spec.get("where"))]
    if spec.get("top_by"):
        key = spec["top_by"]
        ranked = [x for x in items if isinstance(x, dict) and _num(x.get(key)) is not None]
        if not ranked:
            return []
        best = max(_num(x.get(key)) for x in ranked)
        items = [x for x in ranked if _num(x.get(key)) == best]
    return items


def unusable(result: Any) -> str:
    """Why a ground-truth result can't be used ('' = usable): sample data withheld, the FSM on demo, an error."""
    if isinstance(result, dict):
        if result.get("demo_data_withheld"):
            return "demo data"
        if result.get("demo") is True:
            return "demo data"
        if result.get("blocked_in_check_mode"):
            return "the ground-truth tool is not allowed in check mode"
        if "error" in result and result.get("kind"):
            return "demo data" if result["kind"] == "demo" else f"ground truth unavailable ({result['kind']})"
    if isinstance(result, str) and ("isn't connected" in result or "sample" in result):
        return "demo data"
    return ""


def number_truth(result: Any, spec: dict[str, Any]) -> float | None:
    op = spec.get("op", "value")
    if op == "value":
        return _num(_get(result, spec.get("path", "")))
    items = _items(result, spec)
    if items is None:
        return None
    if op == "len":
        return float(len(items))
    vals = [_num(x.get(spec["field"])) for x in items if isinstance(x, dict)]
    return float(sum(v for v in vals if v is not None))


def mention_truth(result: Any, spec: dict[str, Any]) -> list[str] | None:
    items = _items(result, spec)
    if items is None:
        return None
    fields = spec.get("fields") or [spec.get("field")]
    out: list[str] = []
    for x in items:
        if isinstance(x, dict):
            for f in fields:
                v = x.get(f)
                if isinstance(v, (str, int)) and str(v).strip() and str(v).strip() not in out:
                    out.append(str(v).strip())
    return out


@dataclass
class Grade:
    status: str
    reason: str
    expected: str = ""


def grade(check: Check, reply: str, coverage: dict[str, Any] | None, truth: dict[str, Any]) -> Grade:
    """Pass / fail with a plain reason. ``truth`` holds what the runner read for this check: 'number', 'mentions', 'policy'."""
    e = check.expect
    reply = str(reply or "")
    low = reply.lower()
    fails: list[str] = []
    expected: list[str] = []
    if "number_from" in e:
        want = truth.get("number")
        default_tol = "0" if e["number_from"].get("op") == "len" or e["number_from"].get("jarvis") else "0.5%"
        tol = parse_tolerance(e.get("tolerance", default_tol))
        expected.append(f"{want:,.2f}".rstrip("0").rstrip(".") + (f" (±{e.get('tolerance', default_tol)})" if tol[1] else ""))
        found = numbers_in(reply)
        if want is None or not any(within(g, want, tol) for g in found):
            fails.append(f"expected {expected[-1]}, found {', '.join(_fmt(g) for g in found[:6]) or 'no number'}")
    if "mentions_from" in e:
        names = truth.get("mentions") or []
        if names:
            expected.append("one of: " + ", ".join(names[:6]))
            if not any(n.lower() in low for n in names):
                fails.append("didn't name any of " + ", ".join(names[:6]))
        else:
            expected.append("says there are none")
            if not re.search(r"\b(none|no |nothing|not any|zero|0)\b", low):
                fails.append("there are none, but the reply didn't say so")
    if e.get("must_mention_gap"):
        expected.append(f"names the gap: {e['must_mention_gap']}")
        if not mentions_gap(reply, e["must_mention_gap"]):
            fails.append(f"didn't say it couldn't use {e['must_mention_gap']}")
    for phrase in e.get("contains_all") or []:
        if phrase.lower() not in low:
            fails.append(f"missing '{phrase}'")
    if e.get("contains_all"):
        expected.append("contains all of: " + ", ".join(e["contains_all"]))
    if e.get("contains_any"):
        expected.append("contains one of: " + ", ".join(e["contains_any"]))
        if not any(p.lower() in low for p in e["contains_any"]):
            fails.append("contains none of " + ", ".join(e["contains_any"]))
    for phrase in e.get("not_contains") or []:
        if phrase.lower() in low:
            fails.append(f"must not say '{phrase}'")
    if e.get("refuses") is True:
        expected.append("declines")
        if not is_refusal(reply):
            fails.append("didn't decline")
    if e.get("no_digits") and _LONG_DIGITS.search(reply):
        fails.append("contains a number that could be a code")
    if e.get("checked_any"):
        expected.append("checked: " + " or ".join(e["checked_any"]))
        checked = [c.lower() for c in (coverage or {}).get("checked") or []]
        if not any(c.startswith(s.lower()) for s in e["checked_any"] for c in checked):
            fails.append("didn't read " + " or ".join(e["checked_any"]) + f" (checked: {', '.join(checked) or 'nothing'})")
    if e.get("policy"):
        exists = bool(truth.get("policy"))
        if exists:
            expected.append(f"reads the knowledge base for the {e['policy']}")
            checked = [c.lower() for c in (coverage or {}).get("checked") or []]
            if not any(c.startswith("knowledge base") for c in checked):
                fails.append("didn't read the knowledge base")
            if _UNAVAILABLE.search(reply):
                fails.append("said it isn't available, but it is in the knowledge base")
        else:
            expected.append(f"says there is no {e['policy']} on file")
            if not _UNAVAILABLE.search(reply):
                fails.append(f"didn't say the {e['policy']} isn't available (there is none in the knowledge base)")
    if not reply.strip():
        fails.append("empty reply")
    return Grade(FAIL if fails else PASS, "; ".join(fails) or "ok", "; ".join(expected))


def _fmt(v: float) -> str:
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def excerpt(text: Any, limit: int = GIVEN_CHARS) -> str:
    t = redact_text(redact_history(" ".join(str(text or "").split())))
    return t if len(t) <= limit else t[: limit - 1] + "…"


# ------------------------------------------------------------------------------------------------------------ the runner
class QuestionChecks:
    def __init__(self, j: Any, brain_factory: Callable[[str], Any] | None = None, clock: Callable[[], datetime] | None = None):
        self.j = j
        self.brain_factory = brain_factory or self._real_brain
        self._clock = clock
        self._lock = asyncio.Lock()
        self.running = False
        self._task: asyncio.Task | None = None

    # -- time
    def now(self) -> datetime:
        return self._clock() if self._clock is not None else datetime.now(timezone.utc)

    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.j.settings.timezone)
        except Exception:  # noqa: BLE001
            return ZoneInfo("Europe/London")

    def local_today(self) -> date:
        return self.now().astimezone(self._zone()).date()

    def in_peak(self, when: datetime | None = None) -> bool:
        local = (when or self.now()).astimezone(self._zone())
        return local.weekday() < 5 and PEAK_START <= local.hour < PEAK_END

    # -- settings
    @property
    def max_questions(self) -> int:
        try:
            return max(1, min(int(getattr(self.j.settings, "question_checks_max", DEFAULT_MAX)), DEFAULT_MAX))
        except (TypeError, ValueError):
            return DEFAULT_MAX

    @property
    def timeout_s(self) -> float:
        try:
            return float(max(10, min(int(getattr(self.j.settings, "question_checks_timeout_s", DEFAULT_TIMEOUT_S)), 600)))
        except (TypeError, ValueError):
            return float(DEFAULT_TIMEOUT_S)

    # -- the suite (YAML + the owner's own checks)
    def checks(self) -> tuple[list[Check], list[str]]:
        path = getattr(self.j.settings, "question_checks_file", None)
        suite, problems = load_suite(path) if path else ([], ["No question suite configured"])
        ids = {c.id for c in suite}
        for row in self.j.db.query("SELECT * FROM question_checks_custom ORDER BY created_at"):
            try:
                raw = {"id": row["id"], "question": row["question"], "area": row["area"], "as": row["as_role"],
                       "needs": json.loads(row["needs"] or "[]"), "expect": json.loads(row["expect"] or "{}"),
                       "sensitive": bool(row["sensitive"])}
            except ValueError:
                problems.append(f"{row['id']}: its stored expectation could not be read")
                continue
            check, why = _check_from(raw, "custom")
            if check is None:
                problems.append(why)
            elif check.id not in ids:
                suite.append(check)
        return suite, problems

    def flags(self) -> dict[str, dict[str, Any]]:
        return {r["check_id"]: r for r in self.j.db.query("SELECT * FROM question_check_flags")}

    # -- demo / skip rules
    def _demo(self) -> dict[str, bool]:
        j = self.j

        def safe(fn) -> bool:
            try:
                return bool(fn())
            except Exception:  # noqa: BLE001
                return False

        return {"fsm": safe(lambda: j.fsm.demo), "sage": safe(lambda: getattr(j.finance, "demo", False)),
                "ram": safe(lambda: j.ram.demo), "mail": safe(lambda: j.mail.demo), "stock": safe(lambda: j.stores.demo)}

    def skip_reason(self, check: Check, demo: dict[str, bool], flags: dict[str, dict[str, Any]]) -> str:
        flag = flags.get(check.id)
        if flag:
            return f"marked {flag['state']} by the owner"
        if any(demo.get(n) for n in check.needs):
            return "demo data"
        if check.only_when_not_connected and not all(demo.get(n) for n in check.only_when_not_connected):
            return "only checked while " + ", ".join(check.only_when_not_connected) + " is not connected"
        return ""

    # -- ground truth (read in check mode, as the owner)
    def _args(self, args: dict[str, Any] | None) -> dict[str, Any]:
        today = self.local_today()

        def sub(v: Any) -> Any:
            if isinstance(v, str):
                def rep(m: re.Match) -> str:
                    base = {"today": today, "tomorrow": today + timedelta(days=1), "yesterday": today - timedelta(days=1)}[m.group(1)]
                    return (base + timedelta(days=int(m.group(2) or 0))).isoformat()
                return _TODAY_TOKEN.sub(rep, v)
            if isinstance(v, dict):
                return {k: sub(x) for k, x in v.items()}
            if isinstance(v, list):
                return [sub(x) for x in v]
            return v
        return sub(dict(args or {}))

    async def _read(self, spec: dict[str, Any]) -> tuple[Any, str]:
        from ..brain.tools import TOOLS_BY_NAME, dispatch

        if spec.get("jarvis"):
            fact = spec["jarvis"]
            if fact == "pending_approvals":
                return {"value": len(self.j.db.pending_actions())}, ""
            return {"value": len(self.j.db.open_suggestions())}, ""
        tool = TOOLS_BY_NAME.get(spec.get("tool", ""))
        if tool is None or not checkmode.tool_allowed(tool):
            return None, "the ground-truth tool is not allowed in check mode"
        try:
            args = tool.model.model_validate(self._args(spec.get("args")))
            token = access.current_caller.set(None)   # ground truth is read as the owner, whoever the check asks as
            try:
                result = await asyncio.wait_for(dispatch(self.j, tool, args, caller=None, check=True), self.timeout_s)
            finally:
                access.current_caller.reset(token)
        except Exception as e:  # noqa: BLE001
            return None, f"ground truth unavailable ({type(e).__name__})"
        return result, unusable(result)

    def policy_exists(self, topic: str) -> bool:
        """True when the knowledge base holds a document or section about this policy (all its words, and 'policy')."""
        words = [w for w in re.split(r"\W+", topic.lower()) if w and w != "policy"]
        kb = self.j.kb
        for doc, text in getattr(kb, "docs", {}).items():   # (the owner's private folder counts too: the owner is asking)
            head = doc.lower() + " " + " ".join(line.lower() for line in text.splitlines() if line.startswith("#"))
            if "polic" in head and all(w in head for w in words):
                return True
        return False

    async def truth_for(self, check: Check) -> tuple[dict[str, Any], str]:
        truth: dict[str, Any] = {}
        e = check.expect
        if "number_from" in e:
            spec = e["number_from"]
            result, why = await self._read(spec)
            if why:
                return truth, why
            n = number_truth(result, {"path": "value"} if spec.get("jarvis") else spec)
            if n is None:
                return truth, "ground truth unavailable (no number at that path)"
            truth["number"] = n
        if "mentions_from" in e:
            result, why = await self._read(e["mentions_from"])
            if why:
                return truth, why
            names = mention_truth(result, e["mentions_from"])
            if names is None:
                return truth, "ground truth unavailable (no list at that path)"
            truth["mentions"] = names
        if e.get("policy"):
            truth["policy"] = self.policy_exists(e["policy"])
        return truth, ""

    # -- brains
    def _real_brain(self, role: str):
        from ..brain.trace import TurnTrace
        from ..events import EventBus
        from .team_sessions import access_panels

        j = self.j
        if role == access.OWNER:
            caller = None
        elif role in (access.TEAM, access.OFFICE):   # a team member: "team" is an engineer, "office" the office kind
            caller = access.Caller(access.TEAM, "Question check", sid=f"question-check-{role}",
                                   team_role=access.OFFICE if role == access.OFFICE else access.ENGINEER)
        else:
            caller = access.Caller(role, "Question check", sid="question-check")
        team = caller is not None and caller.is_team
        bus = EventBus(check_ok=True)   # private: nothing a check says reaches a console
        if j.settings.effective_llm_backend == "max":
            from ..brain.max_backend import MaxBrain

            brain = MaxBrain(j, caller=caller, bus=bus, check=True)
        else:
            from ..brain.agent import JarvisBrain

            brain = JarvisBrain(j, caller=caller, bus=bus, check=True)
        brain.trace = TurnTrace(j, panels=access_panels() if team else None, team=team)
        bus.add_tap(brain.trace.on_event)
        return brain

    async def _ask(self, brain: Any, question: str) -> tuple[str, dict[str, Any] | None, str]:
        """(reply, coverage, problem). A fresh conversation for every question."""
        try:
            if hasattr(brain, "reset"):
                brain.reset()
            reply = await asyncio.wait_for(brain.ask(question, "typed"), self.timeout_s)
        except asyncio.TimeoutError:
            try:
                await brain.interrupt()
            except Exception:  # noqa: BLE001
                pass
            return "", None, f"timed out after {int(self.timeout_s)} s"
        except Exception as e:  # noqa: BLE001
            return "", None, f"the brain failed ({type(e).__name__})"
        extras = getattr(brain, "last_extras", None) or {}
        return str(reply or ""), extras.get("coverage"), ""

    # -- running
    def manual_runs_today(self) -> list[str]:
        try:
            stamps = json.loads(self.j.db.get_kv(MANUAL_KEY) or "[]")
        except ValueError:
            stamps = []
        cutoff = (self.now() - timedelta(hours=24)).isoformat()
        return [s for s in stamps if isinstance(s, str) and s >= cutoff]

    def last_run(self) -> dict[str, Any] | None:
        return self.j.db.query_one("SELECT * FROM question_check_runs ORDER BY id DESC LIMIT 1")

    def can_start_manual(self) -> str:
        """'' when the owner may press Run now, else why not."""
        if self.running:
            return "A check run is already going."
        if len(self.manual_runs_today()) >= MANUAL_PER_DAY:
            return f"Question checks can be run by hand at most {MANUAL_PER_DAY} times a day (they use your Claude allowance)."
        return ""

    def start_manual(self) -> dict[str, Any]:
        """Start a run in the background (the owner's Run now). Never raises."""
        why = self.can_start_manual()
        if why:
            return {"started": False, "reason": why}
        stamps = self.manual_runs_today() + [self.now().isoformat()]
        self.j.db.set_kv(MANUAL_KEY, json.dumps(stamps[-10:]))
        self.running = True   # (set now, so a second press can't start a second run before the task begins)
        self._task = asyncio.get_running_loop().create_task(self._run_safe("manual"))
        return {"started": True}

    async def _run_safe(self, trigger: str) -> None:
        try:
            await self.run(trigger)
        except Exception:  # noqa: BLE001
            log.exception("Question check run failed")
        finally:
            self.running = False

    async def scheduled(self) -> str:
        """The weekly job: does nothing unless switched on, outside peak hours, and not run in the last six days."""
        if not getattr(self.j.settings, "question_checks_enabled", False):
            return "off"
        if self.in_peak():
            self.j.activity.record("question_checks", "Question checks", "no_change", "Skipped: inside working hours.")
            return "peak"
        last = self.last_run()
        if last and last.get("started_at"):
            try:
                if self.now() - datetime.fromisoformat(last["started_at"]) < MIN_SCHEDULED_GAP:
                    return "too_soon"
            except ValueError:
                pass
        if self.running:
            return "running"
        self.running = True
        try:
            await self.run("scheduled")
        finally:
            self.running = False
        return "ran"

    async def run(self, trigger: str = "manual") -> dict[str, Any]:
        """Ask every check (up to the cap), grade it, store the scorecard. Returns the run row."""
        async with self._lock:
            self.running = True
            checks, problems = self.checks()
            flags, demo = self.flags(), self._demo()
            started = self.now().isoformat(timespec="seconds")
            run_id = self.j.db.execute(
                "INSERT INTO question_check_runs (started_at, trigger, status, note) VALUES (?,?,?,?)",
                (started, trigger, "running", "; ".join(problems)[:500]))
            counts = {PASS: 0, FAIL: 0, SKIP: 0, ERROR: 0}
            brains: dict[str, Any] = {}
            stopped = ""
            asked = 0
            try:
                for check in checks:
                    if stopped:
                        self._result(run_id, check, SKIP, stopped, counts)
                        continue
                    why = self.skip_reason(check, demo, flags)
                    if why:
                        self._result(run_id, check, SKIP, why, counts)
                        continue
                    if asked >= self.max_questions:   # cost control: at most this many questions reach the brain per run
                        self._result(run_id, check, SKIP, f"over the {self.max_questions}-question cap", counts)
                        continue
                    truth, why = await self.truth_for(check)
                    if why:
                        self._result(run_id, check, SKIP, why, counts)
                        continue
                    asked += 1
                    brain = brains.get(check.as_role)
                    if brain is None:
                        brain = brains[check.as_role] = self.brain_factory(check.as_role)
                    reply, coverage, problem = await self._ask(brain, check.question)
                    if problem:
                        self._result(run_id, check, ERROR, problem, counts)
                        continue
                    if "usage limit" in reply.lower():
                        stopped = "skipped: the Claude usage limit was reached"
                        self._result(run_id, check, SKIP, stopped, counts)
                        continue
                    g = grade(check, reply, coverage, truth)
                    self._result(run_id, check, g.status, g.reason, counts, expected=g.expected, given=reply, coverage=coverage)
            finally:
                for brain in brains.values():
                    if hasattr(brain, "close"):
                        try:
                            await brain.close()
                        except Exception:  # noqa: BLE001
                            log.warning("Closing a question-check brain failed")
                graded = counts[PASS] + counts[FAIL]
                self.j.db.execute(
                    "UPDATE question_check_runs SET finished_at = ?, status = ?, total = ?, passed = ?, failed = ?, skipped = ?,"
                    " errors = ? WHERE id = ?",
                    (self.now().isoformat(timespec="seconds"), "done", sum(counts.values()), counts[PASS], counts[FAIL],
                     counts[SKIP], counts[ERROR], run_id))
                self._prune()
                self.running = False
                try:
                    pct = f" ({round(100 * counts[PASS] / graded)}%)" if graded else ""
                    self.j.activity.record("question_checks", "Question checks", "changed" if counts[FAIL] or counts[ERROR] else "no_change",
                                           f"{counts[PASS]} of {graded} passed{pct}, {counts[SKIP]} skipped, {counts[ERROR]} errors.")
                except Exception:  # noqa: BLE001
                    pass
            return self.j.db.query_one("SELECT * FROM question_check_runs WHERE id = ?", (run_id,)) or {}

    def _result(self, run_id: int, check: Check, status: str, reason: str, counts: dict[str, int], *, expected: str = "",
                given: str = "", coverage: dict[str, Any] | None = None) -> None:
        counts[status] = counts.get(status, 0) + 1
        self.j.db.execute(
            "INSERT INTO question_check_results (run_id, check_id, area, question, as_role, status, reason, expected, given,"
            " sensitive, coverage) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, check.id, check.area, check.question, check.as_role, status, excerpt(reason, 400), excerpt(expected, 400),
             excerpt(given), int(check.sensitive), cov.line(coverage)[:400]))

    def _prune(self) -> None:
        rows = self.j.db.query("SELECT id FROM question_check_runs ORDER BY id DESC LIMIT -1 OFFSET ?", (KEEP_RUNS,))
        for r in rows:
            self.j.db.execute("DELETE FROM question_check_results WHERE run_id = ?", (r["id"],))
            self.j.db.execute("DELETE FROM question_check_runs WHERE id = ?", (r["id"],))

    # -- the scorecard
    def scorecard(self, role: str) -> dict[str, Any]:
        """What the Health drawer shows. Owner: everything. Manager: no expected/given/reason for finance or people checks and no
        candidates or controls. Team: never (the route refuses)."""
        owner = role == access.OWNER
        s = self.j.settings
        from ..humanize import cron_to_english

        checks, problems = self.checks()
        flags = self.flags()
        runs = self.j.db.query("SELECT * FROM question_check_runs WHERE status = 'done' ORDER BY id DESC LIMIT 8")
        out: dict[str, Any] = {
            "enabled": bool(getattr(s, "question_checks_enabled", False)),
            "schedule": cron_to_english(getattr(s, "question_checks_cron", "30 2 * * 0")),
            "running": self.running, "suite_size": len(checks), "problems": problems[:10] if owner else [],
            "can_run": owner, "can_mark": owner, "run_blocked": self.can_start_manual() if owner else "",
            "trend": [{"run_id": r["id"], "at": r["started_at"], "pct": _pct(r["passed"], r["failed"])} for r in reversed(runs)],
            "last_run": None, "areas": [], "failing": [], "candidates": [],
        }
        if runs:
            last = runs[0]
            rows = self.j.db.query("SELECT * FROM question_check_results WHERE run_id = ? ORDER BY id", (last["id"],))
            out["last_run"] = {"id": last["id"], "at": last["started_at"], "trigger": last["trigger"], "passed": last["passed"],
                               "failed": last["failed"], "skipped": last["skipped"], "errors": last["errors"],
                               "pct": _pct(last["passed"], last["failed"])}
            by_area: dict[str, dict[str, int]] = {}
            for r in rows:
                a = by_area.setdefault(r["area"], {"passed": 0, "failed": 0, "skipped": 0, "errors": 0})
                a[{PASS: "passed", FAIL: "failed", SKIP: "skipped", ERROR: "errors"}[r["status"]]] += 1
            out["areas"] = [{"area": k, **v, "pct": _pct(v["passed"], v["failed"])} for k, v in sorted(by_area.items())]
            for r in rows:
                if r["status"] not in (FAIL, ERROR):
                    continue
                hidden = bool(r["sensitive"]) and not owner
                out["failing"].append({
                    "check_id": r["check_id"], "area": r["area"], "question": r["question"], "as": r["as_role"], "status": r["status"],
                    "expected": "Owner only" if hidden else r["expected"], "given": "Owner only" if hidden else r["given"],
                    "reason": "Owner only" if hidden else r["reason"], "coverage": "" if hidden else r["coverage"],
                    "hidden": hidden, "flag": (flags.get(r["check_id"]) or {}).get("state", "")})
            out["skipped"] = [{"check_id": r["check_id"], "question": r["question"], "reason": r["reason"]}
                              for r in rows if r["status"] == SKIP][:60]
        if owner:
            out["candidates"] = self.candidates()
            out["flags"] = [{"check_id": k, "state": v["state"]} for k, v in flags.items()]
        return out

    # -- marking a check wrong / obsolete (owner)
    def mark(self, check_id: str, state: str, by: str = "the owner") -> dict[str, Any]:
        if state not in ("obsolete", "wrong", "clear"):
            raise ValueError("state must be obsolete, wrong or clear")
        ids = {c.id for c in self.checks()[0]}
        if check_id not in ids:
            raise KeyError(check_id)
        if state == "clear":
            self.j.db.execute("DELETE FROM question_check_flags WHERE check_id = ?", (check_id,))
        else:
            self.j.db.execute("INSERT INTO question_check_flags (check_id, state, at, by) VALUES (?,?,?,?) "
                              "ON CONFLICT(check_id) DO UPDATE SET state = excluded.state, at = excluded.at, by = excluded.by",
                              (check_id, state, now_iso(), str(by)[:80]))
        return {"check_id": check_id, "state": state}

    # -- candidates from Wrong-marked replies
    def _dismissed(self) -> set[int]:
        try:
            return {int(x) for x in json.loads(self.j.db.get_kv(DISMISSED_KEY) or "[]")}
        except (ValueError, TypeError):
            return set()

    def candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        promoted = {r["source_turn"] for r in self.j.db.query("SELECT source_turn FROM question_checks_custom WHERE source_turn IS NOT NULL")}
        dismissed = self._dismissed()
        rows = self.j.db.query(
            "SELECT f.turn_id, f.note, f.created_at, COALESCE(t.user_text, '') AS user_text, COALESCE(t.coverage, '') AS coverage"
            " FROM turn_feedback f JOIN turn_metrics t ON t.id = f.turn_id WHERE f.rating = 'wrong' ORDER BY f.id DESC LIMIT 100")
        out = []
        for r in rows:
            if r["turn_id"] in promoted or r["turn_id"] in dismissed or not r["user_text"].strip():
                continue
            c = cov.loads(r["coverage"])
            out.append({"turn_id": r["turn_id"], "question": r["user_text"], "note": r["note"], "at": r["created_at"],
                        "coverage": cov.line(c), "area": _area_of(c), "template": template_for(c)})
            if len(out) >= limit:
                break
        return out

    def dismiss_candidate(self, turn_id: int) -> None:
        ids = self._dismissed() | {int(turn_id)}
        self.j.db.set_kv(DISMISSED_KEY, json.dumps(sorted(ids)[-500:]))

    def promote(self, turn_id: int, question: str, expect: Any, area: str = "other", as_role: str = access.OWNER,
                needs: list[str] | None = None, by: str = "the owner") -> dict[str, Any]:
        """Save a real failure as a permanent check. Raises ValueError with plain words when the expectation is not valid."""
        row = self.j.db.query_one("SELECT id FROM turn_metrics WHERE id = ?", (int(turn_id),))
        if row is None:
            raise KeyError(turn_id)
        cid = f"custom-{int(turn_id)}"
        raw = {"id": cid, "question": question, "area": area, "as": as_role, "needs": needs or [], "expect": expect}
        check, why = _check_from(raw, "custom")
        if check is None:
            raise ValueError(why.split(": ", 1)[-1])
        self.j.db.execute(
            "INSERT INTO question_checks_custom (id, question, area, expect, as_role, needs, sensitive, created_at, created_by, source_turn)"
            " VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET question = excluded.question, area = excluded.area,"
            " expect = excluded.expect, as_role = excluded.as_role, needs = excluded.needs, sensitive = excluded.sensitive",
            (cid, check.question, check.area, json.dumps(check.expect), check.as_role, json.dumps(check.needs), int(check.sensitive),
             now_iso(), str(by)[:80], int(turn_id)))
        return check.public()

    # -- the doctor's line
    def doctor_line(self) -> tuple[str, str, str]:
        """(status ok|amber, line, next step)."""
        enabled = bool(getattr(self.j.settings, "question_checks_enabled", False))
        last = self.j.db.query_one("SELECT * FROM question_check_runs WHERE status = 'done' ORDER BY id DESC LIMIT 1")
        if last is None:
            if enabled:
                return "amber", "Question checks: switched on but no run yet.", "Press 'Run question checks now' in the Health drawer, or wait for the weekly run."
            return "ok", "Question checks: switched off (weekly accuracy scorecard).", "Switch them on in Settings > Schedules, or run them now from the Health drawer."
        graded = (last["passed"] or 0) + (last["failed"] or 0)
        pct = _pct(last["passed"], last["failed"])
        line = (f"Question checks: {last['passed']} of {graded} passed" + (f" ({pct}%)" if pct is not None else "")
                + f" on {str(last['started_at'])[:10]}; {last['failed']} failing, {last['skipped']} skipped, {last['errors']} errors.")
        bad = (last["failed"] or 0) + (last["errors"] or 0)
        return ("amber" if bad else "ok", line, "Open the Health drawer to see what failed and why." if bad else "")


def _pct(passed: Any, failed: Any) -> int | None:
    p, f = int(passed or 0), int(failed or 0)
    return round(100 * p / (p + f)) if p + f else None


_AREA_FROM_COVERAGE = {"money": "money", "vans_where": "vans", "vans_compliance": "vans", "stock": "stock", "operations": "jobs",
                       "email": "other", "policy": "policies"}


def _area_of(c: dict[str, Any] | None) -> str:
    for a in (c or {}).get("areas") or []:
        if a in _AREA_FROM_COVERAGE:
            return _AREA_FROM_COVERAGE[a]
    return "other"


def template_for(c: dict[str, Any] | None) -> dict[str, Any]:
    """An expectation the owner starts from: the sources the reply should have read, the gap it should have named, and a
    placeholder phrase to replace. Never a value from the reply."""
    t: dict[str, Any] = {}
    gaps = (c or {}).get("gaps") or []
    nc = next((g["source"] for g in gaps if g.get("kind") in (cov.NOT_CONNECTED, cov.WITHHELD)), None)
    if nc:
        t["must_mention_gap"] = nc
    checked = [x.split(" ")[0] if x.startswith(("Sage", "Outlook")) else ("Salts FSM" if x.startswith("Salts FSM") else x)
               for x in (c or {}).get("checked") or []]
    if checked:
        t["checked_any"] = list(dict.fromkeys(checked))[:4]
    t["contains_any"] = ["(replace with words the right answer must contain)"]
    return t
