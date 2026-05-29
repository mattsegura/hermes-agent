"""Tests for the structural grammar invariants on board operating contracts."""

import json
import os

from hermes_cli.kanban_launch_invariants import check_contract_invariants

_FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "launch_intake")


def _load(name: str) -> dict:
    with open(os.path.join(_FIXTURES, f"{name}.contract.json")) as fh:
        return json.load(fh)


def test_land_wholesaling_fixture_passes_invariants():
    report = check_contract_invariants(_load("land_wholesaling"))
    assert report.ok, report.errors
    # The negotiate stage is the one conversational watch stage.
    assert report.checked.get("conversational_stages", 0) >= 1


def test_insurance_recruiting_fixture_passes_invariants():
    report = check_contract_invariants(_load("insurance_recruiting"))
    assert report.ok, report.errors
    # Has tunable knobs with ranges.
    assert report.checked.get("tunables", 0) >= 1


def test_research_team_fixture_passes_invariants():
    report = check_contract_invariants(_load("research_team"))
    assert report.ok, report.errors


def test_conversational_event_loop_without_timer_fails():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "entities": [{"key": "lead", "type": "lead", "terminal_states": ["won", "lost"]}],
        "event_loops": [
            {
                "type": "lead_loop",
                "entity": "lead",
                # inbound reply but NO timer -> can never follow up
                "triggers": ["lead_reply"],
                "terminal_states": ["won", "lost"],
                "stop_conditions": ["lead says no", "lead ghosts"],
            }
        ],
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("no timer" in e or "timer trigger" in e for e in report.errors)


def test_watched_entity_without_stop_condition_fails():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "event_loops": [
            {
                "type": "thread_loop",
                "entity": "thread",
                "triggers": ["timer"],
                # no terminal_states and no stop_conditions -> zombie risk
            }
        ],
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("stop" in e.lower() or "terminal" in e.lower() for e in report.errors)


def test_conversational_stage_with_single_exit_fails():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {
            "stages": [
                {
                    "key": "negotiate",
                    "substates": [{"key": "waiting"}, {"key": "hot"}],
                    "triggers": [
                        {"type": "inbound_sms"},
                        {"type": "timer", "reason": "follow_up_due"},
                    ],
                    # only one exit -> cannot distinguish success from kill
                    "exit_criteria": [{"transition": "close", "evidence_required": ["done"]}],
                }
            ]
        },
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("exit" in e.lower() for e in report.errors)


def test_unbounded_tunable_fails():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "tunables": {"follow_up_interval_hours": {"default": 72}},  # no range
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("range" in e.lower() for e in report.errors)


# --- Regression ratchet: real synthesized contracts captured from the wild ---
# Every time a real contract is misclassified, drop it here as a fixture so the
# exact failure can never silently return. This one is the "grow my app, 1-week
# deadline" contract whose `daily_proof_packet` loop was wrongly flagged as a
# conversational loop missing a timer (the trigger reads "Every sprint day...").

def test_grow_app_one_week_fixture_passes_invariants():
    report = check_contract_invariants(_load("grow_app_one_week"))
    assert report.ok, report.errors
    # The daily cadence loop must NOT be treated as a missing-timer conversation.
    assert not any("timer trigger" in e or "no timer" in e for e in report.errors)


# --- Property tests: classification must be stable across paraphrase ---
# Keyword matching over free text is the fallback for legacy triggers. These
# guard the two failure modes that bit us: a cadence phrase read as
# non-recurring, and a noun like "message" read as an inbound conversation.

from hermes_cli.kanban_launch_grammar import (  # noqa: E402
    TRIGGER_KINDS,
    classify_legacy_trigger,
    classify_trigger,
    has_inbound,
    has_timer,
    normalize_trigger,
    resolve_trigger_kind,
)


def test_natural_language_cadences_classify_as_timer():
    for phrase in [
        "Every sprint day at 9am",
        "daily check-in",
        "recurring nightly sweep",
        "follow-up due",
        "per day digest",
    ]:
        assert has_timer([phrase]), phrase
        # A pure cadence is not an inbound conversation.
        assert not has_inbound([phrase]), phrase


def test_message_noun_is_not_misread_as_inbound():
    # The exact phrasing that caused the original false positive.
    trigger = "after any approved campaign, message, or store update"
    assert not has_inbound([trigger]), trigger


def test_structured_types_beat_prose():
    # A typed trigger is classified by its type, never by guessing on prose.
    assert has_inbound([{"type": "inbound_sms"}])
    assert has_timer([{"type": "timer", "reason": "anything at all"}])


# --- P0: DECLARE, DON'T INFER -- the typed `kind` is the primary path ---


def test_typed_kind_classifies_with_no_keyword_in_detail():
    # The detail text carries NO inbound/timer keyword; classification must read
    # the declared `kind`, proving validators no longer reverse-engineer prose.
    inbound = {"kind": "inbound", "detail": "the counterparty gets in touch"}
    timer = {"kind": "timer", "detail": "poke the lead again", "cadence_hours": 72}
    assert resolve_trigger_kind(inbound) == "inbound"
    assert resolve_trigger_kind(timer) == "timer"
    assert has_inbound([inbound])
    assert has_timer([timer])
    # And the legacy keyword shim would NOT have found these -- confirming the
    # typed read, not a keyword coincidence.
    assert classify_legacy_trigger(inbound) != "inbound"
    assert classify_legacy_trigger(timer) != "timer"


def test_all_five_typed_kinds_round_trip():
    samples = {
        "timer": {"kind": "timer", "detail": "nudge", "cadence_hours": 24},
        "inbound": {"kind": "inbound", "detail": "they reply", "channel": "infobip"},
        "state_change": {"kind": "state_change", "detail": "x", "from_state": "a", "to_state": "b"},
        "metric": {"kind": "metric", "detail": "drop", "metric": "activation_rate",
                   "comparator": "<=", "threshold": 0.30},
        "manual": {"kind": "manual", "detail": "owner kicks off"},
    }
    assert set(samples) == set(TRIGGER_KINDS)
    for expected, trig in samples.items():
        out = normalize_trigger(trig)
        assert out["kind"] == expected
        assert isinstance(out["detail"], str) and out["detail"]
        assert classify_trigger(trig)["kind"] == expected


def test_legacy_freetext_is_upcast_by_ingest_shim():
    # Exactly the legacy strings the existing fixtures carry.
    assert normalize_trigger("follow_up_timer")["kind"] == "timer"
    assert normalize_trigger("candidate_reply")["kind"] == "inbound"
    assert normalize_trigger("Every sprint day")["kind"] == "timer"
    # A genuinely unclassifiable legacy string falls back to state_change.
    assert normalize_trigger("reviewer_rejection")["kind"] == "state_change"
    # The original free text is preserved as the human-readable detail.
    assert normalize_trigger("follow_up_timer")["detail"] == "follow_up_timer"


def test_legacy_type_object_is_upcast_and_preserved():
    out = normalize_trigger({"type": "inbound_sms", "channel": "infobip"})
    assert out["kind"] == "inbound"
    assert out["type"] == "inbound_sms"  # original field preserved
    assert out["channel"] == "infobip"


def test_unknown_kind_on_typed_input_is_rejected():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        normalize_trigger({"kind": "banana", "detail": "nope"})


def test_land_fixture_conversational_stage_reads_kind():
    # The negotiate stage in the gold contract is now fully typed; the validator
    # classifies it off `kind` and still passes every invariant.
    report = check_contract_invariants(_load("land_wholesaling"))
    assert report.ok, report.errors


# --- 2A (F10): a watcher must have a MACHINE-CHECKABLE stop ---


def test_stop_conditions_only_loop_is_rejected():
    # Free-text stop_conditions alone are NOT machine-checkable: nothing parses
    # them, so the timer fires forever. The tightened rail demands a
    # terminal_states entry or a finite max_nudges.
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "entities": [{"key": "thread", "type": "thread", "states": ["open"]}],
        "event_loops": [
            {
                "type": "thread_loop",
                "entity": "thread",
                "triggers": [{"kind": "timer", "detail": "nudge", "cadence_hours": 24}],
                # ONLY free-text stop_conditions, no terminal_states/max_nudges.
                "stop_conditions": ["owner says stop", "lead goes cold"],
            }
        ],
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("free-text stop_conditions" in e for e in report.errors), report.errors


def test_finite_max_nudges_satisfies_the_stop_rail():
    # A finite max_nudges IS machine-checkable, so a loop with only that (plus
    # free-text stop_conditions) passes the rail.
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "entities": [{"key": "thread", "type": "thread", "states": ["open"]}],
        "event_loops": [
            {
                "type": "thread_loop",
                "entity": "thread",
                "triggers": [{"kind": "timer", "detail": "nudge", "cadence_hours": 24}],
                "stop_conditions": ["owner says stop"],
                "max_nudges": 3,
            }
        ],
    }
    report = check_contract_invariants(contract)
    assert report.ok, report.errors


# --- 2B (B3): side_effect_class is a typed, governed closed vocabulary ---


def _b3_base_contract():
    return {
        "objective": {"statement": "x"},
        "workflow": {
            "stages": [
                {
                    "key": "outreach",
                    "actions": [
                        {"key": "research", "side_effect_class": "none"},
                        {"key": "send_email", "side_effect_class": "external_reversible"},
                    ],
                    "exit_criteria": [
                        {"transition": "won", "evidence_required": ["reply"]},
                        {"transition": "lost", "evidence_required": ["bounce"]},
                    ],
                }
            ]
        },
        "side_effect_policy": {
            "allowed": ["none", "internal"],
            "approval_required": ["external_reversible"],
            "forbidden": ["external_irreversible", "financial"],
        },
    }


def test_typed_external_class_gated_in_policy_passes():
    report = check_contract_invariants(_b3_base_contract())
    assert report.ok, report.errors
    assert report.checked.get("side_effect_classes", 0) >= 2


def test_near_miss_side_effect_class_is_rejected():
    contract = _b3_base_contract()
    # Typo: 'externl_reversible' is not in the closed vocabulary.
    contract["workflow"]["stages"][0]["actions"][1]["side_effect_class"] = "externl_reversible"
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("unknown side_effect_class" in e for e in report.errors), report.errors


def test_ungated_external_side_effect_is_rejected():
    contract = _b3_base_contract()
    # Known class, but not declared in approval_required/forbidden -> ungoverned.
    contract["side_effect_policy"] = {"allowed": ["none", "internal", "external_reversible"]}
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("external side effect" in e for e in report.errors), report.errors


def test_undeclared_internal_class_is_rejected():
    contract = _b3_base_contract()
    contract["workflow"]["stages"][0]["actions"][0]["side_effect_class"] = "internal"
    contract["side_effect_policy"] = {
        "allowed": ["none"],  # 'internal' used but not declared
        "approval_required": ["external_reversible"],
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("not declared in" in e for e in report.errors), report.errors

