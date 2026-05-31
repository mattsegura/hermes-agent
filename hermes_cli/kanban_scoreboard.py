"""Canonical read-only renderers for `/kanban status` and `/kanban scoreboard`.

This is the engine-internal home of the operability views that previously
lived only as the external read-only scripts
``~/.hermes/scripts/kanban-status.py`` and ``kanban-scoreboard.py``. Hoisting
the render logic into an importable engine module lets ``hermes_cli.kanban``
wire ``status`` / ``scoreboard`` subcommands — and, because the gateway pipes
``/kanban`` through :func:`hermes_cli.kanban.run_slash`, that makes both views
reachable from Telegram for free (a CLI subcommand == a Telegram command).

DESIGN / SAFETY (mirrors the external scripts EXACTLY):
  * Strictly READ-ONLY. The dispatch GATE / metadata / objective grading come
    from the engine's own read-only calls (``board_dispatch_gate``,
    ``read_board_metadata``, ``list_boards``); raw task counts are read with a
    ``file:...?mode=ro`` sqlite URI under a SHARED, non-blocking advisory lock
    on the board's ``.kanban.lock`` that BAILS on contention rather than
    waiting. Never sets journal_mode. Never writes any DB / board.json.
  * There are LIVE gateways running; this module must never disturb them. Every
    per-board engine call is wrapped in try/except so one bad board never
    breaks the whole view.

PHONE FORMAT: the status renderer is compact — one line per board with a health
glyph, a top summary line, and the governance posture line — and self-truncates
to stay under the gateway's 3800-char message cap.
"""
from __future__ import annotations

import fcntl
import os
import sqlite3
from typing import Any, Optional

from hermes_cli import kanban_db as kb

# Gateway truncation cap (mirrors gateway/run.py:_handle_kanban_command). The
# rendered status text must fit under this so it is never silently chopped
# mid-line by the gateway.
GATEWAY_MESSAGE_CAP = 3800

# Health glyphs (ASCII-only, GBK-safe — no emoji).
_GLYPH_ACTIVE_OK = "[OK]"
_GLYPH_ACTIVE_BLOCKED = "[!!]"
_GLYPH_CONTRACT_REVIEW = "[..]"
_GLYPH_UNMANAGED = "[--]"
_GLYPH_UNKNOWN = "[??]"


# ---------------------------------------------------------------------------
# Read-only task-status counts (shared-lock-bail pattern from the scripts)
# ---------------------------------------------------------------------------
def _read_task_counts(slug: str) -> tuple[Optional[dict], Optional[str]]:
    """SELECT status, COUNT(*) FROM tasks, strictly read-only.

    Returns (counts_dict, error_str). Takes only a shared non-blocking lock on
    the board's ``.kanban.lock`` if present and bails (returns error) on
    contention rather than waiting.
    """
    try:
        db_path = kb.kanban_db_path(board=slug)
    except Exception as exc:  # noqa: BLE001
        return None, f"db path error: {exc}"
    if not db_path.exists():
        return None, "kanban.db not found"
    lock_path = db_path.parent / ".kanban.lock"
    fd = None
    try:
        if lock_path.exists():
            fd = os.open(str(lock_path), os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                os.close(fd)
                return None, "board busy (shared lock); skipped"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM tasks GROUP BY status"
            ).fetchall()
        finally:
            conn.close()
        return {str(s): int(n) for (s, n) in rows}, None
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return None, "db busy/contended; skipped"
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 -- surface, never write
        return None, str(exc)
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Engine gate (read-only) — defensive per board
# ---------------------------------------------------------------------------
def _gate_summary(slug: str) -> dict[str, Any]:
    """Call ``board_dispatch_gate(slug)`` defensively; never raises."""
    empty = {
        "managed": None,
        "launch_phase": None,
        "ok": None,
        "blocker_codes": [],
        "reason": None,
        "error": None,
    }
    try:
        gate = kb.board_dispatch_gate(slug)
    except Exception as exc:  # noqa: BLE001 -- one bad board never breaks the view
        empty["error"] = f"gate error: {type(exc).__name__}: {exc}"
        return empty
    if not isinstance(gate, dict):
        empty["error"] = "gate returned non-dict"
        return empty
    codes: list[str] = []
    for blocker in gate.get("blockers") or []:
        if isinstance(blocker, dict) and blocker.get("code"):
            codes.append(str(blocker["code"]))
        elif isinstance(blocker, str):
            codes.append(blocker)
    return {
        "managed": gate.get("managed"),
        "launch_phase": gate.get("launch_phase"),
        "ok": gate.get("ok"),
        "blocker_codes": codes,
        "reason": gate.get("reason"),
        "error": None,
    }


def _governance_posture() -> dict[str, Any]:
    """Read the system-wide enforcement stance (read-only engine helpers)."""
    posture: dict[str, Any] = {
        "enforced_completeness_dimensions": None,
        "require_contract_defaults": None,
        "enforce_worker_toolsets": None,
        "bridge_tool_policy": None,
    }
    try:
        dims = kb._launch_completeness_enforced_dimensions()
        posture["enforced_completeness_dimensions"] = sorted(dims)
    except Exception:  # noqa: BLE001
        pass
    for label, fn in (
        ("require_contract_defaults", "_require_contract_defaults_enabled"),
        ("enforce_worker_toolsets", "_enforce_worker_toolsets_enabled"),
        ("bridge_tool_policy", "_bridge_tool_policy_enabled"),
    ):
        try:
            posture[label] = bool(getattr(kb, fn)())
        except Exception:  # noqa: BLE001
            pass
    return posture


def _yn(value: Any) -> str:
    if value is True:
        return "Y"
    if value is False:
        return "N"
    return "?"


def _health_glyph(row: dict[str, Any]) -> str:
    """Pick the compact health glyph for one board row."""
    if row["active_but_blocked"]:
        return _GLYPH_ACTIVE_BLOCKED
    phase = (row.get("launch_phase") or "").lower()
    if phase == "active" and row.get("gate_ok") is True:
        return _GLYPH_ACTIVE_OK
    if phase in {"contract_review", "draft"}:
        return _GLYPH_CONTRACT_REVIEW
    if row.get("managed") is False:
        return _GLYPH_UNMANAGED
    if row.get("gate_ok") is None:
        return _GLYPH_UNKNOWN
    if phase == "active" and row.get("gate_ok") is True:
        return _GLYPH_ACTIVE_OK
    return _GLYPH_UNMANAGED


# ---------------------------------------------------------------------------
# Status (all-boards) assembly + render
# ---------------------------------------------------------------------------
def assemble_status_rows(
    boards: Optional[list[str]] = None,
    *,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Assemble per-board status rows (read-only).

    ``boards`` is an explicit slug list; when omitted, the engine's canonical
    :func:`kanban_db.list_boards` discovery is used (default + non-archived).
    """
    if boards is None:
        try:
            metas = kb.list_boards(include_archived=include_archived)
            slugs = [str(m.get("slug")) for m in metas if m.get("slug")]
        except Exception:  # noqa: BLE001
            slugs = [kb.DEFAULT_BOARD]
    else:
        slugs = list(boards)

    rows: list[dict[str, Any]] = []
    for slug in slugs:
        gate = _gate_summary(slug)
        phase = gate["launch_phase"]
        if phase is None:
            try:
                phase = kb.normalize_board_launch_phase(
                    kb.read_board_metadata(slug).get("launch_phase"),
                    default="active",
                )
            except Exception:  # noqa: BLE001
                phase = None
        counts, count_err = _read_task_counts(slug)
        counts = counts or {}
        row = {
            "slug": slug,
            "managed": gate["managed"],
            "launch_phase": phase,
            "gate_ok": gate["ok"],
            "blocker_codes": gate["blocker_codes"],
            "reason": gate["reason"],
            "ready": int(counts.get("ready", 0)),
            "running": int(counts.get("running", 0)),
            "blocked": int(counts.get("blocked", 0)),
            "done": int(counts.get("done", 0)),
            "total": int(sum(counts.values())),
            "active_but_blocked": (phase == "active" and gate["ok"] is False),
            "errors": [e for e in (gate["error"], count_err) if e],
        }
        rows.append(row)
    return rows


def render_status(
    rows: list[dict[str, Any]],
    *,
    governance: Optional[dict[str, Any]] = None,
    cap: int = GATEWAY_MESSAGE_CAP,
) -> str:
    """Render the compact, phone-friendly all-boards status dashboard.

    Layout:
      * a top summary line (N boards, X blocked, active-but-blocked slugs),
      * the system-wide governance posture line,
      * one compact line per board with a health glyph.

    Self-truncates to stay under ``cap`` chars: if the board list is long it
    drops the lowest-priority lines and appends a truncation marker, so the
    gateway never chops the text mid-line.
    """
    if governance is None:
        governance = _governance_posture()

    total = len(rows)
    blocked = [r for r in rows if r["gate_ok"] is False]
    abb = [r for r in rows if r["active_but_blocked"]]

    summary = f"KANBAN STATUS: {total} board(s), {len(blocked)} blocked"
    if abb:
        summary += "; active-but-blocked: " + ", ".join(r["slug"] for r in abb)

    dims = governance.get("enforced_completeness_dimensions")
    if dims is None:
        dims_str = "?"
    elif not dims:
        dims_str = "none"
    else:
        dims_str = ",".join(dims)
    posture = (
        "governance: completeness=" + dims_str
        + f" contract_defaults={_yn(governance.get('require_contract_defaults'))}"
        + f" worker_toolsets={_yn(governance.get('enforce_worker_toolsets'))}"
        + f" bridge_policy={_yn(governance.get('bridge_tool_policy'))}"
    )

    header = [summary, posture, ""]

    board_lines: list[str] = []
    for r in rows:
        glyph = _health_glyph(r)
        phase = r.get("launch_phase") or "?"
        line = (
            f"{glyph} {r['slug']}  phase={phase}"
            f"  rdy={r['ready']} run={r['running']} blk={r['blocked']}"
            f" done={r['done']}/{r['total']}"
        )
        if r["active_but_blocked"] or r["gate_ok"] is False:
            codes = ",".join(r["blocker_codes"]) or "blocked"
            line += f"  BLOCKED[{codes}]"
        if r["active_but_blocked"]:
            line += "  <ACTIVE-BUT-BLOCKED>"
        board_lines.append(line)
        for err in r["errors"]:
            board_lines.append(f"   ! {err}")

    footer = ["", "read-only view (engine gate + RO sqlite; never writes)."]

    def _join(lines: list[str]) -> str:
        return "\n".join(lines)

    full = _join(header + board_lines + footer)
    if len(full) <= cap:
        return full

    # Too long: keep the header (summary + posture) and as many board lines as
    # fit, then append a truncation marker. The header is the highest-signal
    # content (it already names the blocked + active-but-blocked boards).
    trunc_marker = "... (truncated; run on desktop for the full table)"
    kept: list[str] = []
    budget = cap - len(_join(header)) - len(trunc_marker) - 2
    used = 0
    for line in board_lines:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    return _join(header + kept + [trunc_marker])


# ---------------------------------------------------------------------------
# Scoreboard (attainment) assembly + render
# ---------------------------------------------------------------------------
def _objective_for_board(slug: str) -> dict[str, Any]:
    """Read the board's declared objective (read-only via engine metadata)."""
    try:
        meta = kb.read_board_metadata(slug)
    except Exception:  # noqa: BLE001
        return {"statement": None, "success": [], "failure": [], "constraints": []}
    obj = meta.get("objective") if isinstance(meta.get("objective"), dict) else {}
    # Managed boards keep the canonical contract under business_contract.
    bc = meta.get("business_contract")
    if (not obj or not obj.get("success")) and isinstance(bc, dict):
        bc_obj = bc.get("objective") if isinstance(bc.get("objective"), dict) else {}
        if bc_obj:
            obj = bc_obj
    return {
        "statement": (obj or {}).get("statement"),
        "success": list((obj or {}).get("success") or []),
        "failure": list((obj or {}).get("failure") or []),
        "constraints": list((obj or {}).get("constraints") or []),
    }


def _read_completed_count(slug: str) -> tuple[int, Optional[str]]:
    """Read the proof-gated 'completed' task_events count (read-only)."""
    try:
        db_path = kb.kanban_db_path(board=slug)
    except Exception as exc:  # noqa: BLE001
        return 0, f"db path error: {exc}"
    if not db_path.exists():
        return 0, "kanban.db not found"
    lock_path = db_path.parent / ".kanban.lock"
    fd = None
    try:
        if lock_path.exists():
            fd = os.open(str(lock_path), os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                os.close(fd)
                return 0, "board busy (shared lock); skipped"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE kind = 'completed'"
            ).fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0, None
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return 0, "db busy/contended; skipped"
        return 0, str(exc)
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


# Tokens that mark a success data_source as EXTERNAL (cannot be resolved
# internally) — mirrors the external scoreboard script.
_EXTERNAL_TOKENS = (
    "stripe", "mixpanel", "amplitude", "app store", "appstore", "play store",
    "google analytics", "ga4", "posthog", "beehiiv", "mailchimp", "sendgrid",
    "native analytics", "instagram", "tiktok", "youtube", "shopify",
    "quickbooks", "plaid", "segment", "salesforce", "hubspot", "webhook",
    "external", "api", "manual", "survey", "spreadsheet", "sheet",
)


def _is_typed_success(entry: Any) -> bool:
    return isinstance(entry, dict) and bool(str(entry.get("metric") or "").strip())


def score_board(slug: str) -> dict[str, Any]:
    """Grade a board's realized outcome vs its declared objective.success.

    Read-only. Typed success entries with an EXTERNAL data source are reported
    PENDING (never guessed); the proof-gated 'completed' count is surfaced as
    the honest internal proxy for prose-only boards.
    """
    objective = _objective_for_board(slug)
    completed, comp_err = _read_completed_count(slug)
    success = objective["success"]
    typed = [s for s in success if _is_typed_success(s)]
    prose = [s for s in success if not _is_typed_success(s)]
    pending_external = 0
    for entry in typed:
        ds = str(entry.get("data_source") or "").lower()
        if any(tok in ds for tok in _EXTERNAL_TOKENS):
            pending_external += 1
    return {
        "board": slug,
        "read_only": True,
        "objective_statement": objective["statement"],
        "success_count": len(success),
        "typed_count": len(typed),
        "prose_count": len(prose),
        "pending_external": pending_external,
        "completed_proof_gated": completed,
        "errors": [e for e in (comp_err,) if e],
    }


def render_scoreboard(
    result: dict[str, Any],
    *,
    cap: int = GATEWAY_MESSAGE_CAP,
) -> str:
    """Render a compact attainment summary for one board."""
    lines = [f"KANBAN SCOREBOARD: {result['board']}"]
    stmt = result.get("objective_statement")
    if stmt:
        if len(stmt) > 140:
            stmt = stmt[:137] + "..."
        lines.append(f"objective: {stmt}")
    lines.append(
        f"success criteria: {result['success_count']}"
        f" ({result['typed_count']} typed, {result['prose_count']} prose)"
    )
    lines.append(
        f"proof-gated completed: {result['completed_proof_gated']}"
    )
    if result["pending_external"]:
        lines.append(
            f"PENDING (external data source): {result['pending_external']}"
            " typed criterion/criteria — resolved off-platform, never guessed."
        )
    for err in result["errors"]:
        lines.append(f"   ! {err}")
    lines.append(
        "read-only; 'completed' is proof-gated by construction; external data"
        " sources are PENDING, never faked."
    )
    out = "\n".join(lines)
    if len(out) > cap:
        out = out[: cap - 12].rstrip() + "\n... (trunc)"
    return out
