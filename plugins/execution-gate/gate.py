"""
Execution Gate Plugin

Gated execution system for orchestrator profiles.

Tier 1 (Contracts): Declare intent + criteria → delegate → validate artifacts
Tier 2 (Boards): Persistent kanban work with CEO/worker hierarchy

The orchestrator declares what success looks like. Workers execute.
Results are validated against declared criteria before completion.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, List, Dict

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TaskStatus(Enum):
    IDLE = "idle"
    CONTRACTED = "contracted"
    DELEGATED = "delegated"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class FailureClass(Enum):
    RETRYABLE = "retryable"
    TERMINAL = "terminal"
    ESCALATE = "escalate"


class CriteriaVisibility(Enum):
    DISCLOSED = "disclosed"   # Worker sees full description
    HINTED = "hinted"         # Worker sees category only
    HIDDEN = "hidden"         # Worker never sees (audit)


class ValidationLayer(Enum):
    L1_STRUCTURAL = "L1_structural"   # Files exist, schema valid
    L2_SEMANTIC = "L2_semantic"       # Completeness, coherence
    L3_FUNCTIONAL = "L3_functional"   # Code runs, tests pass
    L4_AUDIT = "L4_audit"             # Hidden spot checks


class ProofType(Enum):
    EXIT_CODE = "exit_code"           # Command exit code == 0
    FILE_EXISTS = "file_exists"       # File at path exists
    CONTENT_MATCH = "content_match"   # Pattern in content
    TEST_PASS = "test_pass"           # Test results show 0 failures
    HASH_MATCH = "hash_match"         # File hash matches expected
    CUSTOM = "custom"                 # Custom validation function


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class Criterion:
    """A single pass criterion."""
    id: str
    description: str
    proof_type: ProofType
    proof_source: str                    # Which artifact key satisfies this
    layer: ValidationLayer = ValidationLayer.L1_STRUCTURAL
    visibility: CriteriaVisibility = CriteriaVisibility.DISCLOSED
    required: bool = True
    pattern: Optional[str] = None        # For content_match
    expected_hash: Optional[str] = None  # For hash_match
    
    def to_worker_view(self) -> Optional[dict]:
        """Return what worker should see based on visibility."""
        if self.visibility == CriteriaVisibility.HIDDEN:
            return None
        
        result = {"id": self.id}
        
        if self.visibility == CriteriaVisibility.DISCLOSED:
            result["description"] = self.description
            result["proof_type"] = self.proof_type.value
            result["proof_source"] = self.proof_source
        else:  # HINTED
            result["description"] = f"[{self.layer.value}] quality requirement"
        
        return result


@dataclass
class Artifact:
    """Proof artifact returned by worker."""
    id: str
    artifact_type: str                   # "file", "command_output", "test_result", etc.
    content_hash: Optional[str] = None   # SHA256 of content
    path: Optional[str] = None           # File path
    content: Optional[str] = None        # Inline content
    exit_code: Optional[int] = None      # For command output
    test_passed: Optional[int] = None    # For test results
    test_failed: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def compute_hash(self) -> str:
        """Compute hash from content or file."""
        if self.content:
            return f"sha256:{hashlib.sha256(self.content.encode()).hexdigest()}"
        elif self.path:
            p = Path(self.path).expanduser()
            if p.exists():
                return f"sha256:{hashlib.sha256(p.read_bytes()).hexdigest()}"
        return ""
    
    def verify_hash(self) -> bool:
        """Verify stored hash matches actual content."""
        if not self.content_hash:
            return True  # No hash to verify
        return self.compute_hash() == self.content_hash


@dataclass
class Contract:
    """Execution contract - what success looks like."""
    contract_id: str
    nonce: str
    intent: str
    success_indicators: List[str]        # What worker sees (fuzzy)
    criteria: List[Criterion]            # What we validate (exact)
    proof_required: List[str]            # Artifact IDs that must exist
    
    declared_at: float = field(default_factory=time.time)
    timeout_seconds: int = 300
    max_attempts: int = 3
    attempt: int = 0
    status: TaskStatus = TaskStatus.CONTRACTED
    
    workspace: Optional[str] = None
    context: str = ""
    
    def to_delegation_payload(self) -> dict:
        """Generate what worker receives (partial visibility)."""
        visible_criteria = [
            c.to_worker_view() for c in self.criteria
            if c.visibility != CriteriaVisibility.HIDDEN
        ]
        visible_criteria = [c for c in visible_criteria if c]
        
        return {
            "contract_id": self.contract_id,
            "goal": self.intent,
            "context": self.context,
            "workspace": self.workspace,
            "success_indicators": self.success_indicators,
            "quality_requirements": visible_criteria,
            "timeout_seconds": self.timeout_seconds,
            "attempt": self.attempt + 1,
            "max_attempts": self.max_attempts,
        }


@dataclass
class ValidationResult:
    """Result of validating artifacts against contract."""
    passed: bool
    layer_results: Dict[str, List[dict]]
    failed_criteria: List[str]
    missing_artifacts: List[str]
    evidence: Dict[str, Any]
    audit_triggered: bool = False
    failure_class: Optional[FailureClass] = None
    feedback: Optional[str] = None


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class GateValidator:
    """Validates worker artifacts against contract criteria."""
    
    def __init__(self, audit_probability: float = 0.15):
        self.audit_probability = audit_probability
    
    def validate(self, contract: Contract, artifacts: List[Artifact]) -> ValidationResult:
        """Run full validation pipeline."""
        
        layer_results = {layer.value: [] for layer in ValidationLayer}
        failed_criteria = []
        evidence = {}
        
        # Build artifact lookup
        artifact_map = {a.id: a for a in artifacts}
        
        # Check required artifacts exist
        missing = [k for k in contract.proof_required if k not in artifact_map]
        if missing:
            return ValidationResult(
                passed=False,
                layer_results=layer_results,
                failed_criteria=["missing_artifacts"],
                missing_artifacts=missing,
                evidence={},
                failure_class=FailureClass.RETRYABLE,
                feedback=f"Missing required artifacts: {missing}. Worker must return these.",
            )
        
        # Verify artifact hashes
        for artifact in artifacts:
            if not artifact.verify_hash():
                return ValidationResult(
                    passed=False,
                    layer_results=layer_results,
                    failed_criteria=["hash_mismatch"],
                    missing_artifacts=[],
                    evidence={},
                    failure_class=FailureClass.RETRYABLE,
                    feedback=f"Artifact '{artifact.id}' hash mismatch. Content was modified.",
                )
        
        # Validate each criterion
        audit_triggered = False
        for criterion in contract.criteria:
            # Skip hidden criteria unless audit triggered
            if criterion.visibility == CriteriaVisibility.HIDDEN:
                if random.random() > self.audit_probability:
                    continue
                audit_triggered = True
            
            artifact = artifact_map.get(criterion.proof_source)
            result = self._evaluate_criterion(criterion, artifact)
            
            layer_results[criterion.layer.value].append({
                "criterion_id": criterion.id,
                "passed": result["passed"],
                "reason": result.get("reason", ""),
            })
            
            if result.get("evidence"):
                evidence[criterion.id] = result["evidence"]
            
            if not result["passed"] and criterion.required:
                failed_criteria.append(criterion.id)
        
        passed = len(failed_criteria) == 0
        
        feedback = None
        failure_class = None
        if not passed:
            failure_class = FailureClass.RETRYABLE
            feedback = self._generate_feedback(failed_criteria, layer_results, contract)
        
        return ValidationResult(
            passed=passed,
            layer_results=layer_results,
            failed_criteria=failed_criteria,
            missing_artifacts=[],
            evidence=evidence,
            audit_triggered=audit_triggered,
            failure_class=failure_class,
            feedback=feedback,
        )
    
    def _evaluate_criterion(self, criterion: Criterion, artifact: Optional[Artifact]) -> dict:
        """Evaluate a single criterion."""
        
        if not artifact:
            return {"passed": False, "reason": f"No artifact '{criterion.proof_source}'"}
        
        if criterion.proof_type == ProofType.EXIT_CODE:
            passed = artifact.exit_code == 0
            return {
                "passed": passed,
                "reason": f"exit_code={artifact.exit_code}",
                "evidence": {"exit_code": artifact.exit_code},
            }
        
        elif criterion.proof_type == ProofType.FILE_EXISTS:
            if artifact.path:
                exists = Path(artifact.path).expanduser().exists()
            else:
                exists = bool(artifact.content)
            return {
                "passed": exists,
                "reason": f"file {'exists' if exists else 'missing'}",
                "evidence": {"path": artifact.path, "exists": exists},
            }
        
        elif criterion.proof_type == ProofType.TEST_PASS:
            passed = artifact.test_failed == 0 if artifact.test_failed is not None else False
            return {
                "passed": passed,
                "reason": f"passed={artifact.test_passed}, failed={artifact.test_failed}",
                "evidence": {"passed": artifact.test_passed, "failed": artifact.test_failed},
            }
        
        elif criterion.proof_type == ProofType.CONTENT_MATCH:
            import re
            content = artifact.content or ""
            if artifact.path:
                p = Path(artifact.path).expanduser()
                if p.exists():
                    try:
                        content = p.read_text()
                    except:
                        pass
            
            pattern = criterion.pattern or ""
            matched = bool(re.search(pattern, content)) if pattern else True
            return {
                "passed": matched,
                "reason": f"pattern {'matched' if matched else 'not found'}",
                "evidence": {"pattern": pattern, "matched": matched},
            }
        
        elif criterion.proof_type == ProofType.HASH_MATCH:
            actual = artifact.compute_hash()
            expected = criterion.expected_hash or ""
            matched = actual == expected
            return {
                "passed": matched,
                "reason": f"hash {'matched' if matched else 'mismatch'}",
                "evidence": {"expected": expected, "actual": actual},
            }
        
        return {"passed": False, "reason": f"Unknown proof_type: {criterion.proof_type}"}
    
    def _generate_feedback(self, failed: List[str], layer_results: dict, contract: Contract) -> str:
        """Generate actionable feedback for retry."""
        
        for layer in ValidationLayer:
            layer_failures = [r for r in layer_results[layer.value] if not r["passed"]]
            if layer_failures:
                first = layer_failures[0]
                criterion = next(
                    (c for c in contract.criteria if c.id == first["criterion_id"]),
                    None
                )
                
                if criterion and criterion.visibility != CriteriaVisibility.HIDDEN:
                    return (
                        f"Failed at {layer.value}: {criterion.description}. "
                        f"Reason: {first['reason']}. Fix and retry."
                    )
        
        return f"Validation failed: {failed}. Review and retry."


# ---------------------------------------------------------------------------
# Gate State Manager
# ---------------------------------------------------------------------------

class ExecutionGate:
    """Main gate orchestrator."""
    
    def __init__(self, state_dir: Optional[Path] = None, config: Optional[dict] = None):
        self.config = config or {}
        
        if state_dir:
            self.state_dir = Path(state_dir) if isinstance(state_dir, str) else state_dir
        else:
            hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
            profile = os.environ.get("HERMES_PROFILE_NAME", "default")
            self.state_dir = Path(hermes_home) / "profiles" / profile / "execution_gate"
        
        self.state_dir.mkdir(parents=True, exist_ok=True)
        
        self.validator = GateValidator(
            audit_probability=self.config.get("audit_probability", 0.15)
        )
        self._contract: Optional[Contract] = None
        self._load_state()
    
    def _state_path(self) -> Path:
        session_id = os.environ.get("HERMES_SESSION_ID", "default")
        return self.state_dir / f"session_{session_id}.json"
    
    def _load_state(self):
        """Load contract state from disk."""
        path = self._state_path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                if data.get("contract"):
                    c = data["contract"]
                    self._contract = Contract(
                        contract_id=c["contract_id"],
                        nonce=c["nonce"],
                        intent=c["intent"],
                        success_indicators=c["success_indicators"],
                        criteria=[
                            Criterion(
                                id=cr["id"],
                                description=cr["description"],
                                proof_type=ProofType(cr["proof_type"]),
                                proof_source=cr["proof_source"],
                                layer=ValidationLayer(cr.get("layer", "L1_structural")),
                                visibility=CriteriaVisibility(cr.get("visibility", "disclosed")),
                                required=cr.get("required", True),
                                pattern=cr.get("pattern"),
                                expected_hash=cr.get("expected_hash"),
                            )
                            for cr in c["criteria"]
                        ],
                        proof_required=c["proof_required"],
                        declared_at=c["declared_at"],
                        timeout_seconds=c.get("timeout_seconds", 300),
                        max_attempts=c.get("max_attempts", 3),
                        attempt=c.get("attempt", 0),
                        status=TaskStatus(c.get("status", "contracted")),
                        workspace=c.get("workspace"),
                        context=c.get("context", ""),
                    )
            except Exception:
                pass
    
    def _save_state(self):
        """Save contract state to disk."""
        path = self._state_path()
        
        if not self._contract:
            if path.exists():
                path.unlink()
            return
        
        c = self._contract
        data = {
            "contract": {
                "contract_id": c.contract_id,
                "nonce": c.nonce,
                "intent": c.intent,
                "success_indicators": c.success_indicators,
                "criteria": [
                    {
                        "id": cr.id,
                        "description": cr.description,
                        "proof_type": cr.proof_type.value,
                        "proof_source": cr.proof_source,
                        "layer": cr.layer.value,
                        "visibility": cr.visibility.value,
                        "required": cr.required,
                        "pattern": cr.pattern,
                        "expected_hash": cr.expected_hash,
                    }
                    for cr in c.criteria
                ],
                "proof_required": c.proof_required,
                "declared_at": c.declared_at,
                "timeout_seconds": c.timeout_seconds,
                "max_attempts": c.max_attempts,
                "attempt": c.attempt,
                "status": c.status.value,
                "workspace": c.workspace,
                "context": c.context,
            }
        }
        path.write_text(json.dumps(data, indent=2))
    
    def _archive_contract(self):
        """Archive completed contract."""
        if not self._contract:
            return
        
        archive_dir = self.state_dir / "archive"
        archive_dir.mkdir(exist_ok=True)
        
        archive_path = archive_dir / f"{self._contract.contract_id}_{int(time.time())}.json"
        
        # Copy current state to archive
        state_path = self._state_path()
        if state_path.exists():
            archive_path.write_text(state_path.read_text())
        
        self._contract = None
        self._save_state()
    
    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------
    
    def contract(
        self,
        intent: str,
        success_indicators: List[str],
        criteria: List[dict],
        proof_required: Optional[List[str]] = None,
        workspace: Optional[str] = None,
        context: str = "",
        timeout_seconds: int = 300,
        max_attempts: int = 3,
    ) -> dict:
        """Declare a contract before delegating work."""
        
        if self._contract and self._contract.status in (TaskStatus.CONTRACTED, TaskStatus.DELEGATED):
            return {
                "ok": False,
                "error": "contract_active",
                "message": f"Contract already active: {self._contract.intent}",
                "hint": "Complete, validate, or abandon the current contract first.",
            }
        
        nonce = hashlib.sha256(os.urandom(32)).hexdigest()[:16]
        contract_id = f"gate_{nonce}"
        
        # Parse criteria
        parsed_criteria = []
        for c in criteria:
            parsed_criteria.append(Criterion(
                id=c["id"],
                description=c["description"],
                proof_type=ProofType(c["proof_type"]),
                proof_source=c["proof_source"],
                layer=ValidationLayer(c.get("layer", "L1_structural")),
                visibility=CriteriaVisibility(c.get("visibility", "disclosed")),
                required=c.get("required", True),
                pattern=c.get("pattern"),
                expected_hash=c.get("expected_hash"),
            ))
        
        # Auto-extract proof_required if not specified
        if not proof_required:
            proof_required = list(set(c.proof_source for c in parsed_criteria))
        
        self._contract = Contract(
            contract_id=contract_id,
            nonce=nonce,
            intent=intent,
            success_indicators=success_indicators,
            criteria=parsed_criteria,
            proof_required=proof_required,
            workspace=workspace,
            context=context,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        
        self._save_state()
        
        return {
            "ok": True,
            "contract_id": contract_id,
            "intent": intent,
            "success_indicators": success_indicators,
            "proof_required": proof_required,
            "message": "Contract declared. Call gate_delegate, then dispatch through scoped_worker_dispatch/scoped_worker_batch.",
        }
    
    def delegate(self, additional_context: str = "") -> dict:
        """Get delegation payload for current contract."""
        
        if not self._contract:
            return {
                "ok": False,
                "error": "no_contract",
                "message": "No active contract. Call gate_contract first.",
            }
        
        if additional_context:
            self._contract.context = f"{self._contract.context}\n\n{additional_context}".strip()
        
        self._contract.status = TaskStatus.DELEGATED
        self._save_state()
        
        payload = self._contract.to_delegation_payload()
        
        return {
            "ok": True,
            "contract_id": self._contract.contract_id,
            "delegation_payload": payload,
            "message": (
                "Dispatch this payload through scoped_worker_dispatch or scoped_worker_batch. "
                "When the scoped worker returns, call gate_validate with artifacts."
            ),
        }
    
    def validate(self, artifacts: List[dict], summary: str = "") -> dict:
        """Validate worker artifacts against contract."""
        
        if not self._contract:
            return {
                "ok": False,
                "error": "no_contract",
                "message": "No active contract.",
            }
        
        # Parse artifacts
        parsed_artifacts = []
        for a in artifacts:
            parsed_artifacts.append(Artifact(
                id=a["id"],
                artifact_type=a.get("type", "unknown"),
                content_hash=a.get("content_hash"),
                path=a.get("path"),
                content=a.get("content"),
                exit_code=a.get("exit_code"),
                test_passed=a.get("test_passed"),
                test_failed=a.get("test_failed"),
                metadata=a.get("metadata", {}),
            ))
        
        self._contract.status = TaskStatus.VALIDATING
        result = self.validator.validate(self._contract, parsed_artifacts)
        
        if result.passed:
            self._contract.status = TaskStatus.COMPLETED
            completion = {
                "ok": True,
                "passed": True,
                "contract_id": self._contract.contract_id,
                "intent": self._contract.intent,
                "attempts": self._contract.attempt + 1,
                "evidence": result.evidence,
                "audit_triggered": result.audit_triggered,
                "message": "Validation passed. Task complete.",
            }
            self._archive_contract()
            return completion
        
        # Failed - prepare retry
        self._contract.attempt += 1
        
        if self._contract.attempt >= self._contract.max_attempts:
            self._contract.status = TaskStatus.FAILED
            failure = {
                "ok": False,
                "passed": False,
                "can_retry": False,
                "contract_id": self._contract.contract_id,
                "attempts": self._contract.attempt,
                "failed_criteria": result.failed_criteria,
                "feedback": result.feedback,
                "message": f"Max attempts ({self._contract.max_attempts}) exceeded. Escalate to user.",
            }
            self._archive_contract()
            return failure
        
        self._contract.status = TaskStatus.CONTRACTED  # Ready for re-delegation
        self._save_state()
        
        return {
            "ok": False,
            "passed": False,
            "can_retry": True,
            "contract_id": self._contract.contract_id,
            "attempt": self._contract.attempt + 1,
            "max_attempts": self._contract.max_attempts,
            "failed_criteria": result.failed_criteria,
            "feedback": result.feedback,
            "message": "Validation failed. Re-delegate with feedback.",
        }
    
    def status(self) -> dict:
        """Get current contract status."""
        
        if not self._contract:
            return {
                "status": "idle",
                "message": "No active contract. Call gate_contract to start.",
            }
        
        visible_criteria = [
            c.to_worker_view() for c in self._contract.criteria
            if c.visibility != CriteriaVisibility.HIDDEN
        ]
        visible_criteria = [c for c in visible_criteria if c]
        
        return {
            "status": self._contract.status.value,
            "contract_id": self._contract.contract_id,
            "intent": self._contract.intent,
            "success_indicators": self._contract.success_indicators,
            "visible_criteria": visible_criteria,
            "proof_required": self._contract.proof_required,
            "attempt": self._contract.attempt,
            "max_attempts": self._contract.max_attempts,
            "age_seconds": int(time.time() - self._contract.declared_at),
        }
    
    def abandon(self, reason: str) -> dict:
        """Abandon current contract."""
        
        if not self._contract:
            return {"ok": True, "message": "No active contract."}
        
        self._contract.status = TaskStatus.ABANDONED
        result = {
            "ok": True,
            "contract_id": self._contract.contract_id,
            "intent": self._contract.intent,
            "reason": reason,
            "message": "Contract abandoned.",
        }
        self._archive_contract()
        return result
    
    def decompose(self, subtasks: List[dict]) -> dict:
        """
        Decompose work into parallel subtasks.
        
        Each subtask gets its own mini-contract derived from the parent.
        Independent tasks run in parallel; dependent tasks wait.
        
        Args:
            subtasks: List of subtask definitions, each with:
                - id: Unique subtask identifier
                - goal: What this subtask should accomplish
                - context: Subtask-specific context
                - toolsets: Tools the worker needs
                - depends_on: List of subtask IDs this depends on (optional)
                - criteria: Subtask-specific criteria (optional, inherits from parent)
        
        Returns:
            Decomposition plan with parallel groups and delegation payloads.
        """
        
        if not self._contract:
            return {
                "ok": False,
                "error": "no_contract",
                "message": "No active contract. Call gate_contract first.",
            }
        
        if not subtasks:
            return {
                "ok": False,
                "error": "no_subtasks",
                "message": "At least one subtask required.",
            }
        
        # Validate subtask structure
        subtask_ids = set()
        for i, st in enumerate(subtasks):
            if not st.get("id"):
                return {"ok": False, "error": f"subtask {i} missing 'id'"}
            if not st.get("goal"):
                return {"ok": False, "error": f"subtask {i} missing 'goal'"}
            if st["id"] in subtask_ids:
                return {"ok": False, "error": f"duplicate subtask id: {st['id']}"}
            subtask_ids.add(st["id"])
        
        # Validate dependencies exist
        for st in subtasks:
            for dep in st.get("depends_on", []):
                if dep not in subtask_ids:
                    return {"ok": False, "error": f"subtask '{st['id']}' depends on unknown '{dep}'"}
        
        # Build dependency graph and compute parallel groups
        # Group 0: no dependencies (can run immediately)
        # Group 1: depends only on group 0
        # etc.
        
        resolved = set()
        groups = []
        remaining = list(subtasks)
        
        while remaining:
            # Find all tasks whose dependencies are resolved
            ready = []
            still_waiting = []
            
            for st in remaining:
                deps = set(st.get("depends_on", []))
                if deps <= resolved:
                    ready.append(st)
                else:
                    still_waiting.append(st)
            
            if not ready:
                # Circular dependency
                unresolved = [st["id"] for st in still_waiting]
                return {
                    "ok": False,
                    "error": "circular_dependency",
                    "message": f"Circular dependency detected among: {unresolved}",
                }
            
            groups.append(ready)
            resolved.update(st["id"] for st in ready)
            remaining = still_waiting
        
        # Generate delegation payloads for each subtask
        payloads = []
        for group_idx, group in enumerate(groups):
            group_payloads = []
            for st in group:
                # Inherit from parent contract
                payload = {
                    "subtask_id": st["id"],
                    "group": group_idx,
                    "goal": st["goal"],
                    "context": f"{self._contract.context}\n\nSubtask: {st['goal']}\n{st.get('context', '')}".strip(),
                    "toolsets": st.get("toolsets", ["terminal", "file"]),
                    "depends_on": st.get("depends_on", []),
                    "parent_contract_id": self._contract.contract_id,
                    "parent_intent": self._contract.intent,
                    "success_indicators": st.get("success_indicators", [st["goal"]]),
                }
                
                # Subtask-specific criteria or inherit
                if st.get("criteria"):
                    payload["criteria"] = st["criteria"]
                
                group_payloads.append(payload)
            payloads.append(group_payloads)
        
        # Store decomposition in contract state
        self._contract.status = TaskStatus.DELEGATED
        self._save_state()
        
        return {
            "ok": True,
            "contract_id": self._contract.contract_id,
            "total_subtasks": len(subtasks),
            "parallel_groups": len(groups),
            "execution_plan": [
                {
                    "group": i,
                    "parallel": True,
                    "subtasks": [{"id": p["subtask_id"], "goal": p["goal"]} for p in group],
                }
                for i, group in enumerate(payloads)
            ],
            "delegation_payloads": payloads,
            "message": (
                f"Decomposed into {len(subtasks)} subtasks across {len(groups)} parallel groups. "
                f"Group 0 can run immediately through scoped_worker_batch with authorized worker profiles."
            ),
        }
    
    def aggregate(self, subtask_results: List[dict]) -> dict:
        """
        Aggregate results from parallel subtasks.
        
        Combines artifacts from all subtasks and validates against parent contract.
        
        Args:
            subtask_results: List of results, each with:
                - subtask_id: Which subtask this is from
                - artifacts: Artifacts from that subtask
                - summary: Worker summary (optional)
        
        Returns:
            Aggregated validation result.
        """
        
        if not self._contract:
            return {
                "ok": False,
                "error": "no_contract",
                "message": "No active contract.",
            }
        
        if not subtask_results:
            return {
                "ok": False,
                "error": "no_results",
                "message": "At least one subtask result required.",
            }
        
        # Combine all artifacts with subtask prefix
        combined_artifacts = []
        summaries = []
        
        for result in subtask_results:
            subtask_id = result.get("subtask_id", "unknown")
            summaries.append(f"[{subtask_id}] {result.get('summary', 'No summary')}")
            
            for artifact in result.get("artifacts", []):
                # Always prefix artifact ID with subtask ID
                # This ensures unique IDs and predictable proof_source mapping
                combined = artifact.copy()
                combined["id"] = f"{subtask_id}_{combined.get('id', 'artifact')}"
                combined_artifacts.append(combined)
        
        # Run validation with combined artifacts
        return self.validate(
            artifacts=combined_artifacts,
            summary="\n".join(summaries),
        )


# ---------------------------------------------------------------------------
# Singleton instance
# ---------------------------------------------------------------------------

_gate: Optional[ExecutionGate] = None


def get_gate() -> ExecutionGate:
    """Get or create the execution gate instance."""
    global _gate
    if _gate is None:
        _gate = ExecutionGate()
    return _gate
