"""Mock robot tool server for the LLM debugging bridge.

Runs on the Mac now, on the Pi later. The tools here are FAKE: they update an
in-memory pose and return dummy data so the laptop <-> server link can be
tested before any serial/Brain code exists.

How it fits together
--------------------
FastAPI turns Python functions into web addresses ("endpoints"). When the
laptop sends POST /tools/drive, FastAPI calls call_tool("drive", ...) below,
which looks up the drive() function and runs it. uvicorn is the program that
actually listens on the network port and hands requests to FastAPI.

Functions are `async` so the server can handle a new request (like stop)
while an earlier one (like a long drive) is still waiting.

Run (bind to this machine's Tailscale IP, so only the tailnet can reach it
and the token always travels encrypted; `tailscale ip -4` prints the IP):
    BRIDGE_TOKEN=... uvicorn server:app --host <tailscale-ip> --port 8000 --no-server-header
Optional: STOP_TOKEN=... (a second token that can only call stop(), for the
phone STOP page).
"""

import asyncio
import json
import math
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent

# --- Auth -------------------------------------------------------------------

# token_urlsafe(32) makes 43 characters; anything much shorter is guessable.
MIN_TOKEN_LEN = 32
TOKEN = os.environ.get("BRIDGE_TOKEN", "")
if len(TOKEN) < MIN_TOKEN_LEN:
    raise SystemExit(f"BRIDGE_TOKEN is missing or shorter than {MIN_TOKEN_LEN} characters. "
                     "Generate one with:\n"
                     "  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")

# A second, weaker token that can ONLY call stop(). Give this one to
# teammates for the phone STOP page: they can stop the robot but not drive it.
STOP_TOKEN = os.environ.get("STOP_TOKEN", "")
if STOP_TOKEN and (len(STOP_TOKEN) < MIN_TOKEN_LEN or STOP_TOKEN == TOKEN):
    raise SystemExit(f"STOP_TOKEN must be at least {MIN_TOKEN_LEN} characters "
                     "and different from BRIDGE_TOKEN.")

# Wrong-token attempts per client IP. After MAX_BAD_TOKENS in the window,
# that IP gets 429 without its token even being checked.
MAX_BAD_TOKENS = 10
BAD_TOKEN_WINDOW_S = 60
bad_tokens: dict[str, deque] = defaultdict(deque)


def audit(event: str, ip: str, **fields) -> None:
    """One JSON line per security-relevant event, on the server's terminal.

    json.dumps escapes everything, so a tool name or argument containing a
    newline can't fake extra log lines. Never log the token.
    """
    print(json.dumps({"t": round(time.time(), 3), "event": event, "ip": ip, **fields}),
          flush=True)


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

    `for_stop` also accepts STOP_TOKEN, and skips the rate limit: stopping
    the robot must work even while someone is hammering the server.
    Raising HTTPException stops the request.
    """
    ip = client_ip(request)
    if not for_stop and too_many_bad_tokens(ip):
        raise HTTPException(status_code=429, detail="too many bad tokens; wait a minute")
    # "Bearer abc123" -> scheme="Bearer", supplied="abc123"
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    supplied_b = supplied.encode()
    # compare_digest takes the same time whether the first or last character
    # is wrong, so the token cannot be guessed one character at a time.
    ok = scheme == "Bearer" and (
        secrets.compare_digest(supplied_b, TOKEN.encode())
        or (for_stop and bool(STOP_TOKEN)
            and secrets.compare_digest(supplied_b, STOP_TOKEN.encode())))
    if not ok:
        bad_tokens[ip].append(time.monotonic())
        audit("bad_token", ip, path=request.url.path)
        raise HTTPException(status_code=401, detail="bad or missing token")


def require_token(request: Request) -> None:
    """For endpoints that opt in with dependencies=[Depends(require_token)]."""
    check_token(request)


# --- Safety limits (enforced here, never trusted to the LLM) ----------------

MAX_SPEED_PCT = 50
MAX_DISTANCE_IN = 48.0
MAX_TURN_DEG = 360.0
COMMAND_TIMEOUT_S = 5.0
FAKE_INCHES_PER_SEC_AT_100 = 40.0  # made-up drivetrain speed for the mock
MAX_BODY_BYTES = 4096  # tool arguments are tiny; refuse anything bigger

# --- Fake robot state -------------------------------------------------------
# Stands in for the real robot. Later these values will come from the Brain.

state = {"x_in": 0.0, "y_in": 0.0, "heading_deg": 0.0, "moving": False,
         "last_command": None}
# The motion in progress, if any. stop() cancels it.
current_motion: asyncio.Task | None = None


def clamp(value: float, limit: float) -> float:
    """Keep value within [-limit, +limit], e.g. clamp(100, 48) -> 48."""
    return max(-limit, min(limit, value))


def is_plain_number(value) -> bool:
    """True for 10 or 2.5; False for "10", True, NaN or infinity.

    bool counts as int in Python (True == 1), so it is excluded by hand.
    NaN slips through clamp() (every comparison with NaN is False), so it
    must never reach the motion math.
    """
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


async def run_motion(seconds: float) -> bool:
    """Pretend to move for `seconds`. Returns False if stopped or timed out.

    The "motion" is just a sleep wrapped in a Task, which is a handle that
    another request can cancel. That is how stop() interrupts a drive.
    With the real robot, this is where we'd send the serial command and wait
    for the Brain to report it finished.
    """
    global current_motion
    state["moving"] = True
    current_motion = asyncio.create_task(asyncio.sleep(seconds))
    try:
        # wait_for gives up after COMMAND_TIMEOUT_S no matter what.
        await asyncio.wait_for(current_motion, timeout=COMMAND_TIMEOUT_S)
        return True
    except (asyncio.CancelledError, asyncio.TimeoutError):
        return False  # stop() cancelled it, or it hit the timeout
    finally:  # runs in every case, so "moving" can never get stuck at True
        state["moving"] = False
        current_motion = None


# --- Tools ------------------------------------------------------------------
# Each tool is a function plus an Ollama-format schema. GET /tools publishes
# the schemas so the agent never keeps its own copy.

async def drive(distance_in: float, speed_pct: float = 30) -> dict:
    # Refuse to start a second motion on top of the first.
    if state["moving"]:
        return {"ok": False, "error": "already moving; call stop first"}
    # Apply the safety limits. `or 1` avoids dividing by zero if speed is 0.
    distance = clamp(distance_in, MAX_DISTANCE_IN)
    speed = min(abs(speed_pct), MAX_SPEED_PCT) or 1
    # How long the fake drive "takes", based on the made-up top speed.
    seconds = abs(distance) / (FAKE_INCHES_PER_SEC_AT_100 * speed / 100)
    completed = await run_motion(seconds)
    if completed:
        # Update the fake pose with basic trig: move along the heading.
        heading = math.radians(state["heading_deg"])
        state["x_in"] += distance * math.cos(heading)
        state["y_in"] += distance * math.sin(heading)
    state["last_command"] = f"drive({distance:.1f} in @ {speed:.0f}%)"
    # This dict is what the LLM reads, so be explicit about what happened.
    return {"ok": completed, "distance_in": distance, "speed_pct": speed,
            "clamped": distance != distance_in or speed != abs(speed_pct),
            "stopped_early": not completed}


async def turn(degrees: float, speed_pct: float = 30) -> dict:
    # Same pattern as drive(); the fake turn rate is 180 deg/s at 100%.
    if state["moving"]:
        return {"ok": False, "error": "already moving; call stop first"}
    angle = clamp(degrees, MAX_TURN_DEG)
    speed = min(abs(speed_pct), MAX_SPEED_PCT) or 1
    completed = await run_motion(abs(angle) / (180 * speed / 100))
    if completed:
        state["heading_deg"] = (state["heading_deg"] + angle) % 360
    state["last_command"] = f"turn({angle:.1f} deg @ {speed:.0f}%)"
    return {"ok": completed, "degrees": angle, "speed_pct": speed,
            "clamped": angle != degrees or speed != abs(speed_pct),
            "stopped_early": not completed}


async def stop() -> dict:
    # Never waits on anything, so it answers even mid-motion.
    if current_motion is not None:
        current_motion.cancel()
    state["last_command"] = "stop()"
    return {"ok": True}


async def status() -> dict:
    # **state copies every key of `state` into this dict.
    return {"ok": True, "time": time.time(), "battery_pct": 87, **state}


# The tool registry. Each entry is:
#   name: (function, description, parameters, required parameter names)
# The description and parameter text are what the LLM reads to decide when
# and how to call a tool, so write them for the model, not for people.
TOOLS = {
    "drive": (drive, "Drive straight. Positive is forward, negative is backward.",
              {"distance_in": {"type": "number", "description": f"Inches, max {MAX_DISTANCE_IN:g}"},
               "speed_pct": {"type": "number", "description": f"Percent, max {MAX_SPEED_PCT}"}},
              ["distance_in"]),
    "turn": (turn, "Turn in place. Positive is counter-clockwise.",
             {"degrees": {"type": "number", "description": f"Degrees, max {MAX_TURN_DEG:g}"},
              "speed_pct": {"type": "number", "description": f"Percent, max {MAX_SPEED_PCT}"}},
             ["degrees"]),
    "stop": (stop, "Stop all motors immediately.", {}, []),
    "status": (status, "Read pose, battery, and whether the robot is moving.", {}, []),
}

# --- HTTP -------------------------------------------------------------------

# docs_url etc. = None: no auto-generated API pages for strangers to browse.
app = FastAPI(title="VEX robot bridge (mock)", docs_url=None, redoc_url=None,
              openapi_url=None)


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


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response


# The STOP page's script and styles. Only this folder is served, never
# server.py or anything else next to it.
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/health")
async def health() -> dict:
    """Unauthenticated so you can check the link without the token, so it
    says nothing useful to strangers. With the real robot, add the Brain's
    serial-link state here."""
    return {"ok": True, "robot": "mock"}


@app.get("/stop")
async def stop_page() -> FileResponse:
    """Emergency STOP page. Served from here, next to the robot, so it works
    even when the laptop/LLM is down. The page holds no secret: pressing the
    button still needs a token, which goes to POST /tools/stop."""
    return FileResponse(HERE / "stop.html")


@app.get("/tools", dependencies=[Depends(require_token)])
async def list_tools() -> list:
    """Convert TOOLS into the JSON format Ollama expects for its `tools` field."""
    return [{"type": "function",
             "function": {"name": name, "description": desc,
                          "parameters": {"type": "object", "properties": props,
                                         "required": required}}}
            for name, (_, desc, props, required) in TOOLS.items()]


@app.post("/tools/{name}")
async def call_tool(name: str, request: Request, args: dict | None = None) -> dict:
    """Run a tool. `name` comes from the URL, `args` from the JSON body.

    Mistakes by the LLM (unknown tool, wrong arguments) come back as a normal
    result with a hint on how to fix it, not as an HTTP error. Small models
    act on a plain-language hint and retry; a raw Python error confuses them.
    """
    # Checked here instead of with Depends so stop() can also accept STOP_TOKEN.
    check_token(request, for_stop=(name == "stop"))
    args = args or {}
    started = time.monotonic()
    result = await run_checked(name, args)
    audit("tool", client_ip(request), tool=name, args=args, ok=result.get("ok"),
          ms=round((time.monotonic() - started) * 1000))
    return result


async def run_checked(name: str, args: dict) -> dict:
    """Validate the tool name and arguments, then run the tool."""
    if name not in TOOLS:
        return {"ok": False, "error": f"There is no tool named {name!r}. "
                f"Available tools: {', '.join(TOOLS)}."}

    func, _, props, required = TOOLS[name]
    unknown = [k for k in args if k not in props]
    missing = [k for k in required if k not in args]
    if unknown or missing:
        problems = []
        if unknown:
            problems.append(f"it does not take {', '.join(unknown)}")
        if missing:
            problems.append(f"it is missing {', '.join(missing)}")
        hint = (f"{name}() was not run: {' and '.join(problems)}. "
                f"{name}() takes only: {', '.join(props) or 'no arguments'}. ")
        if name == "drive" and "degrees" in unknown:
            hint += "To rotate, call turn() as a separate tool call. "
        if name == "turn" and "distance_in" in unknown:
            hint += "To move straight, call drive() as a separate tool call. "
        return {"ok": False, "error": hint + "Fix the arguments and call again."}

    # Every argument is a number; refuse "10", true, NaN and infinity.
    bad = [k for k, v in args.items() if props[k]["type"] == "number" and not is_plain_number(v)]
    if bad:
        return {"ok": False, "error": f"{name}() was not run: {', '.join(bad)} must be a "
                "plain number like 10 or -2.5, not text. Fix the arguments and call again."}

    # **args unpacks the dict into keyword arguments:
    # {"distance_in": 10} -> drive(distance_in=10)
    return await func(**args)
