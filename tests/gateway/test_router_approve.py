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


def _make_runner():
    """Bare GatewayRunner — no __init__, just the attrs the approve path reads."""
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner.session_store = None
    runner._pending_approvals = {}
    return runner


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
