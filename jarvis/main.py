"""FastAPI app: the HUD display, chat/voice API, live WebSocket, staff issue reporting."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
from .config import Settings, get_settings
from .core import Jarvis
from .integrations.finance import SageFinance
from .integrations.voice import VoiceError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jarvis")
WEB = Path(__file__).parent / "web"


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    mode: str = "typed"  # typed | voice
    attachments: list[dict[str, str]] = []


class TTSIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    voice_id: str | None = None


def create_app(settings: Settings | None = None, jarvis: Jarvis | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        j = jarvis or Jarvis(settings)
        app.state.j = j
        await j.start()
        if not settings.jarvis_owner_password:
            log.warning("JARVIS_OWNER_PASSWORD is not set - the display only answers requests from this machine.")
        yield
        await j.stop()

    app = FastAPI(title="Salts Jarvis", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    def J(request: Request) -> Jarvis:  # noqa: N802
        return request.app.state.j

    def owner(request: Request) -> None:
        auth.require_owner(settings, request)

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
    async def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE)
        return resp

    @app.get("/report", response_class=HTMLResponse)
    async def report_page():
        return FileResponse(WEB / "report.html")

    # ------------------------------------------------------------------ chat
    @app.post("/api/chat", dependencies=[Depends(owner)])
    async def chat(body: ChatIn, request: Request):
        reply = await J(request).brain.ask(body.text, "voice" if body.mode == "voice" else "typed", body.attachments)
        return {"reply": reply}

    @app.post("/api/conversation/reset", dependencies=[Depends(owner)])
    async def reset(request: Request):
        J(request).brain.reset()
        return {"ok": True}

    @app.get("/api/transcript", dependencies=[Depends(owner)])
    async def transcript(request: Request):
        return J(request).db.recent_transcript(40)

    @app.get("/api/status", dependencies=[Depends(owner)])
    async def status(request: Request):
        j = J(request)
        data, presence = await asyncio.gather(j.briefings.status(), j.marketing.overview(30))
        data.update(connections=j.connections(), voice=j.voice.client_config(), presence=presence,
                    owner=settings.owner_name, company=settings.company_name,
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
            return JSONResponse({"fallback": "browser", "detail": str(e)}, status_code=503)
        return StreamingResponse(stream, media_type=mime)

    @app.post("/api/stt", dependencies=[Depends(owner)])
    async def stt(request: Request, audio: UploadFile = File(...)):
        data = await audio.read()
        try:
            text = await J(request).voice.transcribe(data, audio.content_type or "audio/webm")
        except VoiceError as e:
            return JSONResponse({"fallback": "browser", "detail": str(e)}, status_code=503)
        return {"text": text}

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
        q = j.bus.subscribe()
        running: set[asyncio.Task] = set()

        async def pump():
            while True:
                await ws.send_json(await q.get())

        async def listen():
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "chat" and msg.get("text"):
                    task = asyncio.create_task(j.brain.ask(msg["text"][:20000],
                                                           "voice" if msg.get("mode") == "voice" else "typed",
                                                           msg.get("attachments") or []))
                    running.add(task)
                    task.add_done_callback(running.discard)
                elif msg.get("type") == "ping":
                    await ws.send_json({"type": "pong"})

        tasks = [asyncio.create_task(pump()), asyncio.create_task(listen())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            j.bus.unsubscribe(q)

    # ------------------------------------------------------------------ approvals
    @app.get("/api/approvals", dependencies=[Depends(owner)])
    async def approvals(request: Request):
        return J(request).db.pending_actions()

    @app.post("/api/approvals/{action_id}/{decision}", dependencies=[Depends(owner)])
    async def decide(action_id: int, decision: str, request: Request):
        j = J(request)
        if decision == "approve":
            return {"result": await j.actions.approve(action_id)}
        if decision == "deny":
            return {"result": await j.actions.deny(action_id)}
        raise HTTPException(400, "decision must be approve or deny")

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
        await j.notifier.notify("Sage connected", "Jarvis can now read your accounts.", level="info")
        return RedirectResponse("/")

    return app


app = create_app()


def run() -> None:
    import os

    import uvicorn

    # Only trust X-Forwarded-* from the configured proxy addresses (Azure's front end sets FORWARDED_ALLOW_IPS).
    uvicorn.run("jarvis.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), proxy_headers=True,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))
