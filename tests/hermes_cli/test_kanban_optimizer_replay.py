"""P4b optimizer replay / robustness tests.

Extends the P2/P3 optimizer coverage with *noisy, recorded reward streams* --
binary and continuous -- and proves the Thompson-sampling proposer is robust:

* it converges to the better arm as evidence accrues (binary and continuous),
* it NEVER proposes a value outside the declared bounded action space,
* under adversarial / noisy reward below the minimum-evidence floor it does not
  thrash -- it leaves the knob exactly where it is.

The reward streams are recorded both as pure ``(knob_value, reward_kind,
reward_value)`` observation tuples (fed through the pure aggregator) and as real
``board_signals`` rows in a fresh DB, so both the pure learner and the DB-backed
adapter are exercised.
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

KNOB = "follow_up_interval_hours"


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


def _noisy_binary_stream(rng, arm_rates: dict, n_per_arm: int):
    """Yield (arm_value, 'conversion', 0/1) with per-arm Bernoulli success rate."""
    rows = []
    for arm, rate in arm_rates.items():
        for _ in range(n_per_arm):
            rows.append((arm, "conversion", 1.0 if rng.random() < rate else 0.0))
    rng.shuffle(rows)
    return rows


def _noisy_continuous_stream(rng, arm_means: dict, sigma: float, n_per_arm: int):
    """Yield (arm_value, 'time_to_done', value) gaussian around a per-arm mean
    (lower is better, so the smaller-mean arm is the winner)."""
    rows = []
    for arm, mean in arm_means.items():
        for _ in range(n_per_arm):
            rows.append((arm, "time_to_done", max(0.1, rng.gauss(mean, sigma))))
    rng.shuffle(rows)
    return rows


def _modal_proposal(spec, family, evidence, current, *, seeds=200):
    """Return (counts, best_arm) over many seeded Thompson draws."""
    counts: dict = {}
    for seed in range(seeds):
        proposal = opt.propose_from_evidence(
            spec, family, evidence, current, knob=KNOB, rng=random.Random(seed)
        )
        counts[proposal.proposed_value] = counts.get(proposal.proposed_value, 0) + 1
    best = max(counts, key=lambda k: counts[k])
    return counts, best


# ---------------------------------------------------------------------------
# 1. Noisy binary stream -> converges to the higher-reward arm.
# ---------------------------------------------------------------------------


def test_noisy_binary_stream_converges_to_better_arm():
    spec = {"default": 72, "allowed": [24, 48, 72]}
    candidates = opt.candidate_arm_values(spec)
    rng = random.Random(2024)
    # 48 is clearly best, 24 mediocre, 72 poor -- but every signal is noisy.
    rows = _noisy_binary_stream(rng, {24: 0.45, 48: 0.8, 72: 0.2}, n_per_arm=40)
    family, evidence = opt.aggregate_arm_evidence(rows, candidates)
    assert family == "binary"

    means = opt.posterior_means(spec, family, evidence, current_value=72)
    assert means[48] == max(means.values())

    counts, best = _modal_proposal(spec, family, evidence, 72)
    assert best == 48
    assert counts.get(48, 0) / sum(counts.values()) > 0.6


def test_binary_convergence_sharpens_with_more_evidence():
    spec = {"default": 72, "allowed": [48, 72]}
    candidates = opt.candidate_arm_values(spec)
    rates = {48: 0.75, 72: 0.3}

    def frac_best(n_per_arm: int) -> float:
        rng = random.Random(11)
        rows = _noisy_binary_stream(rng, rates, n_per_arm)
        family, evidence = opt.aggregate_arm_evidence(rows, candidates)
        counts, _ = _modal_proposal(spec, family, evidence, 72)
        return counts.get(48, 0) / sum(counts.values())

    sparse = frac_best(5)
    rich = frac_best(60)
    # More evidence -> the proposer commits to the winner more decisively.
    assert rich >= sparse
    assert rich > 0.7


# ---------------------------------------------------------------------------
# 2. Noisy continuous stream (lower-is-better) -> converges to the faster arm.
# ---------------------------------------------------------------------------


def test_noisy_continuous_stream_converges_to_lower_cost_arm():
    spec = {"default": 120, "allowed": [24, 72, 120]}
    candidates = opt.candidate_arm_values(spec)
    rng = random.Random(7)
    # 24h cadence resolves fastest (mean 40), 120h slowest (mean 160).
    rows = _noisy_continuous_stream(
        rng, {24: 40.0, 72: 90.0, 120: 160.0}, sigma=20.0, n_per_arm=40
    )
    family, evidence = opt.aggregate_arm_evidence(rows, candidates)
    assert family == "continuous"

    means = opt.posterior_means(spec, family, evidence, current_value=120)
    # Higher utility == lower time_to_done; 24 should have the best (largest) mean.
    assert means[24] == max(means.values())

    counts, best = _modal_proposal(spec, family, evidence, 120)
    assert best == 24
    assert counts.get(24, 0) / sum(counts.values()) > 0.6


# ---------------------------------------------------------------------------
# 3. Bounded by construction: never proposes out-of-range, across many streams.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", [
    {"default": 72, "allowed": [24, 48, 72]},
    {"default": 50, "range": [24, 120]},
    {"default": 6, "range": [1, 12]},
])
def test_proposal_is_always_within_declared_bounds(spec):
    candidates = opt.candidate_arm_values(spec, include=opt.knob_default(spec))
    for seed in range(60):
        rng = random.Random(seed)
        arms = candidates
        rates = {a: rng.random() for a in arms}
        rows = _noisy_binary_stream(rng, rates, n_per_arm=12)
        family, evidence = opt.aggregate_arm_evidence(rows, candidates)
        proposal = opt.propose_from_evidence(
            spec, family, evidence, opt.knob_default(spec),
            knob=KNOB, rng=rng,
        )
        # The proposed value must always be a declared, in-bounds value.
        assert opt.knob_value_in_bounds(spec, proposal.proposed_value)
        assert any(opt._values_equal(proposal.proposed_value, c) for c in candidates)


# ---------------------------------------------------------------------------
# 4. Adversarial / noisy reward below the evidence floor -> no thrashing.
# ---------------------------------------------------------------------------


def test_adversarial_sparse_noise_does_not_thrash():
    spec = {"default": 72, "allowed": [24, 48, 72]}
    candidates = opt.candidate_arm_values(spec)
    # An adversary feeds a few noisy, contradictory outcomes -- but the total is
    # below MIN_EVIDENCE_OUTCOMES, so the learner must refuse to move the knob
    # for EVERY seed (no thrashing on thin, conflicting data).
    rng = random.Random(999)
    rows = _noisy_binary_stream(rng, {24: 0.5, 48: 0.5}, n_per_arm=2)  # 4 total < 6
    family, evidence = opt.aggregate_arm_evidence(rows, candidates)
    assert sum(e.n for e in evidence.values()) < opt.MIN_EVIDENCE_OUTCOMES
    for seed in range(50):
        proposal = opt.propose_from_evidence(
            spec, family, evidence, 72, knob=KNOB, rng=random.Random(seed)
        )
        assert proposal.changed is False
        assert proposal.proposed_value == 72
        assert proposal.reason == "insufficient_evidence"


def test_high_variance_continuous_below_floor_holds_position():
    spec = {"default": 72, "range": [24, 120]}
    candidates = opt.candidate_arm_values(spec, include=72)
    rng = random.Random(31)
    # Wildly noisy but only 4 outcomes -- below the floor; must not move.
    rows = _noisy_continuous_stream(rng, {24: 50.0, 120: 55.0}, sigma=200.0, n_per_arm=2)
    family, evidence = opt.aggregate_arm_evidence(rows, candidates)
    assert sum(e.n for e in evidence.values()) < opt.MIN_EVIDENCE_OUTCOMES
    for seed in range(40):
        proposal = opt.propose_from_evidence(
            spec, family, evidence, 72, knob=KNOB, rng=random.Random(seed)
        )
        assert proposal.changed is False
        assert proposal.reason == "insufficient_evidence"


# ---------------------------------------------------------------------------
# 5. DB-backed replay over a noisy recorded stream.
# ---------------------------------------------------------------------------


def test_db_replay_over_noisy_stream_proposes_better_arm(fresh_home):
    slug = "opt-noisy-replay"
    spec = {"default": 72, "allowed": [48, 72]}
    _make_board(slug, spec)
    rng = random.Random(4242)
    rows = _noisy_binary_stream(rng, {48: 0.78, 72: 0.25}, n_per_arm=40)
    base = 5_000_000
    with kb.connect(board=slug) as conn:
        for i, (value, kind, rval) in enumerate(rows):
            _emit_outcome(
                conn, board=slug, knob=KNOB, value=value,
                reward_kind=kind, reward_value=rval, ts=base + i,
            )
        family, evidence = opt.read_knob_outcome_evidence(
            conn, board=slug, knob=KNOB, spec=spec
        )
        assert family == "binary"
        assert sum(e.n for e in evidence.values()) == len(rows)

        # Modal proposal over seeds settles on the higher-reward arm (48), and is
        # always one of the two declared arms.
        counts = {}
        for seed in range(60):
            proposal = opt.propose_knob_value(
                conn, board=slug, knob=KNOB, rng=random.Random(seed),
                use_cross_business=False,
            )
            assert proposal.proposed_value in (48, 72)
            counts[proposal.proposed_value] = counts.get(proposal.proposed_value, 0) + 1
        assert max(counts, key=lambda k: counts[k]) == 48
