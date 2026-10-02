"""Web chat for the robot. Runs on the gaming laptop, next to Ollama.

The browser only talks to this app. This app runs the agent loop (loop.py),
talks to Ollama, and holds the robot server's token, so the token never
reaches the browser.

    Browser --> this app --> Ollama
                         +-> robot server (Pi / Mac mock)

Run (same env vars as agent.py, see agent/loop.py):
    BRIDGE_URL=http://mac-name:8000 BRIDGE_TOKEN=... uvicorn app:app --host 0.0.0.0 --port 8080
"""

import sys
import threading
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

# loop.py lives in ../agent; make it importable from here.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "agent"))
import loop  # noqa: E402

app = FastAPI(title="Robot chat")

# --- Conversations ----------------------------------------------------------
# One conversation per browser tab, kept in memory: restarting the app clears
# them all, which is fine for debugging. Each has a lock so a tab can't run
# two requests at once and interleave messages in the history.

sessions: dict[str, list] = {}
locks: dict[str, threading.Lock] = {}
sessions_guard = threading.Lock()  # protects the two dicts above


def get_session(session_id: str) -> tuple[list, threading.Lock]:
    with sessions_guard:
        if session_id not in sessions:
            sessions[session_id] = loop.new_conversation()
            locks[session_id] = threading.Lock()
        return sessions[session_id], locks[session_id]


# --- Request bodies ---------------------------------------------------------
# pydantic models describe the JSON the page sends; FastAPI checks it for us.

class ChatRequest(BaseModel):
    session_id: str
    message: str


class SessionRequest(BaseModel):
    session_id: str


# --- Endpoints --------------------------------------------------------------
# These are plain `def` (not async) on purpose: loop.py uses blocking HTTP
# calls, and FastAPI runs plain functions in a thread pool, so one slow chat
# doesn't freeze the STOP button for everyone else.

@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "index.html")


@app.get("/api/health")
def health() -> dict:
    """Every hop, for the status line on the page: LLM, server, serial, brain."""
    return {"llm": loop.check_llm(), "robot": loop.check_health()}


@app.get("/api/config")
def config() -> dict:
    """Non-secret settings the page displays."""
    return {"model": loop.MODEL, "stop_page": f"{loop.BRIDGE_URL}/stop"}


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    messages, lock = get_session(req.session_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "Still working on your previous message.")
    try:
        # Fetched every time so new tools (or a restarted server) just work.
        try:
            tools = loop.get_tools()
        except requests.RequestException as e:
            raise HTTPException(502, f"Stopped: the robot server on the Pi failed. Cannot reach {loop.BRIDGE_URL} ({e})")

        tool_calls = []
        messages.append({"role": "user", "content": req.message})
        # run_turn never raises for a broken hop; the reply says what failed.
        reply = loop.run_turn(messages, tools, on_tool=lambda name, args, result:
                              tool_calls.append({"name": name, "args": args, "result": result}))
        return {"reply": reply, "tool_calls": tool_calls}
    finally:
        lock.release()


@app.post("/api/stop")
def stop() -> dict:
    """STOP button: straight to the robot server, the LLM is not involved."""
    result = loop.call_tool("stop", {})
    if not result.get("ok"):
        raise HTTPException(502, result.get("error", "stop failed"))
    return result


@app.post("/api/reset")
def reset(req: SessionRequest) -> dict:
    """'New chat' button: forget this tab's conversation."""
    with sessions_guard:
        sessions.pop(req.session_id, None)
        locks.pop(req.session_id, None)
    return {"ok": True}
