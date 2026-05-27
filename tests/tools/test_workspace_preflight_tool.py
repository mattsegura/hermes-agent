import json

from tools import workspace_preflight_tool as preflight


def test_parse_probe_output_extracts_git_and_process_sections():
    output = "\n".join([
        "__HERMES_PREFLIGHT_CWD__/repo",
        "__HERMES_PREFLIGHT_GIT_PRESENT__true",
        "__HERMES_PREFLIGHT_GIT_ROOT__/repo",
        "__HERMES_PREFLIGHT_GIT_BRANCH__main",
        "__HERMES_PREFLIGHT_GIT_HEAD__abc1234",
        "__HERMES_PREFLIGHT_BASE_PRESENT__true",
        "__HERMES_PREFLIGHT_STATUS_BEGIN__",
        "## main...origin/main",
        " M model_tools.py",
        "__HERMES_PREFLIGHT_STATUS_END__",
        "__HERMES_PREFLIGHT_PROCESS_BEGIN__0",
        "123 pytest",
        "__HERMES_PREFLIGHT_PROCESS_END__0",
    ])

    result = preflight._parse_probe_output(
        output,
        base_ref="origin/main",
        process_patterns=["pytest"],
        status_limit=80,
    )

    assert result["cwd"] == "/repo"
    assert result["git"] == {
        "inside_work_tree": True,
        "root": "/repo",
        "branch": "main",
        "head": "abc1234",
        "base_ref": "origin/main",
        "base_ref_present": True,
        "status": ["## main...origin/main", " M model_tools.py"],
        "status_limit": 80,
        "status_truncated": False,
    }
    assert result["processes"] == [{
        "pattern": "pytest",
        "matches": ["123 pytest"],
        "limit": preflight._MAX_PROCESS_LINES,
        "truncated": False,
    }]


def test_workspace_preflight_batches_probe_search_and_file_reads(monkeypatch):
    calls = {"terminal": [], "search": [], "read": []}

    def fake_terminal_tool(**kwargs):
        calls["terminal"].append(kwargs)
        return json.dumps({
            "output": "\n".join([
                "__HERMES_PREFLIGHT_CWD__/repo",
                "__HERMES_PREFLIGHT_GIT_PRESENT__false",
            ]),
            "exit_code": 0,
            "error": None,
        })

    def fake_search_tool(**kwargs):
        calls["search"].append(kwargs)
        return json.dumps({"matches": [{"file": "a.py", "line": 1}]})

    def fake_read_file_tool(**kwargs):
        calls["read"].append(kwargs)
        return json.dumps({"path": kwargs["path"], "content": "1|hello"})

    monkeypatch.setattr("tools.terminal_tool.terminal_tool", fake_terminal_tool)
    monkeypatch.setattr("tools.file_tools.search_tool", fake_search_tool)
    monkeypatch.setattr("tools.file_tools.read_file_tool", fake_read_file_tool)

    raw = preflight.workspace_preflight_tool(
        workdir="/repo",
        base_ref="main",
        process_patterns=["pytest"],
        searches=[{"pattern": "class Thing", "limit": 500}],
        files=[{"path": "a.py", "limit": 500}],
        task_id="task-1",
    )
    result = json.loads(raw)

    assert result["cwd"] == "/repo"
    assert result["git"]["inside_work_tree"] is False
    assert result["searches"][0]["request"]["limit"] == preflight._MAX_SEARCH_LIMIT
    assert result["files"][0]["request"]["limit"] == preflight._MAX_FILE_LINES
    assert result["searches"][0]["result"]["matches"][0]["file"] == "a.py"
    assert result["files"][0]["result"]["content"] == "1|hello"
    assert calls["terminal"][0]["workdir"] == "/repo"
    assert calls["terminal"][0]["task_id"] == "task-1"
    assert calls["search"][0]["task_id"] == "task-1"
    assert calls["read"][0]["task_id"] == "task-1"


def test_workspace_preflight_caps_optional_lists(monkeypatch):
    monkeypatch.setattr(
        "tools.terminal_tool.terminal_tool",
        lambda **_kwargs: json.dumps({
            "output": "__HERMES_PREFLIGHT_CWD__/repo\n__HERMES_PREFLIGHT_GIT_PRESENT__false",
            "exit_code": 0,
            "error": None,
        }),
    )
    search_calls = []
    read_calls = []
    monkeypatch.setattr(
        "tools.file_tools.search_tool",
        lambda **kwargs: search_calls.append(kwargs) or json.dumps({"matches": []}),
    )
    monkeypatch.setattr(
        "tools.file_tools.read_file_tool",
        lambda **kwargs: read_calls.append(kwargs) or json.dumps({"content": ""}),
    )

    raw = preflight.workspace_preflight_tool(
        process_patterns=[f"proc-{idx}" for idx in range(20)],
        searches=[{"pattern": f"needle-{idx}"} for idx in range(20)],
        files=[{"path": f"file-{idx}.py"} for idx in range(20)],
    )
    result = json.loads(raw)

    assert len(result["processes"]) == preflight._MAX_PROCESS_PATTERNS
    assert len(result["searches"]) == preflight._MAX_SEARCHES
    assert len(result["files"]) == preflight._MAX_FILES
    assert len(search_calls) == preflight._MAX_SEARCHES
    assert len(read_calls) == preflight._MAX_FILES
