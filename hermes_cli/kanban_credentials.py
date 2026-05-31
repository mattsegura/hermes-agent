#!/usr/bin/env python3
"""Per-board encrypted credential vault for kanban launch provisioning.

Secrets declared by ``launch_required_inputs`` (from capability discovery) are
stored encrypted at rest under each board directory, provisioned into Hermes
profile ``.env`` files at launch, and injected into worker subprocess env at
dispatch time. Values never appear in ``board.json``, launch tool output, or
logs — only fingerprints and provisioned/missing status are public.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import stat
import time
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

VAULT_VERSION = 1
VAULT_FILENAME = "credentials.vault"
VAULT_KEY_REL = Path("kanban") / ".vault_key"

# Catalog / contract keys -> Hermes env var names used by integrations.
_ENV_VAR_OVERRIDES: dict[str, str] = {
    "revenuecat_api_key": "REVENUECAT_API_KEY",
    "stripe_secret_key": "STRIPE_SECRET_KEY",
    "mixpanel_service_secret": "MIXPANEL_SERVICE_SECRET",
    "posthog_project_api_key": "POSTHOG_PROJECT_API_KEY",
    "intercom_access_token": "INTERCOM_ACCESS_TOKEN",
    "meta_access_token": "META_ACCESS_TOKEN",
    "google_ads_developer_token": "GOOGLE_ADS_DEVELOPER_TOKEN",
    "google_ads_refresh_token": "GOOGLE_ADS_REFRESH_TOKEN",
    "asa_client_secret": "APPLE_SEARCH_ADS_CLIENT_SECRET",
    "asc_private_key": "APP_STORE_CONNECT_PRIVATE_KEY",
}


def _kanban_home() -> Path:
    from hermes_cli.kanban_db import kanban_home

    return kanban_home()


def _board_dir(board: Optional[str]) -> Path:
    from hermes_cli.kanban_db import board_dir

    return board_dir(board)


def vault_key_path() -> Path:
    return _kanban_home() / VAULT_KEY_REL


def vault_path(board: Optional[str]) -> Path:
    return _board_dir(board) / VAULT_FILENAME


def credential_env_var(key: str, spec: Optional[dict[str, Any]] = None) -> str:
    """Resolve the env var name for a launch-required input key."""
    if spec:
        explicit = str(spec.get("env_var") or "").strip()
        if explicit:
            return explicit
    normalized = str(key or "").strip()
    if not normalized:
        raise ValueError("credential key is required")
    return _ENV_VAR_OVERRIDES.get(normalized, normalized.upper())


def secret_fingerprint(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def _secure_file(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _load_fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError(
            "kanban credentials require the cryptography package "
            "(pip install cryptography)"
        ) from exc
    return Fernet


def _load_or_create_vault_key() -> bytes:
    path = vault_key_path()
    if path.exists():
        raw = path.read_bytes().strip()
        if raw:
            return raw
    Fernet = _load_fernet()
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    _secure_file(path)
    return key


def _encrypt(value: str) -> str:
    Fernet = _load_fernet()
    fernet = Fernet(_load_or_create_vault_key())
    token = fernet.encrypt(value.encode("utf-8"))
    return base64.urlsafe_b64encode(token).decode("ascii")


def _decrypt(ciphertext: str) -> str:
    Fernet = _load_fernet()
    fernet = Fernet(_load_or_create_vault_key())
    token = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
    return fernet.decrypt(token).decode("utf-8")


def _empty_vault() -> dict[str, Any]:
    return {"version": VAULT_VERSION, "entries": {}}


def load_vault(board: Optional[str]) -> dict[str, Any]:
    path = vault_path(board)
    if not path.exists():
        return _empty_vault()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        _log.warning("kanban credentials: unreadable vault at %s: %s", path, exc)
        return _empty_vault()
    if not isinstance(doc, dict):
        return _empty_vault()
    entries = doc.get("entries")
    if not isinstance(entries, dict):
        doc["entries"] = {}
    doc.setdefault("version", VAULT_VERSION)
    return doc


def save_vault(board: Optional[str], vault: dict[str, Any]) -> Path:
    path = vault_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(vault, indent=2, sort_keys=True)
    tmp = path.with_suffix(f".vault.tmp-{os.getpid()}")
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(path)
    _secure_file(path)
    return path


def launch_required_input_specs(contract: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return normalized launch_required_inputs from a board contract."""
    if not isinstance(contract, dict):
        return []
    from hermes_cli.kanban_db import _normalize_amendment_required_inputs

    return _normalize_amendment_required_inputs(contract.get("launch_required_inputs"))


def _global_env_has_value(env_var: str) -> bool:
    from hermes_cli.config import get_env_value

    val = get_env_value(env_var)
    return bool(str(val or "").strip())


def assess_board_credentials(
    board: Optional[str],
    contract: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Return a public credential readiness report (no secret values)."""
    specs = launch_required_input_specs(contract)
    if not specs:
        return {
            "ok": True,
            "required_count": 0,
            "provisioned_count": 0,
            "missing_count": 0,
            "missing_keys": [],
            "inputs": [],
        }

    vault = load_vault(board) if board is not None else _empty_vault()
    entries = vault.get("entries") or {}
    inputs: list[dict[str, Any]] = []
    missing_keys: list[str] = []

    for spec in specs:
        key = spec["key"]
        env_var = credential_env_var(key, spec)
        entry = entries.get(key) if isinstance(entries, dict) else None
        vault_fp = entry.get("fingerprint") if isinstance(entry, dict) else None
        global_ok = _global_env_has_value(env_var)
        vault_ok = bool(vault_fp)
        provisioned = global_ok or vault_ok
        if spec.get("required", True) and not provisioned:
            missing_keys.append(key)
        source = "global_env" if global_ok else "board_vault" if vault_ok else None
        inputs.append({
            "key": key,
            "label": spec.get("label") or key,
            "type": spec.get("type") or "string",
            "required": bool(spec.get("required", True)),
            "env_var": env_var,
            "provisioned": provisioned,
            "source": source,
            "fingerprint": vault_fp if vault_ok else None,
        })

    required = [s for s in specs if s.get("required", True)]
    provisioned_count = sum(1 for row in inputs if row["provisioned"])
    return {
        "ok": not missing_keys,
        "required_count": len(required),
        "provisioned_count": provisioned_count,
        "missing_count": len(missing_keys),
        "missing_keys": missing_keys,
        "inputs": inputs,
    }


def set_board_credential(
    board: Optional[str],
    key: str,
    value: str,
    *,
    spec: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Encrypt and store one board credential. Never logs the value."""
    normalized_key = str(key or "").strip()
    text = str(value or "")
    if not normalized_key:
        raise ValueError("credential key is required")
    if not text.strip():
        raise ValueError("credential value must be non-empty")

    env_var = credential_env_var(normalized_key, spec)
    vault = load_vault(board)
    entries = vault.setdefault("entries", {})
    entries[normalized_key] = {
        "env_var": env_var,
        "ciphertext": _encrypt(text),
        "fingerprint": secret_fingerprint(text),
        "label": (spec or {}).get("label") or normalized_key,
        "updated_at": int(time.time()),
    }
    save_vault(board, vault)
    return {
        "ok": True,
        "key": normalized_key,
        "env_var": env_var,
        "fingerprint": entries[normalized_key]["fingerprint"],
        "stored_in": str(vault_path(board)),
    }


def submit_launch_credentials(
    board: Optional[str],
    inputs: Any,
    *,
    contract: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Store multiple launch credentials and return updated public status."""
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be a JSON object mapping key -> value")
    specs = launch_required_input_specs(contract)
    spec_by_key = {spec["key"]: spec for spec in specs}
    stored: list[str] = []
    ignored: list[str] = []
    errors: list[str] = []

    for raw_key, raw_value in inputs.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        spec = spec_by_key.get(key)
        if spec is None:
            ignored.append(key)
            continue
        if spec.get("type") == "secret" and not str(raw_value or "").strip():
            errors.append(f"{key}: secret value must be non-empty")
            continue
        try:
            set_board_credential(board, key, str(raw_value), spec=spec)
            stored.append(key)
        except ValueError as exc:
            errors.append(f"{key}: {exc}")

    if errors:
        raise ValueError("invalid launch credentials: " + "; ".join(errors))

    status = assess_board_credentials(board, contract)
    return {
        "ok": status["ok"],
        "stored_keys": stored,
        "ignored_keys": ignored,
        "credentials": status,
    }


def _resolve_credential_value(
    board: Optional[str],
    key: str,
    env_var: str,
    entries: dict[str, Any],
) -> Optional[str]:
    if _global_env_has_value(env_var):
        from hermes_cli.config import get_env_value

        return str(get_env_value(env_var) or "")
    entry = entries.get(key) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        return None
    ciphertext = entry.get("ciphertext")
    if not ciphertext:
        return None
    try:
        return _decrypt(str(ciphertext))
    except Exception as exc:
        _log.warning(
            "kanban credentials: failed to decrypt %s for board %r: %s",
            key,
            board,
            exc,
        )
        return None


def board_credentials_env(board: Optional[str]) -> dict[str, str]:
    """Return env vars to inject into worker subprocesses (in-memory only)."""
    from hermes_cli.kanban_db import read_board_metadata, _metadata_as_business_contract

    try:
        meta = read_board_metadata(board)
        contract = _metadata_as_business_contract(meta)
    except Exception:
        return {}

    specs = launch_required_input_specs(contract)
    if not specs:
        return {}

    vault = load_vault(board)
    entries = vault.get("entries") or {}
    env: dict[str, str] = {}
    for spec in specs:
        if spec.get("type") != "secret":
            continue
        key = spec["key"]
        env_var = credential_env_var(key, spec)
        value = _resolve_credential_value(board, key, env_var, entries)
        if value:
            env[env_var] = value
    return env


def provision_board_credentials_to_profile(
    board: Optional[str],
    profile: str,
    *,
    contract: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Write decrypted board secrets into a profile's ``.env`` via Hermes helpers."""
    from hermes_cli.config import save_env_value
    from hermes_cli.profiles import resolve_profile_env

    if contract is None:
        from hermes_cli.kanban_db import read_board_metadata, _metadata_as_business_contract

        meta = read_board_metadata(board)
        contract = _metadata_as_business_contract(meta)

    specs = launch_required_input_specs(contract)
    if not specs:
        return {"ok": True, "provisioned": [], "profile": profile}

    profile_home = resolve_profile_env(profile)

    def _writer(name: str, val: str) -> None:
        prev_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = profile_home
        try:
            save_env_value(name, val)
        finally:
            if prev_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = prev_home

    vault = load_vault(board)
    entries = vault.get("entries") or {}
    provisioned: list[str] = []
    for spec in specs:
        if spec.get("type") != "secret":
            continue
        key = spec["key"]
        env_var = credential_env_var(key, spec)
        value = _resolve_credential_value(board, key, env_var, entries)
        if not value:
            continue
        _writer(env_var, value)
        provisioned.append(env_var)

    return {
        "ok": True,
        "profile": profile,
        "profile_home": profile_home,
        "provisioned": provisioned,
    }


def launch_credentials_questions(status: dict[str, Any]) -> list[str]:
    """Human prompts for missing launch credentials."""
    missing = [
        row for row in (status.get("inputs") or [])
        if row.get("required") and not row.get("provisioned")
    ]
    if not missing:
        return []
    lines = [
        "Before launch, Hermes still needs credentials for these integrations:"
    ]
    for row in missing:
        label = row.get("label") or row.get("key")
        env_var = row.get("env_var") or row.get("key")
        lines.append(
            f"- {label} (key `{row['key']}`, env `{env_var}`)"
        )
    lines.append(
        "Add them with `hermes kanban boards credentials set <key>` or the "
        "kanban_submit_launch_credentials tool — secrets are encrypted and "
        "never stored in board.json."
    )
    return lines
