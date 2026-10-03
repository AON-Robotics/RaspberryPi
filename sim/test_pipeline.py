"""End-to-end: LLM loop -> HTTP -> robot server -> serial (pty) -> brain code.

The brain is sim/build/brain_sim, which runs Override's real protocol and
command code with a simulated drivetrain. Nothing else is mocked: the server,
BrainLink, pyserial, the agent loop and its HTTP calls are the real ones.

Run from the RaspberryPi repo root:
    cmake -S sim -B sim/build && cmake --build sim/build
    bridge/.venv/bin/python -m pytest sim/test_pipeline.py -v
Add RUN_OLLAMA=1 to also drive it with the real local Ollama model.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import subprocess
import sys

import pytest
import requests

from pipeline import SERVER_DIR, TOKEN, Pipeline, free_port


@pytest.fixture(scope="module")
def pipe(tmp_path_factory):
    # --noise: Override's own printf output; --otos: vexpi's 50 Hz OTOS stream.
    p = Pipeline(tmp_path_factory.mktemp("pipeline"), ["--noise", "--otos"], name="main").start()
    yield p
    p.stop()


@pytest.fixture
def fresh(tmp_path):
    """A private pipeline for tests that break things."""
    pipelines = []

    def make(*sim_args):
        p = Pipeline(tmp_path, list(sim_args), name=f"p{len(pipelines)}").start()
        pipelines.append(p)
        return p
    yield make
    for p in pipelines:
        p.stop()


def run_in_background(fn, *args, **kwargs):
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault("result", fn(*args, **kwargs)))
    thread.start()
    return thread, box


def last_seq(server_log: str, command_prefix: str) -> int:
    seqs = re.findall(r"-> C,(\d+)," + re.escape(command_prefix), server_log)
    assert seqs, f"no {command_prefix} in server log"
    return int(seqs[-1])


# =============================================================================
# 1. Information gets through every hop, both ways
# =============================================================================

def test_health_reports_every_hop(pipe):
    h = pipe.health()
    assert h["ok"] and h["server"]["ok"] and h["serial"]["ok"] and h["brain"]["ok"]
    assert "port" not in h["serial"] and "robot" not in h["brain"]  # public: ok/not-ok only
    d = pipe.health_details()
    assert d["brain"]["robot"] == "sim_small_robot" and d["brain"]["proto"] == 1
    assert d["brain"]["mode"] == "driver" and d["brain"]["heartbeat_age_ms"] < 1000
    assert d["serial"]["port"].endswith("main.tty")


def test_move_round_trip_is_traceable_and_lossless(pipe):
    """The same seq and the same numbers appear at the brain, the server and the tool result."""
    result = pipe.tool("move", distance_in=12, speed_pct=30)
    assert result["ok"] and result["reached"], result

    seq = last_seq(pipe.server_text(), "MOVE,12,180")
    sim = pipe.sim_text()
    assert f"[sim] rx C,{seq},MOVE,12,180*" in sim                 # brain received the command
    assert f"[sim] tx @A,{seq}*" in sim                            # brain acknowledged it
    done = re.search(rf"\[sim\] tx (@D,{seq},ok,[^\n]+)", sim).group(1)
    assert done in pipe.server_text()                              # server received the very same line

    fields = dict(kv.split("=") for kv in done.split(",", 3)[3].rsplit("*", 1)[0].split(";"))
    assert result["traveled_in"] == round(float(fields["traveled"]), 2)
    assert result["end_pose"]["x_in"] == round(float(fields["x1"]), 2)
    assert result["tracking_in"]["left"] == round(float(fields["trk.left"]), 2)
    assert result["duration_s"] == round(int(fields["dur_ms"]) / 1000, 2)


def test_turn_is_clockwise_positive(pipe):
    before = pipe.tool("status")["pose"]["heading_deg"]
    result = pipe.tool("turn", degrees=90)
    assert result["ok"] and abs(result["turned_deg"] - 90) < 3
    assert result["tracking_in"]["left"] > 0 > result["tracking_in"]["right"]  # left side forward = turning right
    after = pipe.tool("turn", degrees=-90)
    assert after["ok"] and abs(after["end_pose"]["heading_deg"] - before) < 3


def test_status_and_sensors(pipe):
    status = pipe.tool("status")
    assert status["ok"] and status["mode"] == "driver" and status["can_move"] and status["battery_pct"] == 87
    assert status["link"]["brain"]["ok"]

    everything = pipe.tool("read_sensors")["sensors"]
    for name in ("odom", "tracking", "imu", "motors", "battery", "competition", "link", "pi_target", "sim"):
        assert name in everything, name
    assert set(everything["motors"]["L"]) == {"p11", "p12", "p13", "p14"}   # long reply survived @P chunking
    assert set(everything["motors"]["R"]) == {"p1", "p2", "p3", "p4"}

    one = pipe.tool("read_sensors", name="imu")
    assert set(one["sensors"]) == {"imu"}
    unknown = pipe.tool("read_sensors", name="lidar")
    assert not unknown["ok"] and unknown["error"] == "unknown_sensor" and "odom" in unknown["message"]


def test_long_replies_are_split_and_rejoined(pipe):
    pipe.tool("read_sensors")
    assert re.search(r"\[sim\] tx @P,\d+,", pipe.sim_text()), "the full SENSORS reply should need @P lines"
    for line in re.findall(r"\[sim\] tx (@[PD],[^\n]+)", pipe.sim_text()):
        assert len(line) <= 210, f"brain line too long: {len(line)}"


def test_reset_odometry(pipe):
    result = pipe.tool("reset_odometry", x_in=5, y_in=-3, heading_deg=0)
    assert result["ok"] and result["pose"] == {"x_in": 5.0, "y_in": -3.0, "heading_deg": 0.0}


@pytest.mark.parametrize("pattern", ["straight", "turn", "square"])
def test_odometry_test_patterns_pass_on_a_healthy_robot(pipe, pattern):
    result = pipe.tool("odometry_test", pattern=pattern, distance_in=12)
    assert result["ok"], result
    assert result["verdict"] == "pass", result["problems"]
    assert result["checks"] and "ask_user" in result


def test_diagnose_passive_and_active_on_a_healthy_robot(pipe):
    passive = pipe.tool("diagnose")
    assert passive["ok"] and passive["verdict"] == "pass", passive["problems"]
    assert not passive["active"]
    active = pipe.tool("diagnose", active=True)
    assert active["ok"] and active["verdict"] == "pass", active["problems"]
    names = {c["check"] for c in active["checks"]}
    assert {"probe_motor_L_p11", "probe_motor_R_p4", "probe_left_tracking", "probe_imu_R"} <= names
    # The brain's console output (Override printf/cout) reaches the Pi, not as errors.
    assert any("currentAngleGyro" in line for line in active["brain_console"])


def test_llm_argument_mistakes_get_a_hint(pipe):
    r = pipe.tool("move", distance_in="twelve")
    assert not r["ok"] and r["hop"] == "server" and r["error"] == "bad_args" and "number" in r["message"]
    r = pipe.tool("move", distance_in=5, degrees=90)
    assert "call turn() as a separate tool call" in r["message"]
    r = pipe.tool("odometry_test", pattern="circle")
    assert "straight, turn, square" in r["message"]
    r = pipe.tool("move", distance_in="6")  # strict: text is refused, with a hint
    assert r["error"] == "bad_args" and "plain number" in r["message"]
    r = pipe.tool("diagnose", active="yes")
    assert r["error"] == "bad_args" and "true or false" in r["message"]
    r = pipe.tool("fly")
    assert r["error"] == "unknown_tool"


def test_limits_are_clamped_and_reported(pipe):
    r = pipe.tool("turn", degrees=500, speed_pct=90)
    assert r["ok"] and r["clamped"] and r["degrees"] == 360 and r["speed_pct"] == 50


# =============================================================================
# 2. Agent loop end to end, with a scripted LLM (deterministic)
# =============================================================================

class FakeOllama:
    """Answers /api/chat with scripted replies and records what it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send({"models": [{"name": "qwen3:8b"}]})

            def do_POST(self):
                outer.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self._send({"message": outer.replies.pop(0)})

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def call(name, **args):
    return {"function": {"name": name, "arguments": args}}


@pytest.fixture
def agent_loop(monkeypatch):
    import loop

    def configure(pipe, ollama):
        monkeypatch.setattr(loop, "BRIDGE_URL", pipe.url)
        monkeypatch.setattr(loop, "AUTH", {"Authorization": f"Bearer {TOKEN}"})
        monkeypatch.setattr(loop, "OLLAMA_URL", f"http://127.0.0.1:{ollama.port}")
        return loop
    return configure


def test_agent_to_brain_and_back(pipe, agent_loop):
    """LLM tool calls reach the brain; the brain's numbers reach the LLM."""
    llm = FakeOllama([
        {"role": "assistant", "content": "", "tool_calls": [call("move", distance_in=10), call("turn", degrees=-45)]},
        {"role": "assistant", "content": "", "tool_calls": [call("status")]},
        {"role": "assistant", "content": "Moved 10 in and turned left 45."},
    ])
    try:
        loop = agent_loop(pipe, llm)
        steps = loop.check_pipeline()
        assert [s["name"] for s in steps] == ["ollama", "model", "robot", "serial", "brain", "token", "tools"]
        assert all(s["ok"] for s in steps), steps
        tools = loop.get_tools()
        assert {t["function"]["name"] for t in tools} >= {"move", "turn", "stop", "status", "odometry_test",
                                                         "diagnose", "read_sensors", "reset_odometry"}
        messages = loop.new_conversation()
        messages.append({"role": "user", "content": "drive 10 then turn left 45"})
        seen = []
        answer = loop.run_turn(messages, tools, on_tool=lambda n, a, r: seen.append((n, r)))
    finally:
        llm.close()

    assert answer == "Moved 10 in and turned left 45."
    assert [n for n, _ in seen] == ["move", "turn", "status"]
    move, turn, status = (r for _, r in seen)
    assert move["ok"] and turn["ok"] and status["ok"]
    # The second LLM request carried the brain's results back to the model.
    tool_msgs = [m for m in llm.requests[1]["messages"] if m["role"] == "tool"]
    assert json.loads(tool_msgs[0]["content"])["traveled_in"] == move["traveled_in"]
    assert json.loads(tool_msgs[1]["content"])["turned_deg"] == turn["turned_deg"]
    # And status agrees with where the turn left the robot.
    assert abs(status["pose"]["heading_deg"] - turn["end_pose"]["heading_deg"]) < 0.5


def test_agent_aborts_when_the_brain_link_breaks(fresh, agent_loop):
    p = fresh("--noise")
    llm = FakeOllama([
        {"role": "assistant", "content": "", "tool_calls": [call("move", distance_in=48, speed_pct=10),
                                                            call("turn", degrees=90)]},
    ])
    try:
        loop = agent_loop(p, llm)
        thread, box = run_in_background(loop.run_turn, loop.new_conversation(), loop.get_tools())
        assert p.wait_for_log("sim", ",MOVE,48,")
        time.sleep(0.5)
        p.kill_sim()          # USB cable pulled / brain off
        thread.join(15)
    finally:
        llm.close()
    answer = box["result"]
    assert answer.startswith("Stopped: the USB serial link between the Pi and the brain failed."), answer
    assert len(llm.requests) == 1  # no retry, turn() never sent to the model's next round


# =============================================================================
# 3. Faults: every break gives a clear hop + message, nothing hangs,
#    and the brain keeps working
# =============================================================================

def test_stop_mid_move(fresh):
    p = fresh()
    thread, box = run_in_background(p.tool, "move", distance_in=48, speed_pct=10)
    assert p.wait_for_log("sim", ",MOVE,48,")
    time.sleep(0.8)
    stop = p.tool("stop")
    thread.join(10)
    assert stop == {"ok": True, "was_moving": True}
    r = box["result"]
    assert not r["ok"] and r["error"] == "aborted" and r["reason"] == "stop" and not r["fatal"]
    assert 0 < r["traveled_in"] < 48
    assert p.tool("stop") == {"ok": True, "was_moving": False}  # idle STOP is harmless


def test_second_motion_while_moving_is_refused(fresh):
    p = fresh()
    thread, _ = run_in_background(p.tool, "move", distance_in=24, speed_pct=10)
    assert p.wait_for_log("sim", ",MOVE,24,")
    busy = p.tool("turn", degrees=90)
    assert not busy["ok"] and busy["error"] == "busy" and "stop()" in busy["message"]
    p.tool("stop")
    thread.join(10)


def test_driver_takeover_aborts_pi_motion(fresh):
    p = fresh()
    thread, box = run_in_background(p.tool, "move", distance_in=48, speed_pct=10)
    assert p.wait_for_log("sim", ",MOVE,48,")
    time.sleep(0.5)
    p.driver_touches_joystick()
    thread.join(10)
    r = box["result"]
    assert r["error"] == "aborted" and r["reason"] == "driver_override" and "joystick" in r["message"]


@pytest.mark.parametrize("mode,why", [("disabled", "disabled"), ("autonomous", "autonomous is running")])
def test_brain_refuses_motion_outside_driver_control(fresh, mode, why):
    p = fresh("--mode", mode)
    r = p.tool("move", distance_in=5)
    assert not r["ok"] and r["hop"] == "brain" and r["error"] == "refused" and why in r["message"]
    assert not r["fatal"]  # the link is fine; the LLM can explain it
    assert p.tool("status")["can_move"] is False


def test_serial_cable_pulled_mid_move_then_recovers(fresh):
    p = fresh()
    thread, box = run_in_background(p.tool, "move", distance_in=48, speed_pct=10)
    assert p.wait_for_log("sim", ",MOVE,48,")
    time.sleep(0.5)
    p.kill_sim()
    thread.join(10)
    r = box["result"]
    assert r["fatal"] and r["hop"] == "serial" and r["error"] == "serial_lost" and "USB cable" in r["message"]

    h = p.health()
    assert not h["ok"] and not h["serial"]["ok"] and h["serial"]["error"]
    down = p.tool("status")
    assert down["fatal"] and down["hop"] == "serial" and down["error"] == "serial_down"

    p.start_sim()         # cable back in: the server reconnects on its own
    assert p.wait_healthy(timeout=15)["ok"]
    assert p.tool("move", distance_in=3)["ok"]


def test_brain_program_freezes_mid_move(fresh):
    p = fresh()
    thread, box = run_in_background(p.tool, "move", distance_in=48, speed_pct=10)
    assert p.wait_for_log("sim", ",MOVE,48,")
    time.sleep(0.5)
    p.freeze_brain()
    thread.join(10)
    r = box["result"]
    assert r["fatal"] and r["hop"] == "brain" and r["error"] == "brain_silent", r
    assert not p.health()["brain"]["ok"]
    p.freeze_brain()      # unfreeze: the stale motion gets aborted, link recovers
    # Either the deadman fires first (it heard nothing while frozen) or the
    # STOP the server queued is read first; both end the motion.
    deadline = time.time() + 5
    while time.time() < deadline and not re.search(r"\[PI\] (aborted|link_lost)", p.sim_text()):
        time.sleep(0.1)
    assert re.search(r"\[PI\] (aborted|link_lost)", p.sim_text())
    assert p.wait_healthy()["ok"]
    assert p.tool("status")["moving"] is False


def test_pi_server_dies_mid_move_brain_aborts_and_keeps_working(fresh):
    p = fresh()
    thread, _ = run_in_background(lambda: _swallow(p.tool, "move", distance_in=48, speed_pct=10))
    assert p.wait_for_log("sim", ",MOVE,48,")
    time.sleep(0.5)
    p.kill_server()
    assert p.wait_for_log("sim", "[PI] link_lost", timeout=3), "brain deadman should fire within ~1 s"
    thread.join(10)

    # The driver loop keeps running once the Pi is gone (status line every 1 s).
    after = []
    deadline = time.time() + 4
    while len(after) < 2 and time.time() < deadline:
        time.sleep(0.2)
        tail = p.sim_text().split("[PI] link_lost")[-1]
        after = re.findall(r"driver_loops=(\d+) pi_control=(\d)", tail)
    assert len(after) >= 2, after
    assert all(control == "0" for _, control in after)
    assert int(after[-1][0]) > int(after[0][0])

    p.start_server()      # Pi back: everything works again
    assert p.wait_healthy()["ok"]
    assert p.tool("move", distance_in=3)["ok"]


def test_laptop_disconnects_mid_move_server_stops_robot(fresh):
    p = fresh()
    body = json.dumps({"distance_in": 48, "speed_pct": 10})
    with socket.create_connection(("127.0.0.1", p.port)) as s:
        s.sendall((f"POST /tools/move HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}").encode())
        assert p.wait_for_log("sim", ",MOVE,48,")
        time.sleep(0.5)
    # socket closed: the laptop is gone
    assert p.wait_for_log("server", "client disconnected during move", timeout=3)
    assert p.wait_for_log("sim", "[PI] aborted reason=stop", timeout=3)


def test_garbage_from_the_pi_never_hurts_the_brain(fresh):
    p = fresh()
    fd = os.open(str(p.tty), os.O_WRONLY | os.O_NOCTTY)
    try:
        for line in [b"hello brain\n", b"C,5,MOVE,10*00\n", b"C,6,MOVE,10\n", b"A" * 300 + b"\n",
                     b"\x00\xff\xfe junk\n", b"C,7,FLY*" + b"%02X" % _xor(b"C,7,FLY") + b"\n",
                     b"C,8,MOVE,a,b,c*" + b"%02X" % _xor(b"C,8,MOVE,a,b,c") + b"\n",
                     b"R,42\n"]:
            os.write(fd, line)
    finally:
        os.close(fd)
    time.sleep(0.5)
    link = p.tool("read_sensors", name="link")["sensors"]["link"]
    assert link["bad_checksum"] >= 2 and link["too_long"] >= 1 and link["unknown_verb"] >= 1
    target = p.tool("read_sensors", name="pi_target")["sensors"]["pi_target"]
    assert target["visible"] == 1 and target["inches"] == 42      # legacy red_tracker packet still works
    assert p.health()["ok"] and p.tool("move", distance_in=3)["ok"]
    assert "bad_checksum" in p.sim_text() and "unknown verb FLY" in p.sim_text()


def test_brain_console_noise_is_tolerated(pipe):
    assert pipe.health_details()["serial"]["bad_lines"] == 0
    assert "brain console: currentAngleGyro" in pipe.server_text()


# =============================================================================
# 3a. Sharing the port with vexpi's OTOS stream (main, PR #5)
# =============================================================================

def test_otos_packets_reach_the_brain_without_reply_traffic(pipe):
    otos = pipe.tool("read_sensors", name="pi_otos")["sensors"]["pi_otos"]
    assert otos["seen"] == 1 and otos["age_ms"] < 300
    truth = pipe.tool("read_sensors", name="sim")["sensors"]["sim"]   # vexpi stand-in streams the true pose
    assert abs(otos["x"] - truth["truth_x"]) < 0.5 and abs(otos["y"] - truth["truth_y"]) < 0.5
    before = pipe.tool("read_sensors", name="link")["sensors"]["link"]["packets"]
    time.sleep(0.5)
    after = pipe.tool("read_sensors", name="link")["sensors"]["link"]["packets"]
    assert after - before >= 15                  # ~50 Hz arriving
    assert "tx @D,0," not in pipe.sim_text()     # and never answered


def test_otos_stream_does_not_keep_the_deadman_alive(fresh):
    p = fresh("--otos")
    thread, _ = run_in_background(lambda: _swallow(p.tool, "move", distance_in=48, speed_pct=10))
    assert p.wait_for_log("sim", ",MOVE,48,")
    time.sleep(0.5)
    p.kill_server()       # the bridge dies; vexpi keeps streaming O packets
    assert p.wait_for_log("sim", "[PI] link_lost", timeout=3), "deadman must fire even with OTOS streaming"
    thread.join(10)


def test_unknown_sensor_packets_are_counted_not_answered(fresh):
    p = fresh()
    fd = os.open(str(p.tty), os.O_WRONLY | os.O_NOCTTY)
    try:
        for line in [b"Z,1,2\n", b"123,0\n", b"O,1.000,2.000,3.000\n"]:
            os.write(fd, line)
    finally:
        os.close(fd)
    time.sleep(0.3)
    link = p.tool("read_sensors", name="link")["sensors"]["link"]
    assert link["unknown_packets"] == 1 and link["ignored_lines"] == 1 and link["packets"] == 1
    otos = p.tool("read_sensors", name="pi_otos")["sensors"]["pi_otos"]
    assert (otos["x"], otos["y"], otos["heading"]) == (1, 2, 3)
    assert "tx @D,0," not in p.sim_text()


def test_odometry_test_uses_otos_as_an_independent_check(pipe):
    r = pipe.tool("odometry_test", pattern="straight", distance_in=12)
    assert r["verdict"] == "pass" and r["independent_check"].startswith("OTOS")
    otos = next(c for c in r["checks"] if c["check"] == "odometry_vs_otos_distance")
    assert otos["status"] == "pass" and abs(otos["otos_in"] - 12) < 1
    r = pipe.tool("odometry_test", pattern="turn")
    assert next(c for c in r["checks"] if c["check"] == "imu_vs_otos_rotation")["status"] == "pass"


def test_otos_catches_odometry_that_lies(fresh):
    """A backwards tracking wheel makes odometry under-read; OTOS sees the truth."""
    p = fresh("--otos", "--fault", "reversed_left_tracking")
    r = p.tool("odometry_test", pattern="straight", distance_in=12)
    otos = next(c for c in r["checks"] if c["check"] == "odometry_vs_otos_distance")
    assert otos["status"] == "fail" and otos["otos_in"] > 10 > otos["odometry_in"]


def test_diagnose_reports_the_otos_stream(fresh, pipe):
    live = {c["check"]: c for c in pipe.tool("diagnose")["checks"]}
    assert live["otos_stream"]["status"] == "pass"
    missing = {c["check"]: c for c in fresh().tool("diagnose")["checks"]}
    assert missing["otos_stream"]["status"] == "warn" and "vexp.service" in missing["otos_stream"]["fix"]


# =============================================================================
# 3b. Security layers merged from agent-implementation, on the real server
# =============================================================================

def test_tokens_and_rate_limit(fresh, tmp_path):
    p = fresh()
    stop_token = "stop-only-token-0123456789abcdefghijkl"
    p.kill_server()
    p.start_server(extra_env={"STOP_TOKEN": stop_token})
    p.wait_healthy()
    url = f"{p.url}/tools"

    # STOP_TOKEN can stop, but not drive or even list the tools.
    stop_only = {"Authorization": f"Bearer {stop_token}"}
    assert requests.post(f"{url}/stop", json={}, headers=stop_only, timeout=5).json()["ok"]
    assert requests.post(f"{url}/move", json={"distance_in": 3}, headers=stop_only, timeout=5).status_code == 401
    assert requests.get(url, headers=stop_only, timeout=5).status_code == 401

    # 10 wrong tokens per minute -> 429 for everything except STOP. The two
    # STOP_TOKEN attempts above already count, so 8 more reach the limit.
    bad = {"Authorization": "Bearer " + "x" * 40}
    codes = [requests.get(url, headers=bad, timeout=5).status_code for _ in range(9)]
    assert codes[:8] == [401] * 8 and codes[8] == 429
    assert requests.post(f"{url}/status", json={}, headers=p.auth, timeout=5).status_code == 429
    assert requests.post(f"{url}/stop", json={}, headers=p.auth, timeout=5).json()["ok"]  # STOP still works
    assert '"event": "bad_token"' in p.server_text()                                     # audited
    assert p.health_text_has_no_token(TOKEN, stop_token)


def test_body_limit_headers_and_no_docs(pipe):
    big = requests.post(f"{pipe.url}/tools/status", data="x" * 5000, headers=pipe.auth, timeout=5)
    assert big.status_code == 413
    r = requests.get(f"{pipe.url}/health", timeout=5)
    assert r.headers["X-Frame-Options"] == "DENY" and "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert requests.get(f"{pipe.url}/docs", timeout=5).status_code == 404
    assert requests.get(f"{pipe.url}/stop", timeout=5).status_code == 200
    assert requests.get(f"{pipe.url}/static/stop.js", timeout=5).status_code == 200


def test_short_token_is_refused(tmp_path):
    env = {**os.environ, "BRIDGE_TOKEN": "short", "BRAIN_PORT": "/dev/null"}
    out = subprocess.run([sys.executable, "-c", "import server"], cwd=SERVER_DIR, env=env,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode != 0 and "shorter than 32" in out.stderr


def test_pipeline_check_names_the_broken_step(fresh, agent_loop):
    p = fresh()
    llm = FakeOllama([])
    try:
        loop = agent_loop(p, llm)
        p.kill_sim()
        time.sleep(1)
        steps = {s["name"]: s for s in loop.check_pipeline()}
    finally:
        llm.close()
    assert steps["robot"]["ok"] and not steps["serial"]["ok"]
    assert "serial" in steps["serial"]["detail"] or "open" in steps["serial"]["detail"]
    assert "USB cable" in steps["serial"]["fix"]
    assert steps["brain"]["detail"] == "skipped: serial failed"


# =============================================================================
# 4. Faulty robot: the tools find the problems
# =============================================================================

def test_tools_find_wiring_faults(fresh):
    p = fresh("--fault", "reversed_left_tracking,reversed_motor:13,hot_motor:11")
    diag = p.tool("diagnose", active=True)
    assert diag["ok"] and diag["verdict"] == "fail"
    text = "\n".join(diag["problems"])
    assert "motor p13 (side L) spins BACKWARDS" in text and "port 13" in text
    assert "left tracking wheel rolled BACKWARDS" in text
    assert "motor p11 (side L): 62 C" in text

    odom = p.tool("odometry_test", pattern="straight", distance_in=12)
    assert odom["verdict"] == "fail"
    assert any("left tracking wheel counts backwards" in prob for prob in odom["problems"])


def test_tools_find_missing_hardware(fresh):
    p = fresh("--fault", "dead_motor:2,no_imu")
    diag = p.tool("diagnose")
    text = "\n".join(diag["problems"])
    assert "drive motor p2 (side R) is not detected" in text
    assert "IMU (port 16) is not detected" in text


# =============================================================================
# 5. Optional: the real local LLM
# =============================================================================

@pytest.mark.skipif(not os.environ.get("RUN_OLLAMA"), reason="set RUN_OLLAMA=1 to use the local Ollama model")
def test_real_llm_picks_the_right_tools(pipe, monkeypatch):
    import loop
    monkeypatch.setattr(loop, "BRIDGE_URL", pipe.url)
    monkeypatch.setattr(loop, "AUTH", {"Authorization": f"Bearer {TOKEN}"})
    steps = loop.check_pipeline()
    assert all(s["ok"] for s in steps), steps
    tools = loop.get_tools()

    def ask(text):
        messages = loop.new_conversation()
        messages.append({"role": "user", "content": text})
        seen = []
        answer = loop.run_turn(messages, tools, on_tool=lambda n, a, r: seen.append((n, a, r)))
        print(f"\nUSER: {text}\nTOOLS: {[(n, a) for n, a, _ in seen]}\nLLM: {answer}")
        return seen, answer

    seen, _ = ask("Drive forward 12 inches, then turn right 90 degrees.")
    assert [n for n, _, _ in seen][:2] == ["move", "turn"]
    assert seen[0][1]["distance_in"] == 12 and seen[1][1]["degrees"] == 90

    seen, _ = ask("Turn left 45 degrees.")
    assert seen[0][0] == "turn" and seen[0][1]["degrees"] == -45

    seen, answer = ask("Run an odometry test driving straight.")
    assert seen[0][0] == "odometry_test" and seen[0][1]["pattern"] == "straight"

    seen, answer = ask("Check the drivetrain for problems.")
    assert seen[0][0] == "diagnose"


# --- helpers -------------------------------------------------------------------

def _xor(data: bytes) -> int:
    value = 0
    for b in data:
        value ^= b
    return value


def _swallow(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - the server is killed on purpose
        return e
