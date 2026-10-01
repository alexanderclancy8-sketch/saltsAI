"""False alarm log and analysis.

BS 5839-1:2025 expects every false alarm on a fire detection and alarm system to be logged, investigated and
reviewed, with the corrective action evidenced. This service helps Salts show that:

* It finds false-alarm and repeat call-outs in Salts FSM jobs, per site and per system (read-only - nothing is
  written to Salts FSM from here).
* It flags repeat offenders: a site/system with at least `repeat_threshold` false alarms in the period. The
  threshold is Salts' own configurable trigger for a closer look, not a figure taken from the standard.
* It keeps a local log of the cause and corrective action for each event (`false_alarm_log` table). Writing to
  that log is a tool that needs the owner's approval, like every other record Jarvis changes.
* It drafts an audit-ready evidence report per site, listing every event with its cause, action, evidence and
  review - and, just as importantly, the gaps still open. It is a draft for a competent person to check and
  sign; check clause references against your own copy of the standard.

How a false alarm is recognised: a call-out job (or any job explicitly flagged) whose type, or any text field
FSM sends with it (description, notes, outcome...), says so - "false alarm", "unwanted fire signal", "UFS",
"nuisance activation", "no fire found" etc. - unless the wording is negated ("not a false alarm"). Anything FSM
doesn't mark that way can still be logged by hand with `false_alarm_record`, and then counts as a false alarm.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterator

log = logging.getLogger(__name__)

DEFAULT_DAYS = 365
DEFAULT_REPEAT_THRESHOLD = 2
RECORD_LOOKBACK_DAYS = 400  # how far back a job is searched for when a cause is recorded against it

CAUSE_CATEGORIES = ("environmental", "equipment_fault", "accidental_damage", "malicious", "good_intent",
                    "cooking_steam_dust", "testing_or_maintenance", "installation_or_design", "unknown")

_FALSE_ALARM_RE = re.compile(
    r"false\s+alarm|unwanted\s+(?:fire\s+)?(?:alarm|signal|activation)|\bufs\b|nuisance\s+(?:alarm|activation)|"
    r"spurious\s+(?:alarm|activation)|(?:accidental(?:ly)?|inadvertent(?:ly)?)\s+(?:activat\w*|trigger\w*|set\s+off)|"
    r"no\s+fire\s+(?:found|present)|no\s+evidence\s+of\s+fire", re.I)
_NEGATION_RE = re.compile(r"\b(?:not?|without)\s+(?:a\s+|an\s+|any\s+)?$", re.I)
_CALLOUT_TYPE_RE = re.compile(r"call[\s_-]?out|reactive|emergency|false", re.I)
_FLAG_KEYS = {"falsealarm", "isfalsealarm"}

# Checked in order: the specific systems first, fire alarm last (its words are the most common).
_SYSTEM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("emergency_lighting", re.compile(r"emergency\s+light|escape\s+light|exit\s+sign|luminaire", re.I)),
    ("intruder", re.compile(r"intruder|burglar|panic\s+alarm|hold[\s-]?up|texecom|pyronix", re.I)),
    ("cctv", re.compile(r"cctv|camera|\bnvr\b|\bdvr\b", re.I)),
    ("access_control", re.compile(r"access\s+control|door\s+entry|paxton|mag\s?lock|\bfob\b", re.I)),
    ("fire_alarm", re.compile(r"fire\s+alarm|smoke|heat\s+detector|detector|call\s*point|sounder|beacon|vigilon|"
                              r"kentec|fire\s+panel|unwanted\s+fire|\bufs\b|\bfire\b", re.I)),
)


def _norm_key(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def _strings(obj: Any, depth: int = 0) -> Iterator[str]:
    """Every text value inside an FSM job's extra fields (notes can arrive as nested lists/dicts)."""
    if depth > 4:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v, depth + 1)


def _mentions_false_alarm(text: str) -> bool:
    for m in _FALSE_ALARM_RE.finditer(text):
        if not _NEGATION_RE.search(text[max(0, m.start() - 20):m.start()]):
            return True
    return False


def _explicit_flag(extra: Any) -> bool:
    if not isinstance(extra, dict):
        return False
    for k, v in extra.items():
        if re.sub(r"[^a-z]", "", str(k).lower()) in _FLAG_KEYS and str(v).strip().lower() in ("true", "yes", "y", "1"):
            return True
    return False


def _type_from_text(text: str) -> str | None:
    for stype, pattern in _SYSTEM_PATTERNS:
        if pattern.search(text):
            return stype
    return None


def _system_type(system: dict[str, Any]) -> str:
    return re.sub(r"\s+", "_", _norm_key(system.get("type")))


def _job_ref(job: dict[str, Any]) -> str:
    return str(job.get("ref") or job.get("id") or "").strip()


def _event_date(job: dict[str, Any]) -> str:
    for key in ("started_at", "scheduled_start", "created_at"):
        value = job.get(key)
        if value:
            try:
                return date.fromisoformat(str(value)[:10]).isoformat()
            except ValueError:
                continue
    return ""


def _resolve_system(job: dict[str, Any], text: str, site_systems: list[dict[str, Any]]) -> dict[str, str]:
    """Which system the job was about. Uses an explicit system id/type if FSM sent one, else the wording of the
    job. If the site has exactly one system of that type it's named; if several and none is identified, only the
    type is given - never a guess at which panel."""
    explicit_id = explicit_val = ""
    extra = job.get("extra") if isinstance(job.get("extra"), dict) else {}
    for k, v in extra.items():
        kn = re.sub(r"[^a-z]", "", str(k).lower())
        if kn == "systemid" and isinstance(v, (str, int)):
            explicit_id = str(v).strip()
        elif kn in ("system", "systemtype", "systemname") and isinstance(v, (str, int)):
            explicit_val = str(v).strip()
    chosen = next((s for s in site_systems if str(s.get("id") or "").lower() in
                   (explicit_id.lower(), explicit_val.lower()) and s.get("id")), None)
    stype = _system_type(chosen) if chosen else None
    if not stype:
        stype = _type_from_text(explicit_val) or _type_from_text(text)
    if not stype:
        return {"system": "Not identified", "system_id": "", "system_type": "unknown"}
    if chosen is None:
        same_type = [s for s in site_systems if _system_type(s) == stype]
        chosen = same_type[0] if len(same_type) == 1 else None
    type_label = stype.replace("_", " ")
    if chosen is None:
        return {"system": type_label, "system_id": "", "system_type": stype}
    detail = " - ".join(str(x) for x in (type_label, chosen.get("make_model")) if x)
    return {"system": f"{detail} ({chosen['id']})" if chosen.get("id") else detail,
            "system_id": str(chosen.get("id") or ""), "system_type": stype}


def _gaps(record: dict[str, Any] | None) -> list[str]:
    rec = record or {}
    gaps = []
    if not rec.get("cause"):
        gaps.append("cause not investigated/recorded")
    if not rec.get("corrective_action"):
        gaps.append("corrective action not recorded")
    elif not (rec.get("action_done_date") or rec.get("evidence_ref")):
        gaps.append("corrective action not evidenced (no completion date or evidence reference)")
    if not (rec.get("reviewed_by") and rec.get("review_date")):
        gaps.append("review not signed off")
    return gaps


def build_events(jobs: list[dict[str, Any]], systems: list[dict[str, Any]],
                 records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One event per call-out job (and per job flagged/logged as a false alarm), oldest first."""
    by_site: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in systems:
        by_site[_norm_key(s.get("site"))].append(s)
    events = []
    for job in jobs:
        if "cancel" in str(job.get("status") or "").lower():
            continue
        ref = _job_ref(job)
        jtype = str(job.get("type") or "")
        is_callout = bool(_CALLOUT_TYPE_RE.search(jtype))
        explicit = _explicit_flag(job.get("extra"))
        text = " ".join([jtype, *_strings(job.get("extra"))]).replace("_", " ")  # "false_alarm" job types
        record = records.get(ref.lower()) if ref else None
        detected = explicit or (is_callout and _mentions_false_alarm(text))
        if not (is_callout or detected or record):
            continue
        site = str(job.get("site") or "").strip() or "Unknown site"
        sysinfo = _resolve_system(job, text, by_site.get(_norm_key(site), []))
        is_false = detected or record is not None
        events.append({
            "job_ref": ref, "job_id": str(job.get("id") or ""), "date": _event_date(job) or (record or {}).get("event_date", ""), "site": site,
            "customer": job.get("customer") or "", "engineer": job.get("engineer") or "", "job_type": jtype,
            "status": job.get("status") or "", **sysinfo, "is_false_alarm": is_false,
            "source": ("logged by hand" if record and not detected else "detected from the job") if is_false else "",
            "record": record, "gaps": _gaps(record) if is_false else [],
        })
    events.sort(key=lambda e: (e["date"], e["job_ref"]))
    return events


def _avg_gap_days(dates: list[str]) -> float | None:
    ds = []
    for d in dates:
        try:
            ds.append(date.fromisoformat(d[:10]))
        except ValueError:
            continue
    ds.sort()
    if len(ds) < 2:
        return None
    return round(sum((b - a).days for a, b in zip(ds, ds[1:])) / (len(ds) - 1), 1)


def _site_rollup(site: str, events: list[dict[str, Any]], threshold: int) -> dict[str, Any]:
    by_system: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_system[e["system_id"] or e["system"]].append(e)
    systems = []
    for evs in by_system.values():
        fa = [e for e in evs if e["is_false_alarm"]]
        flags = []
        if len(fa) >= threshold:
            flags.append(f"repeat false alarms ({len(fa)} in the period)")
        if len(evs) >= threshold:
            flags.append(f"repeat call-outs ({len(evs)} in the period)")
        systems.append({"system": evs[0]["system"], "system_id": evs[0]["system_id"],
                        "system_type": evs[0]["system_type"], "callouts": len(evs), "false_alarms": len(fa),
                        "first": evs[0]["date"], "last": evs[-1]["date"],
                        "avg_days_between_callouts": _avg_gap_days([e["date"] for e in evs]),
                        "repeat_false_alarms": len(fa) >= threshold, "repeat_callouts": len(evs) >= threshold,
                        "flags": flags})
    systems.sort(key=lambda s: (-s["false_alarms"], -s["callouts"], s["system"]))
    fa_events = [e for e in events if e["is_false_alarm"]]
    open_gaps = sum(len(e["gaps"]) for e in fa_events)
    flags = []
    if len(fa_events) >= threshold:
        flags.append(f"repeat offender: {len(fa_events)} false alarms in the period")
    if len(events) >= threshold:
        flags.append(f"repeat call-outs: {len(events)} in the period")
    return {"site": site, "customer": next((e["customer"] for e in events if e["customer"]), ""),
            "callouts": len(events), "false_alarms": len(fa_events),
            "repeat_offender": len(fa_events) >= threshold, "repeat_callouts": len(events) >= threshold,
            "flags": flags, "systems": systems, "events_with_open_gaps": sum(1 for e in fa_events if e["gaps"]),
            "open_gaps": open_gaps, "events": events}


async def collect(fsm, db, days: int, today: date) -> dict[str, Any]:
    start = today - timedelta(days=days)
    notes: list[str] = []
    jobs = await fsm.jobs(start, today)
    try:
        systems = await fsm.systems()
    except Exception as e:  # noqa: BLE001 - analysis still useful without the system list
        log.warning("false alarm analysis: couldn't read systems: %s", e)
        systems = []
        notes.append("Salts FSM systems couldn't be read, so events are matched to a system type only.")
    records = {str(r["job_ref"]).lower(): r for r in (db.list_false_alarm_records() if db else [])}
    return {"events": build_events(jobs, systems, records), "notes": notes, "start": start}


async def analyse(fsm, db, days: int = DEFAULT_DAYS, repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD,
                  site: str | None = None, today: date | None = None) -> dict[str, Any]:
    """Read-only: false-alarm and repeat call-outs per site and system for the last `days` days."""
    today = today or date.today()
    data = await collect(fsm, db, days, today)
    wanted = _norm_key(site)
    by_site: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in data["events"]:
        if wanted and wanted not in _norm_key(e["site"]) and wanted not in _norm_key(e["customer"]):
            continue
        by_site[_norm_key(e["site"])].append(e)
    sites = [_site_rollup(evs[0]["site"], evs, repeat_threshold) for evs in by_site.values()]
    sites.sort(key=lambda s: (not s["repeat_offender"], -s["false_alarms"], -s["callouts"], s["site"]))
    fa_all = [e for s in sites for e in s["events"] if e["is_false_alarm"]]
    return {
        "demo": getattr(fsm, "demo", False),
        "period": {"from": data["start"].isoformat(), "to": today.isoformat(), "days": days},
        "repeat_threshold": repeat_threshold,
        "totals": {"callouts": sum(s["callouts"] for s in sites), "false_alarms": len(fa_all),
                   "sites_with_false_alarms": sum(1 for s in sites if s["false_alarms"]),
                   "repeat_offender_sites": sum(1 for s in sites if s["repeat_offender"]),
                   "false_alarms_with_open_gaps": sum(1 for e in fa_all if e["gaps"])},
        "repeat_offenders": [{"site": s["site"], "false_alarms": s["false_alarms"], "callouts": s["callouts"],
                              "flags": s["flags"],
                              "systems": [x["system"] for x in s["systems"] if x["repeat_false_alarms"]]}
                             for s in sites if s["repeat_offender"]],
        "sites": sites, "notes": data["notes"],
    }


def _cell(value: Any) -> str:
    return " ".join(str(value if value not in (None, "") else "-").replace("|", "/").split())


def build_report(analysis: dict[str, Any], company: str = "Salts Fire and Security",
                 today: date | None = None) -> str | None:
    """Markdown evidence report, one section per site with call-outs in the period. None if there are none."""
    sites = analysis["sites"]
    if not sites:
        return None
    today = today or date.today()
    p, thr = analysis["period"], analysis["repeat_threshold"]
    out = ["# False alarm log and review - evidence report",
           f"Prepared {today.isoformat()} by {company} for the period {p['from']} to {p['to']} ({p['days']} days).", ""]
    if analysis["demo"]:
        out += ["> **DEMO DATA** - Salts FSM isn't connected, so this is not a real record.", ""]
    out += ["Purpose: evidence that every false alarm is logged, investigated and reviewed, with corrective action "
            "recorded, as BS 5839-1:2025 expects. Events come from Salts FSM jobs; cause, action and review come "
            "from the Jarvis false alarm log. **DRAFT** - to be checked and signed by a competent person; confirm "
            "clause references against your copy of the standard.",
            f"A site or system is flagged as a repeat when it has {thr} or more events in the period (an internal "
            "trigger, not a figure from the standard).", ""]
    for s in sites:
        out += [f"## {s['site']}" + (f" ({s['customer']})" if s["customer"] else ""), "",
                f"- Call-outs: {s['callouts']}; false alarms: {s['false_alarms']}",
                f"- Repeat flags: {'; '.join(s['flags']) if s['flags'] else 'none'}",
                f"- False alarms with open gaps: {s['events_with_open_gaps']}", ""]
        out += ["| System | Call-outs | False alarms | First | Last | Avg days between | Flags |",
                "|---|---|---|---|---|---|---|"]
        for x in s["systems"]:
            out.append(f"| {_cell(x['system'])} | {x['callouts']} | {x['false_alarms']} | {_cell(x['first'])} | "
                       f"{_cell(x['last'])} | {_cell(x['avg_days_between_callouts'])} | {_cell('; '.join(x['flags']) or '')} |")
        out += ["", "| Date | Job | System | False alarm | Cause (category) | Corrective action | Evidence | "
                    "Investigated by | Reviewed |", "|---|---|---|---|---|---|---|---|---|"]
        for e in s["events"]:
            r = e["record"] or {}
            cause = _cell(r.get("cause")) + (f" ({r['cause_category']})" if r.get("cause_category") else "")
            action = _cell(r.get("corrective_action"))
            evidence = _cell(" ".join(x for x in (r.get("action_done_date"), r.get("evidence_ref")) if x))
            reviewed = _cell(f"{r['reviewed_by']} {r['review_date']}" if r.get("reviewed_by") and r.get("review_date") else "")
            out.append(f"| {_cell(e['date'])} | {_cell(e['job_ref'])} | {_cell(e['system'])} | "
                       f"{'Yes' if e['is_false_alarm'] else 'No (call-out)'} | {cause} | {action} | {evidence} | "
                       f"{_cell(r.get('investigated_by'))} | {reviewed} |")
        gaps = [(e, g) for e in s["events"] for g in e["gaps"]]
        out += ["", "**Open gaps**", ""]
        out += [f"- {e['job_ref']} ({e['date'] or 'undated'}): {g}" for e, g in gaps] or ["- None - every false alarm above is "
                                                                                             "investigated, actioned, evidenced and reviewed."]
        if s["repeat_offender"] or s["repeat_callouts"]:
            out += ["", "**Suggested next steps (for a competent person to decide)**", "",
                    "- Review the causes above for a pattern (same device, zone, time of day, activity).",
                    "- Consider whether the cause points to detector type or siting, maintenance, or user behaviour, and "
                    "agree actions with the responsible person.",
                    "- Record the agreed action, its completion date and evidence in the false alarm log."]
        out += ["", "Reviewed by: ______________________  Date: ____________", ""]
    if analysis["notes"]:
        out += ["**Notes**", ""] + [f"- {n}" for n in analysis["notes"]]
    return "\n".join(out).rstrip() + "\n"


class FalseAlarmLog:
    def __init__(self, j):
        self.j = j

    async def analyse(self, days: int = DEFAULT_DAYS, repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD,
                      site: str | None = None) -> dict[str, Any]:
        return await analyse(self.j.fsm, self.j.db, days, repeat_threshold, site)

    async def record(self, job_ref: str, site: str = "", **fields: str) -> dict[str, Any]:
        """Create/update the log entry for one job. Run only after the owner approves (see the tool). Writes to
        Jarvis's own log - never to Salts FSM."""
        ref = job_ref.strip()
        if not ref:
            raise ValueError("A job reference is needed to log a false alarm.")
        data = await collect(self.j.fsm, None, RECORD_LOOKBACK_DAYS, date.today())
        event = next((e for e in data["events"] if ref.lower() in (e["job_ref"].lower(), e["job_id"].lower())), None)
        site = (event["site"] if event else "") or site.strip()
        if not site:
            raise ValueError(f"Job {ref} wasn't found in the last {RECORD_LOOKBACK_DAYS} days of call-outs in Salts "
                             "FSM - say which site it was at to log it by hand.")
        if event:
            fields = {"system": event["system"], "event_date": event["date"], **{k: v for k, v in fields.items() if v}}
        return self.j.db.upsert_false_alarm_record(event["job_ref"] if event else ref, site, **fields)

    async def report(self, days: int = DEFAULT_DAYS, repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD,
                     site: str | None = None) -> dict[str, Any]:
        analysis = await self.analyse(days, repeat_threshold, site)
        text = build_report(analysis, self.j.settings.company_name)
        if text is None:
            return {"error": "No call-outs found for that site and period.", "period": analysis["period"]}
        doc_id = self.j.documents._save("false_alarm_report", "False alarm evidence report", text)
        return {"shown_on_display": True, "doc_id": doc_id, "totals": analysis["totals"],
                "repeat_offenders": analysis["repeat_offenders"], "report": text,
                "note": "Draft for review and sign-off - nothing has been written to Salts FSM."}
