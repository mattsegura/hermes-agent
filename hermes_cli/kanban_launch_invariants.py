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
    SENSOR_KINDS,
    SENSOR_OPTIONAL_KNOBS,
    SENSOR_REQUIRED_KNOBS,
    SIDE_EFFECT_CLASSES,
    TERMINAL_OUTCOME_CLASSES,
    has_inbound as _grammar_has_inbound,
    has_timer as _grammar_has_timer,
    is_external_side_effect_class as _grammar_is_external_side_effect,
    is_known_side_effect_class as _grammar_is_known_side_effect,
    loop_declared_terminal_classes as _grammar_loop_terminal_classes,
    normalize_side_effect_class as _grammar_normalize_side_effect,
    normalize_sensor_kind as _grammar_normalize_sensor_kind,
    trigger_kind as _grammar_trigger_kind,
)

#: Side-effect classes whose actions cross an *irreversible* boundary (publish,
#: delete, sign, move money). These MUST be explicitly gated (R5): an
#: approval_gate ``required_before`` the action, or the class declared in the
#: board ``side_effect_policy`` (approval_required / forbidden). The reversible
#: external class is governed by the existing B3 rail; R5 is the stricter rail
#: for the actions you can never take back.
IRREVERSIBLE_SIDE_EFFECT_CLASSES: frozenset[str] = frozenset(
    {"external_irreversible", "financial"}
)

#: Trigger fields the runtime reads as a positive timer cadence (mirrors
#: :func:`hermes_cli.kanban_reactive_runtime.cadence_hours`). A typed timer
#: (R2) must resolve a positive cadence from one of these, or from a managed
#: board cadence knob.
_CADENCE_HOUR_FIELDS: tuple[str, ...] = ("cadence_hours", "every_hours", "interval_hours")
_CADENCE_MINUTE_FIELDS: tuple[str, ...] = ("cadence_minutes", "interval_minutes")
_CADENCE_SECOND_FIELDS: tuple[str, ...] = ("cadence_seconds", "interval_seconds")
_ALL_CADENCE_FIELDS: tuple[str, ...] = (
    _CADENCE_HOUR_FIELDS + _CADENCE_MINUTE_FIELDS + _CADENCE_SECOND_FIELDS
)

#: Fields on a trigger/loop/stage that explicitly NAME a tunable knob the
#: runtime is expected to bind to (R6). A value here that does not resolve to a
#: bounded tunable is a dangling reference and is rejected.
_KNOB_REFERENCE_FIELDS: tuple[str, ...] = (
    "cadence_knob",
    "cadence_tunable",
    "max_nudges_knob",
    "nudge_knob",
    "binds_knob",
    "tunable_ref",
    "knob_ref",
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


def _stage_emits_external_side_effect(stage: dict[str, Any]) -> bool:
    """True iff the stage or one of its actions declares an external side effect.

    Detected STRUCTURALLY off the typed ``side_effect_class`` primitive (R3) --
    not by scanning free text -- so renaming a stage/label can no longer dodge
    the conversational-stage rail. ``external_*``/``financial`` cross the board
    boundary; ``none``/``internal`` do not.
    """
    if _grammar_is_external_side_effect(stage.get("side_effect_class")):
        return True
    for action in _as_list(stage.get("actions")):
        if isinstance(action, dict) and _grammar_is_external_side_effect(
            action.get("side_effect_class")
        ):
            return True
    return False


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
        emits_external = _stage_emits_external_side_effect(stage)
        # A "conversational watch stage" is one that holds an ongoing back-and-forth.
        # It is detected two ways so renaming free text cannot dodge the rail:
        #   (a) classic shape: internal substates AND an inbound trigger; or
        #   (b) R3 structural shape: an inbound trigger AND a typed external
        #       side effect (it talks to an outside party and acts on the world),
        #       even with no substates -- the evasion the old check missed.
        is_watch_stage = has_inbound and (bool(substates) or emits_external)
        if is_watch_stage:
            conversational += 1
            if not _grammar_has_timer(triggers):
                reason = (
                    "has substates + inbound trigger"
                    if substates
                    else "consumes inbound and emits an external side effect"
                )
                report.errors.append(
                    f"stage '{key}' is conversational ({reason}) "
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


def _as_number(value: Any) -> Optional[float]:
    """Return ``value`` as a float iff it is a real number (not bool/str)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _spec_numeric_range(spec: dict[str, Any]) -> Optional[tuple[Optional[float], Optional[float], bool]]:
    """Return ``(lo, hi, declared)`` for a numeric range spec.

    ``declared`` is True when the spec carries a numeric ``range``/``min``+``max``
    shape (even a malformed one), so R1 can distinguish "no numeric range
    declared" (discrete/allowed knob) from "declared but degenerate".
    """
    rng = spec.get("range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        return _as_number(rng[0]), _as_number(rng[1]), True
    if "min" in spec or "max" in spec:
        return _as_number(spec.get("min")), _as_number(spec.get("max")), True
    return None


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


def _iter_declared_loop_terminal_classes(loop: dict[str, Any]):
    """Yield ``(state, raw_class)`` for every declared terminal-state class.

    Mirrors the two declaration shapes the grammar accepts: ``{state, class}``
    objects embedded in ``terminal_states`` and a sibling ``terminal_classes``
    map. Only entries that actually carry a class are yielded; a loop with no
    declared class yields nothing (it did NOT opt in -- legacy substring path).
    """
    for src in ("terminal_states", "terminal_state"):
        items = loop.get(src)
        if isinstance(items, (list, tuple)):
            for item in items:
                if isinstance(item, dict) and (item.get("class") or item.get("kind")):
                    state = str(
                        item.get("state") or item.get("key") or item.get("outcome") or ""
                    ).strip()
                    yield (state or "(unnamed)", item.get("class") or item.get("kind"))
    classes_map = loop.get("terminal_classes")
    if isinstance(classes_map, dict):
        for state, cls in classes_map.items():
            yield (str(state or "(unnamed)").strip(), cls)


def _check_loop_terminal_classes(
    name: str, loop: dict[str, Any], report: InvariantReport
) -> None:
    """9(b): a DECLARED terminal-outcome class must be in the closed vocabulary.

    Opt-in: a loop with no declared classes is unaffected (legacy path). When a
    loop DOES declare classes, an unknown class is an error -- the reward rail
    would otherwise silently drop it and the loop would credit on the substring
    fallback, exactly the infer-from-strings failure this work removes.
    """
    for state, raw in _iter_declared_loop_terminal_classes(loop):
        canon = str(raw or "").strip().lower()
        if canon not in TERMINAL_OUTCOME_CLASSES:
            report.errors.append(
                f"event loop '{name}' terminal state '{state}' declares unknown "
                f"outcome class {raw!r} -- must be one of {sorted(TERMINAL_OUTCOME_CLASSES)}; "
                f"an untyped class is silently dropped and the loop credits on the "
                f"substring fallback instead of the declared win."
            )


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
        # 9(b) DECLARE-DON'T-INFER: when a loop OPTS IN to declared terminal
        # classes, every declared class MUST be a member of the closed
        # vocabulary. A fat-fingered class (e.g. "wonn" / "victory") would
        # otherwise be silently dropped and the loop would fall back to the
        # substring reward path -- so it must surface at intake, not run blind.
        _check_loop_terminal_classes(name, loop, report)
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
    # Entities (synthesized shape) should declare terminal states.
    entities = [e for e in _as_list(root.get("entities")) if isinstance(e, dict)]
    report.checked["entities"] = len(entities)
    declared_entity_keys: set[str] = set()
    for ent in entities:
        name = str(ent.get("key") or ent.get("type") or "entity")
        for k in (ent.get("key"), ent.get("type")):
            if isinstance(k, str) and k.strip():
                declared_entity_keys.add(k.strip().lower())
        if _stop_signal_count(ent.get("terminal_states")) == 0:
            report.warnings.append(
                f"entity '{name}' declares no terminal_states -- confirm how it completes."
            )
    # R4: an event loop that watches an entity which is not declared anywhere is
    # an immortal watcher by construction -- the runtime stop-condition evaluator
    # has nothing to bind the entity's terminal states to, so the loop can never
    # learn the watched thing resolved. (Only enforced when entities are declared
    # at all; the land-style reactive_entities shape uses a different mechanism.)
    if entities:
        for loop in loops:
            ref = loop.get("entity")
            if (
                isinstance(ref, str)
                and ref.strip()
                and ref.strip().lower() not in declared_entity_keys
            ):
                lname = str(loop.get("entity") or loop.get("type") or "loop")
                report.errors.append(
                    f"event loop '{lname}' watches entity {ref!r} that is not declared in "
                    f"'entities' -- the runtime stop-condition evaluator cannot resolve it."
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
        _check_knob_range_sanity(knob, spec, report)


def _check_knob_range_sanity(knob: str, spec: dict[str, Any], report: InvariantReport) -> None:
    """R1: a knob's bounds must be sane -- ``min < max``, numeric, default in range.

    Rejects inverted (``min > max``) and degenerate (``min == max``) numeric
    ranges, non-numeric range bounds, and a ``default`` that falls outside the
    declared ``[min, max]`` (numeric) or ``allowed`` set (discrete). A
    structurally-nonsensical range makes the bounded action space ill-defined,
    so the optimizer could never carve safe arms from it.
    """
    numeric = _spec_numeric_range(spec)
    default = spec.get("default")
    if numeric is not None:
        lo, hi, _declared = numeric
        if lo is None or hi is None:
            report.errors.append(
                f"tunable '{knob}' declares a non-numeric range bound -- a range must be "
                f"two numbers (min, max)."
            )
            return
        if lo >= hi:
            kind = "inverted" if lo > hi else "degenerate (min == max)"
            report.errors.append(
                f"tunable '{knob}' has an {kind} range [{lo}, {hi}] -- require min < max so "
                f"the bounded action space is well-defined."
            )
            return
        dv = _as_number(default)
        if default is not None and dv is None:
            report.errors.append(
                f"tunable '{knob}' has a non-numeric default {default!r} for a numeric "
                f"range [{lo}, {hi}]."
            )
        elif dv is not None and not (lo <= dv <= hi):
            report.errors.append(
                f"tunable '{knob}' default {dv} is outside its range [{lo}, {hi}]."
            )
        return
    # Discrete allowed/options knob: a declared default must be a member.
    allowed = spec.get("allowed")
    if allowed is None:
        allowed = spec.get("options")
    if isinstance(allowed, (list, tuple)) and allowed and default is not None:
        dv = _as_number(default)

        def _matches(opt: Any) -> bool:
            if default == opt:
                return True
            ov = _as_number(opt)
            return dv is not None and ov is not None and dv == ov

        if not any(_matches(opt) for opt in allowed):
            report.errors.append(
                f"tunable '{knob}' default {default!r} is not one of its allowed values "
                f"{list(allowed)!r}."
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


def _root_tunables(root: dict[str, Any]) -> dict[str, Any]:
    tunables = root.get("tunables")
    if not isinstance(tunables, dict):
        tunables = _as_dict(root.get("runtime")).get("tunables")
    return tunables if isinstance(tunables, dict) else {}


def _iter_all_triggers(root: dict[str, Any]):
    """Yield ``(where, trigger)`` for every trigger on a stage or event loop."""
    workflow = _as_dict(root.get("workflow"))
    for stage in _as_list(workflow.get("stages")):
        if not isinstance(stage, dict):
            continue
        skey = str(stage.get("key") or stage.get("label") or "stage")
        for trig in _as_list(stage.get("triggers")):
            yield (f"stage '{skey}'", trig)
    for loop in _as_list(root.get("event_loops")):
        if not isinstance(loop, dict):
            continue
        lkey = str(loop.get("entity") or loop.get("type") or "loop")
        for trig in _as_list(loop.get("triggers")):
            yield (f"event loop '{lkey}'", trig)


def _trigger_positive_cadence(trigger: Any) -> bool:
    """True iff the trigger declares a positive timer cadence the runtime reads."""
    if not isinstance(trigger, dict):
        return False
    for key in _CADENCE_HOUR_FIELDS + _CADENCE_MINUTE_FIELDS + _CADENCE_SECOND_FIELDS:
        val = trigger.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return True
    return False


def _trigger_bad_cadence_field(trigger: Any) -> Optional[str]:
    """Return a description of a DECLARED-but-unusable cadence field, else None.

    A cadence field present with a non-positive number or a non-numeric value
    is degenerate -- the runtime cannot turn it into a real fire interval.
    """
    if not isinstance(trigger, dict):
        return None
    for key in _ALL_CADENCE_FIELDS:
        if key not in trigger:
            continue
        val = trigger.get(key)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return f"{key}={val!r} (not a number)"
        if val <= 0:
            return f"{key}={val} (must be > 0)"
    return None


def _has_managed_cadence_knob(root: dict[str, Any]) -> bool:
    """True iff a tunable knob supplies a positive managed timer cadence.

    Mirrors the optimizer's managed-cadence resolution: a knob whose name
    signals a cadence/follow-up interval and whose default is a positive
    number can drive a timer's fire interval, so a typed timer that binds to
    the board's cadence knob does NOT need its own literal ``cadence_hours``.
    """
    for knob, spec in _root_tunables(root).items():
        name = str(knob).lower()
        if any(tok in name for tok in ("cadence", "interval", "follow_up", "followup")):
            default = spec.get("default") if isinstance(spec, dict) else spec
            if isinstance(default, (int, float)) and not isinstance(default, bool) and default > 0:
                return True
    return False


def _check_timer_cadence(root: dict[str, Any], report: InvariantReport) -> None:
    """R2: a typed ``kind:"timer"`` loop must declare a usable cadence.

    Going-forward (typed) timers must resolve a positive fire interval: a
    positive ``cadence_hours``/``cadence_minutes``/... on the trigger, or a
    positive managed board cadence knob it binds to. A timer with no/zero/
    negative cadence can never decide when to fire. Any trigger (typed or
    legacy) that DECLARES a cadence field with a non-positive/non-numeric value
    is rejected outright -- a declared-but-degenerate cadence is always a bug.

    Legacy free-text timers (no explicit ``kind``) are upcast by the ingest
    shim and are not held to the typed-declaration bar here.
    """
    managed_cadence = _has_managed_cadence_knob(root)
    checked = 0
    for where, trig in _iter_all_triggers(root):
        bad = _trigger_bad_cadence_field(trig)
        if bad is not None:
            checked += 1
            report.errors.append(
                f"{where} declares a timer cadence {bad} -- a zero/negative/non-numeric "
                f"cadence is not a usable fire interval."
            )
            continue
        if _grammar_trigger_kind(trig) == "timer":
            checked += 1
            if not (_trigger_positive_cadence(trig) or managed_cadence):
                report.errors.append(
                    f"{where} is a typed timer but declares no usable cadence (a positive "
                    f"cadence_hours/cadence_minutes/... or a managed cadence knob) -- it can "
                    f"never decide when to fire."
                )
    report.checked["timer_cadences"] = checked


def _check_irreversible_gating(root: dict[str, Any], report: InvariantReport) -> None:
    """R5: every irreversible/financial action must be explicitly gated.

    An ``external_irreversible``/``financial`` side effect cannot be undone, so
    it must be gated by an ``approval_gates`` entry whose ``required_before``
    names the action, OR by the board ``side_effect_policy`` declaring the
    class in ``approval_required``/``forbidden``. An ungated irreversible/
    financial action would dispatch autonomously -- the most dangerous kind of
    ungoverned side effect.
    """
    _declared_policy, gated_policy = _policy_declared_classes(root)

    # Collect every action name guarded by an approval gate's ``required_before``.
    gated_actions: set[str] = set()
    for gate in _as_list(root.get("approval_gates")):
        if not isinstance(gate, dict):
            continue
        req = gate.get("required_before")
        for item in (_as_list(req) if not isinstance(req, str) else [req]):
            if isinstance(item, str) and item.strip():
                gated_actions.add(item.strip().lower())

    workflow = _as_dict(root.get("workflow"))
    checked = 0
    for stage in _as_list(workflow.get("stages")):
        if not isinstance(stage, dict):
            continue
        skey = str(stage.get("key") or stage.get("label") or "stage")
        for action in _as_list(stage.get("actions")):
            if not isinstance(action, dict):
                continue
            canon = _grammar_normalize_side_effect(action.get("side_effect_class"))
            if canon not in IRREVERSIBLE_SIDE_EFFECT_CLASSES:
                continue
            checked += 1
            akey = str(action.get("key") or action.get("label") or "action")
            action_names = {
                str(action.get(k)).strip().lower()
                for k in ("key", "label")
                if isinstance(action.get(k), str) and str(action.get(k)).strip()
            }
            gate_covers = bool(action_names & gated_actions)
            policy_covers = canon in gated_policy
            if not (gate_covers or policy_covers):
                report.errors.append(
                    f"stage '{skey}' action '{akey}' has an irreversible side_effect_class "
                    f"({canon!r}) but is neither named in any approval_gate.required_before "
                    f"nor governed by side_effect_policy (approval_required/forbidden) -- an "
                    f"ungated irreversible/financial action would dispatch autonomously."
                )
    report.checked["irreversible_actions"] = checked


def _check_knob_references(root: dict[str, Any], report: InvariantReport) -> None:
    """R6: a knob referenced by a loop/stage must exist as a bounded tunable.

    When a trigger/loop/stage explicitly NAMES a tunable knob (e.g.
    ``cadence_knob: "follow_up_interval_hours"``), that knob must exist in
    ``tunables`` AND declare a bounded range/allowed set. A dangling reference
    (or a reference to an unbounded knob) means the runtime would bind to a
    knob it cannot resolve or safely tune.
    """
    tunables = _root_tunables(root)

    def _knob_is_bounded(name: str) -> bool:
        spec = tunables.get(name)
        if not isinstance(spec, dict):
            return False
        return (
            ("range" in spec)
            or ("min" in spec and "max" in spec)
            or ("allowed" in spec)
            or ("options" in spec)
        )

    checked = 0
    sources: list[tuple[str, Any]] = list(_iter_all_triggers(root))
    for loop in _as_list(root.get("event_loops")):
        if isinstance(loop, dict):
            sources.append((f"event loop '{loop.get('entity') or loop.get('type') or 'loop'}'", loop))
    workflow = _as_dict(root.get("workflow"))
    for stage in _as_list(workflow.get("stages")):
        if isinstance(stage, dict):
            sources.append((f"stage '{stage.get('key') or stage.get('label') or 'stage'}'", stage))

    for where, obj in sources:
        if not isinstance(obj, dict):
            continue
        for field_name in _KNOB_REFERENCE_FIELDS:
            ref = obj.get(field_name)
            if not isinstance(ref, str) or not ref.strip():
                continue
            checked += 1
            knob_name = ref.strip()
            if knob_name not in tunables:
                report.errors.append(
                    f"{where} references knob {knob_name!r} via '{field_name}' but it is not "
                    f"declared in 'tunables' -- a dangling knob reference the runtime cannot bind."
                )
            elif not _knob_is_bounded(knob_name):
                report.errors.append(
                    f"{where} references knob {knob_name!r} via '{field_name}' but that knob "
                    f"declares no bounded range/allowed set -- it is unsafe to bind/tune."
                )
    report.checked["knob_references"] = checked


def _check_sensors(root: dict[str, Any], report: InvariantReport) -> None:
    """S1-S3: Tier-1 sensor primitives are typed, bounded, and gating-consistent.

    Mirrors the trigger/side-effect rails for the new ``sensors`` block:

    * **S1 (typed kind).** Every declared sensor must carry a ``kind`` from the
      closed :data:`SENSOR_KINDS` vocabulary -- an unknown/missing kind cannot
      be wired to a detector and is rejected.
    * **S2 (bounded-knob thresholds).** Every threshold/window the sensor needs
      (the kind's required knobs) must be bound, via the sensor's ``knobs`` map,
      to a board ``tunables`` knob that declares a bounded range/allowed set --
      so the optimizer can tune it and a P5 amendment can move it. A missing
      required binding, a dangling binding (names a knob that does not exist),
      or a binding to an unbounded knob is rejected. (DECLARE, DON'T INFER: no
      inline magic thresholds.)
    * **S3 (circuit gating consistency).** A circuit breaker that gates an
      ``external_*``/``financial`` side-effect path must be consistent with the
      approval rail: that class must be governed in ``side_effect_policy``
      (approval_required/forbidden), exactly like B3 -- a breaker cannot pretend
      to gate a path the board never declared as governed.
    """
    sensors = _as_list(root.get("sensors"))
    report.checked["sensors"] = len(sensors)
    if not sensors:
        return
    tunables = _root_tunables(root)
    _declared_policy, gated_policy = _policy_declared_classes(root)

    def _knob_is_bounded(name: str) -> bool:
        spec = tunables.get(name)
        if not isinstance(spec, dict):
            return False
        return (
            ("range" in spec)
            or ("min" in spec and "max" in spec)
            or ("allowed" in spec)
            or ("options" in spec)
        )

    for idx, sensor in enumerate(sensors):
        if not isinstance(sensor, dict):
            report.errors.append(f"sensors[{idx}] must be an object/dict.")
            continue
        kind = _grammar_normalize_sensor_kind(sensor.get("kind"))
        skey = str(sensor.get("key") or sensor.get("kind") or f"sensor[{idx}]")
        if kind is None:
            report.errors.append(
                f"sensor '{skey}' declares unknown/missing kind {sensor.get('kind')!r} -- "
                f"must be one of {sorted(SENSOR_KINDS)}; an untyped sensor cannot be wired."
            )
            continue
        knobs = sensor.get("knobs") if isinstance(sensor.get("knobs"), dict) else {}
        required = SENSOR_REQUIRED_KNOBS.get(kind, ())
        optional = SENSOR_OPTIONAL_KNOBS.get(kind, ())
        for logical in required:
            binding = knobs.get(logical)
            if not isinstance(binding, str) or not binding.strip():
                report.errors.append(
                    f"sensor '{skey}' ({kind}) does not bind its required '{logical}' "
                    f"threshold to a tunable knob -- a sensor threshold must be a bounded, "
                    f"tunable knob, not an inline value."
                )
                continue
            name = binding.strip()
            if name not in tunables:
                report.errors.append(
                    f"sensor '{skey}' binds '{logical}' to knob {name!r} which is not "
                    f"declared in 'tunables' -- a dangling sensor knob reference."
                )
            elif not _knob_is_bounded(name):
                report.errors.append(
                    f"sensor '{skey}' binds '{logical}' to knob {name!r} which declares no "
                    f"bounded range/allowed set -- it is unsafe to tune."
                )
        # Optional knobs, when bound, must also resolve to a bounded tunable.
        for logical in optional:
            binding = knobs.get(logical)
            if isinstance(binding, str) and binding.strip():
                name = binding.strip()
                if name not in tunables:
                    report.errors.append(
                        f"sensor '{skey}' binds optional '{logical}' to knob {name!r} which "
                        f"is not declared in 'tunables' -- a dangling sensor knob reference."
                    )
                elif not _knob_is_bounded(name):
                    report.errors.append(
                        f"sensor '{skey}' binds optional '{logical}' to knob {name!r} which "
                        f"declares no bounded range/allowed set -- it is unsafe to tune."
                    )
        # S3: a circuit breaker gating an external side-effect path must be
        # governed by the approval rail (consistent with B3).
        if kind == "circuit_breaker":
            gate_raw = sensor.get("gates_side_effect_class")
            if gate_raw is not None:
                canon = _grammar_normalize_side_effect(gate_raw)
                if canon is None:
                    report.errors.append(
                        f"circuit breaker '{skey}' gates unknown side_effect_class "
                        f"{gate_raw!r} -- must be one of {sorted(SIDE_EFFECT_CLASSES)}."
                    )
                elif _grammar_is_external_side_effect(canon) and canon not in gated_policy:
                    report.errors.append(
                        f"circuit breaker '{skey}' gates an external side effect ({canon!r}) "
                        f"that is neither approval-gated nor forbidden in side_effect_policy "
                        f"-- the gated path is ungoverned. Add {canon!r} to "
                        f"side_effect_policy.approval_required or .forbidden."
                    )


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
    _check_timer_cadence(root, report)
    _check_irreversible_gating(root, report)
    _check_knob_references(root, report)
    _check_sensors(root, report)
    report.ok = not report.errors
    return report
