"""Tests for native Kanban Pixel gates and CLI smoke."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


_WORKTREE = Path(__file__).resolve().parents[2]


def _cli(args: list[str], hermes_home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "HERMES_HOME": str(hermes_home),
            "PYTHONPATH": str(_WORKTREE),
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", *args],
        env=env,
        cwd=str(_WORKTREE),
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_pixel_brief_json_smoke(kanban_home: Path) -> None:
    raw = kc.run_slash("pixel brief --json")
    payload = json.loads(raw)

    assert payload["board"] == "default"
    assert payload["pixel_enabled"] is False
    assert payload["done_gates"] == []


def test_missing_pixel_evidence_blocks_complete(kanban_home: Path) -> None:
    kb.set_pixel_goal("default", "Book qualified meetings", ["meeting booked"])
    kb.set_pixel_stage_event("default", "contact", "meeting_booked")

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Contact lead", stage_key="contact")

        with pytest.raises(kb.PixelDoneGateError) as excinfo:
            kb.complete_task(conn, task_id, summary="done")

        verdict = excinfo.value.verdict
        assert verdict["ok"] is False
        assert {
            blocker["code"] for blocker in verdict["blockers"]
        } == {"missing_stage_conversion_evidence"}
        assert kb.get_task(conn, task_id).status != "done"


def test_pixel_event_evidence_allows_complete(kanban_home: Path) -> None:
    kb.set_pixel_goal("default", "Book qualified meetings", ["meeting booked"])
    kb.set_pixel_stage_event("default", "contact", "meeting_booked")

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Contact lead", stage_key="contact")
        kb.record_pixel_event(
            conn,
            event_type="meeting_booked",
            stage_key="contact",
            status="pass",
            evidence="calendar invite created",
            task_id=task_id,
        )

        assert kb.complete_task(conn, task_id, summary="booked") is True
        assert kb.get_task(conn, task_id).status == "done"


@pytest.mark.slow  # spawns real `python -m hermes_cli.main` subprocesses
def test_cli_pixel_gate_failure_returns_nonzero_then_allows_completion(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()

    goal = _cli(
        ["pixel", "goal", "Book qualified meetings", "--success", "meeting booked"],
        hermes_home,
    )
    assert goal.returncode == 0, goal.stderr

    stage = _cli(["pixel", "stage", "contact", "--event", "meeting_booked"], hermes_home)
    assert stage.returncode == 0, stage.stderr

    created = _cli(["create", "Contact lead", "--stage", "contact", "--json"], hermes_home)
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    blocked = _cli(["complete", task_id, "--summary", "done"], hermes_home)
    assert blocked.returncode != 0
    assert "pixel done gate failed" in blocked.stderr

    shown = _cli(["show", task_id, "--json"], hermes_home)
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["task"]["status"] != "done"

    event = _cli(
        [
            "pixel",
            "event",
            "meeting_booked",
            "--stage",
            "contact",
            "--task",
            task_id,
            "--status",
            "pass",
            "--evidence",
            "calendar invite created",
        ],
        hermes_home,
    )
    assert event.returncode == 0, event.stderr

    done = _cli(["pixel", "done", task_id], hermes_home)
    assert done.returncode == 0, done.stderr

    completed = _cli(["complete", task_id, "--summary", "booked"], hermes_home)
    assert completed.returncode == 0, completed.stderr


def test_pixel_claim_conflict_and_release(kanban_home: Path) -> None:
    with kb.connect() as conn:
        claim = kb.claim_pixel_lane(
            conn,
            lane_id="lead-123",
            agent_id="agent-a",
            evidence="working lead 123",
        )

        with pytest.raises(ValueError, match="pixel claim conflict"):
            kb.claim_pixel_lane(
                conn,
                lane_id="lead-123",
                agent_id="agent-b",
                evidence="also working lead 123",
            )

        released = kb.release_pixel_claim(
            conn,
            claim_id=claim.id,
            agent_id="agent-a",
            evidence="handoff complete",
        )
        assert released.active is False

        next_claim = kb.claim_pixel_lane(
            conn,
            lane_id="lead-123",
            agent_id="agent-b",
            evidence="lane is free",
        )
        assert next_claim.active is True


def test_non_pixel_completion_still_works(kanban_home: Path) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Plain task")

        assert kb.complete_task(conn, task_id, summary="done") is True
        assert kb.get_task(conn, task_id).status == "done"
