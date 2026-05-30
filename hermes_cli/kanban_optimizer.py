"""P2 minimal optimizer: the closed learning loop over ONE bounded knob.

This module proves the loop end-to-end::

    knob value -> action -> signal -> reward -> posterior update -> better value

It is deliberately *sample-efficient*. A real business produces dozens of
outcomes, not millions, so the learner is a textbook Bayesian bandit
(Thompson sampling) rather than a gradient method that needs a firehose of
data. Each discretized knob value is an arm; each ``outcome`` board_signal is
one pull of the arm recorded in its ``knob_snapshot``; the reward column is the
payoff. We keep one posterior per arm and sample to balance explore/exploit.

Design boundaries (intentional for P2):

* **One knob.** The code is written to loop over many knobs, but only one is
  wired by default (``follow_up_interval_hours`` / ``cadence_hours``). Adding a
  knob later is adding a name to :data:`OPTIMIZER_MANAGED_KNOBS`; the math is
  already per-knob.
* **No cross-business priors.** Every board learns from its own signals with a
  weak uniform prior. Sharing posteriors across businesses (a warm start from
  similar domains) is P3 -- see the ``context_features`` already captured on
  every signal.
* **Bounded action space.** Arms are derived strictly from the knob's declared
  ``range``/``allowed`` set. The learner can NEVER propose a value outside the
  declared bounds; the DB layer (:func:`hermes_cli.kanban_db.apply_knob_update`)
  enforces the same guard a second time and routes anything out-of-bounds to a
  human approval gate.

The learner math (priors, posteriors, Thompson selection, the minimum-evidence
floor) is pure and unit-testable without a DB: pass in aggregated evidence. A
thin DB-reading adapter (:func:`read_knob_outcome_evidence` /
:func:`propose_knob_value`) sits on top.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

# ---------------------------------------------------------------------------
# Tunables of the optimizer itself (module constants -- easy to find + tune).
# ---------------------------------------------------------------------------

#: Minimum number of realized outcomes (across the candidate arms) required
#: before the optimizer will propose an autonomous change. Under sparse data
#: the high prior uncertainty should defer to the current value, so we simply
#: refuse to move the knob until the evidence floor ``K`` is cleared.
MIN_EVIDENCE_OUTCOMES: int = 6

#: How many candidate arms to carve a continuous ``range`` knob into. Kept
#: small for sample efficiency -- fewer arms means each one accrues evidence
#: faster.
MAX_RANGE_ARMS: int = 6

#: Weak uniform Beta(1, 1) prior for binary rewards: every arm starts as a
#: fair coin until data moves it.
PRIOR_ALPHA: float = 1.0
PRIOR_BETA: float = 1.0

#: Weakly-informative Gaussian prior over an arm's mean utility (continuous
#: rewards). A huge prior variance keeps the prior from biasing the posterior.
PRIOR_GAUSSIAN_MEAN: float = 0.0
PRIOR_GAUSSIAN_VARIANCE: float = 1.0e6

#: Fallback observation variance for the Gaussian model when too little data
#: exists to estimate it from the signal stream.
DEFAULT_OBS_VARIANCE: float = 1.0

#: Reward kinds that are Bernoulli successes/failures (Beta-Bernoulli arm).
#: ``loop_closed`` is included because the reactive runtime emits it as a 0/1
#: terminal payoff (won -> conversion=1, otherwise loop_closed=0).
BINARY_REWARD_KINDS: frozenset[str] = frozenset({"conversion", "reply", "loop_closed"})

#: Reward kinds that are continuous and *lower is better* (Gaussian arm). We
#: flip the sign so the learner always maximizes a utility.
CONTINUOUS_REWARD_KINDS: frozenset[str] = frozenset({"time_to_done", "cost"})

#: The bounded knobs this optimizer manages, by preference order. Every present
#: managed knob is tuned each tick (E3 multi-knob loop in
#: :func:`hermes_cli.kanban_db.optimizer_tick`).
#:
#: E1 TODO (concurrency caps): the dispatch caps in
#: :data:`CONCURRENCY_CAP_KNOBS` are NOT listed here yet. The E2 read-seam
#: (:func:`managed_cap_default`, wired into ``dispatch_once``) now makes a write
#: to a cap tunable take effect, so adding a cap name here would no longer be a
#: silent no-op at the dispatch layer. It is still withheld because the Thompson
#: learner has no outcome attribution for a *concurrency* knob (rewards are
#: attributed via ``knob_snapshot`` on ``outcome`` signals, which today snapshot
#: cadence-type knobs), and :func:`is_cadence_knob` would mis-classify a cap as a
#: timer-cadence knob and try to re-arm reactive schedules on a cap change. Wiring
#: a cap into the learner needs (a) a cap-aware reward signal + snapshot and
#: (b) splitting the cadence re-arm from the generic apply path -- a follow-up.
OPTIMIZER_MANAGED_KNOBS: tuple[str, ...] = ("follow_up_interval_hours", "cadence_hours")

# ---------------------------------------------------------------------------
# P3 cross-business (hierarchical) priors -- the compounding data moat.
# ---------------------------------------------------------------------------

#: Coarse ``context_features`` keys used to decide whether two businesses are
#: "similar enough" to pool their posteriors. Matching is deliberately coarse
#: (so sparse data pools) but conservative (a disagreement on any *shared* key
#: blocks pooling). ``domain`` is the primary discriminator; ``segment`` refines
#: it. See :func:`context_matches`.
OPTIMIZER_POOL_CONTEXT_KEYS: tuple[str, ...] = ("domain", "segment")

#: The strength (in pseudo-observations) of the cross-business prior, i.e. the
#: ``kappa`` in the shrinkage weight ``kappa / (kappa + local_n)``. A board with
#: zero local outcomes leans fully on the pooled prior; the prior's pull halves
#: once the board has ``CROSS_PRIOR_STRENGTH`` of its own outcomes and keeps
#: decaying, so a mature board ends up driven by its own data.
CROSS_PRIOR_STRENGTH: float = 8.0

#: Per-arm cap on the pooled pseudo-count mass an arm can contribute. Stops a
#: single high-volume sibling board from minting an immovable prior; the local
#: stream can always out-vote at most this much borrowed evidence per arm.
MAX_POOLED_PSEUDO: float = 20.0

#: Cold-start poisoning guard (per-sibling bounded influence). When pooling many
#: sibling boards' outcomes, NO single sibling may contribute more than this many
#: effective observations PER ARM -- an adversarial / degenerate sibling with a
#: huge outcome count is clamped to the same weight as an honest one, so the
#: pooled rate reflects the majority of well-behaved siblings rather than the
#: loudest one. Equal to the global per-arm cap so a lone honest sibling is
#: unaffected; the protection bites when one sibling dwarfs the others.
MAX_SIBLING_ARM_PSEUDO: float = MAX_POOLED_PSEUDO

#: Cold-start poisoning guard (minimum evidence floor per sibling). A sibling
#: must have at least this many relevant outcomes (across arms) before it may
#: contribute to the pooled prior at all -- a board with one or two flukey
#: outcomes is too thin to warm-start anyone and is excluded entirely.
MIN_SIBLING_EVIDENCE: int = 3

_BINARY = "binary"
_CONTINUOUS = "continuous"


# ---------------------------------------------------------------------------
# Knob spec parsing -- the single source of truth for the bounded action space.
# (Imported by kanban_db.apply_knob_update so the bounds guard lives once.)
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def knob_range(spec: Optional[dict]) -> Optional[tuple[float, float]]:
    """Return ``(lo, hi)`` for a numeric range knob, else None."""
    spec = spec or {}
    rng = spec.get("range")
    if rng is None and _is_number(spec.get("min")) and _is_number(spec.get("max")):
        rng = [spec.get("min"), spec.get("max")]
    if (
        isinstance(rng, (list, tuple))
        and len(rng) == 2
        and _is_number(rng[0])
        and _is_number(rng[1])
        and rng[0] <= rng[1]
    ):
        return float(rng[0]), float(rng[1])
    return None


def knob_allowed(spec: Optional[dict]) -> Optional[list[Any]]:
    """Return the explicit allowed/options set for a discrete knob, else None."""
    spec = spec or {}
    allowed = spec.get("allowed")
    if allowed is None:
        allowed = spec.get("options")
    if isinstance(allowed, (list, tuple)) and allowed:
        return list(allowed)
    return None


def knob_value_in_bounds(spec: Optional[dict], value: Any) -> bool:
    """True iff ``value`` is inside the knob's declared bounds.

    A knob that declares neither an ``allowed`` set nor a numeric ``range`` is
    treated as *unbounded* and therefore NOT safe for autonomous tuning -- this
    returns False, forcing the caller to route through a human approval gate.
    """
    allowed = knob_allowed(spec)
    if allowed is not None:
        if value in allowed:
            return True
        # Tolerate int/float spelling differences (72 vs 72.0).
        if _is_number(value):
            return any(_is_number(a) and float(a) == float(value) for a in allowed)
        return False
    rng = knob_range(spec)
    if rng is not None and _is_number(value):
        return rng[0] <= float(value) <= rng[1]
    return False


def _discretize_range(lo: float, hi: float, max_arms: int) -> list[Any]:
    both_int = float(lo).is_integer() and float(hi).is_integer()
    if hi <= lo:
        return [int(lo) if both_int else lo]
    if both_int and (int(hi) - int(lo) + 1) <= max_arms:
        return list(range(int(lo), int(hi) + 1))
    n = max(2, max_arms)
    step = (hi - lo) / (n - 1)
    raw = [lo + step * i for i in range(n)]
    if both_int:
        return sorted({int(round(v)) for v in raw})
    return raw


def candidate_arm_values(
    spec: Optional[dict],
    *,
    max_arms: int = MAX_RANGE_ARMS,
    include: Optional[Any] = None,
) -> list[Any]:
    """Enumerate the candidate knob values (arms) from the declared bounds.

    * ``allowed``/``options`` knobs -> exactly those values.
    * numeric ``range`` knobs -> up to ``max_arms`` discretized points
      (every integer when the span is small, else evenly spaced).

    ``include`` (typically the current value) is merged in when it is within
    bounds, so "leave the knob where it is" is always a representable arm.
    """
    allowed = knob_allowed(spec)
    if allowed is not None:
        values = list(allowed)
    else:
        rng = knob_range(spec)
        values = _discretize_range(rng[0], rng[1], max_arms) if rng else []
    if include is not None and knob_value_in_bounds(spec, include):
        if not any(_values_equal(include, v) for v in values):
            values.append(include)
    # Stable de-dup; sort numerics for readable arm summaries.
    deduped: list[Any] = []
    for v in values:
        if not any(_values_equal(v, d) for d in deduped):
            deduped.append(v)
    if deduped and all(_is_number(v) for v in deduped):
        deduped.sort()
    return deduped


def _values_equal(a: Any, b: Any) -> bool:
    if _is_number(a) and _is_number(b):
        return float(a) == float(b)
    return a == b


def _snap_to_candidate(value: Any, candidates: Sequence[Any]) -> Optional[Any]:
    """Map an observed knob value to its nearest candidate arm."""
    for c in candidates:
        if _values_equal(value, c):
            return c
    if _is_number(value):
        numeric = [c for c in candidates if _is_number(c)]
        if numeric:
            return min(numeric, key=lambda c: abs(float(c) - float(value)))
    return None


# ---------------------------------------------------------------------------
# Reward handling -- normalize every reward into a utility to MAXIMIZE.
# ---------------------------------------------------------------------------


def reward_family(reward_kind: Optional[str]) -> Optional[str]:
    if reward_kind in BINARY_REWARD_KINDS:
        return _BINARY
    if reward_kind in CONTINUOUS_REWARD_KINDS:
        return _CONTINUOUS
    return None


def reward_utility(reward_kind: Optional[str], reward_value: Any) -> Optional[float]:
    """Normalize a (kind, value) pair into a utility where higher == better.

    Binary rewards collapse to 1.0 (success) / 0.0 (failure). Continuous
    rewards are *lower is better*, so we negate them -- a smaller time-to-done
    or cost becomes a larger utility.
    """
    fam = reward_family(reward_kind)
    if fam is None or reward_value is None:
        return None
    try:
        val = float(reward_value)
    except (TypeError, ValueError):
        return None
    if fam == _BINARY:
        return 1.0 if val > 0 else 0.0
    return -val


# ---------------------------------------------------------------------------
# Evidence aggregation -- group outcome rows by the arm they were produced at.
# ---------------------------------------------------------------------------


@dataclass
class ArmEvidence:
    """Aggregated outcomes observed at a single knob value (one arm)."""

    value: Any
    family: str
    n: int = 0
    success: float = 0.0          # binary: number of successes
    util_sum: float = 0.0         # continuous: sum of utilities
    util_sq_sum: float = 0.0      # continuous: sum of squared utilities

    def observe(self, utility: float) -> None:
        self.n += 1
        if self.family == _BINARY:
            self.success += 1.0 if utility > 0 else 0.0
        else:
            self.util_sum += utility
            self.util_sq_sum += utility * utility

    @property
    def mean_utility(self) -> float:
        if self.n == 0:
            return 0.0
        if self.family == _BINARY:
            return self.success / self.n
        return self.util_sum / self.n


def choose_reward_family(observations: Iterable[tuple[Any, Optional[str], Any]]) -> Optional[str]:
    """Pick the reward family to learn from when a knob has mixed signals.

    Binary outcomes are preferred (they are the business-meaningful win/loss
    payoffs); the continuous family is used only when there are no binary
    outcomes at all.
    """
    binary = 0
    continuous = 0
    for _value, kind, rval in observations:
        fam = reward_family(kind)
        if fam is None or rval is None:
            continue
        if fam == _BINARY:
            binary += 1
        elif fam == _CONTINUOUS:
            continuous += 1
    if binary == 0 and continuous == 0:
        return None
    return _BINARY if binary >= continuous else _CONTINUOUS


def aggregate_arm_evidence(
    observations: Iterable[tuple[Any, Optional[str], Any]],
    candidates: Sequence[Any],
    *,
    family: Optional[str] = None,
) -> tuple[Optional[str], dict[Any, ArmEvidence]]:
    """Fold ``(knob_value, reward_kind, reward_value)`` rows into per-arm evidence.

    Returns ``(family, {arm_value: ArmEvidence})``. Rows whose reward kind does
    not match the chosen family, or whose knob value snaps to no candidate arm,
    are ignored. The function is pure: feed it tuples in tests, no DB required.
    """
    observations = list(observations)
    if family is None:
        family = choose_reward_family(observations)
    if family is None:
        return None, {}
    by_arm: dict[Any, ArmEvidence] = {}
    for value, kind, rval in observations:
        if reward_family(kind) != family:
            continue
        utility = reward_utility(kind, rval)
        if utility is None:
            continue
        arm = _snap_to_candidate(value, candidates)
        if arm is None:
            continue
        key = _arm_key(arm)
        ev = by_arm.get(key)
        if ev is None:
            ev = ArmEvidence(value=arm, family=family)
            by_arm[key] = ev
        ev.observe(utility)
    return family, by_arm


def _arm_key(value: Any) -> Any:
    return float(value) if _is_number(value) else value


def _capped_arm_evidence(ev: ArmEvidence, cap: float) -> ArmEvidence:
    """Return ``ev`` scaled down so its ``n`` never exceeds ``cap``.

    Bounded-influence guard: when a single sibling has more than ``cap``
    outcomes at an arm, scale its sufficient statistics by ``cap / n`` so its
    success *rate* / mean utility is preserved but its *weight* is clamped. A
    sibling at or below the cap is returned unchanged.
    """
    if cap <= 0 or ev.n <= cap:
        return ev
    scale = cap / float(ev.n)
    capped = ArmEvidence(value=ev.value, family=ev.family, n=int(round(cap)))
    capped.success = ev.success * scale
    capped.util_sum = ev.util_sum * scale
    capped.util_sq_sum = ev.util_sq_sum * scale
    return capped


def pool_sibling_evidence(
    sibling_observations: Sequence[Iterable[tuple[Any, Optional[str], Any]]],
    candidates: Sequence[Any],
    *,
    family: Optional[str] = None,
    min_sibling_evidence: int = MIN_SIBLING_EVIDENCE,
    max_sibling_arm_pseudo: float = MAX_SIBLING_ARM_PSEUDO,
) -> tuple[Optional[str], dict[Any, ArmEvidence]]:
    """Pool many siblings' outcomes into per-arm evidence with poisoning guards.

    Unlike flattening every sibling's rows into one stream (where one
    high-volume sibling dominates), this aggregates EACH sibling independently
    and then:

    * drops any sibling with fewer than ``min_sibling_evidence`` relevant
      outcomes (too thin to warm-start anyone), and
    * caps each sibling's PER-ARM contribution at ``max_sibling_arm_pseudo``
      (an adversarial / degenerate sibling with a huge count is clamped to the
      same weight as an honest one) before merging.

    Returns ``(family, pooled_by_arm)``. Pure: feed it per-sibling observation
    lists in tests, no DB required.
    """
    sibling_lists = [list(obs) for obs in sibling_observations]
    if family is None:
        flat = [row for lst in sibling_lists for row in lst]
        family = choose_reward_family(flat)
    if family is None:
        return None, {}
    pooled: dict[Any, ArmEvidence] = {}
    for obs in sibling_lists:
        _fam, by_arm = aggregate_arm_evidence(obs, candidates, family=family)
        sibling_total = sum(ev.n for ev in by_arm.values())
        if sibling_total < min_sibling_evidence:
            continue
        for key, ev in by_arm.items():
            capped = _capped_arm_evidence(ev, max_sibling_arm_pseudo)
            agg = pooled.get(key)
            if agg is None:
                agg = ArmEvidence(value=capped.value, family=family)
                pooled[key] = agg
            agg.n += capped.n
            agg.success += capped.success
            agg.util_sum += capped.util_sum
            agg.util_sq_sum += capped.util_sq_sum
    return family, pooled


# ---------------------------------------------------------------------------
# Posteriors -- pure, frozen, sample-efficient. Unit-testable without a DB.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BetaBernoulliPosterior:
    """Beta posterior over an arm's success probability (binary reward)."""

    alpha: float
    beta: float

    @classmethod
    def from_evidence(
        cls,
        evidence: Optional[ArmEvidence],
        *,
        prior_alpha: float = PRIOR_ALPHA,
        prior_beta: float = PRIOR_BETA,
    ) -> "BetaBernoulliPosterior":
        successes = evidence.success if evidence else 0.0
        n = evidence.n if evidence else 0
        failures = max(0.0, n - successes)
        return cls(prior_alpha + successes, prior_beta + failures)

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def stddev(self) -> float:
        a, b = self.alpha, self.beta
        denom = (a + b) * (a + b) * (a + b + 1.0)
        if denom <= 0:
            return 0.0
        return math.sqrt((a * b) / denom)

    def sample(self, rng: random.Random) -> float:
        return rng.betavariate(self.alpha, self.beta)


@dataclass(frozen=True)
class GaussianPosterior:
    """Normal posterior over an arm's mean utility (continuous reward).

    Conjugate Normal-Normal update with a known observation variance. ``mean``
    is the posterior mean of the arm's utility; ``variance`` is the posterior
    variance of that mean (shrinks as evidence accrues -> less exploration).
    """

    mean: float
    variance: float

    @classmethod
    def from_evidence(
        cls,
        evidence: Optional[ArmEvidence],
        *,
        prior_mean: float = PRIOR_GAUSSIAN_MEAN,
        prior_variance: float = PRIOR_GAUSSIAN_VARIANCE,
        obs_variance: float = DEFAULT_OBS_VARIANCE,
    ) -> "GaussianPosterior":
        n = evidence.n if evidence else 0
        if n == 0:
            return cls(prior_mean, prior_variance)
        obs_variance = obs_variance if obs_variance > 0 else DEFAULT_OBS_VARIANCE
        sample_mean = evidence.util_sum / n
        post_precision = (1.0 / prior_variance) + (n / obs_variance)
        post_mean = (
            (prior_mean / prior_variance) + (n * sample_mean / obs_variance)
        ) / post_precision
        return cls(post_mean, 1.0 / post_precision)

    @property
    def stddev(self) -> float:
        return math.sqrt(max(self.variance, 0.0))

    def sample(self, rng: random.Random) -> float:
        return rng.gauss(self.mean, math.sqrt(max(self.variance, 0.0)))


def _pooled_obs_variance(evidence_by_arm: dict[Any, ArmEvidence]) -> float:
    """Estimate a shared observation variance across continuous arms."""
    total_n = 0
    total_var_weight = 0.0
    for ev in evidence_by_arm.values():
        if ev.family != _CONTINUOUS or ev.n < 2:
            continue
        mean = ev.util_sum / ev.n
        var = max(0.0, (ev.util_sq_sum / ev.n) - mean * mean)
        total_var_weight += var * ev.n
        total_n += ev.n
    if total_n == 0 or total_var_weight <= 0:
        return DEFAULT_OBS_VARIANCE
    return total_var_weight / total_n


# ---------------------------------------------------------------------------
# P3 cross-business priors -- context matching, shrinkage, pooled priors.
# ---------------------------------------------------------------------------


def context_matches(
    target: Optional[dict],
    other: Optional[dict],
    *,
    keys: Sequence[str] = OPTIMIZER_POOL_CONTEXT_KEYS,
) -> bool:
    """Coarse similarity test between two ``context_features`` dicts.

    Two contexts are "similar" (and therefore poolable) iff they **share at
    least one** of the coarse pooling keys with an equal value AND **disagree
    on none** of the keys they both carry. So:

    * same ``domain`` (only shared key) -> match (pool sparse data);
    * different ``domain`` -> no match (an unrelated business never borrows);
    * same ``domain`` but conflicting ``segment`` -> no match (conservative).

    A context whose only shared key is the per-board identity ``domain`` (the
    board slug, for legacy boards that never declared a business domain) only
    matches another board carrying that *same* slug -- which never happens
    across distinct boards -- so legacy boards do not cross-pollinate.
    """
    if not isinstance(target, dict) or not isinstance(other, dict):
        return False
    shared = [
        k
        for k in keys
        if target.get(k) not in (None, "") and other.get(k) not in (None, "")
    ]
    if not shared:
        return False
    for k in shared:
        if str(target.get(k)) != str(other.get(k)):
            return False
    return True


def shrinkage_weight(local_n: float, *, strength: float = CROSS_PRIOR_STRENGTH) -> float:
    """Weight in [0, 1] on the cross-business prior given local evidence.

    ``strength / (strength + local_n)`` -- the classic empirical-Bayes shrinkage
    toward a pooled prior. Zero local outcomes -> weight 1.0 (the prior fully
    governs); the weight halves at ``local_n == strength`` and decays toward 0
    as the board accrues its own data, so a mature board ignores the prior.
    """
    if strength <= 0:
        return 0.0
    n = max(0.0, float(local_n))
    return strength / (strength + n)


@dataclass(frozen=True)
class ArmPrior:
    """Per-arm prior fed into the per-board learner in place of the flat prior.

    Holds Beta params (binary) and Normal params (continuous); the learner
    reads whichever pair matches the reward family. ``pseudo_count`` is the
    effective number of borrowed observations this prior represents -- it is
    summed across arms to (a) count toward the minimum-evidence floor for a
    cold-start board and (b) surface "how strongly the prior is influencing
    this knob" in the learned-state read-model.
    """

    alpha: float = PRIOR_ALPHA
    beta: float = PRIOR_BETA
    mean: float = PRIOR_GAUSSIAN_MEAN
    variance: float = PRIOR_GAUSSIAN_VARIANCE
    pseudo_count: float = 0.0


def build_pooled_priors(
    family: str,
    pooled_by_arm: dict[Any, ArmEvidence],
    candidates: Sequence[Any],
    local_n: float,
    *,
    strength: float = CROSS_PRIOR_STRENGTH,
    max_pseudo: float = MAX_POOLED_PSEUDO,
) -> tuple[float, dict[Any, ArmPrior], float]:
    """Turn pooled cross-business evidence into per-arm shrinkage priors.

    Returns ``(weight, priors_by_arm_key, total_pseudo_count)`` where ``weight``
    is :func:`shrinkage_weight` and ``total_pseudo_count`` is the borrowed
    evidence mass summed across arms.

    Math (binary / Beta-Bernoulli): for an arm with pooled successes ``s`` over
    ``n`` sibling outcomes, the borrowed mass is
    ``mass = weight * min(n, max_pseudo)`` and the prior is
    ``Beta(PRIOR_ALPHA + mass*m, PRIOR_BETA + mass*(1-m))`` with ``m = s/n``.
    As ``weight -> 0`` (local data accrues) the prior collapses back to the flat
    ``Beta(1, 1)``; as ``weight -> 1`` (cold start) the arm starts at the
    sibling success rate with ``min(n, max_pseudo)`` pseudo-observations of
    confidence.

    Math (continuous / Normal): the prior mean is the pooled mean utility and
    the prior variance is ``obs_variance / mass`` (more borrowed mass ->
    tighter prior), i.e. ``mass`` pseudo-observations at the pooled mean.
    """
    weight = shrinkage_weight(local_n, strength=strength)
    priors: dict[Any, ArmPrior] = {}
    total_pseudo = 0.0
    obs_variance = _pooled_obs_variance(pooled_by_arm) if family == _CONTINUOUS else DEFAULT_OBS_VARIANCE
    for arm in candidates:
        key = _arm_key(arm)
        ev = pooled_by_arm.get(key)
        if ev is None or ev.n <= 0 or weight <= 0:
            priors[key] = ArmPrior()
            continue
        mass = weight * min(float(ev.n), max_pseudo)
        if mass <= 0:
            priors[key] = ArmPrior()
            continue
        if family == _BINARY:
            m = ev.success / ev.n
            priors[key] = ArmPrior(
                alpha=PRIOR_ALPHA + mass * m,
                beta=PRIOR_BETA + mass * (1.0 - m),
                pseudo_count=mass,
            )
        else:
            m = ev.util_sum / ev.n
            ov = obs_variance if obs_variance > 0 else DEFAULT_OBS_VARIANCE
            priors[key] = ArmPrior(
                mean=m,
                variance=max(ov / mass, 1e-9),
                pseudo_count=mass,
            )
        total_pseudo += mass
    return weight, priors, total_pseudo


# ---------------------------------------------------------------------------
# The proposal -- Thompson sampling with a minimum-evidence floor.
# ---------------------------------------------------------------------------


@dataclass
class KnobProposal:
    knob: str
    current_value: Any
    proposed_value: Any
    changed: bool
    reason: str
    family: Optional[str]
    total_outcomes: int
    in_bounds: bool = True
    arm_summary: dict[Any, dict[str, float]] = field(default_factory=dict)


def build_posterior(
    family: str,
    evidence: Optional[ArmEvidence],
    *,
    obs_variance: float = DEFAULT_OBS_VARIANCE,
    prior: Optional[ArmPrior] = None,
):
    """Posterior for one arm, optionally warm-started from a cross-business prior.

    When ``prior`` is None the flat ``Beta(1, 1)`` / weak-Normal baseline is
    used (P2 behaviour); when supplied the per-arm pooled prior replaces it.
    """
    if family == _BINARY:
        if prior is not None:
            return BetaBernoulliPosterior.from_evidence(
                evidence, prior_alpha=prior.alpha, prior_beta=prior.beta
            )
        return BetaBernoulliPosterior.from_evidence(evidence)
    if prior is not None:
        return GaussianPosterior.from_evidence(
            evidence,
            prior_mean=prior.mean,
            prior_variance=prior.variance,
            obs_variance=obs_variance,
        )
    return GaussianPosterior.from_evidence(evidence, obs_variance=obs_variance)


def posterior_means(
    spec: Optional[dict],
    family: str,
    evidence_by_arm: dict[Any, ArmEvidence],
    *,
    current_value: Any = None,
    priors: Optional[dict[Any, ArmPrior]] = None,
) -> dict[Any, float]:
    """Deterministic exploit view: posterior mean utility per candidate arm."""
    candidates = candidate_arm_values(spec, include=current_value)
    obs_variance = _pooled_obs_variance(evidence_by_arm)
    means: dict[Any, float] = {}
    for arm in candidates:
        ev = evidence_by_arm.get(_arm_key(arm))
        prior = priors.get(_arm_key(arm)) if priors else None
        post = build_posterior(family, ev, obs_variance=obs_variance, prior=prior)
        means[arm] = post.mean
    return means


def propose_from_evidence(
    spec: Optional[dict],
    family: Optional[str],
    evidence_by_arm: dict[Any, ArmEvidence],
    current_value: Any,
    *,
    knob: str = "knob",
    rng: Optional[random.Random] = None,
    min_evidence: int = MIN_EVIDENCE_OUTCOMES,
    priors: Optional[dict[Any, ArmPrior]] = None,
    prior_evidence: float = 0.0,
) -> KnobProposal:
    """Propose the next knob value via Thompson sampling (pure, no DB).

    Honours the minimum-evidence floor: with fewer than ``min_evidence`` total
    outcomes the knob is left exactly where it is (the prior is too uncertain
    to justify an autonomous move). Otherwise one Thompson draw per arm picks
    the winner -- always a value drawn from the declared candidate set, so the
    proposal is bounded by construction.

    ``priors`` supplies per-arm cross-business (pooled) priors and
    ``prior_evidence`` the borrowed pseudo-count mass. The pseudo-count counts
    toward the minimum-evidence floor, so a cold-start board with a strong
    pooled prior can warm-start to the arm learned elsewhere instead of being
    pinned by the floor; as local evidence accumulates the shrinkage weight
    decays the prior away and local data dominates.
    """
    rng = rng or random.Random()
    candidates = candidate_arm_values(spec, include=current_value)
    total = sum(ev.n for ev in evidence_by_arm.values())
    effective_total = total + max(0.0, float(prior_evidence))
    arm_summary = {
        arm: {
            "n": float((evidence_by_arm.get(_arm_key(arm)) or ArmEvidence(arm, family or _BINARY)).n),
        }
        for arm in candidates
    }

    if not candidates:
        return KnobProposal(
            knob=knob, current_value=current_value, proposed_value=current_value,
            changed=False, reason="no_candidates", family=family,
            total_outcomes=total, arm_summary=arm_summary,
        )
    if family is None or effective_total < min_evidence:
        return KnobProposal(
            knob=knob, current_value=current_value, proposed_value=current_value,
            changed=False, reason="insufficient_evidence", family=family,
            total_outcomes=total, arm_summary=arm_summary,
        )

    obs_variance = _pooled_obs_variance(evidence_by_arm)
    best_arm = current_value if current_value in candidates else candidates[0]
    best_sample = -math.inf
    for arm in candidates:
        ev = evidence_by_arm.get(_arm_key(arm))
        prior = priors.get(_arm_key(arm)) if priors else None
        post = build_posterior(family, ev, obs_variance=obs_variance, prior=prior)
        draw = post.sample(rng)
        arm_summary[arm]["posterior_mean"] = post.mean
        arm_summary[arm]["sample"] = draw
        if draw > best_sample:
            best_sample = draw
            best_arm = arm

    changed = not _values_equal(best_arm, current_value)
    return KnobProposal(
        knob=knob, current_value=current_value, proposed_value=best_arm,
        changed=changed, reason="thompson" if changed else "thompson_no_change",
        family=family, total_outcomes=total, in_bounds=True, arm_summary=arm_summary,
    )


# ---------------------------------------------------------------------------
# Thin DB-reading adapter -- reads board_signals, returns a pure proposal.
# ---------------------------------------------------------------------------


def find_knob_spec(contract: Optional[dict], knob: str) -> Optional[dict]:
    """Locate a knob's tunable spec in a contract (top-level or runtime block)."""
    if not isinstance(contract, dict):
        return None
    tunables = contract.get("tunables")
    if not isinstance(tunables, dict):
        runtime = contract.get("runtime")
        tunables = runtime.get("tunables") if isinstance(runtime, dict) else None
    if isinstance(tunables, dict):
        spec = tunables.get(knob)
        if isinstance(spec, dict):
            return spec
    return None


def knob_default(spec: Optional[dict]) -> Any:
    if isinstance(spec, dict):
        return spec.get("default")
    return None


def select_managed_knob(contract: Optional[dict]) -> Optional[str]:
    """Return the single knob this optimizer manages for the board, if present."""
    for knob in OPTIMIZER_MANAGED_KNOBS:
        if find_knob_spec(contract, knob) is not None:
            return knob
    return None


def is_cadence_knob(knob: Optional[str]) -> bool:
    """True if ``knob`` is the optimizer-managed timer cadence knob.

    Both managed knobs (``follow_up_interval_hours`` / ``cadence_hours``) drive
    the reactive timer cadence, so an applied change to either must re-arm the
    live ``reactive_timer_schedules`` (see
    :func:`hermes_cli.kanban_db.apply_knob_update`).
    """
    return bool(knob) and knob in OPTIMIZER_MANAGED_KNOBS


def managed_cadence_default(
    contract: Optional[dict], *, knob: Optional[str] = None
) -> Optional[float]:
    """Return the board's managed timer-cadence knob default (in hours), if any.

    This is the SINGLE SOURCE OF TRUTH for the watcher follow-up cadence: the
    reactive runtime resolves a schedule's cadence as
    ``managed_cadence_default(contract) or trigger.cadence_hours`` so the
    optimizer's tuning of the knob has real behavioural effect. Returns
    ``None`` when no managed cadence knob is declared (then the trigger's own
    ``cadence_hours`` is used).
    """
    name = knob or select_managed_knob(contract)
    if not name:
        return None
    default = knob_default(find_knob_spec(contract, name))
    if isinstance(default, bool):
        return None
    if isinstance(default, (int, float)) and default > 0:
        return float(default)
    return None


#: Concurrency-cap knob names the dispatcher resolves from the contract's
#: ``runtime.tunables`` (E2 read-seam). These are the dispatch caps the gateway
#: reads from ``config.yaml``; declaring one as a tunable lets the optimizer's
#: write to ``tunables[knob].default`` flow through to the live cap (config is
#: the FALLBACK). Mirrors how :func:`managed_cadence_default` feeds cadence.
#:
#: NOTE (E1): these are NOT yet in :data:`OPTIMIZER_MANAGED_KNOBS` -- the learner
#: tunes cadence knobs only. Adding a cap name there is a future step now that
#: the read-seam below makes such a write take effect (no longer a no-op).
CONCURRENCY_CAP_KNOBS: tuple[str, ...] = (
    "max_in_progress",
    "max_in_progress_per_profile",
    "max_spawn",
)


def managed_cap_default(
    contract: Optional[dict], knob: str
) -> Optional[int]:
    """Return a dispatch concurrency-cap knob's default from the contract.

    E2 read-seam. The gateway dispatcher resolves ``max_in_progress`` /
    ``max_in_progress_per_profile`` / ``max_spawn`` from
    ``contract.runtime.tunables[knob].default`` with the ``config.yaml`` value
    as FALLBACK -- mirroring how :func:`managed_cadence_default` feeds cadence.
    Without this seam an optimizer write to a cap tunable is a silent no-op
    (caps were read only from config).

    Returns the declared default coerced to a positive ``int`` when present and
    valid, else ``None`` (then the caller's config value is used). Booleans are
    rejected (a YAML ``true``/``false`` is not a cap), and values below ``1``
    are rejected (a cap of zero/negative is "no cap", which the caller already
    expresses with ``None`` -- so we defer to the config fallback rather than
    silently disable the cap from a malformed tunable).
    """
    if not knob:
        return None
    default = knob_default(find_knob_spec(contract, knob))
    if isinstance(default, bool):
        return None
    if isinstance(default, (int, float)):
        coerced = int(default)
        if coerced >= 1:
            return coerced
    return None


def read_knob_outcome_evidence(
    conn,
    *,
    board: str,
    knob: str,
    spec: Optional[dict],
    family: Optional[str] = None,
) -> tuple[Optional[str], dict[Any, ArmEvidence]]:
    """Read realized ``outcome`` signals and aggregate them per knob arm.

    Each outcome row carries the ``knob_snapshot`` active when the action was
    taken; we read this knob's value out of that snapshot and credit the
    reward to that arm. Rows without a usable snapshot value or reward are
    skipped. Returns ``(family, evidence_by_arm)``.
    """
    import json as _json

    rows = conn.execute(
        "SELECT knob_snapshot, reward_kind, reward_value, dedupe_key FROM board_signals "
        "WHERE board = ? AND primitive_kind = 'outcome' "
        "AND reward_value IS NOT NULL AND reward_kind IS NOT NULL "
        "ORDER BY ts ASC, id ASC",
        (board,),
    ).fetchall()
    observations: list[tuple[Any, Optional[str], Any]] = []
    # Exactly-once read guard: collapse any rows that share a stable
    # ``dedupe_key`` (belt-and-suspenders alongside the write-time UNIQUE index,
    # and a safety net for any legacy double-counted rows). Rows with a NULL
    # dedupe_key are uncontrolled telemetry and each still count.
    seen_keys: set[str] = set()
    for row in rows:
        dedupe_key = row[3]
        if dedupe_key:
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
        snapshot_raw = row[0]
        if not snapshot_raw:
            continue
        try:
            snapshot = _json.loads(snapshot_raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(snapshot, dict) or knob not in snapshot:
            continue
        observations.append((snapshot.get(knob), row[1], row[2]))
    candidates = candidate_arm_values(spec, include=knob_default(spec))

    # Fold in any rolled-up sufficient stats from retention-pruned outcome rows
    # so the posterior is identical whether or not old rows have been pruned
    # (the rollup preserves n / success / utility sums additively). Best-effort:
    # a missing rollup table (legacy DB) simply contributes nothing.
    rollups: dict[Any, dict[str, float]] = {}
    try:
        from hermes_cli import kanban_db as _kb  # local import avoids cycle

        rollups = _kb._read_outcome_rollups(conn, board=board, knob=knob)
    except Exception:  # pragma: no cover - rollup read is best-effort
        rollups = {}

    resolved_family = family
    if resolved_family is None:
        resolved_family = choose_reward_family(observations)
    if resolved_family is None and rollups:
        # No live rows: derive the family from the rollups (prefer binary).
        fams = {entry["family"] for entry in rollups.values()}
        resolved_family = _BINARY if _BINARY in fams else (_CONTINUOUS if _CONTINUOUS in fams else None)
    if resolved_family is None:
        return None, {}

    _fam, by_arm = aggregate_arm_evidence(observations, candidates, family=resolved_family)
    _merge_rollups_into_evidence(by_arm, rollups, candidates, resolved_family)
    return resolved_family, by_arm


def _merge_rollups_into_evidence(
    by_arm: dict[Any, ArmEvidence],
    rollups: dict[Any, dict[str, float]],
    candidates: Sequence[Any],
    family: str,
) -> None:
    """Add rolled-up sufficient stats into per-arm evidence (in place)."""
    for arm_key, entry in rollups.items():
        if entry.get("family") != family:
            continue
        arm = _snap_to_candidate(arm_key, candidates)
        if arm is None:
            continue
        key = _arm_key(arm)
        ev = by_arm.get(key)
        if ev is None:
            ev = ArmEvidence(value=arm, family=family)
            by_arm[key] = ev
        ev.n += int(entry.get("n", 0))
        if family == _BINARY:
            ev.success += float(entry.get("success", 0.0))
        else:
            ev.util_sum += float(entry.get("util_sum", 0.0))
            ev.util_sq_sum += float(entry.get("util_sq_sum", 0.0))


def propose_knob_value(
    conn,
    *,
    board: str,
    knob: str,
    contract: Optional[dict] = None,
    rng: Optional[random.Random] = None,
    min_evidence: int = MIN_EVIDENCE_OUTCOMES,
    use_cross_business: bool = True,
) -> KnobProposal:
    """End-to-end DB-backed proposal for one knob on one board.

    When ``use_cross_business`` is set (the P3 default) the local evidence is
    warm-started with a pooled prior computed from *other* boards' outcomes
    that match this board's ``context_features`` (see
    :func:`hermes_cli.kanban_db.read_cross_business_evidence`). The cross-board
    read is best-effort: any failure (missing/locked/corrupt sibling board)
    falls back to the purely local proposal so a board's own tick never breaks.
    """
    from hermes_cli import kanban_db as _kb  # local import avoids cycle

    if contract is None:
        contract = _kb._metadata_as_business_contract(_kb.read_board_metadata(board))
    spec = find_knob_spec(contract, knob)
    current_value = knob_default(spec)
    family_local, evidence = read_knob_outcome_evidence(
        conn, board=board, knob=knob, spec=spec
    )
    local_n = sum(ev.n for ev in evidence.values())

    resolved_family: Optional[str] = family_local
    priors: Optional[dict[Any, ArmPrior]] = None
    prior_evidence = 0.0
    if use_cross_business:
        try:
            family_pool, pooled_by_arm, _pool_meta = _kb.read_cross_business_evidence(
                target_board=board, knob=knob, spec=spec, family=family_local,
            )
            resolved_family = family_local or family_pool
            if resolved_family and pooled_by_arm:
                candidates = candidate_arm_values(spec, include=current_value)
                _weight, priors, prior_evidence = build_pooled_priors(
                    resolved_family, pooled_by_arm, candidates, local_n,
                )
        except Exception:  # pragma: no cover - cross-board read is best-effort
            priors, prior_evidence, resolved_family = None, 0.0, family_local

    return propose_from_evidence(
        spec, resolved_family, evidence, current_value,
        knob=knob, rng=rng, min_evidence=min_evidence,
        priors=priors, prior_evidence=prior_evidence,
    )
