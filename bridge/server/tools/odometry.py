"""odometry_test and reset_odometry.

The test drives a short pattern with the brain's own move()/turn() and checks
what the tracking wheels, the IMU and odometry reported against each other.
It cannot know where the robot *really* is, so it also asks the user to
measure with a tape and compare.

The analysis functions are pure (fields in, checks out) so they can be unit
tested without a robot: see bridge/tests/test_analysis.py.

Override odometry recap (Override/src/aon/odometry/odometry.cpp):
    heading   = IMU (GYRO_CONFIDENCE = 1)
    distance  = average of left/right tracking wheels
    encoder heading change = (left - right) / (offset_left + offset_right)
"""

from __future__ import annotations

import math

from brain_link import LinkError

from . import Busy, brain_failure, busy_result, ctx, fail, heading_delta, num, ok, tool
from .motion import DEFAULT_SPEED_PCT, MAX_DISTANCE_IN, run_move, run_turn

ORDER = {"pass": 0, "warn": 1, "fail": 2}


def check(name: str, status: str, detail: str, fix: str | None = None, **values) -> dict:
    result = {"check": name, "status": status, "detail": detail, **{k: num(v) for k, v in values.items()}}
    if fix:
        result["fix"] = fix
    return result


def grade(value: float, warn: float, fail_at: float) -> str:
    value = abs(value)
    return "fail" if value > fail_at else "warn" if value > warn else "pass"


def verdict(checks: list[dict]) -> str:
    return max((c["status"] for c in checks), key=ORDER.__getitem__, default="pass")


def _tracking(fields: dict) -> tuple[float, float]:
    trk = fields.get("trk") or {}
    return float(trk.get("left") or 0.0), float(trk.get("right") or 0.0)


def analyze_straight(fields: dict, target: float) -> list[dict]:
    """Checks for one MOVE of `target` inches, from its '@D' fields."""
    checks = []
    left, right = _tracking(fields)
    expected = math.copysign(1, target)
    wheels_ok = True
    for side, value in (("left", left), ("right", right)):
        if abs(value) < 0.2 * abs(target):
            wheels_ok = False
            checks.append(check(f"{side}_tracking_wheel", "fail",
                                f"{side} tracking wheel only read {value:.2f} in for a {target:g} in move",
                                f"check the {side} rotation sensor is plugged in, on the right port, and its wheel "
                                "touches the floor", value_in=value))
        elif math.copysign(1, value) != expected:
            wheels_ok = False
            checks.append(check(f"{side}_tracking_wheel", "fail",
                                f"{side} tracking wheel counts backwards ({value:.2f} in for a {target:g} in move)",
                                f"flip the sign of the {side} port in aon::Odometry(...) in globals.hpp",
                                value_in=value))
        else:
            checks.append(check(f"{side}_tracking_wheel", "pass", f"{side} tracking wheel read {value:.2f} in",
                                value_in=value))

    if wheels_ok:
        mismatch = abs(left - right) / max(abs(left), abs(right)) * 100
        checks.append(check("left_right_agreement", grade(mismatch, 5, 15),
                            f"left and right tracking wheels differ by {mismatch:.1f}% on a straight move",
                            "the robot is veering or a wheel slips; check wheel tension and TRACKING_WHEEL_DIAMETER"
                            if mismatch > 5 else None, mismatch_pct=mismatch))

    drift = heading_delta(float(fields.get("th0") or 0), float(fields.get("th1") or 0))
    checks.append(check("heading_hold", grade(drift, 2, 5),
                        f"heading changed {drift:+.1f} deg during a straight move",
                        "the drive pulls to one side (motor/friction imbalance) or the IMU drifts" if abs(drift) > 2
                        else None, drift_deg=drift))

    traveled = float(fields.get("traveled") or 0)
    if wheels_ok:
        wheels = abs(left + right) / 2
        diff = abs(traveled - wheels) / max(wheels, 1e-6) * 100
        checks.append(check("odometry_matches_wheels", grade(diff, 5, 15),
                            f"odometry says {traveled:.2f} in, the tracking wheels average {wheels:.2f} in",
                            "odometry math disagrees with its own sensors; check Odometry::update()" if diff > 5 else None,
                            odometry_in=traveled, wheels_in=wheels))
    if not fields.get("reached"):
        checks.append(check("reached_target", "warn",
                            f"the move stopped at {traveled:.2f} of {abs(target):g} in (Override's timeout)",
                            "the robot may be blocked, too slow at this speed, or odometry under-reads"))
    return checks


def analyze_turn(fields: dict, target: float, offsets_sum: float | None) -> list[dict]:
    """Checks for one TURN of `target` degrees (positive = clockwise)."""
    checks = []
    turned = float(fields.get("turned") or 0)
    error = turned - target
    checks.append(check("turn_accuracy", grade(error, 3, 10),
                        f"IMU says the robot turned {turned:.1f} deg for a {target:g} deg request",
                        "tune the turn motion profile / CLOCKWISE_ROTATION_DEGREES_OFFSET" if abs(error) > 3 else None,
                        turned_deg=turned, error_deg=error))
    if abs(turned) < 2:
        checks.append(check("imu_reading", "fail", "the IMU registered almost no rotation",
                            "check the IMU port and that it finished calibrating"))
        return checks

    left, right = _tracking(fields)
    if left * right > 0:
        checks.append(check("tracking_wheels_in_turn", "warn",
                            f"both tracking wheels rolled the same way during a turn in place "
                            f"(left {left:.2f}, right {right:.2f} in)",
                            "one tracking wheel is probably reversed; run odometry_test pattern=straight"))
    elif offsets_sum and offsets_sum > 0:
        encoder_deg = math.degrees((left - right) / offsets_sum)
        ratio = encoder_deg / turned
        suggested = (left - right) / math.radians(turned)
        checks.append(check("encoder_heading", grade(ratio - 1, 0.1, 0.3),
                            f"tracking wheels imply {encoder_deg:.1f} deg vs IMU {turned:.1f} deg "
                            "(only used if the gyro is disabled; GYRO_CONFIDENCE is 1)",
                            f"set DISTANCE_LEFT/RIGHT_TRACKING_WHEEL_CENTER so they sum to {suggested:.3f} in "
                            f"(now {offsets_sum:.3f})" if abs(ratio - 1) > 0.1 else None,
                            encoder_deg=encoder_deg, ratio=ratio))

    drift = float(fields.get("drift") or 0)
    checks.append(check("turn_in_place", grade(drift, 1, 3),
                        f"odometry position moved {drift:.2f} in during a turn in place",
                        "the robot scrubs sideways while turning, or odometry leaks heading into position"
                        if drift > 1 else None, drift_in=drift))
    return checks


def analyze_closure(start: dict, end: dict, warn_in: float, fail_in: float, warn_deg: float, fail_deg: float) -> list[dict]:
    """Did the pattern bring odometry back to where it started?"""
    distance = math.hypot(end["x_in"] - start["x_in"], end["y_in"] - start["y_in"])
    heading = heading_delta(start["heading_deg"], end["heading_deg"])
    return [
        check("position_closure", grade(distance, warn_in, fail_in),
              f"odometry ends {distance:.2f} in from where the pattern started", closure_in=distance),
        check("heading_closure", grade(heading, warn_deg, fail_deg),
              f"odometry heading ends {heading:+.1f} deg from the start", closure_deg=heading),
    ]


def pose_of(fields: dict, suffix: str) -> dict:
    return {"x_in": float(fields.get("x" + suffix) or 0), "y_in": float(fields.get("y" + suffix) or 0),
            "heading_deg": float(fields.get("th" + suffix) or 0)}


class StepFailed(Exception):
    def __init__(self, result: dict):
        self.result = result


async def step(kind: str, value: float, speed_pct: float) -> dict:
    if ctx.stop_requested.is_set():
        raise StepFailed(fail("server", "aborted", "odometry test stopped by a stop() call"))
    result, fields = await (run_move(value, speed_pct) if kind == "move" else run_turn(value, speed_pct))
    if not result["ok"] and result.get("error") != "timeout":
        raise StepFailed(result)
    return fields


@tool("odometry_test", "Drive a short pattern and check the odometry: tracking wheel directions, left/right "
      "agreement, heading hold, turn accuracy, and whether odometry returns to the start. "
      "pattern: 'straight' (forward then back), 'turn' (90 right then 90 left), "
      "'square' (4 x forward + 90 right). Moves the robot; needs about 1 m of clear space.",
      {"pattern": {"type": "string", "enum": ["straight", "turn", "square"], "description": "Which pattern to run"},
       "distance_in": {"type": "number", "description": f"Leg length in inches, default 24, max {MAX_DISTANCE_IN:g}"},
       "speed_pct": {"type": "number", "description": f"Percent of top speed, default {DEFAULT_SPEED_PCT}"}},
      ["pattern"], motion=True)
async def odometry_test(pattern: str, distance_in: float = 24, speed_pct: float = DEFAULT_SPEED_PCT) -> dict:
    if pattern not in ("straight", "turn", "square"):
        return fail("server", "bad_args", "pattern must be 'straight', 'turn' or 'square'")
    distance = min(abs(float(distance_in)), MAX_DISTANCE_IN) or 12
    try:
        async with ctx.motion(f"odometry_test({pattern})"):
            tracking = await ctx.link.request("SENSORS", "tracking", timeout=1.0)
            offsets = (tracking.fields.get("tracking") or {}).get("offset") or {}
            offsets_sum = (offsets.get("left") or 0) + (offsets.get("right") or 0)

            checks: list[dict] = []
            legs: list[dict] = []
            if pattern == "straight":
                out = await step("move", distance, speed_pct)
                back = await step("move", -distance, speed_pct)
                legs = [out, back]
                checks += analyze_straight(out, distance)
                checks += analyze_closure(pose_of(out, "0"), pose_of(back, "1"), 1, 3, 2, 5)
            elif pattern == "turn":
                right = await step("turn", 90, speed_pct)
                left = await step("turn", -90, speed_pct)
                legs = [right, left]
                checks += analyze_turn(right, 90, offsets_sum)
                checks += analyze_closure(pose_of(right, "0"), pose_of(left, "1"), 1, 3, 2, 5)
            else:
                for i in range(4):
                    legs.append(await step("move", distance, speed_pct))
                    legs.append(await step("turn", 90, speed_pct))
                checks += analyze_straight(legs[0], distance)
                checks += analyze_turn(legs[1], 90, offsets_sum)
                checks += analyze_closure(pose_of(legs[0], "0"), pose_of(legs[-1], "1"), 2, 6, 3, 8)
    except Busy as e:
        return busy_result(str(e))
    except StepFailed as e:
        return {**e.result, "message": f"odometry test stopped: {e.result.get('message')}"}
    except LinkError as e:
        return e.to_result()

    first = legs[0]
    return ok(pattern=pattern, verdict=verdict(checks),
              checks=checks,
              problems=[f"{c['check']}: {c['detail']}" + (f" -> {c['fix']}" if c.get("fix") else "")
                        for c in checks if c["status"] != "pass"],
              odometry_traveled_in=num(first.get("traveled")) if pattern != "turn" else None,
              ask_user=(f"Measure how far the robot really moved on the first leg. Odometry says "
                        f"{num(first.get('traveled'))} in. If they differ, scale TRACKING_WHEEL_DIAMETER by "
                        "real/odometry." if pattern != "turn" else
                        "Check the robot ended facing the same way it started."))


@tool("reset_odometry", "Set the odometry pose (default 0, 0, 0). Re-tares the IMU, which takes about 3 s; "
      "keep the robot still.",
      {"x_in": {"type": "number", "description": "X in inches, default 0"},
       "y_in": {"type": "number", "description": "Y in inches, default 0"},
       "heading_deg": {"type": "number", "description": "Heading in degrees, default 0"}},
      motion=True)
async def reset_odometry(x_in: float = 0, y_in: float = 0, heading_deg: float = 0) -> dict:
    try:
        async with ctx.motion("reset_odometry"):
            msg = await ctx.link.request("RESET_ODOM", float(x_in), float(y_in), float(heading_deg),
                                         timeout=8.0, motion=True)
    except Busy as e:
        return busy_result(str(e))
    except LinkError as e:
        return e.to_result()
    if msg.status != "ok":
        return brain_failure(msg)
    return ok(pose={"x_in": num(msg.fields.get("x")), "y_in": num(msg.fields.get("y")),
                    "heading_deg": num(msg.fields.get("th"))})
