"""FastAPI app: the HUD display, chat/voice API, live WebSocket, staff issue reporting."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Any

from . import auth
from .config import Settings, get_settings
from .core import Jarvis
from .integrations.finance import SageFinance
from .integrations.stt_chain import SERVER_ENGINES
from .integrations.teamsbot import TeamsBotError, same_service_url, trusted_service_url, verify_activity
from .integrations.voice import STT_ATTEMPT_TIMEOUT_S, STTError, VoiceError
from .redact import install_log_redaction, redact_text
from .services import connection_tests, documents
from .services.teams_approvals import approver_emails, invoke_value, parse_decision_value, parse_typed_command
from .settings_store import OWNER_IDENTITY_KEYS, OWNER_ONLY_KEYS, SECTIONS_BY_ID, SettingsStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
install_log_redaction()  # no secrets (webhook signatures, tokens, keys) in the log stream - see jarvis/redact.py
log = logging.getLogger("jarvis")
WEB = Path(__file__).parent / "web"


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    mode: str = "typed"  # typed | voice
    attachments: list[dict[str, str]] = []
    compose: bool = False  # true only when the owner typed this into the chat box (not a quick button / voice)


class FeedbackIn(BaseModel):
    rating: str = Field(pattern="^(good|wrong)$")
    note: str = Field("", max_length=500)
    turn_id: int | None = None  # default: Jarvis's most recent turn


class VoiceEventIn(BaseModel):
    kind: str = Field(max_length=40)  # stt_failure | stt_empty | echo_suppressed | first_audio
    ms: float | None = None           # first_audio only: milliseconds from sending the request to the first sound
    turn_id: int | None = None
    detail: str = Field("", max_length=300)


class IssueResolveIn(BaseModel):
    note: str = Field("", max_length=1000)


class ForgetIn(BaseModel):
    text: str = Field(min_length=1, max_length=200)


class TTSIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    voice_id: str | None = None


class SettingsIn(BaseModel):
    values: dict[str, Any] = {}
    clear: list[str] = []


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

    app = FastAPI(title="Salts Jarvis", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    def J(request: Request) -> Jarvis:  # noqa: N802
        return request.app.state.j

    def owner(request: Request) -> None:
        auth.require_owner(settings, request)

    def speaker(conn: Request | WebSocket) -> str | None:
        email = auth.signed_in_manager(settings, conn)
        return settings.person(email) if email else None

    # ------------------------------------------------------------------ pages
    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if not auth.is_owner(settings, request):
            return RedirectResponse("/login")
        return FileResponse(WEB / "index.html")

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

    @app.post("/logout")
    async def logout(request: Request):
        # Microsoft sign-in users are signed out of App Service too, or they'd walk straight back in.
        target = "/.auth/logout" if auth.signed_in_manager(settings, request) else "/login"
        resp = RedirectResponse(target, status_code=303)
        resp.delete_cookie(auth.COOKIE)
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

    @app.post("/api/chat", dependencies=[Depends(owner)])
    async def chat(body: ChatIn, request: Request):
        learn_reply(J(request), body.text, body.mode, body.compose, body.attachments)
        reply = await J(request).brain.ask(body.text, "voice" if body.mode == "voice" else "typed", body.attachments,
                                           speaker=speaker(request))
        return {"reply": reply}

    # Same conversation, but for when the live WebSocket isn't available (e.g. it dropped and hasn't
    # reconnected yet): word-by-word as Claude generates it, rather than the client waiting on the full
    # reply. Each line is one of the same {"type", "data"} events the WebSocket already streams.
    CHAT_STREAM_EVENTS = {"user_message", "thinking", "delta", "tool", "reply", "error", "stopped", "ask"}
    CHAT_STREAM_TERMINAL = {"reply", "error", "stopped"}

    @app.post("/api/chat/stream", dependencies=[Depends(owner)])
    async def chat_stream(body: ChatIn, request: Request):
        j = J(request)
        mode = "voice" if body.mode == "voice" else "typed"
        learn_reply(j, body.text, mode, body.compose, body.attachments)
        q = j.bus.subscribe()
        task = asyncio.create_task(j.brain.ask(body.text, mode, body.attachments, speaker=speaker(request)))

        async def events():
            try:
                while True:
                    msg = await q.get()
                    if msg["type"] not in CHAT_STREAM_EVENTS:
                        continue
                    yield f"data: {json.dumps(msg, default=str)}\n\n"
                    if msg["type"] in CHAT_STREAM_TERMINAL:
                        break
            finally:
                j.bus.unsubscribe(q)
                if not task.done():
                    task.cancel()
                with contextlib.suppress(BaseException):
                    await task

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/conversation/reset", dependencies=[Depends(owner)])
    async def reset(request: Request):
        J(request).brain.reset()
        return {"ok": True}

    @app.post("/api/interrupt", dependencies=[Depends(owner)])
    async def interrupt(request: Request):
        j = J(request)
        stopped = await j.brain.interrupt() if hasattr(j.brain, "interrupt") else False
        j.bus.publish("stopped", {"stopped": stopped})
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
        return J(request).db.recent_transcript(40)

    async def _safe(coro, label: str) -> dict[str, Any]:
        # A misconfigured or unreachable connection (a wrong FSM address, Sage down, ...) must degrade
        # gracefully here - it must never take the whole display down with it.
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            log.warning("status %s failed: %s", label, e)
            return {"error": redact_text(f"{type(e).__name__}: {e}")[:200]}

    @app.get("/api/status", dependencies=[Depends(owner)])
    async def status(request: Request):
        j = J(request)
        data, presence, customers = await asyncio.gather(
            j.briefings.status(), _safe(j.marketing.overview(30), "marketing"), _safe(j.customers.scores(), "customers"))
        data.update(connections=j.connections(), voice=j.voice.client_config(), presence=presence,
                    customer_watch=[c for c in customers.get("customers", []) if c["status"] != "healthy"][:6],
                    owner=settings.owner_name, company=settings.company_name,
                    resolved_issues=[j.issues.summary(i) for i in j.db.list_issues("resolved", 5)],
                    accreditations=[t for t in j.accreditations.status()["timeline"] if t["days_left"] <= 60][:6],
                    sage={"configured": isinstance(j.finance, SageFinance),
                          "connected": isinstance(j.finance, SageFinance) and j.finance.connected})
        return data

    @app.get("/api/tracking", dependencies=[Depends(owner)])
    async def tracking(request: Request):
        return await J(request).tracker.live()

    # ------------------------------------------------------------------ voice
    @app.post("/api/tts", dependencies=[Depends(owner)])
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

    @app.post("/api/stt", dependencies=[Depends(owner)])
    async def stt(request: Request, audio: UploadFile = File(...), engine: str | None = None):
        """engine (optional): try exactly this engine once - the browser drives retry and fallback across
        voice.stt_chain (see web/hud.js). Without it, the configured engine is used with one server-side retry."""
        data = await audio.read()
        j = J(request)
        if not data:
            log.warning("STT upload was empty (filename=%s)", audio.filename)
            j.quality.record_event("stt_empty", "no audio received")
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
            j.quality.record_event("stt_failure", str(e))
            return JSONResponse({"detail": str(e), "provider": e.provider, "upstream_status": e.status,
                                 "transient": e.transient}, status_code=502)
        except VoiceError as e:
            j.quality.record_event("stt_failure", str(e))
            return JSONResponse({"fallback": "browser", "detail": str(e)}, status_code=503)
        except Exception as e:  # noqa: BLE001 - anything unexpected: log it in full, never a bare 500
            log.exception("STT failed unexpectedly (%d bytes, %s)", len(data), audio.content_type)
            j.quality.record_event("stt_failure", f"{type(e).__name__}: {e}")
            return JSONResponse({"detail": f"Unexpected speech-to-text error ({type(e).__name__}) - "
                                           "see the Jarvis server log."}, status_code=502)
        log.info("STT ok: %d bytes, %s, %d chars", len(data), audio.content_type, len(text or ""))
        if (text or "").strip():
            j.quality.note_stt((time.monotonic() - started) * 1000, text)
        else:
            j.quality.record_event("stt_empty")
        return {"text": text or "", "engine": engine or settings.effective_stt}

    @app.get("/api/voices", dependencies=[Depends(owner)])
    async def voices(request: Request):
        return await J(request).voice.list_voices()

    @app.websocket("/ws/stt")
    async def ws_stt(ws: WebSocket):
        if not auth.is_owner(settings, ws):
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
        if not auth.is_owner(settings, ws):
            await ws.close(code=4401)
            return
        await ws.accept()
        j: Jarvis = ws.app.state.j
        who = speaker(ws)
        q = j.bus.subscribe()
        running: set[asyncio.Task] = set()
        muted = False  # this session's mute button: Jarvis-initiated messages (see services/proactive.py) are not sent

        async def pump():
            while True:
                msg = await q.get()
                if muted and msg["type"] == "proactive":
                    continue
                await ws.send_json(msg)

        async def listen():
            nonlocal muted
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "chat" and msg.get("text"):
                    learn_reply(j, str(msg["text"])[:20000], "voice" if msg.get("mode") == "voice" else "typed",
                                msg.get("compose") is True, msg.get("attachments"))
                    task =asyncio.create_task(j.brain.ask(msg["text"][:20000],
                                                           "voice" if msg.get("mode") == "voice" else "typed",
                                                           msg.get("attachments") or [], speaker=who))
                    running.add(task)
                    task.add_done_callback(running.discard)
                elif msg.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
                elif msg.get("type") == "proactive_mute":
                    muted = msg.get("muted") is True
                elif msg.get("type") == "stop":
                    stopped = await j.brain.interrupt() if hasattr(j.brain, "interrupt") else False
                    j.bus.publish("stopped", {"stopped": stopped})

        tasks = [asyncio.create_task(pump()), asyncio.create_task(listen())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            j.bus.unsubscribe(q)

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
                                           documents.brand_logo(settings.company_logo_path))
        except ImportError:
            raise HTTPException(503, "Document rendering isn't installed on this server.") from None
        filename = documents.download_filename(doc, fmt)
        return Response(data, media_type=mime,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                 "Cache-Control": "no-store"})

    # ------------------------------------------------------------------ approvals
    @app.get("/api/approvals", dependencies=[Depends(owner)])
    async def approvals(request: Request):
        return J(request).db.pending_actions()

    @app.post("/api/approvals/{action_id}/{decision}", dependencies=[Depends(owner)])
    async def decide(action_id: int, decision: str, request: Request):
        j = J(request)
        if decision == "approve":
            return {"result": await j.actions.approve(action_id, by=speaker(request))}
        if decision == "deny":
            return {"result": await j.actions.deny(action_id, by=speaker(request))}
        raise HTTPException(400, "decision must be approve or deny")

    # ------------------------------------------------------------------ suggestions
    @app.post("/api/suggestions/refresh", dependencies=[Depends(owner)])
    async def refresh_suggestions(request: Request):
        return await J(request).suggestions.sweep(announce=False)

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
    async def _handle_teams_message(j: Jarvis, service_url: str, conversation_id: str, text: str, name: str) -> None:
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
        asyncio.create_task(_handle_teams_message(j, service_url, conversation_id, text, name))
        return {}

    # ------------------------------------------------------------------ settings page
    def settings_view(j: Jarvis) -> dict[str, Any]:
        base = settings.public_base_url.rstrip("/")
        context = {"base_url": base, "app_name": os.environ.get("WEBSITE_SITE_NAME", "salts-jarvis")}
        data = store.view(j.db, context)
        data["context"] = {
            **context,
            "staff_report_link": f"{base}/report?key={settings.staff_report_key}" if settings.staff_report_key else "",
            "sage": {"configured": isinstance(j.finance, SageFinance),
                     "connected": isinstance(j.finance, SageFinance) and j.finance.connected},
            "backend": settings.effective_llm_backend,
            "managers": sorted(settings.managers),
            "microsoft_signin": os.environ.get("WEBSITE_AUTH_ENABLED", "").lower() == "true",
        }
        return data

    @app.get("/api/settings", dependencies=[Depends(owner)])
    async def get_settings_page(request: Request):
        return settings_view(J(request))

    @app.post("/api/settings", dependencies=[Depends(owner)])
    async def save_settings(body: SettingsIn, request: Request):
        # Standing approvals widen what Jarvis may do without asking, and the owner/partner emails, display password
        # and staff key decide who counts as the owner - so only the owner themself (not just any signed-in manager)
        # may change them, and only here, never through the AI or a Teams message.
        if (OWNER_ONLY_KEYS & (set(body.values) | set(body.clear))) and not auth.is_principal_owner(
                settings, request, trusted_owner_email):
            raise HTTPException(403, "Only the owner can change standing approvals, who the owner and partner are, "
                                     "or the display password and staff key.")
        errors = store.update(body.values, body.clear)
        if errors:
            return JSONResponse({"errors": errors}, status_code=400)
        await reload_jarvis(request.app)
        data = settings_view(J(request))
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
