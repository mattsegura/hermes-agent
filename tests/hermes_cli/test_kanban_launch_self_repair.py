"""P4b synthesis self-repair loop tests.

Before degrading to the deterministic universal drafter, the synthesis path now
RETRIES synthesis up to ``LAUNCH_INTAKE_SYNTH_MAX_ATTEMPTS`` times, feeding the
SPECIFIC structural-invariant error strings from the prior attempt back to the
synthesizer so it can fix exactly those defects. These tests prove:

1. A synthesizer that fails invariants on attempt 1 and passes on attempt 2
   yields an APPLIED model-generated contract (not a degrade), and the repair
   feedback handed to attempt 2 carried attempt 1's invariant errors.
2. A synthesizer that always fails degrades to the universal drafter after
   EXACTLY N attempts.
3. With the aux model unconfigured the path degrades immediately, without ever
   calling synthesis (no loop).

All auxiliary calls are mocked -- nothing here touches a network.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_launch_intake as kli
from hermes_cli import kanban_launch_invariants as inv

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures", "launch_intake")


def _load_fixture(domain: str) -> dict:
    with open(os.path.join(FIXTURE_DIR, f"{domain}.intake.json")) as fh:
        fx = json.load(fh)
    with open(os.path.join(FIXTURE_DIR, fx["golden_contract_file"])) as fh:
        fx["golden_contract"] = json.load(fh)
    return fx


def _broken_contract(clean: dict) -> dict:
    """A copy of a valid contract that violates one hard invariant (an unbounded
    tunable knob), so attempt 1 is rejected by the grammar checker."""
    broken = copy.deepcopy(clean)
    broken.setdefault("tunables", {})["unbounded_knob"] = {"default": 5}
    return broken


def _patch_intake_prelude(monkeypatch):
    """Make question-gen + answer-assessment pass so synthesis is reached."""
    monkeypatch.setattr(kli, "aux_configured", lambda: True)
    monkeypatch.setattr(
        kli, "run_question_generation",
        lambda rough_goal, **kw: kli.QuestionGenerationResult(
            ok=True, degraded=False, questions=["q1", "q2", "q3"],
        ),
    )
    monkeypatch.setattr(
        kli, "run_answer_assessment",
        lambda rough_goal, questions, answers, **kw: kli.AnswerAssessmentResult(
            ok=True, degraded=False, sufficient=True, evidence="covered",
        ),
    )


# ---------------------------------------------------------------------------
# 1. Fail attempt 1, pass attempt 2 -> applied model contract; feedback carried
#    the attempt-1 invariant errors.
# ---------------------------------------------------------------------------


def test_repair_loop_fixes_on_second_attempt_and_applies(monkeypatch):
    fx = _load_fixture("land_wholesaling")
    _patch_intake_prelude(monkeypatch)

    clean = fx["golden_contract"]
    broken = _broken_contract(clean)
    expected_errors = inv.check_contract_invariants(broken).errors
    assert expected_errors  # sanity: the broken contract really fails

    calls: list[dict] = []

    def fake_synth(rough_goal, answers, *, repair_feedback=None, **kw):
        calls.append({"repair_feedback": repair_feedback})
        # Attempt 1 -> broken (fails invariants); attempt 2+ -> clean (passes).
        contract = broken if len(calls) == 1 else clean
        return kli.ContractSynthesisResult(ok=True, degraded=False, contract=contract)

    monkeypatch.setattr(kli, "run_contract_synthesis", fake_synth)

    draft = kb.build_business_runtime_contract_draft(
        rough_goal=fx["rough_goal"], intake_answers=fx["answers"]
    )

    # Exactly two attempts: a cold draft + one repair pass.
    assert len(calls) == 2
    # Attempt 1 had no feedback; attempt 2 was handed attempt-1's invariant errors.
    assert calls[0]["repair_feedback"] in (None, [])
    assert calls[1]["repair_feedback"] == expected_errors

    # The model-generated (repaired) contract was APPLIED, not degraded.
    intake = draft["launch_intake"]
    assert intake["state"] == "ready_for_owner_review"
    assert intake["source"] == "model_generated"
    assert intake["degraded_mode"] is False
    assert draft["workflow"]["stages"]


# ---------------------------------------------------------------------------
# 2. Always fails -> degrade to universal drafter after EXACTLY N attempts.
# ---------------------------------------------------------------------------


def test_repair_loop_degrades_after_exactly_n_attempts(monkeypatch):
    fx = _load_fixture("land_wholesaling")
    _patch_intake_prelude(monkeypatch)

    broken = _broken_contract(fx["golden_contract"])
    calls: list[dict] = []

    def always_broken(rough_goal, answers, *, repair_feedback=None, **kw):
        calls.append({"repair_feedback": repair_feedback})
        return kli.ContractSynthesisResult(ok=True, degraded=False, contract=broken)

    monkeypatch.setattr(kli, "run_contract_synthesis", always_broken)

    draft = kb.build_business_runtime_contract_draft(
        rough_goal=fx["rough_goal"], intake_answers=fx["answers"]
    )

    # Bounded: synthesis is called exactly N times, then it degrades.
    assert len(calls) == kb.LAUNCH_INTAKE_SYNTH_MAX_ATTEMPTS
    # Every attempt after the first received repair feedback.
    assert calls[0]["repair_feedback"] in (None, [])
    for call in calls[1:]:
        assert call["repair_feedback"]  # non-empty error feedback

    # Degraded to the deterministic universal drafter.
    intake = draft["launch_intake"]
    assert intake["degraded_mode"] is True
    assert intake["source"] == "model_generated"
    assert draft.get("workflow")  # the universal drafter still yields a workflow


# ---------------------------------------------------------------------------
# 3. Aux unconfigured -> degrade immediately, synthesis never called.
# ---------------------------------------------------------------------------


def test_aux_unconfigured_degrades_without_looping(monkeypatch):
    fx = _load_fixture("land_wholesaling")
    monkeypatch.setattr(kli, "aux_configured", lambda: False)

    def must_not_run(*a, **k):
        pytest.fail("run_contract_synthesis must not run when aux is unconfigured")

    monkeypatch.setattr(kli, "run_contract_synthesis", must_not_run)

    draft = kb.build_business_runtime_contract_draft(
        rough_goal=fx["rough_goal"], intake_answers=fx["answers"]
    )

    intake = draft["launch_intake"]
    assert intake["degraded_mode"] is True
    assert intake["source"] == "model_generated"
    assert draft.get("workflow")


# ---------------------------------------------------------------------------
# 4. A degraded synthesis result (aux became unavailable mid-flight) does not
#    loop -- it breaks immediately to the universal drafter.
# ---------------------------------------------------------------------------


def test_degraded_synth_result_breaks_immediately(monkeypatch):
    fx = _load_fixture("land_wholesaling")
    _patch_intake_prelude(monkeypatch)

    calls: list[int] = []

    def degraded_synth(rough_goal, answers, *, repair_feedback=None, **kw):
        calls.append(1)
        return kli.ContractSynthesisResult(
            ok=False, degraded=True, reason="auxiliary unavailable"
        )

    monkeypatch.setattr(kli, "run_contract_synthesis", degraded_synth)

    draft = kb.build_business_runtime_contract_draft(
        rough_goal=fx["rough_goal"], intake_answers=fx["answers"]
    )

    # Only one call: a degraded result aborts the loop, no repair retries.
    assert len(calls) == 1
    assert draft["launch_intake"]["degraded_mode"] is True
    assert draft.get("workflow")
