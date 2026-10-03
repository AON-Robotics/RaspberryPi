# LLM debugging bridge

Talk to the robot in plain language, for debugging only. The LLM is never in
the real-time loop: every tool finishes on its own and the server enforces the
safety limits.

```
Teammate's browser --Tailscale + team password--> Laptop: web/app.py (Docker) or agent/agent.py
                                                     |  agent/loop.py
                                                     +--> Ollama (native, GPU)
                                                     +--Tailscale + token--> Pi: server/server.py
                                                                               --USB serial--> V5 brain
                                                                                  (Override, src/aon/pi/)
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

## Security model

Every hop is locked separately, so one leak doesn't open the robot:

| Hop | Who can reach it | Secret |
| --- | --- | --- |
| Browser → web chat (laptop :8080) | Tailscale only (port published on the laptop's Tailscale IP) | `TEAM_PASSWORD` → HttpOnly, SameSite=Strict cookie, 12 h |
| Web chat → Ollama (laptop :11434) | This laptop only (`OLLAMA_HOST=127.0.0.1`) | none needed |
| Laptop → robot server (:8000) | Tailscale only (`--host <tailscale-ip>`) | `BRIDGE_TOKEN` (never leaves the laptop and the server) |
| Phone → STOP page (:8000/stop) | Tailscale only | `STOP_TOKEN`: can call `stop()` and nothing else |

Plus, on both servers: rate limits on wrong passwords/tokens (STOP is never
rate-limited), request size limits, strict argument checks (numbers only, no
NaN), no `/docs`, a strict Content-Security-Policy and other security headers,
the LLM can only call tools the server listed, and a JSON audit log line per
login, tool call and STOP (secrets are never logged). Tailscale encrypts all
traffic, so tokens never cross the Wi-Fi in clear text.

## Health checks

`check_pipeline()` in `agent/loop.py` checks every hop in order: Ollama up →
model pulled → robot server up → token accepted → tools present. It is used:

- by `agent.py` at startup (prints a checklist, refuses to start on a failure);
- by the web page's status lights (refreshed every 10 s; Send is disabled while
  a light is red, STOP never is);
- before every chat message (the reply says which hop is down);
- `GET /api/health/live` on the web app is Docker's health check.

## 1. Make the secrets (once)

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Run it once per secret: `BRIDGE_TOKEN` and `STOP_TOKEN` (32), and
`TEAM_PASSWORD` (`token_urlsafe(16)` is enough). Keep them in `.env` files
(git-ignored), never in chat or commits. If one leaks, make a new one.

## 2. Server (the Pi)

```bash
cd bridge
python3 -m venv .venv && .venv/bin/pip install -r server/requirements.txt
cd server
BRIDGE_TOKEN=<token> STOP_TOKEN=<stop-token> ../.venv/bin/uvicorn server:app \
  --host "$(tailscale ip -4)" --port 8000 --no-server-header
```

Binding to the Tailscale IP means only the tailnet can reach it.

- **Port:** the server finds the brain's USB *user* port by itself (see
  [serial-protocol.md](../docs/serial-protocol.md)). Set `BRAIN_PORT=/dev/...`
  to pick it yourself.
- **Permissions:** your user needs the `dialout` group.
- **Brain program:** the robot must run Override with `aon::pi::start(...)` in
  `initialize()`.

Check it with `curl http://<server-tailscale-ip>:8000/health` (no token
needed). It returns only ok/not-ok per hop (`server`, `serial`, `brain`).
`ok` is true only when the serial port is open and the brain program answers.
The reasons are in `/health/details`, which needs `BRIDGE_TOKEN`.

**Emergency STOP:** open `http://<server>:8000/stop` in any browser (a phone
on Tailscale works). Enter the `STOP_TOKEN` once; after that the big button
(or Space/Esc) calls `stop()` directly. The LLM and the laptop are not
involved, so it works even if they are down.

## 3. Laptop: Ollama

Install Ollama natively (not in Docker, so it can use the GPU) and run
`ollama pull qwen3:8b`. hermes3 was tested and kept inventing tool arguments.
While it answers, `ollama ps` shows how much runs on the GPU (the RTX 3050's
4 GB holds about 40% of qwen3:8b; the rest runs on the CPU).

Keep Ollama on this machine only: the user environment variable `OLLAMA_HOST`
must be unset or `127.0.0.1:11434`, never `0.0.0.0`.

## 4a. Laptop: web chat (for the team)

The agent loop (`agent/loop.py`) plus a chat page, in Docker:

```bash
cd bridge
cp .env.example .env      # then fill it in (see the comments inside)
docker compose up -d --build
docker compose ps         # STATUS should say (healthy)
docker compose logs -f    # audit log
```

Teammates (on your tailnet) open `http://<laptop-tailscale-ip>:8080` and type
the team password. The page shows the pipeline lights, a STOP button (Esc also
works) that goes straight to the robot, and a link to the robot server's
backup STOP page. The robot token stays on the laptop; the browser never
sees it.

The container runs as a non-root user with a read-only filesystem, no Linux
capabilities, and memory/CPU/process limits.

Without Docker (PowerShell, from `web/`, with the same values as `.env`):

```powershell
pip install -r requirements.txt
$env:BRIDGE_URL="..."; $env:BRIDGE_TOKEN="..."; $env:TEAM_PASSWORD="..."
uvicorn app:app --host 127.0.0.1 --port 8080 --no-server-header
```

Use `--host <laptop-tailscale-ip>` instead of `127.0.0.1` for teammates. Never
`0.0.0.0`: that also opens it to the Wi-Fi.

## 4b. Laptop: terminal chat

Same loop, no browser:

```bash
cd bridge/agent
pip install -r requirements.txt
BRIDGE_URL=http://<server-tailscale-ip>:8000 BRIDGE_TOKEN=<token> python agent.py
```

It prints the pipeline checklist first. Ctrl+C at any time sends `stop()` to
the robot.

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

Robot server:

| Method | Path | Auth | Does |
| --- | --- | --- | --- |
| GET | `/health` | no | ok/not-ok of each hop: server, serial, brain |
| GET | `/health/details` | `BRIDGE_TOKEN` | Same, plus port, round trip, mode and why a hop is down |
| GET | `/stop` | no (button needs a token) | Emergency STOP page |
| GET | `/tools` | `BRIDGE_TOKEN` | Tool schemas in Ollama format |
| POST | `/tools/stop` | `BRIDGE_TOKEN` or `STOP_TOKEN` | Stop now |
| POST | `/tools/{name}` | `BRIDGE_TOKEN` | Run a tool; JSON body is its arguments |

Server limits: 4 KB per request, 10 wrong tokens per minute per IP (never for
STOP), and arguments must be plain JSON numbers/booleans (`"10"` is refused
with a hint). Motion limits are in `server/tools/motion.py`.

Web chat: `/login`, `/api/login`, `/api/logout`, `/api/health`,
`/api/health/live` (no login), `/api/config`, `/api/chat`, `/api/stop`,
`/api/reset`. Limits at the top of `app.py`. The status lights show every
step of `loop.check_pipeline()`: ollama, model, robot, serial, brain, token,
tools. Send is disabled while one is red; STOP never is.

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

To run every test, use `./run_tests.sh` at the repo root (see the top-level
README).
