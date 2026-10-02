"""The agent loop, shared by the terminal app (agent.py) and the web app.

What an "agent loop" is
-----------------------
The LLM cannot touch the robot. It can only *write text*, and one kind of text
it can write is a request like "please call drive with distance_in=10". This
module is the middleman that turns those requests into real actions:

    1. The user sends a request ("drive forward 10 inches").
    2. We send the whole conversation + the list of tools to Ollama.
    3. Ollama answers with EITHER:
         a) tool calls  -> we run each one against the robot server, add the
                           results to the conversation, and go back to step 2
                           so the model can see what happened; OR
         b) plain text  -> that is the final answer.

Analogy: the LLM is a manager on the phone who can only give instructions.
This code is the assistant who actually walks over to the robot, does what
was asked, and reads back the result, until the manager says "done".

Settings (environment variables)
--------------------------------
    BRIDGE_URL    robot server, e.g. http://mac-name:8000 (Tailscale name)
    BRIDGE_TOKEN  same secret token the server was started with
    OLLAMA_URL    where Ollama runs; default http://localhost:11434
    MODEL         which Ollama model to use; default qwen3:8b
"""

import json
import os
import sys

import requests  # small library for making HTTP requests

# --- Settings ---------------------------------------------------------------
# os.environ.get("NAME", default) reads an environment variable, so you can
# change these without editing the file. rstrip("/") removes a trailing slash
# so "http://x:8000/" + "/tools" doesn't become "//tools".

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://localhost:8000").rstrip("/")
# No default for the token on purpose: if it's missing, quit with a message.
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN") or sys.exit("BRIDGE_TOKEN is not set")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
# qwen3:8b tested far more reliable at tool calling than hermes3 (which kept
# inventing arguments). Any Ollama model with tool support works here.
MODEL = os.environ.get("MODEL", "qwen3:8b")

# Safety valve: a confused model could keep calling tools forever. After this
# many rounds for a single request, we give up and hand control back.
MAX_TOOL_ROUNDS = 8

# A tool can drive a whole test pattern; the server enforces its own, tighter
# per-command timeouts, this is only the outer bound.
TOOL_HTTP_TIMEOUT_S = 120

# What each "hop" in a failure means, for the messages the user reads.
HOPS = {
    "llm": "the LLM (Ollama)",
    "server": "the robot server on the Pi",
    "serial": "the USB serial link between the Pi and the brain",
    "brain": "the robot program on the brain",
}


def abort_message(result: dict) -> str:
    hop = result.get("hop", "server")
    return f"Stopped: {HOPS.get(hop, hop)} failed. {result.get('message') or result.get('error')}"


def check_health() -> dict:
    """Asks the robot server how every hop is doing (GET /health).

    Returns the server's report, or a failure dict with hop="server" when the
    server itself cannot be reached.
    """
    try:
        r = requests.get(f"{BRIDGE_URL}/health", timeout=3)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "hop": "server", "error": "server_unreachable",
                "message": f"cannot reach the robot server at {BRIDGE_URL} ({e}). Is the Pi on and the server running?"}


def check_llm() -> dict:
    """Is Ollama up and does it have the model?"""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        r.raise_for_status()
        names = {m.get("name") for m in r.json().get("models", [])}
        if MODEL not in names and f"{MODEL}:latest" not in names:
            return {"ok": False, "hop": "llm", "error": "model_missing",
                    "message": f"Ollama is up but has no model {MODEL!r}; run: ollama pull {MODEL}"}
        return {"ok": True}
    except requests.RequestException as e:
        return {"ok": False, "hop": "llm", "error": "llm_unreachable",
                "message": f"cannot reach Ollama at {OLLAMA_URL} ({e})"}

# Header sent with every request to the robot server. This is the token
# check: the server rejects anything without it (HTTP 401).
AUTH = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}

# The "system" message is standing instructions the model sees before any
# user message. It sets the model's role and habits.
SYSTEM_PROMPT = """\
You help debug a VEX robot. Use the tools to move it, test it, or read its state.

Rules:
- One motion per tool call. move() only drives straight; turn() only rotates.
  For "drive 10 inches then turn left", call move, wait for its result,
  then call turn.
- turn() degrees are CLOCKWISE positive: "turn right 90" is degrees=90,
  "turn left 90" is degrees=-90.
- Only pass the arguments a tool lists. Never invent arguments.
- If a tool returns an error with "hop": "server" and "error": "bad_args",
  fix the arguments and call again. For any other error, do not retry the
  same motion: tell the user the "message" in plain words.
- If a result says "clamped": true, tell the user what was actually done.
- Never compute or guess the robot's position or heading yourself. When the
  user asks where the robot is, call status() and report what it returns.
- To check the drivetrain or odometry, use diagnose() (active=true to also
  test motor and sensor directions) and odometry_test(). Report the
  "verdict" and every item in "problems", including the suggested fix.
- Report what the tools actually returned, including early stops.
"""


def new_conversation() -> list:
    """A fresh conversation history, starting with the standing instructions."""
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def get_tools() -> list:
    """Ask the robot server which tools it has (also checks link + token).

    The server owns the tool list, so adding a tool there makes it show up
    here automatically.
    """
    r = requests.get(f"{BRIDGE_URL}/tools", headers=AUTH, timeout=5)
    r.raise_for_status()
    return r.json()


def call_tool(name: str, args: dict) -> dict:
    """Run one tool on the robot server and return its result as a dict.

    Sends: POST {BRIDGE_URL}/tools/<name> with the arguments as a JSON body.
    Example: call_tool("drive", {"distance_in": 10}) -> {"ok": True, ...}

    Errors are returned as a dict instead of crashing, so the LLM can read
    "server unreachable" and tell the user, rather than everything dying.
    """
    try:
        r = requests.post(f"{BRIDGE_URL}/tools/{name}", json=args, headers=AUTH, timeout=TOOL_HTTP_TIMEOUT_S)
        if r.status_code == 401:
            return {"ok": False, "fatal": True, "hop": "server", "error": "bad_token",
                    "message": "the robot server rejected the token; BRIDGE_TOKEN differs between laptop and Pi"}
        r.raise_for_status()  # turn other HTTP errors (404, 500...) into exceptions
        return r.json()
    except requests.Timeout:
        # The server stopped answering mid-tool: try to stop the robot. If the
        # server is really gone, the brain stops on its own within 1 s.
        if name != "stop":
            try:
                requests.post(f"{BRIDGE_URL}/tools/stop", json={}, headers=AUTH, timeout=3)
            except requests.RequestException:
                pass
        return {"ok": False, "fatal": True, "hop": "server", "error": "timeout",
                "message": f"the robot server did not answer {name}() within {TOOL_HTTP_TIMEOUT_S} s; sent stop"}
    except requests.RequestException as e:
        return {"ok": False, "fatal": True, "hop": "server", "error": "server_unreachable",
                "message": f"cannot reach the robot server at {BRIDGE_URL} ({e}). The brain stops any Pi "
                           "motion on its own within 1 s of losing the Pi."}


def chat(messages: list, tools: list) -> dict:
    """Ask Ollama for the model's next message.

    `messages` is the entire conversation so far; the model has no memory of
    its own, so we resend everything each time. `tools` tells the model which
    tools exist and what arguments they take.

    Returns the model's message, a dict like one of these:
        {"role": "assistant", "content": "Done! I drove 10 inches."}
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "drive", "arguments": {"distance_in": 10}}}]}
    """
    r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=120, json={
        "model": MODEL,
        "messages": messages,
        "tools": tools,
        "stream": False,  # wait for the full reply instead of word-by-word
        # Models like qwen3 "think out loud" before answering, which made
        # each reply take minutes. Tool picking works fine without it.
        # Models that don't support thinking ignore this.
        "think": False,
    })
    r.raise_for_status()
    return r.json()["message"]


def run_turn(messages: list, tools: list, on_tool=None) -> str:
    """Handle one user request: model <-> tools until the model answers in text.

    The caller appends the user's message to `messages` first. This function
    appends everything that happens (model replies, tool results) to the same
    list, so the conversation is remembered for the next request.

    `on_tool(name, args, result)` is called after each tool runs, so the
    terminal can print it and the web app can show it on the page.

    Returns the model's final text answer. Never raises for a broken hop:
    if the server, serial link, brain or LLM fails, the turn stops and the
    answer says which hop failed and why.
    """
    health = check_health()
    if health.get("hop") == "server":
        return finish(messages, abort_message(health))

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            reply = chat(messages, tools)
        except requests.RequestException as e:
            return finish(messages, abort_message({"hop": "llm", "message": f"Ollama at {OLLAMA_URL} failed: {e}"}))
        messages.append(reply)  # remember what the model said

        calls = reply.get("tool_calls") or []
        if not calls:
            # No tool calls means this is the final answer.
            return (reply.get("content") or "").strip()

        # The model may ask for several tools at once; run them in order.
        for i, call in enumerate(calls):
            name = call["function"]["name"]
            args = call["function"].get("arguments") or {}
            result = call_tool(name, args)
            if on_tool:
                on_tool(name, args, result)
            # Feed the result back as a "tool" message so the model sees it
            # on the next chat() call. Content must be text, so the dict is
            # converted to a JSON string.
            messages.append({"role": "tool", "tool_name": name,
                             "content": json.dumps(result)})
            if result.get("fatal"):
                # The link itself broke: do not let the model carry on with
                # the remaining calls or retry. Skip the rest and say why.
                for skipped in calls[i + 1:]:
                    messages.append({"role": "tool", "tool_name": skipped["function"]["name"],
                                     "content": json.dumps({"ok": False, "skipped": True})})
                return finish(messages, abort_message(result))

    return "(gave up after too many tool calls)"


def finish(messages: list, text: str) -> str:
    """Ends a turn without the LLM, keeping the history consistent."""
    messages.append({"role": "assistant", "content": text})
    return text
