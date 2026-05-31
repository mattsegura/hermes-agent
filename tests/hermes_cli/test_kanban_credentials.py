"""Tests for per-board encrypted launch credentials."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_credentials as kc
from hermes_cli import kanban_db as kb

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "launch_intake" / "grow_app_one_week.contract.json"


@pytest.fixture()
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


def _contract_with_launch_inputs():
    with open(_FIXTURE, encoding="utf-8") as fh:
        contract = json.load(fh)
    contract["launch_required_inputs"] = [
        {
            "key": "revenuecat_api_key",
            "label": "RevenueCat secret API key",
            "type": "secret",
            "required": True,
            "inject_path": None,
        },
        {
            "key": "revenuecat_project_id",
            "label": "RevenueCat project ID",
            "type": "string",
            "required": True,
            "inject_path": "provider_policy.systems.read:subscription_revenue.project_id",
        },
    ]
    return kb.normalize_board_operating_contract(contract)


@pytest.fixture()
def cred_board(fresh_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(fresh_home))
    slug = "cred-test"
    contract = _contract_with_launch_inputs()
    kb.create_board(slug, name="Cred Test", contract=contract, launch_phase="contract_review")
    return slug, contract


def test_assess_missing_credentials(cred_board):
    slug, contract = cred_board
    status = kc.assess_board_credentials(slug, contract)
    assert status["ok"] is False
    assert "revenuecat_api_key" in status["missing_keys"]
    assert all("ciphertext" not in str(row) for row in status["inputs"])


def test_set_and_assess_encrypted(cred_board):
    slug, contract = cred_board
    result = kc.set_board_credential(
        slug, "revenuecat_api_key", "sk_test_secret_value", spec={
            "key": "revenuecat_api_key", "type": "secret", "label": "RC key",
        },
    )
    assert result["fingerprint"].startswith("sha256:")
    vault_path = kc.vault_path(slug)
    assert vault_path.exists()
    raw = vault_path.read_text(encoding="utf-8")
    assert "sk_test_secret_value" not in raw

    status = kc.assess_board_credentials(slug, contract)
    rc_row = next(r for r in status["inputs"] if r["key"] == "revenuecat_api_key")
    assert rc_row["provisioned"] is True
    assert rc_row["source"] == "board_vault"


def test_submit_launch_credentials(cred_board):
    slug, contract = cred_board
    result = kc.submit_launch_credentials(
        slug,
        {"revenuecat_api_key": "sk_live_abc", "unknown_key": "ignored"},
        contract=contract,
    )
    assert "revenuecat_api_key" in result["stored_keys"]
    assert "unknown_key" in result["ignored_keys"]


def test_validate_readiness_blocks_on_missing_credentials(cred_board):
    slug, contract = cred_board
    readiness = kb.validate_business_runtime_contract(contract, board=slug)
    assert readiness["credentials"]["missing_keys"]
    assert any(m.startswith("launch_credentials.") for m in readiness["missing"])


def test_validate_readiness_passes_when_credentials_set(cred_board):
    slug, contract = cred_board
    kc.set_board_credential(slug, "revenuecat_api_key", "sk_test", spec={"type": "secret"})
    kc.set_board_credential(slug, "revenuecat_project_id", "proj_1", spec={"type": "string"})
    readiness = kb.validate_business_runtime_contract(contract, board=slug)
    cred_missing = readiness["credentials"].get("missing_keys") or []
    assert "revenuecat_api_key" not in cred_missing


def test_board_credentials_env_injects_secrets(cred_board, monkeypatch):
    slug, contract = cred_board
    kc.set_board_credential(slug, "revenuecat_api_key", "sk_inject_me", spec={"type": "secret"})

    captured = {}

    class FakeProc:
        pid = 99

    def fake_popen(cmd, *args, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    task = kb.Task(
        id="t_cred",
        title="cred task",
        body=None,
        assignee="land-operator",
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
    kb._default_spawn(task, "/tmp/ws", board=slug)
    assert captured["env"]["REVENUECAT_API_KEY"] == "sk_inject_me"


def test_public_summary_never_contains_secret_values(cred_board):
    slug, contract = cred_board
    secret = "super_secret_token_12345"
    kc.set_board_credential(slug, "revenuecat_api_key", secret, spec={"type": "secret"})
    status = kc.assess_board_credentials(slug, contract)
    dumped = json.dumps(status)
    assert secret not in dumped
