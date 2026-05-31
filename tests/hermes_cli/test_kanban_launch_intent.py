"""Tests for launch intent gate (Layer 1)."""

from __future__ import annotations

import pytest

from hermes_cli.kanban_launch_intent import (
    _template_clarifying_question,
    classify_launch_intent,
    is_binary_clarifying_question,
    normalize_intent,
)


@pytest.mark.parametrize(
    "goal,expected",
    [
        ("What is a land contract?", "no_board_needed"),
        ("Explain how skip tracing works", "no_board_needed"),
        ("Help me grow my TikTok page", "maybe_board"),
        ("I want to build an app", "maybe_board"),
        (
            "Cold text vacant land owners in Polk County every week and negotiate deals",
            "board_required",
        ),
        (
            "Set up an ongoing pipeline to source qualify and close wholesale land deals",
            "board_required",
        ),
    ],
)
def test_classify_launch_intent(goal: str, expected: str) -> None:
    result = classify_launch_intent(goal)
    assert result.intent == expected


@pytest.mark.parametrize(
    "goal,expected_fragment",
    [
        ("Help me grow my TikTok page", "content"),
        ("I want to build an app", "building"),
        ("Help me get started with land investing in Texas", "land"),
    ],
)
def test_maybe_board_clarifying_question_is_open_and_domain_aware(
    goal: str,
    expected_fragment: str,
) -> None:
    result = classify_launch_intent(goal)
    assert result.intent == "maybe_board"
    question = result.clarifying_question or ""
    assert question
    assert "?" in question
    assert not is_binary_clarifying_question(question)
    assert expected_fragment.lower() in question.lower()


def test_maybe_board_includes_clarifying_question_style() -> None:
    result = classify_launch_intent("I want to build an app")
    data = result.as_dict()
    assert data["intent"] == "maybe_board"
    assert data["clarifying_question_style"] == "open_contextual"
    assert not is_binary_clarifying_question(data["clarifying_question"])


@pytest.mark.parametrize(
    "question",
    [
        "Is this ongoing autonomous marketing work, or a one-time question?",
        "Should I treat this as ongoing tracked work, or answer it as a one-time question?",
        "Are you asking for strategy advice you can execute yourself, or managed outreach?",
    ],
)
def test_is_binary_clarifying_question_detects_dichotomies(question: str) -> None:
    assert is_binary_clarifying_question(question)


@pytest.mark.parametrize(
    "goal,expected_fragment",
    [
        ("Help me grow my TikTok page", "content"),
        ("I want to build an iOS app", "building"),
        ("Need wholesale land leads in Texas", "land"),
    ],
)
def test_template_clarifying_question_domain_context(
    goal: str,
    expected_fragment: str,
) -> None:
    question = _template_clarifying_question(goal)
    assert expected_fragment.lower() in question.lower()
    assert not is_binary_clarifying_question(question)


def test_strong_board_match_biases_board_required() -> None:
    result = classify_launch_intent(
        "negotiate land deals",
        top_match_score=4,
        candidate_count=2,
    )
    assert result.intent == "board_required"


def test_normalize_intent() -> None:
    assert normalize_intent("board_required") == "board_required"
    assert normalize_intent("invalid") is None


def test_intent_result_serializes() -> None:
    result = classify_launch_intent("help grow my page")
    data = result.as_dict()
    assert data["intent"] == "maybe_board"
    assert "suggested_next_action" in data
    assert data.get("clarifying_question_style") == "open_contextual"
