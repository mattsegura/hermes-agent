"""Regression net for the unified LaunchCompletenessSpec (hermes_cli/launch_completeness.py).

This module ports the in-module ``__main__`` self-test into pytest and adds full
enforce-mode coverage for every critical dimension. It is the safety net that must
stay green before any report->enforce flip of the dispatch gate (the keystone work):
it pins, for each dimension, that

  * report mode surfaces the gap as a WARNING (never a hard error / never flips ok),
  * enforce mode promotes that same gap to a hard ERROR (ok=False), and
  * a structurally clean contract passes BOTH modes (the false-positive guard that
    protects the live land-wholesaling board when enforce is eventually flipped on).

Each dimension also has a paired false-positive guard so a future change cannot make
the checker fire on a well-formed contract.
"""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

from hermes_cli.launch_completeness import assess_launch_completeness as assess

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "launch_intake"


def _base() -> dict:
    """The self-test template. NOT structurally clean on its own (its action's
    side_effect_class is undeclared in side_effect_policy) -- used only as a base
    to mutate one dimension at a time and observe that dimension's finding."""
    return {
        "objective": {
            "statement": "x",
            "success": ["MRR to $12k by day 7"],
            "failure": ["churn > 10%"],
            "constraints": ["no spam"],
        },
        "workflow": {"stages": [{"key": "s1", "actions": [{"key": "a1", "side_effect_class": "internal"}]}]},
        "event_loops": [
            {"entity": "lead", "triggers": ["t"], "terminal_states": ["closed"], "stop_conditions": ["done"]}
        ],
        "tunables": {},
    }


def _clean() -> dict:
    """A structurally sound contract that passes invariants AND every net-new
    dimension -> ok=True in both report and enforce mode. The anchor for the
    'enforce must not break a good contract' guarantee."""
    return {
        "objective": {
            "statement": "Grow MRR",
            "success": ["MRR to $12k by day 7"],
            "failure": ["churn > 10%"],
            "constraints": ["no spam"],
        },
        "workflow": {
            "stages": [
                {
                    "key": "s1",
                    "actions": [{"key": "a1", "side_effect_class": "none"}],
                    "triggers": [{"type": "timer", "key": "tick", "cadence_hours": 24}],
                    "exit_criteria": [{"transition": "done", "evidence_required": []}],
                },
                {"key": "done", "terminal": True, "exit_criteria": []},
            ]
        },
        "event_loops": [
            {"entity": "lead", "triggers": ["daily_timer"], "terminal_states": ["closed"], "stop_conditions": ["done"]}
        ],
    }


def _has(findings: list[str], dim: str, needle: str = "") -> bool:
    return any(dim in f and (not needle or needle in f) for f in findings)


# --------------------------------------------------------------------------- #
# Clean contract: the false-positive guard for the eventual enforce flip
# --------------------------------------------------------------------------- #

def test_clean_contract_passes_report_mode():
    r = assess(_clean())
    assert r["ok"], r["errors"]


def test_clean_contract_passes_enforce_mode():
    """The single most important guard: turning enforce on must NOT block a
    structurally sound contract (otherwise flipping the gate stalls good boards)."""
    r = assess(_clean(), enforce=True)
    assert r["ok"], r["errors"]


# --------------------------------------------------------------------------- #
# Golden fixtures: invariants must pass in report mode (they model real boards)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fixture", ["grow_app_one_week.contract.json", "land_wholesaling.contract.json"])
def test_golden_fixtures_pass_invariants_report_mode(fixture):
    p = FIXTURES / fixture
    if not p.exists():
        pytest.skip(f"fixture {fixture} not present")
    r = assess(json.loads(p.read_text()))  # report mode
    assert r["ok"], f"{fixture} should pass invariants in report mode: {r['errors']}"


def test_managed_cadence_knob_not_flagged_dead():
    """False-positive guard: an optimizer-managed cadence knob (follow_up_interval_hours)
    must NOT be reported as an unread/dead tunable on the goldens."""
    seen = False
    for fixture in ("grow_app_one_week.contract.json", "land_wholesaling.contract.json"):
        p = FIXTURES / fixture
        if not p.exists():
            continue
        seen = True
        r = assess(json.loads(p.read_text()))
        tun = " ".join(r["dimensions"].get("tunable_consumer_binding", {}).get("findings", []))
        assert "follow_up_interval_hours" not in tun, f"{fixture}: managed knob wrongly flagged: {tun}"
    if not seen:
        pytest.skip("no goldens present")


# --------------------------------------------------------------------------- #
# Report mode: each critical/structural dimension surfaces a WARNING
# --------------------------------------------------------------------------- #

def _mut_prose_success():
    c = _base(); c["objective"]["success"] = ["close some deals", "do a great job"]; return c


def _mut_missing_side_effect_class():
    c = _base(); c["workflow"]["stages"][0]["actions"].append({"key": "send_sms", "label": "text the seller"}); return c


def _mut_freetext_loop_stop():
    c = _base(); c["event_loops"] = [{"entity": "lead", "triggers": ["t"], "stop_conditions": ["when done"]}]; return c


def _mut_unread_tunable():
    c = _base(); c["tunables"] = {"some_business_threshold": {"default": 5, "range": [1, 10]}}; return c


def _mut_no_win_terminal():
    c = _base()
    c["event_loops"] = [{"entity": "lead", "triggers": ["inbound_reply", "follow_up_timer"],
                         "terminal_states": ["dead", "recycled", "paused"]}]
    return c


def _mut_orphan_evidence():
    c = _base()
    c["workflow"]["stages"] = [
        {"key": "s1", "actions": [{"key": "a1", "side_effect_class": "internal"}],
         "exit_criteria": [{"transition": "done", "evidence_required": ["ghost_artifact"]}]},
        {"key": "done", "terminal": True, "exit_criteria": []},
    ]
    c["proof_requirements"] = ["some_other_proof"]
    return c


def _mut_island_stage():
    c = _base()
    c["workflow"]["stages"] = [
        {"key": "s1", "exit_criteria": [{"transition": "nowhere", "evidence_required": []}]},
        {"key": "done", "terminal": True, "exit_criteria": []},
    ]
    c["proof_requirements"] = []
    return c


def _mut_single_terminal_conversation():
    c = _base()
    c["event_loops"] = [{"entity": "lead", "triggers": ["inbound_reply", "follow_up_timer"], "terminal_states": ["closed"]}]
    return c


REPORT_WARN_CASES = [
    ("success_scoreability", _mut_prose_success),
    ("side_effect_class_coverage", _mut_missing_side_effect_class),
    ("event_loop_termination", _mut_freetext_loop_stop),
    ("tunable_consumer_binding", _mut_unread_tunable),
    ("win_signal_rail", _mut_no_win_terminal),
    ("evidence_namespace", _mut_orphan_evidence),
    ("stage_reachability", _mut_island_stage),
    ("distinct_terminals", _mut_single_terminal_conversation),
]


@pytest.mark.parametrize("dim,builder", REPORT_WARN_CASES, ids=[c[0] for c in REPORT_WARN_CASES])
def test_report_mode_warns(dim, builder):
    r = assess(builder())  # report mode
    assert _has(r["warnings"], dim), f"expected {dim} warning, got {r['warnings']}"
    # report mode never promotes a net-new dimension finding to a hard error
    assert not _has(r["errors"], dim), f"{dim} must be a warning (not error) in report mode: {r['errors']}"


# --------------------------------------------------------------------------- #
# False-positive guards: the well-formed counterpart must NOT warn
# --------------------------------------------------------------------------- #

def _ok_referenced_tunable():
    c = _mut_unread_tunable()
    c["sensors"] = [{"kind": "heartbeat", "knobs": {"stall_timeout": "some_business_threshold"}}]
    return c, "tunable_consumer_binding"


def _ok_win_terminal_present():
    c = _mut_no_win_terminal()
    c["event_loops"][0]["terminal_states"] = ["under_contract", "dead", "recycled"]
    return c, "win_signal_rail"


def _ok_non_conversational_loop():
    c = _mut_no_win_terminal()
    c["event_loops"][0]["triggers"] = ["daily_timer"]
    return c, "win_signal_rail"


def _ok_evidence_via_proof_requirements():
    c = _mut_orphan_evidence(); c["proof_requirements"] = ["ghost_artifact"]
    return c, "evidence_namespace"


def _ok_evidence_via_worker_envelope():
    c = _mut_orphan_evidence(); c["runtime"] = {"worker_envelopes": {"w": {"required_proof": ["ghost_artifact"]}}}
    return c, "evidence_namespace"


def _ok_reachable_linear_graph():
    c = _base()
    c["workflow"]["stages"] = [
        {"key": "s1", "exit_criteria": [{"transition": "done", "evidence_required": []}]},
        {"key": "done", "terminal": True, "exit_criteria": []},
    ]
    return c, "stage_reachability"


def _ok_distinct_terminals():
    c = _mut_single_terminal_conversation()
    c["event_loops"][0]["terminal_states"] = ["closed", "paused", "disqualified"]
    return c, "distinct_terminals"


FALSE_POSITIVE_CASES = [
    ("referenced_tunable", _ok_referenced_tunable),
    ("win_terminal_present", _ok_win_terminal_present),
    ("non_conversational_loop", _ok_non_conversational_loop),
    ("evidence_via_proof_requirements", _ok_evidence_via_proof_requirements),
    ("evidence_via_worker_envelope", _ok_evidence_via_worker_envelope),
    ("reachable_linear_graph", _ok_reachable_linear_graph),
    ("distinct_terminals", _ok_distinct_terminals),
]


@pytest.mark.parametrize("name,builder", FALSE_POSITIVE_CASES, ids=[c[0] for c in FALSE_POSITIVE_CASES])
def test_false_positive_guards(name, builder):
    contract, dim = builder()
    r = assess(contract)
    assert not _has(r["warnings"], dim), f"{name}: {dim} should NOT warn, got {r['warnings']}"


# --------------------------------------------------------------------------- #
# Enforce mode: each critical dimension is PROMOTED from warning to hard error
# --------------------------------------------------------------------------- #

ENFORCE_FLIP_CASES = [
    ("success_scoreability", _mut_prose_success),
    ("side_effect_class_coverage", _mut_missing_side_effect_class),
    ("event_loop_termination", _mut_freetext_loop_stop),
    ("win_signal_rail", _mut_no_win_terminal),
    ("evidence_namespace", _mut_orphan_evidence),
]


@pytest.mark.parametrize("dim,builder", ENFORCE_FLIP_CASES, ids=[c[0] for c in ENFORCE_FLIP_CASES])
def test_enforce_mode_promotes_critical_to_hard_error(dim, builder):
    contract = builder()
    report = assess(contract)  # report mode: warning, ok unaffected by this dim
    enforced = assess(copy.deepcopy(contract), enforce=True)
    assert _has(report["warnings"], dim), f"report mode should warn {dim}: {report['warnings']}"
    assert not enforced["ok"], f"enforce mode must fail closed on {dim}"
    assert _has(enforced["errors"], dim), f"enforce mode must surface {dim} as an error: {enforced['errors']}"


def test_structural_invariant_errors_are_hard_in_both_modes():
    """Invariant ERRORS (not net-new dimensions) are always hard, regardless of enforce."""
    bad = _base()  # action side_effect_class 'internal' undeclared in side_effect_policy
    assert not assess(bad)["ok"]
    assert not assess(bad, enforce=True)["ok"]
