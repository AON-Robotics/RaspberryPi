# LLM debugging bridge

Talk to the robot in plain language, for debugging only. The LLM is never in
the real-time loop: every tool finishes on its own and the server enforces the
safety limits.

```
Teammate's browser --Tailscale + team password--> Laptop: web/app.py (Docker)
                                                     |  agent/loop.py
                                                     +--> Ollama (native, GPU)
                                                     +--Tailscale + token--> Mac/Pi: server/server.py --serial--> V5 Brain
```

The server tools are **mock** for now (fake pose, no serial).

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

## 2. Server (Mac, later the Pi)

```bash
cd bridge
python3 -m venv .venv && .venv/bin/pip install -r server/requirements.txt
cd server
BRIDGE_TOKEN=<token> STOP_TOKEN=<stop-token> ../.venv/bin/uvicorn server:app \
  --host "$(tailscale ip -4)" --port 8000 --no-server-header
```

Binding to the Tailscale IP means only the tailnet can reach it. Check it:
`curl http://<server-tailscale-ip>:8000/health` → `{"ok":true,"robot":"mock"}`.

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

## Endpoints

Robot server:

| Method | Path | Auth | Does |
| --- | --- | --- | --- |
| GET | `/health` | no | Link check |
| GET | `/stop` | no (button needs a token) | Emergency STOP page |
| GET | `/tools` | `BRIDGE_TOKEN` | Tool schemas in Ollama format |
| POST | `/tools/stop` | `BRIDGE_TOKEN` or `STOP_TOKEN` | Stop now |
| POST | `/tools/{name}` | `BRIDGE_TOKEN` | Run a tool; JSON body is its arguments |

Limits live at the top of `server.py`: 50% speed, 48 in per drive, 360° per
turn, 5 s per command, 4 KB per request, 10 wrong tokens per minute per IP.
Values over a motion limit are clamped and reported as `"clamped": true`.

Web chat: `/login`, `/api/login`, `/api/logout`, `/api/health`,
`/api/health/live` (no login), `/api/config`, `/api/chat`, `/api/stop`,
`/api/reset`. Limits at the top of `app.py`.
