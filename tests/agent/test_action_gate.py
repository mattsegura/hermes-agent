from __future__ import annotations

import pytest

from agent import action_gate


def _gate_config(mode: str = "smart") -> dict:
    return {
        "mode": mode,
        "rules": {
            "blocked_tools": ["imsg"],
        },
    }


@pytest.mark.parametrize("mode", ["smart", "manual", "yolo", "bogus"])
def test_terminal_blocked_substring_is_denied_regardless_of_mode(monkeypatch, mode):
    monkeypatch.setattr(action_gate, "_get_gate_config", lambda: _gate_config(mode))

    result = action_gate.check_action_gate(
        "terminal",
        {"command": "imsg send +15551234567 hello"},
    )

    assert result is not None
    assert result.startswith("ACTION BLOCKED:")
    assert "explicitly forbidden" in result


def test_unknown_action_gate_mode_fails_closed(monkeypatch):
    monkeypatch.setattr(action_gate, "_get_gate_config", lambda: _gate_config("surprise"))
    monkeypatch.setattr(
        action_gate,
        "_smart_decide",
        lambda *args, **kwargs: pytest.fail(
            "_smart_decide should not run for invalid mode"
        ),
    )

    result = action_gate.check_action_gate(
        "send_message",
        {"platform": "sms", "chat_id": "+15551234567", "text": "hello"},
    )

    assert result is not None
    assert result.startswith("ACTION BLOCKED: CONFIG ERROR:")
    assert "action_gate.mode='surprise' is invalid" in result


def test_terminal_routes_through_smart_gate_when_action_gate_active(monkeypatch):
    calls = []

    def fake_smart_decide(tool_name, tool_args, config, profile):
        calls.append((tool_name, tool_args, config, profile))
        return "approve"

    monkeypatch.setattr(action_gate, "_get_gate_config", lambda: _gate_config("smart"))
    monkeypatch.setattr(action_gate, "_smart_decide", fake_smart_decide)
    monkeypatch.setenv("HERMES_PROFILE", "land-worker")

    result = action_gate.check_action_gate(
        "terminal",
        {"command": "pytest tests/agent/test_action_gate.py"},
    )

    assert result is None
    assert calls == [
        (
            "terminal",
            {"command": "pytest tests/agent/test_action_gate.py"},
            _gate_config("smart"),
            "land-worker",
        )
    ]


def test_read_only_tools_still_skip_smart_gate(monkeypatch):
    monkeypatch.setattr(action_gate, "_get_gate_config", lambda: _gate_config("smart"))
    monkeypatch.setattr(
        action_gate,
        "_smart_decide",
        lambda *args, **kwargs: pytest.fail(
            "read-only tools should not call _smart_decide"
        ),
    )

    assert action_gate.check_action_gate("read_file", {"path": "README.md"}) is None


def test_no_action_gate_config_preserves_passthrough(monkeypatch):
    monkeypatch.setattr(action_gate, "_get_gate_config", lambda: {})
    monkeypatch.setattr(
        action_gate,
        "_smart_decide",
        lambda *args, **kwargs: pytest.fail(
            "_smart_decide should not run without config"
        ),
    )

    assert (
        action_gate.check_action_gate(
            "terminal",
            {"command": "imsg send +15551234567 hello"},
        )
        is None
    )
