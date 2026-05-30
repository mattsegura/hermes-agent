"""Tests for P5 -- the contract-amendment loop (self-evolution state machine).

Covers the full propose -> (owner inputs) -> approve -> validate -> mint
lifecycle plus the failure/concurrency rails (validation_failed, superseded via
compare-and-swap) and the closed-loop trigger hooks (a tripped circuit breaker
and an out-of-range optimizer knob proposal both spawn an owner-gated amendment).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb


_WORKTREE = Path(__file__).resolve().parents[2]
_FIXTURES = _WORKTREE / "tests" / "fixtures" / "launch_intake"


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


def _base_contract() -> dict:
    with open(_FIXTURES / "insurance_recruiting.contract.json") as fh:
        return json.load(fh)


def _activate_board(board: str, contract: dict) -> None:
    """Launch + activate a managed board with ``contract`` (version 1)."""
    kb.review_business_launch_contract(board, contract=contract, create_if_missing=True)
    token = kb.issue_board_launch_approval_token(
        board,
        contract=contract,
        approved_by="owner",
        approval_evidence={"source": "amendment-test-setup"},
        owner_authority_confirmed=True,
    )["token"]
    kb.review_business_launch_contract(
        board, contract=contract, approve=True, approval_token=token
    )
    assert kb.validate_board_launch_readiness(board)["ok"] is True


def _amendment_token(board: str, amendment_id: str) -> str:
    return kb.issue_board_launch_approval_token(
        board,
        amendment_id=amendment_id,
        approved_by="owner",
        approval_evidence={"source": f"approve:{amendment_id}"},
        owner_authority_confirmed=True,
    )["token"]


def _widen_nudges(contract: dict, *, default: int = 10, hi: int = 12) -> dict:
    proposed = copy.deepcopy(contract)
    proposed["tunables"]["max_nudges"]["range"] = [0, hi]
    proposed["tunables"]["max_nudges"]["default"] = default
    return proposed


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_full_happy_path_propose_approve_validate_mint(fresh_home):
    contract = _base_contract()
    _activate_board("acme", contract)
    proposed = _widen_nudges(contract)

    with kb.connect(board="acme") as conn:
        amendment = kb.propose_contract_amendment(
            conn, board="acme", origin="ceo",
            rationale="widen max_nudges for slower funnels",
            proposed_contract=proposed,
        )
        aid = amendment["amendment_id"]
        assert amendment["status"] == kb.AMENDMENT_STATUS_DRAFTED
        assert amendment["base_version"] == 1
        # propose must NOT touch the live contract.
        assert kb.read_board_metadata("acme")["contract_version"] == 1

    token = _amendment_token("acme", aid)

    with kb.connect(board="acme") as conn:
        approved = kb.approve_contract_amendment(
            conn, aid, board="acme", approver="owner", token=token,
        )
        assert approved["status"] == kb.AMENDMENT_STATUS_APPROVED

        validated = kb.validate_contract_amendment(conn, aid, board="acme")
        assert validated["status"] == kb.AMENDMENT_STATUS_VALIDATED
        report = validated["validation_report"]
        assert report["ok"] is True
        assert report["invariants"]["ok"] is True
        assert report["simulation"]["ok"] is True
        assert report["readiness"]["ok"] is True

        minted = kb.mint_contract_amendment(conn, aid, board="acme")
        assert minted["status"] == kb.AMENDMENT_STATUS_ACTIVE
        assert minted["minted_version"] == 2

        # An amendment activation signal was emitted.
        kinds = [
            json.loads(row["action"]).get("kind")
            for row in conn.execute(
                "SELECT action FROM board_signals WHERE board='acme' "
                "AND primitive_kind='amendment' ORDER BY id"
            ).fetchall()
        ]
        assert "proposed" in kinds and "minted" in kinds

    meta = kb.read_board_metadata("acme")
    assert meta["contract_version"] == 2
    assert meta["business_contract"]["tunables"]["max_nudges"]["range"] == [0, 12]
    # Runtime stays dispatch-enabled (the mint recorded an approved launch review).
    assert kb.validate_board_launch_readiness("acme")["ok"] is True


def test_diff_is_materialized_into_full_proposed_contract(fresh_home):
    contract = _base_contract()
    _activate_board("diffboard", contract)
    with kb.connect(board="diffboard") as conn:
        amendment = kb.propose_contract_amendment(
            conn, board="diffboard", origin="optimizer",
            rationale="bump default via diff",
            diff={"tunables": {"max_nudges": {"default": 5}}},
        )
        # The canonical representation is the full proposed contract, with the
        # diff retained only as provenance.
        assert amendment["diff"] == {"tunables": {"max_nudges": {"default": 5}}}
        assert amendment["proposed_contract"]["tunables"]["max_nudges"]["default"] == 5
        # Untouched fields are carried over from the current contract.
        assert "objective" in amendment["proposed_contract"]


# ---------------------------------------------------------------------------
# Owner inputs gate
# ---------------------------------------------------------------------------


def test_approve_blocked_until_required_inputs_satisfied(fresh_home):
    contract = _base_contract()
    _activate_board("gated", contract)
    proposed = _widen_nudges(contract)

    with kb.connect(board="gated") as conn:
        amendment = kb.propose_contract_amendment(
            conn, board="gated", origin="sensor",
            rationale="needs a paid api key",
            proposed_contract=proposed,
            required_inputs=[
                {"key": "api_key", "label": "Paid API key", "type": "secret"},
            ],
        )
        aid = amendment["amendment_id"]
        assert amendment["status"] == kb.AMENDMENT_STATUS_PENDING_INPUT

        # Approval is refused before the owner supplies required inputs -- the
        # inputs gate is checked before the token is even consulted.
        with pytest.raises(ValueError, match="requires owner inputs"):
            kb.approve_contract_amendment(
                conn, aid, board="gated", approver="owner", token="placeholder",
            )


def test_token_issuance_requires_inputs_then_approve_succeeds(fresh_home):
    contract = _base_contract()
    _activate_board("gated2", contract)
    proposed = _widen_nudges(contract)

    with kb.connect(board="gated2") as conn:
        amendment = kb.propose_contract_amendment(
            conn, board="gated2", origin="sensor",
            rationale="needs a paid api key",
            proposed_contract=proposed,
            required_inputs=[
                {"key": "api_key", "label": "Paid API key", "type": "secret"},
                {"key": "monthly_cap", "label": "Spend cap", "type": "number",
                 "inject_path": "tunables.budget_cap_units.default"},
            ],
        )
        aid = amendment["amendment_id"]

    # A token cannot be issued (and thus approval is impossible) until the owner
    # supplies the required inputs.
    with pytest.raises(ValueError, match="requires owner inputs"):
        _amendment_token("gated2", aid)

    with kb.connect(board="gated2") as conn:
        partial = kb.submit_amendment_inputs(
            conn, aid, {"api_key": "sk-live-123"}, board="gated2"
        )
        assert partial["inputs_satisfied"] is False
        assert "monthly_cap" in partial["missing_inputs"]
        assert partial["status"] == kb.AMENDMENT_STATUS_PENDING_INPUT

        done = kb.submit_amendment_inputs(
            conn, aid, {"monthly_cap": 250}, board="gated2"
        )
        assert done["inputs_satisfied"] is True
        assert done["status"] == kb.AMENDMENT_STATUS_DRAFTED
        # Non-secret input injected into the proposed contract; secret is NOT.
        assert done["proposed_contract"]["tunables"]["budget_cap_units"]["default"] == 250
        assert "api_key" not in json.dumps(done["proposed_contract"])
        assert done["provided_inputs"]["api_key"] == "sk-live-123"

    token = _amendment_token("gated2", aid)
    with kb.connect(board="gated2") as conn:
        approved = kb.approve_contract_amendment(
            conn, aid, board="gated2", approver="owner", token=token,
        )
        assert approved["status"] == kb.AMENDMENT_STATUS_APPROVED


# ---------------------------------------------------------------------------
# Validation failure
# ---------------------------------------------------------------------------


def test_validation_failure_marks_failed_and_leaves_live_contract_untouched(fresh_home):
    contract = _base_contract()
    _activate_board("badc", contract)
    # Drop the declared range from a tunable: passes launch readiness but breaks
    # the structural invariant bar (an unbounded knob is unsafe).
    proposed = copy.deepcopy(contract)
    proposed["tunables"]["max_nudges"].pop("range", None)

    with kb.connect(board="badc") as conn:
        aid = kb.propose_contract_amendment(
            conn, board="badc", origin="ceo", rationale="drop a knob range",
            proposed_contract=proposed,
        )["amendment_id"]

    token = _amendment_token("badc", aid)
    with kb.connect(board="badc") as conn:
        kb.approve_contract_amendment(conn, aid, board="badc", approver="owner", token=token)
        result = kb.validate_contract_amendment(conn, aid, board="badc")
        assert result["status"] == kb.AMENDMENT_STATUS_VALIDATION_FAILED
        report = result["validation_report"]
        assert report["ok"] is False
        assert any("invariant" in err for err in report["errors"])

        # Minting a non-validated amendment is refused.
        with pytest.raises(ValueError, match="must be validated"):
            kb.mint_contract_amendment(conn, aid, board="badc")

    # Live contract is completely untouched.
    assert kb.read_board_metadata("badc")["contract_version"] == 1
    assert "range" in kb.read_board_metadata("badc")["business_contract"]["tunables"]["max_nudges"]


# ---------------------------------------------------------------------------
# CAS / superseded + concurrency
# ---------------------------------------------------------------------------


def _drive_to_validated(board: str, contract: dict, *, default: int) -> str:
    proposed = _widen_nudges(contract, default=default)
    with kb.connect(board=board) as conn:
        aid = kb.propose_contract_amendment(
            conn, board=board, origin="ceo",
            rationale=f"widen to default {default}", proposed_contract=proposed,
        )["amendment_id"]
    token = _amendment_token(board, aid)
    with kb.connect(board=board) as conn:
        kb.approve_contract_amendment(conn, aid, board=board, approver="owner", token=token)
        kb.validate_contract_amendment(conn, aid, board=board)
    return aid


def test_concurrency_two_amendments_race_to_mint_exactly_one_wins(fresh_home):
    contract = _base_contract()
    _activate_board("race", contract)

    # Both proposals are drafted, approved, and validated against base version 1.
    aid_a = _drive_to_validated("race", contract, default=6)
    aid_b = _drive_to_validated("race", contract, default=9)

    with kb.connect(board="race") as conn:
        winner = kb.mint_contract_amendment(conn, aid_a, board="race")
        assert winner["status"] == kb.AMENDMENT_STATUS_ACTIVE
        assert winner["minted_version"] == 2

        # The loser's compare-and-swap loses: it is superseded, NOT clobbered.
        loser = kb.mint_contract_amendment(conn, aid_b, board="race")
        assert loser["status"] == kb.AMENDMENT_STATUS_SUPERSEDED
        assert loser.get("minted_version") is None

    meta = kb.read_board_metadata("race")
    assert meta["contract_version"] == 2
    # The winner's value is live; the loser never overwrote it.
    assert meta["business_contract"]["tunables"]["max_nudges"]["default"] == 6


def test_approve_on_stale_base_version_supersedes(fresh_home):
    contract = _base_contract()
    _activate_board("stale", contract)

    # First amendment advances the contract to version 2.
    aid_first = _drive_to_validated("stale", contract, default=6)
    with kb.connect(board="stale") as conn:
        kb.mint_contract_amendment(conn, aid_first, board="stale")
    assert kb.read_board_metadata("stale")["contract_version"] == 2

    # A second amendment was drafted against the now-stale base version 1.
    proposed = _widen_nudges(contract, default=8)
    with kb.connect(board="stale") as conn:
        aid_stale = kb.propose_contract_amendment(
            conn, board="stale", origin="ceo", rationale="stale base",
            proposed_contract=proposed, base_version=1,
        )["amendment_id"]
        # Approving a stale-based amendment is rejected (CAS guard) and the
        # amendment is superseded -- the owner must re-base.
        with pytest.raises(kb.ContractVersionConflict):
            kb.approve_contract_amendment(
                conn, aid_stale, board="stale", approver="owner", token="unused",
            )
        assert kb.get_contract_amendment(conn, aid_stale, board="stale")["status"] == (
            kb.AMENDMENT_STATUS_SUPERSEDED
        )


# ---------------------------------------------------------------------------
# Trigger hooks (the loop wired closed)
# ---------------------------------------------------------------------------


def test_optimizer_out_of_range_knob_drafts_amendment(fresh_home):
    contract = _base_contract()
    _activate_board("optb", contract)
    with kb.connect(board="optb") as conn:
        result = kb.apply_knob_update(
            conn, board="optb", knob="max_nudges", new_value=99, actor="optimizer",
        )
        assert result["status"] == "approval_required"
        aid = result["amendment_id"]
        assert aid is not None

        amendment = kb.get_contract_amendment(conn, aid, board="optb")
        assert amendment["origin"] == "optimizer"
        assert amendment["status"] == kb.AMENDMENT_STATUS_DRAFTED
        # The structural proposal widens the knob range to include the value.
        rng = amendment["proposed_contract"]["tunables"]["max_nudges"]["range"]
        assert rng[1] >= 99

        # A second out-of-range proposal for the same knob is deduped.
        again = kb.apply_knob_update(
            conn, board="optb", knob="max_nudges", new_value=120, actor="optimizer",
        )
        assert again["amendment_id"] is None


def test_circuit_breaker_open_drafts_sensor_amendment(fresh_home):
    contract = _base_contract()
    _activate_board("breaker", contract)

    now = 10_000_000
    with kb.connect(board="breaker") as conn:
        # Seed enough recent failures to trip the circuit breaker
        # (failure_rate_threshold=0.5, min_samples=5).
        with kb.write_txn(conn):
            for i in range(6):
                conn.execute(
                    "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) "
                    "VALUES (?, 'crashed', ?, ?, 'crashed')",
                    (f"t{i}", now - 100, now - 10),
                )
        tick = kb.sensors_tick(conn, board="breaker", now=now)
        circuit = tick.get("circuit") or []
        assert any(c.get("status") == "open" for c in circuit), tick

        # The open transition spawned an owner-gated structural amendment.
        proposed_ids = tick.get("amendments_proposed") or []
        assert proposed_ids, tick
        amendment = kb.get_contract_amendment(conn, proposed_ids[0], board="breaker")
        assert amendment["origin"] == "sensor"
        assert amendment["status"] == kb.AMENDMENT_STATUS_PENDING_INPUT
        assert [i["key"] for i in amendment["required_inputs"]] == ["api_key"]
        assert amendment["required_inputs"][0]["type"] == "secret"

        # Re-ticking does not pile up duplicate drafts.
        tick2 = kb.sensors_tick(conn, board="breaker", now=now + 5)
        assert not (tick2.get("amendments_proposed") or [])


# ---------------------------------------------------------------------------
# Read-model surfacing
# ---------------------------------------------------------------------------


def test_learned_state_read_model_surfaces_pending_amendments(fresh_home):
    contract = _base_contract()
    _activate_board("rm", contract)
    with kb.connect(board="rm") as conn:
        kb.propose_contract_amendment(
            conn, board="rm", origin="owner", rationale="pending one",
            proposed_contract=_widen_nudges(contract),
        )
        model = kb.build_learned_state_read_model(conn, board="rm")
        block = model["contract_amendments"]
        assert block["pending"] == 1
        assert block["amendments"][0]["origin"] == "owner"
        assert block["amendments"][0]["status"] == kb.AMENDMENT_STATUS_DRAFTED


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def _cli(args: list[str], hermes_home: Path, *, board: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "HERMES_HOME": str(hermes_home),
        "HERMES_KANBAN_BOARD": board,
        "PYTHONPATH": str(_WORKTREE),
    })
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", *args],
        env=env, cwd=str(_WORKTREE), capture_output=True, text=True, timeout=60,
    )


def test_cli_amendment_status_list_and_show(fresh_home):
    contract = _base_contract()
    _activate_board("cliboard", contract)
    with kb.connect(board="cliboard") as conn:
        aid = kb.propose_contract_amendment(
            conn, board="cliboard", origin="owner", rationale="cli surfaced",
            proposed_contract=_widen_nudges(contract),
        )["amendment_id"]

    status = _cli(["boards", "contract", "amendment", "status", "--json"], fresh_home, board="cliboard")
    assert status.returncode == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["pending"] == 1

    listing = _cli(["boards", "contract", "amendment", "list"], fresh_home, board="cliboard")
    assert listing.returncode == 0, listing.stderr
    assert aid in listing.stdout

    show = _cli(["boards", "contract", "amendment", "show", aid, "--json"], fresh_home, board="cliboard")
    assert show.returncode == 0, show.stderr
    assert json.loads(show.stdout)["amendment_id"] == aid
