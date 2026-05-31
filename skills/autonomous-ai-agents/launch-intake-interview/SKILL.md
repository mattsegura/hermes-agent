---
name: launch-intake-interview
description: Conduct launch intake interviews that fill the board operating contract. Load during kanban_business_launch_review when needs_clarification, relaying questions, or follow-ups.
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, launch, intake, contract, interview]
    related_skills: [model-route, kanban-orchestrator]
---

# Launch Intake Interview

Skill-driven behavior for Layer 2 launch intake. **Tools harden input** (server generates questions, assesses answers, synthesizes contract); **this skill controls how you conduct the interview** and map answers to contract fields.

## When to load

Load this skill when ANY of these are true:

- `kanban_match_board` returned `intent: board_required` or confirmed `maybe_board` after one clarifying exchange
- `kanban_business_launch_review` returns `needs_clarification`, `assistant_next_action.type` of `launch_intake_clarification`, or `intake_interview_guidance`
- You are relaying server-generated intake questions or submitting `intake_answers`
- Pre-synthesis: owner answers exist but coverage gaps remain

Do **not** load for `no_board_needed` — answer inline instead.

## Interview flow

1. **Pre-interview research** — the server runs this before questions. Use `launch_intake.external_research` in tool results; do not re-research unless the owner adds a new domain.
2. **Relay questions** — pass server `generated_questions` to the owner (light tone edits only). Batch into ONE message.
3. **Collect answers** — submit via `kanban_business_launch_review` `intake_answers`. Never submit identical answers twice.
4. **Follow-ups** — if assessment returns insufficient, ask 1-4 sharper questions (not the same ones). Max rounds then partial draft escalation.
5. **Synthesis** — server drafts contract with ranked `workflow.stages`. Present `owner_contract_summary`; owner approves with `/approve <slug>`.

## Coverage dimensions (map answers here)

| Dimension | What to capture |
| --- | --- |
| outcome_signals | Metrics, thresholds, time horizons |
| subject_scope | Leads, deals, accounts, audience |
| allowed_context | Channels, tools, hard bans |
| workflow_path | First signal to terminal outcome |
| workflow_stages | Ranked stages + conversation wait points |
| integration_points | GitHub, CRM, SMS, ads, analytics APIs |
| approval_boundaries | Autonomous vs owner-gated actions |
| proof_and_stops | Artifacts, updates, stop conditions |

## Domain playbooks (examples)

### Land wholesaling

Stages: source leads -> skip trace/qualify -> initial outreach -> negotiate -> under contract -> close/disqualify.

Capture: SMS/call consent, follow-up cadence, offer ranges, proof of seller intent, when to escalate to owner on price/terms.

### App growth (iOS/Android)

Stages: research channel -> creative/test -> publish -> measure -> iterate.

Capture: App Store vs Play, attribution (RevenueCat etc.), quality bar for creative, spend caps, approval before public posts.

### App development

Stages: repo setup -> implement -> CI/test -> review -> release.

Capture: GitHub repo, stack, branch policy, what workers may merge vs owner-only.

### Content (TikTok/social)

Stages: ideate -> draft -> owner review -> publish -> measure engagement.

Capture: quality metrics (retention, CTR), brand voice boundaries, approval before post.

## Anti-patterns

- Do NOT loop the same questions — check `answer_history` and coverage gaps
- Do NOT skip workflow stages — every durable goal needs A-to-Z stages in the contract
- Do NOT ask yes/no checkboxes — one natural clarifying question at Layer 1 only
- Do NOT expose schema keys (`side_effect_policy`, enum names) to the owner
- Do NOT self-approve launch — only `/approve <slug>` from the owner

## Recovery from weak answers

- Summarize what you understood, state assumptions explicitly, ask ONE targeted gap question
- If `intake_loop_detected` in tool result: acknowledge duplicate submission, do not re-ask
- If `partial_escalation`: present partial draft, list coverage gaps plainly, invite owner to amend before approve

## After intake (not this skill)

- **Model Route skill** — when contract is drafted and models unset
- **Credentials** — `kanban_launch_credentials_status` before approval
- **Payment** — separate gate; less critical at intake
