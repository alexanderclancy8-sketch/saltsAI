"""What Jarvis did to produce one reply: the data behind the "show working" UI.

Each chat turn the console shows (a) a line above the reply naming what he is checking right now (driven by the
real ``tool`` events both brains already publish), and (b) once the reply is finished, a source-and-time line
underneath, a button for the matching pop-up when there is detail behind the answer, and up to two follow-up
questions. (b) is built here from the tool calls that really happened, never from text the model wrote, so the
source line cannot claim a system that was not consulted.

The trace listens to the event bus (``EventBus.add_tap``), so it sees tool calls from every path - the API brain's
loop, the Claude Code (Max) brain's in-process MCP tools, and the server-side web tools - with no change to any
tool. A turn starts at the ``thinking`` event and is read once, by the brain, just before it publishes ``reply``
(``finish()``). Background (quiet) turns publish none of these events, so they never leave a trace.

Nothing here writes or sends anything; it only describes. Offering a pop-up button or a follow-up chip is a
suggestion the owner may click, and a click is an ordinary chat message or the same drawer-open as the rail.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from . import coverage as cov_mod

# The pop-ups a reply may point at: the rail sections of the console (hud.js POPS, minus Settings/Connections/Demo).
PANELS = ("approvals", "activity", "comms", "issues", "health", "ops", "fleet", "finance", "presence", "upcoming")
PANEL_TITLES = {"approvals": "Approvals", "activity": "What Jarvis did", "comms": "Comms", "issues": "Issues", "health": "Health", "ops": "Ops",
                "fleet": "Fleet", "finance": "Finance", "presence": "Presence", "upcoming": "Coming up"}
MAX_FOLLOW_UPS = 2
FOLLOW_UP_CHARS = 90

# tool name -> (sources it reads from, the pop-up that holds the detail or None). Only tools worth naming appear;
# anything unlisted (remember, note_open_request, ...) leaves no source and offers no pop-up.
_FSM = "Salts FSM"
_TOOL_INFO: dict[str, tuple[tuple[str, ...], str | None]] = {
    **{n: (("Outlook",), "comms") for n in ("email_inbox", "email_search", "email_read", "email_attachment_read",
                                            "email_draft_reply")},
    **{n: ((_FSM,), "ops") for n in ("fsm_jobs", "staff_today", "staff_productivity", "staff_overdue_jobs",
                                     "attendance_check", "job_detail", "lone_worker_check", "staff_review",
                                     "office_productivity")},
    **{n: ((_FSM,), None) for n in ("fsm_data", "fsm_analyse", "fsm_document_read", "fsm_query", "fsm_systems_due", "ppm_schedule_plan", "fsm_contracts_renewing",
                                    "fsm_quotes", "fsm_source_search", "fsm_source_read", "staff_certifications",
                                    "unbilled_jobs", "remedial_quotes", "contract_renewals", "out_of_hours_calls",
                                    "route_optimise_advice", "timesheet_check", "false_alarm_analysis", "upsell_opportunities",
                                    "customer_balance")},   # (a balance is a chat answer only: no pop-up, the office has no Finance)
    **{n: (("Stock records",), None) for n in ("stock_levels", "stock_usage", "stock_job_materials")},
    **{n: (("RAM Tracking",), "fleet") for n in ("engineer_locations", "nearest_engineer", "van_day", "fleet_diagnostics")},
    **{n: (("Sage",), "finance") for n in ("finance_snapshot", "finance_aged", "finance_vat", "finance_cashflow",
                                           "finance_corporation_tax", "finance_profit_and_loss",
                                           "finance_credit_control")},
    "finance_deadlines": (("Sage",), "upcoming"),
    "accreditations_status": (("Accreditations register",), "upcoming"),
    "customer_health": ((_FSM,), "finance"),
    "business_health": ((_FSM, "Sage"), "finance"),
    **{n: (("Google and socials",), "presence") for n in ("marketing_overview", "search_rankings", "seo_audit",
                                                         "competitor_audit", "review_requests")},
    **{n: (("Issues log",), "issues") for n in ("issues_list", "issue_report", "issue_fix")},
    **{n: (("Routine tests",), "health") for n in ("routine_tests_run", "routine_tests_status")},
    "suggestions": (("Suggestions",), "approvals"),
    "what_did_you_do": (("Jarvis's activity record",), "activity"),
    "knowledge_search": (("Knowledge base",), None),
    **{n: (("The web",), None) for n in ("web_search", "web_fetch", "WebSearch", "WebFetch", "regulatory_watch",
                                        "technical_watch")},
}

# A source label that is still showing sample data gets "(demo data)" after it, so the line never dresses a
# sample up as real. Each check is defensive: anything unexpected simply means "not demo".
_DEMO: dict[str, Callable[[Any], bool]] = {
    "Outlook": lambda j: bool(j.mail.demo),
    _FSM: lambda j: bool(j.fsm.demo),
    "Sage": lambda j: bool(getattr(j.finance, "demo", False)),
    "Stock records": lambda j: bool(j.stores.demo),
    "RAM Tracking": lambda j: bool(j.ram.demo),
}


def tool_info(name: str) -> tuple[tuple[str, ...], str | None]:
    return _TOOL_INFO.get(name.removeprefix("mcp__jarvis__"), ((), None))


def clean_follow_ups(items: Any) -> list[str]:
    """Up to two short, distinct, non-empty questions."""
    out: list[str] = []
    for raw in items or []:
        text = " ".join(str(raw).split())[:FOLLOW_UP_CHARS].strip()
        if text and text.lower() not in {o.lower() for o in out}:
            out.append(text)
        if len(out) == MAX_FOLLOW_UPS:
            break
    return out


class TurnTrace:
    def __init__(self, j, panels: Any = None, team: bool | None = None) -> None:
        """``panels``: the pop-ups this trace may ever point at (a team session's trace is limited to the ones its console
        has); None means all of them, as for the owner. ``team``: a team session's trace (default: whenever ``panels`` is
        limited) - its coverage line never counts a source the team version can't read (Sage, mail) as missing."""
        self.j = j
        self.allowed = frozenset(PANELS if panels is None else panels)
        self.team = (panels is not None) if team is None else bool(team)
        self.active = False
        self._next_text = ""
        self._reset()

    def _reset(self) -> None:
        self.started = 0.0
        self.panels: list[str] = []
        self.sources: list[str] = []
        self.offered_panel: str | None = None
        self.follow_ups: list[str] = []
        self.pending_before = 0
        self.facts: list[dict[str, str]] = []   # what each finished tool call said about its sources (coverage.call_facts)
        self.user_text = ""
        self.mode = "typed"
        self.entity_notes: list[str] = []   # "Jarvis's notes on X" this turn leaned on (add_source): notes, not a checked system
        self.web: dict[str, Any] | None = None   # the turn's web budget record (add_web): sources really used, uses, errors

    # ------------------------------------------------------------------ feeding it
    def on_event(self, event_type: str, data: Any) -> None:
        """EventBus tap. Never raises: describing a turn must not be able to break one."""
        try:
            if event_type == "user_message" and isinstance(data, dict):
                self._next_text = str(data.get("text") or "")
            elif event_type == "thinking":
                self.begin(str((data or {}).get("mode") or "typed") if isinstance(data, dict) else "typed")
            elif event_type == "tool" and self.active and isinstance(data, dict) and data.get("state") == "start":
                self.note_tool(str(data.get("name") or ""))
            elif event_type == "tool" and self.active and isinstance(data, dict) and data.get("state") in ("done", "error"):
                self.note_result(data)
        except Exception:  # noqa: BLE001
            self.active = False

    def begin(self, mode: str = "typed") -> None:
        text, self._next_text = self._next_text, ""
        self._reset()
        self.user_text, self.mode = text, mode
        self.active = True
        self.started = time.monotonic()
        try:
            self.pending_before = len(self.j.db.pending_actions())
        except Exception:  # noqa: BLE001
            self.pending_before = 0

    def note_tool(self, name: str) -> None:
        sources, panel = tool_info(name)
        for s in sources:
            if s not in self.sources:
                self.sources.append(s)
        if panel and panel in self.allowed:
            self.panels.append(panel)

    def note_result(self, data: dict[str, Any]) -> None:
        """A finished tool call: the facts the brain worked out from its result (``coverage``), or - for a call that raised
        and carried none - an error for every source that tool reads."""
        facts = data.get("coverage")
        if isinstance(facts, list):
            self.facts += [f for f in facts if isinstance(f, dict) and f.get("src") and f.get("status")][:20]
        elif data.get("state") == "error":
            self.facts += [{"src": s, "status": cov_mod.ERROR} for s in cov_mod.tool_sources(str(data.get("name") or ""))]

    def add_source(self, label: str) -> None:
        """A source that is not a tool call: the customer / site notes added to a tool result (services/entity_memory.py), named
        so the source line says the answer leaned on them ("Jarvis's notes on Acme"). Ignored outside a turn."""
        label = " ".join(str(label or "").split())[:80]
        if self.active and label and label not in self.sources:
            self.sources.append(label)
        if self.active and label and label not in self.entity_notes:
            self.entity_notes.append(label)

    def add_web(self, web: Any) -> None:
        """The turn's web record (brain/web_research.py ``WebTurn``), handed over by the brain just before ``finish``: the numbered
        sources its web tools really returned, and how many searches / page reads / errors there were. Ignored outside a turn."""
        if not self.active:
            return
        try:
            summary = web.summary()
        except Exception:  # noqa: BLE001 - describing a turn must never break it
            return
        if summary.get("sources") or summary.get("searches") or summary.get("reads") or summary.get("errors"):
            self.web = summary
            if "The web" not in self.sources:
                self.sources.append("The web")

    def offer(self, panel: str | None, follow_ups: list[str]) -> None:
        """The offer_next_steps tool: what the model chose to suggest. Ignored outside a turn."""
        if not self.active:
            return
        if panel in PANELS and panel in self.allowed:
            self.offered_panel = panel
        self.follow_ups = clean_follow_ups(follow_ups)

    # ------------------------------------------------------------------ reading it
    def _queued_something(self) -> bool:
        try:
            return len(self.j.db.pending_actions()) > self.pending_before
        except Exception:  # noqa: BLE001
            return False

    def finish(self, reply: str = "") -> dict[str, Any]:
        """The extras for the ``reply`` event; {} when no turn is being traced (a background turn). ``reply`` (what was
        said) only decides whether a spoken Low-confidence turn still needs its one-sentence gap note."""
        if not self.active:
            return {}
        self.active = False
        sources = []
        for s in self.sources:
            try:
                demo = _DEMO[s](self.j) if s in _DEMO else False
            except Exception:  # noqa: BLE001
                demo = False
            sources.append(f"{s} (demo data)" if demo else s)
        # Something waiting for the owner's click beats everything else; then what the model asked for; then the
        # pop-up of the tool used most (the last one on a tie, i.e. what he looked at most recently).
        if "approvals" in self.allowed and self._queued_something():
            panel = "approvals"
        elif self.offered_panel:
            panel = self.offered_panel
        elif self.panels:
            panel = max(reversed(self.panels), key=self.panels.count)
        else:
            panel = None
        out: dict[str, Any] = {"elapsed_ms": int((time.monotonic() - self.started) * 1000)}
        if sources:
            out["sources"] = sources
        if panel:
            out["panel"] = panel
            out["panel_title"] = PANEL_TITLES[panel]
        if self.follow_ups:
            out["follow_ups"] = list(self.follow_ups)
        facts = list(self.facts)
        if self.web:
            if self.web.get("sources"):
                out["web_sources"] = [dict(s) for s in self.web["sources"]]
            facts += cov_mod.web_facts(self.web)
        try:
            cov = cov_mod.summarise(facts, self.user_text, demo=cov_mod.demo_map(self.j), team=self.team,
                                    notes=self.entity_notes, reply=reply)
        except Exception:  # noqa: BLE001 - describing a turn must never break it
            cov = None
        if cov:
            if self.mode == "voice":
                said = cov_mod.spoken(cov, reply)
                if said:
                    cov["spoken"] = said
            out["coverage"] = cov
        return out
