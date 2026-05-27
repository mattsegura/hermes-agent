#!/usr/bin/env python3
"""Bundled read-only workspace preflight tool."""

from __future__ import annotations

import json
import logging
import shlex
from typing import Any

from tools.registry import registry

logger = logging.getLogger(__name__)


_MAX_STATUS_LINES = 120
_MAX_PROCESS_PATTERNS = 8
_MAX_PROCESS_LINES = 20
_MAX_SEARCHES = 8
_MAX_SEARCH_LIMIT = 50
_MAX_FILES = 12
_MAX_FILE_LINES = 300


WORKSPACE_PREFLIGHT_SCHEMA = {
    "name": "workspace_preflight",
    "description": (
        "Read-only workspace preflight that batches common repo probes into one call: "
        "cwd, git root/branch/head/status/base-ref presence, optional process pattern "
        "checks, optional bounded searches, and optional bounded file snippets. Use this "
        "early instead of separate pwd/git status/search/read probes; still run targeted "
        "verification and tests before claiming completion."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workdir": {
                "type": "string",
                "description": "Optional working directory for the preflight command.",
            },
            "base_ref": {
                "type": "string",
                "description": "Git ref to check for base-commit presence, e.g. origin/main.",
                "default": "origin/main",
            },
            "status_limit": {
                "type": "integer",
                "description": f"Maximum git status lines to return (max {_MAX_STATUS_LINES}).",
                "default": 80,
            },
            "process_patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional process patterns to check with pgrep -fl. "
                    f"At most {_MAX_PROCESS_PATTERNS}; each returns at most {_MAX_PROCESS_LINES} lines."
                ),
            },
            "searches": {
                "type": "array",
                "description": (
                    "Optional bounded search_files calls. "
                    f"At most {_MAX_SEARCHES}; per-search limit is capped at {_MAX_SEARCH_LIMIT}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "target": {"type": "string", "enum": ["content", "files"], "default": "content"},
                        "path": {"type": "string", "default": "."},
                        "file_glob": {"type": "string"},
                        "limit": {"type": "integer", "default": 25},
                        "offset": {"type": "integer", "default": 0},
                        "output_mode": {
                            "type": "string",
                            "enum": ["content", "files_only", "count"],
                            "default": "content",
                        },
                        "context": {"type": "integer", "default": 0},
                    },
                    "required": ["pattern"],
                },
            },
            "files": {
                "type": "array",
                "description": (
                    "Optional bounded read_file snippets. "
                    f"At most {_MAX_FILES}; per-file limit is capped at {_MAX_FILE_LINES} lines."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "offset": {"type": "integer", "default": 1},
                        "limit": {"type": "integer", "default": 120},
                    },
                    "required": ["path"],
                },
            },
        },
        "required": [],
    },
}


def _coerce_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _bounded_strings(values: Any, *, limit: int, max_len: int = 200) -> list[str]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values[:limit]:
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if stripped:
            result.append(stripped[:max_len])
    return result


def _build_probe_command(base_ref: str, status_limit: int, process_patterns: list[str]) -> str:
    quoted_base = shlex.quote(base_ref)
    lines = [
        "set +e",
        'printf "__HERMES_PREFLIGHT_CWD__%s\\n" "$(pwd)"',
        "if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then",
        '  printf "__HERMES_PREFLIGHT_GIT_PRESENT__true\\n"',
        '  printf "__HERMES_PREFLIGHT_GIT_ROOT__%s\\n" "$(git rev-parse --show-toplevel 2>/dev/null)"',
        '  printf "__HERMES_PREFLIGHT_GIT_BRANCH__%s\\n" "$(git symbolic-ref --short HEAD 2>/dev/null)"',
        '  printf "__HERMES_PREFLIGHT_GIT_HEAD__%s\\n" "$(git rev-parse --short HEAD 2>/dev/null)"',
        f"  if git rev-parse --verify --quiet {quoted_base}^{{commit}} >/dev/null 2>&1; then",
        '    printf "__HERMES_PREFLIGHT_BASE_PRESENT__true\\n"',
        "  else",
        '    printf "__HERMES_PREFLIGHT_BASE_PRESENT__false\\n"',
        "  fi",
        '  printf "__HERMES_PREFLIGHT_STATUS_BEGIN__\\n"',
        f"  git status --short --branch --untracked-files=all 2>&1 | sed -n '1,{status_limit}p'",
        '  printf "__HERMES_PREFLIGHT_STATUS_END__\\n"',
        "else",
        '  printf "__HERMES_PREFLIGHT_GIT_PRESENT__false\\n"',
        "fi",
    ]

    for idx, pattern in enumerate(process_patterns):
        quoted_pattern = shlex.quote(pattern)
        lines.extend([
            f'printf "__HERMES_PREFLIGHT_PROCESS_BEGIN__{idx}\\n"',
            f"(pgrep -fl {quoted_pattern} 2>/dev/null || true) | sed -n '1,{_MAX_PROCESS_LINES}p'",
            f'printf "__HERMES_PREFLIGHT_PROCESS_END__{idx}\\n"',
        ])

    return "\n".join(lines)


def _parse_probe_output(output: str, *, base_ref: str, process_patterns: list[str], status_limit: int) -> dict[str, Any]:
    data: dict[str, Any] = {
        "cwd": "",
        "git": {
            "inside_work_tree": False,
            "root": "",
            "branch": "",
            "head": "",
            "base_ref": base_ref,
            "base_ref_present": False,
            "status": [],
            "status_limit": status_limit,
        },
        "processes": [
            {"pattern": pattern, "matches": [], "limit": _MAX_PROCESS_LINES}
            for pattern in process_patterns
        ],
    }

    section: tuple[str, int | None] | None = None
    for raw_line in output.splitlines():
        line = raw_line.rstrip("\n")
        if section == ("status", None):
            if line == "__HERMES_PREFLIGHT_STATUS_END__":
                section = None
            else:
                data["git"]["status"].append(line)
            continue
        if section and section[0] == "process":
            idx = section[1]
            if line == f"__HERMES_PREFLIGHT_PROCESS_END__{idx}":
                section = None
            elif isinstance(idx, int) and 0 <= idx < len(data["processes"]):
                data["processes"][idx]["matches"].append(line)
            continue

        if line.startswith("__HERMES_PREFLIGHT_CWD__"):
            data["cwd"] = line.removeprefix("__HERMES_PREFLIGHT_CWD__")
            continue
        if line.startswith("__HERMES_PREFLIGHT_GIT_PRESENT__"):
            data["git"]["inside_work_tree"] = line.endswith("true")
            continue
        if line.startswith("__HERMES_PREFLIGHT_GIT_ROOT__"):
            data["git"]["root"] = line.removeprefix("__HERMES_PREFLIGHT_GIT_ROOT__")
            continue
        if line.startswith("__HERMES_PREFLIGHT_GIT_BRANCH__"):
            data["git"]["branch"] = line.removeprefix("__HERMES_PREFLIGHT_GIT_BRANCH__")
            continue
        if line.startswith("__HERMES_PREFLIGHT_GIT_HEAD__"):
            data["git"]["head"] = line.removeprefix("__HERMES_PREFLIGHT_GIT_HEAD__")
            continue
        if line.startswith("__HERMES_PREFLIGHT_BASE_PRESENT__"):
            data["git"]["base_ref_present"] = line.endswith("true")
            continue
        if line == "__HERMES_PREFLIGHT_STATUS_BEGIN__":
            section = ("status", None)
            continue
        if line.startswith("__HERMES_PREFLIGHT_PROCESS_BEGIN__"):
            try:
                section = ("process", int(line.removeprefix("__HERMES_PREFLIGHT_PROCESS_BEGIN__")))
            except ValueError:
                section = None
            continue

    data["git"]["status_truncated"] = len(data["git"]["status"]) >= status_limit
    for process in data["processes"]:
        process["truncated"] = len(process["matches"]) >= _MAX_PROCESS_LINES
    return data


def _parse_tool_json(result: str) -> Any:
    try:
        return json.loads(result)
    except Exception:
        return {"raw": result}


def _run_searches(searches: Any, *, task_id: str) -> list[dict[str, Any]]:
    if not isinstance(searches, list):
        return []

    from tools.file_tools import search_tool

    results = []
    for raw in searches[:_MAX_SEARCHES]:
        if not isinstance(raw, dict) or not raw.get("pattern"):
            continue
        limit = _coerce_int(raw.get("limit", 25), 25, minimum=1, maximum=_MAX_SEARCH_LIMIT)
        offset = _coerce_int(raw.get("offset", 0), 0, minimum=0, maximum=10_000)
        context = _coerce_int(raw.get("context", 0), 0, minimum=0, maximum=5)
        target = raw.get("target", "content")
        if target not in {"content", "files"}:
            target = "content"
        output_mode = raw.get("output_mode", "content")
        if output_mode not in {"content", "files_only", "count"}:
            output_mode = "content"
        result = search_tool(
            pattern=str(raw["pattern"]),
            target=target,
            path=str(raw.get("path") or "."),
            file_glob=raw.get("file_glob"),
            limit=limit,
            offset=offset,
            output_mode=output_mode,
            context=context,
            task_id=task_id,
        )
        results.append({
            "request": {
                "pattern": str(raw["pattern"]),
                "target": target,
                "path": str(raw.get("path") or "."),
                "file_glob": raw.get("file_glob"),
                "limit": limit,
                "offset": offset,
                "output_mode": output_mode,
                "context": context,
            },
            "result": _parse_tool_json(result),
        })
    return results


def _run_file_reads(files: Any, *, task_id: str) -> list[dict[str, Any]]:
    if not isinstance(files, list):
        return []

    from tools.file_tools import read_file_tool

    results = []
    for raw in files[:_MAX_FILES]:
        if not isinstance(raw, dict) or not raw.get("path"):
            continue
        offset = _coerce_int(raw.get("offset", 1), 1, minimum=1, maximum=1_000_000)
        limit = _coerce_int(raw.get("limit", 120), 120, minimum=1, maximum=_MAX_FILE_LINES)
        path = str(raw["path"])
        result = read_file_tool(path=path, offset=offset, limit=limit, task_id=task_id)
        results.append({
            "request": {"path": path, "offset": offset, "limit": limit},
            "result": _parse_tool_json(result),
        })
    return results


def workspace_preflight_tool(
    *,
    workdir: str | None = None,
    base_ref: str = "origin/main",
    status_limit: int = 80,
    process_patterns: Any = None,
    searches: Any = None,
    files: Any = None,
    task_id: str = "default",
) -> str:
    """Gather bounded, read-only workspace state in one tool call."""
    status_limit = _coerce_int(status_limit, 80, minimum=1, maximum=_MAX_STATUS_LINES)
    base_ref = (base_ref or "origin/main").strip()[:200]
    process_patterns_list = _bounded_strings(
        process_patterns,
        limit=_MAX_PROCESS_PATTERNS,
        max_len=200,
    )

    command = _build_probe_command(base_ref, status_limit, process_patterns_list)
    try:
        from tools.terminal_tool import terminal_tool

        raw_probe = terminal_tool(
            command=command,
            background=False,
            timeout=20,
            task_id=task_id,
            workdir=workdir,
        )
        probe_result = json.loads(raw_probe)
    except Exception as exc:
        logger.debug("workspace_preflight terminal probe failed", exc_info=True)
        return json.dumps({
            "error": f"workspace_preflight probe failed: {type(exc).__name__}: {exc}",
        }, ensure_ascii=False)

    output = probe_result.get("output") or ""
    result = _parse_probe_output(
        output,
        base_ref=base_ref,
        process_patterns=process_patterns_list,
        status_limit=status_limit,
    )
    result["probe"] = {
        "exit_code": probe_result.get("exit_code"),
        "error": probe_result.get("error"),
        "workdir": workdir,
    }
    result["searches"] = _run_searches(searches, task_id=task_id)
    result["files"] = _run_file_reads(files, task_id=task_id)
    result["_hint"] = (
        "workspace_preflight is a read-only batch helper. Use its repo/search/read "
        "snapshot to avoid duplicate preflight probes, then run focused diffs, tests, "
        "and any required live verification before finalizing."
    )
    return json.dumps(result, ensure_ascii=False)


def _handle_workspace_preflight(args: dict[str, Any], **kw) -> str:
    return workspace_preflight_tool(
        workdir=args.get("workdir"),
        base_ref=args.get("base_ref", "origin/main"),
        status_limit=args.get("status_limit", 80),
        process_patterns=args.get("process_patterns"),
        searches=args.get("searches"),
        files=args.get("files"),
        task_id=kw.get("task_id") or "default",
    )


registry.register(
    name="workspace_preflight",
    toolset="file",
    schema=WORKSPACE_PREFLIGHT_SCHEMA,
    handler=_handle_workspace_preflight,
    emoji="🧭",
    max_result_size_chars=100_000,
)
