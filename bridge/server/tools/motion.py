"""move / turn / stop: thin wrappers over Override's drivetrain.move() and
drivetrain.turn() running on the brain (see Override/src/aon/pi/commands.cpp).

Safety limits are enforced here, and again (looser) on the brain.
"""

from __future__ import annotations

from brain_link import LinkError

from . import Busy, brain_failure, busy_result, ctx, num, ok, pose, tool

MAX_SPEED_PCT = 50
DEFAULT_SPEED_PCT = 30
MAX_DISTANCE_IN = 48.0
MAX_TURN_DEG = 360.0


def clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def speed(speed_pct: float) -> tuple[float, float]:
    """(clamped percent, motor RPM). `or 1` keeps a 0 % request moving slowly."""
    pct = min(abs(speed_pct), MAX_SPEED_PCT) or 1
    return pct, pct / 100 * ctx.link.max_rpm()


def motion_summary(fields: dict) -> dict:
    trk = fields.get("trk") or {}
    return {
        "start_pose": pose(fields, "0"),
        "end_pose": pose(fields, "1"),
        "tracking_in": {"left": num(trk.get("left")), "right": num(trk.get("right")), "back": num(trk.get("back"))},
        "imu_deg": num(fields.get("imu")),
        "duration_s": num((fields.get("dur_ms") or 0) / 1000),
    }


async def run_move(distance_in: float, speed_pct: float) -> tuple[dict, dict]:
    """One MOVE on the brain. Returns (tool result, raw brain fields).

    Raises LinkError; the caller must hold the motion slot.
    """
    distance = clamp(float(distance_in), MAX_DISTANCE_IN)
    pct, rpm = speed(float(speed_pct))
    # Override's driveProfiled() gives up after distance/3 seconds.
    msg = await ctx.link.request("MOVE", distance, round(rpm), timeout=abs(distance) / 3 + 5, motion=True)
    fields = msg.fields
    base = {"distance_in": distance, "speed_pct": pct,
            "clamped": distance != float(distance_in) or pct != abs(float(speed_pct)),
            "reached": bool(fields.get("reached")), "traveled_in": num(fields.get("traveled")),
            **motion_summary(fields)}
    if msg.status != "ok":
        return brain_failure(msg, **base), fields
    return ok(**base), fields


async def run_turn(degrees: float, speed_pct: float) -> tuple[dict, dict]:
    angle = clamp(float(degrees), MAX_TURN_DEG)
    pct, rpm = speed(float(speed_pct))
    # Override's turnProfiled() gives up after sqrt(angle/2) seconds.
    msg = await ctx.link.request("TURN", angle, round(rpm), timeout=(abs(angle) / 2) ** 0.5 + 5, motion=True)
    fields = msg.fields
    base = {"degrees": angle, "speed_pct": pct,
            "clamped": angle != float(degrees) or pct != abs(float(speed_pct)),
            "reached": bool(fields.get("reached")), "turned_deg": num(fields.get("turned")),
            "position_drift_in": num(fields.get("drift")), **motion_summary(fields)}
    if msg.status != "ok":
        return brain_failure(msg, **base), fields
    return ok(**base), fields


SPEED_PARAM = {"type": "number", "description": f"Percent of top speed, default {DEFAULT_SPEED_PCT}, max {MAX_SPEED_PCT}"}


@tool("move", "Drive straight with the robot's own motion profile and odometry. "
      "Positive distance is forward, negative is backward. Returns where odometry thinks the robot went.",
      {"distance_in": {"type": "number", "description": f"Inches, max {MAX_DISTANCE_IN:g}"},
       "speed_pct": SPEED_PARAM},
      ["distance_in"], motion=True)
async def move(distance_in: float, speed_pct: float = DEFAULT_SPEED_PCT) -> dict:
    try:
        async with ctx.motion(f"move({distance_in} in)"):
            result, _ = await run_move(distance_in, speed_pct)
            return result
    except Busy as e:
        return busy_result(str(e))
    except LinkError as e:
        return e.to_result()


@tool("turn", "Turn in place. Positive degrees turn CLOCKWISE (to the right), negative turn "
      "counter-clockwise (to the left). Example: 'turn left 90' is degrees=-90.",
      {"degrees": {"type": "number", "description": f"Degrees, max {MAX_TURN_DEG:g}. Positive = right/clockwise"},
       "speed_pct": SPEED_PARAM},
      ["degrees"], motion=True)
async def turn(degrees: float, speed_pct: float = DEFAULT_SPEED_PCT) -> dict:
    try:
        async with ctx.motion(f"turn({degrees} deg)"):
            result, _ = await run_turn(degrees, speed_pct)
            return result
    except Busy as e:
        return busy_result(str(e))
    except LinkError as e:
        return e.to_result()


@tool("stop", "Stop the robot immediately. Only stops motion the Pi started; driver control is never touched.")
async def stop() -> dict:
    ctx.stop_requested.set()  # also ends multi-step tools like odometry_test
    try:
        msg = await ctx.link.request("STOP", timeout=1.0)
    except LinkError as e:
        result = e.to_result()
        result["message"] += " The brain aborts Pi motion by itself within 1 s of losing the Pi."
        return result
    if msg.status != "ok":
        return brain_failure(msg)
    return ok(was_moving=bool(msg.fields.get("was_moving")))

