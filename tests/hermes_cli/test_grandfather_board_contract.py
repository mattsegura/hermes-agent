"""Tests for the grandfather migration (scripts/grandfather_board_contract.py).

Runs entirely inside a throwaway temp HERMES_HOME -- it never reads or writes the
operator's live kanban. Uses the in-repo land-wholesaling contract fixture as the
representative hand-authored contract to grandfather.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb

_FIXTURES = _WORKTREE / "tests" / "fixtures" / "launch_intake"
_SCRIPT = _WORKTREE / "scripts" / "grandfather_board_contract.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("grandfather_board_contract", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


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


def _land_contract():
    with open(_FIXTURES / "land_wholesaling.contract.json") as fh:
        return json.load(fh)


def _ready_contract():
    """A launch-ready (passes contract readiness) land-themed contract."""
    return {
        "objective": {
            "statement": "Acquire and assign land-wholesaling contracts at a profit.",
            "success": ["s1", "s2", "s3", "s4"],
            "failure": ["f1", "f2", "f3", "f4"],
            "constraints": ["owner-approved outreach only"],
        },
        "runtime": {
            "mode": "company",
            "dispatcher": {"profile": "land"},
            "profiles": {"ceo": "land-ceo", "optimizer": "land-optimizer", "worker": "land-worker"},
            "require_provider_policy": True,
            "require_worker_envelopes": True,
            "provider_policy": {"skip_trace": {"provider": "fixture", "tool": "mock"}},
            "worker_envelopes": {
                "land-worker": {
                    "capabilities": ["skip_trace"],
                    "toolsets": ["kanban"],
                    "allowed_side_effects": ["read_only"],
                    "required_proof": ["skip_trace_record"],
                }
            },
        },
        "workflow": {
            "id": "land-flow",
            "goal_id": "land-goal",
            "require_semantics": True,
            "workstreams": [{"key": "ops", "stages": ["qualify", "done"]}],
            "stages": [
                {
                    "key": "qualify",
                    "actions": [
                        {
                            "key": "skip_trace",
                            "required_capabilities": ["skip_trace"],
                            "required_toolsets": ["kanban"],
                            "required_proof": ["skip_trace_record"],
                            "side_effect_class": "none",
                        }
                    ],
                    "triggers": [{"type": "timer", "key": "tick", "cadence_hours": 24}],
                    "exit_criteria": [{"transition": "done", "evidence_required": ["skip_trace_record"]}],
                },
                {"key": "done", "actions": [{"key": "archive"}], "exit_criteria": []},
            ],
        },
        "entities": [{"key": "parcel", "type": "parcel", "states": ["open", "done"], "terminal_states": ["done"]}],
        "event_loops": [{"type": "timer", "entity": "parcel", "terminal_states": ["done"]}],
        "approval_gates": [{"key": "outreach_approval", "required_before": ["skip_trace"]}],
        "proof_requirements": ["skip_trace_record"],
        "side_effect_policy": {"allowed": ["read_only"], "forbidden": ["unapproved_outreach"]},
        "escalation_paths": [{"condition": "unclear", "to": "land-ceo"}],
        "owner_summary": {"summary": "Land wholesaling pipeline."},
    }


def test_dry_run_writes_nothing(fresh_home):
    mod = _load_script_module()
    report = mod.grandfather_contract("gf-land", _land_contract(), apply=False)
    assert report["status"] == "dry_run"
    assert report["applied"] is False
    assert report["invariants_ok"] is True
    # No board was created.
    assert not kb.board_exists("gf-land")


def test_apply_activates_board(fresh_home):
    mod = _load_script_module()
    report = mod.grandfather_contract(
        "gf-land", _ready_contract(), apply=True, approved_by="owner@test"
    )
    assert report["status"] == "applied"
    assert report["applied"] is True
    assert report["launch_phase"] == "active"
    assert kb.board_exists("gf-land")

    # The stamped intake is grandfathered (and therefore exempt from owner-ack).
    meta = kb.read_board_metadata("gf-land")
    contract = kb._metadata_as_business_contract(meta)
    intake = (contract or {}).get("launch_intake") or {}
    assert intake.get("source") == "grandfathered"


def test_blocked_when_invariants_fail(fresh_home):
    mod = _load_script_module()
    broken = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "event_loops": [
            {"type": "loop", "entity": "lead", "triggers": ["lead_reply"]}  # no timer, no stop
        ],
    }
    report = mod.grandfather_contract("gf-broken", broken, apply=True)
    assert report["status"] == "blocked"
    assert report["applied"] is False
    assert not kb.board_exists("gf-broken")
