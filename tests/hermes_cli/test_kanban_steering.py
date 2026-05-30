"""Tests for P6 -- the conversational CEO steering channel.

P6 is a THIN conversational layer on top of the P5 amendment loop: an
owner-facing, durable conversation with the system's top-level reasoning (the
"CEO agent"). Its only power to make a STRUCTURAL change is to draft a P5
amendment (origin ``ceo``) -- which still requires the owner's explicit
approve -> validate -> mint. The conversation never mutates the live contract.

These tests mock the auxiliary CEO model exactly like the launch-intake tests:
by monkeypatching ``kanban_launch_intake._call_model`` to return a canned JSON
string, so the real ``run_ceo_turn`` parsing/proposal-extraction runs offline.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_launch_intake as kli


_WORKTREE = Path(__file__).resolve().parents[2]
_FIXTURES = _WORKTREE / "tests" / "fixtures" / "launch_intake"


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
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
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _base_contract() -> dict:
    with open(_FIXTURES / "insurance_recruiting.contract.json") as fh:
        return json.load(fh)


def _activate_board(board: str, contract: dict) -> None:
    kb.review_business_launch_contract(board, contract=contract, create_if_missing=True)
    token = kb.issue_board_launch_approval_token(
        board,
        contract=contract,
        approved_by="owner",
        approval_evidence={"source": "steering-test-setup"},
        owner_authority_confirmed=True,
    )["token"]
    kb.review_business_launch_contract(
        board, contract=contract, approve=True, approval_token=token
    )
    assert kb.validate_board_launch_readiness(board)["ok"] is True


def _amendment_token(board: str, amendment_id: str) -> str:
    return kb.issue_board_launch_approval_token(
        board,
        amendment_id=amendment_id,
        approved_by="owner",
        approval_evidence={"source": f"approve:{amendment_id}"},
        owner_authority_confirmed=True,
    )["token"]


def _widen_nudges(contract: dict, *, default: int = 10, hi: int = 12) -> dict:
    proposed = copy.deepcopy(contract)
    proposed["tunables"]["max_nudges"]["range"] = [0, hi]
    proposed["tunables"]["max_nudges"]["default"] = default
    return proposed


def _stub_ceo(monkeypatch, response: dict, *, capture: list | None = None) -> None:
    """Stub the aux model so ``run_ceo_turn`` parses ``response`` (a CEO JSON)."""

    def _fake_call_model(system_prompt, user_payload, **kwargs):
        if capture is not None:
            capture.append(user_payload)
        return json.dumps(response), False

    monkeypatch.setattr(kli, "_call_model", _fake_call_model)
    monkeypatch.setattr(kli, "aux_configured", lambda: True)


# ---------------------------------------------------------------------------
# Durable conversation store
# ---------------------------------------------------------------------------


def test_session_and_messages_are_durable_across_connections(fresh_home):
    _activate_board("durable", _base_contract())
    with kb.connect(board="durable") as conn:
        session = kb.open_steering_session(
            conn, board="durable", mode="runtime_evolution", title="evolve it",
        )
        sid = session["session_id"]
        kb.append_steering_message(conn, sid, role="owner", content="first owner msg", board="durable")
        kb.append_steering_message(conn, sid, role="ceo", content="ceo reply", board="durable")
        kb.append_steering_message(
            conn, sid, role="system", content="a note",
            attachments={"amendment_id": "cam_x"}, board="durable",
        )

    # Fresh connection -> the full ordered history is intact.
    with kb.connect(board="durable") as conn:
        reloaded = kb.get_steering_session(conn, sid, board="durable")
        assert reloaded["mode"] == "runtime_evolution"
        assert reloaded["status"] == "open"
        msgs = kb.get_steering_messages(conn, sid, board="durable")
        assert [m["role"] for m in msgs] == ["owner", "ceo", "system"]
        assert [m["seq"] for m in msgs] == [1, 2, 3]
        assert msgs[0]["content"] == "first owner msg"
        assert msgs[2]["attachments"] == {"amendment_id": "cam_x"}


def test_close_session_preserves_log(fresh_home):
    _activate_board("closeb", _base_contract())
    with kb.connect(board="closeb") as conn:
        sid = kb.open_steering_session(conn, board="closeb", mode="runtime_evolution")["session_id"]
        kb.append_steering_message(conn, sid, role="owner", content="hi", board="closeb")
        closed = kb.close_steering_session(conn, sid, board="closeb")
        assert closed["status"] == "closed"
        assert closed["closed_at"] is not None
        # Log is preserved after close.
        assert len(kb.get_steering_messages(conn, sid, board="closeb")) == 1
        # A closed session refuses new turns.
        with pytest.raises(ValueError, match="not open"):
            kb.steer_send_message(
                conn, board="closeb", session_id=sid, owner_message="more?",
            )


# ---------------------------------------------------------------------------
# CEO turn -> P5 amendment
# ---------------------------------------------------------------------------


def test_ceo_turn_drafts_ceo_amendment_without_touching_live_contract(fresh_home, monkeypatch):
    contract = _base_contract()
    _activate_board("ceob", contract)
    proposed = _widen_nudges(contract)
    _stub_ceo(monkeypatch, {
        "reply": "Agreed -- widening max_nudges so slower funnels keep nudging.",
        "amendment": {
            "rationale": "widen max_nudges for slower funnels",
            "proposed_contract": proposed,
        },
        "research_query": "",
    })

    with kb.connect(board="ceob") as conn:
        sid = kb.open_steering_session(conn, board="ceob", mode="runtime_evolution")["session_id"]
        result = kb.steer_send_message(
            conn, board="ceob", session_id=sid,
            owner_message="Our nudge cap feels too low for slow leads.",
        )
        amendment = result["amendment"]
        assert amendment is not None
        assert amendment["origin"] == "ceo"
        assert amendment["status"] == kb.AMENDMENT_STATUS_DRAFTED

        # The CEO reply message references the drafted amendment.
        assert result["ceo_message"]["role"] == "ceo"
        assert result["ceo_message"]["attachments"]["amendment_id"] == amendment["amendment_id"]

        # The conversation surfaces the amendment + approval path as a system msg.
        msgs = kb.get_steering_messages(conn, sid, board="ceob")
        assert [m["role"] for m in msgs] == ["owner", "ceo", "system"]
        assert amendment["amendment_id"] in (msgs[-1]["attachments"] or {}).get("amendment_id", "")

    # Live contract is untouched: still version 1, original range.
    meta = kb.read_board_metadata("ceob")
    assert meta["contract_version"] == 1
    assert meta["business_contract"]["tunables"]["max_nudges"]["range"] != [0, 12]


def test_end_to_end_chat_proposal_through_p5_mint(fresh_home, monkeypatch):
    contract = _base_contract()
    _activate_board("e2e", contract)
    proposed = _widen_nudges(contract)
    _stub_ceo(monkeypatch, {
        "reply": "Done -- here's the change for your approval.",
        "amendment": {
            "rationale": "widen max_nudges via chat",
            "proposed_contract": proposed,
        },
    })

    with kb.connect(board="e2e") as conn:
        sid = kb.open_steering_session(conn, board="e2e", mode="runtime_evolution")["session_id"]
        result = kb.steer_send_message(
            conn, board="e2e", session_id=sid, owner_message="Bump the nudge cap.",
        )
        aid = result["amendment"]["amendment_id"]

    # Owner drives it through the EXACT P5 rail (approve -> validate -> mint).
    token = _amendment_token("e2e", aid)
    with kb.connect(board="e2e") as conn:
        kb.approve_contract_amendment(conn, aid, board="e2e", approver="owner", token=token)
        validated = kb.validate_contract_amendment(conn, aid, board="e2e")
        assert validated["status"] == kb.AMENDMENT_STATUS_VALIDATED
        minted = kb.mint_contract_amendment(conn, aid, board="e2e")
        assert minted["status"] == kb.AMENDMENT_STATUS_ACTIVE
        assert minted["minted_version"] == 2

        # The conversation reflects the activated amendment.
        msg = kb.steer_reflect_amendment_state(
            conn, session_id=sid, amendment_id=aid, board="e2e",
        )
        assert msg["role"] == "system"
        assert "minted" in msg["content"].lower()
        assert msg["attachments"]["amendment_status"] == kb.AMENDMENT_STATUS_ACTIVE
        assert msg["attachments"]["contract_version"] == 2

    assert kb.read_board_metadata("e2e")["contract_version"] == 2


def test_validation_failure_surfaced_back_into_conversation(fresh_home, monkeypatch):
    contract = _base_contract()
    _activate_board("vfail", contract)
    # Drop the declared range from a tunable: passes readiness but breaks the
    # structural invariant bar (an unbounded knob is unsafe).
    bad = copy.deepcopy(contract)
    bad["tunables"]["max_nudges"].pop("range", None)
    _stub_ceo(monkeypatch, {
        "reply": "Sure, removing the cap entirely.",
        "amendment": {
            "rationale": "drop the knob range",
            "proposed_contract": bad,
        },
    })

    with kb.connect(board="vfail") as conn:
        sid = kb.open_steering_session(conn, board="vfail", mode="runtime_evolution")["session_id"]
        result = kb.steer_send_message(
            conn, board="vfail", session_id=sid, owner_message="Remove the nudge cap.",
        )
        aid = result["amendment"]["amendment_id"]

    token = _amendment_token("vfail", aid)
    with kb.connect(board="vfail") as conn:
        kb.approve_contract_amendment(conn, aid, board="vfail", approver="owner", token=token)
        failed = kb.validate_contract_amendment(conn, aid, board="vfail")
        assert failed["status"] == kb.AMENDMENT_STATUS_VALIDATION_FAILED

        msg = kb.steer_reflect_amendment_state(
            conn, session_id=sid, amendment_id=aid, board="vfail",
        )
        assert msg["role"] == "system"
        errors = msg["attachments"]["validation_errors"]
        assert errors and any("invariant" in e for e in errors)
        assert "did not pass" in msg["content"].lower()

    # Live contract is completely untouched.
    assert kb.read_board_metadata("vfail")["contract_version"] == 1
    assert "range" in kb.read_board_metadata("vfail")["business_contract"]["tunables"]["max_nudges"]


# ---------------------------------------------------------------------------
# Degraded mode (no aux model)
# ---------------------------------------------------------------------------


def test_degraded_mode_returns_deterministic_message_no_amendment(fresh_home):
    # No aux model is configured in the isolated HERMES_HOME, so the CEO turn
    # must degrade gracefully: a clear system message, no crash, no amendment.
    _activate_board("degr", _base_contract())
    with kb.connect(board="degr") as conn:
        sid = kb.open_steering_session(conn, board="degr", mode="runtime_evolution")["session_id"]
        result = kb.steer_send_message(
            conn, board="degr", session_id=sid, owner_message="change something",
        )
        assert result["degraded"] is True
        assert result["amendment"] is None
        assert result["ceo_message"]["role"] == "system"
        assert "not configured" in result["ceo_message"]["content"].lower()
        assert result["ceo_message"]["attachments"]["degraded"] is True

        # No amendment was created.
        assert kb.build_contract_amendments_read_model(conn, board="degr")["pending"] == 0
        # The log has exactly the owner msg + the degraded system msg.
        roles = [m["role"] for m in kb.get_steering_messages(conn, sid, board="degr")]
        assert roles == ["owner", "system"]

    assert kb.read_board_metadata("degr")["contract_version"] == 1


# ---------------------------------------------------------------------------
# Coverage-gap drive (launch_buildout)
# ---------------------------------------------------------------------------


def test_launch_buildout_surfaces_coverage_gaps_to_model(fresh_home, monkeypatch):
    # A bare, pre-contract board: the coverage report should be full of gaps,
    # and the context builder must hand those gaps to the CEO model.
    kb.create_board("buildout", name="Build Out")
    assert kb.board_exists("buildout")

    captured: list = []
    _stub_ceo(monkeypatch, {
        "reply": "Let's start with your success signals. What does a win look like?",
        "amendment": None,
        "research_query": "",
    }, capture=captured)

    with kb.connect(board="buildout") as conn:
        sid = kb.open_steering_session(conn, board="buildout", mode="launch_buildout")["session_id"]
        result = kb.steer_send_message(
            conn, board="buildout", session_id=sid,
            owner_message="I want to build a board but I'm not sure how to specify it.",
        )
        assert result["amendment"] is None
        assert result["ceo_message"]["role"] == "ceo"

    # The model was handed the coverage report including the missing dimensions.
    assert captured, "the aux model was not called"
    payload = captured[0]
    assert payload["mode"] == "launch_buildout"
    coverage = payload["coverage"]
    assert coverage is not None
    assert "gaps" in coverage
    assert coverage["gaps"], "expected under-specified dimensions to be surfaced"
    # A pre-contract board with no answers is weak across the board.
    assert "outcome_signals" in coverage["gaps"]


def test_research_round_feeds_external_research_then_proposes(fresh_home, monkeypatch):
    # The CEO can ask for grounded research first; the engine runs it, records a
    # system message with citations, and re-invokes the CEO with the findings.
    contract = _base_contract()
    _activate_board("res", contract)
    proposed = _widen_nudges(contract)

    calls = {"n": 0}

    def _fake_call_model(system_prompt, user_payload, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"reply": "", "research_query": "carrier nudge cadence norms"}), False
        # Second call sees the research and proposes.
        assert user_payload["external_research"], "research not fed back to the CEO"
        return json.dumps({
            "reply": "Based on the research, widening the cap is sound.",
            "amendment": {"rationale": "widen per research", "proposed_contract": proposed},
        }), False

    monkeypatch.setattr(kli, "_call_model", _fake_call_model)
    monkeypatch.setattr(kli, "aux_configured", lambda: True)
    # Isolate research from the CEO aux call: stub the research entry point so
    # the engine's bounded research round returns canned grounded findings.
    monkeypatch.setattr(kli, "run_pre_interview_research", lambda q, **kw: kli.PreInterviewResearchResult(
        ok=True, degraded=False, grounded=True,
        items=[kli.ExternalResearchItem(query=q, summary="norms", sources=["https://example.com/cadence"])],
        sources=[{"title": "Carrier cadence", "url": "https://example.com/cadence", "snippet": "norms"}],
    ))

    with kb.connect(board="res") as conn:
        sid = kb.open_steering_session(conn, board="res", mode="runtime_evolution")["session_id"]
        result = kb.steer_send_message(
            conn, board="res", session_id=sid, owner_message="Should we nudge more?",
        )
        assert result["amendment"] is not None
        assert result["amendment"]["origin"] == "ceo"
        # A research system message was recorded before the CEO reply.
        roles = [m["role"] for m in kb.get_steering_messages(conn, sid, board="res")]
        assert "system" in roles
        research_msgs = [
            m for m in kb.get_steering_messages(conn, sid, board="res")
            if (m.get("attachments") or {}).get("research")
        ]
        assert research_msgs, "expected a recorded research message"


# ---------------------------------------------------------------------------
# Read-model surfacing
# ---------------------------------------------------------------------------


def test_learned_state_read_model_surfaces_steering_sessions(fresh_home, monkeypatch):
    contract = _base_contract()
    _activate_board("rm", contract)
    proposed = _widen_nudges(contract)
    _stub_ceo(monkeypatch, {
        "reply": "Proposing the change now.",
        "amendment": {"rationale": "widen", "proposed_contract": proposed},
    })
    with kb.connect(board="rm") as conn:
        sid = kb.open_steering_session(conn, board="rm", mode="runtime_evolution")["session_id"]
        result = kb.steer_send_message(
            conn, board="rm", session_id=sid, owner_message="bump it",
        )
        aid = result["amendment"]["amendment_id"]

        model = kb.build_learned_state_read_model(conn, board="rm")
        block = model["steering_sessions"]
        assert block["open"] == 1
        entry = block["sessions"][0]
        assert entry["session_id"] == sid
        assert entry["mode"] == "runtime_evolution"
        assert aid in entry["amendment_ids"]


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def _cli(args: list[str], hermes_home: Path, *, board: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "HERMES_HOME": str(hermes_home),
        "HERMES_KANBAN_BOARD": board,
        "PYTHONPATH": str(_WORKTREE),
    })
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", *args],
        env=env, cwd=str(_WORKTREE), capture_output=True, text=True, timeout=60,
    )


def test_cli_steer_open_send_list_show(fresh_home):
    _activate_board("cliboard", _base_contract())

    opened = _cli(["steer", "open", "--mode", "runtime_evolution", "--json"], fresh_home, board="cliboard")
    assert opened.returncode == 0, opened.stderr
    sid = json.loads(opened.stdout)["session_id"]

    # Degraded send (no aux model) still works end-to-end through the CLI.
    sent = _cli(["steer", "send", sid, "please change something"], fresh_home, board="cliboard")
    assert sent.returncode == 0, sent.stderr
    assert "[SYSTEM]" in sent.stdout

    listing = _cli(["steer", "list"], fresh_home, board="cliboard")
    assert listing.returncode == 0, listing.stderr
    assert sid in listing.stdout

    show = _cli(["steer", "show", sid, "--json"], fresh_home, board="cliboard")
    assert show.returncode == 0, show.stderr
    payload = json.loads(show.stdout)
    assert payload["session"]["session_id"] == sid
    assert [m["role"] for m in payload["messages"]] == ["owner", "system"]
