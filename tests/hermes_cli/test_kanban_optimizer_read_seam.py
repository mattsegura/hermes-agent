"""E2 read-seam tests: dispatcher resolves concurrency caps from the contract.

The gateway reads ``max_in_progress`` / ``max_in_progress_per_profile`` /
``max_spawn`` from ``config.yaml`` and passes them to ``dispatch_once``. The E2
read-seam makes ``dispatch_once`` resolve each cap from the board contract's
``runtime.tunables[knob].default`` FIRST, with the passed-in config value as
FALLBACK -- mirroring how ``managed_cadence_default`` feeds the reactive cadence.
Without this an optimizer write to a cap tunable was a silent no-op.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest


@pytest.fixture()
def isolated_kanban_home_with_profiles(monkeypatch):
    """Fresh HERMES_HOME with kanban DB + alpha/beta/default profiles."""
    test_home = tempfile.mkdtemp(prefix="kanban_read_seam_test_")
    for prof in ("alpha", "beta", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db


def _fake_spawn(*args, **kwargs):
    return 12345


def _contract_with_cap(cap_knob: str, default) -> dict:
    """Minimal contract carrying a cap tunable under runtime.tunables.

    Used both for the pure ``managed_cap_default`` tests and (via
    ``_set_board_cap_tunable``) for the dispatch wiring tests.
    """
    return {
        "objective": {
            "statement": "cap-tunable board",
            "success": ["ok"],
            "failure": ["bad"],
            "constraints": ["none"],
        },
        "runtime": {
            "mode": "company",
            "dispatcher": {"profile": "default"},
            "tunables": {
                cap_knob: {"default": default, "range": [1, 16]},
            },
        },
        "workflow": {
            "id": "cap-flow",
            "stages": [{"key": "execute", "actions": [{"key": "go"}]}],
        },
    }


def _set_board_cap_tunable(kb, slug: str, cap_knob: str, default) -> None:
    """Declare a cap tunable on a NON-managed board (so the dispatch gate stays
    open). Mode ``goal`` keeps the board out of the managed launch-gate path
    while ``find_knob_spec`` still reads ``runtime.tunables``.
    """
    kb.write_board_metadata(
        slug,
        runtime={
            "mode": "goal",
            "dispatcher": {"profile": "default"},
            "tunables": {cap_knob: {"default": default, "range": [1, 16]}},
        },
    )


# ---------------------------------------------------------------------------
# Pure helper: managed_cap_default
# ---------------------------------------------------------------------------


def test_managed_cap_default_reads_runtime_tunable():
    from hermes_cli import kanban_optimizer as opt

    contract = _contract_with_cap("max_in_progress", 3)
    assert opt.managed_cap_default(contract, "max_in_progress") == 3


def test_managed_cap_default_rejects_bool_and_below_one():
    from hermes_cli import kanban_optimizer as opt

    assert opt.managed_cap_default(_contract_with_cap("max_spawn", True), "max_spawn") is None
    assert opt.managed_cap_default(_contract_with_cap("max_spawn", 0), "max_spawn") is None
    assert opt.managed_cap_default(_contract_with_cap("max_spawn", -2), "max_spawn") is None


def test_managed_cap_default_absent_knob_is_none():
    from hermes_cli import kanban_optimizer as opt

    contract = _contract_with_cap("max_in_progress", 3)
    assert opt.managed_cap_default(contract, "max_spawn") is None
    assert opt.managed_cap_default(None, "max_in_progress") is None


def test_managed_cap_default_coerces_float():
    from hermes_cli import kanban_optimizer as opt

    contract = _contract_with_cap("max_in_progress", 4.0)
    assert opt.managed_cap_default(contract, "max_in_progress") == 4


# ---------------------------------------------------------------------------
# Read-seam wired into dispatch_once
# ---------------------------------------------------------------------------


def test_contract_per_profile_cap_overrides_config(isolated_kanban_home_with_profiles):
    """A contract tunable for max_in_progress_per_profile overrides config.

    Config says cap=5 (would let 5 alpha dispatch), but the contract tunable
    says 2 -> only 2 alpha dispatch and 3 are per-profile-capped.
    """
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(5):
            kb.create_task(conn, title=f"a{i}", assignee="alpha")
    _set_board_cap_tunable(kb, "default", "max_in_progress_per_profile", 2)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_per_profile=5,  # config fallback (higher)
        )
    spawn_assignees = [s[1] for s in res.spawned]
    assert spawn_assignees.count("alpha") == 2
    assert len(res.skipped_per_profile_capped) == 3


def test_config_per_profile_cap_used_when_no_tunable(isolated_kanban_home_with_profiles):
    """With NO cap tunable in the contract, the config value is the fallback."""
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        # No cap tunable -> config cap applies unchanged.
        for i in range(5):
            kb.create_task(conn, title=f"a{i}", assignee="alpha")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_per_profile=2,
        )
    spawn_assignees = [s[1] for s in res.spawned]
    assert spawn_assignees.count("alpha") == 2
    assert len(res.skipped_per_profile_capped) == 3


def test_contract_max_in_progress_overrides_config(isolated_kanban_home_with_profiles):
    """A contract tunable for max_in_progress caps total running below config.

    Config max_in_progress is unset (None); the contract sets 1. With 1 already
    running, the board is at its (contract) cap so NO new task dispatches.
    """
    kb = isolated_kanban_home_with_profiles
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        running = kb.create_task(conn, title="running", assignee="alpha")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = 'test:1' WHERE id = ?",
                (running,),
            )
        kb.create_task(conn, title="ready", assignee="alpha")
    _set_board_cap_tunable(kb, "default", "max_in_progress", 1)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress=None,  # config has no cap; contract supplies it
        )
    assert len(res.spawned) == 0
