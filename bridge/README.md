# LLM debugging bridge

Talk to the robot in plain language, for debugging only. The LLM is never in
the real-time loop: every tool finishes on its own and the server enforces the
safety limits.

```
Laptop: Ollama (qwen3:8b) + web/app.py or agent/agent.py (both use agent/loop.py)
   --HTTP + token-->  Pi: server/server.py  --USB serial-->  V5 brain (Override, src/aon/pi/)
```

The tools run Override's own `drivetrain.move()` / `drivetrain.turn()` and read
its odometry and sensors on the brain. The wire format is in
[../docs/serial-protocol.md](../docs/serial-protocol.md).

No Pi or brain at hand? [`../sim/`](#try-it-without-the-robot) runs the whole
chain on a laptop against a simulated brain.

## The Pi is an add-on

If anything between the LLM and the brain breaks, the robot program keeps
running normally. Any motion the Pi started is aborted, and every layer says
which hop failed:

| Hop | What breaks | You see |
| --- | --- | --- |
| `llm` | Ollama down, model missing | "Stopped: the LLM (Ollama) failed. ..." |
| `server` | Pi off, server down, wrong token, laptop Wi-Fi drop | "Stopped: the robot server on the Pi failed. ..." |
| `serial` | USB cable pulled, brain off | "Stopped: the USB serial link between the Pi and the brain failed. ..." |
| `brain` | robot program not running, crashed or frozen | "Stopped: the robot program on the brain failed. ..." |

- **Brain:** aborts Pi motion after 1 s without hearing from the Pi, when a
  joystick moves, or when X is pressed. It never lets the Pi move the robot
  while disabled or in autonomous.
- **Server:** sends STOP when the laptop disconnects mid-motion, or when the
  brain goes silent.
- **Agent:** ends the turn on the first link failure instead of letting the
  LLM retry.
- **Health:** `GET /health` on the server, the hop line at the top of the web
  page, and the check `agent.py` prints at start all report every hop.

## 1. Make a token (once)

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Both machines use the same value as `BRIDGE_TOKEN`. Don't commit it.

## 2. Server (the Pi)

```bash
cd bridge
python3 -m venv .venv && .venv/bin/pip install -r server/requirements.txt
cd server
BRIDGE_TOKEN=<token> ../.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
```

- **Port:** the server finds the brain's USB *user* port by itself (see
  [serial-protocol.md](../docs/serial-protocol.md)). Set `BRAIN_PORT=/dev/...`
  to pick it yourself.
- **Permissions:** your user needs the `dialout` group.
- **Brain program:** the robot must run Override with `aon::pi::start(...)` in
  `initialize()`.

Check it: `curl http://localhost:8000/health` (no token needed). `ok` is true
only when the serial port is open and the brain program answers.

**Emergency STOP:** open `http://<server>:8000/stop` in any browser (a phone
works). Enter the token once; after that the big button (or Space/Esc) calls
`stop()` directly. The LLM and the laptop are not involved, so it works even
if they are down.

## 3. Laptop: Ollama

Install Ollama natively (not in Docker, so it can use the GPU) and run
`ollama pull qwen3:8b`. hermes3 was tested and kept inventing tool arguments.
While it answers, `ollama ps` should show GPU under PROCESSOR.

## 4a. Laptop: web chat (for the team)

The agent loop (`agent/loop.py`) plus a chat page, in Docker:

```bash
cd bridge
cp .env.example .env      # then fill in BRIDGE_URL and BRIDGE_TOKEN
docker compose up -d --build
```

Teammates open `http://<laptop-tailscale-name>:8080`. The page has a STOP
button (Esc also works) that goes straight to the robot, and a link to the
robot server's backup STOP page. The token stays on the laptop; the browser
never sees it.

Without Docker: `pip install -r web/requirements.txt`, then from `web/` run
`BRIDGE_URL=... BRIDGE_TOKEN=... uvicorn app:app --host 0.0.0.0 --port 8080`.

## 4b. Laptop: terminal chat

Same loop, no browser:

```bash
cd bridge/agent
pip install -r requirements.txt
BRIDGE_URL=http://<mac-tailscale-name>:8000 BRIDGE_TOKEN=<token> python agent.py
```

Ctrl+C at any time sends `stop()` to the robot.

## Tools

| Tool | Does |
| --- | --- |
| `move(distance_in, speed_pct)` | Drive straight with Override's motion profile. Returns the start and end pose, distance traveled, tracking wheels and IMU. |
| `turn(degrees, speed_pct)` | Turn in place. **Positive is clockwise (right).** |
| `stop()` | Abort the Pi's motion. Never touches driver control. |
| `status()` | Pose, mode, battery, moving, and the health of every hop. |
| `read_sensors(name)` | Raw values of every sensor registered on the brain. |
| `reset_odometry(x_in, y_in, heading_deg)` | Set the odometry pose (re-tares the IMU). |
| `odometry_test(pattern, distance_in, speed_pct)` | Run `straight`, `turn` or `square` and check tracking wheel directions, left/right agreement, heading hold, turn accuracy and closure. Gives a pass/warn/fail verdict with the fix for each problem. |
| `diagnose(active)` | Check the link, battery, mode, every drive motor, the tracking wheels, the IMU and odometry. `active=true` also spins each side for 0.5 s to catch reversed motors or encoders and swapped ports. |

- **Limits** live in `server/tools/motion.py`: 50% speed, 48 in per move,
  360° per turn. Values over a limit are clamped and reported as
  `"clamped": true`.
- **Timeouts** come from the brain's own: distance/3 s for a move,
  sqrt(degrees/2) s for a turn, plus a margin. The brain enforces looser
  limits of its own (72 in, 720°).

### Adding a sensor or a tool

- **Sensor on the brain** (distance, optical, ...): add a
  `link.registerSensor("name", ...)` call in `registerSensors()` in
  `Override/src/aon/pi/pi-link.cpp`; there is a commented example. It shows up
  in `read_sensors` right away, with no change on the Pi.
- **New brain command:** register it in `Override/src/aon/pi/commands.cpp`
  (`link.registerCommand`) and mirror it in `sim/brain_sim.cpp` if it needs
  the simulated `Robot`.
- **New tool:** write an `async` function with `@tool(...)` in a module under
  `server/tools/` and import it in `server/tools/__init__.py`. The agent picks
  it up from `GET /tools`.
- **Sensor on the Pi** (OTOS, OAK-D): see `server/tools/pi_sensors.py`.

## Endpoints

| Method | Path | Auth | Does |
| --- | --- | --- | --- |
| GET | `/health` | no | Health of every hop: server, serial, brain |
| GET | `/stop` | no | Emergency STOP page (the button itself needs the token) |
| GET | `/tools` | yes | Tool schemas in Ollama format |
| POST | `/tools/{name}` | yes | Run a tool; JSON body is its arguments |

## Try it without the robot

`../sim/brain_sim` stands in for the brain. It runs Override's real protocol
code (`Override/src/aon/pi/`) with a simulated drivetrain, on a
pseudo-terminal the server opens like the real USB port. This needs the
Override repo next to this one (or pass `-DOVERRIDE_DIR=...` to CMake).

```bash
sim/run_pipeline.sh                                    # terminal chat with the local Ollama
sim/run_pipeline.sh --fault reversed_motor:13          # a robot with a wiring fault
WEB=1 sim/run_pipeline.sh                              # web chat on :8080
```

Tests (from the repo root):

```bash
bridge/.venv/bin/python -m pytest bridge/tests          # protocol, analysis, agent abort logic
bridge/.venv/bin/python -m pytest sim/test_pipeline.py  # whole chain, plus fault injection
RUN_OLLAMA=1 bridge/.venv/bin/python -m pytest sim/test_pipeline.py -k real_llm -s
```
