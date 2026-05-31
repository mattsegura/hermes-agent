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
def test_r2_fix1_legacy_mixed_case_row_keeps_its_safety_gate(fresh_home, policy_field):
    """ROUND-2 FIX 1: a LEGACY schedule row whose ``side_effect_class`` was
    persisted verbatim (mixed-case, e.g. 'External_Irreversible') must STILL be
    caught by a lower-cased forbidden / approval_required policy entry after
    upgrade. ``reactive_tick`` lower-cases the policy sets, so it must also
    coalesce+lower-case the RAW row value at read -- otherwise the gate flips
    blocked->fired / deferred->fired on the two most dangerous controls with no
    flag.

    Mutation check (revert FIX 1): read ``side_effect_class = row[...] or 'none'``
    raw (no .lower()) and the lowercased policy set {external_irreversible} no
    longer contains 'External_Irreversible' -> the loop FIRES, failing the
    blocked/deferred assertions here.
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
        assert res["fired"] == [], (
            "a mixed-case legacy row must NOT fire when its lowercased class is "
            f"in the policy {policy_field} set"
        )
        if policy_field == "forbidden":
            assert res["stopped"] == [
                {"loop_key": "seller_follow_up", "reason": "side_effect_forbidden"}
            ]
        else:
            assert res["deferred"] == [
                {"loop_key": "seller_follow_up", "reason": "approval_required"}
            ]


def test_r2_fix1_mixed_case_row_not_in_policy_still_fires(fresh_home):
    """ROUND-2 FIX 1 control: a mixed-case row whose lowercased class is NOT in
    any policy set still fires (the read-time normalization does not over-block;
    it only makes the policy comparison share one vocabulary)."""
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
    """ROUND-2 FIX 2: '٣' (Arabic-Indic 3) -- which int() DOES parse to 3 --
    must NOT be silently accepted as a bound (the gate is ASCII-narrowed)."""
    assert rt._coerce_int("٣") is None
    assert rt.loop_max_defers({"max_defers": "٣"}) is None


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
    """ROUND-2 FIX 2: intake and runtime now agree -- '³' is flagged at intake
    AND coerces to None at runtime (neither silently accepts nor crashes)."""
    report = inv.check_contract_invariants(
        _invariant_contract(_invariant_loop(max_defers="³"))
    )
    assert any("max_defers" in e for e in report.errors)
    assert rt.loop_max_defers({"max_defers": "³"}) is None
    assert inv._coerce_nonneg_int("³") is None
    assert inv._coerce_nonneg_int("٣") is None  # Arabic-Indic also narrowed


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


# --- FIX 5: hash strips launch_intake.invariants; near-miss tightened ----------


def test_r2_fix5_invariants_block_stripped_from_contract_hash():
    """ROUND-2 FIX 5: ``launch_intake.invariants`` (run-derived validator
    findings) must be stripped from the canonical contract hash, mirroring
    ``launch_intake.completeness`` -- so two copies of the SAME contract that
    differ only in their invariants block hash IDENTICALLY.

    Mutation check (revert FIX 5 strip): leave invariants in the hashed payload
    and the two hashes diverge -- failing the equality assertion here.
    """
    base = _contract()
    with_clean = dict(base)
    with_clean["launch_intake"] = {"invariants": {"ok": True, "errors": [], "warnings": []}}
    with_findings = dict(base)
    with_findings["launch_intake"] = {
        "invariants": {"ok": False, "errors": ["something"], "warnings": ["a typo"]}
    }
    assert kb._business_contract_hash(with_clean) == kb._business_contract_hash(with_findings)
    # And a contract with NO launch_intake hashes the same as one whose only
    # launch_intake content is the (stripped) invariants block.
    assert kb._business_contract_hash(base) == kb._business_contract_hash(with_clean)


def test_r2_fix5_legacy_contract_with_common_max_key_hashes_identically():
    """ROUND-2 FIX 5 (effect): a legacy contract carrying a common ``max_*`` loop
    key (e.g. ``max_followers`` -- plausible in the ninaxfinds-growth context)
    must hash IDENTICALLY base-vs-HEAD. With the strip in place, even if a
    HEAD-only near-miss warning landed in the invariants block, it is stripped
    before hashing; AND the tightened near-miss no longer cries wolf on
    ``max_followers``/``max_depth``."""
    loop = _invariant_loop(max_followers=3, max_depth=2)
    contract = _invariant_contract(loop)
    report = inv.check_contract_invariants(contract)
    # The tightened near-miss must NOT flag these legit keys.
    assert not any("max_followers" in w for w in report.warnings)
    assert not any("max_depth" in w for w in report.warnings)
    # And a synthesized invariants block does not perturb the hash regardless.
    c1 = dict(contract)
    c1["launch_intake"] = {"invariants": report.as_dict()}
    c2 = dict(contract)  # no invariants block at all
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
        # FIX 1 belt-and-suspenders backfill: the verbatim mixed-case class was
        # lower-cased in place by the one-time UPDATE migration.
        assert migrated["side_effect_class"] == "external_irreversible"

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
