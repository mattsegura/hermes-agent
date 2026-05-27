---
status: active
created: 2026-05-26
feature: kanban-funnel-read-model
---

# Design: Kanban Funnel Read Model

## Areas affected

- `hermes_cli/kanban_db.py` — schema, task dataclass, creation API, read-model builder.
- `hermes_cli/kanban.py` — CLI flags and `funnel` subcommand.
- `tools/kanban_tools.py` — structured agent tool surface.
- `tests/hermes_cli/test_kanban_db.py` and `tests/tools/test_kanban_tools.py` — focused coverage.
- `~/.hermes/scripts/pipeline-intelligence-mcp.py` — live optimizer bridge should call the core read-model.

## Data model

Add nullable task columns:

- `goal_id TEXT` — owner intent / goal grouping.
- `workstream_id TEXT` — sub-flow under a goal.
- `stage_key TEXT` — semantic stage in the goal flow.
- `action_key TEXT` — finer-grained action within a stage.
- `funnel_data TEXT` — JSON object for structured, non-column semantics.

No existing column is repurposed. Legacy `workflow_template_id` and `current_step_key` remain supported as fallback sources for workstream/stage.

## Read-model contract

`build_funnel_read_model(conn, board=None, include_archived=False, limit_cards_per_stage=20)` returns:

- `version`
- `board`
- `generated_at`
- `summary`
- `stages[]`
- `edges[]`
- `uncategorized[]` only when needed

Stage grouping key:

`goal_id || "default"` + `workstream_id || workflow_template_id || "default"` + `stage_key || current_step_key || "unclassified"` + `action_key || "default"`

No title keyword inference is allowed. If a task has no semantic coordinates, it goes to `unclassified` with `semantic_source: fallback`.

### Compound stage entity contract

`funnel_data`, run metadata, and event payloads may carry explicit nested entity records under generic keys:

- `entities`
- `funnel_entities`
- `items`

Each entity is a JSON object. Recommended fields:

- `id` — stable entity/opportunity/conversation id.
- `type` — generic entity type (`lead`, `conversation`, `artifact`, etc.); the core treats it as a label only.
- `label` — human-readable label.
- `stage` / `stage_key` — optional entity stage override. Defaults to the card's stage.
- `state` / `substate` / `status` — entity's nested state within the top-level stage.
- `terminal` — boolean terminal marker.
- `blocked` — boolean blocked marker.
- `next_action` — string or object describing what should happen next.
- `actor` / `owner` — who owns the next action.
- `outcome`, `proof`, `artifacts`, `metrics` — structured evidence and measurements.

The read model aggregates these into `entity_metrics`, `entity_states`, `entity_next_actions`, and bounded `entities[]` on the stage. This is generic: a land negotiation board can model per-lead conversations, while another board can model per-customer onboarding, incident substeps, or fulfillment items using the same contract.

## Metrics

Per stage:

- lifecycle counts by status
- active/waiting/blocked/done totals
- completed count
- average cycle seconds from created/start to completion/run end
- failure count from task failure counters and failed run outcomes
- artifact/proof/outcome presence from run metadata and completed event payloads
- compound entity counts by type/state/terminal/blocked/next-action owner
- card summaries bounded by `limit_cards_per_stage`

Edges are derived from `task_links` when parent and child belong to different stage nodes.

## Tool/API behavior

- `hermes kanban create` accepts `--goal`, `--workstream`, `--stage`, `--action`, `--funnel-data JSON`.
- `kanban_create` accepts `goal`, `workstream`, `stage`, `action`, `funnel_data`.
- `kanban_complete.metadata` documentation explicitly encourages artifact/proof/outcome/capability fields.
- `kanban_complete.metadata` and task `funnel_data` can include `entities[]` for compound stages.
- `kanban_funnel` is orchestrator-only, same visibility as `kanban_list`.

## Constraints

- Do not overwrite user dirty work in the repo.
- Additive migration only; no destructive schema changes.
- No domain-specific examples or fixed stage names.
- No hardcoded thresholds for optimizer decisions.
- Domain-specific backfills are data operations against a board, not logic in the generic read-model.
