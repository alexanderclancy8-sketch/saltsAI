"""FastAPI app: the HUD display, chat/voice API, live WebSocket, staff issue reporting."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import html as html_lib

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from starlette.requests import HTTPConnection
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Any

from . import access, auth
from .brain.prompts import address_for
from .config import Settings, get_settings
from .core import Jarvis
from .integrations.finance import SageFinance
from .integrations.stt_chain import SERVER_ENGINES
from .integrations.teamsbot import TeamsBotError, same_service_url, trusted_service_url, verify_activity
from .integrations.voice import STT_ATTEMPT_TIMEOUT_S, STTError, VoiceError
from .redact import install_log_redaction, redact_text
from .services import activity_feed, adverts, approval_inbox, chat_files, connection_tests, documents, images, rulebook
from .services import plan_drawings
from .services.plan_drawings import PlanSourceError, StaleDrawing
from .services.actions import ActionRefused
from .services.memory_book import MemoryBook, MemoryEditError
from .services import entity_memory as entity_mem
from .services.entity_memory import EntityNoteError
from .services.engineer_homes import DEFAULT_RADIUS_M, MAX_RADIUS_M, MIN_RADIUS_M, HomeError
from .services.team_access import CodeRejected
from .services.tracking import requester_label
from .services.teams_approvals import approver_emails, invoke_value, parse_decision_value, parse_typed_command
from .settings_store import AZURE_VOICES, FIELDS, OWNER_IDENTITY_KEYS, OWNER_ONLY_KEYS, SECTIONS_BY_ID, SettingsStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
install_log_redaction()  # no secrets (webhook signatures, tokens, keys) in the log stream - see jarvis/redact.py
log = logging.getLogger("jarvis")
WEB = Path(__file__).parent / "web"
LOGIN_DELAY_S = 1.5  # the pause after a wrong team code (the owner login has its own, fixed one)


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    mode: str = "typed"  # typed | voice
    attachments: list[dict[str, str]] = []
    compose: bool = False  # true only when the owner typed this into the chat box (not a quick button / voice)


class FeedbackIn(BaseModel):
    rating: str = Field(pattern="^(good|wrong)$")
    note: str = Field("", max_length=500)
    turn_id: int | None = None  # default: Jarvis's most recent turn


class CheckMarkIn(BaseModel):
    state: str = Field(pattern="^(obsolete|wrong|clear)$")


class CheckPromoteIn(BaseModel):
    question: str = Field(min_length=3, max_length=400)
    expect: dict[str, Any]
    area: str = Field("other", max_length=20)
    as_role: str = Field("owner", pattern="^(owner|manager|team|office)$")
    needs: list[str] = Field(default_factory=list, max_length=5)


class VoiceEventIn(BaseModel):
    kind: str = Field(max_length=40)  # stt_failure | stt_empty | echo_suppressed | first_audio
    ms: float | None = None           # first_audio only: milliseconds from sending the request to the first sound
    turn_id: int | None = None
    detail: str = Field("", max_length=300)


class IssueResolveIn(BaseModel):
    note: str = Field("", max_length=1000)


class ForgetIn(BaseModel):
    text: str = Field(min_length=1, max_length=200)


class EditActionIn(BaseModel):
    changes: dict[str, Any]


class DismissFailedIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)


class MemoryTextIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class DrawingSaveIn(BaseModel):
    version: int = Field(ge=1)
    meta: dict[str, Any] = Field(default_factory=dict)       # title block fields (validated in services/plan_drawings.clean_meta)
    content: dict[str, Any] | None = None                    # devices / zones / you_are_here / rotation / paper (clean_content)


class DrawingProposeIn(BaseModel):
    brief: str = Field("", max_length=1500)
    kind: str | None = None


class EntityTextIn(BaseModel):
    text: str = Field(max_length=1000)


class ForgetEntityIn(BaseModel):
    confirm: bool = False


class TTSIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    voice_id: str | None = None


class VoiceSampleIn(BaseModel):
    voice: str = Field(max_length=80)


class AdvertReviseIn(BaseModel):
    instructions: str = Field(min_length=1, max_length=500)


class SettingsIn(BaseModel):
    values: dict[str, Any] = {}
    clear: list[str] = []


class TeamCodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=200)


class _NoQuality:
    """Stands in for the conversation-quality recorder where a team member's activity must not be recorded."""

    def record_event(self, *a, **k) -> bool:
        return False

    def note_stt(self, *a, **k) -> None:
        return None


def carry_conversation(old: Jarvis, new: Jarvis) -> None:
    """Keep the conversation going when Jarvis is rebuilt with new settings."""
    if type(old.brain) is type(new.brain):
        new.brain.messages = old.brain.messages
        if hasattr(old.brain, "session_id"):
            new.brain.session_id = old.brain.session_id


def create_app(settings: Settings | None = None, jarvis: Jarvis | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = SettingsStore(settings)  # what the owner saved on the Settings page, over the Azure settings
    # The owner's address as configured OUTSIDE the Settings page (OWNER_EMAIL / .env), captured before any saved
    # override is applied. A Microsoft-signed-in manager counts as "the owner" for the owner-only settings only if
    # they match this - never the live settings.owner_email, which a manager could otherwise edit to their own.
    trusted_owner_email = str(store.base.get("owner_email") or "").strip().lower()
    for key in sorted(OWNER_IDENTITY_KEYS):
        saved = store.overrides.get(key)
        if saved is not None and str(saved).strip().lower() != str(store.base.get(key) or "").strip().lower():
            # A connections.enc from before these became owner-only could hold a value a manager saved. Teams
            # approvers are built from the live value, so the owner should look. (Values are never logged.)
            log.warning("The Settings page holds a saved %s that differs from the app setting / .env value. "
                        "Check it on Settings -> You and the business: Teams approvals use the saved one.", key)
    store.apply()
    reload_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        j = jarvis or Jarvis(settings)
        app.state.j = j
        await j.start()
        if not settings.jarvis_owner_password:
            log.warning("JARVIS_OWNER_PASSWORD is not set - the display only answers requests from this machine.")
        yield
        await app.state.j.stop()

    async def reload_jarvis(app: FastAPI) -> None:
        """Rebuild Jarvis with the new settings and swap it in; open displays reconnect by themselves."""
        async with reload_lock:
            old: Jarvis = app.state.j
            new = Jarvis(settings, db=old.db)
            carry_conversation(old, new)
            app.state.j = new
            await new.start()
            old.bus.publish("reload", {"reason": "settings"})
            await asyncio.sleep(0.1)
            await old.stop()
            log.info("Settings applied; Jarvis reloaded.")

    def caller_of(conn: HTTPConnection) -> access.Caller | None:
        """Who is signed in on this connection: owner, manager, team member, or None. One answer per request, cached."""
        # Cached on the ASGI scope itself (one per connection), never in scope["state"], which a server may share.
        if "jarvis.caller" not in conn.scope:
            j = getattr(conn.app.state, "j", None)
            digests = j.team_codes.digests() if j is not None else {}
            conn.scope["jarvis.caller"] = auth.role_of(settings, conn, trusted_owner_email, digests)
        return conn.scope["jarvis.caller"]

    async def guard(conn: HTTPConnection) -> None:
        """Team mode, enforced on the backend for EVERY route: the matched route is looked up in access.ROUTE_POLICY and the
        request is refused (401 not signed in, 403 not for this role) before any handler runs. A route that is not in the
        table is refused to everyone - default deny - and tests/test_team_mode.py fails if one exists. WebSocket routes check
        the same table in their own handler (they close with 4401 rather than raise)."""
        if conn.scope["type"] != "http":
            return
        route = conn.scope.get("route")
        key = access.route_key("http", conn.scope.get("method"), getattr(route, "path", conn.scope.get("path", "")))
        level = access.ROUTE_POLICY.get(key)
        if level is None:
            raise HTTPException(status_code=403, detail="That isn't available.")
        if level in (access.PUBLIC, access.PAGE):
            return
        caller = caller_of(conn)
        if caller is None:
            raise HTTPException(status_code=401, detail="Not signed in")
        if not access.route_allowed(key, caller):  # the role's level, and (office / engineer) access.OFFICE_ONLY_ROUTES
            raise HTTPException(status_code=403, detail="That isn't available in the team version of Jarvis."
                                if caller.is_team else "Only the owner can do that.")

    # openapi.json would publish the whole route list to anyone; the interactive docs are already switched off.
    app = FastAPI(title="Salts Jarvis", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(guard)])
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    def J(request: Request) -> Jarvis:  # noqa: N802
        return request.app.state.j

    def owner(request: Request) -> None:
        auth.require_owner(settings, request)

    def member(request: Request) -> None:
        """Any signed-in role, team included (routes classified TEAM_OK)."""
        if caller_of(request) is None:
            raise HTTPException(status_code=401, detail="Not signed in")

    def principal(request: Request) -> None:
        """The principal owner only (routes classified OWNER_ONLY)."""
        caller = caller_of(request)
        if caller is None:
            raise HTTPException(status_code=401, detail="Not signed in")
        if caller.role != access.OWNER:
            raise HTTPException(status_code=403, detail="Only the owner can do that.")

    def brain_of(request: Request):
        """(brain, bus) for whoever is asking: the owner's shared ones, or the team session's own private pair."""
        caller = caller_of(request)
        if caller is not None and caller.is_team:
            session = J(request).team_sessions.get(caller)
            return session.brain, session.bus
        return J(request).brain, J(request).bus

    def human_click(request: Request) -> None:
        """CSRF guard (auth.require_same_origin) for every endpoint that changes what Jarvis does or knows."""
        auth.require_same_origin(settings, request)

    def speaker(conn: Request | WebSocket) -> str | None:
        caller = caller_of(conn)
        if caller is not None and caller.is_team:
            return caller.label  # "Sam (team)": named on the turn, in the van look-up log and on any queued request
        email = auth.signed_in_manager(settings, conn)
        return settings.person(email) if email else None

    def mark_manager(caller: access.Caller | None):
        """Managers share the owner's brain, so nothing in the tool layer knows a manager is asking unless the turn says so. Mark it
        (a context variable, copied into the task that runs the turn) so a role-dependent tool - fsm_data's finance / pay / HR
        resources are the owner's alone - can tell. The owner's own turn stays unmarked (None), exactly as before. Returns the token
        to reset, or None."""
        if caller is not None and caller.role == access.MANAGER:
            return access.current_caller.set(caller)
        return None

    login_failures: dict[str, deque] = defaultdict(deque)  # client address -> times of recent wrong team codes

    # ------------------------------------------------------------------ pages
    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        caller = caller_of(request)
        if caller is None:
            return RedirectResponse("/login")
        # The role goes into the page itself (hud.js reads it to build the right console, and it also hides what a role
        # must not see before the first paint). It is only a presentation hint: every route enforces the role itself.
        # A team member's kind (office / engineer) rides along for the top bar's label only: both get the same console.
        team_role = f' data-team-role="{caller.kind}"' if caller.is_team else ""
        page = (WEB / "index.html").read_text(encoding="utf-8").replace(
            '<body class="app"', f'<body class="app" data-role="{caller.role}"{team_role} data-who="{html_lib.escape(caller.name)}"', 1)
        # And the markup itself is cut down for a team member: the Finance, Approvals, Comms, Issues, Health, Memory and
        # Connections sections, the settings that are not theirs and the owner's shortcuts are not in the page they are sent
        # (index.html marks them with role comments: "owner" = only the owner, "manager" = owner and manager, "team" = only
        # team). A manager does not get the owner's Team access controls either.
        cut = {access.OWNER: ("team",), access.MANAGER: ("team", "owner"), access.TEAM: ("manager", "owner")}[caller.role]
        for region in cut:
            page = re.sub(rf"<!--role:{region}-->.*?<!--/role:{region}-->", "", page, flags=re.S)
        page = re.sub(r"<!--/?role:\w+-->", "", page)  # the markers themselves are not part of any page
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/api/me", dependencies=[Depends(member)])
    async def me(request: Request):
        caller = caller_of(request)
        return {"role": caller.role, "name": caller.name, "label": caller.role_label,
                **({"team_role": caller.kind} if caller.is_team else {}), "features": access.FEATURES[caller.role]}

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return FileResponse(WEB / "login.html")

    @app.post("/login")
    async def login(password: str = Form(...)):
        if not auth.check_password(settings, password):
            await asyncio.sleep(1.5)  # slow down guessing
            return RedirectResponse("/login?error=1", status_code=303)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(auth.COOKIE, auth.make_session(settings), max_age=auth.SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=settings.public_base_url.startswith("https"))
        return resp

    @app.post("/login/team")
    async def login_team(request: Request, name: str = Form(""), code: str = Form(...)):
        """Team sign-in: a name and a team access code the owner set in Settings - THE CODE DECIDES THE ROLE (the office code
        gives an office session, the engineer code an engineer one). The cookie it gives is a team session and nothing more
        (auth.read_team_session / access.ROUTE_POLICY). Wrong codes are slowed down and rate-limited."""
        ip = request.client.host if request.client else "?"
        window, now = login_failures[ip], time.time()
        while window and now - window[0] > 900:
            window.popleft()
        if len(window) >= 8:
            return RedirectResponse("/login?team=1&error=wait", status_code=303)
        j = J(request)
        who = access.clean_name(name)
        if not who:
            return RedirectResponse("/login?team=1&error=name", status_code=303)
        team_role = await asyncio.to_thread(j.team_codes.match, code)  # scrypt is slow on purpose: not on the event loop
        if team_role is None:
            window.append(now)
            await asyncio.sleep(LOGIN_DELAY_S)  # slow down guessing
            return RedirectResponse("/login?team=1&error=1", status_code=303)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(auth.TEAM_COOKIE, auth.make_team_session(settings, j.team_codes[team_role].digest(), who, team_role),
                        max_age=auth.TEAM_SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=settings.public_base_url.startswith("https"))
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        # Microsoft sign-in users are signed out of App Service too, or they'd walk straight back in.
        target = "/.auth/logout" if auth.signed_in_manager(settings, request) else "/login"
        resp = RedirectResponse(target, status_code=303)
        resp.delete_cookie(auth.COOKIE)
        resp.delete_cookie(auth.TEAM_COOKIE)
        return resp

    @app.get("/report", response_class=HTMLResponse)
    async def report_page():
        return FileResponse(WEB / "report.html")

    # ------------------------------------------------------------------ chat
    def learn_reply(j: Jarvis, text: str, mode: str, compose: bool, attachments: list | None) -> None:
        """Count a message the owner typed in the chat box towards their usual replies (see
        services/reply_suggestions.py). Must run before the turn so Jarvis's previous reply is the context.
        Never stores spoken text, quick-button text or attachments, and can never break a chat turn."""
        if not compose or mode != "typed" or attachments:
            return
        try:
            j.reply_suggestions.record(text, "typed")
        except Exception as e:  # noqa: BLE001
            log.warning("reply learning skipped: %s", e)

    async def files_for_turn(j: Jarvis, bus, text: str, attachments: list | None, team: bool):
        """(text, attachments, errors) for a chat turn. Attached files are checked and READ here (PDF text or a transcription
        of a scan, Word / Excel / PowerPoint text - see services/chat_files.py), so both brains get the same thing and a refused
        file is explained in words. A team session has no attachments at all."""
        if team or not attachments:
            return text, None, []
        prepared = await chat_files.prepare(j, attachments, bus)
        return text + prepared.notice(), prepared.files, prepared.errors

    @app.post("/api/chat", dependencies=[Depends(member)])
    async def chat(body: ChatIn, request: Request):
        brain, bus = brain_of(request)
        team = caller_of(request).is_team
        if not team:  # a team member's typing is never learned as the owner's usual replies
            learn_reply(J(request), body.text, body.mode, body.compose, body.attachments)
        token = mark_manager(caller_of(request))
        try:
            text, files, errors = await files_for_turn(J(request), bus, body.text, body.attachments, team)
            reply = await brain.ask(text, "voice" if body.mode == "voice" else "typed", files, speaker=speaker(request))
        finally:
            if token is not None:
                access.current_caller.reset(token)
        return {"reply": reply, **({"attachment_errors": errors} if errors else {})}

    # Same conversation, but for when the live WebSocket isn't available (e.g. it dropped and hasn't
    # reconnected yet): word-by-word as Claude generates it, rather than the client waiting on the full
    # reply. Each line is one of the same {"type", "data"} events the WebSocket already streams.
    CHAT_STREAM_EVENTS = {"user_message", "thinking", "delta", "tool", "reply", "error", "stopped", "ask"}
    CHAT_STREAM_TERMINAL = {"reply", "error", "stopped"}

    @app.post("/api/chat/stream", dependencies=[Depends(member)])
    async def chat_stream(body: ChatIn, request: Request):
        j = J(request)
        brain, bus = brain_of(request)  # a team member streams their own session's bus, never the owner's
        team = caller_of(request).is_team
        mode = "voice" if body.mode == "voice" else "typed"
        if not team:
            learn_reply(j, body.text, mode, body.compose, body.attachments)
        q = bus.subscribe()
        token = mark_manager(caller_of(request))
        who = speaker(request)

        async def turn():
            text, files, _ = await files_for_turn(j, bus, body.text, body.attachments, team)
            return await brain.ask(text, mode, files, speaker=who)

        task = asyncio.create_task(turn())
        if token is not None:
            access.current_caller.reset(token)  # (the task has its own copy of the context)

        async def events():
            # The bus carries every turn. This stream is for THIS one, which starts with its own user_message: what comes
            # before it belongs to an older turn that is still winding down (the owner sent a new message mid-reply), and
            # that turn's reply used to end this stream before its own answer began.
            started = False
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), 0.5)
                    except asyncio.TimeoutError:
                        if task.done() and q.empty():
                            break  # the turn is over and said nothing more
                        continue
                    if msg["type"] not in CHAT_STREAM_EVENTS:
                        continue
                    if not started:
                        if msg["type"] != "user_message" or (msg["data"] or {}).get("text") != body.text:
                            continue
                        started = True
                    yield f"data: {json.dumps(msg, default=str)}\n\n"
                    if msg["type"] in CHAT_STREAM_TERMINAL:
                        break
            finally:
                bus.unsubscribe(q)
                if not task.done():
                    task.cancel()
                with contextlib.suppress(BaseException):
                    await task

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/conversation/reset", dependencies=[Depends(member)])
    async def reset(request: Request):
        brain_of(request)[0].reset()  # a team member resets only their own conversation
        return {"ok": True}

    @app.post("/api/interrupt", dependencies=[Depends(member)])
    async def interrupt(request: Request):
        brain, bus = brain_of(request)
        stopped = await brain.interrupt() if hasattr(brain, "interrupt") else False
        bus.publish("stopped", {"stopped": stopped})
        return {"stopped": stopped}

    # Learned replies for the chat box. A suggestion is only text the HUD may put in the input; nothing here sends
    # a message or touches the approval queue.
    @app.get("/api/reply-suggestion", dependencies=[Depends(owner)])
    async def reply_suggestion(request: Request, prefix: str = ""):
        return J(request).reply_suggestions.suggest(prefix[:200])

    @app.get("/api/reply-suggestions", dependencies=[Depends(owner)])
    async def reply_suggestions_learned(request: Request):
        return J(request).reply_suggestions.summary()

    @app.post("/api/reply-suggestions/forget", dependencies=[Depends(owner)])
    async def reply_suggestions_forget(body: ForgetIn, request: Request):
        return {"forgotten": J(request).reply_suggestions.forget(body.text)}

    @app.delete("/api/reply-suggestions", dependencies=[Depends(owner)])
    async def reply_suggestions_clear(request: Request):
        return {"forgotten": J(request).reply_suggestions.clear()}

    # Conversation quality: the owner marks a reply good/wrong (HUD buttons, or by saying "that was wrong" - see
    # services/conversation_quality.py), the browser reports what only it can see (echo it suppressed, time to first
    # audio), and the summary is readable on demand. All of it only writes Jarvis's own metrics tables.
    @app.post("/api/feedback", dependencies=[Depends(owner)])
    async def feedback(body: FeedbackIn, request: Request):
        saved = J(request).quality.feedback(body.rating, body.note, body.turn_id)
        if not saved:
            raise HTTPException(404, "No such turn to give feedback on.")
        return saved

    @app.post("/api/voice-events", dependencies=[Depends(owner)])
    async def voice_event(body: VoiceEventIn, request: Request):
        quality = J(request).quality
        if body.kind == "first_audio":
            if body.ms is None:
                raise HTTPException(400, "first_audio needs ms")
            # Needs the turn it belongs to; untracked turns (e.g. a feedback phrase) have none and are skipped.
            return {"ok": True, "recorded": quality.record_first_audio(body.ms, body.turn_id)}
        if not quality.record_event(body.kind, body.detail):
            raise HTTPException(400, "Unknown event kind.")
        return {"ok": True}

    @app.get("/api/quality", dependencies=[Depends(owner)])
    async def conversation_quality(request: Request, days: int = 7):
        quality = J(request).quality
        days = max(1, min(days, 90))
        return {"days": days, "stats": quality.stats(days), "summary": quality.summary_text(days)}

    @app.delete("/api/quality", dependencies=[Depends(owner)])
    async def conversation_quality_purge(request: Request, older_than_days: int | None = None):
        """Owner-only purge of the conversation-quality records (turn_metrics, voice_events, turn_feedback, which
        hold short excerpts of what was said). No argument deletes everything; `older_than_days=N` only rows older
        than N days (the daily retention job does this with the retention setting, default 90). The full
        conversation in `transcript` is not touched. Not a Jarvis tool: only the owner's own request reaches this."""
        quality = J(request).quality
        removed = quality.purge() if older_than_days is None else quality.prune(older_than_days)
        return {"removed": removed}

    @app.get("/api/transcript", dependencies=[Depends(owner)])
    async def transcript(request: Request):
        from .brain.coverage import loads

        # each reply's coverage line comes back as the stored summary (labels and counts only - brain/coverage.py)
        return [{**r, "coverage": loads(r.get("coverage"))} for r in J(request).db.recent_transcript(40)]

    # ------------------------------------------------------------------ question checks (the accuracy scorecard)
    # Read: owner or manager (finance / people detail is the owner's alone, inside scorecard()). Run / mark / promote: the principal
    # owner only (access.ROUTE_POLICY), each a same-origin click. Nothing here approves, sends or queues anything.
    @app.get("/api/checks", dependencies=[Depends(owner)])
    async def checks_scorecard(request: Request):
        role = caller_of(request).role
        data = await asyncio.to_thread(J(request).question_checks.scorecard, role)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @app.post("/api/checks/run", dependencies=[Depends(principal), Depends(human_click)])
    async def checks_run(request: Request):
        return J(request).question_checks.start_manual()

    @app.post("/api/checks/{check_id}/mark", dependencies=[Depends(principal), Depends(human_click)])
    async def checks_mark(check_id: str, body: CheckMarkIn, request: Request):
        try:
            return J(request).question_checks.mark(check_id, body.state, speaker(request) or "the owner")
        except KeyError:
            raise HTTPException(404, "No such check.") from None

    @app.post("/api/checks/candidates/{turn_id}", dependencies=[Depends(principal), Depends(human_click)])
    async def checks_promote(turn_id: int, body: CheckPromoteIn, request: Request):
        try:
            return J(request).question_checks.promote(turn_id, body.question, body.expect, body.area, body.as_role, body.needs,
                                                      speaker(request) or "the owner")
        except KeyError:
            raise HTTPException(404, "No such reply.") from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None

    @app.post("/api/checks/candidates/{turn_id}/dismiss", dependencies=[Depends(principal), Depends(human_click)])
    async def checks_dismiss(turn_id: int, request: Request):
        J(request).question_checks.dismiss_candidate(turn_id)
        return {"dismissed": turn_id}

    async def _safe(coro, label: str) -> dict[str, Any]:
        # A misconfigured or unreachable connection (a wrong FSM address, Sage down, ...) must degrade
        # gracefully here - it must never take the whole display down with it.
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            log.warning("status %s failed: %s", label, e)
            return {"error": redact_text(f"{type(e).__name__}: {e}")[:200]}

    async def team_status(j: Jarvis, caller: access.Caller) -> dict[str, Any]:
        """The team console's status: only what a team member may see, and ONLY those sources are read - nothing about
        finance, approvals, mail, issues, tests, connections or settings is fetched, then dropped."""
        board, overdue, presence = await asyncio.gather(
            _safe(j.staff.board(), "staff"), _safe(j.staff.overdue_jobs(), "overdue"),
            _safe(j.marketing.overview(30), "marketing"))
        coming = [{"what": t["what"], "date": t["date"], "days_left": t["days_left"]}
                  for t in j.accreditations.status()["timeline"]
                  if t["days_left"] <= 60 and "insurance" not in str(t["what"]).lower()][:6]
        data = {"generated_at": datetime.now().isoformat(timespec="seconds"), "staff": board, "overdue_jobs": overdue,
                "presence": presence, "voice": j.voice.client_config(), "company": settings.company_name,
                "fleet": {"connected": not j.vehicle_tracking_status().startswith(("DEMO", "NOT CONNECTED")), "why": ""},
                "role": caller.role, "team_role": caller.kind, "who": caller.name, "accreditations": coming}
        return {k: v for k, v in data.items() if k in access.TEAM_STATUS_KEYS}

    @app.get("/api/status", dependencies=[Depends(member)])
    async def status(request: Request):
        j = J(request)
        caller = caller_of(request)
        if caller.is_team:
            return await team_status(j, caller)
        if not j.ram.demo:  # RAM is set up: find out (at most every few minutes) whether it actually answers
            await _safe(j.ram.probe(), "RAM Tracking")
        data, presence, customers = await asyncio.gather(
            j.briefings.status(), _safe(j.marketing.overview(30), "marketing"), _safe(j.customers.scores(), "customers"))
        data["inbox"] = {**data.get("inbox", {}), "service": await _safe(j.service_inbox.unread(), "service inbox")}
        data.update(connections=j.connections(), voice=j.voice.client_config(), presence=presence,
                    approvals=approval_inbox.pending_for_display(j.db),
                    activity=j.activity.summary(owner=caller.role == access.OWNER),
                    faults={"open": j.faults.open_count()},   # the Faults rail count (owner / manager; never in the team status)
                    customer_watch=[c for c in customers.get("customers", []) if c["status"] != "healthy"][:6],
                    owner=settings.owner_name, company=settings.company_name, address=address_for(settings),
                    resolved_issues=[j.issues.summary(i) for i in j.db.list_issues("resolved", 5)],
                    accreditations=[t for t in j.accreditations.status()["timeline"] if t["days_left"] <= 60][:6],
                    sage={"configured": isinstance(j.finance, SageFinance),
                          "connected": isinstance(j.finance, SageFinance) and j.finance.connected})
        return data

    @app.get("/api/tracking", dependencies=[Depends(member)])
    async def tracking(request: Request):
        # The Fleet panel. Outside working hours this only shows vans if the owner's setting allows it, and then the
        # look-up is logged against whoever is signed in (the manager's name, or the owner's own display session).
        return await J(request).tracker.live(requester_label(settings, speaker(request)), tool="fleet_panel")

    @app.get("/api/fleet/diagnostics", dependencies=[Depends(principal)])
    async def fleet_diagnostics_view(request: Request):
        # The principal owner only (access.ROUTE_POLICY): per van, RAM's last_event, its age, engineRpm and how Jarvis classified
        # the van and why - no positions, names or homes. The same out-of-hours rule and look-up log as the map.
        data = await J(request).tracker.fleet_diagnostics(requester_label(settings, speaker(request)))
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ voice
    @app.post("/api/tts", dependencies=[Depends(member)])
    async def tts(body: TTSIn, request: Request):
        try:
            stream, mime = await J(request).voice.tts_stream(body.text, body.voice_id)
        except VoiceError as e:
            if settings.effective_tts != "browser":
                log.warning("TTS failed, browser voice used instead: %s", e)
            return JSONResponse({"fallback": "browser", "detail": str(e)}, status_code=503)
        except Exception as e:  # noqa: BLE001 - network trouble reaching the voice service
            log.warning("TTS failed, browser voice used instead: %s", e)
            return JSONResponse({"fallback": "browser", "detail": redact_text(f"{type(e).__name__}: {e}")[:300]},
                                status_code=503)
        return StreamingResponse(stream, media_type=mime)

    @app.post("/api/tts/sample", dependencies=[Depends(owner)])
    async def tts_sample(body: VoiceSampleIn, request: Request):
        """The Settings page's 'Play sample' button: a fixed line in one of the listed Azure voices. It only
        makes audio - it cannot approve, change or send anything."""
        if body.voice not in {v for v, _ in AZURE_VOICES}:
            return JSONResponse({"detail": "Pick one of the listed Azure voices."}, status_code=400)
        try:
            stream, mime = await J(request).voice.azure_sample(body.voice)
        except VoiceError as e:
            return JSONResponse({"detail": redact_text(str(e))[:300]}, status_code=503)
        except Exception as e:  # noqa: BLE001 - network trouble reaching Azure; the type is enough for the log
            log.warning("Voice sample failed: %s", type(e).__name__)
            return JSONResponse({"detail": f"Couldn't reach Azure Speech ({type(e).__name__})."}, status_code=503)
        return StreamingResponse(stream, media_type=mime)

    @app.post("/api/stt", dependencies=[Depends(member)])
    async def stt(request: Request, audio: UploadFile = File(...), engine: str | None = None):
        """engine (optional): try exactly this engine once - the browser drives retry and fallback across
        voice.stt_chain (see web/hud.js). Without it, the configured engine is used with one server-side retry."""
        data = await audio.read()
        j = J(request)
        quality = _NoQuality() if caller_of(request).is_team else j.quality  # never the owner's metrics
        if not data:
            log.warning("STT upload was empty (filename=%s)", audio.filename)
            quality.record_event("stt_empty", "no audio received")
            return JSONResponse({"detail": "No audio was received"}, status_code=400)
        if engine is not None and engine not in SERVER_ENGINES:
            return JSONResponse({"detail": f"Unknown speech-to-text engine '{engine[:20]}'"}, status_code=400)
        started = time.monotonic()
        try:
            if engine:
                text = await j.voice.transcribe(data, audio.content_type or "audio/webm", engine,
                                                retry=False, timeout_s=STT_ATTEMPT_TIMEOUT_S)
            else:
                text = await j.voice.transcribe(data, audio.content_type or "audio/webm")
        except STTError as e:  # the provider failed - already logged in detail; say what went wrong and what to check
            # STTError subclasses VoiceError, so this branch must stay ahead of the VoiceError one.
            quality.record_event("stt_failure", str(e))
            return JSONResponse({"detail": str(e), "provider": e.provider, "upstream_status": e.status,
                                 "transient": e.transient}, status_code=502)
        except VoiceError as e:
            quality.record_event("stt_failure", str(e))
            return JSONResponse({"fallback": "browser", "detail": str(e)}, status_code=503)
        except Exception as e:  # noqa: BLE001 - anything unexpected: log it in full, never a bare 500
            log.exception("STT failed unexpectedly (%d bytes, %s)", len(data), audio.content_type)
            quality.record_event("stt_failure", f"{type(e).__name__}: {e}")
            return JSONResponse({"detail": f"Unexpected speech-to-text error ({type(e).__name__}) - "
                                           "see the Jarvis server log."}, status_code=502)
        log.info("STT ok: %d bytes, %s, %d chars", len(data), audio.content_type, len(text or ""))
        if (text or "").strip():
            quality.note_stt((time.monotonic() - started) * 1000, text)
        else:
            quality.record_event("stt_empty")
        return {"text": text or "", "engine": engine or settings.effective_stt}

    @app.get("/api/voices", dependencies=[Depends(member)])
    async def voices(request: Request):
        return await J(request).voice.list_voices()

    @app.websocket("/ws/stt")
    async def ws_stt(ws: WebSocket):
        if caller_of(ws) is None:  # any signed-in role (access.ROUTE_POLICY: WS /ws/stt)
            await ws.close(code=4401)
            return
        await ws.accept()
        j: Jarvis = ws.app.state.j
        if settings.effective_stt != "deepgram":
            await ws.send_json({"type": "error", "message": "Deepgram is not configured"})
            await ws.close()
            return
        try:
            await j.voice.relay_deepgram(ws)
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("STT relay ended: %s", e)
            try:
                await ws.send_json({"type": "error", "message": "Speech service disconnected"})
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ live events
    @app.websocket("/ws")
    async def ws_events(ws: WebSocket):
        caller = caller_of(ws)
        if caller is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        j: Jarvis = ws.app.state.j
        who = speaker(ws)
        team = caller.is_team
        # The owner's console shares the global bus. A team member's connection reads ONLY its own session's bus (their own
        # turns) plus the global "reload" signal - never approvals, notifications, proactive posts, display or finance events
        # - and the allowlist below is applied on top, so a stray event type could not get through either.
        if team:
            session = j.team_sessions.get(caller)
            brain, bus = session.brain, session.bus
            feeds = [(bus.subscribe(), bus, access.TEAM_EVENTS), (j.bus.subscribe(), j.bus, frozenset({"reload"}))]
        else:
            brain, bus = j.brain, j.bus
            feeds = [(bus.subscribe(), bus, None)]
        running: set[asyncio.Task] = set()
        muted = False  # this session's mute button: Jarvis-initiated messages (see services/proactive.py) are not sent

        async def pump(q, allow):
            while True:
                msg = await q.get()
                if allow is not None and msg["type"] not in allow:
                    continue
                if not access.event_visible(msg, caller.role):  # e.g. a chart of finance / pay / HR figures: the owner's console only
                    continue
                if muted and msg["type"] == "proactive":
                    continue
                await ws.send_json(msg)

        async def listen():
            nonlocal muted
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "chat" and msg.get("text"):
                    if not team:
                        learn_reply(j, str(msg["text"])[:20000], "voice" if msg.get("mode") == "voice" else "typed",
                                    msg.get("compose") is True, msg.get("attachments"))
                    token = mark_manager(caller)
                    async def turn(text=msg["text"][:20000], mode="voice" if msg.get("mode") == "voice" else "typed",
                                   attachments=msg.get("attachments")):
                        text, files, _ = await files_for_turn(j, bus, text, attachments if isinstance(attachments, list)
                                                              else None, team)
                        return await brain.ask(text, mode, files, speaker=who)

                    task = asyncio.create_task(turn())
                    if token is not None:
                        access.current_caller.reset(token)
                    running.add(task)
                    task.add_done_callback(running.discard)
                elif msg.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
                elif msg.get("type") == "proactive_mute":
                    muted = msg.get("muted") is True
                elif msg.get("type") == "stop":
                    stopped = await brain.interrupt() if hasattr(brain, "interrupt") else False
                    bus.publish("stopped", {"stopped": stopped})

        tasks = [asyncio.create_task(pump(q, allow)) for q, _bus, allow in feeds] + [asyncio.create_task(listen())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            for q, source, _allow in feeds:
                source.unsubscribe(q)

    # ------------------------------------------------------------------ drafted documents (PDF / Word)
    @app.get("/api/documents/{doc_id}/{fmt}", dependencies=[Depends(owner)])
    async def download_document(doc_id: str, fmt: str, request: Request):
        renderers = {"pdf": (documents.render_pdf, documents.PDF_MIME),
                     "docx": (documents.render_docx, documents.DOCX_MIME),
                     "xlsx": (documents.render_xlsx, documents.XLSX_MIME)}
        if fmt not in renderers:
            raise HTTPException(404, "No such format - use pdf, docx or xlsx.")
        if not documents.valid_doc_id(doc_id):
            raise HTTPException(400, "Invalid document id")
        doc = J(request).documents.get(doc_id)
        if not doc:
            raise HTTPException(404, "No such document")
        render, mime = renderers[fmt]
        try:
            data = await asyncio.to_thread(render, doc, settings.company_name, settings.company_address,
                                           documents.header_logo(settings.company_logo_path))
        except ImportError:
            raise HTTPException(503, "Document rendering isn't installed on this server.") from None
        filename = documents.download_filename(doc, fmt)
        return Response(data, media_type=mime,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                 "Cache-Control": "no-store"})

    # ------------------------------------------------------------------ a Salts FSM renewal's PDF (services/fsm_renewals.py)
    # The PDF exactly as Salts FSM will attach it, for the link on a renewal's approval card. Owner and managers only; read-only.
    @app.get("/api/fsm/renewals/{renewal_id}/pdf", dependencies=[Depends(owner)])
    async def fsm_renewal_pdf(renewal_id: str, request: Request):
        from .services.fsm_renewals import RenewalsError

        try:
            data = await J(request).fsm_renewals.pdf(renewal_id)
        except RenewalsError as e:
            status = {"bad_request": 400, "not_found": 404, "scope_off": 403, "unavailable": 404, "demo": 503}.get(e.kind, 502)
            raise HTTPException(status, e.message) from None
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": 'inline; filename="renewal.pdf"', "Cache-Control": "no-store",
                                 "X-Content-Type-Options": "nosniff"})

    # ------------------------------------------------------------------ system schematics (services/schematics.py)
    # Every signed-in role (owner, manager, engineer, office) may list, view and download them: a drawing carries no prices, and a
    # download sends and changes nothing (no approval) - it leaves a "What Jarvis did" line. The scene is primitives laid out by code;
    # the console draws it with DOM APIs (web/schematics.js).
    def _who(request: Request) -> str:
        caller = caller_of(request)
        if caller is not None and caller.is_team:
            return caller.label
        return speaker(request) or settings.owner_name or "the owner"

    @app.get("/api/schematics", dependencies=[Depends(member)])
    async def schematics_list(request: Request, site: str = "", kind: str = "", q: str = "", limit: int = 20):
        return JSONResponse({"drawings": J(request).schematics.list(site=site, kind=kind, query=q, limit=limit)},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/schematics/{drawing_id}", dependencies=[Depends(member)])
    async def schematic_view(drawing_id: str, request: Request, rev: int = 0, mode: str = "wide"):
        view = await asyncio.to_thread(J(request).schematics.view, drawing_id, rev or None, mode)
        if view is None:
            raise HTTPException(404, "No such drawing")
        return JSONResponse(view, headers={"Cache-Control": "no-store"})

    @app.get("/api/schematics/{drawing_id}/export/{fmt}", dependencies=[Depends(member)])
    async def schematic_export(drawing_id: str, fmt: str, request: Request, rev: int = 0, paper: str = "a3"):
        from .services import schematics as sch

        if fmt not in sch.EXPORT_FORMATS:
            raise HTTPException(404, "No such format - use svg, png or pdf.")
        out = await asyncio.to_thread(J(request).schematics.export, drawing_id, fmt, rev or None, paper, _who(request))
        if out is None:
            raise HTTPException(404, "No such drawing")
        data, mime, filename = out
        return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                                        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    # ------------------------------------------------------------------ draft social media graphics (PNG)
    @app.get("/api/images/{image_name}", dependencies=[Depends(owner)])
    async def get_image(image_name: str, download: int = 0):
        image_id = image_name[:-4] if image_name.endswith(".png") else ""
        if not images.IMAGE_ID_RE.match(image_id):
            raise HTTPException(404, "No such image")
        path = images.images_dir(settings) / f"{image_id}.png"
        if not path.is_file():
            raise HTTPException(404, "No such image")
        headers = {"Cache-Control": "no-store"}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="salts-draft-post-{image_id[:8]}.png"'
        return Response(path.read_bytes(), media_type="image/png", headers=headers)

    # ------------------------------------------------------------------ Claude-designed adverts (HTML)
    # The stored design is already sanitised (services/adverts.py). The console shows `document` in a sandboxed iframe
    # (sandbox="" + the design's own safety policy) and turns `fragment` into a PNG in the browser. Nothing is rendered here.
    @app.get("/api/adverts/{advert_name}", dependencies=[Depends(owner)])
    async def get_advert(request: Request, advert_name: str, download: int = 0):
        as_html = advert_name.endswith(".html")
        advert_id = advert_name[:-5] if as_html else advert_name
        payload = J(request).adverts.payload(advert_id) if images.IMAGE_ID_RE.match(advert_id) else None
        if payload is None:
            raise HTTPException(404, "No such design")
        if not as_html:
            return JSONResponse(payload, headers={"Cache-Control": "no-store"})
        return Response(payload["document"], media_type="text/html; charset=utf-8",
                        headers=adverts.document_headers(download=bool(download), filename=payload["filename"]))

    @app.post("/api/adverts/{advert_id}/revise", dependencies=[Depends(owner), Depends(human_click)])
    async def revise_advert(request: Request, advert_id: str, body: AdvertReviseIn):
        if not images.IMAGE_ID_RE.match(advert_id):
            raise HTTPException(404, "No such design")
        out = await J(request).adverts.revise(advert_id, body.instructions)
        if out.get("error"):
            return JSONResponse(out, status_code=422)
        return out

    @app.post("/api/brand/logo", dependencies=[Depends(owner)])
    async def upload_logo(logo: UploadFile = File(...)):
        """Supply the company logo once; generated graphics use it from then on (over the bundled Salts logo)."""
        data = await logo.read(images.MAX_LOGO_BYTES + 1)
        try:
            await asyncio.to_thread(images.save_logo, settings, data)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except ImportError:
            raise HTTPException(503, "Image handling isn't installed on this server.") from None
        return {"saved": True, "message": "Logo saved - it will be used on every graphic from now on."}

    # ------------------------------------------------------------------ approvals
    # Every route below that DECIDES or CHANGES something needs the owner's session (401 otherwise) and a same-origin
    # browser click (403 otherwise). Approve / Don't send / Edit / Retry / Dismiss are reachable only from here and from the Teams
    # webhook (Approve / Deny only): no brain tool, standing approval or scheduled job can call them.
    @app.get("/api/approvals", dependencies=[Depends(owner)])
    async def approvals(request: Request):
        return approval_inbox.pending_for_display(J(request).db)

    @app.get("/api/approvals/inbox", dependencies=[Depends(owner)])
    async def approvals_inbox(request: Request):
        """The Approvals pop-up and the chat cards: what is waiting, what failed (retryable), what was decided lately.
        Drawn from redacted `view()`s of the stored actions, so a card shows exactly what will happen, minus secrets."""
        now = datetime.now(timezone.utc)
        return approval_inbox.inbox(J(request).db, (now - timedelta(days=14)).isoformat(timespec="seconds"),
                                    (now - timedelta(hours=24)).isoformat(timespec="seconds"))

    @app.post("/api/approvals/{action_id}/edit", dependencies=[Depends(owner), Depends(human_click)])
    async def edit_action(action_id: int, body: EditActionIn, request: Request):
        """Edit a pending action: queues the validated, edited payload as a NEW pending action (never auto-run) and
        closes the old one. The person still has to press Approve on the new one."""
        try:
            new_id, message = J(request).actions.edit(action_id, body.changes, by=speaker(request))
        except ActionRefused as e:
            raise HTTPException(e.status, str(e)) from None
        return {"id": new_id, "result": message}

    @app.post("/api/approvals/{action_id}/retry", dependencies=[Depends(owner), Depends(human_click)])
    async def retry_action(action_id: int, request: Request):
        """Retry a failed action: queues a copy as a NEW pending action. Nothing runs until Approve is pressed on it."""
        try:
            new_id, message = J(request).actions.retry(action_id, by=speaker(request))
        except ActionRefused as e:
            raise HTTPException(e.status, str(e)) from None
        return {"id": new_id, "result": message}

    @app.post("/api/approvals/{action_id}/dismiss", dependencies=[Depends(owner), Depends(human_click)])
    async def dismiss_action(action_id: int, request: Request):
        """Dismiss a FAILED action: hides it from the failed list and the counts, keeps it in the history. Runs, queues and
        retries nothing and leaves its payload and failed status as they were. Repeating it is harmless (200, `already`)."""
        try:
            newly, message = J(request).actions.dismiss(action_id, by=speaker(request))
        except ActionRefused as e:
            raise HTTPException(e.status, str(e)) from None
        return {"id": action_id, "dismissed": True, "already": not newly, "result": message}

    @app.post("/api/approvals/dismiss-failed", dependencies=[Depends(owner), Depends(human_click)])
    async def dismiss_failed(body: DismissFailedIn, request: Request):
        """"Dismiss all failed": dismisses exactly the failed actions the person saw listed and confirmed (their ids).
        Anything not failed, or already dismissed, is skipped untouched and reported."""
        return J(request).actions.dismiss_many(body.ids, by=speaker(request))

    @app.get("/api/approvals/history", dependencies=[Depends(owner)])
    async def approvals_history(request: Request, limit: int = 100, dismissed: bool = False):
        """Every action in every state, newest first - dismissed failures included and flagged "dismissed by NAME at TIME"."""
        return approval_inbox.history(J(request).db, max(1, min(limit, 500)), dismissed)

    # ------------------------------------------------------------------ what Jarvis did (read only)
    # One list of everything Jarvis proposed, drafted, sent or changed and what a person decided (services/activity_feed.py). It
    # approves, sends and changes nothing. Owner or manager session (a team session gets 403); the CSV is the principal owner's alone.
    def activity_query(request: Request, rng: str, kind: str, status: str, who: str, text: str, everything: bool) -> activity_feed.Query:
        feed = J(request).activity_feed
        try:
            since, until, _ = feed.window(rng)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        csv_ = lambda v: [x.strip() for x in str(v or "").split(",") if x.strip()]  # noqa: E731
        return activity_feed.Query(since, until, kinds=csv_(kind), statuses=csv_(status), who=who, text=text, everything=everything,
                                   owner=caller_of(request).role == access.OWNER)

    @app.get("/api/activity", dependencies=[Depends(owner), Depends(human_click)])
    async def activity_list(request: Request, range: str = "today", kind: str = "", status: str = "", who: str = "", q: str = "",
                            everything: bool = False, limit: int = activity_feed.DEFAULT_LIMIT, offset: int = 0):
        query = activity_query(request, range, kind, status, who, q, everything)
        data = await asyncio.to_thread(J(request).activity_feed.page, query, limit, offset)
        data["can_export"] = caller_of(request).role == access.OWNER
        data["range"] = range
        data["limits"] = {"page_max": activity_feed.PAGE_MAX, "reach": activity_feed.REACH_CAP}
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @app.get("/api/activity/export.csv", dependencies=[Depends(principal), Depends(human_click)])
    async def activity_export(request: Request, range: str = "today", kind: str = "", status: str = "", who: str = "", q: str = "",
                              everything: bool = False):
        query = activity_query(request, range, kind, status, who, q, everything)
        body, cut = await asyncio.to_thread(J(request).activity_feed.export_csv, query, "the owner")
        headers = {"Content-Disposition": f'attachment; filename="what-jarvis-did-{range}.csv"', "Cache-Control": "no-store",
                   "X-Content-Type-Options": "nosniff", "X-Export-Truncated": "true" if cut else "false"}
        return Response("﻿" + body, media_type="text/csv; charset=utf-8", headers=headers)

    @app.post("/api/approvals/{action_id}/{decision}", dependencies=[Depends(owner), Depends(human_click)])
    async def decide(action_id: int, decision: str, request: Request):
        j = J(request)
        if decision == "approve":
            pending = j.db.get_action(action_id)
            # A house rule changes how Jarvis works for everyone: only the principal owner approves one (services/rulebook.py).
            if pending and pending["kind"] in rulebook.OWNER_APPROVAL_KINDS and caller_of(request).role != access.OWNER:
                raise HTTPException(403, "Only the owner can approve a house rule.")
            return {"result": await j.actions.approve(action_id, by=speaker(request))}
        if decision == "deny":
            return {"result": await j.actions.deny(action_id, by=speaker(request))}
        raise HTTPException(400, "decision must be approve or deny")

    # ------------------------------------------------------------------ fault reports (the Faults pop-up; services/faults.py)
    # Owner or manager (MANAGER_OK; a team session gets 403). Internal only: the report is built here for a PERSON to copy into
    # Claude Code - nothing here (or anywhere) sends it to GitHub or outside Jarvis. "Mark fixed" is a same-origin click.
    @app.get("/api/faults", dependencies=[Depends(owner)])
    async def faults_list(request: Request):
        return JSONResponse(J(request).faults.listing(), headers={"Cache-Control": "no-store"})

    @app.get("/api/faults/report", dependencies=[Depends(owner), Depends(human_click)])
    async def faults_report_all(request: Request):
        faults = J(request).faults
        rows = faults.open_faults()
        return JSONResponse({"markdown": faults.report_markdown(rows), "count": len(rows)}, headers={"Cache-Control": "no-store"})

    @app.get("/api/faults/{fault_id}/report", dependencies=[Depends(owner), Depends(human_click)])
    async def faults_report_one(fault_id: int, request: Request):
        faults = J(request).faults
        row = faults.get(fault_id)
        if row is None:
            raise HTTPException(404, "That fault no longer exists.")
        return JSONResponse({"markdown": faults.report_markdown([row])}, headers={"Cache-Control": "no-store"})

    @app.post("/api/faults/{fault_id}/fixed", dependencies=[Depends(owner), Depends(human_click)])
    async def faults_mark_fixed(fault_id: int, request: Request):
        try:
            return J(request).faults.mark_fixed(fault_id, speaker(request) or "the owner")
        except LookupError as e:
            raise HTTPException(404, str(e)) from None

    # ------------------------------------------------------------------ drawings on floor plans (the Drawings pop-up; services/plan_drawings.py)
    # Jarvis proposes, a person adjusts, then exports. Owner / managers: everything. Team: the drawings linked to a job - an engineer
    # may open, edit and export them, office may open and export them (the save handler refuses office: PermissionError -> 403).
    # Upload, delete and "Ask Jarvis to propose" are MANAGER_OK. Every change is a same-origin click; every save / export / create /
    # delete is a "drawing" line in What Jarvis did. Nothing here sends or attaches a drawing anywhere: downloading is the only way out.
    nostore = {"Cache-Control": "no-store"}

    def drawing_or_404(request: Request, drawing_id: str) -> dict[str, Any]:
        row = J(request).drawings._row(drawing_id)
        if row is None or not J(request).drawings.may_view(caller_of(request), row):
            raise HTTPException(404, "That drawing doesn't exist (or isn't one you can open).")
        return row

    @app.get("/api/drawings", dependencies=[Depends(member)])
    async def drawings_list(request: Request):
        return JSONResponse(J(request).drawings.listing(caller_of(request)), headers=nostore)

    @app.post("/api/drawings", dependencies=[Depends(owner), Depends(human_click)])
    async def drawings_create(request: Request, plan: UploadFile = File(...), kind: str = Form("devices"), page: int = Form(1),
                              title: str = Form(""), site_name: str = Form(""), job_ref: str = Form(""), address: str = Form(""),
                              panel_location: str = Form("")):
        dr = J(request).drawings
        if kind not in plan_drawings.KINDS:
            raise HTTPException(400, "Choose a device layout or a zone chart.")
        raw = await plan.read(plan_drawings.MAX_UPLOAD_BYTES + 1)
        name = plan.filename or "plan"
        try:
            img = await dr.plan_from_upload(raw, name, page)
        except PlanSourceError as e:
            raise HTTPException(422, str(e)) from None
        by = speaker(request) or "the owner"
        where = f" (page {img.page} of {img.pages})" if img.pages > 1 else ""
        plan_id = dr.add_plan(img, f"Uploaded: {plan_drawings.clean_text(name, 80)}{where}", by)
        row = dr.create(kind=kind, plan_id=plan_id, by=by, meta={"title": title, "site_name": site_name, "job_ref": job_ref,
                                                                 "address": address, "panel_location": panel_location})
        return JSONResponse(dr.view(row, caller_of(request)), headers=nostore)

    @app.get("/api/drawings/{drawing_id}", dependencies=[Depends(member)])
    async def drawings_get(request: Request, drawing_id: str):
        row = drawing_or_404(request, drawing_id)
        return JSONResponse(J(request).drawings.view(row, caller_of(request)), headers=nostore)

    @app.get("/api/drawings/{drawing_id}/plan", dependencies=[Depends(member)])
    async def drawings_plan(request: Request, drawing_id: str):
        row = drawing_or_404(request, drawing_id)
        plan = J(request).drawings.plan_image(row["plan_id"])
        if plan is None:
            raise HTTPException(404, "This drawing's plan isn't stored any more.")
        return Response(plan.data, media_type=plan.mime, headers={"Cache-Control": "private, max-age=3600",
                                                                  "X-Content-Type-Options": "nosniff"})

    @app.post("/api/drawings/{drawing_id}", dependencies=[Depends(member), Depends(human_click)])
    async def drawings_save(request: Request, drawing_id: str, body: DrawingSaveIn):
        try:
            out = J(request).drawings.save(drawing_id, body.model_dump(), caller_of(request), speaker(request) or "the owner")
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        except PermissionError as e:
            raise HTTPException(403, str(e)) from None
        except StaleDrawing as e:
            raise HTTPException(409, str(e)) from None
        return JSONResponse(out, headers=nostore)

    @app.delete("/api/drawings/{drawing_id}", dependencies=[Depends(owner), Depends(human_click)])
    async def drawings_delete(request: Request, drawing_id: str):
        try:
            J(request).drawings.delete(drawing_id, speaker(request) or "the owner")
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        return {"deleted": True}

    @app.post("/api/drawings/{drawing_id}/propose", dependencies=[Depends(owner), Depends(human_click)])
    async def drawings_propose(request: Request, drawing_id: str, body: DrawingProposeIn):
        """Jarvis's first draft for the editor to load. It is NOT saved: the person adjusts it and presses Save."""
        dr = J(request).drawings
        row = drawing_or_404(request, drawing_id)
        plan = dr.plan_image(row["plan_id"])
        if plan is None:
            raise HTTPException(404, "This drawing's plan isn't stored any more.")
        kind = body.kind if body.kind in plan_drawings.KINDS else row["kind"]
        out = await dr.propose(plan, kind, body.brief)
        if "error" in out:
            return JSONResponse(out, status_code=422, headers=nostore)
        dr.record(speaker(request) or "the owner", f"Jarvis proposed a {plan_drawings.KIND_LABEL[kind].lower()} for drawing "
                                                   f"D{row['id']}: {row['title']} (not saved until a person saves it)",
                  f"drawing D{row['id']}")
        return JSONResponse(out, headers=nostore)

    @app.get("/api/drawings/{drawing_id}/export/{fmt}", dependencies=[Depends(member), Depends(human_click)])
    async def drawings_export(request: Request, drawing_id: str, fmt: str, paper: str = "A3"):
        if fmt not in plan_drawings.FORMATS:
            raise HTTPException(404, "Export as pdf or png.")
        row = drawing_or_404(request, drawing_id)
        try:
            data, mime, filename = await asyncio.to_thread(J(request).drawings.export, row, fmt, paper.upper(),
                                                           speaker(request) or "the owner")
        except LookupError as e:
            raise HTTPException(404, str(e)) from None
        except plan_drawings.FileProblem as e:
            raise HTTPException(503, e.message) from None
        except ImportError:
            raise HTTPException(503, "Drawing export isn't installed on this server.") from None
        return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{filename}"', **nostore})

    # ------------------------------------------------------------------ memory (the Memory pop-up)
    # What Jarvis has learned: list / reword / delete. Console-only (owner session + same-origin click); not a brain
    # tool. The "Things Jarvis should know" setting is rewritten through the same SettingsStore the Settings page uses.
    def memory_book(request: Request) -> MemoryBook:
        def save_notes(lines: list[str]) -> None:
            errors = store.update({"jarvis_notes": "\n".join(lines)}, [])
            if errors:
                raise MemoryEditError(next(iter(errors.values())))

        return MemoryBook(J(request), save_notes)

    def memory_call(fn, *args):
        try:
            return fn(*args)
        except MemoryEditError as e:
            raise HTTPException(e.status, str(e)) from None

    @app.get("/api/memory", dependencies=[Depends(owner)])
    async def memory_list(request: Request):
        data = memory_book(request).listing()
        data["can_edit_rules"] = caller_of(request).role == access.OWNER   # house rules: the principal owner changes them
        return data

    @app.post("/api/memory/facts/{fact_id}", dependencies=[Depends(owner), Depends(human_click)])
    async def memory_edit_fact(fact_id: int, body: MemoryTextIn, request: Request):
        result = memory_call(memory_book(request).edit_fact, fact_id, body.text)
        J(request).activity_feed.record("memory", speaker(request) or "the owner", f"Reworded remembered fact #{fact_id}")
        return result

    @app.delete("/api/memory/facts/{fact_id}", dependencies=[Depends(owner), Depends(human_click)])
    async def memory_delete_fact(fact_id: int, request: Request):
        memory_call(memory_book(request).delete_fact, fact_id)
        J(request).activity_feed.record("memory", speaker(request) or "the owner", f"Removed remembered fact #{fact_id}")
        return {"deleted": fact_id}

    # House rules (services/rulebook.py): listed by GET /api/memory for owner and managers; reworded, switched off / on or deleted by
    # the PRINCIPAL OWNER only (OWNER_ONLY in access.ROUTE_POLICY). The owner is the human, so an edit takes effect directly - after the
    # same safety screen a proposal gets. Every change is a "Rule changed / removed" line in "What Jarvis did".
    def rule_call(fn, *args):
        try:
            return fn(*args)
        except rulebook.RuleError as e:
            raise HTTPException(e.status, str(e)) from None

    @app.post("/api/memory/rules/{rule_id}", dependencies=[Depends(principal), Depends(human_click)])
    async def memory_edit_rule(rule_id: int, body: MemoryTextIn, request: Request):
        return rule_call(J(request).rulebook.edit, rule_id, body.text, speaker(request) or "the owner")

    @app.post("/api/memory/rules/{rule_id}/{state}", dependencies=[Depends(principal), Depends(human_click)])
    async def memory_switch_rule(rule_id: int, state: str, request: Request):
        if state not in ("on", "off"):
            raise HTTPException(400, "state must be on or off")
        return rule_call(J(request).rulebook.set_active, rule_id, state == "on", speaker(request) or "the owner")

    @app.delete("/api/memory/rules/{rule_id}", dependencies=[Depends(principal), Depends(human_click)])
    async def memory_delete_rule(rule_id: int, request: Request):
        rule_call(J(request).rulebook.delete, rule_id, speaker(request) or "the owner")
        return {"deleted": rule_id}

    @app.post("/api/memory/replies/{reply_id}", dependencies=[Depends(owner), Depends(human_click)])
    async def memory_edit_reply(reply_id: int, body: MemoryTextIn, request: Request):
        result = memory_call(memory_book(request).edit_reply, reply_id, body.text)
        J(request).activity_feed.record("memory", speaker(request) or "the owner", f"Reworded learned reply #{reply_id}")
        return result

    @app.delete("/api/memory/replies/{reply_id}", dependencies=[Depends(owner), Depends(human_click)])
    async def memory_delete_reply(reply_id: int, request: Request):
        memory_call(memory_book(request).delete_reply, reply_id)
        J(request).activity_feed.record("memory", speaker(request) or "the owner", f"Removed learned reply #{reply_id}")
        return {"deleted": reply_id}

    # ------------------------------------------------------------------ customer & site notes (Memory pop-up > Customers & sites)
    # Owner and manager (MANAGER_OK in access.ROUTE_POLICY; a team session gets 403 before the handler runs), and forgetting
    # everything on one customer is the principal owner's alone. Every change is a same-origin click. Accept / Discard of a
    # suggested note is ONLY here - no brain tool reaches it. "What Jarvis did" records the customer's name and who, never the text.
    def entity_call(fn, *args):
        try:
            return fn(*args)
        except EntityNoteError as e:
            raise HTTPException(e.status, str(e)) from None

    def who_and_role(request: Request) -> tuple[str, str]:
        caller = caller_of(request)
        return (speaker(request) or "the owner"), (caller.role if caller is not None else access.MANAGER)

    @app.get("/api/entity-notes", dependencies=[Depends(owner)])
    async def entity_notes_list(request: Request, q: str = ""):
        q = q[:120]
        out = J(request).entity_memory.listing(q)
        out["fsm_matches"] = await J(request).entity_memory.search_fsm(q) if q.strip() else []
        return out

    @app.post("/api/entity-notes/entry/{entry_id}", dependencies=[Depends(owner), Depends(human_click)])
    async def entity_note_edit(entry_id: int, body: EntityTextIn, request: Request):
        return entity_call(J(request).entity_memory.edit_entry, entry_id, body.text, who_and_role(request)[0])

    @app.delete("/api/entity-notes/entry/{entry_id}", dependencies=[Depends(owner), Depends(human_click)])
    async def entity_note_delete(entry_id: int, request: Request):
        entity_call(J(request).entity_memory.delete_entry, entry_id, who_and_role(request)[0])
        return {"deleted": entry_id}

    @app.post("/api/entity-notes/entry/{entry_id}/{decision}", dependencies=[Depends(owner), Depends(human_click)])
    async def entity_note_decide(entry_id: int, decision: str, request: Request):
        who, role = who_and_role(request)
        return entity_call(J(request).entity_memory.decide, entry_id, decision, who, role)

    @app.get("/api/entity-notes/{entity_type}/{fsm_id}", dependencies=[Depends(owner)])
    async def entity_notes_view(entity_type: str, fsm_id: str, request: Request):
        return entity_call(J(request).entity_memory.entity_view, entity_type, fsm_id)

    @app.post("/api/entity-notes/{entity_type}/{fsm_id}/notes", dependencies=[Depends(owner), Depends(human_click)])
    async def entity_notes_add(entity_type: str, fsm_id: str, body: EntityTextIn, request: Request):
        who, role = who_and_role(request)
        try:
            return await J(request).entity_memory.console_add(entity_type, fsm_id, body.text, who, role)
        except EntityNoteError as e:
            raise HTTPException(e.status, str(e)) from None

    @app.post("/api/entity-notes/{entity_type}/{fsm_id}/summary", dependencies=[Depends(owner), Depends(human_click)])
    async def entity_notes_summary(entity_type: str, fsm_id: str, body: EntityTextIn, request: Request):
        return entity_call(J(request).entity_memory.set_summary, entity_type, fsm_id, body.text, who_and_role(request)[0])

    @app.post("/api/entity-notes/{entity_type}/{fsm_id}/forget", dependencies=[Depends(principal), Depends(human_click)])
    async def entity_notes_forget(entity_type: str, fsm_id: str, body: ForgetEntityIn, request: Request):
        if not body.confirm:
            raise HTTPException(400, "Confirm first: this forgets every note on them.")
        entity_call(J(request).entity_memory.forget_all, entity_type, fsm_id, who_and_role(request)[0])
        return {"forgotten": True}

    # ------------------------------------------------------------------ suggestions
    @app.post("/api/suggestions/refresh", dependencies=[Depends(owner)])
    async def refresh_suggestions(request: Request):
        return await J(request).suggestions.sweep(announce=False)

    # Prepare and Not now on a suggestion (services/fsm_suggestions.py). Registered BEFORE the catch-all below. Each needs the
    # signed-in owner/manager AND a click from this console (same-origin); the model has no tool for either. Prepare only DRAFTS and
    # queues an approval - it approves, sends and runs nothing, and standing approvals are not consulted.
    @app.post("/api/suggestions/{key:path}/prepare", dependencies=[Depends(owner), Depends(human_click)])
    async def prepare_suggestion(key: str, request: Request):
        j = J(request)
        row = j.db.get_suggestion(key)
        if not row or not row.get("kind"):
            raise HTTPException(404, "No such suggestion")
        return await j.fsm_suggestions.prepare_suggestion(key, by="the console", report=True)

    @app.post("/api/suggestions/{key:path}/snooze", dependencies=[Depends(owner), Depends(human_click)])
    async def snooze_suggestion(key: str, request: Request):
        if not await J(request).fsm_suggestions.snooze_suggestion(key):
            raise HTTPException(404, "No such suggestion")
        return {"snoozed": key}

    @app.post("/api/suggestions/{key:path}/{decision}", dependencies=[Depends(owner)])
    async def decide_suggestion(key: str, decision: str, request: Request):
        if decision not in ("done", "dismissed"):
            raise HTTPException(400, "decision must be done or dismissed")
        s = J(request).suggestions.decide(key, decision)
        if not s:
            raise HTTPException(404, "No such suggestion")
        return {"prompt": s["prompt"]}

    # ------------------------------------------------------------------ issues
    report_times: dict[str, deque] = defaultdict(deque)

    @app.post("/api/issues/report")
    async def report_issue(request: Request, name: str = Form(...), title: str = Form(...),
                           description: str = Form(...), severity: str = Form("medium"),
                           system: str = Form("Salts FSM"), email: str = Form(""), key: str = Form(""),
                           screenshot: UploadFile | None = File(None)):
        if not auth.staff_key_ok(settings, key, request):
            raise HTTPException(403, "Invalid reporting link - ask the office for the current one.")
        ip = request.client.host if request.client else "?"
        window = report_times[ip]
        now = time.time()
        while window and now - window[0] > 3600:
            window.popleft()
        if len(window) >= 15:
            raise HTTPException(429, "Too many reports from this device - try again later.")
        window.append(now)
        image, mime = None, ""
        if screenshot is not None and screenshot.filename:
            image = await screenshot.read(8_000_001)
            mime = screenshot.content_type or ""
            if len(image) > 8_000_000:
                raise HTTPException(413, "Screenshot too large (max 8 MB)")
        severity = severity if severity in ("low", "medium", "high", "critical") else "medium"
        issue = await J(request).issues.report(reporter=name[:80], title=title[:200], description=description[:8000],
                                               severity=severity, system=system[:80], reporter_email=email[:200],
                                               image=image, image_mime=mime, source="web")
        return {"id": issue["id"], "message": f"Thanks {name.split()[0]} - issue #{issue['id']} logged. "
                                              f"{settings.owner_name} has been notified."}

    @app.get("/api/issues", dependencies=[Depends(owner)])
    async def list_issues(request: Request, status: str = "open"):
        j = J(request)
        return [j.issues.summary(i) for i in j.db.list_issues(None if status == "all" else status, 100)]

    # Closing / reopening an issue is bookkeeping, not an approval: it never touches the actions queue.
    def _issue_actor(request: Request) -> str:
        return speaker(request) or settings.owner_name

    @app.post("/api/issues/{issue_id}/resolve", dependencies=[Depends(owner)])
    async def resolve_issue(issue_id: int, request: Request, body: IssueResolveIn | None = None):
        try:
            issue = J(request).issues.mark_resolved(issue_id, by=_issue_actor(request),
                                                    note=body.note if body else "")
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return J(request).issues.summary(issue)

    @app.post("/api/issues/{issue_id}/reopen", dependencies=[Depends(owner)])
    async def reopen_issue(issue_id: int, request: Request):
        try:
            issue = J(request).issues.reopen(issue_id, by=_issue_actor(request))
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return J(request).issues.summary(issue)

    @app.post("/api/issues/{issue_id}/fix", dependencies=[Depends(owner)])
    async def fix_issue(issue_id: int, request: Request):
        j = J(request)
        j.issues._spawn(j.fixer.attempt(issue_id))
        return {"started": True}

    # ------------------------------------------------------------------ tests / reports
    @app.post("/api/tests/run", dependencies=[Depends(owner)])
    async def run_tests(request: Request, suite: str = "all"):
        return await J(request).tester.run(suite if suite in ("system", "compliance", "all") else "all")

    @app.post("/api/briefing", dependencies=[Depends(owner)])
    async def briefing(request: Request):
        return {"text": await J(request).briefings.morning_briefing(deliver=False)}

    @app.get("/api/digests", dependencies=[Depends(owner)])
    async def digests(request: Request, limit: int = 20):
        return J(request).db.list_digests(max(1, min(limit, 100)))

    @app.get("/api/digests/{digest_id}", dependencies=[Depends(owner)])
    async def digest(digest_id: int, request: Request):
        d = J(request).db.get_digest(digest_id)
        if not d:
            raise HTTPException(404, "No such digest")
        return d

    @app.post("/api/digests/now", dependencies=[Depends(owner)])
    async def digest_now(request: Request):
        return await J(request).weekly_digest.run("on_demand", deliver=False)

    @app.post("/api/wrapup", dependencies=[Depends(owner)])
    async def wrapup(request: Request):
        return {"text": await J(request).wrapup.run(deliver=False)}

    # ------------------------------------------------------------------ Teams chat (Bot Framework webhook)
    async def _handle_teams_message(j: Jarvis, service_url: str, conversation_id: str, text: str, name: str,
                                    manager: bool = False) -> None:
        if manager:  # the partner or another approver, not the owner themself (see mark_manager)
            access.current_caller.set(access.Caller(access.MANAGER, name))
        entity_mem.turn_channel.set(entity_mem.TEAMS)  # customer / site notes are never read into a turn that answers in Teams
        try:
            reply = await j.brain.ask(text, "typed", speaker=name)
            await j.teamsbot.reply(service_url, conversation_id, reply)
        except Exception as e:  # noqa: BLE001
            log.exception("Teams chat reply failed")
            try:
                await j.teamsbot.reply(service_url, conversation_id,
                                       "Something went wrong my end - please try again.")
            except Exception:  # noqa: BLE001
                pass

    async def _teams_say(j: Jarvis, service_url: str, conversation_id: str, text: str) -> None:
        try:
            await j.teamsbot.reply(service_url, conversation_id, text)
        except Exception as e:  # noqa: BLE001
            log.warning("Teams reply failed (%s)", type(e).__name__)

    async def _teams_decide(j: Jarvis, decision: str, action_id: int, who: str) -> str:
        """Approve or deny straight through the ActionExecutor, as `who` (an allowlisted, JWT-verified sender)."""
        if decision == "approve":
            pending = j.db.get_action(action_id)
            if pending and pending["kind"] in rulebook.OWNER_APPROVAL_KINDS:   # house rules: the owner, on the console only
                return f"Action #{action_id} is a house rule - only the owner can approve it, on the console. Nothing was approved."
            result = await j.actions.approve(action_id, by=who)
            done = result.startswith("Approved action #")
        else:
            result = await j.actions.deny(action_id, by=who)
            done = result.startswith("Cancelled action #")
        if done and j.actions.teams_approvals is not None:
            return f"{j.actions.teams_approvals.stamp(who, 'Approved' if decision == 'approve' else 'Denied')} " \
                   f"(action #{action_id})."
        return result

    @app.post("/api/teams/messages")
    async def teams_messages(request: Request):
        # Microsoft calls this directly - there's no session cookie, so the bearer token IS the authentication.
        j = J(request)
        try:
            claims = await verify_activity(request.headers.get("authorization"), settings.teams_bot_app_id, j.http)
        except TeamsBotError as e:
            log.warning("Rejected a Teams request: %s", e)
            raise HTTPException(401, "invalid token") from None
        try:
            activity = await request.json()
        except ValueError:
            return {}
        if not isinstance(activity, dict):
            return {}
        kind = activity.get("type")
        is_invoke = kind == "invoke" and activity.get("name") == "adaptiveCard/action"  # Action.Execute buttons
        if not is_invoke and (kind != "message" or not (activity.get("text") or activity.get("value"))):
            return {}
        service_url = activity.get("serviceUrl", "")
        if not trusted_service_url(service_url):
            log.warning("Rejected a Teams activity with an untrusted serviceUrl")
            return {}
        # Bot Framework signs the serviceUrl into the token ("serviceurl" claim): if the body names a different one
        # (a replayed token with a swapped URL), refuse before anything is posted there.
        signed_url = claims.get("serviceurl") if isinstance(claims, dict) else None
        if signed_url and not same_service_url(signed_url, service_url):
            log.warning("Rejected a Teams activity whose serviceUrl differs from the one in its token")
            return {}
        conversation = activity.get("conversation") or {}
        conversation_id = conversation.get("id")
        from_id = (activity.get("from") or {}).get("id")
        if not conversation_id or not from_id:
            return {}
        email = await j.teamsbot.sender_email(service_url, conversation_id, from_id)
        # The same allowlist that decides who may chat decides who may approve (and who gets approval cards).
        if not email or email.lower() not in approver_emails(settings):
            log.info("Ignored a Teams message from an unrecognised account")
            return {}
        name = settings.person(email)
        if j.actions.teams_approvals is not None:
            # They've messaged the bot, so Jarvis now knows where to send them approvals (one-to-one chats only).
            j.actions.teams_approvals.remember(email, service_url, conversation_id, conversation.get("conversationType"))

        # --- Approvals are decided here, deterministically. The language model is never involved, and nothing
        # below this block can approve anything. A card button press first:
        value = invoke_value(activity) if is_invoke else activity.get("value")
        state, decision, action_id = parse_decision_value(value) if value is not None else ("other", "", 0)
        if state != "other":
            if state == "bad":
                text = "I couldn't read that button press, so I did nothing. Reply 'approve 12' or 'deny 12' instead."
            else:
                text = await _teams_decide(j, decision, action_id, name)
            if is_invoke:
                return JSONResponse({"statusCode": 200, "type": "application/vnd.microsoft.activity.message",
                                     "value": text})
            await _teams_say(j, service_url, conversation_id, text)
            return {}
        if is_invoke or not activity.get("text"):
            return {}  # some other card's button, or an empty message: nothing for us to do, never the brain

        # ... then the exact typed commands "approve 12" / "deny #12":
        command = parse_typed_command(activity["text"]) if isinstance(activity["text"], str) else None
        if command:
            await _teams_say(j, service_url, conversation_id, await _teams_decide(j, command[0], command[1], name))
            return {}

        # Everything else is an ordinary chat message for Jarvis.
        text = str(activity["text"]).strip()[:20000]
        asyncio.create_task(_handle_teams_message(j, service_url, conversation_id, text, name,
                                                  manager=not (trusted_owner_email and email.lower() == trusted_owner_email)))
        return {}

    # ------------------------------------------------------------------ settings page
    def settings_view(j: Jarvis, request: Request | None = None) -> dict[str, Any]:
        base = settings.public_base_url.rstrip("/")
        context = {"base_url": base, "app_name": os.environ.get("WEBSITE_SITE_NAME", "salts-jarvis")}
        data = store.view(j.db, context)
        data["context"] = {
            **context,
            # The staff report link carries the staff key, so it is NOT part of this payload (or of any page): the
            # "Copy staff report link" button fetches it from /api/staff-report-address only when it is pressed.
            "staff_report_link_set": bool(settings.staff_report_key),
            "sage": {"configured": isinstance(j.finance, SageFinance),
                     "connected": isinstance(j.finance, SageFinance) and j.finance.connected},
            "backend": settings.effective_llm_backend,
            "managers": sorted(settings.managers),
            "microsoft_signin": os.environ.get("WEBSITE_AUTH_ENABLED", "").lower() == "true",
        }
        # Team access (who may sign in to the cut-down console) is the principal owner's to see and change.
        caller = caller_of(request) if request is not None else None
        if caller is not None and caller.role == access.OWNER:
            data["context"]["team_access"] = team_access_view(j)
        return data

    @app.get("/api/staff-report-address", dependencies=[Depends(owner)])
    async def staff_report_address():
        """The one place the staff report address (which includes the staff key) leaves the server: handed to the
        "Copy staff report link" button, which puts it on the clipboard and never on the page. Not cached."""
        base = settings.public_base_url.rstrip("/")
        link = f"{base}/report?key={settings.staff_report_key}" if settings.staff_report_key else ""
        return JSONResponse({"link": link}, headers={"Cache-Control": "no-store"})

    @app.get("/api/settings", dependencies=[Depends(owner)])
    async def get_settings_page(request: Request):
        return settings_view(J(request), request)

    # ---- team access: the code engineers and office staff sign in with. Principal owner only (access.ROUTE_POLICY), a
    # same-origin click for the changes, and the code itself is never stored, returned or logged - only a salted hash.
    # Two codes since the office / engineer split: /api/team-access/office and /api/team-access/engineer set or switch off one
    # role's code and sign out only that role's sessions. The role-less POST / DELETE are the ENGINEER code (what the single
    # team code was), so anything that set "the team code" before still hands out exactly what it did - engineer access.
    def team_access_view(j: Jarvis) -> dict[str, Any]:
        """What the owner's Settings shows: per role, on/off, when set and how many are signed in. Never a code or a hash.
        The top-level keys are the engineer code's (the pre-split shape)."""
        roles = {role: {**info, "sessions": j.team_sessions.count(role)} for role, info in j.team_codes.info().items()}
        return {**roles[access.ENGINEER], "sessions": len(j.team_sessions), "roles": roles}

    def _team_role_param(team_role: str) -> str:
        if team_role not in access.TEAM_ROLES:
            raise HTTPException(404, "There is no such team role. Use office or engineer.")
        return team_role

    async def _set_team_code(j: Jarvis, team_role: str, code: str) -> dict[str, Any]:
        label = access.TEAM_ROLE_LABEL[team_role]
        try:
            await asyncio.to_thread(j.team_codes.set_code, team_role, code, "the owner")
        except CodeRejected as e:
            raise HTTPException(400, str(e)) from None
        j.db.add_notification("info", f"{label} code set", f"{label} staff can sign in at /login (Team sign-in) with the new code. "
                              f"Anyone signed in with the old {label.lower()} code has been signed out.")
        j.activity_feed.record("team_access", "the owner",
                               f"Set a new {label.lower()} access code (everyone signed in as {label.lower()} was signed out)")
        log.info("%s access code changed by the owner (previous %s sessions are signed out).", label, team_role)
        await j.team_sessions.close(team_role)  # the old code's sessions can't sign in again; drop their conversations too
        return team_access_view(j)

    async def _clear_team_code(j: Jarvis, team_role: str) -> dict[str, Any]:
        label = access.TEAM_ROLE_LABEL[team_role]
        j.team_codes[team_role].clear()
        j.db.add_notification("info", f"{label} sign-in switched off",
                              f"Nobody can sign in as {label.lower()} now, and anyone who was has been signed out.")
        j.activity_feed.record("team_access", "the owner",
                               f"Switched {label.lower()} access off (everyone signed in as {label.lower()} was signed out)")
        log.info("%s access switched off by the owner.", label)
        await j.team_sessions.close(team_role)
        return team_access_view(j)

    @app.get("/api/team-access", dependencies=[Depends(principal)])
    async def team_access_info(request: Request):
        return team_access_view(J(request))

    @app.post("/api/team-access", dependencies=[Depends(principal), Depends(human_click)])
    async def team_access_set(body: TeamCodeIn, request: Request):
        return await _set_team_code(J(request), access.ENGINEER, body.code)  # the role-less form is the engineer code

    @app.delete("/api/team-access", dependencies=[Depends(principal), Depends(human_click)])
    async def team_access_clear(request: Request):
        return await _clear_team_code(J(request), access.ENGINEER)

    @app.post("/api/team-access/{team_role}", dependencies=[Depends(principal), Depends(human_click)])
    async def team_access_set_role(team_role: str, body: TeamCodeIn, request: Request):
        return await _set_team_code(J(request), _team_role_param(team_role), body.code)

    @app.delete("/api/team-access/{team_role}", dependencies=[Depends(principal), Depends(human_click)])
    async def team_access_clear_role(team_role: str, request: Request):
        return await _clear_team_code(J(request), _team_role_param(team_role))

    # ---- engineer homes: where each engineer lives, kept as a rounded map point so Fleet / who_is_home can say "home". The
    # principal owner only (access.ROUTE_POLICY), a same-origin click for every change, and deliberately NOT a brain tool.
    # The owner types a postcode once; it is looked up server-side and dropped - the database, these responses and the logs
    # never hold it, and no response ever carries a point either (only "set" / "not set" and when). The request body is read
    # by hand (not a pydantic model) so a validation error can never echo the postcode back.
    async def homes_view(j: Jarvis, **extra: Any) -> JSONResponse:
        homes = j.homes
        data = {**extra, "radius_m": homes.radius_m, "default_radius_m": DEFAULT_RADIUS_M, "min_radius_m": MIN_RADIUS_M,
                "max_radius_m": MAX_RADIUS_M, "engineers": homes.listing(await homes.known_engineers())}
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    async def json_object(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - not JSON: treated as an empty request, never echoed
            return {}
        return data if isinstance(data, dict) else {}

    @app.get("/api/engineer-homes", dependencies=[Depends(principal)])
    async def engineer_homes_info(request: Request):
        return await homes_view(J(request))

    @app.post("/api/engineer-homes", dependencies=[Depends(principal), Depends(human_click)])
    async def engineer_home_set(request: Request):
        j = J(request)
        body = await json_object(request)
        try:
            await j.homes.set_from_postcode(body.get("engineer"), body.get("postcode"), "the owner")
        except HomeError as e:
            raise HTTPException(e.status, str(e)) from None
        return await homes_view(j)

    @app.post("/api/engineer-homes/radius", dependencies=[Depends(principal), Depends(human_click)])
    async def engineer_homes_radius(request: Request):
        j = J(request)
        try:
            j.homes.set_radius((await json_object(request)).get("metres"), "the owner")
        except HomeError as e:
            raise HTTPException(e.status, str(e)) from None
        return await homes_view(j)

    @app.delete("/api/engineer-homes/{engineer}", dependencies=[Depends(principal), Depends(human_click)])
    async def engineer_home_clear(engineer: str, request: Request):
        j = J(request)
        return await homes_view(j, removed=j.homes.clear(engineer, "the owner"))

    @app.delete("/api/engineer-homes", dependencies=[Depends(principal), Depends(human_click)])
    async def engineer_homes_clear_all(request: Request):
        j = J(request)
        return await homes_view(j, removed=j.homes.clear_all("the owner"))

    def settings_snapshot(keys: set[str]) -> dict[str, str]:
        return {k: str(getattr(settings, k, "")) for k in keys if k in FIELDS}

    def audit_settings(j: Jarvis, before: dict[str, str], actor: str | None) -> None:
        """One line in "What Jarvis did" naming the settings that really changed (their labels - and on/off for a switch) and who saved
        them. Never a value: a password, key, address or webhook is not written anywhere by this."""
        changed = [k for k, old in before.items() if str(getattr(settings, k, "")) != old]
        if not changed:
            return
        parts = []
        for k in changed[:8]:
            field = FIELDS[k]
            now = getattr(settings, k, None)
            parts.append(f"{field.label} ({'on' if now else 'off'})" if field.kind == "bool" else field.label)
        more = f" and {len(changed) - 8} more" if len(changed) > 8 else ""
        j.activity_feed.record("settings", actor or "the owner", "Changed settings: " + ", ".join(parts) + more)

    @app.post("/api/settings", dependencies=[Depends(owner)])
    async def save_settings(body: SettingsIn, request: Request):
        # Standing approvals widen what Jarvis may do without asking, and the owner/partner emails, display password
        # and staff key decide who counts as the owner - so only the owner themself (not just any signed-in manager)
        # may change them, and only here, never through the AI or a Teams message.
        if (OWNER_ONLY_KEYS & (set(body.values) | set(body.clear))) and not auth.is_principal_owner(
                settings, request, trusted_owner_email):
            raise HTTPException(403, "Only the owner can change standing approvals, who the owner and partner are, "
                                     "the display password and staff key, or whether van locations show outside "
                                     "working hours, or whether and where Jarvis may browse the web, or which "
                                     "service inbox Jarvis reads, or the Companies House key.")
        before = settings_snapshot(set(body.values) | set(body.clear))
        errors = store.update(body.values, body.clear)
        if errors:
            return JSONResponse({"errors": errors}, status_code=400)
        audit_settings(J(request), before, speaker(request))
        await reload_jarvis(request.app)
        data = settings_view(J(request), request)
        data["signed_out"] = "jarvis_owner_password" in body.values and bool(body.values["jarvis_owner_password"])
        return data

    @app.post("/api/settings/test/{section}", dependencies=[Depends(owner)])
    async def test_connection(section: str, request: Request):
        if section not in SECTIONS_BY_ID or not SECTIONS_BY_ID[section].test:
            raise HTTPException(404, "Nothing to test there.")
        j = J(request)
        ok, detail = await connection_tests.run(j, section)
        return store.record_test(j.db, section, ok, detail)

    # ------------------------------------------------------------------ Sage connect (OAuth)
    @app.get("/auth/sage/start", dependencies=[Depends(owner)])
    async def sage_start(request: Request):
        j = J(request)
        if not isinstance(j.finance, SageFinance):
            raise HTTPException(400, "Set SAGE_CLIENT_ID and SAGE_CLIENT_SECRET first.")
        state = auth.new_state()
        j.db.set_kv("sage_oauth_state", state)
        return RedirectResponse(j.finance.authorize_url(f"{settings.public_base_url}/auth/sage/callback", state))

    @app.get("/auth/sage/callback", dependencies=[Depends(owner)])
    async def sage_callback(request: Request, code: str = "", state: str = ""):
        j = J(request)
        expected = j.db.get_kv("sage_oauth_state")
        if not code or not expected or state != expected:
            raise HTTPException(400, "Sage sign-in failed or expired - try again.")
        await j.finance.exchange_code(code, f"{settings.public_base_url}/auth/sage/callback")
        j.db.set_kv("sage_oauth_state", "")
        await j.notifier.notify("Sage connected", "Jarvis can now read your accounts.", level="info",
                                   importance="info", management_only=True)
        return RedirectResponse("/")

    return app


app = create_app()


def run() -> None:
    import os

    import uvicorn

    # Only trust X-Forwarded-* from the configured proxy addresses (Azure's front end sets FORWARDED_ALLOW_IPS).
    uvicorn.run("jarvis.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), proxy_headers=True,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))
