"""Phase 4: owner coverage acknowledgment gate on launch-token issuance.

A model-synthesized intake contract (``launch_intake.source == "model_generated"``
carrying a coverage report) must not mint a launch-approval token until the owner
acknowledges the interpreted coverage report. Hand-authored / grandfathered
contracts (no model_generated launch_intake) remain exempt.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _model_generated_contract():
    contract = {
        "objective": {
            "statement": "Mock model-synthesized objective for owner-ack gate test.",
            "success": ["s1", "s2", "s3", "s4"],
            "failure": ["f1", "f2", "f3", "f4"],
            "constraints": ["c1"],
        },
        "runtime": {
            "mode": "company",
            "dispatcher": {"profile": "mock"},
            "profiles": {"ceo": "mock-ceo", "optimizer": "mock-optimizer", "worker": "mock-worker"},
            "require_provider_policy": True,
            "require_worker_envelopes": True,
            "provider_policy": {"research": {"provider": "fixture"}},
            "worker_envelopes": {
                "mock-worker": {
                    "capabilities": ["research"],
                    "toolsets": ["kanban"],
                    "allowed_side_effects": ["read_only"],
                    "required_proof": ["research_log"],
                }
            },
        },
        "workflow": {
            "id": "mock-workflow",
            "goal_id": "mock-goal",
            "require_semantics": True,
            "workstreams": [{"key": "ops", "stages": ["execute", "done"]}],
            "stages": [
                {
                    "key": "execute",
                    "actions": [
                        {
                            "key": "research",
                            "required_capabilities": ["research"],
                            "required_toolsets": ["kanban"],
                            "required_proof": ["research_log"],
                            "side_effect_class": "read_only",
                        }
                    ],
                    "triggers": [{"type": "timer", "key": "tick"}],
                    "exit_criteria": [{"transition": "done", "evidence_required": ["research_log"]}],
                },
                {"key": "done", "actions": [{"key": "archive"}], "exit_criteria": []},
            ],
        },
        "entities": [{"key": "wi", "type": "work_item", "states": ["open", "done"], "terminal_states": ["done"]}],
        "event_loops": [{"type": "timer", "entity": "wi", "terminal_states": ["done"]}],
        "approval_gates": [{"key": "side_effect_approval", "required_before": ["research"]}],
        "proof_requirements": ["research_log"],
        "side_effect_policy": {"allowed": ["read_only"], "forbidden": ["x"]},
        "escalation_paths": [{"condition": "unclear", "to": "mock-ceo"}],
        "owner_summary": {"summary": "mock"},
        "launch_intake": {
            "source": "model_generated",
            "state": "ready_for_owner_review",
            "coverage": {"score": 0.83, "passed": True, "gaps": []},
            "invariants": {"ok": True, "errors": [], "warnings": []},
        },
    }
    return contract


def test_model_generated_contract_requires_owner_ack(fresh_home):
    contract = _model_generated_contract()
    kb.review_business_launch_contract("ackboard", contract=contract, create_if_missing=True)

    with pytest.raises(ValueError, match="acknowledge the launch coverage report"):
        kb.issue_board_launch_approval_token(
            "ackboard",
            contract=contract,
            approved_by="test-owner",
            approval_evidence={"source": "phase4-test"},
            owner_authority_confirmed=True,
        )


def test_model_generated_contract_passes_with_ack(fresh_home):
    contract = _model_generated_contract()
    kb.review_business_launch_contract("ackboard2", contract=contract, create_if_missing=True)

    result = kb.issue_board_launch_approval_token(
        "ackboard2",
        contract=contract,
        approved_by="test-owner",
        approval_evidence={"source": "phase4-test"},
        owner_authority_confirmed=True,
        owner_acknowledged_coverage=True,
    )
    assert result["ok"] is True
    assert result["token"]


def test_hand_authored_contract_is_exempt(fresh_home):
    # No launch_intake / not model_generated -> no acknowledgment required.
    contract = _model_generated_contract()
    contract.pop("launch_intake")
    kb.review_business_launch_contract("plainboard", contract=contract, create_if_missing=True)

    result = kb.issue_board_launch_approval_token(
        "plainboard",
        contract=contract,
        approved_by="test-owner",
        approval_evidence={"source": "phase4-test"},
        owner_authority_confirmed=True,
    )
    assert result["ok"] is True
