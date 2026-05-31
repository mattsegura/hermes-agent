"""Tests for intake loop prevention and partial escalation."""

from __future__ import annotations

import pytest


def test_repeated_intake_answers_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()
    from hermes_cli import kanban_db as kb

    board = "loop-test"
    kb.create_board(board, name="Loop Test")
    first = kb.review_business_launch_contract(
        board,
        rough_goal="Run a weekly land wholesaling outreach pipeline",
        create_if_missing=True,
    )
    assert first.get("launch_intake")
    answers = {
        "outcome_signals": "Close 2 deals per month at $8k assignment fee each.",
        "subject_scope": "Vacant land owners in Polk County FL from PropStream list.",
        "allowed_context": "SMS via Twilio, PropStream, county GIS — no cold calling.",
        "workflow_path": "Import leads -> skip trace -> text -> negotiate -> contract.",
        "workflow_stages": "1 source 2 qualify 3 outreach 4 negotiate 5 close.",
        "integration_points": "PropStream CSV, Twilio SMS, DocuSign for contracts.",
        "approval_boundaries": "Owner approves offers over $5k and all contracts.",
        "proof_and_stops": "Log every SMS, stop after 5 no-replies, escalate dead leads.",
    }
    kb.review_business_launch_contract(
        board,
        intake_answers=answers,
        create_if_missing=False,
    )
    with pytest.raises(ValueError, match="duplicate a prior submission"):
        kb.review_business_launch_contract(
            board,
            intake_answers=answers,
            create_if_missing=False,
        )


def test_partial_escalation_after_max_rounds(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_LAUNCH_INTAKE_MAX_CLARIFICATION_ROUNDS", "1")
    (tmp_path / ".hermes").mkdir()
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_launch_intake as kli
    from hermes_cli.kanban_launch_intake import AnswerAssessmentResult

    monkeypatch.setattr(kb, "LAUNCH_INTAKE_MAX_CLARIFICATION_ROUNDS", 1)
    board = "partial-esc"
    kb.create_board(board, name="Partial Esc")
    kb.review_business_launch_contract(
        board,
        rough_goal="Grow mobile app installs via TikTok",
        create_if_missing=True,
    )
    weak = {"note": "grow the app somehow"}
    monkeypatch.setattr(
        kli,
        "run_answer_assessment",
        lambda *a, **k: AnswerAssessmentResult(
            ok=True,
            degraded=False,
            sufficient=False,
            follow_up_questions=["What is your target CPI and monthly budget?"],
            evidence="too vague",
        ),
    )
    monkeypatch.setattr(kb, "_launch_intake_aux_enabled", lambda: True)
    result = kb.review_business_launch_contract(
        board,
        intake_answers=weak,
        create_if_missing=False,
    )
    intake = result.get("launch_intake") or {}
    assert intake.get("partial_escalation") is True
    quality = intake.get("answer_quality") or {}
    assert quality.get("status") == "partial_escalation"
    assert quality.get("sufficient") is True


def test_workflow_stages_coverage_dimension():
    from hermes_cli.kanban_launch_coverage import (
        DIMENSIONS,
        evaluate_launch_intake_coverage,
    )

    assert "workflow_stages" in DIMENSIONS
    assert "integration_points" in DIMENSIONS
    rich = {
        "outcome_signals": "10 signed contracts per quarter at $12k average assignment.",
        "subject_scope": "Off-market vacant land sellers in Texas counties.",
        "allowed_context": "SMS and email only; no phone calls without consent.",
        "workflow_path": "Lead import -> skip trace -> initial SMS -> follow-up -> offer.",
        "workflow_stages": (
            "1 source/import 2 skip-trace qualify 3 first SMS 4 negotiate price "
            "4b counter-offer loop 5 contract 6 close or disqualify."
        ),
        "integration_points": "PropStream export, Twilio SMS, GitHub for scripts, DocuSign.",
        "approval_boundaries": "Owner approves any offer above $15k and all contracts.",
        "proof_and_stops": "CRM log per touch; stop after 7 days no reply; weekly summary.",
    }
    report = evaluate_launch_intake_coverage(rich, rough_goal="land wholesaling pipeline")
    assert report.dimension("workflow_stages") is not None
    assert report.dimension("integration_points") is not None
