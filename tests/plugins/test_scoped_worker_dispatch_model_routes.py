from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, cast

import pytest


_REPO = Path(__file__).resolve().parents[2]
_PLUGIN = _REPO / "plugins" / "scoped-worker-dispatch" / "__init__.py"


@pytest.fixture(autouse=True)
def _isolate_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECUTION_GATE_LEDGER_DIR", str(tmp_path / "ledger"))


def _load_plugin(tmp_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("scoped_worker_dispatch_under_test", _PLUGIN)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    setattr(mod, "get_hermes_home", lambda: str(tmp_path))
    return cast(Any, mod)


def _write_config(tmp_path: Path, *, require_verified_task_envelope: bool = False) -> None:
    (tmp_path / "profiles" / "worker-code").mkdir(parents=True)
    (tmp_path / "profiles" / "worker-code" / "config.yaml").write_text("model:\n  provider: test\n", encoding="utf-8")
    (tmp_path / "profiles" / "worker-research").mkdir(parents=True)
    (tmp_path / "profiles" / "worker-research" / "config.yaml").write_text("model:\n  provider: test\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        f"""
worker_scope:
  enabled: true
  hermes_bin: /bin/hermes
  default_scope:
    type: tenant
    id: demo
  default_timeout_seconds: 100
  max_batch: 3
  require_verified_task_envelope: {str(require_verified_task_envelope).lower()}
  min_intake_quality_score: 80
  scopes:
  - type: tenant
    id: demo
    workers:
      worker-code:
        description: Code worker.
        capabilities:
        - code
        - testing
        - terminal
        priority: 20
        models:
        - key: deep_reasoning
          provider: ccapi
          model: claude-opus-4-8-xhigh
          description: Hard tasks.
          default: true
        - key: standard
          provider: ccapi
          model: claude-sonnet-4-6
          description: Routine tasks.
          default: false
        toolsets:
        - file
        - terminal
        skills: []
        max_timeout_seconds: 120
        yolo: false
      worker-research:
        description: Research worker.
        capabilities:
        - web_research
        - source_synthesis
        priority: 10
        models:
        - key: deep_reasoning
          provider: openai-codex
          model: gpt-5.5
          description: Research tasks.
          default: true
        toolsets:
        - web
        skills: []
        max_timeout_seconds: 120
        yolo: false
""".lstrip(),
        encoding="utf-8",
    )


def test_policy_exposes_worker_model_routes(tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(tmp_path)

    payload = json.loads(mod._handle_policy({}))

    assert payload["ok"] is True
    models = payload["workers"]["worker-code"]["models"]
    assert [m["key"] for m in models] == ["deep_reasoning", "standard"]
    assert models[0]["provider"] == "ccapi"
    assert models[0]["default"] is True
    assert payload["workers"]["worker-code"]["capabilities"] == ["code", "testing", "terminal"]


def test_match_selects_worker_by_required_capabilities(tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(tmp_path)

    payload = json.loads(
        mod._handle_match(
            {
                "required_capabilities": ["web_research"],
                "toolsets": ["web"],
            }
        )
    )

    assert payload["ok"] is True
    assert payload["selected_profile"] == "worker-research"
    assert payload["selected_worker"]["matched_capabilities"] == ["web_research"]


def test_match_rejects_custom_capability_without_policy_selector(tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(tmp_path)

    payload = json.loads(
        mod._handle_match(
            {
                "custom_capability": "reconcile a Stripe refund against an internal invoice ledger",
            }
        )
    )

    assert payload["ok"] is False
    assert payload["error"] == "custom_capability_requires_policy_selector"
    assert payload["custom_capability"] == "reconcile a Stripe refund against an internal invoice ledger"
    assert payload["custom_capability_verified_by_policy"] is False


def test_match_carries_custom_capability_with_hard_toolset_selector(tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(tmp_path)

    payload = json.loads(
        mod._handle_match(
            {
                "toolsets": ["terminal"],
                "custom_capability": "inspect a local profile config and explain its runtime risk",
            }
        )
    )

    assert payload["ok"] is True
    assert payload["selected_profile"] == "worker-code"
    assert payload["custom_capability"] == "inspect a local profile config and explain its runtime risk"
    assert payload["custom_capability_verified_by_policy"] is False


def test_dispatch_dry_run_uses_selected_model_route(tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(tmp_path)

    payload = json.loads(
        mod._handle_dispatch(
            {
                "profile": "worker-code",
                "goal": "do a dry run",
                "model_key": "standard",
                "dry_run": True,
            }
        )
    )

    assert payload["ok"] is True
    assert payload["model_route"]["key"] == "standard"
    assert payload["model_route"]["model"] == "claude-sonnet-4-6"
    assert "--provider" in payload["command"]
    assert "ccapi" in payload["command"]
    assert "-m" in payload["command"]
    assert "claude-sonnet-4-6" in payload["command"]


def test_dispatch_dry_run_explicit_profile_carries_custom_capability(tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(tmp_path)

    payload = json.loads(
        mod._handle_dispatch(
            {
                "profile": "worker-code",
                "goal": "do a dry run",
                "custom_capability": "inspect a local profile config and explain its runtime risk",
                "dry_run": True,
            }
        )
    )

    assert payload["ok"] is True
    assert payload["profile"] == "worker-code"
    assert payload["selection"]["selected_by"] == "explicit_profile"
    assert payload["selection"]["custom_capability"] == "inspect a local profile config and explain its runtime risk"
    assert payload["selection"]["custom_capability_verified_by_policy"] is False


def test_build_prompt_includes_custom_capability_request(tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(tmp_path)

    prompt = mod._build_prompt(
        "inspect config",
        "context",
        "tenant",
        "demo",
        "worker-code",
        "inspect a local profile config and explain its runtime risk",
    )

    assert "CUSTOM CAPABILITY REQUEST:" in prompt
    assert "inspect a local profile config and explain its runtime risk" in prompt


def test_task_envelope_merges_into_worker_match(tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(tmp_path)

    envelope = {
        "original_prompt": "Inspect the Hermes profile config and explain whether the runtime route is risky.",
        "goal": "Inspect the Hermes profile config and explain runtime/provider risks.",
        "custom_capability": "profile configuration risk inspection",
        "toolsets": ["terminal"],
        "success_criteria": ["Profile config inspected", "Runtime/provider risks explained"],
        "side_effect_risk": "none",
    }
    payload = json.loads(mod._handle_match({"task_envelope": envelope}))

    assert payload["ok"] is True
    assert payload["selected_profile"] == "worker-code"
    assert payload["custom_capability"] == "profile configuration risk inspection"
    assert payload["requested_toolsets"] == ["terminal"]


def test_task_envelope_preserves_original_prompt_in_dispatch_context(tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(tmp_path)

    envelope = {
        "original_prompt": "Inspect the Hermes profile config and explain whether the runtime route is risky.",
        "goal": "Inspect the Hermes profile config and explain runtime/provider risks.",
        "custom_capability": "profile configuration risk inspection",
        "toolsets": ["terminal"],
        "success_criteria": ["Profile config inspected", "Runtime/provider risks explained"],
        "side_effect_risk": "none",
    }
    merged = mod._merge_task_envelope({"task_envelope": envelope})

    assert merged["goal"] == "Inspect the Hermes profile config and explain runtime/provider risks."
    assert merged["custom_capability"] == "profile configuration risk inspection"
    assert merged["toolsets"] == ["terminal"]
    assert "ORIGINAL USER PROMPT:" in merged["context"]
    assert envelope["original_prompt"] in merged["context"]
    assert "Runtime/provider risks explained" in merged["context"]


def test_dispatch_requires_task_envelope_when_scope_requires_verification(tmp_path):
    _write_config(tmp_path, require_verified_task_envelope=True)
    mod = _load_plugin(tmp_path)

    payload = json.loads(
        mod._handle_dispatch(
            {
                "goal": "run tests",
                "toolsets": ["terminal"],
                "dry_run": True,
            }
        )
    )

    assert payload["ok"] is False
    assert payload["error"] == "unverified_task_envelope"
    assert payload["verification"]["blocking_issues"][0]["id"] == "missing_task_envelope"


def test_dispatch_blocks_unverified_task_envelope_when_scope_requires_verification(tmp_path):
    _write_config(tmp_path, require_verified_task_envelope=True)
    mod = _load_plugin(tmp_path)

    payload = json.loads(
        mod._handle_dispatch(
            {
                "task_envelope": {
                    "original_prompt": "Refund the customer in Stripe.",
                    "goal": "Issue a Stripe refund for the customer.",
                    "custom_capability": "customer refund execution in Stripe",
                    "toolsets": ["terminal"],
                    "success_criteria": ["Refund request validated", "Refund result recorded"],
                    "entities": ["Stripe"],
                    "side_effect_risk": "none",
                },
                "dry_run": True,
            }
        )
    )

    issue_ids = {item["id"] for item in payload["verification"]["blocking_issues"]}
    assert payload["ok"] is False
    assert payload["error"] == "unverified_task_envelope"
    assert "side_effect_risk_understated" in issue_ids


def test_dispatch_allows_verified_task_envelope_when_scope_requires_verification(tmp_path):
    _write_config(tmp_path, require_verified_task_envelope=True)
    mod = _load_plugin(tmp_path)

    payload = json.loads(
        mod._handle_dispatch(
            {
                "task_envelope": {
                    "original_prompt": "Inspect the Hermes profile config and explain whether the runtime route is risky.",
                    "goal": "Inspect the Hermes profile config and explain runtime/provider risks.",
                    "custom_capability": "profile configuration risk inspection for Hermes runtime",
                    "toolsets": ["terminal"],
                    "success_criteria": ["Profile config inspected", "Runtime/provider risks explained"],
                    "entities": ["Hermes"],
                    "side_effect_risk": "none",
                },
                "dry_run": True,
            }
        )
    )

    assert payload["ok"] is True
    assert payload["profile"] == "worker-code"
    assert payload["intake_verification"]["verified"] is True
    assert payload["intake_verification"]["quality_score"] >= 80
    rows = [
        json.loads(line)
        for line in (tmp_path / "ledger" / "conversion_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    dispatch_rows = [row for row in rows if row["stage"] == "dispatch"]
    assert dispatch_rows[-1]["outcome"] == "dispatch_validated_dry_run"
    assert dispatch_rows[-1]["worker_profile"] == "worker-code"
    assert dispatch_rows[-1]["custom_capability"] == "profile configuration risk inspection for Hermes runtime"
    assert dispatch_rows[-1]["model_key"] == "deep_reasoning"
    assert dispatch_rows[-1]["model_provider"] == "ccapi"
    assert dispatch_rows[-1]["model"] == "claude-opus-4-8-xhigh"


def test_dispatch_dry_run_auto_selects_worker_by_capability(tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(tmp_path)

    payload = json.loads(
        mod._handle_dispatch(
            {
                "goal": "run tests",
                "required_capabilities": ["testing"],
                "toolsets": ["terminal"],
                "dry_run": True,
            }
        )
    )

    assert payload["ok"] is True
    assert payload["profile"] == "worker-code"
    assert payload["selection"]["selected_by"] == "capability_match"
    assert payload["selection"]["selected_worker"]["matched_capabilities"] == ["testing"]


def test_dispatch_dry_run_fails_closed_for_unknown_capability(tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(tmp_path)

    payload = json.loads(
        mod._handle_dispatch(
            {
                "goal": "reconcile invoices",
                "required_capabilities": ["finance_reconciliation"],
                "dry_run": True,
            }
        )
    )

    assert payload["ok"] is False
    assert payload["error"] == "no_matching_worker"
    assert payload["candidates"][0]["missing_capabilities"] == ["finance_reconciliation"]


def test_dispatch_rejects_unconfigured_provider_model_pair(tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(tmp_path)

    payload = json.loads(
        mod._handle_dispatch(
            {
                "profile": "worker-code",
                "goal": "do a dry run",
                "provider": "openai-codex",
                "model": "gpt-5.5",
                "dry_run": True,
            }
        )
    )

    assert payload["ok"] is False
    assert "not allowed" in payload["error"]


def test_batch_dry_run_includes_each_resolved_model_route(tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(tmp_path)

    payload = json.loads(
        mod._handle_batch(
            {
                "dry_run": True,
                "tasks": [
                    {"profile": "worker-code", "goal": "hard task"},
                    {"profile": "worker-code", "goal": "routine task", "model_key": "standard"},
                ],
            }
        )
    )

    assert payload["ok"] is True
    assert [r["model_route"]["key"] for r in payload["results"]] == [
        "deep_reasoning",
        "standard",
    ]
