"""F1: owner-facing error translation (single source of truth).

Internal validator keys / enum errors / missing-field strings must render to
plain owner sentences with NO raw key, enum name, or validator jargon leaking.
"""

from __future__ import annotations

import sys
from pathlib import Path

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb


# Raw tokens/jargon that must NEVER appear in an owner-facing sentence.
_FORBIDDEN_FRAGMENTS = (
    "side_effect_policy",
    "side_effect_class",
    "exit_criteria",
    "objective.success",
    "must be one of",
    "missing:",
    "[",
    "]",
    "_",  # no snake_case keys at all
)


def _assert_owner_safe(sentence: str) -> None:
    assert sentence and isinstance(sentence, str)
    for frag in _FORBIDDEN_FRAGMENTS:
        assert frag not in sentence, f"raw fragment {frag!r} leaked: {sentence!r}"


def test_missing_side_effect_policy_renders_plain():
    sentence = kb.owner_facing_error_sentence("missing: ['side_effect_policy']")
    _assert_owner_safe(sentence)
    assert "off-limits" in sentence.lower() or "actions" in sentence.lower()


def test_enum_error_renders_plain():
    raw = (
        "side_effect_class must be one of: none, internal, external_reversible, "
        "external_irreversible, financial"
    )
    sentence = kb.owner_facing_error_sentence(raw)
    _assert_owner_safe(sentence)


def test_missing_exit_criteria_renders_plain():
    sentence = kb.owner_facing_error_sentence(
        "workflow.stages.0.exit_criteria is required"
    )
    _assert_owner_safe(sentence)
    assert "finished" in sentence.lower() or "stop" in sentence.lower()


def test_objective_success_renders_plain():
    sentence = kb.owner_facing_error_sentence("objective.success is required")
    _assert_owner_safe(sentence)


def test_unknown_error_falls_back_without_leaking():
    sentence = kb.owner_facing_error_sentence("ZZZ totally opaque internal blowup 42")
    # Generic fallback — never the raw string.
    assert sentence == kb._OWNER_ERROR_GENERIC
    assert "ZZZ" not in sentence


def test_sentences_dedupe_and_translate_a_batch():
    raws = [
        "missing: ['side_effect_policy']",
        "side_effect_class must be one of: none, internal",  # same vocabulary → dedupes
        "objective.success is required",
        "workflow.stages.1.exit_criteria is required",
    ]
    out = kb.owner_facing_error_sentences(raws)
    # side_effect_policy + side_effect_class collapse to one sentence.
    assert len(out) == 3
    for sentence in out:
        _assert_owner_safe(sentence)


def test_translation_reuses_single_question_vocabulary():
    # The translator must resolve through the SAME map as launch_clarity_questions,
    # not a forked vocabulary.
    sentence = kb.owner_facing_error_sentence("side_effect_policy")
    expected_q = kb._LAUNCH_QUESTION_BY_MISSING["side_effect_policy"]
    assert expected_q.rstrip("?").lower()[:20] in sentence.lower()
