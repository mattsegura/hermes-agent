"""Server-side launch-intake orchestration (Phase 2).

Mirrors the auxiliary-model pattern in :mod:`hermes_cli.kanban_specify`: the
*server* (not the main chat model) drives launch intake by calling a dedicated
auxiliary model for three jobs:

    * ``run_question_generation``  - turn a rough goal into owner questions
    * ``run_answer_assessment``    - decide whether answers are good enough
    * ``run_contract_synthesis``   - draft a full board operating contract

This replaces the old "the chat model generates the questions / drafts the
contract" honor-system flow. The generic deterministic drafter
(``kanban_db._build_universal_contract_from_launch_intake``) is retained as an
explicit ``degraded_mode`` fallback for when no auxiliary model is configured.

Tolerance contract (same as ``kanban_specify``): every entry point degrades
gracefully. If the ``kanban_launch_intake`` auxiliary slot is not configured,
or the client import fails, or the API call errors, the ``*Result`` object
carries ``degraded=True`` and the caller falls back to the deterministic path.
Nothing here raises for an expected failure mode, and nothing here performs a
network call when the slot is unconfigured -- which is what keeps the unit
tests (isolated HERMES_HOME, no providers) fully offline.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

AUX_TASK = "kanban_launch_intake"

HERMES_LAUNCH_INTAKE_MAX_TOKENS = max(
    2000,
    int(os.getenv("HERMES_LAUNCH_INTAKE_MAX_TOKENS", "12000")),
)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class QuestionGenerationResult:
    ok: bool
    degraded: bool
    questions: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class AnswerAssessmentResult:
    ok: bool
    degraded: bool
    sufficient: bool = False
    follow_up_questions: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    evidence: str = ""
    reason: str = ""


@dataclass
class ContractSynthesisResult:
    ok: bool
    degraded: bool
    contract: Optional[dict[str, Any]] = None
    reason: str = ""


@dataclass
class CeoAmendmentProposal:
    """A structured structural-change proposal emitted by the CEO turn.

    Exactly one of ``proposed_contract`` / ``diff`` carries the structural
    change; ``rationale`` explains it; ``required_inputs`` declares any owner
    inputs (API keys, caps) needed before approval. This is handed verbatim to
    :func:`hermes_cli.kanban_db.propose_contract_amendment` (origin ``ceo``) --
    the conversation never mutates the live contract directly.
    """

    rationale: str
    proposed_contract: Optional[dict[str, Any]] = None
    diff: Optional[dict[str, Any]] = None
    required_inputs: list[Any] = field(default_factory=list)


@dataclass
class CeoTurnResult:
    """The CEO's reply to one owner message in a steering conversation.

    ``reply`` is the natural-language message shown to the owner. ``proposal``
    is set only when the conversation converged on a concrete STRUCTURAL change
    (it becomes a P5 amendment). ``research_query`` is set when the CEO wants
    grounded web research before answering (the engine runs it and re-asks).
    ``degraded`` mirrors the rest of this module: True means no usable aux model
    and the caller must emit a deterministic system message instead.
    """

    ok: bool
    degraded: bool
    reply: str = ""
    proposal: Optional[CeoAmendmentProposal] = None
    research_query: str = ""
    reason: str = ""


@dataclass
class ExternalResearchItem:
    """A single persisted pre-interview research finding."""

    query: str
    summary: str
    sources: list[str] = field(default_factory=list)
    fetched_at: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "summary": self.summary,
            "sources": list(self.sources),
            "fetched_at": self.fetched_at,
        }


@dataclass
class PreInterviewResearchResult:
    ok: bool
    degraded: bool
    items: list[ExternalResearchItem] = field(default_factory=list)
    reason: str = ""
    # ``grounded`` is True only when the items were synthesized from REAL web
    # search results (via the Hermes ``web`` toolset) rather than from the
    # auxiliary model's prior knowledge. Callers can use this to distinguish
    # citeable, fetched-source research from best-effort ungrounded research.
    grounded: bool = False
    # The flat list of real fetched sources (title/url/snippet) that grounded
    # this research, persisted as evidence. Empty when ``grounded`` is False.
    sources: list[dict[str, Any]] = field(default_factory=list)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.items]


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_QUESTION_GENERATION_PROMPT = """\
You are Hermes launch intake for a durable agentic workflow.

Given the owner's rough goal, surrounding context, and any pre-interview
research, generate the smallest set of plain-language clarification questions
needed before a board operating contract can be drafted. Do NOT use keyword
routing, regex matching, fixed industry templates, or prewritten question
lists. Infer the likely business or workflow shape from the whole request, then
ask only what is genuinely unknown and important.

Use pre-interview research aggressively: when research mentions platforms,
channels, regulations, or domain norms (e.g. iOS vs Android for app growth,
MLS/compliance for real estate, SMS consent for wholesaling, TikTok content
rules for social growth), turn those into specific confirmation questions
instead of generic ones.

The questions should help Hermes understand:
- the concrete outcome and measurable success/failure signals
- the people, items, accounts, or opportunities involved
- domain-specific channels, platforms, and constraints (infer from research)
- the end-to-end workflow as ranked stages (source -> qualify -> negotiate -> close)
- conversation/negotiation points where Hermes waits on replies
- integrations and systems (GitHub, CRM, SMS, ad platforms, analytics)
- where work should start and what systems or channels are allowed
- the real-world path from first signal through done, paused, or disqualified
- what Hermes may do autonomously versus what requires owner approval
- proof, updates, and stop conditions that would make execution trustworthy
- budget sensitivity and quality bar (when tradeoffs matter for model routing)

Rules:
- Ask 2 to 6 questions, written for a non-technical owner.
- Do not ask the owner to design stages, schemas, profiles, dispatchers, event
  loops, provider policies, or worker envelopes.
- If pre-interview research already answers a dimension, do not re-ask it; record
  it as an assumption to confirm instead.
- Prefer one sharp domain-specific question over two vague generic ones.

Output ONLY a JSON object:
{"questions": ["..."], "assumptions": ["..."]}
"""

_ANSWER_ASSESSMENT_PROMPT = """\
You are Hermes launch intake quality control.

Given the owner's rough goal, the questions asked, the owner's answers, any
pre-interview research, and a deterministic coverage report, decide whether the
answers are clear enough to draft a board operating contract. This is a
recursive intake loop: weak answers must produce better follow-up questions
instead of a guessed contract.

Assess against measurable success/failure signals, subject scope, allowed
context, the real-world workflow path, ranked workflow stages, integration
points, autonomous-vs-approval boundaries, and proof/stop conditions.

Rules:
- If answers are vague, incomplete, risky, or contradictory, set
  sufficient=false and ask 1 to 4 sharper follow-up questions.
- If an answer is unknown but low-risk, record it as an assumption to confirm.
- If an answer affects money, compliance, outreach, external side effects, or
  reputation, do not assume it; require a follow-up or owner approval.
- Only set sufficient=true when every dimension is actually covered.

Output ONLY a JSON object:
{"sufficient": true|false, "follow_up_questions": ["..."],
 "assumptions": ["..."], "evidence": "one-paragraph justification"}
"""

_CONTRACT_SYNTHESIS_PROMPT = """\
You are Hermes launch-contract synthesizer. Turn the owner's rough goal and
their launch-intake answers into a COMPLETE board operating contract as JSON.

The contract must be concrete and domain-specific (no generic placeholders). It
must include every one of these top-level keys:

- "objective": {"statement", "success": [>=4 specific measurable items],
  "failure": [>=4 specific items], "constraints": [specific owner boundaries]}
- "runtime": {"mode": "company", "dispatcher": {"profile"},
  "profiles": {"ceo","optimizer","worker"}, "require_provider_policy": true,
  "require_worker_envelopes": true, "provider_policy": {...},
  "worker_envelopes": {<role>: {"capabilities","toolsets",
  "allowed_side_effects","required_proof"}, ...with >=3 distinct named roles}}
- "workflow": {"id","goal_id","require_semantics": true,
  "workstreams": [...], "stages": [>=5 stages, each with domain-specific
  "rank" (integer order, 1=first), "key", "actions" and "exit_criteria"
  carrying "evidence_required". Rank stages so the owner can read the A-to-Z
  pipeline at a glance. Any stage
  "triggers" must be TYPED objects (see the trigger grammar below). A
  CONVERSATIONAL stage -- one that consumes an inbound trigger AND either holds
  "substates" or emits an external side effect (it waits on an outside party,
  e.g. a reply/approval/seller back-and-forth) -- MUST also declare a "timer"
  trigger (follow-up cadence) AND >=2 exit_criteria outcomes (at least one
  success transition and one kill/recycle/give-up transition), so it can tell
  a win from a dead end. A pure approval-wait stage is conversational too: give
  it an approved->next transition and a rejected->recycle transition.]}
- "entities": [{"key","type","states","terminal_states"}]
- "event_loops": [{"entity","triggers","terminal_states","stop_conditions"}]
  where every entry in "triggers" (and in any stage "triggers") is a TYPED
  trigger object -- DECLARE the trigger, do not leave it as free text:
    * {"kind":"timer","detail":"<why>","cadence_hours":<int>}
    * {"kind":"inbound","detail":"<why>","channel":"<channel>"}
    * {"kind":"state_change","detail":"<why>","from_state":"<s>","to_state":"<s>"}
    * {"kind":"metric","detail":"<why>","metric":"<name>","comparator":"<=|>=|<|>","threshold":<num>}
    * {"kind":"manual","detail":"<why>"}
  "kind" MUST be one of: timer, inbound, state_change, metric, manual. Put the
  human-readable description in "detail"; nothing parses "detail", so the
  reactive runtime reads only the typed "kind" + its structured params. A
  conversational loop (an inbound trigger) MUST also declare a timer trigger
  (follow-up cadence) and >=2 terminal_states/stop_conditions.
- "approval_gates": [{"key","required_before"}]
- "proof_requirements": [specific structured proof artifacts]
- Every stage "action" SHOULD declare a TYPED "side_effect_class" -- one of:
  none, internal, external_reversible, external_irreversible, financial.
  Read-only/no-mutation actions are "none"; internal-state writes are
  "internal"; anything that touches the outside world (sends a message,
  posts, books, pays) is an external_*/financial class. Nothing parses a
  free-text class; the runtime + invariants read only this closed vocabulary.
- "side_effect_policy": {"allowed","forbidden","approval_required","owner_boundaries"}
  MUST itself reference these typed classes: list every "none"/"internal"
  class you used under "allowed", and EVERY external_*/financial class you
  used under "approval_required" or "forbidden". An external side effect that
  is neither approval-gated nor forbidden is rejected (it would dispatch
  autonomously, ungated).
- "escalation_paths": [{"condition","to"}]
- "tunables": {<knob>: {"default", "range":[min,max] OR "allowed":[...],
  "controls": "what this dial changes"}, ...>=3 domain-specific knobs the
  optimizer may safely auto-adjust within bounds (e.g. follow-up cadence, nudge
  counts, fit/score thresholds, budget pacing, experiment sample sizes). Every
  knob MUST declare a range or an allowed set. Do NOT expose policy/approval
  boundaries as knobs.}
- "owner_summary": {"summary","pending_confirmation": true}
- "needed_capability_types": [ ... ]  OPTIONAL but STRONGLY preferred. A flat
  list of ABSTRACT capability verbs this goal needs Hermes to be able to do,
  expressed as "<read|write>:<thing>" strings (e.g. "read:subscription_revenue",
  "read:install_attribution", "read:product_analytics", "write:ad_spend",
  "write:store_listing", "write:lifecycle_message", "write:push_message",
  "read:support_inbox", "write:support_reply"). Describe the CAPABILITY, never a
  specific vendor/tool -- Hermes maps verbs to concrete integrations downstream.
  Emit only the verbs the owner's goal actually requires. Omit the key entirely
  if the goal needs no external systems.

Ground every field in the owner's actual answers. Name the real systems,
channels, roles, metrics, and stop conditions the owner described. Keep all
external side effects owner-approval-gated unless the owner explicitly allowed
them.

Output ONLY the JSON contract object, nothing else.
"""

# Appended to the synthesis system prompt on a repair pass. The control plane's
# structural invariant checker rejected the prior attempt; we hand the exact
# error strings back so the synthesizer fixes precisely those defects instead of
# re-rolling blind. This is what turns a single silent degrade into a bounded
# self-repair loop.
_CONTRACT_REPAIR_PROMPT = """\

REPAIR PASS. Your previous contract was rejected by the structural grammar
checker for these specific hard invariant violations:

{errors}

Return a corrected COMPLETE board operating contract as JSON that fixes EXACTLY
these problems. Keep everything that was already valid; do not introduce new
violations. In particular remember:
- every tunable knob MUST declare a "range" or an "allowed" set;
- any event loop / watched entity MUST declare terminal_states or stop_conditions;
- a conversational LOOP (one with an inbound trigger) MUST also declare a timer
  trigger and >=2 terminal/stop outcomes;
- a conversational STAGE (an inbound trigger PLUS substates or an external
  side_effect_class -- this includes any owner-approval-wait or reply-wait
  stage) MUST declare a follow-up "timer" trigger AND >=2 "exit_criteria"
  entries: at least one success transition and one kill/recycle/give-up
  transition. To fix "fewer than 2 exit outcomes", ADD a second exit_criteria
  (e.g. {{"transition":"<recycle_or_dead_stage>","evidence_required":[...]}});
- every external_*/financial side_effect_class you use on any action MUST be
  declared in side_effect_policy.approval_required or .forbidden (and an
  irreversible/financial action must also be named in an approval_gate or that
  policy) -- an undeclared/ungated external side effect is rejected;
- triggers MUST be typed objects whose "kind" is one of:
  timer, inbound, state_change, metric, manual.

Output ONLY the corrected JSON contract object, nothing else.
"""


def _format_repair_errors(errors: list[str]) -> str:
    return "\n".join(f"- {e}" for e in errors if str(e).strip())


_CEO_STEERING_PROMPT = """\
You are the Hermes CEO: the top-level reasoning for a durable agentic workflow
("board"). You are talking directly to the board's OWNER in an ongoing
conversation. Your job depends on the conversation mode:

- mode "launch_buildout": the board is mid-intake or freshly synthesized and
  not yet strong. Walk the owner through the coverage gaps, ask sharpening
  questions, and converge on a STRONGER initial contract.
- mode "runtime_evolution": the board is live. Understand the change the owner
  wants (or explain a change a sensor/optimizer already proposed), answer
  questions, and refine it.

You are given (as JSON): the current normalized "contract" (or null pre-launch),
its "contract_version", a deterministic "coverage" report listing which of the
six rubric dimensions are still weak ("gaps"), the "open_amendments" already in
flight, recent "signals", any "external_research" you previously asked for, and
the prior "conversation" turns. Ground every statement in those inputs.

CRITICAL SAFETY RAIL: you CANNOT change the live contract. The ONLY way to make
a structural change is to emit an "amendment" proposal, which the owner must
still explicitly approve -> validate -> mint through the P5 amendment loop. So:
- When the conversation has NOT yet agreed on a concrete structural change,
  just converse: ask questions, explain, clarify. Set "amendment" to null.
- ONLY when a concrete structural change is genuinely agreed, emit an
  "amendment" object carrying EITHER a full "proposed_contract" (a complete
  board operating contract JSON) OR a "diff" (a partial patch deep-merged over
  the current contract), plus a one-sentence "rationale" and, if the change
  needs owner-supplied values (API keys, spend caps), a "required_inputs" list
  of {key,label,type,required,inject_path} specs. The amendment is held to the
  full launch bar (structural invariants + behavioural simulation), so keep it
  valid: every tunable knob declares a range/allowed set; every watched
  entity/loop declares terminal_states or stop_conditions; triggers are typed
  objects (kind in timer|inbound|state_change|metric|manual); external side
  effects stay owner-approval-gated.
- If you need external facts before you can answer well, set "research_query" to
  a focused web-search query and you will be re-invoked with the results.

Output ONLY a JSON object:
{"reply": "<message to the owner>",
 "amendment": null | {"rationale": "...", "proposed_contract": {...} | null,
                      "diff": {...} | null, "required_inputs": [...]},
 "research_query": "" | "<focused query>"}
"""


def _coerce_ceo_proposal(value: Any) -> Optional[CeoAmendmentProposal]:
    if not isinstance(value, dict):
        return None
    rationale = str(value.get("rationale") or "").strip()
    proposed = value.get("proposed_contract")
    diff = value.get("diff")
    proposed = proposed if isinstance(proposed, dict) and proposed else None
    diff = diff if isinstance(diff, dict) and diff else None
    if proposed is None and diff is None:
        # No structural payload -> not an actionable proposal.
        return None
    required = value.get("required_inputs")
    required_list = list(required) if isinstance(required, list) else []
    if not rationale:
        rationale = "CEO-proposed structural change"
    return CeoAmendmentProposal(
        rationale=rationale,
        proposed_contract=proposed,
        diff=diff,
        required_inputs=required_list,
    )


def run_ceo_turn(
    *,
    mode: str,
    owner_message: str,
    contract: Optional[dict[str, Any]],
    contract_version: int,
    coverage: Optional[dict[str, Any]] = None,
    open_amendments: Optional[list[Any]] = None,
    signals: Optional[list[Any]] = None,
    conversation: Optional[list[Any]] = None,
    external_research: Optional[Iterable[Any]] = None,
    timeout: Optional[int] = None,
) -> CeoTurnResult:
    """Run one CEO conversational turn against the auxiliary model.

    Reuses the SAME aux-call path as launch synthesis (``_call_model`` resolves
    the ``kanban_launch_intake`` slot, degrades gracefully when unconfigured).
    Returns a parsed :class:`CeoTurnResult`. When the model emits an
    ``amendment`` object the caller routes it through ``propose_contract_amendment``
    (origin ``ceo``); the CEO never mutates the live contract.
    """
    payload = {
        "mode": str(mode or "").strip() or "runtime_evolution",
        "owner_message": str(owner_message or "").strip(),
        "contract": contract,
        "contract_version": int(contract_version),
        "coverage": coverage or {},
        "open_amendments": list(open_amendments or []),
        "signals": list(signals or []),
        "conversation": list(conversation or []),
        "external_research": list(external_research or []),
    }
    raw, degraded = _call_model(
        _CEO_STEERING_PROMPT,
        payload,
        timeout=timeout,
        max_tokens=HERMES_LAUNCH_INTAKE_MAX_TOKENS,
    )
    if degraded:
        return CeoTurnResult(ok=False, degraded=True, reason="auxiliary unavailable")
    parsed = _extract_json(raw or "")
    if not isinstance(parsed, dict):
        return CeoTurnResult(ok=False, degraded=False, reason="unparseable response")
    reply = str(parsed.get("reply") or "").strip()
    proposal = _coerce_ceo_proposal(parsed.get("amendment"))
    research_query = str(parsed.get("research_query") or "").strip()
    if not reply and proposal is None and not research_query:
        return CeoTurnResult(ok=False, degraded=False, reason="empty response")
    return CeoTurnResult(
        ok=True,
        degraded=False,
        reply=reply,
        proposal=proposal,
        research_query=research_query,
    )


_PRE_INTERVIEW_RESEARCH_PROMPT = """\
You are Hermes pre-interview research for a durable agentic workflow. Before any
owner questions are asked, you do quiet background research on the owner's rough
goal so the intake interview can be shorter and sharper.

You are GIVEN a list of REAL web search results under "search_results" (each has
a "query", and "results" with "title", "url", and "snippet"). Synthesize a
grounded research brief using ONLY the information in those provided results.

Identify the 2 to 4 highest-value things to learn about this domain BEFORE
talking to the owner -- including a typical workflow stage breakdown (ranked
steps from first signal to terminal outcome) and standard integrations.
For each, write a concise factual
summary grounded in the provided search results, and cite the concrete
real-world systems, channels, data sources, compliance regimes, standard
metrics, or stop conditions that the results mention by name.

Rules:
- Synthesize ONLY from the PROVIDED search results. Do NOT rely on memory for
  facts that are not supported by the provided results.
- NEVER invent, guess, fabricate, or modify URLs. Every URL you place in
  "sources" MUST be copied verbatim from a provided result's "url" field. If a
  finding is not backed by a provided result, leave its "sources" empty.
- Do not invent owner-specific facts (their exact numbers, accounts, or names).
  Capture domain-general knowledge the owner would otherwise have to explain.
- Prefer specifics: name the typical tools/channels/regulators/metrics that the
  provided results actually mention.

Output ONLY a JSON object:
{"research": [{"query": "...", "summary": "...", "sources": ["<url copied verbatim from search_results>"]}]}
"""

_PRE_INTERVIEW_RESEARCH_PROMPT_UNGROUNDED = """\
You are Hermes pre-interview research for a durable agentic workflow. Before any
owner questions are asked, you do quiet background research on the owner's rough
goal so the intake interview can be shorter and sharper.

NOTE: no live web search results are available right now, so this is a
best-effort, UNGROUNDED brief drawn from general domain knowledge.

Identify the 2 to 4 highest-value things to learn about this domain BEFORE
talking to the owner -- including a typical workflow stage breakdown (ranked
steps from first signal to terminal outcome) and standard integrations.
For each, write a concise factual
summary of what is generally true for this kind of work, and list the concrete
real-world systems, channels, data sources, compliance regimes, standard
metrics, or stop conditions that typically apply.

Rules:
- Do not invent owner-specific facts (their exact numbers, accounts, or names).
  Capture domain-general knowledge the owner would otherwise have to explain.
- Prefer specifics: name the typical tools/channels/regulators/metrics by name.
- "sources" should be plausible canonical reference URLs or named authorities;
  never fabricate fake-looking tracking URLs.

Output ONLY a JSON object:
{"research": [{"query": "...", "summary": "...", "sources": ["..."]}]}
"""


# ---------------------------------------------------------------------------
# Client plumbing
# ---------------------------------------------------------------------------


def _task_config() -> dict[str, Any]:
    try:
        from agent.auxiliary_client import _get_auxiliary_task_config
    except Exception:  # pragma: no cover - import smoke
        return {}
    try:
        cfg = _get_auxiliary_task_config(AUX_TASK)
    except Exception:  # pragma: no cover - defensive
        return {}
    return cfg if isinstance(cfg, dict) else {}


def aux_configured() -> bool:
    """True only when the ``kanban_launch_intake`` slot is explicitly set.

    A blank slot (provider ``auto`` / empty model, as in the default config and
    in the isolated test HERMES_HOME) is treated as *not configured*, so the
    server never attempts an auxiliary or network call and the caller degrades
    to the deterministic drafter. This is the key guard that keeps unit tests
    offline without per-test mocking.
    """
    cfg = _task_config()
    if str(cfg.get("model") or "").strip():
        return True
    if str(cfg.get("base_url") or "").strip():
        return True
    provider = str(cfg.get("provider") or "").strip().lower()
    if provider and provider != "auto":
        return True
    return False


def _client():
    try:
        from agent.auxiliary_client import (
            get_auxiliary_extra_body,
            get_text_auxiliary_client,
        )
    except Exception as exc:  # pragma: no cover - import smoke
        logger.debug("launch_intake: auxiliary client import failed: %s", exc)
        return None, None, None
    try:
        client, model = get_text_auxiliary_client(AUX_TASK)
    except Exception as exc:
        logger.debug("launch_intake: get_text_auxiliary_client failed: %s", exc)
        return None, None, None
    try:
        extra_body = get_auxiliary_extra_body() or None
    except Exception:  # pragma: no cover - defensive
        extra_body = None
    return client, model, extra_body


def _extract_json(raw: str) -> Optional[Any]:
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    try:
        return json.loads(stripped[first : last + 1])
    except (ValueError, json.JSONDecodeError):
        return None


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _call_model(
    system_prompt: str,
    user_payload: dict[str, Any],
    *,
    timeout: Optional[int],
    max_tokens: int,
    temperature: float = 0.2,
) -> tuple[Optional[str], bool]:
    """Return (raw_text, degraded). degraded=True means fall back."""
    if not aux_configured():
        return None, True
    client, model, extra_body = _client()
    if client is None or not model:
        return None, True
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout or 180,
            extra_body=extra_body,
        )
    except Exception as exc:
        logger.info("launch_intake: API call failed (%s) — degrading", exc)
        return None, True
    try:
        return (resp.choices[0].message.content or "").strip(), False
    except Exception:
        return "", False


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_question_generation(
    rough_goal: str,
    *,
    context: str = "",
    external_research: Optional[Iterable[Any]] = None,
    timeout: Optional[int] = None,
) -> QuestionGenerationResult:
    raw, degraded = _call_model(
        _QUESTION_GENERATION_PROMPT,
        {
            "rough_goal": str(rough_goal or "").strip(),
            "context": str(context or "").strip(),
            "external_research": list(external_research or []),
        },
        timeout=timeout,
        max_tokens=2000,
    )
    if degraded:
        return QuestionGenerationResult(ok=False, degraded=True, reason="auxiliary unavailable")
    parsed = _extract_json(raw or "")
    if not isinstance(parsed, dict):
        return QuestionGenerationResult(ok=False, degraded=False, reason="unparseable response")
    questions = _str_list(parsed.get("questions"))
    if not questions:
        return QuestionGenerationResult(ok=False, degraded=False, reason="no questions returned")
    return QuestionGenerationResult(
        ok=True,
        degraded=False,
        questions=questions[:6],
        assumptions=_str_list(parsed.get("assumptions")),
    )


def run_answer_assessment(
    rough_goal: str,
    questions: list[str],
    answers: dict[str, Any],
    *,
    external_research: Optional[Iterable[Any]] = None,
    coverage: Optional[dict[str, Any]] = None,
    timeout: Optional[int] = None,
) -> AnswerAssessmentResult:
    raw, degraded = _call_model(
        _ANSWER_ASSESSMENT_PROMPT,
        {
            "rough_goal": str(rough_goal or "").strip(),
            "questions": _str_list(questions),
            "answers": answers,
            "external_research": list(external_research or []),
            "coverage_report": coverage or {},
        },
        timeout=timeout,
        max_tokens=2500,
    )
    if degraded:
        return AnswerAssessmentResult(ok=False, degraded=True, reason="auxiliary unavailable")
    parsed = _extract_json(raw or "")
    if not isinstance(parsed, dict):
        return AnswerAssessmentResult(ok=False, degraded=False, reason="unparseable response")
    sufficient = bool(parsed.get("sufficient"))
    return AnswerAssessmentResult(
        ok=True,
        degraded=False,
        sufficient=sufficient,
        follow_up_questions=_str_list(parsed.get("follow_up_questions"))[:4],
        assumptions=_str_list(parsed.get("assumptions")),
        evidence=str(parsed.get("evidence") or "").strip(),
    )


def run_contract_synthesis(
    rough_goal: str,
    answers: dict[str, Any],
    *,
    external_research: Optional[Iterable[Any]] = None,
    coverage: Optional[dict[str, Any]] = None,
    profile: Optional[str] = None,
    timeout: Optional[int] = None,
    repair_feedback: Optional[list[str]] = None,
) -> ContractSynthesisResult:
    """Draft a board operating contract from the owner's intake answers.

    ``repair_feedback`` carries the structural-invariant error strings from a
    prior synthesis attempt. When present, the synthesis prompt is augmented
    with a repair instruction that lists those exact errors and asks for a
    corrected contract, and the same list is echoed into the user payload so the
    model sees the failures alongside the inputs that produced them. This is the
    feedback channel the caller's bounded self-repair loop uses to fix a
    rejected contract instead of silently degrading.
    """
    feedback = _str_list(repair_feedback)
    system_prompt = _CONTRACT_SYNTHESIS_PROMPT
    if feedback:
        system_prompt = _CONTRACT_SYNTHESIS_PROMPT + _CONTRACT_REPAIR_PROMPT.format(
            errors=_format_repair_errors(feedback)
        )
    raw, degraded = _call_model(
        system_prompt,
        {
            "rough_goal": str(rough_goal or "").strip(),
            "answers": answers,
            "external_research": list(external_research or []),
            "coverage_report": coverage or {},
            "profile": str(profile or "").strip(),
            "repair_feedback": feedback,
        },
        timeout=timeout,
        max_tokens=HERMES_LAUNCH_INTAKE_MAX_TOKENS,
    )
    if degraded:
        return ContractSynthesisResult(ok=False, degraded=True, reason="auxiliary unavailable")
    parsed = _extract_json(raw or "")
    if not isinstance(parsed, dict):
        return ContractSynthesisResult(ok=False, degraded=False, reason="unparseable response")
    if isinstance(parsed.get("operating_contract"), dict):
        parsed = parsed["operating_contract"]
    if not (parsed.get("objective") and parsed.get("workflow")):
        return ContractSynthesisResult(
            ok=False, degraded=False, reason="synthesized contract missing objective/workflow"
        )
    return ContractSynthesisResult(ok=True, degraded=False, contract=parsed)


def _coerce_research_items(
    parsed: Any,
    *,
    allowed_urls: Optional[set[str]] = None,
    fallback_sources: Optional[list[str]] = None,
) -> list[ExternalResearchItem]:
    import time as _time

    rows = parsed.get("research") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        return []
    now = int(_time.time())
    items: list[ExternalResearchItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        query = str(row.get("query") or "").strip()
        summary = str(row.get("summary") or "").strip()
        if not summary:
            continue
        sources = _str_list(row.get("sources"))
        if allowed_urls is not None:
            # Grounded mode: keep ONLY URLs that actually came back from web
            # search. This is the hard guarantee that the model cannot smuggle
            # in invented/confabulated URLs -- anything not in the real result
            # set is dropped. If filtering removes everything, attach the top
            # real sources so the finding stays citeable.
            sources = [s for s in sources if s in allowed_urls]
            if not sources and fallback_sources:
                sources = list(fallback_sources)[:3]
        items.append(
            ExternalResearchItem(
                query=query,
                summary=summary,
                sources=sources,
                fetched_at=now,
            )
        )
    return items[:4]


# ---------------------------------------------------------------------------
# Grounded web research plumbing
# ---------------------------------------------------------------------------


def _web_search_available() -> bool:
    """True when the Hermes ``web`` toolset has a usable search backend.

    Probes ``tools.web_tools.check_web_api_key`` in-process. Returns False on
    any import/probe failure so research degrades gracefully (and so the unit
    tests, which configure no web backend, take the ungrounded path).
    """
    try:
        from tools.web_tools import check_web_api_key
    except Exception:  # pragma: no cover - import smoke
        return False
    try:
        return bool(check_web_api_key())
    except Exception:  # pragma: no cover - defensive
        return False


def _web_search(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Run a single real web search via the Hermes ``web`` toolset, in-process.

    Calls ``tools.web_tools.web_search_tool`` (the same backend the chat model's
    ``web_search`` tool uses) and normalizes the JSON envelope into a list of
    ``{"title", "url", "snippet"}`` dicts. Returns ``[]`` on any failure so the
    caller can decide whether to fall back.
    """
    query = str(query or "").strip()
    if not query:
        return []
    try:
        from tools.web_tools import web_search_tool
    except Exception:  # pragma: no cover - import smoke
        return []
    try:
        raw = web_search_tool(query, limit=limit)
    except Exception as exc:
        logger.info("launch_intake: web_search failed (%s) — degrading", exc)
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    if data.get("success") is False:
        return []
    payload = data.get("data")
    rows = payload.get("web") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        out.append(
            {
                "title": str(row.get("title") or "").strip(),
                "url": url,
                "snippet": str(row.get("description") or row.get("snippet") or "").strip(),
            }
        )
    return out


def _derive_search_queries(
    rough_goal: str, context: str = "", *, max_queries: int = 3
) -> list[str]:
    """Derive focused web-search queries from the rough goal (+ any context).

    Kept deterministic (no model round-trip) so it never adds latency or a
    network call before the search step: the goal itself is the primary query,
    and a goal+context query sharpens it when context is present.
    """
    goal = str(rough_goal or "").strip()
    ctx = str(context or "").strip()
    queries: list[str] = []
    if goal:
        queries.append(goal)
    if ctx:
        combined = f"{goal} {ctx}".strip()[:256]
        if combined:
            queries.append(combined)
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        key = q.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(q)
    return out[:max_queries]


def _run_grounded_research(
    rough_goal: str,
    context: str,
    *,
    timeout: Optional[int],
) -> Optional[PreInterviewResearchResult]:
    """Attempt REAL, web-grounded research.

    Returns ``None`` when grounding could not be completed (no queries, no real
    search hits, or the model produced no usable items) so the caller can fall
    back to the ungrounded path. Returns a populated, ``grounded=True`` result
    on success, or a ``degraded=True`` result if the aux model call itself
    fails after real sources were gathered.
    """
    queries = _derive_search_queries(rough_goal, context)
    if not queries:
        return None

    search_results: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for query in queries:
        hits = _web_search(query, limit=5)[:5]
        if not hits:
            continue
        search_results.append({"query": query, "results": hits})
        for hit in hits:
            url = hit.get("url")
            if url and url not in seen_urls:
                seen_urls.add(url)
                sources.append(hit)

    if not sources:
        # Web toolset reachable but returned nothing real -- prefer the
        # ungrounded fallback over emitting an empty grounded brief.
        return None

    raw, degraded = _call_model(
        _PRE_INTERVIEW_RESEARCH_PROMPT,
        {
            "rough_goal": rough_goal,
            "context": context,
            "search_results": search_results,
        },
        timeout=timeout,
        max_tokens=3000,
    )
    if degraded:
        return PreInterviewResearchResult(
            ok=False, degraded=True, grounded=False, reason="auxiliary unavailable"
        )
    parsed = _extract_json(raw or "")
    items = _coerce_research_items(
        parsed,
        allowed_urls=seen_urls,
        fallback_sources=[s["url"] for s in sources],
    )
    if not items:
        return None
    return PreInterviewResearchResult(
        ok=True,
        degraded=False,
        grounded=True,
        items=items,
        sources=[
            {"title": s.get("title", ""), "url": s["url"], "snippet": s.get("snippet", "")}
            for s in sources
        ],
    )


def run_pre_interview_research(
    rough_goal: str,
    *,
    context: str = "",
    timeout: Optional[int] = None,
) -> PreInterviewResearchResult:
    """Quietly research the domain before any owner questions are asked.

    When BOTH the Hermes ``web`` toolset has a usable backend AND the auxiliary
    slot is configured, research is grounded in REAL web search results: focused
    queries are derived from the rough goal, the ``web`` toolset is called
    in-process to fetch actual results, and those results are handed to the aux
    model to synthesize a brief whose ``sources`` are real, citeable URLs (the
    model cannot inject invented URLs -- they are filtered against the fetched
    set). The fetched source list is persisted on the result as evidence, and
    ``grounded=True``.

    Degrades gracefully: if the web toolset is unavailable/unconfigured, or the
    aux slot is blank, or grounding yields no real hits, it falls back to the
    prior best-effort behavior (aux model from prior knowledge) marked
    ``grounded=False``. Never performs a network call when the aux slot is
    blank, which keeps unit tests offline.
    """
    goal = str(rough_goal or "").strip()
    ctx = str(context or "").strip()

    if _web_search_available() and aux_configured():
        grounded = _run_grounded_research(goal, ctx, timeout=timeout)
        if grounded is not None:
            return grounded
        # grounding could not be completed -> fall through to ungrounded path

    raw, degraded = _call_model(
        _PRE_INTERVIEW_RESEARCH_PROMPT_UNGROUNDED,
        {
            "rough_goal": goal,
            "context": ctx,
        },
        timeout=timeout,
        max_tokens=3000,
    )
    if degraded:
        return PreInterviewResearchResult(
            ok=False, degraded=True, grounded=False, reason="auxiliary unavailable"
        )
    parsed = _extract_json(raw or "")
    items = _coerce_research_items(parsed)
    if not items:
        return PreInterviewResearchResult(
            ok=False, degraded=False, grounded=False, reason="no research returned"
        )
    return PreInterviewResearchResult(ok=True, degraded=False, grounded=False, items=items)
