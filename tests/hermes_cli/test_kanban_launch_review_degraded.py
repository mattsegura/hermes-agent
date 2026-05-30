"""Regression tests: the launch/review path degrades non-conformant contracts
to a structured readiness error instead of crashing with a raw traceback.

Found while dogfooding the four golden domain contracts: ``grow_app_one_week``
carries (a) a ``provider_policy.global_rules`` list of guardrail strings and
(b) a workflow stage whose ``exit_criteria`` is a single object rather than a
list. ``validate_business_runtime_contract`` already degraded both to a clean
``{ok: False, ...}`` result, but ``review_business_launch_contract`` raised an
uncaught ``ValueError`` (operator saw a stack trace, not an actionable error).
"""

from __future__ import annotations

import copy
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


def _ready_contract() -> dict:
    """A minimal launch-grammar-conformant contract (mirrors the runtime tests)."""
    return {
        "objective": {
            "statement": "Run a mock board",
            "success": ["proof is structured"],
            "failure": ["worker escapes contract"],
            "constraints": ["mock only"],
        },
        "runtime": {
            "mode": "company",
            "dispatcher": {"profile": "mock-ceo"},
            "profiles": {"ceo": "mock-ceo", "optimizer": "mock-optimizer", "worker": "mock-worker"},
            "require_worker_envelopes": True,
            "require_provider_policy": True,
            "provider_policy": {"mock_research": {"provider": "fixture", "tool": "mock"}},
            "worker_envelopes": {
                "mock-worker": {
                    "capabilities": ["mock_research"],
                    "toolsets": ["kanban"],
                    "allowed_side_effects": ["none"],
                }
            },
        },
        "workflow": {
            "id": "flow",
            "goal_id": "mock-goal",
            "require_semantics": True,
            "workstreams": [{"key": "ops", "stages": ["execute", "done"]}],
            "stages": [
                {
                    "key": "execute",
                    "actions": [
                        {
                            "key": "research",
                            "required_capabilities": ["mock_research"],
                            "required_toolsets": ["kanban"],
                            "required_proof": ["mock_report"],
                            "side_effect_class": "none",
                        }
                    ],
                    "exit_criteria": [{"transition": "done", "evidence_required": ["mock_report"]}],
                },
                {"key": "done", "actions": [{"key": "archive"}], "terminal": True, "exit_criteria": []},
            ],
        },
        "entities": [
            {"key": "item", "type": "work_item", "states": ["open", "done"], "terminal_states": ["done"]}
        ],
        "event_loops": [{"type": "timer", "entity": "item", "terminal_states": ["done"]}],
        "approval_gates": [{"key": "gate", "required_before": ["research"]}],
        "proof_requirements": ["mock_report"],
        "side_effect_policy": {"allowed": ["none"], "forbidden": ["x"]},
        "escalation_paths": [{"condition": "unclear", "to": "mock-ceo"}],
        "owner_summary": {"summary": "Mock board runs contracted work with proof."},
    }


# ---------------------------------------------------------------------------
# 1) _normalize_policy_map is total: legitimate non-route policy metadata
#    (e.g. provider_policy.global_rules) is preserved, not crashed on.
# ---------------------------------------------------------------------------


def test_policy_map_preserves_non_object_metadata_values():
    rules = ["Use read-only access whenever possible.", "Do not exceed $50/day spend."]
    out = kb._normalize_policy_map(
        {
            "mock_research": {"provider": "fixture"},
            "global_rules": rules,
        },
        field="runtime.provider_policy",
    )
    # The real route is preserved as an object...
    assert out["mock_research"] == {"provider": "fixture"}
    # ...and the cross-cutting metadata list is preserved verbatim (no crash).
    assert out["global_rules"] == rules


def test_normalize_policy_map_does_not_raise_on_scalar_value():
    # A bad scalar route is preserved (fail-closed happens later at the dispatch
    # gate via _policy_has_material_route), never a normalization crash.
    out = kb._normalize_policy_map({"openai": "gpt-4"}, field="runtime.provider_policy")
    assert out["openai"] == "gpt-4"
    assert kb._policy_has_material_route(out["openai"]) is False


def test_contract_with_global_rules_provider_policy_is_launchable(fresh_home):
    contract = _ready_contract()
    contract["runtime"]["provider_policy"]["global_rules"] = [
        "Keep audit logs of all drafts and approvals.",
    ]
    v = kb.validate_business_runtime_contract(contract)
    assert v["ok"] is True, v
    # And it actually launches without raising.
    kb.review_business_launch_contract("gr", contract=contract, create_if_missing=True)
    token = kb.issue_board_launch_approval_token(
        "gr", contract=contract, approved_by="owner",
        approval_evidence={"source": "test"}, owner_authority_confirmed=True,
    )["token"]
    res = kb.review_business_launch_contract("gr", contract=contract, approve=True, approval_token=token)
    assert res["ok"] is True
    assert res["launch_phase"] == "active"


# ---------------------------------------------------------------------------
# 2) review_business_launch_contract degrades a non-normalizable contract to a
#    structured readiness error and leaves board state untouched (no traceback).
# ---------------------------------------------------------------------------


def test_review_degrades_on_malformed_exit_criteria_no_crash(fresh_home):
    bad = _ready_contract()
    # Single object instead of a list -> normalize_workflow_definition rejects it.
    bad["workflow"]["stages"][0]["exit_criteria"] = {
        "transition": "done", "evidence_required": ["mock_report"]
    }
    res = kb.review_business_launch_contract("badx", contract=bad, create_if_missing=True)
    assert res["ok"] is False
    assert res["status"] == "invalid"
    assert any("exit_criteria must be a list" in e for e in res["readiness"]["errors"])
    # No board was created from a contract too malformed to normalize.
    assert kb.board_exists("badx") is False


def test_review_degrades_does_not_clobber_existing_active_board(fresh_home):
    good = _ready_contract()
    kb.review_business_launch_contract("keep", contract=good, create_if_missing=True)
    token = kb.issue_board_launch_approval_token(
        "keep", contract=good, approved_by="owner",
        approval_evidence={"source": "test"}, owner_authority_confirmed=True,
    )["token"]
    kb.review_business_launch_contract("keep", contract=good, approve=True, approval_token=token)
    assert kb.read_board_metadata("keep")["contract_version"] == 1

    bad = _ready_contract()
    bad["workflow"]["stages"][0]["exit_criteria"] = {"transition": "done"}
    res = kb.review_business_launch_contract("keep", contract=bad)
    assert res["ok"] is False and res["status"] == "invalid"
    # The live active board is untouched.
    meta = kb.read_board_metadata("keep")
    assert meta["launch_phase"] == "active"
    assert meta["contract_version"] == 1


def test_build_draft_raises_typed_normalization_error():
    bad = _ready_contract()
    bad["workflow"]["stages"][0]["exit_criteria"] = {"transition": "done"}
    with pytest.raises(kb.ContractNormalizationError):
        kb.build_business_runtime_contract_draft(bad)

    # The typed error is a ValueError subclass, so legacy ``except ValueError``
    # handlers (validate_business_runtime_contract) keep working unchanged.
    assert issubclass(kb.ContractNormalizationError, ValueError)


# NOTE: the narrowed catch must NOT swallow intake-flow control-signal
# ValueErrors (stale answers_hash / round mismatch). That propagation is locked
# in by test_kanban_boards.py::test_intake_contract_draft_is_bound_to_latest_answers
# and the duplicate-answer tests, which set up the required saved-answer state.
