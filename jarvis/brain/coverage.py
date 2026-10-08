"""What a reply really rests on: the "Checked / Not checked / confidence" line under an answer.

Built ONLY from what happened in the turn - the tool calls both brains made and the metadata of what each one returned
(``truncated``, a ``scope_off`` 403, the FSM's "doesn't expose this yet" 404, an owner-only refusal, a sample-data
withholding, an error or a timeout) - plus a fixed map from the words of the question to the systems such a question
needs (money -> Salts FSM and Sage, a van's whereabouts -> RAM Tracking, stock -> the stock records...). It is NEVER the
model's opinion of itself: nothing the model writes can raise the confidence.

The confidence rules (``confidence()``), in order:

* **Low** when any of: the answer relied on sample (demo) data - a source read in the turn is still showing sample data;
  a source errored or timed out and was not read successfully afterwards; a business question was answered with no
  system checked at all; two or more of the sources it needed are missing (not connected, withheld as sample data,
  switched off in the FSM, not exposed by the FSM yet, owner-only, refused, or simply not looked at).
* **Medium** when any of: a scan or list was cut short (truncated / INCOMPLETE); exactly one needed source is missing; an FSM
  document was a scan Jarvis transcribed with its own model and the reply doesn't say so.

Jarvis's own notes on a customer or site ("Jarvis's notes on Acme", services/entity_memory.py) are listed as NOTES, never as a
checked system: they can't satisfy a question's need for a source, so they never raise the confidence by themselves. Caveats that
don't change the rules (items the FSM masked in a document) are listed too.
* **High** otherwise: every source it needed was read, real and complete.

What is stored with the transcript row (``as_stored()``) is labels and counts only - source names, resource names, the
kind of gap, "scanned 50,000 of 64,200 rows" - never a value, a figure from the data, a name from a row, or a secret.
Nothing here writes, sends or approves anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

HIGH, MEDIUM, LOW = "High", "Medium", "Low"
VERSION = 1

FSM = "Salts FSM"
SAGE = "Sage"
RAM = "RAM Tracking"
MAIL = "Outlook"          # the trace's name for the owner's mailbox
STOCK = "Stock records"
REGISTER = "Accreditations register"
KB = "Knowledge base"

# How a source is named on the coverage line (the trace's own names stay as they are for the source line above it).
DISPLAY = {MAIL: "your mailbox"}

# The demo_guard source key -> the source name used here (a withheld tool result names its sources by demo_guard key label).
DEMO_SOURCE = {"accounts": SAGE, "stock": STOCK, "vehicles": RAM, "staff": "Staff register", "socials": "Google and socials"}
_DEMO_LABEL_TO_SOURCE = {"the accounts (Sage)": SAGE, "the stock records": STOCK, "RAM Tracking": RAM,
                         "the staff register": "Staff register",
                         "the social media and Google review figures": "Google and socials"}

# What part of a source a tool reads ("Salts FSM jobs", "Sage aged debt"). The FSM data tools name their resource instead.
TOOL_DETAIL = {
    "fsm_jobs": "jobs", "job_detail": "jobs", "staff_today": "engineers", "staff_productivity": "engineers",
    "staff_overdue_jobs": "overdue jobs", "attendance_check": "attendance", "lone_worker_check": "jobs",
    "staff_review": "engineers", "office_productivity": "office", "fsm_systems_due": "systems due",
    "ppm_schedule_plan": "maintenance plan", "fsm_contracts_renewing": "contracts", "contract_renewals": "contracts",
    "fsm_quotes": "quotes", "remedial_quotes": "quotes", "staff_certifications": "certifications",
    "unbilled_jobs": "jobs", "out_of_hours_calls": "out-of-hours calls", "timesheet_check": "timesheets",
    "false_alarm_analysis": "false alarms", "upsell_opportunities": "upsell opportunities", "customer_health": "customers",
    "finance_snapshot": "accounts", "finance_aged": "aged debt", "finance_vat": "VAT", "finance_cashflow": "cash flow",
    "finance_corporation_tax": "corporation tax", "finance_profit_and_loss": "profit and loss",
    "finance_credit_control": "credit control", "finance_deadlines": "deadlines",
    "customer_balance": "invoices (one customer)",
}
# customer_balance (services/customer_balance.py): its own refusal kinds. A name that matched nothing / several customers is the
# question's input, not a gap in the data; an engineer asking is a refusal.
_BALANCE_KIND = {"not_found": "bad_input", "bad_request": "bad_input", "ambiguous": "bad_input", "office_only": "refused"}
FSM_DATA_TOOLS = ("fsm_data", "fsm_analyse")
DOC_TOOL = "fsm_document_read"

# ------------------------------------------------------------------------------------------------ what a question needs
@dataclass(frozen=True)
class Area:
    name: str
    pattern: re.Pattern
    groups: tuple[tuple[str, ...], ...]   # every group is needed; a group is met by reading ANY source in it


def _rx(words: str) -> re.Pattern:
    return re.compile(r"\b(?:" + words + r")\b", re.I)


AREAS: tuple[Area, ...] = (
    Area("money", _rx(r"invoices?|invoiced|invoicing|overdue invoices?|owed?|owing|debts?|debtors?|creditors?|paid|unpaid|payments?|"
                      r"revenue|turnover|profits?|margins?|cash(?:flow)?|vat|sales|income|spend|spent|costs?|credit control|"
                      r"aged debt|bank balance|wages?|payroll|salar(?:y|ies)|p&l|profit and loss|corporation tax"),
         ((FSM,), (SAGE,))),
    Area("vans_where", _rx(r"where(?:'s| is| are)?\b.*\b(?:van|vans|vehicle|vehicles|engineer|engineers)|van locations?|"
                           r"nearest engineer|who(?:'s| is) closest|moving|parked|at home"),
         ((RAM,),)),
    Area("vans_compliance", _rx(r"mot|road tax|vehicle tax|van service|vans?|vehicles?|fleet|calibration|ladders?|test kit"),
         ((FSM, REGISTER, RAM),)),
    Area("stock", _rx(r"stock|inventory|reorder|re-order|parts|materials|stores"), ((STOCK, FSM),)),
    Area("operations", _rx(r"jobs?|engineers?|quotes?|quoted|contracts?|renewals?|renewing|sites?|customers?|visits?|"
                           r"service due|maintenance|ppm|upsells?|systems? due|overrunning|running late|overdue jobs?|booked"),
         ((FSM,),)),
    Area("email", _rx(r"emails?|inbox|mail|replied|reply from"), ((MAIL,),)),
    Area("policy", _rx(r"polic(?:y|ies)|procedures?|handbook|method statement"), ((KB,),)),
)
TEAM_SOURCES = frozenset({FSM, RAM, KB})   # what a team session can ever read: a team turn is never "missing" Sage or mail

# Per-call statuses and what they mean for the turn.
OK, PARTIAL, DEMO = "ok", "partial", "demo"
WITHHELD, NOT_CONNECTED, SCOPE_OFF, NOT_EXPOSED, OWNER_ONLY = "withheld", "not_connected", "scope_off", "not_exposed", "owner_only"
ERROR, TIMEOUT, RATE_LIMITED = "error", "timeout", "rate_limited"
REFUSED, BLOCKED, BAD_INPUT = "refused", "blocked", "bad_input"
TRANSCRIBED = "transcribed"   # an FSM document that was a scan, transcribed by Jarvis's own model (may be misread)
READ = frozenset({OK, PARTIAL, DEMO, TRANSCRIBED})        # the source was actually read
MISSING = frozenset({WITHHELD, NOT_CONNECTED, SCOPE_OFF, NOT_EXPOSED, OWNER_ONLY, REFUSED, BLOCKED})
FAILED = frozenset({ERROR, TIMEOUT, RATE_LIMITED})

REASON = {WITHHELD: "not connected - sample data withheld", NOT_CONNECTED: "not connected", SCOPE_OFF: "switched off in the FSM",
          NOT_EXPOSED: "the FSM doesn't expose this yet", OWNER_ONLY: "owner only", REFUSED: "not available here",
          BLOCKED: "blocked in check mode", ERROR: "error", TIMEOUT: "timed out", RATE_LIMITED: "rate limited",
          DEMO: "sample data", "not_checked": "not checked"}

_SAID_SCAN = re.compile(r"\bscan|transcri|photo|hand-?written|may be misread", re.I)
_UPSTREAM = re.compile(r"unavailable|not reachable|couldn'?t reach|could not reach|timed? ?out|timeout|connection|"
                       r"server error|not answering|\b5\d\d\b|refused the key|rate limit", re.I)
_FSM_KIND = {"demo": NOT_CONNECTED, "scope_off": SCOPE_OFF, "unavailable": NOT_EXPOSED, "owner_only": OWNER_ONLY,
             "rate_limited": RATE_LIMITED, "not_found": BAD_INPUT, "bad_request": BAD_INPUT}


def display(source: str) -> str:
    return DISPLAY.get(source, source)


def _fact(source: str, status: str, detail: str = "", note: str = "") -> dict[str, str]:
    out = {"src": source, "status": status}
    if detail:
        out["detail"] = str(detail)[:60]
    if note:
        out["note"] = str(note)[:120]
    return out


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _truncation_note(result: dict[str, Any]) -> str:
    """"scanned 50,000 of 64,200 rows" / "showing 120 of 900 rows" - counts only."""
    scanned, total = _int(result.get("rows_scanned")), _int(result.get("rows_matching_in_fsm"))
    if scanned is not None:
        return f"scanned {scanned:,} of {total:,} rows" if total is not None else f"scanned {scanned:,} rows, stopped early"
    shown, total = _int(result.get("returned")), _int(result.get("total"))
    if shown is not None:
        return f"showing {shown:,} of {total:,} rows" if total is not None else f"showing {shown:,} rows, more not read"
    return "only part of the data was read"


def _stubs(value: Any, depth: int = 0) -> list[str]:
    """Labels of sample-data sections a composite tool (briefing, wrap-up, advice) replaced with demo_guard.stub()."""
    found: list[str] = []
    if depth > 3:
        return found
    if isinstance(value, dict):
        nc = value.get("not_connected")
        if isinstance(nc, list) and "error" in value:
            found += [str(x) for x in nc if isinstance(x, str)]
        for v in value.values():
            if isinstance(v, (dict, list)):
                found += _stubs(v, depth + 1)
    elif isinstance(value, list):
        for v in value[:50]:
            found += _stubs(v, depth + 1)
    return found


def tool_sources(name: str) -> tuple[str, ...]:
    from .trace import tool_info

    short = name.removeprefix("mcp__jarvis__")
    if short in FSM_DATA_TOOLS or short == DOC_TOOL:
        return (FSM,)
    return tool_info(short)[0]


def call_facts(name: str, args: Any, result: Any) -> list[dict[str, str]]:
    """What one finished tool call tells us about the sources it read. [] for a tool that reads no business source."""
    short = name.removeprefix("mcp__jarvis__")
    if isinstance(result, dict) and result.get("blocked_in_check_mode"):
        return [_fact(s, BLOCKED) for s in tool_sources(short)] or [_fact(short, BLOCKED)]
    if isinstance(result, dict) and result.get("demo_data_withheld"):
        out = []
        for item in result.get("not_connected") or []:
            label = item.get("source") if isinstance(item, dict) else None
            out.append(_fact(_DEMO_LABEL_TO_SOURCE.get(str(label), str(label or "a source")), WITHHELD))
        return out or [_fact(s, WITHHELD) for s in tool_sources(short)]
    sources = tool_sources(short)
    if not sources:
        return []
    a = args.model_dump() if hasattr(args, "model_dump") else (args if isinstance(args, dict) else {})
    if short == DOC_TOOL:
        return _document_facts(a, result)
    detail = str(a.get("resource") or "") if short in FSM_DATA_TOOLS else TOOL_DETAIL.get(short, "")
    if isinstance(result, str):
        if "isn't available to you here" in result and "team version" in result:
            return [_fact(s, REFUSED, detail) for s in sources]
        return [_fact(s, OK, detail) for s in sources]
    if not isinstance(result, dict):
        return [_fact(s, OK, detail) for s in sources]
    if "error" in result and result.get("kind") and short == "customer_balance":
        kind = str(result["kind"])
        return [_fact(s, _BALANCE_KIND.get(kind) or _FSM_KIND.get(kind, ERROR), detail,
                      str(result.get("group") or "") if kind == "scope_off" else "") for s in sources]
    if "error" in result and result.get("kind"):
        status = _FSM_KIND.get(str(result["kind"]), ERROR)
        if result["kind"] == "not_found" and "did_you_mean" not in result:
            status = NOT_EXPOSED   # the catalog lists it but the FSM answers 404 for its rows: not served yet (not a typo)
        note = str(result.get("group") or "") if status == SCOPE_OFF else ""
        detail = str(result.get("resource") or detail)
        return [_fact(s, status, detail, note) for s in sources]
    if "error" in result and isinstance(result.get("error"), str) and not result.get("not_connected"):
        status = ERROR if _UPSTREAM.search(result["error"]) else BAD_INPUT
        return [_fact(s, status, detail) for s in sources]
    out: list[dict[str, str]] = []
    if result.get("truncated") is True:
        out += [_fact(s, PARTIAL, detail, _truncation_note(result)) for s in sources]
    elif result.get("demo") is True:
        out += [_fact(s, DEMO, detail) for s in sources]
    else:
        out += [_fact(s, OK, detail) for s in sources]
    for label in _stubs(result):
        out.append(_fact(_DEMO_LABEL_TO_SOURCE.get(label, label), WITHHELD))
    return out


def _document_facts(a: dict[str, Any], result: Any) -> list[dict[str, str]]:
    """fsm_document_read: the document's name is the part read; a transcribed scan, a cut-short text and masked items are said."""
    if not isinstance(result, dict):
        return [_fact(FSM, OK, "document")]
    name = str(result.get("name") or result.get("document_id") or a.get("document_id") or "").strip()
    detail = f"document '{name[:40]}'" if name else "document"
    if result.get("ambiguous") or ("error" in result and result.get("kind") in ("not_found", "bad_request")):
        return [_fact(FSM, BAD_INPUT, detail)]   # nothing was read yet: it asked which one / found nothing to read
    if "error" in result and result.get("kind"):
        return [_fact(FSM, _FSM_KIND.get(str(result["kind"]), ERROR), detail)]
    masked = _int(result.get("masked_by_fsm")) or 0
    note = f"{masked} item{'' if masked == 1 else 's'} masked by the FSM" if masked > 0 else ""
    if result.get("transcribed"):
        status = TRANSCRIBED
    elif result.get("truncated"):
        status, note = PARTIAL, "only part of the document was read" + (f"; {note}" if note else "")
    else:
        status = OK
    return [_fact(FSM, status, detail, note)]


def error_facts(name: str, exc: BaseException) -> list[dict[str, str]]:
    """A tool call that raised: its sources errored (or timed out)."""
    timeout = isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()
    short = name.removeprefix("mcp__jarvis__")
    return [_fact(s, TIMEOUT if timeout else ERROR, TOOL_DETAIL.get(short, "")) for s in tool_sources(short)]


# ------------------------------------------------------------------------------------------------ the turn
def areas_of(text: str) -> list[Area]:
    t = str(text or "")
    return [a for a in AREAS if a.pattern.search(t)]


def _label(source: str, detail: str = "") -> str:
    name = display(source)
    return f"{name} {detail}".strip() if detail else name


def _needed_groups(areas: Iterable[Area], team: bool) -> list[tuple[str, ...]]:
    groups: list[tuple[str, ...]] = []
    for a in areas:
        for g in a.groups:
            if team:
                g = tuple(s for s in g if s in TEAM_SOURCES)
                if not g:
                    continue
            if g not in groups:
                groups.append(g)
    return groups


def confidence(*, relied_on_demo: bool, failed: int, missing: int, truncated: bool, business: bool, read_any: bool,
               transcribed_unsaid: bool = False) -> tuple[str, str]:
    """(confidence, why) by the explicit rules in the module docstring."""
    if relied_on_demo:
        return LOW, "Part of this answer came from sample (demo) data."
    if failed:
        return LOW, "A source gave an error or timed out."
    if business and not read_any:
        return LOW, "No system was checked for a business question."
    if missing >= 2:
        return LOW, "Two or more of the sources it needed weren't checked."
    if truncated:
        return MEDIUM, "Only part of the data was read."
    if missing == 1:
        return MEDIUM, "One source it needed wasn't checked."
    if transcribed_unsaid:
        return MEDIUM, "A document was a scan transcribed by Jarvis, and the answer doesn't say so."
    return HIGH, "Everything it needed was read, real and complete."


def summarise(facts: list[dict[str, str]], user_text: str = "", *, demo: dict[str, bool] | None = None, team: bool = False,
              tools_used: int = 0, notes: list[str] | None = None, reply: str = "") -> dict[str, Any] | None:
    """The coverage of one turn, or None when there is nothing to say (no source read, no business question, no notes used).
    ``notes``: the "Jarvis's notes on X" labels the turn leaned on (listed, never counted as a checked system). ``reply``: what was
    said - only to see whether a transcribed scan was owned up to."""
    demo = demo or {}
    areas = areas_of(user_text)
    used_notes = list(dict.fromkeys(" ".join(str(n).split())[:80] for n in (notes or []) if str(n).strip()))[:8]
    if not facts and not areas and not used_notes:
        return None
    caveats: list[str] = []
    transcribed_unsaid = False
    for f in facts:
        lab = _label(f["src"], f.get("detail", ""))
        if f["status"] == TRANSCRIBED:
            said = bool(_SAID_SCAN.search(str(reply or "")))
            transcribed_unsaid = transcribed_unsaid or not said
            text = f"{lab} (transcribed scan - may be misread{'' if said else '; the answer doesn' + chr(39) + 't say so'})"
            if text not in caveats:
                caveats.append(text)
        if f.get("note") and "masked by the FSM" in f["note"]:
            m = re.search(r"\d+ items? masked by the FSM", f["note"])
            text = f"{lab} ({m.group(0) if m else f['note']})"
            if text not in caveats:
                caveats.append(text)
    checked: list[str] = []
    read: set[str] = set()
    partial: dict[str, list[str]] = {}
    demo_used: list[str] = []
    for f in facts:
        src, status, detail = f["src"], f["status"], f.get("detail", "")
        if status in READ:
            read.add(src)
            lab = _label(src, detail)
            if lab not in checked:
                checked.append(lab)
            if status == PARTIAL:
                partial.setdefault(lab, []).append(f.get("note", ""))
            if status == DEMO or demo.get(src):
                if src not in demo_used:
                    demo_used.append(src)
    gaps: list[dict[str, str]] = []
    seen: set[str] = set()

    def gap(source: str, kind: str, text: str) -> None:
        if text not in seen:
            seen.add(text)
            gaps.append({"source": display(source), "kind": kind, "text": text})

    for src in demo_used:
        gap(src, DEMO, f"{display(src)} ({REASON[DEMO]})")
    for lab, notes in partial.items():
        gap(lab, "truncated", f"{lab} ({next((n for n in notes if n), 'only part was read')})")
    # A gap is judged per PART of a source ("Salts FSM invoices"): reading the FSM's jobs doesn't make up for its invoices being
    # switched off, but an error on the jobs followed by a good read of the jobs is recovered. A gap with no part named is
    # recovered by any good read of that source.
    read_labels = {_label(f["src"], f.get("detail", "")) for f in facts if f["status"] in READ}
    missing_sources: list[str] = []    # labels
    failed_sources: list[str] = []     # labels
    gap_srcs: set[str] = set()         # the sources those gaps belong to
    for f in facts:
        src, status, detail = f["src"], f["status"], f.get("detail", "")
        lab = _label(src, detail)
        if status in READ or status == BAD_INPUT or lab in read_labels or (not detail and src in read):
            continue
        if status in FAILED:
            if lab not in failed_sources:
                failed_sources.append(lab)
            gap_srcs.add(src)
            gap(src, status, f"{lab} ({REASON[status]})")
        elif status in MISSING:
            if lab not in missing_sources:
                missing_sources.append(lab)
            gap_srcs.add(src)
            reason = REASON[status] + (f": {f['note']}" if status == SCOPE_OFF and f.get("note") else "")
            gap(src, status, f"{lab} ({reason})")
    for group in _needed_groups(areas, team):
        if any(s in read for s in group):
            continue
        if any(s in gap_srcs for s in group):
            continue  # already named above, with the real reason
        first = group[0]
        not_connected = all(demo.get(s) for s in group)
        missing_sources.append(first)
        gap(first, NOT_CONNECTED if not_connected else "not_checked",
            f"{display(first)} ({REASON[NOT_CONNECTED] if not_connected else REASON['not_checked']})")
    business = bool(areas)
    level, why = confidence(relied_on_demo=bool(demo_used), failed=len(failed_sources), missing=len(missing_sources),
                            truncated=bool(partial), business=business, read_any=bool(read), transcribed_unsaid=transcribed_unsaid)
    out = {"v": VERSION, "checked": checked[:12], "gaps": gaps[:12], "confidence": level, "why": why,
           "areas": [a.name for a in areas]}
    if caveats:
        out["caveats"] = caveats[:8]
    if used_notes:
        out["notes"] = used_notes
    return out


def line(cov: dict[str, Any] | None) -> str:
    """'Checked: Salts FSM invoices · Not checked: Sage (not connected) · Medium' (the collapsed line)."""
    if not cov:
        return ""
    parts = ["Checked: " + (", ".join(cov.get("checked") or []) or "nothing")]
    if cov.get("gaps"):
        parts.append("Not checked: " + ", ".join(g["text"] for g in cov["gaps"]))
    if cov.get("caveats"):
        parts.append("Caveats: " + ", ".join(cov["caveats"]))
    if cov.get("notes"):
        parts.append("Notes: " + ", ".join(cov["notes"]))
    parts.append(str(cov.get("confidence", "")))
    return " · ".join(parts)


def spoken(cov: dict[str, Any] | None, reply: str = "") -> str:
    """One short sentence to say after a SPOKEN reply, only when confidence is Low and the reply didn't already name the
    gap. '' otherwise."""
    if not cov or cov.get("confidence") != LOW:
        return ""
    gaps = cov.get("gaps") or []
    said = str(reply or "").lower()
    if not gaps:
        return "" if "check" in said else "I didn't check any system for that."
    g = next((x for x in gaps if x["kind"] in (DEMO, ERROR, TIMEOUT, RATE_LIMITED, NOT_CONNECTED, WITHHELD)), gaps[0])
    name = g["source"]
    if name.lower().split(" ")[0] in said and any(w in said for w in ("connect", "sample", "couldn't", "can't", "error",
                                                                        "not checked", "didn't check", "unable")):
        return ""
    kind = g["kind"]
    if kind == DEMO:
        return f"That used sample data from {name}, not your real figures."
    if kind in (NOT_CONNECTED, WITHHELD):
        return f"I couldn't check {name}, it isn't connected."
    if kind in (ERROR, TIMEOUT, RATE_LIMITED):
        return f"I couldn't check {name}, it didn't answer properly."
    if kind == SCOPE_OFF:
        return f"I couldn't check {name}, that part is switched off in the FSM."
    if kind == NOT_EXPOSED:
        return f"I couldn't check {name}, the FSM doesn't expose it yet."
    if kind == "not_checked":
        return f"I didn't check {name} for that."
    return f"I couldn't check {name}."


def turn_note(user_text: str, demo: dict[str, bool], team: bool = False) -> str:
    """A line for the model BEFORE it answers, naming the sources this question needs that aren't connected, so the answer
    itself names the gap instead of overclaiming. '' when there is nothing to say."""
    out: list[str] = []
    for group in _needed_groups(areas_of(user_text), team):
        if all(demo.get(s) for s in group):
            name = display(group[0])
            if name not in out:
                out.append(name)
    if not out:
        return ""
    names = out[0] if len(out) == 1 else ", ".join(out[:-1]) + " and " + out[-1]
    return (f"[Coverage: this question needs {names}, which {'is' if len(out) == 1 else 'are'} not connected. Never state a "
            "figure as certain while a source it needs is missing - name the gap in your answer.]\n")


def demo_map(j: Any) -> dict[str, bool]:
    """Which sources are still sample data right now. Defensive: anything unexpected means 'not demo'."""
    from .trace import _DEMO

    out: dict[str, bool] = {}
    for name, check in _DEMO.items():
        try:
            out[name] = bool(check(j))
        except Exception:  # noqa: BLE001
            out[name] = False
    return out


def as_stored(cov: dict[str, Any] | None) -> str:
    """The JSON kept with the transcript / metrics row: the summary only (labels, kinds, counts), never a spoken line."""
    import json

    if not cov:
        return ""
    keep = {k: cov[k] for k in ("v", "checked", "gaps", "caveats", "notes", "confidence", "why", "areas") if k in cov}
    return json.dumps(keep, ensure_ascii=False)[:4000]


def loads(text: Any) -> dict[str, Any] | None:
    import json

    if not text:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None
