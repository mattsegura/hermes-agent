from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest


_REPO = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _REPO / "plugins" / "execution-gate"


@pytest.fixture(autouse=True)
def _isolate_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECUTION_GATE_LEDGER_DIR", str(tmp_path / "ledger"))


def _load_tools() -> Any:
    package_name = "execution_gate_under_test"
    sys.modules.pop(package_name, None)
    sys.modules.pop(f"{package_name}.tools", None)
    spec = importlib.util.spec_from_file_location(
        package_name,
        _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = mod
    spec.loader.exec_module(mod)
    return cast(Any, importlib.import_module(f"{package_name}.tools"))


def test_gate_intake_preserves_original_prompt_and_builds_routing_input():
    tools = _load_tools()
    original_prompt = "Check the Stripe refund, compare it with CRM notes, and draft a reply."

    payload = json.loads(
        tools.handle_gate_intake(
            {
                "original_prompt": original_prompt,
                "goal": "Investigate the customer billing issue and produce a reply-ready summary.",
                "custom_capability": "billing and CRM reconciliation with customer-response preparation",
                "required_capabilities": ["data_lookup"],
                "toolsets": ["terminal"],
                "success_criteria": [
                    "Stripe refund state checked",
                    "CRM notes compared",
                    "Reply-ready summary produced",
                ],
                "entities": ["Stripe", "CRM"],
                "unknowns": ["Which customer account?"],
                "constraints": ["Do not send the reply without approval"],
                "side_effect_risk": "internal",
                "finite_deliverable": True,
            }
        )
    )

    assert payload["ok"] is True
    assert payload["task_envelope"]["original_prompt"] == original_prompt
    assert payload["task_envelope"]["original_prompt_sha256"] == hashlib.sha256(original_prompt.encode("utf-8")).hexdigest()
    assert payload["routing_input"]["custom_capability"] == "billing and CRM reconciliation with customer-response preparation"
    assert payload["routing_input"]["toolsets"] == ["terminal"]
    assert original_prompt in payload["routing_input"]["context"]
    assert "Stripe refund state checked" in payload["routing_input"]["context"]
    assert payload["route_input"]["finite_deliverable"] is True
    assert payload["selector_status"] == "present"
    assert payload["can_dispatch"] is True


def test_gate_intake_verify_passes_specific_envelope():
    tools = _load_tools()
    original_prompt = "Check the Stripe refund, compare it with CRM notes, and draft a reply."
    intake = json.loads(
        tools.handle_gate_intake(
            {
                "original_prompt": original_prompt,
                "goal": "Investigate the customer billing issue and produce a reply-ready summary.",
                "custom_capability": "billing and CRM reconciliation with customer-response preparation",
                "toolsets": ["terminal"],
                "success_criteria": [
                    "Stripe refund state checked",
                    "CRM notes compared",
                    "Reply-ready summary produced",
                ],
                "entities": ["Stripe", "CRM"],
                "side_effect_risk": "internal",
                "finite_deliverable": True,
            }
        )
    )

    payload = json.loads(tools.handle_gate_intake_verify({"intake": intake}))

    assert payload["ok"] is True
    assert payload["verified"] is True
    assert payload["quality_score"] >= 80
    assert payload["checks"]["original_prompt_preserved"] is True
    assert payload["checks"]["selector_present"] is True
    assert payload["can_dispatch"] is True


def test_gate_conversion_ledger_summarizes_intake_and_verification_events():
    tools = _load_tools()
    original_prompt = "Check the Stripe refund, compare it with CRM notes, and draft a reply."
    intake = json.loads(
        tools.handle_gate_intake(
            {
                "original_prompt": original_prompt,
                "goal": "Investigate the customer billing issue and produce a reply-ready summary.",
                "custom_capability": "billing and CRM reconciliation with customer-response preparation",
                "toolsets": ["terminal"],
                "success_criteria": [
                    "Stripe refund state checked",
                    "CRM notes compared",
                    "Reply-ready summary produced",
                ],
                "entities": ["Stripe", "CRM"],
                "side_effect_risk": "internal",
                "finite_deliverable": True,
            }
        )
    )
    json.loads(tools.handle_gate_intake_verify({"intake": intake}))

    payload = json.loads(tools.handle_gate_conversion_ledger({"action": "summary", "limit": 20}))

    assert payload["ok"] is True
    assert payload["summary"]["stage_counts"]["intake"] == 1
    assert payload["summary"]["stage_counts"]["intake_verify"] == 1
    assert payload["summary"]["outcome_counts"]["captured"] == 1
    assert payload["summary"]["outcome_counts"]["verified"] == 1
    assert payload["summary"]["avg_quality_score"] == 100


def test_gate_conversion_ledger_summarizes_model_routes():
    tools = _load_tools()

    tools.record_conversion_event(
        {
            "stage": "dispatch",
            "outcome": "dispatch_validated_dry_run",
            "worker_profile": "worker-code",
            "custom_capability": "profile configuration risk inspection",
            "model_route": {
                "key": "deep_reasoning",
                "provider": "openai-codex",
                "model": "gpt-5.5",
            },
            "ok": True,
        }
    )

    payload = json.loads(tools.handle_gate_conversion_ledger({"action": "summary", "limit": 20}))

    assert payload["ok"] is True
    assert payload["summary"]["provider_counts"]["openai-codex"] == 1
    assert payload["summary"]["model_route_counts"]["deep_reasoning/openai-codex/gpt-5.5"] == 1
    assert payload["recent_events"][-1]["model_key"] == "deep_reasoning"
    assert payload["recent_events"][-1]["model_provider"] == "openai-codex"
    assert payload["recent_events"][-1]["model"] == "gpt-5.5"


def test_gate_intake_verify_blocks_shallow_envelope():
    tools = _load_tools()

    payload = json.loads(
        tools.handle_gate_intake_verify(
            {
                "task_envelope": {
                    "original_prompt": "Check the Stripe refund.",
                    "goal": "done",
                    "custom_capability": "finance",
                    "success_criteria": ["done"],
                    "side_effect_risk": "none",
                }
            }
        )
    )

    issue_ids = {item["id"] for item in payload["blocking_issues"]}
    assert payload["ok"] is False
    assert payload["verified"] is False
    assert "weak_goal" in issue_ids
    assert "generic_custom_capability" in issue_ids
    assert "too_few_success_criteria" in issue_ids
    assert "generic_success_criteria" in issue_ids
    assert "missing_policy_selector" in issue_ids


def test_gate_intake_verify_blocks_missing_expected_entities():
    tools = _load_tools()

    payload = json.loads(
        tools.handle_gate_intake_verify(
            {
                "expected_entities": ["Stripe", "CRM"],
                "task_envelope": {
                    "original_prompt": "Check the Stripe refund and compare it with CRM notes.",
                    "goal": "Investigate the customer billing issue and produce a summary.",
                    "custom_capability": "billing and CRM reconciliation for customer support",
                    "toolsets": ["terminal"],
                    "success_criteria": ["Stripe refund checked", "CRM notes compared"],
                    "entities": ["Stripe"],
                    "side_effect_risk": "internal",
                },
            }
        )
    )

    issue_ids = {item["id"] for item in payload["blocking_issues"]}
    assert payload["ok"] is False
    assert "expected_entities_missing" in issue_ids
    assert payload["blocking_issues"][0]["items"] == ["CRM"]


def test_gate_intake_verify_blocks_understated_financial_side_effect():
    tools = _load_tools()

    payload = json.loads(
        tools.handle_gate_intake_verify(
            {
                "task_envelope": {
                    "original_prompt": "Refund the customer in Stripe.",
                    "goal": "Issue a Stripe refund for the customer.",
                    "custom_capability": "customer refund execution in Stripe",
                    "toolsets": ["terminal"],
                    "success_criteria": ["Refund request validated", "Refund result recorded"],
                    "entities": ["Stripe"],
                    "side_effect_risk": "none",
                }
            }
        )
    )

    issue_ids = {item["id"] for item in payload["blocking_issues"]}
    assert payload["ok"] is False
    assert "side_effect_risk_understated" in issue_ids


def test_gate_intake_requires_success_criteria():
    tools = _load_tools()

    payload = json.loads(
        tools.handle_gate_intake(
            {
                "original_prompt": "Inspect this repo.",
                "goal": "Inspect the repo.",
                "custom_capability": "repo inspection",
            }
        )
    )

    assert payload["ok"] is False
    assert payload["error"] == "success_criteria is required"


def test_gate_intake_external_actions_require_side_effect_risk():
    tools = _load_tools()

    payload = json.loads(
        tools.handle_gate_intake(
            {
                "original_prompt": "Refund the customer in Stripe.",
                "goal": "Refund the customer.",
                "custom_capability": "customer refund execution",
                "success_criteria": ["Refund completed"],
                "external_actions": ["Issue a Stripe refund"],
            }
        )
    )

    assert payload["ok"] is False
    assert payload["error"] == "external_actions require side_effect_risk other than none"
