---
status: active
created: 2026-05-26
feature: kanban-funnel-read-model
---

# Tasks: Kanban Funnel Read Model

- [x] Add task semantic funnel fields and additive migrations in `hermes_cli/kanban_db.py`.
- [x] Implement `build_funnel_read_model()` over tasks, links, runs, events, completion metadata, blockers, and artifacts.
- [x] Wire CLI creation flags and `hermes kanban funnel --json` in `hermes_cli/kanban.py`.
- [x] Wire `kanban_funnel` and funnel-aware `kanban_create` fields in `tools/kanban_tools.py`.
- [x] Point the live pipeline intelligence MCP `funnel` query at the generic Hermes read-model.
- [x] Add focused DB/tool tests and run targeted pytest/static checks.
- [x] Audit diffs and update wiki with implementation status.
- [x] Add compound-stage entity aggregation to `build_funnel_read_model()`.
- [x] Add focused tests for generic nested entity/substate metrics.
- [x] Backfill the live `land-wholesaling` board with structured semantic stage coordinates and per-lead negotiation entity data where current run metadata makes that possible.
