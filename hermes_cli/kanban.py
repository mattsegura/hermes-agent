"""CLI for the Hermes Kanban board — ``hermes kanban …`` subcommand.

Exposes the full Kanban command surface documented in the design spec
(``docs/hermes-kanban-v1-spec.pdf``).  All DB work is delegated to
``kanban_db``.  This module adds:

  * Argparse subcommand construction (``build_parser``).
  * Argument dispatch (``kanban_command``).
  * Output formatting (plain text + ``--json``).
  * A short shared helper that parses a single slash-style string
    (used by ``/kanban …`` in CLI and gateway) and forwards it to the
    argparse surface.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_swarm as ks
from hermes_cli.profiles import get_active_profile_name, get_profile_dir, seed_profile_skills


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    "todo":     "◻",
    "ready":    "▶",
    "running":  "●",
    "watching": "◌",
    "scheduled":"⏱",
    "blocked":  "⊘",
    "done":     "✓",
    "archived": "—",
}


def _fmt_ts(ts: Optional[int]) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _fmt_task_line(t: kb.Task) -> str:
    icon = _STATUS_ICONS.get(t.status, "?")
    assignee = t.assignee or "(unassigned)"
    tenant = f" [{t.tenant}]" if t.tenant else ""
    return f"{icon} {t.id}  {t.status:8s}  {assignee:20s}{tenant}  {t.title}"


def _task_to_dict(t: kb.Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "body": t.body,
        "assignee": t.assignee,
        "status": t.status,
        "priority": t.priority,
        "tenant": t.tenant,
        "workspace_kind": t.workspace_kind,
        "workspace_path": t.workspace_path,
        "branch_name": t.branch_name,
        "created_by": t.created_by,
        "created_at": t.created_at,
        "started_at": t.started_at,
        "completed_at": t.completed_at,
        "result": t.result,
        "skills": list(t.skills) if t.skills else [],
        "max_retries": t.max_retries,
        "session_id": t.session_id,
        "workflow_template_id": t.workflow_template_id,
        "current_step_key": t.current_step_key,
        "goal_id": t.goal_id,
        "workstream_id": t.workstream_id,
        "stage_key": t.stage_key,
        "action_key": t.action_key,
        "funnel_data": t.funnel_data,
    }


def _run_state_kwargs(args: argparse.Namespace) -> Optional[dict[str, str]]:
    st = getattr(args, "state_type", None)
    sn = getattr(args, "state_name", None)
    if (st is None) != (sn is None):
        return None
    if st is None:
        return {}
    return {"state_type": st, "state_name": sn}


def _parse_json_object_flag(raw: Optional[str], flag_name: str) -> tuple[Optional[dict], Optional[str]]:
    if raw is None:
        return None, None
    text = str(raw).strip()
    if not text:
        return None, None
    if text.startswith("@"):
        try:
            text = Path(text[1:]).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            return None, f"{flag_name}: could not read {text[1:]!r}: {exc}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"{flag_name}: {exc}"
    if not isinstance(parsed, dict):
        return None, f"{flag_name}: must be a JSON object"
    return parsed, None


def _current_user_label() -> str:
    return (
        os.environ.get("HERMES_PROFILE")
        or get_active_profile_name()
        or os.environ.get("USER")
        or "user"
    )


_OWNER_LAUNCH_INTAKE_PROFILES = {"default", "personal-assistant"}


def _is_owner_launch_intake_profile() -> bool:
    profile = str(os.environ.get("HERMES_PROFILE") or get_active_profile_name() or "default").strip()
    return profile in _OWNER_LAUNCH_INTAKE_PROFILES


def _owner_approval_authority_confirmed() -> bool:
    if (
        os.environ.get("HERMES_TEST_OWNER_APPROVAL_AUTHORITY") == "1"
        and os.environ.get("PYTEST_CURRENT_TEST")
    ):
        return True
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _parse_text_or_file_flag(raw: Optional[str], flag_name: str) -> tuple[Optional[str], Optional[str]]:
    if raw is None:
        return None, None
    text = str(raw).strip()
    if not text:
        return None, None
    if text.startswith("@"):
        try:
            text = Path(text[1:]).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            return None, f"{flag_name}: could not read {text[1:]!r}: {exc}"
    return text, None


def _parse_workspace_flag(value: str) -> tuple[str, Optional[str]]:
    """Parse ``--workspace`` into ``(kind, path|None)``.

    Accepts: ``scratch``, ``worktree``, ``worktree:<path>``, ``dir:<path>``.
    """
    if not value:
        return ("scratch", None)
    v = value.strip()
    if v in {"scratch", "worktree"}:
        return (v, None)
    for prefix, kind in (("dir:", "dir"), ("worktree:", "worktree")):
        if not v.startswith(prefix):
            continue
        path = v[len(prefix):].strip()
        if not path:
            raise argparse.ArgumentTypeError(
                f"--workspace {prefix} requires a path after the colon"
            )
        return (kind, os.path.expanduser(path))
    raise argparse.ArgumentTypeError(
        f"unknown --workspace value {value!r}: use scratch, worktree, "
        "worktree:<path>, or dir:<path>"
    )


def _parse_branch_flag(value: Optional[str]) -> Optional[str]:
    """Normalize an optional branch name from ``kanban create --branch``."""
    if value is None:
        return None
    branch = value.strip()
    if not branch:
        raise argparse.ArgumentTypeError("--branch requires a non-empty name")
    if branch.startswith("-"):
        raise argparse.ArgumentTypeError("--branch must not start with '-'")
    if any(ch.isspace() for ch in branch):
        raise argparse.ArgumentTypeError("--branch must not contain whitespace")
    return branch


def _check_dispatcher_presence() -> tuple[bool, str]:
    """Return ``(running, message)``.

    - ``running=True``: a gateway is alive for this HERMES_HOME and its
      config has ``kanban.dispatch_in_gateway`` on (default). Message
      is a short status line.
    - ``running=False``: either no gateway is running, or the gateway
      is running but the config flag is off. Message is human guidance
      explaining the next step.

    Used by ``hermes kanban create`` (and callers) to warn when a task
    will sit in ``ready`` because nothing is there to pick it up.
    Defensive against import failures and config-read errors — if the
    probe itself errors, we return ``(True, "")`` so we don't spam
    false warnings (better to miss a warning than to cry wolf).
    """
    try:
        from gateway.status import get_running_pid  # type: ignore
    except Exception:
        return (True, "")  # can't probe — silent
    try:
        pid = get_running_pid()
    except Exception:
        return (True, "")  # probe errored — silent

    # Even if the gateway is up, dispatch_in_gateway may be off.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        dispatch_on = bool(cfg.get("kanban", {}).get("dispatch_in_gateway", True))
    except Exception:
        dispatch_on = True  # can't tell — assume default

    if pid and dispatch_on:
        return (True, f"gateway pid={pid}, dispatch enabled")
    if pid and not dispatch_on:
        return (
            False,
            "Gateway is running but kanban.dispatch_in_gateway=false in "
            "config.yaml — the task will sit in 'ready' until you flip it "
            "back on and restart the gateway, OR run the legacy "
            "standalone daemon (`hermes kanban daemon --force`)."
        )
    return (
        False,
        "No gateway is running — the task will sit in 'ready' until you "
        "start it. Run:\n"
        "    hermes gateway start\n"
        "The gateway hosts an embedded dispatcher (tick interval 60s by "
        "default); your task will be picked up on the next tick after "
        "the gateway comes up."
    )


# ---------------------------------------------------------------------------
# Argparse builder
# ---------------------------------------------------------------------------

def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach the ``kanban`` subcommand tree under an existing subparsers.

    Returns the top-level ``kanban`` parser so caller can ``set_defaults``.
    """
    kanban_parser = parent_subparsers.add_parser(
        "kanban",
        help="Multi-profile collaboration board (tasks, links, comments)",
        description=(
            "Durable SQLite-backed task board shared across Hermes profiles. "
            "Tasks are claimed atomically, can depend on other tasks, and "
            "are executed by a named profile in an isolated workspace. "
            "See https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban "
            "or docs/hermes-kanban-v1-spec.pdf for the full design."
        ),
    )
    # --- global --board flag ---
    # Applies to every subcommand below. When set, scopes all reads and
    # writes to that board's DB. When omitted, resolves via the
    # HERMES_KANBAN_BOARD env var, then the persisted current-board
    # file, then "default". See kanban_db.get_current_board().
    kanban_parser.add_argument(
        "--board",
        default=None,
        metavar="<slug>",
        help=(
            "Board slug to operate on. Defaults to the current board "
            "(set via `hermes kanban boards switch <slug>` or the "
            "HERMES_KANBAN_BOARD env var). Use `hermes kanban boards list` "
            "to see all boards."
        ),
    )
    # --- global --debug flag ---
    # By default the kanban CLI catches unexpected errors at the top level and
    # prints an operator-friendly message + hint (no raw traceback). --debug
    # (or the HERMES_DEBUG env var) re-raises the full traceback for diagnosis.
    kanban_parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Show the full Python traceback on unexpected errors (for diagnosis).",
    )
    sub = kanban_parser.add_subparsers(dest="kanban_action")

    # --- init ---
    sub.add_parser("init", help="Create kanban.db if missing (idempotent)")

    # --- boards (new in v2: multi-project support) ---
    p_boards = sub.add_parser(
        "boards",
        help="Manage kanban boards (one board per project / workstream)",
        description=(
            "Boards let you separate unrelated streams of work "
            "(projects, repos, domains) into isolated queues. Each "
            "board has its own DB, workspaces directory, and dispatcher "
            "loop — tasks on one board cannot collide with tasks on "
            "another. The first board is 'default' and always exists."
        ),
    )
    boards_sub = p_boards.add_subparsers(dest="boards_action")

    b_list = boards_sub.add_parser(
        "list", aliases=["ls"],
        help="List all boards with task counts",
    )
    b_list.add_argument("--json", action="store_true")
    b_list.add_argument("--all", action="store_true",
                        help="Include archived boards too")

    b_create = boards_sub.add_parser(
        "create", aliases=["new"],
        help="Create a new board",
    )
    b_create.add_argument("slug",
                          help="Board slug (kebab-case, e.g. atm10-server)")
    b_create.add_argument("--name", default=None,
                          help="Human-readable display name (defaults to Title Case of slug)")
    b_create.add_argument("--description", default=None,
                          help="Optional description")
    b_create.add_argument("--icon", default=None,
                          help="Optional emoji or single-character icon for the dashboard")
    b_create.add_argument("--color", default=None,
                          help="Optional hex color (e.g. '#8b5cf6') for the dashboard")
    b_create.add_argument("--switch", action="store_true",
                          help="Switch to the new board after creating it")
    b_create.add_argument("--default-workdir", default=None,
                          help="Default workspace path for tasks created on this board")
    b_create.add_argument("--runtime", choices=sorted(kb.VALID_RUNTIME_MODES), default=None,
                          help="Board runtime mode (default: contract runtime.mode, else goal; kernel is isolated/plain)")
    b_create.add_argument("--objective", default=None,
                          help="Objective statement for goal/company runtime boards")
    b_create.add_argument("--success", action="append", default=None,
                          help="Success criterion for the board objective; repeatable")
    b_create.add_argument("--failure", action="append", default=None,
                          help="Failure signal for the board objective; repeatable")
    b_create.add_argument("--constraint", action="append", default=None,
                          help="Objective constraint; repeatable")
    b_create.add_argument("--dispatcher-profile", default=None,
                          help="Profile allowed to dispatch this board")
    b_create.add_argument("--ceo-profile", default=None,
                          help="CEO/orchestrator profile metadata for this board")
    b_create.add_argument("--optimizer-profile", default=None,
                          help="Optimizer profile metadata for this board")
    b_create.add_argument("--worker-profile", default=None,
                          help="Default worker profile metadata for this board")
    b_create.add_argument("--workflow", default=None,
                          help="JSON object or @file path defining semantic workflow stages")
    b_create.add_argument("--contract", default=None,
                          help="JSON object or @file path defining objective/runtime/workflow contract")

    b_rm = boards_sub.add_parser(
        "rm", aliases=["remove", "delete"],
        help="Archive (default) or delete a board",
    )
    b_rm.add_argument("slug")
    b_rm.add_argument("--delete", action="store_true",
                      help="Hard-delete the board directory instead of archiving it. "
                           "Default is to move it to boards/_archived/ so it's recoverable.")

    b_switch = boards_sub.add_parser(
        "switch", aliases=["use"],
        help="Set the active board for subsequent CLI calls",
    )
    b_switch.add_argument("slug")

    boards_sub.add_parser(
        "show", aliases=["current"],
        help="Print the currently-active board slug",
    )

    b_rename = boards_sub.add_parser(
        "rename",
        help="Change a board's human-readable display name (slug is immutable)",
    )
    b_rename.add_argument("slug")
    b_rename.add_argument("name", help="New display name")

    b_set_wd = boards_sub.add_parser(
        "set-default-workdir",
        help="Set the default workspace path for tasks on a board",
    )
    b_set_wd.add_argument("slug")
    b_set_wd.add_argument("path", nargs="?", default=None,
                          help="Absolute path to use as default workdir. Omit to clear.")

    b_workflow = boards_sub.add_parser(
        "workflow",
        help="Show or update a board's semantic workflow schema",
    )
    b_workflow_sub = b_workflow.add_subparsers(dest="workflow_action")
    bw_show = b_workflow_sub.add_parser("show", help="Print board workflow JSON")
    bw_show.add_argument("slug", nargs="?", default=None,
                         help="Board slug (defaults to current board)")
    bw_show.add_argument("--json", action="store_true")
    bw_set = b_workflow_sub.add_parser("set", help="Set workflow from JSON or @file")
    bw_set.add_argument("slug")
    bw_set.add_argument("workflow", help="JSON object or @/path/to/workflow.json")
    bw_clear = b_workflow_sub.add_parser("clear", help="Clear workflow schema")
    bw_clear.add_argument("slug")

    b_contract = boards_sub.add_parser(
        "contract",
        help="Review, activate, and amend a board business contract",
    )
    b_contract_sub = b_contract.add_subparsers(dest="contract_action")
    bc_status = b_contract_sub.add_parser("status", help="Show launch readiness")
    bc_status.add_argument("slug", nargs="?", default=None,
                           help="Board slug (defaults to current board)")
    bc_status.add_argument("--json", action="store_true")
    bc_review = b_contract_sub.add_parser("review", help="Review/store a draft launch contract")
    bc_review.add_argument("slug", help="Board slug")
    bc_review.add_argument("--rough-goal", default=None,
                           help="Owner's rough business goal or launch request")
    bc_review.add_argument("--contract", default=None,
                           help="JSON object or @file path defining the business contract")
    bc_review.add_argument("--intake-answers", default=None,
                           help="Owner answers to generated intake questions, or @file")
    bc_review.add_argument("--create", action="store_true",
                           help="Create the board if it does not exist")
    bc_review.add_argument("--approve", action="store_true",
                           help="Activate only if readiness validation passes")
    bc_review.add_argument("--approval-token", default=None,
                           help="One-time approval token from contract approval-token")
    bc_review.add_argument("--approved-by", default=None,
                           help="Legacy approver metadata; use contract approval-token for activation")
    bc_review.add_argument("--approval-evidence", default=None,
                           help="Legacy approval evidence string or JSON object/@file")
    bc_review.add_argument("--approval-reason", default=None,
                           help="Optional approval rationale")
    bc_review.add_argument("--name", default=None)
    bc_review.add_argument("--description", default=None)
    bc_review.add_argument(
        "--operator-override",
        action="store_true",
        help=(
            "Operator-only bypass for importing a complete direct contract without "
            "saved owner launch intake"
        ),
    )
    bc_review.add_argument("--json", action="store_true")
    bc_token = b_contract_sub.add_parser(
        "approval-token",
        help="Issue a one-time owner approval token for an exact contract/version",
    )
    bc_token.add_argument("slug", help="Board slug")
    bc_token.add_argument("--rough-goal", default=None,
                          help="Owner's rough business goal or launch request")
    bc_token.add_argument("--contract", default=None,
                          help="JSON object or @file path defining the business contract")
    bc_token.add_argument("--amendment-id", default=None,
                          help="Pending amendment id when approving an amendment")
    bc_token.add_argument("--approved-by", required=True,
                          help="Explicit owner/approver identity")
    bc_token.add_argument("--approval-evidence", required=True,
                          help="Owner approval evidence string or JSON object/@file")
    bc_token.add_argument("--approval-reason", default=None,
                          help="Optional approval rationale")
    bc_token.add_argument("--ttl-seconds", type=int, default=None,
                          help="Token lifetime in seconds (default: 900)")
    bc_token.add_argument(
        "--operator-override",
        action="store_true",
        help=(
            "Operator-only bypass for issuing a token for a complete direct "
            "contract without saved owner launch intake"
        ),
    )
    bc_token.add_argument("--json", action="store_true")
    bc_propose = b_contract_sub.add_parser("propose", help="Propose a contract amendment")
    bc_propose.add_argument("slug", help="Board slug")
    bc_propose.add_argument("patch", help="JSON object or @file path patch")
    bc_propose.add_argument("--reason", required=True, help="Why the contract needs to change")
    bc_propose.add_argument("--risk", choices=["low", "medium", "high"], default="medium")
    bc_propose.add_argument("--json", action="store_true")
    bc_apply = b_contract_sub.add_parser("apply", help="Apply a pending contract amendment")
    bc_apply.add_argument("slug", help="Board slug")
    bc_apply.add_argument("amendment_id", help="Pending amendment id")
    bc_apply.add_argument("--approval-token", default=None,
                          help="One-time approval token from contract approval-token")
    bc_apply.add_argument("--approved-by", default=None,
                          help="Legacy approver metadata; use contract approval-token for activation")
    bc_apply.add_argument("--approval-evidence", default=None,
                          help="Legacy owner approval evidence string or JSON object/@file")
    bc_apply.add_argument("--force", action="store_true",
                          help="Deprecated; non-ready amendments are rejected")
    bc_apply.add_argument("--no-activate", action="store_true",
                          help="Do not set launch_phase=active after apply")
    bc_apply.add_argument("--json", action="store_true")

    # --- P5 contract-amendment loop (structural, owner-gated, versioned) ---
    bc_amend = b_contract_sub.add_parser(
        "amendment",
        help="P5 structural contract-amendment loop (propose/approve/validate/mint)",
    )
    bc_amend_sub = bc_amend.add_subparsers(dest="amendment_action")

    am_status = bc_amend_sub.add_parser("status", help="Summarize a board's amendments")
    am_status.add_argument("--board", default=None, help="Board slug (defaults to current)")
    am_status.add_argument("--json", action="store_true")

    am_list = bc_amend_sub.add_parser("list", help="List contract amendments")
    am_list.add_argument("--board", default=None, help="Board slug (defaults to current)")
    am_list.add_argument("--status", default=None, help="Filter by amendment status")
    am_list.add_argument("--json", action="store_true")

    am_show = bc_amend_sub.add_parser("show", help="Show one amendment")
    am_show.add_argument("amendment_id", help="Amendment id")
    am_show.add_argument("--board", default=None, help="Board slug (defaults to current)")
    am_show.add_argument("--json", action="store_true")

    am_propose = bc_amend_sub.add_parser("propose", help="Draft a structural amendment")
    am_propose.add_argument("--board", default=None, help="Board slug (defaults to current)")
    am_propose.add_argument("--origin", default="owner",
                            choices=["optimizer", "sensor", "ceo", "owner"])
    am_propose.add_argument("--rationale", required=True, help="Why the contract must change")
    am_propose.add_argument("--contract", default=None,
                            help="Full proposed contract JSON or @file")
    am_propose.add_argument("--diff", default=None,
                            help="Structured diff/patch JSON or @file over the current contract")
    am_propose.add_argument("--required-inputs", default=None,
                            help="JSON list of owner-input field specs or @file")
    am_propose.add_argument("--json", action="store_true")

    am_inputs = bc_amend_sub.add_parser("submit-inputs", help="Supply required owner inputs")
    am_inputs.add_argument("amendment_id", help="Amendment id")
    am_inputs.add_argument("inputs", help="JSON object of field->value or @file")
    am_inputs.add_argument("--board", default=None, help="Board slug (defaults to current)")
    am_inputs.add_argument("--json", action="store_true")

    am_approve = bc_amend_sub.add_parser("approve", help="Owner-approve with a launch token")
    am_approve.add_argument("amendment_id", help="Amendment id")
    am_approve.add_argument("--token", required=True,
                            help="One-time owner approval token (contract approval-token --amendment-id)")
    am_approve.add_argument("--approved-by", default=None, help="Approver identity")
    am_approve.add_argument("--approval-evidence", default=None,
                            help="Owner approval evidence string/JSON/@file")
    am_approve.add_argument("--board", default=None, help="Board slug (defaults to current)")
    am_approve.add_argument("--json", action="store_true")

    am_validate = bc_amend_sub.add_parser("validate", help="Auto-validate an approved amendment")
    am_validate.add_argument("amendment_id", help="Amendment id")
    am_validate.add_argument("--board", default=None, help="Board slug (defaults to current)")
    am_validate.add_argument("--json", action="store_true")

    am_mint = bc_amend_sub.add_parser("mint", help="Mint+activate a validated amendment (CAS)")
    am_mint.add_argument("amendment_id", help="Amendment id")
    am_mint.add_argument("--board", default=None, help="Board slug (defaults to current)")
    am_mint.add_argument("--json", action="store_true")

    am_reject = bc_amend_sub.add_parser("reject", help="Reject an in-flight amendment")
    am_reject.add_argument("amendment_id", help="Amendment id")
    am_reject.add_argument("--reason", default=None, help="Why it is rejected")
    am_reject.add_argument("--board", default=None, help="Board slug (defaults to current)")
    am_reject.add_argument("--json", action="store_true")

    # --- orphan cleanup + role binding (board↔profile fragmentation guard) ---
    b_quar = boards_sub.add_parser(
        "quarantine-orphans",
        help="Find/quarantine orphan boards (a kanban.db with no board.json)",
    )
    b_quar.add_argument("--apply", action="store_true",
                        help="Actually act (default is a dry-run listing)")
    b_quar.add_argument("--adopt", action="store_true",
                        help="Stamp a board.json onto each orphan instead of quarantining it")
    b_bind = boards_sub.add_parser(
        "bind-roles",
        help="Bind every profile a board's contract names to that ONE board",
    )
    b_bind.add_argument("slug", nargs="?", default=None,
                        help="Board slug (defaults to current board)")

    # --- P6 conversational CEO steering channel ---
    p_steer = sub.add_parser(
        "steer",
        help="Converse with the CEO to deepen (launch) or evolve (runtime) a board contract",
    )
    steer_sub = p_steer.add_subparsers(dest="steer_action")

    st_open = steer_sub.add_parser("open", help="Open a steering session")
    st_open.add_argument("--board", default=None, help="Board slug (defaults to current)")
    st_open.add_argument("--mode", default="runtime_evolution",
                         choices=["launch_buildout", "runtime_evolution"])
    st_open.add_argument("--title", default=None, help="Optional session title")
    st_open.add_argument("--amendment-id", default=None,
                         help="Thread the session onto an existing P5 amendment to discuss it")
    st_open.add_argument("--json", action="store_true")

    st_send = steer_sub.add_parser("send", help="Send an owner message and print the CEO reply")
    st_send.add_argument("session_id", help="Steering session id")
    st_send.add_argument("message", help="Your message to the CEO")
    st_send.add_argument("--board", default=None, help="Board slug (defaults to current)")
    st_send.add_argument("--json", action="store_true")

    st_list = steer_sub.add_parser("list", help="List steering sessions")
    st_list.add_argument("--board", default=None, help="Board slug (defaults to current)")
    st_list.add_argument("--status", default=None, help="Filter by status (open|closed)")
    st_list.add_argument("--json", action="store_true")

    st_show = steer_sub.add_parser("show", help="Show a session with its full message log")
    st_show.add_argument("session_id", help="Steering session id")
    st_show.add_argument("--board", default=None, help="Board slug (defaults to current)")
    st_show.add_argument("--json", action="store_true")

    st_close = steer_sub.add_parser("close", help="Close a steering session")
    st_close.add_argument("session_id", help="Steering session id")
    st_close.add_argument("--board", default=None, help="Board slug (defaults to current)")
    st_close.add_argument("--json", action="store_true")

    st_reflect = steer_sub.add_parser(
        "reflect", help="Append a system message reflecting a P5 amendment's current state",
    )
    st_reflect.add_argument("session_id", help="Steering session id")
    st_reflect.add_argument("amendment_id", help="Amendment id to reflect")
    st_reflect.add_argument("--board", default=None, help="Board slug (defaults to current)")
    st_reflect.add_argument("--json", action="store_true")

    # --- create ---
    p_create = sub.add_parser("create", help="Create a new task")
    p_create.add_argument("title", help="Task title")
    p_create.add_argument("--body", default=None, help="Optional opening post")
    p_create.add_argument("--assignee", default=None, help="Profile name to assign")
    p_create.add_argument("--parent", action="append", default=[],
                          help="Parent task id (repeatable)")
    p_create.add_argument("--workspace", default="scratch",
                          help="scratch | worktree | worktree:<path> | dir:<path> "
                               "(default: scratch)")
    p_create.add_argument("--branch", default=None,
                          help="Branch name for worktree tasks, e.g. wt/t6-wire")
    p_create.add_argument("--tenant", default=None, help="Tenant namespace")
    p_create.add_argument("--priority", type=int, default=0, help="Priority tiebreaker")
    p_create.add_argument("--triage", action="store_true",
                          help="Park in triage — a specifier will flesh out the spec and promote to todo")
    p_create.add_argument("--idempotency-key", default=None,
                          help="Dedup key. If a non-archived task with this key exists, "
                               "its id is returned instead of creating a duplicate.")
    p_create.add_argument("--max-runtime", default=None,
                          help="Per-task runtime cap. Accepts seconds (300) or "
                               "durations (90s, 30m, 2h, 1d). When exceeded, "
                               "the dispatcher SIGTERMs (then SIGKILLs) the worker "
                               "and re-queues the task.")
    p_create.add_argument("--created-by", default="user",
                          help="Author name recorded on the task (default: user)")
    p_create.add_argument("--skill", action="append", default=[], dest="skills",
                          help="Skill to force-load into the worker "
                               "(repeatable). Appended to the built-in "
                               "kanban-worker skill. Example: "
                               "--skill translation --skill github-code-review")
    p_create.add_argument("--goal", default=None,
                          help="Semantic funnel goal id for optimizer/UI read-models")
    p_create.add_argument("--workstream", default=None,
                          help="Semantic funnel workstream id under the goal")
    p_create.add_argument("--stage", default=None,
                          help="Semantic funnel stage key for the card")
    p_create.add_argument("--action", default=None,
                          help="Semantic funnel action key within the stage")
    p_create.add_argument("--funnel-data", default=None,
                          help="JSON object with structured funnel facts")
    p_create.add_argument("--max-retries", type=int, default=None,
                          metavar="N",
                          help="Per-task override for the consecutive-failure "
                               "circuit breaker. Trip on the Nth failure — "
                               "e.g. --max-retries 1 blocks on the first "
                               "failure (no retries), --max-retries 3 allows "
                               "two retries. Omit to use the dispatcher's "
                               "kanban.failure_limit config "
                               f"(default {kb.DEFAULT_FAILURE_LIMIT}).")
    p_create.add_argument("--initial-status",
                          choices=sorted(kb.VALID_INITIAL_STATUSES),
                          default=None,
                          help="Initial card status. Use 'blocked' for cards "
                               "that require immediate human ops (R3 gate) "
                               "to skip the brief running-to-blocked transition. "
                               "Omitted root tasks on kernel boards land in todo.")
    p_create.add_argument("--json", action="store_true", help="Emit JSON output")

    # --- swarm ---
    p_swarm = sub.add_parser(
        "swarm",
        help="Create a Kanban Swarm v1 graph (parallel workers → verifier → synthesizer)",
    )
    p_swarm.add_argument("goal", help="Swarm goal / final outcome")
    p_swarm.add_argument(
        "--worker",
        action="append",
        default=[],
        metavar="PROFILE:TITLE[:SKILL,SKILL]",
        help="Parallel worker card (repeatable)",
    )
    p_swarm.add_argument("--verifier", required=True, help="Verifier profile")
    p_swarm.add_argument("--synthesizer", required=True, help="Synthesizer/writer profile")
    p_swarm.add_argument("--tenant", default=None, help="Tenant namespace")
    p_swarm.add_argument("--priority", type=int, default=0, help="Priority tiebreaker")
    p_swarm.add_argument("--created-by", default=None, help="Creator/anchor profile")
    p_swarm.add_argument("--idempotency-key", default=None, help="Dedup key for the root card")
    p_swarm.add_argument("--json", action="store_true", help="Emit JSON output")

    # --- list ---
    p_list = sub.add_parser("list", aliases=["ls"], help="List tasks")
    p_list.add_argument("--mine", action="store_true",
                        help="Filter by $HERMES_PROFILE as assignee")
    p_list.add_argument("--assignee", default=None)
    p_list.add_argument("--status", default=None,
                        choices=sorted(kb.VALID_STATUSES))
    p_list.add_argument("--tenant", default=None)
    p_list.add_argument("--session", default=None,
                        help="Filter by originating chat/agent session id "
                             "(set on tasks created from inside an ACP loop)")
    p_list.add_argument("--archived", action="store_true",
                        help="Include archived tasks")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument(
        "--sort",
        default=None,
        choices=sorted(kb.VALID_SORT_ORDERS.keys()),
        help="Sort order for listed tasks (default: priority)",
    )
    p_list.add_argument(
        "--workflow-template-id",
        default=None,
        metavar="ID",
        help="Restrict to tasks with this workflow_template_id",
    )
    p_list.add_argument(
        "--step-key",
        default=None,
        dest="current_step_key",
        metavar="KEY",
        help="Restrict to tasks with this current_step_key",
    )

    # --- show ---
    p_show = sub.add_parser("show", help="Show a task with comments + events")
    p_show.add_argument("task_id")
    p_show.add_argument("--json", action="store_true")
    p_show.add_argument(
        "--state-type",
        choices=("status", "outcome"),
        default=None,
        help="With --state-name: filter listed runs by task_runs column",
    )
    p_show.add_argument(
        "--state-name",
        default=None,
        metavar="VALUE",
        help="With --state-type: keep runs whose column equals this value",
    )

    # --- assign ---
    p_assign = sub.add_parser("assign", help="Assign or reassign a task")
    p_assign.add_argument("task_id")
    p_assign.add_argument("profile", help="Profile name (or 'none' to unassign)")

    # --- reclaim / reassign (recovery) ---
    p_reclaim = sub.add_parser(
        "reclaim",
        help="Release an active worker claim on a running task",
    )
    p_reclaim.add_argument("task_id")
    p_reclaim.add_argument(
        "--reason", default=None,
        help="Human-readable reason (recorded on the reclaimed event)",
    )

    p_reassign = sub.add_parser(
        "reassign",
        help="Reassign a task to a different profile, optionally reclaiming first",
    )
    p_reassign.add_argument("task_id")
    p_reassign.add_argument(
        "profile",
        help="New profile name (or 'none' to unassign)",
    )
    p_reassign.add_argument(
        "--reclaim", action="store_true",
        help="Release any active claim before reassigning (required if task is running)",
    )
    p_reassign.add_argument(
        "--reason", default=None,
        help="Human-readable reason (recorded on the reclaimed event)",
    )

    # --- diagnostics (board-wide health) ---
    p_diag = sub.add_parser(
        "diagnostics",
        aliases=["diag"],
        help="List active diagnostics on the current board",
    )
    p_diag.add_argument(
        "--severity",
        choices=["warning", "error", "critical"],
        default=None,
        help="Only show diagnostics at or above this severity",
    )
    p_diag.add_argument(
        "--task",
        default=None,
        help="Only show diagnostics for one task id",
    )
    p_diag.add_argument(
        "--json", action="store_true",
        help="Emit JSON (structured) instead of the default human table",
    )

    # --- link / unlink ---
    p_link = sub.add_parser("link", help="Add a parent->child dependency")
    p_link.add_argument("parent_id")
    p_link.add_argument("child_id")
    p_unlink = sub.add_parser("unlink", help="Remove a parent->child dependency")
    p_unlink.add_argument("parent_id")
    p_unlink.add_argument("child_id")

    # --- claim ---
    p_claim = sub.add_parser(
        "claim",
        help="Atomically claim a ready task (prints resolved workspace path)",
    )
    p_claim.add_argument("task_id")
    p_claim.add_argument("--ttl", type=int, default=kb.DEFAULT_CLAIM_TTL_SECONDS,
                         help="Claim TTL in seconds (default: 900)")

    # --- comment / complete / block / unblock / archive ---
    p_comment = sub.add_parser("comment", help="Append a comment")
    p_comment.add_argument("task_id")
    p_comment.add_argument("text", nargs="+", help="Comment body")
    p_comment.add_argument("--author", default=None,
                           help="Author name (default: $HERMES_PROFILE or 'user')")
    p_comment.add_argument("--max-len", type=int, default=None,
                           help="Trim the stored comment body to this many characters")

    p_complete = sub.add_parser("complete", help="Mark one or more tasks done")
    p_complete.add_argument("task_ids", nargs="+",
                            help="One or more task ids (only --result applies to all of them)")
    p_complete.add_argument("--result", default=None, help="Result summary")
    p_complete.add_argument("--summary", default=None,
                            help="Structured handoff summary for downstream tasks. "
                                 "Falls back to --result if omitted.")
    p_complete.add_argument("--metadata", default=None,
                            help='JSON dict of structured facts (e.g. \'{"changed_files": [...], '
                                 '"tests_run": 12}\'). Stored on the closing run.')

    p_edit = sub.add_parser(
        "edit",
        help="Edit recovery fields on an already-completed task",
    )
    p_edit.add_argument("task_id")
    p_edit.add_argument(
        "--result",
        required=True,
        help="Backfilled task result text for a done task",
    )
    p_edit.add_argument(
        "--summary",
        default=None,
        help="Structured handoff summary. Falls back to --result if omitted.",
    )
    p_edit.add_argument(
        "--metadata",
        default=None,
        help="JSON dict of structured facts to store on the latest completed run.",
    )

    p_block = sub.add_parser("block", help="Mark one or more tasks blocked")
    p_block.add_argument("task_id")
    p_block.add_argument("reason", nargs="*", help="Reason (also appended as a comment)")
    p_block.add_argument("--ids", nargs="+", default=None,
                         help="Additional task ids to block with the same reason (bulk mode)")

    p_schedule = sub.add_parser("schedule", help="Park one or more tasks in Scheduled (waiting on time, not human input)")
    p_schedule.add_argument("task_id")
    p_schedule.add_argument("reason", nargs="*", help="Reason/timing note (also appended as a comment)")
    p_schedule.add_argument("--ids", nargs="+", default=None,
                            help="Additional task ids to schedule with the same reason (bulk mode)")

    p_wait = sub.add_parser(
        "wait",
        help="Park a task in healthy watching state until a trigger fires",
    )
    p_wait.add_argument("task_id")
    p_wait.add_argument("--trigger-type", required=True,
                        help="Wake route type: inbound_event, timer, dependency, webhook, manual, etc.")
    p_wait.add_argument("--trigger-key", default=None,
                        help="Optional route key/channel/id used to match the future event")
    p_wait.add_argument("--reason", default=None,
                        help="Human-readable waiting reason shown in funnel/UI")
    p_wait.add_argument("--wake-status", default="ready",
                        choices=["ready", "todo", "blocked", "review"],
                        help="Lifecycle status to set when triggered (default: ready)")
    p_wait.add_argument("--payload", default=None,
                        help="Optional JSON object with structured watch metadata")
    p_wait.add_argument("--json", action="store_true")

    p_trigger = sub.add_parser(
        "trigger",
        help="Trigger active watch routes and wake matching cards",
    )
    p_trigger.add_argument("--route-id", type=int, default=None)
    p_trigger.add_argument("--task-id", default=None)
    p_trigger.add_argument("--trigger-type", default=None)
    p_trigger.add_argument("--trigger-key", default=None)
    p_trigger.add_argument("--payload", default=None,
                           help="Optional JSON object describing the event that fired")
    p_trigger.add_argument("--actor", default=None)
    p_trigger.add_argument("--json", action="store_true")

    p_transition = sub.add_parser(
        "transition",
        help="Move a card to another semantic workflow stage after evidence validation",
    )
    p_transition.add_argument("task_id")
    p_transition.add_argument("to_stage")
    p_transition.add_argument("--evidence", action="append", default=[],
                              help="Evidence key proving the transition (repeatable)")
    p_transition.add_argument("--action", default=None,
                              help="New semantic action key in the target stage")
    p_transition.add_argument("--actor", default=None)
    p_transition.add_argument("--json", action="store_true")

    # --- pixel ---
    p_pixel = sub.add_parser(
        "pixel",
        help="Native Kanban Pixel goal, evidence, claims, and done gate",
    )
    pixel_sub = p_pixel.add_subparsers(dest="pixel_action")

    px_brief = pixel_sub.add_parser("brief", help="Show native Pixel board brief")
    px_brief.add_argument("--json", action="store_true")

    px_goal = pixel_sub.add_parser("goal", help="Set native Pixel goal/success contract")
    px_goal.add_argument("text")
    px_goal.add_argument("--success", action="append", required=True)
    px_goal.add_argument("--json", action="store_true")

    px_stage = pixel_sub.add_parser("stage", help="Map a stage to its conversion event")
    px_stage.add_argument("stage_key")
    px_stage.add_argument("--event", required=True, dest="conversion_event")
    px_stage.add_argument("--json", action="store_true")

    px_event = pixel_sub.add_parser("event", help="Record typed Pixel evidence event")
    px_event.add_argument("type")
    px_event.add_argument("--stage", required=True, dest="stage_key")
    px_event.add_argument("--task", default=None, dest="task_id")
    px_event.add_argument("--status", required=True, choices=sorted(kb.PIXEL_EVENT_STATUSES))
    px_event.add_argument("--evidence", required=True)
    px_event.add_argument("--json", action="store_true")

    px_claim = pixel_sub.add_parser("claim", help="Claim a Pixel lane/resource")
    px_claim.add_argument("--lane", required=True, dest="lane_id")
    px_claim.add_argument("--task", default=None, dest="task_id")
    px_claim.add_argument("--agent", required=True, dest="agent_id")
    px_claim.add_argument("--evidence", required=True)
    px_claim.add_argument("--json", action="store_true")

    px_release = pixel_sub.add_parser("release", help="Release a Pixel lane/resource claim")
    px_release.add_argument("--claim", required=True, type=int, dest="claim_id")
    px_release.add_argument("--agent", required=True, dest="agent_id")
    px_release.add_argument("--evidence", required=True)
    px_release.add_argument("--json", action="store_true")

    px_done = pixel_sub.add_parser("done", help="Validate and close a task through Pixel done gate")
    px_done.add_argument("task_id", nargs="?")
    px_done.add_argument("--json", action="store_true")

    p_unblock = sub.add_parser("unblock", help="Return one or more blocked/scheduled/watching tasks to ready")
    p_unblock.add_argument(
        "--reason",
        default=None,
        help="Optional reason/note — recorded as a comment before unblocking. Quote multi-word reasons.",
    )
    p_unblock.add_argument("task_ids", nargs="+")

    p_promote = sub.add_parser(
        "promote",
        help="Manually move one or more todo/blocked tasks to ready (recovery path)",
    )
    p_promote.add_argument("task_id")
    p_promote.add_argument(
        "reason",
        nargs="*",
        help="Audit-trail reason (recorded on the task_events row)",
    )
    p_promote.add_argument(
        "--ids",
        nargs="+",
        default=None,
        help="Additional task ids to promote with the same reason (bulk mode)",
    )
    p_promote.add_argument(
        "--force",
        action="store_true",
        help="Promote even if parent dependencies are not yet done/archived",
    )
    p_promote.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the promotion without mutating state",
    )
    p_promote.add_argument(
        "--json",
        dest="json",
        action="store_true",
        help="Emit machine-readable JSON result",
    )

    p_archive = sub.add_parser("archive", help="Archive one or more tasks")
    p_archive.add_argument("task_ids", nargs="*",
                           help="Task ids to archive (default mode)")
    p_archive.add_argument(
        "--rm",
        dest="purge_ids",
        nargs="+",
        default=None,
        help="Permanently delete already-archived task ids from the board",
    )

    # --- tail ---
    p_tail = sub.add_parser("tail", help="Follow a task's event stream")
    p_tail.add_argument("task_id")
    p_tail.add_argument("--interval", type=float, default=1.0)

    # --- dispatch ---
    p_disp = sub.add_parser(
        "dispatch",
        help="One dispatcher pass: reclaim stale, promote ready, spawn workers",
    )
    p_disp.add_argument("--dry-run", action="store_true",
                        help="Don't actually spawn processes; just print what would happen")
    p_disp.add_argument("--max", type=int, default=None,
                        help="Cap number of spawns this pass")
    p_disp.add_argument("--failure-limit", type=int,
                        default=kb.DEFAULT_SPAWN_FAILURE_LIMIT,
                        help=f"Auto-block a task after this many consecutive non-success attempts "
                             f"(spawn_failed, timed_out, or crashed; default: {kb.DEFAULT_SPAWN_FAILURE_LIMIT})")
    p_disp.add_argument("--json", action="store_true")

    # --- daemon (deprecated) ---
    p_daemon = sub.add_parser(
        "daemon",
        help="DEPRECATED — dispatcher now runs in the gateway. Use `hermes gateway start`.",
    )
    p_daemon.add_argument("--interval", type=float, default=60.0,
                          help="Seconds between dispatch ticks (default: 60)")
    p_daemon.add_argument("--max", type=int, default=None,
                          help="Cap number of spawns per tick")
    p_daemon.add_argument("--failure-limit", type=int,
                          default=kb.DEFAULT_SPAWN_FAILURE_LIMIT)
    p_daemon.add_argument("--pidfile", default=None,
                          help="Write the daemon's PID to this file on start")
    p_daemon.add_argument("--verbose", "-v", action="store_true",
                          help="Log each tick's outcome to stdout")
    # Undocumented escape hatch for users who truly cannot run the gateway.
    # Intentionally excluded from --help so nobody discovers it casually and
    # keeps the old double-dispatcher pattern alive.
    p_daemon.add_argument("--force", action="store_true",
                          help=argparse.SUPPRESS)

    # --- watch ---
    p_watch = sub.add_parser(
        "watch",
        help="Live-stream task_events to the terminal (Ctrl+C to exit)",
    )
    p_watch.add_argument("--assignee", default=None,
                         help="Only show events for tasks assigned to this profile")
    p_watch.add_argument("--tenant", default=None,
                         help="Only show events from tasks in this tenant")
    p_watch.add_argument("--kinds", default=None,
                         help="Comma-separated event kinds to include "
                              "(e.g. 'completed,blocked,gave_up,crashed,timed_out')")
    p_watch.add_argument("--interval", type=float, default=0.5,
                         help="Poll interval in seconds (default: 0.5)")

    # --- stats ---
    p_stats = sub.add_parser(
        "stats", help="Per-status + per-assignee counts + oldest-ready age",
    )
    p_stats.add_argument("--json", action="store_true")

    # --- funnel ---
    p_funnel = sub.add_parser(
        "funnel",
        help="Semantic goal/workstream/stage read-model over Kanban cards",
    )
    p_funnel.add_argument("--json", action="store_true", help="Emit JSON output")
    p_funnel.add_argument("--archived", action="store_true", help="Include archived cards")
    p_funnel.add_argument("--limit-cards", type=int, default=20,
                          help="Max cards embedded per stage in output")
    p_funnel.add_argument("--limit-entities", type=int, default=50,
                          help="Max compound entity summaries embedded per stage in output")
    p_funnel.add_argument("--goal", default=None, help="Filter by resolved goal id")
    p_funnel.add_argument("--workstream", default=None, help="Filter by resolved workstream id")
    p_funnel.add_argument("--stage", default=None, help="Filter by resolved stage key")
    p_funnel.add_argument("--action", default=None, help="Filter by resolved action key")

    p_funnel_set = sub.add_parser(
        "funnel-set",
        help="Set semantic funnel fields on an existing task",
    )
    p_funnel_set.add_argument("task_id")
    p_funnel_set.add_argument("--goal", default=None,
                              help="Semantic funnel goal id")
    p_funnel_set.add_argument("--workstream", default=None,
                              help="Semantic funnel workstream id")
    p_funnel_set.add_argument("--stage", default=None,
                              help="Semantic funnel stage key")
    p_funnel_set.add_argument("--action", default=None,
                              help="Semantic funnel action key")
    p_funnel_set.add_argument("--funnel-data", default=None,
                              help="JSON object with structured funnel facts")
    p_funnel_set.add_argument("--merge-funnel-data", action="store_true",
                              help="Shallow-merge --funnel-data into existing funnel_data")
    p_funnel_set.add_argument("--json", action="store_true", help="Emit updated task as JSON")

    # --- notify subscribe / list / remove ---
    p_nsub = sub.add_parser(
        "notify-subscribe",
        help="Subscribe a gateway source to a task's terminal events "
             "(used by /kanban subscribe in the gateway adapter)",
    )
    p_nsub.add_argument("task_id")
    p_nsub.add_argument("--platform", required=True)
    p_nsub.add_argument("--chat-id", required=True)
    p_nsub.add_argument("--thread-id", default=None)
    p_nsub.add_argument("--user-id", default=None)
    p_nsub.add_argument(
        "--notifier-profile", default=None,
        help="Profile gateway that owns/delivers this subscription (default: active profile)",
    )

    p_nlist = sub.add_parser(
        "notify-list",
        help="List notification subscriptions (optionally for a single task)",
    )
    p_nlist.add_argument("task_id", nargs="?", default=None)
    p_nlist.add_argument("--json", action="store_true")

    p_nrm = sub.add_parser(
        "notify-unsubscribe",
        help="Remove a gateway subscription from a task",
    )
    p_nrm.add_argument("task_id")
    p_nrm.add_argument("--platform", required=True)
    p_nrm.add_argument("--chat-id", required=True)
    p_nrm.add_argument("--thread-id", default=None)

    # --- log ---
    p_log = sub.add_parser(
        "log",
        help="Print the worker log for a task (from <kanban-root>/kanban/logs/)",
    )
    p_log.add_argument("task_id")
    p_log.add_argument("--tail", type=int, default=None,
                       help="Only print the last N bytes")

    # --- runs (per-attempt history for a task) ---
    p_runs = sub.add_parser(
        "runs",
        help="Show attempt history for a task (one row per run: profile, "
             "outcome, elapsed, summary)",
    )
    p_runs.add_argument("task_id")
    p_runs.add_argument("--json", action="store_true")
    p_runs.add_argument(
        "--state-type",
        choices=("status", "outcome"),
        default=None,
        help="With --state-name: filter runs by task_runs column",
    )
    p_runs.add_argument(
        "--state-name",
        default=None,
        metavar="VALUE",
        help="With --state-type: keep runs whose column equals this value",
    )

    # --- heartbeat (worker liveness signal) ---
    p_hb = sub.add_parser(
        "heartbeat",
        help="Emit a heartbeat event for a running task (worker liveness signal)",
    )
    p_hb.add_argument("task_id")
    p_hb.add_argument("--note", default=None,
                      help="Optional short note attached to the heartbeat event")

    # --- assignees ---
    p_asg = sub.add_parser(
        "assignees",
        help="List known profiles + per-profile task counts "
             "(union of ~/.hermes/profiles/ and current assignees on the board)",
    )
    p_asg.add_argument("--json", action="store_true")

    # --- context --- (for spawned workers)
    p_ctx = sub.add_parser(
        "context",
        help="Print the full context a worker sees for a task "
             "(title + body + parent results + comments).",
    )
    p_ctx.add_argument("task_id")

    # --- specify --- (triage → todo via auxiliary LLM)
    p_specify = sub.add_parser(
        "specify",
        help="Flesh out a triage-column task into a concrete spec "
             "(title + body) and promote it to todo. Uses the auxiliary "
             "LLM configured under auxiliary.triage_specifier.",
    )
    p_specify.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="Task id to specify (required unless --all is given)",
    )
    p_specify.add_argument(
        "--all",
        dest="all_triage",
        action="store_true",
        help="Specify every task currently in the triage column",
    )
    p_specify.add_argument(
        "--tenant",
        default=None,
        help="When used with --all, restrict the sweep to this tenant",
    )
    p_specify.add_argument(
        "--author",
        default=None,
        help="Author name recorded on the audit comment "
             "(default: $HERMES_PROFILE or 'specifier')",
    )
    p_specify.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object per task on stdout",
    )

    # --- decompose --- (triage → fan-out via auxiliary LLM + orchestrator)
    p_decompose = sub.add_parser(
        "decompose",
        help="Decompose a triage-column task into a graph of child tasks "
             "routed to specialist profiles by description. Falls back to "
             "specify-style single-task promotion when the task doesn't "
             "benefit from fan-out. Uses auxiliary.kanban_decomposer.",
    )
    p_decompose.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="Task id to decompose (required unless --all is given)",
    )
    p_decompose.add_argument(
        "--all",
        dest="all_triage",
        action="store_true",
        help="Decompose every task currently in the triage column",
    )
    p_decompose.add_argument(
        "--tenant",
        default=None,
        help="When used with --all, restrict the sweep to this tenant",
    )
    p_decompose.add_argument(
        "--author",
        default=None,
        help="Author name recorded on the audit comment "
             "(default: $HERMES_PROFILE or 'decomposer')",
    )
    p_decompose.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object per task on stdout",
    )

    # --- doctor (tick liveness check) ---
    p_doctor = sub.add_parser(
        "doctor",
        help="Liveness check: fail loudly when a board has live work "
             "(active timer schedules or an optimizer-managed knob) but the "
             "gateway is not ticking it.",
    )
    p_doctor.add_argument("--json", action="store_true", help="Emit JSON output")
    p_doctor.add_argument(
        "--all-boards", action="store_true",
        help="Check every non-archived board (default: only the current board)",
    )
    p_doctor.add_argument(
        "--staleness-seconds", type=int, default=kb.TICK_STALENESS_SECONDS,
        help=f"How long a board with live work may go without a successful tick "
             f"before it is flagged stale (default: {kb.TICK_STALENESS_SECONDS})",
    )

    # --- gc ---
    p_gc = sub.add_parser(
        "gc", help="Garbage-collect archived-task workspaces, old events, and old logs",
    )
    p_gc.add_argument("--event-retention-days", type=int, default=30,
                      help="Delete task_events older than N days for terminal tasks (default: 30)")
    p_gc.add_argument("--log-retention-days", type=int, default=30,
                      help="Delete worker log files older than N days (default: 30)")

    # --- sensors (Tier-1 sensor inspection + owner overrides) ---
    p_sensors = sub.add_parser(
        "sensors",
        help="Inspect Tier-1 sensors and apply owner overrides (e.g. force-close "
             "a tripped circuit breaker).",
        description=(
            "Tier-1 sensors (heartbeat, circuit breaker, budget) auto-recover on "
            "their own. The `reset` subcommand is an OWNER override that "
            "force-closes a tripped circuit breaker immediately (after the owner "
            "has fixed the underlying fault) instead of waiting out the cooldown. "
            "The machine's own recovery path is unchanged."
        ),
    )
    sensors_sub = p_sensors.add_subparsers(dest="sensors_action")

    s_list = sensors_sub.add_parser(
        "list", aliases=["ls"],
        help="List declared sensors and their current persisted state",
    )
    s_list.add_argument("--json", action="store_true", help="Emit JSON output")

    s_reset = sensors_sub.add_parser(
        "reset",
        help="OWNER override: force a tripped circuit breaker back to 'closed'",
    )
    s_reset.add_argument(
        "--sensor", default="circuit_breaker", metavar="<kind|key>",
        help="Sensor kind ('circuit_breaker') or a specific sensor key to reset. "
             "Only circuit breakers are resettable; omit a key to reset every "
             "circuit breaker on the board (default: circuit_breaker).",
    )
    s_reset.add_argument(
        "--reason", default=None,
        help="Optional audit reason recorded with the override.",
    )
    s_reset.add_argument("--json", action="store_true", help="Emit JSON output")

    kanban_parser.set_defaults(_kanban_parser=kanban_parser)
    return kanban_parser


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

def _kanban_debug_enabled(args: argparse.Namespace) -> bool:
    """True when the operator asked for full tracebacks (``--debug`` or env)."""
    if getattr(args, "debug", False):
        return True
    val = os.environ.get("HERMES_DEBUG", "")
    return val.strip().lower() not in ("", "0", "false", "no", "off")


def _operator_error_message(action: Optional[str], exc: BaseException) -> str:
    """Render an unexpected exception as a clear operator-facing message.

    Keeps the message short and actionable: what failed, the underlying error,
    and a hint to re-run with ``--debug`` for the full traceback. Specific
    well-known failure shapes get a tailored hint.
    """
    kind = type(exc).__name__
    detail = str(exc).strip() or kind
    where = f" while running `kanban {action}`" if action else ""
    lines = [f"kanban: unexpected error{where}: {detail}"]
    # Tailored, actionable hints for the failure shapes an operator hits most.
    import sqlite3 as _sqlite3

    low = detail.lower()
    if isinstance(exc, kb.BoardNotFoundError):
        lines.append(
            f"  hint: board {exc.slug!r} has no board.json/kanban.db. Create it "
            f"explicitly with `hermes kanban boards create {exc.slug}`, or check "
            "HERMES_KANBAN_BOARD / --board for a typo (`hermes kanban boards list`)."
        )
    elif isinstance(exc, kb.UnconfiguredKanbanDbError):
        lines.append(
            f"  hint: HERMES_KANBAN_DB={exc.path!r} is out-of-root and the file "
            f"doesn't exist. Point it at an existing kanban.db, create the board "
            f"explicitly, or set HERMES_KANBAN_ALLOW_IMPLICIT_BOARD=1 to opt in."
        )
    elif isinstance(exc, _sqlite3.OperationalError) and "locked" in low:
        lines.append(
            "  hint: the board database is locked by another process. "
            "Stop other `hermes kanban`/gateway processes and retry."
        )
    elif isinstance(exc, _sqlite3.DatabaseError):
        lines.append(
            "  hint: the board database may be corrupt or from an incompatible "
            "version. Run `hermes kanban doctor` to inspect it."
        )
    elif isinstance(exc, (FileNotFoundError, PermissionError)):
        lines.append(
            "  hint: check HERMES_HOME and that the board exists "
            "(`hermes kanban boards list`)."
        )
    lines.append("  Re-run with --debug (or HERMES_DEBUG=1) for the full traceback.")
    return "\n".join(lines)


def kanban_command(args: argparse.Namespace) -> int:
    """Entry point from ``hermes kanban …`` argparse dispatch.

    Returns a shell-style exit code (0 on success, non-zero on error). Wraps
    the whole dispatch in an operator-friendly error boundary: unexpected
    exceptions become a clear message + actionable hint + nonzero exit instead
    of a raw traceback. Pass ``--debug`` (or set ``HERMES_DEBUG``) to re-raise
    the full traceback for diagnosis.
    """
    action = getattr(args, "kanban_action", None)
    if not action:
        # No subaction given: print help via the stored parser reference.
        parser = getattr(args, "_kanban_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            print(
                "usage: hermes kanban <action> [options]\n"
                "Run 'hermes kanban --help' for the full list of actions.",
                file=sys.stderr,
            )
        return 0

    try:
        return _kanban_command_dispatch(args, action)
    except (BrokenPipeError, KeyboardInterrupt):
        # Don't swallow operator interrupts / closed pipes (e.g. `| head`).
        raise
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - top-level operator boundary
        if _kanban_debug_enabled(args):
            raise
        print(_operator_error_message(action, exc), file=sys.stderr)
        return 1


def _kanban_command_dispatch(args: argparse.Namespace, action: str) -> int:
    """Dispatch a resolved kanban ``action`` (wrapped by kanban_command)."""
    # Board-management commands operate on board metadata and the persisted
    # current-board pointer itself. They must ignore the shared `--board`
    # task-routing override; otherwise `/kanban --board beta boards show`
    # reports beta as the current board even when the on-disk pointer is
    # alpha.
    if action == "boards":
        return _dispatch_boards(args)

    # `--board <slug>` applies to every subcommand below by way of an
    # env-var pin for the duration of this call. Using HERMES_KANBAN_BOARD
    # (rather than threading `board=` through 50+ kb.connect() sites)
    # keeps the patch small and inherits the exact same resolution the
    # dispatcher uses for workers — consistency is a feature here.
    board_override = getattr(args, "board", None)
    prev_board_env = os.environ.get("HERMES_KANBAN_BOARD")
    restore_board_env = False

    def _restore_board_env() -> None:
        if not restore_board_env:
            return
        if prev_board_env is None:
            os.environ.pop("HERMES_KANBAN_BOARD", None)
        else:
            os.environ["HERMES_KANBAN_BOARD"] = prev_board_env
    if board_override:
        try:
            normed = kb._normalize_board_slug(board_override)
        except ValueError as exc:
            print(f"kanban: {exc}", file=sys.stderr)
            return 2
        if not normed:
            print("kanban: --board requires a slug", file=sys.stderr)
            return 2
        # Boards other than 'default' must already exist — typoed slugs
        # would otherwise silently create an empty board.
        if normed != kb.DEFAULT_BOARD and not kb.board_exists(normed):
            print(
                f"kanban: board {normed!r} does not exist. "
                f"Create it with `hermes kanban boards create {normed}`.",
                file=sys.stderr,
            )
            return 1
        os.environ["HERMES_KANBAN_BOARD"] = normed
        restore_board_env = True

    # Auto-initialize the DB before dispatching any subcommand. init_db
    # is idempotent, so running it every invocation is cheap (one
    # SELECT against sqlite_master when tables already exist) and
    # prevents "no such table: tasks" on first use from a fresh
    # HERMES_HOME. Previously only `init` and `daemon` triggered
    # schema creation; `create` / `list` / every other command would
    # error out on a fresh install.
    try:
        kb.init_db()
    except Exception as exc:
        print(f"kanban: could not initialize database: {exc}", file=sys.stderr)
        _restore_board_env()
        return 1

    handlers = {
        "init":     _cmd_init,
        "create":   _cmd_create,
        "swarm":    _cmd_swarm,
        "list":     _cmd_list,
        "ls":       _cmd_list,
        "show":     _cmd_show,
        "assign":   _cmd_assign,
        "reclaim":  _cmd_reclaim,
        "reassign": _cmd_reassign,
        "diagnostics": _cmd_diagnostics,
        "diag":     _cmd_diagnostics,
        "link":     _cmd_link,
        "unlink":   _cmd_unlink,
        "claim":    _cmd_claim,
        "comment":  _cmd_comment,
        "complete": _cmd_complete,
        "edit":     _cmd_edit,
        "block":    _cmd_block,
        "schedule": _cmd_schedule,
        "wait":     _cmd_wait,
        "trigger":  _cmd_trigger,
        "transition": _cmd_transition,
        "pixel":    _cmd_pixel,
        "unblock":  _cmd_unblock,
        "promote":  _cmd_promote,
        "archive":  _cmd_archive,
        "tail":     _cmd_tail,
        "dispatch": _cmd_dispatch,
        "daemon":   _cmd_daemon,
        "watch":    _cmd_watch,
        "stats":    _cmd_stats,
        "funnel":   _cmd_funnel,
        "funnel-set": _cmd_funnel_set,
        "log":      _cmd_log,
        "runs":     _cmd_runs,
        "heartbeat": _cmd_heartbeat,
        "assignees": _cmd_assignees,
        "notify-subscribe":   _cmd_notify_subscribe,
        "notify-list":        _cmd_notify_list,
        "notify-unsubscribe": _cmd_notify_unsubscribe,
        "context":  _cmd_context,
        "steer":    _cmd_steer,
        "specify":  _cmd_specify,
        "decompose":  _cmd_decompose,
        "doctor":   _cmd_doctor,
        "gc":       _cmd_gc,
        "sensors":  _cmd_sensors,
    }
    handler = handlers.get(action)
    if not handler:
        print(f"kanban: unknown action {action!r}", file=sys.stderr)
        _restore_board_env()
        return 2
    try:
        return int(handler(args) or 0)
    except (ValueError, RuntimeError) as exc:
        print(f"kanban: {exc}", file=sys.stderr)
        _restore_board_env()
        return 1
    finally:
        _restore_board_env()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _profile_author() -> str:
    """Best-effort author name for an interactive CLI call."""
    for env in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        v = os.environ.get(env)
        if v:
            return v
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "user"
    except Exception:
        return "user"


# ---------------------------------------------------------------------------
# Boards management (hermes kanban boards …)
# ---------------------------------------------------------------------------

def _dispatch_boards(args: argparse.Namespace) -> int:
    """Handle ``hermes kanban boards <action>``.

    Boards management is deliberately separate from the task-level
    commands: it operates on the filesystem (board directories,
    ``current`` pointer, ``board.json``), not on the per-board SQLite
    DB, so a fresh HERMES_HOME that has never called ``kanban init``
    can still run ``boards create`` / ``boards list``.
    """
    sub = getattr(args, "boards_action", None) or "list"
    if sub in {"list", "ls"}:
        return _cmd_boards_list(args)
    if sub in {"create", "new"}:
        return _cmd_boards_create(args)
    if sub in {"rm", "remove", "delete"}:
        return _cmd_boards_rm(args)
    if sub in {"switch", "use"}:
        return _cmd_boards_switch(args)
    if sub in {"show", "current"}:
        return _cmd_boards_show(args)
    if sub == "rename":
        return _cmd_boards_rename(args)
    if sub == "set-default-workdir":
        return _cmd_boards_set_default_workdir(args)
    if sub == "workflow":
        return _cmd_boards_workflow(args)
    if sub == "contract":
        return _cmd_boards_contract(args)
    if sub == "quarantine-orphans":
        return _cmd_boards_quarantine_orphans(args)
    if sub == "bind-roles":
        return _cmd_boards_bind_roles(args)
    print(f"kanban boards: unknown action {sub!r}", file=sys.stderr)
    return 2


def _cmd_boards_quarantine_orphans(args: argparse.Namespace) -> int:
    """One-time cleanup: move orphan boards (kanban.db, no board.json) aside.

    Dry-run by default — pass ``--apply`` to actually quarantine. ``--adopt``
    instead stamps a board.json onto each orphan (keep the data in place).
    """
    orphans = kb.scan_orphan_boards()
    if not orphans:
        print("No orphan boards found (every kanban.db has a board.json).")
        return 0
    apply = getattr(args, "apply", False)
    adopt = getattr(args, "adopt", False)
    for o in orphans:
        label = f"{o['slug']} ({o['task_count']} task(s)) at {o['path']}"
        if not apply:
            verb = "would adopt" if adopt else "would quarantine"
            print(f"  {verb}: {label}")
            continue
        if adopt:
            kb.adopt_orphan_board(o["slug"])
            print(f"  adopted (board.json written): {label}")
        else:
            res = kb.quarantine_orphan_board(o["slug"])
            print(f"  quarantined -> {res['new_path']}: {label}")
    if not apply:
        print(f"\n{len(orphans)} orphan(s). Re-run with --apply to act "
              f"(add --adopt to keep them as boards instead of quarantining).")
    return 0


def _cmd_boards_bind_roles(args: argparse.Namespace) -> int:
    """Bind every profile a board's contract names to that ONE board.

    Writes ``kanban_board: <slug>`` into each role profile's config.yaml so a
    gateway for any of them operates the single shared board (never 'default').
    """
    slug = getattr(args, "slug", None)
    res = kb.bind_contract_roles_to_board(slug)
    bound = res.get("bound", {})
    if not bound:
        print(f"board {res.get('board')!r} declares no contract role profiles "
              f"to bind (runtime.dispatcher.profile / runtime.profiles).")
        return 0
    print(f"Bound {len(bound)} profile(s) to board {res['board']!r}:")
    for prof, b in bound.items():
        print(f"  {prof} -> {b}")
    for c in res.get("conflicts", []):
        print(f"  note: {c['profile']} was bound to {c['was']!r}, re-bound to "
              f"{c['now']!r}", file=sys.stderr)
    for prof in res.get("skipped", []):
        print(f"  skipped {prof!r}: the default/root profile is never auto-pinned "
              f"to a single board", file=sys.stderr)
    return 0


def _board_task_counts(slug: str) -> dict[str, int]:
    """Return ``{status: count}`` for a board. Safe to call on an empty DB."""
    try:
        path = kb.kanban_db_path(board=slug)
        if not path.exists():
            return {}
        with kb.connect_closing(board=slug) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}
    except Exception:
        return {}


def _cmd_boards_list(args: argparse.Namespace) -> int:
    include_archived = bool(getattr(args, "all", False))
    boards = kb.list_boards(include_archived=include_archived)
    # Enrich each entry with task counts + whether it's the current board.
    current = kb.get_current_board()
    for b in boards:
        b["is_current"] = (b["slug"] == current)
        b["counts"] = _board_task_counts(b["slug"])
        b["total"] = sum(b["counts"].values())
    if getattr(args, "json", False):
        print(json.dumps(boards, indent=2, ensure_ascii=False))
        return 0
    # Human table: marker (•) for current, slug, display name, counts.
    if not boards:
        print("(no boards — create one with `hermes kanban boards create <slug>`)")
        return 0
    print(f"{'':2s}  {'SLUG':24s}  {'NAME':28s}  COUNTS")
    for b in boards:
        marker = "●" if b["is_current"] else " "
        counts = b["counts"] or {}
        counts_str = (
            ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            or "(empty)"
        )
        name = b.get("name") or ""
        if b.get("archived"):
            name += " [archived]"
        print(f"{marker:2s}  {b['slug']:24s}  {name:28s}  {counts_str}")
    print()
    print(f"Current board: {current}")
    if len(boards) > 1:
        print("Switch boards with `hermes kanban boards switch <slug>`.")
    return 0


def _cmd_boards_create(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards create: {exc}", file=sys.stderr)
        return 2
    if not normed:
        print("kanban boards create: slug is required", file=sys.stderr)
        return 2
    already = kb.board_exists(normed) and normed != kb.DEFAULT_BOARD
    workflow, workflow_error = _parse_json_object_flag(getattr(args, "workflow", None), "--workflow")
    if workflow_error:
        print(f"kanban boards create: {workflow_error}", file=sys.stderr)
        return 2
    contract, contract_error = _parse_json_object_flag(getattr(args, "contract", None), "--contract")
    if contract_error:
        print(f"kanban boards create: {contract_error}", file=sys.stderr)
        return 2
    runtime = getattr(args, "runtime", None)
    contract_runtime_raw = contract.get("runtime") if isinstance(contract, dict) else None
    contract_runtime = dict(contract_runtime_raw) if isinstance(contract_runtime_raw, dict) else {}
    effective_runtime = runtime or contract_runtime.get("mode") or "goal"
    dispatcher_profile = getattr(args, "dispatcher_profile", None)
    contract_dispatcher = contract_runtime.get("dispatcher") if isinstance(contract_runtime, dict) else None
    contract_dispatcher_profile = (
        contract_dispatcher.get("profile")
        if isinstance(contract_dispatcher, dict)
        else (contract_dispatcher if isinstance(contract_dispatcher, str) else None)
    )
    if effective_runtime != "kernel" and not dispatcher_profile and not contract_dispatcher_profile:
        dispatcher_profile = get_active_profile_name() or "default"
    try:
        meta = kb.create_board(
            normed,
            name=args.name,
            description=args.description,
            icon=args.icon,
            color=args.color,
            default_workdir=args.default_workdir,
            runtime=runtime,
            objective=getattr(args, "objective", None),
            success=getattr(args, "success", None),
            failure=getattr(args, "failure", None),
            constraints=getattr(args, "constraint", None),
            dispatcher_profile=dispatcher_profile,
            ceo_profile=getattr(args, "ceo_profile", None),
            optimizer_profile=getattr(args, "optimizer_profile", None),
            worker_profile=getattr(args, "worker_profile", None),
            workflow=workflow,
            contract=contract,
        )
    except ValueError as exc:
        print(f"kanban boards create: {exc}", file=sys.stderr)
        return 2
    verb = "already exists" if already else "created"
    print(f"Board {meta['slug']!r} {verb}.")
    print(f"  Display name: {meta.get('name', '')}")
    print(f"  DB path:      {meta['db_path']}")
    objective = meta.get("objective") or {}
    runtime_meta = meta.get("runtime") or {}
    workflow_meta = meta.get("workflow") or {}
    dispatcher = (runtime_meta.get("dispatcher") or {}).get("profile")
    profiles = runtime_meta.get("profiles") or {}
    stage_keys = [
        str(stage.get("key"))
        for stage in workflow_meta.get("stages") or []
        if isinstance(stage, dict) and stage.get("key")
    ]
    print(f"  Runtime:      {runtime_meta.get('mode') or runtime}")
    print(f"  Objective:    {objective.get('statement') or '(none)'}")
    if objective.get("failure"):
        print(f"  Failure:      {'; '.join(objective.get('failure') or [])}")
    print(f"  Workflow:     {' -> '.join(stage_keys) if stage_keys else '(none)'}")
    print(f"  Dispatcher:   {dispatcher or '(unowned)'}")
    if runtime_meta.get("provider_policy"):
        print(f"  Providers:    {', '.join(sorted(runtime_meta.get('provider_policy') or {}))}")
    if runtime_meta.get("worker_envelopes"):
        print(f"  Envelopes:    {', '.join(sorted(runtime_meta.get('worker_envelopes') or {}))}")
    profile_bits = [
        f"{key}={value}"
        for key, value in profiles.items()
        if value
    ]
    if profile_bits:
        print(f"  Profiles:     {', '.join(profile_bits)}")
    if getattr(args, "switch", False):
        kb.set_current_board(meta["slug"])
        print(f"  Switched to {meta['slug']!r}.")
    else:
        print(f"  Use `hermes kanban boards switch {meta['slug']}` to make it current.")
    return 0


def _cmd_boards_rm(args: argparse.Namespace) -> int:
    # When the user runs `hermes kanban boards delete <slug>` (alias), the
    # boards_action is 'delete' but args.delete is never set to True because
    # the --delete flag belongs to the 'rm' subparser only.  Detect the alias
    # and treat it identically to `boards rm --delete` (fixes #23139).
    force_delete = getattr(args, "delete", False) or getattr(args, "boards_action", "") == "delete"
    try:
        res = kb.remove_board(args.slug, archive=not force_delete)
    except ValueError as exc:
        print(f"kanban boards rm: {exc}", file=sys.stderr)
        return 1
    if res["action"] == "archived":
        print(f"Board {res['slug']!r} archived → {res['new_path']}")
        print("Recover by moving the directory back to "
              "<root>/kanban/boards/<slug>/.")
    else:
        print(f"Board {res['slug']!r} deleted.")
    return 0


def _cmd_boards_switch(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards switch: {exc}", file=sys.stderr)
        return 2
    if not normed:
        print("kanban boards switch: slug is required", file=sys.stderr)
        return 2
    if not kb.board_exists(normed):
        print(
            f"kanban boards switch: board {normed!r} does not exist. "
            f"Create it with `hermes kanban boards create {normed}`.",
            file=sys.stderr,
        )
        return 1
    kb.set_current_board(normed)
    print(f"Active board is now {normed!r}.")
    return 0


def _cmd_boards_show(args: argparse.Namespace) -> int:
    current = kb.get_current_board()
    meta = kb.read_board_metadata(current)
    counts = _board_task_counts(current)
    total = sum(counts.values())
    print(f"Current board: {current}")
    print(f"  Display name: {meta.get('name', '')}")
    if meta.get("description"):
        print(f"  Description:  {meta['description']}")
    print(f"  DB path:      {meta['db_path']}")
    print(f"  Tasks:        {total} total"
          + (f" ({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))})"
             if counts else ""))
    return 0


def _cmd_boards_rename(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards rename: {exc}", file=sys.stderr)
        return 2
    if not normed or not kb.board_exists(normed):
        print(f"kanban boards rename: board {args.slug!r} does not exist",
              file=sys.stderr)
        return 1
    meta = kb.write_board_metadata(normed, name=args.name)
    print(f"Board {normed!r} renamed to {meta['name']!r}.")
    return 0


def _cmd_boards_set_default_workdir(args: argparse.Namespace) -> int:
    try:
        normed = kb._normalize_board_slug(args.slug)
    except ValueError as exc:
        print(f"kanban boards set-default-workdir: {exc}", file=sys.stderr)
        return 2
    if not normed or not kb.board_exists(normed):
        print(f"kanban boards set-default-workdir: board {args.slug!r} does not exist",
              file=sys.stderr)
        return 1
    meta = kb.write_board_metadata(normed, default_workdir=args.path)
    new_val = meta.get("default_workdir")
    if new_val:
        print(f"Board {normed!r} default workdir set to {new_val!r}.")
    else:
        print(f"Board {normed!r} default workdir cleared.")
    return 0


def _cmd_boards_workflow(args: argparse.Namespace) -> int:
    sub = getattr(args, "workflow_action", None) or "show"
    slug = getattr(args, "slug", None) or kb.get_current_board()
    try:
        normed = kb._normalize_board_slug(slug)
    except ValueError as exc:
        print(f"kanban boards workflow: {exc}", file=sys.stderr)
        return 2
    if not normed or not kb.board_exists(normed):
        print(f"kanban boards workflow: board {slug!r} does not exist", file=sys.stderr)
        return 1
    if sub == "show":
        workflow = kb.read_board_metadata(normed).get("workflow")
        if getattr(args, "json", False):
            print(json.dumps(workflow, indent=2, ensure_ascii=False))
        elif workflow:
            print(json.dumps(workflow, indent=2, ensure_ascii=False))
        else:
            print(f"Board {normed!r} has no workflow schema.")
        return 0
    if sub == "set":
        workflow, error = _parse_json_object_flag(getattr(args, "workflow", None), "workflow")
        if error:
            print(f"kanban boards workflow set: {error}", file=sys.stderr)
            return 2
        meta = kb.write_board_metadata(normed, workflow=workflow)
        print(f"Board {normed!r} workflow set to {meta['workflow'].get('id')!r}.")
        return 0
    if sub == "clear":
        kb.write_board_metadata(normed, workflow=None)
        print(f"Board {normed!r} workflow cleared.")
        return 0
    print(f"kanban boards workflow: unknown action {sub!r}", file=sys.stderr)
    return 2


def _print_contract_readiness(status: dict[str, Any]) -> None:
    readiness = status.get("readiness") or {}
    print(f"Board:            {status.get('board')}")
    print(f"Launch phase:     {status.get('launch_phase')}")
    print(f"Contract version: {status.get('contract_version')}")
    print(f"Dispatch enabled: {'yes' if status.get('dispatch_enabled') else 'no'}")
    print(f"Readiness:        {readiness.get('status')}")
    missing = readiness.get("missing") or []
    if missing:
        print(f"Missing:          {', '.join(missing)}")
    errors = readiness.get("errors") or []
    if errors:
        print(f"Errors:           {'; '.join(errors)}")
    questions = readiness.get("questions") or []
    if questions:
        print("Questions:")
        for question in questions:
            print(f"  - {question}")


def _contract_apply_output(
    result: dict[str, Any],
    board: str,
    *,
    force: bool,
    activate: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    status = kb.validate_board_launch_readiness(board)
    enriched = dict(result)
    enriched.update({
        "launch_phase": status.get("launch_phase"),
        "dispatch_enabled": bool(status.get("dispatch_enabled")),
        "launch_blocked": not bool(status.get("dispatch_enabled")),
        "forced": bool(force),
        "activate_requested": bool(activate),
        "launch_status": status,
    })
    return enriched, status


def _print_contract_apply_result(
    result: dict[str, Any],
    status: dict[str, Any],
    *,
    board: str,
    amendment_id: str,
    force: bool,
    activate: bool,
) -> None:
    readiness = status.get("readiness") or {}
    dispatch_enabled = bool(status.get("dispatch_enabled"))
    print(
        f"Applied {amendment_id} to board {board!r}; "
        f"contract_version={result.get('contract_version')}."
    )
    print(f"Launch phase:     {status.get('launch_phase')}")
    print(f"Dispatch enabled: {'yes' if dispatch_enabled else 'no'}")
    print(f"launch_blocked:   {'no' if dispatch_enabled else 'yes'}")
    print(f"Readiness:        {readiness.get('status')}")
    if force:
        print("Forced:           yes (deprecated)")
    if not activate:
        print("Activate:         no (--no-activate)")
    missing = readiness.get("missing") or []
    if missing:
        print(f"Missing:          {', '.join(missing)}")
    errors = readiness.get("errors") or []
    if errors:
        print(f"Errors:           {'; '.join(errors)}")
    questions = readiness.get("questions") or []
    if questions:
        print("Questions:")
        for question in questions:
            print(f"  - {question}")


def _cmd_boards_contract(args: argparse.Namespace) -> int:
    sub = getattr(args, "contract_action", None) or "status"
    slug = getattr(args, "slug", None) or kb.get_current_board()
    try:
        normed = kb._normalize_board_slug(slug)
    except ValueError as exc:
        print(f"kanban boards contract: {exc}", file=sys.stderr)
        return 2
    if not normed:
        print("kanban boards contract: board slug is required", file=sys.stderr)
        return 2

    if sub == "status":
        if not kb.board_exists(normed):
            print(f"kanban boards contract status: board {slug!r} does not exist", file=sys.stderr)
            return 1
        status = kb.validate_board_launch_readiness(normed)
        if getattr(args, "json", False):
            print(json.dumps(status, indent=2, ensure_ascii=False))
        else:
            _print_contract_readiness(status)
        return 0

    if sub == "review":
        contract, error = _parse_json_object_flag(getattr(args, "contract", None), "--contract")
        if error:
            print(f"kanban boards contract review: {error}", file=sys.stderr)
            return 2
        approval_evidence, error = _parse_text_or_file_flag(
            getattr(args, "approval_evidence", None),
            "--approval-evidence",
        )
        if error:
            print(f"kanban boards contract review: {error}", file=sys.stderr)
            return 2
        intake_answers, error = _parse_text_or_file_flag(
            getattr(args, "intake_answers", None),
            "--intake-answers",
        )
        if error:
            print(f"kanban boards contract review: {error}", file=sys.stderr)
            return 2
        if contract is None and not getattr(args, "rough_goal", None) and intake_answers is None:
            print("kanban boards contract review: --contract, --rough-goal, or --intake-answers is required", file=sys.stderr)
            return 2
        approve = bool(getattr(args, "approve", False))
        try:
            result = kb.review_business_launch_contract(
                normed,
                contract=contract,
                rough_goal=getattr(args, "rough_goal", None),
                intake_answers=intake_answers,
                create_if_missing=bool(getattr(args, "create", False)),
                approve=approve,
                name=getattr(args, "name", None),
                description=getattr(args, "description", None),
                author=_current_user_label(),
                approved_by=getattr(args, "approved_by", None),
                approval_evidence=approval_evidence,
                approval_reason=getattr(args, "approval_reason", None),
                approval_token=getattr(args, "approval_token", None),
                require_launch_intake=_is_owner_launch_intake_profile(),
                operator_override=bool(getattr(args, "operator_override", False)),
            )
        except ValueError as exc:
            print(f"kanban boards contract review: {exc}", file=sys.stderr)
            return 2
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"Board {normed!r} launch phase: {result.get('launch_phase')}")
            readiness = result.get("readiness") or {}
            print(f"Readiness: {readiness.get('status')}")
            questions = result.get("questions") or []
            if questions:
                print("Questions:")
                for question in questions:
                    print(f"  - {question}")
            else:
                intake = result.get("launch_intake") or {}
                assessment = intake.get("answer_assessment") or {}
                generation = intake.get("question_generation") or {}
                if assessment.get("required"):
                    print(
                        "Next step: Hermes must assess these answers and either ask "
                        "follow-up questions or draft the board contract."
                    )
                elif generation.get("required"):
                    print(
                        "Next step: Hermes must ask tailored clarification questions "
                        "from this rough goal before drafting the board contract."
                    )
        return 0

    if sub == "approval-token":
        contract, error = _parse_json_object_flag(getattr(args, "contract", None), "--contract")
        if error:
            print(f"kanban boards contract approval-token: {error}", file=sys.stderr)
            return 2
        approval_evidence, error = _parse_text_or_file_flag(
            getattr(args, "approval_evidence", None),
            "--approval-evidence",
        )
        if error:
            print(f"kanban boards contract approval-token: {error}", file=sys.stderr)
            return 2
        amendment_id = getattr(args, "amendment_id", None)
        if amendment_id and (contract is not None or getattr(args, "rough_goal", None)):
            print(
                "kanban boards contract approval-token: --amendment-id cannot be combined with --contract or --rough-goal",
                file=sys.stderr,
            )
            return 2
        try:
            result = kb.issue_board_launch_approval_token(
                normed,
                contract=contract,
                rough_goal=getattr(args, "rough_goal", None),
                amendment_id=amendment_id,
                approved_by=getattr(args, "approved_by", None),
                approval_evidence=approval_evidence,
                approval_reason=getattr(args, "approval_reason", None),
                ttl_seconds=getattr(args, "ttl_seconds", None),
                owner_authority_confirmed=_owner_approval_authority_confirmed(),
                require_launch_intake=_is_owner_launch_intake_profile(),
                operator_override=bool(getattr(args, "operator_override", False)),
            )
        except ValueError as exc:
            print(f"kanban boards contract approval-token: {exc}", file=sys.stderr)
            return 2
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(result["token"])
        return 0

    if sub == "propose":
        patch, error = _parse_json_object_flag(getattr(args, "patch", None), "patch")
        if error:
            print(f"kanban boards contract propose: {error}", file=sys.stderr)
            return 2
        try:
            amendment = kb.propose_board_contract_amendment(
                normed,
                patch=patch,
                reason=getattr(args, "reason", ""),
                author=_current_user_label(),
                risk=getattr(args, "risk", None),
            )
        except ValueError as exc:
            print(f"kanban boards contract propose: {exc}", file=sys.stderr)
            return 2
        if getattr(args, "json", False):
            print(json.dumps(amendment, indent=2, ensure_ascii=False))
        else:
            readiness = amendment.get("readiness") or {}
            print(f"Amendment {amendment.get('id')} pending for board {normed!r}.")
            print(f"Candidate readiness: {readiness.get('status')}")
            for question in readiness.get("questions") or []:
                print(f"  - {question}")
        return 0

    if sub == "apply":
        force = bool(getattr(args, "force", False))
        activate = not bool(getattr(args, "no_activate", False))
        if force:
            print(
                "kanban boards contract apply: --force has been retired; "
                "submit a launch-ready amendment and owner approval-token",
                file=sys.stderr,
            )
            return 2
        approval_evidence, error = _parse_text_or_file_flag(
            getattr(args, "approval_evidence", None),
            "--approval-evidence",
        )
        if error:
            print(f"kanban boards contract apply: {error}", file=sys.stderr)
            return 2
        try:
            result = kb.apply_board_contract_amendment(
                normed,
                getattr(args, "amendment_id", ""),
                approved_by=getattr(args, "approved_by", None),
                approval_evidence=approval_evidence,
                approval_token=getattr(args, "approval_token", None),
                activate=activate,
            )
        except ValueError as exc:
            print(f"kanban boards contract apply: {exc}", file=sys.stderr)
            return 2
        result, launch_status = _contract_apply_output(
            result,
            normed,
            force=force,
            activate=activate,
        )
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            _print_contract_apply_result(
                result,
                launch_status,
                board=normed,
                amendment_id=getattr(args, "amendment_id", ""),
                force=force,
                activate=activate,
            )
        return 0

    if sub == "amendment":
        return _cmd_boards_contract_amendment(args, normed)

    print(f"kanban boards contract: unknown action {sub!r}", file=sys.stderr)
    return 2


def _cmd_boards_contract_amendment(args: argparse.Namespace, board: str) -> int:
    action = getattr(args, "amendment_action", None) or "status"
    board = kb._normalize_board_slug(getattr(args, "board", None) or board)
    as_json = bool(getattr(args, "json", False))
    if not kb.board_exists(board):
        print(f"kanban boards contract amendment: board {board!r} does not exist", file=sys.stderr)
        return 1

    def _emit(payload: Any) -> int:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 0

    try:
        with kb.connect(board=board) as conn:
            if action == "status":
                model = kb.build_contract_amendments_read_model(conn, board=board)
                if as_json:
                    return _emit(model)
                print(f"Board {board!r}: {model['pending']} amendment(s) in flight")
                for item in model["amendments"]:
                    print(
                        f"  {item['amendment_id']}  {item['status']:<18} "
                        f"origin={item['origin']} base=v{item['base_version']}"
                        + (f" minted=v{item['minted_version']}" if item.get("minted_version") else "")
                    )
                return 0

            if action == "list":
                items = kb.list_contract_amendments(
                    conn, board=board, status=getattr(args, "status", None)
                )
                if as_json:
                    return _emit(items)
                if not items:
                    print("No contract amendments.")
                for item in items:
                    print(
                        f"{item['amendment_id']}  {item['status']:<18} "
                        f"origin={item['origin']}  base=v{item['base_version']}  "
                        f"{item.get('rationale') or ''}"
                    )
                return 0

            if action == "show":
                amendment = kb.get_contract_amendment(
                    conn, getattr(args, "amendment_id", ""), board=board
                )
                if amendment is None:
                    print("kanban boards contract amendment show: not found", file=sys.stderr)
                    return 1
                return _emit(amendment)

            if action == "propose":
                contract, error = _parse_json_object_flag(getattr(args, "contract", None), "--contract")
                if error:
                    print(f"kanban boards contract amendment propose: {error}", file=sys.stderr)
                    return 2
                diff, error = _parse_json_object_flag(getattr(args, "diff", None), "--diff")
                if error:
                    print(f"kanban boards contract amendment propose: {error}", file=sys.stderr)
                    return 2
                required_inputs = None
                raw_inputs = getattr(args, "required_inputs", None)
                if raw_inputs:
                    text, error = _parse_text_or_file_flag(raw_inputs, "--required-inputs")
                    if error:
                        print(f"kanban boards contract amendment propose: {error}", file=sys.stderr)
                        return 2
                    required_inputs = json.loads(text) if isinstance(text, str) else text
                amendment = kb.propose_contract_amendment(
                    conn, board=board, origin=getattr(args, "origin", "owner"),
                    rationale=getattr(args, "rationale", ""),
                    proposed_contract=contract, diff=diff, required_inputs=required_inputs,
                )
                if as_json:
                    return _emit(amendment)
                print(f"Drafted amendment {amendment['amendment_id']} ({amendment['status']}).")
                return 0

            if action == "submit-inputs":
                inputs, error = _parse_json_object_flag(getattr(args, "inputs", None), "inputs")
                if error:
                    print(f"kanban boards contract amendment submit-inputs: {error}", file=sys.stderr)
                    return 2
                result = kb.submit_amendment_inputs(
                    conn, getattr(args, "amendment_id", ""), inputs, board=board
                )
                if as_json:
                    return _emit(result)
                print(
                    f"Inputs recorded; satisfied={result.get('inputs_satisfied')} "
                    f"missing={result.get('missing_inputs')} status={result['status']}"
                )
                return 0

            if action == "approve":
                approval_evidence, error = _parse_text_or_file_flag(
                    getattr(args, "approval_evidence", None), "--approval-evidence"
                )
                if error:
                    print(f"kanban boards contract amendment approve: {error}", file=sys.stderr)
                    return 2
                result = kb.approve_contract_amendment(
                    conn, getattr(args, "amendment_id", ""), board=board,
                    approver=getattr(args, "approved_by", None) or _current_user_label(),
                    token=getattr(args, "token", ""),
                    approval_evidence=approval_evidence,
                )
                if as_json:
                    return _emit(result)
                print(f"Amendment {result['amendment_id']} status: {result['status']}")
                return 0

            if action == "validate":
                result = kb.validate_contract_amendment(
                    conn, getattr(args, "amendment_id", ""), board=board
                )
                if as_json:
                    return _emit(result)
                report = result.get("validation_report") or {}
                print(f"Amendment {result['amendment_id']} status: {result['status']}")
                for err in report.get("errors") or []:
                    print(f"  - {err}")
                return 0

            if action == "mint":
                result = kb.mint_contract_amendment(
                    conn, getattr(args, "amendment_id", ""), board=board
                )
                if as_json:
                    return _emit(result)
                print(
                    f"Amendment {result['amendment_id']} status: {result['status']}"
                    + (f" (contract_version={result.get('minted_version')})"
                       if result.get("minted_version") else "")
                )
                return 0

            if action == "reject":
                result = kb.reject_contract_amendment(
                    conn, getattr(args, "amendment_id", ""), board=board,
                    reason=getattr(args, "reason", None),
                )
                if as_json:
                    return _emit(result)
                print(f"Amendment {result['amendment_id']} status: {result['status']}")
                return 0
    except kb.ContractVersionConflict as exc:
        print(f"kanban boards contract amendment: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"kanban boards contract amendment: {exc}", file=sys.stderr)
        return 2

    print(f"kanban boards contract amendment: unknown action {action!r}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# P6 conversational CEO steering channel
# ---------------------------------------------------------------------------


def _print_steering_message(msg: dict) -> None:
    role = (msg.get("role") or "").upper()
    print(f"[{role}] {msg.get('content') or ''}")
    att = msg.get("attachments") or {}
    if isinstance(att, dict):
        aid = att.get("amendment_id")
        if aid:
            status = att.get("amendment_status") or ""
            print(f"       (amendment {aid} {status})")
        missing = att.get("missing_inputs")
        if missing:
            print(f"       (required inputs still needed: {', '.join(missing)})")
        errs = att.get("validation_errors")
        if errs:
            for err in errs:
                print(f"       ! {err}")


def _cmd_steer(args: argparse.Namespace) -> int:
    action = getattr(args, "steer_action", None)
    if not action:
        print(
            "usage: hermes kanban steer <open|send|list|show|close|reflect> [options]",
            file=sys.stderr,
        )
        return 2
    board = getattr(args, "board", None) or kb.get_current_board()
    try:
        board = kb._normalize_board_slug(board)
    except ValueError as exc:
        print(f"kanban steer: {exc}", file=sys.stderr)
        return 2
    if not board or not kb.board_exists(board):
        print(f"kanban steer: board {board!r} does not exist", file=sys.stderr)
        return 1
    as_json = bool(getattr(args, "json", False))

    def _emit(payload: Any) -> int:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 0

    try:
        with kb.connect(board=board) as conn:
            if action == "open":
                session = kb.open_steering_session(
                    conn, board=board, mode=getattr(args, "mode", "runtime_evolution"),
                    title=getattr(args, "title", None),
                    amendment_id=getattr(args, "amendment_id", None),
                )
                if as_json:
                    return _emit(session)
                print(
                    f"Opened steering session {session['session_id']} "
                    f"(mode={session['mode']}) on board {board!r}."
                )
                return 0

            if action == "send":
                result = kb.steer_send_message(
                    conn, board=board, session_id=getattr(args, "session_id", ""),
                    owner_message=getattr(args, "message", ""),
                )
                if as_json:
                    return _emit(result)
                _print_steering_message(result["ceo_message"])
                amendment = result.get("amendment")
                if amendment is not None:
                    print(
                        f"\nDrafted P5 amendment {amendment['amendment_id']} "
                        f"({amendment['status']}). Approve it with: "
                        f"hermes kanban boards contract amendment approve "
                        f"{amendment['amendment_id']} --token <token>"
                    )
                elif result.get("degraded"):
                    print("\n(CEO model not configured — degraded mode.)")
                return 0

            if action == "list":
                sessions = kb.list_steering_sessions(
                    conn, board=board, status=getattr(args, "status", None)
                )
                if as_json:
                    return _emit(sessions)
                if not sessions:
                    print("No steering sessions.")
                for s in sessions:
                    print(
                        f"{s['session_id']}  {s['status']:<7} mode={s['mode']}"
                        + (f"  amendment={s['amendment_id']}" if s.get("amendment_id") else "")
                    )
                return 0

            if action == "show":
                session = kb.get_steering_session(
                    conn, getattr(args, "session_id", ""), board=board
                )
                if session is None:
                    print("kanban steer show: session not found", file=sys.stderr)
                    return 1
                messages = kb.get_steering_messages(
                    conn, getattr(args, "session_id", ""), board=board
                )
                if as_json:
                    return _emit({"session": session, "messages": messages})
                print(
                    f"Session {session['session_id']} (mode={session['mode']} "
                    f"status={session['status']})"
                )
                for msg in messages:
                    _print_steering_message(msg)
                return 0

            if action == "close":
                session = kb.close_steering_session(
                    conn, getattr(args, "session_id", ""), board=board
                )
                if as_json:
                    return _emit(session)
                print(f"Closed steering session {session['session_id']}.")
                return 0

            if action == "reflect":
                msg = kb.steer_reflect_amendment_state(
                    conn, session_id=getattr(args, "session_id", ""),
                    amendment_id=getattr(args, "amendment_id", ""), board=board,
                )
                if as_json:
                    return _emit(msg)
                _print_steering_message(msg)
                return 0
    except kb.ContractVersionConflict as exc:
        print(f"kanban steer: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"kanban steer: {exc}", file=sys.stderr)
        return 2

    print(f"kanban steer: unknown action {action!r}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------


def _parse_duration(val) -> Optional[int]:
    """Parse ``30s`` / ``5m`` / ``2h`` / ``1d`` or a raw integer → seconds.

    Returns None for empty input. Raises ValueError on malformed input so
    the CLI can surface a usage error cleanly.
    """
    if val is None or val == "":
        return None
    s = str(val).strip().lower()
    # Bare integer → seconds.
    try:
        return int(s)
    except ValueError:
        pass
    # Suffixed form.
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in units:
        try:
            n = float(s[:-1])
        except ValueError as exc:
            raise ValueError(f"malformed duration {val!r}") from exc
        return int(n * units[s[-1]])
    raise ValueError(f"malformed duration {val!r} (expected 30s, 5m, 2h, 1d, or a number)")


def _cmd_init(args: argparse.Namespace) -> int:
    path = kb.init_db()
    print(f"Kanban DB initialized at {path}")

    # Seed bundled skills (e.g. kanban-worker) into the active profile so
    # the kanban dispatcher can use them without a separate `hermes profile
    # create` step.  This is best-effort — a missing or broken profile is
    # not fatal to `kanban init`.
    try:
        profile_name = get_active_profile_name() or "default"
        profile_dir = get_profile_dir(profile_name)
        result = seed_profile_skills(profile_dir, quiet=True)
        if result:
            copied = result.get("copied", [])
            if copied:
                print(f"Seeded skill(s) into profile {profile_name}: {', '.join(copied)}")
    except Exception:
        pass  # best-effort

    print()
    # Enumerate profiles on disk so the user knows what assignees are
    # already addressable. Multica does this auto-detection on its
    # daemon start; we do it here at init time instead because our
    # dispatcher doesn't need to enumerate — we just pass the name
    # through to `hermes -p <name>`.
    try:
        profiles = kb.list_profiles_on_disk()
    except Exception:
        profiles = []
    if profiles:
        print(f"Discovered {len(profiles)} profile(s) on disk; any of these can "
              f"be an --assignee:")
        for name in profiles:
            print(f"  {name}")
    else:
        print("No profiles found under ~/.hermes/profiles/.")
        print("Create one with `hermes -p <name> setup` before assigning tasks.")
    print()
    print("Next step: start the gateway so ready tasks actually get picked up.")
    print("  hermes gateway start")
    print()
    print(
        "The gateway hosts an embedded dispatcher that ticks every 60 seconds\n"
        "by default (config: kanban.dispatch_interval_seconds). Without a\n"
        "running gateway, tasks stay in 'ready' forever."
    )
    return 0


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        ok = kb.heartbeat_worker(
            conn,
            args.task_id,
            note=getattr(args, "note", None),
            expected_run_id=_worker_run_id_for(args.task_id),
        )
    if not ok:
        print(f"cannot heartbeat {args.task_id} (not running?)", file=sys.stderr)
        return 1
    print(f"Heartbeat recorded for {args.task_id}")
    return 0


def _cmd_assignees(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        data = kb.known_assignees(conn)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    if not data:
        print("(no assignees — create a profile with `hermes -p <name> setup`)")
        return 0
    # Header
    print(f"{'NAME':20s}  {'ON DISK':8s}  COUNTS")
    for entry in data:
        on_disk = "yes" if entry["on_disk"] else "no"
        counts = entry["counts"] or {}
        count_str = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "(idle)"
        print(f"{entry['name']:20s}  {on_disk:8s}  {count_str}")
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    try:
        ws_kind, ws_path = _parse_workspace_flag(args.workspace)
        branch_name = _parse_branch_flag(getattr(args, "branch", None))
    except argparse.ArgumentTypeError as exc:
        print(f"kanban: {exc}", file=sys.stderr)
        return 2
    if branch_name and ws_kind != "worktree":
        print("kanban: --branch is only valid with --workspace worktree", file=sys.stderr)
        return 2
    try:
        max_runtime = _parse_duration(getattr(args, "max_runtime", None))
    except ValueError as exc:
        print(f"kanban: --max-runtime: {exc}", file=sys.stderr)
        return 2
    max_retries = getattr(args, "max_retries", None)
    if max_retries is not None and max_retries < 1:
        print(
            f"kanban: --max-retries must be >= 1 (got {max_retries}); "
            "use 1 to trip on the first failure.",
            file=sys.stderr,
        )
        return 2
    funnel_data, funnel_error = _parse_json_object_flag(
        getattr(args, "funnel_data", None), "--funnel-data"
    )
    if funnel_error:
        print(f"kanban: {funnel_error}", file=sys.stderr)
        return 2
    try:
        with kb.connect_closing() as conn:
            task_id = kb.create_task(
                conn,
                title=args.title,
                body=args.body,
                assignee=args.assignee,
                created_by=args.created_by or _profile_author(),
                workspace_kind=ws_kind,
                workspace_path=ws_path,
                branch_name=branch_name,
                tenant=args.tenant,
                priority=args.priority,
                parents=tuple(args.parent or ()),
                triage=bool(getattr(args, "triage", False)),
                idempotency_key=getattr(args, "idempotency_key", None),
                max_runtime_seconds=max_runtime,
                skills=getattr(args, "skills", None) or None,
                max_retries=max_retries,
                initial_status=getattr(args, "initial_status", None),
                goal_id=getattr(args, "goal", None),
                workstream_id=getattr(args, "workstream", None),
                stage_key=getattr(args, "stage", None),
                action_key=getattr(args, "action", None),
                funnel_data=funnel_data,
                board=getattr(args, "board", None),
            )
            task = kb.get_task(conn, task_id)
    except ValueError as exc:
        print(f"kanban: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(_task_to_dict(task), indent=2, ensure_ascii=False))
    else:
        print(f"Created {task_id}  ({task.status}, assignee={task.assignee or '-'})")

        # Warn when the task would sit in `ready` because no dispatcher is
        # present. Only warn on ready+assigned tasks — triage/todo are
        # expected to sit idle until promoted, and unassigned tasks
        # can't be dispatched. Skipped in --json mode so the stdout
        # stream stays strictly machine-parseable for callers (the JSON
        # response itself carries enough info for them to decide if
        # they want to check dispatcher presence separately).
        if task.status == "ready" and task.assignee:
            running, message = _check_dispatcher_presence()
            if not running and message:
                print(f"\n⚠  {message}", file=sys.stderr)
    return 0


def _cmd_swarm(args: argparse.Namespace) -> int:
    try:
        workers = [ks.parse_worker_arg(raw) for raw in (args.worker or [])]
    except ValueError as exc:
        print(f"kanban swarm: {exc}", file=sys.stderr)
        return 2
    if not workers:
        print("kanban swarm: at least one --worker is required", file=sys.stderr)
        return 2
    with kb.connect_closing() as conn:
        created = ks.create_swarm(
            conn,
            goal=args.goal,
            workers=workers,
            verifier_assignee=args.verifier,
            synthesizer_assignee=args.synthesizer,
            tenant=args.tenant,
            created_by=args.created_by or _profile_author(),
            priority=args.priority,
            idempotency_key=getattr(args, "idempotency_key", None),
        )
    if getattr(args, "json", False):
        print(json.dumps(created.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"Swarm root: {created.root_id}")
        print("Workers: " + ", ".join(created.worker_ids))
        print(f"Verifier: {created.verifier_id}")
        print(f"Synthesizer: {created.synthesizer_id}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    assignee = args.assignee
    if args.mine and not assignee:
        assignee = _profile_author()
    with kb.connect_closing() as conn:
        # Cheap "mini-dispatch": recompute ready so list output reflects
        # dependencies that may have cleared since the last dispatcher tick.
        kb.recompute_ready(conn)
        tasks = kb.list_tasks(
            conn,
            assignee=assignee,
            status=args.status,
            tenant=args.tenant,
            session_id=args.session,
            include_archived=args.archived,
            order_by=getattr(args, "sort", None),
            workflow_template_id=args.workflow_template_id,
            current_step_key=args.current_step_key,
        )
    if getattr(args, "json", False):
        print(json.dumps([_task_to_dict(t) for t in tasks], indent=2, ensure_ascii=False))
        return 0
    # Passive discoverability: when the user has multiple boards, surface
    # which one they're looking at in the list header. Single-board users
    # never see this — the feature stays invisible until you opt in.
    try:
        all_boards = kb.list_boards(include_archived=False)
    except Exception:
        all_boards = []
    if len(all_boards) > 1:
        current = kb.get_current_board()
        other_count = len(all_boards) - 1
        print(
            f"Board: {current} "
            f"({other_count} other board{'s' if other_count != 1 else ''} — "
            f"`hermes kanban boards list`)\n"
        )
    if not tasks:
        print("(no matching tasks)")
        return 0
    for t in tasks:
        print(_fmt_task_line(t))
    return 0


def _diagnostics_config_for_current_board() -> dict[str, Any]:
    from hermes_cli import kanban_diagnostics as kd
    from hermes_cli.config import load_config

    diag_config = kd.config_from_runtime_config(load_config())
    board = kb.get_current_board()
    try:
        diag_config["launch_gate"] = kb.board_dispatch_gate(board)
    except Exception as exc:
        diag_config["launch_gate"] = {
            "ok": False,
            "board": board or kb.DEFAULT_BOARD,
            "launch_phase": "unknown",
            "reason": f"launch gate check failed: {exc}",
            "blockers": [{"code": "launch_gate_check_failed", "error": str(exc)}],
            "readiness": {
                "ok": False,
                "status": "invalid",
                "errors": [str(exc)],
                "missing": [],
                "questions": [],
            },
        }
    return diag_config


def _cmd_show(args: argparse.Namespace) -> int:
    rsk = _run_state_kwargs(args)
    if rsk is None:
        print(
            "kanban show: pass both --state-type and --state-name, or omit both",
            file=sys.stderr,
        )
        return 2
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, args.task_id)
        if not task:
            print(f"no such task: {args.task_id}", file=sys.stderr)
            return 1
        comments = kb.list_comments(conn, args.task_id)
        events = kb.list_events(conn, args.task_id)
        parents = kb.parent_ids(conn, args.task_id)
        children = kb.child_ids(conn, args.task_id)
        runs = kb.list_runs(conn, args.task_id, **rsk)
        watch_routes = kb.list_watch_routes(conn, args.task_id)
        # Workers hand off via ``task_runs.summary`` (kanban-worker skill);
        # ``tasks.result`` is left NULL unless the caller explicitly passed
        # ``result=``. Surfacing the latest summary here keeps ``show`` from
        # looking like a no-op when the worker actually did real work.
        latest_summary = kb.latest_summary(conn, args.task_id)

    if getattr(args, "json", False):
        payload = {
            "task": _task_to_dict(task),
            "latest_summary": latest_summary,
            "parents": parents,
            "children": children,
            "watch_routes": [kb.watch_route_to_dict(route) for route in watch_routes],
            "comments": [
                {"author": c.author, "body": c.body, "created_at": c.created_at}
                for c in comments
            ],
            "events": [
                {
                    "kind": e.kind,
                    "payload": e.payload,
                    "created_at": e.created_at,
                    "run_id": e.run_id,
                }
                for e in events
            ],
            "runs": [
                {
                    "id": r.id,
                    "profile": r.profile,
                    "step_key": r.step_key,
                    "status": r.status,
                    "outcome": r.outcome,
                    "summary": r.summary,
                    "error": r.error,
                    "metadata": r.metadata,
                    "worker_pid": r.worker_pid,
                    "started_at": r.started_at,
                    "ended_at": r.ended_at,
                }
                for r in runs
            ],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(f"Task {task.id}: {task.title}")
    print(f"  status:    {task.status}")
    print(f"  assignee:  {task.assignee or '-'}")
    if task.tenant:
        print(f"  tenant:    {task.tenant}")
    print(f"  workspace: {task.workspace_kind}" +
          (f" @ {task.workspace_path}" if task.workspace_path else ""))
    if task.branch_name:
        print(f"  branch:    {task.branch_name}")
    if task.skills:
        print(f"  skills:    {', '.join(task.skills)}")
    if task.model_override:
        print(f"  model:     {task.model_override}")
    # Effective retry threshold. Show the per-task override if set,
    # otherwise the dispatcher's resolved value from config (or the
    # default if config doesn't set it either). Helps operators see
    # why a task auto-blocked earlier/later than they expected.
    if task.max_retries is not None:
        print(f"  max-retries: {task.max_retries} (task)")
    else:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            cfg_val = (cfg.get("kanban", {}) or {}).get("failure_limit")
        except Exception:
            cfg_val = None
        if cfg_val is not None and int(cfg_val) != kb.DEFAULT_FAILURE_LIMIT:
            print(f"  max-retries: {int(cfg_val)} (config kanban.failure_limit)")
        else:
            print(f"  max-retries: {kb.DEFAULT_FAILURE_LIMIT} (default)")
    print(f"  created:   {_fmt_ts(task.created_at)} by {task.created_by or '-'}")

    # Diagnostics section — surface active distress signals at the top
    # of show output so CLI users see them before scrolling through
    # comments / runs.
    from hermes_cli import kanban_diagnostics as kd
    diags = kd.compute_task_diagnostics(
        task,
        events,
        runs,
        config=_diagnostics_config_for_current_board(),
    )
    if diags:
        sev_marker = {"warning": "⚠", "error": "!!", "critical": "!!!"}
        print(f"\n  Diagnostics ({len(diags)}):")
        for d in diags:
            print(f"    {sev_marker.get(d.severity, '?')} [{d.severity}] {d.title}")
            if d.data:
                bits = []
                for k, v in d.data.items():
                    if isinstance(v, list):
                        bits.append(f"{k}={','.join(str(x) for x in v)}")
                    else:
                        bits.append(f"{k}={v}")
                if bits:
                    print(f"       data: {' | '.join(bits)}")
            # Only show suggested actions in show output to keep it tight;
            # full list is available via `kanban diagnostics --task <id>`.
            for a in d.actions:
                if a.suggested:
                    print(f"       → {a.label}")
    if task.started_at:
        print(f"  started:   {_fmt_ts(task.started_at)}")
    if task.completed_at:
        print(f"  completed: {_fmt_ts(task.completed_at)}")
    if parents:
        print(f"  parents:   {', '.join(parents)}")
    if children:
        print(f"  children:  {', '.join(children)}")
    if watch_routes:
        print()
        print(f"Watch routes ({len(watch_routes)}):")
        for route in watch_routes:
            state = "active" if route.active else "inactive"
            key = f":{route.trigger_key}" if route.trigger_key else ""
            print(
                f"  #{route.id} {state} {route.trigger_type}{key} -> {route.wake_status}"
                + (f" — {route.reason}" if route.reason else "")
            )
    if task.body:
        print()
        print("Body:")
        print(task.body)
    if task.result:
        print()
        print("Result:")
        print(task.result)
    elif latest_summary:
        # Worker handoff lives on the latest run, not on tasks.result.
        # Surface it at top-level so a glance at ``hermes kanban show <id>``
        # tells you what the worker did even if tasks.result is empty.
        print()
        print("Latest summary:")
        print(latest_summary)
    if comments:
        print()
        print(f"Comments ({len(comments)}):")
        for c in comments:
            print(f"  [{_fmt_ts(c.created_at)}] {c.author}: {c.body}")
    if events:
        print()
        print(f"Events ({len(events)}):")
        for e in events[-20:]:
            pl = f" {e.payload}" if e.payload else ""
            run_tag = f" [run {e.run_id}]" if e.run_id else ""
            print(f"  [{_fmt_ts(e.created_at)}]{run_tag} {e.kind}{pl}")
    if runs:
        print()
        print(f"Runs ({len(runs)}):")
        for r in runs:
            # Clamp to 0 so NTP backward-jumps don't print negative seconds.
            elapsed = (max(0, r.ended_at - r.started_at)
                       if r.ended_at else None)
            el = f"{elapsed}s" if elapsed is not None else "active"
            outcome = r.outcome or r.status or "active"
            print(f"  #{r.id:<3} {outcome:<12} @{r.profile or '-'}  {el}  "
                  f"{_fmt_ts(r.started_at)}")
            if r.summary:
                print(f"        → {r.summary.splitlines()[0][:160]}")
            if r.error:
                print(f"        ! {r.error.splitlines()[0][:160]}")
    return 0


def _cmd_assign(args: argparse.Namespace) -> int:
    profile = None if args.profile.lower() in {"none", "-", "null"} else args.profile
    with kb.connect_closing() as conn:
        ok = kb.assign_task(conn, args.task_id, profile)
    if not ok:
        print(f"no such task: {args.task_id}", file=sys.stderr)
        return 1
    print(f"Assigned {args.task_id} to {profile or '(unassigned)'}")
    return 0


def _cmd_reclaim(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        ok = kb.reclaim_task(
            conn, args.task_id,
            reason=getattr(args, "reason", None),
        )
    if not ok:
        print(
            f"cannot reclaim {args.task_id} (not running or unknown id)",
            file=sys.stderr,
        )
        return 1
    print(f"Reclaimed {args.task_id}")
    return 0


def _cmd_reassign(args: argparse.Namespace) -> int:
    profile = None if args.profile.lower() in {"none", "-", "null"} else args.profile
    with kb.connect_closing() as conn:
        ok = kb.reassign_task(
            conn, args.task_id, profile,
            reclaim_first=bool(getattr(args, "reclaim", False)),
            reason=getattr(args, "reason", None),
        )
    if not ok:
        print(
            f"cannot reassign {args.task_id} "
            f"(unknown id, or still running — pass --reclaim to release first)",
            file=sys.stderr,
        )
        return 1
    print(
        f"Reassigned {args.task_id} to "
        f"{profile or '(unassigned)'}"
        + (" (claim reclaimed)" if getattr(args, "reclaim", False) else "")
    )
    return 0


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    """List active diagnostics on the board. Wraps the same rule engine
    the dashboard uses, so CLI output matches what the UI shows.
    """
    from hermes_cli import kanban_diagnostics as kd

    diag_config = _diagnostics_config_for_current_board()

    with kb.connect_closing() as conn:
        # Either one-task mode or fleet mode.
        if getattr(args, "task", None):
            task = kb.get_task(conn, args.task)
            if task is None:
                print(f"no such task: {args.task}", file=sys.stderr)
                return 1
            diags_by_task = {
                args.task: kd.compute_task_diagnostics(
                    task,
                    kb.list_events(conn, args.task),
                    kb.list_runs(conn, args.task),
                    config=diag_config,
                )
            }
        else:
            # Fleet mode: pull all non-archived tasks + their events/runs.
            rows = list(conn.execute(
                "SELECT * FROM tasks WHERE status != 'archived'"
            ).fetchall())
            ids = [r["id"] for r in rows]
            if not ids:
                diags_by_task = {}
            else:
                placeholders = ",".join(["?"] * len(ids))
                ev_by = {i: [] for i in ids}
                for row in conn.execute(
                    f"SELECT * FROM task_events WHERE task_id IN ({placeholders}) ORDER BY id",
                    tuple(ids),
                ):
                    ev_by.setdefault(row["task_id"], []).append(row)
                run_by = {i: [] for i in ids}
                for row in conn.execute(
                    f"SELECT * FROM task_runs WHERE task_id IN ({placeholders}) ORDER BY id",
                    tuple(ids),
                ):
                    run_by.setdefault(row["task_id"], []).append(row)
                diags_by_task = {}
                for r in rows:
                    tid = r["id"]
                    dl = kd.compute_task_diagnostics(
                        r,
                        ev_by.get(tid, []),
                        run_by.get(tid, []),
                        config=diag_config,
                    )
                    if dl:
                        diags_by_task[tid] = dl

        # Severity filter.
        sev = getattr(args, "severity", None)
        if sev:
            for tid in list(diags_by_task.keys()):
                kept = [d for d in diags_by_task[tid] if kd.SEVERITY_ORDER.index(d.severity) >= kd.SEVERITY_ORDER.index(sev)]
                if kept:
                    diags_by_task[tid] = kept
                else:
                    del diags_by_task[tid]

        # Map task_id → title/status/assignee for the table output.
        meta: dict[str, dict] = {}
        if diags_by_task:
            placeholders = ",".join(["?"] * len(diags_by_task))
            for r in conn.execute(
                f"SELECT id, title, status, assignee FROM tasks WHERE id IN ({placeholders})",
                tuple(diags_by_task.keys()),
            ):
                meta[r["id"]] = {
                    "title": r["title"], "status": r["status"],
                    "assignee": r["assignee"],
                }

    if getattr(args, "json", False):
        out_json = [
            {
                "task_id": tid,
                **meta.get(tid, {}),
                "diagnostics": [d.to_dict() for d in dl],
            }
            for tid, dl in diags_by_task.items()
        ]
        print(json.dumps(out_json, indent=2, ensure_ascii=False))
        return 0

    if not diags_by_task:
        print("No active diagnostics on this board.")
        return 0

    # Human-readable summary: grouped by task, severity-marked, with
    # suggested actions inline.
    sev_marker = {"warning": "⚠", "error": "!!", "critical": "!!!"}
    total = sum(len(dl) for dl in diags_by_task.values())
    print(
        f"{total} active diagnostic(s) across "
        f"{len(diags_by_task)} task(s):\n"
    )
    for tid, dl in diags_by_task.items():
        m = meta.get(tid, {})
        title = m.get("title") or "(untitled)"
        status = m.get("status") or "?"
        assignee = m.get("assignee") or "(unassigned)"
        print(f"  {tid}  {status:8s}  @{assignee:18s}  {title}")
        for d in dl:
            print(f"    {sev_marker.get(d.severity, '?')} [{d.severity}] {d.kind}: {d.title}")
            if d.data:
                # Compact key:value pairs on one line.
                bits = []
                for k, v in d.data.items():
                    if isinstance(v, list):
                        bits.append(f"{k}={','.join(str(x) for x in v)}")
                    else:
                        bits.append(f"{k}={v}")
                if bits:
                    print(f"       data: {' | '.join(bits)}")
            # Suggested actions first.
            for a in d.actions:
                if a.suggested:
                    print(f"       → {a.label}")
        print()
    return 0


def _cmd_link(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        kb.link_tasks(conn, args.parent_id, args.child_id)
    print(f"Linked {args.parent_id} -> {args.child_id}")
    return 0


def _cmd_unlink(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        ok = kb.unlink_tasks(conn, args.parent_id, args.child_id)
    if not ok:
        print(f"No such link: {args.parent_id} -> {args.child_id}", file=sys.stderr)
        return 1
    print(f"Unlinked {args.parent_id} -> {args.child_id}")
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        task = kb.claim_task(conn, args.task_id, ttl_seconds=args.ttl)
        if task is None:
            # Report why
            existing = kb.get_task(conn, args.task_id)
            if existing is None:
                print(f"no such task: {args.task_id}", file=sys.stderr)
                return 1
            print(
                f"cannot claim {args.task_id}: status={existing.status} "
                f"lock={existing.claim_lock or '(none)'}",
                file=sys.stderr,
            )
            return 1
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, task.id, str(workspace))
    print(f"Claimed {task.id}")
    print(f"Workspace: {workspace}")
    return 0


def _cmd_comment(args: argparse.Namespace) -> int:
    body = " ".join(args.text).strip()
    if args.max_len is not None:
        if args.max_len < 1:
            print("kanban: --max-len must be positive", file=sys.stderr)
            return 2
        if len(body) > args.max_len:
            suffix = f"\n\n[trimmed to {args.max_len} chars by --max-len]"
            body = body[: max(0, args.max_len - len(suffix))].rstrip() + suffix
    author = args.author or _profile_author()
    with kb.connect_closing() as conn:
        kb.add_comment(conn, args.task_id, author, body)
    print(f"Comment added to {args.task_id}")
    return 0


def _cmd_pixel(args: argparse.Namespace) -> int:
    action = getattr(args, "pixel_action", None) or "brief"
    as_json = bool(getattr(args, "json", False))

    if action == "brief":
        with kb.connect() as conn:
            brief = kb.pixel_brief(conn)
        if as_json:
            print(json.dumps(brief, indent=2, ensure_ascii=False))
        else:
            print(
                f"Pixel board {brief['board']}: "
                f"{'enabled' if brief.get('pixel_enabled') else 'disabled'}"
            )
            if brief.get("goal"):
                print(f"Goal: {brief['goal']}")
            print(f"Done gates: {len(brief.get('done_gates') or [])}")
            blockers = brief.get("blockers") or []
            if blockers:
                print(f"Blockers: {len(blockers)}")
        return 0

    if action == "goal":
        payload = kb.set_pixel_goal(args.board, args.text, args.success)
        if as_json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            pixel = payload.get("pixel") or {}
            print(
                f"Pixel goal set for {payload.get('slug') or payload.get('name') or args.board or kb.get_current_board()}: "
                f"{pixel.get('goal')}"
            )
        return 0

    if action == "stage":
        payload = kb.set_pixel_stage_event(args.board, args.stage_key, args.conversion_event)
        if as_json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            stage = kb._normalize_funnel_text(args.stage_key)
            event = kb._normalize_funnel_text(args.conversion_event)
            print(f"Pixel stage mapped: {stage} -> {event}")
        return 0

    if action == "event":
        with kb.connect() as conn:
            event = kb.record_pixel_event(
                conn,
                event_type=args.type,
                stage_key=args.stage_key,
                status=args.status,
                evidence=args.evidence,
                task_id=args.task_id,
            )
        payload = kb.pixel_event_to_dict(event)
        if as_json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(
                f"Pixel event recorded: {payload['type']} "
                f"({payload['status']}) for {payload.get('task_id') or 'board'}"
            )
        return 0

    if action == "claim":
        with kb.connect() as conn:
            claim = kb.claim_pixel_lane(
                conn,
                lane_id=args.lane_id,
                task_id=args.task_id,
                agent_id=args.agent_id,
                evidence=args.evidence,
            )
        payload = kb.pixel_claim_to_dict(claim)
        if as_json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"Pixel claim {payload['id']} active on lane {payload['lane_id']}")
        return 0

    if action == "release":
        with kb.connect() as conn:
            claim = kb.release_pixel_claim(
                conn,
                claim_id=args.claim_id,
                agent_id=args.agent_id,
                evidence=args.evidence,
            )
        payload = kb.pixel_claim_to_dict(claim)
        if as_json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"Pixel claim {payload['id']} released")
        return 0

    if action == "done":
        with kb.connect() as conn:
            task_id = getattr(args, "task_id", None)
            if task_id:
                verdict = kb.validate_pixel_done(conn, task_id)
                if as_json:
                    print(json.dumps(verdict, indent=2, ensure_ascii=False))
                else:
                    status = "pass" if verdict.get("ok") else "fail"
                    print(f"Pixel done gate {status}: {task_id}")
                    for blocker in verdict.get("blockers") or []:
                        print(f"- {blocker.get('code')}: {blocker.get('message')}")
                return 0 if verdict.get("ok") else 1

            brief = kb.pixel_brief(conn)
        if as_json:
            print(json.dumps(brief, indent=2, ensure_ascii=False))
        else:
            gates = brief.get("done_gates") or []
            passed = sum(1 for gate in gates if gate.get("ok"))
            print(f"Pixel done gates: {passed}/{len(gates)} ok")
            for gate in gates:
                if not gate.get("ok"):
                    codes = ", ".join(
                        str(blocker.get("code"))
                        for blocker in gate.get("blockers", [])
                        if blocker.get("code")
                    )
                    print(f"- {gate.get('task_id')}: {codes or 'blocked'}")
        return 0 if all(gate.get("ok") for gate in brief.get("done_gates", [])) else 1

    print(f"kanban pixel: unknown action {action!r}", file=sys.stderr)
    return 2


def _worker_run_id_for(task_id: str) -> Optional[int]:
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _cmd_complete(args: argparse.Namespace) -> int:
    """Mark one or more tasks done. Supports a single id or a list."""
    ids = list(args.task_ids or [])
    if not ids:
        print("at least one task_id is required", file=sys.stderr)
        return 1
    summary = getattr(args, "summary", None)
    raw_meta = getattr(args, "metadata", None)
    # Guard: structured handoff fields are per-run, so they'd be
    # copy-pasted identically across N runs — almost always a footgun.
    # Refuse instead of silently doing the wrong thing.
    if len(ids) > 1 and (summary or raw_meta):
        print(
            "kanban: --summary / --metadata are per-task and can't be used "
            "with multiple ids (would apply the same handoff to every task). "
            "Complete tasks one at a time, or drop the flags for the bulk close.",
            file=sys.stderr,
        )
        return 2
    metadata = None
    if raw_meta:
        try:
            metadata = json.loads(raw_meta)
            if not isinstance(metadata, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"kanban: --metadata: {exc}", file=sys.stderr)
            return 2
    failed: list[str] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            try:
                completed = kb.complete_task(
                    conn, tid,
                    result=args.result,
                    summary=summary,
                    metadata=metadata,
                    expected_run_id=_worker_run_id_for(tid),
                )
            except kb.ContractDoneGateError as exc:
                failed.append(tid)
                if getattr(args, "json", False):
                    print(json.dumps(exc.verdict, indent=2, ensure_ascii=False))
                else:
                    blockers = exc.verdict.get("blockers") or []
                    codes = ", ".join(
                        str(blocker.get("code"))
                        for blocker in blockers
                        if blocker.get("code")
                    )
                    print(
                        f"kanban: contract done gate failed for {tid}: "
                        f"{codes or 'blocked'}",
                        file=sys.stderr,
                    )
                continue
            except kb.PixelDoneGateError as exc:
                failed.append(tid)
                if getattr(args, "json", False):
                    print(json.dumps(exc.verdict, indent=2, ensure_ascii=False))
                else:
                    blockers = exc.verdict.get("blockers") or []
                    codes = ", ".join(
                        str(blocker.get("code"))
                        for blocker in blockers
                        if blocker.get("code")
                    )
                    print(
                        f"kanban: pixel done gate failed for {tid}: "
                        f"{codes or 'blocked'}",
                        file=sys.stderr,
                    )
                continue
            if not completed:
                failed.append(tid)
                print(f"cannot complete {tid} (unknown id or terminal state)", file=sys.stderr)
            else:
                print(f"Completed {tid}")
    return 0 if not failed else 1


def _cmd_edit(args: argparse.Namespace) -> int:
    raw_meta = getattr(args, "metadata", None)
    metadata = None
    if raw_meta:
        try:
            metadata = json.loads(raw_meta)
            if not isinstance(metadata, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"kanban: --metadata: {exc}", file=sys.stderr)
            return 2
    with kb.connect_closing() as conn:
        if not kb.edit_completed_task_result(
            conn,
            args.task_id,
            result=args.result,
            summary=getattr(args, "summary", None),
            metadata=metadata,
        ):
            print(
                f"cannot edit {args.task_id} (unknown id or task is not done)",
                file=sys.stderr,
            )
            return 1
    print(f"Edited {args.task_id}")
    return 0


def _cmd_block(args: argparse.Namespace) -> int:
    reason = " ".join(args.reason).strip() if args.reason else None
    author = _profile_author()
    ids = [args.task_id] + list(getattr(args, "ids", None) or [])
    failed: list[str] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            if reason:
                kb.add_comment(conn, tid, author, f"BLOCKED: {reason}")
            if not kb.block_task(
                conn,
                tid,
                reason=reason,
                expected_run_id=_worker_run_id_for(tid),
            ):
                failed.append(tid)
                print(f"cannot block {tid}", file=sys.stderr)
            else:
                print(f"Blocked {tid}" + (f": {reason}" if reason else ""))
    return 0 if not failed else 1


def _cmd_schedule(args: argparse.Namespace) -> int:
    reason = " ".join(args.reason).strip() if args.reason else None
    author = _profile_author()
    ids = [args.task_id] + list(getattr(args, "ids", None) or [])
    failed: list[str] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            if reason:
                kb.add_comment(conn, tid, author, f"SCHEDULED: {reason}")
            if not kb.schedule_task(
                conn,
                tid,
                reason=reason,
                expected_run_id=_worker_run_id_for(tid),
            ):
                failed.append(tid)
                print(f"cannot schedule {tid}", file=sys.stderr)
            else:
                print(f"Scheduled {tid}" + (f": {reason}" if reason else ""))
    return 0 if not failed else 1


def _cmd_wait(args: argparse.Namespace) -> int:
    payload, payload_error = _parse_json_object_flag(getattr(args, "payload", None), "--payload")
    if payload_error:
        print(f"kanban wait: {payload_error}", file=sys.stderr)
        return 2
    with kb.connect() as conn:
        route = kb.set_task_watching(
            conn,
            args.task_id,
            trigger_type=args.trigger_type,
            trigger_key=args.trigger_key,
            reason=args.reason,
            payload=payload,
            wake_status=args.wake_status,
            created_by=_profile_author(),
            expected_run_id=_worker_run_id_for(args.task_id),
        )
    if route is None:
        print(f"cannot watch {args.task_id} (unknown task or incompatible status)", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(kb.watch_route_to_dict(route), indent=2, ensure_ascii=False))
    else:
        key = f":{route.trigger_key}" if route.trigger_key else ""
        print(f"Watching {route.task_id} via {route.trigger_type}{key} -> {route.wake_status}")
    return 0


def _cmd_trigger(args: argparse.Namespace) -> int:
    payload, payload_error = _parse_json_object_flag(getattr(args, "payload", None), "--payload")
    if payload_error:
        print(f"kanban trigger: {payload_error}", file=sys.stderr)
        return 2
    with kb.connect() as conn:
        routes = kb.trigger_watch(
            conn,
            route_id=getattr(args, "route_id", None),
            task_id=getattr(args, "task_id", None),
            trigger_type=getattr(args, "trigger_type", None),
            trigger_key=getattr(args, "trigger_key", None),
            payload=payload,
            actor=getattr(args, "actor", None) or _profile_author(),
        )
    if getattr(args, "json", False):
        print(json.dumps([kb.watch_route_to_dict(route) for route in routes], indent=2, ensure_ascii=False))
    else:
        if not routes:
            print("No active watch routes matched.")
        for route in routes:
            print(f"Triggered watch route #{route.id} for {route.task_id}")
    return 0


def _cmd_transition(args: argparse.Namespace) -> int:
    evidence = list(getattr(args, "evidence", None) or [])
    try:
        with kb.connect() as conn:
            task = kb.transition_task_stage(
                conn,
                args.task_id,
                to_stage=args.to_stage,
                evidence=evidence,
                action_key=getattr(args, "action", None),
                actor=getattr(args, "actor", None) or _profile_author(),
                board=getattr(args, "board", None),
            )
    except ValueError as exc:
        print(f"kanban transition: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(_task_to_dict(task), indent=2, ensure_ascii=False))
    else:
        print(f"Transitioned {task.id} to stage={task.stage_key}" + (f" action={task.action_key}" if task.action_key else ""))
    return 0


def _cmd_unblock(args: argparse.Namespace) -> int:
    ids = list(args.task_ids or [])
    if not ids:
        print("at least one task_id is required", file=sys.stderr)
        return 1
    reason = getattr(args, "reason", None)
    if reason is not None:
        reason = reason.strip() or None
    author = _profile_author() if reason else None
    failed: list[str] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            if reason:
                kb.add_comment(conn, tid, author, f"UNBLOCK: {reason}")
            if not kb.unblock_task(conn, tid):
                failed.append(tid)
                print(f"cannot unblock {tid} (not blocked/scheduled/watching?)", file=sys.stderr)
            else:
                print(f"Unblocked {tid}" + (f": {reason}" if reason else ""))
    return 0 if not failed else 1


def _cmd_promote(args: argparse.Namespace) -> int:
    reason = " ".join(args.reason).strip() if args.reason else None
    author = _profile_author()
    as_json = getattr(args, "json", False)
    extra_ids = list(getattr(args, "ids", None) or [])
    # Dedupe while preserving order; positional task_id always first.
    ids: list[str] = []
    seen: set[str] = set()
    for tid in [args.task_id, *extra_ids]:
        if tid not in seen:
            ids.append(tid)
            seen.add(tid)

    results: list[dict[str, object]] = []
    with kb.connect_closing() as conn:
        for tid in ids:
            ok, err = kb.promote_task(
                conn,
                tid,
                actor=author,
                reason=reason,
                force=bool(args.force),
                dry_run=bool(args.dry_run),
            )
            results.append({
                "task_id": tid,
                "promoted": ok,
                "dry_run": bool(args.dry_run),
                "forced": bool(args.force),
                "reason": reason,
                "error": err,
            })

    failed = [r for r in results if not r["promoted"]]
    if as_json:
        # Single-id stays a flat object for back-compat; bulk emits a list.
        payload: object = results[0] if len(results) == 1 else results
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if not failed else 1

    tag = " (dry)" if args.dry_run else ""
    label = "Would promote" if args.dry_run else "Promoted"
    for r in results:
        if r["promoted"]:
            suffix = f": {reason}" if reason else ""
            print(f"{label} {r['task_id']} -> ready{tag}{suffix}")
        else:
            print(f"cannot promote {r['task_id']}: {r['error']}", file=sys.stderr)
    return 0 if not failed else 1


def _cmd_archive(args: argparse.Namespace) -> int:
    ids = list(args.task_ids or [])
    purge_ids = list(getattr(args, "purge_ids", None) or [])
    if ids and purge_ids:
        print("choose either task_ids to archive or --rm archived task_ids", file=sys.stderr)
        return 1
    if not ids and not purge_ids:
        print("at least one task_id is required", file=sys.stderr)
        return 1
    failed: list[str] = []
    with kb.connect_closing() as conn:
        if purge_ids:
            for tid in purge_ids:
                if not kb.delete_archived_task(conn, tid):
                    failed.append(tid)
                    print(f"cannot delete {tid} (must already be archived)", file=sys.stderr)
                else:
                    print(f"Deleted {tid}")
            return 0 if not failed else 1
        for tid in ids:
            if not kb.archive_task(conn, tid):
                failed.append(tid)
                print(f"cannot archive {tid}", file=sys.stderr)
            else:
                print(f"Archived {tid}")
    return 0 if not failed else 1


def _cmd_tail(args: argparse.Namespace) -> int:
    last_id = 0
    print(f"Tailing events for {args.task_id}. Ctrl-C to stop.")
    try:
        while True:
            with kb.connect_closing() as conn:
                events = kb.list_events(conn, args.task_id)
            for e in events:
                if e.id > last_id:
                    pl = f" {e.payload}" if e.payload else ""
                    print(f"[{_fmt_ts(e.created_at)}] {e.kind}{pl}", flush=True)
                    last_id = e.id
            time.sleep(max(0.1, args.interval))
    except KeyboardInterrupt:
        print("\n(stopped)")
        return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    # Honour kanban.default_assignee as the fallback for unassigned ready
    # tasks (#27145), kanban.max_in_progress as the global concurrency cap
    # (#33488), kanban.max_in_progress_per_profile as the per-profile
    # cap (#21582), and kanban.max_spawn as the per-tick spawn limit
    # (#28805). Same semantics as the gateway dispatch path so behavior
    # matches whether the user runs the CLI directly or relies on the
    # gateway-embedded dispatcher.
    try:
        from hermes_cli.config import load_config
        _cfg = load_config()
        _kanban_cfg = _cfg.get("kanban", {}) if isinstance(_cfg, dict) else {}
        default_assignee = (_kanban_cfg.get("default_assignee") or "").strip() or None

        def _coerce_positive_int(value):
            if value is None:
                return None
            try:
                ival = int(value)
            except (TypeError, ValueError):
                return None
            return ival if ival >= 1 else None

        max_in_progress_per_profile = _coerce_positive_int(
            _kanban_cfg.get("max_in_progress_per_profile")
        )
        max_in_progress = _coerce_positive_int(_kanban_cfg.get("max_in_progress"))
        # CLI --max overrides config kanban.max_spawn when both are present;
        # CLI is the more explicit signal so it wins.
        cli_max = getattr(args, "max", None)
        max_spawn = cli_max if cli_max is not None else _coerce_positive_int(
            _kanban_cfg.get("max_spawn")
        )
    except Exception:
        default_assignee = None
        max_in_progress_per_profile = None
        max_in_progress = None
        max_spawn = getattr(args, "max", None)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn,
            dry_run=args.dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=getattr(args, "failure_limit", kb.DEFAULT_SPAWN_FAILURE_LIMIT),
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
        )
    if getattr(args, "json", False):
        print(json.dumps({
            "reclaimed": res.reclaimed,
            "crashed": res.crashed,
            "timed_out": res.timed_out,
            "stale": res.stale,
            "auto_blocked": res.auto_blocked,
            "promoted": res.promoted,
            "spawned": [
                {"task_id": tid, "assignee": who, "workspace": ws}
                for (tid, who, ws) in res.spawned
            ],
            "skipped_unassigned": res.skipped_unassigned,
            "skipped_nonspawnable": res.skipped_nonspawnable,
            "contract_blocked": [
                {"task_id": tid, "blockers": blockers}
                for (tid, blockers) in res.contract_blocked
            ],
            "launch_blocked": res.launch_blocked,
            "skipped_per_profile_capped": [
                {"task_id": tid, "assignee": who, "current": current}
                for (tid, who, current) in res.skipped_per_profile_capped
            ],
            "auto_assigned_default": res.auto_assigned_default,
        }, indent=2))
        return 0
    print(f"Reclaimed:    {res.reclaimed}")
    print(f"Crashed:      {len(res.crashed)}")
    if res.crashed:
        print(f"  {', '.join(res.crashed)}")
    print(f"Timed out:    {len(res.timed_out)}")
    if res.timed_out:
        print(f"  {', '.join(res.timed_out)}")
    print(f"Stale:        {len(res.stale)}")
    if res.stale:
        print(f"  {', '.join(res.stale)}")
    print(f"Auto-blocked: {len(res.auto_blocked)}")
    if res.auto_blocked:
        print(f"  {', '.join(res.auto_blocked)}")
    print(f"Promoted:     {res.promoted}")
    print(f"Spawned:      {len(res.spawned)}")
    for tid, who, ws in res.spawned:
        tag = " (dry)" if args.dry_run else ""
        print(f"  - {tid}  ->  {who}  @ {ws or '-'}{tag}")
    if res.auto_assigned_default:
        print(
            f"Auto-assigned to kanban.default_assignee={default_assignee!r}: "
            f"{', '.join(res.auto_assigned_default)}"
        )
    if res.skipped_unassigned:
        print(f"Skipped (unassigned): {', '.join(res.skipped_unassigned)}")
    if res.skipped_per_profile_capped:
        for tid, who, current in res.skipped_per_profile_capped:
            print(
                f"Deferred ({who} at per-profile cap, {current} running): {tid}"
            )
    if res.skipped_nonspawnable:
        print(
            f"Skipped (non-spawnable assignee — terminal lane, OK): "
            f"{', '.join(res.skipped_nonspawnable)}"
        )
    if res.contract_blocked:
        print("Contract-blocked:")
        for tid, blockers in res.contract_blocked:
            print(f"  - {tid}: {', '.join(blockers) if blockers else 'blocked'}")
    if res.launch_blocked:
        print("Launch-blocked:")
        for gate in res.launch_blocked:
            board = gate.get("board") or kb.DEFAULT_BOARD
            phase = gate.get("launch_phase") or "unknown"
            reason = gate.get("reason") or "blocked"
            print(f"  - {board}: {reason} (launch_phase={phase})")
    return 0


def _cmd_daemon(args: argparse.Namespace) -> int:
    """Deprecated — the dispatcher now runs inside the gateway.

    Left in as a stub so users with the old command in scripts/systemd
    units get a clear migration message instead of a cryptic
    "no such command" error. A ``--force`` escape hatch keeps the old
    standalone daemon alive for the rare edge case where someone truly
    cannot run the gateway (e.g. running on a host that forbids
    long-lived background services), but the default path exits 2
    with guidance so nobody accidentally keeps running two dispatchers
    against the same kanban.db.

    F4: the ``--force`` loop now runs the FULL per-board tick
    (reactive + optimizer + dispatch across every non-archived board),
    matching the gateway, so it is no longer a dead self-tuning loop.
    """
    # --force lets power users keep the standalone loop for one more
    # release cycle. Undocumented in `--help` so nobody discovers it
    # casually — intentional.
    if not getattr(args, "force", False):
        print(
            "hermes kanban daemon: DEPRECATED — the dispatcher now runs\n"
            "inside the gateway. To use kanban:\n"
            "\n"
            "    hermes gateway start       # starts the gateway + embedded dispatcher\n"
            "\n"
            "Ready tasks will be picked up on the next dispatcher tick\n"
            "(default: every 60 seconds). Configure via config.yaml:\n"
            "\n"
            "    kanban:\n"
            "      dispatch_in_gateway: true      # default\n"
            "      dispatch_interval_seconds: 60\n"
            "      failure_limit: 2              # consecutive non-success attempts before auto-block\n"
            "\n"
            "Running both the gateway AND this standalone daemon will\n"
            "race for claims. If you truly need the old standalone\n"
            "daemon (no gateway available), rerun with --force.",
            file=sys.stderr,
        )
        return 2

    # Legacy path — same logic as before, kept behind --force.
    # Make sure the DB exists before printing "started" so the user sees the
    # correct DB path and any init error surfaces immediately.
    kb.init_db()

    pidfile = getattr(args, "pidfile", None)
    if pidfile:
        try:
            Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
            Path(pidfile).write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            print(f"warning: could not write pidfile {pidfile}: {exc}", file=sys.stderr)

    verbose = bool(getattr(args, "verbose", False))
    print(
        f"Kanban dispatcher running STANDALONE via --force "
        f"(interval={args.interval}s, pid={os.getpid()}). "
        f"Runs the FULL per-board tick (reactive + optimizer + dispatch) "
        f"across every non-archived board, matching the gateway. "
        f"Ctrl-C to stop. NOTE: if a gateway is also running with "
        f"dispatch_in_gateway=true (default), you have two dispatchers "
        f"racing for claims.",
        file=sys.stderr,
    )

    # Health telemetry: warn when every tick finds ready work but fails to
    # spawn any worker. Catches broken profiles, PATH drift, missing venv,
    # credential loss — cases where the per-task circuit breaker auto-blocks
    # each task quietly but the operator has no signal that the dispatcher
    # itself is dysfunctional.
    HEALTH_WINDOW = 6  # ticks (default 30s at interval=5)
    health_state = {"bad_ticks": 0, "last_warn_at": 0}

    def _on_tick(res):
        ready_pending = bool(res.skipped_unassigned) or _ready_queue_nonempty()
        spawned_any = bool(res.spawned)
        if ready_pending and not spawned_any:
            health_state["bad_ticks"] += 1
        else:
            health_state["bad_ticks"] = 0
        # Emit a warning once per HEALTH_WINDOW bad ticks (not every tick)
        # so log volume stays bounded while the problem persists.
        if health_state["bad_ticks"] >= HEALTH_WINDOW:
            now = int(time.time())
            # Rate-limit repeats: at most one warning per 5 minutes.
            if now - health_state["last_warn_at"] >= 300:
                print(
                    f"[{_fmt_ts(now)}] WARN dispatcher stuck: "
                    f"ready queue non-empty for {health_state['bad_ticks']} "
                    f"consecutive ticks but 0 workers spawned successfully. "
                    f"Check profile health (venv, PATH, credentials) and "
                    f"`hermes kanban list --status ready` / "
                    f"`hermes kanban list --status blocked` for recent "
                    f"spawn_failed tasks.",
                    file=sys.stderr, flush=True,
                )
                health_state["last_warn_at"] = now
        if not verbose:
            return
        did_work = (
            res.reclaimed or res.crashed or res.timed_out or res.promoted
            or res.spawned or res.auto_blocked or res.stale
        )
        if did_work:
            print(
                f"[{_fmt_ts(int(time.time()))}] "
                f"reclaimed={res.reclaimed} crashed={len(res.crashed)} "
                f"timed_out={len(res.timed_out)} stale={len(res.stale)} "
                f"promoted={res.promoted} spawned={len(res.spawned)} "
                f"auto_blocked={len(res.auto_blocked)}",
                flush=True,
            )

    def _ready_queue_nonempty() -> bool:
        """Cheap probe — is there at least one ready+assigned+unclaimed
        task whose assignee maps to a real Hermes profile (i.e. one the
        dispatcher would actually try to spawn for)?

        Filters out tasks assigned to control-plane lanes
        (e.g. ``orion-cc``, ``orion-research``) that are pulled by
        terminals via ``claim_task`` directly — those are correctly idle
        from the dispatcher's perspective, not stuck.
        """
        try:
            with kb.connect_closing() as conn:
                return kb.has_spawnable_ready(conn)
        except Exception:
            return False

    try:
        kb.run_daemon(
            interval=args.interval,
            max_spawn=args.max,
            failure_limit=getattr(args, "failure_limit", kb.DEFAULT_SPAWN_FAILURE_LIMIT),
            on_tick=_on_tick,
        )
    finally:
        if pidfile:
            try:
                Path(pidfile).unlink()
            except OSError:
                pass
    print("(dispatcher stopped)")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Live-stream task_events to the terminal."""
    kinds = (
        {k.strip() for k in args.kinds.split(",") if k.strip()}
        if args.kinds else None
    )
    cursor = 0
    print("Watching kanban events. Ctrl-C to stop.", flush=True)
    # Seed cursor at the latest id so we don't replay history.
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
        ).fetchone()
        cursor = int(row["m"])

    try:
        while True:
            with kb.connect_closing() as conn:
                rows = conn.execute(
                    "SELECT e.id, e.task_id, e.kind, e.payload, e.created_at, "
                    "       t.assignee, t.tenant "
                    "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
                    "WHERE e.id > ? ORDER BY e.id ASC LIMIT 200",
                    (cursor,),
                ).fetchall()
            for r in rows:
                cursor = max(cursor, int(r["id"]))
                if kinds and r["kind"] not in kinds:
                    continue
                if args.assignee and r["assignee"] != args.assignee:
                    continue
                if args.tenant and r["tenant"] != args.tenant:
                    continue
                try:
                    payload = json.loads(r["payload"]) if r["payload"] else None
                except Exception:
                    payload = None
                pl = f" {payload}" if payload else ""
                print(
                    f"[{_fmt_ts(r['created_at'])}] {r['task_id']:10s} "
                    f"{r['kind']:18s} (@{r['assignee'] or '-'}){pl}",
                    flush=True,
                )
            time.sleep(max(0.1, args.interval))
    except KeyboardInterrupt:
        print("\n(stopped)")
        return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        stats = kb.board_stats(conn)
    if getattr(args, "json", False):
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0
    print("By status:")
    for k in ("triage", "todo", "scheduled", "ready", "running", "watching", "blocked", "review", "done"):
        print(f"  {k:8s}  {stats['by_status'].get(k, 0)}")
    if stats["by_assignee"]:
        print("\nBy assignee:")
        for who, counts in sorted(stats["by_assignee"].items()):
            parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(f"  {who:20s}  {parts}")
    age = stats["oldest_ready_age_seconds"]
    if age is not None:
        print(f"\nOldest ready task age: {int(age)}s")
    return 0


def _cmd_funnel(args: argparse.Namespace) -> int:
    limit_cards = getattr(args, "limit_cards", 20)
    if limit_cards is not None and limit_cards < 0:
        print("kanban funnel: --limit-cards must be >= 0", file=sys.stderr)
        return 2
    limit_entities = getattr(args, "limit_entities", 50)
    if limit_entities is not None and limit_entities < 0:
        print("kanban funnel: --limit-entities must be >= 0", file=sys.stderr)
        return 2
    with kb.connect() as conn:
        model = kb.build_funnel_read_model(
            conn,
            board=getattr(args, "board", None),
            include_archived=bool(getattr(args, "archived", False)),
            limit_cards_per_stage=limit_cards,
            limit_entities_per_stage=limit_entities,
            goal_id=getattr(args, "goal", None),
            workstream_id=getattr(args, "workstream", None),
            stage_key=getattr(args, "stage", None),
            action_key=getattr(args, "action", None),
        )
    if getattr(args, "json", False):
        print(json.dumps(model, indent=2, ensure_ascii=False))
        return 0
    summary = model["summary"]
    print(f"Board: {model['board']}")
    print(
        f"Cards: {summary['total_cards']}  "
        f"Stages: {summary['stage_count']}  "
        f"Edges: {summary['edge_count']}  "
        f"Entities: {summary.get('entity_count', 0)}"
    )
    if summary["counts_by_status"]:
        parts = ", ".join(
            f"{status}={count}"
            for status, count in summary["counts_by_status"].items()
        )
        print(f"Status: {parts}")
    for stage in model["stages"]:
        metrics = stage["metrics"]
        print(
            f"\n{stage['goal_id']} / {stage['workstream_id']} / "
            f"{stage['stage_key']} / {stage['action_key']}"
        )
        print(
            f"  cards={metrics['total_cards']} active={metrics['active_cards']} "
            f"waiting={metrics['waiting_cards']} blocked={metrics['blocked_cards']} "
            f"done={metrics['done_cards']} artifacts={metrics['artifact_count']} "
            f"entities={stage.get('entity_metrics', {}).get('total_entities', 0)}"
        )
        if stage.get("entity_states"):
            states = ", ".join(
                f"{bucket['state']}={bucket['count']}"
                for bucket in stage["entity_states"]
            )
            print(f"  entity_states: {states}")
        for card in stage["cards"]:
            print(f"  - {card['id']} {card['status']:8s} {card['title']}")
        if stage.get("cards_truncated"):
            print(f"  … {stage['cards_truncated']} more card(s)")
    return 0


def _cmd_funnel_set(args: argparse.Namespace) -> int:
    funnel_data = None
    kwargs: dict[str, Any] = {}
    raw_funnel_data = getattr(args, "funnel_data", None)
    if raw_funnel_data is not None:
        funnel_data, funnel_error = _parse_json_object_flag(raw_funnel_data, "--funnel-data")
        if funnel_error:
            print(f"kanban funnel-set: {funnel_error}", file=sys.stderr)
            return 2
        kwargs["funnel_data"] = funnel_data
    for attr, param in (
        ("goal", "goal_id"),
        ("workstream", "workstream_id"),
        ("stage", "stage_key"),
        ("action", "action_key"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            kwargs[param] = value
    if not kwargs:
        print(
            "kanban funnel-set: pass at least one of --goal, --workstream, "
            "--stage, --action, or --funnel-data",
            file=sys.stderr,
        )
        return 2
    try:
        with kb.connect() as conn:
            task = kb.update_task_funnel_fields(
                conn,
                args.task_id,
                merge_funnel_data=bool(getattr(args, "merge_funnel_data", False)),
                board=getattr(args, "board", None),
                **kwargs,
            )
    except ValueError as exc:
        print(f"kanban funnel-set: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(_task_to_dict(task), indent=2, ensure_ascii=False))
    else:
        print(
            f"Updated {task.id}: goal={task.goal_id or '-'} "
            f"workstream={task.workstream_id or '-'} "
            f"stage={task.stage_key or '-'} action={task.action_key or '-'}"
        )
    return 0


def _cmd_notify_subscribe(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            print(f"no such task: {args.task_id}", file=sys.stderr)
            return 1
        kb.add_notify_sub(
            conn, task_id=args.task_id,
            platform=args.platform, chat_id=args.chat_id,
            thread_id=args.thread_id, user_id=args.user_id,
            notifier_profile=args.notifier_profile or _profile_author(),
        )
    print(f"Subscribed {args.platform}:{args.chat_id}"
          + (f":{args.thread_id}" if args.thread_id else "")
          + f" to {args.task_id}")
    return 0


def _cmd_notify_list(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        subs = kb.list_notify_subs(conn, args.task_id)
    if getattr(args, "json", False):
        print(json.dumps(subs, indent=2, ensure_ascii=False))
        return 0
    if not subs:
        print("(no subscriptions)")
        return 0
    for s in subs:
        thr = f":{s['thread_id']}" if s.get("thread_id") else ""
        owner = f"  owner={s['notifier_profile']}" if s.get("notifier_profile") else ""
        print(f"  {s['task_id']:10s}  {s['platform']}:{s['chat_id']}{thr}"
              f"  (since event {s['last_event_id']}){owner}")
    return 0


def _cmd_notify_unsubscribe(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        ok = kb.remove_notify_sub(
            conn, task_id=args.task_id,
            platform=args.platform, chat_id=args.chat_id,
            thread_id=args.thread_id,
        )
    if not ok:
        print("(no such subscription)", file=sys.stderr)
        return 1
    print(f"Unsubscribed from {args.task_id}")
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    content = kb.read_worker_log(args.task_id, tail_bytes=args.tail)
    if content is None:
        print(f"(no log for {args.task_id} — task may not have spawned yet)",
              file=sys.stderr)
        return 1
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    """Show attempt history for a task."""
    rsk = _run_state_kwargs(args)
    if rsk is None:
        print(
            "kanban runs: pass both --state-type and --state-name, or omit both",
            file=sys.stderr,
        )
        return 2
    with kb.connect_closing() as conn:
        runs = kb.list_runs(conn, args.task_id, **rsk)
    if getattr(args, "json", False):
        print(json.dumps([
            {
                "id": r.id, "profile": r.profile, "status": r.status,
                "outcome": r.outcome, "started_at": r.started_at,
                "ended_at": r.ended_at, "summary": r.summary,
                "error": r.error, "metadata": r.metadata,
                "worker_pid": r.worker_pid, "step_key": r.step_key,
            } for r in runs
        ], indent=2, ensure_ascii=False))
        return 0
    if not runs:
        print(f"(no runs yet for {args.task_id})")
        return 0
    print(f"{'#':3s}  {'OUTCOME':12s}  {'PROFILE':16s}  {'ELAPSED':>8s}  STARTED")
    for i, r in enumerate(runs, 1):
        end = r.ended_at or int(time.time())
        # Clamp to 0 so NTP backward-jumps don't print negative durations.
        elapsed = max(0, end - r.started_at)
        if elapsed < 60:
            el = f"{elapsed}s"
        elif elapsed < 3600:
            el = f"{elapsed // 60}m"
        else:
            el = f"{elapsed / 3600:.1f}h"
        outcome = r.outcome or ("(running)" if not r.ended_at else r.status)
        print(f"{i:3d}  {outcome:12s}  {(r.profile or '-'):16s}  {el:>8s}  {_fmt_ts(r.started_at)}")
        if r.summary:
            # Indent and truncate long summaries to keep the table readable.
            summary = r.summary.splitlines()[0][:100]
            print(f"     → {summary}")
        if r.error:
            print(f"     ✖ {r.error[:100]}")
    return 0


def _cmd_context(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        text = kb.build_worker_context(conn, args.task_id)
    print(text)
    return 0


def _cmd_specify(args: argparse.Namespace) -> int:
    """Flesh out a triage task (or all of them) via auxiliary LLM,
    then promote to todo. Thin wrapper over ``kanban_specify``."""
    from hermes_cli import kanban_specify as spec

    all_flag = bool(getattr(args, "all_triage", False))
    tenant = getattr(args, "tenant", None)
    author = getattr(args, "author", None) or _profile_author()
    want_json = bool(getattr(args, "json", False))

    if args.task_id and all_flag:
        print(
            "kanban: pass either a task id OR --all, not both",
            file=sys.stderr,
        )
        return 2

    if all_flag:
        ids = spec.list_triage_ids(tenant=tenant)
        if not ids:
            msg = (
                "No triage tasks"
                + (f" for tenant {tenant!r}" if tenant else "")
                + "."
            )
            if want_json:
                print(json.dumps({"specified": 0, "total": 0}))
            else:
                print(msg)
            return 0
    elif args.task_id:
        ids = [args.task_id]
    else:
        print(
            "kanban: specify requires a task id or --all",
            file=sys.stderr,
        )
        return 2

    ok_count = 0
    fail_count = 0
    for tid in ids:
        outcome = spec.specify_task(tid, author=author)
        if outcome.ok:
            ok_count += 1
        else:
            fail_count += 1
        if want_json:
            print(json.dumps({
                "task_id": outcome.task_id,
                "ok": outcome.ok,
                "reason": outcome.reason,
                "new_title": outcome.new_title,
            }))
        elif outcome.ok:
            title_suffix = (
                f" — retitled: {outcome.new_title!r}"
                if outcome.new_title
                else ""
            )
            print(f"Specified {outcome.task_id} → todo{title_suffix}")
        else:
            print(
                f"kanban: specify {outcome.task_id}: {outcome.reason}",
                file=sys.stderr,
            )
    if not all_flag:
        return 0 if ok_count == 1 else 1
    # --all: succeed if at least one promotion landed; exit 1 only when
    # every candidate failed (honest signal for scripts).
    return 0 if (ok_count > 0 or not ids) else 1


def _cmd_decompose(args: argparse.Namespace) -> int:
    """Fan a triage task (or all of them) out into a graph of child
    tasks via the auxiliary LLM, routed to specialist profiles by
    description. Thin wrapper over ``kanban_decompose``."""
    from hermes_cli import kanban_decompose as decomp

    all_flag = bool(getattr(args, "all_triage", False))
    tenant = getattr(args, "tenant", None)
    author = getattr(args, "author", None) or _profile_author()
    want_json = bool(getattr(args, "json", False))

    if args.task_id and all_flag:
        print(
            "kanban: pass either a task id OR --all, not both",
            file=sys.stderr,
        )
        return 2

    if all_flag:
        ids = decomp.list_triage_ids(tenant=tenant)
        if not ids:
            msg = (
                "No triage tasks"
                + (f" for tenant {tenant!r}" if tenant else "")
                + "."
            )
            if want_json:
                print(json.dumps({"decomposed": 0, "total": 0}))
            else:
                print(msg)
            return 0
    elif args.task_id:
        ids = [args.task_id]
    else:
        print(
            "kanban: decompose requires a task id or --all",
            file=sys.stderr,
        )
        return 2

    ok_count = 0
    for tid in ids:
        outcome = decomp.decompose_task(tid, author=author)
        if outcome.ok:
            ok_count += 1
        if want_json:
            print(json.dumps({
                "task_id": outcome.task_id,
                "ok": outcome.ok,
                "reason": outcome.reason,
                "fanout": outcome.fanout,
                "child_ids": outcome.child_ids,
                "new_title": outcome.new_title,
            }))
        elif outcome.ok:
            if outcome.fanout and outcome.child_ids:
                child_summary = ", ".join(outcome.child_ids)
                print(
                    f"Decomposed {outcome.task_id} → {len(outcome.child_ids)} "
                    f"children ({child_summary}); root promoted to todo"
                )
            else:
                title_suffix = (
                    f" — retitled: {outcome.new_title!r}"
                    if outcome.new_title
                    else ""
                )
                print(
                    f"Specified {outcome.task_id} → todo "
                    f"(no fanout){title_suffix}"
                )
        else:
            print(
                f"kanban: decompose {outcome.task_id}: {outcome.reason}",
                file=sys.stderr,
            )
    if not all_flag:
        return 0 if ok_count == 1 else 1
    return 0 if (ok_count > 0 or not ids) else 1


def _cmd_sensors(args: argparse.Namespace) -> int:
    """Inspect Tier-1 sensors and apply owner overrides.

    Subcommands:

    * ``list`` -- show declared sensors + their current persisted state.
    * ``reset`` -- OWNER override: force a tripped circuit breaker back to
      ``closed`` (D2 human re-enable). Auto-recovery is unchanged; this is the
      escape hatch for when the owner has fixed the fault and wants dispatch
      restored without waiting out the cooldown.
    """
    action = getattr(args, "sensors_action", None) or "list"
    slug = os.environ.get("HERMES_KANBAN_BOARD") or kb.DEFAULT_BOARD
    as_json = bool(getattr(args, "json", False))

    if action in {"list", "ls"}:
        with kb.connect_closing(board=slug) as conn:
            model = kb.build_sensor_state_read_model(conn, board=slug)
        if as_json:
            print(json.dumps(model, indent=2))
            return 0
        declared = model.get("declared", [])
        states = model.get("states", [])
        if not declared and not states:
            print(f"No Tier-1 sensors declared on board {slug!r}.")
            return 0
        print(f"Sensors on board {slug!r}:")
        for d in declared:
            print(f"  - {d.get('kind')}/{d.get('key')}"
                  + (f"  gates={d.get('gates_side_effect_class')}"
                     if d.get("gates_side_effect_class") else ""))
        if states:
            print("Current state:")
            for s in states:
                ent = s.get("entity_ref")
                ent_s = f" entity={ent}" if ent else ""
                print(f"  - {s.get('sensor_kind')}/{s.get('sensor_key')}: "
                      f"{s.get('status')}{ent_s}")
        return 0

    if action == "reset":
        sensor = str(getattr(args, "sensor", "circuit_breaker") or "circuit_breaker").strip()
        reason = getattr(args, "reason", None)
        actor = _profile_author()
        # Only circuit breakers are resettable. Treat the value as a sensor KEY
        # unless it is exactly the kind token 'circuit_breaker'/'circuit', in
        # which case reset every circuit breaker on the board.
        kind_tokens = {"circuit_breaker", "circuit", "circuit-breaker", "all"}
        key_filter = None if sensor.lower() in kind_tokens else sensor
        with kb.connect_closing(board=slug) as conn:
            result = kb.force_close_circuit_breaker(
                conn, board=slug, sensor_key=key_filter, actor=actor, reason=reason,
            )
        if as_json:
            print(json.dumps(result, indent=2))
            return 0
        reset = result.get("reset", [])
        if not reset:
            target = f" {key_filter!r}" if key_filter else ""
            print(f"kanban: no circuit breaker{target} found on board {slug!r}.",
                  file=sys.stderr)
            return 1
        for r in reset:
            if r.get("transitioned"):
                print(f"Force-closed circuit breaker {r['sensor_key']!r}: "
                      f"{r['previous_status']} -> {r['status']} (by {actor}).")
            else:
                print(f"Circuit breaker {r['sensor_key']!r} already "
                      f"{r['status']} -- no change.")
        return 0

    print(f"kanban sensors: unknown action {action!r}", file=sys.stderr)
    return 2


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Tick-liveness doctor (F5+F3): fail loudly when a board has live work
    the gateway should be driving but is not ticking it.

    Exit codes: 0 = all checked boards healthy, 1 = at least one board is
    stale/unhealthy (so it slots into CI / monitoring as a hard signal).
    """
    staleness = int(getattr(args, "staleness_seconds", kb.TICK_STALENESS_SECONDS))
    as_json = bool(getattr(args, "json", False))

    if getattr(args, "all_boards", False):
        try:
            slugs = [b.get("slug") or kb.DEFAULT_BOARD
                     for b in kb.list_boards(include_archived=False)]
        except Exception:
            slugs = [kb.DEFAULT_BOARD]
        slugs = slugs or [kb.DEFAULT_BOARD]
    else:
        slugs = [os.environ.get("HERMES_KANBAN_BOARD") or kb.DEFAULT_BOARD]

    reports: list[dict] = []
    for slug in slugs:
        # Skip orphans (kanban.db, no board.json) here — they're reported by
        # the binding-health section below, and connecting would only confirm
        # they have no live work. Configured boards + default get the
        # tick-staleness probe.
        if slug != kb.DEFAULT_BOARD and not kb.board_is_configured(slug):
            continue
        with kb.connect_closing(board=slug) as conn:
            reports.append(kb.board_tick_stale(conn, board=slug, staleness_seconds=staleness))

    stale = [r for r in reports if r.get("stale")]

    # Board↔profile binding health (orphan boards, profiles bound to a
    # nonexistent board, role profiles with no binding). Fail-loud signal so
    # the fragmentation that spawned orphan boards can't silently re-accumulate.
    binding = kb.board_binding_health()
    binding_unhealthy = not binding.get("ok", True)

    if as_json:
        print(json.dumps({
            "healthy": not stale and not binding_unhealthy,
            "boards": reports,
            "binding": binding,
        }, indent=2))
        return 1 if (stale or binding_unhealthy) else 0

    for orphan in binding.get("orphans", []):
        print(
            f"ORPHAN     {orphan['slug']} (kanban.db, no board.json; "
            f"{orphan['task_count']} task(s)) — `hermes kanban boards "
            f"quarantine-orphans` or adopt it",
            file=sys.stderr,
        )
    for c in binding.get("corrupt", []):
        print(
            f"CORRUPT    {c['slug']} (board.json won't parse: {c['error']}) — "
            f"the board lost its contract; restore board.json from a backup",
            file=sys.stderr,
        )
    for bad in binding.get("bad_profile_bindings", []):
        print(
            f"BAD-BIND   profile {bad['profile']!r} -> board {bad['board']!r} "
            f"which has no board.json",
            file=sys.stderr,
        )
    # Advisory (does NOT fail the exit code): role profiles a board's contract
    # names that carry no explicit kanban_board binding. A daemon launched for
    # such a profile with no HERMES_KANBAN_BOARD set would hit the fail-loud
    # GatewayBoardBindingError at start (it refuses to fall through to
    # 'default'). Surface it here so the operator can SEE/repair the gap before
    # it bites, with the exact remediation. This is the doctor-visible twin of
    # the runtime rail — closing the "can I diagnose an unbound daemon?" gap.
    for rb in binding.get("role_bindings", []):
        if not rb.get("bound"):
            print(
                f"UNBOUND    profile {rb['profile']!r} (role {rb['role']!r} of "
                f"board {rb['board']!r}) has no kanban_board binding — its "
                f"daemon would refuse to start. Fix: `hermes kanban boards "
                f"bind-roles {rb['board']}`.",
                file=sys.stderr,
            )

    for r in reports:
        slug = r["board"]
        if r.get("stale"):
            print(f"UNHEALTHY  {slug}", file=sys.stderr)
            for reason in r.get("reasons", []):
                print(f"    - {reason}", file=sys.stderr)
        elif not r.get("has_live_work"):
            print(f"idle       {slug} (no live reactive/optimizer work)")
        else:
            age = r.get("age_seconds")
            age_str = f"{age}s ago" if age is not None else "n/a"
            print(f"OK         {slug} (last successful tick: {age_str})")

    if stale:
        print(
            f"\nkanban doctor: {len(stale)} board(s) have live work but are not "
            f"being ticked. Is `hermes gateway start` running?",
            file=sys.stderr,
        )
    if binding_unhealthy:
        n_orph = len(binding.get("orphans", []))
        n_corrupt = len(binding.get("corrupt", []))
        n_bad = len(binding.get("bad_profile_bindings", []))
        print(
            f"\nkanban doctor: board↔profile binding issues — "
            f"{n_orph} orphan board(s), {n_corrupt} corrupt board.json, "
            f"{n_bad} bad binding(s). See messages above.",
            file=sys.stderr,
        )
    if stale or binding_unhealthy:
        return 1
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:
    """Remove scratch workspaces of archived tasks, prune old events, and
    delete old worker logs."""
    import shutil
    scratch_root = kb.workspaces_root()
    removed_ws = 0
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT id, workspace_kind, workspace_path FROM tasks WHERE status = 'archived'"
        ).fetchall()
    for row in rows:
        if row["workspace_kind"] != "scratch":
            continue
        path = Path(row["workspace_path"] or (scratch_root / row["id"]))
        try:
            path = path.resolve()
        except OSError:
            continue
        try:
            path.relative_to(scratch_root.resolve())
        except ValueError:
            # Safety: never delete outside the scratch root.
            continue
        if path.exists() and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed_ws += 1

    event_days = getattr(args, "event_retention_days", 30)
    log_days = getattr(args, "log_retention_days", 30)
    with kb.connect_closing() as conn:
        removed_events = kb.gc_events(
            conn, older_than_seconds=event_days * 24 * 3600,
        )
    removed_logs = kb.gc_worker_logs(
        older_than_seconds=log_days * 24 * 3600,
    )
    print(f"GC complete: {removed_ws} workspace(s), "
          f"{removed_events} event row(s), {removed_logs} log file(s) removed")
    return 0


# ---------------------------------------------------------------------------
# Slash-command entry point (used by /kanban from CLI and gateway)
# ---------------------------------------------------------------------------

_SLASH_KANBAN_HELP = """\
**/kanban** — manage the shared task board.

Common subcommands:
  `list` (alias `ls`)   List tasks on the current board
  `show <id>`           Task details + comments + events
  `stats`               Per-status / per-assignee counts
  `create <title>…`     Create a task (auto-subscribes you to events)
  `comment <id> <msg>`  Append a comment
  `complete <id>…`      Mark task(s) done
  `block <id> [reason]` Mark blocked; `schedule <id> [reason]` parks time-delay work; `unblock <id>` to revive
  `assign <id> <profile>`  Reassign
  `boards list`         Show all boards
  `assignees`           Known profiles + counts
  `context <id>`        Full worker-context dump
  `runs <id>`           Attempt history
  `log <id>`            Worker log

Run `/kanban <subcommand> -h` for arguments. \
Read-only commands are safe while an agent is running.\
"""


def run_slash(rest: str) -> str:
    """Execute a ``/kanban …`` string and return captured stdout/stderr.

    ``rest`` is everything after ``/kanban`` (may be empty).  Used from
    both the interactive CLI (``self._handle_kanban_command``) and the
    gateway (``_handle_kanban_command``) so formatting is identical.
    """
    import io
    import contextlib

    tokens = shlex.split(rest) if rest and rest.strip() else []

    # Bare ``/kanban`` or ``/kanban help`` / ``--help`` / ``-h`` / ``?``:
    # show the curated short-help block instead of dumping argparse's full
    # usage tree (which is enormous and reads as garbage in a chat
    # bubble).  Per-subcommand help still works via ``/kanban foo -h``.
    if not tokens or tokens[0] in {"help", "--help", "-h", "?"}:
        return _SLASH_KANBAN_HELP

    # Single argparse tree rooted at "/kanban".  build_parser() expects a
    # subparsers action to attach to, so build a throwaway one and pull
    # the kanban_parser back out — then drive it directly so usage/error
    # text reads as ``/kanban`` (not ``/kanban-wrap kanban``).
    _wrap = argparse.ArgumentParser(prog="/kanban-wrap", add_help=False)
    _wrap.exit_on_error = False  # type: ignore[attr-defined]
    _top_sub = _wrap.add_subparsers(dest="_top")
    kanban_parser = build_parser(_top_sub)
    kanban_parser.prog = "/kanban"
    kanban_parser.exit_on_error = False  # type: ignore[attr-defined]
    for _action in kanban_parser._actions:
        if isinstance(_action, argparse._SubParsersAction):
            for _name, _choice in _action.choices.items():
                _choice.prog = f"/kanban {_name}"
                _choice.exit_on_error = False  # type: ignore[attr-defined]

    def _usage_for_error() -> str:
        if tokens:
            for _action in kanban_parser._actions:
                if isinstance(_action, argparse._SubParsersAction):
                    subparser = _action.choices.get(tokens[0])
                    if subparser is not None:
                        return subparser.format_usage().rstrip()
        return kanban_parser.format_usage().rstrip()

    buf_out = io.StringIO()
    buf_err = io.StringIO()
    # ``-h`` / ``--help`` makes argparse print to stdout and SystemExit(0).
    # Capture both streams so neither the help text nor the error text
    # bypasses our buffer.
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            args = kanban_parser.parse_args(tokens)
    except SystemExit as exc:
        out = buf_out.getvalue().rstrip()
        err = buf_err.getvalue().rstrip()
        # Help dump (exit 0) → return the captured help text directly.
        if exc.code in {0, None} and out:
            return out
        body = err or out
        return f"⚠ /kanban usage error\n{body}" if body else "⚠ /kanban usage error"
    except argparse.ArgumentError as exc:
        return f"⚠ /kanban usage error\n{_usage_for_error()}\n{exc}"

    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        try:
            kanban_command(args)
        except SystemExit:
            pass
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)

    out = buf_out.getvalue().rstrip()
    err = buf_err.getvalue().rstrip()
    if err and out:
        return f"{out}\n{err}"
    return err if err else (out or "(no output)")
