"""diagnose: one-call health check of the link, drivetrain and odometry.

Passive (default) only reads. active=true also runs PROBE on the brain: each
drive side spins forward alone for half a second so we can check that every
motor, tracking wheel and the IMU respond in the right direction. That catches
the usual wiring mistakes: swapped ports, a motor with the wrong reversal, a
backwards tracking wheel.

The analysis functions are pure, see bridge/tests/test_analysis.py.
"""

from __future__ import annotations

from brain_link import LinkError

from . import Busy, brain_failure, busy_result, ctx, ok, tool
from .odometry import OTOS_STALE_MS, check, verdict

PROBE_RPM = 100
PROBE_MS = 500
HOT_WARN_C = 50
HOT_FAIL_C = 60
BATTERY_WARN = 30
BATTERY_FAIL = 15


def analyze_link(health: dict, link_sensor: dict) -> list[dict]:
    checks = []
    serial, brain = health["serial"], health["brain"]
    checks.append(check("serial_port", "pass" if serial["ok"] else "fail",
                        f"serial port {serial.get('port')} open" if serial["ok"] else str(serial.get("error"))))
    checks.append(check("brain_program", "pass" if brain["ok"] else "fail",
                        f"brain answering, robot={brain.get('robot')}, round trip {brain.get('rtt_ms')} ms"
                        if brain["ok"] else str(brain.get("error"))))
    if serial.get("bad_lines"):
        checks.append(check("brain_to_pi_lines", "warn", f"{serial['bad_lines']} corrupt lines from the brain",
                            "check the USB cable; something may be printing without a newline"))
    bad = (link_sensor.get("bad_checksum") or 0) + (link_sensor.get("too_long") or 0)
    if bad:
        checks.append(check("pi_to_brain_lines", "warn", f"the brain rejected {bad} corrupt lines from the Pi"))
    if link_sensor.get("deadman_trips"):
        checks.append(check("deadman", "warn",
                            f"the brain aborted {link_sensor['deadman_trips']} Pi motion(s) after losing the Pi",
                            "the Pi or its server stalled; check the Pi's load and the USB cable"))
    return checks


def analyze_sensors(s: dict) -> list[dict]:
    """Checks on a full SENSORS reply."""
    checks = []

    comp = s.get("competition") or {}
    if comp.get("mode") != "driver":
        checks.append(check("robot_mode", "warn", f"robot is in {comp.get('mode')} mode",
                            "move/turn/odometry_test only work in driver control (enabled, not autonomous)"))

    battery = (s.get("battery") or {}).get("pct")
    if isinstance(battery, (int, float)):
        status = "fail" if battery < BATTERY_FAIL else "warn" if battery < BATTERY_WARN else "pass"
        checks.append(check("battery", status, f"battery at {battery:.0f}%",
                            "charge or swap the battery" if status != "pass" else None, pct=battery))

    for group, motors in (s.get("motors") or {}).items():
        for port, m in motors.items():
            name = f"motor_{group}_{port}"
            if not m.get("connected"):
                checks.append(check(name, "fail", f"drive motor {port} (side {group}) is not detected",
                                    f"check the cable on port {port[1:]} and the port list in globals.hpp"))
                continue
            temp = m.get("temp")
            problems = []
            if isinstance(temp, (int, float)) and temp >= HOT_WARN_C:
                problems.append(f"{temp:.0f} C")
            if m.get("over_temp"):
                problems.append("over-temperature flag (power is being cut)")
            if m.get("over_current"):
                problems.append("over-current flag")
            if problems:
                status = "fail" if m.get("over_temp") or (temp or 0) >= HOT_FAIL_C else "warn"
                checks.append(check(name, status, f"motor {port} (side {group}): " + ", ".join(problems),
                                    "let it cool; check for friction or a jammed drivetrain", temp_c=temp))
            else:
                checks.append(check(name, "pass", f"motor {port} (side {group}) ok, {temp} C", temp_c=temp))

    tracking = s.get("tracking") or {}
    for side in ("left", "right"):
        wheel = tracking.get(side) or {}
        if wheel.get("installed") == 0:
            checks.append(check(f"{side}_tracking_sensor", "fail",
                                f"{side} tracking rotation sensor (port {wheel.get('port')}) is not detected",
                                "odometry distance will be wrong; check its cable and port"))
        elif wheel:
            checks.append(check(f"{side}_tracking_sensor", "pass", f"{side} tracking sensor on port {wheel.get('port')}"))

    imu = s.get("imu") or {}
    if imu.get("installed") == 0:
        checks.append(check("imu", "fail", f"IMU (port {imu.get('port')}) is not detected",
                            "odometry heading and every turn depend on it; check its cable and port"))
    elif imu.get("calibrating"):
        checks.append(check("imu", "warn", "IMU is still calibrating", "keep the robot still for a few seconds"))
    elif imu:
        checks.append(check("imu", "pass", f"IMU heading {imu.get('heading')} deg"))

    # OTOS stream from the Pi's vexpi service (O packets). Optional for the
    # robot, but odometry_test uses it as an independent check.
    otos = s.get("pi_otos") or {}
    if not otos.get("seen"):
        checks.append(check("otos_stream", "warn", "no OTOS packets from vexpi have reached the brain",
                            "on the Pi: systemctl status vexp.service (sudo systemctl start vexp.service)"))
    elif (otos.get("age_ms") or 0) > OTOS_STALE_MS:
        checks.append(check("otos_stream", "warn", f"the last OTOS packet is {otos.get('age_ms')} ms old",
                            "vexpi stopped sending (OTOS fault or the service stopped): "
                            "journalctl -u vexp.service -f", age_ms=otos.get("age_ms")))
    else:
        checks.append(check("otos_stream", "pass", f"OTOS live: x={otos.get('x')} y={otos.get('y')} "
                            f"heading={otos.get('heading')} ({otos.get('age_ms')} ms old)"))

    odom = s.get("odom") or {}
    if any(odom.get(k) is None for k in ("x", "y", "th")):
        checks.append(check("odometry_values", "fail", f"odometry has missing/NaN values: {odom}",
                            "odometry task may have crashed; restart the program"))
    return checks


def analyze_probe(fields: dict, rpm: float) -> list[dict]:
    """Checks on a PROBE reply: side L then side R spun forward alone."""
    checks = []
    for side, wheel, sign_name in (("L", "left", "clockwise"), ("R", "right", "counter-clockwise")):
        data = fields.get(side) or {}
        for port, peak in (data.get("motors") or {}).items():
            name = f"probe_motor_{side}_{port}"
            if peak is None:
                checks.append(check(name, "fail", f"motor {port} (side {side}) gave no velocity reading"))
            elif peak < -0.3 * rpm:
                checks.append(check(name, "fail", f"motor {port} (side {side}) spins BACKWARDS (peak {peak:.0f} rpm)",
                                    f"flip the sign of port {port[1:]} in the drivetrain port list in globals.hpp",
                                    peak_rpm=peak))
            elif peak < 0.3 * rpm:
                checks.append(check(name, "fail", f"motor {port} (side {side}) barely moved (peak {peak:.0f} rpm)",
                                    "unplugged, burnt out, or fighting the other motors of its side", peak_rpm=peak))
            else:
                checks.append(check(name, "pass", f"motor {port} (side {side}) peak {peak:.0f} rpm", peak_rpm=peak))

        trk = (data.get("trk") or {}).get(wheel)
        if trk is None:
            pass
        elif trk < -0.1:
            checks.append(check(f"probe_{wheel}_tracking", "fail",
                                f"{wheel} tracking wheel rolled BACKWARDS ({trk:.2f} in) while side {side} drove forward",
                                f"flip the sign of the {wheel} port in aon::Odometry(...) in globals.hpp", value_in=trk))
        elif trk < 0.1:
            checks.append(check(f"probe_{wheel}_tracking", "warn",
                                f"{wheel} tracking wheel barely moved ({trk:.2f} in) while side {side} drove forward",
                                "is it touching the floor and on the right port?", value_in=trk))
        else:
            checks.append(check(f"probe_{wheel}_tracking", "pass", f"{wheel} tracking wheel rolled {trk:.2f} in",
                                value_in=trk))

        imu = data.get("imu")
        expected = 1 if side == "L" else -1
        if imu is None or abs(imu) < 2:
            checks.append(check(f"probe_imu_{side}", "fail",
                                f"IMU registered {imu} deg while side {side} drove alone",
                                "IMU unplugged/wrong port, or the side did not move"))
        elif imu * expected < 0:
            checks.append(check(f"probe_imu_{side}", "fail",
                                f"IMU turned the wrong way ({imu:.1f} deg, expected {sign_name})",
                                "IMU mounted upside down, or the left and right motor groups are swapped"))
        else:
            checks.append(check(f"probe_imu_{side}", "pass", f"IMU turned {imu:.1f} deg {sign_name}"))
    return checks


@tool("diagnose", "Full health check of the robot: link hops, battery, mode, every drive motor (connected, "
      "temperature, faults), tracking wheels, IMU and odometry. With active=true it also spins each drive side "
      "for half a second to check motor and sensor directions (the robot will twitch in place).",
      {"active": {"type": "boolean", "description": "Also spin each side briefly to test directions. Default false"}},
      motion=True)
async def diagnose(active: bool = False) -> dict:
    health = ctx.link.health()
    try:
        msg = await ctx.link.request("SENSORS", timeout=1.0)
    except LinkError as e:
        return {**e.to_result(), "checks": analyze_link(health, {}), "link": health,
                "brain_console": list(ctx.link.console)[-10:]}
    if msg.status != "ok":
        return brain_failure(msg, link=health)
    sensors = msg.fields
    checks = analyze_link(health, sensors.get("link") or {}) + analyze_sensors(sensors)

    probe_error = None
    if active:
        try:
            async with ctx.motion("diagnose(active)"):
                probe = await ctx.link.request("PROBE", PROBE_RPM, PROBE_MS, timeout=10.0, motion=True)
            if probe.status == "ok":
                checks += analyze_probe(probe.fields, PROBE_RPM)
            else:
                probe_error = brain_failure(probe)
                checks.append(check("probe", "fail", f"active test did not run: {probe_error['message']}"))
        except Busy as e:
            return busy_result(str(e))
        except LinkError as e:
            return {**e.to_result(), "checks": checks}

    counts = {s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "warn", "fail")}
    return ok(verdict=verdict(checks), counts=counts,
              problems=[f"{c['check']}: {c['detail']}" + (f" -> {c['fix']}" if c.get("fix") else "")
                        for c in checks if c["status"] != "pass"],
              checks=checks, active=active, link=health,
              recent_brain_events=list(ctx.link.events)[-5:],
              brain_console=list(ctx.link.console)[-10:])
