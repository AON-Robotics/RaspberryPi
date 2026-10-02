"""read_sensors: every sensor registered on the brain.

New sensors are added on the brain only (link.registerSensor in
Override/src/aon/pi/pi-link.cpp); they appear here without any change.
"""

from __future__ import annotations

from brain_link import LinkError

from . import brain_failure, ctx, ok, tool


@tool("read_sensors", "Read raw sensor values from the brain: odom, tracking, imu, motors, battery, "
      "competition, link, pi_target, and any sensor added later. Leave name empty to read all of them.",
      {"name": {"type": "string", "description": "One sensor name, or empty for all"}})
async def read_sensors(name: str = "") -> dict:
    args = (name,) if name else ()
    try:
        msg = await ctx.link.request("SENSORS", *args, timeout=1.0)
    except LinkError as e:
        return e.to_result()
    if msg.status != "ok":
        return brain_failure(msg)
    return ok(sensors=msg.fields)
