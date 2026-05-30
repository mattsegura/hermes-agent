#!/usr/bin/env python3
"""Owner-operator dogfooding harness for the Hermes kanban agentic runtime.

Drives the full launch -> compile -> tick -> sensors -> optimizer -> amendment
-> steering lifecycle against the four golden domain contracts in an isolated
HERMES_HOME, printing structured observations so rough edges surface fast.

This is a DIAGNOSTIC driver, not a test. It exercises the real product code
paths (``hermes_cli.kanban_db`` + friends) the way an operator would, with the
aux model stubbed deterministically where a live LLM would otherwise be needed.

Run: ``venv/bin/python scripts/kanban_dogfood.py [scenario ...]``
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

_WORKTREE = Path(__file__).resolve().parents[1]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

_FIXTURES = _WORKTREE / "tests" / "fixtures" / "launch_intake"
DOMAINS = ["research_team", "land_wholesaling", "insurance_recruiting", "grow_app_one_week"]


def _isolate_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="kanban_dogfood_")) / ".hermes"
    home.mkdir(parents=True)
    os.environ["HERMES_HOME"] = str(home)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        os.environ.pop(var, None)
    return home


def load_contract(domain: str) -> dict:
    with open(_FIXTURES / f"{domain}.contract.json") as fh:
        return json.load(fh)


def activate(kb, board: str, contract: dict) -> dict:
    kb.review_business_launch_contract(board, contract=contract, create_if_missing=True)
    token = kb.issue_board_launch_approval_token(
        board,
        contract=contract,
        approved_by="owner",
        approval_evidence={"source": "dogfood"},
        owner_authority_confirmed=True,
    )["token"]
    return kb.review_business_launch_contract(
        board, contract=contract, approve=True, approval_token=token, author="owner",
    )


def banner(msg: str) -> None:
    print(f"\n{'=' * 78}\n{msg}\n{'=' * 78}")


def scenario_launch_compile() -> list[str]:
    """S1: launch + compile each golden domain, report what materialized."""
    findings: list[str] = []
    from hermes_cli import kanban_db as kb

    for domain in DOMAINS:
        banner(f"S1 launch+compile :: {domain}")
        contract = load_contract(domain)
        # 1) does it even pass the public validation bar?
        v = kb.validate_business_runtime_contract(contract)
        print(f"  validate_business_runtime_contract -> ok={v.get('ok')} "
              f"missing={v.get('missing')} errors={(v.get('errors') or [])[:3]}")
        if not v.get("ok"):
            findings.append(f"[{domain}] golden contract not launch-ready (expected for "
                            f"older-schema fixtures): missing={v.get('missing')} errors={v.get('errors')}")
            # Confirm the review path degrades gracefully (no crash).
            review = kb.review_business_launch_contract(
                domain, contract=contract, create_if_missing=True,
            )
            print(f"  review (non-ready) -> ok={review.get('ok')} status={review.get('status')} "
                  f"errors={(review.get('readiness') or {}).get('errors')}")
            continue
        try:
            res = activate(kb, domain, contract)
        except Exception as exc:  # noqa: BLE001
            findings.append(f"[{domain}] activate() raised {type(exc).__name__}: {exc}")
            traceback.print_exc()
            continue
        print(f"  activate -> ok={res.get('ok')} phase={res.get('launch_phase')} "
              f"version={res.get('contract_version')} review_id={bool(res.get('launch_review_id'))}")
        if not res.get("ok"):
            findings.append(f"[{domain}] activate not ok: {res}")
        # 2) reactive runtime compiled?
        summary = kb.compile_contract_reactive_runtime(domain)
        print(f"  compile (re-run, idempotency) -> compiled={summary.get('compiled')} "
              f"reused={summary.get('reused')}")
        with kb.connect(board=domain) as conn:
            ents = conn.execute(
                "SELECT entity_type, COUNT(*) n FROM reactive_entities GROUP BY entity_type",
            ).fetchall()
            routes = conn.execute(
                "SELECT COUNT(*) n FROM task_watch_routes WHERE active=1"
            ).fetchone()
            sched = conn.execute(
                "SELECT COUNT(*) n, MIN(cadence_seconds) mn, MAX(cadence_seconds) mx "
                "FROM reactive_timer_schedules WHERE board=?", (domain,)
            ).fetchone()
            meta = kb.read_board_metadata(domain)
            gate = kb.board_dispatch_gate(domain)
            print(f"  reactive_entities: {[(r['entity_type'], r['n']) for r in ents]}")
            print(f"  active task_watch_routes: {routes['n']}")
            print(f"  timer_schedules: n={sched['n']} cadence_range="
                  f"[{sched['mn']},{sched['mx']}]s")
            print(f"  contract_version={meta.get('contract_version')} "
                  f"launch_phase={meta.get('launch_phase')}")
            gate_ok = gate.get("ok") if "ok" in gate else gate.get("allowed")
            print(f"  dispatch_gate: ok={gate_ok} reason={gate.get('reason')} "
                  f"blockers={gate.get('blockers')}")
            if not gate_ok:
                findings.append(f"[{domain}] dispatch gate CLOSED after activation: "
                                f"reason={gate.get('reason')} blockers={gate.get('blockers')}")
    return findings


def _sensor_contract() -> dict:
    """A launch-conformant contract carrying all three sensor primitives."""
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
            {"kind": "heartbeat", "key": "liveness",
             "knobs": {"heartbeat_interval": "heartbeat_interval_seconds",
                       "stall_timeout": "stall_timeout_seconds"}},
            {"kind": "circuit_breaker", "key": "dispatch",
             "knobs": {"failure_rate_threshold": "circuit_failure_rate_threshold",
                       "window": "circuit_window_seconds",
                       "cooldown": "circuit_cooldown_seconds",
                       "min_samples": "circuit_min_samples"}},
            {"kind": "budget", "key": "spend",
             "knobs": {"budget_cap": "budget_cap_units",
                       "rate_limit": "rate_limit_per_window",
                       "warn_fraction": "budget_warn_fraction",
                       "window": "budget_window_seconds"}},
        ],
    }


def scenario_sensors() -> list[str]:
    """S4: heartbeat stall, circuit breaker trip+recovery, budget exhaustion."""
    findings: list[str] = []
    import random
    from hermes_cli import kanban_db as kb

    banner("S4 sensors :: heartbeat stall + dedupe")
    kb.create_board("sx_hb")
    kb.write_board_metadata("sx_hb", business_contract=_sensor_contract())
    with kb.connect(board="sx_hb") as conn:
        tid = kb.create_task(conn, title="worker", board="sx_hb", initial_status="blocked")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running', started_at=?, "
                         "last_heartbeat_at=NULL WHERE id=?", (1000, tid))
        kb.sensors_tick(conn, board="sx_hb", now=1500)
        n1 = conn.execute("SELECT COUNT(*) FROM board_signals WHERE primitive_kind='sensor_heartbeat'").fetchone()[0]
        # 5 more ticks while still stalled -> must NOT emit new transition signals.
        for t in range(1600, 2100, 100):
            kb.sensors_tick(conn, board="sx_hb", now=t)
        n2 = conn.execute("SELECT COUNT(*) FROM board_signals WHERE primitive_kind='sensor_heartbeat'").fetchone()[0]
        print(f"  heartbeat signals after first stall={n1}, after 5 more ticks={n2}")
        if n2 != n1:
            findings.append(f"[sensors] heartbeat emitted {n2-n1} extra signals while persistently stalled (should dedupe)")

    banner("S4 sensors :: circuit breaker trip -> half-open -> close + dispatch gating")
    kb.create_board("sx_cb")
    kb.write_board_metadata("sx_cb", business_contract=_sensor_contract())
    with kb.connect(board="sx_cb") as conn:
        worker = kb.create_task(conn, title="ready work", assignee="mock-ceo", board="sx_cb", initial_status="blocked")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (worker,))
            for _ in range(4):
                conn.execute("INSERT INTO task_runs (task_id,status,started_at,ended_at,outcome) VALUES (?,?,?,?,?)",
                             (worker, "done", 9990, 9990, "crashed"))
            conn.execute("INSERT INTO task_runs (task_id,status,started_at,ended_at,outcome) VALUES (?,?,?,?,?)",
                         (worker, "done", 9990, 9990, "completed"))
        before = kb.evaluate_dispatch_eligibility(conn, worker, board="sx_cb")
        kb.sensors_tick(conn, board="sx_cb", now=10000)
        blocked = kb.evaluate_dispatch_eligibility(conn, worker, board="sx_cb")
        blocked_codes = {b["code"] for b in blocked["blockers"]}
        print(f"  before trip ok={before['ok']}; after trip ok={blocked['ok']} codes={blocked_codes}")
        if before["ok"] and "circuit_open" not in blocked_codes:
            findings.append("[sensors] circuit opened but dispatch NOT gated by circuit_open")
        kb.sensors_tick(conn, board="sx_cb", now=10400)
        probe = kb.evaluate_dispatch_eligibility(conn, worker, board="sx_cb")
        with kb.connect(board="sx_cb") as c2:
            pass
        with kb.write_txn(conn):
            for _ in range(5):
                conn.execute("INSERT INTO task_runs (task_id,status,started_at,ended_at,outcome) VALUES (?,?,?,?,?)",
                             (worker, "done", 10450, 10450, "completed"))
        kb.sensors_tick(conn, board="sx_cb", now=10500)
        recovered = kb.evaluate_dispatch_eligibility(conn, worker, board="sx_cb")
        print(f"  half-open probe ok={probe['ok']}; recovered ok={recovered['ok']}")
        if not probe["ok"]:
            findings.append("[sensors] half-open did not allow probe dispatch")
        if not recovered["ok"]:
            findings.append("[sensors] breaker did not close after successful probes")

    banner("S4 sensors :: budget exhaustion + window reset")
    kb.create_board("sx_bud")
    kb.write_board_metadata("sx_bud", business_contract=_sensor_contract())
    with kb.connect(board="sx_bud") as conn:
        worker = kb.create_task(conn, title="ready work", assignee="mock-ceo", board="sx_bud", initial_status="blocked")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (worker,))
        kb.record_budget_consumption(conn, board="sx_bud", requests=1.0, now=5000)
        kb.record_budget_consumption(conn, board="sx_bud", requests=1.0, now=5001)
        blocked = kb.evaluate_dispatch_eligibility(conn, worker, board="sx_bud")
        print(f"  after rate cap reached: dispatch ok={blocked['ok']} "
              f"codes={ {b['code'] for b in blocked['blockers']} }")
        if blocked["ok"]:
            findings.append("[sensors] budget rate cap reached but dispatch not blocked")
        kb.sensors_tick(conn, board="sx_bud", now=5001 + 1001)
        cleared = kb.evaluate_dispatch_eligibility(conn, worker, board="sx_bud")
        print(f"  after window reset: dispatch ok={cleared['ok']}")
        if not cleared["ok"]:
            findings.append("[sensors] budget window did not reset to allow dispatch")
    return findings


def scenario_optimizer() -> list[str]:
    """S5: posterior convergence, auto-apply, warm-start, anti-poison."""
    findings: list[str] = []
    import random
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_optimizer as opt

    KNOB = "follow_up_interval_hours"

    def contract(spec):
        return {
            "objective": {"statement": "Optimize cadence", "success": ["x"], "failure": ["y"],
                          "constraints": ["z"]},
            "runtime": {"mode": "company", "dispatcher": {"profile": "mock-ceo"}},
            "workflow": {"id": "f", "stages": [{"key": "execute", "actions": [{"key": "nudge"}]}]},
            "tunables": {KNOB: spec},
        }

    banner("S5 optimizer :: convergence + auto-apply + throttle")
    kb.create_board("ox")
    kb.write_board_metadata("ox", business_contract=contract({"default": 72, "allowed": [48, 72]}))
    base = 2_000_000
    with kb.connect(board="ox") as conn:
        for i in range(30):
            for value, rv in ((48, 1.0), (72, 0.0)):
                with kb.write_txn(conn):
                    kb.record_board_signal(conn, board="ox", primitive_kind="outcome",
                                           primitive_key="seller_follow_up", knob_snapshot={KNOB: value},
                                           reward_value=rv, reward_kind="conversion",
                                           realized_at=base + i, ts=base + i)
        res = kb.optimizer_tick(conn, board="ox", now=base + 100, rng=random.Random(0))
        applied = [a["new_value"] for a in res["applied"]]
        print(f"  optimizer applied -> {applied}")
        if applied != [48]:
            findings.append(f"[optimizer] expected convergence to 48, got {applied}")
        res2 = kb.optimizer_tick(conn, board="ox", now=base + 200, rng=random.Random(0))
        if res2["applied"]:
            findings.append(f"[optimizer] second tick applied despite throttle: {res2['applied']}")
        else:
            print(f"  second tick throttled (reasons={[s.get('reason') for s in res2['skipped']]})")

    banner("S5 optimizer :: exactly-once reward attribution (replay same outcome rows)")
    # Re-run optimizer_tick many times; the posterior must be a function of the
    # outcome rows, not double-count on repeated ticks.
    with kb.connect(board="ox") as conn:
        family, ev = opt.read_knob_outcome_evidence(conn, board="ox", knob=KNOB,
                                                    spec={"default": 48, "allowed": [48, 72]})
        n48 = ev[opt._arm_key(48)].n
        for _ in range(5):
            kb.optimizer_tick(conn, board="ox", now=base + 300, rng=random.Random(1))
        family2, ev2 = opt.read_knob_outcome_evidence(conn, board="ox", knob=KNOB,
                                                      spec={"default": 48, "allowed": [48, 72]})
        n48b = ev2[opt._arm_key(48)].n
        print(f"  evidence count arm48 before={n48} after 5 ticks={n48b}")
        if n48 != n48b:
            findings.append(f"[optimizer] reward evidence changed across idle ticks ({n48}->{n48b}); not exactly-once")
    return findings


def scenario_amendment() -> list[str]:
    """S6: amendment loop incl. validation-failure leaves contract untouched."""
    findings: list[str] = []
    from hermes_cli import kanban_db as kb

    base = load_contract("insurance_recruiting")

    def activate(board):
        kb.review_business_launch_contract(board, contract=base, create_if_missing=True)
        tok = kb.issue_board_launch_approval_token(board, contract=base, approved_by="owner",
                                                   approval_evidence={"s": "x"}, owner_authority_confirmed=True)["token"]
        kb.review_business_launch_contract(board, contract=base, approve=True, approval_token=tok)

    import copy
    banner("S6 amendment :: owner-initiated widen -> approve -> validate -> mint")
    activate("am1")
    proposed = copy.deepcopy(base)
    # widen max_nudges range if present, else bump a tunable default in-range
    tun = proposed.get("tunables", {})
    target = None
    for k, spec in tun.items():
        if isinstance(spec, dict) and "range" in spec:
            target = k
            spec["range"] = [spec["range"][0], spec["range"][1] + 5]
            break
    print(f"  widening tunable: {target}")
    with kb.connect(board="am1") as conn:
        amd = kb.propose_contract_amendment(conn, board="am1", proposed_contract=proposed,
                                            rationale="dogfood widen", origin="owner")
        aid = amd["amendment_id"]
        tok = kb.issue_board_launch_approval_token("am1", amendment_id=aid, approved_by="owner",
                                                   approval_evidence={"s": "x"}, owner_authority_confirmed=True)["token"]
        kb.approve_contract_amendment(conn, aid, board="am1", approver="owner", token=tok)
        validated = kb.validate_contract_amendment(conn, aid, board="am1")
        print(f"  validated status={validated['status']}")
        minted = kb.mint_contract_amendment(conn, aid, board="am1")
        print(f"  minted status={minted['status']} version={minted.get('minted_version')}")
        if minted["status"] != kb.AMENDMENT_STATUS_ACTIVE or minted.get("minted_version") != 2:
            findings.append(f"[amendment] mint did not bump to version 2 active: {minted}")
    after = kb.read_board_metadata("am1")["contract_version"]
    if after != 2:
        findings.append(f"[amendment] contract_version not bumped to 2 (got {after})")

    banner("S6 amendment :: validation-failing proposal leaves live contract untouched")
    activate("am2")
    bad = copy.deepcopy(base)
    # remove a tunable range -> unbounded knob -> invariant failure
    for k, spec in bad.get("tunables", {}).items():
        if isinstance(spec, dict) and "range" in spec:
            spec.pop("range")
            break
    with kb.connect(board="am2") as conn:
        amd = kb.propose_contract_amendment(conn, board="am2", proposed_contract=bad,
                                            rationale="dogfood bad", origin="owner")
        aid = amd["amendment_id"]
        tok = kb.issue_board_launch_approval_token("am2", amendment_id=aid, approved_by="owner",
                                                   approval_evidence={"s": "x"}, owner_authority_confirmed=True)["token"]
        kb.approve_contract_amendment(conn, aid, board="am2", approver="owner", token=tok)
        failed = kb.validate_contract_amendment(conn, aid, board="am2")
        print(f"  validation status={failed['status']} errors={(failed.get('validation_errors') or [])[:2]}")
        if failed["status"] != kb.AMENDMENT_STATUS_VALIDATION_FAILED:
            findings.append(f"[amendment] bad proposal did not fail validation: {failed['status']}")
    v = kb.read_board_metadata("am2")["contract_version"]
    if v != 1:
        findings.append(f"[amendment] live contract mutated despite validation failure (version={v})")
    else:
        print(f"  live contract untouched (version={v})")
    return findings


def scenario_perf() -> list[str]:
    """S8a: volume/perf -- many tasks, ticks, signals; watch for superlinear scaling."""
    findings: list[str] = []
    import random
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_optimizer as opt

    KNOB = "follow_up_interval_hours"
    contract = {
        "objective": {"statement": "Perf board", "success": ["x"], "failure": ["y"], "constraints": ["z"]},
        "runtime": {"mode": "company", "dispatcher": {"profile": "mock-ceo"}},
        "workflow": {"id": "f", "stages": [{"key": "execute", "actions": [{"key": "nudge"}]}]},
        "tunables": {KNOB: {"default": 72, "allowed": [48, 72]}},
    }
    kb.create_board("perf")
    kb.write_board_metadata("perf", business_contract=contract)

    timings: dict[str, float] = {}
    # 1) Bulk-create many tasks; measure list_tasks scaling.
    with kb.connect(board="perf") as conn:
        t0 = time.perf_counter()
        for i in range(500):
            kb.create_task(conn, title=f"task {i}", board="perf", assignee="mock-ceo",
                           initial_status="blocked")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready'")
        timings["create_500_tasks"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        tasks = kb.list_tasks(conn)
        timings["list_tasks(500)"] = time.perf_counter() - t0
        print(f"  created {len(tasks)} tasks")

        # 2) Emit a large signal volume, then time optimizer evidence read.
        t0 = time.perf_counter()
        with kb.write_txn(conn):
            for i in range(2000):
                value = 48 if i % 2 == 0 else 72
                kb.record_board_signal(conn, board="perf", primitive_kind="outcome",
                                       primitive_key="seller_follow_up", knob_snapshot={KNOB: value},
                                       reward_value=1.0 if value == 48 else 0.0, reward_kind="conversion",
                                       realized_at=1_000_000 + i, ts=1_000_000 + i,
                                       dedupe_key=f"o:{i}")
        timings["emit_2000_signals"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        family, ev = opt.read_knob_outcome_evidence(conn, board="perf", knob=KNOB,
                                                    spec={"default": 72, "allowed": [48, 72]})
        timings["read_evidence(2000)"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        kb.optimizer_tick(conn, board="perf", now=1_100_000, rng=random.Random(0))
        timings["optimizer_tick(2000 signals)"] = time.perf_counter() - t0

        # 3) dispatch_once dry-run over 500 ready tasks.
        t0 = time.perf_counter()
        kb.dispatch_once(conn, dry_run=True, board="perf")
        timings["dispatch_once_dry(500)"] = time.perf_counter() - t0

        # 4) retention prune over a large signal table.
        t0 = time.perf_counter()
        pruned = kb.prune_board_retention(conn, board="perf", now=10**12)
        timings["prune_retention(2000)"] = time.perf_counter() - t0

        sig_count = conn.execute("SELECT COUNT(*) FROM board_signals WHERE board='perf'").fetchone()[0]
        rollup_count = conn.execute("SELECT COUNT(*) FROM board_signal_rollup WHERE board='perf'").fetchone()[0]

    print("  timings (s):")
    for k, v in timings.items():
        print(f"    {k:34s} {v*1000:8.2f} ms")
    print(f"  after prune: signals={sig_count} rollup={rollup_count}")
    # Heuristic thresholds: nothing here should take seconds.
    for k, v in timings.items():
        if v > 2.0:
            findings.append(f"[perf] {k} took {v:.2f}s (>2s) -- possible hotspot")
    # Retention must bound growth: outcome rows should roll up, not grow unbounded.
    if sig_count > 2000:
        findings.append(f"[perf] signal table grew to {sig_count} after prune (unbounded?)")
    return findings


def scenario_restart() -> list[str]:
    """S8b: durable state survives a fresh connection (sessions, amendments, schedules)."""
    findings: list[str] = []
    import copy
    from hermes_cli import kanban_db as kb

    base = load_contract("insurance_recruiting")
    kb.review_business_launch_contract("rst", contract=base, create_if_missing=True)
    tok = kb.issue_board_launch_approval_token("rst", contract=base, approved_by="owner",
                                               approval_evidence={"s": "x"}, owner_authority_confirmed=True)["token"]
    kb.review_business_launch_contract("rst", contract=base, approve=True, approval_token=tok)

    banner("S8b restart :: persist sessions + amendments + schedules, then reconnect")
    with kb.connect(board="rst") as conn:
        sid = kb.open_steering_session(conn, board="rst", mode="runtime_evolution", title="evolve")["session_id"]
        kb.append_steering_message(conn, sid, role="owner", content="hello", board="rst")
        proposed = copy.deepcopy(base)
        for k, spec in proposed.get("tunables", {}).items():
            if isinstance(spec, dict) and "range" in spec:
                spec["range"] = [spec["range"][0], spec["range"][1] + 3]
                break
        amd = kb.propose_contract_amendment(conn, board="rst", proposed_contract=proposed,
                                            rationale="restart durability", origin="owner")
        aid = amd["amendment_id"]
        sched_before = conn.execute("SELECT COUNT(*) FROM reactive_timer_schedules WHERE board='rst'").fetchone()[0]

    # Fresh connection == simulated restart.
    with kb.connect(board="rst") as conn:
        sess = kb.get_steering_session(conn, sid, board="rst")
        msgs = kb.get_steering_messages(conn, sid, board="rst")
        amd2 = kb.get_contract_amendment(conn, aid, board="rst")
        sched_after = conn.execute("SELECT COUNT(*) FROM reactive_timer_schedules WHERE board='rst'").fetchone()[0]
        print(f"  session reloaded: {sess is not None and sess['status']=='open'}; msgs={len(msgs)}")
        print(f"  amendment reloaded: {amd2 is not None and amd2['amendment_id']==aid}")
        print(f"  schedules before={sched_before} after={sched_after}")
        if sess is None or sess["status"] != "open":
            findings.append("[restart] steering session did not survive reconnect")
        if not msgs:
            findings.append("[restart] steering messages lost on reconnect")
        if amd2 is None:
            findings.append("[restart] amendment lost on reconnect")
        if sched_before != sched_after:
            findings.append("[restart] timer schedules changed across reconnect")
    return findings


_CONCURRENCY_WORKER = r'''
import os, sys, time, random
sys.path.insert(0, "__WORKTREE__")
from hermes_cli import kanban_db as kb
board = sys.argv[1]
n = int(sys.argv[2])
worker_id = sys.argv[3]
ok = 0
for i in range(n):
    with kb.connect_closing(board=board) as conn:
        tid = kb.create_task(conn, title=f"{worker_id}-{i}", board=board,
                             assignee="mock-ceo", initial_status="blocked")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        kb.sensors_tick(conn, board=board, now=int(time.time()) + i)
        kb.reactive_tick(conn, board=board, now=int(time.time()) + i)
    ok += 1
print(f"WORKER {worker_id} DONE {ok}")
'''


def scenario_concurrency() -> list[str]:
    """S8c: many processes hammering one board -> no deadlock, consistent state."""
    findings: list[str] = []
    import subprocess
    from hermes_cli import kanban_db as kb

    # A plain board (no serious launch contract) so create_task needs no
    # workstream/stage semantics -- the point here is to hammer the cross-process
    # board LOCK under contention, not exercise contract gates.
    kb.create_board("conc", name="Concurrency")

    banner("S8c concurrency :: 6 processes x 15 writes/ticks against one board")
    worker_src = _CONCURRENCY_WORKER.replace("__WORKTREE__", str(_WORKTREE))
    worker_path = Path(os.environ["HERMES_HOME"]).parent / "conc_worker.py"
    worker_path.write_text(worker_src)
    env = dict(os.environ)
    procs = []
    t0 = time.perf_counter()
    NPROC, NPER = 6, 15
    for w in range(NPROC):
        procs.append(subprocess.Popen(
            [sys.executable, str(worker_path), "conc", str(NPER), f"w{w}"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        ))
    deadlocked = False
    outputs = []
    for p in procs:
        try:
            out, _ = p.communicate(timeout=120)
            outputs.append((p.returncode, out))
        except subprocess.TimeoutExpired:
            p.kill()
            deadlocked = True
            outputs.append((-1, "TIMEOUT/DEADLOCK"))
    elapsed = time.perf_counter() - t0
    done = sum(1 for rc, out in outputs if rc == 0 and "DONE" in out)
    errors = [out for rc, out in outputs if rc != 0]
    print(f"  {done}/{NPROC} workers finished cleanly in {elapsed:.2f}s")
    for rc, out in outputs:
        print(f"    rc={rc} :: {out.strip().splitlines()[-1] if out.strip() else ''}")
    if deadlocked:
        findings.append("[concurrency] at least one worker DEADLOCKED (120s timeout)")
    if errors:
        findings.append(f"[concurrency] {len(errors)} worker(s) failed: {errors[0][:300]}")
    # Verify final consistency: every task row is intact and counts match.
    with kb.connect(board="conc") as conn:
        total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        print(f"  total tasks after concurrent writes: {total} (expected {NPROC*NPER})")
        if not deadlocked and total != NPROC * NPER:
            findings.append(f"[concurrency] lost writes: {total} tasks, expected {NPROC*NPER}")
        # DB integrity check.
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            findings.append(f"[concurrency] sqlite integrity_check failed: {integrity}")
        print(f"  sqlite integrity_check: {integrity}")
    return findings


def scenario_perf_scale() -> list[str]:
    """S8d: scaling probe -- does optimizer evidence read / prune grow superlinearly?"""
    findings: list[str] = []
    import random
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_optimizer as opt

    KNOB = "follow_up_interval_hours"
    contract = {
        "objective": {"statement": "Scale board", "success": ["x"], "failure": ["y"], "constraints": ["z"]},
        "runtime": {"mode": "company", "dispatcher": {"profile": "mock-ceo"}},
        "workflow": {"id": "f", "stages": [{"key": "execute", "actions": [{"key": "nudge"}]}]},
        "tunables": {KNOB: {"default": 72, "allowed": [48, 72]}},
    }
    banner("S8d perf scale :: read_evidence + optimizer_tick at 1k / 4k / 16k signals")
    prev = None
    for scale in (1000, 4000, 16000):
        slug = f"scale{scale}"
        kb.create_board(slug)
        kb.write_board_metadata(slug, business_contract=contract)
        with kb.connect(board=slug) as conn:
            with kb.write_txn(conn):
                for i in range(scale):
                    value = 48 if i % 2 == 0 else 72
                    kb.record_board_signal(conn, board=slug, primitive_kind="outcome",
                                           primitive_key="seller_follow_up", knob_snapshot={KNOB: value},
                                           reward_value=1.0 if value == 48 else 0.0, reward_kind="conversion",
                                           realized_at=1_000_000 + i, ts=1_000_000 + i, dedupe_key=f"o:{i}")
            t0 = time.perf_counter()
            opt.read_knob_outcome_evidence(conn, board=slug, knob=KNOB, spec={"default": 72, "allowed": [48, 72]})
            dt = time.perf_counter() - t0
            ratio = (dt / prev) if prev else None
            print(f"    {scale:6d} signals -> read_evidence {dt*1000:8.2f} ms"
                  + (f"  (x{ratio:.2f} vs prev 4x data)" if ratio else ""))
            # 4x the data should cost ~4x (linear), not ~16x (quadratic).
            if ratio is not None and ratio > 8.0:
                findings.append(f"[perf-scale] read_evidence scaled x{ratio:.1f} for 4x data at {scale} (superlinear)")
            prev = dt
    return findings


SCENARIOS = {
    "launch": scenario_launch_compile,
    "sensors": scenario_sensors,
    "optimizer": scenario_optimizer,
    "amendment": scenario_amendment,
    "perf": scenario_perf,
    "restart": scenario_restart,
    "concurrency": scenario_concurrency,
    "perfscale": scenario_perf_scale,
}


def main(argv: list[str]) -> int:
    _isolate_home()
    requested = argv or list(SCENARIOS)
    all_findings: list[str] = []
    for name in requested:
        fn = SCENARIOS.get(name)
        if fn is None:
            print(f"unknown scenario: {name} (have {list(SCENARIOS)})")
            continue
        all_findings.extend(fn())
    banner("FINDINGS")
    if not all_findings:
        print("  (none)")
    for f in all_findings:
        print(f"  - {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
