"""Who may do what: the role model and the two default-deny allowlists (routes and tools) behind Team mode.

Three roles:

* ``owner``   - the principal owner (display password session, the owner's own Microsoft sign-in, or local-only mode).
* ``manager`` - everyone else who gets in today (managers signed in through Microsoft). Exactly as before.
* ``team``    - engineers and office staff, new. They sign in with a team access code the owner sets (see
                ``services/team_access.py``) and get a cut-down console and a cut-down Jarvis: no Finance, no Approvals,
                no Connections, no memory editing, no settings, no staff-report key, and a brain that can only use the
                read-only operational tools listed in ``TEAM_TOOLS``.

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
    signing in - it labels their requests, it is not a verified identity)."""
    role: str
    name: str = ""
    sid: str = ""

    @property
    def is_team(self) -> bool:
        return self.role == TEAM

    @property
    def label(self) -> str:
        """How a person is named in logs, approval cards and the van-look-up log."""
        return f"{self.name} (team)" if self.role == TEAM and self.name else ROLE_LABEL.get(self.role, self.role)

    @property
    def requester(self) -> str:
        """The stable key a background call is filed under ("" for the owner, managers and Jarvis himself)."""
        return f"team:{self.name.strip().lower()}" if self.role == TEAM else ""


# The caller of the tool call being run right now (set by brain.tools.dispatch for the duration of the handler, so code
# deep inside a tool - the approval queue, the van-look-up log, background calls - can see who asked). None = the owner's
# own conversation, a scheduled job or Jarvis himself, exactly as before Team mode.
current_caller: contextvars.ContextVar[Caller | None] = contextvars.ContextVar("jarvis_caller", default=None)


def clean_name(raw: str) -> str:
    """A display name safe to put in a prompt, a log line and an approval card: letters, digits, spaces and . ' - only."""
    kept = "".join(c for c in (raw or "") if c.isalnum() or c in " .'-")
    return re.sub(r"\s+", " ", kept).strip()[:40]


def role_meets(role: str | None, level: str) -> bool:
    if level in (PUBLIC, PAGE):
        return True
    return role in _RANK and _RANK[role] >= _NEEDS[level]


# --- what each role can see in the console -------------------------------------------------------------------------------
FEATURES = {
    OWNER: {"approvals": True, "finance": True, "connections": True, "memory": True, "comms": True, "issues": True,
            "health": True, "settings_admin": True, "attachments": True, "feedback": True, "team_access": True},
    MANAGER: {"approvals": True, "finance": True, "connections": True, "memory": True, "comms": True, "issues": True,
              "health": True, "settings_admin": True, "attachments": True, "feedback": True, "team_access": False},
    TEAM: {"approvals": False, "finance": False, "connections": False, "memory": False, "comms": False, "issues": False,
           "health": False, "settings_admin": False, "attachments": False, "feedback": False, "team_access": False},
}

# Keys of /api/status a team session receives. An allowlist, so a key added to the status later is withheld from team
# until someone decides it is fine. (`staff`, `overdue_jobs`, `presence`, `voice`, `activity` are not finance, not
# approvals, not settings; `accreditations` is cut down to what/date/days_left by the handler.)
TEAM_STATUS_KEYS = frozenset({"generated_at", "staff", "overdue_jobs", "presence", "voice", "company", "role", "who",
                              "accreditations"})

# What a team session's live connection ever carries: its own turns, and the signal to reconnect after a reload. Anything
# else on the owner's bus (approvals, notifications, proactive posts, display panels, finance...) never reaches it.
TEAM_EVENTS = frozenset({"user_message", "thinking", "delta", "tool", "reply", "error", "stopped", "conversation_reset",
                         "reload"})

# --- tools -----------------------------------------------------------------------------------------------------------------
# The ONLY tools a team session's Jarvis (and a team session's background calls) may use. Read-only operational data,
# plus `log_job`, which queues an approval for a human with the requester recorded (and never auto-runs: the standing
# approvals are skipped for a team requester). Everything else is denied by default - finance, accounts, staff review and
# pay, email, stock values, quotes and contract values, access codes, settings, connections, memory, approvals, the
# engineering agents and pull requests - so a new tool needs a deliberate line here.
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
    "log_job",               # queues a job for approval, requester recorded, never auto-approved
    "run_in_background",     # only for the tools above; forced SILENT; scoped to the requester
    "background_results",    # only the requester's own
})

# Folders of the knowledge base a team session's `knowledge_search` never reads.
TEAM_KB_EXCLUDED = ("private/", "finance/")


def tool_allowed(name: str, caller: Caller | None) -> bool:
    """May ``caller`` use the tool called ``name``? ``None`` (the owner's conversation, a scheduled job) and owner/manager
    callers: yes, as before. A team caller: only if the name is in TEAM_TOOLS (default deny)."""
    if caller is None or caller.role != TEAM:
        return True
    return name in TEAM_TOOLS


def refusal(name: str) -> str:
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
    # ---- owner or manager only (a team session gets 403)
    "GET /api/reply-suggestion": MANAGER_OK,
    "GET /api/reply-suggestions": MANAGER_OK,
    "POST /api/reply-suggestions/forget": MANAGER_OK,
    "DELETE /api/reply-suggestions": MANAGER_OK,
    "POST /api/feedback": MANAGER_OK,
    "POST /api/voice-events": MANAGER_OK,
    "GET /api/quality": MANAGER_OK,
    "DELETE /api/quality": MANAGER_OK,
    "GET /api/transcript": MANAGER_OK,
    "POST /api/tts/sample": MANAGER_OK,
    "GET /api/documents/{doc_id}/{fmt}": MANAGER_OK,
    "GET /api/images/{image_name}": MANAGER_OK,
    "POST /api/brand/logo": MANAGER_OK,
    "GET /api/approvals": MANAGER_OK,                       # approvals: list / inbox / edit / retry / approve / deny
    "GET /api/approvals/inbox": MANAGER_OK,
    "POST /api/approvals/{action_id}/edit": MANAGER_OK,
    "POST /api/approvals/{action_id}/retry": MANAGER_OK,
    "POST /api/approvals/{action_id}/{decision}": MANAGER_OK,
    "GET /api/memory": MANAGER_OK,                          # memory: read and edit
    "POST /api/memory/facts/{fact_id}": MANAGER_OK,
    "DELETE /api/memory/facts/{fact_id}": MANAGER_OK,
    "POST /api/memory/replies/{reply_id}": MANAGER_OK,
    "DELETE /api/memory/replies/{reply_id}": MANAGER_OK,
    "POST /api/suggestions/refresh": MANAGER_OK,
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
    # ---- the principal owner only: who may sign in as team
    "GET /api/team-access": OWNER_ONLY,
    "POST /api/team-access": OWNER_ONLY,
    "DELETE /api/team-access": OWNER_ONLY,
}


def route_key(kind: str, method: str | None, path: str) -> str:
    """The ROUTE_POLICY key for a route: kind is "http", "websocket" or "mount"."""
    if kind == "websocket":
        return f"WS {path}"
    if kind == "mount":
        return f"MOUNT {path}"
    return f"{(method or 'GET').upper()} {path}"
