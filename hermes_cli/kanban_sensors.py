"""Tier-1 sensor primitives: pure state-machine evaluation.

This module is the deterministic, offline brain of the three first-class sensor
primitives (heartbeat/stall, circuit breaker, budget meter). It contains NO
I/O: the DB-side half (:mod:`hermes_cli.kanban_db`'s ``sensors_tick`` and the
``board_sensor_state`` table) reads the live inputs (running tasks, recent run
outcomes, the budget meter), calls the pure decision functions here, persists
the returned state, and emits the structured signal on a transition.

Keeping the decisions pure means the state machines are unit-testable without a
database, and the same transition logic the runtime executes is the logic the
tests exercise. The closed sensor vocabulary + knob-binding contract live in
:mod:`hermes_cli.kanban_launch_grammar` (``SENSOR_KINDS``).

Design invariants every sensor obeys:

* **Bounded-knob tunable.** Every threshold/window is a board ``tunables`` knob
  (resolved via :func:`resolve_sensor_knobs`), never an inline magic number, so
  the optimizer can tune it and a P5 amendment can move it.
* **Transition-only emission.** A sensor emits its signal on a *state change*
  (healthy->stalled, closed->open, ok->warn->tripped, ...), not every tick, so
  a persistent condition is recorded once. The DB layer de-dupes by comparing
  the persisted status against the freshly-computed status.
* **Best-effort.** A sensor decision is a pure function of its inputs; a bad
  input degrades to a safe default (the DB layer additionally wraps the whole
  tick so a sensor error can never break dispatch).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from hermes_cli.kanban_launch_grammar import (
    SENSOR_OPTIONAL_KNOBS,
    SENSOR_REQUIRED_KNOBS,
    sensor_kind as _sensor_kind,
)

# ---------------------------------------------------------------------------
# Run outcomes the circuit breaker counts as failures vs successes. These are
# the terminal ``task_runs.outcome`` values the dispatcher records.
# ---------------------------------------------------------------------------
CIRCUIT_FAILURE_OUTCOMES: frozenset[str] = frozenset(
    {"crashed", "timed_out", "spawn_failed", "failed", "gave_up"}
)
CIRCUIT_SUCCESS_OUTCOMES: frozenset[str] = frozenset({"completed"})

# Circuit breaker states.
CIRCUIT_CLOSED = "closed"
CIRCUIT_OPEN = "open"
CIRCUIT_HALF_OPEN = "half_open"

# Heartbeat statuses.
HEARTBEAT_HEALTHY = "healthy"
HEARTBEAT_STALLED = "stalled"

# Budget levels.
BUDGET_OK = "ok"
BUDGET_WARN = "warn"
BUDGET_TRIPPED = "tripped"


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _tunable_default(tunables: dict[str, Any], name: str) -> Optional[float]:
    """Read the numeric ``default`` of a board tunable knob, or None."""
    spec = tunables.get(name) if isinstance(tunables, dict) else None
    if isinstance(spec, dict):
        return _as_number(spec.get("default"))
    return _as_number(spec)


def resolve_sensor_knobs(
    sensor: dict[str, Any], tunables: dict[str, Any]
) -> dict[str, Optional[float]]:
    """Resolve the CURRENT numeric value of each of a sensor's logical knobs.

    Reads the bound tunable's ``default`` for every logical knob the sensor
    declares (required + optional). A binding that does not resolve to a number
    maps to ``None`` -- callers treat that knob as unset and fall back to a safe
    default or skip the check. This is the single place the runtime turns a
    sensor's knob *bindings* into the live threshold values it evaluates against,
    so a knob the optimizer moved is honoured on the very next tick.
    """
    kind = _sensor_kind(sensor) or ""
    knobs = sensor.get("knobs") if isinstance(sensor.get("knobs"), dict) else {}
    wanted = SENSOR_REQUIRED_KNOBS.get(kind, ()) + SENSOR_OPTIONAL_KNOBS.get(kind, ())
    resolved: dict[str, Optional[float]] = {}
    for logical in wanted:
        binding = knobs.get(logical)
        resolved[logical] = (
            _tunable_default(tunables, binding) if isinstance(binding, str) else None
        )
    return resolved


@dataclass
class SensorDecision:
    """The result of evaluating one sensor against its inputs.

    ``status`` is the new persisted status. ``state`` is the JSON-able internal
    counter/timestamp blob to persist. ``transitioned`` is True when the status
    changed from ``prev_status`` (the DB layer emits the signal only then).
    ``signal`` is the structured payload to record. ``blocking`` is True when
    the sensor currently gates dispatch (circuit open / budget over).
    """

    status: str
    state: dict[str, Any] = field(default_factory=dict)
    transitioned: bool = False
    signal: dict[str, Any] = field(default_factory=dict)
    blocking: bool = False


# ---------------------------------------------------------------------------
# Sensor 1 -- Heartbeat / liveness + stall detector (pure decision).
# ---------------------------------------------------------------------------
def heartbeat_decision(
    *,
    prev_status: Optional[str],
    now: int,
    last_progress_at: Optional[int],
    stall_timeout: Optional[float],
    heartbeat_interval: Optional[float] = None,
    max_missed_beats: Optional[float] = None,
) -> SensorDecision:
    """Decide whether a watched entity is stalled.

    ``last_progress_at`` is the most recent moment the entity made progress
    (its last heartbeat, or its claim/start time if it never heartbeated). The
    entity is STALLED when the gap since then exceeds the effective stall
    window; otherwise HEALTHY. The effective window is ``stall_timeout``, unless
    a ``heartbeat_interval`` and ``max_missed_beats`` are both bound -- then
    ``heartbeat_interval * max_missed_beats`` is used when it is larger (missing
    N beats is the same stall expressed in beats rather than seconds).
    """
    effective = stall_timeout if stall_timeout and stall_timeout > 0 else None
    if heartbeat_interval and heartbeat_interval > 0 and max_missed_beats and max_missed_beats > 0:
        by_beats = heartbeat_interval * max_missed_beats
        effective = by_beats if effective is None else max(effective, by_beats)
    if effective is None:
        # No usable threshold -> cannot judge; treat as healthy (no-op).
        return SensorDecision(status=HEARTBEAT_HEALTHY)
    gap = None
    if last_progress_at is not None:
        gap = max(0, int(now) - int(last_progress_at))
    stalled = gap is not None and gap > effective
    status = HEARTBEAT_STALLED if stalled else HEARTBEAT_HEALTHY
    prev = prev_status or HEARTBEAT_HEALTHY
    transitioned = status != prev
    return SensorDecision(
        status=status,
        state={"last_progress_at": last_progress_at, "gap_seconds": gap},
        transitioned=transitioned,
        signal={
            "transition": f"{prev}->{status}" if transitioned else status,
            "gap_seconds": gap,
            "stall_timeout": effective,
        },
    )


# ---------------------------------------------------------------------------
# Sensor 2 -- Circuit breaker / anomaly detector (pure decision).
# ---------------------------------------------------------------------------
def circuit_decision(
    *,
    prev_state: Optional[str],
    now: int,
    opened_at: Optional[int],
    failures: int,
    successes: int,
    failure_rate_threshold: Optional[float],
    min_samples: Optional[float],
    cooldown: Optional[float],
) -> SensorDecision:
    """Advance the breaker state machine: closed -> open -> half_open -> closed.

    ``failures``/``successes`` are the counts over the rolling window the DB
    layer measured (recent ended runs on the gated path). The transitions:

    * **closed -> open**: enough samples (``>= min_samples``) and the failure
      rate crossed ``failure_rate_threshold``. Records ``opened_at`` so the
      cooldown can be timed. While open, dispatch on the path is blocked.
    * **open -> half_open**: the cooldown elapsed. A single probe is allowed
      through (dispatch is NOT blocked in half-open) to test recovery.
    * **half_open -> closed**: the probe window shows the failure rate back
      below threshold (with samples) -> recovered.
    * **half_open -> open**: the probe still fails -> re-open and restart the
      cooldown (escalation: the owner keeps being notified each re-open).

    Insufficient samples never trip the breaker (you cannot anomaly-detect on
    noise), which is why ``min_samples`` is a bounded knob.
    """
    prev = prev_state or CIRCUIT_CLOSED
    total = int(failures) + int(successes)
    rate = (float(failures) / total) if total > 0 else 0.0
    thr = failure_rate_threshold if failure_rate_threshold is not None else 1.0
    min_n = int(min_samples) if min_samples and min_samples > 0 else 1
    cool = int(cooldown) if cooldown and cooldown > 0 else 0
    enough = total >= min_n
    tripping = enough and rate >= thr

    state: dict[str, Any] = {
        "failures": int(failures),
        "successes": int(successes),
        "total": total,
        "failure_rate": round(rate, 4),
        "opened_at": opened_at,
    }

    def _decision(new_state: str, *, opened: Optional[int]) -> SensorDecision:
        state["opened_at"] = opened
        transitioned = new_state != prev
        return SensorDecision(
            status=new_state,
            state=state,
            transitioned=transitioned,
            signal={
                "transition": f"{prev}->{new_state}" if transitioned else new_state,
                "failure_rate": round(rate, 4),
                "failures": int(failures),
                "total": total,
                "threshold": thr,
                "min_samples": min_n,
            },
            blocking=(new_state == CIRCUIT_OPEN),
        )

    if prev == CIRCUIT_CLOSED:
        if tripping:
            return _decision(CIRCUIT_OPEN, opened=int(now))
        return _decision(CIRCUIT_CLOSED, opened=None)

    if prev == CIRCUIT_OPEN:
        elapsed = (int(now) - int(opened_at)) if opened_at is not None else cool
        if elapsed >= cool:
            # Cooldown elapsed -> probe (half-open). Keep opened_at for audit.
            return _decision(CIRCUIT_HALF_OPEN, opened=opened_at)
        return _decision(CIRCUIT_OPEN, opened=opened_at)

    # prev == half_open: the probe outcome decides.
    if tripping:
        return _decision(CIRCUIT_OPEN, opened=int(now))
    if enough:
        return _decision(CIRCUIT_CLOSED, opened=None)
    # Not enough probe evidence yet -> hold half-open (do not flap closed).
    return _decision(CIRCUIT_HALF_OPEN, opened=opened_at)


# ---------------------------------------------------------------------------
# Sensor 3 -- Budget / rate meter + pacing (pure decision).
# ---------------------------------------------------------------------------
# Budget over-lifetime level: a cumulative (non-resetting) ceiling was hit. It
# is distinct from per-window ``tripped`` because a window roll can never clear
# it -- only an owner raising/removing the lifetime_cap can.
BUDGET_OVER_LIFETIME = "over_lifetime"


def budget_decision(
    *,
    prev_level: Optional[str],
    now: int,
    window_start: Optional[int],
    spend: float,
    requests: float,
    budget_cap: Optional[float],
    rate_limit: Optional[float],
    warn_fraction: Optional[float],
    window: Optional[float],
    lifetime_spend: Optional[float] = None,
    lifetime_cap: Optional[float] = None,
) -> SensorDecision:
    """Meter spend + request-rate against the per-window cap and emit warn/trip.

    The meter rolls per ``window`` seconds: when the window elapses the spend
    and request counters reset to 0 and the window restarts at ``now``. Usage
    is the worse of the spend fraction and the rate fraction; the level is
    ``tripped`` at >= 1.0, ``warn`` at >= ``warn_fraction``, else ``ok``.

    ``blocking`` is True when over the cap or over the rate limit (the dispatch
    gate must throttle new spawns); the returned ``state`` carries a
    ``pacing`` hint (allow + a suggested delay to the next window) the
    dispatcher can honour.

    D3 cumulative ceiling: ``lifetime_spend`` is a NON-resetting accumulator the
    DB layer maintains across every window roll; ``lifetime_cap`` is an optional
    bounded knob. When lifetime spend >= lifetime_cap the meter blocks at level
    ``over_lifetime`` INDEPENDENT of the window -- a window roll cannot clear it
    (only the owner raising the cap can). Purely additive: when no
    ``lifetime_cap`` is declared the lifetime check is skipped and the meter
    behaves exactly as before.
    """
    win = int(window) if window and window > 0 else 0
    start = int(window_start) if window_start is not None else int(now)
    cur_spend = float(spend)
    cur_requests = float(requests)
    # Roll the window if it elapsed.
    reset = False
    if win > 0 and int(now) - start >= win:
        start = int(now)
        cur_spend = 0.0
        cur_requests = 0.0
        reset = True

    cap = budget_cap if budget_cap and budget_cap > 0 else None
    rl = rate_limit if rate_limit and rate_limit > 0 else None
    spend_frac = (cur_spend / cap) if cap else 0.0
    rate_frac = (cur_requests / rl) if rl else 0.0
    usage = max(spend_frac, rate_frac)
    warn_at = warn_fraction if warn_fraction is not None and warn_fraction > 0 else 0.8

    if usage >= 1.0:
        level = BUDGET_TRIPPED
    elif usage >= warn_at:
        level = BUDGET_WARN
    else:
        level = BUDGET_OK

    over_budget = bool(cap and cur_spend >= cap)
    over_rate = bool(rl and cur_requests >= rl)

    # D3 cumulative lifetime ceiling (additive; only active when a cap is set).
    lcap = lifetime_cap if lifetime_cap and lifetime_cap > 0 else None
    life_spend = float(lifetime_spend) if lifetime_spend is not None else 0.0
    over_lifetime = bool(lcap and life_spend >= lcap)
    lifetime_frac = (life_spend / lcap) if lcap else 0.0
    if over_lifetime:
        # over_lifetime supersedes the per-window level -- a window roll must not
        # mask a breached lifetime ceiling. usage reflects the worse of the two.
        level = BUDGET_OVER_LIFETIME
        usage = max(usage, lifetime_frac)

    blocking = over_budget or over_rate or over_lifetime

    # Pacing: when blocked, advise delaying until the window rolls. A lifetime
    # breach can NEVER be cleared by waiting (no window rolls it), so advise no
    # delay (allow stays False -- the gate still blocks; the owner must act).
    delay = 0
    if blocking and not over_lifetime and win > 0:
        delay = max(0, (start + win) - int(now))
    pacing = {"allow": not blocking, "delay_seconds": delay}

    prev = prev_level or BUDGET_OK
    # A window reset that drops us back to ok is itself a (recovery) transition
    # worth surfacing once; otherwise emit only when the level changes.
    transitioned = level != prev
    state = {
        "window_start": start,
        "spend": round(cur_spend, 6),
        "requests": cur_requests,
        "usage_fraction": round(usage, 4),
        "level": level,
        "over_budget": over_budget,
        "over_rate": over_rate,
        "pacing": pacing,
        "window_reset": reset,
        "lifetime_spend": round(life_spend, 6),
        "over_lifetime": over_lifetime,
    }
    if lcap is not None:
        state["lifetime_cap"] = lcap
        state["lifetime_fraction"] = round(lifetime_frac, 4)
    return SensorDecision(
        status=level,
        state=state,
        transitioned=transitioned,
        signal={
            "transition": f"{prev}->{level}" if transitioned else level,
            "usage_fraction": round(usage, 4),
            "spend": round(cur_spend, 6),
            "requests": cur_requests,
            "budget_cap": cap,
            "rate_limit": rl,
            "over_budget": over_budget,
            "over_rate": over_rate,
            "lifetime_spend": round(life_spend, 6),
            "lifetime_cap": lcap,
            "over_lifetime": over_lifetime,
        },
        blocking=blocking,
    )
