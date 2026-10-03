"""Runs the Pi side and a simulated brain on this machine.

    Pipeline(...)  starts sim/build/brain_sim on a pty and the real robot
    server (bridge/server) pointed at that pty, exactly as it would run on
    the Pi against /dev/ttyACM1.

Used by test_pipeline.py and run_pipeline.sh. Logs land in `workdir`.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "sim" / "build" / "brain_sim"
SERVER_DIR = ROOT / "bridge" / "server"
TOKEN = "pipeline-test-token-0123456789abcdefghij"  # server needs >= 32 chars


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Pipeline:
    def __init__(self, workdir: Path, sim_args: list[str] | None = None, name: str = "run"):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.tty = self.workdir / f"{name}.tty"
        self.sim_log = self.workdir / f"{name}.sim.log"
        self.server_log = self.workdir / f"{name}.server.log"
        self.sim_args = sim_args or []
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.sim: subprocess.Popen | None = None
        self.server: subprocess.Popen | None = None

    # --- processes --------------------------------------------------------------

    def start_sim(self) -> None:
        if not SIM.exists():
            raise RuntimeError(f"{SIM} not built; run: cmake -S sim -B sim/build && cmake --build sim/build")
        log = open(self.sim_log, "a")
        self.sim = subprocess.Popen([str(SIM), "--link", str(self.tty), *self.sim_args], stdout=log, stderr=log)
        deadline = time.time() + 5
        while not self.tty.exists():
            if time.time() > deadline:
                raise RuntimeError("brain_sim did not create its pty")
            time.sleep(0.05)

    def start_server(self, extra_env: dict | None = None) -> None:
        env = {**os.environ, "BRIDGE_TOKEN": TOKEN, "BRAIN_PORT": str(self.tty), **(extra_env or {})}
        log = open(self.server_log, "a")
        self.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=SERVER_DIR, env=env, stdout=log, stderr=log)

    def start(self) -> "Pipeline":
        self.start_sim()
        self.start_server()
        self.wait_healthy()
        return self

    def kill_sim(self) -> None:
        if self.sim:
            self.sim.kill()
            self.sim.wait()
            self.sim = None

    def kill_server(self) -> None:
        if self.server:
            self.server.kill()
            self.server.wait()
            self.server = None

    def signal_sim(self, sig: int) -> None:
        self.sim.send_signal(sig)

    def freeze_brain(self) -> None:
        """Toggle: the brain program stops reading and writing (a hang)."""
        self.signal_sim(signal.SIGUSR1)

    def driver_touches_joystick(self) -> None:
        self.signal_sim(signal.SIGUSR2)

    def stop(self) -> None:
        self.kill_server()
        self.kill_sim()

    # --- HTTP -------------------------------------------------------------------

    @property
    def auth(self) -> dict:
        return {"Authorization": f"Bearer {TOKEN}"}

    def health(self) -> dict:
        """The public /health: ok/not-ok per hop."""
        return requests.get(f"{self.url}/health", timeout=3).json()

    def health_details(self) -> dict:
        """/health/details: the full report, needs the token."""
        return requests.get(f"{self.url}/health/details", headers=self.auth, timeout=3).json()

    def wait_healthy(self, timeout: float = 15) -> dict:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                last = self.health()
                if last["ok"]:
                    return last
            except requests.RequestException as e:
                last = str(e)
            time.sleep(0.2)
        raise RuntimeError(f"pipeline not healthy after {timeout}s: {last}")

    def tool(self, tool_name: str, /, timeout: float = 120, **args) -> dict:
        r = requests.post(f"{self.url}/tools/{tool_name}", json=args, headers=self.auth, timeout=timeout)
        r.raise_for_status()
        return r.json()

    # --- logs -------------------------------------------------------------------

    def health_text_has_no_token(self, *tokens: str) -> bool:
        """The server log (audit lines included) must never contain a token."""
        text = self.server_text()
        return not any(t in text for t in tokens)

    def sim_text(self) -> str:
        return self.sim_log.read_text(errors="replace")

    def server_text(self) -> str:
        return self.server_log.read_text(errors="replace")

    def wait_for_log(self, which: str, needle: str, timeout: float = 5) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in (self.sim_text() if which == "sim" else self.server_text()):
                return True
            time.sleep(0.1)
        return False
