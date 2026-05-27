# Tasks: xhigh accelerator

## Phase 0 — Baseline and guardrails

- [ ] Capture current xhigh baseline for a small golden set of local prompts: Hermes-config question, codebase-orientation question, source-edit question, and general architecture question.
- [ ] Record wall time, model-call count, tool-call count, and perceived quality notes for each baseline turn.
- [ ] Confirm `display.streaming: true` is active in the fresh Hermes session used for testing.

## Phase 1 — Telemetry only, no behavior change

- [x] Add `agent/turn_telemetry.py` with a defensive per-turn telemetry object and JSON-safe summary output.
- [x] Add disabled-or-low-noise config defaults in `hermes_cli/config.py`.
- [x] Initialize telemetry at the top of `agent/conversation_loop.py::run_conversation`.
- [x] Record preflight compression duration without changing compression behavior.
- [x] Record request message count, rough token estimate, request char count, and tool count at the existing request-size calculation site.
- [x] Wrap the streaming first-delta callback to record time to first delta without changing spinner behavior.
- [x] Record API-call final duration at the existing `api_duration` site.
- [x] Extract cache-token hints from normalized usage where available.
- [x] Add one compact `turn_performance` log line and attach summary to the result dict under `performance`.
- [x] Add unit tests for telemetry summary shape and failure isolation.
- [x] Run focused tests for telemetry and conversation loop.

## Phase 2 — Stateful Codex Responses replay

- [ ] Add disabled-by-default config for `performance.responses_state.enabled` and stateless fallback behavior.
- [ ] Teach the Codex Responses transport to support opt-in `store: true` plus `previous_response_id` while preserving the current `store: false` path.
- [ ] Add a state-chain compatibility fingerprint covering model, provider, reasoning config, stable instructions/system prompt, tool schema, and session branch.
- [ ] On follow-up tool-loop calls, send only new delta input items when state is valid; reset state and send a full request when the fingerprint changes.
- [ ] On provider rejection, missing state, or expired state, mark the chain invalid and retry the current turn through the existing stateless full-replay path.
- [ ] Extend telemetry with stateful replay usage, fallback reason, state reset reason, previous-response usage, full-vs-delta request size, and stateless retry count.
- [ ] Add unit tests for disabled path compatibility, successful stateful chaining, state reset, and fallback to stateless on 400/404-style failures.
- [ ] Add a tiny live/manual smoke test plan before local enablement.

## Phase 3 — Tool batch attribution

- [ ] Wrap `agent._execute_tool_calls(...)` from the main loop with telemetry batch start/end.
- [ ] Record individual tool duration and success/failure in `agent/tool_executor.py` for both sequential and concurrent paths.
- [ ] Ensure telemetry never changes the order of tool result messages.
- [ ] Add tests for sequential and concurrent tool timing with mocked tools.
- [ ] Run focused tool-executor tests.

## Phase 4 — Context prefetch core, disabled by default

- [ ] Add `agent/context_prefetch.py` with packet builder, budgets, and source-provider interface.
- [ ] Add config defaults for `context_prefetch.enabled`, total budget, per-source budgets, and initial sources.
- [ ] Call the prefetch builder once per turn in `agent/conversation_loop.py`, after the user message is appended and before the first API request is built.
- [ ] Inject the packet into the API-only current user message path alongside memory and plugin context; do not mutate persisted `messages`.
- [ ] Report prefetch duration and packet size in telemetry.
- [ ] Add tests proving disabled path injects nothing and enabled path injects bounded source-labeled text.

## Phase 5 — Initial source providers

- [ ] Implement skill-card provider using existing `agent.skill_utils` helpers; inject compact cards, not full skill bodies, unless explicitly configured.
- [ ] Implement sanitized Hermes profile/config summary provider using allowlisted keys only: model/provider/api_mode/reasoning/service_tier/display.streaming/toolsets/disabled_toolsets/compression/browser.engine.
- [ ] Preserve existing `pre_llm_call` plugin hook and include plugin-provided context inside the same budgeted packet or as a separately timed contributor.
- [ ] Add tests for secret redaction/allowlisting and deterministic skill-card ordering.

## Phase 6 — Local rollout and measurement

- [ ] Enable telemetry locally in Matthew's default profile.
- [ ] Enable stateful Responses replay locally only after focused tests and a small live smoke pass.
- [ ] Enable context prefetch locally after focused tests pass.
- [ ] Re-run the golden prompt set.
- [ ] Compare wall time, model-call count, tool-call count, and final quality against baseline.
- [ ] If quality regresses, disable prefetch and keep telemetry only; use telemetry to identify safer sources.

## Phase 7 — Optional UX/reporting

- [ ] Add a read-only `/perf` or `hermes insights --performance` view if JSON logs are useful but inconvenient.
- [ ] Consider a profile-local wiki prefetch provider for Matthew's environment only, implemented as a plugin/source provider rather than hardcoded core behavior.
- [ ] Document the feature in docs once the measured local result is positive.

## Validation commands

- [x] Run focused tests for new modules and touched runtime files.
- [x] Run existing conversation-loop regression tests.
- [ ] Run existing tool-executor tests.
- [x] Run config/default migration tests.
- [x] Audit `git diff` manually before reporting completion.
- [x] After any code changes, run `graphify update .` in the Hermes repo to refresh the graph.
