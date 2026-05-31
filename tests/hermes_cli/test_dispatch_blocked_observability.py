"""G3: a launch-gated board must be OBSERVABLE, not a silent hang.

Before this change only ``board_metadata_invalid`` surfaced anything when the
dispatch gate blocked a claim; the transient board-level codes
(``launch_readiness_failed``, ``launch_review_missing``, ``board_not_active``)
fell through to a bare ``return None`` with no event and no audit trail. These
tests pin the new deduped ``dispatch_blocked`` board signal and that it never
strands a task (transient blocks resolve when the board is approved/activated).

This observability is the prerequisite for safely soaking the keystone
report->enforce gate flip (G1): newly-blocked tasks must leave a trace.
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

# Reuse the proven managed-board contract fixture from the sibling test module.
_crt_spec = importlib.util.spec_from_file_location(
    "_crt_helpers", pathlib.Path(__file__).with_name("test_kanban_contract_runtime.py")
)
_crt = importlib.util.module_from_spec(_crt_spec)
_crt_spec.loader.exec_module(_crt)
_contract = _crt._contract


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


def _dispatch_blocked_rows(conn, board):
    return conn.execute(
        "SELECT primitive_key, action FROM board_signals "
        "WHERE board = ? AND primitive_kind = 'dispatch_blocked' ORDER BY id",
        (board,),
    ).fetchall()


def _unapproved_managed_board(slug="serious"):
    """Create a managed board WITHOUT approving it -> board_dispatch_gate is closed."""
    kb.review_business_launch_contract(slug, contract=_contract(), create_if_missing=True)
    gate = kb.board_dispatch_gate(slug)
    assert not gate["ok"], "expected an unapproved managed board to be gate-blocked"
    return slug, gate


# --------------------------------------------------------------------------- #
# Helper: writes a deduped, code-sorted board signal and mutates no task
# --------------------------------------------------------------------------- #

def test_emit_helper_writes_sorted_and_dedupes(fresh_home):
    slug, _ = _unapproved_managed_board()
    gate = {
        "ok": False,
        "reason": "blocked",
        "blockers": [{"code": "launch_review_missing"}, {"code": "board_not_active"}],
    }
    with kb.connect(board=slug) as conn:
        kb._emit_dispatch_blocked_signal(conn, slug, gate)
        kb._emit_dispatch_blocked_signal(conn, slug, gate)  # identical -> deduped
        rows = _dispatch_blocked_rows(conn, slug)
    assert len(rows) == 1, "identical gate verdict must dedupe to a single ledger row"
    # codes are sorted + comma-joined
    assert rows[0]["primitive_key"] == "board_not_active,launch_review_missing"


def test_emit_helper_distinct_code_sets_each_recorded(fresh_home):
    slug, _ = _unapproved_managed_board()
    with kb.connect(board=slug) as conn:
        kb._emit_dispatch_blocked_signal(conn, slug, {"blockers": [{"code": "launch_review_missing"}]})
        kb._emit_dispatch_blocked_signal(conn, slug, {"blockers": [{"code": "launch_readiness_failed"}]})
        rows = _dispatch_blocked_rows(conn, slug)
    keys = sorted(r["primitive_key"] for r in rows)
    assert keys == ["launch_readiness_failed", "launch_review_missing"]


def test_emit_helper_no_codes_is_noop(fresh_home):
    slug, _ = _unapproved_managed_board()
    with kb.connect(board=slug) as conn:
        kb._emit_dispatch_blocked_signal(conn, slug, {"ok": False, "blockers": []})
        assert _dispatch_blocked_rows(conn, slug) == []


# --------------------------------------------------------------------------- #
# Integration: a real gate-blocked board surfaces the signal via recompute_ready
# --------------------------------------------------------------------------- #

def test_recompute_ready_on_blocked_board_emits_deduped_signal(fresh_home):
    slug, gate = _unapproved_managed_board()
    expected_codes = ",".join(
        sorted({b["code"] for b in gate["blockers"]})
    )
    with kb.connect(board=slug) as conn:
        promoted = kb.recompute_ready(conn)
        assert promoted == 0  # gate closed -> nothing promoted
        rows = _dispatch_blocked_rows(conn, slug)
        assert len(rows) == 1
        assert rows[0]["primitive_key"] == expected_codes
        # a stuck board does not spam the ledger
        kb.recompute_ready(conn)
        assert len(_dispatch_blocked_rows(conn, slug)) == 1
