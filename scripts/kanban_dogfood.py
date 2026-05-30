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


SCENARIOS = {
    "launch": scenario_launch_compile,
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
