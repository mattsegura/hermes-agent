"""Board-level model routing for kanban worker dispatch.

Resolution order (first match wins):

1. ``task.model_override`` — explicit per-task override
2. ``runtime.models.actions[action_key]`` — workflow action bucket
3. ``runtime.models.stages[stage_key]`` — workflow stage bucket
4. ``runtime.models.task_types[*]`` — generic buckets (triage, implementation, …)
5. ``runtime.models.roles[role]`` — board role derived from assignee profile
6. ``runtime.models.default`` — board-wide default override
7. ``None`` — fall back to the worker profile's configured model (no ``-m`` flag)

Model slugs are validated against the shipped model catalog when available;
unknown slugs produce warnings but do not block launch (the operator may use a
local or provider-specific id).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "BOARD_MODEL_ROLES",
    "BOARD_MODEL_TASK_TYPES",
    "build_model_routing_read_model",
    "list_supported_model_slugs",
    "merge_model_routing_into_runtime",
    "normalize_model_slug",
    "normalize_runtime_models",
    "resolve_worker_model",
    "validate_runtime_model_slugs",
]

BOARD_MODEL_ROLES = (
    "default",
    "dispatcher",
    "ceo",
    "optimizer",
    "worker",
)

BOARD_MODEL_TASK_TYPES = (
    "triage",
    "specification",
    "implementation",
    "review",
    "research",
)

_MODEL_MAP_KEYS = ("roles", "stages", "actions", "task_types")


def _catalog_path() -> Path:
    return Path(__file__).resolve().parent.parent / "website" / "static" / "api" / "model-catalog.json"


@lru_cache(maxsize=1)
def list_supported_model_slugs() -> tuple[str, ...]:
    """Curated agentic model ids from the shipped catalog (best-effort)."""
    path = _catalog_path()
    slugs: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            catalog = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return _fallback_model_slugs()

    providers = catalog.get("providers")
    if not isinstance(providers, dict):
        return _fallback_model_slugs()

    for provider_cfg in providers.values():
        if not isinstance(provider_cfg, dict):
            continue
        models = provider_cfg.get("models")
        if not isinstance(models, list):
            continue
        for entry in models:
            if isinstance(entry, dict):
                model_id = str(entry.get("id") or "").strip()
                if model_id:
                    slugs.add(model_id)
            elif isinstance(entry, str) and entry.strip():
                slugs.add(entry.strip())

    if not slugs:
        return _fallback_model_slugs()
    return tuple(sorted(slugs))


def _fallback_model_slugs() -> tuple[str, ...]:
    return (
        "anthropic/claude-opus-4.8",
        "anthropic/claude-sonnet-4.6",
        "composer-2.5-fast",
        "gpt-5.3-codex",
        "gpt-5.5-medium",
        "openai/gpt-5.3-codex",
        "openai/gpt-5.5",
    )


def normalize_model_slug(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _normalize_model_map(value: Any, *, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"runtime.models.{field} must be an object")
    out: dict[str, str] = {}
    for raw_key, raw_model in value.items():
        key = str(raw_key or "").strip()
        model = normalize_model_slug(raw_model)
        if key and model:
            out[key] = model
    return out


def normalize_runtime_models(value: Optional[Any]) -> Optional[dict[str, Any]]:
    """Validate and normalize ``runtime.models`` on a board contract."""
    if value is None:
        return None
    if isinstance(value, str):
        model = normalize_model_slug(value)
        return {"default": model} if model else None
    if not isinstance(value, dict):
        raise ValueError("runtime.models must be an object or model slug string")

    out: dict[str, Any] = {}
    default = normalize_model_slug(value.get("default"))
    if default:
        out["default"] = default

    for key in _MODEL_MAP_KEYS:
        if key not in value:
            continue
        out[key] = _normalize_model_map(value.get(key), field=key)

    if not out:
        return None
    return out


def merge_model_routing_into_runtime(
    runtime: Optional[dict[str, Any]],
    model_routing: Optional[Any],
) -> dict[str, Any]:
    """Deep-merge a launch-time ``model_routing`` payload into runtime metadata."""
    base = dict(runtime) if isinstance(runtime, dict) else {}
    if model_routing is None:
        return base

    existing = base.get("models")
    merged: dict[str, Any] = {}
    if isinstance(existing, dict):
        merged = dict(existing)
    elif isinstance(existing, str):
        slug = normalize_model_slug(existing)
        if slug:
            merged["default"] = slug

    if isinstance(model_routing, str):
        slug = normalize_model_slug(model_routing)
        if slug:
            merged["default"] = slug
    elif isinstance(model_routing, dict):
        for key, val in model_routing.items():
            if key == "default":
                slug = normalize_model_slug(val)
                if slug:
                    merged["default"] = slug
                continue
            if key in _MODEL_MAP_KEYS and isinstance(val, dict):
                bucket = dict(merged.get(key) or {})
                bucket.update(_normalize_model_map(val, field=key))
                merged[key] = bucket

    normalized = normalize_runtime_models(merged)
    if normalized is not None:
        base["models"] = normalized
    elif "models" in base and not merged:
        base.pop("models", None)
    return base


def validate_runtime_model_slugs(models: Optional[dict[str, Any]]) -> list[str]:
    """Return warning strings for slugs absent from the curated catalog."""
    if not isinstance(models, dict):
        return []
    supported = set(list_supported_model_slugs())
    warnings: list[str] = []
    seen: set[str] = set()

    def _check(slug: Optional[str], label: str) -> None:
        if not slug or slug in seen:
            return
        seen.add(slug)
        if slug not in supported:
            warnings.append(
                f"runtime.models.{label}={slug!r} is not in the curated catalog "
                "(may still work if configured on the worker profile's provider)"
            )

    _check(normalize_model_slug(models.get("default")), "default")
    for bucket in _MODEL_MAP_KEYS:
        mapping = models.get(bucket)
        if not isinstance(mapping, dict):
            continue
        for key, slug in mapping.items():
            _check(normalize_model_slug(slug), f"{bucket}.{key}")
    return warnings


def _profile_role_map(board: Optional[str]) -> dict[str, str]:
    """Map profile name -> first declared board role."""
    from hermes_cli.kanban_db import board_role_profiles

    roles = board_role_profiles(board)
    profile_roles: dict[str, str] = {}
    for role, profile in roles.items():
        profile_roles.setdefault(profile, role)
    return profile_roles


def _infer_task_type(task: Any) -> Optional[str]:
    status = str(getattr(task, "status", "") or "").strip().lower()
    if status == "triage":
        return "triage"
    if status == "review":
        return "review"
    stage_key = str(getattr(task, "stage_key", "") or "").strip().lower()
    if stage_key:
        if any(token in stage_key for token in ("research", "discover", "intake")):
            return "research"
        if any(token in stage_key for token in ("implement", "build", "execute", "ship")):
            return "implementation"
        if any(token in stage_key for token in ("review", "verify", "audit")):
            return "review"
        if any(token in stage_key for token in ("spec", "plan", "design")):
            return "specification"
    action_key = str(getattr(task, "action_key", "") or "").strip().lower()
    if action_key:
        if "triage" in action_key:
            return "triage"
        if any(token in action_key for token in ("implement", "build", "execute")):
            return "implementation"
        if "review" in action_key:
            return "review"
        if any(token in action_key for token in ("research", "discover")):
            return "research"
        if any(token in action_key for token in ("spec", "plan")):
            return "specification"
    return None


def resolve_worker_model(
    task: Any,
    board_meta: Optional[dict[str, Any]] = None,
    *,
    board: Optional[str] = None,
) -> Optional[str]:
    """Resolve the model slug a dispatched worker should run under."""
    explicit = normalize_model_slug(getattr(task, "model_override", None))
    if explicit:
        return explicit

    if isinstance(board_meta, dict):
        meta = board_meta
    else:
        from hermes_cli.kanban_db import read_board_metadata

        meta = read_board_metadata(board)
    runtime = meta.get("runtime") if isinstance(meta.get("runtime"), dict) else {}
    models = runtime.get("models")
    if not isinstance(models, dict):
        return None

    action_key = str(getattr(task, "action_key", "") or "").strip()
    if action_key:
        actions = models.get("actions")
        if isinstance(actions, dict):
            hit = normalize_model_slug(actions.get(action_key))
            if hit:
                return hit

    stage_key = str(getattr(task, "stage_key", "") or "").strip()
    if stage_key:
        stages = models.get("stages")
        if isinstance(stages, dict):
            hit = normalize_model_slug(stages.get(stage_key))
            if hit:
                return hit

    task_type = _infer_task_type(task)
    if task_type:
        task_types = models.get("task_types")
        if isinstance(task_types, dict):
            hit = normalize_model_slug(task_types.get(task_type))
            if hit:
                return hit

    assignee = str(getattr(task, "assignee", "") or "").strip()
    if assignee:
        role = _profile_role_map(meta.get("slug") or board).get(assignee)
        if role:
            roles = models.get("roles")
            if isinstance(roles, dict):
                hit = normalize_model_slug(roles.get(role))
                if hit:
                    return hit

    return normalize_model_slug(models.get("default"))


def build_model_routing_read_model(board_meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Compact read-model for dashboard / launch review."""
    meta = board_meta if isinstance(board_meta, dict) else {}
    runtime = meta.get("runtime") if isinstance(meta.get("runtime"), dict) else {}
    models = runtime.get("models") if isinstance(runtime.get("models"), dict) else {}
    profiles = runtime.get("profiles") if isinstance(runtime.get("profiles"), dict) else {}
    from hermes_cli.kanban_db import board_role_profiles

    role_map = board_role_profiles(meta.get("slug"))
    profile_models: list[dict[str, str]] = []
    roles_cfg = models.get("roles") if isinstance(models.get("roles"), dict) else {}
    for role, profile in role_map.items():
        if not profile:
            continue
        entry: dict[str, str] = {"role": role, "profile": profile}
        model = normalize_model_slug(roles_cfg.get(role))
        if model:
            entry["model"] = model
        profile_models.append(entry)

    warnings = validate_runtime_model_slugs(models if models else None)
    return {
        "models": models,
        "supported_slugs": list(list_supported_model_slugs()),
        "role_profiles": profile_models,
        "known_roles": list(BOARD_MODEL_ROLES),
        "known_task_types": list(BOARD_MODEL_TASK_TYPES),
        "warnings": warnings,
    }
