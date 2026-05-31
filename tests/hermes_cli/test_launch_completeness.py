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


# --------------------------------------------------------------------------- #
# G4 PART 1: typed objective.success entries are first-class scoreable
# --------------------------------------------------------------------------- #

def _typed_success_entry(**overrides) -> dict:
    """A well-formed typed success entry (metric+comparator+target+data_source)."""
    entry = {"metric": "mrr", "comparator": ">=", "target": 12000, "data_source": "Stripe"}
    entry.update(overrides)
    return entry


def test_typed_success_wellformed_is_scoreable_no_warn():
    """A typed entry declaring all required keys is machine-gradeable -> the
    success_scoreability dimension must NOT warn (no prose regex needed)."""
    c = _base()
    c["objective"]["success"] = [_typed_success_entry()]
    r = assess(c)
    assert not _has(r["warnings"], "success_scoreability"), r["warnings"]
    assert r["dimensions"]["success_scoreability"]["findings"] == [], r["dimensions"]["success_scoreability"]


def test_typed_success_with_optional_baseline_is_scoreable():
    """The optional ``baseline`` does not affect scoreability."""
    c = _base()
    c["objective"]["success"] = [_typed_success_entry(baseline=4000)]
    r = assess(c)
    assert not _has(r["warnings"], "success_scoreability"), r["warnings"]


def test_typed_success_missing_keys_warns_and_names_them():
    """A typed entry missing required keys is NOT scoreable; the finding must name
    exactly which required keys are absent."""
    c = _base()
    c["objective"]["success"] = [{"metric": "mrr", "comparator": ">="}]  # no target/data_source
    r = assess(c)
    assert _has(r["warnings"], "success_scoreability"), r["warnings"]
    finding = " ".join(r["dimensions"]["success_scoreability"]["findings"])
    assert "target" in finding and "data_source" in finding, finding
    assert "missing required key" in finding, finding


def test_typed_success_missing_keys_promoted_in_enforce_mode():
    """Under enforce, a malformed typed entry becomes a hard error (fail closed)."""
    c = _base()
    c["objective"]["success"] = [{"metric": "mrr"}]  # only metric -> not scoreable
    report = assess(c)
    enforced = assess(copy.deepcopy(c), enforce=True)
    assert _has(report["warnings"], "success_scoreability"), report["warnings"]
    assert not enforced["ok"], "enforce must fail closed on a malformed typed success"
    assert _has(enforced["errors"], "success_scoreability"), enforced["errors"]


def test_mixed_prose_and_typed_success_each_judged_on_its_own():
    """A list mixing a measurable prose string and a well-formed typed entry is
    fully scoreable; swapping the prose for vacuous text warns on the prose only."""
    c = _base()
    c["objective"]["success"] = ["MRR to $12k by day 7", _typed_success_entry(metric="subs")]
    assert not _has(assess(c)["warnings"], "success_scoreability"), assess(c)["warnings"]
    c["objective"]["success"] = ["do a great job", _typed_success_entry(metric="subs")]
    assert _has(assess(c)["warnings"], "success_scoreability"), assess(c)["warnings"]


def test_string_success_behavior_unchanged():
    """Byte-compatibility guard: a measurable prose string is still scoreable and a
    vacuous prose string still warns -- the existing regex path is untouched."""
    measurable = _base()  # success already 'MRR to $12k by day 7'
    assert not _has(assess(measurable)["warnings"], "success_scoreability")
    vacuous = _base(); vacuous["objective"]["success"] = ["close some deals"]
    assert _has(assess(vacuous)["warnings"], "success_scoreability")


def test_typed_success_normalize_roundtrips_strings_byte_identically():
    """The kanban_db normalizer keeps STRING entries byte-identical (the golden
    contract guarantee) while preserving a well-formed typed dict entry."""
    from hermes_cli.kanban_db import normalize_objective_metadata

    strings = ["MRR to $12k by day 7", "close some deals"]
    out = normalize_objective_metadata({"statement": "x", "success": list(strings)})
    assert out["success"] == strings  # exact same shape, strings stay strings

    typed = normalize_objective_metadata({
        "statement": "x",
        "success": [{"metric": "mrr", "comparator": "at_least", "target": 12000, "data_source": "Stripe"}],
    })
    entry = typed["success"][0]
    assert isinstance(entry, dict)
    assert entry["comparator"] == ">="  # at_least alias folded to symbol
    assert entry["metric"] == "mrr" and entry["target"] == 12000 and entry["data_source"] == "Stripe"


def test_typed_success_malformed_dict_does_not_raise_and_is_kept():
    """A malformed typed dict (missing required keys) normalizes WITHOUT raising and
    is kept as a dict so the spec can flag it (it is not silently dropped)."""
    from hermes_cli.kanban_db import normalize_objective_metadata

    out = normalize_objective_metadata({"statement": "x", "success": [{"metric": "mrr"}]})
    assert out["success"] == [{"metric": "mrr"}]


# --------------------------------------------------------------------------- #
# FIX 6: typed-success non-scalar target laundering + comparator validation.
# A dict/list target must NOT be repr-stringified into a scoreable-looking
# string; nan/inf targets are dropped; an out-of-set comparator is not scoreable.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_target",
    [{"nested": 1}, [1, 2, 3], (1, 2)],
)
def test_typed_success_nonscalar_target_is_not_scoreable(bad_target):
    """A non-scalar target survives normalization WITH its non-scalar type (no
    repr-laundering) so the spec rejects it. FAILS before FIX 6 (str()-coercion
    turned it into a non-empty string that looked scoreable)."""
    from hermes_cli.kanban_db import normalize_objective_metadata
    from hermes_cli.launch_completeness import _is_scoreable

    out = normalize_objective_metadata(
        {"statement": "x", "success": [_typed_success_entry(target=bad_target)]}
    )
    entry = out["success"][0]
    # Normalizer must NOT have laundered it into a string.
    assert not isinstance(entry.get("target"), str), entry
    assert _is_scoreable(entry) is False

    c = _base()
    c["objective"]["success"] = [_typed_success_entry(target=bad_target)]
    assert _has(assess(c)["warnings"], "success_scoreability"), assess(c)["warnings"]
    assert not assess(copy.deepcopy(c), enforce=True)["ok"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_typed_success_nonfinite_target_is_not_scoreable(bad):
    """A non-finite numeric target (nan/inf) is dropped by the normalizer so the
    entry reads as missing its target -> not scoreable."""
    from hermes_cli.kanban_db import normalize_objective_metadata
    from hermes_cli.launch_completeness import _is_scoreable

    out = normalize_objective_metadata(
        {"statement": "x", "success": [_typed_success_entry(target=bad)]}
    )
    entry = out["success"][0]
    assert "target" not in entry, entry  # dropped
    assert _is_scoreable(entry) is False


@pytest.mark.parametrize("bad_comparator", ["roughly", "approx", "~=", "", "is"])
def test_typed_success_out_of_set_comparator_is_not_scoreable(bad_comparator):
    """A comparator the scoreboard cannot evaluate makes the typed entry NOT
    scoreable (the comparator key is treated as missing/invalid)."""
    from hermes_cli.launch_completeness import _is_scoreable

    entry = _typed_success_entry(comparator=bad_comparator)
    assert _is_scoreable(entry) is False

    c = _base()
    c["objective"]["success"] = [entry]
    assert _has(assess(c)["warnings"], "success_scoreability"), assess(c)["warnings"]


def test_typed_success_valid_comparators_stay_scoreable():
    """Every symbolic comparator and the prose aliases keep a typed entry scoreable
    (the fix must not over-reject)."""
    from hermes_cli.launch_completeness import _is_scoreable

    for cmp in (">=", "<=", ">", "<", "==", "!=", "at_least", "at_most"):
        assert _is_scoreable(_typed_success_entry(comparator=cmp)) is True, cmp


def test_typed_success_comparator_set_is_validated_against_kanban_db():
    """The spec's comparator set mirrors kanban_db._TYPED_SUCCESS_COMPARATORS
    (FIX 6 also addresses the 'defined but never validated' gap)."""
    import hermes_cli.kanban_db as kb
    import hermes_cli.launch_completeness as lc

    assert lc._TYPED_SUCCESS_COMPARATORS == kb._TYPED_SUCCESS_COMPARATORS


# --------------------------------------------------------------------------- #
# G4 PART 2: side_effect_class default-DENY (missing blocks; 'none' is valid)
# --------------------------------------------------------------------------- #

def _action_missing_class() -> dict:
    c = _base()
    c["workflow"]["stages"][0]["actions"].append({"key": "send_sms", "label": "text the seller"})
    return c


def _action_none_class() -> dict:
    c = _base()
    c["workflow"]["stages"][0]["actions"].append({"key": "read_db", "side_effect_class": "none"})
    return c


def test_missing_side_effect_class_is_a_finding():
    """An action with NO side_effect_class produces a side_effect_class_coverage finding."""
    r = assess(_action_missing_class())
    assert _has(r["warnings"], "side_effect_class_coverage"), r["warnings"]


def test_none_side_effect_class_is_valid_not_a_finding():
    """side_effect_class == 'none' is a VALID declared read-only class -> no finding."""
    r = assess(_action_none_class())
    findings = " ".join(r["dimensions"]["side_effect_class_coverage"]["findings"])
    assert "read_db" not in findings, findings


def test_missing_side_effect_class_is_default_deny_under_enforce():
    """Default-DENY: under enforce the missing-class finding becomes a hard error
    (the existing enforce path achieves default-deny -- no gate rewiring needed)."""
    c = _action_missing_class()
    enforced = assess(copy.deepcopy(c), enforce=True)
    assert not enforced["ok"], "missing side_effect_class must fail closed under enforce"
    assert _has(enforced["errors"], "side_effect_class_coverage"), enforced["errors"]


# --------------------------------------------------------------------------- #
# H5: answer_coverage report-mode rail (no-op without inputs; never enforced by default)
# --------------------------------------------------------------------------- #

def test_answer_coverage_is_in_dimensions_tuple():
    from hermes_cli.launch_completeness import DIMENSIONS

    assert "answer_coverage" in DIMENSIONS


def test_answer_coverage_noop_on_clean_contract():
    """The clean contract carries no embedded intake corpus -> answer_coverage is a
    strict no-op (present, empty, never warns) and does not regress ok."""
    r = assess(_clean())
    assert r["dimensions"]["answer_coverage"]["findings"] == [], r["dimensions"]["answer_coverage"]
    assert not _has(r["warnings"], "answer_coverage"), r["warnings"]
    assert r["ok"], r["errors"]


def test_answer_coverage_noop_under_enforce_on_clean_contract():
    """Even if an owner adds answer_coverage to the enforce set, a contract without
    coverage inputs must not be blocked (no inputs -> no finding -> no error)."""
    r = assess(_clean(), enforce=True)
    assert r["dimensions"]["answer_coverage"]["findings"] == []
    assert r["ok"], r["errors"]


@pytest.mark.parametrize("fixture", ["grow_app_one_week.contract.json", "land_wholesaling.contract.json"])
def test_answer_coverage_noop_on_goldens(fixture):
    """The stored golden contracts embed no intake answers -> answer_coverage is a
    no-op on them (the dimension cannot regress an existing golden)."""
    p = FIXTURES / fixture
    if not p.exists():
        pytest.skip(f"fixture {fixture} not present")
    r = assess(json.loads(p.read_text()))
    assert r["dimensions"]["answer_coverage"]["findings"] == [], fixture
    assert not _has(r["warnings"], "answer_coverage"), r["warnings"]


def test_answer_coverage_warns_on_weak_embedded_intake():
    """When a contract embeds a vacuous intake corpus, the coverage scorer fires a
    REPORT-MODE warning (never auto-promoted; only a finding)."""
    c = _clean()
    c["answers"] = {"q1": "do the thing", "q2": "make it good"}
    c["rough_goal"] = "grow my app"
    r = assess(c)
    assert _has(r["warnings"], "answer_coverage"), r["warnings"]
    # report mode: it is a warning, never a hard error, and ok stays True
    assert not _has(r["errors"], "answer_coverage"), r["errors"]
    assert r["ok"], r["errors"]


def test_answer_coverage_noop_when_inputs_malformed_does_not_raise():
    """Defensive: a non-dict answers payload must not raise and must stay a no-op."""
    c = _clean()
    c["answers"] = "not a dict"  # no usable corpus
    r = assess(c)  # must not raise
    assert r["dimensions"]["answer_coverage"]["findings"] == []


# --------------------------------------------------------------------------- #
# H6: advisory_intent_gap report-mode rail
#   objective.budget / definition_of_done are normalized+stored but NOT enforced.
#   This dimension makes that declared-but-advisory gap VISIBLE during soak. It is
#   a strict no-op unless one of those fields is declared (so it never regresses an
#   existing contract or either golden), report-mode only by default, and a
#   declared budget is exempt when a runtime budget sensor is present (the only
#   budget-consuming mechanism that exists).
# --------------------------------------------------------------------------- #

def test_advisory_intent_gap_is_in_dimensions_tuple():
    from hermes_cli.launch_completeness import DIMENSIONS

    assert "advisory_intent_gap" in DIMENSIONS


def test_advisory_intent_gap_noop_on_clean_contract():
    """The clean contract declares neither budget nor definition_of_done -> the
    dimension is a strict no-op (present, empty, never warns) and ok stays True."""
    r = assess(_clean())
    assert r["dimensions"]["advisory_intent_gap"]["findings"] == [], r["dimensions"]["advisory_intent_gap"]
    assert not _has(r["warnings"], "advisory_intent_gap"), r["warnings"]
    assert r["ok"], r["errors"]


def test_advisory_intent_gap_noop_under_enforce_on_clean_contract():
    """Even if an owner adds advisory_intent_gap to the enforce set, a contract that
    declares neither field must not be blocked (no declaration -> no finding)."""
    r = assess(_clean(), enforce=True)
    assert r["dimensions"]["advisory_intent_gap"]["findings"] == []
    assert r["ok"], r["errors"]


@pytest.mark.parametrize("fixture", ["grow_app_one_week.contract.json", "land_wholesaling.contract.json"])
def test_advisory_intent_gap_noop_on_goldens(fixture):
    """The goldens declare a runtime budget *sensor* but no objective.budget and no
    definition_of_done -> the dimension is a strict no-op on them (cannot regress a
    stored golden)."""
    p = FIXTURES / fixture
    if not p.exists():
        pytest.skip(f"fixture {fixture} not present")
    r = assess(json.loads(p.read_text()))
    assert r["dimensions"]["advisory_intent_gap"]["findings"] == [], fixture
    assert not _has(r["warnings"], "advisory_intent_gap"), r["warnings"]


def test_advisory_intent_gap_warns_on_declared_budget_without_sensor():
    """A declared budget ceiling with NO runtime budget sensor is advisory -> a
    report-mode warning fires (never a hard error; ok stays True)."""
    c = _clean()
    c["objective"]["budget"] = {"ceiling": 5000, "currency": "USD", "period": "total"}
    r = assess(c)
    assert _has(r["warnings"], "advisory_intent_gap"), r["warnings"]
    assert _has(r["dimensions"]["advisory_intent_gap"]["findings"], "ADVISORY")
    assert not _has(r["errors"], "advisory_intent_gap"), r["errors"]
    assert r["ok"], r["errors"]


def test_advisory_intent_gap_warns_on_bare_numeric_budget():
    """A bare-number budget (a total-spend ceiling) is also a declaration -> warns."""
    c = _clean()
    c["objective"]["budget"] = 5000
    r = assess(c)
    assert _has(r["warnings"], "advisory_intent_gap"), r["warnings"]


def test_advisory_intent_gap_exempt_when_budget_sensor_present():
    """False-positive guard: a declared budget is NOT flagged when the contract also
    declares a runtime 'budget'-kind sensor (the sensor IS the consuming mechanism)."""
    c = _clean()
    c["objective"]["budget"] = {"ceiling": 5000, "currency": "USD", "period": "total"}
    c["sensors"] = [{"kind": "budget", "window_hours": 24, "ceiling": 5000}]
    r = assess(c)
    # the budget half must not fire; only a declared DoD could, and none is set here
    budget_find = " ".join(
        f for f in r["dimensions"]["advisory_intent_gap"]["findings"] if "budget" in f
    )
    assert budget_find == "", budget_find
    assert not _has(r["warnings"], "advisory_intent_gap"), r["warnings"]


def test_advisory_intent_gap_exempt_when_budget_sensor_under_runtime():
    """The sensor exemption also reads a runtime-nested sensors block."""
    c = _clean()
    c["objective"]["budget"] = 5000
    c["runtime"] = {"sensors": [{"kind": "budget", "ceiling": 5000}]}
    r = assess(c)
    assert not _has(r["warnings"], "advisory_intent_gap"), r["warnings"]


def test_advisory_intent_gap_warns_on_declared_definition_of_done():
    """A declared definition_of_done has no grading seam -> always advisory; warns."""
    c = _clean()
    c["objective"]["definition_of_done"] = "all leads contacted and logged"
    r = assess(c)
    assert _has(r["warnings"], "advisory_intent_gap"), r["warnings"]
    assert _has(r["dimensions"]["advisory_intent_gap"]["findings"], "definition_of_done")
    assert r["ok"], r["errors"]


def test_advisory_intent_gap_warns_on_list_definition_of_done():
    """A list-shaped definition_of_done is also a declaration -> warns."""
    c = _clean()
    c["objective"]["definition_of_done"] = ["leads contacted", "deals logged"]
    r = assess(c)
    assert _has(r["warnings"], "advisory_intent_gap"), r["warnings"]


def test_advisory_intent_gap_noop_on_empty_or_malformed_budget():
    """Defensive: an empty / non-positive / malformed budget is NOT a declaration
    (nothing to enforce) and must not raise or warn."""
    for bad in ({}, {"ceiling": 0}, {"ceiling": "abc"}, True, {"ceiling": None}):
        c = _clean()
        c["objective"]["budget"] = bad
        r = assess(c)  # must not raise
        budget_find = " ".join(
            f for f in r["dimensions"]["advisory_intent_gap"]["findings"] if "budget" in f
        )
        assert budget_find == "", (bad, budget_find)


def test_advisory_intent_gap_promoted_in_enforce_mode_when_opted_in():
    """If an owner adds advisory_intent_gap to the enforce set, a declared-but-advisory
    budget becomes a hard error (fail closed) -- proving it is wired through the same
    enforce machinery as the other net-new dimensions, while staying off by default."""
    c = _clean()
    c["objective"]["budget"] = {"ceiling": 5000, "currency": "USD", "period": "total"}
    report = assess(c)
    enforced = assess(copy.deepcopy(c), enforce=True)
    assert _has(report["warnings"], "advisory_intent_gap"), report["warnings"]
    assert not enforced["ok"], "enforce mode must fail closed on a declared-but-advisory budget"
    assert _has(enforced["errors"], "advisory_intent_gap"), enforced["errors"]
