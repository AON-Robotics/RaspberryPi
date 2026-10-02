"""The agent loop stops a turn cleanly when any hop breaks."""

import pytest
import requests

import loop


@pytest.fixture
def healthy(monkeypatch):
    monkeypatch.setattr(loop, "check_health", lambda: {"ok": True})


def scripted_chat(replies):
    replies = list(replies)
    seen = []

    def chat(messages, tools):
        seen.append([dict(m) for m in messages])
        return replies.pop(0)
    return chat, seen


def tool_call(name, **args):
    return {"function": {"name": name, "arguments": args}}


def test_fatal_tool_result_stops_the_turn_and_skips_the_rest(monkeypatch, healthy):
    chat, _ = scripted_chat([{"role": "assistant", "content": "",
                              "tool_calls": [tool_call("move", distance_in=10), tool_call("turn", degrees=90)]}])
    calls = []

    def call_tool(name, args):
        calls.append(name)
        return {"ok": False, "fatal": True, "hop": "serial", "error": "serial_lost", "message": "cable pulled"}
    monkeypatch.setattr(loop, "chat", chat)
    monkeypatch.setattr(loop, "call_tool", call_tool)
    messages = loop.new_conversation()
    answer = loop.run_turn(messages, [])
    assert calls == ["move"]  # turn() never ran
    assert answer.startswith("Stopped: the USB serial link between the Pi and the brain failed.")
    assert "cable pulled" in answer
    assert messages[-1] == {"role": "assistant", "content": answer}


def test_non_fatal_error_goes_back_to_the_model(monkeypatch, healthy):
    chat, seen = scripted_chat([
        {"role": "assistant", "content": "", "tool_calls": [tool_call("move", distance_in=10)]},
        {"role": "assistant", "content": "The robot is disabled."},
    ])
    monkeypatch.setattr(loop, "chat", chat)
    monkeypatch.setattr(loop, "call_tool", lambda n, a: {"ok": False, "hop": "brain", "error": "refused",
                                                         "message": "robot is disabled", "fatal": False})
    assert loop.run_turn(loop.new_conversation(), []) == "The robot is disabled."
    assert '"refused"' in seen[1][-1]["content"]  # the model saw the brain's answer


def test_unreachable_server_aborts_before_asking_the_llm(monkeypatch):
    monkeypatch.setattr(loop, "check_health", lambda: {"ok": False, "hop": "server", "message": "no route"})
    monkeypatch.setattr(loop, "chat", lambda *a: pytest.fail("LLM must not be called"))
    assert loop.run_turn(loop.new_conversation(), []).startswith("Stopped: the robot server on the Pi failed.")


def test_llm_failure_is_reported_not_raised(monkeypatch, healthy):
    def broken(*_):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(loop, "chat", broken)
    assert loop.run_turn(loop.new_conversation(), []).startswith("Stopped: the LLM (Ollama) failed.")


def test_call_tool_marks_server_failures_fatal(monkeypatch):
    monkeypatch.setattr(loop, "BRIDGE_URL", "http://127.0.0.1:9")  # nothing listens on the discard port
    result = loop.call_tool("status", {})
    assert result["fatal"] and result["hop"] == "server" and result["error"] == "server_unreachable"
