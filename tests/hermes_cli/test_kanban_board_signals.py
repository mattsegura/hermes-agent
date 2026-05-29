"""Tests for the P0 signal-emission ledger (board_signals) and the typed
event-loop trigger normalization that feeds it.

board_signals is the data layer for the learning loop: every live primitive
action (stage transition, terminal outcome) is captured as a clean typed
datapoint. These tests prove the table fills with real data from the parts of
the task loop that are live today, and that event-loop triggers are upcast to
the typed `kind` grammar (with unknown kinds rejected).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb


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


def _signals(conn, board):
    return conn.execute(
        "SELECT id, primitive_kind, primitive_key, entity_ref, knob_snapshot, "
        "action, context_features, reward_value, reward_kind, realized_at "
        "FROM board_signals WHERE board = ? ORDER BY id",
        (board,),
    ).fetchall()


# --------------------------------------------------------------------------
# Outcome signal on completion (the live, in-scope emission point).
# --------------------------------------------------------------------------
def test_outcome_signal_written_on_completion(fresh_home):
    kb.create_board("sig", name="Signals")
    with kb.connect(board="sig") as conn:
        tid = kb.create_task(conn, title="plain work", board="sig")
        assert kb.complete_task(conn, tid, summary="done", board="sig") is True
        rows = _signals(conn, "sig")

    kinds = [r["primitive_kind"] for r in rows]
    assert "outcome" in kinds, kinds
    outcome = next(r for r in rows if r["primitive_kind"] == "outcome")
    assert outcome["entity_ref"] == tid
    assert outcome["reward_kind"] == "time_to_done"
    assert outcome["reward_value"] is not None
    assert outcome["realized_at"] is not None


# --------------------------------------------------------------------------
# Stage-transition signal (the other live emission point).
# --------------------------------------------------------------------------
def test_stage_transition_signal_written(fresh_home):
    workflow = {
        "id": "wf",
        "goal_id": "g",
        "require_semantics": True,
        "workstreams": [{"key": "ops", "stages": ["a", "b"]}],
        "stages": [
            {
                "key": "a",
                "actions": [{"key": "act"}],
                "exit_criteria": [{"transition": "b", "evidence_required": []}],
            },
            {"key": "b", "actions": [{"key": "fin"}], "exit_criteria": []},
        ],
    }
    kb.create_board("sigwf", name="Signals WF", workflow=workflow, launch_phase="active")
    with kb.connect(board="sigwf") as conn:
        tid = kb.create_task(
            conn,
            title="staged work",
            goal_id="g",
            workstream_id="ops",
            stage_key="a",
            action_key="act",
            board="sigwf",
        )
        kb.transition_task_stage(conn, tid, to_stage="b", action_key="fin", board="sigwf")
        rows = _signals(conn, "sigwf")

    stage_rows = [r for r in rows if r["primitive_kind"] == "stage"]
    assert stage_rows, rows
    sig = stage_rows[0]
    assert sig["primitive_key"] == "b"
    assert sig["entity_ref"] == tid
    import json

    action = json.loads(sig["action"])
    assert action["kind"] == "transition"
    assert action["params"]["from_stage"] == "a"
    assert action["params"]["to_stage"] == "b"


def test_record_board_signal_rejects_unknown_primitive_kind(fresh_home):
    kb.create_board("sigx", name="Signals X")
    with kb.connect(board="sigx") as conn:
        with pytest.raises(ValueError):
            kb.record_board_signal(conn, primitive_kind="not_a_kind", board="sigx")


# --------------------------------------------------------------------------
# Event-loop trigger normalization (typed grammar feeding the runtime).
# --------------------------------------------------------------------------
def test_normalize_event_loops_upcasts_legacy_triggers():
    loops = [
        {
            "type": "lead_loop",
            "entity": "lead",
            "triggers": ["candidate_reply", "follow_up_timer", "Activation rate below 30%"],
            "terminal_states": ["won", "lost"],
        }
    ]
    out = kb.normalize_event_loops(loops)
    kinds = [t["kind"] for t in out[0]["triggers"]]
    assert kinds == ["inbound", "timer", "metric"]
    # detail back-filled from the original free text, nothing parses it.
    assert out[0]["triggers"][0]["detail"] == "candidate_reply"


def test_normalize_event_loops_rejects_unknown_kind():
    loops = [
        {
            "entity": "lead",
            "triggers": [{"kind": "banana", "detail": "nope"}],
            "terminal_states": ["x"],
        }
    ]
    with pytest.raises(ValueError):
        kb.normalize_event_loops(loops)


def test_contract_normalization_upcasts_event_loop_triggers():
    contract = {
        "objective": {"statement": "x"},
        "event_loops": [
            {"entity": "lead", "triggers": ["lead_reply", "follow_up_timer"],
             "terminal_states": ["won"], "stop_conditions": ["ghost"]}
        ],
    }
    normalized = kb.normalize_board_operating_contract(contract)
    trigs = normalized["event_loops"][0]["triggers"]
    assert [t["kind"] for t in trigs] == ["inbound", "timer"]
