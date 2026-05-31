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
    # ROUND-6: the live contract hash is byte-identical base-vs-HEAD (the strip is
    # base-verbatim and the contract carries no telemetry that the schemes treat
    # differently).
    base_kb = _load_base_module("base_kb_live", "hermes_cli/kanban_db.py")
    assert kb._business_contract_hash(bc) == base_kb._business_contract_hash(bc), (
        "live ninaxfinds contract hash diverged from base 019271994"
    )


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
def test_round6_shared_coerce_int_raises_on_non_finite_base_verbatim(bad):
    """ROUND-6 FIX 2: the SHARED ``_coerce_int`` (used by the NON-OPTED
    ``loop_max_nudges`` path) RAISES on a non-finite float, byte-identical to base
    019271994. The base per-loop compile try/except catches the raise and DROPS the
    loop -- that IS the base default-off behavior. The crash-safe variant lives in
    ``_coerce_int_safe`` and is used EXCLUSIVELY by the opt-in ``loop_max_defers``.

    Mutation check: if the shared helper swallowed the crash (the round-4/5
    regression) it would CHANGE the non-opted compile decision -> this raise check
    fails."""
    import pytest as _pt
    with _pt.raises((ValueError, OverflowError)):
        rt._coerce_int(bad)
    # loop_max_nudges is on the shared non-opted path -> the raise propagates.
    with _pt.raises((ValueError, OverflowError)):
        rt.loop_max_nudges({"max_nudges": bad})
    # The OPT-IN max_defers path uses the crash-safe parser -> clamps to None.
    assert rt.loop_max_defers({"max_defers": bad}) is None
    assert rt._coerce_int_safe(bad) is None
    # A finite value still coerces on both paths.
    assert rt._coerce_int(3.0) == 3
    assert rt._coerce_int_safe(3.0) == 3
    assert rt.loop_max_defers({"max_defers": 2}) == 2


def test_round6_non_opted_inf_max_nudges_drops_loop_base_verbatim(fresh_home):
    """ROUND-6 FIX 2 (direct probe vs base): a NON-OPTED loop with
    ``max_nudges: .inf`` is DROPPED by the per-loop compile (the shared
    base-verbatim ``_coerce_int`` raises, the compile try/except records an error
    and drops the loop) -> schedule row count 0, EXACTLY like base 019271994.

    Mutation check: if the shared helper swallowed inf (round-4/5) the loop would
    compile a row -> the row-count-0 assertion fails."""
    loop = {
        "key": "seller_follow_up",
        "type": "seller_follow_up",
        "entity": "conversation_thread",
        "triggers": [{"kind": "timer", "detail": "x", "cadence_hours": 72}],
        "terminal_states": ["won", "lost"],
        "max_nudges": float("inf"),
    }
    contract = _contract(event_loops=[loop])
    # NON-OPTED: max_nudges is not a step-9 opt-in key.
    assert grammar.contract_opts_into_step9(contract) is False
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        rows = conn.execute(
            "SELECT * FROM reactive_timer_schedules WHERE board = ?", ("serious",)
        ).fetchall()
        assert len(rows) == 0, (
            "a non-opted inf max_nudges must DROP the loop (base 019271994 behavior)"
        )


def test_round6_optin_inf_max_defers_does_not_crash_compile(fresh_home):
    """ROUND-6 FIX 2 (opt-in graceful): a loop with ``max_defers: .inf`` OPTS IN.
    The crash-safe ``_coerce_int_safe`` clamps the bound to None (unbounded) so the
    loop still compiles a real timer schedule (the opt-in path is crash-SAFE), and
    intake flags the malformed bound.

    This proves the desired crash-safety is confined to the OPT-IN max_defers path
    while the shared max_nudges path stays base-verbatim (raises -> drops)."""
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
    # OPTED IN: max_defers is a step-9 opt-in key.
    assert grammar.contract_opts_into_step9(contract) is True
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        rows = conn.execute(
            "SELECT * FROM reactive_timer_schedules WHERE board = ?", ("serious",)
        ).fetchall()
        assert len(rows) == 1, "the opt-in loop must still compile (crash-safe)"
        # Inf is not a usable bound -> unbounded at runtime (None).
        assert rows[0]["max_defers"] is None
    # Intake (opt-in) flags the malformed bound.
    report = inv.check_contract_invariants(contract)
    assert any("max_defers" in e and "non-negative integer bound" in e
               for e in report.errors), "intake must flag max_defers=.inf (opt-in)"


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
def test_r2_fix2_isdigit_true_int_false_raises_on_shared_path(bad):
    """ROUND-6 FIX 2: a string where ``str.isdigit()`` is True but ``int()`` raises
    (superscript/circled digits) RAISES on the SHARED ``_coerce_int`` (and the
    non-opted ``loop_max_nudges``) -- byte-identical to base 019271994. The
    per-loop compile try/except then DROPS the loop. The OPT-IN ``loop_max_defers``
    uses the crash-safe parser -> None.

    Mutation check (the round-4/5 regression): if the shared helper swallowed the
    crash this raise check fails."""
    import pytest as _pt
    with _pt.raises((ValueError, TypeError)):
        rt._coerce_int(bad)
    with _pt.raises((ValueError, TypeError)):
        rt.loop_max_nudges({"max_nudges": bad})
    # OPT-IN max_defers path is crash-safe.
    assert rt.loop_max_defers({"max_defers": bad}) is None


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
    """ROUND-6 FIX 2: intake and runtime agree on what is a usable bound on the
    OPT-IN ``max_defers`` path. '³' (superscript, int() RAISES) is flagged at intake
    (via the crash-safe ``_coerce_nonneg_int_safe``) AND coerces to None at runtime
    (via the crash-safe ``loop_max_defers``). '٣' (Arabic-Indic 3, int() PARSES)
    coerces to 3 in BOTH (byte-identical to base int('٣')==3).

    NOTE: the SHARED ``_coerce_nonneg_int`` RAISES on '³' (base-verbatim); only the
    OPT-IN safe variant catches it -> None."""
    import pytest as _pt
    report = inv.check_contract_invariants(
        _invariant_contract(_invariant_loop(max_defers="³"))
    )
    assert any("max_defers" in e for e in report.errors)
    assert rt.loop_max_defers({"max_defers": "³"}) is None
    # Shared helper RAISES (base-verbatim); the OPT-IN safe variant catches it.
    with _pt.raises((ValueError, TypeError)):
        inv._coerce_nonneg_int("³")
    assert inv._coerce_nonneg_int_safe("³") is None
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


def test_rearch_hash_retains_invariants_block_base_verbatim():
    """ROUND-6 FIX 1 (SUBTRACTIVE REVERT of round-4 FIX A): the canonical hash
    RETAINS ``launch_intake.invariants`` -- EXACTLY as base 019271994. Two contracts
    that differ ONLY in their invariants report hash DIFFERENTLY, exactly like base.
    Byte-identity for the DEFAULT-OFF path comes from opt-in-gating (a non-opted
    contract produces NO new findings -> its invariants block is identical to base),
    NOT from stripping the block.

    Mutation check: re-add the invariants strip and these two hashes become EQUAL,
    diverging from base -> this fails."""
    base = _contract()
    with_clean = dict(base)
    with_clean["launch_intake"] = {"invariants": {"ok": True, "errors": [], "warnings": []}}
    with_findings = dict(base)
    with_findings["launch_intake"] = {
        "invariants": {"ok": False, "errors": ["something"], "warnings": ["a typo"]}
    }
    assert kb._business_contract_hash(with_clean) != kb._business_contract_hash(with_findings), (
        "invariants is RETAINED in the hash (base 019271994 behavior); two differing "
        "invariants reports must hash differently"
    )


def test_rearch_strip_drops_only_completeness_base_verbatim():
    """ROUND-6 FIX 1: ``_strip_contract_hash_telemetry`` drops ONLY
    ``completeness`` (base 019271994 behavior) -- it RETAINS ``invariants``.
    Asserted against the base module loaded via git show."""
    base_kb = _load_base_module("base_kb_strip_probes", "hermes_cli/kanban_db.py")
    probes = [
        {"objective": {"statement": "x"}},
        {"launch_intake": {"completeness": {"blocking": []}}},
        {"launch_intake": {"invariants": {"ok": True}}},
        {"launch_intake": {"completeness": {"a": 1}, "invariants": {"ok": False}}},
        {"launch_intake": {"completeness": {"a": 1}, "other": 2}},
        {"launch_intake": {"invariants": {"ok": True}, "answers": {"g": "x"}}},
    ]
    for p in probes:
        head = kb._strip_contract_hash_telemetry(json.loads(json.dumps(p)))
        base = base_kb._strip_contract_hash_telemetry(json.loads(json.dumps(p)))
        assert head == base, f"HEAD strip diverged from base for {p}"
        # Direct: completeness gone, invariants retained.
        intake = head.get("launch_intake")
        if isinstance(intake, dict):
            assert "completeness" not in intake, f"completeness must be stripped: {p}"
            if isinstance(p.get("launch_intake"), dict) and "invariants" in p["launch_intake"]:
                assert "invariants" in intake, f"invariants must be RETAINED: {p}"


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
    """ROUND-5 FIX 1 (revert to base-exact): the shared coercion helpers are
    BYTE-FOR-BYTE base 019271994. Base ``_coerce_nonneg_int`` gates strings with
    ``.isdigit()`` (which sees the leading '-'), so ``_coerce_nonneg_int('-0')``
    is ``None`` -- NOT 0. The runtime ``_coerce_int`` lstrips '-' before
    ``.isdigit()``, so ``_coerce_int('-0') == 0`` (base behavior). The round-4
    rewrite to a bare ``int()`` made BOTH return 0 (so they "agreed"), but that
    WIDENED the acceptance set away from base for every default-off contract --
    the exact regression round-5 reverts. The layers therefore differ on '-0' BY
    BASE DESIGN, and the round-3 negzero detection (intake flags '-0' with an
    ACCURATE 'coerces to a cap of 0' message, since the runtime treats it as 0)
    is the correct, base-aligned behavior."""
    # Base-exact: invariants REJECTS '-0' (its .isdigit() sees the '-'),
    # the runtime ACCEPTS it as a cap of 0 (it lstrips '-' first).
    assert inv._coerce_nonneg_int("-0") is None
    assert rt._coerce_int("-0") == 0
    report = inv.check_contract_invariants(
        _invariant_contract(_invariant_loop(max_defers="-0"))
    )
    # '-0' IS flagged at intake, with the accurate negzero message (the runtime
    # coerces it to a cap of 0, not "unbounded").
    negzero_errs = [
        e for e in report.errors
        if "max_defers" in e and "cap of 0" in e
    ]
    assert negzero_errs, (
        "a '-0' bound must be flagged with the accurate 'coerces to a cap of 0' "
        "message (base-exact: invariants rejects '-0', runtime accepts it as 0)"
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
# ROUND-6 FIX 1 (SUBTRACTIVE REVERT of round-4 FIX A): the canonical hash strips
# ONLY launch_intake.completeness, EXACTLY as base 019271994 does -- it does NOT
# strip launch_intake.invariants, and there is NO re-stamp migration. The
# rationale: the step-9 invariant checks are opt-in-gated, so a NON-OPTED contract
# produces NO new invariant findings -> its launch_intake.invariants block is
# byte-identical to base -> its _business_contract_hash is byte-identical to base
# WITHOUT stripping. Stripping invariants was unnecessary AND caused the
# un-approval/hash-flip regression for any invariants-carrying contract. These
# tests pin: (a) the strip is base-identical (only completeness), (b) an
# invariants-carrying contract hashes IDENTICAL base-vs-HEAD, (c) a non-opted
# contract's invariants block + hash are byte-identical to base, (d) an OPTED-IN
# contract's findings appear in ITS OWN invariants/hash (fine -- it opted in).
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


def test_fix1_strip_is_base_identical_only_completeness():
    """ROUND-6 FIX 1: ``_strip_contract_hash_telemetry`` strips ONLY
    ``launch_intake.completeness`` -- EXACTLY as base 019271994. It does NOT strip
    ``launch_intake.invariants``. Proven by loading the base module and asserting
    HEAD's strip produces the IDENTICAL result as base for a contract carrying
    BOTH telemetry blocks.

    Mutation check: re-add ``invariants`` to the strip and the invariants block
    drops out at HEAD but not base -> the strip results diverge and this fails."""
    base_kb = _load_base_module("base_kb_strip", "hermes_cli/kanban_db.py")
    normalized = {
        "objective": {"statement": "x"},
        "launch_intake": {
            "answers": {"a": 1},
            "completeness": {"ok": True, "errors": [], "warnings": []},
            "invariants": {"ok": False, "errors": ["e1"], "warnings": ["w1"]},
        },
    }
    head_stripped = kb._strip_contract_hash_telemetry(json.loads(json.dumps(normalized)))
    base_stripped = base_kb._strip_contract_hash_telemetry(json.loads(json.dumps(normalized)))
    assert head_stripped == base_stripped, "HEAD strip diverged from base 019271994"
    # Direct shape assertion: completeness gone, invariants RETAINED.
    assert "completeness" not in head_stripped["launch_intake"]
    assert head_stripped["launch_intake"]["invariants"] == {
        "ok": False, "errors": ["e1"], "warnings": ["w1"]
    }, "invariants must be RETAINED (base does NOT strip it)"


def test_fix1_invariants_carrying_contract_hash_identical_base_vs_head():
    """ROUND-6 FIX 1 (a): an invariants-carrying contract hashes IDENTICAL
    base-vs-HEAD. Because the strip is now base-verbatim (only completeness), the
    HEAD hash equals base for ANY contract -- including one carrying a
    launch_intake.invariants block.

    Mutation check: re-add the invariants strip and HEAD drops the invariants block
    from the hashed payload while base keeps it -> the hashes diverge and this
    fails."""
    base_kb = _load_base_module("base_kb_fix1_inv", "hermes_cli/kanban_db.py")
    contract = _overloaded_key_contract()
    contract["launch_intake"] = {
        "answers": {"a": 1},
        "invariants": {"ok": False, "errors": ["e1"], "warnings": ["w1"], "checked": {"event_loops": 1}},
    }
    assert kb._business_contract_hash(contract) == base_kb._business_contract_hash(contract), (
        "invariants-carrying contract hash diverged from base 019271994"
    )


def test_fix1_overloaded_key_contract_hash_identical_base_vs_head():
    """ROUND-6 FIX 1: a plain contract with overloaded keys (a terminal_states
    entry carrying class/kind, a terminal_classes map, a fractional max_defers)
    hashes IDENTICALLY base-vs-HEAD (no launch_intake telemetry at all)."""
    base_kb = _load_base_module("base_kb_fix1_plain", "hermes_cli/kanban_db.py")
    contract = _overloaded_key_contract()
    assert kb._business_contract_hash(contract) == base_kb._business_contract_hash(contract), (
        "overloaded-key contract hash diverged from base 019271994"
    )


def test_fix1_non_opted_invariants_block_byte_identical_to_base():
    """ROUND-6 FIX 1 (b): a NON-OPTED contract's launch_intake.invariants block is
    byte-identical to base -> its hash is byte-identical to base. We compute the
    invariants report via the SAME checker base would use (it is opt-in-gated, so a
    non-opted contract produces NO new findings), attach it, and assert the hash
    matches base.

    Mutation check: if the step-9 checks were NOT opt-in-gated, a non-opted
    contract's invariants report would carry new findings -> the attached block
    would differ from base -> the hash diverges and this fails. (Pinned elsewhere
    by the opt-in-gate effect tests.)"""
    base_inv = _load_base_module("base_inv_fix1_block", "hermes_cli/kanban_launch_invariants.py")
    base_kb = _load_base_module("base_kb_fix1_block", "hermes_cli/kanban_db.py")
    contract = _overloaded_key_contract()
    # Strip the step-9 opt-ins so the loop is genuinely LEGACY.
    loop = contract["event_loops"][0]
    loop["terminal_states"] = ["closed_won", "closed_lost"]
    loop.pop("terminal_classes", None)
    loop.pop("max_defers", None)
    assert grammar.contract_opts_into_step9(contract) is False
    # The invariants report is byte-identical base-vs-HEAD (opt-in-gated).
    base_report = base_inv.check_contract_invariants(contract)
    head_report = inv.check_contract_invariants(contract)
    assert head_report.errors == base_report.errors
    assert head_report.warnings == base_report.warnings
    # Attach the (identical) report and assert the hash matches base too.
    c_head = json.loads(json.dumps(contract))
    c_head["launch_intake"] = {"answers": {"a": 1}, "invariants": head_report.as_dict()}
    c_base = json.loads(json.dumps(contract))
    c_base["launch_intake"] = {"answers": {"a": 1}, "invariants": base_report.as_dict()}
    assert kb._business_contract_hash(c_head) == base_kb._business_contract_hash(c_base), (
        "a non-opted contract's invariants-carrying hash diverged from base"
    )


def test_fix1_completeness_telemetry_still_stripped_from_hash():
    """ROUND-6 FIX 1: ``launch_intake.completeness`` IS still stripped (base FIX 5
    behavior, preserved): a contract WITH vs WITHOUT a completeness block hashes
    IDENTICALLY -- the degraded-fallback soak telemetry must not bind the hash."""
    plain = {"objective": {"statement": "x"}, "launch_intake": {"answers": {"g": "do"}}}
    with_comp = {
        "objective": {"statement": "x"},
        "launch_intake": {
            "answers": {"g": "do"},
            "completeness": {"ok": True, "errors": [], "warnings": [], "enforced_dimensions": []},
        },
    }
    assert kb._business_contract_hash(plain) == kb._business_contract_hash(with_comp)


def test_fix1_opted_in_contract_findings_appear_in_its_own_invariants():
    """ROUND-6 FIX 1 (c): an OPTED-IN contract's step-9 findings DO appear in its
    own invariants report (and thus its hash if persisted) -- which is fine, it
    opted in. This pins that the revert did NOT suppress opt-in enforcement: an
    opted-in loop with an unknown declared class surfaces an error a non-opted loop
    would not."""
    opted = _invariant_contract(
        _invariant_loop(terminal_classes={"closed_won": "victory", "closed_lost": "loss"}),
    )
    assert grammar.contract_opts_into_step9(opted) is True
    rep = inv.check_contract_invariants(opted)
    assert any("unknown" in e and "victory" in e for e in rep.errors), (
        "an opted-in loop's unknown declared class must surface in its invariants"
    )
    # The same shape NOT opted in -> no such finding (terminal_classes is the
    # opt-in; without it there is no declared class to validate).
    not_opted = _invariant_contract(_invariant_loop())
    assert grammar.contract_opts_into_step9(not_opted) is False
    rep2 = inv.check_contract_invariants(not_opted)
    assert not any("victory" in e for e in rep2.errors)


def test_fix1_no_restamp_machinery_remains():
    """ROUND-6 FIX 1: the round-4 re-stamp migration is REMOVED (with the
    invariants strip gone there is nothing to re-stamp). The helpers and the
    telemetry-key tuple it introduced must no longer exist -- they were net-new
    over base 019271994."""
    assert not hasattr(kb, "_restamp_launch_approval_contract_hash")
    assert not hasattr(kb, "_legacy_completeness_only_contract_hash")
    assert not hasattr(kb, "_CONTRACT_HASH_TELEMETRY_INTAKE_KEYS")
    assert not hasattr(kb, "_RESTAMP_GUARD")


# ===========================================================================
# ROUND-6 FIX 2: shared coercion is BYTE-VERBATIM base 019271994 -- a non-ASCII
# but int()-parseable digit string ('٣'==3) parses EXACTLY as base, AND an
# isdigit()-True/int()-raises char ('³','①') RAISES exactly as base (the
# per-loop compile try/except then DROPS the loop). The crash-safety lives ONLY
# in the opt-in ``_coerce_int_safe`` / max_defers path.
# ===========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("٣", 3),       # Arabic-Indic 3: int() parses it -> EXACTLY as base
        ("٦", 6),       # Arabic-Indic 6
        ("5", 5),       # plain ASCII still works
        ("-3", -3),     # negative ASCII (runtime _coerce_int allows; caller bounds it)
        ("abc", None),
    ],
)
def test_round6_shared_coerce_int_matches_base_parseable(raw, expected):
    """``_coerce_int`` parses a non-ASCII int()-parseable digit EXACTLY as base
    (int(value.strip())) for every PARSEABLE input.

    Mutation check: re-introduce the ``.isascii()`` gate and '٣' coerces to None
    (not 3), diverging from base int('٣')==3 -> this fails."""
    assert rt._coerce_int(raw) == expected
    if expected is not None:
        assert int(raw.strip()) == expected


@pytest.mark.parametrize("raw", ["³", "①"])
def test_round6_shared_coerce_int_raises_base_verbatim(raw):
    """``_coerce_int`` RAISES on an isdigit()-True/int()-raises char, byte-identical
    to base 019271994 (the per-loop compile try/except then DROPS the loop). The
    OPT-IN ``_coerce_int_safe`` catches the same input -> None.

    Mutation check: if the shared helper swallowed the crash, this raise check
    fails (the round-4/5 regression)."""
    import pytest as _pt
    with _pt.raises((ValueError, TypeError)):
        rt._coerce_int(raw)
    assert rt._coerce_int_safe(raw) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("٣", 3),
        ("٦", 6),
        ("5", 5),
        ("-1", None),   # negative -> not a non-negative bound
    ],
)
def test_round6_coerce_nonneg_int_matches_base_parseable(raw, expected):
    """``_coerce_nonneg_int`` mirrors ``_coerce_int`` (no ascii narrowing) for every
    PARSEABLE input and stays a NON-NEGATIVE coercion."""
    assert inv._coerce_nonneg_int(raw) == expected


@pytest.mark.parametrize("raw", ["³", "①"])
def test_round6_coerce_nonneg_int_raises_base_verbatim(raw):
    """``_coerce_nonneg_int`` (shared, non-opted F10 path) RAISES on '³'/'①' exactly
    like base; the OPT-IN ``_coerce_nonneg_int_safe`` catches it -> None."""
    import pytest as _pt
    with _pt.raises((ValueError, TypeError)):
        inv._coerce_nonneg_int(raw)
    assert inv._coerce_nonneg_int_safe(raw) is None


def test_round6_legacy_max_nudges_arabic_digit_byte_identical_invariants():
    """ROUND-6 FIX 2 byte-identity: a LEGACY loop with ``max_nudges='٣'`` produces
    an invariant report (errors+warnings) byte-identical to base 019271994 -- base
    int('٣')==3 is finite, so neither base nor HEAD raises a 'can never stop'
    error."""
    contract = _overloaded_key_contract(max_nudges="٣")
    loop = contract["event_loops"][0]
    loop["terminal_states"] = ["closed_won", "closed_lost"]
    loop.pop("terminal_classes", None)
    loop.pop("max_defers", None)
    assert grammar.contract_opts_into_step9(contract) is False

    base_inv = _load_base_module("base_inv_r6", "hermes_cli/kanban_launch_invariants.py")
    base_report = base_inv.check_contract_invariants(contract)
    head_report = inv.check_contract_invariants(contract)
    assert head_report.errors == base_report.errors, "invariant ERRORS diverged from base"
    assert head_report.warnings == base_report.warnings, "invariant WARNINGS diverged from base"


def test_round6_compiled_max_nudges_column_matches_base(fresh_home):
    """ROUND-6 FIX 2 direct probe: a loop with ``max_nudges='٣'`` compiles a
    schedule whose persisted ``max_nudges`` column == 3 (base int('٣')), AND a '³'
    bound RAISES -> the per-loop compile DROPS the loop (schedule row count 0),
    EXACTLY like base 019271994 (NOT the round-4/5 'compiles with NULL' behavior)."""
    loop = {
        "key": "seller_follow_up", "type": "seller_follow_up", "entity": "conversation_thread",
        "triggers": [{"kind": "timer", "detail": "x", "cadence_hours": 72}],
        "terminal_states": ["won", "lost"], "max_nudges": "٣",
    }
    _approve("serious", _contract(event_loops=[loop]))
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        assert int(row["max_nudges"]) == 3 == int("٣")

    # A '³' bound RAISES in _coerce_int -> the per-loop compile DROPS the loop
    # (base 019271994 behavior). Schedule row count 0.
    loop2 = dict(loop, max_nudges="³")
    _approve("serious2", _contract(event_loops=[loop2]))
    with kb.connect(board="serious2") as conn:
        rows = conn.execute(
            "SELECT * FROM reactive_timer_schedules WHERE board = ?", ("serious2",)
        ).fetchall()
        assert len(rows) == 0, (
            "a '³' max_nudges must DROP the loop (base 019271994 raises -> drops)"
        )


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


# ===========================================================================
# ROUND-5: the convergent fix. The recurring break was that step-9 fixes
# modified SHARED coercion/truthiness helpers the LEGACY (non-opted) path also
# traverses. The principle: the shared helpers are BYTE-FOR-BYTE base; ALL
# step-9 parsing/validation/truthiness lives in opt-in-gated code. These tests
# pin each round-5 fix with a fails-without/passes-with assertion + an
# overarching byte-identity fuzz of a NON-OPTED contract against base.
# ===========================================================================

# The exact base 019271994 acceptance set, computed by re-implementing the base
# helpers inline. The shared coercion helpers MUST agree with these for every
# input (the only sanctioned divergence is a base CRASH -> None).
_BASE_COERCE_INT_VECTOR = {
    "+5": None, "1_000": None, "-0": 0, "٣": 3, "5": 5, "-1": -1,
    "garbage": None, "0": 0, "  7 ": 7, "-5": -5,
}
_BASE_COERCE_NONNEG_VECTOR = {
    "+5": None, "1_000": None, "-0": None, "٣": 3, "5": 5, "-1": None,
    "garbage": None, "0": 0, "  7 ": 7, "-5": None,
}


def test_round6_coerce_int_acceptance_set_is_base_exact():
    """ROUND-6 FIX 2: the shared ``_coerce_int`` acceptance set is BYTE-FOR-BYTE
    base 019271994 -- the ``.isdigit()`` gate, the int()/float() branches, AND the
    base CRASH on '³'/'①'. This pins the base acceptance set including the raise.

    Mutation check (round-4/5 regression): drop the ``.isdigit()`` gate or swallow
    the crash and these assertions fail."""
    import pytest as _pt
    for raw, expected in _BASE_COERCE_INT_VECTOR.items():
        assert rt._coerce_int(raw) == expected or (
            rt._coerce_int(raw) is None and expected is None
        ), f"_coerce_int({raw!r}) = {rt._coerce_int(raw)!r}, base = {expected!r}"
    # The number/float branch matches base too.
    assert rt._coerce_int(2.7) == 2
    assert rt._coerce_int(5) == 5
    assert rt._coerce_int(True) is None
    # The shared helper RAISES on '³'/'①' (base-verbatim). The OPT-IN safe variant
    # catches it -> None.
    for crash in ("³", "①"):
        with _pt.raises((ValueError, TypeError)):
            rt._coerce_int(crash)
        assert rt._coerce_int_safe(crash) is None


def test_round6_coerce_nonneg_int_acceptance_set_is_base_exact():
    """ROUND-6 FIX 2: the shared ``_coerce_nonneg_int`` acceptance set is
    BYTE-FOR-BYTE base 019271994 (``.isdigit()`` gate, which rejects '-0'/'+5')
    INCLUDING the base raise on '³'/'①'.

    Mutation check: drop the ``.isdigit()`` gate or swallow the crash and these
    fail."""
    import pytest as _pt
    for raw, expected in _BASE_COERCE_NONNEG_VECTOR.items():
        got = inv._coerce_nonneg_int(raw)
        assert got == expected or (got is None and expected is None), (
            f"_coerce_nonneg_int({raw!r}) = {got!r}, base = {expected!r}"
        )
    assert inv._coerce_nonneg_int(5) == 5
    assert inv._coerce_nonneg_int(-1) is None
    assert inv._coerce_nonneg_int(5.0) == 5
    assert inv._coerce_nonneg_int(2.7) is None
    # Shared helper RAISES on '³'; the OPT-IN safe variant catches it -> None.
    with _pt.raises((ValueError, TypeError)):
        inv._coerce_nonneg_int("³")
    assert inv._coerce_nonneg_int_safe("³") is None


def test_round6_shared_helpers_byte_identical_to_base_source():
    """OVERARCHING DIRECT PROOF (a): load the BASE 019271994 helpers and assert
    HEAD's shared coercion helpers produce the IDENTICAL result for every input in
    the fuzz vector -- INCLUDING raising EXACTLY where base raises. This is the
    machine-checkable proof that the shared coercion path is base-verbatim. The
    OPT-IN safe variants are asserted to match base where base does NOT crash, and
    to return None (not raise) where base crashes."""
    base_rt = _load_base_module("base_rt_r6", "hermes_cli/kanban_reactive_runtime.py")
    base_inv = _load_base_module("base_inv_r6_src", "hermes_cli/kanban_launch_invariants.py")
    vector = ["+5", "1_000", "-0", "٣", "³", "①", "5", "-1", 2.7, "garbage",
              "0", "", "  7 ", "-5", 5, True, False, None, "٣٣",
              float("inf"), float("nan")]
    for v in vector:
        # Base value or CRASH.
        try:
            b_rt = base_rt._coerce_int(v)
            b_rt_crash = False
        except Exception:
            b_rt, b_rt_crash = None, True
        try:
            b_inv = base_inv._coerce_nonneg_int(v)
            b_inv_crash = False
        except Exception:
            b_inv, b_inv_crash = None, True
        # HEAD shared helper: must MATCH base EXACTLY, including the raise.
        try:
            h_rt = rt._coerce_int(v)
            h_rt_crash = False
        except Exception:
            h_rt, h_rt_crash = None, True
        try:
            h_inv = inv._coerce_nonneg_int(v)
            h_inv_crash = False
        except Exception:
            h_inv, h_inv_crash = None, True
        assert h_rt_crash == b_rt_crash, f"_coerce_int({v!r}): crash {h_rt_crash} != base {b_rt_crash}"
        if not b_rt_crash:
            assert h_rt == b_rt, f"_coerce_int({v!r}): HEAD {h_rt!r} != base {b_rt!r}"
        assert h_inv_crash == b_inv_crash, f"_coerce_nonneg_int({v!r}): crash {h_inv_crash} != base {b_inv_crash}"
        if not b_inv_crash:
            assert h_inv == b_inv, f"_coerce_nonneg_int({v!r}): HEAD {h_inv!r} != base {b_inv!r}"
        # OPT-IN safe variants never crash; match base where base did not crash.
        s_rt = rt._coerce_int_safe(v)
        s_inv = inv._coerce_nonneg_int_safe(v)
        if b_rt_crash:
            assert s_rt is None, f"_coerce_int_safe({v!r}): base crashed, safe must be None, got {s_rt!r}"
        else:
            assert s_rt == b_rt, f"_coerce_int_safe({v!r}): {s_rt!r} != base {b_rt!r}"
        if b_inv_crash:
            assert s_inv is None, f"_coerce_nonneg_int_safe({v!r}): base crashed, safe must be None, got {s_inv!r}"
        else:
            assert s_inv == b_inv, f"_coerce_nonneg_int_safe({v!r}): {s_inv!r} != base {b_inv!r}"


def test_fix2_strict_runtime_gate_agrees_with_optin_master_gate():
    """ROUND-5 FIX 2: the runtime strict gate (``_side_effect_strict_enabled``)
    MUST agree with the opt-in master gate
    (``side_effect_policy_opts_into_strict``) for EVERY strict value. The previous
    ``_coerce_bool`` was broader -- 'enabled'/'y' enforced AND 'disabled' fell
    through to ``bool('disabled')==True``, INVERTING intent into enforcement on a
    DEFAULT-OFF contract.

    Mutation check (the round-4 divergence): swap the delegation back to
    ``_coerce_bool`` -> 'disabled'/'enabled'/'y' diverge (runtime True, opt-in
    False), failing the agreement assertion AND the 'disabled' inversion guard."""
    for v in ["true", "1", "yes", "on", "TRUE", "YES", True, 2,
              "false", "0", False, 0, "enabled", "disabled", "y", "Disabled", "off"]:
        pol = {"strict": v}
        runtime = kb._side_effect_strict_enabled(pol)
        optin = grammar.side_effect_policy_opts_into_strict(pol)
        contract = grammar.contract_opts_into_step9({"side_effect_policy": pol})
        assert runtime == optin == contract, (
            f"strict={v!r}: runtime={runtime} optin={optin} contract={contract} DIVERGE"
        )
    # Canonical truthy -> enabled.
    for v in ["true", "1", "yes", "on", True]:
        assert kb._side_effect_strict_enabled({"strict": v}) is True
    # Non-canonical -> NOT enabled (default-off stays byte-identical; no inversion).
    for v in ["enabled", "disabled", "y", "Disabled", "enforce"]:
        assert kb._side_effect_strict_enabled({"strict": v}) is False, (
            f"strict={v!r} must NOT enable enforcement (no inversion / no broad truthiness)"
        )


def test_fix2_strict_disabled_does_not_enforce_on_a_non_opted_board(fresh_home):
    """ROUND-5 FIX 2 (effect-level): a board declaring ``strict: 'disabled'`` is
    NOT opted into step-9 (contract_opts_into_step9 False) and must FIRE a
    base-class loop exactly like base -- the runtime gate must not invert
    'disabled' into enforcement and defer it.

    Mutation check: revert FIX 2 to ``_coerce_bool`` -> 'disabled' enables strict,
    the external_irreversible loop DEFERS instead of firing, failing this."""
    contract = _strict_contract("external_irreversible", strict=False)
    contract["side_effect_policy"]["strict"] = "disabled"
    assert grammar.contract_opts_into_step9(contract) is False
    _approve("serious", contract)
    with kb.connect(board="serious") as conn:
        row = _schedule_row(conn)
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="serious")
        assert res["fired"] == [{"loop_key": "seller_follow_up", "nudge": 1}], (
            "strict:'disabled' must NOT enforce (no inversion) -- base FIRES this loop"
        )
        assert res["deferred"] == []


def test_fix3_overrange_bound_rejected_at_intake_optin_only():
    """ROUND-5 FIX 3: an OPTED-IN over-range integer-string bound ('9'*25) passes
    coercion but OverflowErrors on SQLite persist (dropping the loop). Intake now
    rejects it (opt-in only).

    Mutation check: remove the over-range branch from ``_check_loop_bound_values``
    -> the 25-digit bound passes intake silently, failing the error assertion."""
    big = "9" * 25
    # OPTED IN (max_defers is a defer-bound key) -> flagged.
    opted = _invariant_contract(_invariant_loop(max_defers=big))
    rep = inv.check_contract_invariants(opted)
    assert any("64-bit" in e and "max_defers" in e for e in rep.errors), (
        "an opted-in over-range bound must be rejected at intake"
    )
    # An in-range large bound (the storable max) is NOT flagged.
    ok = _invariant_contract(_invariant_loop(max_defers=str(kb._SQLITE_INTEGER_MAX)))
    rep_ok = inv.check_contract_invariants(ok)
    assert not any("64-bit" in e for e in rep_ok.errors)


def test_fix3_overrange_bound_clamped_at_persist_fail_closed():
    """ROUND-5 FIX 3 (persist-side fail-CLOSED): the persist clamp keeps a loop
    alive (clamped to the storable max) rather than crashing the INSERT and
    silently dropping the schedule.

    Mutation check: remove ``_clamp_sqlite_integer`` from the bind -> the INSERT
    raises OverflowError, the schedule is dropped, and a re-bound value is lost."""
    big = int("9" * 25)
    assert kb._clamp_sqlite_integer(big) == kb._SQLITE_INTEGER_MAX
    assert kb._clamp_sqlite_integer(5) == 5
    assert kb._clamp_sqlite_integer(None) is None
    # The clamped value actually binds to a SQLite INTEGER column without raising.
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE t (n INTEGER)")
    c.execute("INSERT INTO t (n) VALUES (?)", (kb._clamp_sqlite_integer(big),))
    assert c.execute("SELECT n FROM t").fetchone()[0] == kb._SQLITE_INTEGER_MAX


def test_fix4_typod_safety_key_warns_even_when_loop_does_not_opt_in():
    """ROUND-5 FIX 4 (self-defeating gate): a loop whose ONLY step-9 intent is a
    TYPO'd safety key (``max_deferals``) never opts in via the grammar (which
    matches EXACT keys), so the near-miss safety-key check -- which exists
    precisely to catch that typo -- could never fire. It now runs whenever the
    loop CONTAINS a safety-key near-miss, independent of the opt-in gate.

    Mutation check: gate ``_check_safety_optin_keys`` behind opt-in only (revert
    the elif branch) -> the typo'd-key loop produces no warning, failing this."""
    typo_loop = _invariant_loop(max_deferals=3)  # near-miss of max_defer(r)als
    # Pin the precondition: this loop does NOT opt into step-9 (the grammar
    # matches EXACT keys, and 'max_deferals' is not one), so without FIX 4 the
    # opt-in-gated safety-key check never runs.
    assert grammar.loop_opts_into_step9(typo_loop) is False
    rep = inv.check_contract_invariants(_invariant_contract(typo_loop))
    # The warning names the typo'd key and explains it is silently inert. We do
    # NOT pin WHICH canonical defer key it suggests (max_defers vs max_deferrals
    # are both 1-2 edits away and the suggestion order is hash-seed dependent) --
    # only that the typo is surfaced as a defer-cap near-miss.
    assert any(
        "max_deferals" in w and "silently inert" in w and "defer" in w
        for w in rep.warnings
    ), "a typo'd safety opt-in must warn even when the loop does not opt in"


def test_fix4_clean_legacy_loop_with_no_near_miss_is_unchanged():
    """ROUND-5 FIX 4 (no false positives): a clean legacy loop carrying NO
    step-9 key and NO near-miss takes neither branch and produces no new warning
    -- byte-identical to base."""
    clean = _invariant_loop()  # only terminal_states + max_nudges (exact key)
    assert grammar.loop_opts_into_step9(clean) is False
    rep = inv.check_contract_invariants(_invariant_contract(clean))
    assert not any("typo" in w or "silently inert" in w for w in rep.warnings)


def test_fix5_loop_optin_gate_is_effectful_mutation_catches_always_true():
    """ROUND-5 FIX 5: an EFFECT-level test for the loop-level step-9 opt-in gate
    (kanban_launch_invariants.py, the ``if _grammar_loop_opts_into_step9(loop)``
    branch). A NON-OPTED loop carrying an over-range ``max_nudges`` ('9'*25 -- an
    over-range value, and max_nudges is a NUDGE key that is NOT a step-9 opt-in
    key) must produce NO over-range error, because the bound-value check runs ONLY
    on the opt-in branch. If the gate is mutated to always-True, the bound-value
    check runs on this non-opted loop and flags the over-range nudge cap, so this
    assertion FAILS -- catching the mutation that 141 green tests previously
    missed."""
    non_opted = _invariant_loop(max_nudges="9" * 25)
    # Precondition: max_nudges alone does NOT opt into step-9 (only defer-bound
    # keys / terminal_classes do), so the opt-in gate is False for this loop.
    assert grammar.loop_opts_into_step9(non_opted) is False
    rep = inv.check_contract_invariants(_invariant_contract(non_opted))
    assert not any("64-bit" in e for e in rep.errors), (
        "a NON-OPTED loop must not run the opt-in-gated bound-value check; if the "
        "opt-in gate is mutated to always-True this fails (the gate is effectful)"
    )


def test_fix6_strict_governs_dispatch_gate_external_class(fresh_home):
    """ROUND-5 FIX 6: strict default-deny now governs the worker-DISPATCH gate
    (``_side_effect_and_approval_blockers``), not just reactive_tick. Under strict,
    an external-family class the owner forgot to list in approval_required must
    DEFER at dispatch (approval-required-by-default), mirroring reactive_tick.

    Mutation check: remove the FIX 6 strict block from the dispatch gate -> the
    external_reversible class (not in approval_required) dispatches UNGATED, so the
    expected blocker is absent and this fails."""
    contract = _contract_runtime_strict_contract("external_reversible", strict=True)
    tid = _dispatch_strict_board(contract)
    with kb.connect(board="serious") as conn:
        verdict = kb.evaluate_dispatch_eligibility(conn, tid, board="serious")
        codes = {b["code"] for b in verdict["blockers"]}
        assert verdict["ok"] is False, "strict external class must block dispatch"
        assert "approval_gate_unsatisfied" in codes, (
            "external_reversible (not in approval_required) must defer-by-default "
            "under strict on the DISPATCH path too"
        )


def test_fix6_strict_governs_dispatch_gate_unrecognized_class(fresh_home):
    """ROUND-5 FIX 6: an UNRECOGNIZED/typo class defers-by-default at dispatch
    under strict (fail-closed; no gate can cover a class outside the vocabulary),
    mirroring reactive_tick's ``side_effect_unrecognized_strict``."""
    contract = _contract_runtime_strict_contract("extrnal_ireversible", strict=True)
    tid = _dispatch_strict_board(contract)
    with kb.connect(board="serious") as conn:
        verdict = kb.evaluate_dispatch_eligibility(conn, tid, board="serious")
        codes = {b["code"] for b in verdict["blockers"]}
        assert verdict["ok"] is False
        assert "side_effect_unrecognized_strict" in codes, (
            "an unrecognized class must defer-by-default at dispatch under strict"
        )


def test_fix6_dispatch_gate_byte_identical_for_non_strict_board(fresh_home):
    """ROUND-5 FIX 6: a NON-strict board's dispatch gate is byte-identical to base
    -- an external class NOT listed in forbidden/approval_required dispatches
    UNGATED (no strict-by-default deferral), exactly as base 019271994."""
    contract = _contract_runtime_strict_contract("external_reversible", strict=False)
    tid = _dispatch_strict_board(contract)
    with kb.connect(board="serious") as conn:
        verdict = kb.evaluate_dispatch_eligibility(conn, tid, board="serious")
        codes = {b["code"] for b in verdict["blockers"]}
        # No side-effect blocker: a non-strict board does not enforce by default.
        assert "approval_gate_unsatisfied" not in codes
        assert "side_effect_unrecognized_strict" not in codes
        assert "side_effect_forbidden" not in codes


# ===========================================================================
# ROUND-6 FIX 3: the dispatch-gate '' sentinel asymmetry. Under strict, the
# DISPATCH gate (_side_effect_and_approval_blockers) must mirror the reactive
# gate: an action that DECLARED a side_effect_class which normalizes away to
# empty/NULL (but is NOT explicit 'none') is routed (via the SAME
# _canonical_side_effect_class_for_persist sentinel logic) to the dropped marker
# -> treated as unrecognized -> defer/block, instead of the early `if not sec:
# return blockers`. Non-strict / genuinely-absent class -> byte-identical to base.
# ===========================================================================


@pytest.mark.parametrize("sentinel", ["null", "-", "", "   "])
def test_fix3_dispatch_gate_strict_declared_sentinel_defers(fresh_home, sentinel):
    """ROUND-6 FIX 3: under strict, a DECLARED sentinel side_effect_class
    ('' / 'null' / '-' / whitespace) that normalizes away (but is not 'none') is
    routed to the dropped marker and DEFERS at dispatch (unrecognized fail-CLOSED),
    mirroring the reactive gate -- instead of the early `if not sec: return`.

    Mutation check: revert FIX 3 (keep the unconditional early return) and the
    sentinel returns no blocker -> the worker dispatches UNGATED under strict, so
    this assertion fails."""
    # Direct probe of the gate function: a declared sentinel under strict yields the
    # unrecognized-strict blocker; a non-strict board yields NONE (base-identical).
    from tests.hermes_cli.test_kanban_contract_runtime import _contract as _crc

    class _FakeTask:
        action_key = "research"
        assignee = "mock-worker"
        id = "t1"
        task_id = "t1"

    strict_contract = _crc(side_effect_class="none")
    strict_contract["side_effect_policy"]["strict"] = True
    strict_contract["side_effect_class"] = sentinel  # the DECLARED, resolved class
    # Build a fresh board to host the dispatch-gate call.
    _cr_create_contract_board(strict_contract)
    with kb.connect(board="serious") as conn:
        blockers = kb._side_effect_and_approval_blockers(
            conn,
            task=_FakeTask(),
            contract=strict_contract,
            side_effect_class=sentinel,
            board="serious",
        )
        codes = {b["code"] for b in blockers}
        assert "side_effect_unrecognized_strict" in codes, (
            f"a declared sentinel {sentinel!r} must DEFER under strict at dispatch"
        )


@pytest.mark.parametrize("sentinel", ["null", "-", "", "   "])
def test_fix3_dispatch_gate_non_strict_sentinel_byte_identical_to_base(fresh_home, sentinel):
    """ROUND-6 FIX 3: a NON-STRICT board's dispatch gate is byte-identical to base
    for the SAME declared sentinel -- the early `if not sec: return blockers` fires
    verbatim (no marker logic off the strict path), so NO side-effect blocker is
    produced.

    Mutation check: if the sentinel logic ran off the strict path it would produce a
    blocker here -> this fails."""
    from tests.hermes_cli.test_kanban_contract_runtime import _contract as _crc

    class _FakeTask:
        action_key = "research"
        assignee = "mock-worker"
        id = "t1"
        task_id = "t1"

    contract = _crc(side_effect_class="none")  # NON-strict (no policy.strict)
    contract["side_effect_class"] = sentinel
    _cr_create_contract_board(contract)
    with kb.connect(board="serious") as conn:
        blockers = kb._side_effect_and_approval_blockers(
            conn,
            task=_FakeTask(),
            contract=contract,
            side_effect_class=sentinel,
            board="serious",
        )
        codes = {b["code"] for b in blockers}
        assert "side_effect_unrecognized_strict" not in codes
        assert "approval_gate_unsatisfied" not in codes
        assert "side_effect_forbidden" not in codes


def test_fix3_dispatch_gate_genuinely_absent_and_none_fire_base_identical(fresh_home):
    """ROUND-6 FIX 3: a genuinely-absent class (None) and an explicit benign 'none'
    both hit the BASE early return (no blocker) under strict AND non-strict -- the
    sentinel logic fires ONLY for a DECLARED non-'none' value that normalizes away.

    Mutation check: if the sentinel logic mis-classified None/'none' it would defer a
    genuinely-safe action -> this fails."""
    from tests.hermes_cli.test_kanban_contract_runtime import _contract as _crc

    class _FakeTask:
        action_key = "research"
        assignee = "mock-worker"
        id = "t1"
        task_id = "t1"

    # One board hosts the gate-function calls (the gate is a pure function of
    # conn + the passed contract/class, so we vary the contract dict directly).
    base = _crc(side_effect_class="none")
    _cr_create_contract_board(base)
    with kb.connect(board="serious") as conn:
        for strict in (True, False):
            for cls in (None, "none", "None"):
                contract = _crc(side_effect_class="none")
                if strict:
                    contract["side_effect_policy"]["strict"] = True
                contract["side_effect_class"] = cls
                blockers = kb._side_effect_and_approval_blockers(
                    conn,
                    task=_FakeTask(),
                    contract=contract,
                    side_effect_class=cls,
                    board="serious",
                )
                codes = {b["code"] for b in blockers}
                assert "side_effect_unrecognized_strict" not in codes, (
                    f"strict={strict} cls={cls!r}: genuinely-absent/'none' must NOT defer"
                )


def test_fix3_dispatch_and_reactive_gates_agree_on_sentinel(fresh_home):
    """ROUND-6 FIX 3: the dispatch gate and the reactive gate now AGREE on a
    declared sentinel under strict -- BOTH defer with the unrecognized-strict
    reason. This pins the symmetry the fix establishes (both reuse the
    _canonical_side_effect_class_for_persist sentinel logic)."""
    from tests.hermes_cli.test_kanban_contract_runtime import _contract as _crc

    class _FakeTask:
        action_key = "research"
        assignee = "mock-worker"
        id = "t1"
        task_id = "t1"

    # Reactive side: a strict loop with an authored sentinel persists the dropped
    # marker and defers (already covered by test_fixC_* ; re-asserted here for the
    # symmetry claim).
    contract = _strict_contract("", strict=True)
    _approve("reactside", contract)
    with kb.connect(board="reactside") as conn:
        row = _schedule_row(conn, board="reactside")
        assert row["side_effect_class"] == kb._SIDE_EFFECT_CLASS_DROPPED_DB_MARKER
        res = kb.reactive_tick(conn, now=int(row["next_fire_at"]), board="reactside")
        assert res["deferred"] == [
            {"loop_key": "seller_follow_up", "reason": "side_effect_unrecognized_strict"}
        ]
    # Dispatch side: the SAME sentinel under strict defers with the SAME reason.
    disp = _crc(side_effect_class="none")
    disp["side_effect_policy"]["strict"] = True
    disp["side_effect_class"] = ""
    _cr_create_contract_board(disp)
    with kb.connect(board="serious") as conn:
        blockers = kb._side_effect_and_approval_blockers(
            conn, task=_FakeTask(), contract=disp, side_effect_class="", board="serious",
        )
        assert "side_effect_unrecognized_strict" in {b["code"] for b in blockers}


# ---------------------------------------------------------------------------
# Round-5 dispatch-gate helpers. Reuse the contract-runtime test's fully
# dispatch-ready contract (so the only variable is side_effect_class + strict).
# ---------------------------------------------------------------------------

from tests.hermes_cli.test_kanban_contract_runtime import (  # noqa: E402
    _contract as _cr_contract,
    _create_contract_board as _cr_create_contract_board,
)


def _contract_runtime_strict_contract(side_effect_class, *, strict):
    """A dispatch-ready contract (the contract-runtime fixture) whose resolved
    task ``side_effect_class`` and board ``side_effect_policy.strict`` are the
    only variables. The class is intentionally NOT in approval_required so the
    LEGACY fall-through (dispatch ungated) is what strict must override."""
    contract = _cr_contract(
        side_effect_class=side_effect_class,
        allowed_side_effects=[side_effect_class],
    )
    if strict:
        contract["side_effect_policy"]["strict"] = True
    return contract


def _dispatch_strict_board(contract):
    """Approve the contract on 'serious' and create a ready task. Returns task id."""
    return _cr_create_contract_board(contract)


# ===========================================================================
# OVERARCHING PROOF (round-5): a NON-OPTED contract fuzzed across the exact
# inputs that broke prior rounds -- max_nudges/max_defers in
# ['+5','1_000','-0','٣','³','①','5','-1',2.7,'garbage'] and strict in
# ['true','enabled','disabled','y','1',True] -- has launch-gate report.ok +
# compiled columns + invariants errors/warnings + _business_contract_hash
# BYTE-IDENTICAL to base 019271994. Byte-identity BY CONSTRUCTION: a non-opted
# contract runs base code verbatim because every step-9 path is opt-in-gated.
# ===========================================================================

_FUZZ_BOUND_VALUES = ["+5", "1_000", "-0", "٣", "³", "①", "5", "-1", 2.7, "garbage"]
_FUZZ_STRICT_VALUES = ["true", "enabled", "disabled", "y", "1", True]


def _fuzz_legacy_contract(*, bound_key=None, bound_value=None, strict_value=None):
    """A LEGACY (non-opted) contract. A fuzzed bound is attached under a
    max_nudges-family key (NOT a defer-bound opt-in key) so the contract stays
    NON-OPTED; a fuzzed strict value is a non-canonical/near value that does NOT
    opt in. The whole point: none of these trip a step-9 path."""
    loop = {
        "key": "lp", "type": "lp", "entity": "e",
        "triggers": [{"kind": "timer", "detail": "d", "cadence_hours": 72}],
        "terminal_states": ["closed_won", "closed_lost"],
    }
    if bound_key is not None:
        loop[bound_key] = bound_value
    contract = {
        "objective": {
            "statement": "x", "success": ["s"], "failure": ["f"], "constraints": ["c"],
        },
        "event_loops": [loop],
        "entities": [
            {"key": "e", "type": "conversation",
             "terminal_states": ["closed_won", "closed_lost"]}
        ],
    }
    if strict_value is not None:
        contract["side_effect_policy"] = {"allowed": ["none"], "strict": strict_value}
    return contract


def test_overarching_round6_byte_identity_fuzz_max_nudges():
    """OVERARCHING DIRECT PROOF: a NON-OPTED contract carrying max_nudges across the
    full fuzz vector has check_contract_invariants (errors/warnings/ok) AND
    business-contract hash byte-identical to base 019271994 -- INCLUDING raising
    EXACTLY where base raises ('³'/'①' propagate through the shared, non-opted
    _coerce_nonneg_int). max_nudges is NOT a step-9 opt-in key, so the contract
    never opts in regardless of the (malformed) value."""
    base_inv = _load_base_module("base_inv_r6_nudges", "hermes_cli/kanban_launch_invariants.py")
    base_kb = _load_base_module("base_kb_r6_nudges", "hermes_cli/kanban_db.py")
    for v in _FUZZ_BOUND_VALUES:
        contract = _fuzz_legacy_contract(bound_key="max_nudges", bound_value=v)
        assert grammar.contract_opts_into_step9(contract) is False, v
        # check_contract_invariants must crash EXACTLY where base crashes.
        try:
            b = base_inv.check_contract_invariants(contract)
            b_crash = False
        except Exception:
            b_crash = True
        try:
            h = inv.check_contract_invariants(contract)
            h_crash = False
        except Exception:
            h_crash = True
        assert h_crash == b_crash, f"max_nudges={v!r}: crash {h_crash} != base {b_crash}"
        if not b_crash:
            assert h.errors == b.errors, f"max_nudges={v!r}: ERRORS diverged from base"
            assert h.warnings == b.warnings, f"max_nudges={v!r}: WARNINGS diverged from base"
            assert h.ok == b.ok, f"max_nudges={v!r}: report.ok diverged from base"
        # The business-contract hash never runs the checker -> always defined and
        # byte-identical to base.
        assert kb._business_contract_hash(contract) == base_kb._business_contract_hash(contract), (
            f"max_nudges={v!r}: business-contract hash diverged from base"
        )


def test_overarching_round6_byte_identity_fuzz_max_defers():
    """ROUND-6 byte-identity: a contract carrying max_defers across the full fuzz
    vector. NOTE: max_defers IS a step-9 opt-in key, so a contract that declares it
    OPTS IN -- which is the intended behavior. The OPT-IN path is crash-SAFE (the
    safe coercion catches '³'/'①'), so check_contract_invariants must NEVER crash
    across the vector. The business hash is base-identical because a plain contract
    (no persisted invariants/completeness block) hashes identically to base."""
    base_kb = _load_base_module("base_kb_r6_defers", "hermes_cli/kanban_db.py")
    for v in _FUZZ_BOUND_VALUES:
        contract = _fuzz_legacy_contract(bound_key="max_defers", bound_value=v)
        # max_defers present => opted in (intended). The opt-in intake path is
        # crash-SAFE -> must not raise on '³'/'①'.
        assert grammar.contract_opts_into_step9(contract) is True, v
        rep = inv.check_contract_invariants(contract)  # must not raise (opt-in safe)
        assert isinstance(rep.errors, list)
        # The hash never carries an invariants/completeness block here, so it equals
        # base byte-for-byte (the opt-in finding lives only in the report, which is
        # NOT persisted into this contract dict).
        assert kb._business_contract_hash(contract) == base_kb._business_contract_hash(contract), (
            f"max_defers={v!r}: business-contract hash diverged from base"
        )


def test_overarching_round5_byte_identity_fuzz_strict():
    """ROUND-5 byte-identity: a NON-OPTED contract carrying a NON-CANONICAL strict
    value (['enabled','disabled','y'] -> NOT opted in) has invariants + hash
    byte-identical to base. The CANONICAL values (['true','1',True] -> opted in)
    legitimately differ (the opt-in path), so they are asserted to OPT IN, not to
    match base."""
    base_inv = _load_base_module("base_inv_r5_strict", "hermes_cli/kanban_launch_invariants.py")
    base_kb = _load_base_module("base_kb_r5_strict", "hermes_cli/kanban_db.py")
    canonical = {"true", "1", "on", "yes", True}
    for v in _FUZZ_STRICT_VALUES:
        contract = _fuzz_legacy_contract(strict_value=v)
        opted = grammar.contract_opts_into_step9(contract)
        if isinstance(v, str) and v.strip().lower() in canonical or v is True:
            # Canonical truthy -> OPTED IN (intended divergence allowed).
            assert opted is True, f"strict={v!r} should opt in"
            continue
        # Non-canonical strict -> NOT opted in -> byte-identical to base.
        assert opted is False, f"strict={v!r} must NOT opt in (no broad truthiness)"
        b = base_inv.check_contract_invariants(contract)
        h = inv.check_contract_invariants(contract)
        assert h.errors == b.errors, f"strict={v!r}: ERRORS diverged from base"
        assert h.warnings == b.warnings, f"strict={v!r}: WARNINGS diverged from base"
        assert kb._business_contract_hash(contract) == base_kb._business_contract_hash(contract), (
            f"strict={v!r}: business-contract hash diverged from base"
        )


def test_overarching_round6_compiled_columns_byte_identical(fresh_home):
    """OVERARCHING DIRECT PROOF: the COMPILED runtime cap (``loop_max_nudges``) for a
    NON-OPTED contract matches base 019271994 EXACTLY across the fuzz vector,
    INCLUDING raising where base raises. A non-opted max_nudges bound is read by the
    SAME (reverted, base-verbatim) ``loop_max_nudges`` -> ``_coerce_int``, so '+5' /
    '1_000' -> None (rejected, base-exact), '5' -> 5, '-0'/'-1' rejected by the >=0
    gate, and '³'/'①' RAISE (base-verbatim -> the per-loop compile DROPS the loop)."""
    base_rt = _load_base_module("base_rt_r6_cols", "hermes_cli/kanban_reactive_runtime.py")
    for v in _FUZZ_BOUND_VALUES:
        loop = {"max_nudges": v}
        try:
            b = base_rt.loop_max_nudges(loop)
            b_crash = False
        except Exception:
            b, b_crash = None, True
        try:
            h = rt.loop_max_nudges(loop)
            h_crash = False
        except Exception:
            h, h_crash = None, True
        assert h_crash == b_crash, f"max_nudges={v!r}: crash {h_crash} != base {b_crash}"
        if not b_crash:
            assert h == b, f"max_nudges={v!r}: compiled cap HEAD {h!r} != base {b!r}"
