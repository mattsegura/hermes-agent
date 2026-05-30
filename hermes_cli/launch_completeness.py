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

    print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILURES'}")
    sys.exit(1 if failures else 0)
