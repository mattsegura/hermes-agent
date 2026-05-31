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
    contract_opts_into_step9 as _grammar_contract_opts_into_step9,
    loop_declared_terminal_classes as _grammar_loop_terminal_classes,
    loop_opts_into_defer_bound as _grammar_loop_opts_into_defer_bound,
    loop_opts_into_step9 as _grammar_loop_opts_into_step9,
    loop_opts_into_terminal_classes as _grammar_loop_opts_into_terminal_classes,
    normalize_side_effect_class as _grammar_normalize_side_effect,
    normalize_sensor_kind as _grammar_normalize_sensor_kind,
    side_effect_policy_opts_into_strict as _grammar_policy_opts_into_strict,
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
    # ROUND-5 FIX 1 (byte-identity by construction): the LEGACY (non-opted) intake
    # path traverses this helper, so its acceptance set must be BYTE-FOR-BYTE base
    # 019271994. The base implementation gated strings with ``.isdigit()``:
    #
    #     if isinstance(value, str) and value.strip().isdigit():
    #         return int(value.strip())
    #
    # The round-4 rewrite swapped that for a bare ``int(value.strip())`` (then a
    # >=0 reject), which WIDENED the acceptance set for the launch gate: a string
    # base rejected via ``.isdigit()`` could now parse and flip a DEFAULT-OFF
    # contract's invariants errors/warnings (and via the persisted invariants
    # block, the contract hash). We restore the EXACT base ``.isdigit()`` gate so
    # every input keeps its base decision. The ONLY change is wrapping the rare
    # ``.isdigit()``-True/``int()``-raises char ('³','①') so it returns None
    # instead of crashing the per-loop intake check -- proven byte-for-byte
    # against base for the full fuzz vector (no acceptance-set change otherwise).
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        try:
            return int(value.strip())
        except (ValueError, TypeError):
            return None
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
                if not isinstance(item, dict):
                    continue
                # FIX 7(b): yield whenever the class/kind KEY is PRESENT, even if
                # its value is falsey (None / "" / 0). Previously the truthiness
                # gate (``item.get("class") or item.get("kind")``) silently
                # dropped an inline ``class: null`` so a half-declared shape-1
                # class escaped validation, while the sibling-map (shape-2)
                # branch flagged the same defect. Mirror shape-2 here so a
                # fat-fingered/empty inline class surfaces at intake.
                if "class" in item or "kind" in item:
                    state = str(
                        item.get("state") or item.get("key") or item.get("outcome") or ""
                    ).strip()
                    raw = item.get("class") if "class" in item else item.get("kind")
                    yield (state or "(unnamed)", raw)
    classes_map = loop.get("terminal_classes")
    if isinstance(classes_map, dict):
        for state, cls in classes_map.items():
            yield (str(state or "(unnamed)").strip(), cls)


def _check_loop_terminal_classes(
    name: str, loop: dict[str, Any], report: InvariantReport
) -> None:
    """9(b): a DECLARED terminal-outcome class must be in the closed vocabulary.

    Opt-in: a loop with no declared classes is unaffected (legacy path). When a
    loop DOES declare classes:

    * an unknown class is an error -- the reward rail would otherwise silently
      drop it and the loop would credit on the substring fallback, exactly the
      infer-from-strings failure this work removes.
    * FIX 7(a) coverage: once a loop opts in, the reward rail is governed ONLY by
      the declared map (no substring fallback), so a loop that opts in but
      declares NO ``win`` class can NEVER earn the conversion reward for its real
      win (silent zero-credit). Require at least one declared ``win`` when the
      loop opts in, and warn when a declared terminal_state has no class (a
      partial map leaves the unlisted state crediting nothing).
    """
    declared: list[tuple[str, Any]] = list(_iter_declared_loop_terminal_classes(loop))
    if not declared:
        return  # did not opt in -> legacy path, unaffected
    has_win = False
    win_missing_state = False
    classed_states: set[str] = set()
    for state, raw in declared:
        canon = str(raw or "").strip().lower()
        state_norm = str(state or "").strip().lower()
        # The yielders substitute a placeholder ('(unnamed)') for a missing state;
        # treat that and a literally-empty state as NO usable state.
        state_present = bool(state_norm) and state_norm != "(unnamed)"
        classed_states.add(state_norm)
        if canon == "win":
            # STEP-9 (LOW, round-3): a declared 'win' with an EMPTY/missing state
            # can never be matched by the reward rail (the outcome is compared to
            # the declared state by exact name), so it does NOT count toward
            # has_win -- otherwise an opted-in loop with {"": "win"} would pass the
            # no-win check yet still credit zero forever.
            if state_present:
                has_win = True
            else:
                win_missing_state = True
        if canon not in TERMINAL_OUTCOME_CLASSES:
            report.errors.append(
                f"event loop '{name}' terminal state '{state}' declares unknown "
                f"outcome class {raw!r} -- must be one of {sorted(TERMINAL_OUTCOME_CLASSES)}; "
                f"an untyped class is silently dropped and the loop credits on the "
                f"substring fallback instead of the declared win."
            )
    # FIX 7(a): an opted-in loop with no declared 'win' can never credit a
    # conversion -- surface it as an error so the silent zero-credit is caught.
    if not has_win:
        if win_missing_state:
            report.errors.append(
                f"event loop '{name}' declares a 'win' terminal class with an "
                f"EMPTY/missing state -- the reward rail matches a win by its declared "
                f"state name, so a stateless 'win' can never credit. Declare the win "
                f"terminal's state name."
            )
        else:
            report.errors.append(
                f"event loop '{name}' declares terminal_classes but NONE is a 'win' -- "
                f"an opted-in loop credits a conversion ONLY from a declared 'win' "
                f"(no substring fallback), so it would earn zero conversion reward "
                f"forever. Declare the loop's win terminal as class 'win'."
            )
    # FIX 7(a) coverage: a declared terminal_state with no class in a
    # partially-declared map silently credits nothing (the legacy fallback is
    # bypassed once the loop opts in). Warn so the gap surfaces.
    for ts in _as_list(loop.get("terminal_states")):
        if isinstance(ts, str):
            key = ts.strip().lower()
            if key and key not in classed_states:
                report.warnings.append(
                    f"event loop '{name}' declares terminal_classes but terminal "
                    f"state '{ts}' has no declared class -- once a loop opts in, an "
                    f"unclassed terminal credits nothing (the legacy substring "
                    f"fallback is bypassed)."
                )


#: The EXACT loop keys the runtime reads for each opt-in safety control (FIX
#: 7d). A near-miss key (a typo) is silently inert -- the operator believes
#: protection is on while the board runs unprotected -- so a near-miss surfaces
#: as a warning at intake. Mirrors kanban_reactive_runtime.loop_max_defers /
#: loop_max_nudges so the recognized set is the single source of truth.
_RECOGNIZED_MAX_DEFERS_KEYS: frozenset[str] = frozenset(
    {"max_defers", "max_deferrals", "max_defer"}
)
_RECOGNIZED_MAX_NUDGES_KEYS: frozenset[str] = frozenset(
    {"max_nudges", "max_follow_ups", "max_followups", "max_retries"}
)

#: ROUND-5 FIX 3: SQLite stores INTEGER columns as a signed 64-bit value. A bound
#: that coerces to an int OUTSIDE this range (e.g. a 25-digit string ``'9'*25``)
#: passes the coercion at intake but raises ``OverflowError: Python int too large
#: to convert to SQLite INTEGER`` when the per-loop compile binds it, silently
#: dropping the ENTIRE timer schedule (the loop disappears). For an OPTED-IN
#: bound we reject an out-of-range value at intake (here) AND clamp fail-closed at
#: persist (``_persist_reactive_timer_schedule``) so the loop is never silently
#: dropped. A non-negative cap can never be negative, so only the max matters in
#: practice, but we keep both edges for completeness.
_SQLITE_INTEGER_MAX: int = (1 << 63) - 1
_SQLITE_INTEGER_MIN: int = -(1 << 63)


def _edit_distance(a: str, b: str, *, cap: int) -> int:
    """Levenshtein distance between ``a`` and ``b``, short-circuited at ``cap``+1.

    Returns ``cap + 1`` as soon as the distance is known to exceed ``cap`` so the
    near-miss test below stays cheap and is a TRUE bounded-edit-distance check
    (not a substring/shared-prefix guess, which over- and under-matched).
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            cur.append(v)
            if v < row_min:
                row_min = v
        if row_min > cap:
            return cap + 1
        prev = cur
    return prev[-1]


#: FIX 5 (round-2): a small CLOSED set of KNOWN aliases/typos for each safety
#: opt-in key. Declare-don't-infer: instead of guessing intent from a substring
#: similarity score (which simultaneously matched unrelated ``max_depth`` /
#: ``max_followers`` and MISSED real typos like ``strikt``), we enumerate the
#: handful of plausible misspellings/aliases for each recognized key. A key is a
#: near-miss only if it is in this set OR within a tight edit distance of a
#: recognized key -- never merely because it shares a 6-char prefix.
_KNOWN_KEY_ALIASES: dict[str, frozenset[str]] = {
    "strict": frozenset({
        "strict", "strikt", "stict", "stirct", "stricct", "stricr",
        "strict_mode", "strictmode", "strict_enabled", "strict_enforcement",
        "is_strict", "enforce_strict", "strictly", "strcit", "default_deny",
        "default_deny_mode",
    }),
    "max_defers": frozenset({
        "max_defers", "max_deferals", "max_deferrals", "max_deferals_",
        "max_defer", "maxdefers", "max_deffers", "max_defferals", "defer_max",
        "defers_max", "max_deferral", "deferral_cap", "defer_cap", "max_defrs",
    }),
    "max_deferrals": frozenset({
        "max_deferrals", "max_deferals", "max_defers", "max_deferral",
        "maxdeferrals", "deferral_cap",
    }),
    "max_defer": frozenset({"max_defer", "max_defers", "max_deferals"}),
    "max_nudges": frozenset({
        "max_nudges", "max_nudge", "maxnudges", "max_nudgs", "nudge_max",
        "nudges_max", "max_nudg", "nudge_cap", "max_nudges_",
    }),
    "max_follow_ups": frozenset({
        "max_follow_ups", "max_followups", "max_follow_up", "max_followup",
        "maxfollowups", "max_follwups", "max_followp",
    }),
    "max_followups": frozenset({
        "max_followups", "max_follow_ups", "max_followup", "max_follow_up",
    }),
    "max_retries": frozenset({
        "max_retries", "max_retry", "max_retrys", "maxretries", "max_reties",
        "max_retires", "retry_max", "retries_max", "retry_cap",
    }),
}


def _near_miss(key: str, recognized: frozenset[str]) -> Optional[str]:
    """Return the recognized key a near-miss ``key`` most likely meant, else None.

    FIX 5 (round-2): a "near miss" is an UNRECOGNIZED key that is EITHER a known
    alias/typo of a recognized key (a CLOSED, enumerated set) OR within a tight
    bounded edit distance of it. The previous ``startswith(r[:6])`` /
    substring-containment rule was an infer-from-strings heuristic that was both
    over-inclusive (legit unrelated keys ``max_depth`` / ``max_delay`` /
    ``max_followers`` / ``district`` cried wolf) and under-inclusive (real typos
    ``strikt`` produced nothing). This declare-don't-infer form catches a typo'd
    safety opt-in (``strict_mode``/``Strict``/``strikt`` for ``strict``,
    ``max_deferals`` for ``max_defers``) without flagging genuinely unrelated
    knobs.
    """
    k = key.strip().lower()
    if not k or k in recognized:
        return None
    # 1) Exact membership of a recognized key's CLOSED alias/typo set.
    for r in recognized:
        aliases = _KNOWN_KEY_ALIASES.get(r)
        if aliases is not None and k in aliases:
            return r
    # 2) A bounded-edit-distance typo of a recognized key. The closed alias set
    # above already covers the common multi-char typos/aliases, so the distance
    # fallback is tight and PER-KEY:
    #
    #   * ``strict`` is a SHORT safety token (6 chars) with NO legitimate
    #     2-edit neighbor in any contract vocabulary, so STEP-9 (MED, round-3)
    #     raises its cap to 2: a 2-edit typo (``striqt`` / ``strikct``) that the
    #     alias set misses still surfaces, and a silently-inert default-deny
    #     opt-in is never missed because of an unenumerated misspelling.
    #   * the longer ``max_*`` keys KEEP cap 1: they have legitimate 2-edit
    #     neighbors (``max_followers`` is distance-2 from ``max_followups``;
    #     ``max_replies`` is distance-2 from ``max_retries``) that must NOT cry
    #     wolf.
    #
    # The SAME-first-character guard stays so a near-miss never matches a
    # different word.
    best: Optional[tuple[int, str]] = None
    for r in recognized:
        if not r or not k or r[0] != k[0]:
            continue
        cap = 2 if r == "strict" else 1
        d = _edit_distance(k, r, cap=cap)
        if 1 <= d <= cap and (best is None or d < best[0]):
            best = (d, r)
    return best[1] if best is not None else None


def _loop_has_safety_key_near_miss(loop: dict[str, Any]) -> bool:
    """ROUND-5 FIX 4: does ``loop`` carry a NEAR-MISS of a step-9 safety key?

    True iff the loop declares a key that is a near-miss (typo/alias) of a
    recognized anti-zombie / nudge-cap key but is NOT itself an exact recognized
    key. Used to run :func:`_check_safety_optin_keys` INDEPENDENTLY of the opt-in
    gate (see :func:`_check_event_loops`): a loop whose ONLY step-9 intent is a
    TYPO'd safety key (``max_deferals``) never opts in via the grammar (the
    grammar matches EXACT keys), so without this the typo-warning could never
    fire. A loop with NO such near-miss returns False -> no new finding ->
    byte-identical to base for every legacy loop.
    """
    for key in loop.keys():
        if not isinstance(key, str):
            continue
        if key in _RECOGNIZED_MAX_DEFERS_KEYS or key in _RECOGNIZED_MAX_NUDGES_KEYS:
            continue
        if _near_miss(key, _RECOGNIZED_MAX_DEFERS_KEYS) or _near_miss(
            key, _RECOGNIZED_MAX_NUDGES_KEYS
        ):
            return True
    return False


def _check_safety_optin_keys(
    name: str, loop: dict[str, Any], report: InvariantReport
) -> None:
    """FIX 7(d): warn on a typo'd opt-in SAFETY key so it is not silently inert.

    The runtime reads EXACT keys (``max_defers``/aliases for the anti-zombie
    bound). A near-miss key (``max_deferals`` with one 'r') is ignored, leaving
    the board on the legacy fall-through with no error -- the operator believes
    a cap is set while it is fully inert. Surface the near-miss at intake.

    ROUND-5 FIX 4 (self-defeating gate): this check used to run ONLY when the loop
    had ALREADY opted into step-9 (via the grammar's EXACT-key match on a real
    bound or a terminal-class declaration). A loop whose ONLY step-9 intent was a
    TYPO'd safety key (``max_deferals``) therefore never opted in, so the typo
    warning -- the one finding that exists precisely to catch that typo -- could
    never fire: the gate defeated itself. It is now also invoked DIRECTLY from
    :func:`_check_event_loops` whenever the loop carries a safety-key near-miss
    (independent of the opt-in gate), so a typo'd safety opt-in is always
    surfaced. It only EVER appends a WARNING for a genuine near-miss (no gate
    change), so a legacy loop with no near-miss stays byte-identical to base.
    """
    for key in loop.keys():
        if not isinstance(key, str):
            continue
        if key in _RECOGNIZED_MAX_DEFERS_KEYS or key in _RECOGNIZED_MAX_NUDGES_KEYS:
            continue
        meant = _near_miss(key, _RECOGNIZED_MAX_DEFERS_KEYS)
        if meant:
            report.warnings.append(
                f"event loop '{name}' declares {key!r} which looks like a typo of "
                f"the anti-zombie bound {meant!r} -- the runtime reads only the exact "
                f"key, so this defer cap is silently inert. Did you mean {meant!r}?"
            )
            continue
        meant = _near_miss(key, _RECOGNIZED_MAX_NUDGES_KEYS)
        if meant:
            report.warnings.append(
                f"event loop '{name}' declares {key!r} which looks like a typo of the "
                f"nudge cap {meant!r} -- the runtime reads only the exact key, so this "
                f"cap is silently inert. Did you mean {meant!r}?"
            )


def _check_loop_bound_values(
    name: str, loop: dict[str, Any], report: InvariantReport
) -> None:
    """FIX 6 intake: a DECLARED but malformed defer/nudge bound must surface.

    ``max_defers: .inf`` / ``.nan`` (a legal YAML float) used to silently drop
    the whole loop at compile (``int(float('inf'))`` raised, the per-loop compile
    swallowed it, the board activated with 0 schedules). ``_coerce_int`` now
    guards non-finite floats, so the loop still compiles -- but a malformed bound
    is still a bug, so flag it at intake instead of silently ignoring it.

    FIX 2 / ROUND-4 FIX B: the string branch must agree with the runtime
    (``kanban_reactive_runtime._coerce_int``). The original gate
    ``not val.strip().lstrip('-').isdigit()`` passed unicode-digit chars int()
    rejects ('³','①') -- blind to the exact input that crashes/silently-drops the
    watcher at compile. Round-4 makes BOTH coercions a plain ``int(value.strip())``
    (no ascii narrowing): '³'/'①' are caught (-> None, flagged here, no crash),
    '٣' (Arabic-Indic 3, which int() PARSES) coerces to 3 in BOTH so it is a
    USABLE bound exactly like base 019271994 (NOT flagged), and a genuine NEGATIVE
    string '-1'/'-5' coerces to a negative int the runtime drops -- still flagged,
    matching the integer -1. A '-0' string coerces to 0 in both layers (a valid
    cap of 0), so it is NOT flagged: intake and runtime agree byte-for-byte.
    """
    import math as _math
    # STEP-9 (LOW, round-3): import the RUNTIME coercion so intake and runtime
    # agree on the '-0' edge (the runtime accepts it as 0). Runtime is a leaf that
    # imports only the grammar, so this cannot create a circular import.
    from hermes_cli.kanban_reactive_runtime import _coerce_int as _runtime_coerce_int

    def _flag_if_bad(keys: frozenset[str], label: str) -> None:
        for key in keys:
            if key not in loop:
                continue
            val = loop.get(key)
            bad = False
            fractional = False
            negzero = False
            overrange = False
            # ROUND-5 FIX 3: a bound that COERCES to a valid non-negative int but
            # is OUTSIDE the signed-64-bit range SQLite can store (e.g. ``'9'*25``)
            # passes every check below yet raises OverflowError when the per-loop
            # compile binds it to the INTEGER column -- silently dropping the whole
            # schedule. Detect it here (opt-in only) using the RUNTIME coercion (the
            # exact value that would be persisted) so the loop is flagged, not lost.
            coerced = _runtime_coerce_int(val)
            if coerced is not None and (
                coerced > _SQLITE_INTEGER_MAX or coerced < _SQLITE_INTEGER_MIN
            ):
                overrange = True
                bad = True
            elif isinstance(val, bool):
                bad = True
            elif isinstance(val, float) and not _math.isfinite(val):
                bad = True
            elif isinstance(val, float) and not val.is_integer():
                # STEP-9 (MED, round-3): a fractional float bound (e.g. 2.7) is
                # silently TRUNCATED by the runtime (``int(2.7) == 2``) -- the
                # author's 2.7 becomes a cap of 2 with no error. Flag it at intake
                # (opt-in only) rather than silently truncating. A negative
                # fractional is caught here too.
                fractional = True
                bad = True
            elif isinstance(val, (int, float)):
                bad = val < 0
            elif isinstance(val, str):
                # ROUND-5 FIX 1: a string bound is usable only if it coerces to a
                # non-negative int via the BASE-EXACT ``_coerce_nonneg_int`` (the
                # ``.isdigit()`` gate, int()-guarded, >=0). A str that does NOT so
                # coerce is bad. This flags '³'/'①' (the .isdigit()-True/int()-raises
                # crash class -- now caught to None, not a crash) and the negative
                # '-1'/'-5' strings, agreeing with the integer-(-1) flag above. A
                # non-ASCII but int()-PARSEABLE digit ('٣' Arabic-Indic 3) coerces to
                # 3 exactly like base, so it is a USABLE bound and is NOT flagged.
                bad = _coerce_nonneg_int(val) is None
                # STEP-9 (LOW, round-3): the '-0' family. The runtime's _coerce_int
                # ACCEPTS '-0' (it lstrips '-' before isdigit, then int('-0')==0),
                # so the runtime treats it as a VALID cap of 0 -- it is NOT "silently
                # dropped, leaving the loop unbounded". _coerce_nonneg_int rejects it
                # (its isdigit() sees the '-'), so intake still flags it, but the
                # generic "dropped/unbounded" message is WRONG. Detect the
                # negative-zero string (runtime coerces it to exactly 0) and emit an
                # accurate message instead.
                if bad and _runtime_coerce_int(val) == 0:
                    negzero = True
            else:
                bad = val is not None
            if bad:
                if overrange:
                    report.errors.append(
                        f"event loop '{name}' declares {label} {key}={val!r} which is "
                        f"OUTSIDE the range a 64-bit integer column can store -- the "
                        f"per-loop compile raises OverflowError binding it and silently "
                        f"DROPS the entire timer schedule (the loop disappears). Declare a "
                        f"bound between 0 and {_SQLITE_INTEGER_MAX}."
                    )
                elif fractional:
                    report.errors.append(
                        f"event loop '{name}' declares {label} {key}={val!r} which is a "
                        f"FRACTIONAL bound -- the runtime silently TRUNCATES it to "
                        f"{int(val)} (a different cap than the {val!r} you declared). "
                        f"Declare a whole non-negative integer bound."
                    )
                elif negzero:
                    report.errors.append(
                        f"event loop '{name}' declares {label} {key}={val!r} which the "
                        f"runtime coerces to a cap of 0 (terminate on the first attempt) "
                        f"-- not unbounded. Declare a canonical non-negative integer "
                        f"(use 0, not {val!r})."
                    )
                else:
                    report.errors.append(
                        f"event loop '{name}' declares {label} {key}={val!r} which is not "
                        f"a usable non-negative integer bound -- a malformed bound is "
                        f"silently dropped at runtime, leaving the loop unbounded."
                    )

    _flag_if_bad(_RECOGNIZED_MAX_DEFERS_KEYS, "defer bound")
    _flag_if_bad(_RECOGNIZED_MAX_NUDGES_KEYS, "nudge cap")


def _check_side_effect_policy_keys(
    root: dict[str, Any], report: InvariantReport
) -> None:
    """FIX 7(d): warn on a typo'd ``side_effect_policy.strict`` opt-in.

    A near-miss key (``strict_mode`` / ``strikt``) that the runtime does not read
    leaves DEFAULT-DENY enforcement off while the operator believes it is on.
    Surface the near-miss at intake.

    FIX 5 (round-2): also surface a CASE-VARIANT of ``strict`` (``Strict`` /
    ``STRICT``). The runtime now canonicalizes the key case
    (``_side_effect_strict_enabled`` lower-cases policy keys), so a capitalized
    opt-in IS honored -- but a case-variant is still a non-canonical declaration
    worth flagging so the contract is written in the canonical lower-case form,
    and a reviewer is never left wondering whether a capitalized key took effect.
    The previous detector lower-cased the key BEFORE the recognized-set check, so
    ``'Strict'.lower()=='strict'`` looked recognized and was skipped (no warning) --
    masking exactly the typo class this check exists to surface.
    """
    policy = _as_dict(root.get("side_effect_policy"))
    _RECOGNIZED_POLICY_KEYS = frozenset(
        {"strict", "allowed", "forbidden", "approval_required"}
    )
    for key in policy.keys():
        if not isinstance(key, str) or key in _RECOGNIZED_POLICY_KEYS:
            continue
        lowered = key.strip().lower()
        # A pure case-variant of an exact recognized key (e.g. 'Strict').
        if lowered == "strict":
            report.warnings.append(
                f"side_effect_policy declares {key!r} which is a CASE-VARIANT of the "
                f"DEFAULT-DENY opt-in 'strict'. It is honored (the runtime canonicalizes "
                f"the key case), but declare it as lower-case 'strict' for clarity."
            )
            continue
        if _near_miss(key, frozenset({"strict"})):
            report.warnings.append(
                f"side_effect_policy declares {key!r} which looks like a typo of the "
                f"DEFAULT-DENY opt-in 'strict' -- the runtime reads only 'strict', so "
                f"strict enforcement is silently inert. Did you mean 'strict'?"
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
        # STEP-9 OPT-IN GATE (the re-architecture principle): all step-9 loop
        # invariant checks run ONLY for a loop that opted in (declared a terminal
        # class or a defer bound). A loop that opted into NEITHER hits ZERO new
        # code paths here, so its errors/warnings are byte-identical to base
        # 019271994 BY CONSTRUCTION -- no new findings can perturb the report or
        # (via the persisted invariants block) the contract hash.
        opted_in = _grammar_loop_opts_into_step9(loop)
        if opted_in:
            # 9(b) DECLARE-DON'T-INFER: when a loop OPTS IN to declared terminal
            # classes, every declared class MUST be a member of the closed
            # vocabulary. A fat-fingered class (e.g. "wonn" / "victory") would
            # otherwise be silently dropped and the loop would fall back to the
            # substring reward path -- so it must surface at intake.
            _check_loop_terminal_classes(name, loop, report)
            # FIX 6 / FIX 7(d): a malformed defer/nudge bound, or a typo'd opt-in
            # safety key, must surface at intake instead of silently going inert.
            # Gated by opt-in so a legacy loop carrying an UNRELATED ``max_*`` key
            # (e.g. ``max_followers`` / ``max_nudges``) produces no new finding.
            _check_loop_bound_values(name, loop, report)
            _check_safety_optin_keys(name, loop, report)
        elif _loop_has_safety_key_near_miss(loop):
            # ROUND-5 FIX 4 (self-defeating gate): a loop whose ONLY step-9 intent
            # is a TYPO'd safety key (``max_deferals``) does NOT opt in via the
            # grammar (which matches EXACT keys), so the branch above never runs
            # and the typo-warning -- the finding that exists precisely to catch
            # that typo -- could never fire. Run the near-miss safety-key check
            # whenever the loop CONTAINS a near-miss of a step-9 safety key,
            # independent of the opt-in gate. This is WARNING-only (no gate
            # change). A legacy loop with no such near-miss takes neither branch
            # and is byte-identical to base.
            _check_safety_optin_keys(name, loop, report)
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
    # STEP-9 OPT-IN GATE: the FIX-7(c) sentinel rejection below is a NEW finding
    # (it changes the ERROR MESSAGE base produced for a ''/'null'/'-' class from
    # the generic "unknown side_effect_class" to a sentinel-specific message). It
    # runs ONLY for a contract that opted into step-9, so a pure-legacy contract
    # gets the BASE "unknown side_effect_class" error verbatim -- byte-identical.
    optin = _grammar_contract_opts_into_step9(root)
    checked = 0
    for where, raw in _iter_declared_side_effect_classes(root):
        checked += 1
        canon = _grammar_normalize_side_effect(raw)
        # FIX 7(c): a side_effect_class that NORMALIZES AWAY to NULL at
        # persistence (a sentinel string: ''/whitespace/'null'/'-', but NOT the
        # benign literal 'none') is silently downgraded to the no-side-effect
        # 'none' at runtime -- so an author who meant a real side effect gets an
        # ungoverned, never-deferred loop. Reject it at intake so the authorial
        # intent is not silently dropped. ('none' is the explicit benign value
        # and is handled below.) Opt-in only: a non-opted contract falls through
        # to the base "unknown side_effect_class" error for these sentinels.
        if optin and isinstance(raw, str):
            sentinel = raw.strip().lower()
            if sentinel in {"", "null", "-"}:
                report.errors.append(
                    f"{where} declares side_effect_class {raw!r} which normalizes "
                    f"away to NULL at persistence -- it would be silently treated as "
                    f"the no-side-effect 'none'. Declare an explicit class from "
                    f"{sorted(SIDE_EFFECT_CLASSES)} (use 'none' if there is truly no "
                    f"side effect)."
                )
                continue
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
    # STEP-9 OPT-IN GATE: the side_effect_policy near-miss check (a typo'd
    # ``strict`` opt-in) is a NEW finding. It runs ONLY for a contract that opted
    # into step-9 elsewhere (a loop terminal-class or defer-bound declaration),
    # so a pure-legacy contract -- including one that happens to carry a
    # ``strict_mode``-shaped policy key with no other step-9 declaration -- hits
    # ZERO new code paths and is byte-identical to base 019271994. A contract
    # that DID opt into step-9 but typo'd its strict key is clearly trying to use
    # the feature, so surfacing the inert typo is correct there.
    if _grammar_contract_opts_into_step9(root):
        _check_side_effect_policy_keys(root, report)
    _check_timer_cadence(root, report)
    _check_irreversible_gating(root, report)
    _check_knob_references(root, report)
    _check_sensors(root, report)
    report.ok = not report.errors
    return report
