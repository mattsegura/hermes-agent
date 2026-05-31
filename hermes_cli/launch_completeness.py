"""Unified launch-completeness check (the one master checklist).

WHY THIS EXISTS
---------------
Today four different mechanisms decide "is this contract complete enough to
launch?" and they disagree:
  1. the intake synthesis/repair prompts (enforce nothing in code),
  2. kanban_launch_coverage.py (prose-specificity on the owner's answers),
  3. check_contract_invariants R1-R6/F10/S1-S3 (the only STRUCTURAL enforcer --
     but it runs only in the synthesis repair loop + the amendment path),
  4. validate_business_runtime_contract (a PURE PRESENCE checker -- and the ONLY
     one board_dispatch_gate consults).

So "launch-ready per the gate" != "structurally sound", and a board can dispatch
having passed only the presence checker. This module is the single source of
truth: it runs the STRONG structural checks (check_contract_invariants) PLUS the
net-new completeness rules the audit found unguarded, and returns ONE merged
report. Wire it into validate_business_runtime_contract (and re-run the degraded
universal-drafter fallback through it) so the gate finally sees the real picture.

REPORT-MODE BY DEFAULT (non-breaking): net-new findings are emitted as WARNINGS,
never errors, so existing/launched boards keep dispatching. Flip enforce=True
per-dimension only after a report-mode soak.

Standalone + read-only: imports check_contract_invariants from the engine, no DB
or board.json access, stdlib only.
"""
from __future__ import annotations

import re
from typing import Any, Optional

try:  # the strong structural checker (keystone): reuse it, do not reimplement it
    from hermes_cli.kanban_launch_invariants import check_contract_invariants
except Exception:  # pragma: no cover - allows the module to load even if engine import fails
    check_contract_invariants = None  # type: ignore

try:  # the knobs the optimizer actually tunes count as "consumed"
    from hermes_cli.kanban_optimizer import OPTIMIZER_MANAGED_KNOBS as _MANAGED_KNOBS
except Exception:  # pragma: no cover
    _MANAGED_KNOBS = ("follow_up_interval_hours", "cadence_hours")

try:  # reuse the typed trigger grammar so "conversational" is detected the same
    from hermes_cli.kanban_launch_grammar import has_inbound as _grammar_has_inbound
except Exception:  # pragma: no cover - keep the module loadable if grammar import fails
    _grammar_has_inbound = None  # type: ignore


# Canonical dimension keys (worst-first). The dispatch gate enforces a configured
# SUBSET of these (see kanban_db._launch_completeness_enforced_dimensions); the
# rest stay advisory/report-mode. "invariants" is the always-on structural
# checker (check_contract_invariants); the others are the net-new audit rules.
# Single source of truth for the gate's per-dimension enforce flip + "all".
DIMENSIONS: tuple[str, ...] = (
    "invariants",
    "side_effect_class_coverage",
    "event_loop_termination",
    "success_scoreability",
    "evidence_namespace",
    "win_signal_rail",
    "tunable_consumer_binding",
    "stage_reachability",
    "distinct_terminals",
)


# --- net-new dimension D: success must be scoreable (mirrors the A1 helper) ---
_SCOREABLE_SUCCESS_RE = re.compile(
    r"(\d|%|\$|>=|<=|>|<|\bat least\b|\bat most\b|\bper\b|\bwithin\b|\brate\b|"
    r"\bratio\b|\bcount\b|\bnumber of\b|\bMRR\b|\bARR\b|\bMAU\b|\bDAU\b|\bLTV\b|\bCAC\b)",
    re.IGNORECASE,
)


def _is_scoreable(entry: Any) -> bool:
    text = str(entry or "").strip()
    return bool(text) and bool(_SCOREABLE_SUCCESS_RE.search(text))


def _as_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if v is None:
        return []
    return [v]


def _section(contract: dict, key: str) -> Any:
    """A section may live top-level or nested under runtime, depending on the
    contract variant. Prefer top-level, fall back to runtime."""
    if not isinstance(contract, dict):
        return None
    if contract.get(key) is not None:
        return contract.get(key)
    runtime = contract.get("runtime")
    if isinstance(runtime, dict):
        return runtime.get(key)
    return None


def _tunable_declares_consumer(spec: Any) -> bool:
    """A tunable may explicitly declare what reads it (the audit's preferred design)."""
    if not isinstance(spec, dict):
        return False
    for k in ("consumed_by", "consumer", "consumers", "sensor", "binding", "read_by"):
        val = spec.get(k)
        if (isinstance(val, str) and val.strip()) or (isinstance(val, (list, tuple)) and val):
            return True
    return False


def _referenced_knob_names(contract: dict) -> set[str]:
    """Every tunable name some consumer reads: the optimizer-managed knobs, any
    sensor knob-binding value, and any `*_knob` field across the contract."""
    refs: set[str] = set(str(k) for k in _MANAGED_KNOBS)
    sensors = _as_list(_section(contract, "sensors"))
    for s in sensors:
        if isinstance(s, dict):
            knobs = s.get("knobs")
            if isinstance(knobs, dict):
                refs.update(str(v) for v in knobs.values() if v)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and k.endswith("_knob") and isinstance(v, str):
                    refs.add(v)
                walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)

    walk(_section(contract, "event_loops"))
    walk(contract.get("workflow"))
    return refs


def _finite_number(v: Any) -> bool:
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


# --- net-new dimension C: a conversion loop must declare a win-class terminal ---
# A "won"-class outcome is the signal the reward/scoreboard binds to (audit #4:
# the optimizer tunes toward a 'won' substring). A conversational loop whose only
# terminals are failure/neutral (dead, rejected, paused, measured) can never emit
# a conversion signal -> no reward, ever. Vocabulary is domain-agnostic and covers
# the marquee domains (under_contract/onboarded/published-as-deliverable, etc.).
_WIN_TERMINAL_TOKENS: tuple[str, ...] = (
    "won", "win", "closed", "close", "converted", "convert", "conversion",
    "signed", "sign", "under_contract", "contracted", "onboarded", "hired",
    "paid", "subscriber", "accepted", "sold", "delivered", "published",
    "completed", "complete", "fulfilled", "succeeded", "success", "approved",
)


def _terminal_state_tokens(loop: Any) -> list[str]:
    """Lowercased terminal-state labels for a loop/entity (str or dict shapes)."""
    out: list[str] = []
    for src in ("terminal_states", "terminal_state", "terminal_outcomes", "terminal_outcome"):
        for item in _as_list(loop.get(src) if isinstance(loop, dict) else None):
            if isinstance(item, str) and item.strip():
                out.append(item.strip().lower())
            elif isinstance(item, dict):
                key = str(item.get("key") or item.get("state") or item.get("outcome") or "").strip().lower()
                cls = str(item.get("class") or item.get("kind") or "").strip().lower()
                if key:
                    out.append(key)
                if cls:
                    out.append(cls)
    return out


def _looks_like_win(token: str) -> bool:
    t = (token or "").lower()
    return any(tok in t for tok in _WIN_TERMINAL_TOKENS)


def _loop_is_conversational(loop: Any) -> bool:
    """A loop is conversational when it consumes an inbound trigger (it talks to
    an outside party and is meant to drive that party toward a conversion)."""
    if not isinstance(loop, dict):
        return False
    triggers = loop.get("triggers")
    if _grammar_has_inbound is not None:
        try:
            return bool(_grammar_has_inbound(triggers))
        except Exception:  # pragma: no cover - defensive
            pass
    # Fallback keyword check if the grammar import failed.
    for t in _as_list(triggers):
        text = ""
        if isinstance(t, str):
            text = t.lower()
        elif isinstance(t, dict):
            text = str(t.get("kind") or t.get("type") or t.get("reason") or "").lower()
        if "inbound" in text or "reply" in text or "incoming" in text:
            return True
    return False


def _stage_next_refs(stage: dict) -> set[str]:
    """Next-stage keys a stage points at, read from exit_criteria + transitions."""
    refs: set[str] = set()
    for ec in _as_list(stage.get("exit_criteria")):
        if isinstance(ec, dict):
            for k in ("transition", "next_stage", "next", "to", "target"):
                v = ec.get(k)
                if isinstance(v, str) and v.strip():
                    refs.add(v.strip())
    for tr in _as_list(stage.get("transitions")):
        if isinstance(tr, str) and tr.strip():
            refs.add(tr.strip())
        elif isinstance(tr, dict):
            for k in ("to", "next", "next_stage", "target", "transition"):
                v = tr.get(k)
                if isinstance(v, str) and v.strip():
                    refs.add(v.strip())
    return refs


def _stage_is_terminal(stage: dict) -> bool:
    """A stage is terminal if flagged, or it has no outbound next-stage refs."""
    if isinstance(stage, dict) and bool(stage.get("terminal")):
        return True
    return not _stage_next_refs(stage)


def _proof_artifact_keys(contract: dict) -> set[str]:
    """Every evidence key SOME declared artifact can produce: proof_requirements
    entries (string keys or {artifact|key|name} dicts) + worker_envelope
    required_proof + stage action emits/produces/artifact declarations."""
    produced: set[str] = set()

    def _add(v: Any) -> None:
        if isinstance(v, str) and v.strip():
            produced.add(v.strip())

    for pr in _as_list(_section(contract, "proof_requirements")):
        if isinstance(pr, str):
            _add(pr)
        elif isinstance(pr, dict):
            for k in ("artifact", "key", "name", "id", "evidence", "produces"):
                _add(pr.get(k))

    runtime = contract.get("runtime") if isinstance(contract.get("runtime"), dict) else {}
    envelopes = runtime.get("worker_envelopes")
    if not isinstance(envelopes, dict):
        envelopes = _section(contract, "worker_envelopes")
    if isinstance(envelopes, dict):
        for env in envelopes.values():
            if isinstance(env, dict):
                for rp in _as_list(env.get("required_proof")):
                    _add(rp)
                    if isinstance(rp, dict):
                        for k in ("artifact", "key", "name"):
                            _add(rp.get(k))

    # Stage actions may also declare what artifact they emit/produce.
    workflow = contract.get("workflow") if isinstance(contract.get("workflow"), dict) else {}
    for st in _as_list(workflow.get("stages")):
        if not isinstance(st, dict):
            continue
        for a in _as_list(st.get("actions")):
            if isinstance(a, dict):
                for k in ("produces", "emits", "artifact", "produces_evidence", "evidence"):
                    val = a.get(k)
                    if isinstance(val, (list, tuple)):
                        for v in val:
                            _add(v)
                    else:
                        _add(val)
    return produced


def assess_launch_completeness(contract: Optional[dict], *, enforce: bool = False) -> dict:
    """Run the strong structural checks + the net-new completeness rules.

    Returns {ok, errors, warnings, dimensions} where `dimensions` maps each
    check name to its findings. In report-mode (enforce=False) net-new findings
    are warnings; in enforce-mode they are errors. `ok` is False only when there
    are hard errors (structural-invariant errors always count; net-new findings
    count only under enforce).
    """
    errors: list[str] = []
    warnings: list[str] = []
    dims: dict[str, dict] = {}

    if not isinstance(contract, dict):
        return {"ok": False, "errors": ["contract is not an object"], "warnings": [], "dimensions": {}}

    def emit(dim: str, findings: list[str]) -> None:
        dims[dim] = {"findings": findings, "enforced": enforce}
        if not findings:
            return
        (errors if enforce else warnings).extend(f"[{dim}] {f}" for f in findings)

    # --- KEYSTONE: run the strong structural checker and merge it in ---
    inv_findings: list[str] = []
    if check_contract_invariants is not None:
        try:
            report = check_contract_invariants(contract)
            # invariant ERRORS are always hard (they are the real structural rails)
            for e in getattr(report, "errors", []) or []:
                errors.append(f"[invariants] {e}")
                inv_findings.append(f"error: {e}")
            for w in getattr(report, "warnings", []) or []:
                warnings.append(f"[invariants] {w}")
                inv_findings.append(f"warning: {w}")
        except Exception as exc:  # surface, do not swallow (audit: silent swallow points)
            errors.append(f"[invariants] check raised: {exc!r}")
            inv_findings.append(f"raised: {exc!r}")
    else:
        inv_findings.append("check_contract_invariants unavailable (engine import failed)")
    dims["invariants"] = {"findings": inv_findings, "enforced": True}

    objective = contract.get("objective") if isinstance(contract.get("objective"), dict) else {}
    workflow = contract.get("workflow") if isinstance(contract.get("workflow"), dict) else {}

    # --- D: success scoreability ---
    succ = [s for s in _as_list(objective.get("success")) if str(s or "").strip()]
    unscoreable = [s for s in succ if not _is_scoreable(s)]
    f_succ: list[str] = []
    if not succ:
        f_succ.append("objective.success is empty")
    elif unscoreable:
        f_succ.append(
            f"{len(unscoreable)}/{len(succ)} success criteria carry no measurable target the "
            f"scoreboard can grade: " + "; ".join(str(u)[:80] for u in unscoreable[:3])
            + (" ..." if len(unscoreable) > 3 else "")
        )
    emit("success_scoreability", f_succ)

    # --- A: every action must carry a side_effect_class (default-deny) ---
    stages = [s for s in _as_list(workflow.get("stages")) if isinstance(s, dict)]
    missing_class: list[str] = []
    for st in stages:
        for a in _as_list(st.get("actions")):
            if isinstance(a, dict) and not str(a.get("side_effect_class") or "").strip():
                missing_class.append(f"{st.get('key') or '?'}.{a.get('key') or '?'}")
    f_class: list[str] = []
    if missing_class:
        f_class.append(
            f"{len(missing_class)} action(s) have NO side_effect_class (default-deny: must be "
            f"declared, never treated as 'none'): " + ", ".join(missing_class[:6])
            + (" ..." if len(missing_class) > 6 else "")
        )
    emit("side_effect_class_coverage", f_class)

    # --- E: every event_loop must have a machine-checkable stop ---
    loops = _as_list(_section(contract, "event_loops"))
    zombie: list[str] = []
    for i, lp in enumerate(loops):
        if not isinstance(lp, dict):
            continue
        has_terminal = bool([t for t in _as_list(lp.get("terminal_states")) if str(t or "").strip()])
        has_finite_nudges = _finite_number(lp.get("max_nudges"))
        has_some_stop = bool(_as_list(lp.get("stop_conditions")) or _as_list(lp.get("exit_rules")) or _as_list(lp.get("triggers")))
        if has_some_stop and not (has_terminal or has_finite_nudges):
            zombie.append(str(lp.get("entity") or lp.get("type") or f"loop[{i}]"))
    f_loop: list[str] = []
    if zombie:
        f_loop.append(
            f"{len(zombie)} event_loop(s) have only free-text stops, no machine-checkable "
            f"termination (terminal_states or finite max_nudges) -> zombie timer: " + ", ".join(zombie[:6])
        )
    emit("event_loop_termination", f_loop)

    # --- B: declared tunables must be read by some consumer ---
    tunables = _section(contract, "tunables")
    tun_items = list(tunables.items()) if isinstance(tunables, dict) else []
    refs = _referenced_knob_names(contract)
    unread = [k for k, spec in tun_items if k not in refs and not _tunable_declares_consumer(spec)]
    f_tun: list[str] = []
    if unread:
        f_tun.append(
            f"{len(unread)}/{len(tun_items)} tunable(s) have no detectable consumer "
            f"(not optimizer-managed, not bound by a sensor, no declared consumer) -> likely "
            f"dead config; wire it or declare its consumer: " + ", ".join(unread[:6])
            + (" ..." if len(unread) > 6 else "")
        )
    emit("tunable_consumer_binding", f_tun)

    # --- C: win-signal rail (a conversion loop must declare a 'won'-class terminal) ---
    # A conversational loop (inbound trigger) is meant to drive an outside party
    # toward a conversion that produces reward. If it has terminal_states but NONE
    # looks like a win/closed/converted outcome, the optimizer has nothing to tune
    # toward -- reward stays permanently zero (audit #4). Report-mode warning.
    no_win: list[str] = []
    for i, lp in enumerate(loops):
        if not isinstance(lp, dict):
            continue
        if not _loop_is_conversational(lp):
            continue
        terminals = _terminal_state_tokens(lp)
        if not terminals:
            continue  # no terminals at all is the F10 zombie rail's concern, not C
        if not any(_looks_like_win(t) for t in terminals):
            name = str(lp.get("entity") or lp.get("type") or f"loop[{i}]")
            no_win.append(
                f"{name} (terminals: {', '.join(sorted(set(terminals))[:6])})"
            )
    f_win: list[str] = []
    if no_win:
        f_win.append(
            f"{len(no_win)} conversational loop(s) declare terminal_states but none looks like a "
            f"win/closed/converted outcome -> no conversion signal for reward to bind to: "
            + "; ".join(no_win[:6])
        )
    emit("win_signal_rail", f_win)

    # --- E1: evidence_namespace (each evidence_required key must be produced) ---
    # Every workflow.stages[].exit_criteria.evidence_required key should be
    # produced by SOME declared artifact (proof_requirements / worker_envelope
    # required_proof / action emits). An evidence key nothing produces is a
    # permanent strand: the stage can never satisfy its exit (audit #5).
    produced = _proof_artifact_keys(contract)
    orphan_evidence: list[str] = []
    for st in stages:
        skey = str(st.get("key") or st.get("label") or "stage")
        for ec in _as_list(st.get("exit_criteria")):
            if not isinstance(ec, dict):
                continue
            for ev in _as_list(ec.get("evidence_required")):
                evk = ev.strip() if isinstance(ev, str) else str(ev.get("key") or ev.get("artifact") or "").strip() if isinstance(ev, dict) else ""
                if evk and evk not in produced:
                    orphan_evidence.append(f"{skey}:{evk}")
    f_ev: list[str] = []
    if orphan_evidence:
        # dedupe while preserving order
        seen_ev: set[str] = set()
        uniq_ev = [e for e in orphan_evidence if not (e in seen_ev or seen_ev.add(e))]
        f_ev.append(
            f"{len(uniq_ev)} evidence_required key(s) are produced by no declared artifact "
            f"(not in proof_requirements / worker_envelope required_proof / action emits) -> "
            f"strandable exit: " + ", ".join(uniq_ev[:8])
            + (" ..." if len(uniq_ev) > 8 else "")
        )
    emit("evidence_namespace", f_ev)

    # --- E2: stage_reachability (every non-terminal stage must reach a terminal) ---
    # Build the stage next-graph and BFS from each non-terminal stage; warn on
    # island stages (next-ref to a missing stage) or stages from which no terminal
    # stage is reachable (audit #10: abnormal exits strand).
    stage_by_key: dict[str, dict] = {}
    for st in stages:
        k = str(st.get("key") or st.get("label") or "").strip()
        if k and k not in stage_by_key:
            stage_by_key[k] = st
    f_reach: list[str] = []
    if stage_by_key:
        terminal_keys = {k for k, st in stage_by_key.items() if _stage_is_terminal(st)}

        def _reaches_terminal(start: str) -> tuple[bool, set[str]]:
            seen: set[str] = set()
            stack = [start]
            dangling: set[str] = set()
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                if cur in terminal_keys:
                    return True, dangling
                for nxt in _stage_next_refs(stage_by_key.get(cur, {})):
                    if nxt in stage_by_key:
                        stack.append(nxt)
                    else:
                        dangling.add(nxt)
            return (bool(seen & terminal_keys), dangling)

        unreachable: list[str] = []
        islands: list[str] = []
        for k, st in stage_by_key.items():
            if k in terminal_keys:
                continue
            reaches, dangling = _reaches_terminal(k)
            if dangling:
                islands.append(f"{k}->{','.join(sorted(dangling)[:3])}")
            if not reaches:
                unreachable.append(k)
        if not terminal_keys:
            f_reach.append(
                f"workflow has {len(stage_by_key)} stage(s) but NO terminal stage "
                f"(no stage flagged terminal and every stage has an outbound transition) -> "
                f"the workflow can never end."
            )
        if unreachable:
            f_reach.append(
                f"{len(unreachable)} non-terminal stage(s) cannot reach any terminal stage "
                f"via exit_criteria/transitions -> abnormal exits strand: "
                + ", ".join(sorted(set(unreachable))[:6])
            )
        if islands:
            f_reach.append(
                f"{len(islands)} stage(s) transition to an undeclared next-stage (island ref): "
                + ", ".join(islands[:6])
            )
    emit("stage_reachability", f_reach)

    # --- E3: distinct_terminals (a conversation must separate done/paused/disqualified) ---
    # A conversational loop should have DISTINCT done/paused/disqualified terminal
    # states; if it collapses them into a single terminal it cannot tell success
    # from kill from recycle (audit #10). Report-mode warning (the invariants
    # already HARD-error on conversational loops with <2 outcomes; this is the
    # softer "exactly one terminal" smell on loops the invariants may not classify).
    collapsed: list[str] = []
    for i, lp in enumerate(loops):
        if not isinstance(lp, dict):
            continue
        if not _loop_is_conversational(lp):
            continue
        distinct = sorted(set(_terminal_state_tokens(lp)))
        if len(distinct) == 1:
            name = str(lp.get("entity") or lp.get("type") or f"loop[{i}]")
            collapsed.append(f"{name} (only terminal: {distinct[0]})")
    f_distinct: list[str] = []
    if collapsed:
        f_distinct.append(
            f"{len(collapsed)} conversational loop(s) collapse done/paused/disqualified into a "
            f"single terminal_state -> cannot distinguish success from kill/recycle: "
            + "; ".join(collapsed[:6])
        )
    emit("distinct_terminals", f_distinct)

    return {"ok": not errors, "errors": errors, "warnings": warnings, "dimensions": dims}


# --------------------------- self-test ---------------------------
if __name__ == "__main__":
    import json
    import pathlib
    import sys

    FIX = pathlib.Path("/Users/matthewsegura/.hermes/hermes-agent/tests/fixtures/launch_intake")
    failures = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        global failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" :: {detail}" if detail and not cond else ""))
        if not cond:
            failures += 1

    print("== golden fixtures: invariants must pass; net-new findings are INFORMATIONAL (real gaps the audit predicted) ==")
    for fn in ("grow_app_one_week.contract.json", "land_wholesaling.contract.json"):
        p = FIX / fn
        if not p.exists():
            print(f"  (skip {fn}: not found)")
            continue
        c = json.loads(p.read_text())
        r = assess_launch_completeness(c)  # report-mode
        check(f"{fn}: invariants ok (no hard errors)", r["ok"], f"errors={r['errors']}")
        netnew = {k: v["findings"] for k, v in r["dimensions"].items() if k != "invariants" and v["findings"]}
        for dim, finds in netnew.items():
            for fnd in finds:
                print(f"    (real finding) {fn}: {dim}: {fnd[:140]}")
        # the follow_up cadence knob is optimizer-managed -> must NOT be flagged dead (false-positive guard)
        tun_find = " ".join(r["dimensions"].get("tunable_consumer_binding", {}).get("findings", []))
        check(f"{fn}: managed cadence knob not flagged dead", "follow_up_interval_hours" not in tun_find, tun_find)

    print("== synthetic broken contracts should each warn the right dimension ==")
    base = {
        "objective": {"statement": "x", "success": ["MRR to $12k by day 7"], "failure": ["churn > 10%"], "constraints": ["no spam"]},
        "workflow": {"stages": [{"key": "s1", "actions": [{"key": "a1", "side_effect_class": "internal"}]}]},
        "event_loops": [{"entity": "lead", "triggers": ["t"], "terminal_states": ["closed"], "stop_conditions": ["done"]}],
        "tunables": {},
    }
    # 1. prose success -> success_scoreability warns
    c1 = json.loads(json.dumps(base)); c1["objective"]["success"] = ["close some deals", "do a great job"]
    r1 = assess_launch_completeness(c1)
    check("prose success warns", any("success_scoreability" in w for w in r1["warnings"]), str(r1["warnings"]))
    # 2. action missing side_effect_class -> warns
    c2 = json.loads(json.dumps(base)); c2["workflow"]["stages"][0]["actions"].append({"key": "send_sms", "label": "text the seller"})
    r2 = assess_launch_completeness(c2)
    check("missing side_effect_class warns", any("side_effect_class_coverage" in w for w in r2["warnings"]), str(r2["warnings"]))
    # 3. event_loop with free-text stop only -> zombie warns
    c3 = json.loads(json.dumps(base)); c3["event_loops"] = [{"entity": "lead", "triggers": ["t"], "stop_conditions": ["when done"]}]
    r3 = assess_launch_completeness(c3)
    check("free-text-only loop stop warns", any("event_loop_termination" in w for w in r3["warnings"]), str(r3["warnings"]))
    # 4. unread (non-managed) tunable -> dead knob warns
    c4 = json.loads(json.dumps(base)); c4["tunables"] = {"some_business_threshold": {"default": 5, "range": [1, 10]}}
    r4 = assess_launch_completeness(c4)
    check("unread tunable warns", any("tunable_consumer_binding" in w for w in r4["warnings"]), str(r4["warnings"]))
    # 4b. same tunable bound by a sensor knob -> no warn
    c4b = json.loads(json.dumps(c4)); c4b["sensors"] = [{"kind": "heartbeat", "knobs": {"stall_timeout": "some_business_threshold"}}]
    r4b = assess_launch_completeness(c4b)
    check("referenced tunable does NOT warn", not any("tunable_consumer_binding" in w for w in r4b["warnings"]), str(r4b["warnings"]))
    # 5. enforce-mode turns net-new findings into hard errors (ok=False)
    r5 = assess_launch_completeness(c1, enforce=True)
    check("enforce-mode makes prose success a hard error", (not r5["ok"]) and any("success_scoreability" in e for e in r5["errors"]), str(r5))

    print("== net-new dimensions C + E: win-signal, evidence namespace, reachability, distinct terminals ==")
    # C1. conversational loop whose terminals carry NO win-class outcome -> warns
    cC = json.loads(json.dumps(base))
    cC["event_loops"] = [{
        "entity": "lead", "triggers": ["inbound_reply", "follow_up_timer"],
        "terminal_states": ["dead", "recycled", "paused"],
    }]
    rC = assess_launch_completeness(cC)
    check("conversion loop with no win terminal warns", any("win_signal_rail" in w for w in rC["warnings"]), str(rC["warnings"]))
    # C1b. same loop but one terminal IS a win ('under_contract') -> no win warn
    cCb = json.loads(json.dumps(cC)); cCb["event_loops"][0]["terminal_states"] = ["under_contract", "dead", "recycled"]
    rCb = assess_launch_completeness(cCb)
    check("conversion loop WITH win terminal does not warn", not any("win_signal_rail" in w for w in rCb["warnings"]), str(rCb["warnings"]))
    # C1c. a NON-conversational loop (no inbound trigger) is exempt from C
    cCc = json.loads(json.dumps(cC)); cCc["event_loops"][0]["triggers"] = ["daily_timer"]
    rCc = assess_launch_completeness(cCc)
    check("non-conversational loop is exempt from win_signal_rail", not any("win_signal_rail" in w for w in rCc["warnings"]), str(rCc["warnings"]))

    # E1. an evidence_required key produced by NOTHING -> evidence_namespace warns
    cE1 = json.loads(json.dumps(base))
    cE1["workflow"]["stages"] = [
        {"key": "s1", "actions": [{"key": "a1", "side_effect_class": "internal"}],
         "exit_criteria": [{"transition": "done", "evidence_required": ["ghost_artifact"]}]},
        {"key": "done", "terminal": True, "exit_criteria": []},
    ]
    cE1["proof_requirements"] = ["some_other_proof"]
    rE1 = assess_launch_completeness(cE1)
    check("orphan evidence key warns", any("evidence_namespace" in w and "ghost_artifact" in w for w in rE1["warnings"]), str(rE1["warnings"]))
    # E1b. same key now produced by proof_requirements -> no warn
    cE1b = json.loads(json.dumps(cE1)); cE1b["proof_requirements"] = ["ghost_artifact"]
    rE1b = assess_launch_completeness(cE1b)
    check("evidence key produced by proof_requirements does not warn", not any("evidence_namespace" in w for w in rE1b["warnings"]), str(rE1b["warnings"]))
    # E1c. key produced by a worker_envelope required_proof (under runtime) -> no warn
    cE1c = json.loads(json.dumps(cE1)); cE1c["runtime"] = {"worker_envelopes": {"w": {"required_proof": ["ghost_artifact"]}}}
    rE1c = assess_launch_completeness(cE1c)
    check("evidence key produced by worker_envelope does not warn", not any("evidence_namespace" in w for w in rE1c["warnings"]), str(rE1c["warnings"]))

    # E2a. an island stage (transition to a missing stage) -> reachability warns
    cE2 = json.loads(json.dumps(base))
    cE2["workflow"]["stages"] = [
        {"key": "s1", "exit_criteria": [{"transition": "nowhere", "evidence_required": []}]},
        {"key": "done", "terminal": True, "exit_criteria": []},
    ]
    cE2["proof_requirements"] = []
    rE2 = assess_launch_completeness(cE2)
    check("island stage ref warns reachability", any("stage_reachability" in w for w in rE2["warnings"]), str(rE2["warnings"]))
    # E2b. no terminal stage at all -> reachability warns
    cE2b = json.loads(json.dumps(base))
    cE2b["workflow"]["stages"] = [
        {"key": "s1", "exit_criteria": [{"transition": "s2", "evidence_required": []}]},
        {"key": "s2", "exit_criteria": [{"transition": "s1", "evidence_required": []}]},
    ]
    rE2b = assess_launch_completeness(cE2b)
    check("no-terminal cycle warns reachability", any("stage_reachability" in w and "terminal" in w for w in rE2b["warnings"]), str(rE2b["warnings"]))
    # E2c. a clean linear graph reaching a terminal -> no reachability warn
    cE2c = json.loads(json.dumps(base))
    cE2c["workflow"]["stages"] = [
        {"key": "s1", "exit_criteria": [{"transition": "done", "evidence_required": []}]},
        {"key": "done", "terminal": True, "exit_criteria": []},
    ]
    rE2c = assess_launch_completeness(cE2c)
    check("reachable linear graph does not warn reachability", not any("stage_reachability" in w for w in rE2c["warnings"]), str(rE2c["warnings"]))

    # E3. a conversational loop with a SINGLE terminal -> distinct_terminals warns
    cE3 = json.loads(json.dumps(base))
    cE3["event_loops"] = [{"entity": "lead", "triggers": ["inbound_reply", "follow_up_timer"], "terminal_states": ["closed"]}]
    rE3 = assess_launch_completeness(cE3)
    check("single-terminal conversation warns distinct_terminals", any("distinct_terminals" in w for w in rE3["warnings"]), str(rE3["warnings"]))
    # E3b. distinct done/paused/disqualified terminals -> no distinct warn
    cE3b = json.loads(json.dumps(cE3)); cE3b["event_loops"][0]["terminal_states"] = ["closed", "paused", "disqualified"]
    rE3b = assess_launch_completeness(cE3b)
    check("distinct terminals do not warn distinct_terminals", not any("distinct_terminals" in w for w in rE3b["warnings"]), str(rE3b["warnings"]))

    print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILURES'}")
    sys.exit(1 if failures else 0)
