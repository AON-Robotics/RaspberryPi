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
    OLLAMA_URL    where Ollama runs; default http://127.0.0.1:11434
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
# 127.0.0.1, not "localhost": on Windows "localhost" tries IPv6 first and
# waits ~2 s per request before falling back, since Ollama listens on IPv4.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
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



# Header sent with every request to the robot server. This is the token
# check: the server rejects anything without it (HTTP 401).
AUTH = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}

# The "system" message is standing instructions the model sees before any
# user message. It sets the model's role and habits.
# Only name tools the server really has: when this prompt mentioned a drive()
# the server no longer had, qwen3 answered with nothing at all. move() and
# turn() are named because the sign rules need them; if a tool is renamed on
# the server, rename it here too. run_turn() also appends the server's live
# tool list to this text every turn (system_prompt(tools)), so the model
# always sees the exact current names.
SYSTEM_PROMPT = """\
You help debug a VEX robot. Use the tools to move it, test it, or read its state.

Rules:
- One motion per tool call. move() only drives straight; turn() only rotates.
  For "drive 10 inches then turn left", call move, wait for its result,
  then call turn.
- turn() degrees are CLOCKWISE positive: "turn right 90" is degrees=90,
  "turn left 90" is degrees=-90.
- Follow each tool's description exactly, especially which sign means
  forward/backward and left/right.
- Only pass the arguments a tool lists. Never invent arguments.
- If a tool returns "error": "bad_args" or "unknown_tool", read its
  "message", fix the tool name or arguments, and call again. For any other
  error, do not retry the same motion: tell the user the "message" in plain
  words.
- If a result says "clamped": true, tell the user what was actually done.
- Never compute or guess the robot's position or heading yourself. When the
  user asks where the robot is, call status() and report what it returns.
- To check the drivetrain or odometry, use diagnose() (active=true to also
  test motor and sensor directions) and odometry_test(). Report the
  "verdict" and every item in "problems", including the suggested fix.
- Report what the tools actually returned, including early stops.
"""


class HopDown(Exception):
    """A pipeline step is down; the message says why (used by check_pipeline)."""


def check_pipeline() -> list[dict]:
    """Check every hop between this app and the robot, in order.

        1. ollama  - Ollama answers at OLLAMA_URL
        2. model   - MODEL is pulled
        3. robot   - robot server answers /health (no token needed)
        4. serial  - the server has the brain's USB serial port open
        5. brain   - the robot program answers on that port (same protocol)
        6. token   - robot server accepts BRIDGE_TOKEN
        7. tools   - robot server offers a stop() tool

    Steps 4-5 come from the server's /health. When one is down, the reason
    is fetched from /health/details (needs the token) so the light says why.

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
        except HopDown as e:  # serial/brain down: the server told us why
            detail, value, ok = str(e), None, False
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
        # Only "is the server there"; serial and brain are their own steps.
        r = requests.get(f"{BRIDGE_URL}/health", timeout=3)
        r.raise_for_status()
        return BRIDGE_URL, r.json()

    def link_hop(report, name):
        hop_report = (report or {}).get(name) or {}
        if hop_report.get("ok"):
            return "ok", None
        try:  # the reason needs the token; fall back to the short code
            details = requests.get(f"{BRIDGE_URL}/health/details", headers=AUTH, timeout=3).json()
            reason = details[name].get("error")
        except (requests.RequestException, ValueError, KeyError, AttributeError):
            reason = hop_report.get("error", "down")
        raise HopDown(reason)

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
    report = hop("robot", f"Is the robot server running and reachable at {BRIDGE_URL}? "
                 "Check Tailscale on both machines.", robot)
    hop("serial", "Check the USB cable between the Pi and the brain's USB port, and that the brain "
        "is on. Set BRAIN_PORT on the Pi if the port is not found.",
        lambda: link_hop(report, "serial"), needs="robot")
    hop("brain", "Run the Override program with aon::pi::start() on the brain, in driver control.",
        lambda: link_hop(report, "brain"), needs="serial")
    tool_list = hop("token", "BRIDGE_TOKEN here must equal the server's BRIDGE_TOKEN.",
                    token, needs="robot")
    hop("tools", "The robot server is missing tools; check server.py.",
        lambda: tools(tool_list), needs="token")
    return results


def new_conversation() -> list:
    """A fresh conversation history, starting with the standing instructions."""
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def system_prompt(tools: list) -> str:
    """SYSTEM_PROMPT plus the server's current tool names and descriptions.

    With thinking off, qwen3 often answered with nothing at all (no text, no
    tool call) unless the prompt itself named the tools. Building the list
    from the server keeps it right when tools are renamed (drive -> move).
    """
    summary = "\n".join(f"- {t['function']['name']}: {t['function'].get('description', '')}"
                        for t in tools)
    return f"{SYSTEM_PROMPT}\nTools available right now (use these exact names):\n{summary}\n"


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
    # Cheap check first: no point asking the LLM if the Pi is unreachable.
    health = check_health()
    if health.get("hop") == "server":
        return finish(messages, abort_message(health))

    # The model may only call tools the server listed. The name ends up in a
    # URL, so an invented one like "../x" must never reach the server.
    allowed = {t["function"]["name"] for t in tools}
    # Refresh the standing instructions with the tools the server has now.
    if messages and messages[0].get("role") == "system":
        messages[0] = {"role": "system", "content": system_prompt(tools)}
    retried = False
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            reply = chat(messages, tools)
        except requests.RequestException as e:
            return finish(messages, abort_message({"hop": "llm", "message": f"Ollama at {OLLAMA_URL} failed: {e}"}))
        calls = reply.get("tool_calls") or []
        content = (reply.get("content") or "").strip()
        if not calls and not content and not retried:
            # An empty answer is a model glitch, not an answer: ask once more
            # without keeping the empty message in the history.
            retried = True
            continue
        messages.append(reply)  # remember what the model said

        if not calls:
            # No tool calls means this is the final answer.
            return content or ("(The model gave an empty answer. Try rephrasing, "
                               "e.g. \"move forward 10 inches\".)")

        # The model may ask for several tools at once; run them in order.
        for i, call in enumerate(calls):
            name = call["function"]["name"]
            args = call["function"].get("arguments") or {}
            if name not in allowed:
                result = {"ok": False, "hop": "llm", "error": "unknown_tool",
                          "message": f"There is no tool named {name!r}. "
                          f"Available tools: {', '.join(sorted(allowed))}."}
            elif not isinstance(args, dict):
                result = {"ok": False, "hop": "llm", "error": "bad_args",
                          "message": "Arguments must be a JSON object like {\"distance_in\": 10}. Call again."}
            else:
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
