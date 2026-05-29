"""Layer-2 behavioural simulation tests for conversational stages.

These prove that a generated/known contract's conversational stage actually
drives scripted prospects to a terminal outcome -- it is *functional*, not just
well-shaped. A contract missing the follow-up timer leaves the ghost/slow-burn
scenarios unresolved, which is the failure the harness exists to catch.
"""

import json
import os

import pytest

from hermes_cli.kanban_launch_simulation import (
    build_conversation_model,
    run_default_simulation,
    simulate_scenario,
)

_FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "launch_intake")


def _load(name: str) -> dict:
    with open(os.path.join(_FIXTURES, f"{name}.contract.json")) as fh:
        return json.load(fh)


@pytest.mark.parametrize("fixture", ["land_wholesaling", "insurance_recruiting"])
def test_conversational_stage_resolves_every_scenario(fixture):
    contract = _load(fixture)
    model = build_conversation_model(contract)
    assert model is not None, f"{fixture}: no conversational stage found"
    assert model.has_inbound and model.has_timer, f"{fixture}: missing inbound/timer wiring"

    results = run_default_simulation(contract)
    assert results, f"{fixture}: nothing simulated"
    # No zombies: every scripted prospect must reach a terminal outcome.
    for name, res in results.items():
        assert res.resolved, f"{fixture}/{name} did not resolve (outcome={res.outcome})"


def test_happy_path_reaches_success_outcome():
    results = run_default_simulation(_load("insurance_recruiting"))
    assert results["happy_path"].category == "success"


def test_ghost_scenario_fires_followups_then_kills():
    results = run_default_simulation(_load("insurance_recruiting"))
    ghost = results["ghost"]
    assert ghost.resolved
    assert ghost.nudges_fired >= 1  # the follow-up timer actually fired


def test_slow_burn_recovers_after_one_nudge():
    results = run_default_simulation(_load("insurance_recruiting"))
    slow = results["slow_burn"]
    assert slow.resolved and slow.category == "success"
    assert slow.nudges_fired == 1


def test_timerless_contract_cannot_resolve_ghost():
    """A conversational stage with no timer leaves ghosts unresolved -- caught."""
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "entities": [{"key": "lead", "terminal_states": ["won", "lost"]}],
        "event_loops": [
            {
                "type": "lead_loop",
                "entity": "lead",
                "triggers": ["lead_reply"],  # inbound, but NO timer
                "terminal_states": ["won", "lost"],
                "stop_conditions": ["lead ghosts", "lead says no"],
            }
        ],
    }
    model = build_conversation_model(contract)
    assert model is not None and not model.has_timer
    ghost = simulate_scenario(model, {"name": "ghost", "events": ["timeout", "timeout", "timeout", "timeout"]})
    assert not ghost.resolved  # the harness catches the missing follow-up timer


def test_simulation_reads_typed_kind_not_prose():
    """A typed event loop whose trigger detail contains NO inbound/timer keyword
    is still recognised as a conversational watch loop -- the simulator reads the
    declared `kind`, not the prose."""
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "entities": [{"key": "lead", "terminal_states": ["won", "lost"]}],
        "event_loops": [
            {
                "type": "lead_loop",
                "entity": "lead",
                "triggers": [
                    {"kind": "inbound", "detail": "the counterparty gets in touch"},
                    {"kind": "timer", "detail": "poke them once more", "cadence_hours": 48},
                ],
                "terminal_states": ["won", "lost"],
                "stop_conditions": ["lead ghosts", "lead says no"],
            }
        ],
    }
    model = build_conversation_model(contract)
    assert model is not None
    assert model.has_inbound and model.has_timer
    ghost = simulate_scenario(
        model, {"name": "ghost", "events": ["timeout", "timeout", "timeout", "timeout"]}
    )
    assert ghost.resolved and ghost.nudges_fired >= 1
