"""Tests for board-level kanban model routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from hermes_cli import kanban_model_routing as kmr


@dataclass
class _Task:
    id: str = "t_test"
    assignee: Optional[str] = "worker-profile"
    status: str = "ready"
    model_override: Optional[str] = None
    stage_key: Optional[str] = None
    action_key: Optional[str] = None


def test_normalize_runtime_models_roundtrip():
    raw = {
        "default": "composer-2.5-fast",
        "roles": {"ceo": "anthropic/claude-opus-4.8", "worker": "gpt-5.3-codex"},
        "task_types": {"triage": "composer-2.5-fast"},
        "stages": {"implementation": "gpt-5.3-codex"},
        "actions": {"review": "anthropic/claude-sonnet-4.6"},
    }
    normalized = kmr.normalize_runtime_models(raw)
    assert normalized == raw


def test_merge_model_routing_into_runtime():
    runtime = {"mode": "company", "profiles": {"worker": "worker-profile"}}
    merged = kmr.merge_model_routing_into_runtime(
        runtime,
        {
            "default": "composer-2.5-fast",
            "roles": {"worker": "gpt-5.3-codex"},
        },
    )
    assert merged["models"]["default"] == "composer-2.5-fast"
    assert merged["models"]["roles"]["worker"] == "gpt-5.3-codex"
    assert merged["mode"] == "company"


def test_resolve_worker_model_priority(monkeypatch):
    board_meta = {
        "slug": "demo",
        "runtime": {
            "profiles": {"worker": "worker-profile"},
            "models": {
                "default": "composer-2.5-fast",
                "roles": {"worker": "gpt-5.3-codex"},
                "actions": {"review": "anthropic/claude-sonnet-4.6"},
                "stages": {"build": "openai/gpt-5.5"},
                "task_types": {"triage": "composer-2.5-fast"},
            },
        },
    }

    monkeypatch.setattr(
        kmr,
        "_profile_role_map",
        lambda _board: {"worker-profile": "worker"},
    )

    assert kmr.resolve_worker_model(
        _Task(model_override="explicit-model"),
        board_meta,
    ) == "explicit-model"

    assert kmr.resolve_worker_model(
        _Task(action_key="review"),
        board_meta,
    ) == "anthropic/claude-sonnet-4.6"

    assert kmr.resolve_worker_model(
        _Task(stage_key="build"),
        board_meta,
    ) == "openai/gpt-5.5"

    assert kmr.resolve_worker_model(
        _Task(status="triage"),
        board_meta,
    ) == "composer-2.5-fast"

    assert kmr.resolve_worker_model(
        _Task(),
        board_meta,
    ) == "gpt-5.3-codex"

    empty_meta = {"slug": "demo", "runtime": {}}
    assert kmr.resolve_worker_model(_Task(), empty_meta) is None


def test_build_model_routing_read_model(monkeypatch):
    monkeypatch.setattr(
        kmr,
        "list_supported_model_slugs",
        lambda: ("composer-2.5-fast", "gpt-5.3-codex"),
    )
    import hermes_cli.kanban_db as kb

    monkeypatch.setattr(
        kb,
        "board_role_profiles",
        lambda _slug: {"worker": "worker-profile", "ceo": "ceo-profile"},
    )
    meta = {
        "slug": "demo",
        "runtime": {
            "models": {
                "roles": {"worker": "gpt-5.3-codex", "ceo": "unknown-model"},
            }
        },
    }
    read_model = kmr.build_model_routing_read_model(meta)
    assert read_model["models"]["roles"]["worker"] == "gpt-5.3-codex"
    assert any("unknown-model" in w for w in read_model["warnings"])
    assert any(row["role"] == "worker" for row in read_model["role_profiles"])
