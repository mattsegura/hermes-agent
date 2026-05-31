"""Hardening Pass 2 -- WS1: atomic + versioned contract writes.

board.json is the operating contract's source of truth. These tests prove:

* the write is **atomic** -- a crash at the rename boundary leaves the original
  board.json fully intact (never a half-written file), and
* a monotonic ``contract_version`` + **compare-and-swap** rejects a stale write
  (``ContractVersionConflict``) instead of silently clobbering a contract that
  another writer already advanced, and the happy path bumps the version.
"""

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


# ---------------------------------------------------------------------------
# Atomicity: a crash at the os.replace boundary never corrupts board.json.
# ---------------------------------------------------------------------------


def test_atomic_write_survives_midwrite_crash(fresh_home, monkeypatch):
    kb.create_board("atom", name="Atomic")
    path = kb.board_metadata_path("atom")
    original = path.read_text(encoding="utf-8")
    original_obj = json.loads(original)

    # Simulate a crash exactly at the rename: the temp file is written and
    # fsync'd, but the atomic swap never lands.
    real_replace = kb.os.replace

    def boom(src, dst):  # noqa: ANN001
        raise OSError("simulated crash before atomic rename")

    monkeypatch.setattr(kb.os, "replace", boom)

    with pytest.raises(OSError):
        kb.write_board_metadata("atom", name="Renamed Mid-Crash")

    monkeypatch.setattr(kb.os, "replace", real_replace)

    # Original is byte-for-byte intact -- no half-written board.json.
    assert path.read_text(encoding="utf-8") == original
    assert json.loads(path.read_text(encoding="utf-8")) == original_obj
    # And no stray temp files were left behind in the board dir.
    leftovers = list(path.parent.glob(f".{path.name}.tmp-*"))
    assert leftovers == [], leftovers


def test_atomic_write_replaces_cleanly_on_happy_path(fresh_home):
    kb.create_board("atom2", name="Atomic2")
    kb.write_board_metadata("atom2", description="new desc")
    meta = kb.read_board_metadata("atom2")
    assert meta["description"] == "new desc"
    path = kb.board_metadata_path("atom2")
    assert list(path.parent.glob(f".{path.name}.tmp-*")) == []


# ---------------------------------------------------------------------------
# Compare-and-swap on contract_version.
# ---------------------------------------------------------------------------


def test_cas_happy_path_bumps_version(fresh_home):
    kb.create_board("cas", name="CAS")
    start = kb.read_board_metadata("cas")["contract_version"]

    # A write that expects the current version succeeds and advances it.
    kb.write_board_metadata(
        "cas",
        description="v2",
        contract_version=start + 1,
        expected_version=start,
    )
    assert kb.read_board_metadata("cas")["contract_version"] == start + 1


def test_cas_rejects_stale_version(fresh_home):
    kb.create_board("cas2", name="CAS2")
    start = kb.read_board_metadata("cas2")["contract_version"]

    # Writer A advances the contract to start+1.
    kb.write_board_metadata(
        "cas2", description="A", contract_version=start + 1, expected_version=start
    )

    # Writer B read the old version and tries to write against it -> rejected.
    with pytest.raises(kb.ContractVersionConflict) as exc:
        kb.write_board_metadata(
            "cas2", description="B (stale)", contract_version=start + 1, expected_version=start
        )
    assert exc.value.expected == start
    assert exc.value.actual == start + 1
    # The losing write left no trace: contract is still A's content/version.
    meta = kb.read_board_metadata("cas2")
    assert meta["contract_version"] == start + 1
    assert meta["description"] == "A"


def test_no_expected_version_keeps_legacy_behavior(fresh_home):
    # Omitting expected_version must NOT enforce CAS (back-compat).
    kb.create_board("cas3", name="CAS3")
    kb.write_board_metadata("cas3", description="free write")  # no expected_version
    assert kb.read_board_metadata("cas3")["description"] == "free write"


# ---------------------------------------------------------------------------
# FIX 5: launch_intake.completeness is TELEMETRY, not contract semantics -- it
# must not perturb the business-contract hash (which approval tokens and the
# contract_version bind to). The degraded universal-drafter fallback attaches
# this block with all enforcement flags OFF; if it changed the hash, a no-op
# upgrade would invalidate approval tokens / bump contract_version.
# ---------------------------------------------------------------------------


def _intake_contract(*, with_completeness):
    contract = {
        "objective": {
            "statement": "Run a mock board",
            "success": ["proof is structured"],
            "failure": ["worker escapes"],
            "constraints": ["mock only"],
        },
        "launch_intake": {
            "answers": {"goal": "do the thing"},
            "owner_review_required": True,
        },
    }
    if with_completeness:
        contract["launch_intake"]["completeness"] = {
            "ok": True,
            "errors": [],
            "warnings": ["[success_scoreability] success criteria are not scoreable"],
            "dimensions": {"invariants": {"findings": ["warning: soft"], "enforced": True}},
            "enforced_dimensions": ["success_scoreability"],
            "blocking": [],
        }
    return contract


def test_completeness_telemetry_does_not_change_contract_hash(fresh_home):
    # A contract WITH vs WITHOUT the launch_intake.completeness block hashes
    # IDENTICALLY. FAILS before FIX 5 (normalize preserved the block verbatim,
    # so the degraded-path contract drifted the hash head-vs-base).
    h_plain = kb._business_contract_hash(_intake_contract(with_completeness=False))
    h_telemetry = kb._business_contract_hash(_intake_contract(with_completeness=True))
    assert h_plain == h_telemetry, (
        "launch_intake.completeness telemetry must not change the contract hash"
    )


def test_approval_token_binding_unaffected_by_completeness_telemetry(fresh_home):
    # The approval-token contract binding compares the bound contract_hash to the
    # hash of the contract presented at verify time (_validate_approval_token_record).
    # A token issued against the plain contract therefore still matches the SAME
    # contract after the degraded fallback attaches completeness telemetry,
    # because the hash is identical. We assert that binding hash equivalence
    # directly (the level the token validator actually compares).
    plain = _intake_contract(with_completeness=False)
    with_tel = _intake_contract(with_completeness=True)

    bound_hash = kb._business_contract_hash(plain)
    presented_hash = kb._business_contract_hash(with_tel)
    assert bound_hash == presented_hash

    # The token validator rejects only when the hashes differ; identical hashes
    # pass the contract-binding check. Build a minimal pending record bound to
    # the plain hash and verify it validates against the telemetry-carrying hash.
    record = {
        "status": "pending",
        "board": "casintake",
        "kind": "launch",
        "contract_version": 1,
        "contract_hash": bound_hash,
        "amendment_id": None,
    }
    # Must NOT raise: the telemetry-carrying contract presents the same hash.
    kb._validate_approval_token_record(
        record,
        board="casintake",
        kind="launch",
        contract_hash=presented_hash,
        contract_version=1,
    )
