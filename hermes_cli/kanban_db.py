"""SQLite-backed Kanban board for multi-profile, multi-project collaboration.

In a fresh install the board lives at ``<root>/kanban.db`` where
``<root>`` is the **shared Hermes root** (the parent of any active
profile). Profiles intentionally collapse onto a shared board: it IS
the cross-profile coordination primitive. A worker spawned with
``hermes -p <profile>`` joins the same board as the dispatcher that
claimed the task. The same applies to ``<root>/kanban/workspaces/`` and
``<root>/kanban/logs/``.

**Multiple boards (projects):** users can create additional boards to
separate unrelated streams of work (e.g. one per project / repo / domain).
Each board is a directory under ``<root>/kanban/boards/<slug>/`` with
its own ``kanban.db``, ``workspaces/``, and ``logs/``. All boards share
the profile's Hermes home but are otherwise isolated: a worker spawned
for a task on board ``atm10-server`` sees only that board's tasks,
cannot enumerate other boards, and its dispatcher ticks don't touch
other boards' DBs.

The first (and for single-project users, only) board is ``default``.
For back-compat its on-disk DB is ``<root>/kanban.db`` (not
``boards/default/kanban.db``), so installs that predate the boards
feature keep working with zero migration. See :func:`kanban_db_path`.

Board resolution order (highest precedence first, all optional):

* ``board=`` argument passed directly to :func:`connect` / :func:`init_db`
  (explicit — used by the CLI ``--board`` flag and the dashboard
  ``?board=...`` query param).
* ``HERMES_KANBAN_BOARD`` env var (used by the dispatcher to pin workers
  to the board their task lives on — workers cannot see other boards).
* ``HERMES_KANBAN_DB`` env var (pins the DB file path directly — legacy
  override still honoured; highest precedence when the file path itself
  is what the caller wants to force).
* ``<root>/kanban/current`` — a one-line text file holding the slug of
  the "currently selected" board. Written by ``hermes kanban boards
  switch <slug>``. When absent, the active board is ``default``.

In standard installs ``<root>`` is ``~/.hermes``. In Docker / custom
deployments where ``HERMES_HOME`` points outside ``~/.hermes`` (e.g.
``/opt/hermes``), ``<root>`` is ``HERMES_HOME``. Legacy env-var
overrides still work:

* ``HERMES_KANBAN_DB`` — pin the database file path directly.
* ``HERMES_KANBAN_WORKSPACES_ROOT`` — pin the workspaces root directly.
* ``HERMES_KANBAN_HOME`` — pin the umbrella root that anchors kanban
  paths. Useful for tests and unusual deployments.

The dispatcher injects ``HERMES_KANBAN_DB``,
``HERMES_KANBAN_WORKSPACES_ROOT``, and ``HERMES_KANBAN_BOARD`` into
worker subprocess env so workers converge on the exact DB the
dispatcher used to claim their task — even under unusual symlink or
Docker layouts.

Schema is intentionally small: tasks, task_links, task_comments,
task_events.  The ``workspace_kind`` field decouples coordination from git
worktrees so that research / ops / digital-twin workloads work alongside
coding workloads.  See ``docs/hermes-kanban-v1-spec.pdf`` for the full
design specification.

Concurrency strategy: WAL mode + ``BEGIN IMMEDIATE`` for write
transactions + compare-and-swap (CAS) updates on ``tasks.status`` and
``tasks.claim_lock``.  SQLite serializes writers via its WAL lock, so at
most one claimer can win any given task.  Losers observe zero affected
rows and move on -- no retry loops, no distributed-lock machinery.
The CAS coordination is **per-board** — each board is a separate DB,
so multi-board installs get the same atomicity guarantees without any
new locking.
"""

from __future__ import annotations

import contextlib
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl module.
    fcntl = None  # type: ignore[assignment]
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from toolsets import get_toolset_names

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_STATUSES = {
    "triage", "todo", "scheduled", "ready", "running", "watching",
    "blocked", "review", "done", "archived",
}
# New cards should not be born in ``watching`` because a healthy watching
# card needs an active task_watch_routes row. Use set_task_watching()/
# ``hermes kanban wait``/``kanban_watch`` after creation or during a run.
VALID_INITIAL_STATUSES = {"running", "blocked"}
VALID_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}
KNOWN_TOOLSET_NAMES = frozenset(name.casefold() for name in get_toolset_names())
_UNSET = object()
_IS_WINDOWS = sys.platform == "win32"

# A running task's claim is valid for 15 minutes by default; after that the
# next dispatcher tick reclaims it. Workers that outlive this window should
# call ``heartbeat_claim(task_id)`` periodically. In practice most kanban
# workloads either finish within 15m, set a longer claim explicitly, or use
# ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` to raise the default claim window for
# long single-call MCP workflows.
DEFAULT_CLAIM_TTL_SECONDS = 15 * 60


def _resolve_claim_ttl_seconds(ttl_seconds: Optional[int] = None) -> int:
    """Return the effective claim TTL, honoring the kanban env override.

    Explicit call-site values win. Otherwise a positive integer from
    ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` overrides the built-in default.
    Invalid or non-positive env values fall back silently so existing
    installs keep working.
    """
    if ttl_seconds is not None:
        return max(1, int(ttl_seconds))

    raw = os.environ.get("HERMES_KANBAN_CLAIM_TTL_SECONDS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed

    return DEFAULT_CLAIM_TTL_SECONDS


# Grace period after a task transitions to ``running`` during which
# ``detect_crashed_workers`` skips the ``_pid_alive`` check. Covers the
# fork() → /proc-visibility window where liveness can transiently report
# False for a freshly-spawned worker. The 15-minute claim TTL still
# catches genuinely-crashed workers; this only suppresses false positives
# during the launch window.
DEFAULT_CRASH_GRACE_SECONDS = 30


def _resolve_crash_grace_seconds() -> int:
    """Return the crash-detection grace period in seconds.

    Reads ``HERMES_KANBAN_CRASH_GRACE_SECONDS`` from the environment;
    falls back to ``DEFAULT_CRASH_GRACE_SECONDS`` when absent, empty,
    non-integer, or negative. A value of 0 restores immediate-reclaim
    behaviour (useful for tests).
    """
    raw = os.environ.get("HERMES_KANBAN_CRASH_GRACE_SECONDS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = -1
        if parsed >= 0:
            return parsed
    return DEFAULT_CRASH_GRACE_SECONDS


# Worker-context caps so build_worker_context() stays bounded on
# pathological boards (retry-heavy tasks, comment storms, giant
# summaries). Values chosen to fit a typical 100k-char LLM prompt with
# plenty of headroom. Each constant is tuned independently so users
# who need to relax one don't have to relax all of them.
_CTX_MAX_PRIOR_ATTEMPTS = 10      # most recent N prior runs shown in full
_CTX_MAX_COMMENTS       = 30      # most recent N comments shown in full
_CTX_MAX_FIELD_BYTES    = 4 * 1024   # 4 KB per summary/error/metadata/result
_CTX_MAX_BODY_BYTES     = 8 * 1024   # 8 KB per task.body (opening post)
_CTX_MAX_COMMENT_BYTES  = 2 * 1024   # 2 KB per comment


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DEFAULT_BOARD = "default"
VALID_RUNTIME_MODES = {"kernel", "goal", "company"}
DEFAULT_SEMANTIC_STAGES = ("intake", "plan", "execute", "verify", "deliver", "improve")

# Slug validator: lowercase alphanumerics, digits, hyphens; 1–64 chars.
# Strict enough to stop traversal (`..`) and embedded path separators, loose
# enough that kebab-case names like ``atm10-server`` or ``hermes-agent``
# pass without fuss. Board names with display formatting (spaces, emoji)
# live in ``board.json``; the slug is just the directory name.
_BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def _normalize_board_slug(slug: Optional[str]) -> Optional[str]:
    """Lowercase + strip a slug; validate; return ``None`` for empty."""
    if slug is None:
        return None
    s = str(slug).strip().lower()
    if not s:
        return None
    if not _BOARD_SLUG_RE.match(s):
        raise ValueError(
            f"invalid board slug {slug!r}: must be 1-64 chars, lowercase "
            f"alphanumerics / hyphens / underscores, not starting with '-' or '_'"
        )
    return s


def kanban_home() -> Path:
    """Return the shared Hermes root that anchors the kanban board.

    Resolution order:

    1. ``HERMES_KANBAN_HOME`` env var when set and non-empty (explicit
       override for tests and unusual deployments).
    2. ``get_default_hermes_root()``, which already returns ``<root>``
       when ``HERMES_HOME`` is ``<root>/profiles/<name>``, and returns
       ``HERMES_HOME`` directly for Docker / custom deployments.

    The kanban board is shared across profiles **by design** (see the
    module docstring). Resolving the kanban paths through the active
    profile's ``HERMES_HOME`` would silently fork the board per profile,
    which breaks the dispatcher / worker handoff.
    """
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def boards_root() -> Path:
    """Return ``<root>/kanban/boards`` — the parent of non-default board dirs.

    ``default`` is intentionally NOT under this directory — its DB lives at
    ``<root>/kanban.db`` for back-compat with pre-boards installs. This
    function returns the directory where *additional* named boards live,
    used by :func:`list_boards` to enumerate them.
    """
    return kanban_home() / "kanban" / "boards"


def current_board_path() -> Path:
    """Return the path to ``<root>/kanban/current``.

    One-line text file written by ``hermes kanban boards switch <slug>``
    to persist the user's board selection across CLI invocations. Absent
    by default (meaning: active board is ``default``).
    """
    return kanban_home() / "kanban" / "current"


def get_current_board() -> str:
    """Return the active board slug, honouring the resolution chain.

    Order (highest precedence first):

    1. ``HERMES_KANBAN_BOARD`` env var (set by the dispatcher on worker
       spawn, or manually for ad-hoc overrides).
    2. ``<root>/kanban/current`` on disk (set by ``hermes kanban boards
       switch``), but only when that board still exists.
    3. ``DEFAULT_BOARD`` (``"default"``).

    A malformed or stale slug at any step falls through to the next layer
    with a best-effort warning — the dispatcher must never crash because a
    user hand-edited a file or removed a board directory.
    """
    env = os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    if env:
        try:
            normed = _normalize_board_slug(env)
            if normed and board_exists(normed):
                return normed
        except ValueError:
            pass
    try:
        f = current_board_path()
        if f.exists():
            val = f.read_text(encoding="utf-8").strip()
            if val:
                try:
                    normed = _normalize_board_slug(val)
                    if normed and board_exists(normed):
                        return normed
                except ValueError:
                    pass
    except OSError:
        pass
    return DEFAULT_BOARD


def set_current_board(slug: str) -> Path:
    """Persist ``slug`` as the active board. Returns the file written.

    Writes ``<root>/kanban/current``. The caller should validate the slug
    exists first (via :func:`board_exists`) — this function does not —
    so that ``hermes kanban boards switch <typo>`` returns an error
    instead of silently pointing at nothing.
    """
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    path = current_board_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normed + "\n", encoding="utf-8")
    return path


def clear_current_board() -> None:
    """Remove ``<root>/kanban/current`` so the active board reverts to ``default``."""
    try:
        current_board_path().unlink()
    except FileNotFoundError:
        pass


def board_dir(board: Optional[str] = None) -> Path:
    """Return the on-disk directory for ``board``.

    ``default`` is ``<root>/kanban/boards/default/`` **for metadata only**
    (board.json + workspaces/ + logs/). Its DB file stays at
    ``<root>/kanban.db`` for back-compat — see :func:`kanban_db_path`.

    All other boards live at ``<root>/kanban/boards/<slug>/`` with
    everything inside that directory including the ``kanban.db``.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    return boards_root() / slug


def board_exists(board: Optional[str] = None) -> bool:
    """Return True if the board has persisted metadata or a DB on disk.

    ``default`` is considered to always exist — its DB is created
    on first :func:`connect` and there's no way for it to be missing
    in a configuration where the kanban feature is usable at all.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    if slug == DEFAULT_BOARD:
        return True
    d = board_dir(slug)
    return (d / "board.json").exists() or (d / "kanban.db").exists()


def kanban_db_path(board: Optional[str] = None) -> Path:
    """Return the path to the ``kanban.db`` for ``board``.

    Resolution (highest precedence first):

    1. ``HERMES_KANBAN_DB`` env var — pins the path directly. Honoured for
       back-compat and for the dispatcher→worker handoff (defense in
       depth: dispatcher injects this into worker env so workers are
       immune to any path-resolution disagreement).
    2. When ``board`` arg is None, the active board from
       :func:`get_current_board` is used.
    3. Board ``default`` → ``<root>/kanban.db`` (back-compat path).
       Other boards → ``<root>/kanban/boards/<slug>/kanban.db``.
    """
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban.db"
    return board_dir(slug) / "kanban.db"


def _board_slug_for_db_path(path: Path) -> Optional[str]:
    """Infer a board slug from a canonical Kanban DB path when possible."""
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path.expanduser().absolute()
    try:
        if resolved == (kanban_home() / "kanban.db").resolve():
            return DEFAULT_BOARD
    except OSError:
        pass
    try:
        rel = resolved.relative_to(boards_root().resolve())
    except (OSError, ValueError):
        return None
    if len(rel.parts) == 2 and rel.parts[1] == "kanban.db":
        return _normalize_board_slug(rel.parts[0])
    return None


def workspaces_root(board: Optional[str] = None) -> Path:
    """Return the directory under which ``scratch`` workspaces are created.

    Anchored per-board so workspaces don't leak between projects.
    ``HERMES_KANBAN_WORKSPACES_ROOT`` pins the path directly (highest
    precedence) — the dispatcher injects this into worker env.

    ``default`` keeps the legacy path ``<root>/kanban/workspaces/`` so
    that existing scratch workspaces from before the boards feature are
    preserved. Other boards use ``<root>/kanban/boards/<slug>/workspaces/``.
    """
    override = os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban" / "workspaces"
    return board_dir(slug) / "workspaces"


def worker_logs_dir(board: Optional[str] = None) -> Path:
    """Return the directory under which per-task worker logs are written.

    ``default`` keeps the legacy path ``<root>/kanban/logs/``. Other
    boards use ``<root>/kanban/boards/<slug>/logs/``. Logs follow the
    board — makes ``hermes kanban log`` unambiguous even when multiple
    boards have tasks with the same id.
    """
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban" / "logs"
    return board_dir(slug) / "logs"


def board_metadata_path(board: Optional[str] = None) -> Path:
    """Return the path to ``board.json`` for ``board``.

    Stores display metadata (display name, description, icon, color,
    created_at). The on-disk slug is the canonical identity; this file
    is purely for presentation in the CLI / dashboard.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    return board_dir(slug) / "board.json"


def _default_board_display_name(slug: str) -> str:
    """Turn a slug into a reasonable default display name.

    ``atm10-server`` → ``Atm10 Server``. Users can override via
    ``board.json`` but the default should look presentable in the
    dashboard without any follow-up editing.
    """
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part) or slug


def default_semantic_workflow() -> dict:
    """Return the generic objective-first workflow scaffold for new boards."""
    stages: list[dict[str, Any]] = []
    for idx, key in enumerate(DEFAULT_SEMANTIC_STAGES):
        stage: dict[str, Any] = {
            "key": key,
            "label": key.replace("_", " ").title(),
            "substates": [],
            "actions": [],
            "triggers": [],
            "exit_criteria": [],
        }
        if idx + 1 < len(DEFAULT_SEMANTIC_STAGES):
            stage["exit_criteria"].append({
                "transition": DEFAULT_SEMANTIC_STAGES[idx + 1],
                "evidence_required": [],
            })
        stages.append(stage)
    return {
        "id": "objective-first-v1",
        "stages": stages,
    }


def normalize_runtime_mode(runtime: Optional[str]) -> str:
    """Validate a board runtime mode."""
    mode = str(runtime or "goal").strip().lower()
    if mode not in VALID_RUNTIME_MODES:
        raise ValueError(
            "runtime must be one of: " + ", ".join(sorted(VALID_RUNTIME_MODES))
        )
    return mode


def _string_list(values: Optional[Iterable[str]]) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    elif isinstance(values, dict):
        return []
    elif not isinstance(values, (list, tuple, set)):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _coerce_bool(value: Any) -> bool:
    """Parse operator-authored booleans without making ``"false"`` truthy."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"", "0", "false", "no", "n", "off", "none", "null"}:
            return False
    return bool(value)


def _json_object(value: Optional[Any], *, field: str) -> Optional[dict]:
    """Normalize a JSON object supplied as dict or JSON string."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object/dict, got {type(value).__name__}")
    return dict(value)


def _normalize_policy_map(value: Optional[Any], *, field: str) -> dict:
    """Normalize board runtime policy maps while preserving future keys."""
    if value in (None, ""):
        return {}
    parsed = _json_object(value, field=field)
    if parsed is None:
        return {}
    out: dict[str, Any] = {}
    for raw_key, raw_policy in parsed.items():
        key = str(raw_key).strip()
        if not key:
            continue
        if raw_policy is None:
            out[key] = {}
        elif isinstance(raw_policy, dict):
            out[key] = dict(raw_policy)
        else:
            raise ValueError(f"{field}.{key} must be an object")
    return out


def _normalize_worker_envelopes(value: Optional[Any]) -> dict:
    """Normalize runtime.worker_envelopes keyed by worker/profile name."""
    raw = _normalize_policy_map(value, field="runtime.worker_envelopes")
    out: dict[str, dict] = {}
    for profile, envelope in raw.items():
        row = dict(envelope)
        for key in (
            "capabilities",
            "allowed_capabilities",
            "toolsets",
            "allowed_toolsets",
            "allowed_side_effects",
            "required_proof",
            "skills",
        ):
            if key in row:
                row[key] = _string_list(row.get(key))
        if "capabilities" not in row and "allowed_capabilities" in row:
            row["capabilities"] = list(row.get("allowed_capabilities") or [])
        if "toolsets" not in row and "allowed_toolsets" in row:
            row["toolsets"] = list(row.get("allowed_toolsets") or [])
        out[profile] = row
    return out


def normalize_board_operating_contract(contract: Optional[Any]) -> dict:
    """Normalize a serious-board operating contract wrapper."""
    parsed = _json_object(contract, field="contract")
    if parsed is None:
        return {}
    if isinstance(parsed.get("operating_contract"), dict):
        parsed = dict(parsed["operating_contract"])
    elif isinstance(parsed.get("contract"), dict):
        parsed = dict(parsed["contract"])
    out: dict[str, Any] = {}
    if parsed.get("objective") is not None:
        out["objective"] = normalize_objective_metadata(parsed.get("objective"))
    if parsed.get("runtime") is not None:
        out["runtime"] = normalize_runtime_metadata(parsed.get("runtime"))
    if parsed.get("workflow") is not None:
        out["workflow"] = normalize_workflow_definition(parsed.get("workflow"))
    return out


def normalize_objective_metadata(objective: Optional[Any]) -> Optional[dict]:
    """Validate and normalize board-level objective metadata."""
    if objective is None:
        return None
    if isinstance(objective, str):
        return {
            "statement": objective.strip(),
            "success": [],
            "failure": [],
            "constraints": [],
        }
    if not isinstance(objective, dict):
        raise ValueError("objective must be a string or object")
    out = dict(objective)
    out["statement"] = str(out.get("statement") or out.get("objective") or "").strip()
    out["success"] = _string_list(out.get("success") or out.get("success_criteria"))
    out["failure"] = _string_list(out.get("failure") or out.get("failure_criteria"))
    out["constraints"] = _string_list(out.get("constraints"))
    return out


def build_objective_metadata(
    *,
    statement: Optional[str],
    fallback_statement: str,
    success: Optional[Iterable[str]] = None,
    failure: Optional[Iterable[str]] = None,
    constraints: Optional[Iterable[str]] = None,
) -> dict:
    """Build the objective scaffold used by goal/company runtime boards."""
    return {
        "statement": str(statement or fallback_statement or "").strip(),
        "success": _string_list(success),
        "failure": _string_list(failure),
        "constraints": _string_list(constraints),
    }


def normalize_runtime_metadata(runtime: Optional[Any]) -> Optional[dict]:
    """Validate and normalize board-level runtime metadata."""
    if runtime is None:
        return None
    if isinstance(runtime, str):
        runtime = {"mode": runtime}
    if not isinstance(runtime, dict):
        raise ValueError("runtime must be a string or object")
    out = dict(runtime)
    out["mode"] = normalize_runtime_mode(out.get("mode"))
    dispatcher = out.get("dispatcher") or {}
    if isinstance(dispatcher, str):
        dispatcher = {"profile": dispatcher}
    if not isinstance(dispatcher, dict):
        raise ValueError("runtime.dispatcher must be an object")
    dispatcher_profile = str(dispatcher.get("profile") or "").strip()
    out["dispatcher"] = {"profile": dispatcher_profile or None}

    profiles = out.get("profiles") or {}
    if not isinstance(profiles, dict):
        raise ValueError("runtime.profiles must be an object")
    out["profiles"] = {
        key: (str(profiles.get(key) or "").strip() or None)
        for key in ("ceo", "optimizer", "worker")
    }
    out["provider_policy"] = _normalize_policy_map(
        out.get("provider_policy") or out.get("capabilities"),
        field="runtime.provider_policy",
    )
    out["tool_policy"] = _normalize_policy_map(
        out.get("tool_policy"),
        field="runtime.tool_policy",
    )
    out["worker_envelopes"] = _normalize_worker_envelopes(out.get("worker_envelopes"))
    if out.get("require_worker_envelopes") is not None:
        out["require_worker_envelopes"] = _coerce_bool(out.get("require_worker_envelopes"))
    if out.get("require_provider_policy") is not None:
        out["require_provider_policy"] = _coerce_bool(out.get("require_provider_policy"))
    return out


def build_runtime_metadata(
    *,
    mode: str,
    dispatcher_profile: Optional[str] = None,
    ceo_profile: Optional[str] = None,
    optimizer_profile: Optional[str] = None,
    worker_profile: Optional[str] = None,
) -> dict:
    """Build normalized runtime metadata for a non-kernel board."""
    return normalize_runtime_metadata({
        "mode": mode,
        "dispatcher": {"profile": dispatcher_profile},
        "profiles": {
            "ceo": ceo_profile,
            "optimizer": optimizer_profile,
            "worker": worker_profile,
        },
    }) or {}


def _normalize_workflow_requirement_lists(row: dict) -> dict:
    """Normalize generic contract lists preserved on stages/actions/workstreams."""
    out = dict(row)
    for key in (
        "required_capabilities",
        "required_toolsets",
        "required_proof",
        "evidence_required",
        "allowed_side_effects",
    ):
        if key in out:
            out[key] = _string_list(out.get(key))
    return out


def _workflow_list_keys(items: Any, *, field: str) -> list[dict]:
    """Normalize workflow lists that may contain strings or object rows."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError(f"workflow.{field} must be a list")
    out: list[dict] = []
    seen: set[str] = set()
    for idx, item in enumerate(items):
        if isinstance(item, str):
            key = item.strip()
            row: dict[str, Any] = {"key": key}
        elif isinstance(item, dict):
            row = dict(item)
            key = str(row.get("key") or row.get("id") or "").strip()
            row["key"] = key
        else:
            raise ValueError(f"workflow.{field}[{idx}] must be a string or object")
        if not key:
            raise ValueError(f"workflow.{field}[{idx}].key is required")
        if key in seen:
            raise ValueError(f"workflow.{field} contains duplicate key {key!r}")
        seen.add(key)
        out.append(row)
    return out


def normalize_workflow_definition(workflow: Optional[Any]) -> Optional[dict]:
    """Validate and normalize a board-level semantic workflow definition.

    The workflow is deliberately generic: board authors define stage keys,
    substates, actions, triggers, and exit evidence. The kernel enforces
    shape and declared stage/action references, not any domain vocabulary.
    """
    if workflow is None:
        return None
    if isinstance(workflow, str):
        stripped = workflow.strip()
        if not stripped:
            return None
        try:
            workflow = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"workflow must be a JSON object: {exc}") from exc
    if not isinstance(workflow, dict):
        raise ValueError(f"workflow must be an object/dict, got {type(workflow).__name__}")
    out = dict(workflow)
    out["id"] = str(out.get("id") or "workflow").strip() or "workflow"
    stages = _workflow_list_keys(out.get("stages"), field="stages")
    if not stages:
        raise ValueError("workflow.stages must contain at least one stage")
    stage_keys = {stage["key"] for stage in stages}
    normalized_stages: list[dict] = []
    for stage in stages:
        row = _normalize_workflow_requirement_lists(dict(stage))
        if "label" in row and row["label"] is not None:
            row["label"] = str(row["label"])
        if "allowed_lifecycle_states" in row:
            states = row.get("allowed_lifecycle_states") or []
            if not isinstance(states, list):
                raise ValueError(
                    f"workflow stage {row['key']!r} allowed_lifecycle_states must be a list"
                )
            bad = [str(s) for s in states if str(s) not in VALID_STATUSES]
            if bad:
                raise ValueError(
                    f"workflow stage {row['key']!r} has invalid lifecycle states: "
                    + ", ".join(bad)
                )
            row["allowed_lifecycle_states"] = [str(s) for s in states]
        row["substates"] = _workflow_list_keys(row.get("substates"), field=f"stages.{row['key']}.substates")
        row["actions"] = _workflow_list_keys(row.get("actions"), field=f"stages.{row['key']}.actions")
        triggers = row.get("triggers") or []
        if not isinstance(triggers, list):
            raise ValueError(f"workflow stage {row['key']!r} triggers must be a list")
        row["triggers"] = [dict(t) if isinstance(t, dict) else {"type": str(t)} for t in triggers]
        exits = row.get("exit_criteria") or []
        if not isinstance(exits, list):
            raise ValueError(f"workflow stage {row['key']!r} exit_criteria must be a list")
        normalized_exits: list[dict] = []
        for idx, item in enumerate(exits):
            if not isinstance(item, dict):
                raise ValueError(
                    f"workflow stage {row['key']!r} exit_criteria[{idx}] must be an object"
                )
            exit_row = dict(item)
            transition = str(exit_row.get("transition") or exit_row.get("to") or "").strip()
            if not transition:
                raise ValueError(
                    f"workflow stage {row['key']!r} exit_criteria[{idx}].transition is required"
                )
            if transition not in stage_keys:
                raise ValueError(
                    f"workflow stage {row['key']!r} exits to unknown stage {transition!r}"
                )
            evidence = exit_row.get("evidence_required") or []
            if isinstance(evidence, str):
                evidence = [evidence]
            if not isinstance(evidence, list):
                raise ValueError(
                    f"workflow stage {row['key']!r} exit_criteria[{idx}].evidence_required must be a list"
                )
            exit_row["transition"] = transition
            exit_row["evidence_required"] = [str(e).strip() for e in evidence if str(e).strip()]
            normalized_exits.append(exit_row)
        row["exit_criteria"] = normalized_exits
        actions = row.get("actions") or []
        normalized_actions: list[dict] = []
        for action in actions:
            if not isinstance(action, dict):
                normalized_actions.append(action)
                continue
            action_row = _normalize_workflow_requirement_lists(dict(action))
            schema = action_row.get("output_schema")
            if schema is None:
                normalized_actions.append(action_row)
                continue
            if isinstance(schema, str):
                schema = [schema]
            if not isinstance(schema, list):
                raise ValueError(
                    f"workflow stage {row['key']!r} action {action_row.get('key')!r} "
                    "output_schema must be a list of artifact keys"
                )
            action_row["output_schema"] = [
                str(item).strip() for item in schema if str(item).strip()
            ]
            normalized_actions.append(action_row)
        row["actions"] = normalized_actions
        normalized_stages.append(row)
    out["stages"] = normalized_stages
    workstreams = _workflow_list_keys(out.get("workstreams"), field="workstreams")
    if workstreams:
        normalized_workstreams: list[dict] = []
        for ws in workstreams:
            ws_row = _normalize_workflow_requirement_lists(dict(ws))
            stages_allowed = ws_row.get("stages") or ws_row.get("stage_keys") or []
            if isinstance(stages_allowed, str):
                stages_allowed = [stages_allowed]
            if not isinstance(stages_allowed, list):
                raise ValueError(
                    f"workflow.workstreams.{ws_row['key']!r}.stages must be a list"
                )
            allowed = [str(s).strip() for s in stages_allowed if str(s).strip()]
            bad = [s for s in allowed if s not in stage_keys]
            if bad:
                raise ValueError(
                    f"workflow.workstream {ws_row['key']!r} references unknown stages: "
                    + ", ".join(bad)
                )
            ws_row["stages"] = allowed
            ws_row.pop("stage_keys", None)
            normalized_workstreams.append(ws_row)
        out["workstreams"] = normalized_workstreams
    if out.get("require_semantics") is not None:
        out["require_semantics"] = _coerce_bool(out["require_semantics"])
    return out


def _workflow_stage_map(workflow: Optional[dict]) -> dict[str, dict]:
    if not isinstance(workflow, dict):
        return {}
    return {
        str(stage.get("key")): stage
        for stage in workflow.get("stages") or []
        if isinstance(stage, dict) and stage.get("key")
    }


def _workflow_workstream_map(workflow: Optional[dict]) -> dict[str, dict]:
    if not isinstance(workflow, dict):
        return {}
    return {
        str(ws.get("key")): ws
        for ws in workflow.get("workstreams") or []
        if isinstance(ws, dict) and ws.get("key")
    }


def _workflow_action_row(stage: Optional[dict], action_key: Optional[str]) -> Optional[dict]:
    if not stage or not action_key:
        return None
    for action in stage.get("actions") or []:
        if isinstance(action, dict) and str(action.get("key")) == action_key:
            return action
    return None


def read_board_metadata(board: Optional[str] = None) -> dict:
    """Return ``board.json`` contents (or synthesized defaults).

    Never raises — a missing / malformed ``board.json`` falls back to a
    synthesised entry so the dashboard always has something to render.
    Includes the canonical ``slug`` and ``db_path`` so the caller
    doesn't need to reconstruct them.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    meta: dict[str, Any] = {
        "slug": slug,
        "name": _default_board_display_name(slug),
        "description": "",
        "icon": "",
        "color": "",
        "default_workdir": None,
        "objective": None,
        "runtime": None,
        "workflow": None,
        "metadata_error": None,
        "created_at": None,
        "archived": False,
    }
    try:
        p = board_metadata_path(slug)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                meta["metadata_error"] = (
                    f"invalid board metadata: expected object/dict, got {type(raw).__name__}"
                )
            else:
                # Never let the metadata file claim a different slug than
                # its directory — trust the filesystem.
                raw["slug"] = slug
                if raw.get("objective") is not None:
                    raw["objective"] = normalize_objective_metadata(raw.get("objective"))
                if raw.get("runtime") is not None:
                    raw["runtime"] = normalize_runtime_metadata(raw.get("runtime"))
                if raw.get("workflow") is not None:
                    raw["workflow"] = normalize_workflow_definition(raw.get("workflow"))
                meta.update(raw)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        meta["metadata_error"] = f"invalid board metadata: {exc}"
    meta["db_path"] = str(kanban_db_path(slug))
    return meta


def write_board_metadata(
    board: Optional[str],
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    color: Optional[str] = None,
    archived: Optional[bool] = None,
    default_workdir: Optional[str] = None,
    objective: Any = _UNSET,
    runtime: Any = _UNSET,
    workflow: Any = _UNSET,
) -> dict:
    """Create / update ``board.json`` for ``board``.

    Preserves any existing fields not mentioned in the call. Sets
    ``created_at`` on first write. Returns the resulting metadata dict.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    meta = read_board_metadata(slug)
    # Preserve existing DB-derived/runtime-error fields — they get re-computed
    # each read but shouldn't be written into board.json.
    meta.pop("db_path", None)
    meta.pop("metadata_error", None)
    if name is not None:
        meta["name"] = str(name).strip() or _default_board_display_name(slug)
    if description is not None:
        meta["description"] = str(description)
    if icon is not None:
        meta["icon"] = str(icon)
    if color is not None:
        meta["color"] = str(color)
    if archived is not None:
        meta["archived"] = bool(archived)
    if default_workdir is not None:
        meta["default_workdir"] = str(default_workdir) if default_workdir else None
    if objective is not _UNSET:
        if objective is None:
            meta.pop("objective", None)
        else:
            meta["objective"] = normalize_objective_metadata(objective)
    if runtime is not _UNSET:
        if runtime is None:
            meta.pop("runtime", None)
        else:
            meta["runtime"] = normalize_runtime_metadata(runtime)
    if workflow is not _UNSET:
        if workflow is None:
            meta.pop("workflow", None)
        else:
            meta["workflow"] = normalize_workflow_definition(workflow)
    if not meta.get("created_at"):
        meta["created_at"] = int(time.time())
    for optional_key in ("objective", "runtime", "workflow"):
        if meta.get(optional_key) is None:
            meta.pop(optional_key, None)
    path = board_metadata_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    for optional_key in ("objective", "runtime", "workflow"):
        meta.setdefault(optional_key, None)
    meta["db_path"] = str(kanban_db_path(slug))
    return meta


def create_board(
    slug: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    color: Optional[str] = None,
    default_workdir: Optional[str] = None,
    runtime: Optional[str] = None,
    objective: Optional[str] = None,
    success: Optional[Iterable[str]] = None,
    failure: Optional[Iterable[str]] = None,
    constraints: Optional[Iterable[str]] = None,
    dispatcher_profile: Optional[str] = None,
    ceo_profile: Optional[str] = None,
    optimizer_profile: Optional[str] = None,
    worker_profile: Optional[str] = None,
    workflow: Optional[Any] = None,
    contract: Optional[Any] = None,
) -> dict:
    """Create a new board directory + DB + metadata. Idempotent.

    Returns the resulting metadata. Raises :class:`ValueError` for a
    malformed slug; returns the existing metadata (not an error) if the
    board already exists — matching ``mkdir -p`` semantics.
    """
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    contract_meta = normalize_board_operating_contract(contract) if contract is not None else {}
    contract_objective = contract_meta.get("objective") if isinstance(contract_meta.get("objective"), dict) else None
    contract_runtime = contract_meta.get("runtime") if isinstance(contract_meta.get("runtime"), dict) else None
    contract_workflow = contract_meta.get("workflow") if isinstance(contract_meta.get("workflow"), dict) else None
    runtime_mode = normalize_runtime_mode(runtime or (contract_runtime or {}).get("mode") or "goal")
    fallback_objective = (
        objective
        or (contract_objective or {}).get("statement")
        or description
        or name
        or _default_board_display_name(normed)
    )
    if runtime_mode == "kernel":
        objective_meta = contract_objective if contract_objective is not None else None
        runtime_meta = normalize_runtime_metadata(contract_runtime or {"mode": "kernel"}) or {}
        runtime_meta["mode"] = "kernel"
        workflow_meta: Any = workflow if workflow is not None else contract_workflow
    else:
        objective_meta = dict(contract_objective or {})
        if not objective_meta:
            objective_meta = build_objective_metadata(
                statement=objective,
                fallback_statement=fallback_objective,
                success=success,
                failure=failure,
                constraints=constraints,
            )
        else:
            if objective is not None:
                objective_meta["statement"] = str(objective).strip()
            else:
                objective_meta.setdefault("statement", str(fallback_objective or "").strip())
            objective_meta["success"] = _string_list(success if success is not None else objective_meta.get("success"))
            objective_meta["failure"] = _string_list(failure if failure is not None else objective_meta.get("failure"))
            objective_meta["constraints"] = _string_list(constraints if constraints is not None else objective_meta.get("constraints"))
            objective_meta = normalize_objective_metadata(objective_meta) or objective_meta
        runtime_meta = dict(contract_runtime or {})
        runtime_meta["mode"] = runtime_mode
        dispatcher = runtime_meta.get("dispatcher") or {}
        if isinstance(dispatcher, str):
            dispatcher = {"profile": dispatcher}
        elif not isinstance(dispatcher, dict):
            dispatcher = {}
        if dispatcher_profile is not None:
            dispatcher["profile"] = dispatcher_profile
        runtime_meta["dispatcher"] = dispatcher
        profiles = runtime_meta.get("profiles") or {}
        if not isinstance(profiles, dict):
            profiles = {}
        if ceo_profile is not None:
            profiles["ceo"] = ceo_profile
        if optimizer_profile is not None:
            profiles["optimizer"] = optimizer_profile
        if worker_profile is not None:
            profiles["worker"] = worker_profile
        runtime_meta["profiles"] = profiles
        runtime_meta = normalize_runtime_metadata(runtime_meta)
        workflow_meta = workflow if workflow is not None else (contract_workflow if contract_workflow is not None else default_semantic_workflow())
    meta = write_board_metadata(
        normed,
        name=name,
        description=description,
        icon=icon,
        color=color,
        default_workdir=default_workdir,
        objective=objective_meta,
        runtime=runtime_meta,
        workflow=workflow_meta,
    )
    # Touch the DB so list_boards() sees it immediately.
    init_db(board=normed)
    return meta


def list_boards(*, include_archived: bool = True) -> list[dict]:
    """Enumerate all boards that exist on disk.

    Always includes ``default`` (even when the ``boards/default/``
    metadata dir doesn't exist, because its DB lives at the historical
    top-level path). Other boards are discovered by scanning ``boards/``
    for subdirectories that either contain a ``kanban.db`` or a ``board.json``.

    Returns a list of metadata dicts, sorted with ``default`` first and
    the rest alphabetically.
    """
    entries: list[dict] = []
    seen: set[str] = set()

    # Default board is always first.
    entries.append(read_board_metadata(DEFAULT_BOARD))
    seen.add(DEFAULT_BOARD)

    root = boards_root()
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            slug = child.name
            # Keep slug normalisation soft for discovery — but skip dirs
            # that don't parse as valid slugs so we don't surface junk.
            try:
                normed = _normalize_board_slug(slug)
            except ValueError:
                continue
            if not normed or normed in seen:
                continue
            has_db = (child / "kanban.db").exists()
            has_meta = (child / "board.json").exists()
            if not (has_db or has_meta):
                continue
            meta = read_board_metadata(normed)
            if meta.get("archived") and not include_archived:
                continue
            entries.append(meta)
            seen.add(normed)
    return entries


def remove_board(slug: str, *, archive: bool = True) -> dict:
    """Remove or archive a board.

    ``archive=True`` (default) moves the board's directory to
    ``<root>/kanban/boards/_archived/<slug>-<timestamp>/`` so the data
    is recoverable. ``archive=False`` deletes the directory outright.

    The ``default`` board cannot be removed — raises :class:`ValueError`.
    Returns a summary dict describing what happened (``{"slug", "action",
    "new_path"}``).
    """
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    if normed == DEFAULT_BOARD:
        raise ValueError("the 'default' board cannot be removed")
    d = board_dir(normed)
    if not d.exists():
        raise ValueError(f"board {normed!r} does not exist")

    # If the user removed the currently-active board, revert to default.
    if get_current_board() == normed:
        clear_current_board()

    # A concurrent connect(board=normed) after the rename/delete recreates
    # an empty sqlite file via mkdir(exist_ok=True); the cache entry must be
    # dropped first so the schema init pass re-runs on that fresh file.
    _INITIALIZED_PATHS.discard(str((d / "kanban.db").resolve()))

    if archive:
        archive_root = boards_root() / "_archived"
        archive_root.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        target = archive_root / f"{normed}-{ts}"
        # Avoid collision on rapid double-archives.
        suffix = 1
        while target.exists():
            target = archive_root / f"{normed}-{ts}-{suffix}"
            suffix += 1
        d.rename(target)
        return {"slug": normed, "action": "archived", "new_path": str(target)}
    else:
        import shutil
        shutil.rmtree(d)
        return {"slug": normed, "action": "deleted", "new_path": ""}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """In-memory view of a row from the ``tasks`` table."""

    id: str
    title: str
    body: Optional[str]
    assignee: Optional[str]
    status: str
    priority: int
    created_by: Optional[str]
    created_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    workspace_kind: str
    workspace_path: Optional[str]
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    tenant: Optional[str]
    branch_name: Optional[str] = None
    result: Optional[str] = None
    idempotency_key: Optional[str] = None
    # Unified non-success counter. Incremented on any of:
    #   * spawn failure (dispatcher couldn't launch the worker)
    #   * timed_out outcome (worker exceeded max_runtime_seconds)
    #   * crashed outcome (worker PID vanished)
    # Reset to 0 only on a successful completion. See
    # ``_record_task_failure`` for the circuit-breaker trip rule.
    # (Pre-rename column: ``spawn_failures``.)
    consecutive_failures: int = 0
    worker_pid: Optional[int] = None
    # Short excerpt of the last failure's error text (any outcome, not
    # just spawn). Pre-rename column: ``last_spawn_error``.
    last_failure_error: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    current_run_id: Optional[int] = None
    workflow_template_id: Optional[str] = None
    current_step_key: Optional[str] = None
    # Force-loaded skills for the worker on this task (appended to the
    # dispatcher's built-in `kanban-worker` via --skills). Stored as a
    # JSON array of skill names. None = use only the defaults; empty
    # list = explicitly no extra skills.
    skills: Optional[list] = None
    model_override: Optional[str] = None
    # Per-task override for the consecutive-failure circuit breaker.
    # The value is the failure count at which the breaker trips — e.g.
    # ``max_retries=1`` blocks on the first failure (zero retries),
    # ``max_retries=3`` blocks on the third (two retries allowed).
    # ``None`` (the common case) falls through to the dispatcher-level
    # ``kanban.failure_limit`` config, and then to ``DEFAULT_FAILURE_LIMIT``.
    # Name matches the ``--max-retries`` CLI flag on ``kanban create``.
    max_retries: Optional[int] = None
    # Originating chat/agent session id, when the task was created from
    # within an agent loop that propagated ``HERMES_SESSION_ID``. NULL for
    # tasks created from the CLI, the dashboard, or any path that doesn't
    # set the env var. Lets clients render a per-session board without
    # relying on tenant + time-window heuristics.
    session_id: Optional[str] = None
    # Optional semantic funnel coordinates. These do NOT drive dispatch;
    # they let optimizers and UIs group the same lifecycle cards into a
    # goal/workstream/stage/action read-model without hardcoded columns or
    # domain-specific title inference.
    goal_id: Optional[str] = None
    workstream_id: Optional[str] = None
    stage_key: Optional[str] = None
    action_key: Optional[str] = None
    funnel_data: Optional[dict] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        keys = set(row.keys())
        # Parse skills JSON blob if present
        skills_value: Optional[list] = None
        if "skills" in keys and row["skills"]:
            try:
                parsed = json.loads(row["skills"])
                if isinstance(parsed, list):
                    skills_value = [str(s) for s in parsed if s]
            except Exception:
                skills_value = None
        funnel_data_value: Optional[dict] = None
        if "funnel_data" in keys and row["funnel_data"]:
            try:
                parsed = json.loads(row["funnel_data"])
                if isinstance(parsed, dict):
                    funnel_data_value = parsed
            except Exception:
                funnel_data_value = None
        return cls(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            assignee=row["assignee"],
            status=row["status"],
            priority=row["priority"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            workspace_kind=row["workspace_kind"],
            workspace_path=row["workspace_path"],
            branch_name=row["branch_name"] if "branch_name" in keys else None,
            claim_lock=row["claim_lock"],
            claim_expires=row["claim_expires"],
            tenant=row["tenant"] if "tenant" in keys else None,
            result=row["result"] if "result" in keys else None,
            idempotency_key=row["idempotency_key"] if "idempotency_key" in keys else None,
            consecutive_failures=(
                row["consecutive_failures"] if "consecutive_failures" in keys
                # Pre-migration fallback: ``_migrate_add_optional_columns`` always
                # adds ``consecutive_failures`` now, so this branch is only reachable
                # on a DB that was never opened since pre-#20410 code ran. Keep for
                # belt-and-suspenders safety; in practice it is dead code post-migration.
                else (row["spawn_failures"] if "spawn_failures" in keys else 0)
            ),
            worker_pid=row["worker_pid"] if "worker_pid" in keys else None,
            last_failure_error=(
                row["last_failure_error"] if "last_failure_error" in keys
                # Same belt-and-suspenders fallback as consecutive_failures above.
                else (row["last_spawn_error"] if "last_spawn_error" in keys else None)
            ),
            max_runtime_seconds=(
                row["max_runtime_seconds"] if "max_runtime_seconds" in keys else None
            ),
            last_heartbeat_at=(
                row["last_heartbeat_at"] if "last_heartbeat_at" in keys else None
            ),
            current_run_id=(
                row["current_run_id"] if "current_run_id" in keys else None
            ),
            workflow_template_id=(
                row["workflow_template_id"] if "workflow_template_id" in keys else None
            ),
            current_step_key=(
                row["current_step_key"] if "current_step_key" in keys else None
            ),
            skills=skills_value,
            model_override=row["model_override"] if "model_override" in keys and row["model_override"] else None,
            max_retries=(
                row["max_retries"] if "max_retries" in keys else None
            ),
            session_id=(
                row["session_id"] if "session_id" in keys else None
            ),
            goal_id=row["goal_id"] if "goal_id" in keys else None,
            workstream_id=(
                row["workstream_id"] if "workstream_id" in keys else None
            ),
            stage_key=row["stage_key"] if "stage_key" in keys else None,
            action_key=row["action_key"] if "action_key" in keys else None,
            funnel_data=funnel_data_value,
        )


@dataclass
class Run:
    """In-memory view of a ``task_runs`` row.

    A run is one attempt to execute a task — created on claim, closed
    on complete/block/crash/timeout/spawn_failure/reclaim. Multiple runs
    per task when retries happen. Carries the claim machinery, PID,
    heartbeat, and the structured handoff summary that downstream workers
    read via ``build_worker_context``.
    """

    id: int
    task_id: str
    profile: Optional[str]
    step_key: Optional[str]
    status: str
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    worker_pid: Optional[int]
    max_runtime_seconds: Optional[int]
    last_heartbeat_at: Optional[int]
    started_at: int
    ended_at: Optional[int]
    outcome: Optional[str]
    summary: Optional[str]
    metadata: Optional[dict]
    error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Run":
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else None
        except Exception:
            meta = None
        return cls(
            id=int(row["id"]),
            task_id=row["task_id"],
            profile=row["profile"],
            step_key=row["step_key"],
            status=row["status"],
            claim_lock=row["claim_lock"],
            claim_expires=row["claim_expires"],
            worker_pid=row["worker_pid"],
            max_runtime_seconds=row["max_runtime_seconds"],
            last_heartbeat_at=row["last_heartbeat_at"],
            started_at=int(row["started_at"]),
            ended_at=(int(row["ended_at"]) if row["ended_at"] is not None else None),
            outcome=row["outcome"],
            summary=row["summary"],
            metadata=meta,
            error=row["error"],
        )


@dataclass
class Comment:
    id: int
    task_id: str
    author: str
    body: str
    created_at: int


@dataclass
class Event:
    id: int
    task_id: str
    kind: str
    payload: Optional[dict]
    created_at: int
    run_id: Optional[int] = None


@dataclass
class PixelEvent:
    id: int
    event_type: str
    stage_key: str
    task_id: Optional[str]
    status: str
    evidence: str
    created_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PixelEvent":
        return cls(
            id=int(row["id"]),
            event_type=row["event_type"],
            stage_key=row["stage_key"],
            task_id=row["task_id"],
            status=row["status"],
            evidence=row["evidence"],
            created_at=int(row["created_at"]),
        )


@dataclass
class PixelClaim:
    id: int
    lane_id: str
    task_id: Optional[str]
    agent_id: str
    claim_token: str
    evidence: str
    active: bool
    claimed_at: int
    released_at: Optional[int]
    release_evidence: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PixelClaim":
        return cls(
            id=int(row["id"]),
            lane_id=row["lane_id"],
            task_id=row["task_id"],
            agent_id=row["agent_id"],
            claim_token=row["claim_token"],
            evidence=row["evidence"],
            active=bool(row["active"]),
            claimed_at=int(row["claimed_at"]),
            released_at=(int(row["released_at"]) if row["released_at"] is not None else None),
            release_evidence=row["release_evidence"],
        )


@dataclass
class WatchRoute:
    """Event route that wakes a task parked in the healthy ``watching`` state."""

    id: int
    task_id: str
    trigger_type: str
    trigger_key: Optional[str]
    wake_status: str
    reason: Optional[str]
    payload: Optional[dict]
    active: bool
    created_by: Optional[str]
    created_at: int
    triggered_at: Optional[int]
    trigger_payload: Optional[dict]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WatchRoute":
        payload = None
        if row["payload"]:
            try:
                parsed = json.loads(row["payload"])
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = None
        trigger_payload = None
        if row["trigger_payload"]:
            try:
                parsed = json.loads(row["trigger_payload"])
                if isinstance(parsed, dict):
                    trigger_payload = parsed
            except Exception:
                trigger_payload = None
        return cls(
            id=int(row["id"]),
            task_id=row["task_id"],
            trigger_type=row["trigger_type"],
            trigger_key=row["trigger_key"],
            wake_status=row["wake_status"],
            reason=row["reason"],
            payload=payload,
            active=bool(row["active"]),
            created_by=row["created_by"],
            created_at=int(row["created_at"]),
            triggered_at=(int(row["triggered_at"]) if row["triggered_at"] is not None else None),
            trigger_payload=trigger_payload,
        )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    -- Unified consecutive-failure counter. Incremented on spawn
    -- failure, timeout, or crash; reset only on successful completion.
    -- The circuit breaker in _record_task_failure trips when this
    -- exceeds DEFAULT_FAILURE_LIMIT consecutive non-successes.
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    -- Short excerpt of the most recent failure's error text.
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    -- Pointer into task_runs for the currently-active run (NULL if no
    -- run is in-flight). Denormalised for cheap reads.
    current_run_id       INTEGER,
    -- Forward-compat for v2 workflow routing. In v1 the kernel writes
    -- these when the task is opted into a template but otherwise ignores
    -- them; the dispatcher doesn't consult them for routing yet.
    workflow_template_id TEXT,
    current_step_key     TEXT,
    -- Force-loaded skills for the worker on this task, stored as JSON.
    -- Appended to the dispatcher's built-in `--skills kanban-worker`.
    -- NULL or empty array = no extras.
    skills               TEXT,
    -- Per-task model override. When set, the dispatcher passes -m <model>
    -- to the worker, overriding the profile's default model. NULL = use
    -- the profile default.
    model_override       TEXT,
    -- Per-task override for the consecutive-failure circuit breaker.
    -- The value is the failure count at which the breaker trips — e.g.
    -- ``max_retries=1`` blocks on the first failure. NULL (the common
    -- case) falls through to the dispatcher-level ``kanban.failure_limit``
    -- config and then ``DEFAULT_FAILURE_LIMIT``.
    max_retries          INTEGER,
    -- Originating chat/agent session id when the task was created from
    -- inside an agent loop that propagated ``HERMES_SESSION_ID``. NULL
    -- for tasks created from the CLI, dashboard, or any path that doesn't
    -- set the env var. Indexed so per-session list queries stay cheap on
    -- larger boards.
    session_id           TEXT,
    -- Semantic funnel coordinates for optimizer/UI read-models. These
    -- fields are descriptive only; dispatch still uses lifecycle status,
    -- assignee, and task_links.
    goal_id              TEXT,
    workstream_id        TEXT,
    stage_key            TEXT,
    action_key           TEXT,
    funnel_data          TEXT
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);

-- Historical attempt record. Each time the dispatcher claims a task, a
-- new row is created here; claim state, PID, heartbeat, runtime cap,
-- and structured summary all live on the run, not the task. Multiple
-- rows per task id when the task was retried after crash/timeout/block.
-- v2 of the kanban schema will use ``step_key`` to drive per-stage
-- workflow routing; in v1 the column is nullable and unused (kernel
-- ignores it).
CREATE TABLE IF NOT EXISTS task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    -- status: running | done | blocked | crashed | timed_out | failed | released
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    -- outcome: completed | blocked | crashed | timed_out | spawn_failed |
    --          gave_up | reclaimed | (null while still running)
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);

-- Subscription from a gateway source (platform + chat + thread) to a
-- task. The gateway's kanban-notifier watcher tails task_events and
-- pushes ``completed`` / ``blocked`` / ``spawn_auto_blocked`` events to
-- the original requester so human-in-the-loop workflows close the loop.
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    notifier_profile TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);

-- Event-driven wake routes for cards in the healthy waiting state.
-- A route is active while the card is ``status='watching'`` and becomes
-- inactive once a matching event/timer/dependency/manual trigger wakes it.
CREATE TABLE IF NOT EXISTS task_watch_routes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    trigger_type    TEXT NOT NULL,
    trigger_key     TEXT,
    wake_status     TEXT NOT NULL DEFAULT 'ready',
    reason          TEXT,
    payload         TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    created_by      TEXT,
    created_at      INTEGER NOT NULL,
    triggered_at    INTEGER,
    trigger_payload TEXT
);

-- Native Kanban Pixel ledger. Pixel state intentionally lives in the board DB
-- and board metadata, never in .humanless_pixel sidecars.
CREATE TABLE IF NOT EXISTS kanban_pixel_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    stage_key   TEXT NOT NULL,
    task_id     TEXT,
    status      TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kanban_pixel_claims (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    lane_id          TEXT NOT NULL,
    task_id          TEXT,
    agent_id         TEXT NOT NULL,
    claim_token      TEXT NOT NULL,
    evidence         TEXT NOT NULL,
    active           INTEGER NOT NULL DEFAULT 1,
    claimed_at       INTEGER NOT NULL,
    released_at      INTEGER,
    release_evidence TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_links_child           ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent          ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_task         ON task_comments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task           ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_task             ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status           ON task_runs(status);
CREATE INDEX IF NOT EXISTS idx_notify_task           ON kanban_notify_subs(task_id);
CREATE INDEX IF NOT EXISTS idx_watch_task            ON task_watch_routes(task_id, active);
CREATE INDEX IF NOT EXISTS idx_watch_trigger         ON task_watch_routes(trigger_type, trigger_key, active);
CREATE INDEX IF NOT EXISTS idx_pixel_events_stage    ON kanban_pixel_events(stage_key, event_type, status, created_at);
CREATE INDEX IF NOT EXISTS idx_pixel_events_task     ON kanban_pixel_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pixel_claims_active   ON kanban_pixel_claims(active, lane_id, task_id);
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

_INITIALIZED_PATHS: set[str] = set()
_INIT_LOCK = threading.RLock()
_SQLITE_HEADER = b"SQLite format 3\x00"

# ---------------------------------------------------------------------------
# Cross-process file lock
# ---------------------------------------------------------------------------
# Every process that opens a kanban DB acquires an exclusive flock on a
# sibling lockfile (.kanban.lock) in the same directory. This serializes
# all writers across processes — the single-owner guarantee that prevents
# WAL corruption from concurrent checkpoint/write races.
#
# The lock is held for the lifetime of the connection via _LockedConnection,
# which releases it on close(). Within a single process, threads are
# serialized by _INIT_LOCK (threading.RLock) as before.

_LOCK_FDS: dict[str, int] = {}  # path -> fd, so we don't double-lock in-process


def _acquire_db_lock(db_path: Path) -> int:
    """Acquire an exclusive cross-process lock for a kanban DB directory.

    Returns the file descriptor (kept open to hold the lock). The lock is
    non-blocking for the first 30s via retry, then raises if still contended.
    """
    if fcntl is None:
        raise sqlite3.OperationalError("kanban DB file locking requires fcntl on this platform")
    lockfile = db_path.parent / ".kanban.lock"
    resolved = str(lockfile.resolve())
    # If this process already holds the lock (re-entrant connect), return existing fd
    if resolved in _LOCK_FDS:
        return _LOCK_FDS[resolved]
    fd = os.open(str(lockfile), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        # Try non-blocking first
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            # Another process holds it — block with timeout via polling
            deadline = time.monotonic() + 30
            acquired = False
            while time.monotonic() < deadline:
                time.sleep(0.1)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except (OSError, BlockingIOError):
                    continue
            if not acquired:
                os.close(fd)
                raise sqlite3.OperationalError(
                    f"kanban DB lock timeout after 30s: {db_path} — "
                    f"another process is holding .kanban.lock"
                )
    except Exception:
        os.close(fd)
        raise
    _LOCK_FDS[resolved] = fd
    return fd


def _release_db_lock(db_path: Path) -> None:
    """Release the cross-process lock for a kanban DB directory."""
    if fcntl is None:
        return
    lockfile = db_path.parent / ".kanban.lock"
    resolved = str(lockfile.resolve())
    fd = _LOCK_FDS.pop(resolved, None)
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


class _LockedConnection:
    """Wraps a sqlite3.Connection, its board identity, and optional file lock.

    Proxies all attribute access to the underlying connection so callers
    see a normal sqlite3.Connection interface.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        db_path: Path,
        board: Optional[str] = None,
        *,
        release_lock: bool = True,
    ):
        object.__setattr__(self, '_conn', conn)
        object.__setattr__(self, '_db_path', db_path)
        object.__setattr__(self, '_board_slug', board)
        object.__setattr__(self, '_release_lock', release_lock)
        object.__setattr__(self, '_closed', False)

    def close(self):
        if not object.__getattribute__(self, '_closed'):
            object.__setattr__(self, '_closed', True)
            conn = object.__getattribute__(self, '_conn')
            db_path = object.__getattribute__(self, '_db_path')
            try:
                conn.close()
            finally:
                if object.__getattribute__(self, '_release_lock'):
                    _release_db_lock(db_path)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_conn'), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, '_conn'), name, value)


def _looks_like_tls_record_at(data: bytes, offset: int) -> bool:
    """Return True for a TLS record header at ``data[offset:]``."""
    if len(data) < offset + 5:
        return False
    content_type = data[offset]
    major = data[offset + 1]
    minor = data[offset + 2]
    length = int.from_bytes(data[offset + 3:offset + 5], "big")
    return (
        content_type in {0x14, 0x15, 0x16, 0x17}
        and major == 0x03
        and minor in {0x00, 0x01, 0x02, 0x03, 0x04}
        and 0 < length <= 18432
    )


def _validate_sqlite_header(path: Path) -> None:
    """Fail early with an actionable error for non-SQLite Kanban DB files.

    ``sqlite3.connect()`` creates missing and zero-byte files, so those are
    allowed. Existing non-empty files must have the SQLite header before we
    hand them to SQLite/WAL setup. This keeps corrupted page-0 failures from
    being collapsed into a generic PRAGMA error and lets the gateway's corrupt
    board handling identify the board by fingerprint.
    """
    try:
        stat = path.stat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if stat.st_size == 0:
        return
    try:
        with path.open("rb") as handle:
            head = handle.read(64)
    except OSError:
        return
    if head.startswith(_SQLITE_HEADER):
        return
    signature = ""
    if head.startswith(b"SQLit") and _looks_like_tls_record_at(head, 5):
        signature = " (TLS record header detected at byte offset 5)"
    elif _looks_like_tls_record_at(head, 0):
        signature = " (TLS record header detected at byte offset 0)"
    raise sqlite3.DatabaseError(
        "file is not a database: invalid SQLite header for "
        f"{path}{signature}; first_32={head[:32].hex(' ')}"
    )


class KanbanDbCorruptError(RuntimeError):
    """Raised when an existing kanban DB file fails integrity checks.

    Fail-closed guard against silent recreation of a corrupt board file,
    which would otherwise destroy the user's tasks. Carries both the
    original path and the timestamped backup we made before refusing.
    """

    def __init__(self, db_path: Path, backup_path: Optional[Path], reason: str):
        self.db_path = db_path
        self.backup_path = backup_path
        self.reason = reason
        backup_str = str(backup_path) if backup_path is not None else "<backup failed>"
        super().__init__(
            f"Refusing to open corrupt kanban DB at {db_path}: {reason}. "
            f"Original preserved; backup at {backup_str}."
        )


def _backup_corrupt_db(path: Path) -> Optional[Path]:
    """Copy a corrupt DB (and its WAL/SHM sidecars) to a timestamped backup.

    Returns the backup path of the main DB file, or ``None`` if the copy
    itself failed (the caller still raises loudly in that case).

    Writes are confined to the original DB's parent directory. The
    backup basename is derived purely from ``path.name``, never from
    caller-supplied directory segments — no traversal is possible.
    """
    # Resolve once and pin the parent so subsequent path operations cannot
    # escape it. ``Path.resolve()`` collapses any ``..`` segments and
    # symlinks, and we only ever write inside ``parent``.
    resolved = path.resolve()
    parent = resolved.parent
    base_name = resolved.name  # basename only
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = parent / f"{base_name}.corrupt.{stamp}.bak"
    # Defensive: candidate must still be inside parent after construction.
    # f-string interpolation of ``base_name`` cannot escape ``parent``
    # because ``base_name`` is itself a resolved basename, but assert it
    # anyway so static analyzers can see the containment guarantee.
    if candidate.parent != parent:
        return None
    counter = 0
    while candidate.exists():
        counter += 1
        candidate = parent / f"{base_name}.corrupt.{stamp}.{counter}.bak"
        if candidate.parent != parent:
            return None
    try:
        shutil.copy2(resolved, candidate)
    except OSError:
        return None
    for suffix in ("-wal", "-shm"):
        sidecar = parent / (base_name + suffix)
        if sidecar.parent != parent or not sidecar.exists():
            continue
        try:
            sidecar_backup = parent / (candidate.name + suffix)
            if sidecar_backup.parent != parent:
                continue
            shutil.copy2(sidecar, sidecar_backup)
        except OSError:
            pass
    return candidate


def _guard_existing_db_is_healthy(path: Path) -> None:
    """Run ``PRAGMA integrity_check`` on an existing non-empty DB file.

    If corruption is detected, attempts a multi-step recovery before raising:

    1. WAL checkpoint (TRUNCATE) — often the WAL is fine, just needs flushing
    2. ``.recover`` — SQLite's built-in row-level recovery into a new DB
    3. Restore from most recent known-good guardian backup

    Only raises :class:`KanbanDbCorruptError` if ALL recovery steps fail.
    On successful recovery, logs a warning but does NOT raise — the caller
    proceeds with the repaired DB.

    Transient lock/busy errors (``sqlite3.OperationalError``) are NOT
    treated as corruption; they propagate raw so the caller sees a
    normal lock failure and no spurious ``.corrupt`` backup is made.

    No-op for missing files, zero-byte files (treated as fresh), and
    paths already proven healthy this process (cache hit).
    """
    # Resolve before any I/O.
    try:
        resolved = path.resolve()
    except OSError:
        return
    try:
        if not resolved.exists() or resolved.stat().st_size == 0:
            return
    except OSError:
        return
    if str(resolved) in _INITIALIZED_PATHS:
        return
    reason: Optional[str] = None
    try:
        probe = sqlite3.connect(str(resolved), timeout=5, isolation_level=None)
        try:
            row = probe.execute("PRAGMA integrity_check").fetchone()
        finally:
            probe.close()
        if not row or (row[0] or "").lower() != "ok":
            reason = f"integrity_check returned {row[0] if row else '<no row>'!r}"
    except sqlite3.OperationalError:
        # Lock contention, busy, transient IO — not corruption.
        raise
    except sqlite3.DatabaseError as exc:
        reason = f"sqlite refused to open file: {exc}"
    if reason is None:
        return

    # --- Recovery cascade ---
    _log.warning("Kanban DB corruption detected at %s: %s. Attempting recovery...", resolved, reason)

    # Step 1: Try WAL checkpoint (sometimes WAL just needs flushing)
    try:
        probe = sqlite3.connect(str(resolved), timeout=5, isolation_level=None)
        try:
            probe.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            row = probe.execute("PRAGMA integrity_check").fetchone()
            if row and (row[0] or "").lower() == "ok":
                _log.warning("Recovery succeeded via WAL checkpoint for %s", resolved)
                probe.close()
                return
        finally:
            probe.close()
    except Exception:
        pass

    # Step 2: Try .recover (SQLite's row-level recovery)
    try:
        import subprocess as _sp
        recovered_path = resolved.parent / f"{resolved.name}.recovered"
        result = _sp.run(
            ["sqlite3", str(resolved), ".recover"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            # Pipe recovered SQL into a new DB
            result2 = _sp.run(
                ["sqlite3", str(recovered_path)],
                input=result.stdout, capture_output=True, text=True, timeout=30
            )
            if result2.returncode == 0:
                # Verify the recovered DB
                verify = sqlite3.connect(str(recovered_path), timeout=5)
                try:
                    row = verify.execute("PRAGMA integrity_check").fetchone()
                    count = verify.execute("SELECT COUNT(*) FROM tasks").fetchone()
                finally:
                    verify.close()
                if row and row[0] == "ok" and count and count[0] > 0:
                    # Recovery succeeded — swap files
                    _backup_corrupt_db(resolved)
                    # Remove corrupt original + sidecars
                    for suffix in ("", "-wal", "-shm"):
                        try:
                            (resolved.parent / (resolved.name + suffix)).unlink()
                        except FileNotFoundError:
                            pass
                    recovered_path.rename(resolved)
                    _log.warning(
                        "Recovery succeeded via .recover for %s (%d tasks restored)",
                        resolved, count[0]
                    )
                    return
        # Clean up failed recovery attempt
        try:
            recovered_path.unlink()
        except FileNotFoundError:
            pass
    except Exception:
        pass

    # Step 3: Restore from most recent guardian backup
    try:
        backup_dir = resolved.parent.parent.parent / "backups"
        board_name = resolved.parent.name
        backups = sorted(
            backup_dir.glob(f"{board_name}_kanban_*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        for backup_path in backups[:3]:  # Try up to 3 most recent
            verify = sqlite3.connect(str(backup_path), timeout=5)
            try:
                row = verify.execute("PRAGMA integrity_check").fetchone()
                count = verify.execute("SELECT COUNT(*) FROM tasks").fetchone()
            finally:
                verify.close()
            if row and row[0] == "ok" and count and count[0] > 0:
                # Backup is good — swap in
                _backup_corrupt_db(resolved)
                for suffix in ("", "-wal", "-shm"):
                    try:
                        (resolved.parent / (resolved.name + suffix)).unlink()
                    except FileNotFoundError:
                        pass
                shutil.copy2(backup_path, resolved)
                _log.warning(
                    "Recovery succeeded via backup restore for %s (%d tasks, from %s)",
                    resolved, count[0], backup_path.name
                )
                return
    except Exception:
        pass

    # All recovery failed — preserve and raise
    backup = _backup_corrupt_db(resolved)
    raise KanbanDbCorruptError(resolved, backup, reason)


def connect(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> sqlite3.Connection:
    """Open (and initialize if needed) the kanban DB.

    WAL mode is enabled on every connection; it's a no-op after the first
    time but keeps the code robust if the DB file is ever re-created.

    A cross-process file lock (.kanban.lock) is acquired before opening the
    DB and held for the lifetime of the returned connection. This serializes
    all writers across processes, preventing WAL corruption from concurrent
    checkpoint/write races. The lock is released when close() is called.

    The first connection to a given path auto-runs :func:`init_db` so
    fresh installs and test harnesses that construct `connect()`
    directly don't have to remember a separate init step. Subsequent
    connections skip the schema check via a module-level path cache.

    Path resolution:

    * ``db_path`` explicit → used as-is (legacy callers, tests).
    * ``board`` explicit → resolves to that board's DB.
    * Neither → :func:`kanban_db_path` resolves via
      ``HERMES_KANBAN_DB`` env → ``HERMES_KANBAN_BOARD`` env →
      ``<root>/kanban/current`` → ``default``.
    """
    if db_path is not None:
        path = db_path
    else:
        path = kanban_db_path(board=board)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Acquire cross-process lock BEFORE any DB I/O. On Windows, skip
    # (fcntl is Unix-only) and fall back to SQLite's built-in locking.
    if not _IS_WINDOWS:
        _acquire_db_lock(path)
    try:
        # Cheap byte-level check first — catches the #29507 TLS-overwrite shape
        # and other invalid-header cases without opening a sqlite connection.
        _validate_sqlite_header(path)
        # Full integrity probe — catches corruption past the header (malformed
        # pages, broken internal metadata). Cached per-path after first success
        # via _INITIALIZED_PATHS so it only runs once per process per path.
        _guard_existing_db_is_healthy(path)
        resolved = str(path.resolve())
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            with _INIT_LOCK:
                # WAL activation can take an exclusive lock while SQLite creates the
                # sidecar files for a fresh database. Keep it in the same process-local
                # critical section as schema initialization so concurrent gateway
                # startup threads do not race before _INITIALIZED_PATHS is populated.
                # WAL doesn't work on network filesystems (NFS/SMB/FUSE). Shared helper
                # falls back to DELETE with one WARNING so kanban stays usable there.
                # See hermes_state._WAL_INCOMPAT_MARKERS for detection logic.
                from hermes_state import apply_wal_with_fallback
                apply_wal_with_fallback(conn, db_label=f"kanban.db ({path.name})")
                # FULL (was NORMAL): fsync before each checkpoint to narrow the
                # crash window that can leave a b-tree page header torn.
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA wal_autocheckpoint=100")
                conn.execute("PRAGMA foreign_keys=ON")
                # Zero freed pages so a later torn write cannot expose stale
                # cell content; persisted in the DB header for new DBs.
                conn.execute("PRAGMA secure_delete=ON")
                # Surface corrupt cells as read errors instead of silent
                # wrong-data returns.
                conn.execute("PRAGMA cell_size_check=ON")
                needs_init = resolved not in _INITIALIZED_PATHS
                if needs_init:
                    # Idempotent: runs CREATE TABLE IF NOT EXISTS + the additive
                    # migrations. Cached so subsequent connect() calls in the same
                    # process are cheap. The lock prevents same-process dispatcher
                    # threads from racing through the additive ALTER TABLE pass with
                    # stale PRAGMA snapshots during gateway startup.
                    conn.executescript(SCHEMA_SQL)
                    _migrate_add_optional_columns(conn)
                    _INITIALIZED_PATHS.add(resolved)
        except Exception:
            conn.close()
            raise
    except Exception:
        if not _IS_WINDOWS:
            _release_db_lock(path)
        raise
    # Always attach board identity to the connection wrapper. File locking is
    # POSIX-only, but board identity is a cross-platform safety invariant.
    try:
        db_path_board = _board_slug_for_db_path(path)
        try:
            env_board = _normalize_board_slug(os.environ.get("HERMES_KANBAN_BOARD"))
        except ValueError:
            env_board = None
        requested_board = _normalize_board_slug(board)
        if requested_board is None and env_board and (
            os.environ.get("HERMES_KANBAN_DB") or board_exists(env_board)
        ):
            requested_board = env_board
        if db_path_board:
            if requested_board and requested_board != db_path_board:
                raise ValueError(
                    f"board argument {requested_board!r} does not match pinned DB board {db_path_board!r}"
                )
            board_slug = db_path_board
        else:
            board_slug = _connection_board(conn, board)
    except Exception:
        conn.close()
        if not _IS_WINDOWS:
            _release_db_lock(path)
        raise
    return _LockedConnection(conn, path, board_slug, release_lock=not _IS_WINDOWS)  # type: ignore[return-value]


def init_db(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> Path:
    """Create the schema if it doesn't exist; return the path used.

    Kept as a public entry point so CLI ``hermes kanban init`` and the
    daemon have something explicit to call. Unlike :func:`connect`'s
    first-time auto-init (which caches by path), ``init_db`` always
    re-runs the migration pass. Callers that know the on-disk schema
    may have drifted — tests that write legacy event kinds directly,
    external tools that upgrade an old DB file — can call this to
    force re-migration.
    """
    if db_path is not None:
        path = db_path
    else:
        path = kanban_db_path(board=board)
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    # Clear the cache entry so the underlying connect() re-runs the
    # schema + migration pass unconditionally.
    with _INIT_LOCK:
        _INITIALIZED_PATHS.discard(resolved)
    with contextlib.closing(connect(path)):
        pass
    return path


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> bool:
    """Run ``ALTER TABLE <table> ADD COLUMN <ddl>``, idempotent across races.

    Returns ``True`` when the column was actually added by this call.
    Swallows ``duplicate column name`` errors so a concurrent connection
    that ran the same migration first does not crash the dispatcher tick
    (issue #21708).
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        return True
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            return False
        raise


def _migrate_add_optional_columns(conn: sqlite3.Connection) -> None:
    """Add columns that were introduced after v1 release to legacy DBs.

    Called by ``init_db`` so opening an old DB is always safe.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "tenant" not in cols:
        _add_column_if_missing(conn, "tasks", "tenant", "tenant TEXT")
    if "result" not in cols:
        _add_column_if_missing(conn, "tasks", "result", "result TEXT")
    if "branch_name" not in cols:
        _add_column_if_missing(conn, "tasks", "branch_name", "branch_name TEXT")
    if "idempotency_key" not in cols:
        _add_column_if_missing(
            conn, "tasks", "idempotency_key", "idempotency_key TEXT"
        )
    # ``idx_tasks_idempotency`` is created unconditionally below alongside
    # the other additive-column indexes — see the block after the
    # legacy-column migration. Creating it here too would be redundant.

    # Refresh after early additive migrations above. Some existing DBs were
    # partially migrated in older releases and can already contain the later
    # columns (for example ``consecutive_failures``) even when this function's
    # initial snapshot did not. Re-snapshot here so the legacy-column migration
    # below is truly idempotent and never re-adds columns that already exist.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}

    # Legacy column migration: ``spawn_failures`` → ``consecutive_failures``
    # and ``last_spawn_error`` → ``last_failure_error``.
    #
    # Avoid ``ALTER TABLE ... RENAME COLUMN`` for two reasons:
    #   1. Primary: very old DBs may never have had ``spawn_failures`` at
    #      all, so RENAME raises OperationalError: no such column (the crash
    #      reported in issue #20842 after the #20410 update).
    #   2. Secondary: SQLite reparses the whole schema on any RENAME, which
    #      fails if related objects (views, triggers) reference the old name.
    #
    # ADD-first-then-copy is tolerant of both shapes and preserves
    # historical counter values when the legacy columns do exist.
    if "consecutive_failures" not in cols:
        added = _add_column_if_missing(
            conn,
            "tasks",
            "consecutive_failures",
            "consecutive_failures INTEGER NOT NULL DEFAULT 0",
        )
        if added and "spawn_failures" in cols:
            conn.execute(
                "UPDATE tasks SET consecutive_failures = COALESCE(spawn_failures, 0)"
            )
    if "worker_pid" not in cols:
        _add_column_if_missing(conn, "tasks", "worker_pid", "worker_pid INTEGER")
    if "last_failure_error" not in cols:
        added = _add_column_if_missing(
            conn, "tasks", "last_failure_error", "last_failure_error TEXT"
        )
        if added and "last_spawn_error" in cols:
            conn.execute(
                "UPDATE tasks SET last_failure_error = last_spawn_error"
            )
    if "max_runtime_seconds" not in cols:
        _add_column_if_missing(
            conn, "tasks", "max_runtime_seconds", "max_runtime_seconds INTEGER"
        )
    if "last_heartbeat_at" not in cols:
        _add_column_if_missing(
            conn, "tasks", "last_heartbeat_at", "last_heartbeat_at INTEGER"
        )
    if "current_run_id" not in cols:
        _add_column_if_missing(
            conn, "tasks", "current_run_id", "current_run_id INTEGER"
        )
    if "workflow_template_id" not in cols:
        _add_column_if_missing(
            conn, "tasks", "workflow_template_id", "workflow_template_id TEXT"
        )
    if "current_step_key" not in cols:
        _add_column_if_missing(
            conn, "tasks", "current_step_key", "current_step_key TEXT"
        )
    if "skills" not in cols:
        # JSON array of skill names the dispatcher force-loads into the
        # worker (additive to the built-in `kanban-worker`). NULL is fine
        # for existing rows.
        _add_column_if_missing(conn, "tasks", "skills", "skills TEXT")

    if "max_retries" not in cols:
        # Per-task override for the consecutive-failure circuit breaker.
        # NULL = fall through to the dispatcher-level ``kanban.failure_limit``
        # config, then ``DEFAULT_FAILURE_LIMIT``. Existing rows get NULL,
        # which is the correct default (they keep the global behaviour
        # they were getting before the column existed).
        _add_column_if_missing(conn, "tasks", "max_retries", "max_retries INTEGER")

    if "model_override" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN model_override TEXT")

    if "session_id" not in cols:
        # Originating agent/chat session id, populated when the task is
        # created from within an agent loop that propagated
        # ``HERMES_SESSION_ID`` (e.g. ACP). NULL on legacy rows and on any
        # creation path that doesn't set the env var (CLI, dashboard).
        _add_column_if_missing(
            conn, "tasks", "session_id", "session_id TEXT"
        )

    # Semantic funnel coordinates. Additive and nullable so old boards keep
    # their exact dispatch semantics; the funnel read-model falls back to
    # workflow_template_id/current_step_key and finally an unclassified bucket.
    if "goal_id" not in cols:
        _add_column_if_missing(conn, "tasks", "goal_id", "goal_id TEXT")
    if "workstream_id" not in cols:
        _add_column_if_missing(conn, "tasks", "workstream_id", "workstream_id TEXT")
    if "stage_key" not in cols:
        _add_column_if_missing(conn, "tasks", "stage_key", "stage_key TEXT")
    if "action_key" not in cols:
        _add_column_if_missing(conn, "tasks", "action_key", "action_key TEXT")
    if "funnel_data" not in cols:
        _add_column_if_missing(conn, "tasks", "funnel_data", "funnel_data TEXT")

    # Indexes over additive ``tasks`` columns must be created after the
    # columns exist. Keeping them in SCHEMA_SQL breaks existing board DBs: SQLite
    # parses each statement in ``executescript`` against the live schema, so a
    # ``CREATE INDEX`` over a missing column aborts initialization before the
    # additive ``ALTER TABLE`` migrations below can run. Re-running them here
    # is cheap thanks to ``IF NOT EXISTS`` and stays correct on fresh DBs
    # (where the columns already exist from SCHEMA_SQL).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON tasks(tenant)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_idempotency ON tasks(idempotency_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON tasks(session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_funnel_stage "
        "ON tasks(goal_id, workstream_id, stage_key, action_key)"
    )

    # task_events gained a run_id column; back-fill it as NULL for
    # historical events (they predate runs and can't be attributed).
    ev_cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_events)")}
    if "run_id" not in ev_cols:
        _add_column_if_missing(conn, "task_events", "run_id", "run_id INTEGER")

    # Same ordering rule as the additive ``tasks`` indexes above: create the
    # index after the additive column migration so legacy ``task_events``
    # tables don't fail during SCHEMA_SQL execution before ``run_id`` exists.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_run "
        "ON task_events(run_id, id)"
    )

    notify_table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='kanban_notify_subs'"
    ).fetchone() is not None
    if notify_table_exists:
        notify_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(kanban_notify_subs)")
        }
        if "notifier_profile" not in notify_cols:
            _add_column_if_missing(
                conn, "kanban_notify_subs", "notifier_profile", "notifier_profile TEXT"
            )

    # One-shot backfill: any task that is 'running' before runs existed
    # had its claim_lock / claim_expires / worker_pid on the task row.
    # Synthesize a matching task_runs row so subsequent end-run / heartbeat
    # calls have something to write to. Wrapped in write_txn to serialize
    # against any concurrent dispatcher, and the per-row UPDATE uses
    # ``current_run_id IS NULL`` as a CAS guard so a racing claim can't
    # produce an orphaned row if it interleaves with the backfill pass.
    runs_exist = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'"
    ).fetchone() is not None
    if runs_exist:
        with write_txn(conn):
            inflight = conn.execute(
                "SELECT id, assignee, claim_lock, claim_expires, worker_pid, "
                "       max_runtime_seconds, last_heartbeat_at, started_at "
                "FROM tasks "
                "WHERE status = 'running' AND current_run_id IS NULL"
            ).fetchall()
            for row in inflight:
                started = row["started_at"] or int(time.time())
                cur = conn.execute(
                    """
                    INSERT INTO task_runs (
                        task_id, profile, status,
                        claim_lock, claim_expires, worker_pid,
                        max_runtime_seconds, last_heartbeat_at,
                        started_at
                    ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"], row["assignee"], row["claim_lock"],
                        row["claim_expires"], row["worker_pid"],
                        row["max_runtime_seconds"], row["last_heartbeat_at"],
                        started,
                    ),
                )
                # CAS: only install the pointer if nothing else claimed
                # the task between our SELECT and here (shouldn't happen
                # under the write_txn, but belt-and-suspenders). If the
                # CAS fails we've got an orphan run_row — mark it
                # reclaimed so it doesn't look in-flight.
                upd = conn.execute(
                    "UPDATE tasks SET current_run_id = ? "
                    "WHERE id = ? AND current_run_id IS NULL",
                    (cur.lastrowid, row["id"]),
                )
                if upd.rowcount != 1:
                    conn.execute(
                        "UPDATE task_runs SET status = 'reclaimed', "
                        "    outcome = 'reclaimed', ended_at = ? "
                        "WHERE id = ?",
                        (int(time.time()), cur.lastrowid),
                    )

    # One-shot event-kind rename pass. The old names ("ready", "priority",
    # "spawn_auto_blocked") still worked but were awkward on the wire;
    # rename them in-place so existing DBs migrate cleanly. Fires once
    # per DB because after the UPDATE no rows match the old kinds.
    _EVENT_RENAMES = (
        # (old, new)
        ("ready",              "promoted"),
        ("priority",           "reprioritized"),
        ("spawn_auto_blocked", "gave_up"),
    )
    for old, new in _EVENT_RENAMES:
        conn.execute(
            "UPDATE task_events SET kind = ? WHERE kind = ?",
            (new, old),
        )


def _check_file_length_invariant(conn: sqlite3.Connection) -> None:
    """Read the SQLite header page_count and compare against actual file size.

    Raises sqlite3.DatabaseError if the file is shorter than the header claims
    (torn-extend corruption).
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row is None:
            return
        path_str = row[2]  # column 2 is the file path; empty for in-memory DBs
        if not path_str:
            return  # in-memory or unnamed DB; skip
        path = path_str
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        file_size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(28)
            header_bytes = f.read(4)
        if len(header_bytes) < 4:
            return  # can't read header; skip
        header_page_count = int.from_bytes(header_bytes, "big")
        if header_page_count == 0:
            return  # new/empty DB; skip
        actual_pages = file_size // page_size
        if actual_pages < header_page_count:
            raise sqlite3.DatabaseError(
                f"torn-extend detected: page count mismatch on {path}: "
                f"header claims {header_page_count} pages, "
                f"file has {actual_pages} pages "
                f"(missing {header_page_count - actual_pages} pages, "
                f"file_size={file_size}, page_size={page_size})"
            )
    except sqlite3.DatabaseError:
        raise
    except Exception:
        pass  # I/O errors during check are non-fatal; let normal ops continue


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection):
    """Context manager for an IMMEDIATE write transaction.

    Use for any multi-statement write (creating a task + link, claiming a
    task + recording an event, etc.).  A claim CAS inside this context is
    atomic -- at most one concurrent writer can succeed.

    The explicit ROLLBACK on exception is wrapped in try/except so that
    a SQLite auto-rollback (which leaves no active transaction) does not
    shadow the original exception with a spurious rollback error.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            # SQLite has already auto-rolled-back the transaction (typical
            # under EIO, lock contention, or corruption). Nothing to undo;
            # do not let this secondary failure shadow the real one.
            pass
        raise
    else:
        conn.execute("COMMIT")
        # Post-commit file-length check: header page_count must match actual file pages.
        # A discrepancy means a torn-extend — raise now rather than silently corrupt.
        _check_file_length_invariant(conn)


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def _new_task_id() -> str:
    """Generate a short, URL-safe task id.

    4 hex bytes = ~4.3B possibilities. At 10k tasks the collision
    probability is ~1.2e-5; at 100k it's ~1.2e-3. Previously we used 2
    hex bytes (65k possibilities) which hit the birthday paradox hard:
    ~5% collision probability at 1k tasks, ~50% at 10k. Callers that
    care about idempotency should pass ``idempotency_key`` to
    :func:`create_task` rather than rely on id uniqueness.
    """
    return "t_" + secrets.token_hex(4)


def _claimer_id() -> str:
    """Return a ``host:pid`` string that identifies this claimer."""
    import socket
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


# ---------------------------------------------------------------------------
# Task creation / mutation
# ---------------------------------------------------------------------------

def _canonical_assignee(assignee: Optional[str]) -> Optional[str]:
    """Lowercase-assignee normalization for Kanban rows (dashboard/CLI parity)."""
    if assignee is None:
        return None
    from hermes_cli.profiles import normalize_profile_name

    return normalize_profile_name(assignee)


def _normalize_funnel_text(value: Optional[Any]) -> Optional[str]:
    """Normalize optional semantic funnel coordinates.

    Empty strings, ``none``-style sentinels, and JSON nulls collapse to NULL.
    Values are otherwise stored exactly as caller-provided strings so each
    board can define its own vocabulary without central enums.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "-"}:
        return None
    return text


def _normalize_funnel_data(value: Optional[Any]) -> Optional[dict]:
    """Validate optional funnel_data as a JSON object/dict."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"funnel_data must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(
            f"funnel_data must be a JSON object/dict, got {type(value).__name__}"
        )
    return value


def _validate_task_against_workflow(
    board: Optional[str],
    *,
    goal_id: Optional[str] = None,
    workstream_id: Optional[str] = None,
    stage_key: Optional[str] = None,
    action_key: Optional[str] = None,
    lifecycle_status: Optional[str] = None,
) -> None:
    """Validate declared semantic fields against the board workflow when set."""
    workflow = read_board_metadata(board).get("workflow")
    if not isinstance(workflow, dict):
        return
    stages = _workflow_stage_map(workflow)
    workstreams = _workflow_workstream_map(workflow)
    if workflow.get("require_semantics"):
        default_goal = _normalize_funnel_text(workflow.get("goal_id"))
        if not goal_id and not default_goal:
            raise ValueError("board workflow requires goal_id/--goal on tasks")
        if not workstream_id:
            raise ValueError("board workflow requires workstream_id/--workstream on tasks")
        if not stage_key:
            raise ValueError("board workflow requires stage_key/--stage on tasks")
        if not action_key:
            raise ValueError("board workflow requires action_key/--action on tasks")
    if not stage_key:
        return
    if stage_key not in stages:
        raise ValueError(
            f"stage_key {stage_key!r} is not defined in board workflow "
            f"{workflow.get('id')!r}"
        )
    stage = stages[stage_key]
    if workstream_id and workstreams:
        ws = workstreams.get(workstream_id)
        if ws is None:
            raise ValueError(
                f"workstream_id {workstream_id!r} is not defined in board workflow "
                f"{workflow.get('id')!r}"
            )
        allowed_stages = ws.get("stages") or []
        if allowed_stages and stage_key not in allowed_stages:
            raise ValueError(
                f"stage_key {stage_key!r} is not allowed for workstream "
                f"{workstream_id!r}; allowed: {', '.join(allowed_stages)}"
            )
    allowed = stage.get("allowed_lifecycle_states") or []
    if lifecycle_status and allowed and lifecycle_status not in allowed:
        raise ValueError(
            f"workflow stage {stage_key!r} does not allow lifecycle status "
            f"{lifecycle_status!r}"
        )
    if action_key:
        actions = {str(a.get("key")) for a in stage.get("actions") or [] if isinstance(a, dict)}
        if actions and action_key not in actions:
            raise ValueError(
                f"action_key {action_key!r} is not defined for workflow stage {stage_key!r}"
            )


def _evidence_keys(evidence: Optional[Any]) -> set[str]:
    """Normalize evidence provided by callers/metadata into comparable keys."""
    if evidence is None:
        return set()
    if isinstance(evidence, dict):
        return {str(k).strip() for k, v in evidence.items() if str(k).strip() and v}
    if isinstance(evidence, str):
        return {evidence.strip()} if evidence.strip() else set()
    if isinstance(evidence, (list, tuple, set)):
        return {str(item).strip() for item in evidence if str(item).strip()}
    return {str(evidence).strip()} if str(evidence).strip() else set()


def _funnel_transition_state(data: Optional[dict]) -> dict:
    if isinstance(data, dict):
        existing = data.get("transition_evidence")
        return {
            "evidence": set(data.get("transition_evidence", []) if isinstance(existing, list) else []),
            "data": dict(data),
        }
    return {"evidence": set(), "data": {}}


def _completion_evidence_keys(
    *,
    metadata: Optional[dict],
    result: Optional[str],
    summary: Optional[str],
    funnel_data: Optional[dict],
) -> set[str]:
    """Collect artifact/evidence keys present in completion payloads."""
    keys = _evidence_keys(metadata)
    keys |= _evidence_keys(funnel_data)
    for blob in (metadata, funnel_data):
        if not isinstance(blob, dict):
            continue
        for list_key in ("artifacts", "proof", "evidence", "transition_evidence"):
            keys |= _evidence_keys(blob.get(list_key))
    scan_text = " ".join(filter(None, [summary, result]))
    if scan_text:
        for match in re.findall(r"\b([a-z][a-z0-9_]{2,})\b", scan_text.lower()):
            keys.add(match)
    return keys


def _apply_completion_evidence_and_maybe_transition(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    metadata: Optional[dict],
    result: Optional[str],
    summary: Optional[str],
    board: Optional[str],
) -> None:
    """Merge completion facts into transition_evidence; auto-advance when unambiguous.

    Workers should put durable proof keys in ``metadata`` and/or ``funnel_data``.
    When every key for exactly one workflow exit is satisfied, the card stage
    advances via :func:`transition_task_stage` without a separate operator step.
    """
    task = get_task(conn, task_id)
    if task is None or not task.stage_key:
        return
    workflow = read_board_metadata(board).get("workflow")
    if not isinstance(workflow, dict):
        _warn_action_output_schema(
            conn, task, metadata=metadata, funnel_data=task.funnel_data, workflow=None,
        )
        return
    stages = _workflow_stage_map(workflow)
    stage = stages.get(task.stage_key)
    if not stage:
        _warn_action_output_schema(
            conn, task, metadata=metadata, funnel_data=task.funnel_data, workflow=workflow,
        )
        return

    provided = _completion_evidence_keys(
        metadata=metadata,
        result=result,
        summary=summary,
        funnel_data=task.funnel_data,
    )
    state = _funnel_transition_state(task.funnel_data)
    merged = {str(e) for e in state["evidence"]} | provided
    required_union: set[str] = set()
    for exit_row in stage.get("exit_criteria") or []:
        if isinstance(exit_row, dict):
            required_union |= {str(e) for e in exit_row.get("evidence_required") or []}
    newly_found = sorted(required_union & provided)
    if not newly_found and not (merged - state["evidence"]):
        _warn_action_output_schema(
            conn, task, metadata=metadata, funnel_data=task.funnel_data, workflow=workflow,
        )
        return

    next_data = state["data"]
    if merged:
        next_data["transition_evidence"] = sorted(merged)
    next_data["missing_evidence"] = sorted(required_union - merged) if required_union else []

    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET funnel_data = ? WHERE id = ?",
            (
                json.dumps(next_data, ensure_ascii=False) if next_data else None,
                task_id,
            ),
        )
        if newly_found:
            _append_event(
                conn,
                task_id,
                "funnel_evidence_merged",
                {
                    "stage_key": task.stage_key,
                    "keys": newly_found,
                    "transition_evidence": sorted(merged),
                },
            )

    ready_transitions: list[tuple[str, list[str]]] = []
    for exit_row in stage.get("exit_criteria") or []:
        if not isinstance(exit_row, dict):
            continue
        target = str(exit_row.get("transition") or "").strip()
        required = [str(e) for e in exit_row.get("evidence_required") or []]
        if target and required and all(item in merged for item in required):
            ready_transitions.append((target, required))

    _warn_action_output_schema(
        conn, task, metadata=metadata, funnel_data=next_data, workflow=workflow,
    )

    if len(ready_transitions) != 1:
        return
    to_stage, _required = ready_transitions[0]
    try:
        transition_task_stage(
            conn,
            task_id,
            to_stage=to_stage,
            evidence=sorted(merged),
            action_key=task.action_key,
            actor="completion_contract",
            board=board,
        )
    except ValueError:
        return


def _warn_action_output_schema(
    conn: sqlite3.Connection,
    task: Task,
    *,
    metadata: Optional[dict],
    funnel_data: Optional[dict],
    workflow: Optional[dict],
) -> None:
    """Emit a soft warning when completion metadata lacks required action artifacts."""
    if not isinstance(workflow, dict) or not task.stage_key or not task.action_key:
        return
    stage = _workflow_stage_map(workflow).get(task.stage_key)
    action = _workflow_action_row(stage, task.action_key)
    if not action:
        return
    required = action.get("output_schema") or []
    if not required:
        return
    present = _completion_evidence_keys(
        metadata=metadata,
        result=task.result,
        summary=None,
        funnel_data=funnel_data,
    )
    missing = [key for key in required if key not in present]
    if not missing:
        return
    with write_txn(conn):
        _append_event(
            conn,
            task.id,
            "completion_output_schema_warn",
            {
                "stage_key": task.stage_key,
                "action_key": task.action_key,
                "missing_keys": missing,
                "required_keys": list(required),
            },
        )


def _merge_unique_strings(*values: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        for item in _string_list(value):
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def _contract_object(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _connection_board(conn: sqlite3.Connection, board: Optional[str] = None) -> str:
    """Resolve the board attached to a DB connection; reject spoofed mismatches."""
    explicit = _normalize_board_slug(board)
    try:
        attached = object.__getattribute__(conn, "_board_slug")
    except Exception:
        attached = None
    attached_slug = _normalize_board_slug(attached) if attached else None
    if attached_slug:
        if explicit and explicit != attached_slug:
            raise ValueError(
                f"board argument {explicit!r} does not match connected board {attached_slug!r}"
            )
        return attached_slug

    try:
        env_slug = _normalize_board_slug(os.environ.get("HERMES_KANBAN_BOARD"))
    except ValueError:
        env_slug = None
    db_path_override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    db_path_slug = _board_slug_for_db_path(Path(db_path_override)) if db_path_override else None
    if db_path_slug:
        if env_slug and env_slug != db_path_slug:
            raise ValueError(
                f"pinned board {env_slug!r} does not match pinned DB board {db_path_slug!r}"
            )
        if explicit and explicit != db_path_slug:
            raise ValueError(
                f"board argument {explicit!r} does not match pinned DB board {db_path_slug!r}"
            )
        return db_path_slug
    if env_slug and board_exists(env_slug):
        if explicit and explicit != env_slug:
            raise ValueError(
                f"board argument {explicit!r} does not match pinned board {env_slug!r}"
            )
        return env_slug
    return explicit or get_current_board()


def _policy_has_material_route(policy: Any) -> bool:
    """Return True when a provider policy contains an actual execution route."""
    def material_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, dict):
            return _policy_has_material_route(value)
        if isinstance(value, (list, tuple, set)):
            return any(material_value(item) for item in value)
        if isinstance(value, bool):
            return value
        return bool(str(value).strip())

    row = _contract_object(policy)
    if not row:
        return False
    material_keys = {
        "provider",
        "providers",
        "model",
        "models",
        "tool",
        "tools",
        "tool_name",
        "endpoint",
        "url",
        "base_url",
        "command",
        "mcp_server",
        "route",
        "routes",
    }
    ignored_keys = {
        "allowed_for",
        "denied_for",
        "required_toolsets",
        "constraints",
        "notes",
        "description",
    }
    for raw_key, value in row.items():
        key = str(raw_key).strip()
        if key in ignored_keys:
            continue
        if key in material_keys:
            if material_value(value):
                return True
        elif isinstance(value, dict) and key in {"execution", "backend", "config"}:
            if _policy_has_material_route(value):
                return True
    return False


def _task_execution_contract(task: Task) -> dict:
    data = _contract_object(task.funnel_data)
    contract = data.get("execution_contract")
    merged = dict(contract) if isinstance(contract, dict) else {}
    for key in (
        "required_capabilities",
        "required_toolsets",
        "required_proof",
        "side_effect_class",
        "require_provider_policy",
    ):
        if key in data and key not in merged:
            merged[key] = data.get(key)
    return merged


def resolve_task_contract(task: Task, *, board: Optional[str] = None) -> dict[str, Any]:
    """Return the derived execution contract for a task on its board."""
    board_meta = read_board_metadata(board)
    workflow_raw = board_meta.get("workflow")
    runtime_raw = board_meta.get("runtime")
    workflow = dict(workflow_raw) if isinstance(workflow_raw, dict) else {}
    runtime = dict(runtime_raw) if isinstance(runtime_raw, dict) else {}
    stage = _workflow_stage_map(workflow).get(task.stage_key or "")
    action = _workflow_action_row(stage, task.action_key)
    workstream = _workflow_workstream_map(workflow).get(task.workstream_id or "")
    explicit = _task_execution_contract(task)
    envelope_map = _normalize_worker_envelopes(runtime.get("worker_envelopes"))
    assignee_key = task.assignee or ""
    if assignee_key in envelope_map:
        envelope = _contract_object(envelope_map.get(assignee_key))
    elif "default" in envelope_map:
        envelope = _contract_object(envelope_map.get("default"))
    else:
        envelope = {}
    provider_policy = _normalize_policy_map(runtime.get("provider_policy"), field="runtime.provider_policy")
    tool_policy = _normalize_policy_map(runtime.get("tool_policy"), field="runtime.tool_policy")
    required_capabilities = _merge_unique_strings(
        explicit.get("required_capabilities"),
        action.get("required_capabilities") if isinstance(action, dict) else None,
        stage.get("required_capabilities") if isinstance(stage, dict) else None,
        workstream.get("required_capabilities") if isinstance(workstream, dict) else None,
    )
    required_toolsets = _merge_unique_strings(
        explicit.get("required_toolsets"),
        action.get("required_toolsets") if isinstance(action, dict) else None,
        stage.get("required_toolsets") if isinstance(stage, dict) else None,
        workstream.get("required_toolsets") if isinstance(workstream, dict) else None,
    )
    required_proof = _merge_unique_strings(
        explicit.get("required_proof"),
        action.get("required_proof") if isinstance(action, dict) else None,
        stage.get("required_proof") if isinstance(stage, dict) else None,
        workstream.get("required_proof") if isinstance(workstream, dict) else None,
        envelope.get("required_proof") if isinstance(envelope, dict) else None,
    )
    side_effect_class = (
        (action.get("side_effect_class") if isinstance(action, dict) else None)
        or (stage.get("side_effect_class") if isinstance(stage, dict) else None)
        or (workstream.get("side_effect_class") if isinstance(workstream, dict) else None)
        or explicit.get("side_effect_class")
    )
    for cap in required_capabilities:
        required_toolsets = _merge_unique_strings(
            required_toolsets,
            _contract_object(tool_policy.get(cap)).get("required_toolsets"),
        )
    require_provider_policy = (
        _coerce_bool(explicit.get("require_provider_policy"))
        or _coerce_bool(runtime.get("require_provider_policy"))
        or _coerce_bool(workflow.get("require_provider_policy"))
    )
    require_worker_envelopes = _coerce_bool(runtime.get("require_worker_envelopes"))
    return {
        "board": board_meta.get("slug"),
        "objective": board_meta.get("objective"),
        "metadata_error": board_meta.get("metadata_error"),
        "workflow_id": workflow.get("id") if isinstance(workflow, dict) else None,
        "goal_id": task.goal_id or (workflow.get("goal_id") if isinstance(workflow, dict) else None),
        "workstream_id": task.workstream_id,
        "stage_key": task.stage_key,
        "action_key": task.action_key,
        "required_capabilities": required_capabilities,
        "required_toolsets": required_toolsets,
        "required_proof": required_proof,
        "side_effect_class": side_effect_class,
        "require_provider_policy": require_provider_policy,
        "provider_policy": provider_policy,
        "tool_policy": tool_policy,
        "worker_envelope": envelope,
        "worker_envelopes_declared": bool(envelope_map),
        "require_worker_envelopes": require_worker_envelopes,
    }


def evaluate_dispatch_eligibility(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Validate a ready/review task before the dispatcher claims/spawns it."""
    task = get_task(conn, task_id)
    if task is None:
        return {"ok": False, "task_id": task_id, "blockers": [{"code": "unknown_task"}]}
    board_slug = _connection_board(conn, board)
    contract = resolve_task_contract(task, board=board_slug)
    blockers: list[dict[str, Any]] = []
    if contract.get("metadata_error"):
        blockers.append({"code": "board_metadata_invalid", "error": contract.get("metadata_error")})
    envelope = _contract_object(contract.get("worker_envelope"))
    required_capabilities = set(_string_list(contract.get("required_capabilities")))
    capabilities = set(_string_list(envelope.get("capabilities")))
    required_toolsets = set(_string_list(contract.get("required_toolsets")))
    envelope_toolsets = set(_string_list(envelope.get("toolsets")))
    side_effect = contract.get("side_effect_class")
    required_proof = set(_string_list(contract.get("required_proof")))
    if (
        contract.get("require_worker_envelopes")
        and not envelope
        and (required_capabilities or required_toolsets or side_effect or required_proof)
    ):
        blockers.append({"code": "missing_worker_envelope", "assignee": task.assignee})
    if required_capabilities and envelope:
        missing = sorted(required_capabilities - capabilities)
        if missing:
            blockers.append({"code": "missing_capabilities", "missing": missing, "assignee": task.assignee})
    if required_toolsets and envelope:
        missing_toolsets = sorted(required_toolsets - envelope_toolsets)
        if missing_toolsets:
            blockers.append({"code": "missing_toolsets", "missing": missing_toolsets, "assignee": task.assignee})
    allowed_side_effects = set(_string_list(envelope.get("allowed_side_effects")))
    if side_effect and envelope and str(side_effect) not in allowed_side_effects:
        blockers.append({
            "code": "side_effect_not_allowed",
            "side_effect_class": side_effect,
            "allowed": sorted(allowed_side_effects),
        })
    provider_policy = _contract_object(contract.get("provider_policy"))
    if contract.get("require_provider_policy"):
        missing_policy = sorted(cap for cap in required_capabilities if cap not in provider_policy)
        if missing_policy:
            blockers.append({"code": "missing_provider_policy", "capabilities": missing_policy})
        empty_policy = sorted(
            cap for cap in required_capabilities
            if cap in provider_policy and not _policy_has_material_route(provider_policy.get(cap))
        )
        if empty_policy:
            blockers.append({"code": "empty_provider_policy", "capabilities": empty_policy})
    for cap in required_capabilities:
        policy = _contract_object(provider_policy.get(cap))
        allowed_for = _string_list(policy.get("allowed_for"))
        if allowed_for and task.assignee not in allowed_for:
            blockers.append({
                "code": "provider_policy_denied",
                "capability": cap,
                "assignee": task.assignee,
                "allowed_for": allowed_for,
            })
    return {"ok": not blockers, "task_id": task_id, "contract": contract, "blockers": blockers}


def _block_dispatch_ineligible(
    conn: sqlite3.Connection,
    task_id: str,
    verdict: dict[str, Any],
) -> bool:
    return _block_contract_ineligible(
        conn,
        task_id,
        verdict,
        source="dispatch",
        allowed_statuses=("ready", "review"),
    )


def _block_contract_ineligible(
    conn: sqlite3.Connection,
    task_id: str,
    verdict: dict[str, Any],
    *,
    source: str,
    allowed_statuses: tuple[str, ...],
) -> bool:
    blockers = verdict.get("blockers") or []
    reason = f"{source} eligibility failed: " + ", ".join(
        str(blocker.get("code") or "blocked") for blocker in blockers if isinstance(blocker, dict)
    )
    placeholders = ",".join("?" for _ in allowed_statuses)
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'blocked', last_failure_error = ? "
            f"WHERE id = ? AND status IN ({placeholders}) AND claim_lock IS NULL",
            (reason[:1000], task_id, *allowed_statuses),
        )
        if cur.rowcount != 1:
            return False
        _append_event(
            conn,
            task_id,
            f"{source}_contract_blocked",
            {"verdict": verdict, "reason": reason[:1000]},
        )
    return True


def _structured_completion_evidence_keys(*, metadata: Optional[dict], funnel_data: Optional[dict]) -> set[str]:
    """Evidence keys for hard done gates; intentionally ignores prose."""
    keys: set[str] = set()
    for blob in (metadata, funnel_data):
        if not isinstance(blob, dict):
            continue
        keys |= _evidence_keys(blob)
        for list_key in ("artifacts", "proof", "evidence", "transition_evidence"):
            keys |= _evidence_keys(blob.get(list_key))
        contract = blob.get("execution_contract")
        if isinstance(contract, dict):
            keys |= _evidence_keys(contract.get("proof"))
            keys |= _evidence_keys(contract.get("evidence"))
    return keys


def validate_contract_done(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    metadata: Optional[dict] = None,
    board: Optional[str] = None,
) -> dict[str, Any]:
    task = get_task(conn, task_id)
    if task is None:
        return {"ok": False, "task_id": task_id, "blockers": [{"code": "unknown_task"}]}
    board_slug = _connection_board(conn, board)
    contract = resolve_task_contract(task, board=board_slug)
    required = set(_string_list(contract.get("required_proof")))
    provided = _structured_completion_evidence_keys(metadata=metadata, funnel_data=None)
    missing = sorted(required - provided)
    blockers: list[dict[str, Any]] = []
    runtime_contract_active = bool(
        contract.get("metadata_error")
        or
        required
        or _string_list(contract.get("required_capabilities"))
        or _string_list(contract.get("required_toolsets"))
        or contract.get("side_effect_class")
        or contract.get("require_worker_envelopes")
        or contract.get("require_provider_policy")
    )
    if runtime_contract_active and metadata is not None and not isinstance(metadata, dict):
        blockers.append({
            "code": "invalid_completion_metadata",
            "metadata_type": type(metadata).__name__,
            "message": "contracted task completion metadata must be a structured object/dict",
        })
    if runtime_contract_active and task.status != "running":
        blockers.append({
            "code": "missing_eligible_run",
            "status": task.status,
            "message": "contracted tasks must be running under an eligible claim before completion",
        })
    if runtime_contract_active and task.status == "running":
        now = int(time.time())
        run_row = None
        if task.current_run_id is not None:
            run_row = conn.execute(
                "SELECT id, status, ended_at, claim_lock, claim_expires FROM task_runs WHERE id = ?",
                (int(task.current_run_id),),
            ).fetchone()
        if not task.claim_lock or task.current_run_id is None or run_row is None:
            blockers.append({
                "code": "missing_eligible_run",
                "status": task.status,
                "message": "contracted tasks must have a current running claim/run before completion",
            })
        elif run_row["status"] != "running" or run_row["ended_at"] is not None:
            blockers.append({
                "code": "missing_eligible_run",
                "status": task.status,
                "run_status": run_row["status"],
                "message": "current run is not open/running",
            })
        elif task.claim_expires is None or int(task.claim_expires) <= now:
            blockers.append({
                "code": "stale_claim",
                "claim_expires": task.claim_expires,
                "message": "contracted task claim is missing or expired",
            })
    if runtime_contract_active:
        eligibility = evaluate_dispatch_eligibility(conn, task_id, board=board)
        if not eligibility.get("ok"):
            blockers.extend(eligibility.get("blockers") or [])
    if missing:
        blockers.append({
            "code": "missing_required_proof",
            "missing": missing,
            "provided": sorted(provided),
        })
    return {
        "ok": not blockers,
        "task_id": task_id,
        "required_proof": sorted(required),
        "provided_proof": sorted(provided),
        "contract": contract,
        "blockers": blockers,
    }


class ContractDoneGateError(RuntimeError):
    """Raised when the generic board operating contract rejects completion."""

    def __init__(self, verdict: dict[str, Any]):
        self.verdict = verdict
        codes = ", ".join(str(b.get("code")) for b in verdict.get("blockers", []))
        super().__init__(f"contract done gate failed for {verdict.get('task_id')}: {codes}")


def repair_orphan_task_runs(conn: sqlite3.Connection) -> list[dict]:
    """Close open task_runs whose parent task is no longer ``running``.

    Returns the rows that were reclaimed (empty when nothing to repair).
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT r.id, r.task_id, COALESCE(t.status, '<missing>') AS task_status,
               COALESCE(t.title, '<missing>') AS title, r.worker_pid,
               r.started_at, r.step_key
          FROM task_runs r
          LEFT JOIN tasks t ON t.id = r.task_id
         WHERE r.status = 'running'
           AND r.ended_at IS NULL
           AND COALESCE(t.status, '') <> 'running'
         ORDER BY r.id
        """
    ).fetchall()
    if not rows:
        return []
    now = int(time.time())
    ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in ids)
    with write_txn(conn):
        conn.execute(
            f"""
            UPDATE task_runs
               SET status = 'reclaimed',
                   outcome = 'reclaimed',
                   ended_at = ?,
                   summary = COALESCE(summary, 'kanban_db repair: closed orphan running task_run'),
                   error = COALESCE(error, 'open run had no matching running task'),
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL
             WHERE id IN ({placeholders})
               AND status = 'running'
               AND ended_at IS NULL
            """,
            (now, *ids),
        )
    return [dict(row) for row in rows]


def _load_profile_kanban_flags(profile_config_path: Path) -> dict[str, Any]:
    try:
        text = profile_config_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    kanban: dict[str, Any] = {}
    board: str | None = None
    in_kanban = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw.startswith(" ") and stripped.endswith(":"):
            section = stripped[:-1]
            in_kanban = section == "kanban"
            continue
        if in_kanban and line.startswith("  ") and ":" in stripped:
            key, value = [part.strip() for part in stripped.split(":", 1)]
            if value.lower() in {"true", "false"}:
                kanban[key] = value.lower() == "true"
            else:
                kanban[key] = value.strip("'\"")
        if stripped.startswith("HERMES_KANBAN_BOARD="):
            board = stripped.split("=", 1)[1].strip() or None
    env_path = profile_config_path.parent / ".env"
    try:
        for env_line in env_path.read_text(encoding="utf-8").splitlines():
            if env_line.startswith("HERMES_KANBAN_BOARD="):
                board = env_line.split("=", 1)[1].strip() or board
    except OSError:
        pass
    return {"board": board, "kanban": kanban}


def audit_board_dispatcher_ownership(
    board: str,
    *,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    """Return dispatcher ownership audit for a board (profiles + board.json owner)."""
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    meta = read_board_metadata(slug)
    runtime = meta.get("runtime") if isinstance(meta.get("runtime"), dict) else {}
    dispatcher = runtime.get("dispatcher") if isinstance(runtime.get("dispatcher"), dict) else {}
    expected = str(dispatcher.get("profile") or "").strip() or None

    root = home or kanban_home()
    profiles_root = root / "profiles"
    dispatchers: list[dict[str, Any]] = []
    unscoped: list[str] = []
    if profiles_root.exists():
        for config_path in sorted(profiles_root.glob("*/config.yaml")):
            flags = _load_profile_kanban_flags(config_path)
            kanban = flags.get("kanban") if isinstance(flags.get("kanban"), dict) else {}
            if not kanban.get("dispatch_in_gateway"):
                continue
            name = config_path.parent.name
            profile_board = flags.get("board")
            entry = {"profile": name, "board": profile_board}
            if profile_board == slug:
                dispatchers.append(entry)
            elif profile_board is None:
                unscoped.append(name)
    live_profiles = [d["profile"] for d in dispatchers]
    status = "ok"
    summary = "single dispatcher profile claims this board"
    if len(dispatchers) > 1:
        status = "critical"
        summary = "multiple profiles claim dispatch for this board"
    elif len(dispatchers) == 0 and unscoped:
        status = "warning"
        summary = "no board-pinned dispatcher; unscoped dispatch-capable profiles exist"
    elif len(dispatchers) == 0:
        status = "warning"
        summary = "no profile with dispatch_in_gateway pinned to this board"
    if expected and live_profiles and expected not in live_profiles:
        status = "warning" if status == "ok" else status
        summary = (
            f"board runtime.dispatcher.profile expects {expected!r} but active "
            f"dispatchers are {live_profiles!r}"
        )
    return {
        "board": slug,
        "status": status,
        "summary": summary,
        "expected_dispatcher_profile": expected,
        "dispatchers": dispatchers,
        "unscoped_dispatch_profiles": unscoped,
    }


def kanban_semantic_diagnostics(
    conn: sqlite3.Connection,
    *,
    board: Optional[str] = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Lint funnel semantics: invalid stages, lifecycle drift, and unclassified cards."""
    board_slug = _connection_board(conn, board)
    workflow = read_board_metadata(board_slug).get("workflow")
    stages = _workflow_stage_map(workflow if isinstance(workflow, dict) else None)
    workstreams = _workflow_workstream_map(workflow if isinstance(workflow, dict) else None)
    require_semantics = bool(
        isinstance(workflow, dict) and workflow.get("require_semantics")
    )
    default_goal = (
        _normalize_funnel_text(workflow.get("goal_id"))
        if isinstance(workflow, dict) else None
    )

    issues: list[dict[str, Any]] = []
    tasks = list_tasks(conn, include_archived=include_archived)
    for task in tasks:
        node = _funnel_task_node(task)
        if node["semantic_source"] == "fallback":
            issues.append({
                "task_id": task.id,
                "code": "unclassified",
                "severity": "warning",
                "message": "card lacks explicit funnel coordinates",
            })
        if require_semantics:
            for field in ("goal_id", "workstream_id", "stage_key", "action_key"):
                if not getattr(task, field, None):
                    issues.append({
                        "task_id": task.id,
                        "code": "missing_semantics",
                        "severity": "error",
                        "message": f"missing required funnel field {field}",
                    })
        if task.goal_id and default_goal and task.goal_id != default_goal:
            issues.append({
                "task_id": task.id,
                "code": "goal_drift",
                "severity": "warning",
                "message": f"goal_id {task.goal_id!r} differs from workflow default {default_goal!r}",
            })
        if task.stage_key and stages and task.stage_key not in stages:
            issues.append({
                "task_id": task.id,
                "code": "unknown_stage",
                "severity": "error",
                "message": f"stage_key {task.stage_key!r} is not in workflow",
            })
        elif task.stage_key and task.workstream_id and workstreams:
            ws = workstreams.get(task.workstream_id)
            allowed = (ws or {}).get("stages") or []
            if ws is None:
                issues.append({
                    "task_id": task.id,
                    "code": "unknown_workstream",
                    "severity": "error",
                    "message": f"workstream_id {task.workstream_id!r} is not in workflow",
                })
            elif allowed and task.stage_key not in allowed:
                issues.append({
                    "task_id": task.id,
                    "code": "workstream_stage_mismatch",
                    "severity": "error",
                    "message": (
                        f"stage_key {task.stage_key!r} is not allowed for "
                        f"workstream {task.workstream_id!r}"
                    ),
                })
            stage = stages.get(task.stage_key)
            if stage:
                allowed_lifecycle = stage.get("allowed_lifecycle_states") or []
                if (
                    allowed_lifecycle
                    and task.status in VALID_STATUSES
                    and task.status not in allowed_lifecycle
                ):
                    issues.append({
                        "task_id": task.id,
                        "code": "lifecycle_stage_mismatch",
                        "severity": "warning",
                        "message": (
                            f"status {task.status!r} is not allowed in stage "
                            f"{task.stage_key!r}"
                        ),
                    })
                if task.action_key:
                    action_keys = {
                        str(a.get("key"))
                        for a in stage.get("actions") or []
                        if isinstance(a, dict) and a.get("key")
                    }
                    if action_keys and task.action_key not in action_keys:
                        issues.append({
                            "task_id": task.id,
                            "code": "unknown_action",
                            "severity": "error",
                            "message": (
                                f"action_key {task.action_key!r} is not defined for "
                                f"stage {task.stage_key!r}"
                            ),
                        })

    by_code: dict[str, int] = {}
    for issue in issues:
        code = str(issue.get("code") or "unknown")
        by_code[code] = int(by_code.get(code, 0)) + 1
    return {
        "board": board_slug,
        "generated_at": int(time.time()),
        "issue_count": len(issues),
        "issues": issues,
        "summary": {"by_code": {k: by_code[k] for k in sorted(by_code)}},
    }


def create_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    created_by: Optional[str] = None,
    workspace_kind: str = "scratch",
    workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None,
    tenant: Optional[str] = None,
    priority: int = 0,
    parents: Iterable[str] = (),
    triage: bool = False,
    idempotency_key: Optional[str] = None,
    max_runtime_seconds: Optional[int] = None,
    skills: Optional[Iterable[str]] = None,
    max_retries: Optional[int] = None,
    initial_status: Optional[str] = None,
    session_id: Optional[str] = None,
    goal_id: Optional[str] = None,
    workstream_id: Optional[str] = None,
    stage_key: Optional[str] = None,
    action_key: Optional[str] = None,
    funnel_data: Optional[dict] = None,
    board: Optional[str] = None,
) -> str:
    """Create a new task and optionally link it under parent tasks.

    Returns the new task id.  Status is ``ready`` when there are no
    parents (or all parents already ``done``), otherwise ``todo``.
    If ``triage=True``, status is forced to ``triage`` regardless of
    parents — a specifier/triager is expected to promote the task to
    ``todo`` once the spec is fleshed out.

    If ``idempotency_key`` is provided and a non-archived task with the
    same key already exists, returns the existing task's id instead of
    creating a duplicate. Useful for retried webhooks / automation that
    should not double-write.

    ``max_runtime_seconds`` caps how long a worker may run before the
    dispatcher SIGTERMs (then SIGKILLs after a grace window) and
    re-queues the task. ``None`` means no cap (default).

    ``skills`` is an optional list of skill names to force-load into
    the worker when dispatched. Stored as JSON; the dispatcher passes
    each name to ``hermes --skills ...`` alongside the built-in
    ``kanban-worker``. Use this to pin a task to a specialist skill
    (e.g. ``skills=["translation"]`` so the worker loads the
    translation skill regardless of the profile's default config).
    """
    assignee = _canonical_assignee(assignee)
    if not title or not title.strip():
        raise ValueError("title is required")
    initial_status_explicit = initial_status is not None
    if initial_status is None:
        initial_status = "running"
    if initial_status not in VALID_INITIAL_STATUSES:
        raise ValueError(
            f"initial_status must be one of {sorted(VALID_INITIAL_STATUSES)}"
        )
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(
            f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, "
            f"got {workspace_kind!r}"
        )
    if branch_name is not None:
        branch_name = str(branch_name).strip() or None
    if branch_name and workspace_kind != "worktree":
        raise ValueError("branch_name is only valid for worktree workspaces")
    parents = tuple(p for p in parents if p)
    board_slug = _connection_board(conn, board)
    board_meta = read_board_metadata(board_slug)
    workflow = board_meta.get("workflow") if isinstance(board_meta.get("workflow"), dict) else None
    goal_id = _normalize_funnel_text(goal_id)
    if not goal_id and isinstance(workflow, dict) and workflow.get("require_semantics"):
        goal_id = _normalize_funnel_text(workflow.get("goal_id"))
    workstream_id = _normalize_funnel_text(workstream_id)
    stage_key = _normalize_funnel_text(stage_key)
    action_key = _normalize_funnel_text(action_key)
    funnel_data = _normalize_funnel_data(funnel_data)

    # Normalise + validate skills: strip whitespace, drop empties, dedupe
    # (preserving order). Refuse commas inside a single name so we don't
    # invisibly splatter a comma-joined string into one argv slot — the
    # `hermes --skills X,Y` comma syntax is handled in the dispatcher,
    # not here.
    skills_list: Optional[list[str]] = None
    if skills is not None:
        cleaned: list[str] = []
        seen: set[str] = set()
        # Collect all toolset-name confusions up front so the user sees the
        # whole list at once. Raising on the first hit is friendly when the
        # input has one mistake, but agents that confuse skills with toolsets
        # usually pass several at once (`skills=["web", "browser", "terminal"]`)
        # and serial-correcting one per failure round-trips wastes tokens.
        toolset_typos: list[str] = []
        for s in skills:
            if not s:
                continue
            name = str(s).strip()
            if not name:
                continue
            if "," in name:
                raise ValueError(
                    f"skill name cannot contain comma: {name!r} "
                    f"(pass a list of separate names instead of a comma-joined string)"
                )
            if name.casefold() in KNOWN_TOOLSET_NAMES:
                toolset_typos.append(name)
                continue
            if name in seen:
                continue
            seen.add(name)
            cleaned.append(name)
        if toolset_typos:
            quoted = ", ".join(repr(n) for n in toolset_typos)
            noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
            raise ValueError(
                f"{quoted} {noun}, not skill name(s). "
                "Put toolsets in the assignee profile's `toolsets:` config "
                "instead of per-task skills. Skills are named skill bundles "
                "(e.g. `kanban-worker`, `blogwatcher`); toolsets are runtime "
                "capabilities (e.g. `web`, `browser`, `terminal`)."
            )
        skills_list = cleaned

    # Idempotency check — return the existing task instead of creating a
    # duplicate. Done BEFORE entering write_txn to keep the fast path fast
    # and to avoid holding a write lock during the lookup. Race is
    # acceptable: two concurrent creators with the same key might both
    # insert, at which point both rows exist but the next lookup stabilises.
    if idempotency_key:
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? "
            "AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if row:
            return row["id"]

    now = int(time.time())

    # Resolve workspace_path from board-level default_workdir when the
    # caller did not specify one explicitly. Board defaults represent
    # persistent project checkouts, so only persistent workspace kinds may
    # inherit them. Scratch workspaces are auto-deleted on completion and
    # must stay under the per-board scratch root created by
    # ``resolve_workspace``; inheriting ``default_workdir`` for a scratch
    # task would point cleanup at the user's source tree (#28818). The
    # containment guard in ``_cleanup_workspace`` is the safety rail, but
    # we also stop the bad state from being created in the first place.
    #
    # Auto-promote: if the board has a default_workdir and the caller did
    # not explicitly request a workspace kind, upgrade from scratch to dir.
    # A board with default_workdir is declaring "tasks here need persistent
    # workspaces" — requiring every caller to pass --workspace dir is
    # error-prone and leads to data loss when stages share output files.
    if workspace_path is None:
        board_default = board_meta.get("default_workdir")
        if board_default and workspace_kind == "scratch":
            workspace_kind = "dir"
            workspace_path = str(board_default)
        elif board_default and workspace_kind in {"dir", "worktree"}:
            workspace_path = str(board_default)

    # Retry once on the extremely unlikely id collision.
    for attempt in range(2):
        task_id = _new_task_id()
        try:
            with write_txn(conn):
                # Determine task status from parent status, unless the caller
                # parks it directly in blocked for human-ops review or in
                # triage for a specifier.
                if initial_status == "blocked":
                    task_status = "blocked"
                    if parents:
                        missing = _find_missing_parents(conn, parents)
                        if missing:
                            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
                elif triage:
                    task_status = "triage"
                else:
                    task_status = "ready"
                    if parents:
                        missing = _find_missing_parents(conn, parents)
                        if missing:
                            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
                        # If any parent is not yet done, we're todo.
                        rows = conn.execute(
                            "SELECT status FROM tasks WHERE id IN "
                            "(" + ",".join("?" * len(parents)) + ")",
                            parents,
                        ).fetchall()
                        if any(r["status"] != "done" for r in rows):
                            task_status = "todo"
                    elif (
                        not initial_status_explicit
                        and (board_meta.get("runtime") or {}).get("mode") == "kernel"
                    ):
                        task_status = "todo"
                # Even in triage mode we still need to validate parent ids
                # so the eventual link rows don't dangle.
                if triage and parents:
                    missing = _find_missing_parents(conn, parents)
                    if missing:
                        raise ValueError(f"unknown parent task(s): {', '.join(missing)}")

                _validate_task_against_workflow(
                    board_slug,
                    goal_id=goal_id,
                    workstream_id=workstream_id,
                    stage_key=stage_key,
                    action_key=action_key,
                    lifecycle_status=task_status,
                )

                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, body, assignee, status, priority,
                        created_by, created_at, workspace_kind, workspace_path,
                        branch_name, tenant, idempotency_key, max_runtime_seconds,
                        skills, max_retries, session_id,
                        goal_id, workstream_id, stage_key, action_key, funnel_data
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        title.strip(),
                        body,
                        assignee,
                        task_status,
                        priority,
                        created_by,
                        now,
                        workspace_kind,
                        workspace_path,
                        branch_name,
                        tenant,
                        idempotency_key,
                        int(max_runtime_seconds) if max_runtime_seconds is not None else None,
                        json.dumps(skills_list) if skills_list is not None else None,
                        int(max_retries) if max_retries is not None else None,
                        session_id,
                        goal_id,
                        workstream_id,
                        stage_key,
                        action_key,
                        json.dumps(funnel_data, ensure_ascii=False) if funnel_data else None,
                    ),
                )
                for pid in parents:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                        (pid, task_id),
                    )
                _append_event(
                    conn,
                    task_id,
                    "created",
                    {
                        "assignee": assignee,
                        "status": task_status,
                        "parents": list(parents),
                        "tenant": tenant,
                        "branch_name": branch_name,
                        "skills": list(skills_list) if skills_list else None,
                        "goal_id": goal_id,
                        "workstream_id": workstream_id,
                        "stage_key": stage_key,
                        "action_key": action_key,
                        "funnel_data": funnel_data,
                    },
                )
            return task_id
        except sqlite3.IntegrityError:
            if attempt == 1:
                raise
            # Retry with a fresh id.
            continue
    raise RuntimeError("unreachable")


def _find_missing_parents(conn: sqlite3.Connection, parents: Iterable[str]) -> list[str]:
    parents = list(parents)
    if not parents:
        return []
    placeholders = ",".join("?" * len(parents))
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})",
        parents,
    ).fetchall()
    present = {r["id"] for r in rows}
    return [p for p in parents if p not in present]


def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[Task]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Task.from_row(row) if row else None


def update_task_funnel_fields(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    goal_id: Any = _UNSET,
    workstream_id: Any = _UNSET,
    stage_key: Any = _UNSET,
    action_key: Any = _UNSET,
    funnel_data: Any = _UNSET,
    merge_funnel_data: bool = False,
    board: Optional[str] = None,
) -> Task:
    """Update semantic funnel coordinates on an existing task.

    This is the durable/operator path for backfills and manual corrections.
    Omitted fields are left unchanged; fields explicitly passed as ``None`` are
    cleared. ``funnel_data`` must be a JSON object when provided.
    """
    current = get_task(conn, task_id)
    if current is None:
        raise ValueError(f"unknown task: {task_id}")

    next_goal = current.goal_id if goal_id is _UNSET else _normalize_funnel_text(goal_id)
    next_workstream = (
        current.workstream_id
        if workstream_id is _UNSET else _normalize_funnel_text(workstream_id)
    )
    next_stage = current.stage_key if stage_key is _UNSET else _normalize_funnel_text(stage_key)
    next_action = current.action_key if action_key is _UNSET else _normalize_funnel_text(action_key)
    if funnel_data is _UNSET:
        next_funnel_data = current.funnel_data
    else:
        normalized = _normalize_funnel_data(funnel_data)
        if merge_funnel_data and isinstance(current.funnel_data, dict) and normalized:
            next_funnel_data = {**current.funnel_data, **normalized}
        else:
            next_funnel_data = normalized

    board_slug = _connection_board(conn, board)
    _validate_task_against_workflow(
        board_slug,
        goal_id=next_goal,
        workstream_id=next_workstream,
        stage_key=next_stage,
        action_key=next_action,
        lifecycle_status=current.status,
    )

    with write_txn(conn):
        row = conn.execute(
            "SELECT id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown task: {task_id}")
        conn.execute(
            """
            UPDATE tasks
               SET goal_id = ?,
                   workstream_id = ?,
                   stage_key = ?,
                   action_key = ?,
                   funnel_data = ?
             WHERE id = ?
            """,
            (
                next_goal,
                next_workstream,
                next_stage,
                next_action,
                json.dumps(next_funnel_data, ensure_ascii=False) if next_funnel_data else None,
                task_id,
            ),
        )
        _append_event(
            conn,
            task_id,
            "funnel_updated",
            {
                "goal_id": next_goal,
                "workstream_id": next_workstream,
                "stage_key": next_stage,
                "action_key": next_action,
                "funnel_data": next_funnel_data,
                "merge_funnel_data": bool(merge_funnel_data),
            },
        )
    updated = get_task(conn, task_id)
    if updated is None:  # defensive: row existed inside the transaction.
        raise ValueError(f"unknown task: {task_id}")
    return updated


# Canonical sort-order mappings for ``hermes kanban list --sort``.
# Each value is a raw SQL fragment appended after ``ORDER BY``.
VALID_SORT_ORDERS: dict[str, str] = {
    "created": "created_at ASC, id ASC",
    "created-desc": "created_at DESC, id DESC",
    "priority": "priority DESC, created_at ASC",
    "priority-desc": "priority ASC, created_at ASC",
    "status": "status ASC, created_at ASC",
    "assignee": "assignee ASC, created_at ASC",
    "title": "title ASC, id ASC",
    "updated": "started_at DESC NULLS LAST, created_at DESC",
}


def list_tasks(
    conn: sqlite3.Connection,
    *,
    assignee: Optional[str] = None,
    status: Optional[str] = None,
    tenant: Optional[str] = None,
    session_id: Optional[str] = None,
    include_archived: bool = False,
    limit: Optional[int] = None,
    order_by: Optional[str] = None,
    workflow_template_id: Optional[str] = None,
    current_step_key: Optional[str] = None,
) -> list[Task]:
    query = "SELECT * FROM tasks WHERE 1=1"
    params: list[Any] = []
    if assignee is not None:
        query += " AND assignee = ?"
        params.append(_canonical_assignee(assignee))
    if status is not None:
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
        query += " AND status = ?"
        params.append(status)
    if tenant is not None:
        query += " AND tenant = ?"
        params.append(tenant)
    if session_id is not None:
        query += " AND session_id = ?"
        params.append(session_id)
    if workflow_template_id is not None:
        query += " AND workflow_template_id = ?"
        params.append(workflow_template_id)
    if current_step_key is not None:
        query += " AND current_step_key = ?"
        params.append(current_step_key)
    if not include_archived and status != "archived":
        query += " AND status != 'archived'"
    if order_by is not None:
        order_by = order_by.strip().lower()
        if order_by not in VALID_SORT_ORDERS:
            raise ValueError(
                f"order_by must be one of {sorted(VALID_SORT_ORDERS.keys())}"
            )
        query += f" ORDER BY {VALID_SORT_ORDERS[order_by]}"
    else:
        query += " ORDER BY priority DESC, created_at ASC"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query, params).fetchall()
    return [Task.from_row(r) for r in rows]


def assign_task(conn: sqlite3.Connection, task_id: str, profile: Optional[str]) -> bool:
    """Assign or reassign a task.  Returns True on success.

    Refuses to reassign a task that's currently running (claim_lock set).
    Reassign after the current run completes if needed.
    """
    profile = _canonical_assignee(profile)
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        if row["claim_lock"] is not None and row["status"] == "running":
            raise RuntimeError(
                f"cannot reassign {task_id}: currently running (claimed). "
                "Wait for completion or reclaim the stale lock first."
            )
        if row["assignee"] != profile:
            # The retry guard is scoped to the task/profile combination. A
            # human reassigning the task is an explicit recovery action, so the
            # new profile should not inherit the previous profile's streak.
            conn.execute(
                "UPDATE tasks SET assignee = ?, consecutive_failures = 0, "
                "last_failure_error = NULL WHERE id = ?",
                (profile, task_id),
            )
        else:
            conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (profile, task_id))
        _append_event(conn, task_id, "assigned", {"assignee": profile})
        return True


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def link_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    if parent_id == child_id:
        raise ValueError("a task cannot depend on itself")
    with write_txn(conn):
        missing = _find_missing_parents(conn, [parent_id, child_id])
        if missing:
            raise ValueError(f"unknown task(s): {', '.join(missing)}")
        if _would_cycle(conn, parent_id, child_id):
            raise ValueError(
                f"linking {parent_id} -> {child_id} would create a cycle"
            )
        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (parent_id, child_id),
        )
        # If child was ready but parent is not yet done, demote child to todo.
        parent_status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (parent_id,)
        ).fetchone()["status"]
        if parent_status != "done":
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                (child_id,),
            )
        _append_event(
            conn, child_id, "linked",
            {"parent": parent_id, "child": child_id},
        )


def _would_cycle(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    """Return True if adding parent->child creates a cycle.

    A cycle exists iff ``parent_id`` is already a descendant of
    ``child_id`` via existing parent->child links.  We walk downward
    from ``child_id`` and check whether we reach ``parent_id``.
    """
    seen = set()
    stack = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
        ).fetchall()
        stack.extend(r["child_id"] for r in rows)
    return False


def unlink_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
            (parent_id, child_id),
        )
        if cur.rowcount:
            _append_event(
                conn, child_id, "unlinked",
                {"parent": parent_id, "child": child_id},
            )
        removed = cur.rowcount > 0
    if removed:
        # Dependency edge removed — re-evaluate promotion eligibility for the
        # child immediately.  Matches the contract of complete_task and
        # unblock_task; without this the child stays stuck in todo until the
        # next dispatcher tick or a manual `hermes kanban recompute` (issue #22459).
        recompute_ready(conn)
    return removed


def parent_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    return [r["parent_id"] for r in rows]


def child_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
        (task_id,),
    ).fetchall()
    return [r["child_id"] for r in rows]


def parent_results(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, Optional[str]]]:
    """Return ``(parent_id, result)`` for every done parent of ``task_id``."""
    rows = conn.execute(
        """
        SELECT t.id AS id, t.result AS result
        FROM tasks t
        JOIN task_links l ON l.parent_id = t.id
        WHERE l.child_id = ? AND t.status = 'done'
        ORDER BY t.completed_at ASC
        """,
        (task_id,),
    ).fetchall()
    return [(r["id"], r["result"]) for r in rows]


# ---------------------------------------------------------------------------
# Comments & events
# ---------------------------------------------------------------------------

def add_comment(
    conn: sqlite3.Connection, task_id: str, author: str, body: str
) -> int:
    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    now = int(time.time())
    with write_txn(conn):
        if not conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone():
            raise ValueError(f"unknown task {task_id}")
        cur = conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, author.strip(), body.strip(), now),
        )
        _append_event(conn, task_id, "commented", {"author": author, "len": len(body)})
        return int(cur.lastrowid or 0)


def list_comments(conn: sqlite3.Connection, task_id: str) -> list[Comment]:
    rows = conn.execute(
        "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()
    return [
        Comment(
            id=r["id"],
            task_id=r["task_id"],
            author=r["author"],
            body=r["body"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def list_events(conn: sqlite3.Connection, task_id: str) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at ASC, id ASC",
        (task_id,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except Exception:
            payload = None
        out.append(
            Event(
                id=r["id"],
                task_id=r["task_id"],
                kind=r["kind"],
                payload=payload,
                created_at=r["created_at"],
                run_id=(int(r["run_id"]) if "run_id" in r.keys() and r["run_id"] is not None else None),
            )
        )
    return out


PIXEL_EVENT_STATUSES = {"pass", "fail", "info", "blocked"}


def _pixel_config(board: Optional[str] = None) -> dict:
    meta = read_board_metadata(board)
    pixel = meta.get("pixel")
    return dict(pixel) if isinstance(pixel, dict) else {}


def is_pixel_enabled(board: Optional[str] = None) -> bool:
    pixel = _pixel_config(board)
    return bool(str(pixel.get("goal") or "").strip() and _string_list(pixel.get("success")))


def set_pixel_goal(board: Optional[str], goal: str, success: Iterable[str]) -> dict:
    """Enable Pixel on a board by writing the native goal/success contract."""
    goal_text = str(goal or "").strip()
    success_list = _string_list(success)
    if not goal_text:
        raise ValueError("pixel goal text is required")
    if not success_list:
        raise ValueError("at least one --success criterion is required")
    slug = _normalize_board_slug(board) or get_current_board()
    meta = read_board_metadata(slug)
    pixel = dict(meta.get("pixel") or {})
    pixel["goal"] = goal_text
    pixel["success"] = success_list
    meta.pop("db_path", None)
    meta["pixel"] = pixel
    if not meta.get("created_at"):
        meta["created_at"] = int(time.time())
    path = board_metadata_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    meta["db_path"] = str(kanban_db_path(slug))
    return meta


def set_pixel_stage_event(board: Optional[str], stage_key: str, conversion_event: str) -> dict:
    """Configure the typed conversion event required to exit a Pixel stage."""
    stage = _normalize_funnel_text(stage_key)
    event = _normalize_funnel_text(conversion_event)
    if not stage:
        raise ValueError("stage_key is required")
    if not event:
        raise ValueError("conversion_event is required")
    slug = _normalize_board_slug(board) or get_current_board()
    meta = read_board_metadata(slug)
    pixel = dict(meta.get("pixel") or {})
    stages = dict(pixel.get("stages") or {})
    stages[stage] = event
    pixel["stages"] = stages
    meta.pop("db_path", None)
    meta["pixel"] = pixel
    if not meta.get("created_at"):
        meta["created_at"] = int(time.time())
    path = board_metadata_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    meta["db_path"] = str(kanban_db_path(slug))
    return meta


def record_pixel_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    stage_key: str,
    status: str,
    evidence: str,
    task_id: Optional[str] = None,
) -> PixelEvent:
    event = _normalize_funnel_text(event_type)
    stage = _normalize_funnel_text(stage_key)
    normalized_status = str(status or "").strip().lower()
    evidence_text = str(evidence or "").strip()
    if not event:
        raise ValueError("pixel event type is required")
    if not stage:
        raise ValueError("pixel event stage_key is required")
    if normalized_status not in PIXEL_EVENT_STATUSES:
        raise ValueError("pixel event status must be one of: blocked, fail, info, pass")
    if not evidence_text:
        raise ValueError("pixel event evidence is required")
    if task_id and get_task(conn, task_id) is None:
        raise ValueError(f"unknown task: {task_id}")
    now = int(time.time())
    with write_txn(conn):
        cur = conn.execute(
            """
            INSERT INTO kanban_pixel_events
                (event_type, stage_key, task_id, status, evidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event, stage, task_id, normalized_status, evidence_text, now),
        )
        event_id = int(cur.lastrowid or 0)
        if task_id:
            _append_event(
                conn,
                task_id,
                "pixel_event",
                {
                    "pixel_event_id": event_id,
                    "event_type": event,
                    "stage_key": stage,
                    "status": normalized_status,
                    "evidence": evidence_text,
                },
            )
    row = conn.execute("SELECT * FROM kanban_pixel_events WHERE id = ?", (event_id,)).fetchone()
    return PixelEvent.from_row(row)


def list_pixel_events(
    conn: sqlite3.Connection,
    *,
    task_id: Optional[str] = None,
    limit: int = 20,
) -> list[PixelEvent]:
    params: list[Any] = []
    query = "SELECT * FROM kanban_pixel_events WHERE 1=1"
    if task_id is not None:
        query += " AND (task_id = ? OR task_id IS NULL)"
        params.append(task_id)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return [PixelEvent.from_row(row) for row in conn.execute(query, params).fetchall()]


def claim_pixel_lane(
    conn: sqlite3.Connection,
    *,
    lane_id: str,
    agent_id: str,
    evidence: str,
    task_id: Optional[str] = None,
) -> PixelClaim:
    lane = _normalize_funnel_text(lane_id)
    agent = str(agent_id or "").strip()
    evidence_text = str(evidence or "").strip()
    if not lane:
        raise ValueError("pixel claim lane_id is required")
    if not agent:
        raise ValueError("pixel claim agent_id is required")
    if not evidence_text:
        raise ValueError("pixel claim evidence is required")
    if task_id and get_task(conn, task_id) is None:
        raise ValueError(f"unknown task: {task_id}")
    now = int(time.time())
    token = secrets.token_hex(12)
    with write_txn(conn):
        conflict = conn.execute(
            """
            SELECT * FROM kanban_pixel_claims
             WHERE active = 1 AND lane_id = ?
             ORDER BY claimed_at DESC, id DESC LIMIT 1
            """,
            (lane,),
        ).fetchone()
        if conflict is not None:
            raise ValueError(
                f"pixel claim conflict on lane {lane!r}: active claim "
                f"{conflict['id']} by {conflict['agent_id']}"
            )
        cur = conn.execute(
            """
            INSERT INTO kanban_pixel_claims
                (lane_id, task_id, agent_id, claim_token, evidence, active, claimed_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (lane, task_id, agent, token, evidence_text, now),
        )
        claim_id = int(cur.lastrowid or 0)
        if task_id:
            _append_event(
                conn,
                task_id,
                "pixel_claimed",
                {
                    "claim_id": claim_id,
                    "lane_id": lane,
                    "agent_id": agent,
                    "evidence": evidence_text,
                },
            )
    row = conn.execute("SELECT * FROM kanban_pixel_claims WHERE id = ?", (claim_id,)).fetchone()
    return PixelClaim.from_row(row)


def release_pixel_claim(
    conn: sqlite3.Connection,
    *,
    claim_id: int,
    agent_id: str,
    evidence: str,
) -> PixelClaim:
    agent = str(agent_id or "").strip()
    evidence_text = str(evidence or "").strip()
    if not agent:
        raise ValueError("pixel release agent_id is required")
    if not evidence_text:
        raise ValueError("pixel release evidence is required")
    now = int(time.time())
    with write_txn(conn):
        row = conn.execute(
            "SELECT * FROM kanban_pixel_claims WHERE id = ? AND active = 1",
            (int(claim_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"active pixel claim {claim_id} not found")
        if row["agent_id"] != agent:
            raise ValueError(
                f"pixel claim {claim_id} is owned by {row['agent_id']!r}, not {agent!r}"
            )
        conn.execute(
            """
            UPDATE kanban_pixel_claims
               SET active = 0, released_at = ?, release_evidence = ?
             WHERE id = ? AND active = 1
            """,
            (now, evidence_text, int(claim_id)),
        )
        if row["task_id"]:
            _append_event(
                conn,
                row["task_id"],
                "pixel_released",
                {
                    "claim_id": int(claim_id),
                    "lane_id": row["lane_id"],
                    "agent_id": agent,
                    "evidence": evidence_text,
                },
            )
    released = conn.execute("SELECT * FROM kanban_pixel_claims WHERE id = ?", (int(claim_id),)).fetchone()
    return PixelClaim.from_row(released)


def list_pixel_claims(
    conn: sqlite3.Connection,
    *,
    active: Optional[bool] = None,
    task_id: Optional[str] = None,
) -> list[PixelClaim]:
    query = "SELECT * FROM kanban_pixel_claims WHERE 1=1"
    params: list[Any] = []
    if active is not None:
        query += " AND active = ?"
        params.append(1 if active else 0)
    if task_id is not None:
        query += " AND (task_id = ? OR task_id IS NULL)"
        params.append(task_id)
    query += " ORDER BY active DESC, claimed_at DESC, id DESC"
    return [PixelClaim.from_row(row) for row in conn.execute(query, params).fetchall()]


def pixel_claim_to_dict(claim: PixelClaim) -> dict[str, Any]:
    return {
        "id": claim.id,
        "lane_id": claim.lane_id,
        "task_id": claim.task_id,
        "agent_id": claim.agent_id,
        "evidence": claim.evidence,
        "active": claim.active,
        "claimed_at": claim.claimed_at,
        "released_at": claim.released_at,
        "release_evidence": claim.release_evidence,
    }


def pixel_event_to_dict(event: PixelEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "type": event.event_type,
        "stage_key": event.stage_key,
        "task_id": event.task_id,
        "status": event.status,
        "evidence": event.evidence,
        "created_at": event.created_at,
    }


def _pixel_typed_evidence_keys(
    *,
    funnel_data: Optional[dict],
    metadata: Optional[dict],
    pixel_events: Iterable[PixelEvent],
) -> set[str]:
    keys: set[str] = set()

    def add(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                if v:
                    keys.add(str(k).strip())
                    add(v)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
            return
        text = str(value).strip()
        if text:
            keys.add(text)

    if isinstance(funnel_data, dict):
        add(funnel_data.get("transition_evidence"))
        for field in ("proof", "proofs", "artifact", "artifacts", "outcome", "outcomes", "evidence"):
            add(funnel_data.get(field))
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            key_text = str(key).strip()
            if key_text and value and any(part in key_text for part in ("proof", "artifact", "outcome")):
                keys.add(key_text)
            if key_text in {
                "proof", "proofs", "artifact", "artifacts",
                "outcome", "outcomes", "evidence", "transition_evidence",
            }:
                add(value)
    for event in pixel_events:
        if event.status == "pass":
            keys.add(event.event_type)
            keys.add(event.evidence)
    return {key for key in keys if key}


def _pixel_open_required_variables(funnel_data: Optional[dict]) -> list[str]:
    if not isinstance(funnel_data, dict):
        return []
    candidates: list[Any] = [
        funnel_data.get("open_required_variables"),
        funnel_data.get("required_variables_open"),
        funnel_data.get("open_variables"),
    ]
    conversation_state = funnel_data.get("conversation_state")
    if isinstance(conversation_state, dict):
        candidates.extend([
            conversation_state.get("open_required_variables"),
            conversation_state.get("required_variables_open"),
            conversation_state.get("open_variables"),
        ])
    out: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            out.extend(str(k) for k, v in candidate.items() if v)
        elif isinstance(candidate, (list, tuple, set)):
            out.extend(str(item) for item in candidate if str(item).strip())
        elif isinstance(candidate, str) and candidate.strip():
            out.append(candidate.strip())
    return sorted(set(out))


def _pixel_requires_approval(pixel: dict, funnel_data: Optional[dict]) -> bool:
    if pixel.get("approval_required") or pixel.get("owner_approval_required") or pixel.get("policy_approval_required"):
        return True
    if not isinstance(funnel_data, dict):
        return False
    if funnel_data.get("approval_required") or funnel_data.get("requires_approval"):
        return True
    authority = funnel_data.get("authority") or funnel_data.get("approval") or {}
    return isinstance(authority, dict) and bool(
        authority.get("requires_approval")
        or authority.get("owner_approval_required")
        or authority.get("policy_approval_required")
    )


def validate_pixel_done(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    metadata: Optional[dict] = None,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Return the native Pixel done-gate verdict for ``task_id``.

    The verdict is machine-readable and fail-closed: Pixel-enabled boards must
    satisfy their goal/success contract, typed conversion evidence, no open
    missing evidence/variables/approval gates, and no active execution routes
    or Pixel claims before a task may move to ``done``.
    """
    slug = _normalize_board_slug(board) or get_current_board()
    pixel = _pixel_config(slug)
    task = get_task(conn, task_id)
    blockers: list[dict[str, Any]] = []
    if task is None:
        return {"ok": False, "task_id": task_id, "board": slug, "pixel_enabled": bool(pixel), "blockers": [{"code": "unknown_task", "message": f"unknown task: {task_id}"}]}

    goal = str(pixel.get("goal") or "").strip()
    success = _string_list(pixel.get("success"))
    pixel_enabled = bool(goal and success)
    if not pixel_enabled:
        blockers.append({
            "code": "missing_pixel_contract",
            "message": "board has no native Pixel goal/success contract",
        })

    current_stage = task.stage_key or task.current_step_key
    stage_events = dict(pixel.get("stages") or {}) if isinstance(pixel.get("stages"), dict) else {}
    if stage_events and not current_stage:
        blockers.append({
            "code": "missing_current_stage",
            "message": "task has no current Pixel stage",
        })

    pixel_events = list_pixel_events(conn, task_id=task_id, limit=100)
    typed_keys = _pixel_typed_evidence_keys(
        funnel_data=task.funnel_data,
        metadata=metadata,
        pixel_events=pixel_events,
    )
    required_evidence: set[str] = set()
    if current_stage and stage_events.get(current_stage):
        required_evidence.add(str(stage_events[current_stage]).strip())

    workflow = read_board_metadata(slug).get("workflow")
    if isinstance(workflow, dict) and current_stage:
        stage = _workflow_stage_map(workflow).get(current_stage)
        if isinstance(stage, dict):
            for exit_row in stage.get("exit_criteria") or []:
                if isinstance(exit_row, dict):
                    required_evidence |= {
                        str(item).strip()
                        for item in exit_row.get("evidence_required") or []
                        if str(item).strip()
                    }

    missing_required = sorted(key for key in required_evidence if key not in typed_keys)
    if missing_required:
        blockers.append({
            "code": "missing_stage_conversion_evidence",
            "message": "missing typed Pixel stage conversion evidence",
            "stage_key": current_stage,
            "missing": missing_required,
        })

    if isinstance(task.funnel_data, dict):
        missing_evidence = task.funnel_data.get("missing_evidence")
        if isinstance(missing_evidence, dict):
            missing_evidence = [k for k, v in missing_evidence.items() if v]
        if isinstance(missing_evidence, (list, tuple, set)) and any(str(item).strip() for item in missing_evidence):
            blockers.append({
                "code": "funnel_missing_evidence",
                "message": "funnel_data.missing_evidence is not empty",
                "missing": [str(item) for item in missing_evidence if str(item).strip()],
            })

    open_vars = _pixel_open_required_variables(task.funnel_data)
    if open_vars:
        blockers.append({
            "code": "open_required_variables",
            "message": "required conversation variables are still open",
            "variables": open_vars,
        })

    if _pixel_requires_approval(pixel, task.funnel_data):
        approval_keys = {"approval", "owner_approval", "policy_approval"}
        has_approval = bool(approval_keys & typed_keys)
        if not has_approval:
            blockers.append({
                "code": "missing_approval_evidence",
                "message": "owner/policy approval is required but no approval evidence exists",
            })

    if task.status == "blocked":
        blockers.append({"code": "task_blocked", "message": "task status is blocked"})

    active_routes = list_watch_routes(conn, task_id=task_id, active=True)
    if active_routes:
        blockers.append({
            "code": "active_watch_routes",
            "message": "task has active watch routes",
            "routes": [watch_route_to_dict(route) for route in active_routes],
        })

    active_claims = list_pixel_claims(conn, active=True)
    if active_claims:
        blockers.append({
            "code": "active_pixel_claims",
            "message": "board has active Pixel claims",
            "claims": [pixel_claim_to_dict(claim) for claim in active_claims],
        })

    return {
        "ok": not blockers,
        "task_id": task_id,
        "board": slug,
        "pixel_enabled": pixel_enabled,
        "goal": goal or None,
        "success": success,
        "stage_key": current_stage,
        "required_evidence": sorted(required_evidence),
        "typed_evidence": sorted(typed_keys),
        "blockers": blockers,
    }


class PixelDoneGateError(RuntimeError):
    """Raised when a Pixel-enabled board rejects task completion."""

    def __init__(self, verdict: dict[str, Any]):
        self.verdict = verdict
        codes = ", ".join(str(b.get("code")) for b in verdict.get("blockers", []))
        super().__init__(f"pixel done gate failed for {verdict.get('task_id')}: {codes}")


def pixel_brief(conn: sqlite3.Connection, *, board: Optional[str] = None) -> dict[str, Any]:
    slug = _normalize_board_slug(board) or get_current_board()
    pixel = _pixel_config(slug)
    recent_events = [pixel_event_to_dict(event) for event in list_pixel_events(conn, limit=20)]
    active_claims = [pixel_claim_to_dict(claim) for claim in list_pixel_claims(conn, active=True)]
    blockers: list[dict[str, Any]] = []
    done_gates: list[dict[str, Any]] = []
    for task in list_tasks(conn, include_archived=False):
        if task.status in {"done", "archived"}:
            continue
        verdict = validate_pixel_done(conn, task.id, board=slug)
        summary = {
            "task_id": task.id,
            "ok": verdict["ok"],
            "stage_key": verdict.get("stage_key"),
            "blockers": verdict.get("blockers", []),
        }
        done_gates.append(summary)
        if not verdict["ok"]:
            blockers.extend(verdict.get("blockers", []))
    return {
        "board": slug,
        "pixel_enabled": is_pixel_enabled(slug),
        "goal": pixel.get("goal"),
        "success": _string_list(pixel.get("success")),
        "stages": dict(pixel.get("stages") or {}),
        "recent_events": recent_events,
        "active_claims": active_claims,
        "blockers": blockers,
        "done_gates": done_gates,
    }


def _append_event(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: Optional[dict] = None,
    *,
    run_id: Optional[int] = None,
) -> None:
    """Record an event row.  Called from within an already-open txn.

    ``run_id`` is optional: pass the current run id so UIs can group
    events by attempt. For events that aren't scoped to a single run
    (task created/edited/archived, dependency promotion) leave it None
    and the row carries NULL.
    """
    now = int(time.time())
    pl = json.dumps(payload, ensure_ascii=False) if payload else None
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, kind, pl, now),
    )


def _end_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    summary: Optional[str] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
    status: Optional[str] = None,
) -> Optional[int]:
    """Close the currently-active run for ``task_id`` and clear the pointer.

    ``outcome`` is the semantic result (completed / blocked / crashed /
    timed_out / spawn_failed / gave_up / reclaimed). ``status`` is the
    run-row status (usually just ``outcome``, but callers can pass it
    explicitly). Returns the closed run_id or ``None`` if no active run
    existed (e.g. a CLI user calling ``hermes kanban complete`` on a
    task that was never claimed).
    """
    now = int(time.time())
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if not row or not row["current_run_id"]:
        return None
    run_id = int(row["current_run_id"])
    conn.execute(
        """
        UPDATE task_runs
           SET status        = ?,
               outcome       = ?,
               summary       = ?,
               error         = ?,
               metadata      = ?,
               ended_at      = ?,
               claim_lock    = NULL,
               claim_expires = NULL,
               worker_pid    = NULL
         WHERE id = ?
           AND ended_at IS NULL
        """,
        (
            status or outcome,
            outcome,
            summary,
            error,
            json.dumps(metadata, ensure_ascii=False) if metadata else None,
            now,
            run_id,
        ),
    )
    conn.execute(
        "UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,),
    )
    return run_id


def _current_run_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    return int(row["current_run_id"]) if row and row["current_run_id"] else None


def _synthesize_ended_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    summary: Optional[str] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> int:
    """Insert a zero-duration, already-closed run row.

    Used when a terminal transition happens on a task that was never
    claimed (CLI user calling ``hermes kanban complete <ready-task>
    --summary X``, or dashboard "mark done" on a ready task). Without
    this, the handoff fields (summary / metadata / error) would be
    silently dropped: ``_end_run`` is a no-op because there's no
    current run.

    The synthetic run has ``started_at == ended_at == now`` so it
    shows up in attempt history as "instant" and doesn't skew elapsed
    stats. Caller is responsible for leaving ``current_run_id`` NULL
    (or for clearing it elsewhere in the same txn) since this
    function does NOT touch the tasks row.
    """
    now = int(time.time())
    trow = conn.execute(
        "SELECT assignee, current_step_key FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    profile = trow["assignee"] if trow else None
    step_key = trow["current_step_key"] if trow else None
    cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key,
            status, outcome,
            summary, error, metadata,
            started_at, ended_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id, profile, step_key,
            outcome, outcome,
            summary, error,
            json.dumps(metadata, ensure_ascii=False) if metadata else None,
            now, now,
        ),
    )
    return int(cur.lastrowid or 0)


# ---------------------------------------------------------------------------
# Dependency resolution (todo -> ready)
# ---------------------------------------------------------------------------

def _has_sticky_block(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return True when ``task_id`` is sticky-blocked by an explicit
    worker/operator ``kanban_block`` call (#28712).

    A ``blocked`` status can come from two very different sources:

    * **Worker- or operator-initiated** — a worker called
      ``kanban_block(reason="review-required: ...")`` (or somebody ran
      ``hermes kanban block <id>``).  This is a deliberate handoff that
      should stay blocked until an operator unblocks it.  The block tool
      emits a ``"blocked"`` event row in ``task_events``.

    * **Circuit-breaker** — ``_record_task_failure`` tripped after
      repeated crashes / spawn failures / timeouts.  This emits
      ``"gave_up"``, *not* ``"blocked"``, and is meant to recover
      automatically once the underlying conditions change (e.g. parents
      finish, transient infra error clears).

    The cheapest signal that distinguishes the two is the most recent
    ``"blocked"`` / ``"unblocked"`` event for the task.  If the most
    recent one is ``"blocked"`` (or there is a ``"blocked"`` event and
    no ``"unblocked"`` event has fired since), the task is sticky and
    ``recompute_ready`` must *not* auto-promote it.

    Returns ``False`` when there is no such event at all (e.g. the task
    was set to ``status='blocked'`` by the circuit breaker or by direct
    DB manipulation) — preserves the pre-#28712 auto-recover semantics
    for that path.
    """
    row = conn.execute(
        "SELECT kind FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return bool(row) and row["kind"] == "blocked"


def recompute_ready(conn: sqlite3.Connection) -> int:
    """Promote ``todo`` tasks to ``ready`` when all parents are ``done`` or ``archived``.

    Returns the number of tasks promoted.  Safe to call inside or outside
    an existing transaction; it opens its own IMMEDIATE txn.

    ``blocked`` tasks are also considered for promotion (so a task
    blocked purely by a parent dependency unblocks itself when the
    parent completes), *except* when the most recent block event was a
    worker-initiated ``kanban_block`` — those stay blocked until an
    explicit ``kanban_unblock`` (#28712).  Without that guard, a
    ``review-required`` handoff would auto-respawn, the fresh worker
    would find nothing to do, exit cleanly, get recorded as a protocol
    violation, and the cycle would repeat indefinitely.
    """
    promoted = 0
    with write_txn(conn):
        todo_rows = conn.execute(
            "SELECT id, status FROM tasks WHERE status IN ('todo', 'blocked')"
        ).fetchall()
        for row in todo_rows:
            task_id = row["id"]
            cur_status = row["status"]
            if cur_status == "blocked" and _has_sticky_block(conn, task_id):
                # Worker / operator asked for human review — do not
                # silently auto-recover.  ``unblock_task`` is the only
                # legitimate exit (it emits ``"unblocked"`` which flips
                # this predicate back).
                continue
            parents = conn.execute(
                "SELECT t.status FROM tasks t "
                "JOIN task_links l ON l.parent_id = t.id "
                "WHERE l.child_id = ?",
                (task_id,),
            ).fetchall()
            if all(p["status"] in ("done", "archived") for p in parents):
                # Blocked tasks also get their failure counters reset —
                # this is effectively an auto-unblock (circuit-breaker
                # recovery; worker-initiated blocks are skipped above).
                if cur_status == "blocked":
                    conn.execute(
                        "UPDATE tasks SET status = 'ready', "
                        "consecutive_failures = 0, last_failure_error = NULL "
                        "WHERE id = ? AND status = 'blocked'",
                        (task_id,),
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'todo'",
                        (task_id,),
                    )
                _append_event(conn, task_id, "promoted", None)
                promoted += 1
    return promoted


# ---------------------------------------------------------------------------
# Claim / complete / block
# ---------------------------------------------------------------------------

def claim_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
    board: Optional[str] = None,
) -> Optional[Task]:
    """Atomically transition ``ready -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``ready`` status).
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    board_slug = _connection_board(conn, board)
    eligibility = evaluate_dispatch_eligibility(conn, task_id, board=board_slug)
    if not eligibility.get("ok"):
        _block_contract_ineligible(
            conn,
            task_id,
            eligibility,
            source="claim",
            allowed_statuses=("ready",),
        )
        return None
    with write_txn(conn):
        # Structural invariant: never transition ready -> running while any
        # parent is not yet 'done'. This is the single enforcement point
        # regardless of which writer (create_task, link_tasks, unblock_task,
        # release_stale_claims, manual SQL) set status='ready'. If a racy
        # writer promoted a task with undone parents, demote it back to
        # 'todo' here — recompute_ready will re-promote when the parents
        # actually finish. See RCA at
        # kanban/boards/cookai/workspaces/t_a6acd07d/root-cause.md.
        undone = conn.execute(
            "SELECT 1 FROM task_links l "
            "JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') LIMIT 1",
            (task_id,),
        ).fetchone()
        if undone:
            conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'ready'",
                (task_id,),
            )
            _append_event(
                conn, task_id, "claim_rejected",
                {"reason": "parents_not_done"},
            )
            return None
        # Defensive: if a prior run somehow leaked (invariant violation from
        # an unknown code path), close it as 'reclaimed' so we don't strand
        # it when the CAS resets the pointer below. No-op when the invariant
        # holds (the common case).
        stale = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ? AND status = 'ready'",
            (task_id,),
        ).fetchone()
        if stale and stale["current_run_id"]:
            conn.execute(
                """
                UPDATE task_runs
                   SET status = 'reclaimed', outcome = 'reclaimed',
                       summary = COALESCE(summary, 'invariant recovery on re-claim'),
                       ended_at = ?,
                       claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
                 WHERE id = ? AND ended_at IS NULL
                """,
                (now, int(stale["current_run_id"])),
            )
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'running',
                   claim_lock    = ?,
                   claim_expires = ?,
                   started_at    = COALESCE(started_at, ?)
             WHERE id = ?
               AND status = 'ready'
               AND claim_lock IS NULL
            """,
            (lock, expires, now, task_id),
        )
        if cur.rowcount != 1:
            return None
        # Look up the current task row so we can populate the run with
        # its assignee / step / runtime cap.
        trow = conn.execute(
            "SELECT assignee, max_runtime_seconds, current_step_key "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_cur = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, step_key, status,
                claim_lock, claim_expires, max_runtime_seconds,
                started_at
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (
                task_id,
                trow["assignee"] if trow else None,
                trow["current_step_key"] if trow else None,
                lock,
                expires,
                trow["max_runtime_seconds"] if trow else None,
                now,
            ),
        )
        run_id = run_cur.lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (run_id, task_id),
        )
        _append_event(
            conn, task_id, "claimed",
            {"lock": lock, "expires": expires, "run_id": run_id},
            run_id=run_id,
        )
        return get_task(conn, task_id)


def claim_review_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
    board: Optional[str] = None,
) -> Optional[Task]:
    """Atomically transition ``review -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``review`` status).

    Unlike ``claim_task`` (which handles ``ready -> running``), this
    does NOT check parent dependencies — the task already passed that
    gate on its original ``todo -> ready -> running`` transition.

    Creates a new run entry so the review agent's lifecycle is tracked
    independently from the original worker run.
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    board_slug = _connection_board(conn, board)
    eligibility = evaluate_dispatch_eligibility(conn, task_id, board=board_slug)
    if not eligibility.get("ok"):
        _block_contract_ineligible(
            conn,
            task_id,
            eligibility,
            source="claim",
            allowed_statuses=("review",),
        )
        return None
    with write_txn(conn):
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'running',
                   claim_lock    = ?,
                   claim_expires = ?,
                   started_at    = COALESCE(started_at, ?)
             WHERE id = ?
               AND status = 'review'
               AND claim_lock IS NULL
            """,
            (lock, expires, now, task_id),
        )
        if cur.rowcount != 1:
            return None
        trow = conn.execute(
            "SELECT assignee, max_runtime_seconds, current_step_key "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_cur = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, step_key, status,
                claim_lock, claim_expires, max_runtime_seconds,
                started_at
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (
                task_id,
                trow["assignee"] if trow else None,
                trow["current_step_key"] if trow else None,
                lock,
                expires,
                trow["max_runtime_seconds"] if trow else None,
                now,
            ),
        )
        run_id = run_cur.lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (run_id, task_id),
        )
        _append_event(
            conn, task_id, "claimed",
            {"lock": lock, "expires": expires, "run_id": run_id,
             "source_status": "review"},
            run_id=run_id,
        )
        return get_task(conn, task_id)


def heartbeat_claim(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> bool:
    """Extend a running claim.  Returns True if we still own it.

    Workers that know they'll exceed 15 minutes should call this every
    few minutes to keep ownership.
    """
    expires = int(time.time()) + _resolve_claim_ttl_seconds(ttl_seconds)
    lock = claimer or _claimer_id()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock = ?",
            (expires, task_id, lock),
        )
        if cur.rowcount == 1:
            run_id = _current_run_id(conn, task_id)
            if run_id is not None:
                conn.execute(
                    "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                    (expires, run_id),
                )
            return True
        return False


def release_stale_claims(
    conn: sqlite3.Connection,
    *,
    signal_fn=None,
) -> int:
    """Reset any ``running`` task whose claim has expired.

    A stale-by-TTL claim whose host-local worker PID is still alive is
    *extended* (with a ``claim_extended`` event) instead of being
    reclaimed. Reclaiming a live worker mid-flight produces the spawn-
    then-immediately-reclaim loop seen on slow models that spend longer
    than ``DEFAULT_CLAIM_TTL_SECONDS`` inside a single tool-free LLM
    call (#23025): no tool calls means no ``kanban_heartbeat``, even
    though the subprocess is healthy. ``enforce_max_runtime`` and
    ``detect_crashed_workers`` remain the upper bounds for genuinely
    wedged or dead workers.

    Returns the number of stale claims actually reclaimed (live-pid
    extensions don't count). Safe to call often.
    """
    now = int(time.time())
    reclaimed = 0
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    stale = conn.execute(
        "SELECT id, claim_lock, worker_pid, claim_expires, last_heartbeat_at "
        "FROM tasks "
        "WHERE status = 'running' AND claim_expires IS NOT NULL "
        "  AND claim_expires < ?",
        (now,),
    ).fetchall()
    for row in stale:
        lock = row["claim_lock"] or ""
        host_local = lock.startswith(host_prefix)
        if host_local and row["worker_pid"] and _pid_alive(row["worker_pid"]):
            new_expires = now + _resolve_claim_ttl_seconds()
            with write_txn(conn):
                cur = conn.execute(
                    "UPDATE tasks SET claim_expires = ? "
                    "WHERE id = ? AND status = 'running' "
                    "  AND claim_lock IS ? "
                    "  AND claim_expires IS NOT NULL "
                    "  AND claim_expires < ?",
                    (new_expires, row["id"], row["claim_lock"], now),
                )
                if cur.rowcount != 1:
                    continue
                run_id = _current_run_id(conn, row["id"])
                if run_id is not None:
                    conn.execute(
                        "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                        (new_expires, run_id),
                    )
                _append_event(
                    conn, row["id"], "claim_extended",
                    {
                        "reason": "pid_alive",
                        "worker_pid": int(row["worker_pid"]),
                        "claim_lock": row["claim_lock"],
                        "claim_expires_was": int(row["claim_expires"]),
                        "claim_expires_now": new_expires,
                        "last_heartbeat_at": (
                            int(row["last_heartbeat_at"])
                            if row["last_heartbeat_at"] is not None
                            else None
                        ),
                    },
                    run_id=run_id,
                )
            continue

        termination = _terminate_reclaimed_worker(
            row["worker_pid"], row["claim_lock"], signal_fn=signal_fn,
        )
        with write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status = 'running' AND claim_lock IS ? "
                "AND claim_expires IS NOT NULL AND claim_expires < ?",
                (row["id"], row["claim_lock"], now),
            )
            if cur.rowcount != 1:
                continue
            run_id = _end_run(
                conn, row["id"],
                outcome="reclaimed", status="reclaimed",
                error=f"stale_lock={row['claim_lock']}",
                metadata=termination,
            )
            payload = {
                "stale_lock": row["claim_lock"],
                "worker_pid": (
                    int(row["worker_pid"])
                    if row["worker_pid"] is not None else None
                ),
                "claim_expires": int(row["claim_expires"]),
                "last_heartbeat_at": (
                    int(row["last_heartbeat_at"])
                    if row["last_heartbeat_at"] is not None else None
                ),
                "now": now,
                "host_local": host_local,
            }
            payload.update(termination)
            _append_event(
                conn, row["id"], "reclaimed",
                payload,
                run_id=run_id,
            )
            reclaimed += 1
    return reclaimed


def reclaim_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    signal_fn=None,
) -> bool:
    """Operator-driven reclaim: release the claim and reset to ``ready``.

    Unlike :func:`release_stale_claims` which only acts on tasks whose
    ``claim_expires`` has passed, this function reclaims immediately
    regardless of TTL. Intended for the dashboard/CLI recovery flow
    when an operator wants to abort a running worker without waiting
    for the TTL to expire (e.g. after seeing a hallucination warning).

    Returns True if a reclaim happened, False if the task isn't in a
    reclaimable state (not running, or doesn't exist).
    """
    row = conn.execute(
        "SELECT status, claim_lock, worker_pid FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return False
    if row["status"] != "running" and row["claim_lock"] is None:
        # Nothing to reclaim — already ready / blocked / done.
        return False
    prev_lock = row["claim_lock"]
    termination = _terminate_reclaimed_worker(
        row["worker_pid"], prev_lock, signal_fn=signal_fn,
    )
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status IN ('running', 'ready', 'blocked') "
            "AND claim_lock IS ?",
            (task_id, prev_lock),
        )
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="reclaimed", status="reclaimed",
            error=(
                f"manual_reclaim: {reason}" if reason
                else f"manual_reclaim lock={prev_lock}"
            ),
            metadata=termination,
        )
        payload = {
            "manual": True,
            "reason": reason,
            "prev_lock": prev_lock,
        }
        payload.update(termination)
        _append_event(
            conn, task_id, "reclaimed",
            payload,
            run_id=run_id,
        )
    # Operator intervention — they've looked at the task, so the
    # consecutive-failures counter is now stale. Give the next retry
    # a fresh budget. (_clear_failure_counter opens its own write_txn,
    # so it runs after the enclosing one commits.)
    _clear_failure_counter(conn, task_id)
    return True


def reassign_task(
    conn: sqlite3.Connection,
    task_id: str,
    profile: Optional[str],
    *,
    reclaim_first: bool = False,
    reason: Optional[str] = None,
) -> bool:
    """Reassign a task, optionally reclaiming a stuck running worker first.

    This is the recovery path for "this profile's model is broken, try
    a different one". If ``reclaim_first`` is True, any active claim is
    released (via :func:`reclaim_task`) before the reassign happens;
    otherwise the function refuses to reassign a currently-running task
    and returns False (caller can retry with ``reclaim_first=True``).

    Returns True if the reassign landed. ``profile`` may be ``None`` to
    unassign entirely.
    """
    if reclaim_first:
        # Safe to call even if nothing to reclaim.
        reclaim_task(conn, task_id, reason=reason or "reassign")
    # assign_task handles its own txn + the still-running guard.
    try:
        return assign_task(conn, task_id, profile)
    except RuntimeError:
        # Task is still running and reclaim_first was False; caller
        # needs to decide whether to retry with reclaim.
        return False


def _verify_created_cards(
    conn: sqlite3.Connection,
    completing_task_id: str,
    claimed_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Partition ``claimed_ids`` into (verified, phantom).

    A card is "verified" iff a row exists in ``tasks`` AND at least one
    of the following holds:

    * ``created_by`` matches the completing task's ``assignee`` profile
      (the common case: worker A spawns a card via ``kanban_create``,
      which stamps ``created_by=A``).
    * ``created_by`` matches the completing task's id (edge case where
      a worker passed its own task id as the ``created_by`` value).
    * The card is linked as a ``task_links.child`` of the completing
      task — i.e. the worker explicitly called ``kanban_create`` with
      ``parents=[<current_task>]``. This accepts cards created through
      the dashboard/CLI by a different principal but then attached to
      the completing task by the worker.

    ``phantom`` returns ids that either don't exist at all, or exist
    but don't satisfy any of the three trust conditions. The caller
    decides what to do with each bucket; this helper never mutates.
    """
    claimed = [str(x).strip() for x in (claimed_ids or []) if str(x).strip()]
    if not claimed:
        return [], []
    # Dedupe while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for cid in claimed:
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)

    row = conn.execute(
        "SELECT assignee FROM tasks WHERE id = ?", (completing_task_id,),
    ).fetchone()
    if row is None:
        # Completing task not found — nothing resolves.
        return [], ordered
    completing_assignee = row["assignee"]

    # Batch-fetch existence + created_by in one query.
    placeholders = ",".join(["?"] * len(ordered))
    rows = conn.execute(
        f"SELECT id, created_by FROM tasks WHERE id IN ({placeholders})",
        tuple(ordered),
    ).fetchall()
    found = {r["id"]: r["created_by"] for r in rows}

    # Pull the set of cards linked as children of the completing task.
    # Cheap: one query, indexed on parent_id.
    linked_children: set[str] = set(child_ids(conn, completing_task_id))

    verified: list[str] = []
    phantom: list[str] = []
    for cid in ordered:
        created_by = found.get(cid)
        if created_by is None:
            phantom.append(cid)
            continue
        # Accept if any of the three trust conditions holds.
        if completing_assignee and created_by == completing_assignee:
            verified.append(cid)
        elif created_by == completing_task_id:
            verified.append(cid)
        elif cid in linked_children:
            verified.append(cid)
        else:
            phantom.append(cid)
    return verified, phantom


# Task-id pattern used both by ``kanban_create`` (``t_<12 hex>``) and
# ``_new_task_id`` below. Kept permissive on length for forward compat:
# accept 8+ hex chars after the ``t_`` prefix.
_TASK_ID_PROSE_RE = re.compile(r"\bt_[a-f0-9]{8,}\b")


def _scan_prose_for_phantom_ids(
    conn: sqlite3.Connection,
    text: str,
) -> list[str]:
    """Regex-scan free-form text for ``t_<hex>`` references; return the
    ones that don't exist in ``tasks``.

    Used as a non-blocking advisory check on completion summaries. An
    empty return means "no suspicious references found" — either the
    text had no IDs at all, or every ID it mentioned resolves to a real
    task. Duplicates are deduped.
    """
    if not text:
        return []
    matches = _TASK_ID_PROSE_RE.findall(text)
    if not matches:
        return []
    # Dedupe preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    placeholders = ",".join(["?"] * len(unique))
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})",
        tuple(unique),
    ).fetchall()
    existing = {r["id"] for r in rows}
    return [m for m in unique if m not in existing]


class HallucinatedCardsError(ValueError):
    """Raised by ``complete_task`` when ``created_cards`` contains ids
    that don't exist or weren't created by the completing worker.

    The phantom list is attached as ``.phantom`` for callers that want
    structured access. Kept as ``ValueError`` subclass so existing
    tool-error handlers treat it as a recoverable user error.
    """

    def __init__(self, phantom: list[str], completing_task_id: str):
        self.phantom = list(phantom)
        self.completing_task_id = completing_task_id
        super().__init__(
            f"completion blocked: claimed created_cards that do not exist "
            f"or were not created by this worker: {', '.join(phantom)}"
        )


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    created_cards: Optional[Iterable[str]] = None,
    expected_run_id: Optional[int] = None,
    board: Optional[str] = None,
) -> bool:
    """Transition ``running|ready -> done`` and record ``result``.

    Accepts a task that is merely ``ready`` too, so a manual CLI
    completion (``hermes kanban complete <id>``) works without requiring
    a claim/start/complete sequence.

    ``summary`` and ``metadata`` are stored on the closing run (if any)
    and surfaced to downstream children via :func:`build_worker_context`.
    When ``summary`` is omitted we fall back to ``result`` so single-run
    callers do not have to pass both. ``metadata`` is a free-form dict
    (e.g. ``{"changed_files": [...], "tests_run": [...]}``) — workers
    are encouraged to use it for structured handoff facts.

    ``created_cards`` is an optional list of task ids the completing
    worker claims to have created. Each id is verified against
    ``tasks.created_by``. If any id is phantom (does not exist or was
    not created by this worker's assignee profile), completion is blocked
    with a ``HallucinatedCardsError`` and a
    ``completion_blocked_hallucination`` event is emitted so the rejected
    attempt is auditable. When all ids verify, they are recorded on the
    ``completed`` event payload.

    After a successful completion, ``summary`` and ``result`` are scanned
    for prose references like ``t_deadbeefcafe`` that do not resolve.
    Any suspected phantom references are recorded as a
    ``suspected_hallucinated_references`` event. This pass is advisory
    and never blocks.
    """
    now = int(time.time())

    # Gate: verify created_cards BEFORE the main write txn. A rejected
    # completion still needs an auditable event, so we emit it in a
    # tiny dedicated txn, then raise. The caller is responsible for
    # surfacing HallucinatedCardsError to the worker; this function
    # never mutates task state on a phantom-card rejection.
    if created_cards:
        verified_cards, phantom_cards = _verify_created_cards(
            conn, task_id, created_cards
        )
        if phantom_cards:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "completion_blocked_hallucination",
                    {
                        "phantom_cards": phantom_cards,
                        "verified_cards": verified_cards,
                        "summary_preview": (
                            (summary or result or "").strip().splitlines()[0][:200]
                            if (summary or result)
                            else None
                        ),
                    },
                )
            raise HallucinatedCardsError(phantom_cards, task_id)
    else:
        verified_cards = []

    board_slug = _connection_board(conn, board)
    contract_verdict = validate_contract_done(
        conn,
        task_id,
        metadata=metadata,
        board=board_slug,
    )
    if not contract_verdict.get("ok"):
        with write_txn(conn):
            _append_event(
                conn,
                task_id,
                "contract_done_blocked",
                {"verdict": contract_verdict},
            )
        raise ContractDoneGateError(contract_verdict)
    if is_pixel_enabled(board_slug):
        verdict = validate_pixel_done(conn, task_id, metadata=metadata, board=board_slug)
        if not verdict.get("ok"):
            with write_txn(conn):
                _append_event(
                    conn,
                    task_id,
                    "pixel_done_blocked",
                    {"verdict": verdict},
                )
            raise PixelDoneGateError(verdict)

    with write_txn(conn):
        if expected_run_id is None:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked', 'watching')
                """,
                (result, now, task_id),
            )
        else:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked', 'watching')
                   AND current_run_id = ?
                """,
                (result, now, task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        conn.execute(
            "UPDATE task_watch_routes SET active = 0, triggered_at = COALESCE(triggered_at, ?) "
            "WHERE task_id = ? AND active = 1",
            (now, task_id),
        )
        run_id = _end_run(
            conn, task_id,
            outcome="completed", status="done",
            summary=summary if summary is not None else result,
            metadata=metadata,
        )
        # If complete_task was called on a never-claimed task (ready or
        # blocked → done with no run in flight), synthesize a
        # zero-duration run so the handoff fields are persisted in
        # attempt history instead of silently lost.
        if run_id is None and (summary or metadata or result):
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="completed",
                summary=summary if summary is not None else result,
                metadata=metadata,
            )
        # Carry the handoff summary in the event payload so gateway
        # notifiers and dashboard WS consumers can render it without a
        # second SQL round-trip. First line only, 400 char cap — the
        # full summary stays on the run row.
        ev_summary = (summary if summary is not None else result) or ""
        ev_summary = ev_summary.strip().splitlines()[0][:400] if ev_summary else ""
        completed_payload: dict = {
            "result_len": len(result) if result else 0,
            "summary": ev_summary or None,
        }
        if verified_cards:
            completed_payload["verified_cards"] = verified_cards
        # Carry artifact paths in the event payload so the gateway
        # notifier can upload them as native attachments alongside the
        # completion message. Workers pass these via
        # ``kanban_complete(artifacts=[...])`` which stashes the list in
        # ``metadata["artifacts"]`` — we promote it onto the event so
        # consumers don't have to fetch the run row to find it.
        if isinstance(metadata, dict):
            md_artifacts = metadata.get("artifacts")
            if isinstance(md_artifacts, (list, tuple)):
                cleaned_artifacts = [
                    str(p).strip() for p in md_artifacts if isinstance(p, str) and str(p).strip()
                ]
                if cleaned_artifacts:
                    completed_payload["artifacts"] = cleaned_artifacts
        _append_event(
            conn, task_id, "completed",
            completed_payload,
            run_id=run_id,
        )
    # Prose-scan the summary + result for t_<hex> references that do
    # not resolve. Advisory — does not block the completion. Runs in
    # its own txn so the completion itself is already durable by the
    # time we emit the warning.
    scan_text = " ".join(filter(None, [summary, result]))
    if scan_text:
        phantom_refs = _scan_prose_for_phantom_ids(conn, scan_text)
        # Drop any phantom refs that were already flagged as verified
        # above (shouldn't happen — verified means they exist — but
        # belt-and-suspenders).
        phantom_refs = [p for p in phantom_refs if p not in set(verified_cards)]
        if phantom_refs:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "suspected_hallucinated_references",
                    {
                        "phantom_refs": phantom_refs,
                        "source": "completion_summary",
                    },
                    run_id=run_id,
                )
    # Successful completion — wipe the consecutive-failures counter.
    # Failure history stays on the event log for audit; the counter
    # just tracks "is there a current pathology the breaker should
    # care about", and a success resets that question.
    _clear_failure_counter(conn, task_id)
    # Recompute ready status for dependents (separate txn so children see done).
    recompute_ready(conn)
    # Clean up the scratch workspace and any stale tmux session for the worker.
    _cleanup_workspace(conn, task_id)
    board_slug = _connection_board(conn, board)
    _apply_completion_evidence_and_maybe_transition(
        conn,
        task_id,
        metadata=metadata,
        result=result,
        summary=summary,
        board=board_slug,
    )
    return True


# ---------------------------------------------------------------------------
# Workspace / tmux cleanup
# ---------------------------------------------------------------------------


def _is_managed_scratch_path(p: Path) -> bool:
    """Return True iff *p* is a strict descendant of a kanban-managed scratch root.

    A managed root is exclusively a ``workspaces/`` directory — never the
    broader kanban home, a board root, or sibling subtrees like ``logs/`` or
    ``boards/<slug>/`` itself. Allowed roots:

    * ``HERMES_KANBAN_WORKSPACES_ROOT`` when set (worker-side override
      injected by the dispatcher).
    * ``<kanban_home>/kanban/workspaces`` — default-board compatibility scratch root.
    * ``<kanban_home>/kanban/boards/<slug>/workspaces`` for each board slug
      that currently exists on disk.

    The check requires strict descendancy: a path equal to one of these
    roots is NOT managed (deleting the workspaces root would wipe every
    task's scratch dir at once), and a path that resolves to ``<kanban_home>
    /kanban`` itself, ``<kanban_home>/kanban/logs``, or
    ``<kanban_home>/kanban/boards/<slug>`` is rejected because those
    subtrees hold Hermes' own DB, metadata, and logs, not task workspaces.

    Used by :func:`_cleanup_workspace` to refuse to ``shutil.rmtree`` paths
    outside Hermes-managed storage. A board ``default_workdir`` pointing at a
    real source tree can otherwise pair with ``workspace_kind='scratch'`` and
    cause task completion to delete user data (#28818).
    """
    try:
        p_abs = p.resolve(strict=False)
    except OSError:
        return False
    roots: list[Path] = []
    override = os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT", "").strip()
    if override:
        try:
            roots.append(Path(override).expanduser().resolve(strict=False))
        except OSError:
            pass
    try:
        home = kanban_home()
    except OSError:
        home = None
    if home is not None:
        try:
            roots.append((home / "kanban" / "workspaces").resolve(strict=False))
        except OSError:
            pass
        try:
            boards_parent = (home / "kanban" / "boards").resolve(strict=False)
        except OSError:
            boards_parent = None
        if boards_parent is not None:
            try:
                entries = list(boards_parent.iterdir())
            except OSError:
                entries = []
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                try:
                    roots.append((entry / "workspaces").resolve(strict=False))
                except OSError:
                    continue
    for root in roots:
        if p_abs == root:
            continue
        try:
            if p_abs.is_relative_to(root):
                return True
        except ValueError:
            continue
    return False


def _cleanup_workspace(conn: sqlite3.Connection, task_id: str) -> None:
    """Remove a task's scratch workspace dir and kill its stale tmux session.

    Called from :func:`complete_task` after the DB transaction commits.
    Best-effort — any error is swallowed so cleanup never blocks task completion.
    Only ``scratch`` workspaces are removed; ``worktree`` and ``dir`` workspaces
    are intentionally preserved.
    """
    try:
        row = conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return
        kind: Optional[str] = row["workspace_kind"]
        path: Optional[str] = row["workspace_path"]
        if kind != "scratch" or not path:
            return
        import shutil
        wp = Path(path)
        if wp.is_dir():
            # Containment guard (#28818): a board's ``default_workdir`` can
            # pair ``workspace_kind='scratch'`` with a user-supplied path
            # pointing at a real source tree. Without this check, task
            # completion would unconditionally ``shutil.rmtree`` that path
            # and silently delete the user's source data.
            if _is_managed_scratch_path(wp):
                shutil.rmtree(wp, ignore_errors=True)
                _log.debug("Removed scratch workspace: %s", wp)
            else:
                _log.warning(
                    "Refusing to remove out-of-scratch workspace for task %s: %s "
                    "(workspace_kind='scratch' but path is outside any "
                    "kanban-managed workspaces root)",
                    task_id, wp,
                )
        # Also kill the tmux session for the worker that owned this task,
        # if the tmux session is now dead (worker process exited).
        _cleanup_worker_tmux(conn, task_id)
    except Exception:
        pass  # best-effort — never block completion


def _cleanup_worker_tmux(conn: sqlite3.Connection, task_id: str) -> None:
    """Kill the tmux session associated with a task's assignee, if dead."""
    try:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row or not row["assignee"]:
            return
        assignee: str = row["assignee"]
        # Workers named swarm1-12 use tmux sessions named swarm-swarm1 etc.
        session = f"swarm-{assignee}"
        # Check if session exists and pane is dead before killing
        out = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_dead}"],
            capture_output=True, text=True, timeout=5,
        )
        if out.stdout.strip() == "1":
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                capture_output=True, timeout=5,
            )
            _log.debug("Killed stale tmux session: %s", session)
    except Exception:
        pass  # best-effort — never block completion


# ---------------------------------------------------------------------------
# First-use tip for scratch workspaces
# ---------------------------------------------------------------------------
#
# Scratch workspaces are intentionally ephemeral — ``_cleanup_workspace``
# removes them as soon as ``complete_task`` runs.  New users often don't
# realize that and lose worker output (community report, May 2026).  The
# behavior is right; the lack of warning is the bug.
#
# On the FIRST scratch workspace materialization across the whole install
# we:
#   1. Log a warning line on the dispatcher logger.
#   2. Append a ``tip_scratch_workspace`` event on the task so it's visible
#      via ``hermes kanban show <id>`` and the dashboard.
#   3. Touch a sentinel file under ``kanban_home() / '.scratch_tip_shown'``
#      so we don't repeat the tip — once you know, you know.
#
# Scope is per-install, not per-board: a user creating a second board
# already learned the lesson on board #1.

_SCRATCH_TIP_SENTINEL_NAME = ".scratch_tip_shown"

_SCRATCH_TIP_MESSAGE = (
    "scratch workspaces are ephemeral — they're deleted when the task "
    "completes. Use --workspace worktree: (git worktree) or "
    "--workspace dir:/abs/path (existing dir) to preserve worker output."
)


def _scratch_tip_sentinel_path() -> Path:
    """Path to the per-install scratch-workspace-tip sentinel file."""
    return kanban_home() / _SCRATCH_TIP_SENTINEL_NAME


def _scratch_tip_shown() -> bool:
    """True iff the scratch-workspace tip has already been emitted on this
    install. Best-effort — any error means we re-emit, which is the safer
    failure mode for a help message."""
    try:
        return _scratch_tip_sentinel_path().exists()
    except OSError:
        return False


def _mark_scratch_tip_shown() -> None:
    """Touch the sentinel so future scratch workspaces stay silent.

    Best-effort: a failure here just means the tip might appear once more,
    which is preferable to crashing dispatch over a help message.
    """
    try:
        path = _scratch_tip_sentinel_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError:
        pass


def _maybe_emit_scratch_tip(
    conn: sqlite3.Connection,
    task_id: str,
    workspace_kind: Optional[str],
) -> None:
    """Emit the first-use scratch-workspace tip exactly once per install.

    Called from the dispatcher right after a scratch workspace is
    materialized. No-op for ``worktree`` / ``dir`` workspaces (they're
    preserved by design) and no-op after the sentinel exists.
    """
    if (workspace_kind or "scratch") != "scratch":
        return
    if _scratch_tip_shown():
        return
    try:
        _log.warning("kanban: %s (task %s)", _SCRATCH_TIP_MESSAGE, task_id)
        with write_txn(conn):
            _append_event(
                conn, task_id, "tip_scratch_workspace",
                {"message": _SCRATCH_TIP_MESSAGE},
            )
    except Exception:
        # Best-effort — never block the spawn loop over a help message.
        pass
    finally:
        _mark_scratch_tip_shown()


def edit_completed_task_result(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: str,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Backfill the user-visible result for an already completed task."""
    handoff_summary = summary if summary is not None else result
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if not row or row["status"] != "done":
            return False
        conn.execute(
            "UPDATE tasks SET result = ? WHERE id = ?",
            (result, task_id),
        )
        run = conn.execute(
            """
            SELECT id FROM task_runs
             WHERE task_id = ?
               AND outcome = 'completed'
             ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        run_id = int(run["id"]) if run else None
        if run_id is None:
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="completed",
                summary=handoff_summary,
                metadata=metadata,
            )
        else:
            conn.execute(
                "UPDATE task_runs SET summary = ? WHERE id = ?",
                (handoff_summary, run_id),
            )
            if metadata is not None:
                conn.execute(
                    "UPDATE task_runs SET metadata = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False), run_id),
                )
        ev_summary = (
            handoff_summary.strip().splitlines()[0][:400]
            if handoff_summary else ""
        )
        _append_event(
            conn, task_id, "edited",
            {
                "fields": (
                    ["result", "summary"]
                    + (["metadata"] if metadata is not None else [])
                ),
                "result_len": len(result) if result else 0,
                "summary": ev_summary or None,
            },
            run_id=run_id,
        )
    return True


def block_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Transition ``running -> blocked``."""
    with write_txn(conn):
        if expected_run_id is None:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'blocked',
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'watching')
                """,
                (task_id,),
            )
        else:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'blocked',
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'watching')
                   AND current_run_id = ?
                """,
                (task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        conn.execute(
            "UPDATE task_watch_routes SET active = 0, triggered_at = COALESCE(triggered_at, ?) "
            "WHERE task_id = ? AND active = 1",
            (int(time.time()), task_id),
        )
        run_id = _end_run(
            conn, task_id,
            outcome="blocked", status="blocked",
            summary=reason,
        )
        # Synthesize a run when blocking a never-claimed task so the
        # reason is preserved in attempt history.
        if run_id is None and reason:
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="blocked",
                summary=reason,
            )
        _append_event(conn, task_id, "blocked", {"reason": reason}, run_id=run_id)
        return True



def promote_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    actor: str,
    reason: Optional[str] = None,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[bool, Optional[str]]:
    """Manually promote a `todo` or `blocked` task to `ready`.

    Mirrors the automatic promotion done by ``recompute_ready`` but
    drives it from a deliberate operator action with an audit-trail
    entry. Refuses to promote if any parent dep is not in a terminal
    state (`done`/`archived`) unless ``force=True``. Does NOT change
    assignee or claim state. Returns ``(True, None)`` on success and
    ``(False, reason)`` if refused. ``dry_run=True`` validates the
    promotion would succeed without mutating state.
    """
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return False, f"task {task_id} not found"

    cur_status = row["status"]
    if cur_status not in ("todo", "blocked"):
        return False, (
            f"task {task_id} is {cur_status!r}; promote only applies to "
            f"'todo' or 'blocked'"
        )

    if not force:
        parents = conn.execute(
            "SELECT t.id, t.status FROM tasks t "
            "JOIN task_links l ON l.parent_id = t.id "
            "WHERE l.child_id = ?",
            (task_id,),
        ).fetchall()
        unsatisfied = [
            p["id"] for p in parents
            if p["status"] not in ("done", "archived")
        ]
        if unsatisfied:
            return False, (
                f"unsatisfied parent dependencies: "
                f"{', '.join(unsatisfied)} (use --force to override)"
            )

    if dry_run:
        return True, None

    with write_txn(conn):
        upd = conn.execute(
            "UPDATE tasks SET status = 'ready' "
            "WHERE id = ? AND status IN ('todo', 'blocked')",
            (task_id,),
        )
        if upd.rowcount != 1:
            return False, f"task {task_id} status changed during promotion"
        _append_event(
            conn,
            task_id,
            "promoted_manual",
            {"actor": actor, "reason": reason, "forced": force},
        )

    return True, None


def unblock_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Transition ``blocked``/``scheduled``/``watching`` -> ready or todo.

    Defensively closes any stale ``current_run_id`` pointer before flipping
    status. In the common path (``block_task`` closed the run already) this
    is a no-op. If a future or external write left the pointer dangling,
    the leaked run is closed as ``reclaimed`` inside the same txn so the
    runs invariant (``current_run_id IS NULL`` ⇔ run row in terminal
    state) holds for the rest of this function's lifetime.
    """
    now = int(time.time())
    with write_txn(conn):
        stale = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ? AND status IN ('blocked', 'scheduled', 'watching')",
            (task_id,),
        ).fetchone()
        if stale and stale["current_run_id"]:
            conn.execute(
                """
                UPDATE task_runs
                   SET status = 'reclaimed', outcome = 'reclaimed',
                       summary = COALESCE(summary, 'invariant recovery on unblock'),
                       ended_at = ?,
                       claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
                 WHERE id = ? AND ended_at IS NULL
                """,
                (now, int(stale["current_run_id"])),
            )
        # Re-gate on parent completion before flipping back to 'ready'.
        undone_parents = conn.execute(
            "SELECT 1 FROM task_links l "
            "JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? AND p.status != 'done' LIMIT 1",
            (task_id,),
        ).fetchone()
        new_status = "todo" if undone_parents else "ready"
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "consecutive_failures = 0, last_failure_error = NULL "
            "WHERE id = ? AND status IN ('blocked', 'scheduled', 'watching')",
            (new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        conn.execute(
            "UPDATE task_watch_routes SET active = 0, triggered_at = COALESCE(triggered_at, ?) "
            "WHERE task_id = ? AND active = 1",
            (now, task_id),
        )
        _append_event(
            conn, task_id, "unblocked",
            {"status": new_status} if new_status != "ready" else None,
        )
        return True


def _normalize_watch_payload(value: Optional[Any], *, field: str) -> Optional[dict]:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object/dict, got {type(value).__name__}")
    return value


def _normalize_trigger_type(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        raise ValueError("trigger_type is required")
    if not re.match(r"^[a-z0-9_:.]+$", text):
        raise ValueError("trigger_type may contain only letters, digits, _, :, and .")
    return text


def list_watch_routes(
    conn: sqlite3.Connection,
    task_id: Optional[str] = None,
    *,
    active: Optional[bool] = None,
) -> list[WatchRoute]:
    query = "SELECT * FROM task_watch_routes WHERE 1=1"
    params: list[Any] = []
    if task_id is not None:
        query += " AND task_id = ?"
        params.append(task_id)
    if active is not None:
        query += " AND active = ?"
        params.append(1 if active else 0)
    query += " ORDER BY active DESC, created_at DESC, id DESC"
    return [WatchRoute.from_row(row) for row in conn.execute(query, tuple(params)).fetchall()]


def watch_route_to_dict(route: WatchRoute) -> dict[str, Any]:
    return {
        "id": route.id,
        "task_id": route.task_id,
        "trigger_type": route.trigger_type,
        "trigger_key": route.trigger_key,
        "wake_status": route.wake_status,
        "reason": route.reason,
        "payload": route.payload,
        "active": route.active,
        "created_by": route.created_by,
        "created_at": route.created_at,
        "triggered_at": route.triggered_at,
        "trigger_payload": route.trigger_payload,
    }


def set_task_watching(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    trigger_type: str,
    trigger_key: Optional[str] = None,
    reason: Optional[str] = None,
    payload: Optional[dict] = None,
    wake_status: str = "ready",
    created_by: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> Optional[WatchRoute]:
    """Park a task in healthy waiting and register the event that wakes it."""
    trigger_type = _normalize_trigger_type(trigger_type)
    trigger_key = _normalize_funnel_text(trigger_key)
    payload = _normalize_watch_payload(payload, field="payload")
    if wake_status not in {"ready", "todo", "blocked", "review"}:
        raise ValueError("wake_status must be one of ['blocked', 'ready', 'review', 'todo']")
    now = int(time.time())
    with write_txn(conn):
        existing = conn.execute(
            "SELECT current_run_id, funnel_data, assignee FROM tasks "
            "WHERE id = ? AND status IN ('running', 'ready', 'todo', 'scheduled', 'blocked', 'watching')",
            (task_id,),
        ).fetchone()
        if existing is None:
            return None
        current_run_id = existing["current_run_id"]
        if expected_run_id is not None and (
            current_run_id is None or int(current_run_id) != int(expected_run_id)
        ):
            return None
        run_id = _end_run(
            conn,
            task_id,
            outcome="watching",
            status="watching",
            summary=reason,
            metadata={
                "watch": {
                    "trigger_type": trigger_type,
                    "trigger_key": trigger_key,
                    "wake_status": wake_status,
                },
                **({"watch_payload": payload} if payload else {}),
            },
        )
        conn.execute(
            "UPDATE task_watch_routes SET active = 0, triggered_at = COALESCE(triggered_at, ?) "
            "WHERE task_id = ? AND active = 1",
            (now, task_id),
        )
        funnel_data: dict[str, Any] = {}
        if existing["funnel_data"]:
            try:
                parsed_funnel = json.loads(existing["funnel_data"])
                if isinstance(parsed_funnel, dict):
                    funnel_data = parsed_funnel
            except Exception:
                funnel_data = {}
        funnel_data.setdefault("substate", "watching")
        funnel_data["next_expected_event"] = (
            f"{trigger_type}:{trigger_key}" if trigger_key else trigger_type
        )
        funnel_data["watch"] = {
            "trigger_type": trigger_type,
            "trigger_key": trigger_key,
            "wake_status": wake_status,
        }
        funnel_data["owner"] = existing["assignee"]
        if reason:
            funnel_data["last_meaningful_event"] = reason
        conn.execute(
            "UPDATE tasks SET status = 'watching', claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, current_run_id = NULL, funnel_data = ? WHERE id = ?",
            (json.dumps(funnel_data, ensure_ascii=False), task_id),
        )
        cur = conn.execute(
            """
            INSERT INTO task_watch_routes (
                task_id, trigger_type, trigger_key, wake_status, reason,
                payload, active, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                task_id,
                trigger_type,
                trigger_key,
                wake_status,
                reason,
                json.dumps(payload, ensure_ascii=False) if payload else None,
                created_by,
                now,
            ),
        )
        route_id = int(cur.lastrowid)
        event_payload = {
            "route_id": route_id,
            "trigger_type": trigger_type,
            "trigger_key": trigger_key,
            "wake_status": wake_status,
            "reason": reason,
        }
        if payload:
            event_payload["payload"] = payload
        _append_event(conn, task_id, "watching", event_payload, run_id=run_id)
        row = conn.execute("SELECT * FROM task_watch_routes WHERE id = ?", (route_id,)).fetchone()
        return WatchRoute.from_row(row)


def _dependency_gated_wake_status(conn: sqlite3.Connection, task_id: str, requested: str) -> str:
    if requested != "ready":
        return requested
    undone = conn.execute(
        "SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') LIMIT 1",
        (task_id,),
    ).fetchone()
    return "todo" if undone else "ready"


def _watch_latest_inbound(
    route: WatchRoute,
    *,
    payload: Optional[dict],
    actor: Optional[str],
    triggered_at: int,
) -> dict[str, Any]:
    inbound = {
        "route_id": route.id,
        "trigger_type": route.trigger_type,
        "trigger_key": route.trigger_key,
        "actor": actor,
        "triggered_at": triggered_at,
        "payload": payload,
    }
    return inbound


def trigger_watch(
    conn: sqlite3.Connection,
    *,
    route_id: Optional[int] = None,
    task_id: Optional[str] = None,
    trigger_type: Optional[str] = None,
    trigger_key: Optional[str] = None,
    payload: Optional[dict] = None,
    actor: Optional[str] = None,
) -> list[WatchRoute]:
    """Wake active watch routes by id, task id, or trigger type/key."""
    payload = _normalize_watch_payload(payload, field="payload")
    where = ["active = 1"]
    params: list[Any] = []
    if route_id is not None:
        where.append("id = ?")
        params.append(int(route_id))
    if task_id is not None:
        where.append("task_id = ?")
        params.append(task_id)
    if trigger_type is not None:
        where.append("trigger_type = ?")
        params.append(_normalize_trigger_type(trigger_type))
    if trigger_key is not None:
        where.append("trigger_key = ?")
        params.append(_normalize_funnel_text(trigger_key))
    if route_id is None and task_id is None and trigger_type is None:
        raise ValueError("route_id, task_id, or trigger_type is required")
    now = int(time.time())
    triggered: list[WatchRoute] = []
    with write_txn(conn):
        rows = conn.execute(
            "SELECT * FROM task_watch_routes WHERE " + " AND ".join(where) + " ORDER BY id",
            tuple(params),
        ).fetchall()
        for row in rows:
            route = WatchRoute.from_row(row)
            new_status = _dependency_gated_wake_status(conn, route.task_id, route.wake_status)
            upd = conn.execute(
                "UPDATE task_watch_routes SET active = 0, triggered_at = ?, trigger_payload = ? "
                "WHERE id = ? AND active = 1",
                (
                    now,
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                    route.id,
                ),
            )
            if upd.rowcount != 1:
                continue
            task_row = conn.execute(
                "SELECT funnel_data FROM tasks WHERE id = ?",
                (route.task_id,),
            ).fetchone()
            funnel_data: dict[str, Any] = {}
            if task_row and task_row["funnel_data"]:
                try:
                    parsed_funnel = json.loads(task_row["funnel_data"])
                    if isinstance(parsed_funnel, dict):
                        funnel_data = parsed_funnel
                except Exception:
                    funnel_data = {}
            latest_inbound = _watch_latest_inbound(
                route,
                payload=payload,
                actor=actor,
                triggered_at=now,
            )
            funnel_data["substate"] = "triggered"
            funnel_data["last_meaningful_event"] = (
                f"watch_triggered:{route.trigger_type}"
                + (f":{route.trigger_key}" if route.trigger_key else "")
            )
            funnel_data["latest_inbound"] = latest_inbound
            if isinstance(funnel_data.get("conversation_state"), dict):
                funnel_data["conversation_state"] = {
                    **funnel_data["conversation_state"],
                    "latest_message": latest_inbound,
                }
            if isinstance(funnel_data.get("watch"), dict):
                funnel_data["watch"] = {**funnel_data["watch"], "active": False, "triggered_at": now}
            task_upd = conn.execute(
                "UPDATE tasks SET status = ?, current_run_id = NULL, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, funnel_data = ? "
                "WHERE id = ? AND status = 'watching'",
                (new_status, json.dumps(funnel_data, ensure_ascii=False), route.task_id),
            )
            event_payload = {
                "route_id": route.id,
                "trigger_type": route.trigger_type,
                "trigger_key": route.trigger_key,
                "wake_status": new_status,
                "actor": actor,
                "task_status_changed": bool(task_upd.rowcount),
            }
            if payload:
                event_payload["payload"] = payload
            _append_event(conn, route.task_id, "watch_triggered", event_payload)
            if new_status == "blocked" and task_upd.rowcount:
                blocked_payload = {
                    "reason": route.reason,
                    "route_id": route.id,
                    "trigger_type": route.trigger_type,
                    "trigger_key": route.trigger_key,
                    "actor": actor,
                    "wake_status": new_status,
                    "triggered_at": now,
                }
                if payload:
                    blocked_payload["payload"] = payload
                _append_event(conn, route.task_id, "blocked", blocked_payload)
            triggered_row = conn.execute("SELECT * FROM task_watch_routes WHERE id = ?", (route.id,)).fetchone()
            triggered.append(WatchRoute.from_row(triggered_row))
    return triggered


def transition_task_stage(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    to_stage: str,
    evidence: Optional[Any] = None,
    action_key: Optional[str] = None,
    actor: Optional[str] = None,
    board: Optional[str] = None,
) -> Task:
    """Move a card to another semantic workflow stage after evidence validation."""
    task = get_task(conn, task_id)
    if task is None:
        raise ValueError(f"unknown task: {task_id}")
    if task.status in {"done", "archived"}:
        raise ValueError(
            "cannot transition terminal task; create or transition a child card "
            "for the next semantic stage"
        )
    to_stage = _normalize_funnel_text(to_stage) or ""
    if not to_stage:
        raise ValueError("to_stage is required")
    workflow = read_board_metadata(board).get("workflow")
    stages = _workflow_stage_map(workflow if isinstance(workflow, dict) else None)
    if stages and to_stage not in stages:
        raise ValueError(f"target stage {to_stage!r} is not defined in board workflow")
    current_stage = task.stage_key
    required: list[str] = []
    if stages and current_stage in stages:
        exits = stages[current_stage].get("exit_criteria") or []
        matching = [e for e in exits if isinstance(e, dict) and e.get("transition") == to_stage]
        if not matching:
            raise ValueError(f"workflow has no transition from {current_stage!r} to {to_stage!r}")
        required = list(matching[0].get("evidence_required") or [])
    provided = _evidence_keys(evidence)
    state = _funnel_transition_state(task.funnel_data)
    merged_evidence = {str(e) for e in state["evidence"]} | provided
    missing = [item for item in required if item not in merged_evidence]
    if missing:
        raise ValueError("missing transition evidence: " + ", ".join(missing))
    next_action = _normalize_funnel_text(action_key)
    _validate_task_against_workflow(
        board,
        goal_id=task.goal_id,
        workstream_id=task.workstream_id,
        stage_key=to_stage,
        action_key=next_action,
        lifecycle_status=task.status,
    )
    next_data = state["data"]
    if merged_evidence:
        next_data["transition_evidence"] = sorted(merged_evidence)
    next_data["missing_evidence"] = []
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET stage_key = ?, action_key = ?, funnel_data = ? WHERE id = ?",
            (
                to_stage,
                next_action,
                json.dumps(next_data, ensure_ascii=False) if next_data else None,
                task_id,
            ),
        )
        _append_event(
            conn,
            task_id,
            "stage_transitioned",
            {
                "from_stage": current_stage,
                "to_stage": to_stage,
                "action_key": next_action,
                "evidence": sorted(provided),
                "actor": actor,
            },
        )
    updated = get_task(conn, task_id)
    if updated is None:
        raise ValueError(f"unknown task after transition: {task_id}")
    return updated


def specify_triage_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    author: Optional[str] = None,
) -> bool:
    """Flesh out a triage task and promote it to ``todo``.

    Atomically updates ``title`` / ``body`` / ``assignee`` (when provided)
    and transitions ``status: triage -> todo`` in a single write txn. Returns
    False when the task is missing or not in the ``triage`` column — callers
    should surface that as "nothing to specify" rather than an error.

    ``todo`` (not ``ready``) is the correct landing column: ``recompute_ready``
    promotes parent-free / parent-done todos to ``ready`` on the next
    dispatcher tick, which keeps the normal parent-gating behaviour intact
    for specified tasks that happen to have open parents.

    ``author`` is recorded on an audit comment only when at least one of
    ``title`` / ``body`` / ``assignee`` actually changed — avoids noisy
    comment spam for status-only promotions.
    """
    if title is not None and not title.strip():
        raise ValueError("title cannot be blank")
    assignee = _canonical_assignee(assignee)
    with write_txn(conn):
        existing = conn.execute(
            "SELECT title, body, assignee FROM tasks WHERE id = ? AND status = 'triage'",
            (task_id,),
        ).fetchone()
        if existing is None:
            return False
        sets: list[str] = ["status = 'todo'"]
        params: list[Any] = []
        changed_fields: list[str] = []
        if title is not None and title.strip() != (existing["title"] or ""):
            sets.append("title = ?")
            params.append(title.strip())
            changed_fields.append("title")
        if body is not None and (body or "") != (existing["body"] or ""):
            sets.append("body = ?")
            params.append(body)
            changed_fields.append("body")
        if assignee is not None and assignee != (existing["assignee"] or None):
            sets.append("assignee = ?")
            params.append(assignee)
            changed_fields.append("assignee")
        params.append(task_id)
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE id = ? AND status = 'triage'",
            tuple(params),
        )
        if cur.rowcount != 1:
            return False
        if changed_fields and author and author.strip():
            # Inline INSERT (rather than ``add_comment``) because we're
            # already inside this function's write_txn — nested BEGIN
            # IMMEDIATE would raise OperationalError. We also skip the
            # 'commented' event that ``add_comment`` emits, since the
            # 'specified' event below already records the change.
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    author.strip(),
                    "Specified — updated "
                    + ", ".join(changed_fields)
                    + " and promoted to todo.",
                    int(time.time()),
                ),
            )
        _append_event(
            conn,
            task_id,
            "specified",
            {"changed_fields": changed_fields} if changed_fields else None,
        )
    # Outside the write_txn above, so we don't nest BEGIN IMMEDIATE — the
    # ready-promotion pass opens its own IMMEDIATE txn. This runs the same
    # logic the dispatcher would on its next tick, so a specified task
    # with no open parents flips straight to 'ready' here instead of
    # idling in 'todo' until the next sweep.
    recompute_ready(conn)
    return True


def decompose_triage_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    root_assignee: Optional[str],
    children: list[dict],
    author: Optional[str] = None,
    auto_promote: bool = True,
) -> Optional[list[str]]:
    """Fan a triage task out into child tasks and promote the root to ``todo``.

    The root task stays alive and becomes the parent of every child —
    when all children reach ``done``, the root promotes to ``ready`` and
    its assignee (typically the orchestrator profile) wakes back up to
    judge completion or spawn more work.

    ``children`` is a list of dicts, each shaped like::

        {
            "title": "...",
            "body": "...",                     # optional
            "assignee": "profile-name",        # optional, None -> default fallback
            "parents": [0, 2],                 # indices into this same children list
        }

    Returns the list of created child task ids (in input order) on
    success. Returns ``None`` when:
      - The root task does not exist
      - The root task is not in ``triage``
      - A cycle would result (caller built a bad graph)

    Validation of titles/assignees happens inside the same write_txn as
    the inserts so a malformed entry aborts the whole decomposition
    cleanly (no orphan children).
    """
    if not children:
        return None
    if root_assignee is not None:
        root_assignee = _canonical_assignee(root_assignee)

    # Pre-validate the children list shape outside the txn. Cheap checks
    # that don't need DB access. Bad input aborts before we touch the DB.
    for idx, child in enumerate(children):
        if not isinstance(child, dict):
            raise ValueError(f"child[{idx}] is not a dict")
        title = child.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"child[{idx}].title is required")
        parents_idx = child.get("parents") or []
        if not isinstance(parents_idx, list):
            raise ValueError(f"child[{idx}].parents must be a list")
        for p in parents_idx:
            if not isinstance(p, int) or p < 0 or p >= len(children):
                raise ValueError(
                    f"child[{idx}].parents[{p}] is not a valid index into children"
                )
            if p == idx:
                raise ValueError(f"child[{idx}] cannot list itself as a parent")

    # Detect cycles in the sibling parent graph (Kahn's topological sort).
    # link_tasks() calls _would_cycle() for every new edge; here we check
    # the entire sibling graph before touching the DB.  A cycle silently
    # deadlocks every involved child in 'todo' because recompute_ready()
    # can never promote them.
    _in_deg = [0] * len(children)
    _adj: list[list[int]] = [[] for _ in range(len(children))]
    for _i, _c in enumerate(children):
        for _p in (_c.get("parents") or []):
            _adj[_p].append(_i)
            _in_deg[_i] += 1
    _queue = [_i for _i in range(len(children)) if _in_deg[_i] == 0]
    _seen = 0
    while _queue:
        _node = _queue.pop()
        _seen += 1
        for _nb in _adj[_node]:
            _in_deg[_nb] -= 1
            if _in_deg[_nb] == 0:
                _queue.append(_nb)
    if _seen != len(children):
        raise ValueError("cyclic dependency detected in decomposed children list")

    # We do the full decomposition in a SINGLE write_txn so it's
    # atomic: either every child is created AND the root flips to
    # ``todo``, or nothing changes. We deliberately do NOT call any
    # kb helper that opens its own write_txn (create_task, link_tasks,
    # add_comment) from inside this block — see architecture.md
    # write_txn pitfalls. Instead we inline the INSERTs and
    # _append_event calls.
    now = int(time.time())
    child_ids: list[str] = []
    with write_txn(conn):
        root_row = conn.execute(
            "SELECT id, status, tenant FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if root_row is None:
            return None
        if root_row["status"] != "triage":
            return None
        tenant = root_row["tenant"]

        # Create children. Status is 'todo' regardless of parents — we
        # link them under the root AFTER creation so the dispatcher
        # sees a coherent state, and recompute_ready() at the end
        # promotes parent-free children to 'ready'.
        for idx, child in enumerate(children):
            new_id = _new_task_id()
            title = child["title"].strip()
            body = child.get("body")
            assignee = _canonical_assignee(child.get("assignee"))
            conn.execute(
                "INSERT INTO tasks "
                "(id, title, body, assignee, status, workspace_kind, "
                " tenant, created_at, created_by) "
                "VALUES (?, ?, ?, ?, 'todo', 'scratch', ?, ?, ?)",
                (
                    new_id,
                    title,
                    body if isinstance(body, str) else None,
                    assignee,
                    tenant,
                    now,
                    (author or "decomposer"),
                ),
            )
            _append_event(
                conn, new_id, "created",
                {"by": author or "decomposer", "from_decompose_of": task_id},
            )
            child_ids.append(new_id)

        # Link children to their sibling parents (within the decomposed graph).
        for idx, child in enumerate(children):
            for p_idx in child.get("parents") or []:
                parent_id = child_ids[p_idx]
                child_id = child_ids[idx]
                conn.execute(
                    "INSERT OR IGNORE INTO task_links (parent_id, child_id) "
                    "VALUES (?, ?)",
                    (parent_id, child_id),
                )
                _append_event(
                    conn, child_id, "linked",
                    {"parent": parent_id, "child": child_id},
                )

        # Link the ROOT task as a child of every leaf child — i.e. the
        # root waits for the whole graph. Simpler than computing leaves:
        # link root under every child. Cycle-free because the root is
        # only ever a child here, never a parent of children.
        for cid in child_ids:
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) "
                "VALUES (?, ?)",
                (cid, task_id),
            )

        # Flip the root: triage -> todo, set assignee to the orchestrator.
        sets = ["status = 'todo'"]
        params: list[Any] = []
        if root_assignee is not None:
            sets.append("assignee = ?")
            params.append(root_assignee)
        params.append(task_id)
        conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )

        # Audit comment + event on the root so the timeline shows the fan-out.
        if author and author.strip():
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    author.strip(),
                    "Decomposed into "
                    + ", ".join(child_ids)
                    + ". Root will wake when all children complete.",
                    now,
                ),
            )
        _append_event(
            conn, task_id, "decomposed",
            {
                "child_ids": child_ids,
                "root_assignee": root_assignee,
            },
        )

    # Outside the write_txn: promote parent-free children to 'ready'
    # so the dispatcher picks them up on its next tick. Same pattern
    # specify_triage_task uses.  When auto_promote is False children
    # stay in 'todo' until the user manually promotes them — useful
    # for manual-review-first workflows.
    if auto_promote:
        recompute_ready(conn)
    return child_ids


def archive_task(conn: sqlite3.Connection, task_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'archived', "
            "    claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status != 'archived'",
            (task_id,),
        )
        if cur.rowcount != 1:
            return False
        # If archive happened while a run was still in flight (e.g. user
        # archived a running task from the dashboard), close that run with
        # outcome='reclaimed' so attempt history isn't orphaned.
        run_id = _end_run(
            conn, task_id,
            outcome="reclaimed", status="reclaimed",
            summary="task archived with run still active",
        )
        _append_event(conn, task_id, "archived", None, run_id=run_id)
    # ``archived`` parents no longer block children, same as ``done``.
    # Promote newly-unblocked dependents immediately instead of waiting
    # for a later dispatcher tick.
    recompute_ready(conn)
    return True


def delete_archived_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Permanently remove an already-archived task and its related rows.

    Safety guard: only archived tasks can be deleted. Active / blocked / done
    tasks must be explicitly archived first so accidental data loss requires a
    second deliberate action.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row or row["status"] != "archived":
            return False
        conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? OR child_id = ?",
            (task_id, task_id),
        )
        conn.execute("DELETE FROM task_comments WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ?", (task_id,))
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount == 1


def delete_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Hard-delete a task and cascade to all related rows.

    Because the schema does not use ``ON DELETE CASCADE`` foreign keys,
    we explicitly delete from child tables first, then the task row.
    This keeps the operation atomic (single ``write_txn``).

    Returns ``True`` if the task existed and was deleted, ``False``
    if the task was not found.
    """
    with write_txn(conn):
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount != 1:
            return False
        conn.execute("DELETE FROM task_links WHERE parent_id = ? OR child_id = ?", (task_id, task_id))
        conn.execute("DELETE FROM task_comments WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ?", (task_id,))
    recompute_ready(conn)
    return True


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------

def resolve_workspace(task: Task, *, board: Optional[str] = None) -> Path:
    """Resolve (and create if needed) the workspace for a task.

    - ``scratch``: a fresh dir under ``<board-root>/workspaces/<id>/``,
      where ``<board-root>`` is the active board's root. The path is the
      same for the dispatcher and every profile worker, so handoff is
      path-stable.
    - ``dir:<path>``: the path stored in ``workspace_path``.  Created
      if missing.  MUST be absolute — relative paths are rejected to
      prevent confused-deputy traversal where ``../../../tmp/attacker``
      resolves against the dispatcher's CWD instead of a meaningful
      root.  Users who want a kanban-root-relative workspace should
      compute the absolute path themselves.
    - ``worktree``: a git worktree at ``workspace_path``.  Not created
      automatically in v1 -- the kanban-worker skill documents
      ``git worktree add`` as a worker-side step.  Returns the intended path.

    Persist the resolved path back to the task row via ``set_workspace_path``
    so subsequent runs reuse the same directory.
    """
    kind = task.workspace_kind or "scratch"
    if kind == "scratch":
        if task.workspace_path:
            # Legacy scratch tasks that were set to an explicit path get the
            # same absolute-path guard as dir: — consistent with the
            # threat model.
            p = Path(task.workspace_path).expanduser()
            if not p.is_absolute():
                raise ValueError(
                    f"task {task.id} has non-absolute workspace_path "
                    f"{task.workspace_path!r}; workspace paths must be absolute"
                )
        else:
            p = workspaces_root(board=board) / task.id
        p.mkdir(parents=True, exist_ok=True)
        return p
    if kind == "dir":
        if not task.workspace_path:
            raise ValueError(
                f"task {task.id} has workspace_kind=dir but no workspace_path"
            )
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute workspace_path "
                f"{task.workspace_path!r}; use an absolute path "
                f"(relative paths are ambiguous against the dispatcher's CWD)"
            )
        p.mkdir(parents=True, exist_ok=True)
        return p
    if kind == "worktree":
        if not task.workspace_path:
            # Default: .worktrees/<id>/ under CWD.  Worker skill creates it.
            return Path.cwd() / ".worktrees" / task.id
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute worktree path "
                f"{task.workspace_path!r}; use an absolute path"
            )
        return p
    raise ValueError(f"unknown workspace_kind: {kind}")


def set_workspace_path(
    conn: sqlite3.Connection, task_id: str, path: Path | str
) -> None:
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(path), task_id),
        )


# ---------------------------------------------------------------------------
def schedule_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Park a task in ``scheduled`` so it is waiting on time, not human input.

    ``scheduled`` tasks are intentionally not dispatchable; an external cron,
    human action, or automation can later call ``unblock_task`` to re-gate them
    to ``ready`` (or ``todo`` if parents are still incomplete).
    """
    with write_txn(conn):
        params: list[Any] = [task_id]
        sql = """
            UPDATE tasks
               SET status       = 'scheduled',
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL
             WHERE id = ?
               AND status IN ('todo', 'ready', 'running', 'blocked')
        """
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(int(expected_run_id))
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="scheduled", status="scheduled",
            summary=reason,
        )
        if run_id is None and reason:
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="scheduled",
                summary=reason,
            )
        _append_event(conn, task_id, "scheduled", {"reason": reason}, run_id=run_id)
        return True


# Dispatcher (one-shot pass)
# ---------------------------------------------------------------------------

# After this many consecutive non-success attempts on a task/profile, the
# dispatcher stops retrying and parks the task in ``blocked`` with a reason so
# a human can investigate. Prevents retry storms when a worker repeatedly times
# out, crashes, or cannot spawn.
DEFAULT_FAILURE_LIMIT = 2
# Legacy alias — callers / tests still reference the old name.
DEFAULT_SPAWN_FAILURE_LIMIT = DEFAULT_FAILURE_LIMIT

# Max bytes to keep in a single worker log file. The dispatcher truncates
# and rotates on spawn if the file is larger than this at spawn time.
DEFAULT_LOG_ROTATE_BYTES = 2 * 1024 * 1024   # 2 MiB
DEFAULT_LOG_BACKUP_COUNT = 1

# Keep a little wall-clock budget for the worker to observe a terminal timeout
# and call kanban_block/kanban_complete before max_runtime_seconds kills it.
KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS = 30

# ---------------------------------------------------------------------------
# Respawn guard constants
# ---------------------------------------------------------------------------

# Patterns in last_failure_error that indicate a quota / auth blocker.
# These errors won't resolve by retrying immediately — auto-block instead.
_RESPAWN_BLOCKER_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit|429|403|auth\w*|"
    r"unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|"
    r"invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

# Within this window a completed run counts as "recent proof"; don't re-spawn.
_RESPAWN_GUARD_SUCCESS_WINDOW = 3600  # 1 hour

# Within this window a GitHub PR URL in a comment blocks re-spawn.
_RESPAWN_GUARD_PR_WINDOW = 86400  # 24 hours

# Pattern matching a GitHub PR URL in task comments.
_RESPAWN_GUARD_PR_URL_RE = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+",
    re.IGNORECASE,
)


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch`` pass."""

    reclaimed: int = 0
    promoted: int = 0
    spawned: list[tuple[str, str, str]] = field(default_factory=list)
    """List of ``(task_id, assignee, workspace_path)`` triples."""
    skipped_unassigned: list[str] = field(default_factory=list)
    """Ready task ids skipped because they have no assignee at all.
    Operator-actionable — usually a misfiled task waiting for routing."""
    skipped_nonspawnable: list[str] = field(default_factory=list)
    """Ready task ids skipped because their assignee names a control-plane
    lane (a Claude Code terminal like ``orion-cc``) rather than a Hermes
    profile. Expected steady-state on multi-lane setups; NOT an
    operator-actionable failure. Tracked separately so health telemetry
    can distinguish "real stuck" (nothing spawned but spawnable work
    available) from "correctly idle" (nothing spawnable in the queue)."""
    crashed: list[str] = field(default_factory=list)
    """Task ids reclaimed because their worker PID disappeared."""
    auto_blocked: list[str] = field(default_factory=list)
    """Task ids auto-blocked by the spawn-failure circuit breaker."""
    timed_out: list[str] = field(default_factory=list)
    """Task ids whose workers exceeded ``max_runtime_seconds``."""
    stale: list[str] = field(default_factory=list)
    """Task ids reclaimed because no progress (heartbeat) was seen
    within ``dispatch_stale_timeout_seconds``."""
    respawn_guarded: list[tuple[str, str]] = field(default_factory=list)
    """Tasks skipped by the respawn guard, as ``(task_id, reason)`` pairs.

    Reasons: ``"blocker_auth"`` (quota/auth error — also auto-blocked),
    ``"recent_success"`` (completed run within guard window),
    ``"active_pr"`` (GitHub PR URL in a recent comment)."""
    contract_blocked: list[tuple[str, list[str]]] = field(default_factory=list)
    """Tasks rejected by the board operating contract before claim/spawn.

    Stored as ``(task_id, blocker_codes)`` so dispatch telemetry and dry-run
    output can show contract failures without requiring DB event inspection.
    """


# Bounded registry of recently-reaped worker child exits, populated by the
# reap loop at the top of ``dispatch_once`` and consulted by
# ``detect_crashed_workers`` to classify a dead-pid task.
#
# Entry: ``pid -> (raw_wait_status, reaped_at_epoch)``. We keep raw status
# so both ``os.WIFEXITED`` / ``os.WEXITSTATUS`` and ``os.WIFSIGNALED`` can
# be consulted. Entries are trimmed by age (and total size cap as a
# belt-and-braces against unbounded growth on exotic platforms).
_RECENT_WORKER_EXIT_TTL_SECONDS = 600
_RECENT_WORKER_EXITS_MAX = 4096
_recent_worker_exits: "dict[int, tuple[int, float]]" = {}


def _record_worker_exit(pid: int, raw_status: int) -> None:
    """Record a reaped child's exit status for later classification.

    Called from the reap loop in ``dispatch_once``. Safe to call many
    times; duplicate pids overwrite (pids can cycle, latest wins).
    """
    if not pid or pid <= 0:
        return
    now = time.time()
    _recent_worker_exits[int(pid)] = (int(raw_status), now)
    # Age-based trim: drop entries older than the TTL.
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX // 2:
        cutoff = now - _RECENT_WORKER_EXIT_TTL_SECONDS
        for _pid in [p for p, (_s, t) in _recent_worker_exits.items() if t < cutoff]:
            _recent_worker_exits.pop(_pid, None)
    # Size cap as a final guard.
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX:
        # Drop oldest half.
        ordered = sorted(_recent_worker_exits.items(), key=lambda kv: kv[1][1])
        for _pid, _ in ordered[: len(ordered) // 2]:
            _recent_worker_exits.pop(_pid, None)


def _classify_worker_exit(pid: int) -> "tuple[str, Optional[int]]":
    """Classify a recently-reaped worker by pid.

    Returns ``(kind, code)`` where ``kind`` is one of:

    * ``"clean_exit"`` — ``WIFEXITED`` with ``WEXITSTATUS == 0``. When the
      task is still ``running`` in the DB, this is a protocol violation
      (worker exited without calling ``kanban_complete`` / ``kanban_block``)
      and should be auto-blocked immediately — retrying will just loop.
    * ``"nonzero_exit"`` — ``WIFEXITED`` with non-zero status. Real error.
    * ``"signaled"`` — ``WIFSIGNALED`` (OOM killer, SIGKILL, etc). Real crash.
    * ``"unknown"`` — pid was not in the reap registry (either reaped by
      something else, or died between reap tick and liveness check). Fall
      back to existing crashed-counter behavior.

    ``code`` is the exit status (for ``clean_exit`` / ``nonzero_exit``) or
    the signal number (for ``signaled``), or ``None`` for ``unknown``.
    """
    entry = _recent_worker_exits.get(int(pid))
    if entry is None:
        return ("unknown", None)
    raw, _ = entry
    try:
        if os.WIFEXITED(raw):
            code = os.WEXITSTATUS(raw)
            if code == 0:
                return ("clean_exit", 0)
            return ("nonzero_exit", code)
        if os.WIFSIGNALED(raw):
            return ("signaled", os.WTERMSIG(raw))
    except Exception:
        pass
    return ("unknown", None)


def reap_worker_zombies() -> "list[int]":
    """Reap all zombie children of this process without blocking.

    Returns the list of reaped PIDs. Safe to call when there are no
    children (returns []). No-op on Windows.
    """
    reaped: "list[int]" = []
    if os.name != "nt":
        try:
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
                _record_worker_exit(pid, status)
                reaped.append(pid)
        except Exception:
            pass
    return reaped


def _pid_alive(pid: Optional[int]) -> bool:
    """Return True if ``pid`` is still running on this host.

    Cross-platform: uses ``OpenProcess`` + ``WaitForSingleObject`` on
    Windows (via ``gateway.status._pid_exists``) and ``os.kill(pid, 0)``
    on POSIX. Returns False for falsy PIDs or on any OS error.

    **DO NOT** use ``os.kill(pid, 0)`` directly on Windows — Python's
    Windows ``os.kill`` treats ``sig=0`` as ``CTRL_C_EVENT`` (bpo-14484)
    and will broadcast it to the target's console group, potentially
    killing unrelated processes.

    **Zombie handling:** the existence check succeeds against zombie
    processes (post-exit, pre-reap) because the process table entry
    still exists. A worker that exits without being reaped by its
    parent would stay "alive" to the dispatcher forever. Dispatcher
    workers are started via ``start_new_session=True`` + intentional
    Popen handle abandonment, so init reaps them quickly — but during
    the window between exit and reap, we'd otherwise see stale "alive"
    signals. On Linux we peek at ``/proc/<pid>/status`` and treat
    ``State: Z`` as dead. On macOS we ask ``ps`` for the BSD ``stat``
    field and treat values containing ``Z`` as dead.
    """
    if not pid or pid <= 0:
        return False
    from gateway.status import _pid_exists
    if not _pid_exists(int(pid)):
        return False
    # Still here → process exists. Check for zombie on platforms
    # where we have a cheap, deterministic process-state probe.
    if sys.platform == "linux":
        try:
            with open(f"/proc/{int(pid)}/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        # "State:\tZ (zombie)" → dead
                        if "Z" in line.split(":", 1)[1]:
                            return False
                        break
        except (FileNotFoundError, PermissionError, OSError):
            # proc entry gone → already reaped; treat as dead.
            # PermissionError shouldn't happen for our own children but
            # be defensive.
            pass
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(int(pid))],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1,
                check=False,
            )
            if proc.returncode != 0:
                return False
            if "Z" in (proc.stdout or "").strip():
                return False
        except (OSError, subprocess.SubprocessError, TimeoutError):
            # If the secondary probe fails, keep the kill(0) answer.
            pass
    return True


def _terminate_reclaimed_worker(
    pid: Optional[int],
    claim_lock: Optional[str],
    *,
    signal_fn=None,
) -> dict[str, Any]:
    """Best-effort host-local worker termination for reclaim paths."""
    import signal

    info: dict[str, Any] = {
        "prev_pid": int(pid) if pid else None,
        "host_local": False,
        "termination_attempted": False,
        "terminated": False,
        "sigkill": False,
    }
    if not pid or pid <= 0 or not claim_lock:
        return info

    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    if not str(claim_lock).startswith(host_prefix):
        return info
    info["host_local"] = True

    kill = signal_fn if signal_fn is not None else (
        os.kill if hasattr(os, "kill") else None
    )
    if kill is None:
        return info

    info["termination_attempted"] = True
    try:
        kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return info

    for _ in range(10):
        if not _pid_alive(pid):
            info["terminated"] = True
            return info
        time.sleep(0.5)

    if _pid_alive(pid):
        try:
            # signal.SIGKILL doesn't exist on Windows; fall back to SIGTERM
            # (which maps to TerminateProcess via the stdlib shim).
            _sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            kill(int(pid), _sigkill)
            info["sigkill"] = True
        except (ProcessLookupError, OSError):
            return info

    info["terminated"] = not _pid_alive(pid)
    return info


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Record a ``heartbeat`` event + touch ``last_heartbeat_at``.

    Called by long-running workers as a liveness signal orthogonal to
    the PID check. A worker that forks a long-lived child (train loop,
    video encode, web crawl) can have its Python still alive while the
    actual work process is stuck; periodic heartbeats catch that.

    Returns True on success, False if the task is not in a state that
    should be heartbeating (not running, or claim expired).
    """
    now = int(time.time())
    with write_txn(conn):
        if expected_run_id is None:
            cur = conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? "
                "WHERE id = ? AND status = 'running'",
                (now, task_id),
            )
        else:
            cur = conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? "
                "WHERE id = ? AND status = 'running' AND current_run_id = ?",
                (now, task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        run_id = (
            int(expected_run_id)
            if expected_run_id is not None
            else _current_run_id(conn, task_id)
        )
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?",
                (now, run_id),
            )
        _append_event(
            conn, task_id, "heartbeat",
            {"note": note} if note else None,
            run_id=run_id,
        )
    return True


def enforce_max_runtime(
    conn: sqlite3.Connection,
    *,
    signal_fn=None,
) -> list[str]:
    """Terminate workers whose per-task ``max_runtime_seconds`` has elapsed.

    Sends SIGTERM, waits a short grace window, then SIGKILL. Emits a
    ``timed_out`` event and drops the task back to ``ready`` so the next
    dispatcher tick re-spawns it — unless the spawn-failure circuit
    breaker has already given up, in which case the task stays blocked
    where ``_record_spawn_failure`` parked it.

    Runs host-local: only tasks claimed by this host are candidates
    (same reasoning as ``detect_crashed_workers``). ``signal_fn`` is a
    test hook; defaults to ``os.kill`` on POSIX.
    """
    import signal
    timed_out: list[str] = []
    now = int(time.time())
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.max_runtime_seconds, t.claim_lock "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.max_runtime_seconds IS NOT NULL "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        # Runtime is per attempt, not lifetime-of-task. ``tasks.started_at``
        # intentionally records the first time a task ever started, so retries
        # must be measured from the active task_runs row when present.
        elapsed = now - int(row["active_started_at"])
        if elapsed < int(row["max_runtime_seconds"]):
            continue

        pid = int(row["worker_pid"])
        tid = row["id"]
        # SIGTERM then SIGKILL. Keep it simple: 5 s grace. Workers that
        # want a cleaner shutdown can install their own SIGTERM handler
        # before the grace expires.
        killed = False
        kill = signal_fn if signal_fn is not None else (
            os.kill if hasattr(os, "kill") else None
        )
        if kill is not None:
            try:
                kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            # Short polling wait — no time.sleep on the write txn.
            for _ in range(10):
                if not _pid_alive(pid):
                    break
                time.sleep(0.5)
            if _pid_alive(pid):
                try:
                    # signal.SIGKILL doesn't exist on Windows.
                    _sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
                    kill(pid, _sigkill)
                    killed = True
                except (ProcessLookupError, OSError):
                    pass

        with write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running'",
                (tid,),
            )
            if cur.rowcount == 1:
                payload = {
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": int(row["max_runtime_seconds"]),
                    "sigkill": killed,
                }
                run_id = _end_run(
                    conn, tid,
                    outcome="timed_out", status="timed_out",
                    error=f"elapsed {int(elapsed)}s > limit {int(row['max_runtime_seconds'])}s",
                    metadata=payload,
                )
                _append_event(
                    conn, tid, "timed_out", payload, run_id=run_id,
                )
                timed_out.append(tid)
        # Increment the unified failure counter. Outside the write_txn
        # above because ``_record_task_failure`` opens its own. If the
        # breaker trips, this flips the task ``ready → blocked`` and
        # emits a ``gave_up`` event on top of the ``timed_out`` we
        # already emitted.
        if cur.rowcount == 1:
            _record_task_failure(
                conn, tid,
                error=f"elapsed {int(elapsed)}s > limit {int(row['max_runtime_seconds'])}s",
                outcome="timed_out",
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "sigkill": killed},
            )
    return timed_out


# Heartbeat staleness heartbeat gap — if a running task hasn't sent a
# heartbeat in this many seconds it's considered inactive regardless of
# the ``dispatch_stale_timeout_seconds`` threshold.  Hardcoded at 1 hour
# to match the original spec (">4h started + no commits in 1h").
_STALE_HEARTBEAT_GAP_SECONDS = 3600


def detect_stale_running(
    conn: sqlite3.Connection,
    *,
    stale_timeout_seconds: int = 0,
    signal_fn=None,
) -> list[str]:
    """Reclaim ``running`` tasks that show no progress (heartbeat) within the
    staleness window.

    A task is considered stale when BOTH of these hold:

    1. It has been running for longer than ``stale_timeout_seconds``
       (measured from the active run's ``started_at``, falling back to
       ``tasks.started_at`` on older runs).
    2. Its ``last_heartbeat_at`` is older than
       ``_STALE_HEARTBEAT_GAP_SECONDS`` (or NULL — never sent a heartbeat).

    On reclaim the task is reset to ``ready``, the run is closed with
    ``outcome='stale'``, and the host-local worker (if still running) is
    terminated.

    Only considers ``status='running'`` tasks. Blocked tasks are never
    candidates.  Returns the list of reclaimed task IDs.

    ``stale_timeout_seconds=0`` disables the check entirely (returns ``[]``
    immediately).  ``signal_fn`` is a test hook; defaults to ``os.kill``
    on POSIX.
    """
    if stale_timeout_seconds <= 0:
        return []

    import signal as _signal_mod

    now = int(time.time())
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    reclaimed: list[str] = []

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.last_heartbeat_at, t.claim_lock, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running'"
    ).fetchall()

    for row in rows:
        # Skip if no started_at (shouldn't happen for running, but be safe).
        if row["active_started_at"] is None:
            continue

        elapsed = now - int(row["active_started_at"])
        if elapsed < stale_timeout_seconds:
            continue  # not old enough to check

        last_hb = row["last_heartbeat_at"]
        hb_age = (now - int(last_hb)) if last_hb is not None else None
        if hb_age is not None and hb_age < _STALE_HEARTBEAT_GAP_SECONDS:
            continue  # recent heartbeat → still alive

        pid = row["worker_pid"]
        tid = row["id"]
        lock = row["claim_lock"] or ""

        # Terminate the worker if it's still host-local.
        termination = _terminate_reclaimed_worker(
            pid, lock, signal_fn=signal_fn,
        )

        with write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running'",
                (tid,),
            )
            if cur.rowcount != 1:
                continue

            payload = {
                "elapsed_seconds": int(elapsed),
                "last_heartbeat_at": (
                    int(last_hb) if last_hb is not None else None
                ),
                "heartbeat_age_seconds": (
                    int(hb_age) if hb_age is not None else None
                ),
                "timeout_seconds": stale_timeout_seconds,
                "pid": int(pid) if pid else None,
            }
            payload.update(termination)

            run_id = _end_run(
                conn, tid,
                outcome="stale", status="stale",
                error=(
                    f"no heartbeat for {int(hb_age)}s "
                    if hb_age is not None
                    else "no heartbeat ever"
                ) + f" after {int(elapsed)}s running",
                metadata=payload,
            )
            _append_event(
                conn, tid, "stale", payload, run_id=run_id,
            )
            reclaimed.append(tid)

        # Intentionally NOT calling _record_task_failure here. Stale reclaim
        # is dispatcher-side detection of an absent heartbeat; the task is
        # going straight back to ``ready`` for re-dispatch. Counting it as
        # a worker failure would let two legitimately-long-running tasks
        # (>4h without explicit heartbeat) trip the circuit breaker and
        # auto-block, even though no worker actually failed. The 'stale'
        # event already lives in task_events for auditability; that's the
        # right surface for "this happened" without conflating with the
        # spawn_failed / timed_out / crashed counters.

    return reclaimed


def set_max_runtime(
    conn: sqlite3.Connection,
    task_id: str,
    seconds: Optional[int],
) -> bool:
    """Set or clear the per-task max_runtime_seconds. Returns True on
    success."""
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET max_runtime_seconds = ? WHERE id = ?",
            (int(seconds) if seconds is not None else None, task_id),
        )
    return cur.rowcount == 1


def _error_fingerprint(error_text: str) -> str:
    """Normalize an error message for grouping identical failures.

    Strips host-specific details (PIDs, timestamps) so that errors
    with the same root cause produce the same fingerprint.
    """
    fp = re.sub(r'\bpid \d+\b', 'pid N', error_text[:80])
    fp = re.sub(r'\b\d{10,}\b', '<TS>', fp)
    return fp.lower().strip()


def detect_crashed_workers(conn: sqlite3.Connection) -> list[str]:
    """Reclaim ``running`` tasks whose worker PID is no longer alive.

    Appends a ``crashed`` event and drops the task back to ``ready``.
    Different from ``release_stale_claims``: this checks liveness
    immediately rather than waiting for the claim TTL.

    Only considers tasks claimed by *this host* — PIDs from other hosts
    are meaningless here. The host-local check is enough because
    ``_default_spawn`` always runs the worker on the same host as the
    dispatcher (the whole design is single-host).

    When the reap registry shows the worker exited cleanly (rc=0) but
    the task was still ``running`` in the DB, treat it as a protocol
    violation (worker answered conversationally without calling
    ``kanban_complete`` / ``kanban_block``) and trip the circuit breaker
    on the first occurrence — retrying a worker whose CLI keeps
    returning 0 without a terminal transition just loops forever.
    """
    crashed: list[str] = []
    # Per-crash details collected inside the main txn, used after it
    # closes to run ``_record_task_failure`` (which needs its own
    # write_txn so can't nest). ``protocol_violation`` flags the
    # clean-exit-but-still-running case so we can trip the breaker
    # immediately instead of incrementing by 1.
    crash_details: list[tuple[str, int, str, bool, str]] = []
    # (task_id, pid, claimer, protocol_violation, error_text)
    with write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, claim_lock, started_at FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
        for row in rows:
            # Only check liveness for claims owned by this host.
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            # Skip liveness check inside the launch-window grace period
            # so a freshly-spawned worker isn't reclaimed before its PID
            # is visible on /proc.
            started_at = row["started_at"] if "started_at" in row.keys() else None
            if started_at is not None:
                grace = _resolve_crash_grace_seconds()
                if time.time() - started_at < grace:
                    continue
            if _pid_alive(row["worker_pid"]):
                continue

            pid = int(row["worker_pid"])
            kind, code = _classify_worker_exit(pid)
            if kind == "clean_exit":
                # Worker subprocess returned 0 but its task is still
                # ``running`` in the DB — it exited without calling
                # ``kanban_complete`` / ``kanban_block``. Retrying won't
                # help.
                protocol_violation = True
                error_text = (
                    "worker exited cleanly (rc=0) without calling "
                    "kanban_complete or kanban_block — protocol violation"
                )
                event_kind = "protocol_violation"
                event_payload = {
                    "pid": pid,
                    "claimer": row["claim_lock"],
                    "exit_code": code,
                }
            else:
                protocol_violation = False
                if kind == "nonzero_exit":
                    error_text = f"pid {pid} exited with code {code}"
                elif kind == "signaled":
                    error_text = f"pid {pid} killed by signal {code}"
                else:
                    error_text = f"pid {pid} not alive"
                event_kind = "crashed"
                event_payload = {"pid": pid, "claimer": row["claim_lock"]}
                if code is not None and kind != "unknown":
                    event_payload["exit_kind"] = kind
                    event_payload["exit_code"] = code

            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status = 'running'",
                (row["id"],),
            )
            if cur.rowcount == 1:
                run_id = _end_run(
                    conn, row["id"],
                    outcome="crashed", status="crashed",
                    error=error_text,
                    metadata=dict(event_payload),
                )
                _append_event(
                    conn, row["id"], event_kind,
                    event_payload,
                    run_id=run_id,
                )
                crashed.append(row["id"])
                crash_details.append(
                    (row["id"], pid, row["claim_lock"],
                     protocol_violation, error_text)
                )
    # Outside the main txn: increment the unified failure counter for
    # each crashed task. If the breaker trips, the task transitions
    # ready → blocked with a ``gave_up`` event on top of the ``crashed``
    # event we already emitted.
    #
    # Protocol-violation crashes force an immediate trip (failure_limit=1)
    # because clean-exit-without-transition is deterministic: the next
    # respawn will do exactly the same thing. Better to surface to a
    # human with a clear reason than to loop ``DEFAULT_FAILURE_LIMIT``
    # times first.
    auto_blocked: list[str] = []
    if crash_details:
        # Fingerprint errors to detect systemic failures.
        _fp_counts: dict[str, int] = {}
        for _, _, _, _, err_text in crash_details:
            fp = _error_fingerprint(err_text)
            _fp_counts[fp] = _fp_counts.get(fp, 0) + 1
        for tid, pid, claimer, protocol_violation, error_text in crash_details:
            fp = _error_fingerprint(error_text)
            is_systemic = (
                not protocol_violation
                and _fp_counts.get(fp, 0) >= 3
            )
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=1 if (protocol_violation or is_systemic) else None,
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "claimer": claimer},
            )
            if tripped:
                auto_blocked.append(tid)
    # Stash auto-blocked ids on the function for the dispatch loop to pick up.
    # Keeps the public return type (``list[str]``) stable for direct callers
    # and tests that destructure the result; ``dispatch_once`` reads this
    # side-channel attribute to populate ``DispatchResult.auto_blocked``.
    detect_crashed_workers._last_auto_blocked = auto_blocked  # type: ignore[attr-defined]
    return crashed


def _record_task_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    outcome: str,
    failure_limit: int = None,
    release_claim: bool = False,
    end_run: bool = False,
    event_payload_extra: Optional[dict] = None,
) -> bool:
    """Record a non-success outcome (spawn_failed / crashed / timed_out)
    and maybe trip the circuit breaker.

    Unified replacement for the old spawn-only ``_record_spawn_failure``.
    Every path that ends a task with a non-success outcome funnels
    through here so the ``consecutive_failures`` counter and the
    auto-block threshold stay consistent.

    Returns True when the task was auto-blocked (counter reached
    ``failure_limit``), False when it was just updated in place.

    Modes:

    * ``release_claim=True, end_run=True`` — spawn-failure path.
      Caller has a running task with an open run; this transitions
      it back to ``ready`` (or ``blocked`` when the breaker trips),
      releases the claim, and closes the run with ``outcome=<outcome>``.

    * ``release_claim=False, end_run=False`` — timeout/crash path.
      Caller has ALREADY flipped the task to ``ready`` and closed the
      run with the appropriate outcome. This just increments the
      counter; if the breaker trips, the task is re-transitioned
      ``ready → blocked`` and a ``gave_up`` event is emitted.

    ``event_payload_extra`` merges into the ``gave_up`` event payload
    when the breaker trips, so callers can include outcome-specific
    context (e.g. pid on crash, elapsed on timeout).

    Resolution order for the effective threshold:
      1. per-task ``max_retries`` if set (nothing else overrides)
      2. caller-supplied ``failure_limit`` (gateway passes the config
         value from ``kanban.failure_limit``; tests pass fixed values)
      3. ``DEFAULT_FAILURE_LIMIT``
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    blocked = False
    with write_txn(conn):
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        failures = int(row["consecutive_failures"]) + 1
        cur_status = row["status"]

        # Per-task override wins over both caller-supplied and default
        # thresholds. None (the common case) falls through.
        task_override = (
            row["max_retries"] if "max_retries" in row.keys() else None
        )
        if task_override is not None:
            effective_limit = int(task_override)
            limit_source = "task"
        else:
            effective_limit = int(failure_limit)
            limit_source = "dispatcher"

        if failures >= effective_limit:
            # Trip the breaker.
            if release_claim:
                # Spawn path: still running, also clear claim state.
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status IN ('running', 'ready')",
                    (failures, error[:500], task_id),
                )
            else:
                # Timeout/crash path: task is already at ``ready``
                # with claim cleared; just flip to blocked + update
                # counter fields.
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status IN ('ready', 'running')",
                    (failures, error[:500], task_id),
                )
            run_id = None
            if end_run:
                # Only the spawn path has an open run to close.
                run_id = _end_run(
                    conn, task_id,
                    outcome="gave_up", status="gave_up",
                    error=error[:500],
                    metadata={
                        "failures": failures,
                        "trigger_outcome": outcome,
                        "effective_limit": effective_limit,
                        "limit_source": limit_source,
                    },
                )
            payload = {
                "failures": failures,
                "effective_limit": effective_limit,
                "limit_source": limit_source,
                "error": error[:500],
                "trigger_outcome": outcome,
            }
            if event_payload_extra:
                payload.update(event_payload_extra)
            _append_event(
                conn, task_id, "gave_up", payload, run_id=run_id,
            )
            blocked = True
        else:
            # Below threshold.
            if release_claim:
                # Spawn path: transition running → ready + clear claim.
                conn.execute(
                    "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (failures, error[:500], task_id),
                )
            else:
                # Timeout/crash path: task is already at ``ready`` via
                # its own UPDATE. Just bookkeep the counter + last error.
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, error[:500], task_id),
                )
            if end_run:
                # Spawn path: close the open run with outcome.
                run_id = _end_run(
                    conn, task_id,
                    outcome=outcome, status=outcome,
                    error=error[:500],
                    metadata={"failures": failures},
                )
                _append_event(
                    conn, task_id, outcome,
                    {"error": error[:500], "failures": failures},
                    run_id=run_id,
                )
            # Timeout/crash path's caller already emitted its own event.
    return blocked


# Backward-compat alias. Old name is referenced from tests and possibly
# third-party callers. New code should call ``_record_task_failure``.
def _record_spawn_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    failure_limit: int = None,
) -> bool:
    return _record_task_failure(
        conn, task_id, error,
        outcome="spawn_failed",
        failure_limit=failure_limit,
        release_claim=True,
        end_run=True,
    )


def _set_worker_pid(conn: sqlite3.Connection, task_id: str, pid: int) -> None:
    """Record the spawned child's pid + emit a ``spawned`` event.

    The event's payload carries the pid so a human reading ``hermes kanban
    tail`` can correlate log lines with OS-level traces without opening
    the drawer.
    """
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (int(pid), task_id),
        )
        run_id = _current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                (int(pid), run_id),
            )
        _append_event(conn, task_id, "spawned", {"pid": int(pid)}, run_id=run_id)


def _clear_failure_counter(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset the unified consecutive-failures counter.

    Called from ``complete_task`` on successful completion — a fresh
    success means the task + profile combination is working and any
    past failures are history. NOT called on spawn success anymore:
    a successful spawn proves the worker could start but says nothing
    about whether the run will succeed, so we need to let timeouts and
    crashes accumulate across spawn boundaries.
    """
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 0, "
            "last_failure_error = NULL WHERE id = ?",
            (task_id,),
        )


# Legacy alias for test-code and anything else that still imports it.
_clear_spawn_failures = _clear_failure_counter


def check_respawn_guard(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Return a guard reason if ``task_id`` should NOT be re-spawned, else None.

    Called per ready task in ``dispatch_once`` before any claim attempt.
    Returning a reason defers the spawn this tick; the task stays in
    ``ready`` and gets another chance on the next dispatcher tick.

    Checks in priority order:

    ``"blocker_auth"``
        The task's last failure error matches a quota / authentication
        pattern. Retrying immediately is unlikely to help (rate limits
        reset on a timer; auth needs human action), so we defer to the
        next tick. The existing ``consecutive_failures`` counter still
        trips the auto-block circuit breaker after ``failure_limit``
        consecutive failures, so a persistent auth error eventually
        blocks via the normal path — but a transient 429 gets a few
        ticks of recovery first.

    ``"recent_success"``
        A completed run exists within ``_RESPAWN_GUARD_SUCCESS_WINDOW``
        seconds.  Useful work already succeeded for this task; wait for
        human review rather than immediately re-spawning.

    ``"active_pr"``
        A GitHub PR URL appears in a recent task comment (within
        ``_RESPAWN_GUARD_PR_WINDOW`` seconds).  A prior worker already
        opened a PR; re-spawning risks a duplicate PR on the same task.

    Stale / dead claim locks are NOT a guard reason — they are handled
    by ``release_stale_claims`` and ``detect_crashed_workers`` which
    reset the task to ``ready`` only after verifying the lock is
    genuinely dead (no live PID on this host).
    """
    row = conn.execute(
        "SELECT last_failure_error FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    # 1. Quota / auth blocker: retrying immediately will not help.
    err = row["last_failure_error"]
    if err and _RESPAWN_BLOCKER_RE.search(err):
        return "blocker_auth"

    now = int(time.time())

    # 2. Completed run within guard window — proof of recent success.
    cutoff = now - _RESPAWN_GUARD_SUCCESS_WINDOW
    if conn.execute(
        "SELECT id FROM task_runs "
        "WHERE task_id = ? AND outcome = 'completed' AND ended_at >= ?",
        (task_id, cutoff),
    ).fetchone():
        return "recent_success"

    # 3. GitHub PR URL in a recent comment — prior worker already opened a PR.
    pr_cutoff = now - _RESPAWN_GUARD_PR_WINDOW
    for c in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND created_at >= ?",
        (task_id, pr_cutoff),
    ).fetchall():
        if c["body"] and _RESPAWN_GUARD_PR_URL_RE.search(c["body"]):
            return "active_pr"

    return None


def has_spawnable_ready(conn: sqlite3.Connection) -> bool:
    """Return True iff there is at least one ready+assigned+unclaimed task
    whose assignee maps to a real Hermes profile.

    Used by the gateway- and CLI-embedded dispatchers' health telemetry to
    decide whether ``0 spawned`` is a "stuck" condition (real spawnable
    work waiting) or a "correctly idle" condition (only control-plane
    lanes like ``orion-cc`` / ``orion-research`` waiting on terminals
    that pull tasks via ``claim_task`` directly).

    Falls back to "any ready+assigned" if ``profile_exists`` is not
    importable (e.g. partial install) — preserves the old behavior so
    the warning still fires in degraded environments.
    """
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM tasks "
        "WHERE status = 'ready' AND assignee IS NOT NULL "
        "    AND claim_lock IS NULL"
    ).fetchall()
    if not rows:
        return False
    try:
        from hermes_cli.profiles import profile_exists  # local import: avoids cycle
    except Exception:
        # Can't introspect — assume spawnable, preserve legacy behavior.
        return True
    for row in rows:
        if profile_exists(row["assignee"]):
            return True
    return False


def has_spawnable_review(conn: sqlite3.Connection) -> bool:
    """Return True iff there is at least one review+assigned+unclaimed task
    whose assignee maps to a real Hermes profile.

    Mirror of :func:`has_spawnable_ready` for the review column —
    used by the health telemetry to decide whether the dispatcher
    should have spawned a review agent.
    """
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM tasks "
        "WHERE status = 'review' AND assignee IS NOT NULL "
        "    AND claim_lock IS NULL"
    ).fetchall()
    if not rows:
        return False
    try:
        from hermes_cli.profiles import profile_exists  # local import: avoids cycle
    except Exception:
        return True
    for row in rows:
        if profile_exists(row["assignee"]):
            return True
    return False


def dispatch_once(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_SPAWN_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
) -> DispatchResult:
    """Run one dispatcher tick.

    Steps:
      1. Reclaim stale running tasks (TTL expired).
      2. Reclaim stale running tasks (no recent heartbeat).
      3. Reclaim crashed running tasks (host-local PID no longer alive).
      3. Promote todo -> ready where all parents are done.
      4. For each ready task with an assignee, atomically claim and call
         ``spawn_fn(task, workspace_path, board) -> Optional[int]``. The
         return value (if any) is recorded as ``worker_pid`` so subsequent
         ticks can detect crashes before the TTL expires.

    Spawn failures are counted per-task. After ``failure_limit`` consecutive
    failures the task is auto-blocked with the last error as its reason —
    prevents the dispatcher from thrashing forever on an unfixable task.

    ``max_spawn`` is a **live concurrency cap**, not a per-tick spawn budget:
    it counts tasks already in ``status='running'`` plus this tick's spawns
    against the limit. So ``max_spawn=4`` means "at most 4 workers running
    at any time across the whole board" — matching the gateway's stated
    intent ("limit concurrent kanban tasks"). With a per-tick interpretation
    a 60-second tick interval could grow concurrency by N every minute on a
    busy board and accumulate without bound.

    ``spawn_fn`` defaults to ``_default_spawn``. Tests pass a stub.
    ``board`` pins workspace/log/db resolution for this tick to a specific
    board. When omitted, the current-board resolution chain is used.
    """
    board_slug = _connection_board(conn, board)
    dispatch_audit = audit_board_dispatcher_ownership(board_slug)
    if dispatch_audit.get("status") == "critical":
        _log.error(
            "kanban dispatcher: %s on board %s: %s",
            dispatch_audit.get("summary"),
            board_slug,
            dispatch_audit.get("dispatchers"),
        )
    elif dispatch_audit.get("status") == "warning":
        _log.warning(
            "kanban dispatcher: %s on board %s",
            dispatch_audit.get("summary"),
            board_slug,
        )

    # Reap zombie children from previously spawned workers. See
    # reap_worker_zombies() for the full rationale.
    reap_worker_zombies()

    result = DispatchResult()
    result.reclaimed = release_stale_claims(conn)
    result.stale = detect_stale_running(
        conn, stale_timeout_seconds=stale_timeout_seconds,
    )
    result.crashed = detect_crashed_workers(conn)
    # detect_crashed_workers stashes protocol-violation auto-blocks on
    # itself so the public list-return stays stable. Pull them into the
    # DispatchResult here so telemetry / tests see the trip.
    _crash_auto_blocked = getattr(
        detect_crashed_workers, "_last_auto_blocked", []
    )
    if _crash_auto_blocked:
        result.auto_blocked.extend(_crash_auto_blocked)
    result.timed_out = enforce_max_runtime(conn)
    result.promoted = recompute_ready(conn)

    # Count tasks already running so max_spawn enforces concurrency rather
    # than a per-tick spawn budget. See the docstring above for the full
    # rationale; the short version is that a 60-second tick interval with a
    # per-tick budget of N would grow concurrency by N every tick on a busy
    # board, since "running" tasks aren't reclaimed by completion alone —
    # they sit in status='running' until the worker calls
    # kanban_complete/kanban_block (or the dispatcher TTL-reclaims them).
    running_count = 0
    if max_spawn is not None:
        running_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
            ).fetchone()[0]
        )

    ready_rows = conn.execute(
        "SELECT id, assignee FROM tasks "
        "WHERE status = 'ready' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    # Honour kanban.max_in_progress: if the board already has enough running
    # tasks, skip spawning this tick so slow workers (local LLMs,
    # resource-constrained hosts) can finish what they have before more tasks
    # pile up and time out.
    if max_in_progress is not None and ready_rows:
        in_progress = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
        ).fetchone()[0]
        if in_progress >= max_in_progress:
            return result
        # Only spawn enough to reach the cap, respecting max_spawn too.
        remaining = max_in_progress - in_progress
        if max_spawn is None or max_spawn > remaining:
            max_spawn = remaining
    spawned = 0
    for row in ready_rows:
        if max_spawn is not None and running_count + spawned >= max_spawn:
            break
        if not row["assignee"]:
            result.skipped_unassigned.append(row["id"])
            continue
        eligibility = evaluate_dispatch_eligibility(conn, row["id"], board=board_slug)
        if not eligibility.get("ok"):
            codes = [
                str(blocker.get("code") or "blocked")
                for blocker in eligibility.get("blockers") or []
                if isinstance(blocker, dict)
            ]
            result.contract_blocked.append((row["id"], codes))
            if not dry_run:
                _block_dispatch_ineligible(conn, row["id"], eligibility)
            continue
        # Skip ready tasks whose assignee is not a real Hermes profile.
        # `_default_spawn` invokes ``hermes -p <assignee>`` which fails
        # with "Profile 'X' does not exist" when the assignee names a
        # control-plane lane (e.g. an interactive Claude Code terminal
        # like ``orion-cc`` / ``orion-research``) rather than a Hermes
        # profile. Those task lanes are pulled by terminals via
        # ``claim_task`` directly and should NEVER auto-spawn — the
        # subprocess would crash on startup, get reaped as a zombie,
        # the task would loop back to ``ready`` on next tick, and we'd
        # burn CPU forever (#kanban-dispatcher-crash-loop 2026-05-05).
        try:
            from hermes_cli.profiles import profile_exists  # local import: avoids cycle
        except Exception:
            profile_exists = None  # type: ignore[assignment]
        if profile_exists is not None and not profile_exists(row["assignee"]):
            # Bucket separately from skipped_unassigned: the operator
            # cannot fix this by assigning a profile (the assignee IS the
            # intended owner — a terminal lane). Health telemetry uses
            # this distinction to suppress spurious "stuck" warnings on
            # multi-lane setups where the ready queue is steadily full
            # of human-pulled work.
            result.skipped_nonspawnable.append(row["id"])
            continue
        # Respawn guard: refuse to re-spawn when useful work is already
        # in-flight/recent, or when the last failure is a deterministic
        # blocker (quota / auth). The guard defers the spawn this tick so
        # the task gets a chance to clear (rate limits often reset in
        # seconds-to-minutes); the existing consecutive_failures counter
        # still trips the auto-block circuit breaker after failure_limit
        # consecutive failures, so a persistent auth error eventually
        # blocks via the normal path rather than on first occurrence.
        guard_reason = check_respawn_guard(conn, row["id"])
        if guard_reason is not None:
            result.respawn_guarded.append((row["id"], guard_reason))
            # Emit an event so operators can see why the task was
            # skipped when reading `hermes kanban tail` — without
            # this the task appears stuck in ready with no diagnosis.
            if not dry_run:
                with write_txn(conn):
                    _append_event(
                        conn, row["id"], "respawn_guarded",
                        {"reason": guard_reason},
                    )
            continue
        if dry_run:
            result.spawned.append((row["id"], row["assignee"], ""))
            continue
        claimed = claim_task(conn, row["id"], ttl_seconds=ttl_seconds, board=board_slug)
        if claimed is None:
            continue
        try:
            workspace = resolve_workspace(claimed, board=board)
        except Exception as exc:
            auto = _record_spawn_failure(
                conn, claimed.id, f"workspace: {exc}",
                failure_limit=failure_limit,
            )
            if auto:
                result.auto_blocked.append(claimed.id)
            continue
        # Persist the resolved workspace path so the worker can cd there.
        set_workspace_path(conn, claimed.id, str(workspace))
        _maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
        _spawn = spawn_fn if spawn_fn is not None else _default_spawn
        try:
            # Back-compat: older spawn_fn signatures accept only
            # (task, workspace). Test stubs in the suite rely on that.
            # Introspect the callable and pass `board` only when supported.
            import inspect
            try:
                sig = inspect.signature(_spawn)
                if "board" in sig.parameters:
                    pid = _spawn(claimed, str(workspace), board=board)
                else:
                    pid = _spawn(claimed, str(workspace))
            except (TypeError, ValueError):
                pid = _spawn(claimed, str(workspace))
            if pid:
                _set_worker_pid(conn, claimed.id, int(pid))
            # NOTE: we intentionally do NOT reset consecutive_failures
            # here. A successful spawn proves the worker can start but
            # doesn't prove the run will succeed. Under unified
            # failure counting, resetting on spawn would let a task
            # that keeps timing out after spawn loop forever. The
            # counter is cleared only on successful completion (see
            # complete_task).
            result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
            spawned += 1
        except Exception as exc:
            auto = _record_spawn_failure(
                conn, claimed.id, str(exc),
                failure_limit=failure_limit,
            )
            if auto:
                result.auto_blocked.append(claimed.id)

    # ---- review column dispatch ----
    # Review tasks are tasks that a worker moved to 'review' after
    # creating a PR.  The dispatcher spawns a review agent (loading
    # sdlc-review skill) that verifies the PR and either merges (→ done)
    # or rejects (→ back to running for the worker to fix).
    #
    # Same concurrency model as ready dispatch: review spawns count
    # against max_spawn alongside ready tasks, so the total number of
    # running workers stays bounded.
    review_rows = conn.execute(
        "SELECT id, assignee FROM tasks "
        "WHERE status = 'review' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    for row in review_rows:
        if max_spawn is not None and running_count + spawned >= max_spawn:
            break
        if not row["assignee"]:
            result.skipped_unassigned.append(row["id"])
            continue
        eligibility = evaluate_dispatch_eligibility(conn, row["id"], board=board_slug)
        if not eligibility.get("ok"):
            codes = [
                str(blocker.get("code") or "blocked")
                for blocker in eligibility.get("blockers") or []
                if isinstance(blocker, dict)
            ]
            result.contract_blocked.append((row["id"], codes))
            if not dry_run:
                _block_dispatch_ineligible(conn, row["id"], eligibility)
            continue
        try:
            from hermes_cli.profiles import profile_exists
        except Exception:
            profile_exists = None  # type: ignore[assignment]
        if profile_exists is not None and not profile_exists(row["assignee"]):
            result.skipped_nonspawnable.append(row["id"])
            continue
        if dry_run:
            result.spawned.append((row["id"], row["assignee"], ""))
            continue
        claimed = claim_review_task(conn, row["id"], ttl_seconds=ttl_seconds, board=board_slug)
        if claimed is None:
            continue
        try:
            workspace = resolve_workspace(claimed, board=board)
        except Exception as exc:
            auto = _record_spawn_failure(
                conn, claimed.id, f"workspace: {exc}",
                failure_limit=failure_limit,
            )
            if auto:
                result.auto_blocked.append(claimed.id)
            continue
        # Persist the resolved workspace path so the worker can cd there.
        set_workspace_path(conn, claimed.id, str(workspace))
        _maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
        # Force-load sdlc-review skill for review agents.  The
        # _default_spawn function already auto-loads kanban-worker, and
        # appends task.skills via --skills.  Setting task.skills here
        # means the review agent gets both kanban-worker (lifecycle)
        # and sdlc-review (review logic: AC verification, merge, etc.).
        claimed.skills = ["sdlc-review"]
        _spawn = spawn_fn if spawn_fn is not None else _default_spawn
        try:
            import inspect
            try:
                sig = inspect.signature(_spawn)
                if "board" in sig.parameters:
                    pid = _spawn(claimed, str(workspace), board=board)
                else:
                    pid = _spawn(claimed, str(workspace))
            except (TypeError, ValueError):
                pid = _spawn(claimed, str(workspace))
            if pid:
                _set_worker_pid(conn, claimed.id, int(pid))
            result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
            spawned += 1
        except Exception as exc:
            auto = _record_spawn_failure(
                conn, claimed.id, str(exc),
                failure_limit=failure_limit,
            )
            if auto:
                result.auto_blocked.append(claimed.id)
    return result


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def worker_log_rotation_config(kanban_cfg: Optional[dict] = None) -> tuple[int, int]:
    """Return ``(rotate_bytes, backup_count)`` for worker log rotation.

    Defaults preserve the historical behavior: rotate at 2 MiB and keep one
    backup generation (``.log.1``). Operators with long-running workers can
    raise either value from ``config.yaml`` without changing dispatcher code.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    max_bytes = _positive_int(
        (kanban_cfg or {}).get("worker_log_rotate_bytes"),
        DEFAULT_LOG_ROTATE_BYTES,
        minimum=1,
    )
    backup_count = _positive_int(
        (kanban_cfg or {}).get("worker_log_backup_count"),
        DEFAULT_LOG_BACKUP_COUNT,
        minimum=0,
    )
    return max_bytes, backup_count


def _rotated_log_path(log_path: Path, generation: int) -> Path:
    return log_path.with_suffix(log_path.suffix + f".{generation}")


def _rotate_worker_log(
    log_path: Path,
    max_bytes: int,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Rotate ``<log>`` when it exceeds ``max_bytes``.

    ``backup_count=1`` preserves the legacy single-generation behavior:
    ``<log>`` moves to ``<log>.1`` and any previous ``.1`` is replaced.
    Higher values shift older generations up to ``backup_count``.
    """
    try:
        if not log_path.exists():
            return
        if log_path.stat().st_size <= max_bytes:
            return
        backup_count = _positive_int(
            backup_count,
            DEFAULT_LOG_BACKUP_COUNT,
            minimum=0,
        )
        if backup_count == 0:
            log_path.unlink()
            return
        oldest = _rotated_log_path(log_path, backup_count)
        try:
            if oldest.exists():
                oldest.unlink()
        except OSError:
            pass
        for generation in range(backup_count - 1, 0, -1):
            src = _rotated_log_path(log_path, generation)
            if not src.exists():
                continue
            try:
                src.rename(_rotated_log_path(log_path, generation + 1))
            except OSError:
                pass
        log_path.rename(_rotated_log_path(log_path, 1))
    except OSError:
        pass


def _module_hermes_argv() -> list[str]:
    """Return the interpreter-bound Hermes CLI invocation."""
    # ``hermes_cli.main`` is the console-script target declared in
    # pyproject.toml, NOT a top-level ``hermes`` package — there is no
    # ``hermes`` package to import.
    return [sys.executable, "-m", "hermes_cli.main"]


def _absolute_hermes_path(path: str) -> str:
    """Return an absolute filesystem path for a resolved Hermes shim."""
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


def _looks_like_path(value: str) -> bool:
    """Return true when a command override is an explicit path, not a name."""
    expanded = os.path.expanduser(value)
    return (
        expanded.startswith("~")
        or os.path.isabs(expanded)
        or bool(os.path.dirname(expanded))
        or "\\" in expanded
        or bool(re.match(r"^[A-Za-z]:", expanded))
    )


def _is_windows_batch_shim(path: str) -> bool:
    """Return true for Windows shell/batch shims that should not be argv[0]."""
    return path.lower().endswith((".cmd", ".bat"))


def _path_search_names(command: str) -> list[str]:
    """Return executable names to try for an unqualified command."""
    if not _IS_WINDOWS or os.path.splitext(command)[1]:
        return [command]
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    exts = [ext for ext in raw.split(";") if ext]
    return [command + ext for ext in exts]


def _safe_which_no_cwd(command: str) -> Optional[str]:
    """Resolve a bare command from PATH without implicit current-dir search.

    ``shutil.which`` follows platform search behavior. On Windows that can
    include the current directory before PATH for bare names, which is not a
    safe dispatcher primitive. This resolver only considers explicit PATH
    entries and skips empty / ``.`` entries.
    """
    path_env = os.environ.get("PATH", "")
    for raw_dir in path_env.split(os.pathsep):
        if not raw_dir or raw_dir == ".":
            continue
        directory = os.path.expanduser(raw_dir)
        for name in _path_search_names(command):
            candidate = os.path.join(directory, name)
            if not os.path.isfile(candidate):
                continue
            if _IS_WINDOWS or os.access(candidate, os.X_OK):
                return candidate
    return None


def _hermes_path_argv(path: str) -> list[str]:
    """Return argv for a resolved Hermes executable path.

    Windows batch shims (`.cmd` / `.bat`) are not safe as argv[0] for
    worker launches because the argument vector includes task-derived
    values. Prefer the interpreter-bound module form whenever the resolved
    executable is only a shell shim.
    """
    if _IS_WINDOWS and _is_windows_batch_shim(path):
        return _module_hermes_argv()
    return [_absolute_hermes_path(path)]


def _resolve_hermes_argv() -> list[str]:
    """Resolve the ``hermes`` invocation as argv parts for ``Popen``.

    Tries in order:

    1. ``$HERMES_BIN`` — explicit operator override. Path-like values are
       normalized to absolute paths; bare command names keep normal PATH
       semantics and never prefer a same-directory file before ``PATH``.
    2. ``shutil.which("hermes")`` — the console-script shim, normalized to
       an absolute path. On Windows, ``which`` can return a relative
       ``.\\hermes.CMD`` when the current directory is on ``PATH``; directly
       launching batch shims is also unsafe with task-derived argv. The
       dispatcher therefore falls back to the interpreter-bound module form
       for implicit ``.cmd`` / ``.bat`` shims.
    3. ``sys.executable -m hermes_cli.main`` — fallback for setups where
       Hermes is launched from a venv and the ``hermes`` shim is not on
       the dispatcher's ``$PATH`` (cron, systemd ``User=`` services,
       launchd jobs, detached processes, etc.). Goes through the running
       interpreter so the result is independent of ``$PATH``.

    Mirrors ``gateway.run._resolve_hermes_bin`` for the same reason. Kept
    local (not imported from gateway) because ``hermes_cli`` sits below
    ``gateway`` in the dependency order.
    """
    import shutil

    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin:
        if _looks_like_path(env_bin):
            return _hermes_path_argv(env_bin)
        resolved_env_bin = _safe_which_no_cwd(env_bin)
        if resolved_env_bin:
            return _hermes_path_argv(resolved_env_bin)
        return _module_hermes_argv()

    hermes_bin = _safe_which_no_cwd("hermes") if _IS_WINDOWS else shutil.which("hermes")
    if hermes_bin:
        return _hermes_path_argv(hermes_bin)
    return _module_hermes_argv()


def _kanban_worker_skill_available(hermes_home: Optional[str]) -> bool:
    """True if the bundled ``kanban-worker`` skill resolves for the home the
    spawned worker will run under.

    The dispatcher injects ``--skills kanban-worker`` into every worker. When
    the worker activates a profile (``hermes -p <name>``), its ``SKILLS_DIR``
    becomes ``<profile_home>/skills`` — which on many profiles does NOT contain
    the bundled skill (it ships in the *default* root home, not every
    profile-scoped skills dir). Preloading a missing skill is fatal at CLI
    startup (``ValueError: Unknown skill(s): kanban-worker``), aborting the
    worker before the agent loop runs. Gate the flag on actual resolvability;
    the kanban lifecycle contract is still injected via ``KANBAN_GUIDANCE``, so
    omitting the flag only drops the supplementary pattern library.
    """
    from pathlib import Path as _Path

    # An unset HERMES_HOME means the worker falls back to the default root
    # home (``~/.hermes``), which ships the bundled skill.
    base = _Path(hermes_home) if hermes_home else (_Path.home() / ".hermes")
    skills_root = base / "skills"
    if not skills_root.is_dir():
        return False
    # Canonical bundled location first (cheap), then a bounded scan for
    # profiles that have it nested elsewhere.
    if (skills_root / "devops" / "kanban-worker" / "SKILL.md").is_file():
        return True
    try:
        for skill_md in skills_root.rglob("kanban-worker/SKILL.md"):
            if skill_md.is_file():
                return True
    except OSError:
        pass
    return False


def _worker_terminal_timeout_env(
    max_runtime_seconds: Optional[int],
    current_timeout: Optional[str],
) -> Optional[str]:
    """Return a worker-scoped TERMINAL_TIMEOUT override, if needed.

    Kanban's ``max_runtime_seconds`` bounds the whole worker attempt. The
    terminal tool has its own default timeout via ``TERMINAL_TIMEOUT``; when
    the worker runtime is longer, raise only the child process default so a
    long command is not killed by the generic terminal default first.
    """
    if max_runtime_seconds is None:
        return None
    try:
        runtime = int(max_runtime_seconds)
    except (TypeError, ValueError):
        return None
    if runtime <= 0:
        return None

    desired = max(1, runtime - KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS)
    try:
        existing = int(str(current_timeout).strip()) if current_timeout else 0
    except (TypeError, ValueError):
        existing = 0
    if existing >= desired:
        return None
    return str(desired)


def _default_spawn(
    task: Task,
    workspace: str,
    *,
    board: Optional[str] = None,
) -> Optional[int]:
    """Fire-and-forget ``hermes -p <profile> chat -q ...`` subprocess.

    Returns the spawned child's PID so the dispatcher can detect crashes
    before the claim TTL expires. The child's completion is still observed
    via the ``complete`` / ``block`` transitions the worker writes itself;
    the PID check is a safety net for crashes, OOM kills, and Ctrl+C.

    ``board`` pins the child's kanban context to that board: the child's
    ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` / workspaces_root env
    vars all resolve to the same board the dispatcher claimed the task
    from. Workers cannot accidentally see other boards.
    """
    import subprocess
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")

    from hermes_cli.profiles import normalize_profile_name

    profile_arg = normalize_profile_name(task.assignee)

    prompt = f"work kanban task {task.id}"
    env = dict(os.environ)

    # Inject HERMES_HOME so the worker reads the profile-scoped config.yaml
    # (fallback_providers, toolsets, agent settings, etc.) instead of the root
    # config.  Without this, `env = dict(os.environ)` copies only the parent's
    # env, and when the child process starts `hermes -p <name>` the
    # _apply_profile_override() runs *before* hermes_constants is imported.
    # If HERMES_HOME is absent from the child's env, get_hermes_home() falls
    # back to Path.home() / ".hermes" (the DEFAULT profile root), ignoring the
    # profile-specific config entirely.  Fixes profile-scoped fallback_providers
    # being invisible to kanban workers.
    from hermes_cli.profiles import resolve_profile_env
    try:
        env["HERMES_HOME"] = resolve_profile_env(profile_arg)
    except FileNotFoundError:
        # Profile dir doesn't exist — defer resolution to the CLI's
        # _apply_profile_override() via HERMES_PROFILE (set below).
        # This only happens in test fixtures where the isolated
        # HERMES_HOME never had profiles created.
        pass
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    if task.branch_name:
        env["HERMES_KANBAN_BRANCH"] = task.branch_name
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    terminal_timeout = _worker_terminal_timeout_env(
        task.max_runtime_seconds,
        env.get("TERMINAL_TIMEOUT"),
    )
    if terminal_timeout is not None:
        env["TERMINAL_TIMEOUT"] = terminal_timeout
    foreground_timeout = _worker_terminal_timeout_env(
        task.max_runtime_seconds,
        env.get("TERMINAL_MAX_FOREGROUND_TIMEOUT"),
    )
    if foreground_timeout is not None:
        env["TERMINAL_MAX_FOREGROUND_TIMEOUT"] = foreground_timeout
    # Pin the shared board + workspaces root the dispatcher resolved, so
    # that even when the worker activates a profile (`hermes -p <name>`
    # rewrites HERMES_HOME), its kanban paths still match the
    # dispatcher's. Belt-and-braces with the `get_default_hermes_root()`
    # resolution in `kanban_home()` — symmetric resolution is the norm,
    # but unusual symlink / Docker layouts are caught here too.
    env["HERMES_KANBAN_DB"] = str(kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(workspaces_root(board=board))
    # Board slug — the final defense-in-depth pin. If the worker ever
    # resolves kanban paths without the DB / workspaces env vars, the
    # board slug still forces it to the right directory.
    resolved_board = _normalize_board_slug(board) or get_current_board()
    env["HERMES_KANBAN_BOARD"] = resolved_board
    # HERMES_PROFILE is the author the kanban_comment tool defaults to.
    # `hermes -p <assignee>` activates the profile, but the env var is
    # what the tool reads — set it explicitly here so comments are
    # attributed correctly regardless of how the child loads config.
    env["HERMES_PROFILE"] = profile_arg

    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,
        # Worker subprocesses switch to a profile-scoped HERMES_HOME above,
        # so they see that profile's shell-hook allowlist instead of the
        # dispatcher's root allowlist. Pass --accept-hooks explicitly so
        # profile-local worker sessions still register configured hooks.
        "--accept-hooks",
    ]
    # Auto-load the kanban-worker skill so every dispatched worker
    # has the pattern library (good summary/metadata shapes, retry
    # diagnostics, block-reason examples) in its context, even if
    # the profile hasn't wired it into skills config. The MANDATORY
    # lifecycle is already in the system prompt via KANBAN_GUIDANCE;
    # this skill is the deeper reference. Users can point a profile
    # at a different/additional skill via config if they want —
    # --skills is additive to the profile's default skill set.
    #
    # Only add the flag when the skill actually resolves for the home
    # the worker runs under: the bundled skill is absent from many
    # profile-scoped skills dirs, and preloading a missing skill is
    # fatal at CLI startup. Omitting it is safe — the lifecycle
    # contract still ships via KANBAN_GUIDANCE.
    if _kanban_worker_skill_available(env.get("HERMES_HOME")):
        cmd.extend(["--skills", "kanban-worker"])
    # Per-task force-loaded skills. Each name goes in its own
    # `--skills X` pair rather than a single comma-joined arg: the CLI
    # accepts both forms (action='append' + comma-split), but
    # per-name pairs are easier to read in `ps` output and avoid any
    # quoting ambiguity if a skill name ever contains unusual chars.
    # Dedupe against the built-in so we don't double-load kanban-worker
    # if a task author asks for it explicitly.
    if task.skills:
        for sk in task.skills:
            if sk and sk != "kanban-worker":
                cmd.extend(["--skills", sk])
    if task.model_override:
        cmd.extend(["-m", task.model_override])
    cmd.extend([
        "chat",
        "-q", prompt,
    ])
    # Redirect output to a per-task log under <board-root>/logs/.
    # Anchored at the board root (not the shared kanban root), so
    # `hermes kanban log` on a specific board reads its own file and
    # logs don't collide across boards that happen to share task ids.
    log_dir = worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rotate_bytes, backup_count = worker_log_rotation_config()
    _rotate_worker_log(log_path, rotate_bytes, backup_count)

    # Use 'a' so a re-run on unblock appends rather than overwrites.
    log_f = open(log_path, "ab")
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            cmd,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
        )
    except FileNotFoundError:
        log_f.close()
        raise RuntimeError(
            "`hermes` executable not found on PATH. "
            "Install Hermes Agent or activate its venv before running the kanban dispatcher."
        )
    # NOTE: we intentionally do NOT close log_f here — we want Popen's
    # child process to keep writing after this function returns.  The
    # handle is kept alive by the child's inheritance.  The parent's
    # reference goes out of scope and is GC'd, but the OS-level FD stays
    # open in the child until the child exits.
    return proc.pid


# ---------------------------------------------------------------------------
# Long-lived dispatcher daemon
# ---------------------------------------------------------------------------

def run_daemon(
    *,
    interval: float = 60.0,
    max_spawn: Optional[int] = None,
    failure_limit: int = DEFAULT_SPAWN_FAILURE_LIMIT,
    stop_event=None,
    on_tick=None,
) -> None:
    """Run the dispatcher in a loop until interrupted.

    Calls :func:`dispatch_once` every ``interval`` seconds. Exits cleanly
    on SIGINT / SIGTERM so ``hermes kanban daemon`` is systemd-friendly.
    ``stop_event`` (a :class:`threading.Event`) and ``on_tick`` (a
    callable receiving the :class:`DispatchResult`) are test hooks.
    """
    import signal
    import threading

    if stop_event is None:
        stop_event = threading.Event()

    def _handle(_signum, _frame):
        stop_event.set()

    # Install handlers only when running on the main thread — tests call
    # this inline from worker threads and signal() would raise there.
    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handle)
                except (ValueError, OSError):
                    pass

    while not stop_event.is_set():
        try:
            with contextlib.closing(connect()) as conn:
                res = dispatch_once(
                    conn,
                    max_spawn=max_spawn,
                    failure_limit=failure_limit,
                )
            if on_tick is not None:
                try:
                    on_tick(res)
                except Exception:
                    pass
        except Exception:
            # Don't let any single tick kill the daemon.
            import traceback
            traceback.print_exc()
        stop_event.wait(timeout=interval)


# ---------------------------------------------------------------------------
# Worker context builder (what a spawned worker sees)
# ---------------------------------------------------------------------------

def build_worker_context(conn: sqlite3.Connection, task_id: str) -> str:
    """Return the full text a worker should read to understand its task.

    Order:
      1. Task title (mandatory).
      2. Task body (optional opening post, capped at 8 KB).
      3. Prior attempts on THIS task (most recent ``_CTX_MAX_PRIOR_ATTEMPTS``
         shown; older attempts collapsed into a one-line summary).
         Each attempt's ``summary`` / ``error`` / ``metadata`` capped at
         ``_CTX_MAX_FIELD_BYTES`` each.
      4. Structured handoff results of every done parent task. Prefers
         ``run.summary`` / ``run.metadata`` when the parent was executed
         via a run; falls back to ``task.result`` for older data. Same
         per-field cap.
      5. Cross-task role history for the assignee (most recent 5
         completed runs on other tasks).
      6. Comment thread (most recent ``_CTX_MAX_COMMENTS`` shown, older
         collapsed).

    All caps exist so worker prompts stay bounded even on pathological
    boards (retry-heavy tasks, comment storms). The per-field char cap
    prevents a single 1 MB summary from dominating context.
    """
    task = get_task(conn, task_id)
    if not task:
        raise ValueError(f"unknown task {task_id}")

    def _cap(s: Optional[str], limit: int = _CTX_MAX_FIELD_BYTES) -> str:
        """Truncate a string to `limit` chars with a visible ellipsis."""
        if not s:
            return ""
        s = s.strip()
        if len(s) <= limit:
            return s
        return s[:limit] + f"… [truncated, {len(s) - limit} chars omitted]"

    lines: list[str] = []
    lines.append(f"# Kanban task {task.id}: {task.title}")
    lines.append("")
    lines.append(f"Assignee: {task.assignee or '(unassigned)'}")
    lines.append(f"Status:   {task.status}")
    if task.tenant:
        lines.append(f"Tenant:   {task.tenant}")
    lines.append(f"Workspace: {task.workspace_kind} @ {task.workspace_path or '(unresolved)'}")
    if task.max_runtime_seconds is not None:
        terminal_timeout = _worker_terminal_timeout_env(
            task.max_runtime_seconds,
            os.environ.get("TERMINAL_TIMEOUT"),
        )
        effective_terminal_timeout = terminal_timeout or os.environ.get("TERMINAL_TIMEOUT")
        lines.append(f"Max runtime: {task.max_runtime_seconds}s")
        if effective_terminal_timeout:
            lines.append(f"Terminal timeout: {effective_terminal_timeout}s")
    if task.branch_name:
        lines.append(f"Branch:   {task.branch_name}")
    lines.append("")

    if task.body and task.body.strip():
        lines.append("## Body")
        lines.append(_cap(task.body, _CTX_MAX_BODY_BYTES))
        lines.append("")

    # Prior attempts — show closed runs so a retrying worker sees the
    # history. Skip the currently-active run (that's this worker).
    # Cap at _CTX_MAX_PRIOR_ATTEMPTS most-recent closed runs; older
    # attempts get collapsed into a one-line marker so the worker knows
    # more exist without bloating the prompt.
    all_prior = [r for r in list_runs(conn, task_id) if r.ended_at is not None]
    # list_runs returns ascending by started_at; "most recent" = last N
    if len(all_prior) > _CTX_MAX_PRIOR_ATTEMPTS:
        omitted = len(all_prior) - _CTX_MAX_PRIOR_ATTEMPTS
        shown = all_prior[-_CTX_MAX_PRIOR_ATTEMPTS:]
        first_shown_idx = omitted + 1
    else:
        omitted = 0
        shown = all_prior
        first_shown_idx = 1
    if shown:
        lines.append("## Prior attempts on this task")
        if omitted:
            lines.append(
                f"_({omitted} earlier attempt{'s' if omitted != 1 else ''} "
                f"omitted; showing most recent {len(shown)})_"
            )
        for offset, run in enumerate(shown):
            idx = first_shown_idx + offset
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(run.started_at))
            profile = run.profile or "(unknown)"
            outcome = run.outcome or run.status
            lines.append(f"### Attempt {idx} — {outcome} ({profile}, {ts})")
            if run.summary and run.summary.strip():
                lines.append(_cap(run.summary))
            if run.error and run.error.strip():
                lines.append(f"_error_: {_cap(run.error)}")
            if run.metadata:
                try:
                    meta_str = json.dumps(run.metadata, ensure_ascii=False, sort_keys=True)
                    lines.append(f"_metadata_: `{_cap(meta_str)}`")
                except Exception:
                    pass
            lines.append("")

    # Parents: prefer the most-recent 'completed' run's summary + metadata,
    # fall back to ``task.result`` when no run rows exist (legacy DBs,
    # or tasks completed before the runs table landed).
    parent_rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    parent_ids = [r["parent_id"] for r in parent_rows]

    if parent_ids:
        wrote_header = False
        for pid in parent_ids:
            pt = get_task(conn, pid)
            if not pt or pt.status != "done":
                continue
            runs = [r for r in list_runs(conn, pid) if r.outcome == "completed"]
            runs.sort(key=lambda r: r.started_at, reverse=True)
            run = runs[0] if runs else None

            if not wrote_header:
                lines.append("## Parent task results")
                wrote_header = True
            lines.append(f"### {pid}")

            body_lines: list[str] = []
            if run is not None and run.summary and run.summary.strip():
                body_lines.append(_cap(run.summary))
            elif pt.result:
                body_lines.append(_cap(pt.result))
            else:
                body_lines.append("(no result recorded)")

            if run is not None and run.metadata:
                try:
                    meta_str = json.dumps(run.metadata, ensure_ascii=False, sort_keys=True)
                    body_lines.append(f"_metadata_: `{_cap(meta_str)}`")
                except Exception:
                    pass
            lines.extend(body_lines)
            lines.append("")

    # Cross-task role history: what else has THIS assignee completed
    # recently? Gives the worker implicit continuity — "I'm the reviewer
    # and my last three reviews focused on security" — without forcing
    # the user to wire anything into SOUL.md / MEMORY.md. Bounded to the
    # most recent 5 completed runs, excluding this task so the retry
    # section above isn't duplicated. Safe on assignee=None (skipped).
    if task.assignee:
        role_rows = conn.execute(
            "SELECT t.id, t.title, r.summary, r.ended_at "
            "FROM task_runs r JOIN tasks t ON r.task_id = t.id "
            "WHERE r.profile = ? AND r.task_id != ? "
            "  AND r.outcome = 'completed' "
            "ORDER BY r.ended_at DESC LIMIT 5",
            (task.assignee, task_id),
        ).fetchall()
        if role_rows:
            lines.append(f"## Recent work by @{task.assignee}")
            for row in role_rows:
                ts = time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(int(row["ended_at"]))
                )
                s = (row["summary"] or "").strip().splitlines()
                first = s[0][:200] if s else "(no summary)"
                lines.append(f"- {row['id']} — {row['title']} ({ts}): {first}")
            lines.append("")

    # Comments: cap at the most-recent _CTX_MAX_COMMENTS so
    # comment-storm tasks don't blow out the worker's prompt. Older
    # comments summarised in a one-line marker like prior attempts.
    all_comments = list_comments(conn, task_id)
    if len(all_comments) > _CTX_MAX_COMMENTS:
        omitted_c = len(all_comments) - _CTX_MAX_COMMENTS
        shown_c = all_comments[-_CTX_MAX_COMMENTS:]
    else:
        omitted_c = 0
        shown_c = all_comments
    if shown_c:
        lines.append("## Comment thread")
        if omitted_c:
            lines.append(
                f"_({omitted_c} earlier comment{'s' if omitted_c != 1 else ''} "
                f"omitted; showing most recent {len(shown_c)})_"
            )
        for c in shown_c:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.created_at))
            # Render author with explicit "comment from worker" framing so
            # operator-controlled HERMES_PROFILE values like "hermes-system"
            # or "operator" can't be misread by the next worker as a system
            # directive above the (attacker-influenceable) comment body.
            # Defense-in-depth — the LLM-controlled author-forgery surface
            # was already closed in #22435. See #22452.
            safe_author = (c.author or "").replace("`", "")
            lines.append(f"comment from worker `{safe_author}` at {ts}:")
            lines.append(_cap(c.body, _CTX_MAX_COMMENT_BYTES))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Funnel read-model helpers
# ---------------------------------------------------------------------------

_FUNNEL_WAITING_STATUSES = {"triage", "todo", "scheduled", "watching", "review"}
_FUNNEL_ACTIVE_STATUSES = {"ready", "running"}
_FUNNEL_FAILURE_OUTCOMES = {
    "crashed", "timed_out", "spawn_failed", "gave_up", "failed"
}
_FUNNEL_ARTIFACT_KEYS = (
    "artifacts", "artifact", "deliverables", "deliverable",
    "attachments", "attachment", "files", "file", "paths", "path",
)
_FUNNEL_PROOF_KEYS = (
    "proof", "proofs", "evidence", "verification", "verified",
    "validation", "checks", "tests", "tests_run",
)
_FUNNEL_OUTCOME_KEYS = (
    "outcome", "outcomes", "result", "results", "decision",
    "decisions", "finding", "findings", "capability", "capabilities",
)
_FUNNEL_ENTITY_KEYS = ("entities", "funnel_entities", "entity", "items")
_FUNNEL_ENTITY_ID_KEYS = ("id", "entity_id", "key", "ref")
_FUNNEL_ENTITY_TYPE_KEYS = ("type", "entity_type", "kind")
_FUNNEL_ENTITY_LABEL_KEYS = ("label", "name", "title")
_FUNNEL_ENTITY_STATE_KEYS = ("state", "substate", "status")
_FUNNEL_ENTITY_STAGE_KEYS = ("stage_key", "stage")
_FUNNEL_ENTITY_NEXT_ACTION_KEYS = ("next_action", "next_actions")
_FUNNEL_ENTITY_ACTOR_KEYS = ("actor", "owner", "assignee")


def _json_safe_value(value: Any) -> Any:
    """Return a JSON-serializable representation for read-model output."""
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return str(value)


def _funnel_value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _funnel_as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(v) for v in value if _funnel_value_present(v)]
    return [_json_safe_value(value)] if _funnel_value_present(value) else []


def _funnel_collect_values(mapping: Optional[dict], keys: Iterable[str]) -> list[dict]:
    if not isinstance(mapping, dict):
        return []
    out: list[dict] = []
    for key in keys:
        if key not in mapping:
            continue
        values = _funnel_as_list(mapping.get(key))
        for value in values:
            out.append({"key": key, "value": value})
    return out


def _funnel_first_present(mapping: dict, keys: Iterable[str]) -> Any:
    for key in keys:
        if key not in mapping:
            continue
        value = mapping.get(key)
        if _funnel_value_present(value):
            return value
    return None


def _funnel_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _funnel_collect_raw_entities(mapping: Optional[dict]) -> list[dict]:
    """Extract explicit nested entity records from structured funnel data.

    This intentionally looks only at JSON object/list fields. It does not parse
    titles, summaries, or prose. Compound funnel stages are therefore powered by
    worker/tool contracts, not keyword heuristics.
    """
    if not isinstance(mapping, dict):
        return []
    entity_shape_keys = (
        _FUNNEL_ENTITY_ID_KEYS + _FUNNEL_ENTITY_TYPE_KEYS +
        _FUNNEL_ENTITY_LABEL_KEYS + _FUNNEL_ENTITY_STATE_KEYS +
        _FUNNEL_ENTITY_STAGE_KEYS + _FUNNEL_ENTITY_NEXT_ACTION_KEYS +
        _FUNNEL_ENTITY_ACTOR_KEYS
    )

    def entity_like(value: dict) -> bool:
        return any(k in value for k in entity_shape_keys)

    out: list[dict] = []
    for key in _FUNNEL_ENTITY_KEYS:
        if key not in mapping:
            continue
        raw = mapping.get(key)
        if isinstance(raw, dict):
            if entity_like(raw):
                out.append(dict(raw))
            else:
                for entity_id, value in raw.items():
                    if isinstance(value, dict) and entity_like(value):
                        item = dict(value)
                        item.setdefault("id", str(entity_id))
                        out.append(item)
            continue
        if isinstance(raw, (list, tuple, set)):
            for value in raw:
                if isinstance(value, dict) and entity_like(value):
                    out.append(dict(value))
    return out


def _funnel_next_action_label(value: Any) -> Optional[str]:
    if not _funnel_value_present(value):
        return None
    if isinstance(value, dict):
        label = _funnel_first_present(value, ("action", "key", "type", "label", "status"))
        if _funnel_value_present(label):
            return str(label)
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return str(value)


def _funnel_normalize_entity(raw: dict, *, task_id: str, ordinal: int) -> dict:
    entity_id = _funnel_first_present(raw, _FUNNEL_ENTITY_ID_KEYS)
    entity_type = _funnel_first_present(raw, _FUNNEL_ENTITY_TYPE_KEYS) or "entity"
    label = _funnel_first_present(raw, _FUNNEL_ENTITY_LABEL_KEYS) or entity_id
    state = _funnel_first_present(raw, _FUNNEL_ENTITY_STATE_KEYS) or "unspecified"
    stage_key = _funnel_first_present(raw, _FUNNEL_ENTITY_STAGE_KEYS)
    next_action = _funnel_first_present(raw, _FUNNEL_ENTITY_NEXT_ACTION_KEYS)
    actor = _funnel_first_present(raw, _FUNNEL_ENTITY_ACTOR_KEYS)
    artifacts = [item["value"] for item in _funnel_collect_values(raw, _FUNNEL_ARTIFACT_KEYS)]
    proof = _funnel_collect_values(raw, _FUNNEL_PROOF_KEYS)
    outcomes = _funnel_collect_values(raw, _FUNNEL_OUTCOME_KEYS)
    stable_id = str(entity_id) if _funnel_value_present(entity_id) else f"{task_id}:entity:{ordinal}"
    return {
        "id": stable_id,
        "type": str(entity_type),
        "label": str(label) if _funnel_value_present(label) else stable_id,
        "state": str(state),
        "stage_key": str(stage_key) if _funnel_value_present(stage_key) else None,
        "source_task_id": task_id,
        "terminal": _funnel_bool(raw.get("terminal")),
        "blocked": _funnel_bool(raw.get("blocked")),
        "next_action": _json_safe_value(next_action) if _funnel_value_present(next_action) else None,
        "next_action_key": _funnel_next_action_label(next_action),
        "next_action_actor": str(actor) if _funnel_value_present(actor) else None,
        "outcome_keys": sorted({item["key"] for item in outcomes}),
        "proof_keys": sorted({item["key"] for item in proof}),
        "artifact_count": len(artifacts),
        "data": {str(k): _json_safe_value(v) for k, v in raw.items()},
    }


def _funnel_entity_dedupe_key(entity: dict) -> tuple[str, str, str]:
    return (
        str(entity.get("stage_key") or ""),
        str(entity.get("type") or "entity"),
        str(entity.get("id") or ""),
    )


def _funnel_count(sorted_values: Iterable[Optional[str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in sorted_values:
        if not _funnel_value_present(value):
            continue
        key = str(value)
        counts[key] = int(counts.get(key, 0)) + 1
    return {key: counts[key] for key in sorted(counts)}


def _funnel_increment_count(mapping: dict[str, int], value: Any, amount: int = 1) -> None:
    if not _funnel_value_present(value):
        return
    key = str(value)
    mapping[key] = int(mapping.get(key, 0)) + int(amount)


def _funnel_task_node(task: Task) -> dict:
    """Return the semantic stage node for a task.

    Explicit funnel fields win. Existing workflow columns are a backward-
    compatible fallback. Titles are deliberately ignored so the optimizer
    reasons from structured data instead of domain keywords.
    """
    has_explicit = any((
        task.goal_id, task.workstream_id, task.stage_key, task.action_key,
    ))
    has_workflow = any((task.workflow_template_id, task.current_step_key))
    goal_id = task.goal_id or "default"
    workstream_id = task.workstream_id or task.workflow_template_id or "default"
    stage_key = task.stage_key or task.current_step_key or "unclassified"
    action_key = task.action_key or "default"
    if has_explicit:
        semantic_source = "explicit"
    elif has_workflow:
        semantic_source = "workflow"
    else:
        semantic_source = "fallback"
    return {
        "id": (
            f"goal={goal_id}|workstream={workstream_id}|"
            f"stage={stage_key}|action={action_key}"
        ),
        "goal_id": goal_id,
        "workstream_id": workstream_id,
        "stage_key": stage_key,
        "action_key": action_key,
        "semantic_source": semantic_source,
    }


def _funnel_cycle_seconds(task: Task, runs: list[Run]) -> Optional[int]:
    if task.completed_at is not None:
        start = task.started_at or task.created_at
        return max(0, int(task.completed_at) - int(start))
    completed = [r for r in runs if r.outcome == "completed" and r.ended_at is not None]
    if completed:
        run = completed[-1]
        ended_at = run.ended_at
        if ended_at is not None:
            return max(0, int(ended_at) - int(run.started_at))
    return None


def _funnel_latest_blocker(task: Task, events: list[Event]) -> Optional[dict]:
    if task.status not in {"blocked", "scheduled", "watching"}:
        return None
    event_kinds = {"blocked", "scheduled", "watching", "watch_triggered", "gave_up", "spawn_auto_blocked"}
    for event in reversed(events):
        if event.kind not in event_kinds:
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        reason = (
            payload.get("reason") or payload.get("error") or
            payload.get("message") or payload.get("summary") or
            payload.get("trigger_type") or payload.get("wake_status")
        )
        return {
            "task_id": task.id,
            "kind": event.kind,
            "reason": reason,
            "created_at": event.created_at,
        }
    return {"task_id": task.id, "kind": task.status, "reason": None, "created_at": None}


def _funnel_task_signals(task: Task, runs: list[Run], events: list[Event]) -> dict:
    metadata_blobs: list[dict] = []
    if isinstance(task.funnel_data, dict):
        metadata_blobs.append(task.funnel_data)
    for run in runs:
        if isinstance(run.metadata, dict):
            metadata_blobs.append(run.metadata)
    for event in events:
        if isinstance(event.payload, dict):
            metadata_blobs.append(event.payload)

    artifacts: list[Any] = []
    proof: list[dict] = []
    outcomes: list[dict] = []
    entities_by_key: dict[tuple[str, str, str], dict] = {}
    entity_ordinal = 0
    for blob in metadata_blobs:
        artifacts.extend(
            item["value"] for item in _funnel_collect_values(blob, _FUNNEL_ARTIFACT_KEYS)
        )
        proof.extend(_funnel_collect_values(blob, _FUNNEL_PROOF_KEYS))
        outcomes.extend(_funnel_collect_values(blob, _FUNNEL_OUTCOME_KEYS))
        for raw_entity in _funnel_collect_raw_entities(blob):
            entity_ordinal += 1
            entity = _funnel_normalize_entity(
                raw_entity,
                task_id=task.id,
                ordinal=entity_ordinal,
            )
            entities_by_key[_funnel_entity_dedupe_key(entity)] = entity
    deduped_artifacts: list[Any] = []
    seen_artifacts: set[str] = set()
    for artifact in artifacts:
        marker = json.dumps(artifact, sort_keys=True, ensure_ascii=False, default=str)
        if marker in seen_artifacts:
            continue
        seen_artifacts.add(marker)
        deduped_artifacts.append(artifact)
    artifacts = deduped_artifacts
    if task.result:
        outcomes.append({"key": "task.result", "value": task.result})
    latest_summary = None
    for run in reversed(runs):
        if run.summary:
            latest_summary = run.summary
            outcomes.append({"key": "run.summary", "value": run.summary})
            break

    failed_runs = sum(
        1 for run in runs
        if (run.outcome in _FUNNEL_FAILURE_OUTCOMES or run.status in _FUNNEL_FAILURE_OUTCOMES)
    )
    failure_count = int(task.consecutive_failures or 0) + failed_runs

    return {
        "artifacts": artifacts,
        "proof": proof,
        "outcomes": outcomes,
        "entities": sorted(
            entities_by_key.values(),
            key=lambda e: (
                str(e.get("stage_key") or ""),
                str(e.get("type") or "entity"),
                str(e.get("id") or ""),
            ),
        ),
        "latest_summary": latest_summary,
        "failed_runs": failed_runs,
        "failure_count": failure_count,
    }


def build_funnel_read_model(
    conn: sqlite3.Connection,
    *,
    board: Optional[str] = None,
    include_archived: bool = False,
    limit_cards_per_stage: Optional[int] = 20,
    limit_entities_per_stage: Optional[int] = 50,
    goal_id: Optional[str] = None,
    workstream_id: Optional[str] = None,
    stage_key: Optional[str] = None,
    action_key: Optional[str] = None,
) -> dict:
    """Build a semantic funnel view over the Kanban board.

    This is a read-model: it never mutates dispatch state. Lifecycle columns
    still drive execution; funnel fields and run/event metadata drive the
    optimizer/UI view of end-to-end goal flow.
    """
    if limit_cards_per_stage is None:
        card_limit: Optional[int] = None
    else:
        card_limit = max(0, int(limit_cards_per_stage))
    if limit_entities_per_stage is None:
        entity_limit: Optional[int] = None
    else:
        entity_limit = max(0, int(limit_entities_per_stage))
    goal_filter = _normalize_funnel_text(goal_id)
    workstream_filter = _normalize_funnel_text(workstream_id)
    stage_filter = _normalize_funnel_text(stage_key)
    action_filter = _normalize_funnel_text(action_key)
    board_slug = _connection_board(conn, board)

    tasks = list_tasks(
        conn,
        include_archived=include_archived,
        order_by="created",
    )
    links = conn.execute(
        "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
    ).fetchall()
    watch_routes_by_task: dict[str, list[dict]] = {}
    for route in list_watch_routes(conn, active=True):
        watch_routes_by_task.setdefault(route.task_id, []).append(watch_route_to_dict(route))
    parents_by_child: dict[str, list[str]] = {}
    children_by_parent: dict[str, list[str]] = {}
    for link in links:
        parents_by_child.setdefault(link["child_id"], []).append(link["parent_id"])
        children_by_parent.setdefault(link["parent_id"], []).append(link["child_id"])

    stages: dict[str, dict] = {}
    node_by_task: dict[str, str] = {}
    summary_status: dict[str, int] = {}
    semantic_sources: dict[str, int] = {}
    uncategorized: list[dict] = []

    for task in tasks:
        node = _funnel_task_node(task)
        if goal_filter and node["goal_id"] != goal_filter:
            continue
        if workstream_filter and node["workstream_id"] != workstream_filter:
            continue
        if stage_filter and node["stage_key"] != stage_filter:
            continue
        if action_filter and node["action_key"] != action_filter:
            continue

        node_id = node["id"]
        node_by_task[task.id] = node_id
        stage = stages.setdefault(
            node_id,
            {
                **node,
                "counts_by_status": {},
                "metrics": {
                    "total_cards": 0,
                    "waiting_cards": 0,
                    "watching_cards": 0,
                    "active_cards": 0,
                    "blocked_cards": 0,
                    "done_cards": 0,
                    "failure_count": 0,
                    "completed_count": 0,
                    "artifact_count": 0,
                    "cards_with_artifacts": 0,
                    "cards_with_proof": 0,
                    "cards_with_outcomes": 0,
                    "avg_cycle_seconds": None,
                },
                "blockers": [],
                "cards": [],
                "cards_truncated": 0,
                "entities": [],
                "entities_truncated": 0,
                "entity_metrics": {
                    "total_entities": 0,
                    "blocked_entities": 0,
                    "terminal_entities": 0,
                    "entities_with_next_action": 0,
                    "entities_with_artifacts": 0,
                    "entities_with_proof": 0,
                    "entities_with_outcomes": 0,
                    "artifact_count": 0,
                    "by_type": {},
                    "by_state": {},
                    "by_stage": {},
                    "by_next_action": {},
                    "by_next_action_actor": {},
                },
                "entity_states": [],
                "entity_next_actions": [],
                "_cycle_seconds": [],
                "_cards_with_artifacts": set(),
                "_cards_with_proof": set(),
                "_cards_with_outcomes": set(),
                "_entities_by_key": {},
            },
        )

        runs = list_runs(conn, task.id)
        events = list_events(conn, task.id)
        signals = _funnel_task_signals(task, runs, events)
        cycle_seconds = _funnel_cycle_seconds(task, runs)
        blocker = _funnel_latest_blocker(task, events)
        latest_run = runs[-1] if runs else None
        parents = parents_by_child.get(task.id, [])
        children = children_by_parent.get(task.id, [])
        watch_routes = watch_routes_by_task.get(task.id, [])

        status_counts = stage["counts_by_status"]
        status_counts[task.status] = int(status_counts.get(task.status, 0)) + 1
        summary_status[task.status] = int(summary_status.get(task.status, 0)) + 1
        semantic_sources[node["semantic_source"]] = (
            int(semantic_sources.get(node["semantic_source"], 0)) + 1
        )

        metrics = stage["metrics"]
        metrics["total_cards"] += 1
        if task.status in _FUNNEL_WAITING_STATUSES:
            metrics["waiting_cards"] += 1
        if task.status == "watching":
            metrics["watching_cards"] += 1
        if task.status in _FUNNEL_ACTIVE_STATUSES:
            metrics["active_cards"] += 1
        if task.status == "blocked":
            metrics["blocked_cards"] += 1
        if task.status == "done":
            metrics["done_cards"] += 1
            metrics["completed_count"] += 1
        metrics["failure_count"] += signals["failure_count"]
        metrics["artifact_count"] += len(signals["artifacts"])
        if signals["artifacts"]:
            stage["_cards_with_artifacts"].add(task.id)
        if signals["proof"]:
            stage["_cards_with_proof"].add(task.id)
        if signals["outcomes"]:
            stage["_cards_with_outcomes"].add(task.id)
        if cycle_seconds is not None:
            stage["_cycle_seconds"].append(cycle_seconds)
        if blocker:
            stage["blockers"].append(blocker)
        for entity in signals["entities"]:
            entity = dict(entity)
            if not entity.get("stage_key"):
                entity["stage_key"] = node["stage_key"]
            stage["_entities_by_key"][_funnel_entity_dedupe_key(entity)] = entity

        card = {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "assignee": task.assignee,
            "priority": task.priority,
            "tenant": task.tenant,
            "created_by": task.created_by,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "completed_at": task.completed_at,
            "cycle_seconds": cycle_seconds,
            "current_run_id": task.current_run_id,
            "latest_run": (
                {
                    "id": latest_run.id,
                    "profile": latest_run.profile,
                    "status": latest_run.status,
                    "outcome": latest_run.outcome,
                    "started_at": latest_run.started_at,
                    "ended_at": latest_run.ended_at,
                }
                if latest_run else None
            ),
            "parents": parents,
            "children": children,
            "watch_routes": watch_routes,
            "runtime": {
                "lifecycle_status": task.status,
                "waiting_reason": watch_routes[0].get("reason") if watch_routes else None,
                "next_expected_event": (
                    watch_routes[0].get("trigger_type") if watch_routes else None
                ),
                "owner": task.assignee,
            },
            "semantic": node,
            "funnel_data": task.funnel_data,
            "signals": {
                "artifact_count": len(signals["artifacts"]),
                "artifacts": signals["artifacts"],
                "proof_keys": sorted({item["key"] for item in signals["proof"]}),
                "outcome_keys": sorted({item["key"] for item in signals["outcomes"]}),
                "latest_summary": signals["latest_summary"],
                "failure_count": signals["failure_count"],
                "failed_runs": signals["failed_runs"],
                "entity_count": len(signals["entities"]),
            },
            "blocker": blocker,
        }
        if card_limit is None or len(stage["cards"]) < card_limit:
            stage["cards"].append(card)
        else:
            stage["cards_truncated"] += 1
        if node["semantic_source"] == "fallback":
            uncategorized.append({
                "id": task.id,
                "title": task.title,
                "status": task.status,
                "assignee": task.assignee,
            })

    edge_map: dict[tuple[str, str], dict] = {}
    for link in links:
        parent_id = link["parent_id"]
        child_id = link["child_id"]
        parent_node = node_by_task.get(parent_id)
        child_node = node_by_task.get(child_id)
        if not parent_node or not child_node or parent_node == child_node:
            continue
        edge = edge_map.setdefault(
            (parent_node, child_node),
            {"from": parent_node, "to": child_node, "count": 0, "task_edges": []},
        )
        edge["count"] += 1
        edge["task_edges"].append({"parent_id": parent_id, "child_id": child_id})

    stage_list: list[dict] = []
    for stage in stages.values():
        metrics = stage["metrics"]
        cycles = stage.pop("_cycle_seconds")
        metrics["avg_cycle_seconds"] = (
            int(sum(cycles) / len(cycles)) if cycles else None
        )
        metrics["cards_with_artifacts"] = len(stage.pop("_cards_with_artifacts"))
        metrics["cards_with_proof"] = len(stage.pop("_cards_with_proof"))
        metrics["cards_with_outcomes"] = len(stage.pop("_cards_with_outcomes"))
        stage["counts_by_status"] = {
            status: stage["counts_by_status"][status]
            for status in sorted(stage["counts_by_status"])
        }

        entities = sorted(
            stage.pop("_entities_by_key").values(),
            key=lambda e: (
                str(e.get("stage_key") or ""),
                str(e.get("type") or "entity"),
                str(e.get("state") or "unspecified"),
                str(e.get("id") or ""),
            ),
        )
        entity_metrics = stage["entity_metrics"]
        entity_metrics["total_entities"] = len(entities)
        entity_metrics["blocked_entities"] = sum(1 for e in entities if e.get("blocked"))
        entity_metrics["terminal_entities"] = sum(1 for e in entities if e.get("terminal"))
        entity_metrics["entities_with_next_action"] = sum(
            1 for e in entities if _funnel_value_present(e.get("next_action_key"))
        )
        entity_metrics["entities_with_artifacts"] = sum(
            1 for e in entities if int(e.get("artifact_count") or 0) > 0
        )
        entity_metrics["entities_with_proof"] = sum(
            1 for e in entities if e.get("proof_keys")
        )
        entity_metrics["entities_with_outcomes"] = sum(
            1 for e in entities if e.get("outcome_keys")
        )
        entity_metrics["artifact_count"] = sum(
            int(e.get("artifact_count") or 0) for e in entities
        )
        entity_metrics["by_type"] = _funnel_count(e.get("type") for e in entities)
        entity_metrics["by_state"] = _funnel_count(e.get("state") for e in entities)
        entity_metrics["by_stage"] = _funnel_count(e.get("stage_key") for e in entities)
        entity_metrics["by_next_action"] = _funnel_count(
            e.get("next_action_key") for e in entities
        )
        entity_metrics["by_next_action_actor"] = _funnel_count(
            e.get("next_action_actor") for e in entities
        )

        state_buckets: dict[str, dict] = {}
        action_buckets: dict[str, dict] = {}
        for entity in entities:
            state_key = str(entity.get("state") or "unspecified")
            state_bucket = state_buckets.setdefault(
                state_key,
                {
                    "state": state_key,
                    "count": 0,
                    "blocked": 0,
                    "terminal": 0,
                    "with_next_action": 0,
                    "by_type": {},
                },
            )
            state_bucket["count"] += 1
            if entity.get("blocked"):
                state_bucket["blocked"] += 1
            if entity.get("terminal"):
                state_bucket["terminal"] += 1
            if _funnel_value_present(entity.get("next_action_key")):
                state_bucket["with_next_action"] += 1
            _funnel_increment_count(state_bucket["by_type"], entity.get("type"))

            action_key = entity.get("next_action_key")
            if _funnel_value_present(action_key):
                action_bucket = action_buckets.setdefault(
                    str(action_key),
                    {"action": str(action_key), "count": 0, "by_actor": {}, "by_state": {}},
                )
                action_bucket["count"] += 1
                _funnel_increment_count(action_bucket["by_actor"], entity.get("next_action_actor"))
                _funnel_increment_count(action_bucket["by_state"], entity.get("state"))

        stage["entity_states"] = [
            {
                **bucket,
                "by_type": {k: bucket["by_type"][k] for k in sorted(bucket["by_type"])},
            }
            for _, bucket in sorted(state_buckets.items())
        ]
        stage["entity_next_actions"] = [
            {
                **bucket,
                "by_actor": {k: bucket["by_actor"][k] for k in sorted(bucket["by_actor"])},
                "by_state": {k: bucket["by_state"][k] for k in sorted(bucket["by_state"])},
            }
            for _, bucket in sorted(action_buckets.items())
        ]
        if entity_limit is None or len(entities) <= entity_limit:
            stage["entities"] = entities
        else:
            stage["entities"] = entities[:entity_limit]
            stage["entities_truncated"] = len(entities) - entity_limit
        stage_list.append(stage)

    stage_list.sort(
        key=lambda s: (s["goal_id"], s["workstream_id"], s["stage_key"], s["action_key"])
    )
    workflow_def = read_board_metadata(board_slug).get("workflow")
    workflow_stage_coverage: list[dict[str, Any]] = []
    if isinstance(workflow_def, dict):  # schema-aware skeleton stages for optimizer gaps
        counts_by_stage: dict[str, int] = {}
        for stage_row in stage_list:
            key = str(stage_row.get("stage_key") or "")
            counts_by_stage[key] = int(counts_by_stage.get(key, 0)) + int(
                stage_row.get("metrics", {}).get("total_cards", 0)
            )
        default_goal = _normalize_funnel_text(workflow_def.get("goal_id")) or "default"
        present_nodes = {
            (s["goal_id"], s["workstream_id"], s["stage_key"], s["action_key"])
            for s in stage_list
        }
        for wf_stage in workflow_def.get("stages") or []:
            if not isinstance(wf_stage, dict):
                continue
            stage_key = str(wf_stage.get("key") or "").strip()
            if not stage_key:
                continue
            card_count = int(counts_by_stage.get(stage_key, 0))
            workflow_stage_coverage.append({
                "stage_key": stage_key,
                "label": wf_stage.get("label"),
                "card_count": card_count,
                "gap": card_count == 0,
            })
            skeleton_key = (default_goal, "__workflow__", stage_key, "__none__")
            if skeleton_key in present_nodes:
                continue
            stage_list.append({
                "id": (
                    f"goal={default_goal}|workstream=__workflow__|"
                    f"stage={stage_key}|action=__none__"
                ),
                "goal_id": default_goal,
                "workstream_id": "__workflow__",
                "stage_key": stage_key,
                "action_key": "__none__",
                "semantic_source": "workflow_skeleton",
                "counts_by_status": {},
                "metrics": {
                    "total_cards": 0,
                    "waiting_cards": 0,
                    "watching_cards": 0,
                    "active_cards": 0,
                    "blocked_cards": 0,
                    "done_cards": 0,
                    "failure_count": 0,
                    "completed_count": 0,
                    "artifact_count": 0,
                    "cards_with_artifacts": 0,
                    "cards_with_proof": 0,
                    "cards_with_outcomes": 0,
                    "avg_cycle_seconds": None,
                },
                "blockers": [],
                "cards": [],
                "cards_truncated": 0,
                "entities": [],
                "entities_truncated": 0,
                "entity_metrics": {
                    "total_entities": 0,
                    "blocked_entities": 0,
                    "terminal_entities": 0,
                    "entities_with_next_action": 0,
                    "entities_with_artifacts": 0,
                    "entities_with_proof": 0,
                    "entities_with_outcomes": 0,
                    "artifact_count": 0,
                    "by_type": {},
                    "by_state": {},
                    "by_stage": {},
                    "by_next_action": {},
                    "by_next_action_actor": {},
                },
                "entity_states": [],
                "entity_next_actions": [],
            })
            present_nodes.add(skeleton_key)
        stage_list.sort(
            key=lambda s: (s["goal_id"], s["workstream_id"], s["stage_key"], s["action_key"])
        )
    edges = sorted(edge_map.values(), key=lambda e: (e["from"], e["to"]))
    summary = {
        "total_cards": sum(summary_status.values()),
        "counts_by_status": {
            status: summary_status[status] for status in sorted(summary_status)
        },
        "stage_count": len(stage_list),
        "edge_count": len(edges),
        "semantic_sources": {
            source: semantic_sources[source] for source in sorted(semantic_sources)
        },
        "active_cards": sum(
            count for status, count in summary_status.items()
            if status in _FUNNEL_ACTIVE_STATUSES
        ),
        "waiting_cards": sum(
            count for status, count in summary_status.items()
            if status in _FUNNEL_WAITING_STATUSES
        ),
        "watching_cards": summary_status.get("watching", 0),
        "blocked_cards": summary_status.get("blocked", 0),
        "done_cards": summary_status.get("done", 0),
        "entity_count": sum(
            s["entity_metrics"].get("total_entities", 0) for s in stage_list
        ),
        "compound_stage_count": sum(
            1 for s in stage_list if s["entity_metrics"].get("total_entities", 0)
        ),
        "blocked_entities": sum(
            s["entity_metrics"].get("blocked_entities", 0) for s in stage_list
        ),
        "terminal_entities": sum(
            s["entity_metrics"].get("terminal_entities", 0) for s in stage_list
        ),
        "entities_with_next_action": sum(
            s["entity_metrics"].get("entities_with_next_action", 0) for s in stage_list
        ),
    }
    semantic_lint = kanban_semantic_diagnostics(
        conn, board=board_slug, include_archived=include_archived,
    )
    out = {
        "version": 1,
        "board": board_slug,
        "workflow": read_board_metadata(board_slug).get("workflow"),
        "generated_at": int(time.time()),
        "include_archived": include_archived,
        "filters": {
            "goal_id": goal_filter,
            "workstream_id": workstream_filter,
            "stage_key": stage_filter,
            "action_key": action_filter,
        },
        "summary": summary,
        "stages": stage_list,
        "edges": edges,
        "workflow_stage_coverage": workflow_stage_coverage,
        "semantic_lint": {
            "issue_count": semantic_lint.get("issue_count", 0),
            "summary": semantic_lint.get("summary"),
            "issues": semantic_lint.get("issues"),
        },
    }
    if uncategorized:
        out["uncategorized"] = uncategorized
    return out


# ---------------------------------------------------------------------------
# Stats + SLA helpers
# ---------------------------------------------------------------------------

def board_stats(conn: sqlite3.Connection) -> dict:
    """Per-status + per-assignee counts, plus the oldest ``ready`` age in
    seconds (the clearest staleness signal for a router or HUD).
    """
    by_status: dict[str, int] = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' GROUP BY status"
    ):
        by_status[row["status"]] = int(row["n"])

    by_assignee: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        by_assignee.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])

    oldest_row = conn.execute(
        "SELECT MIN(created_at) AS ts FROM tasks WHERE status = 'ready'"
    ).fetchone()
    now = int(time.time())
    oldest_ready_age = (
        (now - int(oldest_row["ts"]))
        if oldest_row and oldest_row["ts"] is not None else None
    )

    return {
        "by_status": by_status,
        "by_assignee": by_assignee,
        "oldest_ready_age_seconds": oldest_ready_age,
        "now": now,
    }


def _to_epoch(val) -> Optional[int]:
    """Normalise a timestamp to unix epoch seconds.

    Accepts ints (pass-through), numeric strings, and ISO-8601 strings.
    Returns ``None`` for ``None`` / empty values.
    """
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    # ISO-8601 fallback (e.g. '2026-05-10T15:00:00Z')
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, OSError):
        return None


def task_age(task: Task) -> dict:
    """Return age metrics for a single task. All values are seconds or None."""
    now = int(time.time())
    _c = _to_epoch(task.created_at)
    _s = _to_epoch(task.started_at)
    _co = _to_epoch(task.completed_at)
    age_since_created = now - _c if _c is not None else None
    age_since_started = now - _s if _s is not None else None
    time_to_complete = (
        _co - (_s or _c) if _co is not None else None
    )
    return {
        "created_age_seconds": age_since_created,
        "started_age_seconds": age_since_started,
        "time_to_complete_seconds": time_to_complete,
    }


# ---------------------------------------------------------------------------
# Notification subscriptions (used by the gateway kanban-notifier)
# ---------------------------------------------------------------------------

def add_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    notifier_profile: Optional[str] = None,
) -> None:
    """Register a gateway source that wants terminal-state notifications
    for ``task_id``. Idempotent on (task, platform, chat, thread)."""
    now = int(time.time())
    with write_txn(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO kanban_notify_subs
                (task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, platform, chat_id, thread_id or "", user_id, notifier_profile, now),
        )
        if notifier_profile:
            # Self-heal legacy rows that predate notifier ownership by
            # backfilling only when the existing value is unset.
            conn.execute(
                """
                UPDATE kanban_notify_subs
                   SET notifier_profile = ?
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                   AND (notifier_profile IS NULL OR notifier_profile = '')
                """,
                (notifier_profile, task_id, platform, chat_id, thread_id or ""),
            )


def list_notify_subs(
    conn: sqlite3.Connection, task_id: Optional[str] = None,
) -> list[dict]:
    if task_id is not None:
        rows = conn.execute(
            "SELECT * FROM kanban_notify_subs WHERE task_id = ?", (task_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM kanban_notify_subs").fetchall()
    return [dict(r) for r in rows]


def remove_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM kanban_notify_subs WHERE task_id = ? "
            "AND platform = ? AND chat_id = ? AND thread_id = ?",
            (task_id, platform, chat_id, thread_id or ""),
        )
    return cur.rowcount > 0


def unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
) -> tuple[int, list[Event]]:
    """Return ``(new_cursor, events)`` for a given subscription.

    Only events with ``id > last_event_id`` are returned. The subscription's
    cursor is NOT advanced here; call :func:`advance_notify_cursor` after
    the gateway has successfully delivered the notifications.
    """
    row = conn.execute(
        "SELECT last_event_id FROM kanban_notify_subs "
        "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
        (task_id, platform, chat_id, thread_id or ""),
    ).fetchone()
    if row is None:
        return 0, []
    cursor = int(row["last_event_id"])
    kind_list = list(kinds) if kinds else None
    q = (
        "SELECT * FROM task_events WHERE task_id = ? AND id > ? "
        + ("AND kind IN (" + ",".join("?" * len(kind_list)) + ") " if kind_list else "")
        + "ORDER BY id ASC"
    )
    params: list[Any] = [task_id, cursor]
    if kind_list:
        params.extend(kind_list)
    rows = conn.execute(q, params).fetchall()
    out: list[Event] = []
    max_id = cursor
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except Exception:
            payload = None
        out.append(Event(
            id=r["id"], task_id=r["task_id"], kind=r["kind"],
            payload=payload, created_at=r["created_at"],
            run_id=(int(r["run_id"]) if "run_id" in r.keys() and r["run_id"] is not None else None),
        ))
        max_id = max(max_id, int(r["id"]))
    return max_id, out


def claim_unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
) -> tuple[int, int, list[Event]]:
    """Atomically claim unseen notification events for one subscription.

    Returns ``(old_cursor, new_cursor, events)``. When events are returned,
    ``kanban_notify_subs.last_event_id`` has already been advanced to
    ``new_cursor`` inside a ``BEGIN IMMEDIATE`` transaction. That makes the
    notifier's read/claim step single-owner across multiple gateway watcher
    processes pointed at the same board DB: concurrent watchers serialize on
    SQLite's writer lock, and only the first process sees and claims a given
    event range.

    Callers should send the claimed events, then either leave the cursor at
    ``new_cursor`` on success or call :func:`rewind_notify_cursor` if delivery
    failed before any terminal unsubscribe removed the row.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT last_event_id FROM kanban_notify_subs "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (task_id, platform, chat_id, thread_id or ""),
        ).fetchone()
        if row is None:
            return 0, 0, []
        old_cursor = int(row["last_event_id"])
        new_cursor, events = unseen_events_for_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            kinds=kinds,
        )
        if not events:
            return old_cursor, old_cursor, []
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? "
            "AND last_event_id = ?",
            (int(new_cursor), task_id, platform, chat_id, thread_id or "", int(old_cursor)),
        )
        return old_cursor, new_cursor, events


def advance_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    new_cursor: int,
) -> None:
    with write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (int(new_cursor), task_id, platform, chat_id, thread_id or ""),
        )


def rewind_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    claimed_cursor: int,
    old_cursor: int,
) -> bool:
    """Undo a notification claim when delivery fails.

    The CAS guard only rewinds if no later notifier advanced the row after our
    claim. This keeps retry behavior for transient send failures without
    clobbering newer progress.
    """
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? "
            "AND last_event_id = ?",
            (
                int(old_cursor), task_id, platform, chat_id, thread_id or "",
                int(claimed_cursor),
            ),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Retention + garbage collection
# ---------------------------------------------------------------------------

def gc_events(
    conn: sqlite3.Connection, *, older_than_seconds: int = 30 * 24 * 3600,
) -> int:
    """Delete task_events rows older than ``older_than_seconds`` for tasks
    in a terminal state (``done`` or ``archived``). Returns the number of
    rows deleted. Running / ready / blocked tasks keep their full event
    history."""
    cutoff = int(time.time()) - int(older_than_seconds)
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_events WHERE created_at < ? AND task_id IN "
            "(SELECT id FROM tasks WHERE status IN ('done', 'archived'))",
            (cutoff,),
        )
    return int(cur.rowcount or 0)


def gc_worker_logs(
    *, older_than_seconds: int = 30 * 24 * 3600,
    board: Optional[str] = None,
) -> int:
    """Delete worker log files older than ``older_than_seconds``. Returns
    the number of files removed. Kept separate from ``gc_events`` because
    log files live on disk, not in SQLite. Scoped to ``board`` (defaults
    to the active board) — per-board isolation means deleting logs from
    board A cannot touch board B's logs."""
    log_dir = worker_logs_dir(board=board)
    if not log_dir.exists():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for p in log_dir.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# Worker log accessor
# ---------------------------------------------------------------------------

def worker_log_path(task_id: str, *, board: Optional[str] = None) -> Path:
    """Return the path to a worker's log file. The file may not exist
    (task never spawned, or log already GC'd).

    When ``board`` is None, resolves via the active board (env var →
    current-board file → default). The dispatcher always passes the
    board explicitly to avoid any resolution ambiguity when multiple
    boards exist."""
    return worker_logs_dir(board=board) / f"{task_id}.log"


def read_worker_log(
    task_id: str, *, tail_bytes: Optional[int] = None,
    board: Optional[str] = None,
) -> Optional[str]:
    """Read the worker log for ``task_id``. Returns None if the file
    doesn't exist. If ``tail_bytes`` is set, only the last N bytes are
    returned (useful for the dashboard drawer which shouldn't page megabytes)."""
    path = worker_log_path(task_id, board=board)
    if not path.exists():
        return None
    try:
        if tail_bytes is None:
            return path.read_text(encoding="utf-8", errors="replace")
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                # Skip a partial line if we tailed mid-line. But if the
                # window has no newline at all (one giant log line),
                # readline() would eat everything — in that case don't
                # skip and return the raw tail.
                probe = f.tell()
                partial = f.readline()
                if not partial.endswith(b"\n") and f.tell() >= size:
                    f.seek(probe)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Assignee enumeration (known profiles + per-profile board stats)
# ---------------------------------------------------------------------------

def list_profiles_on_disk() -> list[str]:
    """Return the set of assignee/profile names discovered on disk.

    Includes:
    - named profiles under ``<default-root>/profiles/<name>/config.yaml``
    - the implicit ``default`` profile when the default Hermes root exists

    Reads profile paths directly so this module has no import dependency on
    ``hermes_cli.profiles`` (which pulls in a large chunk of the CLI startup
    path).
    """
    try:
        from hermes_constants import get_default_hermes_root
        default_root = get_default_hermes_root()
        profiles_dir = default_root / "profiles"
    except Exception:
        return []

    names: set[str] = set()
    if default_root.exists():
        names.add("default")

    if profiles_dir.is_dir():
        try:
            for entry in sorted(profiles_dir.iterdir()):
                if not entry.is_dir():
                    continue
                if (entry / "config.yaml").is_file():
                    names.add(entry.name)
        except OSError:
            pass

    return sorted(names)


def known_assignees(conn: sqlite3.Connection) -> list[dict]:
    """Return every assignee name known to the board or on disk.

    Each entry is ``{"name": str, "on_disk": bool, "counts": {status: n}}``.
    A name is included when it's a configured profile on disk OR when
    any non-archived task has it as the assignee. Used by:

    - ``hermes kanban assignees`` for the terminal.
    - The dashboard assignee dropdown (so a fresh profile appears in
      the picker even before it's been given any task).
    - Router-profile heuristics ("who's overloaded?") without scanning
      the whole board.
    """
    on_disk = set(list_profiles_on_disk())

    # Count tasks per (assignee, status), excluding archived.
    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        counts.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])

    names = sorted(on_disk | set(counts.keys()))
    return [
        {
            "name": name,
            "on_disk": name in on_disk,
            "counts": counts.get(name, {}),
        }
        for name in names
    ]


# ---------------------------------------------------------------------------
# Runs (attempt history on a task)
# ---------------------------------------------------------------------------

def list_runs(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    include_active: bool = True,
    state_type: Optional[str] = None,
    state_name: Optional[str] = None,
) -> list[Run]:
    """Return all runs for ``task_id`` in start order.

    ``include_active=True`` (default) includes the currently-running
    attempt if any. Set False to return only closed runs (useful for
    "how many prior attempts have there been?" checks).

    When ``state_type`` and ``state_name`` are set, restrict to rows
    where that column equals ``state_name`` (``state_type`` is
    ``status`` or ``outcome``). Both must be passed together.
    """
    if (state_type is None) ^ (state_name is None):
        raise ValueError("state_type and state_name must both be set or both omitted")
    if state_type is not None:
        if state_type not in ("status", "outcome"):
            raise ValueError("state_type must be 'status' or 'outcome'")
    q = "SELECT * FROM task_runs WHERE task_id = ?"
    params: list[Any] = [task_id]
    if not include_active:
        q += " AND ended_at IS NOT NULL"
    if state_type is not None:
        q += f" AND {state_type} = ?"
        params.append(state_name)
    q += " ORDER BY started_at ASC, id ASC"
    rows = conn.execute(q, params).fetchall()
    return [Run.from_row(r) for r in rows]


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[Run]:
    row = conn.execute(
        "SELECT * FROM task_runs WHERE id = ?", (int(run_id),),
    ).fetchone()
    return Run.from_row(row) if row else None


def active_run(conn: sqlite3.Connection, task_id: str) -> Optional[Run]:
    """Return the currently-open run for ``task_id`` (``ended_at IS NULL``)."""
    row = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? AND ended_at IS NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return Run.from_row(row) if row else None


def latest_run(conn: sqlite3.Connection, task_id: str) -> Optional[Run]:
    """Return the most recent run regardless of outcome (active or closed)."""
    row = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return Run.from_row(row) if row else None


def latest_summary(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Return the latest non-null ``task_runs.summary`` for ``task_id``.

    The kanban-worker skill writes its handoff to ``task_runs.summary``
    via ``complete_task(summary=...)``; ``tasks.result`` is left empty
    unless the caller passes ``result=`` explicitly. Dashboards and CLI
    "show" views need this value to surface what a worker actually did
    — without it, ``tasks.result`` is NULL and the task looks like a
    no-op even when the run completed.

    Picks the most recent run by ``ended_at`` (falling back to ``id``
    for ties or unfinished rows). Returns None if no run has a summary.
    """
    row = conn.execute(
        "SELECT summary FROM task_runs "
        "WHERE task_id = ? AND summary IS NOT NULL AND summary != '' "
        "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["summary"] if row else None


def latest_summaries(
    conn: sqlite3.Connection, task_ids: Iterable[str]
) -> dict[str, str]:
    """Batch-fetch latest non-null summaries for a list of task ids.

    Used by the dashboard board endpoint to attach ``latest_summary`` to
    every card in a single SQL query, avoiding the N+1 pattern of
    calling :func:`latest_summary` per task. Returns a dict mapping
    ``task_id`` → summary string, omitting tasks with no summary.

    Approach: a window function picks the newest non-null-summary row
    per ``task_id``; works against SQLite ≥ 3.25 (default on every
    supported platform).
    """
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT task_id, summary FROM (
            SELECT task_id, summary,
                   ROW_NUMBER() OVER (
                       PARTITION BY task_id
                       ORDER BY COALESCE(ended_at, started_at) DESC, id DESC
                   ) AS rn
              FROM task_runs
             WHERE task_id IN ({placeholders})
               AND summary IS NOT NULL AND summary != ''
        ) WHERE rn = 1
        """,
        ids,
    ).fetchall()
    return {r["task_id"]: r["summary"] for r in rows}
