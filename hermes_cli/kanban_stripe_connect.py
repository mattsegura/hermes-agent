"""Stripe Connect scaffold for per-board payment scope (P0 stub).

Payment is scoped to one board domain (models + integrations for that board).
Full Connect onboarding is P1; this module exposes assessment gates and
contract fields so launch approval can fail closed when payment is required.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

__all__ = [
    "assess_board_payment",
    "launch_payment_questions",
    "normalize_runtime_payment",
    "payment_required_for_contract",
    "save_board_payment_stub",
]

_PAYMENT_STATE_FILENAME = "payment.connect.json"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _contract_root(contract: Any) -> dict[str, Any]:
    parsed = contract if isinstance(contract, dict) else {}
    if isinstance(parsed.get("operating_contract"), dict):
        return dict(parsed["operating_contract"])
    inner = parsed.get("contract")
    if isinstance(inner, dict) and any(
        k in inner for k in ("objective", "runtime", "workflow")
    ):
        return dict(inner)
    return dict(parsed)


def _board_dir(board: Optional[str]) -> Path:
    from hermes_cli.kanban_db import board_dir

    return board_dir(board)


def _payment_state_path(board: Optional[str]) -> Path:
    return _board_dir(board) / _PAYMENT_STATE_FILENAME


def normalize_runtime_payment(value: Optional[Any]) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("runtime.payment must be an object")
    out: dict[str, Any] = {
        "provider": "stripe_connect",
        "scope": str(value.get("scope") or "board").strip() or "board",
        "required": bool(value.get("required", False)),
        "status": str(value.get("status") or "pending").strip().lower(),
    }
    acct = str(value.get("stripe_connect_account_id") or "").strip()
    if acct:
        out["stripe_connect_account_id"] = acct
    if value.get("board_slug"):
        out["board_slug"] = str(value.get("board_slug")).strip()
    return out


def _contract_requires_payment_input(contract: dict[str, Any]) -> bool:
    for spec in _as_list(contract.get("launch_required_inputs")):
        if not isinstance(spec, dict):
            continue
        key = str(spec.get("key") or "").strip().lower()
        typ = str(spec.get("type") or "").strip().lower()
        if key == "payment_method" or typ == "payment_method":
            return bool(spec.get("required", True))
    return False


def payment_required_for_contract(contract: Optional[dict[str, Any]]) -> bool:
    root = _contract_root(contract)
    runtime = _as_dict(root.get("runtime"))
    payment = _as_dict(runtime.get("payment"))
    if payment.get("required"):
        return True
    return _contract_requires_payment_input(root)


def _load_payment_state(board: Optional[str]) -> dict[str, Any]:
    path = _payment_state_path(board)
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("kanban payment: unreadable state at %s: %s", path, exc)
        return {}
    return doc if isinstance(doc, dict) else {}


def assess_board_payment(
    board: Optional[str],
    contract: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Public payment readiness (no secrets)."""
    root = _contract_root(contract)
    required = payment_required_for_contract(root)
    if not required:
        return {
            "ok": True,
            "required": False,
            "status": "not_required",
            "scope": "board",
        }

    runtime_payment = normalize_runtime_payment(
        _as_dict(_as_dict(root.get("runtime")).get("payment"))
    ) or {"required": True, "status": "pending", "scope": "board"}

    state = _load_payment_state(board) if board else {}
    account_id = (
        str(state.get("stripe_connect_account_id") or "").strip()
        or str(runtime_payment.get("stripe_connect_account_id") or "").strip()
    )
    status = str(state.get("status") or runtime_payment.get("status") or "pending")
    ok = bool(account_id) and status in ("connected", "active", "ready")

    return {
        "ok": ok,
        "required": True,
        "status": status if ok else "pending",
        "scope": runtime_payment.get("scope") or "board",
        "stripe_connect_account_id": account_id or None,
        "stub": True,
        "todo": (
            "P1: implement Stripe Connect onboarding webhook and scoped "
            "billing per board domain"
        ),
    }


def launch_payment_questions(status: dict[str, Any]) -> list[str]:
    if not status.get("required") or status.get("ok"):
        return []
    return [
        "Before launch, connect a payment method scoped to this board's domain "
        "(models + services for this board only).",
        "Run `hermes kanban boards payment connect` when Stripe Connect is "
        "enabled (P1). Until then, operators can record a stub with "
        "`hermes kanban boards payment stub-set --account-id acct_...`.",
    ]


def save_board_payment_stub(
    board: Optional[str],
    *,
    account_id: str,
    status: str = "connected",
) -> dict[str, Any]:
    """Persist a non-secret Connect account id for gate testing (stub)."""
    acct = str(account_id or "").strip()
    if not acct.startswith("acct_"):
        raise ValueError("account_id must look like a Stripe Connect account id (acct_...)")
    path = _payment_state_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stripe_connect_account_id": acct,
        "status": str(status or "connected").strip().lower(),
        "provider": "stripe_connect",
        "stub": True,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(path), **payload}
