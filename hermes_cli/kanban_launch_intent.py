"""Launch intent gate (Layer 1) for the reasoning+router profile.

Classifies an owner message before ``kanban_business_launch_review`` so the
router agent can answer lightweight requests inline, ask one natural clarifying
question when ambiguous, or proceed to full launch intake when durable agentic
structure is clearly needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

LaunchIntent = Literal["no_board_needed", "maybe_board", "board_required"]

_INTENT_VALUES: frozenset[str] = frozenset(
    {"no_board_needed", "maybe_board", "board_required"}
)

# Lightweight: questions, lookups, explanations — no durable board.
_LIGHTWEIGHT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:what|why|how|when|where|who|which|is|are|explain|describe|"
        r"can you explain|could you explain|tell me about|define|compare|"
        r"difference between|summarize|summary of|look up|lookup)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\?\s*$"),
    re.compile(
        r"\b(?:just curious|quick question|for my understanding|"
        r"help me understand|walk me through how)\b",
        re.IGNORECASE,
    ),
)

# Durable agentic: recurring, multi-step, action-taking work on a board.
_DURABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:every day|every week|daily|weekly|ongoing|continuously|"
        r"automatically|autonomously|on a schedule|recurring|pipeline|"
        r"campaign|outreach|cold (?:text|call|email)|follow up with|"
        r"manage my|handle all|run my|operate my|keep (?:texting|calling|"
        r"posting|running))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:set up and run|launch and maintain|track and (?:act|respond|"
        r"negotiate)|negotiate (?:with|deals)|source (?:and|&) qualify|"
        r"close deals|wholesale|under contract|assignee|dispatcher)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:multi-?step|over time|long-?running|durable|standing board|"
        r"existing board|work the board|on the board)\b",
        re.IGNORECASE,
    ),
)

# Ambiguous: could be advice or durable work — one clarifying question first.
_AMBIGUOUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:help (?:me )?(?:grow|build|start|launch|scale|improve)|"
        r"i want to (?:build|start|launch|grow|create|make)|"
        r"need to (?:build|start|launch|grow)|"
        r"thinking about (?:building|starting|launching))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:grow my|scale my|build an? |start an? |launch an? )\b",
        re.IGNORECASE,
    ),
)

_CLARIFY_TEMPLATES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(?:app|ios|android|mobile|saas|software)\b", re.IGNORECASE),
        (
            "Before I set up a tracked plan: do you want ongoing development, "
            "releases, and ops managed over time, or a one-time roadmap I can "
            "walk through with you here?"
        ),
    ),
    (
        re.compile(r"\b(?:grow|page|followers|audience|marketing|tiktok|instagram|"
                   r"youtube|content)\b", re.IGNORECASE),
        (
            "To route this well: should I run growth experiments and outreach "
            "on a schedule with measurable targets, or are you looking for "
            "strategy advice you can execute yourself?"
        ),
    ),
    (
        re.compile(r"\b(?:land|wholesale|real estate|parcel|seller|buyer)\b", re.IGNORECASE),
        (
            "Are you asking for a durable deal pipeline — sourcing, outreach, "
            "negotiation, and contracts tracked end-to-end — or a one-off analysis "
            "on a specific property?"
        ),
    ),
    (
        re.compile(r"\b(?:build|create|make|project)\b", re.IGNORECASE),
        (
            "Is this something you want managed as ongoing multi-step work with "
            "approvals and tracking, or a single planning conversation for now?"
        ),
    ),
)

_DEFAULT_CLARIFY = (
    "Should I treat this as ongoing tracked work you'll approve before anything "
    "runs, or answer it as a one-time question here in chat?"
)


@dataclass(frozen=True)
class LaunchIntentResult:
    intent: LaunchIntent
    confidence: str
    rationale: str
    clarifying_question: Optional[str] = None
    suggested_next_action: str = ""

    def as_dict(self) -> dict[str, str]:
        out: dict[str, str] = {
            "intent": self.intent,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "suggested_next_action": self.suggested_next_action,
        }
        if self.clarifying_question:
            out["clarifying_question"] = self.clarifying_question
        return out


def _matches(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def _suggest_clarifying_question(goal: str) -> str:
    text = str(goal or "").strip()
    for pattern, question in _CLARIFY_TEMPLATES:
        if pattern.search(text):
            return question
    return _DEFAULT_CLARIFY


def _next_action_for_intent(
    intent: LaunchIntent,
    *,
    clarifying_question: Optional[str],
    has_strong_board_match: bool,
    top_board_slug: Optional[str],
) -> str:
    if intent == "no_board_needed":
        return (
            "Answer inline using safe tools (web, read, skills). Do NOT call "
            "kanban_business_launch_review."
        )
    if intent == "maybe_board":
        q = clarifying_question or _DEFAULT_CLARIFY
        return (
            "Ask the owner ONE natural clarifying question (not a yes/no checkbox) "
            f"before launch review. Example shape: {q!r}. After they reply, "
            "re-run kanban_match_board; if durable work is confirmed, call "
            "kanban_business_launch_review(create_if_missing=true) and load the "
            "launch-intake-interview skill."
        )
    if has_strong_board_match and top_board_slug:
        return (
            f"A strong existing board match was found ({top_board_slug!r}). Route "
            "through that board (status, contract, amendments) unless the owner "
            "explicitly wants a new board. For a new board, call "
            "kanban_business_launch_review(create_if_missing=true) and load "
            "launch-intake-interview during intake."
        )
    return (
        "Proceed to kanban_business_launch_review(create_if_missing=true, "
        "rough_goal=...). Load launch-intake-interview skill when relaying "
        "questions or follow-ups. Never self-approve."
    )


def classify_launch_intent(
    goal: str,
    *,
    top_match_score: int = 0,
    candidate_count: int = 0,
) -> LaunchIntentResult:
    """Classify whether a message needs a kanban board / launch review."""
    text = str(goal or "").strip()
    if not text:
        return LaunchIntentResult(
            intent="no_board_needed",
            confidence="high",
            rationale="Empty goal — nothing to route.",
            suggested_next_action=_next_action_for_intent(
                "no_board_needed",
                clarifying_question=None,
                has_strong_board_match=False,
                top_board_slug=None,
            ),
        )

    strong_match = top_match_score >= 3
    moderate_match = top_match_score >= 2 and candidate_count > 0

    if _matches(_LIGHTWEIGHT_PATTERNS, text) and not _matches(_DURABLE_PATTERNS, text):
        if len(text) < 120 or "?" in text:
            return LaunchIntentResult(
                intent="no_board_needed",
                confidence="high",
                rationale=(
                    "Reads as a question, lookup, or explanation — answer inline "
                    "without launch review."
                ),
                suggested_next_action=_next_action_for_intent(
                    "no_board_needed",
                    clarifying_question=None,
                    has_strong_board_match=strong_match,
                    top_board_slug=None,
                ),
            )

    if _matches(_DURABLE_PATTERNS, text) or strong_match:
        slug_hint = None
        if moderate_match or strong_match:
            slug_hint = "matched-board"
        return LaunchIntentResult(
            intent="board_required",
            confidence="high" if _matches(_DURABLE_PATTERNS, text) else "medium",
            rationale=(
                "Durable multi-step or recurring agentic work detected"
                + ("; existing board candidate aligns." if moderate_match else ".")
            ),
            suggested_next_action=_next_action_for_intent(
                "board_required",
                clarifying_question=None,
                has_strong_board_match=strong_match or moderate_match,
                top_board_slug=slug_hint,
            ),
        )

    if _matches(_AMBIGUOUS_PATTERNS, text) or (len(text) < 200 and not moderate_match):
        question = _suggest_clarifying_question(text)
        return LaunchIntentResult(
            intent="maybe_board",
            confidence="medium",
            rationale=(
                "Goal could be lightweight advice or durable tracked work — confirm "
                "before launch review."
            ),
            clarifying_question=question,
            suggested_next_action=_next_action_for_intent(
                "maybe_board",
                clarifying_question=question,
                has_strong_board_match=False,
                top_board_slug=None,
            ),
        )

    if moderate_match:
        return LaunchIntentResult(
            intent="board_required",
            confidence="medium",
            rationale="Existing board candidate likely covers this goal.",
            suggested_next_action=_next_action_for_intent(
                "board_required",
                clarifying_question=None,
                has_strong_board_match=True,
                top_board_slug="matched-board",
            ),
        )

    return LaunchIntentResult(
        intent="maybe_board",
        confidence="low",
        rationale="No strong lightweight or durable signal — confirm intent before launch.",
        clarifying_question=_suggest_clarifying_question(text),
        suggested_next_action=_next_action_for_intent(
            "maybe_board",
            clarifying_question=_suggest_clarifying_question(text),
            has_strong_board_match=False,
            top_board_slug=None,
        ),
    )


def normalize_intent(value: Optional[str]) -> Optional[LaunchIntent]:
    text = str(value or "").strip().lower()
    if text in _INTENT_VALUES:
        return text  # type: ignore[return-value]
    return None
