"""Web chat for the robot. Runs on the gaming laptop, next to Ollama.

The browser only talks to this app. This app runs the agent loop (loop.py),
talks to Ollama, and holds the robot server's token, so the token never
reaches the browser.

    Browser --> this app --> Ollama
                         +-> robot server (Pi / Mac mock)

Security, in layers:
  - Reachable only over Tailscale: compose.yml publishes the port on the
    laptop's Tailscale IP only, never on the Wi-Fi.
  - Team password (TEAM_PASSWORD), separate from the robot token. Logging in
    sets an HttpOnly, SameSite=Strict cookie; every /api/* call needs it
    except /api/health/live (for Docker's health check).
  - Failed logins are rate-limited per client IP.
  - POSTs from another website are refused (Origin check + SameSite cookie).
  - Size limits on every request, message, conversation and session count.
  - Strict Content-Security-Policy: the page can only load this app's own
    files, which is why the JS/CSS live in static/ instead of inline.
  - Audit log: one JSON line per login, tool call and STOP (docker logs).

Run (same env vars as agent.py, see agent/loop.py, plus TEAM_PASSWORD):
    BRIDGE_URL=... BRIDGE_TOKEN=... TEAM_PASSWORD=... uvicorn app:app --host 127.0.0.1 --port 8080
"""

import json
import os
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

import requests
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# loop.py lives in ../agent; make it importable from here.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "agent"))
import loop  # noqa: E402

# --- Settings ---------------------------------------------------------------

MIN_PASSWORD_LEN = 16
TEAM_PASSWORD = os.environ.get("TEAM_PASSWORD", "")
if len(TEAM_PASSWORD) < MIN_PASSWORD_LEN:
    raise SystemExit(f"TEAM_PASSWORD is missing or shorter than {MIN_PASSWORD_LEN} characters. "
                     "Generate one with:\n"
                     "  python -c \"import secrets; print(secrets.token_urlsafe(16))\"")

COOKIE = "robotchat_session"
LOGIN_HOURS = 12
MAX_LOGINS = 200            # logged-in browsers kept at once
MAX_CONVERSATIONS = 50      # chat tabs kept at once (least recently used go first)
MAX_HISTORY = 40            # messages kept per conversation, besides the system prompt
MAX_MESSAGE_CHARS = 2000
MAX_BODY_BYTES = 16 * 1024
LOGIN_FAILS = 5             # wrong passwords per IP...
LOGIN_WINDOW_S = 300        # ...per 5 minutes, then 429
HEALTH_CACHE_S = 3          # many tabs polling share one pipeline check

# --- Audit log --------------------------------------------------------------


def audit(event: str, ip: str, **fields) -> None:
    """One JSON line per security-relevant event (see `docker compose logs`).

    json.dumps escapes newlines, so user text can't fake extra log lines.
    Never log the password, the token or the session cookie.
    """
    print(json.dumps({"t": round(time.time(), 3), "event": event, "ip": ip, **fields}),
          flush=True)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


# --- Logins -----------------------------------------------------------------
# Endpoints are plain `def`, so FastAPI runs them in a thread pool; every
# shared dict below has a lock.

logins: dict[str, float] = {}  # cookie value -> expiry (time.time())
login_fails: dict[str, deque] = defaultdict(deque)  # IP -> failure times
logins_guard = threading.Lock()


def too_many_login_fails(ip: str) -> bool:
    """Call with logins_guard held."""
    fails = login_fails[ip]
    now = time.monotonic()
    while fails and now - fails[0] > LOGIN_WINDOW_S:
        fails.popleft()
    if not fails:
        del login_fails[ip]
        return False
    return len(fails) >= LOGIN_FAILS


def current_login(request: Request) -> str | None:
    """The caller's session id if logged in and not expired, else None."""
    sid = request.cookies.get(COOKIE, "")
    with logins_guard:
        expiry = logins.get(sid)
        if expiry is not None and expiry < time.time():
            del logins[sid]
            expiry = None
    return sid if expiry is not None else None


def same_origin(request: Request) -> None:
    """Refuse POSTs sent by another website.

    Browsers always send Origin on a POST from a page; it must match the
    address this app was reached at. (The SameSite=Strict cookie already
    blocks this; this is the second lock on the same door.)
    """
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
        audit("cross_origin", client_ip(request), origin=origin[:200])
        raise HTTPException(403, "Cross-site request refused.")


def logged_in(request: Request) -> str:
    """Dependency for every /api/* endpoint that needs the team password."""
    if request.method == "POST":
        same_origin(request)
    sid = current_login(request)
    if sid is None:
        raise HTTPException(401, "Not logged in.")
    return sid


# --- Conversations ----------------------------------------------------------
# One conversation per (login, browser tab), kept in memory: restarting the
# app clears them all, which is fine for debugging. Each has a lock so a tab
# can't run two requests at once and interleave messages in the history.

conversations: dict[tuple[str, str], dict] = {}
conversations_guard = threading.Lock()


def get_conversation(sid: str, tab_id: str) -> dict:
    with conversations_guard:
        key = (sid, tab_id)
        if key not in conversations:
            if len(conversations) >= MAX_CONVERSATIONS:
                idle = [k for k, c in conversations.items() if not c["lock"].locked()]
                if not idle:
                    raise HTTPException(503, "Too many chats running; try again soon.")
                del conversations[min(idle, key=lambda k: conversations[k]["used"])]
            conversations[key] = {"messages": loop.new_conversation(),
                                  "lock": threading.Lock(), "used": 0.0}
        conversations[key]["used"] = time.monotonic()
        return conversations[key]


def trim_history(messages: list) -> None:
    """Keep the system prompt and only the most recent turns.

    Cuts only right before a user message, so a tool result is never
    separated from the model message that asked for it.
    """
    if len(messages) <= MAX_HISTORY + 1:
        return
    user_turns = [i for i, m in enumerate(messages) if m["role"] == "user"]
    for i in user_turns:
        if len(messages) - i <= MAX_HISTORY:
            del messages[1:i]
            return
    del messages[1:user_turns[-1]]  # the newest turn alone is over the limit


# --- Pipeline health --------------------------------------------------------

health_cache: dict = {"at": float("-inf"), "results": []}
health_guard = threading.Lock()


def pipeline_health() -> list[dict]:
    """loop.check_pipeline(), shared for HEALTH_CACHE_S between callers."""
    with health_guard:
        if time.monotonic() - health_cache["at"] > HEALTH_CACHE_S:
            health_cache["results"] = loop.check_pipeline()
            health_cache["at"] = time.monotonic()
        return health_cache["results"]


# --- App, middleware --------------------------------------------------------

# docs_url etc. = None: no auto-generated API pages for strangers to browse.
app = FastAPI(title="Robot chat", docs_url=None, redoc_url=None, openapi_url=None)


class LimitBody:
    """Refuse oversized request bodies before they are read.

    Plain ASGI middleware: it looks at the Content-Length header and answers
    411/413 itself. uvicorn guarantees the body is no longer than that
    header, so nothing bigger can sneak in.
    """

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT", "PATCH"):
            length = dict(scope["headers"]).get(b"content-length")
            if length is None:
                problem = (411, "Content-Length required")
            elif not length.isdigit() or int(length) > self.max_bytes:
                problem = (413, f"request body over {self.max_bytes} bytes")
            else:
                problem = None
            if problem:
                response = JSONResponse({"detail": problem[1]}, status_code=problem[0])
                return await response(scope, receive, send)
        await self.app(scope, receive, send)


app.add_middleware(LimitBody, max_bytes=MAX_BODY_BYTES)

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                               "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response


# JS and CSS only; the HTML pages are served by the routes below.
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

# --- Request bodies ---------------------------------------------------------
# pydantic models describe the JSON the page sends; FastAPI checks it and
# answers 422 if it doesn't match, before our code runs.

TAB_ID = r"^[A-Za-z0-9_-]{16,64}$"


class LoginRequest(BaseModel):
    password: str = Field(max_length=256)


class ChatRequest(BaseModel):
    session_id: str = Field(pattern=TAB_ID)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class SessionRequest(BaseModel):
    session_id: str = Field(pattern=TAB_ID)


# --- Pages ------------------------------------------------------------------

@app.get("/", response_model=None)
def index(request: Request) -> Response:
    if current_login(request) is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(HERE / "index.html")


@app.get("/login", response_model=None)
def login_page(request: Request) -> Response:
    if current_login(request) is not None:
        return RedirectResponse("/", status_code=303)
    return FileResponse(HERE / "login.html")


# --- Endpoints --------------------------------------------------------------
# These are plain `def` (not async) on purpose: loop.py uses blocking HTTP
# calls, and FastAPI runs plain functions in a thread pool, so one slow chat
# doesn't freeze the STOP button for everyone else.

@app.get("/api/health/live")
def health_live() -> dict:
    """No login: only says this process is up. Docker's health check uses it."""
    return {"ok": True}


@app.get("/api/health")
def health(sid: str = Depends(logged_in)) -> dict:
    """Every hop from here to the robot; the page's status lights."""
    hops = pipeline_health()
    return {"ok": all(h["ok"] for h in hops), "hops": hops}


@app.post("/api/login")
def login(req: LoginRequest, request: Request, response: Response) -> dict:
    same_origin(request)
    ip = client_ip(request)
    with logins_guard:
        if too_many_login_fails(ip):
            audit("login_locked", ip)
            raise HTTPException(429, "Too many wrong passwords. Wait 5 minutes.")
    if not secrets.compare_digest(req.password.encode(), TEAM_PASSWORD.encode()):
        with logins_guard:
            login_fails[ip].append(time.monotonic())
        audit("login_failed", ip)
        raise HTTPException(401, "Wrong password.")

    sid = secrets.token_urlsafe(32)
    with logins_guard:
        now = time.time()
        for old in [s for s, exp in logins.items() if exp < now]:
            del logins[old]
        if len(logins) >= MAX_LOGINS:
            del logins[min(logins, key=logins.get)]  # the one expiring soonest
        logins[sid] = now + LOGIN_HOURS * 3600
    # HttpOnly: page scripts can't read it. SameSite=Strict: other websites
    # can't make the browser send it. Not `Secure`: the app is plain HTTP
    # inside Tailscale, which already encrypts the traffic.
    response.set_cookie(COOKIE, sid, max_age=LOGIN_HOURS * 3600, httponly=True,
                        samesite="strict", path="/")
    audit("login", ip)
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response, sid: str = Depends(logged_in)) -> dict:
    with logins_guard:
        logins.pop(sid, None)
    with conversations_guard:
        for key in [k for k in conversations if k[0] == sid]:
            del conversations[key]
    response.delete_cookie(COOKIE, path="/")
    audit("logout", client_ip(request))
    return {"ok": True}


@app.get("/api/config")
def config(sid: str = Depends(logged_in)) -> dict:
    """Non-secret settings the page displays."""
    return {"model": loop.MODEL, "stop_page": f"{loop.BRIDGE_URL}/stop"}


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request, sid: str = Depends(logged_in)) -> dict:
    ip = client_ip(request)
    convo = get_conversation(sid, req.session_id)
    if not convo["lock"].acquire(blocking=False):
        raise HTTPException(409, "Still working on your previous message.")
    try:
        # Health check before every turn: say exactly which hop is down
        # instead of failing halfway through a multi-step command.
        down = [h for h in pipeline_health() if not h["ok"]]
        if down:
            raise HTTPException(503, f"Not sent: {down[0]['name']} is down. {down[0]['fix']}")

        # Fetched every time so new tools (or a restarted server) just work.
        try:
            tools = loop.get_tools()
        except requests.RequestException as e:
            audit("error", ip, where="get_tools", error=str(e)[:300])
            raise HTTPException(502, "Stopped: the robot server on the Pi failed (unreachable).")

        tool_calls = []

        def on_tool(name, args, result):
            tool_calls.append({"name": name, "args": args, "result": result})
            audit("tool", ip, tool=name, args=args, ok=result.get("ok"))

        messages = convo["messages"]
        messages.append({"role": "user", "content": req.message})
        trim_history(messages)
        audit("chat", ip, chars=len(req.message))
        # run_turn never raises for a broken hop (LLM, Pi server, serial,
        # brain): it ends the turn and the reply says which hop failed.
        reply = loop.run_turn(messages, tools, on_tool=on_tool)
        if reply.startswith("Stopped:"):
            audit("aborted", ip, reason=reply[:300])
        return {"reply": reply, "tool_calls": tool_calls}
    finally:
        convo["lock"].release()


@app.post("/api/stop")
def stop(request: Request, sid: str = Depends(logged_in)) -> dict:
    """STOP button: straight to the robot server, the LLM is not involved."""
    result = loop.call_tool("stop", {})
    audit("stop", client_ip(request), ok=result.get("ok"))
    if not result.get("ok"):
        # Full detail (server address, raw error) goes to the audit log only;
        # the page gets which hop failed, like the rest of this app.
        audit("error", client_ip(request), where="stop", error=str(result.get("error"))[:300],
              hop=result.get("hop"), detail=str(result.get("message"))[:300])
        hop = loop.HOPS.get(result.get("hop"), "the robot link")
        raise HTTPException(502, f"Robot NOT confirmed stopped: {hop} failed "
                                 f"({result.get('error')}). Use the backup STOP page or the physical stop.")
    return result


@app.post("/api/reset")
def reset(req: SessionRequest, sid: str = Depends(logged_in)) -> dict:
    """'New chat' button: forget this tab's conversation."""
    with conversations_guard:
        convo = conversations.get((sid, req.session_id))
        if convo and not convo["lock"].locked():
            del conversations[(sid, req.session_id)]
    return {"ok": True}
