# Proposal: xhigh accelerator

## Summary

Keep `agent.reasoning_effort: xhigh` for final reasoning, but reduce wall-clock latency by eliminating avoidable model/tool loops and measuring latency at turn-level granularity.

## What

Add an opt-in xhigh accelerator layer with three initial capabilities:

1. Per-turn latency telemetry that attributes time to deterministic preflight, model calls, first-token delay, final-token delay, tool batches, retries, prompt size, tool schema size, transport replay mode, and cache/state indicators.
2. Opt-in stateful Responses replay for Codex-compatible Responses transports: keep xhigh and the same tools, but avoid resending the full conversation/tool-output history on every tool-loop iteration by using provider-side response state when supported, with automatic fallback to the existing stateless replay path.
3. Deterministic bounded context prefetch before the first model call, injected into the current user turn rather than the system prompt, so the model starts with relevant source-labeled context instead of spending xhigh loops discovering it.

The feature must preserve prompt-cache invariants, tool availability, and final reasoning quality.

## Why

`xhigh` is currently the best reasoning setting, but each extra model/tool iteration is expensive. The fastest quality-preserving path is not lowering reasoning effort; it is reducing unnecessary xhigh round trips and reducing repeated full-history replay inside necessary tool loops, then proving improvements with telemetry.

## Scope

In scope:

- Add a config-gated accelerator path; default behavior remains unchanged unless explicitly enabled.
- Add read-only telemetry around existing model calls and tool execution.
- Add config-gated stateful Responses replay for providers that support stored responses / `previous_response_id`, with a stateless fallback path.
- Add a small deterministic prefetch pipeline using source providers with strict budgets.
- Use the existing API-call-time user-message injection pattern so system prompt bytes stay stable.
- Start with Hermes-profile/Hermes-config questions and general skill-card retrieval as the first source providers.
- Add focused tests proving no behavior change when disabled and bounded source-labeled context when enabled.

## Not in scope

- Lowering `xhigh` globally.
- Rewriting model selection or final-answer generation.
- Dynamically hiding tools mid-session.
- Blindly compressing tool schemas or prompt guidance.
- Provider-specific prompt-cache hacks that break other transports or lack stateless fallback.
- Full semantic memory replacement or broad wiki automation in core Hermes.

## Success criteria

- Disabled config path is byte-for-byte behavior compatible for request construction except for optional telemetry logging.
- Enabled context prefetch never mutates persisted conversation history or system prompt.
- Prefetch packets are source-labeled, bounded, deterministic, and read-only.
- Stateful Responses mode resets deterministically when the model, provider, tool schema, system prompt fingerprint, or session branch changes.
- Unsupported or expired provider-side state falls back to the current full-replay request without losing the turn.
- A Hermes-config question that previously required one or more context-gathering tool loops can start the first xhigh model call with relevant skill/config context.
- Telemetry can report per-turn: model call count, API duration, time to first delta where available, tool batch wall time, request size, tool count, stateful replay usage/fallbacks, and cache-token hints when provider usage exposes them.
- Focused tests pass for conversation loop, telemetry, prefetch packet building, and config defaults.

## Rollout

- Ship disabled by default in upstream-safe form.
- Enable locally in Matthew's default profile after tests pass.
- Compare a small golden set of xhigh turns before and after enabling: wall time, model-call count, tool-call count, and final answer quality.
