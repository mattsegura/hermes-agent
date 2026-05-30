"""Kanban tools — structured tool-call surface for worker + orchestrator agents.

Task execution tools are registered into the model's schema when the agent is
running under the dispatcher (env var ``HERMES_KANBAN_TASK`` set) or when
the active profile explicitly enables the full ``kanban`` toolset for
orchestrator work. Launch-intake tools live in a narrower
``kanban_launch_intake`` toolset so owner-facing profiles can review a
business launch contract without seeing task creation/execution tools.

Why tools instead of just shelling out to ``hermes kanban``?

1. **Backend portability.** A worker whose terminal tool points at Docker
   / Modal / Singularity / SSH would run ``hermes kanban complete …``
   inside the container, where ``hermes`` isn't installed and the DB
   isn't mounted. Tools run in the agent's Python process, so they
   always reach ``~/.hermes/kanban.db`` regardless of terminal backend.

2. **No shell-quoting footguns.** Passing ``--metadata '{"x": [...]}'``
   through shlex+argparse is fragile. Structured tool args skip it.

3. **Better errors.** Tool-call failures return structured JSON the
   model can reason about, not stderr strings it has to parse.

Humans continue to use the CLI (``hermes kanban …``), the dashboard
(``hermes dashboard``), and the slash command (``/kanban …``) — all
three bypass the agent entirely. The tools are for dispatcher-spawned
worker handoffs and for configured orchestrator profiles that route work
through the board.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

KANBAN_LIST_DEFAULT_LIMIT = 50
KANBAN_LIST_MAX_LIMIT = 200

KANBAN_FULL_TOOLSET = "kanban"
KANBAN_LAUNCH_INTAKE_TOOLSET = "kanban_launch_intake"
KANBAN_OWNER_LAUNCH_INTAKE_PROFILES = {"default", "personal-assistant"}


def _configured_toolsets() -> set[str]:
    """Return toolsets configured anywhere in the active profile config."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception:
        return set()

    names: set[str] = set()

    def _add(raw: Any) -> None:
        if isinstance(raw, str):
            values = raw.split(",")
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            return
        for value in values:
            text = str(value).strip()
            if text:
                names.add(text)

    _add(cfg.get("toolsets", []))
    platform_toolsets = cfg.get("platform_toolsets") or {}
    if isinstance(platform_toolsets, dict):
        for value in platform_toolsets.values():
            _add(value)

    return names


def _env_profile_name() -> str:
    for key in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _active_profile_name() -> str:
    env_profile = _env_profile_name()
    if env_profile:
        return env_profile
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "default"
    except Exception:
        return "default"


def _is_owner_launch_intake_profile() -> bool:
    return _active_profile_name() in KANBAN_OWNER_LAUNCH_INTAKE_PROFILES


def _profile_has_toolset(*toolsets: str) -> bool:
    configured = _configured_toolsets()
    return any(toolset in configured for toolset in toolsets)


def _profile_has_kanban_toolset() -> bool:
    return _profile_has_toolset(KANBAN_FULL_TOOLSET)


def _profile_has_launch_intake_toolset() -> bool:
    if _profile_has_toolset(KANBAN_LAUNCH_INTAKE_TOOLSET, KANBAN_FULL_TOOLSET):
        return True
    return (
        _active_profile_name() in KANBAN_OWNER_LAUNCH_INTAKE_PROFILES
        and _profile_has_toolset("hermes-cli")
    )


def _check_kanban_mode() -> bool:
    """Task-lifecycle tools are available when:

    1. ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), OR
    2. The current profile has ``kanban`` in its toolsets config
       (orchestrator profiles like techlead that route work via Kanban).

    Owner intake profiles without the full kanban toolset only see the
    launch-review tools. Workers spawned by the kanban dispatcher (gateway-
    embedded by default) and non-owner orchestrator profiles with the kanban
    toolset enabled see the Kanban lifecycle tool surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    if _is_owner_launch_intake_profile():
        return False
    return _profile_has_kanban_toolset()


def _check_kanban_orchestrator_mode() -> bool:
    """Board-routing tools (kanban_list, kanban_unblock) are intentionally
    hidden from task workers.

    Dispatcher-spawned workers should close their own task via the
    lifecycle tools (complete/block/heartbeat), not enumerate or unblock
    board state. Profiles that explicitly opt into the kanban toolset
    and are NOT scoped to a single task are the orchestrator surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    if _is_owner_launch_intake_profile():
        return False
    return _profile_has_kanban_toolset()


def _check_kanban_launch_intake_mode() -> bool:
    """Launch-intake tools are read/review/amend-only.

    They are never exposed to dispatcher-spawned task workers. Owner-facing
    default/personal-assistant sessions may receive them as a narrow intake
    surface, and full Kanban orchestrators keep them for contract review.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    return _profile_has_launch_intake_toolset()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _default_task_id(arg: Optional[str]) -> Optional[str]:
    """Resolve ``task_id`` arg or fall back to the env var the dispatcher set."""
    if arg:
        return arg
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    return env_tid or None


def _worker_run_id(task_id: str) -> Optional[int]:
    """Return this worker's dispatcher run id when it is scoped to task_id."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _stamp_worker_session_metadata(
    task_id: str, metadata: Optional[dict]
) -> Optional[dict]:
    """Add trusted worker session id metadata for this worker's own task."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return metadata
    session_id = os.environ.get("HERMES_SESSION_ID")
    if not session_id:
        return metadata
    stamped = dict(metadata or {})
    stamped["worker_session_id"] = session_id
    return stamped


def _enforce_worker_task_ownership(tid: str) -> Optional[str]:
    """Reject worker-driven destructive calls on foreign task IDs.

    A process spawned by the dispatcher has ``HERMES_KANBAN_TASK`` set
    to its own task id. Tools like ``kanban_complete`` / ``kanban_block``
    / ``kanban_heartbeat`` mutate run-lifecycle state, so a buggy or
    prompt-injected worker that passed an explicit ``task_id`` for some
    other task could corrupt sibling or cross-tenant runs (see #19534).

    Orchestrator profiles (kanban toolset enabled but **no**
    ``HERMES_KANBAN_TASK`` in env) aren't subject to this check — their
    job is routing, and they sometimes legitimately close out child
    tasks or reopen blocked ones. Workers are narrowly scoped to their
    one task.

    Returns ``None`` when the call is allowed, or a tool-error string
    when it must be rejected. Callers should ``return`` the error
    verbatim.
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if not env_tid:
        # Orchestrator or CLI context — no task-scope restriction.
        return None
    if tid != env_tid:
        return tool_error(
            f"worker is scoped to task {env_tid}; refusing to mutate "
            f"{tid}. Use kanban_comment to hand off information to other "
            f"tasks, or kanban_create to spawn follow-up work."
        )
    return None


def _connect(board: Optional[str] = None):
    """Import + connect lazily so the module imports cleanly in non-kanban
    contexts (e.g. test rigs that import every tool module).

    When ``board`` is provided it's forwarded to :func:`kb.connect`, which
    routes the connection to that board's sqlite file. ``None`` (the
    default) preserves the legacy resolution chain
    (``HERMES_KANBAN_DB`` → ``HERMES_KANBAN_BOARD`` env → current symlink
    → ``default``). Per-tool ``board`` lets a Telegram-side agent override
    the env-pinned active board without restarting Hermes.
    """
    from hermes_cli import kanban_db as kb
    return kb, kb.connect(board=board)


# ---------------------------------------------------------------------------
# Runtime-activity → board-heartbeat bridge (#31752)
# ---------------------------------------------------------------------------
# When the agent ticks ``_touch_activity`` during normal work (between
# tool calls, mid-stream chunks, etc.), we want the kanban board's
# ``last_heartbeat_at`` columns to reflect that liveness so the dispatcher
# watchdog (which reads ``tasks.last_heartbeat_at``, not the agent's
# in-process timestamp) doesn't reclaim an actively-running worker as
# stale. The model is not required to call the explicit ``kanban_heartbeat``
# tool for this to work — that tool stays available for workers that want
# to attach a note or pre-emptively extend a claim across a known-long op.
#
# Constraints:
#   - Best-effort: never raise. The agent loop must not care if the bridge
#     fails (board missing, DB locked, etc.).
#   - Rate-limited to one DB write per 60s per-process; runtime activity
#     can tick on every chunk/tool result and we don't need that resolution.
#   - No-op outside dispatcher-spawned worker context (no ``HERMES_KANBAN_TASK``).
#   - No durable note on these auto-heartbeats; that's reserved for the
#     explicit tool which carries a model-supplied note.

_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0
_auto_heartbeat_last_attempt: float = 0.0


def heartbeat_current_worker_from_env() -> bool:
    """Best-effort: extend the kanban claim + bump board heartbeat for the
    current dispatcher-spawned worker, using identity from env vars.

    Returns True if a write was attempted (whether or not it succeeded);
    False if the call was skipped (not a kanban worker, rate-limited, or
    swallowed exception). The boolean is informational — callers should
    not branch on it.

    Identity comes from:
      * ``HERMES_KANBAN_TASK`` — task id (required; absence means no-op)
      * ``HERMES_KANBAN_RUN_ID`` — pins the run row so we don't heartbeat
        a stale run that may have already been reclaimed
      * ``HERMES_KANBAN_CLAIM_LOCK`` — claim lock for ``heartbeat_claim``;
        falls back to the default ``_claimer_id()`` for locally-driven
        workers that never went through the dispatcher path

    Rate-limited via the module-level ``_auto_heartbeat_last_attempt``
    timestamp (monotonic clock); not thread-safe in the strict sense, but
    the worst case is one extra DB write per race, which is harmless.
    """
    global _auto_heartbeat_last_attempt
    tid = os.environ.get("HERMES_KANBAN_TASK")
    if not tid:
        return False
    import time as _time
    now = _time.monotonic()
    if (now - _auto_heartbeat_last_attempt) < _AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS:
        return False
    _auto_heartbeat_last_attempt = now
    try:
        kb, conn = _connect()
        try:
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            try:
                kb.heartbeat_claim(conn, tid, claimer=claim_lock)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_claim failed", exc_info=True)
            run_id_raw = os.environ.get("HERMES_KANBAN_RUN_ID")
            run_id: Optional[int]
            try:
                run_id = int(run_id_raw) if run_id_raw else None
            except (TypeError, ValueError):
                run_id = None
            try:
                kb.heartbeat_worker(conn, tid, note=None, expected_run_id=run_id)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_worker failed", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True
    except Exception:
        logger.debug("auto-heartbeat: bridge failed", exc_info=True)
        return False


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _public_launch_approval(kb, board: Any, approval: Any) -> Optional[dict[str, Any]]:
    if isinstance(board, dict):
        summary = kb._public_launch_approval_summary(board)
        if summary:
            return summary
    if not isinstance(approval, dict):
        return None
    return {
        "id": str(approval.get("id") or "").strip() or None,
        "status": str(approval.get("status") or "").strip() or None,
        "approved_by": str(approval.get("approved_by") or "").strip() or None,
        "reason": str(approval.get("reason") or "").strip() or None,
        "contract_version": kb._normalize_contract_version(approval.get("contract_version")),
        "amendment_id": approval.get("amendment_id") or None,
        "created_at": approval.get("created_at"),
    }


def _public_launch_board(kb, meta: Any) -> Any:
    if not isinstance(meta, dict):
        return meta
    return {
        "slug": meta.get("slug"),
        "name": meta.get("name"),
        "description": meta.get("description"),
        "runtime": meta.get("runtime"),
        "objective": meta.get("objective"),
        "workflow": meta.get("workflow"),
        "business_contract": meta.get("business_contract"),
        "launch_phase": meta.get("launch_phase"),
        "contract_version": meta.get("contract_version"),
        "contract_readiness": meta.get("contract_readiness"),
        "launch_review_id": meta.get("launch_review_id"),
        "launch_approval": kb._public_launch_approval_summary(meta),
    }


def _sanitize_launch_tool_result(kb, result: dict[str, Any]) -> dict[str, Any]:
    """Remove approval-token authority from model-facing launch tools."""
    sanitized = dict(result)
    board = sanitized.get("board")
    approval = sanitized.get("approval")
    sanitized["board"] = _public_launch_board(kb, board)
    if approval is not None:
        sanitized["approval"] = _public_launch_approval(kb, board, approval)
    _attach_launch_intake_followup(sanitized)
    return sanitized


def _extract_launch_question_generation(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    intake = payload.get("launch_intake")
    if isinstance(intake, dict) and isinstance(intake.get("question_generation"), dict):
        return dict(intake["question_generation"])
    readiness = payload.get("readiness")
    if isinstance(readiness, dict) and isinstance(readiness.get("question_generation"), dict):
        return dict(readiness["question_generation"])
    return None


def _extract_launch_answer_assessment(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    intake = payload.get("launch_intake")
    if isinstance(intake, dict) and isinstance(intake.get("answer_assessment"), dict):
        return dict(intake["answer_assessment"])
    readiness = payload.get("readiness")
    if isinstance(readiness, dict) and isinstance(readiness.get("answer_assessment"), dict):
        return dict(readiness["answer_assessment"])
    return None


def _render_owner_contract_summary_for_payload(payload: dict[str, Any]) -> str:
    """Render a deterministic owner-facing contract summary from a tool result.

    Reads the normalized ``contract`` (and the board slug, when present) off a
    launch-review result and returns the shared, human-legible Markdown summary.
    Best-effort: any failure (missing/partial contract, import error) returns an
    empty string so the caller falls back to the model-authored summary rather
    than breaking the tool result.
    """
    contract = payload.get("contract")
    if not isinstance(contract, dict):
        return ""
    board_slug = None
    board_meta = payload.get("board")
    if isinstance(board_meta, dict):
        board_slug = board_meta.get("slug")
    try:
        from hermes_cli.kanban_launch_summary import render_owner_contract_summary
        return render_owner_contract_summary(
            contract, board=board_slug, include_approve_hint=True, active=False
        )
    except Exception:
        logger.debug("owner contract summary render failed", exc_info=True)
        return ""


def _attach_launch_intake_followup(payload: dict[str, Any]) -> None:
    """Tell the model what to say next after a launch-intake tool call."""
    questions = payload.get("questions")
    question_list = [str(q) for q in questions] if isinstance(questions, list) else []
    intake = payload.get("launch_intake")
    if isinstance(intake, dict):
        intake_state = str(intake.get("state") or "").strip().lower()
        answer_quality = intake.get("answer_quality")
        answer_sufficient = (
            isinstance(answer_quality, dict)
            and (
                bool(answer_quality.get("sufficient"))
                or str(answer_quality.get("status") or "").strip().lower()
                in {"sufficient", "ready"}
            )
        )
        if intake_state == "ready_for_owner_review" and answer_sufficient:
            owner_summary_text = _render_owner_contract_summary_for_payload(payload)
            next_action = {
                "type": "launch_contract_owner_review",
                "required": True,
                "must_show_owner_review_now": True,
                "instruction": (
                    "Your next assistant response must show the owner the drafted board "
                    "operating contract in plain language. A deterministic, owner-ready "
                    "summary is provided in this result as 'owner_contract_summary' -- relay "
                    "it (you may lightly adjust tone, but keep every section: pipeline, what "
                    "it watches, what needs approval, the auto-tunable dials, and the "
                    "/approve call to action). State that the board is still in "
                    "contract_review, dispatch is disabled, and owner approval is required "
                    "before activation. Do not call intake_answers again. Do not approve or "
                    "activate launch."
                ),
                "response_style": (
                    "Keep it owner-facing and concise. Prefer the provided "
                    "owner_contract_summary over re-deriving your own; never dump raw "
                    "contract JSON to the owner."
                ),
            }
            if owner_summary_text:
                next_action["owner_contract_summary"] = owner_summary_text
                payload["owner_contract_summary"] = owner_summary_text
            payload["assistant_next_action"] = next_action
            return
    server_orchestrated = False
    if isinstance(intake, dict):
        gen = intake.get("question_generation")
        server_orchestrated = (
            str(intake.get("source") or "").strip().lower() == "server_generated"
            or (isinstance(gen, dict) and str(gen.get("mode") or "").strip().lower() == "server_generated")
        )
    assessment = _extract_launch_answer_assessment(payload)
    server_assessed = isinstance(assessment, dict) and (
        bool(assessment.get("result"))
        or str(
            (intake.get("answer_quality") or {}).get("assessed_by")
            if isinstance(intake, dict)
            else ""
        ).strip().lower()
        == "server"
    )
    if assessment and not server_assessed:
        mode = str(assessment.get("mode") or "").strip().lower()
        if assessment.get("required") and mode == "model_assessed":
            payload["assistant_next_action"] = {
                "type": "launch_intake_answer_assessment",
                "required": True,
                "must_assess_answers_now": True,
                "mode": "model_assessed_answers",
                "source": "launch_intake.answer_assessment",
                "instruction": (
                    "Your next assistant response must use launch_intake.answer_assessment.system_prompt "
                    "to assess the owner's answers. If the answers are vague, incomplete, risky, or "
                    "contradictory, ask 1-4 sharper follow-up questions now. If the answers are sufficient, "
                    "do not merely summarize; draft the board contract and call kanban_business_launch_review "
                    "again with that contract and launch_intake.answer_quality.sufficient=true. Do not call "
                    "kanban_business_launch_review again with the same intake_answers; repeated answer "
                    "submissions are rejected until the owner changes the answers. Do not approve or activate "
                    "launch from this answer assessment."
                ),
                "response_style": (
                    "Be direct and owner-facing. Do not expose internal schema names unless drafting the "
                    "actual contract tool payload."
                ),
            }
            return
    generation = _extract_launch_question_generation(payload)
    if question_list:
        if server_orchestrated:
            instruction = (
                "Relay the server-generated clarification questions below to the owner "
                "verbatim (lightly rephrasing for tone only). Hermes already generated "
                "these server-side; do NOT invent your own questions, and do not draft or "
                "approve the board contract yet. Collect the owner's answers and submit "
                "them back via kanban_business_launch_review intake_answers."
            )
            mode = "relay_server_generated_questions"
        else:
            instruction = (
                "Ask the owner the provided clarification questions now. "
                "Do not draft or approve the board contract yet."
            )
            mode = "provided_questions"
        payload["assistant_next_action"] = {
            "type": "launch_intake_clarification",
            "required": True,
            "must_ask_owner_now": True,
            "mode": mode,
            "instruction": instruction,
            "questions": question_list[:6],
        }
        return
    if not generation:
        return
    mode = str(generation.get("mode") or "").strip().lower()
    if not generation.get("required") or mode != "model_generated":
        return
    payload["assistant_next_action"] = {
        "type": "launch_intake_clarification",
        "required": True,
        "must_ask_owner_now": True,
        "mode": "model_generated_questions",
        "source": "launch_intake.question_generation",
        "instruction": (
            "Your next assistant response must use launch_intake.question_generation.system_prompt "
            "to generate and ask 2-6 tailored, owner-facing clarification questions from the rough "
            "goal and conversation context. Ask the questions now; do not say Hermes will ask later. "
            "Do not draft the board contract, create stages, or approve launch until the owner answers."
        ),
        "response_style": (
            "Ask concise plain-language questions only. Avoid internal terms such as schemas, "
            "profiles, dispatchers, event loops, provider policies, and worker envelopes."
        ),
    }


def _normalize_profile(value: Any) -> Optional[str]:
    """Normalize CLI-compatible assignee sentinels for the tool surface."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "-", "null"}:
        return None
    return text


def _get_company_workers() -> Optional[list]:
    """Read the calling profile's company.workers from config.

    Returns None if the profile has no company config (no enforcement).
    Returns a list of allowed worker profile names if company.workers is defined.
    """
    from hermes_constants import get_hermes_home
    config_path = get_hermes_home() / "config.yaml"
    if not config_path.exists():
        return None
    try:
        content = config_path.read_text()
    except OSError:
        return None
    # Quick check: does this profile have type: company?
    if "type: company" not in content:
        return None
    # Parse workers list from YAML (simple line-based, no dep needed)
    workers = []
    in_workers = False
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("workers:"):
            val = stripped[len("workers:"):].strip()
            if val == "[]":
                return []
            in_workers = True
            continue
        if in_workers:
            if stripped.startswith("- "):
                workers.append(stripped[2:].strip())
            elif stripped and not stripped.startswith("#"):
                break
    return workers if workers else None


def _get_company_optimizer() -> Optional[str]:
    """Read the company.optimizer field from the CEO config that owns this worker."""
    from hermes_constants import get_hermes_home
    config_path = get_hermes_home() / "config.yaml"
    if not config_path.exists():
        return None
    try:
        content = config_path.read_text()
    except OSError:
        return None
    # Check if this is a worker profile — find its company CEO
    company_ceo = None
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("company:") and "name:" not in stripped:
            company_ceo = stripped[len("company:"):].strip()
            break
    # If this IS the CEO, read optimizer directly
    if "type: company" in content:
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("optimizer:"):
                return stripped[len("optimizer:"):].strip()
        return None
    # If this is a worker, read the CEO's config to find the optimizer
    if company_ceo:
        hermes_home = get_hermes_home()
        # Go up to profiles dir
        profiles_dir = hermes_home.parent
        ceo_config = profiles_dir / company_ceo / "config.yaml"
        if ceo_config.exists():
            try:
                ceo_content = ceo_config.read_text()
            except OSError:
                return None
            for line in ceo_content.split("\n"):
                stripped = line.strip()
                if stripped.startswith("optimizer:"):
                    return stripped[len("optimizer:"):].strip()
    return None


_optimizer_webhook_url_cache = None
_optimizer_webhook_url_cache_time = 0
_WEBHOOK_CACHE_TTL = 300  # 5 minutes


def _get_optimizer_webhook_url() -> Optional[str]:
    """Resolve the optimizer's webhook URL from the CEO config. Cached 5 min."""
    global _optimizer_webhook_url_cache, _optimizer_webhook_url_cache_time
    import time as _time
    now = _time.time()
    if _optimizer_webhook_url_cache and (now - _optimizer_webhook_url_cache_time) < _WEBHOOK_CACHE_TTL:
        return _optimizer_webhook_url_cache

    from hermes_constants import get_hermes_home
    optimizer = _get_company_optimizer()
    if not optimizer:
        return None
    # Find the optimizer's config to get its webhook port
    hermes_home = get_hermes_home()
    profiles_dir = hermes_home.parent
    opt_config = profiles_dir / optimizer / "config.yaml"
    if not opt_config.exists():
        return None
    try:
        content = opt_config.read_text()
    except OSError:
        return None
    # Parse port from webhook platform config
    port = "8700"  # default
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("port:"):
            port = stripped[len("port:"):].strip().strip('"')
            break
    # Parse host
    host = "127.0.0.1"
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("host:"):
            host = stripped[len("host:"):].strip().strip('"')
            break
    url = f"http://{host}:{port}/webhooks/card_event"
    _optimizer_webhook_url_cache = url
    _optimizer_webhook_url_cache_time = now
    return url


def _notify_optimizer(event_type: str, task_id: str, extra: dict = None):
    """POST an event to the optimizer's webhook. Non-blocking, best-effort."""
    import threading
    url = _get_optimizer_webhook_url()
    if not url:
        return

    def _post():
        import urllib.request
        import json as _json
        payload = {"event_type": event_type, "task_id": task_id}
        if extra:
            payload.update(extra)
        data = _json.dumps(payload).encode()
        try:
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
    threading.Thread(target=_post, daemon=True).start()


def _notify_optimizer_of_block(kb, conn, task_id: str, reason: str, board=None):
    """Notify optimizer that a card blocked."""
    optimizer = _get_company_optimizer()
    if not optimizer:
        return
    task = kb.get_task(conn, task_id)
    if task and task.assignee == optimizer:
        return
    _notify_optimizer("card.blocked", task_id, {
        "assignee": task.assignee if task else None,
        "reason": reason,
        "board": board,
    })


def _parse_bool_arg(args: dict, name: str, *, default: bool = False):
    value = args.get(name)
    if value is None:
        return default, None
    if isinstance(value, bool):
        return value, None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True, None
    if text in {"false", "0", "no"}:
        return False, None
    return default, f"{name} must be a boolean or 'true'/'false'"


def _require_orchestrator_tool(
    tool_name: str,
    *,
    allow_launch_intake: bool = False,
) -> Optional[str]:
    """Belt-and-suspenders runtime guard for orchestrator-only handlers.

    The check_fn (`_check_kanban_orchestrator_mode`) keeps these tools
    out of the worker schema entirely, but in case a stale registration
    or test harness routes a worker to one of them anyway, return a
    structured tool_error so the model gets a clear refusal instead of
    silently mutating board state from a worker context.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return tool_error(
            f"{tool_name} is orchestrator-only; dispatcher-spawned workers "
            "must use kanban_complete, kanban_block, kanban_watch, "
            "kanban_heartbeat, or kanban_comment for their assigned task."
        )
    if _is_owner_launch_intake_profile() and not allow_launch_intake:
        return tool_error(
            f"{tool_name} requires the full kanban orchestrator surface; "
            "owner launch-intake profiles are limited to launch status, "
            "review, and contract amendment tools."
        )
    return None


def _task_summary_dict(kb, conn, task) -> dict[str, Any]:
    """Compact task shape for board-listing tools."""
    parents = kb.parent_ids(conn, task.id)
    children = kb.child_ids(conn, task.id)
    return {
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status,
        "priority": task.priority,
        "tenant": task.tenant,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "current_run_id": task.current_run_id,
        "model_override": task.model_override,
        "goal_id": getattr(task, "goal_id", None),
        "workstream_id": getattr(task, "workstream_id", None),
        "stage_key": getattr(task, "stage_key", None),
        "action_key": getattr(task, "action_key", None),
        "funnel_data": getattr(task, "funnel_data", None),
        "watch_routes": [
            kb.watch_route_to_dict(route)
            for route in kb.list_watch_routes(conn, task.id, active=True)
        ],
        "parents": parents,
        "children": children,
        "parent_count": len(parents),
        "child_count": len(children),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_show(args: dict, **kw) -> str:
    """Read a task's full state: task row, parents, children, comments,
    runs (attempt history), and the last N events."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return tool_error(f"task {tid} not found")
            comments = kb.list_comments(conn, tid)
            events = kb.list_events(conn, tid)
            runs = kb.list_runs(conn, tid)
            parents = kb.parent_ids(conn, tid)
            children = kb.child_ids(conn, tid)

            def _task_dict(t):
                return {
                    "id": t.id, "title": t.title, "body": t.body,
                    "assignee": t.assignee, "status": t.status,
                    "tenant": t.tenant, "priority": t.priority,
                    "workspace_kind": t.workspace_kind,
                    "workspace_path": t.workspace_path,
                    "created_by": t.created_by, "created_at": t.created_at,
                    "started_at": t.started_at,
                    "completed_at": t.completed_at,
                    "result": t.result,
                    "current_run_id": t.current_run_id,
                    "model_override": t.model_override,
                    "goal_id": getattr(t, "goal_id", None),
                    "workstream_id": getattr(t, "workstream_id", None),
                    "stage_key": getattr(t, "stage_key", None),
                    "action_key": getattr(t, "action_key", None),
                    "funnel_data": getattr(t, "funnel_data", None),
                    "watch_routes": [
                        kb.watch_route_to_dict(route)
                        for route in kb.list_watch_routes(conn, t.id, active=True)
                    ],
                }

            def _run_dict(r):
                return {
                    "id": r.id, "profile": r.profile,
                    "status": r.status, "outcome": r.outcome,
                    "summary": r.summary, "error": r.error,
                    "metadata": r.metadata,
                    "started_at": r.started_at, "ended_at": r.ended_at,
                }

            return json.dumps({
                "task": _task_dict(task),
                "parents": parents,
                "children": children,
                "comments": [
                    {"author": c.author, "body": c.body,
                     "created_at": c.created_at}
                    for c in comments
                ],
                "events": [
                    {"kind": e.kind, "payload": e.payload,
                     "created_at": e.created_at, "run_id": e.run_id}
                    for e in events[-50:]   # cap; full log via CLI
                ],
                "runs": [_run_dict(r) for r in runs],
                # Also surface the worker's own context block so the
                # agent can include it directly if it wants. This is
                # the same string build_worker_context returns to the
                # dispatcher at spawn time.
                "worker_context": kb.build_worker_context(conn, tid),
            })
        finally:
            conn.close()
    except ValueError as e:
        # Invalid board slug surfaces as ValueError from _normalize_board_slug.
        return tool_error(f"kanban_show: {e}")
    except Exception as e:
        logger.exception("kanban_show failed")
        return tool_error(f"kanban_show: {e}")


def _handle_list(args: dict, **kw) -> str:
    """List task summaries with the same core filters as the CLI."""
    guard = _require_orchestrator_tool("kanban_list")
    if guard:
        return guard
    assignee = args.get("assignee")
    status = args.get("status")
    tenant = args.get("tenant")
    include_archived, bool_error = _parse_bool_arg(args, "include_archived")
    if bool_error:
        return tool_error(bool_error)
    limit = args.get("limit")
    if limit is None:
        limit = KANBAN_LIST_DEFAULT_LIMIT
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return tool_error("limit must be an integer")
    if limit < 1:
        return tool_error("limit must be >= 1")
    if limit > KANBAN_LIST_MAX_LIMIT:
        return tool_error(f"limit must be <= {KANBAN_LIST_MAX_LIMIT}")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Match CLI list: dependencies that cleared since the last
            # dispatcher tick should be visible to orchestrators immediately.
            promoted = kb.recompute_ready(conn)
            # Fetch one extra row so model-facing output can report that
            # a bounded listing was truncated without dumping the board.
            rows = kb.list_tasks(
                conn,
                assignee=assignee,
                status=status,
                tenant=tenant,
                include_archived=include_archived,
                limit=limit + 1,
            )
            truncated = len(rows) > limit
            tasks = rows[:limit]
            return json.dumps({
                "tasks": [_task_summary_dict(kb, conn, t) for t in tasks],
                "count": len(tasks),
                "limit": limit,
                "truncated": truncated,
                "next_limit": (
                    min(limit * 2, KANBAN_LIST_MAX_LIMIT)
                    if truncated and limit < KANBAN_LIST_MAX_LIMIT else None
                ),
                "promoted": promoted,
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_list: {e}")
    except Exception as e:
        logger.exception("kanban_list failed")
        return tool_error(f"kanban_list: {e}")


def _handle_funnel(args: dict, **kw) -> str:
    """Return the semantic funnel read-model for orchestrators/optimizers."""
    guard = _require_orchestrator_tool("kanban_funnel")
    if guard:
        return guard
    include_archived, bool_error = _parse_bool_arg(args, "include_archived")
    if bool_error:
        return tool_error(bool_error)
    limit_cards = args.get("limit_cards_per_stage")
    if limit_cards is None:
        limit_cards = 20
    try:
        limit_cards = int(limit_cards)
    except (TypeError, ValueError):
        return tool_error("limit_cards_per_stage must be an integer")
    if limit_cards < 0:
        return tool_error("limit_cards_per_stage must be >= 0")
    limit_entities = args.get("limit_entities_per_stage")
    if limit_entities is None:
        limit_entities = 50
    try:
        limit_entities = int(limit_entities)
    except (TypeError, ValueError):
        return tool_error("limit_entities_per_stage must be an integer")
    if limit_entities < 0:
        return tool_error("limit_entities_per_stage must be >= 0")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            model = kb.build_funnel_read_model(
                conn,
                board=board,
                include_archived=include_archived,
                limit_cards_per_stage=limit_cards,
                limit_entities_per_stage=limit_entities,
                goal_id=args.get("goal"),
                workstream_id=args.get("workstream"),
                stage_key=args.get("stage"),
                action_key=args.get("action"),
            )
            return json.dumps(model, ensure_ascii=False)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_funnel: {e}")
    except Exception as e:
        logger.exception("kanban_funnel failed")
        return tool_error(f"kanban_funnel: {e}")


def _board_listing_summary(meta: dict) -> dict[str, Any]:
    """Compact, model-facing board shape for discovery/routing tools.

    Deliberately metadata-only (no DB connection) so enumerating boards
    can never initialise a board's sqlite file as a side effect.
    """
    objective = meta.get("objective")
    objective_statement = None
    if isinstance(objective, dict):
        objective_statement = objective.get("statement")
    elif isinstance(objective, str):
        objective_statement = objective
    runtime = meta.get("runtime")
    runtime_mode = runtime.get("mode") if isinstance(runtime, dict) else runtime
    return {
        "slug": meta.get("slug"),
        "name": meta.get("name"),
        "description": meta.get("description") or "",
        "objective": objective_statement,
        "launch_phase": meta.get("launch_phase"),
        "runtime": runtime_mode,
        "archived": bool(meta.get("archived")),
    }


def _real_boards(kb, *, include_archived: bool) -> list[dict]:
    """Enumerate boards that genuinely exist on disk.

    ``kanban_db.list_boards`` always synthesises a ``default`` entry even
    when ``boards/default/board.json`` was never written (the historical
    top-level DB special-case). The router contract is "no standing
    personal board; nothing auto-created", so a ``default`` board that has
    no ``board.json`` metadata of its own is the phantom entry and must be
    excluded. Every other board returned by ``list_boards`` already passed
    the has-db-or-has-metadata filter, so it is real and kept.
    """
    boards = kb.list_boards(include_archived=include_archived)
    out: list[dict] = []
    for meta in boards:
        slug = meta.get("slug")
        if slug == kb.DEFAULT_BOARD:
            try:
                has_meta = kb.board_metadata_path(slug).exists()
            except Exception:
                has_meta = False
            if not has_meta:
                # Phantom unconfigured default board — drop it.
                continue
        out.append(meta)
    return out


def _handle_list_boards(args: dict, **kw) -> str:
    """Enumerate real boards (metadata only) for the reasoning+router front-end."""
    guard = _require_orchestrator_tool(
        "kanban_list_boards",
        allow_launch_intake=True,
    )
    if guard:
        return guard
    include_archived, bool_error = _parse_bool_arg(
        args, "include_archived", default=False
    )
    if bool_error:
        return tool_error(bool_error)
    try:
        from hermes_cli import kanban_db as kb
        boards = [
            _board_listing_summary(meta)
            for meta in _real_boards(kb, include_archived=include_archived)
        ]
        return json.dumps({
            "ok": True,
            "count": len(boards),
            "boards": boards,
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("kanban_list_boards failed")
        return tool_error(f"kanban_list_boards: {e}")


_BOARD_MATCH_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with",
    "my", "our", "your", "i", "we", "want", "need", "get", "more", "new",
    "set", "up", "start", "launch", "build", "make", "do", "run", "help",
    "business", "board", "system", "project", "work", "workflow", "this",
    "that", "it", "is", "are", "be", "can", "will", "would", "should",
})


def _board_match_tokens(text: str) -> set[str]:
    import re

    tokens = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return {tok for tok in tokens if len(tok) > 2 and tok not in _BOARD_MATCH_STOPWORDS}


def _handle_match_board(args: dict, **kw) -> str:
    """Surface candidate boards for a free-text goal.

    This tool does NOT decide the match — it enumerates real boards and
    attaches a coarse lexical overlap score so the agent can do the actual
    semantic matching in-model. The full candidate list is always returned
    (ranked best-first) so the model can reason over every option, not just
    the top lexical hit.
    """
    guard = _require_orchestrator_tool(
        "kanban_match_board",
        allow_launch_intake=True,
    )
    if guard:
        return guard
    goal = str(args.get("goal") or args.get("rough_goal") or "").strip()
    if not goal:
        return tool_error("goal is required (free-text description of the work)")
    include_archived, bool_error = _parse_bool_arg(
        args, "include_archived", default=False
    )
    if bool_error:
        return tool_error(bool_error)
    goal_tokens = _board_match_tokens(goal)
    try:
        from hermes_cli import kanban_db as kb
        candidates: list[dict] = []
        for meta in _real_boards(kb, include_archived=include_archived):
            summary = _board_listing_summary(meta)
            haystack = " ".join(
                str(part) for part in (
                    summary["slug"], summary["name"],
                    summary["description"], summary["objective"],
                ) if part
            )
            board_tokens = _board_match_tokens(haystack)
            overlap = sorted(goal_tokens & board_tokens)
            summary["match_score"] = len(overlap)
            summary["match_terms"] = overlap
            candidates.append(summary)
        candidates.sort(
            key=lambda c: (c["match_score"], c["slug"] or ""),
            reverse=True,
        )
        return json.dumps({
            "ok": True,
            "goal": goal,
            "count": len(candidates),
            "candidates": candidates,
            "guidance": (
                "match_score is a coarse lexical hint only. Decide the actual "
                "match by reasoning over each board's name/description/objective. "
                "If a board clearly covers this goal, work in it. If none fit, "
                "clarify with the owner, then propose a new board via "
                "kanban_business_launch_review(create_if_missing=true)."
            ),
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("kanban_match_board failed")
        return tool_error(f"kanban_match_board: {e}")


def _handle_board_launch_status(args: dict, **kw) -> str:
    """Read board launch phase, contract readiness, and clarity questions."""
    guard = _require_orchestrator_tool(
        "kanban_board_launch_status",
        allow_launch_intake=True,
    )
    if guard:
        return guard
    board = args.get("board")
    try:
        from hermes_cli import kanban_db as kb
        meta = kb.read_board_metadata(board)
        status = kb.validate_board_launch_readiness(board)
        board_summary = {
            "slug": meta.get("slug"),
            "name": meta.get("name"),
            "description": meta.get("description"),
            "runtime": meta.get("runtime"),
            "objective": meta.get("objective"),
            "workflow": meta.get("workflow"),
            "business_contract": meta.get("business_contract"),
            "launch_phase": meta.get("launch_phase"),
            "contract_version": meta.get("contract_version"),
            "contract_readiness": meta.get("contract_readiness"),
            "launch_review_id": meta.get("launch_review_id"),
        }
        return json.dumps({
            "ok": True,
            "board": board_summary,
            "launch": status,
        }, ensure_ascii=False)
    except ValueError as e:
        return tool_error(f"kanban_board_launch_status: {e}")
    except Exception as e:
        logger.exception("kanban_board_launch_status failed")
        return tool_error(f"kanban_board_launch_status: {e}")


def _handle_business_launch_review(args: dict, **kw) -> str:
    """Store/review a draft business board contract before launch."""
    guard = _require_orchestrator_tool(
        "kanban_business_launch_review",
        allow_launch_intake=True,
    )
    if guard:
        return guard
    board = args.get("board")
    contract = args.get("contract")
    rough_goal = args.get("rough_goal")
    intake_answers = args.get("intake_answers")
    if not contract and not rough_goal and intake_answers is None:
        return tool_error("contract, rough_goal, or intake_answers is required")
    create_if_missing, bool_error = _parse_bool_arg(args, "create_if_missing")
    if bool_error:
        return tool_error(bool_error)
    approve, bool_error = _parse_bool_arg(args, "approve")
    if bool_error:
        return tool_error(bool_error)
    try:
        from hermes_cli import kanban_db as kb
        result = kb.review_business_launch_contract(
            board,
            contract=contract,
            rough_goal=rough_goal,
            intake_answers=intake_answers,
            create_if_missing=create_if_missing,
            approve=approve,
            name=args.get("name"),
            description=args.get("description"),
            author=os.environ.get("HERMES_PROFILE") or "orchestrator",
            approved_by=args.get("approved_by"),
            approval_evidence=args.get("approval_evidence"),
            approval_reason=args.get("approval_reason"),
            approval_token=args.get("approval_token"),
            require_launch_intake=_is_owner_launch_intake_profile(),
        )
        return json.dumps(_sanitize_launch_tool_result(kb, result), ensure_ascii=False)
    except ValueError as e:
        return tool_error(f"kanban_business_launch_review: {e}")
    except Exception as e:
        logger.exception("kanban_business_launch_review failed")
        return tool_error(f"kanban_business_launch_review: {e}")


def _handle_contract_amendment_propose(args: dict, **kw) -> str:
    """Create a pending board contract amendment."""
    guard = _require_orchestrator_tool(
        "kanban_contract_amendment_propose",
        allow_launch_intake=True,
    )
    if guard:
        return guard
    board = args.get("board")
    patch = args.get("patch")
    reason = args.get("reason")
    if not board:
        return tool_error("board is required")
    if not isinstance(patch, dict):
        return tool_error("patch must be an object/dict")
    if not str(reason or "").strip():
        return tool_error("reason is required")
    try:
        from hermes_cli import kanban_db as kb
        amendment = kb.propose_board_contract_amendment(
            board,
            patch=patch,
            reason=str(reason),
            author=os.environ.get("HERMES_PROFILE") or "orchestrator",
            risk=args.get("risk"),
        )
        return _ok(amendment=amendment)
    except ValueError as e:
        return tool_error(f"kanban_contract_amendment_propose: {e}")
    except Exception as e:
        logger.exception("kanban_contract_amendment_propose failed")
        return tool_error(f"kanban_contract_amendment_propose: {e}")


def _handle_contract_amendment_apply(args: dict, **kw) -> str:
    """Apply a pending board contract amendment after approval."""
    guard = _require_orchestrator_tool(
        "kanban_contract_amendment_apply",
        allow_launch_intake=True,
    )
    if guard:
        return guard
    board = args.get("board")
    amendment_id = args.get("amendment_id")
    if not board:
        return tool_error("board is required")
    if not amendment_id:
        return tool_error("amendment_id is required")
    force, bool_error = _parse_bool_arg(args, "force")
    if bool_error:
        return tool_error(bool_error)
    if force:
        return tool_error(
            "kanban_contract_amendment_apply: force apply is not available; "
            "submit a launch-ready amendment and owner approval_token"
        )
    activate, bool_error = _parse_bool_arg(args, "activate", default=True)
    if bool_error:
        return tool_error(bool_error)
    try:
        from hermes_cli import kanban_db as kb
        result = kb.apply_board_contract_amendment(
            board,
            str(amendment_id),
            approved_by=args.get("approved_by"),
            approval_evidence=args.get("approval_evidence"),
            approval_token=args.get("approval_token"),
            activate=activate,
        )
        return json.dumps(_sanitize_launch_tool_result(kb, result), ensure_ascii=False)
    except ValueError as e:
        return tool_error(f"kanban_contract_amendment_apply: {e}")
    except Exception as e:
        logger.exception("kanban_contract_amendment_apply failed")
        return tool_error(f"kanban_contract_amendment_apply: {e}")


def _handle_complete(args: dict, **kw) -> str:
    """Mark the current task done with a structured handoff."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    summary = args.get("summary")
    metadata = args.get("metadata")
    result = args.get("result")
    created_cards = args.get("created_cards")
    artifacts = args.get("artifacts")
    if created_cards is not None:
        if isinstance(created_cards, str):
            # Accept a single id as a string for convenience.
            created_cards = [created_cards]
        if not isinstance(created_cards, (list, tuple)):
            return tool_error(
                f"created_cards must be a list of task ids, got "
                f"{type(created_cards).__name__}"
            )
        # Normalise: strings only, stripped, non-empty.
        created_cards = [
            str(c).strip() for c in created_cards if str(c).strip()
        ]
    if artifacts is not None:
        if isinstance(artifacts, str):
            # Accept a single path as a string for convenience.
            artifacts = [artifacts]
        if not isinstance(artifacts, (list, tuple)):
            return tool_error(
                f"artifacts must be a list of file paths, got "
                f"{type(artifacts).__name__}"
            )
        artifacts = [
            str(p).strip() for p in artifacts if str(p).strip()
        ]
        # Carry the artifact list inside metadata so it rides the
        # existing completed-event payload without a schema change at
        # the DB layer.  The gateway notifier reads payload['artifacts']
        # off the completion event and uploads each path as a native
        # attachment.
        if artifacts:
            if metadata is None:
                metadata = {}
            elif not isinstance(metadata, dict):
                return tool_error(
                    f"metadata must be an object/dict, got "
                    f"{type(metadata).__name__}"
                )
            # Don't overwrite an existing metadata.artifacts the worker
            # passed manually — merge instead.
            existing = metadata.get("artifacts")
            if isinstance(existing, (list, tuple)):
                merged: list[str] = []
                seen: set[str] = set()
                for item in list(existing) + artifacts:
                    s = str(item).strip()
                    if s and s not in seen:
                        seen.add(s)
                        merged.append(s)
                metadata["artifacts"] = merged
            else:
                metadata["artifacts"] = artifacts
    if not (summary or result):
        return tool_error(
            "provide at least one of: summary (preferred), result"
        )
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    metadata = _stamp_worker_session_metadata(tid, metadata)
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            try:
                ok = kb.complete_task(
                    conn, tid,
                    result=result, summary=summary, metadata=metadata,
                    created_cards=created_cards,
                    expected_run_id=_worker_run_id(tid),
                    board=board,
                )
            except kb.HallucinatedCardsError as hall_err:
                # Structured rejection — surface the phantom ids so the
                # worker can retry with a corrected list or drop the
                # field. Audit event already landed in the DB.
                #
                # The task itself was NOT mutated (the gate runs before
                # the write txn), so the worker can simply call
                # kanban_complete again. Spell that out — without it the
                # model often interprets a tool_error as a terminal
                # failure and either blocks or crashes the run instead
                # of retrying. See #22923.
                return tool_error(
                    f"kanban_complete blocked: the following created_cards "
                    f"do not exist or were not created by this worker: "
                    f"{', '.join(hall_err.phantom)}. "
                    f"Your task is still in-flight (no state change). "
                    f"Retry kanban_complete with the same summary/metadata "
                    f"and either drop these ids from created_cards, or pass "
                    f"created_cards=[] to skip the card-claim check entirely."
                )
            except kb.ContractDoneGateError as gate_err:
                blockers = gate_err.verdict.get("blockers") or []
                codes = ", ".join(
                    str(blocker.get("code"))
                    for blocker in blockers
                    if isinstance(blocker, dict) and blocker.get("code")
                )
                return tool_error(
                    f"kanban_complete blocked by contract done gate: "
                    f"{codes or 'missing required proof'}. "
                    f"Your task is still in-flight (no state change). "
                    f"Add the required structured proof/artifacts to metadata "
                    f"and retry kanban_complete. Verdict: {gate_err.verdict}"
                )
            except kb.PixelDoneGateError as gate_err:
                blockers = gate_err.verdict.get("blockers") or []
                codes = ", ".join(
                    str(blocker.get("code"))
                    for blocker in blockers
                    if isinstance(blocker, dict) and blocker.get("code")
                )
                return tool_error(
                    f"kanban_complete blocked by Pixel done gate: "
                    f"{codes or 'blocked'}. Your task is still in-flight "
                    f"(no state change). Verdict: {gate_err.verdict}"
                )
            if not ok:
                return tool_error(
                    f"could not complete {tid} (unknown id or already terminal)"
                )
            run = kb.latest_run(conn, tid)
            return _ok(task_id=tid, run_id=run.id if run else None)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_complete: {e}")
    except Exception as e:
        logger.exception("kanban_complete failed")
        return tool_error(f"kanban_complete: {e}")


def _handle_block(args: dict, **kw) -> str:
    """Transition the task to blocked with a reason a human will read."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    reason = args.get("reason")
    if not reason or not str(reason).strip():
        return tool_error("reason is required — explain what input you need")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            ok = kb.block_task(
                conn, tid,
                reason=reason,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not block {tid} (unknown id or not in "
                    f"running/ready/watching)"
                )
            # Notify optimizer: create a micro-card so it reacts in real-time
            _notify_optimizer_of_block(kb, conn, tid, reason, board)
            run = kb.latest_run(conn, tid)
            return _ok(task_id=tid, run_id=run.id if run else None)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_block: {e}")
    except Exception as e:
        logger.exception("kanban_block failed")
        return tool_error(f"kanban_block: {e}")


def _handle_watch(args: dict, **kw) -> str:
    """Park the current task in healthy waiting until a trigger wakes it."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error("task_id is required (or set HERMES_KANBAN_TASK in the env)")
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    trigger_type = args.get("trigger_type")
    if not trigger_type:
        return tool_error("trigger_type is required")
    payload = args.get("payload")
    if payload is not None and not isinstance(payload, dict):
        return tool_error(f"payload must be an object/dict, got {type(payload).__name__}")
    wake_status = str(args.get("wake_status") or "ready")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            route = kb.set_task_watching(
                conn,
                tid,
                trigger_type=trigger_type,
                trigger_key=args.get("trigger_key"),
                reason=args.get("reason"),
                payload=payload,
                wake_status=wake_status,
                created_by=os.environ.get("HERMES_PROFILE") or "worker",
                expected_run_id=_worker_run_id(tid),
            )
            if route is None:
                return tool_error(f"could not watch {tid} (unknown id or incompatible status)")
            return _ok(task_id=tid, route=kb.watch_route_to_dict(route))
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_watch: {e}")
    except Exception as e:
        logger.exception("kanban_watch failed")
        return tool_error(f"kanban_watch: {e}")


def _handle_trigger(args: dict, **kw) -> str:
    """Wake active watch routes by id, task id, or trigger type/key."""
    guard = _require_orchestrator_tool("kanban_trigger")
    if guard:
        return guard
    payload = args.get("payload")
    if payload is not None and not isinstance(payload, dict):
        return tool_error(f"payload must be an object/dict, got {type(payload).__name__}")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            routes = kb.trigger_watch(
                conn,
                route_id=args.get("route_id"),
                task_id=args.get("task_id"),
                trigger_type=args.get("trigger_type"),
                trigger_key=args.get("trigger_key"),
                payload=payload,
                actor=os.environ.get("HERMES_PROFILE") or "orchestrator",
            )
            return _ok(routes=[kb.watch_route_to_dict(route) for route in routes], count=len(routes))
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_trigger: {e}")
    except Exception as e:
        logger.exception("kanban_trigger failed")
        return tool_error(f"kanban_trigger: {e}")


def _handle_transition(args: dict, **kw) -> str:
    """Move a card between semantic workflow stages after evidence validation."""
    guard = _require_orchestrator_tool("kanban_transition")
    if guard:
        return guard
    task_id = args.get("task_id")
    to_stage = args.get("to_stage")
    if not task_id or not to_stage:
        return tool_error("task_id and to_stage are required")
    evidence = args.get("evidence")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            task = kb.transition_task_stage(
                conn,
                task_id,
                to_stage=to_stage,
                evidence=evidence,
                action_key=args.get("action"),
                actor=os.environ.get("HERMES_PROFILE") or "orchestrator",
                board=board,
            )
            return _ok(task=_task_summary_dict(kb, conn, task))
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_transition: {e}")
    except Exception as e:
        logger.exception("kanban_transition failed")
        return tool_error(f"kanban_transition: {e}")


def _handle_heartbeat(args: dict, **kw) -> str:
    """Signal that the worker is still alive during a long operation.

    Extends the claim TTL via ``heartbeat_claim`` AND records a heartbeat
    event via ``heartbeat_worker``. Without the ``heartbeat_claim`` half,
    a diligent worker that loops this tool while a single tool call
    blocks the agent for >DEFAULT_CLAIM_TTL_SECONDS still gets reclaimed
    by ``release_stale_claims`` — which is exactly the trap that
    ``heartbeat_claim``'s docstring warns against.
    """
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    note = args.get("note")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Extend the claim TTL first. The dispatcher pins
            # HERMES_KANBAN_CLAIM_LOCK in the worker env at spawn time
            # (see _default_spawn in kanban_db.py); falling back to the
            # default _claimer_id() covers locally-driven workers that
            # never went through the dispatcher path.
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            kb.heartbeat_claim(conn, tid, claimer=claim_lock)

            ok = kb.heartbeat_worker(
                conn,
                tid,
                note=note,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not heartbeat {tid} (unknown id or not running)"
                )
            return _ok(task_id=tid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_heartbeat: {e}")
    except Exception as e:
        logger.exception("kanban_heartbeat failed")
        return tool_error(f"kanban_heartbeat: {e}")


def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    tid = args.get("task_id")
    if not tid:
        return tool_error(
            "task_id is required (use the current task id if that's what "
            "you mean — pulls from env but kept explicit here)"
        )
    body = args.get("body")
    if not body or not str(body).strip():
        return tool_error("body is required")
    # Author is intentionally derived from the worker's own runtime
    # identity, NOT from caller-supplied args. Comments are injected
    # into the next worker's system prompt by ``build_worker_context``
    # as ``**{author}** (timestamp): {body}`` — accepting an
    # ``args["author"]`` override let a worker forge a comment from
    # an authoritative-looking name like ``hermes-system`` and poison
    # the future-worker context with what reads as a system directive.
    # Cross-task commenting itself remains unrestricted (see #19713) —
    # comments are the deliberate handoff channel between tasks.
    author = os.environ.get("HERMES_PROFILE") or "worker"
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            cid = kb.add_comment(conn, tid, author=author, body=str(body))
            return _ok(task_id=tid, comment_id=cid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_comment: {e}")
    except Exception as e:
        logger.exception("kanban_comment failed")
        return tool_error(f"kanban_comment: {e}")


def _handle_create(args: dict, **kw) -> str:
    """Create a child task. Orchestrator workers use this to fan out.

    ``parents`` can be a list of task ids; dependency-gated promotion
    works as usual.
    """
    title = args.get("title")
    if not title or not str(title).strip():
        return tool_error("title is required")
    assignee = args.get("assignee")
    if not assignee:
        return tool_error(
            "assignee is required — name the profile that should execute this "
            "task (the dispatcher will only spawn tasks with an assignee)"
        )
    # Enforce company-scoped assignment: if the calling profile has
    # company.workers defined, only allow assignment to those workers.
    _allowed = _get_company_workers()
    if _allowed is not None and str(assignee) not in _allowed:
        return tool_error(
            f"assignee '{assignee}' is not in this company's workers list. "
            f"Allowed: {', '.join(_allowed) if _allowed else '(none — no workers configured)'}"
        )
    body = args.get("body")
    parents = args.get("parents") or []
    tenant = args.get("tenant") or os.environ.get("HERMES_TENANT")
    # Stamp the originating session id when the agent loop runs under
    # ACP (which sets HERMES_SESSION_ID before invoking tools). NULL on
    # CLI / dashboard paths and on legacy hosts that don't set the env.
    session_id = args.get("session_id") or os.environ.get("HERMES_SESSION_ID")
    priority = args.get("priority")
    workspace_kind = args.get("workspace_kind") or "scratch"
    workspace_path = args.get("workspace_path")
    triage, bool_error = _parse_bool_arg(args, "triage")
    if bool_error:
        return tool_error(bool_error)
    idempotency_key = args.get("idempotency_key")
    max_runtime_seconds = args.get("max_runtime_seconds")
    initial_status = args.get("initial_status")
    skills = args.get("skills")
    if isinstance(skills, str):
        # Accept a single skill name as a string for convenience.
        skills = [skills]
    if skills is not None and not isinstance(skills, (list, tuple)):
        return tool_error(
            f"skills must be a list of skill names, got {type(skills).__name__}"
        )
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, (list, tuple)):
        return tool_error(
            f"parents must be a list of task ids, got {type(parents).__name__}"
        )
    funnel_data = args.get("funnel_data")
    if funnel_data is not None and not isinstance(funnel_data, dict):
        return tool_error(
            f"funnel_data must be an object/dict, got {type(funnel_data).__name__}"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            new_tid = kb.create_task(
                conn,
                title=str(title).strip(),
                body=body,
                assignee=str(assignee),
                parents=tuple(parents),
                tenant=tenant,
                priority=int(priority) if priority is not None else 0,
                workspace_kind=str(workspace_kind),
                workspace_path=workspace_path,
                triage=triage,
                idempotency_key=idempotency_key,
                max_runtime_seconds=(
                    int(max_runtime_seconds)
                    if max_runtime_seconds is not None else None
                ),
                skills=skills,
                initial_status=str(initial_status) if initial_status is not None else None,
                board=board,
                created_by=os.environ.get("HERMES_PROFILE") or "worker",
                session_id=session_id,
                goal_id=args.get("goal"),
                workstream_id=args.get("workstream"),
                stage_key=args.get("stage"),
                action_key=args.get("action"),
                funnel_data=funnel_data,
            )
            new_task = kb.get_task(conn, new_tid)
            return _ok(
                task_id=new_tid,
                status=new_task.status if new_task else None,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_create: {e}")
    except Exception as e:
        logger.exception("kanban_create failed")
        return tool_error(f"kanban_create: {e}")


def _handle_unblock(args: dict, **kw) -> str:
    """Transition a blocked task back to ready."""
    guard = _require_orchestrator_tool("kanban_unblock")
    if guard:
        return guard
    tid = args.get("task_id")
    if not tid:
        return tool_error("task_id is required")
    ownership_err = _enforce_worker_task_ownership(str(tid))
    if ownership_err:
        return ownership_err
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            ok = kb.unblock_task(conn, str(tid))
            if not ok:
                return tool_error(f"could not unblock {tid} (not blocked or unknown)")
            return _ok(task_id=str(tid), status="ready")
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_unblock: {e}")
    except Exception as e:
        logger.exception("kanban_unblock failed")
        return tool_error(f"kanban_unblock: {e}")


def _handle_link(args: dict, **kw) -> str:
    """Add a parent→child dependency edge after the fact."""
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    if not parent_id or not child_id:
        return tool_error("both parent_id and child_id are required")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)
            return _ok(parent_id=parent_id, child_id=child_id)
        finally:
            conn.close()
    except ValueError as e:
        # Covers cycle + self-parent rejections
        return tool_error(f"kanban_link: {e}")
    except Exception as e:
        logger.exception("kanban_link failed")
        return tool_error(f"kanban_link: {e}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_DESC_TASK_ID_DEFAULT = (
    "Task id. If omitted, defaults to HERMES_KANBAN_TASK from the env "
    "(the task the dispatcher spawned you to work on)."
)

_DESC_BOARD = (
    "Kanban board slug to target. When omitted, the call resolves the "
    "active board the usual way: HERMES_KANBAN_DB env → "
    "HERMES_KANBAN_BOARD env → the 'current' symlink under the kanban "
    "home → 'default'. Dispatcher-spawned workers are DB/board pinned; "
    "an explicit board must match that pinned board and cannot be used "
    "to relabel a task from another board."
)


def _board_schema_prop() -> dict[str, str]:
    """Schema fragment for the optional ``board`` parameter.

    Centralised so a future tweak to the description / validation hint
    only has to land in one place.
    """
    return {"type": "string", "description": _DESC_BOARD}

KANBAN_SHOW_SCHEMA = {
    "name": "kanban_show",
    "description": (
        "Read a task's full state — title, body, assignee, parent task "
        "handoffs, your prior attempts on this task if any, comments, "
        "and recent events. Use this to (re)orient yourself before "
        "starting work, especially on retries. The response includes a "
        "pre-formatted ``worker_context`` string suitable for inclusion "
        "verbatim in your reasoning."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_LIST_SCHEMA = {
    "name": "kanban_list",
    "description": (
        "List Kanban task summaries so an orchestrator profile can discover "
        "work to route. Supports the same core filters as the CLI: assignee, "
        "status, tenant, include_archived, and limit. Returns compact rows "
        "with ids, title, status, assignee, priority, parent/child ids, and "
        "counts. Bounded to 50 rows by default, 200 max, with truncation "
        "metadata. Also recomputes ready tasks before listing, matching the "
        "CLI. Orchestrator-only — dispatcher-spawned task workers never see "
        "this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "assignee": {
                "type": "string",
                "description": "Optional assignee/profile filter.",
            },
            "status": {
                "type": "string",
                "enum": [
                    "triage", "todo", "scheduled", "ready", "running",
                    "watching", "blocked", "review", "done", "archived",
                ],
                "description": "Optional task status filter.",
            },
            "tenant": {
                "type": "string",
                "description": "Optional tenant/project namespace filter.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include archived tasks. Defaults to false.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum rows to return (default 50, max 200).",
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_FUNNEL_SCHEMA = {
    "name": "kanban_funnel",
    "description": (
        "Return the semantic funnel read-model for the board: cards grouped "
        "by goal, workstream, stage, and action with lifecycle counts, "
        "dependency edges, blockers, run outcomes, artifacts, proof, and "
        "compound entity/substate signals. This gives optimizer/orchestrator profiles a goal-flow "
        "view without relying on title keywords or fixed columns. "
        "Orchestrator-only — dispatcher-spawned task workers never see this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "include_archived": {
                "type": "boolean",
                "description": "Include archived tasks. Defaults to false.",
            },
            "limit_cards_per_stage": {
                "type": "integer",
                "description": "Maximum card summaries embedded per stage. Defaults to 20.",
            },
            "limit_entities_per_stage": {
                "type": "integer",
                "description": "Maximum compound entity summaries embedded per stage. Defaults to 50.",
            },
            "goal": {"type": "string", "description": "Filter by resolved goal id."},
            "workstream": {"type": "string", "description": "Filter by resolved workstream id."},
            "stage": {"type": "string", "description": "Filter by resolved stage key."},
            "action": {"type": "string", "description": "Filter by resolved action key."},
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_LIST_BOARDS_SCHEMA = {
    "name": "kanban_list_boards",
    "description": (
        "Enumerate the durable kanban boards that already exist (metadata "
        "only — slug, name, description, objective, launch phase, runtime, "
        "archived). Use this to answer 'do we already have a board for this?' "
        "before proposing a new one. The phantom unconfigured 'default' board "
        "is excluded, so an empty list means no standing board exists yet."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "include_archived": {
                "type": "boolean",
                "description": "Include archived boards. Defaults to false.",
            },
        },
        "required": [],
    },
}

KANBAN_MATCH_BOARD_SCHEMA = {
    "name": "kanban_match_board",
    "description": (
        "Given a free-text description of work, return the existing boards as "
        "ranked candidates (with a coarse lexical match_score) so you can pick "
        "the right board to route to. This tool only supplies enumerated board "
        "metadata — YOU decide the semantic match. If a board clearly fits, "
        "work in it; if none fit, clarify with the owner then propose a new "
        "board via kanban_business_launch_review(create_if_missing=true)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "Free-text description of the work to route.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include archived boards as candidates. Defaults to false.",
            },
        },
        "required": ["goal"],
    },
}

KANBAN_BOARD_LAUNCH_STATUS_SCHEMA = {
    "name": "kanban_board_launch_status",
    "description": (
        "Read a board's launch phase, contract version, business contract "
        "readiness, missing clarity fields, and follow-up questions. Use before "
        "creating execution cards for a new business/runtime board."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_BUSINESS_LAUNCH_REVIEW_SCHEMA = {
    "name": "kanban_business_launch_review",
    "description": (
        "Review and optionally store a draft business-runtime contract before "
        "launching a board. If the owner gives a vague business idea, call this "
        "with rough_goal and create_if_missing=true. For a rough goal, the tool "
        "returns a mandatory model-generated launch-intake prompt instead of "
        "hardcoded questions; use that prompt to ask tailored owner clarification "
        "questions in your next response before drafting the contract. When the "
        "owner answers, call this with intake_answers so the answer quality is "
        "assessed recursively before any contract draft. Set "
        "approve=true only after the owner has approved a launch-ready contract "
        "through an external approval token; otherwise the board remains in "
        "contract_review and dispatch is gated."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "board": _board_schema_prop(),
            "rough_goal": {
                "type": "string",
                "description": "Owner's rough business goal or launch request.",
            },
            "intake_answers": {
                "type": ["object", "array", "string"],
                "description": (
                    "Owner answers to the generated launch-intake clarification "
                    "questions. Use this before drafting the contract so Hermes "
                    "can assess answer quality and ask recursive follow-ups if needed."
                ),
            },
            "contract": {
                "type": "object",
                "description": (
                    "Draft business contract with objective, runtime, workflow, "
                    "entities, event loops, and approval/proof policy."
                ),
            },
            "create_if_missing": {
                "type": "boolean",
                "description": "Create the board if it does not exist. Defaults to false.",
            },
            "approve": {
                "type": "boolean",
                "description": (
                    "Activate dispatch only if the contract is launch-ready and "
                    "approval_token matches the exact contract/version. Defaults to false."
                ),
            },
            "approval_token": {
                "type": "string",
                "description": (
                    "Required with approve=true for a launch-ready contract. "
                    "This one-time token must be minted outside model-callable "
                    "launch tools from explicit owner approval."
                ),
            },
            "approved_by": {
                "type": "string",
                "description": "Legacy approver metadata; does not activate without approval_token.",
            },
            "approval_evidence": {
                "type": "object",
                "description": (
                    "Legacy approval evidence metadata; does not activate without approval_token."
                ),
            },
            "approval_reason": {
                "type": "string",
                "description": "Optional rationale for the owner approval.",
            },
            "name": {"type": "string", "description": "Optional display name for a new board."},
            "description": {"type": "string", "description": "Optional board description."},
        },
        "required": [],
    },
}

KANBAN_CONTRACT_AMENDMENT_PROPOSE_SCHEMA = {
    "name": "kanban_contract_amendment_propose",
    "description": (
        "Propose a versioned patch to a board's business contract. This does "
        "not change the active runtime; it stores a pending amendment, validates "
        "the candidate contract, and returns any missing clarity questions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "board": _board_schema_prop(),
            "patch": {
                "type": "object",
                "description": "Deep-merge patch for objective/runtime/workflow/entities/policy.",
            },
            "reason": {
                "type": "string",
                "description": "Why the board contract needs to change.",
            },
            "risk": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "Estimated operational risk for the amendment.",
            },
        },
        "required": ["board", "patch", "reason"],
    },
}

KANBAN_CONTRACT_AMENDMENT_APPLY_SCHEMA = {
    "name": "kanban_contract_amendment_apply",
    "description": (
        "Apply a pending board contract amendment after owner-token approval. The old "
        "contract is preserved in history, contract_version increments, and "
        "the exact amendment/version must be launch-ready and approved with "
        "approval_token."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "board": _board_schema_prop(),
            "amendment_id": {"type": "string", "description": "Pending amendment id."},
            "approval_token": {
                "type": "string",
                "description": (
                    "Required for ready amendments. This one-time token must be "
                    "minted outside model-callable launch tools from explicit owner approval."
                ),
            },
            "approved_by": {
                "type": "string",
                "description": "Legacy approver metadata; does not activate without approval_token.",
            },
            "approval_evidence": {
                "type": "object",
                "description": "Legacy approval evidence metadata; does not activate without approval_token.",
            },
            "activate": {
                "type": "boolean",
                "description": "Set launch_phase=active when readiness passes. Defaults to true.",
            },
        },
        "required": ["board", "amendment_id"],
    },
}

KANBAN_COMPLETE_SCHEMA = {
    "name": "kanban_complete",
    "description": (
        "Mark your current task done with a structured handoff for "
        "downstream workers and humans. Prefer ``summary`` for a "
        "human-readable 1-3 sentence description of what you did; put "
        "machine-readable facts in ``metadata`` (changed_files, "
        "tests_run, decisions, findings, etc). At least one of "
        "``summary`` or ``result`` is required. If you created new "
        "tasks via ``kanban_create`` during this run, list their ids "
        "in ``created_cards`` — the kernel verifies them so phantom "
        "references are caught before they leak into downstream "
        "automation. If you produced deliverable files (charts, PDFs, "
        "spreadsheets, generated images), list their absolute paths "
        "in ``artifacts`` — the gateway notifier will upload them as "
        "native attachments to the human who subscribed to the task, "
        "so the deliverable lands in their chat alongside the summary "
        "instead of being a path they have to fetch by hand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "summary": {
                "type": "string",
                "description": (
                    "Human-readable handoff, 1-3 sentences. Appears in "
                    "Run History on the dashboard and in downstream "
                    "workers' context."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Free-form dict of structured facts about this "
                    "attempt — changed_files, tests_run, findings, "
                    "artifacts, proof, outcomes, decisions, capabilities, "
                    "entities/funnel_entities for compound stages, "
                    "or other machine-readable handoff facts. Surfaced to "
                    "downstream workers and the funnel read-model alongside ``summary``."
                ),
            },
            "result": {
                "type": "string",
                "description": (
                    "Short result log line (legacy field, maps to "
                    "task.result). Use ``summary`` instead when "
                    "possible; this exists for compatibility with "
                    "callers that still set --result on the CLI."
                ),
            },
            "created_cards": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional structured manifest of task ids you "
                    "created via ``kanban_create`` during this run. "
                    "The kernel verifies each id exists and was "
                    "created by this worker's profile; any phantom "
                    "id blocks the completion with an error listing "
                    "what went wrong (auditable in the task's events). "
                    "Only list ids you got back from a successful "
                    "``kanban_create`` call — do not invent or "
                    "remember ids from prose. Omit the field if you "
                    "did not create any cards."
                ),
            },
            "artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of absolute paths to deliverable "
                    "files you produced during this run — generated "
                    "charts, PDFs, spreadsheets, images, archives. "
                    "Examples: [\"/tmp/q3-revenue.png\", "
                    "\"/tmp/report.pdf\"]. The gateway notifier "
                    "uploads each path as a native attachment to the "
                    "subscribed chat (images embed inline, everything "
                    "else uploads as a file) so the deliverable "
                    "lands with the completion notification. Skip "
                    "intermediate scratch files and references that "
                    "are not the deliverable. The path must exist "
                    "on disk when the notifier runs; missing files "
                    "are silently skipped."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_BLOCK_SCHEMA = {
    "name": "kanban_block",
    "description": (
        "Transition the task to blocked because you need human input "
        "to proceed. ``reason`` will be shown to the human on the "
        "board and included in context when someone unblocks you. "
        "Use for genuine blockers only — don't block on things you can "
        "resolve yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "reason": {
                "type": "string",
                "description": (
                    "What you need answered, in one or two sentences. "
                    "Don't paste the whole conversation; the human has "
                    "the board and can ask follow-ups via comments."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["reason"],
    },
}

KANBAN_WATCH_SCHEMA = {
    "name": "kanban_watch",
    "description": (
        "Park your current task in healthy watching state until a future "
        "event wakes it. Use this instead of blocking when no human decision "
        "is needed and the right next step is to wait for an inbound event, "
        "timer, dependency, webhook, or other route. The task leaves the "
        "running claim, records a watch route, and will resume as the chosen "
        "wake_status when kanban_trigger fires."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "trigger_type": {
                "type": "string",
                "description": (
                    "Route family that wakes the task, e.g. inbound_event, "
                    "timer, dependency, webhook, or manual. Use stable "
                    "machine-readable keys, not prose."
                ),
            },
            "trigger_key": {
                "type": "string",
                "description": "Optional route key/channel/id used to match the future event.",
            },
            "reason": {
                "type": "string",
                "description": "Human-readable waiting reason shown in the funnel/UI.",
            },
            "wake_status": {
                "type": "string",
                "enum": ["ready", "todo", "blocked", "review"],
                "description": "Lifecycle status to set after the route fires. Defaults to ready.",
            },
            "payload": {
                "type": "object",
                "description": "Optional structured watch metadata for matching/routing.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["trigger_type"],
    },
}

KANBAN_TRIGGER_SCHEMA = {
    "name": "kanban_trigger",
    "description": (
        "Wake active watch routes by route id, task id, or trigger_type/key. "
        "This is the event ingress surface for orchestrators/optimizers: "
        "when an inbound event, timer, dependency, or webhook arrives, call "
        "this to move matching watching cards back into executable lifecycle "
        "state. Orchestrator-only — task workers do not see this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "route_id": {"type": "integer", "description": "Specific watch route id to trigger."},
            "task_id": {"type": "string", "description": "Wake active routes for a specific task."},
            "trigger_type": {"type": "string", "description": "Route family to match."},
            "trigger_key": {"type": "string", "description": "Optional route key/channel/id to match."},
            "payload": {"type": "object", "description": "Structured event payload recorded on the route/event."},
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_TRANSITION_SCHEMA = {
    "name": "kanban_transition",
    "description": (
        "Move a card to another semantic workflow stage after evidence "
        "validation. If the board workflow declares exit criteria, every "
        "required evidence key must be present before the transition is "
        "accepted. This changes semantic stage/action fields only; lifecycle "
        "dispatch still runs through status/assignee/dependencies. "
        "Orchestrator-only — task workers do not see this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task to move."},
            "to_stage": {"type": "string", "description": "Target workflow stage key."},
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Evidence keys proving the transition. Required keys come from board workflow exit_criteria.",
            },
            "action": {"type": "string", "description": "Optional new semantic action key in the target stage."},
            "board": _board_schema_prop(),
        },
        "required": ["task_id", "to_stage"],
    },
}

KANBAN_HEARTBEAT_SCHEMA = {
    "name": "kanban_heartbeat",
    "description": (
        "Signal that you're still alive during a long operation "
        "(training, encoding, large crawls). Call every few minutes so "
        "humans see liveness separately from PID checks. Pure side "
        "effect — no work changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional short note describing current progress. "
                    "Shown in the event log."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMMENT_SCHEMA = {
    "name": "kanban_comment",
    "description": (
        "Append a comment to a task's thread. Use for durable notes "
        "that should outlive this run (questions for the next worker, "
        "partial findings, rationale). Ephemeral reasoning doesn't "
        "belong here — use your normal response instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task id. Required (may be your own task or "
                    "another's — comment threads are per-task)."
                ),
            },
            "body": {
                "type": "string",
                "description": "Markdown-supported comment body.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id", "body"],
    },
}

KANBAN_CREATE_SCHEMA = {
    "name": "kanban_create",
    "description": (
        "Create a new kanban task, optionally as a child of the current "
        "one (pass the current task id in ``parents``). Used by "
        "orchestrator workers to fan out — decompose work into child "
        "tasks with specific assignees, link them into a pipeline, "
        "then complete your own task. The dispatcher picks up the new "
        "tasks on its next tick and spawns the assigned profiles."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short task title (required).",
            },
            "assignee": {
                "type": "string",
                "description": (
                    "Profile name that should execute this task "
                    "(e.g. 'researcher-a', 'reviewer', 'writer'). "
                    "Required — tasks without an assignee are never "
                    "dispatched."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "Opening post: full spec, acceptance criteria, "
                    "links. The assigned worker reads this as part of "
                    "its context."
                ),
            },
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Parent task ids. The new task stays in 'todo' "
                    "until every parent reaches 'done'; then it "
                    "auto-promotes to 'ready'. Typical fan-in: list "
                    "all the researcher task ids when creating a "
                    "synthesizer task."
                ),
            },
            "tenant": {
                "type": "string",
                "description": (
                    "Optional namespace for multi-project isolation. "
                    "Defaults to HERMES_TENANT env if set."
                ),
            },
            "priority": {
                "type": "integer",
                "description": (
                    "Dispatcher tiebreaker. Higher = picked sooner "
                    "when multiple ready tasks share an assignee."
                ),
            },
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
                "description": (
                    "Workspace flavor: 'scratch' (fresh tmp dir, "
                    "default), 'dir' (shared directory, requires "
                    "absolute workspace_path), 'worktree' (git worktree)."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": (
                    "Absolute path for 'dir' or 'worktree' workspace. "
                    "Relative paths are rejected at dispatch."
                ),
            },
            "triage": {
                "type": "boolean",
                "description": (
                    "If true, task lands in 'triage' instead of 'todo' "
                    "— a specifier profile is expected to flesh out "
                    "the body before work starts."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "If a non-archived task with this key already "
                    "exists, return that task's id instead of creating "
                    "a duplicate. Useful for retry-safe automation."
                ),
            },
            "max_runtime_seconds": {
                "type": "integer",
                "description": (
                    "Per-task runtime cap. When exceeded, the "
                    "dispatcher SIGTERMs the worker and re-queues the "
                    "task with outcome='timed_out'."
                ),
            },
            "initial_status": {
                "type": "string",
                "enum": ["running", "blocked"],
                "description": (
                    "Initial card status. Use 'blocked' for tasks that "
                    "require immediate human ops (R3 gate) to skip the "
                    "brief running-to-blocked transition. Defaults to "
                    "'running', which preserves the usual dispatch path."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Skill names to force-load into the dispatched "
                    "worker (in addition to the built-in kanban-worker "
                    "skill). Use this to pin a task to a specialist "
                    "context — e.g. ['translation'] for a translation "
                    "task, ['github-code-review'] for a reviewer task. "
                    "The names must match skills installed on the "
                    "assignee's profile."
                ),
            },
            "goal": {
                "type": "string",
                "description": "Semantic funnel goal id for optimizer/UI grouping.",
            },
            "workstream": {
                "type": "string",
                "description": "Semantic funnel workstream id under the goal.",
            },
            "stage": {
                "type": "string",
                "description": "Semantic funnel stage key for this card.",
            },
            "action": {
                "type": "string",
                "description": "Semantic funnel action key within the stage.",
            },
            "funnel_data": {
                "type": "object",
                "description": "Optional structured funnel facts for the read-model.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["title", "assignee"],
    },
}

KANBAN_UNBLOCK_SCHEMA = {
    "name": "kanban_unblock",
    "description": (
        "Move a blocked Kanban task back to ready. Orchestrator-only — only "
        "profiles with the kanban toolset can unblock routed work; "
        "dispatcher-spawned task workers never see this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Blocked task id to return to ready.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id"],
    },
}

KANBAN_LINK_SCHEMA = {
    "name": "kanban_link",
    "description": (
        "Add a parent→child dependency edge after both tasks already "
        "exist. The child won't promote to 'ready' until all parents "
        "are 'done'. Cycles and self-links are rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "parent_id": {"type": "string", "description": "Parent task id."},
            "child_id":  {"type": "string", "description": "Child task id."},
            "board": _board_schema_prop(),
        },
        "required": ["parent_id", "child_id"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="kanban_show",
    toolset="kanban",
    schema=KANBAN_SHOW_SCHEMA,
    handler=_handle_show,
    check_fn=_check_kanban_mode,
    emoji="📋",
)

registry.register(
    name="kanban_list",
    toolset="kanban",
    schema=KANBAN_LIST_SCHEMA,
    handler=_handle_list,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="📋",
)

registry.register(
    name="kanban_funnel",
    toolset="kanban",
    schema=KANBAN_FUNNEL_SCHEMA,
    handler=_handle_funnel,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="🧭",
)

registry.register(
    name="kanban_list_boards",
    toolset=KANBAN_LAUNCH_INTAKE_TOOLSET,
    schema=KANBAN_LIST_BOARDS_SCHEMA,
    handler=_handle_list_boards,
    check_fn=_check_kanban_launch_intake_mode,
    emoji="🗂",
)

registry.register(
    name="kanban_match_board",
    toolset=KANBAN_LAUNCH_INTAKE_TOOLSET,
    schema=KANBAN_MATCH_BOARD_SCHEMA,
    handler=_handle_match_board,
    check_fn=_check_kanban_launch_intake_mode,
    emoji="🧭",
)

registry.register(
    name="kanban_board_launch_status",
    toolset=KANBAN_LAUNCH_INTAKE_TOOLSET,
    schema=KANBAN_BOARD_LAUNCH_STATUS_SCHEMA,
    handler=_handle_board_launch_status,
    check_fn=_check_kanban_launch_intake_mode,
    emoji="🧭",
)

registry.register(
    name="kanban_business_launch_review",
    toolset=KANBAN_LAUNCH_INTAKE_TOOLSET,
    schema=KANBAN_BUSINESS_LAUNCH_REVIEW_SCHEMA,
    handler=_handle_business_launch_review,
    check_fn=_check_kanban_launch_intake_mode,
    emoji="🧭",
)

registry.register(
    name="kanban_contract_amendment_propose",
    toolset=KANBAN_LAUNCH_INTAKE_TOOLSET,
    schema=KANBAN_CONTRACT_AMENDMENT_PROPOSE_SCHEMA,
    handler=_handle_contract_amendment_propose,
    check_fn=_check_kanban_launch_intake_mode,
    emoji="🧭",
)

registry.register(
    name="kanban_contract_amendment_apply",
    toolset=KANBAN_LAUNCH_INTAKE_TOOLSET,
    schema=KANBAN_CONTRACT_AMENDMENT_APPLY_SCHEMA,
    handler=_handle_contract_amendment_apply,
    check_fn=_check_kanban_launch_intake_mode,
    emoji="🧭",
)

registry.register(
    name="kanban_complete",
    toolset="kanban",
    schema=KANBAN_COMPLETE_SCHEMA,
    handler=_handle_complete,
    check_fn=_check_kanban_mode,
    emoji="✔",
)

registry.register(
    name="kanban_block",
    toolset="kanban",
    schema=KANBAN_BLOCK_SCHEMA,
    handler=_handle_block,
    check_fn=_check_kanban_mode,
    emoji="⏸",
)

registry.register(
    name="kanban_watch",
    toolset="kanban",
    schema=KANBAN_WATCH_SCHEMA,
    handler=_handle_watch,
    check_fn=_check_kanban_mode,
    emoji="👀",
)

registry.register(
    name="kanban_trigger",
    toolset="kanban",
    schema=KANBAN_TRIGGER_SCHEMA,
    handler=_handle_trigger,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="🔔",
)

registry.register(
    name="kanban_transition",
    toolset="kanban",
    schema=KANBAN_TRANSITION_SCHEMA,
    handler=_handle_transition,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="🧭",
)

registry.register(
    name="kanban_heartbeat",
    toolset="kanban",
    schema=KANBAN_HEARTBEAT_SCHEMA,
    handler=_handle_heartbeat,
    check_fn=_check_kanban_mode,
    emoji="💓",
)

registry.register(
    name="kanban_comment",
    toolset="kanban",
    schema=KANBAN_COMMENT_SCHEMA,
    handler=_handle_comment,
    check_fn=_check_kanban_mode,
    emoji="💬",
)

registry.register(
    name="kanban_create",
    toolset="kanban",
    schema=KANBAN_CREATE_SCHEMA,
    handler=_handle_create,
    check_fn=_check_kanban_mode,
    emoji="➕",
)

registry.register(
    name="kanban_unblock",
    toolset="kanban",
    schema=KANBAN_UNBLOCK_SCHEMA,
    handler=_handle_unblock,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="▶",
)

registry.register(
    name="kanban_link",
    toolset="kanban",
    schema=KANBAN_LINK_SCHEMA,
    handler=_handle_link,
    check_fn=_check_kanban_mode,
    emoji="🔗",
)
