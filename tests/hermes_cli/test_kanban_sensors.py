"""Tests for the Tier-1 sensor primitives.

Covers all four layers of the feature:

* the closed sensor grammar (kanban_launch_grammar) -- typed kind + knob map,
* the pure state-machine decisions (kanban_sensors) -- heartbeat/circuit/budget,
* the structural invariants (kanban_launch_invariants) -- typed/bounded/gated,
* the DB-side runtime (kanban_db) -- sensors_tick, the per-window budget meter,
  dispatch gating, the read-model surface, and signal/retention behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_sensors as ks
from hermes_cli.kanban_launch_grammar import (
    SENSOR_KINDS,
    normalize_sensor,
    normalize_sensors,
)
from hermes_cli.kanban_launch_invariants import check_contract_invariants


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


# ===========================================================================
# Grammar
# ===========================================================================
def test_sensor_kinds_vocabulary():
    assert SENSOR_KINDS == {"heartbeat", "circuit_breaker", "budget"}


def test_normalize_sensor_validates_kind_and_knobs():
    out = normalize_sensor({
        "kind": "Heartbeat",
        "knobs": {"stall_timeout": "stall_timeout_seconds", "bad": 5},
    })
    assert out["kind"] == "heartbeat"
    assert out["key"] == "heartbeat"  # back-filled from kind
    # Non-string knob bindings are dropped; string bindings kept.
    assert out["knobs"] == {"stall_timeout": "stall_timeout_seconds"}


def test_normalize_sensor_rejects_unknown_kind():
    with pytest.raises(ValueError):
        normalize_sensor({"kind": "smoke_alarm"})
    with pytest.raises(ValueError):
        normalize_sensor({"key": "x"})  # missing kind


def test_normalize_sensors_accepts_single_or_list():
    one = normalize_sensors({"kind": "budget", "key": "spend"})
    assert len(one) == 1 and one[0]["kind"] == "budget"
    many = normalize_sensors([
        {"kind": "budget"}, {"kind": "circuit_breaker", "gates_side_effect_class": "external_reversible"}
    ])
    assert [s["kind"] for s in many] == ["budget", "circuit_breaker"]
    assert many[1]["gates_side_effect_class"] == "external_reversible"


# ===========================================================================
# Pure decisions -- heartbeat
# ===========================================================================
def test_heartbeat_healthy_within_window():
    d = ks.heartbeat_decision(
        prev_status=None, now=1000, last_progress_at=950, stall_timeout=100
    )
    assert d.status == ks.HEARTBEAT_HEALTHY
    assert not d.transitioned


def test_heartbeat_transitions_to_stalled():
    d = ks.heartbeat_decision(
        prev_status=ks.HEARTBEAT_HEALTHY, now=1000, last_progress_at=800, stall_timeout=100
    )
    assert d.status == ks.HEARTBEAT_STALLED
    assert d.transitioned
    assert d.signal["transition"] == "healthy->stalled"


def test_heartbeat_recovers():
    d = ks.heartbeat_decision(
        prev_status=ks.HEARTBEAT_STALLED, now=1000, last_progress_at=990, stall_timeout=100
    )
    assert d.status == ks.HEARTBEAT_HEALTHY
    assert d.transitioned and d.signal["transition"] == "stalled->healthy"


def test_heartbeat_uses_missed_beats_when_larger():
    # heartbeat_interval * max_missed_beats = 60*5 = 300 > stall_timeout 100.
    d = ks.heartbeat_decision(
        prev_status=None, now=1000, last_progress_at=750, stall_timeout=100,
        heartbeat_interval=60, max_missed_beats=5,
    )
    # gap=250 < effective 300 -> still healthy.
    assert d.status == ks.HEARTBEAT_HEALTHY


# ===========================================================================
# Pure decisions -- circuit breaker
# ===========================================================================
def test_circuit_stays_closed_below_threshold():
    d = ks.circuit_decision(
        prev_state=None, now=0, opened_at=None, failures=1, successes=9,
        failure_rate_threshold=0.5, min_samples=3, cooldown=300,
    )
    assert d.status == ks.CIRCUIT_CLOSED
    assert not d.blocking


def test_circuit_does_not_trip_below_min_samples():
    # 2 of 2 failures (rate 1.0) but min_samples=3 -> not enough evidence.
    d = ks.circuit_decision(
        prev_state=None, now=0, opened_at=None, failures=2, successes=0,
        failure_rate_threshold=0.5, min_samples=3, cooldown=300,
    )
    assert d.status == ks.CIRCUIT_CLOSED


def test_circuit_trips_open_and_blocks():
    d = ks.circuit_decision(
        prev_state=ks.CIRCUIT_CLOSED, now=500, opened_at=None, failures=6, successes=2,
        failure_rate_threshold=0.5, min_samples=3, cooldown=300,
    )
    assert d.status == ks.CIRCUIT_OPEN
    assert d.transitioned and d.blocking
    assert d.state["opened_at"] == 500


def test_circuit_half_opens_after_cooldown_then_closes():
    # Open at t=0; cooldown 300; at t=400 cooldown elapsed -> half_open.
    half = ks.circuit_decision(
        prev_state=ks.CIRCUIT_OPEN, now=400, opened_at=0, failures=0, successes=0,
        failure_rate_threshold=0.5, min_samples=3, cooldown=300,
    )
    assert half.status == ks.CIRCUIT_HALF_OPEN
    assert not half.blocking  # half-open lets a probe through
    # Probe succeeds (enough samples, low rate) -> closed.
    closed = ks.circuit_decision(
        prev_state=ks.CIRCUIT_HALF_OPEN, now=500, opened_at=0, failures=0, successes=5,
        failure_rate_threshold=0.5, min_samples=3, cooldown=300,
    )
    assert closed.status == ks.CIRCUIT_CLOSED


def test_circuit_stays_open_before_cooldown():
    d = ks.circuit_decision(
        prev_state=ks.CIRCUIT_OPEN, now=100, opened_at=0, failures=0, successes=0,
        failure_rate_threshold=0.5, min_samples=3, cooldown=300,
    )
    assert d.status == ks.CIRCUIT_OPEN and d.blocking


# ===========================================================================
# Pure decisions -- budget
# ===========================================================================
def test_budget_levels_ok_warn_tripped():
    ok = ks.budget_decision(
        prev_level=None, now=0, window_start=0, spend=10, requests=0,
        budget_cap=100, rate_limit=None, warn_fraction=0.8, window=1000,
    )
    assert ok.status == ks.BUDGET_OK and not ok.blocking
    warn = ks.budget_decision(
        prev_level=ks.BUDGET_OK, now=0, window_start=0, spend=85, requests=0,
        budget_cap=100, rate_limit=None, warn_fraction=0.8, window=1000,
    )
    assert warn.status == ks.BUDGET_WARN and warn.transitioned
    tripped = ks.budget_decision(
        prev_level=ks.BUDGET_WARN, now=0, window_start=0, spend=100, requests=0,
        budget_cap=100, rate_limit=None, warn_fraction=0.8, window=1000,
    )
    assert tripped.status == ks.BUDGET_TRIPPED
    assert tripped.blocking and tripped.state["over_budget"]


def test_budget_window_reset_clears_counters():
    d = ks.budget_decision(
        prev_level=ks.BUDGET_TRIPPED, now=2000, window_start=0, spend=100, requests=50,
        budget_cap=100, rate_limit=10, warn_fraction=0.8, window=1000,
    )
    # now - window_start = 2000 >= window 1000 -> reset.
    assert d.state["window_reset"] is True
    assert d.state["spend"] == 0.0 and d.state["requests"] == 0.0
    assert d.status == ks.BUDGET_OK


def test_budget_over_rate_blocks_with_pacing_delay():
    d = ks.budget_decision(
        prev_level=None, now=100, window_start=0, spend=0, requests=5,
        budget_cap=None, rate_limit=5, warn_fraction=0.8, window=1000,
    )
    assert d.blocking and d.state["over_rate"]
    assert d.state["pacing"]["allow"] is False
    assert d.state["pacing"]["delay_seconds"] == 900  # (0+1000) - 100


# ===========================================================================
# resolve_sensor_knobs
# ===========================================================================
def test_resolve_sensor_knobs_reads_tunable_defaults():
    sensor = {
        "kind": "budget",
        "knobs": {
            "budget_cap": "cap", "rate_limit": "rl",
            "warn_fraction": "wf", "window": "win",
        },
    }
    tunables = {
        "cap": {"default": 250, "range": [1, 1000]},
        "rl": {"default": 7, "range": [1, 100]},
        "wf": {"default": 0.75, "range": [0.1, 1.0]},
        "win": {"default": 3600, "range": [60, 86400]},
    }
    resolved = ks.resolve_sensor_knobs(sensor, tunables)
    assert resolved == {"budget_cap": 250.0, "rate_limit": 7.0,
                        "warn_fraction": 0.75, "window": 3600.0}


# ===========================================================================
# Invariants
# ===========================================================================
def _sensor_invariant_base() -> dict:
    return {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "tunables": {
            "stall_timeout_seconds": {"default": 1800, "range": [60, 86400]},
            "heartbeat_interval_seconds": {"default": 300, "range": [30, 3600]},
        },
        "sensors": [
            {
                "kind": "heartbeat",
                "key": "liveness",
                "knobs": {
                    "heartbeat_interval": "heartbeat_interval_seconds",
                    "stall_timeout": "stall_timeout_seconds",
                },
            }
        ],
    }


def test_invariant_valid_heartbeat_sensor_passes():
    report = check_contract_invariants(_sensor_invariant_base())
    assert report.ok, report.errors
    assert report.checked.get("sensors") == 1


def test_invariant_unknown_sensor_kind_rejected():
    contract = _sensor_invariant_base()
    contract["sensors"][0]["kind"] = "smoke_alarm"
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("unknown/missing kind" in e for e in report.errors), report.errors


def test_invariant_missing_required_knob_binding_rejected():
    contract = _sensor_invariant_base()
    contract["sensors"][0]["knobs"] = {"heartbeat_interval": "heartbeat_interval_seconds"}
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("required 'stall_timeout'" in e for e in report.errors), report.errors


def test_invariant_dangling_sensor_knob_rejected():
    contract = _sensor_invariant_base()
    contract["sensors"][0]["knobs"]["stall_timeout"] = "does_not_exist"
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("dangling sensor knob reference" in e for e in report.errors), report.errors


def test_invariant_unbounded_sensor_knob_rejected():
    contract = _sensor_invariant_base()
    contract["tunables"]["stall_timeout_seconds"] = {"default": 1800}  # no range
    report = check_contract_invariants(contract)
    assert not report.ok
    # The unbounded tunable trips both the generic R-tunable rail and the
    # sensor-specific "unsafe to tune" rail.
    assert any("unsafe" in e or "no range" in e.lower() for e in report.errors), report.errors


def test_invariant_circuit_gating_ungoverned_external_rejected():
    contract = {
        "objective": {"statement": "x"},
        "workflow": {"stages": []},
        "tunables": {
            "thr": {"default": 0.5, "range": [0.1, 1.0]},
            "win": {"default": 3600, "range": [60, 86400]},
            "cool": {"default": 600, "range": [30, 86400]},
            "mins": {"default": 5, "range": [1, 100]},
        },
        "side_effect_policy": {"allowed": ["none", "external_reversible"]},
        "sensors": [
            {
                "kind": "circuit_breaker",
                "key": "send",
                "gates_side_effect_class": "external_reversible",
                "knobs": {
                    "failure_rate_threshold": "thr", "window": "win",
                    "cooldown": "cool", "min_samples": "mins",
                },
            }
        ],
    }
    report = check_contract_invariants(contract)
    assert not report.ok
    assert any("gated path is ungoverned" in e for e in report.errors), report.errors
    # Governing it in approval_required clears the rail.
    contract["side_effect_policy"]["approval_required"] = ["external_reversible"]
    report2 = check_contract_invariants(contract)
    assert report2.ok, report2.errors


def test_insurance_fixture_with_sensors_passes_invariants():
    import json
    import os

    path = os.path.join(
        os.path.dirname(__file__), "..", "fixtures", "launch_intake",
        "insurance_recruiting.contract.json",
    )
    with open(path) as fh:
        contract = json.load(fh)
    report = check_contract_invariants(contract)
    assert report.ok, report.errors
    assert report.checked.get("sensors", 0) == 3


# ===========================================================================
# DB runtime integration
# ===========================================================================
def _sensor_contract(*, board_wide_circuit: bool = True) -> dict:
    circuit = {
        "kind": "circuit_breaker",
        "key": "dispatch",
        "knobs": {
            "failure_rate_threshold": "circuit_failure_rate_threshold",
            "window": "circuit_window_seconds",
            "cooldown": "circuit_cooldown_seconds",
            "min_samples": "circuit_min_samples",
        },
    }
    if not board_wide_circuit:
        circuit["gates_side_effect_class"] = "external_reversible"
    return {
        "objective": {"statement": "Sensor board", "success": ["x"], "failure": ["y"],
                      "constraints": ["z"]},
        "runtime": {"mode": "company", "dispatcher": {"profile": "mock-ceo"}},
        "workflow": {"id": "wf", "stages": [{"key": "execute", "actions": [{"key": "do"}]}]},
        "tunables": {
            "stall_timeout_seconds": {"default": 100, "range": [10, 86400]},
            "heartbeat_interval_seconds": {"default": 30, "range": [10, 3600]},
            "circuit_failure_rate_threshold": {"default": 0.5, "range": [0.1, 1.0]},
            "circuit_window_seconds": {"default": 1000, "range": [60, 86400]},
            "circuit_cooldown_seconds": {"default": 300, "range": [30, 86400]},
            "circuit_min_samples": {"default": 3, "range": [1, 100]},
            "budget_cap_units": {"default": 100, "range": [1, 100000]},
            "rate_limit_per_window": {"default": 2, "range": [1, 10000]},
            "budget_warn_fraction": {"default": 0.5, "range": [0.1, 1.0]},
            "budget_window_seconds": {"default": 1000, "range": [60, 86400]},
        },
        "sensors": [
            {
                "kind": "heartbeat",
                "key": "liveness",
                "knobs": {
                    "heartbeat_interval": "heartbeat_interval_seconds",
                    "stall_timeout": "stall_timeout_seconds",
                },
            },
            circuit,
            {
                "kind": "budget",
                "key": "spend",
                "knobs": {
                    "budget_cap": "budget_cap_units",
                    "rate_limit": "rate_limit_per_window",
                    "warn_fraction": "budget_warn_fraction",
                    "window": "budget_window_seconds",
                },
            },
        ],
    }


def _make_sensor_board(slug: str, **kw) -> None:
    kb.create_board(slug)
    kb.write_board_metadata(slug, business_contract=_sensor_contract(**kw))


def _insert_run(conn, *, task_id, outcome, ended_at, started_at=None):
    conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, "done", started_at if started_at is not None else ended_at, ended_at, outcome),
    )


def test_sensor_signal_kinds_registered():
    assert {"sensor_heartbeat", "sensor_circuit", "sensor_budget"} <= kb.SIGNAL_PRIMITIVE_KINDS


def test_sensors_tick_heartbeat_emits_stall_then_dedupes(fresh_home):
    _make_sensor_board("hb")
    with kb.connect(board="hb") as conn:
        tid = kb.create_task(conn, title="worker", board="hb", initial_status="blocked")
        # Force the task running with progress 500s ago (stall_timeout=100).
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', started_at=?, last_heartbeat_at=NULL "
                "WHERE id=?",
                (1000, tid),
            )
        res = kb.sensors_tick(conn, board="hb", now=1500)
        assert any(h["task_id"] == tid for h in res.get("heartbeat", []))
        states = {(s["sensor_kind"], s["entity_ref"]): s for s in kb.get_sensor_states(conn, board="hb")}
        assert states[("heartbeat", tid)]["status"] == "stalled"
        sig_before = conn.execute(
            "SELECT COUNT(*) FROM board_signals WHERE board='hb' AND primitive_kind='sensor_heartbeat'"
        ).fetchone()[0]
        assert sig_before >= 1
        # A second tick with the entity still stalled emits NO new transition.
        kb.sensors_tick(conn, board="hb", now=1600)
        sig_after = conn.execute(
            "SELECT COUNT(*) FROM board_signals WHERE board='hb' AND primitive_kind='sensor_heartbeat'"
        ).fetchone()[0]
        assert sig_after == sig_before


def test_sensors_tick_circuit_opens_blocks_dispatch_then_recovers(fresh_home):
    _make_sensor_board("cb")
    with kb.connect(board="cb") as conn:
        # A ready, dispatchable task with no special requirements.
        worker = kb.create_task(conn, title="ready work", assignee="mock-ceo", board="cb",
                                initial_status="blocked")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (worker,))
            # 4 failing runs, 1 success within the window -> rate 0.8 >= 0.5.
            for _ in range(4):
                _insert_run(conn, task_id=worker, outcome="crashed", ended_at=9990)
            _insert_run(conn, task_id=worker, outcome="completed", ended_at=9990)

        # Healthy first: dispatch is eligible.
        before = kb.evaluate_dispatch_eligibility(conn, worker, board="cb")
        assert before["ok"], before["blockers"]

        res = kb.sensors_tick(conn, board="cb", now=10000)
        assert any("closed->open" in c["transition"] for c in res.get("circuit", []))
        st = {s["sensor_kind"]: s for s in kb.get_sensor_states(conn, board="cb")}
        assert st["circuit_breaker"]["status"] == "open"

        # Dispatch is now blocked by the open breaker.
        blocked = kb.evaluate_dispatch_eligibility(conn, worker, board="cb")
        assert not blocked["ok"]
        assert any(b["code"] == "circuit_open" for b in blocked["blockers"]), blocked["blockers"]

        # After the cooldown elapses the breaker half-opens (probe allowed).
        kb.sensors_tick(conn, board="cb", now=10000 + 400)
        st2 = {s["sensor_kind"]: s for s in kb.get_sensor_states(conn, board="cb")}
        assert st2["circuit_breaker"]["status"] == "half_open"
        probe = kb.evaluate_dispatch_eligibility(conn, worker, board="cb")
        assert probe["ok"], probe["blockers"]

        # Successful probe runs in the recovery window close the breaker.
        with kb.write_txn(conn):
            for _ in range(5):
                _insert_run(conn, task_id=worker, outcome="completed", ended_at=10000 + 450)
        kb.sensors_tick(conn, board="cb", now=10000 + 500)
        st3 = {s["sensor_kind"]: s for s in kb.get_sensor_states(conn, board="cb")}
        assert st3["circuit_breaker"]["status"] == "closed"
        recovered = kb.evaluate_dispatch_eligibility(conn, worker, board="cb")
        assert recovered["ok"], recovered["blockers"]


def test_budget_consumption_trips_and_blocks_dispatch_then_resets(fresh_home):
    _make_sensor_board("bud")
    with kb.connect(board="bud") as conn:
        worker = kb.create_task(conn, title="ready work", assignee="mock-ceo", board="bud",
                                initial_status="blocked")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (worker,))

        # rate_limit is 2 per window: two spawns reaches the cap.
        kb.record_budget_consumption(conn, board="bud", requests=1.0, now=5000)
        ok_mid = kb.evaluate_dispatch_eligibility(conn, worker, board="bud")
        assert ok_mid["ok"], ok_mid["blockers"]
        meter = kb.record_budget_consumption(conn, board="bud", requests=1.0, now=5001)
        assert meter["metered"][0]["level"] == "tripped"

        blocked = kb.evaluate_dispatch_eligibility(conn, worker, board="bud")
        assert not blocked["ok"]
        bud_block = next(b for b in blocked["blockers"] if b["code"] == "budget_exceeded")
        assert bud_block["over_rate"] is True

        # A tick after the window rolls (window=1000) resets the meter to ok.
        kb.sensors_tick(conn, board="bud", now=5001 + 1001)
        st = {s["sensor_kind"]: s for s in kb.get_sensor_states(conn, board="bud")}
        assert st["budget"]["status"] == "ok"
        cleared = kb.evaluate_dispatch_eligibility(conn, worker, board="bud")
        assert cleared["ok"], cleared["blockers"]


def test_path_specific_circuit_only_blocks_matching_side_effect(fresh_home):
    _make_sensor_board("path", board_wide_circuit=False)
    with kb.connect(board="path") as conn:
        # Manually mark the path-specific breaker open.
        with kb.write_txn(conn):
            kb._write_sensor_state(
                conn, board="path", kind="circuit_breaker", key="dispatch",
                entity_ref=kb._SENSOR_BOARD_ENTITY, status="open",
                state={"gates_side_effect_class": "external_reversible", "failure_rate": 0.9},
                now=1,
            )
        # A task with no side effect on a different path is NOT blocked.
        none_blockers = kb._sensor_dispatch_blockers(conn, board="path", side_effect_class=None)
        assert not any(b["code"] == "circuit_open" for b in none_blockers)
        # A task on the gated path IS blocked.
        gated = kb._sensor_dispatch_blockers(
            conn, board="path", side_effect_class="external_reversible"
        )
        assert any(b["code"] == "circuit_open" for b in gated)


def test_sensors_surface_in_learned_state_read_model(fresh_home):
    _make_sensor_board("rm")
    with kb.connect(board="rm") as conn:
        kb.sensors_tick(conn, board="rm", now=1000)
        model = kb.build_learned_state_read_model(conn, board="rm")
        assert "sensors" in model
        declared_kinds = {d["kind"] for d in model["sensors"]["declared"]}
        assert declared_kinds == {"heartbeat", "circuit_breaker", "budget"}


def test_sensor_signals_pruned_as_telemetry_not_outcomes(fresh_home):
    _make_sensor_board("ret")
    with kb.connect(board="ret") as conn:
        with kb.write_txn(conn):
            kb.record_board_signal(
                conn, board="ret", primitive_kind="sensor_circuit",
                primitive_key="dispatch", action={"kind": "sensor_circuit"},
                ts=1000, dedupe_key="sensor_circuit:dispatch:closed->open:1000",
            )
        # Telemetry rows older than the horizon are pruned outright (not rolled
        # up like 'outcome' rows). Force a far-future prune horizon.
        pruned = kb.prune_board_retention(conn, board="ret", now=10**12)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM board_signals WHERE board='ret' AND primitive_kind='sensor_circuit'"
        ).fetchone()[0]
        assert remaining == 0
        # And nothing leaked into the learning rollup.
        rollup = conn.execute(
            "SELECT COUNT(*) FROM board_signal_rollup WHERE board='ret'"
        ).fetchone()[0]
        assert rollup == 0
