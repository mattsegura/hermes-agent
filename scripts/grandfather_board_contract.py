#!/usr/bin/env python3
"""Grandfather an existing hand-authored board contract into the intake schema.

Boards that were built before the intake->contract pipeline existed already have
a working operating contract but no ``launch_intake`` provenance. This migration
stamps a ``source: "grandfathered"`` launch-intake block (with a coverage +
invariant snapshot) onto such a contract and re-approves the board, so it keeps
dispatching while gaining the same audit surface a freshly synthesized board has.

Because ``source`` is ``"grandfathered"`` (not ``"model_generated"``), the board
is exempt from the Phase-4 owner-coverage-acknowledgment gate -- this is a
back-compat migration, not a new launch.

SAFETY: dry-run is the DEFAULT. Nothing is written unless ``--apply`` is passed.
The migration only ever touches the board in the *currently configured*
``HERMES_HOME``; point ``HERMES_HOME`` at a copy before applying to anything you
care about. The bundled tests run it against a throwaway temp home only.

Usage:
    # Inspect what would happen (no writes):
    python scripts/grandfather_board_contract.py --board land-wholesaling \\
        --contract ~/.hermes/kanban/contracts/land-wholesaling-contract.json

    # Actually apply (writes to $HERMES_HOME):
    python scripts/grandfather_board_contract.py --board land-wholesaling \\
        --contract <path>.json --apply --approved-by "owner@example.com"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

_WORKTREE = Path(__file__).resolve().parents[1]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))


def upcast_legacy_triggers(contract: dict[str, Any]) -> dict[str, Any]:
    """Upcast a legacy contract's triggers to the typed ``kind`` grammar.

    Hand-authored / pre-grammar contracts carry triggers as free-text strings or
    ``type``-only objects in ``workflow.stages[].triggers`` and
    ``event_loops[].triggers``. This runs the one-time ingest classifier
    (:func:`hermes_cli.kanban_launch_grammar.normalize_trigger`) so the
    grandfathered contract -- and the invariant/coverage snapshot stamped onto
    it -- speaks the same typed grammar a freshly synthesized board does. Only
    triggers are touched; the rest of the contract is preserved verbatim, so
    structurally-broken contracts still fail the downstream invariant gate
    instead of raising here.
    """
    from hermes_cli.kanban_launch_grammar import normalize_trigger

    def _upcast_triggers(holder: Any) -> Any:
        if not isinstance(holder, dict) or not isinstance(holder.get("triggers"), list):
            return holder
        row = dict(holder)
        row["triggers"] = [normalize_trigger(t) for t in holder["triggers"]]
        return row

    out = dict(contract)
    workflow = out.get("workflow")
    if isinstance(workflow, dict) and isinstance(workflow.get("stages"), list):
        wf = dict(workflow)
        wf["stages"] = [_upcast_triggers(stage) for stage in wf["stages"]]
        out["workflow"] = wf
    if isinstance(out.get("event_loops"), list):
        out["event_loops"] = [_upcast_triggers(loop) for loop in out["event_loops"]]
    return out


def build_grandfathered_launch_intake(contract: dict[str, Any]) -> dict[str, Any]:
    """Return the launch_intake block to stamp onto a grandfathered contract."""
    from hermes_cli.kanban_launch_coverage import coverage_report_for_contract
    from hermes_cli.kanban_launch_invariants import check_contract_invariants

    coverage = coverage_report_for_contract(contract)
    invariants = check_contract_invariants(contract)
    return {
        "source": "grandfathered",
        "state": "ready_for_owner_review",
        "owner_review_required": False,
        "grandfathered_at": int(time.time()),
        "coverage": coverage.as_dict(),
        "invariants": invariants.as_dict(),
        "answer_quality": {
            "status": "sufficient",
            "sufficient": True,
            "coverage_score": round(coverage.score, 4),
            "assessed_by": "grandfather_migration",
            "evidence": "Pre-existing hand-authored contract migrated into the intake schema.",
        },
    }


def grandfather_contract(
    board: str,
    contract: dict[str, Any],
    *,
    apply: bool = False,
    approved_by: Optional[str] = None,
) -> dict[str, Any]:
    """Plan (and optionally apply) the grandfather migration for one board.

    Returns a report dict. When ``apply`` is False this performs NO writes.
    """
    from hermes_cli import kanban_db as kb

    contract = upcast_legacy_triggers(contract)
    intake = build_grandfathered_launch_intake(contract)
    stamped = dict(contract)
    stamped["launch_intake"] = intake

    report: dict[str, Any] = {
        "board": board,
        "applied": False,
        "coverage_score": intake["coverage"].get("score"),
        "coverage_passed": intake["coverage"].get("passed"),
        "invariants_ok": intake["invariants"].get("ok"),
        "invariant_errors": intake["invariants"].get("errors", []),
        "invariant_warnings": intake["invariants"].get("warnings", []),
    }

    if not intake["invariants"].get("ok"):
        report["status"] = "blocked"
        report["reason"] = "contract violates structural invariants; fix before grandfathering"
        return report

    if not apply:
        report["status"] = "dry_run"
        return report

    approver = (approved_by or "grandfather-migration").strip()
    kb.review_business_launch_contract(board, contract=stamped, create_if_missing=True)
    token = kb.issue_board_launch_approval_token(
        board,
        contract=stamped,
        approved_by=approver,
        approval_evidence={"source": "grandfather_migration", "approved_by": approver},
        owner_authority_confirmed=True,
    )["token"]
    result = kb.review_business_launch_contract(
        board,
        contract=stamped,
        approve=True,
        author=approver,
        approval_token=token,
    )
    report["applied"] = True
    report["status"] = "applied"
    report["launch_phase"] = result.get("launch_phase")
    report["launch_review_id"] = result.get("launch_review_id")
    return report


def _load_contract(path: str) -> dict[str, Any]:
    with open(path) as fh:
        data = json.load(fh)
    if isinstance(data, dict) and isinstance(data.get("operating_contract"), dict):
        return data["operating_contract"]
    if not isinstance(data, dict):
        raise SystemExit(f"contract at {path} is not a JSON object")
    return data


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--board", required=True, help="board slug to grandfather")
    parser.add_argument("--contract", required=True, help="path to the operating contract JSON")
    parser.add_argument("--apply", action="store_true", help="actually write (default: dry-run)")
    parser.add_argument("--approved-by", default=None, help="owner identity recorded on approval")
    args = parser.parse_args(argv)

    contract = _load_contract(args.contract)
    report = grandfather_contract(
        args.board, contract, apply=args.apply, approved_by=args.approved_by
    )
    home = os.environ.get("HERMES_HOME", "<default>")
    print(f"HERMES_HOME = {home}")
    print(json.dumps(report, indent=2))
    if report.get("status") == "blocked":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
