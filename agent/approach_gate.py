"""Board-level approach gating for kanban worker tool calls."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional

from hermes_cli import kanban_db


_MCP_ACTION_WORDS = (
    "send",
    "post",
    "create_message",
    "sms",
    "email",
    "call",
)

_OUTBOUND_CLI_WORDS = (
    "twilio",
    "sendgrid",
    "mailgun",
    "postmark",
    "resend",
    "aws ses",
)


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten_text(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text(v) for v in value)
    if value is None:
        return ""
    return str(value)


def _terminal_command(tool_args: dict) -> str:
    for key in ("command", "cmd", "input", "script"):
        value = tool_args.get(key)
        if value:
            return str(value)
    return _flatten_text(tool_args)


def _browser_click_target(tool_args: dict) -> str:
    for key in ("text", "label", "button", "selector", "target", "aria_label"):
        value = tool_args.get(key)
        if value:
            return str(value)
    return _flatten_text(tool_args)


def _send_message_action_type(tool_args: dict) -> str:
    text = _flatten_text(tool_args).casefold()
    if "sms" in text or "phone" in text:
        return "sms_outreach"
    if "email" in text or "mail" in text:
        return "email_send"
    return "message_send"


def _mcp_action_type(tool_name: str) -> str:
    name = tool_name.casefold()
    if "sms" in name:
        return "sms_outreach"
    if "email" in name:
        return "email_send"
    if "call" in name:
        return "phone_call"
    if "create_message" in name or "send" in name:
        return "message_send"
    return "external_post"


def _terminal_action_type(command: str) -> str:
    text = command.casefold()
    if "twilio" in text or "sms" in text:
        return "sms_outreach"
    if any(word in text for word in ("sendgrid", "mailgun", "postmark", "resend", "email")):
        return "email_send"
    return "external_post"


def _classify_irreversible_external_action(
    tool_name: str,
    tool_args: dict,
) -> tuple[bool, str, str]:
    name = (tool_name or "").strip()
    name_folded = name.casefold()

    if name_folded == "send_message":
        action_type = _send_message_action_type(tool_args)
        if action_type == "sms_outreach":
            return True, action_type, "send an outbound SMS message with send_message"
        if action_type == "email_send":
            return True, action_type, "send an outbound email with send_message"
        return True, action_type, "send an outbound message with send_message"

    if name_folded == "terminal":
        command = _terminal_command(tool_args)
        command_folded = command.casefold()
        has_post = bool(re.search(r"\bcurl\b.*(?:-x\s*post|--request\s+post)", command_folded, re.DOTALL))
        has_outbound_cli = any(word in command_folded for word in _OUTBOUND_CLI_WORDS)
        if has_post or has_outbound_cli:
            action_type = _terminal_action_type(command)
            return True, action_type, f"run a terminal command that performs an outbound action: {command}"

    if name_folded == "browser_click":
        target = _browser_click_target(tool_args)
        if re.search(r"\b(send|submit)\b", target.casefold()):
            return True, "form_submit", f"click a browser {target!r} control that may submit a form"

    if name_folded.startswith("mcp__") and any(word in name_folded for word in _MCP_ACTION_WORDS):
        action_type = _mcp_action_type(name)
        return True, action_type, f"call outbound MCP tool {name}"

    return False, "", ""


def is_irreversible_external_action(tool_name: str, tool_args: dict) -> tuple[bool, str]:
    """Return whether a tool call is likely irreversible, plus a description."""
    is_irreversible, _action_type, description = _classify_irreversible_external_action(
        tool_name,
        tool_args or {},
    )
    return is_irreversible, description


def _active_board() -> str:
    return (os.environ.get("HERMES_KANBAN_BOARD") or kanban_db.DEFAULT_BOARD).strip() or kanban_db.DEFAULT_BOARD


def _approved_approaches(board: str) -> dict:
    metadata = kanban_db.read_board_metadata(board)
    approved = metadata.get("approved_approaches")
    return approved if isinstance(approved, dict) else {}


def check_approach_gate(tool_name: str, tool_args: dict) -> Optional[str]:
    """Return a block message for unapproved irreversible worker actions."""
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return None

    is_irreversible, action_type, description = _classify_irreversible_external_action(
        tool_name,
        tool_args or {},
    )
    if not is_irreversible:
        return None

    try:
        approved = _approved_approaches(_active_board())
    except Exception:
        approved = {}

    if action_type in approved:
        return None

    return (
        f"APPROACH GATE: You're about to {description}. "
        "This approach hasn't been approved for this board. "
        "Block yourself with kanban_block and describe your planned approach "
        "(what tool, what method, what resource you'll use). "
        "Once the owner approves and unblocks, the approach will be cached "
        "and future runs won't ask again."
    )


def record_approach_approval(board: str, action_type: str, details: dict) -> None:
    """Record owner approval for an approach in the board metadata file."""
    if not action_type:
        return

    metadata = kanban_db.read_board_metadata(board)
    metadata.pop("db_path", None)
    approved = metadata.get("approved_approaches")
    if not isinstance(approved, dict):
        approved = {}
    approved[str(action_type)] = {
        "approved_at": int(time.time()),
        "details": dict(details or {}),
    }
    metadata["approved_approaches"] = approved

    path = kanban_db.board_metadata_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
