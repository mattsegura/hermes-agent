"""Single source of truth for the typed trigger/primitive grammar.

DECLARE, DON'T INFER. A trigger is a *typed primitive*: a closed ``kind`` field
that validators read directly, plus a free-text ``detail`` field that nothing
parses. The closed vocabulary is::

    timer | inbound | state_change | metric | manual

and a typed trigger looks like::

    {"kind": "timer",        "detail": "nudge seller", "cadence_hours": 72}
    {"kind": "inbound",      "detail": "seller replies", "channel": "infobip"}
    {"kind": "state_change", "detail": "...", "from_state": "...", "to_state": "..."}
    {"kind": "metric",       "detail": "activation drop", "metric": "activation_rate",
                              "comparator": "<=", "threshold": 0.30}
    {"kind": "manual",       "detail": "owner kicks off"}

Why this module exists: the invariant checker and the behavioural simulator both
need to answer the same questions about a contract -- "is this trigger an inbound
reply?", "is this a recurring/timer trigger?". Classification lives here, once,
so the checkers can never disagree, and so there is exactly one place to harden.

Typed is the PRIMARY path: :func:`resolve_trigger_kind` reads the ``kind`` field
and never guesses from prose when it is present. Keyword/token matching survives
ONLY inside :func:`classify_legacy_trigger`, the one-time *ingest* shim that
upcasts legacy free-text / ``type``-only triggers into a typed ``kind`` at
normalization time. It is a migration shim, NOT a runtime/validation checker --
the validation hot path reads ``kind``.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# Closed vocabulary -- the single source of truth for trigger kinds.
# --------------------------------------------------------------------------
TRIGGER_KINDS: frozenset[str] = frozenset(
    {"timer", "inbound", "state_change", "metric", "manual"}
)

# --------------------------------------------------------------------------
# Closed vocabulary -- the single source of truth for side-effect classes.
#
# DECLARE, DON'T INFER (mirrors the trigger ``kind`` pattern). A
# ``side_effect_class`` is a typed primitive the dispatch/approval rail reads
# directly -- NOT free text the synthesizer can fat-finger into a near-miss that
# silently dispatches ungated. The closed vocabulary is::
#
#     none | internal | external_reversible | external_irreversible | financial
#
#   none                  -- pure read / no state change anywhere.
#   internal              -- mutates only board-internal state (drafts, notes).
#   external_reversible   -- an external side effect that can be undone
#                            (send a message, post a draft, book a slot).
#   external_irreversible -- an external side effect that cannot be undone
#                            (publish, delete, sign).
#   financial             -- moves money / incurs spend.
#
# Everything from ``external_reversible`` up is "external": it crosses the board
# boundary and MUST be governed by the board ``side_effect_policy`` (declared in
# ``approval_required`` or ``forbidden``). See
# :func:`hermes_cli.kanban_launch_invariants.check_contract_invariants`.
# --------------------------------------------------------------------------
SIDE_EFFECT_CLASSES: frozenset[str] = frozenset(
    {"none", "internal", "external_reversible", "external_irreversible", "financial"}
)

#: The subset of side-effect classes that cross the board boundary and so MUST
#: be governed by the board's ``side_effect_policy`` (a hard rail: an
#: ``external_*``/``financial`` action that is neither approval-gated nor
#: forbidden is rejected at contract-check time).
EXTERNAL_SIDE_EFFECT_CLASSES: frozenset[str] = frozenset(
    {"external_reversible", "external_irreversible", "financial"}
)


def is_known_side_effect_class(value: Any) -> bool:
    """True iff ``value`` is a member of the closed side-effect vocabulary."""
    return isinstance(value, str) and value.strip().lower() in SIDE_EFFECT_CLASSES


def is_external_side_effect_class(value: Any) -> bool:
    """True iff ``value`` names a board-boundary-crossing side-effect class."""
    return isinstance(value, str) and value.strip().lower() in EXTERNAL_SIDE_EFFECT_CLASSES


def normalize_side_effect_class(value: Any) -> Optional[str]:
    """Return the canonical (lower, stripped) class, or None if not in vocab."""
    if not isinstance(value, str):
        return None
    canon = value.strip().lower()
    return canon if canon in SIDE_EFFECT_CLASSES else None

# Reply-style tokens: an external party is responding to us, so the stage models
# an ongoing conversation that must be watched. Kept tight to reply words so a
# free-text trigger that merely contains the noun "message" or "call" (e.g.
# "after any approved campaign, message, or store update") is not misread.
INBOUND_TOKENS: tuple[str, ...] = (
    "inbound", "reply", "replie", "incoming", "responds", "responded", "writes back",
)

# Time-based / recurring wake tokens (follow-up, timeout, cadence). Includes
# natural-language cadence phrases because legacy synthesized triggers are often
# free text ("Every sprint day", "daily check-in") rather than structured types.
TIMER_TOKENS: tuple[str, ...] = (
    "timer", "schedule", "delay", "timeout", "cron", "follow_up", "follow-up",
    "daily", "hourly", "weekly", "nightly", "recurring", "periodic", "cadence",
    "every ", "each day", "each hour", "per day", "per hour", "sprint day", "interval",
)

# Metric / threshold tokens: a measured value crossing a bound.
METRIC_TOKENS: tuple[str, ...] = (
    "rate", "ratio", "percent", "%", "threshold", "below", "above", "drops",
    "drop ", "kpi", "metric", "approaches", "remains near", "remains at or",
    "conversion", "activation",
)

# Manual / operator tokens: a human deliberately kicks the loop.
MANUAL_TOKENS: tuple[str, ...] = (
    "manual", "owner kicks", "owner decision", "kick off", "kickoff",
    "owner check", "owner_check", "owner question", "owner_question",
    "owner approves", "owner approve", "by hand", "operator kicks",
)

# Closed vocabulary of LEGACY structured trigger types (the pre-``kind`` shape,
# carried in a ``type`` field). The ingest shim recognises these directly.
INBOUND_TYPES: frozenset[str] = frozenset({
    "inbound", "inbound_sms", "inbound_email", "inbound_message", "inbound_call",
    "reply", "message_received", "incoming",
})
TIMER_TYPES: frozenset[str] = frozenset({
    "timer", "schedule", "scheduled", "cron", "recurring", "interval", "follow_up",
})


def trigger_type(trigger: Any) -> str:
    if isinstance(trigger, str):
        return trigger.lower()
    if isinstance(trigger, dict):
        return str(trigger.get("type") or trigger.get("reason") or "").lower()
    return ""


def trigger_types(triggers: Any) -> list[str]:
    return [t for t in (trigger_type(x) for x in _iter_triggers(triggers)) if t]


def _iter_triggers(triggers: Any) -> list[Any]:
    if isinstance(triggers, (list, tuple, set)):
        return list(triggers)
    if triggers is None:
        return []
    return [triggers]


def _matches(text: str, tokens: Iterable[str]) -> bool:
    return any(tok in text for tok in tokens)


# --------------------------------------------------------------------------
# Typed (PRIMARY) classification: read the closed ``kind`` field directly.
# --------------------------------------------------------------------------
def trigger_kind(trigger: Any) -> Optional[str]:
    """Return the declared ``kind`` of a trigger, or None if it carries none.

    Only a value inside the closed vocabulary is accepted; an unknown ``kind``
    returns None here (and is rejected at normalization time).
    """
    if isinstance(trigger, dict):
        raw = trigger.get("kind")
        if isinstance(raw, str):
            k = raw.strip().lower()
            if k in TRIGGER_KINDS:
                return k
    return None


def is_timer_kind(trigger: Any) -> bool:
    return trigger_kind(trigger) == "timer"


def is_inbound_kind(trigger: Any) -> bool:
    return trigger_kind(trigger) == "inbound"


def is_state_change_kind(trigger: Any) -> bool:
    return trigger_kind(trigger) == "state_change"


def is_metric_kind(trigger: Any) -> bool:
    return trigger_kind(trigger) == "metric"


def is_manual_kind(trigger: Any) -> bool:
    return trigger_kind(trigger) == "manual"


# --------------------------------------------------------------------------
# Legacy keyword text predicates (used only by the ingest shim below).
# --------------------------------------------------------------------------
def is_inbound_text(text: str) -> bool:
    t = (text or "").lower()
    return t in INBOUND_TYPES or _matches(t, INBOUND_TOKENS)


def is_timer_text(text: str) -> bool:
    t = (text or "").lower()
    return t in TIMER_TYPES or _matches(t, TIMER_TOKENS)


def _legacy_text(trigger: Any) -> str:
    """Flatten a legacy trigger's describable fields into one lowercase string."""
    if isinstance(trigger, str):
        return trigger.lower()
    if isinstance(trigger, dict):
        parts: list[str] = []
        for key in ("type", "reason", "detail", "name", "event", "description"):
            value = trigger.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
        return " ".join(parts).lower()
    return ""


def classify_legacy_trigger(trigger: Any) -> str:
    """ONE-TIME ingest classification: map a legacy trigger to a closed ``kind``.

    MIGRATION SHIM ONLY. This is the single surviving place where keyword/token
    matching over free text is allowed -- it upcasts legacy free-text or
    ``type``-only triggers into the typed grammar at normalization time. It is
    NOT a runtime or validation checker; validators read ``kind`` (see
    :func:`resolve_trigger_kind`).

    A legacy trigger that matches no known token falls back to ``state_change``
    -- the domain-agnostic "something happened that we should react to" bucket.
    """
    text = _legacy_text(trigger)
    if not text:
        return "manual"
    if is_inbound_text(text):
        return "inbound"
    if is_timer_text(text):
        return "timer"
    if _matches(text, METRIC_TOKENS):
        return "metric"
    if _matches(text, MANUAL_TOKENS):
        return "manual"
    return "state_change"


def resolve_trigger_kind(trigger: Any) -> str:
    """Resolve the effective ``kind`` of a trigger -- typed first, shim second.

    Typed input (a valid ``kind``) is read directly; legacy input falls back to
    the one-time ingest classifier. This is the function the validators use so
    the typed path is always primary and keyword matching only ever touches
    un-normalized legacy input.
    """
    declared = trigger_kind(trigger)
    if declared is not None:
        return declared
    return classify_legacy_trigger(trigger)


def has_inbound(triggers: Any) -> bool:
    return any(resolve_trigger_kind(t) == "inbound" for t in _iter_triggers(triggers))


def has_timer(triggers: Any) -> bool:
    return any(resolve_trigger_kind(t) == "timer" for t in _iter_triggers(triggers))


# --------------------------------------------------------------------------
# Normalization: upcast any trigger to the typed shape with a validated kind.
# --------------------------------------------------------------------------
def normalize_trigger(trigger: Any) -> dict[str, Any]:
    """Return a typed trigger object carrying a validated closed ``kind``.

    * Genuinely typed input (already declares ``kind``): the kind is validated
      against the closed vocabulary -- an unknown kind is REJECTED.
    * Legacy free-text / ``type``-only input: the one-time ingest classifier
      assigns a ``kind`` from the closed vocabulary. Original fields (``type``,
      ``channel``, ``reason``, ...) are preserved for back-compat.

    Every normalized trigger has a ``detail`` string (the free text nothing
    parses), back-filled from legacy fields when absent.
    """
    if isinstance(trigger, dict):
        out = dict(trigger)
        raw_kind = out.get("kind")
        if raw_kind is not None and str(raw_kind).strip():
            k = str(raw_kind).strip().lower()
            if k not in TRIGGER_KINDS:
                raise ValueError(
                    f"trigger declares unknown kind {raw_kind!r}; "
                    f"valid kinds: {sorted(TRIGGER_KINDS)}"
                )
            out["kind"] = k
        else:
            out["kind"] = classify_legacy_trigger(out)
        detail = out.get("detail")
        if detail is None or not str(detail).strip():
            detail = out.get("reason") or out.get("type") or out.get("name") or ""
        out["detail"] = str(detail)
        return out
    if isinstance(trigger, str):
        return {"kind": classify_legacy_trigger(trigger), "detail": trigger}
    # Unknown shapes stay visible but typed as operator-driven.
    return {"kind": "manual", "detail": str(trigger)}


def classify_trigger(trigger: Any) -> dict[str, Any]:
    """Return an observable classification of a single trigger.

    Surfacing this (e.g. in the owner/coverage report) makes a wrong call visible
    to a human reviewer and to tests, instead of being re-derived silently inside
    each checker. ``kind`` reads the typed field when present and falls back to
    the ingest classifier for legacy input.
    """
    kind = resolve_trigger_kind(trigger)
    return {
        "raw": trigger,
        "kind": kind,
        "type": trigger_type(trigger),
        "inbound": kind == "inbound",
        "timer": kind == "timer",
    }
