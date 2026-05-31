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


# ===========================================================================
# FIX 4 -- enforce FAILS OPEN when the checker crashes/unavailable.
#   (a) a 'raised:'/'unavailable' invariants finding BLOCKS (denylist, not
#       error:-only allowlist).
#   (b) a crash in assess_launch_completeness under enforcement fails CLOSED.
# Plus FIX 7 gate-path coverage of the 'invariants' dimension.
# ===========================================================================


def test_fix4a_invariants_checker_crash_blocks_under_enforcement(fresh_home, monkeypatch):
    """ENFORCE=invariants with check_contract_invariants monkeypatched to RAISE:
    the assessor records a 'raised:' invariants finding which must BLOCK the gate.
    FAILS before FIX 4(a) (a 'raised:' finding was dropped by the error:-only
    filter -> gate stayed ok despite a crashed structural checker)."""
    import hermes_cli.launch_completeness as lc

    def _boom(_contract_arg):
        raise RuntimeError("invariants checker exploded")

    monkeypatch.setattr(lc, "check_contract_invariants", _boom)
    c = _contract()
    _approve_contract_board("probe", c)
    monkeypatch.setenv("HERMES_LAUNCH_COMPLETENESS_ENFORCE", "invariants")
    gate = kb.board_dispatch_gate("probe")
    assert gate["ok"] is False
    assert "launch_completeness_failed" in _codes(gate)
    blocker = next(b for b in gate["blockers"] if b["code"] == "launch_completeness_failed")
    assert any("raised:" in f for f in blocker["findings"]), (
        f"a crashed invariants checker must surface a blocking 'raised:' finding: {blocker}"
    )


def test_fix4a_invariants_warning_only_stays_ok_under_enforcement(fresh_home, monkeypatch):
    """ENFORCE=invariants where the checker yields ONLY a warning (no error,
    no crash): the gate stays OK -- invariant warnings remain advisory even when
    the dimension is enforced (the denylist exempts 'warning:' only)."""
    import hermes_cli.launch_completeness as lc

    class _Report:
        errors: list = []
        warnings = ["soft structural smell"]

    monkeypatch.setattr(lc, "check_contract_invariants", lambda _c: _Report())
    c = _contract()
    _approve_contract_board("probe", c)
    monkeypatch.setenv("HERMES_LAUNCH_COMPLETENESS_ENFORCE", "invariants")
    gate = kb.board_dispatch_gate("probe")
    assert gate["ok"] is True
    assert "launch_completeness_failed" not in _codes(gate)


def test_fix4a_invariants_error_blocks_under_enforcement(fresh_home, monkeypatch):
    """ENFORCE=invariants where the checker yields a hard ERROR: the gate blocks
    (the always-on structural rail)."""
    import hermes_cli.launch_completeness as lc

    class _Report:
        errors = ["R1: missing terminal state"]
        warnings: list = []

    monkeypatch.setattr(lc, "check_contract_invariants", lambda _c: _Report())
    c = _contract()
    _approve_contract_board("probe", c)
    monkeypatch.setenv("HERMES_LAUNCH_COMPLETENESS_ENFORCE", "invariants")
    gate = kb.board_dispatch_gate("probe")
    assert gate["ok"] is False
    assert "launch_completeness_failed" in _codes(gate)


def test_fix4b_assessor_crash_blocks_under_enforcement(fresh_home, monkeypatch):
    """ENFORCE=success_scoreability with assess_launch_completeness monkeypatched
    to RAISE: validate must FAIL CLOSED (readiness.ok=False) and the gate blocks
    with an [enforcement_unavailable] finding. FAILS before FIX 4(b) (the except
    set completeness ok=True and dropped enforcement silently)."""
    import hermes_cli.launch_completeness as lc

    def _boom(*_a, **_k):
        raise RuntimeError("assessor exploded")

    c = _contract()
    _approve_contract_board("probe", c)
    # Patch AFTER approval so the approval path (which also validates) is not
    # disturbed; the gate re-runs validate and hits the patched assessor.
    monkeypatch.setattr(lc, "assess_launch_completeness", _boom)
    monkeypatch.setenv("HERMES_LAUNCH_COMPLETENESS_ENFORCE", "success_scoreability")
    gate = kb.board_dispatch_gate("probe")
    assert gate["ok"] is False
    readiness = gate["readiness"]
    assert readiness["ok"] is False
    assert any("enforcement_unavailable" in e for e in readiness.get("errors", []))


def test_fix4b_default_off_assessor_crash_is_noop(fresh_home, monkeypatch):
    """DEFAULT-OFF (no enforced dimension) + assess_launch_completeness raising:
    the gate stays OK -- a crash in report-mode is a no-op (default-off preserved).
    """
    import hermes_cli.launch_completeness as lc

    def _boom(*_a, **_k):
        raise RuntimeError("assessor exploded")

    c = _contract()
    _approve_contract_board("probe", c)
    monkeypatch.setattr(lc, "assess_launch_completeness", _boom)
    # No HERMES_LAUNCH_COMPLETENESS_ENFORCE set -> empty enforced set.
    gate = kb.board_dispatch_gate("probe")
    assert gate["ok"] is True
    assert "launch_completeness_failed" not in _codes(gate)
    # Telemetry records it as unavailable but ok=True (no-op).
    comp = gate["readiness"]["completeness"]
    assert comp.get("ok") is True
    assert "unavailable" in comp


def test_fix7_completeness_blocking_findings_invariants_denylist():
    """Direct unit coverage of _completeness_blocking_findings for the
    'invariants' dimension: warning -> advisory; error/raised/unavailable ->
    blocking; empty enforced set -> nothing blocks."""
    f = kb._completeness_blocking_findings
    ed = frozenset(["invariants"])
    assert f({"dimensions": {"invariants": {"findings": ["warning: soft"]}}}, ed) == []
    assert f({"dimensions": {"invariants": {"findings": ["error: R1"]}}}, ed) == ["[invariants] error: R1"]
    raised = f({"dimensions": {"invariants": {"findings": ['raised: ValueError("x")']}}}, ed)
    assert raised == ['[invariants] raised: ValueError("x")']
    unavail = f(
        {"dimensions": {"invariants": {"findings": ["check_contract_invariants unavailable (engine import failed)"]}}},
        ed,
    )
    assert unavail == ["[invariants] check_contract_invariants unavailable (engine import failed)"]
    # Empty enforced set never blocks (default-off).
    assert f({"dimensions": {"invariants": {"findings": ["error: R1"]}}}, frozenset()) == []
    # Mixed: only the warning is exempt.
    mixed = f(
        {"dimensions": {"invariants": {"findings": ["warning: soft", "error: hard", "raised: boom"]}}},
        ed,
    )
    assert "[invariants] error: hard" in mixed
    assert "[invariants] raised: boom" in mixed
    assert all("warning: soft" not in m for m in mixed)
