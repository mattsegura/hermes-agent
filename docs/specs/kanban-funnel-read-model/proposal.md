---
status: active
created: 2026-05-26
feature: kanban-funnel-read-model
---

# Proposal: Kanban Funnel Read Model

## Summary

Add a generic Hermes Kanban funnel/read-model so agents and humans can see semantic goal flow over the same cards instead of reasoning only from lifecycle columns.

## What

- Add optional semantic fields to Kanban tasks: goal, workstream, stage, action, and structured funnel data.
- Derive a board-wide funnel read-model from task semantic fields, dependency links, lifecycle state, run history, events, artifacts, and completion metadata.
- Support compound stages by reading structured per-entity state from `funnel_data` / run metadata / event payloads. A top-level stage can then expand into nested entities, substates, next actions, blockers, outcomes, and proof without hardcoded domain stages.
- Expose the read-model through `hermes kanban funnel --json` and an orchestrator-only `kanban_funnel` tool.
- Make the existing pipeline optimizer MCP `funnel` query use the generic Hermes read-model instead of domain/title keyword heuristics.

## Why

Kanban columns answer what can run. They do not explain whether work is moving from owner intent to proof/outcome. The optimizer needs a semantic view with stage metrics, blockers, artifacts, dependencies, and conversion/backflow signals.

## Scope

In scope:
- Additive SQLite schema migration only.
- Generic read-model builder with no domain-specific stage names or title keyword inference.
- CLI/tool output for downstream UI/optimizer use.
- Focused tests around schema persistence, read-model structure, and tool gating.

Not in scope:
- Full visual Canvas UI.
- Full watch-route runtime table.
- Rewriting dispatch semantics around stage routing.
- Hardcoded business-specific stages.

## Success criteria

- Existing Kanban boards migrate without data loss.
- New tasks can carry semantic funnel coordinates.
- `build_funnel_read_model()` groups cards by goal/workstream/stage/action and reports lifecycle counts, durations, blockers, artifacts/proof, and dependency edges.
- Compound stages report `entity_metrics` and `entity_states` from explicit structured entity records; no title/prose inference is used.
- `hermes kanban funnel --json` returns a stable JSON contract.
- `kanban_funnel` is visible to orchestrator profiles with the `kanban` toolset and hidden from dispatcher-spawned workers.
- Pipeline optimizer MCP `pipeline_query funnel` returns the generic read-model and contains no domain keyword stage inference.
