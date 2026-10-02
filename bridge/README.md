# LLM debugging bridge

Talk to the robot in plain language, for debugging only. The LLM is never in
the real-time loop: every tool finishes on its own and the server enforces the
safety limits.

```
Laptop: Ollama (qwen3:8b) + web/app.py or agent/agent.py (both use agent/loop.py)
   --HTTP + token-->  Mac/Pi: server/server.py  --serial-->  V5 Brain
```

The server tools are **mock** for now (fake pose, no serial).

## 1. Make a token (once)

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Both machines use the same value as `BRIDGE_TOKEN`. Don't commit it.

## 2. Server (Mac, later the Pi)

```bash
cd bridge
python3 -m venv .venv && .venv/bin/pip install -r server/requirements.txt
cd server
BRIDGE_TOKEN=<token> ../.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
```

Check it: `curl http://localhost:8000/health` (no token needed).

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

## Endpoints

| Method | Path | Auth | Does |
| --- | --- | --- | --- |
| GET | `/health` | no | Link check |
| GET | `/tools` | yes | Tool schemas in Ollama format |
| POST | `/tools/{name}` | yes | Run a tool; JSON body is its arguments |

Limits live at the top of `server.py`: 50% speed, 48 in per drive, 360° per
turn, 5 s per command. Values over a limit are clamped and reported as
`"clamped": true`.
