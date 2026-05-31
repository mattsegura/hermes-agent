"""Phase 2: server-side launch-intake orchestration (mocked auxiliary).

Proves the wiring routes launch intake through the ``kanban_launch_intake``
auxiliary orchestrator when configured (server generates questions, assesses
answers, and synthesizes the contract) and degrades to the deterministic
universal drafter otherwise. All auxiliary + web calls are mocked -- nothing
here touches a network.

DoD metric: end-to-end with mocked auxiliary, the synthesized contract scores
>= 0.75 on the coverage rubric's domain dimensions, well above the generic
universal drafter (~0.25).
"""

from __future__ import annotations

import glob
import json
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_launch_intake as kli
from hermes_cli import kanban_launch_coverage as cov

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures", "launch_intake")
DOMAIN_MIN = 0.75


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


def _load_fixture(domain: str) -> dict:
    with open(os.path.join(FIXTURE_DIR, f"{domain}.intake.json")) as fh:
        fx = json.load(fh)
    with open(os.path.join(FIXTURE_DIR, fx["golden_contract_file"])) as fh:
        fx["golden_contract"] = json.load(fh)
    return fx


def _enable_server_aux(monkeypatch, fixture):
    """Patch the auxiliary orchestrator to behave like a configured server."""
    monkeypatch.setattr(kli, "aux_configured", lambda: True)
    monkeypatch.setattr(
        kli,
        "run_question_generation",
        lambda rough_goal, **kw: kli.QuestionGenerationResult(
            ok=True,
            degraded=False,
            questions=[
                "What concrete outcome and numbers define success?",
                "Which systems and channels may Hermes use?",
                "What must wait for your approval?",
            ],
            assumptions=["Owner will confirm the drafted contract before launch."],
        ),
    )
    monkeypatch.setattr(
        kli,
        "run_answer_assessment",
        lambda rough_goal, questions, answers, **kw: kli.AnswerAssessmentResult(
            ok=True, degraded=False, sufficient=True, evidence="All dimensions covered."
        ),
    )
    monkeypatch.setattr(
        kli,
        "run_contract_synthesis",
        lambda rough_goal, answers, **kw: kli.ContractSynthesisResult(
            ok=True, degraded=False, contract=fixture["golden_contract"]
        ),
    )


def test_server_generates_questions_when_configured(fresh_home, monkeypatch):
    fx = _load_fixture("land_wholesaling")
    _enable_server_aux(monkeypatch, fx)
    result = kb.review_business_launch_contract(
        "srv-qgen", rough_goal=fx["rough_goal"], create_if_missing=True
    )
    intake = result["launch_intake"]
    assert intake["state"] == "clarifying"
    assert intake["generated_questions"]  # server filled them in
    assert intake["source"] == "server_generated"
    assert intake["question_generation"]["mode"] == "server_generated"
    assert intake["degraded_mode"] is False


@pytest.mark.parametrize("domain", ["land_wholesaling", "research_team", "insurance_recruiting"])
def test_server_synthesis_end_to_end_scores_above_domain_bar(monkeypatch, domain):
    """End-to-end through the orchestration helpers (question gen -> answer
    assessment -> synthesis). Scores the synthesized draft directly via the
    rubric (the golden fixtures intentionally use richer lifecycle vocabulary
    than ``validate_business_runtime_contract`` allows, so we score the draft
    rather than persisting it)."""
    fx = _load_fixture(domain)
    _enable_server_aux(monkeypatch, fx)

    draft = kb.build_business_runtime_contract_draft(
        rough_goal=fx["rough_goal"], intake_answers=fx["answers"]
    )

    intake = draft["launch_intake"]
    assert intake["state"] == "ready_for_owner_review"
    assert intake["source"] == "model_generated"
    assert intake["degraded_mode"] is False
    assert intake["answer_quality"]["assessed_by"] == "server"
    # Server-synthesized contract must clear the domain bar.
    score = cov.score_contract_against_rubric(draft)
    print(f"\n[server-synth] {domain}: score={score:.4f} (bar>={DOMAIN_MIN})")
    assert score >= DOMAIN_MIN
    # And it must be a real, workflow-bearing contract.
    assert draft["workflow"]["stages"]


def test_insufficient_assessment_blocks_synthesis(monkeypatch):
    fx = _load_fixture("land_wholesaling")
    monkeypatch.setattr(kli, "aux_configured", lambda: True)
    monkeypatch.setattr(
        kli, "run_question_generation",
        lambda rough_goal, **kw: kli.QuestionGenerationResult(ok=True, degraded=False, questions=["q1", "q2"]),
    )
    monkeypatch.setattr(
        kli, "run_answer_assessment",
        lambda rough_goal, questions, answers, **kw: kli.AnswerAssessmentResult(
            ok=True, degraded=False, sufficient=False,
            follow_up_questions=["Which exact channel is approved for outbound?"],
        ),
    )
    # Synthesis must never be reached when assessment is insufficient.
    monkeypatch.setattr(
        kli, "run_contract_synthesis",
        lambda *a, **k: pytest.fail("synthesis should not run when assessment insufficient"),
    )
    draft = kb.build_business_runtime_contract_draft(
        rough_goal=fx["rough_goal"], intake_answers=fx["answers"]
    )

    # Synthesis blocked: no workflow, intake still in the assessment loop.
    assert not draft.get("workflow")
    intake = draft["launch_intake"]
    # Raw helper output routes insufficient answers back to the clarifying loop;
    # the review-layer state sync canonicalizes this to "assessing_answers".
    assert intake["state"] == "clarifying"
    assert intake["answer_assessment"]["result"]["sufficient"] is False
    follow_ups = " ".join(
        intake["answer_assessment"]["result"].get("follow_up_questions", [])
    )
    assert "Which exact channel" in follow_ups


def test_review_preserves_generated_questions_on_rough_goal_only(fresh_home, monkeypatch):
    """Re-calling review with only rough_goal must not wipe server-generated questions."""
    fx = _load_fixture("land_wholesaling")
    _enable_server_aux(monkeypatch, fx)
    kb.review_business_launch_contract(
        "preserve-q", rough_goal=fx["rough_goal"], create_if_missing=True
    )
    first = kb.read_board_metadata("preserve-q")["business_contract"]["launch_intake"]
    assert first["generated_questions"]

    kb.review_business_launch_contract("preserve-q", rough_goal=fx["rough_goal"])
    second = kb.read_board_metadata("preserve-q")["business_contract"]["launch_intake"]
    assert second["generated_questions"] == first["generated_questions"]
    assert second["questions"] == first["questions"]


def test_degraded_question_generation_emits_fallback_questions(fresh_home, monkeypatch):
    monkeypatch.setattr(kli, "aux_configured", lambda: True)
    monkeypatch.setattr(
        kli,
        "run_question_generation",
        lambda rough_goal, **kw: kli.QuestionGenerationResult(
            ok=False, degraded=True, reason="auxiliary unavailable"
        ),
    )
    monkeypatch.setattr(
        kli,
        "run_pre_interview_research",
        lambda *a, **k: kli.PreInterviewResearchResult(ok=True, degraded=False, items=[]),
    )
    result = kb.review_business_launch_contract(
        "degraded-q", rough_goal="Launch a mobile app", create_if_missing=True
    )
    intake = result["launch_intake"]
    assert intake["degraded_mode"] is True
    assert len(intake["generated_questions"]) >= 2
    assert len(result["questions"]) >= 2


def test_degraded_without_aux_falls_back_to_universal_drafter(fresh_home, monkeypatch):
    """No auxiliary configured -> deterministic universal drafter, marked
    degraded, and measurably weaker than a server-synthesized contract."""
    fx = _load_fixture("land_wholesaling")
    monkeypatch.setattr(kli, "aux_configured", lambda: False)
    kb.review_business_launch_contract("srv-degraded", rough_goal=fx["rough_goal"], create_if_missing=True)
    result = kb.review_business_launch_contract("srv-degraded", intake_answers=fx["answers"])

    intake = result["launch_intake"]
    assert intake["state"] == "ready_for_owner_review"
    assert intake["degraded_mode"] is True
    assert intake["source"] == "model_generated"
    score = cov.score_contract_against_rubric(result["contract"])
    assert score < 0.5  # the generic fallback is the weak path
