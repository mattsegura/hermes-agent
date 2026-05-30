"""HARD RAIL tests for the board↔profile binding fix.

Root cause being locked down: board identity used to resolve via a silent
fallback chain (``HERMES_KANBAN_BOARD`` env → ``current`` symlink →
``'default'``) and ``connect()``/``init_db()`` would SILENTLY MATERIALIZE a
``kanban.db`` for whatever slug it landed on — even one with no ``board.json``.
Gateways for profiles ``land-ceo`` / ``land-pipeline-optimizer`` each spawned
their own orphan board instead of all operating the single real board.

These tests pin every part of the cure (declare / validate / fail-loud):

1. ``connect()`` refuses to materialize a brand-new named board (no
   board.json AND no kanban.db) unless creation is explicit — and does NOT
   leave an orphan kanban.db behind.
2. Profiles declare their board (``kanban_board:`` in config.yaml); the
   strict daemon resolver fails loud rather than falling through to default.
3. ``kanban doctor`` flags orphan boards and bad profile bindings.
4. A board's contract roles bind every matching profile to that ONE board.
5. The orphan cleanup utility (scan / quarantine / adopt).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME in STRICT mode (implicit-create disabled).

    The session conftest enables ``HERMES_KANBAN_ALLOW_IMPLICIT_BOARD`` for
    the ~280 legacy call sites; these tests assert the *production* fail-loud
    default, so we delenv it here.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_ALLOW_IMPLICIT_BOARD",
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
# 1. No silent auto-create (HARD RAIL)
# ---------------------------------------------------------------------------

def test_open_missing_board_raises_typed_error_and_creates_no_db(fresh_home):
    """Opening a board that was never created fails loud — no orphan db."""
    with pytest.raises(kb.BoardNotFoundError) as exc_info:
        kb.connect(board="land-ceo")
    assert exc_info.value.slug == "land-ceo"
    # Typed but still a ValueError subclass for back-compat with callers.
    assert isinstance(exc_info.value, ValueError)
    # Crucially: NO kanban.db was fabricated for the ghost board.
    assert not (kb.board_dir("land-ceo") / "kanban.db").exists()


def test_default_board_is_exempt_from_the_rail(fresh_home):
    """The default board always opens (back-compat) without explicit create."""
    with kb.connect(board="default") as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_explicit_create_board_then_open_succeeds(fresh_home):
    """create_board writes board.json; the board then opens normally."""
    kb.create_board("land-wholesaling", name="Land Wholesaling")
    assert (kb.board_dir("land-wholesaling") / "board.json").exists()
    with kb.connect(board="land-wholesaling") as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_connect_create_flag_bypasses_the_rail(fresh_home):
    """connect(create=True) is the explicit opt-in used by init_db."""
    with kb.connect(board="explicit-create", create=True) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert (kb.board_dir("explicit-create") / "kanban.db").exists()


def test_init_db_creates_a_board(fresh_home):
    """init_db is an explicit creation entry point and always allows create."""
    kb.init_db(board="via-init")
    assert (kb.board_dir("via-init") / "kanban.db").exists()


def test_env_var_reenables_implicit_create(fresh_home, monkeypatch):
    """The test-only env flag restores permissive auto-create (legacy sites)."""
    monkeypatch.setenv("HERMES_KANBAN_ALLOW_IMPLICIT_BOARD", "1")
    with kb.connect(board="implicit-ok") as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_reopening_an_existing_orphan_still_works(fresh_home):
    """The rail blocks *genesis*, not reopening — a pre-existing db opens."""
    d = kb.board_dir("legacy-orphan")
    d.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(d / "kanban.db").close()
    # board_exists() is True (kanban.db present), so connect must NOT raise.
    with kb.connect(board="legacy-orphan", create=True) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 2. board_is_configured semantics
# ---------------------------------------------------------------------------

def test_board_is_configured_requires_board_json(fresh_home):
    assert kb.board_is_configured("default") is True  # always
    d = kb.board_dir("orphan2")
    d.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(d / "kanban.db").close()
    # Orphan: db present, no board.json -> exists() True but configured False.
    assert kb.board_exists("orphan2") is True
    assert kb.board_is_configured("orphan2") is False
    kb.create_board("real-board")
    assert kb.board_is_configured("real-board") is True


# ---------------------------------------------------------------------------
# 3. Profile → board binding + strict daemon resolver (fail-loud)
# ---------------------------------------------------------------------------

def test_set_and_read_profile_board_binding(fresh_home):
    assert kb.profile_board_binding("solo") is None
    kb.set_profile_board_binding("solo", "land-wholesaling")
    assert kb.profile_board_binding("solo") == "land-wholesaling"
    # Re-binding replaces the value, doesn't duplicate the key.
    kb.set_profile_board_binding("solo", "other-board")
    assert kb.profile_board_binding("solo") == "other-board"
    cfg = (kb._profile_config_path("solo")).read_text()
    assert cfg.count("kanban_board:") == 1


def test_set_profile_board_binding_preserves_existing_config(fresh_home):
    """Surgical edit must not clobber existing config.yaml content."""
    path = kb._profile_config_path("withcfg")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("model: test/mock\n# a comment\ntoolsets:\n  - terminal\n")
    kb.set_profile_board_binding("withcfg", "b1")
    text = path.read_text()
    assert "model: test/mock" in text
    assert "# a comment" in text
    assert "kanban_board: b1" in text


def test_resolve_daemon_board_env_first(fresh_home, monkeypatch):
    kb.create_board("envboard")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "envboard")
    assert kb.resolve_daemon_board("ignored-profile") == "envboard"


def test_resolve_daemon_board_uses_profile_binding(fresh_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.create_board("bound-board")
    kb.set_profile_board_binding("p1", "bound-board")
    assert kb.resolve_daemon_board("p1") == "bound-board"


def test_resolve_daemon_board_fails_loud_without_binding(fresh_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    with pytest.raises(kb.GatewayBoardBindingError):
        kb.resolve_daemon_board("unbound-profile")
    # Also fails with no profile at all — never silently 'default'.
    with pytest.raises(kb.GatewayBoardBindingError):
        kb.resolve_daemon_board(None)


def test_resolve_daemon_board_rejects_unconfigured_env_board(fresh_home, monkeypatch):
    """An env-pinned board that has no board.json must not be fabricated."""
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "ghosty")
    with pytest.raises(kb.GatewayBoardBindingError):
        kb.resolve_daemon_board("any")


def test_resolve_daemon_board_rejects_binding_to_unconfigured_board(fresh_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.set_profile_board_binding("p2", "never-made")
    with pytest.raises(kb.GatewayBoardBindingError):
        kb.resolve_daemon_board("p2")


# ---------------------------------------------------------------------------
# 4. Contract roles → bind many profiles to ONE board
# ---------------------------------------------------------------------------

def _land_contract():
    return {
        "objective": {"statement": "land wholesaling"},
        "runtime": {
            "mode": "goal",
            "dispatcher": {"profile": "land-pipeline-optimizer"},
            "profiles": {
                "ceo": "land-ceo",
                "optimizer": "land-pipeline-optimizer",
                "worker": "land-negotiator",
            },
        },
    }


def test_board_role_profiles_reads_contract(fresh_home):
    kb.create_board("land-wholesaling", contract=_land_contract())
    roles = kb.board_role_profiles("land-wholesaling")
    assert roles["dispatcher"] == "land-pipeline-optimizer"
    assert roles["ceo"] == "land-ceo"
    assert roles["worker"] == "land-negotiator"


def test_bind_contract_roles_binds_many_profiles_to_one_board(fresh_home):
    kb.create_board("land-wholesaling", contract=_land_contract())
    res = kb.bind_contract_roles_to_board("land-wholesaling")
    assert set(res["bound"]) == {"land-ceo", "land-pipeline-optimizer", "land-negotiator"}
    # Every named profile now resolves to the single shared board.
    for prof in ("land-ceo", "land-pipeline-optimizer", "land-negotiator"):
        assert kb.profile_board_binding(prof) == "land-wholesaling"
        assert kb.resolve_daemon_board(prof) == "land-wholesaling"
    assert res["conflicts"] == []


def test_out_of_root_db_override_refuses_to_fabricate(fresh_home, tmp_path, monkeypatch):
    """A daemon-inherited out-of-root HERMES_KANBAN_DB at a non-existent path
    must fail loud, not silently fabricate a ghost DB out-of-tree."""
    ghost = tmp_path / "elsewhere" / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(ghost))
    assert not ghost.exists()
    with pytest.raises(kb.UnconfiguredKanbanDbError):
        kb.connect()
    # No ghost DB left behind.
    assert not ghost.exists()


def test_out_of_root_db_override_works_when_db_exists(fresh_home, tmp_path, monkeypatch):
    """A legitimate power-user override that points at an EXISTING out-of-root
    DB is honoured (not gated)."""
    real = tmp_path / "elsewhere" / "kanban.db"
    real.parent.mkdir(parents=True)
    # Explicit creation is allowed (the documented opt-in act).
    kb.init_db(real)
    assert real.exists()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(real))
    import contextlib
    with contextlib.closing(kb.connect()) as conn:
        # Usable connection on the existing override DB.
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()


def test_out_of_root_db_override_create_true_bypasses_gate(fresh_home, tmp_path, monkeypatch):
    """Explicit create=True (create_board / init_db semantics) may create even
    an out-of-root override DB."""
    fresh = tmp_path / "elsewhere" / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(fresh))
    import contextlib
    with contextlib.closing(kb.connect(create=True)) as conn:
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
    assert fresh.exists()


def test_in_root_worker_handoff_db_override_unaffected(fresh_home, monkeypatch):
    """The dispatcher→worker handoff injects an IN-root HERMES_KANBAN_DB for a
    configured board; that must keep working (the out-of-root gate must not
    touch it)."""
    kb.create_board("realbiz", contract={"objective": {"statement": "x"},
                                         "runtime": {"mode": "goal",
                                                     "dispatcher": {"profile": "p"}}})
    db = kb.kanban_db_path(board="realbiz")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    import contextlib
    with contextlib.closing(kb.connect()) as conn:
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()


def test_corrupt_board_json_flagged_not_orphaned(fresh_home):
    """A truncated/partial-write board.json must be flagged CORRUPT (fail-loud),
    not silently treated as healthy and not misclassified as an orphan."""
    kb.create_board(
        "realbiz",
        contract={"objective": {"statement": "x"},
                  "runtime": {"mode": "goal", "dispatcher": {"profile": "p-ceo"}}},
    )
    meta_path = kb.board_metadata_path("realbiz")
    assert meta_path.exists()
    meta_path.write_text('{ "slug": "realbiz", "runtime": {  TRUNCATED')

    # Still "configured" (file present) so it is NOT an orphan ...
    assert kb.board_is_configured("realbiz") is True
    assert [o["slug"] for o in kb.scan_orphan_boards()] == []
    # ... but it IS detected as corrupt.
    corrupt = kb.scan_corrupt_boards()
    assert [c["slug"] for c in corrupt] == ["realbiz"]
    assert corrupt[0]["error"]

    health = kb.board_binding_health()
    assert health["ok"] is False
    assert [c["slug"] for c in health["corrupt"]] == ["realbiz"]


def test_board_role_profiles_harvests_agents_list(fresh_home):
    """Extra named agents (negotiator/operator) declared under runtime.agents
    must be harvested so every contract-named profile binds to the one board."""
    c = {
        "objective": {"statement": "wholesale land"},
        "runtime": {
            "mode": "goal",
            "dispatcher": {"profile": "land-ceo"},
            "profiles": {"optimizer": "land-optimizer", "worker": "land-worker"},
            "agents": [
                {"role": "negotiator", "profile": "land-negotiator"},
                {"role": "operator", "profile": "land-operator"},
            ],
        },
    }
    kb.create_board("lw", contract=c)
    roles = kb.board_role_profiles("lw")
    assert roles.get("negotiator") == "land-negotiator"
    assert roles.get("operator") == "land-operator"
    res = kb.bind_contract_roles_to_board("lw")
    # Every named non-default profile resolves to the one board.
    for prof in ("land-ceo", "land-optimizer", "land-worker",
                 "land-negotiator", "land-operator"):
        assert res["bound"].get(prof) == "lw", (prof, res["bound"])
        assert kb.resolve_daemon_board(profile=prof) == "lw"


def test_bind_contract_roles_never_pins_default_profile(fresh_home):
    """A contract naming 'default' must not pollute the global root config."""
    c = {
        "objective": {"statement": "x"},
        "runtime": {"mode": "goal",
                    "dispatcher": {"profile": "default"},
                    "profiles": {"worker": "default", "ceo": "real-ceo"}},
    }
    kb.create_board("b1", contract=c)
    res = kb.bind_contract_roles_to_board("b1")
    # default is skipped; the real profile is bound.
    assert "default" in res["skipped"]
    assert "default" not in res["bound"]
    assert res["bound"].get("real-ceo") == "b1"
    # Root config.yaml was NOT pinned.
    root_cfg = kb._profile_config_path("default")
    assert (not root_cfg.exists()) or ("kanban_board" not in root_cfg.read_text())


def test_bind_contract_roles_reports_conflicts(fresh_home):
    kb.create_board("land-wholesaling", contract=_land_contract())
    # Pre-bind one role profile to a different board.
    kb.set_profile_board_binding("land-ceo", "some-other-board")
    res = kb.bind_contract_roles_to_board("land-wholesaling")
    conflicts = {c["profile"]: c for c in res["conflicts"]}
    assert "land-ceo" in conflicts
    assert conflicts["land-ceo"]["was"] == "some-other-board"
    assert conflicts["land-ceo"]["now"] == "land-wholesaling"
    # Last launch wins — it's re-bound to the real board.
    assert kb.profile_board_binding("land-ceo") == "land-wholesaling"


# ---------------------------------------------------------------------------
# 5. Orphan cleanup utility
# ---------------------------------------------------------------------------

def _make_orphan(slug: str) -> Path:
    d = kb.board_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / "kanban.db")
    conn.execute("CREATE TABLE tasks (id TEXT)")
    conn.execute("INSERT INTO tasks (id) VALUES ('t1')")
    conn.commit()
    conn.close()
    return d


def test_scan_orphan_boards_finds_db_without_metadata(fresh_home):
    _make_orphan("land-deals")
    kb.create_board("configured")  # has board.json -> not an orphan
    orphans = {o["slug"]: o for o in kb.scan_orphan_boards()}
    assert "land-deals" in orphans
    assert "configured" not in orphans
    assert "default" not in orphans
    assert orphans["land-deals"]["task_count"] == 1


def test_quarantine_orphan_board_moves_it_aside(fresh_home):
    _make_orphan("land-deals")
    res = kb.quarantine_orphan_board("land-deals")
    assert res["action"] == "quarantined"
    assert Path(res["new_path"]).exists()
    assert not (kb.board_dir("land-deals") / "kanban.db").exists()
    assert kb.scan_orphan_boards() == []


def test_quarantine_refuses_configured_board(fresh_home):
    kb.create_board("configured")
    with pytest.raises(ValueError):
        kb.quarantine_orphan_board("configured")


def test_adopt_orphan_board_writes_metadata(fresh_home):
    _make_orphan("adoptme")
    kb.adopt_orphan_board("adoptme")
    assert kb.board_is_configured("adoptme") is True
    assert kb.scan_orphan_boards() == []


# ---------------------------------------------------------------------------
# 6. kanban doctor flags orphans + bad bindings
# ---------------------------------------------------------------------------

def test_doctor_health_flags_orphans_and_bad_bindings(fresh_home):
    _make_orphan("ghost-board")
    kb.set_profile_board_binding("p-bad", "nonexistent-board")
    health = kb.board_binding_health()
    assert health["ok"] is False
    assert any(o["slug"] == "ghost-board" for o in health["orphans"])
    assert any(b["profile"] == "p-bad" for b in health["bad_profile_bindings"])


def test_doctor_cli_exit_code_flags_orphan(fresh_home, monkeypatch):
    from hermes_cli import kanban as kbc
    _make_orphan("ghost-board")
    args = SimpleNamespace(
        json=False, all_boards=False, staleness_seconds=kb.TICK_STALENESS_SECONDS,
    )
    # Orphan present -> doctor must exit non-zero.
    assert kbc._cmd_doctor(args) == 1
    # After cleanup, healthy again.
    kb.quarantine_orphan_board("ghost-board")
    assert kbc._cmd_doctor(args) == 0


def test_doctor_health_clean_when_all_configured(fresh_home):
    kb.create_board("clean1")
    kb.create_board("clean2")
    health = kb.board_binding_health()
    assert health["ok"] is True
    assert health["orphans"] == []
    assert health["bad_profile_bindings"] == []


# ---------------------------------------------------------------------------
# 7. Launch auto-binds the contract's role profiles to the ONE board
# ---------------------------------------------------------------------------

def _launch_ready_land_contract():
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


def _approve_launch(slug, contract):
    kb.review_business_launch_contract(slug, contract=contract, create_if_missing=True)
    token = kb.issue_board_launch_approval_token(
        slug, contract=contract, approved_by="owner",
        approval_evidence={"source": "test-owner-approval"},
        owner_authority_confirmed=True,
    )["token"]
    return kb.review_business_launch_contract(
        slug, contract=contract, approve=True, author="owner", approval_token=token,
    )


def test_launch_binds_contract_role_profiles_to_the_board(fresh_home):
    """Activating a board binds ceo/optimizer/worker/dispatcher to THAT board."""
    res = _approve_launch("land-wholesaling", _launch_ready_land_contract())
    assert res["launch_phase"] == "active"
    # Every profile the contract names now resolves to the single shared board.
    for prof in ("land-ceo", "land-opt", "land-operator"):
        assert kb.profile_board_binding(prof) == "land-wholesaling"
        assert kb.resolve_daemon_board(prof) == "land-wholesaling"
