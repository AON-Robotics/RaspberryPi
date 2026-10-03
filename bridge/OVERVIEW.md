# VEX Robot LLM Bridge: Team Overview

*Status as of 2026-10-02. For setup commands, see [README.md](README.md).*

## What it is

A way to **debug the VEX robot by chatting with it in plain language**
("drive forward 10 inches, then turn left 90°, where are you?"). A local AI
model (an LLM) turns the request into robot commands. Teammates use it from
a web page, so they don't need to install anything except Tailscale.

The LLM never drives the robot directly. It can only ask for a small set of
tools: `move`, `turn`, `stop`, `status`, `read_sensors`, `reset_odometry`,
`odometry_test` and `diagnose`. The robot server checks every request and
enforces the speed and distance limits. The brain runs each command with
Override's own `drivetrain.move()`/`turn()` and odometry.

```
Teammate's browser ──Tailscale + team password──▶ Gaming laptop
                                                   ├─ Web chat app (Docker)
                                                   ├─ Ollama + qwen3:8b (GPU)
                                                   └──Tailscale + token──▶ Robot server (Raspberry Pi)
                                                                            └──USB serial──▶ V5 Brain (Override, src/aon/pi/)
```

## Current status

| Part | Status |
| --- | --- |
| Robot server (mock, fake pose) on the Mac | ✅ Works |
| LLM on the laptop (Ollama + qwen3:8b) | ✅ Works, roughly 2 s for short replies, about 20 s for a multi-step command |
| Terminal chat (`agent.py`) | ✅ Correct tool order: drive → turn → status |
| Web chat + STOP button | ✅ Worked on the same Wi-Fi (first test) |
| Security hardening + health checks | ✅ 59/59 local tests pass. Real chat + STOP work with the GPU, and STOP interrupts a motion mid-drive. |
| Web chat in Docker | ❌ Blocked: virtualization is off in the laptop BIOS and WSL2 isn't installed. Runs natively for now. |
| Over Tailscale (laptop ↔ Mac, teammates) | ⏳ To test |
| Real robot tools over serial (server ⇄ Override `src/aon/pi/`) | ✅ Written. Full chain tested on a laptop against `sim/brain_sim` (Override's real protocol code + a simulated drivetrain). ⏳ Needs `pros make` and a bench test on the robot. |
| Brain stays in charge if the Pi fails | ✅ Deadman (1 s), joystick/X takeover; tested with cable pulled, brain frozen, Pi server killed, laptop gone |

## Technologies used

| Technology | What it is | How we use it | Limitations |
| --- | --- | --- | --- |
| **Ollama** | Runs AI models locally, no cloud | Serves the LLM at `localhost:11434` on the laptop | Runs natively (not in Docker) for direct GPU use. Only one laptop serves everyone. |
| **qwen3:8b** | 8-billion-parameter open model with tool calling | Chooses which robot tools to call | About 5 GB, and the RTX 3050 has only 4 GB, so it runs about 40% on the GPU and 60% on the CPU. hermes3 was tested and was worse. |
| **Python 3.12** | Language for all the code | Server, web app, agent loop | Not real-time. That's fine, because the LLM is never in the control loop. |
| **FastAPI** | Python web framework | Turns Python functions into HTTP endpoints (robot server + web app) | None that matter at this scale |
| **Uvicorn** | Python web server | Runs the FastAPI apps on a port | One process each |
| **Pydantic** | Data validation | Checks the JSON the browser sends (lengths, formats) | None that matter here |
| **Requests** | HTTP client library | Laptop → Ollama and laptop → robot server calls | Blocking calls (handled with threads) |
| **Docker + Compose** | Packages an app with everything it needs | Runs the web chat on the laptop with locked-down settings | Needs CPU virtualization enabled in the BIOS (AMD-V/SVM) + WSL2. Both are off/missing on the laptop today. Uses about 2 GB of RAM while it runs. Ollama stays outside Docker so it uses the NVIDIA GPU directly. |
| **Tailscale** | Private encrypted network (VPN) between our devices | Connects teammates → laptop → Mac/Pi from anywhere | Everyone needs the Tailscale app and an invite to the tailnet |
| **Git / GitHub** | Version control | Branch `agent-implementation` in `AON-Robotics/RaspberryPi` | Secrets are never committed (`.env` is git-ignored) |

## Key files

| File | Role |
| --- | --- |
| `server/server.py` | Robot server HTTP layer: tokens, limits, health, tool dispatch (Pi) |
| `server/tools/` | The tools, one module per area (motion, status, sensors, odometry, diagnostics) |
| `server/brain_link.py`, `server/protocol.py` | Serial link to the brain: framing, replies, keepalive, reconnect |
| `../sim/` | Laptop brain simulator and end-to-end tests; `../run_tests.sh` runs everything |
| `server/stop.html` | Backup emergency STOP page, served by the robot server |
| `agent/loop.py` | The "agent loop": LLM ↔ tools, plus `check_pipeline()` health checks |
| `agent/agent.py` | Terminal chat |
| `web/app.py` + `web/static/` | Web chat: login, chat, STOP, status lights |
| `compose.yml`, `web/Dockerfile` | Docker setup for the web chat |
| `.env` (not in git) | Secrets: `BRIDGE_TOKEN`, `TEAM_PASSWORD`, `STOP_TOKEN` |

## Security, in brief

- **Three separate secrets.**
  - `TEAM_PASSWORD` opens the web chat.
  - `BRIDGE_TOKEN` lets the laptop control the robot; it never reaches a browser.
  - `STOP_TOKEN` can *only* stop the robot (for the phone STOP page).
- **Tailscale-only access.** Nothing is reachable from the public Wi-Fi, and all traffic is encrypted.
- **Server-side checks.** Speed and distance limits, numbers-only arguments, request size limits, rate limits on wrong passwords and tokens.
- **STOP always works.** It's never rate-limited, it doesn't need the LLM, and it has a backup page on the robot server.
- **Audit log.** Every login, tool call and STOP is recorded with a timestamp, and secrets are never logged.

## Health checks

Every step of the pipeline is checked in order: **Ollama → model → robot server → serial → brain → token → tools**.
- The web page shows one status light per step. Send is disabled while a light is red; STOP never is.
- The terminal chat prints the checklist and won't start if a step fails.
- If something is down, the error message names the step and how to fix it.

## How to use it (teammates)

1. Install Tailscale and accept the team invite.
2. Open `http://<laptop-tailscale-ip>:8080` and enter the team password. Ask the operator for both.
3. Check that all the lights are green, then type a request.
4. **STOP button or Esc** stops the robot immediately.

## Limitations

- **Debugging only:** not for driving matches or anything time-critical.
- **Not yet on the real robot:** the brain code is checked against the PROS headers and run in the simulator, but not yet compiled with `pros make` or run on hardware.
- **One laptop:** if the gaming laptop is off or busy, nobody can chat. Gaming and the LLM compete for the GPU.
- **Docker not running yet:** until the BIOS + WSL2 setup is done, the web chat runs natively (same code, same security, minus the container isolation).
- **Slow on CPU spill:** multi-step commands take about 20 s. A smaller model (`qwen3:4b`) would fit on the GPU, but it hasn't been tested for tool-calling quality.
- **Memory:** conversations are lost when the app restarts.
- **Laptop's own security still has gaps.** These are on the morning list: Windows Firewall, and Ollama is still exposed through `OLLAMA_HOST`.

## Next steps

1. **Morning:** finish the laptop security settings and test over Tailscale (laptop ↔ Mac, then a teammate's device), using the native web app.
   Docker comes after: enable virtualization in the BIOS, install WSL2 (`wsl --install`, then reboot), then `docker compose up -d --build`.
2. Push the branch, and update the Mac with the new code and tokens.
3. Move the robot server from the Mac to the **Raspberry Pi** (same code, `--host <pi-tailscale-ip>`).
4. ~~Replace the mock tools with real serial commands to the V5 Brain.~~ Done; next is the bench test (`pros make`, upload, `diagnose`, a short `move`).
5. Add tools and sensors as needed: register a sensor on the brain and it shows up in `read_sensors`. The LLM picks new tools up automatically from `/tools`.
6. Optional: per-user login via Tailscale identity, so the audit log shows *who* sent each command.
