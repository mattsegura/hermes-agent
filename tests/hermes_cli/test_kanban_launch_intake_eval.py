"""Phase 0 eval harness for the launch-intake -> contract pipeline.

Scores the deterministic coverage rubric against:

* hand-authored *golden* contracts (land wholesaling, research desk, insurance
  recruiting) -- these must score >= 0.85, and
* the *generic universal drafter* output built from the same intake answers --
  this must score < 0.5.

The gap between the two numbers is the whole point of the upgrade: the old
"5 fields / 300 chars" honor-system heuristic let a boilerplate universal
contract sail through, while a real, concrete contract and a weak one looked
the same to the gate. The rubric makes the difference measurable.

Run just this file:

    .venv/bin/python -m pytest tests/hermes_cli/test_kanban_launch_intake_eval.py -v -s
"""

from __future__ import annotations

import glob
import json
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_launch_coverage as cov
from hermes_cli import kanban_launch_invariants as inv
from hermes_cli import kanban_launch_simulation as sim

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures", "launch_intake")

GOLD_MIN = 0.85
UNIVERSAL_MAX = 0.5


def _load_fixtures() -> list[dict]:
    fixtures = []
    for path in sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.intake.json"))):
        with open(path) as fh:
            fx = json.load(fh)
        contract_path = os.path.join(FIXTURE_DIR, fx["golden_contract_file"])
        with open(contract_path) as fh:
            fx["golden_contract"] = json.load(fh)
        fixtures.append(fx)
    return fixtures


FIXTURES = _load_fixtures()
FIXTURE_IDS = [fx["domain"] for fx in FIXTURES]


def _universal_contract(fixture: dict) -> dict:
    """Build the generic universal-drafter contract from a fixture's intake."""
    draft = kb.build_business_runtime_contract_draft(
        None, rough_goal=fixture["rough_goal"]
    )
    draft = kb._merge_launch_intake_answers(draft, intake_answers=fixture["answers"])
    return kb._build_universal_contract_from_launch_intake(draft)


def test_fixtures_present():
    assert FIXTURES, "expected launch-intake fixtures to be discovered"
    # All four marquee golden domains must be present and exercised by the
    # parametrized invariant + simulation + coverage ratchet below.
    assert "land_wholesaling" in FIXTURE_IDS
    assert "research_team" in FIXTURE_IDS
    assert "insurance_recruiting" in FIXTURE_IDS
    assert "grow_app_one_week" in FIXTURE_IDS


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_golden_contract_scores_at_or_above_bar(fixture):
    score = cov.score_contract_against_rubric(fixture["golden_contract"], fixture)
    print(f"\n[golden] {fixture['domain']}: score={score:.4f} (bar>={GOLD_MIN})")
    assert score >= GOLD_MIN, (
        f"golden contract for {fixture['domain']} scored {score} < {GOLD_MIN}"
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_universal_drafter_scores_below_bar(fixture):
    universal = _universal_contract(fixture)
    score = cov.score_contract_against_rubric(universal, fixture)
    print(f"\n[universal] {fixture['domain']}: score={score:.4f} (bar<{UNIVERSAL_MAX})")
    assert score < UNIVERSAL_MAX, (
        f"universal contract for {fixture['domain']} scored {score} >= {UNIVERSAL_MAX}; "
        "the generic drafter should be measurably weaker than a real contract"
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_rich_intake_answers_pass_coverage(fixture):
    report = cov.evaluate_launch_intake_coverage(
        fixture["answers"], rough_goal=fixture["rough_goal"]
    )
    assert report.passed, (
        f"{fixture['domain']} rich answers should pass coverage; gaps={report.gaps}"
    )


def test_weak_intake_answers_fail_coverage():
    report = cov.evaluate_launch_intake_coverage(
        {"raw": "I just want more partners, do whatever."},
        rough_goal="Launch a partner workflow",
    )
    assert not report.passed
    assert report.gaps  # at least one dimension is not satisfied


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_golden_contract_satisfies_structural_invariants(fixture):
    """Every golden fixture must satisfy the hard structural grammar -- the same
    gate the synthesis self-repair loop enforces -- so the corpus stays a valid
    target for the repair loop, not just the coverage rubric."""
    report = inv.check_contract_invariants(fixture["golden_contract"])
    assert report.ok, (
        f"golden contract for {fixture['domain']} violates invariants: {report.errors}"
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_golden_contract_passes_behavioural_simulation(fixture):
    """Behavioural pass over every domain fixture. A contract that declares a
    conversational control plane (an inbound event loop / watch stage) must drive
    all default scenarios -- happy path, disqualifier, slow burn, ghost -- to a
    terminal outcome (no zombie, no infinite loop). Contracts with no
    conversational loop (e.g. a metric/approval-driven sprint) legitimately
    expose no model; those are recorded but not failed."""
    contract = fixture["golden_contract"]
    model = sim.build_conversation_model(contract)
    results = sim.run_default_simulation(contract)
    if model is None:
        assert results == {}, (
            f"{fixture['domain']} has no conversational model but produced results"
        )
        return
    assert set(results) == {"happy_path", "disqualifier", "slow_burn", "ghost"}
    unresolved = [name for name, r in results.items() if not r.resolved]
    assert not unresolved, (
        f"{fixture['domain']} left scenarios unresolved: {unresolved}"
    )


def test_phase0_definition_of_done_scoreboard():
    """Phase 0 DoD: universal output < 0.5, land-wholesaling gold >= 0.85."""
    land = next(fx for fx in FIXTURES if fx["domain"] == "land_wholesaling")
    gold_score = cov.score_contract_against_rubric(land["golden_contract"], land)
    universal_score = cov.score_contract_against_rubric(_universal_contract(land), land)
    print(
        "\n=== Phase 0 scoreboard (land_wholesaling) ===\n"
        f"  universal drafter : {universal_score:.4f}  (target < {UNIVERSAL_MAX})\n"
        f"  gold contract     : {gold_score:.4f}  (target >= {GOLD_MIN})"
    )
    assert universal_score < UNIVERSAL_MAX
    assert gold_score >= GOLD_MIN
