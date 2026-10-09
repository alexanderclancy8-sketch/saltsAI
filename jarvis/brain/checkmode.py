"""Check mode: the question-check runner asking the live brain, unable to send, queue or write anything.

The weekly accuracy scorecard (``services/question_checks.py``) asks a real brain owner-style questions. That brain must
behave like the owner's for READING - same model, same prompt, same read tools - and be unable to change anything at all,
however a question is worded or whatever the model decides. It is enforced in layers, every one of them default-deny:

1. ``tools.dispatch(..., check=True)`` sets ``active`` for the whole call and only runs a tool named in ``CHECK_TOOLS``,
   a closed allowlist of pure reads. Any tool with ``approval=True`` is refused outright (never queued), and so is every
   tool not on the list - ``log_job`` / ``log_purchase_order`` (which queue by themselves), ``remember`` (memory),
   email / Teams / display / notification tools, the van-location tools (they write the look-up log), the access-code
   tool (a secret must never land in a stored check result) and anything added to Jarvis later until someone puts it on
   the list on purpose.
2. While ``active`` is set, the approval queue (``ActionExecutor.queue``), the notifier, the transcript and the owner's
   event bus refuse or drop whatever reaches them (``guard()`` / ``is_active()``), so even a read tool that unexpectedly
   tried to write could not.
3. The brain built for a check run has its own private event bus, no transcript, no conversation-quality record, no
   proactive posts and a fresh conversation per question (``JarvisBrain(..., check=True)`` / ``MaxBrain(..., check=True)``).

Nothing here approves, sends or writes; it only refuses.
"""

from __future__ import annotations

from typing import Any

from ..events import check_mode as active  # one variable, shared with the event bus


class CheckModeBlocked(RuntimeError):
    """Something tried to send, queue or write while a question check was running."""


# Pure reads a check-run brain may use. Default deny: a tool missing from here is refused in check mode.
CHECK_TOOLS = frozenset({
    # Salts FSM (read-only GETs)
    "fsm_jobs", "fsm_query", "fsm_catalog", "fsm_data", "fsm_analyse", "calculate", "fsm_systems_due", "ppm_schedule_plan",
    "route_optimise_advice", "fsm_contracts_renewing", "fsm_quotes", "fsm_source_search", "fsm_source_read", "job_detail",
    "staff_today", "staff_productivity", "staff_roles", "staff_review", "office_productivity", "staff_overdue_jobs",
    "staff_certifications", "attendance_check", "lone_worker_check", "unbilled_jobs", "remedial_quotes", "customer_health",
    "contract_renewals", "upsell_opportunities", "false_alarm_analysis", "business_health",
    # renewals in Salts FSM: the due list is a read; preparing a draft and queueing a send are writes and stay off this list
    "fsm_renewals_due",
    # accounts, stock, registers (reads; sample data is withheld by demo_guard as usual)
    "finance_snapshot", "finance_aged", "finance_vat", "finance_cashflow", "finance_corporation_tax",
    "finance_profit_and_loss", "finance_deadlines", "finance_credit_control", "stock_levels", "stock_usage",
    # FSM documents (read-only; a scan may be transcribed by Jarvis's own model) and Jarvis's own customer / site notes (read only -
    # entity_note_add / entity_note_propose WRITE and stay off this list)
    "fsm_document_read", "entity_notes_get",
    # one customer's balance (read-only; office team members and the owner / managers)
    "customer_balance",
    # similar past quotes, jobs and emails for a described job (read-only; services/similar_work.py)
    "find_similar_work",
    # a drafted layout / zone chart on a floor plan: in a check it only PROPOSES (a vision read + compute); saving the drawing is a
    # write and is skipped (services/plan_drawings.py checks checkmode itself, and checkmode.guard refuses the save underneath)
    "draw_on_plan",
    "stock_job_materials", "stock_reorder", "accreditations_status", "audit_evidence", "oncall_roster",
    # mail (read only), presence, knowledge, Jarvis's own records
    "email_inbox", "email_search", "email_read", "marketing_overview", "search_rankings", "knowledge_search",
    "what_did_you_do", "issues_list", "routine_tests_status", "list_automations", "agent_runs", "action_items",
    "weekly_digest_latest",
    # system schematics: laying a drawing out is compute (allowed); SAVING it is a write, which draw_schematic skips in check mode
    # (and Schematics.save refuses through guard()); listing / opening saved drawings is a read
    "draw_schematic", "list_schematics", "open_schematic",
})


def is_active() -> bool:
    return bool(active.get())


def tool_allowed(tool: Any) -> bool:
    """May this tool run in check mode? Never one with approval=True; otherwise only the allowlist."""
    return not getattr(tool, "approval", False) and getattr(tool, "name", "") in CHECK_TOOLS


def refusal(name: str) -> dict[str, Any]:
    return {"blocked_in_check_mode": True, "tool": name,
            "error": (f"{name} is not available during a question check: checks may only read. Nothing was sent, queued or "
                      "changed. Answer from what you could read, and say what you could not do.")}


def guard(what: str) -> None:
    """Raise if a question check is running. Called by every path that sends, queues or writes."""
    if active.get():
        raise CheckModeBlocked(f"{what} is not allowed during a question check")
