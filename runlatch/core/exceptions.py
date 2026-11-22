"""Typed domain exceptions for runlatch core."""

from __future__ import annotations

from uuid import UUID


class RunLatchError(Exception):
    """Base exception for all runlatch errors."""


class BudgetExceededError(RunLatchError):
    """Raised when a run exceeds max_steps, max_tokens, or max_cost_usd."""

    def __init__(
        self, message: str, *, metric: str, current: float | int, limit: float | int
    ) -> None:
        super().__init__(message)
        self.metric = metric
        self.current = current
        self.limit = limit


class LoopDetectedError(RunLatchError):
    """Raised when an agent repeatedly invokes the same tool with identical arguments."""

    def __init__(self, tool_name: str, count: int) -> None:
        super().__init__(
            f"Infinite tool loop detected: '{tool_name}' called {count} "
            "times with identical arguments."
        )
        self.tool_name = tool_name
        self.count = count


class PolicyDeniedError(RunLatchError):
    """Raised when a tool invocation is explicitly denied by PolicyEngine."""

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"Policy denied tool '{tool_name}': {reason}")
        self.tool_name = tool_name
        self.reason = reason


class ToolValidationError(RunLatchError):
    """Raised when tool arguments fail Pydantic schema validation."""


class SandboxViolationError(RunLatchError):
    """Raised when a file or command operation violates sandbox boundaries."""


class PatchTooLargeError(RunLatchError):
    """Raised when a workspace git patch exceeds the 512 KB cap or contains binary data."""


class ApprovalRequiredSignal(RunLatchError):
    """Signal raised when a tool call requires Human-in-the-Loop approval."""

    def __init__(self, run_id: UUID, approval_id: UUID, tool_name: str) -> None:
        super().__init__(
            f"Run {run_id} paused waiting for human approval {approval_id} on tool '{tool_name}'."
        )
        self.run_id = run_id
        self.approval_id = approval_id
        self.tool_name = tool_name
