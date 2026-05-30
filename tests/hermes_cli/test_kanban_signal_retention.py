"""Hardening Pass 2 -- WS4: signal / audit retention (operational hygiene).

board_signals and board_knob_audit are otherwise unbounded. Retention prunes
rows beyond a horizon (and a max-row cap) WITHOUT corrupting the learner:

* learning-relevant 'outcome' rows older than the horizon are rolled up into
  board_signal_rollup (additive sufficient stats) and only then deleted -- the
  posterior is identical before and after a prune (no learning regression);
* non-learning telemetry and knob-audit rows beyond the horizon (and beyond the
  max-row cap) are pruned outright.

Policy (defaults, env-overridable):
  HERMES_KANBAN_SIGNAL_RETENTION_SECONDS  90 days
  HERMES_KANBAN_SIGNAL_MAX_TELEMETRY_ROWS 50_000 / board
  HERMES_KANBAN_KNOB_AUDIT_MAX_ROWS       10_000 / board
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
DAY = 24 * 3600


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
        "objective": {"statement": "Optimize cadence"},
        "workflow": {"id": "f", "stages": [{"key": "execute", "actions": [{"key": "nudge"}]}]},
        "tunables": {KNOB: spec},
    }


def _make_board(slug: str, spec: dict) -> None:
    kb.create_board(slug)
    kb.write_board_metadata(slug, business_contract=_contract(spec))


def _emit_outcome(conn, *, board, value, reward_value, ts, dedupe_key=None, kind="conversion"):
    with kb.write_txn(conn):
        kb.record_board_signal(
            conn, board=board, primitive_kind="outcome", primitive_key="reply",
            knob_snapshot={KNOB: value}, reward_value=reward_value, reward_kind=kind,
            realized_at=ts, ts=ts, dedupe_key=dedupe_key,
        )


def _emit_telemetry(conn, *, board, ts):
    with kb.write_txn(conn):
        kb.record_board_signal(
            conn, board=board, primitive_kind="knob_action", primitive_key="nudge",
            knob_snapshot={KNOB: 24}, ts=ts,
        )


def _evidence(conn, board, spec):
    family, ev = opt.read_knob_outcome_evidence(conn, board=board, knob=KNOB, spec=spec)
    return family, {k: (v.n, v.success) for k, v in ev.items()}


# ---------------------------------------------------------------------------
# Posteriors are preserved exactly across a prune (rollup of outcome rows).
# ---------------------------------------------------------------------------


def test_outcome_prune_preserves_posterior(fresh_home):
    spec = {"default": 72, "allowed": [24, 48, 72]}
    _make_board("ret", spec)
    now = 1_000_000_000
    old_ts = now - 100 * DAY   # beyond the 90-day horizon -> rolled up + pruned
    recent_ts = now - 1 * DAY  # inside horizon -> stays live
    with kb.connect(board="ret") as conn:
        # Old outcomes (mixed arms / rewards).
        for i in range(10):
            _emit_outcome(conn, board="ret", value=24, reward_value=1.0 if i < 7 else 0.0,
                          ts=old_ts + i, dedupe_key=f"old24:{i}")
        for i in range(8):
            _emit_outcome(conn, board="ret", value=48, reward_value=1.0 if i < 3 else 0.0,
                          ts=old_ts + 100 + i, dedupe_key=f"old48:{i}")
        # Recent outcomes (kept live).
        for i in range(6):
            _emit_outcome(conn, board="ret", value=24, reward_value=1.0 if i < 4 else 0.0,
                          ts=recent_ts + i, dedupe_key=f"new24:{i}")

        fam_before, ev_before = _evidence(conn, "ret", spec)
        n_rows_before = conn.execute(
            "SELECT COUNT(*) FROM board_signals WHERE board='ret' AND primitive_kind='outcome'"
        ).fetchone()[0]
        assert n_rows_before == 24

        stats = kb.prune_board_retention(conn, board="ret", now=now)
        assert stats["outcomes_rolled_up"] == 18  # the 10 + 8 old rows

        # Raw old rows are gone; recent rows remain.
        n_rows_after = conn.execute(
            "SELECT COUNT(*) FROM board_signals WHERE board='ret' AND primitive_kind='outcome'"
        ).fetchone()[0]
        assert n_rows_after == 6
        rollup_rows = conn.execute(
            "SELECT COUNT(*) FROM board_signal_rollup WHERE board='ret'"
        ).fetchone()[0]
        assert rollup_rows >= 1

        # The learner's evidence (live rows + rollups) is byte-identical.
        fam_after, ev_after = _evidence(conn, "ret", spec)
        assert fam_after == fam_before
        assert ev_after == ev_before


def test_repeated_prune_is_idempotent(fresh_home):
    spec = {"default": 72, "allowed": [24, 72]}
    _make_board("ret2", spec)
    now = 2_000_000_000
    old_ts = now - 200 * DAY
    with kb.connect(board="ret2") as conn:
        for i in range(12):
            _emit_outcome(conn, board="ret2", value=24, reward_value=1.0 if i % 2 else 0.0,
                          ts=old_ts + i, dedupe_key=f"o:{i}")
        _fam_b, ev_b = _evidence(conn, "ret2", spec)
        kb.prune_board_retention(conn, board="ret2", now=now)
        # A second prune finds nothing new to roll up.
        stats2 = kb.prune_board_retention(conn, board="ret2", now=now)
        assert stats2["outcomes_rolled_up"] == 0
        _fam_a, ev_a = _evidence(conn, "ret2", spec)
        assert ev_a == ev_b


# ---------------------------------------------------------------------------
# Telemetry + knob-audit rows are pruned beyond the horizon / max-row cap.
# ---------------------------------------------------------------------------


def test_telemetry_pruned_by_horizon(fresh_home):
    spec = {"default": 72, "allowed": [24, 72]}
    _make_board("tel", spec)
    now = 3_000_000_000
    with kb.connect(board="tel") as conn:
        for i in range(5):
            _emit_telemetry(conn, board="tel", ts=now - 120 * DAY + i)  # old
        for i in range(3):
            _emit_telemetry(conn, board="tel", ts=now - 2 * DAY + i)    # recent
        stats = kb.prune_board_retention(conn, board="tel", now=now)
        assert stats["telemetry_pruned"] == 5
        remaining = conn.execute(
            "SELECT COUNT(*) FROM board_signals WHERE board='tel' AND primitive_kind!='outcome'"
        ).fetchone()[0]
        assert remaining == 3


def test_telemetry_max_row_cap(fresh_home):
    spec = {"default": 72, "allowed": [24, 72]}
    _make_board("cap", spec)
    now = 4_000_000_000
    with kb.connect(board="cap") as conn:
        # All recent (inside horizon) but exceeding a tiny max-row cap.
        for i in range(20):
            _emit_telemetry(conn, board="cap", ts=now - 3600 + i)
        stats = kb.prune_board_retention(conn, board="cap", now=now, max_telemetry_rows=8)
        assert stats["telemetry_pruned"] == 12
        remaining = conn.execute(
            "SELECT COUNT(*) FROM board_signals WHERE board='cap' AND primitive_kind!='outcome'"
        ).fetchone()[0]
        assert remaining == 8


def test_knob_audit_pruned(fresh_home):
    spec = {"default": 72, "allowed": [24, 72]}
    _make_board("aud", spec)
    now = 5_000_000_000
    with kb.connect(board="aud") as conn:
        with kb.write_txn(conn):
            for i in range(6):
                ts = now - 200 * DAY + i  # old
                conn.execute(
                    "INSERT INTO board_knob_audit (board, ts, knob, old_value, new_value, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("aud", ts, KNOB, "24", "48", "applied"),
                )
            for i in range(2):
                ts = now - 1 * DAY + i  # recent
                conn.execute(
                    "INSERT INTO board_knob_audit (board, ts, knob, old_value, new_value, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("aud", ts, KNOB, "48", "72", "applied"),
                )
        stats = kb.prune_board_retention(conn, board="aud", now=now)
        assert stats["audit_pruned"] == 6
        remaining = conn.execute(
            "SELECT COUNT(*) FROM board_knob_audit WHERE board='aud'"
        ).fetchone()[0]
        assert remaining == 2


def test_optimizer_tick_runs_retention(fresh_home):
    # Retention runs opportunistically from optimizer_tick (best-effort).
    spec = {"default": 72, "allowed": [24, 72]}
    _make_board("tick", spec)
    now = 6_000_000_000
    with kb.connect(board="tick") as conn:
        for i in range(20):
            _emit_telemetry(conn, board="tick", ts=now - 200 * DAY + i)
        result = kb.optimizer_tick(conn, board="tick", now=now)
        assert "retention" in result
        assert result["retention"]["telemetry_pruned"] == 20
