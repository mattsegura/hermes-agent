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


# --- Hardening Pass 2: invariant semantic gaps R1-R6 ------------------------
#
# Each Rn gets a positive (valid passes) and negative (violating contract is
# rejected with a clear error) case. The four golden fixtures continue to pass
# all invariants (covered by the fixture tests above + the eval ratchet).


# R1: knob ranges must be sane (min < max, default in range, numeric types).


def test_r1_valid_knob_range_passes():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "tunables": {
            "follow_up_interval_hours": {"default": 72, "range": [24, 240]},
            "fit_threshold": {"default": 70, "min": 0, "max": 100},
            "mode": {"default": "fast", "allowed": ["fast", "slow"]},
        },
    }
    report = check_contract_invariants(contract)
    assert report.ok, report.errors


def test_r1_inverted_range_is_rejected():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "tunables": {"cadence_hours": {"default": 48, "range": [240, 24]}},  # min > max
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("inverted" in e and "range" in e for e in report.errors), report.errors


def test_r1_degenerate_range_is_rejected():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "tunables": {"cadence_hours": {"default": 24, "range": [24, 24]}},  # min == max
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("degenerate" in e for e in report.errors), report.errors


def test_r1_default_outside_range_is_rejected():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "tunables": {"cadence_hours": {"default": 999, "range": [24, 240]}},
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("outside its range" in e for e in report.errors), report.errors


def test_r1_default_not_in_allowed_is_rejected():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "tunables": {"mode": {"default": "turbo", "allowed": ["fast", "slow"]}},
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("not one of its allowed values" in e for e in report.errors), report.errors


# R2: a typed kind:"timer" loop must declare a usable cadence.


def test_r2_typed_timer_with_cadence_passes():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "entities": [{"key": "t", "type": "t", "terminal_states": ["done"]}],
        "event_loops": [
            {
                "type": "t_loop",
                "entity": "t",
                "triggers": [{"kind": "timer", "detail": "nudge", "cadence_hours": 24}],
                "terminal_states": ["done"],
            }
        ],
    }
    report = check_contract_invariants(contract)
    assert report.ok, report.errors


def test_r2_typed_timer_binds_managed_cadence_knob_passes():
    # No literal cadence on the trigger, but a managed cadence knob supplies it.
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "entities": [{"key": "t", "type": "t", "terminal_states": ["done"]}],
        "event_loops": [
            {
                "type": "t_loop",
                "entity": "t",
                "triggers": [{"kind": "timer", "detail": "nudge"}],
                "terminal_states": ["done"],
            }
        ],
        "tunables": {"follow_up_interval_hours": {"default": 72, "range": [24, 240]}},
    }
    report = check_contract_invariants(contract)
    assert report.ok, report.errors


def test_r2_typed_timer_without_cadence_is_rejected():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "entities": [{"key": "t", "type": "t", "terminal_states": ["done"]}],
        "event_loops": [
            {
                "type": "t_loop",
                "entity": "t",
                "triggers": [{"kind": "timer", "detail": "nudge"}],  # no cadence, no knob
                "terminal_states": ["done"],
            }
        ],
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("no usable cadence" in e for e in report.errors), report.errors


def test_r2_zero_cadence_is_rejected():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "entities": [{"key": "t", "type": "t", "terminal_states": ["done"]}],
        "event_loops": [
            {
                "type": "t_loop",
                "entity": "t",
                "triggers": [{"kind": "timer", "detail": "nudge", "cadence_hours": 0}],
                "terminal_states": ["done"],
            }
        ],
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("not a usable fire interval" in e for e in report.errors), report.errors


# R3: a stage that consumes inbound AND emits an external side effect must have
# a timer/timeout exit and >= 2 exits -- detected structurally so renaming the
# free text cannot dodge the rail.


def _r3_external_inbound_stage(extra: dict | None = None) -> dict:
    stage = {
        "key": "outreach",
        # NOTE: no substates -- this is the evasion the old check missed.
        "triggers": [
            {"kind": "inbound", "detail": "they reply"},
            {"kind": "timer", "detail": "follow up", "cadence_hours": 48},
        ],
        "actions": [{"key": "send", "side_effect_class": "external_reversible"}],
        "exit_criteria": [
            {"transition": "won", "evidence_required": ["reply"]},
            {"transition": "lost", "evidence_required": ["ghost"]},
        ],
        "side_effect_policy": None,
    }
    if extra:
        stage.update(extra)
    return {
        "objective": {"statement": "x"},
        "workflow": {"stages": [stage]},
        "side_effect_policy": {
            "allowed": ["none"],
            "approval_required": ["external_reversible"],
        },
    }


def test_r3_external_inbound_stage_with_timer_and_two_exits_passes():
    report = check_contract_invariants(_r3_external_inbound_stage())
    assert report.ok, report.errors
    assert report.checked.get("conversational_stages", 0) >= 1


def test_r3_external_inbound_stage_without_timer_is_rejected():
    contract = _r3_external_inbound_stage()
    # Drop the timer trigger -> conversational stage can never nudge.
    contract["workflow"]["stages"][0]["triggers"] = [
        {"kind": "inbound", "detail": "they reply"}
    ]
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("no follow-up timer" in e for e in report.errors), report.errors


def test_r3_external_inbound_stage_with_single_exit_is_rejected():
    contract = _r3_external_inbound_stage()
    contract["workflow"]["stages"][0]["exit_criteria"] = [
        {"transition": "won", "evidence_required": ["reply"]}
    ]
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("fewer than 2 exit" in e for e in report.errors), report.errors


# R4: an event loop must not watch an entity that is never declared.


def test_r4_loop_watching_declared_entity_passes():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "entities": [{"key": "lead", "type": "lead", "terminal_states": ["won", "lost"]}],
        "event_loops": [
            {
                "type": "lead_loop",
                "entity": "lead",
                "triggers": [{"kind": "timer", "detail": "nudge", "cadence_hours": 24}],
                "terminal_states": ["won", "lost"],
            }
        ],
    }
    report = check_contract_invariants(contract)
    assert report.ok, report.errors


def test_r4_loop_watching_undeclared_entity_is_rejected():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "entities": [{"key": "lead", "type": "lead", "terminal_states": ["won", "lost"]}],
        "event_loops": [
            {
                "type": "ghost_loop",
                "entity": "ghost",  # not declared in entities
                "triggers": [{"kind": "timer", "detail": "nudge", "cadence_hours": 24}],
                "terminal_states": ["done"],
            }
        ],
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("not declared in 'entities'" in e for e in report.errors), report.errors


# R5: an irreversible/financial action must be explicitly gated.


def _r5_contract(gated: bool) -> dict:
    contract = {
        "objective": {"statement": "x"},
        "workflow": {
            "stages": [
                {
                    "key": "close",
                    "actions": [
                        {"key": "wire_funds", "side_effect_class": "financial"},
                    ],
                    "exit_criteria": [
                        {"transition": "done", "evidence_required": ["receipt"]},
                    ],
                }
            ]
        },
        "side_effect_policy": {"allowed": ["none"]},
    }
    if gated:
        contract["approval_gates"] = [
            {"key": "owner_payment_approval", "required_before": ["wire_funds"]}
        ]
        contract["side_effect_policy"]["approval_required"] = ["financial"]
    return contract


def test_r5_gated_financial_action_passes():
    report = check_contract_invariants(_r5_contract(gated=True))
    assert report.ok, report.errors
    assert report.checked.get("irreversible_actions", 0) >= 1


def test_r5_ungated_financial_action_is_rejected():
    # No approval gate AND not in side_effect_policy.approval_required/forbidden.
    contract = _r5_contract(gated=False)
    contract["side_effect_policy"]["approval_required"] = []
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("ungated irreversible/financial" in e for e in report.errors), report.errors


# R6: a knob referenced by a loop/stage must exist as a bounded tunable.


def test_r6_resolvable_knob_reference_passes():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "entities": [{"key": "t", "type": "t", "terminal_states": ["done"]}],
        "event_loops": [
            {
                "type": "t_loop",
                "entity": "t",
                "cadence_knob": "follow_up_interval_hours",
                "triggers": [{"kind": "timer", "detail": "nudge", "cadence_hours": 24}],
                "terminal_states": ["done"],
            }
        ],
        "tunables": {"follow_up_interval_hours": {"default": 72, "range": [24, 240]}},
    }
    report = check_contract_invariants(contract)
    assert report.ok, report.errors
    assert report.checked.get("knob_references", 0) >= 1


def test_r6_dangling_knob_reference_is_rejected():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "entities": [{"key": "t", "type": "t", "terminal_states": ["done"]}],
        "event_loops": [
            {
                "type": "t_loop",
                "entity": "t",
                "cadence_knob": "does_not_exist",  # dangling reference
                "triggers": [{"kind": "timer", "detail": "nudge", "cadence_hours": 24}],
                "terminal_states": ["done"],
            }
        ],
        "tunables": {"follow_up_interval_hours": {"default": 72, "range": [24, 240]}},
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("dangling knob reference" in e for e in report.errors), report.errors

