"""Scoped Worker Dispatch Plugin.

Hard, config-backed worker scoping for locked orchestrator profiles.

This plugin intentionally replaces raw delegate_task for profiles like
humanless-ai. The model cannot pick arbitrary tools or arbitrary worker
profiles: every dispatch must name a scope and either an authorized worker
profile or the required capabilities/toolsets for policy selection. The worker
runs as a separate Hermes profile process, so its tools/skills come from the
target worker profile plus the subset allowed by the scope policy.
"""

from __future__ import annotations

import json
import importlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from hermes_constants import get_hermes_home

PLUGIN_NAME = "scoped-worker-dispatch"
PLUGIN_VERSION = "1.0.0"

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")
_SAFE_SCOPE_TYPE = {"tenant", "board"}
_DEFAULT_MAX_BATCH = 3
_DEFAULT_TIMEOUT_SECONDS = 600
_OUTPUT_LIMIT = 20000
_EXECUTION_GATE_TOOLS: Any = None


def _json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _execution_gate_tools() -> Any:
    global _EXECUTION_GATE_TOOLS
    if _EXECUTION_GATE_TOOLS is not None:
        return _EXECUTION_GATE_TOOLS

    plugin_dir = Path(__file__).resolve().parent.parent / "execution-gate"
    init_path = plugin_dir / "__init__.py"
    if not init_path.exists():
        raise RuntimeError("execution-gate plugin is required for verified task envelope enforcement")

    package_name = "_scoped_worker_execution_gate"
    if package_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            package_name,
            init_path,
            submodule_search_locations=[str(plugin_dir)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load execution-gate plugin for intake verification")
        module = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = module
        spec.loader.exec_module(module)

    _EXECUTION_GATE_TOOLS = importlib.import_module(f"{package_name}.tools")
    return _EXECUTION_GATE_TOOLS


def _active_profile_name() -> str:
    for key in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    home = Path(get_hermes_home()).expanduser().resolve()
    if home.parent.name == "profiles":
        return home.name
    return "default"


def _base_home() -> Path:
    home = Path(get_hermes_home()).expanduser().resolve()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _profiles_dir() -> Path:
    return _base_home() / "profiles"


def _load_active_config() -> Dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is not available")
    cfg_path = Path(get_hermes_home()).expanduser().resolve() / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError("active config root must be a mapping")
    return data


def _worker_scope_config() -> Dict[str, Any]:
    cfg = _load_active_config().get("worker_scope") or {}
    if not isinstance(cfg, dict):
        raise ValueError("worker_scope must be a mapping")
    if cfg.get("enabled") is not True:
        raise ValueError("worker_scope.enabled must be true for scoped worker dispatch")
    return cfg


def _normalize_scope(scope_type: Optional[str], scope_id: Optional[str], cfg: Dict[str, Any]) -> Tuple[str, str]:
    st = str(scope_type or "").strip()
    sid = str(scope_id or "").strip()
    raw_default = cfg.get("default_scope")
    default: Dict[str, Any] = raw_default if isinstance(raw_default, dict) else {}
    if not st:
        st = str(default.get("type") or "").strip()
    if not sid:
        sid = str(default.get("id") or "").strip()
    if st not in _SAFE_SCOPE_TYPE:
        raise ValueError("scope_type must be 'tenant' or 'board'")
    if not sid or not _SAFE_NAME_RE.match(sid):
        raise ValueError("scope_id must be a safe non-empty scope identifier")
    return st, sid


def _find_scope(cfg: Dict[str, Any], scope_type: str, scope_id: str) -> Dict[str, Any]:
    scopes = cfg.get("scopes") or []
    if not isinstance(scopes, list):
        raise ValueError("worker_scope.scopes must be a list")
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        if str(scope.get("type") or "") == scope_type and str(scope.get("id") or "") == scope_id:
            return scope
    raise ValueError(f"no worker scope configured for {scope_type}:{scope_id}")


def _scope_workers(scope: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    workers = scope.get("workers") or {}
    if not isinstance(workers, dict):
        raise ValueError("scope.workers must be a mapping of profile -> policy")
    normalized: Dict[str, Dict[str, Any]] = {}
    for profile, policy in workers.items():
        if not _SAFE_NAME_RE.match(str(profile)):
            continue
        normalized[str(profile)] = policy if isinstance(policy, dict) else {}
    return normalized


def _capabilities_for(policy: Dict[str, Any]) -> List[str]:
    return _normalize_capabilities(policy.get("capabilities") or policy.get("capability_tags") or [])


def _normalize_capabilities(value: Any) -> List[str]:
    seen = set()
    caps: List[str] = []
    for item in _as_list(value):
        cap = str(item).strip()
        if not cap:
            continue
        if not _SAFE_NAME_RE.match(cap):
            raise ValueError(f"capability must be safe ascii [a-zA-Z0-9_.-]: {cap!r}")
        if cap not in seen:
            seen.add(cap)
            caps.append(cap)
    return caps


def _requested_capabilities(args: Dict[str, Any]) -> List[str]:
    raw = args.get("required_capabilities")
    if raw is None:
        raw = args.get("capabilities")
    return _normalize_capabilities(raw)


def _custom_capability(args: Dict[str, Any]) -> str:
    value = str(args.get("custom_capability") or args.get("capability_description") or "").strip()
    if len(value) > 1000:
        raise ValueError("custom_capability must be 1000 characters or fewer")
    if any(ord(ch) < 9 or (13 < ord(ch) < 32) for ch in value):
        raise ValueError("custom_capability contains unsupported control characters")
    return value


def _task_envelope_from_args(args: Dict[str, Any]) -> Dict[str, Any]:
    raw = args.get("task_envelope") or args.get("intake")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("task_envelope must be an object")
    if isinstance(raw.get("task_envelope"), dict):
        raw = raw["task_envelope"]
    elif isinstance(raw.get("routing_input"), dict):
        raw = raw["routing_input"]
    if not isinstance(raw, dict):
        raise ValueError("task_envelope must be an object")
    return dict(raw)


def _format_task_envelope_context(envelope: Dict[str, Any]) -> str:
    sections: List[Tuple[str, str]] = []
    original_prompt = str(envelope.get("original_prompt") or "").strip()
    custom_capability = str(envelope.get("custom_capability") or "").strip()
    context = str(envelope.get("context") or "").strip()
    success_criteria = _as_list(envelope.get("success_criteria"))
    entities = _as_list(envelope.get("entities"))
    unknowns = _as_list(envelope.get("unknowns"))
    constraints = _as_list(envelope.get("constraints"))
    external_actions = _as_list(envelope.get("external_actions"))
    side_effect_risk = str(envelope.get("side_effect_risk") or "").strip()

    if original_prompt:
        sections.append(("ORIGINAL USER PROMPT", original_prompt))
    if custom_capability:
        sections.append(("CUSTOM CAPABILITY", custom_capability))
    if success_criteria:
        sections.append(("SUCCESS CRITERIA", "\n".join(f"- {item}" for item in success_criteria)))
    if entities:
        sections.append(("ENTITIES", "\n".join(f"- {item}" for item in entities)))
    if unknowns:
        sections.append(("UNKNOWN/BLOCKERS", "\n".join(f"- {item}" for item in unknowns)))
    if constraints:
        sections.append(("CONSTRAINTS", "\n".join(f"- {item}" for item in constraints)))
    if external_actions:
        sections.append(("EXTERNAL ACTIONS", "\n".join(f"- {item}" for item in external_actions)))
    if side_effect_risk:
        sections.append(("SIDE EFFECT RISK", side_effect_risk))
    if context:
        sections.append(("ADDITIONAL CONTEXT", context))
    return "\n\n".join(f"{title}:\n{body}" for title, body in sections)


def _merge_task_envelope(args: Dict[str, Any]) -> Dict[str, Any]:
    envelope = _task_envelope_from_args(args)
    if not envelope:
        return args

    merged = dict(args)
    for key in ("goal", "custom_capability", "required_capabilities", "toolsets", "skills"):
        if not merged.get(key) and envelope.get(key):
            merged[key] = envelope.get(key)

    envelope_context = _format_task_envelope_context(envelope)
    if envelope_context:
        existing_context = str(merged.get("context") or "").strip()
        original_prompt = str(envelope.get("original_prompt") or "").strip()
        if not existing_context:
            merged["context"] = envelope_context
        elif original_prompt and original_prompt not in existing_context:
            merged["context"] = f"{existing_context}\n\nTASK ENVELOPE:\n{envelope_context}"

    return merged


def _verify_task_envelope_for_dispatch(args: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if cfg.get("require_verified_task_envelope") is not True:
        return None

    envelope = _task_envelope_from_args(args)
    if not envelope:
        return {
            "ok": False,
            "verified": False,
            "error": "unverified_task_envelope",
            "message": "This worker scope requires a verified task_envelope. Call gate_intake, then gate_intake_verify, and pass the task_envelope to scoped_worker_dispatch.",
            "verification": {
                "verified": False,
                "blocking_issues": [
                    {
                        "id": "missing_task_envelope",
                        "message": "scoped_worker_dispatch requires task_envelope when worker_scope.require_verified_task_envelope is true",
                    }
                ],
            },
        }

    try:
        verifier = _execution_gate_tools()
        verification_raw = verifier.handle_gate_intake_verify(
            {
                "task_envelope": envelope,
                "min_quality_score": int(cfg.get("min_intake_quality_score") or 80),
                "require_selector": True,
            }
        )
        verification = json.loads(verification_raw)
    except Exception as exc:
        return {
            "ok": False,
            "verified": False,
            "error": "intake_verifier_unavailable",
            "message": str(exc),
        }

    if verification.get("verified") is not True:
        return {
            "ok": False,
            "verified": False,
            "error": "unverified_task_envelope",
            "message": "Task envelope failed gate_intake_verify. Repair blocking_issues before scoped_worker_dispatch can run.",
            "verification": verification,
        }

    return {
        "ok": True,
        "verified": True,
        "verification": verification,
    }


def _record_conversion_event(event: Dict[str, Any]) -> None:
    try:
        _execution_gate_tools().record_conversion_event(event)
    except Exception:
        return


def _match_worker(
    profile: str,
    policy: Dict[str, Any],
    *,
    required_capabilities: List[str],
    requested_toolsets: List[str],
    requested_skills: List[str],
) -> Dict[str, Any]:
    capabilities = _capabilities_for(policy)
    allowed_toolsets = _as_list(policy.get("toolsets"))
    allowed_skills = _as_list(policy.get("skills") or [])

    missing_capabilities = [cap for cap in required_capabilities if cap not in capabilities]
    missing_toolsets = [toolset for toolset in requested_toolsets if toolset not in allowed_toolsets]
    missing_skills = [skill for skill in requested_skills if skill not in allowed_skills]
    ok = not (missing_capabilities or missing_toolsets or missing_skills)
    priority = int(policy.get("priority") or 0)
    score = priority + (10 * len(required_capabilities)) + (5 * len(requested_toolsets)) + len(requested_skills)
    return {
        "profile": profile,
        "ok": ok,
        "score": score,
        "priority": priority,
        "description": str(policy.get("description") or ""),
        "capabilities": capabilities,
        "toolsets": allowed_toolsets,
        "skills": allowed_skills,
        "matched_capabilities": [cap for cap in required_capabilities if cap in capabilities],
        "missing_capabilities": missing_capabilities,
        "missing_toolsets": missing_toolsets,
        "missing_skills": missing_skills,
        "models": list(_model_routes(policy).values()),
        "max_timeout_seconds": policy.get("max_timeout_seconds"),
        "yolo": bool(policy.get("yolo")),
    }


def _ranked_worker_matches(
    workers: Dict[str, Dict[str, Any]],
    *,
    required_capabilities: List[str],
    requested_toolsets: List[str],
    requested_skills: List[str],
) -> List[Dict[str, Any]]:
    matches = [
        _match_worker(
            profile,
            policy,
            required_capabilities=required_capabilities,
            requested_toolsets=requested_toolsets,
            requested_skills=requested_skills,
        )
        for profile, policy in workers.items()
    ]
    matches.sort(key=lambda row: (row["ok"], row["score"], row["profile"]), reverse=True)
    return matches


def _worker_policy(scope_type: str, scope_id: str, profile: str) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    if not _SAFE_NAME_RE.match(str(profile or "")):
        raise ValueError("profile must be a safe Hermes profile name")
    cfg = _worker_scope_config()
    scope_type, scope_id = _normalize_scope(scope_type, scope_id, cfg)
    scope = _find_scope(cfg, scope_type, scope_id)
    workers = _scope_workers(scope)
    if profile not in workers:
        allowed = ", ".join(sorted(workers)) or "(none)"
        raise ValueError(f"worker profile '{profile}' is not authorized for {scope_type}:{scope_id}. Allowed: {allowed}")
    profile_home = _profiles_dir() / profile
    if not (profile_home / "config.yaml").exists():
        raise ValueError(f"authorized worker profile '{profile}' has no config.yaml")
    return cfg, scope, workers[profile]


def _resolve_worker_policy(
    args: Dict[str, Any],
    scope_type: str,
    scope_id: str,
) -> Tuple[str, Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    cfg = _worker_scope_config()
    scope_type, scope_id = _normalize_scope(scope_type, scope_id, cfg)
    scope = _find_scope(cfg, scope_type, scope_id)
    workers = _scope_workers(scope)
    requested_profile = str(args.get("profile") or "").strip()
    if requested_profile == "auto":
        requested_profile = ""

    required_capabilities = _requested_capabilities(args)
    requested_toolsets = _as_list(args.get("toolsets"))
    requested_skills = _as_list(args.get("skills") or [])
    custom_capability = _custom_capability(args)

    if requested_profile:
        if not _SAFE_NAME_RE.match(requested_profile):
            raise ValueError("profile must be a safe Hermes profile name")
        if requested_profile not in workers:
            allowed = ", ".join(sorted(workers)) or "(none)"
            raise ValueError(f"worker profile '{requested_profile}' is not authorized for {scope_type}:{scope_id}. Allowed: {allowed}")
        policy = workers[requested_profile]
        match = _match_worker(
            requested_profile,
            policy,
            required_capabilities=required_capabilities,
            requested_toolsets=requested_toolsets,
            requested_skills=requested_skills,
        )
        if not match["ok"]:
            raise ValueError(
                "worker profile does not cover requested policy: "
                f"missing_capabilities={match['missing_capabilities']}, "
                f"missing_toolsets={match['missing_toolsets']}, "
                f"missing_skills={match['missing_skills']}"
            )
    else:
        if not (required_capabilities or requested_toolsets or requested_skills):
            if custom_capability:
                raise ValueError("custom_capability is open-ended; provide an explicit profile or at least hard toolsets/required_capabilities for policy matching")
            raise ValueError("profile is required unless required_capabilities, toolsets, or skills are provided for policy matching")
        matches = _ranked_worker_matches(
            workers,
            required_capabilities=required_capabilities,
            requested_toolsets=requested_toolsets,
            requested_skills=requested_skills,
        )
        matching = [row for row in matches if row["ok"]]
        if not matching:
            return (
                "",
                cfg,
                scope,
                {},
                {
                    "selected_by": "capability_match",
                    "required_capabilities": required_capabilities,
                    "custom_capability": custom_capability,
                    "requested_toolsets": requested_toolsets,
                    "requested_skills": requested_skills,
                    "candidates": matches,
                },
            )
        match = matching[0]
        requested_profile = str(match["profile"])
        policy = workers[requested_profile]

    profile_home = _profiles_dir() / requested_profile
    if not (profile_home / "config.yaml").exists():
        raise ValueError(f"authorized worker profile '{requested_profile}' has no config.yaml")
    return requested_profile, cfg, scope, policy, {
        "selected_by": "explicit_profile" if str(args.get("profile") or "").strip() and str(args.get("profile") or "").strip() != "auto" else "capability_match",
        "required_capabilities": required_capabilities,
        "custom_capability": custom_capability,
        "custom_capability_verified_by_policy": False if custom_capability else None,
        "requested_toolsets": requested_toolsets,
        "requested_skills": requested_skills,
        "selected_worker": match,
    }


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    raise ValueError("expected a string or list of strings")


def _resolve_allowed(requested: Any, allowed: Any, field: str, default_to_allowed: bool = True) -> List[str]:
    allowed_list = _as_list(allowed)
    if not allowed_list:
        raise ValueError(f"worker policy has empty {field}; fail-closed")
    requested_list = _as_list(requested)
    result = requested_list if requested_list else (allowed_list if default_to_allowed else [])
    bad = [v for v in result if v not in allowed_list]
    if bad:
        raise ValueError(f"{field} not allowed for worker policy: {bad}; allowed: {allowed_list}")
    return result


def _model_routes(policy: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return validated model routes allowed by this worker policy."""
    raw = policy.get("models") or policy.get("model_routes") or []
    rows: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("key", key)
                rows.append(row)
    elif isinstance(raw, list):
        rows = [dict(v) for v in raw if isinstance(v, dict)]
    else:
        raise ValueError("worker policy models/model_routes must be a list or mapping")

    routes: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("key") or "").strip()
        provider = str(row.get("provider") or "").strip()
        model = str(row.get("model") or "").strip()
        if not key:
            raise ValueError("worker model route is missing key")
        if not _SAFE_NAME_RE.match(key):
            raise ValueError(f"worker model route key is not safe: {key!r}")
        if not provider or not _SAFE_NAME_RE.match(provider):
            raise ValueError(f"worker model route {key!r} has invalid provider")
        if not model:
            raise ValueError(f"worker model route {key!r} has empty model")
        routes[key] = {
            "key": key,
            "provider": provider,
            "model": model,
            "description": str(row.get("description") or ""),
            "default": bool(row.get("default")),
        }
    return routes


def _resolve_model_route(args: Dict[str, Any], policy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Validate and resolve the CEO-selected worker model route."""
    routes = _model_routes(policy)
    requested_key = str(args.get("model_key") or args.get("model_route") or "").strip()
    requested_provider = str(args.get("provider") or args.get("model_provider") or "").strip()
    requested_model = str(args.get("model") or "").strip()
    explicit = bool(requested_key or requested_provider or requested_model)

    if not routes:
        if explicit:
            raise ValueError("worker policy has no model routes; explicit model selection is not allowed")
        return None

    if requested_key:
        route = routes.get(requested_key)
        if route is None:
            raise ValueError(f"model_key {requested_key!r} is not allowed; allowed: {sorted(routes)}")
        if requested_provider and requested_provider != route["provider"]:
            raise ValueError(f"provider {requested_provider!r} does not match model_key {requested_key!r}")
        if requested_model and requested_model != route["model"]:
            raise ValueError(f"model {requested_model!r} does not match model_key {requested_key!r}")
        return dict(route)

    if requested_provider or requested_model:
        if not requested_provider or not requested_model:
            raise ValueError("explicit model selection requires both provider and model, or use model_key")
        for route in routes.values():
            if route["provider"] == requested_provider and route["model"] == requested_model:
                return dict(route)
        allowed = [f"{r['key']}={r['provider']}/{r['model']}" for r in routes.values()]
        raise ValueError(f"provider/model route is not allowed; allowed: {allowed}")

    for route in routes.values():
        if route.get("default"):
            return dict(route)
    return None


def _timeout_for(policy: Dict[str, Any], requested: Any, cfg: Dict[str, Any]) -> int:
    default_timeout = int(cfg.get("default_timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS)
    max_timeout = int(policy.get("max_timeout_seconds") or default_timeout)
    timeout = int(requested or default_timeout)
    timeout = max(1, timeout)
    if timeout > max_timeout:
        raise ValueError(f"timeout_seconds {timeout} exceeds worker max {max_timeout}")
    return timeout


def _hermes_bin() -> str:
    configured = str((_load_active_config().get("worker_scope") or {}).get("hermes_bin") or "").strip()
    if configured:
        return configured
    return shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")


def _build_prompt(goal: str, context: str, scope_type: str, scope_id: str, profile: str, custom_capability: str = "") -> str:
    custom = f"\nCUSTOM CAPABILITY REQUEST:\n{custom_capability.strip()}\n" if custom_capability else ""
    return (
        f"You are running as scoped worker profile `{profile}` for {scope_type}:{scope_id}.\n"
        "Do the concrete task using only your available tools. Return proof/artifacts, "
        "or return a concrete blocker with exact missing capability. Do not claim success without proof.\n\n"
        f"{custom}"
        f"TASK:\n{goal.strip()}\n\n"
        f"CONTEXT:\n{context.strip() if context else '(none)'}"
    )


def _run_dispatch(args: Dict[str, Any]) -> Dict[str, Any]:
    args = _merge_task_envelope(args)
    cfg = _worker_scope_config()
    task_envelope = _task_envelope_from_args(args)
    intake_verification = _verify_task_envelope_for_dispatch(args, cfg)
    if intake_verification is not None and intake_verification.get("ok") is not True:
        _record_conversion_event({
            "stage": "dispatch",
            "outcome": "dispatch_blocked",
            "task_envelope": task_envelope,
            "blockers": [issue.get("id") for issue in ((intake_verification.get("verification") or {}).get("blocking_issues") or [])],
            "error": intake_verification.get("error"),
            "ok": False,
        })
        return {
            "ok": False,
            "dry_run": bool(args.get("dry_run", False)),
            **intake_verification,
        }

    goal = str(args.get("goal") or "").strip()
    if not goal:
        raise ValueError("goal is required")
    scope_type, scope_id = _normalize_scope(args.get("scope_type"), args.get("scope_id"), cfg)
    profile, cfg, scope, policy, selection = _resolve_worker_policy(args, scope_type, scope_id)
    if not profile:
        _record_conversion_event({
            "stage": "dispatch",
            "outcome": "dispatch_blocked",
            "task_envelope": task_envelope,
            "blockers": ["no_matching_worker"],
            "error": "no_matching_worker",
            "ok": False,
        })
        return {
            "ok": False,
            "dry_run": bool(args.get("dry_run", False)),
            "error": "no_matching_worker",
            "scope_type": scope_type,
            "scope_id": scope_id,
            **selection,
            "message": "No authorized worker covers the requested capabilities/toolsets. Do not force the task into a starter worker; create or approve a worker with the missing capabilities.",
        }
    toolsets = _resolve_allowed(args.get("toolsets"), policy.get("toolsets"), "toolsets")
    skills = _resolve_allowed(args.get("skills"), policy.get("skills") or [], "skills", default_to_allowed=False) if (policy.get("skills") or args.get("skills")) else []
    model_route = _resolve_model_route(args, policy)
    timeout = _timeout_for(policy, args.get("timeout_seconds"), cfg)
    dry_run = bool(args.get("dry_run", False))
    context = str(args.get("context") or "")
    prompt = _build_prompt(goal, context, scope_type, scope_id, profile, selection.get("custom_capability") or "")
    yolo = bool(policy.get("yolo") or cfg.get("worker_yolo"))

    cmd = [_hermes_bin(), "--profile", profile, "chat", "-q", prompt]
    if model_route:
        cmd.extend(["--provider", model_route["provider"], "-m", model_route["model"]])
    cmd.extend(["-t", ",".join(toolsets), "-Q", "--source", "scoped-worker-dispatch"])
    if yolo:
        cmd.append("--yolo")
    for skill in skills:
        cmd.extend(["-s", skill])

    env = os.environ.copy()
    # Prevent active profile home/env from leaking into the worker process.
    for key in ("HERMES_HOME", "HERMES_PROFILE_NAME"):
        env.pop(key, None)
    env["HERMES_PROFILE"] = profile
    env["HERMES_PARENT_PROFILE"] = _active_profile_name()
    env["HERMES_WORKER_SCOPE_TYPE"] = scope_type
    env["HERMES_WORKER_SCOPE_ID"] = scope_id
    if scope_type == "tenant":
        env["HERMES_TENANT"] = scope_id
        env.pop("HERMES_KANBAN_BOARD", None)
    elif scope_type == "board":
        env["HERMES_KANBAN_BOARD"] = scope_id
        env["HERMES_TENANT"] = str(scope.get("tenant") or scope_id)

    public_cmd = [cmd[0], "--profile", profile, "chat", "-q", "<prompt>"]
    if model_route:
        public_cmd.extend(["--provider", model_route["provider"], "-m", model_route["model"]])
    public_cmd.extend(["-t", ",".join(toolsets), "-Q", "--source", "scoped-worker-dispatch"])
    if yolo:
        public_cmd.append("--yolo")
    for skill in skills:
        public_cmd.extend(["-s", skill])

    base = {
        "profile": profile,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "toolsets": toolsets,
        "skills": skills,
        "timeout_seconds": timeout,
        "yolo": yolo,
        "model_route": model_route,
        "command": public_cmd,
        "policy_description": policy.get("description") or "",
        "capabilities": _capabilities_for(policy),
        "selection": selection,
    }
    if intake_verification is not None:
        base["intake_verification"] = intake_verification.get("verification")
    if dry_run:
        _record_conversion_event({
            "stage": "dispatch",
            "outcome": "dispatch_validated_dry_run",
            "task_envelope": task_envelope,
            "worker_profile": profile,
            "model_route": model_route,
            "quality_score": (intake_verification.get("verification") or {}).get("quality_score") if intake_verification else None,
            "ok": True,
        })
        return {"ok": True, "dry_run": True, **base}

    started = time.time()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if len(stdout) > _OUTPUT_LIMIT:
        stdout = stdout[:_OUTPUT_LIMIT] + "\n...[truncated]"
    if len(stderr) > _OUTPUT_LIMIT:
        stderr = stderr[:_OUTPUT_LIMIT] + "\n...[truncated]"
    result = {
        "ok": proc.returncode == 0,
        "dry_run": False,
        **base,
        "exit_code": proc.returncode,
        "duration_seconds": round(time.time() - started, 2),
        "stdout": stdout.strip(),
        "stderr": stderr.strip(),
    }
    _record_conversion_event({
        "stage": "dispatch",
        "outcome": "worker_success" if result["ok"] else "worker_failed",
        "task_envelope": task_envelope,
        "worker_profile": profile,
        "model_route": model_route,
        "quality_score": (intake_verification.get("verification") or {}).get("quality_score") if intake_verification else None,
        "duration_seconds": result["duration_seconds"],
        "error": "" if result["ok"] else "worker_exit_nonzero",
        "ok": result["ok"],
    })
    return result


POLICY_SCHEMA = {
    "name": "scoped_worker_policy",
    "description": "Inspect authorized scoped workers and their capabilities for a tenant or board scope.",
    "parameters": {
        "type": "object",
        "properties": {
            "scope_type": {"type": "string", "description": "tenant or board. Defaults from worker_scope.default_scope."},
            "scope_id": {"type": "string", "description": "Tenant id or board slug. Defaults from worker_scope.default_scope."},
        },
    },
}

MATCH_SCHEMA = {
    "name": "scoped_worker_match",
    "description": "Select the best authorized worker for required capabilities/toolsets without dispatching it. custom_capability is descriptive and does not authorize a worker by itself.",
    "parameters": {
        "type": "object",
        "properties": {
            "scope_type": {"type": "string", "description": "tenant or board. Defaults from worker_scope.default_scope."},
            "scope_id": {"type": "string", "description": "Tenant id or board slug. Defaults from worker_scope.default_scope."},
            "profile": {"type": "string", "description": "Optional authorized worker profile to validate against the requested selectors."},
            "task_envelope": {"type": "object", "description": "Optional gate_intake task envelope. Missing goal, custom_capability, required_capabilities, toolsets, skills, and context are filled from it."},
            "required_capabilities": {"type": "array", "items": {"type": "string"}, "description": "Policy capability tags the task needs, e.g. code, browser, web_research. These are enforceable selectors, not an exhaustive taxonomy."},
            "custom_capability": {"type": "string", "description": "Freeform task-specific capability request when no fixed tag is precise enough. Does not select or authorize a worker unless paired with profile, required_capabilities, toolsets, or skills."},
            "toolsets": {"type": "array", "items": {"type": "string"}, "description": "Optional required toolsets."},
            "skills": {"type": "array", "items": {"type": "string"}, "description": "Optional required skills."},
        },
    },
}

DISPATCH_SCHEMA = {
    "name": "scoped_worker_dispatch",
    "description": "Dispatch one authorized worker inside a hard tenant/board scope. Provide either profile or required_capabilities/toolsets for policy selection. custom_capability is passed to the worker as task intent, not as authorization. Scopes may require a verified gate_intake task_envelope before dispatch.",
    "parameters": {
        "type": "object",
        "properties": {
            "scope_type": {"type": "string", "description": "tenant or board."},
            "scope_id": {"type": "string", "description": "Tenant id or board slug."},
            "profile": {"type": "string", "description": "Optional authorized worker profile for this scope. Use auto or omit to select by required_capabilities/toolsets."},
            "task_envelope": {"type": "object", "description": "Optional gate_intake task envelope. Missing goal, custom_capability, required_capabilities, toolsets, skills, and context are filled from it. Required when worker_scope.require_verified_task_envelope is true."},
            "required_capabilities": {"type": "array", "items": {"type": "string"}, "description": "Policy capability tags the task needs. Used to select and validate worker policy."},
            "custom_capability": {"type": "string", "description": "Freeform task-specific capability request when fixed tags are not enough. Passed into the worker prompt but does not select or authorize a worker by itself."},
            "goal": {"type": "string", "description": "Concrete task the worker should perform."},
            "context": {"type": "string", "description": "Relevant bounded context for the worker."},
            "toolsets": {"type": "array", "items": {"type": "string"}, "description": "Optional subset of this worker policy's allowed toolsets."},
            "skills": {"type": "array", "items": {"type": "string"}, "description": "Optional subset of this worker policy's allowed skills."},
            "model_key": {"type": "string", "description": "Optional configured model route key from the worker policy, e.g. deep_reasoning, standard, or economy."},
            "provider": {"type": "string", "description": "Optional explicit provider; must match an allowed worker model route and be paired with model."},
            "model": {"type": "string", "description": "Optional explicit model; must match an allowed worker model route and be paired with provider."},
            "timeout_seconds": {"type": "integer", "description": "Run timeout, capped by policy."},
            "dry_run": {"type": "boolean", "description": "Validate scope/policy and show command without running the worker."},
        },
    },
}

BATCH_SCHEMA = {
    "name": "scoped_worker_batch",
    "description": "Dispatch multiple authorized scoped workers in parallel. Every task is independently policy-validated before any process starts.",
    "parameters": {
        "type": "object",
        "properties": {
            "tasks": {"type": "array", "items": {"type": "object"}, "description": "Each task accepts the same fields as scoped_worker_dispatch."},
            "dry_run": {"type": "boolean", "description": "Validate all tasks and show commands without running."},
        },
        "required": ["tasks"],
    },
}


def _handle_policy(args: Dict[str, Any], **kw) -> str:
    try:
        cfg = _worker_scope_config()
        scope_type, scope_id = _normalize_scope(args.get("scope_type"), args.get("scope_id"), cfg)
        scope = _find_scope(cfg, scope_type, scope_id)
        workers = _scope_workers(scope)
        public_workers = {
            name: {
                "description": policy.get("description", ""),
                "capabilities": _capabilities_for(policy),
                "toolsets": _as_list(policy.get("toolsets")),
                "skills": _as_list(policy.get("skills") or []),
                "models": list(_model_routes(policy).values()),
                "max_timeout_seconds": policy.get("max_timeout_seconds"),
                "yolo": bool(policy.get("yolo")),
            }
            for name, policy in sorted(workers.items())
        }
        return _json({"ok": True, "scope_type": scope_type, "scope_id": scope_id, "workers": public_workers})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})


def _handle_match(args: Dict[str, Any], **kw) -> str:
    try:
        args = _merge_task_envelope(args)
        cfg = _worker_scope_config()
        scope_type, scope_id = _normalize_scope(args.get("scope_type"), args.get("scope_id"), cfg)
        scope = _find_scope(cfg, scope_type, scope_id)
        workers = _scope_workers(scope)
        requested_profile = str(args.get("profile") or "").strip()
        if requested_profile == "auto":
            requested_profile = ""
        required_capabilities = _requested_capabilities(args)
        requested_toolsets = _as_list(args.get("toolsets"))
        requested_skills = _as_list(args.get("skills") or [])
        custom_capability = _custom_capability(args)
        matches = _ranked_worker_matches(
            workers,
            required_capabilities=required_capabilities,
            requested_toolsets=requested_toolsets,
            requested_skills=requested_skills,
        )

        if requested_profile:
            if not _SAFE_NAME_RE.match(requested_profile):
                raise ValueError("profile must be a safe Hermes profile name")
            if requested_profile not in workers:
                allowed = ", ".join(sorted(workers)) or "(none)"
                raise ValueError(f"worker profile '{requested_profile}' is not authorized for {scope_type}:{scope_id}. Allowed: {allowed}")
            profile_home = _profiles_dir() / requested_profile
            if not (profile_home / "config.yaml").exists():
                raise ValueError(f"authorized worker profile '{requested_profile}' has no config.yaml")
            selected = _match_worker(
                requested_profile,
                workers[requested_profile],
                required_capabilities=required_capabilities,
                requested_toolsets=requested_toolsets,
                requested_skills=requested_skills,
            )
            if not selected["ok"]:
                return _json({
                    "ok": False,
                    "error": "profile_does_not_cover_requested_policy",
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "selected_profile": requested_profile,
                    "selected_by": "explicit_profile",
                    "required_capabilities": required_capabilities,
                    "custom_capability": custom_capability,
                    "custom_capability_verified_by_policy": False if custom_capability else None,
                    "requested_toolsets": requested_toolsets,
                    "requested_skills": requested_skills,
                    "selected_worker": selected,
                    "candidates": matches,
                    "message": "The explicit worker profile is authorized but does not cover the requested policy selectors.",
                })
            return _json({
                "ok": True,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "selected_profile": requested_profile,
                "selected_by": "explicit_profile",
                "required_capabilities": required_capabilities,
                "custom_capability": custom_capability,
                "custom_capability_verified_by_policy": False if custom_capability else None,
                "requested_toolsets": requested_toolsets,
                "requested_skills": requested_skills,
                "selected_worker": selected,
                "candidates": matches,
            })

        selected = next((row for row in matches if row["ok"]), None)
        if not (required_capabilities or requested_toolsets or requested_skills):
            if custom_capability:
                return _json({
                    "ok": False,
                    "error": "custom_capability_requires_policy_selector",
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "selected_profile": None,
                    "required_capabilities": required_capabilities,
                    "custom_capability": custom_capability,
                    "custom_capability_verified_by_policy": False,
                    "requested_toolsets": requested_toolsets,
                    "requested_skills": requested_skills,
                    "candidates": matches,
                    "message": "custom_capability is freeform task intent, not an authorization selector. Provide an explicit profile or at least hard toolsets/required_capabilities/skills for policy matching.",
                })
            return _json({
                "ok": True,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "selected_profile": None,
                "required_capabilities": required_capabilities,
                "custom_capability": custom_capability,
                "custom_capability_verified_by_policy": None,
                "requested_toolsets": requested_toolsets,
                "requested_skills": requested_skills,
                "candidates": matches,
                "message": "No selector was provided. Pass required_capabilities and/or toolsets to choose a worker.",
            })
        if selected is None:
            return _json({
                "ok": False,
                "error": "no_matching_worker",
                "scope_type": scope_type,
                "scope_id": scope_id,
                "selected_profile": None,
                "required_capabilities": required_capabilities,
                "custom_capability": custom_capability,
                "custom_capability_verified_by_policy": False if custom_capability else None,
                "requested_toolsets": requested_toolsets,
                "requested_skills": requested_skills,
                "candidates": matches,
                "message": "No authorized worker covers the requested capabilities/toolsets. Do not force the task into a starter worker.",
            })
        return _json({
            "ok": True,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "selected_profile": selected["profile"],
            "required_capabilities": required_capabilities,
            "custom_capability": custom_capability,
            "custom_capability_verified_by_policy": False if custom_capability else None,
            "requested_toolsets": requested_toolsets,
            "requested_skills": requested_skills,
            "selected_worker": selected,
            "candidates": matches,
        })
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})


def _handle_dispatch(args: Dict[str, Any], **kw) -> str:
    try:
        return _json(_run_dispatch(args))
    except subprocess.TimeoutExpired as exc:
        return _json({"ok": False, "error": "timeout", "message": str(exc)})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})


def _handle_batch(args: Dict[str, Any], **kw) -> str:
    try:
        tasks = args.get("tasks") or []
        if not isinstance(tasks, list) or not tasks:
            return _json({"ok": False, "error": "tasks must be a non-empty list"})
        cfg = _worker_scope_config()
        max_batch = int(cfg.get("max_batch") or _DEFAULT_MAX_BATCH)
        if len(tasks) > max_batch:
            return _json({"ok": False, "error": f"batch size {len(tasks)} exceeds max_batch {max_batch}"})
        dry_run = bool(args.get("dry_run", False))
        normalized = []
        # Validate every task before launching any worker.
        for i, task in enumerate(tasks):
            if not isinstance(task, dict):
                return _json({"ok": False, "error": f"task {i} must be an object"})
            item = dict(task)
            if dry_run:
                item["dry_run"] = True
            result = _run_dispatch({**item, "dry_run": True})
            normalized.append((i, item, result))
        if dry_run:
            return _json({"ok": True, "dry_run": True, "results": [r for _, _, r in normalized]})

        results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(len(normalized), max_batch)) as pool:
            future_map = {pool.submit(_run_dispatch, item): idx for idx, item, _ in normalized}
            for fut in as_completed(future_map):
                idx = future_map[fut]
                try:
                    res = fut.result()
                except subprocess.TimeoutExpired as exc:
                    res = {"ok": False, "error": "timeout", "message": str(exc)}
                except Exception as exc:
                    res = {"ok": False, "error": str(exc)}
                res["task_index"] = idx
                results.append(res)
        results.sort(key=lambda r: r.get("task_index", 0))
        return _json({"ok": all(r.get("ok") for r in results), "dry_run": False, "results": results})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})


def register(ctx) -> None:
    ctx.register_tool(name="scoped_worker_policy", toolset="scoped_worker", schema=POLICY_SCHEMA, handler=_handle_policy, emoji="🪪")
    ctx.register_tool(name="scoped_worker_match", toolset="scoped_worker", schema=MATCH_SCHEMA, handler=_handle_match, emoji="🎯")
    ctx.register_tool(name="scoped_worker_dispatch", toolset="scoped_worker", schema=DISPATCH_SCHEMA, handler=_handle_dispatch, emoji="🛡️")
    ctx.register_tool(name="scoped_worker_batch", toolset="scoped_worker", schema=BATCH_SCHEMA, handler=_handle_batch, emoji="🛡️")
