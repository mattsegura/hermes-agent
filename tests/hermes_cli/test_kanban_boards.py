"""Tests for the multi-board kanban layer (``hermes kanban boards …``).

Covers the pieces added when boards became a first-class concept:

* Slug validation and normalisation.
* Path resolution for ``default`` (compat ``<root>/kanban.db``) vs
  named boards (``<root>/kanban/boards/<slug>/kanban.db``).
* Current-board persistence via ``<root>/kanban/current`` and
  ``HERMES_KANBAN_BOARD`` env var.
* ``connect(board=)`` isolation — writes on one board don't leak.
* ``create_board`` / ``list_boards`` / ``remove_board`` round trip.
* CLI surface: ``hermes kanban boards list/create/switch/rm``.
* ``_default_spawn`` injects ``HERMES_KANBAN_BOARD`` into worker env.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

# Ensure the worktree (not the stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with no prior kanban state.

    The autouse hermetic conftest already nukes credentials + TZ; this
    fixture layers a per-test HERMES_HOME plus a path-init cache reset
    so each test sees a truly empty board set.
    """
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
    # Also reset hermes_constants cache so get_default_hermes_root() re-reads.
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    # Kanban module-level init cache must not leak between tests.
    kb._INITIALIZED_PATHS.clear()
    return home


def _launch_ready_contract():
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


def _approve_launch_contract(slug, contract, *, evidence_source="test-owner-approval"):
    kb.review_business_launch_contract(
        slug,
        contract=contract,
        create_if_missing=not kb.board_exists(slug),
    )
    token = kb.issue_board_launch_approval_token(
        slug,
        contract=contract,
        approved_by="owner",
        approval_evidence={"source": evidence_source},
        owner_authority_confirmed=True,
    )["token"]
    return kb.review_business_launch_contract(
        slug,
        contract=contract,
        approve=True,
        author="owner",
        approval_token=token,
    )


def _issue_amendment_token(slug, amendment_id, *, evidence_source="amendment-approval"):
    return kb.issue_board_launch_approval_token(
        slug,
        amendment_id=amendment_id,
        approved_by="owner",
        approval_evidence={"source": evidence_source},
        owner_authority_confirmed=True,
    )["token"]


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

class TestSlugValidation:
    @pytest.mark.parametrize("good", [
        "default", "atm10-server", "hermes-agent", "proj_1", "a",
        "very-long-but-still-ok-slug-with-hyphens-and-numbers-1234",
    ])
    def test_accepts_valid(self, good):
        assert kb._normalize_board_slug(good) == good

    @pytest.mark.parametrize("bad", [
        "-leading-hyphen", "_leading_underscore",
        "with/slash", "with space",
        "has.dot", "has?question",
        "..", "../etc", "foo\x00bar",
    ])
    def test_rejects_invalid(self, bad):
        with pytest.raises(ValueError):
            kb._normalize_board_slug(bad)

    def test_empty_returns_none(self):
        assert kb._normalize_board_slug(None) is None
        assert kb._normalize_board_slug("") is None
        assert kb._normalize_board_slug("   ") is None

    def test_auto_lowercases(self):
        # Uppercase is auto-downcased (friendlier than rejecting). ``Default``
        # → ``default``, ``ATM10`` → ``atm10``. The on-disk slug is always
        # lowercase regardless of what the user typed.
        assert kb._normalize_board_slug("Default") == "default"
        assert kb._normalize_board_slug("ATM10-Server") == "atm10-server"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

class TestPathResolution:
    def test_default_board_compat_path(self, fresh_home):
        """The default board's DB lives at ``<root>/kanban.db`` for back-compat."""
        assert kb.kanban_db_path() == fresh_home / "kanban.db"
        assert kb.kanban_db_path(board="default") == fresh_home / "kanban.db"

    def test_named_board_under_boards_dir(self, fresh_home):
        p = kb.kanban_db_path(board="atm10-server")
        assert p == fresh_home / "kanban" / "boards" / "atm10-server" / "kanban.db"

    def test_workspaces_per_board(self, fresh_home):
        assert kb.workspaces_root() == fresh_home / "kanban" / "workspaces"
        # Uppercase input gets auto-downcased to the on-disk slug.
        assert kb.workspaces_root(board="projA") == (
            fresh_home / "kanban" / "boards" / "proja" / "workspaces"
        )

    def test_logs_per_board(self, fresh_home):
        assert kb.worker_logs_dir() == fresh_home / "kanban" / "logs"
        assert kb.worker_logs_dir(board="other") == (
            fresh_home / "kanban" / "boards" / "other" / "logs"
        )

    def test_env_var_db_override_still_wins(self, fresh_home, tmp_path, monkeypatch):
        """``HERMES_KANBAN_DB`` pins the file regardless of board= arg."""
        forced = tmp_path / "custom.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
        assert kb.kanban_db_path() == forced
        assert kb.kanban_db_path(board="ignored") == forced

    def test_env_var_workspaces_override(self, fresh_home, tmp_path, monkeypatch):
        forced = tmp_path / "ws"
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(forced))
        assert kb.workspaces_root(board="any") == forced


# ---------------------------------------------------------------------------
# Current-board resolution
# ---------------------------------------------------------------------------

class TestCurrentBoard:
    def test_default_when_unset(self, fresh_home):
        assert kb.get_current_board() == "default"

    def test_env_var_takes_precedence(self, fresh_home, monkeypatch):
        # Create the board so the env-var value is honoured (get_current_board
        # trusts env-var validity, but the resolution chain doesn't require
        # the board to exist; we just test that env trumps).
        kb.create_board("envboard")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "envboard")
        assert kb.get_current_board() == "envboard"

    def test_file_pointer_honoured(self, fresh_home):
        kb.create_board("filepick")
        kb.set_current_board("filepick")
        assert kb.get_current_board() == "filepick"

    def test_stale_file_pointer_falls_back_to_default(self, fresh_home):
        current = fresh_home / "kanban" / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("missing-board\n", encoding="utf-8")

        assert kb.get_current_board() == "default"
        assert not kb.board_exists("missing-board")
        assert [b["slug"] for b in kb.list_boards()] == ["default"]

    def test_empty_board_dir_does_not_count_as_existing(self, fresh_home):
        ghost = fresh_home / "kanban" / "boards" / "ghost"
        ghost.mkdir(parents=True)

        assert not kb.board_exists("ghost")
        assert [b["slug"] for b in kb.list_boards()] == ["default"]

    def test_env_beats_file(self, fresh_home, monkeypatch):
        kb.create_board("a")
        kb.create_board("b")
        kb.set_current_board("a")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "b")
        assert kb.get_current_board() == "b"

    def test_stale_env_falls_through_to_file_pointer(self, fresh_home, monkeypatch):
        kb.create_board("persisted")
        kb.set_current_board("persisted")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "missing-board")
        assert kb.get_current_board() == "persisted"

    def test_invalid_env_falls_through(self, fresh_home, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "!!bad!!")
        # Should not crash — falls through to default.
        assert kb.get_current_board() == "default"

    def test_clear_current_board(self, fresh_home):
        kb.create_board("x")
        kb.set_current_board("x")
        kb.clear_current_board()
        assert kb.get_current_board() == "default"

    def test_kanban_db_path_reads_current(self, fresh_home):
        """kanban_db_path() with no args respects the on-disk pointer."""
        kb.create_board("my-proj")
        kb.set_current_board("my-proj")
        expected = fresh_home / "kanban" / "boards" / "my-proj" / "kanban.db"
        assert kb.kanban_db_path() == expected


# ---------------------------------------------------------------------------
# Board CRUD
# ---------------------------------------------------------------------------

class TestBoardCRUD:
    def test_create_and_list(self, fresh_home):
        assert [b["slug"] for b in kb.list_boards()] == ["default"]
        kb.create_board("foo", name="Foo Board", description="test")
        slugs = [b["slug"] for b in kb.list_boards()]
        assert slugs == ["default", "foo"]

    def test_create_is_idempotent(self, fresh_home):
        kb.create_board("bar")
        kb.create_board("bar")  # no error
        slugs = [b["slug"] for b in kb.list_boards()]
        assert slugs == ["default", "bar"]

    def test_create_does_not_mutate_existing_board_metadata(self, fresh_home):
        kb.create_board("bar", name="Original", runtime="goal")
        again = kb.create_board("bar", name="Replacement", runtime="company")

        assert again["name"] == "Original"
        assert again["runtime"]["mode"] == "goal"
        meta = kb.read_board_metadata("bar")
        assert meta["name"] == "Original"
        assert meta["runtime"]["mode"] == "goal"

    def test_create_writes_metadata(self, fresh_home):
        meta = kb.create_board(
            "baz",
            name="Baz",
            description="desc",
            icon="📦",
            color="#abcdef",
        )
        assert meta["slug"] == "baz"
        assert meta["name"] == "Baz"
        assert meta["icon"] == "📦"
        # Round-trip via read_board_metadata.
        again = kb.read_board_metadata("baz")
        assert again["name"] == "Baz"
        assert again["description"] == "desc"
        assert again["icon"] == "📦"

    def test_create_defaults_to_objective_first_goal_scaffold(self, fresh_home):
        meta = kb.create_board(
            "factory",
            name="Factory",
            objective="Ship the release",
            success=["tests pass"],
            constraints=["no downtime"],
            dispatcher_profile="default",
            worker_profile="builder",
        )

        assert meta["runtime"]["mode"] == "goal"
        assert meta["runtime"]["dispatcher"]["profile"] == "default"
        assert meta["runtime"]["profiles"]["worker"] == "builder"
        assert meta["objective"] == {
            "statement": "Ship the release",
            "success": ["tests pass"],
            "failure": [],
            "constraints": ["no downtime"],
        }
        assert [s["key"] for s in meta["workflow"]["stages"]] == [
            "intake", "plan", "execute", "verify", "deliver", "improve",
        ]

    def test_create_accepts_board_operating_contract(self, fresh_home):
        contract = {
            "objective": {
                "statement": "Ship safely",
                "success": ["all proof collected"],
                "failure": ["unsafe outbound action"],
                "constraints": ["mock-only"],
            },
            "runtime": {
                "mode": "company",
                "dispatcher": {"profile": "ceo"},
                "profiles": {"ceo": "ceo", "optimizer": "opt", "worker": "worker"},
                "require_worker_envelopes": True,
                "require_provider_policy": True,
                "provider_policy": {"mock_research": {"provider": "fixture"}},
                "tool_policy": {"mock_research": {"required_toolsets": ["kanban"]}},
                "worker_envelopes": {
                    "worker": {
                        "capabilities": ["mock_research"],
                        "toolsets": ["kanban"],
                        "allowed_side_effects": ["none"],
                    }
                },
            },
            "workflow": {
                "id": "safe-flow",
                "goal_id": "safe-goal",
                "require_semantics": True,
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
                    }
                ],
            },
        }

        meta = kb.create_board("contract", runtime="company", contract=contract)

        assert meta["objective"]["failure"] == ["unsafe outbound action"]
        assert meta["runtime"]["mode"] == "company"
        assert meta["runtime"]["dispatcher"]["profile"] == "ceo"
        assert meta["runtime"]["provider_policy"]["mock_research"]["provider"] == "fixture"
        assert meta["runtime"]["worker_envelopes"]["worker"]["capabilities"] == ["mock_research"]
        action = meta["workflow"]["stages"][0]["actions"][0]
        assert action["required_capabilities"] == ["mock_research"]
        assert action["required_proof"] == ["mock_report"]

    def test_business_launch_review_keeps_rough_goal_in_universal_intake(self, fresh_home):
        result = kb.review_business_launch_contract(
            "land-draft",
            rough_goal="Launch a land wholesaling business",
            create_if_missing=True,
        )

        assert result["ok"] is False
        assert result["status"] == "needs_clarification"
        assert result["launch_phase"] == "contract_review"
        assert result["questions"] == []
        assert result["launch_intake"]["state"] == "clarifying"
        assert result["launch_intake"]["workflow_type"] == "agentic_workflow"
        assert result["launch_intake"]["question_generation"]["required"] is True
        assert result["launch_intake"]["question_generation"]["mode"] in {
            "model_generated",
            "deterministic_fallback",
            "server_generated",
        }
        assert "keyword routing" in result["launch_intake"]["question_generation"]["system_prompt"]
        assert result["launch_intake"]["assumptions"]
        meta = kb.read_board_metadata("land-draft")
        assert meta["launch_phase"] == "contract_review"
        assert meta["business_contract"]["objective"]["statement"] == (
            "Launch a land wholesaling business"
        )
        assert "workflow" not in meta["business_contract"]
        assert "land_wholesaling_v1" not in json.dumps(meta["business_contract"])
        assert meta["contract_readiness"]["questions"] == []

    def test_rough_launch_for_any_goal_uses_model_intake_prompt(self, fresh_home):
        result = kb.review_business_launch_contract(
            "research-draft",
            rough_goal="I need agents to run a weekly research pipeline",
            create_if_missing=True,
        )

        assert result["ok"] is False
        assert result["status"] == "needs_clarification"
        assert result["launch_phase"] == "contract_review"
        assert result["launch_intake"]["workflow_type"] == "agentic_workflow"
        assert result["questions"] == []
        assert result["readiness"]["question_generation"]["required"] is True
        assert kb.read_board_metadata("research-draft")["contract_readiness"]["questions"] == []
        assert "workflow.stages" in result["readiness"]["missing"]

    def test_rough_launch_uses_model_prompt_instead_of_domain_questions(self, fresh_home):
        result = kb.review_business_launch_contract(
            "life-insurance-recruiting",
            rough_goal="I want to get more recruits for my life insurance business",
            create_if_missing=True,
        )

        generation = result["launch_intake"]["question_generation"]
        prompt_text = generation["system_prompt"].lower()
        questions_text = " ".join(result["questions"]).lower()

        assert result["status"] == "needs_clarification"
        assert result["launch_phase"] == "contract_review"
        assert result["launch_intake"]["workflow_type"] == "agentic_workflow"
        assert result["questions"] == []
        assert generation["required"] is True
        assert generation["mode"] in {
            "model_generated",
            "deterministic_fallback",
            "server_generated",
        }
        assert generation["input"]["rough_goal"] == (
            "I want to get more recruits for my life insurance business"
        )
        assert "fixed" in prompt_text
        assert "industry templates" in prompt_text
        assert "prewritten question lists" in prompt_text
        assert "recruit" not in prompt_text
        for technical_term in (
            "dispatcher",
            "optimizer",
            "worker profile",
            "event loop",
            "entities",
            "provider policy",
            "schema",
            "contract",
        ):
            assert technical_term not in questions_text

    def test_intake_answers_enter_recursive_assessment_before_contract_draft(self, fresh_home):
        kb.review_business_launch_contract(
            "weak-answer-intake",
            rough_goal="I want to get more recruits for my life insurance business",
            create_if_missing=True,
        )

        result = kb.review_business_launch_contract(
            "weak-answer-intake",
            intake_answers="I just want more people, do whatever.",
        )

        intake = result["launch_intake"]
        assert result["status"] == "needs_clarification"
        assert result["launch_phase"] == "contract_review"
        assert result["questions"] == []
        assert intake["state"] == "assessing_answers"
        assert intake["clarification_round"] == 1
        assert intake["answers"]["raw"] == "I just want more people, do whatever."
        assert intake["answer_quality"]["status"] == "needs_assessment"
        assert intake["answer_quality"]["sufficient"] is False
        assert intake["answer_assessment"]["required"] is True
        assert intake["answer_assessment"]["mode"] == "model_assessed"
        assert result["readiness"]["answer_assessment"]["required"] is True
        meta = kb.read_board_metadata("weak-answer-intake")
        assert meta["business_contract"]["launch_intake"]["state"] == "assessing_answers"

    def test_duplicate_intake_answers_do_not_advance_assessment_round(self, fresh_home):
        kb.review_business_launch_contract(
            "duplicate-answer-intake",
            rough_goal="I want to get more recruits for my life insurance business",
            create_if_missing=True,
        )
        kb.review_business_launch_contract(
            "duplicate-answer-intake",
            intake_answers="I want 10 qualified recruiting conversations per month.",
        )

        with pytest.raises(ValueError, match="intake_answers already submitted"):
            kb.review_business_launch_contract(
                "duplicate-answer-intake",
                intake_answers="I want 10 qualified recruiting conversations per month.",
            )

        meta = kb.read_board_metadata("duplicate-answer-intake")
        intake = meta["business_contract"]["launch_intake"]
        assert intake["clarification_round"] == 1
        assert len(intake["answer_history"]) == 1

        changed = kb.review_business_launch_contract(
            "duplicate-answer-intake",
            intake_answers="I want 10 qualified recruiting conversations per month from licensed agents.",
        )
        assert changed["launch_intake"]["clarification_round"] == 2

    def test_duplicate_intake_answers_normalize_generic_answer_keys(self, fresh_home):
        kb.review_business_launch_contract(
            "duplicate-generic-answer-intake",
            rough_goal="I want to get more recruits for my life insurance business",
            create_if_missing=True,
        )
        first = kb.review_business_launch_contract(
            "duplicate-generic-answer-intake",
            intake_answers={"owner_response": "I just want more partners, do whatever."},
        )
        assert first["launch_intake"]["answers"] == {
            "raw": "I just want more partners, do whatever."
        }

        with pytest.raises(ValueError, match="intake_answers already submitted"):
            kb.review_business_launch_contract(
                "duplicate-generic-answer-intake",
                intake_answers={"answers": "I just want more partners, do whatever."},
            )

        intake = kb.read_board_metadata("duplicate-generic-answer-intake")["business_contract"]["launch_intake"]
        assert intake["clarification_round"] == 1
        assert len(intake["answer_history"]) == 1

    def test_structured_clear_intake_answers_draft_contract_for_owner_review(self, fresh_home):
        kb.review_business_launch_contract(
            "clear-answer-intake",
            rough_goal="Launch an ongoing referral partner workflow",
            create_if_missing=True,
        )

        result = kb.review_business_launch_contract(
            "clear-answer-intake",
            intake_answers={
                "success_criteria": (
                    "Success means producing 20 qualified referral partner prospects, "
                    "drafting outreach for owner approval, and booking 5 qualified "
                    "partner conversations per month."
                ),
                "good_partners": (
                    "Good partners are CPAs, tax preparers, payroll firms, business "
                    "attorneys, and local business coaches serving small businesses."
                ),
                "allowed_sources": (
                    "Hermes may research LinkedIn, Google Maps, public websites, "
                    "and existing notes if available."
                ),
                "prohibited_actions": (
                    "Hermes may not send outreach, spend money, use personal accounts, "
                    "make pricing promises, or represent itself as the owner without approval."
                ),
                "workflow": (
                    "Find prospects, qualify fit, draft a personalized message, wait for "
                    "owner approval, send only after approval if a sending channel is "
                    "authorized, track replies, negotiate next steps, and stop when a "
                    "partner signs, says no, is unqualified, or needs owner judgment."
                ),
                "proof_requirements": (
                    "Proof should include the prospect list, why each is qualified, "
                    "proposed message, approval status, sent/reply log, next action, "
                    "and weekly summary."
                ),
                "owner_escalation_rules": (
                    "Stop and ask for competitors, unclear fit, complaints, legal or "
                    "financial claims, negative replies, or anything involving money."
                ),
            },
        )

        intake = result["launch_intake"]
        assert result["ok"] is True
        assert result["status"] == "ready_for_owner_review"
        assert result["launch_phase"] == "contract_review"
        assert intake["state"] == "ready_for_owner_review"
        assert intake["answer_quality"]["sufficient"] is True
        assert intake["answer_quality"]["answers_hash"]
        assert intake["answer_quality"]["round"] == 1
        # New path (Phase 1): sufficiency is backed by the deterministic
        # structural coverage gate, not the old >=5-field / >=300-char heuristic.
        assert intake["answer_quality"]["coverage_score"] >= 0.6
        assert intake["answer_quality"]["coverage_dimensions"]
        assert intake["coverage"]["passed"] is True
        assert intake["coverage"]["score"] >= 0.6
        assert result["contract"]["workflow"]["stages"]
        assert result["contract"]["entities"]
        assert result["contract"]["event_loops"]
        assert result["contract"]["approval_gates"]
        assert result["contract"]["proof_requirements"]
        assert result["contract"]["side_effect_policy"]
        assert result["contract"]["escalation_paths"]

    def test_generic_but_long_answers_no_longer_auto_draft(self, fresh_home):
        """Phase 1 regression: answers that passed the OLD honor-system gate
        (>=5 fields, >=300 chars) but are generic filler must NOT auto-draft a
        contract. The deterministic structural coverage gate blocks them."""
        kb.review_business_launch_contract(
            "generic-filler-intake",
            rough_goal="Launch a partner workflow",
            create_if_missing=True,
        )
        result = kb.review_business_launch_contract(
            "generic-filler-intake",
            intake_answers={
                "a": "We want to do the thing well and make it good for everyone involved over time.",
                "b": "It should be nice and helpful and work the way we hope it will work for us.",
                "c": "Please just handle it however seems best and keep things moving along smoothly.",
                "d": "Do whatever you think is right and try to make people happy with the results.",
                "e": "Keep it simple and easy and do not overthink any of the small details here.",
            },
        )
        # Coverage fails -> no synthesized workflow, not ready for owner review.
        assert result["ok"] is False
        assert result["status"] != "ready_for_owner_review"
        assert result["launch_phase"] == "contract_review"
        assert not result["contract"].get("workflow")

    def test_intake_contract_draft_requires_quality_evidence(self, fresh_home):
        kb.review_business_launch_contract(
            "draft-evidence-intake",
            rough_goal="Launch a partner referral workflow",
            create_if_missing=True,
        )
        kb.review_business_launch_contract(
            "draft-evidence-intake",
            intake_answers={
                "outcome": "Book 5 qualified partner conversations per month.",
                "allowed_channels": "Research only; outreach drafts need owner approval.",
                "stop_conditions": "Stop for complaints, money, or legal claims.",
            },
        )
        contract = _launch_ready_contract()
        contract["launch_intake"] = {
            "question_generation": {"required": True, "mode": "model_generated"},
            "answer_quality": {"status": "sufficient", "sufficient": True},
        }

        reviewed = kb.review_business_launch_contract(
            "draft-evidence-intake",
            contract=contract,
        )

        assert reviewed["ok"] is False
        assert "launch_intake.answer_quality.evidence" in reviewed["readiness"]["missing"]
        assert reviewed["launch_phase"] == "contract_review"

    def test_intake_contract_draft_is_bound_to_latest_answers(self, fresh_home):
        answers = {
            "outcome": "Book 5 qualified partner conversations per month.",
            "allowed_channels": "Research only; outreach drafts need owner approval.",
            "stop_conditions": "Stop for complaints, money, or legal claims.",
        }
        kb.review_business_launch_contract(
            "draft-bound-intake",
            rough_goal="Launch a partner referral workflow",
            create_if_missing=True,
        )
        kb.review_business_launch_contract(
            "draft-bound-intake",
            intake_answers=answers,
        )
        contract = _launch_ready_contract()
        contract["launch_intake"] = {
            "question_generation": {"required": True, "mode": "model_generated"},
            "answer_quality": {
                "status": "sufficient",
                "sufficient": True,
                "evidence": "Owner gave measurable outcome, allowed channels, and stop conditions.",
            },
        }

        reviewed = kb.review_business_launch_contract(
            "draft-bound-intake",
            contract=contract,
        )

        expected_hash = kb._launch_intake_answers_hash(answers)
        assert reviewed["ok"] is True
        assert reviewed["launch_intake"]["answer_quality"]["answers_hash"] == expected_hash
        assert reviewed["launch_intake"]["answer_quality"]["round"] == 1
        assert reviewed["launch_intake"]["state"] == "ready_for_owner_review"

        stale = _launch_ready_contract()
        stale["launch_intake"] = {
            "question_generation": {"required": True, "mode": "model_generated"},
            "answer_quality": {
                "status": "sufficient",
                "sufficient": True,
                "evidence": "Owner gave measurable outcome, allowed channels, and stop conditions.",
                "answers_hash": "stale",
            },
        }
        with pytest.raises(ValueError, match="answers_hash does not match"):
            kb.review_business_launch_contract("draft-bound-intake", contract=stale)

        token = kb.issue_board_launch_approval_token(
            "draft-bound-intake",
            contract=contract,
            approved_by="owner",
            approval_evidence={"source": "owner approved interpreted intake contract"},
            owner_authority_confirmed=True,
        )["token"]
        activated = kb.review_business_launch_contract(
            "draft-bound-intake",
            contract=contract,
            approve=True,
            approval_token=token,
        )
        assert activated["launch_phase"] == "active"
        assert activated["approval"]["approval_token_id"]

    def test_partial_launch_answers_recompute_targeted_questions(self, fresh_home):
        contract = {
            "objective": {
                "statement": "Run a support escalation workflow",
                "success": ["urgent customer issues are resolved"],
                "failure": ["agents keep working after a customer opts out"],
                "constraints": ["do not issue refunds without approval"],
            },
            "launch_intake": {
                "state": "clarifying",
                "questions": [
                    "What kind of business or recurring operation should this board run?"
                ],
            },
        }

        result = kb.review_business_launch_contract(
            "partial-intake",
            contract=contract,
            create_if_missing=True,
        )

        assert result["status"] == "needs_clarification"
        assert result["questions"]
        assert len(result["questions"]) <= 6
        assert "What kind of business or recurring operation should this board run?" not in result["questions"]
        assert any("final call" in question.lower() for question in result["questions"])

    def test_ready_contract_with_assumptions_waits_for_owner_review(self, fresh_home):
        contract = _launch_ready_contract()
        contract["assumptions"] = ["Owner approval confirms the initial operating plan."]

        result = kb.review_business_launch_contract(
            "owner-review",
            contract=contract,
            create_if_missing=True,
        )

        assert result["ok"] is True
        assert result["status"] == "ready_for_owner_review"
        assert result["launch_phase"] == "contract_review"
        assert result["assumptions"] == ["Owner approval confirms the initial operating plan."]
        status = kb.validate_board_launch_readiness("owner-review")
        assert status["dispatch_enabled"] is False
        assert status["readiness"]["status"] == "ready_for_owner_review"

    def test_optimizer_or_approved_absence_required_for_non_company_runtime(self, fresh_home):
        contract = json.loads(json.dumps(_launch_ready_contract()))
        contract["runtime"]["mode"] = "goal"
        del contract["runtime"]["profiles"]["optimizer"]

        missing = kb.validate_business_runtime_contract(contract)

        assert missing["ok"] is False
        assert "runtime.profiles.optimizer" in missing["missing"]

        contract["runtime"]["optimizer_policy"] = {
            "disabled": True,
            "approved_by": "owner",
            "reason": "Owner approved direct dispatcher-to-worker operation for this board.",
        }
        ready = kb.validate_business_runtime_contract(contract)

        assert ready["ok"] is True

    def test_ready_contract_with_rough_goal_syncs_intake_state_and_activates_with_token(self, fresh_home):
        contract = _launch_ready_contract()
        rough_goal = "I need agents to run this workflow"
        contract["launch_intake"] = {
            "source": "rough_goal",
            "rough_goal": rough_goal,
            "question_generation": {"required": True, "mode": "model_generated"},
            "assumptions": ["Owner answered launch-intake questions clearly."],
            "answer_quality": {
                "status": "sufficient",
                "sufficient": True,
                "evidence": "Owner answered launch-intake questions clearly.",
            },
        }
        reviewed = kb.review_business_launch_contract(
            "rough-ready",
            contract=contract,
            rough_goal=rough_goal,
            create_if_missing=True,
        )

        assert reviewed["ok"] is True
        assert reviewed["status"] == "ready_for_owner_review"
        assert reviewed["launch_phase"] == "contract_review"
        assert reviewed["launch_intake"]["state"] == "ready_for_owner_review"
        assert reviewed["launch_intake"]["owner_review_required"] is True

        token = kb.issue_board_launch_approval_token(
            "rough-ready",
            contract=contract,
            rough_goal=rough_goal,
            approved_by="owner",
            approval_evidence={"source": "owner approved interpreted contract"},
            owner_authority_confirmed=True,
        )["token"]
        activated = kb.review_business_launch_contract(
            "rough-ready",
            contract=contract,
            rough_goal=rough_goal,
            approve=True,
            approval_token=token,
        )

        assert activated["launch_phase"] == "active"
        assert activated["approval"]["approval_token_id"]

    def test_launch_ready_contract_can_activate_board(self, fresh_home):
        result = _approve_launch_contract(
            "land-ready",
            _launch_ready_contract(),
        )

        assert result["ok"] is True
        assert result["launch_phase"] == "active"
        assert result["launch_review_id"]
        assert result["approval"]["status"] == "approved"
        assert result["approval"]["approval_token_id"]
        assert result["approval"]["contract_hash"]
        meta = kb.read_board_metadata("land-ready")
        assert meta["launch_phase"] == "active"
        assert meta["launch_review_id"] == result["launch_review_id"]
        assert meta["launch_approval"]["approved_by"] == "owner"
        assert meta["contract_readiness"]["ok"] is True
        assert meta["contract_version"] == 1
        assert meta["launch_approval_tokens"] == []
        with kb.connect(board="land-ready") as conn:
            token_row = conn.execute(
                "SELECT status, kind FROM board_launch_approval_tokens WHERE id = ?",
                (result["approval"]["approval_token_id"],),
            ).fetchone()
            review_row = conn.execute(
                "SELECT status, kind FROM board_launch_reviews WHERE id = ?",
                (result["launch_review_id"],),
            ).fetchone()
        assert dict(token_row) == {"status": "consumed", "kind": "launch_review"}
        assert dict(review_row) == {"status": "approved", "kind": "launch_review"}
        assert kb.board_dispatch_gate("land-ready")["ok"] is True

    def test_launch_approval_requires_token(self, fresh_home):
        contract = _launch_ready_contract()
        kb.review_business_launch_contract(
            "missing-approval",
            contract=contract,
            create_if_missing=True,
        )

        with pytest.raises(ValueError, match="approval_token is required"):
            kb.review_business_launch_contract(
                "missing-approval",
                contract=contract,
                approve=True,
            )

        assert kb.read_board_metadata("missing-approval")["launch_phase"] == "contract_review"

    def test_failed_launch_activation_write_leaves_token_pending(self, fresh_home, monkeypatch):
        contract = _launch_ready_contract()
        kb.review_business_launch_contract(
            "write-fail-approval",
            contract=contract,
            create_if_missing=True,
        )
        issued = kb.issue_board_launch_approval_token(
            "write-fail-approval",
            contract=contract,
            approved_by="owner",
            approval_evidence={"source": "write-fail"},
            owner_authority_confirmed=True,
        )
        original_write = kb.write_board_metadata

        def fail_active_write(board, *args, **kwargs):
            if kwargs.get("launch_phase") == "active":
                raise RuntimeError("simulated metadata write failure")
            return original_write(board, *args, **kwargs)

        monkeypatch.setattr(kb, "write_board_metadata", fail_active_write)

        with pytest.raises(RuntimeError, match="simulated metadata write failure"):
            kb.review_business_launch_contract(
                "write-fail-approval",
                contract=contract,
                approve=True,
                approval_token=issued["token"],
            )

        with kb.connect(board="write-fail-approval") as conn:
            token_row = conn.execute(
                "SELECT status, consumed_at FROM board_launch_approval_tokens WHERE id = ?",
                (issued["token_id"],),
            ).fetchone()
            review_row = conn.execute(
                "SELECT COUNT(*) AS count FROM board_launch_reviews",
            ).fetchone()
        assert token_row["status"] == "pending"
        assert token_row["consumed_at"] is None
        assert review_row["count"] == 0

    def test_launch_approval_token_expires(self, fresh_home):
        contract = _launch_ready_contract()
        kb.review_business_launch_contract(
            "expired-approval",
            contract=contract,
            create_if_missing=True,
        )
        token = kb.issue_board_launch_approval_token(
            "expired-approval",
            contract=contract,
            approved_by="owner",
            approval_evidence={"source": "expired"},
            ttl_seconds=1,
            owner_authority_confirmed=True,
        )
        with kb.connect(board="expired-approval") as conn:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE board_launch_approval_tokens SET expires_at = 1 WHERE id = ?",
                    (token["token_id"],),
                )

        with pytest.raises(ValueError, match="approval_token has expired"):
            kb.review_business_launch_contract(
                "expired-approval",
                contract=contract,
                approve=True,
                approval_token=token["token"],
            )

        with kb.connect(board="expired-approval") as conn:
            token_record = conn.execute(
                "SELECT status FROM board_launch_approval_tokens WHERE id = ?",
                (token["token_id"],),
            ).fetchone()
        assert token_record["status"] == "expired"

    def test_launch_approval_token_is_one_time(self, fresh_home):
        contract = _launch_ready_contract()
        kb.review_business_launch_contract(
            "one-time-approval",
            contract=contract,
            create_if_missing=True,
        )
        token = kb.issue_board_launch_approval_token(
            "one-time-approval",
            contract=contract,
            approved_by="owner",
            approval_evidence={"source": "one-time"},
            owner_authority_confirmed=True,
        )["token"]
        kb.review_business_launch_contract(
            "one-time-approval",
            contract=contract,
            approve=True,
            approval_token=token,
        )

        with pytest.raises(ValueError, match="already been used"):
            kb.review_business_launch_contract(
                "one-time-approval",
                contract=contract,
                approve=True,
                approval_token=token,
            )

    def test_launch_approval_token_concurrent_double_consume(self, fresh_home):
        contract = _launch_ready_contract()
        kb.review_business_launch_contract(
            "concurrent-approval",
            contract=contract,
            create_if_missing=True,
        )
        token = kb.issue_board_launch_approval_token(
            "concurrent-approval",
            contract=contract,
            approved_by="owner",
            approval_evidence={"source": "concurrent"},
            owner_authority_confirmed=True,
        )["token"]
        barrier = threading.Barrier(2)

        def activate_once():
            barrier.wait(timeout=5)
            try:
                result = kb.review_business_launch_contract(
                    "concurrent-approval",
                    contract=contract,
                    approve=True,
                    approval_token=token,
                )
            except Exception as exc:
                return ("error", str(exc))
            return ("ok", result["launch_phase"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: activate_once(), range(2)))

        assert sorted(kind for kind, _ in results) == ["error", "ok"]
        assert any("already been used" in message for kind, message in results if kind == "error")
        meta = kb.read_board_metadata("concurrent-approval")
        assert meta["launch_phase"] == "active"
        assert meta["launch_approval"]["status"] == "approved"
        with kb.connect(board="concurrent-approval") as conn:
            token_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM board_launch_approval_tokens GROUP BY status"
            ).fetchall()
            review_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM board_launch_reviews GROUP BY status"
            ).fetchall()
        assert {row["status"]: row["count"] for row in token_rows} == {"consumed": 1}
        assert {row["status"]: row["count"] for row in review_rows} == {"approved": 1}

    def test_launch_approval_token_is_bound_to_contract_hash(self, fresh_home):
        contract = _launch_ready_contract()
        kb.review_business_launch_contract(
            "hash-bound",
            contract=contract,
            create_if_missing=True,
        )
        token = kb.issue_board_launch_approval_token(
            "hash-bound",
            contract=contract,
            approved_by="owner",
            approval_evidence={"source": "hash-bound"},
            owner_authority_confirmed=True,
        )["token"]
        changed = _launch_ready_contract()
        changed["objective"]["success"] = ["changed success target"]

        with pytest.raises(ValueError, match="different contract"):
            kb.review_business_launch_contract(
                "hash-bound",
                contract=changed,
                approve=True,
                approval_token=token,
            )

        assert kb.read_board_metadata("hash-bound")["launch_phase"] == "contract_review"

    def test_launch_approval_token_is_bound_to_board(self, fresh_home):
        contract = _launch_ready_contract()
        kb.review_business_launch_contract(
            "board-a",
            contract=contract,
            create_if_missing=True,
        )
        kb.review_business_launch_contract(
            "board-b",
            contract=contract,
            create_if_missing=True,
        )
        token = kb.issue_board_launch_approval_token(
            "board-a",
            contract=contract,
            approved_by="owner",
            approval_evidence={"source": "board-a-only"},
            owner_authority_confirmed=True,
        )["token"]

        with pytest.raises(ValueError, match="approval_token is not valid for this board"):
            kb.review_business_launch_contract(
                "board-b",
                contract=contract,
                approve=True,
                approval_token=token,
            )

    def test_launch_gate_rejects_synthetic_approval_without_consumed_token(self, fresh_home):
        contract = _launch_ready_contract()
        kb.create_board(
            "synthetic-token",
            contract=contract,
            launch_phase="active",
        )
        kb.write_board_metadata(
            "synthetic-token",
            launch_review_id="lr_fake",
            launch_approval={
                "id": "lr_fake",
                "type": "launch_review",
                "status": "approved",
                "approved_by": "owner",
                "evidence": {"source": "synthetic"},
                "approval_token_id": "lat_fake",
                "contract_hash": kb._business_contract_hash(contract),
                "contract_version": 1,
            },
            launch_approval_tokens=[
                {
                    "id": "lat_fake",
                    "status": "consumed",
                    "kind": "launch_review",
                    "board": "synthetic-token",
                    "contract_version": 1,
                    "contract_hash": kb._business_contract_hash(contract),
                    "approved_by": "owner",
                    "evidence": {"source": "synthetic"},
                    "created_at": 1,
                    "expires_at": 9999999999,
                    "token_hash": "fake",
                    "consumed_at": 2,
                }
            ],
        )

        gate = kb.board_dispatch_gate("synthetic-token")

        assert gate["ok"] is False
        assert gate["launch_approved"] is False
        assert "launch_review_missing" in {b["code"] for b in gate["blockers"]}

    def test_launch_gate_rejects_synthetic_approval_without_evidence(self, fresh_home):
        kb.create_board(
            "synthetic-approval",
            contract=_launch_ready_contract(),
            launch_phase="active",
        )
        kb.write_board_metadata(
            "synthetic-approval",
            launch_review_id="lr_fake",
            launch_approval={
                "id": "lr_fake",
                "type": "launch_review",
                "status": "approved",
                "approved_by": "owner",
                "evidence": {"source": "synthetic"},
                "contract_version": 1,
            },
        )

        gate = kb.board_dispatch_gate("synthetic-approval")

        assert gate["ok"] is False
        assert gate["launch_approved"] is False
        assert "launch_review_missing" in {b["code"] for b in gate["blockers"]}

    def test_launch_review_accepts_exit_criteria_on_every_stage(self, fresh_home):
        contract = _launch_ready_contract()
        contract["workflow"]["stages"][2]["exit_criteria"] = [
            {"transition": "close", "evidence_required": ["outcome_archived"]}
        ]

        readiness = kb.validate_business_runtime_contract(contract)

        assert readiness["ok"] is True
        assert "workflow.exit_criteria" not in readiness["missing"]

    def test_launch_review_requires_nonterminal_stage_exit_criteria(self, fresh_home):
        contract = _launch_ready_contract()
        contract["workflow"]["stages"][1]["exit_criteria"] = []

        readiness = kb.validate_business_runtime_contract(contract)

        assert readiness["ok"] is False
        assert "workflow.stages.negotiate.exit_criteria" in readiness["missing"]

    def test_managed_active_board_requires_approved_launch_review(self, fresh_home):
        kb.create_board(
            "unreviewed-active",
            contract=_launch_ready_contract(),
            launch_phase="active",
        )

        gate = kb.board_dispatch_gate("unreviewed-active")

        assert gate["ok"] is False
        assert gate["launch_approved"] is False
        assert "launch_review_missing" in {b["code"] for b in gate["blockers"]}

        conn = kb.connect(board="unreviewed-active")
        try:
            with pytest.raises(ValueError, match="approved launch review"):
                kb.create_task(
                    conn,
                    title="should not create executable work",
                    assignee="worker",
                    workstream_id="seller-conversion",
                    stage_key="source",
                    action_key="capture_lead",
                )
        finally:
            conn.close()

    def test_company_launch_requires_optimizer_or_approved_disable_policy(self, fresh_home):
        missing_optimizer = _launch_ready_contract()
        missing_optimizer["runtime"]["profiles"].pop("optimizer")

        blocked = kb.review_business_launch_contract(
            "no-optimizer",
            contract=missing_optimizer,
            create_if_missing=True,
            approve=True,
            author="owner",
            approved_by="owner",
            approval_evidence={"source": "optimizer-disable-test"},
        )

        assert blocked["ok"] is False
        assert blocked["launch_phase"] == "contract_review"
        assert "runtime.profiles.optimizer" in blocked["readiness"]["missing"]
        assert blocked["launch_review_id"] is None

        approved_disable = _launch_ready_contract()
        approved_disable["runtime"]["profiles"].pop("optimizer")
        approved_disable["runtime"]["optimizer_policy"] = {
            "enabled": False,
            "approved_by": "owner",
            "reason": "manual review during launch",
        }

        activated = _approve_launch_contract(
            "optimizer-disabled",
            approved_disable,
            evidence_source="optimizer-disable-approval",
        )

        assert activated["ok"] is True
        assert activated["launch_phase"] == "active"
        assert kb.board_dispatch_gate("optimizer-disabled")["ok"] is True

    def test_company_dispatch_gate_requires_launch_readiness(self, fresh_home):
        kb.create_board(
            "company-not-ready",
            runtime="company",
            objective="Run a real business board",
            success=["work completes"],
            failure=["work launches without contract"],
            dispatcher_profile="ceo",
            optimizer_profile="optimizer",
            worker_profile="worker",
        )

        gate = kb.board_dispatch_gate("company-not-ready")

        assert gate["ok"] is False
        assert gate["managed"] is True
        assert "launch_readiness_failed" in {b["code"] for b in gate["blockers"]}

    def test_dispatch_is_gated_until_board_launch_is_active(self, fresh_home):
        kb.review_business_launch_contract(
            "paused-launch",
            rough_goal="Launch a vague business",
            create_if_missing=True,
        )
        conn = kb.connect(board="paused-launch")
        try:
            with pytest.raises(ValueError, match="only blocked or triage tasks"):
                kb.create_task(conn, title="should not dispatch", assignee="worker")
            tid = kb.create_task(
                conn,
                title="triage can collect launch details",
                assignee="worker",
                triage=True,
            )
            blocked_tid = kb.create_task(
                conn,
                title="blocked launch backlog",
                assignee="worker",
                initial_status="blocked",
            )
            result = kb.dispatch_once(conn, dry_run=True, board="paused-launch")
            with pytest.raises(ValueError, match="launch gate is closed"):
                kb.specify_triage_task(conn, tid, body="details collected")
            with pytest.raises(ValueError, match="launch gate is closed"):
                kb.decompose_triage_task(
                    conn,
                    tid,
                    root_assignee="worker",
                    children=[{"title": "child work", "assignee": "worker"}],
                )
        finally:
            conn.close()

        assert result.spawned == []
        assert result.launch_blocked[0]["launch_phase"] == "contract_review"
        assert tid not in [row[0] for row in result.spawned]
        assert blocked_tid not in [row[0] for row in result.spawned]

    def test_closed_launch_gate_blocks_direct_executable_transitions(self, fresh_home):
        kb.review_business_launch_contract(
            "closed-direct",
            rough_goal="Launch a vague business",
            create_if_missing=True,
        )
        conn = kb.connect(board="closed-direct")
        try:
            parent = kb.create_task(
                conn,
                title="blocked parent",
                assignee="worker",
                initial_status="blocked",
            )
            child = kb.create_task(
                conn,
                title="blocked child",
                assignee="worker",
                initial_status="blocked",
                parents=[parent],
            )

            assert kb.recompute_ready(conn) == 0
            assert kb.get_task(conn, parent).status == "blocked"

            assert kb.unblock_task(conn, child) is True
            assert kb.get_task(conn, child).status == "todo"
            ok, reason = kb.promote_task(
                conn,
                child,
                actor="tester",
                force=True,
                reason="direct bypass attempt",
            )
            assert ok is False
            assert "launch gate is closed" in reason
            assert kb.get_task(conn, child).status == "todo"

            assert kb.unblock_task(conn, parent) is False
            assert kb.get_task(conn, parent).status == "blocked"

            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
            assert kb.claim_task(conn, child) is None
            assert kb.get_task(conn, child).status == "ready"

            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (child,))
            assert kb.claim_review_task(conn, child) is None
            assert kb.get_task(conn, child).status == "review"
        finally:
            conn.close()

    def test_closed_launch_gate_reclaims_running_work_to_blocked(self, fresh_home):
        _approve_launch_contract(
            "closed-reclaim",
            _launch_ready_contract(),
            evidence_source="closed-reclaim-test",
        )
        conn = kb.connect(board="closed-reclaim")
        try:
            tid = kb.create_task(
                conn,
                title="running work",
                assignee="worker",
                workstream_id="seller-conversion",
                stage_key="source",
                action_key="capture_lead",
            )
            assert kb.claim_task(conn, tid) is not None
            kb.write_board_metadata("closed-reclaim", launch_phase="contract_review")

            assert kb.reclaim_task(conn, tid, reason="launch closed") is True
            assert kb.get_task(conn, tid).status == "blocked"
        finally:
            conn.close()

    def test_contract_amendment_applies_with_version_history(self, fresh_home):
        _approve_launch_contract(
            "amendable",
            _launch_ready_contract(),
            evidence_source="amendment-test",
        )
        amendment = kb.propose_board_contract_amendment(
            "amendable",
            patch={"objective": {"success": ["two signed contracts per month"]}},
            reason="tighten measurable success",
            author="tester",
            risk="low",
        )

        assert amendment["status"] == "pending"
        assert amendment["readiness"]["ok"] is True

        token = _issue_amendment_token(
            "amendable",
            amendment["id"],
            evidence_source="amendment-approval",
        )
        applied = kb.apply_board_contract_amendment(
            "amendable",
            amendment["id"],
            approval_token=token,
        )

        meta = kb.read_board_metadata("amendable")
        assert applied["contract_version"] == 2
        assert meta["contract_version"] == 2
        assert meta["objective"]["success"] == ["two signed contracts per month"]
        assert meta["contract_history"][0]["version"] == 1
        assert any(
            row.get("id") == amendment["id"] and row.get("status") == "applied"
            for row in meta["contract_amendments"]
        )

    def test_contract_amendment_apply_requires_approval_token(self, fresh_home):
        _approve_launch_contract(
            "amend-no-evidence",
            _launch_ready_contract(),
            evidence_source="initial-owner-approval",
        )
        amendment = kb.propose_board_contract_amendment(
            "amend-no-evidence",
            patch={"objective": {"success": ["two signed contracts per month"]}},
            reason="tighten measurable success",
            author="tester",
            risk="low",
        )

        with pytest.raises(ValueError, match="approval_token is required"):
            kb.apply_board_contract_amendment(
                "amend-no-evidence",
                amendment["id"],
            )

        with pytest.raises(ValueError, match="approval_token is required"):
            kb.apply_board_contract_amendment(
                "amend-no-evidence",
                amendment["id"],
                activate=False,
            )

    def test_contract_amendment_force_does_not_apply_non_ready_contract(self, fresh_home):
        _approve_launch_contract(
            "amend-no-force",
            _launch_ready_contract(),
            evidence_source="initial-owner-approval",
        )
        amendment = kb.propose_board_contract_amendment(
            "amend-no-force",
            patch={
                "workflow": {
                    "id": "not-ready",
                    "require_semantics": False,
                    "workstreams": [],
                    "stages": [{"key": "only", "actions": [], "exit_criteria": []}],
                }
            },
            reason="simulate incomplete launch amendment",
            author="tester",
            risk="medium",
        )

        with pytest.raises(ValueError, match="contract amendment is not launch-ready"):
            kb.apply_board_contract_amendment(
                "amend-no-force",
                amendment["id"],
                force=True,
            )

        meta = kb.read_board_metadata("amend-no-force")
        assert meta["contract_version"] == 1
        assert meta["launch_phase"] == "active"

    def test_contract_amendment_token_is_bound_to_amendment_id(self, fresh_home):
        _approve_launch_contract(
            "amend-token-bound",
            _launch_ready_contract(),
            evidence_source="initial-owner-approval",
        )
        patch = {"objective": {"success": ["two signed contracts per month"]}}
        first = kb.propose_board_contract_amendment(
            "amend-token-bound",
            patch=patch,
            reason="first target",
            author="tester",
            risk="low",
        )
        # Mint the token bound to `first` while it is still the sole live
        # pending amendment — re-proposing below supersedes it (P0-2), so the
        # token must be issued first to exercise the binding check.
        token = _issue_amendment_token(
            "amend-token-bound",
            first["id"],
            evidence_source="first-amendment-approval",
        )
        second = kb.propose_board_contract_amendment(
            "amend-token-bound",
            patch=patch,
            reason="second target",
            author="tester",
            risk="low",
        )

        # A token bound to `first` cannot apply the (now-live) `second`.
        with pytest.raises(ValueError, match="different amendment"):
            kb.apply_board_contract_amendment(
                "amend-token-bound",
                second["id"],
                approval_token=token,
            )

        assert kb.read_board_metadata("amend-token-bound")["contract_version"] == 1

    def test_launch_token_cannot_apply_amendment(self, fresh_home):
        contract = _launch_ready_contract()
        _approve_launch_contract(
            "cross-kind-launch-token",
            contract,
            evidence_source="initial-owner-approval",
        )
        launch_token = kb.issue_board_launch_approval_token(
            "cross-kind-launch-token",
            contract=contract,
            approved_by="owner",
            approval_evidence={"source": "wrong-kind"},
            owner_authority_confirmed=True,
        )["token"]
        amendment = kb.propose_board_contract_amendment(
            "cross-kind-launch-token",
            patch={"objective": {"success": ["two signed contracts per month"]}},
            reason="tighten measurable success",
            author="tester",
            risk="low",
        )

        with pytest.raises(ValueError, match="different approval kind"):
            kb.apply_board_contract_amendment(
                "cross-kind-launch-token",
                amendment["id"],
                approval_token=launch_token,
            )

    def test_amendment_token_cannot_approve_launch_review(self, fresh_home):
        contract = _launch_ready_contract()
        _approve_launch_contract(
            "cross-kind-amend-token",
            contract,
            evidence_source="initial-owner-approval",
        )
        amendment = kb.propose_board_contract_amendment(
            "cross-kind-amend-token",
            patch={"objective": {"success": ["two signed contracts per month"]}},
            reason="tighten measurable success",
            author="tester",
            risk="low",
        )
        amendment_token = _issue_amendment_token(
            "cross-kind-amend-token",
            amendment["id"],
            evidence_source="wrong-kind",
        )

        with pytest.raises(ValueError, match="different approval kind"):
            kb.review_business_launch_contract(
                "cross-kind-amend-token",
                contract=contract,
                approve=True,
                approval_token=amendment_token,
            )

    def test_contract_amendment_token_is_one_time(self, fresh_home):
        _approve_launch_contract(
            "amend-token-reuse",
            _launch_ready_contract(),
            evidence_source="initial-owner-approval",
        )
        patch = {"objective": {"success": ["two signed contracts per month"]}}
        first = kb.propose_board_contract_amendment(
            "amend-token-reuse",
            patch=patch,
            reason="first target",
            author="tester",
            risk="low",
        )
        token = _issue_amendment_token(
            "amend-token-reuse",
            first["id"],
            evidence_source="first-amendment-approval",
        )
        kb.apply_board_contract_amendment(
            "amend-token-reuse",
            first["id"],
            approval_token=token,
        )
        second = kb.propose_board_contract_amendment(
            "amend-token-reuse",
            patch=patch,
            reason="second target",
            author="tester",
            risk="low",
        )

        with pytest.raises(ValueError, match="already been used"):
            kb.apply_board_contract_amendment(
                "amend-token-reuse",
                second["id"],
                approval_token=token,
            )

    def test_reproposing_supersedes_prior_pending_amendment(self, fresh_home):
        # P0-2: a board iterating toward launch must not accumulate competing
        # pending amendments. Re-proposing on the same base version supersedes
        # the prior pending draft so exactly one live candidate remains.
        _approve_launch_contract(
            "stale-amend",
            _launch_ready_contract(),
            evidence_source="initial-owner-approval",
        )
        first = kb.propose_board_contract_amendment(
            "stale-amend",
            patch={"objective": {"success": ["two signed contracts per month"]}},
            reason="first target",
            author="tester",
            risk="low",
        )
        second = kb.propose_board_contract_amendment(
            "stale-amend",
            patch={"objective": {"success": ["three signed contracts per month"]}},
            reason="second target",
            author="tester",
            risk="low",
        )

        meta = kb.read_board_metadata("stale-amend")
        by_id = {a["id"]: a for a in meta["contract_amendments"]}
        assert by_id[first["id"]]["status"] == "superseded"
        assert by_id[first["id"]]["superseded_by"] == second["id"]
        assert by_id[second["id"]]["status"] == "pending"

        # The superseded draft can no longer be applied.
        with pytest.raises(ValueError, match="is not pending"):
            kb.apply_board_contract_amendment("stale-amend", first["id"])

        # The single live candidate applies cleanly, advancing to v2.
        token = _issue_amendment_token(
            "stale-amend",
            second["id"],
            evidence_source="second-owner-approval",
        )
        kb.apply_board_contract_amendment(
            "stale-amend",
            second["id"],
            approval_token=token,
        )
        meta = kb.read_board_metadata("stale-amend")
        assert meta["contract_version"] == 2
        assert meta["objective"]["success"] == ["three signed contracts per month"]

        # Defense-in-depth: the stale guard still rejects an amendment pinned to
        # the old from_version (simulated legacy drift), never applying it.
        candidate = by_id[second["id"]]["candidate_contract"]
        stale_amendments = list(meta.get("contract_amendments") or [])
        stale_amendments.append({
            "id": "ca_stalefixture",
            "status": "pending",
            "from_version": 1,
            "created_at": 0,
            "author": "tester",
            "reason": "stale leftover",
            "risk": "low",
            "patch": {"objective": {"success": ["four signed contracts per month"]}},
            "candidate_contract": candidate,
            "readiness": {"ok": True},
        })
        kb.write_board_metadata("stale-amend", contract_amendments=stale_amendments)
        with pytest.raises(ValueError, match="is stale"):
            kb.apply_board_contract_amendment("stale-amend", "ca_stalefixture")
        assert kb.read_board_metadata("stale-amend")["contract_version"] == 2

    def test_active_board_review_cannot_overwrite_contract(self, fresh_home):
        original = _launch_ready_contract()
        _approve_launch_contract(
            "active-review",
            original,
            evidence_source="initial-owner-approval",
        )
        changed = _launch_ready_contract()
        changed["objective"]["success"] = ["changed target"]

        with pytest.raises(ValueError, match="contract amendments"):
            kb.review_business_launch_contract("active-review", contract=changed)

        meta = kb.read_board_metadata("active-review")
        assert meta["objective"]["success"] == original["objective"]["success"]
        assert meta["contract_version"] == 1

    def test_create_kernel_board_writes_plain_metadata(self, fresh_home):
        meta = kb.create_board("plain", runtime="kernel", name="Plain")
        raw = json.loads(kb.board_metadata_path("plain").read_text(encoding="utf-8"))

        assert meta["objective"] is None
        assert meta["runtime"]["mode"] == "kernel"
        assert meta["workflow"] is None
        assert "objective" not in raw
        assert raw["runtime"]["mode"] == "kernel"
        assert "workflow" not in raw

    def test_create_kernel_board_can_store_custom_workflow(self, fresh_home):
        workflow = {"id": "custom", "stages": ["intake", "deliver"]}
        meta = kb.create_board("plain-flow", runtime="kernel", workflow=workflow)
        raw = json.loads(kb.board_metadata_path("plain-flow").read_text(encoding="utf-8"))

        assert meta["objective"] is None
        assert meta["runtime"]["mode"] == "kernel"
        assert [s["key"] for s in meta["workflow"]["stages"]] == ["intake", "deliver"]
        assert "objective" not in raw
        assert raw["runtime"]["mode"] == "kernel"
        assert raw["workflow"]["id"] == "custom"

    def test_remove_archive(self, fresh_home):
        kb.create_board("toremove")
        res = kb.remove_board("toremove")
        assert res["action"] == "archived"
        assert Path(res["new_path"]).exists()
        assert "toremove" not in [b["slug"] for b in kb.list_boards()]

    def test_remove_hard_delete(self, fresh_home):
        kb.create_board("nuke")
        d = kb.board_dir("nuke")
        assert d.exists()
        res = kb.remove_board("nuke", archive=False)
        assert res["action"] == "deleted"
        assert not d.exists()

    def test_remove_default_forbidden(self, fresh_home):
        with pytest.raises(ValueError, match="default"):
            kb.remove_board("default")

    def test_remove_nonexistent_raises(self, fresh_home):
        with pytest.raises(ValueError, match="does not exist"):
            kb.remove_board("nosuch")

    def test_remove_clears_current_pointer(self, fresh_home):
        kb.create_board("pinned")
        kb.set_current_board("pinned")
        kb.remove_board("pinned")
        assert kb.get_current_board() == "default"

    @pytest.mark.parametrize("archive", [True, False])
    def test_remove_clears_init_cache_for_recreated_db(self, fresh_home, archive):
        # Regression for #23833: poll loops that call connect(board=slug) right
        # after remove_board() recreate an empty kanban.db at the same path
        # (connect() does mkdir(exist_ok=True)). If _INITIALIZED_PATHS still
        # contains the resolved path, the CREATE TABLE pass is skipped and
        # downstream readers hit `no such table: task_events`.
        kb.create_board("recycle")
        # First connect populates _INITIALIZED_PATHS for this DB.
        with kb.connect(board="recycle") as conn:
            kb.create_task(conn, title="t1", assignee="dev")
        db_path = kb.board_dir("recycle") / "kanban.db"
        assert str(db_path.resolve()) in kb._INITIALIZED_PATHS

        kb.remove_board("recycle", archive=archive)
        # remove_board must drop the cache entry so a re-create through
        # connect() gets a fresh schema-init pass.
        assert str(db_path.resolve()) not in kb._INITIALIZED_PATHS

        # Simulate the event-stream poll: re-open the same slug. connect()
        # recreates the directory + empty .db; the schema must be re-applied.
        with kb.connect(board="recycle") as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "task_events" in tables
        assert "tasks" in tables

    def test_rename_updates_metadata(self, fresh_home):
        kb.create_board("slug-immutable")
        kb.write_board_metadata("slug-immutable", name="New Display Name")
        assert kb.read_board_metadata("slug-immutable")["name"] == "New Display Name"
        # Slug must not change.
        assert kb.board_exists("slug-immutable")


# ---------------------------------------------------------------------------
# Connection isolation
# ---------------------------------------------------------------------------

class TestConnectionIsolation:
    def test_tasks_do_not_leak_across_boards(self, fresh_home):
        kb.create_board("alpha")
        kb.create_board("beta")

        with kb.connect(board="alpha") as conn:
            kb.create_task(conn, title="alpha-task-1", assignee="dev")
            kb.create_task(conn, title="alpha-task-2", assignee="dev")

        with kb.connect(board="beta") as conn:
            kb.create_task(conn, title="beta-only", assignee="dev")

        with kb.connect(board="alpha") as conn:
            a = kb.list_tasks(conn)
        with kb.connect(board="beta") as conn:
            b = kb.list_tasks(conn)
        with kb.connect(board="default") as conn:
            d = kb.list_tasks(conn)

        assert {t.title for t in a} == {"alpha-task-1", "alpha-task-2"}
        assert {t.title for t in b} == {"beta-only"}
        assert d == []

    def test_connect_without_args_uses_current(self, fresh_home):
        kb.create_board("curr")
        kb.set_current_board("curr")
        with kb.connect() as conn:
            kb.create_task(conn, title="implicit", assignee="x")
        with kb.connect(board="curr") as conn:
            tasks = kb.list_tasks(conn)
        assert [t.title for t in tasks] == ["implicit"]

    def test_connect_env_var_overrides_current(self, fresh_home, monkeypatch):
        kb.create_board("persist")
        kb.create_board("envwin")
        kb.set_current_board("persist")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "envwin")
        with kb.connect() as conn:
            kb.create_task(conn, title="via-env", assignee="x")
        with kb.connect(board="envwin") as conn:
            assert [t.title for t in kb.list_tasks(conn)] == ["via-env"]
        with kb.connect(board="persist") as conn:
            assert kb.list_tasks(conn) == []

    def test_connect_stale_env_uses_fallback_board_without_recreating_it(
        self, fresh_home, monkeypatch,
    ):
        kb.create_board("ephemeral")
        kb.remove_board("ephemeral")
        kb.create_board("persist")
        kb.set_current_board("persist")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "ephemeral")

        with kb.connect() as conn:
            kb.create_task(conn, title="via-fallback", assignee="x")

        with kb.connect(board="persist") as conn:
            assert [t.title for t in kb.list_tasks(conn)] == ["via-fallback"]
        assert not kb.board_exists("ephemeral")


# ---------------------------------------------------------------------------
# Worker spawn env injection
# ---------------------------------------------------------------------------

class TestWorkerSpawnEnv:
    """Ensure the dispatcher pins ``HERMES_KANBAN_BOARD`` / DB / workspaces on spawn.

    We monkey-patch ``subprocess.Popen`` to capture the child env without
    actually spawning anything.
    """

    def test_default_spawn_sets_env_vars(self, fresh_home, monkeypatch):
        captured = {}

        class FakeProc:
            pid = 12345

        def fake_popen(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        kb.create_board("spawntest")

        task = kb.Task(
            id="t_abc",
            title="worker test",
            body=None,
            assignee="teknium",
            status="ready",
            priority=0,
            created_by="user",
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="scratch",
            workspace_path=None,
            claim_lock=None,
            claim_expires=None,
            tenant=None,
        )

        kb._default_spawn(task, str(fresh_home / "ws"), board="spawntest")

        env = captured["env"]
        assert env["HERMES_KANBAN_BOARD"] == "spawntest"
        assert env["HERMES_KANBAN_TASK"] == "t_abc"
        # DB path should match the per-board DB, not the default board DB.
        expected_db = fresh_home / "kanban" / "boards" / "spawntest" / "kanban.db"
        assert env["HERMES_KANBAN_DB"] == str(expected_db)
        expected_ws = fresh_home / "kanban" / "boards" / "spawntest" / "workspaces"
        assert env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(expected_ws)

    def test_default_board_spawn_keeps_default_paths(self, fresh_home, monkeypatch):
        captured = {}

        class FakeProc:
            pid = 1

        def fake_popen(cmd, *args, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        task = kb.Task(
            id="t_def",
            title="",
            body=None,
            assignee="teknium",
            status="ready",
            priority=0,
            created_by=None,
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="scratch",
            workspace_path=None,
            claim_lock=None,
            claim_expires=None,
            tenant=None,
        )
        kb._default_spawn(task, str(fresh_home / "ws"), board=None)
        env = captured["env"]
        assert env["HERMES_KANBAN_BOARD"] == "default"
        assert env["HERMES_KANBAN_DB"] == str(fresh_home / "kanban.db")


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def _cli(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``hermes kanban …`` with PYTHONPATH pinned to the worktree."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_WORKTREE)
    env["HERMES_TEST_OWNER_APPROVAL_AUTHORITY"] = "1"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban"] + args,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_WORKTREE),
        timeout=30,
    )


@pytest.mark.slow  # every method spawns a real `python -m hermes_cli.main` subprocess
class TestCLI:
    def test_boards_list_default_only(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        res = _cli(["boards", "list", "--json"], env_extra=env)
        assert res.returncode == 0, res.stderr
        data = json.loads(res.stdout)
        slugs = [b["slug"] for b in data]
        assert slugs == ["default"]
        assert data[0]["is_current"] is True

    def test_boards_create_and_switch(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        r1 = _cli(
            ["boards", "create", "myproj", "--name", "My Project", "--switch"],
            env_extra=env,
        )
        assert r1.returncode == 0, r1.stderr
        assert "created" in r1.stdout
        assert "Switched" in r1.stdout

        r2 = _cli(["boards", "list", "--json"], env_extra=env)
        data = json.loads(r2.stdout)
        cur = [b for b in data if b["is_current"]][0]
        assert cur["slug"] == "myproj"
        assert cur["runtime"]["mode"] == "goal"
        assert cur["objective"]["statement"] == "My Project"
        assert [s["key"] for s in cur["workflow"]["stages"]] == [
            "intake", "plan", "execute", "verify", "deliver", "improve",
        ]

    def test_boards_create_kernel_plain_via_cli(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        result = _cli(["boards", "create", "plain", "--runtime", "kernel"], env_extra=env)
        assert result.returncode == 0, result.stderr
        assert "Runtime:" in result.stdout
        assert "Objective:    (none)" in result.stdout
        assert "Workflow:     (none)" in result.stdout

        board_json = tmp_path / "kanban" / "boards" / "plain" / "board.json"
        raw = json.loads(board_json.read_text(encoding="utf-8"))
        assert "objective" not in raw
        assert raw["runtime"]["mode"] == "kernel"
        assert "workflow" not in raw

    def test_boards_create_contract_via_cli(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        contract = {
            "objective": {
                "statement": "CLI contract goal",
                "success": ["proof accepted"],
                "failure": ["missing proof"],
            },
            "runtime": {
                "mode": "company",
                "dispatcher": {"profile": "ceo"},
                "provider_policy": {"mock_research": {"provider": "fixture"}},
                "worker_envelopes": {"worker": {"capabilities": ["mock_research"]}},
            },
            "workflow": {"id": "cli-flow", "stages": ["execute"]},
        }
        path = tmp_path / "contract.json"
        path.write_text(json.dumps(contract), encoding="utf-8")

        result = _cli(
            [
                "boards", "create", "cli-contract",
                "--contract", f"@{path}",
                "--failure", "override failure",
            ],
            env_extra=env,
        )

        assert result.returncode == 0, result.stderr
        assert "Runtime:      company" in result.stdout
        assert "Dispatcher:   ceo" in result.stdout
        assert "Failure:      override failure" in result.stdout
        assert "Providers:    mock_research" in result.stdout
        raw = json.loads(
            (tmp_path / "kanban" / "boards" / "cli-contract" / "board.json").read_text(
                encoding="utf-8"
            )
        )
        assert raw["objective"]["statement"] == "CLI contract goal"
        assert raw["objective"]["failure"] == ["override failure"]
        assert raw["runtime"]["mode"] == "company"
        assert raw["runtime"]["dispatcher"]["profile"] == "ceo"
        assert raw["runtime"]["worker_envelopes"]["worker"]["capabilities"] == ["mock_research"]

    def test_boards_contract_launch_intake_via_cli(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path), "HERMES_PROFILE": "personal-assistant"}

        rough = _cli(
            [
                "boards", "contract", "review", "cli-intake",
                "--rough-goal", "I want to launch an ongoing workflow to recruit referral partners",
                "--create",
                "--json",
            ],
            env_extra=env,
        )
        assert rough.returncode == 0, rough.stderr
        rough_payload = json.loads(rough.stdout)
        assert rough_payload["launch_phase"] == "contract_review"
        assert rough_payload["board"]["contract_readiness"]["ok"] is False
        assert rough_payload["launch_intake"]["state"] == "clarifying"
        assert rough_payload["launch_intake"]["question_generation"]["mode"] in {
            "model_generated",
            "deterministic_fallback",
            "server_generated",
        }
        assert rough_payload["readiness"]["questions"] == []
        assert rough_payload["board"]["contract_readiness"]["questions"] == []

        weak = _cli(
            [
                "boards", "contract", "review", "cli-intake",
                "--intake-answers", json.dumps({"owner_response": "I just want more partners, do whatever."}),
                "--json",
            ],
            env_extra=env,
        )
        assert weak.returncode == 0, weak.stderr
        weak_payload = json.loads(weak.stdout)
        assert weak_payload["ok"] is False
        assert weak_payload["launch_intake"]["state"] == "assessing_answers"
        assert weak_payload["launch_intake"]["clarification_round"] == 1

        duplicate = _cli(
            [
                "boards", "contract", "review", "cli-intake",
                "--intake-answers", json.dumps({"answers": "I just want more partners, do whatever."}),
                "--json",
            ],
            env_extra=env,
        )
        assert duplicate.returncode == 2
        assert "intake_answers already submitted" in duplicate.stderr

        clear_answers = {
            "success_criteria": "Create 20 qualified referral partner opportunities and book 5 qualified conversations per month.",
            "good_fit": "Good partners are CPAs, payroll providers, tax preparers, business attorneys, and local advisors serving small businesses.",
            "allowed_context": "Hermes may use public websites, public directories, owner-provided notes, and approved CRM exports.",
            "approval_boundaries": "Hermes may not send messages, spend money, make promises, use personal accounts, or schedule meetings without owner approval.",
            "workflow_path": "Collect possible partners, check fit, draft the next action, wait for owner approval, execute only approved steps, track outcomes, and close on signed partner, no, disqualified, or owner stop.",
            "proof": "Show the source/context log, fit rationale, draft action, approval status, action log, next action, and weekly summary.",
            "stop_conditions": "Stop for unclear fit, complaints, legal or financial claims, money, reputation risk, negative replies, or any new external side effect.",
        }
        answers_path = tmp_path / "clear-intake.json"
        answers_path.write_text(json.dumps({"answers": clear_answers}), encoding="utf-8")

        clear = _cli(
            [
                "boards", "contract", "review", "cli-intake",
                "--intake-answers", f"@{answers_path}",
                "--json",
            ],
            env_extra=env,
        )
        assert clear.returncode == 0, clear.stderr
        clear_payload = json.loads(clear.stdout)
        assert clear_payload["ok"] is True
        assert clear_payload["status"] == "ready_for_owner_review"
        assert clear_payload["launch_phase"] == "contract_review"
        assert clear_payload["launch_intake"]["state"] == "ready_for_owner_review"
        assert clear_payload["launch_intake"]["answer_quality"]["sufficient"] is True
        assert clear_payload["launch_intake"]["answers"]["success_criteria"].startswith("Create 20")
        assert clear_payload["board"]["contract_readiness"]["ok"] is True

        status = _cli(
            ["boards", "contract", "status", "cli-intake", "--json"],
            env_extra=env,
        )
        assert status.returncode == 0, status.stderr
        status_payload = json.loads(status.stdout)
        assert status_payload["dispatch_enabled"] is False
        assert status_payload["launch_approved"] is False

    def test_owner_profile_direct_contract_requires_intake_or_operator_override(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path), "HERMES_PROFILE": "personal-assistant"}
        contract_path = tmp_path / "launch-contract.json"
        contract_path.write_text(json.dumps(_launch_ready_contract()), encoding="utf-8")

        blocked = _cli(
            [
                "boards", "contract", "review", "cli-direct-blocked",
                "--contract", f"@{contract_path}",
                "--create",
                "--json",
            ],
            env_extra=env,
        )
        assert blocked.returncode == 2
        assert "launch_intake is required before direct contract review" in blocked.stderr

        override = _cli(
            [
                "boards", "contract", "review", "cli-direct-blocked",
                "--contract", f"@{contract_path}",
                "--create",
                "--operator-override",
                "--json",
            ],
            env_extra=env,
        )
        assert override.returncode == 0, override.stderr
        assert json.loads(override.stdout)["ok"] is True

    def test_approval_token_cli_requires_owner_authority(self, tmp_path):
        env = {
            "HERMES_HOME": str(tmp_path),
            "HERMES_PROFILE": "test-orchestrator",
            "HERMES_TEST_OWNER_APPROVAL_AUTHORITY": "0",
        }
        contract_path = tmp_path / "launch-contract.json"
        contract_path.write_text(json.dumps(_launch_ready_contract()), encoding="utf-8")
        reviewed = _cli(
            [
                "boards", "contract", "review", "cli-token-authority",
                "--contract", f"@{contract_path}",
                "--create",
                "--json",
            ],
            env_extra=env,
        )
        assert reviewed.returncode == 0, reviewed.stderr

        token = _cli(
            [
                "boards", "contract", "approval-token", "cli-token-authority",
                "--contract", f"@{contract_path}",
                "--approved-by", "owner",
                "--approval-evidence", "owner approved cli launch",
                "--json",
            ],
            env_extra=env,
        )
        assert token.returncode == 2
        assert "interactive owner authority boundary" in token.stderr

    def test_boards_contract_review_and_amend_via_cli(self, tmp_path):
        env = {
            "HERMES_HOME": str(tmp_path),
            "HERMES_PROFILE": "test-orchestrator",
            "HERMES_TEST_OWNER_APPROVAL_AUTHORITY": "1",
        }
        contract_path = tmp_path / "launch-contract.json"
        contract_path.write_text(json.dumps(_launch_ready_contract()), encoding="utf-8")

        reviewed = _cli(
            [
                "boards", "contract", "review", "cli-launch",
                "--contract", f"@{contract_path}",
                "--create",
                "--json",
            ],
            env_extra=env,
        )
        assert reviewed.returncode == 0, reviewed.stderr
        review_payload = json.loads(reviewed.stdout)
        assert review_payload["ok"] is True
        assert review_payload["launch_phase"] == "contract_review"

        approval = _cli(
            [
                "boards", "contract", "approval-token", "cli-launch",
                "--contract", f"@{contract_path}",
                "--approved-by", "owner",
                "--approval-evidence", "owner approved cli launch",
                "--json",
            ],
            env_extra=env,
        )
        assert approval.returncode == 0, approval.stderr
        approval_payload = json.loads(approval.stdout)
        assert approval_payload["kind"] == "launch_review"

        activated = _cli(
            [
                "boards", "contract", "review", "cli-launch",
                "--contract", f"@{contract_path}",
                "--approve",
                "--approval-token", approval_payload["token"],
                "--json",
            ],
            env_extra=env,
        )
        assert activated.returncode == 0, activated.stderr
        activated_payload = json.loads(activated.stdout)
        assert activated_payload["ok"] is True
        assert activated_payload["launch_phase"] == "active"
        assert activated_payload["approval"]["approval_token_id"] == approval_payload["token_id"]

        status = _cli(
            ["boards", "contract", "status", "cli-launch", "--json"],
            env_extra=env,
        )
        assert status.returncode == 0, status.stderr
        status_payload = json.loads(status.stdout)
        assert status_payload["dispatch_enabled"] is True

        patch_path = tmp_path / "amendment.json"
        patch_path.write_text(
            json.dumps({"objective": {"success": ["five seller calls per week"]}}),
            encoding="utf-8",
        )
        proposed = _cli(
            [
                "boards", "contract", "propose", "cli-launch", f"@{patch_path}",
                "--reason", "change target",
                "--risk", "low",
                "--json",
            ],
            env_extra=env,
        )
        assert proposed.returncode == 0, proposed.stderr
        amendment = json.loads(proposed.stdout)

        amendment_approval = _cli(
            [
                "boards", "contract", "approval-token", "cli-launch",
                "--amendment-id", amendment["id"],
                "--approved-by", "owner",
                "--approval-evidence", "owner approved amendment",
                "--json",
            ],
            env_extra=env,
        )
        assert amendment_approval.returncode == 0, amendment_approval.stderr
        amendment_approval_payload = json.loads(amendment_approval.stdout)
        assert amendment_approval_payload["kind"] == "contract_amendment"

        applied = _cli(
            [
                "boards", "contract", "apply", "cli-launch", amendment["id"],
                "--approval-token", amendment_approval_payload["token"],
                "--json",
            ],
            env_extra=env,
        )
        assert applied.returncode == 0, applied.stderr
        applied_payload = json.loads(applied.stdout)
        assert applied_payload["contract_version"] == 2
        assert applied_payload["dispatch_enabled"] is True
        assert applied_payload["launch_blocked"] is False
        assert applied_payload["launch_phase"] == "active"

    def test_boards_contract_review_approve_requires_token_via_cli(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path), "HERMES_PROFILE": "test-orchestrator"}
        contract_path = tmp_path / "launch-contract.json"
        contract_path.write_text(json.dumps(_launch_ready_contract()), encoding="utf-8")
        reviewed = _cli(
            [
                "boards", "contract", "review", "cli-review-no-token",
                "--contract", f"@{contract_path}",
                "--create",
            ],
            env_extra=env,
        )
        assert reviewed.returncode == 0, reviewed.stderr

        activated = _cli(
            [
                "boards", "contract", "review", "cli-review-no-token",
                "--contract", f"@{contract_path}",
                "--approve",
            ],
            env_extra=env,
        )

        assert activated.returncode == 2
        assert "approval_token is required" in activated.stderr

    def test_boards_contract_apply_requires_token_via_cli(self, tmp_path):
        env = {
            "HERMES_HOME": str(tmp_path),
            "HERMES_PROFILE": "test-orchestrator",
            "HERMES_TEST_OWNER_APPROVAL_AUTHORITY": "1",
        }
        contract_path = tmp_path / "launch-contract.json"
        contract_path.write_text(json.dumps(_launch_ready_contract()), encoding="utf-8")
        reviewed = _cli(
            [
                "boards", "contract", "review", "cli-apply-no-token",
                "--contract", f"@{contract_path}",
                "--create",
                "--json",
            ],
            env_extra=env,
        )
        assert reviewed.returncode == 0, reviewed.stderr
        approval = _cli(
            [
                "boards", "contract", "approval-token", "cli-apply-no-token",
                "--contract", f"@{contract_path}",
                "--approved-by", "owner",
                "--approval-evidence", "owner approved cli launch",
                "--json",
            ],
            env_extra=env,
        )
        assert approval.returncode == 0, approval.stderr
        activated = _cli(
            [
                "boards", "contract", "review", "cli-apply-no-token",
                "--contract", f"@{contract_path}",
                "--approve",
                "--approval-token", json.loads(approval.stdout)["token"],
            ],
            env_extra=env,
        )
        assert activated.returncode == 0, activated.stderr

        patch_path = tmp_path / "amendment.json"
        patch_path.write_text(
            json.dumps({"objective": {"success": ["five seller calls per week"]}}),
            encoding="utf-8",
        )
        proposed = _cli(
            [
                "boards", "contract", "propose", "cli-apply-no-token", f"@{patch_path}",
                "--reason", "change target",
                "--risk", "low",
                "--json",
            ],
            env_extra=env,
        )
        assert proposed.returncode == 0, proposed.stderr
        amendment = json.loads(proposed.stdout)

        applied = _cli(
            [
                "boards", "contract", "apply", "cli-apply-no-token", amendment["id"],
            ],
            env_extra=env,
        )

        assert applied.returncode == 2
        assert "approval_token is required" in applied.stderr

    def test_boards_contract_apply_force_is_rejected(self, tmp_path):
        env = {
            "HERMES_HOME": str(tmp_path),
            "HERMES_PROFILE": "test-orchestrator",
            "HERMES_TEST_OWNER_APPROVAL_AUTHORITY": "1",
        }
        contract_path = tmp_path / "launch-contract.json"
        contract_path.write_text(json.dumps(_launch_ready_contract()), encoding="utf-8")

        reviewed = _cli(
            [
                "boards", "contract", "review", "cli-force-launch",
                "--contract", f"@{contract_path}",
                "--create",
                "--json",
            ],
            env_extra=env,
        )
        assert reviewed.returncode == 0, reviewed.stderr
        approval = _cli(
            [
                "boards", "contract", "approval-token", "cli-force-launch",
                "--contract", f"@{contract_path}",
                "--approved-by", "owner",
                "--approval-evidence", "owner approved force launch",
                "--json",
            ],
            env_extra=env,
        )
        assert approval.returncode == 0, approval.stderr
        approval_payload = json.loads(approval.stdout)
        activated = _cli(
            [
                "boards", "contract", "review", "cli-force-launch",
                "--contract", f"@{contract_path}",
                "--approve",
                "--approval-token", approval_payload["token"],
                "--json",
            ],
            env_extra=env,
        )
        assert activated.returncode == 0, activated.stderr

        patch_path = tmp_path / "not-ready-amendment.json"
        patch_path.write_text(
            json.dumps({
                "workflow": {
                    "id": "not-ready",
                    "require_semantics": False,
                    "workstreams": [],
                    "stages": [
                        {"key": "only", "actions": [], "exit_criteria": []}
                    ],
                }
            }),
            encoding="utf-8",
        )
        proposed = _cli(
            [
                "boards", "contract", "propose", "cli-force-launch", f"@{patch_path}",
                "--reason", "simulate incomplete launch amendment",
            ],
            env_extra=env,
        )
        assert proposed.returncode == 0, proposed.stderr
        amendment_id = proposed.stdout.split()[1]

        applied = _cli(
            [
                "boards", "contract", "apply", "cli-force-launch", amendment_id,
                "--approved-by", "owner",
                "--force",
            ],
            env_extra=env,
        )
        assert applied.returncode == 2
        assert "--force has been retired" in applied.stderr

    def test_per_board_task_isolation_via_cli(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        assert _cli(["boards", "create", "projA"], env_extra=env).returncode == 0
        assert _cli(["boards", "create", "projB"], env_extra=env).returncode == 0

        # Create one task on each via --board.
        r = _cli(["--board", "projA", "create", "Task A", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr
        r = _cli(["--board", "projB", "create", "Task B", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr

        # list on each board only shows its own.
        listA = _cli(["--board", "projA", "list", "--json"], env_extra=env)
        listB = _cli(["--board", "projB", "list", "--json"], env_extra=env)
        listD = _cli(["list", "--json"], env_extra=env)

        titlesA = [t["title"] for t in json.loads(listA.stdout)]
        titlesB = [t["title"] for t in json.loads(listB.stdout)]
        titlesD = [t["title"] for t in json.loads(listD.stdout)]

        assert titlesA == ["Task A"]
        assert titlesB == ["Task B"]
        assert titlesD == []

    def test_board_flag_rejects_unknown(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        r = _cli(["--board", "ghost", "list"], env_extra=env)
        # main.py's dispatcher doesn't propagate return codes today, so we
        # assert the user-visible signal: a stderr error message. Whether
        # the exit code stays 0 is a separate (pre-existing) issue.
        assert "does not exist" in r.stderr

    def test_board_flag_rejects_empty_board_dir(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        ghost = tmp_path / "kanban" / "boards" / "ghost"
        ghost.mkdir(parents=True)
        r = _cli(["--board", "ghost", "list"], env_extra=env)
        assert "does not exist" in r.stderr

    def test_boards_rm_archives(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        _cli(["boards", "create", "rmme"], env_extra=env)
        r = _cli(["boards", "rm", "rmme"], env_extra=env)
        assert r.returncode == 0, r.stderr
        assert "archived" in r.stdout
        # Default board list no longer shows it.
        res = _cli(["boards", "list", "--json"], env_extra=env)
        slugs = [b["slug"] for b in json.loads(res.stdout)]
        assert "rmme" not in slugs
