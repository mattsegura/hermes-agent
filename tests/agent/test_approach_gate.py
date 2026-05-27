from __future__ import annotations

import json

from agent.approach_gate import (
    check_approach_gate,
    is_irreversible_external_action,
    record_approach_approval,
)
from hermes_cli import kanban_db


def test_noop_outside_kanban_worker(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    assert check_approach_gate("send_message", {"channel": "sms"}) is None


def test_detects_irreversible_actions():
    assert is_irreversible_external_action("send_message", {"channel": "email"}) == (
        True,
        "send an outbound email with send_message",
    )
    assert is_irreversible_external_action("terminal", {"command": "curl -X POST https://api.example.test"})[0]
    assert is_irreversible_external_action("browser_click", {"text": "Submit"})[0]
    assert is_irreversible_external_action("mcp__crm__create_message", {})[0]
    assert is_irreversible_external_action("read_file", {"path": "notes.txt"}) == (False, "")


def test_unapproved_worker_action_returns_block_message(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_gate")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")

    message = check_approach_gate("send_message", {"channel": "sms", "to": "+15551234567"})

    assert message is not None
    assert message.startswith("APPROACH GATE: You're about to send an outbound SMS message")
    assert "kanban_block" in message


def test_record_approval_allows_future_worker_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_gate")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")

    record_approach_approval(
        "default",
        "email_send",
        {"tool": "send_message", "method": "email", "resource": "owner-approved SMTP"},
    )

    assert check_approach_gate("send_message", {"channel": "email"}) is None

    board_json = kanban_db.board_metadata_path("default")
    metadata = json.loads(board_json.read_text(encoding="utf-8"))
    assert metadata["approved_approaches"]["email_send"]["details"]["tool"] == "send_message"
