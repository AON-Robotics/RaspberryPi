"""Tool registry for the robot server.

A tool is an async function plus the description the LLM reads. Register one
with the @tool decorator and it shows up in GET /tools and the agent with no
other change:

    @tool("read_battery", "Battery charge in percent.")
    async def read_battery() -> dict:
        msg = await ctx.link.request("SENSORS", "battery")
        return ok(**msg.fields)

Result conventions (the LLM and the web page read these):
    {"ok": True, ...fields}
    {"ok": False, "hop": "server|serial|brain", "error": "<code>",
     "message": "<what happened and what to do>", "fatal": bool, ...}

"hop" says where the chain broke. "fatal" means the link itself failed: the
agent stops the whole turn instead of letting the LLM try again.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from dataclasses import dataclass, field

from brain_link import BrainLink
from protocol import Message


@dataclass
class Tool:
    name: str
    fn: object
    description: str
    params: dict
    required: list
    motion: bool  # moves the robot: one at a time, STOP on client disconnect


TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, params: dict | None = None, required=(), motion: bool = False):
    def register(fn):
        TOOLS[name] = Tool(name, fn, description, params or {}, list(required), motion)
        return fn
    return register


def ok(**fields) -> dict:
    return {"ok": True, **fields}


def fail(hop: str, code: str, message: str, *, fatal: bool = False, **extra) -> dict:
    return {"ok": False, "hop": hop, "error": code, "message": message, "fatal": fatal, **extra}


class Busy(Exception):
    pass


@dataclass
class Context:
    """State shared by all tools. server.py sets `link` at startup."""
    link: BrainLink | None = None
    last_command: str | None = None
    current_motion: str | None = None
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)
    _motion_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @contextlib.asynccontextmanager
    async def motion(self, label: str):
        """One robot motion (or motion sequence) at a time."""
        if self._motion_lock.locked():
            raise Busy(self.current_motion or "a motion")
        async with self._motion_lock:
            self.current_motion = label
            self.stop_requested.clear()
            try:
                yield
            finally:
                self.current_motion = None
                self.last_command = label


ctx = Context()


def busy_result(what: str) -> dict:
    return fail("server", "busy", f"{what} is still running; wait for it to finish or call stop().")


ABORT_REASONS = {
    "stop": "stopped by a stop() call (STOP button or the LLM)",
    "driver_override": "the driver moved a joystick and took control back",
    "x_button": "X was pressed on the controller",
    "link_lost": "the brain stopped hearing from the Pi and aborted on its own",
}


def brain_failure(msg: Message, **extra) -> dict:
    """Turns a non-ok '@D' reply into a tool result."""
    fields = msg.fields
    if msg.status == "err":
        return fail("brain", str(fields.get("code", "error")), str(fields.get("msg", "the brain refused the command")),
                    **extra)
    if msg.status == "aborted":
        reason = str(fields.get("reason", "unknown"))
        return fail("brain", "aborted", f"motion aborted: {ABORT_REASONS.get(reason, reason)}.",
                    fatal=reason == "link_lost", reason=reason, **extra)
    if msg.status == "timeout":
        return fail("brain", "timeout", "the brain's motion timed out before reaching the target "
                    "(Override's own timeout); the robot is stopped.", **extra)
    return fail("brain", "unknown_status", f"unexpected status {msg.status!r} from the brain", **extra)


def num(value, digits: int = 2):
    """Rounds numbers for the LLM; passes None/strings through."""
    if isinstance(value, float) and math.isfinite(value):
        return round(value, digits)
    return value


def facing(heading_deg) -> str | None:
    """A plain-words label for a heading, so the LLM never interprets angles.

    Heading is degrees clockwise from the direction the robot faced when
    odometry was reset. The model kept describing 180 as "left" (it mixed it
    up with the -90 it had just turned), so the server says it instead.
    """
    if not isinstance(heading_deg, (int, float)) or not math.isfinite(heading_deg):
        return None
    h = (heading_deg + 180.0) % 360.0 - 180.0  # -180..180
    if abs(h) <= 20:
        return "forward (the starting direction)"
    if abs(h) >= 160:
        return "backwards"
    if 70 <= h <= 110:
        return "right"
    if -110 <= h <= -70:
        return "left"
    side = "right" if h > 0 else "left"
    return f"forward-{side}" if abs(h) < 90 else f"backward-{side}"


def pose(fields: dict, suffix: str = "") -> dict:
    heading = num(fields.get("th" + suffix))
    return {"x_in": num(fields.get("x" + suffix)), "y_in": num(fields.get("y" + suffix)),
            "heading_deg": heading, "facing": facing(heading)}


def heading_delta(a: float, b: float) -> float:
    """Shortest signed b - a in degrees."""
    return (b - a + 180.0) % 360.0 - 180.0


# Import the tool modules so their @tool decorators run.
from . import motion, status, sensors, odometry, diagnostics, pi_sensors  # noqa: E402,F401
