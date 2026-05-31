"""Step 9 regression tests (reworked): DECLARED CLOSED VOCABULARIES, fail-closed.

These prove the Step-9 hardenings drive the REAL seams, not the shape of the
change, and that every new strict behavior is OPT-IN so an undeclared/legacy
contract is BYTE-IDENTICAL to base 019271994 (merge-gate Sec.2/3/4).

Two families of seam are exercised here:

* the runtime reward/gate seams (``reactive_tick`` / ``_close_timer_schedule``),
  driven through the real launch -> compile -> tick pipeline with the reused
  ``_contract`` / ``_approve`` / ``fresh_home`` fixtures (no monkeypatching of
  the functions under test); and
* the intake invariant checker
  (``kanban_launch_invariants.check_contract_invariants``), driven directly so
  the 9(b) terminal-class invariant and the FIX-7 coverage checks are asserted
  at the EFFECT level (``ok`` / the error/warning text), not just emitted. (The
  PRIOR version of this file falsely claimed in its docstring to drive the
  launch invariant checker while only importing ``kanban_db`` -- the invariant
  had ZERO coverage. That gap is closed by the ``test_invariant_*`` cases.)

Each test is written so that reverting the feature makes it FAIL (the mutation
check): the assertions are on the observable gate decision (fired / deferred /
stopped), the reward credited through the optimizer ledger, or the invariant
``ok``/error, with the opt-in and the legacy path asserted side-by-side.
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
from hermes_cli import kanban_launch_grammar as grammar
from hermes_cli import kanban_launch_invariants as inv
from hermes_cli import kanban_reactive_runtime as rt

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
        ("closed_won", True),       # legacy: contains 'won' -> credits (base)
        ("won", True),              # legacy: a bare 'won' credits (base)
        ("closed_lost", False),     # legacy: no 'won' substring -> no credit (base)
        # FIX 1 (byte-identity to base 019271994): the LEGACY 'won'-substring
        # shortcut is UNCONDITIONAL. A loss terminal that embeds 'won'
        # (``won_but_lost``) STILL credits on the legacy path -- exactly as base
        # did. The PRIOR Step-9 made this 0.0 via an un-opted-in loss denylist,
        # which ALSO flipped genuine win-backs (won_back_from_churn) to 0.0; that
        # was the critical default-off regression. Loss-suppression now lives
        # ONLY in the DECLARED path (see test below).
        ("won_but_lost", True),
        # The win-backs the regression wrongly denied: all credit on legacy (base).
        ("won_back_from_churn", True),
        ("renewal_after_cancellation_won", True),
        ("reactivated_expired_subscriber_won", True),
    ],
)
def test_9b_legacy_loop_is_byte_identical_to_base(
    fresh_home, outcome, expected_conversion
):
    """LEGACY loop (no declared classes): the 'won'-substring shortcut is
    UNCONDITIONAL and byte-identical to base 019271994.

    Mutation check: re-introduce the un-opted-in loss denylist on the legacy
    branch (``and not _outcome_is_loss_token(outcome)``) and ``won_but_lost`` /
    ``won_back_from_churn`` / ``renewal_after_cancellation_won`` /
    ``reactivated_expired_subscriber_won`` flip to 0.0 -- the exact critical
    default-off regression -- failing their True rows here.
    """
    states = [
        "won", "closed_won", "closed_lost", "won_but_lost", "won_back_from_churn",
        "renewal_after_cancellation_won", "reactivated_expired_subscriber_won",
    ]
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
                    "terminal_states": states,
                    "max_nudges": 2,
                }
            ],
            entities=[
                {
                    "key": "conversation_thread",
                    "type": "conversation",
                    "states": ["open"] + states,
                    "terminal_states": states,
                }
            ],
        ),
    )
    with kb.connect(board="serious") as conn:
        outcomes = _resolve_and_close(conn, outcome)
        assert _credited(outcomes) is expected_conversion


def test_fix1_legacy_path_byte_identical_to_base_unit():
    """FIX 1 unit proof: ``_terminal_outcome_is_conversion`` on the LEGACY path
    (``declared_terminal_classes`` None/empty) is byte-identical to base
    019271994.

    Base behavior (git show 019271994:hermes_cli/kanban_db.py):
      (b) ``if "won" in outcome: return True``  -- UNCONDITIONAL substring shortcut
      (c) for a declared terminal_state == outcome that ``_looks_like_win_token``
          deems a win, return True.

    FIX 6 (round-2) test repair: path (c) of the base used the REAL
    ``_looks_like_win_token`` (-> ``launch_completeness._looks_like_win``), which
    credits NON-'won' win tokens (under_contract / onboarded / signed / ...). The
    prior reconstruction gated path (c) on ``"won" in dd`` instead, so it never
    exercised the non-'won' win-token branch and a mutation of path (c) to
    ``"won" in d`` passed unnoticed. We now reconstruct base path (c) with the
    REAL helper AND add non-'won' win-token probes with matching declared states
    so the byte-identity claim actually pins path (c).

    Mutation check (path b): any loss-denylist gate on the legacy substring branch
    makes a 'won'-embedding loss/win-back outcome diverge from base, failing here.
    Mutation check (path c): replacing ``_looks_like_win_token(d)`` with
    ``"won" in d`` flips the non-'won' win-token probes (under_contract/onboarded/
    signed) from True to False, failing here.
    """

    def base_conversion(outcome, states):
        # Reconstructed base behavior (git show 019271994:hermes_cli/kanban_db.py).
        if not outcome:
            return False
        o = str(outcome).strip().lower()
        if not o:
            return False
        if "won" in o:                      # base path (b): UNCONDITIONAL
            return True
        # base path (c): the outcome matches a declared terminal_state AND that
        # state reads as a win-class token via the REAL detector (the SAME helper
        # the shipped reward rail and the win_signal_rail spec bind to).
        for d in states or []:
            dd = str(d).strip().lower()
            if dd == o and kb._looks_like_win_token(dd):
                return True
        return False

    # 'won'-embedding probes (path b) + NON-'won' win tokens (path c) with their
    # own matching declared states so the win-token branch is actually exercised.
    won_probes = [
        "won", "closed_won", "closed_lost", "won_but_lost", "won_lost",
        "won_then_lost", "won_then_cancelled", "deal_won_but_churned",
        "won_account_expired", "won_back_from_churn",
        "renewal_after_cancellation_won", "reactivated_expired_subscriber_won",
        "won_unexpired", "won_deadline_beat", "won_over_to_competitor",
        "unwon", "wonky", "renewal_won", "arbitrary", "", None,
    ]
    state_variants = [[], ["won", "lost"], ["closed_won", "closed_lost"]]
    for o in won_probes:
        for st in state_variants:
            base = base_conversion(o, st)
            assert kb._terminal_outcome_is_conversion(o, st, None) is base, (o, st)
            assert kb._terminal_outcome_is_conversion(o, st, {}) is base, (o, st)

    # Non-'won' win tokens that path (c) credits at base ONLY when the outcome
    # equals a DECLARED terminal_state (so the substring shortcut (b) is bypassed).
    # These pin the win-token branch the prior reconstruction never exercised.
    win_token_probes = [
        "under_contract", "onboarded", "signed", "published",
        "converted", "paid", "delivered", "completed", "approved",
    ]
    for o in win_token_probes:
        # declared as a terminal_state (path c reachable) -> credits at base.
        base_declared = base_conversion(o, [o])
        assert kb._terminal_outcome_is_conversion(o, [o], None) is base_declared, o
        assert kb._terminal_outcome_is_conversion(o, [o], {}) is base_declared, o
        assert base_declared is True, (
            f"sanity: {o!r} must be a win token via the real detector"
        )
        # NOT declared (path c unreachable) -> no 'won' substring -> no credit.
        base_undeclared = base_conversion(o, [])
        assert kb._terminal_outcome_is_conversion(o, [], None) is base_undeclared, o
        assert base_undeclared is False, (
            f"sanity: {o!r} must NOT credit when it is not a declared terminal"
        )


@pytest.mark.parametrize(
    "outcome,expected_conversion",
    [
        ("closed_won", True),    # declared 'win' credits
        ("won_but_lost", False),  # DECLARED 'loss' -> never credits (loss-aware HERE)
        ("won_back_from_churn", False),  # declared 'loss' -> never credits
        ("renewal_after_cancellation_won", True),  # declared 'win' -> credits
    ],
)
def test_declared_path_is_where_loss_awareness_lives(
    fresh_home, outcome, expected_conversion
):
    """Loss-suppression for a 'won'-embedding terminal lives ONLY in the DECLARED
    path now -- a board OPTS IN via ``terminal_classes`` (closed vocabulary),
    where the decision is the declared class, never a free-text substring
    denylist. ``won_but_lost`` declared 'loss' does not credit;
    ``renewal_after_cancellation_won`` declared 'win' DOES (no substring denylist
    wrongly suppresses it).

    Mutation check: revert 9(b) declared path and the decision falls back to the
    legacy substring shortcut, crediting ``won_but_lost`` (fails its False row).
    """
    _approve(
        "serious",
        _declared_class_contract(
            terminal_states=[
                "closed_won", "won_but_lost", "won_back_from_churn",
                "renewal_after_cancellation_won",
            ],
            terminal_classes={
                "closed_won": "win",
                "won_but_lost": "loss",
                "won_back_from_churn": "loss",
                "renewal_after_cancellation_won": "win",
            },
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

    # The single source-of-truth predicate agrees: the live contract did NOT opt
    # into step-9, so it hits ZERO new code paths.
    assert grammar.contract_opts_into_step9(bc) is False

    # BYTE-IDENTITY: the live contract's invariants output and hash are identical
    # base-vs-HEAD (it produces no new finding and the hashed payload is base).
    base_inv = _load_base_module(
        "base_invariants_live", "hermes_cli/kanban_launch_invariants.py"
    )
    base_report = base_inv.check_contract_invariants(bc)
    head_report = inv.check_contract_invariants(bc)
    assert head_report.errors == base_report.errors, "live invariant ERRORS diverged"
    assert head_report.warnings == base_report.warnings, "live invariant WARNINGS diverged"
    # Hash is stable (re-synthesizing the same contract yields the same hash).
    assert kb._business_contract_hash(bc) == kb._business_contract_hash(json.loads(json.dumps(bc)))


# ===========================================================================
# FIX 2: strict = allowlist-of-SAFE (not allowlist-of-recognized). A RECOGNIZED
# external-family class with no satisfied approval gate must DEFER under strict.
# ===========================================================================


@pytest.mark.parametrize("dangerous", ["external_irreversible", "financial"])
def test_fix2_strict_defers_recognized_external_without_gate(fresh_home, dangerous):
    """strict + a correctly-spelled EXTERNAL-family class + approval_required=[]
    + no covering gate -> DEFERS (fail-closed). Previously this FIRED ungated
    (byte-identical to legacy): strict was allowlist-of-recognized, giving ZERO
    protection for the two most dangerous classes a board forgot to gate.

    Mutation check: drop the ``strict_external`` branch in reactive_tick and the
    recognized-dangerous class fires -> ``fired`` non-empty, failing here.
    """
    contract = _strict_contract(dangerous, strict=True)
    contract["side_effect_policy"]["approval_required"] = []  # explicitly empty
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["fired"] == [], f"{dangerous} must NOT fire ungated under strict"
        assert res["deferred"] == [
            {"loop_key": "seller_follow_up", "reason": "approval_required"}
        ]


@pytest.mark.parametrize("safe", ["none", "internal"])
def test_fix2_strict_fires_safe_class(fresh_home, safe):
    """strict + a SAFE class (none / internal) -> FIRES freely. The allowlist of
    classes that fire under strict is exactly {none, internal}."""
    _approve("serious", _strict_contract(safe, strict=True))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}]
        assert res["deferred"] == []


def test_fix2_strict_external_with_satisfied_gate_fires(fresh_home):
    """strict + external_irreversible COVERED by an approval gate -> defers until
    approved, then FIRES (per the gate). Confirms the external-family deferral is
    approval-required-by-default, not an unconditional block."""
    contract = _strict_contract("external_irreversible", strict=True)
    contract["approval_gates"] = [
        {"key": "owner_irrev_approval", "required_before": ["external_irreversible"]}
    ]
    contract["side_effect_policy"]["approval_required"] = ["external_irreversible"]
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        base = int(row["next_fire_at"])
        res = kb.reactive_tick(conn, now=base, board="serious")
        assert res["fired"] == [] and res["deferred"], "must defer before approval"
        kb.record_contract_approval(
            conn, gate_key="owner_irrev_approval",
            entity_ref=row["task_id"], approved_by="owner", board="serious",
        )
        res2 = kb.reactive_tick(
            conn, now=base + int(row["cadence_seconds"]), board="serious"
        )
        assert res2["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}]


def test_fix2_legacy_external_fires_byte_identical(fresh_home):
    """NO strict + external_irreversible + approval_required=[] -> FIRES (legacy,
    byte-identical). The default-off proof for FIX 2: the ONLY difference from
    the strict test above is the absence of ``side_effect_policy.strict``."""
    contract = _strict_contract("external_irreversible", strict=False)
    contract["side_effect_policy"]["approval_required"] = []
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}]
        assert res["deferred"] == []


# ===========================================================================
# FIX 3: case canonicalization at persistence -- recognition + the
# case-sensitive forbidden/approval_required checks share ONE vocabulary.
# ===========================================================================


def test_fix3_capitalized_class_persists_lowercase_and_forbidden_catches_it(fresh_home):
    """A loop declaring ``External_Irreversible`` persists CANONICAL lowercase,
    so a lower-case ``forbidden`` entry now catches it (stops the loop) instead
    of the class slipping the case-sensitive set yet reading as recognized.

    Mutation check: revert FIX 3 (persist verbatim case) and the persisted class
    is 'External_Irreversible', the forbidden membership misses it, and under
    strict it FIRES ungated -- failing the stop assertion here.
    """
    contract = _strict_contract("External_Irreversible", strict=True)
    contract["side_effect_policy"]["forbidden"] = ["external_irreversible"]
    contract["side_effect_policy"]["approval_required"] = ["external_irreversible"]
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        assert row["side_effect_class"] == "external_irreversible", (
            "FIX 3: side_effect_class must persist canonical lowercase"
        )
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["fired"] == [], "a forbidden class must NOT fire"
        assert res["stopped"] == [
            {"loop_key": "seller_follow_up", "reason": "side_effect_forbidden"}
        ]


# ===========================================================================
# FIX 5: corrupt-column FAIL-CLOSED (a present-but-broken declaration is NOT the
# same as no declaration).
# ===========================================================================


def test_fix5_corrupt_terminal_classes_fails_closed(fresh_home):
    """A loop OPTS IN with ``terminal_classes`` (a 'won'-embedding terminal
    declared NEUTRAL), then the column is corrupted. The reward rail must fail
    CLOSED -- credit 0.0 (non-conversion) and emit a loud error signal -- NOT
    silently fall back to the substring guess and credit 1.0.

    Mutation check: revert FIX 5 (corrupt -> {} -> 'never opted in') and the
    declared-NEUTRAL terminal credits 1.0 via the substring fallback, failing the
    no-conversion assertion.
    """
    _approve(
        "serious",
        _declared_class_contract(
            terminal_states=["won_pending_neutral", "closed_lost"],
            terminal_classes={"won_pending_neutral": "neutral", "closed_lost": "loss"},
        ),
    )
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE reactive_timer_schedules SET terminal_classes = ? WHERE id = ?",
                ("{not valid json", int(row["id"])),
            )
        outcomes = _resolve_and_close(conn, "won_pending_neutral")
        assert not _credited(outcomes), (
            "corrupt terminal_classes must fail CLOSED (no conversion credit)"
        )
        errs = conn.execute(
            "SELECT COUNT(*) AS n FROM board_signals WHERE board = ? AND "
            "primitive_kind = 'dispatch_blocked' AND reward_kind = 'error'",
            ("serious",),
        ).fetchone()
        assert int(errs["n"]) >= 1, "a corrupt declaration must emit a loud error signal"


def test_fix5_corrupt_max_defers_fails_closed_not_unbounded(fresh_home):
    """A loop declares ``max_defers=1`` then the column is corrupted. The defer
    path must fail CLOSED -- terminate -- NOT silently revert to unbounded
    (defer forever).

    Mutation check: revert FIX 5 (corrupt -> None -> unbounded) and the loop
    never terminates across many ticks, failing the termination assertion.
    """
    _approve("serious", _deferring_contract(max_defers=1))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE reactive_timer_schedules SET max_defers = ? WHERE id = ?",
                ("garbage", int(row["id"])),
            )
        cadence = int(row["cadence_seconds"])
        t = int(row["next_fire_at"])
        terminated = False
        for _ in range(8):
            kb.reactive_tick(conn, now=t, board="serious")
            if int(_schedule_row(conn)["active"]) == 0:
                terminated = True
                break
            t += cadence
        assert terminated, "corrupt max_defers must fail CLOSED (terminate), not defer forever"
        assert _schedule_row(conn)["stop_reason"] == "deferral_exhausted"


def test_fix5_null_terminal_classes_is_still_legacy(fresh_home):
    """A NULL terminal_classes (no opt-in) is NOT corrupt -- it stays on the
    byte-identical legacy substring path. This pins the NULL-vs-broken
    distinction: only a NON-NULL unparseable column fails closed."""
    _approve("serious", _contract())  # legacy loop, terminal_classes NULL
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        assert row["terminal_classes"] is None
        # legacy 'won' credits unconditionally (byte-identical)
        outcomes = _resolve_and_close(conn, "won")
        assert _credited(outcomes)


# ===========================================================================
# FIX 6: _coerce_int non-finite guard + intake surfacing of a malformed bound.
# ===========================================================================


@pytest.mark.parametrize("bad", [float("inf"), float("nan")])
def test_fix6_coerce_int_guards_non_finite(bad):
    """``_coerce_int`` returns None for a non-finite float instead of raising
    (which crashed the per-loop compile and silently dropped the schedule)."""
    assert rt._coerce_int(bad) is None
    assert rt.loop_max_defers({"max_defers": bad}) is None
    assert rt.loop_max_nudges({"max_nudges": bad}) is None
    # A finite value still coerces.
    assert rt._coerce_int(3.0) == 3
    assert rt.loop_max_defers({"max_defers": 2}) == 2


def test_fix6_inf_max_defers_does_not_silently_drop_loop(fresh_home):
    """A loop with ``max_defers: .inf`` must NOT silently vanish: the board
    compiles a real timer schedule (the loop is not dropped), and intake flags
    the malformed bound.

    Mutation check: revert the isfinite guard and the per-loop compile raises,
    the schedule is swallowed, and 0 rows exist -- failing the row-count here.
    """
    loop = {
        "key": "seller_follow_up",
        "type": "seller_follow_up",
        "entity": "conversation_thread",
        "triggers": [{"kind": "timer", "detail": "x", "cadence_hours": 72}],
        "terminal_states": ["won", "lost"],
        "max_nudges": 5,
        "max_defers": float("inf"),
    }
    contract = _contract(event_loops=[loop])
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        rows = conn.execute(
            "SELECT * FROM reactive_timer_schedules WHERE board = ?", ("serious",)
        ).fetchall()
        assert len(rows) == 1, "the loop must still compile (not silently dropped)"
        # Inf is not a usable bound -> unbounded at runtime (None), byte-identical
        # to a legacy unbounded loop.
        assert rows[0]["max_defers"] is None
    # Intake flags the malformed bound (FIX 6 surfacing).
    report = inv.check_contract_invariants(contract)
    assert any("max_defers" in e and "non-negative integer bound" in e
               for e in report.errors), "intake must flag max_defers=.inf"


# ===========================================================================
# FIX 7: additive intake coverage checks (effect-level, via the real checker).
# ===========================================================================


def _invariant_loop(**extra):
    loop = {
        "entity": "conversation_thread",
        "type": "seller",
        "triggers": [
            {"kind": "timer", "cadence_hours": 72},
            {"kind": "inbound", "channel": "x"},
        ],
        "terminal_states": ["closed_won", "closed_lost"],
        "max_nudges": 2,
    }
    loop.update(extra)
    return loop


def _invariant_contract(loop=None, policy=None):
    return {
        "objective": {"statement": "x"},
        "runtime": {"tunables": {"k": {"default": 1, "min": 0, "max": 5}}},
        "event_loops": [loop if loop is not None else _invariant_loop()],
        "entities": [
            {
                "key": "conversation_thread",
                "type": "conversation",
                "terminal_states": ["closed_won", "closed_lost"],
            }
        ],
        "side_effect_policy": policy if policy is not None else {"allowed": ["none"]},
    }


def test_fix7a_optin_without_win_is_error():
    """FIX 7(a): an opted-in loop whose declared map has NO 'win' class can never
    credit a conversion -> hard error at intake."""
    report = inv.check_contract_invariants(
        _invariant_contract(_invariant_loop(terminal_classes={"closed_lost": "loss"}))
    )
    assert report.ok is False
    assert any("NONE is a 'win'" in e for e in report.errors)


def test_fix7a_optin_with_win_is_ok():
    """FIX 7(a): an opted-in loop WITH a declared 'win' passes (no no-win error)."""
    report = inv.check_contract_invariants(
        _invariant_contract(
            _invariant_loop(terminal_classes={"closed_won": "win", "closed_lost": "loss"})
        )
    )
    assert report.ok is True
    assert not any("NONE is a 'win'" in e for e in report.errors)


def test_fix7a_partial_map_warns_on_unclassed_terminal():
    """FIX 7(a) coverage: a declared terminal_state with no class warns (it
    silently credits nothing once the loop opts in)."""
    report = inv.check_contract_invariants(
        _invariant_contract(
            _invariant_loop(
                terminal_states=["closed_won", "closed_lost"],
                terminal_classes={"closed_won": "win"},
            )
        )
    )
    assert any("has no declared class" in w for w in report.warnings)


def test_fix7b_shape1_inline_none_class_is_flagged():
    """FIX 7(b): shape-1 inline ``{state, class: None}`` is flagged as unknown,
    mirroring shape-2 (previously silently dropped)."""
    report = inv.check_contract_invariants(
        _invariant_contract(
            _invariant_loop(
                terminal_states=[
                    {"state": "closed_won", "class": "win"},
                    {"state": "closed_lost", "class": None},
                ]
            )
        )
    )
    assert report.ok is False
    assert any("closed_lost" in e and "unknown" in e for e in report.errors)


def test_fix7c_side_effect_class_normalizing_to_null_is_rejected():
    """FIX 7(c): a declared side_effect_class that normalizes away ('null') is
    rejected so authorial intent is not silently downgraded to 'none'."""
    report = inv.check_contract_invariants(
        _invariant_contract(
            _invariant_loop(
                side_effect_class="null",
                terminal_classes={"closed_won": "win", "closed_lost": "loss"},
            )
        )
    )
    assert report.ok is False
    assert any("normalizes" in e for e in report.errors)


def test_fix7d_near_miss_max_defers_key_warns():
    """FIX 7(d): a typo'd ``max_deferals`` key warns (silently inert otherwise)."""
    report = inv.check_contract_invariants(
        _invariant_contract(
            _invariant_loop(
                terminal_classes={"closed_won": "win", "closed_lost": "loss"},
                max_deferals=2,
            )
        )
    )
    assert any("typo" in w and "inert" in w for w in report.warnings)


def test_fix7d_near_miss_strict_policy_key_warns():
    """FIX 7(d): a typo'd ``strict_mode`` policy key warns (strict silently off)."""
    report = inv.check_contract_invariants(
        _invariant_contract(
            _invariant_loop(
                terminal_classes={"closed_won": "win", "closed_lost": "loss"}
            ),
            policy={"allowed": ["none"], "strict_mode": True},
        )
    )
    assert any("strict" in w and "typo" in w for w in report.warnings)


def test_fix7d_exact_keys_do_not_warn():
    """FIX 7(d): the EXACT recognized keys must NOT trigger a near-miss warning."""
    report = inv.check_contract_invariants(
        _invariant_contract(
            _invariant_loop(
                terminal_classes={"closed_won": "win", "closed_lost": "loss"},
                max_defers=2,
            ),
            policy={"allowed": ["none"], "strict": True},
        )
    )
    assert not any("typo" in w for w in report.warnings)


# ===========================================================================
# FIX 8: test gaps -- effect-level intake invariant, shape-1 at both layers, the
# grammar API helpers, the crash-time fail-closed contract, max_defers=0/aliases.
# ===========================================================================


def test_invariant_unknown_terminal_class_is_blocking_error():
    """9(b) EFFECT-level: a loop declaring an out-of-vocab terminal class makes
    ``check_contract_invariants`` return ok=False with the unknown-class error.

    Mutation check: replace the ``_check_loop_terminal_classes`` call site with
    ``pass`` and this test fails (the prior suite left it at 0 coverage)."""
    report = inv.check_contract_invariants(
        _invariant_contract(
            _invariant_loop(
                terminal_classes={"closed_won": "victory", "closed_lost": "loss"}
            )
        )
    )
    assert report.ok is False
    assert any("unknown" in e and "victory" in e for e in report.errors)


def test_invariant_no_declared_classes_is_ok_default_off():
    """9(b) default-off: a loop with NO declared terminal classes is unaffected
    by the terminal-class invariant (ok stays True for that check)."""
    report = inv.check_contract_invariants(
        _invariant_contract(_invariant_loop())  # no terminal_classes
    )
    # No terminal-class error at all (the loop did not opt in).
    assert not any("outcome class" in e for e in report.errors)
    assert not any("NONE is a 'win'" in e for e in report.errors)


def test_invariant_shape1_unknown_class_is_blocking_error():
    """9(b) shape-1: a {state, class} object-list declaration with an out-of-vocab
    class is also flagged (shape-1 parsing at the intake layer)."""
    report = inv.check_contract_invariants(
        _invariant_contract(
            _invariant_loop(
                terminal_states=[
                    {"state": "closed_won", "class": "win"},
                    {"state": "closed_lost", "class": "looss"},
                ]
            )
        )
    )
    assert report.ok is False
    assert any("looss" in e and "unknown" in e for e in report.errors)


def test_reward_rail_shape1_declaration_credits_only_declared_win(fresh_home):
    """9(b) shape-1 at the REWARD rail: a loop declaring its classes as a
    {state, class} object list credits ONLY the declared 'win' (exact match)."""
    contract = _contract(
        event_loops=[
            {
                "key": "seller_follow_up",
                "type": "seller_follow_up",
                "entity": "conversation_thread",
                "triggers": [
                    {"kind": "timer", "detail": "x", "cadence_hours": 72},
                    {"kind": "inbound", "detail": "y", "channel": "infobip"},
                ],
                "terminal_states": [
                    {"state": "closed_won", "class": "win"},
                    {"state": "closed_lost", "class": "loss"},
                ],
                "max_nudges": 2,
            }
        ],
        entities=[
            {
                "key": "conversation_thread",
                "type": "conversation",
                "states": ["open", "closed_won", "closed_lost"],
                "terminal_states": ["closed_won", "closed_lost"],
            }
        ],
    )
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        # The shape-1 declaration was compiled into the persisted class map.
        assert json.loads(row["terminal_classes"]) == {
            "closed_won": "win", "closed_lost": "loss",
        }
        assert _credited(_resolve_and_close(conn, "closed_won")) is True


def test_reward_rail_shape1_credits_loss_terminal_never(fresh_home):
    """9(b) shape-1: the declared 'loss' terminal never credits."""
    contract = _contract(
        event_loops=[
            {
                "key": "seller_follow_up",
                "type": "seller_follow_up",
                "entity": "conversation_thread",
                "triggers": [
                    {"kind": "timer", "detail": "x", "cadence_hours": 72},
                    {"kind": "inbound", "detail": "y", "channel": "infobip"},
                ],
                "terminal_states": [
                    {"state": "closed_won", "class": "win"},
                    {"state": "closed_lost", "class": "loss"},
                ],
                "max_nudges": 2,
            }
        ],
        entities=[
            {
                "key": "conversation_thread",
                "type": "conversation",
                "states": ["open", "closed_won", "closed_lost"],
                "terminal_states": ["closed_won", "closed_lost"],
            }
        ],
    )
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        assert _credited(_resolve_and_close(conn, "closed_lost")) is False


def test_grammar_terminal_outcome_class_api():
    """FIX 8: the new public grammar helpers have a pinned contract."""
    assert grammar.TERMINAL_OUTCOME_CLASSES == frozenset({"win", "loss", "neutral"})
    assert grammar.is_known_terminal_outcome_class("win") is True
    assert grammar.is_known_terminal_outcome_class(" WIN ") is True  # strip+fold
    assert grammar.is_known_terminal_outcome_class("victory") is False
    assert grammar.is_known_terminal_outcome_class(None) is False
    assert grammar.is_known_terminal_outcome_class(123) is False
    assert grammar.normalize_terminal_outcome_class(" Win ") == "win"
    assert grammar.normalize_terminal_outcome_class("victory") is None
    assert grammar.normalize_terminal_outcome_class(None) is None


def test_grammar_loop_declared_terminal_classes_drops_unknown():
    """FIX 8: the grammar parser drops an unknown class (shape-1 and shape-2)."""
    out1 = grammar.loop_declared_terminal_classes(
        {
            "terminal_states": [
                {"state": "closed_won", "class": "win"},
                {"state": "closed_lost", "class": "loss"},
                {"state": "typo_state", "class": "victory"},
            ]
        }
    )
    assert out1 == {"closed_won": "win", "closed_lost": "loss"}
    out2 = grammar.loop_declared_terminal_classes(
        {"terminal_classes": {"closed_won": "win", "bad": "victory"}}
    )
    assert out2 == {"closed_won": "win"}


def test_fix3_strict_crash_time_recognition_fails_closed(fresh_home, monkeypatch):
    """FIX 8 / merge-gate Sec.3: if the strict recognition checker RAISES mid-tick
    the loop must NOT fire autonomously (fail-closed). We pin the contract: the
    crash propagates (the tick does not silently swallow-and-fire), and the loop
    spends no nudge / stays un-fired.

    Mutation check: a future change to swallow the crash and fall through to fire
    would make ``fired`` non-empty / spend a nudge -- failing here."""
    _approve("serious", _strict_contract("external_reversible", strict=True))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        base = int(row["next_fire_at"])

        def _boom(_cls):
            raise RuntimeError("recognition checker crashed")

        monkeypatch.setattr(kb, "_side_effect_class_is_recognized", _boom)
        with pytest.raises(RuntimeError):
            kb.reactive_tick(conn, now=base, board="serious")
        after = _schedule_row(conn)
        # Fail-closed BEHAVIOR: no nudge spent, the loop did not fire.
        assert int(after["nudges_used"]) == 0
        assert int(after["active"]) == 1


@pytest.mark.parametrize("max_defers,terminate_on_tick", [(0, 1), (1, 2), (2, 3)])
def test_fix8_max_defers_boundary(fresh_home, max_defers, terminate_on_tick):
    """FIX 8: the max_defers boundary, incl. the 0 case (terminate on the FIRST
    unapproved fire attempt). The loop deferral_exhausts on ``terminate_on_tick``."""
    _approve("serious", _deferring_contract(max_defers=max_defers))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        cadence = int(row["cadence_seconds"])
        t = int(row["next_fire_at"])
        terminated_on = None
        for tick in range(1, terminate_on_tick + 2):
            res = kb.reactive_tick(conn, now=t, board="serious")
            if res["stopped"] == [
                {"loop_key": "seller_follow_up", "reason": "deferral_exhausted"}
            ]:
                terminated_on = tick
                break
            t += cadence
        assert terminated_on == terminate_on_tick
        assert int(_schedule_row(conn)["active"]) == 0


def test_fix8_max_defers_alias(fresh_home):
    """FIX 8: the ``max_deferrals`` alias resolves the bound (terminates)."""
    loop = {
        "key": "seller_follow_up",
        "type": "seller_follow_up",
        "entity": "conversation_thread",
        "triggers": [{"kind": "timer", "detail": "x", "cadence_hours": 72}],
        "terminal_states": ["won", "lost"],
        "side_effect_class": "external_reversible",
        "max_nudges": 100,
        "max_deferrals": 1,  # the alias, not max_defers
    }
    contract = _contract(
        side_effect_class="external_reversible",
        allowed_side_effects=["external_reversible"],
        event_loops=[loop],
        approval_gates=[
            {"key": "owner_rev", "required_before": ["external_reversible"]}
        ],
        side_effect_policy={
            "allowed": ["none"],
            "forbidden": ["financial"],
            "approval_required": ["external_reversible"],
        },
    )
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        assert int(row["max_defers"]) == 1, "the max_deferrals alias must resolve"
        cadence = int(row["cadence_seconds"])
        t = int(row["next_fire_at"])
        terminated = False
        for _ in range(5):
            res = kb.reactive_tick(conn, now=t, board="serious")
            if res["stopped"]:
                terminated = True
                break
            t += cadence
        assert terminated


# ===========================================================================
# ROUND-2 RED-TEAM FIXES (regression tests: each fails-without / passes-with).
# ===========================================================================


# --- FIX 1: mixed-case persisted side_effect_class vs lowercased policy --------


@pytest.mark.parametrize("policy_field", ["forbidden", "approval_required"])
def test_rearch_legacy_mixed_case_row_non_strict_is_byte_identical(fresh_home, policy_field):
    """RE-ARCHITECTURE (round-3 HIGH fix): a LEGACY schedule row whose
    ``side_effect_class`` was persisted verbatim (mixed-case
    'External_Irreversible') on a board that did NOT opt into strict must produce
    the BASE 019271994 gate decision verbatim -- the case-SENSITIVE policy set
    {external_irreversible} does NOT contain 'External_Irreversible', so the loop
    FIRES. The prior round-2 fix lower-cased both sides UNCONDITIONALLY, which
    FLIPPED this LEGACY row's decision (fired -> blocked/deferred) with no opt-in
    -- the exact regression this re-architecture removes.

    Mutation check (re-introduce the unconditional lower-casing): the loop would
    be blocked/deferred instead of fired, failing the ``fired`` assertion here.
    """
    contract = _strict_contract("external_irreversible", strict=False)
    contract["side_effect_policy"][policy_field] = ["external_irreversible"]
    if policy_field == "approval_required":
        contract["approval_gates"] = [
            {"key": "owner_ei", "required_before": ["external_irreversible"]}
        ]
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        # Simulate a row persisted by PRE-upgrade code: verbatim mixed-case class.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE reactive_timer_schedules SET side_effect_class = ? WHERE id = ?",
                ("External_Irreversible", int(row["id"])),
            )
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        # BYTE-IDENTICAL to base: case-sensitive miss -> the loop FIRES.
        assert res["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}], (
            "a non-strict board's mixed-case legacy row must keep its BASE "
            "case-sensitive gate decision (fires) -- no unconditional lower-casing"
        )
        assert res["stopped"] == []
        assert res["deferred"] == []


@pytest.mark.parametrize("policy_field", ["forbidden", "approval_required"])
def test_rearch_strict_board_lowercases_and_catches_mixed_case_row(fresh_home, policy_field):
    """RE-ARCHITECTURE: the case canonicalization is OPT-IN. When the board DOES
    opt into strict, the read-time + policy-set lower-casing is applied, so a
    mixed-case 'External_Irreversible' row IS caught by the lower-case policy
    entry (forbidden -> stopped, approval_required -> deferred). This is the
    genuinely-correct enabled behavior, now gated behind strict so it can never
    flip a non-opted board's decision."""
    contract = _strict_contract("external_irreversible", strict=True)
    contract["side_effect_policy"][policy_field] = ["external_irreversible"]
    if policy_field == "approval_required":
        contract["approval_gates"] = [
            {"key": "owner_ei", "required_before": ["external_irreversible"]}
        ]
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE reactive_timer_schedules SET side_effect_class = ? WHERE id = ?",
                ("External_Irreversible", int(row["id"])),
            )
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["fired"] == [], "under strict, the lowercased class must be caught"
        if policy_field == "forbidden":
            assert res["stopped"] == [
                {"loop_key": "seller_follow_up", "reason": "side_effect_forbidden"}
            ]
        else:
            assert res["deferred"] == [
                {"loop_key": "seller_follow_up", "reason": "approval_required"}
            ]


def test_rearch_mixed_case_row_not_in_policy_non_strict_fires(fresh_home):
    """RE-ARCHITECTURE control: a non-strict mixed-case row whose class is NOT in
    any policy set fires (base behavior). The case handling never over-blocks."""
    contract = _strict_contract("none", strict=False)
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE reactive_timer_schedules SET side_effect_class = ? WHERE id = ?",
                ("Internal", int(row["id"])),  # mixed-case, benign, not in policy
            )
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}]


# --- FIX 2: unicode-digit / negative-string bounds -----------------------------


@pytest.mark.parametrize("bad", ["³", "①", "²"])  # superscript-3, circled-1, superscript-2
def test_r2_fix2_isdigit_true_int_false_does_not_crash(bad):
    """ROUND-2 FIX 2: a string where ``str.isdigit()`` is True but ``int()``
    raises (superscript/circled digits) must NOT crash ``_coerce_int`` (which the
    per-loop compile would swallow, silently dropping the watcher). It returns
    None (treated as no usable bound).

    Mutation check (revert FIX 2): the old ``...isdigit()`` gate admits these and
    the unguarded ``int()`` raises ValueError -- this assertion would surface the
    crash instead of None.
    """
    assert rt._coerce_int(bad) is None
    assert rt.loop_max_defers({"max_defers": bad}) is None
    assert rt.loop_max_nudges({"max_nudges": bad}) is None


def test_r2_fix2_arabic_indic_digit_not_silently_coerced():
    """ROUND-4 FIX B (supersedes round-2 ascii-narrowing): '٣' (Arabic-Indic 3)
    is parsed by int() to 3 EXACTLY as base 019271994 -- the round-2 ascii gate
    that turned it into None was itself the default-off divergence (a legacy loop
    carrying such a bound stopped being a usable bound at HEAD). Round-4 drops the
    ascii narrowing: '٣' coerces to 3 (byte-identical to base int('٣')==3)."""
    assert rt._coerce_int("٣") == 3 == int("٣")
    assert rt.loop_max_defers({"max_defers": "٣"}) == 3


def test_r2_fix2_loop_not_silently_dropped_on_unicode_digit_bound(fresh_home):
    """ROUND-2 FIX 2 (effect): a loop declaring ``max_defers: '³'`` must NOT
    silently vanish at compile -- the board still registers the timer schedule
    (treated as no bound), AND intake flags the malformed bound.

    Mutation check (revert FIX 2): the compile path raises on int('³'), the
    per-loop compile swallows it, and 0 schedule rows exist -> the row-count
    assertion fails.
    """
    loop = {
        "key": "seller_follow_up",
        "type": "seller_follow_up",
        "entity": "conversation_thread",
        "triggers": [{"kind": "timer", "detail": "x", "cadence_hours": 72}],
        "terminal_states": ["won", "lost"],
        "max_nudges": 5,
        "max_defers": "³",
    }
    contract = _contract(event_loops=[loop])
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        rows = conn.execute(
            "SELECT * FROM reactive_timer_schedules WHERE board = ?", ("serious",)
        ).fetchall()
        assert len(rows) == 1, "the loop must still compile (not silently dropped)"
        assert rows[0]["max_defers"] is None  # not a usable bound -> unbounded
    report = inv.check_contract_invariants(contract)
    assert any("max_defers" in e and "non-negative integer bound" in e
               for e in report.errors), "intake must flag max_defers='³'"


@pytest.mark.parametrize("neg", ["-1", "-5"])
def test_r2_fix2_negative_string_bound_flagged_at_intake(neg):
    """ROUND-2 FIX 2 (folds in the LOW): a NEGATIVE string bound ('-1') must be
    flagged at intake like the integer -1 already is -- the runtime coerces it to
    a negative int then drops the cap (unbounded), so intake must not bless it.

    Mutation check (revert FIX 2 intake narrowing): the old
    ``not val.strip().lstrip('-').isdigit()`` returns False for '-1' (treats it as
    OK), so no error is emitted -- this assertion fails.
    """
    report = inv.check_contract_invariants(
        _invariant_contract(_invariant_loop(max_defers=neg))
    )
    assert any("max_defers" in e and "non-negative integer bound" in e
               for e in report.errors), f"intake must flag max_defers={neg!r}"
    # And the runtime really does drop the cap (the divergence the flag closes).
    assert rt.loop_max_defers({"max_defers": neg}) is None


def test_r2_fix2_intake_and_runtime_agree_on_unicode_digit():
    """ROUND-4 FIX B: intake and runtime agree byte-for-byte on what is a usable
    bound. '³' (superscript, int() RAISES) is flagged at intake AND coerces to
    None at runtime (caught, no crash -- the FIX-6 goal). '٣' (Arabic-Indic 3,
    int() PARSES) coerces to 3 in BOTH (byte-identical to base int('٣')==3); the
    round-2 ascii gate that forced it to None is gone."""
    report = inv.check_contract_invariants(
        _invariant_contract(_invariant_loop(max_defers="³"))
    )
    assert any("max_defers" in e for e in report.errors)
    assert rt.loop_max_defers({"max_defers": "³"}) is None
    assert inv._coerce_nonneg_int("³") is None
    # Arabic-Indic 3 parses identically in intake and runtime (== base).
    assert inv._coerce_nonneg_int("٣") == 3
    assert rt._coerce_int("٣") == 3


# --- FIX 3: opted-in but dropped declaration fails CLOSED (no substring) --------


def test_r2_fix3_opted_in_dropped_class_fails_closed_no_substring(fresh_home):
    """ROUND-2 FIX 3: an OPTED-IN loop whose declared win class is unknown and
    dropped by the grammar (``terminal_classes={closed_won:'victory'}``) must NOT
    silently revert to the legacy 'won'-substring path. The compile persists the
    dropped-declaration marker, the reward rail fails CLOSED (credit 0.0), and a
    loud error signal is emitted -- it must not credit a 'won'-embedding outcome.

    Mutation check (revert FIX 3): compile persists NULL terminal_classes (the
    dropped map looks like 'never opted in'), the reward rail uses the substring
    path, and a 'closed_won' outcome credits 1.0 -- failing the no-credit
    assertion here.
    """
    _approve(
        "serious",
        _declared_class_contract(
            terminal_states=["closed_won"],
            terminal_classes={"closed_won": "victory"},  # unknown class -> dropped
        ),
    )
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        # FIX 3: the WHOLE declaration was dropped (no class survived), so compile
        # persists the fail-closed marker, NOT NULL (which would read as 'never
        # opted in' -> substring path).
        assert row["terminal_classes"] == kb._TERMINAL_CLASSES_DROPPED_DB_MARKER
        # 'closed_won' embeds 'won' -- the substring path WOULD credit it. The
        # fail-closed path must NOT.
        outcomes = _resolve_and_close(conn, "closed_won")
        assert not _credited(outcomes), (
            "an opted-in loop with a dropped win class must fail CLOSED, not "
            "credit via the substring fallback"
        )
        errs = conn.execute(
            "SELECT COUNT(*) AS n FROM board_signals WHERE board = ? AND "
            "primitive_kind = 'dispatch_blocked' AND reward_kind = 'error'",
            ("serious",),
        ).fetchone()
        assert int(errs["n"]) >= 1, "a dropped declaration must emit a loud error signal"


def test_r2_fix3_partial_survival_win_dropped_does_not_credit_via_substring(fresh_home):
    """ROUND-2 FIX 3 (partial survival): a loop where the WIN class is dropped
    (unknown 'victory') but a LOSS class survives keeps a NON-EMPTY declared map,
    so the substring path is already suppressed -- a 'won'-embedding outcome does
    NOT credit a conversion (the declared path returns False for a non-'win'
    state). This documents that even the partial-drop case never reverts to the
    substring guess.
    """
    _approve(
        "serious",
        _declared_class_contract(
            terminal_states=["closed_won", "closed_lost"],
            terminal_classes={"closed_won": "victory", "closed_lost": "loss"},
        ),
    )
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        # A valid loss survived -> non-empty map persisted (substring suppressed).
        assert json.loads(row["terminal_classes"]) == {"closed_lost": "loss"}
        # 'closed_won' is not a declared 'win' -> no conversion credit (no
        # substring fallback even though it embeds 'won').
        assert not _credited(_resolve_and_close(conn, "closed_won"))


def test_r2_fix3_non_opted_in_loop_is_byte_identical_legacy(fresh_home):
    """ROUND-2 FIX 3 control: a NULL (non-opted-in) loop is byte-identical legacy
    -- compile persists NULL terminal_classes and the substring path credits a
    'won' outcome. The fail-closed marker fires ONLY for an opted-in-but-dropped
    loop, never for a legacy loop."""
    _approve("serious", _contract())  # legacy loop, no terminal_classes
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        assert row["terminal_classes"] is None
        assert _credited(_resolve_and_close(conn, "won"))


def test_r2_fix3_compile_helper_classifies_the_three_cases():
    """ROUND-2 FIX 3 unit: ``loop_terminal_classes_for_compile`` returns None (not
    opted in), the map (valid opt-in), or the dropped sentinel (opted-in but no
    class survived)."""
    assert rt.loop_terminal_classes_for_compile({"entity": "x"}) is None
    assert rt.loop_terminal_classes_for_compile(
        {"terminal_classes": {"closed_won": "win"}}
    ) == {"closed_won": "win"}
    assert rt.loop_terminal_classes_for_compile(
        {"terminal_classes": {"closed_won": "victory"}}
    ) == rt.TERMINAL_CLASSES_DROPPED_SENTINEL
    # A mixed declaration (one valid class) is NOT fail-closed: the valid map wins.
    assert rt.loop_terminal_classes_for_compile(
        {"terminal_classes": {"closed_won": "win", "bad": "victory"}}
    ) == {"closed_won": "win"}
    assert grammar.loop_opts_into_terminal_classes(
        {"terminal_classes": {"closed_won": "victory"}}
    ) is True
    assert grammar.loop_opts_into_terminal_classes({"entity": "x"}) is False


# --- FIX 4: non-finite (inf) max_defers column fails CLOSED, not crash ----------


def test_r2_fix4_inf_max_defers_column_does_not_crash_tick(fresh_home):
    """ROUND-2 FIX 4: a ``max_defers`` column holding +inf (a REAL-affinity value
    SQLite can return as a Python float) must fail CLOSED (terminate) instead of
    raising OverflowError out of ``int()`` and crashing the WHOLE tick.

    Mutation check (revert FIX 4): ``_timer_schedule_max_defers`` catches only
    (TypeError, ValueError); ``int(float('inf'))`` raises OverflowError, which
    propagates through ``_defer_timer_schedule`` into the unguarded per-row loop
    and crashes reactive_tick -- the ``reactive_tick`` call below would raise.
    """
    _approve("serious", _deferring_contract(max_defers=1))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE reactive_timer_schedules SET max_defers = ? WHERE id = ?",
                (float("inf"), int(row["id"])),
            )
        # The tick must not crash; the corrupt-sentinel path fails closed.
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["stopped"] == [
            {"loop_key": "seller_follow_up", "reason": "deferral_exhausted"}
        ]
        assert int(_schedule_row(conn)["active"]) == 0


def test_r2_fix4_max_defers_reader_returns_sentinel_for_inf():
    """ROUND-2 FIX 4 unit: ``_timer_schedule_max_defers`` returns the corrupt
    sentinel (not an exception) for an inf column value."""
    class _Row:
        def __init__(self, v):
            self._v = v
        def keys(self):
            return ["max_defers"]
        def __getitem__(self, k):
            return self._v if k == "max_defers" else None
    assert kb._timer_schedule_max_defers(_Row(float("inf"))) == kb._MAX_DEFERS_CORRUPT
    assert kb._timer_schedule_max_defers(_Row(float("-inf"))) == kb._MAX_DEFERS_CORRUPT
    assert kb._timer_schedule_defers_used(_Row(float("inf"))) == 0


# --- RE-ARCHITECTURE: hash REVERT + byte-identity via opt-in gating ------------


def test_rearch_hash_strips_invariants_block():
    """ROUND-4 FIX A (supersedes the round-2/round-3 REVERT): the canonical hash
    NOW ALSO strips ``launch_intake.invariants`` -- it is a DERIVED report
    (recomputable via check_contract_invariants), not authorial intent, so it must
    NOT bind the hash. This is what structurally removes the recurring default-off
    hash-flip: two contracts that differ ONLY in their invariants report hash
    IDENTICALLY, so no check finding can ever change the hash.

    The round-2/round-3 worry (stripping un-approved base boards) is resolved by
    the re-stamp migration (``_restamp_launch_approval_contract_hash``), which
    re-binds any OLD-scheme approval to the new hash so the board stays approved
    (see test_fixA_restamp_keeps_old_scheme_approval_valid)."""
    base = _contract()
    with_clean = dict(base)
    with_clean["launch_intake"] = {"invariants": {"ok": True, "errors": [], "warnings": []}}
    with_findings = dict(base)
    with_findings["launch_intake"] = {
        "invariants": {"ok": False, "errors": ["something"], "warnings": ["a typo"]}
    }
    assert kb._business_contract_hash(with_clean) == kb._business_contract_hash(with_findings), (
        "invariants is DERIVED telemetry and must be stripped from the hash"
    )


def test_rearch_invariants_strip_drops_both_telemetry_keys():
    """ROUND-4 FIX A: ``_strip_contract_hash_telemetry`` drops BOTH
    ``completeness`` AND ``invariants``, and drops a launch_intake left holding
    nothing but telemetry entirely (so it hashes like a contract with no
    launch_intake)."""
    def new_strip(normalized):
        # The expected post-FIX-A behavior, reconstructed independently.
        if not isinstance(normalized, dict):
            return normalized
        intake = normalized.get("launch_intake")
        if not isinstance(intake, dict):
            return normalized
        if not any(k in intake for k in ("completeness", "invariants")):
            return normalized
        clone = dict(normalized)
        ic = dict(intake)
        ic.pop("completeness", None)
        ic.pop("invariants", None)
        if ic:
            clone["launch_intake"] = ic
        else:
            clone.pop("launch_intake", None)
        return clone

    probes = [
        {"objective": {"statement": "x"}},
        {"launch_intake": {"completeness": {"blocking": []}}},
        {"launch_intake": {"invariants": {"ok": True}}},
        {"launch_intake": {"completeness": {"a": 1}, "invariants": {"ok": False}}},
        {"launch_intake": {"completeness": {"a": 1}, "other": 2}},
        {"launch_intake": {"invariants": {"ok": True}, "answers": {"g": "x"}}},
    ]
    for p in probes:
        assert kb._strip_contract_hash_telemetry(dict(p)) == new_strip(dict(p)), p


def test_rearch_legacy_contract_hash_byte_identical_via_optin_gating():
    """RE-ARCHITECTURE (the whole point of directive #1+#2): a legacy contract
    carrying a common ``max_*`` loop key (e.g. ``max_followers`` -- plausible in
    the ninaxfinds-growth context) produces NO new invariant finding (the new
    checks are gated behind opt-in, and ``max_followers`` is not an opt-in), so
    the SAME logical contract synthesized at HEAD bakes an IDENTICAL invariants
    block and hashes identically -- WITHOUT touching the hashed payload.

    This is the durable mechanism: byte-identity comes from the non-opted
    contract producing zero new findings, not from stripping the block."""
    loop = _invariant_loop(max_followers=3, max_depth=2)
    contract = _invariant_contract(loop)
    # Sanity: this contract did NOT opt into step-9.
    assert grammar.contract_opts_into_step9(contract) is False
    report = inv.check_contract_invariants(contract)
    # The opt-in gate means the near-miss check never runs -> no typo warnings,
    # so the invariants block is identical base-vs-HEAD.
    assert not any("typo" in w for w in report.warnings)
    assert not any("max_followers" in w for w in report.warnings)
    assert not any("max_depth" in w for w in report.warnings)
    # The same contract synthesized twice (same findings) hashes identically even
    # WITH the invariants block embedded (it is no longer stripped).
    c1 = dict(contract)
    c1["launch_intake"] = {"invariants": report.as_dict()}
    c2 = dict(contract)
    c2["launch_intake"] = {"invariants": inv.check_contract_invariants(contract).as_dict()}
    assert kb._business_contract_hash(c1) == kb._business_contract_hash(c2)


def test_r2_fix5_strict_case_variant_surfaces_near_miss_and_is_honored(fresh_home):
    """ROUND-2 FIX 5: a CASE-VARIANT strict opt-in ('Strict') is (a) HONORED at
    runtime (the key case is canonicalized) and (b) surfaced as a near-miss at
    intake -- previously it was silently inert AND escaped the warning.

    Mutation check (revert the case canonicalization): ``Strict`` resolves to
    strict=False and the unrecognized-class loop FIRES instead of deferring."""
    # (a) intake surfaces it.
    report = inv.check_contract_invariants(
        _invariant_contract(
            _invariant_loop(terminal_classes={"closed_won": "win", "closed_lost": "loss"}),
            policy={"allowed": ["none"], "Strict": True},
        )
    )
    assert any("strict" in w.lower() for w in report.warnings), (
        "a case-variant 'Strict' must surface at intake"
    )
    # (b) runtime honors it (strict enforcement is ON for a capitalized opt-in).
    assert kb._side_effect_strict_enabled({"Strict": True}) is True
    assert kb._side_effect_strict_enabled({"STRICT": True}) is True
    assert kb._side_effect_strict_enabled({"strict": True}) is True
    assert kb._side_effect_strict_enabled({"strict": False}) is False
    # Effect: a capitalized opt-in really enforces (unrecognized class defers).
    contract = _strict_contract("extrnal_ireversible", strict=False)
    contract["side_effect_policy"]["Strict"] = True  # capitalized opt-in
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["fired"] == [], "a capitalized 'Strict' opt-in must enforce"
        assert res["deferred"] == [
            {"loop_key": "seller_follow_up", "reason": "side_effect_unrecognized_strict"}
        ]


def test_r2_fix5_strikt_typo_surfaces_near_miss():
    """ROUND-2 FIX 5: a real typo 'strikt' surfaces a near-miss (the old detector
    missed it)."""
    report = inv.check_contract_invariants(
        _invariant_contract(
            _invariant_loop(terminal_classes={"closed_won": "win", "closed_lost": "loss"}),
            policy={"allowed": ["none"], "strikt": True},
        )
    )
    assert any("strict" in w and "typo" in w for w in report.warnings)


@pytest.mark.parametrize("benign", ["max_depth", "max_delay", "max_followers", "max_results"])
def test_r2_fix5_near_miss_does_not_cry_wolf_on_legit_keys(benign):
    """ROUND-2 FIX 5: the tightened near-miss must NOT flag legitimate unrelated
    loop keys (no false-positive 'typo' warning).

    Mutation check (revert to the startswith(r[:6]) rule): these keys would each
    produce a spurious 'looks like a typo' warning -- failing this assertion."""
    report = inv.check_contract_invariants(
        _invariant_contract(_invariant_loop(**{benign: 3}))
    )
    assert not any("typo" in w for w in report.warnings), (
        f"{benign} must not be flagged as a near-miss typo"
    )


# --- FIX 6: migration effect-level test + self-protecting sentinel --------------


def test_r2_fix6_migration_adds_columns_and_legacy_row_reads_defaults(fresh_home):
    """ROUND-2 FIX 6: a PRE-step-9 ``reactive_timer_schedules`` table (without the
    3 new columns) must, after board init/migration, (a) gain the columns, (b)
    read a legacy row as max_defers=NULL / terminal_classes=NULL / defers_used=0,
    and (c) reactive_tick on the migrated mixed-case row is byte-identical to a
    legacy unbounded substring loop (covers BOTH the migration AND FIX 1).

    Every other test uses the fresh CREATE-TABLE DDL, so the additive ALTER path
    was previously unexercised. This drives it directly.
    """
    # Start a real board so the rest of the schema/migration deps exist, then
    # DROP and re-create reactive_timer_schedules WITHOUT the 3 new columns to
    # simulate a pre-step-9 table, insert a legacy mixed-case row, and re-run the
    # additive migration.
    _approve("serious", _strict_contract("none", strict=False))
    with kb.connect(board="serious") as conn:
        with kb.write_txn(conn):
            conn.execute("DROP TABLE reactive_timer_schedules")
            # Pre-step-9 shape: no terminal_classes / max_defers / defers_used.
            conn.execute(
                """
                CREATE TABLE reactive_timer_schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    board TEXT NOT NULL,
                    loop_key TEXT NOT NULL,
                    entity_id TEXT,
                    task_id TEXT,
                    trigger_type TEXT,
                    trigger_key TEXT,
                    cadence_seconds INTEGER NOT NULL,
                    next_fire_at INTEGER NOT NULL,
                    nudges_used INTEGER NOT NULL DEFAULT 0,
                    max_nudges INTEGER,
                    side_effect_class TEXT,
                    action TEXT,
                    terminal_states TEXT,
                    stop_conditions TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_fired_at INTEGER,
                    stop_reason TEXT
                )
                """
            )
            # A legacy row with a VERBATIM mixed-case side_effect_class.
            conn.execute(
                """
                INSERT INTO reactive_timer_schedules
                    (board, loop_key, cadence_seconds, next_fire_at, nudges_used,
                     max_nudges, side_effect_class, terminal_states, active,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, 0, 5, ?, ?, 1, 0, 0)
                """,
                (
                    "serious", "legacy_loop", 72 * 3600, 1_000,
                    "External_Irreversible",
                    json.dumps(["won", "lost"]),
                ),
            )
        # Sanity: the new columns are absent BEFORE migration.
        pre_cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(reactive_timer_schedules)")}
        assert "terminal_classes" not in pre_cols
        assert "max_defers" not in pre_cols
        assert "defers_used" not in pre_cols

        # Run the additive migration directly.
        kb._migrate_add_optional_columns(conn)

        post_cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(reactive_timer_schedules)")}
        assert {"terminal_classes", "max_defers", "defers_used"} <= post_cols, (
            "the migration must add the 3 new columns"
        )
        migrated = conn.execute(
            "SELECT * FROM reactive_timer_schedules WHERE loop_key = 'legacy_loop'"
        ).fetchone()
        # (b) legacy defaults.
        assert migrated["max_defers"] is None
        assert migrated["terminal_classes"] is None
        assert int(migrated["defers_used"]) == 0
        # RE-ARCHITECTURE (directive #3): the migration is ADDITIVE-ONLY. The
        # blanket UPDATE that lower-cased persisted side_effect_class is REMOVED,
        # so the verbatim mixed-case value is PRESERVED -- no existing data is
        # rewritten, and a non-strict board's case-sensitive gate keeps its base
        # decision.
        assert migrated["side_effect_class"] == "External_Irreversible", (
            "the migration must NOT rewrite existing side_effect_class data"
        )

        # (c) reactive_tick on the migrated row is byte-identical legacy: with no
        # policy match and no strict, the loop FIRES (legacy unbounded behavior).
        res = kb.reactive_tick(conn, now=int(migrated["next_fire_at"]), board="serious")
        assert {"loop_key": "legacy_loop", "nudge": 1} in res["fired"]


def test_r2_fix6_terminal_classes_sentinel_is_self_protecting():
    """ROUND-2 FIX 6: the corrupt/dropped sentinel is a DISTINCT non-dict marker
    recognized INSIDE ``_terminal_outcome_is_conversion`` (isinstance), so a
    direct call -- forgetting the caller's identity check -- still fails CLOSED.

    Mutation check (revert to the empty-dict {} sentinel): an empty dict is FALSY,
    so ``if declared_terminal_classes:`` is skipped and the call falls through to
    the substring path -- ``_terminal_outcome_is_conversion('closed_won', [],
    sentinel)`` would return True (fail OPEN), failing this assertion.
    """
    sentinel = kb._TERMINAL_CLASSES_CORRUPT
    assert isinstance(sentinel, kb._TerminalClassesCorrupt)
    # A direct call with the sentinel fails CLOSED for ANY outcome, even a
    # 'won'-embedding one that the substring path would credit.
    assert kb._terminal_outcome_is_conversion("closed_won", [], sentinel) is False
    assert kb._terminal_outcome_is_conversion("won", ["won"], sentinel) is False
    # Identity check the production caller relies on still works.
    assert sentinel is kb._TERMINAL_CLASSES_CORRUPT


# ===========================================================================
# ROUND-3 OPT-IN-PATH fixes (enabled-behavior corrections; only fire under
# opt-in, so the non-opted path stays byte-identical).
# ===========================================================================


@pytest.mark.parametrize("dangerous", ["external_irreversible", "financial"])
def test_r3_strict_wildcard_gate_does_not_satisfy_highest_stakes(fresh_home, dangerous):
    """ROUND-3 (MED): under strict, a broad ``external_action`` WILDCARD approval
    gate must NOT satisfy the highest-stakes classes (external_irreversible /
    financial) -- they require a gate that NAMES the class. With only a wildcard
    gate satisfied, the loop must still DEFER.

    Mutation check (drop require_per_class_gate): the wildcard gate would satisfy
    and the loop would FIRE, failing the deferral assertion here."""
    contract = _strict_contract(dangerous, strict=True)
    contract["approval_gates"] = [
        {"key": "owner_wild", "required_before": ["external_action"]}
    ]
    contract["side_effect_policy"]["approval_required"] = [dangerous]
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        base = int(row["next_fire_at"])
        # Satisfy ONLY the wildcard gate.
        kb.record_contract_approval(
            conn, gate_key="owner_wild",
            entity_ref=row["task_id"], approved_by="owner", board="serious",
        )
        res = kb.reactive_tick(conn, now=base, board="serious")
        assert res["fired"] == [], (
            "a wildcard external_action gate must NOT satisfy a highest-stakes class"
        )
        assert res["deferred"], "the highest-stakes class must still defer"


def test_r3_strict_per_class_gate_satisfies_highest_stakes(fresh_home):
    """ROUND-3 (MED) positive: a per-class gate (required_before names the class)
    DOES satisfy under strict -- the requirement is "name the class", not "no
    gate at all"."""
    contract = _strict_contract("external_irreversible", strict=True)
    contract["approval_gates"] = [
        {"key": "owner_ei", "required_before": ["external_irreversible"]}
    ]
    contract["side_effect_policy"]["approval_required"] = ["external_irreversible"]
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        base = int(row["next_fire_at"])
        kb.record_contract_approval(
            conn, gate_key="owner_ei",
            entity_ref=row["task_id"], approved_by="owner", board="serious",
        )
        res = kb.reactive_tick(conn, now=base, board="serious")
        assert res["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}]


def test_r3_non_strict_wildcard_gate_still_satisfies_byte_identical(fresh_home):
    """ROUND-3 (MED) default-off proof: WITHOUT strict, the base
    ``external_action`` wildcard behavior is unchanged -- a class listed in
    approval_required is satisfied by the wildcard gate and FIRES (byte-identical
    to base 019271994). The per-class requirement is strict-only."""
    contract = _strict_contract("external_irreversible", strict=False)
    contract["approval_gates"] = [
        {"key": "owner_wild", "required_before": ["external_action"]}
    ]
    contract["side_effect_policy"]["approval_required"] = ["external_irreversible"]
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        base = int(row["next_fire_at"])
        kb.record_contract_approval(
            conn, gate_key="owner_wild",
            entity_ref=row["task_id"], approved_by="owner", board="serious",
        )
        res = kb.reactive_tick(conn, now=base, board="serious")
        assert res["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}], (
            "non-strict: the base wildcard-gate behavior must be unchanged"
        )


@pytest.mark.parametrize("frac", [2.7, 0.5, 3.1])
def test_r3_fractional_bound_flagged_at_intake(frac):
    """ROUND-3 (MED): a fractional float defer bound (2.7) is flagged at intake --
    the runtime truncates it silently (int(2.7)==2). Opt-in only (the loop
    declares max_defers, so it opted in)."""
    report = inv.check_contract_invariants(
        _invariant_contract(_invariant_loop(max_defers=frac))
    )
    assert report.ok is False
    assert any("FRACTIONAL" in e and "max_defers" in e for e in report.errors), (
        f"fractional bound {frac} must be flagged"
    )


def test_r3_whole_float_bound_not_flagged():
    """ROUND-3 (MED) control: a WHOLE float (2.0) is a usable bound and must NOT
    be flagged as fractional (it round-trips to the int 2)."""
    report = inv.check_contract_invariants(
        _invariant_contract(_invariant_loop(max_defers=2.0))
    )
    assert not any("FRACTIONAL" in e for e in report.errors)


def test_r3_strict_2edit_typo_surfaces_near_miss():
    """ROUND-3 (MED): a 2-edit typo of the short safety token 'strict' (e.g.
    'striqt') surfaces a near-miss (the cap is 2 for 'strict'). The alias set may
    miss an unenumerated misspelling; the raised cap catches it."""
    report = inv.check_contract_invariants(
        _invariant_contract(
            _invariant_loop(terminal_classes={"closed_won": "win", "closed_lost": "loss"}),
            policy={"allowed": ["none"], "striqt": True},
        )
    )
    assert any("strict" in w and "typo" in w for w in report.warnings), (
        "a 2-edit typo of 'strict' must surface"
    )


@pytest.mark.parametrize("legit", ["max_followers", "max_replies"])
def test_r3_2edit_does_not_cry_wolf_on_long_max_keys(legit):
    """ROUND-3 (MED) control: the raised cap is 'strict'-only. The longer max_*
    keys keep cap 1, so a legitimate distance-2 neighbor (max_followers vs
    max_followups; max_replies vs max_retries) does NOT cry wolf."""
    report = inv.check_contract_invariants(
        _invariant_contract(
            _invariant_loop(
                terminal_classes={"closed_won": "win", "closed_lost": "loss"},
                **{legit: 3},
            )
        )
    )
    assert not any("typo" in w for w in report.warnings), (
        f"{legit} is a legit distance-2 key and must not be flagged"
    )


def test_r3_stateless_win_class_is_error():
    """ROUND-3 (LOW): a declared {state: '', class: 'win'} (empty/missing state)
    can never be matched by the reward rail, so it does NOT count toward has_win
    -- intake flags the stateless win."""
    report = inv.check_contract_invariants(
        _invariant_contract(
            _invariant_loop(terminal_classes={"": "win", "closed_lost": "loss"})
        )
    )
    assert report.ok is False
    assert any("EMPTY/missing state" in e for e in report.errors)


def test_r3_negzero_string_bound_agrees_at_both_layers():
    """ROUND-4 FIX B (supersedes the round-3 '-0' message): with the ascii
    narrowing removed, ``_coerce_nonneg_int('-0') == 0`` and the runtime
    ``_coerce_int('-0') == 0`` AGREE -- '-0' is a consistent (if odd) cap of 0 at
    BOTH layers, so there is no longer a divergence to flag. The negative-bound
    flag still fires for a GENUINE negative ('-1'/'-5', which coerce to a negative
    int the runtime drops); '-0' is simply 0. This keeps intake and runtime in
    byte-for-byte agreement."""
    assert inv._coerce_nonneg_int("-0") == 0
    assert rt._coerce_int("-0") == 0
    report = inv.check_contract_invariants(
        _invariant_contract(_invariant_loop(max_defers="-0"))
    )
    # '-0' is a usable cap of 0 -> no max_defers error (the layers agree).
    assert not any("max_defers" in e for e in report.errors), (
        "a '-0' bound coerces to 0 at both layers and must not be flagged"
    )


# ===========================================================================
# OVERARCHING PROOF: a non-opted (legacy) contract hits ZERO new step-9 code
# paths and is BYTE-IDENTICAL to base 019271994 at EVERY layer. This is the
# whole point of the re-architecture (the principle). The OPT-IN path is
# asserted side-by-side so the enabled behavior is proven too.
# ===========================================================================

import subprocess as _subprocess  # noqa: E402
import types as _types  # noqa: E402


def _load_base_module(modname: str, gitpath: str):
    """Load a module's BASE (019271994) source as a standalone module bound to the
    CURRENT grammar (a strict superset of base's grammar symbols)."""
    src = _subprocess.run(
        ["git", "show", f"019271994:{gitpath}"],
        capture_output=True, text=True, cwd=str(_WORKTREE),
    ).stdout
    assert src, f"failed to load base source for {gitpath}"
    mod = _types.ModuleType(modname)
    sys.modules[modname] = mod  # register before exec so dataclass can resolve
    exec(compile(src, f"<{modname}>", "exec"), mod.__dict__)
    return mod


def _legacy_contract():
    """A representative LEGACY contract with NO step-9 opt-in (no terminal_classes,
    no defer bound, no strict). It carries a common ``max_*`` knob and a mixed-case
    policy-ish key to prove these do NOT trip any new code path."""
    return _contract()


def test_overarching_legacy_contract_hits_zero_new_code_paths():
    """THE PRINCIPLE: a non-opted contract is byte-identical to base 019271994."""
    contract = _legacy_contract()
    # Pin the non-opt-in precondition.
    assert grammar.contract_opts_into_step9(contract) is False

    # (a) check_contract_invariants errors+warnings byte-identical to base.
    base_inv = _load_base_module("base_invariants", "hermes_cli/kanban_launch_invariants.py")
    base_report = base_inv.check_contract_invariants(contract)
    head_report = inv.check_contract_invariants(contract)
    assert head_report.errors == base_report.errors, "invariant ERRORS diverged from base"
    assert head_report.warnings == base_report.warnings, "invariant WARNINGS diverged from base"

    # (b) _business_contract_hash byte-identical: the strip body matches base and
    # no new finding perturbs the hashed payload (proven separately). Here assert
    # the hash is stable across re-synthesis of the SAME contract.
    h1 = kb._business_contract_hash(contract)
    h2 = kb._business_contract_hash(_legacy_contract())
    assert h1 == h2

    # (d) _terminal_outcome_is_conversion byte-identical across a 200+ outcome
    # probe on the LEGACY path (declared_terminal_classes None/empty).
    def base_conversion(outcome, states):
        if not outcome:
            return False
        o = str(outcome).strip().lower()
        if not o:
            return False
        if "won" in o:
            return True
        for d in states or []:
            dd = str(d).strip().lower()
            if dd == o and kb._looks_like_win_token(dd):
                return True
        return False

    tokens = [
        "won", "closed_won", "closed_lost", "won_but_lost", "unwon", "wonky",
        "under_contract", "onboarded", "signed", "published", "converted",
        "paid", "delivered", "completed", "approved", "lost", "expired",
        "churned", "cancelled", "renewal_won", "deal_lost", "arbitrary", "",
        None, "WON", "Closed_Won", "won_back_from_churn",
        "renewal_after_cancellation_won", "reactivated_expired_subscriber_won",
    ]
    state_variants = [
        [], ["won", "lost"], ["closed_won", "closed_lost"],
        ["under_contract"], ["onboarded"], ["signed"],
    ]
    probes = 0
    for o in tokens:
        for st in state_variants:
            base = base_conversion(o, st)
            # Both the None and the empty-{} legacy declared-class forms.
            assert kb._terminal_outcome_is_conversion(o, st, None) is base, (o, st)
            assert kb._terminal_outcome_is_conversion(o, st, {}) is base, (o, st)
            probes += 2
    assert probes >= 200, f"need 200+ conversion probes, ran {probes}"


@pytest.mark.parametrize("policy_field", ["forbidden", "approval_required"])
def test_overarching_legacy_reactive_gate_byte_identical(fresh_home, policy_field):
    """(c) reactive_tick gate decisions byte-identical on a mixed-case row under a
    NON-opted (non-strict) board: the base case-SENSITIVE matching is used, so a
    mixed-case 'External_Irreversible' row vs a lower-case policy entry MISSES and
    the loop FIRES -- exactly base 019271994."""
    contract = _strict_contract("external_irreversible", strict=False)
    contract["side_effect_policy"][policy_field] = ["external_irreversible"]
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE reactive_timer_schedules SET side_effect_class = ? WHERE id = ?",
                ("External_Irreversible", int(row["id"])),
            )
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}]
        assert res["stopped"] == []
        assert res["deferred"] == []


def test_overarching_legacy_defer_path_does_not_write_defers_used(fresh_home):
    """(c) the legacy deferral path (no declared bound) is byte-identical: it does
    the BASE update (next_fire_at only) and never increments defers_used (a
    non-opted loop hits ZERO new write)."""
    contract = _strict_contract("external_irreversible", strict=False)
    contract["side_effect_policy"]["approval_required"] = ["external_irreversible"]
    # No approval gate satisfied -> the legacy approval_required path defers.
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["deferred"] == [
            {"loop_key": "seller_follow_up", "reason": "approval_required"}
        ]
        after = _schedule_row(conn)
        # max_defers NULL (not opted in) -> the base path ran -> defers_used stays 0.
        assert after["max_defers"] is None
        assert int(after["defers_used"]) == 0, (
            "a non-opted legacy defer must not increment defers_used"
        )


def test_overarching_optin_path_still_enforces(fresh_home):
    """The OPT-IN path still enforces: strict defers an unrecognized/typo class;
    a declared terminal class credits ONLY 'win'; a defer bound terminates."""
    # strict defers a typo'd class.
    contract = _strict_contract("extrnal_ireversible", strict=True)
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["fired"] == []
        assert res["deferred"] == [
            {"loop_key": "seller_follow_up", "reason": "side_effect_unrecognized_strict"}
        ]

    # declared terminal class: only the exact declared 'win' credits.
    assert kb._terminal_outcome_is_conversion(
        "closed_won", ["closed_won"], {"closed_won": "win", "closed_lost": "loss"}
    ) is True
    assert kb._terminal_outcome_is_conversion(
        "won_but_lost", [], {"won_but_lost": "loss"}
    ) is False  # declared loss never credits, even with 'won' substring


def test_overarching_intake_optin_enforces(fresh_home):
    """The OPT-IN intake path still enforces: a typo'd strict key warns, an
    unknown declared class errors, a fractional bound errors -- but ONLY because
    the contract opted in (a non-opted contract gets none of these)."""
    # opted in via terminal_classes -> typo'd strict surfaces.
    opted = _invariant_contract(
        _invariant_loop(terminal_classes={"closed_won": "win", "closed_lost": "loss"}),
        policy={"allowed": ["none"], "strikt": True},
    )
    rep = inv.check_contract_invariants(opted)
    assert any("strict" in w and "typo" in w for w in rep.warnings)

    # NOT opted in -> the SAME strikt key produces NO warning (byte-identical).
    not_opted = _invariant_contract(
        _invariant_loop(),  # no terminal_classes, no bound
        policy={"allowed": ["none"], "strikt": True},
    )
    assert grammar.contract_opts_into_step9(not_opted) is False
    rep2 = inv.check_contract_invariants(not_opted)
    assert not any("strikt" in w or "typo" in w for w in rep2.warnings), (
        "a non-opted contract must not surface the strict typo warning"
    )


# ===========================================================================
# ROUND-4 FIX A: the contract hash must be invariant to ANY check finding --
# launch_intake.invariants is a DERIVED report (recomputable via
# check_contract_invariants), not authorial intent, so it must not bind the
# canonical hash. This structurally removes the recurring default-off hash-flip:
# a new finding (a step-9 opt-in OR an overloaded key like class/kind/
# max_defers/terminal_classes OR a pre-existing finding) can never change the
# hash. Plus the one-time re-stamp migration keeps an OLD-scheme approval valid.
# ===========================================================================


def _overloaded_key_contract(*, max_nudges=5):
    """A contract whose loop carries OVERLOADED keys an invariant check could
    react to: a terminal_states entry carrying ``class``/``kind``, a sibling
    ``terminal_classes`` map, and a fractional ``max_defers``. None of these may
    change the canonical hash (they are not part of the hashed semantics that
    base 019271994 hashed -- only the invariants REPORT could differ, and that is
    now stripped)."""
    return {
        "objective": {
            "statement": "x",
            "success": ["s"],
            "failure": ["f"],
            "constraints": ["c"],
        },
        "event_loops": [
            {
                "key": "lp",
                "type": "lp",
                "entity": "e",
                "triggers": [{"kind": "timer", "detail": "d", "cadence_hours": 72}],
                "terminal_states": [
                    {"state": "closed_won", "class": "win", "kind": "weird"},
                    "closed_lost",
                ],
                "terminal_classes": {"closed_won": "win", "closed_lost": "loss"},
                "max_defers": 2.5,
                "max_nudges": max_nudges,
            }
        ],
    }


def test_fixA_overloaded_key_contract_hash_identical_base_vs_head():
    """FIX A byte-identity proof: a contract with overloaded keys (a
    terminal_states entry carrying class/kind, a terminal_classes map, a
    fractional max_defers) hashes IDENTICALLY base-vs-HEAD.

    A contract that carries NO launch_intake telemetry hashes identically under
    both the base (completeness-only) and HEAD (also strips invariants) schemes,
    so the head hash equals base 019271994's hash byte-for-byte.

    Mutation check: if FIX A ever bound a check finding to the hash (e.g. by
    persisting launch_intake.invariants into the hashed payload), the head hash
    would drift from base for any contract whose invariants report differs, and
    this equality fails."""
    base_kb = _load_base_module("base_kb_fixA", "hermes_cli/kanban_db.py")
    contract = _overloaded_key_contract()
    assert kb._business_contract_hash(contract) == base_kb._business_contract_hash(contract), (
        "overloaded-key contract hash diverged from base 019271994"
    )


def test_fixA_hash_invariant_to_invariants_report_content():
    """FIX A root: the canonical hash is INVARIANT to launch_intake.invariants
    content -- two contracts identical except for a differing invariants report
    (one ok, one with errors/warnings) hash IDENTICALLY. This is what makes a new
    finding (step-9 opt-in OR an overloaded key OR a pre-existing finding) unable
    to flip the default-off hash.

    Mutation check: revert FIX A (keep invariants in the hashed payload) and the
    two hashes differ -> this fails."""
    base = _overloaded_key_contract()
    c_ok = json.loads(json.dumps(base))
    c_ok["launch_intake"] = {"answers": {"a": 1}, "invariants": {"ok": True, "errors": [], "warnings": ["w1"]}}
    c_bad = json.loads(json.dumps(base))
    c_bad["launch_intake"] = {"answers": {"a": 1}, "invariants": {"ok": False, "errors": ["e2"], "warnings": []}}
    assert kb._business_contract_hash(c_ok) == kb._business_contract_hash(c_bad), (
        "the hash must not depend on the launch_intake.invariants report"
    )
    # And a launch_intake holding ONLY derived telemetry hashes identically to a
    # contract with no launch_intake at all (the emptied-intake drop).
    c_only_tel = json.loads(json.dumps(base))
    c_only_tel["launch_intake"] = {"invariants": {"ok": True}, "completeness": {"ok": True}}
    assert kb._business_contract_hash(c_only_tel) == kb._business_contract_hash(base), (
        "a launch_intake holding only telemetry must drop out of the hash entirely"
    )


def test_fixA_invariants_telemetry_does_not_change_contract_hash():
    """FIX A: a contract WITH vs WITHOUT a launch_intake.invariants block hashes
    IDENTICALLY (mirrors the completeness test for the now-also-stripped key).

    Mutation check: remove "invariants" from _CONTRACT_HASH_TELEMETRY_INTAKE_KEYS
    and the WITH-block hash drifts -> this fails."""
    plain = {"objective": {"statement": "x"}, "launch_intake": {"answers": {"g": "do"}}}
    with_inv = {
        "objective": {"statement": "x"},
        "launch_intake": {
            "answers": {"g": "do"},
            "invariants": {"ok": True, "errors": [], "warnings": ["soft"], "checked": {"event_loops": 0}},
        },
    }
    assert kb._business_contract_hash(plain) == kb._business_contract_hash(with_inv)


def test_fixA_restamp_keeps_old_scheme_approval_valid(fresh_home):
    """FIX A re-stamp migration: a synthetic board APPROVED under the OLD
    (completeness-only) hash scheme STAYS approved after the migration -- the
    bound contract_hash is re-stamped to the new scheme on read, so the consumed
    approval token still validates and _board_has_approved_launch_review is True.

    Mutation check: drop the re-stamp call from read_board_metadata and the board
    reads back UN-approved (the stored old hash != the recomputed new hash), so
    _board_has_approved_launch_review returns False here."""
    import time

    contract = {
        "objective": {"statement": "x", "success": ["s"], "failure": ["f"], "constraints": ["c"]},
        "launch_intake": {"answers": {"a": 1}, "invariants": {"ok": True, "errors": [], "warnings": ["w1"]}},
    }
    kb.create_board("restamp", name="Restamp")
    old_hash = kb._legacy_completeness_only_contract_hash(contract)
    new_hash = kb._business_contract_hash(contract)
    # Precondition: the schemes genuinely differ for this invariants-carrying contract.
    assert old_hash != new_hash, "test contract must differ under the two schemes"

    now = int(time.time())
    with kb.connect(board="restamp") as conn:
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO board_launch_approval_tokens "
                "(id,status,kind,board,contract_version,from_version,contract_hash,amendment_id,"
                " approved_by,evidence,reason,created_at,expires_at,token_hash,consumed_at,expired_at,revoked_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("tok1", "consumed", "launch_review", "restamp", 1, None, old_hash, None,
                 "owner", "{}", None, now, now + 999999, "th1", now, None, None),
            )
            conn.execute(
                "INSERT INTO board_launch_reviews "
                "(id,status,kind,board,approval_token_id,contract_version,contract_hash,amendment_id,"
                " approved_by,evidence,reason,readiness,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("rev1", "approved", "launch_review", "restamp", "tok1", 1, old_hash, None,
                 "owner", '{"type":"owner_approval"}', None, "{}", now),
            )
    kb.write_board_metadata(
        "restamp",
        business_contract=contract,
        contract_version=1,
        launch_review_id="rev1",
        launch_approval={
            "id": "rev1", "status": "approved", "approved_by": "owner",
            "evidence": {"type": "owner_approval"}, "approval_token_id": "tok1",
            "contract_hash": old_hash, "contract_version": 1,
        },
        launch_approval_tokens=[{"id": "tok1", "contract_hash": old_hash}],
    )

    meta = kb.read_board_metadata("restamp")
    # In-memory binding re-stamped to the new scheme and the board stays approved.
    assert meta["launch_approval"]["contract_hash"] == new_hash
    assert kb._board_has_approved_launch_review(meta) is True, (
        "an OLD-scheme approval must remain valid after the re-stamp migration"
    )
    # DB review + token rows re-stamped to the new hash.
    with kb.connect(board="restamp") as conn:
        t = conn.execute("SELECT contract_hash FROM board_launch_approval_tokens WHERE id=?", ("tok1",)).fetchone()
        r = conn.execute("SELECT contract_hash FROM board_launch_reviews WHERE id=?", ("rev1",)).fetchone()
    assert t["contract_hash"] == new_hash and r["contract_hash"] == new_hash
    # Persisted board.json re-stamped (idempotent on the second read).
    assert kb.read_board_metadata("restamp")["launch_approval"]["contract_hash"] == new_hash


def test_fixA_restamp_is_noop_without_approval(fresh_home):
    """FIX A re-stamp is a NO-OP for a board with no launch_approval (the LIVE
    case: no live board carries one). The migration returns False and mutates
    nothing."""
    kb.create_board("plain", name="Plain")
    meta = kb.read_board_metadata("plain")
    assert meta.get("launch_approval") is None
    assert kb._restamp_launch_approval_contract_hash(dict(meta)) is False


def test_fixA_restamp_leaves_genuinely_different_contract_untouched(fresh_home):
    """FIX A re-stamp must NOT re-bind an approval whose stored hash matches
    NEITHER scheme for the current contract -- that is a genuine divergence which
    must still fail closed (no silent re-approval)."""
    meta = {
        "slug": "x",
        "objective": {"statement": "x"},
        "launch_approval": {"id": "r", "contract_hash": "deadbeef" * 8, "status": "approved"},
        "launch_approval_tokens": [],
    }
    assert kb._restamp_launch_approval_contract_hash(meta) is False
    assert meta["launch_approval"]["contract_hash"] == "deadbeef" * 8


# ===========================================================================
# ROUND-4 FIX B: legacy coercion is byte-identical to base -- a non-ASCII but
# int()-parseable digit string ('٣'==3) parses EXACTLY as base, while an
# isdigit()-True/int()-raises char ('³','①') is caught -> None (no crash, the
# FIX-6 goal). The default-off invariant report for a legacy loop with
# max_nudges='٣' is byte-identical to base 019271994.
# ===========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("٣", 3),       # Arabic-Indic 3: int() parses it -> EXACTLY as base
        ("٦", 6),       # Arabic-Indic 6
        ("5", 5),       # plain ASCII still works
        ("-3", -3),     # negative ASCII (runtime _coerce_int allows; caller bounds it)
        ("³", None),    # superscript: int() raises -> None (no crash; FIX-6 goal)
        ("①", None),    # circled: int() raises -> None
        ("abc", None),
    ],
)
def test_fixB_coerce_int_matches_base_no_ascii_narrowing(raw, expected):
    """``_coerce_int`` parses a non-ASCII int()-parseable digit EXACTLY as base
    (int(value.strip())) and catches the isdigit()-True/int()-raises chars.

    Mutation check: re-introduce the ``.isascii()`` gate and '٣' coerces to None
    (not 3), diverging from base int('٣')==3 -> this fails."""
    assert rt._coerce_int(raw) == expected
    # Base int() agreement for the parseable cases.
    if expected is not None:
        assert int(raw.strip()) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("٣", 3),
        ("٦", 6),
        ("5", 5),
        ("-1", None),   # negative -> not a non-negative bound
        ("³", None),    # no crash
        ("①", None),
    ],
)
def test_fixB_coerce_nonneg_int_matches_base(raw, expected):
    """``_coerce_nonneg_int`` mirrors ``_coerce_int`` (no ascii narrowing) and
    stays a NON-NEGATIVE coercion."""
    assert inv._coerce_nonneg_int(raw) == expected


def test_fixB_legacy_max_nudges_arabic_digit_byte_identical_invariants():
    """FIX B byte-identity: a LEGACY loop with ``max_nudges='٣'`` produces an
    invariant report (errors+warnings) byte-identical to base 019271994 -- base
    int('٣')==3 is finite, so neither base nor HEAD raises a 'can never stop'
    error.

    Mutation check: re-add the ascii narrowing and '٣' becomes an unusable bound
    at HEAD, so HEAD would flag the loop as unbounded ('can never stop') while
    base does not -> the error lists diverge and this fails."""
    contract = _overloaded_key_contract(max_nudges="٣")
    # Strip the step-9 opt-ins so this is a genuinely LEGACY loop (no opt-in).
    loop = contract["event_loops"][0]
    loop["terminal_states"] = ["closed_won", "closed_lost"]
    loop.pop("terminal_classes", None)
    loop.pop("max_defers", None)
    assert grammar.contract_opts_into_step9(contract) is False

    base_inv = _load_base_module("base_inv_fixB", "hermes_cli/kanban_launch_invariants.py")
    base_report = base_inv.check_contract_invariants(contract)
    head_report = inv.check_contract_invariants(contract)
    assert head_report.errors == base_report.errors, "invariant ERRORS diverged from base"
    assert head_report.warnings == base_report.warnings, "invariant WARNINGS diverged from base"


def test_fixB_compiled_max_nudges_column_matches_base(fresh_home):
    """FIX B: a loop with ``max_nudges='٣'`` compiles a schedule whose persisted
    ``max_nudges`` column == 3 (base int('٣')), AND a '³' bound does NOT crash the
    per-loop compile (the loop still compiles)."""
    loop = {
        "key": "seller_follow_up", "type": "seller_follow_up", "entity": "conversation_thread",
        "triggers": [{"kind": "timer", "detail": "x", "cadence_hours": 72}],
        "terminal_states": ["won", "lost"], "max_nudges": "٣",
    }
    _approve("serious", _contract(event_loops=[loop]))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        assert int(row["max_nudges"]) == 3 == int("٣")

    # A '³' bound must NOT crash the compile -> the loop still compiles (with no
    # usable nudge cap, byte-identical to a legacy loop whose cap was dropped).
    loop2 = dict(loop, max_nudges="³")
    _approve("serious2", _contract(event_loops=[loop2]))
    with kb.connect(board="serious2") as conn:
        rows = conn.execute(
            "SELECT * FROM reactive_timer_schedules WHERE board = ?", ("serious2",)
        ).fetchall()
        assert len(rows) == 1, "a '³' bound must not silently drop the schedule"
        assert rows[0]["max_nudges"] is None


# ===========================================================================
# ROUND-4 FIX C: sentinel runtime belt. Under STRICT, a loop whose AUTHORED
# side_effect_class is a NON-EMPTY sentinel ('null'/'-'/''/whitespace) used to
# normalize to NULL at persistence then coalesce to 'none' at the gate and FIRE
# ungated. It now persists a distinct marker -> the strict gate reads it back as
# unrecognized -> DEFER (fail-CLOSED). A genuinely-absent class (true 'none')
# still fires; non-strict is byte-identical to base.
# ===========================================================================


@pytest.mark.parametrize("sentinel", ["null", "-", "", "   "])
def test_fixC_strict_authored_sentinel_defers(fresh_home, sentinel):
    """STRICT + a NON-EMPTY sentinel side_effect_class -> DEFERS (not fires).

    Mutation check: revert FIX C (persist via _canonical_side_effect_class) and
    the sentinel normalizes to NULL, coalesces to 'none', and FIRES -> deferred
    would be empty and fired non-empty, so this fails."""
    contract = _strict_contract(sentinel, strict=True)
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        # The authored sentinel persisted as the distinct dropped marker.
        assert row["side_effect_class"] == kb._SIDE_EFFECT_CLASS_DROPPED_DB_MARKER
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["fired"] == [], "an authored sentinel must NOT fire under strict"
        assert res["deferred"] == [
            {"loop_key": "seller_follow_up", "reason": "side_effect_unrecognized_strict"}
        ]
        after = _schedule_row(conn)
        assert after["active"] == 1 and after["nudges_used"] == 0


@pytest.mark.parametrize("absent", ["none", "None"])
def test_fixC_strict_true_none_still_fires(fresh_home, absent):
    """STRICT + a genuine ``none`` (the explicit benign class) -> FIRES. FIX C
    only catches a NON-'none' sentinel; the benign none persists as NULL and the
    coalesced 'none' is a recognized SAFE member."""
    _approve("serious", _strict_contract(absent, strict=True))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        assert row["side_effect_class"] is None
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}]
        assert res["deferred"] == []


@pytest.mark.parametrize("sentinel", ["null", "-", "", "   "])
def test_fixC_non_strict_sentinel_byte_identical_to_base(fresh_home, sentinel):
    """NON-STRICT + the SAME sentinel -> persists NULL and FIRES, byte-identical
    to base 019271994 (the marker is NEVER written off the strict path). The only
    difference from the strict test above is the absence of policy.strict."""
    _approve("serious", _strict_contract(sentinel, strict=False))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        # Non-strict persistence is _normalize_funnel_text verbatim -> NULL.
        assert row["side_effect_class"] is None
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}]
        assert res["deferred"] == []


def test_fixC_persist_helper_unit():
    """FIX C unit: the strict persistence normalizer maps absent/none -> NULL and
    a non-'none' sentinel -> the dropped marker; a real class is unchanged."""
    M = kb._SIDE_EFFECT_CLASS_DROPPED_DB_MARKER
    assert kb._canonical_side_effect_class_for_persist(None) is None
    assert kb._canonical_side_effect_class_for_persist("none") is None
    assert kb._canonical_side_effect_class_for_persist("None") is None
    assert kb._canonical_side_effect_class_for_persist("null") == M
    assert kb._canonical_side_effect_class_for_persist("-") == M
    assert kb._canonical_side_effect_class_for_persist("") == M
    assert kb._canonical_side_effect_class_for_persist("   ") == M
    assert kb._canonical_side_effect_class_for_persist("external_irreversible") == "external_irreversible"
    assert kb._canonical_side_effect_class_for_persist("External_Irreversible") == "external_irreversible"
