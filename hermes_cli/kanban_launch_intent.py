"""Launch intent gate (Layer 1) for the reasoning+router profile.

Classifies an owner message before ``kanban_business_launch_review`` so the
router agent can answer lightweight requests inline, ask one natural clarifying
question when ambiguous, or proceed to full launch intake when durable agentic
structure is clearly needed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal, Optional

LaunchIntent = Literal["no_board_needed", "maybe_board", "board_required"]

logger = logging.getLogger(__name__)

_INTENT_VALUES: frozenset[str] = frozenset(
    {"no_board_needed", "maybe_board", "board_required"}
)

CLARIFYING_QUESTION_STYLE = "open_contextual"

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

# Open-ended, domain-aware fallbacks — invite scope/outcome description, not A/B picks.
_CLARIFY_TEMPLATES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:tiktok|instagram|youtube|content|followers|audience|page|"
            r"marketing|social)\b",
            re.IGNORECASE,
        ),
        (
            "What would success look like for you over the next few weeks — "
            "growth targets, content cadence, engagement you care about?"
        ),
    ),
    (
        re.compile(r"\b(?:app|ios|android|mobile|saas|software)\b", re.IGNORECASE),
        (
            "Tell me more about what you're building and what outcomes you'd "
            "want tracked over the next few weeks?"
        ),
    ),
    (
        re.compile(
            r"\b(?:land|wholesale|real estate|parcel|seller|buyer)\b",
            re.IGNORECASE,
        ),
        (
            "Help me understand your land goal — geography, deal flow, and "
            "what you'd want happening week to week?"
        ),
    ),
    (
        re.compile(r"\b(?:grow|scale|build|start|launch|create|make|project)\b", re.IGNORECASE),
        (
            "Tell me a bit more about what you're trying to accomplish and "
            "how hands-on you want this to be?"
        ),
    ),
)

_DEFAULT_CLARIFY = (
    "Tell me a bit more about what you're trying to accomplish and how "
    "hands-on you want this to be?"
)

# Patterns that make a clarifying question feel like a checkbox — used for validation.
_BANNED_CLARIFY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bis this\b.*\bor\b", re.IGNORECASE),
    re.compile(r"\b(?:one-?time|ongoing)\b", re.IGNORECASE),
    re.compile(r"\b(?:autonomous(?:ly)?|automatic(?:ally)?)\b.*\b(?:manual|yourself)\b", re.IGNORECASE),
    re.compile(r"\bshould i (?:run|treat|set up)\b.*\bor\b", re.IGNORECASE),
    re.compile(r"\b(?:advice|strategy)\b.*\bor\b", re.IGNORECASE),
    re.compile(r"\bare you asking for\b.*\bor\b", re.IGNORECASE),
)

_CLARIFY_AUX_SYSTEM = """You generate ONE open-ended clarifying question for a kanban launch router.

Rules:
- Single sentence, conversational — invite the owner to describe scope and desired outcome
- NO binary choices (never "Is this X or Y", never one-time vs ongoing, never autonomous vs manual)
- NO multiple-choice or checkbox feel
- Domain-aware when the message hints at a domain (TikTok/social -> content cadence and growth; app -> features/releases; land -> geography and deal flow)
- Return JSON only: {"question": "..."}"""


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
            out["clarifying_question_style"] = CLARIFYING_QUESTION_STYLE
        return out


def _matches(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def is_binary_clarifying_question(question: str) -> bool:
    """True when a question reads like a dichotomy or checkbox."""
    text = str(question or "").strip()
    if not text:
        return True
    return any(p.search(text) for p in _BANNED_CLARIFY_PATTERNS)


def _template_clarifying_question(goal: str) -> str:
    text = str(goal or "").strip()
    for pattern, question in _CLARIFY_TEMPLATES:
        if pattern.search(text):
            return question
    return _DEFAULT_CLARIFY


def _try_aux_clarifying_question(goal: str, fallback: str) -> str:
    """Optional aux-model question; returns fallback when unconfigured or invalid."""
    try:
        from hermes_cli.company_launch import _call_model, aux_configured
    except Exception:  # pragma: no cover - defensive import
        return fallback

    if not aux_configured():
        return fallback

    raw, degraded = _call_model(
        _CLARIFY_AUX_SYSTEM,
        {"owner_message": goal, "fallback_question": fallback},
        timeout=15,
        max_tokens=120,
        temperature=0.3,
    )
    if degraded or not raw:
        return fallback

    try:
        from hermes_cli.company_launch import _extract_json
    except Exception:  # pragma: no cover
        return fallback

    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        return fallback

    question = str(parsed.get("question") or "").strip()
    if not question or "?" not in question:
        return fallback
    if is_binary_clarifying_question(question):
        return fallback
    return question


def _suggest_clarifying_question(goal: str) -> str:
    fallback = _template_clarifying_question(goal)
    return _try_aux_clarifying_question(goal, fallback)


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
        return (
            "Ask the owner ONE open, contextual clarifying question before launch "
            "review. Use intent.clarifying_question as a guide only — paraphrase in "
            "your own voice; do not read it verbatim or offer labeled either/or "
            "choices. After they reply, re-run kanban_match_board; if durable work "
            "is confirmed, call kanban_business_launch_review(create_if_missing=true) "
            "and load the launch-intake-interview skill."
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

    question = _suggest_clarifying_question(text)
    return LaunchIntentResult(
        intent="maybe_board",
        confidence="low",
        rationale="No strong lightweight or durable signal — confirm intent before launch.",
        clarifying_question=question,
        suggested_next_action=_next_action_for_intent(
            "maybe_board",
            clarifying_question=question,
            has_strong_board_match=False,
            top_board_slug=None,
        ),
    )


def normalize_intent(value: Optional[str]) -> Optional[LaunchIntent]:
    text = str(value or "").strip().lower()
    if text in _INTENT_VALUES:
        return text  # type: ignore[return-value]
    return None
