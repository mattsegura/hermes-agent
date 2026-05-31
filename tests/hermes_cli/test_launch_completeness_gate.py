"""G1 keystone: board_dispatch_gate consults the ONE LaunchCompletenessSpec.

These tests pin the spec->gate enforce seam end-to-end:

  * DEFAULT (no dimension enforced) is a perfect no-op -- the gate's verdict is
    byte-identical to before, completeness findings stay advisory warnings. This
    is the guarantee that LANDING the seam breaks no live board.
  * Flipping a dimension to enforce (the soak->flip control surface) makes the
    gate fail closed with a DISTINCT launch_completeness_failed blocker -- and,
    importantly, can block an ALREADY-APPROVED board (the reason a report-mode
    soak must precede any flip).
  * Enforcement is precise: a contract that SATISFIES the enforced dimension is
    not blocked (no false positive that would strand a compliant board).

The enforce set is resolved from env HERMES_LAUNCH_COMPLETENESS_ENFORCE
(comma-list of dimension names or 'all').
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_WORKTREE = pathlib.Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb

_crt_spec = importlib.util.spec_from_file_location(
    "_crt_helpers", pathlib.Path(__file__).with_name("test_kanban_contract_runtime.py")
)
_crt = importlib.util.module_from_spec(_crt_spec)
_crt_spec.loader.exec_module(_crt)
_contract = _crt._contract
_approve_contract_board = _crt._approve_contract_board


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
        "HERMES_LAUNCH_COMPLETENESS_ENFORCE",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _codes(gate):
    return {b["code"] for b in gate.get("blockers", [])}


def test_default_off_is_a_noop(fresh_home):
    """An approved board with a prose (un-scoreable) success criterion: with no
    dimension enforced, the gate is OK and no completeness blocker appears -- the
    finding is advisory only."""
    c = _contract()  # default success is prose ("proof is structured")
    _approve_contract_board("probe", c)
    gate = kb.board_dispatch_gate("probe")
    assert gate["ok"] is True
    assert "launch_completeness_failed" not in _codes(gate)
    # the finding is still visible as an advisory warning in readiness
    warns = gate["readiness"].get("warnings") or []
    assert any("success_scoreability" in w for w in warns)
    assert gate["readiness"]["completeness"]["blocking"] == []


def test_enforce_flip_blocks_already_approved_board(fresh_home, monkeypatch):
    """Approve under report mode, THEN flip success_scoreability to enforce: the
    gate now fails closed with the distinct launch_completeness_failed blocker.
    Demonstrates why a flip must follow a report-mode soak -- it can block a
    live, already-approved board."""
    c = _contract()  # prose success
    _approve_contract_board("probe", c)
    assert kb.board_dispatch_gate("probe")["ok"] is True  # approved fine under report mode

    monkeypatch.setenv("HERMES_LAUNCH_COMPLETENESS_ENFORCE", "success_scoreability")
    gate = kb.board_dispatch_gate("probe")
    assert gate["ok"] is False
    assert "launch_completeness_failed" in _codes(gate)
    blocker = next(b for b in gate["blockers"] if b["code"] == "launch_completeness_failed")
    assert blocker["enforced_dimensions"] == ["success_scoreability"]
    assert any("success_scoreability" in f for f in blocker["findings"])


def test_enforcement_is_precise_compliant_contract_not_blocked(fresh_home, monkeypatch):
    """A contract that SATISFIES the enforced dimension (scoreable success) is
    NOT blocked when that dimension is enforced -- no false positive that would
    strand a compliant board."""
    c = _contract()
    c["objective"]["success"] = ["MRR to $12k by day 7", "30 paying customers"]
    _approve_contract_board("probe", c)

    monkeypatch.setenv("HERMES_LAUNCH_COMPLETENESS_ENFORCE", "success_scoreability")
    gate = kb.board_dispatch_gate("probe")
    assert gate["ok"] is True
    assert "launch_completeness_failed" not in _codes(gate)


def test_unknown_dimension_token_cannot_enforce(fresh_home, monkeypatch):
    """A typo in the enforce config is ignored -- it can neither silently enforce
    nothing-special nor accidentally enforce everything."""
    c = _contract()  # prose success
    _approve_contract_board("probe", c)
    monkeypatch.setenv("HERMES_LAUNCH_COMPLETENESS_ENFORCE", "succes_scoreabilty_typo")
    gate = kb.board_dispatch_gate("probe")
    assert gate["ok"] is True
    assert "launch_completeness_failed" not in _codes(gate)
