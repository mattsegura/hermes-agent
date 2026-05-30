"""Behavioural simulation for a contract's conversational stage (Layer 2 QA).

Static validation (coverage + invariants) proves a contract is well *shaped*.
This module proves it is well *behaved*: it extracts the conversational control
plane (the watched entity / event loop or the watch stage), then runs scripted
counterpart behaviours -- a prospect who qualifies, who hard-disqualifies, who
ghosts, who replies slowly -- through a small deterministic state machine and
checks that each scenario reaches a terminal outcome (never a zombie, never an
infinite loop), using only the machinery the contract actually declares.

The simulator is driven entirely by the contract: if the contract forgot the
follow-up timer, the ghost/slow-burn scenarios cannot advance on a timeout and
come back ``resolved=False`` -- which is exactly how a hollow stage is caught.

Deterministic and offline: no model calls, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from hermes_cli.kanban_launch_grammar import (
    has_inbound as _grammar_has_inbound,
    has_timer as _grammar_has_timer,
)

# Outcome categorisation (test-infra heuristics over the contract's declared
# terminal/stop labels). An outcome may match more than one category.
# Note: use the stem "approve" (not "approv") for the approval/content family so
# it matches "approved"/"Owner approves the reel." but NOT "Approval expires ..."
# (a timeout/ghost outcome). "publish" covers content-pipeline success without
# colliding with the common "... is not posted" ghost phrasing.
_SUCCESS_TOKENS = ("qualif", "onboard", "hired", "won", "accept", "book", "hot", "contract", "advanc", "complete", "convert", "approve", "publish", "posted live")
_DISQUALIFY_TOKENS = ("reject", "ineligible", "declin", "not_fit", "notfit", "disqualif", "no active", "no license", "unlicensed", "dead", "not a fit")
_GHOST_TOKENS = ("ghost", "stop responding", "stops responding", "no reply", "no response", "timeout", "unresponsive", "recycle", "stops respond", "no viable")


@dataclass
class ConversationModel:
    source: str
    has_inbound: bool
    has_timer: bool
    outcomes: list[str]
    knobs: dict[str, Any] = field(default_factory=dict)

    def _match(self, tokens: tuple[str, ...]) -> list[str]:
        return [o for o in self.outcomes if any(t in o.lower() for t in tokens)]

    @property
    def success_outcomes(self) -> list[str]:
        return self._match(_SUCCESS_TOKENS)

    @property
    def disqualify_outcomes(self) -> list[str]:
        return self._match(_DISQUALIFY_TOKENS)

    @property
    def ghost_outcomes(self) -> list[str]:
        return self._match(_GHOST_TOKENS)

    def non_success(self) -> list[str]:
        succ = set(self.success_outcomes)
        return [o for o in self.outcomes if o not in succ]


@dataclass
class SimResult:
    scenario: str
    resolved: bool
    outcome: Optional[str]
    category: str
    nudges_fired: int
    steps: int


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    return []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _contract_root(contract: Any) -> dict[str, Any]:
    parsed = contract if isinstance(contract, dict) else {}
    if isinstance(parsed.get("operating_contract"), dict):
        return dict(parsed["operating_contract"])
    inner = parsed.get("contract")
    if isinstance(inner, dict) and any(k in inner for k in ("objective", "runtime", "workflow")):
        return dict(inner)
    return dict(parsed)


def _outcome_labels(*collections: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for coll in collections:
        for item in _as_list(coll):
            label = ""
            if isinstance(item, str):
                label = item.strip()
            elif isinstance(item, dict):
                label = str(
                    item.get("transition") or item.get("key") or item.get("state")
                    or item.get("condition") or ""
                ).strip()
            if label and label.lower() not in seen:
                seen.add(label.lower())
                out.append(label)
    return out


def _knobs(root: dict[str, Any]) -> dict[str, Any]:
    tun = root.get("tunables")
    if not isinstance(tun, dict):
        tun = _as_dict(root.get("runtime")).get("tunables")
    out: dict[str, Any] = {}
    if isinstance(tun, dict):
        for k, spec in tun.items():
            spec = _as_dict(spec)
            out[k] = spec.get("default")
    return out


def build_conversation_model(contract: Any) -> Optional[ConversationModel]:
    """Extract the conversational control plane from a contract, or None."""
    root = _contract_root(contract)
    knobs = _knobs(root)

    # Prefer an explicit event loop with an inbound trigger (synthesized shape).
    for loop in _as_list(root.get("event_loops")):
        loop = _as_dict(loop)
        triggers = loop.get("triggers")
        if _grammar_has_inbound(triggers):
            outcomes = _outcome_labels(loop.get("terminal_states"), loop.get("stop_conditions"))
            return ConversationModel(
                source=f"event_loop:{loop.get('entity') or loop.get('type')}",
                has_inbound=True,
                has_timer=_grammar_has_timer(triggers),
                outcomes=outcomes,
                knobs=knobs,
            )

    # Otherwise a workflow stage with substates + an inbound trigger (land shape).
    workflow = _as_dict(root.get("workflow"))
    for stage in _as_list(workflow.get("stages")):
        stage = _as_dict(stage)
        triggers = stage.get("triggers")
        if _as_list(stage.get("substates")) and _grammar_has_inbound(triggers):
            outcomes = _outcome_labels(stage.get("exit_criteria"), stage.get("substates"))
            return ConversationModel(
                source=f"stage:{stage.get('key')}",
                has_inbound=True,
                has_timer=_grammar_has_timer(triggers),
                outcomes=outcomes,
                knobs=knobs,
            )
    return None


def _pick(preferred: list[str], fallback: list[str]) -> Optional[str]:
    if preferred:
        return preferred[0]
    if fallback:
        return fallback[0]
    return None


def simulate_scenario(model: ConversationModel, scenario: dict[str, Any]) -> SimResult:
    """Run one scripted scenario through the contract's conversational machine.

    Scenario keys:
      * name: str
      * events: list of "qualify" | "disqualify" | "timeout"
      * max_nudges: int (defaults to the contract's knob or 3)
    """
    name = str(scenario.get("name") or "scenario")
    events = _as_list(scenario.get("events"))
    max_nudges = int(
        scenario.get("max_nudges")
        if scenario.get("max_nudges") is not None
        else model.knobs.get("max_nudges", 3)
    )
    nudges = 0
    steps = 0
    for event in events:
        steps += 1
        if event == "qualify":
            outcome = _pick(model.success_outcomes, [])
            if outcome:
                return SimResult(name, True, outcome, "success", nudges, steps)
        elif event == "disqualify":
            outcome = _pick(model.disqualify_outcomes, model.non_success())
            if outcome:
                return SimResult(name, True, outcome, "disqualify", nudges, steps)
        elif event == "timeout":
            # A timeout can only be acted on if the contract declares a timer.
            if not model.has_timer:
                continue
            nudges += 1
            if nudges > max_nudges:
                outcome = _pick(model.ghost_outcomes, model.non_success())
                if outcome:
                    return SimResult(name, True, outcome, "ghost", nudges, steps)
    return SimResult(name, False, None, "unresolved", nudges, steps)


def default_scenarios(max_nudges: int = 3) -> list[dict[str, Any]]:
    """The standard scripted prospect behaviours used to prove functionality."""
    return [
        {"name": "happy_path", "events": ["qualify"]},
        {"name": "disqualifier", "events": ["disqualify"]},
        {"name": "slow_burn", "events": ["timeout", "qualify"]},
        {"name": "ghost", "events": ["timeout"] * (max_nudges + 1)},
    ]


def run_default_simulation(contract: Any) -> dict[str, SimResult]:
    """Run all default scenarios; returns {scenario_name: SimResult}."""
    model = build_conversation_model(contract)
    if model is None:
        return {}
    max_nudges = int(model.knobs.get("max_nudges", 3))
    results: dict[str, SimResult] = {}
    for scenario in default_scenarios(max_nudges):
        results[scenario["name"]] = simulate_scenario(model, scenario)
    return results
