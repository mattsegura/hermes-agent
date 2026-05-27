# Design: xhigh accelerator

## Areas affected

Primary runtime:

- `agent/conversation_loop.py` — start/end per-turn telemetry, preflight timing, inject context prefetch before the first API call, wrap model-call first-delta callback, attach final telemetry to result metadata.
- `agent/tool_executor.py` — aggregate per-batch tool wall time and per-tool durations into telemetry while preserving current logs/callbacks.
- `agent/chat_completion_helpers.py` — expose first-delta and request timing signals in a transport-neutral way where practical.
- `agent/system_prompt.py` — no prompt-content changes expected; must preserve cached system-prompt invariant.
- `hermes_cli/config.py` — add disabled-by-default config keys for the accelerator.

New modules:

- `agent/turn_telemetry.py` — lightweight per-turn timing/measurement object and JSON-safe summary helpers.
- `agent/context_prefetch.py` — deterministic context packet builder with provider registry and budgets.

Optional CLI/reporting later:

- `hermes_cli/commands.py` and `cli.py` — optional `/perf` command or integration into `/usage`, after the runtime data exists.
- `hermes_state.py` — optional durable storage if JSON logs are insufficient; not needed for the first slice.

Tests:

- `tests/agent/test_turn_telemetry.py`
- `tests/agent/test_context_prefetch.py`
- `tests/agent/test_conversation_loop_xhigh_accelerator.py`
- Existing conversation-loop and tool-executor tests should remain green.

## Config shape

Add a top-level `performance` or `context_prefetch` section. Recommended naming is generic, not xhigh-specific, because the mechanism helps any slow reasoning model:

- `performance.telemetry.enabled`: default `true` or `false` depending noise tolerance; safe even when enabled because it only logs metrics.
- `performance.telemetry.log_turn_summary`: default `true` once telemetry is enabled.
- `context_prefetch.enabled`: default `false` upstream.
- `context_prefetch.max_chars`: default bounded packet size.
- `context_prefetch.sources.skills.enabled`: default `true` when prefetch is enabled.
- `context_prefetch.sources.profile_config.enabled`: default `true` when prefetch is enabled.
- `context_prefetch.sources.plugins.enabled`: default `true`, allowing installed plugins to contribute through existing hook mechanics.

Avoid model-specific config names such as `xhigh_mode`; the user-level reason is xhigh, but the implementation is a general accelerator.

## Architecture

### 1. Turn telemetry

Create a small object at the start of `run_conversation` and attach it to the agent for this turn only. It records:

- turn start/end wall time;
- preflight context/compression/prefetch duration;
- per API-call start/end duration;
- time to first visible delta when streaming exposes it;
- retry count and fallback activation markers;
- per tool batch wall duration;
- individual tool names, durations, and success/failure flags;
- request message count, rough token estimate, request char count, tool count;
- usage totals and cached-token hints from `response.usage` where available.

Telemetry must not change runtime behavior. Failures in telemetry must be swallowed with debug logging.

### 2. Context prefetch packet

Build a packet after the current user message exists and after memory-manager prefetch is available, but before constructing the first API request. Inject packet text into the API-only copy of the current user message, using the same non-persisted pattern already used for memory and plugin context.

Packet rules:

- deterministic and read-only;
- source-labeled;
- bounded by per-source and total character budgets;
- never writes files, calls shell, calls LLMs, or makes network requests in the core path;
- never mutates the cached system prompt;
- never mutates persisted `messages`;
- if no confident source is available, returns empty string rather than noise.

Initial source providers:

1. Skill cards: scan installed skills using existing `agent.skill_utils` helpers, rank by task-description overlap and configured pinned/required metadata, inject only compact cards unless the skill is explicitly configured for auto-load.
2. Profile/config summary: when the user asks about Hermes/profile/config/runtime, inject a sanitized summary of model/provider/reasoning/display/toolset/compression settings. Do not include API keys, raw `.env`, or status output that can expose secrets.
3. Plugin contributions: preserve existing `pre_llm_call` hook behavior, but report its duration and budget its text in the combined packet.

Later source providers can include session DB FTS, repo orientation, or wiki adapters, but those should be provider plugins or profile-local integrations rather than hardcoded core assumptions.

### 3. First-token timing

`conversation_loop.py` already calls `_interruptible_streaming_api_call(api_kwargs, on_first_delta=_stop_spinner)`. Wrap that callback so it both stops the spinner and records telemetry. For non-streaming fallback, first-delta time is null and final duration still records.

For Codex Responses, preserve the existing transport behavior and do not bypass `agent._get_transport().preflight_kwargs(...)`.

### 4. Tool batch timing

Wrap `agent._execute_tool_calls(...)` in the loop with telemetry batch start/end. Inside `agent/tool_executor.py`, record individual tool durations that are already computed. The aggregate should work for both sequential and concurrent paths.

### 5. Result/reporting

Add a compact telemetry summary to the returned result under a non-breaking key such as `performance`. Existing callers ignore unknown keys. Also write a single JSON line to `agent.log` with a stable prefix such as `turn_performance` for quick grep and future `/perf` work.

## Prompt-cache and quality constraints

- Do not alter `agent._cached_system_prompt` for prefetch content.
- Do not inject volatile context into the system prompt.
- Do not remove tools dynamically from `agent.tools` mid-session.
- Do not replace the model's final reasoning with deterministic classifications.
- Treat prefetch as input retrieval only; final judgment stays with the xhigh model.

## Risks and mitigations

- Risk: prefetch adds irrelevant context and hurts quality. Mitigation: keep budgets small, source-label everything, and return empty when not useful.
- Risk: skill ranking becomes hidden intent logic. Mitigation: ranking is retrieval only; the model still decides how to use the context and can call tools normally.
- Risk: telemetry creates log noise. Mitigation: config gate and one compact JSON line per turn.
- Risk: config summary leaks secrets. Mitigation: use allowlisted config keys only; never dump raw config or env.
- Risk: first implementation overfits Matthew's wiki. Mitigation: core ships generic providers; wiki-specific retrieval belongs in plugin/profile-local source provider.

## Open questions

- Should telemetry default on because it is non-behavioral, or off to keep logs quiet?
- Should compact skill cards be injected automatically whenever skills tools are available, or only when context prefetch is enabled?
- Should the first local rollout use a profile-local plugin for Matthew's wiki, then upstream the generic source-provider interface after measurement?
