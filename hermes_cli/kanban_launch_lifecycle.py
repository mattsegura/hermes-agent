"""Board launch contract lifecycle: status, phase gates, and approval blockers.

Contracts move through human-visible states before dispatch is enabled:

    draft -> pending_credentials -> pending_payment -> pending_approval -> active

``launch_phase`` on ``board.json`` mirrors these gates (plus legacy aliases
``draft_intake`` and ``contract_review``). ``contract_status`` is persisted on
the operating contract for portable querying from CLI, Telegram, and tools.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

ContractStatus = Literal[
    "draft",
    "pending_credentials",
    "pending_payment",
    "pending_approval",
    "active",
]

CONTRACT_STATUSES: tuple[ContractStatus, ...] = (
    "draft",
    "pending_credentials",
    "pending_payment",
    "pending_approval",
    "active",
)

# Legacy launch_phase values map to the nearest modern gate for display logic.
_LEGACY_LAUNCH_PHASE_TO_STATUS: dict[str, ContractStatus] = {
    "draft_intake": "draft",
    "contract_review": "pending_approval",
}

# Phases that never dispatch work.
NON_DISPATCH_LAUNCH_PHASES = frozenset(
    {
        "draft",
        "draft_intake",
        "pending_credentials",
        "pending_payment",
        "pending_approval",
        "contract_review",
        "paused",
        "retired",
    }
)

__all__ = [
    "CONTRACT_STATUSES",
    "ContractStatus",
    "NON_DISPATCH_LAUNCH_PHASES",
    "derive_contract_status",
    "launch_gate_blockers",
    "launch_phase_for_contract_status",
    "normalize_contract_status",
    "resolve_post_review_launch_phase",
    "sync_contract_lifecycle_on_contract",
]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
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


def normalize_contract_status(value: Any, *, default: str = "draft") -> ContractStatus:
    text = str(value or default or "draft").strip().lower()
    if text in CONTRACT_STATUSES:
        return text  # type: ignore[return-value]
    mapped = _LEGACY_LAUNCH_PHASE_TO_STATUS.get(text)
    if mapped:
        return mapped
    if text == "active":
        return "active"
    return "draft"  # type: ignore[return-value]


def launch_phase_for_contract_status(status: ContractStatus) -> str:
    """Map contract_status to launch_phase on board.json.

    ``contract_status`` is the precise portable lifecycle field on the contract.
    ``launch_phase`` keeps legacy ``contract_review`` for draft and
    pending_approval so existing boards and tests stay compatible; only
    credential and payment gates use dedicated phase values.
    """
    if status in ("draft", "pending_approval"):
        return "contract_review"
    if status == "pending_credentials":
        return "pending_credentials"
    if status == "pending_payment":
        return "pending_payment"
    if status == "active":
        return "active"
    return "contract_review"


def _intake_incomplete(contract: dict[str, Any]) -> bool:
    from hermes_cli.kanban_db import (
        LAUNCH_INTAKE_STATE_ASSESSING,
        LAUNCH_INTAKE_STATE_CLARIFYING,
        LAUNCH_INTAKE_STATE_READY_FOR_OWNER_REVIEW,
        _contract_object,
        _launch_intake_answer_quality_sufficient,
        _normalize_launch_intake_answers,
    )

    intake = _contract_object(contract.get("launch_intake"))
    if not intake:
        return False
    state = str(intake.get("state") or "").strip().lower()
    if state in (LAUNCH_INTAKE_STATE_CLARIFYING, LAUNCH_INTAKE_STATE_ASSESSING):
        return True
    if state == LAUNCH_INTAKE_STATE_READY_FOR_OWNER_REVIEW:
        return False
    if not _normalize_launch_intake_answers(intake.get("answers")):
        return True
    return not _launch_intake_answer_quality_sufficient(contract)


def derive_contract_status(
    contract: Any,
    *,
    board: Optional[str] = None,
    launch_phase: Optional[str] = None,
    credentials: Optional[dict[str, Any]] = None,
    payment: Optional[dict[str, Any]] = None,
    readiness: Optional[dict[str, Any]] = None,
) -> ContractStatus:
    """Derive the portable contract_status from contract content and gates."""
    phase = str(launch_phase or "").strip().lower()
    if phase == "active":
        return "active"
    if phase == "retired":
        return "draft"

    root = _contract_root(contract)
    explicit = root.get("contract_status")
    if explicit and str(explicit).strip().lower() == "active":
        return "active"

    if _intake_incomplete(root):
        return "draft"

    if readiness and (readiness.get("errors") or readiness.get("status") == "invalid"):
        return "draft"

    if credentials is None:
        try:
            from hermes_cli import kanban_credentials as _creds

            credentials = _creds.assess_board_credentials(board, root)
        except Exception:
            credentials = {"ok": True, "missing_keys": []}

    if not credentials.get("ok"):
        return "pending_credentials"

    if payment is None:
        try:
            from hermes_cli import kanban_stripe_connect as _pay

            payment = _pay.assess_board_payment(board, root)
        except Exception:
            payment = {"ok": True, "required": False}

    if payment.get("required") and not payment.get("ok"):
        return "pending_payment"

    if readiness is not None and not readiness.get("ok"):
        missing = _as_list(readiness.get("missing"))
        if any(str(m).startswith("launch_credentials.") for m in missing):
            return "pending_credentials"
        if any(str(m).startswith("launch_payment.") for m in missing):
            return "pending_payment"
        return "draft"

    return "pending_approval"


def launch_gate_blockers(
    contract: Any,
    *,
    board: Optional[str] = None,
    operator_override: bool = False,
    approving: bool = False,
) -> list[str]:
    """Human-readable blockers for approval / activation (CLI + Telegram)."""
    root = _contract_root(contract)
    blockers: list[str] = []

    if _intake_incomplete(root) and not operator_override:
        blockers.append(
            "launch intake is incomplete: finish owner Q&A and answer assessment "
            "before synthesizing or approving the contract"
        )

    from hermes_cli.kanban_db import (
        _contract_uses_model_generated_intake,
        _launch_intake_requires_owner_ack,
        _contract_object,
    )

    if approving and _contract_uses_model_generated_intake(root) and not operator_override:
        intake = _contract_object(root.get("launch_intake"))
        if _launch_intake_requires_owner_ack(intake):
            coverage = intake.get("coverage")
            if not isinstance(coverage, dict):
                blockers.append(
                    "owner must review launch_intake.coverage before approval"
                )

    try:
        from hermes_cli import kanban_credentials as _creds

        creds = _creds.assess_board_credentials(board, root)
        if not creds.get("ok"):
            missing = ", ".join(creds.get("missing_keys") or [])
            blockers.append(
                f"missing launch credentials: {missing}. "
                "Use `hermes kanban boards credentials set` or "
                "kanban_submit_launch_credentials before approval."
            )
    except Exception:
        pass

    try:
        from hermes_cli import kanban_stripe_connect as _pay

        pay = _pay.assess_board_payment(board, root)
        if pay.get("required") and not pay.get("ok"):
            blockers.append(
                "payment method not configured for this board domain. "
                "Connect Stripe (scoped to this board) before approval."
            )
    except Exception:
        pass

    return blockers


def sync_contract_lifecycle_on_contract(
    contract: dict[str, Any],
    *,
    board: Optional[str] = None,
    launch_phase: Optional[str] = None,
    readiness: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Persist contract_status and ensure cost scaffold fields exist."""
    synced = dict(contract)
    if not _intake_incomplete(synced):
        try:
            from hermes_cli.kanban_launch_cost import ensure_contract_cost_scaffold

            synced = ensure_contract_cost_scaffold(synced)
        except Exception:
            pass

    status = derive_contract_status(
        synced,
        board=board,
        launch_phase=launch_phase,
        readiness=readiness,
    )
    synced["contract_status"] = status
    if isinstance(contract.get("runtime"), dict) or not _intake_incomplete(synced):
        runtime = _as_dict(synced.get("runtime"))
        runtime["contract_status"] = status
        synced["runtime"] = runtime
    return synced


def resolve_post_review_launch_phase(
    *,
    contract: dict[str, Any],
    board: Optional[str] = None,
    readiness: Optional[dict[str, Any]] = None,
    approve: bool = False,
) -> str:
    """Choose launch_phase after contract review (non-active path)."""
    if approve and readiness and readiness.get("ok"):
        blockers = launch_gate_blockers(contract, board=board, approving=True)
        if not blockers:
            return "active"

    status = derive_contract_status(
        contract, board=board, readiness=readiness,
    )
    return launch_phase_for_contract_status(status)
