"""Cross-process tick safety: two processes ticking the same due timer must
fire it EXACTLY once (no double-spent nudge, no duplicate nudge signal).

``_fire_timer_schedule`` advances ``nudges_used``/``next_fire_at`` with a plain
``WHERE id = ?`` UPDATE (no CAS on the prior cadence). That is safe because
``kanban_db.connect()`` takes an exclusive flock held for the connection's
lifetime, so two processes can never hold a board connection at the same time --
their ticks serialize: the first fires and pushes ``next_fire_at`` into the
future, the second sees the schedule as not-due and does nothing. This test
pins that guarantee with real subprocesses (fcntl flock is per-process, so only
a real second process exercises the cross-process lock).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_FIXTURE = _REPO / "tests" / "fixtures" / "launch_intake" / "insurance_recruiting.contract.json"

_TICK_WORKER = textwrap.dedent(
    """
    import os, sys
    from hermes_cli import kanban_db as kb
    board, now = sys.argv[1], int(sys.argv[2])
    with kb.connect(board=board) as conn:
        r = kb.reactive_tick(conn, now=now, board=board)
    print("FIRED", len(r["fired"]))
    """
)


@pytest.mark.slow  # launches real worker subprocesses to exercise cross-process flock
@pytest.mark.skipif(os.name == "nt", reason="flock-based serialization is POSIX-only")
def test_two_processes_ticking_one_timer_fire_exactly_once(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT"):
        env.pop(var, None)

    # Launch a board with a live timer schedule, in-process (parent).
    # kanban_db reads HERMES_HOME at call time (not import time), so set the env
    # and use the already-imported module -- do NOT delete it from sys.modules,
    # which would create a module-identity split-brain for any sibling test that
    # imported hermes_cli.main earlier.
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import kanban_db as kb

    contract = json.loads(_FIXTURE.read_text())
    board = "insure"
    kb.review_business_launch_contract(board, contract=contract, create_if_missing=True)
    token = kb.issue_board_launch_approval_token(
        board, contract=contract, approved_by="o",
        approval_evidence={"s": "x"}, owner_authority_confirmed=True,
    )["token"]
    kb.review_business_launch_contract(
        board, contract=contract, approve=True, approval_token=token, author="o",
    )

    with kb.connect(board=board) as conn:
        sched = conn.execute(
            "SELECT id, next_fire_at, nudges_used FROM reactive_timer_schedules "
            "WHERE active = 1 LIMIT 1"
        ).fetchone()
        assert sched is not None, "expected a live timer schedule after launch"
        fire_at = int(sched["next_fire_at"])
        assert int(sched["nudges_used"]) == 0

    now = fire_at + 1  # strictly due for both workers

    # Spawn TWO real processes that both try to tick the same due timer.
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _TICK_WORKER, board, str(now)],
            env=env, cwd=str(_REPO),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=60) for p in procs]
    for (out, err), p in zip(outs, procs):
        assert p.returncode == 0, f"tick worker crashed: {err}\n{out}"

    total_fired = sum(int(o.split()[1]) for o, _ in outs if o.startswith("FIRED"))

    # Exactly one of the two serialized ticks fired the timer.
    with kb.connect(board=board) as conn:
        nudges = conn.execute(
            "SELECT nudges_used FROM reactive_timer_schedules WHERE id = ?",
            (sched["id"],),
        ).fetchone()["nudges_used"]
        nudge_signals = conn.execute(
            "SELECT COUNT(*) FROM board_signals WHERE primitive_kind = 'event_loop' "
            "AND json_extract(action, '$.kind') = 'nudge'"
        ).fetchone()[0]

    assert total_fired == 1, f"expected exactly one process to fire, got {total_fired}"
    assert nudges == 1, f"nudge double-spent: nudges_used={nudges}"
    assert nudge_signals == 1, f"duplicate nudge signal: {nudge_signals}"
