"""G5 hardening tests for the optimizer's two autonomous-action safety gates.

A) REAL KILL-SWITCH (G5-A): ``runtime.optimizer_policy.disabled`` must make
   ``optimizer_tick`` short-circuit with ZERO knob writes -- it used to be honored
   ONLY by the contract validator, so a "disabled" board was still tuned (an
   autonomous-action fail-open). A board with the policy absent / disabled=false
   must behave exactly as before.

B) CANARY DEFAULT-ON (G5-B): auto-revert is now a default-ON safety net so a
   regressive autonomous knob write to a live contract is always rollback-able.
   It resolves with precedence ``arg > env HERMES_OPTIMIZER_AUTO_REVERT >
   config.yaml kanban.optimizer_auto_revert > default-True``.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirror tests/hermes_cli/test_kanban_optimizer.py)
# ---------------------------------------------------------------------------


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
        kb.OPTIMIZER_AUTO_REVERT_ENV,
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


KNOB = "follow_up_interval_hours"


def _contract(spec: dict, *, optimizer_policy: dict | None = None) -> dict:
    runtime: dict = {"mode": "company", "dispatcher": {"profile": "mock-ceo"}}
    if optimizer_policy is not None:
        runtime["optimizer_policy"] = optimizer_policy
    return {
        "objective": {
            "statement": "Optimize seller follow-up cadence",
            "success": ["sellers convert"],
            "failure": ["loops run forever"],
            "constraints": ["no real outbound"],
        },
        "runtime": runtime,
        "workflow": {
            "id": "opt-flow",
            "stages": [{"key": "execute", "actions": [{"key": "nudge"}]}],
        },
        "tunables": {KNOB: spec},
    }


def _make_board(slug: str, spec: dict, *, optimizer_policy: dict | None = None) -> None:
    kb.create_board(slug)
    kb.write_board_metadata(
        slug, business_contract=_contract(spec, optimizer_policy=optimizer_policy)
    )


def _emit_outcome(conn, *, board, knob, value, reward_value, ts):
    with kb.write_txn(conn):
        kb.record_board_signal(
            conn,
            board=board,
            primitive_kind="outcome",
            primitive_key="seller_follow_up",
            knob_snapshot={knob: value},
            reward_value=reward_value,
            reward_kind="conversion",
            realized_at=ts,
            ts=ts,
        )


def _seed_strong_evidence_for_48(conn, slug, base):
    """30x: value 48 always converts, 72 never -- overwhelming evidence to move."""
    for i in range(30):
        _emit_outcome(conn, board=slug, knob=KNOB, value=48, reward_value=1.0, ts=base + i)
        _emit_outcome(conn, board=slug, knob=KNOB, value=72, reward_value=0.0, ts=base + i)


def _knob_audit_rows(conn, slug):
    return conn.execute(
        "SELECT knob, status, old_value, new_value FROM board_knob_audit WHERE board = ?",
        (slug,),
    ).fetchall()


# ---------------------------------------------------------------------------
# A) Real kill-switch: optimizer_policy.disabled => ZERO knob writes.
# ---------------------------------------------------------------------------


def test_disabled_policy_short_circuits_with_zero_knob_writes(fresh_home):
    """G5-A: optimizer_policy.disabled=true => no propose/apply, no audit rows."""
    slug = "ks-disabled"
    _make_board(
        slug,
        {"default": 72, "allowed": [48, 72]},
        optimizer_policy={"disabled": True, "approved_by": "owner", "reason": "paused"},
    )
    base = 2_000_000
    with kb.connect(board=slug) as conn:
        # Overwhelming evidence that WOULD apply 48 if the optimizer ran.
        _seed_strong_evidence_for_48(conn, slug, base)
        res = kb.optimizer_tick(conn, board=slug, now=base + 100, rng=random.Random(0))

        # Short-circuited with the disabled-skip and did nothing else.
        assert res["applied"] == []
        assert res["evaluated"] == []
        assert res["gated"] == []
        assert res["reverted"] == []
        assert res["skipped"] == [{"knob": None, "reason": "optimizer_disabled"}]

        # ZERO knob writes (the strongest assertion): no audit rows at all.
        assert _knob_audit_rows(conn, slug) == []

        # The contract default was NOT moved.
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        assert contract["tunables"][KNOB]["default"] == 72


def test_disabled_via_mode_off_also_short_circuits(fresh_home):
    """The disabled-state resolver also honors mode/status 'off' -- same skip."""
    slug = "ks-mode-off"
    _make_board(
        slug,
        {"default": 72, "allowed": [48, 72]},
        optimizer_policy={"mode": "off", "approved_by": "owner", "reason": "paused"},
    )
    base = 2_500_000
    with kb.connect(board=slug) as conn:
        _seed_strong_evidence_for_48(conn, slug, base)
        res = kb.optimizer_tick(conn, board=slug, now=base + 100, rng=random.Random(0))
        assert res["skipped"] == [{"knob": None, "reason": "optimizer_disabled"}]
        assert _knob_audit_rows(conn, slug) == []


def test_disabled_killswitch_also_blocks_canary_revert(fresh_home):
    """A disabled board must not even auto-revert -- the kill-switch is total."""
    slug = "ks-disabled-canary"
    _make_board(
        slug,
        {"default": 72, "allowed": [48, 72]},
        optimizer_policy={"disabled": True, "approved_by": "owner", "reason": "paused"},
    )
    base = 3_000_000
    hold = kb.OPTIMIZER_CANARY_HOLD_SECONDS
    with kb.connect(board=slug) as conn:
        # A prior optimizer-applied change that regressed (would be reverted).
        for i in range(6):
            _emit_outcome(conn, board=slug, knob=KNOB, value=72, reward_value=1.0, ts=base + i)
        kb.apply_knob_update(
            conn, board=slug, knob=KNOB, new_value=48, old_value=72,
            reason="optimizer:thompson", actor="optimizer", now=base + 10,
        )
        for i in range(6):
            _emit_outcome(conn, board=slug, knob=KNOB, value=48, reward_value=0.0, ts=base + 20 + i)
        before = len(_knob_audit_rows(conn, slug))
        # Even with auto_revert forced ON, a disabled board reverts nothing.
        res = kb.optimizer_tick(
            conn, board=slug, now=base + hold + 100, rng=random.Random(0),
            auto_revert=True,
        )
        assert res["reverted"] == []
        assert res["skipped"] == [{"knob": None, "reason": "optimizer_disabled"}]
        # No NEW audit rows (no revert was written).
        assert len(_knob_audit_rows(conn, slug)) == before


# ---------------------------------------------------------------------------
# A') Control: policy absent / disabled=false => unchanged (applies as before).
# ---------------------------------------------------------------------------


def test_policy_absent_applies_as_before(fresh_home):
    """No optimizer_policy => the optimizer tunes exactly as it did pre-G5-A."""
    slug = "ks-absent"
    _make_board(slug, {"default": 72, "allowed": [48, 72]})  # no policy
    base = 4_000_000
    with kb.connect(board=slug) as conn:
        _seed_strong_evidence_for_48(conn, slug, base)
        res = kb.optimizer_tick(conn, board=slug, now=base + 100, rng=random.Random(0))
        assert res["applied"], res
        assert res["applied"][0]["new_value"] == 48
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        assert contract["tunables"][KNOB]["default"] == 48


def test_policy_disabled_false_applies_as_before(fresh_home):
    """optimizer_policy.disabled=false => the optimizer still tunes normally."""
    slug = "ks-false"
    _make_board(
        slug, {"default": 72, "allowed": [48, 72]},
        optimizer_policy={"disabled": False},
    )
    base = 5_000_000
    with kb.connect(board=slug) as conn:
        _seed_strong_evidence_for_48(conn, slug, base)
        res = kb.optimizer_tick(conn, board=slug, now=base + 100, rng=random.Random(0))
        assert res["applied"], res
        assert res["applied"][0]["new_value"] == 48
        # No disabled-skip was recorded.
        assert all(s.get("reason") != "optimizer_disabled" for s in res["skipped"])


# ---------------------------------------------------------------------------
# B) Auto-revert resolution: arg > env > config > default-True.
# ---------------------------------------------------------------------------


def test_auto_revert_defaults_on_when_unset(fresh_home):
    """G5-B: with neither arg, env, nor config set, auto-revert resolves ON."""
    assert kb._auto_revert_enabled(None) is True


def test_auto_revert_explicit_arg_wins(fresh_home, monkeypatch):
    """The explicit arg beats everything (env says on, arg says off => off)."""
    monkeypatch.setenv(kb.OPTIMIZER_AUTO_REVERT_ENV, "1")
    assert kb._auto_revert_enabled(False) is False
    assert kb._auto_revert_enabled(True) is True


def test_auto_revert_env_false_overrides_default_on(fresh_home, monkeypatch):
    """HERMES_OPTIMIZER_AUTO_REVERT=false overrides the default-ON to off."""
    monkeypatch.setenv(kb.OPTIMIZER_AUTO_REVERT_ENV, "false")
    assert kb._auto_revert_enabled(None) is False
    monkeypatch.setenv(kb.OPTIMIZER_AUTO_REVERT_ENV, "0")
    assert kb._auto_revert_enabled(None) is False


def test_auto_revert_env_true_keeps_on(fresh_home, monkeypatch):
    monkeypatch.setenv(kb.OPTIMIZER_AUTO_REVERT_ENV, "on")
    assert kb._auto_revert_enabled(None) is True


def _write_config_yaml(home: Path, optimizer_auto_revert) -> None:
    import yaml

    (home / "config.yaml").write_text(
        yaml.safe_dump({"kanban": {"optimizer_auto_revert": optimizer_auto_revert}}),
        encoding="utf-8",
    )


def test_auto_revert_config_false_overrides_default_on(fresh_home):
    """config.yaml kanban.optimizer_auto_revert=false overrides the default-ON."""
    _write_config_yaml(fresh_home, False)
    assert kb._auto_revert_enabled(None) is False


def test_auto_revert_config_true_keeps_on(fresh_home):
    _write_config_yaml(fresh_home, True)
    assert kb._auto_revert_enabled(None) is True


def test_auto_revert_env_beats_config(fresh_home, monkeypatch):
    """Env wins over config: config says off, env says on => on."""
    _write_config_yaml(fresh_home, False)
    monkeypatch.setenv(kb.OPTIMIZER_AUTO_REVERT_ENV, "1")
    assert kb._auto_revert_enabled(None) is True
    # And the reverse: config says on, env says off => off.
    _write_config_yaml(fresh_home, True)
    monkeypatch.setenv(kb.OPTIMIZER_AUTO_REVERT_ENV, "0")
    assert kb._auto_revert_enabled(None) is False


def test_default_on_canary_reverts_regression_without_flag(fresh_home):
    """End-to-end: optimizer_tick (no auto_revert arg) now reverts a regression
    because the default is ON -- the pre-G5-B no-op default is gone."""
    slug = "ks-default-revert"
    _make_board(slug, {"default": 72, "allowed": [48, 72]})
    base = 6_000_000
    hold = kb.OPTIMIZER_CANARY_HOLD_SECONDS
    with kb.connect(board=slug) as conn:
        # Strong pre-change baseline at 72.
        for i in range(6):
            _emit_outcome(conn, board=slug, knob=KNOB, value=72, reward_value=1.0, ts=base + i)
        # Optimizer applied a change to 48 that then regressed.
        kb.apply_knob_update(
            conn, board=slug, knob=KNOB, new_value=48, old_value=72,
            reason="optimizer:thompson", actor="optimizer", now=base + 10,
        )
        for i in range(6):
            _emit_outcome(conn, board=slug, knob=KNOB, value=48, reward_value=0.0, ts=base + 20 + i)
        # No auto_revert arg => resolves to the default-ON => the canary fires.
        res = kb.optimizer_tick(
            conn, board=slug, now=base + hold + 100, rng=random.Random(0),
        )
        assert len(res["reverted"]) == 1, res
        assert res["reverted"][0]["new_value"] == 72
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        assert contract["tunables"][KNOB]["default"] == 72
