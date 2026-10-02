"""Serial link to the V5 brain: the Pi side of protocol.py.

What it does
------------
- Finds and opens the brain's USB *user* port, and reopens it if the cable is
  pulled or the brain reboots.
- Sends commands with a sequence number and matches the brain's replies to
  them, so a STOP can go out while a MOVE is still waiting for its result.
- Pings the brain every 300 ms. That keeps the brain's deadman happy (it
  aborts any Pi motion after 1 s of silence) and tells us if the brain stops
  answering.
- Keeps the brain's own console output (lines without '@') for diagnostics.

Every failure is raised as a LinkError that says which hop broke ("serial":
the port/cable, "brain": the robot program) in words the user can act on.

The serial port is read by a background thread (pyserial is blocking); lines
are handed to the asyncio loop, which owns all the state.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import threading
import time
from collections import deque

import serial  # pyserial

from protocol import PROTOCOL_VERSION, Message, ProtocolError, encode_command, parse_fields, parse_line

log = logging.getLogger("bridge.link")

# Verbs that run every few hundred ms; not worth a log line each.
QUIET_VERBS = {"PING"}


class LinkError(Exception):
    """A failure between the server and the brain.

    hop:  "serial" (port missing/closed, cable) or "brain" (program not
          answering, timed out, frozen)
    code: short machine-readable reason
    """

    def __init__(self, hop: str, code: str, message: str):
        super().__init__(message)
        self.hop = hop
        self.code = code
        self.message = message

    def to_result(self) -> dict:
        return {"ok": False, "fatal": True, "hop": self.hop, "error": self.code, "message": self.message}


def find_brain_port() -> str | None:
    """Best guess at the brain's USB *user* port (where stdin/stdout go).

    A brain plugged in directly shows two CDC-ACM ports: the system/comms
    port (used to upload programs) and the user port. On Linux the user port
    is usually the second one (/dev/ttyACM1). Set BRAIN_PORT to override.
    """
    by_id = sorted(glob.glob("/dev/serial/by-id/*V5*Brain*") + glob.glob("/dev/serial/by-id/*VEX*"))
    by_id = sorted(set(by_id))
    if by_id:
        user = [p for p in by_id if "if02" in p or "User" in p]
        return user[0] if user else by_id[-1]
    acm = sorted(glob.glob("/dev/ttyACM*"))
    if acm:
        return acm[1] if len(acm) > 1 else acm[0]
    mac = sorted(glob.glob("/dev/cu.usbmodem*"))  # brain on a Mac, for desk testing
    if mac:
        return mac[-1]
    return None


class BrainLink:
    def __init__(self, port: str | None = None, *, ping_interval: float = 0.3,
                 heartbeat_timeout: float = 1.0, reply_timeout: float = 0.5):
        self.port_setting = port            # None/"auto" -> find_brain_port()
        self.ping_interval = ping_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.reply_timeout = reply_timeout

        self.port: str | None = None
        self.last_error: str | None = "not started"
        self._serial: serial.Serial | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []
        self._closing = False

        self._seq = 0
        self._pending: dict[int, tuple[asyncio.Future, asyncio.Event, str]] = {}
        self._fire_and_forget: set[int] = set()
        self._partials: dict[int, list[str]] = {}  # seq -> '@P' field texts

        # Brain health
        self._last_rx = 0.0             # monotonic time of the last valid '@' line
        self.last_heartbeat: dict = {}
        self._last_heartbeat_at = 0.0
        self.brain_info: dict = {}      # from PING: proto, robot, max_rpm, up
        self.brain_ok = False
        self.brain_error: str | None = "no reply yet"
        self.rtt_ms: float | None = None

        # Diagnostics
        self.console: deque = deque(maxlen=50)   # brain printf output
        self.events: deque = deque(maxlen=50)    # @E lines and unsolicited replies
        self.counters = {"tx": 0, "rx": 0, "bad_lines": 0, "reconnects": 0}

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._closing = False
        self._tasks = [asyncio.create_task(self._connect_loop()), asyncio.create_task(self._keepalive())]

    async def close(self) -> None:
        self._closing = True
        for task in self._tasks:
            task.cancel()
        if self._serial is not None:
            self._drop(self._serial, "server shutting down")

    @property
    def connected(self) -> bool:
        return self._serial is not None

    def _open(self, port: str) -> serial.Serial:
        return serial.Serial(port, 115200, timeout=0.1, write_timeout=0.5)

    async def _connect_loop(self) -> None:
        backoff = 0.5
        while not self._closing:
            if self._serial is None:
                setting = self.port_setting
                port = find_brain_port() if setting in (None, "", "auto") else setting
                if port is None:
                    self._set_serial_error("no V5 brain serial port found; plug the brain's USB into the Pi or set BRAIN_PORT")
                else:
                    try:
                        ser = await self._loop.run_in_executor(None, self._open, port)
                    except Exception as e:  # noqa: BLE001 - any open failure means "port down"
                        self._set_serial_error(f"could not open {port}: {e}")
                    else:
                        self._serial, self.port, self.last_error = ser, port, None
                        self.counters["reconnects"] += 1
                        backoff = 0.5
                        log.info("serial: connected to %s", port)
                        threading.Thread(target=self._reader, args=(ser,), daemon=True, name="brain-reader").start()
                        continue
                backoff = min(backoff * 2, 5.0)
                await asyncio.sleep(backoff)
            else:
                await asyncio.sleep(0.5)

    def _set_serial_error(self, message: str) -> None:
        if message != self.last_error:
            log.warning("serial: %s", message)
        self.last_error = message

    def _reader(self, ser: serial.Serial) -> None:
        """Background thread: bytes -> lines -> event loop."""
        buffer = b""
        try:
            while not self._closing and self._serial is ser:
                chunk = ser.read(256)
                if not chunk:
                    continue
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    self._loop.call_soon_threadsafe(self._on_line, line.decode("ascii", "replace"))
                if len(buffer) > 8192:  # no newline in 8 KB: garbage, start over
                    buffer = b""
        except Exception as e:  # noqa: BLE001 - pyserial raises several types on unplug
            self._loop.call_soon_threadsafe(self._drop, ser, f"serial read failed: {e}")

    def _drop(self, ser: serial.Serial, reason: str) -> None:
        """Port lost: close it and fail every waiting request (event loop only)."""
        if self._serial is not ser:
            return
        self._serial = None
        self.last_error = reason
        self.brain_ok = False
        self.brain_error = "serial link down"
        log.error("serial: lost %s: %s", self.port, reason)
        try:
            ser.close()
        except Exception:  # noqa: BLE001
            pass
        error = LinkError("serial", "serial_lost", f"serial link to the brain was lost ({reason}). "
                          "Check the USB cable between the Pi and the brain.")
        for future, _, _ in self._pending.values():
            if not future.done():
                future.set_exception(error)

    # --- receive ----------------------------------------------------------------

    def _on_line(self, text: str) -> None:
        text = text.rstrip("\r")
        if not text:
            return
        self.counters["rx"] += 1
        try:
            msg = parse_line(text)
        except ProtocolError as e:
            self.counters["bad_lines"] += 1
            log.warning("brain: ignored corrupt line: %s", e)
            return
        if msg is None:
            self.console.append(f"{time.strftime('%H:%M:%S')} {text}")
            log.info("brain console: %s", text)
            return

        self._last_rx = time.monotonic()
        if msg.kind == "H":
            self.last_heartbeat = msg.fields
            self._last_heartbeat_at = self._last_rx
            return
        if msg.kind == "E":
            self.events.append({"time": time.strftime("%H:%M:%S"), "event": msg.code, **msg.fields})
            log.warning("brain event: %s %s", msg.code, msg.fields)
            return

        if msg.kind == "P":
            # More fields of a long reply; the '@D' that follows completes it.
            if msg.seq in self._pending:
                self._partials.setdefault(msg.seq, []).append(msg.text)
            return
        if msg.kind == "D" and msg.seq in self._partials:
            msg.text = ";".join(self._partials.pop(msg.seq) + ([msg.text] if msg.text else []))
            msg.fields = parse_fields(msg.text)

        entry = self._pending.get(msg.seq)
        if entry is None:
            if msg.seq in self._fire_and_forget:
                self._fire_and_forget.discard(msg.seq)
            else:
                self.events.append({"time": time.strftime("%H:%M:%S"), "event": "unsolicited", "line": msg.raw})
                log.warning("brain: reply for unknown seq: %s", msg.raw)
            return
        future, acked, verb = entry
        if verb not in QUIET_VERBS:
            log.info("<- %s", msg.raw)
        if msg.kind == "A":
            acked.set()
        elif msg.kind == "D" and not future.done():
            future.set_result(msg)

    # --- send -------------------------------------------------------------------

    def _write(self, data: bytes) -> None:
        ser = self._serial
        if ser is None:
            raise LinkError("serial", "serial_down",
                            f"the brain's serial port is not open ({self.last_error}).")
        try:
            ser.write(data)
        except Exception as e:  # noqa: BLE001
            self._drop(ser, f"serial write failed: {e}")
            raise LinkError("serial", "serial_lost", f"could not write to the brain ({e}).") from None
        self.counters["tx"] += 1

    def _next_seq(self) -> int:
        self._seq = self._seq % 65535 + 1
        return self._seq

    def send_stop_nowait(self) -> None:
        """Best-effort STOP that does not wait for an answer."""
        seq = self._next_seq()
        self._fire_and_forget.add(seq)
        try:
            self._write(encode_command(seq, "STOP"))
            log.warning("-> STOP (best effort, seq %d)", seq)
        except LinkError:
            self._fire_and_forget.discard(seq)

    async def request(self, verb: str, *args, timeout: float = 2.0, motion: bool = False) -> Message:
        """Sends one command and returns the brain's final '@D' reply.

        Immediate commands must be answered within `reply_timeout`. Motion
        commands must be acknowledged within it, then finish within `timeout`
        while the brain keeps sending data (heartbeats/ping replies).
        """
        seq = self._next_seq()
        future = self._loop.create_future()
        acked = asyncio.Event()
        self._pending[seq] = (future, acked, verb)
        data = encode_command(seq, verb, *args)
        try:
            self._write(data)
            if verb not in QUIET_VERBS:
                log.info("-> %s", data.decode().strip())
            start = time.monotonic()
            while True:
                if future.done():
                    return future.result()  # raises LinkError if the port dropped
                now = time.monotonic()
                if not acked.is_set() and now - start > self.reply_timeout:
                    raise LinkError("brain", "no_reply",
                                    f"the brain did not answer {verb} within {int(self.reply_timeout * 1000)} ms. "
                                    "Is the robot program running (it must call aon::pi::start) and is the Pi "
                                    "plugged into the brain's USB port?")
                if motion and acked.is_set() and now - self._last_rx > self.heartbeat_timeout:
                    silent = int((now - self._last_rx) * 1000)
                    self.send_stop_nowait()
                    raise LinkError("brain", "brain_silent",
                                    f"no data from the brain for {silent} ms during {verb}; treating it as aborted. "
                                    "The robot program may have crashed or frozen; it stops Pi motion by itself "
                                    "when it stops hearing from the Pi.")
                if now - start > timeout:
                    self.send_stop_nowait()
                    raise LinkError("brain", "timeout", f"{verb} did not finish within {timeout:.0f} s; sent STOP.")
                await asyncio.wait([future], timeout=0.05)
        finally:
            self._pending.pop(seq, None)
            self._partials.pop(seq, None)

    # --- health -----------------------------------------------------------------

    async def _keepalive(self) -> None:
        while not self._closing:
            await asyncio.sleep(self.ping_interval)
            if self._serial is None:
                continue
            start = time.monotonic()
            try:
                msg = await self.request("PING", timeout=1.0)
            except LinkError as e:
                if self.brain_ok or self.brain_error != e.message:
                    log.error("brain: %s", e.message)
                self.brain_ok, self.brain_error = False, e.message
                continue
            self.rtt_ms = round((time.monotonic() - start) * 1000, 1)
            self.brain_info = msg.fields
            proto = msg.fields.get("proto")
            if proto != PROTOCOL_VERSION:
                error = (f"protocol mismatch: brain speaks v{proto}, this server v{PROTOCOL_VERSION}; "
                         "update Override or the Pi so they match")
                if self.brain_error != error:
                    log.error("brain: %s", error)
                self.brain_ok, self.brain_error = False, error
                continue
            if not self.brain_ok:
                log.info("brain: answering (robot=%s, rtt %.0f ms)", msg.fields.get("robot"), self.rtt_ms)
            self.brain_ok, self.brain_error = True, None

    def max_rpm(self) -> float:
        value = self.brain_info.get("max_rpm")
        return float(value) if isinstance(value, (int, float)) and value > 0 else 600.0

    def health(self) -> dict:
        now = time.monotonic()
        heartbeat_age = int((now - self._last_heartbeat_at) * 1000) if self._last_heartbeat_at else None
        return {
            "serial": {"ok": self.connected, "port": self.port if self.connected else None,
                       "error": None if self.connected else self.last_error, **self.counters},
            "brain": {"ok": self.connected and self.brain_ok,
                      "error": None if self.connected and self.brain_ok else (self.brain_error if self.connected else "serial link down"),
                      "robot": self.brain_info.get("robot"), "proto": self.brain_info.get("proto"),
                      "rtt_ms": self.rtt_ms, "heartbeat_age_ms": heartbeat_age,
                      "mode": self.last_heartbeat.get("mode"), "pi_control": bool(self.last_heartbeat.get("pi")),
                      "busy": self.last_heartbeat.get("busy")},
        }
