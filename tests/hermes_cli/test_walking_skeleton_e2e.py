"""STANDING walking-skeleton e2e: the compiler-for-intent loop under REAL enforcement.

This is the keystone proof that the runtime-hardening flags actually hold together
end-to-end on a scratch board -- "test the EFFECT, not the paperwork" applied to the
WHOLE loop. Every test asserts an OBSERVABLE effect (gate.ok, blocker codes,
board_knob_audit rows, board_signals rows, reward_value, task status), never a mock
of the thing under test or an argv-text string-match of the seam itself.

The four hardening flags exercised (all DEFAULT-OFF; opt-in only):
  * HERMES_LAUNCH_COMPLETENESS_ENFORCE=all  -- the unified LaunchCompletenessSpec
    is consulted by board_dispatch_gate; an ENFORCED dimension finding becomes a
    distinct ``launch_completeness_failed`` blocker.
  * HERMES_KANBAN_REQUIRE_CONTRACT_DEFAULTS=1 -- an ABSENT require_* on a managed
    board fails closed. The sound contract declares them explicitly, so this stays
    a no-op for it (proven via contract_defaults_applied == []).
  * HERMES_KANBAN_ENFORCE_WORKER_TOOLSETS=1 -- the spawned worker is constrained to
    the declared worker_envelope toolsets (proven by PARSING the real spawn argv,
    not string-matching it -- the chat subparser nulls a mis-placed flag).
  * HERMES_KANBAN_BRIDGE_TOOL_POLICY=1 -- the contract's
    runtime.tool_policy.blocked_tools is bridged into the live action gate (proven
    by check_action_gate actually blocking the tool).

SOUND-CONTRACT SHAPE (what it took to pass enforce=all -- see _sound_contract):
  * objective.success is a TYPED scoreable entry {metric, comparator, target,
    data_source} -> success_scoreability.
  * EVERY workflow action (incl. the terminal stage's ``archive``) declares a
    side_effect_class from the canonical vocab {none, internal, external_reversible,
    external_irreversible, financial} -> side_effect_class_coverage.
  * the conversational event_loop declares a timer trigger WITH cadence_hours AND an
    inbound trigger, terminal_states ['closed_won','closed_lost'] (a declared WIN +
    LOSS, distinct), and a finite max_nudges -> event_loop_termination, win_signal_rail,
    distinct_terminals.
  * stages form a reachable graph to a flagged terminal ``closed`` stage with distinct
    terminals -> stage_reachability.
  * exit evidence key ``mock_report`` is produced (proof_requirements + action
    required_proof + worker_envelope required_proof) -> evidence_namespace.
  * tunable ``follow_up_interval_hours`` is an optimizer-managed knob -> tunable_consumer_binding.
  * worker_envelope declares toolsets + capabilities + allowed_side_effects;
    runtime declares provider_policy, require_worker_envelopes/provider_policy,
    workflow.require_semantics, runtime.tool_policy.blocked_tools.
  * no objective.budget / definition_of_done declared -> advisory_intent_gap is a
    strict no-op; no embedded intake corpus -> answer_coverage is a strict no-op;
    structural invariants pass -> invariants.

WHY scenario 2 would CATCH an enforcement revert (sanity-check of the design):
  Each broken contract is APPROVED under report-mode (enforce off) so the board is
  genuinely live, THEN the dimension is flipped to enforce. If enforcement were
  reverted (the gate no longer consulted the spec / the enforced-dimension findings
  no longer promoted to errors), board_dispatch_gate would return ok=True and NO
  ``launch_completeness_failed`` blocker -- exactly the assertions that would then
  FAIL. The test is therefore pass-iff-enforced: it cannot pass when broken unless
  the gate truly fails closed.
"""

from __future__ import annotations

import copy
import importlib.util
import pathlib
import sys

import pytest

_WORKTREE = pathlib.Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli.launch_completeness import DIMENSIONS, assess_launch_completeness

# Reuse the canonical fresh_home + contract approval helpers from the existing
# contract-runtime suite (imported via importlib exactly like
# test_launch_completeness_gate.py does), so this standing test stays in lockstep
# with the suite it certifies.
_crt_spec = importlib.util.spec_from_file_location(
    "_ws_crt_helpers", pathlib.Path(__file__).with_name("test_kanban_contract_runtime.py")
)
_crt = importlib.util.module_from_spec(_crt_spec)
_crt_spec.loader.exec_module(_crt)
_approve_contract_board = _crt._approve_contract_board


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Hermetic per-test HERMES_HOME with every hardening flag CLEARED.

    Mirrors the fresh_home in test_kanban_contract_runtime / test_per_task_failclosed
    so a flag leaked from another test (or a stale config root) can never make this
    proof pass-when-broken. ``Path.home`` is pinned so config.yaml resolution is
    deterministic, matching test_per_task_failclosed.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_LAUNCH_COMPLETENESS_ENFORCE",
        "HERMES_KANBAN_REQUIRE_CONTRACT_DEFAULTS",
        "HERMES_KANBAN_ENFORCE_WORKER_TOOLSETS",
        "HERMES_KANBAN_BRIDGE_TOOL_POLICY",
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
# The structurally-sound managed contract that passes EVERY enforce=all dimension.
# ---------------------------------------------------------------------------


def _sound_contract() -> dict:
    """A managed company-mode contract that reaches ok=True under
    ``assess_launch_completeness(enforce=set(DIMENSIONS))`` AND passes the
    board-level launch readiness presence checks (so it APPROVES active).

    The final shape was derived by iterating against
    ``assess_launch_completeness(contract, enforce=set(DIMENSIONS))``; the ONLY
    delta from the existing reactive-runtime fixture needed for all-green was
    declaring ``side_effect_class`` on the terminal stage's ``archive`` action
    (default-DENY: every action must carry a class, never an implicit 'none').
    """
    return {
        "objective": {
            "statement": "Drive watched leads to a closed-won outcome with proof.",
            # TYPED, machine-gradeable success: {metric, comparator, target,
            # data_source} -> success_scoreability passes without prose heuristics.
            "success": [
                {
                    "metric": "closed_won_count",
                    "comparator": ">=",
                    "target": 10,
                    "data_source": "kanban",
                }
            ],
            "failure": ["a watcher fires forever or drops external state"],
            "constraints": ["no real outbound services"],
        },
        "runtime": {
            "mode": "company",
            "dispatcher": {"profile": "mock-ceo"},
            "profiles": {
                "ceo": "mock-ceo",
                "optimizer": "mock-optimizer",
                "worker": "mock-worker",
            },
            # require_* declared EXPLICITLY -> REQUIRE_CONTRACT_DEFAULTS finds
            # nothing absent to promote (contract_defaults_applied stays []).
            "require_worker_envelopes": True,
            "require_provider_policy": True,
            "provider_policy": {"mock_research": {"provider": "fixture", "tool": "mock"}},
            # bridged into the action gate when HERMES_KANBAN_BRIDGE_TOOL_POLICY=1.
            "tool_policy": {"blocked_tools": ["send_message"]},
            "worker_envelopes": {
                "mock-worker": {
                    "capabilities": ["mock_research"],
                    "toolsets": ["kanban"],
                    "allowed_side_effects": ["none"],
                    "required_proof": ["mock_report"],
                }
            },
        },
        "workflow": {
            "id": "skeleton-flow",
            "goal_id": "mock-goal",
            "require_semantics": True,
            "workstreams": [{"key": "ops", "stages": ["execute", "closed"]}],
            "stages": [
                {
                    "key": "execute",
                    "actions": [
                        {
                            "key": "research",
                            "required_capabilities": ["mock_research"],
                            "required_toolsets": ["kanban"],
                            "required_proof": ["mock_report"],
                            "side_effect_class": "none",
                        }
                    ],
                    "triggers": [
                        {"kind": "timer", "key": "contract_tick", "cadence_hours": 72}
                    ],
                    # produced evidence key is namespaced (mock_report is produced
                    # by proof_requirements + action + envelope) -> evidence_namespace.
                    "exit_criteria": [
                        {"transition": "closed", "evidence_required": ["mock_report"]}
                    ],
                },
                # reachable, flagged-terminal stage; archive declares a class.
                {
                    "key": "closed",
                    "actions": [{"key": "archive", "side_effect_class": "none"}],
                    "terminal": True,
                    "exit_criteria": [],
                },
            ],
        },
        "entities": [
            {
                "key": "conversation_thread",
                "type": "conversation",
                "states": ["open", "watching", "closed_won", "closed_lost"],
                "terminal_states": ["closed_won", "closed_lost"],
            }
        ],
        "event_loops": [
            {
                "key": "seller_follow_up",
                "type": "seller_follow_up",
                "entity": "conversation_thread",
                "triggers": [
                    {"kind": "timer", "detail": "nudge seller", "cadence_hours": 72},
                    {"kind": "inbound", "detail": "seller replies", "channel": "infobip"},
                ],
                # declared WIN + LOSS, distinct, finite cap -> win_signal_rail,
                # distinct_terminals, event_loop_termination.
                "terminal_states": ["closed_won", "closed_lost"],
                "max_nudges": 2,
            }
        ],
        # optimizer-managed knob -> tunable_consumer_binding sees a real consumer.
        "tunables": {
            "follow_up_interval_hours": {"default": 72, "range": [24, 240]},
        },
        "approval_gates": [
            {"key": "side_effect_approval", "required_before": ["research"]}
        ],
        "proof_requirements": ["mock_report"],
        "side_effect_policy": {
            "allowed": ["none", "internal"],
            "forbidden": ["external_irreversible"],
        },
        "escalation_paths": [
            {"condition": "contract proof or side-effect authority is unclear", "to": "mock-ceo"}
        ],
        "owner_summary": {
            "summary": "The board runs mock contracted work and advances only with declared proof."
        },
    }


def _all_flags_on(monkeypatch) -> None:
    """Turn ON every hardening flag (the FULL-enforcement posture)."""
    monkeypatch.setenv("HERMES_LAUNCH_COMPLETENESS_ENFORCE", "all")
    monkeypatch.setenv("HERMES_KANBAN_REQUIRE_CONTRACT_DEFAULTS", "1")
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_WORKER_TOOLSETS", "1")
    monkeypatch.setenv("HERMES_KANBAN_BRIDGE_TOOL_POLICY", "1")


def _codes(gate) -> set[str]:
    return {b["code"] for b in gate.get("blockers", [])}


def _completeness_blocker(gate):
    return next(
        (b for b in gate.get("blockers", []) if b["code"] == "launch_completeness_failed"),
        None,
    )


def _create_ready_research_task(slug: str) -> str:
    with kb.connect(board=slug) as conn:
        return kb.create_task(
            conn,
            title="contracted work",
            assignee="mock-worker",
            goal_id="mock-goal",
            workstream_id="ops",
            stage_key="execute",
            action_key="research",
            board=slug,
        )


# ===========================================================================
# Self-check: the sound contract really is all-green under enforce=all.
# This is the "documented final shape" assertion -- if any dimension regresses,
# this fails FIRST and names it, before any board machinery runs.
# ===========================================================================


def test_sound_contract_passes_every_enforced_dimension():
    report = assess_launch_completeness(_sound_contract(), enforce=set(DIMENSIONS))
    assert report["ok"] is True, (
        "sound contract must pass ALL launch-completeness dimensions under "
        f"enforce=all; errors: {report['errors']}"
    )
    assert report["errors"] == []
    # Every canonical dimension is present in the report (none silently skipped).
    for dim in DIMENSIONS:
        assert dim in report["dimensions"], f"dimension {dim} missing from report"


# ===========================================================================
# SCENARIO 1: a SOUND board DISPATCHES under full enforcement.
#   Effect: board_dispatch_gate(slug).ok is True, NO launch_completeness_failed
#   blocker, AND a ready task is actually claimable (status running). Plus the
#   two per-task seams have real effect: the worker spawn is toolset-constrained
#   (parsed argv) and the contract's blocked tool is bridged into the action gate.
# ===========================================================================


def test_sound_board_dispatches_under_full_enforcement(fresh_home, monkeypatch):
    _all_flags_on(monkeypatch)
    contract = _sound_contract()
    _approve_contract_board("skeleton", contract)

    gate = kb.board_dispatch_gate("skeleton")
    assert gate["ok"] is True, f"sound board must dispatch under full enforcement: {gate['blockers']}"
    assert "launch_completeness_failed" not in _codes(gate)
    assert gate["readiness"]["completeness"]["blocking"] == []
    # The full enforced set is the entire canonical dimension list.
    assert set(gate["readiness"]["completeness"]["enforced_dimensions"]) == set(DIMENSIONS)

    # A ready task is genuinely CLAIMABLE (the effect that matters most).
    tid = _create_ready_research_task("skeleton")
    with kb.connect(board="skeleton") as conn:
        verdict = kb.evaluate_dispatch_eligibility(conn, tid, board="skeleton")
        assert verdict["ok"] is True, f"dispatch eligibility blocked: {verdict['blockers']}"
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "running"
        # REQUIRE_CONTRACT_DEFAULTS finds nothing absent (require_* all declared).
        resolved = kb.resolve_task_contract(task, board="skeleton")
        assert resolved["contract_defaults_applied"] == []
        assert resolved["worker_envelope"].get("toolsets") == ["kanban"]


def test_full_enforcement_constrains_worker_toolset_at_spawn(fresh_home, monkeypatch):
    """ENFORCE_WORKER_TOOLSETS effect: the spawned worker argv resolves
    args.toolsets == the declared envelope subset. Asserted by PARSING the argv
    with the real hermes parser (a bare string-match is insufficient -- the chat
    subparser nulls a mis-placed flag), so this proves the constraint actually
    reaches the worker, not merely that a token is present."""
    _all_flags_on(monkeypatch)
    _approve_contract_board("skeleton", _sound_contract())
    tid = _create_ready_research_task("skeleton")

    captured: dict = {}

    class _FakeProc:
        pid = 4242

    def _fake_popen(cmd, **_kw):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    monkeypatch.setattr(kb, "_kanban_worker_skill_available", lambda _h: False)
    with kb.connect(board="skeleton") as conn:
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task, board="skeleton")
    kb._default_spawn(task, str(workspace), board="skeleton")
    cmd = captured["cmd"]

    # Effect: the constraint reaches the worker (placed AFTER 'chat' so the chat
    # subparser receives it) and the REAL parser resolves the envelope toolset.
    assert "--toolsets" in cmd
    assert cmd.index("--toolsets") > cmd.index("chat")
    assert _parse_resolved_toolsets(cmd) == "kanban"


def test_full_enforcement_bridges_contract_blocked_tool_into_action_gate(fresh_home, monkeypatch):
    """BRIDGE_TOOL_POLICY effect: the contract's runtime.tool_policy.blocked_tools
    is enforced by the LIVE action gate. Asserted by check_action_gate actually
    returning a block verdict for the contract-blocked tool."""
    from agent import action_gate

    _all_flags_on(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "skeleton")
    _approve_contract_board("skeleton", _sound_contract())

    assert action_gate._contract_blocked_tools() == ["send_message"]
    verdict = action_gate.check_action_gate("send_message", {"platform": "imsg"})
    assert verdict is not None and "ACTION BLOCKED" in verdict
    # A tool NOT in the contract's blocked set is not gated by the bridge.
    assert action_gate.check_action_gate("delegate_task", {}) is None


def _parse_resolved_toolsets(cmd):
    """Drive the SAME parse path ``hermes`` uses to resolve args.toolsets from a
    spawned worker argv (lifted from test_per_task_failclosed._parse_spawn_toolsets):
    strip the -p/--profile pair, cut to the first recognised top-level flag, then
    parse with build_top_level_parser()."""
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, chat_parser = build_top_level_parser()
    chat_parser.set_defaults(func=lambda *a, **k: None)
    argv = list(cmd)
    stripped = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-p", "--profile") and i + 1 < len(argv):
            i += 2
            continue
        if a.startswith("--profile="):
            i += 1
            continue
        stripped.append(a)
        i += 1
    if "--accept-hooks" in stripped:
        stripped = stripped[stripped.index("--accept-hooks"):]
    ns, _unknown = parser.parse_known_args(stripped)
    return getattr(ns, "toolsets", None)


# ===========================================================================
# SCENARIO 2: each CRITICAL dimension, when violated, BLOCKS the gate.
#   Pattern: approve the broken contract under report-mode (board goes ACTIVE),
#   THEN flip the dimension(s) to enforce. Effect: gate.ok is False AND a
#   launch_completeness_failed blocker NAMES the violated dimension.
#
#   This is the pass-iff-enforced proof: with enforcement reverted the gate would
#   be ok=True and carry no completeness blocker, failing both asserts.
# ===========================================================================


def _break_success(c):
    c["objective"]["success"] = ["close some deals", "do a great job"]  # prose, un-scoreable


def _break_side_effect(c):
    # an action with NO side_effect_class (default-DENY violation)
    c["workflow"]["stages"][0]["actions"].append({"key": "send_sms", "label": "text the seller"})


def _break_event_loop_termination(c):
    loop = c["event_loops"][0]
    loop.pop("terminal_states", None)
    loop.pop("max_nudges", None)
    loop["stop_conditions"] = ["when the seller says stop"]  # free-text only -> zombie


def _break_win_signal(c):
    # conversational loop with terminals but NONE a win-class outcome
    terms = ["dead", "recycled", "paused"]
    c["event_loops"][0]["terminal_states"] = terms
    c["entities"][0]["terminal_states"] = terms
    c["entities"][0]["states"] = ["open", "watching", *terms]


def _break_evidence_namespace(c):
    # exit requires an evidence key NO declared artifact produces
    c["workflow"]["stages"][0]["exit_criteria"] = [
        {"transition": "closed", "evidence_required": ["ghost_artifact"]}
    ]


def _break_distinct_terminals(c):
    # collapse the conversational loop to a SINGLE terminal
    c["event_loops"][0]["terminal_states"] = ["closed_won"]
    c["entities"][0]["terminal_states"] = ["closed_won"]


def _break_tunable_consumer(c):
    # a declared tunable that NO consumer reads (not managed, no sensor binding)
    c["tunables"] = {"some_business_threshold": {"default": 5, "range": [1, 10]}}


# Each (dimension, mutation) pair APPROVES cleanly under report-mode and then
# blocks the gate when that dimension is enforced. stage_reachability and the
# always-on ``invariants`` dimension are intentionally NOT parametrized here:
# their violations are hard structural-invariant ERRORS that reject the contract
# at REVIEW time (the board never goes active), so the approve-then-flip pattern
# does not apply -- they are covered by the always-on invariants rail and the
# dedicated launch-completeness-gate suite instead.
_DIMENSION_BREAKERS = [
    ("success_scoreability", _break_success),
    ("side_effect_class_coverage", _break_side_effect),
    ("event_loop_termination", _break_event_loop_termination),
    ("win_signal_rail", _break_win_signal),
    ("evidence_namespace", _break_evidence_namespace),
    ("distinct_terminals", _break_distinct_terminals),
    ("tunable_consumer_binding", _break_tunable_consumer),
]


@pytest.mark.parametrize("dimension,breaker", _DIMENSION_BREAKERS, ids=[d for d, _ in _DIMENSION_BREAKERS])
def test_violated_dimension_blocks_gate_under_enforcement(fresh_home, monkeypatch, dimension, breaker):
    contract = _sound_contract()
    breaker(contract)
    # Approve under REPORT-mode: the board genuinely goes active (the finding is
    # advisory only), so the subsequent block is the gate failing closed on a LIVE
    # board -- not an approval-time rejection.
    _approve_contract_board("broken", contract)
    assert kb.board_dispatch_gate("broken")["ok"] is True, (
        f"{dimension}: broken contract must APPROVE active under report-mode "
        "(advisory only) so the flip-to-enforce block is meaningful"
    )

    # Flip the SINGLE violated dimension to enforce.
    monkeypatch.setenv("HERMES_LAUNCH_COMPLETENESS_ENFORCE", dimension)
    gate = kb.board_dispatch_gate("broken")
    assert gate["ok"] is False, f"{dimension}: enforce must fail the gate closed"
    assert "launch_completeness_failed" in _codes(gate)
    blocker = _completeness_blocker(gate)
    assert blocker is not None
    assert blocker["enforced_dimensions"] == [dimension]
    assert any(f"[{dimension}]" in f for f in blocker["findings"]), (
        f"{dimension}: the completeness blocker must NAME the violated dimension; "
        f"findings={blocker['findings']}"
    )


def test_all_breakers_also_block_under_enforce_all(fresh_home, monkeypatch):
    """The same broken contracts, with ENFORCE=all (the production posture), each
    still block -- proving the broad 'all' switch subsumes the per-dimension flip."""
    for dimension, breaker in _DIMENSION_BREAKERS:
        slug = f"brk-{dimension}"
        contract = _sound_contract()
        breaker(contract)
        _approve_contract_board(slug, contract)
        monkeypatch.setenv("HERMES_LAUNCH_COMPLETENESS_ENFORCE", "all")
        gate = kb.board_dispatch_gate(slug)
        assert gate["ok"] is False, f"{dimension}: enforce=all must block"
        blocker = _completeness_blocker(gate)
        assert blocker is not None and any(f"[{dimension}]" in f for f in blocker["findings"]), (
            f"{dimension}: enforce=all must surface the violated dimension"
        )
        monkeypatch.delenv("HERMES_LAUNCH_COMPLETENESS_ENFORCE", raising=False)


# ===========================================================================
# SCENARIO 3: optimizer kill-switch under the LIVE tick.
#   A board with runtime.optimizer_policy.disabled -> optimizer_tick writes ZERO
#   knobs (board_knob_audit gains no rows) and the audit ledger is preserved.
# ===========================================================================


def test_optimizer_kill_switch_writes_zero_knobs_under_live_tick(fresh_home, monkeypatch):
    _all_flags_on(monkeypatch)
    contract = _sound_contract()
    # Disable the optimizer with the owner-authority fields the validator requires
    # for a disabled board to remain launch-ready.
    contract["runtime"]["optimizer_policy"] = {
        "disabled": True,
        "approved_by": "owner",
        "reason": "manual cadence control during the pilot",
    }
    contract["runtime"]["profiles"].pop("optimizer", None)
    _approve_contract_board("killsw", contract)
    assert kb.board_dispatch_gate("killsw")["ok"] is True

    with kb.connect(board="killsw") as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM board_knob_audit WHERE board = ?", ("killsw",)
        ).fetchone()[0]
        # Drive the REAL optimizer tick with force=True (bypass throttle) so a
        # non-disabled board WOULD have proposed/applied -- the only reason no
        # knob is written is the kill-switch short-circuit.
        result = kb.optimizer_tick(conn, board="killsw", force=True)
        after = conn.execute(
            "SELECT COUNT(*) FROM board_knob_audit WHERE board = ?", ("killsw",)
        ).fetchone()[0]

    assert result["applied"] == []
    assert result["reverted"] == []
    assert any(s.get("reason") == "optimizer_disabled" for s in result["skipped"]), (
        f"kill-switch must short-circuit with optimizer_disabled: {result['skipped']}"
    )
    # The observable effect: ZERO new applied-knob audit rows.
    assert after == before == 0


# ===========================================================================
# SCENARIO 4: reward correctness end-to-end through the REAL _close_timer_schedule
#   path (reactive_tick), NOT a unit call.
#     * a declared WIN terminal (closed_won) emits a conversion reward (1.0)
#     * a declared LOSS terminal (closed_lost) emits NO conversion (loop_closed 0.0)
# ===========================================================================


def _loop_terminal_outcomes(conn, board):
    return conn.execute(
        "SELECT reward_kind, reward_value FROM board_signals "
        "WHERE board = ? AND primitive_kind = 'outcome' AND action LIKE '%loop_terminal%'",
        (board,),
    ).fetchall()


def _drive_terminal_close(slug, outcome):
    """Resolve the watched entity to ``outcome`` and run the live timer tick so the
    reactive runtime's _close_timer_schedule path emits the terminal reward."""
    _approve_contract_board(slug, _sound_contract())
    with kb.connect(board=slug) as conn:
        row = conn.execute(
            "SELECT * FROM reactive_timer_schedules WHERE board = ?", (slug,)
        ).fetchone()
        entity_id = row["entity_id"]
        base = int(row["next_fire_at"])
        kb.resolve_reactive_entity(
            conn, entity_id, terminal_outcome=outcome, state=outcome, actor="fixture"
        )
        res = kb.reactive_tick(conn, now=base, board=slug)
        outcomes = [(o["reward_kind"], o["reward_value"]) for o in _loop_terminal_outcomes(conn, slug)]
    return res, outcomes


def test_declared_win_terminal_emits_conversion_reward_via_live_tick(fresh_home, monkeypatch):
    _all_flags_on(monkeypatch)
    res, outcomes = _drive_terminal_close("win", "closed_won")
    assert res["stopped"] == [{"loop_key": "seller_follow_up", "reason": "entity_terminal"}]
    assert any(kind == "conversion" and value == 1.0 for kind, value in outcomes), (
        f"a declared WIN terminal must earn a conversion reward 1.0: {outcomes}"
    )


def test_declared_loss_terminal_emits_no_conversion_reward_via_live_tick(fresh_home, monkeypatch):
    _all_flags_on(monkeypatch)
    res, outcomes = _drive_terminal_close("loss", "closed_lost")
    assert res["stopped"] == [{"loop_key": "seller_follow_up", "reason": "entity_terminal"}]
    assert outcomes, "a terminal outcome must still be recorded for a loss"
    assert all(kind != "conversion" for kind, _ in outcomes), (
        f"a declared LOSS terminal must NOT earn a conversion reward: {outcomes}"
    )
    assert any(kind == "loop_closed" and value == 0.0 for kind, value in outcomes), (
        f"a declared LOSS terminal must record loop_closed 0.0: {outcomes}"
    )


# ===========================================================================
# SCENARIO 5: observability -- a blocked board writes a deduped dispatch_blocked
#   board_signals row (blocks are VISIBLE, not silent).
# ===========================================================================


def test_blocked_board_writes_deduped_dispatch_blocked_signal(fresh_home, monkeypatch):
    contract = _sound_contract()
    _break_success(contract)  # prose success -> success_scoreability finding
    _approve_contract_board("blk", contract)  # report-mode: goes active
    tid = _create_ready_research_task("blk")

    # Flip enforcement so the gate fails closed; a claim attempt routes through
    # board_dispatch_gate -> _emit_dispatch_blocked_signal.
    monkeypatch.setenv("HERMES_LAUNCH_COMPLETENESS_ENFORCE", "all")
    with kb.connect(board="blk") as conn:
        assert kb.claim_task(conn, tid) is None  # blocked
        rows = conn.execute(
            "SELECT primitive_key, dedupe_key FROM board_signals "
            "WHERE board = ? AND primitive_kind = 'dispatch_blocked'",
            ("blk",),
        ).fetchall()
        assert len(rows) == 1, f"exactly one dispatch_blocked signal expected: {rows}"
        # The block reason is observable in the signal's primitive_key.
        assert "launch_completeness_failed" in rows[0]["primitive_key"]
        assert rows[0]["dedupe_key"].startswith("dispatch_blocked:blk:")

        # A second blocked claim is DEDUPED (the stuck board does not spam).
        assert kb.claim_task(conn, tid) is None
        count = conn.execute(
            "SELECT COUNT(*) FROM board_signals "
            "WHERE board = ? AND primitive_kind = 'dispatch_blocked'",
            ("blk",),
        ).fetchone()[0]
        assert count == 1, "duplicate blocked claims must dedupe to a single signal row"


# ===========================================================================
# SCENARIO 6: DEFAULT-OFF end-to-end no-op. The SAME sound + broken contracts,
#   with NO enforcement flags set, ALL dispatch (gate ok) -- enforcement is
#   strictly opt-in and the loop runs unchanged by default.
# ===========================================================================


def test_default_off_sound_contract_dispatches_and_claims(fresh_home):
    """No flags set: the sound board dispatches and a task claims (the baseline
    the enforcement posture must not regress)."""
    _approve_contract_board("skeleton", _sound_contract())
    gate = kb.board_dispatch_gate("skeleton")
    assert gate["ok"] is True
    assert gate["readiness"]["completeness"]["enforced_dimensions"] == []
    tid = _create_ready_research_task("skeleton")
    with kb.connect(board="skeleton") as conn:
        assert kb.claim_task(conn, tid) is not None
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "running"


@pytest.mark.parametrize("dimension,breaker", _DIMENSION_BREAKERS, ids=[d for d, _ in _DIMENSION_BREAKERS])
def test_default_off_broken_contracts_all_dispatch(fresh_home, dimension, breaker):
    """No flags set: EVERY contract that scenario 2 blocks under enforcement
    dispatches cleanly here (gate ok, no completeness blocker) -- the finding is a
    pure advisory warning. This is the guarantee that LANDING the seam breaks no
    live board until a dimension is explicitly flipped on."""
    contract = _sound_contract()
    breaker(contract)
    _approve_contract_board("offbrk", contract)
    gate = kb.board_dispatch_gate("offbrk")
    assert gate["ok"] is True, f"{dimension}: default-off must NOT block ({gate['blockers']})"
    assert "launch_completeness_failed" not in _codes(gate)
    assert gate["readiness"]["completeness"]["blocking"] == []
    # The finding is still VISIBLE as an advisory warning (report-mode), not silent.
    warns = gate["readiness"].get("warnings") or []
    assert any(f"[{dimension}]" in w for w in warns), (
        f"{dimension}: the finding must remain an advisory warning under default-off"
    )


def test_default_off_no_dispatch_blocked_signal_for_sound_board(fresh_home):
    """Default-off + sound board: a successful claim writes NO dispatch_blocked
    signal (the observability rail only fires on a genuine block)."""
    _approve_contract_board("clean", _sound_contract())
    tid = _create_ready_research_task("clean")
    with kb.connect(board="clean") as conn:
        assert kb.claim_task(conn, tid) is not None
        count = conn.execute(
            "SELECT COUNT(*) FROM board_signals WHERE board = ? AND primitive_kind = 'dispatch_blocked'",
            ("clean",),
        ).fetchone()[0]
        assert count == 0
