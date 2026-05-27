import copy
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock


sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

import run_agent


def _patch_agent_bootstrap(monkeypatch):
    monkeypatch.setattr(
        run_agent,
        "get_tool_definitions",
        lambda **kwargs: [
            {
                "type": "function",
                "function": {
                    "name": "terminal",
                    "description": "Run shell commands.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})


def _patch_responses_state_config(monkeypatch, *, enabled: bool, fallback: bool = True):
    from hermes_cli import config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "performance": {
                "telemetry": {"enabled": True, "log_turn_summary": False},
                "responses_state": {
                    "enabled": enabled,
                    "fallback_to_stateless": fallback,
                },
            }
        },
    )


def _build_agent(monkeypatch):
    _patch_agent_bootstrap(monkeypatch)

    agent = run_agent.AIAgent(
        model="gpt-5-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="codex-token",
        quiet_mode=True,
        max_iterations=4,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.client = MagicMock()
    agent._cleanup_task_resources = lambda task_id: None
    agent._persist_session = lambda messages, history=None: None
    agent._save_trajectory = lambda messages, user_message, completed: None
    return agent


def _message_response(text: str, *, response_id: str = "resp_final"):
    return SimpleNamespace(
        id=response_id,
        output=[
            SimpleNamespace(
                type="message",
                role="assistant",
                content=[SimpleNamespace(type="output_text", text=text)],
                status="completed",
            )
        ],
        usage=SimpleNamespace(input_tokens=5, output_tokens=3, total_tokens=8),
        status="completed",
        model="gpt-5-codex",
    )


def _tool_call_response(*, response_id: str = "resp_tool"):
    return SimpleNamespace(
        id=response_id,
        output=[
            SimpleNamespace(
                type="function_call",
                id="fc_1",
                call_id="call_1",
                name="terminal",
                arguments="{}",
                status="completed",
            )
        ],
        usage=SimpleNamespace(input_tokens=12, output_tokens=4, total_tokens=16),
        status="completed",
        model="gpt-5-codex",
    )


def _append_tool_results(assistant_message, messages, *_args):
    for tc in assistant_message.tool_calls:
        messages.append(
            {
                "role": "tool",
                "name": tc.function.name,
                "tool_call_id": getattr(tc, "call_id", None) or tc.id,
                "content": "tool ok",
            }
        )


def test_responses_state_disabled_sends_stateless_payload(monkeypatch):
    _patch_responses_state_config(monkeypatch, enabled=False)
    agent = _build_agent(monkeypatch)
    captured = []

    def _fake_api(api_kwargs):
        captured.append(copy.deepcopy(api_kwargs))
        return _message_response("OK")

    monkeypatch.setattr(agent, "_interruptible_api_call", _fake_api)

    result = agent.run_conversation("Say OK")

    assert result["completed"] is True
    assert captured[0]["store"] is False
    assert "previous_response_id" not in captured[0]
    assert result["performance"]["responses_state"]["enabled"] is False
    assert result["performance"]["responses_state"]["used"] is False


def test_responses_state_enabled_first_call_sends_full_store_request(monkeypatch):
    _patch_responses_state_config(monkeypatch, enabled=True)
    agent = _build_agent(monkeypatch)
    captured = []

    def _fake_api(api_kwargs):
        captured.append(copy.deepcopy(api_kwargs))
        return _message_response("OK", response_id="resp_first")

    monkeypatch.setattr(agent, "_interruptible_api_call", _fake_api)

    result = agent.run_conversation("Say OK")

    assert result["completed"] is True
    assert captured[0]["store"] is True
    assert "previous_response_id" not in captured[0]
    assert captured[0]["input"][0]["role"] == "user"
    assert result["performance"]["responses_state"]["enabled"] is True
    assert result["performance"]["responses_state"]["used"] is True


def test_responses_state_followup_sends_previous_id_with_tool_delta(monkeypatch):
    _patch_responses_state_config(monkeypatch, enabled=True)
    agent = _build_agent(monkeypatch)
    captured = []

    def _fake_api(api_kwargs):
        captured.append(copy.deepcopy(api_kwargs))
        if len(captured) == 1:
            return _tool_call_response(response_id="resp_tool")
        return _message_response("Done", response_id="resp_final")

    monkeypatch.setattr(agent, "_interruptible_api_call", _fake_api)
    monkeypatch.setattr(agent, "_execute_tool_calls", _append_tool_results)

    result = agent.run_conversation("Run the terminal tool")

    assert result["completed"] is True
    assert len(captured) == 2
    assert captured[0]["store"] is True
    assert "previous_response_id" not in captured[0]
    assert captured[1]["store"] is True
    assert captured[1]["previous_response_id"] == "resp_tool"
    assert captured[1]["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "tool ok",
        }
    ]
    state = result["performance"]["responses_state"]
    assert state["previous_response_id_used"] is True
    assert state["delta_input_items"] == 1


def test_responses_state_compatibility_change_resets_to_full_request(monkeypatch):
    _patch_responses_state_config(monkeypatch, enabled=True)
    agent = _build_agent(monkeypatch)
    captured = []

    def _fake_api(api_kwargs):
        captured.append(copy.deepcopy(api_kwargs))
        if len(captured) == 1:
            return _tool_call_response(response_id="resp_tool")
        return _message_response("Done", response_id="resp_final")

    def _execute_and_change_model(assistant_message, messages, *args):
        _append_tool_results(assistant_message, messages, *args)
        agent.model = "gpt-5.4"

    monkeypatch.setattr(agent, "_interruptible_api_call", _fake_api)
    monkeypatch.setattr(agent, "_execute_tool_calls", _execute_and_change_model)

    result = agent.run_conversation("Run the terminal tool")

    assert result["completed"] is True
    assert len(captured) == 2
    assert "previous_response_id" not in captured[1]
    assert captured[1]["store"] is True
    assert any(item.get("type") == "function_call" for item in captured[1]["input"])
    assert any(item.get("type") == "function_call_output" for item in captured[1]["input"])
    assert result["performance"]["responses_state"]["reset_reason"] == "compatibility_changed"


def test_responses_state_provider_rejection_falls_back_to_stateless(monkeypatch):
    _patch_responses_state_config(monkeypatch, enabled=True, fallback=True)
    agent = _build_agent(monkeypatch)
    captured = []

    class _StatefulRejected(RuntimeError):
        status_code = 400
        body = {"error": {"message": "Unknown parameter: store"}}

    def _fake_api(api_kwargs):
        captured.append(copy.deepcopy(api_kwargs))
        if len(captured) == 1:
            raise _StatefulRejected("Unknown parameter: store")
        return _message_response("Recovered stateless", response_id="resp_final")

    monkeypatch.setattr(agent, "_interruptible_api_call", _fake_api)

    result = agent.run_conversation("Say OK")

    assert result["completed"] is True
    assert result["final_response"] == "Recovered stateless"
    assert len(captured) == 2
    assert captured[0]["store"] is True
    assert captured[1]["store"] is False
    assert "previous_response_id" not in captured[1]
    state = result["performance"]["responses_state"]
    assert state["fallback_reason"] == "provider_rejected_stateful"
    assert state["stateless_retry_count"] == 1
