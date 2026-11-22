"""Pure domain data models for runlatch.

Imports ONLY Python stdlib and pydantic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class RiskTier(StrEnum):
    """Deterministic risk classification for tools."""

    READ = "READ"
    WRITE = "WRITE"
    EXEC = "EXEC"
    EXTERNAL = "EXTERNAL"

    @property
    def rank(self) -> int:
        order = {
            RiskTier.READ: 0,
            RiskTier.WRITE: 1,
            RiskTier.EXEC: 2,
            RiskTier.EXTERNAL: 3,
        }
        return order[self]

    def __le__(self, other: object) -> bool:
        if not isinstance(other, RiskTier):
            return NotImplemented
        return self.rank <= other.rank

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, RiskTier):
            return NotImplemented
        return self.rank < other.rank

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, RiskTier):
            return NotImplemented
        return self.rank >= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, RiskTier):
            return NotImplemented
        return self.rank > other.rank


class RunStatus(StrEnum):
    """Lifecycle states of a workflow or agent run."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RESUMING = "RESUMING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepStatus(StrEnum):
    """Lifecycle states of an individual workflow step."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"


class ToolCallStatus(StrEnum):
    """Audit outcomes of a single tool invocation."""

    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class ApprovalStatus(StrEnum):
    """Human-in-the-Loop approval decision states."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PolicyAction(StrEnum):
    """PolicyEngine evaluation outcomes."""

    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class PolicyDecision(BaseModel):
    """Result returned by PolicyEngine.evaluate()."""

    action: PolicyAction
    reason: str = ""


class ToolSpec(BaseModel):
    """Compiled metadata and JSON schema for an LLM-callable tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    risk_tier: RiskTier = RiskTier.READ
    requires_approval: bool = False
    timeout_s: float = 30.0
    max_output_chars: int = 16000


class ToolCall(BaseModel):
    """A single tool invocation requested by an LLM."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """Normalized conversation transcript message."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


class LLMResponse(BaseModel):
    """Normalized completion payload returned by any LLMProvider."""

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = "mock"
    latency_ms: int = 0
    finish_reason: Literal["stop", "tool_calls", "length", "error"] = "stop"

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class BudgetConfig(BaseModel):
    """Hard execution limits enforced before every LLM call."""

    max_steps: int = 15
    max_tokens: int = 80000
    max_cost_usd: float = 0.50
    max_duplicate_tool_calls: int = 2


class WorkspaceMeta(BaseModel):
    """Repository state metadata enabling stateless git worktree resume."""

    repo_url: str = ""
    base_commit_sha: str = ""
    branch_name: str = ""
    workspace_root: str = ""


class ToolCallRecord(BaseModel):
    """Immutable audit record of a single tool call (with redacted args/output)."""

    id: UUID = Field(default_factory=uuid4)
    step_id: UUID | None = None
    run_id: UUID
    call_id: str
    tool_name: str
    risk_tier: RiskTier
    redacted_args: dict[str, Any] = Field(default_factory=dict)
    redacted_output: str | None = None
    status: ToolCallStatus
    latency_ms: int = 0
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class StepRecord(BaseModel):
    """Execution record for a single step in a Workflow."""

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    step_index: int
    step_name: str
    agent_name: str
    status: StepStatus
    model_used: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    output_payload: dict[str, Any] | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ApprovalRequestRecord(BaseModel):
    """Human-in-the-Loop approval gate record."""

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    tool_call_id: UUID | None = None
    call_id: str = ""
    tool_name: str
    risk_tier: RiskTier
    sanitized_args: dict[str, Any] = Field(default_factory=dict)
    diff_preview: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: str | None = None
    decision_reason: str | None = None
    expires_at: datetime = Field(default_factory=utc_now)
    decided_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class RunCheckpoint(BaseModel):
    """Durable state snapshot persisted at step boundaries and HITL pauses."""

    run_id: UUID = Field(default_factory=uuid4)
    workflow_name: str = "agent"
    status: RunStatus = RunStatus.RUNNING
    trigger_source: Literal["cli", "api", "github_webhook", "schedule"] = "cli"
    repository: str | None = None
    input_payload: dict[str, Any] = Field(default_factory=dict)
    context_state: dict[str, Any] = Field(default_factory=dict)
    messages_checkpoint: list[Message] = Field(default_factory=list)
    pending_tool_call: ToolCall | None = None
    remaining_tool_calls: list[ToolCall] = Field(default_factory=list)
    pending_approval_id: UUID | None = None
    workspace_meta: WorkspaceMeta | None = None
    workspace_patch: str | None = None
    current_step_index: int = 0
    steps_taken: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    max_cost_usd: float = 0.50
    error_message: str | None = None
    started_at: datetime | None = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class RunResult(BaseModel):
    """Final or paused outcome of an Agent or Workflow execution."""

    run_id: UUID
    workflow_name: str = "agent"
    status: RunStatus
    output: str = ""
    context_state: dict[str, Any] = Field(default_factory=dict)
    messages: list[Message] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    approval_id: UUID | None = None
    pending_tool_call: ToolCall | None = None
    workspace_patch: str | None = None
    workspace_meta: WorkspaceMeta | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    steps_taken: int = 0
    error_message: str | None = None


class CommandResult(BaseModel):
    """Outcome of a command executed by a CommandExecutor."""

    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_ms: int = 0


class NotificationEvent(BaseModel):
    """Structured event payload routed to NotificationProviders."""

    event_id: UUID = Field(default_factory=uuid4)
    event_type: Literal[
        "approval.requested",
        "approval.expired",
        "run.completed",
        "run.failed",
        "budget.exceeded",
    ]
    run_id: UUID
    workflow_name: str
    title: str
    summary: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    action_urls: dict[str, str] = Field(default_factory=dict)


class DeliveryReceipt(BaseModel):
    """Result returned by a NotificationProvider."""

    channel: str
    target: str
    status: Literal["SENT", "FAILED"]
    provider_message_id: str | None = None
    error: str | None = None


class LifecycleEvent(BaseModel):
    """Internal EventBus event emitted during agent and workflow runs."""

    event_type: str
    run_id: UUID
    workflow_name: str
    step_name: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)
