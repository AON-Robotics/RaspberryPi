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


def print_health() -> bool:
    """One line per hop, so a broken link is obvious before typing anything."""
    llm = loop.check_llm()
    print(f"  llm     {'ok ' if llm['ok'] else 'FAIL'} {loop.MODEL} @ {loop.OLLAMA_URL} {llm.get('message', '')}")
    health = loop.check_health()
    if health.get("hop") == "server":
        print(f"  server  FAIL {health['message']}")
        return False
    print(f"  server  ok   {loop.BRIDGE_URL}")
    serial, brain = health["serial"], health["brain"]
    print(f"  serial  {'ok ' if serial['ok'] else 'FAIL'} {serial.get('port') or serial.get('error')}")
    print(f"  brain   {'ok ' if brain['ok'] else 'FAIL'} "
          f"{('robot=%s mode=%s rtt=%sms' % (brain.get('robot'), brain.get('mode'), brain.get('rtt_ms'))) if brain['ok'] else brain.get('error')}")
    return llm["ok"]


def main() -> None:
    print("Link check:")
    if not print_health():
        print("Fix the failing hop above, then start again.")
        return
    tools = loop.get_tools()
    print(f"Tools: {', '.join(t['function']['name'] for t in tools)}")
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
