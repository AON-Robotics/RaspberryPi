"""Robot tool server for the LLM debugging bridge. Runs on the Pi.

    laptop (agent/web) --HTTP + token--> this server --USB serial--> V5 brain

The tools live in tools/ (one module per area); this file is only the HTTP
layer: authentication, request limits, the tool list, argument checks and the
health report. The brain side is Override/src/aon/pi/ (protocol.cpp,
commands.cpp, pi-link.cpp); the wire format is docs/serial-protocol.md.

Security, in layers
-------------------
  - Bind to the Pi's Tailscale IP only, so only the tailnet can reach it and
    the token always travels encrypted (`tailscale ip -4` prints the IP).
  - BRIDGE_TOKEN (>= 32 chars) for every tool. An optional STOP_TOKEN can
    only call stop(): give that one to teammates for the phone STOP page.
  - Wrong tokens are rate-limited per client IP, but stop() never is:
    stopping the robot must work even while someone hammers the server.
  - Request bodies over 4 KB are refused before they are read.
  - Tool arguments must be plain JSON numbers/booleans: no "10", no NaN.
  - Security headers on every response, no auto-generated /docs pages.
  - One JSON audit line per tool call and per bad token. Never the token.

Settings (environment variables)
--------------------------------
    BRIDGE_TOKEN  required, >= 32 chars; shared secret with the laptop
    STOP_TOKEN    optional, >= 32 chars; can only call stop()
    BRAIN_PORT    serial port of the brain's USB *user* port; default: auto
                  detect (/dev/serial/by-id, then /dev/ttyACM1). For the
                  laptop simulator, the pty path printed by sim/brain_sim.

Run:
    BRIDGE_TOKEN=... uvicorn server:app --host <tailscale-ip> --port 8000 --no-server-header
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from brain_link import BrainLink
from tools import TOOLS, ctx, fail

HERE = Path(__file__).resolve().parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bridge.server")

# --- Auth -------------------------------------------------------------------

# token_urlsafe(32) makes 43 characters; anything much shorter is guessable.
MIN_TOKEN_LEN = 32
TOKEN = os.environ.get("BRIDGE_TOKEN", "")
if len(TOKEN) < MIN_TOKEN_LEN:
    raise SystemExit(f"BRIDGE_TOKEN is missing or shorter than {MIN_TOKEN_LEN} characters. "
                     "Generate one with:\n"
                     "  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")

# A second token that can ONLY call stop(). Give this one to teammates for
# the phone STOP page: they can stop the robot but not drive it.
STOP_TOKEN = os.environ.get("STOP_TOKEN", "")
if STOP_TOKEN and (len(STOP_TOKEN) < MIN_TOKEN_LEN or STOP_TOKEN == TOKEN):
    raise SystemExit(f"STOP_TOKEN must be at least {MIN_TOKEN_LEN} characters "
                     "and different from BRIDGE_TOKEN.")

# Wrong-token attempts per client IP. After MAX_BAD_TOKENS in the window,
# that IP gets 429 without its token even being checked (stop() excepted).
MAX_BAD_TOKENS = 10
BAD_TOKEN_WINDOW_S = 60
bad_tokens: dict[str, deque] = defaultdict(deque)

MAX_BODY_BYTES = 4096  # tool arguments are tiny; refuse anything bigger


def audit(event: str, ip: str, **fields) -> None:
    """One JSON line per security-relevant event, on the server's terminal.

    json.dumps escapes everything, so a tool name or argument containing a
    newline can't fake extra log lines. Never log the token.
    """
    print(json.dumps({"t": round(time.time(), 3), "event": event, "ip": ip, **fields}), flush=True)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def too_many_bad_tokens(ip: str) -> bool:
    attempts = bad_tokens[ip]
    now = time.monotonic()
    while attempts and now - attempts[0] > BAD_TOKEN_WINDOW_S:
        attempts.popleft()
    if not attempts:
        del bad_tokens[ip]  # keep the dict from growing forever
        return False
    return len(attempts) >= MAX_BAD_TOKENS


def check_token(request: Request, for_stop: bool = False) -> None:
    """Reject any request without 'Authorization: Bearer <token>'.

    `for_stop` also accepts STOP_TOKEN, and skips the rate limit.
    Raising HTTPException stops the request.
    """
    ip = client_ip(request)
    if not for_stop and too_many_bad_tokens(ip):
        raise HTTPException(status_code=429, detail="too many bad tokens; wait a minute")
    # "Bearer abc123" -> scheme="Bearer", supplied="abc123"
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    supplied_b = supplied.encode()
    # compare_digest takes the same time wherever the mismatch is, so the
    # token cannot be guessed one character at a time.
    ok = scheme == "Bearer" and (
        secrets.compare_digest(supplied_b, TOKEN.encode())
        or (for_stop and bool(STOP_TOKEN) and secrets.compare_digest(supplied_b, STOP_TOKEN.encode())))
    if not ok:
        bad_tokens[ip].append(time.monotonic())
        audit("bad_token", ip, path=request.url.path)
        raise HTTPException(status_code=401, detail="bad or missing token")


# --- App and middleware -------------------------------------------------------

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Opens the serial link to the brain for the life of the server."""
    ctx.link = BrainLink(os.environ.get("BRAIN_PORT"))
    await ctx.link.start()
    log.info("tools: %s", ", ".join(TOOLS))
    yield
    await ctx.link.close()


# docs_url etc. = None: no auto-generated API pages for strangers to browse.
app = FastAPI(title="VEX robot bridge", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
STARTED = time.time()


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

# Sent with every response. The CSP only allows this server's own files, so
# even if something sneaked into the STOP page it could not load or send
# anything elsewhere.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                               "form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class SecurityHeaders:
    """Adds SECURITY_HEADERS to every response.

    Plain ASGI on purpose, not @app.middleware("http"): that style wraps the
    request stream, which hides a client disconnect from
    request.is_disconnected(), and the server relies on noticing that to
    STOP the robot when the laptop goes away mid-motion.
    """

    HEADERS = [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {k for k, _ in self.HEADERS}
                message["headers"] = [h for h in message.get("headers", []) if h[0] not in names] + self.HEADERS
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)


# The STOP page's script and styles. Only this folder is served.
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


# --- Health -------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    """No token: is every hop up? Says ok/not-ok and a short error code per
    hop, nothing more, because anyone on the tailnet can call it. The
    details (port, round trip, messages) are in /health/details.

    ok is true only when the serial port is open and the brain program
    answers PING with the same protocol version.
    """
    hops = ctx.link.health()
    return {"ok": hops["serial"]["ok"] and hops["brain"]["ok"],
            "server": {"ok": True},
            "serial": {"ok": hops["serial"]["ok"], "error": None if hops["serial"]["ok"] else "serial_down"},
            "brain": {"ok": hops["brain"]["ok"], "error": None if hops["brain"]["ok"] else "brain_down"}}


@app.get("/health/details")
async def health_details(request: Request) -> dict:
    """With the token: the full per-hop report, including why a hop is down."""
    check_token(request)
    hops = ctx.link.health()
    return {"ok": hops["serial"]["ok"] and hops["brain"]["ok"],
            "server": {"ok": True, "uptime_s": round(time.time() - STARTED)}, **hops}


@app.get("/stop")
async def stop_page() -> FileResponse:
    """Emergency STOP page, served next to the robot so it works without the
    laptop. Pressing the button still needs a token (POST /tools/stop)."""
    return FileResponse(HERE / "stop.html")


# --- Tools --------------------------------------------------------------------

@app.get("/tools")
async def list_tools(request: Request) -> list:
    """The tools in the JSON format Ollama expects for its `tools` field."""
    check_token(request)
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description,
                          "parameters": {"type": "object", "properties": t.params, "required": t.required}}}
            for t in TOOLS.values()]


def is_plain_number(value) -> bool:
    """True for 10 or 2.5; False for "10", True, NaN or infinity.

    bool counts as int in Python (True == 1), so it is excluded by hand.
    NaN slips through clamp() (every comparison with NaN is False), so it
    must never reach the motion math.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_args(props: dict, args: dict) -> list[str]:
    """Problems with argument *types*, in words the LLM can act on."""
    problems = []
    for key, value in args.items():
        kind = props[key].get("type")
        if kind == "number" and not is_plain_number(value):
            problems.append(f"{key} must be a plain number like 10 or -2.5, not {value!r}")
        elif kind == "boolean" and not isinstance(value, bool):
            problems.append(f"{key} must be true or false, not {value!r}")
        elif kind == "string":
            allowed = props[key].get("enum")
            if not isinstance(value, str):
                problems.append(f"{key} must be text, not {value!r}")
            elif allowed and value not in allowed:
                problems.append(f"{key} must be one of {', '.join(allowed)}, not {value!r}")
    return problems


@app.post("/tools/{name}")
async def call_tool(name: str, request: Request, args: dict | None = None) -> dict:
    """Run a tool. `name` comes from the URL, `args` from the JSON body.

    LLM mistakes (unknown tool, wrong arguments) come back as a normal result
    with a hint, not an HTTP error: small models act on a plain hint. A tool
    never raises: anything unexpected becomes {"ok": false, "hop": "server"}.
    """
    # Checked here instead of with Depends so stop() can also accept STOP_TOKEN.
    check_token(request, for_stop=(name == "stop"))
    args = args or {}
    started = time.monotonic()
    result = await run_checked(name, args, request)
    audit("tool", client_ip(request), tool=name, args=args, ok=result.get("ok"),
          error=result.get("error"), ms=round((time.monotonic() - started) * 1000))
    return result


async def run_checked(name: str, args: dict, request: Request) -> dict:
    """Validate the tool name and arguments, then run the tool."""
    if name not in TOOLS:
        return fail("server", "unknown_tool", f"There is no tool named {name!r}. Available tools: {', '.join(TOOLS)}.")

    t = TOOLS[name]
    unknown = [k for k in args if k not in t.params]
    missing = [k for k in t.required if k not in args]
    problems = check_args(t.params, {k: v for k, v in args.items() if k in t.params})
    if unknown or missing or problems:
        if unknown:
            problems.append(f"it does not take {', '.join(unknown)}")
        if missing:
            problems.append(f"it is missing {', '.join(missing)}")
        hint = (f"{name}() was not run: {'; '.join(problems)}. "
                f"{name}() takes only: {', '.join(t.params) or 'no arguments'}. ")
        if name == "move" and "degrees" in unknown:
            hint += "To rotate, call turn() as a separate tool call. "
        if name == "turn" and "distance_in" in unknown:
            hint += "To move straight, call move() as a separate tool call. "
        return fail("server", "bad_args", hint + "Fix the arguments and call again.")

    task = asyncio.create_task(t.fn(**args))
    stopped_for_disconnect = False
    while True:
        done, _ = await asyncio.wait({task}, timeout=0.25)
        if done:
            break
        # The laptop went away mid-motion (crash, Wi-Fi drop, Ctrl+C): stop.
        if t.motion and not stopped_for_disconnect and await request.is_disconnected():
            stopped_for_disconnect = True
            log.warning("client disconnected during %s; sending STOP", name)
            audit("client_gone", client_ip(request), tool=name)
            ctx.stop_requested.set()
            ctx.link.send_stop_nowait()
    try:
        return task.result()
    except Exception as e:  # noqa: BLE001 - a tool bug must not become an HTTP 500
        log.exception("tool %s crashed", name)
        return fail("server", "internal_error", f"{name}() crashed on the server: {e!r}")
