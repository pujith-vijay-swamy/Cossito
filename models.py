"""
models.py — Validated contracts for OmniTasker v2.

Defines structured types for tasks, observations, actions, results,
approvals, and the overall agent state machine. All task lifecycle
transitions are governed by the deterministic state machine in
state_machine.py, not by prompts.

Schema version: 2.0
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TaskState(str, Enum):
    """Deterministic task lifecycle states (v2 state machine).

    See state_machine.py for valid transitions.
    """

    QUEUED = "queued"
    RECONCILING = "reconciling"
    READY = "ready"
    PLANNING = "planning"
    EDITING = "editing"
    VALIDATING = "validating"
    AWAITING_APPROVAL = "awaiting_approval"
    SUBMITTING = "submitting"
    RECONCILING_SUBMISSION = "reconciling_submission"
    VERIFYING = "verifying"
    PASSED = "passed"
    REPAIRING = "repairing"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"
    # Supervisory transitions
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class AgentPhase(str, Enum):
    """High-level phase of the agent orchestration."""

    IDLE = "idle"
    RECON = "recon"
    EXECUTING = "executing"
    PAUSED_HITL = "paused_hitl"
    DONE = "done"
    ERROR = "error"


class SecurityDecision(str, Enum):
    """Outcomes from the two-layer URL security gate."""

    ALLOWED = "allowed"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"


class FailureCategory(str, Enum):
    """Classified failure types for targeted recovery."""

    COMPILATION = "compilation"
    RUNTIME = "runtime"
    WRONG_ANSWER = "wrong_answer"
    TIMEOUT = "timeout"
    SELECTOR_TIMEOUT = "selector_timeout"
    DOM_MUTATION = "dom_mutation"
    FRAME_DETACHED = "frame_detached"
    EDITOR_MISMATCH = "editor_mismatch"
    STALE_EVIDENCE = "stale_evidence"
    NETWORK = "network"
    AUTH_WALL = "auth_wall"
    SECURITY_BLOCK = "security_block"
    UNKNOWN = "unknown"


class RecoveryDecision(str, Enum):
    """What the recovery controller decided to do."""

    RETRY = "retry"
    FALLBACK = "fallback"
    ESCALATE = "escalate"
    EXHAUSTED = "exhausted"


class ControlCommand(str, Enum):
    """Typed human control commands."""

    PAUSE = "pause"
    RESUME = "resume"
    SKIP = "skip"
    CANCEL = "cancel"
    GUIDANCE = "guidance"
    APPROVE_SUBMIT = "approve_submit"
    SECURITY_APPROVE = "security_approve"
    SECURITY_BLOCK = "security_block"


# --- v1 compatibility alias ---
class TaskStatus(str, Enum):
    """v1-compatible task status (kept for app.py backward compatibility)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"


# ---------------------------------------------------------------------------
# Evidence & provenance models
# ---------------------------------------------------------------------------

class PlatformTask(BaseModel):
    """Stable platform identity for a task (distinct from queue position)."""

    key: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    navigation_hint: Optional[str] = None


class StatementEvidence(BaseModel):
    """Problem statement with extraction provenance."""

    raw_text: str = ""
    source_evidence_ids: list[str] = Field(default_factory=list)
    coverage: str = "unknown"  # "complete", "partial", "unknown"
    missing_sections: list[str] = Field(default_factory=list)


class SampleCase(BaseModel):
    """An observed input/output sample from the problem statement."""

    input: str = ""
    expected_output: str = ""
    explanation: str = ""
    observed: bool = True  # True if directly observed, False if derived


class Requirements(BaseModel):
    """Task requirements extracted from the problem statement."""

    language: Optional[str] = None
    entrypoint: Optional[str] = None  # Required class/function name
    constraints: list[str] = Field(default_factory=list)
    samples: list[SampleCase] = Field(default_factory=list)


class ReferenceAsset(BaseModel):
    """A reference image or design mockup with provenance."""

    source_url: str = ""
    artifact_path: Optional[str] = None
    dimensions: Optional[str] = None  # "WxH" when known
    observations: str = ""
    confidence: str = "visual_estimate"  # "measured", "visual_estimate", "unknown"


class WorkspaceSnapshot(BaseModel):
    """Editor/workspace state at a point in time."""

    snapshot_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    editor_kind: Optional[str] = None  # "monaco", "codemirror5", "codemirror6", "ace", "textarea"
    model_id: Optional[str] = None  # Monaco model URI or equivalent
    language: Optional[str] = None
    content_hash: str = ""
    content: str = ""
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def compute_hash(self) -> str:
        """Compute and store SHA-256 hash of content."""
        self.content_hash = hashlib.sha256(self.content.encode()).hexdigest()[:16]
        return self.content_hash


class SubmissionRecord(BaseModel):
    """Tracks a single submission attempt with evidence."""

    submission_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str = ""
    code_hash: str = ""
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    terminal: bool = False
    outcome: Optional[str] = None  # "accepted", "rejected", "pending", "timeout", "unknown"
    passed_count: Optional[int] = None
    total_count: Optional[int] = None
    failed_count: Optional[int] = None
    evidence_ids: list[str] = Field(default_factory=list)
    raw_output: str = ""


class Budgets(BaseModel):
    """Remaining execution budgets for a task."""

    code_repairs: int = 5
    browser_recoveries: int = 3
    total_time_seconds: int = 1800
    model_calls: int = 100
    submissions: int = 10

    def has_code_repairs(self) -> bool:
        return self.code_repairs > 0

    def has_browser_recoveries(self) -> bool:
        return self.browser_recoveries > 0

    def has_submissions(self) -> bool:
        return self.submissions > 0

    def use_code_repair(self) -> None:
        if self.code_repairs <= 0:
            raise BudgetExhausted("code_repairs")
        self.code_repairs -= 1

    def use_browser_recovery(self) -> None:
        if self.browser_recoveries <= 0:
            raise BudgetExhausted("browser_recoveries")
        self.browser_recoveries -= 1

    def use_submission(self) -> None:
        if self.submissions <= 0:
            raise BudgetExhausted("submissions")
        self.submissions -= 1


# ---------------------------------------------------------------------------
# Verification models
# ---------------------------------------------------------------------------

class VerificationResult(BaseModel):
    """Combined functional and visual verification outcome."""

    functional: str = "unknown"  # "passed", "failed", "partial", "unknown"
    visual: str = "not_applicable"  # "matches", "differs", "not_applicable", "unknown"
    submission_id: Optional[str] = None
    evidence_ids: list[str] = Field(default_factory=list)
    details: str = ""


# ---------------------------------------------------------------------------
# DevTools Perception & Telemetry models (Phase 3)
# ---------------------------------------------------------------------------

class AXNode(BaseModel):
    """A semantic node from the Chrome Accessibility (AX) tree."""

    node_id: str = ""
    role: str = ""
    name: str = ""
    value: str = ""
    description: str = ""
    disabled: bool = False
    focused: bool = False
    bounds: list[float] = Field(default_factory=list)  # [x, y, width, height]
    children_ids: list[str] = Field(default_factory=list)


class AccessibilitySnapshot(BaseModel):
    """Structured summary of the page's accessibility tree."""

    nodes: list[AXNode] = Field(default_factory=list)
    interactive_elements: list[str] = Field(default_factory=list)
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ConsoleMessage(BaseModel):
    """Captured console log or uncaught exception from Chrome runtime."""

    level: str = "info"  # "log", "warn", "error", "exception", "debug"
    text: str = ""
    source: str = ""
    url: Optional[str] = None
    line_number: Optional[int] = None
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class NetworkRequestLog(BaseModel):
    """Captured network request/response telemetry."""

    request_id: str = ""
    url: str = ""
    method: str = "GET"
    status_code: Optional[int] = None
    resource_type: str = ""
    failed: bool = False
    error_text: Optional[str] = None
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class MultimodalObservation(BaseModel):
    """Multi-channel sensory perception bundle from Chrome DevTools MCP layer."""

    url: str = ""
    dom_summary: str = ""
    a11y_tree: Optional[AccessibilitySnapshot] = None
    console_errors: list[ConsoleMessage] = Field(default_factory=list)
    network_failures: list[NetworkRequestLog] = Field(default_factory=list)
    has_screenshot: bool = False
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Subagent Task Execution models (Phase 1)
# ---------------------------------------------------------------------------

class SubagentLifecycleState(str, Enum):
    """1:1 Antigravity subagent lifecycle states: Spawning -> Running -> Idle."""

    SPAWNING = "spawning"
    RUNNING = "running"
    IDLE = "idle"
    ERRORED = "errored"


class TaskExecutionResult(BaseModel):
    """Isolated execution outcome returned by a TaskSubagent to orchestrator."""

    task_id: str
    queue_index: int = 0
    lifecycle_state: SubagentLifecycleState = SubagentLifecycleState.IDLE
    success: bool = False
    skipped: bool = False
    solution_code: str = ""
    submissions: list[SubmissionRecord] = Field(default_factory=list)
    verification: VerificationResult = Field(default_factory=VerificationResult)
    error_log: list[str] = Field(default_factory=list)
    recovery_attempts: int = 0
    multimodal_evidence: Optional[MultimodalObservation] = None
    output_message: str = ""


# ---------------------------------------------------------------------------
# Security Models (Phase 2)
# ---------------------------------------------------------------------------

class SecurityPolicy(BaseModel):
    """Two-layer URL security policy configuration."""

    target_base_url: str = ""
    allowed_domains: list[str] = Field(default_factory=list)
    denied_patterns: list[str] = Field(default_factory=list)
    session_approved_domains: set[str] = Field(default_factory=set)


class SecurityCheckResult(BaseModel):
    """Result of evaluating a URL against the two-layer security gate."""

    url: str
    decision: SecurityDecision
    reason: str
    domain: str = ""


# ---------------------------------------------------------------------------
# Recovery models (Phase 4)
# ---------------------------------------------------------------------------

class RecoveryOperation(str, Enum):
    """Typed recovery operations (no arbitrary JS generation)."""

    FRESH_OBSERVATION = "fresh_observation"
    WAIT_FOR_SELECTOR = "wait_for_selector"
    FRAME_REACQUISITION = "frame_reacquisition"
    EDITOR_FALLBACK = "editor_fallback"
    OVERLAY_DISMISSAL = "overlay_dismissal"
    PAGE_RELOAD = "page_reload"
    TAB_SWITCH = "tab_switch"
    AXTREE_LOOKUP = "axtree_lookup"
    CONSOLE_ERROR_PURGE = "console_error_purge"
    NETWORK_RETRY = "network_retry"
    ESCALATE_HUMAN = "escalate_human"


class RecoveryPlan(BaseModel):
    """Structured recovery plan from the recovery planner."""

    diagnosis: str = ""
    confidence: str = "uncertain"  # "confirmed", "likely", "uncertain"
    failure_category: FailureCategory = FailureCategory.UNKNOWN
    side_effect_certainty: str = "unknown"  # "none", "possible", "confirmed"
    decision: RecoveryDecision = RecoveryDecision.ESCALATE
    operations: list[RecoveryOperation] = Field(default_factory=list)
    required_evidence: str = ""
    stop_condition: str = ""


# ---------------------------------------------------------------------------
# Skill models
# ---------------------------------------------------------------------------

class SkillCandidate(BaseModel):
    """A skill candidate in the quarantine pipeline."""

    name: str
    source_code: str = ""
    source_trace: str = ""
    ast_valid: bool = False
    import_safe: bool = False
    prohibited_ops: list[str] = Field(default_factory=list)
    status: str = "quarantined"  # "quarantined", "reviewed", "approved", "rejected"
    version: int = 1
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Human control models
# ---------------------------------------------------------------------------

class PauseRecord(BaseModel):
    """Durable pause state."""

    reason: str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    task_id: Optional[str] = None
    command: ControlCommand = ControlCommand.PAUSE
    acknowledged: bool = False


# ---------------------------------------------------------------------------
# Core task model (v2)
# ---------------------------------------------------------------------------

class CodingTask(BaseModel):
    """v2 task with identity separation, contracts, and deterministic state.

    Key distinctions from v1:
    - task_id is a stable unique ID, NOT the queue position
    - queue_index is the position in the pending execution queue
    - platform_task holds the platform's own identity (question number, URL)
    - state is governed by state_machine.py, not prompts
    - budgets enforce bounded execution
    - submissions track every attempt with evidence
    """

    task_id: str = Field(default_factory=lambda: f"task-{uuid.uuid4().hex[:8]}")
    queue_index: int = 0
    platform_task: PlatformTask = Field(default_factory=PlatformTask)
    kind: str = "unknown"  # "algorithm", "frontend", "database", "intro"
    statement: StatementEvidence = Field(default_factory=StatementEvidence)
    requirements: Requirements = Field(default_factory=Requirements)
    workspace: Optional[WorkspaceSnapshot] = None
    references: list[ReferenceAsset] = Field(default_factory=list)
    verification: VerificationResult = Field(default_factory=VerificationResult)
    state: TaskState = TaskState.QUEUED
    budgets: Budgets = Field(default_factory=Budgets)
    submissions: list[SubmissionRecord] = Field(default_factory=list)
    error_log: list[str] = Field(default_factory=list)
    solution_code: str = ""

    # --- v1 compatibility ---

    @property
    def description(self) -> str:
        """v1-compatible description accessor."""
        return self.statement.raw_text

    @description.setter
    def description(self, value: str) -> None:
        self.statement.raw_text = value

    @property
    def target_selector(self) -> str:
        """v1-compatible target_selector accessor."""
        return self.platform_task.navigation_hint or ""

    @target_selector.setter
    def target_selector(self, value: str) -> None:
        self.platform_task.navigation_hint = value

    @property
    def expected_behavior(self) -> str:
        """v1-compatible expected_behavior accessor."""
        return self.verification.details or ""

    @expected_behavior.setter
    def expected_behavior(self, value: str) -> None:
        self.verification.details = value

    @property
    def visual_design_spec(self) -> str:
        """v1-compatible visual_design_spec accessor."""
        if self.references:
            return self.references[0].observations
        return ""

    @visual_design_spec.setter
    def visual_design_spec(self, value: str) -> None:
        if value and value.strip():
            if self.references:
                self.references[0].observations = value
            else:
                self.references.append(
                    ReferenceAsset(observations=value, confidence="visual_estimate")
                )

    @property
    def status(self) -> TaskStatus:
        """v1-compatible status mapping."""
        _MAP = {
            TaskState.QUEUED: TaskStatus.PENDING,
            TaskState.RECONCILING: TaskStatus.IN_PROGRESS,
            TaskState.READY: TaskStatus.IN_PROGRESS,
            TaskState.PLANNING: TaskStatus.IN_PROGRESS,
            TaskState.EDITING: TaskStatus.IN_PROGRESS,
            TaskState.VALIDATING: TaskStatus.IN_PROGRESS,
            TaskState.AWAITING_APPROVAL: TaskStatus.IN_PROGRESS,
            TaskState.SUBMITTING: TaskStatus.IN_PROGRESS,
            TaskState.RECONCILING_SUBMISSION: TaskStatus.IN_PROGRESS,
            TaskState.VERIFYING: TaskStatus.IN_PROGRESS,
            TaskState.PASSED: TaskStatus.SUCCESS,
            TaskState.REPAIRING: TaskStatus.RETRYING,
            TaskState.NEEDS_INPUT: TaskStatus.IN_PROGRESS,
            TaskState.FAILED: TaskStatus.FAILED,
            TaskState.SKIPPED: TaskStatus.SUCCESS,
            TaskState.CANCELLED: TaskStatus.FAILED,
        }
        return _MAP.get(self.state, TaskStatus.PENDING)

    @status.setter
    def status(self, value: TaskStatus) -> None:
        """v1-compatible status setter (maps back to v2 states)."""
        _REVERSE = {
            TaskStatus.PENDING: TaskState.QUEUED,
            TaskStatus.IN_PROGRESS: TaskState.PLANNING,
            TaskStatus.SUCCESS: TaskState.PASSED,
            TaskStatus.FAILED: TaskState.FAILED,
            TaskStatus.RETRYING: TaskState.REPAIRING,
        }
        self.state = _REVERSE.get(value, self.state)

    @property
    def attempts(self) -> int:
        """v1-compatible attempts counter."""
        return len(self.submissions)

    @attempts.setter
    def attempts(self, value: int) -> None:
        """v1-compatible: no-op (attempts derived from submissions)."""
        pass

    @property
    def max_retries(self) -> int:
        return self.budgets.code_repairs

    @max_retries.setter
    def max_retries(self, value: int) -> None:
        self.budgets.code_repairs = value

    @classmethod
    def from_v1_dict(cls, item: dict, queue_index: int = 0) -> CodingTask:
        """Create a v2 CodingTask from a v1 recon JSON dict."""
        task = cls(
            queue_index=queue_index,
            kind="unknown",
        )
        task.statement.raw_text = item.get("description", "")
        task.platform_task.navigation_hint = item.get("target_selector", "")
        task.verification.details = item.get("expected_behavior", "")

        vds = item.get("visual_design_spec", "")
        if vds and vds.strip():
            task.references.append(
                ReferenceAsset(observations=vds, confidence="visual_estimate")
            )
            task.kind = "frontend"

        # Use task_id from v1 if present for platform identity
        v1_id = item.get("task_id")
        if v1_id is not None:
            task.platform_task.key = str(v1_id)
            task.platform_task.title = f"Question {v1_id}"

        return task


class TaskQueue(BaseModel):
    """The structured output of the reconnaissance phase."""

    tasks: list[CodingTask] = Field(default_factory=list)
    source_url: str = ""
    recon_summary: str = Field(
        "", description="LLM's overall analysis of the page"
    )


# ---------------------------------------------------------------------------
# Logging & UI state
# ---------------------------------------------------------------------------

class LogEntry(BaseModel):
    """A timestamped log line for the Streamlit UI."""

    timestamp: str
    level: str = "info"  # info | warn | error | success
    message: str


class AgentState(BaseModel):
    """Tracks the full state of the agent orchestration for the UI."""

    phase: AgentPhase = AgentPhase.IDLE
    current_task_index: int = 0
    task_queue: Optional[TaskQueue] = None
    logs: list[LogEntry] = Field(default_factory=list)
    hitl_reason: Optional[str] = None
    error_message: Optional[str] = None
    progress_pct: float = 0.0
    model_thinking: Optional[str] = None
    model_memory: Optional[str] = None
    model_next_goal: Optional[str] = None
    model_last_actions: list[str] = Field(default_factory=list)
    failed_injection_code: Optional[str] = None
    current_solution_code: Optional[str] = None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class InvalidTransition(Exception):
    """Raised when a state machine transition is invalid."""
    pass


class BudgetExhausted(Exception):
    """Raised when a budget limit is reached."""

    def __init__(self, budget_name: str) -> None:
        self.budget_name = budget_name
        super().__init__(f"Budget exhausted: {budget_name}")
