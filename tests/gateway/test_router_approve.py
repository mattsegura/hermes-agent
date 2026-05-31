"""Router front-end: in-chat owner approval (Decision B(ii)) + driven flow.

These tests lock down the HARD RAIL for board launch:

* The launch token is minted ONLY by the explicit human ``/approve <board>``
  command in the trusted (admin-gated) command path. No model tool can mint
  it — ``issue_board_launch_approval_token`` refuses without
  ``owner_authority_confirmed=True``, which only trusted code ever sets.
* ``/approve <board>`` drafts→launches→binds a ready board end-to-end.
* An un-drafted / not-ready board yields a clear refusal, never a launch.

The driven-flow test walks the realistic router arc deterministically (no
aux model): a lightweight request touches no board; an existing board is
surfaced for routing; an unmatched goal is drafted then owner-approved into
an active, bound board; and a direct side-effecting action stays hard-denied.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with the kanban env scrubbed."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_TASK",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _make_runner(config=None):
    """Bare GatewayRunner — no __init__, just the attrs the approve path reads."""
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner.session_store = None
    runner._pending_approvals = {}
    runner.config = config
    return runner


def _config_with_admin(admin_ids):
    """A GatewayConfig whose telegram DM scope gates /approve to ``admin_ids``."""
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    cfg = GatewayConfig()
    cfg.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token="test-token",
        extra={"allow_admin_from": list(admin_ids)},
    )
    return cfg


def _make_event(text: str, *, platform=Platform.TELEGRAM, user_id="owner-1",
                chat_id="chat-1"):
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="owner",
    )
    return MessageEvent(text=text, source=source)


def _launch_ready_contract():
    """A known launch-ready operating contract (mirrors the binding tests)."""
    return {
        "objective": {
            "statement": "Launch a land wholesaling business",
            "success": ["qualified seller leads reach signed purchase agreements"],
            "failure": ["seller conversations continue without approval boundaries"],
            "constraints": ["owner approves outbound offers"],
        },
        "runtime": {
            "mode": "company",
            "dispatcher": {"profile": "land-ceo"},
            "profiles": {"ceo": "land-ceo", "optimizer": "land-opt", "worker": "land-operator"},
            "require_worker_envelopes": True,
            "require_provider_policy": True,
            "provider_policy": {"seller_outreach": {"provider": "approved_sms_gateway"}},
            "worker_envelopes": {
                "land-operator": {
                    "capabilities": ["seller_outreach"],
                    "toolsets": ["kanban"],
                    "allowed_side_effects": ["owner_approved_external_write"],
                }
            },
        },
        "workflow": {
            "id": "land-close-flow",
            "goal_id": "close-land-deals",
            "require_semantics": True,
            "workstreams": [{"key": "seller-conversion", "stages": ["source", "negotiate", "close"]}],
            "stages": [
                {
                    "key": "source",
                    "actions": [{"key": "capture_lead"}],
                    "triggers": [{"type": "timer", "key": "daily_source"}],
                    "exit_criteria": [
                        {"transition": "negotiate", "evidence_required": ["qualified_lead"]}
                    ],
                },
                {
                    "key": "negotiate",
                    "actions": [
                        {
                            "key": "seller_follow_up",
                            "required_capabilities": ["seller_outreach"],
                            "side_effect_class": "owner_approved_external_write",
                        }
                    ],
                    "exit_criteria": [
                        {"transition": "close", "evidence_required": ["accepted_terms"]}
                    ],
                },
                {"key": "close", "actions": [{"key": "archive_outcome"}], "exit_criteria": []},
            ],
        },
        "entities": [
            {
                "key": "seller_lead",
                "type": "lead",
                "states": ["new", "qualified", "negotiating", "closed", "dead"],
                "terminal_states": ["closed", "dead"],
            }
        ],
        "event_loops": [
            {"type": "inbound_sms", "entity": "seller_lead", "terminal_states": ["closed", "dead"]}
        ],
        "approval_gates": [
            {"key": "owner_offer_approval", "required_before": ["seller_follow_up"]}
        ],
        "proof_requirements": ["qualified_lead", "accepted_terms", "seller_outcome"],
        "side_effect_policy": {
            "allowed": ["read_only", "owner_approved_external_write"],
            "forbidden": ["unapproved_external_write"],
            "approval_required": ["seller_outreach"],
        },
        "escalation_paths": [
            {"condition": "offer exceeds owner-approved authority", "to": "owner"}
        ],
        "owner_summary": {"summary": "Sources, qualifies, negotiates within owner limits, archives."},
    }


def _draft_board(slug: str) -> dict:
    """Draft a launch-ready board into ``contract_review`` (no approval)."""
    return kb.review_business_launch_contract(
        slug,
        contract=_launch_ready_contract(),
        create_if_missing=True,
    )


# ---------------------------------------------------------------------------
# HARD RAIL: the agent can never mint a launch token
# ---------------------------------------------------------------------------

def test_token_mint_requires_owner_authority(fresh_home):
    """issue_board_launch_approval_token refuses without owner authority.

    This is the rail the model can never cross: it has no way to set
    ``owner_authority_confirmed=True``.
    """
    _draft_board("land-wholesaling")
    with pytest.raises(ValueError, match="owner authority"):
        kb.issue_board_launch_approval_token(
            "land-wholesaling",
            contract=_launch_ready_contract(),
            approved_by="model",
            approval_evidence={"source": "agent"},
            owner_authority_confirmed=False,
        )


def test_no_agent_tool_can_reach_the_token_mint():
    """No registered model tool handler mints a launch token.

    The launch-intake tool surface can draft/review/propose, but the token
    mint lives only in the CLI and the gateway /approve command.
    """
    import inspect

    import tools.kanban_tools as kt

    # Every kanban tool handler in the launch-intake surface must not call the
    # token mint. We assert it at the source level — the mint symbol must not
    # appear in any of the launch-intake handler bodies.
    handler_names = [
        "_handle_business_launch_review",
        "_handle_board_launch_status",
        "_handle_contract_amendment_propose",
        "_handle_contract_amendment_apply",
        "_handle_list_boards",
        "_handle_match_board",
    ]
    for name in handler_names:
        handler = getattr(kt, name)
        src = inspect.getsource(handler)
        assert "issue_board_launch_approval_token" not in src, (
            f"{name} must never mint a launch token"
        )
        assert "owner_authority_confirmed" not in src, (
            f"{name} must never assert owner authority"
        )


def test_agent_review_without_token_does_not_activate(fresh_home):
    """kanban_business_launch_review(approve=True) without a token cannot launch.

    Driven through the real agent tool handler: even asking to approve a
    fully launch-ready board fails (no token to mint), and the board stays
    out of ``active``.
    """
    import json

    import tools.kanban_tools as kt

    _draft_board("land-wholesaling")
    out = json.loads(kt._handle_business_launch_review({
        "board": "land-wholesaling",
        "contract": _launch_ready_contract(),
        "approve": True,  # asks to approve… but the agent has no token to pass.
    }))
    assert "error" in out, "agent approve without a token must error, not launch"
    meta = kb.read_board_metadata("land-wholesaling")
    assert kb.normalize_board_launch_phase(meta.get("launch_phase")) != "active"


# ---------------------------------------------------------------------------
# /approve <board> — explicit human authority mints + launches
# ---------------------------------------------------------------------------

def test_parse_approve_board_arg():
    runner = _make_runner()
    # Tool-approval keywords are NOT board args.
    for kw in ("", "all", "session", "always", "all session", "all always"):
        assert runner._parse_approve_board_arg(_make_event(f"/approve {kw}".strip())) is None
    # A board slug IS a board arg.
    assert runner._parse_approve_board_arg(_make_event("/approve land-wholesaling")) == "land-wholesaling"


@pytest.mark.asyncio
async def test_approve_command_launches_and_binds(fresh_home):
    """End-to-end: draft → /approve <board> → active + role profiles bound."""
    _draft_board("land-wholesaling")
    runner = _make_runner()

    out = await runner._handle_approve_command(_make_event("/approve land-wholesaling"))

    assert "active" in out.lower()
    # Board is actually active.
    meta = kb.read_board_metadata("land-wholesaling")
    assert kb.normalize_board_launch_phase(meta.get("launch_phase")) == "active"
    # Contract-role profiles are bound to THIS board.
    for prof in ("land-ceo", "land-opt", "land-operator"):
        assert kb.profile_board_binding(prof) == "land-wholesaling"


@pytest.mark.asyncio
async def test_approve_recovers_from_expired_token_via_remint(fresh_home, monkeypatch):
    """S6: a CLEAN token expiry on activation is recovered by ONE re-mint.

    Drives the REAL retry seam: the first activation sees a genuinely-expired
    token (we expire the just-minted row in the DB so the REAL consume path
    raises the REAL "approval_token has expired" ValueError); the handler
    re-mints (the owner's /approve is fresh authority) and the second
    activation succeeds. Asserts the EFFECT: the reply is a success (not a raw
    "expired" dead-end) and the board ends up ACTIVE.
    """
    _draft_board("land-wholesaling")
    runner = _make_runner()

    real_review = kb.review_business_launch_contract
    calls = {"n": 0}

    def _review_expiring_first_token(board, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Expire EVERY pending token for this board so the real consume
            # path inside review_business_launch_contract rejects it as
            # expired -- exactly the late-answer scenario.
            with kb.connect(board=board) as conn:
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE board_launch_approval_tokens "
                        "SET expires_at = 1 WHERE status = 'pending'",
                    )
        return real_review(board, *args, **kwargs)

    monkeypatch.setattr(kb, "review_business_launch_contract", _review_expiring_first_token)

    out = await runner._handle_approve_command(_make_event("/approve land-wholesaling"))

    # Recovered: two activation attempts (first expired, second clean).
    assert calls["n"] == 2, calls
    # The owner sees success, not a raw expired dead-end.
    assert "active" in out.lower()
    assert "expired" not in out.lower()
    # Board is genuinely active now.
    meta = kb.read_board_metadata("land-wholesaling")
    assert kb.normalize_board_launch_phase(meta.get("launch_phase")) == "active"


@pytest.mark.asyncio
async def test_approve_owner_id_match_mints(fresh_home, monkeypatch):
    """S5(a): with slash-gating ON, the matching owner mints + launches."""
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    _draft_board("land-wholesaling")
    runner = _make_runner(config=_config_with_admin(["owner-1"]))
    out = await runner._handle_approve_command(
        _make_event("/approve land-wholesaling", user_id="owner-1")
    )
    assert "active" in out.lower()
    # No "ran without slash-gating" warning when identity WAS verified.
    assert "without slash-gating" not in out.lower()
    meta = kb.read_board_metadata("land-wholesaling")
    assert kb.normalize_board_launch_phase(meta.get("launch_phase")) == "active"


@pytest.mark.asyncio
async def test_approve_non_owner_is_refused_before_mint(fresh_home, monkeypatch):
    """S5(a) FAIL-CLOSED: a non-owner with slash-gating ON is refused, and NO
    token is minted (the board never goes active)."""
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    _draft_board("land-wholesaling")
    runner = _make_runner(config=_config_with_admin(["owner-1"]))

    minted = {"n": 0}
    real_mint = kb.issue_board_launch_approval_token

    def _counting_mint(*a, **k):
        minted["n"] += 1
        return real_mint(*a, **k)

    monkeypatch.setattr(kb, "issue_board_launch_approval_token", _counting_mint)

    out = await runner._handle_approve_command(
        _make_event("/approve land-wholesaling", user_id="intruder-9")
    )
    assert "refused" in out.lower()
    # Critically: the mint was NEVER reached.
    assert minted["n"] == 0, "non-owner must be refused BEFORE the token mint"
    meta = kb.read_board_metadata("land-wholesaling")
    assert kb.normalize_board_launch_phase(meta.get("launch_phase")) != "active"


@pytest.mark.asyncio
async def test_approve_unconfirmed_owner_mints_with_warning(fresh_home, monkeypatch):
    """S5(a): with NO slash-gating AND no allowlist, /approve still mints (the
    admin-gated command path is the legacy trust boundary) but MUST warn that
    it ran without slash-gating — never a silent un-gated mint."""
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    _draft_board("land-wholesaling")
    runner = _make_runner(config=None)  # no config => slash-gating disabled
    out = await runner._handle_approve_command(
        _make_event("/approve land-wholesaling", user_id="whoever")
    )
    # Still launches…
    assert "active" in out.lower()
    meta = kb.read_board_metadata("land-wholesaling")
    assert kb.normalize_board_launch_phase(meta.get("launch_phase")) == "active"
    # …but the un-gated approval is surfaced, not silent.
    assert "without slash-gating" in out.lower()


@pytest.mark.asyncio
async def test_approve_allowlist_owner_match_mints_no_warning(fresh_home, monkeypatch):
    """S5(a): TELEGRAM_ALLOWED_USERS[0] is the canonical owner; a matching
    invoker mints with no warning even when slash-gating config is absent."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "owner-1,helper-2")
    _draft_board("land-wholesaling")
    runner = _make_runner(config=None)
    out = await runner._handle_approve_command(
        _make_event("/approve land-wholesaling", user_id="owner-1")
    )
    assert "active" in out.lower()
    assert "without slash-gating" not in out.lower()


@pytest.mark.asyncio
async def test_approve_allowlist_non_owner_refused(fresh_home, monkeypatch):
    """S5(a) FAIL-CLOSED: a user not matching TELEGRAM_ALLOWED_USERS[0] is
    refused even with slash-gating config absent."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "owner-1")
    _draft_board("land-wholesaling")
    runner = _make_runner(config=None)
    minted = {"n": 0}
    real_mint = kb.issue_board_launch_approval_token

    def _counting_mint(*a, **k):
        minted["n"] += 1
        return real_mint(*a, **k)

    monkeypatch.setattr(kb, "issue_board_launch_approval_token", _counting_mint)
    out = await runner._handle_approve_command(
        _make_event("/approve land-wholesaling", user_id="someone-else")
    )
    assert "refused" in out.lower()
    assert minted["n"] == 0


@pytest.mark.asyncio
async def test_approve_message_shows_owner_summary(fresh_home):
    """The /approve confirmation renders the deterministic owner summary.

    The owner must see WHAT just went live (pipeline + approval boundary), not
    just "phase: active" -- so the gateway renders the shared contract summary.
    """
    _draft_board("land-wholesaling")
    runner = _make_runner()
    out = await runner._handle_approve_command(_make_event("/approve land-wholesaling"))
    # Explicit activation confirmation is preserved.
    assert "active" in out.lower()
    # …and the structured summary is appended.
    assert "Pipeline" in out
    assert "approval" in out.lower()


@pytest.mark.asyncio
async def test_bare_approve_pending_list_shows_one_liner(fresh_home):
    """`/approve` with a pending board shows a one-line shape per board."""
    _draft_board("land-wholesaling")
    runner = _make_runner()
    out = await runner._handle_approve_command(_make_event("/approve"))
    assert "land-wholesaling" in out
    # The one-liner carries the arrowed pipeline so the owner sees the shape.
    assert "→" in out


@pytest.mark.asyncio
async def test_approve_nonexistent_board_refuses(fresh_home):
    runner = _make_runner()
    out = await runner._handle_approve_command(_make_event("/approve ghost-board"))
    assert "ghost-board" in out
    assert "no board" in out.lower() or "nothing to approve" in out.lower()
    # Nothing was created.
    assert not kb.board_exists("ghost-board")


@pytest.mark.asyncio
async def test_approve_not_ready_board_refuses(fresh_home):
    """A board drafted but not launch-ready gets a clear refusal, never a launch."""
    # An incomplete contract drafts the board into contract_review (not active,
    # not ready) — the realistic "agent proposed but it isn't fleshed out" state.
    kb.review_business_launch_contract(
        "half-baked",
        contract={"objective": {"statement": "do something vague"}},
        create_if_missing=True,
    )
    runner = _make_runner()
    out = await runner._handle_approve_command(_make_event("/approve half-baked"))
    assert "can't approve" in out.lower() or "⛔" in out
    # Still not active.
    meta = kb.read_board_metadata("half-baked")
    assert kb.normalize_board_launch_phase(meta.get("launch_phase")) != "active"


@pytest.mark.asyncio
async def test_approve_already_active_board_is_noop(fresh_home):
    _draft_board("land-wholesaling")
    runner = _make_runner()
    await runner._handle_approve_command(_make_event("/approve land-wholesaling"))
    # Second approve is a clean no-op, not a re-launch.
    out = await runner._handle_approve_command(_make_event("/approve land-wholesaling"))
    assert "already active" in out.lower()


@pytest.mark.asyncio
async def test_approve_active_but_blocked_surfaces_blocker_not_false_success(fresh_home):
    """ACTIVE-BUT-BLOCKED dead-end fix (the ninaxfinds-growth trap).

    A board can be phase=active yet still gate-BLOCKED (e.g. an incomplete
    managed contract -> launch_readiness_failed), which strands its tasks. The
    old `/approve` early-return claimed "already active — nothing to approve"
    without consulting the gate. This regression pins that the handler now
    consults board_dispatch_gate(): an active-but-blocked board returns the
    blocker reasons (in plain owner language) PLUS the concrete next step, and
    explicitly does NOT return the false "already active" success string.

    FAILS WITHOUT the fix (the early return fires before the gate is consulted,
    so the output is "already active — nothing to approve").
    """
    # Manufacture the ninaxfinds-growth state: a managed board whose contract is
    # incomplete (-> readiness fails) that has nonetheless been forced into
    # launch_phase=active. board_dispatch_gate() reports ok=False with a
    # launch_readiness_failed blocker carrying the missing-field tokens.
    kb.review_business_launch_contract(
        "stuck-board",
        contract={"objective": {"statement": "do something vague"}},
        create_if_missing=True,
    )
    kb.write_board_metadata("stuck-board", launch_phase="active")
    # Precondition: this really is the active-but-blocked trap.
    gate = kb.board_dispatch_gate("stuck-board")
    assert kb.normalize_board_launch_phase(gate.get("launch_phase")) == "active"
    assert gate.get("ok") is False
    assert any(
        (b.get("code") if isinstance(b, dict) else b) == "launch_readiness_failed"
        for b in gate.get("blockers") or []
    )

    runner = _make_runner()
    out = await runner._handle_approve_command(_make_event("/approve stuck-board"))

    # The false success is gone…
    assert "already active — nothing to approve" not in out
    assert "nothing to approve" not in out.lower()
    # …replaced by the blocked signal + plain-language blocker reasons…
    assert "blocked" in out.lower()
    assert "contract is incomplete" in out.lower()
    # …and the concrete next step (re-run /approve after finishing the contract).
    assert "/approve stuck-board" in out
    # READ-ONLY: surfacing the blocker must not mutate the board's phase.
    meta_after = kb.read_board_metadata("stuck-board")
    assert kb.normalize_board_launch_phase(meta_after.get("launch_phase")) == "active"


@pytest.mark.asyncio
async def test_approve_active_and_ok_board_still_reports_already_active(fresh_home):
    """The genuinely active-and-OK path is unchanged: still 'already active'.

    Guards against the active-but-blocked fix accidentally swallowing the happy
    path — an approved, launch-ready, active board with an OK gate must still
    return the normal success no-op.
    """
    _draft_board("land-wholesaling")
    runner = _make_runner()
    await runner._handle_approve_command(_make_event("/approve land-wholesaling"))
    # Sanity: the freshly-approved board's gate is OK.
    assert kb.board_dispatch_gate("land-wholesaling").get("ok") is True
    out = await runner._handle_approve_command(_make_event("/approve land-wholesaling"))
    assert "already active" in out.lower()


@pytest.mark.asyncio
async def test_bare_approve_surfaces_pending_boards(fresh_home):
    """`/approve` with no pending tool approval lists boards awaiting launch."""
    _draft_board("land-wholesaling")
    runner = _make_runner()
    out = await runner._handle_approve_command(_make_event("/approve"))
    assert "land-wholesaling" in out


# ---------------------------------------------------------------------------
# Task 5 — driven router flow (deterministic, no aux model)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_driven_router_flow(fresh_home):
    """Walk the realistic router arc and assert the safety boundary holds.

    1. Lightweight request → no board exists / is created.
    2. A goal matching an existing board → that board is surfaced top-ranked.
    3. An unmatched goal → drafted, then owner-approved into an active board.
    4. A direct side-effecting action is hard-denied by the action gate.
    """
    import json

    import tools.kanban_tools as kt

    # (1) Lightweight request: the router answers inline. No board exists, and
    #     enumerating boards never auto-creates the phantom default board.
    listing = json.loads(kt._handle_list_boards({}))
    assert listing["count"] == 0 and listing["boards"] == []

    # (2) Existing matching board: launch one, then a related goal surfaces it.
    _draft_board("land-wholesaling")
    runner = _make_runner()
    await runner._handle_approve_command(_make_event("/approve land-wholesaling"))

    matched = json.loads(kt._handle_match_board(
        {"goal": "negotiate a land deal with a seller"}
    ))
    assert matched["candidates"], "an active board should be a routing candidate"
    assert matched["candidates"][0]["slug"] == "land-wholesaling"
    assert matched["candidates"][0]["match_score"] >= 1
    assert kb.DEFAULT_BOARD not in {c["slug"] for c in matched["candidates"]}

    # (3) Unmatched goal: zero lexical overlap → router would clarify + propose.
    unmatched = json.loads(kt._handle_match_board(
        {"goal": "compose a weekly poetry newsletter"}
    ))
    top = unmatched["candidates"][0] if unmatched["candidates"] else {"match_score": 0}
    assert top["match_score"] == 0

    # …the proposal path drafts a new board, which only the owner can launch.
    _draft_board("poetry-news")  # stands in for an intake-drafted contract
    meta = kb.read_board_metadata("poetry-news")
    assert kb.normalize_board_launch_phase(meta.get("launch_phase")) != "active"
    out = await runner._handle_approve_command(_make_event("/approve poetry-news"))
    assert "active" in out.lower()
    assert kb.normalize_board_launch_phase(
        kb.read_board_metadata("poetry-news").get("launch_phase")
    ) == "active"

    # (4) The router profile can never take a side-effecting action directly.
    (fresh_home / "config.yaml").write_text(
        "action_gate:\n"
        "  mode: yolo\n"
        "  rules:\n"
        "    blocked_tools:\n"
        "    - write_file\n"
        "    - patch\n"
        "    - terminal\n"
        "    - execute_code\n"
        "    - delegate_task\n"
        "    - send_message\n"
        "    - browser_navigate\n",
        encoding="utf-8",
    )
    from agent.action_gate import check_action_gate
    for blocked in ("write_file", "patch", "execute_code", "send_message", "browser_navigate"):
        msg = check_action_gate(blocked, {})
        assert msg and "BLOCKED" in msg, f"{blocked} must be hard-denied"
    # Read / reason / board-ops stay allowed.
    assert check_action_gate("read_file", {"path": "/tmp/x"}) is None
    assert check_action_gate("kanban_match_board", {"goal": "x"}) is None


# ---------------------------------------------------------------------------
# S7(a): /approve auto-subscribes the owner at the BOARD level
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_creates_board_level_notify_sub(fresh_home, monkeypatch):
    """S7(a): a successful /approve registers a BOARD-LEVEL notify sub for the
    approving owner's chat and tells them updates will arrive here."""
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    _draft_board("land-wholesaling")
    runner = _make_runner()
    out = await runner._handle_approve_command(
        _make_event("/approve land-wholesaling", chat_id="chat-77")
    )
    assert "active" in out.lower()
    assert "updates here" in out.lower()
    # A board-level subscription row exists for the approving chat.
    conn = kb.connect(board="land-wholesaling")
    try:
        board_subs = kb.list_board_notify_subs(conn)
    finally:
        conn.close()
    assert any(
        s["task_id"] == kb.BOARD_NOTIFY_SENTINEL_TASK_ID
        and s["chat_id"] == "chat-77"
        and (s["platform"] or "").lower() == "telegram"
        for s in board_subs
    ), board_subs


def test_board_notify_fanout_covers_future_tasks(fresh_home):
    """S7(a) EFFECT: the notifier fan-out expands a board-level sub into a
    per-task sub for a task created AFTER the subscription (future coverage)."""
    runner = _make_runner()
    conn = kb.connect(board=kb.DEFAULT_BOARD)
    try:
        kb.add_board_notify_sub(
            conn, platform="telegram", chat_id="chat-77", notifier_profile="default",
        )
        # A task created AFTER the board-level subscribe.
        tid = kb.create_task(conn, title="future task", assignee="worker")
        subs = kb.list_notify_subs(conn)
        runner._expand_board_notify_subs(kb, conn, subs, "default")
        per_task = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()
    assert any(
        s["chat_id"] == "chat-77" and s["task_id"] == tid for s in per_task
    ), "board-level sub must seed a per-task sub for the future task"


# ---------------------------------------------------------------------------
# S7(b): read-only /pending digest
# ---------------------------------------------------------------------------

def _insert_pending_action(profile="default", tool="send_message", task_id="t_abc"):
    """Insert one open action-gate escalation into the queue DB."""
    import json as _json
    import time as _t
    from agent import action_gate as _ag
    conn = _ag._ensure_queue_db()
    try:
        now = int(_t.time())
        cur = conn.execute(
            """INSERT INTO pending_actions
               (profile, tool_name, tool_args, description, classification,
                status, created_at, expires_at, session_id, task_id)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (profile, tool, _json.dumps({"to": "x"}), "send a message",
             "external_write", now, now + 300, "sess-1", task_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_pending_enumerates_escalations_and_boards_readonly(fresh_home, monkeypatch):
    """S7(b): /pending lists open escalations AND boards awaiting launch, and
    does NOT mutate any escalation (read-only)."""
    # Reset the action_gate queue path so it uses this fresh HERMES_HOME.
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    from agent import action_gate as _ag
    _ag._QUEUE_DB_PATH = None  # force re-resolve under fresh_home

    aid = _insert_pending_action(tool="send_message", task_id="t_dead")
    _draft_board("land-wholesaling")  # a board awaiting /approve

    runner = _make_runner()
    out = await runner._handle_pending_command(_make_event("/pending"))

    # Escalation enumerated (id + tool + task).
    assert f"#{aid}" in out
    assert "send_message" in out
    # Board awaiting launch enumerated.
    assert "land-wholesaling" in out
    assert "awaiting launch" in out.lower()

    # READ-ONLY: the escalation is still pending (never approved/denied).
    conn = _ag._ensure_queue_db()
    try:
        row = conn.execute(
            "SELECT status FROM pending_actions WHERE id = ?", (aid,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending", "/pending must not mutate escalations"


@pytest.mark.asyncio
async def test_pending_empty_is_clean(fresh_home, monkeypatch):
    """S7(b): /pending with nothing open says so without error."""
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    from agent import action_gate as _ag
    _ag._QUEUE_DB_PATH = None
    runner = _make_runner()
    out = await runner._handle_pending_command(_make_event("/pending"))
    assert "no open action-gate escalations" in out.lower()
    assert "no boards awaiting launch" in out.lower()
