"""Hexagonal boundary protocols for runlatch core.

Every external adapter (LLM, Storage, Command Executor, Task Queue,
Notification Channel, Step Executor) implements a Protocol defined here.
Imports ONLY Python stdlib, pydantic, and runlatch.core.types.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from runlatch.core.types import (
    ApprovalRequestRecord,
    ApprovalStatus,
    BudgetConfig,
    CommandResult,
    DeliveryReceipt,
    LLMResponse,
    Message,
    NotificationEvent,
    PolicyDecision,
    RunCheckpoint,
    RunResult,
    StepRecord,
    ToolCallRecord,
    ToolSpec,
    WorkspaceMeta,
)


@runtime_checkable
class RegisteredTool(Protocol):
    """Protocol implemented by any @tool or MCP-wrapped tool."""

    @property
    def spec(self) -> ToolSpec: ...

    async def invoke(self, args: dict[str, Any]) -> str: ...


@runtime_checkable
class LLMProvider(Protocol):
    """Unified async LLM completion adapter protocol."""

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse: ...


@runtime_checkable
class PolicyEvaluator(Protocol):
    """Evaluates whether a tool call is allowed, denied, or requires approval."""

    def evaluate(self, spec: ToolSpec, args: dict[str, Any]) -> PolicyDecision: ...


@runtime_checkable
class SecretRedactorProtocol(Protocol):
    """Scrubs sensitive credentials from strings and nested payloads."""

    def redact_text(self, text: str) -> str: ...

    def redact_obj(self, obj: Any) -> Any: ...


@runtime_checkable
class WorkspacePatcher(Protocol):
    """Captures and restores workspace git patches for stateless HITL pause/resume."""

    async def capture_patch(self) -> tuple[str, WorkspaceMeta]: ...

    async def apply_patch_in_worktree(
        self, patch: str, meta: WorkspaceMeta, target_dir: Path | None = None
    ) -> Path: ...


@runtime_checkable
class CheckpointStore(Protocol):
    """Durable persistence interface for runs, steps, tool calls, and approvals."""

    async def initialize(self) -> None: ...

    async def save_checkpoint(self, checkpoint: RunCheckpoint) -> None: ...

    async def get_checkpoint(self, run_id: UUID) -> RunCheckpoint | None: ...

    async def list_checkpoints(
        self,
        *,
        status: str | None = None,
        workflow_name: str | None = None,
        limit: int = 50,
    ) -> list[RunCheckpoint]: ...

    async def save_step(self, step: StepRecord) -> None: ...

    async def list_steps(self, run_id: UUID) -> list[StepRecord]: ...

    async def save_tool_call(self, record: ToolCallRecord) -> None: ...

    async def list_tool_calls(self, run_id: UUID) -> list[ToolCallRecord]: ...

    async def create_approval(self, approval: ApprovalRequestRecord) -> None: ...

    async def get_approval(self, approval_id: UUID) -> ApprovalRequestRecord | None: ...

    async def list_approvals(
        self, *, status: ApprovalStatus | None = None, run_id: UUID | None = None
    ) -> list[ApprovalRequestRecord]: ...

    async def decide_approval(
        self,
        approval_id: UUID,
        *,
        approved: bool,
        decided_by: str,
        reason: str = "",
    ) -> ApprovalRequestRecord: ...


@runtime_checkable
class StepExecutor(Protocol):
    """Protocol allowing Workflow steps to run built-in Agent or BYO-Agent runners."""

    @property
    def name(self) -> str: ...

    async def execute_step(
        self,
        prompt: str,
        *,
        context: dict[str, Any],
        run_id: UUID | None = None,
        workflow_name: str = "workflow",
        step_index: int = 0,
        budget: BudgetConfig | None = None,
        store: CheckpointStore | None = None,
    ) -> RunResult: ...


@runtime_checkable
class CommandExecutor(Protocol):
    """Two-tier command execution boundary (SubprocessExecutor vs DockerSandboxExecutor)."""

    async def run_command(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float = 60.0,
        env_overrides: dict[str, str] | None = None,
    ) -> CommandResult: ...


@runtime_checkable
class TaskDispatcher(Protocol):
    """Background task queue abstraction (InProcess vs Celery)."""

    async def dispatch_run(
        self, run_id: UUID, workflow_name: str, payload: dict[str, Any]
    ) -> str: ...

    async def dispatch_resume(self, run_id: UUID, approval_id: UUID) -> str: ...


@runtime_checkable
class NotificationProvider(Protocol):
    """Outbound notification delivery channel protocol."""

    @property
    def name(self) -> str: ...

    async def send(self, event: NotificationEvent, target: str) -> DeliveryReceipt: ...
