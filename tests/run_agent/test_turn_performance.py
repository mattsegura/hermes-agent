from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _mock_response():
    message = SimpleNamespace(content="Final answer", tool_calls=None, reasoning=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(
        choices=[choice],
        model="test/model",
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            prompt_tokens_details=SimpleNamespace(cached_tokens=4),
            cache_creation_input_tokens=2,
        ),
    )


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr("run_agent._hermes_home", tmp_path)
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent.compression_enabled = False
        agent.save_trajectories = False
        return agent


def test_run_conversation_attaches_turn_performance(agent, monkeypatch, caplog):
    from hermes_cli import config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "performance": {
                "telemetry": {"enabled": True, "log_turn_summary": True}
            }
        },
    )
    monkeypatch.setattr(agent, "_has_stream_consumers", lambda: True)

    def _streaming_call(api_kwargs, on_first_delta=None):
        if on_first_delta:
            on_first_delta()
        return _mock_response()

    agent._interruptible_streaming_api_call = _streaming_call

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        caplog.at_level("INFO", logger="agent.conversation_loop"),
    ):
        result = agent.run_conversation("hello")

    assert result["final_response"] == "Final answer"
    assert result["completed"] is True
    assert result["performance"]["enabled"] is True
    assert result["performance"]["request"]["message_count"] >= 1
    assert result["performance"]["request"]["tool_count"] == 1
    assert result["performance"]["request"]["rough_token_estimate"] is not None
    assert result["performance"]["api_duration_ms"] is not None
    assert result["performance"]["time_to_first_delta_ms"] is not None
    assert result["performance"]["cache"] == {"read_tokens": 4, "write_tokens": 2}
    assert "turn_performance " in caplog.text
