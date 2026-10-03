#!/usr/bin/env bash
# Runs the whole LLM bridge on this laptop, with brain_sim standing in for the
# V5 brain and this machine standing in for the Pi:
#
#   you -> agent.py -> Ollama -> agent.py -> server.py --pty--> brain_sim
#
# Usage (from anywhere):
#   sim/run_pipeline.sh                       # healthy robot, terminal chat
#   sim/run_pipeline.sh --fault reversed_motor:13,reversed_left_tracking
#   sim/run_pipeline.sh --mode disabled
#   WEB=1 sim/run_pipeline.sh                 # web chat on http://localhost:8080 instead
#
# Logs: sim/build/run/{sim,server}.log. Ctrl+C stops everything.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/sim/build"
RUN="$BUILD/run"
PY="$ROOT/bridge/.venv/bin/python"
mkdir -p "$RUN"

if [[ ! -x "$PY" ]]; then
  python3 -m venv "$ROOT/bridge/.venv"
  "$PY" -m pip install -q -r "$ROOT/bridge/server/requirements.txt" -r "$ROOT/bridge/web/requirements.txt" pytest
fi
cmake -S "$ROOT/sim" -B "$BUILD" >/dev/null
cmake --build "$BUILD" >/dev/null

# The server refuses tokens under 32 characters; the web chat needs a password.
export BRIDGE_TOKEN="${BRIDGE_TOKEN:-local-sim-token-0123456789abcdefghijkl}"
export TEAM_PASSWORD="${TEAM_PASSWORD:-local-sim-password}"
export BRAIN_PORT="$RUN/brain.tty"
export BRIDGE_URL="http://127.0.0.1:8000"

"$BUILD/brain_sim" --link "$BRAIN_PORT" --noise "$@" >"$RUN/sim.log" 2>&1 &
SIM_PID=$!
(cd "$ROOT/bridge/server" && exec "$PY" -m uvicorn server:app --host 127.0.0.1 --port 8000) >"$RUN/server.log" 2>&1 &
SERVER_PID=$!
trap 'kill $SIM_PID $SERVER_PID ${WEB_PID:-} 2>/dev/null || true' EXIT

for _ in $(seq 50); do
  curl -fs "$BRIDGE_URL/health" | grep -q '"ok":true' && break
  sleep 0.2
done
echo "health: $(curl -s -H "Authorization: Bearer $BRIDGE_TOKEN" "$BRIDGE_URL/health/details")"
echo "logs:   $RUN/sim.log  $RUN/server.log"
echo "STOP page: $BRIDGE_URL/stop (token: $BRIDGE_TOKEN)"

if [[ "${WEB:-}" == "1" ]]; then
  (cd "$ROOT/bridge/web" && exec "$PY" -m uvicorn app:app --host 127.0.0.1 --port 8080) &
  WEB_PID=$!
  echo "web chat: http://localhost:8080  password: $TEAM_PASSWORD  (Ctrl+C to stop)"
  wait $WEB_PID
else
  cd "$ROOT/bridge/agent" && "$PY" agent.py
fi
