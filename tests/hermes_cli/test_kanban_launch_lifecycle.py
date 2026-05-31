"""Tests for launch contract lifecycle gates and cost scaffold."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli.kanban_launch_cost import ensure_contract_cost_scaffold, project_contract_costs
from hermes_cli.kanban_launch_lifecycle import (
    derive_contract_status,
    launch_gate_blockers,
    launch_phase_for_contract_status,
    resolve_post_review_launch_phase,
)
from hermes_cli.kanban_launch_summary import export_contract_document, render_contract_sections_table


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "launch_intake"


def _load(slug: str) -> dict:
    return json.loads((FIXTURES / f"{slug}.contract.json").read_text())


def test_cost_projection_is_labeled_estimate():
    contract = _load("land_wholesaling")
    enriched = ensure_contract_cost_scaffold(contract)
    projection = enriched["runtime"]["cost_projection"]
    assert projection.get("accuracy") == "estimate"
    assert projection.get("total_usd_weekly") is not None
    assert "disclaimer" in projection


def test_derive_contract_status_pending_approval_when_ready():
    contract = _load("land_wholesaling")
    readiness = {"ok": True, "missing": [], "errors": [], "status": "ready"}
    status = derive_contract_status(contract, readiness=readiness)
    assert status == "pending_approval"
    assert launch_phase_for_contract_status("pending_credentials") == "pending_credentials"
    assert launch_phase_for_contract_status("pending_approval") == "contract_review"
    assert launch_phase_for_contract_status("draft") == "contract_review"


def test_resolve_post_review_launch_phase_not_active_without_approve():
    contract = _load("land_wholesaling")
    phase = resolve_post_review_launch_phase(
        contract=contract,
        readiness={"ok": True, "missing": [], "errors": []},
        approve=False,
    )
    assert phase == "contract_review"


def test_launch_gate_blockers_intake_incomplete():
    contract = {
        "launch_intake": {
            "source": "model_generated",
            "state": "clarifying",
            "answers": {"q1": "a1"},
        },
        "objective": {"statement": "x"},
    }
    blockers = launch_gate_blockers(contract, approving=True)
    assert blockers
    assert any("intake" in b.lower() for b in blockers)


def test_export_markdown_contains_sections_table():
    contract = _load("grow_app_one_week")
    doc = export_contract_document(contract, format="markdown", board="grow_app_one_week")
    assert "## Sections" in doc
    assert "| Section | Summary |" in doc


def test_export_json_roundtrip_facts():
    contract = _load("land_wholesaling")
    raw = export_contract_document(contract, format="json", board="land")
    payload = json.loads(raw)
    assert payload["board"] == "land"
    assert payload["summary_facts"]["pipeline"]


def test_sections_table_renders_status():
    contract = ensure_contract_cost_scaffold(_load("land_wholesaling"))
    table = render_contract_sections_table(contract, board="land_wholesaling")
    assert "| Status |" in table
    assert "| Budget |" in table
