"""Property / fuzz tests for the typed trigger grammar + invariant checker.

These use Hypothesis to hammer the grammar with random-but-structured input and
assert the *invariants of the invariants*: classification is total and
deterministic, the typed ``kind`` is always honoured over free-text ``detail``,
the one-time legacy ingest shim is stable, and the structural invariant checker
never raises (it returns a well-formed report) on arbitrary structurally-valid
contracts. This is the hardening half of P4b: instead of a handful of golden
fixtures, we prove the grammar holds across the whole input space.

Offline + deterministic-under-seed: no model calls, no I/O.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_launch_grammar as g
from hermes_cli import kanban_launch_invariants as inv

# A generous per-test budget keeps these comfortably inside the suite's 30s
# per-test hard cap while still exploring a wide input space. Deadlines are
# disabled because CI load can make a single example slow without meaning the
# property failed.
_SETTINGS = settings(max_examples=200, deadline=None)

# Free text that nothing parses. Deliberately includes adversarial nouns
# ("message", "call", "reply", "timer", "rate") so we prove the typed ``kind``
# wins over whatever the prose happens to say.
_DETAIL_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=80,
) | st.sampled_from([
    "",
    "seller replies to our message",
    "after any approved campaign, message, or store update",
    "owner places a call to the lead",
    "the conversion rate drops below threshold",
    "follow up every 72 hours",
    "nothing in particular happens here",
    "🤖 emoji and unicode ☎ message",
])

_CHANNEL = st.sampled_from(["infobip", "sms", "email", "intercom", "slack", "phone"])
_STATE = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12)
_METRIC = st.sampled_from(["activation_rate", "mrr", "conversion", "cac", "churn"])
_COMPARATOR = st.sampled_from(["<=", ">=", "<", ">"])


@st.composite
def typed_triggers(draw) -> dict:
    """A trigger that DECLARES a valid ``kind`` plus random params + free detail."""
    kind = draw(st.sampled_from(sorted(g.TRIGGER_KINDS)))
    trigger: dict = {"kind": kind, "detail": draw(_DETAIL_TEXT)}
    if kind == "timer":
        trigger["cadence_hours"] = draw(st.integers(min_value=1, max_value=720))
    elif kind == "inbound":
        trigger["channel"] = draw(_CHANNEL)
    elif kind == "state_change":
        trigger["from_state"] = draw(_STATE)
        trigger["to_state"] = draw(_STATE)
    elif kind == "metric":
        trigger["metric"] = draw(_METRIC)
        trigger["comparator"] = draw(_COMPARATOR)
        trigger["threshold"] = draw(st.floats(min_value=0, max_value=1e6,
                                               allow_nan=False, allow_infinity=False))
    return trigger


# ---------------------------------------------------------------------------
# 1. Typed triggers: declared ``kind`` always wins, classification is total +
#    deterministic regardless of the free-text ``detail``.
# ---------------------------------------------------------------------------


@_SETTINGS
@given(trigger=typed_triggers())
def test_typed_kind_is_authoritative_over_detail(trigger):
    declared = trigger["kind"]
    # The declared kind is read directly -- prose in ``detail`` never overrides.
    assert g.resolve_trigger_kind(trigger) == declared
    assert g.trigger_kind(trigger) == declared
    classification = g.classify_trigger(trigger)
    assert classification["kind"] == declared
    assert classification["inbound"] == (declared == "inbound")
    assert classification["timer"] == (declared == "timer")


@_SETTINGS
@given(trigger=typed_triggers())
def test_typed_classification_is_deterministic(trigger):
    a = g.resolve_trigger_kind(trigger)
    b = g.resolve_trigger_kind(dict(trigger))
    assert a == b
    # normalize_trigger preserves the declared kind and is idempotent on kind.
    norm = g.normalize_trigger(trigger)
    assert norm["kind"] == trigger["kind"]
    assert g.normalize_trigger(norm)["kind"] == trigger["kind"]


@_SETTINGS
@given(triggers=st.lists(typed_triggers(), min_size=0, max_size=6))
def test_invariant_checker_never_raises_on_typed_triggers(triggers):
    # Embed the random typed triggers in an event loop with declared stops; the
    # checker must read the typed kinds and complete without raising.
    contract = {
        "event_loops": [{
            "entity": "thing",
            "triggers": triggers,
            "terminal_states": ["done", "dead"],
            "stop_conditions": ["owner stops", "deadline passed"],
        }],
    }
    report = inv.check_contract_invariants(contract)
    assert isinstance(report.ok, bool)
    assert isinstance(report.errors, list)


# ---------------------------------------------------------------------------
# 2. Legacy free-text ingest shim: stable, and a noun like "message"/"call"
#    without a real reply token is NOT misread as inbound.
# ---------------------------------------------------------------------------

# Non-reply phrases that merely *contain* inbound-ish nouns. None of these are
# an external party replying to us, so none should classify as inbound.
_NON_INBOUND_NOUN_PHRASES = st.sampled_from([
    "after any approved campaign, message, or store update",
    "draft the outbound message for owner review",
    "schedule a call with the title company next week",
    "log a voicemail message in the system",
    "the call list is refreshed nightly",
    "prepare a broadcast message draft",
    "owner reviews the message template",
    "a new message template is created",
])

_FREE_TEXT = st.text(min_size=0, max_size=120)


@_SETTINGS
@given(text=_FREE_TEXT)
def test_legacy_classification_is_stable(text):
    # Same string -> same kind, every time, and always within the closed vocab.
    first = g.classify_legacy_trigger(text)
    second = g.classify_legacy_trigger(text)
    assert first == second
    assert first in g.TRIGGER_KINDS


@_SETTINGS
@given(phrase=_NON_INBOUND_NOUN_PHRASES)
def test_inbound_noun_without_reply_token_not_misread_as_inbound(phrase):
    # A bare "message"/"call" noun must not trip the inbound classifier; only
    # genuine reply-style tokens (reply/incoming/responds/...) may.
    assert g.classify_legacy_trigger(phrase) != "inbound"
    # And the same holds when wrapped as a legacy free-text trigger dict.
    assert g.classify_legacy_trigger({"detail": phrase}) != "inbound"


@_SETTINGS
@given(phrase=_NON_INBOUND_NOUN_PHRASES, suffix=_FREE_TEXT)
def test_legacy_idempotent_under_normalization(phrase, suffix):
    # Normalizing a legacy trigger then re-resolving its kind agrees with the
    # one-shot ingest classification -- normalization does not change the call.
    trigger = {"detail": f"{phrase} {suffix}"}
    direct = g.classify_legacy_trigger(trigger)
    norm = g.normalize_trigger(trigger)
    assert norm["kind"] == direct
    assert g.resolve_trigger_kind(norm) == direct


# ---------------------------------------------------------------------------
# 3. Random small typed contracts: the invariant checker terminates and returns
#    a well-formed report (no crash) on arbitrary structurally-valid-ish input.
# ---------------------------------------------------------------------------


@st.composite
def small_contracts(draw) -> dict:
    n_stages = draw(st.integers(min_value=0, max_value=4))
    stages = []
    for i in range(n_stages):
        stage: dict = {"key": f"stage_{i}"}
        if draw(st.booleans()):
            stage["substates"] = draw(st.lists(_STATE, min_size=0, max_size=3))
        if draw(st.booleans()):
            stage["triggers"] = draw(st.lists(typed_triggers(), min_size=0, max_size=3))
        if draw(st.booleans()):
            stage["exit_criteria"] = draw(
                st.lists(st.sampled_from(["won", "lost", "recycled", "dead"]),
                         min_size=0, max_size=3)
            )
        stages.append(stage)

    n_loops = draw(st.integers(min_value=0, max_value=3))
    loops = []
    for i in range(n_loops):
        loops.append({
            "entity": f"entity_{i}",
            "triggers": draw(st.lists(typed_triggers(), min_size=0, max_size=3)),
            "terminal_states": draw(st.lists(_STATE, min_size=0, max_size=3)),
            "stop_conditions": draw(st.lists(_STATE, min_size=0, max_size=3)),
        })

    tunables = {}
    for i in range(draw(st.integers(min_value=0, max_value=3))):
        spec: dict = {"default": draw(st.integers(0, 100))}
        choice = draw(st.integers(0, 2))
        if choice == 0:
            lo = draw(st.integers(0, 50))
            spec["range"] = [lo, lo + draw(st.integers(0, 50))]
        elif choice == 1:
            spec["allowed"] = draw(st.lists(st.integers(0, 100), min_size=1, max_size=4))
        # choice == 2: intentionally unbounded (should produce an *error*, not a crash)
        tunables[f"knob_{i}"] = spec

    contract: dict = {
        "workflow": {"stages": stages},
        "event_loops": loops,
        "tunables": tunables,
    }
    return contract


@_SETTINGS
@given(contract=small_contracts())
def test_invariant_checker_terminates_with_well_formed_report(contract):
    report = inv.check_contract_invariants(contract)
    # Well-formed report shape regardless of pass/fail.
    assert isinstance(report.ok, bool)
    assert isinstance(report.errors, list)
    assert isinstance(report.warnings, list)
    assert isinstance(report.checked, dict)
    # ok is True iff there are no hard errors -- the documented contract.
    assert report.ok == (not report.errors)
    # as_dict round-trips without raising.
    payload = report.as_dict()
    assert payload["ok"] == report.ok
