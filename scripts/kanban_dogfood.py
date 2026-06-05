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
from typing import Optional

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


def scenario_amendment_triggers() -> list[str]:
    """S6: drive ALL THREE amendment triggers end-to-end through CAS mint.

    (a) optimizer out-of-range knob, (b) circuit-breaker open on a side-effect
    path, (c) owner-initiated. Each must propose -> approve (token + inputs) ->
    validate -> mint (version bump + recompile).
    """
    findings: list[str] = []
    from hermes_cli import kanban_db as kb

    base = load_contract("insurance_recruiting")

    def activate(board):
        kb.review_business_launch_contract(board, contract=base, create_if_missing=True)
        tok = kb.issue_board_launch_approval_token(
            board, contract=base, approved_by="owner",
            approval_evidence={"s": "x"}, owner_authority_confirmed=True)["token"]
        kb.review_business_launch_contract(board, contract=base, approve=True, approval_token=tok)

    # (a) optimizer out-of-range knob -----------------------------------------
    banner("S6 triggers :: (a) optimizer out-of-range knob -> mint")
    activate("trig_opt")
    with kb.connect(board="trig_opt") as conn:
        # Find a tunable with a declared range and propose a value past its top.
        bc = kb._metadata_as_business_contract(kb.read_board_metadata("trig_opt"))
        tun = bc.get("tunables") or (bc.get("runtime") or {}).get("tunables") or {}
        knob = next((k for k, s in tun.items() if isinstance(s, dict) and ("range" in s or "max" in s)), None)
        print(f"  knob={knob}")
        if knob is None:
            findings.append("[trigger-a] no ranged tunable to drive optimizer out-of-range")
        else:
            spec = tun[knob]
            top = (spec.get("range") or [0, spec.get("max", 1)])[1]
            out = kb.apply_knob_update(conn, board="trig_opt", knob=knob,
                                       new_value=float(top) + 50, actor="optimizer")
            print(f"  apply_knob_update -> status={out['status']} amendment={out.get('amendment_id')}")
            aid = out.get("amendment_id")
            if not aid:
                findings.append(f"[trigger-a] out-of-range knob did not draft an amendment: {out}")
            else:
                amd = kb.get_contract_amendment(conn, aid, board="trig_opt")
                if amd["origin"] != "optimizer":
                    findings.append(f"[trigger-a] origin != optimizer: {amd['origin']}")
                tok = kb.issue_board_launch_approval_token(
                    "trig_opt", amendment_id=aid, approved_by="owner",
                    approval_evidence={"s": "x"}, owner_authority_confirmed=True)["token"]
                kb.approve_contract_amendment(conn, aid, board="trig_opt", approver="owner", token=tok)
                v = kb.validate_contract_amendment(conn, aid, board="trig_opt")
                m = kb.mint_contract_amendment(conn, aid, board="trig_opt")
                print(f"  validated={v['status']} minted={m['status']} v={m.get('minted_version')}")
                if m["status"] != kb.AMENDMENT_STATUS_ACTIVE or m.get("minted_version") != 2:
                    findings.append(f"[trigger-a] optimizer amendment did not mint to v2: {m}")

    # (b) circuit-breaker open on a side-effect path ---------------------------
    banner("S6 triggers :: (b) circuit-breaker open -> sensor amendment -> mint")
    activate("trig_cb")  # golden contract carries a side-effect-gating breaker
    now = 10_000_000
    with kb.connect(board="trig_cb") as conn:
        with kb.write_txn(conn):
            for i in range(6):
                conn.execute(
                    "INSERT INTO task_runs (task_id,status,started_at,ended_at,outcome) "
                    "VALUES (?, 'crashed', ?, ?, 'crashed')", (f"t{i}", now - 100, now - 10))
        tick = kb.sensors_tick(conn, board="trig_cb", now=now)
        proposed = tick.get("amendments_proposed") or []
        print(f"  circuit={[c.get('status') for c in (tick.get('circuit') or [])]} proposed={proposed}")
        if not proposed:
            findings.append(f"[trigger-b] circuit open did not propose a sensor amendment: {tick}")
        else:
            aid = proposed[0]
            amd = kb.get_contract_amendment(conn, aid, board="trig_cb")
            print(f"  origin={amd['origin']} status={amd['status']} inputs={[i['key'] for i in amd['required_inputs']]}")
            if amd["origin"] != "sensor":
                findings.append(f"[trigger-b] origin != sensor: {amd['origin']}")
            # Supply the required owner api_key input, then approve->validate->mint.
            kb.submit_amendment_inputs(conn, aid, {"api_key": "sk-test-12345"}, board="trig_cb")
            base_v = kb.read_board_metadata("trig_cb").get("contract_version") or 1
            tok = kb.issue_board_launch_approval_token(
                "trig_cb", amendment_id=aid, approved_by="owner",
                approval_evidence={"s": "x"}, owner_authority_confirmed=True)["token"]
            kb.approve_contract_amendment(conn, aid, board="trig_cb", approver="owner", token=tok)
            v = kb.validate_contract_amendment(conn, aid, board="trig_cb")
            m = kb.mint_contract_amendment(conn, aid, board="trig_cb")
            print(f"  validated={v['status']} minted={m['status']} v={m.get('minted_version')}")
            if m["status"] != kb.AMENDMENT_STATUS_ACTIVE:
                findings.append(f"[trigger-b] sensor amendment did not mint active: {m}")

    return findings


def scenario_steering() -> list[str]:
    """S7: P6 conversational CEO steering -> ceo-origin P5 amendment -> mint.

    Drives both session modes with a DETERMINISTIC CEO stub (the aux model is
    monkeypatched the way the tests do), confirms a structured proposal becomes
    a ceo-origin amendment surfaced back into the chat, mints it through the P5
    rail, and confirms the conversation + state survive a fresh connection.
    """
    import copy
    findings: list[str] = []
    from hermes_cli import kanban_db as kb
    from hermes_cli import company_launch as kli

    base = load_contract("insurance_recruiting")

    def activate(board):
        kb.review_business_launch_contract(board, contract=base, create_if_missing=True)
        tok = kb.issue_board_launch_approval_token(
            board, contract=base, approved_by="owner",
            approval_evidence={"s": "x"}, owner_authority_confirmed=True)["token"]
        kb.review_business_launch_contract(board, contract=base, approve=True, approval_token=tok)

    # Build a valid in-range widening proposal for a ranged tunable.
    proposed = copy.deepcopy(base)
    tun = proposed.get("tunables") or {}
    knob = next((k for k, s in tun.items() if isinstance(s, dict) and "range" in s), None)
    if knob is not None:
        lo, hi = tun[knob]["range"]
        tun[knob]["range"] = [lo, hi + 5]

    # Deterministic CEO stub: run_ceo_turn parses this canned JSON offline.
    def _fake_call_model(system_prompt, user_payload, **kwargs):
        return __import__("json").dumps({
            "reply": "Agreed -- here's the change for your approval.",
            "amendment": {"rationale": "widen via chat", "proposed_contract": proposed},
            "research_query": "",
        }), False
    _orig_call, _orig_aux = kli._call_model, kli.aux_configured
    kli._call_model = _fake_call_model
    kli.aux_configured = lambda: True
    try:
        for mode in ("launch_buildout", "runtime_evolution"):
            banner(f"S7 steering :: {mode} -> ceo amendment -> mint")
            board = f"steer_{mode}"
            activate(board)
            with kb.connect(board=board) as conn:
                try:
                    sid = kb.open_steering_session(conn, board=board, mode=mode)["session_id"]
                except Exception as exc:
                    findings.append(f"[steer:{mode}] open_steering_session crashed: {exc!r}")
                    continue
                result = kb.steer_send_message(
                    conn, board=board, session_id=sid,
                    owner_message="Our nudge cap feels too low; please widen it.")
                amd = result.get("amendment")
                if not amd:
                    findings.append(f"[steer:{mode}] CEO turn produced no amendment: {result}")
                    continue
                print(f"  amendment origin={amd['origin']} status={amd['status']} id={amd['amendment_id']}")
                if amd["origin"] != "ceo":
                    findings.append(f"[steer:{mode}] amendment origin != ceo: {amd['origin']}")
                # CEO reply must reference the amendment back into the chat.
                ceo_msg = result.get("ceo_message") or {}
                if (ceo_msg.get("attachments") or {}).get("amendment_id") != amd["amendment_id"]:
                    findings.append(f"[steer:{mode}] CEO reply did not surface the amendment id")
                aid = amd["amendment_id"]
                tok = kb.issue_board_launch_approval_token(
                    board, amendment_id=aid, approved_by="owner",
                    approval_evidence={"s": "x"}, owner_authority_confirmed=True)["token"]
                kb.approve_contract_amendment(conn, aid, board=board, approver="owner", token=tok)
                kb.validate_contract_amendment(conn, aid, board=board)
                m = kb.mint_contract_amendment(conn, aid, board=board)
                print(f"  minted status={m['status']} v={m.get('minted_version')}")
                if m["status"] != kb.AMENDMENT_STATUS_ACTIVE or m.get("minted_version") != 2:
                    findings.append(f"[steer:{mode}] ceo amendment did not mint to v2: {m}")
                # Conversation reflects activation.
                refl = kb.steer_reflect_amendment_state(conn, session_id=sid, amendment_id=aid, board=board)
                if "minted" not in (refl.get("content") or "").lower():
                    findings.append(f"[steer:{mode}] conversation did not reflect mint: {refl.get('content')}")
            # Durability across a FRESH connection.
            with kb.connect(board=board) as conn:
                msgs = kb.get_steering_messages(conn, sid, board=board)
                roles = [m["role"] for m in msgs]
                print(f"  durable reload: {len(msgs)} msgs roles={roles}")
                if not msgs or roles[0] != "owner":
                    findings.append(f"[steer:{mode}] steering log not durable across reconnect")
    finally:
        kli._call_model, kli.aux_configured = _orig_call, _orig_aux
    return findings


def _loop_terminal_state(contract: dict, loop_key: str) -> Optional[str]:
    """First declared terminal_state for the loop whose entity the watcher tracks."""
    for loop in (contract.get("event_loops") or []):
        if str(loop.get("type")) == loop_key:
            ts = loop.get("terminal_states") or []
            if ts:
                return str(ts[0])
    return None


def _drive_watcher(kb, conn, board: str, contract: dict, findings: list[str]) -> None:
    """Drive the compiled prospect/entity watcher: inbound wake -> timer nudge ->
    terminal stop, asserting exactly-once wake/nudge/outcome signals. Robust to
    timer-only (non-inbound) loops, e.g. a metric-driven sprint."""
    sched = conn.execute(
        "SELECT * FROM reactive_timer_schedules WHERE board=? AND active=1 "
        "ORDER BY id ASC LIMIT 1", (board,),
    ).fetchone()
    if sched is None:
        findings.append(f"[{board}] no active timer schedule compiled for the watcher")
        return
    cadence = int(sched["cadence_seconds"])
    base = int(sched["next_fire_at"])
    entity_id = sched["entity_id"]
    task_id = sched["task_id"]
    loop_key = sched["loop_key"]
    print(f"  watcher: loop_key={loop_key} cadence={cadence}s "
          f"max_nudges={sched['max_nudges']} entity_id={entity_id}")

    # 1) Inbound reply wakes the watcher (prospect replies on the business line).
    routes = kb.list_watch_routes(conn, task_id, active=True) if task_id else []
    had_inbound = bool(routes)
    if routes:
        route = routes[0]
        kb.trigger_reactive_event(
            conn, trigger_type=route.trigger_type, trigger_key=route.trigger_key,
            payload={"event_id": "evt-reply-1", "body": "yes still interested"},
            actor="mock-gateway", board=board,
        )
        wakes = conn.execute(
            "SELECT action FROM board_signals WHERE board=? AND primitive_kind='event_loop'",
            (board,),
        ).fetchall()
        woke = any(w["action"] and "wake" in w["action"] for w in wakes)
        replies = conn.execute(
            "SELECT reward_value FROM board_signals WHERE board=? AND primitive_kind='outcome' "
            "AND reward_kind='reply'", (board,),
        ).fetchall()
        print(f"  inbound reply -> watcher woke={woke}; reply rewards={len(replies)}")
        if not woke:
            findings.append(f"[{board}] inbound reply did not wake the watcher")
        if not replies:
            findings.append(f"[{board}] inbound reply emitted no reply reward for the optimizer")
    else:
        print(f"  (watcher task {task_id} timer-only; no inbound route -- metric/timer loop)")

    # 2) Timer fires a follow-up nudge on cadence; re-ticking same instant is idempotent.
    res = kb.reactive_tick(conn, now=base, board=board)
    fired1 = res["fired"]
    res_again = kb.reactive_tick(conn, now=base, board=board)
    print(f"  timer fire @cadence -> fired={fired1}; re-tick same instant -> fired={res_again['fired']}")
    if not fired1:
        findings.append(f"[{board}] follow-up timer did not fire on cadence")
    if res_again["fired"]:
        findings.append(f"[{board}] timer double-fired within one cadence window")

    # 3) Resolve the watched entity to a declared terminal state -> timer stops
    #    (no zombie). For an inbound (conversation) loop a conversion outcome is
    #    credited exactly once.
    if entity_id:
        term = _loop_terminal_state(contract, loop_key) or "won"
        kb.resolve_reactive_entity(conn, entity_id, terminal_outcome="won",
                                   state=term, actor="dogfood")
        stop = kb.reactive_tick(conn, now=base + cadence, board=board)
        zombie = kb.reactive_tick(conn, now=base + 10 * cadence, board=board)
        # Only THIS loop must have stopped; a board may have other independent
        # loops still legitimately firing (e.g. grow_app's 5 parallel loops).
        this_stopped = any(s["loop_key"] == loop_key for s in stop["stopped"])
        this_zombie = any(f["loop_key"] == loop_key for f in zombie["fired"])
        print(f"  resolve entity -> '{term}'; this-loop stopped={this_stopped}; "
              f"this-loop later fires={this_zombie} (other loops: "
              f"stopped={[s['loop_key'] for s in stop['stopped'] if s['loop_key']!=loop_key]}, "
              f"fired={[f['loop_key'] for f in zombie['fired'] if f['loop_key']!=loop_key]})")
        if not this_stopped:
            findings.append(f"[{board}] watcher loop {loop_key} did not stop after terminal state")
        if this_zombie:
            findings.append(f"[{board}] zombie: loop {loop_key} kept firing after terminal stop")
        if had_inbound:
            conv = conn.execute(
                "SELECT COUNT(*) n FROM board_signals WHERE board=? AND primitive_kind='outcome' "
                "AND reward_kind='conversion'", (board,),
            ).fetchone()["n"]
            print(f"  terminal conversion outcomes credited: {conv}")
            if conv < 1:
                findings.append(f"[{board}] terminal resolution credited no conversion outcome")


def _trip_circuit_to_amendment(kb, conn, board: str, findings: list[str]) -> Optional[str]:
    """Drive a failure burst -> circuit breaker open -> sensor amendment proposal.

    Returns the proposed amendment id (or None). Uses the domain's own
    side-effect-gating breaker so the trip is faithful to the contract.
    """
    now = 9_000_000
    with kb.write_txn(conn):
        for i in range(8):
            conn.execute(
                "INSERT INTO task_runs (task_id,status,started_at,ended_at,outcome) "
                "VALUES (?, 'crashed', ?, ?, 'crashed')", (f"cb{i}", now - 100, now - 10))
    tick = kb.sensors_tick(conn, board=board, now=now)
    circuits = [c.get("status") for c in (tick.get("circuit") or [])]
    proposed = tick.get("amendments_proposed") or []
    print(f"  circuit states={circuits}; sensor amendments proposed={proposed}")
    if not any(s == "open" for s in circuits):
        findings.append(f"[{board}] failure burst did not open the circuit breaker")
    return proposed[0] if proposed else None


def scenario_golden_lifecycle(domain: str) -> list[str]:
    """Full live lifecycle for a repaired golden domain: launch -> compile ->
    prospect/entity watcher (inbound wake + timer nudge + terminal stop) ->
    sensor trip -> optimizer auto-apply -> P5 amendment mint + recompile + re-arm."""
    findings: list[str] = []
    import copy
    import random
    from hermes_cli import kanban_db as kb

    contract = load_contract(domain)
    banner(f"GL launch+compile :: {domain}")
    res = activate(kb, domain, contract)
    summary = kb.compile_contract_reactive_runtime(domain)
    meta = kb.read_board_metadata(domain)
    print(f"  activate ok={res.get('ok')} version={meta.get('contract_version')} "
          f"phase={meta.get('launch_phase')} compiled_loops={summary.get('reused') or summary.get('compiled')}")

    # ---- P5 amendment FIRST (schedule still active) to prove re-arm visibly. ----
    banner(f"GL P5 amendment :: {domain} owner change cadence -> mint -> recompile + re-arm")
    cad_knob = "follow_up_interval_hours"
    with kb.connect(board=domain) as conn:
        before_v = kb.read_board_metadata(domain).get("contract_version") or 1
        sched_before = conn.execute(
            "SELECT cadence_seconds FROM reactive_timer_schedules WHERE board=? AND active=1 "
            "ORDER BY id ASC LIMIT 1", (domain,)).fetchone()
        proposed = copy.deepcopy(contract)
        ptun = proposed.get("tunables") or {}
        rearm_expected = None
        if cad_knob in ptun and "range" in ptun[cad_knob]:
            # Halve the follow-up cadence (in-range) so the re-armed schedule must change.
            old_default = int(ptun[cad_knob]["default"])
            new_default = max(int(ptun[cad_knob]["range"][0]), old_default // 2)
            ptun[cad_knob]["default"] = new_default
            rearm_expected = new_default * 3600
            print(f"  changing {cad_knob} default {old_default}h -> {new_default}h "
                  f"(expect cadence {rearm_expected}s)")
        amd = kb.propose_contract_amendment(conn, board=domain, proposed_contract=proposed,
                                            rationale="dogfood change cadence", origin="owner")
        aid = amd["amendment_id"]
        tok = kb.issue_board_launch_approval_token(domain, amendment_id=aid, approved_by="owner",
                                                   approval_evidence={"s": "x"},
                                                   owner_authority_confirmed=True)["token"]
        kb.approve_contract_amendment(conn, aid, board=domain, approver="owner", token=tok)
        v = kb.validate_contract_amendment(conn, aid, board=domain)
        m = kb.mint_contract_amendment(conn, aid, board=domain)
        after_v = kb.read_board_metadata(domain).get("contract_version")
        sched_after = conn.execute(
            "SELECT cadence_seconds FROM reactive_timer_schedules WHERE board=? AND active=1 "
            "ORDER BY id ASC LIMIT 1", (domain,)).fetchone()
        cb = sched_before and sched_before["cadence_seconds"]
        ca = sched_after and sched_after["cadence_seconds"]
        print(f"  validated={v['status']} minted={m['status']} version {before_v}->{after_v}; "
              f"cadence re-arm {cb} -> {ca}")
        if m["status"] != kb.AMENDMENT_STATUS_ACTIVE:
            findings.append(f"[{domain}] amendment did not mint active: {m}")
        if after_v != before_v + 1:
            findings.append(f"[{domain}] contract_version not bumped after mint ({before_v}->{after_v})")
        if rearm_expected is not None and ca is not None and int(ca) != rearm_expected:
            findings.append(f"[{domain}] timer not re-armed to new cadence "
                            f"(got {ca}, expected {rearm_expected})")

    banner(f"GL watcher :: {domain} (inbound wake / timer nudge / terminal stop)")
    with kb.connect(board=domain) as conn:
        _drive_watcher(kb, conn, domain, contract, findings)

    # Derive a workstream/stage/action whose action carries an external_reversible
    # side effect so the breaker (which gates external_reversible) actually gates
    # this task's dispatch; assign a REAL worker envelope so the envelope check
    # passes and circuit_open is the operative blocker.
    wf = contract.get("workflow") or {}
    goal_id = wf.get("goal_id")
    worker_profile = ((contract.get("runtime") or {}).get("profiles") or {}).get("worker")
    gated_ws = gated_stage = gated_action = None
    for ws in (wf.get("workstreams") or []):
        for st in (wf.get("stages") or []):
            if st.get("key") not in (ws.get("stages") or []):
                continue
            for a in (st.get("actions") or []):
                if isinstance(a, dict) and a.get("side_effect_class") == "external_reversible":
                    gated_ws, gated_stage, gated_action = ws.get("key"), st.get("key"), a.get("key")
                    break
            if gated_action:
                break
        if gated_action:
            break

    banner(f"GL sensor trip :: {domain} circuit breaker -> dispatch gating")
    with kb.connect(board=domain) as conn:
        before = None
        wid = None
        if gated_action and worker_profile:
            wid = kb.create_task(conn, title="gated worker", assignee=worker_profile, board=domain,
                                 initial_status="blocked", goal_id=goal_id, workstream_id=gated_ws,
                                 stage_key=gated_stage, action_key=gated_action)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (wid,))
            before = kb.evaluate_dispatch_eligibility(conn, wid, board=domain)
        cb_amd = _trip_circuit_to_amendment(kb, conn, domain, findings)
        if wid is not None:
            after = kb.evaluate_dispatch_eligibility(conn, wid, board=domain)
            after_codes = {b["code"] for b in after["blockers"]}
            print(f"  gated task ({gated_stage}/{gated_action}) dispatch before trip ok={before['ok']} "
                  f"codes_before={ {b['code'] for b in before['blockers']} }; "
                  f"after trip ok={after['ok']} codes={after_codes}")
            if "circuit_open" not in after_codes:
                findings.append(f"[{domain}] circuit open but dispatch not gated by circuit_open "
                                f"(codes={after_codes})")
        # Drive the sensor-origin amendment through mint to prove the sensor->P5 path.
        if cb_amd:
            kb.submit_amendment_inputs(conn, cb_amd, {"api_key": "sk-test-1"}, board=domain)
            tok = kb.issue_board_launch_approval_token(domain, amendment_id=cb_amd, approved_by="owner",
                                                       approval_evidence={"s": "x"},
                                                       owner_authority_confirmed=True)["token"]
            kb.approve_contract_amendment(conn, cb_amd, board=domain, approver="owner", token=tok)
            kb.validate_contract_amendment(conn, cb_amd, board=domain)
            sm = kb.mint_contract_amendment(conn, cb_amd, board=domain)
            print(f"  sensor amendment {cb_amd} minted status={sm['status']}")

    banner(f"GL optimizer :: {domain} convergence + in-range auto-apply")
    # Pick a ranged tunable and feed an outcome stream that favours its low arm.
    tun = contract.get("tunables") or {}
    knob = next((k for k, s in tun.items()
                 if isinstance(s, dict) and "range" in s and k not in (
                     "heartbeat_interval_seconds", "stall_timeout_seconds")), None)
    if knob is None:
        findings.append(f"[{domain}] no ranged tunable to drive optimizer")
    else:
        lo, hi = tun[knob]["range"]
        # Two candidate arms strictly inside the declared range.
        arm_good = lo if lo > 0 else (lo + max(1, (hi - lo) // 4))
        arm_bad = hi
        with kb.connect(board=domain) as conn:
            base_ts = 3_000_000
            for i in range(40):
                for value, rv in ((arm_good, 1.0), (arm_bad, 0.0)):
                    with kb.write_txn(conn):
                        kb.record_board_signal(conn, board=domain, primitive_kind="outcome",
                                               primitive_key="opt", knob_snapshot={knob: value},
                                               reward_value=rv, reward_kind="conversion",
                                               realized_at=base_ts + i, ts=base_ts + i,
                                               dedupe_key=f"opt:{i}:{value}")
            out = kb.optimizer_tick(conn, board=domain, now=base_ts + 100, rng=random.Random(0))
            applied = [(a["knob"], a["new_value"]) for a in out["applied"]]
            print(f"  optimizer knob={knob} arms=({arm_good}<-good,{arm_bad}<-bad) applied={applied}")
            if not any(a[0] == knob for a in applied):
                findings.append(f"[{domain}] optimizer did not auto-apply the converged knob {knob}")
            else:
                new_val = next(a[1] for a in applied if a[0] == knob)
                if not (lo <= new_val <= hi):
                    findings.append(f"[{domain}] optimizer applied out-of-range value {new_val} for {knob}")
    return findings


def scenario_land_lifecycle() -> list[str]:
    return scenario_golden_lifecycle("land_wholesaling")


def scenario_grow_lifecycle() -> list[str]:
    return scenario_golden_lifecycle("grow_app_one_week")


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
    "land_lifecycle": scenario_land_lifecycle,
    "grow_lifecycle": scenario_grow_lifecycle,
    "sensors": scenario_sensors,
    "optimizer": scenario_optimizer,
    "amendment": scenario_amendment,
    "triggers": scenario_amendment_triggers,
    "steering": scenario_steering,
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
