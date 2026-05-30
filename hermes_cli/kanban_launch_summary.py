"""Deterministic contract -> owner-facing summary renderer.

The launch pipeline can synthesize a *correct* board operating contract, but a
correct contract is a wall of JSON. The owner driving Hermes over Telegram needs
to understand, in plain language and BEFORE they ``/approve``:

    * what this board will actually *do* (the pipeline of stages),
    * what it will *watch* and how it knows when to stop watching,
    * which dials Hermes may turn on its own (and within what bounds),
    * what runs autonomously vs. what waits for owner approval,
    * when the whole thing stops.

This module turns any normalized contract (either the land-style
``workflow.stages`` + ``runtime.reactive_entities`` shape OR the synthesized
``entities`` + ``event_loops`` shape) into a compact, Telegram-friendly Markdown
summary. It is the single source of truth for "explain a contract to a human",
so the launch-review tool result, the gateway ``/approve`` message, and the
amendment proposal copy all read the same legible summary instead of each
hand-rolling (or dumping JSON).

Design constraints mirror the rest of the launch stack: deterministic, offline,
no model calls, never raises on a malformed contract (it degrades to "(unknown)"
fragments so a partial contract still produces a usable summary).
"""

from __future__ import annotations

from typing import Any, Optional

from hermes_cli.kanban_launch_grammar import (
    is_external_side_effect_class,
    normalize_side_effect_class,
    resolve_trigger_kind,
)

__all__ = [
    "render_owner_contract_summary",
    "render_contract_one_liner",
    "contract_summary_facts",
]


# ---------------------------------------------------------------------------
# Small structural helpers (tolerant of both contract shapes / partials)
# ---------------------------------------------------------------------------


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


def _humanize(token: Any) -> str:
    """Turn a snake/kebab identifier into a short human label."""
    text = str(token or "").strip()
    if not text:
        return ""
    return text.replace("_", " ").replace("-", " ").strip()


def _first_sentence(text: str, *, limit: int = 240) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "..."


def _stage_label(stage: dict[str, Any]) -> str:
    return str(stage.get("label") or _humanize(stage.get("key")) or "stage")


def _trigger_cadence_hours(trigger: dict[str, Any]) -> Optional[float]:
    for key, mult in (
        ("cadence_hours", 1.0),
        ("every_hours", 1.0),
        ("interval_hours", 1.0),
        ("cadence_minutes", 1 / 60),
        ("interval_minutes", 1 / 60),
        ("cadence_seconds", 1 / 3600),
        ("interval_seconds", 1 / 3600),
    ):
        val = trigger.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return float(val) * mult
    return None


def _format_hours(hours: float) -> str:
    if hours >= 24 and hours % 24 == 0:
        days = int(hours // 24)
        return f"every {days} day{'s' if days != 1 else ''}"
    if hours >= 1 and float(hours).is_integer():
        h = int(hours)
        return f"every {h} hour{'s' if h != 1 else ''}"
    if hours < 1:
        mins = int(round(hours * 60))
        return f"every {mins} min"
    return f"every {hours:g} hours"


def _stop_labels(*collections: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for coll in collections:
        for item in _as_list(coll):
            label = ""
            if isinstance(item, str):
                label = item.strip()
            elif isinstance(item, dict):
                label = str(
                    item.get("state")
                    or item.get("key")
                    or item.get("condition")
                    or item.get("transition")
                    or ""
                ).strip()
            if label and label.lower() not in seen:
                seen.add(label.lower())
                out.append(label)
    return out


# ---------------------------------------------------------------------------
# Section extractors
# ---------------------------------------------------------------------------


def _objective_line(root: dict[str, Any]) -> str:
    objective = root.get("objective")
    if isinstance(objective, dict):
        statement = objective.get("statement")
    else:
        statement = objective
    return _first_sentence(statement or "")


def _pipeline(root: dict[str, Any]) -> list[str]:
    """Return human stage labels, with substates noted inline."""
    workflow = _as_dict(root.get("workflow"))
    stages = [s for s in _as_list(workflow.get("stages")) if isinstance(s, dict)]
    out: list[str] = []
    for stage in stages:
        label = _stage_label(stage)
        substates = [
            _humanize(ss.get("key") if isinstance(ss, dict) else ss)
            for ss in _as_list(stage.get("substates"))
        ]
        substates = [s for s in substates if s]
        if substates:
            shown = ", ".join(substates[:4])
            label = f"{label} ({shown})"
        out.append(label)
    return out


def _watchers(root: dict[str, Any]) -> list[str]:
    """Describe what the board watches + how it nudges + when it stops."""
    out: list[str] = []
    runtime = _as_dict(root.get("runtime"))

    # Synthesized-style watchers: event_loops (+ entities for state vocab).
    entity_states: dict[str, list[str]] = {}
    for ent in _as_list(root.get("entities")):
        if isinstance(ent, dict):
            key = str(ent.get("key") or ent.get("type") or "").strip().lower()
            if key:
                entity_states[key] = _stop_labels(ent.get("terminal_states"))

    for loop in _as_list(root.get("event_loops")):
        if not isinstance(loop, dict):
            continue
        subject = _humanize(loop.get("entity") or loop.get("type") or "items")
        triggers = loop.get("triggers")
        kinds = {resolve_trigger_kind(t) for t in _as_list(triggers)}
        cadence_hrs: Optional[float] = None
        for t in _as_list(triggers):
            if isinstance(t, dict) and resolve_trigger_kind(t) == "timer":
                cadence_hrs = _trigger_cadence_hours(t)
                if cadence_hrs:
                    break
        stops = _stop_labels(loop.get("terminal_states"), loop.get("stop_conditions"))
        if not stops:
            stops = entity_states.get(
                str(loop.get("entity") or "").strip().lower(), []
            )
        desc = f"Watches each {subject}"
        if "inbound" in kinds:
            desc += ", responding to replies"
        if cadence_hrs:
            desc += f" and following up {_format_hours(cadence_hrs)}"
        if stops:
            desc += f"; stops when: {', '.join(_humanize(s) for s in stops[:3])}"
        out.append(desc)

    # Land-style reactive entities (conversation threads etc.).
    reactive = _as_dict(runtime.get("reactive_entities"))
    for name, cfg in reactive.items():
        cfg = _as_dict(cfg)
        subject = _humanize(cfg.get("entity_type") or name)
        desc = f"Watches each {subject}"
        if cfg.get("terminal_completion_requires_entity_resolution"):
            desc += "; stays open until the thread reaches a terminal outcome"
        stops = _stop_labels(cfg.get("terminal_states"), cfg.get("stop_conditions"))
        if stops:
            desc += f" ({', '.join(_humanize(s) for s in stops[:3])})"
        out.append(desc)

    return out


def _knobs(root: dict[str, Any], *, limit: int = 6) -> list[str]:
    tunables = root.get("tunables")
    if not isinstance(tunables, dict):
        tunables = _as_dict(root.get("runtime")).get("tunables")
    if not isinstance(tunables, dict):
        return []
    out: list[str] = []
    for knob, spec in tunables.items():
        spec = _as_dict(spec)
        default = spec.get("default")
        bound = ""
        rng = spec.get("range")
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            bound = f"{rng[0]}–{rng[1]}"
        elif "min" in spec and "max" in spec:
            bound = f"{spec.get('min')}–{spec.get('max')}"
        elif isinstance(spec.get("allowed"), (list, tuple)):
            bound = "/".join(str(a) for a in spec["allowed"][:4])
        elif isinstance(spec.get("options"), (list, tuple)):
            bound = "/".join(str(a) for a in spec["options"][:4])
        label = _humanize(knob)
        piece = label
        if default is not None and bound:
            piece = f"{label} (now {default}, range {bound})"
        elif default is not None:
            piece = f"{label} (now {default})"
        elif bound:
            piece = f"{label} (range {bound})"
        out.append(piece)
    return out[:limit]


def _sensors(root: dict[str, Any]) -> list[str]:
    out: list[str] = []
    _SENSOR_HUMAN = {
        "heartbeat": "liveness/stall detection",
        "circuit_breaker": "auto-pause on error spikes",
        "budget": "spend/rate cap metering",
    }
    for sensor in _as_list(root.get("sensors")):
        if not isinstance(sensor, dict):
            continue
        k = str(sensor.get("kind") or "").strip().lower()
        human = _SENSOR_HUMAN.get(k, _humanize(k))
        if human:
            out.append(human)
    # de-dup preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for s in out:
        if s.lower() not in seen:
            seen.add(s.lower())
            deduped.append(s)
    return deduped


def _gated_actions(root: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (needs_approval, autonomous_external) human action labels.

    An action whose typed ``side_effect_class`` crosses the board boundary is
    classified by whether it is gated (named in an approval_gate's
    ``required_before`` OR governed by side_effect_policy) -> needs approval,
    else surfaced as autonomous-external (which the invariants would actually
    reject, so this list should normally be empty -- it exists so a partial /
    pre-validation contract is still explained honestly).
    """
    workflow = _as_dict(root.get("workflow"))

    gated_action_names: set[str] = set()
    for gate in _as_list(root.get("approval_gates")):
        if not isinstance(gate, dict):
            continue
        req = gate.get("required_before")
        items = _as_list(req) if not isinstance(req, str) else [req]
        for item in items:
            if isinstance(item, str) and item.strip():
                gated_action_names.add(item.strip().lower())

    policy = _as_dict(root.get("side_effect_policy"))
    gated_classes: set[str] = set()
    for key in ("approval_required", "forbidden"):
        for item in _as_list(policy.get(key)):
            canon = normalize_side_effect_class(item)
            if canon:
                gated_classes.add(canon)

    needs_approval: list[str] = []
    autonomous_external: list[str] = []
    seen_na: set[str] = set()
    seen_ax: set[str] = set()
    for stage in _as_list(workflow.get("stages")):
        if not isinstance(stage, dict):
            continue
        for action in _as_list(stage.get("actions")):
            if not isinstance(action, dict):
                continue
            canon = normalize_side_effect_class(action.get("side_effect_class"))
            if not canon or not is_external_side_effect_class(canon):
                continue
            label = str(action.get("label") or _humanize(action.get("key")) or "action").strip()
            names = {
                str(action.get(k)).strip().lower()
                for k in ("key", "label")
                if isinstance(action.get(k), str) and str(action.get(k)).strip()
            }
            gated = bool(names & gated_action_names) or canon in gated_classes
            if gated:
                if label.lower() not in seen_na:
                    seen_na.add(label.lower())
                    needs_approval.append(label)
            else:
                if label.lower() not in seen_ax:
                    seen_ax.add(label.lower())
                    autonomous_external.append(label)
    return needs_approval, autonomous_external


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def contract_summary_facts(contract: Any) -> dict[str, Any]:
    """Return the structured facts the renderers use (handy for tests)."""
    root = _contract_root(contract)
    needs_approval, autonomous_external = _gated_actions(root)
    return {
        "objective": _objective_line(root),
        "pipeline": _pipeline(root),
        "watchers": _watchers(root),
        "knobs": _knobs(root),
        "sensors": _sensors(root),
        "needs_approval": needs_approval,
        "autonomous_external": autonomous_external,
    }


def render_contract_one_liner(contract: Any) -> str:
    """A single dense line for board listings / pending-approval lists."""
    facts = contract_summary_facts(contract)
    pipeline = facts["pipeline"]
    arrow = " → ".join(p.split(" (")[0] for p in pipeline[:6]) if pipeline else "(no stages)"
    bits = [arrow]
    if facts["needs_approval"]:
        bits.append(f"{len(facts['needs_approval'])} owner-gated action(s)")
    if facts["watchers"]:
        bits.append(f"{len(facts['watchers'])} watcher(s)")
    return " · ".join(bits)


def render_owner_contract_summary(
    contract: Any,
    *,
    board: Optional[str] = None,
    include_approve_hint: bool = True,
    active: bool = False,
) -> str:
    """Render a Telegram-friendly Markdown summary of a board contract.

    ``active=False`` (default) frames it as a *draft awaiting approval* and (when
    ``include_approve_hint``) appends the explicit ``/approve <board>`` call to
    action. ``active=True`` frames it as a now-live board.
    """
    facts = contract_summary_facts(contract)
    lines: list[str] = []

    name = f"`{board}`" if board else "this board"
    if active:
        lines.append(f"✅ *Launched {name}* — here's what's now running:")
    else:
        lines.append(f"📋 *Draft contract for {name}* — review before approving:")

    if facts["objective"]:
        lines.append(f"\n*Goal:* {facts['objective']}")

    if facts["pipeline"]:
        arrow = " → ".join(facts["pipeline"])
        lines.append(f"\n*Pipeline:* {arrow}")

    if facts["watchers"]:
        lines.append("\n*Watches & follow-ups:*")
        for w in facts["watchers"][:4]:
            lines.append(f"• {w}")

    if facts["needs_approval"]:
        gated = ", ".join(facts["needs_approval"][:8])
        lines.append(f"\n*Needs your approval:* {gated}")
    else:
        lines.append("\n*Needs your approval:* every outside-world action is owner-gated.")

    if facts["autonomous_external"]:
        # Should be empty for a validated contract; surface loudly if not.
        ax = ", ".join(facts["autonomous_external"][:8])
        lines.append(f"\n⚠️ *Would run WITHOUT approval (review!):* {ax}")

    if facts["knobs"]:
        lines.append("\n*Dials Hermes can auto-tune (within bounds):*")
        lines.append("• " + "; ".join(facts["knobs"][:6]))

    if facts["sensors"]:
        lines.append(f"\n*Safety sensors:* {', '.join(facts['sensors'])}.")

    if not active and include_approve_hint:
        slug = board or "<board>"
        lines.append(
            f"\nNothing runs yet — the board is in *contract review* and dispatch is "
            f"disabled. Reply `/approve {slug}` to launch it, or tell me what to change."
        )

    return "\n".join(lines)
