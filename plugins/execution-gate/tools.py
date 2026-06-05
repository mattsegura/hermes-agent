"""
Execution Gate Tools

Hermes tool interface for the gated execution system.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from .gate import get_gate, ProofType, ValidationLayer, CriteriaVisibility


# ---------------------------------------------------------------------------
# Intake Helpers
# ---------------------------------------------------------------------------

_SIDE_EFFECT_RISKS = {
    "none",
    "internal",
    "external_reversible",
    "external_irreversible",
    "financial",
    "legal_compliance",
    "destructive",
}

_GENERIC_TEXT = {
    "done",
    "complete",
    "completed",
    "success",
    "works",
    "working",
    "good",
    "looks good",
    "task complete",
    "task completed",
}

_ENTITY_STOPWORDS = {
    "Add",
    "Build",
    "Call",
    "Check",
    "Compare",
    "Create",
    "Draft",
    "Explain",
    "Fix",
    "Go",
    "Implement",
    "Inspect",
    "Report",
    "Return",
    "Review",
    "Run",
    "Test",
    "Update",
    "Use",
}

_KNOWN_ENTITY_TERMS = {
    "aws": "AWS",
    "crm": "CRM",
    "github": "GitHub",
    "hermes": "Hermes",
    "humanless": "Humanless",
    "stripe": "Stripe",
    "supabase": "Supabase",
    "vercel": "Vercel",
}


def _string_list(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a string or list of strings")

    seen = set()
    result: List[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        if text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _required_text(args: dict, field: str, max_len: int) -> str:
    value = args.get(field)
    if value is None:
        raise ValueError(f"{field} is required")
    text = str(value)
    if not text.strip():
        raise ValueError(f"{field} is required")
    if len(text) > max_len:
        raise ValueError(f"{field} must be {max_len} characters or fewer")
    if any(ord(ch) < 9 or (13 < ord(ch) < 32) for ch in text):
        raise ValueError(f"{field} contains unsupported control characters")
    return text


def _optional_text(args: dict, field: str, max_len: int) -> str:
    value = args.get(field)
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > max_len:
        raise ValueError(f"{field} must be {max_len} characters or fewer")
    if any(ord(ch) < 9 or (13 < ord(ch) < 32) for ch in text):
        raise ValueError(f"{field} contains unsupported control characters")
    return text


def _format_intake_context(envelope: Dict[str, Any]) -> str:
    sections = [
        ("ORIGINAL USER PROMPT", envelope["original_prompt"]),
        ("CUSTOM CAPABILITY", envelope["custom_capability"]),
        ("SUCCESS CRITERIA", "\n".join(f"- {item}" for item in envelope["success_criteria"])),
    ]
    optional_lists = [
        ("ENTITIES", envelope.get("entities") or []),
        ("UNKNOWN/BLOCKERS", envelope.get("unknowns") or []),
        ("CONSTRAINTS", envelope.get("constraints") or []),
        ("EXTERNAL ACTIONS", envelope.get("external_actions") or []),
    ]
    for title, items in optional_lists:
        if items:
            sections.append((title, "\n".join(f"- {item}" for item in items)))
    if envelope.get("context"):
        sections.append(("ADDITIONAL CONTEXT", envelope["context"]))
    sections.append(("SIDE EFFECT RISK", str(envelope["side_effect_risk"])))
    return "\n\n".join(f"{title}:\n{body}" for title, body in sections)


def _looks_generic(text: str) -> bool:
    normalized = " ".join(str(text).strip().lower().split())
    return not normalized or normalized in _GENERIC_TEXT or len(normalized) < 8


def _extract_prompt_entities(prompt: str) -> List[str]:
    import re

    found: List[str] = []
    seen = set()

    for raw in re.findall(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+", prompt):
        if raw.lower() not in seen:
            seen.add(raw.lower())
            found.append(raw)

    for raw in re.findall(r"\b[A-Z][A-Za-z0-9_.-]{1,}\b|\b[A-Z]{2,}\b", prompt):
        if raw in _ENTITY_STOPWORDS:
            continue
        if raw.lower() not in seen:
            seen.add(raw.lower())
            found.append(raw)

    lowered = prompt.lower()
    for key, label in _KNOWN_ENTITY_TERMS.items():
        if key in lowered and label.lower() not in seen:
            seen.add(label.lower())
            found.append(label)

    return found[:12]


def _missing_entities(expected: List[str], captured: List[str]) -> List[str]:
    captured_lower = {item.lower() for item in captured}
    return [item for item in expected if item.lower() not in captured_lower]


def _risk_hints(text: str) -> List[str]:
    import re

    lowered = text.lower()
    hints: List[str] = []
    patterns = {
        "financial": r"\b(issue|process|execute|send|submit|approve|make|perform)\s+(a\s+)?(refund|payment|charge|transfer|payout)\b|\brefund\s+(the\s+)?customer\b|\bcharge\s+(the\s+)?(customer|card)\b",
        "external_reversible": r"\b(send|email|message|post|publish|deploy|invite|notify)\b",
        "external_irreversible": r"\b(cancel|terminate|close)\s+(account|subscription|contract|service)\b",
        "destructive": r"\b(delete|drop|destroy|purge|remove)\b",
        "legal_compliance": r"\b(sign|file|submit)\s+(contract|legal|tax|compliance)\b",
    }
    for risk, pattern in patterns.items():
        if re.search(pattern, lowered):
            hints.append(risk)
    return hints


def _envelope_from_verify_args(args: dict) -> Dict[str, Any]:
    raw = args.get("task_envelope") or args.get("intake")
    if isinstance(raw, dict) and isinstance(raw.get("task_envelope"), dict):
        raw = raw["task_envelope"]
    elif isinstance(raw, dict) and isinstance(raw.get("routing_input"), dict):
        merged = dict(raw.get("task_envelope") or {})
        routing_input = raw["routing_input"]
        for key in ("goal", "custom_capability", "required_capabilities", "toolsets", "skills", "context"):
            if key not in merged and key in routing_input:
                merged[key] = routing_input[key]
        raw = merged
    elif raw is None:
        raw = args
    if not isinstance(raw, dict):
        raise ValueError("task_envelope must be an object")
    return dict(raw)


def _approval_required(side_effect_risk: str) -> bool:
    return side_effect_risk in {
        "external_reversible",
        "external_irreversible",
        "financial",
        "legal_compliance",
        "destructive",
    }


def _conversion_id_from_envelope(envelope: Dict[str, Any]) -> str:
    prompt_hash = str(envelope.get("original_prompt_sha256") or "").strip()
    if prompt_hash:
        return prompt_hash[:16]
    raw = "|".join(
        str(envelope.get(key) or "")
        for key in ("original_prompt", "goal", "custom_capability")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _ledger_dir() -> Path:
    override = os.environ.get("EXECUTION_GATE_LEDGER_DIR")
    if override:
        path = Path(override).expanduser()
    else:
        path = get_gate().state_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ledger_path() -> Path:
    return _ledger_dir() / "conversion_ledger.jsonl"


def _safe_stage(stage: str) -> str:
    stage = str(stage or "").strip()
    if not stage:
        return "unknown"
    return "".join(ch for ch in stage if ch.isalnum() or ch in "_.-")[:80] or "unknown"


def _event_projection(event: Dict[str, Any]) -> Dict[str, Any]:
    envelope = event.get("task_envelope") if isinstance(event.get("task_envelope"), dict) else {}
    custom_capability = str(event.get("custom_capability") or envelope.get("custom_capability") or "").strip()
    model_route = event.get("model_route") if isinstance(event.get("model_route"), dict) else {}
    return {
        "stage": _safe_stage(event.get("stage")),
        "outcome": str(event.get("outcome") or "").strip() or "unknown",
        "conversion_id": str(event.get("conversion_id") or _conversion_id_from_envelope(envelope))[:80],
        "original_prompt_sha256": str(event.get("original_prompt_sha256") or envelope.get("original_prompt_sha256") or "").strip(),
        "custom_capability": custom_capability,
        "required_capabilities": _string_list(event.get("required_capabilities") or envelope.get("required_capabilities"), "required_capabilities"),
        "toolsets": _string_list(event.get("toolsets") or envelope.get("toolsets"), "toolsets"),
        "entities": _string_list(event.get("entities") or envelope.get("entities"), "entities"),
        "side_effect_risk": str(event.get("side_effect_risk") or envelope.get("side_effect_risk") or "").strip(),
        "worker_profile": str(event.get("worker_profile") or event.get("selected_worker") or "").strip(),
        "model_key": str(event.get("model_key") or model_route.get("key") or "").strip(),
        "model_provider": str(event.get("model_provider") or event.get("provider") or model_route.get("provider") or "").strip(),
        "model": str(event.get("model") or model_route.get("model") or "").strip(),
        "quality_score": event.get("quality_score"),
        "proof_quality": event.get("proof_quality"),
        "retry_count": event.get("retry_count"),
        "blockers": _string_list(event.get("blockers"), "blockers"),
        "artifact_count": event.get("artifact_count"),
        "ok": event.get("ok"),
        "error": str(event.get("error") or "").strip(),
    }


def record_conversion_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Append a local conversion-funnel event. Best effort, never raises."""

    try:
        projected = _event_projection(event)
        now = time.time()
        row = {
            "event_id": hashlib.sha256(
                f"{now}:{projected['stage']}:{projected['conversion_id']}:{projected['outcome']}".encode("utf-8")
            ).hexdigest()[:20],
            "ts": now,
            "session_id": os.environ.get("HERMES_SESSION_ID") or os.environ.get("HERMES_SESSION") or "default",
            "profile": os.environ.get("HERMES_PROFILE_NAME") or os.environ.get("HERMES_PROFILE") or "default",
            **projected,
        }
        _ledger_path().write_text("", encoding="utf-8") if not _ledger_path().exists() else None
        with _ledger_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return {"ok": True, "event": row, "path": str(_ledger_path())}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _read_ledger(limit: int = 1000) -> List[Dict[str, Any]]:
    path = _ledger_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    rows: List[Dict[str, Any]] = []
    for line in lines[-max(1, limit):]:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except json.JSONDecodeError:
            continue
    return rows


def _ledger_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    stage_counts: Dict[str, int] = {}
    outcome_counts: Dict[str, int] = {}
    worker_counts: Dict[str, int] = {}
    model_route_counts: Dict[str, int] = {}
    provider_counts: Dict[str, int] = {}
    capability_counts: Dict[str, int] = {}
    blocker_counts: Dict[str, int] = {}
    quality_scores: List[float] = []

    for row in rows:
        stage_counts[row.get("stage") or "unknown"] = stage_counts.get(row.get("stage") or "unknown", 0) + 1
        outcome_counts[row.get("outcome") or "unknown"] = outcome_counts.get(row.get("outcome") or "unknown", 0) + 1
        worker = row.get("worker_profile")
        if worker:
            worker_counts[worker] = worker_counts.get(worker, 0) + 1
        provider = row.get("model_provider")
        model = row.get("model")
        model_key = row.get("model_key")
        if provider:
            provider_counts[provider] = provider_counts.get(provider, 0) + 1
        if provider or model:
            route = "/".join(item for item in (str(model_key or ""), str(provider or ""), str(model or "")) if item)
            model_route_counts[route] = model_route_counts.get(route, 0) + 1
        capability = row.get("custom_capability")
        if capability:
            capability_counts[capability] = capability_counts.get(capability, 0) + 1
        for blocker in row.get("blockers") or []:
            blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
        score = row.get("quality_score")
        if isinstance(score, (int, float)):
            quality_scores.append(float(score))

    return {
        "total_events": len(rows),
        "stage_counts": stage_counts,
        "outcome_counts": outcome_counts,
        "worker_counts": worker_counts,
        "model_route_counts": model_route_counts,
        "provider_counts": provider_counts,
        "custom_capability_counts": capability_counts,
        "blocker_counts": blocker_counts,
        "avg_quality_score": round(sum(quality_scores) / len(quality_scores), 2) if quality_scores else None,
    }


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

def handle_gate_intake(args: dict, **kw) -> str:
    """Capture a lossless task envelope before routing or delegation."""

    try:
        original_prompt = _required_text(args, "original_prompt", 20000)
        goal = _required_text(args, "goal", 2000).strip()
        custom_capability = _required_text(args, "custom_capability", 1000).strip()
        success_criteria = _string_list(args.get("success_criteria"), "success_criteria")
        if not success_criteria:
            return json.dumps({"ok": False, "error": "success_criteria is required"})

        required_capabilities = _string_list(args.get("required_capabilities"), "required_capabilities")
        toolsets = _string_list(args.get("toolsets"), "toolsets")
        skills = _string_list(args.get("skills"), "skills")
        entities = _string_list(args.get("entities"), "entities")
        unknowns = _string_list(args.get("unknowns"), "unknowns")
        constraints = _string_list(args.get("constraints"), "constraints")
        external_actions = _string_list(args.get("external_actions"), "external_actions")
        context = _optional_text(args, "context", 12000)

        side_effect_risk = str(args.get("side_effect_risk") or "none").strip()
        if side_effect_risk not in _SIDE_EFFECT_RISKS:
            return json.dumps({
                "ok": False,
                "error": f"side_effect_risk must be one of {sorted(_SIDE_EFFECT_RISKS)}",
            })
        if external_actions and side_effect_risk == "none":
            return json.dumps({
                "ok": False,
                "error": "external_actions require side_effect_risk other than none",
            })

        unknowns_blocking = bool(args.get("unknowns_blocking", False))
        finite_deliverable = args.get("finite_deliverable")
        if finite_deliverable is not None and not isinstance(finite_deliverable, bool):
            return json.dumps({"ok": False, "error": "finite_deliverable must be boolean when provided"})

        durable_fields = [
            "recurring_or_continuous",
            "persists_across_sessions",
            "watches_external_events",
            "manages_pipeline_or_queue",
            "repeated_domain_items",
            "needs_optimizer_over_time",
        ]
        durable_signals: Dict[str, bool] = {}
        for field in durable_fields:
            value = args.get(field, False)
            if not isinstance(value, bool):
                return json.dumps({"ok": False, "error": f"{field} must be boolean"})
            durable_signals[field] = value

        prompt_hash = hashlib.sha256(original_prompt.encode("utf-8")).hexdigest()
        envelope: Dict[str, Any] = {
            "original_prompt": original_prompt,
            "original_prompt_sha256": prompt_hash,
            "goal": goal,
            "custom_capability": custom_capability,
            "required_capabilities": required_capabilities,
            "toolsets": toolsets,
            "skills": skills,
            "success_criteria": success_criteria,
            "entities": entities,
            "unknowns": unknowns,
            "unknowns_blocking": unknowns_blocking,
            "constraints": constraints,
            "side_effect_risk": side_effect_risk,
            "external_actions": external_actions,
            "context": context,
            "finite_deliverable": finite_deliverable,
            "durable_signals": durable_signals,
        }
        routing_input = {
            "goal": goal,
            "context": _format_intake_context(envelope),
            "required_capabilities": required_capabilities,
            "custom_capability": custom_capability,
            "toolsets": toolsets,
            "skills": skills,
        }
        route_input = {"task_summary": goal}
        if finite_deliverable is not None:
            route_input["finite_deliverable"] = finite_deliverable
        route_input.update(durable_signals)

        response = {
            "ok": True,
            "task_envelope": envelope,
            "route_input": route_input,
            "routing_input": routing_input,
            "can_dispatch": not unknowns_blocking,
            "selector_status": "present" if (required_capabilities or toolsets or skills) else "missing",
            "approval_required": side_effect_risk in {
                "external_reversible",
                "external_irreversible",
                "financial",
                "legal_compliance",
                "destructive",
            },
            "quality_checks": {
                "original_prompt_preserved": True,
                "original_prompt_sha256": prompt_hash,
                "success_criteria_count": len(success_criteria),
                "custom_capability_present": True,
            },
            "next_step": "Call gate_intake_verify on task_envelope, then gate_route with route_input, then use routing_input for scoped_worker_match or scoped_worker_dispatch if verified and can_dispatch are true.",
        }
        record_conversion_event({
            "stage": "intake",
            "outcome": "captured",
            "task_envelope": envelope,
            "custom_capability": custom_capability,
            "required_capabilities": required_capabilities,
            "toolsets": toolsets,
            "entities": entities,
            "side_effect_risk": side_effect_risk,
            "ok": True,
        })
        return json.dumps(response)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def handle_gate_intake_verify(args: dict, **kw) -> str:
    """Verify that a task envelope is specific enough to route safely."""

    try:
        envelope = _envelope_from_verify_args(args)
        min_success_criteria = int(args.get("min_success_criteria") or 2)
        min_quality_score = int(args.get("min_quality_score") or 80)
        require_selector = bool(args.get("require_selector", True))

        blocking_issues: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []

        original_prompt = str(envelope.get("original_prompt") or "").strip()
        goal = str(envelope.get("goal") or "").strip()
        custom_capability = str(envelope.get("custom_capability") or "").strip()
        success_criteria = _string_list(envelope.get("success_criteria"), "success_criteria")
        required_capabilities = _string_list(envelope.get("required_capabilities"), "required_capabilities")
        toolsets = _string_list(envelope.get("toolsets"), "toolsets")
        skills = _string_list(envelope.get("skills"), "skills")
        entities = _string_list(envelope.get("entities"), "entities")
        expected_entities = _string_list(args.get("expected_entities"), "expected_entities")
        unknowns = _string_list(envelope.get("unknowns"), "unknowns")
        external_actions = _string_list(envelope.get("external_actions"), "external_actions")
        side_effect_risk = str(envelope.get("side_effect_risk") or "none").strip()
        unknowns_blocking = bool(envelope.get("unknowns_blocking", False))

        if not original_prompt:
            blocking_issues.append({"id": "missing_original_prompt", "message": "task_envelope.original_prompt is required"})
        else:
            expected_hash = str(envelope.get("original_prompt_sha256") or "").strip()
            actual_hash = hashlib.sha256(original_prompt.encode("utf-8")).hexdigest()
            if expected_hash and expected_hash != actual_hash:
                blocking_issues.append({"id": "original_prompt_hash_mismatch", "message": "original_prompt_sha256 does not match original_prompt"})

        if not goal or _looks_generic(goal):
            blocking_issues.append({"id": "weak_goal", "message": "goal is missing or too generic"})
        if not custom_capability:
            blocking_issues.append({"id": "missing_custom_capability", "message": "custom_capability is required"})
        elif len(custom_capability.split()) < 4 or _looks_generic(custom_capability):
            blocking_issues.append({"id": "generic_custom_capability", "message": "custom_capability must describe the real task, not a one-word category"})

        generic_criteria = [item for item in success_criteria if _looks_generic(item)]
        if len(success_criteria) < min_success_criteria:
            blocking_issues.append({
                "id": "too_few_success_criteria",
                "message": f"success_criteria must include at least {min_success_criteria} specific checks",
            })
        if generic_criteria:
            blocking_issues.append({
                "id": "generic_success_criteria",
                "message": "success_criteria contains vague checks",
                "items": generic_criteria,
            })

        selector_present = bool(required_capabilities or toolsets or skills or str(envelope.get("profile") or "").strip())
        if require_selector and not selector_present:
            blocking_issues.append({
                "id": "missing_policy_selector",
                "message": "Add an explicit profile or hard selectors such as required_capabilities, toolsets, or skills before dispatch",
            })

        auto_entities = _extract_prompt_entities(original_prompt) if original_prompt else []
        entity_expectations = expected_entities or auto_entities
        missing_entities = _missing_entities(entity_expectations, entities) if entity_expectations else []
        if expected_entities and missing_entities:
            blocking_issues.append({
                "id": "expected_entities_missing",
                "message": "Expected entities are not captured in task_envelope.entities",
                "items": missing_entities,
            })
        elif auto_entities and not entities:
            blocking_issues.append({
                "id": "entities_missing",
                "message": "Likely prompt entities are not captured in task_envelope.entities",
                "items": auto_entities,
            })
        elif missing_entities:
            warnings.append({
                "id": "some_entities_may_be_missing",
                "message": "Some likely prompt entities are not listed in task_envelope.entities",
                "items": missing_entities,
            })

        if side_effect_risk not in _SIDE_EFFECT_RISKS:
            blocking_issues.append({
                "id": "invalid_side_effect_risk",
                "message": f"side_effect_risk must be one of {sorted(_SIDE_EFFECT_RISKS)}",
            })
        if external_actions and side_effect_risk == "none":
            blocking_issues.append({
                "id": "external_actions_without_risk",
                "message": "external_actions require side_effect_risk other than none",
                "items": external_actions,
            })

        hinted_risks = _risk_hints(" ".join([original_prompt, goal, custom_capability]))
        understated_risks = [risk for risk in hinted_risks if side_effect_risk in {"none", "internal"} and risk != "external_reversible"]
        if understated_risks:
            blocking_issues.append({
                "id": "side_effect_risk_understated",
                "message": "Prompt appears to request a higher-risk side effect than the envelope declares",
                "items": sorted(set(understated_risks)),
            })
        elif hinted_risks and side_effect_risk == "none":
            warnings.append({
                "id": "side_effect_risk_maybe_understated",
                "message": "Prompt may involve external side effects; confirm side_effect_risk",
                "items": sorted(set(hinted_risks)),
            })

        if unknowns_blocking:
            blocking_issues.append({
                "id": "unknowns_blocking",
                "message": "unknowns_blocking is true; ask for missing context before dispatch",
                "items": unknowns,
            })
        elif unknowns:
            warnings.append({
                "id": "non_blocking_unknowns_present",
                "message": "Envelope has unknowns marked non-blocking; worker should return a blocker if they matter",
                "items": unknowns,
            })

        score = 100 - (20 * len(blocking_issues)) - (7 * len(warnings))
        score = max(0, min(100, score))
        threshold_met = score >= min_quality_score
        verified = not blocking_issues and threshold_met

        checks = {
            "original_prompt_preserved": bool(original_prompt),
            "goal_specific": bool(goal and not _looks_generic(goal)),
            "custom_capability_specific": bool(custom_capability and len(custom_capability.split()) >= 4 and not _looks_generic(custom_capability)),
            "success_criteria_count": len(success_criteria),
            "success_criteria_specific": bool(success_criteria and not generic_criteria and len(success_criteria) >= min_success_criteria),
            "selector_present": selector_present,
            "entities_captured": bool(not entity_expectations or not missing_entities),
            "captured_entities": entities,
            "expected_or_detected_entities": entity_expectations,
            "side_effect_risk": side_effect_risk,
            "side_effect_risk_acceptable": not any(issue["id"] in {"invalid_side_effect_risk", "external_actions_without_risk", "side_effect_risk_understated"} for issue in blocking_issues),
            "approval_required": _approval_required(side_effect_risk),
            "unknowns_blocking": unknowns_blocking,
        }
        repair_guidance = [
            "Repair the task envelope and rerun gate_intake_verify before gate_route or scoped_worker_dispatch.",
            "Keep original_prompt exact; make custom_capability a specific plain-English job description.",
            "Add specific success_criteria and entities from the prompt.",
            "Use hard selectors only when they are true policy/tool requirements.",
            "Raise side_effect_risk when the task touches real external systems, money, legal/compliance, or destructive actions.",
        ]

        response = {
            "ok": verified,
            "verified": verified,
            "quality_score": score,
            "min_quality_score": min_quality_score,
            "threshold_met": threshold_met,
            "blocking_issues": blocking_issues,
            "warnings": warnings,
            "checks": checks,
            "can_dispatch": verified and not unknowns_blocking and selector_present,
            "approval_required": checks["approval_required"],
            "repair_guidance": repair_guidance if not verified else [],
            "next_step": "Call gate_route with the verified route_input, then scoped_worker_match/dispatch with the verified task_envelope." if verified else "Repair the envelope, then rerun gate_intake_verify.",
        }
        record_conversion_event({
            "stage": "intake_verify",
            "outcome": "verified" if verified else "blocked",
            "task_envelope": envelope,
            "custom_capability": custom_capability,
            "required_capabilities": required_capabilities,
            "toolsets": toolsets,
            "entities": entities,
            "side_effect_risk": side_effect_risk,
            "quality_score": score,
            "blockers": [issue["id"] for issue in blocking_issues],
            "ok": verified,
        })
        return json.dumps(response)
    except ValueError as exc:
        return json.dumps({"ok": False, "verified": False, "error": str(exc)})


def handle_gate_conversion_ledger(args: dict, **kw) -> str:
    """Inspect recent conversion-funnel events."""

    try:
        action = str(args.get("action") or "summary").strip()
        limit = int(args.get("limit") or 100)
        limit = max(1, min(limit, 1000))
        rows = _read_ledger(limit=limit)

        stage = str(args.get("stage") or "").strip()
        worker = str(args.get("worker_profile") or args.get("worker") or "").strip()
        custom_capability = str(args.get("custom_capability") or "").strip()
        outcome = str(args.get("outcome") or "").strip()
        model_provider = str(args.get("model_provider") or args.get("provider") or "").strip()
        model = str(args.get("model") or "").strip()
        model_key = str(args.get("model_key") or "").strip()

        def matches(row: Dict[str, Any]) -> bool:
            if stage and row.get("stage") != stage:
                return False
            if worker and row.get("worker_profile") != worker:
                return False
            if custom_capability and row.get("custom_capability") != custom_capability:
                return False
            if outcome and row.get("outcome") != outcome:
                return False
            if model_provider and row.get("model_provider") != model_provider:
                return False
            if model and row.get("model") != model:
                return False
            if model_key and row.get("model_key") != model_key:
                return False
            return True

        filtered = [row for row in rows if matches(row)]
        if action == "recent":
            return json.dumps({
                "ok": True,
                "path": str(_ledger_path()),
                "events": filtered[-limit:],
                "summary": _ledger_summary(filtered),
            })
        if action == "summary":
            return json.dumps({
                "ok": True,
                "path": str(_ledger_path()),
                "summary": _ledger_summary(filtered),
                "recent_events": filtered[-min(10, limit):],
            })
        return json.dumps({"ok": False, "error": "action must be 'summary' or 'recent'"})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def handle_gate_route(args: dict, **kw) -> str:
    """Classify whether work belongs in Tier 1 contract flow or Tier 2 board flow."""

    task_summary = str(args.get("task_summary") or "").strip()
    if not task_summary:
        return json.dumps({"ok": False, "error": "task_summary is required"})

    finite_deliverable = args.get("finite_deliverable")
    if finite_deliverable is not None and not isinstance(finite_deliverable, bool):
        return json.dumps({"ok": False, "error": "finite_deliverable must be boolean when provided"})

    durable_signal_fields = [
        "recurring_or_continuous",
        "persists_across_sessions",
        "watches_external_events",
        "manages_pipeline_or_queue",
        "repeated_domain_items",
        "needs_optimizer_over_time",
    ]

    durable_signals = []
    for field in durable_signal_fields:
        value = args.get(field, False)
        if not isinstance(value, bool):
            return json.dumps({"ok": False, "error": f"{field} must be boolean"})
        if value:
            durable_signals.append(field)

    if durable_signals:
        return json.dumps({
            "ok": True,
            "tier": "tier2_board",
            "board_allowed": True,
            "contract_allowed": False,
            "reason": "Tier 2 requires recurrence/durability; durable signals are present.",
            "durable_signals": durable_signals,
            "next_step": "Call kanban_match_board, then route to an existing board or launch intake if no board fits.",
            "clarifying_question": None,
        })

    if finite_deliverable is True:
        return json.dumps({
            "ok": True,
            "tier": "tier1_contract",
            "board_allowed": False,
            "contract_allowed": True,
            "reason": "Finite deliverable with no durable/recurring signals. Complexity alone does not justify a board.",
            "durable_signals": [],
            "next_step": "Use gate_contract, gate_delegate or gate_parallel, scoped_worker_dispatch/scoped_worker_batch, then gate_validate or gate_aggregate.",
            "clarifying_question": None,
        })

    return json.dumps({
        "ok": True,
        "tier": "ambiguous",
        "board_allowed": False,
        "contract_allowed": False,
        "reason": "No recurrence/durability signals were provided, and the task was not declared finite.",
        "durable_signals": [],
        "next_step": "Ask one clarification before choosing a tier.",
        "clarifying_question": "Is this a one-time deliverable, or should it keep running/tracking over time?",
    })


def handle_gate_contract(args: dict, **kw) -> str:
    """Declare a contract before delegating work."""
    
    intent = args.get("intent", "").strip()
    if not intent:
        return json.dumps({"ok": False, "error": "intent is required"})
    
    success_indicators = args.get("success_indicators", [])
    if not success_indicators:
        success_indicators = [intent]
    
    criteria = args.get("criteria", [])
    if not criteria:
        return json.dumps({"ok": False, "error": "criteria is required"})
    
    # Validate criteria structure
    for i, c in enumerate(criteria):
        if not c.get("id"):
            return json.dumps({"ok": False, "error": f"criterion {i} missing 'id'"})
        if not c.get("description"):
            return json.dumps({"ok": False, "error": f"criterion {i} missing 'description'"})
        if not c.get("proof_type"):
            return json.dumps({"ok": False, "error": f"criterion {i} missing 'proof_type'"})
        if not c.get("proof_source"):
            return json.dumps({"ok": False, "error": f"criterion {i} missing 'proof_source'"})
        
        # Validate enums
        try:
            ProofType(c["proof_type"])
        except ValueError:
            valid = [p.value for p in ProofType]
            return json.dumps({"ok": False, "error": f"criterion {i} invalid proof_type. Valid: {valid}"})
        
        # Defaults
        c.setdefault("layer", "L1_structural")
        c.setdefault("visibility", "disclosed")
        c.setdefault("required", True)
    
    gate = get_gate()
    result = gate.contract(
        intent=intent,
        success_indicators=success_indicators,
        criteria=criteria,
        proof_required=args.get("proof_required"),
        workspace=args.get("workspace"),
        context=args.get("context", ""),
        timeout_seconds=args.get("timeout_seconds", 300),
        max_attempts=args.get("max_attempts", 3),
    )
    
    return json.dumps(result)


def handle_gate_delegate(args: dict, **kw) -> str:
    """Get delegation payload for current contract."""
    
    gate = get_gate()
    result = gate.delegate(
        additional_context=args.get("context", ""),
    )
    
    return json.dumps(result)


def handle_gate_validate(args: dict, **kw) -> str:
    """Validate worker artifacts against contract."""
    
    artifacts = args.get("artifacts", [])
    if not artifacts:
        return json.dumps({"ok": False, "error": "artifacts is required"})
    
    # Validate artifact structure
    for i, a in enumerate(artifacts):
        if not a.get("id"):
            return json.dumps({"ok": False, "error": f"artifact {i} missing 'id'"})
    
    gate = get_gate()
    result = gate.validate(
        artifacts=artifacts,
        summary=args.get("summary", ""),
    )
    record_conversion_event({
        "stage": "validate",
        "outcome": "validated_success" if result.get("passed") is True else "validation_failed",
        "artifact_count": len(artifacts),
        "retry_count": result.get("attempts") or result.get("attempt"),
        "blockers": result.get("failed_criteria") or [],
        "ok": result.get("ok"),
    })
    
    return json.dumps(result)


def handle_gate_status(args: dict, **kw) -> str:
    """Get current contract status."""
    
    gate = get_gate()
    result = gate.status()
    
    return json.dumps(result)


def handle_gate_abandon(args: dict, **kw) -> str:
    """Abandon current contract."""
    
    reason = args.get("reason", "").strip()
    if not reason:
        return json.dumps({"ok": False, "error": "reason is required"})
    
    gate = get_gate()
    result = gate.abandon(reason=reason)
    
    return json.dumps(result)


def handle_gate_parallel(args: dict, **kw) -> str:
    """Decompose work into parallel subtasks."""
    
    subtasks = args.get("subtasks", [])
    if not subtasks:
        return json.dumps({"ok": False, "error": "subtasks is required"})
    
    # Validate subtask structure
    for i, st in enumerate(subtasks):
        if not st.get("id"):
            return json.dumps({"ok": False, "error": f"subtask {i} missing 'id'"})
        if not st.get("goal"):
            return json.dumps({"ok": False, "error": f"subtask {i} missing 'goal'"})
    
    gate = get_gate()
    result = gate.decompose(subtasks=subtasks)
    
    return json.dumps(result)


def handle_gate_aggregate(args: dict, **kw) -> str:
    """Aggregate results from parallel subtasks."""
    
    subtask_results = args.get("subtask_results", [])
    if not subtask_results:
        return json.dumps({"ok": False, "error": "subtask_results is required"})
    
    # Validate result structure
    for i, r in enumerate(subtask_results):
        if not r.get("subtask_id"):
            return json.dumps({"ok": False, "error": f"result {i} missing 'subtask_id'"})
        if not r.get("artifacts"):
            return json.dumps({"ok": False, "error": f"result {i} missing 'artifacts'"})
    
    gate = get_gate()
    result = gate.aggregate(subtask_results=subtask_results)
    record_conversion_event({
        "stage": "aggregate",
        "outcome": "aggregate_success" if result.get("ok") is True else "aggregate_failed",
        "artifact_count": sum(len(item.get("artifacts") or []) for item in subtask_results if isinstance(item, dict)),
        "blockers": result.get("failed_criteria") or [],
        "ok": result.get("ok"),
    })
    
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Tool Schemas
# ---------------------------------------------------------------------------

GATE_INTAKE_SCHEMA = {
    "name": "gate_intake",
    "description": (
        "Capture the full original user prompt plus structured routing metadata before gate_route or worker dispatch. "
        "Use this to preserve context losslessly while extracting goal, custom_capability, selectors, success criteria, unknowns, and side-effect risk."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "original_prompt": {"type": "string", "description": "Exact original user request. Preserve it; do not summarize it here."},
            "goal": {"type": "string", "description": "Concrete outcome the work should accomplish."},
            "custom_capability": {"type": "string", "description": "Freeform description of the real task capability, not just a category label."},
            "required_capabilities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional enforceable policy capability tags for worker matching."
            },
            "toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional hard toolset selectors needed for worker matching."
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional skill selectors needed for worker matching."
            },
            "success_criteria": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific conditions that must be true before the task can be called done."
            },
            "entities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Named systems, people, files, accounts, repos, products, or domain objects from the prompt."
            },
            "unknowns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Missing details or blockers discovered during intake."
            },
            "unknowns_blocking": {"type": "boolean", "description": "True if dispatch should wait for missing user input."},
            "constraints": {
                "type": "array",
                "items": {"type": "string"},
                "description": "User constraints, safety limits, deadlines, output requirements, or non-goals."
            },
            "side_effect_risk": {
                "type": "string",
                "enum": ["none", "internal", "external_reversible", "external_irreversible", "financial", "legal_compliance", "destructive"],
                "description": "Highest action risk implied by the task."
            },
            "external_actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Actions that touch real external systems or people."
            },
            "context": {"type": "string", "description": "Additional background context to preserve with the prompt."},
            "finite_deliverable": {"type": "boolean", "description": "True if the work should end after one completed/proven deliverable."},
            "recurring_or_continuous": {"type": "boolean", "description": "True when the work should keep running repeatedly or continuously."},
            "persists_across_sessions": {"type": "boolean", "description": "True when state must survive beyond this task/session."},
            "watches_external_events": {"type": "boolean", "description": "True when it waits for replies/webhooks/timers/future events."},
            "manages_pipeline_or_queue": {"type": "boolean", "description": "True when it manages ongoing leads/tickets/backlog/items."},
            "repeated_domain_items": {"type": "boolean", "description": "True when the same workflow repeats for new domain items."},
            "needs_optimizer_over_time": {"type": "boolean", "description": "True when it needs ongoing optimization/learning over multiple cycles."}
        },
        "required": ["original_prompt", "goal", "custom_capability", "success_criteria"]
    }
}

GATE_INTAKE_VERIFY_SCHEMA = {
    "name": "gate_intake_verify",
    "description": (
        "Verify a gate_intake task envelope before route/dispatch. Checks original prompt preservation, "
        "specific goal/custom_capability/success criteria, entity capture, policy selectors, unknowns, and side-effect risk."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_envelope": {"type": "object", "description": "Task envelope returned by gate_intake."},
            "intake": {"type": "object", "description": "Full gate_intake response; task_envelope will be read from it."},
            "expected_entities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional explicit entities that must appear in task_envelope.entities."
            },
            "min_success_criteria": {
                "type": "integer",
                "description": "Minimum number of specific success criteria. Defaults to 2."
            },
            "min_quality_score": {
                "type": "integer",
                "description": "Minimum verifier score required for verified=true. Defaults to 80."
            },
            "require_selector": {
                "type": "boolean",
                "description": "Require profile, required_capabilities, toolsets, or skills before dispatch. Defaults to true."
            }
        }
    }
}

GATE_CONVERSION_LEDGER_SCHEMA = {
    "name": "gate_conversion_ledger",
    "description": "Inspect the local conversion funnel ledger for recent intake, verification, dispatch, and validation events.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["summary", "recent"],
                "description": "summary returns aggregate counts plus recent events; recent returns matching events."
            },
            "limit": {"type": "integer", "description": "Maximum number of ledger rows to read, capped at 1000."},
            "stage": {"type": "string", "description": "Optional stage filter, e.g. intake, intake_verify, dispatch, validate."},
            "worker_profile": {"type": "string", "description": "Optional worker profile filter."},
            "model_key": {"type": "string", "description": "Optional worker model route key filter."},
            "model_provider": {"type": "string", "description": "Optional model provider filter."},
            "model": {"type": "string", "description": "Optional model name filter."},
            "custom_capability": {"type": "string", "description": "Optional exact custom capability filter."},
            "outcome": {"type": "string", "description": "Optional outcome filter."}
        }
    }
}

GATE_ROUTE_SCHEMA = {
    "name": "gate_route",
    "description": (
        "Classify work as Tier 1 contract flow or Tier 2 board flow. "
        "Tier 2 is allowed only for recurring/durable operating loops, not merely large or multi-step work. "
        "Call before choosing between gate_contract and kanban board launch."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_summary": {"type": "string", "description": "Plain-English summary of the requested work"},
            "finite_deliverable": {"type": "boolean", "description": "True when the work should end after one completed/proven deliverable"},
            "recurring_or_continuous": {"type": "boolean", "description": "True when the work should keep running repeatedly or continuously"},
            "persists_across_sessions": {"type": "boolean", "description": "True when state must survive beyond this task/session"},
            "watches_external_events": {"type": "boolean", "description": "True when it waits for replies/webhooks/timers/future events"},
            "manages_pipeline_or_queue": {"type": "boolean", "description": "True when it manages ongoing leads/tickets/backlog/items"},
            "repeated_domain_items": {"type": "boolean", "description": "True when the same workflow repeats for new domain items"},
            "needs_optimizer_over_time": {"type": "boolean", "description": "True when it needs ongoing optimization/learning over multiple cycles"}
        },
        "required": ["task_summary"]
    }
}


GATE_CONTRACT_SCHEMA = {
    "name": "gate_contract",
    "description": (
        "REQUIRED before delegating work. Declare a contract with intent, success indicators "
        "(what worker sees), and criteria (what you validate). The gate blocks completion "
        "until all criteria pass. Call this FIRST, then gate_delegate."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "What the task should accomplish"
            },
            "success_indicators": {
                "type": "array",
                "description": "Fuzzy goals the worker sees (e.g., 'tests pass')",
                "items": {"type": "string"}
            },
            "criteria": {
                "type": "array",
                "description": "Validation criteria. Each needs: id, description, proof_type, proof_source",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "proof_type": {
                            "type": "string",
                            "enum": ["exit_code", "file_exists", "content_match", "test_pass", "hash_match", "custom"]
                        },
                        "proof_source": {"type": "string", "description": "Artifact ID that satisfies this"},
                        "layer": {
                            "type": "string",
                            "enum": ["L1_structural", "L2_semantic", "L3_functional", "L4_audit"]
                        },
                        "visibility": {
                            "type": "string",
                            "enum": ["disclosed", "hinted", "hidden"]
                        },
                        "required": {"type": "boolean"},
                        "pattern": {"type": "string", "description": "Regex for content_match"},
                        "expected_hash": {"type": "string", "description": "Expected hash for hash_match"}
                    },
                    "required": ["id", "description", "proof_type", "proof_source"]
                }
            },
            "proof_required": {
                "type": "array",
                "description": "Artifact IDs that must be returned (auto-extracted from criteria if omitted)",
                "items": {"type": "string"}
            },
            "workspace": {"type": "string", "description": "Working directory"},
            "context": {"type": "string", "description": "Background context for worker"},
            "timeout_seconds": {"type": "integer", "description": "Max execution time (default: 300)"},
            "max_attempts": {"type": "integer", "description": "Max retry attempts (default: 3)"}
        },
        "required": ["intent", "criteria"]
    }
}

GATE_DELEGATE_SCHEMA = {
    "name": "gate_delegate",
    "description": (
        "Get the delegation payload for the current contract. Returns what to pass to scoped_worker_dispatch/scoped_worker_batch. "
        "Worker sees success_indicators and disclosed criteria, never hidden criteria."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "context": {
                "type": "string",
                "description": "Additional context to append for this delegation"
            }
        }
    }
}

GATE_VALIDATE_SCHEMA = {
    "name": "gate_validate",
    "description": (
        "Validate worker artifacts against the contract. If passed, task completes. "
        "If failed, returns actionable feedback for retry."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "artifacts": {
                "type": "array",
                "description": "Artifacts from worker. Each needs: id. Optional: type, content_hash, path, content, exit_code, test_passed, test_failed",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"type": "string"},
                        "content_hash": {"type": "string"},
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "exit_code": {"type": "integer"},
                        "test_passed": {"type": "integer"},
                        "test_failed": {"type": "integer"},
                        "metadata": {"type": "object"}
                    },
                    "required": ["id"]
                }
            },
            "summary": {"type": "string", "description": "Worker's summary (informational only)"}
        },
        "required": ["artifacts"]
    }
}

GATE_STATUS_SCHEMA = {
    "name": "gate_status",
    "description": "Check current contract status.",
    "parameters": {"type": "object", "properties": {}}
}

GATE_ABANDON_SCHEMA = {
    "name": "gate_abandon",
    "description": "Abandon the current contract. Requires a reason.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Why abandoning"}
        },
        "required": ["reason"]
    }
}


GATE_PARALLEL_SCHEMA = {
    "name": "gate_parallel",
    "description": (
        "Decompose work into parallel subtasks for maximum throughput. "
        "Analyzes dependencies and groups independent tasks to run concurrently. "
        "Use this when a task has multiple independent parts that can be done simultaneously. "
        "Returns delegation payloads grouped by execution order."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "subtasks": {
                "type": "array",
                "description": "List of subtasks to execute",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Unique subtask identifier"},
                        "goal": {"type": "string", "description": "What this subtask should accomplish"},
                        "context": {"type": "string", "description": "Subtask-specific context"},
                        "toolsets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tools the worker needs (default: ['terminal', 'file'])"
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Subtask IDs this depends on (runs after those complete)"
                        },
                        "success_indicators": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Success indicators for this subtask"
                        },
                        "criteria": {
                            "type": "array",
                            "description": "Subtask-specific validation criteria (optional)"
                        }
                    },
                    "required": ["id", "goal"]
                }
            }
        },
        "required": ["subtasks"]
    }
}


GATE_AGGREGATE_SCHEMA = {
    "name": "gate_aggregate",
    "description": (
        "Aggregate results from parallel subtasks and validate against the parent contract. "
        "Call this after all subtasks complete to combine artifacts and run final validation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "subtask_results": {
                "type": "array",
                "description": "Results from each subtask",
                "items": {
                    "type": "object",
                    "properties": {
                        "subtask_id": {"type": "string", "description": "Which subtask this is from"},
                        "artifacts": {
                            "type": "array",
                            "description": "Artifacts from this subtask"
                        },
                        "summary": {"type": "string", "description": "Worker summary"}
                    },
                    "required": ["subtask_id", "artifacts"]
                }
            }
        },
        "required": ["subtask_results"]
    }
}


# ---------------------------------------------------------------------------
# Tool Registration
# ---------------------------------------------------------------------------

TOOLS = [
    {"schema": GATE_INTAKE_SCHEMA, "handler": handle_gate_intake},
    {"schema": GATE_INTAKE_VERIFY_SCHEMA, "handler": handle_gate_intake_verify},
    {"schema": GATE_CONVERSION_LEDGER_SCHEMA, "handler": handle_gate_conversion_ledger},
    {"schema": GATE_ROUTE_SCHEMA, "handler": handle_gate_route},
    {"schema": GATE_CONTRACT_SCHEMA, "handler": handle_gate_contract},
    {"schema": GATE_DELEGATE_SCHEMA, "handler": handle_gate_delegate},
    {"schema": GATE_PARALLEL_SCHEMA, "handler": handle_gate_parallel},
    {"schema": GATE_VALIDATE_SCHEMA, "handler": handle_gate_validate},
    {"schema": GATE_AGGREGATE_SCHEMA, "handler": handle_gate_aggregate},
    {"schema": GATE_STATUS_SCHEMA, "handler": handle_gate_status},
    {"schema": GATE_ABANDON_SCHEMA, "handler": handle_gate_abandon},
]
