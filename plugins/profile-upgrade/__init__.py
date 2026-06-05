"""Profile Upgrade Plugin

Narrow, approval-gated profile upgrade proposals for orchestrator profiles.

The active profile is the command post. This plugin deliberately does NOT arm
that orchestrator with direct execution tools. It stores proposals under the
active profile's state directory, but every proposal must name a delegated worker
profile to upgrade. Persistent writes happen only after an external owner
approval marker is present.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from hermes_constants import get_hermes_home

PLUGIN_NAME = "profile-upgrade"
PLUGIN_VERSION = "1.1.0"

# Worker profiles are allowed to be armed with execution capabilities. The
# active orchestrator is not. External/live-side-effect toolsets stay blocked by
# this narrow upgrade path unless a future tool adds a more specific approval
# contract for them.
WORKER_ALLOWED_TOOLSETS = {
    "web",
    "browser",
    "terminal",
    "file",
    "code_execution",
    "computer_use",
    "vision",
    "skills",
    "memory",
    "session_search",
    "clarify",
    "todo",
    "kanban",
}

BLOCKED_WORKER_TOOLSETS = {
    "messaging",
    "cronjob",
    "discord",
    "discord_admin",
    "homeassistant",
    "spotify",
    "feishu_doc",
    "feishu_drive",
    "yuanbao",
    "image_gen",
    "video",
    "video_gen",
    "tts",
}

PROTECTED_PROFILE_NAMES = {"default", "development"}

ALLOWED_SET_PATH_PREFIXES = (
    "browser.",
    "web.",
    "terminal.",
    "delegation.",
    "skills.prompt_budget.",
    "skills.router.",
    "agent.environment_hint",
    "agent.environment_probe",
    "agent.disabled_toolsets",
    "platform_toolsets.",
)

ALLOWED_MCP_COMMANDS = {"uvx", "npx", "node", "python3", "python", "/usr/bin/env"}
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")
_SAFE_ARG_RE = re.compile(r"^[^;&|`$<>\n\r]{0,500}$")


def _home() -> Path:
    return Path(get_hermes_home()).expanduser().resolve()


def _active_profile_name() -> str:
    for key in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    home = _home()
    if home.parent.name == "profiles":
        return home.name
    return "default"


def _base_hermes_home() -> Path:
    home = _home()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _config_path() -> Path:
    """Active/orchestrator config path. Used for state ownership only."""
    return _home() / "config.yaml"


def _target_profile_home(target_profile: str) -> Path:
    name = str(target_profile or "").strip()
    if not name:
        raise ValueError("target_profile is required")
    if name in {"self", "current", "active", "orchestrator"}:
        raise ValueError("target_profile must be a named delegated worker profile, not the active orchestrator")
    if not _SAFE_NAME_RE.match(name):
        raise ValueError("target_profile must be safe ascii [a-zA-Z0-9_.-]")
    active = _active_profile_name()
    if name == active:
        raise ValueError(f"refusing to upgrade active orchestrator profile '{active}'")
    if name in PROTECTED_PROFILE_NAMES:
        raise ValueError(f"refusing to upgrade protected non-worker profile '{name}'")
    target_home = _base_hermes_home() / "profiles" / name
    if not (target_home / "config.yaml").exists():
        raise ValueError(f"target worker profile '{name}' does not exist or has no config.yaml")
    return target_home.resolve()


def _target_config_path(target_profile: str) -> Path:
    return _target_profile_home(target_profile) / "config.yaml"


def _active_config() -> Dict[str, Any]:
    return _load_yaml(_config_path())


def _worker_scope_config() -> Dict[str, Any]:
    cfg = _active_config().get("worker_scope") or {}
    if not isinstance(cfg, dict):
        raise ValueError("worker_scope must be a mapping")
    if cfg.get("enabled") is not True:
        raise ValueError("worker_scope.enabled must be true before worker-profile upgrades are allowed")
    return cfg


def _normalize_scope(scope_type: Any, scope_id: Any, cfg: Dict[str, Any]) -> Tuple[str, str]:
    st = str(scope_type or "").strip()
    sid = str(scope_id or "").strip()
    raw_default = cfg.get("default_scope")
    default: Dict[str, Any] = raw_default if isinstance(raw_default, dict) else {}
    if not st:
        st = str(default.get("type") or "").strip()
    if not sid:
        sid = str(default.get("id") or "").strip()
    if st not in {"tenant", "board"}:
        raise ValueError("scope_type must be 'tenant' or 'board'")
    if not sid or not _SAFE_NAME_RE.match(sid):
        raise ValueError("scope_id must be a safe non-empty scope identifier")
    return st, sid


def _find_worker_scope(scope_type: str, scope_id: str) -> Dict[str, Any]:
    cfg = _worker_scope_config()
    scope_type, scope_id = _normalize_scope(scope_type, scope_id, cfg)
    scopes = cfg.get("scopes") or []
    if not isinstance(scopes, list):
        raise ValueError("worker_scope.scopes must be a list")
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        if str(scope.get("type") or "") == scope_type and str(scope.get("id") or "") == scope_id:
            return scope
    raise ValueError(f"no worker scope configured for {scope_type}:{scope_id}")


def _require_target_in_worker_scope(target_profile: str, scope_type: Any, scope_id: Any) -> Tuple[str, str, Dict[str, Any]]:
    cfg = _worker_scope_config()
    scope_type, scope_id = _normalize_scope(scope_type, scope_id, cfg)
    scope = _find_worker_scope(scope_type, scope_id)
    workers = scope.get("workers") or {}
    if not isinstance(workers, dict):
        raise ValueError("scope.workers must be a mapping")
    if target_profile not in workers:
        allowed = ", ".join(sorted(str(k) for k in workers.keys())) or "(none)"
        raise ValueError(f"target_profile '{target_profile}' is not authorized for {scope_type}:{scope_id}; allowed: {allowed}")
    policy = workers.get(target_profile)
    return scope_type, scope_id, policy if isinstance(policy, dict) else {}


def _state_dir() -> Path:
    path = _home() / "profile_upgrades"
    path.mkdir(parents=True, exist_ok=True)
    (path / "proposals").mkdir(exist_ok=True)
    (path / "archive").mkdir(exist_ok=True)
    return path


def _proposal_path(proposal_id: str) -> Path:
    if not _SAFE_NAME_RE.match(proposal_id):
        raise ValueError("invalid proposal_id")
    return _state_dir() / "proposals" / f"{proposal_id}.json"


def _load_yaml(path: Path) -> Dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is not available")
    if not path.exists():
        raise FileNotFoundError(str(path))
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")
    return data


def _dump_yaml(path: Path, data: Dict[str, Any]) -> None:
    if yaml is None:
        raise RuntimeError("PyYAML is not available")
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    path.write_text(text)


def _get_path(data: Dict[str, Any], dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set_path(data: Dict[str, Any], dotted: str, value: Any) -> None:
    cur: Dict[str, Any] = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _ensure_list(data: Dict[str, Any], dotted: str) -> List[Any]:
    cur = _get_path(data, dotted)
    if isinstance(cur, list):
        return cur
    _set_path(data, dotted, [])
    ensured = _get_path(data, dotted)
    if not isinstance(ensured, list):
        raise ValueError(f"failed to create list at {dotted}")
    return ensured


def _canonical_change(change: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(change, sort_keys=True, separators=(",", ":")))


def _proposal_digest(payload: Dict[str, Any]) -> str:
    relevant = {
        "target_profile": payload.get("target_profile", ""),
        "scope_type": payload.get("scope_type", ""),
        "scope_id": payload.get("scope_id", ""),
        "goal": payload.get("goal", ""),
        "blocker": payload.get("blocker", ""),
        "worker_blocker_evidence": payload.get("worker_blocker_evidence", ""),
        "changes": [_canonical_change(c) for c in payload.get("changes", [])],
    }
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:16]


def _validate_mcp_server(server: Dict[str, Any]) -> Tuple[bool, str]:
    name = server.get("name") or server.get("server_name")
    if not name or not _SAFE_NAME_RE.match(str(name)):
        return False, "mcp server name must be safe ascii [a-zA-Z0-9_.-]"
    command = str(server.get("command", ""))
    if not command:
        return False, "mcp server command is required"
    if command not in ALLOWED_MCP_COMMANDS and not command.startswith("/Users/") and not command.startswith("/usr/") and not command.startswith("/opt/homebrew/"):
        return False, f"mcp command '{command}' is not allowed"
    args = server.get("args", [])
    if args is None:
        args = []
    if not isinstance(args, list):
        return False, "mcp args must be a list"
    for arg in args:
        if not _SAFE_ARG_RE.match(str(arg)):
            return False, f"unsafe mcp arg: {arg!r}"
    env = server.get("env", {})
    if env and not isinstance(env, dict):
        return False, "mcp env must be a mapping"
    for key, value in (env or {}).items():
        if not re.match(r"^[A-Z0-9_]{1,80}$", str(key)):
            return False, f"unsafe env key: {key!r}"
        # Values should normally be env var names or placeholders, not raw secrets.
        if value and re.search(r"sk-|ghp_|Bearer\s+|api[_-]?key", str(value), re.I):
            return False, "do not embed raw secrets in mcp env; use existing env var names/placeholders"
    return True, "ok"


def _validate_change(change: Dict[str, Any]) -> Tuple[bool, str, str]:
    """Return (ok, reason, risk)."""
    if not isinstance(change, dict):
        return False, "change must be an object", "high"
    kind = change.get("change_type") or change.get("type")
    if not kind:
        return False, "change_type is required", "high"

    if kind == "enable_toolset":
        name = change.get("name")
        if name in BLOCKED_WORKER_TOOLSETS:
            return False, f"refusing broad live-side-effect toolset '{name}' via worker upgrade path", "high"
        if name not in WORKER_ALLOWED_TOOLSETS:
            return False, f"toolset '{name}' is not in delegated-worker allowlist", "medium"
        return True, "ok", "medium" if name in {"terminal", "file", "browser", "code_execution", "computer_use", "kanban"} else "low"

    if kind == "disable_toolset":
        name = change.get("name")
        if not name or not _SAFE_NAME_RE.match(str(name)):
            return False, "safe toolset name required", "medium"
        return True, "ok", "low"

    if kind == "enable_plugin":
        name = change.get("name")
        if not name or not _SAFE_NAME_RE.match(str(name)):
            return False, "safe plugin name required", "medium"
        return True, "ok", "medium"

    if kind == "disable_plugin":
        name = change.get("name")
        if not name or not _SAFE_NAME_RE.match(str(name)):
            return False, "safe plugin name required", "medium"
        return True, "ok", "medium"

    if kind == "add_mcp_server":
        ok, reason = _validate_mcp_server(change)
        return ok, reason, "high"

    if kind == "remove_mcp_server":
        name = change.get("name") or change.get("server_name")
        if not name or not _SAFE_NAME_RE.match(str(name)):
            return False, "safe mcp server name required", "medium"
        return True, "ok", "medium"

    if kind == "set_config":
        key = str(change.get("key", ""))
        if key in {"toolsets", "plugins.enabled", "plugins.disabled", "mcp_servers"}:
            return False, f"use structured change_type instead of raw set_config for {key}", "medium"
        if not any(key == p.rstrip(".") or key.startswith(p) for p in ALLOWED_SET_PATH_PREFIXES):
            return False, f"config path '{key}' is not allowed", "medium"
        return True, "ok", "medium"

    if kind == "add_skill_always_include":
        name = change.get("name")
        if not name or not _SAFE_NAME_RE.match(str(name)):
            return False, "safe skill name required", "low"
        return True, "ok", "low"

    return False, f"unknown change_type '{kind}'", "high"


def _validate_changes(changes: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    risk_order = {"low": 0, "medium": 1, "high": 2}
    highest = "low"
    for idx, change in enumerate(changes):
        ok, reason, risk = _validate_change(change)
        annotated = copy.deepcopy(change)
        annotated["validation"] = {"ok": ok, "reason": reason, "risk": risk}
        annotated["index"] = idx
        if ok:
            accepted.append(annotated)
            if risk_order[risk] > risk_order[highest]:
                highest = risk
        else:
            rejected.append(annotated)
    return accepted, rejected, highest


def _apply_change(config: Dict[str, Any], change: Dict[str, Any]) -> None:
    kind = change.get("change_type") or change.get("type")

    if kind == "enable_toolset":
        name = change["name"]
        top = config.setdefault("toolsets", [])
        if isinstance(top, list) and name not in top:
            top.append(name)
        disabled = _ensure_list(config, "agent.disabled_toolsets")
        while name in disabled:
            disabled.remove(name)
        cli_toolsets = _ensure_list(config, "platform_toolsets.cli")
        if name not in cli_toolsets:
            cli_toolsets.append(name)
        return

    if kind == "disable_toolset":
        name = change["name"]
        disabled = _ensure_list(config, "agent.disabled_toolsets")
        if name not in disabled:
            disabled.append(name)
        top = config.get("toolsets")
        if isinstance(top, list):
            while name in top:
                top.remove(name)
        cli_toolsets = _get_path(config, "platform_toolsets.cli")
        if isinstance(cli_toolsets, list):
            while name in cli_toolsets:
                cli_toolsets.remove(name)
        return

    if kind == "enable_plugin":
        name = change["name"]
        enabled = _ensure_list(config, "plugins.enabled")
        disabled = _ensure_list(config, "plugins.disabled")
        if name not in enabled:
            enabled.append(name)
        while name in disabled:
            disabled.remove(name)
        return

    if kind == "disable_plugin":
        name = change["name"]
        disabled = _ensure_list(config, "plugins.disabled")
        enabled = _ensure_list(config, "plugins.enabled")
        if name not in disabled:
            disabled.append(name)
        while name in enabled:
            enabled.remove(name)
        return

    if kind == "add_mcp_server":
        name = change.get("name") or change.get("server_name")
        server = {"command": change["command"], "enabled": True}
        if change.get("args") is not None:
            server["args"] = change.get("args") or []
        if change.get("env"):
            server["env"] = change.get("env")
        for optional in ("timeout", "connect_timeout", "url", "headers"):
            if optional in change:
                server[optional] = change[optional]
        config.setdefault("mcp_servers", {})[name] = server
        return

    if kind == "remove_mcp_server":
        name = change.get("name") or change.get("server_name")
        if isinstance(config.get("mcp_servers"), dict):
            config["mcp_servers"].pop(name, None)
        return

    if kind == "set_config":
        _set_path(config, change["key"], change.get("value"))
        return

    if kind == "add_skill_always_include":
        name = change["name"]
        always = _ensure_list(config, "skills.prompt_budget.always_include")
        if name not in always:
            always.append(name)
        pinned = _ensure_list(config, "skills.router.pinned")
        if name not in pinned:
            pinned.append(name)
        return

    raise ValueError(f"unsupported change_type: {kind}")


def _diff_summary(before: Dict[str, Any], after: Dict[str, Any], changes: List[Dict[str, Any]]) -> List[str]:
    summaries: List[str] = []
    for change in changes:
        kind = change.get("change_type") or change.get("type")
        if kind == "set_config":
            key = str(change.get("key") or "")
            before_val = _get_path(before, key)
            after_val = _get_path(after, key)
            summaries.append(f"set {key}: {before_val!r} -> {after_val!r}")
        elif kind == "add_mcp_server":
            summaries.append(f"add/update mcp server: {change.get('name') or change.get('server_name')}")
        elif kind == "remove_mcp_server":
            summaries.append(f"remove mcp server: {change.get('name') or change.get('server_name')}")
        else:
            summaries.append(f"{kind}: {change.get('name', '')}")
    return summaries


PROPOSE_SCHEMA = {
    "name": "profile_upgrade_propose",
    "description": (
        "Create an approval-gated proposal to upgrade a delegated worker profile after a worker attempt is blocked. "
        "Do not use this to enable execution tools on the active orchestrator profile."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target_profile": {"type": "string", "description": "Named delegated worker profile to upgrade. Must be authorized in worker_scope for the given scope."},
            "scope_type": {"type": "string", "description": "Hard worker scope type: tenant or board."},
            "scope_id": {"type": "string", "description": "Tenant id or board slug that owns this worker profile."},
            "goal": {"type": "string", "description": "The user goal or delegated task being blocked."},
            "blocker": {"type": "string", "description": "What blocked completion."},
            "worker_blocker_evidence": {"type": "string", "description": "Concrete evidence from an attempted delegated worker run. Required so upgrades happen after dispatch, not before."},
            "reasoning": {"type": "string", "description": "Why the proposed worker-profile upgrade is the best next tool/config path."},
            "sources": {"type": "array", "items": {"type": "string"}, "description": "URLs or references used to choose the tool/config."},
            "changes": {
                "type": "array",
                "description": "Structured config changes for the target worker profile. Supported change_type: enable_toolset, disable_toolset, enable_plugin, disable_plugin, add_mcp_server, remove_mcp_server, set_config, add_skill_always_include.",
                "items": {"type": "object"},
            },
        },
        "required": ["target_profile", "scope_type", "scope_id", "goal", "blocker", "worker_blocker_evidence", "reasoning", "changes"],
    },
}

STATUS_SCHEMA = {
    "name": "profile_upgrade_status",
    "description": "List worker-profile upgrade proposals or inspect one proposal.",
    "parameters": {
        "type": "object",
        "properties": {
            "proposal_id": {"type": "string", "description": "Optional proposal id to inspect."},
            "limit": {"type": "integer", "description": "Max proposals to list.", "default": 10},
        },
    },
}

APPLY_SCHEMA = {
    "name": "profile_upgrade_apply",
    "description": "Dry-run or apply an approved worker-profile upgrade proposal. Persistent apply requires external owner approval marker.",
    "parameters": {
        "type": "object",
        "properties": {
            "proposal_id": {"type": "string", "description": "Proposal id returned by profile_upgrade_propose."},
            "dry_run": {"type": "boolean", "description": "If true, show what would change without writing config.", "default": True},
        },
        "required": ["proposal_id"],
    },
}


def _handle_propose(args: Dict[str, Any], **kw) -> str:
    target_profile = str(args.get("target_profile") or "").strip()
    try:
        target_home = _target_profile_home(target_profile)
        scope_type, scope_id, worker_policy = _require_target_in_worker_scope(
            target_profile,
            args.get("scope_type"),
            args.get("scope_id"),
        )
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "error": "invalid_target_or_scope",
            "target_profile": target_profile,
            "scope_type": args.get("scope_type"),
            "scope_id": args.get("scope_id"),
            "message": str(exc),
        })

    worker_evidence = str(args.get("worker_blocker_evidence") or "").strip()
    if not worker_evidence:
        return json.dumps({
            "ok": False,
            "error": "worker_blocker_evidence_required",
            "message": "Dispatch an existing worker first. Only propose a worker-profile upgrade after a delegated worker returns concrete blocker evidence.",
        })

    changes = args.get("changes") or []
    if not isinstance(changes, list) or not changes:
        return json.dumps({"ok": False, "error": "changes must be a non-empty list"})

    accepted, rejected, highest_risk = _validate_changes(changes)
    if not accepted:
        return json.dumps({
            "ok": False,
            "error": "no_valid_changes",
            "target_profile": target_profile,
            "rejected_changes": rejected,
            "message": "All proposed changes were rejected by the worker-profile-upgrade guard.",
        })

    created_at = int(time.time())
    proposal = {
        "schema_version": 2,
        "created_at": created_at,
        "status": "pending_owner_approval",
        "orchestrator_profile": _active_profile_name(),
        "profile_home": str(_home()),
        "config_path": str(_config_path()),
        "target_profile": target_profile,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "worker_policy_snapshot": {
            "toolsets": worker_policy.get("toolsets") if isinstance(worker_policy, dict) else None,
            "skills": worker_policy.get("skills") if isinstance(worker_policy, dict) else None,
            "description": worker_policy.get("description") if isinstance(worker_policy, dict) else "",
        },
        "target_profile_home": str(target_home),
        "target_config_path": str(target_home / "config.yaml"),
        "goal": args.get("goal", ""),
        "blocker": args.get("blocker", ""),
        "worker_blocker_evidence": worker_evidence,
        "reasoning": args.get("reasoning", ""),
        "sources": args.get("sources", []),
        "changes": accepted,
        "rejected_changes": rejected,
        "risk": highest_risk,
        "approved": False,
        "approved_at": None,
    }
    digest = _proposal_digest(proposal)
    proposal_id = f"upgrade_{created_at}_{digest}"
    proposal["proposal_id"] = proposal_id
    proposal["approval_digest"] = digest

    path = _proposal_path(proposal_id)
    path.write_text(json.dumps(proposal, indent=2, sort_keys=True))

    approve_cmd = f"python3 {_home() / 'bin' / 'profile_upgrade_approve.py'} {proposal_id}"

    return json.dumps({
        "ok": True,
        "proposal_id": proposal_id,
        "target_profile": target_profile,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "risk": highest_risk,
        "accepted_changes": accepted,
        "rejected_changes": rejected,
        "proposal_path": str(path),
        "approval_required": True,
        "approval_command": approve_cmd,
        "next_step": "Show the owner the blocked worker evidence, target_profile, best fix, and approval_command. After approval, call profile_upgrade_apply, then re-dispatch the worker. Do not enable tools on the orchestrator.",
    })


def _handle_status(args: Dict[str, Any], **kw) -> str:
    proposal_id = args.get("proposal_id")
    if proposal_id:
        path = _proposal_path(proposal_id)
        if not path.exists():
            return json.dumps({"ok": False, "error": "proposal_not_found", "proposal_id": proposal_id})
        return json.dumps({"ok": True, "proposal": json.loads(path.read_text())})

    limit = int(args.get("limit") or 10)
    proposals = []
    for path in sorted((_state_dir() / "proposals").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text())
            proposals.append({
                "proposal_id": data.get("proposal_id"),
                "status": data.get("status"),
                "target_profile": data.get("target_profile"),
                "scope_type": data.get("scope_type"),
                "scope_id": data.get("scope_id"),
                "risk": data.get("risk"),
                "goal": data.get("goal"),
                "blocker": data.get("blocker"),
                "created_at": data.get("created_at"),
                "approved": data.get("approved"),
            })
        except Exception:
            continue
    return json.dumps({"ok": True, "proposals": proposals})


def _handle_apply(args: Dict[str, Any], **kw) -> str:
    proposal_id = args.get("proposal_id")
    if not proposal_id:
        return json.dumps({"ok": False, "error": "proposal_id is required"})
    dry_run = bool(args.get("dry_run", True))

    path = _proposal_path(proposal_id)
    if not path.exists():
        return json.dumps({"ok": False, "error": "proposal_not_found", "proposal_id": proposal_id})
    proposal = json.loads(path.read_text())
    target_profile = str(proposal.get("target_profile") or "").strip()
    scope_type = proposal.get("scope_type")
    scope_id = proposal.get("scope_id")
    try:
        target_config_path = _target_config_path(target_profile)
        scope_type, scope_id, _worker_policy = _require_target_in_worker_scope(target_profile, scope_type, scope_id)
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "error": "invalid_target_or_scope",
            "target_profile": target_profile,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "message": str(exc),
        })

    changes = proposal.get("changes") or []
    accepted, rejected, highest = _validate_changes(changes)
    if rejected:
        return json.dumps({"ok": False, "error": "proposal_no_longer_valid", "rejected_changes": rejected})

    before = _load_yaml(target_config_path)
    after = copy.deepcopy(before)
    for change in accepted:
        _apply_change(after, change)

    summary = _diff_summary(before, after, accepted)

    if dry_run:
        return json.dumps({
            "ok": True,
            "dry_run": True,
            "proposal_id": proposal_id,
            "target_profile": target_profile,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "target_config_path": str(target_config_path),
            "risk": highest,
            "would_change": summary,
            "approved": bool(proposal.get("approved")),
            "message": "Dry run only. Persistent worker-profile apply still requires owner approval marker.",
        })

    if not proposal.get("approved"):
        return json.dumps({
            "ok": False,
            "error": "owner_approval_required",
            "proposal_id": proposal_id,
            "target_profile": target_profile,
            "approval_command": f"python3 {_home() / 'bin' / 'profile_upgrade_approve.py'} {proposal_id}",
            "message": "Persistent worker-profile changes require owner approval outside the model context.",
        })

    backup = _state_dir() / "archive" / f"{target_profile}_config_before_{proposal_id}.yaml"
    backup.write_text(target_config_path.read_text())
    _dump_yaml(target_config_path, after)

    proposal["status"] = "applied"
    proposal["applied_at"] = int(time.time())
    proposal["backup_path"] = str(backup)
    proposal["applied_changes"] = summary
    path.write_text(json.dumps(proposal, indent=2, sort_keys=True))

    return json.dumps({
        "ok": True,
        "dry_run": False,
        "proposal_id": proposal_id,
        "target_profile": target_profile,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "target_config_path": str(target_config_path),
        "applied_changes": summary,
        "backup_path": str(backup),
        "message": "Worker profile upgrade applied. Restart/reset sessions that use the target worker profile for config/tool changes to take effect.",
    })


def register(ctx) -> None:
    ctx.register_tool(
        name="profile_upgrade_propose",
        toolset="worker_upgrade",
        schema=PROPOSE_SCHEMA,
        handler=_handle_propose,
        emoji="🧩",
    )
    ctx.register_tool(
        name="profile_upgrade_status",
        toolset="worker_upgrade",
        schema=STATUS_SCHEMA,
        handler=_handle_status,
        emoji="🧾",
    )
    ctx.register_tool(
        name="profile_upgrade_apply",
        toolset="worker_upgrade",
        schema=APPLY_SCHEMA,
        handler=_handle_apply,
        emoji="🔧",
    )
