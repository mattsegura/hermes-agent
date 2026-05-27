"""Action Gate — three-tier approval system for irreversible tool calls.

Intercepts tool calls before execution and routes them through one of
three modes:

  yolo   → execute immediately, no checks
  smart  → lightweight LLM decides: approve / deny / escalate to human
  manual → always escalate to human

The gate is HARD — it physically prevents execution by returning an error
to the agent. The agent cannot bypass it. This is code enforcement, not
prompt enforcement.

Configuration lives in the profile's config.yaml:

    action_gate:
      mode: smart          # yolo | smart | manual
      notify: telegram     # where escalations go
      timeout: 300         # seconds to wait for human response
      rules:
        blocked_tools: [imsg]  # tools that are NEVER allowed
        sms_only_via: infobip  # if set, block imsg/twilio for SMS
      smart_model: claude-haiku-3-5  # cheap model for gate decisions

The gate classifies tools into:
  - safe: file reads, searches, kanban operations → always pass
  - gated: outbound messages, payments, credential use → check mode
  - blocked: explicitly forbidden tools → always deny
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import threading
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------

# Tools that are obviously read-only or internal — skip the gate entirely
_READ_ONLY_TOOLS = frozenset({
    "read_file", "search_files", "browser_snapshot", "browser_navigate",
    "browser_scroll", "browser_back", "browser_get_images",
    "web_search", "web_extract", "wiki_read", "wiki_search", "wiki_list",
    "wiki_status", "wiki_write", "wiki_patch", "wiki_append",
    "session_search", "fact_store", "fact_feedback",
    "skill_view", "skills_list", "todo", "memory", "process",
    "kanban_list", "kanban_show", "kanban_log", "kanban_comment",
    "kanban_create", "kanban_complete", "kanban_block", "kanban_update",
    "write_file", "patch", "terminal",
    "delegate_task", "execute_code", "clarify",
    "browser_click", "browser_type", "browser_press",
})


def _classify_tool(
    tool_name: str,
    tool_args: dict,
    config: dict,
) -> str:
    """Classify a tool call as 'safe', 'blocked', or 'check'.

    'safe' = skip gate entirely (read-only operations)
    'blocked' = hard deny (explicitly forbidden)
    'check' = send to smart LLM or escalate (everything else)
    """
    name = (tool_name or "").strip()
    name_lower = name.casefold()
    rules = config.get("rules", {})
    blocked_tools = [t.casefold() for t in rules.get("blocked_tools", [])]

    # Explicitly blocked tools — check both tool name and terminal commands
    if name_lower in blocked_tools:
        return "blocked"
    if name_lower == "terminal":
        cmd = str(tool_args.get("command", "")).casefold()
        for blocked in blocked_tools:
            if blocked in cmd:
                return "blocked"

    # Read-only tools never need approval
    if name_lower in _READ_ONLY_TOOLS:
        return "safe"

    # Everything else goes through the mode (smart/manual)
    return "check"


# ---------------------------------------------------------------------------
# Approval queue (SQLite-backed, lives alongside kanban)
# ---------------------------------------------------------------------------

_QUEUE_DB_PATH: Optional[Path] = None
_QUEUE_INIT_LOCK = threading.Lock()


def _queue_db_path() -> Path:
    """Return path to the action gate queue database."""
    global _QUEUE_DB_PATH
    if _QUEUE_DB_PATH is not None:
        return _QUEUE_DB_PATH
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    _QUEUE_DB_PATH = hermes_home / "action_gate.db"
    return _QUEUE_DB_PATH


def _ensure_queue_db() -> sqlite3.Connection:
    """Open and initialize the approval queue database."""
    path = _queue_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    with _QUEUE_INIT_LOCK:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pending_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_args TEXT NOT NULL,
                description TEXT NOT NULL,
                classification TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                decision TEXT,
                decided_by TEXT,
                decided_at INTEGER,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                session_id TEXT,
                task_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pending_status
                ON pending_actions(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_pending_profile
                ON pending_actions(profile, status);
        """)
    return conn


# ---------------------------------------------------------------------------
# Smart mode — lightweight LLM decision
# ---------------------------------------------------------------------------

_SMART_SYSTEM_PROMPT = """You are an action approval gate for an autonomous AI agent system.
You decide whether to APPROVE, DENY, or ESCALATE tool calls that could have irreversible effects.

Rules:
- APPROVE: routine actions on approved channels/tools that match the agent's assigned work
- DENY: actions that violate explicit rules (wrong tool, wrong channel, blocked behavior)
- ESCALATE: novel actions, high-value decisions, or anything you're unsure about

You MUST respond with exactly one JSON object:
{"decision": "approve"|"deny"|"escalate", "reason": "brief explanation"}

Context about this system:
- Agents work on a land wholesaling pipeline (FL vacant land)
- SMS should ONLY go through the configured business number, NEVER personal iMessage
- Outbound messages to real people are high-stakes (reputation, legal)
- File operations, web searches, kanban updates are safe
- The owner wants autonomous operation but with guardrails on external actions
"""


def _smart_decide(
    tool_name: str,
    tool_args: dict,
    config: dict,
    profile: str,
) -> str:
    """Use a cheap LLM call to decide: approve / deny / escalate.

    Returns one of: 'approve', 'deny', 'escalate'
    """
    rules = config.get("rules", {})
    smart_model = config.get("smart_model", "claude-haiku-3-5")

    # Build context for the decision
    context = (
        f"Profile: {profile}\n"
        f"Tool: {tool_name}\n"
        f"Args: {json.dumps(tool_args, default=str)[:2000]}\n"
        f"Rules: {json.dumps(rules, default=str)}\n"
    )

    try:
        # Use a minimal LLM call via the provider system
        from agent.provider_adapters import get_completion_simple
        response = get_completion_simple(
            model=smart_model,
            system=_SMART_SYSTEM_PROMPT,
            user=context,
            max_tokens=100,
        )
        # Parse the response
        match = re.search(r'"decision"\s*:\s*"(approve|deny|escalate)"', response)
        if match:
            return match.group(1)
        # If parsing fails, escalate (safe default)
        _log.warning("Smart gate: could not parse LLM response, escalating: %s", response[:200])
        return "escalate"
    except Exception as e:
        _log.warning("Smart gate LLM call failed, escalating: %s", e)
        return "escalate"


# ---------------------------------------------------------------------------
# Notification (Telegram)
# ---------------------------------------------------------------------------

def _notify_human(
    action_id: int,
    tool_name: str,
    tool_args: dict,
    description: str,
    profile: str,
    config: dict,
) -> None:
    """Send approval request to the configured notification channel."""
    notify_channel = config.get("notify", "telegram")

    # Format the message
    args_preview = json.dumps(tool_args, default=str)[:500]
    message = (
        f"🚨 Action Gate — Approval Required\n\n"
        f"Profile: {profile}\n"
        f"Tool: {tool_name}\n"
        f"Action: {description}\n"
        f"Args: {args_preview}\n\n"
        f"Reply with:\n"
        f"  /approve {action_id}\n"
        f"  /deny {action_id}\n"
        f"  /deny {action_id} disable_tool"
    )

    if notify_channel == "telegram":
        try:
            _send_telegram_notification(message)
        except Exception as e:
            _log.error("Failed to send Telegram notification: %s", e)


def _send_telegram_notification(message: str) -> None:
    """Send a message via the configured Telegram bot."""
    import urllib.request

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    if not bot_token or not chat_id:
        _log.warning("Telegram not configured for action gate notifications")
        return

    # Use first allowed user as the notification target
    target = chat_id.split(",")[0].strip()
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = json.dumps({
        "chat_id": target,
        "text": message,
        "parse_mode": "HTML",
    }).encode()

    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        _log.error("Telegram send failed: %s", e)


# ---------------------------------------------------------------------------
# Main gate function — called from model_tools.py
# ---------------------------------------------------------------------------

def _describe_action(tool_name: str, tool_args: dict) -> str:
    """Generate a human-readable description of what the tool call does."""
    name = tool_name.casefold()
    if name == "terminal":
        cmd = str(tool_args.get("command", ""))
        return f"Run: {cmd[:150]}"
    if name == "send_message":
        platform = tool_args.get("platform", "unknown")
        chat_id = tool_args.get("chat_id", "unknown")
        return f"Send message via {platform} to {chat_id}"
    if name.startswith("mcp__"):
        return f"Call MCP tool: {tool_name}"
    args_short = json.dumps(tool_args, default=str)[:100]
    return f"{tool_name}({args_short})"


def _get_gate_config() -> dict:
    """Load action_gate config from the active profile."""
    try:
        from hermes_constants import get_hermes_home
        config_path = get_hermes_home() / "config.yaml"
        if config_path.exists():
            import yaml
            with open(config_path) as f:
                full_config = yaml.safe_load(f) or {}
            return full_config.get("action_gate", {})
    except Exception:
        pass
    return {}


def check_action_gate(
    tool_name: str,
    tool_args: dict,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Main entry point — check if a tool call requires approval.

    Returns None if the action is allowed to proceed.
    Returns an error message string if the action is blocked/pending.

    This function is SYNCHRONOUS. For escalated actions, it blocks
    (polls the queue) until the human responds or timeout expires.
    """
    config = _get_gate_config()

    # If no action_gate config exists, gate is disabled — pass through
    if not config:
        return None

    mode = config.get("mode", "smart")
    if mode == "yolo":
        return None

    # Classify the tool call
    classification = _classify_tool(tool_name, tool_args, config)

    if classification == "safe":
        return None

    if classification == "blocked":
        return (
            f"ACTION BLOCKED: {tool_name} is explicitly forbidden by the action gate. "
            f"This tool cannot be used regardless of approval mode. "
            f"Use an approved alternative."
        )

    # classification == "check" — needs smart decision or escalation
    profile = os.environ.get("HERMES_PROFILE", "default")
    description = _describe_action(tool_name, tool_args)

    if mode == "manual":
        # Always escalate to human
        return _escalate_to_human(
            tool_name, tool_args, description, classification,
            profile, config, session_id, task_id,
        )

    if mode == "smart":
        decision = _smart_decide(tool_name, tool_args, config, profile)
        if decision == "approve":
            _log.info("Action gate [smart]: APPROVED %s by %s", tool_name, profile)
            return None
        if decision == "deny":
            return (
                f"ACTION DENIED by smart gate: {description}. "
                f"The action gate determined this violates configured rules. "
                f"Use an approved tool/channel instead."
            )
        # decision == "escalate"
        return _escalate_to_human(
            tool_name, tool_args, description, classification,
            profile, config, session_id, task_id,
        )

    # Unknown mode — fail safe
    return None


def _escalate_to_human(
    tool_name: str,
    tool_args: dict,
    description: str,
    classification: str,
    profile: str,
    config: dict,
    session_id: Optional[str],
    task_id: Optional[str],
) -> str:
    """Queue the action for human approval and wait for response.

    Uses the file-based proposal system (~/.hermes/proposals/pending/)
    which the HumanlessAI app already polls. Also writes to SQLite for
    Telegram fallback and audit trail.
    """
    timeout = config.get("timeout", 300)
    now = int(time.time())
    action_id = f"action-gate-{now}-{profile}-{tool_name}"

    # Write to SQLite queue (audit trail + Telegram fallback)
    db_id = 0
    try:
        conn = _ensure_queue_db()
        cursor = conn.execute(
            """INSERT INTO pending_actions
               (profile, tool_name, tool_args, description, classification,
                status, created_at, expires_at, session_id, task_id)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (
                profile, tool_name, json.dumps(tool_args, default=str),
                description, classification, now, now + timeout,
                session_id, task_id,
            ),
        )
        conn.commit()
        db_id = cursor.lastrowid or 0
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Write YAML proposal file for HumanlessAI app
    proposals_dir = Path.home() / ".hermes" / "proposals" / "pending"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    proposal_file = proposals_dir / f"{action_id}.yaml"

    args_preview = json.dumps(tool_args, default=str)[:1000]
    proposal_content = (
        f"id: {action_id}\n"
        f"type: action_gate\n"
        f"title: \"{description}\"\n"
        f"profile: {profile}\n"
        f"tool: {tool_name}\n"
        f"args: |\n"
        f"  {args_preview}\n"
        f"classification: {classification}\n"
        f"board: {os.environ.get('HERMES_KANBAN_BOARD', 'default')}\n"
        f"assignee: {profile}\n"
        f"reason: |\n"
        f"  Agent {profile} wants to execute a gated action.\n"
        f"  Tool: {tool_name}\n"
        f"  Action: {description}\n"
        f"task_id: {task_id or ''}\n"
        f"session_id: {session_id or ''}\n"
        f"db_id: {db_id}\n"
        f"created_at: {now}\n"
        f"expires_at: {now + timeout}\n"
    )
    try:
        proposal_file.write_text(proposal_content)
    except Exception as e:
        _log.error("Failed to write proposal file: %s", e)

    # Also send Telegram notification as fallback
    _notify_human(db_id, tool_name, tool_args, description, profile, config)

    # Poll for decision — check both file system and SQLite
    approved_dir = Path.home() / ".hermes" / "proposals" / "approved"
    denied_dir = Path.home() / ".hermes" / "proposals" / "denied"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        time.sleep(2)

        # Check file-based decision (HumanlessAI app)
        approved_file = approved_dir / f"{action_id}.yaml"
        denied_file = denied_dir / f"{action_id}.yaml"

        if approved_file.exists():
            _log.info("Action gate: APPROVED via app — %s (id=%s)", description, action_id)
            # Update SQLite for consistency
            _update_db_decision(db_id, "approve", "app")
            return None  # proceed

        if denied_file.exists():
            _log.info("Action gate: DENIED via app — %s (id=%s)", description, action_id)
            # Check if deny includes disable_tool
            try:
                content = denied_file.read_text()
                if "disable_tool" in content:
                    _add_blocked_tool(profile, tool_name)
            except Exception:
                pass
            _update_db_decision(db_id, "deny", "app")
            return (
                f"ACTION DENIED by owner: {description}. "
                f"The action was escalated and denied. "
                f"Do not retry this action."
            )

        # Check SQLite decision (Telegram /approve command)
        try:
            conn = _ensure_queue_db()
            row = conn.execute(
                "SELECT status, decision FROM pending_actions WHERE id = ?",
                (db_id,),
            ).fetchone()
            conn.close()
            if row and row["status"] != "pending":
                # Clean up the proposal file
                try:
                    proposal_file.unlink(missing_ok=True)
                except Exception:
                    pass
                if row["decision"] == "approve":
                    _log.info("Action gate: APPROVED by human — %s (db_id=%d)", description, db_id)
                    return None
                else:
                    return (
                        f"ACTION DENIED by owner: {description}. "
                        f"The action was escalated and denied. "
                        f"Do not retry this action."
                    )
        except Exception:
            pass

    # Timeout — clean up and deny
    try:
        proposal_file.unlink(missing_ok=True)
    except Exception:
        pass
    _update_db_decision(db_id, "timeout", "system")

    return (
        f"ACTION TIMED OUT: {description}. "
        f"No approval received within {timeout}s. Action denied. "
        f"Block yourself and wait for owner input."
    )


def _update_db_decision(db_id: int, decision: str, decided_by: str) -> None:
    """Update the SQLite record with the decision."""
    if not db_id:
        return
    try:
        conn = _ensure_queue_db()
        conn.execute(
            """UPDATE pending_actions
               SET status='decided', decision=?, decided_by=?, decided_at=?
               WHERE id=?""",
            (decision, decided_by, int(time.time()), db_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# External approval API (called by Telegram bot / CLI / app)
# ---------------------------------------------------------------------------

def approve_action(action_id: int, decided_by: str = "owner") -> bool:
    """Approve a pending action. Returns True if found and updated."""
    try:
        conn = _ensure_queue_db()
        conn.execute(
            """UPDATE pending_actions
               SET status='decided', decision='approve',
                   decided_by=?, decided_at=?
               WHERE id=? AND status='pending'""",
            (decided_by, int(time.time()), action_id),
        )
        conn.commit()
        changed = conn.total_changes > 0
        conn.close()
        return changed
    except Exception as e:
        _log.error("approve_action failed: %s", e)
        return False


def deny_action(
    action_id: int,
    decided_by: str = "owner",
    disable_tool: bool = False,
) -> bool:
    """Deny a pending action. Optionally disable the tool for the profile."""
    try:
        conn = _ensure_queue_db()
        conn.execute(
            """UPDATE pending_actions
               SET status='decided', decision='deny',
                   decided_by=?, decided_at=?
               WHERE id=? AND status='pending'""",
            (decided_by, int(time.time()), action_id),
        )
        conn.commit()
        changed = conn.total_changes > 0

        if disable_tool and changed:
            # Get the tool name and profile to add to blocked list
            row = conn.execute(
                "SELECT profile, tool_name FROM pending_actions WHERE id=?",
                (action_id,),
            ).fetchone()
            if row:
                _add_blocked_tool(row["profile"], row["tool_name"])

        conn.close()
        return changed
    except Exception as e:
        _log.error("deny_action failed: %s", e)
        return False


def _add_blocked_tool(profile: str, tool_name: str) -> None:
    """Add a tool to the profile's blocked list in config."""
    try:
        from hermes_constants import get_hermes_home
        import yaml

        # Determine config path for the profile
        hermes_home = get_hermes_home()
        if profile == "default":
            config_path = hermes_home / "config.yaml"
        else:
            config_path = hermes_home / "profiles" / profile / "config.yaml"

        if not config_path.exists():
            return

        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}

        gate = cfg.setdefault("action_gate", {})
        rules = gate.setdefault("rules", {})
        blocked = rules.setdefault("blocked_tools", [])
        if tool_name not in blocked:
            blocked.append(tool_name)

        with open(config_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

        _log.info("Added %s to blocked_tools for profile %s", tool_name, profile)
    except Exception as e:
        _log.error("Failed to add blocked tool: %s", e)

