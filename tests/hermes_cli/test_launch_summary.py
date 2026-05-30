"""Tests for the deterministic contract -> owner-facing summary renderer.

The renderer is the single source of truth for "explain a contract to a human"
used by the launch-review tool result, the gateway /approve message, and the
pending-board list. These tests lock the owner-legible shape: the pipeline, what
the board watches (with stop semantics), the bounded auto-tunable dials, the
approval boundary, and the /approve call to action -- and prove the renderer
never raises on a partial/empty contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.kanban_launch_summary import (
    contract_summary_facts,
    render_contract_one_liner,
    render_owner_contract_summary,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "launch_intake"


def _load(slug: str) -> dict:
    return json.loads((FIXTURES / f"{slug}.contract.json").read_text())


# ---------------------------------------------------------------------------
# Golden-fixture summaries
# ---------------------------------------------------------------------------


def test_land_summary_has_every_owner_section():
    contract = _load("land_wholesaling")
    out = render_owner_contract_summary(contract, board="land_wholesaling")
    # Draft framing + the explicit /approve call to action.
    assert "Draft contract" in out
    assert "/approve land_wholesaling" in out
    assert "contract review" in out.lower()
    # Pipeline names the real stages.
    assert "*Pipeline:*" in out
    assert "Source leads" in out
    assert "Negotiate with sellers" in out
    # Substates are surfaced inline on the negotiate stage.
    assert "follow up due" in out or "hot lead" in out
    # Watchers carry stop semantics.
    assert "*Watches & follow-ups:*" in out
    assert "stops when" in out.lower()
    # Approval boundary is explicit.
    assert "*Needs your approval:*" in out
    # Bounded auto-tunable dials with their ranges.
    assert "auto-tune" in out.lower()
    assert "range" in out.lower()
    assert "follow up interval hours" in out
    # Safety sensors named.
    assert "Safety sensors" in out


def test_active_framing_drops_approve_hint():
    contract = _load("land_wholesaling")
    out = render_owner_contract_summary(
        contract, board="land_wholesaling", active=True, include_approve_hint=False
    )
    assert "Launched" in out
    assert "/approve" not in out
    # Still shows the structure.
    assert "*Pipeline:*" in out


def test_one_liner_is_dense_and_arrowed():
    contract = _load("land_wholesaling")
    line = render_contract_one_liner(contract)
    assert "→" in line
    assert "Source leads" in line
    assert "owner-gated" in line
    assert "watcher" in line
    # One line only.
    assert "\n" not in line


def test_facts_extract_pipeline_watchers_knobs():
    contract = _load("land_wholesaling")
    facts = contract_summary_facts(contract)
    assert facts["pipeline"]  # non-empty
    assert facts["watchers"]
    assert facts["knobs"]
    assert facts["needs_approval"]
    # A validated contract must surface NO autonomous external action.
    assert facts["autonomous_external"] == []


def test_grow_app_and_research_render_without_error():
    for slug in ("grow_app_one_week", "research_team", "insurance_recruiting"):
        contract = _load(slug)
        out = render_owner_contract_summary(contract, board=slug)
        assert "*Pipeline:*" in out
        assert f"/approve {slug}" in out


def test_tiktok_summary_surfaces_publish_gate_and_cadence():
    """The content-automation domain renders the same owner-legible shape: a
    produce->post pipeline, a publish action that is owner-gated (irreversible),
    a 48-hour performance watcher, and the bounded posting-cadence dial."""
    contract = _load("tiktok_reels")
    out = render_owner_contract_summary(contract, board="tiktok-reels")
    # Produce -> post pipeline is named.
    assert "*Pipeline:*" in out
    assert "production" in out.lower()
    assert "publish" in out.lower()
    # The irreversible TikTok publish is owner-gated, never autonomous.
    assert "*Needs your approval:*" in out
    assert "publish to tiktok" in out.lower()
    facts = contract_summary_facts(contract)
    assert facts["autonomous_external"] == []
    # A performance watcher with stop semantics.
    assert "*Watches & follow-ups:*" in out
    assert "stops when" in out.lower()
    # Posting-cadence / backlog dials are bounded.
    assert "auto-tune" in out.lower()
    assert "daily reel target" in out.lower()
    assert "/approve tiktok-reels" in out


# ---------------------------------------------------------------------------
# Robustness: never raise on a partial / malformed / empty contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "contract",
    [
        {},
        {"objective": {"statement": "Do a thing."}},
        {"workflow": {"stages": [{"key": "only_stage"}]}},
        {"event_loops": [{"entity": "lead"}]},  # no stop conditions declared
        {"tunables": {"x": {}}},  # knob with no bounds
        {"objective": "string objective", "workflow": {"stages": "bad"}},
        None,
        "not a contract",
        123,
    ],
)
def test_render_never_raises_on_partial(contract):
    out = render_owner_contract_summary(contract, board="b")
    assert isinstance(out, str)
    # Draft framing is always present even for a near-empty contract.
    assert "/approve b" in out
    line = render_contract_one_liner(contract)
    assert isinstance(line, str)
    facts = contract_summary_facts(contract)
    assert isinstance(facts, dict)


def test_autonomous_external_surfaces_when_ungated():
    """A pre-validation contract with an ungated external action is flagged loudly."""
    contract = {
        "objective": {"statement": "Send stuff."},
        "workflow": {
            "stages": [
                {
                    "key": "blast",
                    "label": "Blast",
                    "actions": [
                        {
                            "key": "send_sms",
                            "label": "Send SMS to everyone",
                            "side_effect_class": "external_reversible",
                        }
                    ],
                }
            ]
        },
        # No approval_gates, no side_effect_policy -> ungated.
    }
    facts = contract_summary_facts(contract)
    assert "Send SMS to everyone" in facts["autonomous_external"]
    out = render_owner_contract_summary(contract, board="b")
    assert "WITHOUT approval" in out
