"""Tests for generic Kanban reactive entity runtime support."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb


_WORKTREE = Path(__file__).resolve().parents[2]


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _cli(args: list[str], hermes_home: Path, *, board: str = "serious") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "HERMES_HOME": str(hermes_home),
            "HERMES_KANBAN_BOARD": board,
            "PYTHONPATH": str(_WORKTREE),
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", *args],
        env=env,
        cwd=str(_WORKTREE),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _contract() -> dict:
    return {
        "objective": {
            "statement": "Run a mock reactive serious board",
            "success": ["conversation state is closed with proof"],
            "failure": ["reactive entity is dropped or spoofed"],
            "constraints": ["no outbound services"],
        },
        "runtime": {
            "mode": "company",
            "require_worker_envelopes": True,
            "require_provider_policy": True,
            "provider_policy": {
                "mock_research": {"provider": "fixture", "tool": "mock"}
            },
            "worker_envelopes": {
                "mock-worker": {
                    "capabilities": ["mock_research"],
                    "toolsets": ["kanban"],
                    "allowed_side_effects": ["none"],
                }
            },
        },
        "workflow": {
            "id": "reactive-contract-flow",
            "goal_id": "mock-goal",
            "require_semantics": True,
            "workstreams": [{"key": "ops", "stages": ["execute"]}],
            "stages": [
                {
                    "key": "execute",
                    "actions": [
                        {
                            "key": "research",
                            "required_capabilities": ["mock_research"],
                            "required_toolsets": ["kanban"],
                            "required_proof": ["mock_report"],
                            "side_effect_class": "none",
                        }
                    ],
                }
            ],
        },
    }


def _create_serious_root() -> str:
    kb.create_board("serious", runtime="company", contract=_contract())
    with kb.connect(board="serious") as conn:
        task_id = kb.create_task(
            conn,
            title="contracted reactive root",
            assignee="mock-worker",
            goal_id="mock-goal",
            workstream_id="ops",
            stage_key="execute",
            action_key="research",
            board="serious",
        )
    return task_id


def test_reactive_conversation_entity_watches_wakes_audits_and_gates_done(fresh_home):
    root_id = _create_serious_root()
    with kb.connect(board="serious") as conn:
        assert kb.claim_task(conn, root_id) is not None
        created = kb.create_reactive_entity_card(
            conn,
            entity_type="conversation_thread",
            title="Mock contact conversation",
            assignee="mock-worker",
            created_by="fixture",
            parents=[root_id],
            external_key="mock:thread-1",
            external_identity={
                "platform": "mock",
                "thread_id": "thread-1",
                "contact_id": "contact-1",
            },
            authority_limits={"no_outbound": True, "allowed_actions": ["read", "park"]},
            proof_requirements=["conversation_closed"],
            metadata={"contact": {"id": "contact-1"}},
            trigger={
                "trigger_type": "conversation.message",
                "trigger_key": "mock:thread-1",
                "reason": "Waiting for mock inbound message",
                "payload": {"source": "unit-test"},
                "wake_status": "ready",
            },
            goal_id="mock-goal",
            workstream_id="ops",
            stage_key="execute",
            action_key="research",
            board="serious",
        )
        child_id = created["task_id"]
        entity_id = created["entity"].id
        route = created["watch_route"]

        assert route is not None
        assert kb.get_task(conn, child_id).status == "watching"
        assert kb.get_reactive_entity(conn, entity_id).state == "watching"
        model = kb.build_funnel_read_model(conn, board="serious")
        child_card = next(
            card
            for stage in model["stages"]
            for card in stage["cards"]
            if card["id"] == child_id
        )
        assert child_card["reactive_entities"][0]["id"] == entity_id

        with pytest.raises(kb.ContractDoneGateError) as blocked:
            kb.complete_task(
                conn,
                root_id,
                summary="root done too early",
                metadata={"proof": ["mock_report"]},
                board="serious",
            )
        codes = {item["code"] for item in blocked.value.verdict["blockers"]}
        assert "unresolved_reactive_entity" in codes
        assert "reactive_entity_missing_required_proof" in codes

    cli_blocked = _cli(
        [
            "complete",
            root_id,
            "--summary",
            "cli done too early",
            "--metadata",
            json.dumps({"proof": ["mock_report"]}),
        ],
        fresh_home,
    )
    assert cli_blocked.returncode != 0
    assert "contract done gate failed" in cli_blocked.stderr

    with kb.connect(board="serious") as conn:
        triggered = kb.trigger_reactive_event(
            conn,
            trigger_type="conversation.message",
            trigger_key="mock:thread-1",
            payload={
                "event_id": "evt-1",
                "message_id": "msg-1",
                "body": "mock inbound",
            },
            actor="mock-gateway",
            board="serious",
        )
        assert triggered["status"] == "triggered"
        assert triggered["triggered_routes"][0]["id"] == route.id
        woken = kb.get_task(conn, child_id)
        assert woken.status == "todo"
        latest_inbound = woken.funnel_data["latest_inbound"]
        assert latest_inbound["payload"]["message_id"] == "msg-1"
        assert latest_inbound["actor"] == "mock-gateway"

        refreshed_entity = kb.get_reactive_entity(conn, entity_id)
        assert refreshed_entity.state == "triggered"
        assert refreshed_entity.metadata["latest_trigger"]["payload"]["event_id"] == "evt-1"
        events = kb.list_events(conn, child_id)
        assert "watch_triggered" in [event.kind for event in events]
        assert "reactive_entity_triggered" in [event.kind for event in events]
        audits = kb.list_reactive_trigger_audits(conn, entity_id=entity_id)
        assert audits[-1]["accepted"] is True
        assert audits[-1]["reason"] == "matched_route"

        duplicate = kb.trigger_reactive_event(
            conn,
            trigger_type="conversation.message",
            trigger_key="mock:thread-1",
            payload={"event_id": "evt-1", "message_id": "msg-1"},
            actor="mock-gateway",
            board="serious",
        )
        assert duplicate["status"] == "duplicate"
        assert duplicate["triggered_routes"] == []
        duplicate_audit = kb.list_reactive_trigger_audits(
            conn,
            fingerprint=triggered["fingerprint"],
        )[-1]
        assert duplicate_audit["accepted"] is False
        assert duplicate_audit["reason"] == "duplicate"

        unknown = kb.trigger_reactive_event(
            conn,
            trigger_type="conversation.message",
            trigger_key="mock:missing-thread",
            payload={"event_id": "evt-unknown", "message_id": "msg-unknown"},
            actor="mock-gateway",
            board="serious",
        )
        assert unknown["status"] == "triage_created"
        triage_task = kb.get_task(conn, unknown["triage_task_id"])
        assert triage_task.status == "triage"
        assert any(
            event.kind == "reactive_trigger_unknown"
            for event in kb.list_events(conn, unknown["triage_task_id"])
        )

        resolved = kb.resolve_reactive_entity(
            conn,
            entity_id,
            terminal_outcome="conversation_closed",
            metadata={"final_message_id": "msg-1"},
            proof=["conversation_closed"],
            actor="fixture",
        )
        assert resolved.active is False
        assert resolved.terminal is True

        assert kb.complete_task(
            conn,
            root_id,
            summary="root closed after entity proof",
            metadata={"proof": ["mock_report"]},
            board="serious",
        ) is True


def test_reactive_entity_malformed_metadata_fails_serious_done_gate(fresh_home):
    root_id = _create_serious_root()
    with kb.connect(board="serious") as conn:
        assert kb.claim_task(conn, root_id) is not None
        created = kb.create_reactive_entity_card(
            conn,
            entity_type="conversation_thread",
            title="Malformed entity",
            assignee="mock-worker",
            parents=[root_id],
            external_key="mock:bad",
            proof_requirements=["conversation_closed"],
            trigger={
                "trigger_type": "conversation.message",
                "trigger_key": "mock:bad",
            },
            goal_id="mock-goal",
            workstream_id="ops",
            stage_key="execute",
            action_key="research",
            board="serious",
        )
        entity_id = created["entity"].id
        conn.execute(
            "UPDATE reactive_entities SET metadata = '{not-json' WHERE id = ?",
            (entity_id,),
        )
        conn.commit()

        with pytest.raises(kb.ContractDoneGateError) as excinfo:
            kb.complete_task(
                conn,
                root_id,
                summary="malformed bypass",
                metadata={"proof": ["mock_report", "conversation_closed"]},
                board="serious",
            )
        codes = {item["code"] for item in excinfo.value.verdict["blockers"]}
        assert "reactive_entity_invalid_metadata" in codes
        assert kb.get_task(conn, root_id).status == "running"
