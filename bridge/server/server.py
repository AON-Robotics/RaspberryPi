"""Robot tool server for the LLM debugging bridge. Runs on the Pi.

    laptop (agent/web) --HTTP + token--> this server --USB serial--> V5 brain

The tools live in tools/ (one module per area); this file is only the HTTP
layer: auth, the tool list, argument checks and the health report.

The brain side is Override/src/aon/pi/ (protocol.cpp, commands.cpp,
pi-link.cpp). The wire format is in docs/serial-protocol.md.

Settings (environment variables)
--------------------------------
    BRIDGE_TOKEN  required; shared secret with the laptop
    BRAIN_PORT    serial port of the brain's USB *user* port; default: auto
                  detect (/dev/serial/by-id, then /dev/ttyACM1). For the
                  laptop simulator, the pty path printed by sim/brain_sim.

Run:
    BRIDGE_TOKEN=... uvicorn server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from brain_link import BrainLink
from tools import TOOLS, ctx, fail

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bridge.server")

# --- Auth -------------------------------------------------------------------

TOKEN = os.environ.get("BRIDGE_TOKEN")
if not TOKEN:
    raise SystemExit("BRIDGE_TOKEN is not set. Generate one with:\n"
                     "  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")


def require_token(authorization: str = Header(default="")) -> None:
    """Reject any request without 'Authorization: Bearer <BRIDGE_TOKEN>'."""
    scheme, _, supplied = authorization.partition(" ")
    # compare_digest takes the same time wherever the mismatch is.
    if scheme != "Bearer" or not secrets.compare_digest(supplied, TOKEN):
        raise HTTPException(status_code=401, detail="bad or missing token")


# --- App --------------------------------------------------------------------

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    ctx.link = BrainLink(os.environ.get("BRAIN_PORT"))
    await ctx.link.start()
    log.info("tools: %s", ", ".join(TOOLS))
    yield
    await ctx.link.close()


app = FastAPI(title="VEX robot bridge", lifespan=lifespan)
STARTED = time.time()


@app.get("/health")
async def health() -> dict:
    """Unauthenticated: every hop from here to the robot program.

    ok is true only when the serial port is open and the brain program
    answers PING with the same protocol version.
    """
    hops = ctx.link.health()
    return {"ok": hops["serial"]["ok"] and hops["brain"]["ok"],
            "server": {"ok": True, "uptime_s": round(time.time() - STARTED)}, **hops}


@app.get("/stop")
async def stop_page() -> FileResponse:
    """Emergency STOP page, served next to the robot so it works without the
    laptop. Pressing the button still needs the token (POST /tools/stop)."""
    return FileResponse(Path(__file__).with_name("stop.html"))


@app.get("/tools", dependencies=[Depends(require_token)])
async def list_tools() -> list:
    """The tools in the JSON format Ollama expects for its `tools` field."""
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description,
                          "parameters": {"type": "object", "properties": t.params, "required": t.required}}}
            for t in TOOLS.values()]


def coerce(props: dict, args: dict) -> tuple[dict, list]:
    """Small models send "12" for 12 or "true" for true; fix what is safe."""
    fixed, problems = {}, []
    for key, value in args.items():
        kind = props[key].get("type")
        if kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            try:
                value = float(value)
            except (TypeError, ValueError):
                problems.append(f"{key} must be a number, got {value!r}")
                continue
        elif kind == "boolean" and not isinstance(value, bool):
            if str(value).lower() in ("true", "1", "yes"):
                value = True
            elif str(value).lower() in ("false", "0", "no", ""):
                value = False
            else:
                problems.append(f"{key} must be true or false, got {value!r}")
                continue
        elif kind == "string":
            value = "" if value is None else str(value)
            allowed = props[key].get("enum")
            if allowed and value not in allowed:
                problems.append(f"{key} must be one of {', '.join(allowed)}, got {value!r}")
                continue
        fixed[key] = value
    return fixed, problems


@app.post("/tools/{name}", dependencies=[Depends(require_token)])
async def call_tool(name: str, request: Request, args: dict | None = None) -> dict:
    """Run a tool. `name` comes from the URL, `args` from the JSON body.

    LLM mistakes (unknown tool, wrong arguments) come back as a normal result
    with a hint, not an HTTP error: small models act on a plain hint. A tool
    never raises: anything unexpected becomes {"ok": false, "hop": "server"}.
    """
    args = args or {}
    log.info("tool %s(%s)", name, args)
    if name not in TOOLS:
        return fail("server", "unknown_tool", f"There is no tool named {name!r}. Available tools: {', '.join(TOOLS)}.")

    t = TOOLS[name]
    unknown = [k for k in args if k not in t.params]
    missing = [k for k in t.required if k not in args]
    args, problems = coerce(t.params, {k: v for k, v in args.items() if k in t.params})
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
            ctx.stop_requested.set()
            ctx.link.send_stop_nowait()
    try:
        result = task.result()
    except Exception as e:  # noqa: BLE001 - a tool bug must not become an HTTP 500
        log.exception("tool %s crashed", name)
        result = fail("server", "internal_error", f"{name}() crashed on the server: {e!r}")
    log.info("tool %s -> %s", name, {k: result.get(k) for k in ("ok", "hop", "error", "message") if k in result})
    return result
