"""Odometry-test and diagnostic analysis on synthetic brain replies."""

from tools.diagnostics import analyze_probe, analyze_sensors
from tools.odometry import (analyze_closure, analyze_otos_straight, analyze_otos_turn, analyze_straight,
                            analyze_turn, verdict)


def statuses(checks):
    return {c["check"]: c["status"] for c in checks}


def move_fields(left=24.0, right=24.0, th0=0.0, th1=0.0, traveled=24.0, reached=1):
    return {"trk": {"left": left, "right": right, "back": 0}, "th0": th0, "th1": th1,
            "traveled": traveled, "reached": reached, "x0": 0, "y0": 0, "x1": traveled, "y1": 0}


def test_good_straight_move_passes():
    checks = analyze_straight(move_fields(), 24)
    assert verdict(checks) == "pass", checks


def test_reversed_left_tracking_wheel_is_caught_with_a_fix():
    checks = analyze_straight(move_fields(left=-24.0, traveled=0.0), 24)
    s = statuses(checks)
    assert s["left_tracking_wheel"] == "fail"
    fix = next(c["fix"] for c in checks if c["check"] == "left_tracking_wheel")
    assert "flip the sign" in fix and "globals.hpp" in fix


def test_veering_and_heading_drift():
    s = statuses(analyze_straight(move_fields(left=24, right=19, th0=0, th1=6), 24))
    assert s["left_right_agreement"] == "fail" and s["heading_hold"] == "fail"


def test_heading_drift_handles_the_180_seam():
    s = statuses(analyze_straight(move_fields(th0=179, th1=-179), 24))
    assert s["heading_hold"] == "pass"  # 2 deg, not 358


def test_turn_checks_and_offset_suggestion():
    good = {"turned": 90.2, "trk": {"left": 1.77, "right": -1.77}, "drift": 0.1}
    assert verdict(analyze_turn(good, 90, 2.25)) == "pass"
    wrong_offsets = {"turned": 90.0, "trk": {"left": 3.0, "right": -3.0}, "drift": 0.1}
    check = next(c for c in analyze_turn(wrong_offsets, 90, 2.25) if c["check"] == "encoder_heading")
    assert check["status"] == "fail" and "sum to 3.820" in check["fix"]


def test_closure():
    start = {"x_in": 0, "y_in": 0, "heading_deg": 0}
    assert verdict(analyze_closure(start, {"x_in": 0.5, "y_in": 0, "heading_deg": 1}, 1, 3, 2, 5)) == "pass"
    assert verdict(analyze_closure(start, {"x_in": 4, "y_in": 0, "heading_deg": 1}, 1, 3, 2, 5)) == "fail"


def test_sensor_analysis_flags_disconnected_hot_and_missing():
    sensors = {
        "competition": {"mode": "driver"},
        "battery": {"pct": 12},
        "motors": {"L": {"p11": {"connected": 1, "temp": 62, "over_temp": 1},
                         "p12": {"connected": 0}}},
        "tracking": {"left": {"installed": 0, "port": 19}, "right": {"installed": 1, "port": 18}},
        "imu": {"installed": 1, "calibrating": 0, "heading": 10},
        "odom": {"x": 1, "y": None, "th": 0},
    }
    s = statuses(analyze_sensors(sensors))
    assert s["battery"] == "fail"
    assert s["motor_L_p11"] == "fail" and s["motor_L_p12"] == "fail"
    assert s["left_tracking_sensor"] == "fail" and s["right_tracking_sensor"] == "pass"
    assert s["odometry_values"] == "fail"


def test_probe_finds_backwards_motor_and_tracking_wheel():
    fields = {
        "L": {"motors": {"p11": 98, "p12": 97, "p13": -60, "p14": 99}, "trk": {"left": -0.8, "right": 0.1}, "imu": 9.0},
        "R": {"motors": {"p1": 99, "p2": 98, "p3": 97, "p4": 0}, "trk": {"left": 0.1, "right": 0.9}, "imu": -9.0},
    }
    s = statuses(analyze_probe(fields, 100))
    assert s["probe_motor_L_p13"] == "fail"       # backwards
    assert s["probe_motor_R_p4"] == "fail"        # did not spin
    assert s["probe_left_tracking"] == "fail"     # backwards
    assert s["probe_right_tracking"] == "pass"
    assert s["probe_imu_L"] == "pass" and s["probe_imu_R"] == "pass"


def test_otos_cross_check():
    before, after = {"x": 0, "y": 0, "heading": 0}, {"x": 24, "y": 0.5, "heading": 0}
    good = analyze_otos_straight({"traveled": 24.1}, before, after)[0]
    assert good["status"] == "pass"
    short = analyze_otos_straight({"traveled": 20.0}, before, after)[0]
    assert short["status"] == "fail" and "TRACKING_WHEEL_DIAMETER by 1.2" in short["fix"]
    turn = analyze_otos_turn({"turned": 90.5}, {"heading": 179}, {"heading": -91})[0]
    assert turn["status"] == "pass" and abs(turn["otos_deg"] - 90) < 0.01   # across the 180 seam


def test_facing_labels_cover_the_circle():
    from tools import facing
    assert facing(0.4).startswith("forward") and facing(-12).startswith("forward")
    assert facing(89.7) == "right" and facing(-90.04) == "left"
    assert facing(179.85) == "backwards" and facing(-179.9) == "backwards" and facing(540) == "backwards"
    assert facing(45) == "forward-right" and facing(-135) == "backward-left"
    assert facing(None) is None
