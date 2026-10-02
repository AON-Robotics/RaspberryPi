"""Terminal chat with the robot. Runs on the gaming laptop.

The actual agent loop lives in loop.py (shared with the web app); this file
only handles typing and printing. See loop.py for the settings it reads
(BRIDGE_URL, BRIDGE_TOKEN, OLLAMA_URL, MODEL).

Run:
    BRIDGE_URL=http://mac-name:8000 BRIDGE_TOKEN=... python agent.py
"""

import loop


def print_tool(name: str, args: dict, result: dict) -> None:
    """Show each tool call as it happens, so you see what the robot did."""
    print(f"  [{name}({args}) -> {result}]")


def preflight() -> bool:
    """Print the health of every hop; True if all are fine."""
    print("Checking the pipeline:")
    results = loop.check_pipeline()
    for r in results:
        mark = "ok  " if r["ok"] else "FAIL"
        ms = f" ({r['ms']} ms)" if r["ms"] is not None else ""
        print(f"  [{mark}] {r['name']:<7}{r['detail']}{ms}")
        if r["fix"]:
            print(f"         fix: {r['fix']}")
    print()
    return all(r["ok"] for r in results)


def main() -> None:
    if not preflight():
        raise SystemExit("Not starting: fix the failed step above and run again.")
    tools = loop.get_tools()
    print(f"Connected to {loop.BRIDGE_URL}; tools: {', '.join(t['function']['name'] for t in tools)}")
    print("Type a request, or 'quit'. Ctrl+C sends stop() to the robot.\n")

    # The conversation history. Every message (yours, the model's, and each
    # tool result) gets appended here, and the whole list is sent each turn.
    messages = loop.new_conversation()

    # One pass per thing you type.
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            # Ctrl+C / Ctrl+D at the prompt: stop the robot, then exit.
            loop.call_tool("stop", {})
            print("\nstopped robot, bye")
            return
        if user.lower() in {"quit", "exit"}:
            return
        if not user:
            continue
        messages.append({"role": "user", "content": user})

        try:
            answer = loop.run_turn(messages, tools, on_tool=print_tool)
            print(f"robot> {answer}\n")
        except KeyboardInterrupt:
            # Ctrl+C while the model or a tool is running: emergency stop.
            loop.call_tool("stop", {})
            print("\n  [interrupted: sent stop()]\n")


# Only run main() when this file is executed directly (python agent.py),
# not when another file imports it.
if __name__ == "__main__":
    main()
