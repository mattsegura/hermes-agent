import json

import model_tools


def _dispatches_as(payload):
    def _dispatch(_name, _args, **_kwargs):
        return json.dumps(payload)

    return _dispatch


def test_exact_duplicate_read_file_gets_advisory(monkeypatch):
    from tools.registry import registry

    model_tools._reset_duplicate_tool_advisory_state()
    monkeypatch.setattr(registry, "dispatch", _dispatches_as({"content": "1|x"}))

    first = model_tools.handle_function_call(
        "read_file",
        {"path": "a.py", "offset": 1, "limit": 50},
        task_id="task-dup",
        session_id="session-dup",
        skip_pre_tool_call_hook=True,
    )
    second = model_tools.handle_function_call(
        "read_file",
        {"path": "a.py", "offset": 1, "limit": 50},
        task_id="task-dup",
        session_id="session-dup",
        skip_pre_tool_call_hook=True,
    )

    assert "_advisories" not in json.loads(first)
    advisory = json.loads(second)["_advisories"][0]
    assert "Duplicate read-only probe" in advisory
    assert "workspace_preflight" in advisory


def test_duplicate_advisory_is_scoped_by_session(monkeypatch):
    from tools.registry import registry

    model_tools._reset_duplicate_tool_advisory_state()
    monkeypatch.setattr(registry, "dispatch", _dispatches_as({"matches": []}))
    args = {"pattern": "needle", "path": "."}

    model_tools.handle_function_call(
        "search_files",
        args,
        task_id="task",
        session_id="session-a",
        skip_pre_tool_call_hook=True,
    )
    other_session = model_tools.handle_function_call(
        "search_files",
        args,
        task_id="task",
        session_id="session-b",
        skip_pre_tool_call_hook=True,
    )

    assert "_advisories" not in json.loads(other_session)


def test_blocked_call_does_not_seed_duplicate_advisory(monkeypatch):
    from tools.registry import registry

    model_tools._reset_duplicate_tool_advisory_state()
    args = {"path": "blocked.py"}

    monkeypatch.setattr(registry, "dispatch", _dispatches_as({"content": "ok"}))

    def block_once(tool_name, tool_args, **kwargs):
        assert tool_name == "read_file"
        assert tool_args == args
        return "Blocked"

    monkeypatch.setattr(
        "hermes_cli.plugins.get_pre_tool_call_block_message",
        block_once,
    )

    blocked = model_tools.handle_function_call(
        "read_file",
        args,
        task_id="task-blocked",
    )
    assert json.loads(blocked) == {"error": "Blocked"}

    monkeypatch.setattr(
        "hermes_cli.plugins.get_pre_tool_call_block_message",
        lambda *_args, **_kwargs: None,
    )

    allowed = model_tools.handle_function_call(
        "read_file",
        args,
        task_id="task-blocked",
    )
    assert json.loads(allowed) == {"content": "ok"}


def test_duplicate_advisory_requires_task_or_session_scope(monkeypatch):
    from tools.registry import registry

    model_tools._reset_duplicate_tool_advisory_state()
    monkeypatch.setattr(registry, "dispatch", _dispatches_as({"content": "ok"}))
    args = {"path": "unscoped.py"}

    first = model_tools.handle_function_call(
        "read_file",
        args,
        skip_pre_tool_call_hook=True,
    )
    second = model_tools.handle_function_call(
        "read_file",
        args,
        skip_pre_tool_call_hook=True,
    )

    assert json.loads(first) == {"content": "ok"}
    assert json.loads(second) == {"content": "ok"}


def test_terminal_status_probe_duplicate_gets_advisory(monkeypatch):
    from tools.registry import registry

    model_tools._reset_duplicate_tool_advisory_state()
    monkeypatch.setattr(registry, "dispatch", _dispatches_as({"output": "## main"}))

    args = {"command": "git status --short --branch"}
    model_tools.handle_function_call(
        "terminal",
        args,
        task_id="task-terminal",
        session_id="session-terminal",
        skip_pre_tool_call_hook=True,
    )
    second = model_tools.handle_function_call(
        "terminal",
        args,
        task_id="task-terminal",
        session_id="session-terminal",
        skip_pre_tool_call_hook=True,
    )

    assert "Duplicate read-only probe" in json.loads(second)["_advisories"][0]


def test_terminal_mutation_duplicate_is_not_advised(monkeypatch):
    from tools.registry import registry

    model_tools._reset_duplicate_tool_advisory_state()
    monkeypatch.setattr(registry, "dispatch", _dispatches_as({"output": ""}))

    args = {"command": "touch created.txt"}
    model_tools.handle_function_call(
        "terminal",
        args,
        task_id="task-mutation",
        session_id="session-mutation",
        skip_pre_tool_call_hook=True,
    )
    second = model_tools.handle_function_call(
        "terminal",
        args,
        task_id="task-mutation",
        session_id="session-mutation",
        skip_pre_tool_call_hook=True,
    )

    assert "_advisories" not in json.loads(second)
