"""Who may do what: the role model and the two default-deny allowlists (routes and tools) behind Team mode.

Three roles:

* ``owner``   - the principal owner (display password session, the owner's own Microsoft sign-in, or local-only mode).
* ``manager`` - everyone else who gets in today (managers signed in through Microsoft). Exactly as before.
* ``team``    - engineers and office staff, new. They sign in with a team access code the owner sets (see
                ``services/team_access.py``) and get a cut-down console and a cut-down Jarvis: no Finance, no Approvals,
                no Connections, no memory editing, no settings, no staff-report key, and a brain that can only use the
                read-only operational tools listed in ``TEAM_TOOLS``.

  Since the owner's decision of 2026-10-08 the team role has two kinds (``Caller.team_role``), each with its OWN code:

  * ``engineer`` - exactly the team role as it was: ``TEAM_TOOLS`` and nothing more. The old single team code and every
                   team session signed in with it ARE the engineer code and engineer sessions (least privilege, no data
                   migration: same database key, same cookie key), so an upgrade gives nobody more than they had.
  * ``office``   - the engineer allowlist MINUS the engineer-only tools (``ENGINEER_ONLY_TOOLS``: drawing a system schematic -
                   office may list, open and export saved ones) plus ONE read-only tool, ``customer_balance``
                   (``OFFICE_EXTRA_TOOLS``): for one customer at a time, what they owe, what is overdue and their oldest overdue
                   invoice - so the office can answer a customer who rings about their account. Nothing else finance-related (no invoice lists, payments,
                   credit notes, company finance, pay, fsm_data / fsm_analyse) and the same console as an engineer.

  Both kinds are tier ``team`` everywhere else - the route table, the console features, what stored work (approvals,
  background calls) is filed and re-run as. Stored work only ever records ``team``, which is re-run as an ENGINEER: the
  office's one extra tool never runs later, somewhere else, on a record's say-so.

Team mode is enforced here, on the backend, in two places that this module is the single source of truth for:

1. **Routes.** ``ROUTE_POLICY`` classifies EVERY route of the app (HTTP and WebSocket) as ``public``, ``page``, ``team``,
   ``manager`` or ``owner``. ``main.create_app`` installs one app-wide guard that looks the matched route up here and refuses
   (401 not signed in / 403 not for this role) before the handler runs; a route that is not in the table is refused to
   everyone (default deny), and ``tests/test_team_mode.py`` fails if any route in the app is missing from it.
2. **Tools.** ``tool_allowed(name, caller)`` is the one check used by ``brain.tools.dispatch`` (every tool call from a
   conversation) AND by ``AsyncTools.start`` (``run_in_background``), so a background call can never reach a tool its
   requester could not call directly. A team caller may use only the names in ``TEAM_TOOLS``; anything new is denied
   until someone adds it there on purpose.

Nothing here can approve anything, and a team user can never approve, deny, edit or retry an action: those routes are
manager-level, and the Teams approval path is separate (an allowlist of approver emails, never a team session).
"""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass

OWNER, MANAGER, TEAM = "owner", "manager", "team"
ROLES = (OWNER, MANAGER, TEAM)
ROLE_LABEL = {OWNER: "Owner", MANAGER: "Manager", TEAM: "Team"}
# The two kinds of team member. Each has its own access code (services/team_access.py). An unknown or missing kind is an
# ENGINEER - least privilege, and what every team session from before the split is.
OFFICE, ENGINEER = "office", "engineer"
TEAM_ROLES = (OFFICE, ENGINEER)
TEAM_ROLE_LABEL = {OFFICE: "Office", ENGINEER: "Engineer"}


def team_role_of(raw: object) -> str:
    """A team member's kind from a cookie, a request body or a parameter: ``office`` only when it says exactly that, else engineer."""
    return OFFICE if str(raw or "").strip().lower() == OFFICE else ENGINEER

# --- route levels ------------------------------------------------------------------------------------------------------
PUBLIC = "public"    # no session needed (login, health, static files, the staff-key report form, the Teams webhook)
PAGE = "page"        # an HTML page whose handler redirects the signed-out to /login itself
TEAM_OK = "team"     # any signed-in role, team included
MANAGER_OK = "manager"  # owner or manager; a team session is refused with 403
OWNER_ONLY = "owner"    # the principal owner only; a manager or team session is refused with 403
LEVELS = (PUBLIC, PAGE, TEAM_OK, MANAGER_OK, OWNER_ONLY)
_RANK = {TEAM: 0, MANAGER: 1, OWNER: 2}
_NEEDS = {TEAM_OK: 0, MANAGER_OK: 1, OWNER_ONLY: 2}


@dataclass(frozen=True)
class Caller:
    """Who is acting. ``name`` and ``sid`` only exist for a team session (the name is whatever the person typed when
    signing in - it labels their requests, it is not a verified identity). ``team_role`` is the kind of team member
    (``office`` / ``engineer``); anything but ``office`` - including the empty default - is an engineer."""
    role: str
    name: str = ""
    sid: str = ""
    team_role: str = ""

    @property
    def is_team(self) -> bool:
        return self.role == TEAM

    @property
    def kind(self) -> str:
        """``office`` or ``engineer`` for a team member; "" for the owner and managers."""
        return team_role_of(self.team_role) if self.role == TEAM else ""

    @property
    def is_office(self) -> bool:
        return self.kind == OFFICE

    @property
    def is_engineer(self) -> bool:
        return self.kind == ENGINEER

    @property
    def role_label(self) -> str:
        """'Office' / 'Engineer' for a team member, else 'Owner' / 'Manager' (the console's top bar, the audit lines)."""
        return TEAM_ROLE_LABEL[self.kind] if self.role == TEAM else ROLE_LABEL.get(self.role, self.role)

    @property
    def label(self) -> str:
        """How a person is named in logs, approval cards and the van-look-up log: "Sam (office)", "Sam (engineer)"."""
        return f"{self.name} ({self.kind})" if self.role == TEAM and self.name else ROLE_LABEL.get(self.role, self.role)

    @property
    def requester(self) -> str:
        """The stable key a background call is filed under ("" for the owner, managers and Jarvis himself). An engineer keeps
        the pre-split key (``team:<name>``) so their earlier background results are still theirs; office is ``office:<name>``,
        so an office Sam and an engineer Sam never see each other's."""
        if self.role != TEAM:
            return ""
        return f"{'office' if self.is_office else 'team'}:{self.name.strip().lower()}"


# The suffixes ``Caller.label`` has ever put after a team member's name (an approval stored before the split says "(team)").
TEAM_LABEL_SUFFIXES = (" (team)", " (office)", " (engineer)")


def strip_team_label(label: str) -> str:
    for suffix in TEAM_LABEL_SUFFIXES:
        if label.endswith(suffix):
            return label[: -len(suffix)]
    return label


# The caller of the tool call being run right now (set by brain.tools.dispatch for the duration of the handler, so code
# deep inside a tool - the approval queue, the van-look-up log, background calls - can see who asked). None = the owner's
# own conversation, a scheduled job or Jarvis himself, exactly as before Team mode.
current_caller: contextvars.ContextVar[Caller | None] = contextvars.ContextVar("jarvis_caller", default=None)


# --- the role a piece of stored work runs with ----------------------------------------------------------------------------
# An automation, a background call, a queued approval or a self-improvement request is created in one person's turn and run
# later, somewhere else (the scheduler, a worker task, the approver's click). It must run with the permissions of whoever
# CREATED it, never those of whoever's turn or click happens to trigger it. (A team member's record stores ``team`` - never
# office / engineer - and so is re-run as an ENGINEER: see the module docstring.) Each such record stores the creator's role
# (owner | manager | team) and the run re-creates the creator's ``Caller`` from it, in the same ``current_caller`` context
# variable a live chat turn uses, so every role-dependent tool (fsm_data's finance / pay / HR resources, fleet_diagnostics)
# looks in the one place it already looks.
#
# The owner's own turn is "no caller" (None), exactly as before, so the owner's stored work runs with no caller too.
# A stored role that is missing or not one of the three is read as MANAGER: least privilege, never the owner's.
AUTOMATION_ROLES = (OWNER, MANAGER)   # who may create an automation: a team caller never may (create_automation is not a team tool)


# Jarvis's own scheduled reflection over the shared transcript (services/self_learning.py): what it reads includes what a manager
# typed, so it runs as a manager, never the owner.
REFLECTION_CALLER = Caller(MANAGER, "Jarvis (self-reflection)")


def role_of(caller: Caller | None) -> str:
    """The role a caller acts with. None is the owner's own conversation (or a scheduled job / Jarvis himself)."""
    return OWNER if caller is None else caller.role


def stored_role(raw: object) -> str:
    """A role read back from a database column: one of the three roles, else MANAGER (least privilege - an unknown, empty or
    tampered value never becomes the owner's)."""
    value = str(raw or "").strip().lower()
    return value if value in ROLES else MANAGER


def caller_for_role(role: str, name: str = "") -> Caller | None:
    """The caller a stored record runs as: None for the owner (as before), a ``Caller`` for a manager or team member."""
    role = stored_role(role)
    return None if role == OWNER else Caller(role, clean_name(name) if name else "")


def outranks(role: str, other: str) -> bool:
    """True when ``role`` is strictly higher than ``other`` (owner > manager > team)."""
    return _RANK[stored_role(role)] > _RANK[stored_role(other)]


def clean_name(raw: str) -> str:
    """A display name safe to put in a prompt, a log line and an approval card: letters, digits, spaces and . ' - only."""
    kept = "".join(c for c in (raw or "") if c.isalnum() or c in " .'-")
    return re.sub(r"\s+", " ", kept).strip()[:40]


def role_meets(role: str | None, level: str) -> bool:
    if level in (PUBLIC, PAGE):
        return True
    return role in _RANK and _RANK[role] >= _NEEDS[level]


# Routes an OFFICE session may use and an engineer may not: NONE. An office member's one extra (a customer's balance) is only
# ever a chat answer from the ``customer_balance`` tool - never a route, a drawer, a pop-up or a download - so the office console
# is exactly the engineer console. A route added here would be refused to engineers by ``route_allowed``; the inventory test
# (tests/test_office_role.py) pins that this stays empty unless someone decides otherwise on purpose.
OFFICE_ONLY_ROUTES: frozenset[str] = frozenset()


def route_allowed(key: str, caller: Caller | None) -> bool:
    """May ``caller`` use the route ``key`` (a ROUTE_POLICY key)? Unclassified = no (default deny)."""
    level = ROUTE_POLICY.get(key)
    if level is None:
        return False
    if level in (PUBLIC, PAGE):
        return True
    if caller is None or not role_meets(caller.role, level):
        return False
    return not (key in OFFICE_ONLY_ROUTES and caller.is_team and not caller.is_office)


# --- what each role can see in the console -------------------------------------------------------------------------------
FEATURES = {
    OWNER: {"approvals": True, "finance": True, "connections": True, "memory": True, "comms": True, "issues": True,
            "health": True, "settings_admin": True, "attachments": True, "feedback": True, "team_access": True,
            "engineer_homes": True, "activity": True, "activity_export": True},
    MANAGER: {"approvals": True, "finance": True, "connections": True, "memory": True, "comms": True, "issues": True,
              "health": True, "settings_admin": True, "attachments": True, "feedback": True, "team_access": False,
              "engineer_homes": False, "activity": True, "activity_export": False},
    TEAM: {"approvals": False, "finance": False, "connections": False, "memory": False, "comms": False, "issues": False,
           "health": False, "settings_admin": False, "attachments": False, "feedback": False, "team_access": False,
           "engineer_homes": False, "activity": False, "activity_export": False},
}

# Keys of /api/status a team session receives. An allowlist, so a key added to the status later is withheld from team
# until someone decides it is fine. (`staff`, `overdue_jobs`, `presence`, `voice`, `activity` are not finance, not
# approvals, not settings; `accreditations` is cut down to what/date/days_left by the handler.)
TEAM_STATUS_KEYS = frozenset({"generated_at", "staff", "overdue_jobs", "presence", "voice", "company", "role", "team_role", "who",
                              "accreditations", "fleet"})

# What a team session's live connection ever carries: its own turns, and the signal to reconnect after a reload. Anything
# else on the owner's bus (approvals, notifications, proactive posts, display panels, finance...) never reaches it.
TEAM_EVENTS = frozenset({"user_message", "thinking", "delta", "tool", "reply", "error", "stopped", "conversation_reset",
                         "reload"})

# A display panel can be marked ``"audience": "owner"`` (a chart or table built from owner-only FSM data - finance, staff pay, HR).
# The owner's bus is shared by the owner's and every manager's console, so the live connection (``main.ws_events``) asks this
# before sending: only the principal owner's console receives such a panel. (A team console never sees "display" at all.)
OWNER_AUDIENCE = "owner"


def event_visible(event: object, role: str | None) -> bool:
    """May a console signed in with ``role`` be sent this bus event? Everything is visible except a panel marked for the owner alone."""
    data = event.get("data") if isinstance(event, dict) else None
    if isinstance(data, dict) and data.get("audience") == OWNER_AUDIENCE:
        return role == OWNER
    return True


# --- tools -----------------------------------------------------------------------------------------------------------------
# The ONLY tools a team session's Jarvis (and a team session's background calls) may use. Read-only operational data,
# plus `log_job`, which queues an approval for a human with the requester recorded (and never auto-runs: the standing
# approvals are skipped for a team requester). Everything else is denied by default - finance, accounts, staff review and
# pay, email, stock values, quote and contract values, access codes, settings, connections, memory, approvals, the
# engineering agents and pull requests - so a new tool needs a deliberate line here. (``find_similar_work`` shows a team caller
# similar past jobs and quotes, but strips every value and price and never reads email for them.)
TEAM_TOOLS = frozenset({
    "fsm_jobs",              # today's / a date range's jobs
    "job_detail",            # one job: notes, status, materials used
    "fsm_systems_due",       # maintained systems that are due service
    "staff_overdue_jobs",    # jobs past their time and not completed
    "staff_today",           # the live engineer board: who is on what
    "engineer_locations",    # vans / engineers, subject to the owner's out-of-hours privacy rule, look-ups are logged
    "nearest_engineer",      # closest engineer to a place, same privacy rule
    "marketing_overview",    # followers and reviews (the Presence pop-up)
    "knowledge_search",      # standards and company how-tos (never the owner's private or finance folders)
    "find_similar_work",     # similar past jobs and quotes - for a team caller WITHOUT any money (no values, no pricing guide)
                             # and without emails; owner-only FSM resources are never read for them (services/similar_work.py)
    "log_job",               # queues a job for approval, requester recorded, never auto-approved
    "run_in_background",     # only for the tools above; forced SILENT; scoped to the requester
    "background_results",    # only the requester's own
    # system schematics (services/schematics.py): no prices ever. Engineers draw and revise (useful on site); everyone in the team
    # may list, open and export saved drawings. draw_schematic saves to Jarvis's own records only - nothing is sent or changed.
    "draw_schematic",        # ENGINEER_ONLY_TOOLS: not office
    "list_schematics",
    "open_schematic",
})

# What an OFFICE team member has on top of TEAM_TOOLS - and nothing else. ``customer_balance`` is read-only and answers for ONE
# customer at a time with three figures and one invoice (services/customer_balance.py); it is the office's only finance tool.
OFFICE_EXTRA_TOOLS = frozenset({"customer_balance"})
# What an ENGINEER has that office does not: creating / revising a system schematic (office views and exports saved ones).
ENGINEER_ONLY_TOOLS = frozenset({"draw_schematic"})
ENGINEER_TOOLS = TEAM_TOOLS
OFFICE_TOOLS = (TEAM_TOOLS - ENGINEER_ONLY_TOOLS) | OFFICE_EXTRA_TOOLS

# Folders of the knowledge base a team session's `knowledge_search` never reads.
TEAM_KB_EXCLUDED = ("private/", "finance/")


def tools_for(caller: Caller | None) -> frozenset[str] | None:
    """The allowlist for a team caller (OFFICE_TOOLS / ENGINEER_TOOLS); None = no list (the owner, managers, scheduled jobs)."""
    if caller is None or caller.role != TEAM:
        return None
    return OFFICE_TOOLS if caller.is_office else ENGINEER_TOOLS


def tool_allowed(name: str, caller: Caller | None) -> bool:
    """May ``caller`` use the tool called ``name``? ``None`` (the owner's conversation, a scheduled job) and owner/manager
    callers: yes, as before. A team caller: only if the name is in their kind's allowlist (default deny) - an engineer has
    TEAM_TOOLS exactly, office has TEAM_TOOLS minus ENGINEER_ONLY_TOOLS plus OFFICE_EXTRA_TOOLS."""
    allowed = tools_for(caller)
    return True if allowed is None else name in allowed


OFFICE_ONLY_REFUSAL = ("That's for the office: account balances are looked up by the office, not from an engineer's Jarvis. "
                       "If a customer asks what they owe, pass them to the office.")


def refusal(name: str, caller: Caller | None = None) -> str:
    if name in OFFICE_EXTRA_TOOLS and caller is not None and caller.is_engineer:
        return OFFICE_ONLY_REFUSAL
    if name in ENGINEER_ONLY_TOOLS and caller is not None and caller.is_office:
        return ("Drawing or revising a schematic is for the engineers and managers. You can list, open and download the saved "
                "drawings (list_schematics / open_schematic); ask an engineer or a manager to draw a new one.")
    return (f"{name} isn't available to you here. This is the team version of Jarvis, which covers jobs, engineers, "
            "systems and fleet but not finance, approvals, accounts, staff pay or settings. If you need that, ask the office.")


# --- routes ----------------------------------------------------------------------------------------------------------------
# "METHOD /path-as-registered" -> level. WebSocket routes use "WS /path", mounts "MOUNT /path". Every route in the app
# must appear here (tests/test_team_mode.py enumerates app.routes and fails on an unclassified one), and the app-wide
# guard in main.create_app refuses a request whose route is not listed. When you add a route, decide its level here.
ROUTE_POLICY: dict[str, str] = {
    # ---- no session
    "GET /healthz": PUBLIC,
    "GET /login": PUBLIC,
    "POST /login": PUBLIC,                 # the owner's display password
    "POST /login/team": PUBLIC,            # the team access code (rate-limited)
    "POST /logout": PUBLIC,                # clears whatever session there is
    "GET /report": PUBLIC,                 # the staff report form (its POST needs the staff key)
    "POST /api/issues/report": PUBLIC,     # staff key checked in the handler (auth.staff_key_ok), not a session
    "POST /api/teams/messages": PUBLIC,    # Microsoft's webhook: the Bot Framework bearer token is the authentication
    "MOUNT /static": PUBLIC,               # the console's own scripts, styles and logo - no data
    # ---- the console page: signed-out visitors are sent to /login by the handler
    "GET /": PAGE,
    # ---- any signed-in role, team included
    "GET /api/me": TEAM_OK,
    "POST /api/chat": TEAM_OK,             # a team session talks to ITS OWN cut-down Jarvis (services/team_sessions.py)
    "POST /api/chat/stream": TEAM_OK,
    "POST /api/conversation/reset": TEAM_OK,
    "POST /api/interrupt": TEAM_OK,
    "GET /api/status": TEAM_OK,            # team receives only TEAM_STATUS_KEYS
    "GET /api/tracking": TEAM_OK,          # Fleet, under the owner's out-of-hours privacy rule; look-ups are logged
    "POST /api/tts": TEAM_OK,
    "POST /api/stt": TEAM_OK,
    "GET /api/voices": TEAM_OK,
    "WS /ws": TEAM_OK,                     # a team session gets its own bus and only TEAM_EVENTS
    "WS /ws/stt": TEAM_OK,
    # drawings on floor plans (services/plan_drawings.py): a team member sees only drawings linked to a job; an ENGINEER may edit
    # them, office may only view and export (the save handler refuses office with 403 - a handler rule, not a route of its own)
    "GET /api/drawings": TEAM_OK,
    "GET /api/drawings/{drawing_id}": TEAM_OK,
    "GET /api/drawings/{drawing_id}/plan": TEAM_OK,
    "GET /api/drawings/{drawing_id}/export/{fmt}": TEAM_OK,
    "POST /api/drawings/{drawing_id}": TEAM_OK,
    # system schematics: list, view (the laid-out drawing) and download SVG / PNG / PDF. No prices on a drawing; a download needs no
    # approval (it sends and changes nothing; it leaves a "What Jarvis did" line). Engineers and office may view and export.
    "GET /api/schematics": TEAM_OK,
    "GET /api/schematics/{drawing_id}": TEAM_OK,
    "GET /api/schematics/{drawing_id}/export/{fmt}": TEAM_OK,
    # ---- owner or manager only (a team session gets 403)
    "GET /api/reply-suggestion": MANAGER_OK,
    "GET /api/reply-suggestions": MANAGER_OK,
    "POST /api/reply-suggestions/forget": MANAGER_OK,
    "DELETE /api/reply-suggestions": MANAGER_OK,
    "POST /api/feedback": MANAGER_OK,
    "POST /api/voice-events": MANAGER_OK,
    "GET /api/quality": MANAGER_OK,
    "GET /api/checks": MANAGER_OK,                          # the question-check scorecard (finance / people detail: owner only)
    "DELETE /api/quality": MANAGER_OK,
    "GET /api/transcript": MANAGER_OK,
    "POST /api/tts/sample": MANAGER_OK,
    "GET /api/documents/{doc_id}/{fmt}": MANAGER_OK,
    "GET /api/images/{image_name}": MANAGER_OK,
    "GET /api/adverts/{advert_name}": MANAGER_OK,
    "POST /api/adverts/{advert_id}/revise": MANAGER_OK,
    "POST /api/brand/logo": MANAGER_OK,
    "GET /api/approvals": MANAGER_OK,                       # approvals: list / inbox / history / edit / retry / dismiss / approve / deny
    "GET /api/approvals/inbox": MANAGER_OK,
    "POST /api/approvals/{action_id}/edit": MANAGER_OK,
    "POST /api/approvals/{action_id}/retry": MANAGER_OK,
    "POST /api/approvals/{action_id}/dismiss": MANAGER_OK,   # hide a failed action (runs nothing)
    "POST /api/approvals/dismiss-failed": MANAGER_OK,
    "GET /api/approvals/history": MANAGER_OK,
    "POST /api/approvals/{action_id}/{decision}": MANAGER_OK,
    "GET /api/activity": MANAGER_OK,                        # "What Jarvis did": everything proposed, changed and decided (read only)
    "POST /api/drawings": MANAGER_OK,                       # drawings: upload a plan / create, delete, and ask Jarvis to propose a
    "DELETE /api/drawings/{drawing_id}": MANAGER_OK,        # layout (a model call) - never team
    "POST /api/drawings/{drawing_id}/propose": MANAGER_OK,
    "GET /api/faults": MANAGER_OK,                          # fault reports (services/faults.py): list, copy as markdown for
    "GET /api/faults/report": MANAGER_OK,                   # Claude Code, mark fixed. Internal only - never sent outside Jarvis
    "GET /api/faults/{fault_id}/report": MANAGER_OK,
    "POST /api/faults/{fault_id}/fixed": MANAGER_OK,
    "GET /api/memory": MANAGER_OK,                          # memory: read and edit
    "POST /api/memory/facts/{fact_id}": MANAGER_OK,
    "DELETE /api/memory/facts/{fact_id}": MANAGER_OK,
    "POST /api/memory/replies/{reply_id}": MANAGER_OK,
    "DELETE /api/memory/replies/{reply_id}": MANAGER_OK,
    "GET /api/entity-notes": MANAGER_OK,                    # customer & site notes (Memory pop-up): read, add, reword, delete,
    "GET /api/entity-notes/{entity_type}/{fsm_id}": MANAGER_OK,  # and Accept / Discard a suggested note - never team
    "POST /api/entity-notes/{entity_type}/{fsm_id}/notes": MANAGER_OK,
    "POST /api/entity-notes/{entity_type}/{fsm_id}/summary": MANAGER_OK,
    "POST /api/entity-notes/entry/{entry_id}": MANAGER_OK,
    "DELETE /api/entity-notes/entry/{entry_id}": MANAGER_OK,
    "POST /api/entity-notes/entry/{entry_id}/{decision}": MANAGER_OK,
    "POST /api/suggestions/refresh": MANAGER_OK,
    "POST /api/suggestions/{key:path}/prepare": MANAGER_OK,   # drafts the work and queues it for approval; runs nothing
    "POST /api/suggestions/{key:path}/snooze": MANAGER_OK,    # Not now: quiet here and in Salts FSM until tomorrow
    "POST /api/suggestions/{key:path}/{decision}": MANAGER_OK,
    "GET /api/issues": MANAGER_OK,
    "POST /api/issues/{issue_id}/resolve": MANAGER_OK,
    "POST /api/issues/{issue_id}/reopen": MANAGER_OK,
    "POST /api/issues/{issue_id}/fix": MANAGER_OK,
    "POST /api/tests/run": MANAGER_OK,
    "POST /api/briefing": MANAGER_OK,                       # finance and staff in the text
    "GET /api/digests": MANAGER_OK,
    "GET /api/digests/{digest_id}": MANAGER_OK,
    "POST /api/digests/now": MANAGER_OK,
    "POST /api/wrapup": MANAGER_OK,
    "GET /api/staff-report-address": MANAGER_OK,            # carries the staff key
    "GET /api/settings": MANAGER_OK,                        # settings / connections: read and write
    "POST /api/settings": MANAGER_OK,                       # (owner-only keys are further limited to the principal owner)
    "POST /api/settings/test/{section}": MANAGER_OK,
    "GET /auth/sage/start": MANAGER_OK,                     # finance connection
    "GET /auth/sage/callback": MANAGER_OK,
    # ---- the principal owner only: house rules change how Jarvis works for everyone (services/rulebook.py). Managers see them in
    # GET /api/memory; only the owner rewords, switches off / on or deletes one (and only the owner approves a new one: main.decide).
    "POST /api/memory/rules/{rule_id}": OWNER_ONLY,
    "POST /api/memory/rules/{rule_id}/{state}": OWNER_ONLY,
    "DELETE /api/memory/rules/{rule_id}": OWNER_ONLY,
    # ---- the principal owner only: forget every note on one customer / site (after a confirm)
    "POST /api/entity-notes/{entity_type}/{fsm_id}/forget": OWNER_ONLY,
    # ---- the principal owner only: the activity list as a CSV file (it leaves the system, so it is the owner's click alone)
    "GET /api/activity/export.csv": OWNER_ONLY,
    # ---- the principal owner only: question checks spend the owner's Claude allowance and decide what "right" means
    "POST /api/checks/run": OWNER_ONLY,
    "POST /api/checks/{check_id}/mark": OWNER_ONLY,
    "POST /api/checks/candidates/{turn_id}": OWNER_ONLY,
    "POST /api/checks/candidates/{turn_id}/dismiss": OWNER_ONLY,
    # ---- the principal owner only: how each van's moving / stopped / parked state was decided (no positions, no homes)
    "GET /api/fleet/diagnostics": OWNER_ONLY,
    # ---- the principal owner only: who may sign in as team - the office code and the engineer code. The role-less POST /
    # DELETE are the ENGINEER code (what the single team code was before the office/engineer split; kept so nothing that set
    # the team code before quietly starts handing out office access).
    "GET /api/team-access": OWNER_ONLY,
    "POST /api/team-access": OWNER_ONLY,
    "DELETE /api/team-access": OWNER_ONLY,
    "POST /api/team-access/{team_role}": OWNER_ONLY,
    "DELETE /api/team-access/{team_role}": OWNER_ONLY,
    # ---- the principal owner only: where each engineer lives (a rounded map point, never the postcode). Sensitive personal
    # data: not for a manager, not for team, and deliberately not a tool.
    "GET /api/engineer-homes": OWNER_ONLY,
    "POST /api/engineer-homes": OWNER_ONLY,
    "DELETE /api/engineer-homes": OWNER_ONLY,
    "POST /api/engineer-homes/radius": OWNER_ONLY,
    "DELETE /api/engineer-homes/{engineer}": OWNER_ONLY,
}


def route_key(kind: str, method: str | None, path: str) -> str:
    """The ROUTE_POLICY key for a route: kind is "http", "websocket" or "mount"."""
    if kind == "websocket":
        return f"WS {path}"
    if kind == "mount":
        return f"MOUNT {path}"
    return f"{(method or 'GET').upper()} {path}"
