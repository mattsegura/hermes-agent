"""Tests for launch intent gate (Layer 1)."""

from __future__ import annotations

import pytest

from hermes_cli.kanban_launch_intent import classify_launch_intent, normalize_intent


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


def test_maybe_board_includes_clarifying_question() -> None:
    result = classify_launch_intent("I want to build an app")
    assert result.intent == "maybe_board"
    assert result.clarifying_question
    assert "?" in result.clarifying_question or "or" in result.clarifying_question.lower()


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
