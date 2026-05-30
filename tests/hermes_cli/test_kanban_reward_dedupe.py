"""Hardening Pass 2 -- WS2: reward over-counting fix (exactly-once attribution).

A single logical outcome (a reply, a loop-terminal resolution, a completion)
must count **at most once** toward a knob's posterior, no matter how many times
a tick is replayed or the signal is re-emitted. The fix is a stable
``dedupe_key`` on board_signals (partial UNIQUE index + INSERT OR IGNORE) plus a
read-path dedupe guard. These tests prove replays do not bias the learner.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_optimizer as opt

KNOB = "follow_up_interval_hours"


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


def _contract(spec: dict) -> dict:
    return {
        "objective": {"statement": "Optimize follow-up cadence"},
        "workflow": {"id": "f", "stages": [{"key": "execute", "actions": [{"key": "nudge"}]}]},
        "tunables": {KNOB: spec},
    }


def _make_board(slug: str, spec: dict) -> None:
    kb.create_board(slug)
    kb.write_board_metadata(slug, business_contract=_contract(spec))


def _emit(conn, *, board, value, reward_value, ts, dedupe_key=None, reward_kind="conversion"):
    with kb.write_txn(conn):
        return kb.record_board_signal(
            conn,
            board=board,
            primitive_kind="outcome",
            primitive_key="reply",
            knob_snapshot={KNOB: value},
            reward_value=reward_value,
            reward_kind=reward_kind,
            realized_at=ts,
            ts=ts,
            dedupe_key=dedupe_key,
        )


def _total_n(conn, board, spec):
    _family, ev = opt.read_knob_outcome_evidence(conn, board=board, knob=KNOB, spec=spec)
    return sum(e.n for e in ev.values())


# ---------------------------------------------------------------------------
# Replaying the same outcome N times counts exactly once.
# ---------------------------------------------------------------------------


def test_replayed_outcome_with_dedupe_key_counts_once(fresh_home):
    spec = {"default": 72, "allowed": [24, 72]}
    _make_board("dedupe", spec)
    with kb.connect(board="dedupe") as conn:
        ids = [
            _emit(conn, board="dedupe", value=48, reward_value=1.0, ts=1_000 + i,
                  dedupe_key="reply:route1:fingerprintA")
            for i in range(5)
        ]
        # Only one row physically exists, and every replay returns the same id.
        assert len(set(ids)) == 1, ids
        count = conn.execute(
            "SELECT COUNT(*) FROM board_signals WHERE board = ? AND dedupe_key = ?",
            ("dedupe", "reply:route1:fingerprintA"),
        ).fetchone()[0]
        assert count == 1
        # The learner sees exactly one observation for that arm.
        assert _total_n(conn, "dedupe", spec) == 1


def test_without_dedupe_key_each_emit_counts(fresh_home):
    # Control: legacy telemetry path (no dedupe_key) still appends every row.
    spec = {"default": 72, "allowed": [24, 72]}
    _make_board("nodedupe", spec)
    with kb.connect(board="nodedupe") as conn:
        for i in range(5):
            _emit(conn, board="nodedupe", value=48, reward_value=1.0, ts=2_000 + i)
        assert _total_n(conn, "nodedupe", spec) == 5


def test_distinct_dedupe_keys_each_count(fresh_home):
    spec = {"default": 72, "allowed": [24, 72]}
    _make_board("distinct", spec)
    with kb.connect(board="distinct") as conn:
        for i in range(4):
            _emit(conn, board="distinct", value=24, reward_value=1.0, ts=3_000 + i,
                  dedupe_key=f"reply:route1:fp{i}")
        assert _total_n(conn, "distinct", spec) == 4


def test_terminal_after_intermediate_counts_once(fresh_home):
    # Intermediate replies are distinct logical outcomes; the loop's terminal
    # resolution is ONE outcome even if the terminal tick fires repeatedly.
    spec = {"default": 72, "allowed": [24, 72]}
    _make_board("terminal", spec)
    with kb.connect(board="terminal") as conn:
        _emit(conn, board="terminal", value=48, reward_value=1.0, ts=4_000,
              dedupe_key="reply:r:fp1")
        _emit(conn, board="terminal", value=48, reward_value=1.0, ts=4_001,
              dedupe_key="reply:r:fp2")
        # Terminal outcome re-emitted 3x on repeated ticks -> counts once.
        for i in range(3):
            _emit(conn, board="terminal", value=48, reward_value=0.0, ts=4_010 + i,
                  dedupe_key="loop_terminal:7")
        # 2 distinct replies + 1 terminal == 3 logical outcomes.
        assert _total_n(conn, "terminal", spec) == 3


def test_idempotent_posterior_matches_single_application(fresh_home):
    # The posterior after N replays must equal the posterior after 1 application.
    spec = {"default": 72, "allowed": [24, 72]}
    _make_board("once", spec)
    _make_board("many", spec)
    with kb.connect(board="once") as c1:
        _emit(c1, board="once", value=24, reward_value=1.0, ts=10, dedupe_key="k1")
        fam1, ev1 = opt.read_knob_outcome_evidence(c1, board="once", knob=KNOB, spec=spec)
    with kb.connect(board="many") as c2:
        for i in range(8):
            _emit(c2, board="many", value=24, reward_value=1.0, ts=10 + i, dedupe_key="k1")
        fam2, ev2 = opt.read_knob_outcome_evidence(c2, board="many", knob=KNOB, spec=spec)
    assert fam1 == fam2
    assert {k: v.n for k, v in ev1.items()} == {k: v.n for k, v in ev2.items()}
    assert {k: v.success for k, v in ev1.items()} == {k: v.success for k, v in ev2.items()}
