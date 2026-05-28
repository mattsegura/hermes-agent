# Kanban Reactive Entities

Serious/company Kanban boards can model durable external or resource state as
reactive entities. A reactive entity belongs to a task card, can sleep on an
active `task_watch_routes` row, wakes on a normalized trigger, and blocks
terminal completion until it is resolved with structured proof.

The implementation is generic:

- `reactive_entities` stores entity identity, `entity_type`, owning `task_id`,
  state/substate, external identity mapping, owner/capability/assignee,
  allowed trigger types, authority limits, proof requirements, terminal
  outcome, active/terminal flags, metadata, and linked watch route.
- `reactive_trigger_audit` records matched, duplicate, and unknown inbound
  triggers. Matched triggers still use existing `task_events` and
  `task_watch_routes.trigger_payload` so existing audit readers keep working.
- Conversation threads are represented as
  `entity_type="conversation_thread"` with external identity such as
  `{"platform": "mock", "thread_id": "thread-1"}`. No outbound transport is
  implied by the entity layer.

Use `create_reactive_entity_card(...)` to create a synthetic entity card from
contract/template data and optionally activate a watch route. Use
`trigger_reactive_event(...)` for normalized inbound events; unknown routes can
create a triage card instead of disappearing. Use `resolve_reactive_entity(...)`
to mark the entity terminal after its required proof is present.

For boards with `runtime.mode == "company"`, `complete_task(...)` evaluates the
root task, its descendants, and all scoped reactive entities. Active unresolved
entities, malformed entity metadata, or missing entity proof fail the same
contract done gate used by the CLI and dashboard.
