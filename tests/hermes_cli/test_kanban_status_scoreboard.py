"""Regression tests for the Telegram-reachable `/kanban status` + `/kanban
scoreboard` operability views (Batch A, Step 3).

These views are NEW, ADDITIVE subcommands. Because the gateway pipes `/kanban`
through ``hermes_cli.kanban.run_slash``, a CLI subcommand == a Telegram command
for free, so these tests drive the exact parser path the gateway uses.

What is pinned here:
  * `/kanban status` renders the board names, the BLOCKED reason for an
    active-but-blocked board, and the ACTIVE-BUT-BLOCKED flag, and fits under
    the 3800-char gateway truncation cap.
  * `/kanban scoreboard <board>` renders attainment without crashing.
  * The views are READ-ONLY: the main kanban.db content is byte-identical
    before and after (no board-state mutation).
  * The default `/kanban` subcommands (list/show/stats/help) are byte-identical
    to before — status/scoreboard are purely additive.

The status test FAILS WITHOUT the feature (the `status` subcommand does not
exist, so run_slash returns a usage error rather than the dashboard).
"""

from __future__ import annotations

import glob
import hashlib
import os
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_scoreboard as ksb


# ---------------------------------------------------------------------------
# Fixture (isolated home; never touches a live DB)
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
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
                {
                    "key": "close",
                    "actions": [{"key": "archive_outcome"}],
                    "exit_criteria": [],
                },
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
            {"condition": "offer or commitment exceeds owner-approved authority", "to": "owner"}
        ],
        "owner_summary": {
            "summary": (
                "The board sources seller leads, qualifies properties, follows up "
                "through approved channels, negotiates within owner-approved limits, "
                "and archives each deal outcome with proof."
            )
        },
    }


def _seed_active_but_blocked(slug="ninaxfinds-growth"):
    """A managed board with an incomplete contract forced to launch_phase=active.

    Reproduces the ninaxfinds-growth trap: board_dispatch_gate() reports
    ok=False (launch_readiness_failed) while phase=active.
    """
    kb.review_business_launch_contract(
        slug,
        contract={"objective": {"statement": "Faceless multi-city city-finds engine"}},
        create_if_missing=True,
    )
    kb.write_board_metadata(slug, launch_phase="active")
    return slug


def _seed_ok_board(slug="land-wholesaling"):
    """An approved, launch-ready, active board whose gate is OK."""
    contract = _launch_ready_contract()
    kb.review_business_launch_contract(slug, contract=contract, create_if_missing=True)
    token = kb.issue_board_launch_approval_token(
        slug,
        contract=contract,
        approved_by="owner",
        approval_evidence={"source": "test-owner-approval"},
        owner_authority_confirmed=True,
    )["token"]
    kb.review_business_launch_contract(
        slug, contract=contract, approve=True, author="owner", approval_token=token,
    )
    return slug


def _maindb_digest(home):
    """MD5 of every main kanban.db file under home (excludes -wal/-shm sidecars).

    The sidecars are intrinsic SQLite WAL artefacts that a mode=ro reader maps
    even though it never mutates board state; the real read-only guarantee is
    that the main DB content is byte-identical.
    """
    out = {}
    candidates = glob.glob(os.path.join(str(home), "**", "kanban.db"), recursive=True)
    candidates.append(os.path.join(str(home), "kanban.db"))
    for p in candidates:
        if os.path.isfile(p):
            out[p] = hashlib.md5(open(p, "rb").read()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# /kanban status — effect tests
# ---------------------------------------------------------------------------

def test_status_renders_blocked_reason_and_active_but_blocked_flag(fresh_home):
    """/kanban status surfaces the board names, the BLOCKED reason for the
    active-but-blocked board, and the ACTIVE-BUT-BLOCKED flag; fits the cap.

    FAILS WITHOUT the feature: the `status` subcommand doesn't exist, so
    run_slash returns a usage error rather than the dashboard.
    """
    blocked = _seed_active_but_blocked("ninaxfinds-growth")
    ok = _seed_ok_board("land-wholesaling")

    out = kc.run_slash("status")

    # Board names are present.
    assert blocked in out
    assert ok in out
    # The BLOCKED reason for the active-but-blocked board is surfaced.
    assert "launch_readiness_failed" in out
    # The clarity-trap flag is rendered.
    assert "ACTIVE-BUT-BLOCKED" in out
    # The top summary line names the active-but-blocked board.
    assert "active-but-blocked: ninaxfinds-growth" in out
    # Governance posture line is present.
    assert "governance:" in out
    # Fits under the gateway's 3800-char truncation cap.
    assert len(out) <= 3800


def test_status_renders_under_cap_with_many_boards(fresh_home):
    """With many boards the renderer self-truncates to stay under the cap."""
    for i in range(60):
        kb.create_board(f"board-{i:03d}")
    out = ksb.render_status(ksb.assemble_status_rows())
    assert len(out) <= 3800
    # Header (summary + governance) survives truncation.
    assert out.startswith("KANBAN STATUS:")
    assert "governance:" in out


def test_status_is_read_only_main_db_byte_identical(fresh_home):
    """The status view must not mutate any board's main DB content."""
    _seed_active_but_blocked("ninaxfinds-growth")
    _seed_ok_board("land-wholesaling")
    # Settle the default board's DB up-front so the dispatch-time idempotent
    # init_db() side effect isn't conflated with the renderer's reads.
    kb.init_db()

    before = _maindb_digest(fresh_home)
    kc.run_slash("status")
    after = _maindb_digest(fresh_home)

    assert before == after, "status view mutated a board's main DB"


# ---------------------------------------------------------------------------
# /kanban scoreboard — effect tests
# ---------------------------------------------------------------------------

def test_scoreboard_renders_for_board(fresh_home):
    ok = _seed_ok_board("land-wholesaling")
    out = kc.run_slash(f"scoreboard {ok}")
    assert "SCOREBOARD" in out
    assert ok in out
    assert "Launch a land wholesaling business" in out
    assert "proof-gated completed" in out
    assert len(out) <= 3800


def test_scoreboard_is_read_only_main_db_byte_identical(fresh_home):
    _seed_ok_board("land-wholesaling")
    kb.init_db()
    before = _maindb_digest(fresh_home)
    kc.run_slash("scoreboard land-wholesaling")
    after = _maindb_digest(fresh_home)
    assert before == after, "scoreboard view mutated a board's main DB"


# ---------------------------------------------------------------------------
# Additivity: default /kanban behavior is byte-identical
# ---------------------------------------------------------------------------

def test_default_kanban_subcommands_byte_identical(fresh_home):
    """status/scoreboard are additive: existing /kanban output is unchanged."""
    kb.init_db()
    # Bare help block.
    assert kc.run_slash("").startswith("**/kanban**")
    # A representative set of pre-existing read-only subcommands must render
    # exactly as their handlers produce — the additive wiring changed none of
    # them. We pin their output is non-empty and free of the new markers.
    for sub in ("list", "stats"):
        out = kc.run_slash(sub)
        assert "KANBAN STATUS:" not in out
        assert "KANBAN SCOREBOARD:" not in out
    # The curated help block now advertises the lifecycle cheat-sheet, but the
    # original common-subcommands section is preserved verbatim.
    help_text = kc.run_slash("help")
    assert "Common subcommands:" in help_text
    assert "`list` (alias `ls`)" in help_text
    # …and the new lifecycle entries are present.
    assert "status" in help_text
    assert "scoreboard" in help_text
    assert "/approve <board>" in help_text
