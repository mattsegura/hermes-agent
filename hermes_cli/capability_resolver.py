#!/usr/bin/env python3
"""capability_resolver.py -- GENERIC capability -> integration resolver (B1).

Refreshed-plan item B1, the "capability-discovery layer". Standalone: stdlib +
PyYAML only. It does NOT import hermes_cli (avoids version-skew / side effects)
and it never touches a DB or board.json.

DESIGN PRINCIPLE (critical): zero hardcoding of domains.
  * The catalog (capability_catalog.yaml) is DATA -- adding a tool/domain = adding
    entries, never code.
  * This resolver is a GENERIC matcher: it has no per-domain branch. It matches a
    list of abstract `capability_type` strings (supplied BY THE CALLER -- the
    model infers goal -> needs at runtime, NOT here) against each integration's
    `provides` list.

Public API
----------
resolve_capabilities(needed_capability_types: list[str],
                     have: set[str] | None = None,
                     *, catalog_path: str | None = None) -> dict

Returns:
{
  "resolved": [
    {"capability_type", "integration_id", "access", "cost",
     "access_state": "connected" | "needs_provisioning"}
  ],
  "gaps": [ unmet capability_types ],
  "required_inputs": [            # wire-compatible with the engine amendment schema
     {"key", "label", "type", "required", "inject_path"}
  ],
  "cost_plan": {
     "external_subscriptions": [...],
     "ad_spend": [...],
     "per_call": [...],
  },
}

`have` is the set of ALREADY-CONNECTED integration ids (e.g. provider ids already
present in the board's provider_policy). An integration in `have` is reported as
`connected` and contributes NO required_inputs (already provisioned).

required_inputs field names are intentionally identical to the engine's
hermes_cli/kanban_db.py::_normalize_amendment_required_inputs output
({key,label,type,required,inject_path}) so the result drops straight into the P5
amendment / intake provisioning path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import yaml  # PyYAML
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "capability_resolver requires PyYAML (pip install pyyaml)"
    ) from exc


DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent / "capability_catalog.yaml"

# Cost-plan buckets, keyed by the catalog `cost.model` vocabulary. `free` needs
# no plan line, so it is intentionally absent.
_COST_BUCKET_BY_MODEL: dict[str, str] = {
    "subscription": "external_subscriptions",
    "ad_spend": "ad_spend",
    "per_call": "per_call",
}


# ---------------------------------------------------------------------------
# Catalog loading (data only)
# ---------------------------------------------------------------------------
def load_catalog(catalog_path: Optional[str] = None) -> list[dict[str, Any]]:
    """Load the integration list from the YAML catalog. Pure data, no domains."""
    path = Path(catalog_path) if catalog_path else DEFAULT_CATALOG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    integrations = doc.get("integrations") or []
    if not isinstance(integrations, list):
        raise ValueError("catalog 'integrations' must be a list")
    return [i for i in integrations if isinstance(i, dict) and i.get("id")]


# ---------------------------------------------------------------------------
# required_inputs normalization -- mirror the engine shape EXACTLY
# ---------------------------------------------------------------------------
def _normalize_required_input(item: Any, inject_path_tmpl: Optional[str],
                              capability_type: str) -> Optional[dict[str, Any]]:
    """Coerce one catalog required_input into the engine amendment schema.

    Output keys/order match hermes_cli/kanban_db.py
    ::_normalize_amendment_required_inputs -> {key,label,type,required,inject_path}.

    A NON-secret input carries the integration's inject_path (with the
    {capability_type} placeholder resolved) so the engine can materialize the
    connection hint into board.json at provision time. A SECRET input deliberately
    gets inject_path=None: secrets are recorded as provisioning evidence only and
    are never written into board.json (matches the engine's secret handling).
    """
    if isinstance(item, str):
        key = item.strip()
        if not key:
            return None
        return {"key": key, "label": key, "type": "string",
                "required": True, "inject_path": None}
    if not isinstance(item, dict):
        return None
    key = str(item.get("key") or "").strip()
    if not key:
        return None
    typ = (str(item.get("type") or "string").strip().lower()) or "string"
    inject: Optional[str] = None
    if typ != "secret" and inject_path_tmpl:
        inject = inject_path_tmpl.replace("{capability_type}", capability_type).strip() or None
    env_var = str(item.get("env_var") or "").strip() or None
    result = {
        "key": key,
        "label": str(item.get("label") or key),
        "type": typ,
        "required": bool(item.get("required", True)),
        "inject_path": inject,
    }
    if env_var:
        result["env_var"] = env_var
    return result


# ---------------------------------------------------------------------------
# Resolver -- generic matcher, no domain branching
# ---------------------------------------------------------------------------
def resolve_capabilities(
    needed_capability_types: list[str],
    have: Optional[set[str]] = None,
    *,
    catalog_path: Optional[str] = None,
) -> dict[str, Any]:
    """Match needed capability_types against the catalog. Generic; no domains.

    `needed_capability_types` come from the CALLER (the model infers them from the
    goal at runtime). `have` is the set of already-connected integration ids.
    """
    have = set(have or set())
    catalog = load_catalog(catalog_path)

    # Index: capability_type -> [integration entries that provide it], preserving
    # catalog order (deterministic, stable preference).
    by_capability: dict[str, list[dict[str, Any]]] = {}
    for entry in catalog:
        for cap in entry.get("provides") or []:
            by_capability.setdefault(str(cap), []).append(entry)

    resolved: list[dict[str, Any]] = []
    gaps: list[str] = []
    required_inputs: list[dict[str, Any]] = []
    seen_input_keys: set[str] = set()
    cost_plan: dict[str, list[dict[str, Any]]] = {
        "external_subscriptions": [],
        "ad_spend": [],
        "per_call": [],
    }
    costed_integration_ids: set[str] = set()

    # Dedup needs while preserving first-seen order.
    seen_caps: set[str] = set()
    ordered_caps = [c for c in (str(x) for x in needed_capability_types)
                    if not (c in seen_caps or seen_caps.add(c))]

    for cap in ordered_caps:
        candidates = by_capability.get(cap)
        if not candidates:
            gaps.append(cap)
            continue

        # Generic preference: an already-connected integration wins; otherwise the
        # first catalog entry that provides this capability. No domain logic.
        chosen = next((e for e in candidates if e["id"] in have), candidates[0])
        integration_id = chosen["id"]
        connected = integration_id in have
        access = chosen.get("access") or {}
        cost = chosen.get("cost") or {}

        resolved.append({
            "capability_type": cap,
            "integration_id": integration_id,
            "access": access,
            "cost": cost,
            "access_state": "connected" if connected else "needs_provisioning",
        })

        if connected:
            # Already provisioned: contributes no inputs and no new cost line.
            continue

        inject_tmpl = access.get("inject_path")
        for raw in access.get("required_inputs") or []:
            spec = _normalize_required_input(raw, inject_tmpl, cap)
            if spec and spec["key"] not in seen_input_keys:
                seen_input_keys.add(spec["key"])
                required_inputs.append(spec)

        # Cost plan: one line per integration (not per capability), only for
        # non-free models, only once.
        model = str(cost.get("model") or "").strip().lower()
        bucket = _COST_BUCKET_BY_MODEL.get(model)
        if bucket and integration_id not in costed_integration_ids:
            costed_integration_ids.add(integration_id)
            cost_plan[bucket].append({
                "integration_id": integration_id,
                "model": model,
                "notes": cost.get("notes") or "",
            })

    return {
        "resolved": resolved,
        "gaps": gaps,
        "required_inputs": required_inputs,
        "cost_plan": cost_plan,
    }


# ---------------------------------------------------------------------------
# Unit test (assert resolve returns the right integrations for a sample)
# ---------------------------------------------------------------------------
def _run_unit_test() -> None:
    # Sample needs -> deterministic expected integration picks (catalog-order).
    needs = ["read:subscription_revenue", "write:ad_spend", "read:support_inbox"]
    out = resolve_capabilities(needs, have=None)

    got = {r["capability_type"]: r["integration_id"] for r in out["resolved"]}
    assert got["read:subscription_revenue"] == "app_store_connect", got
    assert got["write:ad_spend"] == "apple_search_ads", got
    assert got["read:support_inbox"] == "intercom", got
    assert out["gaps"] == [], out["gaps"]

    # Every resolved (unconnected) integration must be marked needs_provisioning.
    assert all(r["access_state"] == "needs_provisioning" for r in out["resolved"])

    # required_inputs must match the engine schema field set, exactly.
    expected_keys = {"key", "label", "type", "required", "inject_path"}
    assert out["required_inputs"], "expected some required_inputs"
    for ri in out["required_inputs"]:
        assert set(ri.keys()) == expected_keys, ri.keys()
        # secrets never carry an inject_path; non-secrets that have one carry the
        # capability_type resolved (no leftover placeholder).
        if ri["type"] == "secret":
            assert ri["inject_path"] is None, ri
        if ri["inject_path"]:
            assert "{capability_type}" not in ri["inject_path"], ri

    # An unmet need becomes a gap, not a crash.
    out2 = resolve_capabilities(["read:nonexistent_thing"], have=None)
    assert out2["resolved"] == [] and out2["gaps"] == ["read:nonexistent_thing"], out2

    # `have` flips an integration to connected and suppresses its inputs/cost.
    out3 = resolve_capabilities(["read:subscription_revenue"], have={"app_store_connect"})
    assert out3["resolved"][0]["access_state"] == "connected", out3
    assert out3["required_inputs"] == [], out3
    assert out3["cost_plan"]["external_subscriptions"] == [], out3

    # Cost-plan bucketing: ad_spend integration lands in the ad_spend bucket.
    assert any(c["integration_id"] == "apple_search_ads"
               for c in out["cost_plan"]["ad_spend"]), out["cost_plan"]

    print("unit test: PASS")


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
def _demo() -> None:
    needs = [
        "read:subscription_revenue",
        "read:install_attribution",
        "read:product_analytics",
        "write:ad_spend",
        "write:store_listing",
    ]
    print("=" * 74)
    print("DEMO: resolve an app-growth needs list (have = {} -- nothing connected)")
    print("=" * 74)
    print("needed_capability_types:")
    for n in needs:
        print(f"  - {n}")
    print()

    out = resolve_capabilities(needs, have=set())

    print("RESOLVED integrations:")
    for r in out["resolved"]:
        acc = r["access"]
        print(f"  {r['capability_type']:<28} -> {r['integration_id']}")
        print(f"      access_state : {r['access_state']}")
        print(f"      access.kind  : {acc.get('kind')}")
        print(f"      cost         : {r['cost'].get('model')} -- {r['cost'].get('notes')}")
    print()

    print(f"GAPS (unmet capability_types): {out['gaps'] or 'none'}")
    print()

    print("required_inputs (wire-ready -> engine amendment required_inputs schema):")
    print(json.dumps(out["required_inputs"], indent=2))
    print()

    print("cost_plan:")
    print(json.dumps(out["cost_plan"], indent=2))


if __name__ == "__main__":
    _run_unit_test()
    print()
    _demo()
