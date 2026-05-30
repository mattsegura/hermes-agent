"""Hardening Pass 2 -- WS3: cross-business prior poisoning fix (cold-start).

build_pooled_priors warm-starts a new board from sibling posteriors. A single
adversarial / degenerate sibling must not be able to poison the pooled prior.
The guards: (a) a per-sibling per-arm cap on contributed pseudo-mass
(MAX_SIBLING_ARM_PSEUDO) so a high-volume sibling is clamped to an honest one's
weight, and (b) a minimum-evidence floor (MIN_SIBLING_EVIDENCE) excluding thin
siblings. Shrinkage toward local data as local evidence grows must still hold
(covered by the existing cross-business prior tests).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_optimizer as opt

CANDIDATES = [24, 48, 72]


def _binary(arm, n, success):
    """n outcomes at arm, `success` of them 1.0 (rest 0.0)."""
    rows = []
    for i in range(n):
        rows.append((arm, "conversion", 1.0 if i < success else 0.0))
    return rows


# ---------------------------------------------------------------------------
# A single extreme sibling cannot move the pooled prior beyond the cap.
# ---------------------------------------------------------------------------


def test_extreme_sibling_is_capped():
    # 3 honest siblings strongly favor arm 48; 1 adversary floods arm 72 with a
    # huge, mediocre stream. The cap clamps the adversary to honest weight.
    honest = [_binary(48, 5, 5) for _ in range(3)]  # each: 5/5 success at 48
    adversary = [_binary(72, 100_000, 40_000)]      # mean 0.4 at 72, massive n
    siblings = honest + adversary

    family, pooled = opt.pool_sibling_evidence(siblings, CANDIDATES)
    assert family == "binary"

    k48 = opt._arm_key(48)
    k72 = opt._arm_key(72)
    # The adversary's per-arm contribution is clamped to the cap, not 100k.
    assert pooled[k72].n == int(round(opt.MAX_SIBLING_ARM_PSEUDO))
    # Honest arm keeps its (sub-cap) weight summed across siblings: 3 * 5 == 15.
    assert pooled[k48].n == 15

    # The honest arm's mean still clearly beats the poisoned arm's mean.
    assert pooled[k48].success / pooled[k48].n == pytest.approx(1.0)
    poisoned_mean = pooled[k72].success / pooled[k72].n
    assert poisoned_mean == pytest.approx(0.4, abs=1e-6)
    assert pooled[k48].success / pooled[k48].n > poisoned_mean


def test_capped_sibling_preserves_its_rate():
    # Capping scales numerator and denominator together -> mean is preserved.
    siblings = [_binary(24, 1000, 700)]  # mean 0.7, n=1000
    _family, pooled = opt.pool_sibling_evidence(siblings, CANDIDATES)
    k24 = opt._arm_key(24)
    assert pooled[k24].n == int(round(opt.MAX_SIBLING_ARM_PSEUDO))
    assert pooled[k24].success / pooled[k24].n == pytest.approx(0.7, abs=1e-6)


# ---------------------------------------------------------------------------
# A sibling below the evidence floor is excluded entirely.
# ---------------------------------------------------------------------------


def test_sibling_below_evidence_floor_is_excluded():
    assert opt.MIN_SIBLING_EVIDENCE >= 2
    thin = [_binary(48, opt.MIN_SIBLING_EVIDENCE - 1, 1)]  # too few outcomes
    family, pooled = opt.pool_sibling_evidence(thin, CANDIDATES)
    # Family resolves but the thin sibling contributes nothing.
    assert family == "binary"
    assert pooled == {}


def test_floor_excludes_only_thin_siblings():
    thin = [_binary(48, opt.MIN_SIBLING_EVIDENCE - 1, 1)]
    solid = [_binary(72, opt.MIN_SIBLING_EVIDENCE + 2, 2)]
    _family, pooled = opt.pool_sibling_evidence(thin + solid, CANDIDATES)
    assert opt._arm_key(48) not in pooled
    assert opt._arm_key(72) in pooled
    assert pooled[opt._arm_key(72)].n == opt.MIN_SIBLING_EVIDENCE + 2


# ---------------------------------------------------------------------------
# An adversary cannot dominate even modest LOCAL data via the pooled prior.
# ---------------------------------------------------------------------------


def test_pooled_prior_cannot_dominate_local_data():
    # Adversary floods arm 72; honest siblings + local data favor arm 48.
    honest = [_binary(48, 5, 5) for _ in range(3)]
    adversary = [_binary(72, 100_000, 40_000)]
    family, pooled = opt.pool_sibling_evidence(honest + adversary, CANDIDATES)

    # Build the pooled prior the warm-start path would hand the learner at cold
    # start (local_n == 0 -> maximum shrinkage toward siblings).
    weight, priors, total_pseudo = opt.build_pooled_priors(
        family, pooled, CANDIDATES, local_n=0
    )
    # No single arm's borrowed pseudo-mass may exceed the global per-arm cap,
    # even with the adversary's 100k raw outcomes.
    for arm, prior in priors.items():
        assert prior.pseudo_count <= opt.MAX_POOLED_PSEUDO + 1e-9, (arm, prior.pseudo_count)
