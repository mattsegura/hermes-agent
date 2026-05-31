---
name: model-route
description: Guide owners through quality-vs-budget model selection during kanban board launch. Writes runtime.models and runtime.budget on the operating contract before approval.
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, launch, models, budget, routing]
    related_skills: [kanban-orchestrator]
---

# Model Route — launch-time model selection

Use this skill when a board contract reaches **contract review** and `runtime.models` is empty or the owner has not chosen a routing preference.

## When to invoke

- Launch intake is complete and a draft contract exists.
- The owner asks about model cost, quality, or "which model should this board use?"
- Before `/approve <board>` or `hermes kanban boards contract approval-token`.

## Conversation flow

1. **Confirm goal sensitivity** — irreversible external actions, compliance, creative quality bar.
2. **Offer routing modes** (pick one with the owner):
   - **Quality** — highest-capability model per task type (`runtime.budget.preference=quality`).
   - **Balanced** — strong default with cheaper models for triage/research (`preference=balanced`).
   - **Budget** — minimum viable models that still meet stated success metrics (`preference=budget`).
3. **Map roles** — populate `runtime.models`:
   - `default` — board-wide fallback
   - `roles.ceo`, `roles.optimizer`, `roles.worker`, `roles.dispatcher`
   - `task_types.triage|implementation|review|research|specification`
   - `stages.<stage_key>` and `actions.<action_key>` when stages need different capability
4. **Tie to budget** — set `runtime.budget.weekly_usd_cap` when the owner gives a cap; run cost projection (estimates) and show `runtime.cost_projection.total_usd_weekly` with the **estimate** disclaimer.
5. **Write back** — merge via `kanban_business_launch_review` / `hermes kanban boards contract review` with updated contract JSON; never approve in the same turn unless the owner explicitly approves.

## Example runtime.models payload

```json
{
  "runtime": {
    "budget": {
      "preference": "balanced",
      "weekly_usd_cap": 75,
      "weekly_task_estimate": 40,
      "tokens_per_task_estimate": 25000
    },
    "models": {
      "default": "composer-2.5-fast",
      "roles": {
        "ceo": "anthropic/claude-opus-4.8",
        "worker": "gpt-5.3-codex"
      },
      "task_types": {
        "triage": "composer-2.5-fast",
        "review": "anthropic/claude-sonnet-4.6"
      }
    }
  }
}
```

## Rules

- Slugs must come from the curated model catalog when possible; warn on unknown slugs.
- Label all cost numbers as **projected estimates** until metered billing is wired (P1).
- Do not skip launch intake or credentials gates to set models.
- Payment method (Stripe Connect, board-scoped) is separate from model routing — do not conflate API keys with billing.

## CLI shortcuts

```bash
hermes kanban boards contract status <board>
hermes kanban boards contract export <board> --format markdown
hermes kanban boards contract review <board> --contract @updated.json
```
