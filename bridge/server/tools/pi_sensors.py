"""Sensors attached to the Pi itself (not the brain): extension point.

The Pi already has two sensors with C++ drivers in this repo:
  - SparkFun OTOS optical odometry (src/otos.cpp, apps/otos_monitor.cpp)
  - OAK-D Lite depth camera (src/oak_camera.cpp, apps/red_tracker.cpp)

To expose one to the LLM, read it here and register a tool. Example sketch
for the OTOS, once otos_monitor writes its pose to a file or socket:

    @tool("otos_pose", "Pose from the Pi's OTOS optical odometry sensor, to compare with the brain's odometry.")
    async def otos_pose() -> dict:
        x, y, heading = read_otos()          # your reader
        return ok(x_in=x, y_in=y, heading_deg=heading)

red_tracker already streams R,<inches> packets to the brain; the brain shows
the latest one as the `pi_target` sensor (read_sensors name=pi_target).
"""

from __future__ import annotations
