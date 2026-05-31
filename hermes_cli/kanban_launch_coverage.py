"""Deterministic launch-intake coverage rubric (Phase 0 foundation).

This module scores how completely a launch-intake conversation (or a drafted
board operating contract) covers the dimensions Hermes needs before it can
safely synthesize and launch an agentic workflow:

    1. outcome_signals     - measurable success / failure conditions
    2. subject_scope       - the people / items / accounts the work is about
    3. allowed_context     - the systems, channels, and tools that are allowed
    4. workflow_path       - the real-world path from first signal to terminal
    5. workflow_stages     - named stages, ranked steps, conversation points
    6. integration_points  - GitHub, CRM, ad platforms, messaging APIs, etc.
    7. approval_boundaries - autonomous vs. owner-approval-gated actions
    8. proof_and_stops     - proof, status updates, and stop conditions

Design constraints (see Phase 0 of the intake->contract upgrade):

* The Phase-1 checks are *keyword-agnostic structural* checks. They look for the
  shape of a good answer -- numbers / thresholds, named systems (capitalized
  brands, acronyms, URLs), sequence/stage verbs, deontic (permission) markers,
  and proof/stop markers -- rather than matching a fixed list of domain words
  like "wholesale" or "insurance". There is intentionally **no LLM judge** here;
  this is the cheap, deterministic gate that runs before any auxiliary model.

* ``evaluate_launch_intake_coverage`` scores raw intake answers (the corpus the
  owner produced) so the synthesis path can be gated on real coverage rather
  than the old "5 fields and 300 chars" heuristic.

* ``score_contract_against_rubric`` scores a *drafted contract* by mapping its
  structured fields onto the same six dimensions. This is the eval-harness
  scorer used to prove that the generic universal drafter is weak (< 0.5) while
  a hand-authored gold contract is strong (>= 0.85).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

DIMENSIONS: tuple[str, ...] = (
    "outcome_signals",
    "subject_scope",
    "allowed_context",
    "workflow_path",
    "workflow_stages",
    "integration_points",
    "approval_boundaries",
    "proof_and_stops",
)

_DIMENSION_HINTS: dict[str, str] = {
    "outcome_signals": "Measurable success and failure signals (numbers, thresholds, time horizons).",
    "subject_scope": "Who or what the work is about (customers, deals, accounts, products).",
    "allowed_context": "Systems, channels, and tools Hermes may use (and any hard bans).",
    "workflow_path": "Real-world path from first signal through done, paused, or disqualified.",
    "workflow_stages": "Named stages with order: lead, qualify, negotiate, close, etc.",
    "integration_points": "Concrete integrations: GitHub, CRM, SMS, ad platforms, analytics.",
    "approval_boundaries": "What runs autonomously vs what needs owner approval before acting.",
    "proof_and_stops": "Proof artifacts, status updates, and conditions that stop the board.",
}


def intake_answer_template_json() -> dict[str, str]:
    """Six-dimension intake answer skeleton for CLI ``--intake-template``."""
    return {name: _DIMENSION_HINTS.get(name, "") for name in DIMENSIONS}

# Default gate: every dimension must be at least ``partial`` and the mean score
# must clear this threshold. Tuned so rich, structured intake passes while
# generic blather and the placeholder universal contract do not.
DEFAULT_PASS_THRESHOLD = 0.6

_STATUS_SCORE = {"pass": 1.0, "partial": 0.5, "fail": 0.0}


@dataclass
class DimensionResult:
    """Score for a single coverage dimension."""

    dimension: str
    status: str  # "pass" | "partial" | "fail"
    score: float
    evidence: list[str]
    min_chars: int
    markers: int = 0
    chars: int = 0
    specificity: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "status": self.status,
            "score": self.score,
            "evidence": self.evidence,
            "min_chars": self.min_chars,
            "markers": self.markers,
            "chars": self.chars,
            "specificity": self.specificity,
        }


@dataclass
class CoverageReport:
    """Aggregate coverage across all six dimensions."""

    dimensions: list[DimensionResult]
    score: float
    passed: bool
    gaps: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    threshold: float = DEFAULT_PASS_THRESHOLD

    def dimension(self, name: str) -> Optional[DimensionResult]:
        for result in self.dimensions:
            if result.dimension == name:
                return result
        return None

    @property
    def passing_dimensions(self) -> list[str]:
        return [d.dimension for d in self.dimensions if d.status == "pass"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "passed": self.passed,
            "threshold": self.threshold,
            "gaps": list(self.gaps),
            "assumptions": list(self.assumptions),
            "dimensions": [d.as_dict() for d in self.dimensions],
        }


# ---------------------------------------------------------------------------
# Structural marker detectors (keyword-agnostic)
# ---------------------------------------------------------------------------

# Numbers, currency, percentages, counts and time windows -> measurable signal.
_METRIC_RE = re.compile(
    r"""(
        \$\s?\d[\d,]*(?:\.\d+)?          # currency
        | \b\d[\d,]*(?:\.\d+)?\s?%       # percentages
        | \b\d[\d,]*(?:\.\d+)?\s*
            (?:per\s+\w+|/\s*\w+|x\b|times|days?|weeks?|months?|years?
             |hours?|hrs?|minutes?|mins?|seconds?|secs?|k\b|m\b)   # rates / windows
        | \b\d{2,}\b                     # bare multi-digit counts
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Named systems / channels / tools: acronyms, CamelCase brands, URLs/domains,
# capitalized proper nouns appearing mid-sentence (not just sentence starts).
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,6}(?:\d+)?\b")
_CAMEL_RE = re.compile(r"\b[A-Z][a-z]+[A-Z][A-Za-z]*\b")
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+|\b[a-z0-9-]+\.(?:com|io|org|net|ai|app|gov)\b", re.IGNORECASE)
_PROPER_MIDSENTENCE_RE = re.compile(r"(?<=[a-z,;:]\s)([A-Z][a-zA-Z]{2,})")

# Sequence / stage verbs: arrows and ordered connectors are pure structure.
_ARROW_RE = re.compile(r"->|=>|\u2192|\u27a4")
_SEQUENCE_RE = re.compile(
    r"\b(then|next|after|once|before|finally|first|second|third|"
    r"start(?:s|ing)?|begin(?:s|ning)?|move(?:s)?\s+to|advance(?:s)?|"
    r"transition(?:s)?|stage|step|until|when\b)\b",
    re.IGNORECASE,
)

# Deontic (permission / obligation) markers -> approval boundaries.
_DEONTIC_RE = re.compile(
    r"\b(must|may\s+not|cannot|can'?t|only\s+after|require[sd]?|requires|"
    r"approv\w+|permission|allowed|forbidden|prohibit\w+|not\s+allowed|"
    r"without\s+\w+|gate[sd]?|gated|explicit\w*|authoriz\w+|never|"
    r"do\s+not|don'?t|owner-?approv\w*)\b",
    re.IGNORECASE,
)

# Proof / evidence markers and stop / terminal markers.
_PROOF_RE = re.compile(
    r"\b(proof|evidence|log(?:s|ged|ging)?|record(?:s|ed)?|receipt|"
    r"screenshot|audit\w*|verif\w+|summary|report|citation|artifact|"
    r"trace|status\s+update)\b",
    re.IGNORECASE,
)
_STOP_RE = re.compile(
    r"\b(stop|halt|escalat\w+|terminal|disqualif\w+|abort|paus\w+|"
    r"closed?|done|complete[sd]?|won|lost|reject\w+|decline[sd]?|"
    r"give\s+up|cancel\w*)\b",
    re.IGNORECASE,
)


def _dedup(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _findall(pattern: re.Pattern[str], text: str) -> list[str]:
    out: list[str] = []
    for match in pattern.finditer(text):
        out.append(match.group(0))
    return out


def _metric_markers(text: str) -> list[str]:
    return _dedup(_findall(_METRIC_RE, text))


def _named_system_markers(text: str) -> list[str]:
    found: list[str] = []
    found.extend(_findall(_URL_RE, text))
    found.extend(_findall(_CAMEL_RE, text))
    # Acronyms, but skip common English all-caps words that are not systems.
    _COMMON = {"OK", "ID", "FAQ", "CEO", "TODO", "USA", "US", "AM", "PM", "ASAP"}
    for tok in _findall(_ACRONYM_RE, text):
        if tok.upper() not in _COMMON or tok.upper() in {"CEO"}:
            found.append(tok)
    found.extend(_findall(_PROPER_MIDSENTENCE_RE, text))
    return _dedup(found)


def _sequence_markers(text: str) -> list[str]:
    return _dedup(_findall(_ARROW_RE, text) + _findall(_SEQUENCE_RE, text))


def _deontic_markers(text: str) -> list[str]:
    return _dedup(_findall(_DEONTIC_RE, text))


def _proof_markers(text: str) -> list[str]:
    return _dedup(_findall(_PROOF_RE, text) + _findall(_STOP_RE, text))


# Generic / boilerplate vocabulary. A concrete, well-specified intake or
# contract earns its score from *domain-specific* tokens (``skip_trace``,
# ``parcel_verify``, ``infobip``, ``cpas``...), not from recycled agentic
# boilerplate. We discount English function words plus the generic vocabulary
# the universal placeholder drafter leans on, so a template full of
# "owner-approved context / work item / next action" collapses to near-zero
# specificity while a real domain contract stays rich. This is *structural*
# (it measures vocabulary diversity), not domain keyword matching: no specific
# industry term is required or rewarded.
_GENERIC_TOKENS: frozenset[str] = frozenset({
    # English function / filler words
    "the", "a", "an", "of", "to", "and", "or", "is", "are", "be", "been", "as",
    "at", "by", "for", "from", "in", "into", "on", "onto", "with", "without",
    "any", "all", "this", "that", "these", "those", "it", "its", "if", "then",
    "than", "but", "not", "no", "yes", "do", "does", "did", "done", "can",
    "cannot", "may", "must", "should", "would", "could", "will", "shall",
    "before", "after", "until", "when", "while", "only", "also", "via", "per",
    "each", "every", "other", "such", "more", "most", "less", "least", "some",
    "they", "them", "their", "i", "we", "you", "he", "she", "him", "her",
    "what", "which", "who", "whom", "how", "where", "why", "up", "out", "over",
    "use", "used", "using", "make", "makes", "made", "set", "get", "got",
    "keep", "stay", "stays", "run", "runs", "running", "is", "have", "has",
    "wants", "want", "need", "needs", "just", "whatever", "etc", "e", "g",
    # Generic agentic-contract / intake boilerplate
    "owner", "owners", "owner-defined", "owner-approved", "approval",
    "approved", "approve", "approves", "context", "work", "item", "items",
    "action", "actions", "log", "logs", "summary", "summaries", "launch",
    "intake", "answer", "answers", "defined", "outcome", "outcomes",
    "achieved", "captured", "draft", "drafts", "drafting", "execute",
    "executed", "execution", "step", "steps", "track", "tracking", "tracked",
    "state", "states", "status", "response", "responses", "archive",
    "archived", "observe", "observed", "plan", "planning", "understand",
    "understanding", "closed", "close", "authorized", "authorize", "collect",
    "collected", "interpret", "interpreted", "requirements", "requirement",
    "allowed", "allow", "required", "require", "requires", "proof",
    "record", "records", "decision", "rationale", "next", "periodic",
    "stop", "stops", "condition", "conditions", "escalate", "escalation",
    "external", "side", "effect", "effects", "reach", "reached", "reaches",
    "boundary", "boundaries", "explicit", "explicitly", "interpreted",
    "agentic", "workflow", "workflows", "operating", "contract", "contracts",
    "business", "runtime", "runtimes", "goal", "goals", "objective",
    "objectives", "success", "failure", "fail", "fails", "constraint",
    "constraints", "policy", "policies", "process", "processes", "task",
    "tasks", "data", "sources", "source", "system", "systems", "channel",
    "channels", "tool", "tools", "toolset", "toolsets", "capability",
    "capabilities", "stage", "stages", "phase", "phases", "trigger",
    "triggers", "transition", "transitions", "evidence", "criteria",
    "criterion", "gate", "gates", "gated", "loop", "loops", "entity",
    "entities", "terminal", "qualified", "disqualified", "approved",
    "read", "only", "write", "writes", "spend", "money", "risk", "reputation",
    "compliance", "legal", "identity", "person", "people", "thing", "things",
    "first", "second", "third", "new", "old", "good", "bad", "create",
    "created", "produce", "produced", "produces", "provide", "provided",
})

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")


def _specific_tokens(text: str) -> list[str]:
    """Distinct domain-specific tokens (length>=3) after dropping boilerplate."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in _TOKEN_RE.findall(str(text or "")):
        tok = raw.lower()
        if len(tok) < 3 or tok in _GENERIC_TOKENS:
            continue
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


# ---------------------------------------------------------------------------
# Dimension scoring
# ---------------------------------------------------------------------------

# Per-dimension config: which detectors feed it, the minimum char budget, and
# how many distinct structural markers are needed for a "pass".
@dataclass(frozen=True)
class _DimensionSpec:
    name: str
    min_chars: int
    # Relevant structural-marker detectors for this dimension.
    detectors: tuple[str, ...]
    # Distinct domain-specific tokens needed for pass / partial.
    spec_strong: int
    spec_partial: int
    # Structural markers needed (in addition to specificity) for a pass.
    markers_for_pass: int


_DETECTOR_FUNCS = {
    "metric": _metric_markers,
    "named": _named_system_markers,
    "sequence": _sequence_markers,
    "deontic": _deontic_markers,
    "proof": _proof_markers,
}

# Calibration note: a hand-authored gold contract scores 42-94 distinct
# domain-specific tokens per dimension; the generic universal placeholder
# drafter scores only 4-17. A strong threshold of 20 (partial 10) sits well
# inside that gap, so a concrete contract passes every dimension while the
# boilerplate placeholder mostly fails. The same thresholds gate raw intake
# answers: a rich answer set clears them, a one-line "do whatever" does not.
_DIMENSION_SPECS: dict[str, _DimensionSpec] = {
    "outcome_signals": _DimensionSpec(
        "outcome_signals", min_chars=40, detectors=("metric", "named", "proof"),
        spec_strong=20, spec_partial=10, markers_for_pass=1,
    ),
    "subject_scope": _DimensionSpec(
        "subject_scope", min_chars=30, detectors=("named", "metric"),
        spec_strong=20, spec_partial=10, markers_for_pass=0,
    ),
    "allowed_context": _DimensionSpec(
        "allowed_context", min_chars=30, detectors=("named", "deontic"),
        spec_strong=20, spec_partial=10, markers_for_pass=1,
    ),
    "workflow_path": _DimensionSpec(
        "workflow_path", min_chars=60, detectors=("sequence", "named"),
        spec_strong=20, spec_partial=10, markers_for_pass=1,
    ),
    "workflow_stages": _DimensionSpec(
        "workflow_stages", min_chars=40, detectors=("sequence", "named", "metric"),
        spec_strong=15, spec_partial=8, markers_for_pass=2,
    ),
    "integration_points": _DimensionSpec(
        "integration_points", min_chars=25, detectors=("named", "deontic"),
        spec_strong=12, spec_partial=6, markers_for_pass=1,
    ),
    "approval_boundaries": _DimensionSpec(
        "approval_boundaries", min_chars=30, detectors=("deontic", "named"),
        spec_strong=20, spec_partial=10, markers_for_pass=2,
    ),
    "proof_and_stops": _DimensionSpec(
        "proof_and_stops", min_chars=30, detectors=("proof", "metric"),
        spec_strong=20, spec_partial=10, markers_for_pass=2,
    ),
}


# In "answers" mode the corpus is owner prose, so we demand real structural
# markers (numbers, named systems, deontic/proof language) for a pass --
# otherwise verbose but vacuous filler ("do the thing well, keep it simple")
# games the specificity count. In "contract" mode the corpus is already
# structured JSON whose richness lives in field counts and identifiers, so the
# per-dimension ``markers_for_pass`` floor governs instead.
_ANSWER_MARKERS_FOR_PASS = 3
_ANSWER_MARKERS_FOR_PARTIAL = 1


def _score_dimension(spec: _DimensionSpec, text: str, *, mode: str = "contract") -> DimensionResult:
    text = str(text or "")
    chars = len(text.strip())
    markers: list[str] = []
    for detector in spec.detectors:
        markers.extend(_DETECTOR_FUNCS[detector](text))
    markers = _dedup(markers)
    marker_count = len(markers)
    specificity = len(_specific_tokens(text))

    if mode == "answers":
        marker_pass_floor = _ANSWER_MARKERS_FOR_PASS
        marker_partial_floor = _ANSWER_MARKERS_FOR_PARTIAL
    else:
        marker_pass_floor = spec.markers_for_pass
        marker_partial_floor = 0

    if (
        specificity >= spec.spec_strong
        and marker_count >= marker_pass_floor
        and chars >= spec.min_chars
    ):
        status = "pass"
    elif (
        specificity >= spec.spec_partial
        and marker_count >= marker_partial_floor
        and chars >= max(15, spec.min_chars // 2)
    ):
        status = "partial"
    else:
        status = "fail"

    # Surface both the structural markers and a few specific tokens as evidence.
    evidence = _dedup(markers + _specific_tokens(text))[:12]
    return DimensionResult(
        dimension=spec.name,
        status=status,
        score=_STATUS_SCORE[status],
        evidence=evidence,
        min_chars=spec.min_chars,
        markers=marker_count,
        chars=chars,
        specificity=specificity,
    )


def _build_report(
    corpus_by_dimension: dict[str, str],
    *,
    threshold: float,
    mode: str = "contract",
    assumptions: Optional[list[str]] = None,
) -> CoverageReport:
    results: list[DimensionResult] = []
    for name in DIMENSIONS:
        spec = _DIMENSION_SPECS[name]
        results.append(_score_dimension(spec, corpus_by_dimension.get(name, ""), mode=mode))

    mean_score = sum(r.score for r in results) / len(results) if results else 0.0
    any_fail = any(r.status == "fail" for r in results)
    passed = (mean_score >= threshold) and not any_fail
    gaps = [r.dimension for r in results if r.status != "pass"]
    return CoverageReport(
        dimensions=results,
        score=mean_score,
        passed=passed,
        gaps=gaps,
        assumptions=list(assumptions or []),
        threshold=threshold,
    )


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------


def _flatten_text(value: Any) -> str:
    """Flatten an arbitrary JSON-ish value into a whitespace-joined string."""
    parts: list[str] = []

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, str):
            text = node.strip()
            if text:
                parts.append(text)
        elif isinstance(node, (int, float, bool)):
            parts.append(str(node))
        elif isinstance(node, dict):
            for key, val in node.items():
                # Keys themselves are meaningful structural signal (e.g. tool
                # names like ``outbound_sms`` or worker roles like ``land-ceo``).
                if isinstance(key, str) and key.strip():
                    parts.append(key.replace("_", " ").replace("-", " "))
                walk(val)
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item)

    walk(value)
    return " . ".join(parts)


def _answers_corpus(
    answers: Optional[dict[str, Any]],
    rough_goal: Optional[str],
    external_research: Optional[Iterable[Any]],
) -> str:
    chunks: list[str] = []
    if rough_goal:
        chunks.append(str(rough_goal))
    if isinstance(answers, dict):
        chunks.append(_flatten_text(answers))
    elif answers is not None:
        chunks.append(_flatten_text(answers))
    if external_research:
        for item in external_research:
            if isinstance(item, dict):
                chunks.append(_flatten_text({
                    "query": item.get("query"),
                    "summary": item.get("summary"),
                    "sources": item.get("sources"),
                }))
            else:
                chunks.append(_flatten_text(item))
    return "\n".join(c for c in chunks if c)


def evaluate_launch_intake_coverage(
    answers: Optional[dict[str, Any]],
    external_research: Optional[Iterable[Any]] = None,
    rough_goal: Optional[str] = None,
    *,
    threshold: float = DEFAULT_PASS_THRESHOLD,
) -> CoverageReport:
    """Score raw launch-intake answers against the six coverage dimensions.

    ``answers`` is the (already normalized) owner answer mapping. Because the
    answers are not labeled per dimension, every dimension is scored against the
    full corpus (answers + rough_goal + any external research summaries). The
    detectors differ per dimension, so a corpus that only contains, say, a vague
    one-liner fails most dimensions while a rich, structured answer set passes.
    """
    corpus = _answers_corpus(answers, rough_goal, external_research)
    by_dim = {name: corpus for name in DIMENSIONS}
    return _build_report(by_dim, threshold=threshold, mode="answers")


# ---------------------------------------------------------------------------
# Contract -> dimension extraction (eval-harness scorer)
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    return []


def _contract_root(contract: Any) -> dict[str, Any]:
    parsed = contract if isinstance(contract, dict) else {}
    if isinstance(parsed.get("operating_contract"), dict):
        return dict(parsed["operating_contract"])
    if isinstance(parsed.get("contract"), dict) and any(
        k in parsed["contract"] for k in ("objective", "runtime", "workflow")
    ):
        return dict(parsed["contract"])
    return dict(parsed)


def _contract_dimension_corpus(contract: Any) -> dict[str, str]:
    root = _contract_root(contract)
    objective = _as_dict(root.get("objective"))
    runtime = _as_dict(root.get("runtime"))
    workflow = _as_dict(root.get("workflow"))

    success = _as_list(objective.get("success"))
    failure = _as_list(objective.get("failure"))
    constraints = _as_list(objective.get("constraints"))
    statement = objective.get("statement")

    worker_envelopes = _as_dict(runtime.get("worker_envelopes"))
    channel_policy = runtime.get("channel_policy")
    tool_policy = runtime.get("tool_policy")
    provider_policy = runtime.get("provider_policy")
    reactive_entities = runtime.get("reactive_entities")

    stages = [s for s in _as_list(workflow.get("stages")) if isinstance(s, dict)]
    workstreams = workflow.get("workstreams")

    # Proof artifacts pulled from every place a contract can encode evidence.
    required_proof: list[Any] = []
    for env in worker_envelopes.values():
        if isinstance(env, dict):
            required_proof.extend(_as_list(env.get("required_proof")))
    evidence_required: list[Any] = []
    for stage in stages:
        for exit_row in _as_list(stage.get("exit_criteria")):
            if isinstance(exit_row, dict):
                evidence_required.extend(_as_list(exit_row.get("evidence_required")))

    return {
        "outcome_signals": _flatten_text([statement, success, failure]),
        "subject_scope": _flatten_text([
            statement,
            list(worker_envelopes.keys()),
            reactive_entities,
            workstreams,
            root.get("entities"),
            [s.get("key") for s in stages],
        ]),
        "allowed_context": _flatten_text([
            provider_policy,
            channel_policy,
            tool_policy,
            [env.get("toolsets") for env in worker_envelopes.values() if isinstance(env, dict)],
            root.get("side_effect_policy"),
            constraints,
        ]),
        "workflow_path": _flatten_text([
            [s.get("key") for s in stages],
            [s.get("rank") for s in stages],
            [s.get("actions") for s in stages],
            [s.get("exit_criteria") for s in stages],
            workstreams,
            root.get("event_loops"),
        ]),
        "workflow_stages": _flatten_text([
            [s.get("key") for s in stages],
            [s.get("rank") for s in stages],
            [s.get("substates") for s in stages],
            [s.get("exit_criteria") for s in stages],
        ]),
        "integration_points": _flatten_text([
            provider_policy,
            channel_policy,
            tool_policy,
            root.get("needed_capability_types"),
            [env.get("toolsets") for env in worker_envelopes.values() if isinstance(env, dict)],
        ]),
        "approval_boundaries": _flatten_text([
            root.get("approval_gates"),
            constraints,
            tool_policy,
            root.get("side_effect_policy"),
            failure,
        ]),
        "proof_and_stops": _flatten_text([
            root.get("proof_requirements"),
            required_proof,
            evidence_required,
            root.get("escalation_paths"),
            root.get("event_loops"),
            failure,
        ]),
    }


def coverage_report_for_contract(
    contract: Any,
    *,
    threshold: float = DEFAULT_PASS_THRESHOLD,
) -> CoverageReport:
    """Return the full per-dimension coverage report for a drafted contract."""
    by_dim = _contract_dimension_corpus(contract)
    return _build_report(by_dim, threshold=threshold, mode="contract")


def score_contract_against_rubric(
    contract: Any,
    fixture: Optional[dict[str, Any]] = None,
) -> float:
    """Return a 0..1 rubric score for a drafted contract.

    ``fixture`` is accepted for parity with the eval harness (it may carry the
    rough_goal / expectations for a domain) but the score is intrinsic to the
    contract's structural concreteness so the number is comparable across
    fixtures. The generic universal drafter scores low; a concrete,
    hand-authored gold contract scores high.
    """
    threshold = DEFAULT_PASS_THRESHOLD
    if isinstance(fixture, dict):
        threshold = float(fixture.get("threshold", threshold))
    return round(coverage_report_for_contract(contract, threshold=threshold).score, 4)
