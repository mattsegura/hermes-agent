"""P2 optimizer tests: the closed learning loop over one bounded knob.

Covers (per the P2 spec):

* Pure posterior math (Beta-Bernoulli + Gaussian) with no DB.
* Reward normalization + the bounded action space (arms from range/allowed).
* A replay where one knob value clearly outperforms -> the learner converges
  to the better arm (both a pure-evidence replay and a DB-signal replay).
* The minimum-evidence floor: too few outcomes -> the knob is left unchanged.
* Bounded autonomy: an in-range update is applied autonomously and emits a
  ``knob_action`` signal + audit row; an out-of-range / unknown-knob proposal
  is NOT applied and instead records an approval gate.
* The scheduled ``optimizer_tick`` runs end-to-end and self-throttles.
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
from hermes_cli import kanban_optimizer as opt


# ---------------------------------------------------------------------------
# Fixtures / helpers
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


def _contract_with_knob(spec: dict) -> dict:
    return {
        "objective": {
            "statement": "Optimize seller follow-up cadence",
            "success": ["sellers convert"],
            "failure": ["loops run forever"],
            "constraints": ["no real outbound"],
        },
        "runtime": {"mode": "company", "dispatcher": {"profile": "mock-ceo"}},
        "workflow": {
            "id": "opt-flow",
            "stages": [{"key": "execute", "actions": [{"key": "nudge"}]}],
        },
        "tunables": {KNOB: spec},
    }


def _make_board(slug: str, spec: dict) -> None:
    kb.create_board(slug)
    kb.write_board_metadata(slug, business_contract=_contract_with_knob(spec))


def _emit_outcome(conn, *, board, knob, value, reward_kind, reward_value, ts):
    with kb.write_txn(conn):
        kb.record_board_signal(
            conn,
            board=board,
            primitive_kind="outcome",
            primitive_key="seller_follow_up",
            knob_snapshot={knob: value},
            reward_value=reward_value,
            reward_kind=reward_kind,
            realized_at=ts,
            ts=ts,
        )


def _binary_evidence(value, *, family="binary", successes, failures):
    ev = opt.ArmEvidence(value=value, family=family)
    for _ in range(successes):
        ev.observe(1.0)
    for _ in range(failures):
        ev.observe(0.0)
    return ev


# ---------------------------------------------------------------------------
# 1. Pure posterior math (no DB)
# ---------------------------------------------------------------------------


def test_beta_bernoulli_posterior_update_is_correct():
    ev = _binary_evidence(48, successes=7, failures=3)
    post = opt.BetaBernoulliPosterior.from_evidence(ev)
    # Beta(1,1) prior + 7 successes + 3 failures.
    assert post.alpha == pytest.approx(8.0)
    assert post.beta == pytest.approx(4.0)
    assert post.mean == pytest.approx(8.0 / 12.0)


def test_beta_bernoulli_empty_evidence_is_uniform_prior():
    post = opt.BetaBernoulliPosterior.from_evidence(None)
    assert post.alpha == pytest.approx(1.0)
    assert post.beta == pytest.approx(1.0)
    assert post.mean == pytest.approx(0.5)


def test_gaussian_posterior_shrinks_toward_sample_mean_and_tightens():
    ev = opt.ArmEvidence(value=48, family="continuous")
    for _ in range(10):
        ev.observe(-100.0)  # utility = -time_to_done; all 100s
    post = opt.GaussianPosterior.from_evidence(ev, obs_variance=25.0)
    # Posterior mean is pulled essentially onto the sample mean (-100) given a
    # weak prior, and its variance is far smaller than the prior variance.
    assert post.mean == pytest.approx(-100.0, abs=1e-3)
    assert post.variance < opt.PRIOR_GAUSSIAN_VARIANCE
    assert post.variance == pytest.approx(25.0 / 10.0, rel=1e-3)


def test_gaussian_posterior_no_evidence_is_prior():
    post = opt.GaussianPosterior.from_evidence(None)
    assert post.mean == pytest.approx(opt.PRIOR_GAUSSIAN_MEAN)
    assert post.variance == pytest.approx(opt.PRIOR_GAUSSIAN_VARIANCE)


def test_reward_utility_normalizes_direction():
    # Binary: collapse to 1/0.
    assert opt.reward_utility("conversion", 1.0) == 1.0
    assert opt.reward_utility("reply", 1.0) == 1.0
    assert opt.reward_utility("loop_closed", 0.0) == 0.0
    # Continuous (lower is better): negate so higher == better.
    assert opt.reward_utility("time_to_done", 100.0) == -100.0
    assert opt.reward_utility("cost", 5.0) == -5.0
    # Unknown reward kinds are ignored.
    assert opt.reward_utility("mystery", 1.0) is None
    assert opt.reward_utility("conversion", None) is None


# ---------------------------------------------------------------------------
# 2. Bounded action space (arms from range / allowed)
# ---------------------------------------------------------------------------


def test_candidate_arms_from_allowed_set():
    spec = {"default": 72, "allowed": [24, 48, 72]}
    assert opt.candidate_arm_values(spec) == [24, 48, 72]


def test_candidate_arms_discretize_range_and_include_current():
    spec = {"default": 50, "range": [24, 120]}
    arms = opt.candidate_arm_values(spec, include=50)
    assert len(arms) <= opt.MAX_RANGE_ARMS + 1
    assert min(arms) == 24 and max(arms) == 120
    assert 50 in arms  # current value is always representable


def test_knob_value_in_bounds():
    assert opt.knob_value_in_bounds({"allowed": [24, 48, 72]}, 48) is True
    assert opt.knob_value_in_bounds({"allowed": [24, 48, 72]}, 96) is False
    assert opt.knob_value_in_bounds({"range": [24, 120]}, 24) is True
    assert opt.knob_value_in_bounds({"range": [24, 120]}, 120) is True
    assert opt.knob_value_in_bounds({"range": [24, 120]}, 200) is False
    # An unbounded knob (no range/allowed) is NEVER safe for autonomy.
    assert opt.knob_value_in_bounds({"default": 5}, 5) is False


# ---------------------------------------------------------------------------
# 3. Replay convergence (pure evidence) + minimum-evidence floor
# ---------------------------------------------------------------------------


def test_propose_converges_to_better_arm_pure():
    spec = {"default": 72, "allowed": [24, 48, 72]}
    # Arm 48 clearly outperforms; arm 72 clearly underperforms; arm 24 unseen.
    evidence = {
        opt._arm_key(48): _binary_evidence(48, successes=18, failures=2),
        opt._arm_key(72): _binary_evidence(72, successes=2, failures=18),
    }
    # Deterministic exploit view: 48 has the highest posterior mean.
    means = opt.posterior_means(spec, "binary", evidence, current_value=72)
    assert means[48] > means[72]
    assert means[48] == max(means.values())

    # Thompson sampling converges on 48 across many seeded draws.
    counts = {24: 0, 48: 0, 72: 0}
    rng = random.Random(1234)
    for _ in range(300):
        proposal = opt.propose_from_evidence(
            spec, "binary", evidence, 72, knob=KNOB, rng=rng
        )
        counts[proposal.proposed_value] += 1
    assert counts[48] == max(counts.values())
    assert counts[48] > counts[72]
    assert counts[48] / 300 > 0.6


def test_minimum_evidence_floor_leaves_knob_unchanged():
    spec = {"default": 72, "allowed": [24, 48, 72]}
    # Only 2 outcomes total -- below the K floor -> no autonomous change.
    evidence = {
        opt._arm_key(48): _binary_evidence(48, successes=2, failures=0),
    }
    assert sum(e.n for e in evidence.values()) < opt.MIN_EVIDENCE_OUTCOMES
    rng = random.Random(7)
    proposal = opt.propose_from_evidence(spec, "binary", evidence, 72, knob=KNOB, rng=rng)
    assert proposal.changed is False
    assert proposal.proposed_value == 72
    assert proposal.reason == "insufficient_evidence"


def test_aggregate_prefers_binary_family_and_snaps_values():
    candidates = [24, 48, 72]
    observations = [
        (48, "conversion", 1.0),
        (48, "conversion", 0.0),
        (47, "reply", 1.0),         # snaps to 48
        (72, "time_to_done", 100),  # continuous ignored when binary present
    ]
    family, by_arm = opt.aggregate_arm_evidence(observations, candidates)
    assert family == "binary"
    assert by_arm[opt._arm_key(48)].n == 3
    assert opt._arm_key(72) not in by_arm  # the continuous row was dropped


# ---------------------------------------------------------------------------
# 4. DB-signal replay: read board_signals -> converge to the better arm
# ---------------------------------------------------------------------------


def test_db_replay_proposes_better_arm(fresh_home):
    slug = "opt-replay"
    _make_board(slug, {"default": 72, "allowed": [48, 72]})
    base = 1_000_000
    with kb.connect(board=slug) as conn:
        for i in range(30):
            _emit_outcome(
                conn, board=slug, knob=KNOB, value=48,
                reward_kind="conversion", reward_value=1.0, ts=base + i,
            )
            _emit_outcome(
                conn, board=slug, knob=KNOB, value=72,
                reward_kind="conversion", reward_value=0.0, ts=base + i,
            )
        family, evidence = opt.read_knob_outcome_evidence(
            conn, board=slug, knob=KNOB,
            spec={"default": 72, "allowed": [48, 72]},
        )
        assert family == "binary"
        assert evidence[opt._arm_key(48)].n == 30
        assert evidence[opt._arm_key(72)].n == 30

        proposal = opt.propose_knob_value(
            conn, board=slug, knob=KNOB, rng=random.Random(0)
        )
        assert proposal.proposed_value == 48
        assert proposal.changed is True


# ---------------------------------------------------------------------------
# 5. Bounded autonomy: in-range autonomous apply
# ---------------------------------------------------------------------------


def test_in_range_update_applied_autonomously_with_signal_and_audit(fresh_home):
    slug = "opt-apply"
    _make_board(slug, {"default": 72, "allowed": [24, 48, 72]})
    with kb.connect(board=slug) as conn:
        result = kb.apply_knob_update(
            conn, board=slug, knob=KNOB, new_value=48, reason="test",
        )
        assert result["applied"] is True
        assert result["status"] == "applied"
        assert result["new_value"] == 48

        # The contract default was actually moved.
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        assert contract["tunables"][KNOB]["default"] == 48

        # A knob_action signal was emitted.
        rows = conn.execute(
            "SELECT primitive_key, action FROM board_signals "
            "WHERE board = ? AND primitive_kind = 'knob_action'",
            (slug,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == KNOB
        assert '"knob_update"' in rows[0][1]

        # An 'applied' audit row was recorded.
        audit = conn.execute(
            "SELECT knob, status, old_value, new_value FROM board_knob_audit "
            "WHERE board = ?",
            (slug,),
        ).fetchall()
        assert len(audit) == 1
        assert audit[0][1] == "applied"
        assert audit[0][2] == "72" and audit[0][3] == "48"


def test_apply_same_value_is_noop(fresh_home):
    slug = "opt-noop"
    _make_board(slug, {"default": 72, "allowed": [24, 48, 72]})
    with kb.connect(board=slug) as conn:
        result = kb.apply_knob_update(conn, board=slug, knob=KNOB, new_value=72)
        assert result["applied"] is False
        assert result["status"] == "noop"


# ---------------------------------------------------------------------------
# 6. Bounded autonomy: out-of-range / unknown knob -> approval gate
# ---------------------------------------------------------------------------


def test_out_of_range_update_routes_to_approval_gate(fresh_home):
    slug = "opt-oor"
    _make_board(slug, {"default": 72, "range": [24, 120]})
    with kb.connect(board=slug) as conn:
        result = kb.apply_knob_update(conn, board=slug, knob=KNOB, new_value=999)
        assert result["applied"] is False
        assert result["status"] == "approval_required"
        assert result["approval_gate"] == f"knob_update:{KNOB}"

        # The contract default was NOT changed.
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        assert contract["tunables"][KNOB]["default"] == 72

        # An approval_required audit row exists...
        audit = conn.execute(
            "SELECT status FROM board_knob_audit WHERE board = ?", (slug,)
        ).fetchall()
        assert [r[0] for r in audit] == ["approval_required"]

        # ...recorded as a knob_action *request* (kind approval_required), NOT
        # an 'approval' grant -- so it does not auto-satisfy the gate.
        kinds = conn.execute(
            "SELECT primitive_kind FROM board_signals WHERE board = ?", (slug,)
        ).fetchall()
        kind_set = {r[0] for r in kinds}
        assert "knob_action" in kind_set
        assert "approval" not in kind_set
        action = conn.execute(
            "SELECT action FROM board_signals "
            "WHERE board = ? AND primitive_kind = 'knob_action'",
            (slug,),
        ).fetchone()[0]
        assert '"approval_required"' in action


def test_unknown_knob_routes_to_approval_gate(fresh_home):
    slug = "opt-unknown"
    _make_board(slug, {"default": 72, "allowed": [24, 48, 72]})
    with kb.connect(board=slug) as conn:
        result = kb.apply_knob_update(
            conn, board=slug, knob="totally_new_knob", new_value=5,
        )
        assert result["applied"] is False
        assert result["status"] == "approval_required"
        assert result["reason"] == "unknown_knob"


# ---------------------------------------------------------------------------
# 7. Scheduled optimizer_tick: end-to-end + throttle
# ---------------------------------------------------------------------------


def test_optimizer_tick_applies_change_then_throttles(fresh_home):
    slug = "opt-tick"
    _make_board(slug, {"default": 72, "allowed": [48, 72]})
    base = 2_000_000
    with kb.connect(board=slug) as conn:
        for i in range(30):
            _emit_outcome(
                conn, board=slug, knob=KNOB, value=48,
                reward_kind="conversion", reward_value=1.0, ts=base + i,
            )
            _emit_outcome(
                conn, board=slug, knob=KNOB, value=72,
                reward_kind="conversion", reward_value=0.0, ts=base + i,
            )
        # First tick: overwhelming evidence -> applies the better arm (48).
        res = kb.optimizer_tick(
            conn, board=slug, now=base + 100, rng=random.Random(0)
        )
        assert res["applied"], res
        assert res["applied"][0]["new_value"] == 48
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        assert contract["tunables"][KNOB]["default"] == 48

        # Second tick immediately after: throttled (no new outcomes, cooldown
        # not elapsed) -> nothing applied.
        res2 = kb.optimizer_tick(
            conn, board=slug, now=base + 200, rng=random.Random(0)
        )
        assert res2["applied"] == []
        assert any(s.get("reason") == "throttled" for s in res2["skipped"])


def test_optimizer_tick_sparse_data_leaves_knob_unchanged(fresh_home):
    slug = "opt-sparse"
    _make_board(slug, {"default": 72, "allowed": [48, 72]})
    base = 3_000_000
    with kb.connect(board=slug) as conn:
        # Only 2 outcomes -- below the minimum-evidence floor.
        _emit_outcome(
            conn, board=slug, knob=KNOB, value=48,
            reward_kind="conversion", reward_value=1.0, ts=base,
        )
        _emit_outcome(
            conn, board=slug, knob=KNOB, value=48,
            reward_kind="conversion", reward_value=1.0, ts=base + 1,
        )
        res = kb.optimizer_tick(conn, board=slug, now=base + 100, rng=random.Random(0))
        assert res["applied"] == []
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        assert contract["tunables"][KNOB]["default"] == 72


# ---------------------------------------------------------------------------
# 8. E3 multi-knob tick: tune ALL present managed knobs, not just the first.
# ---------------------------------------------------------------------------


KNOB2 = "cadence_hours"


def _contract_with_two_knobs(spec1: dict, spec2: dict) -> dict:
    c = _contract_with_knob(spec1)
    c["tunables"][KNOB2] = spec2
    return c


def test_optimizer_tick_tunes_all_present_managed_knobs(fresh_home):
    slug = "opt-multi"
    kb.create_board(slug)
    kb.write_board_metadata(
        slug,
        business_contract=_contract_with_two_knobs(
            {"default": 72, "allowed": [48, 72]},
            {"default": 12, "allowed": [6, 12]},
        ),
    )
    base = 5_000_000
    with kb.connect(board=slug) as conn:
        # Knob 1: value 48 clearly beats 72.
        for i in range(30):
            _emit_outcome(
                conn, board=slug, knob=KNOB, value=48,
                reward_kind="conversion", reward_value=1.0, ts=base + i,
            )
            _emit_outcome(
                conn, board=slug, knob=KNOB, value=72,
                reward_kind="conversion", reward_value=0.0, ts=base + i,
            )
        # Knob 2: value 6 clearly beats 12.
        for i in range(30):
            _emit_outcome(
                conn, board=slug, knob=KNOB2, value=6,
                reward_kind="conversion", reward_value=1.0, ts=base + i,
            )
            _emit_outcome(
                conn, board=slug, knob=KNOB2, value=12,
                reward_kind="conversion", reward_value=0.0, ts=base + i,
            )
        res = kb.optimizer_tick(conn, board=slug, now=base + 100, rng=random.Random(0))
        # BOTH managed knobs were evaluated and applied in a single tick (the
        # pre-E3 behaviour only touched the first select_managed_knob result).
        evaluated = {e["knob"] for e in res["evaluated"]}
        assert evaluated == {KNOB, KNOB2}, res
        applied = {a["knob"]: a["new_value"] for a in res["applied"]}
        assert applied == {KNOB: 48, KNOB2: 6}, res
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        assert contract["tunables"][KNOB]["default"] == 48
        assert contract["tunables"][KNOB2]["default"] == 6


def test_optimizer_tick_pinned_knob_tunes_only_that_one(fresh_home):
    slug = "opt-multi-pinned"
    kb.create_board(slug)
    kb.write_board_metadata(
        slug,
        business_contract=_contract_with_two_knobs(
            {"default": 72, "allowed": [48, 72]},
            {"default": 12, "allowed": [6, 12]},
        ),
    )
    base = 6_000_000
    with kb.connect(board=slug) as conn:
        for i in range(30):
            _emit_outcome(
                conn, board=slug, knob=KNOB2, value=6,
                reward_kind="conversion", reward_value=1.0, ts=base + i,
            )
            _emit_outcome(
                conn, board=slug, knob=KNOB2, value=12,
                reward_kind="conversion", reward_value=0.0, ts=base + i,
            )
        # Pin to KNOB2 only -> KNOB is never touched (backward-compatible path).
        res = kb.optimizer_tick(
            conn, board=slug, knob=KNOB2, now=base + 100, rng=random.Random(0)
        )
        assert {e["knob"] for e in res["evaluated"]} == {KNOB2}, res
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        assert contract["tunables"][KNOB]["default"] == 72  # untouched
        assert contract["tunables"][KNOB2]["default"] == 6   # tuned


# ---------------------------------------------------------------------------
# 9. E4 canary + auto-revert (default-off).
# ---------------------------------------------------------------------------


def _seed_applied_change(conn, slug, *, knob, old_value, new_value, ts):
    """Apply an in-bounds change so an 'applied' audit row exists to canary."""
    return kb.apply_knob_update(
        conn, board=slug, knob=knob, new_value=new_value,
        old_value=old_value, reason="seed", actor="optimizer", now=ts,
    )


def _setup_regression(conn, slug, base):
    """Strong pre-change baseline, an applied change to 48, then a collapse."""
    for i in range(6):
        _emit_outcome(
            conn, board=slug, knob=KNOB, value=72,
            reward_kind="conversion", reward_value=1.0, ts=base + i,
        )
    _seed_applied_change(conn, slug, knob=KNOB, old_value=72, new_value=48, ts=base + 10)
    for i in range(6):
        _emit_outcome(
            conn, board=slug, knob=KNOB, value=48,
            reward_kind="conversion", reward_value=0.0, ts=base + 20 + i,
        )


def test_canary_reverts_regression_when_enabled(fresh_home):
    """Flag on: a post-change reward collapse is rolled back to last-known-good.

    Tested at the canary boundary (``_optimizer_canary_revert``) so the result
    is independent of the separate Thompson re-tune loop.
    """
    slug = "canary-revert"
    _make_board(slug, {"default": 72, "allowed": [48, 72]})
    base = 8_000_000
    hold = kb.OPTIMIZER_CANARY_HOLD_SECONDS
    with kb.connect(board=slug) as conn:
        _setup_regression(conn, slug, base)
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        rev = kb._optimizer_canary_revert(
            conn, board=slug, knob=KNOB, contract=contract, now=base + hold + 100,
        )
        assert rev is not None
        assert rev["status"] == "reverted"
        assert rev["new_value"] == 72  # back to last-known-good old_value
        assert rev["pre_mean_reward"] == 1.0
        assert rev["post_mean_reward"] == 0.0
        # The contract default was rolled back.
        contract2 = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        assert contract2["tunables"][KNOB]["default"] == 72
        # A 'reverted' audit row exists.
        statuses = [
            r[0] for r in conn.execute(
                "SELECT status FROM board_knob_audit WHERE board = ? AND knob = ? "
                "ORDER BY ts, id", (slug, KNOB),
            ).fetchall()
        ]
        assert "reverted" in statuses


def test_canary_no_op_when_flag_off(fresh_home):
    """Default-off: optimizer_tick does NOT run the canary (no revert path)."""
    slug = "canary-off"
    _make_board(slug, {"default": 72, "allowed": [48, 72]})
    base = 7_000_000
    hold = kb.OPTIMIZER_CANARY_HOLD_SECONDS
    with kb.connect(board=slug) as conn:
        _setup_regression(conn, slug, base)
        # Flag off (default) -> the tick never enters the canary branch.
        res = kb.optimizer_tick(
            conn, board=slug, now=base + hold + 100, rng=random.Random(0),
        )
        assert res.get("reverted") == []


def test_canary_keeps_change_that_improved(fresh_home):
    """Flag on: an applied change that DID improve is kept (canary returns None)."""
    slug = "canary-keep"
    _make_board(slug, {"default": 72, "allowed": [48, 72]})
    base = 9_000_000
    hold = kb.OPTIMIZER_CANARY_HOLD_SECONDS
    with kb.connect(board=slug) as conn:
        # Weak baseline before the change.
        for i in range(6):
            _emit_outcome(
                conn, board=slug, knob=KNOB, value=72,
                reward_kind="conversion", reward_value=0.0, ts=base + i,
            )
        _seed_applied_change(conn, slug, knob=KNOB, old_value=72, new_value=48, ts=base + 10)
        # Post-change reward improved.
        for i in range(6):
            _emit_outcome(
                conn, board=slug, knob=KNOB, value=48,
                reward_kind="conversion", reward_value=1.0, ts=base + 20 + i,
            )
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        rev = kb._optimizer_canary_revert(
            conn, board=slug, knob=KNOB, contract=contract, now=base + hold + 100,
        )
        assert rev is None
        contract2 = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        assert contract2["tunables"][KNOB]["default"] == 48


def test_canary_holds_within_window(fresh_home):
    """The hold window has NOT elapsed -> the canary holds (returns None)."""
    slug = "canary-hold"
    _make_board(slug, {"default": 72, "allowed": [48, 72]})
    base = 10_000_000
    with kb.connect(board=slug) as conn:
        _setup_regression(conn, slug, base)
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        # now is only a little after the change -> still baking.
        rev = kb._optimizer_canary_revert(
            conn, board=slug, knob=KNOB, contract=contract, now=base + 30,
        )
        assert rev is None
        contract2 = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        assert contract2["tunables"][KNOB]["default"] == 48


def test_canary_holds_when_too_few_post_outcomes(fresh_home):
    """Past the hold window but too few post-change outcomes -> keep holding."""
    slug = "canary-thin"
    _make_board(slug, {"default": 72, "allowed": [48, 72]})
    base = 12_000_000
    hold = kb.OPTIMIZER_CANARY_HOLD_SECONDS
    with kb.connect(board=slug) as conn:
        for i in range(6):
            _emit_outcome(
                conn, board=slug, knob=KNOB, value=72,
                reward_kind="conversion", reward_value=1.0, ts=base + i,
            )
        _seed_applied_change(conn, slug, knob=KNOB, old_value=72, new_value=48, ts=base + 10)
        # Only 2 post-change outcomes -- below OPTIMIZER_CANARY_MIN_OUTCOMES.
        for i in range(2):
            _emit_outcome(
                conn, board=slug, knob=KNOB, value=48,
                reward_kind="conversion", reward_value=0.0, ts=base + 20 + i,
            )
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        rev = kb._optimizer_canary_revert(
            conn, board=slug, knob=KNOB, contract=contract, now=base + hold + 100,
        )
        assert rev is None


def test_canary_integrated_revert_appears_in_tick_result(fresh_home):
    """End-to-end: with the flag on, optimizer_tick surfaces the revert."""
    slug = "canary-tick"
    _make_board(slug, {"default": 72, "allowed": [48, 72]})
    base = 13_000_000
    hold = kb.OPTIMIZER_CANARY_HOLD_SECONDS
    with kb.connect(board=slug) as conn:
        _setup_regression(conn, slug, base)
        res = kb.optimizer_tick(
            conn, board=slug, now=base + hold + 100, rng=random.Random(0),
            auto_revert=True,
        )
        assert len(res["reverted"]) == 1, res
        assert res["reverted"][0]["new_value"] == 72
        # A reverted change short-circuits the re-tune for this knob this tick.
        assert all(e["knob"] != KNOB for e in res["evaluated"])
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        assert contract["tunables"][KNOB]["default"] == 72


def test_canary_env_flag_enables_revert(fresh_home, monkeypatch):
    """The HERMES_OPTIMIZER_AUTO_REVERT env var enables the canary."""
    monkeypatch.setenv(kb.OPTIMIZER_AUTO_REVERT_ENV, "1")
    slug = "canary-env"
    _make_board(slug, {"default": 72, "allowed": [48, 72]})
    base = 11_000_000
    hold = kb.OPTIMIZER_CANARY_HOLD_SECONDS
    with kb.connect(board=slug) as conn:
        _setup_regression(conn, slug, base)
        res = kb.optimizer_tick(
            conn, board=slug, now=base + hold + 100, rng=random.Random(0),
        )
        assert len(res["reverted"]) == 1, res
        contract = kb._metadata_as_business_contract(kb.read_board_metadata(slug))
        assert contract["tunables"][KNOB]["default"] == 72
