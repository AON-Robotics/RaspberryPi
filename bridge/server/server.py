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

Run:
    BRIDGE_TOKEN=... uvicorn server:app --host 0.0.0.0 --port 8000
    (--host 0.0.0.0 means "accept connections from other machines", not
    just this one.)
"""

import asyncio
import math
import os
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

# --- Auth -------------------------------------------------------------------

TOKEN = os.environ.get("BRIDGE_TOKEN")
if not TOKEN:
    raise SystemExit("BRIDGE_TOKEN is not set. Generate one with:\n"
                     "  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")


def require_token(authorization: str = Header(default="")) -> None:
    """Reject any request without 'Authorization: Bearer <BRIDGE_TOKEN>'.

    FastAPI fills `authorization` from the request's Authorization header.
    Endpoints opt in with `dependencies=[Depends(require_token)]`, which runs
    this check before the endpoint; raising HTTPException stops the request.
    """
    # "Bearer abc123" -> scheme="Bearer", supplied="abc123"
    scheme, _, supplied = authorization.partition(" ")
    # compare_digest takes the same time whether the first or last character
    # is wrong, so the token cannot be guessed one character at a time.
    if scheme != "Bearer" or not secrets.compare_digest(supplied, TOKEN):
        raise HTTPException(status_code=401, detail="bad or missing token")


# --- Safety limits (enforced here, never trusted to the LLM) ----------------

MAX_SPEED_PCT = 50
MAX_DISTANCE_IN = 48.0
MAX_TURN_DEG = 360.0
COMMAND_TIMEOUT_S = 5.0
FAKE_INCHES_PER_SEC_AT_100 = 40.0  # made-up drivetrain speed for the mock

# --- Fake robot state -------------------------------------------------------
# Stands in for the real robot. Later these values will come from the Brain.

state = {"x_in": 0.0, "y_in": 0.0, "heading_deg": 0.0, "moving": False,
         "last_command": None}
# The motion in progress, if any. stop() cancels it.
current_motion: asyncio.Task | None = None


def clamp(value: float, limit: float) -> float:
    """Keep value within [-limit, +limit], e.g. clamp(100, 48) -> 48."""
    return max(-limit, min(limit, value))


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

app = FastAPI(title="VEX robot bridge (mock)")


@app.get("/health")
async def health() -> dict:
    """Unauthenticated so you can check the link without the token."""
    return {"ok": True}


@app.get("/stop")
async def stop_page() -> FileResponse:
    """Emergency STOP page. Served from here, next to the robot, so it works
    even when the laptop/LLM is down. The page holds no secret: pressing the
    button still needs the token, which goes to POST /tools/stop."""
    return FileResponse(Path(__file__).with_name("stop.html"))


@app.get("/tools", dependencies=[Depends(require_token)])
async def list_tools() -> list:
    """Convert TOOLS into the JSON format Ollama expects for its `tools` field."""
    return [{"type": "function",
             "function": {"name": name, "description": desc,
                          "parameters": {"type": "object", "properties": props,
                                         "required": required}}}
            for name, (_, desc, props, required) in TOOLS.items()]


@app.post("/tools/{name}", dependencies=[Depends(require_token)])
async def call_tool(name: str, args: dict | None = None) -> dict:
    """Run a tool. `name` comes from the URL, `args` from the JSON body.

    Mistakes by the LLM (unknown tool, wrong arguments) come back as a normal
    result with a hint on how to fix it, not as an HTTP error. Small models
    act on a plain-language hint and retry; a raw Python error confuses them.
    """
    args = args or {}
    print(f"[tool] {name}({args})")  # log on the server's terminal
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

    # **args unpacks the dict into keyword arguments:
    # {"distance_in": 10} -> drive(distance_in=10)
    return await func(**args)
