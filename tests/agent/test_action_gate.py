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


# ---------------------------------------------------------------------------
# H4: the action-gate notification text must NOT instruct the owner to reply
# "/approve <numeric-id>". That text path silently FAILS because the gateway
# routes a numeric token to _parse_approve_board_arg (board slugs may be
# all-digit), so the action-gate decision never lands. The honest fix points
# the owner at the inline Approve/Deny buttons; the action id is audit-only.
# ---------------------------------------------------------------------------

def _capture_notify_message(monkeypatch) -> dict:
    captured: dict = {}

    def _fake_send(message: str) -> None:
        captured["message"] = message

    monkeypatch.setattr(action_gate, "_send_telegram_notification", _fake_send)
    return captured


def test_notify_message_does_not_instruct_silently_failing_text_approve(monkeypatch):
    captured = _capture_notify_message(monkeypatch)

    action_gate._notify_human(
        action_id=42,
        tool_name="send_message",
        tool_args={"platform": "sms", "text": "hi"},
        description="Send SMS to seller",
        profile="land-worker",
        config={"notify": "telegram"},
    )

    msg = captured["message"]
    # The stale, silently-failing instruction must be gone.
    assert "/approve 42" not in msg
    assert "/deny 42" not in msg
    # The owner is pointed at the working inline-button path instead.
    assert "button" in msg.lower()
    # The action id is still present for the audit trail (just not as a command).
    assert "42" in msg


def test_notify_message_skipped_for_non_telegram_channel(monkeypatch):
    """Back-compat: a non-telegram channel still does not attempt a Telegram send."""
    captured = _capture_notify_message(monkeypatch)

    action_gate._notify_human(
        action_id=7,
        tool_name="terminal",
        tool_args={"command": "echo hi"},
        description="run a command",
        profile="default",
        config={"notify": "none"},
    )

    assert "message" not in captured
