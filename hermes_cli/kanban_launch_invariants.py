"""Structural grammar invariants for synthesized board operating contracts.

These checks are the enforcement half of the dynamic-schema design: intake +
synthesis may assemble *any* combination of primitives for *any* goal, but the
resulting contract must obey a small, domain-agnostic grammar so the reactive
control plane is always actually wired -- never a hollow stage that looks fine
in JSON but cannot run a real back-and-forth or know when to stop.

The rules are about the *shape* of the contract (watched things must be able to
terminate; a conversational stage must have a follow-up timer and more than one
way out; tunable knobs must declare a range), not about any industry. They hold
for land wholesaling, insurance recruiting, a research team, or anything else.

A finding is an ``error`` (hard invariant -- synthesis must not ship a contract
that fails it) or a ``warning`` (smell worth surfacing but not blocking).

This module is deterministic and offline: no model calls, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from hermes_cli.kanban_launch_grammar import (
    SIDE_EFFECT_CLASSES,
    has_inbound as _grammar_has_inbound,
    has_timer as _grammar_has_timer,
    is_external_side_effect_class as _grammar_is_external_side_effect,
    is_known_side_effect_class as _grammar_is_known_side_effect,
    normalize_side_effect_class as _grammar_normalize_side_effect,
)


@dataclass
class InvariantReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "checked": dict(self.checked),
        }


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    return []


def _contract_root(contract: Any) -> dict[str, Any]:
    parsed = contract if isinstance(contract, dict) else {}
    if isinstance(parsed.get("operating_contract"), dict):
        return dict(parsed["operating_contract"])
    inner = parsed.get("contract")
    if isinstance(inner, dict) and any(k in inner for k in ("objective", "runtime", "workflow")):
        return dict(inner)
    return dict(parsed)


def _stop_signal_count(*collections: Any) -> int:
    """Count distinct terminal/stop signals across the given collections."""
    seen: set[str] = set()
    for coll in collections:
        for item in _as_list(coll):
            key = ""
            if isinstance(item, str):
                key = item.strip().lower()
            elif isinstance(item, dict):
                key = str(
                    item.get("key")
                    or item.get("state")
                    or item.get("condition")
                    or item.get("transition")
                    or ""
                ).strip().lower()
            if key:
                seen.add(key)
    return len(seen)


def _stage_exit_outcomes(stage: dict[str, Any]) -> int:
    return _stop_signal_count(stage.get("exit_criteria"))


def _check_stages(root: dict[str, Any], report: InvariantReport) -> None:
    workflow = _as_dict(root.get("workflow"))
    stages = [s for s in _as_list(workflow.get("stages")) if isinstance(s, dict)]
    report.checked["stages"] = len(stages)
    conversational = 0
    for stage in stages:
        key = str(stage.get("key") or stage.get("label") or "stage")
        substates = _as_list(stage.get("substates"))
        triggers = stage.get("triggers")
        has_inbound = _grammar_has_inbound(triggers)
        # A "conversational watch stage" is one that holds an ongoing back-and-forth:
        # it has internal substates AND can receive an external party's reply.
        is_watch_stage = bool(substates) and has_inbound
        if is_watch_stage:
            conversational += 1
            if not _grammar_has_timer(triggers):
                report.errors.append(
                    f"stage '{key}' is conversational (has substates + inbound trigger) "
                    f"but declares no follow-up timer trigger -- it can never decide to nudge."
                )
            if _stage_exit_outcomes(stage) < 2:
                report.errors.append(
                    f"stage '{key}' is conversational but has fewer than 2 exit outcomes -- "
                    f"it cannot distinguish success from kill/recycle."
                )
        # Dead-end smell: substates but no way out declared and not a terminal stage.
        if substates and _stage_exit_outcomes(stage) == 0 and not has_inbound:
            report.warnings.append(
                f"stage '{key}' has substates but no exit_criteria -- confirm it is "
                f"intentionally terminal/cyclic and not a dead end."
            )
    report.checked["conversational_stages"] = conversational


def _check_reactive_entities(root: dict[str, Any], report: InvariantReport) -> None:
    runtime = _as_dict(root.get("runtime"))
    reactive = _as_dict(runtime.get("reactive_entities"))
    report.checked["reactive_entities"] = len(reactive)
    for name, cfg in reactive.items():
        cfg = _as_dict(cfg)
        resolves = bool(cfg.get("terminal_completion_requires_entity_resolution"))
        has_stop = _stop_signal_count(
            cfg.get("terminal_states"), cfg.get("stop_conditions")
        ) > 0
        if not (resolves or has_stop):
            report.errors.append(
                f"reactive entity '{name}' has no terminal resolution rule and no stop "
                f"conditions -- a watched thing that can never be closed becomes a zombie."
            )


def _coerce_nonneg_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _loop_has_finite_max_nudges(loop: dict[str, Any], root: dict[str, Any]) -> bool:
    """True if the loop is capped by a finite ``max_nudges`` (machine-checkable).

    Mirrors :func:`hermes_cli.kanban_reactive_runtime.loop_max_nudges`: a cap
    declared on the loop, or a board ``tunables`` knob whose name signals a
    nudge cap with a numeric default. A finite nudge cap is a hard, runtime-
    enforceable terminator -- unlike free-text ``stop_conditions``.
    """
    for key in ("max_nudges", "max_follow_ups", "max_followups", "max_retries"):
        if _coerce_nonneg_int(loop.get(key)) is not None:
            return True
    tunables = root.get("tunables")
    if not isinstance(tunables, dict):
        tunables = _as_dict(root.get("runtime")).get("tunables")
    if isinstance(tunables, dict):
        for knob, spec in tunables.items():
            name = str(knob).lower()
            if "nudge" in name or "follow_up" in name or "followup" in name:
                default = spec.get("default") if isinstance(spec, dict) else spec
                if _coerce_nonneg_int(default) is not None:
                    return True
    return False


def _check_event_loops(root: dict[str, Any], report: InvariantReport) -> None:
    # Synthesized-style contracts model the watcher as event_loops + entities.
    loops = [e for e in _as_list(root.get("event_loops")) if isinstance(e, dict)]
    report.checked["event_loops"] = len(loops)
    for loop in loops:
        name = str(loop.get("entity") or loop.get("type") or "loop")
        triggers = loop.get("triggers")
        stop_count = _stop_signal_count(loop.get("terminal_states"), loop.get("stop_conditions"))
        # F10 rail: a watcher must have a MACHINE-CHECKABLE stop -- a
        # ``terminal_states`` entry or a finite ``max_nudges`` -- that the
        # runtime can actually evaluate. Free-text ``stop_conditions`` alone do
        # NOT satisfy "must terminate": nothing parses them, so the timer fires
        # forever (the F10 zombie). They remain valuable as human guidance and
        # as a runtime halt when they happen to name a state (see reactive_tick).
        has_terminal = _stop_signal_count(loop.get("terminal_states")) > 0
        has_machine_stop = has_terminal or _loop_has_finite_max_nudges(loop, root)
        if not has_machine_stop:
            if _stop_signal_count(loop.get("stop_conditions")) > 0:
                report.errors.append(
                    f"event loop '{name}' declares only free-text stop_conditions and no "
                    f"terminal_states or finite max_nudges -- a free-text stop is not "
                    f"machine-checkable, so the watcher can fire forever."
                )
            else:
                report.errors.append(
                    f"event loop '{name}' declares no terminal_states or stop_conditions -- "
                    f"it can never stop watching."
                )
        if _grammar_has_inbound(triggers):
            if not _grammar_has_timer(triggers):
                report.errors.append(
                    f"event loop '{name}' is conversational (inbound trigger) but has no "
                    f"timer trigger -- no follow-up cadence."
                )
            if stop_count < 2:
                report.errors.append(
                    f"event loop '{name}' is conversational but has fewer than 2 terminal/stop "
                    f"outcomes -- cannot separate success from give-up."
                )
    # Entities (synthesized shape) must declare terminal states.
    entities = [e for e in _as_list(root.get("entities")) if isinstance(e, dict)]
    report.checked["entities"] = len(entities)
    for ent in entities:
        name = str(ent.get("key") or ent.get("type") or "entity")
        if _stop_signal_count(ent.get("terminal_states")) == 0:
            report.warnings.append(
                f"entity '{name}' declares no terminal_states -- confirm how it completes."
            )


def _check_tunables(root: dict[str, Any], report: InvariantReport) -> None:
    runtime = _as_dict(root.get("runtime"))
    tunables = root.get("tunables")
    if tunables is None:
        tunables = runtime.get("tunables")
    if not isinstance(tunables, dict) or not tunables:
        # Not an error: many valid contracts predate the tunables block. Surface
        # it so the owner knows which knobs they will (not) be able to adjust.
        report.warnings.append(
            "contract declares no 'tunables' block -- the operation will run but expose "
            "no explicit knobs for the optimizer/owner to tune."
        )
        report.checked["tunables"] = 0
        return
    report.checked["tunables"] = len(tunables)
    for knob, spec in tunables.items():
        spec = _as_dict(spec)
        has_range = (
            ("range" in spec)
            or ("min" in spec and "max" in spec)
            or ("allowed" in spec)
            or ("options" in spec)
        )
        if not has_range:
            report.errors.append(
                f"tunable '{knob}' declares no range/min-max/allowed values -- an unbounded "
                f"knob is unsafe for autonomous tuning."
            )


def _policy_declared_classes(root: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return ``(declared, gated)`` side-effect classes from side_effect_policy.

    ``declared`` is every class named in allowed/forbidden/approval_required;
    ``gated`` is the subset named in forbidden/approval_required (the
    machine-checkable governance an external action must appear under).
    """
    policy = _as_dict(root.get("side_effect_policy"))

    def _classes(*keys: str) -> set[str]:
        out: set[str] = set()
        for key in keys:
            for item in _as_list(policy.get(key)):
                if isinstance(item, str):
                    canon = item.strip().lower()
                    if canon:
                        out.add(canon)
        return out

    gated = _classes("forbidden", "approval_required")
    declared = _classes("allowed") | gated
    return declared, gated


def _iter_declared_side_effect_classes(root: dict[str, Any]):
    """Yield ``(where, raw_class)`` for every declared ``side_effect_class``.

    Walks the places a typed side-effect class can be stamped: workflow stages
    and their actions, plus synthesized event_loops/entities. Only entries that
    actually carry a ``side_effect_class`` are yielded -- an action with none is
    treated as benign (implicitly ``none``) and not validated.
    """
    workflow = _as_dict(root.get("workflow"))
    for stage in _as_list(workflow.get("stages")):
        if not isinstance(stage, dict):
            continue
        skey = str(stage.get("key") or stage.get("label") or "stage")
        if "side_effect_class" in stage:
            yield (f"stage '{skey}'", stage.get("side_effect_class"))
        for action in _as_list(stage.get("actions")):
            if isinstance(action, dict) and "side_effect_class" in action:
                akey = str(action.get("key") or action.get("label") or "action")
                yield (f"stage '{skey}' action '{akey}'", action.get("side_effect_class"))
    for loop in _as_list(root.get("event_loops")):
        if isinstance(loop, dict) and "side_effect_class" in loop:
            lkey = str(loop.get("entity") or loop.get("type") or "loop")
            yield (f"event loop '{lkey}'", loop.get("side_effect_class"))
    for ent in _as_list(root.get("entities")):
        if isinstance(ent, dict) and "side_effect_class" in ent:
            ekey = str(ent.get("key") or ent.get("type") or "entity")
            yield (f"entity '{ekey}'", ent.get("side_effect_class"))


def _check_side_effect_classes(root: dict[str, Any], report: InvariantReport) -> None:
    """HARD rail (B3): a declared ``side_effect_class`` must be typed + governed.

    The approval boundary used to rest on free-text the synthesizer stamped: a
    near-miss class name or a missing one dispatched autonomously, ungated. Now
    every declared class must (a) be a member of the closed vocabulary, (b) be
    declared in ``side_effect_policy``, and (c) -- if it crosses the board
    boundary (``external_*``/``financial``) -- appear in ``approval_required``
    or ``forbidden``. ``none`` is benign and exempt from (b)/(c).
    """
    declared_policy, gated_policy = _policy_declared_classes(root)
    checked = 0
    for where, raw in _iter_declared_side_effect_classes(root):
        checked += 1
        canon = _grammar_normalize_side_effect(raw)
        if not _grammar_is_known_side_effect(raw):
            report.errors.append(
                f"{where} declares unknown side_effect_class {raw!r} -- must be one of "
                f"{sorted(SIDE_EFFECT_CLASSES)}; an untyped/near-miss class dispatches ungated."
            )
            continue
        if canon == "none":
            continue
        if canon not in declared_policy:
            report.errors.append(
                f"{where} uses side_effect_class {canon!r} but it is not declared in "
                f"side_effect_policy (allowed/approval_required/forbidden) -- an "
                f"undeclared side effect is ungoverned."
            )
        if _grammar_is_external_side_effect(canon) and canon not in gated_policy:
            report.errors.append(
                f"{where} performs an external side effect ({canon!r}) that is neither "
                f"approval-gated nor forbidden -- it would dispatch autonomously. Add it "
                f"to side_effect_policy.approval_required or .forbidden."
            )
    report.checked["side_effect_classes"] = checked


def check_contract_invariants(contract: Any) -> InvariantReport:
    """Validate the structural grammar of a board operating contract.

    Returns an :class:`InvariantReport`; ``ok`` is False when any hard invariant
    (an ``error``) is violated. Tolerant of both the land-style
    (``workflow.stages`` + ``runtime.reactive_entities``) and synthesized-style
    (``entities`` + ``event_loops``) contract shapes.
    """
    report = InvariantReport(ok=True)
    root = _contract_root(contract)
    _check_stages(root, report)
    _check_reactive_entities(root, report)
    _check_event_loops(root, report)
    _check_tunables(root, report)
    _check_side_effect_classes(root, report)
    report.ok = not report.errors
    return report
