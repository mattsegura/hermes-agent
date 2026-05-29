"""Tests for serious-board operating contract gates."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb


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


def _contract(
    *,
    worker_capabilities=None,
    worker_toolsets=None,
    worker_required_proof=None,
    allowed_side_effects=None,
    provider_policy=None,
    required_capability="mock_research",
    required_toolset="kanban",
    required_proof="mock_report",
    side_effect_class="none",
):
    envelope = {}
    if worker_capabilities is not None:
        envelope["capabilities"] = worker_capabilities
    else:
        envelope["capabilities"] = [required_capability]
    if worker_toolsets is not None:
        envelope["toolsets"] = worker_toolsets
    else:
        envelope["toolsets"] = [required_toolset]
    if allowed_side_effects is not None:
        envelope["allowed_side_effects"] = allowed_side_effects
    else:
        envelope["allowed_side_effects"] = [side_effect_class]
    if worker_required_proof is not None:
        envelope["required_proof"] = worker_required_proof
    return {
        "objective": {
            "statement": "Safely run a mock serious board",
            "success": ["proof is structured"],
            "failure": ["worker escapes contract"],
            "constraints": ["mock only"],
        },
        "runtime": {
            "mode": "company",
            "dispatcher": {"profile": "mock-ceo"},
            "profiles": {
                "ceo": "mock-ceo",
                "optimizer": "mock-optimizer",
                "worker": "mock-worker",
            },
            "require_worker_envelopes": True,
            "require_provider_policy": True,
            "provider_policy": (
                {required_capability: {"provider": "fixture", "tool": "mock"}}
                if provider_policy is None
                else provider_policy
            ),
            "worker_envelopes": {
                "mock-worker": envelope
            },
        },
        "workflow": {
            "id": "contract-flow",
            "goal_id": "mock-goal",
            "require_semantics": True,
            "workstreams": [{"key": "ops", "stages": ["execute", "done"]}],
            "stages": [
                {
                    "key": "execute",
                    "actions": [
                        {
                            "key": "research",
                            "required_capabilities": [required_capability],
                            "required_toolsets": [required_toolset],
                            "required_proof": [required_proof],
                            "side_effect_class": side_effect_class,
                        }
                    ],
                    "triggers": [{"type": "timer", "key": "contract_tick"}],
                    "exit_criteria": [
                        {"transition": "done", "evidence_required": [required_proof]}
                    ],
                },
                {
                    "key": "done",
                    "actions": [{"key": "archive"}],
                    "exit_criteria": [],
                }
            ],
        },
        "entities": [
            {
                "key": "mock_work_item",
                "type": "work_item",
                "states": ["open", "done"],
                "terminal_states": ["done"],
            }
        ],
        "event_loops": [
            {"type": "timer", "entity": "mock_work_item", "terminal_states": ["done"]}
        ],
        "approval_gates": [
            {"key": "side_effect_approval", "required_before": ["research"]}
        ],
        "proof_requirements": [required_proof],
        "side_effect_policy": {
            "allowed": [side_effect_class],
            "forbidden": ["undeclared_side_effect"],
        },
        "escalation_paths": [
            {"condition": "contract proof or side-effect authority is unclear", "to": "mock-ceo"}
        ],
        "owner_summary": {
            "summary": "The board runs mock contracted work and advances only with declared proof."
        },
    }


def _approve_contract_board(slug, contract):
    kb.review_business_launch_contract(
        slug,
        contract=contract,
        create_if_missing=True,
    )
    token = kb.issue_board_launch_approval_token(
        slug,
        contract=contract,
        approved_by="test-owner",
        approval_evidence={"source": "contract-runtime-test"},
        owner_authority_confirmed=True,
    )["token"]
    result = kb.review_business_launch_contract(
        slug,
        contract=contract,
        approve=True,
        author="test-owner",
        approval_token=token,
    )
    assert result["ok"] is True
    assert result["launch_phase"] == "active"
    assert result["launch_review_id"]


def _create_contract_board(contract):
    _approve_contract_board("serious", contract)
    with kb.connect(board="serious") as conn:
        tid = kb.create_task(
            conn,
            title="contracted work",
            assignee="mock-worker",
            goal_id="mock-goal",
            workstream_id="ops",
            stage_key="execute",
            action_key="research",
            board="serious",
        )
    return tid


def test_dispatch_blocks_missing_capability_and_provider_policy(fresh_home):
    tid = _create_contract_board(
        _contract(
            worker_capabilities=["other_capability"],
            worker_toolsets=["kanban"],
            provider_policy={"other_capability": {"provider": "fixture"}},
        )
    )

    with kb.connect(board="serious") as conn:
        verdict = kb.evaluate_dispatch_eligibility(conn, tid, board="serious")
        codes = {b["code"] for b in verdict["blockers"]}
        assert verdict["ok"] is False
        assert "missing_capabilities" in codes
        assert "missing_provider_policy" in codes

        dry = kb.dispatch_once(conn, dry_run=True, board="serious")
        assert dry.contract_blocked == [(tid, ["missing_capabilities", "missing_provider_policy"])]
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"

        live = kb.dispatch_once(conn, dry_run=False, board="serious")
        assert live.contract_blocked == [(tid, ["missing_capabilities", "missing_provider_policy"])]
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert "dispatch eligibility failed" in (task.last_failure_error or "")
        events = [event.kind for event in kb.list_events(conn, tid)]
        assert "dispatch_contract_blocked" in events


def test_done_gate_requires_structured_proof_before_completion(fresh_home):
    tid = _create_contract_board(_contract(required_proof="mock_report"))

    with kb.connect(board="serious") as conn:
        assert kb.claim_task(conn, tid) is not None
        with pytest.raises(kb.ContractDoneGateError) as excinfo:
            kb.complete_task(conn, tid, summary="I did it", metadata={"proof": ["wrong"]}, board="serious")
        assert excinfo.value.verdict["blockers"][0]["code"] == "missing_required_proof"
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "running"
        events = [event.kind for event in kb.list_events(conn, tid)]
        assert "contract_done_blocked" in events

        assert kb.complete_task(
            conn,
            tid,
            summary="Done with structured proof",
            metadata={"proof": ["mock_report"]},
            board="serious",
        ) is True
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done"


def test_worker_envelope_fields_fail_closed_when_empty(fresh_home):
    tid = _create_contract_board(
        _contract(
            worker_capabilities=[],
            worker_toolsets=[],
            allowed_side_effects=[],
        )
    )

    with kb.connect(board="serious") as conn:
        verdict = kb.evaluate_dispatch_eligibility(conn, tid, board="serious")
        codes = {b["code"] for b in verdict["blockers"]}
        assert verdict["ok"] is False
        assert {"missing_capabilities", "missing_toolsets", "side_effect_not_allowed"} <= codes


def test_empty_provider_policy_value_is_not_a_material_route(fresh_home):
    tid = _create_contract_board(_contract(provider_policy={"mock_research": {}}))

    with kb.connect(board="serious") as conn:
        verdict = kb.evaluate_dispatch_eligibility(conn, tid, board="serious")
        codes = {b["code"] for b in verdict["blockers"]}
        assert verdict["ok"] is False
        assert "empty_provider_policy" in codes


def test_direct_claim_is_gated_on_serious_board(fresh_home):
    tid = _create_contract_board(_contract(worker_capabilities=[]))

    with kb.connect(board="serious") as conn:
        assert kb.claim_task(conn, tid) is None
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert "claim eligibility failed" in (task.last_failure_error or "")
        assert "claim_contract_blocked" in [event.kind for event in kb.list_events(conn, tid)]


def test_contract_completion_requires_running_eligible_claim(fresh_home):
    tid = _create_contract_board(_contract())

    with kb.connect(board="serious") as conn:
        with pytest.raises(kb.ContractDoneGateError) as excinfo:
            kb.complete_task(
                conn,
                tid,
                summary="Trying to bypass the run",
                metadata={"proof": ["mock_report"]},
            )
        codes = {b["code"] for b in excinfo.value.verdict["blockers"]}
        assert "missing_eligible_run" in codes
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"


def test_completion_proof_must_be_submitted_at_completion_time(fresh_home):
    tid = _create_contract_board(_contract())

    with kb.connect(board="serious") as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET funnel_data = ? WHERE id = ?",
                ('{"proof": ["mock_report"]}', tid),
            )
        assert kb.claim_task(conn, tid) is not None
        with pytest.raises(kb.ContractDoneGateError) as excinfo:
            kb.complete_task(conn, tid, summary="Pre-seeded proof only", metadata={})
        codes = {b["code"] for b in excinfo.value.verdict["blockers"]}
        assert "missing_required_proof" in codes


def test_worker_envelope_required_proof_is_merged(fresh_home):
    tid = _create_contract_board(_contract(worker_required_proof=["worker_receipt"]))

    with kb.connect(board="serious") as conn:
        assert kb.claim_task(conn, tid) is not None
        with pytest.raises(kb.ContractDoneGateError) as excinfo:
            kb.complete_task(
                conn,
                tid,
                summary="Action proof but no worker envelope proof",
                metadata={"proof": ["mock_report"]},
            )
        missing = [
            b.get("missing", [])
            for b in excinfo.value.verdict["blockers"]
            if b.get("code") == "missing_required_proof"
        ][0]
        assert "worker_receipt" in missing
        assert kb.complete_task(
            conn,
            tid,
            summary="All proof submitted",
            metadata={"proof": ["mock_report", "worker_receipt"]},
        ) is True


def test_missing_worker_envelope_blocks_toolset_only_contract(fresh_home):
    contract = _contract(required_capability="")
    contract["runtime"]["provider_policy"] = {"kanban_dispatch": {"provider": "fixture"}}
    contract["runtime"]["worker_envelopes"] = {
        "other-worker": {"toolsets": ["kanban"], "capabilities": []}
    }
    action = contract["workflow"]["stages"][0]["actions"][0]
    action["required_capabilities"] = []
    action["required_toolsets"] = ["kanban"]
    action["required_proof"] = []
    tid = _create_contract_board(contract)

    with kb.connect(board="serious") as conn:
        verdict = kb.evaluate_dispatch_eligibility(conn, tid)
        assert verdict["ok"] is False
        assert "missing_worker_envelope" in {b["code"] for b in verdict["blockers"]}


def test_provider_policy_allowed_for_denies_wrong_worker_and_is_not_route(fresh_home):
    denied = _create_contract_board(
        _contract(provider_policy={"mock_research": {"provider": "fixture", "allowed_for": ["other-worker"]}})
    )
    with kb.connect(board="serious") as conn:
        verdict = kb.evaluate_dispatch_eligibility(conn, denied)
        assert "provider_policy_denied" in {b["code"] for b in verdict["blockers"]}

    kb.remove_board("serious", archive=False)
    only_acl = _create_contract_board(
        _contract(provider_policy={"mock_research": {"allowed_for": ["mock-worker"]}})
    )
    with kb.connect(board="serious") as conn:
        verdict = kb.evaluate_dispatch_eligibility(conn, only_acl)
        assert "empty_provider_policy" in {b["code"] for b in verdict["blockers"]}


def test_task_local_contract_cannot_weaken_board_side_effect_policy(fresh_home):
    contract = _contract(side_effect_class="external_write", allowed_side_effects=["none"])
    _approve_contract_board("serious", contract)
    with kb.connect(board="serious") as conn:
        tid = kb.create_task(
            conn,
            title="attempt side-effect downgrade",
            assignee="mock-worker",
            goal_id="mock-goal",
            workstream_id="ops",
            stage_key="execute",
            action_key="research",
            funnel_data={"execution_contract": {"side_effect_class": "none"}},
            board="serious",
        )
        verdict = kb.evaluate_dispatch_eligibility(conn, tid)
        assert "side_effect_not_allowed" in {b["code"] for b in verdict["blockers"]}


@pytest.mark.parametrize("status", ["ready", "blocked", "watching"])
def test_completion_bypass_blocked_from_non_running_statuses(fresh_home, status):
    tid = _create_contract_board(_contract(worker_required_proof=["worker_receipt"]))
    with kb.connect(board="serious") as conn:
        if status != "ready":
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        with pytest.raises(kb.ContractDoneGateError) as excinfo:
            kb.complete_task(
                conn,
                tid,
                summary="bypass from non-running status",
                metadata={"proof": ["mock_report", "worker_receipt"]},
            )
        assert "missing_eligible_run" in {b["code"] for b in excinfo.value.verdict["blockers"]}
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == status


def test_review_claim_is_contract_gated(fresh_home):
    tid = _create_contract_board(_contract(worker_capabilities=[]))
    with kb.connect(board="serious") as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        assert kb.claim_review_task(conn, tid) is None
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert "claim_contract_blocked" in [event.kind for event in kb.list_events(conn, tid)]


def test_running_contract_completion_requires_fresh_claim(fresh_home):
    tid = _create_contract_board(_contract())
    with kb.connect(board="serious") as conn:
        assert kb.claim_task(conn, tid) is not None
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (tid,))
        with pytest.raises(kb.ContractDoneGateError) as excinfo:
            kb.complete_task(conn, tid, summary="expired claim", metadata={"proof": ["mock_report"]})
        assert "stale_claim" in {b["code"] for b in excinfo.value.verdict["blockers"]}


def test_malformed_board_metadata_fails_closed(fresh_home):
    tid = _create_contract_board(_contract())
    kb.board_metadata_path("serious").write_text("{not-json", encoding="utf-8")
    with kb.connect(board="serious") as conn:
        verdict = kb.evaluate_dispatch_eligibility(conn, tid)
        assert "board_metadata_invalid" in {b["code"] for b in verdict["blockers"]}
        assert kb.claim_task(conn, tid) is None
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"

def test_explicit_board_cannot_spoof_connected_serious_board(fresh_home):
    tid = _create_contract_board(_contract(worker_capabilities=[]))
    with kb.connect(board="serious") as conn:
        with pytest.raises(ValueError, match="does not match connected board"):
            kb.claim_task(conn, tid, board="default")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"

    kb.remove_board("serious", archive=False)
    tid = _create_contract_board(_contract())
    with kb.connect(board="serious") as conn:
        assert kb.claim_task(conn, tid) is not None
        with pytest.raises(ValueError, match="does not match connected board"):
            kb.complete_task(conn, tid, summary="spoofed board", metadata={}, board="default")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "running"


def test_worker_tool_cannot_spoof_env_pinned_board(fresh_home, monkeypatch):
    tid = _create_contract_board(_contract())
    with kb.connect(board="serious") as conn:
        assert kb.claim_task(conn, tid) is not None
        run = kb.latest_run(conn, tid)
        assert run is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "serious")
        monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board="serious")))
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.id))

        from tools import kanban_tools as kt
        out = kt._handle_complete({"board": "default", "summary": "spoofed no-proof completion"})
        payload = json.loads(out)
        assert "error" in payload
        assert "does not match" in payload["error"]
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "running"

def test_db_path_pin_without_board_env_cannot_be_mislabeled(fresh_home, monkeypatch):
    tid = _create_contract_board(_contract())
    serious_db = str(kb.kanban_db_path(board="serious"))
    monkeypatch.setenv("HERMES_KANBAN_DB", serious_db)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    with pytest.raises(ValueError, match="does not match pinned DB board"):
        kb.connect(board="default")

    with kb.connect(board="serious") as conn:
        attached = getattr(conn, "_board_slug")
        assert attached == "serious"
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"


def test_explicit_db_path_cannot_mislabel_serious_board(fresh_home):
    tid = _create_contract_board(_contract())
    serious_db = kb.kanban_db_path(board="serious")

    with kb.connect(db_path=serious_db) as conn:
        assert getattr(conn, "_board_slug") == "serious"
        verdict = kb.evaluate_dispatch_eligibility(conn, tid)
        assert verdict["contract"]["board"] == "serious"
        assert kb.claim_task(conn, tid) is not None
        with pytest.raises(kb.ContractDoneGateError) as excinfo:
            kb.complete_task(conn, tid, summary="db_path spoof", metadata={})
        assert "missing_required_proof" in {b["code"] for b in excinfo.value.verdict["blockers"]}

    with pytest.raises(ValueError, match="does not match pinned DB board"):
        kb.connect(db_path=serious_db, board="default")


def test_windows_connection_wrapper_preserves_board_identity(fresh_home, monkeypatch):
    tid = _create_contract_board(_contract())
    serious_db = kb.kanban_db_path(board="serious")
    monkeypatch.setattr(kb, "_IS_WINDOWS", True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(serious_db))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    with pytest.raises(ValueError, match="does not match pinned DB board"):
        kb.connect(board="default")

    with kb.connect() as conn:
        assert getattr(conn, "_board_slug") == "serious"
        verdict = kb.evaluate_dispatch_eligibility(conn, tid)
        assert verdict["contract"]["board"] == "serious"
        assert kb.claim_task(conn, tid) is not None
        with pytest.raises(kb.ContractDoneGateError):
            kb.complete_task(conn, tid, summary="windows spoof", metadata={})


def test_create_task_uses_connected_board_contract_when_board_omitted(fresh_home):
    _approve_contract_board("serious", _contract())
    with kb.connect(board="serious") as conn:
        with pytest.raises(ValueError, match="requires workstream_id"):
            kb.create_task(conn, title="unstructured bypass", assignee="mock-worker")

        tid = kb.create_task(
            conn,
            title="structured without explicit board",
            assignee="mock-worker",
            goal_id="mock-goal",
            workstream_id="ops",
            stage_key="execute",
            action_key="research",
        )
        verdict = kb.evaluate_dispatch_eligibility(conn, tid)
        assert verdict["contract"]["board"] == "serious"
        assert verdict["contract"]["required_proof"] == ["mock_report"]


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("action_key", None, "missing_semantic_action"),
        ("stage_key", "unknown", "unknown_stage"),
    ],
)
def test_dispatch_blocks_strict_workflow_semantic_drift(
    fresh_home, field, value, expected_code,
):
    tid = _create_contract_board(_contract())
    with kb.connect(board="serious") as conn:
        with kb.write_txn(conn):
            conn.execute(f"UPDATE tasks SET {field} = ? WHERE id = ?", (value, tid))

        verdict = kb.evaluate_dispatch_eligibility(conn, tid)
        assert verdict["ok"] is False
        assert expected_code in {b["code"] for b in verdict["blockers"]}

        dry = kb.dispatch_once(conn, dry_run=True, board="serious")
        assert dry.contract_blocked == [(tid, [expected_code])]

        live = kb.dispatch_once(conn, dry_run=False, board="serious")
        assert live.contract_blocked == [(tid, [expected_code])]
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert "dispatch_contract_blocked" in [event.kind for event in kb.list_events(conn, tid)]


def test_non_object_board_metadata_fails_closed(fresh_home):
    tid = _create_contract_board(_contract())
    kb.board_metadata_path("serious").write_text("[]", encoding="utf-8")
    with kb.connect(board="serious") as conn:
        verdict = kb.evaluate_dispatch_eligibility(conn, tid)
        codes = {b["code"] for b in verdict["blockers"]}
        assert verdict["ok"] is False
        assert "board_metadata_invalid" in codes
        assert kb.claim_task(conn, tid) is None
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"


def test_non_dict_completion_metadata_cannot_satisfy_structured_proof(fresh_home):
    tid = _create_contract_board(_contract(required_proof="mock_report"))
    with kb.connect(board="serious") as conn:
        assert kb.claim_task(conn, tid) is not None
        with pytest.raises(kb.ContractDoneGateError) as excinfo:
            kb.complete_task(conn, tid, summary="string metadata bypass", metadata="mock_report")  # type: ignore[arg-type]
        codes = {b["code"] for b in excinfo.value.verdict["blockers"]}
        assert "invalid_completion_metadata" in codes
        assert "missing_required_proof" in codes
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "running"


def test_provider_policy_list_entries_must_be_material_routes(fresh_home):
    for provider_policy in (
        {"mock_research": {"providers": [{}]}},
        {"mock_research": {"providers": [None]}},
    ):
        tid = _create_contract_board(_contract(provider_policy=provider_policy))
        with kb.connect(board="serious") as conn:
            verdict = kb.evaluate_dispatch_eligibility(conn, tid)
            assert verdict["ok"] is False
            assert "empty_provider_policy" in {b["code"] for b in verdict["blockers"]}
        kb.remove_board("serious", archive=False)


def test_malformed_worker_envelope_dict_fields_fail_closed(fresh_home):
    tid = _create_contract_board(
        _contract(
            worker_capabilities={"mock_research": False},
            worker_toolsets={"kanban": False},
            allowed_side_effects={"none": False},
        )
    )
    with kb.connect(board="serious") as conn:
        verdict = kb.evaluate_dispatch_eligibility(conn, tid)
        codes = {b["code"] for b in verdict["blockers"]}
        assert verdict["ok"] is False
        assert {"missing_capabilities", "missing_toolsets", "side_effect_not_allowed"} <= codes
