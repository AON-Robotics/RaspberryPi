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
import time

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

# Header sent with every request to the robot server. This is the token
# check: the server rejects anything without it (HTTP 401).
AUTH = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}

# The "system" message is standing instructions the model sees before any
# user message. It sets the model's role and habits.
SYSTEM_PROMPT = """\
You help debug a VEX robot. Use the tools to move it or read its state.

Rules:
- One motion per tool call. drive() only moves straight; turn() only rotates.
  For "drive 10 inches then turn left", call drive, wait for its result,
  then call turn.
- Only pass the arguments a tool lists. Never invent arguments.
- If a tool returns an error, read it, fix the arguments, and call again.
  Do not ask the user to rephrase because of a tool error.
- The server enforces speed and distance limits. If a result says
  "clamped": true, tell the user what was actually done.
- Never compute or guess the robot's position or heading yourself. When the
  user asks where the robot is, call status() and report what it returns.
- Keep moves small, check status() when unsure, and report what the tools
  actually returned, including early stops.
"""


def check_pipeline() -> list[dict]:
    """Check every hop between this app and the robot, in order.

        1. ollama  - Ollama answers at OLLAMA_URL
        2. model   - MODEL is pulled
        3. robot   - robot server answers /health (no token needed)
        4. token   - robot server accepts BRIDGE_TOKEN
        5. tools   - robot server offers a stop() tool

    Returns one dict per hop: {"name", "ok", "ms", "detail", "fix"}. A hop
    that depends on a failed one is reported as skipped (ok False) instead of
    timing out a second time. Never raises, so callers can always show it.
    """
    results = []

    def hop(name, fix, check, needs=None):
        # `needs` is the hop this one depends on; skip if it failed.
        if needs and not next(r for r in results if r["name"] == needs)["ok"]:
            results.append({"name": name, "ok": False, "ms": None,
                            "detail": f"skipped: {needs} failed", "fix": ""})
            return None
        start = time.perf_counter()
        try:
            detail, value = check()
            ok = True
        except requests.ConnectionError:
            detail, value, ok = "not reachable (connection refused or no route)", None, False
        except requests.Timeout:
            detail, value, ok = "no answer within 3 s", None, False
        except Exception as e:  # any failure is a red light, never a crash
            detail, value, ok = f"{type(e).__name__}: {e}", None, False
        results.append({"name": name, "ok": ok,
                        "ms": round((time.perf_counter() - start) * 1000),
                        "detail": detail, "fix": "" if ok else fix})
        return value

    def ollama():
        r = requests.get(f"{OLLAMA_URL}/api/version", timeout=3)
        r.raise_for_status()
        return f"version {r.json().get('version', '?')}", None

    def model():
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        r.raise_for_status()
        names = {m["name"] for m in r.json().get("models", [])}
        # "qwen3" means "qwen3:latest" to Ollama.
        wanted = MODEL if ":" in MODEL else f"{MODEL}:latest"
        if wanted not in names:
            raise LookupError(f"{MODEL} is not pulled")
        return MODEL, None

    def robot():
        r = requests.get(f"{BRIDGE_URL}/health", timeout=3)
        r.raise_for_status()
        if not r.json().get("ok"):
            raise RuntimeError(f"server says not ok: {r.text[:200]}")
        return BRIDGE_URL, None

    def token():
        r = requests.get(f"{BRIDGE_URL}/tools", headers=AUTH, timeout=3)
        if r.status_code == 401:
            raise PermissionError("server rejected BRIDGE_TOKEN (401)")
        if r.status_code == 429:
            raise PermissionError("server is blocking this machine for a minute after "
                                  "too many wrong tokens (429)")
        r.raise_for_status()
        return "accepted", r.json()

    def tools(tool_list):
        names = [t["function"]["name"] for t in tool_list]
        if "stop" not in names:
            raise LookupError(f"no stop() tool; server offers {names}")
        return ", ".join(names), None

    hop("ollama", "Start Ollama (ollama serve) on this laptop.", ollama)
    hop("model", f"Run: ollama pull {MODEL}", model, needs="ollama")
    hop("robot", f"Is the robot server running and reachable at {BRIDGE_URL}? "
        "Check Tailscale on both machines.", robot)
    tool_list = hop("token", "BRIDGE_TOKEN here must equal the server's BRIDGE_TOKEN.",
                    token, needs="robot")
    hop("tools", "The robot server is missing tools; check server.py.",
        lambda: tools(tool_list), needs="token")
    return results


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
        r = requests.post(f"{BRIDGE_URL}/tools/{name}", json=args, headers=AUTH, timeout=15)
        r.raise_for_status()  # turn HTTP errors (401, 404, 500...) into exceptions
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"server unreachable or refused: {e}"}


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

    Returns the model's final text answer.
    """
    # The model may only call tools the server listed. The name ends up in a
    # URL, so an invented one like "../x" must never reach the server.
    allowed = {t["function"]["name"] for t in tools}
    for _ in range(MAX_TOOL_ROUNDS):
        reply = chat(messages, tools)
        messages.append(reply)  # remember what the model said

        calls = reply.get("tool_calls") or []
        if not calls:
            # No tool calls means this is the final answer.
            return (reply.get("content") or "").strip()

        # The model may ask for several tools at once; run them in order.
        for call in calls:
            name = call["function"]["name"]
            args = call["function"].get("arguments") or {}
            if name not in allowed:
                result = {"ok": False, "error": f"There is no tool named {name!r}. "
                          f"Available tools: {', '.join(sorted(allowed))}."}
            elif not isinstance(args, dict):
                result = {"ok": False, "error": "Arguments must be a JSON object "
                          "like {\"distance_in\": 10}. Call again."}
            else:
                result = call_tool(name, args)
            if on_tool:
                on_tool(name, args, result)
            # Feed the result back as a "tool" message so the model sees it
            # on the next chat() call. Content must be text, so the dict is
            # converted to a JSON string.
            messages.append({"role": "tool", "tool_name": name,
                             "content": json.dumps(result)})

    return "(gave up after too many tool calls)"
