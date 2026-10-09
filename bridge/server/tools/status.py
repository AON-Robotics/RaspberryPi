"""status: where the robot is and whether every hop of the link is healthy."""

from __future__ import annotations

from brain_link import LinkError

from . import brain_failure, ctx, ok, pose, tool


@tool("status", "Read the robot's pose (from its odometry), battery, mode, whether it is moving, and the "
      "health of every link hop (server, serial port, brain program).")
async def status() -> dict:
    health = ctx.link.health()
    try:
        msg = await ctx.link.request("STATUS", timeout=1.0)
    except LinkError as e:
        return {**e.to_result(), "link": health}
    if msg.status != "ok":
        return brain_failure(msg, link=health)
    f = msg.fields
    return ok(pose=pose(f),
              mode=f.get("mode"), moving=bool(f.get("pi")), busy=f.get("busy"),
              battery_pct=f.get("bat"), can_move=bool(f.get("can_move")), cannot_move_because=f.get("why"),
              last_command=ctx.last_command, current_motion=ctx.current_motion, link=health)
