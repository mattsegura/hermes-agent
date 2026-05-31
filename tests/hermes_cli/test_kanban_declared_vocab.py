"""Step 9 regression tests: DECLARED CLOSED VOCABULARIES on the safety paths.

These prove the three declared-vocabulary hardenings drive the REAL seams
(``reactive_tick`` / ``_close_timer_schedule`` / the launch invariant checker),
not the shape of the change, and that every new strict behavior is OPT-IN so an
undeclared/legacy contract is byte-identical to today (merge-gate Sec.2/3/4).

Each test is written so that reverting the feature makes it FAIL (mutation
check): the assertions are on the observable gate decision (fired / deferred /
stopped) and on the reward credited through the optimizer ledger, with the
opt-in and the legacy path asserted side-by-side.

The contract / approve / fresh_home fixtures are reused from
``test_kanban_reactive_runtime`` so these tests exercise the same real launch ->
compile -> tick pipeline (no monkeypatching of the functions under test).
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb

from tests.hermes_cli.test_kanban_reactive_runtime import (  # noqa: E402
    _approve,
    _contract,
    fresh_home,  # re-exported fixture
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _schedule_row(conn, board="serious"):
    return conn.execute(
        "SELECT * FROM reactive_timer_schedules WHERE board = ?", (board,)
    ).fetchone()


def _conversion_outcomes(conn, board="serious"):
    return conn.execute(
        "SELECT reward_kind, reward_value FROM board_signals "
        "WHERE board = ? AND primitive_kind = 'outcome' AND action LIKE '%loop_terminal%'",
        (board,),
    ).fetchall()


def _strict_contract(side_effect_class, *, strict):
    """A contract whose single loop carries ``side_effect_class`` and whose board
    side_effect_policy opts in/out of strict default-deny enforcement.

    The loop's timer (no inbound) so the timer-fire gate is the only path; the
    bogus class is intentionally NOT in the policy's forbidden/approval_required
    so the LEGACY behavior is the fall-through fire we are hardening."""
    contract = _contract(
        side_effect_class=side_effect_class,
        allowed_side_effects=[side_effect_class],
        event_loops=[
            {
                "key": "seller_follow_up",
                "type": "seller_follow_up",
                "entity": "conversation_thread",
                "triggers": [
                    {"kind": "timer", "detail": "nudge seller", "cadence_hours": 72},
                ],
                "terminal_states": ["won", "lost"],
                "side_effect_class": side_effect_class,
                "max_nudges": 5,
            }
        ],
        side_effect_policy={
            "allowed": ["none"],
            "forbidden": ["unapproved_external_write"],
            # NOTE: the bogus/undeclared class is deliberately absent here.
            **({"strict": True} if strict else {}),
        },
    )
    return contract


# ===========================================================================
# 9(a) side_effect_class DEFAULT-DENY at the reactive timer gate (opt-in)
# ===========================================================================


def test_9a_strict_board_does_not_fire_an_unrecognized_side_effect_class(fresh_home):
    """OPTED-IN board + a timer loop whose side_effect_class is a typo (not a
    member of SIDE_EFFECT_CLASSES) -> the gate does NOT fire it autonomously; it
    routes to the approval-deferral path (fail-CLOSED).

    Mutation check: without 9(a) the unrecognized class falls through both the
    forbidden and approval_required sets and FIRES -- this test would then see
    ``fired`` non-empty and ``deferred`` empty, so it fails.
    """
    _approve("serious", _strict_contract("extrnal_ireversible", strict=True))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        # Sanity: the bogus class really was persisted (not coalesced away).
        assert row["side_effect_class"] == "extrnal_ireversible"
        base = int(row["next_fire_at"])

        res = kb.reactive_tick(conn, now=base, board="serious")
        assert res["fired"] == [], "an unrecognized class must NOT fire under strict"
        assert res["deferred"] == [
            {"loop_key": "seller_follow_up", "reason": "side_effect_unrecognized_strict"}
        ]
        # The schedule stayed active (deferred, not closed) and spent NO nudge.
        after = _schedule_row(conn)
        assert after["active"] == 1
        assert after["nudges_used"] == 0


def test_9a_strict_board_fires_a_recognized_class_as_classified(fresh_home):
    """OPTED-IN board + a timer loop whose side_effect_class IS a recognized
    member (``external_reversible``, gated+approved) -> fires as classified.
    Strict mode only catches the unrecognized/near-miss case; a valid declared
    class flows through the normal gate."""
    contract = _strict_contract("external_reversible", strict=True)
    contract["approval_gates"] = [
        {"key": "owner_reversible_approval", "required_before": ["external_reversible"]}
    ]
    contract["side_effect_policy"]["approval_required"] = ["external_reversible"]
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        assert row["side_effect_class"] == "external_reversible"
        # Recognized + gated: it defers for APPROVAL (not for being unrecognized).
        base = int(row["next_fire_at"])
        res = kb.reactive_tick(conn, now=base, board="serious")
        assert res["deferred"] == [
            {"loop_key": "seller_follow_up", "reason": "approval_required"}
        ]
        # Grant the approval -> it now FIRES as classified.
        kb.record_contract_approval(
            conn, gate_key="owner_reversible_approval",
            entity_ref=row["task_id"], approved_by="owner", board="serious",
        )
        res2 = kb.reactive_tick(
            conn, now=base + int(row["cadence_seconds"]), board="serious"
        )
        assert res2["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}]


def test_9a_strict_board_fires_a_recognized_none_class(fresh_home):
    """OPTED-IN board + a benign ``none`` loop -> fires normally. A declared
    ``none`` persists as NULL (the established convention), and the coalesced
    ``none`` is a recognized member, so strict mode does NOT hold it."""
    _approve("serious", _strict_contract("none", strict=True))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        # _normalize_funnel_text collapses a declared "none" to NULL.
        assert row["side_effect_class"] is None
        base = int(row["next_fire_at"])
        res = kb.reactive_tick(conn, now=base, board="serious")
        assert res["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}]
        assert res["deferred"] == []


def test_9a_strict_board_fires_a_null_class_byte_identical(fresh_home):
    """OPTED-IN board + a schedule whose side_effect_class is NULL -> FIRES.

    A NULL class coalesces to the benign ``none`` (a recognized member), so an
    absent class is NOT treated as suspicious. This keeps every legitimate
    read-only loop firing under strict mode -- the strict gate only catches a
    NON-EMPTY near-miss class (see the typo test above)."""
    _approve("serious", _strict_contract("none", strict=True))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        base = int(row["next_fire_at"])
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE reactive_timer_schedules SET side_effect_class = NULL WHERE id = ?",
                (int(row["id"]),),
            )
        res = kb.reactive_tick(conn, now=base, board="serious")
        assert res["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}]
        assert res["deferred"] == []


def test_9a_legacy_board_fires_a_bogus_class_byte_identical(fresh_home):
    """NOT opted in (legacy, every board incl. ninaxfinds) + the SAME bogus class
    -> FIRES (byte-identical legacy fall-through).

    This is the default-off proof for 9(a): the only difference between this test
    and the strict one above is the absence of ``side_effect_policy.strict``."""
    _approve("serious", _strict_contract("extrnal_ireversible", strict=False))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        assert row["side_effect_class"] == "extrnal_ireversible"
        base = int(row["next_fire_at"])
        res = kb.reactive_tick(conn, now=base, board="serious")
        # Legacy behavior: an unrecognized class falls through and fires.
        assert res["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}]
        assert res["deferred"] == []


# ===========================================================================
# 9(b) declared win/loss/neutral terminal class + retire the 'won'-substring
#      reward shortcut
# ===========================================================================


def _declared_class_contract(terminal_states, terminal_classes):
    """A loop that OPTS IN to declared terminal classes (closed vocabulary)."""
    return _contract(
        event_loops=[
            {
                "key": "seller_follow_up",
                "type": "seller_follow_up",
                "entity": "conversation_thread",
                "triggers": [
                    {"kind": "timer", "detail": "nudge seller", "cadence_hours": 72},
                    {"kind": "inbound", "detail": "seller replies", "channel": "infobip"},
                ],
                "terminal_states": terminal_states,
                "terminal_classes": terminal_classes,
                "max_nudges": 2,
            }
        ],
        entities=[
            {
                "key": "conversation_thread",
                "type": "conversation",
                "states": ["open", "watching"] + list(terminal_states),
                "terminal_states": list(terminal_states),
            }
        ],
    )


def _resolve_and_close(conn, terminal_outcome, board="serious"):
    """Resolve the watched entity to ``terminal_outcome`` and tick so the timer
    gate closes the loop through _close_timer_schedule (the real reward seam)."""
    row = _schedule_row(conn, board)
    kb.resolve_reactive_entity(
        conn, row["entity_id"], terminal_outcome=terminal_outcome,
        state=terminal_outcome, actor="fixture",
    )
    kb.reactive_tick(conn, now=int(row["next_fire_at"]), board=board)
    return _conversion_outcomes(conn, board)


def _credited(outcomes):
    return any(o["reward_kind"] == "conversion" and o["reward_value"] == 1.0 for o in outcomes)


@pytest.mark.parametrize(
    "outcome,expected_conversion",
    [
        ("closed_won", True),    # the declared win terminal credits
        ("closed_lost", False),  # the declared loss terminal never credits
        ("owner_stopped", False),  # the declared neutral terminal never credits
        ("unwon", False),        # NOT declared -> no substring shortcut -> no credit
        ("wonky", False),        # NOT declared -> no substring shortcut -> no credit
        ("arbitrary", False),    # NOT declared -> no credit
    ],
)
def test_9b_declared_loop_credits_only_declared_win_terminal(
    fresh_home, outcome, expected_conversion
):
    """DECLARED loop: conversion credit comes ONLY from the declared ``win``
    class (exact match, closed vocabulary). 'unwon'/'wonky'/arbitrary strings do
    NOT credit even though they embed a win substring.

    Mutation check: revert 9(b) and the declared path disappears -> 'unwon' /
    'wonky' would hit the ``"won" in outcome`` shortcut and wrongly credit 1.0,
    failing the False rows here.
    """
    _approve(
        "serious",
        _declared_class_contract(
            terminal_states=["closed_won", "closed_lost", "owner_stopped"],
            terminal_classes={
                "closed_won": "win",
                "closed_lost": "loss",
                "owner_stopped": "neutral",
            },
        ),
    )
    with kb.connect(board="serious") as conn:
        outcomes = _resolve_and_close(conn, outcome)
        assert _credited(outcomes) is expected_conversion


@pytest.mark.parametrize(
    "outcome,expected_conversion",
    [
        ("closed_won", True),     # legacy: contains 'won', not a loss -> credits
        ("won", True),            # legacy: a bare 'won' still credits (no regression)
        ("closed_lost", False),   # legacy: no 'won' substring -> no credit (unchanged)
        ("won_but_lost", False),  # THE FIX: embeds 'won' but is a loss -> no credit
    ],
)
def test_9b_legacy_loop_corrected_substring_reward(
    fresh_home, outcome, expected_conversion
):
    """LEGACY loop (no declared classes): the corrected 'won'-substring reward.

    closed_won and a bare 'won' still credit (no legit-win regression);
    closed_lost still does not; and won_but_lost -- which embeds 'won' but is a
    loss -- NO LONGER credits (the G6 loss-denylist correction).

    Mutation check: revert the loss-aware routing and ``won_but_lost`` returns to
    crediting 1.0 (the original bug), failing its False row.
    """
    _approve(
        "serious",
        _contract(
            event_loops=[
                {
                    "key": "seller_follow_up",
                    "type": "seller_follow_up",
                    "entity": "conversation_thread",
                    "triggers": [
                        {"kind": "timer", "detail": "nudge seller", "cadence_hours": 72},
                        {"kind": "inbound", "detail": "seller replies", "channel": "infobip"},
                    ],
                    "terminal_states": ["won", "closed_won", "closed_lost", "won_but_lost"],
                    "max_nudges": 2,
                }
            ],
            entities=[
                {
                    "key": "conversation_thread",
                    "type": "conversation",
                    "states": ["open", "won", "closed_won", "closed_lost", "won_but_lost"],
                    "terminal_states": ["won", "closed_won", "closed_lost", "won_but_lost"],
                }
            ],
        ),
    )
    with kb.connect(board="serious") as conn:
        outcomes = _resolve_and_close(conn, outcome)
        assert _credited(outcomes) is expected_conversion


def test_9b_reward_dedupe_still_exactly_once(fresh_home):
    """The declared-class reward must still be attributed EXACTLY ONCE: a
    re-tick of an already-closed loop does not double-count the conversion."""
    _approve(
        "serious",
        _declared_class_contract(
            terminal_states=["closed_won", "closed_lost"],
            terminal_classes={"closed_won": "win", "closed_lost": "loss"},
        ),
    )
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        kb.resolve_reactive_entity(
            conn, row["entity_id"], terminal_outcome="closed_won",
            state="closed_won", actor="fixture",
        )
        kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        kb.reactive_tick(conn, now=int(row["next_fire_at"]) + 10_000, board="serious")
        conv = conn.execute(
            "SELECT COUNT(*) AS n FROM board_signals "
            "WHERE board = ? AND reward_kind = 'conversion' AND reward_value = 1.0",
            ("serious",),
        ).fetchone()
        assert int(conv["n"]) == 1, "the conversion reward must count exactly once"


# ===========================================================================
# 9(c) bounded approval-deferral counter (anti-zombie)
# ===========================================================================


def _deferring_contract(*, max_defers=None):
    """A timer loop that always DEFERS: its side effect needs an approval that is
    never granted, so each tick defers. With ``max_defers`` declared, the loop
    must terminate after the bound instead of deferring forever."""
    loop = {
        "key": "seller_follow_up",
        "type": "seller_follow_up",
        "entity": "conversation_thread",
        "triggers": [
            {"kind": "timer", "detail": "nudge seller", "cadence_hours": 72},
        ],
        "terminal_states": ["won", "lost"],
        "side_effect_class": "external_reversible",
        "max_nudges": 100,
    }
    if max_defers is not None:
        loop["max_defers"] = max_defers
    return _contract(
        side_effect_class="external_reversible",
        allowed_side_effects=["external_reversible"],
        event_loops=[loop],
        approval_gates=[
            {"key": "owner_reversible_approval", "required_before": ["external_reversible"]}
        ],
        side_effect_policy={
            "allowed": ["none", "internal"],
            "forbidden": ["external_irreversible", "financial"],
            "approval_required": ["external_reversible"],
        },
    )


def test_9c_bounded_defers_terminate_the_loop(fresh_home):
    """max_defers=2 -> the 3rd defer terminates the loop (no infinite defer).

    Mutation check: revert 9(c) and the loop defers forever -- the 3rd tick would
    report a defer, not a deferral_exhausted stop, and the schedule stays active.
    """
    _approve("serious", _deferring_contract(max_defers=2))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        cadence = int(row["cadence_seconds"])
        t = int(row["next_fire_at"])

        # Defer 1.
        r1 = kb.reactive_tick(conn, now=t, board="serious")
        assert r1["deferred"] == [
            {"loop_key": "seller_follow_up", "reason": "approval_required"}
        ]
        assert r1["stopped"] == []
        t += cadence
        # Defer 2.
        r2 = kb.reactive_tick(conn, now=t, board="serious")
        assert r2["deferred"] and r2["stopped"] == []
        assert _schedule_row(conn)["active"] == 1
        assert int(_schedule_row(conn)["defers_used"]) == 2
        t += cadence
        # 3rd attempt -> bound exceeded -> the loop TERMINATES.
        r3 = kb.reactive_tick(conn, now=t, board="serious")
        assert r3["deferred"] == []
        assert r3["stopped"] == [
            {"loop_key": "seller_follow_up", "reason": "deferral_exhausted"}
        ]
        closed = _schedule_row(conn)
        assert closed["active"] == 0
        assert closed["stop_reason"] == "deferral_exhausted"
        assert closed["nudges_used"] == 0, "termination must not spend a nudge"

        # No zombie: further ticks do nothing.
        t += cadence
        r4 = kb.reactive_tick(conn, now=t, board="serious")
        assert r4["deferred"] == [] and r4["stopped"] == [] and r4["fired"] == []


def test_9c_unset_max_defers_is_unbounded_byte_identical(fresh_home):
    """max_defers unset -> unbounded deferral (byte-identical to today).

    The loop defers on every tick and NEVER terminates from a defer bound, just
    like the legacy behavior. This is the default-off proof for 9(c)."""
    _approve("serious", _deferring_contract(max_defers=None))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        assert row["max_defers"] is None, "legacy loop must persist NULL max_defers"
        cadence = int(row["cadence_seconds"])
        t = int(row["next_fire_at"])
        for _ in range(6):
            res = kb.reactive_tick(conn, now=t, board="serious")
            assert res["deferred"] == [
                {"loop_key": "seller_follow_up", "reason": "approval_required"}
            ]
            assert res["stopped"] == []
            t += cadence
        # Still active after many defers -- unbounded, exactly as before.
        assert _schedule_row(conn)["active"] == 1


# ===========================================================================
# Cross-cutting: the LIVE ninaxfinds-growth contract is byte-identical by
# default (no declared strict / terminal classes / max_defers).
# ===========================================================================


def test_live_ninaxfinds_contract_has_no_optin_declarations():
    """READ-ONLY guard: the live ninaxfinds-growth business_contract declares
    none of the Step 9 opt-ins, so every new strict behavior is inert for it.

    This pins the default-byte-identical claim to the ACTUAL live contract: if a
    future edit (or a bad default) flipped any of these on, this test fails.
    """
    board_json = (
        Path.home() / ".hermes" / "kanban" / "boards" / "ninaxfinds-growth" / "board.json"
    )
    if not board_json.exists():
        pytest.skip("live ninaxfinds-growth board not present in this environment")
    data = json.loads(board_json.read_text())
    bc = data.get("business_contract", {})

    def _walk_find(obj, key):
        found = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == key:
                    found.append(v)
                found += _walk_find(v, key)
        elif isinstance(obj, list):
            for v in obj:
                found += _walk_find(v, key)
        return found

    # No board declares side_effect_policy.strict, declared terminal classes, or
    # a defer bound -> all three Step 9 strict paths are OFF for this board.
    policies = _walk_find(bc, "side_effect_policy")
    for p in policies:
        if isinstance(p, dict):
            assert not p.get("strict"), "live contract must not opt into strict side-effects"
    assert _walk_find(bc, "terminal_classes") == [], "no declared terminal classes"
    assert _walk_find(bc, "max_defers") == [], "no declared defer bound"
    # And it declares no event_loops at all, so there is nothing to fire/defer.
    assert not bc.get("event_loops")
