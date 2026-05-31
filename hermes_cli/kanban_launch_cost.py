"""Launch contract budget and cost projection scaffold (estimates).

Projections combine curated model slug heuristics and capability_catalog cost
notes. All outputs are labeled ``estimate`` until live metering reaches 100%
accuracy.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "ensure_contract_cost_scaffold",
    "format_cost_lines",
    "normalize_cost_projection",
    "normalize_runtime_budget",
    "project_contract_costs",
]

# USD per 1M tokens (input+output blended heuristic). ESTIMATES ONLY.
_MODEL_USD_PER_1M_TOKENS: dict[str, float] = {
    "anthropic/claude-opus-4.8": 25.0,
    "anthropic/claude-opus-4.6": 18.0,
    "anthropic/claude-sonnet-4.6": 6.0,
    "anthropic/claude-haiku-4.5": 1.5,
    "composer-2.5-fast": 2.0,
    "gpt-5.3-codex": 8.0,
    "gpt-5.5-medium": 10.0,
    "openai/gpt-5.5": 12.0,
    "openai/gpt-5.3-codex": 8.0,
    "openai/gpt-5.4-mini": 2.5,
    "openai/gpt-5.4-nano": 0.8,
}

_DEFAULT_WEEKLY_TASKS = 40
_DEFAULT_TOKENS_PER_TASK = 25_000


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _contract_root(contract: Any) -> dict[str, Any]:
    parsed = contract if isinstance(contract, dict) else {}
    if isinstance(parsed.get("operating_contract"), dict):
        return dict(parsed["operating_contract"])
    inner = parsed.get("contract")
    if isinstance(inner, dict) and any(
        k in inner for k in ("objective", "runtime", "workflow")
    ):
        return dict(inner)
    return dict(parsed)


def _catalog_path() -> Path:
    return Path(__file__).resolve().parent / "capability_catalog.yaml"


@lru_cache(maxsize=1)
def _load_capability_catalog() -> list[dict[str, Any]]:
    path = _catalog_path()
    if not path.exists():
        return []
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    integrations = doc.get("integrations") if isinstance(doc, dict) else None
    if not isinstance(integrations, list):
        return []
    return [i for i in integrations if isinstance(i, dict)]


def _estimate_model_usd_per_1m(slug: str) -> float:
    key = str(slug or "").strip()
    if key in _MODEL_USD_PER_1M_TOKENS:
        return _MODEL_USD_PER_1M_TOKENS[key]
    lowered = key.lower()
    for pattern, rate in _MODEL_USD_PER_1M_TOKENS.items():
        if pattern.split("/")[-1] in lowered or pattern in lowered:
            return rate
    if "haiku" in lowered or "nano" in lowered or "mini" in lowered:
        return 1.5
    if "sonnet" in lowered or "medium" in lowered:
        return 6.0
    if "opus" in lowered or "pro" in lowered:
        return 20.0
    return 5.0


def _collect_model_slugs(contract: dict[str, Any]) -> list[str]:
    runtime = _as_dict(contract.get("runtime"))
    models = _as_dict(runtime.get("models"))
    slugs: list[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        text = str(raw or "").strip()
        if text and text not in seen:
            seen.add(text)
            slugs.append(text)

    _add(models.get("default"))
    for bucket in ("roles", "stages", "actions", "task_types"):
        mapping = models.get(bucket)
        if isinstance(mapping, dict):
            for val in mapping.values():
                _add(val)
    return slugs


def _integration_cost_lines(contract: dict[str, Any]) -> list[dict[str, Any]]:
    needed = {
        str(v).strip()
        for v in _as_list(contract.get("needed_capability_types"))
        if str(v or "").strip()
    }
    if not needed:
        return []

    lines: list[dict[str, Any]] = []
    for entry in _load_capability_catalog():
        provides = {
            str(p).strip() for p in _as_list(entry.get("provides")) if str(p or "").strip()
        }
        if not provides & needed:
            continue
        cost = _as_dict(entry.get("cost"))
        lines.append({
            "integration_id": str(entry.get("id") or ""),
            "cost_model": str(cost.get("model") or "unknown"),
            "notes": str(cost.get("notes") or ""),
            "accuracy": "estimate",
        })
    return lines


def normalize_runtime_budget(value: Optional[Any]) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("runtime.budget must be an object")
    out: dict[str, Any] = {"accuracy": "estimate"}
    for key in (
        "weekly_usd_cap",
        "monthly_usd_cap",
        "token_budget_weekly",
        "preference",
        "notes",
    ):
        if key in value and value[key] is not None:
            out[key] = value[key]
    pref = str(out.get("preference") or "").strip().lower()
    if pref and pref not in ("quality", "balanced", "budget"):
        raise ValueError("runtime.budget.preference must be quality, balanced, or budget")
    return out or None


def normalize_cost_projection(value: Optional[Any]) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("runtime.cost_projection must be an object")
    out = dict(value)
    out.setdefault("accuracy", "estimate")
    out.setdefault("currency", "USD")
    out.setdefault("period", "weekly")
    return out


def project_contract_costs(contract: dict[str, Any]) -> dict[str, Any]:
    """Build a structured weekly cost projection (estimates)."""
    root = _contract_root(contract)
    runtime = _as_dict(root.get("runtime"))
    budget = _as_dict(runtime.get("budget"))
    slugs = _collect_model_slugs(root) or ["composer-2.5-fast"]

    weekly_tasks = int(budget.get("weekly_task_estimate") or _DEFAULT_WEEKLY_TASKS)
    tokens_per_task = int(budget.get("tokens_per_task_estimate") or _DEFAULT_TOKENS_PER_TASK)
    total_tokens = weekly_tasks * tokens_per_task

    model_lines: list[dict[str, Any]] = []
    slug_rates = [_estimate_model_usd_per_1m(s) for s in slugs]
    avg_rate = sum(slug_rates) / len(slug_rates) if slug_rates else 5.0
    model_usd = round(total_tokens / 1_000_000 * avg_rate, 2)

    for slug in slugs[:8]:
        rate = _estimate_model_usd_per_1m(slug)
        share = total_tokens / max(len(slugs), 1)
        model_lines.append({
            "model": slug,
            "estimated_tokens_weekly": int(share),
            "usd_per_1m_tokens": rate,
            "estimated_usd_weekly": round(share / 1_000_000 * rate, 2),
            "accuracy": "estimate",
        })

    service_lines = _integration_cost_lines(root)
    messaging_usd = 0.0
    for line in service_lines:
        model_kind = str(line.get("cost_model") or "").lower()
        if model_kind in ("per_call", "subscription"):
            messaging_usd += 5.0 if model_kind == "per_call" else 0.0

    tool_usd = round(len(service_lines) * 2.0, 2)
    total_usd = round(model_usd + tool_usd + messaging_usd, 2)

    return normalize_cost_projection({
        "accuracy": "estimate",
        "currency": "USD",
        "period": "weekly",
        "assumptions": {
            "weekly_tasks": weekly_tasks,
            "tokens_per_task": tokens_per_task,
            "models_considered": slugs,
        },
        "breakdown": {
            "models_usd": model_usd,
            "tools_and_integrations_usd": tool_usd,
            "messaging_and_services_usd": messaging_usd,
        },
        "model_lines": model_lines,
        "service_lines": service_lines,
        "total_usd_weekly": total_usd,
        "disclaimer": (
            "Projected costs are estimates from catalog heuristics, not metered "
            "usage. Actual spend may differ."
        ),
    }) or {}


def ensure_contract_cost_scaffold(contract: dict[str, Any]) -> dict[str, Any]:
    """Ensure runtime.budget and runtime.cost_projection exist on a contract."""
    synced = dict(contract)
    runtime = dict(_as_dict(synced.get("runtime")))
    if not runtime.get("budget"):
        runtime["budget"] = normalize_runtime_budget({
            "weekly_usd_cap": None,
            "preference": "balanced",
            "weekly_task_estimate": _DEFAULT_WEEKLY_TASKS,
            "tokens_per_task_estimate": _DEFAULT_TOKENS_PER_TASK,
            "notes": "Placeholder until owner sets caps during Model Route / contract review.",
        })
    else:
        runtime["budget"] = normalize_runtime_budget(runtime.get("budget"))
    runtime["cost_projection"] = project_contract_costs({**synced, "runtime": runtime})
    synced["runtime"] = runtime
    return synced


def format_cost_lines(projection: Optional[dict[str, Any]]) -> list[str]:
    if not isinstance(projection, dict):
        return []
    total = projection.get("total_usd_weekly")
    period = str(projection.get("period") or "weekly")
    accuracy = str(projection.get("accuracy") or "estimate")
    lines = [
        f"~${total} USD / {period} ({accuracy})",
    ]
    breakdown = _as_dict(projection.get("breakdown"))
    if breakdown:
        parts = []
        for key, label in (
            ("models_usd", "models"),
            ("tools_and_integrations_usd", "tools"),
            ("messaging_and_services_usd", "services"),
        ):
            val = breakdown.get(key)
            if val is not None:
                parts.append(f"{label} ~${val}")
        if parts:
            lines.append("Breakdown: " + ", ".join(parts))
    disclaimer = str(projection.get("disclaimer") or "").strip()
    if disclaimer:
        lines.append(disclaimer)
    return lines
