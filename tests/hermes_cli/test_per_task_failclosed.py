"""G2: per-task execution-contract fail-open conversions.

Three sub-fixes, each FLAG-GATED and DEFAULT-OFF. Every test asserts BOTH the
default-off path (byte-identical to today) AND the flag-on path (fail-closed):

  (a) HERMES_KANBAN_REQUIRE_CONTRACT_DEFAULTS -- absent require_* on a MANAGED
      board fails closed (empty contract -> blocked) instead of open.
  (b) HERMES_KANBAN_ENFORCE_WORKER_TOOLSETS -- the spawned worker argv is
      constrained to the declared worker_envelope toolsets via ``--toolsets``.
  (c) HERMES_KANBAN_BRIDGE_TOOL_POLICY -- the board contract's
      runtime.tool_policy.blocked_tools is bridged into the action gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb

# A minimal VALID workflow (the normalizer requires >=1 stage). It declares no
# require_semantics, so absence is meaningful for sub-fix (a).
_MIN_WORKFLOW = {
    "id": "flow",
    "stages": [{"key": "execute", "actions": [{"key": "do"}]}],
    "workstreams": [],
}


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_REQUIRE_CONTRACT_DEFAULTS",
        "HERMES_KANBAN_ENFORCE_WORKER_TOOLSETS",
        "HERMES_KANBAN_BRIDGE_TOOL_POLICY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _managed_board_empty_contract(slug: str = "managed") -> None:
    """A managed board whose execution contract declares NOTHING.

    No require_*, no worker_envelopes, no provider_policy. Today this yields an
    ungoverned worker (every dispatch blocker is conditional on the contract
    declaring something). It is the empty-contract fail-open the audit found.
    """
    kb.write_board_metadata(
        slug,
        runtime={"mode": "company"},
        workflow=_MIN_WORKFLOW,
    )


def _goal_board_empty_contract(slug: str = "casual") -> None:
    """A NON-managed (goal-mode) board with an equally empty contract.

    Used to prove sub-fix (a) is scoped strictly to managed boards.
    """
    kb.write_board_metadata(
        slug,
        runtime={"mode": "goal"},
        workflow=_MIN_WORKFLOW,
    )


# ===========================================================================
# (a) ABSENT-DEFAULT FAIL-CLOSED (managed boards only)
# ===========================================================================

def test_a_absent_require_stays_open_by_default(fresh_home):
    """DEFAULT-OFF: an empty managed contract resolves require_* falsy and the
    dispatch gate yields ok=True (today's fail-open behavior, unchanged)."""
    _managed_board_empty_contract()
    with kb.connect(board="managed") as conn:
        tid = kb.create_task(conn, title="ungoverned", assignee="worker", board="managed", initial_status="blocked")
        contract = kb.resolve_task_contract(kb.get_task(conn, tid), board="managed")
        assert contract["require_worker_envelopes"] is False
        assert contract["require_provider_policy"] is False
        assert contract["require_semantics"] is False
        assert contract["contract_defaults_applied"] == []
        verdict = kb.evaluate_dispatch_eligibility(conn, tid, board="managed")
        assert verdict["ok"] is True
        assert verdict["blockers"] == []


def test_a_absent_require_fails_closed_when_flag_on(fresh_home, monkeypatch):
    """FLAG-ON + managed board: absent require_* are promoted to True and the
    empty contract fails CLOSED at the dispatch gate."""
    monkeypatch.setenv("HERMES_KANBAN_REQUIRE_CONTRACT_DEFAULTS", "1")
    _managed_board_empty_contract()
    with kb.connect(board="managed") as conn:
        tid = kb.create_task(conn, title="ungoverned", assignee="worker", board="managed", initial_status="blocked")
        contract = kb.resolve_task_contract(kb.get_task(conn, tid), board="managed")
        assert contract["require_worker_envelopes"] is True
        assert contract["require_provider_policy"] is True
        assert contract["require_semantics"] is True
        assert set(contract["contract_defaults_applied"]) == {
            "require_worker_envelopes",
            "require_provider_policy",
            "require_semantics",
        }
        verdict = kb.evaluate_dispatch_eligibility(conn, tid, board="managed")
        assert verdict["ok"] is False
        codes = {b["code"] for b in verdict["blockers"]}
        assert "missing_worker_envelope" in codes
        assert "missing_provider_policy" in codes
        # semantics: the empty task has no goal/workstream/stage/action.
        assert "missing_semantic_goal" in codes
        assert "missing_semantic_workstream" in codes
        assert "missing_semantic_stage" in codes
        assert "missing_semantic_action" in codes


def test_a_flag_on_does_not_affect_non_managed_board(fresh_home, monkeypatch):
    """FLAG-ON but a NON-managed (goal) board is untouched -- scoped strictly to
    MANAGED_BOARD_RUNTIME_MODES, so casual boards never fail closed."""
    monkeypatch.setenv("HERMES_KANBAN_REQUIRE_CONTRACT_DEFAULTS", "1")
    _goal_board_empty_contract()
    with kb.connect(board="casual") as conn:
        tid = kb.create_task(conn, title="casual work", assignee="worker", board="casual")
        contract = kb.resolve_task_contract(kb.get_task(conn, tid), board="casual")
        assert contract["require_worker_envelopes"] is False
        assert contract["require_provider_policy"] is False
        assert contract["require_semantics"] is False
        assert contract["contract_defaults_applied"] == []
        verdict = kb.evaluate_dispatch_eligibility(conn, tid, board="casual")
        assert verdict["ok"] is True


def test_a_explicit_false_is_honored_when_flag_on(fresh_home, monkeypatch):
    """FLAG-ON: only genuine ABSENCE is promoted. An explicit ``false`` on a
    managed board is honored (not overridden to True)."""
    monkeypatch.setenv("HERMES_KANBAN_REQUIRE_CONTRACT_DEFAULTS", "1")
    kb.write_board_metadata(
        "managed",
        runtime={
            "mode": "company",
            "require_worker_envelopes": False,
            "require_provider_policy": False,
        },
        workflow={
            "id": "flow",
            "require_semantics": False,
            "stages": [{"key": "execute", "actions": [{"key": "do"}]}],
            "workstreams": [],
        },
    )
    with kb.connect(board="managed") as conn:
        tid = kb.create_task(conn, title="explicitly-loose", assignee="worker", board="managed", initial_status="blocked")
        contract = kb.resolve_task_contract(kb.get_task(conn, tid), board="managed")
        assert contract["require_worker_envelopes"] is False
        assert contract["require_provider_policy"] is False
        assert contract["require_semantics"] is False
        assert contract["contract_defaults_applied"] == []
        verdict = kb.evaluate_dispatch_eligibility(conn, tid, board="managed")
        assert verdict["ok"] is True


def test_a_config_yaml_flag_resolution(fresh_home, monkeypatch):
    """FIX 7: the flag resolves from a REAL config.yaml
    ``kanban.require_contract_defaults`` when the env var is unset, exercising
    the actual config-layer branch of ``_resolve_kanban_bool_flag`` (no
    monkeypatch of the resolver). env > config.yaml kanban.* > default."""
    import yaml as _yaml

    monkeypatch.delenv("HERMES_KANBAN_REQUIRE_CONTRACT_DEFAULTS", raising=False)
    # Write a genuine config.yaml under the fresh HERMES_HOME so load_config()
    # reads it through the normal pipeline.
    config_path = fresh_home / "config.yaml"
    config_path.write_text(
        _yaml.safe_dump({"kanban": {"require_contract_defaults": True}}),
        encoding="utf-8",
    )
    # Defensively clear the config caches so the just-written file is read
    # (the caches are keyed on mtime/size, but be explicit for test isolation).
    try:
        import hermes_cli.config as _cfgmod
        _cfgmod._LOAD_CONFIG_CACHE.clear()
        _cfgmod._RAW_CONFIG_CACHE.clear()
    except Exception:
        pass

    # Sanity: the REAL resolver (not monkeypatched) now reports True from config.
    assert kb._resolve_kanban_bool_flag(
        "HERMES_KANBAN_REQUIRE_CONTRACT_DEFAULTS", "require_contract_defaults"
    ) is True
    assert kb._require_contract_defaults_enabled() is True

    _managed_board_empty_contract()
    with kb.connect(board="managed") as conn:
        tid = kb.create_task(conn, title="cfg", assignee="worker", board="managed", initial_status="blocked")
        contract = kb.resolve_task_contract(kb.get_task(conn, tid), board="managed")
        # The config-layer flag promoted the absent require_* defaults.
        assert contract["require_worker_envelopes"] is True
        assert contract["require_provider_policy"] is True
        assert contract["require_semantics"] is True
        assert set(contract["contract_defaults_applied"]) == {
            "require_worker_envelopes",
            "require_provider_policy",
            "require_semantics",
        }


def test_a_config_yaml_flag_false_keeps_default_off(fresh_home, monkeypatch):
    """FIX 7 companion: a real config.yaml with the flag set FALSE (or absent)
    keeps the default-off behavior -- the config-layer branch is exercised in
    BOTH directions, not just the promote path."""
    import yaml as _yaml

    monkeypatch.delenv("HERMES_KANBAN_REQUIRE_CONTRACT_DEFAULTS", raising=False)
    config_path = fresh_home / "config.yaml"
    config_path.write_text(
        _yaml.safe_dump({"kanban": {"require_contract_defaults": False}}),
        encoding="utf-8",
    )
    try:
        import hermes_cli.config as _cfgmod
        _cfgmod._LOAD_CONFIG_CACHE.clear()
        _cfgmod._RAW_CONFIG_CACHE.clear()
    except Exception:
        pass

    assert kb._require_contract_defaults_enabled() is False
    _managed_board_empty_contract()
    with kb.connect(board="managed") as conn:
        tid = kb.create_task(conn, title="cfg-off", assignee="worker", board="managed", initial_status="blocked")
        contract = kb.resolve_task_contract(kb.get_task(conn, tid), board="managed")
        assert contract["require_worker_envelopes"] is False
        assert contract["contract_defaults_applied"] == []


# ===========================================================================
# (b) WORKER-ENVELOPE TOOLSET INJECTION
# ===========================================================================

def _spawn_board_with_envelope(slug: str, *, toolsets) -> None:
    """Managed board whose ``worker`` profile envelope declares ``toolsets``.

    ``toolsets=None`` => no envelope declared for that worker (no flag should
    ever be injected, even with the enforce flag on).
    """
    worker_envelopes = {}
    if toolsets is not None:
        worker_envelopes["worker"] = {"toolsets": toolsets, "capabilities": []}
    kb.write_board_metadata(
        slug,
        runtime={"mode": "company", "worker_envelopes": worker_envelopes},
        workflow=_MIN_WORKFLOW,
    )


def _capture_spawn_argv(monkeypatch, task, workspace, board):
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(kb, "_kanban_worker_skill_available", lambda _h: False)
    kb._default_spawn(task, str(workspace), board=board)
    return captured["cmd"]


def _parse_spawn_toolsets(cmd):
    """Parse the spawned worker argv with the REAL ``hermes`` parser and return
    the resolved ``args.toolsets``.

    This is the crux of FIX 2: the worker keeps whatever toolset the parser
    actually resolves -- a string-match on argv is NOT sufficient because the
    ``chat`` subparser redefines ``-t/--toolsets`` (default=None) and will
    silently null any ``--toolsets`` placed BEFORE ``chat``. We therefore drive
    the same parse path ``hermes`` itself uses:

      1. strip the leading ``hermes`` invocation prefix (everything up to and
         including the ``chat`` subcommand's owning argv -- here we just locate
         the ``-p``/``--profile`` pair and drop it, mirroring
         ``main._apply_profile_override`` which removes it before argparse),
      2. drop the bare ``hermes`` exe / ``-m module`` prefix tokens that precede
         the first recognised top-level flag,
      3. parse the remainder with ``build_top_level_parser()`` (chat registered,
         exactly as ``main.main`` wires it).
    """
    import argparse
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, chat_parser = build_top_level_parser()
    # main.main() sets this so the chat subparser is dispatchable.
    chat_parser.set_defaults(func=lambda *a, **k: None)

    argv = list(cmd)
    # (1) Strip the ``-p``/``--profile`` pair the real launcher consumes BEFORE
    # argparse ever sees it (main._apply_profile_override).
    stripped = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-p", "--profile") and i + 1 < len(argv):
            i += 2
            continue
        if a.startswith("--profile="):
            i += 1
            continue
        stripped.append(a)
        i += 1
    # (2) Drop the bare exe / module-launch prefix: everything before the first
    # token the top-level parser owns. ``--accept-hooks`` is always present in a
    # worker spawn and is a recognised top-level flag, so cut there.
    if "--accept-hooks" in stripped:
        start = stripped.index("--accept-hooks")
        stripped = stripped[start:]
    # (3) parse_known_args so any leftover engine-only flags don't abort.
    ns, _unknown = parser.parse_known_args(stripped)
    return getattr(ns, "toolsets", None)


def test_b_no_toolsets_flag_by_default(fresh_home, monkeypatch):
    """DEFAULT-OFF: even with an envelope present, the worker resolves to the
    UNRESTRICTED (None) toolset -- the parser sees no constraint."""
    _spawn_board_with_envelope("spawnboard", toolsets=["web", "kanban"])
    with kb.connect(board="spawnboard") as conn:
        tid = kb.create_task(conn, title="w", assignee="worker", board="spawnboard", initial_status="blocked")
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task, board="spawnboard")
        cmd = _capture_spawn_argv(monkeypatch, task, workspace, "spawnboard")
    assert "--toolsets" not in cmd, f"unexpected --toolsets in default-off argv: {cmd}"
    # And the real parser confirms: no constraint resolved.
    assert _parse_spawn_toolsets(cmd) is None


def test_b_toolsets_injected_when_flag_on_and_envelope_present(fresh_home, monkeypatch):
    """FLAG-ON + declared envelope: the REAL parser resolves
    ``args.toolsets == 'web,kanban'`` (the envelope's declared subset).

    This is the FIX 2 regression: it FAILS before the fix because the flag was
    appended BEFORE ``chat`` and the chat subparser nulls it -> args.toolsets is
    None and the worker keeps its full profile toolset (silently inert)."""
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_WORKER_TOOLSETS", "1")
    _spawn_board_with_envelope("spawnboard", toolsets=["web", "kanban"])
    with kb.connect(board="spawnboard") as conn:
        tid = kb.create_task(conn, title="w", assignee="worker", board="spawnboard", initial_status="blocked")
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task, board="spawnboard")
        cmd = _capture_spawn_argv(monkeypatch, task, workspace, "spawnboard")
    assert "--toolsets" in cmd, f"spawn argv missing --toolsets: {cmd}"
    # Placement contract: the flag MUST come AFTER 'chat' so the chat subparser
    # receives it (the whole point of the fix).
    assert cmd.index("--toolsets") > cmd.index("chat"), (
        f"--toolsets must come AFTER 'chat' so the subparser receives it: {cmd}"
    )
    # The authoritative assertion: PARSE the argv and confirm the resolved value.
    assert _parse_spawn_toolsets(cmd) == "web,kanban", (
        f"parser must resolve the envelope toolset, got {_parse_spawn_toolsets(cmd)!r}"
    )


def test_b_no_flag_when_envelope_absent_even_if_enabled(fresh_home, monkeypatch):
    """FLAG-ON but NO envelope declared for this worker: the parser resolves to
    the UNRESTRICTED (None) toolset (no accidental lockout)."""
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_WORKER_TOOLSETS", "1")
    _spawn_board_with_envelope("spawnboard", toolsets=None)
    with kb.connect(board="spawnboard") as conn:
        tid = kb.create_task(conn, title="w", assignee="worker", board="spawnboard", initial_status="blocked")
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task, board="spawnboard")
        cmd = _capture_spawn_argv(monkeypatch, task, workspace, "spawnboard")
    assert "--toolsets" not in cmd, (
        f"no envelope declared -> must NOT inject --toolsets: {cmd}"
    )
    assert _parse_spawn_toolsets(cmd) is None


def test_b_empty_toolsets_envelope_does_not_inject(fresh_home, monkeypatch):
    """FLAG-ON + envelope present but with an EMPTY toolsets list: still no
    flag (an empty subset would lock the worker out of everything)."""
    monkeypatch.setenv("HERMES_KANBAN_ENFORCE_WORKER_TOOLSETS", "1")
    _spawn_board_with_envelope("spawnboard", toolsets=[])
    with kb.connect(board="spawnboard") as conn:
        tid = kb.create_task(conn, title="w", assignee="worker", board="spawnboard", initial_status="blocked")
        task = kb.get_task(conn, tid)
        workspace = kb.resolve_workspace(task, board="spawnboard")
        cmd = _capture_spawn_argv(monkeypatch, task, workspace, "spawnboard")
    assert "--toolsets" not in cmd, f"empty toolsets must not inject flag: {cmd}"
    assert _parse_spawn_toolsets(cmd) is None


# ===========================================================================
# (c) TOOL_POLICY BRIDGE (action gate)
# ===========================================================================

def _board_with_blocked_tools(slug: str, blocked) -> None:
    kb.write_board_metadata(
        slug,
        runtime={"mode": "company", "tool_policy": {"blocked_tools": blocked}},
        workflow=_MIN_WORKFLOW,
    )


def _write_action_gate_config(home: Path, *, blocked_tools=None) -> None:
    """Write a minimal action_gate config.yaml so the gate is active."""
    import yaml

    rules = {}
    if blocked_tools is not None:
        rules["blocked_tools"] = blocked_tools
    cfg = {"action_gate": {"mode": "yolo", "rules": rules}}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def test_c_contract_blocked_tool_allowed_by_default(fresh_home, monkeypatch):
    """DEFAULT-OFF: a tool listed in the contract's blocked_tools (but NOT in
    config.yaml) is allowed -- only config.yaml rules apply today."""
    from agent import action_gate

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "agboard")
    _board_with_blocked_tools("agboard", ["send_message"])
    _write_action_gate_config(fresh_home, blocked_tools=[])
    # Default-off: contract blocked_tools must not bite.
    assert action_gate._contract_blocked_tools() == []
    verdict = action_gate.check_action_gate("send_message", {"platform": "imsg"})
    assert verdict is None, "default-off must allow the contract-blocked tool"


def test_c_contract_blocked_tool_blocked_when_flag_on(fresh_home, monkeypatch):
    """FLAG-ON: a tool in the contract's runtime.tool_policy.blocked_tools is
    hard-blocked by the action gate (even though config.yaml does not list it)."""
    from agent import action_gate

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "agboard")
    monkeypatch.setenv("HERMES_KANBAN_BRIDGE_TOOL_POLICY", "1")
    _board_with_blocked_tools("agboard", ["send_message"])
    _write_action_gate_config(fresh_home, blocked_tools=[])
    assert action_gate._contract_blocked_tools() == ["send_message"]
    verdict = action_gate.check_action_gate("send_message", {"platform": "imsg"})
    assert verdict is not None
    assert "ACTION BLOCKED" in verdict


def test_c_config_blocked_tool_still_blocked_when_flag_off(fresh_home, monkeypatch):
    """Regression guard: config.yaml blocked_tools keep blocking regardless of
    the bridge flag (the bridge is purely additive)."""
    from agent import action_gate

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "agboard")
    _board_with_blocked_tools("agboard", [])
    _write_action_gate_config(fresh_home, blocked_tools=["send_message"])
    verdict = action_gate.check_action_gate("send_message", {"platform": "imsg"})
    assert verdict is not None
    assert "ACTION BLOCKED" in verdict


def test_c_union_of_config_and_contract_when_flag_on(fresh_home, monkeypatch):
    """FLAG-ON: the blocked set is the UNION of config.yaml + contract rules."""
    from agent import action_gate

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "agboard")
    monkeypatch.setenv("HERMES_KANBAN_BRIDGE_TOOL_POLICY", "1")
    _board_with_blocked_tools("agboard", ["send_message"])
    _write_action_gate_config(fresh_home, blocked_tools=["execute_code"])
    # contract-only tool blocked
    assert action_gate.check_action_gate("send_message", {}) is not None
    # config-only tool still blocked
    assert action_gate.check_action_gate("execute_code", {}) is not None
    # an unrelated tool is not blocked (yolo mode passes 'check' tools through)
    assert action_gate.check_action_gate("delegate_task", {}) is None


def test_c_contract_read_failure_never_breaks_gate(fresh_home, monkeypatch):
    """Defensive: if the contract read raises, the bridge degrades to ``[]`` and
    the action gate behaves as if no contract blocked_tools exist."""
    from agent import action_gate

    monkeypatch.setenv("HERMES_KANBAN_BRIDGE_TOOL_POLICY", "1")

    def _boom(*_a, **_k):
        raise RuntimeError("metadata read exploded")

    monkeypatch.setattr(kb, "read_board_metadata", _boom)
    assert action_gate._contract_blocked_tools() == []
    _write_action_gate_config(fresh_home, blocked_tools=[])
    # No crash, no spurious block.
    assert action_gate.check_action_gate("send_message", {}) is None
